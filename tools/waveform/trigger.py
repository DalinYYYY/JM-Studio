"""
触发捕获: 类似示波器的触发功能。

触发条件:
- 源通道 (任意可见通道)
- 边沿 (上升沿 / 下降沿)
- 触发电平
- 触发抑制 (hold_s): 两次触发间最小间隔, 防止抖动重复触发

满足条件时, WaveformPlot 拍当前可见区快照, 通过 capture_armed 信号发出,
供 FFT 面板或其他分析模块使用。
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QLabel, QSpinBox, QVBoxLayout, QCheckBox,
)


class TriggerDialog(QDialog):
    """触发配置对话框"""

    def __init__(self, parent=None, channel_keys: Optional[list] = None):
        super().__init__(parent)
        self.setWindowTitle("触发设置")
        self._channel_keys = channel_keys or []
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # 启用
        self._chk_enable = QCheckBox("启用触发捕获")
        layout.addWidget(self._chk_enable)

        # 源通道
        form = QFormLayout()
        self._cmb_channel = QComboBox()
        for k in self._channel_keys:
            self._cmb_channel.addItem(k, k)
        form.addRow("源通道:", self._cmb_channel)

        # 边沿
        self._cmb_edge = QComboBox()
        self._cmb_edge.addItem("上升沿", 'rising')
        self._cmb_edge.addItem("下降沿", 'falling')
        form.addRow("边沿:", self._cmb_edge)

        # 触发电平
        self._spin_level = QDoubleSpinBox()
        self._spin_level.setRange(-1e6, 1e6)
        self._spin_level.setDecimals(4)
        self._spin_level.setSingleStep(0.1)
        form.addRow("触发电平:", self._spin_level)

        # 触发抑制
        self._spin_hold = QDoubleSpinBox()
        self._spin_hold.setRange(0.0, 60.0)
        self._spin_hold.setDecimals(3)
        self._spin_hold.setSingleStep(0.1)
        self._spin_hold.setSuffix(" s")
        self._spin_hold.setValue(0.5)
        form.addRow("触发抑制:", self._spin_hold)

        layout.addLayout(form)

        # 说明
        hint = QLabel(
            "说明: 满足条件时拍快照当前可见区数据, 通过 capture_armed 信号发出。\n"
            "触发抑制防止抖动重复触发。"
        )
        hint.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(hint)

        # 按钮
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def set_config(self, enabled: bool, key: Optional[str], edge: str,
                   level: float, hold_s: float):
        self._chk_enable.setChecked(enabled)
        if key:
            idx = self._cmb_channel.findData(key)
            if idx >= 0:
                self._cmb_channel.setCurrentIndex(idx)
        idx = self._cmb_edge.findData(edge)
        if idx >= 0:
            self._cmb_edge.setCurrentIndex(idx)
        self._spin_level.setValue(level)
        self._spin_hold.setValue(hold_s)

    def get_config(self) -> dict:
        return {
            'enabled': self._chk_enable.isChecked(),
            'key': self._cmb_channel.currentData() or None,
            'edge': self._cmb_edge.currentData() or 'rising',
            'level': self._spin_level.value(),
            'hold_s': self._spin_hold.value(),
        }
