"""遥测订阅面板: 按 JmTlmBit 各位生成勾选框, 组合 mask 下发 SET_TELEMETRY"""

from PyQt6.QtWidgets import (
    QGroupBox, QGridLayout, QCheckBox, QLabel, QSpinBox, QPushButton, QWidget, QHBoxLayout,
)
from PyQt6.QtCore import pyqtSignal

from jmproto import JmTlmBit


class TelemetryPanel(QGroupBox):
    """遥测订阅。发出 apply_telemetry(mask, period_ms) / poll_toggled(enabled, period)。"""

    apply_telemetry = pyqtSignal(int, int)
    poll_toggled = pyqtSignal(bool, int)

    def __init__(self, parent=None):
        super().__init__("遥测 / 反馈", parent)
        self._checks = []   # (mask_bit, QCheckBox)
        self._build()

    def _build(self):
        layout = QGridLayout(self)

        # 遥测分组勾选(每行2个)
        for i, (bit, label) in enumerate(JmTlmBit.ITEMS):
            chk = QCheckBox(label)
            # 默认勾选最常用项
            if bit in (JmTlmBit.POS_VEL, JmTlmBit.BUS, JmTlmBit.TEMP, JmTlmBit.STATE):
                chk.setChecked(True)
            self._checks.append((bit, chk))
            layout.addWidget(chk, i // 2, i % 2)

        next_row = (len(JmTlmBit.ITEMS) + 1) // 2

        # 上报周期
        layout.addWidget(QLabel("上报周期(ms):"), next_row, 0)
        self._spin_period = QSpinBox()
        self._spin_period.setRange(0, 5000)
        self._spin_period.setValue(20)
        layout.addWidget(self._spin_period, next_row, 1)

        # 应用遥测订阅
        self._btn_apply = QPushButton("应用遥测订阅")
        self._btn_apply.setStyleSheet("background-color: #009688; color: white; padding: 6px;")
        self._btn_apply.clicked.connect(self._on_apply)
        layout.addWidget(self._btn_apply, next_row + 1, 0, 1, 2)

        # 轮询兜底
        poll_row = QWidget()
        poll_layout = QHBoxLayout(poll_row)
        poll_layout.setContentsMargins(0, 0, 0, 0)
        self._chk_poll = QCheckBox("轮询兜底(无遥测时)")
        self._chk_poll.stateChanged.connect(self._on_poll)
        poll_layout.addWidget(self._chk_poll)
        poll_layout.addWidget(QLabel("周期(ms):"))
        self._spin_poll = QSpinBox()
        self._spin_poll.setRange(10, 5000)
        self._spin_poll.setValue(100)
        self._spin_poll.valueChanged.connect(self._on_poll)
        poll_layout.addWidget(self._spin_poll)
        layout.addWidget(poll_row, next_row + 2, 0, 1, 2)

    def current_mask(self) -> int:
        mask = 0
        for bit, chk in self._checks:
            if chk.isChecked():
                mask |= bit
        return mask

    def _on_apply(self):
        self.apply_telemetry.emit(self.current_mask(), self._spin_period.value())

    def _on_poll(self, *_):
        self.poll_toggled.emit(self._chk_poll.isChecked(), self._spin_poll.value())
