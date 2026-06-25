"""虚拟协议应答器(纯逻辑, 无 Qt 依赖)。

职责: 接收上位机逻辑命令 (cmd, payload), 更新物理仿真状态, 并产出应当回传的
逻辑帧列表 [(cmd, payload), ...]。同时按 SET_TELEMETRY 配置, 周期产出 0xCA 遥测帧。

帧格式严格对齐固件与 jmproto 解析:
  - READ_FEEDBACK(0xC0): 22B = pos,vel,torque,temp_motor,vbus(f32*5)+fault(u16)
  - READ_STATE(0xC1):    top_fsm,run_state,ctrl_mode,enable(u8*4)
  - 0xC2~0xC8 各读命令: 见 jmproto.feedback.parse_read_reply 反向构造
  - TELEMETRY(0xCA):     mask(u16) + 按位序拼接数据组(对齐 parse_telemetry)
  - PARAM_READ(0xE0):    param_id(u16)+type(u8)+value
  - PARAM_WRITE(0xE1):   param_id(u16)+status(u8)
  - 控制类(<=0xB8):       payload[0]=status(0=OK)
设计为可独立单元测试: 不依赖串口/Qt, 喂 (cmd,payload) 即得 (reply_frames)。
"""

import struct

from jmproto import JmCmd, JmErr, JmTlmBit, JmParamType, codec, get_registry
from .motor_sim import MotorSim


# top_fsm 取值(对齐 TopFsm): IDLE=3, READY=4, RUN=5
_TOP_IDLE = 3
_TOP_READY = 4
_TOP_RUN = 5

# 控制命令(无运动载荷)的系统语义
_SYS_ENABLE = int(JmCmd.ENABLE)
_SYS_DISABLE = int(JmCmd.DISABLE)
_SYS_STOP = int(JmCmd.STOP)
_SYS_IDLE = int(JmCmd.IDLE)
_SYS_ESTOP = int(JmCmd.ESTOP)
_SYS_CLEAR_FAULT = int(JmCmd.CLEAR_FAULT)


