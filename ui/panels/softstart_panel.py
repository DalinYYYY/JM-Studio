"""
缓启配置面板 (顶级 Tab)。

对应固件 ref_smooth_cfg_t 缓启动渐变配置, 经 0xA3/0xA4 读写:
  1. 工具栏 — 读取配置(0xA4 FF 整块) / 下发修改(逐字段 0xA3, 仅 dirty) / 固化 EEPROM(0xEA) / 恢复默认
  2. 全局设置 — 总开关 / 渐变形状 / 兜底时长
  3. 目标参数表 — 按目标对齐 rate / thresh
  4. 过渡预览 — 目标类型 + Δ目标 → 预计时长与曲线
  5. 折叠说明与固化提示

信号:
  smooth_cfg_read_requested()                — 主窗口连 client.smooth_cfg_get(0xFF)
  smooth_cfg_set_requested(int, object)       — 主窗口连 client.smooth_cfg_set(pid, value)
  smooth_cfg_save_requested()                — 主窗口连 _on_config_save(0) → 0xEA

时长换算: 控制环 10kHz(dt=100µs), 次数×100µs=时间。
"""
from PyQt6.QtCore import pyqtSignal, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
    QDoubleSpinBox, QSpinBox, QComboBox, QScrollArea, QWidget, QGridLayout,
    QMessageBox, QSizePolicy, QButtonGroup, QBoxLayout, QStyle,
)

from jmproto import smoothstart as ss
from ui.theme import theme

# 控制环周期(s): 电流环 10kHz。固件 transition 时长以调用次数计。
_DT_SECONDS = 1.0e-4

# 估算器目标类型 → (显示名, rate 字段名, 单位)
_ESTIM_TARGETS = [
    ("位置", "pos_rate", "rad"),
    ("速度", "vel_rate", "rad/s"),
    ("力矩", "torque_rate", "N·m"),
    ("电流", "current_rate", "A"),
]

# 字段名 → (显示名, 单位, 小数位)
_RATE_FIELDS = [
    ("pos_rate", "位置", "rad/s", 1),
    ("vel_rate", "速度", "rad/s²", 1),
    ("torque_rate", "力矩", "N·m/s", 1),
    ("current_rate", "电流", "A/s", 1),
]

_THRESH_FIELDS = [
    ("pos_thresh", "位置", "rad", 3),
    ("vel_thresh", "速度", "rad/s", 2),
    ("torque_thresh", "力矩", "N·m", 3),
    ("current_thresh", "电流", "A", 2),
    ("voltage_thresh", "电压", "V", 2),
    ("duty_thresh", "占空比", "", 3),
]

class _SpinField:
    """字段条目: 控件 + 单位标签 + dirty 基准跟踪"""

    def __init__(self, name, spin, unit_label=None):
        self.name = name
        self.spin = spin
        self.unit_label = unit_label
        self.baseline = None  # 读取回填时记录基准值

    def set_baseline(self, value):
        # 设备使用 float32，基准按控件精度对齐，避免 0.1 等回读值误报修改。
        self.baseline = round(value, self.spin.decimals()) if isinstance(
            self.spin, QDoubleSpinBox) else value
        self.refresh_dirty()

    def current(self):
        return self.spin.value()

    def is_dirty(self):
        if self.baseline is None:
            return False
        return abs(self.current() - self.baseline) > 1e-9

    def refresh_dirty(self):
        dirty = self.is_dirty()
        self.spin.setStyleSheet(
            f"QAbstractSpinBox {{ border: 1px solid {theme.hex('value_hot')}; }}"
            if dirty else "")
        if self.unit_label is not None:
            color = theme.hex("value_hot") if dirty else theme.hex("muted")
            self.unit_label.setStyleSheet(f"color:{color};")


