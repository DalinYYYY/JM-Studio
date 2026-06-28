"""
主窗口 + 连接面板 + 状态栏。

布局:
┌─────────────────────────────────────────────┐
│ 工具栏: 连接/断开  遥测订阅  ENABLE/DISABLE  │
├──────────────┬──────────────────────────────┤
│              │                              │
│  连接配置    │       波形显示               │
│  + 通道选择  │                              │
│  + 状态显示  │                              │
│              │                              │
├──────────────┴──────────────────────────────┤
│ 状态栏: 连接状态 / FPS / cursor 读数        │
└─────────────────────────────────────────────┘
"""
from __future__ import annotations

import time
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
    QSpinBox, QSplitter, QStatusBar, QVBoxLayout, QWidget,
)

from .protocol_reference import (
    JmCmd, JmTlmBit, JmErr, JMERR_CN,
    parse_telemetry, parse_state, parse_read_reply, FeedbackData,
    wr_u8, wr_u16,
)
from .transport import (
    Transport, SerialTransport, VirtualTransport, CanTransport,
    create_transport,
)
from .waveform_plot import WaveformPlot, CHANNEL_METAS, CHANNELS_BY_KEY


# ============================================================================
# 连接面板
# ============================================================================

class ConnectionPanel(QWidget):
    """左侧: 连接配置 + 通道选择 + 状态"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # ---- 连接类型 ----
        gb_conn = QGroupBox("连接")
        form_conn = QFormLayout()
        self._cmb_kind = QComboBox()
        self._cmb_kind.addItem("虚拟电机 (本机回环)", 'virtual')
        self._cmb_kind.addItem("串口 (RS232/UART)", 'serial')
        self._cmb_kind.addItem("CAN 总线", 'can')
        form_conn.addRow("类型:", self._cmb_kind)

        # 串口参数
        self._cmb_port = QComboBox()
        self._cmb_port.setEditable(True)
        self._btn_refresh = QPushButton("刷新")
        h_port = QHBoxLayout()
        h_port.addWidget(self._cmb_port, 1)
        h_port.addWidget(self._btn_refresh)
        form_conn.addRow("端口:", h_port)

        self._cmb_baud = QComboBox()
        for b in [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]:
            self._cmb_baud.addItem(str(b), b)
        self._cmb_baud.setCurrentIndex(4)
        form_conn.addRow("波特率:", self._cmb_baud)

        # CAN 参数
        self._txt_can_ch = QComboBox()
        self._txt_can_ch.setEditable(True)
        self._txt_can_ch.addItem("vcan0")
        self._txt_can_ch.addItem("can0")
        form_conn.addRow("CAN通道:", self._txt_can_ch)
        self._spin_motor_id = QSpinBox()
        self._spin_motor_id.setRange(0, 127)
        self._spin_motor_id.setValue(1)
        form_conn.addRow("电机ID:", self._spin_motor_id)

        self._btn_connect = QPushButton("连接")
        self._btn_connect.setStyleSheet("background: #4CAF50; color: white; font-weight: bold; padding: 6px;")
        form_conn.addRow(self._btn_connect)

        gb_conn.setLayout(form_conn)
        layout.addWidget(gb_conn)

        # ---- 遥测订阅 ----
        gb_tlm = QGroupBox("遥测订阅")
        v_tlm = QVBoxLayout()

        h_tlm = QHBoxLayout()
        h_tlm.addWidget(QLabel("周期:"))
        self._spin_period = QSpinBox()
        self._spin_period.setRange(1, 1000)
        self._spin_period.setValue(10)
        self._spin_period.setSuffix(" ms")
        h_tlm.addWidget(self._spin_period)
        self._btn_subscribe = QPushButton("订阅")
        self._btn_subscribe.setEnabled(False)
        h_tlm.addWidget(self._btn_subscribe)
        v_tlm.addLayout(h_tlm)

        # 通道勾选 (按 JmTlmBit 分组)
        self._chk_groups: dict = {}
        grid_tlm = QFormLayout()
        for mask_val, group_label, field_names, _ in JmTlmBit.ITEMS:
            if mask_val == JmTlmBit.DEBUG:
                # 调试通道变长, 单独勾选
                chk = QCheckBox(f"{group_label} (调试通道)")
                chk.setProperty('mask', mask_val)
                chk.setProperty('fields', field_names)
                self._chk_groups[mask_val] = chk
                grid_tlm.addRow(chk)
                continue
            chk = QCheckBox(f"{group_label} ({', '.join(field_names)})")
            chk.setProperty('mask', mask_val)
            chk.setProperty('fields', field_names)
            self._chk_groups[mask_val] = chk
            grid_tlm.addRow(chk)
        v_tlm.addLayout(grid_tlm)

        # 默认勾选 POS_VEL + TORQUE
        self._chk_groups[JmTlmBit.POS_VEL].setChecked(True)
        self._chk_groups[JmTlmBit.TORQUE].setChecked(True)

        gb_tlm.setLayout(v_tlm)
        layout.addWidget(gb_tlm)

        # ---- 运动控制 (基础) ----
        gb_motion = QGroupBox("运动控制")
        v_motion = QVBoxLayout()
        h_motion = QHBoxLayout()
        self._btn_enable = QPushButton("ENABLE")
        self._btn_disable = QPushButton("DISABLE")
        self._btn_enable.setEnabled(False)
        self._btn_disable.setEnabled(False)
        h_motion.addWidget(self._btn_enable)
        h_motion.addWidget(self._btn_disable)
        v_motion.addLayout(h_motion)
        gb_motion.setLayout(v_motion)
        layout.addWidget(gb_motion)

        # ---- 状态显示 ----
        gb_state = QGroupBox("设备状态")
        form_state = QFormLayout()
        self._lbl_top_fsm = QLabel("-")
        self._lbl_run_state = QLabel("-")
        self._lbl_enable = QLabel("-")
        self._lbl_fault = QLabel("-")
        form_state.addRow("顶层状态:", self._lbl_top_fsm)
        form_state.addRow("运行子状态:", self._lbl_run_state)
        form_state.addRow("使能:", self._lbl_enable)
        form_state.addRow("故障码:", self._lbl_fault)
        gb_state.setLayout(form_state)
        layout.addWidget(gb_state)

        layout.addStretch()

        # ---- 统计 ----
        self._lbl_stats = QLabel("TX: 0 B / 0 帧\nRX: 0 B / 0 帧")
        self._lbl_stats.setStyleSheet("color: gray; font-family: monospace;")
        layout.addWidget(self._lbl_stats)

    # ---- 公共 API ----
    def get_kind(self) -> str:
        return self._cmb_kind.currentData()

    def get_serial_config(self) -> dict:
        return {
            'port': self._cmb_port.currentText(),
            'baudrate': self._cmb_baud.currentData(),
        }

    def get_can_config(self) -> dict:
        return {
            'channel': self._txt_can_ch.currentText(),
            'bitrate': 1000000,
            'motor_id': self._spin_motor_id.value(),
        }

    def get_tlm_config(self) -> dict:
        mask = 0
        for m, chk in self._chk_groups.items():
            if chk.isChecked():
                mask |= m
        return {
            'enable': 1,
            'mask': mask,
            'period_ms': self._spin_period.value(),
        }

    def set_connected(self, connected: bool):
        self._btn_connect.setText("断开" if connected else "连接")
        self._btn_connect.setStyleSheet(
            "background: #F44336; color: white; font-weight: bold; padding: 6px;"
            if connected else
            "background: #4CAF50; color: white; font-weight: bold; padding: 6px;"
        )
        self._btn_subscribe.setEnabled(connected)
        self._btn_enable.setEnabled(connected)
        self._btn_disable.setEnabled(connected)

    def update_state(self, top_fsm: int, run_state: int, enable: int, fault: int):
        from .protocol_reference import top_fsm_name, run_state_name
        self._lbl_top_fsm.setText(top_fsm_name(top_fsm))
        self._lbl_run_state.setText(run_state_name(run_state))
        self._lbl_enable.setText("使能" if enable else "未使能")
        if fault:
            self._lbl_fault.setText(f"0x{fault:04X}")
            self._lbl_fault.setStyleSheet("color: red; font-weight: bold;")
        else:
            self._lbl_fault.setText("无")
            self._lbl_fault.setStyleSheet("color: green;")

    def update_stats(self, tx_bytes: int, tx_frames: int, rx_bytes: int, rx_frames: int):
        self._lbl_stats.setText(
            f"TX: {tx_bytes} B / {tx_frames} 帧\nRX: {rx_bytes} B / {rx_frames} 帧"
        )


# ============================================================================
# 主窗口
# ============================================================================

class MainWindow(QMainWindow):
    """波形显示上位机主窗口"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("关节电机波形显示上位机")
        self.resize(1400, 850)

        self._transport: Optional[Transport] = None
        self._last_top_fsm = 0
        self._last_run_state = 0
        self._last_enable = 0
        self._last_fault = 0

        self._setup_ui()
        self._setup_connections()
        self._setup_timer()

        # 默认在第一个窗口显示 位置/速度/力矩
        panel = self._waveform.get_active_panel()
        if panel is not None:
            for k in ['pos', 'vel', 'torque']:
                panel.set_channel_visible(k, True)

    def _setup_ui(self):
        # 中央 splitter: 左连接面板, 右波形
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._conn = ConnectionPanel()
        splitter.addWidget(self._conn)

        self._waveform = WaveformPlot()
        splitter.addWidget(self._waveform)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 1080])

        self.setCentralWidget(splitter)

        # 状态栏
        sb = QStatusBar()
        self._sb_conn = QLabel("未连接")
        self._sb_conn.setStyleSheet("color: red; font-weight: bold;")
        self._sb_cursor = QLabel("")
        self._sb_cursor.setMinimumWidth(400)
        self._sb_cursor.setStyleSheet("color: #0066CC; font-family: monospace;")
        sb.addWidget(self._sb_conn)
        sb.addPermanentWidget(self._sb_cursor)
        self.setStatusBar(sb)

    def _setup_connections(self):
        self._conn._btn_connect.clicked.connect(self._on_connect)
        self._conn._btn_refresh.clicked.connect(self._refresh_ports)
        self._conn._cmb_kind.currentIndexChanged.connect(self._on_kind_changed)
        self._conn._btn_subscribe.clicked.connect(self._on_subscribe)
        self._conn._btn_enable.clicked.connect(self._on_enable)
        self._conn._btn_disable.clicked.connect(self._on_disable)

        # 通道勾选 -> 波形显隐
        for m, chk in self._conn._chk_groups.items():
            chk.stateChanged.connect(self._on_channel_group_toggled)

        # 波形 cursor 信号
        self._waveform.cursor_moved.connect(self._on_cursor_moved)

        self._refresh_ports()
        self._on_kind_changed()

    def _setup_timer(self):
        # 定期更新统计 + 状态查询
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(500)

    # ---------------- 连接管理 ----------------
    def _refresh_ports(self):
        self._conn._cmb_port.clear()
        for port, desc in SerialTransport.list_ports():
            self._conn._cmb_port.addItem(f"{port} - {desc}", port)
        if self._conn._cmb_port.count() == 0:
            self._conn._cmb_port.addItem("COM1")

    def _on_kind_changed(self):
        kind = self._conn.get_kind()
        is_serial = (kind == 'serial')
        is_can = (kind == 'can')
        # 串口参数
        self._conn._cmb_port.setEnabled(is_serial)
        self._conn._cmb_baud.setEnabled(is_serial)
        self._conn._btn_refresh.setEnabled(is_serial)
        # CAN 参数
        self._conn._txt_can_ch.setEnabled(is_can)
        self._conn._spin_motor_id.setEnabled(is_can)

    def _on_connect(self):
        if self._transport is not None and self._transport.is_open():
            self._do_disconnect()
        else:
            self._do_connect()

    def _do_connect(self):
        kind = self._conn.get_kind()
        self._transport = create_transport(kind)
        if self._transport is None:
            QMessageBox.warning(self, "错误", f"不支持的连接类型: {kind}")
            return

        # 信号
        self._transport.frame_received.connect(self._on_frame_received)
        self._transport.connected.connect(self._on_connected)
        self._transport.error_occurred.connect(self._on_error)

        # 配置
        if kind == 'serial':
            cfg = self._conn.get_serial_config()
            if not cfg['port'] or '-' in cfg['port']:
                # 取 " - " 之前的部分
                cfg['port'] = cfg['port'].split(' - ')[0].strip()
        elif kind == 'can':
            cfg = self._conn.get_can_config()
        else:
            cfg = {}

        # 启动后台线程 + 打开
        self._transport.start()
        if not self._transport.open(**cfg):
            self._transport.stop()
            self._transport = None
            return

    def _do_disconnect(self):
        if self._transport is not None:
            # 先停遥测
            try:
                self._transport.send(JmCmd.SET_TELEMETRY, wr_u8(0) + wr_u16(0) + wr_u16(0))
            except Exception:
                pass
            self._transport.close()
            self._transport.stop()
            self._transport = None
        self._conn.set_connected(False)
        self._sb_conn.setText("未连接")
        self._sb_conn.setStyleSheet("color: red; font-weight: bold;")

    def _on_connected(self, ok: bool):
        self._conn.set_connected(ok)
        if ok:
            self._sb_conn.setText(f"已连接 ({self._transport.name})")
            self._sb_conn.setStyleSheet("color: green; font-weight: bold;")
            # 自动订阅
            self._on_subscribe()
        else:
            self._sb_conn.setText("未连接")
            self._sb_conn.setStyleSheet("color: red; font-weight: bold;")

    def _on_error(self, msg: str):
        self.statusBar().showMessage(msg, 5000)

    # ---------------- 帧接收 ----------------
    def _on_frame_received(self, cmd: int, payload: bytes):
        try:
            if cmd == JmCmd.TELEMETRY:
                fb, filled = parse_telemetry(payload)
                fb.rx_ts = time.monotonic()
                self._waveform.append_feedback(fb, filled)
                # 状态字段更新
                if 'top_fsm' in filled:
                    self._last_top_fsm = fb.top_fsm
                    self._last_run_state = fb.run_state
                    self._last_enable = fb.enable
                if 'fault_mask' in filled:
                    self._last_fault = fb.fault_mask
                self._conn.update_state(self._last_top_fsm, self._last_run_state,
                                        self._last_enable, self._last_fault)
            elif cmd == JmCmd.READ_STATE:
                top, run, _, en = parse_state(payload)
                self._last_top_fsm = top
                self._last_run_state = run
                self._last_enable = en
                self._conn.update_state(top, run, en, self._last_fault)
            elif cmd == JmCmd.READ_FEEDBACK:
                fb = FeedbackData.from_feedback_payload(payload)
                self._waveform.append_feedback(fb, fb.filled_fields)
                self._last_fault = fb.fault_mask
                self._conn.update_state(self._last_top_fsm, self._last_run_state,
                                        self._last_enable, fb.fault_mask)
            elif cmd == JmCmd.READ_FAULT:
                fb, filled = parse_read_reply(cmd, payload)
                if fb is not None:
                    self._last_fault = fb.fault_mask
                    self._conn.update_state(self._last_top_fsm, self._last_run_state,
                                            self._last_enable, fb.fault_mask)
            elif cmd == JmCmd.NACK:
                if len(payload) >= 2:
                    nack_cmd = payload[0]
                    err = payload[1]
                    self.statusBar().showMessage(
                        f"NACK cmd=0x{nack_cmd:02X} err={JMERR_CN.get(JmErr(err), '?')}",
                        3000
                    )
            elif cmd <= JmCmd.SINGLE_STEP or cmd == JmCmd.SET_TELEMETRY:
                # ACK 类, 忽略 (状态码 0=OK)
                pass
        except Exception as e:
            self.statusBar().showMessage(f"解析错误: {e}", 3000)

    # ---------------- 控制命令 ----------------
    def _on_subscribe(self):
        if self._transport is None or not self._transport.is_open():
            return
        cfg = self._conn.get_tlm_config()
        payload = wr_u8(cfg['enable']) + wr_u16(cfg['mask']) + wr_u16(cfg['period_ms'])
        self._transport.send(JmCmd.SET_TELEMETRY, payload)

    def _on_enable(self):
        if self._transport is not None:
            self._transport.send(JmCmd.ENABLE)

    def _on_disable(self):
        if self._transport is not None:
            self._transport.send(JmCmd.DISABLE)

    # ---------------- 通道勾选 ----------------
    def _on_channel_group_toggled(self):
        # 检查勾选状态, 联动到第一个面板的波形显隐
        panel = self._waveform.get_active_panel()
        if panel is None:
            return
        for m, chk in self._conn._chk_groups.items():
            checked = chk.isChecked()
            fields = chk.property('fields') or []
            for f in fields:
                if f == 'debug':
                    continue
                panel.set_channel_visible(f, checked)

    # ---------------- cursor ----------------
    def _on_cursor_moved(self, x: float, values: dict):
        parts = [f"t={x:.3f}s"]
        for key, v in values.items():
            meta = CHANNELS_BY_KEY.get(key)
            if meta:
                parts.append(f"{meta.label}={v:.3f}{meta.unit}")
        self._sb_cursor.setText("  |  ".join(parts))

    # ---------------- 定时 ----------------
    def _on_tick(self):
        if self._transport is not None:
            self._conn.update_stats(
                self._transport.tx_bytes, self._transport.tx_frames,
                self._transport.rx_bytes, self._transport.rx_frames
            )
            # 没有遥测时主动查询状态
            if self._transport.is_open():
                # 检查是否订阅了遥测, 若否则周期查询
                pass

    # ---------------- 关闭 ----------------
    def closeEvent(self, event):
        self._do_disconnect()
        super().closeEvent(event)
