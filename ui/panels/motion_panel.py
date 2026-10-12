"""运动控制面板(数据驱动)

模式下拉由 registry 中"运动控制/高级力控/轨迹同步/对拖台负载/特殊应用"类命令填充;
选中命令后按其 fields 动态生成输入框; 发送时用 registry.pack_command 打包。
常规命令只要 CSV 加一行即可出现；特殊交互可在本面板增加控件。

模式列表可由 resources/motion_modes.txt 白名单配置(按文件顺序显示),
详见 motion_mode_config 模块; 文件缺失或无有效条目时显示全部。
该文件随 resources 嵌入 exe, 打包后对最终用户不可见。
"""

from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QFormLayout, QHBoxLayout, QWidget,
    QComboBox, QLabel, QPushButton, QDoubleSpinBox, QSpinBox, QMessageBox,
)
from PyQt6.QtCore import QTimer, pyqtSignal

from jmproto import codec
from jmproto.cmd_def import JmCmd
from jmproto.registry import Field, validate_motion_values
from ui.panels.motion_mode_config import load_motion_mode_cmds


class MotionPanel(QGroupBox):
    """数据驱动的运动指令面板。发出 send_command(cmd:int, values:dict)。"""

    send_command = pyqtSignal(int, dict)

    # 纳入运动面板的命令类别
    CATEGORIES = ("运动控制", "高级力控", "轨迹同步", "对拖台负载", "特殊应用")

    _DEFAULTS = {
        JmCmd.PP: {'flags': 0, 'vel': 1.0, 'acc': 10.0, 'dec': 10.0, 'jerk': 100.0},
        JmCmd.PV: {'acc': 10.0, 'dec': 10.0, 'current_lim': 0.0},
        JmCmd.PT: {'slope': 1.0},
        JmCmd.JOG: {'speed': 1.0, 'acc': 10.0, 'dec': 10.0},
    }
    _FIELD_LABELS = {
        'flags': '位置方式 / 曲线', 'pos': '目标位置 (rad)',
        'vel': '目标/最大速度 (rad/s)', 'acc': '加速度 (rad/s²)',
        'dec': '减速度 (rad/s²)', 'jerk': '加加速度 (rad/s³)',
        'torque': '目标转矩 (Nm)', 'slope': '转矩斜率 (Nm/s)',
        'current_lim': '电流上限 (A，0=默认)',
        'vel_lim': '速度上限 (rad/s，0=默认)',
        'speed': '点动速度 (rad/s)',
        'kp': 'Kp 刚度 (Nm/rad)',
        'kd': 'Kd 阻尼 (Nm·s/rad)',
        'tff': '前馈转矩 (Nm)',
    }

    def __init__(self, registry, parent=None, mode_subset=None):
        super().__init__("运动控制", parent)
        self._reg = registry
        self._field_widgets = {}   # 字段名 -> widget
        self._jog_pressed = 0
        self._jog_timer = QTimer(self)
        self._jog_timer.setInterval(100)
        self._jog_timer.timeout.connect(self._send_jog_keepalive)
        self._mode_subset = mode_subset   # 应用形态模式子集(如 tester profile), None=用全局白名单
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)

        # 模式选择
        top = QHBoxLayout()
        top.addWidget(QLabel("控制模式:"))
        self._combo_mode = QComboBox()
        self._specs = self._filter_specs(self._reg.commands_by_category(*self.CATEGORIES))
        for spec in self._specs:
            self._combo_mode.addItem(f"{spec.name}  (0x{spec.cmd:02X})", spec.cmd)
        self._combo_mode.currentIndexChanged.connect(self._rebuild_fields)
        top.addWidget(self._combo_mode, 1)
        layout.addLayout(top)

        # 动态字段区
        self._form_host = QWidget()
        # 裸 QWidget 会被全局 QSS 的 QWidget{background:app_bg} 命中而发黑, 置透明跟随 GroupBox 底色
        self._form_host.setStyleSheet("background: transparent;")
        self._form = QFormLayout(self._form_host)
        self._form.setContentsMargins(0, 6, 0, 6)
        layout.addWidget(self._form_host)

        # 提示标签(无载荷命令)
        self._hint = QLabel("")
        self._hint.setStyleSheet("color: #888;")
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        # 发送按钮
        self._btn_send = QPushButton("发送运动指令")
        self._btn_send.setStyleSheet(
            "background-color: #673AB7; color: white; font-weight: bold; padding: 8px;")
        self._btn_send.clicked.connect(self._on_send)
        layout.addWidget(self._btn_send)

        if self._specs:
            self._rebuild_fields()

    def _current_spec(self):
        cmd = self._combo_mode.currentData()
        return self._reg.get_command(cmd) if cmd is not None else None

    def _filter_specs(self, specs):
        """按白名单过滤, 白名单顺序即下拉框顺序。

        mode_subset(应用形态子集)优先于 motion_modes.txt;
        无白名单(返回 None)时原样返回; 白名单条目均不在面板类别中时
        回退全部, 防止把下拉框配置为空。加载层已对重复条目去重。
        """
        allowed = load_motion_mode_cmds(self._reg, subset=self._mode_subset)
        if allowed is None:
            return specs
        by_cmd = {s.cmd: s for s in specs}
        filtered = [by_cmd[c] for c in allowed if c in by_cmd]
        return filtered if filtered else specs

    def _clear_form(self):
        while self._form.count():
            item = self._form.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._field_widgets.clear()

    def _make_widget(self, field, cmd):
        """按字段类型生成输入控件"""
        if cmd == JmCmd.PP and field.name == 'flags':
            w = QComboBox()
            for label, flags in (
                ('绝对位置 / 梯形', 0), ('相对位置 / 梯形', 1),
                ('绝对位置 / S 曲线', 2), ('相对位置 / S 曲线', 3),
            ):
                w.addItem(label, flags)
            return w
        if codec.is_float_type(field.dtype):
            w = QDoubleSpinBox()
            w.setRange(-1e6, 1e6)
            w.setDecimals(4)
            w.setValue(self._DEFAULTS.get(cmd, {}).get(field.name, 0.0))
            if cmd == JmCmd.MIT and field.name == 'kd':
                w.setSingleStep(0.01)
            if cmd in (JmCmd.PP, JmCmd.PV, JmCmd.PT, JmCmd.JOG) and field.name in (
                    'vel', 'acc', 'dec', 'jerk', 'slope', 'current_lim', 'vel_lim', 'speed'):
                # PV 的 vel 是可带符号的目标速度。
                if not (cmd == JmCmd.PV and field.name == 'vel'):
                    w.setMinimum(0.0)
        else:
            w = QSpinBox()
            limits = {'u8': (0, 255), 'i8': (-128, 127),
                      'u16': (0, 65535), 'i16': (-32768, 32767)}
            # QSpinBox 最大只支持 int32，u32 按可显示区间处理。
            w.setRange(*limits.get(field.dtype, (-2147483648, 2147483647)))
            w.setValue(0)
        return w

    @staticmethod
    def _widget_value(widget):
        return widget.currentData() if isinstance(widget, QComboBox) else widget.value()

    def _collect_values(self):
        return {name: self._widget_value(w) for name, w in self._field_widgets.items()}

    def _emit_validated(self, cmd, values):
        try:
            if cmd == JmCmd.PP:
                validate_motion_values(cmd, values)
            else:
                self._reg.pack_command(cmd, values)
        except (ValueError, OverflowError) as exc:
            QMessageBox.warning(self, "运动参数无效", str(exc))
            return False
        self.send_command.emit(int(cmd), values)
        return True

    def _on_jog_press(self, direction):
        values = self._collect_values()
        values['dir'] = direction
        if self._emit_validated(JmCmd.JOG, values):
            self._jog_pressed = direction
            self._jog_timer.start()

    def _send_jog_keepalive(self):
        if self._jog_pressed:
            values = self._collect_values()
            values['dir'] = self._jog_pressed
            self._emit_validated(JmCmd.JOG, values)

    def _on_jog_release(self):
        if not self._jog_pressed:
            return
        self._jog_pressed = 0
        self._emit_jog_stop()

    def set_link_active(self, connected: bool):
        if not connected:
            self._jog_pressed = 0
            self._jog_timer.stop()

    def _emit_jog_stop(self):
        self._jog_pressed = 0
        self._jog_timer.stop()
        values = self._collect_values()
        values['dir'] = 0
        self._emit_validated(JmCmd.JOG, values)

    def hideEvent(self, event):
        self._on_jog_release()
        super().hideEvent(event)

    def _rebuild_fields(self):
        self._on_jog_release()
        self._clear_form()
        spec = self._current_spec()
        if spec is None:
            return
        editable = spec.editable_fields
        for f in editable:
            if spec.cmd == JmCmd.JOG and f.name == 'dir':
                continue  # 方向由按住/松开按钮生成，避免锁存点动命令。
            if spec.cmd == JmCmd.PP and f.name in ('profile_rev', 'seq'):
                continue  # 0x5F COMMIT 回读后由客户端填入，不允许手工填写。
            w = self._make_widget(f, spec.cmd)
            self._field_widgets[f.name] = w
            label = self._FIELD_LABELS.get(f.name, f"{f.name} [{f.dtype}]")
            if spec.cmd == JmCmd.MIT and f.name == 'vel':
                label = '目标速度 (rad/s)'
            self._form.addRow(QLabel(f"{label}:"), w)

        if spec.cmd == JmCmd.PP:
            for name in ('vel', 'acc', 'dec', 'jerk'):
                w = self._make_widget(Field(name, 'f32'), spec.cmd)
                self._field_widgets[name] = w
                self._form.addRow(QLabel(f"{self._FIELD_LABELS[name]}:"), w)

        if spec.cmd == JmCmd.JOG:
            jog_host = QWidget()
            jog_host.setStyleSheet("background: transparent;")
            jog_layout = QVBoxLayout(jog_host)
            jog_layout.setContentsMargins(0, 0, 0, 0)
            self._btn_jog_reverse = QPushButton("按住反向")
            self._btn_jog_forward = QPushButton("按住正向")
            self._btn_jog_stop = QPushButton("松开 / 减速停止")
            self._btn_jog_reverse.pressed.connect(lambda: self._on_jog_press(-1))
            self._btn_jog_forward.pressed.connect(lambda: self._on_jog_press(1))
            self._btn_jog_reverse.released.connect(self._on_jog_release)
            self._btn_jog_forward.released.connect(self._on_jog_release)
            self._btn_jog_stop.clicked.connect(self._emit_jog_stop)
            for button in (self._btn_jog_reverse, self._btn_jog_forward, self._btn_jog_stop):
                jog_layout.addWidget(button)
            self._form.addRow(QLabel("点动操作:"), jog_host)
        self._btn_send.setVisible(spec.cmd != JmCmd.JOG)

        # 提示信息: 固定字段 / 无载荷
        notes = []
        if not spec.has_payload:
            notes.append("该命令无需参数, 直接发送。")
        fixed = [f for f in spec.fields if f.is_fixed]
        if fixed:
            notes.append("固定字段: " + ", ".join(f"{f.name}=0x{f.default:X}" for f in fixed))
        if spec.unit and spec.unit != '-':
            notes.append(f"单位/范围: {spec.unit}")
        if spec.cmd == JmCmd.PP:
            notes.append("先用 0x5F 配置并读回参数，再发 8B 单帧；每次点击生成新轨迹序号。")
        elif spec.cmd == JmCmd.PV:
            notes.append("正负速度均可，切换方向时先减速过零。")
        elif spec.cmd == JmCmd.PT:
            notes.append("电流/速度上限为 0 时使用电机默认上限。")
        elif spec.cmd == JmCmd.JOG:
            notes.append("按住时每 100ms 续发；松开发送 dir=0。失联超过 350ms 时下位机自动减速停下。")
        elif spec.cmd == JmCmd.MIT:
            notes.append("MIT 的目标速度不是限速。转矩=Kp×(目标位置−当前位置)+Kd×(目标速度−实际速度)+前馈。"
                         "先设目标位置为当前反馈，再小步改变；Kd=0 时目标速度不起作用。"
                         "更新固件后，电流输出受电机配置 162（MIT 最大电流）和电机峰值电流共同限制。")
        self._hint.setText("  ".join(notes))
        self._form_host.adjustSize()
        self.adjustSize()
        self.updateGeometry()

    def _on_send(self):
        spec = self._current_spec()
        if spec is None or spec.cmd == JmCmd.JOG:
            return
        self._emit_validated(spec.cmd, self._collect_values())

    # ==================== 配置持久化 ====================
    def get_opts(self) -> dict:
        """收集可持久化的 UI 配置. 返回当前模式 cmd + 各模式字段值字典."""
        opts = {"current_cmd": None, "fields": {}}
        spec = self._current_spec()
        if spec is not None:
            opts["current_cmd"] = int(spec.cmd)
        # 当前模式的字段值
        for name, value in self._collect_values().items():
            try:
                opts["fields"][name] = float(value)
            except Exception:
                pass
        return opts

    def set_opts(self, opts: dict):
        """启动时套用配置 (容错). 注: 仅恢复当前模式选择 + 当前模式字段值;
        其它模式的字段值不保留 (动态构建, 切换模式时会重新生成)."""
        if not isinstance(opts, dict):
            return
        # 恢复模式选择
        cmd = opts.get("current_cmd")
        if cmd is not None:
            try:
                idx = self._combo_mode.findData(int(cmd))
                if idx >= 0:
                    self._combo_mode.setCurrentIndex(idx)
            except Exception:
                pass
        # 恢复当前模式字段值 (模式切换会触发 _rebuild_fields, 故需在信号处理后再赋值)
        fields = opts.get("fields")
        if isinstance(fields, dict):
            for name, val in fields.items():
                w = self._field_widgets.get(name)
                if w is None:
                    continue
                try:
                    if isinstance(w, QComboBox):
                        idx = w.findData(int(val))
                        if idx >= 0:
                            w.setCurrentIndex(idx)
                    else:
                        w.setValue(float(val))
                except Exception:
                    pass
