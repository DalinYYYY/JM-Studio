"""控制面板: 引擎连接 + 系统控制 + 运动控制 + 遥测推送 + 设备信息 + 运行状态 + 菜单配置。

直接构造 MotorCmd 投递给 TwinEngineBridge, 不经过协议层。
运动控制按 ControlMode 动态切换目标字段。

左侧布局对齐主上位机 (ui/main_window.py): 多个独立 QGroupBox 纵向堆叠。
"""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from transport.virtual_engine import ControlMode, MotorCmd
from ui.theme import theme


# 各模式的目标字段规格: (attr, 中文, 单位, 默认, 最小, 最大, 步长, 小数位)
_MODE_FIELDS = {
    ControlMode.POSITION: [
        ("set_pos", "目标位置", "rad", 0.0, -1e6, 1e6, 0.1, 3),
    ],
    ControlMode.VELOCITY: [
        ("set_vel", "目标速度", "rad/s", 0.0, -1e6, 1e6, 1.0, 2),
    ],
    ControlMode.TORQUE: [
        ("set_torque", "目标力矩", "Nm", 0.0, -1000, 1000, 0.1, 3),
    ],
    ControlMode.CURRENT: [
        ("set_id", "目标 Id", "A", 0.0, -100, 100, 0.1, 3),
        ("set_iq", "目标 Iq", "A", 0.0, -100, 100, 0.1, 3),
    ],
    ControlMode.VOLTAGE: [
        ("set_voltage", "目标电压", "V", 0.0, -100, 100, 0.1, 3),
    ],
    ControlMode.DUTY: [
        ("set_duty", "占空比", "", 0.0, -1.0, 1.0, 0.01, 3),
    ],
    ControlMode.IMPEDANCE: [
        ("set_pos", "目标位置", "rad", 0.0, -1e6, 1e6, 0.1, 3),
        ("set_vel", "速度前馈", "rad/s", 0.0, -1e6, 1e6, 0.1, 2),
        ("set_kp", "刚度 Kp", "Nm/rad", 10.0, 0, 1000, 1.0, 2),
        ("set_kd", "阻尼 Kd", "Nm/(rad/s)", 0.5, 0, 100, 0.1, 3),
        ("set_torque_ff", "力矩前馈", "Nm", 0.0, -100, 100, 0.1, 3),
    ],
}

_MODE_LABELS = {
    ControlMode.POSITION: "位置 (POSITION)",
    ControlMode.VELOCITY: "速度 (VELOCITY)",
    ControlMode.TORQUE: "力矩 (TORQUE)",
    ControlMode.CURRENT: "电流 (CURRENT)",
    ControlMode.VOLTAGE: "电压 (VOLTAGE)",
    ControlMode.DUTY: "占空比 (DUTY)",
    ControlMode.IMPEDANCE: "阻抗 (IMPEDANCE/MIT)",
}


class SystemControlGroup(QGroupBox):
    """系统控制按钮组: enable/disable/stop/idle/estop/clear/reset。"""

    command = pyqtSignal(str)   # 命令名

    def __init__(self, parent=None):
        super().__init__("系统控制", parent)
        self._build()

    def _build(self):
        g = QGridLayout(self)

        def mk(text, slot, style=""):
            btn = QPushButton(text)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            if style:
                btn.setStyleSheet(style)
            btn.clicked.connect(slot)
            return btn

        btn_enable = mk("使能 ENABLE", lambda: self.command.emit("enable"),
                        self._btn_style("#2E9E6A"))
        btn_disable = mk("去使能 DISABLE", lambda: self.command.emit("disable"))
        btn_stop = mk("停止 STOP", lambda: self.command.emit("stop"))
        btn_idle = mk("待机 IDLE", lambda: self.command.emit("idle"))
        btn_estop = mk("急停 ESTOP", lambda: self.command.emit("estop"),
                       self._btn_style("#F44336"))
        btn_clear = mk("清障 CLEAR", lambda: self.command.emit("clear"))
        btn_reset = mk("复位 RESET", lambda: self.command.emit("reset"),
                       self._btn_style("#C8963C"))

        g.addWidget(btn_enable, 0, 0)
        g.addWidget(btn_disable, 0, 1)
        g.addWidget(btn_stop, 1, 0)
        g.addWidget(btn_idle, 1, 1)
        g.addWidget(btn_estop, 2, 0)
        g.addWidget(btn_clear, 2, 1)
        g.addWidget(btn_reset, 3, 0, 1, 2)

    @staticmethod
    def _btn_style(bg):
        return (f"QPushButton {{ background: {bg}; color: white; font-weight: bold;"
                f" padding: 6px; border: none; border-radius: 3px; }}"
                f"QPushButton:hover {{ background: {bg}; opacity: 0.85; }}")


