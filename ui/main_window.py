"""关节电机上位机主窗口: 组装各面板 + 连接信号

主窗口只负责: 创建 JmClient(注入 SerialTransport)、组装面板、连接信号槽、
管理连接状态与轮询兜底定时器。具体 UI 细节都在各 panel 内。
"""

import time
from collections import deque
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QGroupBox, QGridLayout, QPushButton, QMessageBox, QLabel, QSplitter,
    QScrollArea, QLayout, QFrame, QDialog, QDialogButtonBox, QFormLayout,
    QSpinBox, QApplication,
)
from PyQt6.QtCore import Qt, QTimer

import jmproto as jp
from jmproto import JmCmd, cmd_name, err_name, err_name_cn, fault_mask_to_str
from transport.serial_transport import SerialTransport
from transport.virtual_engine import VirtualTransport
from core.motor_client import JmClient

from ui.panels.connection_panel import ConnectionPanel
from ui.panels.control_panel import ControlPanel
from ui.panels.motion_panel import MotionPanel
from ui.panels.param_panel import ParamPanel
from ui.panels.feedback_panel import FeedbackPanel
from ui.panels.telemetry_panel import TelemetryPanel
from ui.panels.plot_panel import PlotPanel
from ui.panels.log_panel import LogPanel
from ui.panels.state_machine_panel import StateMachinePanel
from ui.panels.calibration_panel import CalibrationPanel
from ui.panels.twin_param_panel import TwinParamPanel
from ui.panels.fault_info_panel import FaultInfoPanel
from ui.theme import theme
from ui import layout_store


