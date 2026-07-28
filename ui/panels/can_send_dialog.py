"""CAN 手动发帧对话框 (P2-2 调试工具)

输入 CAN ID(十六进制) + Data(最多 8 字节十六进制) + 扩展帧复选框,
确认后发出 send_requested(can_id, data, extended) 信号, 由主窗口调用
transport.send_raw() 发送。仅用于总线调试, 不走协议组帧。
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QCheckBox,
    QDialogButtonBox, QLabel, QHBoxLayout, QMessageBox,
)
from PyQt6.QtGui import QRegularExpressionValidator
from PyQt6.QtCore import pyqtSignal, QRegularExpression, Qt


class CanSendDialog(QDialog):
    """CAN 手动发帧对话框。

    信号:
        send_requested(can_id:int, data:bytes, extended:bool)
    """

    send_requested = pyqtSignal(int, bytes, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CAN 手动发帧")
        self.setMinimumWidth(420)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)

        tip = QLabel(
            "调试工具: 直接发送原始 CAN 物理帧, 不走协议组帧。\n"
            "CAN ID 编码: (CMD << 8) | motor_id, 例如反馈帧 0xC001 = CMD 0xC0 + MID 0x01。\n"
            "Data 每格 1 字节十六进制(00~FF), 留空的尾随格不发送。")
        tip.setStyleSheet("color: #F9A825; font-size: 11px;")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        form = QFormLayout()

        # CAN ID (hex)
        self._id_edit = QLineEdit("C001")
        self._id_edit.setMaxLength(8)
        self._id_edit.setValidator(
            QRegularExpressionValidator(QRegularExpression("[0-9A-Fa-f]{1,8}"), self))
        self._id_edit.setToolTip("十六进制, 1~8 位。扩展帧最大 0x1FFFFFFF")
        form.addRow("CAN ID (hex):", self._id_edit)

        # Data (8 格 hex)
        data_row = QHBoxLayout()
        data_row.setSpacing(4)
        self._data_edits = []
        hex_validator = QRegularExpressionValidator(
            QRegularExpression("[0-9A-Fa-f]{0,2}"), self)
        for i in range(8):
            edit = QLineEdit("00")
            edit.setMaxLength(2)
            edit.setFixedWidth(34)
            edit.setValidator(hex_validator)
            edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._data_edits.append(edit)
            data_row.addWidget(edit)
        data_row.addStretch()
        form.addRow("Data (hex):", data_row)

        # 扩展帧
        self._chk_extended = QCheckBox("扩展帧 (29-bit ID)")
        self._chk_extended.setChecked(True)
        self._chk_extended.setToolTip("关节电机 CAN 协议使用 29 位扩展帧")
        form.addRow("", self._chk_extended)

        layout.addLayout(form)

        # 按钮
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("发送")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _on_ok(self):
        # 校验 CAN ID
        id_text = self._id_edit.text().strip()
        if not id_text:
            QMessageBox.warning(self, "输入错误", "CAN ID 不能为空")
            self._id_edit.setFocus()
            return
        try:
            can_id = int(id_text, 16)
        except ValueError:
            QMessageBox.warning(self, "输入错误", f"CAN ID 十六进制无效: {id_text}")
            self._id_edit.setFocus()
            return
        max_id = 0x1FFFFFFF if self._chk_extended.isChecked() else 0x7FF
        if can_id > max_id:
            QMessageBox.warning(
                self, "输入错误",
                f"CAN ID 超出范围: 0x{can_id:X} > 0x{max_id:X}\n"
                f"({'扩展帧最大 0x1FFFFFFF' if self._chk_extended.isChecked() else '标准帧最大 0x7FF'})")
            self._id_edit.setFocus()
            return

        # 收集 Data: 按顺序填, 遇到第一个空格停止(避免中间空格歧义)
        data_bytes = []
        for i, edit in enumerate(self._data_edits):
            text = edit.text().strip()
            if not text:
                break
            try:
                b = int(text, 16)
            except ValueError:
                QMessageBox.warning(self, "输入错误", f"第 {i + 1} 字节十六进制无效: {text}")
                edit.setFocus()
                return
            data_bytes.append(b)
        data = bytes(data_bytes)

        if not data:
            QMessageBox.warning(self, "输入错误", "Data 至少 1 字节")
            return

        self.send_requested.emit(can_id, data, self._chk_extended.isChecked())
        self.accept()
