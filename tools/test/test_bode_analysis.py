"""Pure-Python regression tests for Bode TRACE aggregation and demodulation."""

import cmath
import csv
import math
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.bode_analysis import (ESTIMATOR_FFT, ESTIMATOR_LOCKIN,
                                BodeAnalyzer, BodePoint, fit_mechanical,
                                trace_phase_step)
from core.bode_session import BodeSession


def _packet(rows, flags, sequence=0, first=0, overflow=0,
            rate=1000, freq=10.0):
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


def test_lockin_rejects_dc_bias_and_out_of_band_ripple():
    rate = 1000.0
    freq = 10.0
    gain = 1.5
    phase = math.radians(-22.0)
    rows = []
    for i in range(500):
        t = 2.0 * math.pi * freq * i / rate
        ripple = 0.8 * math.sin(2.0 * math.pi * 137.0 * i / rate)
        rows.append((2.3 + math.sin(t) + ripple,
                     -1.1 + gain * math.sin(t + phase) + ripple))
    analyzer = BodeAnalyzer((1 << 2) | (1 << 5), estimator=ESTIMATOR_LOCKIN,
                            pll_bandwidth_hz=10000.0)
    point = analyzer.feed_packet(
        _packet(rows, 0x10 | 0x20 | 0x04, rate=int(rate), freq=freq))[0]
    assert point.valid
    assert abs(point.gain_db - 20.0 * math.log10(gain)) < 0.4
    assert abs(point.phase_deg + 22.0) < 2.0
    assert abs(point.input_dc - 2.3) < 0.05
    assert abs(point.output_dc + 1.1) < 0.05
    assert abs(point.output_ac_rms - gain / math.sqrt(2.0)) < 0.08
    assert point.coherence > 0.5


def test_fft_estimator_recovers_target_frequency():
    rate = 2000.0
    freq = 37.0
    gain = 0.75
    phase = math.radians(41.0)
    rows = []
    for i in range(600):
        t = 2.0 * math.pi * freq * i / rate
        noise = 0.12 * math.sin(2.0 * math.pi * 311.0 * i / rate)
        rows.append((0.4 + math.sin(t) + noise,
                     -0.2 + gain * math.sin(t + phase) + noise))
    analyzer = BodeAnalyzer((1 << 2) | (1 << 5), estimator=ESTIMATOR_FFT,
                            pll_bandwidth_hz=10000.0)
    point = analyzer.feed_packet(
        _packet(rows, 0x10 | 0x20 | 0x04, rate=int(rate), freq=freq))[0]
    assert point.valid
    assert point.estimator == ESTIMATOR_FFT
    assert abs(point.gain_db - 20.0 * math.log10(gain)) < 0.8
    assert abs(point.phase_deg - 41.0) < 4.0


def test_estimators_use_trace_rate_for_decimated_phase_inc():
    """TRACE phase_inc is in control-loop ticks, not TRACE sample ticks."""
    control_rate = 10000.0
    sample_rate = 3333.0  # firmware decimation is 3 (reported rate is rounded)
    freq = 1.0
    gain = 1.8
    phase = math.radians(-27.0)
    phase_inc = int(freq * 4294967296.0 / control_rate + 0.5)
    wx = trace_phase_step(freq, sample_rate, phase_inc)
    rows = [
        (math.sin(wx * i), gain * math.sin(wx * i + phase))
        for i in range(9999)
    ]
    for estimator in (ESTIMATOR_LOCKIN, ESTIMATOR_FFT):
        analyzer = BodeAnalyzer((1 << 2) | (1 << 5), estimator=estimator,
                                pll_bandwidth_hz=10000.0)
        point = analyzer.feed_packet({
            **_packet([], 0x10 | 0x20 | 0x04, rate=int(sample_rate), freq=freq),
            "phase_inc": phase_inc,
            "sample_count": len(rows),
            "samples": rows,
        })[0]
        assert point.valid
        assert abs(point.output_ac_rms - gain / math.sqrt(2.0)) < 0.02
        assert abs(point.gain_db - 20.0 * math.log10(gain)) < 0.25
        assert abs(point.phase_deg - math.degrees(phase)) < 2.0