class MotionControlGroup(QGroupBox):
    """运动控制: 模式选择 + 动态目标字段 + 发送。发出 MotorCmd。"""

    send_motion = pyqtSignal(object)   # MotorCmd

    def __init__(self, parent=None):
        super().__init__("运动控制", parent)
        self._field_widgets = {}
        self._engine = None   # 由 set_engine 注入, 用于切换模式时同步当前 target
        self._build()

    def set_engine(self, engine):
        """注入引擎句柄, 切换模式时从 fsm 同步当前 target 值到 spinbox。"""
        self._engine = engine

    def _build(self):
        v = QVBoxLayout(self)

        # 模式选择
        top = QHBoxLayout()
        top.addWidget(QLabel("控制模式:"))
        self._combo = QComboBox()
        for mode in (ControlMode.POSITION, ControlMode.VELOCITY, ControlMode.TORQUE,
                     ControlMode.CURRENT, ControlMode.VOLTAGE, ControlMode.DUTY,
                     ControlMode.IMPEDANCE):
            self._combo.addItem(_MODE_LABELS[mode], int(mode))
        self._combo.currentIndexChanged.connect(self._rebuild_fields)
        top.addWidget(self._combo, 1)
        v.addLayout(top)

        # 动态字段区
        self._form_host = QWidget()
        self._form = QFormLayout(self._form_host)
        self._form.setContentsMargins(0, 6, 0, 6)
        v.addWidget(self._form_host)

        # 发送按钮
        self._btn_send = QPushButton("发送运动指令 (start)")
        self._btn_send.setStyleSheet(
            f"QPushButton {{ background: #673AB7; color: white; font-weight: bold; padding: 8px;"
            f" border: none; border-radius: 3px; }}"
            f"QPushButton:hover {{ background: #7E57C2; }}")
        self._btn_send.clicked.connect(self._on_send)
        v.addWidget(self._btn_send)

        self._rebuild_fields()

    def _current_mode(self) -> ControlMode:
        return ControlMode(int(self._combo.currentData()))

    def _clear_form(self):
        while self._form.count():
            it = self._form.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._field_widgets.clear()

    def _rebuild_fields(self):
        self._clear_form()
        mode = self._current_mode()
        # 从 fsm 同步当前 target 值 (若有且非0), 否则用规格默认值
        # 非0回退避免初始 fsm.target_*=0 让用户困惑 (Kp=0 电机不动)
        fsm = getattr(self._engine, "fsm", None) if self._engine else None
        for attr, cn, unit, default, vmin, vmax, step, dec in _MODE_FIELDS[mode]:
            w = QDoubleSpinBox()
            w.setRange(vmin, vmax)
            w.setSingleStep(step)
            w.setDecimals(dec)
            cur_val = None
            if fsm is not None:
                fsm_attr = "target_" + attr.replace("set_", "")
                cur_val = getattr(fsm, fsm_attr, None)
                if cur_val is not None and abs(float(cur_val)) < 1e-12:
                    cur_val = None   # 0 值回退到默认
            w.setValue(float(cur_val) if cur_val is not None else default)
            label = f"{cn}  [{unit}]" if unit else cn
            self._form.addRow(label, w)
            self._field_widgets[attr] = w

    def _on_send(self):
        mode = self._current_mode()
        mc = MotorCmd(start=True, set_mode=mode)
        for attr, w in self._field_widgets.items():
            setattr(mc, attr, float(w.value()))
        self.send_motion.emit(mc)


