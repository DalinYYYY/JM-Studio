"""
关节电机通信协议 - 可执行参考实现 (独立模块, 无外部依赖)

适用范围:
  - 串口 (RS232 / UART): A5 5A + LEN + HDR_CHK + 数据 + CRC16-XMODEM
  - CAN 总线: 扩展帧 ID = (CMD<<8)|motor_id, 单帧 8B / 多帧分包 / MIT 定点压缩
  - 虚拟电机 (本机回环): 同串口协议, 但下位机由 VirtualMotor 类模拟

可直接 import 使用:
  from tools.waveform.protocol_reference import (
      FrameCodec, FrameDecoder, JmCmd, JmErr, JmTlmBit,
      FeedbackData, parse_telemetry, parse_read_reply,
      pack_command, MIT_PACK, MIT_UNPACK,
  )

与项目内 jmproto/ 模块功能等价, 但本模块零依赖 (仅用标准库 struct/enum/threading/queue),
可拷贝到任意上位机工程独立使用。

协议详细说明见同目录 protocol_reference.md。
"""

from __future__ import annotations

import struct
import threading
import time
from collections import deque
from enum import IntEnum
from typing import Optional, Iterator, Tuple, Dict, List


# ============================================================================
# 1. CRC16-CCITT/XMODEM 校验
#    与固件 User/Common/crc16.c 完全一致:
#    多项式 0x1021, 初值 0x0000, MSB-first, 无输入/输出反转, 无最终异或。
# ============================================================================

def _build_crc16_table() -> List[int]:
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
        table.append(crc)
    return table


_CRC16_TABLE = _build_crc16_table()


def crc16_calc(data: bytes) -> int:
    """计算 CRC16-XMODEM, 返回 16 位校验值 (与固件 crc16_calc 一致)。"""
    crc = 0x0000
    for b in data:
        crc = ((crc << 8) ^ _CRC16_TABLE[((crc >> 8) ^ b) & 0xFF]) & 0xFFFF
    return crc & 0xFFFF


# ============================================================================
# 2. 小端编解码助手 (与固件 jm_proto.h 的 jm_rd_*/jm_wr_* 一致)
#    多字节统一小端, 浮点为 IEEE-754 f32。
# ============================================================================

def rd_u8(p: bytes, off: int = 0) -> int: return p[off]
def rd_i8(p: bytes, off: int = 0) -> int: return struct.unpack_from('<b', p, off)[0]
def rd_u16(p: bytes, off: int = 0) -> int: return struct.unpack_from('<H', p, off)[0]
def rd_i16(p: bytes, off: int = 0) -> int: return struct.unpack_from('<h', p, off)[0]
def rd_u32(p: bytes, off: int = 0) -> int: return struct.unpack_from('<I', p, off)[0]
def rd_i32(p: bytes, off: int = 0) -> int: return struct.unpack_from('<i', p, off)[0]
def rd_f32(p: bytes, off: int = 0) -> float: return struct.unpack_from('<f', p, off)[0]

def wr_u8(v: int) -> bytes: return struct.pack('<B', v & 0xFF)
def wr_i8(v: int) -> bytes: return struct.pack('<b', v)
def wr_u16(v: int) -> bytes: return struct.pack('<H', v & 0xFFFF)
def wr_i16(v: int) -> bytes: return struct.pack('<h', v)
def wr_u32(v: int) -> bytes: return struct.pack('<I', v & 0xFFFFFFFF)
def wr_i32(v: int) -> bytes: return struct.pack('<i', v)
def wr_f32(v: float) -> bytes: return struct.pack('<f', v)


_TYPE_INFO: Dict[str, Tuple[int, bool, str]] = {
    'u8':  (1, False, '<B'),
    'i8':  (1, False, '<b'),
    'u16': (2, False, '<H'),
    'i16': (2, False, '<h'),
    'u32': (4, False, '<I'),
    'i32': (4, False, '<i'),
    'f32': (4, True,  '<f'),
}


def pack_value(dtype: str, value) -> bytes:
    """按类型名打包单值为小端字节 (char[N] 按 utf-8 定长补零)。"""
    d = dtype.strip().lower()
    if d in _TYPE_INFO:
        _, is_float, fmt = _TYPE_INFO[d]
        return struct.pack(fmt, float(value) if is_float else int(value))
    if d.startswith('char[') and d.endswith(']'):
        try:
            n = int(d[5:-1])
        except ValueError:
            n = 0
        raw = str(value).encode('utf-8')[:n]
        return raw.ljust(n, b'\x00')
    return struct.pack('<f', float(value))


def unpack_value(dtype: str, raw: bytes):
    """按类型名从小端字节解出值 (char[N] 返回去尾零字符串)。"""
    d = dtype.strip().lower()
    if d in _TYPE_INFO:
        n, _, fmt = _TYPE_INFO[d]
        if len(raw) < n:
            return None
        return struct.unpack(fmt, raw[:n])[0]
    if d.startswith('char['):
        return raw.rstrip(b'\x00').decode('utf-8', errors='replace')
    return None


# ============================================================================
# 3. 命令码 / 错误码 / 遥测位 / 状态机 枚举
#    忠实转写固件 jm_cmd_def.h / state_define.h
# ============================================================================

class JmCmd(IntEnum):
    """关节电机命令码 (CMD 字节)"""
    # 系统控制 0x00~0x0F
    IDLE = 0x00
    HOLD = 0x01
    BRAKE = 0x02
    ESTOP = 0x03
    ENABLE = 0x04
    DISABLE = 0x05
    STOP = 0x06

    # 运动控制 0x10~0x2F
    OPEN_LOOP = 0x10
    CURRENT = 0x11
    TORQUE = 0x12
    MIT = 0x13
    VELOCITY = 0x14
    POSITION = 0x15
    POSITION_VELOCITY = 0x16
    POSITION_TORQUE = 0x17
    VELOCITY_TORQUE = 0x18
    DUTY_CYCLE = 0x19
    VOLTAGE_VECTOR = 0x1A
    FIELD_WEAKENING = 0x1B
    SENSORLESS = 0x1C

    # 高级力控 0x30~0x4F
    IMPEDANCE = 0x30
    ADMITTANCE = 0x31
    FORCE_CONTROL = 0x32
    FORCE_POSITION_HYBRID = 0x33
    GRAVITY_COMPENSATION = 0x34
    COLLISION_DETECTION = 0x35
    ZERO_FORCE = 0x36
    CONSTANT_FORCE = 0x37
    VARIABLE_IMPEDANCE = 0x38
    ADAPTIVE_GRAVITY_COMP = 0x39
    LANDING_BUFFER = 0x3A

    # 轨迹同步 0x50~0x6F
    PVT = 0x50
    CUBIC_SPLINE = 0x51
    TRAPEZOIDAL_TRAJ = 0x52
    S_CURVE_TRAJ = 0x53
    HOMING = 0x54
    CANOPEN_SYNC = 0x55
    ETHERCAT_CSP = 0x56
    ETHERCAT_CSV = 0x57
    ETHERCAT_CST = 0x58
    PP = 0x59
    PV = 0x5A
    PT = 0x5B
    ELECTRONIC_GEAR = 0x5C
    ELECTRONIC_CAM = 0x5D

    # 特殊应用与测试 0x70~0x8F
    STEP_DIR = 0x70
    ANALOG_INPUT = 0x71
    PWM_INPUT = 0x72
    JOG = 0x73
    SAFE_TEACH = 0x74
    TEST_AGING = 0x75
    TEST_SWEEP_FREQ = 0x76
    TEST_COGGING = 0x77
    TEST_FRICTION = 0x78
    TEST_INERTIA = 0x79
    TEST_CURRENT_LOOP = 0x7A
    TEST_VELOCITY_LOOP = 0x7B

    # 校准 0x90~0xAF
    CALIB_MOTOR_PARAM = 0x90
    CALIB_ENCODER_OFFSET = 0x91
    CALIB_ENCODER_LINEARITY = 0x92
    CALIB_TORQUE_CONST = 0x93
    CALIB_COGGING_COMP = 0x94
    CALIB_FRICTION_COMP = 0x95
    CALIB_INERTIA = 0x96
    CALIB_ADC_OFFSET = 0x97
    CALIB_ADC_GAIN = 0x98
    CALIB_CURRENT_SENSOR = 0x99
    CALIB_TEMPERATURE = 0x9A
    CALIB_FULL_AUTO = 0x9B

    # 系统诊断 0xB0~0xBF
    CLEAR_FAULT = 0xB0
    DIAGNOSTIC = 0xB1
    ENTER_BOOTLOADER = 0xB2
    SAVE_CONFIG = 0xB3
    FACTORY_RESET = 0xB4
    START_LOG = 0xB5
    STOP_LOG = 0xB6
    HIGH_SPEED_DAQ = 0xB7
    SINGLE_STEP = 0xB8

    # 反馈查询 0xC0~0xCF
    READ_FEEDBACK = 0xC0
    READ_STATE = 0xC1
    READ_PHASE_CURRENT = 0xC2
    READ_DQ_CURRENT = 0xC3
    READ_BUS = 0xC4
    READ_TEMPERATURE = 0xC5
    READ_POS_VEL = 0xC6
    READ_MULTITURN = 0xC7
    READ_FAULT = 0xC8
    READ_DEBUG = 0xC9
    TELEMETRY = 0xCA
    SET_TELEMETRY = 0xCB

    # 设备信息 0xD0~0xDF
    READ_DEV_INFO = 0xD0
    READ_DEV_NAME = 0xD1
    HEARTBEAT = 0xD2

    # 参数读写 0xE0~0xEF
    PARAM_READ = 0xE0
    PARAM_WRITE = 0xE1
    PARAM_READ_BULK = 0xE2
    PARAM_WRITE_BULK = 0xE3
    PARAM_SAVE = 0xE4
    PARAM_RESET = 0xE5

    # CAN 管理与通用 0xF0~0xFF
    SET_CAN_ID = 0xF0
    SET_BAUDRATE = 0xF1
    BROADCAST_SYNC = 0xF2
    NACK = 0xFE


