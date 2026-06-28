"""引擎桥接: GUI 线程 QTimer 驱动 DigitalTwinEngine, 转发遥测/状态/故障信号。

设计要点:
  - 全部在 GUI 主线程运行(QTimer 驱动), 与 TwinParamPanel 直接操作 engine.mp/fsm
    在同一线程, 无并发竞争(对齐 VirtualTransport 的线程模型)。
  - 每个 tick(默认 20ms) 内按 FOC 周期(100us) 批量推进 200 步, 实现多速率仿真。
  - 通信心跳由 advance() 内部按 50ms 更新, 避免误触发 COMM_LOST。
  - send_cmd() 直接投递 MotorCmd 并即时 step 一次, 命令响应无延迟。
"""

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from transport.virtual_engine import (
    DigitalTwinEngine, TwinResponder, MotorCmd, SystemState, Fault,
)


class TwinEngineBridge(QObject):
    """数字孪生引擎桥接器。

    信号:
        telemetry_ready(dict): 每个仿真周期发出最新遥测
        state_changed(str, str): sys_state 变化 (旧名, 新名)
        fault_occurred(int, str): 故障上升沿 (fault_flags, 故障描述)
        running_changed(bool): 仿真启停状态变化
        command_sent(str, str): 命令派发 (类别, 描述), 用于日志面板订阅
    """

    telemetry_ready = pyqtSignal(dict)
    state_changed = pyqtSignal(str, str)
    fault_occurred = pyqtSignal(int, str)
    running_changed = pyqtSignal(bool)
    command_sent = pyqtSignal(str, str)   # (category, message)

    SIM_PERIOD_MS = 20   # 仿真步进周期(50Hz, 每次 200 个 FOC 步)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.engine = DigitalTwinEngine()
        # 复用 TwinResponder.advance() 做批量步进 + 心跳更新(纯逻辑, 不走协议)
        self._adv = TwinResponder(self.engine)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._running = False
        self._prev_sys_state = self.engine.fsm.sys_state
        self._prev_fault = 0

    # ---------- 生命周期 ----------
    def start(self):
        if not self._running:
            self._running = True
            self._timer.start(self.SIM_PERIOD_MS)
            self.running_changed.emit(True)

    def stop(self):
        if self._running:
            self._running = False
            self._timer.stop()
            self.running_changed.emit(False)

    def is_running(self) -> bool:
        return self._running

    # ---------- 主 tick ----------
    def _on_tick(self):
        # 批量推进仿真(20ms -> 200 个 FOC 步), 内部按 50ms 更新通信心跳
        self._adv.advance(self.SIM_PERIOD_MS / 1000.0)
        telem = self.engine.get_telemetry()
        self.telemetry_ready.emit(telem)

        # sys_state 变化
        cur = self.engine.fsm.sys_state
        if cur != self._prev_sys_state:
            old_name = self._prev_sys_state.name if isinstance(self._prev_sys_state, SystemState) else str(self._prev_sys_state)
            self.state_changed.emit(old_name, cur.name)
            self._prev_sys_state = cur

        # 故障上升沿
        cur_fault = self.engine.fsm.fault_flags
        if cur_fault != self._prev_fault and cur_fault != 0:
            self.fault_occurred.emit(cur_fault, Fault.to_str(cur_fault))
        self._prev_fault = cur_fault

    # ---------- 命令接口 ----------
    def send_cmd(self, cmd: MotorCmd):
        """投递 MotorCmd 并即时 step 一次, 让状态机立即响应。"""
        self.engine.apply_cmd(cmd)
        self.engine.step()
        # 派发日志信号: 描述命令关键字段
        parts = []
        if cmd.enable:
            parts.append("enable")
        if cmd.disable:
            parts.append("disable")
        if cmd.stop:
            parts.append("stop")
        if cmd.reset:
            parts.append("reset")
        if cmd.fault_clear:
            parts.append("fault_clear")
        if cmd.start:
            parts.append(f"start mode={cmd.set_mode.name}")
        for attr in ("set_pos", "set_vel", "set_torque", "set_iq", "set_id",
                     "set_voltage", "set_duty", "set_kp", "set_kd", "set_torque_ff"):
            val = getattr(cmd, attr, None)
            if val is not None:
                parts.append(f"{attr}={val}")
        msg = "MotorCmd: " + " ".join(parts) if parts else "MotorCmd: (empty)"
        self.command_sent.emit("CMD", msg)

    # ---------- 便捷控制 ----------
    def enable(self):
        self.send_cmd(MotorCmd(enable=True))

    def disable(self):
        self.send_cmd(MotorCmd(disable=True))

    def stop_motion(self):
        self.send_cmd(MotorCmd(stop=True))

    def idle(self):
        self.send_cmd(MotorCmd(disable=True))

    def e_stop(self):
        self.send_cmd(MotorCmd(stop=True, disable=True))

    def clear_fault(self):
        self.engine.fault_detector.clear_injected()
        self.send_cmd(MotorCmd(fault_clear=True))

    def reset(self):
        self.send_cmd(MotorCmd(reset=True))

    def inject_fault(self, bits: int):
        self.engine.inject_fault(bits)
        self.command_sent.emit("SYS", f"inject_fault bits=0x{bits:04X}")

    def clear_injected_fault(self):
        self.engine.clear_injected_fault()
        self.command_sent.emit("SYS", "clear_injected_fault")
