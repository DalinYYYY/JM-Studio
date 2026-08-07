"""PID debug policy and mode-specific acquisition tests."""

import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..")))

from core.pid_debug_profile import PID_DEBUG_PROFILES, get_pid_debug_profile
from jmproto.cmd_def import JmTlmBit


expected = {
    "current_d": (0x03, 5000, (0, 1)),
    "current_q": (0x0C, 5000, (2, 3)),
    "velocity": (0x30, 1000, (4, 5)),
    "position": (0xC0, 500, (6, 7)),
}

assert set(PID_DEBUG_PROFILES) == set(expected)
for mode, (mask, rate, channels) in expected.items():
    profile = get_pid_debug_profile(mode)
    assert profile.capture_mask == mask
    assert profile.capture_rate_hz == rate
    assert profile.capture_channels == channels
    assert profile.capture_mask == sum(1 << channel for channel in channels)
    assert profile.capture_samples == 256
    assert not (profile.telemetry_mask & JmTlmBit.PHASE)

for mode in ("current_d", "current_q"):
    mask = get_pid_debug_profile(mode).telemetry_mask
    assert mask == (JmTlmBit.DQ | JmTlmBit.CURRENT_TARGET | JmTlmBit.STATE)

for mode in ("velocity", "position"):
    mask = get_pid_debug_profile(mode).telemetry_mask
    assert mask == (JmTlmBit.POS_VEL | JmTlmBit.MOTION_TARGET | JmTlmBit.STATE)

assert get_pid_debug_profile("invalid") == PID_DEBUG_PROFILES["current_d"]
print("PID debug profile: PASS")
