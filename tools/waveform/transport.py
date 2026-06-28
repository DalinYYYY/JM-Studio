"""
传输层适配: 把 protocol_reference 的串口字节 / CAN 帧 / 虚拟电机回环
统一封装为 Qt 信号驱动的 Transport 抽象, 供主窗口使用。

提供三种实现:
- SerialTransport: pyserial 串口, 后台线程读, frame_received 信号发射
- VirtualTransport: 直接对接 protocol_reference.VirtualMotor (无硬件调试)
- CanTransport: 占位, python-can 未装时禁用

所有实现都满足:
  - open(**cfg) -> bool
  - close()
  - is_open() -> bool
  - send(cmd: int, payload: bytes) -> bool
  - frame_received = pyqtSignal(int, bytes)   # 解出的逻辑帧
  - connected = pyqtSignal(bool)
  - error_occurred = pyqtSignal(str)
"""
from __future__ import annotations

import threading
import time
from typing import Optional, List, Tuple

from PyQt6.QtCore import QObject, pyqtSignal

from .protocol_reference import (
    FrameCodec, FrameDecoder, JmCmd, VirtualMotor, LoopbackBridge,
    can_make_id, can_get_cmd, can_pack_frames, CanReassembler,
)


# ============================================================================
# Transport 抽象基类
# ============================================================================

class Transport(QObject):
    """传输层抽象基类"""

    frame_received = pyqtSignal(int, bytes)   # (cmd, payload)
    connected = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)

    name = "base"

    def __init__(self):
        super().__init__()
        self.tx_bytes = 0
        self.rx_bytes = 0
        self.tx_frames = 0
        self.rx_frames = 0

    def reset_stats(self):
        self.tx_bytes = self.rx_bytes = self.tx_frames = self.rx_frames = 0

    def open(self, **cfg) -> bool:
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def is_open(self) -> bool:
        raise NotImplementedError

    def send(self, cmd: int, payload: bytes = b'') -> bool:
        raise NotImplementedError

    def start(self):
        """启动后台线程 (程序初始化时调用一次)"""
        pass

    def stop(self):
        """停止后台线程 (程序退出时调用)"""
        pass


# ============================================================================
# 串口传输 (pyserial)
# ============================================================================

class SerialTransport(Transport):
    """串口传输: pyserial + 后台读线程

    关键约束:
    - _io_lock 串行化对 serial 句柄的所有访问 (read/write/close)
    - _alive (线程生命周期) 与 _running (串口打开) 双标志分离
    - 打开时复位 FrameDecoder, 清除残留半帧
    """

    name = "serial"

    def __init__(self):
        super().__init__()
        self._ser = None
        self._decoder = FrameDecoder()
        self._alive = False
        self._running = False
        self._io_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._alive = True
        self._thread = threading.Thread(target=self._run, name="SerialRx", daemon=True)
        self._thread.start()

    def stop(self):
        self.close()
        self._alive = False
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def open(self, port: str = "", baudrate: int = 115200, **_) -> bool:
        try:
            import serial
        except ImportError:
            self.error_occurred.emit("未安装 pyserial, 请 pip install pyserial")
            return False
        try:
            if self._ser and self._ser.is_open:
                self.close()
            ser = serial.Serial(
                port=port, baudrate=baudrate,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.001, write_timeout=0.02,
            )
            with self._io_lock:
                self._ser = ser
                self._decoder.reset()
                self.reset_stats()
                self._running = True
            self._wake.set()
            self.connected.emit(True)
            return True
        except Exception as e:
            self.error_occurred.emit(f"串口打开失败: {e}")
            return False

    def close(self):
        was = self._running
        with self._io_lock:
            self._running = False
            if self._ser and self._ser.is_open:
                try: self._ser.close()
                except Exception: pass
        self._wake.set()
        if was:
            self.connected.emit(False)

    def is_open(self) -> bool:
        return self._ser is not None and getattr(self._ser, 'is_open', False) and self._running

    def send(self, cmd: int, payload: bytes = b'') -> bool:
        frame = FrameCodec.pack(cmd, payload)
        with self._io_lock:
            try:
                if not (self._ser and self._ser.is_open and self._running):
                    return False
                self._ser.write(frame)
                self.tx_bytes += len(frame)
                self.tx_frames += 1
                return True
            except Exception:
                return False

    def _run(self):
        while self._alive:
            try:
                data = b''
                if self._running and self._ser and self._ser.is_open:
                    with self._io_lock:
                        if self._ser and self._ser.is_open and self._running:
                            n = self._ser.in_waiting
                            if n > 0:
                                data = self._ser.read(n)
                            else:
                                first = self._ser.read(1)
                                if first:
                                    n = self._ser.in_waiting
                                    data = first + (self._ser.read(n) if n > 0 else b'')
                    if data:
                        self.rx_bytes += len(data)
                        for cmd, pl in self._decoder.feed(data):
                            self.rx_frames += 1
                            self.frame_received.emit(cmd, bytes(pl))
                    else:
                        self._wake.wait(0.0005)
                        self._wake.clear()
                else:
                    self._wake.wait(0.01)
                    self._wake.clear()
            except Exception as e:
                self.error_occurred.emit(f"串口异常: {e}")
                self.close()
                self._wake.wait(0.01)
                self._wake.clear()

    @staticmethod
    def list_ports() -> List[Tuple[str, str]]:
        try:
            import serial.tools.list_ports
            return [(p.device, f"{p.description} ({p.hwid})") for p in serial.tools.list_ports.comports()]
        except ImportError:
            return []


# ============================================================================
# 虚拟电机传输 (本机回环, 无硬件调试)
# ============================================================================