class MainWindow(QMainWindow):
    """关节电机上位机主窗口"""

    _MAX_FEEDBACK_BATCH_PER_TICK = 1000

    # 电机本体参数(转发到反馈面板转子示意图), code_name 对应 CSV 参数字段
    _MOTOR_PARAM_KEYS = ("r", "ld", "lq", "flux", "kt", "ke", "pole_pairs",
                         "elec_angle_bias")

    def __init__(self):
        super().__init__()
        self._motor_params = {}
        # 虚拟电机模式开关的期望状态(从持久化读取, 连接虚拟引擎后套用)
        self._pending_virtual_mode = False
        self._logo_path = Path(__file__).resolve().parent.parent / "resources" / "pic" / "log_ioc.png"
        if self._logo_path.exists():
            self.setWindowIcon(QIcon(str(self._logo_path)))

        # 通信客户端(串口传输)
        self._client = JmClient(SerialTransport())
        self._registry = self._client.registry

        # 流量统计刷新定时器(状态栏速率/总量)
        self._stats_timer = QTimer()
        self._stats_timer.timeout.connect(self._on_stats_tick)
        self._stats_period = 500   # ms
        self._last_tx_bytes = 0
        self._last_rx_bytes = 0

        # 高频遥测进入 UI 前先做有界缓存, 显示/绘图按固定频率刷新。
        self._display_period_ms = 50
        self._display_buffer_max = 2000
        self._feedback_pending = deque()
        self._feedback_dropped = 0
        self._last_drop_log_time = 0.0
        self._latest_state = None
        self._pending_param_reads = deque()
        self._pending_param_writes = deque()
        self._param_read_queue = deque()
        self._param_write_queue = deque()
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(self._display_period_ms)
        self._ui_timer.timeout.connect(self._on_ui_tick)

        self.setWindowTitle("JM Studio")
        self.setMinimumSize(1200, 800)

        self._build_ui()
        self._build_statusbar()
        self._connect_signals()

        # 主题: 订阅变更(重铺 QSS + 广播各面板); 启动时已由 main.py 设好色板
        theme.changed.connect(self._on_theme_changed)

        # 启动加载已保存的自绘图布局(resources/ui_layout.json)
        self._feedback_panel.load_layout()
        self._state_panel.load_layout()

        # 启动恢复用户设置(波特率/缓存/遥测/日志开关与选项)
        self._apply_settings(layout_store.get_meta("settings", {}))

        self._client.start()
        self._stats_timer.start(self._stats_period)
        self._ui_timer.start()

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
        self._left_scroll = QScrollArea()
        self._left_scroll.setWidgetResizable(True)
        self._left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._left_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._left_scroll.setMinimumWidth(220)  # 允许用户拖到很窄
        self._left_scroll.setFrameShape(QFrame.Shape.NoFrame)

        left_content = QWidget()
        self._left_scroll.setWidget(left_content)
        left_layout = QVBoxLayout(left_content)
        left_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self._conn_panel = ConnectionPanel()
        self._control_panel = ControlPanel()
        self._motion_panel = MotionPanel(self._registry)
        self._telemetry_panel = TelemetryPanel()
        self._feedback_panel = FeedbackPanel()
        self._param_panel = ParamPanel(
            self._registry, title="电机参数", source="motor_param", show_save=False)
        self._config_panel = ParamPanel(
            self._registry, title="电机配置", source="motor_config",
            show_save=True, save_text="保存配置到Flash/EEPROM")
        # 标定结果面板: 复用 motor_config 通道(0xE6 读 / 0xE7 写 / 0xEA 固化),
        # 仅展示 MotorCalibParam 段(Index 16~42), 注入到标定 Tab 内嵌展示。
        self._calib_results_panel = ParamPanel(
            self._registry, title="标定结果", source="motor_config",
            groups=("MotorCalibParam",), show_save=True,
            save_text="保存标定结果到Flash/EEPROM")
        self._plot_panel = PlotPanel()
        self._state_panel = StateMachinePanel()
        self._calib_panel = CalibrationPanel()
        self._twin_param_panel = TwinParamPanel()
        self._fault_info_panel = FaultInfoPanel()
        self._log_panel = LogPanel()
        self._log_panel_visible = True

        left_layout.addWidget(self._conn_panel)
        left_layout.addWidget(self._control_panel)
        left_layout.addWidget(self._motion_panel)
        left_layout.addWidget(self._telemetry_panel)
        left_layout.addWidget(self._create_dev_info_group())
        left_layout.addWidget(self._create_menu_config_group())
        left_layout.addStretch()

        # 右侧: 反馈/参数/绘图 选项卡 + 日志
        right = QWidget()
        right_layout = QVBoxLayout(right)

        tabs = QTabWidget()
        tabs.addTab(self._feedback_panel, "实时反馈")
        tabs.addTab(self._state_panel, "状态机")
        tabs.addTab(self._calib_panel, "电机标定")
        tabs.addTab(self._param_panel, "电机参数")
        tabs.addTab(self._config_panel, "电机配置")
        tabs.addTab(self._twin_param_panel, "孪生参数")
        tabs.addTab(self._fault_info_panel, "故障信息")
        tabs.addTab(self._plot_panel, "实时曲线")
        self._tabs = tabs
        # 虚拟电机模式默认关闭: 初始隐藏孪生参数 Tab (故障信息面板内部子表/屏蔽区自行隐藏)
        tabs.setTabVisible(tabs.indexOf(self._twin_param_panel), False)

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

        # 左右分栏: 水平 QSplitter, 用户可拖动手柄调整左右宽度
        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setChildrenCollapsible(False)  # 不允许完全折叠
        self._main_splitter.setHandleWidth(8)
        self._main_splitter.setStyleSheet("""
            QSplitter::handle:horizontal {
                background: #3A3A3A;
                margin: 0 2px;
            }
            QSplitter::handle:horizontal:hover {
                background: #5A8DFF;
            }
        """)
        self._main_splitter.addWidget(self._left_scroll)
        self._main_splitter.addWidget(right)
        self._main_splitter.setStretchFactor(0, 0)  # 左侧不抢空间
        self._main_splitter.setStretchFactor(1, 1)  # 右侧吃多余空间
        self._main_splitter.setSizes([300, 900])    # 初始左 300 / 右 900
        root.addWidget(self._main_splitter)

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
        btn.setMinimumWidth(88)
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

    def _apply_menu_button_style(self, btn: QPushButton):
        # 颜色交由全局 QSS(theme.qss()) 统一管理, 这里只设交互属性与尺寸
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedHeight(26)
        btn.setMinimumWidth(88)
        btn.setStyleSheet("QPushButton { font-weight: bold; }")

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

    def _open_display_settings(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("显示刷新设置")
        layout = QFormLayout(dlg)

        spin_period = QSpinBox(dlg)
        spin_period.setRange(10, 1000)
        spin_period.setSingleStep(10)
        spin_period.setSuffix(" ms")
        spin_period.setValue(self._display_period_ms)

        spin_buffer = QSpinBox(dlg)
        spin_buffer.setRange(100, 50000)
        spin_buffer.setSingleStep(100)
        spin_buffer.setValue(self._display_buffer_max)

        layout.addRow("显示更新周期:", spin_period)
        layout.addRow("最大缓存帧数:", spin_buffer)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel,
            dlg,
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addRow(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        self._apply_display_settings(spin_period.value(), spin_buffer.value())

    def _apply_display_settings(self, period_ms: int, buffer_max: int):
        self._display_period_ms = max(10, int(period_ms))
        self._display_buffer_max = max(100, int(buffer_max))
        self._ui_timer.setInterval(self._display_period_ms)
        self._log_panel.set_flush_period_ms(self._display_period_ms)
        self._log_panel.set_max_pending(self._display_buffer_max)

        while len(self._feedback_pending) > self._display_buffer_max:
            self._feedback_pending.popleft()
            self._feedback_dropped += 1

        self.statusBar().showMessage(
            f"显示刷新 {self._display_period_ms}ms, 缓存 {self._display_buffer_max} 帧",
            3000,
        )

    # ==================== 用户设置持久化 ====================
    def _collect_settings(self) -> dict:
        """收集需持久化的用户设置(关窗时写入 ui_layout.json 的 settings 节)。"""
        s = {
            "baud": self._conn_panel.get_baud(),
            "display_period_ms": self._display_period_ms,
            "display_buffer_max": self._display_buffer_max,
            "main_window": {
                "width": int(self.width()),
                "height": int(self.height()),
                "x": int(self.x()),
                "y": int(self.y()),
            },
            "right_splitter": self._safe_sizes(self._right_splitter),
            "log_splitter_sizes": self._safe_sizes_log(),
            "layout_edit": bool(self._btn_layout_edit.isChecked()),
            "telemetry": {
                "mask": self._telemetry_panel.current_mask(),
                "period_ms": self._telemetry_panel.get_period(),
            },
            "feedback_poll": {
                "enabled": self._feedback_panel._chk_poll.isChecked(),
                "period_ms": self._feedback_panel.get_poll_period(),
            },
            "log_visible": self._log_panel_visible,
            "log_opts": self._log_panel.get_opts(),
            # 虚拟电机模式开关状态(下次连接虚拟引擎时自动恢复)
            "virtual_motor_mode": bool(self._btn_virtual_mode.isChecked()),
        }
        # 各面板配置 (容错: 单面板失败不影响整体保存)
        for key, panel in (
            ("plot", self._plot_panel),
            ("motion", self._motion_panel),
            ("motor_param", self._param_panel),
            ("motor_config", self._config_panel),
            ("calib", self._calib_panel),
            ("twin_param", self._twin_param_panel),
            ("fault_info", self._fault_info_panel),
        ):
            try:
                s[key] = panel.get_opts()
            except Exception:
                pass
        return s

    def _safe_sizes(self, splitter) -> list:
        try:
            return [int(x) for x in splitter.sizes()]
        except Exception:
            return []

    def _safe_sizes_log(self) -> list:
        try:
            return [int(x) for x in (self._log_splitter_sizes or [])]
        except Exception:
            return []

    def _apply_settings(self, d: dict):
        """启动时把已存设置套回各控件(仅设 UI 状态, 不触发串口/遥测命令)。"""
        if not isinstance(d, dict):
            return
        baud = d.get("baud")
        if baud:
            self._conn_panel.set_baud(baud)
        if "display_period_ms" in d or "display_buffer_max" in d:
            self._apply_display_settings(
                d.get("display_period_ms", self._display_period_ms),
                d.get("display_buffer_max", self._display_buffer_max))
        mw = d.get("main_window")
        if isinstance(mw, dict):
            try:
                width = int(mw.get("width", self.width()))
                height = int(mw.get("height", self.height()))
                self.resize(max(self.minimumWidth(), width),
                            max(self.minimumHeight(), height))
                # 恢复窗口位置 (避免移出屏幕)
                x = mw.get("x")
                y = mw.get("y")
                if x is not None and y is not None:
                    self.move(int(x), int(y))
            except Exception:
                pass
        # 右侧 splitter 尺寸 (优先 log_splitter_sizes 若日志隐藏)
        rs = d.get("right_splitter")
        if isinstance(rs, list) and len(rs) >= 2:
            try:
                self._right_splitter.setSizes([int(x) for x in rs])
                self._log_splitter_sizes = [int(x) for x in rs]
            except Exception:
                pass
        lss = d.get("log_splitter_sizes")
        if isinstance(lss, list) and len(lss) >= 2:
            try:
                self._log_splitter_sizes = [int(x) for x in lss]
            except Exception:
                pass
        # 布局编辑开关
        if "layout_edit" in d:
            try:
                self._btn_layout_edit.setChecked(bool(d["layout_edit"]))
            except Exception:
                pass
        tlm = d.get("telemetry")
        if isinstance(tlm, dict):
            # 强制补全 DQ/PHASE 位: 实时反馈与曲线的三相电流依赖这两位,
            # 否则遥测帧不含 ia/ib/ic, 显示恒为0
            mask = tlm.get("mask", 0) | jp.JmTlmBit.DQ | jp.JmTlmBit.PHASE
            self._telemetry_panel.apply_config(mask, tlm.get("period_ms", 20))
        fb_poll = d.get("feedback_poll")
        if isinstance(fb_poll, dict):
            self._feedback_panel.apply_poll_config(
                fb_poll.get("enabled", True), fb_poll.get("period_ms", 200))
        log_opts = d.get("log_opts")
        if isinstance(log_opts, dict):
            self._log_panel.set_opts(log_opts)
        # 日志显隐: 若存值与当前不一致则切换(复用现有逻辑维护 splitter 尺寸)
        if "log_visible" in d and bool(d["log_visible"]) != self._log_panel_visible:
            self._toggle_log_panel()
        # 虚拟电机模式开关: 仅记录期望状态, 待连接虚拟引擎后套用
        # (开关可用性取决于是否连接虚拟引擎, 启动时未连接故保持禁用)
        vm = d.get("virtual_motor_mode")
        if isinstance(vm, bool):
            self._pending_virtual_mode = vm
        # 各面板配置 (容错: 单面板失败不影响其他)
        for key, panel in (
            ("plot", self._plot_panel),
            ("motion", self._motion_panel),
            ("motor_param", self._param_panel),
            ("motor_config", self._config_panel),
            ("calib", self._calib_panel),
            ("twin_param", self._twin_param_panel),
            ("fault_info", self._fault_info_panel),
        ):
            opts = d.get(key)
            if isinstance(opts, dict):
                try:
                    panel.set_opts(opts)
                except Exception:
                    pass

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

    def _create_menu_config_group(self) -> QGroupBox:
        grp = QGroupBox("菜单配置")
        layout = QGridLayout(grp)
        btn_display = QPushButton("缓存设置")
        btn_display.clicked.connect(self._open_display_settings)
        self._apply_menu_button_style(btn_display)
        layout.addWidget(btn_display, 0, 0)
        layout.addWidget(self._create_log_toggle_button(), 0, 1)
        # 布局编辑门控: 默认关; 开启后各图显示编辑/导出按钮并允许拖拽
        self._btn_layout_edit = QPushButton("布局编辑")
        self._btn_layout_edit.setCheckable(True)
        self._apply_menu_button_style(self._btn_layout_edit)
        self._btn_layout_edit.setStyleSheet(self._btn_layout_edit.styleSheet() +
            "\nQPushButton:checked{background:#C8963C;color:#1a1a1a;border-color:#E0B566;}")
        self._btn_layout_edit.toggled.connect(self._on_layout_edit_gate)
        layout.addWidget(self._btn_layout_edit, 1, 0)
        # 主题切换(深/浅), 记住上次选择
        self._btn_theme = QPushButton("主题: 深色" if theme.is_dark else "主题: 浅色")
        self._apply_menu_button_style(self._btn_theme)
        self._btn_theme.clicked.connect(self._on_toggle_theme)
        layout.addWidget(self._btn_theme, 1, 1)
        # 虚拟电机模式开关: 仅在选中虚拟引擎并打开串口后可用。
        # 开启后显示孪生参数 Tab、虚拟电机 Fault 位子表与故障屏蔽区。
        self._btn_virtual_mode = QPushButton("虚拟电机: 关")
        self._btn_virtual_mode.setCheckable(True)
        self._btn_virtual_mode.setChecked(False)
        self._btn_virtual_mode.setEnabled(False)  # 未连接虚拟引擎前禁用
        self._btn_virtual_mode.setToolTip(
            "仅在连接虚拟引擎时可用。开启后显示孪生参数、虚拟电机 Fault 位表与故障屏蔽区。")
        self._apply_menu_button_style(self._btn_virtual_mode)
        self._btn_virtual_mode.setStyleSheet(
            self._btn_virtual_mode.styleSheet() +
            "\nQPushButton:disabled{background:#2A2A2A;color:#777;border-color:#444;}"
            "\nQPushButton:checked{background:#03A9F4;color:white;border-color:#29B6F6;}")
        self._btn_virtual_mode.toggled.connect(self._on_virtual_mode_toggled)
        layout.addWidget(self._btn_virtual_mode, 2, 0, 1, 2)
        return grp

    def _on_toggle_theme(self):
        theme.set("light" if theme.is_dark else "dark")

    def _on_theme_changed(self, name: str):
        """主题变更: 重铺全局 QSS + 广播各面板 apply_theme + 持久化。"""
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.qss())
        self._btn_theme.setText("主题: 深色" if theme.is_dark else "主题: 浅色")
        for panel in (self._feedback_panel, self._state_panel, self._plot_panel,
                      self._log_panel, self._param_panel, self._config_panel,
                      self._calib_panel, self._twin_param_panel, self._fault_info_panel):
            fn = getattr(panel, "apply_theme", None)
            if callable(fn):
                fn()
        self._refresh_statusbar_theme()
        layout_store.set_meta("theme", name)

    def _on_layout_edit_gate(self, on: bool):
        self._feedback_panel.set_layout_edit(on)
        self._state_panel.set_layout_edit(on)
        self.statusBar().showMessage(
            "布局编辑已开启: 在实时反馈/状态机页拖动方框, 完成点[导出布局]" if on
            else "布局编辑已关闭", 4000)

    def _on_virtual_mode_toggled(self, on: bool):
        """虚拟电机模式开关: 控制孪生参数 Tab 与故障信息面板内虚拟电机专属视图显隐。

        守卫: 仅在连接虚拟引擎时允许开启; 否则强制取消勾选(由 setEnabled 联动保证)。
        """
        # 更新按钮文字
        self._btn_virtual_mode.setText("虚拟电机: 开" if on else "虚拟电机: 关")
        # 孪生参数 Tab 显隐
        idx_param = self._tabs.indexOf(self._twin_param_panel)
        self._tabs.setTabVisible(idx_param, on)
        # 故障信息面板内: 虚拟电机 Fault 位子表 + 故障屏蔽区显隐
        self._fault_info_panel.set_virtual_mode(on)
        # 关闭模式时, 若当前正停在孪生参数 Tab, 切回首页(实时反馈)
        if not on and self._tabs.currentWidget() is self._twin_param_panel:
            self._tabs.setCurrentIndex(0)
        self.statusBar().showMessage(
            "虚拟电机模式: 开启 (孪生参数/虚拟电机 Fault 位/故障屏蔽已显示)" if on
            else "虚拟电机模式: 关闭", 3000)


    # ==================== 信号连接 ====================
    def _connect_signals(self):
        c = self._client
        c.connected.connect(self._on_connected)
        c.error_occurred.connect(self._on_error)
        c.tx_frame.connect(self._log_panel.log_tx)
        c.raw_frame.connect(self._log_panel.log_rx)
        c.feedback_updated.connect(self._queue_feedback)
        c.state_updated.connect(self._queue_state)
        c.ack_received.connect(self._on_ack)
        c.nack_received.connect(self._on_nack)
        c.dev_info_received.connect(self._on_dev_info)
        c.dev_name_received.connect(self._on_dev_name)
        c.param_read_result.connect(self._on_param_result)
        c.motor_info_read_result.connect(self._on_param_result)

        # 状态机面板: 周期请求 -> 拉取 READ_STATE
        self._state_panel.poll_state.connect(self._on_poll_state)
        self._feedback_panel.poll_state.connect(self._on_poll_state)

        # 面板 -> 客户端
        self._conn_panel.connect_requested.connect(self._on_connect)
        self._conn_panel.disconnect_requested.connect(self._on_disconnect)
        self._control_panel.command.connect(self._on_control_command)
        self._motion_panel.send_command.connect(self._on_motion_command)
        # 标定面板: 复用运动指令发送通道 (cmd=0x90~0x98, values={"submode":N} 或 {})
        self._calib_panel.send_command.connect(self._on_motion_command)
        # 标定结果面板: 注入标定 Tab 内嵌, 复用 motor_config 通道(0xE6/0xE7/0xEA)
        self._calib_panel.attach_results_panel(self._calib_results_panel)
        self._calib_results_panel.read_param.connect(
            lambda param_id, panel=self._calib_results_panel:
                self._on_param_read(panel, param_id))
        self._calib_results_panel.write_param.connect(
            lambda param_id, text, panel=self._calib_results_panel:
                self._on_param_write(panel, param_id, text))
        self._calib_results_panel.read_params.connect(
            lambda param_ids, panel=self._calib_results_panel:
                self._on_param_read_many(panel, param_ids))
        self._calib_results_panel.write_params.connect(
            lambda writes, panel=self._calib_results_panel:
                self._on_param_write_many(panel, writes))
        self._calib_results_panel.save_all.connect(self._on_config_save)
        self._telemetry_panel.apply_telemetry.connect(self._on_apply_telemetry)
        self._param_panel.read_param.connect(
            lambda param_id, panel=self._param_panel: self._on_param_read(panel, param_id))
        self._param_panel.write_param.connect(
            lambda param_id, text, panel=self._param_panel: self._on_param_write(panel, param_id, text))
        self._param_panel.read_params.connect(
            lambda param_ids, panel=self._param_panel: self._on_param_read_many(panel, param_ids))
        self._param_panel.write_params.connect(
            lambda writes, panel=self._param_panel: self._on_param_write_many(panel, writes))

        self._config_panel.read_param.connect(
            lambda param_id, panel=self._config_panel: self._on_param_read(panel, param_id))
        self._config_panel.write_param.connect(
            lambda param_id, text, panel=self._config_panel: self._on_param_write(panel, param_id, text))
        self._config_panel.read_params.connect(
            lambda param_ids, panel=self._config_panel: self._on_param_read_many(panel, param_ids))
        self._config_panel.write_params.connect(
            lambda writes, panel=self._config_panel: self._on_param_write_many(panel, writes))
        self._config_panel.save_all.connect(self._on_config_save)

    # ==================== 连接管理 ====================
    def _on_connect(self, port: str, baud: int):
        # 选择虚拟数据引擎: 切换到 VirtualTransport, 无需真实串口
        if port == "VIRTUAL":
            if not isinstance(self._client.transport, VirtualTransport):
                self._client.set_transport(VirtualTransport())
            if self._client.open():
                self.statusBar().showMessage("已连接 虚拟数据引擎 (演示)")
                self._cur_port = "VIRTUAL"
                self._log_panel.log("[SIM] 虚拟数据引擎已启动, 所有数据由本地仿真生成")
                # 把孪生引擎句柄传给孪生参数面板
                self._twin_param_panel.set_engine(self._client.transport.get_engine())
                # 故障屏蔽需要引擎句柄
                self._fault_info_panel.set_engine(self._client.transport.get_engine())
                # 虚拟电机模式开关: 连接虚拟引擎后才可用
                self._btn_virtual_mode.setEnabled(True)
                # 若上次保存的状态为开启, 自动勾选
                if self._pending_virtual_mode and not self._btn_virtual_mode.isChecked():
                    self._btn_virtual_mode.setChecked(True)
                self._pending_virtual_mode = False
            return
        # 真实串口: 若当前是虚拟传输, 换回串口传输
        if not isinstance(self._client.transport, SerialTransport):
            self._client.set_transport(SerialTransport())
        if self._client.open(port=port, baudrate=baud):
            self.statusBar().showMessage(f"已连接 {port} @{baud}")
            self._cur_port = port
            # 连接真机: 虚拟电机模式开关必须关闭并禁用
            self._force_disable_virtual_mode()

    def _force_disable_virtual_mode(self):
        """真机连接或断开时: 强制关闭并禁用虚拟电机模式开关, 隐藏相关视图。"""
        self._btn_virtual_mode.setEnabled(False)
        if self._btn_virtual_mode.isChecked():
            # blockSignals 避免重复触发回调; 直接同步隐藏 UI
            self._btn_virtual_mode.blockSignals(True)
            self._btn_virtual_mode.setChecked(False)
            self._btn_virtual_mode.blockSignals(False)
            self._on_virtual_mode_toggled(False)

    def _on_disconnect(self):
        # 断开前若仍在周期上报, 通知下位机停止, 并复位面板开关
        if self._client.is_open():
            self._client.set_telemetry(False, 0, 0)
        self._telemetry_panel.set_running(False)
        self._feedback_pending.clear()
        self._latest_state = None
        self._pending_param_reads.clear()
        self._pending_param_writes.clear()
        self._param_read_queue.clear()
        self._param_write_queue.clear()
        self._client.close()
        self._twin_param_panel.set_engine(None)
        self._fault_info_panel.set_engine(None)
        # 断开连接: 强制关闭并禁用虚拟电机模式开关
        self._force_disable_virtual_mode()
        self.statusBar().showMessage("已断开")

    def _on_connected(self, connected: bool):
        self._conn_panel.set_connected(connected)
        self._feedback_panel.set_link_active(connected)
        self._state_panel.set_link_active(connected)
        self._calib_panel.set_link_active(connected)
        self._link_connected = connected
        if not connected:
            self._telemetry_panel.set_running(False)
        self._refresh_statusbar_theme()

    def _refresh_statusbar_theme(self):
        """按连接状态 + 当前主题重设状态栏连接标签颜色。"""
        connected = getattr(self, "_link_connected", False)
        if connected:
            self._sb_link.setText(f"● {getattr(self, '_cur_port', '')}")
            self._sb_link.setStyleSheet(
                f"font-family: Consolas; padding: 0 8px; color: {theme.hex('ok')}; font-weight: bold;")
        else:
            self._sb_link.setText("● 未连接")
            self._sb_link.setStyleSheet(
                f"font-family: Consolas; padding: 0 8px; color: {theme.hex('muted')};")

    def _on_error(self, msg: str):
        self._log_panel.log_err(msg)
        self.statusBar().showMessage(msg)

    # ==================== 命令下发 ====================
    def _ensure_open(self) -> bool:
        if not self._client.is_open():
            QMessageBox.warning(self, "提示", "请先连接串口")
            return False
        return True

    def _on_poll_state(self):
        """状态机面板周期请求: 静默拉取 READ_STATE + 三相/dq 电流 + 反馈(不弹窗)。

        READ_FEEDBACK 只含 pos/vel/torque/temp/vbus/fault 6 字段, 不含电流;
        READ_STATE 只含状态机 4 字节。三相电流只能通过 READ_PHASE_CURRENT /
        READ_DQ_CURRENT 主动查询。这里周期补查, 配合 JmClient 的 _last_feedback
        合并机制, 让 UI 拿到完整的反馈数据。
        """
        if not self._client.is_open():
            return
        self._client.query_state()
        self._client.query_dq_current()
        self._client.query_phase_current()

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

    def _on_apply_telemetry(self, enable: bool, mask: int, period_ms: int):
        if not self._ensure_open():
            self._telemetry_panel.set_running(False)
            return
        self._client.set_telemetry(enable, mask, period_ms)
        if enable:
            self._log_panel.log(
                f"[TX] 遥控使能: mask=0x{mask:04X} 周期={period_ms}ms")
        else:
            self._log_panel.log("[TX] 遥控停止")

    # ==================== 参数读写 ====================
    def _panel_remote_supported(self, panel: ParamPanel) -> bool:
        # motor_config(motor_info 0xE6-0xEB) 与 motor_param(0xE0-0xE5) 均已支持
        return True

    def _on_param_read(self, panel: ParamPanel, param_id: int):
        if not self._panel_remote_supported(panel):
            return
        if self._ensure_open():
            self._param_read_queue.append((panel, int(param_id)))
            self._pump_param_read_queue()

    def _on_param_read_many(self, panel: ParamPanel, param_ids):
        if not self._panel_remote_supported(panel):
            return
        if not self._ensure_open():
            return
        queued = 0
        for param_id in param_ids:
            self._param_read_queue.append((panel, int(param_id)))
            queued += 1
        if queued:
            self._pump_param_read_queue()
            self._log_panel.log(f"[TX] {panel.panel_name()} 批量读取 {queued} 项, 应答驱动")

    def _on_param_write(self, panel: ParamPanel, param_id: int, text: str):
        if not self._panel_remote_supported(panel):
            return
        if not self._ensure_open():
            return
        try:
            value = panel.pack_value(param_id, text)
        except Exception:
            QMessageBox.warning(self, "错误", f"参数值无效: {text}")
            return
        self._param_write_queue.append((panel, int(param_id), value))
        self._pump_param_write_queue()

    def _on_param_write_many(self, panel: ParamPanel, writes):
        if not self._panel_remote_supported(panel):
            return
        if not self._ensure_open():
            return
        queued = 0
        for param_id, text in writes:
            try:
                value = panel.pack_value(param_id, text)
            except Exception:
                self._log_panel.log_warn(f"{panel.panel_name()} 参数值无效: id={param_id} value={text}")
                continue
            self._param_write_queue.append((panel, int(param_id), value))
            queued += 1
        if queued:
            self._pump_param_write_queue()
            self._log_panel.log(f"[TX] {panel.panel_name()} 批量写入 {queued} 项, 应答驱动")

    def _pump_param_read_queue(self):
        if self._pending_param_reads or not self._param_read_queue:
            return
        panel, param_id = self._param_read_queue.popleft()
        self._send_param_read_queued(panel, param_id)

    def _pump_param_write_queue(self):
        if self._pending_param_writes or not self._param_write_queue:
            return
        panel, param_id, value = self._param_write_queue.popleft()
        self._send_param_write_queued(panel, param_id, value)

    def _send_param_read_queued(self, panel: ParamPanel, param_id: int):
        if not self._client.is_open():
            return
        # 按面板来源分派: motor_config -> 0xE6, motor_param -> 0xE0
        if panel.source() == "motor_config":
            if self._client.motor_info_read(int(param_id)):
                self._pending_param_reads.append(panel)
        else:
            if self._client.param_read(int(param_id)):
                self._pending_param_reads.append(panel)

    def _send_param_write_queued(self, panel: ParamPanel, param_id: int, value: bytes):
        if not self._client.is_open():
            return
        if panel.source() == "motor_config":
            if self._client.motor_info_write(int(param_id), value):
                panel.note_write_sent(int(param_id))
                self._pending_param_writes.append(panel)
        else:
            if self._client.param_write(int(param_id), value):
                panel.note_write_sent(int(param_id))
                self._pending_param_writes.append(panel)

    def _on_config_save(self):
        if not self._panel_remote_supported(self._config_panel):
            return
        if self._ensure_open():
            # 电机配置走 0xEA (motor_info_save), 区别于运行时参数的 0xE4
            self._client.motor_info_save()
            self._log_panel.log("[TX] 保存电机配置到Flash/EEPROM (0xEA)")

    def _on_param_result(self, param_id: int, ptype: int, value_bytes: bytes):
        panel = self._pending_param_reads.popleft() if self._pending_param_reads else self._param_panel
        val = panel.unpack_value(param_id, value_bytes)
        # float 用 %.6g 格式化(6位有效数字), 去掉 float32 表示误差产生的长尾小数
        # 例: 0.10000000149011612 -> 0.1, 0.8500000238418579 -> 0.85, 1000 -> 1000
        spec = panel.get_param(param_id)
        if val is None:
            disp = value_bytes.hex(' ')
        elif spec is not None and jp.codec.is_float_type(spec.dtype):
            disp = f'{val:.6g}'
        else:
            disp = str(val)
        panel.set_value(param_id, disp)
        name = spec.code_name if spec else f"id={param_id}"
        self._log_panel.log(f"[RX] {panel.panel_name()} {name}(id={param_id}) = {disp}")
        # 电机本体参数转发到反馈面板的转子示意图(电阻/电感/磁链/转矩常数/极对数等)
        if spec is not None and val is not None and spec.code_name in self._MOTOR_PARAM_KEYS:
            self._motor_params[spec.code_name] = val
            self._feedback_panel.set_motor_params(self._motor_params)
        self._pump_param_read_queue()

    # ==================== 数据接收 ====================
    def _queue_feedback(self, fb):
        if len(self._feedback_pending) >= self._display_buffer_max:
            self._feedback_pending.popleft()
            self._feedback_dropped += 1
        self._feedback_pending.append((time.monotonic(), fb))

    def _queue_state(self, top_fsm, run_state, ctrl_mode, enable):
        self._latest_state = (top_fsm, run_state, ctrl_mode, enable)

    def _on_ui_tick(self):
        if self._latest_state is not None:
            self._feedback_panel.update_state(*self._latest_state)
            self._state_panel.update_state(*self._latest_state)
            self._calib_panel.update_state(*self._latest_state)
            self._control_panel.set_enabled_state(bool(self._latest_state[3]))
            self._latest_state = None

        pending_count = len(self._feedback_pending)
        if pending_count:
            if pending_count > self._MAX_FEEDBACK_BATCH_PER_TICK:
                drop_count = pending_count - self._MAX_FEEDBACK_BATCH_PER_TICK
                for _ in range(drop_count):
                    self._feedback_pending.popleft()
                self._feedback_dropped += drop_count

            batch = []
            while self._feedback_pending:
                batch.append(self._feedback_pending.popleft())

            # P4 优化: 批量向量化喂曲线, 避免逐帧 Python 调用开销
            ts_list = [item[0] for item in batch]
            fb_list = [item[1] for item in batch]
            self._plot_panel.feed_feedback_batch(ts_list, fb_list)
            last_fb = batch[-1][1]
            self._feedback_panel.update_feedback(last_fb)
            # 实时故障解码: 故障上升沿才记入历史
            self._fault_info_panel.update_fault_mask(
                getattr(last_fb, 'fault_mask', 0) or 0)

        if self._feedback_dropped:
            now = time.monotonic()
            if now - self._last_drop_log_time >= 2.0:
                dropped = self._feedback_dropped
                self._feedback_dropped = 0
                self._last_drop_log_time = now
                self._log_panel.log_warn(f"遥测显示缓存已满, 丢弃 {dropped} 帧旧数据")

    def _on_ack(self, cmd: int):
        self._log_panel.log(f"[RX] ACK {cmd_name(cmd)}(0x{cmd:02X})")
        # 标定面板: 0x90~0x98 ACK 转发(内部按 cmd 过滤)
        self._calib_panel.on_ack(cmd)
        # 写完成: 0xE1(运行时参数) / 0xE7(电机配置) 共用同一待应答队列
        if cmd in (JmCmd.PARAM_WRITE, JmCmd.MOTOR_INFO_WRITE):
            panel = self._pending_param_writes.popleft() if self._pending_param_writes else self._param_panel
            panel.confirm_pending_write()
            self._pump_param_write_queue()

    def _on_nack(self, cmd: int, err: int):
        # NACK 中文描述: err_name_cn 返回 "枚举名(中文)"; 错误信息加载到故障面板
        cn = err_name_cn(err)
        self._log_panel.log_warn(
            f"NACK {cmd_name(cmd)}(0x{cmd:02X}) err={cn}(0x{err:02X})")
        # 故障信息面板加载 NACK 错误信息
        self._fault_info_panel.show_nack_error(cmd, err)
        # 标定面板: 0x90~0x98 NACK 转发(内部按 cmd 过滤)
        self._calib_panel.on_nack(cmd, err)
        # 读失败: 0xE0 / 0xE6 共用同一待应答队列
        if cmd in (JmCmd.PARAM_READ, JmCmd.MOTOR_INFO_READ) and self._pending_param_reads:
            self._pending_param_reads.popleft()
            self._pump_param_read_queue()
        # 写失败: 0xE1 / 0xE7 共用同一待应答队列
        if cmd in (JmCmd.PARAM_WRITE, JmCmd.MOTOR_INFO_WRITE):
            panel = self._pending_param_writes.popleft() if self._pending_param_writes else self._param_panel
            panel.reject_pending_write()
            self._pump_param_write_queue()

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
        # 保存用户设置到本地(波特率/缓存/遥测/日志开关与选项)
        try:
            layout_store.set_meta("settings", self._collect_settings())
        except Exception:
            pass
        # 退出前若仍在周期上报, 通知下位机停止, 避免串口关闭后下位机继续发
        if self._client.is_open():
            self._client.set_telemetry(False, 0, 0)
        self._stats_timer.stop()
        self._ui_timer.stop()
        self._log_panel.flush()
        self._client.stop()
        event.accept()
