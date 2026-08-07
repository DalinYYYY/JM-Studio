"""PID debug-mode acquisition policy shared by UI components.

Protocol channel masks and rates live here so tuning-mode changes do not need
coordinated edits in the PID panel and the main window.
"""

from dataclasses import dataclass

from jmproto.cmd_def import JmTlmBit


@dataclass(frozen=True)
class PidDebugProfile:
    capture_mask: int
    capture_rate_hz: int
    capture_samples: int
    capture_channels: tuple[int, int]
    telemetry_mask: int
    telemetry_max_period_ms: int = 20


_CURRENT_TELEMETRY = (
    JmTlmBit.DQ | JmTlmBit.CURRENT_TARGET | JmTlmBit.STATE
)
_MOTION_TELEMETRY = (
    JmTlmBit.POS_VEL | JmTlmBit.MOTION_TARGET | JmTlmBit.STATE
)


PID_DEBUG_PROFILES = {
    "current_d": PidDebugProfile(
        0x03, 5000, 256, (0, 1), _CURRENT_TELEMETRY),
    "current_q": PidDebugProfile(
        0x0C, 5000, 256, (2, 3), _CURRENT_TELEMETRY),
    "velocity": PidDebugProfile(
        0x30, 1000, 256, (4, 5), _MOTION_TELEMETRY),
    "position": PidDebugProfile(
        0xC0, 500, 256, (6, 7), _MOTION_TELEMETRY),
}


def get_pid_debug_profile(mode: str) -> PidDebugProfile:
    """Return a valid profile, falling back to the D-axis current loop."""
    return PID_DEBUG_PROFILES.get(mode, PID_DEBUG_PROFILES["current_d"])


# ---- 波形窗口可选的上传数据源 ----
# 波形显示与 DEBUG 整定模式解耦: 采什么通道由波形窗口顶部直接切换,
# 与当前是否处于 DEBUG source、整定哪个环无关。key 复用 PID_DEBUG_PROFILES,
# 因此掩码/通道对/采样上限沿用同一份协议定义。
PID_WAVEFORM_SOURCES = (
    ("current_d", "D轴电流 Id", "A"),
    ("current_q", "Q轴电流 Iq", "A"),
    ("velocity", "速度", "rad/s"),
    ("position", "位置", "rad"),
)

PID_WAVEFORM_DEFAULT_SOURCE = PID_WAVEFORM_SOURCES[0][0]


def get_waveform_source_def(key: str):
    """Return (key, label, unit) for a waveform source, falling back to Id."""
    for item in PID_WAVEFORM_SOURCES:
        if item[0] == key:
            return item
    return PID_WAVEFORM_SOURCES[0]