class _CurvePreview(QWidget):
    """渐变形状实时预览: 归一化混合曲线 out(t) = (1-r)·old + r·target。

    当前 shape 实线高亮, 另一形状虚线参考; SCURVE 标注峰值斜率点。
    X 轴总时长随估算参数(rate/Δ)或兜底时长实时变化。
    """

    _MARGIN_L = 44   # Y 轴标注宽度
    _MARGIN_R = 12
    _MARGIN_T = 44   # 图例与轴标题
    _MARGIN_B = 42   # 刻度与时间轴标题

    def __init__(self, parent=None):
        super().__init__(parent)
        self._shape = 0
        self._total_seconds = 0.5   # X 轴总时长(0=未知)
        self.setMinimumSize(280, 260)
        self.setAccessibleName("目标过渡曲线")
        # 纵向跟随组高度撑满(组吃掉剩余空间), 横向扩展
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_state(self, shape: int, total_seconds: float):
        if shape != self._shape or total_seconds != self._total_seconds:
            self._shape = shape
            self._total_seconds = max(0.0, total_seconds)
            self.update()

    @staticmethod
    def _ratio_curve(r: float, shape: int) -> float:
        """归一化输出: LINEAR y=x; SCURVE y=x²(3-2x) (smoothstep)"""
        if shape == 1:
            return r * r * (3.0 - 2.0 * r)
        return r

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        bg = QColor(theme.hex("panel_bg"))
        p.fillRect(self.rect(), bg)

        left = self._MARGIN_L
        right = w - self._MARGIN_R
        top = self._MARGIN_T
        bottom = h - self._MARGIN_B
        if right - left < 40 or bottom - top < 40:
            return

        # 网格 + 坐标轴
        grid_pen = QPen(QColor(theme.hex("border")), 1)
        p.setPen(grid_pen)
        for i in range(3):
            y = top + (bottom - top) * i / 2.0
            p.drawLine(left, int(y), right, int(y))
        p.drawLine(left, top, left, bottom)
        p.drawLine(left, bottom, right, bottom)

        axis_color = QColor(theme.hex("muted"))
        p.setPen(QPen(axis_color, 1))
        font = p.font()
        font.setPointSizeF(8.0)
        p.setFont(font)
        p.drawText(left, top - 22, 100, 18,
                   Qt.AlignmentFlag.AlignLeft, "目标进度")
        p.drawText(0, top - 8, left - 6, 16,
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "100%")
        p.drawText(0, (top + bottom) // 2 - 8, left - 6, 16,
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "50%")
        p.drawText(0, bottom - 8, left - 6, 16,
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "0%")

        # X 轴时长刻度
        if self._total_seconds >= 0:
            mid_s = self._total_seconds / 2.0
            tick_width = max(60, p.fontMetrics().horizontalAdvance(
                f"{self._total_seconds:.2f}") + 4)
            p.drawText(left, bottom + 5, tick_width, 16,
                       Qt.AlignmentFlag.AlignLeft, "0.00")
            if right - left > tick_width * 3:
                p.drawText((left + right - tick_width) // 2, bottom + 5, tick_width, 16,
                           Qt.AlignmentFlag.AlignHCenter, f"{mid_s:.2f}")
            p.drawText(right - tick_width, bottom + 5, tick_width, 16,
                       Qt.AlignmentFlag.AlignRight, f"{self._total_seconds:.2f}")
        p.drawText(left, bottom + 23, right - left, 18,
                   Qt.AlignmentFlag.AlignRight, "时间 (s)")

        # 参考曲线(非当前 shape): 虚线灰
        other = 1 - self._shape
        self._draw_curve(p, other, QPen(axis_color, 1, Qt.PenStyle.DashLine),
                         left, right, top, bottom)

        # 当前曲线: 实线高亮
        accent = QColor(theme.hex("accent"))
        self._draw_curve(p, self._shape, QPen(accent, 2),
                         left, right, top, bottom)

        # SCURVE 峰值斜率点标注(r=0.5)
        if self._shape == 1:
            r = 0.5
            y = self._ratio_curve(r, 1)
            px = left + (right - left) * r
            py = bottom - (bottom - top) * y
            p.setPen(QPen(accent, 1))
            p.drawEllipse(int(px) - 3, int(py) - 3, 6, 6)
            p.drawText(int(px) + 6, int(py) - 6, "峰值斜率")

        # 图例
        name_cur = "S 曲线" if self._shape == 1 else "线性"
        name_ref = "线性" if self._shape == 1 else "S 曲线"
        p.setPen(QPen(accent, 2))
        p.drawLine(left + 8, 9, left + 26, 9)
        p.setPen(QPen(axis_color))
        p.drawText(left + 30, 3, 100, 12, Qt.AlignmentFlag.AlignVCenter, name_cur)
        p.setPen(QPen(axis_color, 1, Qt.PenStyle.DashLine))
        p.drawLine(left + 108, 9, left + 126, 9)
        p.setPen(QPen(axis_color))
        p.drawText(left + 130, 3, 100, 12, Qt.AlignmentFlag.AlignVCenter, name_ref)

    def _draw_curve(self, p: QPainter, shape: int, pen: QPen,
                    left, right, top, bottom):
        path = QPainterPath()
        steps = 64
        for i in range(steps + 1):
            r = i / steps
            y = self._ratio_curve(r, shape)
            px = left + (right - left) * r
            py = bottom - (bottom - top) * y
            if i == 0:
                path.moveTo(px, py)
            else:
                path.lineTo(px, py)
        p.setPen(pen)
        p.drawPath(path)


