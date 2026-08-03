"""CAN-DI设备发现和节点地址管理窗口。"""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPushButton, QSizePolicy, QSpinBox, QStyle, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)


class CanDeviceManagerDialog(QDialog):
    scan_requested = pyqtSignal()
    identify_requested = pyqtSignal(bytes, int)
    set_id_requested = pyqtSignal(bytes, int)
    batch_set_requested = pyqtSignal(list)
    select_requested = pyqtSignal(int)
    session_closed = pyqtSignal()

    COL_STATE = 0
    COL_ID = 1
    COL_DI = 2
    COL_ROUNDS = 3
    COL_CONFLICT = 4
    COL_NEW_ID = 5
    COL_ACTIONS = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CAN 设备管理")
        self.setModal(False)
        self.setMinimumSize(1060, 560)
        self.resize(1120, 620)
        self._records = []
        self._spins: dict[bytes, QSpinBox] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self._scan_button = QPushButton("重新扫描")
        self._scan_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self._scan_button.clicked.connect(self.scan_requested.emit)
        self._auto_button = QPushButton("自动分配空闲 ID")
        self._auto_button.clicked.connect(self._auto_assign)
        self._apply_button = QPushButton("按顺序应用")
        self._apply_button.clicked.connect(self._apply_selected)
        self._summary = QLabel("尚未扫描")
        self._summary.setMinimumWidth(220)
        self._summary.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        toolbar.addWidget(self._scan_button)
        toolbar.addWidget(self._auto_button)
        toolbar.addWidget(self._apply_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self._summary)
        layout.addLayout(toolbar)

        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels(
            ["状态", "当前 ID", "CAN-DI", "发现轮次", "冲突", "新 ID", "操作"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)
        self._table.setShowGrid(False)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(38)
        self._table.verticalHeader().setMinimumSectionSize(34)
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(self.COL_STATE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self.COL_ID, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self.COL_DI, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_ROUNDS, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self.COL_CONFLICT, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self.COL_NEW_ID, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(self.COL_ACTIONS, QHeaderView.ResizeMode.Fixed)
        self._apply_column_widths()
        layout.addWidget(self._table)

    def _apply_column_widths(self):
        widths = {
            self.COL_STATE: 62,
            self.COL_ID: 74,
            self.COL_ROUNDS: 84,
            self.COL_CONFLICT: 126,
            self.COL_NEW_ID: 88,
            self.COL_ACTIONS: 236,
        }
        for col, width in widths.items():
            self._table.setColumnWidth(col, width)

    def set_scan_active(self, active: bool):
        self._scan_button.setEnabled(not active)
        self._scan_button.setText("扫描中..." if active else "重新扫描")

    def set_records(self, records: list[dict], total_rounds: int):
        old_values = {key: spin.value() for key, spin in self._spins.items()}
        self._records = list(records)
        self._spins.clear()
        self._table.setRowCount(len(records))

        conflict_ids: dict[int, int] = {}
        selector_counts: dict[bytes, int] = {}
        for rec in records:
            conflict_ids[rec["current_id"]] = conflict_ids.get(rec["current_id"], 0) + 1
            selector_counts[rec["di56"]] = selector_counts.get(rec["di56"], 0) + 1

        for row, rec in enumerate(records):
            key = rec["key"]
            node_conflict = conflict_ids[rec["current_id"]] > 1
            di_conflict = selector_counts[rec["di56"]] > 1
            can_id_ready = not node_conflict and not di_conflict
            conflict_text = "DI冲突" if di_conflict else (
                f"ID {rec['current_id']}冲突" if node_conflict else "-")
            values = [
                rec.get("state", "在线"), str(rec["current_id"]),
                key.hex().upper(), f"{len(rec.get('seen_rounds', set()))}/{total_rounds}",
                conflict_text,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, key)
                item.setToolTip(value)
                if node_conflict or di_conflict:
                    item.setBackground(QColor("#5A2A2A"))
                    item.setForeground(QColor("#FFFFFF"))
                elif can_id_ready:
                    item.setBackground(QColor("#1F4D3A"))
                    item.setForeground(QColor("#FFFFFF"))
                self._table.setItem(row, col, item)

            spin = QSpinBox()
            spin.setRange(1, 127)
            spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
            spin.setMinimumWidth(68)
            desired = rec.get("desired_id")
            spin.setValue(int(desired if desired is not None else
                              old_values.get(key, rec["current_id"])))
            spin.setEnabled(not di_conflict)
            if di_conflict:
                spin.setToolTip("CAN-DI压缩标识冲突，禁止远程改址")
            self._table.setCellWidget(row, self.COL_NEW_ID, spin)
            self._spins[key] = spin

            holder = QWidget()
            actions = QHBoxLayout()
            actions.setContentsMargins(4, 2, 4, 2)
            actions.setSpacing(6)
            identify = QPushButton("识别")
            identify.setMinimumWidth(58)
            identify.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            identify.clicked.connect(
                lambda _=False, di=bytes(rec["di56"]):
                self.identify_requested.emit(di, 30))
            apply_one = QPushButton("改址")
            apply_one.setMinimumWidth(58)
            apply_one.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            apply_one.setEnabled(not di_conflict)
            if di_conflict:
                apply_one.setToolTip("CAN-DI压缩标识冲突，禁止远程改址")
            apply_one.clicked.connect(
                lambda _=False, di=bytes(rec["di56"]), s=spin:
                self.set_id_requested.emit(di, s.value()))
            select = QPushButton("选中")
            select.setMinimumWidth(64)
            select.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            select.setEnabled(can_id_ready)
            if can_id_ready:
                select.setToolTip("CAN ID唯一，可以进入主窗口控制")
                select.setStyleSheet(
                    "QPushButton { background:#1E7A4D; color:white; font-weight:bold; }"
                    "QPushButton:hover { background:#24915C; }")
            else:
                select.setToolTip("当前ID或CAN-DI存在冲突，需先改址后再选择")
            select.clicked.connect(
                lambda _=False, nid=int(rec["current_id"]):
                self.select_requested.emit(nid))
            actions.addWidget(identify)
            actions.addWidget(apply_one)
            actions.addWidget(select)
            actions.addStretch(1)
            holder.setLayout(actions)
            self._table.setCellWidget(row, self.COL_ACTIONS, holder)
            self._table.setRowHeight(row, 38)

        conflicts = sum(1 for count in conflict_ids.values() if count > 1)
        self._summary.setText(f"设备 {len(records)} 台，ID冲突组 {conflicts} 个")
        self._apply_column_widths()

    def _target_rows(self) -> list[int]:
        rows = sorted({index.row() for index in self._table.selectionModel().selectedRows()})
        return rows if rows else list(range(self._table.rowCount()))

    def _auto_assign(self):
        occupied = {int(rec["current_id"]) for rec in self._records}
        available = [node_id for node_id in range(1, 128) if node_id not in occupied]
        for row in self._target_rows():
            if not available:
                QMessageBox.warning(self, "自动分配", "没有足够的空闲 CAN ID")
                return
            key_item = self._table.item(row, self.COL_DI)
            key = bytes(key_item.data(Qt.ItemDataRole.UserRole))
            spin = self._spins.get(key)
            if spin is not None and spin.isEnabled():
                spin.setValue(available.pop(0))

    def _apply_selected(self):
        changes = []
        by_key = {rec["key"]: rec for rec in self._records}
        for row in self._target_rows():
            key_item = self._table.item(row, self.COL_DI)
            key = bytes(key_item.data(Qt.ItemDataRole.UserRole))
            rec = by_key.get(key)
            spin = self._spins.get(key)
            if rec is None or spin is None or not spin.isEnabled():
                continue
            new_id = spin.value()
            if new_id != rec["current_id"]:
                changes.append((bytes(rec["di56"]), new_id))
        if changes:
            self.batch_set_requested.emit(changes)

    def closeEvent(self, event):
        self.session_closed.emit()
        super().closeEvent(event)
