"""连接面板: 串口连接(CAN 预留)"""

from PyQt6.QtWidgets import (
    QGroupBox, QGridLayout, QLabel, QComboBox, QPushButton,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QIntValidator

from transport.serial_transport import SerialTransport


class ConnectionPanel(QGroupBox):
    """串口连接配置。发出 connect_requested(port, baud) / disconnect_requested 信号。"""

    connect_requested = pyqtSignal(str, int)
    disconnect_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("串口连接", parent)
        self._connected = False
        self._build()
        self.refresh_ports()

    def _build(self):
        self._layout = QGridLayout(self)

        self._layout.addWidget(QLabel("串口号:"), 0, 0)
        self._combo_port = QComboBox()
        self._combo_port.setMinimumWidth(200)
        self._layout.addWidget(self._combo_port, 0, 1)

        self._btn_refresh = QPushButton("刷新")
        self._btn_refresh.setFixedWidth(60)
        self._btn_refresh.clicked.connect(self.refresh_ports)
        self._layout.addWidget(self._btn_refresh, 0, 2)

        self._layout.addWidget(QLabel("波特率:"), 1, 0)
        self._combo_baud = QComboBox()
        self._combo_baud.addItems(["9600", "19200", "38400", "57600", "115200",
                                   "230400", "460800", "921600", "2000000",
                                   "5000000", "10000000"])
        self._combo_baud.setCurrentText("115200")
        self._combo_baud.setEditable(True)
        self._combo_baud.lineEdit().setValidator(QIntValidator(1, 10000000, self))
        self._layout.addWidget(self._combo_baud, 1, 1, 1, 2)

        self._btn_connect = QPushButton("连接")
        self._btn_connect.setFixedHeight(26)
        self._btn_connect.setStyleSheet(
            "background-color: #4CAF50; color: white; font-weight: bold;")
        self._btn_connect.clicked.connect(self._on_toggle)
        self._layout.addWidget(self._btn_connect, 2, 0, 1, 3)

    def set_connect_row_tail_widget(self, widget):
        """在连接按钮右侧放置一个工具按钮。"""
        self._layout.removeWidget(self._btn_connect)
        self._layout.addWidget(
            self._btn_connect, 2, 0, 1, 2, Qt.AlignmentFlag.AlignVCenter)
        widget.setFixedHeight(self._btn_connect.height())
        self._layout.addWidget(widget, 2, 2, Qt.AlignmentFlag.AlignVCenter)

    def refresh_ports(self):
        if self._connected:
            return
        self._combo_port.clear()
        ports = SerialTransport.list_ports()
        if not ports:
            self._combo_port.addItem("(无可用串口)")
            return
        for name, desc in ports:
            self._combo_port.addItem(f"{name}  {desc}")

    def _on_toggle(self):
        if self._connected:
            self.disconnect_requested.emit()
            return
        text = self._combo_port.currentText().strip()
        if not text or text.startswith("("):
            return
        port = text.split()[0]
        try:
            baud = int(self._combo_baud.currentText())
        except ValueError:
            baud = 115200
        baud = max(1, min(10000000, baud))
        self.connect_requested.emit(port, baud)

    def set_connected(self, connected: bool):
        """由主窗口在连接状态确认后调用, 更新按钮外观"""
        self._connected = connected
        self._combo_port.setEnabled(not connected)
        self._combo_baud.setEnabled(not connected)
        self._btn_refresh.setEnabled(not connected)
        if connected:
            self._btn_connect.setText("断开")
            self._btn_connect.setStyleSheet(
                "background-color: #F44336; color: white; font-weight: bold;")
        else:
            self._btn_connect.setText("连接")
            self._btn_connect.setStyleSheet(
                "background-color: #4CAF50; color: white; font-weight: bold;")
