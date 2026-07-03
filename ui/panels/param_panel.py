"""参数读写面板(数据驱动)

按 registry 的参数分组以表格展示全部参数。
每行提供中文名、param_id、类型/单位、当前值、读/写按钮。
读写走 PARAM_READ/WRITE; 值按参数类型编解码。
"""

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
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

from jmproto.codec import is_float_type
from ui.theme import theme


COL_NAME = 0
COL_ID = 1
COL_TYPE = 2
COL_CURRENT = 3
COL_EDIT = 4
COL_DESC = 5
COL_READ = 6
COL_WRITE = 7


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
    save_all = pyqtSignal()

    def __init__(self, registry, parent=None, title="电机参数",
                 source="motor_param", show_save=False, save_text="保存到Flash",
                 groups=None, show_legend=True):
        super().__init__("", parent)
        self._panel_name = title
        self._reg = registry
        self._source = source
        self._show_save = bool(show_save)
        self._save_text = save_text
        # 是否显示图例(固有/可配置 色块说明); 内嵌场景(如标定结果)可关闭
        self._show_legend = bool(show_legend)
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
        self._btn_base_style = """
            QPushButton {
                padding: 0 8px;
                border: 1px solid #555;
                border-radius: 4px;
                background: #2A2A2A;
                color: #DDD;
                font-size: 12px;
            }
            QPushButton:hover {
                background: #3A3A3A;
                border-color: #777;
            }
            QPushButton:disabled {
                color: #777;
                border-color: #444;
                background: #222;
            }
        """
        self._btn_dirty_style = """
            QPushButton {
                padding: 0 8px;
                border: 1px solid #FFB74D;
                border-radius: 4px;
                background: #EF6C00;
                color: white;
                font-weight: bold;
                font-size: 12px;
            }
            QPushButton:hover {
                background: #FF8F00;
                border-color: #FFCC80;
            }
            QPushButton:disabled {
                color: #DDD;
                border-color: #C57C00;
                background: #B35D00;
            }
        """
        self._build()

    def panel_name(self) -> str:
        return self._panel_name

    def source(self) -> str:
        """参数来源: 'motor_param'(运行时参数 0xE0-0xE5) 或 'motor_config'(电机配置 0xE6-0xEB)"""
        return self._source

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

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(["参数", "ID", "类型/单位", "当前值", "修改值", "描述", "读", "写"])
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
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

        self._btn_save = QPushButton(self._save_text)
        self._btn_save.clicked.connect(self.save_all)
        # 先 addWidget 父级化, 再 setVisible; 否则 widget 无 parent 时被设为可见
        # 会作为独立小窗口在 Windows 上短暂弹出
        btn_layout.addWidget(self._btn_save)
        self._btn_save.setVisible(self._show_save)
        btn_layout.addStretch()
        layout.addWidget(btn_row)

        self._table.itemChanged.connect(self._on_item_changed)
        self._populate()
        self._table.setColumnWidth(COL_NAME, 220)
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
        btn.setStyleSheet(self._btn_base_style)
        return btn

    def _make_button_cell(self, button: QPushButton) -> QWidget:
        cell = QWidget()
        layout = QHBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignCenter)
        return cell

    # ---------- 主题 ----------
    def apply_theme(self):
        """主题切换: 刷新图例色块/只读行样式。"""
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
            self._append_group_row(group, params)
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

    def _append_param_row(self, p):
        row = self._table.rowCount()
        self._table.insertRow(row)

        unit = f" ({p.unit})" if p.unit and p.unit != '-' else ""
        desc = p.desc or p.cn_name
        name_item = self._make_cell_item(p.code_name)
        id_item = self._make_cell_item(str(p.param_id))
        type_item = self._make_cell_item(f"{p.dtype}{unit}")
        current_item = self._make_cell_item("--")
        edit_item = self._make_cell_item("--", editable=p.writable)
        desc_item = self._make_cell_item(desc)

        self._table.setItem(row, COL_NAME, name_item)
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
        """按可写性涂行底色: 只读=card_bottom(暗), 可改=table_bg(亮)。"""
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

    def _set_write_button_state(self, param_id: int, dirty: bool, pending: bool = False):
        btn = self._write_buttons.get(int(param_id))
        state = self._row_state.get(int(param_id))
        if btn is None or state is None:
            return
        state['dirty'] = bool(dirty)
        state['pending'] = bool(pending)
        if dirty:
            btn.setStyleSheet(self._btn_dirty_style)
            btn.setToolTip("当前值已修改，等待写入")
        else:
            btn.setStyleSheet(self._btn_base_style)
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
        """
        pid = int(param_id)
        current_item = self._current_items.get(pid)
        edit_item = self._edit_items.get(pid)
        if current_item is not None:
            self._syncing_table = True
            try:
                current_item.setText(text)
            finally:
                self._syncing_table = False
        state = self._row_state.get(pid)
        if state is not None:
            state['synced'] = text
            # 修改值列若仍为 "--"(用户未改过): 不跟随同步为读回值, 保持 "--"
            # 仅当用户已输入具体值时, 才用归一化比较判定 dirty
            dirty = self._compute_dirty(pid)
            self._set_write_button_state(pid, dirty, False)

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

    def unpack_value(self, param_id: int, value_bytes: bytes):
        if self._source == "motor_config":
            return self._reg.unpack_motor_config_value(param_id, value_bytes)
        return self._reg.unpack_param_value(param_id, value_bytes)