class _SignalStub:
    def connect(self, *_args, **_kwargs):
        pass


class _ClientStub:
    bode_sweep_ack_received = _SignalStub()
    trace_data_received = _SignalStub()
    nack_received = _SignalStub()


def test_session_reanalyze_switches_method_without_new_sweep():
    rate = 1000
    freq = 10.0
    rows = []
    for i in range(400):
        t = 2.0 * math.pi * freq * i / rate
        rows.append({
            "sweep_point": 0, "sample_index": i,
            "actual_rate_hz": rate, "f_actual_mhz": int(freq * 1000),
            "phase_inc": int(freq * 4294967296.0 / rate),
            "input": math.sin(t), "output": 2.0 * math.sin(t + 0.2),
        })
    session = BodeSession(_ClientStub())
    analyzer = BodeAnalyzer((1 << 2) | (1 << 5), estimator=ESTIMATOR_LOCKIN,
                            pll_bandwidth_hz=10000.0)
    analyzer.feed_packet(_packet(
        [(r["input"], r["output"]) for r in rows],
        0x10 | 0x20 | 0x04, rate=rate, freq=freq))
    session.analyzer = analyzer
    session.raw_samples = rows
    points = session.reanalyze(ESTIMATOR_FFT)
    assert len(points) == 1
    assert points[0].estimator == ESTIMATOR_FFT
    assert abs(points[0].gain_db - 20.0 * math.log10(2.0)) < 0.8


def test_export_calculates_effective_feedback_for_every_raw_sample(tmp_path):
    rate = 1000
    freq = 10.0
    rows = []
    for i in range(400):
        t = 2.0 * math.pi * freq * i / rate
        rows.append({
            "session_id": 1, "sequence": 0, "sample_index": i,
            "actual_rate_hz": rate, "sweep_point": 0,
            "f_actual_mhz": int(freq * 1000),
            "phase_inc": int(freq * 4294967296.0 / rate),
            "input": math.sin(t), "output": 2.0 * math.sin(t + 0.2),
        })
    session = BodeSession(_ClientStub())
    session.session_id = 1
    session.raw_samples = rows
    analyzer = BodeAnalyzer((1 << 2) | (1 << 5), estimator=ESTIMATOR_LOCKIN,
                            pll_bandwidth_hz=10000.0)
    analyzer.feed_packet(_packet(
        [(r["input"], r["output"]) for r in rows],
        0x10 | 0x20 | 0x04, rate=rate, freq=freq))
    session.analyzer = analyzer
    exported = session.export(tmp_path)
    assert exported is not None
    with exported[1].open(encoding="utf-8-sig", newline="") as f:
        output_rows = list(csv.DictReader(f))
    assert len(output_rows) == len(rows)
    assert all(row["output_effective"] != "" for row in output_rows)


def test_reanalyze_attributes_intra_point_gap_to_own_point():
    """回归: 组内缺口归属到发生丢样的频点自身, 不再错位一档。

    旧 reanalyze 每频点重建单一整包, 频点内部 sample_index 缺口被藏掉;
    表现为丢样的频点假有效、下一个干净频点反被误判不连续。
    """
    rate = 1000
    rows = []
    # 频点0: index 0..99 中段丢 20 (60→80), 频点内缺口
    for i in list(range(60)) + list(range(80, 100)):
        t = 2.0 * math.pi * 10.0 * i / rate
        rows.append({"sweep_point": 0, "sample_index": i,
                     "actual_rate_hz": rate, "f_actual_mhz": 10000,
                     "phase_inc": int(10.0 * 4294967296.0 / rate),
                     "input": math.sin(t), "output": 2.0 * math.sin(t + 0.2)})
    # 频点1: 干净连续 100..199
    for i in range(100, 200):
        t = 2.0 * math.pi * 10.0 * i / rate
        rows.append({"sweep_point": 1, "sample_index": i,
                     "actual_rate_hz": rate, "f_actual_mhz": 10000,
                     "phase_inc": int(10.0 * 4294967296.0 / rate),
                     "input": math.sin(t), "output": 2.0 * math.sin(t + 0.2)})
    session = BodeSession(_ClientStub())
    session.session_id = 1
    session.raw_samples = rows
    session.analyzer = BodeAnalyzer(0x24, estimator=ESTIMATOR_LOCKIN)
    points = {p.point: p for p in session.reanalyze(ESTIMATOR_LOCKIN)}
    assert not points[0].valid, "带缺口的频点0应判无效"
    assert points[0].discontinuity
    assert points[1].valid, "连续的频点1不应受频点0缺口牵连"


