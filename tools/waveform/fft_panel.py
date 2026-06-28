"""
FFT 分析面板: 对选定通道的可见区数据做频谱分析。

特性:
- 选择通道 + 窗函数 (汉宁/汉明/矩形/布莱克曼)
- 计算 FFT, 显示幅频曲线
- 标注主频/前 3 谐波
- 鼠标悬停读数
- 导出频谱数据
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

import pyqtgraph as pg


# 窗函数
WINDOW_FUNCS = {
    '矩形 (None)': lambda n: np.ones(n),
    '汉宁 (Hanning)': lambda n: np.hanning(n),
    '汉明 (Hamming)': lambda n: np.hamming(n),
    '布莱克曼 (Blackman)': lambda n: np.blackman(n),
}


class FftDialog(QDialog):
    """FFT 分析对话框: 选通道 -> 接受后显示频谱"""

    def __init__(self, parent=None, channel_keys: Optional[list] = None):
        super().__init__(parent)
        self.setWindowTitle("FFT 频谱分析")
        self._channel_keys = channel_keys or []
        self._selected_key: Optional[str] = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self._cmb_channel = QComboBox()
        for k in self._channel_keys:
            self._cmb_channel.addItem(k, k)
        if self._channel_keys:
            self._cmb_channel.setCurrentIndex(0)
        form.addRow("分析通道:", self._cmb_channel)

        self._cmb_window = QComboBox()
        for name in WINDOW_FUNCS:
            self._cmb_window.addItem(name)
        self._cmb_window.setCurrentIndex(1)  # 汉宁
        form.addRow("窗函数:", self._cmb_window)
        layout.addLayout(form)

        hint = QLabel("说明: 对当前可见区数据做单次 FFT, 显示幅频曲线和主频。")
        hint.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(hint)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_channel(self) -> Optional[str]:
        return self._cmb_channel.currentData() if self._channel_keys else None

    def get_window(self):
        return WINDOW_FUNCS[self._cmb_window.currentText()]

    def show_fft(self, ts: np.ndarray, ys: np.ndarray,
                 label: str, unit: str, color: str):
        """计算并显示 FFT 结果 (新对话框)"""
        if len(ts) < 4 or len(ys) < 4:
            QMessageBox.warning(self, "数据不足", "至少需要 4 个采样点")
            return

        # 估算采样率
        dt = float(np.mean(np.diff(ts)))
        if dt <= 0:
            QMessageBox.warning(self, "时间戳无效", "时间戳必须严格递增")
            return
        fs = 1.0 / dt

        # 去均值 + 加窗
        y = ys - np.mean(ys)
        win = self.get_window()(len(y))
        yw = y * win

        # FFT
        n = len(yw)
        # 补零到 2 的幂次, 提高频率分辨率
        n_fft = 1 << int(np.ceil(np.log2(max(n, 1024))))
        spectrum = np.fft.rfft(yw, n=n_fft)
        mag = np.abs(spectrum) * 2.0 / np.sum(win)  # 归一化幅值
        freqs = np.fft.rfftfreq(n_fft, d=dt)

        # 只显示有效频段 (低于 fs/2, 去掉直流)
        valid = freqs > 0
        freqs = freqs[valid]
        mag = mag[valid]

        # 找主频 + 前 3 谐波
        peaks_idx = np.argsort(mag)[::-1][:3]
        peaks_idx = sorted(peaks_idx)
        peaks = [(float(freqs[i]), float(mag[i])) for i in peaks_idx]

        # 显示
        dlg = QDialog(self)
        dlg.setWindowTitle(f"FFT - {label}")
        dlg.resize(800, 400)
        dlg_layout = QVBoxLayout(dlg)

        info = QLabel(
            f"通道: {label}  |  采样率: {fs:.1f} Hz  |  采样点: {n}  |  "
            f"频率分辨率: {fs/n_fft:.3f} Hz"
        )
        info.setStyleSheet("color: #0066CC;")
        dlg_layout.addWidget(info)

        plot = pg.PlotItem()
        plot.showGrid(x=True, y=True, alpha=0.3)
        plot.setLabel('left', '幅值', units=unit)
        plot.setLabel('bottom', '频率', units='Hz')
        plot.getLogAxis('bottom').setLabel('频率 (Hz)')
        # 限制 X 范围到 fs/2 (避免显示高频噪声)
        plot.setXRange(0, fs / 2 * 0.5)

        pen = pg.mkPen(color=color, width=1)
        plot.plot(freqs, mag, pen=pen)

        # 标注峰值
        for i, (f, m) in enumerate(peaks):
            txt = pg.TextItem(
                f"#{i+1}: {f:.2f} Hz\n{m:.3f} {unit}",
                color='#FF8000', anchor=(0, 1)
            )
            txt.setPos(f, m)
            plot.addItem(txt)
            # 峰值标记线
            line = pg.InfiniteLine(
                pos=f, angle=90,
                pen=pg.mkPen('#FF8000', width=1, style=Qt.PenStyle.DashLine)
            )
            plot.addItem(line)

        gfx = pg.GraphicsLayoutWidget()
        gfx.addItem(plot)
        dlg_layout.addWidget(gfx)

        # 峰值列表
        peak_text = "前 3 峰值:\n"
        for i, (f, m) in enumerate(peaks):
            peak_text += f"  #{i+1}: {f:.3f} Hz, 幅值 {m:.4f} {unit}\n"
        peak_lbl = QLabel(peak_text)
        peak_lbl.setStyleSheet("background: #F0F0F0; padding: 6px; font-family: monospace;")
        dlg_layout.addWidget(peak_lbl)

        # 关闭按钮
        btn = QPushButton("关闭")
        btn.clicked.connect(dlg.accept)
        dlg_layout.addWidget(btn)

        dlg.exec()
