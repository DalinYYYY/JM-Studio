"""故障历史面板: 记录所有故障发生/恢复事件, 便于回溯。

订阅 TwinEngineBridge 的 fault_occurred 信号 + 遥测中的故障清除事件,
按时间倒序展示故障历史表 (时间 / 故障码 / 故障名 / 系统状态 / 来源)。

并提供一键故障注入按钮 (OVER_CURRENT / OVER_TEMP_FET / COMM_LOST 等),
便于测试故障响应。
"""
from collections import deque
import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ui.theme import theme
from transport.virtual_engine import Fault


# Fault 位 -> 中文名 (覆盖 FaultDetector 所有故障位)
_FAULT_NAMES = {
    Fault.OVER_CURRENT: "过流",
    Fault.OVER_VOLTAGE: "过压",
    Fault.UNDER_VOLTAGE: "欠压",
    Fault.OVER_TEMP_FET: "FET过温",
    Fault.OVER_TEMP_MOTOR: "电机过温",
    Fault.POS_LIMIT: "位置超限",
    Fault.FOLLOW_ERROR: "跟随误差",
    Fault.COMM_LOST: "通信丢失",
    Fault.OVER_SPEED: "超速",
    Fault.INJECTED: "注入故障",
}


def _fault_bits_to_names(bits: int) -> str:
    """将故障位掩码转换为中文名列表。"""
    if bits == 0:
        return ""
    names = []
    for bit, name in _FAULT_NAMES.items():
        if bits & bit:
            names.append(name)
    return ", ".join(names)


# 一键故障注入列表: (按钮文字, 故障位)
_INJECT_BUTTONS = [
    ("过流", Fault.OVER_CURRENT),
    ("过压", Fault.OVER_VOLTAGE),
    ("欠压", Fault.UNDER_VOLTAGE),
    ("FET过温", Fault.OVER_TEMP_FET),
    ("电机过温", Fault.OVER_TEMP_MOTOR),
    ("位置超限", Fault.POS_LIMIT),
    ("跟随误差", Fault.FOLLOW_ERROR),
    ("通信丢失", Fault.COMM_LOST),
    ("超速", Fault.OVER_SPEED),
]


class TwinFaultHistoryPanel(QWidget):
    """数字孪生故障历史面板。"""

    # 用户点击注入按钮时发出 (fault_bit)
    inject_requested = pyqtSignal(int)
    # 用户点击清障按钮时发出
    clear_requested = pyqtSignal()

    MAX_ROWS = 500

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = deque(maxlen=self.MAX_ROWS)
        self._last_fault_flags = 0
        self._last_sys_state = ""
        self._t0 = time.monotonic()
        self._build()

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(6)

        # 顶部: 一键注入按钮组
        inj_bar = QHBoxLayout()
        inj_bar.addWidget(QLabel("一键注入故障:"))
        self._inject_buttons = []
        for label, bit in _INJECT_BUTTONS:
            btn = QPushButton(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ padding: 2px 8px; }}"
                f"QPushButton:hover {{ background: {theme.hex('danger')}; color: white; }}")
            btn.clicked.connect(lambda _=False, b=bit: self.inject_requested.emit(b))
            inj_bar.addWidget(btn)
            self._inject_buttons.append(btn)
        inj_bar.addStretch()
        btn_clear_inj = QPushButton("清除注入")
        btn_clear_inj.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_clear_inj.clicked.connect(self.clear_requested.emit)
        inj_bar.addWidget(btn_clear_inj)
        v.addLayout(inj_bar)

        # 工具栏
        bar = QHBoxLayout()
        bar.addWidget(QLabel("故障历史"))
        bar.addStretch()
        self._btn_clear = QPushButton("清空历史")
        self._btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear.clicked.connect(self._clear_history)
        bar.addWidget(self._btn_clear)
        self._lbl_count = QLabel("0")
        self._lbl_count.setStyleSheet(f"color: {theme.hex('muted')};")
        bar.addWidget(self._lbl_count)
        v.addLayout(bar)

        # 故障历史表
        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["时间", "故障码", "故障名", "系统状态", "事件类型"])
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setDefaultSectionSize(24)
        v.addWidget(self._table, 1)

    # ---------- 公共 API ----------
    def on_fault_occurred(self, flags: int, desc: str, sys_state: str = ""):
        """故障上升沿: 新增故障行。"""
        elapsed = time.monotonic() - self._t0
        ts = f"{elapsed:7.2f}s"
        self._rows.append((ts, f"0x{flags:04X}", _fault_bits_to_names(flags) or desc,
                           sys_state or self._last_sys_state, "触发"))
        self._refresh_table()

    def on_telemetry(self, t: dict):
        """从遥测中检测故障清除事件。"""
        flags = int(t.get("fault_flags", 0) or 0)
        sys_state = t.get("sys_state_name", "") or ""
        # 故障下降沿: 之前的故障位现在被清除
        cleared = self._last_fault_flags & (~flags)
        if cleared:
            elapsed = time.monotonic() - self._t0
            ts = f"{elapsed:7.2f}s"
            self._rows.append((ts, f"0x{cleared:04X}", _fault_bits_to_names(cleared),
                               sys_state, "清除"))
            self._refresh_table()
        self._last_fault_flags = flags
        self._last_sys_state = sys_state

    def _clear_history(self):
        self._rows.clear()
        self._refresh_table()

    def _refresh_table(self):
        self._table.setRowCount(len(self._rows))
        # 倒序展示 (最新在顶部)
        for row_idx, (ts, code, name, state, etype) in enumerate(reversed(self._rows)):
            items = [ts, code, name, state, etype]
            for col, text in enumerate(items):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                # 触发用红色, 清除用绿色
                if col == 4 and etype == "触发":
                    item.setForeground(Qt.GlobalColor.red)
                elif col == 4 and etype == "清除":
                    item.setForeground(Qt.GlobalColor.green)
                self._table.setItem(row_idx, col, item)
        self._lbl_count.setText(str(len(self._rows)))

    def apply_theme(self):
        for btn in self._inject_buttons:
            btn.setStyleSheet(
                f"QPushButton {{ padding: 2px 8px; }}"
                f"QPushButton:hover {{ background: {theme.hex('danger')}; color: white; }}")
        self._lbl_count.setStyleSheet(f"color: {theme.hex('muted')};")
