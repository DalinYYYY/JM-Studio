"""Pure-Python regression tests for Bode TRACE aggregation and demodulation."""

import math
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.bode_analysis import BodeAnalyzer


def _packet(rows, flags, sequence=0, first=0, overflow=0):
    rate = 1000
    freq = 10.0
    return {
        "session_id": 1,
        "sequence": sequence,
        "first_sample_index": first,
        "actual_rate_hz": rate,
        "channel_mask": (1 << 2) | (1 << 5),
        "sample_count": len(rows),
        "flags": 0x08 | flags,
        "overflow_count": overflow,
        "sweep_point": 0,
        "phase_inc": int(freq * 4294967296.0 / rate),
        "f_actual_mhz": int(freq * 1000.0),
        "samples": rows,
    }


def test_synchronous_demodulation_recovers_gain_and_phase():
    rate = 1000.0
    freq = 10.0
    gain = 2.0
    phase = math.radians(30.0)
    rows = [
        (math.sin(2.0 * math.pi * freq * i / rate),
         gain * math.sin(2.0 * math.pi * freq * i / rate + phase))
        for i in range(400)
    ]

    analyzer = BodeAnalyzer((1 << 2) | (1 << 5), pll_bandwidth_hz=10000.0)
    points = analyzer.feed_packet(_packet(rows, 0x10 | 0x20 | 0x04))

    assert len(points) == 1
    point = points[0]
    assert point.valid
    assert abs(point.gain_db - 20.0 * math.log10(gain)) < 0.1
    assert abs(point.phase_deg - 30.0) < 1.0


def test_discontinuity_and_overflow_mark_point_invalid():
    rows = [(math.sin(i * 0.1), math.sin(i * 0.1)) for i in range(100)]
    analyzer = BodeAnalyzer((1 << 2) | (1 << 5), pll_bandwidth_hz=10000.0)
    analyzer.feed_packet(_packet(rows[:50], 0x10, sequence=0, first=0))
    points = analyzer.feed_packet(
        _packet(rows[50:], 0x20 | 0x04, sequence=2, first=60, overflow=1)
    )

    assert len(points) == 1
    assert not points[0].valid
    assert points[0].discontinuity
    assert points[0].overflow_count == 1
    assert "数据不连续" in points[0].valid_reason

