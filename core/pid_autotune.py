"""Host-side PID theoretical estimates used as editable proposals."""

import math


DEFAULT_CURRENT_BW_HZ = 1000.0
DEFAULT_VELOCITY_BW_HZ = 100.0
DEFAULT_POSITION_BW_HZ = 20.0


def _positive(values: dict, name: str, minimum: float) -> float:
    try:
        value = float(values[name])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"缺少有效电机参数: {name}") from None
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"电机参数无效: {name}={value}")
    return value


def _current_limit(values: dict) -> float:
    for name in ("current_lim", "peak_current"):
        try:
            value = float(values.get(name, 0.0))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0.0:
            return value
    return 1.0


def estimate_pid_proposal(values: dict, ring_mask: int,
                          current_bw_hz: float = 0.0,
                          velocity_bw_hz: float = 0.0,
                          position_bw_hz: float = 0.0) -> dict:
    """Calculate selected rings without mutating device/runtime parameters."""
    if not isinstance(values, dict):
        raise ValueError("电机参数尚未读取")
    if ring_mask <= 0 or ring_mask > 0x07:
        raise ValueError("请至少选择一个整定环")

    configured_current_bw = values.get(
        "current_control_bandwidth", DEFAULT_CURRENT_BW_HZ)
    try:
        configured_current_bw = float(configured_current_bw)
    except (TypeError, ValueError):
        configured_current_bw = DEFAULT_CURRENT_BW_HZ
    if not math.isfinite(configured_current_bw) or configured_current_bw <= 0.0:
        configured_current_bw = DEFAULT_CURRENT_BW_HZ

    cur_bw = float(current_bw_hz) if current_bw_hz > 0.0 else configured_current_bw
    vel_bw = (float(velocity_bw_hz)
             if velocity_bw_hz > 0.0 else DEFAULT_VELOCITY_BW_HZ)
    pos_bw = (float(position_bw_hz)
             if position_bw_hz > 0.0 else DEFAULT_POSITION_BW_HZ)
    for name, value, upper in (
            ("电流环带宽", cur_bw, 20000.0),
            ("速度环带宽", vel_bw, 10000.0),
            ("位置环带宽", pos_bw, 5000.0)):
        if not math.isfinite(value) or value <= 0.0 or value > upper:
            raise ValueError(f"{name}无效: {value}")
    if ring_mask & 0x03 == 0x03 and vel_bw > cur_bw * 0.2:
        raise ValueError("速度环带宽需不高于电流环的 1/5")
    if ring_mask & 0x06 == 0x06 and pos_bw > vel_bw * 0.2:
        raise ValueError("位置环带宽需不高于速度环的 1/5")

    proposal = {}
    if ring_mask & 0x01:
        resistance = _positive(values, "phase_resistance", 0.001)
        ld = _positive(values, "phase_inductance_d", 0.000001)
        try:
            lq = float(values.get("phase_inductance_q", ld))
        except (TypeError, ValueError):
            lq = ld
        if not math.isfinite(lq) or lq < 0.000001:
            lq = ld
        wc = 2.0 * math.pi * cur_bw
        kp_q = wc * lq
        proposal.update({
            "kp_ld": wc * ld,
            "ki_ld": wc * resistance,
            "kp_lq": kp_q,
            "ki_lq": wc * resistance,
            "integral_limit": kp_q * _current_limit(values) * 0.9,
        })

    if ring_mask & 0x02:
        inertia = _positive(values, "rotor_inertia", 0.0000001)
        kt = _positive(values, "torque_constant", 0.00001)
        wc = 2.0 * math.pi * vel_bw
        proposal.update({
            "kp_s": inertia * wc / kt,
            "ki_s": inertia * wc * wc / (4.0 * kt),
            "speed_integral_limit": _current_limit(values) * 0.5,
        })

    if ring_mask & 0x04:
        proposal["kp_p"] = 2.0 * math.pi * pos_bw

    return proposal
