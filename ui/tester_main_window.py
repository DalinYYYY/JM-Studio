"""测试员版主窗口: 复用现有面板的精简形态(app_profiles.json 驱动)。

与开发版 MainWindow 的差异:
- 仅组装 连接/系统控制/运动控制 左栏 + 实时反馈/实时曲线 双 Tab;
- 不使用下位机遥测(0xCA 主动上报), 所有数据由 _PollScheduler 主动轮询拉取
  (0xC0 反馈 + 0xC4 DQ 电流 + 0xC1 状态 + 慢速总线/温度), 轮询组由 profile 配置;
- 运动模式下拉按 profile.motion_modes 子集白名单过滤(优先于 motion_modes.txt);
- 用户设置存 ui_layout.json 的 tester_settings 节, 与开发版 settings 节互不干扰。

新增形态: resources/app_profiles.json 加一节 profile 即可, 本文件无需改动。
"""

import time
from collections import deque
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QSplitter,
    QScrollArea, QLayout, QFrame, QLabel, QMessageBox,
)
from PyQt6.QtCore import Qt, QTimer

from jmproto import JmCmd, cmd_name, err_name_cn
from jmproto import codec
from transport.serial_transport import SerialTransport
from core.motor_client import JmClient, DANGER_CMDS

from ui.panels.connection_panel import ConnectionPanel
from ui.panels.control_panel import ControlPanel
from ui.panels.motion_panel import MotionPanel
from ui.panels.feedback_panel import FeedbackPanel
from ui.panels.plot_panel import PlotPanel
from ui.theme import theme
from ui import layout_store
from ui import APP_VERSION

# 轮询命令 → JmClient 查询方法名
_POLL_METHODS = {
    JmCmd.READ_FEEDBACK: 'query_feedback',
    JmCmd.READ_POS_VEL: 'query_pos_vel',
    JmCmd.READ_BUS: 'query_bus',
    JmCmd.READ_TEMPERATURE: 'query_temperature',
    JmCmd.READ_DQ_CURRENT: 'query_dq_current',
    JmCmd.READ_PHASE_CURRENT: 'query_phase_current',
    JmCmd.READ_MULTITURN: 'query_multiturn',
    JmCmd.READ_STATE: 'query_state',
    JmCmd.READ_FAULT: 'query_fault',
}


class _PollScheduler:
    """统一主动轮询调度: 各命令独立周期, 单定时器分时触发。

    各条目起始时刻按序号错开, 避免同拍突发多帧; 落后超过一个周期时重同步,
    避免断链后补发堆积。仅链路打开时发送。
    """

    _TICK_MS = 10   # 基础节拍(各周期均为其整数倍时无相位漂移)

    def __init__(self, client: JmClient, group: list, parent=None):
        self._client = client
        self._entries = []
        for item in group:
            method = _POLL_METHODS.get(item['cmd'])
            if method is None:
                continue   # 不支持的查询命令直接剔除
            self._entries.append({
                'method': method,
                'period': item['period_ms'] / 1000.0,
                'next': 0.0,
                'label': cmd_name(item['cmd']),
            })
        self._timer = QTimer(parent)
        self._timer.setInterval(self._TICK_MS)
        self._timer.timeout.connect(self._on_tick)

    def start(self):
        if not self._entries:
            return
        now = time.monotonic()
        for i, e in enumerate(self._entries):
            e['next'] = now + self._TICK_MS / 1000.0 * (i + 1)   # 错峰启动
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def describe(self) -> str:
        """轮询组描述(状态栏提示用)。"""
        return " · ".join(f"{e['label']} {int(e['period'] * 1000)}ms"
                          for e in self._entries)

    def _on_tick(self):
        if not self._client.is_open():
            return
        now = time.monotonic()
        for e in self._entries:
            if now < e['next']:
                continue
            try:
                getattr(self._client, e['method'])()
            except Exception:
                pass   # 单条发送失败不影响其余轮询
            e['next'] = (e['next'] + e['period']
                         if now - e['next'] < e['period']
                         else now + e['period'])