class StateDisplayGroup(QGroupBox):
    """实时状态显示: sys_state / run_state / control_mode / fault。"""

    def __init__(self, parent=None):
        super().__init__("运行状态", parent)
        self._build()

    def _build(self):
        f = QFormLayout(self)
        f.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._lbl_sys = QLabel("-")
        self._lbl_run = QLabel("-")
        self._lbl_mode = QLabel("-")
        self._lbl_fault = QLabel("无")
        self._lbl_t = QLabel("0.000 s")
        self._lbl_sys.setStyleSheet(f"font-weight: bold; color: {theme.hex('title')};")
        self._lbl_fault.setStyleSheet(f"color: {theme.hex('ok')};")
        f.addRow("顶层状态:", self._lbl_sys)
        f.addRow("运行子状态:", self._lbl_run)
        f.addRow("控制模式:", self._lbl_mode)
        f.addRow("故障:", self._lbl_fault)
        f.addRow("仿真时间:", self._lbl_t)

    def update_telemetry(self, t: dict):
        self._lbl_sys.setText(str(t.get("sys_state_name", "-")))
        self._lbl_run.setText(str(t.get("run_state_name", "-")))
        self._lbl_mode.setText(str(t.get("control_mode_name", "-")))
        fault = int(t.get("fault_flags", 0))
        if fault:
            self._lbl_fault.setText(f"0x{fault:04X}  {t.get('fault_str', '')}")
            self._lbl_fault.setStyleSheet(f"color: {theme.hex('danger')}; font-weight: bold;")
        else:
            self._lbl_fault.setText("无")
            self._lbl_fault.setStyleSheet(f"color: {theme.hex('ok')};")
        self._lbl_t.setText(f"{t.get('t_sim', 0.0):.3f} s")

    def apply_theme(self):
        self._lbl_sys.setStyleSheet(f"font-weight: bold; color: {theme.hex('title')};")
        self._lbl_fault.setStyleSheet(
            f"color: {theme.hex('danger') if self._lbl_fault.text() != '无' else theme.hex('ok')};"
            + (" font-weight: bold;" if self._lbl_fault.text() != "无" else ""))


# ============================================================================
# 引擎连接组 (替代主上位机的"串口连接"组)
# ============================================================================

