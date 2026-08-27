"""数字孪生协议应答器(纯逻辑, 无 Qt 依赖)。

职责: 把上位机逻辑命令 (cmd, payload) 翻译成 DigitalTwinEngine 的 MotorCmd,
并从孪生遥测构造应答帧。帧格式严格对齐 jmproto。

设计为 VirtualResponder 的高保真替代:
  - 状态机: 用孪生的 SystemStateMachine (IDLE/READY/RUN/FAULT/SAFETY)
  - 故障系统: 用孪生的 FaultDetector (OVER_CURRENT/OVER_TEMP/COMM_LOST/FOLLOW_ERR/...)
  - 物理量: 用孪生的 MotorPhysics (dq电流/位置/速度/温度/母线)
  - 参数: 直接读写 MotorParam 的嵌套 dataclass 字段
"""

import math
import struct
from typing import Optional

from jmproto import (JmCmd, JmErr, JmTlmBit, JmParamType, TopFsm, RunState,
                     codec, get_registry)
from jmproto.cmd_def import MAGIC_SOFT_RESET

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
    ControlMode.PASSIVE_TORQUE: int(RunState.PASSIVE_TORQUE),  # 对拖台 0x60, 对齐固件 run_state_e
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
    ControlMode.PASSIVE_TORQUE: int(JmCmd.PASSIVE_TORQUE),  # 对拖台 0x60
}


