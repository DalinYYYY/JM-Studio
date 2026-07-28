"""
高层电机客户端: 传输无关的命令下发 + 应答分发 + 遥测

JmClient 持有一个 Transport(串口或 CAN), 订阅其 frame_received 做业务分发,
向 UI 暴露高层信号。命令打包基于 jmproto.registry, 新增命令无需改本文件。
"""

import copy
import struct

from PyQt6.QtCore import QObject, pyqtSignal

import jmproto as jp
from jmproto import JmCmd, JmErr, codec
from transport.base import Transport

# 危险操作命令集合: 发送前需 UI 二次确认
# 标定启动 / Flash 保存 / 恢复出厂 / 清除故障 / 配置重置等不可逆或高风险操作
DANGER_CMDS = {
    JmCmd.CALIB_LEVEL1: "L1 标定启动",
    JmCmd.CALIB_LEVEL2: "L2 标定启动",
    JmCmd.CALIB_LEVEL3: "L3 标定启动",
    JmCmd.CALIB_LEVEL4: "L4 标定启动",
    JmCmd.CALIB_LEVEL5: "L5 标定启动",
    JmCmd.CALIB_LEVEL6: "L6 标定启动",
    JmCmd.CALIB_LEVEL7: "L7 标定启动",
    JmCmd.CLEAR_FAULT: "故障清除",
    JmCmd.FACTORY_RESET: "恢复出厂设置",
    JmCmd.PARAM_SAVE: "参数保存到 Flash",
    JmCmd.PARAM_RESET: "参数重置",
    JmCmd.MOTOR_INFO_WRITE: "电机配置写入",
    JmCmd.MOTOR_INFO_WRITE_BULK: "电机配置批量写入",
    JmCmd.MOTOR_INFO_SAVE: "电机配置保存到 Flash",
    JmCmd.MOTOR_INFO_RESET: "电机配置恢复默认",
    JmCmd.MOTOR_INFO_RECALIB_RESET: "清除标定状态",
    JmCmd.SET_CAN_ID: "设置 CAN 地址(重启生效)",
}


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
    motor_info_read_result = pyqtSignal(int, int, object)   # param_id, type, value_bytes(固定4B)
    motor_info_read_bulk_result = pyqtSignal(int, list)     # start_id, [(param_id, value_bytes)...]
    calib_status_received = pyqtSignal(dict)           # 0x97 详细标定状态(parse_calib_status 结果)
    pid_autotune_result_received = pyqtSignal(dict)    # 0xA0 PID理论估计应答(parse_pid_autotune_ack 结果)
    pid_source_received = pyqtSignal(int, int, int)    # 0xA2 三环source状态(cur, vel, pos)
    pid_param_received = pyqtSignal(int, int, bytes)   # 0xA6 PID参数读取结果(ring, param_type, value4)
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
        # 危险命令二次确认回调: UI 层注入, 签名 (cmd, payload) -> bool
        # None 表示无 UI 注入, 直接发送(用于脚本/无 GUI 场景)
        self._confirm_cb = None

    @property
    def registry(self):
        return self._reg

    @property
    def transport(self):
        return self._tp

    def set_confirm_callback(self, cb):
        """注入危险命令二次确认回调, 签名: cb(cmd: int, payload: bytes) -> bool。
        传 None 可清除回调(用于脚本/无 GUI 场景)。"""
        self._confirm_cb = cb

    def set_transport(self, transport: Transport):
        """切换底层传输(如 串口<->虚拟引擎), 切换对 UI 透明。"""
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
        # 重置累积反馈, 避免旧设备数据混入新连接
        self._last_feedback = jp.FeedbackData()
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
        # 危险命令二次确认: DANGER_CMDS 中的命令发送前需 UI 确认
        # 无 _confirm_cb 时直接发送(脚本/无 GUI 场景), 保持向后兼容
        if cmd in DANGER_CMDS and self._confirm_cb is not None:
            try:
                if not self._confirm_cb(cmd, payload):
                    desc = DANGER_CMDS.get(cmd, f"cmd=0x{cmd:02X}")
                    self.error_occurred.emit(f"已取消危险操作: {desc}")
                    return False
            except Exception as e:
                # 回调异常不阻塞发送, 仅记录日志
                self.error_occurred.emit(f"确认回调异常(忽略, 继续发送): {e}")
        ok = self._tp.send(cmd, payload)
        if ok:
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

    # ---- 电机配置(motor_info)读写 0xE6~0xEB ----
    # 与0xE0-0xE5的运行时参数独立, 面向Flash/EEPROM持久化的硬件配置/校准数据。
    # 帧内 value 固定4字节, 上位机按 motor_info.csv 的 DataType 打包/解包。
    def motor_info_read(self, param_id: int):
        """读单个电机配置(0xE6): {param_id:u16}"""
        return self._send(JmCmd.MOTOR_INFO_READ, codec.wr_u16(param_id))

    def motor_info_write(self, param_id: int, value: bytes):
        """写单个电机配置(0xE7, RAM生效): {param_id:u16;value:4B}。需调 motor_info_save 固化。"""
        if len(value) != 4:
            self.error_occurred.emit(
                f"motor_info_write param_id={param_id} value长度{len(value)}!=4, 已截断/补零")
            value = value[:4].ljust(4, b'\x00')
        return self._send(JmCmd.MOTOR_INFO_WRITE, codec.wr_u16(param_id) + value)

    def motor_info_read_bulk(self, start_id: int, count: int):
        """批量读电机配置(0xE8): {start_id:u16;count:u16}。块内连续ID有效, 跨块返回NACK。"""
        return self._send(JmCmd.MOTOR_INFO_READ_BULK,
                          codec.wr_u16(start_id) + codec.wr_u16(count))

    def motor_info_write_bulk(self, start_id: int, values: list):
        """批量写电机配置(0xE9): {start_id:u16;count:u16;value:u32[]}。
        values 为 4字节 bytes 列表, 数量即 count。块内连续ID有效。"""
        count = len(values)
        payload = codec.wr_u16(start_id) + codec.wr_u16(count)
        for v in values:
            if len(v) != 4:
                v = v[:4].ljust(4, b'\x00')
            payload += v
        return self._send(JmCmd.MOTOR_INFO_WRITE_BULK, payload)

    def motor_info_save(self):
        """把motor_info整块写入Flash(0xEA)。"""
        return self._send(JmCmd.MOTOR_INFO_SAVE)

    def motor_info_reset(self, param_id: int = 0xFFFF):
        """电机配置恢复默认(0xEB): param_id=0xFFFF 全部恢复。"""
        return self._send(JmCmd.MOTOR_INFO_RESET, codec.wr_u16(param_id))

    def motor_info_recalib_reset(self):
        """清除标定状态以便重新标定(0xEC): 保留电机本体参数, 仅清 RAM, 需随后 motor_info_save() 固化。"""
        return self._send(JmCmd.MOTOR_INFO_RECALIB_RESET)

    # ---- PID 管理 0xA0~0xA6 ----
    def pid_autotune(self, ring_select: int, cur_bw_hz: float = 0.0,
                     vel_bw_hz: float = 0.0, pos_bw_hz: float = 0.0):
        """PID 理论估计(0xA0): 基于辨识参数零极点对消法计算三环PID。
        ring_select: 位掩码 bit0=电流环 bit1=速度环 bit2=位置环; bw<=0 使用固件默认。
        应答走 pid_autotune_result_received 信号, 仅 IDLE 态可执行。"""
        payload = struct.pack('<Bfff', ring_select & 0xFF, cur_bw_hz, vel_bw_hz, pos_bw_hz)
        return self._send(JmCmd.PID_AUTOTUNE, payload)

    def pid_source_set(self, ring_select: int, source: int):
        """PID 来源切换(0xA1): ring_select 0/1/2=电流/速度/位置, source 0=默认 1=Flash 2=理论 3=调试。"""
        payload = struct.pack('<BB', ring_select & 0xFF, source & 0xFF)
        return self._send(JmCmd.PID_SOURCE_SET, payload)

    def pid_source_get(self):
        """读取三环当前 PID 参数来源(0xA2), 应答走 pid_source_received 信号(3字节)。"""
        return self._send(JmCmd.PID_SOURCE_GET)

    def pid_param_set(self, ring: int, param_type: int, value: float):
        """实时写单个PID参数(0xA5): 仅 source=3(调试)时允许写, 允许运行时整定。
        ring: 0=D轴 1=Q轴 2=速度 3=位置; param_type: 1=kp 2=ki 3=kd 4=output_limit 5=integral_limit 6=output_filter_alpha"""
        payload = struct.pack('<BBf', ring & 0xFF, param_type & 0xFF, float(value))
        return self._send(JmCmd.PID_PARAM_SET, payload)

    def pid_param_get(self, ring: int, param_type: int):
        """实时读单个PID参数(0xA6), 应答走 pid_param_received 信号。"""
        payload = struct.pack('<BB', ring & 0xFF, param_type & 0xFF)
        return self._send(JmCmd.PID_PARAM_GET, payload)

    # ---- 遥测订阅 / 遥控开关 ----
    def set_telemetry(self, enable: bool, mask: int, period_ms: int = 0):
        """遥控模式开关: enable=True 启动周期上报, False 停止。
        载荷 = enable(u8) + mask(u16) + period_ms(u16), 与固件 0xCB 解析一致。"""
        payload = struct.pack('<BHH', 1 if enable else 0, mask & 0xFFFF, period_ms & 0xFFFF)
        return self._send(JmCmd.SET_TELEMETRY, payload)

    # ---- CAN 管理 0xF0~0xF2 ----
    def cmd_set_can_id(self, new_id: int):
        """设置本机 CAN 地址(0xF0): 仅写 RAM 生效, 需随后 param_save()(0xE4) 固化, 重启后 CAN 滤波生效。
        new_id 范围 1~127, 越界由固件 dispatch 层拦截返回 NACK(OUT_OF_RANGE)。"""
        if not 1 <= new_id <= 127:
            self.error_occurred.emit(f"CAN ID 越界: {new_id}, 应在 1~127 范围内")
            return False
        return self._send(JmCmd.SET_CAN_ID, struct.pack('<B', new_id & 0xFF))

    # ---------------- 帧分发(主线程槽) ----------------
    def _merge_and_emit_feedback(self, fb):
        """把部分字段帧合并到 _last_feedback, 再 emit 合并后的完整 fb。"""
        merged = self._last_feedback
        for field in getattr(fb, 'filled_fields', ()):
            try:
                setattr(merged, field, getattr(fb, field))
            except Exception:
                pass
        # 浅拷贝后发射, 避免 QueuedConnection 槽拿到被持续修改的同一对象
        self.feedback_updated.emit(copy.copy(merged))

    def _on_frame(self, cmd: int, payload: bytes):
        self.raw_frame.emit(cmd, payload)

        # NACK
        if cmd == JmCmd.NACK:
            if len(payload) >= 2:
                self.nack_received.emit(payload[0], payload[1])
            return

        # CALIB_QUERY(0x97) 详细状态应答: 8 字节 ACK, 不走通用 ACK 分支
        # (无论 state 为何都返回 ACK, 故需在 cmd<=SINGLE_STEP 通用 ACK 分支前特判)
        if cmd == JmCmd.CALIB_QUERY:
            self.calib_status_received.emit(jp.parse_calib_status(payload))
            return

        # PID_AUTOTUNE(0xA0) 应答: 8 字节 ACK(含 status/fail_reason), 不走通用 ACK 分支
        # (无论成功/失败都返回 8 字节 ACK, 故需在 cmd<=SINGLE_STEP 通用 ACK 分支前特判)
        if cmd == JmCmd.PID_AUTOTUNE:
            self.pid_autotune_result_received.emit(jp.parse_pid_autotune_ack(payload))
            return

        # PID_SOURCE_GET(0xA2) 应答: 3 字节 (cur_src/vel_src/pos_src), 不走通用 ACK 分支
        if cmd == JmCmd.PID_SOURCE_GET:
            if len(payload) >= 3:
                self.pid_source_received.emit(payload[0], payload[1], payload[2])
            return

        # PID_PARAM_GET(0xA6) 应答: 6 字节 (ring + param_type + 4B value), 不走通用 ACK 分支
        if cmd == JmCmd.PID_PARAM_GET:
            if len(payload) >= 6:
                self.pid_param_received.emit(payload[0], payload[1], payload[2:6])
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

        # READ_DEBUG(0xC9): 调试帧, 当前不解析, 避免误入 ACK 兜底分支
        if cmd == JmCmd.READ_DEBUG:
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

        # MOTOR_INFO_READ 应答(0xE6): param_id(u16) type(u8) value(4B)
        if cmd == JmCmd.MOTOR_INFO_READ and len(payload) >= 3:
            param_id = codec.rd_u16(payload, 0)
            ptype = payload[2]
            value_bytes = bytes(payload[3:7])  # 固定4字节
            self.motor_info_read_result.emit(param_id, ptype, value_bytes)
            return

        # MOTOR_INFO_WRITE 应答(0xE7): param_id(u16) status(u8)
        if cmd == JmCmd.MOTOR_INFO_WRITE and len(payload) >= 3:
            status = payload[2]
            if status == JmErr.OK:
                self.ack_received.emit(cmd)
            else:
                self.nack_received.emit(cmd, status)
            return

        # MOTOR_INFO_READ_BULK 应答(0xE8): start_id(u16) count(u8) value[count*4]
        if cmd == JmCmd.MOTOR_INFO_READ_BULK and len(payload) >= 3:
            start_id = codec.rd_u16(payload, 0)
            count = payload[2]
            values = []
            for i in range(count):
                off = 3 + i * 4
                if off + 4 > len(payload):
                    break
                values.append((start_id + i, bytes(payload[off:off + 4])))
            self.motor_info_read_bulk_result.emit(start_id, values)
            return

        # MOTOR_INFO_WRITE_BULK(0xE9) / MOTOR_INFO_SAVE(0xEA): ACK{status:u8}
        if cmd in (JmCmd.MOTOR_INFO_WRITE_BULK, JmCmd.MOTOR_INFO_SAVE) and len(payload) >= 1:
            status = payload[0]
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
        else:
            # 空 payload 帧: 既不匹配特化分支也不满足 ACK 条件, 上报避免队列死锁
            self.error_occurred.emit(f"空payload帧 cmd=0x{cmd:02X}")