class EngineConnectionGroup(QGroupBox):
    """引擎连接: 启动/停止仿真 + 引擎状态 + 虚拟电机模式开关。

    twin 直接驱动 DigitalTwinEngine, 无串口; 此组用"启动/停止仿真"替代"连接/断开"。
    """

    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    virtual_mode_toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__("引擎连接", parent)
        self._running = False
        self._virtual_mode = False
        self._build()

    def _build(self):
        g = QGridLayout(self)
        g.setHorizontalSpacing(6)
        g.setVerticalSpacing(4)

        # 启动/停止仿真按钮 (双态)
        self._btn_toggle = QPushButton("启动仿真")
        self._btn_toggle.setCheckable(True)
        self._btn_toggle.clicked.connect(self._on_toggle)
        g.addWidget(self._btn_toggle, 0, 0, 1, 2)

        # 引擎状态显示
        self._lbl_status = QLabel("● 已停止")
        self._lbl_status.setStyleSheet(
            f"color: {theme.hex('muted')}; padding: 2px 4px;")
        g.addWidget(self._lbl_status, 1, 0, 1, 2)

        # 虚拟电机模式开关
        self._btn_virtual = QPushButton("虚拟电机模式: 关")
        self._btn_virtual.setCheckable(True)
        self._btn_virtual.setChecked(False)
        self._btn_virtual.setEnabled(False)
        self._btn_virtual.setToolTip(
            "仅在引擎运行时可用。开启后显示孪生参数、虚拟电机故障注入与错误历史表。")
        self._btn_virtual.toggled.connect(self._on_virtual_toggled)
        self._apply_virtual_btn_style()
        g.addWidget(self._btn_virtual, 2, 0, 1, 2)

        self._apply_toggle_style()

    def _apply_toggle_style(self):
        """启动/停止按钮双态样式"""
        if self._running:
            self._btn_toggle.setText("停止仿真")
            self._btn_toggle.setStyleSheet(
                f"QPushButton {{ background: {theme.hex('danger')}; color: white;"
                f" font-weight: bold; padding: 6px; border: none; border-radius: 3px; }}"
                f"QPushButton:hover {{ background: {theme.hex('danger')}; opacity: 0.85; }}")
            self._lbl_status.setText("● 运行中")
            self._lbl_status.setStyleSheet(
                f"color: {theme.hex('ok')}; padding: 2px 4px; font-weight: bold;")
        else:
            self._btn_toggle.setText("启动仿真")
            self._btn_toggle.setStyleSheet(
                f"QPushButton {{ background: {theme.hex('ok')}; color: white;"
                f" font-weight: bold; padding: 6px; border: none; border-radius: 3px; }}"
                f"QPushButton:hover {{ background: {theme.hex('ok')}; opacity: 0.85; }}")
            self._lbl_status.setText("● 已停止")
            self._lbl_status.setStyleSheet(
                f"color: {theme.hex('muted')}; padding: 2px 4px;")

    def _apply_virtual_btn_style(self):
        """虚拟电机模式按钮样式 (随主题, checked 态用 accent)"""
        on = self._virtual_mode
        text = "虚拟电机模式: 开" if on else "虚拟电机模式: 关"
        self._btn_virtual.setText(text)
        if on:
            self._btn_virtual.setStyleSheet(f"""
                QPushButton {{
                    border: 1px solid {theme.hex('accent')};
                    border-radius: 4px; padding: 4px;
                    background: {theme.hex('accent')};
                    color: {theme.hex('card_bottom')};
                    font-weight: bold;
                }}
            """)
        else:
            self._btn_virtual.setStyleSheet(f"""
                QPushButton {{
                    border: 1px solid {theme.hex('btn_border')};
                    border-radius: 4px; padding: 4px;
                    background: {theme.hex('btn_bg')};
                    color: {theme.hex('btn_text')};
                }}
                QPushButton:hover {{
                    background: {theme.hex('btn_hover')};
                }}
                QPushButton:disabled {{
                    color: {theme.hex('muted')};
                    background: {theme.hex('card_bottom')};
                }}
            """)

    def _on_toggle(self):
        if self._btn_toggle.isChecked():
            self.start_requested.emit()
        else:
            self.stop_requested.emit()

    def _on_virtual_toggled(self, on: bool):
        self._virtual_mode = on
        self._apply_virtual_btn_style()
        self.virtual_mode_toggled.emit(on)

    # ---- 公共 API ----
    def set_running(self, running: bool):
        """外部同步引擎运行状态"""
        self._running = running
        self._btn_toggle.blockSignals(True)
        self._btn_toggle.setChecked(running)
        self._btn_toggle.blockSignals(False)
        self._apply_toggle_style()
        # 虚拟电机模式开关仅在运行时可用
        self._btn_virtual.setEnabled(running)
        if not running and self._virtual_mode:
            # 引擎停止时强制关闭虚拟电机模式
            self._btn_virtual.blockSignals(True)
            self._btn_virtual.setChecked(False)
            self._btn_virtual.blockSignals(False)
            self._virtual_mode = False
            self._apply_virtual_btn_style()

    def set_virtual_mode(self, on: bool):
        """程序化设置虚拟电机模式 (不触发信号)"""
        self._btn_virtual.blockSignals(True)
        self._btn_virtual.setChecked(on)
        self._btn_virtual.blockSignals(False)
        self._virtual_mode = on
        self._apply_virtual_btn_style()

    def is_virtual_mode(self) -> bool:
        return self._virtual_mode

    def apply_theme(self):
        self._apply_toggle_style()
        self._apply_virtual_btn_style()


# ============================================================================
# 遥测推送控制组 (对齐主上位机 TelemetryPanel)
# ============================================================================

