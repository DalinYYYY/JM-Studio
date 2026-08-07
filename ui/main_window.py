"""关节电机上位机主窗口: 组装各面板 + 连接信号

主窗口只负责: 创建 JmClient(注入 SerialTransport)、组装面板、连接信号槽、
管理连接状态与轮询兜底定时器。具体 UI 细节都在各 panel 内。
"""

import time
import json
import secrets
import math
from collections import deque
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QGroupBox, QGridLayout, QPushButton, QMessageBox, QLabel, QSplitter,
    QScrollArea, QLayout, QFrame, QDialog, QDialogButtonBox, QFormLayout,
    QSpinBox, QApplication, QComboBox, QFileDialog, QInputDialog,
)
from PyQt6.QtCore import Qt, QTimer

import jmproto as jp
from jmproto import JmCmd, cmd_name, err_name, err_name_cn, fault_mask_to_str
from transport.serial_transport import SerialTransport
from transport.virtual_engine import VirtualTransport
from core.motor_client import JmClient, DANGER_CMDS
from core.pid_debug_profile import get_pid_debug_profile
from core.pid_autotune import estimate_pid_proposal

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
from ui.panels.pid_panel import PidPanel, PidSource
from ui.panels.twin_param_panel import TwinParamPanel
from ui.panels.fault_info_panel import FaultInfoPanel
from ui.panels.debug_plot_window import DebugPlotWindow
from ui.csv_load_dialog import CsvLoadDialog
from ui.can_device_manager import CanDeviceManagerDialog
from ui.theme import theme
from ui import feature_flags
from ui import layout_store
from ui import preset_store
from ui import APP_NAME, APP_VERSION
from ui.preset_edit_dialog import PresetEditDialog
from ui.preset_manage_dialog import PresetManageDialog


def format_version(value: int) -> str:
    """将协议 u32 版本号格式化为 major.minor.patch.build。"""
    value = int(value) & 0xFFFFFFFF
    return ".".join(str((value >> shift) & 0xFF)
                    for shift in (24, 16, 8, 0))


