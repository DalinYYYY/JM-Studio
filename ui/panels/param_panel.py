"""参数读写面板(数据驱动)

按 registry 的参数分组以表格展示全部参数, 每行可读/可写。
读写走 PARAM_READ/WRITE; 值按参数类型编解码。
"""

from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QWidget, QPushButton,
    QTreeWidget, QTreeWidgetItem, QHeaderView,
)
from PyQt6.QtCore import Qt, pyqtSignal


# 列定义
COL_NAME = 0     # 中文名 / 代码字段
COL_ID = 1       # param_id
COL_TYPE = 2     # 类型(单位)
COL_VALUE = 3    # 当前值(可编辑)
COL_RW = 4       # 读写权限


class ParamPanel(QGroupBox):
    """参数表格面板。发出 read_param(id) / write_param(id, text) / save_all() 信号。"""

    read_param = pyqtSignal(int)
    write_param = pyqtSignal(int, str)
    save_all = pyqtSignal()

    def __init__(self, registry, parent=None):
        super().__init__("参数读写", parent)
        self._reg = registry
        self._items = {}   # param_id -> QTreeWidgetItem
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(5)
        self._tree.setHeaderLabels(["参数", "ID", "类型/单位", "当前值", "权限"])
        self._tree.setAlternatingRowColors(True)
        hdr = self._tree.header()
        hdr.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        for c in (COL_ID, COL_TYPE, COL_VALUE, COL_RW):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        # 当前值列可编辑
        self._tree.setEditTriggers(
            QTreeWidget.EditTrigger.DoubleClicked | QTreeWidget.EditTrigger.SelectedClicked)
        layout.addWidget(self._tree)

        self._populate()

        # 操作按钮
        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)

        self._btn_read = QPushButton("读选中")
        self._btn_read.clicked.connect(self._on_read_selected)
        btn_layout.addWidget(self._btn_read)

        self._btn_write = QPushButton("写选中")
        self._btn_write.clicked.connect(self._on_write_selected)
        btn_layout.addWidget(self._btn_write)

        self._btn_save = QPushButton("保存到Flash")
        self._btn_save.clicked.connect(self.save_all)
        btn_layout.addWidget(self._btn_save)
        btn_layout.addStretch()

        layout.addWidget(btn_row)

    def _populate(self):
        groups = self._reg.params_by_group()
        for group, params in groups.items():
            top = QTreeWidgetItem([group])
            top.setFirstColumnSpanned(True)
            self._tree.addTopLevelItem(top)
            top.setExpanded(True)
            for p in params:
                unit = f" ({p.unit})" if p.unit and p.unit != '-' else ""
                item = QTreeWidgetItem([
                    f"{p.cn_name}  {p.code_name}",
                    str(p.param_id),
                    f"{p.dtype}{unit}",
                    "--",
                    p.rw,
                ])
                # 仅当前值列可编辑
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                item.setData(COL_ID, Qt.ItemDataRole.UserRole, p.param_id)
                top.addChild(item)
                self._items[p.param_id] = item

    def _selected_param_id(self):
        sel = self._tree.currentItem()
        if sel is None:
            return None
        return sel.data(COL_ID, Qt.ItemDataRole.UserRole)

    def _on_read_selected(self):
        pid = self._selected_param_id()
        if pid is not None:
            self.read_param.emit(int(pid))

    def _on_write_selected(self):
        pid = self._selected_param_id()
        if pid is None:
            return
        item = self._items.get(int(pid))
        if item is None:
            return
        text = item.text(COL_VALUE).strip()
        if text and text != "--":
            self.write_param.emit(int(pid), text)

    def set_value(self, param_id: int, text: str):
        """收到读应答后更新当前值列"""
        item = self._items.get(int(param_id))
        if item is not None:
            item.setText(COL_VALUE, text)
