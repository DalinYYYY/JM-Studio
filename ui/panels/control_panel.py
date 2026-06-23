"""系统控制面板: 使能/失能/停止/急停/清障/待机"""

from PyQt6.QtWidgets import QGroupBox, QGridLayout, QPushButton
from PyQt6.QtCore import pyqtSignal

from jmproto import JmCmd


class ControlPanel(QGroupBox):
    """系统控制按钮组。点击发出 command(cmd:int) 信号。"""

    command = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__("系统控制", parent)
        self._build()

    def _build(self):
        layout = QGridLayout(self)

        style_enable = "background-color: #2196F3; color: white; padding: 6px;"
        style_disable = "background-color: #FF9800; color: white; padding: 6px;"
        style_danger = "background-color: #F44336; color: white; padding: 6px;"
        style_normal = "padding: 6px;"

        # (标签, cmd, 样式, row, col)
        buttons = [
            ("使能 ENABLE", JmCmd.ENABLE, style_enable, 0, 0),
            ("去使能 DISABLE", JmCmd.DISABLE, style_disable, 0, 1),
            ("停止 STOP", JmCmd.STOP, style_normal, 1, 0),
            ("待机 IDLE", JmCmd.IDLE, style_normal, 1, 1),
            ("急停 ESTOP", JmCmd.ESTOP, style_danger, 2, 0),
            ("清除故障 CLEAR_FAULT", JmCmd.CLEAR_FAULT, style_normal, 2, 1),
        ]
        for label, cmd, style, row, col in buttons:
            btn = QPushButton(label)
            btn.setStyleSheet(style)
            btn.clicked.connect(lambda _, c=int(cmd): self.command.emit(c))
            layout.addWidget(btn, row, col)