class TelemetryControlGroup(QGroupBox):
    """遥测推送控制: 数据种类勾选 + 推送周期 + 使能/停止。

    twin 引擎默认每个仿真周期(20ms)推送全部遥测; 此组允许用户筛选字段和调整周期。
    发出 telemetry_config_changed(enabled, mask, period_ms)。
    """

    telemetry_config_changed = pyqtSignal(bool, int, int)

    # 遥测字段分组 (对齐 JmTlmBit, 但 twin 不走协议层, 仅做 UI 筛选)
    _FIELD_GROUPS = [
        ("位置/速度", ["pos", "vel"]),
        ("DQ电流", ["id", "iq"]),
        ("三相电流", ["ia", "ib", "ic"]),
        ("母线", ["vbus", "ibus", "power"]),
        ("温度", ["temp_fet", "temp_motor"]),
        ("多圈", ["multiturn", "single"]),
        ("力矩", ["torque"]),
        ("故障/警告", ["fault_flags", "warn_mask"]),
        ("状态机", ["sys_state", "run_state", "ctrl_mode", "enable", "motion_state"]),
        ("调试通道", ["debug"]),
    ]

    def __init__(self, parent=None):
        super().__init__("遥测推送 / 周期上报", parent)
        self._checks = []   # (group_label, QCheckBox)
        self._running = True  # twin 引擎默认推送
        self._build()

    def _build(self):
        g = QGridLayout(self)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(2)

        # 字段勾选 (2 列)
        for i, (label, _fields) in enumerate(self._FIELD_GROUPS):
            chk = QCheckBox(label)
            chk.setChecked(True)  # 默认全选
            self._checks.append((label, chk))
            g.addWidget(chk, i // 2, i % 2)

        next_row = (len(self._FIELD_GROUPS) + 1) // 2

        # 推送周期
        g.addWidget(QLabel("推送周期(ms):"), next_row, 0)
        self._spin_period = QSpinBox()
        self._spin_period.setRange(1, 1000)
        self._spin_period.setValue(20)
        self._spin_period.valueChanged.connect(self._on_config_changed)
        g.addWidget(self._spin_period, next_row, 1)

        # 使能/停止按钮 (双态)
        self._btn_toggle = QPushButton("停止推送")
        self._btn_toggle.setCheckable(True)
        self._btn_toggle.setChecked(True)
        self._btn_toggle.clicked.connect(self._on_toggle)
        g.addWidget(self._btn_toggle, next_row + 1, 0, 1, 2)
        self._apply_button_style()

    def _apply_button_style(self):
        for _label, chk in self._checks:
            chk.setEnabled(not self._running)
        self._spin_period.setEnabled(self._running)
        if self._running:
            self._btn_toggle.setText("停止推送")
            self._btn_toggle.setStyleSheet(
                f"background: {theme.hex('danger')}; color: white; padding: 6px;"
                f" border: none; border-radius: 3px; font-weight: bold;")
        else:
            self._btn_toggle.setText("使能推送")
            self._btn_toggle.setStyleSheet(
                f"background: {theme.hex('ok')}; color: white; padding: 6px;"
                f" border: none; border-radius: 3px; font-weight: bold;")

    def _on_toggle(self):
        self._running = self._btn_toggle.isChecked()
        self._apply_button_style()
        self._on_config_changed()

    def _on_config_changed(self):
        self.telemetry_config_changed.emit(
            self._running, self.current_mask(), self._spin_period.value())

    def current_mask(self) -> int:
        """当前勾选的分组掩码 (位 0..N)"""
        mask = 0
        for i, (_label, chk) in enumerate(self._checks):
            if chk.isChecked():
                mask |= (1 << i)
        return mask

    def get_period(self) -> int:
        return self._spin_period.value()

    def apply_theme(self):
        self._apply_button_style()


# ============================================================================
# 设备信息组 (对齐主上位机, 但 twin 无真实设备, 仅显示引擎元信息)
# ============================================================================

class DevInfoGroup(QGroupBox):
    """设备信息: 读引擎信息 / 读设备名称 / 心跳。

    twin 无真实设备, 按钮改为读取引擎元信息 (参数快照/场景信息)。
    """

    read_info_requested = pyqtSignal()
    read_name_requested = pyqtSignal()
    heartbeat_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("设备信息", parent)
        self._build()

    def _build(self):
        g = QGridLayout(self)
        btn_info = QPushButton("读引擎信息")
        btn_info.clicked.connect(self.read_info_requested)
        btn_name = QPushButton("读设备名称")
        btn_name.clicked.connect(self.read_name_requested)
        btn_hb = QPushButton("心跳")
        btn_hb.clicked.connect(self.heartbeat_requested)
        for btn in (btn_info, btn_name, btn_hb):
            self._apply_btn_style(btn)
        g.addWidget(btn_info, 0, 0)
        g.addWidget(btn_name, 0, 1)
        g.addWidget(btn_hb, 0, 2)

    @staticmethod
    def _apply_btn_style(btn):
        btn.setStyleSheet(f"""
            QPushButton {{
                border: 1px solid {theme.hex('btn_border')};
                border-radius: 4px; padding: 4px 8px;
                background: {theme.hex('btn_bg')};
                color: {theme.hex('btn_text')};
            }}
            QPushButton:hover {{
                background: {theme.hex('btn_hover')};
                border-color: {theme.hex('muted')};
            }}
        """)

    def apply_theme(self):
        for i in range(self.layout().count()):
            w = self.layout().itemAt(i).widget()
            if w is not None:
                self._apply_btn_style(w)


# ============================================================================
# 菜单配置组 (对齐主上位机, 迁移顶部工具栏功能)
# ============================================================================

class MenuConfigGroup(QGroupBox):
    """菜单配置: 主题切换 + 参数导入导出 + 场景预设 + 故障注入。

    把原顶部 QToolBar 的功能迁移到左侧, 与主上位机"菜单配置"组对齐。
    """

    toggle_theme_requested = pyqtSignal()
    export_params_requested = pyqtSignal()
    import_params_requested = pyqtSignal()
    scenario_requested = pyqtSignal(str)   # "high_inertia" / "low_inertia" / "heavy_load"
    inject_fault_requested = pyqtSignal()
    clear_injected_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("菜单配置", parent)
        self._build()

    def _build(self):
        g = QGridLayout(self)
        g.setHorizontalSpacing(6)
        g.setVerticalSpacing(4)

        # 行 0: 主题切换 + 缓存设置(占位)
        self._btn_theme = QPushButton("主题: 深色" if theme.is_dark else "主题: 浅色")
        self._btn_theme.clicked.connect(self.toggle_theme_requested)
        self._apply_menu_btn_style(self._btn_theme)
        g.addWidget(self._btn_theme, 0, 0)

        self._btn_export = QPushButton("导出参数")
        self._btn_export.clicked.connect(self.export_params_requested)
        self._apply_menu_btn_style(self._btn_export)
        g.addWidget(self._btn_export, 0, 1)

        # 行 1: 导入参数 + 故障注入
        self._btn_import = QPushButton("导入参数")
        self._btn_import.clicked.connect(self.import_params_requested)
        self._apply_menu_btn_style(self._btn_import)
        g.addWidget(self._btn_import, 1, 0)

        self._btn_inject = QPushButton("注入故障…")
        self._btn_inject.clicked.connect(self.inject_fault_requested)
        self._apply_menu_btn_style(self._btn_inject)
        g.addWidget(self._btn_inject, 1, 1)

        # 行 2: 清除注入 (跨 2 列)
        self._btn_clear_inj = QPushButton("清除注入")
        self._btn_clear_inj.clicked.connect(self.clear_injected_requested)
        self._apply_menu_btn_style(self._btn_clear_inj)
        g.addWidget(self._btn_clear_inj, 2, 0, 1, 2)

        # 行 3-4: 场景预设
        lbl_scn = QLabel("场景预设:")
        lbl_scn.setStyleSheet(f"color: {theme.hex('muted')};")
        g.addWidget(lbl_scn, 3, 0, 1, 2)

        self._btn_scn_high = QPushButton("高惯量")
        self._btn_scn_high.clicked.connect(lambda: self.scenario_requested.emit("high_inertia"))
        self._apply_menu_btn_style(self._btn_scn_high)
        g.addWidget(self._btn_scn_high, 4, 0)

        self._btn_scn_low = QPushButton("低惯量")
        self._btn_scn_low.clicked.connect(lambda: self.scenario_requested.emit("low_inertia"))
        self._apply_menu_btn_style(self._btn_scn_low)
        g.addWidget(self._btn_scn_low, 4, 1)

        self._btn_scn_heavy = QPushButton("重载")
        self._btn_scn_heavy.clicked.connect(lambda: self.scenario_requested.emit("heavy_load"))
        self._apply_menu_btn_style(self._btn_scn_heavy)
        g.addWidget(self._btn_scn_heavy, 5, 0, 1, 2)

    @staticmethod
    def _apply_menu_btn_style(btn):
        btn.setStyleSheet(f"""
            QPushButton {{
                border: 1px solid {theme.hex('btn_border')};
                border-radius: 4px; padding: 4px 8px;
                background: {theme.hex('btn_bg')};
                color: {theme.hex('btn_text')};
            }}
            QPushButton:hover {{
                background: {theme.hex('btn_hover')};
                border-color: {theme.hex('muted')};
            }}
        """)

    def apply_theme(self):
        self._btn_theme.setText("主题: 深色" if theme.is_dark else "主题: 浅色")
        for i in range(self.layout().count()):
            w = self.layout().itemAt(i).widget()
            if w is not None and isinstance(w, QPushButton):
                self._apply_menu_btn_style(w)


# ============================================================================
# 左侧控制面板总成
# ============================================================================

class TwinControlPanel(QWidget):
    """左侧控制面板总成: 引擎连接 + 系统控制 + 运动控制 + 遥测推送 + 设备信息 + 运行状态 + 菜单配置。

    布局对齐主上位机 (ui/main_window.py): 多个独立 QGroupBox 纵向堆叠。
    顶部工具栏功能已迁移到菜单配置组。

    信号:
        system_command(str): 系统命令名 (enable/disable/stop/idle/estop/clear/reset)
        motion_command(object): MotorCmd
        start_requested(): 启动仿真
        stop_requested(): 停止仿真
        virtual_mode_toggled(bool): 虚拟电机模式开关
        telemetry_config_changed(bool, int, int): 遥测推送配置 (enabled, mask, period_ms)
        toggle_theme_requested(): 切换主题
        export_params_requested(): 导出参数
        import_params_requested(): 导入参数
        scenario_requested(str): 场景预设
        inject_fault_requested(): 注入故障
        clear_injected_requested(): 清除注入
    """

    system_command = pyqtSignal(str)
    motion_command = pyqtSignal(object)
    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    virtual_mode_toggled = pyqtSignal(bool)
    telemetry_config_changed = pyqtSignal(bool, int, int)
    toggle_theme_requested = pyqtSignal()
    export_params_requested = pyqtSignal()
    import_params_requested = pyqtSignal()
    scenario_requested = pyqtSignal(str)
    inject_fault_requested = pyqtSignal()
    clear_injected_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(2, 2, 2, 2)
        v.setSpacing(6)

        # 1. 引擎连接 (替代主上位机"串口连接")
        self.engine_conn = EngineConnectionGroup()
        self.engine_conn.start_requested.connect(self.start_requested)
        self.engine_conn.stop_requested.connect(self.stop_requested)
        self.engine_conn.virtual_mode_toggled.connect(self.virtual_mode_toggled)
        v.addWidget(self.engine_conn)

        # 2. 系统控制
        self.sys_ctrl = SystemControlGroup()
        self.sys_ctrl.command.connect(self.system_command)
        v.addWidget(self.sys_ctrl)

        # 3. 运动控制
        self.motion_ctrl = MotionControlGroup()
        self.motion_ctrl.send_motion.connect(self.motion_command)
        v.addWidget(self.motion_ctrl)

        # 4. 遥测推送控制
        self.telemetry_ctrl = TelemetryControlGroup()
        self.telemetry_ctrl.telemetry_config_changed.connect(self.telemetry_config_changed)
        v.addWidget(self.telemetry_ctrl)

        # 5. 设备信息
        self.dev_info = DevInfoGroup()
        v.addWidget(self.dev_info)

        # 6. 运行状态
        self.state_disp = StateDisplayGroup()
        v.addWidget(self.state_disp)

        # 7. 菜单配置 (迁移自顶部工具栏)
        self.menu_config = MenuConfigGroup()
        self.menu_config.toggle_theme_requested.connect(self.toggle_theme_requested)
        self.menu_config.export_params_requested.connect(self.export_params_requested)
        self.menu_config.import_params_requested.connect(self.import_params_requested)
        self.menu_config.scenario_requested.connect(self.scenario_requested)
        self.menu_config.inject_fault_requested.connect(self.inject_fault_requested)
        self.menu_config.clear_injected_requested.connect(self.clear_injected_requested)
        v.addWidget(self.menu_config)

        v.addStretch()

    def update_telemetry(self, t: dict):
        self.state_disp.update_telemetry(t)

    def set_engine(self, engine):
        """注入引擎句柄, 转发给运动控制组 (切换模式时同步 target)。"""
        self.motion_ctrl.set_engine(engine)

    def set_running(self, running: bool):
        """同步引擎运行状态到引擎连接组"""
        self.engine_conn.set_running(running)

    def set_virtual_mode(self, on: bool):
        """程序化设置虚拟电机模式"""
        self.engine_conn.set_virtual_mode(on)

    def is_virtual_mode(self) -> bool:
        return self.engine_conn.is_virtual_mode()

    def apply_theme(self):
        self.state_disp.apply_theme()
        self.engine_conn.apply_theme()
        self.telemetry_ctrl.apply_theme()
        self.dev_info.apply_theme()
        self.menu_config.apply_theme()