class TesterMainWindow(QMainWindow):
    """测试员版主窗口(profile 配置驱动, 面板全部复用开发版组件)。"""

    _MAX_FEEDBACK_BATCH_PER_TICK = 1000
    _SETTINGS_KEY = "tester_settings"

    # 连接后要读的电机本体参数(转子示意图 + 电角度计算用):
    # param_id → feedback_panel.set_motor_params 键; 槽数由极对数按槽极配合推导
    _MOTOR_PARAM_KEYS = {
        17: 'pole_pairs',        # 极对数
        20: 'r',                 # 相电阻
        21: 'ld',                # d 轴电感
        22: 'lq',                # q 轴电感
        23: 'flux',              # 磁链
        24: 'kt',                # 转矩常数
        38: 'elec_angle_bias',   # 电角度偏置(电角度显示)
    }

    def __init__(self, profile: dict):
        super().__init__()
        self._profile = profile
        self._link_connected = False
        self._motor_params = {}   # 连接后读回的电机本体参数

        logo = Path(__file__).resolve().parent.parent / "resources" / "pic" / "log_ioc.png"
        if logo.exists():
            self.setWindowIcon(QIcon(str(logo)))

        self._client = JmClient(SerialTransport())
        self._client.set_confirm_callback(self._on_confirm_danger_cmd)
        self._registry = self._client.registry

        # 反馈缓冲(高频帧进 UI 前有界缓存, 显示按固定频率刷新)
        self._feedback_pending = deque()
        self._feedback_dropped = 0
        self._latest_state = None

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(50)
        self._ui_timer.timeout.connect(self._on_ui_tick)

        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(500)
        self._stats_timer.timeout.connect(self._on_stats_tick)
        self._last_tx_bytes = 0
        self._last_rx_bytes = 0

        self.setWindowTitle(f"{profile['title']} v{APP_VERSION}")
        w, h = profile['window_size']
        self.setMinimumSize(960, 640)
        self.resize(w, h)

        self._build_ui()
        self._build_statusbar()
        self._connect_signals()

        theme.changed.connect(self._on_theme_changed)

        self._poller = _PollScheduler(self._client, profile['poll_group'], self)

        self._feedback_panel.load_layout()
        self._apply_settings(layout_store.get_meta(self._SETTINGS_KEY, {}))

        self._client.start()
        self._ui_timer.start()
        self._stats_timer.start()
        self.statusBar().showMessage("未连接")

    # ==================== UI 组装 ====================
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        # 左栏: 连接 / 系统控制 / 运动控制(滚动区, 小窗口不挤压)
        self._left_scroll = QScrollArea()
        self._left_scroll.setWidgetResizable(True)
        self._left_scroll.setMinimumWidth(230)
        self._left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_content = QWidget()
        self._left_scroll.setWidget(left_content)
        left_layout = QVBoxLayout(left_content)
        left_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self._conn_panel = ConnectionPanel()
        self._control_panel = ControlPanel()
        self._motion_panel = MotionPanel(
            self._registry, mode_subset=self._profile.get('motion_modes'))

        left_map = {
            'connection': self._conn_panel,
            'control': self._control_panel,
            'motion': self._motion_panel,
        }
        for name in self._profile['left_panels']:
            left_layout.addWidget(left_map[name])
        left_layout.addStretch()

        # 右栏: 实时反馈 / 实时曲线 Tab
        self._feedback_panel = FeedbackPanel()
        self._feedback_panel.set_poll_controls_visible(False)
        self._feedback_panel.set_top_diagram(self._profile.get('feedback_diagram', 'foc'))
        self._plot_panel = PlotPanel()
        self._plot_panel.set_debug_channel_visible(False)

        tabs = QTabWidget()
        tab_map = {
            'feedback_chart': (self._feedback_panel, "实时反馈"),
            'waveform_plot': (self._plot_panel, "实时曲线"),
        }
        for name in self._profile['tabs']:
            panel, label = tab_map[name]
            tabs.addTab(panel, label)
        self._tabs = tabs

        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.setHandleWidth(8)
        self._main_splitter.addWidget(self._left_scroll)
        self._main_splitter.addWidget(tabs)
        self._main_splitter.setStretchFactor(0, 0)
        self._main_splitter.setStretchFactor(1, 1)
        self._main_splitter.setSizes([300, 880])
        root.addWidget(self._main_splitter)

    def _build_statusbar(self):
        sb = self.statusBar()

        def _mk(text):
            lbl = QLabel(text)
            lbl.setStyleSheet("font-family: Consolas, monospace; padding: 0 8px;")
            return lbl

        self._sb_link = _mk("● 未连接")
        self._sb_link.setStyleSheet(
            "font-family: Consolas; padding: 0 8px; color: #999;")
        self._sb_fw_ver = _mk("")
        self._sb_fw_ver.setStyleSheet(
            "font-family: Consolas; padding: 0 8px; color: #999;")
        self._sb_mode = _mk("模式: -")
        self._sb_tx = _mk("TX 0 B/s  Σ0 B")
        self._sb_rx = _mk("RX 0 B/s  Σ0 B")
        self._sb_poll = _mk("")
        for w in (self._sb_link, self._sb_fw_ver, self._sb_mode, self._sb_tx,
                  self._sb_rx, self._sb_poll):
            sb.addPermanentWidget(w)

    # ==================== 信号连接 ====================
    def _connect_signals(self):
        c = self._client
        c.connected.connect(self._on_connected)
        c.error_occurred.connect(self._on_error)
        c.feedback_updated.connect(self._queue_feedback)
        c.state_updated.connect(self._queue_state)
        c.dev_info_received.connect(self._on_dev_info)
        c.motor_info_read_result.connect(self._on_motor_info_value)
        c.nack_received.connect(self._on_nack)

        self._conn_panel.connect_requested.connect(self._on_connect)
        self._conn_panel.connect_can_requested.connect(self._on_connect_can)
        self._conn_panel.disconnect_requested.connect(self._on_disconnect)
        self._conn_panel.scan_can_requested.connect(
            lambda: self.statusBar().showMessage(
                "CAN 设备管理请使用开发版(JM Studio)", 4000))
        self._conn_panel.set_can_id_requested.connect(
            lambda: self.statusBar().showMessage(
                "CAN 设备管理请使用开发版(JM Studio)", 4000))

        self._control_panel.command.connect(self._on_control_command)
        self._motion_panel.send_command.connect(self._on_motion_command)

    # ==================== 连接管理 ====================
    def _on_connect(self, port: str, baud: int):
        if port == "VIRTUAL":
            self.statusBar().showMessage("测试版不支持虚拟数据引擎, 请选择真实串口", 4000)
            return
        if self._client.transport.name != "serial":
            self._client.set_transport(SerialTransport())
        if self._client.open(port=port, baudrate=baud):
            self.statusBar().showMessage(f"已连接 {port} @{baud}")

    def _on_connect_can(self, channel: str, bitrate: int, motor_id: int):
        try:
            from transport.can_transport import CanTransport
        except ImportError:
            QMessageBox.warning(self, "提示", "python-can 未安装, 无法使用 CAN 传输")
            return
        if self._client.transport.name != "can":
            self._client.set_transport(CanTransport())
        if self._client.open(channel=channel, bitrate=bitrate, motor_id=motor_id):
            self.statusBar().showMessage(f"已连接 {channel} @{bitrate} ID={motor_id}")

    def _on_disconnect(self):
        if self._client.is_open():
            self._client.close()
        self.statusBar().showMessage("已断开")

    def _on_connected(self, connected: bool):
        self._link_connected = connected
        self._conn_panel.set_connected(connected)
        self._feedback_panel.set_link_active(connected)
        is_can = (self._client.transport.name == "can")
        self._feedback_panel.set_can_mode(connected and is_can)
        if connected:
            self._poller.start()
            self._sb_poll.setText(f"轮询: {self._poller.describe()}")
            self.statusBar().showMessage("已连接", 3000)
            try:
                self._client.query_state()      # 立即拉一次状态(使能态高亮)
                self._client.query_dev_info()   # 连接后请求软件版本(显示在连接面板)
            except Exception:
                pass
            # 延迟读取电机本体参数(转子示意图极对数/槽数 + 电角度偏置),
            # 避开连接瞬间的状态/版本请求
            QTimer.singleShot(150, self._read_motor_params)
        else:
            self._poller.stop()
            self._feedback_pending.clear()
            self._latest_state = None
            self._motor_params = {}
            self._feedback_panel.set_motor_params({})   # 恢复默认极对数
            self._sb_poll.setText("")
            self._sb_mode.setText("模式: -")
            self._sb_fw_ver.setText("")
            self._sb_fw_ver.setToolTip("")
        self._refresh_link_label()

    def _refresh_link_label(self):
        if self._link_connected:
            tp_name = self._client.transport.name
            prefix = "[CAN] " if tp_name == "can" else "[UART] "
            self._sb_link.setText(f"● {prefix}已连接")
            self._sb_link.setStyleSheet(
                f"font-family: Consolas; padding: 0 8px; "
                f"color: {theme.hex('ok')}; font-weight: bold;")
            self._sb_fw_ver.setStyleSheet(
                f"font-family: Consolas; padding: 0 8px; color: {theme.hex('ok')};")
        else:
            self._sb_link.setText("● 未连接")
            self._sb_link.setStyleSheet(
                f"font-family: Consolas; padding: 0 8px; color: {theme.hex('muted')};")
            self._sb_fw_ver.setStyleSheet(
                f"font-family: Consolas; padding: 0 8px; color: {theme.hex('muted')};")

    def _on_error(self, msg: str):
        self.statusBar().showMessage(msg, 5000)

    def _on_nack(self, cmd: int, err: int):
        self.statusBar().showMessage(
            f"命令被拒绝: {cmd_name(cmd)} err={err_name_cn(err)}", 5000)

    @staticmethod
    def _format_version(value: int) -> str:
        """协议 u32 版本号 → major.minor.patch.build(与开发版 MainWindow 一致)。"""
        value = int(value) & 0xFFFFFFFF
        return ".".join(str((value >> shift) & 0xFF)
                        for shift in (24, 16, 8, 0))

    def _on_dev_info(self, hw: int, fw: int, uid: bytes):
        """连接后读设备信息(0xD0)应答: 固件版本显示在状态栏。"""
        fw_ver = self._format_version(fw)
        hw_ver = self._format_version(hw)
        uid_hex = uid.hex(':').upper()
        detail = (f"固件版本: {fw_ver}\n硬件版本: {hw_ver}\n"
                  f"UID: {uid_hex}")
        proto = getattr(self._client, "fw_proto_version", None)
        if proto:
            detail += f"\n协议版本: {proto[0]}.{proto[1]}"
        # 更新状态栏固件版本显示
        if proto:
            self._sb_fw_ver.setText(f"FW: {fw_ver}  (协议 v{proto[0]}.{proto[1]})")
        else:
            self._sb_fw_ver.setText(f"FW: {fw_ver}")
        self._sb_fw_ver.setToolTip(detail)
        self.statusBar().showMessage(f"固件版本 v{fw_ver}", 4000)

    # ==================== 电机本体参数读取(转子示意图) ====================
    def _read_motor_params(self):
        """连接后逐条读电机本体参数(0xE6), 驱动转子示意图极对数/槽数。"""
        if not self._client.is_open():
            return
        for param_id in self._MOTOR_PARAM_KEYS:
            try:
                self._client.motor_info_read(param_id)
            except Exception:
                pass

    def _apply_motor_param(self, param_id: int, value_bytes: bytes):
        """单条参数应答落地: 解包 → 收集 → 刷新转子示意图。"""
        key = self._MOTOR_PARAM_KEYS.get(int(param_id))
        if key is None or not value_bytes:
            return
        spec = self._registry.get_motor_config_param(int(param_id))
        dtype = spec.dtype if spec is not None else 'f32'
        val = codec.unpack_value(dtype, value_bytes)
        if val is None:
            return
        self._motor_params[key] = val
        self._feedback_panel.set_motor_params(self._motor_params)

    def _on_motor_info_value(self, param_id: int, _ptype: int, value_bytes):
        self._apply_motor_param(param_id, bytes(value_bytes or b''))

    # ==================== 命令下发 ====================
    def _ensure_open(self) -> bool:
        if not self._client.is_open():
            QMessageBox.warning(self, "提示", "请先连接电机")
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
            self.statusBar().showMessage(
                f"已发送: {cmd_name(cmd)} (0x{cmd:02X})", 2000)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"发送异常: {e}")

    def _on_confirm_danger_cmd(self, cmd: int, payload: bytes) -> bool:
        """危险命令二次确认(测试版仅'清除故障'可达)。"""
        desc = DANGER_CMDS.get(cmd, f"cmd=0x{cmd:02X}")
        btn = QMessageBox.warning(
            self, "危险操作确认",
            f"即将执行: {cmd_name(cmd)} ({desc})\n\n确认要继续执行吗?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return btn == QMessageBox.StandardButton.Yes

    # ==================== 数据接收/分发 ====================
    def _queue_feedback(self, fb):
        if len(self._feedback_pending) >= 2000:
            self._feedback_pending.popleft()
            self._feedback_dropped += 1
        self._feedback_pending.append(fb)

    def _queue_state(self, top_fsm, run_state, ctrl_mode, enable):
        self._latest_state = (top_fsm, run_state, ctrl_mode, enable)

    def _on_ui_tick(self):
        if self._latest_state is not None:
            top, run_state, ctrl_mode, enable = self._latest_state
            self._feedback_panel.update_state(top, run_state, ctrl_mode, enable)
            self._control_panel.set_enabled_state(bool(enable))
            self._sb_mode.setText(
                f"模式: {cmd_name(ctrl_mode)}  "
                f"{'已使能' if enable else '未使能'}")
            self._latest_state = None

        pending = len(self._feedback_pending)
        if pending:
            if pending > self._MAX_FEEDBACK_BATCH_PER_TICK:
                for _ in range(pending - self._MAX_FEEDBACK_BATCH_PER_TICK):
                    self._feedback_pending.popleft()
                self._feedback_dropped += pending - self._MAX_FEEDBACK_BATCH_PER_TICK
            batch = []
            while self._feedback_pending:
                batch.append(self._feedback_pending.popleft())
            self._plot_panel.feed_feedback_batch(batch)
            self._feedback_panel.update_feedback(batch[-1])
        self._feedback_dropped = 0   # 测试版无日志面板, 丢弃计数不累计展示

    def _on_stats_tick(self):
        tp = self._client.transport
        dt = 0.5
        tx_rate = (tp.tx_bytes - self._last_tx_bytes) / dt
        rx_rate = (tp.rx_bytes - self._last_rx_bytes) / dt
        self._last_tx_bytes = tp.tx_bytes
        self._last_rx_bytes = tp.rx_bytes
        self._sb_tx.setText(f"TX {tx_rate:.0f} B/s  Σ{tp.tx_bytes}")
        self._sb_rx.setText(f"RX {rx_rate:.0f} B/s  Σ{tp.rx_bytes}")

    # ==================== 主题/设置 ====================
    def _on_theme_changed(self, name: str):
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.qss())
        for panel in (self._feedback_panel, self._plot_panel):
            fn = getattr(panel, "apply_theme", None)
            if callable(fn):
                fn()
        self._refresh_link_label()
        layout_store.set_meta("theme", name)

    def _apply_settings(self, s: dict):
        if not isinstance(s, dict):
            return
        try:
            self._conn_panel.set_baud(str(s.get("baud", "115200")))
            motion = s.get("motion")
            if isinstance(motion, dict):
                self._motion_panel.set_opts(motion)
            geo = s.get("window")
            if (isinstance(geo, (list, tuple)) and len(geo) == 2
                    and all(isinstance(v, int) for v in geo)):
                self.resize(geo[0], geo[1])
        except Exception:
            pass

    def _collect_settings(self) -> dict:
        return {
            "baud": self._conn_panel.get_baud(),
            "motion": self._motion_panel.get_opts(),
            "window": [int(self.width()), int(self.height())],
        }

    def closeEvent(self, event):
        try:
            layout_store.set_meta(self._SETTINGS_KEY, self._collect_settings())
        except Exception:
            pass
        self._poller.stop()
        self._ui_timer.stop()
        self._stats_timer.stop()
        if self._client.is_open():
            self._client.close()
        self._client.stop()
        event.accept()
