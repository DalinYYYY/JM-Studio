"""实时反馈面板: 按分组展示数字孪生遥测字典。

直接消费 engine.get_telemetry() 返回的 dict, 不依赖协议帧/FeedbackData。
命令目标/限位/保护参数从 engine.fsm 与 engine.mp 读取(每帧刷新), 弥补 telemetry 缺失字段。
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame, QGridLayout, QGroupBox, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from ui.theme import theme


# (key, 中文, 单位, 格式) — key 优先从 telemetry 取, 缺失时从 _extra 取
_ROWS = [
    # 位置/速度 (实测)
    ("pos", "电机端位置", "rad", "{:.4f}"),
    ("vel", "电机端速度", "rad/s", "{:.4f}"),
    ("pos_out", "输出端位置", "rad", "{:.4f}"),
    ("vel_out", "输出端速度", "rad/s", "{:.4f}"),
    ("theta_m", "电机端角度 θm", "rad", "{:.4f}"),
    ("omega_m", "电机端角速度 ωm", "rad/s", "{:.4f}"),
    ("multiturn", "多圈计数", "", "{:.0f}"),
    ("single", "单圈角度", "rad", "{:.4f}"),
    # 命令目标 (生效后, ramp 输出)
    ("target_pos", "目标位置", "rad", "{:.4f}"),
    ("target_vel", "目标速度", "rad/s", "{:.4f}"),
    ("target_torque", "目标力矩", "Nm", "{:.4f}"),
    ("target_iq", "目标 Iq", "A", "{:.4f}"),
    ("target_id", "目标 Id", "A", "{:.4f}"),
    ("target_voltage", "目标电压", "V", "{:.4f}"),
    ("target_duty", "目标占空比", "", "{:.4f}"),
    ("cmd_target_pos", "命令位置", "rad", "{:.4f}"),
    ("cmd_target_vel", "命令速度", "rad/s", "{:.4f}"),
    ("cmd_target_torque", "命令力矩", "Nm", "{:.4f}"),
    # 力矩/电流 (实测)
    ("torque", "电机端力矩", "Nm", "{:.4f}"),
    ("torque_out", "输出端力矩", "Nm", "{:.4f}"),
    ("id", "Id 实测", "A", "{:.4f}"),
    ("iq", "Iq 实测", "A", "{:.4f}"),
    ("ia", "Ia", "A", "{:.4f}"),
    ("ib", "Ib", "A", "{:.4f}"),
    ("ic", "Ic", "A", "{:.4f}"),
    ("id_ref", "Id 参考", "A", "{:.4f}"),
    ("iq_ref", "Iq 参考", "A", "{:.4f}"),
    # 电压/功率
    ("vbus", "母线电压", "V", "{:.3f}"),
    ("ibus", "母线电流", "A", "{:.4f}"),
    ("power", "功率", "W", "{:.3f}"),
    ("ud", "Ud", "V", "{:.4f}"),
    ("uq", "Uq", "V", "{:.4f}"),
    # 温度
    ("temp_fet", "FET 温度", "°C", "{:.2f}"),
    ("temp_motor", "电机温度", "°C", "{:.2f}"),
    # 负载
    ("t_load", "负载力矩", "Nm", "{:.4f}"),
    ("t_gravity", "重力矩", "Nm", "{:.4f}"),
    ("t_external", "外力矩", "Nm", "{:.4f}"),
    ("t_collision", "碰撞力矩", "Nm", "{:.4f}"),
    ("J_load", "负载惯量", "kg·m²", "{:.6f}"),
    # 控制/跟随
    ("vel_setpoint", "速度设定", "rad/s", "{:.4f}"),
    ("follow_err", "跟随误差", "rad", "{:.4f}"),
    # 限位/保护参数 (输出端坐标)
    ("pos_limit_max", "位置上限(输出端)", "rad", "{:.3f}"),
    ("pos_limit_min", "位置下限(输出端)", "rad", "{:.3f}"),
    ("max_speed", "最大速度(电机端)", "rad/s", "{:.3f}"),
    ("follow_error_threshold", "跟随误差阈值", "rad", "{:.3f}"),
    ("gear_ratio", "减速比", "", "{:.1f}"),
]

_GROUPS = [
    ("位置/速度 (实测)", 0, 8),
    ("命令目标 (生效/命令)", 8, 10),
    ("力矩/电流 (实测)", 18, 9),
    ("电压/功率", 27, 5),
    ("温度", 32, 2),
    ("负载", 34, 5),
    ("控制/跟随", 39, 2),
    ("限位/保护参数", 41, 5),
]


class TwinFeedbackPanel(QWidget):
    """数字孪生实时反馈面板。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._labels = {}
        self._engine = None   # 由主窗口 set_engine 注入, 用于读取 fsm/mp 字段
        self._build()

    def set_engine(self, engine):
        """注入引擎句柄, 用于补齐 telemetry 缺失的 target_*/限位/保护字段。"""
        self._engine = engine

    def _build(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(6)

        for name, start, cnt in _GROUPS:
            grp = QGroupBox(name)
            g = QGridLayout(grp)
            g.setVerticalSpacing(4)
            g.setHorizontalSpacing(12)
            cols = 2   # 每行 2 个字段(标签+值), 共 4 列
            for i, idx in enumerate(range(start, start + cnt)):
                key, cn, unit, fmt = _ROWS[idx]
                lbl_name = QLabel(cn)
                lbl_name.setStyleSheet(f"color: {theme.hex('muted')};")
                lbl_val = QLabel("-")
                lbl_val.setStyleSheet(
                    f"font-family: Consolas, monospace; color: {theme.hex('value')};"
                    f" font-weight: bold;")
                lbl_val.setMinimumWidth(110)
                lbl_val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                r, c = divmod(i, cols)
                g.addWidget(lbl_name, r, c * 2)
                g.addWidget(lbl_val, r, c * 2 + 1)
                self._labels[key] = (lbl_val, fmt, unit)
            v.addWidget(grp)
        v.addStretch()

        scroll.setWidget(host)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def _collect_extra(self) -> dict:
        """从 engine.fsm / engine.mp 补齐 telemetry 缺失字段。"""
        extra = {}
        eng = self._engine
        if eng is None:
            return extra
        fsm = eng.fsm
        mp = eng.mp
        # 命令目标 / 生效目标 (fsm 字段)
        for attr in ("target_pos", "target_vel", "target_torque", "target_iq",
                     "target_id", "target_voltage", "target_duty",
                     "cmd_target_pos", "cmd_target_vel", "cmd_target_torque",
                     "cmd_target_iq", "cmd_target_id", "cmd_target_voltage",
                     "cmd_target_duty"):
            if hasattr(fsm, attr):
                extra[attr] = getattr(fsm, attr)
        # 限位 / 减速比 (fsm)
        for attr in ("pos_limit_max", "pos_limit_min", "gear_ratio"):
            if hasattr(fsm, attr):
                extra[attr] = getattr(fsm, attr)
        # 保护参数 (mp 子结构)
        if hasattr(mp, "motor_base"):
            extra["max_speed"] = mp.motor_base.max_speed
        if hasattr(mp, "position_loop"):
            extra["follow_error_threshold"] = mp.position_loop.follow_error_threshold
        return extra

    def update_telemetry(self, t: dict):
        # 合并: telemetry 优先, 缺失字段从 engine 补
        merged = t if self._engine is None else {**self._collect_extra(), **t}
        for key, (lbl, fmt, unit) in self._labels.items():
            val = merged.get(key)
            if val is None:
                lbl.setText("-")
            else:
                try:
                    text = fmt.format(float(val))
                    if unit:
                        text += f" {unit}"
                    lbl.setText(text)
                except (ValueError, TypeError):
                    lbl.setText(str(val))

    def apply_theme(self):
        for key, (lbl, fmt, unit) in self._labels.items():
            lbl.setStyleSheet(
                f"font-family: Consolas, monospace; color: {theme.hex('value')};"
                f" font-weight: bold;")
