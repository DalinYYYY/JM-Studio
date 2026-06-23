"""参数读写面板(数据驱动)

按 registry 的参数分组以表格展示全部参数。
每行提供中文名、param_id、类型/单位、当前值、读/写按钮。
读写走 PARAM_READ/WRITE; 值按参数类型编解码。
"""

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QBrush, QFont


COL_NAME = 0
COL_ID = 1
COL_TYPE = 2
COL_VALUE = 3
COL_READ = 4
COL_WRITE = 5


class ParamPanel(QGroupBox):
    """参数表格面板。发出 read_param(id) / write_param(id, text) / save_all() 信号。"""

    read_param = pyqtSignal(int)
    write_param = pyqtSignal(int, str)
    save_all = pyqtSignal()

    def __init__(self, registry, parent=None):
        super().__init__("参数读写", parent)
        self._reg = registry
        self._value_items = {}   # param_id -> QTableWidgetItem
        self._write_buttons = {} # param_id -> QPushButton
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(["参数", "ID", "类型/单位", "当前值", "读", "写"])
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

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_ID, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_TYPE, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_VALUE, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_READ, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_WRITE, QHeaderView.ResizeMode.ResizeToContents)

        self._table.setStyleSheet("""
            QTableWidget {
                gridline-color: #444;
            }
            QHeaderView::section {
                padding: 4px 6px;
                background: #2A2A2A;
                color: #D8D8D8;
                border: 1px solid #444;
                font-weight: bold;
            }
            QTableWidget::item {
                padding: 2px 6px;
            }
        """)

        layout.addWidget(self._table, 1)

        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)

        self._btn_save = QPushButton("保存到Flash")
        self._btn_save.clicked.connect(self.save_all)
        btn_layout.addWidget(self._btn_save)
        btn_layout.addStretch()
        layout.addWidget(btn_row)

        self._populate()

    def _make_group_item(self, text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setForeground(QBrush(QColor("#F0F0F0")))
        font = QFont()
        font.setBold(True)
        item.setFont(font)
        item.setBackground(QBrush(QColor("#303030")))
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

    def _make_button(self, text: str, callback) -> QPushButton:
        btn = QPushButton(text)
        btn.setFixedHeight(22)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(callback)
        btn.setStyleSheet("""
            QPushButton {
                padding: 0 8px;
                border: 1px solid #555;
                border-radius: 4px;
                background: #2A2A2A;
                color: #DDD;
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
        """)
        return btn

    def _populate(self):
        self._table.setRowCount(0)
        self._value_items.clear()
        self._write_buttons.clear()

        groups = self._reg.params_by_group()
        for group, params in groups.items():
            self._append_group_row(group)
            for p in params:
                self._append_param_row(p)

        self._table.resizeRowsToContents()

    def _append_group_row(self, group: str):
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setRowHeight(row, 24)

        item = self._make_group_item(group)
        self._table.setItem(row, COL_NAME, item)
        self._table.setSpan(row, COL_NAME, 1, self._table.columnCount())

    def _append_param_row(self, p):
        row = self._table.rowCount()
        self._table.insertRow(row)

        unit = f" ({p.unit})" if p.unit and p.unit != '-' else ""
        name_item = self._make_cell_item(f"{p.cn_name}  {p.code_name}")
        id_item = self._make_cell_item(str(p.param_id))
        type_item = self._make_cell_item(f"{p.dtype}{unit}")
        value_item = self._make_cell_item("--", editable=p.writable)

        self._table.setItem(row, COL_NAME, name_item)
        self._table.setItem(row, COL_ID, id_item)
        self._table.setItem(row, COL_TYPE, type_item)
        self._table.setItem(row, COL_VALUE, value_item)

        read_btn = self._make_button("读", lambda _=False, pid=p.param_id: self.read_param.emit(int(pid)))
        write_btn = self._make_button("写", lambda _=False, pid=p.param_id: self._on_write_clicked(int(pid)))
        write_btn.setEnabled(p.writable)
        self._table.setCellWidget(row, COL_READ, read_btn)
        self._table.setCellWidget(row, COL_WRITE, write_btn)

        self._value_items[p.param_id] = value_item
        self._write_buttons[p.param_id] = write_btn

        for col in range(self._table.columnCount()):
            item = self._table.item(row, col)
            if item is not None:
                item.setData(Qt.ItemDataRole.UserRole, p.param_id)

    def _on_write_clicked(self, param_id: int):
        item = self._value_items.get(int(param_id))
        if item is None:
            return
        text = item.text().strip()
        if text and text != "--":
            self.write_param.emit(int(param_id), text)

    def set_value(self, param_id: int, text: str):
        """收到读应答后更新当前值列"""
        item = self._value_items.get(int(param_id))
        if item is not None:
            item.setText(text)
