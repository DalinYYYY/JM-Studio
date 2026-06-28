"""数字孪生协议应答器(纯逻辑, 无 Qt 依赖)。

职责: 把上位机逻辑命令 (cmd, payload) 翻译成 DigitalTwinEngine 的 MotorCmd,
并从孪生遥测构造应答帧。帧格式严格对齐 jmproto。

设计为 VirtualResponder 的高保真替代:
  - 状态机: 用孪生的 SystemStateMachine (IDLE/READY/RUN/FAULT/SAFETY)
  - 故障系统: 用孪生的 FaultDetector (OVER_CURRENT/OVER_TEMP/COMM_LOST/FOLLOW_ERR/...)
  - 物理量: 用孪生的 MotorPhysics (dq电流/位置/速度/温度/母线)
  - 参数: 直接读写 MotorParam 的嵌套 dataclass 字段
"""

import struct
from typing import Optional

from jmproto import (JmCmd, JmErr, JmTlmBit, JmParamType, TopFsm, RunState,
                     codec, get_registry)

from .digital_twin import DigitalTwinEngine
from .twin_fsm import MotorCmd, ControlMode, SystemState


# ================= 状态映射 =================
# twin SystemState -> 协议 TopFsm (数值对齐固件 state_define.h)
_SYS_TO_TOP = {
    SystemState.IDLE:    int(TopFsm.IDLE),
    SystemState.READY:   int(TopFsm.READY),
    SystemState.RUN:     int(TopFsm.RUN),
    SystemState.FAULT:   int(TopFsm.FAULT),
    SystemState.SAFETY:  int(TopFsm.SAFETY),
}

# twin ControlMode -> 协议 RunState (对齐固件 run_state_e)
_CTRL_TO_RUN = {
    ControlMode.NONE:       int(RunState.IDLE),
    ControlMode.CURRENT:    int(RunState.CURRENT),
    ControlMode.TORQUE:     int(RunState.TORQUE),
    ControlMode.IMPEDANCE:  int(RunState.MIT),         # MIT/IMPEDANCE 共用 MIT 子态
    ControlMode.VELOCITY:   int(RunState.VELOCITY),
    ControlMode.POSITION:   int(RunState.POSITION),
    ControlMode.DUTY:       int(RunState.DUTY_CYCLE),
    ControlMode.VOLTAGE:    int(RunState.VOLTAGE_VECTOR),
}

# twin ControlMode -> 协议 JmCmd 命令码 (状态帧第3字节 ctrl_mode)。
# 状态机面板运行模式图的节点 key 是 JmCmd 命令码(POSITION=21/VELOCITY=20/...),
# 故状态帧上报的 ctrl_mode 必须是 JmCmd 值, 而非 twin ControlMode 枚举值(POSITION=1/...),
# 否则面板永远点不亮当前模式块。IMPEDANCE 复用 MIT 块(面板未单列阻抗节点)。
_CTRL_TO_JMCMD = {
    ControlMode.NONE:       0,
    ControlMode.CURRENT:    int(JmCmd.CURRENT),
    ControlMode.TORQUE:     int(JmCmd.TORQUE),
    ControlMode.IMPEDANCE:  int(JmCmd.MIT),
    ControlMode.VELOCITY:   int(JmCmd.VELOCITY),
    ControlMode.POSITION:   int(JmCmd.POSITION),
    ControlMode.DUTY:       int(JmCmd.DUTY_CYCLE),
    ControlMode.VOLTAGE:    int(JmCmd.VOLTAGE_VECTOR),
}


