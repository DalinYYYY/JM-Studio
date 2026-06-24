"""遥控模式面板: 按 JmTlmBit 各位选择上传数据种类 + 上传周期, 使能/停止周期上报。

遥控模式 = 周期性无应答上传: 上位机配置 mask(数据种类) + period(周期), 点击使能后
下位机按配置主动周期上报 0xCA 数据帧(不逐帧应答), 点击停止后下位机停止上报。
开关通过 SET_TELEMETRY(0xCB) 下发 enable+mask+period, 仅 0xCB 回单次 ACK。
"""

from PyQt6.QtWidgets import (
    QGroupBox, QGridLayout, QCheckBox, QLabel, QSpinBox, QPushButton,
)
from PyQt6.QtCore import pyqtSignal

from jmproto import JmTlmBit


class TelemetryPanel(QGroupBox):
    """遥控模式(周期无应答上报)。

    发出 apply_telemetry(enable, mask, period_ms): enable=True 使能周期上报, False 停止。
    """

    apply_telemetry = pyqtSignal(bool, int, int)

    def __init__(self, parent=None):
        super().__init__("遥控模式 / 周期上报", parent)
        self._checks = []      # (mask_bit, QCheckBox)
        self._running = False  # 当前是否处于上报使能态
        self._build()

    def _build(self):
        layout = QGridLayout(self)

        # 上传数据种类勾选(每行2个)
        for i, (bit, label) in enumerate(JmTlmBit.ITEMS):
            chk = QCheckBox(label)
            # 默认勾选最常用项
            if bit in (JmTlmBit.POS_VEL, JmTlmBit.BUS, JmTlmBit.TEMP, JmTlmBit.STATE):
                chk.setChecked(True)
            self._checks.append((bit, chk))
            layout.addWidget(chk, i // 2, i % 2)

        next_row = (len(JmTlmBit.ITEMS) + 1) // 2

        # 上报周期
        layout.addWidget(QLabel("上传周期(ms):"), next_row, 0)
        self._spin_period = QSpinBox()
        self._spin_period.setRange(1, 5000)
        self._spin_period.setValue(20)
        layout.addWidget(self._spin_period, next_row, 1)

        # 使能/停止开关(双态)
        self._btn_toggle = QPushButton("使能周期上报")
        self._btn_toggle.setCheckable(True)
        self._btn_toggle.clicked.connect(self._on_toggle)
        layout.addWidget(self._btn_toggle, next_row + 1, 0, 1, 2)
        self._apply_button_style()

    def current_mask(self) -> int:
        mask = 0
        for bit, chk in self._checks:
            if chk.isChecked():
                mask |= bit
        return mask

    def _apply_button_style(self):
        """按当前使能态刷新按钮文案与配色"""
        for _bit, chk in self._checks:
            chk.setEnabled(not self._running)
        self._spin_period.setEnabled(not self._running)

        if self._running:
            self._btn_toggle.setText("停止周期上报")
            self._btn_toggle.setStyleSheet(
                "background-color: #E53935; color: white; padding: 6px;")
        else:
            self._btn_toggle.setText("使能周期上报")
            self._btn_toggle.setStyleSheet(
                "background-color: #009688; color: white; padding: 6px;")

    def _on_toggle(self):
        # 进入使能态须至少选一种数据; 否则回弹为停止态
        enable = self._btn_toggle.isChecked()
        if enable and self.current_mask() == 0:
            self._btn_toggle.setChecked(False)
            return
        self._running = enable
        self._apply_button_style()
        self.apply_telemetry.emit(enable, self.current_mask(), self._spin_period.value())

    def set_running(self, running: bool):
        """外部(如断开连接/收到 NACK)同步开关状态, 不再次发命令"""
        self._running = running
        self._btn_toggle.setChecked(running)
        self._apply_button_style()