class JmErr(IntEnum):
    """错误码 (NACK 0xFE 的 err_code 字段)"""
    OK = 0x00
    UNSUPPORTED = 0x01
    OUT_OF_RANGE = 0x02
    STATE_DENY = 0x03
    BAD_PARAM_ID = 0x04
    CRC = 0x05
    LENGTH = 0x06
    READ_ONLY = 0x07
    FLASH = 0x08
    FAULT_STATE = 0x09
    CALIB_BUSY = 0x0A


JMERR_CN: Dict[int, str] = {
    JmErr.OK:           '成功',
    JmErr.UNSUPPORTED:  '命令不支持',
    JmErr.OUT_OF_RANGE: '参数超出范围',
    JmErr.STATE_DENY:   '状态拒绝(当前状态不允许此操作)',
    JmErr.BAD_PARAM_ID: '参数ID无效',
    JmErr.CRC:          'CRC校验失败',
    JmErr.LENGTH:       '载荷长度错误',
    JmErr.READ_ONLY:    '参数只读',
    JmErr.FLASH:        'FLASH读写错误',
    JmErr.FAULT_STATE:  '故障状态(需先清除故障)',
    JmErr.CALIB_BUSY:   '标定忙(标定进行中)',
}


class JmTlmBit:
    """同步遥测分组位掩码 (0xCA/0xCB 共用), 与固件 jm_telemetry_bit_e 一致"""
    POS_VEL    = 1 << 0
    DQ         = 1 << 1
    PHASE      = 1 << 2
    BUS        = 1 << 3
    TEMP       = 1 << 4
    MULTITURN  = 1 << 5
    TORQUE     = 1 << 6
    FAULT      = 1 << 7
    STATE      = 1 << 8
    DEBUG      = 1 << 9

    # (位值, 中文标签, 字段名列表, 各字段字节数) - 供波形上位机生成勾选框
    ITEMS = [
        (POS_VEL,   "位置/速度",     ['pos', 'vel'],                  [4, 4]),
        (DQ,        "DQ电流",        ['id', 'iq'],                     [4, 4]),
        (PHASE,     "三相电流",      ['ia', 'ib', 'ic'],               [4, 4, 4]),
        (BUS,       "母线",          ['vbus', 'ibus', 'power'],        [4, 4, 4]),
        (TEMP,      "温度",          ['temp_fet', 'temp_motor'],       [4, 4]),
        (MULTITURN, "多圈",          ['multiturn', 'single'],          [4, 4]),
        (TORQUE,    "力矩",          ['torque'],                       [4]),
        (FAULT,     "故障/警告",     ['fault_mask', 'warn_mask'],      [4, 4]),
        (STATE,     "状态机",        ['top_fsm', 'run_state',
                                     'ctrl_mode', 'enable',
                                     'motion_state'],                 [1, 1, 1, 1, 1]),
        (DEBUG,     "调试通道",      ['debug'],                        [-1]),  # 变长 f32 数组
    ]


class JmParamType(IntEnum):
    """参数类型码 (0xE0 读应答的 type 字段)"""
    U8 = 0
    I8 = 1
    U16 = 2
    I16 = 3
    U32 = 4
    I32 = 5
    F32 = 6
    STR = 7


class TopFsm(IntEnum):
    """顶层主状态 (与固件 state_define.h:top_fsm_e 一致, 数值即上报 top_fsm)"""
    INIT = 0
    SAFETY = 1
    FAULT = 2
    IDLE = 3
    READY = 4
    RUN = 5
    CALIB = 6
    CONFIG = 7
    BOOTLOADER = 8


TOP_FSM_CN = {
    TopFsm.INIT: "初始化", TopFsm.SAFETY: "急停/安全", TopFsm.FAULT: "故障",
    TopFsm.IDLE: "待机", TopFsm.READY: "就绪", TopFsm.RUN: "运行",
    TopFsm.CALIB: "校准", TopFsm.CONFIG: "配置", TopFsm.BOOTLOADER: "升级",
}


class RunState(IntEnum):
    """运行子状态 (与固件 state_define.h:run_state_e 一致, 数值即上报 run_state)"""
    IDLE = 0
    OPEN_LOOP = 1
    CURRENT = 2
    TORQUE = 3
    MIT = 4
    VELOCITY = 5
    POSITION = 6
    POSITION_VELOCITY = 7
    POSITION_TORQUE = 8
    VELOCITY_TORQUE = 9
    DUTY_CYCLE = 10
    VOLTAGE_VECTOR = 11
    FIELD_WEAKENING = 12
    SENSORLESS = 13
    IMPEDANCE = 14
    ADMITTANCE = 15
    FORCE_CONTROL = 16
    FORCE_POSITION_HYBRID = 17
    GRAVITY_COMPENSATION = 18
    COLLISION_DETECTION = 19
    ZERO_FORCE = 20
    CONSTANT_FORCE = 21
    VARIABLE_IMPEDANCE = 22
    ADAPTIVE_GRAVITY_COMP = 23
    LANDING_BUFFER = 24
    PVT = 25
    CUBIC_SPLINE = 26
    TRAPEZOIDAL_TRAJ = 27
    S_CURVE_TRAJ = 28
    HOMING = 29
    ELECTRONIC_GEAR = 30
    ELECTRONIC_CAM = 31
    STEP_DIR = 32
    ANALOG_INPUT = 33
    PWM_INPUT = 34
    JOG = 35
    SAFE_TEACH = 36
    TEST_AGING = 37
    TEST_SWEEP_FREQ = 38
    TEST_COGGING = 39
    TEST_FRICTION = 40
    TEST_INERTIA = 41
    DIAGNOSTIC = 42
    HIGH_SPEED_DAQ = 43
    SINGLE_STEP = 44


