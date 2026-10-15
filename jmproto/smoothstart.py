# -*- coding: utf-8 -*-
"""缓启动渐变配置 (0xA3/0xA4) 编解码

对应固件 ctrl_transition.h 的 ref_smooth_cfg_t + 跨模式兜底时长:
- param_id 0~13 与固件 jm_cmd_def.h JM_SMOOTH_PID_* 一一对应
- 当前固件仅支持 0~6 写入，7~13 只读。
- 0xA4 param_id=0xFF 整块 52B:
  enable(u8) + shape(u8) + rsv(2) + duration(u32) + rate[4](f32) + thresh[6](f32) + fallback(u32)
"""
from dataclasses import dataclass

from .codec import rd_f32, rd_u32, rd_u8, wr_f32, wr_u8, wr_u32

# ---------------- param_id 定义 (与固件 JM_SMOOTH_PID_* 一致) ----------------
PID_ENABLE = 0x00           # u8  总开关
PID_SHAPE = 0x01            # u8  渐变形状 0线性 1S曲线
PID_DURATION = 0x02         # u32 兜底时长(控制环调用次数)
PID_POS_RATE = 0x03         # f32 位置速率 rad/s
PID_VEL_RATE = 0x04         # f32 速度速率 rad/s²
PID_TORQUE_RATE = 0x05      # f32 力矩速率 N·m/s
PID_CURRENT_RATE = 0x06     # f32 电流速率 A/s
PID_POS_THRESH = 0x07       # f32 位置阈值 rad
PID_VEL_THRESH = 0x08       # f32 速度阈值 rad/s
PID_TORQUE_THRESH = 0x09    # f32 力矩阈值 N·m
PID_CURRENT_THRESH = 0x0A   # f32 电流阈值 A
PID_VOLTAGE_THRESH = 0x0B   # f32 电压阈值 V
PID_DUTY_THRESH = 0x0C      # f32 占空比阈值
PID_MODE_SW_FALLBACK = 0x0D # u32 跨模式切换兜底时长(调用次数)
PID_ALL = 0xFF              # 仅GET: 整块读取 52B

# 整块布局总长 (固定)
BLOCK_LEN = 52

# u32 类 param_id (其余均为 f32; enable/shape 虽为 u8 语义但传输按 4B)
_U32_PIDS = {PID_ENABLE, PID_SHAPE, PID_DURATION, PID_MODE_SW_FALLBACK}

# 编译期默认值 (与固件 ref_smooth_cfg_init_defaults + g_run_state_trans_count 一致)
WRITABLE_PIDS = frozenset(range(PID_CURRENT_RATE + 1))
DEFAULTS = {
    PID_ENABLE: 1,
    PID_SHAPE: 1,
    PID_DURATION: 500,
    PID_POS_RATE: 5.0,
    PID_VEL_RATE: 20.0,
    PID_TORQUE_RATE: 2.0,
    PID_CURRENT_RATE: 20.0,
    PID_POS_THRESH: 0.1,
    PID_VEL_THRESH: 1.0,
    PID_TORQUE_THRESH: 0.1,
    PID_CURRENT_THRESH: 0.5,
    PID_VOLTAGE_THRESH: 1.0,
    PID_DUTY_THRESH: 0.1,
    PID_MODE_SW_FALLBACK: 1000,
}

# 字段中文名 (UI 显示/日志)
PID_NAMES = {
    PID_ENABLE: "启用缓启动渐变",
    PID_SHAPE: "渐变形状",
    PID_DURATION: "兜底时长(次)",
    PID_POS_RATE: "位置速率(rad/s)",
    PID_VEL_RATE: "速度速率(rad/s²)",
    PID_TORQUE_RATE: "力矩速率(N·m/s)",
    PID_CURRENT_RATE: "电流速率(A/s)",
    PID_POS_THRESH: "位置阈值(rad)",
    PID_VEL_THRESH: "速度阈值(rad/s)",
    PID_TORQUE_THRESH: "力矩阈值(N·m)",
    PID_CURRENT_THRESH: "电流阈值(A)",
    PID_VOLTAGE_THRESH: "电压阈值(V)",
    PID_DUTY_THRESH: "占空比阈值",
    PID_MODE_SW_FALLBACK: "跨模式兜底时长(次)",
}


