"""
PID 整定面板 (顶级 Tab)。

对标"电机标定" Tab 的三段式布局:
  1. 状态卡 — 连接状态 / 三环 source 徽章 / 最近操作
  2. 操作区 — 左:上位机理论估计 右:来源/调试模式切换(0xA1)
  3. 实时调试区 — 0xA5 写 / 0xA6 读 单个PID参数 (需对应环 source=DEBUG)
  4. 结果/历史 Splitter — ControlParam 参数表 + 操作历史

信号:
  pid_autotune_requested(ring_mask, cur_bw, vel_bw, pos_bw) — 主窗口连 client.pid_autotune
  pid_source_set_requested(ring_select, source)              — 主窗口连 client.pid_source_set
  pid_param_set_requested(ring, param_type, value)           — 主窗口连 client.pid_param_set
  pid_param_get_requested(ring, param_type)                  — 主窗口连 client.pid_param_get

状态守卫: 理论估计只读 motor_info；0xA1 允许 IDLE/READY。0xA5/0xA6 无状态限制。
"""
from collections import deque
from datetime import datetime
from enum import IntEnum
import math

from PyQt6.QtCore import pyqtSignal, Qt, QTimer
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
    QPushButton, QSplitter, QTextEdit, QCheckBox, QDoubleSpinBox,
    QButtonGroup, QRadioButton, QSizePolicy, QComboBox, QSlider,
    QWidget, QScrollArea, QGridLayout,
)

from jmproto.cmd_def import JmCmd, TopFsm, top_fsm_name
from core.pid_debug_profile import (
    PID_WAVEFORM_DEFAULT_SOURCE, get_pid_debug_profile, get_waveform_source_def,
)
from core.pid_autotune import (
    DEFAULT_CURRENT_BW_HZ, DEFAULT_VELOCITY_BW_HZ, DEFAULT_POSITION_BW_HZ,
)
from ui.theme import theme
from ui.panels.pid_waveform_window import PidWaveformWindow


# ---- 三环 source 枚举 (与固件 motor_pid_load.h pid_source_e 一致) ----
class PidSource(IntEnum):
    DEFAULT = 0    # motor_param.c 默认值
    FLASH = 1      # Flash ControlParam 工程值
    AUTOTUNE = 2   # 理论估计值
    DEBUG = 3      # 调试值: 0xA5 下发的参数 (不持久化, 重启消失)


# ---- 三环标识 (与固件 pid_ring_e 一致) ----
class PidRing(IntEnum):
    CURRENT = 0
    VELOCITY = 1
    POSITION = 2


_RING_CN = {PidRing.CURRENT: "电流环", PidRing.VELOCITY: "速度环", PidRing.POSITION: "位置环"}
_SOURCE_CN = {PidSource.DEFAULT: "默认", PidSource.FLASH: "Flash", PidSource.AUTOTUNE: "理论估计", PidSource.DEBUG: "调试"}
_SOURCE_COLOR_KEY = {PidSource.DEFAULT: "muted", PidSource.FLASH: "warn", PidSource.AUTOTUNE: "accent", PidSource.DEBUG: "ok"}

# 来源区域同时承载 DEBUG 子模式。option_id 仅用于上位机单选组，
# 下发 A1 时仍转换为原协议 source=0..3。
_SOURCE_OPTIONS = {
    PidRing.CURRENT: (
        (0, "默认", PidSource.DEFAULT, None),
        (1, "Flash", PidSource.FLASH, None),
        (2, "理论估计", PidSource.AUTOTUNE, None),
        (3, "调试-ID", PidSource.DEBUG, "current_d"),
        (31, "调试-IQ", PidSource.DEBUG, "current_q"),
    ),
    PidRing.VELOCITY: (
        (0, "默认", PidSource.DEFAULT, None),
        (1, "Flash", PidSource.FLASH, None),
        (2, "理论估计", PidSource.AUTOTUNE, None),
        (3, "调试-速度", PidSource.DEBUG, "velocity"),
    ),
    PidRing.POSITION: (
        (0, "默认", PidSource.DEFAULT, None),
        (1, "Flash", PidSource.FLASH, None),
        (2, "理论估计", PidSource.AUTOTUNE, None),
        (3, "调试-位置", PidSource.DEBUG, "position"),
    ),
}


