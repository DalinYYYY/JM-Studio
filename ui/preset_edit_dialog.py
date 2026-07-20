"""电机预设编辑对话框：新建/编辑用户自定义电机预设。

提供 10 个字段的表单输入（名称 + 9 电气 + 保护阈值），
提交时按 motor_info.csv 范围校验，非法值红色提示。
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QDoubleSpinBox,
    QDialogButtonBox, QLabel, QMessageBox
)

from ui import preset_store


class PresetEditDialog(QDialog):
    """预设编辑对话框

    用法:
        dlg = PresetEditDialog(parent, existing_preset=None)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            name, preset = dlg.get_preset()
    """

    # 字段定义: (字段名, 显示名, 单位, 最小值, 最大值, 步长, 小数位数)
    FIELDS = [
        ("pole_pairs",           "极对数",           "",    1,    128,   1,    0),
        ("phase_resistance",     "相电阻",           "Ω",   0.001, 50.0, 0.1,  4),
        ("phase_inductance_d",   "d轴电感",          "H",   1e-6, 0.1,  1e-4, 7),
        ("phase_inductance_q",   "q轴电感",          "H",   1e-6, 0.1,  1e-4, 7),
        ("flux_linkage",         "磁链",             "Wb",  1e-5, 1.0,  1e-4, 6),
        ("torque_constant",      "转矩常数",         "Nm/A",1e-5, 50.0, 0.01, 5),
        ("rotor_inertia",        "转子惯量",         "kg·m²",1e-7, 1.0, 1e-6, 8),
        ("peak_current",         "峰值电流",         "A",   0.1,  200.0, 0.1, 2),
        ("max_speed",            "最大转速",         "rad/s",10.0, 1000.0, 10.0, 1),
        ("protect_over_current", "过流保护阈值",     "A",   0.1,  1000.0, 0.1, 2),
    ]

    def __init__(self, parent=None, existing_preset=None, existing_name=None):
        """
        Args:
            existing_preset: 编辑模式时传入现有预设 dict; None=新建模式
            existing_name: 编辑模式时传入现有名称
        """
        super().__init__(parent)
        self._is_edit = existing_preset is not None
        self.setWindowTitle("编辑电机预设" if self._is_edit else "新建电机预设")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)

        # 预设名称输入
        form = QFormLayout()
        self._name_edit = QLineEdit(existing_name or "")
        self._name_edit.setPlaceholderText("例如: GM6010_100KV")
        form.addRow("预设名称:", self._name_edit)

        # 10 个字段的数值输入框
        self._spin_boxes = {}
        for field, label, unit, lo, hi, step, decimals in self.FIELDS:
            spin = QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setSingleStep(step)
            spin.setDecimals(decimals)
            spin.setMinimumWidth(140)
            if existing_preset and field in existing_preset:
                spin.setValue(float(existing_preset[field]))
            else:
                # 默认值取范围中点附近的合理值
                spin.setValue((lo + hi) * 0.1 if field != "pole_pairs" else 7)
            self._spin_boxes[field] = spin
            form.addRow(f"{label} ({unit}):", spin)

        layout.addLayout(form)

        # 提示标签
        hint = QLabel(
            "<i>提示: 字段范围参考 motor_info.csv 定义。<br>"
            "保存后预设会写入 user_motor_presets.json，与内置预设分离。</i>")
        hint.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(hint)

        # 按钮
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self):
        """确认按钮: 校验名称 + 字段范围"""
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "名称为空", "请输入预设名称")
            return
        if not self._is_edit and preset_store.get(name) is not None:
            QMessageBox.warning(self, "名称冲突", f"预设 '{name}' 已存在")
            return

        # 构造预设 dict
        preset = {}
        for field, *_ in self.FIELDS:
            preset[field] = self._spin_boxes[field].value()

        # 复用 preset_store.validate 做最终校验（理论上 spinbox 范围已保证）
        errors = preset_store.validate(preset)
        if errors:
            QMessageBox.warning(self, "校验失败", "\n".join(errors))
            return

        self._name = name
        self._preset = preset
        self.accept()

    def get_preset(self):
        """返回 (name, preset_dict); 仅在 exec()==Accepted 后有效"""
        return getattr(self, "_name", ""), getattr(self, "_preset", {})