class VirtualTransport(Transport):
    """虚拟电机传输: 用 protocol_reference.VirtualMotor 模拟下位机

    适合无硬件时验证波形上位机的所有功能。
    后台线程周期性从 LoopbackBridge 拉取应答字节, 解帧后发 frame_received。
    """

    name = "virtual"

    def __init__(self, period_ms: int = 10):
        super().__init__()
        self._period_ms = period_ms
        self._vm: Optional[VirtualMotor] = None
        self._bridge: Optional[LoopbackBridge] = None
        self._decoder = FrameDecoder()
        self._alive = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._alive = True
        self._thread = threading.Thread(target=self._run, name="VirtualRx", daemon=True)
        self._thread.start()

    def stop(self):
        self.close()
        self._alive = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def open(self, **_) -> bool:
        self._vm = VirtualMotor(period_ms=self._period_ms)
        self._bridge = LoopbackBridge(self._vm)
        self._bridge.start()
        self._decoder.reset()
        self.reset_stats()
        self.connected.emit(True)
        return True

    def close(self):
        if self._bridge is not None:
            self._bridge.close()
            self._bridge = None
            self._vm = None
            self.connected.emit(False)

    def is_open(self) -> bool:
        return self._bridge is not None and getattr(self._bridge, 'is_open', False)

    def send(self, cmd: int, payload: bytes = b'') -> bool:
        if self._bridge is None:
            return False
        frame = FrameCodec.pack(cmd, payload)
        self._bridge.write(frame)
        self.tx_bytes += len(frame)
        self.tx_frames += 1
        return True

    def _run(self):
        while self._alive:
            if self._bridge is None:
                time.sleep(0.01)
                continue
            rx = self._bridge.read(8192)
            if rx:
                self.rx_bytes += len(rx)
                for cmd, pl in self._decoder.feed(rx):
                    self.rx_frames += 1
                    self.frame_received.emit(cmd, bytes(pl))
            else:
                time.sleep(0.001)


# ============================================================================
# CAN 传输 (占位, 需要 python-can)
# ============================================================================

class CanTransport(Transport):
    """CAN 传输: 基于 python-can, 仲裁 ID = (CMD<<8)|motor_id

    注: 单帧 (≤8B) 与多帧 (8B 满载) 在协议层有歧义, 实际使用时需要按 CMD 查表
    决定路径, 或用 DLC < 8 作为单帧判据。本实现按 DLC 判别。
    """

    name = "can"

    def __init__(self):
        super().__init__()
        self._bus = None
        self._motor_id = 1
        self._reassembler = CanReassembler()
        self._alive = False
        self._thread: Optional[threading.Thread] = None
        self._can_module = False
        try:
            import can  # noqa: F401
            self._can_module = True
        except ImportError:
            pass

    def available(self) -> bool:
        return self._can_module

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._alive = True
        self._thread = threading.Thread(target=self._run, name="CanRx", daemon=True)
        self._thread.start()

    def stop(self):
        self.close()
        self._alive = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def open(self, channel: str = "", bitrate: int = 1000000, motor_id: int = 1, **_) -> bool:
        if not self._can_module:
            self.error_occurred.emit("未安装 python-can, 请 pip install python-can")
            return False
        try:
            import can
            self._bus = can.interface.Bus(channel=channel, interface='socketcan', bitrate=bitrate)
            self._motor_id = motor_id
            self._reassembler = CanReassembler()
            self.reset_stats()
            self.connected.emit(True)
            return True
        except Exception as e:
            self.error_occurred.emit(f"CAN 打开失败: {e}")
            return False

    def close(self):
        if self._bus is not None:
            try: self._bus.shutdown()
            except Exception: pass
            self._bus = None
            self.connected.emit(False)

    def is_open(self) -> bool:
        return self._bus is not None

    def send(self, cmd: int, payload: bytes = b'') -> bool:
        if self._bus is None:
            return False
        try:
            import can
            can_id_base = can_make_id(cmd, self._motor_id)
            frames = can_pack_frames(cmd, payload)
            for can_id, data in frames:
                full_id = (can_id & 0xFF00) | (self._motor_id & 0xFF)
                msg = can.Message(
                    arbitration_id=full_id, data=data,
                    is_extended_id=True, dlc=len(data),
                )
                self._bus.send(msg, timeout=0.02)
                self.tx_bytes += len(data)
            self.tx_frames += 1
            return True
        except Exception as e:
            self.error_occurred.emit(f"CAN 发送失败: {e}")
            return False

    def _run(self):
        while self._alive:
            if self._bus is None:
                time.sleep(0.01)
                continue
            try:
                msg = self._bus.recv(0.01)
                if msg is None:
                    continue
                self.rx_bytes += len(msg.data)
                cmd = can_get_cmd(msg.arbitration_id)
                data = bytes(msg.data)
                # 单帧判别: DLC < 8 直接给载荷; DLC == 8 走多帧重组器
                if len(data) < 8:
                    self.rx_frames += 1
                    self.frame_received.emit(cmd, data)
                else:
                    for c, pl in self._reassembler.feed(cmd, data):
                        self.rx_frames += 1
                        self.frame_received.emit(c, pl)
            except Exception as e:
                self.error_occurred.emit(f"CAN 接收异常: {e}")
                time.sleep(0.1)


# ============================================================================
# 工厂
# ============================================================================

TRANSPORTS = {
    'serial': SerialTransport,
    'virtual': VirtualTransport,
    'can': CanTransport,
}


def create_transport(kind: str) -> Optional[Transport]:
    cls = TRANSPORTS.get(kind)
    return cls() if cls is not None else None
