"""系统控制面板: 使能/失能/停止/急停/清障/待机"""

from PyQt6.QtWidgets import QGroupBox, QGridLayout, QPushButton
from PyQt6.QtCore import pyqtSignal

from jmproto import JmCmd


class ControlPanel(QGroupBox):
    """系统控制按钮组。点击发出 command(cmd:int) 信号。

    set_enabled_state(enabled) 按上报的真实使能态高亮对应按钮(当前所处态):
      已使能 -> "使能" 高亮; 未使能 -> "去使能" 高亮。
    """

    command = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__("系统控制", parent)
        self._enabled_state = None     # 当前上报使能态(None=未知)
        self._build()

    def _build(self):
        layout = QGridLayout(self)

        style_danger = "background-color: #F44336; color: white; padding: 6px;"
        style_normal = "padding: 6px;"

        self._btn_enable = QPushButton("使能")
        self._btn_disable = QPushButton("去使能")
        self._btn_enable.clicked.connect(lambda: self.command.emit(int(JmCmd.ENABLE)))
        self._btn_disable.clicked.connect(lambda: self.command.emit(int(JmCmd.DISABLE)))
        layout.addWidget(self._btn_enable, 0, 0)
        layout.addWidget(self._btn_disable, 0, 1)

        # 其余命令按钮(样式固定)
        others = [
            ("停止", JmCmd.STOP, style_normal, 1, 0),
            ("待机", JmCmd.IDLE, style_normal, 1, 1),
            ("急停", JmCmd.ESTOP, style_danger, 2, 0),
            ("清除故障", JmCmd.CLEAR_FAULT, style_normal, 2, 1),
        ]
        for label, cmd, style, row, col in others:
            btn = QPushButton(label)
            btn.setStyleSheet(style)
            btn.clicked.connect(lambda _, c=int(cmd): self.command.emit(c))
            layout.addWidget(btn, row, col)

        self._refresh_enable_buttons()

    def set_enabled_state(self, enabled: bool):
        """由主窗口按上报的 enable 字段调用, 高亮当前所处态对应按钮。"""
        self._enabled_state = bool(enabled)
        self._refresh_enable_buttons()

    def _refresh_enable_buttons(self):
        # 高亮"当前所处态": 已使能高亮"使能"(绿), 未使能高亮"去使能"(橙); 未知则都置灰常态
        on = self._enabled_state
        active = "color: white; padding: 6px; font-weight: bold; border: 2px solid; "
        idle = "padding: 6px; "
        if on is True:
            self._btn_enable.setStyleSheet(active + "background-color: #2E9E6A; border-color: #5FE6AC;")
            self._btn_disable.setStyleSheet(idle)
        elif on is False:
            self._btn_enable.setStyleSheet(idle)
            self._btn_disable.setStyleSheet(active + "background-color: #E08A1E; border-color: #FFB454;")
        else:
            self._btn_enable.setStyleSheet(idle)
            self._btn_disable.setStyleSheet(idle)
