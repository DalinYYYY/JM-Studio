"""实时反馈 + 设备状态显示面板"""

from PyQt6.QtWidgets import QGroupBox, QGridLayout, QLabel
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont


class FeedbackPanel(QGroupBox):
    """实时反馈数据 + 状态机显示。update_feedback(fb) / update_state(...) 由主窗口调用。"""

    # (标签, 单位, 属性名, fb字段名, 格式)
    FIELDS = [
        ("位置 pos", "rad", "pos", "{:.4f}"),
        ("速度 vel", "rad/s", "vel", "{:.4f}"),
        ("力矩 torque", "Nm", "torque", "{:.4f}"),
        ("母线电压 vbus", "V", "vbus", "{:.2f}"),
        ("母线电流 ibus", "A", "ibus", "{:.3f}"),
        ("功率 power", "W", "power", "{:.2f}"),
        ("温度 FET", "°C", "temp_fet", "{:.1f}"),
        ("温度电机", "°C", "temp_motor", "{:.1f}"),
        ("d轴电流 id", "A", "id", "{:.3f}"),
        ("q轴电流 iq", "A", "iq", "{:.3f}"),
        ("相电流 ia", "A", "ia", "{:.3f}"),
        ("相电流 ib", "A", "ib", "{:.3f}"),
        ("相电流 ic", "A", "ic", "{:.3f}"),
        ("多圈计数", "", "multiturn", "{}"),
        ("单圈位置", "rad", "single", "{:.4f}"),
    ]

    FSM_NAMES = ["未知", "初始化", "待机", "校准中", "运行中", "故障"]
    RUN_NAMES = ["空闲", "运行", "停止", "错误"]
    CTRL_NAMES = ["无", "开环", "电流", "力矩", "速度", "位置", "MIT", "阻抗"]

    def __init__(self, parent=None):
        super().__init__("", parent)
        self._val_labels = {}
        self._build()

    def _build(self):
        layout = QGridLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setHorizontalSpacing(24)
        layout.setVerticalSpacing(8)

        # 反馈数值网格(4列)
        for i, (name, unit, attr, _fmt) in enumerate(self.FIELDS):
            col = i % 4
            row = (i // 4) * 2
            lbl = QLabel(f"{name} ({unit}):" if unit else f"{name}:")
            lbl.setFont(QFont("", -1, QFont.Weight.Bold))
            val = QLabel("--")
            val.setStyleSheet("font-family: Consolas, monospace; font-size: 13px; color: #1565C0;")
            self._val_labels[attr] = val
            layout.addWidget(lbl, row, col)
            layout.addWidget(val, row + 1, col)

        base_row = (len(self.FIELDS) // 4 + 1) * 2

        # 故障/警告
        self._lbl_fault = self._add_status_field(layout, base_row, 0, "故障掩码")
        self._lbl_warn = self._add_status_field(layout, base_row, 1, "警告掩码")

        # 状态机
        self._lbl_fsm = self._add_status_field(layout, base_row, 2, "顶层状态")
        self._lbl_enable = self._add_status_field(layout, base_row, 3, "使能")
        self._lbl_run = self._add_status_field(layout, base_row + 2, 0, "运行状态")
        self._lbl_ctrl = self._add_status_field(layout, base_row + 2, 1, "控制模式")

    def _add_status_field(self, layout, row, col, name):
        lbl = QLabel(f"{name}:")
        lbl.setFont(QFont("", -1, QFont.Weight.Bold))
        val = QLabel("--")
        val.setStyleSheet("font-family: Consolas, monospace; font-size: 13px; font-weight: bold;")
        layout.addWidget(lbl, row, col)
        layout.addWidget(val, row + 1, col)
        return val

    def update_feedback(self, fb):
        for name, unit, attr, fmt in self.FIELDS:
            lbl = self._val_labels.get(attr)
            if lbl is not None:
                try:
                    lbl.setText(fmt.format(getattr(fb, attr)))
                except Exception:
                    pass
        self._lbl_fault.setText(f"{fb.fault_mask:04X}")
        self._lbl_warn.setText(f"{fb.warn_mask:04X}")
        if fb.fault_mask:
            self._lbl_fault.setStyleSheet(
                "font-family: Consolas; font-size: 13px; color: white; "
                "background-color: #F44336; padding: 1px 4px; border-radius: 2px;")
        else:
            self._lbl_fault.setStyleSheet(
                "font-family: Consolas; font-size: 13px; font-weight: bold; color: #1565C0;")

    def update_state(self, top_fsm, run_state, ctrl_mode, enable):
        self._lbl_fsm.setText(self.FSM_NAMES[top_fsm] if top_fsm < len(self.FSM_NAMES) else f"({top_fsm})")
        self._lbl_run.setText(self.RUN_NAMES[run_state] if run_state < len(self.RUN_NAMES) else f"({run_state})")
        self._lbl_ctrl.setText(self.CTRL_NAMES[ctrl_mode] if ctrl_mode < len(self.CTRL_NAMES) else f"({ctrl_mode})")
        if enable:
            self._lbl_enable.setText("ON")
            self._lbl_enable.setStyleSheet(
                "font-family: Consolas; font-size: 13px; font-weight: bold; color: white; "
                "background-color: #4CAF50; padding: 1px 4px; border-radius: 2px;")
        else:
            self._lbl_enable.setText("OFF")
            self._lbl_enable.setStyleSheet(
                "font-family: Consolas; font-size: 13px; font-weight: bold; color: #666;")
