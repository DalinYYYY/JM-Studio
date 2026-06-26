"""虚拟传输: 实现 Transport 接口, 用数字孪生引擎回环产生数据, 无需真实串口。

在连接面板选择"虚拟数据引擎"时使用。对 JmClient 而言, 它与 SerialTransport 完全等价:
  - send(cmd, payload): 交给应答器立即生成应答帧, 经 frame_received 发回(下一事件循环)
  - 单一 QTimer 周期推进孪生仿真 + 产出遥测帧
线程模型: 全部在 GUI 主线程的定时器上下文运行, 无并发, 无需锁。
"""

from PyQt6.QtCore import QTimer

from jmproto import FrameCodec
from ..base import Transport
from .twin_responder import TwinResponder
from .digital_twin import DigitalTwinEngine


class VirtualTransport(Transport):
    """数字孪生虚拟传输。

    默认使用高保真数字孪生引擎 (FOC + 级联控制 + 完整状态机 + 故障系统)。
    每 20ms 的仿真周期内, 按 100μs 的 FOC 周期推进 200 步, 实现多速率仿真。
    """

    name = "virtual"
    _SIM_PERIOD_MS = 20      # 仿真步进周期(50Hz, 每次 200 个 FOC 步)

    def __init__(self):
        super().__init__()
        self._engine = DigitalTwinEngine()
        self._resp = TwinResponder(self._engine)
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
        """推进孪生仿真: 20ms 内按 FOC 周期(100μs)推进 200 步。"""
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

    # ---------------- 孪生专属接口 ----------------
    def get_engine(self) -> DigitalTwinEngine:
        """暴露孪生引擎, 供 UI 获取详细遥测/注入故障。"""
        return self._engine

    # ---------------- 工具 ----------------
    @staticmethod
    def list_ports() -> list:
        return [("VIRTUAL", "数字孪生引擎 (高保真仿真, 无需硬件)")]
