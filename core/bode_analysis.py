"""Bode 扫频 TRACE 分组和同步解调。"""

from __future__ import annotations

import cmath
import csv
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path


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

    def csv_row(self):
        d = asdict(self)
        d.pop("input_complex", None)
        d.pop("output_complex", None)
        return d


class BodeAnalyzer:
    """按 TRACE_FLAG_POINT_START/END 聚合一个扫频会话。"""

    def __init__(self, channel_mask: int, pll_bandwidth_hz: float = 100.0):
        self.channel_mask = int(channel_mask)
        self.pll_bandwidth_hz = float(pll_bandwidth_hz)
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
                           overflow_count=meta["overflow_count"])
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
        wx = 2.0 * math.pi * freq / rate if rate > 0 else 0.0
        x = sum((vx - mean_x) * cmath.exp(-1j * wx * i)
                for i, (vx, _) in enumerate(rows))
        y = sum((vy - mean_y) * cmath.exp(-1j * wx * i)
                for i, (_, vy) in enumerate(rows))
        result.input_complex = 2.0 * x / len(rows)
        result.output_complex = 2.0 * y / len(rows)
        if abs(result.input_complex) <= 1e-12:
            reasons.append("输入频点能量过小")
        else:
            h = result.output_complex / result.input_complex
            result.gain_db = 20.0 * math.log10(max(abs(h), 1e-30))
            result.phase_deg = math.degrees(cmath.phase(h))
            residual = sum(abs((vy - mean_y) -
                               (result.output_complex.real * math.cos(wx * i) -
                                result.output_complex.imag * math.sin(wx * i))) ** 2
                            for i, (_, vy) in enumerate(rows))
            signal = max(abs(result.output_complex) ** 2 * len(rows), 1e-30)
            result.snr_db = 10.0 * math.log10(signal / max(residual, 1e-30))
            # SNR 低只降精度不否定测量: 标注警告, 仍参与绘图(用户自行取舍)
            if result.snr_db < 10.0:
                result.valid_reason = "SNR低"
            elif result.snr_db < 20.0:
                result.valid_reason = "SNR偏低"
        # PLL 带宽仅作估计器影响提示: 下位机 PLL 带宽运行时可调,
        # 上位机无法得知准确值, 不据此判无效(默认 0 = 完全不判定)
        if self.pll_bandwidth_hz > 0:
            if freq > self.pll_bandwidth_hz:
                result.estimator_warning = "超过PLL带宽"
            elif freq > 0.5 * self.pll_bandwidth_hz:
                result.estimator_warning = "接近PLL带宽"
            elif freq > 0.3 * self.pll_bandwidth_hz:
                result.estimator_warning = "存在PLL影响"
        result.valid = not reasons
        if reasons:
            result.valid_reason = ";".join(reasons)
        elif result.valid_reason in ("", "未完成"):  # 保留SNR警告, 无警告才标"有效"
            result.valid_reason = "有效"
        self.points[point] = result
        return result

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
    """按一阶机械模型 G(s) = 1/(J·s + B) 拟合有效频点。

    对每个频点取 z = 1/H(jω)，则 Re(z) = B、Im(z) = ω·J，
    对全部有效点做最小二乘。输入为电流模式(1)时结果为
    B/kt、J/kt 等效值(含 Kt)。至少 3 个有效点才可信。
    返回 {J, B, corner_hz, fit_err_pct, point_count} 或 None。
    """
    valid = [p for p in points if p.valid and math.isfinite(p.gain_db)
             and abs(p.input_complex) > 1e-12]
    if len(valid) < 3:
        return None
    sw = 0.0   # Σ ω²
    swi = 0.0  # Σ ω·Im(z)
    re_sum = 0.0
    for p in valid:
        w = 2.0 * math.pi * p.f_actual_hz
        z = 1.0 / (p.output_complex / p.input_complex)
        sw += w * w
        swi += w * z.imag
        re_sum += z.real
    J = swi / sw if sw > 0 else 0.0
    B = re_sum / len(valid)
    if J <= 0.0 or B <= 0.0:
        return None
    # 拟合残差: 模型 |G| 与实测 |G| 的相对误差 RMS
    err = 0.0
    for p in valid:
        w = 2.0 * math.pi * p.f_actual_hz
        model = 1.0 / math.sqrt((w * J) ** 2 + B * B)
        meas = abs(p.output_complex / p.input_complex)
        err += ((model - meas) / meas) ** 2
    return {"J": J, "B": B, "corner_hz": B / (2.0 * math.pi * J),
            "fit_err_pct": 100.0 * math.sqrt(err / len(valid)),
            "point_count": len(valid)}


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
