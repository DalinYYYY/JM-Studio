"""
串口传输实现: 基于 pyserial + QThread 异步收发

关键约束(踩过的坑, 勿改):
- 单一 _io_lock 串行化对 self._ser 的所有访问(read/write/close), 否则主线程发送与
  工作线程接收并发操作同一句柄, 在 Windows 上触发底层访问冲突导致进程段错误崩溃。
- _alive(线程生命周期) 与 _running(串口打开) 双标志分离: 线程在整个程序存活, 串口未开时
  空转休眠, 连接/断开无需重建线程。
- 打开串口时复位 FrameDecoder, 清除残留半帧。
- CRC 为 CRC16-XMODEM(jmproto.frame 内置), 必须与固件一致。
"""

import threading
from typing import Optional

import serial
import serial.tools.list_ports

from jmproto import FrameCodec, FrameDecoder
from .base import Transport


class SerialTransport(Transport):
    """串口传输"""

    name = "serial"
    _READ_TIMEOUT_S = 0.001
    _IDLE_WAIT_S = 0.0005
    _CLOSED_WAIT_S = 0.01
    _WRITE_TIMEOUT_S = 0.02

    def __init__(self):
        super().__init__()
        self._ser: Optional[serial.Serial] = None
        self._decoder = FrameDecoder()
        self._alive = False     # 线程存活标志(线程生命周期)
        self._running = False   # 串口打开标志(收发使能)
        self._io_lock = threading.Lock()  # 串行化所有句柄访问
        self._thread: Optional[threading.Thread] = None

    # ---------------- 线程生命周期 ----------------
        self._thread: Optional[threading.Thread] = None
        self._wake_event = threading.Event()

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._alive = True
        self._thread = threading.Thread(target=self._run, name="SerialRx", daemon=True)
        self._thread.start()

    def stop(self):
        self.close()
        self._alive = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ---------------- 链路操作 ----------------
    def open(self, port: str = "", baudrate: int = 115200, **_) -> bool:
        try:
            if self._ser and self._ser.is_open:
                self.close()
            ser = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self._READ_TIMEOUT_S,
                write_timeout=self._WRITE_TIMEOUT_S,
            )
            with self._io_lock:
                self._ser = ser
                self._decoder.reset()
                self.reset_stats()
                self._running = True
            self._wake_event.set()
            self.connected.emit(True)
            return True
        except Exception as e:
            self.error_occurred.emit(f"串口打开失败: {e}")
            return False

    def close(self):
        was_running = self._running
        with self._io_lock:
            self._running = False
            if self._ser and self._ser.is_open:
                try:
                    self._ser.close()
                except Exception:
                    pass
        self._wake_event.set()
        if was_running:
            self.connected.emit(False)

    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open and self._running

    def send(self, cmd: int, payload: bytes = b'') -> bool:
        frame = FrameCodec.pack(cmd, payload)
        with self._io_lock:
            try:
                if not (self._ser and self._ser.is_open and self._running):
                    return False
                self._ser.write(frame)
                self.tx_bytes += len(frame)
                self.tx_frames += 1
                self._wake_event.set()
                return True
            except Exception:
                return False

    # ---------------- 后台收循环 ----------------
    def _run(self):
        while self._alive:
            data = b''
            try:
                if self._running and self._ser and self._ser.is_open:
                    # 持锁内只做最小句柄访问(读), 解帧/emit 放锁外
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
                        for cmd, payload in self._decoder.feed(data):
                            self.rx_frames += 1
                            self.frame_received.emit(cmd, bytes(payload))
                    else:
                        self._wake_event.wait(self._IDLE_WAIT_S)
                        self._wake_event.clear()
                else:
                    self._wake_event.wait(self._CLOSED_WAIT_S)
                    self._wake_event.clear()
            except serial.SerialException as e:
                self.error_occurred.emit(f"串口异常: {e}")
                self.close()
                self._wake_event.wait(self._CLOSED_WAIT_S)
                self._wake_event.clear()
            except Exception as e:
                self.error_occurred.emit(f"未知错误: {e}")
                self._wake_event.wait(self._CLOSED_WAIT_S)
                self._wake_event.clear()

    # ---------------- 工具 ----------------
    @staticmethod
    def list_ports() -> list:
        """列出可用串口: [(device, description), ...]"""
        ports = []
        for p in serial.tools.list_ports.comports():
            ports.append((p.device, f"{p.description} ({p.hwid})"))
        return ports
