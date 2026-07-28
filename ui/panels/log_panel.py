"""通信日志面板: HTML 着色 + 可开关时间戳/TX/RX/RAW/自动滚动"""

import datetime
import html
import time
from collections import deque

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QWidget, QPushButton,
    QCheckBox, QTextEdit, QSizePolicy, QFileDialog, QMessageBox,
)
from PyQt6.QtGui import QFont, QTextCursor

from jmproto import cmd_name, err_name
from ui.theme import theme


# 配色(深色文本编辑器友好)
_COLOR = {
    'tx':    '#2962FF',   # 蓝 - 发送
    'rx':    '#00897B',   # 青绿 - 接收
    'raw':   '#9E9E9E',   # 灰 - 原始帧
    'warn':  '#F9A825',   # 琥珀 - 警告
    'err':   '#E53935',   # 红 - 错误
    'info':  '#616161',   # 深灰 - 普通信息
    'debug': '#90A4AE',   # 蓝灰 - 调试(细粒度, 默认关闭)
    'ts':    '#9E9E9E',   # 灰 - 时间戳
}

# 日志级别: 应用日志(log/log_debug/log_warn/log_err)按级别过滤
# 通信帧(log_tx/log_rx/log_can_frame)有独立类别开关, 不受级别过滤影响
LOG_LEVEL_DEBUG = 0
LOG_LEVEL_INFO  = 1
LOG_LEVEL_WARN  = 2
LOG_LEVEL_ERROR = 3