RUN_STATE_CN = {
    RunState.IDLE: "空闲", RunState.OPEN_LOOP: "开环", RunState.CURRENT: "电流环",
    RunState.TORQUE: "力矩环", RunState.MIT: "MIT", RunState.VELOCITY: "速度环",
    RunState.POSITION: "位置环", RunState.POSITION_VELOCITY: "位置+速度",
    RunState.POSITION_TORQUE: "位置+力矩", RunState.VELOCITY_TORQUE: "速度+力矩",
    RunState.DUTY_CYCLE: "占空比", RunState.VOLTAGE_VECTOR: "电压矢量",
    RunState.FIELD_WEAKENING: "弱磁", RunState.SENSORLESS: "无感FOC",
    RunState.IMPEDANCE: "阻抗", RunState.ADMITTANCE: "导纳",
    RunState.FORCE_CONTROL: "力控", RunState.FORCE_POSITION_HYBRID: "力位混合",
    RunState.GRAVITY_COMPENSATION: "重力补偿", RunState.COLLISION_DETECTION: "碰撞检测",
    RunState.ZERO_FORCE: "零力", RunState.CONSTANT_FORCE: "恒力",
    RunState.VARIABLE_IMPEDANCE: "变阻抗",
    RunState.ADAPTIVE_GRAVITY_COMP: "自适应重力补偿",
    RunState.LANDING_BUFFER: "落地缓冲", RunState.PVT: "PVT",
    RunState.CUBIC_SPLINE: "三次样条", RunState.TRAPEZOIDAL_TRAJ: "梯形轨迹",
    RunState.S_CURVE_TRAJ: "S曲线", RunState.HOMING: "回零",
    RunState.ELECTRONIC_GEAR: "电子齿轮", RunState.ELECTRONIC_CAM: "电子凸轮",
    RunState.STEP_DIR: "脉冲方向", RunState.ANALOG_INPUT: "模拟量",
    RunState.PWM_INPUT: "PWM输入", RunState.JOG: "点动",
    RunState.SAFE_TEACH: "安全示教", RunState.TEST_AGING: "老化测试",
    RunState.TEST_SWEEP_FREQ: "扫频测试", RunState.TEST_COGGING: "齿槽测试",
    RunState.TEST_FRICTION: "摩擦测试", RunState.TEST_INERTIA: "惯量测试",
    RunState.DIAGNOSTIC: "诊断", RunState.HIGH_SPEED_DAQ: "高速采集",
    RunState.SINGLE_STEP: "单步调试",
}


# 防误触魔数
MAGIC_BOOTLOADER = 0xB00710AD
MAGIC_FACTORY_RESET = 0xFAC70F5F


def cmd_name(cmd: int) -> str:
    try: return JmCmd(cmd).name
    except ValueError: return f"0x{cmd:02X}"


def err_name(err: int) -> str:
    try: return JmErr(err).name
    except ValueError: return f"0x{err:02X}"


def err_name_cn(err: int) -> str:
    try:
        e = JmErr(int(err))
        cn = JMERR_CN.get(e)
        return f'{e.name}({cn})' if cn else e.name
    except ValueError:
        return f'0x{int(err):02X}'


def top_fsm_name(v: int) -> str:
    try: return TOP_FSM_CN[TopFsm(v)]
    except (ValueError, KeyError): return f"({v})"


def run_state_name(v: int) -> str:
    try: return RUN_STATE_CN[RunState(v)]
    except (ValueError, KeyError): return f"({v})"


# ============================================================================
# 4. 串口帧编解码 (与固件 packer_parser 一致)
#    帧: A5 5A + LEN(2B 大端) + HDR_CHK(1B) + CMD+Payload + CRC16(2B 小端)
#    LEN = CMD(1) + DATA(n) 总字节数
#    HDR_CHK = (STX_H + STX_L + LEN_H + LEN_L) & 0xFF
#    CRC16(XMODEM) 覆盖数据区 (CMD + DATA), 小端落帧
# ============================================================================

STX_H = 0xA5
STX_L = 0x5A
MAX_PACK_SIZE = 1024
PACK_OVERHEAD = 7  # 帧头2 + 长度2 + 头校验1 + CRC2


class FrameCodec:
    """串口帧封包器"""

    @staticmethod
    def pack(cmd: int, payload: bytes = b'') -> bytes:
        data = bytes([cmd & 0xFF]) + payload
        length = len(data)
        head_chk = (STX_H + STX_L + ((length >> 8) & 0xFF) + (length & 0xFF)) & 0xFF
        crc = crc16_calc(data)

        frame = bytearray()
        frame.append(STX_H)
        frame.append(STX_L)
        frame.append((length >> 8) & 0xFF)
        frame.append(length & 0xFF)
        frame.append(head_chk)
        frame.extend(data)
        frame.append(crc & 0xFF)
        frame.append((crc >> 8) & 0xFF)
        return bytes(frame)


class FrameDecoder:
    """串口帧解包器 (状态机, 流式 feed)"""

    class State(IntEnum):
        HEADER_HIGH = 0
        HEADER_LOW = 1
        LEN_HIGH = 2
        LEN_LOW = 3
        HEADER_CHK = 4
        DATA_RECV = 5
        CRC_LOW = 6
        CRC_HIGH = 7

    def __init__(self):
        self._state = self.State.HEADER_HIGH
        self._calc = 0
        self._flen = 0
        self._cnt = 0
        self._data = bytearray(MAX_PACK_SIZE)
        self._rx_crc = 0

    def reset(self):
        """复位状态机 (打开串口时调用, 清除残留半帧)"""
        self._state = self.State.HEADER_HIGH
        self._cnt = 0

    def feed(self, raw: bytes) -> Iterator[Tuple[int, bytes]]:
        """喂入原始字节, 解出完整帧后 yield (cmd, payload)"""
        for d in raw:
            result = self._decode(d)
            if result is not None:
                yield result

    def _decode(self, d: int):
        if self._state == self.State.HEADER_HIGH:
            if d == STX_H:
                self._calc = STX_H
                self._state = self.State.HEADER_LOW
        elif self._state == self.State.HEADER_LOW:
            if d == STX_L:
                self._calc += STX_L
                self._state = self.State.LEN_HIGH
            else:
                self._state = self.State.HEADER_HIGH
        elif self._state == self.State.LEN_HIGH:
            self._flen = (d << 8)
            self._calc += d
            self._state = self.State.LEN_LOW
        elif self._state == self.State.LEN_LOW:
            self._flen |= d
            self._calc += d
            if self._flen > MAX_PACK_SIZE:
                self._state = self.State.HEADER_HIGH
            else:
                self._state = self.State.HEADER_CHK
        elif self._state == self.State.HEADER_CHK:
            if d != (self._calc & 0xFF):
                self._state = self.State.HEADER_HIGH
                return None
            self._cnt = 0
            self._state = self.State.CRC_LOW if self._flen == 0 else self.State.DATA_RECV
        elif self._state == self.State.DATA_RECV:
            self._data[self._cnt] = d
            self._cnt += 1
            if self._cnt >= self._flen:
                self._state = self.State.CRC_LOW
        elif self._state == self.State.CRC_LOW:
            self._rx_crc = d
            self._state = self.State.CRC_HIGH
        elif self._state == self.State.CRC_HIGH:
            self._rx_crc |= (d << 8)
            self._state = self.State.HEADER_HIGH
            data_bytes = bytes(self._data[:self._flen])
            if crc16_calc(data_bytes) == self._rx_crc and self._flen > 0:
                return self._data[0], bytes(self._data[1:self._flen])
        return None


# ============================================================================
# 5. CAN 总线帧编解码 (依据固件 jm_proto_can.h)
#    仲裁 ID (扩展帧 29 位): ID = (CMD << 8) | motor_id
#    单帧: 数据区 ≤ 8B, 直接为命令载荷 (不含 CMD)
#    多帧: data[0] = (末帧标志 << 7) | (序号 & 0x7F), data[1..7] = 7B 分片
#    MIT (0x13/0x30) 与反馈帧可定点压缩进 8B
# ============================================================================

