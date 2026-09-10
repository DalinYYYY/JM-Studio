"""
缓启配置面板 (顶级 Tab)。

对应固件 ref_smooth_cfg_t 缓启动渐变配置, 经 0xA3/0xA4 读写:
  1. 工具栏 — 读取配置(0xA4 FF 整块) / 下发修改(逐字段 0xA3, 仅 dirty) / 固化 EEPROM(0xEA) / 恢复默认
  2. 全局设置 — 总开关 / 渐变形状 / 兜底时长
  3. 目标速率限制 — 位置/速度/力矩/电流 rate (过渡时长=|Δ|/rate)
  4. 突变触发阈值 — 六字段 (小于阈值直接透传)
  5. 跨模式切换 — 兜底时长 (rate 全 0 时生效)
  6. 过渡时长估算器 — 目标类型 + Δ目标 → 预计时长

信号:
  smooth_cfg_read_requested()                — 主窗口连 client.smooth_cfg_get(0xFF)
  smooth_cfg_set_requested(int, object)       — 主窗口连 client.smooth_cfg_set(pid, value)
  smooth_cfg_save_requested()                — 主窗口连 _on_config_save(0) → 0xEA

时长换算: 控制环 10kHz(dt=100µs), 次数×100µs=时间。
"""
from PyQt6.QtCore import pyqtSignal, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
    QDoubleSpinBox, QSpinBox, QComboBox, QScrollArea, QWidget, QGridLayout,
    QMessageBox, QSizePolicy,
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

# dirty 高亮样式
_DIRTY_SS = "QAbstractSpinBox { border: 1px solid #5a8dff; }"


class _SpinField:
    """字段条目: 控件 + 单位标签 + dirty 基准跟踪"""

    def __init__(self, name, spin, unit_label=None):
        self.name = name
        self.spin = spin
        self.unit_label = unit_label
        self.baseline = None  # 读取回填时记录基准值

    def set_baseline(self, value):
        self.baseline = value
        self.refresh_dirty()

    def current(self):
        return self.spin.value()

    def is_dirty(self):
        if self.baseline is None:
            return False
        return abs(self.current() - self.baseline) > 1e-9

    def refresh_dirty(self):
        dirty = self.is_dirty()
        self.spin.setStyleSheet(_DIRTY_SS if dirty else "")
        if self.unit_label is not None:
            color = theme.hex("accent") if dirty else theme.hex("muted")
            self.unit_label.setStyleSheet(f"color:{color};")


