"""
关节电机命令码 / 错误码 / 遥测位掩码定义

忠实转写固件 User/Protocol/joint_proto/jm_cmd_def.h。
CMD 0x00~0xB8 段数值与 state_define.h 的 ctrl_mode_e 一致。
"""

from enum import IntEnum


class JmCmd(IntEnum):
    """关节电机命令码"""
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

    # CAN管理与通用 0xF0~0xFF
    SET_CAN_ID = 0xF0
    SET_BAUDRATE = 0xF1
    BROADCAST_SYNC = 0xF2
    NACK = 0xFE


class JmErr(IntEnum):
    """错误码(NACK 0xFE 的 err_code)"""
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


class JmTlmBit:
    """同步遥测分组位掩码(0xCA/0xCB 共用), 与固件 jm_telemetry_bit_e 一致"""
    POS_VEL = (1 << 0)
    DQ = (1 << 1)
    PHASE = (1 << 2)
    BUS = (1 << 3)
    TEMP = (1 << 4)
    MULTITURN = (1 << 5)
    TORQUE = (1 << 6)
    FAULT = (1 << 7)
    STATE = (1 << 8)
    DEBUG = (1 << 9)

    # (mask位, 中文标签) 列表, 供 UI 生成勾选框
    ITEMS = [
        (POS_VEL, "位置/速度"),
        (DQ, "DQ电流"),
        (PHASE, "三相电流"),
        (BUS, "母线"),
        (TEMP, "温度"),
        (MULTITURN, "多圈"),
        (TORQUE, "力矩"),
        (FAULT, "故障/警告"),
        (STATE, "状态机"),
        (DEBUG, "调试通道"),
    ]


class JmParamType(IntEnum):
    """参数类型码(0xE0 读应答的 type 字段)"""
    U8 = 0
    I8 = 1
    U16 = 2
    I16 = 3
    U32 = 4
    I32 = 5
    F32 = 6
    STR = 7


class TopFsm(IntEnum):
    """系统顶层主状态, 与固件 state_define.h:top_fsm_e 严格一致(数值即上报 top_fsm)"""
    INIT = 0        # 系统初始化
    SAFETY = 1      # 安全/急停(最高优先级, 任意态可进入)
    FAULT = 2       # 故障(任意态可进入)
    IDLE = 3        # 待机(伺服失能)
    READY = 4       # 就绪(已使能, 等待运行指令, 电机不动)
    RUN = 5         # 运行(运行子状态生效)
    CALIB = 6       # 校准
    CONFIG = 7      # 配置
    BOOTLOADER = 8  # 固件升级


# 顶层状态中文名(供 UI 显示)
TOP_FSM_CN = {
    TopFsm.INIT: "初始化",
    TopFsm.SAFETY: "急停/安全",
    TopFsm.FAULT: "故障",
    TopFsm.IDLE: "待机",
    TopFsm.READY: "就绪",
    TopFsm.RUN: "运行",
    TopFsm.CALIB: "校准",
    TopFsm.CONFIG: "配置",
    TopFsm.BOOTLOADER: "升级",
}


class RunState(IntEnum):
    """电机运行子状态, 与固件 state_define.h:run_state_e 严格一致(数值即上报 run_state)"""
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


# 运行子状态中文名(供 UI 显示)
RUN_STATE_CN = {
    RunState.IDLE: "空闲",
    RunState.OPEN_LOOP: "开环",
    RunState.CURRENT: "电流环",
    RunState.TORQUE: "力矩环",
    RunState.MIT: "MIT",
    RunState.VELOCITY: "速度环",
    RunState.POSITION: "位置环",
    RunState.POSITION_VELOCITY: "位置+速度",
    RunState.POSITION_TORQUE: "位置+力矩",
    RunState.VELOCITY_TORQUE: "速度+力矩",
    RunState.DUTY_CYCLE: "占空比",
    RunState.VOLTAGE_VECTOR: "电压矢量",
    RunState.FIELD_WEAKENING: "弱磁",
    RunState.SENSORLESS: "无感FOC",
    RunState.IMPEDANCE: "阻抗",
    RunState.ADMITTANCE: "导纳",
    RunState.FORCE_CONTROL: "力控",
    RunState.FORCE_POSITION_HYBRID: "力位混合",
    RunState.GRAVITY_COMPENSATION: "重力补偿",
    RunState.COLLISION_DETECTION: "碰撞检测",
    RunState.ZERO_FORCE: "零力",
    RunState.CONSTANT_FORCE: "恒力",
    RunState.VARIABLE_IMPEDANCE: "变阻抗",
    RunState.ADAPTIVE_GRAVITY_COMP: "自适应重力补偿",
    RunState.LANDING_BUFFER: "落地缓冲",
    RunState.PVT: "PVT",
    RunState.CUBIC_SPLINE: "三次样条",
    RunState.TRAPEZOIDAL_TRAJ: "梯形轨迹",
    RunState.S_CURVE_TRAJ: "S曲线",
    RunState.HOMING: "回零",
    RunState.ELECTRONIC_GEAR: "电子齿轮",
    RunState.ELECTRONIC_CAM: "电子凸轮",
    RunState.STEP_DIR: "脉冲方向",
    RunState.ANALOG_INPUT: "模拟量",
    RunState.PWM_INPUT: "PWM输入",
    RunState.JOG: "点动",
    RunState.SAFE_TEACH: "安全示教",
    RunState.TEST_AGING: "老化测试",
    RunState.TEST_SWEEP_FREQ: "扫频测试",
    RunState.TEST_COGGING: "齿槽测试",
    RunState.TEST_FRICTION: "摩擦测试",
    RunState.TEST_INERTIA: "惯量测试",
    RunState.DIAGNOSTIC: "诊断",
    RunState.HIGH_SPEED_DAQ: "高速采集",
    RunState.SINGLE_STEP: "单步调试",
}


# 防误触魔数
MAGIC_BOOTLOADER = 0xB00710AD
MAGIC_FACTORY_RESET = 0xFAC70F5F


def cmd_name(cmd: int) -> str:
    """返回命令名, 未知则返回十六进制"""
    try:
        return JmCmd(cmd).name
    except ValueError:
        return f"0x{cmd:02X}"


def err_name(err: int) -> str:
    """返回错误码名, 未知则返回十六进制"""
    try:
        return JmErr(err).name
    except ValueError:
        return f"0x{err:02X}"


def top_fsm_name(top_fsm: int) -> str:
    """返回顶层状态中文名, 未知则返回 (数值)"""
    try:
        return TOP_FSM_CN[TopFsm(top_fsm)]
    except (ValueError, KeyError):
        return f"({top_fsm})"


def run_state_name(run_state: int) -> str:
    """返回运行子状态中文名, 未知则返回 (数值)"""
    try:
        return RUN_STATE_CN[RunState(run_state)]
    except (ValueError, KeyError):
        return f"({run_state})"