class TwinResponder:
    """数字孪生应答器: 协议命令 <-> DigitalTwinEngine。"""

    def __init__(self, engine: DigitalTwinEngine = None):
        self.engine = engine or DigitalTwinEngine()
        self.reg = get_registry()
        # 遥测订阅
        self.tlm_enabled = False
        self.tlm_mask = 0
        self.tlm_period_ms = 50
        # 额外参数存储(非 MotorParam 内置字段)
        self._extra_params: dict = {}

    # ================= 命令入口 =================
    def on_command(self, cmd: int, payload: bytes):
        """处理一条命令, 返回需要立即回传的帧列表 [(cmd, payload)...]。"""
        cmd = int(cmd)

        # ---- 系统控制 (命令即时生效: apply_cmd 后立即 step 一次) ----
        if cmd == int(JmCmd.ENABLE):
            self.engine.apply_cmd(MotorCmd(enable=True))
            self.engine.step()
            return [self._ack(cmd)]
        if cmd in (int(JmCmd.DISABLE), int(JmCmd.IDLE)):
            self.engine.apply_cmd(MotorCmd(disable=True))
            self.engine.step()
            return [self._ack(cmd)]
        if cmd in (int(JmCmd.STOP), int(JmCmd.BRAKE), int(JmCmd.HOLD)):
            self.engine.apply_cmd(MotorCmd(stop=True))
            self.engine.step()
            return [self._ack(cmd)]
        if cmd == int(JmCmd.ESTOP):
            self.engine.apply_cmd(MotorCmd(stop=True, disable=True))
            self.engine.step()
            return [self._ack(cmd)]
        if cmd == int(JmCmd.CLEAR_FAULT):
            self.engine.apply_cmd(MotorCmd(fault_clear=True))
            self.engine.fault_detector.clear_injected()
            self.engine.step()
            return [self._ack(cmd)]

        # ---- 运动控制 ----
        spec = self.reg.get_command(cmd)
        is_motion = (spec is not None and
                     spec.category in ("运动控制", "高级力控") and
                     cmd <= int(JmCmd.SINGLE_STEP))
        if is_motion:
            top = self._top_fsm()
            # 允许 READY/RUN/SAFETY 接收运动命令:
            #   SAFETY 状态下 FSM._handle_safety 会判断方向, 仅允许反向退出
            if top not in (int(TopFsm.READY), int(TopFsm.RUN), int(TopFsm.SAFETY)):
                return [self._nack(cmd, int(JmErr.STATE_DENY))]
            values = self._unpack_fields(spec, payload)
            mc = self._build_motion_cmd(cmd, values)
            if mc is None:
                return [self._ack(cmd)]   # 不支持的运动模式, 仍 ACK
            self.engine.apply_cmd(mc)
            self.engine.step()   # 让状态机即时响应 start 指令
            return [self._ack(cmd)]

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
            return [(int(JmCmd.READ_DEV_NAME), b"DigitalTwinMotor")]
        if cmd == int(JmCmd.HEARTBEAT):
            self.engine.update_comm_heartbeat()
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

        return [self._ack(cmd)]

    # ================= 周期回调 =================
    def on_tick(self):
        """周期回调, 返回本拍要主动上报的遥测帧。"""
        if not self.tlm_enabled or self.tlm_mask == 0:
            return []
        return [self._telemetry_frame(self.tlm_mask)]

    def advance(self, dt: float):
        """推进仿真: 按孪生 FOC 周期拆分多步执行。

        传输层定时器驱动 advance 时, 默认上位机仍在线, 定期更新通信心跳,
        避免误触发 COMM_LOST (对齐固件: 有帧到达即刷新心跳)。
        """
        n = max(1, int(round(dt / self.engine.dt_foc)))
        # 每 50ms 更新一次通信心跳(模拟上位机心跳/查询帧)
        comm_period = max(1, int(0.05 / self.engine.dt_foc))
        for i in range(n):
            if i % comm_period == 0:
                self.engine.fault_detector.update_comm(self.engine.t_sim)
            self.engine.step()

    # ================= 内部: 运动命令构造 =================
    @staticmethod
    def _pick(v: dict, *names):
        """按候选字段名顺序取第一个非 None 值(兼容 CSV pos_ref / fallback pos 等命名差异)。"""
        for n in names:
            val = v.get(n)
            if val is not None:
                return val
        return None

    @staticmethod
    def _build_motion_cmd(cmd: int, v: dict) -> Optional[MotorCmd]:
        """协议运动命令 + 字段字典 -> MotorCmd (含 start/set_mode/目标)。

        字段名兼容: CSV 加载用 pos_ref/vel_ref, fallback 用 pos/vel;
        _pick 按 (短名, 长名) 顺序取值。
        """
        mc = MotorCmd()
        mc.start = True
        pick = TwinResponder._pick

        if cmd == int(JmCmd.POSITION):
            mc.set_mode = ControlMode.POSITION
            mc.set_pos = pick(v, 'pos', 'pos_ref')
        elif cmd == int(JmCmd.VELOCITY):
            mc.set_mode = ControlMode.VELOCITY
            mc.set_vel = pick(v, 'vel', 'vel_ref')
        elif cmd == int(JmCmd.TORQUE):
            mc.set_mode = ControlMode.TORQUE
            mc.set_torque = pick(v, 'torque', 'torque_ref')
        elif cmd == int(JmCmd.CURRENT):
            mc.set_mode = ControlMode.CURRENT
            mc.set_id = pick(v, 'id', 'id_ref')
            mc.set_iq = pick(v, 'iq', 'iq_ref')
        elif cmd == int(JmCmd.MIT) or cmd == int(JmCmd.IMPEDANCE):
            mc.set_mode = ControlMode.IMPEDANCE
            mc.set_pos = pick(v, 'pos', 'pos_ref')
            mc.set_kp = v.get('kp')
            mc.set_kd = v.get('kd')
            mc.set_torque_ff = pick(v, 'tff', 'torque_ff', 'torque')
            vv = pick(v, 'vel', 'vel_ref', 'vel_ff')
            if vv is not None:
                mc.set_vel_ff = vv
        elif cmd == int(JmCmd.POSITION_VELOCITY):
            mc.set_mode = ControlMode.POSITION
            mc.set_pos = pick(v, 'pos', 'pos_ref')
            mc.set_vel_ff = pick(v, 'vel_ff', 'vel')
        elif cmd == int(JmCmd.POSITION_TORQUE):
            mc.set_mode = ControlMode.POSITION
            mc.set_pos = pick(v, 'pos', 'pos_ref')
            mc.set_torque_ff = pick(v, 'tq_lim', 'torque_ff')
        elif cmd == int(JmCmd.VELOCITY_TORQUE):
            mc.set_mode = ControlMode.VELOCITY
            mc.set_vel = pick(v, 'vel', 'vel_ref')
            mc.set_torque_ff = pick(v, 'tq_lim', 'torque_ff')
        elif cmd == int(JmCmd.DUTY_CYCLE):
            mc.set_mode = ControlMode.DUTY
            mc.set_duty = v.get('duty')
        elif cmd == int(JmCmd.VOLTAGE_VECTOR):
            mc.set_mode = ControlMode.VOLTAGE
            mc.set_voltage = pick(v, 'u_alpha', 'ud')
        elif cmd == int(JmCmd.OPEN_LOOP):
            mc.set_mode = ControlMode.VOLTAGE
            mc.set_voltage = pick(v, 'u_alpha', 'ud') or 0.0
        else:
            return None
        return mc

    # ================= 内部: 帧构造 =================
    @staticmethod
    def _ack(cmd: int):
        return (int(cmd), bytes([int(JmErr.OK)]))

    @staticmethod
    def _nack(cmd: int, err: int):
        return (int(JmCmd.NACK), bytes([int(cmd) & 0xFF, int(err) & 0xFF]))

    def _tlm(self):
        """取最新遥测快照。"""
        return self.engine.get_telemetry()

    def _top_fsm(self) -> int:
        return _SYS_TO_TOP.get(self.engine.fsm.sys_state, int(TopFsm.IDLE))

    def _run_state(self) -> int:
        return _CTRL_TO_RUN.get(self.engine.fsm.control_mode, int(RunState.IDLE))

    def _ctrl_mode_jmcmd(self) -> int:
        """当前控制模式对应的 JmCmd 命令码(状态帧第3字节, 供面板运行模式图点亮)。"""
        return _CTRL_TO_JMCMD.get(self.engine.fsm.control_mode, 0)

    def _feedback_frame(self):
        t = self._tlm()
        payload = struct.pack('<fffffH',
                              t.get('pos', 0.0), t.get('vel', 0.0),
                              t.get('torque', 0.0), t.get('temp_motor', 25.0),
                              t.get('vbus', 24.0),
                              int(t.get('fault_flags', 0)) & 0xFFFF)
        return (int(JmCmd.READ_FEEDBACK), payload)

    def _state_frame(self):
        t = self._tlm()
        return (int(JmCmd.READ_STATE),
                bytes([self._top_fsm() & 0xFF, self._run_state() & 0xFF,
                       self._ctrl_mode_jmcmd() & 0xFF,
                       1 if self._top_fsm() in (int(TopFsm.READY), int(TopFsm.RUN)) else 0,
                       int(t.get('run_state', 0)) & 0xFF]))  # 第5字节: 运动子状态(STANDSTILL/MOVING/...)

    def _read_reply_frame(self, cmd: int):
        t = self._tlm()
        if cmd == int(JmCmd.READ_POS_VEL):
            return (cmd, struct.pack('<ff', t.get('pos', 0.0), t.get('vel', 0.0)))
        if cmd == int(JmCmd.READ_BUS):
            return (cmd, struct.pack('<fff', t.get('vbus', 24.0),
                                    t.get('ibus', 0.0), t.get('power', 0.0)))
        if cmd == int(JmCmd.READ_TEMPERATURE):
            return (cmd, struct.pack('<ff', t.get('temp_fet', 25.0),
                                    t.get('temp_motor', 25.0)))
        if cmd == int(JmCmd.READ_DQ_CURRENT):
            return (cmd, struct.pack('<ff', t.get('id', 0.0), t.get('iq', 0.0)))
        if cmd == int(JmCmd.READ_PHASE_CURRENT):
            return (cmd, struct.pack('<fff', t.get('ia', 0.0), t.get('ib', 0.0),
                                     t.get('ic', 0.0)))
        if cmd == int(JmCmd.READ_MULTITURN):
            return (cmd, struct.pack('<if', t.get('multiturn', 0),
                                    t.get('single', 0.0)))
        if cmd == int(JmCmd.READ_FAULT):
            return (cmd, struct.pack('<II', int(t.get('fault_flags', 0)), 0))
        return self._ack(cmd)

    def _dev_info_frame(self):
        payload = struct.pack('<II', 0x00010002, 0x00000100) + bytes(range(1, 13))
        return (int(JmCmd.READ_DEV_INFO), payload)

    def _handle_set_telemetry(self, payload: bytes):
        if len(payload) >= 5:
            enable, mask, period = struct.unpack_from('<BHH', payload, 0)
            self.tlm_enabled = bool(enable)
            self.tlm_mask = mask
            self.tlm_period_ms = max(5, period) if period else self.tlm_period_ms

    def _telemetry_frame(self, mask: int):
        t = self._tlm()
        out = bytearray()
        out += codec.wr_u16(mask)
        if mask & JmTlmBit.POS_VEL:
            out += struct.pack('<ff', t.get('pos', 0.0), t.get('vel', 0.0))
        if mask & JmTlmBit.DQ:
            out += struct.pack('<ff', t.get('id', 0.0), t.get('iq', 0.0))
        if mask & JmTlmBit.PHASE:
            out += struct.pack('<fff', t.get('ia', 0.0), t.get('ib', 0.0),
                               t.get('ic', 0.0))
        if mask & JmTlmBit.BUS:
            out += struct.pack('<fff', t.get('vbus', 24.0), t.get('ibus', 0.0),
                               t.get('power', 0.0))
        if mask & JmTlmBit.TEMP:
            out += struct.pack('<ff', t.get('temp_fet', 25.0),
                               t.get('temp_motor', 25.0))
        if mask & JmTlmBit.MULTITURN:
            out += struct.pack('<if', t.get('multiturn', 0),
                               t.get('single', 0.0))
        if mask & JmTlmBit.TORQUE:
            out += struct.pack('<f', t.get('torque', 0.0))
        if mask & JmTlmBit.FAULT:
            out += struct.pack('<II', int(t.get('fault_flags', 0)), 0)
        if mask & JmTlmBit.STATE:
            out += bytes([self._top_fsm() & 0xFF, self._run_state() & 0xFF,
                          self._ctrl_mode_jmcmd() & 0xFF,
                          1 if self._top_fsm() in (int(TopFsm.READY), int(TopFsm.RUN)) else 0,
                          int(t.get('run_state', 0)) & 0xFF])  # 第5字节: 运动子状态
        return (int(JmCmd.TELEMETRY), bytes(out))

    # ================= 内部: 参数读写 =================
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
        if not self._set_param_value(spec.code_name, val):
            self._extra_params[pid] = val
        else:
            # 写入 MotorParam 成功: 重新加载控制器 PID 配置, 使增益/限幅实时生效
            self.engine.reload_params()
        return (int(JmCmd.PARAM_WRITE), codec.wr_u16(pid) + bytes([int(JmErr.OK)]))

    # ---- 参数值访问: 遍历 MotorParam 嵌套 dataclass ----
    def _param_value(self, spec):
        """取参数当前值: 优先从 MotorParam 取, 否则从额外存储或默认值。"""
        val = self._get_param_field(spec.code_name)
        if val is not None:
            return val
        if spec.param_id in self._extra_params:
            return self._extra_params[spec.param_id]
        if codec.is_float_type(spec.dtype):
            return round(0.1 * (spec.param_id % 10) + 0.01 * spec.param_id, 4)
        return spec.param_id % 100

    def _get_param_field(self, code_name: str):
        """从 MotorParam 的嵌套 dataclass 中按 code_name 查找字段值。"""
        if not code_name:
            return None
        mp = self.engine.mp
        # 顶层字段
        if hasattr(mp, code_name):
            return getattr(mp, code_name)
        # 遍历子 dataclass
        for fname in mp.__dataclass_fields__:
            sub = getattr(mp, fname)
            if hasattr(sub, '__dataclass_fields__') and hasattr(sub, code_name):
                return getattr(sub, code_name)
        return None

    def _set_param_field(self, code_name: str, value) -> bool:
        """写入 MotorParam 的嵌套字段, 成功返回 True。"""
        if not code_name or value is None:
            return False
        mp = self.engine.mp
        if hasattr(mp, code_name):
            setattr(mp, code_name, value)
            return True
        for fname in mp.__dataclass_fields__:
            sub = getattr(mp, fname)
            if hasattr(sub, '__dataclass_fields__') and hasattr(sub, code_name):
                setattr(sub, code_name, value)
                return True
        return False

    # 兼容旧接口名
    _set_param_value = _set_param_field

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
