"""
传输层抽象基类

串口(serial_transport)与 CAN(can_transport)都实现本接口, 高层 JmClient 只依赖抽象。
传输层职责: 物理链路收发字节/帧 + 组帧拆帧 -> 向上发出逻辑帧 (cmd, payload)。
不做业务解析(那是 JmClient 的事)。
"""

from PyQt6.QtCore import QObject, pyqtSignal


class Transport(QObject):
    """传输层抽象基类"""

    # 收到完整逻辑帧 (cmd:int, payload:bytes); 在后台线程发射, 经信号槽投递到主线程
    frame_received = pyqtSignal(int, bytes)
    # 连接状态变化
    connected = pyqtSignal(bool)
    # 错误信息(字符串)
    error_occurred = pyqtSignal(str)

    # 传输类型标识(子类覆盖)
    name = "base"

    def __init__(self):
        super().__init__()
        # 流量统计(累计字节/帧数), 子类在收发时累加; 速率由上层按时间差算
        self.tx_bytes = 0
        self.rx_bytes = 0
        self.tx_frames = 0
        self.rx_frames = 0

    def reset_stats(self):
        """复位流量统计(打开链路时调用)"""
        self.tx_bytes = self.rx_bytes = self.tx_frames = self.rx_frames = 0

    def start(self):
        """启动后台收发线程(程序初始化时调用一次)"""
        raise NotImplementedError

    def stop(self):
        """结束后台线程(程序退出时调用)"""
        raise NotImplementedError

    def open(self, **cfg) -> bool:
        """打开链路。cfg 由具体实现定义(串口: port/baudrate; CAN: channel/bitrate/motor_id)"""
        raise NotImplementedError

    def close(self):
        """关闭链路(不结束线程, 可重新 open)"""
        raise NotImplementedError

    def is_open(self) -> bool:
        raise NotImplementedError

    def send(self, cmd: int, payload: bytes = b'') -> bool:
        """发送一条逻辑命令(传输层负责组帧)。返回是否成功写入。"""
        raise NotImplementedError
