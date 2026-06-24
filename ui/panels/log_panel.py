"""通信日志面板: HTML 着色 + 可开关时间戳/TX/RX/RAW/自动滚动"""

import datetime
import html
from collections import deque

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QWidget, QPushButton,
    QCheckBox, QTextEdit, QSizePolicy,
)
from PyQt6.QtGui import QFont, QTextCursor

from jmproto import cmd_name, err_name


# 配色(深色文本编辑器友好)
_COLOR = {
    'tx':   '#2962FF',   # 蓝 - 发送
    'rx':   '#00897B',   # 青绿 - 接收
    'raw':  '#9E9E9E',   # 灰 - 原始帧
    'warn': '#F9A825',   # 琥珀 - 警告
    'err':  '#E53935',   # 红 - 错误
    'info': '#616161',   # 深灰 - 普通信息
    'ts':   '#9E9E9E',   # 灰 - 时间戳
}


class LogPanel(QGroupBox):
    """通信日志(TX/RX/原始帧)。

    对外接口:
        log_tx(cmd, payload)  发送帧
        log_rx(cmd, payload)  接收帧(由 raw_frame 驱动)
        log(msg)              普通信息
        log_warn(msg) / log_err(msg)
    显示由各开关控制: 时间戳 / 显示TX / 显示RX / 原始HEX / 自动滚动。
    """

    MAX_BLOCKS = 2000
    DEFAULT_FLUSH_PERIOD_MS = 50
    DEFAULT_MAX_PENDING = 2000
    DEFAULT_MAX_LINES_PER_FLUSH = 200

    def __init__(self, parent=None):
        super().__init__("通信日志", parent)
        self._pending = deque()
        self._max_pending = self.DEFAULT_MAX_PENDING
        self._max_lines_per_flush = self.DEFAULT_MAX_LINES_PER_FLUSH
        self._dropped = 0
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(self.DEFAULT_FLUSH_PERIOD_MS)
        self._flush_timer.timeout.connect(self.flush)
        self._build()
        self._flush_timer.start()

    def _build(self):
        layout = QVBoxLayout(self)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.document().setMaximumBlockCount(self.MAX_BLOCKS)
        self._log.setFont(QFont("Consolas", 9))
        self._log.setStyleSheet(
            "QTextEdit { background-color: #FAFAFA; border: 1px solid #DDD; }")
        layout.addWidget(self._log)

        # ---- 底部工具行 ----
        tool_row = QWidget()
        tool_layout = QHBoxLayout(tool_row)
        tool_layout.setContentsMargins(0, 0, 0, 0)
        tool_layout.setSpacing(8)

        self.chk_ts = QCheckBox("时间戳")
        self.chk_ts.setChecked(True)
        self.chk_tx = QCheckBox("显示发送")
        self.chk_tx.setChecked(True)
        self.chk_rx = QCheckBox("显示接收")
        self.chk_rx.setChecked(True)
        self.chk_raw = QCheckBox("原始HEX")
        self.chk_raw.setChecked(True)
        self.chk_autoscroll = QCheckBox("自动滚动")
        self.chk_autoscroll.setChecked(True)

        for chk in (self.chk_ts, self.chk_tx, self.chk_rx,
                    self.chk_raw, self.chk_autoscroll):
            tool_layout.addWidget(chk)

        self._btn_clear = QPushButton("清空日志")
        self._btn_clear.clicked.connect(self._log.clear)
        tool_layout.addSpacing(8)
        tool_layout.addWidget(self._btn_clear)

        self._footer_fill = QWidget()
        self._footer_fill.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tool_layout.addWidget(self._footer_fill, 1)

        layout.addWidget(tool_row)

    # ---------------- 配置 ----------------
    def set_flush_period_ms(self, period_ms: int):
        self._flush_timer.setInterval(max(10, int(period_ms)))

    def set_max_pending(self, max_pending: int):
        self._max_pending = max(100, int(max_pending))
        while len(self._pending) > self._max_pending:
            self._pending.popleft()
            self._dropped += 1

    # ---------------- 内部输出 ----------------
    def _queue(self, kind: str, body_html: str):
        """追加到显示队列, 由定时器批量刷新到 QTextEdit。"""
        if len(self._pending) >= self._max_pending:
            self._pending.popleft()
            self._dropped += 1
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._pending.append((kind, body_html, ts))

    def _append_html(self, kind: str, body_html: str, ts: str = None):
        """拼时间戳 + 着色正文, 追加一行并按需滚动。"""
        parts = []
        if self.chk_ts.isChecked():
            if ts is None:
                ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            parts.append(f'<span style="color:{_COLOR["ts"]}">[{ts}]</span> ')
        color = _COLOR.get(kind, _COLOR['info'])
        parts.append(f'<span style="color:{color}">{body_html}</span>')
        self._log.append("".join(parts))
        if self.chk_autoscroll.isChecked():
            self._log.moveCursor(QTextCursor.MoveOperation.End)

    def flush(self):
        """定时批量刷新日志, 避免高频 QTextEdit.append 卡住界面。"""
        if self._dropped:
            dropped = self._dropped
            self._dropped = 0
            self._append_html('warn', html.escape(f"日志显示队列已满, 丢弃 {dropped} 条旧日志"))

        count = min(len(self._pending), self._max_lines_per_flush)
        for _ in range(count):
            kind, body_html, ts = self._pending.popleft()
            self._append_html(kind, body_html, ts)

    @staticmethod
    def _hex(payload: bytes) -> str:
        return payload.hex(' ').upper() if payload else "(空)"

    # ---------------- 对外接口 ----------------
    def log_tx(self, cmd: int, payload: bytes):
        if not self.chk_tx.isChecked():
            return
        line = f"▲ TX  {html.escape(cmd_name(cmd))}(0x{cmd:02X})  [{len(payload)}B]"
        if self.chk_raw.isChecked():
            line += f"  {self._hex(payload)}"
        self._queue('tx', line)

    def log_rx(self, cmd: int, payload: bytes):
        if not self.chk_rx.isChecked():
            return
        line = f"▼ RX  {html.escape(cmd_name(cmd))}(0x{cmd:02X})  [{len(payload)}B]"
        if self.chk_raw.isChecked():
            line += f"  {self._hex(payload)}"
        self._queue('rx', line)

    def log(self, msg: str):
        self._queue('info', html.escape(msg))

    def log_warn(self, msg: str):
        self._queue('warn', "⚠ " + html.escape(msg))

    def log_err(self, msg: str):
        self._queue('err', "✖ " + html.escape(msg))
