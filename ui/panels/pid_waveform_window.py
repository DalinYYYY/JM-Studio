"""独立 PID 目标/反馈波形窗口。

三层能力:
- 数据层: 存储缓冲与可视区解耦。面板持全量历史, 本窗口只负责渲染;
  增量追加 + 节流渲染 + 可视区降采样, 高频也不至于全量重绘变卡。
- 视觉层: 目标—反馈差值半透明填充、误差独立右轴、误差超限高亮、信号显示开关。
- 交互层: 十字光标读数、测量游标(Δt/ΔV)、信号统计、暂停后回看、Autoscale/轴属性。
"""

from bisect import bisect_left

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QMenu,
    QPushButton, QRadioButton, QSpinBox, QVBoxLayout, QWidget,
)

from core.pid_debug_profile import (
    PID_WAVEFORM_DEFAULT_SOURCE, PID_WAVEFORM_SOURCES, get_waveform_source_def,
)
from ui.theme import theme
from ui.plot_history import PointHistory

try:
    import pyqtgraph as pg
except Exception:
    pg = None


# 右键菜单英 -> 中 文本映射 (覆盖 ViewBox 菜单项与轴控制模板控件)
_ZH_MENU_MAP = {
    "ViewBox options": "视框选项",
    "View All": "查看全部",
    "X axis": "X 轴",
    "Y axis": "Y 轴",
    "Mouse Mode": "鼠标模式",
    "3 button": "3 键平移",
    "1 button": "1 键缩放",
    "Auto": "自动",
    "Manual": "手动",
    "Invert Axis": "反转坐标轴",
    "Mouse Enabled": "鼠标可用",
    "Visible Data Only": "仅可见数据",
    "Auto Pan Only": "仅自动平移",
    "Link Axis:": "联动轴:",
}


def _translate_widget_texts(widget):
    """把 QWidgetAction 内嵌控件(轴控制模板)的文本替换为中文。"""
    for lbl in widget.findChildren((QLabel, QCheckBox, QRadioButton)):
        txt = lbl.text().replace("&", "")
        if txt in _ZH_MENU_MAP:
            lbl.setText(_ZH_MENU_MAP[txt])


def _translate_menu_recursive(menu):
    """递归把 pyqtgraph 右键菜单项/子菜单/内嵌控件文本替换为中文。"""
    if menu is None:
        return
    title = menu.title().replace("&", "")
    if title in _ZH_MENU_MAP:
        menu.setTitle(_ZH_MENU_MAP[title])
    for action in menu.actions():
        if action.isSeparator():
            continue
        atext = action.text().replace("&", "")
        if atext in _ZH_MENU_MAP:
            action.setText(_ZH_MENU_MAP[atext])
        # PyQt6 的 QAction 已移除 defaultWidget(), 用 hasattr 兼容
        if hasattr(action, "defaultWidget"):
            w = action.defaultWidget()
            if w is not None:
                _translate_widget_texts(w)
        sub = action.menu()
        if sub is not None:
            _translate_menu_recursive(sub)


