"""虚拟传输: 实现 Transport 接口, 用 VirtualResponder 回环产生数据, 无需真实串口。

在连接面板选择"虚拟数据引擎"时使用。对 JmClient 而言, 它与 SerialTransport 完全等价:
  - send(cmd, payload): 交给应答器立即生成应答帧, 经 frame_received 发回(下一事件循环)
  - 单一 QTimer 周期推进物理仿真 + 产出遥测帧
线程模型: 全部在 GUI 主线程的定时器上下文运行, 无并发, 无需锁。
"""

from PyQt6.QtCore import QTimer

from jmproto import FrameCodec
from ..base import Transport
from .responder import VirtualResponder
from .motor_sim import MotorSim


class VirtualTransport(Transport):
    """虚拟数据引擎传输。"""

    name = "virtual"
    _SIM_PERIOD_MS = 20      # 物理仿真步进(50Hz)

    def __init__(self):
        super().__init__()
        self._sim = MotorSim()
        self._resp = VirtualResponder(self._sim)
        self._open = False
        self._sim_timer = QTimer(self)
        self._sim_timer.timeout.connect(self._on_sim_tick)
        self._tlm_timer = QTimer(self)
        self._tlm_timer.timeout.connect(self._on_tlm_tick)
        self._tlm_period_ms = self._resp.tlm_period_ms

    # ---------------- 生命周期 ----------------
    def start(self):
        pass   # 无后台线程, 定时器在 open() 时启动

    def stop(self):
        self.close()

    def open(self, **cfg) -> bool:
        self.reset_stats()
        self._open = True
        self._sim_timer.start(self._SIM_PERIOD_MS)
        self.connected.emit(True)
        return True

    def close(self):
        was = self._open
        self._open = False
        self._sim_timer.stop()
        self._tlm_timer.stop()
        if was:
            self.connected.emit(False)

    def is_open(self) -> bool:
        return self._open

    # ---------------- 发送(命令回环) ----------------
    def send(self, cmd: int, payload: bytes = b'') -> bool:
        if not self._open:
            return False
        frame = FrameCodec.pack(cmd, payload)
        self.tx_bytes += len(frame)
        self.tx_frames += 1
        try:
            replies = self._resp.on_command(cmd, payload)
        except Exception as e:
            self.error_occurred.emit(f"虚拟引擎异常: {e}")
            return False
        # 应答下一拍发回, 模拟链路延迟, 避免重入
        for rcmd, rpayload in replies:
            QTimer.singleShot(0, lambda c=rcmd, p=rpayload: self._emit_frame(c, p))
        # 遥测周期可能被 SET_TELEMETRY 改变, 同步定时器
        self._sync_tlm_timer()
        return True

    def _emit_frame(self, cmd: int, payload: bytes):
        if not self._open:
            return
        rframe = FrameCodec.pack(cmd, payload)
        self.rx_bytes += len(rframe)
        self.rx_frames += 1
        self.frame_received.emit(int(cmd), bytes(payload))

    # ---------------- 定时器 ----------------
    def _on_sim_tick(self):
        self._resp.advance(self._SIM_PERIOD_MS / 1000.0)

    def _sync_tlm_timer(self):
        if self._resp.tlm_enabled:
            period = max(5, int(self._resp.tlm_period_ms))
            if not self._tlm_timer.isActive() or period != self._tlm_period_ms:
                self._tlm_period_ms = period
                self._tlm_timer.start(period)
        else:
            self._tlm_timer.stop()

    def _on_tlm_tick(self):
        if not self._open:
            return
        for rcmd, rpayload in self._resp.on_tick():
            self._emit_frame(rcmd, rpayload)

    # ---------------- 工具 ----------------
    @staticmethod
    def list_ports() -> list:
        return [("VIRTUAL", "虚拟数据引擎 (演示, 无需硬件)")]
