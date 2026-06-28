"""
高层电机客户端: 传输无关的命令下发 + 应答分发 + 遥测

JmClient 持有一个 Transport(串口或 CAN), 订阅其 frame_received 做业务分发,
向 UI 暴露高层信号。命令打包基于 jmproto.registry, 新增命令无需改本文件。
"""

import struct

from PyQt6.QtCore import QObject, pyqtSignal

import jmproto as jp
from jmproto import JmCmd, JmErr, codec
from transport.base import Transport


class JmClient(QObject):
    """关节电机高层通信客户端"""

    # ---- 高层信号 ----
    feedback_updated = pyqtSignal(object)              # FeedbackData(可能部分字段)
    state_updated = pyqtSignal(int, int, int, int)     # top_fsm, run_state, ctrl_mode, enable
    ack_received = pyqtSignal(int)                     # cmd
    nack_received = pyqtSignal(int, int)               # cmd, err_code
    dev_info_received = pyqtSignal(int, int, bytes)    # hw_ver, fw_ver, uid
    dev_name_received = pyqtSignal(str)
    param_read_result = pyqtSignal(int, int, object)   # param_id, type, value_bytes
    raw_frame = pyqtSignal(int, bytes)                 # cmd, payload(RX 入口, 日志面板格式化)
    tx_frame = pyqtSignal(int, bytes)                  # cmd, payload(TX 入口, 日志面板格式化)

    connected = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)

    def __init__(self, transport: Transport):
        super().__init__()
        self._tp = transport
        self._reg = jp.get_registry()
        self._tp.frame_received.connect(self._on_frame)
        self._tp.connected.connect(self.connected)
        self._tp.error_occurred.connect(self.error_occurred)
        # 合并反馈状态: 各 READ_*/TELEMETRY 帧按 filled_fields 增量合并,
        # 让 UI 拿到的是累积完整的 FeedbackData (避免部分帧字段覆盖为默认0)
        self._last_feedback = jp.FeedbackData()

    @property
    def registry(self):
        return self._reg

    @property
    def transport(self):
        return self._tp

    def set_transport(self, transport: Transport):
        """切换底层传输(如 串口<->虚拟引擎)。会断开旧传输信号并启动新传输。

        UI 只连接 JmClient 的高层信号, 故切换传输对 UI 透明。"""
        if transport is self._tp:
            return
        old = self._tp
        try:
            old.stop()
        except Exception:
            pass
        for sig, slot in ((old.frame_received, self._on_frame),
                          (old.connected, self.connected),
                          (old.error_occurred, self.error_occurred)):
            try:
                sig.disconnect(slot)
            except Exception:
                pass
        self._tp = transport
        transport.frame_received.connect(self._on_frame)
        transport.connected.connect(self.connected)
        transport.error_occurred.connect(self.error_occurred)
        transport.start()

    # ---------------- 链路 ----------------
    def start(self):
        self._tp.start()

    def stop(self):
        self._tp.stop()

    def open(self, **cfg) -> bool:
        return self._tp.open(**cfg)

    def close(self):
        self._tp.close()

    def is_open(self) -> bool:
        return self._tp.is_open()

    # ---------------- 统一发送(含 TX 日志) ----------------
    def _send(self, cmd: int, payload: bytes = b'') -> bool:
        ok = self._tp.send(cmd, payload)
        self.tx_frame.emit(cmd, payload)
        return ok

    # ---------------- 数据驱动命令 ----------------
    def send_command(self, cmd: int, values: dict = None) -> bool:
        """按 registry 规格打包并发送任意命令"""
        payload = self._reg.pack_command(cmd, values or {})
        return self._send(cmd, payload)

    # ---- 系统控制(无载荷) ----
    def cmd_enable(self):       return self._send(JmCmd.ENABLE)
    def cmd_disable(self):      return self._send(JmCmd.DISABLE)
    def cmd_stop(self):         return self._send(JmCmd.STOP)
    def cmd_idle(self):         return self._send(JmCmd.IDLE)
    def cmd_hold(self):         return self._send(JmCmd.HOLD)
    def cmd_brake(self):        return self._send(JmCmd.BRAKE)
    def cmd_estop(self):        return self._send(JmCmd.ESTOP)
    def cmd_clear_fault(self):  return self._send(JmCmd.CLEAR_FAULT)

    # ---- 反馈查询 ----
    def query_feedback(self):   return self._send(JmCmd.READ_FEEDBACK)
    def query_state(self):      return self._send(JmCmd.READ_STATE)
    def query_pos_vel(self):    return self._send(JmCmd.READ_POS_VEL)
    def query_bus(self):        return self._send(JmCmd.READ_BUS)
    def query_temperature(self):return self._send(JmCmd.READ_TEMPERATURE)
    def query_fault(self):      return self._send(JmCmd.READ_FAULT)
    def query_dq_current(self): return self._send(JmCmd.READ_DQ_CURRENT)
    def query_phase_current(self): return self._send(JmCmd.READ_PHASE_CURRENT)
    def query_multiturn(self):  return self._send(JmCmd.READ_MULTITURN)

    # ---- 设备信息 ----
    def query_dev_info(self):   return self._send(JmCmd.READ_DEV_INFO)
    def query_dev_name(self):   return self._send(JmCmd.READ_DEV_NAME)
    def heartbeat(self):        return self._send(JmCmd.HEARTBEAT)

    # ---- 参数读写 ----
    def param_read(self, param_id: int):
        return self._send(JmCmd.PARAM_READ, codec.wr_u16(param_id))

    def param_write(self, param_id: int, value: bytes):
        return self._send(JmCmd.PARAM_WRITE, codec.wr_u16(param_id) + value)

    def param_save(self):       return self._send(JmCmd.PARAM_SAVE)

    def param_reset(self, param_id: int = 0xFFFF):
        return self._send(JmCmd.PARAM_RESET, codec.wr_u16(param_id))

    # ---- 遥测订阅 / 遥控开关 ----
    def set_telemetry(self, enable: bool, mask: int, period_ms: int = 0):
        """遥控模式开关: enable=True 启动周期上报, False 停止。
        载荷 = enable(u8) + mask(u16) + period_ms(u16), 与固件 0xCB 解析一致。
        下位机按 mask 周期主动推送 0xCA 数据帧, 不要求逐帧应答; 0xCB 本身回单次 ACK。"""
        payload = struct.pack('<BHH', 1 if enable else 0, mask & 0xFFFF, period_ms & 0xFFFF)
        return self._send(JmCmd.SET_TELEMETRY, payload)

    # ---------------- 帧分发(主线程槽) ----------------
    def _merge_and_emit_feedback(self, fb):
        """把部分字段帧合并到 _last_feedback, 再 emit 合并后的完整 fb.

        各 READ_*/TELEMETRY 帧只填充本帧涉及的字段(filled_fields), 其余保持默认 0.
        若直接 emit, UI 会把未涉及字段当作 0 显示 (如 READ_PHASE_CURRENT 帧的 pos=0
        会覆盖曲线). 这里按 filled_fields 增量合并到累积状态, 保证 UI 拿到完整数据.
        """
        merged = self._last_feedback
        for field in getattr(fb, 'filled_fields', ()):
            try:
                setattr(merged, field, getattr(fb, field))
            except Exception:
                pass
        # merged 是被持续修改的同一对象; UI 侧只读不写, 安全
        self.feedback_updated.emit(merged)

    def _on_frame(self, cmd: int, payload: bytes):
        self.raw_frame.emit(cmd, payload)

        # NACK
        if cmd == JmCmd.NACK:
            if len(payload) >= 2:
                self.nack_received.emit(payload[0], payload[1])
            return

        # ACK 类应答 (0x00~0xB8, payload[0]==status)
        if cmd <= JmCmd.SINGLE_STEP and len(payload) >= 1:
            if payload[0] == JmErr.OK:
                self.ack_received.emit(cmd)
            else:
                self.nack_received.emit(cmd, payload[0])
            return

        # READ_FEEDBACK(22B)
        if cmd == JmCmd.READ_FEEDBACK and len(payload) >= 22:
            self._merge_and_emit_feedback(jp.FeedbackData.from_feedback_payload(payload))
            return

        # READ_STATE
        if cmd == JmCmd.READ_STATE and len(payload) >= 4:
            self.state_updated.emit(*jp.parse_state(payload))
            return

        # READ_POS_VEL ~ READ_FAULT (0xC2~0xC8)
        if JmCmd.READ_PHASE_CURRENT <= cmd <= JmCmd.READ_FAULT:
            fb, filled = jp.parse_read_reply(cmd, payload)
            if fb is not None:
                self._merge_and_emit_feedback(fb)
            return

        # DEV_INFO
        if cmd == JmCmd.READ_DEV_INFO and len(payload) >= 20:
            hw = codec.rd_u32(payload, 0)
            fw = codec.rd_u32(payload, 4)
            uid = bytes(payload[8:20])
            self.dev_info_received.emit(hw, fw, uid)
            return

        # DEV_NAME
        if cmd == JmCmd.READ_DEV_NAME:
            name = payload.rstrip(b'\x00').decode('utf-8', errors='replace')
            self.dev_name_received.emit(name)
            return

        # PARAM_READ 应答: param_id(u16) type(u8) value
        if cmd == JmCmd.PARAM_READ and len(payload) >= 3:
            param_id = codec.rd_u16(payload, 0)
            ptype = payload[2]
            value_bytes = payload[3:]
            self.param_read_result.emit(param_id, ptype, value_bytes)
            return

        # PARAM_WRITE 应答: param_id(u16) status(u8)
        if cmd == JmCmd.PARAM_WRITE and len(payload) >= 3:
            status = payload[2]
            if status == JmErr.OK:
                self.ack_received.emit(cmd)
            else:
                self.nack_received.emit(cmd, status)
            return

        # TELEMETRY 同步遥测帧
        if cmd == JmCmd.TELEMETRY and len(payload) >= 2:
            fb, filled = jp.parse_telemetry(payload)
            if 'top_fsm' in filled:
                self.state_updated.emit(fb.top_fsm, fb.run_state, fb.ctrl_mode, fb.enable)
            self._merge_and_emit_feedback(fb)
            return

        # 其余: 作 ACK 处理
        if len(payload) >= 1:
            self.ack_received.emit(cmd)