def test_analyzer_default_disables_pll_warning():
    """默认 pll_bandwidth_hz=0: 上位机不可知下位机 PLL 带宽, 不做检测。"""
    rows = [(math.sin(2.0 * math.pi * 50.0 * i / 1000.0),
             math.sin(2.0 * math.pi * 50.0 * i / 1000.0)) for i in range(200)]
    analyzer = BodeAnalyzer((1 << 2) | (1 << 5))
    point = analyzer.feed_packet(
        _packet(rows, 0x10 | 0x20 | 0x04, rate=1000, freq=50.0))[0]
    assert point.valid
    assert "PLL" not in point.estimator_warning


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


def _ident_points(j, b, tau, n=43, f_lo=0.5, f_hi=30.0, invert=False):
    """合成 G(s)=e^(-τs)/(Js+B) 的辨识频点(轻阻尼: 转折频率低于扫频带)。"""
    pts = []
    for i in range(n):
        f = f_lo * (f_hi / f_lo) ** (i / (n - 1))
        w = 2.0 * math.pi * f
        h = cmath.exp(-1j * w * tau) / (b + 1j * w * j)
        if invert:
            h = -h  # 速度方向与电流方向反接
        p = BodePoint(point=i, f_actual_hz=f, phase_inc=0)
        p.input_complex = 0.5 + 0j
        p.output_complex = 0.5 * h
        p.gain_db = 20.0 * math.log10(abs(h))
        p.valid = True
        pts.append(p)
    return pts


def test_fit_mechanical_recovers_j_despite_loop_delay():
    """回归: 轻阻尼电机(转折0.016Hz)+1.5ms环路延迟, 全带相位>90°。

    旧算法 B=mean(Re z) 在该场景必为负 → 拟合失败(用户实测复现)。
    新算法网格搜索等效延迟后拟合, 应恢复 J 并给出延迟估计。
    """
    j_true, b_true, tau_true = 2e-4, 2e-5, 1.5e-3
    fit = fit_mechanical(_ident_points(j_true, b_true, tau_true))
    assert fit is not None, "延迟补偿后应能拟合(旧算法此场景返回None)"
    assert abs(fit["J"] - j_true) / j_true < 0.02, fit["J"]
    assert 0.0 <= fit["B"] <= 5.0 * max(b_true, 1e-6), fit["B"]
    assert abs(fit["delay_ms"] - tau_true * 1000.0) < 0.15, fit["delay_ms"]
    assert fit["point_count"] == 43


def test_fit_mechanical_without_delay_matches_classic():
    """无延迟、扫频带跨越转折频率的经典场景, 拟合应同时恢复 J 和 B。"""
    j_true, b_true = 1e-3, 1e-2  # 转折频率 1.6Hz, 在 0.5~30Hz 带内
    fit = fit_mechanical(_ident_points(j_true, b_true, 0.0))
    assert fit is not None
    assert abs(fit["J"] - j_true) / j_true < 1e-6
    assert abs(fit["B"] - b_true) / b_true < 1e-6
    assert abs(fit["corner_hz"] - b_true / (2 * math.pi * j_true)) < 1e-3


def test_fit_mechanical_rejects_inverted_velocity():
    """速度方向反接(180°翻转)时 J 无物理解, 应返回 None 而非垃圾值。"""
    assert fit_mechanical(_ident_points(2e-4, 2e-5, 1.5e-3, invert=True)) is None


def test_fit_mechanical_needs_three_valid_points():
    pts = _ident_points(2e-4, 2e-5, 0.0, n=5)
    for p in pts[3:]:
        p.valid = False  # 仅 3 个有效点为下限, 2 个应拒绝
    pts[2].valid = False
    assert fit_mechanical(pts) is None
