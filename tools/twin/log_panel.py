"""事件日志面板: 记录状态变化/故障/命令/系统事件, 带时间戳与着色。

订阅 TwinEngineBridge 的 state_changed / fault_occurred 信号,
以及主窗口的命令派发, 自动追加日志行。
环形缓冲 (默认 2000 行), 超出自动丢弃最旧行, 避免无界增长。
"""

from collections import deque
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QTextCharFormat, QTextCursor, QColor
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from ui.theme import theme


# 事件类别 -> 颜色 theme 键
_CATEGORY_COLOR = {
    "STATE": "accent",     # 状态迁移
    "FAULT": "danger",     # 故障
    "CMD": "value",        # 命令派发
    "SYS": "muted",        # 系统事件 (启停/注入)
    "WARN": "warn",        # 警告
    "OK": "ok",            # 恢复
}


class TwinLogPanel(QWidget):
    """数字孪生事件日志面板。"""

    MAX_LINES = 2000

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lines = deque(maxlen=self.MAX_LINES)
        self._t0 = time.monotonic()
        self._build()

        # 30Hz 批量刷新, 避免每条日志都触发重绘
        self._dirty = False
        self._flush_timer = QTimer(self)
        self._flush_timer.timeout.connect(self._flush)
        self._flush_timer.start(33)

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(2, 2, 2, 2)

        # 工具栏
        bar = QHBoxLayout()
        bar.addWidget(QLabel("事件日志"))
        bar.addStretch()
        self._btn_clear = QPushButton("清空")
        self._btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear.clicked.connect(self.clear)
        bar.addWidget(self._btn_clear)
        self._lbl_count = QLabel("0")
        self._lbl_count.setStyleSheet(f"color: {theme.hex('muted')};")
        bar.addWidget(self._lbl_count)
        v.addLayout(bar)

        # 日志文本框
        self._edit = QTextEdit()
        self._edit.setReadOnly(True)
        self._edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._edit.setStyleSheet(
            f"QTextEdit {{ background: {theme.hex('panel_bg')};"
            f" color: {theme.hex('text')};"
            f" font-family: Consolas, 'Cascadia Mono', monospace;"
            f" font-size: 12px; border: none; }}")
        v.addWidget(self._edit, 1)

    # ---------- 公共 API ----------
    def log(self, category: str, msg: str):
        """追加一条日志。category 见 _CATEGORY_COLOR。"""
        if category not in _CATEGORY_COLOR:
            category = "SYS"
        elapsed = time.monotonic() - self._t0
        ts = f"[{elapsed:7.2f}s]"
        self._lines.append((category, ts, msg))
        self._dirty = True

    def clear(self):
        self._lines.clear()
        self._edit.clear()
        self._lbl_count.setText("0")

    # ---------- 内部 ----------
    def _flush(self):
        if not self._dirty:
            return
        self._dirty = False
        # 全量重绘 (deque 已限制行数, 总量可控)
        cursor = self._edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        for category, ts, msg in list(self._lines):
            color_key = _CATEGORY_COLOR.get(category, "text")
            fmt.setForeground(QColor(theme.hex(color_key)))
            cursor.setCharFormat(fmt)
            cursor.insertText(f"{ts} {category:5s} | {msg}\n")
        # 自动滚动到底部
        self._edit.setTextCursor(cursor)
        sb = self._edit.verticalScrollBar()
        sb.setValue(sb.maximum())
        self._lbl_count.setText(str(len(self._lines)))

    def apply_theme(self):
        self._edit.setStyleSheet(
            f"QTextEdit {{ background: {theme.hex('panel_bg')};"
            f" color: {theme.hex('text')};"
            f" font-family: Consolas, 'Cascadia Mono', monospace;"
            f" font-size: 12px; border: none; }}")
        self._lbl_count.setStyleSheet(f"color: {theme.hex('muted')};")
        # 重绘已有日志以应用新颜色
        self._edit.clear()
        self._dirty = True
        self._flush()