class PidPanel(QGroupBox):
    """PID 整定面板 (顶级 Tab)。"""

    # ---- 请求信号 (主窗口连接到 JmClient) ----
    pid_autotune_requested = pyqtSignal(int, float, float, float)  # ring_mask(bit0=电流 bit1=速度 bit2=位置), cur_bw, vel_bw, pos_bw
    pid_source_set_requested = pyqtSignal(int, int)                # ring_select, source
    pid_source_get_requested = pyqtSignal()                        # A2 readback/heartbeat
    pid_state_get_requested = pyqtSignal()                         # C1 refresh before guarded operations
    pid_param_set_requested = pyqtSignal(int, int, float)          # ring, param_type, value (0xA5)
    pid_param_get_requested = pyqtSignal(int, int)                 # ring, param_type (0xA6)
    pid_capture_start_requested = pyqtSignal(int, int, int)       # mask, rate_hz, samples
    pid_capture_stop_requested = pyqtSignal()
    pid_capture_read_requested = pyqtSignal(int, int)             # offset, count
    pid_debug_commit_requested = pyqtSignal(int, object)          # active profile ring, values
    pid_tuning_mode_changed = pyqtSignal(str)                     # current_d/current_q/velocity/position
    pid_waveform_config_changed = pyqtSignal(bool, int)           # enabled, requested sample/upload rate

    _HISTORY_MAX = 200
    # 波形存储: 按时间跨度(秒)配置, 而非固定点数。内存上限用于高频自动抽稀。
    _WAVEFORM_SPAN_SEC = 20.0
    _WAVEFORM_MAX_POINTS = 200000

    def __init__(self, registry=None, parent=None):
        super().__init__("", parent)
        self._panel_name = "PID 整定"
        self._registry = registry
        self._link_active = False
        self._current_top_fsm = None
        # 三环 source 状态 (连接后由 0xA2 查询回填, 本地仅做缓存)
        self._ring_source = {r: PidSource.DEFAULT for r in PidRing}
        # 操作历史
        self._history = deque(maxlen=self._HISTORY_MAX)
        # 内嵌参数面板引用 (由主窗口 attach_params_panel 注入)
        self._params_panel = None
        # DEBUG 滑条组: ring(0/1/2/3) -> _SliderGroup 实例
        self._debug_slider_groups = {}
        # 当前 PID 调试模式 + 观测数据缓存
        self._tuning_mode = "current_d"
        self._selected_debug_mode = self._tuning_mode
        self._pid_targets = {"id": 0.0, "iq": 0.0, "vel": 0.0, "pos": 0.0}
        self._last_feedback = None
        self._plot_sample_index = 0      # 波形横坐标: 采样点编号 (从 0 递增)
        self._plot_paused = False
        # 波形显示与 DEBUG 整定解耦: 采哪路数据由波形窗口顶部切换
        self._waveform_source = PID_WAVEFORM_DEFAULT_SOURCE
        self._waveform_rate_hz = 1000
        # 波形存储缓冲: 按时间跨度(秒)×采样率决定容量, 高频时自动抽稀
        self._plot_points = deque(
            maxlen=self._waveform_buffer_size(self._waveform_rate_hz))
        self._waveform_window = None
        self._waveform_window_opts = {}
        self._capture_active = False
        self._debug_values = {}
        # 写入后延迟回读定时器: 每个 (ring, param_type) 一个单次 QTimer
        self._readback_timers = {}
        self._source_set_queue = deque()
        self._source_set_pending = None
        self._source_waiting_for_state = False
        # 构建UI
        self._build()
        self._refresh_status()

    # ========================================================================
    #  UI 构建
    # ========================================================================
    def _build(self):
        """整个 PID 整定界面用 QScrollArea 包裹, 内容超出时整体滚动。"""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._main_scroll = QScrollArea()
        self._main_scroll.setWidgetResizable(True)
        self._main_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._main_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._main_scroll.setStyleSheet(self._main_scroll_qss())

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self._build_status_card(layout)
        self._build_operation_panel(layout)
        self._build_tuning_observer(layout)
        self._build_debug_slider_area(layout)
        self._build_results_history_split(layout)
        self._sync_slider_groups()

        self._main_scroll.setWidget(content)
        outer.addWidget(self._main_scroll)

    def _main_scroll_qss(self) -> str:
        return (f"QScrollArea{{background:transparent; border:none;}}"
                f"QScrollBar:vertical{{background:transparent; width:10px; margin:0;}}"
                f"QScrollBar::handle:vertical{{background:{theme.hex('scroll_handle')};"
                f"border-radius:4px; min-height:24px;}}"
                f"QScrollBar::handle:vertical:hover{{background:{theme.hex('accent')};}}"
                f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}"
                f"QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{{background:transparent;}}")

    def _build_status_card(self, parent_layout):
        """状态卡: 左(连接状态 + 最近操作) 右(三环 source 徽章)。"""
        card = QFrame()
        card.setObjectName("statusCard")
        card.setMaximumHeight(52)
        h = QHBoxLayout(card)
        h.setContentsMargins(12, 4, 12, 4)
        h.setSpacing(10)

        # 左: 连接状态 + 最近操作
        left = QVBoxLayout()
        left.setSpacing(0)
        self._status_label = QLabel("未连接")
        self._status_label.setStyleSheet(
            f"font-weight:bold; color:{theme.hex('text_strong')};")
        self._last_op_label = QLabel("就绪")
        self._last_op_label.setStyleSheet(f"color:{theme.hex('muted')};")
        left.addWidget(self._status_label)
        left.addWidget(self._last_op_label)
        h.addLayout(left, 1)

        # 右: 三环 source 徽章
        right = QHBoxLayout()
        right.setSpacing(6)
        self._source_labels = {}
        for ring in PidRing:
            vbox = QVBoxLayout()
            vbox.setSpacing(0)
            name_lbl = QLabel(_RING_CN[ring])
            name_lbl.setStyleSheet(
                f"color:{theme.hex('muted')}; font-size:11px;")
            name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            src_lbl = QLabel(_SOURCE_CN[PidSource.DEFAULT])
            src_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            src_lbl.setStyleSheet(self._source_badge_qss(PidSource.DEFAULT))
            vbox.addWidget(name_lbl)
            vbox.addWidget(src_lbl)
            right.addLayout(vbox)
            self._source_labels[ring] = src_lbl
        h.addLayout(right, 0)

        parent_layout.addWidget(card)

    def _source_badge_qss(self, source: PidSource) -> str:
        """source 徽章 QSS: 按来源类型着色。"""
        color_key = _SOURCE_COLOR_KEY.get(source, "muted")
        bg = theme.hex(color_key)
        return (f"background:{bg}; color:{theme.hex('text_strong')};"
                f"padding:2px 8px; border-radius:8px; font-size:11px; font-weight:bold;")

    def _build_operation_panel(self, parent_layout):
        """操作区: 左(生成修改值) 右(来源及调试模式切换 0xA1)。"""
        box = QFrame()
        box.setObjectName("pidOperationRow")
        box.setStyleSheet("QFrame#pidOperationRow{background:transparent; border:none;}")
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        # ---- 左: 理论估计 ----
        autotune_box = QGroupBox("理论估计（生成修改值）")
        av = QVBoxLayout(autotune_box)
        av.setSpacing(6)

        # 环选择
        ring_row = QHBoxLayout()
        ring_row.addWidget(QLabel("整定环:"))
        self._autotune_ring_checks = {}
        for ring in PidRing:
            chk = QCheckBox(_RING_CN[ring])
            chk.setChecked(True)
            self._autotune_ring_checks[ring] = chk
            ring_row.addWidget(chk)
        ring_row.addStretch()
        av.addLayout(ring_row)

        # 带宽输入
        bw_row = QHBoxLayout()
        bw_row.addWidget(QLabel("电流环(Hz):"))
        self._cur_bw_spin = QDoubleSpinBox()
        self._cur_bw_spin.setRange(0.0, 10000.0)
        self._cur_bw_spin.setValue(0.0)
        self._cur_bw_spin.setToolTip("0 使用电机配置带宽")
        bw_row.addWidget(self._cur_bw_spin)
        bw_row.addWidget(QLabel("速度环(Hz):"))
        self._vel_bw_spin = QDoubleSpinBox()
        self._vel_bw_spin.setRange(0.0, 5000.0)
        self._vel_bw_spin.setValue(0.0)
        self._vel_bw_spin.setToolTip(f"0 使用 {DEFAULT_VELOCITY_BW_HZ:g} Hz")
        bw_row.addWidget(self._vel_bw_spin)
        bw_row.addWidget(QLabel("位置环(Hz):"))
        self._pos_bw_spin = QDoubleSpinBox()
        self._pos_bw_spin.setRange(0.0, 1000.0)
        self._pos_bw_spin.setValue(0.0)
        self._pos_bw_spin.setToolTip(f"0 使用 {DEFAULT_POSITION_BW_HZ:g} Hz")
        bw_row.addWidget(self._pos_bw_spin)
        av.addLayout(bw_row)

        # 开始按钮
        self._autotune_btn = QPushButton("开始理论估计")
        self._autotune_btn.setStyleSheet(self._ok_btn_qss())
        self._autotune_btn.clicked.connect(self._on_autotune_clicked)
        av.addWidget(self._autotune_btn)

        h.addWidget(autotune_box, 1)

        # ---- 右: 来源切换 (0xA1)，同时选择 DEBUG 子模式 ----
        source_box = QGroupBox("来源切换 (0xA1)")
        sv = QVBoxLayout(source_box)
        sv.setSpacing(6)

        self._source_btn_groups = {}
        for ring in PidRing:
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{_RING_CN[ring]}:"))
            grp = QButtonGroup(self)
            grp.setExclusive(True)
            for option_id, label, src, mode in _SOURCE_OPTIONS[ring]:
                rb = QRadioButton(label)
                grp.addButton(rb, int(option_id))
                if src == PidSource.DEFAULT and option_id == 0:
                    rb.setChecked(True)
                row.addWidget(rb)
            grp.idClicked.connect(
                lambda option_id, r=ring: self._on_source_option_clicked(r, option_id))
            self._source_btn_groups[int(ring)] = grp
            row.addStretch()
            sv.addLayout(row)

        self._source_apply_btn = QPushButton("应用来源切换")
        self._source_apply_btn.setStyleSheet(self._normal_btn_qss())
        self._source_apply_btn.clicked.connect(self._on_source_apply_clicked)
        sv.addWidget(self._source_apply_btn)

        h.addWidget(source_box, 1)

        parent_layout.addWidget(box)

    # 0xA5/0xA6 调试参数: ring 与 param_type 映射 (与固件 motor_pid_profile_set_param 一致)
    _DEBUG_RING_ITEMS = [("D轴", 0), ("Q轴", 1), ("速度", 2), ("位置", 3)]
    _TUNING_MODE_ITEMS = [
        ("current_d", "ID电流", 0, PidRing.CURRENT, "id", "A",
         "电流环 D 轴参数, 对应固件实时 PID ring=0"),
        ("current_q", "IQ电流", 1, PidRing.CURRENT, "iq", "A",
         "电流环 Q 轴参数, 对应固件实时 PID ring=1"),
        ("velocity", "速度环", 2, PidRing.VELOCITY, "vel", "rad/s",
         "速度环参数, 对应固件实时 PID ring=2"),
        ("position", "位置环", 3, PidRing.POSITION, "pos", "rad",
         "位置环参数, 对应固件实时 PID ring=3"),
    ]
    _DEBUG_PARAM_ITEMS = [
        ("比例KP", 1), ("积分KI", 2), ("微分KD", 3),
        ("输出限幅", 4), ("积分限幅", 5),
        ("滤波系数", 6), ("标志位", 7),
    ]

    # 滑条精度: int 0~_SLIDER_STEPS 映射到 [min, max]
    _SLIDER_STEPS = 10000

    # 各参数默认 min/max (首次 0xA6 读回前使用; 读回后按 读回值×2 自适应)
    _PARAM_DEFAULT_RANGE = {
        1: (0.0, 1.0),        # kp
        2: (0.0, 1.0),        # ki
        3: (0.0, 1.0),        # kd
        4: (0.0, 30.0),       # output_limit
        5: (0.0, 20.0),       # integral_limit
        6: (0.0, 1.0),        # output_filter_alpha
        7: (0.0, 65535.0),    # flags (uint32, 默认 0~0xFFFF)
    }

    # 写入后延迟回读时间 (ms)
    _READBACK_DELAY_MS = 100

    # ========================================================================
    #  DEBUG 滑条面板区 (source=DEBUG 时动态展开, 0xA5 写 / 0xA6 轮询读)
    # ========================================================================
    def _build_tuning_observer(self, parent_layout):
        """显示当前 DEBUG 模式及目标/反馈。"""
        box = QFrame()
        box.setObjectName("pidObserver")
        box.setStyleSheet(self._observer_box_qss())
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 2, 0, 2)
        v.setSpacing(6)

        obs_row = QHBoxLayout()
        obs_row.setSpacing(8)
        self._obs_mode_label = QLabel("--")
        self._obs_target_label = QLabel("--")
        self._obs_feedback_label = QLabel("--")
        self._obs_error_label = QLabel("--")
        self._obs_source_label = QLabel("--")
        for title, lbl in (
            ("当前", self._obs_mode_label),
            ("目标", self._obs_target_label),
            ("反馈", self._obs_feedback_label),
            ("误差", self._obs_error_label),
            ("状态", self._obs_source_label),
        ):
            obs_row.addWidget(self._make_observer_metric(title, lbl))
        v.addLayout(obs_row)

        parent_layout.addWidget(box)
        self._tuning_observer_box = box
        self._refresh_observer_labels()

    def _build_debug_slider_area(self, parent_layout):
        """滑条面板容器: 初始隐藏, 当某环 source 切换到 DEBUG 时动态添加对应滑条组。
        滑条组直接展开 (由外层 _main_scroll 统一提供滚动)。
        """
        self._slider_container = QFrame()
        self._slider_container.setObjectName("pidDebugArea")
        self._slider_container.setStyleSheet(self._slider_container_qss())
        v = QVBoxLayout(self._slider_container)
        v.setContentsMargins(0, 2, 0, 2)
        v.setSpacing(4)

        # 标题行: 左(PID 调试波形 · 目标/反馈/误差) 右(打开波形窗口 + 保存当前调试值)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        header_title = QLabel("PID 调试波形")
        header_title.setStyleSheet(
            f"color:{theme.hex('text_strong')}; font-weight:bold; font-size:12px;")
        self._plot_info_label = QLabel("目标 / 反馈 / 误差")
        self._plot_info_label.setStyleSheet(
            f"color:{theme.hex('muted')}; font-size:11px;")
        header.addWidget(header_title)
        header.addWidget(self._plot_info_label)
        header.addStretch()

        self._debug_commit_btn = QPushButton("保存当前调试值")
        self._debug_commit_btn.setToolTip("把当前环可持久化字段写入ControlParam，读回校验后固化Flash")
        self._debug_commit_btn.setFixedHeight(24)
        self._debug_commit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._debug_commit_btn.setStyleSheet(self._normal_btn_qss())
        self._debug_commit_btn.clicked.connect(self._on_debug_commit_clicked)
        header.addWidget(self._debug_commit_btn)

        v.addLayout(header)

        # 滑条组动态挂载区 (直接布局, 不再用内部 QScrollArea)
        self._slider_area = QVBoxLayout()
        self._slider_area.setSpacing(6)
        self._slider_area.setContentsMargins(2, 2, 2, 2)
        v.addLayout(self._slider_area)

        parent_layout.addWidget(self._slider_container)
        # 初始隐藏 (无 DEBUG 组时不占空间)
        self._slider_container.setVisible(False)

    def _make_observer_metric(self, title: str, value_label: QLabel) -> QFrame:
        frame = QFrame()
        frame.setObjectName("pidObserverMetric")
        frame.setStyleSheet(self._metric_frame_qss())
        frame.setMinimumWidth(120)
        v = QVBoxLayout(frame)
        v.setContentsMargins(8, 5, 8, 5)
        v.setSpacing(1)
        title_label = QLabel(title)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet(f"color:{theme.hex('muted')}; font-size:11px;")
        value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        value_label.setStyleSheet(self._metric_value_qss())
        value_label.setMinimumHeight(20)
        v.addWidget(title_label)
        v.addWidget(value_label)
        return frame

    def _observer_box_qss(self) -> str:
        return "QFrame#pidObserver{background:transparent; border:none;}"

    def _mode_btn_qss(self) -> str:
        return (f"QPushButton{{background:{theme.hex('input_bg')};"
                f"color:{theme.hex('text')}; border:1px solid {theme.hex('btn_border')};"
                f"padding:5px 12px; border-radius:4px; font-weight:bold;}}"
                f"QPushButton:hover{{border-color:{theme.hex('accent')};}}"
                f"QPushButton:checked{{background:{theme.hex('accent')};"
                f"color:{theme.hex('card_top')}; border-color:{theme.hex('accent')};}}")

    def _metric_frame_qss(self) -> str:
        return (f"QFrame#pidObserverMetric{{background:{theme.hex('panel_bg')};"
                f"border:none; border-radius:4px;}}")

    def _metric_value_qss(self, color_key: str = "text_strong") -> str:
        return (f"color:{theme.hex(color_key)}; font-family:Consolas, 'Microsoft YaHei', monospace;"
                f"font-size:13px; font-weight:bold;")

    def _slider_container_qss(self) -> str:
        return "QFrame#pidDebugArea{background:transparent; border:none;}"

    def _ring_cn(self, ring: int) -> str:
        """ring(0~3) -> 中文名。"""
        if 0 <= ring < len(self._DEBUG_RING_ITEMS):
            return self._DEBUG_RING_ITEMS[ring][0]
        return f"ring{ring}"

    def _param_name(self, param_type: int) -> str:
        """param_type(1~7) -> 参数名。"""
        if 1 <= param_type <= len(self._DEBUG_PARAM_ITEMS):
            return self._DEBUG_PARAM_ITEMS[param_type - 1][0]
        return f"type{param_type}"

    def _ensure_slider_group(self, ring: int):
        """创建(若不存在)并显示指定 ring 的滑条组面板。"""
        if ring in self._debug_slider_groups:
            grp = self._debug_slider_groups[ring]
            grp.container.setVisible(True)
            return grp
        grp = _SliderGroup(ring, self)
        self._slider_area.addWidget(grp.container)
        self._debug_slider_groups[ring] = grp
        return grp

    def _mode_def(self, key: str | None = None):
        key = key or self._tuning_mode
        for item in self._TUNING_MODE_ITEMS:
            if item[0] == key:
                return item
        return self._TUNING_MODE_ITEMS[0]

    def current_tuning_mode(self) -> str:
        return self._tuning_mode

    def is_debug_active(self) -> bool:
        """当前选中调试模式是否已切到 DEBUG source。"""
        return self._ring_source.get(self._active_source_ring(), PidSource.DEFAULT) == PidSource.DEBUG

    def has_any_debug_source(self) -> bool:
        """Return whether the device-confirmed source contains any DEBUG ring."""
        return any(source == PidSource.DEBUG
                   for source in self._ring_source.values())

    def has_unapplied_source_selection(self) -> bool:
        """Keep A2 refreshes from overwriting a user's unapplied radio draft."""
        if (self._source_set_pending is not None or self._source_set_queue or
                self._source_waiting_for_state):
            return True
        selected_debug_modes = []
        for ring_id, group in self._source_btn_groups.items():
            ring = PidRing(ring_id)
            option = self._source_option(ring, group.checkedId())
            if option is None:
                continue
            if PidSource(option[2]) != self._ring_source[ring]:
                return True
            if option[3] is not None:
                selected_debug_modes.append(option[3])
        if selected_debug_modes:
            selected = (self._selected_debug_mode
                        if self._selected_debug_mode in selected_debug_modes
                        else selected_debug_modes[0])
            return selected != self._tuning_mode
        return False

    def _active_debug_ring(self) -> int:
        return int(self._mode_def()[2])

    def _active_source_ring(self) -> PidRing:
        return self._mode_def()[3]

    def _remove_slider_group(self, ring: int):
        """移除指定 ring 的滑条组面板, 并取消该环的待发回读定时器。"""
        grp = self._debug_slider_groups.pop(ring, None)
        if grp is not None:
            self._slider_area.removeWidget(grp.container)
            grp.container.setParent(None)
            grp.container.deleteLater()
        # 取消该环所有待发的延迟回读
        for key in list(self._readback_timers.keys()):
            if key[0] == ring:
                t = self._readback_timers.pop(key)
                t.stop()
                t.deleteLater()

    def _sync_slider_groups(self):
        """根据当前调试模式和 source 状态同步滑条面板显隐。

        电流环 source 仍按固件三环模型共享, 但 UI 上将 D轴/ID 与 Q轴/IQ
        拆成两个调试模式, 每次只展开当前模式对应的实时 PID ring。
        """
        active_ring = self._active_debug_ring()
        active_source_ring = self._active_source_ring()
        new_rings = []
        for ring, _label in enumerate(self._DEBUG_RING_ITEMS):
            src = self._ring_source.get(active_source_ring, PidSource.DEFAULT)
            if ring == active_ring and src == PidSource.DEBUG:
                was_new = ring not in self._debug_slider_groups
                self._ensure_slider_group(ring)
                if was_new:
                    new_rings.append(ring)
            else:
                self._remove_slider_group(ring)
        # 容器显隐: 有 DEBUG 组时显示容器, 无时隐藏 (不占空间)
        # 波形控制行已内嵌于 _slider_container，随容器一起显隐，无需单独控制
        has_debug = len(self._debug_slider_groups) > 0
        if hasattr(self, '_slider_container'):
            self._slider_container.setVisible(has_debug)
        # 新增 DEBUG 组时立即请求一次全部参数 (不再周期轮询)
        if new_rings:
            for ring in new_rings:
                self._request_all_params(ring)
        self._refresh_observer_labels()

    def set_tuning_mode(self, mode: str):
        """设置当前 PID 调试模式。"""
        valid = {item[0] for item in self._TUNING_MODE_ITEMS}
        if mode not in valid:
            mode = "current_d"
        changed = mode != self._tuning_mode
        self._tuning_mode = mode
        self._sync_slider_groups()
        self._refresh_observer_labels()
        if changed:
            # 波形显示与调试模式解耦: 换整定环不再清空/重启波形采集
            self._set_last_op(f"调试模式: {self._mode_def()[1]}")
            self.pid_tuning_mode_changed.emit(self._tuning_mode)

    def _on_tuning_mode_id_clicked(self, idx: int):
        """Compatibility shim for scripts written before modes moved into source rows.

        The production UI has no standalone mode buttons; this method only maps
        an old numeric mode request to the corresponding source radio button and
        applies the same unified source workflow.
        """
        try:
            mode = self._TUNING_MODE_ITEMS[int(idx)][0]
        except (IndexError, TypeError, ValueError):
            return
        for ring, options in _SOURCE_OPTIONS.items():
            option = next((item for item in options if item[3] == mode), None)
            if option is None:
                continue
            button = self._source_btn_groups[int(ring)].button(option[0])
            if button is not None:
                button.setChecked(True)
            self._selected_debug_mode = mode
            self._on_source_apply_clicked()
            return

    @staticmethod
    def _source_option(ring: PidRing, option_id: int):
        for item in _SOURCE_OPTIONS[ring]:
            if item[0] == option_id:
                return item
        return None

    def _on_source_option_clicked(self, ring: PidRing, option_id: int):
        option = self._source_option(ring, option_id)
        if option is not None and option[3] is not None:
            self._selected_debug_mode = option[3]

    def set_pid_targets(self, targets: dict):
        """更新上位机最近一次下发的 PID 相关目标值。"""
        if not isinstance(targets, dict):
            return
        for key in ("id", "iq", "vel", "pos"):
            if key in targets:
                self._pid_targets[key] = targets.get(key)
        self._refresh_observer_labels()

    def update_pid_observation(self, feedback=None, targets: dict | None = None):
        """更新目标/反馈观测区。"""
        if targets is not None:
            self.set_pid_targets(targets)
        if feedback is not None:
            self._last_feedback = feedback
        self._refresh_observer_labels()

    def _waveform_buffer_size(self, rate_hz: int) -> int:
        """存储缓冲点数 = 时间跨度(秒)×有效采样率, 最少保留若干点。"""
        return max(2, int(self._WAVEFORM_SPAN_SEC * max(50, int(rate_hz))))

    def feed_capture_chunk(self, chunk: dict):
        """把 C9 高速采样分块转换成所选数据源的目标/反馈曲线。

        数据层: 存储缓冲按时间跨度(秒)配置, 高频时自动抽稀降采样,
        保证时间跨度并限制内存; 新点只做增量追加, 不触发全量重绘。
        采集与 DEBUG source 无关: 只要波形窗口在采, 数据就照常显示。
        """
        if (not isinstance(chunk, dict) or not self._waveform_enabled or
                self._plot_paused):
            return
        mask = int(chunk.get('mask', 0))
        rows = chunk.get('samples') or []
        bit_map = [i for i in range(8) if mask & (1 << i)]
        pair = get_pid_debug_profile(self._waveform_source).capture_channels
        try:
            ti, fi = bit_map.index(pair[0]), bit_map.index(pair[1])
        except ValueError:
            return
        # 高频自动降采样: 需要的点数超上限时, 每 stride 存 1 点, 跨度不缩水
        target_size = self._waveform_buffer_size(int(chunk.get('rate_hz', 0) or 0))
        decimate = 1
        if target_size > self._WAVEFORM_MAX_POINTS:
            decimate = target_size // self._WAVEFORM_MAX_POINTS + 1
            target_size = self._WAVEFORM_MAX_POINTS
        if self._plot_points.maxlen != target_size:
            keep = list(self._plot_points)
            self._plot_points = deque(keep, maxlen=target_size)

        # 只追加新点并增量推给可视窗口; 横坐标 = 采样点编号 (含抽稀跳过的点)
        new_points = []
        for idx, row in enumerate(rows):
            if ti >= len(row) or fi >= len(row):
                continue
            sample_no = self._plot_sample_index
            self._plot_sample_index += 1
            if decimate > 1 and idx % decimate != 0:
                continue
            target = float(row[ti]); feedback = float(row[fi])
            pt = (sample_no, target, feedback, target - feedback)
            self._plot_points.append(pt)
            new_points.append(pt)
        if new_points:
            self._waveform_window.append_points(new_points)

    @property
    def _waveform_enabled(self) -> bool:
        """波形采集只由波形窗口是否打开决定 (面板上无独立开关)。"""
        return self._waveform_window is not None

    def waveform_enabled(self) -> bool:
        return bool(self._waveform_enabled)

    def waveform_source(self) -> str:
        """当前波形上传数据源 (与 DEBUG 整定模式无关)。"""
        return str(self._waveform_source)

    def waveform_profile(self):
        """所选数据源对应的采集配置 (掩码/通道对/采样上限)。"""
        return get_pid_debug_profile(self._waveform_source)

    def waveform_rate_hz(self) -> int:
        """返回按所选数据源采样能力限幅后的高速采样上传频率。"""
        profile = self.waveform_profile()
        return max(50, min(int(self._waveform_rate_hz), profile.capture_rate_hz))

    def set_capture_active(self, active: bool):
        self._capture_active = bool(active)
        self._refresh_waveform_status()

    def _refresh_waveform_status(self):
        """把采集状态推送到波形子窗口 (面板上不再有状态标签)。"""
        window = self._waveform_window
        if window is None:
            return
        effective = self.waveform_rate_hz()
        requested = int(self._waveform_rate_hz)
        limited = effective != requested
        window.set_capture_status(self._capture_active, effective, limited)

    def _on_waveform_config_changed(self, rate_hz: int):
        """子窗口修改采样上传频率: 更新缓存并通知主窗口重启采集。"""
        rate = int(rate_hz)
        if rate == int(self._waveform_rate_hz):
            return
        self._waveform_rate_hz = rate
        self._clear_pid_waveform()
        self._refresh_waveform_status()
        self.pid_waveform_config_changed.emit(
            self._waveform_enabled, self.waveform_rate_hz())

    def _on_waveform_source_changed(self, source: str):
        """子窗口切换上传数据源: 换掩码并重启采集 (不影响 DEBUG source)。"""
        key = get_waveform_source_def(str(source))[0]
        if key == self._waveform_source:
            return
        self._waveform_source = key
        self._clear_pid_waveform()
        # 各数据源的固件采样上限不同, 切换后同步限幅并回写窗口控件
        if self._waveform_window is not None:
            self._waveform_window.set_rate(
                int(self._waveform_rate_hz),
                int(self.waveform_profile().capture_rate_hz))
        self._refresh_waveform_status()
        self.pid_waveform_config_changed.emit(
            self._waveform_enabled, self.waveform_rate_hz())

    def _on_plot_pause_toggled(self, checked: bool):
        """子窗口暂停/继续: 仅记录状态, 不回推窗口 (避免信号回环)。"""
        self._plot_paused = bool(checked)

    def _on_waveform_btn_clicked(self, checked: bool):
        """波形窗口按钮点击：打开/关闭窗口，同步按钮状态。"""
        if checked:
            self._open_pid_waveform_window()
        else:
            self.close_waveform_window()

    def _open_pid_waveform_window(self):
        if self._waveform_window is None:
            self._waveform_window = PidWaveformWindow()
            self._waveform_window.window_closed.connect(
                self._on_waveform_window_closed)
            self._waveform_window.clear_requested.connect(
                self._clear_pid_waveform)
            self._waveform_window.pause_toggled.connect(
                self._on_plot_pause_toggled)
            self._waveform_window.rate_changed.connect(
                self._on_waveform_config_changed)
            self._waveform_window.source_changed.connect(
                self._on_waveform_source_changed)
            self._waveform_window.set_opts(self._waveform_window_opts)
            # set_opts 可能恢复上次的数据源, 以窗口为准同步回面板
            self._waveform_source = self._waveform_window.source()
        # 数据源/采样上传频率控件都在窗口内: 同步当前值与该数据源的固件上限
        self._waveform_window.set_source(self._waveform_source)
        self._waveform_window.set_rate(
            int(self._waveform_rate_hz),
            int(self.waveform_profile().capture_rate_hz))
        self._waveform_window.set_paused(self._plot_paused)
        self._waveform_window.show()
        self._waveform_window.raise_()
        self._waveform_window.activateWindow()
        self._refresh_pid_waveform()
        self._refresh_waveform_status()
        self._sync_waveform_btn(True)
        # 打开窗口即开始采集 (与 DEBUG 无关)
        self.pid_waveform_config_changed.emit(True, self.waveform_rate_hz())

    def _on_waveform_window_closed(self):
        if self._waveform_window is not None:
            self._waveform_window_opts = self._waveform_window.get_opts()
        self._waveform_window = None
        self._sync_waveform_btn(False)
        # 关窗即停止采集, 避免下位机继续上传无消费者数据
        self.pid_waveform_config_changed.emit(False, self.waveform_rate_hz())

    def close_waveform_window(self):
        window = self._waveform_window
        if window is not None:
            try:
                window.close()
            except RuntimeError:
                self._waveform_window = None
                self.pid_waveform_config_changed.emit(
                    False, self.waveform_rate_hz())
        self._sync_waveform_btn(False)

    def _sync_waveform_btn(self, opened: bool):
        """同步「打开波形窗口」按钮高亮态 (已打开=高亮, 未打开=暗色)。"""
        btn = getattr(self, "_waveform_open_btn", None)
        if btn is None:
            return
        was = btn.blockSignals(True)   # 避免 setChecked 反向触发 clicked
        btn.setChecked(bool(opened))
        btn.blockSignals(was)
        btn.setText("波形窗口已打开" if opened else "打开波形窗口")

    def _clear_pid_waveform(self):
        """清空 PID 页内波形缓存。"""
        if not hasattr(self, "_plot_points"):
            return
        self._plot_points.clear()
        self._plot_sample_index = 0
        self._refresh_pid_waveform()

    def _current_observation_values(self):
        """返回当前模式下 (target, feedback, error, unit)。"""
        _key, _label, _ring, _source_ring, field, unit, _tip = self._mode_def()
        target = self._target_for_field(field)
        feedback = None
        if self._last_feedback is not None and hasattr(self._last_feedback, field):
            feedback = getattr(self._last_feedback, field)

        def as_float(value):
            if value is None:
                return None
            try:
                v = float(value)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(v):
                return None
            return v

        target_f = as_float(target)
        feedback_f = as_float(feedback)
        error_f = None
        if target_f is not None and feedback_f is not None:
            error_f = target_f - feedback_f
        return target_f, feedback_f, error_f, unit

    def _target_for_field(self, field: str):
        """遥测目标优先；未订阅目标组时回退到上位机最近下发值。"""
        ref_field = f"{field}_ref"
        fb = self._last_feedback
        if fb is not None:
            filled = getattr(fb, "filled_fields", ())
            if ref_field in filled:
                return getattr(fb, ref_field, None)
        return self._pid_targets.get(field)

    def _refresh_pid_waveform(self):
        if self._waveform_window is None:
            return
        self._waveform_window.set_points(self._plot_points)

    def _fmt_observer_value(self, value, unit: str) -> str:
        if value is None:
            return "--"
        try:
            return f"{float(value):.4g} {unit}"
        except (TypeError, ValueError):
            return "--"

    def _refresh_observer_labels(self):
        if not hasattr(self, "_obs_mode_label"):
            return
        key, label, _ring, source_ring, field, unit, _tip = self._mode_def()
        target, feedback, error, _unit = self._current_observation_values()
        src = self._ring_source.get(source_ring, PidSource.DEFAULT)
        self._obs_mode_label.setText(label)
        self._obs_target_label.setText(self._fmt_observer_value(target, unit))
        self._obs_feedback_label.setText(self._fmt_observer_value(feedback, unit))
        self._obs_error_label.setText(self._fmt_observer_value(error, unit))
        if src == PidSource.DEBUG:
            self._obs_source_label.setText("DEBUG 可写")
            self._obs_source_label.setStyleSheet(self._metric_value_qss("ok"))
        else:
            self._obs_source_label.setText(f"{_RING_CN[source_ring]}={_SOURCE_CN[src]}")
            self._obs_source_label.setStyleSheet(self._metric_value_qss("warn"))

    def _request_all_params(self, ring: int):
        """一次性请求指定环的全部 7 个参数 (0xA6)。"""
        for i in range(len(self._DEBUG_PARAM_ITEMS)):
            self.pid_param_get_requested.emit(ring, i + 1)

    def _schedule_readback(self, ring: int, param_type: int):
        """写入后延迟 100ms 请求一次该参数 (确认写入生效)。"""
        key = (ring, param_type)
        # 若已有定时器在等待, 取消旧的 (避免重复请求)
        old = self._readback_timers.pop(key, None)
        if old is not None:
            old.stop()
            old.deleteLater()
        t = QTimer(self)
        t.setSingleShot(True)
        t.setInterval(self._READBACK_DELAY_MS)
        t.timeout.connect(lambda r=ring, p=param_type, tmr=t:
                          self._on_readback_timeout(r, p, tmr))
        self._readback_timers[key] = t
        t.start()

    def _on_readback_timeout(self, ring: int, param_type: int, tmr):
        """延迟回读定时器到期: 发送一次 0xA6 请求。"""
        self._readback_timers.pop((ring, param_type), None)
        tmr.stop()
        tmr.deleteLater()
        self.pid_param_get_requested.emit(ring, param_type)

    def _on_slider_pressed(self, ring: int, param_type: int):
        """滑条开始拖动: 标记拖动中, 轮询时不覆盖。"""
        grp = self._debug_slider_groups.get(ring)
        if grp is not None:
            grp.set_dragging(param_type, True)

    def _on_slider_released(self, ring: int, param_type: int):
        """滑条松开: 触发 0xA5 写入, 100ms 后回读确认。需先勾选激活。"""
        grp = self._debug_slider_groups.get(ring)
        if grp is None:
            return
        grp.set_dragging(param_type, False)
        if not grp.is_enabled(param_type):  # 未勾选激活, 不写入
            return
        value = grp.slider_value(param_type)
        if value is None:
            return
        pname = self._param_name(param_type)
        ring_cn = self._ring_cn(ring)
        self._add_history(f"[TX] 滑条写入 {ring_cn}.{pname} = {value:.6g}")
        self.pid_param_set_requested.emit(ring, param_type, float(value))
        # 写入后延迟 100ms 回读该参数
        self._schedule_readback(ring, param_type)

    def _on_slider_range_changed(self, ring: int, param_type: int):
        """min/max 输入框修改: 重新映射滑条刻度 (不触发写入)。"""
        grp = self._debug_slider_groups.get(ring)
        if grp is not None:
            grp.remap_slider(param_type)

    def _on_slider_write_btn(self, ring: int, param_type: int):
        """写入按钮: 读取当前滑条值并发送 0xA5, 100ms 后回读确认。需先勾选激活。"""
        grp = self._debug_slider_groups.get(ring)
        if grp is None:
            return
        if not grp.is_enabled(param_type):  # 未勾选激活, 不写入
            return
        value = grp.slider_value(param_type)
        if value is None:
            return
        pname = self._param_name(param_type)
        ring_cn = self._ring_cn(ring)
        self._add_history(f"[TX] 按钮写入 {ring_cn}.{pname} = {value:.6g}")
        self.pid_param_set_requested.emit(ring, param_type, float(value))
        self._schedule_readback(ring, param_type)

    def _on_slider_read_all(self, ring: int):
        """全部读取按钮: 连续发送该环所有 7 个 0xA6 请求。"""
        if not self._link_active:
            return
        for i in range(len(self._DEBUG_PARAM_ITEMS)):
            self.pid_param_get_requested.emit(ring, i + 1)

    def _on_debug_commit_clicked(self):
        ring = self._active_debug_ring()
        if not self._link_active or not self.is_debug_active():
            self._set_last_op("需连接并进入 DEBUG 模式")
            return
        if self._current_top_fsm != int(TopFsm.IDLE):
            self._set_last_op("保存调试值前请先停止到 IDLE")
            return
        values = {ptype: value for (r, ptype), value in self._debug_values.items()
                  if r == ring}
        if not values:
            self._set_last_op("尚未读到当前环参数，请先全部读取")
            self._request_all_params(ring)
            return
        self.pid_debug_commit_requested.emit(ring, values)
        self._set_last_op("正在写入工程参数并校验")

    def _on_slider_write_all(self, ring: int):
        """全部写入按钮: 连续发送该环所有 7 个 0xA5 请求。"""
        if not self._link_active:
            return
        grp = self._debug_slider_groups.get(ring)
        if grp is None:
            return
        ring_cn = self._ring_cn(ring)
        for i in range(len(self._DEBUG_PARAM_ITEMS)):
            ptype = i + 1
            if not grp.is_enabled(ptype):  # 未勾选激活, 跳过
                continue
            value = grp.slider_value(ptype)
            if value is None:
                continue
            pname = self._param_name(ptype)
            self._add_history(f"[TX] 批量写入 {ring_cn}.{pname} = {value:.6g}")
            self.pid_param_set_requested.emit(ring, ptype, float(value))
            self._schedule_readback(ring, ptype)

    def _on_slider_restore_range(self, ring: int):
        """恢复默认范围按钮: 各参数恢复自适应初始范围。"""
        grp = self._debug_slider_groups.get(ring)
        if grp is None:
            return
        for ptype in range(1, len(self._DEBUG_PARAM_ITEMS) + 1):
            lo, hi = self._PARAM_DEFAULT_RANGE.get(ptype, (0.0, 1.0))
            grp.set_range(ptype, lo, hi)

    def _on_slider_read_all_click(self, ring: int):
        """全部读取按钮: 立即请求该环全部 7 个参数。"""
        self._request_all_params(ring)

    def _build_results_history_split(self, parent_layout):
        """结果/历史 Splitter: 上(PID 参数表) 下(操作历史)。

        - 历史默认隐藏, 隐藏时参数表 stretch 占满
        - 历史显示时 splitter 手柄可拖动, 两区皆可缩放
        - 历史开关按钮放到参数面板保存按钮同行最右(由 attach_params_panel 注入)
        - 输出框去掉 maxHeight, 填满整个历史子框
        """
        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(6)
        split.setStyleSheet(f"""
            QSplitter::handle:vertical {{
                background: {theme.hex('border')};
                margin: 1px 0;
            }}
            QSplitter::handle:vertical:hover {{
                background: {theme.hex('accent')};
            }}
        """)

        # 上: 参数表占位 (attach_params_panel 时替换)
        self._params_placeholder = QLabel("等待参数面板注入...")
        self._params_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._params_placeholder.setStyleSheet(
            f"color:{theme.hex('muted')}; padding:20px;")
        split.addWidget(self._params_placeholder)

        # 下: 操作历史
        self._history_grp = QGroupBox("操作历史")
        hv = QVBoxLayout(self._history_grp)
        hv.setContentsMargins(4, 4, 4, 4)
        hv.setSpacing(4)
        self._history_view = QTextEdit()
        self._history_view.setReadOnly(True)
        self._history_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._history_view.setStyleSheet(
            f"background:{theme.hex('panel_bg')}; color:{theme.hex('text')};"
            f"font-family: Consolas, 'Microsoft YaHei', monospace; font-size:12px;"
            f"border:1px solid {theme.hex('border')};")
        hv.addWidget(self._history_view, 1)

        op_row = QHBoxLayout()
        op_row.addStretch()
        self._btn_clear_history = QPushButton("清空历史")
        self._btn_clear_history.setFixedHeight(24)
        self._btn_clear_history.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear_history.setStyleSheet(self._normal_btn_qss())
        self._btn_clear_history.clicked.connect(self._on_clear_history)
        op_row.addWidget(self._btn_clear_history)
        hv.addLayout(op_row)

        split.addWidget(self._history_grp)

        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        split.setSizes([400, 100])
        split.setMinimumHeight(350)
        parent_layout.addWidget(split, 1)
        self._params_split = split

        # 默认隐藏历史, 让参数表占满
        self._history_grp.setVisible(False)
        self._history_visible = False

    # ========================================================================
    #  按钮样式
    # ========================================================================
    def _ok_btn_qss(self) -> str:
        return (f"QPushButton{{background:{theme.hex('ok')};"
                f"color:{theme.hex('ok_text')};"
                f"border:none;padding:6px 12px;border-radius:4px;font-weight:bold;}}"
                f"QPushButton:hover{{background:{theme.hex('accent')};}}")

    def _normal_btn_qss(self) -> str:
        return (f"QPushButton{{background:{theme.hex('input_bg')};"
                f"color:{theme.hex('text')};"
                f"border:1px solid {theme.hex('btn_border')};"
                f"padding:6px 12px;border-radius:4px;}}"
                f"QPushButton:hover{{border-color:{theme.hex('accent')};}}")

    def _waveform_btn_qss(self) -> str:
        """波形窗口按钮样式：未打开时暗色，打开时高亮（checked状态）。"""
        return (f"QPushButton{{background:{theme.hex('input_bg')};"
                f"color:{theme.hex('text')};"
                f"border:1px solid {theme.hex('btn_border')};"
                f"padding:6px 12px;border-radius:4px;}}"
                f"QPushButton:hover{{border-color:{theme.hex('accent')};}}"
                f"QPushButton:checked{{background:{theme.hex('accent')};"
                f"color:{theme.hex('card_top')};"
                f"border-color:{theme.hex('accent')};"
                f"font-weight:bold;}}")

    # ========================================================================
    #  按钮回调
    # ========================================================================
    def _on_autotune_clicked(self):
        """开始理论估计按钮回调。"""
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接, 无法整定")
            return
        # 计算 ring_select 位掩码: bit0=电流 bit1=速度 bit2=位置 (可组合)
        ring_select = 0
        for ring, chk in self._autotune_ring_checks.items():
            if chk.isChecked():
                ring_select |= (1 << int(ring))
        if ring_select == 0:
            self._add_history("[参数错误] 请至少选择一个环")
            self._set_last_op("未选择整定环")
            return
        cur_bw = self._cur_bw_spin.value()
        vel_bw = self._vel_bw_spin.value()
        pos_bw = self._pos_bw_spin.value()
        cur_eff = cur_bw if cur_bw > 0 else DEFAULT_CURRENT_BW_HZ
        vel_eff = vel_bw if vel_bw > 0 else DEFAULT_VELOCITY_BW_HZ
        pos_eff = pos_bw if pos_bw > 0 else DEFAULT_POSITION_BW_HZ
        if (ring_select & 0x03) == 0x03 and vel_eff > cur_eff * 0.2:
            self._set_last_op("带宽层级无效: 速度环需不高于电流环的1/5")
            return
        if (ring_select & 0x06) == 0x06 and pos_eff > vel_eff * 0.2:
            self._set_last_op("带宽层级无效: 位置环需不高于速度环的1/5")
            return
        rings_cn = "/".join(
            _RING_CN[r] for r, c in self._autotune_ring_checks.items() if c.isChecked())
        self._add_history(
            f"[TX] 理论估计 {rings_cn} 带宽=({cur_bw},{vel_bw},{pos_bw})Hz")
        self._set_last_op(f"理论估计中... ({rings_cn})")
        self.pid_autotune_requested.emit(ring_select, cur_bw, vel_bw, pos_bw)

    def _on_source_apply_clicked(self):
        """应用来源切换按钮回调: 逐环发送 0xA1。"""
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接")
            return
        if self._source_set_pending is not None:
            self._set_last_op("来源切换正在执行")
            return
        self._source_set_queue.clear()
        selected_debug_modes = []
        for ring_id, grp in self._source_btn_groups.items():
            ring = PidRing(ring_id)
            option = self._source_option(ring, grp.checkedId())
            if option is None:
                continue
            _option_id, _label, source, mode = option
            if mode is not None:
                selected_debug_modes.append(mode)
            src = int(source)
            if int(self._ring_source[ring]) == src:
                continue
            self._add_history(
                f"[TX] 来源切换 {_RING_CN[ring]} -> {_SOURCE_CN[PidSource(src)]}")
            self._source_set_queue.append((ring_id, src))
        if selected_debug_modes:
            mode = (self._selected_debug_mode
                    if self._selected_debug_mode in selected_debug_modes
                    else selected_debug_modes[0])
            self.set_tuning_mode(mode)
        if self._source_set_queue:
            if self._current_top_fsm is None:
                self._source_waiting_for_state = True
                self._add_history("[等待状态] 正在读取电机状态，状态返回后自动切换")
                self._set_last_op("状态未同步，已请求 C1 状态")
                self.pid_state_get_requested.emit()
                return
            allowed = (int(TopFsm.IDLE), int(TopFsm.READY))
            if self._current_top_fsm not in allowed:
                state = top_fsm_name(self._current_top_fsm)
                self._source_set_queue.clear()
                self._source_waiting_for_state = False
                self._add_history(
                    f"[状态拒绝] 当前为{state}，来源切换只允许待机/就绪状态")
                self._set_last_op(f"请先停止到待机/就绪（当前: {state}）")
                self.pid_state_get_requested.emit()
                return
            self._set_last_op(f"待切换 {len(self._source_set_queue)} 个环")
            self._source_waiting_for_state = False
            self._send_next_source_change()
        else:
            if selected_debug_modes:
                self._sync_slider_groups()
                self._set_last_op(f"调试模式已切换: {self._mode_def()[1]}")
            else:
                self._set_last_op("无来源变更")

    def _send_next_source_change(self):
        if self._source_set_pending is not None or not self._source_set_queue:
            return
        self._source_set_pending = self._source_set_queue.popleft()
        self.pid_source_set_requested.emit(*self._source_set_pending)

    # ========================================================================
    #  公共 API (主窗口调用)
    # ========================================================================
    def showEvent(self, event):
        """界面显示时: 无特殊处理 (不再周期轮询)。"""
        super().showEvent(event)

    def hideEvent(self, event):
        """界面隐藏(切换到其他tab)时: 取消所有待发的延迟回读定时器。"""
        super().hideEvent(event)
        self._cancel_all_readback()

    def _cancel_all_readback(self):
        """取消所有待发的延迟回读定时器。"""
        for key, t in list(self._readback_timers.items()):
            t.stop()
            t.deleteLater()
        self._readback_timers.clear()

    def set_link_active(self, active: bool):
        """连接状态变化: 断连时取消所有延迟回读, 不重置 source 状态。"""
        self._link_active = active
        if not active:
            self._cancel_all_readback()
        self._refresh_status()

    def update_state(self, top_fsm: int, run_state: int, ctrl_mode: int, enable: int):
        self._current_top_fsm = top_fsm
        self._refresh_status()
        if self._source_waiting_for_state and self._source_set_queue:
            if top_fsm in (int(TopFsm.IDLE), int(TopFsm.READY)):
                self._source_waiting_for_state = False
                self._set_last_op(
                    f"状态已同步，开始切换 {len(self._source_set_queue)} 个环")
                self._send_next_source_change()
            elif top_fsm != int(TopFsm.INIT):
                self._source_waiting_for_state = False
                self._source_set_queue.clear()
                self._set_last_op(
                    f"当前为{top_fsm_name(top_fsm)}，请先停止到待机/就绪")

    def on_autotune_result(self, result: dict):
        """0xA0 PID_AUTOTUNE 应答 (8字节 ACK)。"""
        ok = bool(result.get('ok', False))
        fail_cn = result.get('fail_reason_cn', '未知')
        ring_done = int(result.get('ring_select_done', 0))
        if ok:
            rings_cn = []
            if ring_done & 0x01:  # bit0=电流环
                self._ring_source[PidRing.CURRENT] = PidSource.AUTOTUNE
                rings_cn.append(_RING_CN[PidRing.CURRENT])
            if ring_done & 0x02:  # bit1=速度环
                self._ring_source[PidRing.VELOCITY] = PidSource.AUTOTUNE
                rings_cn.append(_RING_CN[PidRing.VELOCITY])
            if ring_done & 0x04:  # bit2=位置环
                self._ring_source[PidRing.POSITION] = PidSource.AUTOTUNE
                rings_cn.append(_RING_CN[PidRing.POSITION])
            self._refresh_source_badges()
            self._add_history(f"[RX] 理论估计成功 ({'/'.join(rings_cn)})")
            self._set_last_op(f"理论估计完成 ({'/'.join(rings_cn)})")
            # 自动刷新参数表 (读回 autotune 写入的 ControlParam)
            if self._params_panel:
                try:
                    self._params_panel.read_all()
                except Exception:
                    pass
        else:
            self._add_history(f"[RX] 理论估计失败: {fail_cn}")
            self._set_last_op(f"理论估计失败: {fail_cn}")

    def on_autotune_proposal(self, ring_mask: int, values: dict):
        """把上位机理论估计结果填入 PID 参数表修改值，不写设备。"""
        applied = 0
        if self._params_panel is not None:
            applied = self._params_panel.set_edit_values(values)
        rings = [_RING_CN[ring] for ring in PidRing
                 if ring_mask & (1 << int(ring))]
        text = "/".join(rings)
        self._add_history(
            f"[理论估计] {text} 已生成 {applied} 项修改值，尚未写入设备")
        self._set_last_op(f"理论值已填入修改值 ({text})，请检查后写入")

    def on_autotune_error(self, reason: str):
        """Show a host-side estimate failure without changing PID sources."""
        self._add_history(f"[理论估计失败] {reason}")
        self._set_last_op(f"理论估计失败: {reason}")

    def on_source_received(self, cur_src: int, vel_src: int, pos_src: int,
                           sync_selection: bool = True):
        """0xA2 PID_SOURCE_GET 应答 (3字节: cur/vel/pos source)。

        连接建立后主窗口主动查询, 回填三环 source 状态并同步单选按钮。
        """
        sources = {PidRing.CURRENT: cur_src, PidRing.VELOCITY: vel_src, PidRing.POSITION: pos_src}
        for ring, src in sources.items():
            try:
                self._ring_source[ring] = PidSource(src)
            except ValueError:
                self._ring_source[ring] = PidSource.DEFAULT
            # A2 always updates confirmed device state/badges. During an
            # unapplied draft or A1 transaction, preserve the radio selection.
            grp = self._source_btn_groups.get(int(ring))
            if sync_selection and grp is not None:
                source = self._ring_source[ring]
                option_id = int(source)
                if source == PidSource.DEBUG:
                    preferred = self._tuning_mode
                    candidates = [item for item in _SOURCE_OPTIONS[ring]
                                  if item[2] == PidSource.DEBUG]
                    chosen = next((item for item in candidates
                                   if item[3] == preferred), candidates[0])
                    option_id = chosen[0]
                    self._selected_debug_mode = chosen[3]
                btn = grp.button(option_id)
                if btn is not None:
                    btn.setChecked(True)
        self._refresh_source_badges()
        self._add_history(
            f"[RX] PID来源 电流={_SOURCE_CN[self._ring_source[PidRing.CURRENT]]} "
            f"速度={_SOURCE_CN[self._ring_source[PidRing.VELOCITY]]} "
            f"位置={_SOURCE_CN[self._ring_source[PidRing.POSITION]]}")
        # 同步 DEBUG 滑条面板显隐
        self._sync_slider_groups()


    def on_ack(self, cmd: int):
        """0xA1 PID_SOURCE_SET / 0xA5 PID_PARAM_SET 成功 ACK。"""
        if cmd == int(JmCmd.PID_SOURCE_SET):
            self._add_history("[RX] 来源切换成功")
            if self._source_set_pending is not None:
                ring_id, src = self._source_set_pending
                self._ring_source[PidRing(ring_id)] = PidSource(src)
                self._source_set_pending = None
            self._refresh_source_badges()
            self._sync_slider_groups()
            if self._source_set_queue:
                self._set_last_op(f"继续切换，剩余 {len(self._source_set_queue)} 个环")
                self._send_next_source_change()
            else:
                self._set_last_op("来源切换完成")
                self.pid_source_get_requested.emit()
        elif cmd == int(JmCmd.PID_PARAM_SET):
            self._add_history(f"[RX] 调试参数写入成功")

    def on_nack(self, cmd: int, err: int):
        """0xA1 PID_SOURCE_SET / 0xA5 PID_PARAM_SET 失败 NACK。"""
        if cmd == int(JmCmd.PID_SOURCE_SET):
            if err == 0x03:  # STATE_DENY
                state = (top_fsm_name(self._current_top_fsm)
                         if self._current_top_fsm is not None else "未知")
                self._add_history(
                    f"[RX] 来源切换失败: 当前状态不允许（{state}），请先停止运行")
                self._set_last_op(f"来源切换需待机/就绪（当前: {state}）")
                self.pid_state_get_requested.emit()
            else:
                self._add_history(f"[RX] 来源切换失败 err=0x{err:02X}")
                self._set_last_op(f"来源切换失败 (0x{err:02X})")
            self._source_set_pending = None
            self._source_set_queue.clear()
            self._source_waiting_for_state = False
        elif cmd == int(JmCmd.PID_PARAM_SET):
            if err == 0x03:  # STATE_DENY
                self._add_history("[RX] 调试参数写入失败: 对应环未处于 DEBUG 模式")
                self._set_last_op("需先切换到 DEBUG 模式")
            else:
                self._add_history(f"[RX] 调试参数写入失败 err=0x{err:02X}")
                self._set_last_op(f"写入失败 (0x{err:02X})")

    def on_pid_param_received(self, ring: int, param_type: int, value4: bytes):
        """0xA6 PID_PARAM_GET 应答 (6字节: ring + param_type + 4B value)。

        更新滑条面板位置。不更新状态栏 (轮询频繁, 避免刷屏)。
        """
        import struct as _struct
        if len(value4) >= 4:
            if param_type == 7:  # flags: uint32, 仅显示十进制
                val = _struct.unpack('<I', value4[:4])[0]
            else:
                val = _struct.unpack('<f', value4[:4])[0]
        else:
            val = None
        # 滑条面板更新
        if val is not None:
            self._debug_values[(ring, param_type)] = val
            grp = self._debug_slider_groups.get(ring)
            if grp is not None:
                grp.update_value(param_type, float(val), is_uint32=(param_type == 7))

    def attach_params_panel(self, panel):
        """注入 ParamPanel 实例, 替换占位控件, 并把「历史:显/隐」开关注入其 footer。"""
        self._params_panel = panel
        # 替换 Splitter 中的占位控件
        idx = self._params_split.indexOf(self._params_placeholder)
        if idx >= 0:
            self._params_placeholder.setParent(None)
            self._params_placeholder.deleteLater()
            self._params_placeholder = None
            self._params_split.insertWidget(idx, panel)

        # 历史开关按钮 — 注入到 ParamPanel 保存按钮同行最右 (同标定面板设计)
        self._btn_history_toggle = QPushButton("历史: 隐")
        self._btn_history_toggle.setCheckable(True)
        self._btn_history_toggle.setChecked(False)
        self._btn_history_toggle.setFixedHeight(24)
        self._btn_history_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_history_toggle.setToolTip("显示/隐藏 操作历史 (显示后可拖动手柄调整与参数表的高度比例)")
        self._btn_history_toggle.setStyleSheet(self._history_toggle_qss())
        self._btn_history_toggle.toggled.connect(self._on_history_toggle)
        panel.add_footer_widget(self._btn_history_toggle)
        # 恢复持久化的历史显隐态
        if getattr(self, "_history_visible", False):
            self._btn_history_toggle.setChecked(True)

        # 波形窗口开关 — 注入到参数表下方按钮行最右 (与参数导入/导出同一行)
        self._waveform_open_btn = QPushButton("打开波形窗口")
        self._waveform_open_btn.setCheckable(True)
        self._waveform_open_btn.setFixedHeight(24)
        self._waveform_open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._waveform_open_btn.setToolTip(
            "打开独立波形窗口; 数据源与采样上传频率在窗口顶部切换, 与调试模式无关")
        self._waveform_open_btn.setStyleSheet(self._waveform_btn_qss())
        self._waveform_open_btn.clicked.connect(self._on_waveform_btn_clicked)
        panel.add_footer_widget_right(self._waveform_open_btn)

        self._set_last_op("参数面板已加载")

    def _on_history_toggle(self, on: bool):
        """「历史:显/隐」开关 (注入到参数面板保存按钮同行右侧)。"""
        self._btn_history_toggle.setText("历史: 显" if on else "历史: 隐")
        self.set_history_visible(on)

    def set_history_visible(self, visible: bool):
        """显示/隐藏整个操作历史区(含 QGroupBox), 隐藏时参数表独占空间。"""
        self._history_visible = bool(visible)
        if hasattr(self, "_history_grp"):
            self._history_grp.setVisible(self._history_visible)
        if hasattr(self, "_params_split"):
            self._params_split.setStretchFactor(0, 4)
            self._params_split.setStretchFactor(1, 1 if self._history_visible else 0)

    def is_history_visible(self) -> bool:
        return self._history_visible

    def _on_clear_history(self):
        self._history.clear()
        if self._history_view:
            self._history_view.setPlainText("")

    def _history_toggle_qss(self) -> str:
        return (f"QPushButton{{background:{theme.hex('input_bg')};"
                f"color:{theme.hex('text')};"
                f"border:1px solid {theme.hex('border')};border-radius:3px;"
                f"padding:0 10px;font-size:12px;}}"
                f"QPushButton:hover{{border-color:{theme.hex('accent')};"
                f"color:{theme.hex('accent')};}}"
                f"QPushButton:checked{{background:{theme.hex('accent')};"
                f"color:{theme.hex('card_bottom')};"
                f"border-color:{theme.hex('accent')};font-weight:bold;}}")

    def apply_theme(self):
        """主题切换: 刷新所有控件样式。"""
        self._status_label.setStyleSheet(
            f"font-weight:bold; color:{theme.hex('text_strong')};")
        self._last_op_label.setStyleSheet(f"color:{theme.hex('muted')};")
        if hasattr(self, '_autotune_btn'):
            self._autotune_btn.setStyleSheet(self._ok_btn_qss())
        if hasattr(self, '_source_apply_btn'):
            self._source_apply_btn.setStyleSheet(self._normal_btn_qss())
        if hasattr(self, '_tuning_observer_box'):
            self._tuning_observer_box.setStyleSheet(self._observer_box_qss())
        for lbl in ("_obs_mode_label", "_obs_target_label", "_obs_feedback_label", "_obs_error_label"):
            if hasattr(self, lbl):
                getattr(self, lbl).setStyleSheet(self._metric_value_qss())
        if hasattr(self, "_obs_hint_label"):
            self._obs_hint_label.setStyleSheet(f"color:{theme.hex('muted')}; font-size:11px;")
        if hasattr(self, "_obs_source_label"):
            self._refresh_observer_labels()
        # splitter 手柄随主题刷新
        if hasattr(self, "_params_split"):
            self._params_split.setStyleSheet(f"""
                QSplitter::handle:vertical {{
                    background: {theme.hex('border')};
                    margin: 1px 0;
                }}
                QSplitter::handle:vertical:hover {{
                    background: {theme.hex('accent')};
                }}
            """)
        # 历史开关按钮主题刷新
        if hasattr(self, "_btn_history_toggle"):
            self._btn_history_toggle.setStyleSheet(self._history_toggle_qss())
        # 清空历史按钮
        if hasattr(self, "_btn_clear_history"):
            self._btn_clear_history.setStyleSheet(self._normal_btn_qss())
        # 历史输出框
        if self._history_view:
            self._history_view.setStyleSheet(
                f"background:{theme.hex('panel_bg')}; color:{theme.hex('text')};"
                f"font-family: Consolas, 'Microsoft YaHei', monospace; font-size:12px;"
                f"border:1px solid {theme.hex('border')};")
        # DEBUG 滑条面板主题刷新
        if hasattr(self, '_slider_container'):
            self._slider_container.setStyleSheet(self._slider_container_qss())
        if hasattr(self, '_waveform_open_btn'):
            self._waveform_open_btn.setStyleSheet(self._waveform_btn_qss())
        if hasattr(self, '_debug_commit_btn'):
            self._debug_commit_btn.setStyleSheet(self._normal_btn_qss())
        if hasattr(self, '_main_scroll'):
            self._main_scroll.setStyleSheet(self._main_scroll_qss())
        for grp in self._debug_slider_groups.values():
            grp.apply_theme()
        if self._waveform_window is not None:
            self._waveform_window.apply_theme()
        self._refresh_source_badges()
        self._refresh_status()
        if self._params_panel and hasattr(self._params_panel, 'apply_theme'):
            self._params_panel.apply_theme()

    def get_opts(self) -> dict:
        """收集可持久化的 UI 配置: 历史显隐 + 内嵌参数面板列宽。"""
        opts = {}
        try:
            opts["history_visible"] = bool(self._history_visible)
        except Exception:
            pass
        try:
            opts["tuning_mode"] = self._tuning_mode
        except Exception:
            pass
        # 频率/数据源控件在子窗口, 但值由面板持有 (关窗后仍是下次采集的配置)
        opts["waveform_rate_hz"] = int(self._waveform_rate_hz)
        opts["waveform_source"] = str(self._waveform_source)
        if self._waveform_window is not None:
            self._waveform_window_opts = self._waveform_window.get_opts()
        opts["waveform_window"] = dict(self._waveform_window_opts)
        if self._params_panel is not None:
            try:
                opts["params_panel"] = self._params_panel.get_opts()
            except Exception:
                pass
        return opts

    def set_opts(self, opts: dict):
        """启动时套用配置 (容错)。"""
        if not isinstance(opts, dict):
            return
        if "history_visible" in opts:
            try:
                vis = bool(opts["history_visible"])
                self._history_visible = vis
                if hasattr(self, "_btn_history_toggle"):
                    self._btn_history_toggle.setChecked(vis)
                else:
                    self.set_history_visible(vis)
            except Exception:
                pass
        if "tuning_mode" in opts:
            try:
                self.set_tuning_mode(str(opts["tuning_mode"]))
            except Exception:
                pass
        if "waveform_rate_hz" in opts:
            try:
                self._waveform_rate_hz = max(50, min(int(opts["waveform_rate_hz"]), 5000))
            except (TypeError, ValueError):
                pass
        if opts.get("waveform_source"):
            self._waveform_source = get_waveform_source_def(
                str(opts["waveform_source"]))[0]
        if isinstance(opts.get("waveform_window"), dict):
            self._waveform_window_opts = dict(opts["waveform_window"])
        if self._params_panel is not None and isinstance(opts.get("params_panel"), dict):
            try:
                self._params_panel.set_opts(opts["params_panel"])
            except Exception:
                pass

    # ========================================================================
    #  内部辅助
    # ========================================================================
    def _refresh_status(self):
        if not self._link_active:
            self._status_label.setText("未连接")
            self._status_label.setStyleSheet(
                f"font-weight:bold; color:{theme.hex('danger')};")
            return
        if self._current_top_fsm in (int(TopFsm.IDLE), int(TopFsm.READY)):
            state = top_fsm_name(self._current_top_fsm)
            self._status_label.setText(f"已连接 · {state}可切换来源")
            self._status_label.setStyleSheet(
                f"font-weight:bold; color:{theme.hex('ok')};")
        else:
            self._status_label.setText(
                f"已连接 · top_fsm={self._current_top_fsm}")
            self._status_label.setStyleSheet(
                f"font-weight:bold; color:{theme.hex('warn')};")

    def _refresh_source_badges(self):
        for ring, lbl in self._source_labels.items():
            src = self._ring_source[ring]
            lbl.setText(_SOURCE_CN[src])
            lbl.setStyleSheet(self._source_badge_qss(src))

    def _set_last_op(self, text: str):
        if self._last_op_label:
            self._last_op_label.setText(text)

    def _add_history(self, text: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._history.append(f"{ts}  {text}")
        if self._history_view:
            self._history_view.setPlainText("\n".join(self._history))
            sb = self._history_view.verticalScrollBar()
            if sb:
                sb.setValue(sb.maximum())


# ============================================================================
#  _SliderGroup: 单个 ring 的调试滑条面板 (7 参数行 + 底部操作按钮)
# ============================================================================
class _SliderGroup:
    """单个 ring 的调试滑条组状态 + UI 构建。

    每行: 参数名 | QSlider | 当前值 | min输入 | max输入 | 写入按钮
    滑条 int 0~_SLIDER_STEPS 映射到 [min, max] 浮点值。
    """

    def __init__(self, ring: int, panel: 'PidPanel'):
        self.ring = ring
        self.panel = panel
        # param_type -> 行控件 dict
        self.rows = {}
        self._build_ui()

    def _build_ui(self):
        ring_cn = self.panel._ring_cn(self.ring)
        self.container = QFrame()
        self.container.setObjectName("debugSliderGroup")
        self.container.setStyleSheet(
            f"QFrame#debugSliderGroup{{border:1px solid {theme.hex('accent')};"
            f"border-radius:6px;"
            f"background:{theme.hex('card_top')};}}")
        v = QVBoxLayout(self.container)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)

        # ---- 标题栏 ----
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        title = QLabel(f"{ring_cn} · 实时调试")
        title.setStyleSheet(
            f"font-weight:bold; color:{theme.hex('text_strong')}; font-size:13px;")
        hdr.addWidget(title)
        badge = QLabel("DEBUG")
        badge.setStyleSheet(
            f"background:{theme.hex('accent')}; color:{theme.hex('card_top')};"
            f"padding:1px 8px; border-radius:8px; font-size:11px; font-weight:bold;")
        hdr.addWidget(badge)
        hdr.addStretch()
        self.poll_label = QLabel(f"写入后 {self.panel._READBACK_DELAY_MS}ms 回读确认")
        self.poll_label.setStyleSheet(f"color:{theme.hex('muted')}; font-size:11px;")
        hdr.addWidget(self.poll_label)
        v.addLayout(hdr)

        # ---- 表头 ----
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(2)
        headers = ["启用", "参数", "滑条 (拖动松开即写入)", "当前值", "min", "max", "写入"]
        col_widths = [40, 90, 0, 70, 60, 60, 48]
        for col, (htext, w) in enumerate(zip(headers, col_widths)):
            lbl = QLabel(htext)
            lbl.setStyleSheet(
                f"color:{theme.hex('muted')}; font-size:11px;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(lbl, 0, col)
            if w > 0:
                grid.setColumnMinimumWidth(col, w)

        # ---- 7 行滑条 ----
        for row_idx, (pname, ptype) in enumerate(self.panel._DEBUG_PARAM_ITEMS, start=1):
            lo, hi = self.panel._PARAM_DEFAULT_RANGE.get(ptype, (0.0, 1.0))

            # 激活勾选 (默认不勾选, 未勾选时滑条/写入按钮禁用, 保证安全)
            enable_chk = QCheckBox()
            enable_chk.setChecked(False)
            enable_chk.setStyleSheet(
                f"QCheckBox::indicator{{width:14px;height:14px;"
                f"border:1px solid {theme.hex('btn_border')};border-radius:3px;"
                f"background:{theme.hex('input_bg')};}}"
                f"QCheckBox::indicator:checked{{background:{theme.hex('accent')};"
                f"border:1px solid {theme.hex('accent')};}}")
            grid.addWidget(enable_chk, row_idx, 0)

            # 参数名
            name_lbl = QLabel(pname)
            name_lbl.setStyleSheet(
                f"color:{theme.hex('text_strong')};"
                f"font-size:12px; font-weight:bold;")
            grid.addWidget(name_lbl, row_idx, 1)

            # 滑条
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, self.panel._SLIDER_STEPS)
            slider.setValue(0)
            slider.setEnabled(False)  # 默认禁用, 勾选后启用
            slider.setStyleSheet(self._slider_qss())
            grid.addWidget(slider, row_idx, 2)

            # 当前值
            val_lbl = QLabel("--")
            val_lbl.setStyleSheet(
                f"color:{theme.hex('accent')}; font-family:Consolas,monospace;"
                f"font-size:12px; font-weight:bold;")
            val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val_lbl.setMinimumWidth(70)
            grid.addWidget(val_lbl, row_idx, 3)

            # min 输入 (移除箭头按钮, 节省空间给数字显示)
            min_spin = QDoubleSpinBox()
            min_spin.setRange(-1e9, 1e9)
            min_spin.setDecimals(6)
            min_spin.setValue(lo)
            min_spin.setMinimumWidth(68)
            min_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
            min_spin.setStyleSheet(self._spin_qss())
            grid.addWidget(min_spin, row_idx, 4)

            # max 输入 (移除箭头按钮)
            max_spin = QDoubleSpinBox()
            max_spin.setRange(-1e9, 1e9)
            max_spin.setDecimals(6)
            max_spin.setValue(hi)
            max_spin.setMinimumWidth(68)
            max_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
            max_spin.setStyleSheet(self._spin_qss())
            grid.addWidget(max_spin, row_idx, 5)

            # 写入按钮
            write_btn = QPushButton("0xA5")
            write_btn.setFixedWidth(48)
            write_btn.setEnabled(False)  # 默认禁用, 勾选后启用
            write_btn.setStyleSheet(self._write_btn_qss())
            grid.addWidget(write_btn, row_idx, 6)

            # 存储行控件
            self.rows[ptype] = {
                'enable_chk': enable_chk,
                'slider': slider,
                'val_lbl': val_lbl,
                'min_spin': min_spin,
                'max_spin': max_spin,
                'write_btn': write_btn,
                'dragging': False,
                'initialized': False,
            }

            # 信号绑定 (用默认参数捕获 ptype)
            enable_chk.toggled.connect(
                lambda checked, pt=ptype: self._on_enable_toggled(pt, checked))
            slider.sliderPressed.connect(
                lambda pt=ptype: self.panel._on_slider_pressed(self.ring, pt))
            slider.sliderReleased.connect(
                lambda pt=ptype: self.panel._on_slider_released(self.ring, pt))
            min_spin.valueChanged.connect(
                lambda _, pt=ptype: self.panel._on_slider_range_changed(self.ring, pt))
            max_spin.valueChanged.connect(
                lambda _, pt=ptype: self.panel._on_slider_range_changed(self.ring, pt))
            write_btn.clicked.connect(
                lambda _, pt=ptype: self.panel._on_slider_write_btn(self.ring, pt))

        grid.setColumnStretch(2, 1)  # 滑条列占满剩余空间
        v.addLayout(grid)

        # ---- 底部操作栏 ----
        footer = QHBoxLayout()
        footer.setSpacing(6)
        btn_read_all = QPushButton("全部读取 (0xA6×7)")
        btn_write_all = QPushButton("全部写入 (0xA5×7)")
        btn_restore = QPushButton("恢复默认范围")
        for btn in (btn_read_all, btn_write_all, btn_restore):
            btn.setStyleSheet(self.panel._normal_btn_qss())
            btn.setFixedHeight(26)
        btn_read_all.clicked.connect(lambda: self.panel._on_slider_read_all_click(self.ring))
        btn_write_all.clicked.connect(lambda: self.panel._on_slider_write_all(self.ring))
        btn_restore.clicked.connect(lambda: self.panel._on_slider_restore_range(self.ring))
        footer.addWidget(btn_read_all)
        footer.addWidget(btn_write_all)
        footer.addWidget(btn_restore)
        footer.addStretch()
        v.addLayout(footer)

    # ---- 值映射 ----
    def _on_enable_toggled(self, ptype: int, checked: bool):
        """激活勾选变化: 启用/禁用该行的滑条和写入按钮。"""
        row = self.rows.get(ptype)
        if row is None:
            return
        row['slider'].setEnabled(checked)
        row['write_btn'].setEnabled(checked)
        # 未勾选时清除拖动标记
        if not checked:
            row['dragging'] = False

    def is_enabled(self, ptype: int) -> bool:
        """该参数是否已勾选激活。"""
        row = self.rows.get(ptype)
        return row['enable_chk'].isChecked() if row else False

    def _value_to_slider(self, ptype: int, value: float) -> int:
        """浮点值 -> 滑条 int 位置。"""
        row = self.rows.get(ptype)
        if row is None:
            return 0
        lo = row['min_spin'].value()
        hi = row['max_spin'].value()
        if hi <= lo:
            return 0
        pos = int((value - lo) / (hi - lo) * self.panel._SLIDER_STEPS)
        return max(0, min(self.panel._SLIDER_STEPS, pos))

    def slider_value(self, ptype: int):
        """滑条当前位置 -> 浮点值 (flags 返回 uint32)。"""
        row = self.rows.get(ptype)
        if row is None:
            return None
        pos = row['slider'].value()
        lo = row['min_spin'].value()
        hi = row['max_spin'].value()
        if hi <= lo:
            return lo
        val = lo + pos / self.panel._SLIDER_STEPS * (hi - lo)
        if ptype == 7:  # flags: uint32
            return float(int(max(0, min(0xFFFFFFFF, val))))
        return val

    def remap_slider(self, ptype: int):
        """min/max 变更后重新映射滑条位置 (保持当前值不变)。"""
        row = self.rows.get(ptype)
        if row is None:
            return
        # 读取当前显示值
        txt = row['val_lbl'].text()
        if txt == "--":
            return
        try:
            val = float(txt)
        except ValueError:
            return
        pos = self._value_to_slider(ptype, val)
        # 临时阻塞信号避免触发 released
        slider = row['slider']
        was = slider.blockSignals(True)
        slider.setValue(pos)
        slider.blockSignals(was)

    def set_range(self, ptype: int, lo: float, hi: float):
        """设置 min/max (不触发 valueChanged 回调)。"""
        row = self.rows.get(ptype)
        if row is None:
            return
        row['min_spin'].blockSignals(True)
        row['max_spin'].blockSignals(True)
        row['min_spin'].setValue(lo)
        row['max_spin'].setValue(hi)
        row['min_spin'].blockSignals(False)
        row['max_spin'].blockSignals(False)
        self.remap_slider(ptype)

    def set_dragging(self, ptype: int, on: bool):
        row = self.rows.get(ptype)
        if row is not None:
            row['dragging'] = on

    def is_dragging(self, ptype: int) -> bool:
        row = self.rows.get(ptype)
        return row['dragging'] if row else False

    def update_value(self, ptype: int, value: float, is_uint32: bool = False):
        """0xA6 应答回调: 更新滑条位置 + 当前值显示。

        首次更新时按 读回值×2 自适应 max (不小于 0.001)。
        用户拖动中不覆盖滑条位置。
        """
        row = self.rows.get(ptype)
        if row is None:
            return
        # 显示值 (flags 仅显示十进制, 不显示16进制)
        if is_uint32:
            uval = int(max(0, min(0xFFFFFFFF, value)))
            val_str = f"{uval}"
        else:
            val_str = f"{value:.6g}"
        row['val_lbl'].setText(val_str)
        # 首次初始化: 按 读回值×2 自适应 max
        if not row['initialized']:
            row['initialized'] = True
            lo = row['min_spin'].value()
            if is_uint32:
                new_hi = max(value * 2, 65535.0, lo + 1)
            else:
                new_hi = max(abs(value) * 2, 0.001, lo + 0.001)
            self.set_range(ptype, lo, new_hi)
        # 更新滑条位置 (用户未拖动时)
        if not row['dragging']:
            pos = self._value_to_slider(ptype, value)
            slider = row['slider']
            was = slider.blockSignals(True)
            slider.setValue(pos)
            slider.blockSignals(was)

    # ---- 样式 ----
    def _slider_qss(self) -> str:
        return (f"QSlider::groove:horizontal{{"
                f"border:1px solid {theme.hex('border')};"
                f"height:4px;background:{theme.hex('card_bottom')};"
                f"border-radius:2px;}}"
                f"QSlider::handle:horizontal{{"
                f"background:{theme.hex('input_bg')};"
                f"border:2px solid {theme.hex('accent')};"
                f"width:12px;height:12px;margin:-6px 0;border-radius:8px;}}"
                f"QSlider::sub-page:horizontal{{"
                f"background:{theme.hex('accent')};border-radius:2px;}}")

    def _spin_qss(self) -> str:
        return (f"QDoubleSpinBox{{background:{theme.hex('input_bg')};"
                f"border:1px solid {theme.hex('btn_border')};"
                f"border-radius:3px;padding:1px 2px;"
                f"font-size:11px;color:{theme.hex('text')};}}")

    def _write_btn_qss(self) -> str:
        return (f"QPushButton{{background:{theme.hex('accent')};color:{theme.hex('card_top')};"
                f"border:none;border-radius:3px;padding:2px 6px;"
                f"font-size:11px;font-weight:bold;}}"
                f"QPushButton:hover{{opacity:0.85;}}")

    def apply_theme(self):
        """主题切换时刷新所有控件样式。"""
        self.container.setStyleSheet(
            f"QFrame#debugSliderGroup{{border:1px solid {theme.hex('accent')};"
            f"border-radius:6px;background:{theme.hex('card_top')};}}")
        for row in self.rows.values():
            row['slider'].setStyleSheet(self._slider_qss())
            row['min_spin'].setStyleSheet(self._spin_qss())
            row['max_spin'].setStyleSheet(self._spin_qss())
            row['write_btn'].setStyleSheet(self._write_btn_qss())
            row['val_lbl'].setStyleSheet(
                f"color:{theme.hex('accent')}; font-family:Consolas,monospace;"
                f"font-size:12px; font-weight:bold;")