class LogPanel(QGroupBox):
    """通信日志(TX/RX/原始帧)。

    对外接口:
        log_tx(cmd, payload)  发送帧
        log_rx(cmd, payload)  接收帧(由 raw_frame 驱动)
        log(msg)              普通信息(INFO)
        log_debug(msg)        调试信息(DEBUG, 默认不显示)
        log_warn(msg) / log_err(msg)
    显示由各开关控制: 时间戳 / 显示TX / 显示RX / 原始HEX / 自动滚动。
    日志级别: DEBUG/INFO/WARN/ERROR 复选框过滤应用日志。
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
        self.chk_can_raw = QCheckBox("CAN原始帧")
        self.chk_can_raw.setChecked(True)
        self.chk_can_raw.setToolTip("显示 CAN 物理帧日志(ID/DLC/data), 仅 CAN 模式有效")
        self.chk_autoscroll = QCheckBox("自动滚动")
        self.chk_autoscroll.setChecked(True)

        for chk in (self.chk_ts, self.chk_tx, self.chk_rx,
                    self.chk_raw, self.chk_can_raw, self.chk_autoscroll):
            tool_layout.addWidget(chk)

        # 日志级别过滤: 控制应用日志(log/log_debug/log_warn/log_err)显示
        # 通信帧(log_tx/log_rx/log_can_frame)有独立类别开关, 不受此过滤影响
        # DEBUG 默认关闭(细粒度日志量大), INFO/WARN/ERROR 默认开启
        tool_layout.addSpacing(8)
        self.chk_lvl_debug = QCheckBox("DEBUG")
        self.chk_lvl_debug.setChecked(False)
        self.chk_lvl_debug.setToolTip("显示调试日志(协议交互细节/状态转换等细粒度信息)")
        self.chk_lvl_info = QCheckBox("INFO")
        self.chk_lvl_info.setChecked(True)
        self.chk_lvl_warn = QCheckBox("WARN")
        self.chk_lvl_warn.setChecked(True)
        self.chk_lvl_error = QCheckBox("ERROR")
        self.chk_lvl_error.setChecked(True)
        for chk in (self.chk_lvl_debug, self.chk_lvl_info,
                    self.chk_lvl_warn, self.chk_lvl_error):
            tool_layout.addWidget(chk)

        self._btn_clear = QPushButton("清空日志")
        self._btn_clear.clicked.connect(self._log.clear)
        tool_layout.addSpacing(8)
        tool_layout.addWidget(self._btn_clear)

        self._btn_export = QPushButton("导出日志")
        self._btn_export.clicked.connect(self._export_log)
        tool_layout.addWidget(self._btn_export)

        self._footer_fill = QWidget()
        self._footer_fill.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tool_layout.addWidget(self._footer_fill, 1)

        layout.addWidget(tool_row)
        self.apply_theme()

    def apply_theme(self):
        self._log.setStyleSheet(
            f"QTextEdit {{ background-color:{theme.hex('log_bg')}; color:{theme.hex('log_text')}; "
            f"border:1px solid {theme.hex('border')}; }}")

    # ---------------- 配置 ----------------
    def get_opts(self) -> dict:
        """显示开关状态(供设置持久化)。"""
        return {
            "ts": self.chk_ts.isChecked(),
            "tx": self.chk_tx.isChecked(),
            "rx": self.chk_rx.isChecked(),
            "raw": self.chk_raw.isChecked(),
            "can_raw": self.chk_can_raw.isChecked(),
            "autoscroll": self.chk_autoscroll.isChecked(),
            # 日志级别开关
            "lvl_debug": self.chk_lvl_debug.isChecked(),
            "lvl_info": self.chk_lvl_info.isChecked(),
            "lvl_warn": self.chk_lvl_warn.isChecked(),
            "lvl_error": self.chk_lvl_error.isChecked(),
        }

    def set_opts(self, opts: dict):
        """套用显示开关(启动恢复设置用); 缺项保持当前。"""
        if not isinstance(opts, dict):
            return
        mapping = {"ts": self.chk_ts, "tx": self.chk_tx, "rx": self.chk_rx,
                   "raw": self.chk_raw, "can_raw": self.chk_can_raw,
                   "autoscroll": self.chk_autoscroll,
                   # 日志级别开关
                   "lvl_debug": self.chk_lvl_debug, "lvl_info": self.chk_lvl_info,
                   "lvl_warn": self.chk_lvl_warn, "lvl_error": self.chk_lvl_error}
        for key, chk in mapping.items():
            if key in opts:
                chk.setChecked(bool(opts[key]))

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
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")
        self._pending.append((kind, body_html, ts))

    def _append_html(self, kind: str, body_html: str, ts: str = None):
        """拼时间戳 + 着色正文, 追加一行并按需滚动。"""
        parts = []
        if self.chk_ts.isChecked():
            if ts is None:
                ts = datetime.datetime.now().strftime("%H:%M:%S.%f")
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

    def _export_log(self):
        """导出当前日志内容为 txt 或 html。"""
        self.flush()

        default_name = f"jm_log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        path, filt = QFileDialog.getSaveFileName(
            self,
            "导出日志",
            default_name,
            "Text Files (*.txt);;HTML Files (*.html)",
        )
        if not path:
            return

        try:
            if filt.startswith("HTML"):
                if not path.lower().endswith(".html"):
                    path += ".html"
                content = self._log.document().toHtml()
            else:
                if not path.lower().endswith(".txt"):
                    path += ".txt"
                content = self._log.toPlainText()

            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"日志导出失败:\n{e}")
        else:
            QMessageBox.information(self, "导出完成", f"日志已导出到:\n{path}")

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

    def log_can_frame(self, can_id: int, data: bytes, is_rx: bool, ts: float):
        """CAN 物理帧日志。ts 为 python-can 提供的硬件时间戳(相对值, 当前用系统时间显示)。"""
        if not self.chk_can_raw.isChecked():
            return
        arrow = "▼ RX" if is_rx else "▲ TX"
        motor_id = can_id & 0xFF
        cmd = (can_id >> 8) & 0xFF
        hex_str = data.hex(' ').upper() if data else "(空)"
        line = (f"{arrow} CAN ID:0x{can_id:X} DLC:{len(data)} "
                f"CMD:0x{cmd:02X} MID:{motor_id} "
                f"{html.escape(hex_str)}")
        self._queue('raw', line)

    # ---------------- 日志级别过滤 ----------------
    def _level_enabled(self, level: int) -> bool:
        """检查指定级别是否启用显示。"""
        if level == LOG_LEVEL_DEBUG:
            return self.chk_lvl_debug.isChecked()
        if level == LOG_LEVEL_INFO:
            return self.chk_lvl_info.isChecked()
        if level == LOG_LEVEL_WARN:
            return self.chk_lvl_warn.isChecked()
        if level == LOG_LEVEL_ERROR:
            return self.chk_lvl_error.isChecked()
        return True

    def log(self, msg: str):
        """普通信息日志(INFO 级别)。"""
        if not self._level_enabled(LOG_LEVEL_INFO):
            return
        self._queue('info', "[I] " + html.escape(msg))

    def log_debug(self, msg: str):
        """调试日志(DEBUG 级别, 默认不显示)。

        用于协议交互细节、状态转换、重连过程等细粒度信息。
        开启 DEBUG 复选框后显示, 便于联调排查问题。
        """
        if not self._level_enabled(LOG_LEVEL_DEBUG):
            return
        self._queue('debug', "[D] " + html.escape(msg))

    def log_warn(self, msg: str):
        """警告日志(WARN 级别)。"""
        if not self._level_enabled(LOG_LEVEL_WARN):
            return
        self._queue('warn', "[W] ⚠ " + html.escape(msg))

    def log_err(self, msg: str):
        """错误日志(ERROR 级别)。"""
        if not self._level_enabled(LOG_LEVEL_ERROR):
            return
        self._queue('err', "[E] ✖ " + html.escape(msg))