CAN_SEG_LAST = 0x80
CAN_SEG_SEQ_MASK = 0x7F
CAN_SEG_PAYLOAD = 7
CAN_SINGLE_MAX = 8

# MIT 定点压缩范围
MIT_POS_MIN,  MIT_POS_MAX  = -12.5, 12.5
MIT_VEL_MIN,  MIT_VEL_MAX  = -65.0, 65.0
MIT_KP_MIN,   MIT_KP_MAX   = 0.0,   500.0
MIT_KD_MIN,   MIT_KD_MAX   = 0.0,   5.0
MIT_TFF_MIN,  MIT_TFF_MAX  = -50.0, 50.0
FB_TEMP_MIN,  FB_TEMP_MAX  = -40.0, 215.0


def can_make_id(cmd: int, motor_id: int) -> int:
    return ((cmd & 0xFF) << 8) | (motor_id & 0xFF)


def can_get_cmd(can_id: int) -> int:
    return (can_id >> 8) & 0xFF


def can_get_motor_id(can_id: int) -> int:
    return can_id & 0xFF


def _to_fixed(v: float, vmin: float, vmax: float, bits: int) -> int:
    span = vmax - vmin
    if span <= 0:
        return 0
    raw = (v - vmin) / span * ((1 << bits) - 1)
    return int(max(0, min((1 << bits) - 1, raw)))


def _from_fixed(raw: int, vmin: float, vmax: float, bits: int) -> float:
    span = vmax - vmin
    return vmin + raw / ((1 << bits) - 1) * span if span > 0 else vmin


def MIT_PACK(pos: float, vel: float, kp: float, kd: float, tff: float) -> bytes:
    """MIT/IMPEDANCE 命令的 8 字节定点压缩 (与固件 jm_proto_can.h 一致)。
    布局: pos(16) | vel(12) | kp(12) | kd(12) | tff(12) = 64 bit"""
    p = _to_fixed(pos, MIT_POS_MIN, MIT_POS_MAX, 16)
    v = _to_fixed(vel, MIT_VEL_MIN, MIT_VEL_MAX, 12)
    kp = _to_fixed(kp, MIT_KP_MIN, MIT_KP_MAX, 12)
    kd = _to_fixed(kd, MIT_KD_MIN, MIT_KD_MAX, 12)
    tf = _to_fixed(tff, MIT_TFF_MIN, MIT_TFF_MAX, 12)
    # 大端拼装: [pos16][vel12][kp12][kd12][tff12]
    u64 = (p << 48) | (v << 36) | (kp << 24) | (kd << 12) | tf
    return u64.to_bytes(8, byteorder='big')


def MIT_UNPACK(data: bytes) -> Tuple[float, float, float, float, float]:
    """解压 MIT 8 字节为 (pos, vel, kp, kd, tff)"""
    if len(data) < 8:
        raise ValueError("MIT 解压需要 8 字节")
    u64 = int.from_bytes(data[:8], byteorder='big')
    p = (u64 >> 48) & 0xFFFF
    v = (u64 >> 36) & 0xFFF
    kp = (u64 >> 24) & 0xFFF
    kd = (u64 >> 12) & 0xFFF
    tf = u64 & 0xFFF
    return (
        _from_fixed(p,  MIT_POS_MIN,  MIT_POS_MAX,  16),
        _from_fixed(v,  MIT_VEL_MIN,  MIT_VEL_MAX,  12),
        _from_fixed(kp, MIT_KP_MIN,   MIT_KP_MAX,   12),
        _from_fixed(kd, MIT_KD_MIN,   MIT_KD_MAX,   12),
        _from_fixed(tf, MIT_TFF_MIN,  MIT_TFF_MAX,  12),
    )


def FB_CAN_PACK(pos: float, vel: float, torque: float, temp: float, err: int) -> bytes:
    """反馈帧压缩进 8 字节: pos16 vel16 tq16 temp8 err8 (与固件一致)"""
    p = _to_fixed(pos, MIT_POS_MIN, MIT_POS_MAX, 16)
    v = _to_fixed(vel, MIT_VEL_MIN, MIT_VEL_MAX, 16)
    t = _to_fixed(torque, MIT_TFF_MIN, MIT_TFF_MAX, 16)
    tp = _to_fixed(temp, FB_TEMP_MIN, FB_TEMP_MAX, 8)
    u64 = (p << 48) | (v << 32) | (t << 16) | ((tp & 0xFF) << 8) | (err & 0xFF)
    return u64.to_bytes(8, byteorder='big')


def FB_CAN_UNPACK(data: bytes) -> Tuple[float, float, float, float, int]:
    if len(data) < 8:
        raise ValueError("FB 解压需要 8 字节")
    u64 = int.from_bytes(data[:8], byteorder='big')
    p = (u64 >> 48) & 0xFFFF
    v = (u64 >> 32) & 0xFFFF
    t = (u64 >> 16) & 0xFFFF
    tp = (u64 >> 8) & 0xFF
    err = u64 & 0xFF
    return (
        _from_fixed(p,  MIT_POS_MIN,  MIT_POS_MAX, 16),
        _from_fixed(v,  MIT_VEL_MIN,  MIT_VEL_MAX, 16),
        _from_fixed(t,  MIT_TFF_MIN,  MIT_TFF_MAX, 16),
        _from_fixed(tp, FB_TEMP_MIN, FB_TEMP_MAX, 8),
        err,
    )


def can_pack_frames(cmd: int, payload: bytes) -> List[Tuple[int, bytes]]:
    """将一条逻辑命令拆成 CAN 帧列表 [(can_id, data), ...]。
    单帧 (≤8B): data 即 payload (不补零, DLC=len); 多帧 (>8B): 每帧 8B = 1B 控制 + 7B 分片

    注: 单帧与多帧首帧 (seq=0, is_last=0) 在 8B 满载时字节完全相同, 协议层无法区分。
    接收端需要按 cmd 查表 (载荷长度) 决定单帧还是多帧路径, 或用 DLC < 8 作为单帧判据。
    本函数输出保证: 单帧 DLC = 实际字节数 (≤ 8), 多帧每帧 DLC = 8。
    """
    can_id = can_make_id(cmd, 0)  # motor_id 由调用方 OR 进来
    if len(payload) <= CAN_SINGLE_MAX:
        return [(can_id, payload)]  # 保持原长, 由 DLC 区分
    frames = []
    seq = 0
    for i in range(0, len(payload), CAN_SEG_PAYLOAD):
        chunk = payload[i:i + CAN_SEG_PAYLOAD]
        is_last = (i + CAN_SEG_PAYLOAD) >= len(payload)
        ctrl = (CAN_SEG_LAST if is_last else 0) | (seq & CAN_SEG_SEQ_MASK)
        frames.append((can_id, bytes([ctrl]) + chunk.ljust(CAN_SEG_PAYLOAD, b'\x00')))
        seq += 1
    return frames


class CanReassembler:
    """CAN 多帧重组器: 专为多帧路径设计, 喂入 (cmd, data8), 末帧后 yield (cmd, payload)。

    调用方应在确认是 multi-frame 路径时再喂入本重组器 (单帧应直接处理)。
    判别建议: DLC < 8 → 单帧; DLC == 8 且 data[0] 高位为 1 → 多帧末帧;
              DLC == 8 且 cmd 已有缓冲 → 多帧中间帧/首帧。
    """

    def __init__(self):
        self._buf: Dict[int, bytearray] = {}    # cmd -> 累积字节
        self._seq: Dict[int, int] = {}           # cmd -> 期望序号

    def feed(self, cmd: int, data: bytes) -> Iterator[Tuple[int, bytes]]:
        if len(data) < 1:
            return
        ctrl = data[0]
        seq = ctrl & CAN_SEG_SEQ_MASK
        is_last_flag = bool(ctrl & CAN_SEG_LAST)
        chunk = data[1:1 + CAN_SEG_PAYLOAD]
        if cmd not in self._buf:
            self._buf[cmd] = bytearray()
            self._seq[cmd] = 0
        if seq != self._seq[cmd]:
            # 序列错位, 丢弃本 cmd 上下文
            self._buf.pop(cmd, None)
            self._seq.pop(cmd, None)
            return
        self._buf[cmd].extend(chunk)
        self._seq[cmd] = seq + 1
        if is_last_flag:
            payload = bytes(self._buf.pop(cmd))
            self._seq.pop(cmd, None)
            yield cmd, payload


