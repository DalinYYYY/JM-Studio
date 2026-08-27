"""参数读写面板(数据驱动)

按 registry 的参数分组以表格展示全部参数。
每行提供中文名、param_id、类型/单位、当前值、读/写按钮。
读写走 PARAM_READ/WRITE; 值按参数类型编解码。
"""

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QBrush, QFont
from collections import deque
import json
import os

from jmproto.codec import is_float_type
from ui.theme import theme


COL_NAME = 0
COL_CN_NAME = 1
COL_ID = 2
COL_TYPE = 3
COL_CURRENT = 4
COL_EDIT = 5
COL_DESC = 6
COL_READ = 7
COL_WRITE = 8


class _ColorSwatch(QFrame):
    """图例小色块。"""

    def __init__(self, color: QColor, parent=None):
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self._color = color
        self._update()

    def _update(self):
        c = self._color
        self.setStyleSheet(
            f"background: rgba({c.red()},{c.green()},{c.blue()},180);"
            f"border: 1px solid #555; border-radius: 3px;")

    def setColor(self, color: QColor):
        self._color = color
        self._update()


class ParamPanel(QGroupBox):
    """参数表格面板。发出 read_param/write_param/read_params/write_params/save_all 信号。"""

    read_param = pyqtSignal(int)
    write_param = pyqtSignal(int, str)
    read_params = pyqtSignal(object)
    write_params = pyqtSignal(object)
    save_all = pyqtSignal(int)  # flags: bit0=1 追加写Flash备份

    def __init__(self, registry, parent=None, title="电机参数",
                 source="motor_param", show_save=False, save_text="保存到Flash",
                 groups=None, show_legend=True, show_bulk_rw=False,
                 show_import_export=False):
        super().__init__("", parent)
        self._panel_name = title
        self._reg = registry
        self._source = source
        self._show_save = bool(show_save)
        self._save_text = save_text
        # 导入/导出按钮+文件名标签: 仅 motor_config 面板启用
        self._show_import_export = bool(show_import_export)
        # 是否显示图例(固有/可配置 色块说明); 内嵌场景(如标定结果)可关闭
        self._show_legend = bool(show_legend)
        # 需求3: 是否在底部显示全读/全写按钮(标定结果面板用)
        self._show_bulk_rw = bool(show_bulk_rw)
        # 仅展示指定分组(按 motor_info.csv / param_index.csv 的 group 名);
        # None 表示不过滤(向后兼容)。
        self._groups = tuple(groups) if groups else None
        self._current_items = {} # param_id -> QTableWidgetItem
        self._edit_items = {}    # param_id -> QTableWidgetItem
        self._write_buttons = {} # param_id -> QPushButton
        self._param_specs = {}    # param_id -> ParamSpec
        self._group_param_ids = {} # group -> [param_id]
        self._row_state = {}     # param_id -> {'synced': str, 'dirty': bool, 'pending': bool}
        self._pending_writes = deque()  # (param_id, sent_text)
        self._syncing_table = False
        self._row_of_pid = {}                # param_id -> 行号(用于主题刷新只读行)
        self._btn_layout = None              # 保存按钮所在 QHBoxLayout (供 add_footer_widget 注入)
        self._btn_row = None
        self._footer_after_save = None       # 需求3: 保存按钮后的注入槽 (放历史开关)
        self._fresh_ids = set()              # 需求2: 标定完成新读到的参数集合(加粗彩色显示)
        # 需求4: 按钮样式改用主题键(随主题切换), 不再硬编码 #2A2A2A/#EF6C00 等
        self._build()

    def panel_name(self) -> str:
        return self._panel_name

    def source(self) -> str:
        """参数来源: 'motor_param'(运行时参数 0xE0-0xE5) 或 'motor_config'(电机配置 0xE6-0xEB)"""
        return self._source

    def add_footer_widget(self, widget):
        """在保存按钮之后、stretch 之前注入一个 widget (紧跟保存按钮).

        用于内嵌场景(标定结果面板)把外部开关(如历史显隐)放到保存按钮同行后面。
        """
        if self._footer_after_save is not None:
            self._footer_after_save.addWidget(widget)

    def add_footer_widget_right(self, widget):
        """在按钮行最右(stretch 之后)注入一个 widget.

        用于把外部按钮放到参数表下方按钮行的最右侧。
        """
        if self._btn_layout is not None:
            self._btn_layout.addWidget(widget)

    def param_ids(self):
        """返回当前面板展示的所有 param_id (按表格顺序). 用于外部触发批量读取。"""
        return list(self._param_specs.keys())

    def read_all(self):
        """触发批量读取当前面板全部参数 (发 read_params 信号)."""
        ids = self.param_ids()
        if ids:
            self.read_params.emit(list(ids))

    def current_values_by_name(self, names=None) -> dict:
        """返回已读回的当前值，键为 CSV code_name，未读取/非法值跳过。"""
        wanted = set(names) if names is not None else None
        values = {}
        for pid, spec in self._param_specs.items():
            if wanted is not None and spec.code_name not in wanted:
                continue
            item = self._current_items.get(pid)
            text = item.text().strip() if item is not None else "--"
            if not text or text == "--":
                continue
            try:
                values[spec.code_name] = float(text)
            except (TypeError, ValueError):
                continue
        return values

    def param_ids_by_name(self, names) -> list[int]:
        wanted = set(names)
        return [int(pid) for pid, spec in self._param_specs.items()
                if spec.code_name in wanted]

    def set_edit_values(self, values_by_name: dict) -> int:
        """把计算/导入结果填入“修改值”列，不改变当前值。"""
        if not isinstance(values_by_name, dict):
            return 0
        by_name = {spec.code_name: int(pid)
                   for pid, spec in self._param_specs.items()}
        applied = 0
        self._syncing_table = True
        try:
            for name, value in values_by_name.items():
                pid = by_name.get(name)
                item = self._edit_items.get(pid) if pid is not None else None
                state = self._row_state.get(pid) if pid is not None else None
                if item is None or state is None or not state.get('writable'):
                    continue
                try:
                    text = f"{float(value):.7g}"
                except (TypeError, ValueError):
                    continue
                item.setText(text)
                current = self._current_items.get(pid)
                current_missing = current is None or current.text().strip() == "--"
                dirty = current_missing or self._compute_dirty(pid)
                self._set_write_button_state(pid, dirty, False)
                applied += 1
        finally:
            self._syncing_table = False
        return applied

    def reload(self):
        """从 registry 重新构建表格(用于运行时 CSV 重新加载后刷新)。

        清空待写队列与 fresh 标记(它们可能指向已失效的 param_id),
        然后调用 _populate() 按 registry 当前内容重建表格。
        _populate → _append_param_row 会重新构建 tooltip, 故 reload 后 tooltip 自动更新。
        """
        self._pending_writes.clear()
        self._fresh_ids.clear()
        self._populate()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)

        # 图例: 固有(只读)/可配置(可改) 颜色区分说明 (可由 show_legend 关闭)
        if self._show_legend:
            legend = QHBoxLayout()
            legend.setContentsMargins(4, 0, 4, 0)
            legend.setSpacing(12)
            sw_ro = _ColorSwatch(theme.c("card_bottom"))
            sw_rw = _ColorSwatch(theme.c("table_bg"))
            legend.addWidget(QLabel("■ 固有参数"))
            legend.addWidget(sw_ro)
            legend.addWidget(QLabel("只读"))
            legend.addSpacing(8)
            legend.addWidget(QLabel("■ 可配置参数"))
            legend.addWidget(sw_rw)
            legend.addWidget(QLabel("可改"))
            legend.addStretch()
            legend_w = QWidget()
            legend_w.setLayout(legend)
            layout.addWidget(legend_w)

        self._table = QTableWidget(0, 9)
        self._table.setHorizontalHeaderLabels(["参数", "中文名", "ID", "类型/单位", "当前值", "修改值", "描述", "读", "写"])
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # 需求1: 标定结果表格与电机参数/配置表格用相同的颜色逻辑
        # (交替行 + _style_row 逐行涂 table_bg/card_bottom), 不再特殊化
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self._table.setShowGrid(True)
        self._table.setWordWrap(False)
        self._table.setSortingEnabled(False)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(24)
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self._table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setMinimumSectionSize(48)
        hdr.setDefaultSectionSize(120)
        hdr.setSectionResizeMode(COL_DESC, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_READ, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(COL_WRITE, QHeaderView.ResizeMode.Fixed)

        layout.addWidget(self._table, 1)

        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(6)

        # "同时写Flash备份"复选框: 默认仅写EEPROM, 勾选后追加写Flash备份(0xEA flags bit0)。
        # 置于保存按钮之前(用户先勾选备份目标, 再点击固化)。
        self._chk_flash_backup = QCheckBox("同时写Flash备份")
        self._chk_flash_backup.setToolTip(
            "默认仅写EEPROM(上电优先加载)。勾选后追加写Flash备份, 频繁勾选会降低Flash寿命。")
        # 先 addWidget 父级化, 再 setVisible; 否则 widget 无 parent 时被设为可见
        # 会作为独立小窗口在 Windows 上短暂弹出
        btn_layout.addWidget(self._chk_flash_backup)
        self._chk_flash_backup.setVisible(self._show_save)

        self._btn_save = QPushButton(self._save_text)
        self._btn_save.clicked.connect(self._on_save_clicked)
        btn_layout.addWidget(self._btn_save)
        self._btn_save.setVisible(self._show_save)
        # 全读/全写按钮: 先创建, 位置由下方模式决定(保存后 或 最右)
        self._btn_read_all = self._make_button(
            "参数全读",
            lambda _=False: self.read_params.emit(list(self._param_specs.keys())),
            width=88)
        self._btn_write_all = self._make_button(
            "参数全写", self._on_write_all_clicked, width=88)
        # show_import_export 模式(电机配置面板): 全读/全写放保存按钮后、导入导出前
        if self._show_import_export:
            btn_layout.addWidget(self._btn_read_all)
            btn_layout.addWidget(self._btn_write_all)
            self._btn_read_all.setVisible(True)
            self._btn_write_all.setVisible(True)
        # 导入/导出按钮 + 文件名标签 (紧跟保存按钮, 在 footer 注入位之前)
        self._btn_export = QPushButton("参数导出")
        self._btn_export.setFixedHeight(24)
        self._btn_export.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_export.clicked.connect(self._on_export_clicked)
        self._btn_import = QPushButton("参数导入")
        self._btn_import.setFixedHeight(24)
        self._btn_import.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_import.clicked.connect(self._on_import_clicked)
        self._lbl_import_file = QLabel("")
        self._lbl_import_file.setStyleSheet(
            f"color: {theme.hex('muted')}; font-style: italic;"
        )
        btn_layout.addWidget(self._btn_export)
        btn_layout.addWidget(self._btn_import)
        btn_layout.addWidget(self._lbl_import_file)
        has_ie = self._show_import_export
        self._btn_export.setVisible(has_ie)
        self._btn_import.setVisible(has_ie)
        self._lbl_import_file.setVisible(has_ie)
        # 需求3: 保存按钮后的注入位(放历史开关等), 在 stretch 之前
        self._footer_after_save = QHBoxLayout()
        self._footer_after_save.setContentsMargins(0, 0, 0, 0)
        btn_layout.addLayout(self._footer_after_save)
        btn_layout.addStretch()
        # show_bulk_rw 模式(标定/PID 面板): 全读/全写放最右(stretch 之后)
        # 与 show_import_export 模式(电机配置面板, 按钮在保存后)互斥
        if self._show_bulk_rw:
            btn_layout.addWidget(self._btn_read_all)
            btn_layout.addWidget(self._btn_write_all)
            self._btn_read_all.setVisible(True)
            self._btn_write_all.setVisible(True)
        self._btn_layout = btn_layout   # 保留引用, 供 add_footer_widget 注入
        self._btn_row = btn_row
        layout.addWidget(btn_row)

        self._table.itemChanged.connect(self._on_item_changed)
        self._populate()
        self._table.setColumnWidth(COL_NAME, 200)
        self._table.setColumnWidth(COL_CN_NAME, 140)
        self._table.setColumnWidth(COL_ID, 60)
        self._table.setColumnWidth(COL_TYPE, 120)
        self._table.setColumnWidth(COL_CURRENT, 120)
        self._table.setColumnWidth(COL_EDIT, 120)
        self._table.setColumnWidth(COL_READ, 72)
        self._table.setColumnWidth(COL_WRITE, 72)
        self._update_table_layout()

    def _make_group_item(self, text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setForeground(QBrush(theme.c("title")))
        font = QFont()
        font.setBold(True)
        item.setFont(font)
        item.setBackground(QBrush(theme.c("table_header")))
        item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        return item

    def _make_cell_item(self, text: str, editable: bool = False) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if editable:
            flags |= Qt.ItemFlag.ItemIsEditable
        item.setFlags(flags)
        item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        return item

    def _make_button(self, text: str, callback, width: int = 56) -> QPushButton:
        btn = QPushButton(text)
        btn.setFixedHeight(24)
        btn.setFixedWidth(width)
        btn.setMinimumWidth(width)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(callback)
        btn.setStyleSheet(self._btn_base_style())
        return btn

    def _make_button_cell(self, button: QPushButton) -> QWidget:
        cell = QWidget()
        layout = QHBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignCenter)
        return cell

    # ---------- 主题 ----------
    def _btn_base_style(self) -> str:
        """需求4: 读/写按钮基础样式(随主题切换, 不再硬编码)."""
        return f"""
            QPushButton {{
                padding: 0 8px;
                border: 1px solid {theme.hex('btn_border')};
                border-radius: 4px;
                background: {theme.hex('btn_bg')};
                color: {theme.hex('btn_text')};
                font-size: 12px;
            }}
            QPushButton:hover {{
                background: {theme.hex('btn_hover')};
                border-color: {theme.hex('muted')};
            }}
            QPushButton:disabled {{
                color: {theme.hex('muted')};
                border-color: {theme.hex('border')};
                background: {theme.hex('card_bottom')};
            }}
        """

    def _btn_dirty_style(self) -> str:
        """需求4: 已修改待写入按钮高亮样式(随主题切换)."""
        return f"""
            QPushButton {{
                padding: 0 8px;
                border: 1px solid {theme.hex('value_hot')};
                border-radius: 4px;
                background: {theme.hex('warn')};
                color: {theme.hex('card_bottom')};
                font-weight: bold;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background: {theme.hex('value_hot')};
                border-color: {theme.hex('accent')};
            }}
            QPushButton:disabled {{
                color: {theme.hex('muted')};
                border-color: {theme.hex('warn')};
                background: {theme.hex('card_bottom')};
            }}
        """

    def apply_theme(self):
        """主题切换: 刷新图例色块/只读行样式 + 全部读/写按钮样式."""
        # 重建图例色块颜色(色块在 _build 时按 [固有, 可配置] 顺序创建)
        ro = theme.c("card_bottom")
        rw = theme.c("table_bg")
        swatches = self.findChildren(_ColorSwatch)
        if len(swatches) >= 2:
            swatches[0].setColor(ro)
            swatches[1].setColor(rw)
        # 只读行重涂
        for pid, spec in self._param_specs.items():
            row = self._row_of_pid.get(pid)
            if row is None:
                continue
            self._style_row(row, spec.writable)
        # 需求4: 刷新全部读/写按钮样式(按 dirty 状态分别套 base/dirty 样式)
        base_css = self._btn_base_style()
        dirty_css = self._btn_dirty_style()
        for pid, btn in self._write_buttons.items():
            state = self._row_state.get(pid)
            is_dirty = bool(state and state.get('dirty'))
            btn.setStyleSheet(dirty_css if is_dirty else base_css)
        # 需求5: 底部全读/全写按钮也套 base 样式(若存在)
        for attr in ("_btn_read_all", "_btn_write_all"):
            btn = getattr(self, attr, None)
            if btn is not None:
                btn.setStyleSheet(base_css)

    def _populate(self):
        self._syncing_table = True
        self._table.setRowCount(0)
        self._current_items.clear()
        self._edit_items.clear()
        self._write_buttons.clear()
        self._param_specs.clear()
        self._group_param_ids.clear()
        self._row_state.clear()
        self._pending_writes.clear()
        self._row_of_pid.clear()

        groups = self._params_by_group()
        for group, params in groups.items():
            # 需求3: show_bulk_rw=True 时(标定结果面板)不创建分组显示行,
            # 全读/全写按钮改放底部; _group_param_ids 仍记录分组映射以兼容外部查询。
            if not self._show_bulk_rw:
                self._append_group_row(group, params)
            else:
                self._group_param_ids[group] = [int(p.param_id) for p in params]
            for p in params:
                self._append_param_row(p)

        self._table.resizeRowsToContents()
        self._syncing_table = False

    def _update_table_layout(self):
        self._table.horizontalHeader().setStretchLastSection(False)

    def _append_group_row(self, group: str, params):
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setRowHeight(row, 24)
        param_ids = [int(p.param_id) for p in params]
        self._group_param_ids[group] = param_ids

        item = self._make_group_item(group)
        self._table.setItem(row, COL_NAME, item)
        self._table.setSpan(row, COL_NAME, 1, COL_READ)

        read_btn = self._make_button(
            "全读", lambda _=False, ids=tuple(param_ids): self.read_params.emit(list(ids)), width=64)
        write_btn = self._make_button(
            "全写", lambda _=False, ids=tuple(param_ids): self._on_write_group_clicked(ids), width=64)
        writable = any(getattr(p, 'writable', False) for p in params)
        write_btn.setEnabled(writable)
        self._table.setCellWidget(row, COL_READ, self._make_button_cell(read_btn))
        self._table.setCellWidget(row, COL_WRITE, self._make_button_cell(write_btn))

    def _build_param_tooltip(self, p) -> str:
        """构建参数详细信息的悬停提示文本(鼠标悬停描述/名称列时淡入显示)。

        汇总 ParamSpec 全部可读字段: 中文名/单位/类型/范围[min,max]/读写/说明(含默认值与Remarks)。
        范围行仅当存在 vmin/vmax 时显示; 说明行含 motor_info.csv 的默认值与 Remarks(加载时已拼入 desc)。
        """
        lines = []
        if p.cn_name:
            lines.append(f"中文名: {p.cn_name}")
        if p.unit and p.unit != '-':
            lines.append(f"单位: {p.unit}")
        if p.dtype:
            lines.append(f"类型: {p.dtype}")
        # 范围: 仅当存在 vmin/vmax 时显示, 无约束则不显示该行
        if p.vmin is not None or p.vmax is not None:
            lo = p.vmin if p.vmin is not None else "-∞"
            hi = p.vmax if p.vmax is not None else "+∞"
            lines.append(f"范围: [{lo}, {hi}]")
        if p.rw:
            lines.append(f"读写: {p.rw}")
        if p.desc:
            lines.append(f"说明: {p.desc}")
        return "\n".join(lines)

    def _append_param_row(self, p):
        row = self._table.rowCount()
        self._table.insertRow(row)

        unit = f" ({p.unit})" if p.unit and p.unit != '-' else ""
        desc = p.desc or p.cn_name
        name_item = self._make_cell_item(p.code_name)
        cn_item = self._make_cell_item(p.cn_name or "--")
        id_item = self._make_cell_item(str(p.param_id))
        type_item = self._make_cell_item(f"{p.dtype}{unit}")
        current_item = self._make_cell_item("--")
        edit_item = self._make_cell_item("--", editable=p.writable)
        desc_item = self._make_cell_item(desc)

        # 鼠标悬停描述列/名称列时淡入显示参数详细信息(含 min/max/单位/Remarks)
        tooltip = self._build_param_tooltip(p)
        if tooltip:
            desc_item.setToolTip(tooltip)
            name_item.setToolTip(tooltip)
            cn_item.setToolTip(tooltip)

        self._table.setItem(row, COL_NAME, name_item)
        self._table.setItem(row, COL_CN_NAME, cn_item)
        self._table.setItem(row, COL_ID, id_item)
        self._table.setItem(row, COL_TYPE, type_item)
        self._table.setItem(row, COL_CURRENT, current_item)
        self._table.setItem(row, COL_EDIT, edit_item)
        self._table.setItem(row, COL_DESC, desc_item)

        read_btn = self._make_button("读", lambda _=False, pid=p.param_id: self.read_param.emit(int(pid)), width=52)
        write_btn = self._make_button("写", lambda _=False, pid=p.param_id: self._on_write_clicked(int(pid)), width=52)
        write_btn.setEnabled(p.writable)
        self._table.setCellWidget(row, COL_READ, self._make_button_cell(read_btn))
        self._table.setCellWidget(row, COL_WRITE, self._make_button_cell(write_btn))

        self._current_items[p.param_id] = current_item
        self._edit_items[p.param_id] = edit_item
        self._write_buttons[p.param_id] = write_btn
        self._param_specs[p.param_id] = p
        self._row_state[p.param_id] = {
            'synced': current_item.text().strip(),
            'dirty': False,
            'pending': False,
            'writable': bool(p.writable),
        }
        self._row_of_pid[int(p.param_id)] = row

        # 视觉区分: 固有参数(只读)用暗色背景, 可配置参数用默认底色
        self._style_row(row, p.writable)

        for col in range(self._table.columnCount()):
            item = self._table.item(row, col)
            if item is not None:
                item.setData(Qt.ItemDataRole.UserRole, p.param_id)

    def _style_row(self, row: int, writable: bool):
        """按可写性涂行底色: 只读=card_bottom(暗), 可改=table_bg(亮)。
        需求1: 标定结果面板与电机参数/配置表格用相同颜色逻辑, 不再特殊化。
        """
        bg = theme.c("table_bg") if writable else theme.c("card_bottom")
        fg = theme.c("text") if writable else theme.c("muted")
        for col in range(self._table.columnCount()):
            item = self._table.item(row, col)
            if item is not None:
                item.setBackground(QBrush(bg))
                # 名称/ID/类型/描述列保持原色, 仅数值列随可写性着色以增强对比
                if col in (COL_CURRENT, COL_EDIT):
                    item.setForeground(QBrush(fg))

    def _on_write_clicked(self, param_id: int):
        item = self._edit_items.get(int(param_id))
        if item is None:
            return
        text = item.text().strip()
        if text and text != "--":
            self.write_param.emit(int(param_id), text)

    def _values_equal(self, param_id: int, a: str, b: str) -> bool:
        """归一化比较两个参数值文本是否相等, 用于判定写按钮高亮。

        - 任一为 "--"(未设置/未读取) 视为相等, 不触发 dirty
        - float 类型用 6 位有效数字归一化比较, 与显示格式一致 (避免 0.10 vs 0.1 误判)
        - int 类型按整数值比较 (避免 1000 vs 1000.0 误判)
        - char[N] 等字符串类型按字符串比较
        """
        if a == "--" or b == "--":
            return True
        spec = self._param_specs.get(int(param_id))
        if spec is None:
            return a == b
        dtype = (spec.dtype or '').strip().lower()
        if dtype.startswith('char['):
            return a == b
        try:
            fa, fb = float(a), float(b)
            if is_float_type(dtype):
                return f'{fa:.6g}' == f'{fb:.6g}'
            return int(fa) == int(fb)
        except (ValueError, TypeError):
            return a == b

    def _compute_dirty(self, param_id: int) -> bool:
        """统一计算写按钮是否应高亮: 修改值列 vs 当前已同步值(读回值)。"""
        pid = int(param_id)
        state = self._row_state.get(pid)
        edit_item = self._edit_items.get(pid)
        if state is None or edit_item is None or not state.get('writable'):
            return False
        edit_text = edit_item.text().strip()
        if edit_text == "--":
            return False
        # 当前值列也是 "--"(未读取过): 无比较基准, 不高亮
        current_item = self._current_items.get(pid)
        if current_item is not None and current_item.text().strip() == "--":
            return False
        return not self._values_equal(pid, edit_text, state['synced'])

    def _on_write_group_clicked(self, param_ids):
        writes = []
        for pid in param_ids:
            state = self._row_state.get(int(pid))
            item = self._edit_items.get(int(pid))
            if not state or not state.get('writable') or item is None:
                continue
            text = item.text().strip()
            # 仅收集 dirty 行(修改值 != 实际读回值); "--"和未读取的行不发送
            if text and text != "--" and state.get('dirty'):
                writes.append((int(pid), text))
        if writes:
            self.write_params.emit(writes)

    def _on_write_all_clicked(self):
        """需求3: 底部全写按钮 — 收集当前面板所有 dirty 行批量写。"""
        self._on_write_group_clicked(list(self._param_specs.keys()))

    def _on_save_clicked(self):
        """保存按钮: 按复选框状态拼装 flags 发出 save_all。
        flags bit0=1 追加写Flash备份(0xEA), 默认仅写EEPROM。"""
        flags = 0
        if self._chk_flash_backup.isChecked():
            flags |= 1 << 0  # MOTOR_INFO_SAVE_FLAG_FLASH
        self.save_all.emit(flags)

    def _set_write_button_state(self, param_id: int, dirty: bool, pending: bool = False):
        btn = self._write_buttons.get(int(param_id))
        state = self._row_state.get(int(param_id))
        if btn is None or state is None:
            return
        state['dirty'] = bool(dirty)
        state['pending'] = bool(pending)
        if dirty:
            btn.setStyleSheet(self._btn_dirty_style())
            btn.setToolTip("当前值已修改，等待写入")
        else:
            btn.setStyleSheet(self._btn_base_style())
            btn.setToolTip("写入已发送，等待返回" if pending else "")

    def note_write_sent(self, param_id: int):
        """主窗口在写指令真正发送后调用。"""
        pid = int(param_id)
        item = self._edit_items.get(pid)
        sent_text = item.text().strip() if item is not None else ""
        self._pending_writes.append((pid, sent_text))
        dirty = self._compute_dirty(pid)
        self._set_write_button_state(pid, dirty, True)

    def confirm_pending_write(self):
        """主窗口收到 PARAM_WRITE ACK 后调用。"""
        if not self._pending_writes:
            return None
        pid, sent_text = self._pending_writes.popleft()
        state = self._row_state.get(int(pid))
        current_item = self._current_items.get(int(pid))
        if state is not None and current_item is not None:
            state['synced'] = sent_text
            self._syncing_table = True
            try:
                current_item.setText(sent_text)
            finally:
                self._syncing_table = False
            dirty = self._compute_dirty(int(pid))
            self._set_write_button_state(int(pid), dirty, False)
        return pid

    def reject_pending_write(self):
        """主窗口收到 PARAM_WRITE NACK 后调用。"""
        if not self._pending_writes:
            return None
        pid, _sent_text = self._pending_writes.popleft()
        dirty = self._compute_dirty(int(pid))
        self._set_write_button_state(int(pid), dirty, False)
        return pid

    def reset_write_state(self):
        """断开连接时调用: 清空全部待确认写状态, 恢复按钮显示。"""
        while self._pending_writes:
            pid, _ = self._pending_writes.popleft()
            dirty = self._compute_dirty(int(pid))
            self._set_write_button_state(int(pid), dirty, False)

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._syncing_table:
            return
        if item is None or item.column() != COL_EDIT:
            return
        pid = item.data(Qt.ItemDataRole.UserRole)
        if pid is None:
            return
        state = self._row_state.get(int(pid))
        if state is None or not state['writable']:
            return
        dirty = self._compute_dirty(int(pid))
        self._set_write_button_state(int(pid), dirty, state.get('pending', False) and dirty)

    def set_value(self, param_id: int, text: str):
        """收到读应答后更新当前值列。

        修改值列保持 "--" 不被覆盖, 只有用户改过且与读回值不一致时才高亮写按钮。
        需求2: 若该参数在 fresh 集合中(标定完成新读到), 当前值列加粗彩色显示。
        """
        pid = int(param_id)
        current_item = self._current_items.get(pid)
        edit_item = self._edit_items.get(pid)
        if current_item is not None:
            self._syncing_table = True
            try:
                current_item.setText(text)
                # 需求2: fresh 参数加粗彩色 (适配主题: value_hot 暖色高亮)
                if pid in self._fresh_ids:
                    font = current_item.font()
                    font.setBold(True)
                    current_item.setFont(font)
                    current_item.setForeground(QBrush(theme.c("value_hot")))
            finally:
                self._syncing_table = False
        state = self._row_state.get(pid)
        if state is not None:
            state['synced'] = text
            # 修改值列若仍为 "--"(用户未改过): 不跟随同步为读回值, 保持 "--"
            # 仅当用户已输入具体值时, 才用归一化比较判定 dirty
            dirty = self._compute_dirty(pid)
            self._set_write_button_state(pid, dirty, False)

    def mark_fresh(self, param_ids=None):
        """需求2: 标记参数为「新读到」(标定完成主动请求的结果)。

        后续 set_value 会把这些参数的当前值列加粗彩色显示;
        clear_fresh (保存到 Flash/EEPROM ACK 后调用) 恢复正常样式。
        param_ids=None 表示标记当前面板全部参数。
        """
        if param_ids is None:
            self._fresh_ids = set(int(p) for p in self._param_specs.keys())
        else:
            self._fresh_ids.update(int(p) for p in param_ids)

    def clear_fresh(self):
        """需求2: 保存到 Flash/EEPROM 后恢复正常样式(去加粗、去彩色)."""
        for pid in self._fresh_ids:
            current_item = self._current_items.get(pid)
            if current_item is not None:
                font = current_item.font()
                font.setBold(False)
                current_item.setFont(font)
                # 恢复为该行可写性对应的默认前景色
                spec = self._param_specs.get(pid)
                writable = getattr(spec, 'writable', True) if spec else True
                fg = theme.c("text") if writable else theme.c("muted")
                current_item.setForeground(QBrush(fg))
        self._fresh_ids.clear()

    # ==================== 配置持久化 ====================
    def get_opts(self) -> dict:
        """收集可持久化的 UI 配置: 仅表格列宽。

        修改值列不持久化, 上电统一为 "--", 只有用户主动改且与实际读回值不一致时才高亮写按钮。
        """
        opts = {"column_widths": {}}
        try:
            for col in range(self._table.columnCount()):
                opts["column_widths"][str(int(col))] = int(self._table.columnWidth(col))
        except Exception:
            pass
        return opts

    def set_opts(self, opts: dict):
        """启动时套用配置 (容错)。仅恢复列宽; 修改值列保持 "--"。"""
        if not isinstance(opts, dict):
            return
        cw = opts.get("column_widths")
        if isinstance(cw, dict):
            for k, w in cw.items():
                try:
                    col = int(k)
                    if 0 <= col < self._table.columnCount():
                        self._table.setColumnWidth(col, int(w))
                except Exception:
                    pass

    # ==================== 导入/导出参数 ====================
    def _on_export_clicked(self):
        """导出当前面板参数到 JSON 文件。值取"当前值"列(读回值)。"""
        params_data = []
        for pid, spec in self._param_specs.items():
            cur_item = self._current_items.get(int(pid))
            value = cur_item.text().strip() if cur_item is not None else "--"
            params_data.append({
                "param_id": int(pid),
                "code_name": spec.code_name,
                "cn_name": spec.cn_name,
                "value": value,
                "unit": spec.unit,
                "dtype": spec.dtype,
            })
        payload = {
            "panel": self._panel_name,
            "source": self._source,
            "param_count": len(params_data),
            "params": params_data,
        }
        default_name = f"{self._panel_name}_export.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出参数", default_name, "JSON 文件 (*.json)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"写入文件失败:\n{e}")
            return
        self._lbl_import_file.setText(f"已导出: {os.path.basename(path)}")
        QMessageBox.information(
            self, "导出成功", f"已导出 {len(params_data)} 个参数到:\n{path}"
        )

    def _on_import_clicked(self):
        """从 JSON 文件导入参数, 填入"修改值"列并触发 dirty 高亮。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "导入参数", "", "JSON 文件 (*.json)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "导入失败", f"读取文件失败:\n{e}")
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("params"), list):
            QMessageBox.warning(self, "导入失败", "文件格式无效: 缺少 params 列表")
            return
        applied = 0
        skipped = 0
        self._syncing_table = True
        try:
            for item in payload["params"]:
                if not isinstance(item, dict):
                    skipped += 1
                    continue
                pid = item.get("param_id")
                if pid is None:
                    skipped += 1
                    continue
                pid = int(pid)
                edit_item = self._edit_items.get(pid)
                state = self._row_state.get(pid)
                if edit_item is None or state is None or not state.get('writable'):
                    skipped += 1
                    continue
                value = item.get("value", "")
                if not isinstance(value, str):
                    value = str(value)
                value = value.strip()
                if not value or value == "--":
                    skipped += 1
                    continue
                edit_item.setText(value)
                dirty = self._compute_dirty(pid)
                self._set_write_button_state(pid, dirty, False)
                applied += 1
        finally:
            self._syncing_table = False
        self._lbl_import_file.setText(f"已导入: {os.path.basename(path)}")
        tip = f'已导入 {applied} 个参数到修改值列'
        if skipped:
            tip += f'（跳过 {skipped} 项）'
        tip += '\n请检查后点击「写」或「全写」下发。'
        QMessageBox.information(self, "导入成功", tip)

    def _params_by_group(self):
        if self._source == "motor_config":
            all_groups = self._reg.motor_config_by_group()
        else:
            all_groups = self._reg.params_by_group()
        if self._groups:
            return {g: params for g, params in all_groups.items()
                    if g in self._groups}
        return all_groups

    def get_param(self, param_id: int):
        return self._param_specs.get(int(param_id))

    def pack_value(self, param_id: int, text: str) -> bytes:
        if self._source == "motor_config":
            return self._reg.pack_motor_config_value(param_id, text)
        return self._reg.pack_param_value(param_id, text)

    def check_value_range(self, param_id: int, text: str):
        """返回越界原因(str)或 None; 无范围约束或非数值类型返回 None。"""
        spec = self._param_specs.get(int(param_id))
        if spec is None:
            return None
        return self._reg.check_spec_range(spec, text)

    def unpack_value(self, param_id: int, value_bytes: bytes):
        if self._source == "motor_config":
            return self._reg.unpack_motor_config_value(param_id, value_bytes)
        return self._reg.unpack_param_value(param_id, value_bytes)