class PidWaveformWindow(QWidget):
    """独立 PID 目标/反馈波形窗口, 与调试模式解耦。"""

    window_closed = pyqtSignal()
    clear_requested = pyqtSignal()
    pause_toggled = pyqtSignal(bool)   # 暂停/继续 (面板据此停更缓存)
    receive_toggled = pyqtSignal(bool) # 波形接收开关
    rate_changed = pyqtSignal(int)     # 采样上传频率(Hz) 变更
    source_changed = pyqtSignal(str)   # 上传数据源变更
    buffer_changed = pyqtSignal(int)  # 主机历史存储深度变更

    # 误差曲线颜色(比 warn 更浅的琥珀色, 置于最底层)
    _ERROR_COLOR = "#E8B04B"
    _ERROR_HOT_COLOR = "#E8463A"       # 误差超限高亮
    # 实时跟随时的默认视口跨度；关闭跟随后 X 轴完全由用户交互控制。
    _VIEW_SPAN_POINTS = 2000
    _MAX_DISPLAY_POINTS = 20000
    _MIN_BUFFER_POINTS = 1000
    _MAX_BUFFER_POINTS = 2000000
    _DEFAULT_BUFFER_POINTS = 200000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PID 波形显示")
        self.setWindowFlags(Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumSize(900, 520)
        self._paused = False
        self._receiving = False
        self._follow = True
        self._y_auto = True
        self._buffer_max = self._DEFAULT_BUFFER_POINTS
        self._history_rate_hz = 0.0
        self._x_view_initialized = False
        self._suppress_view_signal = False
        self._points = PointHistory(maxlen=self._buffer_max)
        self._source = PID_WAVEFORM_DEFAULT_SOURCE
        self._rate_ceiling = 5000
        self._status_color_key = "muted"
        self._err_threshold = 0.0
        self._wave_style = "line"        # line / line+dot / scatter
        self._scatter = False
        self._sym_opt = {}
        self._line_width = 2           # 目标/反馈曲线线宽 (右键菜单可调)
        self._dot_size = 4             # 点线/散点模式点大小 (右键菜单可调)
        self._render_timer = None      # 由 _build 创建, 先置 None 供 _schedule_render 守卫
        self._build()

    # ========================================================================
    #  UI 构建
    # ========================================================================
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        def add_separator(row: QHBoxLayout):
            separator = QFrame()
            separator.setFrameShape(QFrame.Shape.VLine)
            separator.setFrameShadow(QFrame.Shadow.Plain)
            separator.setFixedHeight(22)
            separator.setStyleSheet(f"color:{theme.hex('border')};")
            row.addWidget(separator)

        def fit_numeric_input(spin, sample_text: str):
            # 预留左右内边距和步进按钮宽度，保证最大值仍完整可见。
            text_width = spin.fontMetrics().horizontalAdvance(sample_text)
            spin.setFixedWidth(max(spin.sizeHint().width(), text_width + 46))
            spin.setAlignment(Qt.AlignmentFlag.AlignRight)

        bar = QHBoxLayout()
        bar.setSpacing(8)

        # 上传数据源
        source_title = QLabel("数据源:")
        source_title.setStyleSheet(f"color:{theme.hex('muted')};")
        bar.addWidget(source_title)
        self._source_combo = QComboBox()
        self._source_combo.setMinimumWidth(120)
        self._source_combo.setToolTip(
            "选择上传并显示的目标/反馈通道；与 PID 调试模式相互独立")
        for key, label, unit in PID_WAVEFORM_SOURCES:
            self._source_combo.addItem(f"{label} [{unit}]", key)
        source_width = max(
            self._source_combo.fontMetrics().horizontalAdvance(
                self._source_combo.itemText(index))
            for index in range(self._source_combo.count()))
        self._source_combo.setMinimumWidth(max(120, source_width + 36))
        self._source_combo.currentIndexChanged.connect(self._on_source_index_changed)
        bar.addWidget(self._source_combo)
        add_separator(bar)

        # 接收开关与采样上传频率组成连续的采集操作组。
        self._receive_btn = QPushButton("接收波形")
        self._receive_btn.setCheckable(True)
        self._receive_btn.setToolTip("开始/停止 TRACE 高速波形主动上传")
        self._receive_btn.toggled.connect(self._on_receive_toggled)
        bar.addWidget(self._receive_btn)
        rate_title = QLabel("上传频率:")
        rate_title.setStyleSheet(f"color:{theme.hex('muted')};")
        bar.addWidget(rate_title)
        self._rate_spin = QSpinBox()
        self._rate_spin.setRange(50, 5000)
        self._rate_spin.setSingleStep(50)
        self._rate_spin.setValue(1000)
        self._rate_spin.setToolTip(
            "高速采样及波形数据上传频率；会按当前控制环上限自动限幅")
        fit_numeric_input(self._rate_spin, "5000")
        self._rate_spin.valueChanged.connect(self._on_rate_changed)
        bar.addWidget(self._rate_spin)
        self._rate_unit_label = QLabel("Hz")
        self._rate_unit_label.setStyleSheet(f"color:{theme.hex('muted')};")
        bar.addWidget(self._rate_unit_label)
        add_separator(bar)

        # 存储总量放在接收开关和采样上传频率之后。
        buf_title = QLabel("存储深度:")
        buf_title.setStyleSheet(f"color:{theme.hex('muted')};")
        bar.addWidget(buf_title)
        self._buf_spin = QSpinBox()
        self._buf_spin.setRange(self._MIN_BUFFER_POINTS, self._MAX_BUFFER_POINTS)
        self._buf_spin.setSingleStep(10000)
        self._buf_spin.setValue(self._buffer_max)
        self._buf_spin.setToolTip(
            "窗口内可存储的最大数据点总量；超出后丢弃最早的数据")
        fit_numeric_input(self._buf_spin, "1000000")
        self._buf_spin.valueChanged.connect(self._on_buffer_changed)
        bar.addWidget(self._buf_spin)
        self._buf_unit_label = QLabel("点")
        self._buf_unit_label.setStyleSheet(f"color:{theme.hex('muted')};")
        bar.addWidget(self._buf_unit_label)
        bar.addStretch()

        layout.addLayout(bar)

        # 第二控制行: 显示控制 / 显示方式 / 误差阈值 / 辅助操作
        row2 = QHBoxLayout()
        row2.setSpacing(8)

        # 显示选项
        row2.addWidget(QLabel("显示方式:"))
        self._style_combo = QComboBox()
        self._style_combo.addItem("折线", "line")
        self._style_combo.addItem("点线", "line+dot")
        self._style_combo.addItem("散点", "scatter")
        self._style_combo.setMinimumWidth(
            self._style_combo.fontMetrics().horizontalAdvance("显示方式") + 36)
        self._style_combo.setToolTip("波形曲线的绘制方式: 折线 / 点线 / 散点")
        self._style_combo.currentIndexChanged.connect(self._on_style_changed)
        row2.addWidget(self._style_combo)
        self._follow_check = QCheckBox("跟随")
        self._follow_check.setChecked(True)
        self._follow_check.toggled.connect(self._on_follow_toggled)
        row2.addWidget(self._follow_check)
        self._y_auto_check = QCheckBox("Y自适应")
        self._y_auto_check.setChecked(True)
        self._y_auto_check.toggled.connect(self._on_y_auto_toggled)
        row2.addWidget(self._y_auto_check)
        add_separator(row2)

        # 分析选项
        row2.addWidget(QLabel("误差阈值:"))
        self._err_thr_spin = QDoubleSpinBox()
        self._err_thr_spin.setRange(0.0, 1e6)
        self._err_thr_spin.setDecimals(3)
        self._err_thr_spin.setValue(0.0)
        self._err_thr_spin.setSpecialValueText("关")
        self._err_thr_spin.setToolTip("误差绝对值超过该值则误差曲线高亮(红色)")
        fit_numeric_input(self._err_thr_spin, "1000000.000")
        self._err_thr_spin.valueChanged.connect(self._on_err_threshold_changed)
        row2.addWidget(self._err_thr_spin)
        self._stats_check = QCheckBox("统计")
        self._stats_check.setChecked(True)
        self._stats_check.toggled.connect(self._on_stats_toggled)
        row2.addWidget(self._stats_check)
        add_separator(row2)

        # 操作按钮
        self._pause_btn = QPushButton("暂停")
        self._pause_btn.setCheckable(True)
        self._pause_btn.toggled.connect(self._on_pause_toggled)
        row2.addWidget(self._pause_btn)
        self._fit_all_btn = QPushButton("适配全量")
        self._fit_all_btn.setToolTip("Y 轴适配当前全部已存储数据")
        self._fit_all_btn.clicked.connect(self._on_fit_all)
        row2.addWidget(self._fit_all_btn)
        self._latest_btn = QPushButton("回到最新")
        self._latest_btn.setToolTip("恢复跟随最新数据，并保留当前 X 轴缩放跨度")
        self._latest_btn.clicked.connect(self._on_latest_clicked)
        row2.addWidget(self._latest_btn)
        self._clear_btn = QPushButton("清空")
        self._clear_btn.clicked.connect(self.clear_requested.emit)
        row2.addWidget(self._clear_btn)
        row2.addStretch()
        layout.addLayout(row2)

        # 底部状态区。以最长预期文本计算固定宽度，避免实时数据刷新时抖动或截断。
        def fixed_label(text: str, width_sample: str) -> QLabel:
            label = QLabel(text)
            width = label.fontMetrics().horizontalAdvance(width_sample) + 16
            label.setFixedWidth(width)
            label.setToolTip(text)
            return label

        self._bottom_bar = QFrame()
        self._bottom_bar.setObjectName("pidWaveformBottomBar")
        bottom_layout = QVBoxLayout(self._bottom_bar)
        bottom_layout.setContentsMargins(6, 4, 6, 4)
        bottom_layout.setSpacing(4)

        self._bottom_status_layout = QHBoxLayout()
        self._bottom_status_layout.setSpacing(8)
        self._status_source_label = fixed_label(
            "D轴电流 Id", "Q轴电流 Iq")
        self._status_state_label = fixed_label(
            "未接收", "接收中 · 显示已暂停")
        # 兼容既有诊断代码；状态标签已从顶部移至底部状态区。
        self._status_label = self._status_state_label
        self._status_request_label = fixed_label(
            "请求 --", "请求 5000 Hz")
        self._status_device_label = fixed_label(
            "设备 --", "设备 5000 Hz")
        self._status_transport_label = fixed_label(
            "上位机 --", "上位机接收 5000.0 Hz")
        for label in (
                self._status_source_label, self._status_state_label,
                self._status_request_label, self._status_device_label,
                self._status_transport_label):
            self._bottom_status_layout.addWidget(label)
        self._meas_hint_label = fixed_label(
            "双击选点测量 · 再次双击清除",
            "双击选点测量 · 再次双击清除")
        self._bottom_status_layout.addWidget(self._meas_hint_label)
        self._bottom_status_layout.addStretch()
        bottom_layout.addLayout(self._bottom_status_layout)

        self._bottom_stats_layout = QHBoxLayout()
        self._bottom_stats_layout.setSpacing(8)
        self._stats_title_label = fixed_label("统计", "统计")
        stat_width_sample = "反馈 min -1.23e+05 · max -1.23e+05 · mean -1.23e+05"
        self._stats_target_label = fixed_label("目标 --", stat_width_sample)
        self._stats_feedback_label = fixed_label("反馈 --", stat_width_sample)
        self._stats_error_label = fixed_label("误差 --", stat_width_sample)
        self._history_label = fixed_label(
            "历史 --", "历史 200000/200000 点 · 200.0 s")
        for label in (
                self._stats_title_label, self._stats_target_label,
                self._stats_feedback_label, self._stats_error_label,
                self._history_label):
            self._bottom_stats_layout.addWidget(label)
        self._bottom_stats_layout.addStretch()
        bottom_layout.addLayout(self._bottom_stats_layout)
        self._apply_bottom_bar_style()

        if pg is None:
            hint = QLabel("当前环境缺少 pyqtgraph，无法显示 PID 波形。")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet(f"color:{theme.hex('muted')}; padding:20px;")
            layout.addWidget(hint, 1)
            self._plot = None
            layout.addWidget(self._bottom_bar)
            return

        self._build_plot()
        layout.addWidget(self._plot, 1)
        layout.addWidget(self._bottom_bar)
        self._apply_style()

        # 节流渲染: 增量追加后只在一段时间后真正重绘一次
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(40)
        self._render_timer.timeout.connect(self._render)
        self._apply_source_axis()

    def _apply_bottom_bar_style(self):
        """刷新底部状态区及各字段颜色。"""
        self._bottom_bar.setStyleSheet(
            f"QFrame#pidWaveformBottomBar {{"
            f"background:{theme.hex('panel_bg')};"
            f"border-top:1px solid {theme.hex('border')};"
            "}")
        muted_style = f"color:{theme.hex('muted')};"
        for label in (
                self._status_source_label, self._status_request_label,
                self._status_device_label, self._status_transport_label,
                self._meas_hint_label, self._stats_title_label,
                self._stats_target_label, self._stats_feedback_label,
                self._stats_error_label, self._history_label):
            label.setStyleSheet(muted_style)
        status_style = f"color:{theme.hex(self._status_color_key)};"
        for label in (
                self._status_state_label, self._status_request_label,
                self._status_device_label, self._status_transport_label):
            label.setStyleSheet(status_style)

    @staticmethod
    def _set_fixed_label_text(label: QLabel, text: str):
        label.setText(text)
        label.setToolTip(text)

    def _build_plot(self):
        """构建绘图: 左轴目标/反馈, 右轴误差, 差值填充, 十字光标与测量游标。"""
        self._plot = pg.PlotWidget()
        self._plot.setBackground(theme.hex("plot_bg"))
        self._plot.showGrid(x=True, y=True, alpha=0.25)
        self._plot.setMenuEnabled(False)
        self._plot.setMouseEnabled(x=True, y=True)
        self._plot.getPlotItem().hideButtons()
        self._plot.setLabel("bottom", "采样点")
        pi = self._plot.getPlotItem()
        for ax in ("left", "bottom", "right"):
            a = pi.getAxis(ax)
            a.setPen(theme.hex("border"))
            a.setTextPen(theme.hex("muted"))
        self._legend = self._plot.addLegend(offset=(8, 8))

        # 曲线: 用 PlotDataItem 以支持 symbol 点符渲染 (PlotCurveItem 不支持)
        self._target_curve = pg.PlotDataItem(
            [], [], pen=pg.mkPen("#2DD4BF", width=2))
        self._target_curve.setZValue(2)
        self._feedback_curve = pg.PlotDataItem(
            [], [], pen=pg.mkPen("#F59E0B", width=2))
        self._feedback_curve.setZValue(1)
        self._error_curve = pg.PlotDataItem(
            [], [], pen=pg.mkPen(self._ERROR_COLOR, width=1))
        self._error_curve.setZValue(0)
        self._plot.addItem(self._target_curve)
        self._plot.addItem(self._feedback_curve)
        self._legend.addItem(self._target_curve, "目标")
        self._legend.addItem(self._feedback_curve, "反馈")

        # 目标—反馈差值半透明填充 (偏差一眼可见)
        self._fill = pg.FillBetweenItem(
            self._target_curve, self._feedback_curve,
            brush=pg.mkBrush(34, 211, 238, 26))
        self._fill.setZValue(0.5)
        self._plot.addItem(self._fill)

        # 误差独立右轴: 第二个 ViewBox, X 与主视图联动, Y 独立
        pi.showAxis("right")
        self._vb2 = pg.ViewBox()
        pi.scene().addItem(self._vb2)
        pi.getAxis("right").linkToView(self._vb2)
        pi.getAxis("right").setLabel("误差")
        self._vb2.setXLink(pi.vb)
        self._vb2.addItem(self._error_curve)
        self._legend.addItem(self._error_curve, "误差")

        # 右键菜单替换为中文
        self._hook_zh_menu(pi.vb)
        self._hook_zh_menu(self._vb2)

        def _update_vb2():
            self._vb2.setGeometry(pi.vb.sceneBoundingRect())
            self._vb2.linkedViewChanged(pi.vb, self._vb2.XAxis)
        _update_vb2()
        pi.vb.sigResized.connect(_update_vb2)
        pi.vb.sigRangeChanged.connect(
            lambda *_: None if self._suppress_view_signal else self._schedule_render())

        # 十字光标 (跟随鼠标)
        self._cursor_v = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(theme.c("plot_cursor"), width=1,
                         style=Qt.PenStyle.DashLine))
        self._cursor_h = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(theme.c("plot_cursor"), width=1,
                         style=Qt.PenStyle.DashLine))
        self._plot.addItem(self._cursor_v, ignoreBounds=True)
        self._plot.addItem(self._cursor_h, ignoreBounds=True)
        self._cursor_v.hide()
        self._cursor_h.hide()
        self._graphics_scene = self._plot.scene()
        self._mouse_proxy = pg.SignalProxy(
            self._graphics_scene.sigMouseMoved, rateLimit=30,
            slot=lambda event: self._on_mouse_moved(event[0]))
        self._plot.scene().sigMouseClicked.connect(self._on_plot_click)

        # 就近数据点坐标提示 (跟随鼠标, 显示在最近数据点旁)
        self._point_tip = pg.TextItem(anchor=(0, 0))
        self._point_tip.setColor(theme.hex("muted"))
        self._plot.addItem(self._point_tip)
        self._point_tip.setVisible(False)

        # 双击测量: A/B 两点标记 + 水平/竖直虚线引导 + 差值标签
        self._meas_a_marker = pg.PlotDataItem(
            [], [], symbol="+", symbolSize=14, pen=None,
            symbolBrush=pg.mkBrush("#4B3FE3"))
        self._meas_b_marker = pg.PlotDataItem(
            [], [], symbol="+", symbolSize=14, pen=None,
            symbolBrush=pg.mkBrush("#22A5F7"))
        self._plot.addItem(self._meas_a_marker)
        self._plot.addItem(self._meas_b_marker)
        self._meas_a_marker.hide()
        self._meas_b_marker.hide()

        self._meas_vline = pg.PlotDataItem(
            [], [], pen=pg.mkPen("#4B3FE3", width=1,
                                  style=Qt.PenStyle.DashLine))
        self._meas_hline = pg.PlotDataItem(
            [], [], pen=pg.mkPen("#22A5F7", width=1,
                                  style=Qt.PenStyle.DashLine))
        self._meas_vline.setZValue(1.5)
        self._meas_hline.setZValue(1.5)
        self._plot.addItem(self._meas_vline)
        self._plot.addItem(self._meas_hline)
        self._meas_vline.hide()
        self._meas_hline.hide()

        self._meas_a_label = pg.TextItem(anchor=(1, 1))
        self._meas_a_label.setColor("#4B3FE3")
        self._meas_b_label = pg.TextItem(anchor=(0, 1))
        self._meas_b_label.setColor("#22A5F7")
        self._meas_diff_label = pg.TextItem(anchor=(0, 0))
        self._meas_diff_label.setColor(self._ERROR_COLOR)
        self._plot.addItem(self._meas_a_label)
        self._plot.addItem(self._meas_b_label)
        self._plot.addItem(self._meas_diff_label)
        self._meas_a_label.setVisible(False)
        self._meas_b_label.setVisible(False)
        self._meas_diff_label.setVisible(False)

        # 测量状态
        self._meas_a_pt = None
        self._meas_b_pt = None

    # ========================================================================
    #  右键菜单: 线宽 / 点大小 配置
    # ========================================================================
    def _hook_zh_menu(self, vb):
        """让指定 ViewBox 的右键菜单在弹出前替换为中文，并追加线宽/点大小配置。"""
        if vb is None or getattr(vb, "_zh_menu_hooked", False):
            return
        vb._zh_menu_hooked = True
        orig = vb.raiseContextMenu

        def _patched(ev):
            menu = vb.getMenu(ev)
            if menu is not None:
                _translate_menu_recursive(menu)
                self._append_style_menu(menu)
                vb.scene().addParentContextMenus(vb, menu, ev)
                menu.popup(ev.screenPos().toPoint())

        vb.raiseContextMenu = _patched

    def _append_style_menu(self, menu):
        """在右键菜单追加"线宽/点大小"子菜单；先移除旧的样式动作避免重复。"""
        if self._plot is None:
            return
        for act in list(menu.actions()):
            if act.property("_style_menu"):
                menu.removeAction(act)
        menu.addSeparator()

        line_menu = QMenu("线宽", menu)
        for w in (1, 2, 3, 4):
            a = line_menu.addAction(f"{w} px")
            a.setCheckable(True)
            a.setChecked(int(self._line_width) == w)
            a.triggered.connect(
                lambda _checked, ww=w: self._set_line_width(ww))
        dot_menu = QMenu("点大小", menu)
        for s in (2, 4, 6, 8):
            a = dot_menu.addAction(f"{s} px")
            a.setCheckable(True)
            a.setChecked(int(self._dot_size) == s)
            a.triggered.connect(
                lambda _checked, ss=s: self._set_dot_size(ss))
        act_line = menu.addMenu(line_menu)
        act_line.setProperty("_style_menu", True)
        act_dot = menu.addMenu(dot_menu)
        act_dot.setProperty("_style_menu", True)

    def _set_line_width(self, width: int):
        self._line_width = width
        self._schedule_render()

    def _set_dot_size(self, size: int):
        self._dot_size = size
        self._sym_opt = self._build_sym_opt()
        self._schedule_render()

    def _build_sym_opt(self) -> dict:
        """按显示方式生成符号绘制参数, 点大小取 _dot_size。"""
        if self._wave_style == "scatter":
            return {"symbol": "o", "symbolSize": self._dot_size}
        if self._wave_style == "line+dot":
            return {"symbol": "o", "symbolSize": self._dot_size}
        return {}

    # ========================================================================
    #  数据注入 (面板增量推送)
    # ========================================================================
    def set_points(self, points):
        """面板全量重置 (清空/换源/换率时调用)。"""
        self._points = PointHistory(points or [], maxlen=self._buffer_max)
        self._schedule_render()

    def set_history_points(self, points):
        """同步面板历史到窗口，不改变当前 X 轴视口。"""
        self._points = PointHistory(points or [], maxlen=self._buffer_max)
        self._schedule_render()

    def append_points(self, points):
        """面板增量推送新点，保持有界历史，避免每包复制头部列表。"""
        pts = list(points or [])
        if not pts:
            return
        self._points.extend(pts)
        self._schedule_render()

    def clear(self):
        self._points.clear()
        self._meas_a_pt = None
        self._meas_b_pt = None
        self._render_measurement()
        self._point_tip.setVisible(False)
        self._schedule_render()

    def _schedule_render(self):
        if self._plot is None or not self.isVisible():
            return
        if self._render_timer is not None and not self._render_timer.isActive():
            self._render_timer.start()

    # ========================================================================
    #  渲染
    # ========================================================================
    def _visible_points(self):
        if not self._points:
            return []
        points = self._points
        first, last = points[0][0], points[-1][0]
        view = self._plot.viewRange()[0] if self._plot is not None else (first, last)
        span = max(1.0, float(view[1] - view[0]))
        if self._follow:
            left = max(first, last - span)
            right = last
        else:
            left = float(view[0])
            right = float(view[1])
            if right <= first:
                left, right = first, first + span
            elif left >= last:
                right = last
                left = max(first, last - span)
            else:
                left = max(first, left)
                right = min(last, right)
        lo = max(0, bisect_left(points, (left,)) - 1)
        hi = min(len(points), bisect_left(points, (right,)) + 1)
        return points[lo:hi]

    @staticmethod
    def _envelope(points, pixel_width, max_points=None):
        """按像素桶保留三条曲线的首/末/极值点，确定性保留尖峰。"""
        pixel_width = max(1, int(pixel_width))
        if max_points is not None:
            max_points = max(12, int(max_points))
            pixel_width = min(pixel_width, max(1, max_points // 12))
        if len(points) <= pixel_width:
            return points
        x0, x1 = points[0][0], points[-1][0]
        span = max(1.0, float(x1 - x0))
        buckets = {}
        for point in points:
            bucket = min(pixel_width - 1, max(0, int(
                (point[0] - x0) / span * pixel_width)))
            entry = buckets.setdefault(bucket, {
                "first": point, "last": point,
                "min": [point, point, point],
                "max": [point, point, point],
            })
            entry["last"] = point
            for col in range(3):
                if point[col + 1] < entry["min"][col][col + 1]:
                    entry["min"][col] = point
                if point[col + 1] > entry["max"][col][col + 1]:
                    entry["max"][col] = point
        selected = []
        for bucket in sorted(buckets):
            entry = buckets[bucket]
            candidates = [entry["first"]]
            candidates.extend(entry["min"])
            candidates.extend(entry["max"])
            candidates.append(entry["last"])
            for point in sorted(candidates, key=lambda item: item[0]):
                if not selected or point[0] != selected[-1][0]:
                    selected.append(point)
        return selected

    def _render(self):
        self._update_history_label()
        if self._plot is None or not self.isVisible():
            return
        if not self._points:
            for curve in (self._target_curve, self._feedback_curve, self._error_curve):
                curve.setData([], [])
            self._update_stats([], [], [], [])
            return
        visible = self._visible_points()
        if not visible:
            return
        width = max(64, int(self._plot.width()))
        if len(visible) > min(width * 4, self._MAX_DISPLAY_POINTS):
            visible = self._envelope(
                visible, width, max_points=self._MAX_DISPLAY_POINTS)
        xs = [p[0] for p in visible]
        tgt = [p[1] for p in visible]
        fb = [p[2] for p in visible]
        err = [p[3] for p in visible]

        scatter = self._scatter
        kw = self._curve_kwargs()
        self._target_curve.setData(
            xs, tgt,
            pen=None if scatter else pg.mkPen("#2DD4BF", width=self._line_width),
            symbolBrush=pg.mkBrush("#2DD4BF"), **kw)
        self._feedback_curve.setData(
            xs, fb,
            pen=None if scatter else pg.mkPen("#F59E0B", width=self._line_width),
            symbolBrush=pg.mkBrush("#F59E0B"), **kw)
        self._error_curve.setData(
            xs, err,
            pen=None if scatter else pg.mkPen(
                self._ERROR_COLOR, width=max(1, self._line_width - 1)),
            symbolBrush=pg.mkBrush(self._ERROR_COLOR), **kw)

        if self._follow and xs:
            view = self._plot.viewRange()[0]
            span = max(1.0, float(view[1] - view[0]))
            self._suppress_view_signal = True
            try:
                self._plot.setXRange(
                    max(0, xs[-1] - span), max(span / 10.0, xs[-1]),
                    padding=0)
            finally:
                self._suppress_view_signal = False

        self._update_error_highlight(err)
        self._update_stats(xs, tgt, fb, err)

    def _update_error_highlight(self, err_vals):
        """误差超阈值时误差曲线高亮(红色), 否则正常琥珀色。"""
        hot = bool(self._err_threshold > 0.0 and
                   any(abs(v) > self._err_threshold for v in err_vals))
        color = self._ERROR_HOT_COLOR if hot else self._ERROR_COLOR
        if self._scatter:
            # 散点模式无连线, 改点符颜色高亮
            self._error_curve.setData(
                symbolBrush=pg.mkBrush(color))
        else:
            self._error_curve.setPen(
                pg.mkPen(color, width=max(1, self._line_width - 1)))

    def _update_history_label(self):
        """显示历史实际存储量、采样点范围和按设备频率估算的时长。"""
        count = len(self._points)
        if count:
            start = int(self._points[0][0])
            end = int(self._points[-1][0])
        else:
            start = end = 0
        rate = max(0.0, float(getattr(self, "_history_rate_hz", 0.0)))
        span = count / rate if rate > 0.0 else 0.0
        self._set_fixed_label_text(
            self._history_label,
            f"历史 {count}/{self._buffer_max} 点 · {span:.1f} s · "
            f"{start}~{end}")

    def _update_stats(self, xs, tgt, fb, err):
        """基于当前(可视)数据计算 目标/反馈/误差 的 min·max·mean。"""
        if not xs or not self._stats_check.isChecked():
            self._set_fixed_label_text(self._stats_target_label, "目标 --")
            self._set_fixed_label_text(self._stats_feedback_label, "反馈 --")
            self._set_fixed_label_text(self._stats_error_label, "误差 --")
            return

        def stat(vals):
            vals = [v for v in vals if v == v]   # 过滤 nan
            if not vals:
                return None
            return min(vals), max(vals), sum(vals) / len(vals)

        def fmt(s):
            return "--" if s is None else (
                f"min {s[0]:.3g} · max {s[1]:.3g} · mean {s[2]:.3g}")

        self._set_fixed_label_text(
            self._stats_target_label, f"目标 {fmt(stat(tgt))}")
        self._set_fixed_label_text(
            self._stats_feedback_label, f"反馈 {fmt(stat(fb))}")
        self._set_fixed_label_text(
            self._stats_error_label, f"误差 {fmt(stat(err))}")

    # ========================================================================
    #  交互: 就近点坐标提示 + 双击选点测量
    # ========================================================================
    def _nearest_sample(self, at_x):
        """按横坐标(采样点编号)找最近的数据点 (n, target, feedback, error)。"""
        if not self._points:
            return None
        idx = bisect_left(self._points, (at_x,))
        if idx == 0:
            return self._points[0]
        if idx == len(self._points):
            return self._points[-1]
        before, after = self._points[idx - 1], self._points[idx]
        return before if at_x - before[0] <= after[0] - at_x else after

    def _on_mouse_moved(self, pos):
        if self._plot is None:
            return
        vb = self._plot.plotItem.vb
        mouse = vb.mapSceneToView(pos)
        self._cursor_v.setPos(mouse.x())
        self._cursor_h.setPos(mouse.y())
        self._update_point_tip(mouse.x())

    def _update_point_tip(self, at_x):
        """在最近数据点旁显示坐标数据 (点号/目标/反馈/误差)。"""
        pt = self._nearest_sample(at_x)
        if pt is None:
            self._point_tip.setVisible(False)
            return
        n, target, fb, err = pt
        self._point_tip.setText(
            f"点 {n:.0f}\n目标 {target:.4g}\n反馈 {fb:.4g}\n误差 {err:.4g}")
        self._point_tip.setPos(n, fb)
        self._point_tip.setVisible(True)

    def _on_plot_click(self, ev):
        """双击选点测量: 第1次选A, 第2次选B, 第3次清除。"""
        if self._plot is None or not self._points:
            return
        if ev.button() != Qt.MouseButton.LeftButton or not ev.double():
            return
        pos = self._plot.plotItem.vb.mapSceneToView(ev.scenePos())
        pt = self._nearest_sample(pos.x())
        if pt is None:
            return
        if self._meas_a_pt is None:
            self._meas_a_pt = pt
            self._meas_b_pt = None
        elif self._meas_b_pt is None:
            self._meas_b_pt = pt
        else:
            self._meas_a_pt = None
            self._meas_b_pt = None
        self._render_measurement()

    def _render_measurement(self):
        """按 A/B 两点绘制标记、水平/竖直虚线引导与差值标签。"""
        if self._plot is None:
            return
        a = self._meas_a_pt
        b = self._meas_b_pt
        if a is None:
            for item in (self._meas_a_marker, self._meas_b_marker,
                         self._meas_vline, self._meas_hline):
                item.hide()
            for item in (self._meas_a_label, self._meas_b_label,
                         self._meas_diff_label):
                item.setVisible(False)
            return

        na, ta, fa, _ea = a
        self._meas_a_marker.setData([na], [fa])
        self._meas_a_marker.show()
        self._meas_a_label.setText(
            f"A 点{na:.0f} · 目标 {ta:.4g} · 反馈 {fa:.4g}")
        self._meas_a_label.setPos(na, fa)
        self._meas_a_label.setVisible(True)

        if b is None:
            self._meas_b_marker.hide()
            self._meas_vline.hide()
            self._meas_hline.hide()
            self._meas_b_label.setVisible(False)
            self._meas_diff_label.setVisible(False)
            return

        nb, tb, fb, _eb = b
        self._meas_b_marker.setData([nb], [fb])
        self._meas_b_marker.show()
        self._meas_b_label.setText(
            f"B 点{nb:.0f} · 目标 {tb:.4g} · 反馈 {fb:.4g}")
        self._meas_b_label.setPos(nb, fb)
        self._meas_b_label.setVisible(True)

        # 竖直虚线: 过 A 反馈值, 从 A 反馈值到 B 反馈值; 水平虚线: 回到 B 点号
        self._meas_vline.setData([na, na], [fa, fb])
        self._meas_hline.setData([na, nb], [fb, fb])
        self._meas_vline.show()
        self._meas_hline.show()

        dn = nb - na
        self._meas_diff_label.setText(
            f"ΔN {dn:.0f} · Δ目标 {tb - ta:.4g} · Δ反馈 {fb - fa:.4g}")
        self._meas_diff_label.setPos(
            (na + nb) / 2, max(fa, fb))
        self._meas_diff_label.setVisible(True)

    # ========================================================================
    #  视觉层: 显示方式 / 误差阈值 / Y 自适应 / 适配全量 / 统计开关
    # ========================================================================
    def _on_style_changed(self, index: int):
        self._wave_style = str(self._style_combo.itemData(index))
        self._apply_style()

    def _apply_style(self):
        """按显示方式(折线/点线/散点)记录绘制参数, 并触发一次重绘。

        符号需随 setData 传入 PlotDataItem 才生效, 故这里只记录参数。
        """
        if self._plot is None:
            return
        self._scatter = (self._wave_style == "scatter")
        self._sym_opt = self._build_sym_opt()
        self._schedule_render()

    def _curve_kwargs(self):
        """返回符号绘制参数; 连线穿透由 _render 显式传 pen 控制。"""
        return dict(self._sym_opt)

    def _on_stats_toggled(self, on: bool):
        for label in (
                self._stats_title_label, self._stats_target_label,
                self._stats_feedback_label, self._stats_error_label):
            label.setVisible(bool(on))
        self._update_history_label()

    def _on_err_threshold_changed(self, value: float):
        self._err_threshold = float(value or 0.0)
        self._schedule_render()

    def _on_y_auto_toggled(self, on: bool):
        self._y_auto = bool(on)
        if self._plot is not None:
            self._plot.enableAutoRange(axis="y", enable=self._y_auto)
            self._vb2.enableAutoRange(axis="y", enable=self._y_auto)

    def _on_fit_all(self):
        if self._plot is None:
            return
        self._plot.enableAutoRange(axis="y", enable=True)
        self._vb2.enableAutoRange(axis="y", enable=True)
        self._plot.enableAutoRange(axis="x", enable=True)
        self._plot.enableAutoRange(enable=True)

    def _on_latest_clicked(self):
        """回到历史尾部，同时保留用户当前 X 轴缩放跨度。"""
        self._follow = True
        was = self._follow_check.blockSignals(True)
        self._follow_check.setChecked(True)
        self._follow_check.blockSignals(was)
        self._schedule_render()

    # ========================================================================
    #  数据源 / 采样 / 暂停 / 跟随
    # ========================================================================
    def set_source(self, source: str):
        """由面板下发当前数据源 (不回抛 source_changed)。"""
        key = get_waveform_source_def(str(source))[0]
        index = self._source_combo.findData(key)
        if index < 0:
            return
        self._source = key
        was = self._source_combo.blockSignals(True)
        self._source_combo.setCurrentIndex(index)
        self._source_combo.blockSignals(was)
        self._apply_source_axis()
        if hasattr(self, "_status_state_label"):
            self.set_capture_status(False, self.rate_hz())

    def source(self) -> str:
        return str(self._source)

    def _apply_source_axis(self):
        """左轴标题跟随数据源单位。"""
        if self._plot is None:
            return
        _key, label, unit = get_waveform_source_def(self._source)
        self._plot.setLabel("left", label, units=unit or None)

    def set_paused(self, paused: bool):
        was = self._pause_btn.blockSignals(True)
        self._pause_btn.setChecked(bool(paused))
        self._pause_btn.blockSignals(was)
        self._paused = bool(paused)
        self._pause_btn.setText("继续" if self._paused else "暂停")

    def set_rate(self, rate_hz: int, ceiling_hz: int | None = None):
        """由面板下发当前采样上传频率与固件上限 (不回抛 rate_changed)。"""
        if ceiling_hz:
            self._rate_ceiling = max(50, int(ceiling_hz))
        was = self._rate_spin.blockSignals(True)
        self._rate_spin.setMaximum(self._rate_ceiling)
        self._rate_spin.setValue(max(50, min(int(rate_hz), self._rate_ceiling)))
        self._rate_spin.blockSignals(was)

    def rate_hz(self) -> int:
        return int(self._rate_spin.value())

    def set_capture_status(self, active: bool, requested_hz: int,
                           device_hz: int = 0, transport_hz: float = 0.0,
                           limited: bool = False, message: str = "",
                           lost_packets: int = 0, overflow_count: int = 0):
        """显示 TRACE 状态、请求/设备/接收频率及丢包统计。"""
        _key, source_label, _unit = get_waveform_source_def(self._source)
        if device_hz > 0:
            self._history_rate_hz = float(device_hz)
        if message:
            state_text, color_key = message, "danger"
        elif not self._receiving:
            state_text, color_key = "未接收", "muted"
        elif self._paused:
            state_text, color_key = "接收中 · 显示已暂停", "warn"
        elif not active:
            state_text, color_key = "等待设备数据", "warn"
        else:
            state_text = "接收中 · 已限幅" if limited else "接收中"
            color_key = "ok"

        request_text = (
            f"请求 {int(requested_hz)} Hz" if requested_hz > 0 else "请求 --")
        device_text = (
            f"设备 {int(device_hz)} Hz" if device_hz > 0 else "设备 --")
        transport_text = (
            f"上位机接收 {transport_hz:.1f} Hz"
            if transport_hz > 0 else "上位机接收率计算中")
        if lost_packets > 0:
            transport_text += f" · 丢包 {int(lost_packets)}"
        if overflow_count > 0:
            transport_text += f" · 溢出 {int(overflow_count)}"
        self._set_fixed_label_text(self._status_source_label, source_label)
        self._set_fixed_label_text(self._status_state_label, state_text)
        self._set_fixed_label_text(self._status_request_label, request_text)
        self._set_fixed_label_text(self._status_device_label, device_text)
        self._set_fixed_label_text(
            self._status_transport_label, transport_text)
        self._status_color_key = color_key
        status_style = f"color:{theme.hex(color_key)};"
        for label in (
                self._status_state_label, self._status_request_label,
                self._status_device_label, self._status_transport_label):
            label.setStyleSheet(status_style)
        was = self._receive_btn.blockSignals(True)
        self._receive_btn.setChecked(bool(self._receiving))
        self._receive_btn.setText("停止接收" if self._receiving else "接收波形")
        self._receive_btn.blockSignals(was)

    def set_receiving(self, receiving: bool):
        was = self._receive_btn.blockSignals(True)
        self._receiving = bool(receiving)
        self._receive_btn.setChecked(self._receiving)
        self._receive_btn.setText("停止接收" if self._receiving else "接收波形")
        self._receive_btn.blockSignals(was)

    def receiving(self) -> bool:
        return bool(self._receiving)

    def buffer_max(self) -> int:
        return int(self._buffer_max)

    def _on_receive_toggled(self, receiving: bool):
        self._receiving = bool(receiving)
        self._receive_btn.setText("停止接收" if self._receiving else "接收波形")
        self.receive_toggled.emit(self._receiving)

    def _on_rate_changed(self, rate_hz: int):
        self.rate_changed.emit(int(rate_hz))

    def _on_buffer_changed(self, points: int):
        """存储总量变化: 立即裁剪旧数据。"""
        self._buffer_max = max(self._MIN_BUFFER_POINTS,
                               min(int(points), self._MAX_BUFFER_POINTS))
        self._points = PointHistory(self._points, maxlen=self._buffer_max)
        self.buffer_changed.emit(self._buffer_max)
        self._schedule_render()

    def _on_source_index_changed(self, index: int):
        key = self._source_combo.itemData(index)
        if not key or key == self._source:
            return
        self._source = str(key)
        self._apply_source_axis()
        self.source_changed.emit(self._source)

    def _on_pause_toggled(self, paused: bool):
        self._paused = bool(paused)
        self._pause_btn.setText("继续" if self._paused else "暂停")
        self.pause_toggled.emit(self._paused)

    def _on_follow_toggled(self, follow: bool):
        self._follow = bool(follow)
        self._schedule_render()

    def apply_theme(self):
        self._apply_bottom_bar_style()
        if self._plot is None:
            return
        self._plot.setBackground(theme.hex("plot_bg"))
        for name in ("left", "bottom", "right"):
            axis = self._plot.getAxis(name)
            axis.setPen(theme.hex("border"))
            axis.setTextPen(theme.hex("muted"))
        self._apply_style()

    def get_opts(self) -> dict:
        geo = self.geometry()
        return {
            "geo": [geo.x(), geo.y(), geo.width(), geo.height()],
            "follow": bool(self._follow_check.isChecked()),
            "rate_hz": int(self._rate_spin.value()),
            "source": str(self._source),
            "buffer_max": int(self._buffer_max),
            "line_width": int(self._line_width),
            "dot_size": int(self._dot_size),
            "wave_style": str(self._wave_style),
            "stats": bool(self._stats_check.isChecked()),
            "y_auto": bool(self._y_auto_check.isChecked()),
            "err_threshold": float(self._err_threshold),
        }

    def set_opts(self, opts: dict):
        if not isinstance(opts, dict):
            return
        geo = opts.get("geo")
        if isinstance(geo, list) and len(geo) >= 4:
            try:
                self.setGeometry(int(geo[0]), int(geo[1]),
                                 max(900, int(geo[2])), max(520, int(geo[3])))
            except (TypeError, ValueError):
                pass
        if "follow" in opts:
            self._follow_check.setChecked(bool(opts["follow"]))
        if "rate_hz" in opts:
            try:
                self.set_rate(int(opts["rate_hz"]))
            except (TypeError, ValueError):
                pass
        if opts.get("source"):
            self.set_source(str(opts["source"]))
        if opts.get("buffer_max"):
            try:
                self._buf_spin.setValue(max(1000, int(opts["buffer_max"])))
            except (TypeError, ValueError):
                pass
        if "line_width" in opts:
            try:
                self._line_width = min(4, max(1, int(opts["line_width"])))
            except (TypeError, ValueError):
                pass
        if "dot_size" in opts:
            try:
                self._dot_size = min(8, max(2, int(opts["dot_size"])))
            except (TypeError, ValueError):
                pass
        if opts.get("wave_style"):
            idx = self._style_combo.findData(str(opts["wave_style"]))
            if idx >= 0:
                self._style_combo.setCurrentIndex(idx)
        if "stats" in opts:
            self._stats_check.setChecked(bool(opts["stats"]))
        if "y_auto" in opts:
            self._y_auto_check.setChecked(bool(opts["y_auto"]))
        if "err_threshold" in opts:
            try:
                self._err_thr_spin.setValue(float(opts["err_threshold"]))
            except (TypeError, ValueError):
                pass
        self._sym_opt = self._build_sym_opt()

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_render()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._render_timer is not None:
            self._render_timer.stop()

    def closeEvent(self, event):
        if self._render_timer is not None:
            self._render_timer.stop()
        self.window_closed.emit()
        event.accept()