# ============================================================================
# 6. 命令载荷字段定义 + 数据驱动打包
#    每条命令的字段顺序与类型来自 resources/joint_motor_command_list.csv
#    (字段名:类型[:默认值]; 多字段以 ; 分隔)
# ============================================================================

class Field:
    __slots__ = ['name', 'dtype', 'default']

    def __init__(self, name: str, dtype: str, default=None):
        self.name = name
        self.dtype = dtype.lower()
        self.default = default

    @property
    def is_fixed(self) -> bool:
        return self.default is not None


class CommandSpec:
    __slots__ = ['cmd', 'name', 'category', 'fields', 'note']

    def __init__(self, cmd: int, name: str, category: str, fields: List[Field], note: str = ''):
        self.cmd = cmd
        self.name = name
        self.category = category
        self.fields = fields
        self.note = note

    @property
    def editable_fields(self) -> List[Field]:
        return [f for f in self.fields if not f.is_fixed]


# 内置常用命令的字段定义 (无需 CSV 即可使用; 完整 103 条见 resources/joint_motor_command_list.csv)
BUILTIN_COMMANDS: Dict[int, CommandSpec] = {
    JmCmd.OPEN_LOOP:         CommandSpec(JmCmd.OPEN_LOOP,        "开环电压",     "运动控制", [Field('ud', 'f32'), Field('uq', 'f32')]),
    JmCmd.CURRENT:           CommandSpec(JmCmd.CURRENT,          "电流环",       "运动控制", [Field('id_ref', 'f32'), Field('iq_ref', 'f32')]),
    JmCmd.TORQUE:            CommandSpec(JmCmd.TORQUE,           "力矩环",       "运动控制", [Field('torque', 'f32')]),
    JmCmd.MIT:               CommandSpec(JmCmd.MIT,              "MIT控制",      "运动控制", [Field('pos', 'f32'), Field('vel', 'f32'), Field('kp', 'f32'), Field('kd', 'f32'), Field('tff', 'f32')]),
    JmCmd.VELOCITY:          CommandSpec(JmCmd.VELOCITY,         "速度环",       "运动控制", [Field('vel_ref', 'f32')]),
    JmCmd.POSITION:          CommandSpec(JmCmd.POSITION,         "位置环",       "运动控制", [Field('pos_ref', 'f32')]),
    JmCmd.POSITION_VELOCITY: CommandSpec(JmCmd.POSITION_VELOCITY,"位置+速度前馈","运动控制", [Field('pos', 'f32'), Field('vel_ff', 'f32')]),
    JmCmd.POSITION_TORQUE:   CommandSpec(JmCmd.POSITION_TORQUE,  "位置+力矩限幅","运动控制", [Field('pos', 'f32'), Field('tq_lim', 'f32')]),
    JmCmd.VELOCITY_TORQUE:   CommandSpec(JmCmd.VELOCITY_TORQUE,  "速度+力矩限幅","运动控制", [Field('vel', 'f32'), Field('tq_lim', 'f32')]),
    JmCmd.DUTY_CYCLE:        CommandSpec(JmCmd.DUTY_CYCLE,       "占空比控制",   "运动控制", [Field('duty', 'f32')]),
    JmCmd.VOLTAGE_VECTOR:    CommandSpec(JmCmd.VOLTAGE_VECTOR,   "电压矢量",     "运动控制", [Field('u_alpha', 'f32'), Field('u_beta', 'f32')]),
    JmCmd.FIELD_WEAKENING:   CommandSpec(JmCmd.FIELD_WEAKENING,  "弱磁控制",     "运动控制", [Field('id_weak', 'f32'), Field('iq_ref', 'f32')]),
    JmCmd.SENSORLESS:        CommandSpec(JmCmd.SENSORLESS,       "无感FOC",      "运动控制", [Field('vel_ref', 'f32')]),
    JmCmd.IMPEDANCE:         CommandSpec(JmCmd.IMPEDANCE,        "阻抗控制",     "高级力控", [Field('pos', 'f32'), Field('vel', 'f32'), Field('kp', 'f32'), Field('kd', 'f32'), Field('tff', 'f32')]),
    JmCmd.ADMITTANCE:        CommandSpec(JmCmd.ADMITTANCE,       "导纳控制",     "高级力控", [Field('force', 'f32'), Field('mass', 'f32'), Field('damp', 'f32'), Field('stiff', 'f32')]),
    JmCmd.FORCE_CONTROL:     CommandSpec(JmCmd.FORCE_CONTROL,    "纯力控制",     "高级力控", [Field('force', 'f32')]),
    JmCmd.FORCE_POSITION_HYBRID: CommandSpec(JmCmd.FORCE_POSITION_HYBRID, "力位混合", "高级力控", [Field('pos', 'f32'), Field('force', 'f32'), Field('sel_mask', 'u32')]),
    JmCmd.GRAVITY_COMPENSATION: CommandSpec(JmCmd.GRAVITY_COMPENSATION, "重力补偿", "高级力控", []),
    JmCmd.COLLISION_DETECTION:  CommandSpec(JmCmd.COLLISION_DETECTION, "碰撞检测", "高级力控", [Field('threshold', 'f32'), Field('enable', 'u8')]),
    JmCmd.ZERO_FORCE:        CommandSpec(JmCmd.ZERO_FORCE,       "零力模式",     "高级力控", []),
    JmCmd.CONSTANT_FORCE:    CommandSpec(JmCmd.CONSTANT_FORCE,   "恒力控制",     "高级力控", [Field('force', 'f32')]),
    JmCmd.VARIABLE_IMPEDANCE:CommandSpec(JmCmd.VARIABLE_IMPEDANCE,"变阻抗",      "高级力控", [Field('kp', 'f32'), Field('kd', 'f32'), Field('rate', 'f32')]),
    JmCmd.ADAPTIVE_GRAVITY_COMP: CommandSpec(JmCmd.ADAPTIVE_GRAVITY_COMP, "自适应重力补偿", "高级力控", [Field('gain', 'f32')]),
    JmCmd.LANDING_BUFFER:    CommandSpec(JmCmd.LANDING_BUFFER,   "落地缓冲",     "高级力控", [Field('stiffness', 'f32'), Field('damp', 'f32')]),
    JmCmd.PVT:               CommandSpec(JmCmd.PVT,              "PVT插补",      "轨迹同步", [Field('pos', 'f32'), Field('vel', 'f32'), Field('time_ms', 'u32')]),
    JmCmd.TRAPEZOIDAL_TRAJ:  CommandSpec(JmCmd.TRAPEZOIDAL_TRAJ, "梯形轨迹",     "轨迹同步", [Field('target', 'f32'), Field('vmax', 'f32'), Field('acc', 'f32')]),
    JmCmd.S_CURVE_TRAJ:      CommandSpec(JmCmd.S_CURVE_TRAJ,     "S型轨迹",      "轨迹同步", [Field('target', 'f32'), Field('vmax', 'f32'), Field('acc', 'f32'), Field('jerk', 'f32')]),
    JmCmd.HOMING:            CommandSpec(JmCmd.HOMING,           "回零",         "轨迹同步", [Field('method', 'u8')]),
    JmCmd.PP:                CommandSpec(JmCmd.PP,               "轮廓位置",     "轨迹同步", [Field('pos', 'f32'), Field('vel', 'f32')]),
    JmCmd.PV:                CommandSpec(JmCmd.PV,               "轮廓速度",     "轨迹同步", [Field('vel', 'f32'), Field('acc', 'f32')]),
    JmCmd.PT:                CommandSpec(JmCmd.PT,               "轮廓力矩",     "轨迹同步", [Field('torque', 'f32'), Field('slope', 'f32')]),
    JmCmd.ELECTRONIC_GEAR:   CommandSpec(JmCmd.ELECTRONIC_GEAR,  "电子齿轮",     "轨迹同步", [Field('ratio_num', 'i32'), Field('ratio_den', 'i32')]),
    JmCmd.ELECTRONIC_CAM:    CommandSpec(JmCmd.ELECTRONIC_CAM,   "电子凸轮",     "轨迹同步", [Field('cam_table_id', 'u16')]),
    JmCmd.STEP_DIR:          CommandSpec(JmCmd.STEP_DIR,         "脉冲方向",     "特殊应用", [Field('pulse_per_rev', 'u32')]),
    JmCmd.ANALOG_INPUT:      CommandSpec(JmCmd.ANALOG_INPUT,     "模拟量输入",   "特殊应用", [Field('ch', 'u8'), Field('scale', 'f32')]),
    JmCmd.PWM_INPUT:         CommandSpec(JmCmd.PWM_INPUT,        "PWM输入",      "特殊应用", [Field('min_us', 'u16'), Field('max_us', 'u16')]),
    JmCmd.JOG:               CommandSpec(JmCmd.JOG,              "点动",         "特殊应用", [Field('dir', 'i8'), Field('speed', 'f32')]),
    JmCmd.ENTER_BOOTLOADER:  CommandSpec(JmCmd.ENTER_BOOTLOADER,  "进入Bootloader","系统诊断", [Field('magic', 'u32', MAGIC_BOOTLOADER)]),
    JmCmd.FACTORY_RESET:     CommandSpec(JmCmd.FACTORY_RESET,    "恢复出厂",     "系统诊断", [Field('magic', 'u32', MAGIC_FACTORY_RESET)]),
    JmCmd.START_LOG:         CommandSpec(JmCmd.START_LOG,        "开始日志",     "系统诊断", [Field('rate_hz', 'u16'), Field('mask', 'u32')]),
    JmCmd.HIGH_SPEED_DAQ:    CommandSpec(JmCmd.HIGH_SPEED_DAQ,   "高速采集",     "系统诊断", [Field('ch_mask', 'u32'), Field('rate_hz', 'u32')]),
    JmCmd.SET_TELEMETRY:     CommandSpec(JmCmd.SET_TELEMETRY,    "设置遥测",     "反馈查询", [Field('enable', 'u8'), Field('mask', 'u16'), Field('period', 'u16')]),
    JmCmd.PARAM_READ:        CommandSpec(JmCmd.PARAM_READ,       "读单个参数",   "参数读写", [Field('param_id', 'u16')]),
    JmCmd.PARAM_WRITE:       CommandSpec(JmCmd.PARAM_WRITE,      "写单个参数",   "参数读写", [Field('param_id', 'u16')]),  # value 跟在后面
    JmCmd.PARAM_READ_BULK:   CommandSpec(JmCmd.PARAM_READ_BULK,  "批量读参数",   "参数读写", [Field('start_id', 'u16'), Field('count', 'u16')]),
    JmCmd.PARAM_RESET:       CommandSpec(JmCmd.PARAM_RESET,      "参数恢复默认", "参数读写", [Field('param_id', 'u16', 0xFFFF)]),
    JmCmd.SET_CAN_ID:        CommandSpec(JmCmd.SET_CAN_ID,       "设置CAN_ID",   "CAN管理",  [Field('new_id', 'u8')]),
    JmCmd.SET_BAUDRATE:      CommandSpec(JmCmd.SET_BAUDRATE,     "设置波特率",   "CAN管理",  [Field('baud_code', 'u8')]),
}