class _CurvePreview(QWidget):
    """渐变形状实时预览: 归一化混合曲线 out(t) = (1-r)·old + r·target。

    当前 shape 实线高亮, 另一形状虚线参考; SCURVE 标注峰值斜率点。
    X 轴总时长随估算参数(rate/Δ)或兜底时长实时变化。
    """

    _MARGIN_L = 34   # Y 轴标注宽度
    _MARGIN_R = 12
    _MARGIN_T = 14   # 顶部图例
    _MARGIN_B = 22    # X 轴标注高度

    def __init__(self, parent=None):
        super().__init__(parent)
        self._shape = 0
        self._total_seconds = 0.5   # X 轴总时长(0=未知)
        self.setMinimumSize(240, 170)
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
        grid_pen = QPen(QColor(255, 255, 255, 26), 1)
        p.setPen(grid_pen)
        for i in range(5):  # 4 等分横线
            y = top + (bottom - top) * i / 4.0
            p.drawLine(left, int(y), right, int(y))
        p.drawLine(left, top, left, bottom)
        p.drawLine(left, bottom, right, bottom)

        axis_color = QColor(theme.hex("muted"))
        p.setPen(QPen(axis_color, 1))
        font = p.font()
        font.setPointSizeF(8.0)
        p.setFont(font)
        p.drawText(0, top - 2, left - 4, 16,
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "100%")
        p.drawText(0, bottom - 8, left - 4, 16,
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "0")

        # X 轴时长刻度
        if self._total_seconds > 0:
            mid_s = self._total_seconds / 2.0
            p.drawText(left - 6, bottom + 3, 60, 14,
                       Qt.AlignmentFlag.AlignHCenter, f"{mid_s:.2f}s")
            p.drawText(right - 56, bottom + 3, 60, 14,
                       Qt.AlignmentFlag.AlignRight, f"{self._total_seconds:.2f}s")
        else:
            p.drawText(left, bottom + 3, right - left, 14,
                       Qt.AlignmentFlag.AlignHCenter, "rate=0，时长由兜底时长决定")

        # 参考曲线(非当前 shape): 虚线灰
        other = 1 - self._shape
        self._draw_curve(p, other, QPen(QColor(255, 255, 255, 70), 1, Qt.PenStyle.DashLine),
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
        p.setPen(QPen(QColor(255, 255, 255, 70), 1, Qt.PenStyle.DashLine))
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
        self._dirty_counterparts = {}  # 字段名 -> 基准值描述(旧值)
        self._build_ui()
        self._update_state_label()
        self._update_estimate()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)   # 边距集中在 body, 避免与默认 9px 叠加导致 Tab 内留白过大

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        # 左列配置区(控件聚拢) + 右侧说明栏: 宽窗口下不留大片空白
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(4, 4, 4, 4)
        body_layout.setSpacing(8)
        scroll.setWidget(body)
        root.addWidget(scroll)

        left = QVBoxLayout()
        left.setSpacing(6)
        left.addWidget(self._build_toolbar())
        left.addWidget(self._build_global_group())
        left.addWidget(self._build_rate_group())
        left.addWidget(self._build_thresh_group())
        left.addWidget(self._build_mode_sw_group())
        # 估算器/预览组吃掉剩余高度, 避免底部留白
        left.addWidget(self._build_estimator_group(), 1)
        body_layout.addLayout(left, 1)
        body_layout.addWidget(self._build_help_panel())

    def _build_help_panel(self):
        """右侧固定宽度说明栏: 填充宽窗口剩余空间, 收纳使用流程与参数语义"""
        box = QGroupBox("使用说明")
        box.setFixedWidth(252)
        box.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(10, 8, 10, 8)

        text = QLabel(
            "<b>配置流程</b><br>"
            "① 读取配置 → ② 修改参数 → ③ 下发修改 → ④ 固化 EEPROM"
            "<br><br>"
            "<b>生效机制</b><br>"
            "0xA3 下发即写入 RAM 立即生效；0xEA 固化到 EEPROM+Flash 双备份后断电保持"
            "<br><br>"
            "<b>过渡时长</b><br>"
            "速率模式：|Δ目标| / rate，随目标阶跃自适应；"
            "rate 全 0 时改由兜底时长决定"
            "<br><br>"
            "<b>突变阈值</b><br>"
            "单拍目标变化小于阈值 → 直接透传不渐变，避免微小调节被拖慢"
            "<br><br>"
            "<b>渐变形状</b><br>"
            "线性：恒斜率；S 曲线：起停柔和，峰值斜率≈平均 1.5 倍"
            "<br><br>"
            "<b>待下发标记</b><br>"
            "与设备不一致的项显示蓝框，右上角提示待下发数量，下发后自动回读校验"
            "<br><br>"
            "<b><span style='color:#e0a458'>⚠ EEPROM 有写入寿命，请勿频繁固化</span></b>"
        )
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        text.setStyleSheet(f"color:{theme.hex('text')}; font-size:12px;")
        lay.addWidget(text)
        lay.addStretch()
        return box

    def _build_toolbar(self):
        bar = QGroupBox()
        lay = QHBoxLayout(bar)

        self._read_btn = QPushButton("读取配置")
        self._read_btn.setObjectName("primary")
        self._read_btn.clicked.connect(self._on_read_clicked)

        self._send_btn = QPushButton("下发修改")
        self._send_btn.clicked.connect(self._on_send_clicked)

        self._save_btn = QPushButton("固化 EEPROM")
        self._save_btn.clicked.connect(self._on_save_clicked)

        self._reset_btn = QPushButton("恢复默认")
        self._reset_btn.clicked.connect(self._on_reset_clicked)

        self._state_label = QLabel()
        self._state_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        for w in (self._read_btn, self._send_btn, self._save_btn, self._reset_btn):
            lay.addWidget(w)
        lay.addStretch()
        lay.addWidget(self._state_label)
        return bar

    def _build_global_group(self):
        box = QGroupBox("全局设置")
        grid = QGridLayout(box)

        self._enable_check = QCheckBox("启用缓启动渐变")
        self._enable_check.toggled.connect(self._on_checkbox_changed)
        grid.addWidget(self._enable_check, 0, 0)

        grid.addWidget(QLabel("渐变形状"), 0, 1)
        self._shape_combo = QComboBox()
        self._shape_combo.addItem("线性（恒斜率）", 0)
        self._shape_combo.addItem("S 曲线（起停柔和）", 1)
        self._shape_combo.currentIndexChanged.connect(self._on_combo_changed)
        self._shape_combo.setMinimumWidth(150)
        self._shape_combo.setMaximumWidth(220)
        grid.addWidget(self._shape_combo, 0, 2)

        grid.addWidget(QLabel("兜底时长"), 0, 3)
        self._duration_spin = self._make_spin(QSpinBox, 1, 1000000, ss.DEFAULTS[ss.PID_DURATION])
        grid.addWidget(self._duration_spin, 0, 4)
        self._duration_ms = QLabel()
        grid.addWidget(self._duration_ms, 0, 5)

        hint = QLabel("仅当 4 个速率全部为 0 时兜底时长才生效 · 写入 RAM 立即生效，0xEA 固化后重启保持")
        hint.setStyleSheet(f"color:{theme.hex('muted')}; font-size:11px;")
        grid.addWidget(hint, 1, 0, 1, 6)
        # 尾列吃掉剩余宽度, 控件聚拢不稀疏
        grid.setColumnStretch(6, 1)

        # 全局字段 dirty 跟踪基线
        self._enable_baseline = None
        self._shape_baseline = None
        return box

    def _build_rate_group(self):
        box = QGroupBox("目标速率限制（rate）")
        lay = QVBoxLayout(box)

        head = QHBoxLayout()
        title_hint = QLabel("过渡时长 = |Δ目标| / rate")
        title_hint.setStyleSheet(f"color:{theme.hex('muted')};")
        title_hint.setAlignment(Qt.AlignmentFlag.AlignRight)
        head.addStretch()
        head.addWidget(title_hint)
        lay.addLayout(head)

        grid = QGridLayout()
        for i, (name, disp, unit, dec) in enumerate(_RATE_FIELDS):
            row, col = divmod(i, 2)
            grid.addWidget(QLabel(disp), row * 2, col * 3)
            spin = self._make_spin(QDoubleSpinBox, 0.0, 100000.0,
                                   ss.DEFAULTS[getattr(ss, "PID_" + name.upper())], dec)
            grid.addWidget(spin, row * 2, col * 3 + 1)
            unit_label = QLabel(unit)
            grid.addWidget(unit_label, row * 2, col * 3 + 2)
            self._fields[name] = _SpinField(name, spin, unit_label)
        grid.setColumnStretch(6, 1)   # 尾列吃掉剩余宽度, 控件聚拢
        lay.addLayout(grid)

        hint = QLabel("rate 置 0 = 该字段退出速率模式（渐变时长改由兜底时长决定）")
        hint.setStyleSheet(f"color:{theme.hex('muted')}; font-size:11px;")
        lay.addWidget(hint)
        return box

    def _build_thresh_group(self):
        box = QGroupBox("突变触发阈值（thresh）")
        lay = QVBoxLayout(box)

        head = QHBoxLayout()
        title_hint = QLabel("单拍突变小于阈值 → 直接透传")
        title_hint.setStyleSheet(f"color:{theme.hex('muted')};")
        title_hint.setAlignment(Qt.AlignmentFlag.AlignRight)
        head.addStretch()
        head.addWidget(title_hint)
        lay.addLayout(head)

        grid = QGridLayout()
        for i, (name, disp, unit, dec) in enumerate(_THRESH_FIELDS):
            row, col = divmod(i, 3)
            grid.addWidget(QLabel(disp), row * 2, col * 3)
            spin = self._make_spin(QDoubleSpinBox, 0.0, 100000.0,
                                   ss.DEFAULTS[getattr(ss, "PID_" + name.upper())], dec)
            grid.addWidget(spin, row * 2, col * 3 + 1)
            unit_label = QLabel(unit)
            grid.addWidget(unit_label, row * 2, col * 3 + 2)
            self._fields[name] = _SpinField(name, spin, unit_label)
        grid.setColumnStretch(9, 1)   # 尾列吃掉剩余宽度, 控件聚拢
        lay.addLayout(grid)
        return box

    def _build_mode_sw_group(self):
        box = QGroupBox("跨模式切换")
        grid = QGridLayout(box)

        grid.addWidget(QLabel("兜底时长"), 0, 0)
        self._fallback_spin = self._make_spin(QSpinBox, 1, 1000000,
                                              ss.DEFAULTS[ss.PID_MODE_SW_FALLBACK])
        grid.addWidget(self._fallback_spin, 0, 1)
        self._fallback_ms = QLabel()
        grid.addWidget(self._fallback_ms, 0, 2)

        hint = QLabel("使能 / 切模式的目标阶跃按 rate 自适应；rate 全 0 时用此时长")
        hint.setStyleSheet(f"color:{theme.hex('muted')}; font-size:11px;")
        grid.addWidget(hint, 1, 0, 1, 3)
        grid.setColumnStretch(3, 1)   # 尾列吃掉剩余宽度, 控件聚拢
        return box

    def _build_estimator_group(self):
        box = QGroupBox("过渡时长估算器 / 渐变形状预览")
        lay = QHBoxLayout(box)

        grid_host = QWidget()
        # 裸 QWidget 会被全局 QSS 的 QWidget{background:app_bg} 命中而发黑, 置透明跟随 GroupBox 底色
        grid_host.setStyleSheet("background: transparent;")
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)

        grid.addWidget(QLabel("目标类型"), 0, 0)
        self._estim_combo = QComboBox()
        for disp, _, unit in _ESTIM_TARGETS:
            self._estim_combo.addItem(f"{disp} ({unit})")
        self._estim_combo.currentIndexChanged.connect(self._update_estimate)
        grid.addWidget(self._estim_combo, 0, 1)

        grid.addWidget(QLabel("Δ目标"), 1, 0)
        self._estim_delta = QDoubleSpinBox()
        self._estim_delta.setDecimals(2)
        self._estim_delta.setRange(0.0, 100000.0)
        self._estim_delta.setValue(50.0)
        self._estim_delta.valueChanged.connect(self._update_estimate)
        grid.addWidget(self._estim_delta, 1, 1)

        grid.addWidget(QLabel("预计时长"), 2, 0)
        self._estim_result = QLabel("—")
        self._estim_result.setStyleSheet(
            f"color:{theme.hex('accent')}; font-size:16px; font-weight:bold;")
        grid.addWidget(self._estim_result, 2, 1, 1, 2)

        self._estim_note = QLabel("")
        self._estim_note.setStyleSheet(f"color:{theme.hex('muted')}; font-size:11px;")
        self._estim_note.setWordWrap(True)
        grid.addWidget(self._estim_note, 3, 0, 1, 3)

        self._curve_preview = _CurvePreview()

        lay.addWidget(grid_host, 1)
        lay.addWidget(self._curve_preview, 2)
        return box

    def _make_spin(self, cls, vmin, vmax, default, decimals=None):
        spin = cls()
        spin.setRange(vmin, vmax)
        if decimals is not None:
            spin.setDecimals(decimals)
        spin.setValue(default)
        spin.setMinimumWidth(90)
        spin.setMaximumWidth(160)   # 防宽窗口下输入框被拉伸导致界面稀疏
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
        self._update_estimate()

    def _dirty_pids(self):
        """返回 dirty 字段的 [(param_id, 新值, 旧值)] 列表"""
        result = []
        for f in self._fields.values():
            if f.is_dirty():
                pid = self._field_pid(f.name)
                result.append((pid, f.current(), f.baseline))
        # 全局三字段
        if self._enable_baseline is not None and \
                int(self._enable_check.isChecked()) != self._enable_baseline:
            result.append((ss.PID_ENABLE, int(self._enable_check.isChecked()), self._enable_baseline))
        if self._shape_baseline is not None and \
                self._shape_combo.currentData() != self._shape_baseline:
            result.append((ss.PID_SHAPE, self._shape_combo.currentData(), self._shape_baseline))
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
            text, color = f"{dirty} 项待下发 · 未固化", "#e0a458"
        else:
            text, color = "已同步", theme.hex("accent")
        self._state_label.setText(text)
        self._state_label.setStyleSheet(f"color:{color};")
        self._send_btn.setEnabled(self._link_active and dirty > 0)
        self._save_btn.setEnabled(self._link_active)
        self._read_btn.setEnabled(self._link_active)

    # ------------------------------------------------------------------ 估算器
    def _current_config(self):
        """从控件收集当前配置(用于估算, 不要求已读取)"""
        cfg = ss.SmoothConfig()
        cfg.enable = int(self._enable_check.isChecked())
        cfg.shape = self._shape_combo.currentData()
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
        if rate <= 0:
            self._estim_result.setText("—")
            self._estim_note.setText("该字段 rate=0，不参与速率模式")
            # rate=0: X 轴退回兜底时长(次数×dt)
            self._curve_preview.set_state(cfg.shape, cfg.duration * _DT_SECONDS)
            return
        seconds = ss.estimate_transition_seconds(cfg, delta, rate)
        self._estim_result.setText(f"{seconds:.2f} s")
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
        self._shape_combo.setCurrentIndex(ss.DEFAULTS[ss.PID_SHAPE])
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
        self._shape_combo.setCurrentIndex(cfg.shape)
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
