"""控制面板: 系统控制 + 运动控制 + 状态显示。

直接构造 MotorCmd 投递给 TwinEngineBridge, 不经过协议层。
运动控制按 ControlMode 动态切换目标字段。
"""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
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


class TwinControlPanel(QWidget):
    """左侧控制面板总成: 系统控制 + 运动控制 + 状态显示。

    信号:
        system_command(str): 系统命令名 (enable/disable/stop/idle/estop/clear/reset)
        motion_command(object): MotorCmd
    """

    system_command = pyqtSignal(str)
    motion_command = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(2, 2, 2, 2)
        v.setSpacing(6)

        self.sys_ctrl = SystemControlGroup()
        self.motion_ctrl = MotionControlGroup()
        self.state_disp = StateDisplayGroup()

        self.sys_ctrl.command.connect(self.system_command)
        self.motion_ctrl.send_motion.connect(self.motion_command)

        v.addWidget(self.sys_ctrl)
        v.addWidget(self.motion_ctrl)
        v.addWidget(self.state_disp)
        v.addStretch()

    def update_telemetry(self, t: dict):
        self.state_disp.update_telemetry(t)

    def set_engine(self, engine):
        """注入引擎句柄, 转发给运动控制组 (切换模式时同步 target)。"""
        self.motion_ctrl.set_engine(engine)

    def apply_theme(self):
        self.state_disp.apply_theme()
