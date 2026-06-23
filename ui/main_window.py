"""关节电机上位机主窗口: 组装各面板 + 连接信号

主窗口只负责: 创建 JmClient(注入 SerialTransport)、组装面板、连接信号槽、
管理连接状态与轮询兜底定时器。具体 UI 细节都在各 panel 内。
"""

import time

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QGroupBox, QGridLayout, QPushButton, QMessageBox, QLabel, QSplitter,
    QScrollArea, QLayout, QFrame,
)
from PyQt6.QtCore import Qt, QTimer

import jmproto as jp
from jmproto import JmCmd, cmd_name, err_name
from transport.serial_transport import SerialTransport
from core.motor_client import JmClient

from ui.panels.connection_panel import ConnectionPanel
from ui.panels.control_panel import ControlPanel
from ui.panels.motion_panel import MotionPanel
from ui.panels.param_panel import ParamPanel
from ui.panels.feedback_panel import FeedbackPanel
from ui.panels.telemetry_panel import TelemetryPanel
from ui.panels.plot_panel import PlotPanel
from ui.panels.log_panel import LogPanel


class MainWindow(QMainWindow):
    """关节电机上位机主窗口"""

    def __init__(self):
        super().__init__()

        # 通信客户端(串口传输)
        self._client = JmClient(SerialTransport())
        self._registry = self._client.registry

        # 轮询兜底定时器
        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._on_poll_tick)
        self._poll_period = 100

        # 流量统计刷新定时器(状态栏速率/总量)
        self._stats_timer = QTimer()
        self._stats_timer.timeout.connect(self._on_stats_tick)
        self._stats_period = 500   # ms
        self._last_tx_bytes = 0
        self._last_rx_bytes = 0

        self.setWindowTitle("Joint Motor Controller - 关节电机控制面板")
        self.setMinimumSize(1200, 800)

        self._build_ui()
        self._build_statusbar()
        self._connect_signals()

        self._client.start()
        self._stats_timer.start(self._stats_period)

        # 启动告警(CSV 加载情况)
        for w in self._registry.warnings:
            self._log_panel.log_warn(w)
        self._log_panel.log(
            f"协议表加载: 命令 {len(self._registry.commands)} 条, 参数 {len(self._registry.params)} 个")

    # ==================== UI 组装 ====================
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        # 左侧: 控制类面板。整体放入滚动区, 小窗口或参数较多时不挤压控件。
        left = QScrollArea()
        left.setWidgetResizable(True)
        left.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        left.setMaximumWidth(440)
        left.setMinimumWidth(420)
        left.setFrameShape(QFrame.Shape.NoFrame)

        left_content = QWidget()
        left.setWidget(left_content)
        left_layout = QVBoxLayout(left_content)
        left_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self._conn_panel = ConnectionPanel()
        self._control_panel = ControlPanel()
        self._motion_panel = MotionPanel(self._registry)
        self._telemetry_panel = TelemetryPanel()
        self._feedback_panel = FeedbackPanel()
        self._param_panel = ParamPanel(self._registry)
        self._plot_panel = PlotPanel()
        self._log_panel = LogPanel()
        self._log_panel_visible = True
        self._conn_panel.set_connect_row_tail_widget(self._create_log_toggle_button())

        left_layout.addWidget(self._conn_panel)
        left_layout.addWidget(self._control_panel)
        left_layout.addWidget(self._motion_panel)
        left_layout.addWidget(self._telemetry_panel)
        left_layout.addWidget(self._create_dev_info_group())
        left_layout.addStretch()

        # 右侧: 反馈/参数/绘图 选项卡 + 日志
        right = QWidget()
        right_layout = QVBoxLayout(right)

        tabs = QTabWidget()
        tabs.addTab(self._feedback_panel, "实时反馈")
        tabs.addTab(self._param_panel, "参数读写")
        tabs.addTab(self._plot_panel, "实时曲线")

        self._right_splitter = QSplitter(Qt.Orientation.Vertical)
        self._right_splitter.setChildrenCollapsible(False)
        self._right_splitter.setHandleWidth(8)
        self._right_splitter.setStyleSheet("""
            QSplitter::handle:vertical {
                background: #3A3A3A;
                margin: 2px 0;
            }
            QSplitter::handle:vertical:hover {
                background: #5A8DFF;
            }
        """)
        self._right_splitter.addWidget(tabs)
        self._right_splitter.addWidget(self._log_panel)
        self._right_splitter.setStretchFactor(0, 3)
        self._right_splitter.setStretchFactor(1, 2)
        self._right_splitter.setSizes([480, 300])
        self._log_splitter_sizes = self._right_splitter.sizes()

        right_layout.addWidget(self._right_splitter)

        root.addWidget(left)
        root.addWidget(right, 1)

        self.statusBar().showMessage("未连接")

    def _toggle_log_panel(self):
        visible = not self._log_panel_visible
        if not visible:
            sizes = self._right_splitter.sizes()
            if len(sizes) == 2 and sizes[1] > 0:
                self._log_splitter_sizes = sizes

        self._log_panel_visible = visible
        self._log_panel.setVisible(visible)
        self._btn_toggle_log.setChecked(visible)
        self._update_log_toggle_button()

        if visible:
            self._right_splitter.setSizes(self._log_splitter_sizes or [480, 300])

    def _update_log_toggle_button(self):
        visible = self._log_panel_visible
        tip = "通信日志：已显示，点击隐藏" if visible else "通信日志：已隐藏，点击显示"
        self._btn_toggle_log.setText("日志开" if visible else "日志关")
        self._btn_toggle_log.setToolTip(tip)
        self._btn_toggle_log.setStatusTip("")
        self._btn_toggle_log.setAccessibleName(tip)

    def _create_log_toggle_button(self) -> QPushButton:
        btn = QPushButton(self)
        btn.setCheckable(True)
        btn.setChecked(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedHeight(26)
        btn.setMinimumWidth(64)
        btn.setStyleSheet("""
            QPushButton {
                border: 1px solid #555;
                border-radius: 4px;
                padding: 0 8px;
                background: #2A2A2A;
                color: #DDD;
                font-weight: bold;
            }
            QPushButton:hover {
                background: #3A3A3A;
                border-color: #777;
            }
            QPushButton:checked {
                background: #03A9F4;
                border-color: #29B6F6;
                color: white;
            }
        """)
        btn.clicked.connect(self._toggle_log_panel)
        self._btn_toggle_log = btn
        self._update_log_toggle_button()
        return btn

    def _build_statusbar(self):
        """底部状态栏: 连接状态 + TX/RX 速率与累计总量"""
        sb = self.statusBar()

        def _mk(text):
            lbl = QLabel(text)
            lbl.setStyleSheet("font-family: Consolas, monospace; padding: 0 8px;")
            return lbl

        self._sb_link = _mk("● 未连接")
        self._sb_link.setStyleSheet(
            "font-family: Consolas; padding: 0 8px; color: #999;")
        self._sb_tx = _mk("TX 0 B/s  Σ0 B")
        self._sb_rx = _mk("RX 0 B/s  Σ0 B")
        self._sb_frames = _mk("帧 TX:0 RX:0")

        for w in (self._sb_link, self._sb_tx, self._sb_rx, self._sb_frames):
            sb.addPermanentWidget(w)

    @staticmethod
    def _fmt_bytes(n: int) -> str:
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        return f"{n / (1024 * 1024):.2f} MB"

    def _fmt_rate(self, bps: float) -> str:
        return self._fmt_bytes(int(bps)) + "/s"

    def _on_stats_tick(self):
        tp = self._client.transport
        dt = self._stats_period / 1000.0
        tx_rate = (tp.tx_bytes - self._last_tx_bytes) / dt
        rx_rate = (tp.rx_bytes - self._last_rx_bytes) / dt
        self._last_tx_bytes = tp.tx_bytes
        self._last_rx_bytes = tp.rx_bytes
        self._sb_tx.setText(f"TX {self._fmt_rate(tx_rate)}  Σ{self._fmt_bytes(tp.tx_bytes)}")
        self._sb_rx.setText(f"RX {self._fmt_rate(rx_rate)}  Σ{self._fmt_bytes(tp.rx_bytes)}")
        self._sb_frames.setText(f"帧 TX:{tp.tx_frames} RX:{tp.rx_frames}")

    def _create_dev_info_group(self) -> QGroupBox:
        grp = QGroupBox("设备信息")
        layout = QGridLayout(grp)
        btn_info = QPushButton("读设备信息")
        btn_info.clicked.connect(lambda: self._client.query_dev_info())
        btn_name = QPushButton("读设备名称")
        btn_name.clicked.connect(lambda: self._client.query_dev_name())
        btn_hb = QPushButton("心跳")
        btn_hb.clicked.connect(lambda: self._client.heartbeat())
        layout.addWidget(btn_info, 0, 0)
        layout.addWidget(btn_name, 0, 1)
        layout.addWidget(btn_hb, 0, 2)
        return grp

    # ==================== 信号连接 ====================
    def _connect_signals(self):
        c = self._client
        c.connected.connect(self._on_connected)
        c.error_occurred.connect(self._on_error)
        c.tx_frame.connect(self._log_panel.log_tx)
        c.raw_frame.connect(self._log_panel.log_rx)
        c.feedback_updated.connect(self._on_feedback)
        c.state_updated.connect(self._feedback_panel.update_state)
        c.ack_received.connect(self._on_ack)
        c.nack_received.connect(self._on_nack)
        c.dev_info_received.connect(self._on_dev_info)
        c.dev_name_received.connect(self._on_dev_name)
        c.param_read_result.connect(self._on_param_result)

        # 面板 -> 客户端
        self._conn_panel.connect_requested.connect(self._on_connect)
        self._conn_panel.disconnect_requested.connect(self._on_disconnect)
        self._control_panel.command.connect(self._on_control_command)
        self._motion_panel.send_command.connect(self._on_motion_command)
        self._telemetry_panel.apply_telemetry.connect(self._on_apply_telemetry)
        self._telemetry_panel.poll_toggled.connect(self._on_poll_toggled)
        self._param_panel.read_param.connect(self._on_param_read)
        self._param_panel.write_param.connect(self._on_param_write)
        self._param_panel.save_all.connect(lambda: self._client.param_save())

    # ==================== 连接管理 ====================
    def _on_connect(self, port: str, baud: int):
        if self._client.open(port=port, baudrate=baud):
            self.statusBar().showMessage(f"已连接 {port} @{baud}")
            self._cur_port = port

    def _on_disconnect(self):
        self._poll_timer.stop()
        self._client.close()
        self.statusBar().showMessage("已断开")

    def _on_connected(self, connected: bool):
        self._conn_panel.set_connected(connected)
        if connected:
            self._sb_link.setText(f"● {getattr(self, '_cur_port', '')}")
            self._sb_link.setStyleSheet(
                "font-family: Consolas; padding: 0 8px; color: #2E7D32; font-weight: bold;")
        else:
            self._sb_link.setText("● 未连接")
            self._sb_link.setStyleSheet(
                "font-family: Consolas; padding: 0 8px; color: #999;")
            self._poll_timer.stop()

    def _on_error(self, msg: str):
        self._log_panel.log_err(msg)
        self.statusBar().showMessage(msg)

    # ==================== 命令下发 ====================
    def _ensure_open(self) -> bool:
        if not self._client.is_open():
            QMessageBox.warning(self, "提示", "请先连接串口")
            return False
        return True

    def _on_control_command(self, cmd: int):
        if self._ensure_open():
            self._client.send_command(cmd)

    def _on_motion_command(self, cmd: int, values: dict):
        if not self._ensure_open():
            return
        try:
            self._client.send_command(cmd, values)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"发送异常: {e}")

    def _on_apply_telemetry(self, mask: int, period_ms: int):
        if self._ensure_open():
            self._client.set_telemetry(mask, period_ms)

    def _on_poll_toggled(self, enabled: bool, period: int):
        self._poll_period = period
        if enabled and self._client.is_open():
            self._poll_timer.start(period)
        else:
            self._poll_timer.stop()

    def _on_poll_tick(self):
        if self._client.is_open():
            self._client.query_feedback()
            self._client.query_state()

    # ==================== 参数读写 ====================
    def _on_param_read(self, param_id: int):
        if self._ensure_open():
            self._client.param_read(param_id)

    def _on_param_write(self, param_id: int, text: str):
        if not self._ensure_open():
            return
        try:
            value = self._registry.pack_param_value(param_id, text)
        except ValueError:
            QMessageBox.warning(self, "错误", f"参数值无效: {text}")
            return
        self._client.param_write(param_id, value)

    def _on_param_result(self, param_id: int, ptype: int, value_bytes: bytes):
        val = self._registry.unpack_param_value(param_id, value_bytes)
        disp = str(val) if val is not None else value_bytes.hex(' ')
        self._param_panel.set_value(param_id, disp)
        spec = self._registry.get_param(param_id)
        name = spec.code_name if spec else f"id={param_id}"
        self._log_panel.log(f"[RX] PARAM {name}(id={param_id}) = {disp}")

    # ==================== 数据接收 ====================
    def _on_feedback(self, fb):
        self._feedback_panel.update_feedback(fb)
        self._plot_panel.feed_feedback(time.monotonic(), fb)

    def _on_ack(self, cmd: int):
        self._log_panel.log(f"[RX] ACK {cmd_name(cmd)}(0x{cmd:02X})")

    def _on_nack(self, cmd: int, err: int):
        self._log_panel.log_warn(f"NACK {cmd_name(cmd)}(0x{cmd:02X}) err={err_name(err)}(0x{err:02X})")

    def _on_dev_info(self, hw: int, fw: int, uid: bytes):
        uid_hex = uid.hex(':').upper()
        self._log_panel.log(f"[RX] DEV_INFO HW=0x{hw:08X} FW=0x{fw:08X} UID={uid_hex}")
        QMessageBox.information(
            self, "设备信息",
            f"硬件版本: 0x{hw:08X}\n固件版本: 0x{fw:08X}\nUID: {uid_hex}")

    def _on_dev_name(self, name: str):
        self._log_panel.log(f'[RX] DEV_NAME="{name}"')
        QMessageBox.information(self, "设备名称", f"设备名称: {name}")

    # ==================== 退出 ====================
    def closeEvent(self, event):
        self._poll_timer.stop()
        self._stats_timer.stop()
        self._client.stop()
        event.accept()