@dataclass
class SmoothConfig:
    """缓启动渐变配置运行时快照 (字段与 param_id 对应)"""
    enable: int = 1
    shape: int = 1
    duration: int = 500
    pos_rate: float = 5.0
    vel_rate: float = 20.0
    torque_rate: float = 2.0
    current_rate: float = 20.0
    pos_thresh: float = 0.1
    vel_thresh: float = 1.0
    torque_thresh: float = 0.1
    current_thresh: float = 0.5
    voltage_thresh: float = 1.0
    duty_thresh: float = 0.1
    mode_sw_fallback: int = 1000

    # param_id -> 字段名
    _FIELD_BY_PID = {
        PID_ENABLE: "enable", PID_SHAPE: "shape", PID_DURATION: "duration",
        PID_POS_RATE: "pos_rate", PID_VEL_RATE: "vel_rate",
        PID_TORQUE_RATE: "torque_rate", PID_CURRENT_RATE: "current_rate",
        PID_POS_THRESH: "pos_thresh", PID_VEL_THRESH: "vel_thresh",
        PID_TORQUE_THRESH: "torque_thresh", PID_CURRENT_THRESH: "current_thresh",
        PID_VOLTAGE_THRESH: "voltage_thresh", PID_DUTY_THRESH: "duty_thresh",
        PID_MODE_SW_FALLBACK: "mode_sw_fallback",
    }

    def get_by_pid(self, pid: int):
        return getattr(self, self._FIELD_BY_PID[pid])

    def set_by_pid(self, pid: int, value) -> None:
        setattr(self, self._FIELD_BY_PID[pid], value)


def is_u32_pid(pid: int) -> bool:
    return pid in _U32_PIDS


def pack_set_request(param_id: int, value) -> bytes:
    """编码 0xA3 写请求: param_id(1) + value(4) 小端"""
    if param_id not in DEFAULTS:
        raise ValueError(f"未知 param_id: {param_id:#04x}")
    if param_id not in WRITABLE_PIDS:
        raise ValueError(f"缓启动参数只读: {param_id:#04x}")
    if param_id in _U32_PIDS:
        return wr_u8(param_id) + wr_u32(int(value))
    return wr_u8(param_id) + wr_f32(float(value))


def pack_get_request(param_id: int) -> bytes:
    """编码 0xA4 读请求: param_id(1)"""
    if param_id != PID_ALL and param_id not in DEFAULTS:
        raise ValueError(f"未知 param_id: {param_id:#04x}")
    return wr_u8(param_id)


def decode_field_value(param_id: int, raw: bytes):
    """解码 0xA4 单字段应答的 4B 值"""
    if len(raw) < 4:
        raise ValueError("单字段应答不足 4B")
    if param_id in _U32_PIDS:
        return rd_u32(raw, 0)
    return rd_f32(raw, 0)


def unpack_block(raw: bytes) -> SmoothConfig:
    """解码 0xA4 整块应答 (52B)"""
    if len(raw) < BLOCK_LEN:
        raise ValueError(f"整块应答不足 {BLOCK_LEN}B: {len(raw)}")
    cfg = SmoothConfig()
    cfg.enable = rd_u8(raw, 0)
    cfg.shape = rd_u8(raw, 1)
    # raw[2:4] 保留
    cfg.duration = rd_u32(raw, 4)
    cfg.pos_rate = rd_f32(raw, 8)
    cfg.vel_rate = rd_f32(raw, 12)
    cfg.torque_rate = rd_f32(raw, 16)
    cfg.current_rate = rd_f32(raw, 20)
    cfg.pos_thresh = rd_f32(raw, 24)
    cfg.vel_thresh = rd_f32(raw, 28)
    cfg.torque_thresh = rd_f32(raw, 32)
    cfg.current_thresh = rd_f32(raw, 36)
    cfg.voltage_thresh = rd_f32(raw, 40)
    cfg.duty_thresh = rd_f32(raw, 44)
    cfg.mode_sw_fallback = rd_u32(raw, 48)
    return cfg


def estimate_transition_seconds(cfg: SmoothConfig, delta: float, rate: float,
                                s_curve: bool = True) -> float:
    """过渡时长估算: |Δ|/rate, S曲线 ×1.5 (峰值斜率补偿)"""
    if rate <= 0:
        return float("inf")
    t = abs(delta) / rate
    return t * 1.5 if (s_curve and cfg.shape == 1) else t
