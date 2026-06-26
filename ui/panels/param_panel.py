"""参数读写面板(数据驱动)

按 registry 的参数分组以表格展示全部参数。
每行提供中文名、param_id、类型/单位、当前值、读/写按钮。
读写走 PARAM_READ/WRITE; 值按参数类型编解码。
"""

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
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
                 source="motor_param", show_save=False, save_text="保存到Flash"):
        super().__init__("", parent)
        self._panel_name = title
        self._reg = registry
        self._source = source
        self._show_save = bool(show_save)
        self._save_text = save_text
        self._current_items = {} # param_id -> QTableWidgetItem
        self._edit_items = {}    # param_id -> QTableWidgetItem
        self._write_buttons = {} # param_id -> QPushButton
        self._param_specs = {}    # param_id -> ParamSpec
        self._group_param_ids = {} # group -> [param_id]
        self._row_state = {}     # param_id -> {'synced': str, 'dirty': bool, 'pending': bool}
        self._pending_writes = deque()  # (param_id, sent_text)
        self._syncing_table = False
        # 衍生参数卡: 电流环带宽/KP/KI 实时计算展示(仅 motor_param 源)
        self._code_name_to_pid = {}          # code_name -> param_id
        self._derived_labels = {}            # key -> QLabel
        self._derived_card = None
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

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)

        # 图例: 固有(只读)/可配置(可改) 颜色区分说明
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

        # 衍生参数卡(仅电机参数源): 电流环带宽/KP/KI 实时计算
        if self._source == "motor_param":
            self._derived_card = self._build_derived_card()
            layout.addWidget(self._derived_card)

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
        self._btn_save.setVisible(self._show_save)
        btn_layout.addWidget(self._btn_save)
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

    # ---------- 衍生参数卡: 电流环带宽/KP/KI 实时计算 ----------
    def _build_derived_card(self) -> QFrame:
        """构建电流环衍生参数卡。值由 _update_derived_card() 实时刷新。"""
        card = QFrame()
        card.setObjectName("derivedCard")
        grid = QGridLayout(card)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)

        title = QLabel("电流环衍生参数(实时计算)")
        f = QFont(); f.setBold(True)
        title.setFont(f)
        title.setStyleSheet(f"color: {theme.hex('title')};")
        grid.addWidget(title, 0, 0, 1, 4)

        # 行1: 配置带宽 / KP(q) / KI(q)
        # 行2: 由KP推算带宽 / 由KI推算带宽 / 一致性
        fields = [
            ("bw_cfg",  "配置带宽",         "Hz",       1, 0),
            ("kp_q",    "KP (q轴)",          "V/A",      1, 1),
            ("ki_q",    "KI (q轴)",          "V/(A·s)",  1, 2),
            ("bw_kp",   "由KP推算带宽",     "Hz",       2, 0),
            ("bw_ki",   "由KI推算带宽",     "Hz",       2, 1),
            ("consist", "一致性",            "",          2, 2),
        ]
        for key, label, unit, row, col in fields:
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color: {theme.hex('muted')};")
            val = QLabel("--")
            val.setStyleSheet(
                f"color: {theme.hex('value')}; font-weight: bold;")
            wrap = QWidget()
            wl = QVBoxLayout(wrap)
            wl.setContentsMargins(0, 0, 0, 0)
            wl.setSpacing(0)
            wl.addWidget(lbl)
            wl.addWidget(val)
            unit_lbl = QLabel(unit) if unit else QLabel("")
            unit_lbl.setStyleSheet(f"color: {theme.hex('muted')};")
            grid.addWidget(wrap, row, col * 2)
            grid.addWidget(unit_lbl, row, col * 2 + 1)
            self._derived_labels[key] = val
        grid.setColumnStretch(7, 1)
        self._refresh_derived_card_style(card)
        return card

    def _refresh_derived_card_style(self, card: QFrame):
        card.setStyleSheet(
            f"QFrame#derivedCard {{ background: {theme.hex('card_top')};"
            f" border: 1px solid {theme.hex('border')}; border-radius: 6px; }}")

    def _update_derived_card(self):
        """从当前/编辑值实时计算电流环衍生参数并刷新显示。"""
        if self._derived_card is None:
            return
        # 取值: 可配参数优先用编辑框(反映待写入值), 否则用当前读回值;
        #       固有参数(R/Lq)只能用读回值。
        def val_of(code_name, prefer_edit=False):
            pid = self._code_name_to_pid.get(code_name)
            if pid is None:
                return None
            item = None
            if prefer_edit:
                e = self._edit_items.get(pid)
                if e is not None:
                    t = e.text().strip()
                    if t and t != "--":
                        item = t
            if item is None:
                c = self._current_items.get(pid)
                if c is not None:
                    t = c.text().strip()
                    if t and t != "--":
                        item = t
            if item is None:
                return None
            try:
                return float(item)
            except ValueError:
                return None

        bw_cfg = val_of("current_bandwidth_hz", prefer_edit=True)
        kp_q = val_of("current_kp_q", prefer_edit=True)
        ki_q = val_of("current_ki_q", prefer_edit=True)
        r = val_of("r")
        lq = val_of("lq")

        import math
        two_pi = 2.0 * math.pi
        bw_kp = (kp_q / (two_pi * lq)) if (kp_q is not None and lq is not None and lq > 0) else None
        bw_ki = (ki_q / (two_pi * r)) if (ki_q is not None and r is not None and r > 0) else None

        def fmt(x, prec=3):
            return f"{x:.{prec}f}" if x is not None else "--"

        self._derived_labels["bw_cfg"].setText(fmt(bw_cfg, 1))
        self._derived_labels["kp_q"].setText(fmt(kp_q))
        self._derived_labels["ki_q"].setText(fmt(ki_q))
        self._derived_labels["bw_kp"].setText(fmt(bw_kp, 1))
        self._derived_labels["bw_ki"].setText(fmt(bw_ki, 1))

        # 一致性: 三个带宽两两相对误差 < 15% 视为一致
        consist_lbl = self._derived_labels["consist"]
        vals = [v for v in (bw_cfg, bw_kp, bw_ki) if v is not None and v > 0]
        if len(vals) < 2:
            consist_lbl.setText("--")
            consist_lbl.setStyleSheet(f"color: {theme.hex('muted')}; font-weight: bold;")
        else:
            mx, mn = max(vals), min(vals)
            ok = (mx - mn) / mx < 0.15
            consist_lbl.setText("一致 ✓" if ok else "不一致 ✗")
            consist_lbl.setStyleSheet(
                f"color: {theme.hex('ok') if ok else 'danger'}; font-weight: bold;")

    # ---------- 主题 ----------
    def apply_theme(self):
        """主题切换: 刷新图例色块/只读行样式/衍生卡配色。"""
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
        # 衍生卡
        if self._derived_card is not None:
            self._refresh_derived_card_style(self._derived_card)
            for key, lbl in self._derived_labels.items():
                if key == "consist":
                    continue
                lbl.setStyleSheet(
                    f"color: {theme.hex('value')}; font-weight: bold;")
            self._update_derived_card()

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
        self._code_name_to_pid.clear()
        self._row_of_pid.clear()

        groups = self._params_by_group()
        for group, params in groups.items():
            self._append_group_row(group, params)
            for p in params:
                self._append_param_row(p)

        self._table.resizeRowsToContents()
        self._syncing_table = False
        self._update_derived_card()

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
        self._code_name_to_pid[p.code_name] = int(p.param_id)
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

    def _on_write_group_clicked(self, param_ids):
        writes = []
        for pid in param_ids:
            state = self._row_state.get(int(pid))
            item = self._edit_items.get(int(pid))
            if not state or not state.get('writable') or item is None:
                continue
            text = item.text().strip()
            if text and text != "--":
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
        state = self._row_state.get(pid)
        dirty = bool(state and item is not None and item.text().strip() != state['synced'])
        self._set_write_button_state(pid, dirty, True)

    def confirm_pending_write(self):
        """主窗口收到 PARAM_WRITE ACK 后调用。"""
        if not self._pending_writes:
            return None
        pid, sent_text = self._pending_writes.popleft()
        state = self._row_state.get(int(pid))
        current_item = self._current_items.get(int(pid))
        edit_item = self._edit_items.get(int(pid))
        if state is not None and current_item is not None and edit_item is not None:
            state['synced'] = sent_text
            current_item.setText(sent_text)
            dirty = edit_item.text().strip() != sent_text
            self._set_write_button_state(int(pid), dirty, False)
        self._update_derived_card()
        return pid

    def reject_pending_write(self):
        """主窗口收到 PARAM_WRITE NACK 后调用。"""
        if not self._pending_writes:
            return None
        pid, _sent_text = self._pending_writes.popleft()
        state = self._row_state.get(int(pid))
        item = self._edit_items.get(int(pid))
        if state is not None and item is not None:
            dirty = item.text().strip() != state['synced']
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
        text = item.text().strip()
        dirty = (text != state['synced'])
        self._set_write_button_state(int(pid), dirty, state.get('pending', False) and dirty)
        self._update_derived_card()

    def set_value(self, param_id: int, text: str):
        """收到读应答后更新当前值列"""
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
            edit_dirty = bool(edit_item and edit_item.text().strip() != state['synced'])
            state['synced'] = text
            if edit_item is not None and not edit_dirty:
                self._syncing_table = True
                try:
                    edit_item.setText(text)
                finally:
                    self._syncing_table = False
            dirty = bool(edit_item and edit_item.text().strip() != text)
            self._set_write_button_state(pid, dirty, False)
        self._update_derived_card()

    def _params_by_group(self):
        if self._source == "motor_config":
            return self._reg.motor_config_by_group()
        return self._reg.params_by_group()

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
