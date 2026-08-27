"""Bode 扫频 TRACE 分组和同步解调。"""

from __future__ import annotations

import cmath
import csv
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import numpy as np
except Exception:  # pragma: no cover - the lock-in path remains dependency-free
    np = None


ESTIMATOR_LOCKIN = "lockin"
ESTIMATOR_FFT = "fft"
_ESTIMATOR_ALIASES = {
    "lock-in": ESTIMATOR_LOCKIN,
    "lockin": ESTIMATOR_LOCKIN,
    "correlation": ESTIMATOR_LOCKIN,
    "相关": ESTIMATOR_LOCKIN,
    "同步解调": ESTIMATOR_LOCKIN,
    "fft": ESTIMATOR_FFT,
    "频谱": ESTIMATOR_FFT,
}


def normalize_estimator(value: str) -> str:
    """将界面/脚本传入的算法名称归一化为 ``lockin`` 或 ``fft``。"""
    key = str(value).strip().lower()
    return _ESTIMATOR_ALIASES.get(key, ESTIMATOR_LOCKIN)


def trace_phase_step(freq_hz: float, sample_rate_hz: float,
                     phase_inc: int = 0) -> float:
    """Return the NCO phase advance between two TRACE samples.

    ``phase_inc`` is generated in the control-loop timebase.  TRACE may be
    decimated, so using it directly as a per-sample increment is wrong when
    ``actual_rate_hz`` is lower than the control frequency.  The protocol also
    carries the quantized actual frequency; use it with the TRACE sample rate
    and, when possible, recover the integer decimation factor to retain the
    exact DDS increment.
    """
    rate = float(sample_rate_hz)
    freq = float(freq_hz)
    if rate <= 0.0 or not math.isfinite(rate):
        return 0.0
    if phase_inc:
        unit = (int(phase_inc) & 0xFFFFFFFF) / 4294967296.0
        if unit > 0.0 and math.isfinite(freq) and freq > 0.0:
            control_rate = freq / unit
            decim_real = control_rate / rate
            decim = max(1, int(round(decim_real)))
            # f_actual_mHz is rounded, therefore allow a small mismatch while
            # rejecting metadata from a different timebase.
            if abs(decim_real - decim) <= 0.02 * max(1.0, decim):
                return 2.0 * math.pi * unit * decim
    return 2.0 * math.pi * freq / rate


@dataclass
class BodePoint:
    point: int
    f_actual_hz: float
    phase_inc: int
    input_rms: float = 0.0
    output_rms: float = 0.0
    gain_db: float = float("nan")
    phase_deg: float = float("nan")
    snr_db: float = float("nan")
    sample_count: int = 0
    valid: bool = False
    valid_reason: str = "未完成"
    flags: int = 0
    discontinuity: bool = False
    overflow_count: int = 0
    estimator_warning: str = ""
    input_complex: complex = field(default=0j, repr=False)
    output_complex: complex = field(default=0j, repr=False)
    # 新增字段放在原有位置之后，保持旧脚本按位置构造 BodePoint 的兼容性。
    input_ac_rms: float = 0.0
    output_ac_rms: float = 0.0
    input_dc: float = 0.0
    output_dc: float = 0.0
    estimator: str = ESTIMATOR_LOCKIN
    coherence: float = float("nan")
    # 实际 TRACE 样本之间的相位步进(rad/sample), 供重建波形复用。
    phase_step_rad: float = 0.0

    def csv_row(self):
        d = asdict(self)
        d.pop("input_complex", None)
        d.pop("output_complex", None)
        return d


