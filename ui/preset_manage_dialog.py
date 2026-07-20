"""电机预设管理对话框：查看/编辑/删除/导入导出用户预设。

显示内置预设（只读）+ 用户预设（可编辑），支持:
  - 新建预设（调 PresetEditDialog）
  - 编辑用户预设
  - 删除用户预设
  - 复制内置预设为用户预设（作为编辑起点）
  - 导出用户预设为 JSON 文件
  - 导入外部 JSON 为用户预设
  - 选中预设实时显示参数详情（10 字段 + 描述）
"""

import json
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QMessageBox, QFileDialog, QLabel, QGridLayout, QFrame,
    QSizePolicy
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

from ui import preset_store
from ui.preset_edit_dialog import PresetEditDialog
from ui.theme import theme


# 字段显示定义（与 PresetEditDialog.FIELDS 同步，但此处用于只读展示）
_PRESET_DETAIL_FIELDS = [
    ("pole_pairs",           "极对数",           ""),
    ("phase_resistance",     "相电阻",           "Ω"),
    ("phase_inductance_d",   "d轴电感",          "H"),
    ("phase_inductance_q",   "q轴电感",          "H"),
    ("flux_linkage",         "磁链",             "Wb"),
    ("torque_constant",      "转矩常数",         "Nm/A"),
    ("rotor_inertia",        "转子惯量",         "kg·m²"),
    ("peak_current",         "峰值电流",         "A"),
    ("max_speed",            "最大转速",         "rad/s"),
    ("protect_over_current", "过流保护阈值",     "A"),
]


def _format_value(field: str, value) -> str:
    """按字段类型格式化数值显示"""
    if field == "pole_pairs":
        return f"{int(value)}"
    # 浮点数智能精度: 大值保留2位, 小值保留更多位
    v = float(value)
    if abs(v) >= 1.0:
        return f"{v:.4g}"
    elif abs(v) >= 0.001:
        return f"{v:.6g}"
    else:
        return f"{v:.8g}"