def pack_command(cmd: int, values: Dict[str, object], spec: Optional[CommandSpec] = None) -> bytes:
    """按命令字段定义打包载荷; 固定字段用其默认值"""
    s = spec or BUILTIN_COMMANDS.get(int(cmd))
    if s is None or not s.fields:
        return b''
    out = bytearray()
    for f in s.fields:
        v = f.default if f.is_fixed else values.get(f.name, 0)
        out += pack_value(f.dtype, v)
    return bytes(out)


# ============================================================================
# 7. 反馈数据结构 + 应答解析
#    每帧解析后填入 FeedbackData 的对应字段 (filled_fields 标记哪些字段有效)
# ============================================================================

class FeedbackData:
    """实时反馈数据 (各字段单位见注释)"""
    __slots__ = [
        'pos', 'vel', 'torque',
        'id', 'iq', 'ia', 'ib', 'ic',
        'vbus', 'ibus', 'power', 'temp_fet', 'temp_motor',
        'multiturn', 'single',
        'fault_mask', 'warn_mask',
        'top_fsm', 'run_state', 'ctrl_mode', 'enable',
        'motion_state',
        'debug',
        'filled_fields',
        'rx_ts',
    ]

    def __init__(self):
        self.pos = 0.0          # rad
        self.vel = 0.0          # rad/s
        self.torque = 0.0       # Nm
        self.id = 0.0           # A
        self.iq = 0.0           # A
        self.ia = 0.0           # A
        self.ib = 0.0           # A
        self.ic = 0.0           # A
        self.vbus = 0.0         # V
        self.ibus = 0.0         # A
        self.power = 0.0        # W
        self.temp_fet = 0.0     # C
        self.temp_motor = 0.0   # C
        self.multiturn = 0      # 多圈计数
        self.single = 0.0       # 单圈位置 rad
        self.fault_mask = 0     # 故障位掩码
        self.warn_mask = 0      # 警告位掩码
        self.top_fsm = 0        # 顶层状态
        self.run_state = 0      # 运行子状态
        self.ctrl_mode = 0      # 控制模式
        self.enable = 0         # 是否使能
        self.motion_state = 0   # 运动子状态 (0=STANDSTILL,1=MOVING,2=DECEL,3=HOLDING,4=BRAKING)
        self.debug = ()         # 调试通道 jm_dbg[] (f32 元组)
        self.filled_fields = ()
        self.rx_ts = 0.0        # 接收时间戳 (s), 波形显示对齐用

    @staticmethod
    def from_feedback_payload(payload: bytes) -> 'FeedbackData':
        """解析 READ_FEEDBACK(0xC0) 应答 (22 字节):
        pos, vel, torque, temp_motor, vbus (f32×5) + fault_mask(u16)"""
        fb = FeedbackData()
        n = 0
        fb.pos = rd_f32(payload, n); n += 4
        fb.vel = rd_f32(payload, n); n += 4
        fb.torque = rd_f32(payload, n); n += 4
        fb.temp_motor = rd_f32(payload, n); n += 4
        fb.vbus = rd_f32(payload, n); n += 4
        fb.fault_mask = rd_u16(payload, n); n += 2
        fb.filled_fields = ('pos', 'vel', 'torque', 'temp_motor', 'vbus', 'fault_mask')
        return fb


def parse_state(payload: bytes) -> Tuple[int, int, int, int]:
    """READ_STATE(0xC1): top_fsm, run_state, ctrl_mode, enable"""
    if len(payload) < 4:
        return 0, 0, 0, 0
    return payload[0], payload[1], payload[2], payload[3]