class SoftstartPanel(QWidget):
    """缓启配置面板 (顶级 Tab)"""

    # ---- 请求信号 (主窗口连接到 JmClient) ----
    smooth_cfg_read_requested = pyqtSignal()
    smooth_cfg_set_requested = pyqtSignal(int, object)  # param_id, value(int/float)
    smooth_cfg_save_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._link_active = False
        self._fields = {}          # 字段名 -> _SpinField
        self._build_ui()
        self._apply_theme()
        theme.changed.connect(self._apply_theme)
        self._update_state_label()
        self._update_estimate()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self.setObjectName("softstartPanel")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(6, 6, 6, 6)
        body_layout.setSpacing(0)
        self._scroll.setWidget(body)
        root.addWidget(self._scroll)
        body_layout.addWidget(self._build_toolbar())
        body_layout.addWidget(self._build_global_group())
        self._content_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._content_layout.setSpacing(0)
        self._parameter_panel = self._build_parameter_table()
        self._estimator_panel = self._build_estimator_group()
        self._content_layout.addWidget(self._parameter_panel, 1)
        self._content_layout.addWidget(self._estimator_panel, 1)
        body_layout.addLayout(self._content_layout, 1)
        self._help_panel = self._build_help_panel()
        self._help_panel.hide()
        self._help_btn.toggled.connect(self._help_panel.setVisible)
        body_layout.addWidget(self._help_panel)
        footer = QWidget()
        footer.setObjectName("softstartFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(16, 10, 16, 10)
        note = self._muted_label("下发后 RAM 生效 · 固化后断电保持")
        note.setWordWrap(True)
        warning = QLabel("EEPROM 有写入寿命，请勿频繁固化")
        warning.setObjectName("softstartWarning")
        warning.setWordWrap(True)
        warning.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        footer_layout.addWidget(note, 1)
        footer_layout.addWidget(warning, 1)
        body_layout.addWidget(footer)

    @staticmethod
    def _muted_label(text=""):
        label = QLabel(text)
        label.setProperty("softstartRole", "muted")
        return label

    def _build_help_panel(self):
        """说明按需展开，不挤占参数与曲线的常驻空间。"""
        box = QWidget()
        box.setObjectName("softstartHelp")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(16, 12, 16, 12)

        text = QLabel(
            "<b>配置流程</b><br>"
            "① 读取配置 → ② 修改参数 → ③ 下发修改 → ④ 固化 EEPROM"
            "<br>"
            "<b>生效机制</b><br>"
            "0xA3 下发即写入 RAM 立即生效；0xEA 固化到 EEPROM+Flash 双备份后断电保持"
            "<br>"
            "<b>过渡时长</b><br>"
            "速率模式：|Δ目标| / rate，随目标阶跃自适应；"
            "rate 全 0 时改由兜底时长决定"
            "<br>"
            "<b>突变阈值</b><br>"
            "单拍目标变化小于阈值 → 直接透传不渐变，避免微小调节被拖慢"
            "<br>"
            "<b>渐变形状</b><br>"
            "线性：恒斜率；S 曲线：起停柔和，峰值斜率≈平均 1.5 倍"
            "<br>"
            "<b>待下发标记</b><br>"
            "与设备不一致的项显示橙色标记，顶部提示待下发数量，下发后自动回读校验"
        )
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(text)
        return box

    def _build_toolbar(self):
        bar = QWidget()
        bar.setObjectName("softstartToolbar")
        self._toolbar_layout = QGridLayout(bar)
        self._toolbar_layout.setContentsMargins(16, 12, 16, 12)
        actions = QWidget()
        lay = QHBoxLayout(actions)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        title = QLabel("缓启动")
        title.setProperty("softstartRole", "title")
        lay.addWidget(title)
        lay.addSpacing(8)
        self._read_btn = QPushButton("读取配置")
        self._read_btn.clicked.connect(self._on_read_clicked)
        self._send_btn = QPushButton("下发修改")
        self._send_btn.setObjectName("softstartSend")
        self._send_btn.clicked.connect(self._on_send_clicked)
        self._save_btn = QPushButton("固化 EEPROM")
        self._save_btn.clicked.connect(self._on_save_clicked)
        self._reset_btn = QPushButton("恢复默认")
        self._reset_btn.clicked.connect(self._on_reset_clicked)
        icons = (QStyle.StandardPixmap.SP_ArrowDown, QStyle.StandardPixmap.SP_ArrowUp,
                 QStyle.StandardPixmap.SP_DialogSaveButton, QStyle.StandardPixmap.SP_BrowserReload)
        for button, icon in zip((self._read_btn, self._send_btn, self._save_btn, self._reset_btn), icons):
            button.setIcon(self.style().standardIcon(icon))
            lay.addWidget(button)
        self._toolbar_status = QWidget()
        status_layout = QHBoxLayout(self._toolbar_status)
        status_layout.setContentsMargins(0, 0, 0, 0)
        self._state_label = QLabel()
        self._state_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._help_btn = QPushButton()
        self._help_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogHelpButton))
        self._help_btn.setFixedWidth(32)
        self._help_btn.setCheckable(True)
        self._help_btn.setToolTip("展开 / 收起使用说明")
        self._help_btn.setAccessibleName("使用说明")
        status_layout.addWidget(self._state_label, 1)
        status_layout.addWidget(self._help_btn)
        self._toolbar_layout.addWidget(actions, 0, 0)
        self._toolbar_layout.addWidget(self._toolbar_status, 0, 1)
        self._toolbar_layout.setColumnStretch(1, 1)
        return bar

    def _build_global_group(self):
        box = QWidget()
        box.setObjectName("softstartGlobal")
        self._global_grid = QGridLayout(box)
        self._global_grid.setContentsMargins(16, 16, 16, 18)
        self._global_grid.setHorizontalSpacing(24)
        self._global_grid.setVerticalSpacing(14)
        self._global_cells = []
        for col, title in enumerate(("全局开关", "渐变形状", "常规兜底时长", "跨模式兜底时长")):
            cell = QWidget()
            lay = QVBoxLayout(cell)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(6)
            lay.addWidget(self._muted_label(title))
            self._global_cells.append(cell)
            self._global_grid.addWidget(cell, 0, col)
            self._global_grid.setColumnStretch(col, 1)
        self._enable_check = QCheckBox("启用缓启动渐变")
        self._enable_check.setChecked(bool(ss.DEFAULTS[ss.PID_ENABLE]))
        self._enable_check.setMinimumHeight(32)
        self._enable_check.toggled.connect(self._on_checkbox_changed)
        self._global_cells[0].layout().addWidget(self._enable_check)
        self._global_cells[0].layout().addStretch()
        shape_layout = QHBoxLayout()
        shape_layout.setSpacing(0)
        self._shape_buttons = QButtonGroup(self)
        for shape, text in ((1, "S 曲线"), (0, "线性")):
            button = QPushButton(text)
            button.setProperty("softstartRole", "shape")
            button.setCheckable(True)
            button.setToolTip("起停柔和，峰值斜率为平均 1.5 倍" if shape else "恒定斜率")
            self._shape_buttons.addButton(button, shape)
            shape_layout.addWidget(button)
        self._shape_buttons.button(ss.DEFAULTS[ss.PID_SHAPE]).setChecked(True)
        self._shape_buttons.idClicked.connect(self._on_combo_changed)
        self._global_cells[1].layout().addLayout(shape_layout)
        self._global_cells[1].layout().addStretch()
        self._duration_spin = self._make_spin(QSpinBox, 1, 1000000, ss.DEFAULTS[ss.PID_DURATION])
        self._fallback_spin = self._make_spin(QSpinBox, 1, 1000000, ss.DEFAULTS[ss.PID_MODE_SW_FALLBACK])
        self._duration_ms = self._muted_label(self._ms_text(self._duration_spin.value()))
        self._fallback_ms = self._muted_label(self._ms_text(self._fallback_spin.value()))
        for col, name, spin, converted in (
                (2, "duration", self._duration_spin, self._duration_ms),
                (3, "mode_sw_fallback", self._fallback_spin, self._fallback_ms)):
            row = QHBoxLayout()
            row.addWidget(spin, 1)
            row.addWidget(self._muted_label("tick"))
            self._global_cells[col].layout().addLayout(row)
            self._global_cells[col].layout().addWidget(converted)
            self._fields[name] = _SpinField(name, spin)
        self._duration_spin.setToolTip("控制环调用次数，1 tick = 0.1 ms；4 个速率均为 0 时生效")
        self._fallback_spin.setToolTip("使能 / 跨模式切换的兜底时长；1 tick = 0.1 ms")
        self._duration_spin.setAccessibleName("常规兜底时长")
        self._fallback_spin.setAccessibleName("跨模式兜底时长")
        self._enable_baseline = None
        self._shape_baseline = None
        return box

    def _build_parameter_table(self):
        box = QWidget()
        box.setObjectName("softstartParameters")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(16, 18, 16, 16)
        lay.setSpacing(12)
        head = QHBoxLayout()
        title = QLabel("目标参数")
        title.setProperty("softstartRole", "heading")
        head.addWidget(title)
        head.addStretch()
        head.addWidget(self._muted_label("rate / thresh"))
        lay.addLayout(head)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for col, text, span in ((0, "目标", 1), (1, "速率限制", 2), (3, "突变阈值", 2)):
            label = self._muted_label(text)
            label.setMinimumHeight(30)
            if col == 1:
                label.setToolTip("rate=0 时该字段不参与速率模式")
            elif col == 3:
                label.setToolTip("单拍目标变化小于阈值时直接透传")
            grid.addWidget(label, 0, col, 1, span)
        for row, threshold in enumerate(_THRESH_FIELDS, 1):
            grid.addWidget(QLabel(threshold[1]), row, 0)
            grid.setRowMinimumHeight(row, 40)
            fields = [(3, threshold)]
            if row <= len(_RATE_FIELDS):
                fields.insert(0, (1, _RATE_FIELDS[row - 1]))
            else:
                grid.addWidget(self._muted_label("—"), row, 1, 1, 2)
            for col, (name, disp, unit, dec) in fields:
                spin = self._make_spin(QDoubleSpinBox, 0.0, 100000.0,
                                       ss.DEFAULTS[self._field_pid(name)], dec)
                spin.setAccessibleName(disp + ("速率限制" if col == 1 else "突变阈值"))
                unit_label = self._muted_label(unit)
                grid.addWidget(spin, row, col)
                grid.addWidget(unit_label, row, col + 1)
                self._fields[name] = _SpinField(name, spin, unit_label)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        lay.addLayout(grid)
        legend = QLabel("● 待下发参数")
        legend.setProperty("softstartRole", "dirtyLegend")
        lay.addWidget(legend)
        lay.addStretch(1)
        return box

    def _build_estimator_group(self):
        box = QWidget()
        box.setObjectName("softstartEstimator")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(16, 18, 16, 16)
        lay.setSpacing(12)
        heading = QLabel("过渡预览")
        heading.setProperty("softstartRole", "heading")
        lay.addWidget(heading)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.addWidget(self._muted_label("目标类型"), 0, 0)
        self._estim_combo = QComboBox()
        for disp, _, unit in _ESTIM_TARGETS:
            self._estim_combo.addItem(f"{disp} ({unit})")
        self._estim_combo.currentIndexChanged.connect(self._update_estimate)
        self._estim_combo.setAccessibleName("预览目标类型")
        grid.addWidget(self._estim_combo, 1, 0)
        grid.addWidget(self._muted_label("目标变化量 Δ"), 0, 1)
        self._estim_delta = QDoubleSpinBox()
        self._estim_delta.setDecimals(2)
        self._estim_delta.setRange(0.0, 100000.0)
        self._estim_delta.setValue(50.0)
        self._estim_delta.setAccessibleName("目标变化量")
        self._estim_delta.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._estim_delta.setMinimumWidth(100)
        self._estim_delta.valueChanged.connect(self._update_estimate)
        grid.addWidget(self._estim_delta, 1, 1)

        grid.addWidget(self._muted_label("预计时长"), 0, 2)
        self._estim_result = QLabel("—")
        self._estim_result.setObjectName("softstartEstimate")
        grid.addWidget(self._estim_result, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        self._estim_note = self._muted_label()
        self._estim_note.setWordWrap(True)
        self._curve_preview = _CurvePreview()
        lay.addWidget(self._curve_preview, 1)
        self._estim_metrics = self._muted_label()
        self._estim_metrics.setWordWrap(True)
        lay.addWidget(self._estim_metrics)
        lay.addWidget(self._estim_note)
        return box

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = self.width() < 960
        if not hasattr(self, "_content_layout"):
            return
        self._content_layout.setDirection(
            QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight)
        columns = 2 if narrow else 4
        for cell in self._global_cells:
            self._global_grid.removeWidget(cell)
        for index, cell in enumerate(self._global_cells):
            self._global_grid.addWidget(cell, index // columns, index % columns)
        for col in range(4):
            self._global_grid.setColumnStretch(col, 1 if col < columns else 0)
        self._toolbar_layout.removeWidget(self._toolbar_status)
        if narrow:
            self._toolbar_layout.addWidget(self._toolbar_status, 1, 0, 1, 2)
        else:
            self._toolbar_layout.addWidget(self._toolbar_status, 0, 1)

    def _apply_theme(self, _name=None):
        self.setStyleSheet(f"""
            QWidget#softstartPanel QWidget {{ background: {theme.hex('panel_bg')}; }}
            QWidget#softstartPanel QLabel, QWidget#softstartPanel QCheckBox {{ background: transparent; }}
            QWidget#softstartPanel QLabel[softstartRole="muted"] {{ color: {theme.hex('muted')}; }}
            QWidget#softstartPanel QLabel[softstartRole="title"] {{ font-size:16px; font-weight:bold; }}
            QWidget#softstartPanel QLabel[softstartRole="heading"] {{ font-size:13px; font-weight:bold; }}
            QWidget#softstartToolbar, QWidget#softstartGlobal {{ border-bottom:1px solid {theme.hex('border')}; }}
            QWidget#softstartFooter, QWidget#softstartHelp {{ border-top:1px solid {theme.hex('border')}; }}
            QWidget#softstartEstimator {{ border-left:1px solid {theme.hex('border')}; }}
            QWidget#softstartPanel QAbstractSpinBox, QWidget#softstartPanel QComboBox {{
                background: {theme.hex('input_bg')}; min-height:26px; padding:2px 6px;
            }}
            QWidget#softstartPanel QPushButton {{ background:{theme.hex('btn_bg')}; min-height:26px; padding:2px 8px; }}
            QWidget#softstartPanel QPushButton:hover {{ background:{theme.hex('btn_hover')}; }}
            QWidget#softstartPanel QPushButton:checked, QPushButton#softstartSend:enabled {{
                background:{theme.hex('sel_bg')}; color:{theme.hex('sel_text')}; border-color:{theme.hex('title')};
            }}
            QLabel#softstartEstimate {{ color:{theme.hex('accent')}; font-size:22px; font-weight:bold; }}
            QLabel#softstartWarning, QLabel[softstartRole="dirtyLegend"] {{ color:{theme.hex('value_hot')}; }}
        """)
        for field in self._fields.values():
            field.refresh_dirty()
        self._update_state_label()
        self._curve_preview.update()

    def _make_spin(self, cls, vmin, vmax, default, decimals=None):
        spin = cls()
        spin.setRange(vmin, vmax)
        if decimals is not None:
            spin.setDecimals(decimals)
        spin.setValue(default)
        spin.setMinimumWidth(90)
        spin.setMaximumWidth(160)   # 防宽窗口下输入框被拉伸导致界面稀疏
        spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        spin.valueChanged.connect(self._on_spin_changed)
        return spin

    # ------------------------------------------------------------------ dirty 跟踪
    def _on_spin_changed(self):
        # 找到发送者对应的字段
        sender = self.sender()
        for f in self._fields.values():
            if f.spin is sender:
                f.refresh_dirty()
                break
        self._duration_ms.setText(self._ms_text(self._duration_spin.value()))
        self._fallback_ms.setText(self._ms_text(self._fallback_spin.value()))
        self._update_state_label()
        self._update_estimate()

    def _on_checkbox_changed(self, _checked):
        self._update_state_label()

    def _on_combo_changed(self, _idx):
        self._update_state_label()
        self._update_estimate()

    def _dirty_pids(self):
        """返回 dirty 字段的 [(param_id, 新值, 旧值)] 列表"""
        result = []
        for f in self._fields.values():
            if f.is_dirty():
                pid = self._field_pid(f.name)
                result.append((pid, f.current(), f.baseline))
        # 非数值控件单独跟踪。
        if self._enable_baseline is not None and \
                int(self._enable_check.isChecked()) != self._enable_baseline:
            result.append((ss.PID_ENABLE, int(self._enable_check.isChecked()), self._enable_baseline))
        if self._shape_baseline is not None and \
                self._shape_buttons.checkedId() != self._shape_baseline:
            result.append((ss.PID_SHAPE, self._shape_buttons.checkedId(), self._shape_baseline))
        return result

    @staticmethod
    def _field_pid(name):
        return getattr(ss, "PID_" + name.upper())

    @staticmethod
    def _ms_text(counts):
        ms = counts * _DT_SECONDS * 1000.0
        return f"≈ {ms:g} ms"

    def _update_state_label(self):
        dirty = len(self._dirty_pids())
        if not self._link_active:
            text, color = "未连接", theme.hex("muted")
        elif dirty > 0:
            text, color = f"{dirty} 项待下发 · 未固化", theme.hex("value_hot")
        else:
            text, color = "已同步", theme.hex("accent")
        self._state_label.setText(text)
        self._state_label.setStyleSheet(f"color:{color};")
        self._send_btn.setEnabled(self._link_active and dirty > 0)
        self._save_btn.setEnabled(self._link_active)
        self._read_btn.setEnabled(self._link_active)
        enable_dirty = (self._enable_baseline is not None and
                        int(self._enable_check.isChecked()) != self._enable_baseline)
        self._enable_check.setStyleSheet(
            f"color:{theme.hex('value_hot')};" if enable_dirty else "")
        shape_dirty = (self._shape_baseline is not None and
                       self._shape_buttons.checkedId() != self._shape_baseline)
        for button in self._shape_buttons.buttons():
            button.setStyleSheet(
                f"border:1px solid {theme.hex('value_hot')};"
                if shape_dirty and button.isChecked() else "")

    # ------------------------------------------------------------------ 估算器
    def _current_config(self):
        """从控件收集当前配置(用于估算, 不要求已读取)"""
        cfg = ss.SmoothConfig()
        cfg.enable = int(self._enable_check.isChecked())
        cfg.shape = self._shape_buttons.checkedId()
        cfg.duration = self._duration_spin.value()
        for f in self._fields.values():
            setattr(cfg, f.name, f.current())
        cfg.mode_sw_fallback = self._fallback_spin.value()
        return cfg

    def _update_estimate(self):
        cfg = self._current_config()
        _disp, rate_field, _unit = _ESTIM_TARGETS[self._estim_combo.currentIndex()]
        delta = self._estim_delta.value()
        rate = getattr(cfg, rate_field)
        rate_unit = next(unit for name, _, unit, _ in _RATE_FIELDS if name == rate_field)
        self._estim_metrics.setText(f"当前 rate  {rate:g} {rate_unit}")
        if rate <= 0:
            self._estim_result.setText("—")
            self._estim_note.setText("该字段 rate=0，不参与速率模式")
            # rate=0: X 轴退回兜底时长(次数×dt)
            self._curve_preview.set_state(cfg.shape, cfg.duration * _DT_SECONDS)
            return
        seconds = ss.estimate_transition_seconds(cfg, delta, rate)
        self._estim_result.setText(f"{seconds:.2f} s")
        if seconds > 0:
            peak = delta / seconds * (1.5 if cfg.shape == 1 else 1.0)
            self._estim_metrics.setText(
                f"当前 rate  {rate:g} {rate_unit}    峰值斜率  {peak:g} {rate_unit}")
        if cfg.shape == 1:
            self._estim_note.setText("S 曲线峰值斜率为平均 1.5 倍，起停更柔和")
        else:
            self._estim_note.setText("线性斜坡，恒定斜率")
        self._curve_preview.set_state(cfg.shape, seconds)

    # ------------------------------------------------------------------ 事件
    def _on_read_clicked(self):
        self.smooth_cfg_read_requested.emit()

    def _on_send_clicked(self):
        dirty = self._dirty_pids()
        if not dirty:
            return
        for pid, value, _old in dirty:
            self.smooth_cfg_set_requested.emit(pid, value)
        # 300ms 后自动回读校验(留出逐帧应答时间)
        QTimer.singleShot(300, self.smooth_cfg_read_requested.emit)

    def _on_save_clicked(self):
        dirty = self._dirty_pids()
        if dirty:
            ret = QMessageBox.question(
                self, "固化前下发",
                f"还有 {len(dirty)} 项修改未下发，是否先下发再固化？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel)
            if ret == QMessageBox.StandardButton.Cancel:
                return
            if ret == QMessageBox.StandardButton.Yes:
                for pid, value, _old in dirty:
                    self.smooth_cfg_set_requested.emit(pid, value)
        ret = QMessageBox.question(
            self, "固化确认",
            "将把当前配置写入 EEPROM + Flash 双备份。\nEEPROM 有写入寿命，确认固化？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret == QMessageBox.StandardButton.Yes:
            self.smooth_cfg_save_requested.emit()

    def _on_reset_clicked(self):
        """本地恢复编译期默认值(不下发, 需手动下发)"""
        self._enable_check.setChecked(bool(ss.DEFAULTS[ss.PID_ENABLE]))
        self._shape_buttons.button(ss.DEFAULTS[ss.PID_SHAPE]).setChecked(True)
        self._duration_spin.setValue(ss.DEFAULTS[ss.PID_DURATION])
        self._fallback_spin.setValue(ss.DEFAULTS[ss.PID_MODE_SW_FALLBACK])
        for f in self._fields.values():
            f.spin.setValue(ss.DEFAULTS[self._field_pid(f.name)])
        self._duration_ms.setText(self._ms_text(self._duration_spin.value()))
        self._fallback_ms.setText(self._ms_text(self._fallback_spin.value()))
        self._update_state_label()
        self._update_estimate()

    # ------------------------------------------------------------------ 外部接口
    def set_link_active(self, active: bool):
        """连接状态变化"""
        self._link_active = active
        self._update_state_label()

    def auto_read_if_connected(self):
        """切到本 Tab 时由主窗口调用: 已连接则自动读取"""
        if self._link_active:
            self.smooth_cfg_read_requested.emit()

    def apply_config(self, cfg: "ss.SmoothConfig"):
        """0xA4 整块应答回填(清 dirty 基线)"""
        self._enable_check.setChecked(bool(cfg.enable))
        self._shape_buttons.button(cfg.shape).setChecked(True)
        self._duration_spin.setValue(cfg.duration)
        self._fallback_spin.setValue(cfg.mode_sw_fallback)
        self._enable_baseline = cfg.enable
        self._shape_baseline = cfg.shape
        for f in self._fields.values():
            f.spin.setValue(getattr(cfg, f.name))
            f.set_baseline(getattr(cfg, f.name))
        self._duration_ms.setText(self._ms_text(cfg.duration))
        self._fallback_ms.setText(self._ms_text(cfg.mode_sw_fallback))
        self._update_state_label()
        self._update_estimate()