class VirtualResponder:
    """把命令翻译成仿真动作 + 应答帧; 维护遥测订阅与状态机映射。"""

    def __init__(self, sim: MotorSim = None):
        self.sim = sim or MotorSim()
        self.reg = get_registry()
        # 遥测订阅
        self.tlm_enabled = False
        self.tlm_mask = 0
        self.tlm_period_ms = 50
        # 状态机
        self.top_fsm = _TOP_IDLE
        self.run_state = 0

    # ================= 命令入口 =================
    def on_command(self, cmd: int, payload: bytes):
        """处理一条命令, 返回需要立即回传的帧列表 [(cmd, payload)...]。"""
        cmd = int(cmd)

        # ---- 运动控制类(运动模式命令码) ----
        spec = self.reg.get_command(cmd)
        is_motion = spec is not None and spec.category in (
            "运动控制", "高级力控", "轨迹同步", "特殊应用与测试")

        # 系统控制
        if cmd in (_SYS_ENABLE, _SYS_DISABLE, _SYS_STOP, _SYS_IDLE, _SYS_ESTOP, _SYS_CLEAR_FAULT):
            self._handle_system(cmd)
            return [self._ack(cmd)]

        if is_motion and cmd <= int(JmCmd.SINGLE_STEP):
            values = self._unpack_fields(spec, payload)
            if self.top_fsm in (_TOP_READY, _TOP_RUN):
                self.top_fsm = _TOP_RUN
                self.sim.command(cmd, values)
                self.run_state = self._ctrl_to_run_state(cmd)
                return [self._ack(cmd)]
            else:
                # 未使能拒绝运动指令
                return [self._nack(cmd, int(JmErr.STATE_DENY))]

        # ---- 反馈查询 ----
        if cmd == int(JmCmd.READ_FEEDBACK):
            return [self._feedback_frame()]
        if cmd == int(JmCmd.READ_STATE):
            return [self._state_frame()]
        if cmd in (int(JmCmd.READ_POS_VEL), int(JmCmd.READ_BUS), int(JmCmd.READ_TEMPERATURE),
                   int(JmCmd.READ_DQ_CURRENT), int(JmCmd.READ_PHASE_CURRENT),
                   int(JmCmd.READ_MULTITURN), int(JmCmd.READ_FAULT)):
            return [self._read_reply_frame(cmd)]

        # ---- 设备信息 ----
        if cmd == int(JmCmd.READ_DEV_INFO):
            return [self._dev_info_frame()]
        if cmd == int(JmCmd.READ_DEV_NAME):
            name = "VirtualJointMotor".encode('utf-8')
            return [(int(JmCmd.READ_DEV_NAME), name)]
        if cmd == int(JmCmd.HEARTBEAT):
            return [self._ack(cmd)]

        # ---- 遥测订阅 ----
        if cmd == int(JmCmd.SET_TELEMETRY):
            self._handle_set_telemetry(payload)
            return [self._ack(cmd)]

        # ---- 参数读写 ----
        if cmd == int(JmCmd.PARAM_READ):
            return [self._param_read_frame(payload)]
        if cmd == int(JmCmd.PARAM_WRITE):
            return [self._param_write_frame(payload)]
        if cmd in (int(JmCmd.PARAM_SAVE), int(JmCmd.PARAM_RESET)):
            return [self._ack(cmd)]

        # 其它: 通用 ACK
        return [self._ack(cmd)]

    # ================= 周期遥测 =================
    def on_tick(self):
        """周期回调, 返回本拍要主动上报的帧(遥测开启时一帧 0xCA, 否则空)。"""
        if not self.tlm_enabled or self.tlm_mask == 0:
            return []
        return [self._telemetry_frame(self.tlm_mask)]

    def advance(self, dt: float):
        """推进物理仿真。"""
        self.sim.step(dt)

    # ================= 内部: 命令语义 =================
    def _handle_system(self, cmd: int):
        if cmd == _SYS_ENABLE:
            self.sim.set_enabled(True)
            self.top_fsm = _TOP_READY
            self.run_state = 0
        elif cmd in (_SYS_DISABLE, _SYS_IDLE):
            self.sim.set_enabled(False)
            self.top_fsm = _TOP_IDLE
            self.run_state = 0
        elif cmd == _SYS_STOP:
            self.sim.target_vel = 0.0
            self.sim.target_torque = 0.0
            self.top_fsm = _TOP_READY if self.sim.enabled else _TOP_IDLE
            self.run_state = 0
        elif cmd == _SYS_ESTOP:
            self.sim.set_enabled(False)
            self.top_fsm = 1   # SAFETY
        elif cmd == _SYS_CLEAR_FAULT:
            self.sim.fault_mask = 0
            self.sim.warn_mask = 0
            self.top_fsm = _TOP_IDLE

    @staticmethod
    def _ctrl_to_run_state(cmd: int) -> int:
        """运动命令码 -> run_state(简化映射: 与固件同序的基础控制段一致)。"""
        mapping = {
            int(JmCmd.OPEN_LOOP): 1, int(JmCmd.CURRENT): 2, int(JmCmd.TORQUE): 3,
            int(JmCmd.MIT): 4, int(JmCmd.VELOCITY): 5, int(JmCmd.POSITION): 6,
            int(JmCmd.POSITION_VELOCITY): 7, int(JmCmd.POSITION_TORQUE): 8,
            int(JmCmd.VELOCITY_TORQUE): 9,
        }
        return mapping.get(int(cmd), 0)

    def _unpack_fields(self, spec, payload: bytes) -> dict:
        """按命令 fields 顺序从 payload 解出字段字典。"""
        values = {}
        off = 0
        for f in spec.fields:
            nb = codec.type_nbytes(f.dtype)
            if off + nb > len(payload):
                break
            values[f.name] = codec.unpack_value(f.dtype, payload[off:off + nb])
            off += nb
        return values

    # ================= 内部: 帧构造 =================
    @staticmethod
    def _ack(cmd: int):
        return (int(cmd), bytes([int(JmErr.OK)]))

    @staticmethod
    def _nack(cmd: int, err: int):
        return (int(JmCmd.NACK), bytes([int(cmd) & 0xFF, int(err) & 0xFF]))

    def _feedback_frame(self):
        s = self.sim
        payload = struct.pack('<fffffH',
                              s.pos, s.vel, s.torque, s.temp_motor, s.vbus,
                              s.fault_mask & 0xFFFF)
        return (int(JmCmd.READ_FEEDBACK), payload)

    def _state_frame(self):
        return (int(JmCmd.READ_STATE),
                bytes([self.top_fsm & 0xFF, self.run_state & 0xFF,
                       self.sim.ctrl_mode & 0xFF, 1 if self.sim.enabled else 0]))

    def _read_reply_frame(self, cmd: int):
        s = self.sim
        if cmd == int(JmCmd.READ_POS_VEL):
            return (cmd, struct.pack('<ff', s.pos, s.vel))
        if cmd == int(JmCmd.READ_BUS):
            return (cmd, struct.pack('<fff', s.vbus, s.ibus, s.power))
        if cmd == int(JmCmd.READ_TEMPERATURE):
            return (cmd, struct.pack('<ff', s.temp_fet, s.temp_motor))
        if cmd == int(JmCmd.READ_DQ_CURRENT):
            return (cmd, struct.pack('<ff', s.id, s.iq))
        if cmd == int(JmCmd.READ_PHASE_CURRENT):
            return (cmd, struct.pack('<fff', getattr(s, 'ia', 0.0),
                                     getattr(s, 'ib', 0.0), getattr(s, 'ic', 0.0)))
        if cmd == int(JmCmd.READ_MULTITURN):
            return (cmd, struct.pack('<if', s.multiturn, s.single_turn))
        if cmd == int(JmCmd.READ_FAULT):
            return (cmd, struct.pack('<II', s.fault_mask, s.warn_mask))
        return self._ack(cmd)

    def _dev_info_frame(self):
        # hw_ver(u32) fw_ver(u32) uid(12B)
        payload = struct.pack('<II', 0x00010002, 0x00000100) + bytes(range(1, 13))
        return (int(JmCmd.READ_DEV_INFO), payload)

    def _handle_set_telemetry(self, payload: bytes):
        if len(payload) >= 5:
            enable, mask, period = struct.unpack_from('<BHH', payload, 0)
            self.tlm_enabled = bool(enable)
            self.tlm_mask = mask
            self.tlm_period_ms = max(5, period) if period else self.tlm_period_ms

    def _telemetry_frame(self, mask: int):
        """按 mask 位序拼装 0xCA 帧(严格对齐 jmproto.feedback.parse_telemetry)。"""
        s = self.sim
        out = bytearray()
        out += codec.wr_u16(mask)
        if mask & JmTlmBit.POS_VEL:
            out += struct.pack('<ff', s.pos, s.vel)
        if mask & JmTlmBit.DQ:
            out += struct.pack('<ff', s.id, s.iq)
        if mask & JmTlmBit.PHASE:
            out += struct.pack('<fff', getattr(s, 'ia', 0.0),
                               getattr(s, 'ib', 0.0), getattr(s, 'ic', 0.0))
        if mask & JmTlmBit.BUS:
            out += struct.pack('<fff', s.vbus, s.ibus, s.power)
        if mask & JmTlmBit.TEMP:
            out += struct.pack('<ff', s.temp_fet, s.temp_motor)
        if mask & JmTlmBit.MULTITURN:
            out += struct.pack('<if', s.multiturn, s.single_turn)
        if mask & JmTlmBit.TORQUE:
            out += struct.pack('<f', s.torque)
        if mask & JmTlmBit.FAULT:
            out += struct.pack('<II', s.fault_mask, s.warn_mask)
        if mask & JmTlmBit.STATE:
            out += bytes([self.top_fsm & 0xFF, self.run_state & 0xFF,
                          self.sim.ctrl_mode & 0xFF, 1 if self.sim.enabled else 0])
        return (int(JmCmd.TELEMETRY), bytes(out))

    # ---- 参数 ----
    _PTYPE = {
        'u8': int(JmParamType.U8), 'i8': int(JmParamType.I8),
        'u16': int(JmParamType.U16), 'i16': int(JmParamType.I16),
        'u32': int(JmParamType.U32), 'i32': int(JmParamType.I32),
        'f32': int(JmParamType.F32),
    }

    def _param_read_frame(self, payload: bytes):
        if len(payload) < 2:
            return self._nack(int(JmCmd.PARAM_READ), int(JmErr.LENGTH))
        pid = codec.rd_u16(payload, 0)
        spec = self.reg.get_param(pid)
        if spec is None:
            return self._nack(int(JmCmd.PARAM_READ), int(JmErr.BAD_PARAM_ID))
        value = self._param_value(spec)
        ptype = self._PTYPE.get(spec.dtype, int(JmParamType.F32))
        vbytes = codec.pack_value(spec.dtype, value)
        return (int(JmCmd.PARAM_READ), codec.wr_u16(pid) + bytes([ptype]) + vbytes)

    def _param_write_frame(self, payload: bytes):
        if len(payload) < 3:
            return self._nack(int(JmCmd.PARAM_WRITE), int(JmErr.LENGTH))
        pid = codec.rd_u16(payload, 0)
        spec = self.reg.get_param(pid)
        if spec is None:
            return self._nack(int(JmCmd.PARAM_WRITE), int(JmErr.BAD_PARAM_ID))
        val = codec.unpack_value(spec.dtype, payload[2:])
        # 电机本体参数回写到仿真(用 code_name)
        if spec.code_name in self.sim.params and val is not None:
            self.sim.params[spec.code_name] = val
        else:
            self._extra_params[pid] = val
        return (int(JmCmd.PARAM_WRITE), codec.wr_u16(pid) + bytes([int(JmErr.OK)]))

    def __post_init_extra(self):
        pass

    @property
    def _extra_params(self):
        if not hasattr(self, '_extra'):
            self._extra = {}
        return self._extra

    def _param_value(self, spec):
        """取参数当前值: 电机本体参数走仿真, 其它给一个由 param_id 派生的稳定演示值。"""
        if spec.code_name in self.sim.params:
            return self.sim.params[spec.code_name]
        if spec.param_id in self._extra_params:
            return self._extra_params[spec.param_id]
        # 演示默认: 浮点给个小数, 整型给 param_id
        if codec.is_float_type(spec.dtype):
            return round(0.1 * (spec.param_id % 10) + 0.01 * spec.param_id, 4)
        return spec.param_id % 100