def parse_read_reply(cmd: int, payload: bytes) -> Tuple[Optional[FeedbackData], Tuple[str, ...]]:
    """解析 0xC2~0xC8 各读命令应答, 返回 (FeedbackData 或 None, 已填充字段名元组)"""
    fb = FeedbackData()
    if cmd == JmCmd.READ_POS_VEL and len(payload) >= 8:
        fb.pos = rd_f32(payload, 0)
        fb.vel = rd_f32(payload, 4)
        fb.filled_fields = ('pos', 'vel')
        return fb, fb.filled_fields
    if cmd == JmCmd.READ_BUS and len(payload) >= 12:
        fb.vbus = rd_f32(payload, 0)
        fb.ibus = rd_f32(payload, 4)
        fb.power = rd_f32(payload, 8)
        fb.filled_fields = ('vbus', 'ibus', 'power')
        return fb, fb.filled_fields
    if cmd == JmCmd.READ_TEMPERATURE and len(payload) >= 8:
        fb.temp_fet = rd_f32(payload, 0)
        fb.temp_motor = rd_f32(payload, 4)
        fb.filled_fields = ('temp_fet', 'temp_motor')
        return fb, fb.filled_fields
    if cmd == JmCmd.READ_DQ_CURRENT and len(payload) >= 8:
        fb.id = rd_f32(payload, 0)
        fb.iq = rd_f32(payload, 4)
        fb.filled_fields = ('id', 'iq')
        return fb, fb.filled_fields
    if cmd == JmCmd.READ_PHASE_CURRENT and len(payload) >= 12:
        fb.ia = rd_f32(payload, 0)
        fb.ib = rd_f32(payload, 4)
        fb.ic = rd_f32(payload, 8)
        fb.filled_fields = ('ia', 'ib', 'ic')
        return fb, fb.filled_fields
    if cmd == JmCmd.READ_MULTITURN and len(payload) >= 8:
        fb.multiturn = rd_i32(payload, 0)
        fb.single = rd_f32(payload, 4)
        fb.filled_fields = ('multiturn', 'single')
        return fb, fb.filled_fields
    if cmd == JmCmd.READ_FAULT and len(payload) >= 8:
        fb.fault_mask = rd_u32(payload, 0)
        fb.warn_mask = rd_u32(payload, 4)
        fb.filled_fields = ('fault_mask', 'warn_mask')
        return fb, fb.filled_fields
    return None, ()


def parse_telemetry(payload: bytes) -> Tuple[FeedbackData, Tuple[str, ...]]:
    """解析同步遥测帧 TELEMETRY(0xCA): mask(u16) + 按位序拼接的数据组。
    位序与固件 jm_telemetry_bit_e 一致。"""
    fb = FeedbackData()
    if len(payload) < 2:
        return fb, ()
    mask = rd_u16(payload, 0)
    offset = 2
    filled: List[str] = []

    def take_f32() -> float:
        nonlocal offset
        v = rd_f32(payload, offset)
        offset += 4
        return v

    if mask & JmTlmBit.POS_VEL:
        fb.pos = take_f32(); fb.vel = take_f32()
        filled += ['pos', 'vel']
    if mask & JmTlmBit.DQ:
        fb.id = take_f32(); fb.iq = take_f32()
        filled += ['id', 'iq']
    if mask & JmTlmBit.PHASE:
        fb.ia = take_f32(); fb.ib = take_f32(); fb.ic = take_f32()
        filled += ['ia', 'ib', 'ic']
    if mask & JmTlmBit.BUS:
        fb.vbus = take_f32(); fb.ibus = take_f32(); fb.power = take_f32()
        filled += ['vbus', 'ibus', 'power']
    if mask & JmTlmBit.TEMP:
        fb.temp_fet = take_f32(); fb.temp_motor = take_f32()
        filled += ['temp_fet', 'temp_motor']
    if mask & JmTlmBit.MULTITURN:
        fb.multiturn = rd_i32(payload, offset); offset += 4
        fb.single = take_f32()
        filled += ['multiturn', 'single']
    if mask & JmTlmBit.TORQUE:
        fb.torque = take_f32()
        filled += ['torque']
    if mask & JmTlmBit.FAULT:
        fb.fault_mask = rd_u32(payload, offset); offset += 4
        fb.warn_mask = rd_u32(payload, offset); offset += 4
        filled += ['fault_mask', 'warn_mask']
    if mask & JmTlmBit.STATE:
        fb.top_fsm = payload[offset]; offset += 1
        fb.run_state = payload[offset]; offset += 1
        fb.ctrl_mode = payload[offset]; offset += 1
        fb.enable = payload[offset]; offset += 1
        if offset < len(payload):
            fb.motion_state = payload[offset]; offset += 1
        filled += ['top_fsm', 'run_state', 'ctrl_mode', 'enable', 'motion_state']
    if mask & JmTlmBit.DEBUG:
        # 调试通道 jm_dbg[]: 帧内剩余字节, 每 4 字节一个 f32 (通道数由固件 JM_DBG_CH 决定)
        dbg = []
        while offset + 4 <= len(payload):
            dbg.append(take_f32())
        fb.debug = tuple(dbg)
        filled += ['debug']

    fb.filled_fields = tuple(filled)
    return fb, fb.filled_fields


def parse_dev_info(payload: bytes) -> Tuple[int, int, bytes]:
    """READ_DEV_INFO(0xD0): hw_ver(u32), fw_ver(u32), uid(12B)"""
    if len(payload) < 20:
        return 0, 0, b''
    return rd_u32(payload, 0), rd_u32(payload, 4), bytes(payload[8:20])


def parse_param_read(payload: bytes) -> Tuple[int, int, bytes]:
    """PARAM_READ(0xE0) 应答: param_id(u16), type(u8), value(bytes)"""
    if len(payload) < 3:
        return 0, 0, b''
    return rd_u16(payload, 0), payload[2], payload[3:]


# ============================================================================
# 8. 虚拟电机 (本机回环): 不依赖真实硬件, 用于波形上位机离线调试
#    - 用一个 Queue 把串口字节流喂回, 实现 TX -> RX 回环
#    - 内置一个简易应答器: 收到读命令就产生一个伪反馈帧
#    - 周期产生 TELEMETRY 帧 (依据 SET_TELEMETRY 配置)
# ============================================================================

