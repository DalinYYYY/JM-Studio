"""运动控制面板(数据驱动)

模式下拉由 registry 中"运动控制/高级力控/轨迹同步"类命令填充;
选中命令后按其 fields 动态生成输入框; 发送时用 registry.pack_command 打包。
新增命令只要 CSV 加一行即可出现, 无需改本文件。
"""

from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QFormLayout, QHBoxLayout, QWidget,
    QComboBox, QLabel, QPushButton, QDoubleSpinBox, QSpinBox,
)
from PyQt6.QtCore import pyqtSignal

from jmproto import codec


class MotionPanel(QGroupBox):
    """数据驱动的运动指令面板。发出 send_command(cmd:int, values:dict)。"""

    send_command = pyqtSignal(int, dict)

    # 纳入运动面板的命令类别
    CATEGORIES = ("运动控制", "高级力控", "轨迹同步")

    def __init__(self, registry, parent=None):
        super().__init__("运动控制", parent)
        self._reg = registry
        self._field_widgets = {}   # 字段名 -> widget
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)

        # 模式选择
        top = QHBoxLayout()
        top.addWidget(QLabel("控制模式:"))
        self._combo_mode = QComboBox()
        self._specs = self._reg.commands_by_category(*self.CATEGORIES)
        for spec in self._specs:
            self._combo_mode.addItem(f"{spec.name}  (0x{spec.cmd:02X})", spec.cmd)
        self._combo_mode.currentIndexChanged.connect(self._rebuild_fields)
        top.addWidget(self._combo_mode, 1)
        layout.addLayout(top)

        # 动态字段区
        self._form_host = QWidget()
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

    def _clear_form(self):
        while self._form.count():
            item = self._form.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._field_widgets.clear()

    def _make_widget(self, field):
        """按字段类型生成输入控件"""
        if codec.is_float_type(field.dtype):
            w = QDoubleSpinBox()
            w.setRange(-1e6, 1e6)
            w.setDecimals(4)
            w.setValue(0.0)
        else:
            w = QSpinBox()
            # u32 超出 QSpinBox int 上限时夹到 int32 范围, 满足绝大多数输入
            w.setRange(-2147483648, 2147483647)
            w.setValue(0)
        return w

    def _rebuild_fields(self):
        self._clear_form()
        spec = self._current_spec()
        if spec is None:
            return
        editable = spec.editable_fields
        for f in editable:
            w = self._make_widget(f)
            self._field_widgets[f.name] = w
            unit = f"  [{f.dtype}]"
            self._form.addRow(QLabel(f"{f.name}{unit}:"), w)

        # 提示信息: 固定字段 / 无载荷
        notes = []
        if not spec.has_payload:
            notes.append("该命令无需参数, 直接发送。")
        fixed = [f for f in spec.fields if f.is_fixed]
        if fixed:
            notes.append("固定字段: " + ", ".join(f"{f.name}=0x{f.default:X}" for f in fixed))
        if spec.unit and spec.unit != '-':
            notes.append(f"单位/范围: {spec.unit}")
        self._hint.setText("  ".join(notes))
        self._form_host.adjustSize()
        self.adjustSize()
        self.updateGeometry()

    def _on_send(self):
        spec = self._current_spec()
        if spec is None:
            return
        values = {}
        for name, w in self._field_widgets.items():
            values[name] = w.value()
        self.send_command.emit(spec.cmd, values)
