"""连接面板: 串口/CAN 连接配置"""

from PyQt6.QtWidgets import (
    QGroupBox, QGridLayout, QLabel, QComboBox, QPushButton, QSpinBox,
    QCheckBox,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QIntValidator

from transport.serial_transport import SerialTransport
from ui import feature_flags


class ConnectionPanel(QGroupBox):
    """传输连接配置。发出 connect_requested(port, baud) / connect_can_requested /
    disconnect_requested 信号。"""

    connect_requested = pyqtSignal(str, int)            # 串口
    connect_can_requested = pyqtSignal(str, int, int)   # CAN: channel, bitrate, motor_id
    disconnect_requested = pyqtSignal()
    bitrate_changed = pyqtSignal(int)                   # P2-4: CAN 比特率热切换
    listen_only_changed = pyqtSignal(bool)              # P2-1: 监听模式

    def __init__(self, parent=None):
        super().__init__("连接", parent)
        self._connected = False
        self._can_available = False  # python-can 与 can_transport 模块是否可用
        self._build()
        self._detect_can_support()
        self.refresh_ports()

    def _build(self):
        self._layout = QGridLayout(self)
        self._layout.setColumnStretch(1, 1)  # 中间列(下拉框)可伸缩, 允许压缩

        # ---- 传输类型下拉 (第 0 行) ----
        self._layout.addWidget(QLabel("传输类型:"), 0, 0)
        self._combo_transport = QComboBox()
        self._combo_transport.addItem("串口", "serial")
        # CAN 选项在 _detect_can_support 中按需追加
        self._combo_transport.currentIndexChanged.connect(self._on_transport_changed)
        self._layout.addWidget(self._combo_transport, 0, 1, 1, 2)

        # ---- 串口配置区 (第 1-2 行) ----
        self._lbl_port = QLabel("串口号:")
        self._layout.addWidget(self._lbl_port, 1, 0)
        self._combo_port = QComboBox()
        self._combo_port.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._combo_port.setMinimumContentsLength(12)
        self._layout.addWidget(self._combo_port, 1, 1)

        self._btn_refresh = QPushButton("刷新")
        self._btn_refresh.setMinimumWidth(48)
        self._btn_refresh.clicked.connect(self.refresh_ports)
        self._layout.addWidget(self._btn_refresh, 1, 2)

        self._lbl_baud = QLabel("波特率:")
        self._layout.addWidget(self._lbl_baud, 2, 0)
        self._combo_baud = QComboBox()
        self._combo_baud.addItems(["9600", "19200", "38400", "57600", "115200",
                                   "230400", "460800", "921600", "2000000",
                                   "5000000", "10000000"])
        self._combo_baud.setCurrentText("115200")
        self._combo_baud.setEditable(True)
        self._combo_baud.lineEdit().setValidator(QIntValidator(1, 10000000, self))
        self._layout.addWidget(self._combo_baud, 2, 1, 1, 2)

        # ---- CAN 配置区 (第 3-5 行) ----
        self._lbl_can_ch = QLabel("CAN 通道:")
        self._layout.addWidget(self._lbl_can_ch, 3, 0)
        self._combo_can_channel = QComboBox()
        self._combo_can_channel.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._combo_can_channel.setMinimumContentsLength(18)
        self._layout.addWidget(self._combo_can_channel, 3, 1)

        self._btn_can_refresh = QPushButton("刷新")
        self._btn_can_refresh.setMinimumWidth(48)
        self._btn_can_refresh.clicked.connect(self._refresh_can_channels)
        self._layout.addWidget(self._btn_can_refresh, 3, 2)

        self._lbl_can_bitrate = QLabel("比特率:")
        self._layout.addWidget(self._lbl_can_bitrate, 4, 0)
        self._combo_can_bitrate = QComboBox()
        self._combo_can_bitrate.addItems(["500000", "800000", "1000000"])
        self._combo_can_bitrate.setCurrentText("1000000")
        self._combo_can_bitrate.setEditable(True)
        self._combo_can_bitrate.lineEdit().setValidator(QIntValidator(10000, 8000000, self))
        # P2-4: 比特率热切换(已连接时改比特率触发重连)
        self._combo_can_bitrate.currentTextChanged.connect(self._on_bitrate_changed)
        self._layout.addWidget(self._combo_can_bitrate, 4, 1, 1, 2)

        self._lbl_motor_id = QLabel("电机 ID:")
        self._layout.addWidget(self._lbl_motor_id, 5, 0)
        self._spin_can_motor_id = QSpinBox()
        self._spin_can_motor_id.setRange(0, 255)
        self._spin_can_motor_id.setValue(1)
        self._layout.addWidget(self._spin_can_motor_id, 5, 1, 1, 2)

        # P2-1: 监听模式 (第 6 行)
        self._chk_listen_only = QCheckBox("监听模式(只看帧不解析)")
        self._chk_listen_only.setToolTip("开启后只记录 CAN 物理帧日志, 不解析协议帧")
        self._chk_listen_only.toggled.connect(self.listen_only_changed.emit)
        self._layout.addWidget(self._chk_listen_only, 6, 0, 1, 3)

        # ---- 连接按钮 (第 7 行) ----
        self._btn_connect = QPushButton("连接")
        self._btn_connect.setFixedHeight(26)
        self._btn_connect.setStyleSheet(
            "background-color: #4CAF50; color: white; font-weight: bold;")
        self._btn_connect.clicked.connect(self._on_toggle)
        self._layout.addWidget(self._btn_connect, 7, 0, 1, 3)

        # 默认串口模式: 隐藏 CAN 区
        self._set_can_visible(False)

    def set_connect_row_tail_widget(self, widget):
        """在连接按钮右侧放置一个工具按钮。"""
        self._layout.removeWidget(self._btn_connect)
        self._layout.addWidget(
            self._btn_connect, 7, 0, 1, 2, Qt.AlignmentFlag.AlignVCenter)
        widget.setFixedHeight(self._btn_connect.height())
        self._layout.addWidget(widget, 7, 2, Qt.AlignmentFlag.AlignVCenter)

    # ---------------- CAN 可用性探测 ----------------
    def _detect_can_support(self):
        """探测 python-can 与 can_transport 模块可用性; 可用时在下拉中追加 CAN 选项。"""
        if not feature_flags.is_enabled("can_transport"):
            self._can_available = False
            return
        try:
            from transport.can_transport import CanTransport  # noqa: F401
            self._can_available = True
            self._combo_transport.blockSignals(True)
            self._combo_transport.addItem("CAN", "can")
            self._combo_transport.blockSignals(False)
        except ImportError:
            self._can_available = False

    # ---------------- 传输类型切换 ----------------
    def _on_transport_changed(self, _idx: int):
        is_serial = (self._combo_transport.currentData() == "serial")
        self._set_serial_visible(is_serial)
        self._set_can_visible(not is_serial)
        # 切换时刷新对应设备列表
        if is_serial:
            self.refresh_ports()
        else:
            self._refresh_can_channels()

    def _set_serial_visible(self, visible: bool):
        for w in (self._lbl_port, self._combo_port, self._btn_refresh,
                  self._lbl_baud, self._combo_baud):
            w.setVisible(visible)

    def _set_can_visible(self, visible: bool):
        for w in (self._lbl_can_ch, self._combo_can_channel, self._btn_can_refresh,
                  self._lbl_can_bitrate, self._combo_can_bitrate,
                  self._lbl_motor_id, self._spin_can_motor_id,
                  self._chk_listen_only):
            w.setVisible(visible)

    # ---------------- 设备列表刷新 ----------------
    def refresh_ports(self):
        if self._connected:
            return
        self._combo_port.clear()
        # 虚拟数据引擎选项: 受 virtual_twin 功能开关控制, 关闭时不显示
        if feature_flags.is_enabled("virtual_twin"):
            self._combo_port.addItem("VIRTUAL  虚拟数据引擎 (演示, 无需硬件)")
        ports = SerialTransport.list_ports()
        for name, desc in ports:
            self._combo_port.addItem(f"{name}  {desc}")

    def _refresh_can_channels(self):
        if self._connected or not self._can_available:
            return
        self._combo_can_channel.clear()
        try:
            from transport.can_transport import CanTransport
            for name, desc in CanTransport.list_channels():
                self._combo_can_channel.addItem(f"{name}  {desc}", name)
        except Exception as e:
            self._combo_can_channel.addItem(f"(枚举失败: {e})", "")

    # ---------------- 连接/断开 ----------------
    def _on_toggle(self):
        if self._connected:
            self.disconnect_requested.emit()
            return
        if self._combo_transport.currentData() == "can":
            channel = self._combo_can_channel.currentData() or ""
            if not channel:
                return
            try:
                bitrate = int(self._combo_can_bitrate.currentText())
            except ValueError:
                bitrate = 1000000
            motor_id = self._spin_can_motor_id.value()
            self.connect_can_requested.emit(channel, bitrate, motor_id)
            return
        # 串口分支
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

    def _on_bitrate_changed(self, text: str):
        """P2-4: CAN 已连接时改比特率, 触发 bitrate_changed 信号让主窗口重连"""
        if self._connected and text.isdigit():
            self.bitrate_changed.emit(int(text))

    # ---------------- 状态/设置 读写 ----------------
    def set_connected(self, connected: bool):
        """由主窗口在连接状态确认后调用, 更新按钮外观"""
        self._connected = connected
        self._combo_transport.setEnabled(not connected)
        self._combo_port.setEnabled(not connected)
        self._combo_baud.setEnabled(not connected)
        self._btn_refresh.setEnabled(not connected)
        self._combo_can_channel.setEnabled(not connected)
        self._btn_can_refresh.setEnabled(not connected)
        # P2-4: 比特率下拉保持可用(允许热切换), 仅禁用通道/电机ID
        self._spin_can_motor_id.setEnabled(not connected)
        if connected:
            self._btn_connect.setText("断开")
            self._btn_connect.setStyleSheet(
                "background-color: #F44336; color: white; font-weight: bold;")
        else:
            self._btn_connect.setText("连接")
            self._btn_connect.setStyleSheet(
                "background-color: #4CAF50; color: white; font-weight: bold;")

    # ---- 串口设置 ----
    def get_baud(self) -> str:
        return self._combo_baud.currentText()

    def set_baud(self, text: str):
        if text:
            self._combo_baud.setCurrentText(str(text))

    # ---- 传输类型设置 ----
    def get_transport_type(self) -> str:
        return self._combo_transport.currentData() or "serial"

    def set_transport_type(self, t: str):
        idx = self._combo_transport.findData(t)
        if idx >= 0:
            self._combo_transport.setCurrentIndex(idx)

    # ---- CAN 设置 ----
    def get_can_bitrate(self) -> str:
        return self._combo_can_bitrate.currentText()

    def set_can_bitrate(self, text: str):
        if text:
            self._combo_can_bitrate.setCurrentText(str(text))

    def get_can_motor_id(self) -> int:
        return self._spin_can_motor_id.value()

    def set_can_motor_id(self, v: int):
        self._spin_can_motor_id.setValue(int(v))

    def get_can_channel_name(self) -> str:
        return self._combo_can_channel.currentData() or ""

    def set_can_channel_name(self, name: str):
        """按 channel 名匹配下拉项; 匹配不到则保持当前选项"""
        if not name:
            return
        idx = self._combo_can_channel.findData(name)
        if idx >= 0:
            self._combo_can_channel.setCurrentIndex(idx)