class VirtualMotor:
    """本机回环虚拟电机 (用于上位机离线测试)。

    使用方式:
      vm = VirtualMotor()
      vm.start()
      # 上位机发送的串口字节: bytes_tx -> vm.feed_tx(bytes_tx)
      # 上位机接收的串口字节: bytes_rx = vm.take_rx()
      vm.stop()
    """

    def __init__(self, period_ms: int = 10):
        self._period_ms = max(1, int(period_ms))
        self._rx_queue: deque = deque()
        self._tx_decoder = FrameDecoder()
        self._thread: Optional[threading.Thread] = None
        self._alive = False
        self._lock = threading.Lock()

        # 遥测订阅配置
        self._tlm_enable = False
        self._tlm_mask = 0
        self._tlm_period_ms = 0

        # 模拟状态
        self._t0 = time.monotonic()
        self._pos = 0.0
        self._vel = 0.0
        self._torque = 0.0
        self._top_fsm = int(TopFsm.IDLE)
        self._run_state = int(RunState.IDLE)
        self._enable = 0
        self._fault_mask = 0

    # ---- 上下行字节接口 ----
    def feed_tx(self, data: bytes):
        """上位机 TX 的串口字节喂入虚拟电机 (会被解码为命令)"""
        with self._lock:
            for cmd, payload in self._tx_decoder.feed(data):
                self._handle_cmd(cmd, payload)

    def take_rx(self) -> bytes:
        """取出虚拟电机待回送给上位机的串口字节 (可能为空)"""
        with self._lock:
            chunks = []
            while self._rx_queue:
                chunks.append(self._rx_queue.popleft())
            return b''.join(chunks)

    # ---- 启停 ----
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._alive = True
        self._thread = threading.Thread(target=self._run, name="VirtualMotor", daemon=True)
        self._thread.start()

    def stop(self):
        self._alive = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    # ---- 内部: 命令处理 ----
    def _handle_cmd(self, cmd: int, payload: bytes):
        # ACK 类命令 (0x00~0xB8): 回一个 ACK
        if cmd <= JmCmd.SINGLE_STEP:
            if cmd == JmCmd.ENABLE:
                self._enable = 1
                self._top_fsm = int(TopFsm.READY)
            elif cmd == JmCmd.DISABLE:
                self._enable = 0
                self._top_fsm = int(TopFsm.IDLE)
                self._run_state = int(RunState.IDLE)
            elif JmCmd.OPEN_LOOP <= cmd <= JmCmd.TEST_VELOCITY_LOOP and self._enable:
                self._top_fsm = int(TopFsm.RUN)
                self._run_state = int(cmd)
            elif cmd == JmCmd.CLEAR_FAULT:
                self._fault_mask = 0
                self._top_fsm = int(TopFsm.IDLE)
            self._emit(cmd, bytes([JmErr.OK]))
            return

        # 反馈查询
        if cmd == JmCmd.READ_FEEDBACK:
            t = time.monotonic() - self._t0
            self._pos = 1.0 * t
            self._vel = 1.0
            self._torque = 0.1 * t
            data = wr_f32(self._pos) + wr_f32(self._vel) + wr_f32(self._torque) \
                 + wr_f32(25.0 + 5 * t) + wr_f32(24.0) + wr_u16(self._fault_mask)
            self._emit(cmd, data)
            return
        if cmd == JmCmd.READ_STATE:
            self._emit(cmd, bytes([self._top_fsm, self._run_state, self._run_state, self._enable]))
            return
        if cmd == JmCmd.READ_POS_VEL:
            self._emit(cmd, wr_f32(self._pos) + wr_f32(self._vel))
            return
        if cmd == JmCmd.READ_DQ_CURRENT:
            self._emit(cmd, wr_f32(0.0) + wr_f32(self._torque * 5))
            return
        if cmd == JmCmd.READ_BUS:
            self._emit(cmd, wr_f32(24.0) + wr_f32(0.5) + wr_f32(12.0))
            return
        if cmd == JmCmd.READ_TEMPERATURE:
            self._emit(cmd, wr_f32(35.0) + wr_f32(30.0))
            return
        if cmd == JmCmd.READ_FAULT:
            self._emit(cmd, wr_u32(self._fault_mask) + wr_u32(0))
            return
        if cmd == JmCmd.SET_TELEMETRY:
            if len(payload) >= 5:
                self._tlm_enable = bool(payload[0])
                self._tlm_mask = rd_u16(payload, 1)
                self._tlm_period_ms = rd_u16(payload, 3)
            self._emit(cmd, bytes([JmErr.OK]))
            return

    def _emit(self, cmd: int, payload: bytes):
        """把应答帧字节推入 RX 队列"""
        self._rx_queue.append(FrameCodec.pack(cmd, payload))

    # ---- 内部: 周期遥测 ----
    def _run(self):
        next_t = time.monotonic()
        while self._alive:
            now = time.monotonic()
            if now >= next_t:
                # 触发一拍遥测
                if self._tlm_enable and self._tlm_mask:
                    self._emit_telemetry()
                next_t = now + max(0.001, self._tlm_period_ms / 1000.0)
            else:
                time.sleep(0.001)

    def _emit_telemetry(self):
        t = time.monotonic() - self._t0
        # 更新模拟值 (简单的正弦波 + 线性漂移, 便于波形调试)
        self._pos = 1.0 * t
        self._vel = 1.0 + 0.5 * (t % 2.0 - 1.0)
        self._torque = 0.5 * (t % 4.0 - 2.0)

        payload = bytearray()
        payload += wr_u16(self._tlm_mask)
        m = self._tlm_mask
        if m & JmTlmBit.POS_VEL:
            payload += wr_f32(self._pos) + wr_f32(self._vel)
        if m & JmTlmBit.DQ:
            payload += wr_f32(0.0) + wr_f32(self._torque * 5)
        if m & JmTlmBit.PHASE:
            payload += wr_f32(0.0) + wr_f32(0.0) + wr_f32(0.0)
        if m & JmTlmBit.BUS:
            payload += wr_f32(24.0) + wr_f32(0.5) + wr_f32(12.0)
        if m & JmTlmBit.TEMP:
            payload += wr_f32(35.0) + wr_f32(30.0)
        if m & JmTlmBit.MULTITURN:
            payload += wr_i32(int(self._pos)) + wr_f32(self._pos % 6.2831853)
        if m & JmTlmBit.TORQUE:
            payload += wr_f32(self._torque)
        if m & JmTlmBit.FAULT:
            payload += wr_u32(self._fault_mask) + wr_u32(0)
        if m & JmTlmBit.STATE:
            payload += bytes([self._top_fsm, self._run_state, self._run_state, self._enable, 0])
        if m & JmTlmBit.DEBUG:
            payload += wr_f32(0.1 * t) + wr_f32(0.2 * t)
        self._emit(JmCmd.TELEMETRY, bytes(payload))


# ============================================================================
# 9. 简易串口回环桥 (上位机 TX -> VirtualMotor -> 上位机 RX)
#    不打开真实串口, 直接在内存中回环, 用于无硬件时的波形调试
# ============================================================================

class LoopbackBridge:
    """把虚拟电机接到一个伪串口对象上, 上位机调用 write() 即触发虚拟电机应答"""

    def __init__(self, vm: Optional[VirtualMotor] = None):
        self._vm = vm or VirtualMotor()
        self._rx_buf = bytearray()
        self._lock = threading.Lock()
        self.is_open = True
        self.in_waiting = 0

    def write(self, data: bytes) -> int:
        self._vm.feed_tx(data)
        # 立即拉取应答字节, 缓存到 _rx_buf
        self._pump_rx()
        return len(data)

    def read(self, n: int = -1) -> bytes:
        self._pump_rx()
        with self._lock:
            if n < 0 or n > len(self._rx_buf):
                n = len(self._rx_buf)
            out = bytes(self._rx_buf[:n])
            del self._rx_buf[:n]
            self.in_waiting = len(self._rx_buf)
            return out

    def _pump_rx(self):
        """从虚拟电机拉取所有待发字节到本地 RX 缓冲 (遥测由后台线程异步产生)"""
        rx = self._vm.take_rx()
        if rx:
            with self._lock:
                self._rx_buf.extend(rx)
                self.in_waiting = len(self._rx_buf)

    def close(self):
        self.is_open = False
        self._vm.stop()

    def start(self):
        self._vm.start()


__all__ = [
    # CRC
    'crc16_calc',
    # 编解码助手
    'rd_u8', 'rd_i8', 'rd_u16', 'rd_i16', 'rd_u32', 'rd_i32', 'rd_f32',
    'wr_u8', 'wr_i8', 'wr_u16', 'wr_i16', 'wr_u32', 'wr_i32', 'wr_f32',
    'pack_value', 'unpack_value',
    # 枚举
    'JmCmd', 'JmErr', 'JmTlmBit', 'JmParamType', 'TopFsm', 'RunState',
    'TOP_FSM_CN', 'RUN_STATE_CN', 'JMERR_CN',
    'MAGIC_BOOTLOADER', 'MAGIC_FACTORY_RESET',
    'cmd_name', 'err_name', 'err_name_cn', 'top_fsm_name', 'run_state_name',
    # 串口帧
    'FrameCodec', 'FrameDecoder', 'STX_H', 'STX_L', 'MAX_PACK_SIZE', 'PACK_OVERHEAD',
    # CAN
    'can_make_id', 'can_get_cmd', 'can_get_motor_id',
    'can_pack_frames', 'CanReassembler',
    'MIT_PACK', 'MIT_UNPACK', 'FB_CAN_PACK', 'FB_CAN_UNPACK',
    'CAN_SEG_LAST', 'CAN_SEG_SEQ_MASK', 'CAN_SEG_PAYLOAD', 'CAN_SINGLE_MAX',
    'MIT_POS_MIN', 'MIT_POS_MAX', 'MIT_VEL_MIN', 'MIT_VEL_MAX',
    'MIT_KP_MIN', 'MIT_KP_MAX', 'MIT_KD_MIN', 'MIT_KD_MAX',
    'MIT_TFF_MIN', 'MIT_TFF_MAX', 'FB_TEMP_MIN', 'FB_TEMP_MAX',
    # 命令规格
    'Field', 'CommandSpec', 'BUILTIN_COMMANDS', 'pack_command',
    # 反馈
    'FeedbackData', 'parse_state', 'parse_read_reply', 'parse_telemetry',
    'parse_dev_info', 'parse_param_read',
    # 虚拟电机
    'VirtualMotor', 'LoopbackBridge',
]