class MainWindow(QMainWindow):
    """关节电机上位机主窗口"""

    _MAX_FEEDBACK_BATCH_PER_TICK = 1000

    # 电机本体参数(转发到反馈面板转子示意图), code_name 对应 CSV 参数字段
    _MOTOR_PARAM_KEYS = ("r", "ld", "lq", "flux", "kt", "ke", "pole_pairs",
                         "elec_angle_bias")

    _PID_THEORY_PARAM_NAMES = (
        "phase_resistance", "phase_inductance_d", "phase_inductance_q",
        "torque_constant", "rotor_inertia", "current_lim",
        "current_control_bandwidth", "peak_current",
    )

    def __init__(self):
        super().__init__()
        self._motor_params = {}
        # 虚拟电机模式开关的期望状态(从持久化读取, 连接虚拟引擎后套用)
        self._pending_virtual_mode = False
        # 调试通道窗口
        self._debug_window: DebugPlotWindow | None = None
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
        # P3: motor_info 导出状态(异步批量读时拦截 _on_param_result 收集值)
        # None=未导出; dict={file_path, fields, total, collected, config_version}
        self._export_state = None
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(self._display_period_ms)
        self._ui_timer.timeout.connect(self._on_ui_tick)
        # 命令超时看门狗: 设备不响应时弹出 pending 项, 防止读写队列永久死锁
        self._cmd_watchdog = QTimer(self)
        self._cmd_watchdog.setSingleShot(True)
        self._cmd_watchdog.setInterval(1000)  # 1s 超时
        self._cmd_watchdog.timeout.connect(self._on_cmd_timeout)
        # 自动重连: 链路异常断开时周期尝试重连, 用户主动断开不触发
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(False)
        self._reconnect_timer.setInterval(2000)  # 2s 重试间隔
        self._reconnect_timer.timeout.connect(self._on_reconnect_tick)
        self._reconnect_attempts = 0
        self._reconnect_max_attempts = 10  # 最多重试 10 次 (~20s), 超过则放弃
        self._user_disconnected = False    # True=用户主动断开, 不自动重连
        self._last_connect_params: dict | None = None  # 上次成功连接参数

        # CAN 节点扫描与地址修改状态
        self._can_scan_timer = QTimer(self)
        self._can_scan_timer.setSingleShot(True)
        self._can_scan_timer.timeout.connect(self._advance_can_di_scan)
        self._can_scan_active = False
        self._can_scan_round = 0
        self._can_scan_current_round = 0
        self._can_scan_total_rounds = 3
        self._can_scan_slot_exp = 8
        self._can_scan_slot_ms = 2
        self._can_scan_results: dict[bytes, dict] = {}
        self._can_device_dialog: CanDeviceManagerDialog | None = None
        self._can_commissioning_required = False
        self._can_set_queue = deque()
        self._can_set_pending_key: bytes | None = None
        self._can_set_pending_new_id: int | None = None
        self._can_set_generation = 0
        self._pending_can_id: int | None = None
        self._verify_can_id: int | None = None

        # PID 调试页运行态: 最近一次上位机下发目标值 + 临时遥测接管快照
        self._pid_last_targets = {"id": None, "iq": None, "vel": None, "pos": None}
        self._pid_telemetry_snapshot: dict | None = None
        self._pid_telemetry_active = False
        self._active_main_tab_widget = None
        self._trace_session_id = 0
        self._trace_expected_sequence = None
        self._trace_last_sample_index = None
        self._trace_received_points = 0
        self._trace_lost_packets = 0
        self._trace_overflow_count = 0
        self._trace_session_start_time = 0.0
        self._trace_device_rate_hz = 0
        self._trace_effective_rate_hz = 0.0
        self._trace_last_data_time = 0.0
        self._pid_debug_heartbeat_timer = QTimer(self)
        self._pid_debug_heartbeat_timer.setInterval(500)
        self._pid_debug_heartbeat_timer.timeout.connect(self._send_pid_debug_heartbeat)
        self._pid_commit_expected = {}
        self._pid_commit_source_ring = None
        self._pid_commit_phase = 'idle'
        self._pid_theory_pending: dict | None = None

        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
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
            self._registry, title="电机参数", source="motor_param",
            show_save=False, show_legend=False)
        self._config_panel = ParamPanel(
            self._registry, title="电机配置", source="motor_config",
            show_save=True, save_text="参数固化", show_legend=False,
            show_import_export=True)
        # 电机型号预设下拉框: 包装 _config_panel, 顶部放 ComboBox + 应用按钮
        # 选择预设后一键填充 9 个电气字段(R/Ld/Lq/flux/pole_pairs/Kt/inertia/peak_current/max_speed)
        # 用户确认后点击"参数固化"按钮 0xEA 持久化
        self._config_tab_container = QWidget()
        _config_layout = QVBoxLayout(self._config_tab_container)
        _config_layout.setContentsMargins(4, 4, 4, 4)
        _preset_layout = QHBoxLayout()
        _preset_layout.addWidget(QLabel("电机型号预设:"))
        self._motor_preset_combo = QComboBox()
        self._motor_preset_combo.addItem("(请选择电机型号)", None)
        self._motor_preset_combo.addItem("(自定义参数)", "")
        self._motor_preset_combo.currentIndexChanged.connect(self._on_motor_preset_selected)
        _preset_layout.addWidget(self._motor_preset_combo, 1)
        self._motor_preset_apply_btn = QPushButton("应用到 motor_info (0xE7)")
        self._motor_preset_apply_btn.setToolTip(
            "把所选电机型号的电气参数批量写入 motor_info RAM (0xE7)\n"
            "写入后需点击下方'参数固化'按钮(0xEA)持久化到 Flash")
        self._motor_preset_apply_btn.clicked.connect(self._on_motor_preset_apply)
        _preset_layout.addWidget(self._motor_preset_apply_btn)
        # P3: motor_info 导出/导入按钮 — 应对 CONFIG_VERSION 升级清空 Flash 风险
        # 导出: 0xE6 批量读取 → JSON 文件; 导入: JSON 文件 → 0xE7 批量写入
        self._motor_info_export_btn = QPushButton("导出")
        self._motor_info_export_btn.setToolTip(
            "通过 0xE6 批量读取所有 motor_info 字段, 保存为 JSON 文件\n"
            "用于固件升级前备份 (CONFIG_VERSION 升级会清空 Flash)")
        self._motor_info_export_btn.clicked.connect(self._on_motor_info_export)
        _preset_layout.addWidget(self._motor_info_export_btn)
        self._motor_info_import_btn = QPushButton("导入")
        self._motor_info_import_btn.setToolTip(
            "从 JSON 文件加载 motor_info, 通过 0xE7 批量写入 RAM\n"
            "用于固件升级后恢复标定数据, 写入后需点 0xEA 固化")
        self._motor_info_import_btn.clicked.connect(self._on_motor_info_import)
        _preset_layout.addWidget(self._motor_info_import_btn)
        # 新建电机预设按钮: 弹出 PresetEditDialog 新建用户预设
        self._preset_new_btn = QPushButton("新建预设")
        self._preset_new_btn.setToolTip(
            "新建用户自定义电机预设，保存到 user_motor_presets.json\n"
            "新建后可在下拉框选择并应用到下位机")
        self._preset_new_btn.clicked.connect(self._on_preset_new)
        _preset_layout.addWidget(self._preset_new_btn)
        # 管理电机预设按钮: 弹出 PresetManageDialog 查看/编辑/删除/导入导出
        self._preset_manage_btn = QPushButton("管理预设")
        self._preset_manage_btn.setToolTip(
            "管理电机预设: 查看/编辑/删除/复制/导入导出\n"
            "内置预设只读，用户预设可编辑")
        self._preset_manage_btn.clicked.connect(self._on_preset_manage)
        _preset_layout.addWidget(self._preset_manage_btn)
        # 重新标定按钮: 调用 0xEC 清除 is_calibrated + 编码器字段
        # 用于场景: 同一电机重做编码器标定（如零位漂移）
        self._motor_info_recalib_btn = QPushButton("重新标定")
        self._motor_info_recalib_btn.setToolTip(
            "清除标定状态（is_calibrated + 编码器字段），保留电机本体参数\n"
            "用于同一电机重做标定（如编码器零位漂移）\n"
            "流程: 清除 → 固化 → 重启 → 标定")
        self._motor_info_recalib_btn.clicked.connect(self._on_motor_info_recalib)
        _preset_layout.addWidget(self._motor_info_recalib_btn)
        _config_layout.addLayout(_preset_layout)
        _config_layout.addWidget(self._config_panel)
        self._motor_presets = self._load_motor_presets()
        _user_names = self._motor_presets.get("_user_names", set())
        for name in self._motor_presets.get("presets", {}).keys():
            display = f"[用户] {name}" if name in _user_names else name
            self._motor_preset_combo.addItem(display, name)
        self._motor_preset_apply_btn.setEnabled(False)
        # 标定结果面板: 复用 motor_config 通道(0xE6 读 / 0xE7 写 / 0xEA 固化),
        # 仅展示 MotorCalibParam 段(Index 16~42), 注入到标定 Tab 内嵌展示。
        # show_legend=False: 去掉图例行, 让表格直接占满
        self._calib_results_panel = ParamPanel(
            self._registry, title="标定结果", source="motor_config",
            groups=("MotorCalibParam",), show_save=True,
            save_text="保存标定结果到Flash/EEPROM", show_legend=False,
            show_bulk_rw=True)
        self._plot_panel = PlotPanel()
        self._state_panel = StateMachinePanel()
        self._calib_panel = CalibrationPanel()
        # PID 整定面板: 复用标定面板三段式布局, 0xA0~0xA6 命令收发 + ControlParam 读写
        self._pid_panel = PidPanel(self._registry)
        # PID 参数面板: 复用 motor_config 通道(0xE6/0xE7/0xEA), 仅展示 ControlParam 段
        self._pid_params_panel = ParamPanel(
            self._registry, title="PID 参数 (Flash)", source="motor_config",
            groups=("ControlParam",), show_save=True,
            save_text="保存PID参数到Flash", show_legend=False,
            show_bulk_rw=False, show_import_export=True)
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
        # 功能开关: 只控制是否显示 Tab, 面板实例始终创建(信号连接/转发不受影响)
        ff = feature_flags
        if ff.is_enabled("feedback_chart"):
            tabs.addTab(self._feedback_panel, "实时反馈")
        if ff.is_enabled("state_machine"):
            tabs.addTab(self._state_panel, "状态机")
        if ff.is_enabled("calibration"):
            tabs.addTab(self._calib_panel, "电机标定")
        if ff.is_enabled("pid_tuning"):
            tabs.addTab(self._pid_panel, "PID 整定")
        if ff.is_enabled("motor_params"):
            tabs.addTab(self._param_panel, "电机参数")
        if ff.is_enabled("motor_config"):
            tabs.addTab(self._config_tab_container, "电机配置")
        if ff.is_enabled("virtual_twin"):
            tabs.addTab(self._twin_param_panel, "孪生参数")
        if ff.is_enabled("fault_info"):
            tabs.addTab(self._fault_info_panel, "故障信息")
        if ff.is_enabled("waveform_plot"):
            tabs.addTab(self._plot_panel, "实时曲线")
        self._tabs = tabs
        # 虚拟电机模式默认关闭: 初始隐藏孪生参数 Tab (故障信息面板内部子表/屏蔽区自行隐藏)
        if ff.is_enabled("virtual_twin"):
            tabs.setTabVisible(tabs.indexOf(self._twin_param_panel), False)
        # A2: 切到标定 Tab 且已连接时, 自动读一次标定结果(MotorCalibParam 全段)
        tabs.currentChanged.connect(self._on_main_tab_changed)

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
        if feature_flags.is_enabled("log_panel"):
            self._right_splitter.addWidget(self._log_panel)
            self._right_splitter.setStretchFactor(0, 3)
            self._right_splitter.setStretchFactor(1, 2)
            self._right_splitter.setSizes([480, 300])
        else:
            self._right_splitter.setStretchFactor(0, 1)
            self._log_panel_visible = False
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
        btn.setStyleSheet(self._log_toggle_btn_style())
        btn.clicked.connect(self._toggle_log_panel)
        self._btn_toggle_log = btn
        self._update_log_toggle_button()
        # log_panel 关闭时隐藏切换按钮(日志面板已在 _build_ui 隐藏)
        if not feature_flags.is_enabled("log_panel"):
            btn.setVisible(False)
        return btn

    def _log_toggle_btn_style(self) -> str:
        """需求4: 日志开关按钮样式(随主题切换)."""
        return f"""
            QPushButton {{
                border: 1px solid {theme.hex('btn_border')};
                border-radius: 4px;
                padding: 0 8px;
                background: {theme.hex('btn_bg')};
                color: {theme.hex('btn_text')};
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: {theme.hex('btn_hover')};
                border-color: {theme.hex('muted')};
            }}
            QPushButton:checked {{
                background: {theme.hex('accent')};
                border-color: {theme.hex('accent')};
                color: {theme.hex('card_bottom')};
            }}
        """

    def _apply_menu_button_style(self, btn: QPushButton):
        # 颜色交由全局 QSS(theme.qss()) 统一管理, 这里只设交互属性与尺寸
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedHeight(26)
        btn.setMinimumWidth(88)
        btn.setStyleSheet("QPushButton { font-weight: bold; }")

    def _build_statusbar(self):
        """底部状态栏: 连接状态 + CAN 总线状态 + TX/RX 速率与累计总量 + 占用率"""
        sb = self.statusBar()

        def _mk(text):
            lbl = QLabel(text)
            lbl.setStyleSheet("font-family: Consolas, monospace; padding: 0 8px;")
            return lbl

        self._sb_link = _mk("● 未连接")
        self._sb_link.setStyleSheet(
            "font-family: Consolas; padding: 0 8px; color: #999;")
        # P1-1/P1-2: CAN 总线状态(ERROR_ACTIVE/PASSIVE/BUS_OFF + TEC/REC), 串口模式为空
        self._sb_bus_state = _mk("")
        self._sb_tx = _mk("TX 0 B/s  Σ0 B")
        self._sb_rx = _mk("RX 0 B/s  Σ0 B")
        self._sb_frames = _mk("帧 TX:0 RX:0")
        # P1-3: CAN 总线占用率估算, 串口模式为空
        self._sb_load = _mk("")

        for w in (self._sb_link, self._sb_bus_state, self._sb_tx,
                  self._sb_rx, self._sb_frames, self._sb_load):
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
            "transport_type": self._conn_panel.get_transport_type(),
            "can_bitrate": self._conn_panel.get_can_bitrate(),
            "can_motor_id": self._conn_panel.get_can_motor_id(),
            "can_channel_name": self._conn_panel.get_can_channel_name(),
            "display_period_ms": self._display_period_ms,
            "display_buffer_max": self._display_buffer_max,
            "main_window": {
                "width": int(self.width()),
                "height": int(self.height()),
                "x": int(self.x()),
                "y": int(self.y()),
            },
            "right_splitter": self._safe_sizes(self._right_splitter),
            "main_splitter": self._safe_sizes(self._main_splitter),
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
            # 参数表 CSV 路径(运行时换表后记住, 下次启动自动加载)
            "csv_paths": {
                "motor_config": self._registry.motor_config_csv_path,
                "motor_param": self._registry.params_csv_path,
            },
        }
        # 各面板配置 (容错: 单面板失败不影响整体保存; 被开关关闭的面板跳过)
        for key, panel in (
            ("plot", self._plot_panel),
            ("motion", self._motion_panel),
            ("motor_param", self._param_panel),
            ("motor_config", self._config_panel),
            ("calib_results", self._calib_results_panel),
            ("pid_params", self._pid_params_panel),
            ("calib", self._calib_panel),
            ("pid", self._pid_panel),
            ("twin_param", self._twin_param_panel),
            ("fault_info", self._fault_info_panel),
        ):
            try:
                s[key] = panel.get_opts()
            except Exception:
                pass
        # 调试窗口运行时配置 (只在窗口打开时收集)
        if self._debug_window is not None:
            try:
                dw_opts = self._debug_window.get_opts()
                if isinstance(dw_opts, dict):
                    s["plot"]["debug_window"].update(dw_opts)
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
        # 传输类型恢复 (先恢复类型, 再恢复对应配置项)
        tt = d.get("transport_type")
        if isinstance(tt, str) and tt:
            self._conn_panel.set_transport_type(tt)
        can_br = d.get("can_bitrate")
        if isinstance(can_br, str) and can_br:
            self._conn_panel.set_can_bitrate(can_br)
        can_mid = d.get("can_motor_id")
        if isinstance(can_mid, int):
            self._conn_panel.set_can_motor_id(can_mid)
        # CAN 通道名恢复: 启动时通道列表可能尚未刷新, 延迟套用
        can_ch = d.get("can_channel_name")
        if isinstance(can_ch, str) and can_ch:
            QTimer.singleShot(100, lambda: self._conn_panel.set_can_channel_name(can_ch))
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
        # 左右分栏 splitter 尺寸
        ms = d.get("main_splitter")
        if isinstance(ms, list) and len(ms) >= 2:
            try:
                self._main_splitter.setSizes([int(x) for x in ms])
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
        # 参数表 CSV 路径: 启动时按持久化路径重新加载 registry 并刷新依赖面板
        # (面板在 __init__._build_ui 已用默认 CSV 建好, 此处按自定义 CSV 重建;
        #  必须在下面"各面板配置"opts 循环之前执行, 以便 opts 循环恢复列宽)
        csv_paths = d.get("csv_paths")
        if isinstance(csv_paths, dict):
            reloaded_any = False
            mc = csv_paths.get("motor_config")
            if mc and Path(mc).is_file() and mc != self._registry.motor_config_csv_path:
                if self._registry.reload_motor_config(mc):
                    reloaded_any = True
            pi = csv_paths.get("motor_param")
            if pi and Path(pi).is_file() and pi != self._registry.params_csv_path:
                if self._registry.reload_params(pi):
                    reloaded_any = True
            if reloaded_any:
                for panel in (self._param_panel, self._config_panel,
                              self._calib_results_panel, self._pid_params_panel):
                    panel.reload()
        # 各面板配置 (容错: 单面板失败不影响其他; 被开关关闭的面板跳过)
        for key, panel in (
            ("plot", self._plot_panel),
            ("motion", self._motion_panel),
            ("motor_param", self._param_panel),
            ("motor_config", self._config_panel),
            ("calib_results", self._calib_results_panel),
            ("pid_params", self._pid_params_panel),
            ("calib", self._calib_panel),
            ("pid", self._pid_panel),
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
        # P1-3: CAN 模式下估算总线占用率; 串口模式清空 BUS/LOAD 标签
        if tp.name == "can":
            # 取样本周期内总线所有帧(RX+TX, 含发给其他节点的帧)累计位长并清零
            # 位长含帧头+位填充+帧间隔, 与 PCAN-View 等工具口径对齐
            bus_bits = tp.get_and_reset_bus_load_bits()
            bitrate = getattr(tp, 'bitrate', 1000000) or 1000000
            load_pct = bus_bits / (bitrate * dt) * 100 if bitrate > 0 else 0.0
            self._sb_load.setText(f"LOAD: {load_pct:.1f}%")
            color = ("#E53935" if load_pct > 70
                     else "#F9A825" if load_pct > 40
                     else theme.hex('muted'))
            self._sb_load.setStyleSheet(
                f"font-family: Consolas; padding: 0 8px; color: {color};")
        else:
            self._sb_load.setText("")
            self._sb_bus_state.setText("")

    def _on_bus_state_changed(self, state: str, tec: int, rec: int):
        """P1-1/P1-2: CAN 总线状态变化 → 状态栏 BUS 标签着色显示。

        state 取值: ERRORACTIVE / PASSIVE / BUSOFF (python-can BusState 枚举名)。
        """
        color_map = {"ERRORACTIVE": theme.hex('ok'),
                     "PASSIVE": "#F9A825",
                     "BUSOFF": "#E53935"}
        color = color_map.get(state, theme.hex('muted'))
        self._sb_bus_state.setText(f"BUS:{state} TEC:{tec} REC:{rec}")
        self._sb_bus_state.setStyleSheet(
            f"font-family: Consolas; padding: 0 8px; color: {color};")

    def _create_dev_info_group(self) -> QGroupBox:
        grp = QGroupBox("设备信息")
        layout = QGridLayout(grp)
        btn_info = QPushButton("读设备信息")
        btn_info.clicked.connect(lambda: self._client.query_dev_info())
        btn_name = QPushButton("读设备名称")
        btn_name.clicked.connect(lambda: self._client.query_dev_name())
        # 心跳: checkable, 按下后周期发送(100ms), 再按停止; 断开连接时自动停止
        self._btn_hb = QPushButton("心跳")
        self._btn_hb.setCheckable(True)
        self._btn_hb.setToolTip("点击开始周期发送心跳(10Hz), 再次点击停止")
        self._btn_hb.toggled.connect(self._on_hb_toggled)
        self._hb_timer = QTimer(self)
        self._hb_timer.setInterval(100)
        self._hb_timer.timeout.connect(lambda: self._client.heartbeat())
        layout.addWidget(btn_info, 0, 0)
        layout.addWidget(btn_name, 0, 1)
        layout.addWidget(self._btn_hb, 0, 2)
        return grp

    def _on_hb_toggled(self, on: bool):
        """心跳按钮切换: 开启周期发送 / 停止。未连接时拒绝开启。

        高亮(绿色)表示正在发送, 暗色(默认)表示已停止。
        """
        if on:
            if not self._client.is_open():
                self._btn_hb.setChecked(False)
                return
            self._client.heartbeat()       # 立即发一帧, 再启动周期
            self._hb_timer.start()
            self._btn_hb.setText("心跳●")
            self._btn_hb.setStyleSheet(
                "background-color: #4CAF50; color: white; font-weight: bold;")
        else:
            self._hb_timer.stop()
            self._btn_hb.setText("心跳")
            self._btn_hb.setStyleSheet("")   # 恢复默认暗色样式

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
        self._btn_layout_edit.setStyleSheet(self._layout_edit_btn_style())
        self._btn_layout_edit.toggled.connect(self._on_layout_edit_gate)
        layout.addWidget(self._btn_layout_edit, 1, 0)
        # 主题切换(深/浅), 记住上次选择
        self._btn_theme = QPushButton("主题: 深色" if theme.is_dark else "主题: 浅色")
        self._apply_menu_button_style(self._btn_theme)
        self._btn_theme.clicked.connect(self._on_toggle_theme)
        layout.addWidget(self._btn_theme, 1, 1)
        # 虚拟电机模式开关: 仅在选中虚拟引擎并打开串口后可用。
        # 开启后显示孪生参数 Tab、虚拟电机 Fault 位子表与故障屏蔽区。
        # virtual_twin 关闭时保留按钮但隐藏(避免大量引用点加守卫)
        self._btn_virtual_mode = QPushButton("虚拟电机: 关")
        self._btn_virtual_mode.setCheckable(True)
        self._btn_virtual_mode.setChecked(False)
        self._btn_virtual_mode.setEnabled(False)  # 未连接虚拟引擎前禁用
        self._btn_virtual_mode.setToolTip(
            "仅在连接虚拟引擎时可用。开启后显示孪生参数、虚拟电机 Fault 位表与故障屏蔽区。")
        self._apply_menu_button_style(self._btn_virtual_mode)
        self._btn_virtual_mode.setStyleSheet(self._virtual_mode_btn_style())
        self._btn_virtual_mode.toggled.connect(self._on_virtual_mode_toggled)
        layout.addWidget(self._btn_virtual_mode, 2, 0, 1, 2)
        if not feature_flags.is_enabled("virtual_twin"):
            self._btn_virtual_mode.setVisible(False)
        # 加载参数表 CSV: 运行时换 motor_info.csv / param_index.csv, 刷新全部参数表格
        btn_load_csv = QPushButton("加载参数表CSV")
        btn_load_csv.setToolTip(
            "加载新的 motor_info.csv 或 param_index.csv, "
            "电机配置/PID参数/标定结果/运行时参数表格将按新CSV重建(数据与格式均来自CSV)")
        btn_load_csv.clicked.connect(self._open_csv_load_dialog)
        self._apply_menu_button_style(btn_load_csv)
        layout.addWidget(btn_load_csv, 3, 0, 1, 2)
        # P2-2: CAN 手动发帧 — 调试工具, 直接发送原始 CAN 物理帧(不走协议组帧)
        # 仅 CAN 连接时可用, 由 _on_connected 切换 enabled
        self._btn_can_send = QPushButton("CAN 手动发帧")
        self._btn_can_send.setToolTip(
            "手动发送原始 CAN 物理帧 (调试用, 不走协议组帧)\n"
            "仅在 CAN 模式下可用\n"
            "快捷键: Ctrl+Shift+S")
        self._btn_can_send.setStatusTip("手动发送原始 CAN 物理帧 (仅 CAN 模式)")
        self._btn_can_send.clicked.connect(self._on_can_send)
        self._btn_can_send.setShortcut("Ctrl+Shift+S")
        self._btn_can_send.setEnabled(False)
        self._apply_menu_button_style(self._btn_can_send)
        layout.addWidget(self._btn_can_send, 4, 0, 1, 2)
        # 注: 标定操作历史开关已移到标定结果保存按钮同行最右 (CalibrationPanel 内部)
        return grp

    def _layout_edit_btn_style(self) -> str:
        """需求4: 布局编辑按钮样式(随主题切换, checked 态用 warn)."""
        return f"""
            QPushButton {{
                border: 1px solid {theme.hex('btn_border')};
                border-radius: 4px;
                padding: 0 8px;
                background: {theme.hex('btn_bg')};
                color: {theme.hex('btn_text')};
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: {theme.hex('btn_hover')};
                border-color: {theme.hex('muted')};
            }}
            QPushButton:checked {{
                background: {theme.hex('warn')};
                color: {theme.hex('card_bottom')};
                border-color: {theme.hex('warn')};
            }}
        """

    def _virtual_mode_btn_style(self) -> str:
        """需求4: 虚拟电机开关按钮样式(随主题切换, checked 态用 accent)."""
        return f"""
            QPushButton {{
                border: 1px solid {theme.hex('btn_border')};
                border-radius: 4px;
                padding: 0 8px;
                background: {theme.hex('btn_bg')};
                color: {theme.hex('btn_text')};
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: {theme.hex('btn_hover')};
                border-color: {theme.hex('muted')};
            }}
            QPushButton:disabled {{
                background: {theme.hex('card_bottom')};
                color: {theme.hex('muted')};
                border-color: {theme.hex('border')};
            }}
            QPushButton:checked {{
                background: {theme.hex('accent')};
                color: {theme.hex('card_bottom')};
                border-color: {theme.hex('accent')};
            }}
        """

    def _on_calib_history_toggled(self, on: bool):
        """标定操作历史显隐开关 (已移至标定结果保存按钮同行, 此处保留兼容)."""
        self._calib_panel.set_history_visible(on)

    def _on_toggle_theme(self):
        theme.set("light" if theme.is_dark else "dark")

    def _on_theme_changed(self, name: str):
        """主题变更: 重铺全局 QSS + 广播各面板 apply_theme + 持久化。"""
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.qss())
        self._btn_theme.setText("主题: 深色" if theme.is_dark else "主题: 浅色")
        # 需求4: 刷新浅色主题下颜色不对的按钮(此前硬编码不随主题切换)
        if hasattr(self, "_btn_toggle_log") and self._btn_toggle_log is not None:
            self._btn_toggle_log.setStyleSheet(self._log_toggle_btn_style())
        if hasattr(self, "_btn_layout_edit") and self._btn_layout_edit is not None:
            self._btn_layout_edit.setStyleSheet(self._layout_edit_btn_style())
        if hasattr(self, "_btn_virtual_mode") and self._btn_virtual_mode is not None:
            self._btn_virtual_mode.setStyleSheet(self._virtual_mode_btn_style())
        for panel in (self._feedback_panel, self._state_panel, self._plot_panel,
                      self._log_panel, self._param_panel, self._config_panel,
                      self._calib_panel, self._pid_panel, self._twin_param_panel,
                      self._fault_info_panel):
            fn = getattr(panel, "apply_theme", None)
            if callable(fn):
                fn()
        if self._debug_window is not None:
            self._debug_window.apply_theme()
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
        c.calib_status_received.connect(self._on_calib_status)
        c.dev_info_received.connect(self._on_dev_info)
        c.can_id_set_result.connect(self._on_can_id_set_result)
        c.can_node_discovered.connect(self._on_can_node_discovered)
        c.can_di_discovered.connect(self._on_can_di_discovered)
        c.can_di_set_result.connect(self._on_can_di_set_result)
        c.can_di_identify_result.connect(self._on_can_di_identify_result)
        c.dev_name_received.connect(self._on_dev_name)
        c.param_read_result.connect(self._on_param_result)
        c.motor_info_read_result.connect(self._on_param_result)
        c.pid_autotune_result_received.connect(self._on_pid_autotune_result)
        c.pid_source_received.connect(self._on_pid_source_received)
        c.pid_param_received.connect(self._on_pid_param_received)
        c.trace_config_received.connect(self._on_trace_config_received)
        c.trace_data_received.connect(self._on_trace_data)

        # 状态机面板: 周期请求 -> 拉取 READ_STATE
        self._state_panel.poll_state.connect(self._on_poll_state)
        self._feedback_panel.poll_state.connect(self._on_poll_state)

        # 面板 -> 客户端
        self._conn_panel.connect_requested.connect(self._on_connect)
        self._conn_panel.connect_can_requested.connect(self._on_connect_can)
        self._conn_panel.disconnect_requested.connect(self._on_disconnect)
        self._conn_panel.scan_can_requested.connect(self._start_can_scan)
        self._conn_panel.set_can_id_requested.connect(self._request_set_can_id)
        self._plot_panel.debug_window_toggled.connect(self._on_debug_window_toggled)
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
        # PID 面板: 专用信号直连 client (0xA0~0xA6 已有 struct.pack, 不走 registry 通用打包)
        self._pid_panel.pid_autotune_requested.connect(
            lambda r, c_bw, v_bw, p_bw: self._on_pid_autotune(r, c_bw, v_bw, p_bw))
        self._pid_panel.pid_source_set_requested.connect(
            lambda r, s: self._on_pid_source_set(r, s))
        self._pid_panel.pid_source_get_requested.connect(
            lambda: self._client.pid_source_get() if self._client.is_open() else None)
        self._pid_panel.pid_state_get_requested.connect(
            lambda: self._client.query_state() if self._client.is_open() else None)
        self._pid_panel.pid_param_set_requested.connect(
            lambda ring, ptype, val: self._on_pid_param_set(ring, ptype, val))
        self._pid_panel.pid_param_get_requested.connect(
            lambda ring, ptype: self._on_pid_param_get(ring, ptype))
        self._pid_panel.pid_tuning_mode_changed.connect(
            self._on_pid_tuning_mode_changed)
        self._pid_panel.pid_waveform_config_changed.connect(
            self._on_pid_waveform_config_changed)
        self._pid_panel.pid_debug_commit_requested.connect(
            self._on_pid_debug_commit)
        # PID 参数面板: 注入 PID Tab 内嵌, 复用 motor_config 通道(0xE6/0xE7/0xEA)
        self._pid_panel.attach_params_panel(self._pid_params_panel)
        self._pid_params_panel.read_param.connect(
            lambda param_id, panel=self._pid_params_panel:
                self._on_param_read(panel, param_id))
        self._pid_params_panel.write_param.connect(
            lambda param_id, text, panel=self._pid_params_panel:
                self._on_param_write(panel, param_id, text))
        self._pid_params_panel.read_params.connect(
            lambda param_ids, panel=self._pid_params_panel:
                self._on_param_read_many(panel, param_ids))
        self._pid_params_panel.write_params.connect(
            lambda writes, panel=self._pid_params_panel:
                self._on_param_write_many(panel, writes))
        self._pid_params_panel.save_all.connect(self._on_config_save)
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

        # P0-1/P1-1: CAN 专用信号(传输切换时需重连, 串口发射这些信号无害)
        self._reconnect_can_signals()
        # P2-1/P2-4: connection_panel CAN 增强信号
        self._conn_panel.bitrate_changed.connect(self._on_can_bitrate_changed)
        self._conn_panel.listen_only_changed.connect(self._on_listen_only_changed)
        # 危险命令二次确认: 注入回调, _send 命中 DANGER_CMDS 时同步询问用户
        c.set_confirm_callback(self._on_confirm_danger_cmd)

    def _reconnect_can_signals(self):
        """传输切换后重连 CAN 专用信号(raw_can_frame / bus_state_changed)。

        SerialTransport 不发射这两个信号, 连接无害; CanTransport 发射时由
        log_panel / _on_bus_state_changed 接收。set_transport 只重连 3 个核心
        信号, 新增的 CAN 信号在这里统一处理, 避免切换后日志丢失。
        """
        tp = self._client.transport
        try:
            tp.raw_can_frame.disconnect()
        except TypeError:
            pass  # 未连接任何槽
        try:
            tp.bus_state_changed.disconnect()
        except TypeError:
            pass
        tp.raw_can_frame.connect(self._log_panel.log_can_frame)
        tp.bus_state_changed.connect(self._on_bus_state_changed)

    def _switch_transport(self, transport, desc: str):
        """切换传输通道并记录主控切换日志。
        封装 set_transport + _reconnect_can_signals + 日志记录, 避免3处连接入口重复代码。"""
        self._client.set_transport(transport)
        self._reconnect_can_signals()
        self._log_panel.log(f"[主控] 通道切换: → {desc}")

    # ==================== 危险命令二次确认 ====================
    def _on_confirm_danger_cmd(self, cmd: int, payload: bytes) -> bool:
        """危险命令发送前 UI 二次确认。由 JmClient._send 在命中 DANGER_CMDS 时同步调用, 返回 True=确认, False=取消。"""
        desc = DANGER_CMDS.get(cmd, f"cmd=0x{cmd:02X}")
        name = cmd_name(cmd)  # 未知命令返回 "0xXX", 永远非空
        # 载荷 hex 截断显示, 避免长载荷撑爆对话框
        payload_hex = payload.hex(' ').upper() if payload else "(无)"
        if len(payload_hex) > 96:
            payload_hex = payload_hex[:96] + " ..."
        # 风险等级与额外提示
        if cmd in (JmCmd.FACTORY_RESET, JmCmd.MOTOR_INFO_RESET):
            risk = "极高"
            extra = "此操作将清除所有用户配置和标定数据, 不可恢复!"
        elif cmd in (JmCmd.PARAM_SAVE, JmCmd.MOTOR_INFO_SAVE):
            risk = "高"
            extra = "此操作将写入 Flash, 频繁擦写会降低 Flash 寿命。"
        elif cmd in (JmCmd.CALIB_LEVEL1, JmCmd.CALIB_LEVEL2, JmCmd.CALIB_LEVEL3,
                     JmCmd.CALIB_LEVEL4, JmCmd.CALIB_LEVEL5, JmCmd.CALIB_LEVEL6,
                     JmCmd.CALIB_LEVEL7):
            risk = "高"
            extra = "标定过程电机会运动并通大电流, 请确保机械结构安全且无阻挡。"
        elif cmd == JmCmd.CLEAR_FAULT:
            risk = "中"
            extra = "清除故障后若根因未排除, 故障会再次触发。"
        elif cmd == JmCmd.MOTOR_INFO_RECALIB_RESET:
            risk = "中"
            extra = "清除标定状态后需重新标定编码器零点, 电机才能正常工作。"
        else:
            risk = "中"
            extra = "此操作可能修改设备配置, 请确认无误后继续。"

        title = f"危险操作确认 - {name}"
        text = (
            f"<b>即将执行危险操作</b><br><br>"
            f"<b>命令:</b> {name} ({desc})<br>"
            f"<b>风险等级:</b> <span style='color:#E53935'><b>{risk}</b></span><br>"
            f"<b>载荷(hex):</b> <code>{payload_hex}</code><br><br>"
            f"<span style='color:#FF9800'>{extra}</span><br><br>"
            f"确认要继续执行吗?"
        )
        btn = QMessageBox.warning(
            self, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel  # 默认聚焦取消, 防误触
        )
        return btn == QMessageBox.StandardButton.Yes

    # ==================== CAN 调试槽 (P2-1/P2-2/P2-4) ====================
    def _on_can_send(self):
        """P2-2: 打开 CAN 手动发帧对话框, 用户确认后调用 transport.send_raw。"""
        if self._client.transport.name != "can" or not self._client.is_open():
            QMessageBox.information(self, "提示", "请先连接 CAN 设备")
            return
        from ui.panels.can_send_dialog import CanSendDialog
        dlg = CanSendDialog(self)
        dlg.send_requested.connect(
            lambda cid, data, ext: self._client.transport.send_raw(cid, data, ext))
        dlg.exec()

    def _on_can_bitrate_changed(self, bitrate: int):
        """P2-4: CAN 已连接时改比特率 → 断开并以新比特率重连。

        重连期间正在传输的帧会丢失(调试场景可接受)。
        通道/电机 ID 沿用连接面板当前值。
        """
        if self._client.transport.name != "can" or not self._client.is_open():
            return
        channel = self._conn_panel.get_can_channel_name()
        motor_id = self._conn_panel.get_can_motor_id()
        self._log_panel.log(f"[CAN] 比特率热切换: {bitrate}, 重连中...")
        self._client.close()
        if self._client.open(channel=channel, bitrate=bitrate, motor_id=motor_id):
            self.statusBar().showMessage(f"已重连 {channel} @{bitrate}", 2000)
        else:
            self._log_panel.log_warn("[CAN] 比特率热切换重连失败")

    def _on_listen_only_changed(self, enabled: bool):
        """P2-1: 监听模式开关 — 只记 CAN 物理帧日志, 不解析协议帧。"""
        if self._client.transport.name == "can":
            self._client.transport.set_listen_only(enabled)
            mode = "开启" if enabled else "关闭"
            self.statusBar().showMessage(f"CAN 监听模式已{mode}", 2000)

    def _start_can_scan(self):
        """通过CAN-DI广播执行多轮分时扫描。"""
        if self._can_scan_active:
            return
        if self._can_set_pending_key is not None:
            QMessageBox.information(self, "CAN-DI扫描", "设备正在切换ID，请等待确认完成")
            return
        if not self._client.begin_can_scan():
            QMessageBox.information(self, "节点扫描", "请先连接 CAN 总线")
            return
        self._can_commissioning_required = True
        self._show_can_device_dialog()
        self._can_scan_active = True
        self._can_scan_round = 0
        self._can_scan_current_round = 0
        self._can_scan_results.clear()
        self._conn_panel.set_can_scan_active(True)
        self._can_device_dialog.set_scan_active(True)
        self._refresh_can_device_dialog()
        self._log_panel.log("[CAN-DI] 开始3轮广播扫描")
        self._send_can_di_round()

    def _send_can_di_round(self):
        if not self._can_scan_active or not self._client.is_open():
            self._cancel_can_scan()
            return
        if self._can_scan_round >= self._can_scan_total_rounds:
            self._finish_can_scan()
            return
        self._can_scan_current_round = (self._can_scan_round + 1) & 0xFF
        nonce = secrets.randbits(32)
        if not self._client.discover_can_di(
                self._can_scan_current_round, self._can_scan_slot_exp,
                self._can_scan_slot_ms, nonce):
            self._cancel_can_scan()
            return
        self._can_scan_round += 1
        window_ms = (1 << self._can_scan_slot_exp) * self._can_scan_slot_ms + 50
        self._can_scan_timer.start(window_ms)
        self._log_panel.log(
            f"[CAN-DI] 扫描轮次 {self._can_scan_round}/{self._can_scan_total_rounds}")

    def _advance_can_di_scan(self):
        self._send_can_di_round()

    def _cancel_can_scan(self):
        self._can_scan_timer.stop()
        self._can_scan_active = False
        self._conn_panel.set_can_scan_active(False)
        if self._can_device_dialog is not None:
            self._can_device_dialog.set_scan_active(False)

    def _finish_can_scan(self):
        if not self._can_scan_active:
            return
        self._cancel_can_scan()
        self._refresh_can_device_dialog()
        ids = sorted(rec["current_id"] for rec in self._can_scan_results.values())
        self._log_panel.log(
            f"[CAN-DI] 扫描完成，发现 {len(ids)} 台设备，节点ID={ids}")
        if not ids:
            QMessageBox.information(
                self, "CAN-DI扫描",
                "未收到CAN-DI响应。设备固件可能尚不支持0xF4发现命令。")
            return

        target_id = None
        if self._last_connect_params and self._last_connect_params.get("type") == "can":
            target_id = int(self._last_connect_params.get("motor_id", 0))
        target_count = sum(1 for rec in self._can_scan_results.values()
                           if rec["current_id"] == target_id)
        if target_count == 1:
            self._can_commissioning_required = True
            self._log_panel.log(
                f"[CAN-DI] 当前 ID={target_id} 唯一，请点击设备行的“选中”进入控制")
        elif target_count > 1:
            self._can_commissioning_required = True
            self._log_panel.log_warn(
                f"[CAN-DI] 当前 ID={target_id} 存在 {target_count} 台设备，"
                "已暂停普通查询和控制，请先完成改址")
        else:
            self._can_commissioning_required = True
            self._log_panel.log_warn(
                "[CAN-DI] 当前目标未发现，请在设备管理窗口选择一个唯一 ID")

    def _show_can_device_dialog(self):
        if self._can_device_dialog is None:
            dlg = CanDeviceManagerDialog(self)
            dlg.scan_requested.connect(self._start_can_scan)
            dlg.identify_requested.connect(self._on_can_di_identify_requested)
            dlg.set_id_requested.connect(self._on_can_di_set_requested)
            dlg.batch_set_requested.connect(self._on_can_di_batch_requested)
            dlg.select_requested.connect(self._select_can_di_node)
            dlg.session_closed.connect(self._on_can_di_session_closed)
            self._can_device_dialog = dlg
        self._can_device_dialog.show()
        self._can_device_dialog.raise_()
        self._can_device_dialog.activateWindow()

    def _refresh_can_device_dialog(self):
        if self._can_device_dialog is None:
            return
        records = sorted(self._can_scan_results.values(),
                         key=lambda rec: (rec["current_id"], rec["key"]))
        self._can_device_dialog.set_records(records, self._can_scan_total_rounds)

    def _on_can_di_discovered(self, node_id: int, di56: bytes, guard: int):
        if not self._can_scan_active:
            return
        key = bytes(di56) + bytes([guard & 0xFF])
        rec = self._can_scan_results.get(key)
        if rec is None:
            rec = {
                "key": key, "di56": bytes(di56), "guard": guard & 0xFF,
                "current_id": node_id, "seen_rounds": set(),
                "desired_id": None, "state": "在线",
            }
            self._can_scan_results[key] = rec
        rec["current_id"] = node_id
        rec["seen_rounds"].add(self._can_scan_current_round)
        if rec.get("state") not in ("写入中", "切换中"):
            rec["state"] = "在线"
        self._refresh_can_device_dialog()

    def _record_for_di(self, di56: bytes):
        matches = [rec for rec in self._can_scan_results.values()
                   if rec["di56"] == bytes(di56)]
        return matches[0] if len(matches) == 1 else None

    def _validate_can_di_changes(self, changes: list[tuple[bytes, int]]) -> list:
        occupied = {rec["current_id"] for rec in self._can_scan_results.values()}
        targets = set()
        queue_items = []
        for di56, new_id in changes:
            rec = self._record_for_di(di56)
            if rec is None:
                QMessageBox.warning(self, "设置CAN ID", "CAN-DI不存在或发生压缩标识冲突")
                return []
            if new_id == rec["current_id"]:
                continue
            if new_id in occupied or new_id in targets:
                QMessageBox.warning(self, "设置CAN ID", f"目标 ID {new_id} 已被占用")
                return []
            targets.add(new_id)
            queue_items.append((rec["key"], int(new_id)))
        return queue_items

    def _on_can_di_set_requested(self, di56: bytes, new_id: int):
        items = self._validate_can_di_changes([(bytes(di56), int(new_id))])
        if items:
            self._start_can_di_set_queue(items)

    def _on_can_di_batch_requested(self, changes: list):
        items = self._validate_can_di_changes(
            [(bytes(di), int(new_id)) for di, new_id in changes])
        if items:
            self._start_can_di_set_queue(items)

    def _start_can_di_set_queue(self, items: list):
        if self._can_set_pending_key is not None:
            QMessageBox.information(self, "设置CAN ID", "已有设备正在切换，请等待完成")
            return
        self._can_set_queue = deque(items)
        self._process_next_can_di_set()

    def _process_next_can_di_set(self):
        if not self._can_set_queue:
            self._can_set_pending_key = None
            self._can_set_pending_new_id = None
            self._log_panel.log("[CAN-DI] 顺序设置完成，开始重新扫描验证")
            QTimer.singleShot(100, self._start_can_scan)
            return
        key, new_id = self._can_set_queue.popleft()
        rec = self._can_scan_results.get(key)
        if rec is None:
            self._process_next_can_di_set()
            return
        self._can_set_pending_key = key
        self._can_set_pending_new_id = new_id
        self._can_set_generation += 1
        generation = self._can_set_generation
        rec["desired_id"] = new_id
        rec["state"] = "写入中"
        self._refresh_can_device_dialog()
        if not self._client.set_can_id_by_di(rec["di56"], new_id):
            rec["state"] = "发送失败"
            self._can_set_pending_key = None
            self._can_set_pending_new_id = None
            self._can_set_queue.clear()
            self._refresh_can_device_dialog()
            return
        QTimer.singleShot(1500, lambda g=generation: self._on_can_di_set_timeout(g))

    def _on_can_di_set_result(self, node_id: int, di56: bytes, status: int):
        rec = self._record_for_di(di56)
        if rec is None or rec["key"] != self._can_set_pending_key:
            return
        if status != 0:
            rec["state"] = f"失败 0x{status:02X}"
            self._log_panel.log_warn(
                f"[CAN-DI] {bytes(di56).hex().upper()} 设置失败: {err_name(status)}")
            self._can_set_queue.clear()
            self._can_set_pending_key = None
            self._can_set_pending_new_id = None
            self._refresh_can_device_dialog()
            return

        if node_id != self._can_set_pending_new_id:
            rec["state"] = "切换中"
            self._refresh_can_device_dialog()
            return

        rec["current_id"] = node_id
        rec["desired_id"] = node_id
        rec["state"] = "验证成功"
        self._log_panel.log(
            f"[CAN-DI] {bytes(di56).hex().upper()} 已切换到 ID={node_id}")
        self._can_set_pending_key = None
        self._can_set_pending_new_id = None
        self._refresh_can_device_dialog()
        QTimer.singleShot(50, self._process_next_can_di_set)

    def _on_can_di_set_timeout(self, generation: int):
        if generation != self._can_set_generation or self._can_set_pending_key is None:
            return
        rec = self._can_scan_results.get(self._can_set_pending_key)
        if rec is not None:
            rec["state"] = "确认超时"
        self._can_set_queue.clear()
        self._can_set_pending_key = None
        self._can_set_pending_new_id = None
        self._refresh_can_device_dialog()
        self._log_panel.log_warn("[CAN-DI] 新ID确认超时，请重新扫描确认设备实际地址")

    def _on_can_di_identify_requested(self, di56: bytes, duration_100ms: int):
        if not self._client.identify_can_device(di56, duration_100ms):
            QMessageBox.warning(self, "识别设备", "识别命令发送失败")

    def _on_can_di_identify_result(self, node_id: int, di56: bytes, status: int):
        rec = self._record_for_di(di56)
        if rec is not None:
            rec["state"] = "正在指示" if status == 0 else f"识别失败 0x{status:02X}"
            self._refresh_can_device_dialog()

    def _select_can_di_node(self, node_id: int):
        matching = [rec for rec in self._can_scan_results.values()
                    if rec["current_id"] == int(node_id)]
        if len(matching) != 1:
            self._can_commissioning_required = True
            QMessageBox.warning(
                self, "选择CAN设备",
                f"CAN ID={node_id} 不是唯一设备，不能进入普通控制。请先完成改址。")
            return
        if self._client.select_can_node(node_id):
            self._can_commissioning_required = False
            self._conn_panel.set_can_motor_id(node_id)
            if self._last_connect_params and self._last_connect_params.get("type") == "can":
                self._last_connect_params["motor_id"] = node_id
            self.statusBar().showMessage(f"当前 CAN 节点已切换为 ID={node_id}", 3000)
            self._log_panel.log(f"[CAN-DI] 已选中唯一设备 ID={node_id}，主窗口控制已启用")
            if self._can_device_dialog is not None:
                self._can_device_dialog.close()

    def _on_can_di_session_closed(self):
        if self._can_scan_active:
            self._cancel_can_scan()
        if self._can_set_pending_key is None:
            self._client.end_can_scan()
        if (self._client.transport.name == "can" and self._client.is_open()
                and self._can_commissioning_required):
            self._log_panel.log_warn("[CAN-DI] 未选中唯一设备，已断开CAN连接")
            self.statusBar().showMessage("未选中唯一CAN设备，已断开CAN连接", 3000)
            self._user_disconnected = True
            self._reconnect_timer.stop()
            self._client.close()

    def _on_can_node_discovered(self, node_id: int, hw: int, fw: int,
                                uid: bytes, reported_id: int):
        if self._can_scan_active:
            return
        if self._verify_can_id is not None:
            expected = self._verify_can_id
            self._verify_can_id = None
            if node_id == expected and reported_id == expected:
                self._log_panel.log(f"[CAN] 重启后节点 ID 校验通过: {node_id}")
            else:
                self._log_panel.log_warn(
                    f"[CAN] 节点 ID 校验失败: 期望 {expected}, "
                    f"仲裁ID {node_id}, 设备报告 {reported_id}")

    def _request_set_can_id(self):
        if self._client.transport.name != "can" or not self._client.is_open():
            QMessageBox.information(self, "设置节点 ID", "请先连接目标 CAN 节点")
            return
        if self._can_scan_results:
            self._show_can_device_dialog()
        else:
            self._start_can_scan()

    def _on_can_id_set_result(self, new_id: int, restart_required: bool):
        self._pending_can_id = None
        self._verify_can_id = new_id
        self._conn_panel.set_can_motor_id(new_id)
        if self._last_connect_params and self._last_connect_params.get("type") == "can":
            self._last_connect_params["motor_id"] = new_id
        self._log_panel.log(
            f"[CAN] 节点 ID={new_id} 已写入 Flash，"
            f"{'重启后生效' if restart_required else '已生效'}")
        QMessageBox.information(
            self, "设置节点 ID",
            f"节点 ID {new_id} 已保存到设备。\n"
            f"{'请重启设备后使用新 ID 重连。' if restart_required else '新 ID 已生效。'}")

    # ==================== 连接管理 ====================
    def _on_connect(self, port: str, baud: int):
        # 选择虚拟数据引擎: 切换到 VirtualTransport, 无需真实串口
        if port == "VIRTUAL":
            if not isinstance(self._client.transport, VirtualTransport):
                self._switch_transport(VirtualTransport(), "虚拟数据引擎(SIM)")
            if self._client.open():
                self.statusBar().showMessage("已连接 虚拟数据引擎 (演示)")
                self._cur_port = "VIRTUAL"
                # 虚拟引擎不自动重连(本地仿真不会异常断开), 仅标记类型
                self._last_connect_params = {"type": "virtual"}
                self._user_disconnected = False
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
        # 真实串口: 若当前是虚拟/CAN 传输, 换回串口传输
        if self._client.transport.name != "serial":
            self._switch_transport(SerialTransport(), "串口(UART)")
        if self._client.open(port=port, baudrate=baud):
            self.statusBar().showMessage(f"已连接 {port} @{baud}")
            self._cur_port = port
            # 记录连接参数供自动重连使用
            self._last_connect_params = {"type": "serial", "port": port, "baud": baud}
            self._user_disconnected = False
            # 连接真机: 虚拟电机模式开关必须关闭并禁用
            self._force_disable_virtual_mode()

    def _on_connect_can(self, channel: str, bitrate: int, motor_id: int):
        """CAN 连接: 懒导入 CanTransport, 热切换后打开链路。"""
        try:
            from transport.can_transport import CanTransport
        except ImportError:
            QMessageBox.warning(self, "提示", "python-can 未安装, 无法使用 CAN 传输")
            return
        if self._client.transport.name != "can":
            self._switch_transport(CanTransport(), "CAN 总线")
        if self._client.open(channel=channel, bitrate=bitrate, motor_id=motor_id):
            self.statusBar().showMessage(f"已连接 {channel} @{bitrate} ID={motor_id}")
            self._cur_port = channel
            # 记录连接参数供自动重连使用
            self._last_connect_params = {
                "type": "can", "channel": channel,
                "bitrate": bitrate, "motor_id": motor_id}
            self._user_disconnected = False
            # CAN 连接真机: 虚拟电机模式开关必须关闭并禁用
            self._force_disable_virtual_mode()
            if self._verify_can_id == motor_id:
                QTimer.singleShot(150, self._client.query_dev_info)

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
        # 标记用户主动断开, 阻止 _on_connected(False) 启动自动重连
        self._user_disconnected = True
        self._reconnect_timer.stop()
        self._reconnect_attempts = 0
        if self._can_scan_active:
            self._cancel_can_scan()
        # 断开前停止调试采样和周期遥测，避免下位机继续产生无消费者数据。
        self._stop_trace()
        self._pid_panel.close_waveform_window()
        if self._client.is_open():
            self._client.set_telemetry(False, 0, 0)
        self._telemetry_panel.set_running(False)
        self._feedback_pending.clear()
        self._latest_state = None
        self._pending_param_reads.clear()
        self._pending_param_writes.clear()
        self._param_read_queue.clear()
        self._param_write_queue.clear()
        self._pid_theory_pending = None
        self._cmd_watchdog.stop()
        # 清除各 ParamPanel 内部待确认写状态, 防止"等待返回"高亮卡死
        for pnl in (self._param_panel, self._config_panel,
                    self._calib_results_panel, self._pid_params_panel):
            pnl.reset_write_state()
        self._client.close()
        self._twin_param_panel.set_engine(None)
        self._fault_info_panel.set_engine(None)
        # 断开连接: 强制关闭并禁用虚拟电机模式开关
        self._force_disable_virtual_mode()
        # 关闭调试窗口
        if self._debug_window is not None:
            self._debug_window.close()
            self._debug_window = None
        self._plot_panel.set_debug_checkbox(False)
        self.statusBar().showMessage("已断开")

    def _on_debug_window_toggled(self, checked: bool):
        """PlotPanel 调试通道复选框变化 → 创建/关闭调试窗口。"""
        if checked:
            if self._debug_window is None:
                self._debug_window = DebugPlotWindow()
                self._debug_window.window_closed.connect(self._on_debug_window_closed)
                # 恢复上次窗口大小/位置/通道名称 (从持久化配置)
                settings = layout_store.get_meta("settings", {})
                plot_opts = settings.get("plot", {})
                dw_opts = plot_opts.get("debug_window", {})
                if isinstance(dw_opts, dict):
                    self._debug_window.set_opts(dw_opts)
            self._debug_window.show()
        else:
            if self._debug_window is not None:
                self._debug_window.close()
                self._debug_window = None

    def _on_debug_window_closed(self):
        """用户手动关闭调试窗口 → 同步取消 PlotPanel 复选框。"""
        self._plot_panel.set_debug_checkbox(False)
        self._debug_window = None

    def _on_connected(self, connected: bool):
        self._conn_panel.set_connected(connected)
        self._feedback_panel.set_link_active(connected)
        self._state_panel.set_link_active(connected)
        self._calib_panel.set_link_active(connected)
        self._pid_panel.set_link_active(connected)
        self._link_connected = connected
        is_can = (self._client.transport.name == "can")
        if not connected:
            self._pid_debug_heartbeat_timer.stop()
            self._stop_trace(send_command=False)
            self._can_commissioning_required = False
            if self._can_scan_active:
                self._cancel_can_scan()
            self._telemetry_panel.set_running(False)
            # P2-5: 断开时隐藏 CAN 提示; P1-1/P1-3: 清空状态栏 CAN 标签
            self._feedback_panel.set_can_mode(False)
            self._sb_bus_state.setText("")
            self._sb_load.setText("")
            # 断开时停止周期心跳, 避免向已关闭的链路发帧
            if self._btn_hb.isChecked():
                self._btn_hb.setChecked(False)
            # 自动重连: 非用户主动断开 + 有上次连接参数 → 启动重连定时器
            # 虚拟引擎不自动重连(本地仿真不会异常断开, 重连无意义)
            if (not self._user_disconnected
                    and self._last_connect_params is not None
                    and self._last_connect_params.get("type") != "virtual"
                    and not self._reconnect_timer.isActive()):
                self._reconnect_attempts = 0
                self._reconnect_timer.start()
                self._log_panel.log(
                    f"[重连] 链路异常断开, 启动自动重连 "
                    f"(最多 {self._reconnect_max_attempts} 次, 间隔 2s)")
        else:
            # 重连成功: 停止定时器, 重置计数
            if self._reconnect_timer.isActive():
                self._reconnect_timer.stop()
                self._log_panel.log("[重连] 自动重连成功")
            self._reconnect_attempts = 0
            self._user_disconnected = False
            # CAN总线上可能存在重复节点ID。必须先做CAN-DI发现，确认当前ID唯一后
            # 才能发送普通查询；否则多个节点会把彼此的同CMD响应误当成新请求。
            if is_can:
                self._can_commissioning_required = True
                QTimer.singleShot(50, self._start_can_scan)
            else:
                try:
                    self._client.query_state()
                    self._client.pid_source_get()
                except Exception:
                    pass
            # P0-7: CAN 模式看门狗策略 — 保守延长到 5s
            # (固件 CAN 侧是否回应用层 ACK 待联调确认; 串口维持 1s)
            self._cmd_watchdog.setInterval(5000 if is_can else 1000)
            # P2-5: 反馈面板 CAN 模式提示
            self._feedback_panel.set_can_mode(is_can)
        # P2-2: CAN 手动发帧按钮仅在 CAN 连接时可用
        self._btn_can_send.setEnabled(connected and is_can)
        self._refresh_statusbar_theme()

    # ==================== 自动重连 ====================
    def _on_reconnect_tick(self):
        """自动重连定时器槽: 用上次连接参数尝试重连。失败计数 +1, 超过最大次数则放弃。"""
        if self._last_connect_params is None:
            self._reconnect_timer.stop()
            return
        if self._client.is_open():
            # 已连接(可能用户手动重连成功), 停止定时器
            self._reconnect_timer.stop()
            self._reconnect_attempts = 0
            return
        self._reconnect_attempts += 1
        if self._reconnect_attempts > self._reconnect_max_attempts:
            self._reconnect_timer.stop()
            self._log_panel.log_err(
                f"[重连] 已尝试 {self._reconnect_max_attempts} 次仍失败, "
                f"停止自动重连, 请手动检查设备后重连")
            self.statusBar().showMessage("自动重连失败, 请手动重连")
            return
        p = self._last_connect_params
        try:
            if p["type"] == "serial":
                ok = self._client.open(port=p["port"], baudrate=p["baud"])
            elif p["type"] == "can":
                ok = self._client.open(channel=p["channel"], bitrate=p["bitrate"],
                                       motor_id=p["motor_id"])
            else:
                ok = False
        except Exception as e:
            ok = False
            self._log_panel.log_warn(f"[重连] 第 {self._reconnect_attempts} 次异常: {e}")
        if ok:
            # 成功后 _on_connected(True) 会处理后续逻辑并停止定时器
            self._log_panel.log(f"[重连] 第 {self._reconnect_attempts} 次重连成功")
        else:
            self.statusBar().showMessage(
                f"重连中... ({self._reconnect_attempts}/{self._reconnect_max_attempts})")

    def _refresh_statusbar_theme(self):
        """按连接状态 + 当前主题重设状态栏连接标签颜色, 前缀显示通道类型(UART/CAN/SIM)。"""
        connected = getattr(self, "_link_connected", False)
        if connected:
            # 通道类型前缀: serial→UART, can→CAN, virtual→SIM
            tp_name = self._client.transport.name
            if tp_name == "can":
                ch_prefix = "[CAN] "
            elif tp_name == "virtual":
                ch_prefix = "[SIM] "
            else:
                ch_prefix = "[UART] "
            self._sb_link.setText(f"● {ch_prefix}{getattr(self, '_cur_port', '')}")
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
        if (self._client.transport.name == "can"
                and self._can_commissioning_required):
            QMessageBox.information(
                self, "CAN设备未确认",
                "请先在CAN设备管理窗口完成扫描，并选择一个无ID冲突的设备")
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
        if (self._client.transport.name == "can"
                and self._can_commissioning_required):
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
            ok = self._client.send_command(cmd, values)
            if ok:
                self._track_pid_command_targets(cmd, values)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"发送异常: {e}")

    def _track_pid_command_targets(self, cmd: int, values: dict):
        """记录最近一次运动指令中的 PID 相关目标值, 供 PID 页观测显示。"""
        if not isinstance(values, dict):
            return
        aliases = {
            "id": ("id_ref", "id", "id_weak"),
            "iq": ("iq_ref", "iq"),
            "vel": ("vel_ref", "vel", "vel_ff"),
            "pos": ("pos_ref", "pos"),
        }
        changed = False
        for target_key, names in aliases.items():
            for name in names:
                if name in values:
                    try:
                        self._pid_last_targets[target_key] = float(values[name])
                    except (TypeError, ValueError):
                        self._pid_last_targets[target_key] = values[name]
                    changed = True
                    break
        if changed:
            self._pid_panel.set_pid_targets(self._pid_last_targets)

    def _pid_required_telemetry_mask(self) -> int:
        """返回当前调试环真正需要的遥测分组。"""
        mode = self._pid_panel.current_tuning_mode()
        return get_pid_debug_profile(mode).telemetry_mask

    def _pid_telemetry_period(self) -> int:
        try:
            base = int(self._pid_telemetry_snapshot.get("period_ms", 20)
                       if self._pid_telemetry_snapshot else self._telemetry_panel.get_period())
        except Exception:
            base = 20
        profile = get_pid_debug_profile(self._pid_panel.current_tuning_mode())
        return max(1, min(base, profile.telemetry_max_period_ms))

    def _activate_pid_telemetry(self):
        """进入 PID 页时临时接管遥测配置, 退出后恢复用户原设置。"""
        if not self._client.is_open():
            return
        if not self._pid_telemetry_active:
            try:
                self._pid_telemetry_snapshot = self._telemetry_panel.snapshot_config()
            except Exception:
                self._pid_telemetry_snapshot = {
                    "enable": False,
                    "mask": self._telemetry_panel.current_mask(),
                    "period_ms": self._telemetry_panel.get_period(),
                }
            self._pid_telemetry_active = True
        self._apply_pid_telemetry_for_mode()

    def _apply_pid_telemetry_for_mode(self):
        if not self._pid_telemetry_active or not self._client.is_open():
            return
        mask = int(self._pid_required_telemetry_mask())
        period_ms = self._pid_telemetry_period()
        self._telemetry_panel.apply_config(mask, period_ms, force=True)
        self._telemetry_panel.set_running(True)
        self._client.set_telemetry(True, mask, period_ms)
        mode = self._pid_panel.current_tuning_mode()
        self._log_panel.log(
            f"[PID] 调试页接管遥测: mode={mode} mask=0x{mask:04X} 周期={period_ms}ms")

    def _restore_pid_telemetry(self):
        """离开 PID 页时恢复进入前的遥测配置。"""
        if not self._pid_telemetry_active:
            return
        snap = self._pid_telemetry_snapshot or {"enable": False, "mask": 0, "period_ms": 20}
        self._pid_telemetry_active = False
        self._pid_telemetry_snapshot = None
        enable = bool(snap.get("enable", False))
        mask = int(snap.get("mask", 0))
        period_ms = int(snap.get("period_ms", 20) or 20)
        self._telemetry_panel.apply_config(mask, period_ms, force=True)
        self._telemetry_panel.set_running(enable)
        if self._client.is_open():
            self._client.set_telemetry(enable, mask if enable else 0, period_ms if enable else 0)
            if enable:
                self._log_panel.log(
                    f"[PID] 恢复进入前遥测: mask=0x{mask:04X} 周期={period_ms}ms")
            else:
                self._log_panel.log("[PID] 恢复进入前遥测: 停止")

    def _on_pid_tuning_mode_changed(self, _mode: str):
        # 整定模式只决定遥测观测组; 波形采集由波形窗口自己的数据源决定。
        if self._active_main_tab_widget is self._pid_panel:
            self._apply_pid_telemetry_for_mode()

    def _send_pid_debug_heartbeat(self):
        if (self._active_main_tab_widget is self._pid_panel and
                self._client.is_open() and
                self._pid_panel.has_any_debug_source()):
            self._client.pid_source_get()
        else:
            self._pid_debug_heartbeat_timer.stop()

    def _sync_pid_debug_heartbeat(self):
        active = (self._active_main_tab_widget is self._pid_panel and
                  self._client.is_open() and
                  self._pid_panel.has_any_debug_source())
        if active:
            if not self._pid_debug_heartbeat_timer.isActive():
                self._pid_debug_heartbeat_timer.start()
        else:
            self._pid_debug_heartbeat_timer.stop()

    def _on_pid_waveform_config_changed(self, enabled: bool, _rate_hz: int):
        """应用波形窗口的 TRACE 接收开关或配置变化。"""
        if not enabled:
            self._stop_trace()
            return
        if self._active_main_tab_widget is self._pid_panel:
            self._restart_trace()

    def _reset_trace_metrics(self):
        self._trace_expected_sequence = None
        self._trace_last_sample_index = None
        self._trace_received_points = 0
        self._trace_lost_packets = 0
        self._trace_overflow_count = 0
        self._trace_session_start_time = time.monotonic()
        self._trace_device_rate_hz = 0
        self._trace_effective_rate_hz = 0.0
        self._trace_last_data_time = 0.0
        self._pid_panel.set_capture_metrics(0, 0.0, "", 0, 0)

    def _stop_trace(self, send_command: bool = True):
        """停止 TRACE，并使已在链路中的旧帧失效。"""
        self._trace_session_id = (self._trace_session_id + 1) & 0xFFFF
        self._reset_trace_metrics()
        self._pid_panel.set_capture_active(False)
        if send_command and self._client.is_open():
            try:
                self._client.trace_stop(self._trace_session_id)
            except Exception:
                pass

    def _restart_trace(self):
        self._stop_trace()
        self._pid_panel._clear_pid_waveform()
        self._start_trace()

    def _start_trace(self):
        if (not self._client.is_open() or
                self._active_main_tab_widget is not self._pid_panel or
                not self._pid_panel.waveform_enabled()):
            self._pid_panel.set_capture_active(False)
            return
        self._reset_trace_metrics()
        self._trace_session_id = (self._trace_session_id + 1) & 0xFFFF
        if self._trace_session_id == 0:
            self._trace_session_id = 1
        profile = self._pid_panel.waveform_profile()
        if self._client.trace_config(
                True, self._trace_session_id, profile.capture_mask,
                self._pid_panel.waveform_rate_hz(), 4):
            self._pid_panel.set_capture_active(True)
        else:
            self._pid_panel.set_capture_metrics(0, 0.0, "TRACE 配置发送失败")
            self._pid_panel.set_capture_active(False)

    def _on_trace_config_received(self, status: dict):
        # 停止命令只返回空ACK；停止旧会话的异步应答不能改变当前会话状态。
        if not status:
            return
        session = int(status.get('session_id', 0))
        if session != self._trace_session_id:
            return
        rate = int(status.get('actual_rate_hz', 0))
        self._trace_device_rate_hz = rate
        self._pid_panel.set_capture_metrics(
            rate, self._trace_effective_rate_hz, "",
            self._trace_lost_packets, self._trace_overflow_count)

    def _on_trace_data(self, packet: dict):
        if (self._active_main_tab_widget is not self._pid_panel or
                not self._pid_panel.waveform_enabled() or
                not isinstance(packet, dict)):
            return
        if int(packet.get('session_id', -1)) != self._trace_session_id:
            return
        sequence = int(packet.get('sequence', 0))
        first_index = int(packet.get('first_sample_index', 0))
        count = int(packet.get('sample_count', 0))
        if count <= 0:
            return
        discontinuity = bool(int(packet.get('flags', 0)) & 0x02)
        expected = self._trace_expected_sequence
        if expected is not None:
            sequence_delta = (sequence - expected) & 0xFFFFFFFF
            if sequence_delta >= 0x80000000:
                return  # 乱序/回绕前的旧包，不得污染当前会话统计
            if sequence_delta != 0:
                self._trace_lost_packets += sequence_delta
                discontinuity = True
        last_index = self._trace_last_sample_index
        if last_index is not None:
            index_delta = (first_index - last_index) & 0xFFFFFFFF
            if index_delta >= 0x80000000:
                return  # 样本索引倒退，按乱序包丢弃
            if index_delta != 0:
                discontinuity = True
        self._trace_expected_sequence = (sequence + 1) & 0xFFFFFFFF
        self._trace_last_sample_index = (first_index + count) & 0xFFFFFFFF
        if discontinuity:
            packet = dict(packet)
            packet['flags'] = int(packet.get('flags', 0)) | 0x02
        self._trace_received_points += count
        self._trace_overflow_count = max(
            self._trace_overflow_count, int(packet.get('overflow_count', 0)))
        now = time.monotonic()
        elapsed = now - self._trace_session_start_time
        if elapsed > 0.0:
            self._trace_effective_rate_hz = self._trace_received_points / elapsed
        rate = int(packet.get('actual_rate_hz', 0))
        if rate > 0:
            self._trace_device_rate_hz = rate
        flags = int(packet.get('flags', 0))
        message = "数据不连续" if flags & 0x02 else ""
        if flags & 0x01:
            message = "设备缓冲溢出"
        self._pid_panel.set_capture_metrics(
            self._trace_device_rate_hz, self._trace_effective_rate_hz, message,
            self._trace_lost_packets, self._trace_overflow_count)
        self._pid_panel.feed_trace_data(packet)

    def _on_pid_debug_commit(self, ring: int, values: dict):
        """A6运行值 -> E7工程值 -> E6校验 -> A1切Flash -> EA固化。"""
        if self._pid_commit_phase != 'idle':
            self._log_panel.log_warn('[PID] 已有调试值固化流程正在执行，请等待完成')
            return
        if not self._ensure_open():
            return
        if (self._pending_param_reads or self._pending_param_writes or
                self._param_read_queue or self._param_write_queue):
            self._log_panel.log_warn('[PID] 参数通道忙，请等待当前读写完成后重试')
            return
        mapping = {
            0: {1: 'kp_ld', 2: 'ki_ld', 5: 'integral_limit'},
            1: {1: 'kp_lq', 2: 'ki_lq', 5: 'integral_limit'},
            2: {1: 'kp_s', 2: 'ki_s', 5: 'speed_integral_limit'},
            3: {1: 'kp_p', 5: 'position_integral_limit'},
        }
        by_name = {spec.code_name: pid for pid, spec in
                   self._registry.motor_config_params.items()}
        writes = []
        expected = {}
        for ptype, name in mapping.get(int(ring), {}).items():
            if ptype not in values or name not in by_name:
                continue
            pid = by_name[name]
            value = float(values[ptype])
            writes.append((pid, f'{value:.9g}'))
            expected[pid] = value
        if not writes:
            self._log_panel.log_warn('[PID] 当前环没有可持久化的已读字段')
            return
        self._pid_commit_expected = expected
        self._pid_commit_source_ring = 0 if ring <= 1 else ring - 1
        self._pid_commit_phase = 'writing'
        self._on_param_write_many(self._pid_params_panel, writes)
        self._log_panel.log(
            f'[PID] 提交调试值: A6缓存 -> E7写入 {len(writes)} 项；Kd/输出限幅/滤波/flags为运行期字段，不写Flash')

    def _start_pid_commit_verify(self):
        if self._pid_commit_phase != 'writing' or not self._pid_commit_expected:
            return
        self._pid_commit_phase = 'verifying'
        self._on_param_read_many(self._pid_params_panel,
                                 list(self._pid_commit_expected.keys()))
        self._log_panel.log('[PID] E7写入完成，开始E6逐项读回校验')

    def _cancel_pid_commit(self, reason: str):
        if self._pid_commit_phase == 'idle':
            return
        phase = self._pid_commit_phase
        self._pid_commit_expected.clear()
        self._pid_commit_source_ring = None
        self._pid_commit_phase = 'idle'
        if phase == 'writing':
            self._param_write_queue.clear()
        elif phase == 'verifying':
            self._param_read_queue.clear()
        self._log_panel.log_warn(f'[PID] {reason}，已取消调试值固化流程')

    def _on_main_tab_changed(self, index: int):
        """切到标定/PID Tab 且已连接时, 自动读一次参数。"""
        try:
            w = self._tabs.widget(index)
        except Exception:
            return
        if self._active_main_tab_widget is self._pid_panel and w is not self._pid_panel:
            self._restore_pid_telemetry()
            self._stop_trace()
            self._pid_debug_heartbeat_timer.stop()
            if self._pid_panel.has_any_debug_source():
                self._log_panel.log(
                    "[PID] 已离开调试页，停止DEBUG租约；设备将在2秒内安全回滚")
        self._active_main_tab_widget = w
        if w is self._calib_panel:
            if not self._client.is_open():
                return
            # 已连接: 触发标定结果面板批量读取 (含 Index 16 is_calibrated)
            try:
                self._calib_results_panel.read_all()
            except Exception:
                pass
            return
        if w is self._pid_panel:
            if not self._client.is_open():
                return
            # 来源切换有 IDLE/READY 状态守卫，进入页面先主动同步 C1，
            # 避免使用上一次连接或遥测缓存中的旧状态。
            try:
                self._client.query_state()
                self._client.pid_source_get()
            except Exception:
                pass
            self._activate_pid_telemetry()
            # 已连接: 触发 PID 参数面板批量读取 (ControlParam 段)
            try:
                self._pid_params_panel.read_all()
            except Exception:
                pass
            # 波形窗口开着就恢复采集 (与是否处于 DEBUG source 无关)
            self._start_trace()
            return

    def _on_apply_telemetry(self, enable: bool, mask: int, period_ms: int):
        if not self._ensure_open():
            self._telemetry_panel.set_running(False)
            return
        # 强制补全 DQ|PHASE, 否则三相电流/DQ电流恒为0(与启动恢复路径一致)
        if enable:
            mask |= jp.JmTlmBit.DQ | jp.JmTlmBit.PHASE
            # UI 必须反映实际下发掩码，避免复选框状态与下位机订阅不一致。
            self._telemetry_panel.apply_config(mask, period_ms, force=True)
        sent = self._client.set_telemetry(enable, mask, period_ms)
        if not sent:
            self._telemetry_panel.set_running(False)
            self._log_panel.log_warn("[遥测] SET_TELEMETRY 未发送成功")
        if enable:
            self._log_panel.log(
                f"[TX] 遥控使能: mask=0x{mask:04X} 周期={period_ms}ms")
        else:
            self._log_panel.log("[TX] 遥控停止")

    # ==================== 参数读写 ====================
    def _panel_remote_supported(self, panel: ParamPanel) -> bool:
        # 所有面板均支持远程读写(motor_config 0xE6-0xEB 与 motor_param 0xE0-0xE5 通道均已实现)
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
        # 范围检查: 越界时弹确认框, 用户确认后仍下发(交由下位机最终裁决)
        warn = panel.check_value_range(param_id, text)
        if warn:
            btn = QMessageBox.question(
                self, "参数超出范围", f"{warn}\n\n是否仍要写入?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if btn != QMessageBox.StandardButton.Yes:
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
            # 越界项仍下发, 仅 log 提示(批量场景避免弹框打断)
            warn = panel.check_value_range(param_id, text)
            if warn:
                self._log_panel.log_warn(f"{panel.panel_name()} {warn} (仍下发)")
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
                self._cmd_watchdog.start()
            elif self._pid_commit_phase == 'verifying':
                self._cancel_pid_commit('E6读回命令发送失败')
        else:
            if self._client.param_read(int(param_id)):
                self._pending_param_reads.append(panel)
                self._cmd_watchdog.start()

    def _send_param_write_queued(self, panel: ParamPanel, param_id: int, value: bytes):
        if not self._client.is_open():
            return
        if panel.source() == "motor_config":
            if self._client.motor_info_write(int(param_id), value):
                panel.note_write_sent(int(param_id))
                self._pending_param_writes.append(panel)
                self._cmd_watchdog.start()
            elif self._pid_commit_phase == 'writing':
                self._cancel_pid_commit('E7写入命令发送失败')
        else:
            if self._client.param_write(int(param_id), value):
                panel.note_write_sent(int(param_id))
                self._pending_param_writes.append(panel)
                self._cmd_watchdog.start()

    def _open_csv_load_dialog(self):
        """打开 CSV 加载对话框: 选择 motor_info / param_index CSV 并刷新全部参数表格。"""
        dlg = CsvLoadDialog(
            self._registry.motor_config_csv_path,
            self._registry.params_csv_path,
            self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dlg.chosen_paths()
        mc_path = chosen["motor_config"]
        pi_path = chosen["motor_param"]
        reloaded = []
        if mc_path and mc_path != self._registry.motor_config_csv_path:
            if self._registry.reload_motor_config(mc_path):
                reloaded.append("电机配置表")
            else:
                self._log_panel.log_warn(f"[CSV] 电机配置表加载失败: {mc_path}")
        if pi_path and pi_path != self._registry.params_csv_path:
            if self._registry.reload_params(pi_path):
                reloaded.append("运行时参数表")
            else:
                self._log_panel.log_warn(f"[CSV] 运行时参数表加载失败: {pi_path}")
        if reloaded:
            self._reload_all_param_panels()
            # 持久化(含新CSV路径), 复用关窗同款写回
            layout_store.set_meta("settings", self._collect_settings())
            msg = f"已加载 {' / '.join(reloaded)}, 参数表格已刷新"
            self._log_panel.log(f"[CSV] {msg}")
            self.statusBar().showMessage(msg, 4000)
        else:
            self._log_panel.log("[CSV] 无变更(路径未变或加载失败)")

    def _reload_all_param_panels(self):
        """CSV 重新加载后, 重建所有依赖 registry 的 ParamPanel 表格(保留列宽)。"""
        panels = [self._param_panel, self._config_panel,
                  self._calib_results_panel, self._pid_params_panel]
        # 清空读写队列: 在途读/写可能指向 reload 后已失效的 param_id
        self._pending_param_reads.clear()
        self._param_read_queue.clear()
        self._pending_param_writes.clear()
        self._param_write_queue.clear()
        # reload 重建表格会清空列宽, 先备份
        backups = []
        for p in panels:
            try:
                backups.append(p.get_opts())
            except Exception:
                backups.append({})
        for p in panels:
            p.reload()
        for p, opt in zip(panels, backups):
            try:
                if opt:
                    p.set_opts(opt)
            except Exception:
                pass

    def _on_config_save(self):
        if not self._panel_remote_supported(self._config_panel):
            return
        if self._ensure_open():
            # 电机配置走 0xEA (motor_info_save), 区别于运行时参数的 0xE4
            self._client.motor_info_save()
            self._log_panel.log("[TX] 保存电机配置到Flash/EEPROM (0xEA)")

    def _load_motor_presets(self):
        """加载内置 motor_presets.json + 用户 user_motor_presets.json

        返回: {"presets": {name: preset_dict}, "_user_names": set()}
        用户预设名存入 _user_names 用于 ComboBox 显示 [用户] 前缀
        """
        # 内置预设
        builtin_path = Path(__file__).parent.parent / "resources" / "motor_presets.json"
        builtin = {"presets": {}}
        try:
            with open(builtin_path, encoding='utf-8') as f:
                builtin = json.load(f)
        except Exception as e:
            self._log_panel.log_warn(f"[预设] 加载 motor_presets.json 失败: {e}")

        # 用户预设
        user_presets = preset_store.list_presets()

        # 合并: 内置 + 用户（用户优先级高，名称冲突时用户覆盖）
        merged = {"presets": {}, "_user_names": set()}
        merged["presets"].update(builtin.get("presets", {}))
        for name, preset in user_presets.items():
            merged["presets"][name] = preset
            merged["_user_names"].add(name)
        return merged

    def _on_motor_preset_selected(self, idx: int):
        """电机型号 ComboBox 选择变化: 仅控制应用按钮启用状态"""
        name = self._motor_preset_combo.currentData()
        # name=None: 请选择; name="": 自定义; 其他: 预设名
        self._motor_preset_apply_btn.setEnabled(name not in (None, ""))

    def _on_motor_preset_apply(self):
        """应用电机型号预设: 批量下发 9 个电气字段到 motor_info RAM (0xE7)"""
        name = self._motor_preset_combo.currentData()
        if not name:
            return
        presets = self._motor_presets.get("presets", {})
        if name not in presets:
            self._log_panel.log_warn(f"[预设] 未找到电机型号 '{name}'")
            return
        preset = presets[name]
        # 从 registry 查找 VariableName -> param_id (motor_config 通道)
        specs_by_name = {}
        for pid, spec in self._registry.motor_config_params.items():
            specs_by_name[spec.code_name] = (pid, spec)
        # 目标字段的 VariableName (10 个: 9 电气字段 + 1 保护阈值联动)
        # protect_over_current 在 ProtectCommParam 段(Index 128),
        # 通过 0xE7 下发时 dispatch 自动处理段偏移
        target_fields = [
            "pole_pairs", "phase_resistance", "phase_inductance_d",
            "phase_inductance_q", "flux_linkage", "torque_constant",
            "rotor_inertia", "peak_current", "max_speed",
            "protect_over_current",
        ]
        writes = []
        skipped = []
        for fname in target_fields:
            if fname not in preset:
                skipped.append(f"{fname}(预设缺失)")
                continue
            if fname not in specs_by_name:
                skipped.append(f"{fname}(CSV无此字段)")
                continue
            pid, spec = specs_by_name[fname]
            value = preset[fname]
            # 数值转字符串(与 _on_param_write_many 的签名一致: (param_id, text))
            text = str(value)
            writes.append((pid, text))
        if skipped:
            self._log_panel.log_warn(f"[预设] 部分字段跳过: {', '.join(skipped)}")
        if not writes:
            self._log_panel.log_warn(f"[预设] 电机型号 '{name}' 无可写字段")
            return
        label = preset.get("label", name)
        reply = QMessageBox.question(
            self, "应用电机型号预设",
            f"确认将电机型号 '{name}' ({label}) 的 {len(writes)} 个电气参数\n"
            f"批量写入 motor_info RAM (0xE7)?\n\n"
            f"写入后请点击下方'参数固化'按钮(0xEA)持久化到 Flash, 重启生效。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        # 复用批量写通道(走 _on_param_write_many → 0xE7)
        self._on_param_write_many(self._config_panel, writes)
        self._log_panel.log(f"[预设] 已应用电机型号 '{name}' ({len(writes)} 项 0xE7 写入), 请点击'参数固化'持久化")
        # P0: 写入成功后明确提示用户需两步操作才能完全生效
        # 原因: motor_profile_sync_to_param 仅在启动时执行一次,
        # 运行期不刷新 peak_current/max_speed 到控制环
        QMessageBox.information(
            self, "电机型号预设已应用",
            f"已将 '{name}' 预设的 {len(writes)} 个字段写入 motor_info RAM (0xE7)。\n\n"
            "请完成以下两步才能让新参数完全生效：\n"
            "  1. 点击下方'参数固化'按钮 (0xEA) 持久化到 Flash\n"
            "  2. 重启设备 (断电重上 或 发送 RESET 命令)\n"
            "     原因: motor_profile_sync_to_param 仅在启动时执行一次,\n"
            "     运行期不刷新 peak_current/max_speed 到控制环",
            QMessageBox.StandardButton.Ok)

    # ==================== P4: 用户预设管理 ====================
    def _on_preset_new(self):
        """新建用户电机预设: 弹出 PresetEditDialog"""
        dlg = PresetEditDialog(self, existing_preset=None)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            name, preset = dlg.get_preset()
            if not name:
                return
            if preset_store.get(name) is not None:
                QMessageBox.warning(self, "冲突", f"用户预设 '{name}' 已存在")
                return
            if not preset_store.add(name, preset):
                QMessageBox.critical(self, "失败", "写入 user_motor_presets.json 失败")
                return
            # 重新加载 ComboBox
            self._reload_preset_combo()
            self._log_panel.log(f"[预设] 已新建用户预设 '{name}'")

    def _reload_preset_combo(self):
        """重新加载 ComboBox（预设变更后调用）"""
        self._motor_preset_combo.clear()
        self._motor_preset_combo.addItem("(请选择电机型号)", None)
        self._motor_preset_combo.addItem("(自定义参数)", "")
        self._motor_presets = self._load_motor_presets()
        _user_names = self._motor_presets.get("_user_names", set())
        for name in self._motor_presets.get("presets", {}).keys():
            display = f"[用户] {name}" if name in _user_names else name
            self._motor_preset_combo.addItem(display, name)
        self._motor_preset_apply_btn.setEnabled(False)

    def _on_preset_manage(self):
        """打开预设管理对话框"""
        # 过滤掉用户预设，只传内置预设给对话框
        all_presets = self._motor_presets.get("presets", {})
        user_names = self._motor_presets.get("_user_names", set())
        builtin_only = {k: v for k, v in all_presets.items() if k not in user_names}

        dlg = PresetManageDialog(self, builtin_presets=builtin_only)
        dlg.exec()
        # 对话框关闭后重新加载 ComboBox
        self._reload_preset_combo()

    # ==================== P5: 重新标定向导 (0xEC) ====================
    def _on_motor_info_recalib(self):
        """重新标定向导: 清除标定状态以便重做标定

        场景: 同一电机编码器零位漂移，需重做 L3 编码器标定，
        电机本体参数（R/Ld/Lq/flux）不变。
        """
        if not self._ensure_open():
            return

        reply = QMessageBox.question(
            self, "重新标定向导",
            "此操作将清除以下字段，以便重新标定：\n\n"
            "  • is_calibrated → 0（标定状态标志）\n"
            "  • enc_offset → 0（编码器零位偏移）\n"
            "  • elec_angle_bias → 0（电角度偏移）\n"
            "  • enc_direction → 0（编码器方向）\n\n"
            "保留以下字段（电机本体参数不变）：\n"
            "  • R / Ld / Lq / flux / pole_pairs / Kt / inertia\n"
            "  • peak_current / max_speed / protect_over_current\n\n"
            "操作流程：\n"
            "  1. 点击'是'清除上述字段（仅 RAM，不落盘）\n"
            "  2. 点击'参数固化'按钮（0xEA）持久化到 Flash\n"
            "  3. 重启设备\n"
            "  4. 进入'电机标定'Tab 执行 L3 编码器标定\n"
            "  5. 标定完成后再次'参数固化' + 重启\n\n"
            "确认继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        # 调用 0xEC 命令（异步发送，ACK 通过 ack_received/nack_received 信号返回）
        if not self._client.motor_info_recalib_reset():
            QMessageBox.critical(self, "重新标定失败",
                "0xEC 命令发送失败，请检查:\n"
                "  • 设备是否已连接\n"
                "  • 串口/CAN 是否正常")
            return

        self._log_panel.log("[重新标定] 已发送 0xEC 命令清除 is_calibrated + 编码器字段（RAM）")

        QMessageBox.information(
            self, "重新标定命令已发送",
            "已发送 0xEC 命令清除标定状态字段（仅 RAM，未落盘）。\n\n"
            "请完成以下步骤：\n"
            "  1. 等待设备 ACK 应答（看日志面板）\n"
            "  2. 点击下方'参数固化'按钮 (0xEA) 持久化到 Flash\n"
            "  3. 重启设备 (断电重上 或 发送 RESET 命令)\n"
            "  4. 进入'电机标定'Tab 执行 L3 编码器零位/方向标定\n"
            "  5. 标定完成后再次'参数固化' + 重启",
            QMessageBox.StandardButton.Ok)

    # ==================== P3: motor_info 导出/导入 ====================
    def _on_motor_info_export(self):
        """导出 motor_info: 0xE6 批量读取所有字段 → JSON 文件。
        异步流程: 触发批量读 → _on_param_result 拦截收集 → _finalize_export 保存。"""
        if not self._ensure_open():
            return
        if self._export_state is not None:
            QMessageBox.warning(self, "导出进行中", "上一次导出尚未完成, 请稍候")
            return
        # 弹保存对话框
        default_name = time.strftime("motor_info_export_%Y%m%d_%H%M%S.json")
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出 motor_info 到文件", default_name, "JSON 文件 (*.json)")
        if not file_path:
            return
        # 收集所有 motor_config 通道的 param_id (排序确保 JSON 字段顺序稳定)
        param_ids = sorted(self._registry.motor_config_params.keys())
        if not param_ids:
            QMessageBox.warning(self, "导出失败", "motor_config 通道无可用字段")
            return
        # 初始化导出状态
        self._export_state = {
            "file": file_path,
            "fields": {},
            "total": len(param_ids),
            "collected": 0,
            "skipped": 0,
        }
        self._log_panel.log(f"[导出] 开始批量读取 {len(param_ids)} 个字段 → {file_path}")
        # 触发批量读(异步, _on_param_result 会拦截收集)
        self._on_param_read_many(self._config_panel, param_ids)

    def _finalize_export(self):
        """导出完成: 序列化 fields 为 JSON 保存到文件。"""
        if self._export_state is None:
            return
        state = self._export_state
        self._export_state = None
        try:
            # 读取 config_version (Index 0) 用于版本兼容性检查
            config_version = state["fields"].get("0", 0)
            try:
                config_version = int(config_version)
            except (ValueError, TypeError):
                config_version = 0
            export_data = {
                "_meta": {
                    "export_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "config_version": config_version,
                    "total_fields": state["total"],
                    "collected": state["collected"],
                    "skipped": state["skipped"],
                    "tool": f"{APP_NAME} v{APP_VERSION}",
                },
                "fields": state["fields"],
            }
            with open(state["file"], "w", encoding="utf-8") as f:
                json.dump(export_data, f, ensure_ascii=False, indent=2)
            self._log_panel.log(
                f"[导出] 完成: {state['collected']}/{state['total']} 字段保存到 {state['file']}")
            QMessageBox.information(
                self, "motor_info 导出完成",
                f"已导出 {state['collected']}/{state['total']} 个字段到:\n{state['file']}\n\n"
                "此文件可用于固件升级后恢复标定数据 (导入功能)。",
                QMessageBox.StandardButton.Ok)
        except Exception as e:
            self._log_panel.log_warn(f"[导出] 保存文件失败: {e}")
            QMessageBox.critical(self, "导出失败", f"保存文件失败:\n{e}")

    def _on_motor_info_import(self):
        """导入 motor_info: JSON 文件 → 0xE7 批量写入 RAM。"""
        if not self._ensure_open():
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self, "从文件导入 motor_info", "", "JSON 文件 (*.json)")
        if not file_path:
            return
        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "导入失败", f"加载 JSON 文件失败:\n{e}")
            return
        fields = data.get("fields", {})
        if not fields:
            QMessageBox.warning(self, "导入失败", "JSON 文件中无 fields 字段或为空")
            return
        # 版本兼容性检查
        meta = data.get("_meta", {})
        file_version = meta.get("config_version", "未知")
        # 构造 writes 列表: [(param_id, text), ...]
        # 只导入当前 registry 中存在的 param_id (避免导入旧版本不存在的字段)
        valid_pids = set(self._registry.motor_config_params.keys())
        writes = []
        skipped = []
        for pid_str, value in fields.items():
            try:
                pid = int(pid_str)
            except (ValueError, TypeError):
                skipped.append(f"{pid_str}(无效ID)")
                continue
            if pid not in valid_pids:
                skipped.append(f"{pid}(当前CSV无此字段)")
                continue
            writes.append((pid, str(value)))
        if skipped:
            self._log_panel.log_warn(f"[导入] 部分字段跳过: {', '.join(skipped[:5])}{'...' if len(skipped) > 5 else ''}")
        if not writes:
            QMessageBox.warning(self, "导入失败", "JSON 文件中无有效字段可导入")
            return
        reply = QMessageBox.question(
            self, "导入 motor_info",
            f"确认从文件导入 {len(writes)} 个字段到 motor_info RAM (0xE7)?\n\n"
            f"源文件: {file_path}\n"
            f"源 config_version: {file_version}\n\n"
            "写入后需点击'参数固化'按钮 (0xEA) + 重启设备才能完全生效。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._on_param_write_many(self._config_panel, writes)
        self._log_panel.log(f"[导入] 已从 {file_path} 写入 {len(writes)} 项到 motor_info RAM")
        QMessageBox.information(
            self, "motor_info 导入完成",
            f"已将 {len(writes)} 个字段写入 motor_info RAM (0xE7)。\n\n"
            "请完成以下两步才能让数据完全生效：\n"
            "  1. 点击下方'参数固化'按钮 (0xEA) 持久化到 Flash\n"
            "  2. 重启设备 (断电重上 或 发送 RESET 命令)",
            QMessageBox.StandardButton.Ok)

    def _on_param_result(self, param_id: int, ptype: int, value_bytes: bytes):
        if not self._pending_param_reads:
            # 无待处理读请求(设备主动上报/重复帧/自愈误触), 丢弃避免误填默认面板
            self._log_panel.log(f"[WARN] 收到意外参数应答 id={param_id}, 无待处理读请求")
            return
        panel = self._pending_param_reads.popleft()
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
        if self._pid_theory_pending is not None and panel is self._config_panel:
            waiting = self._pid_theory_pending["waiting"]
            waiting.discard(int(param_id))
            if not waiting:
                self._finish_pid_theory()
        if (self._pid_commit_phase == 'verifying' and
                param_id in self._pid_commit_expected):
            expected = self._pid_commit_expected[param_id]
            try:
                verified = math.isclose(float(val), float(expected), rel_tol=1e-5, abs_tol=1e-6)
            except (TypeError, ValueError):
                verified = False
            if not verified:
                self._cancel_pid_commit(
                    f'E6校验失败 {name}: expected={expected} actual={val}')
            else:
                self._pid_commit_expected.pop(param_id, None)
                if not self._pid_commit_expected and self._pid_commit_source_ring is not None:
                    source_ring = self._pid_commit_source_ring
                    self._pid_commit_source_ring = None
                    self._pid_commit_phase = 'switching'
                    if self._client.pid_source_set(source_ring, int(PidSource.FLASH)):
                        self._log_panel.log('[PID] E6校验通过，发送A1切换Flash来源')
                    else:
                        self._cancel_pid_commit('A1切换Flash来源发送失败')
        # P3: motor_info 导出拦截 — 收集读回值到 _export_state
        # _on_motor_info_export 触发批量读后, 每次读应答都收集到这里
        # 收集完成后由 _finalize_export 序列化为 JSON 保存
        if self._export_state is not None:
            self._export_state["fields"][str(param_id)] = disp
            self._export_state["collected"] += 1
            if self._export_state["collected"] >= self._export_state["total"]:
                self._finalize_export()
        # Task 5: Index 16 (is_calibrated) 读回值同步到标定面板的已标定徽章
        # (无论哪个 panel 收到, 都转发给 calib_panel; 写后 ACK 不自动读回时徽章靠乐观更新)
        if param_id == 16:
            self._calib_panel.set_calibrated_value(disp)
        # 电机本体参数转发到反馈面板的转子示意图(电阻/电感/磁链/转矩常数/极对数等)
        if spec is not None and val is not None and spec.code_name in self._MOTOR_PARAM_KEYS:
            self._motor_params[spec.code_name] = val
            self._feedback_panel.set_motor_params(self._motor_params)
        self._cmd_watchdog.stop()
        self._pump_param_read_queue()

    def _on_cmd_timeout(self):
        """命令超时看门狗: 弹出当前 pending 项并推进队列, 防止设备不响应时死锁。"""
        timed_out = []
        if self._pending_param_reads:
            panel = self._pending_param_reads.popleft()
            timed_out.append(f"读({panel.panel_name()})")
            if panel is self._config_panel and self._pid_theory_pending is not None:
                self._cancel_pid_theory("读取电机参数超时")
            self._pump_param_read_queue()
        elif self._pending_param_writes:
            panel = self._pending_param_writes.popleft()
            panel.reject_pending_write()
            timed_out.append(f"写({panel.panel_name()})")
            self._pump_param_write_queue()
        if timed_out:
            if self._pid_commit_phase in ('writing', 'verifying'):
                self._cancel_pid_commit(
                    'E7写入超时' if self._pid_commit_phase == 'writing' else 'E6读回超时')
            self._log_panel.log_warn(f"命令超时, 弹出: {', '.join(timed_out)}")
            self.statusBar().showMessage("命令超时, 队列已自愈", 3000)
            # 若队列仍有待发项, 重启看门狗
            if self._pending_param_reads or self._pending_param_writes:
                self._cmd_watchdog.start()

    # ==================== 数据接收 ====================
    def _queue_feedback(self, fb):
        if len(self._feedback_pending) >= self._display_buffer_max:
            self._feedback_pending.popleft()
            self._feedback_dropped += 1
        self._feedback_pending.append(fb)

    def _queue_state(self, top_fsm, run_state, ctrl_mode, enable):
        self._latest_state = (top_fsm, run_state, ctrl_mode, enable)

    def _on_ui_tick(self):
        if self._latest_state is not None:
            self._feedback_panel.update_state(*self._latest_state)
            self._state_panel.update_state(*self._latest_state)
            self._calib_panel.update_state(*self._latest_state)
            self._pid_panel.update_state(*self._latest_state)
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
            # X 轴使用数据点编号, 无需时间戳
            self._plot_panel.feed_feedback_batch(batch)
            last_fb = batch[-1]
            self._feedback_panel.update_feedback(last_fb)
            self._pid_panel.update_pid_observation(
                last_fb, targets=self._pid_last_targets)
            # 实时故障解码: 故障上升沿才记入历史
            self._fault_info_panel.update_fault_mask(
                getattr(last_fb, 'fault_mask', 0) or 0)
            # 调试通道数据分发 (逐帧全部绘制)
            if self._debug_window is not None and self._debug_window.isVisible():
                debug_vals = []
                for fb in batch:
                    d = getattr(fb, 'debug', ())
                    if d:
                        debug_vals.append(d)
                if debug_vals:
                    self._debug_window.feed_debug_batch(debug_vals)

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
        # PID 面板: 0xA1 来源切换 / 0xA5 调试参数写入 成功 ACK 转发(内部按 cmd 过滤)
        self._pid_panel.on_ack(cmd)
        if cmd == JmCmd.PID_SOURCE_SET:
            self._sync_pid_debug_heartbeat()
        # 写完成: 0xE1(运行时参数) / 0xE7(电机配置) 共用同一待应答队列
        if cmd in (JmCmd.PARAM_WRITE, JmCmd.MOTOR_INFO_WRITE):
            if not self._pending_param_writes:
                self._log_panel.log(f"[WARN] 收到意外写ACK {cmd_name(cmd)}, 无待处理写请求")
            else:
                panel = self._pending_param_writes.popleft()
                panel.confirm_pending_write()
                self._cmd_watchdog.stop()
                self._pump_param_write_queue()
                if (cmd == JmCmd.MOTOR_INFO_WRITE and self._pid_commit_phase == 'writing'
                        and not self._pending_param_writes and not self._param_write_queue):
                    self._start_pid_commit_verify()
        if cmd == JmCmd.PID_SOURCE_SET and self._pid_commit_phase == 'switching':
            self._pid_commit_phase = 'saving'
            if self._client.motor_info_save():
                self._log_panel.log('[PID] A1已切到Flash来源，发送EA固化')
            else:
                self._cancel_pid_commit('EA固化命令发送失败')
        # 需求2: 保存到 Flash/EEPROM (0xEA) ACK -> 标定结果/电机配置面板恢复正常颜色
        if cmd == JmCmd.MOTOR_INFO_SAVE:
            for pnl in (self._calib_results_panel, self._config_panel, self._pid_params_panel):
                fn = getattr(pnl, "clear_fresh", None)
                if callable(fn):
                    fn()
            if self._pid_commit_phase == 'saving':
                self._pid_commit_phase = 'idle'
                self._log_panel.log('[PID] 当前调试值已校验并固化到Flash')
        # 队列自愈: 若收到 PARAM_READ/MOTOR_INFO_READ 的意外 ACK(正常应走数据帧,
        # 但某些固件/仿真器可能只回 ACK), 弹出读队列防止永久死锁
        if cmd in (JmCmd.PARAM_READ, JmCmd.MOTOR_INFO_READ) and self._pending_param_reads:
            if cmd == JmCmd.MOTOR_INFO_READ and self._pid_commit_phase == 'verifying':
                self._cancel_pid_commit('E6未返回参数数据')
            panel = self._pending_param_reads.popleft()
            if panel is self._config_panel and self._pid_theory_pending is not None:
                self._cancel_pid_theory("设备未返回电机参数数据")
            self._cmd_watchdog.stop()
            self._pump_param_read_queue()

    def _on_nack(self, cmd: int, err: int):
        # NACK 中文描述: err_name_cn 返回 "枚举名(中文)"; 错误信息加载到故障面板
        cn = err_name_cn(err)
        self._log_panel.log_warn(
            f"NACK {cmd_name(cmd)}(0x{cmd:02X}) err={cn}(0x{err:02X})")
        # 故障信息面板加载 NACK 错误信息
        self._fault_info_panel.show_nack_error(cmd, err)
        if cmd == JmCmd.SET_TELEMETRY:
            self._telemetry_panel.set_running(False)
            self._log_panel.log_warn("[遥测] 下位机拒绝 SET_TELEMETRY，已停止显示为运行中")
        if cmd == JmCmd.SET_CAN_ID:
            self._pending_can_id = None
        if cmd == JmCmd.MOTOR_INFO_WRITE and self._pid_commit_phase == 'writing':
            self._cancel_pid_commit('E7写入失败')
        if cmd == JmCmd.PID_SOURCE_SET and self._pid_commit_phase == 'switching':
            self._cancel_pid_commit('A1切换Flash来源失败，未执行EA固化')
        if cmd == JmCmd.MOTOR_INFO_SAVE and self._pid_commit_phase == 'saving':
            self._cancel_pid_commit('EA固化失败')
        # 标定面板: 0x90~0x98 NACK 转发(内部按 cmd 过滤)
        self._calib_panel.on_nack(cmd, err)
        # PID 面板: 0xA1 来源切换 / 0xA5 调试参数写入 失败 NACK 转发(内部按 cmd 过滤)
        self._pid_panel.on_nack(cmd, err)
        if cmd == JmCmd.PID_SOURCE_SET:
            self._sync_pid_debug_heartbeat()
        # 读失败: 0xE0 / 0xE6 共用同一待应答队列
        if cmd in (JmCmd.PARAM_READ, JmCmd.MOTOR_INFO_READ) and self._pending_param_reads:
            if cmd == JmCmd.MOTOR_INFO_READ and self._pid_commit_phase == 'verifying':
                self._cancel_pid_commit('E6读回失败')
            panel = self._pending_param_reads.popleft()
            if panel is self._config_panel and self._pid_theory_pending is not None:
                self._cancel_pid_theory("读取电机参数失败")
            self._cmd_watchdog.stop()
            self._pump_param_read_queue()
        # 写失败: 0xE1 / 0xE7 共用同一待应答队列
        if cmd in (JmCmd.PARAM_WRITE, JmCmd.MOTOR_INFO_WRITE):
            if not self._pending_param_writes:
                self._log_panel.log(f"[WARN] 收到意外写NACK {cmd_name(cmd)}, 无待处理写请求")
            else:
                panel = self._pending_param_writes.popleft()
                panel.reject_pending_write()
                self._cmd_watchdog.stop()
                self._pump_param_write_queue()

    def _on_calib_status(self, status: dict):
        """0x97 CALIB_QUERY 详细状态应答 (JmClient.calib_status_received 信号).

        status 含: state/fail_reason/progress/level/submode/step/step_total +
        state_cn/fail_reason_cn/target_cn。转发给标定面板做进度/失败展示,
        并在日志面板按一行简要记录(详细记录由标定面板自身历史维护)。
        """
        state = int(status.get('state', 0))
        progress = int(status.get('progress', 0))
        target_cn = status.get('target_cn', '')
        state_cn = status.get('state_cn', '')
        # 简要日志(标定面板历史有更详细记录)
        self._log_panel.log(
            f"[RX] CALIB_QUERY {state_cn} {target_cn} progress={progress}%")
        self._calib_panel.on_calib_status(status)

    def _on_pid_autotune_result(self, result: dict):
        """0xA0 PID_AUTOTUNE 应答 (JmClient.pid_autotune_result_received 信号)。

        result 含: ok/status/fail_reason/ring_select_done + status_cn/fail_reason_cn。
        转发给 PID 面板做 source 徽章更新/历史记录, 主窗口仅记简要日志。
        """
        ok = result.get('ok', False)
        fail_cn = result.get('fail_reason_cn', '')
        ring_done = int(result.get('ring_select_done', 0))
        if ok:
            self._log_panel.log(f"[RX] PID_AUTOTUNE 成功 ring_select={ring_done}")
        else:
            self._log_panel.log_warn(f"[RX] PID_AUTOTUNE 失败: {fail_cn}")
        self._pid_panel.on_autotune_result(result)

    def _on_pid_source_received(self, cur_src: int, vel_src: int, pos_src: int):
        """0xA2 PID_SOURCE_GET 应答 (JmClient.pid_source_received 信号)。

        转发给 PID 面板更新 source 徽章 + 同步单选按钮, 主窗口仅记简要日志。
        """
        self._log_panel.log(
            f"[RX] PID_SOURCE cur={cur_src} vel={vel_src} pos={pos_src}")
        preserve_draft = self._pid_panel.has_unapplied_source_selection()
        self._pid_panel.on_source_received(
            cur_src, vel_src, pos_src, sync_selection=not preserve_draft)
        self._sync_pid_debug_heartbeat()

    def _on_pid_param_received(self, ring: int, param_type: int, value4: bytes):
        """0xA6 PID_PARAM_GET 应答 (JmClient.pid_param_received 信号)。

        转发给 PID 面板显示读回值 + 历史记录。
        """
        self._pid_panel.on_pid_param_received(ring, param_type, value4)

    def _on_pid_autotune(self, ring_select: int, cur_bw: float, vel_bw: float, pos_bw: float):
        """Read motor_info and generate editable PID values on the host."""
        if not self._ensure_open():
            return
        if self._pid_theory_pending is not None:
            self._pid_panel.on_autotune_error("理论估计正在读取电机参数")
            return

        names = set()
        if ring_select & 0x01:
            names.update((
                "phase_resistance", "phase_inductance_d",
                "phase_inductance_q", "current_lim",
                "current_control_bandwidth", "peak_current"))
        if ring_select & 0x02:
            names.update((
                "torque_constant", "rotor_inertia",
                "current_lim", "peak_current"))

        param_ids = self._config_panel.param_ids_by_name(names)
        if names and not param_ids:
            self._pid_panel.on_autotune_error(
                "motor_info.csv 缺少理论估计所需参数")
            return
        self._pid_theory_pending = {
            "ring_select": int(ring_select),
            "cur_bw": float(cur_bw),
            "vel_bw": float(vel_bw),
            "pos_bw": float(pos_bw),
            "waiting": set(param_ids),
        }
        if not param_ids:
            self._finish_pid_theory()
            return
        self._log_panel.log(
            f"[PID] 理论估计读取 motor_info: {len(param_ids)} 项")
        self._on_param_read_many(self._config_panel, param_ids)

    def _finish_pid_theory(self):
        pending = self._pid_theory_pending
        if pending is None:
            return
        self._pid_theory_pending = None
        values = self._config_panel.current_values_by_name(
            self._PID_THEORY_PARAM_NAMES)
        try:
            proposal = estimate_pid_proposal(
                values, pending["ring_select"], pending["cur_bw"],
                pending["vel_bw"], pending["pos_bw"])
        except (TypeError, ValueError) as exc:
            self._pid_panel.on_autotune_error(str(exc))
            self._log_panel.log_warn(f"[PID] 理论估计失败: {exc}")
            return
        self._pid_panel.on_autotune_proposal(
            pending["ring_select"], proposal)
        self._log_panel.log(
            f"[PID] 理论估计生成 {len(proposal)} 项修改值，未写入设备")

    def _cancel_pid_theory(self, reason: str):
        if self._pid_theory_pending is None:
            return
        self._pid_theory_pending = None
        self._pid_panel.on_autotune_error(reason)
        self._log_panel.log_warn(f"[PID] 理论估计取消: {reason}")

    def _on_pid_source_set(self, ring_select: int, source: int):
        """PID 面板请求来源切换 -> 调用 client.pid_source_set (0xA1)。"""
        if not self._ensure_open():
            return
        try:
            self._client.pid_source_set(ring_select, source)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"发送异常: {e}")

    def _on_pid_param_set(self, ring: int, param_type: int, value: float):
        """PID 面板请求调试参数写入 -> 调用 client.pid_param_set (0xA5)。"""
        if not self._ensure_open():
            return
        try:
            self._client.pid_param_set(ring, param_type, value)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"发送异常: {e}")

    def _on_pid_param_get(self, ring: int, param_type: int):
        """PID 面板请求调试参数读取 -> 调用 client.pid_param_get (0xA6)。"""
        if not self._ensure_open():
            return
        try:
            self._client.pid_param_get(ring, param_type)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"发送异常: {e}")

    def _on_dev_info(self, hw: int, fw: int, uid: bytes):
        uid_hex = uid.hex(':').upper()
        self._log_panel.log(
            f"[RX] DEV_INFO HW={format_version(hw)} "
            f"FW={format_version(fw)} UID={uid_hex}")
        QMessageBox.information(
            self, "设备信息",
            f"硬件版本: {format_version(hw)}\n"
            f"固件版本: {format_version(fw)}\nUID: {uid_hex}")

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
        # 退出前停止调试采样和周期遥测，避免串口关闭后下位机继续发送。
        self._stop_trace()
        if self._client.is_open():
            self._client.set_telemetry(False, 0, 0)
        self._stats_timer.stop()
        self._ui_timer.stop()
        # 停止自动重连定时器, 避免退出后残留触发
        self._reconnect_timer.stop()
        self._log_panel.flush()
        self._client.stop()
        event.accept()