class TwinResponder:
    """数字孪生应答器: 协议命令 <-> DigitalTwinEngine。"""

    # motor_info.csv VariableName -> MotorParam 字段名别名映射
    # (motor_info 用长名, MotorParam 用短名; 不在此表的 code_name 视为同名直接匹配)
    _CSV_NAME_ALIASES = {
        'phase_resistance': 'r',
        'phase_inductance_d': 'ld',
        'phase_inductance_q': 'lq',
        'flux_linkage': 'flux',
        'torque_constant': 'kt',
        'rotor_inertia': 'inertia',
        # current_lim(Index 32) 与 peak_current(Index 43) 是两个独立字段:
        #   current_lim = 硬件保护瞬时电流上限 (仿真模型不直接使用, 仅存储)
        #   peak_current = 运行期限幅电流 (映射到 MotorParam.peak_current 用于限幅)
        'current_lim': 'current_lim',
        'peak_current': 'peak_current',
        'max_speed': 'max_speed',
        'current_control_bandwidth': 'current_bandwidth_hz',
        'kp_ld': 'current_kp_d', 'ki_ld': 'current_ki_d',
        'kp_lq': 'current_kp_q', 'ki_lq': 'current_ki_q',
        'integral_limit': 'current_integral_limit',
        'comp_du_V': 'deadtime_comp_v',
        'pwm_duty_max': 'pwm_max_duty',
        'kp_s': 'speed_kp', 'ki_s': 'speed_ki',
        'vff': 'velocity_ff_gain', 'aff': 'accel_ff_gain',
        'kp_p': 'position_kp',
        'ki_p': 'position_integral_limit',
        'position_filter_alpha': 'pos_filter_alpha',
        'following_error_limit': 'follow_error_threshold',
        'over_current_A': 'protect_over_current',
        'over_voltage_V': 'protect_over_voltage',
        'under_voltage_V': 'protect_under_voltage',
        'over_temp_drive': 'protect_over_temp',
        'over_temp_motor': 'motor_over_temp_threshold',
        'over_speed_rad_s': 'protect_over_speed',
        'error_enable_mask': 'protect_enable_mask',
        'mit_kp': 'impedance_kp', 'mit_kd': 'impedance_kd',
        'mit_max_current': 'iq_max',
        'homing_speed': 'homing_speed_fast',
    }

    def __init__(self, engine: DigitalTwinEngine = None):
        self.engine = engine or DigitalTwinEngine()
        self.reg = get_registry()
        # 遥测订阅
        self.tlm_enabled = False
        self.tlm_mask = 0
        self.tlm_period_ms = 50
        # 额外参数存储(非 MotorParam 内置字段)
        self._extra_params: dict = {}
        # 电机配置(motor_info)写入存储(非 MotorParam 内置字段)
        self._motor_config_store: dict = {}
        self._pid_sources = [0, 0, 0]
        self._pid_profiles = {
            0: {1: 5.5, 2: 1500.0, 3: 0.0, 4: 24.0, 5: 200.0, 6: 1.0, 7: 1},
            1: {1: 5.5, 2: 1500.0, 3: 0.0, 4: 24.0, 5: 200.0, 6: 1.0, 7: 1},
            2: {1: 0.08, 2: 0.8, 3: 0.0, 4: 20.0, 5: 200.0, 6: 1.0, 7: 3},
            3: {1: 10.0, 2: 0.0, 3: 0.0, 4: 30.0, 5: 0.0, 6: 1.0, 7: 0},
        }
        self._trace_session = 0
        self._trace_mask = 0
        self._trace_rate = 0
        self._trace_packet_samples = 0
        self._trace_sequence = 0
        self._trace_sample_index = 0
        self._sweep_active = False
        self._sweep_session = 0
        self._sweep_test_mode = 0
        self._sweep_bias = 0.0
        self._sweep_freqs_hz: list[float] = []
        self._sweep_point = 0
        self._sweep_point_sample = 0
        self._sweep_point_total = 0

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
            self.engine.apply_cmd(MotorCmd(estop=True))
            self.engine.step()
            return [self._ack(cmd)]
        if cmd == int(JmCmd.CLEAR_FAULT):
            self.engine.apply_cmd(MotorCmd(fault_clear=True))
            self.engine.fault_detector.clear_injected()
            self.engine.step()
            return [self._ack(cmd)]

        # ---- 软件复位 0x07: 校验魔数后模拟重启(停机+停遥测), 回 ACK ----
        if cmd == int(JmCmd.SOFT_RESET):
            if len(payload) >= 4 and int.from_bytes(payload[:4], 'little') != MAGIC_SOFT_RESET:
                return [self._nack(cmd, int(JmErr.UNAUTHORIZED))]
            self.engine.apply_cmd(MotorCmd(disable=True))
            self.engine.step()
            self.tlm_enabled = False
            return [self._ack(cmd)]

        # ---- 运动控制 ----
        spec = self.reg.get_command(cmd)
        is_motion = (spec is not None and
                     spec.category in ("运动控制", "高级力控", "轨迹同步",
                                       "对拖台负载", "特殊应用与测试") and
                     cmd <= int(JmCmd.SINGLE_STEP) and
                     cmd != int(JmCmd.TEST_SWEEP_FREQ))
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

        # ---- Bode 扫频(0x76): 虚拟机生成带扩展元数据的合成 TRACE ----
        if cmd == int(JmCmd.TEST_SWEEP_FREQ):
            if len(payload) == 1 and (payload[0] & 0x80) == 0:
                self._sweep_active = False
                self._trace_mask = 0
                self._trace_packet_samples = 0
                return [(cmd, b'')]
            if len(payload) not in (8, 12) or (payload[0] & 0x80) == 0:
                return [self._nack(cmd, int(JmErr.LENGTH))]
            control, point_cfg = payload[0], payload[1]
            test_mode = (control >> 4) & 0x07
            sweep_mode = (control >> 2) & 0x03
            if (control & 0x03) or test_mode > 6 or sweep_mode > 2 or not 1 <= point_cfg <= 255:
                return [self._nack(cmd, int(JmErr.OUT_OF_RANGE))]
            f_start_x10, f_end_x10, amp_raw = struct.unpack_from('<HHH', payload, 2)
            bias_raw = 0
            if len(payload) == 12:
                if struct.unpack_from('<H', payload, 10)[0] != 0xB076:
                    return [self._nack(cmd, int(JmErr.LENGTH))]
                bias_raw = struct.unpack_from('<h', payload, 8)[0]
            if not (1 <= f_start_x10 <= f_end_x10 <= 65535 and amp_raw > 0):
                return [self._nack(cmd, int(JmErr.OUT_OF_RANGE))]
            if self._top_fsm() != int(TopFsm.READY) or self._trace_mask:
                return [self._nack(cmd, int(JmErr.BUSY))]
            f_start = f_start_x10 * 0.1
            f_end = f_end_x10 * 0.1
            if f_start == f_end:
                freqs = [f_start]
            elif sweep_mode == 0:
                freqs = [f_start + (f_end - f_start) * i / (point_cfg - 1)
                         for i in range(point_cfg)] if point_cfg > 1 else []
            elif sweep_mode == 2:
                # 1Hz固定步进: 频点数由起止频率决定, 与固件
                # motor_mode_test_sweep.c 的 ceil(f_end-f_start)+1 规则一致。
                count = math.ceil(f_end - f_start) + 1
                freqs = [min(f_start + i, f_end) for i in range(count)]
                freqs[-1] = f_end
            else:
                count = math.ceil(math.log10(f_end / f_start) * point_cfg) + 1
                freqs = [min(f_end, f_start * (10.0 ** (i / point_cfg)))
                         for i in range(count)]
                freqs[-1] = f_end
            if not freqs or len(freqs) > 1024:
                return [self._nack(cmd, int(JmErr.OUT_OF_RANGE))]
            self._sweep_session = (self._sweep_session + 1) & 0xFFFF or 1
            self._sweep_active = True
            self._sweep_test_mode = test_mode
            self._sweep_bias = bias_raw * (0.01 if test_mode in (3, 4, 5) else 0.001)
            self._sweep_freqs_hz = freqs
            self._sweep_point = 0
            self._sweep_point_sample = 0
            self._sweep_point_total = 0
            self._trace_session = self._sweep_session
            # 与固件 jm_proto_ops.c 对齐: 机械模式0/1输入取目标电流
            # (IQ_REF), 模式6取实测电流(IQ), 模式3取实测速度(VEL);
            # 验证模式(2/4/5)输入保持参考通道
            self._trace_mask = ((1 << 2) | (1 << 3)) if test_mode == 2 else ((1 << 3) | (1 << 5)) if test_mode == 6 else ((1 << 5) | (1 << 7)) if test_mode == 3 else ((1 << 4) | (1 << 5)) if test_mode == 4 else ((1 << 6) | (1 << 7)) if test_mode == 5 else ((1 << 2) | (1 << 5))
            # 采样率策略与固件 jm_proto_ops.c 对齐: 电流环对象≥5kHz,
            # 速度/位置对象≥2kHz, 保底 f_end×20, 上限钳制到控制频率10kHz。
            rate_min = 5000 if test_mode == 2 else 2000
            self._trace_rate = min(max(int(f_end * 20.0), rate_min), 10000)
            self._trace_packet_samples = 24
            self._trace_sequence = 0
            self._trace_sample_index = 0
            duration_x100 = int(sum((3 + 8) / max(f, 0.1) * 100 for f in freqs) + 0.5)
            return [(cmd, codec.wr_u16(self._sweep_session) +
                     codec.wr_u16(len(freqs)) + codec.wr_u32(duration_x100))]

        # ---- 遥测订阅 ----
        if cmd == int(JmCmd.SET_TELEMETRY):
            self._handle_set_telemetry(payload)
            return [self._ack(cmd)]

        # ---- 标定查询(0x97): 返回 8 字节状态帧(state=IDLE/0) ----
        if cmd == int(JmCmd.CALIB_QUERY):
            # state=0(IDLE), fail_reason=0, progress=0, level=0, submode=0, step=0, step_total=0, reserved=0
            return [(int(JmCmd.CALIB_QUERY), bytes(8))]

        # ---- PID 自整定(0xA0): 返回 8 字节 ACK(status=OK) ----
        if cmd == int(JmCmd.PID_AUTOTUNE):
            # status=0(OK), fail_reason=0, ring_select_done=0, reserved×5=0
            return [(int(JmCmd.PID_AUTOTUNE), bytes(8))]
        if cmd == int(JmCmd.PID_SOURCE_SET) and len(payload) >= 2:
            ring, source = payload[0], payload[1]
            if ring > 2 or source > 3:
                return [self._nack(cmd, int(JmErr.OUT_OF_RANGE))]
            if self._top_fsm() not in (int(TopFsm.IDLE), int(TopFsm.READY)):
                return [self._nack(cmd, int(JmErr.STATE_DENY))]
            self._pid_sources[ring] = source
            return [self._ack(cmd)]
        if cmd == int(JmCmd.PID_SOURCE_GET):
            return [(cmd, bytes(self._pid_sources))]
        if cmd == int(JmCmd.PID_PARAM_SET) and len(payload) >= 6:
            ring, ptype = payload[0], payload[1]
            if ring > 3 or self._pid_sources[0 if ring < 2 else ring - 1] != 3:
                return [self._nack(cmd, int(JmErr.STATE_DENY))]
            if ptype == 0:
                count = payload[2]
                if len(payload) != 3 + count * 5:
                    return [self._nack(cmd, int(JmErr.LENGTH))]
                for i in range(count):
                    typ = payload[3 + i * 5]
                    self._pid_profiles[ring][typ] = struct.unpack_from('<f', payload, 4 + i * 5)[0] if typ != 7 else struct.unpack_from('<I', payload, 4 + i * 5)[0]
            else:
                self._pid_profiles[ring][ptype] = struct.unpack_from('<f', payload, 2)[0] if ptype != 7 else struct.unpack_from('<I', payload, 2)[0]
            return [self._ack(cmd)]
        if cmd == int(JmCmd.PID_PARAM_GET) and len(payload) >= 2:
            ring, ptype = payload[0], payload[1]
            value = self._pid_profiles.get(ring, {}).get(ptype, 0.0)
            raw = struct.pack('<I', int(value)) if ptype == 7 else struct.pack('<f', float(value))
            return [(cmd, bytes([ring, ptype]) + raw)]
        if cmd == int(JmCmd.TRACE_CONFIG):
            if len(payload) != 13:
                return [self._nack(cmd, int(JmErr.LENGTH))]
            enable = payload[0]
            session, mask, rate = struct.unpack_from('<HII', payload, 1)
            packet = payload[11]
            if enable:
                channels = (mask & 0xFF).bit_count()
                if (enable != 1 or mask == 0 or mask & ~0xFF or
                        channels == 0 or rate == 0 or not 1 <= packet <= 8):
                    return [self._nack(cmd, int(JmErr.OUT_OF_RANGE))]
                self._trace_session = session
                self._trace_mask = mask
                self._trace_rate = min(rate, 5000)
                self._trace_packet_samples = packet
                self._trace_sequence = 0
                self._trace_sample_index = 0
                return [(cmd, codec.wr_u16(session) + codec.wr_u32(self._trace_rate) +
                         bytes([packet]) + codec.wr_u16(128))]
            self._trace_mask = 0
            self._trace_packet_samples = 0
            self._trace_rate = 0
            return [(cmd, b'')]

        # ---- 参数读写 ----
        if cmd == int(JmCmd.PARAM_READ):
            return [self._param_read_frame(payload)]
        if cmd == int(JmCmd.PARAM_WRITE):
            return [self._param_write_frame(payload)]
        if cmd in (int(JmCmd.PARAM_SAVE), int(JmCmd.PARAM_RESET)):
            return [self._ack(cmd)]

        # ---- 电机配置读写 (0xE6~0xEB, motor_info) ----
        if cmd == int(JmCmd.MOTOR_INFO_READ):
            return [self._motor_info_read_frame(payload)]
        if cmd == int(JmCmd.MOTOR_INFO_WRITE):
            return [self._motor_info_write_frame(payload)]
        if cmd in (int(JmCmd.MOTOR_INFO_SAVE), int(JmCmd.MOTOR_INFO_RESET),
                   int(JmCmd.MOTOR_INFO_RECALIB_RESET)):
            # 0xEC (RECALIB_RESET): 虚拟引擎不维护 is_calibrated/编码器字段语义,
            # 仅回 ACK 让上位机烟雾测试可通过; 真实语义由下位机 motor_info_calib.c 实现
            return [self._ack(cmd)]

        # 未知命令返回 NACK(UNSUPPORTED), 避免掩盖上位机 bug
        return [self._nack(cmd, int(JmErr.UNSUPPORTED))]

    # ================= 周期回调 =================
    @staticmethod
    def _plant_h(test_mode: int, freq: float) -> complex:
        """按测试模式合成传递函数 H(jω), 供辨识/验证两个方向做端到端验证。

        辨识模式(3/6)输入为实测通道, H 即被辨识对象本身; 机械模式0/1
        输入为目标电流(双胎电流环按理想单位增益近似, H 同机械对象)。
        - mode 0/1/6(辨识·机械对象): 一阶 1/(J·s+B), 转折 8Hz
        - mode 3(辨识·位置对象): 纯积分器 1/s(实际速度→实际位置)
        - mode 2(验证·电流环): 一阶闭环 200Hz
        - mode 4(验证·速度环): 一阶闭环 30Hz
        - mode 5(验证·位置环): 二阶闭环 ωn=2π·8Hz, ζ=0.7
        """
        w = 2.0 * math.pi * freq
        if test_mode in (0, 1, 6):
            return 1.0 / (1.0 + 1j * freq / 8.0)
        if test_mode == 3:
            return 1.0 / (1j * w)
        if test_mode == 2:
            return 1.0 / (1.0 + 1j * freq / 200.0)
        if test_mode == 4:
            return 1.0 / (1.0 + 1j * freq / 30.0)
        # mode 5: 标准二阶闭环
        wn = 2.0 * math.pi * 8.0
        jw = 1j * w
        return wn * wn / (jw * jw + 2.0 * 0.7 * wn * jw + wn * wn)

    def on_tick(self, live_due=True, trace_due=True):
        """周期回调；LIVE/TRACE 是否到期由调用方分别调度。"""
        frames = []
        if live_due and self.tlm_enabled and self.tlm_mask:
            frames.append(self._telemetry_frame(self.tlm_mask))
        if trace_due and self._sweep_active:
            freq = self._sweep_freqs_hz[self._sweep_point]
            rate = max(1, self._trace_rate)
            # 每频点按“整数周期”生成测量段样本, 保证上位机同步解调有完整周期、
            # 且 phase_inc/f_actual 元数据与生成频率一致(避免频谱泄漏导致 SNR 偏低)。
            # 周期策略与固件 sweep_measure_cycles 对齐: clamp(2*freq, 1, 8),
            # 低频单周期, 高频逐渐过渡到 8 周期。
            if self._sweep_point_total <= 0:
                cycles = max(1, min(8, int(round(2.0 * freq))))
                total = max(rate // 10, int(round(cycles * rate / freq)))
                f_gen = cycles * rate / total
                self._sweep_point_total = total
                self._sweep_point_fgen = f_gen
                self._sweep_point_phase_inc = int(round(f_gen * 4294967296.0 / rate))
                self._sweep_point_f_mhz = int(round(f_gen * 1000.0))
            f_gen = self._sweep_point_fgen
            count = min(self._trace_packet_samples,
                        self._sweep_point_total - self._sweep_point_sample)
            h = self._plant_h(self._sweep_test_mode, freq)
            gain = abs(h)
            phase = math.atan2(h.imag, h.real)
            rows = []
            for i in range(count):
                n = self._sweep_point_sample + i
                x = self._sweep_bias + math.sin(2.0 * math.pi * f_gen * n / rate)
                y = gain * math.sin(2.0 * math.pi * f_gen * n / rate + phase)
                rows.append([x, y])
            flags = 0x08
            if self._sweep_point_sample == 0:
                flags |= 0x10
            if self._sweep_point_sample + count >= self._sweep_point_total:
                flags |= 0x20
            if flags & 0x20 and self._sweep_point == len(self._sweep_freqs_hz) - 1:
                flags |= 0x04
            out = bytearray(codec.wr_u16(self._trace_session))
            out += codec.wr_u32(self._trace_sequence)
            out += codec.wr_u32(self._trace_sample_index)
            out += codec.wr_u32(rate) + codec.wr_u32(self._trace_mask)
            out += bytes([count, 2, flags]) + codec.wr_u32(0)
            out += codec.wr_u16(self._sweep_point)
            out += codec.wr_u32(self._sweep_point_phase_inc)
            out += codec.wr_u32(self._sweep_point_f_mhz)
            for row in rows:
                out += codec.wr_f32(row[0]) + codec.wr_f32(row[1])
            self._trace_sequence = (self._trace_sequence + 1) & 0xFFFFFFFF
            self._trace_sample_index += count
            self._sweep_point_sample += count
            if flags & 0x20:
                if flags & 0x04:
                    self._sweep_active = False
                    self._trace_mask = 0
                    self._trace_packet_samples = 0
                else:
                    self._sweep_point += 1
                    self._sweep_point_sample = 0
                    self._sweep_point_total = 0
            frames.append((int(JmCmd.TRACE_DATA), bytes(out)))
        elif trace_due and self._trace_mask and self._trace_packet_samples:
            rows = []
            for i in range(self._trace_packet_samples):
                t = self._tlm()
                vals = [t.get('id_ref', 0.0), t.get('id', 0.0),
                        t.get('iq_ref', 0.0), t.get('iq', 0.0),
                        t.get('vel_ref', 0.0), t.get('vel', 0.0),
                        t.get('pos_ref', 0.0), t.get('pos', 0.0)]
                rows.append([vals[j] for j in range(8) if self._trace_mask & (1 << j)])
            out = bytearray(codec.wr_u16(self._trace_session))
            out += codec.wr_u32(self._trace_sequence)
            out += codec.wr_u32(self._trace_sample_index)
            out += codec.wr_u32(self._trace_rate) + codec.wr_u32(self._trace_mask)
            out += bytes([self._trace_packet_samples,
                          (self._trace_mask & 0xFF).bit_count(), 0])
            out += codec.wr_u32(0)
            for row in rows:
                for value in row:
                    out += codec.wr_f32(value)
            self._trace_sequence = (self._trace_sequence + 1) & 0xFFFFFFFF
            self._trace_sample_index += self._trace_packet_samples
            frames.append((int(JmCmd.TRACE_DATA), bytes(out)))
        return frames

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
        elif cmd == int(JmCmd.PASSIVE_TORQUE):
            # 被动恒转矩(对拖台 0x60): t_set 为制动转矩幅值, 方向运行时跟随转速
            mc.set_mode = ControlMode.PASSIVE_TORQUE
            mc.set_load_torque = pick(v, 't_set', 'torque')
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
        return (int(JmCmd.READ_STATE),
                bytes([self._top_fsm() & 0xFF, self._run_state() & 0xFF,
                       self._ctrl_mode_jmcmd() & 0xFF,
                       1 if self._top_fsm() in (int(TopFsm.READY), int(TopFsm.RUN)) else 0]))

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
                          1 if self._top_fsm() in (int(TopFsm.READY), int(TopFsm.RUN)) else 0])
            trailing_groups = (
                JmTlmBit.DEBUG | JmTlmBit.CURRENT_TARGET | JmTlmBit.MOTION_TARGET)
            if not (mask & trailing_groups):
                out += bytes([int(t.get('run_state', 0)) & 0xFF])
        if mask & JmTlmBit.CURRENT_TARGET:
            out += struct.pack(
                '<ff',
                t.get('id_ref', getattr(self.engine.fsm, 'target_id', 0.0)),
                t.get('iq_ref', getattr(self.engine.fsm, 'target_iq', 0.0)),
            )
        if mask & JmTlmBit.MOTION_TARGET:
            out += struct.pack(
                '<ff',
                t.get('vel_ref', getattr(self.engine.fsm, 'target_vel', 0.0)),
                t.get('pos_ref', getattr(self.engine.fsm, 'target_pos', 0.0)),
            )
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

    # ---- 电机配置(motor_info)读写: 0xE6/0xE7, 值固定4字节 ----
    def _motor_info_read_frame(self, payload: bytes):
        if len(payload) < 2:
            return self._nack(int(JmCmd.MOTOR_INFO_READ), int(JmErr.LENGTH))
        pid = codec.rd_u16(payload, 0)
        spec = self.reg.get_motor_config_param(pid)
        if spec is None:
            return self._nack(int(JmCmd.MOTOR_INFO_READ), int(JmErr.BAD_PARAM_ID))
        value = self._motor_config_value(spec)
        ptype = self._PTYPE.get(spec.dtype, int(JmParamType.F32))
        vbytes = codec.pack_value(spec.dtype, value)
        # motor_info 帧固定 4 字节值
        vbytes = vbytes[:4].ljust(4, b'\x00')
        return (int(JmCmd.MOTOR_INFO_READ), codec.wr_u16(pid) + bytes([ptype]) + vbytes)

    def _motor_info_write_frame(self, payload: bytes):
        if len(payload) < 3:
            return self._nack(int(JmCmd.MOTOR_INFO_WRITE), int(JmErr.LENGTH))
        pid = codec.rd_u16(payload, 0)
        spec = self.reg.get_motor_config_param(pid)
        if spec is None:
            return self._nack(int(JmCmd.MOTOR_INFO_WRITE), int(JmErr.BAD_PARAM_ID))
        val = codec.unpack_value(spec.dtype, payload[2:6])
        # 优先写入 MotorParam 字段(使仿真即时生效), 否则存额外字典
        if not self._set_param_field(spec.code_name, val):
            self._motor_config_store[pid] = val
        else:
            self.engine.reload_params()
        return (int(JmCmd.MOTOR_INFO_WRITE), codec.wr_u16(pid) + bytes([int(JmErr.OK)]))

    def _motor_config_value(self, spec):
        """取电机配置参数当前值: 优先 MotorParam 字段, 其次已写存储, 最后默认值。"""
        val = self._get_param_field(spec.code_name)
        if val is not None:
            return val
        if spec.param_id in self._motor_config_store:
            return self._motor_config_store[spec.param_id]
        # 解析 CSV DefaultValue (registry 加载时未单独存储, 从 desc "默认: x" 提取)
        if spec.desc and spec.desc.startswith('默认: '):
            default_str = spec.desc.split(';')[0][len('默认: '):].strip()
            try:
                if codec.is_float_type(spec.dtype):
                    return float(default_str)
                return int(default_str, 0)
            except (ValueError, TypeError):
                pass
        # 兜底: 与 _param_value 一致的策略
        if codec.is_float_type(spec.dtype):
            return round(0.1 * (spec.param_id % 10) + 0.01 * spec.param_id, 4)
        return spec.param_id % 100

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

    def _resolve_field_name(self, code_name: str) -> str:
        """将 motor_info.csv 的 VariableName 转换为 MotorParam 字段名。"""
        return self._CSV_NAME_ALIASES.get(code_name, code_name)

    def _get_param_field(self, code_name: str):
        """从 MotorParam 的嵌套 dataclass 中按 code_name 查找字段值。"""
        if not code_name:
            return None
        field_name = self._resolve_field_name(code_name)
        mp = self.engine.mp
        # 顶层字段
        if hasattr(mp, field_name):
            return getattr(mp, field_name)
        # 遍历子 dataclass
        for fname in mp.__dataclass_fields__:
            sub = getattr(mp, fname)
            if hasattr(sub, '__dataclass_fields__') and hasattr(sub, field_name):
                return getattr(sub, field_name)
        return None

    def _set_param_field(self, code_name: str, value) -> bool:
        """写入 MotorParam 的嵌套字段, 成功返回 True。"""
        if not code_name or value is None:
            return False
        field_name = self._resolve_field_name(code_name)
        mp = self.engine.mp
        if hasattr(mp, field_name):
            setattr(mp, field_name, value)
            return True
        for fname in mp.__dataclass_fields__:
            sub = getattr(mp, fname)
            if hasattr(sub, '__dataclass_fields__') and hasattr(sub, field_name):
                setattr(sub, field_name, value)
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