class PresetManageDialog(QDialog):
    """预设管理对话框

    用法:
        dlg = PresetManageDialog(parent, builtin_presets={"presets": {...}})
        dlg.exec()
        # 用户预设已实时写入 user_motor_presets.json
    """

    def __init__(self, parent=None, builtin_presets=None):
        super().__init__(parent)
        self.setWindowTitle("电机预设管理")
        self.setMinimumSize(720, 500)

        # 内置预设 dict（来自 motor_presets.json 的 presets 段）
        self._builtin = builtin_presets or {}

        layout = QVBoxLayout(self)

        # 顶部说明
        hint = QLabel(
            "<b>内置预设</b>（[内置]前缀）只读，不可编辑。<br>"
            "<b>用户预设</b>（[用户]前缀）可新建/编辑/删除/导入导出。<br>"
            "选中任意预设可在右侧查看参数详情。")
        hint.setStyleSheet("color: gray; padding: 4px;")
        layout.addWidget(hint)

        # 中间: 列表 + 详情面板 + 右侧操作按钮
        middle = QHBoxLayout()

        # 左侧: 预设列表
        self._list = QListWidget()
        self._list.setMinimumWidth(200)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        middle.addWidget(self._list, 1)

        # 中间: 详情面板（紧凑布局，宽度刚好容纳字段值即可）
        self._detail_frame = QFrame()
        self._detail_frame.setFrameShape(QFrame.Shape.StyledPanel)
        self._detail_frame.setMinimumWidth(240)
        self._detail_frame.setMaximumWidth(320)
        self._detail_frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        detail_layout = QVBoxLayout(self._detail_frame)
        detail_layout.setContentsMargins(8, 8, 8, 8)

        # 详情标题
        self._detail_title = QLabel("（请选择左侧预设查看详情）")
        self._detail_title.setStyleSheet("font-weight: bold; font-size: 13px; padding: 2px;")
        self._detail_title.setWordWrap(True)
        detail_layout.addWidget(self._detail_title)

        # 详情 label（电机描述）
        self._detail_label = QLabel("")
        self._detail_label.setStyleSheet("color: gray; font-style: italic; padding: 2px 2px 6px 2px;")
        self._detail_label.setWordWrap(True)
        detail_layout.addWidget(self._detail_label)

        # 字段网格: 2列 x N行（字段名 | 值+单位）
        self._detail_grid = QGridLayout()
        self._detail_grid.setContentsMargins(4, 4, 4, 4)
        self._detail_grid.setHorizontalSpacing(12)
        self._detail_grid.setVerticalSpacing(4)
        self._field_labels = {}  # field -> (QLabel name, QLabel value)
        for row, (field, name, unit) in enumerate(_PRESET_DETAIL_FIELDS):
            name_lbl = QLabel(f"{name}:")
            name_lbl.setStyleSheet("color: gray;")
            val_lbl = QLabel("—")
            val_lbl.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
            self._detail_grid.addWidget(name_lbl, row, 0)
            self._detail_grid.addWidget(val_lbl, row, 1)
            self._field_labels[field] = (name_lbl, val_lbl)
            # 让 value 列拉伸吸收剩余宽度
            self._detail_grid.setColumnStretch(1, 1)
        detail_layout.addLayout(self._detail_grid)
        detail_layout.addStretch()
        middle.addWidget(self._detail_frame, 0)  # 不拉伸，按 sizeHint 显示

        # 右侧按钮列
        btn_col = QVBoxLayout()
        self._btn_new = QPushButton("新建...")
        self._btn_new.clicked.connect(self._on_new)
        btn_col.addWidget(self._btn_new)

        self._btn_edit = QPushButton("编辑...")
        self._btn_edit.clicked.connect(self._on_edit)
        self._btn_edit.setEnabled(False)
        btn_col.addWidget(self._btn_edit)

        self._btn_copy = QPushButton("复制为用户预设...")
        self._btn_copy.clicked.connect(self._on_copy)
        self._btn_copy.setEnabled(False)
        btn_col.addWidget(self._btn_copy)

        self._btn_delete = QPushButton("删除")
        self._btn_delete.clicked.connect(self._on_delete)
        self._btn_delete.setEnabled(False)
        btn_col.addWidget(self._btn_delete)

        btn_col.addStretch()

        self._btn_export = QPushButton("导出全部用户预设...")
        self._btn_export.clicked.connect(self._on_export_all)
        btn_col.addWidget(self._btn_export)

        self._btn_import = QPushButton("从文件导入...")
        self._btn_import.clicked.connect(self._on_import)
        btn_col.addWidget(self._btn_import)

        middle.addLayout(btn_col)
        layout.addLayout(middle, 1)

        # 底部关闭按钮
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

        # 订阅主题变更信号: 重新着色列表项 + 详情面板
        theme.changed.connect(self._on_theme_changed)

        self._refresh_list()

    def _builtin_color(self) -> QColor:
        """内置预设文字色: 主题感知（深色用青蓝, 浅色用深蓝）"""
        return QColor(theme.hex("value"))

    def _user_color(self) -> QColor:
        """用户预设文字色: 主题感知（深色用青绿, 浅色用墨绿）"""
        return QColor(theme.hex("accent"))

    def _on_theme_changed(self, _name: str):
        """主题切换: 重新着色所有列表项"""
        builtin_color = self._builtin_color()
        user_color = self._user_color()
        for i in range(self._list.count()):
            item = self._list.item(i)
            kind, _name = item.data(Qt.ItemDataRole.UserRole)
            item.setForeground(builtin_color if kind == "builtin" else user_color)

    def _refresh_list(self):
        """刷新预设列表显示"""
        self._list.clear()
        builtin_color = self._builtin_color()
        user_color = self._user_color()
        # 内置预设
        for name in sorted(self._builtin.keys()):
            item = QListWidgetItem(f"[内置] {name}")
            item.setData(Qt.ItemDataRole.UserRole, ("builtin", name))
            item.setForeground(builtin_color)
            self._list.addItem(item)
        # 用户预设
        user_presets = preset_store.list_presets()
        for name in sorted(user_presets.keys()):
            item = QListWidgetItem(f"[用户] {name}")
            item.setData(Qt.ItemDataRole.UserRole, ("user", name))
            item.setForeground(user_color)
            self._list.addItem(item)
        # 清空详情面板
        self._clear_detail()

    def _clear_detail(self):
        """清空详情面板"""
        self._detail_title.setText("（请选择左侧预设查看详情）")
        self._detail_label.setText("")
        for _, val_lbl in self._field_labels.values():
            val_lbl.setText("—")

    def _show_preset_detail(self, kind: str, name: str):
        """显示选中预设的详情

        Args:
            kind: "builtin" 或 "user"
            name: 预设名称
        """
        if kind == "builtin":
            preset = self._builtin.get(name, {})
            prefix = "[内置] "
            title_color = theme.hex("value")
        else:
            preset = preset_store.get(name) or {}
            prefix = "[用户] "
            title_color = theme.hex("accent")

        self._detail_title.setText(f"{prefix}{name}")
        self._detail_title.setStyleSheet(
            f"font-weight: bold; font-size: 13px; padding: 2px; color: {title_color};")

        # label 字段（电机描述）
        label_text = preset.get("label", "") if preset else ""
        if label_text:
            self._detail_label.setText(label_text)
            self._detail_label.setVisible(True)
        else:
            self._detail_label.setText("（无描述）")
            self._detail_label.setVisible(True)

        # 10 个数值字段
        for field, _, _ in _PRESET_DETAIL_FIELDS:
            name_lbl, val_lbl = self._field_labels[field]
            if field in preset:
                val_lbl.setText(_format_value(field, preset[field]))
            else:
                val_lbl.setText("—")

    def _on_selection_changed(self):
        """选中项变化: 控制按钮启用状态 + 显示详情"""
        items = self._list.selectedItems()
        if not items:
            self._btn_edit.setEnabled(False)
            self._btn_copy.setEnabled(False)
            self._btn_delete.setEnabled(False)
            self._clear_detail()
            return
        kind, name = items[0].data(Qt.ItemDataRole.UserRole)
        self._btn_copy.setEnabled(True)  # 内置+用户都可复制
        self._btn_edit.setEnabled(kind == "user")
        self._btn_delete.setEnabled(kind == "user")
        self._show_preset_detail(kind, name)

    def _on_new(self):
        """新建用户预设"""
        dlg = PresetEditDialog(self, existing_preset=None)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            name, preset = dlg.get_preset()
            if not name:
                return
            if preset_store.get(name) is not None:
                QMessageBox.warning(self, "冲突", f"用户预设 '{name}' 已存在")
                return
            if not preset_store.add(name, preset):
                QMessageBox.critical(self, "失败", "写入 user_motor_presets.json 失败")
                return
            self._refresh_list()

    def _on_edit(self):
        """编辑用户预设"""
        items = self._list.selectedItems()
        if not items:
            return
        kind, name = items[0].data(Qt.ItemDataRole.UserRole)
        if kind != "user":
            return
        existing = preset_store.get(name)
        if existing is None:
            return
        dlg = PresetEditDialog(self, existing_preset=existing, existing_name=name)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_name, new_preset = dlg.get_preset()
            if new_name != name:
                # 重命名: 先删后增
                preset_store.delete(name)
                preset_store.add(new_name, new_preset)
            else:
                preset_store.update(name, new_preset)
            self._refresh_list()

    def _on_copy(self):
        """复制选中的预设（内置或用户）为新的用户预设"""
        items = self._list.selectedItems()
        if not items:
            return
        kind, name = items[0].data(Qt.ItemDataRole.UserRole)
        if kind == "builtin":
            src_preset = self._builtin.get(name, {})
        else:
            src_preset = preset_store.get(name) or {}
        if not src_preset:
            return
        # 默认新名称
        new_name = f"{name}_副本"
        idx = 1
        while preset_store.get(new_name) is not None:
            idx += 1
            new_name = f"{name}_副本{idx}"
        dlg = PresetEditDialog(self, existing_preset=src_preset, existing_name=new_name)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            final_name, final_preset = dlg.get_preset()
            if preset_store.add(final_name, final_preset):
                self._refresh_list()
            else:
                QMessageBox.warning(self, "失败", f"新增 '{final_name}' 失败（名称冲突？）")

    def _on_delete(self):
        """删除用户预设"""
        items = self._list.selectedItems()
        if not items:
            return
        kind, name = items[0].data(Qt.ItemDataRole.UserRole)
        if kind != "user":
            return
        reply = QMessageBox.question(
            self, "确认删除",
            f"确认删除用户预设 '{name}'？\n此操作仅影响上位机预设，不会影响下位机 Flash。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        if preset_store.delete(name):
            self._refresh_list()
        else:
            QMessageBox.warning(self, "失败", f"删除 '{name}' 失败")

    def _on_export_all(self):
        """导出所有用户预设为 JSON 文件"""
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出用户预设", "user_motor_presets_export.json", "JSON 文件 (*.json)")
        if not file_path:
            return
        data = preset_store.load()
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            QMessageBox.information(self, "导出成功",
                f"已导出 {len(data.get('presets', {}))} 个用户预设到\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _on_import(self):
        """从 JSON 文件导入用户预设"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "导入用户预设", "", "JSON 文件 (*.json)")
        if not file_path:
            return
        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "导入失败", f"读取文件失败: {e}")
            return
        external_presets = data.get("presets", {})
        if not external_presets:
            QMessageBox.warning(self, "导入失败", "文件中无 presets 字段")
            return
        # 逐个校验 + 导入，名称冲突自动加 _导入 后缀
        imported = 0
        skipped = 0
        for name, preset in external_presets.items():
            errors = preset_store.validate(preset)
            if errors:
                skipped += 1
                continue
            final_name = name
            idx = 1
            while preset_store.get(final_name) is not None:
                final_name = f"{name}_导入{idx}"
                idx += 1
            if preset_store.add(final_name, preset):
                imported += 1
            else:
                skipped += 1
        self._refresh_list()
        QMessageBox.information(self, "导入完成",
            f"成功导入 {imported} 个预设\n跳过 {skipped} 个（校验失败或写入失败）")
