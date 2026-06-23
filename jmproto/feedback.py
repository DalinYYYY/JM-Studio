"""
实时反馈数据结构 + 各 READ_* / TELEMETRY 应答解析(纯函数, 无 Qt 依赖)
"""

from . import codec
from .cmd_def import JmCmd, JmTlmBit


class FeedbackData:
    """实时反馈数据(各字段单位见注释)"""
    __slots__ = [
        'pos', 'vel', 'torque',
        'id', 'iq', 'ia', 'ib', 'ic',
        'vbus', 'ibus', 'power', 'temp_fet', 'temp_motor',
        'multiturn', 'single',
        'fault_mask', 'warn_mask',
        'top_fsm', 'run_state', 'ctrl_mode', 'enable',
        'filled_fields',
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
        self.filled_fields = ()

    @staticmethod
    def from_feedback_payload(payload: bytes) -> 'FeedbackData':
        """从 READ_FEEDBACK(0xC0) 应答解析(22字节):
        pos,vel,torque,temp_motor,vbus(f32×5) + fault_mask(u16), 顺序须与固件 pack_feedback 一致"""
        fb = FeedbackData()
        n = 0
        fb.pos = codec.rd_f32(payload, n); n += 4
        fb.vel = codec.rd_f32(payload, n); n += 4
        fb.torque = codec.rd_f32(payload, n); n += 4
        fb.temp_motor = codec.rd_f32(payload, n); n += 4
        fb.vbus = codec.rd_f32(payload, n); n += 4
        fb.fault_mask = codec.rd_u16(payload, n); n += 2
        fb.filled_fields = ('pos', 'vel', 'torque', 'temp_motor', 'vbus', 'fault_mask')
        return fb


def parse_state(payload: bytes):
    """READ_STATE(0xC1): top_fsm, run_state, ctrl_mode, enable"""
    if len(payload) < 4:
        return 0, 0, 0, 0
    return payload[0], payload[1], payload[2], payload[3]


def parse_read_reply(cmd: int, payload: bytes):
    """解析 0xC2~0xC8 各读命令应答, 返回部分填充的 FeedbackData(无法解析返回 None)。
    调用方应只更新本帧涉及的字段(通过逐字段合并)。"""
    fb = FeedbackData()
    if cmd == JmCmd.READ_POS_VEL and len(payload) >= 8:
        fb.pos = codec.rd_f32(payload, 0)
        fb.vel = codec.rd_f32(payload, 4)
        fb.filled_fields = ('pos', 'vel')
        return fb, ('pos', 'vel')
    if cmd == JmCmd.READ_BUS and len(payload) >= 12:
        fb.vbus = codec.rd_f32(payload, 0)
        fb.ibus = codec.rd_f32(payload, 4)
        fb.power = codec.rd_f32(payload, 8)
        fb.filled_fields = ('vbus', 'ibus', 'power')
        return fb, ('vbus', 'ibus', 'power')
    if cmd == JmCmd.READ_TEMPERATURE and len(payload) >= 8:
        fb.temp_fet = codec.rd_f32(payload, 0)
        fb.temp_motor = codec.rd_f32(payload, 4)
        fb.filled_fields = ('temp_fet', 'temp_motor')
        return fb, ('temp_fet', 'temp_motor')
    if cmd == JmCmd.READ_DQ_CURRENT and len(payload) >= 8:
        fb.id = codec.rd_f32(payload, 0)
        fb.iq = codec.rd_f32(payload, 4)
        fb.filled_fields = ('id', 'iq')
        return fb, ('id', 'iq')
    if cmd == JmCmd.READ_PHASE_CURRENT and len(payload) >= 12:
        fb.ia = codec.rd_f32(payload, 0)
        fb.ib = codec.rd_f32(payload, 4)
        fb.ic = codec.rd_f32(payload, 8)
        fb.filled_fields = ('ia', 'ib', 'ic')
        return fb, ('ia', 'ib', 'ic')
    if cmd == JmCmd.READ_MULTITURN and len(payload) >= 8:
        fb.multiturn = codec.rd_i32(payload, 0)
        fb.single = codec.rd_f32(payload, 4)
        fb.filled_fields = ('multiturn', 'single')
        return fb, ('multiturn', 'single')
    if cmd == JmCmd.READ_FAULT and len(payload) >= 8:
        fb.fault_mask = codec.rd_u32(payload, 0)
        fb.warn_mask = codec.rd_u32(payload, 4)
        fb.filled_fields = ('fault_mask', 'warn_mask')
        return fb, ('fault_mask', 'warn_mask')
    return None, ()


def parse_telemetry(payload: bytes):
    """解析同步遥测帧 TELEMETRY(0xCA): mask(u16) + 按位序拼接的数据组。
    返回 (FeedbackData, 已填充字段名元组)。位序须与固件 jm_telemetry_bit_e 一致。"""
    fb = FeedbackData()
    if len(payload) < 2:
        return fb, ()
    mask = codec.rd_u16(payload, 0)
    offset = 2
    filled = []

    def take_f32():
        nonlocal offset
        v = codec.rd_f32(payload, offset)
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
        fb.multiturn = codec.rd_u32(payload, offset); offset += 4
        fb.single = take_f32()
        filled += ['multiturn', 'single']
    if mask & JmTlmBit.TORQUE:
        fb.torque = take_f32()
        filled += ['torque']
    if mask & JmTlmBit.FAULT:
        fb.fault_mask = codec.rd_u32(payload, offset); offset += 4
        fb.warn_mask = codec.rd_u32(payload, offset); offset += 4
        filled += ['fault_mask', 'warn_mask']
    if mask & JmTlmBit.STATE:
        fb.top_fsm = payload[offset]; offset += 1
        fb.run_state = payload[offset]; offset += 1
        fb.ctrl_mode = payload[offset]; offset += 1
        fb.enable = payload[offset]; offset += 1
        filled += ['top_fsm', 'run_state', 'ctrl_mode', 'enable']

    fb.filled_fields = tuple(filled)
    return fb, tuple(filled)