class BodeAnalyzer:
    """按 TRACE_FLAG_POINT_START/END 聚合一个扫频会话。"""

    def __init__(self, channel_mask: int, pll_bandwidth_hz: float = 0.0,
                 estimator: str = ESTIMATOR_LOCKIN):
        self.channel_mask = int(channel_mask)
        self.pll_bandwidth_hz = float(pll_bandwidth_hz)
        self.estimator = normalize_estimator(estimator)
        self.points: dict[int, BodePoint] = {}
        self._buffers: dict[int, list[tuple[float, float]]] = {}
        self._meta: dict[int, dict] = {}
        self._active_point: int | None = None
        self.last_sequence: int | None = None
        self.last_sample_index: int | None = None
        self.lost_packets = 0
        self.overflow_count = 0

    def reset(self):
        self.points.clear()
        self._buffers.clear()
        self._meta.clear()
        self._active_point = None
        self.last_sequence = None
        self.last_sample_index = None
        self.lost_packets = 0
        self.overflow_count = 0

    def _mark_discontinuity(self, packet: dict) -> bool:
        # 固件的 DISCONTINUITY/OVERFLOW 标志是会话级粘滞位: 溢出一次后
        # 所有后续包都带着, 据此判死会污染整场扫频; 丢样已由下方
        # sequence(包丢失)与 first_sample_index(样本丢失)跳变按包精确检测。
        discontinuity = False
        sequence = int(packet.get("sequence", 0))
        first = int(packet.get("first_sample_index", 0))
        count = int(packet.get("sample_count", 0))
        if self.last_sequence is not None:
            delta = (sequence - self.last_sequence) & 0xFFFFFFFF
            if delta != 0:
                self.lost_packets += delta
                discontinuity = True
        if self.last_sample_index is not None:
            delta = (first - self.last_sample_index) & 0xFFFFFFFF
            if delta != 0:
                discontinuity = True
        self.last_sequence = (sequence + 1) & 0xFFFFFFFF
        self.last_sample_index = (first + count) & 0xFFFFFFFF
        self.overflow_count = max(self.overflow_count,
                                  int(packet.get("overflow_count", 0)))
        return discontinuity

    def feed_packet(self, packet: dict) -> list[BodePoint]:
        if not packet or not (int(packet.get("flags", 0)) & 0x08):
            return []
        point = packet.get("sweep_point")
        freq_mhz = packet.get("f_actual_mhz")
        phase_inc = packet.get("phase_inc")
        if point is None or freq_mhz is None or phase_inc is None:
            return []
        point = int(point)
        flags = int(packet.get("flags", 0))
        discontinuity = self._mark_discontinuity(packet)
        rows = packet.get("samples") or []
        if not rows:
            return []
        if self._active_point is not None and self._active_point != point:
            # 固件应以 POINT_END 结束前一点；若没有，前一点无效。
            self._finalize(self._active_point, force_invalid=True,
                           reason="频点边界缺失")
        self._active_point = point
        buf = self._buffers.setdefault(point, [])
        buf.extend((float(row[0]), float(row[1])) for row in rows
                    if len(row) >= 2)
        meta = self._meta.setdefault(point, {
            "freq_mhz": int(freq_mhz), "phase_inc": int(phase_inc),
            "flags": 0, "discontinuity": False,
            "overflow_count": 0, "rate_hz": int(packet.get("actual_rate_hz", 0)),
        })
        meta["flags"] |= flags
        meta["discontinuity"] |= discontinuity
        meta["overflow_count"] = max(meta["overflow_count"],
                                      int(packet.get("overflow_count", 0)))
        completed = []
        if flags & 0x20:
            result = self._finalize(point, force_invalid=discontinuity)
            if result is not None:
                completed.append(result)
            self._active_point = None
        return completed

    def _finalize(self, point: int, force_invalid: bool = False,
                  reason: str = "") -> BodePoint | None:
        rows = self._buffers.pop(point, None)
        meta = self._meta.pop(point, None)
        if not rows or not meta:
            return None
        freq = meta["freq_mhz"] / 1000.0
        result = BodePoint(point=point, f_actual_hz=freq,
                           phase_inc=meta["phase_inc"],
                           sample_count=len(rows), flags=meta["flags"],
                           discontinuity=bool(meta["discontinuity"]),
                           overflow_count=meta["overflow_count"],
                           estimator=self.estimator)
        result.input_rms = math.sqrt(sum(x * x for x, _ in rows) / len(rows))
        result.output_rms = math.sqrt(sum(y * y for _, y in rows) / len(rows))
        rate = float(meta.get("rate_hz", 0))
        duration = len(rows) / rate if rate > 0 else 0.0
        reasons = []
        if force_invalid or result.discontinuity:
            reasons.append(reason or "数据不连续")
        # overflow_count 是固件会话级全局累计(不按频点清零), 一次溢出
        # 即粘滞到之后所有频点, 不能据此判死; 缺样本的影响已由
        # sample_index 跳变检测精确归属到受影响的频点。字段仅作诊断。
        # 固件测量周期自适应 clamp(2*freq,1,8): 低频仅 1 个整周期,
        # 阈值取 0.99 允许单周期, 不足 1 个整周期才判无效。
        if len(rows) < 2 or duration * freq < 0.99:
            reasons.append("测量周期不足")
        if rate <= 0 or rate < 10.0 * freq:
            reasons.append("采样率不足")
        if result.input_rms <= 1e-9:
            reasons.append("输入幅值过小")
        mean_x = sum(x for x, _ in rows) / len(rows)
        mean_y = sum(y for _, y in rows) / len(rows)
        result.input_dc = mean_x
        result.output_dc = mean_y
        values_x = [float(row[0]) for row in rows]
        phase_step = self._select_phase_step(
            values_x, freq, rate, meta["phase_inc"])
        result.phase_step_rad = phase_step
        result.input_complex, result.output_complex, used_estimator = \
            self._estimate_complex(rows, freq, rate, meta["phase_inc"],
                                   phase_step=phase_step)
        result.estimator = used_estimator
        if used_estimator != self.estimator:
            result.estimator_warning = "FFT不可用，已回退锁相相关"
        result.input_ac_rms = abs(result.input_complex) / math.sqrt(2.0)
        result.output_ac_rms = abs(result.output_complex) / math.sqrt(2.0)
        if abs(result.input_complex) <= 1e-12:
            reasons.append("输入频点能量过小")
        else:
            h = result.output_complex / result.input_complex
            result.gain_db = 20.0 * math.log10(max(abs(h), 1e-30))
            result.phase_deg = math.degrees(cmath.phase(h))
            wx = phase_step
            residual = sum(abs((vy - mean_y) -
                               (result.output_complex.real * math.cos(wx * i) -
                                result.output_complex.imag * math.sin(wx * i))) ** 2
                            for i, (_, vy) in enumerate(rows))
            signal = max(abs(result.output_complex) ** 2 * len(rows), 1e-30)
            result.snr_db = 10.0 * math.log10(signal / max(residual, 1e-30))
            result.coherence = signal / max(signal + residual, 1e-30)
            # SNR 低只降精度不否定测量: 标注警告, 仍参与绘图(用户自行取舍)
            if result.snr_db < 10.0:
                result.valid_reason = "SNR低"
            elif result.snr_db < 20.0:
                result.valid_reason = "SNR偏低"
        # PLL 带宽仅作估计器影响提示: 下位机 PLL 带宽运行时可调,
        # 上位机无法得知准确值, 不据此判无效(默认 0 = 完全不判定)
        pll_warning = ""
        if self.pll_bandwidth_hz > 0:
            if freq > self.pll_bandwidth_hz:
                pll_warning = "超过PLL带宽"
            elif freq > 0.5 * self.pll_bandwidth_hz:
                pll_warning = "接近PLL带宽"
            elif freq > 0.3 * self.pll_bandwidth_hz:
                pll_warning = "存在PLL影响"
        if pll_warning:
            result.estimator_warning = (f"{result.estimator_warning};{pll_warning}"
                                         if result.estimator_warning
                                         else pll_warning)
        result.valid = not reasons
        if reasons:
            result.valid_reason = ";".join(reasons)
        elif result.valid_reason in ("", "未完成"):  # 保留SNR警告, 无警告才标"有效"
            result.valid_reason = "有效"
        self.points[point] = result
        return result

    @staticmethod
    def _lockin_complex(values, freq: float, rate: float,
                        phase_inc: int = 0,
                        phase_step: float | None = None) -> complex:
        """用已知扫频载波做同步解调，返回交流基波峰值复数。"""
        n = len(values)
        if n <= 0 or rate <= 0:
            return 0j
        mean = sum(values) / n
        wx = (float(phase_step) if phase_step is not None else
              trace_phase_step(freq, rate, phase_inc))
        real = imag = 0.0
        for i, value in enumerate(values):
            angle = wx * i
            centered = value - mean
            real += centered * math.cos(angle)
            imag -= centered * math.sin(angle)
        return complex(2.0 * real / n, 2.0 * imag / n)

    @classmethod
    def _fft_pair(cls, values_x, values_y, freq: float, rate: float,
                  phase_inc: int = 0,
                  phase_step: float | None = None) -> tuple[complex, complex]:
        """Extract X/Y at one common FFT bin.

        Selecting an independent peak for each channel can make output pick a
        ripple/harmonic bin while input picks the excitation bin, corrupting
        both the feedback amplitude and the transfer ratio.  The excitation
        frequency defines one shared bin for both channels.
        """
        if np is None or len(values_x) < 4 or len(values_y) != len(values_x) \
                or rate <= 0:
            raise RuntimeError("numpy unavailable")
        x = np.asarray(values_x, dtype=float)
        y = np.asarray(values_y, dtype=float)
        n = int(x.size)
        # 零填充提升频率栅格分辨率；Hann 窗抑制整周期不足/边界泄漏。
        window = np.hanning(n)
        coherent_gain = float(window.sum())
        if coherent_gain <= 1e-12:
            raise RuntimeError("invalid FFT window")
        nfft = 1
        while nfft < n * 8:
            nfft <<= 1
        # Match the exact discrete-time DDS frequency when metadata is
        # available; this also accounts for TRACE decimation/rounded rate.
        wx = (float(phase_step) if phase_step is not None else
              trace_phase_step(freq, rate, phase_inc))
        target_bin = wx \
            * nfft / (2.0 * math.pi)
        # 目标低于 FFT 最低非零栅格时，硬取 bin=1 会把直流/低频误当成反馈；
        # 此时交给已知频率的锁相相关，它不受 FFT 栅格限制。
        if target_bin < 1.0 or target_bin > (nfft // 2 - 1):
            raise RuntimeError("target outside FFT bins")
        k = int(round(target_bin))
        k = max(1, min(k, nfft // 2))
        # 2/sum(window) 将 FFT 结果恢复为正弦峰值复数。
        sx = np.fft.rfft((x - float(x.mean())) * window, n=nfft)
        sy = np.fft.rfft((y - float(y.mean())) * window, n=nfft)
        return (complex(2.0 * sx[k] / coherent_gain),
                complex(2.0 * sy[k] / coherent_gain))

    @staticmethod
    def _correlation_amplitude(values, phase_step: float) -> float:
        n = len(values)
        if n <= 0 or not math.isfinite(phase_step):
            return 0.0
        mean = sum(values) / n
        real = imag = 0.0
        for i, value in enumerate(values):
            angle = phase_step * i
            centered = value - mean
            real += centered * math.cos(angle)
            imag -= centered * math.sin(angle)
        return 2.0 * math.hypot(real, imag) / n

    @classmethod
    def _select_phase_step(cls, values, freq: float, rate: float,
                           phase_inc: int) -> float:
        """Select the sample-time phase step validated by the input waveform.

        Firmware revisions have used both control-loop and TRACE-sample
        interpretations of phase metadata.  The excitation input is present
        in every Bode segment, so choose the metadata candidate that produces
        the strongest correlation on the complete input segment.  This keeps
        amplitude and phase extraction tied to the measured carrier.
        """
        candidates = [trace_phase_step(freq, rate, phase_inc)]
        if rate > 0.0 and freq > 0.0:
            candidates.append(2.0 * math.pi * float(freq) / float(rate))
        if phase_inc:
            unit = (int(phase_inc) & 0xFFFFFFFF) / 4294967296.0
            if unit > 0.0:
                candidates.append(2.0 * math.pi * unit)
        # Keep only physical positive-frequency candidates and deduplicate
        # nearly identical values so ties remain deterministic.
        unique = []
        for step in candidates:
            if not (0.0 < step < math.pi):
                continue
            if all(abs(step - old) > 1e-12 for old in unique):
                unique.append(step)
        if not unique:
            return candidates[0] if candidates else 0.0
        return max(unique, key=lambda step:
                   cls._correlation_amplitude(values, step))

    def _estimate_complex(self, rows, freq: float, rate: float,
                          phase_inc: int,
                          phase_step: float | None = None
                          ) -> tuple[complex, complex, str]:
        values_x = [float(row[0]) for row in rows]
        values_y = [float(row[1]) for row in rows]
        if phase_step is None:
            phase_step = self._select_phase_step(
                values_x, freq, rate, phase_inc)
        if self.estimator == ESTIMATOR_FFT:
            try:
                x, y = self._fft_pair(values_x, values_y, freq, rate,
                                      phase_inc, phase_step)
                return x, y, ESTIMATOR_FFT
            except (RuntimeError, ValueError, TypeError):
                pass
        return (self._lockin_complex(values_x, freq, rate, phase_inc,
                                     phase_step),
                self._lockin_complex(values_y, freq, rate, phase_inc,
                                     phase_step),
                ESTIMATOR_LOCKIN)

    def finish(self) -> list[BodePoint]:
        """会话结束时收尾：无 POINT_END 的遗留频点按其已收样本定版。"""
        completed: list[BodePoint] = []
        if self._active_point is not None:
            result = self._finalize(self._active_point, force_invalid=False)
            if result is not None:
                completed.append(result)
            self._active_point = None
        for point in list(self._buffers.keys()):
            result = self._finalize(point, force_invalid=False)
            if result is not None:
                completed.append(result)
        return completed

    def ordered_points(self) -> list[BodePoint]:
        return [self.points[k] for k in sorted(self.points)]

    def export_csv(self, bode_path: str | Path):
        rows = [p.csv_row() for p in self.ordered_points()]
        if not rows:
            return
        path = Path(bode_path)
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def export_meta(self, meta_path: str | Path, meta: dict):
        Path(meta_path).write_text(json.dumps(meta, ensure_ascii=False,
                                              indent=2), encoding="utf-8")


# ===================== 方向一：机械对象拟合(辨识) =====================

def fit_mechanical(points: list) -> dict | None:
    """按一阶机械模型含等效延迟 G(s) = e^(-τs)/(J·s + B) 拟合有效频点。

    模式6(实测电流输入)拟合的是纯机械对象、不含电流环闭环动态；
    模式0/1(目标电流输入)低频段电流环增益高、实测≈目标，惯量
    拟合同样成立，且不受电流采样噪声影响。对每个频点取
    z = 1/H(jω)，模型预测 z = (B + jωJ)·e^(jωτ)。

    延迟补偿是关键: 直驱/轻阻尼电机的机械转折频率远低于扫频起点,
    全带相位≈-90°, 环路+采样的毫秒级额外滞后会把相位推过 -90°,
    使 Re(z) 变负 → 无补偿时 B=mean(Re z) 必然为负、拟合失败。
    因此对 τ 做 0~8ms 网格搜索(50µs 步进): 将 z 旋转 e^(-jωτ) 后
    做分离最小二乘(B=mean Re, J=Σω·Im/Σω²), 取复域残差最小的 τ。
    B 允许拟合到 0(纯惯量退化), J 必须为正否则视为相位异常。
    电流输入时结果为 B/kt、J/kt 等效值(含 Kt)。至少 3 个有效点才可信。
    返回 {J, B, corner_hz, fit_err_pct, point_count, delay_ms} 或 None。
    """
    valid = [p for p in points if p.valid and math.isfinite(p.gain_db)
             and abs(p.input_complex) > 1e-12]
    if len(valid) < 3:
        return None
    # 极坐标预分解: 旋转 z·e^(-jωτ) 只需角度平移, 网格搜索成本恒定
    ws = [2.0 * math.pi * p.f_actual_hz for p in valid]
    zs = [p.input_complex / p.output_complex for p in valid]
    rs = [abs(z) for z in zs]
    ths = [cmath.phase(z) for z in zs]

    best = None  # (err, tau, J, B)
    for k in range(161):  # τ = 0 ~ 8ms, 步进 50µs
        tau = k * 5e-5
        sw = 0.0    # Σ ω²
        swi = 0.0   # Σ ω·Im(z_τ)
        re_sum = 0.0
        for w, r, th in zip(ws, rs, ths):
            ang = th - w * tau
            sw += w * w
            swi += w * r * math.sin(ang)
            re_sum += r * math.cos(ang)
        if sw <= 0.0:
            continue
        J = swi / sw
        B = re_sum / len(valid)
        if J <= 0.0:
            continue  # 相位超前(如速度方向反接), 该 τ 下无物理解
        if B < 0.0:
            B = 0.0   # 阻尼非负: 轻阻尼下按纯惯量退化
        err = 0.0
        for w, r, th in zip(ws, rs, ths):
            ang = th - w * tau
            err += (r * math.cos(ang) - B) ** 2 + (r * math.sin(ang) - w * J) ** 2
        if best is None or err < best[0]:
            best = (err, tau, J, B)
    if best is None:
        return None
    _, tau, J, B = best
    # 拟合残差: 模型 |G| 与实测 |G| 的相对误差 RMS(幅值域, 与 τ 无关)
    err = 0.0
    for p in valid:
        w = 2.0 * math.pi * p.f_actual_hz
        model = 1.0 / math.sqrt((w * J) ** 2 + B * B)
        meas = abs(p.output_complex / p.input_complex)
        err += ((model - meas) / meas) ** 2
    return {"J": J, "B": B, "corner_hz": B / (2.0 * math.pi * J),
            "fit_err_pct": 100.0 * math.sqrt(err / len(valid)),
            "point_count": len(valid), "delay_ms": tau * 1000.0}


# ===================== 方向二：闭环验收指标(验证) =====================

def closed_loop_metrics(points: list) -> dict | None:
    """由实测闭环传函 T(jω) 计算验收指标。

    - 带宽: |T| 相对低频参考(最低频点增益)下降 3dB 的频率
    - Mr: 闭环峰值(低频参考之上的最大增益)
    - PM/GM: 由 L = T/(1−T) 反推; |1−T| < 0.005 的低频点
      噪声被放大不参与(反推只在中高频可信)
    返回 {ref_db, bandwidth_hz, peak_db, pm_deg, gm_db} 或 None。
    """
    valid = sorted((p for p in points if p.valid
                    and math.isfinite(p.gain_db)), key=lambda p: p.f_actual_hz)
    if len(valid) < 3:
        return None
    ref_db = valid[0].gain_db
    fs = [p.f_actual_hz for p in valid]
    dbs = [p.gain_db - ref_db for p in valid]  # 归一化到低频参考
    bandwidth = None
    for i in range(len(fs)):
        if dbs[i] <= -3.0:
            if i == 0:
                bandwidth = fs[0]
            else:
                t = (-3.0 - dbs[i - 1]) / (dbs[i] - dbs[i - 1])
                bandwidth = fs[i - 1] + t * (fs[i] - fs[i - 1])
            break
    peak_db = ref_db + max(dbs) if max(dbs) > 0.5 else None
    # L = T/(1−T) 反推开环
    lfs, lmags, lphases = [], [], []
    for p in valid:
        t = p.output_complex / p.input_complex
        if abs(1.0 - t) < 5e-3:
            continue  # 低频 T≈1, 1−T 有效数字不足
        l = t / (1.0 - t)
        lfs.append(p.f_actual_hz)
        lmags.append(20.0 * math.log10(max(abs(l), 1e-30)))
        lphases.append(math.degrees(cmath.phase(l)))
    # 相位连续化(unwrap): 滞后系统相位滚过 ±180° 时 cmath.phase 会跳变
    for i in range(1, len(lphases)):
        while lphases[i] - lphases[i - 1] > 180.0:
            lphases[i] -= 360.0
        while lphases[i] - lphases[i - 1] < -180.0:
            lphases[i] += 360.0

    def _cross(ys, level):
        for i in range(1, len(ys)):
            if ys[i - 1] != ys[i] and (ys[i - 1] - level) * (ys[i] - level) <= 0:
                t = (level - ys[i - 1]) / (ys[i] - ys[i - 1])
                return i - 1, t
        return None

    pm = gm = None
    if len(lfs) >= 2:
        cross = _cross(lmags, 0.0)  # |L|=1 穿越点
        if cross is not None:
            i, t = cross
            ph = lphases[i]
            if i + 1 < len(lfs):
                ph = lphases[i] + t * (lphases[i + 1] - lphases[i])
            pm = 180.0 + ph
        cross = _cross(lphases, -180.0)  # 相位 -180° 穿越点
        if cross is not None:
            i, t = cross
            mag = lmags[i] if i + 1 >= len(lfs) else \
                lmags[i] + t * (lmags[i + 1] - lmags[i])
            gm = -mag
    return {"ref_db": ref_db, "bandwidth_hz": bandwidth,
            "peak_db": peak_db, "pm_deg": pm, "gm_db": gm,
            "lpoint_count": len(lfs)}
