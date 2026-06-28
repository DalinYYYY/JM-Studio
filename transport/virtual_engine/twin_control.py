"""数字孪生控制算法: 复刻固件 pid_core / foc_core / cascade_control / ctrl_transition。

本模块是孪生的"控制同源"核心——Python 重写但与固件 C 实现严格对齐:
  - PidController:  复刻 pid_core.c (P + I抗饱和 + D微分先行 + 增量式 + 限幅 + 滤波)
  - PidProfile:     复刻 pid_profile.h (从 motor_param 加载各环路 PID 参数)
  - FocController:  复刻 foc_core.c (Clarke→Park→PI→反Park→SVPWM 6扇区)
  - CascadeController: 复刻 cascade_control.c (位置→速度→电流参考, 无扰预装载)
  - Transition:     复刻 ctrl_transition.c (同量纲线性混合, 异量纲直接切)

数据流(每个电流环周期):
  motor_ref(来自状态机) → CascadeController → (id_ref, iq_ref)
  → FocController(电流环 PI + SVPWM) → (ud, uq) → MotorPhysics

位置环按位置节拍执行, 速度环/电流环按电流环节拍执行(级联分频)。
"""

import math
from dataclasses import dataclass, field
from enum import IntEnum

from .twin_config import MotorParam


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


# ==================== PID 标志位 (对齐 pid_core.h) ====================
PID_FLAG_NONE = 0
PID_FLAG_ANTI_WINDUP = 1 << 0          # 抗积分饱和
PID_FLAG_DIFF_ON_MEASUREMENT = 1 << 1  # 微分先行(对测量值微分)
PID_FLAG_INCREMENTAL = 1 << 2          # 增量式PID
PID_FLAG_OUTPUT_FILTER = 1 << 3        # 输出滤波


@dataclass
class PIDParam:
    """PID 参数 (对齐 pid_core.h:pid_param_t)。"""
    kp: float = 0.0
    ki: float = 0.0
    kd: float = 0.0
    output_limit: float = 1e9          # 输出限幅
    integral_limit: float = 1e9         # 积分限幅
    output_filter_alpha: float = 1.0   # 输出滤波系数(1.0=不滤波)
    flags: int = PID_FLAG_ANTI_WINDUP   # 默认启用抗饱和


@dataclass
class PIDState:
    """PID 运行时状态 (对齐 pid_core.h:pid_state_t)。"""
    integral: float = 0.0
    prev_error: float = 0.0
    prev_output: float = 0.0
    prev_actual: float = 0.0


class PidController:
    """PID 控制器, 忠实复刻 pid_core.c:pid_core_calculate_with_ff。"""

    @staticmethod
    def calculate(state: PIDState, param: PIDParam,
                  target: float, actual: float, feedforward: float, dt: float) -> float:
        error = target - actual
        output = feedforward

        # 比例项
        output += param.kp * error

        # 积分项(带抗积分饱和)
        if param.ki != 0.0 and dt > 0.0:
            enable_integral = True
            if (param.flags & PID_FLAG_ANTI_WINDUP) and abs(state.prev_output) >= param.output_limit:
                enable_integral = False
            if enable_integral:
                state.integral += param.ki * error * dt
                state.integral = _clamp(state.integral, -param.integral_limit, param.integral_limit)
            output += state.integral

        # 微分项
        if param.kd != 0.0 and dt > 0.0:
            if param.flags & PID_FLAG_DIFF_ON_MEASUREMENT:
                # 微分先行: 对实际值微分, 避免目标阶跃冲击
                d_term = param.kd * (state.prev_actual - actual) / dt
            else:
                d_term = param.kd * (error - state.prev_error) / dt
            output += d_term

        # 增量式PID
        if param.flags & PID_FLAG_INCREMENTAL:
            output += state.prev_output

        # 输出限幅
        output = _clamp(output, -param.output_limit, param.output_limit)

        # 输出滤波
        if param.flags & PID_FLAG_OUTPUT_FILTER:
            output = (param.output_filter_alpha * output
                      + (1.0 - param.output_filter_alpha) * state.prev_output)

        # 更新状态
        state.prev_error = error
        state.prev_actual = actual
        state.prev_output = output
        return output

    @staticmethod
    def reset(state: PIDState):
        state.integral = 0.0
        state.prev_error = 0.0
        state.prev_output = 0.0
        state.prev_actual = 0.0

    @staticmethod
    def preload(state: PIDState, output_now: float, actual: float):
        """无扰预装载: 令入环瞬间输出≈output_now。

        复刻 pid_profile_preload: 设积分使 P 项+积分 = output_now。
        对 PI(kd=0): integral = output_now - kp*(target-actual) ≈ output_now (因 target≈actual 时)
        """
        state.integral = output_now
        state.prev_output = output_now
        state.prev_actual = actual
        state.prev_error = 0.0


# ==================== PID Profile (对齐 pid_profile.h) ====================
class PidProfileID(IntEnum):
    CURRENT_D = 0
    CURRENT_Q = 1
    VELOCITY = 2
    POSITION = 3
    IMPEDANCE = 4
    HOMING = 5
    JOG = 6
    TEST = 7


class PidProfile:
    """PID 参数配置文件管理, 复刻 pid_profile.c:pid_profile_load_from_motor_param。"""

    def __init__(self, mp: MotorParam):
        self.mp = mp
        self.profiles: dict[PidProfileID, PIDParam] = {}
        self._load_from_motor_param()

    def reload(self):
        """重新从 MotorParam 加载各环路 PID 参数(参数写入后实时生效)。

        仅重建 PIDParam 副本(kp/ki/kd/限幅), 不触碰运行时积分状态(PIDState),
        故可在运行中安全调用。
        """
        self._load_from_motor_param()

    def _load_from_motor_param(self):
        mp = self.mp
        cl = mp.current_loop
        pl = mp.position_loop
        ic = mp.impedance_ctrl
        mb = mp.motor_base

        # 电压限幅(SVPWM线性区): Vmax = vbus · pwm_max_duty / √3
        vmax = mb.rated_voltage * cl.pwm_max_duty / math.sqrt(3.0)

        # d轴电流环: PI, 输出=ud, 限幅=电压
        self.profiles[PidProfileID.CURRENT_D] = PIDParam(
            kp=cl.current_kp_d, ki=cl.current_ki_d,
            output_limit=vmax, integral_limit=cl.current_integral_limit,
            flags=PID_FLAG_ANTI_WINDUP)

        # q轴电流环: PI, 输出=uq, 限幅=电压
        self.profiles[PidProfileID.CURRENT_Q] = PIDParam(
            kp=cl.current_kp_q, ki=cl.current_ki_q,
            output_limit=vmax, integral_limit=cl.current_integral_limit,
            flags=PID_FLAG_ANTI_WINDUP)

        # 速度环: PI, 输出=iq参考, 限幅=峰值电流
        self.profiles[PidProfileID.VELOCITY] = PIDParam(
            kp=pl.speed_kp, ki=pl.speed_ki,
            output_limit=mb.peak_current, integral_limit=pl.speed_integral_limit,
            flags=PID_FLAG_ANTI_WINDUP | PID_FLAG_DIFF_ON_MEASUREMENT)

        # 位置环: P(比例), 输出=速度设定, 限幅=最大转速
        # position_kp 单位 Hz, 输出 = kp * error (rad) = rad/s
        self.profiles[PidProfileID.POSITION] = PIDParam(
            kp=pl.position_kp, ki=0.0, kd=0.0,
            output_limit=mb.max_speed, integral_limit=pl.position_integral_limit,
            flags=PID_FLAG_NONE)

        # 阻抗控制: PD, 输出=力矩, 限幅=iq_max·kt
        self.profiles[PidProfileID.IMPEDANCE] = PIDParam(
            kp=ic.impedance_kp, ki=0.0, kd=ic.impedance_kd,
            output_limit=ic.iq_max * mb.kt, integral_limit=0.0,
            flags=PID_FLAG_NONE)

        # 回零/点动/测试: 暂复用速度环参数
        for pid in (PidProfileID.HOMING, PidProfileID.JOG, PidProfileID.TEST):
            self.profiles[pid] = PIDParam(
                kp=pl.speed_kp, ki=pl.speed_ki,
                output_limit=mb.peak_current, integral_limit=pl.speed_integral_limit,
                flags=PID_FLAG_ANTI_WINDUP)

    def get(self, pid: PidProfileID) -> PIDParam:
        return self.profiles.get(pid, self.profiles[PidProfileID.VELOCITY])


# ==================== 参考控制类型 (对齐 motor_control.h) ====================
class RefCtrlType(IntEnum):
    IDLE = 0
    VOLTAGE = 1
    DUTY = 2
    CURRENT = 3
    TORQUE = 4
    VELOCITY = 5
    POSITION = 6


@dataclass
class MotorRef:
    """电机控制参考输出 (对齐 motor_control.h:motor_ref_t)。

    状态机模块生成此结构, 下游三环据此决定级联入口。
    """
    ctrl_type: RefCtrlType = RefCtrlType.IDLE
    pos: float = 0.0           # 目标位置 rad
    vel: float = 0.0           # 目标速度 rad/s
    torque: float = 0.0        # 目标力矩 Nm
    id: float = 0.0             # 目标d轴电流 A
    iq: float = 0.0             # 目标q轴电流 A
    kp: float = 0.0             # MIT刚度
    kd: float = 0.0             # MIT阻尼
    torque_ff: float = 0.0       # 力矩前馈 Nm
    vel_ff: float = 0.0         # 速度前馈 rad/s
    voltage: float = 0.0        # 开环q轴电压 V
    duty: float = 0.0           # 占空比 -1~1
    pos_profile: PidProfileID = PidProfileID.POSITION
    vel_profile: PidProfileID = PidProfileID.VELOCITY


# ==================== FOC (复刻 foc_core.c) ====================
_SQRT3_BY_2 = math.sqrt(3.0) / 2.0


@dataclass
class FocState:
    """FOC 运行时状态。"""
    theta_e: float = 0.0       # 电角度 rad
    ialpha: float = 0.0        # α轴电流
    ibeta: float = 0.0         # β轴电流
    id: float = 0.0            # d轴电流
    iq: float = 0.0            # q轴电流
    ud: float = 0.0            # d轴电压
    uq: float = 0.0            # q轴电压
    ualpha: float = 0.0       # α轴电压
    ubeta: float = 0.0        # β轴电压
    sector: int = 0           # SVPWM扇区
    ta: float = 0.0           # A相占空比
    tb: float = 0.0           # B相占空比
    tc: float = 0.0           # C相占空比


class FocController:
    """FOC 控制器, 复刻 foc_core.c (Clarke/Park/反Park/SVPWM)。

    在孪生中, FOC 的职责:
      1. 接收物理模型的 id/iq(经 Park 变换后的实测值)
      2. 用 PI 闭环跟踪 id_ref/iq_ref, 输出 ud/uq
      3. SVPWM 限幅(保证电压不超出线性区)
      4. 输出 ud/uq 给物理模型
    """

    def __init__(self, profile: PidProfile, dt: float):
        self.profile = profile
        self.dt = dt
        self.pid_id = PIDState()
        self.pid_iq = PIDState()
        self.foc = FocState()
        self._filt_alpha = 0.8  # dq电流滤波(对齐 foc_core.c park_transfer)
        self._prev_id = 0.0
        self._prev_iq = 0.0

    def reset(self):
        PidController.reset(self.pid_id)
        PidController.reset(self.pid_iq)
        self.foc = FocState()
        self._prev_id = 0.0
        self._prev_iq = 0.0

    def run(self, id_ref: float, iq_ref: float,
            id_meas: float, iq_meas: float, theta_e: float) -> tuple:
        """运行一次电流环。

        Args:
            id_ref, iq_ref: dq轴电流参考 A
            id_meas, iq_meas: dq轴电流实测 A (来自物理模型, 经 Clarke/Park)
            theta_e: 电角度 rad
        Returns:
            (ud, uq): dq轴电压命令 V
        """
        f = self.foc
        f.theta_e = theta_e

        # Park 变换的电流(这里 id_meas/iq_meas 已是 dq 域, 模拟滤波)
        f.id = self._filt_alpha * id_meas + (1.0 - self._filt_alpha) * self._prev_id
        f.iq = self._filt_alpha * iq_meas + (1.0 - self._filt_alpha) * self._prev_iq
        self._prev_id = f.id
        self._prev_iq = f.iq

        # dq 轴 PI (pid_profile_calculate)
        p_d = self.profile.get(PidProfileID.CURRENT_D)
        p_q = self.profile.get(PidProfileID.CURRENT_Q)
        f.ud = PidController.calculate(self.pid_id, p_d, id_ref, f.id, 0.0, self.dt)
        f.uq = PidController.calculate(self.pid_iq, p_q, iq_ref, f.iq, 0.0, self.dt)

        # SVPWM (限幅 + 占空比计算)
        self._svpwm(f.ud, f.uq)

        return f.ud, f.uq

    def _svpwm(self, ud: float, uq: float):
        """SVPWM 6扇区, 复刻 foc_core.c:foc_svpwm。

        这里主要起电压限幅作用: 当 ud/uq 超出线性区时按六边形限幅,
        并计算占空比 ta/tb/tc (供显示与 PWM 量化)。
        """
        f = self.foc
        # 反Park: αβ = [cos -sin; sin cos] · [ud; uq]
        sin_t = math.sin(f.theta_e)
        cos_t = math.cos(f.theta_e)
        f.ualpha = ud * cos_t - uq * sin_t
        f.ubeta = ud * sin_t + uq * cos_t

        Ts = 1.0  # 归一化周期
        u_alpha = f.ualpha
        u_beta = f.ubeta

        # 扇区判断
        u1 = u_beta
        u2 = _SQRT3_BY_2 * u_alpha - 0.5 * u_beta
        u3 = -_SQRT3_BY_2 * u_alpha - 0.5 * u_beta
        f.sector = ((1 if u1 > 0 else 0)
                    | (2 if u2 > 0 else 0)
                    | (4 if u3 > 0 else 0))

        # 按扇区计算基本矢量作用时间(对齐 foc_core.c switch)
        ta = tb = tc = 0.0
        if f.sector == 3:      # t4, t6
            t4 = u2
            t6 = u1
            s = t4 + t6
            if s > Ts:
                k = Ts / s
                t4 *= k
                t6 *= k
            t0 = (Ts - t4 - t6) / 2
            ta = t4 + t6 + t0
            tb = t6 + t0
            tc = t0
        elif f.sector == 1:   # t2, t6
            t6 = -u3
            t2 = -u2
            s = t2 + t6
            if s > Ts:
                k = Ts / s
                t2 *= k
                t6 *= k
            t0 = (Ts - t2 - t6) / 2
            ta = t6 + t0
            tb = t2 + t6 + t0
            tc = t0
        elif f.sector == 5:   # t2, t3
            t2 = u1
            t3 = u3
            s = t2 + t3
            if s > Ts:
                k = Ts / s
                t2 *= k
                t3 *= k
            t0 = (Ts - t2 - t3) / 2
            ta = t0
            tb = t2 + t3 + t0
            tc = t3 + t0
        elif f.sector == 4:   # t1, t3
            t3 = -u2
            t1 = -u1
            s = t1 + t3
            if s > Ts:
                k = Ts / s
                t1 *= k
                t3 *= k
            t0 = (Ts - t1 - t3) / 2
            ta = t0
            tb = t3 + t0
            tc = t1 + t3 + t0
        elif f.sector == 6:   # t1, t5
            t1 = u3
            t5 = u2
            s = t1 + t5
            if s > Ts:
                k = Ts / s
                t1 *= k
                t5 *= k
            t0 = (Ts - t1 - t5) / 2
            ta = t5 + t0
            tb = t0
            tc = t1 + t5 + t0
        elif f.sector == 2:   # t4, t5
            t5 = -u1
            t4 = -u3
            s = t4 + t5
            if s > Ts:
                k = Ts / s
                t4 *= k
                t5 *= k
            t0 = (Ts - t4 - t5) / 2
            ta = t4 + t5 + t0
            tb = t0
            tc = t5 + t0

        f.ta = _clamp(ta, 0.0, 1.0)
        f.tb = _clamp(tb, 0.0, 1.0)
        f.tc = _clamp(tc, 0.0, 1.0)


# ==================== 级联控制 (复刻 cascade_control.c) ====================
@dataclass
class CascadeFeedback:
    """级联控制反馈输入 (对齐 cascade_control.h:cascade_fb_t)。"""
    pos: float = 0.0    # 实际位置 rad
    vel: float = 0.0    # 实际速度 rad/s
    id: float = 0.0     # 实际d轴电流 A
    iq: float = 0.0    # 实际q轴电流 A


@dataclass
class CascadeOutput:
    """级联控制输出 (对齐 cascade_control.h:cascade_out_t)。"""
    id_ref: float = 0.0
    iq_ref: float = 0.0


class CascadeController:
    """三环级联控制器, 复刻 cascade_control.c。

    环路结构: 位置环 → vel_sp → 速度环 → iq_ref → [电流环]
    入环层级由 ref.ctrl_type 决定, 切换时做无扰预装载。
    """

    def __init__(self, mp: MotorParam, profile: PidProfile, dt_pos: float, dt_vel: float):
        self.mp = mp
        self.profile = profile
        self.dt_pos = dt_pos
        self.dt_vel = dt_vel
        self.pid_pos = PIDState()
        self.pid_vel = PIDState()
        self.vel_setpoint = 0.0
        self.last_ctrl_type = RefCtrlType.IDLE
        self.last_pos_profile = PidProfileID.POSITION
        self.last_vel_profile = PidProfileID.VELOCITY

    def reset(self):
        PidController.reset(self.pid_pos)
        PidController.reset(self.pid_vel)
        self.vel_setpoint = 0.0
        self.last_ctrl_type = RefCtrlType.IDLE

    def run_position(self, ref: MotorRef, fb: CascadeFeedback):
        """位置环: 位置误差 → 速度设定。复刻 cascade_control_run_position。"""
        if ref.ctrl_type != RefCtrlType.POSITION:
            return

        p_pos = self.profile.get(ref.pos_profile)
        vel_sp = PidController.calculate(self.pid_pos, p_pos, ref.pos, fb.pos, 0.0, self.dt_pos)
        # 叠加速度前馈
        vel_sp += ref.vel_ff * self.mp.position_loop.velocity_ff_gain
        # 速度限幅
        vel_sp = _clamp(vel_sp, -self.mp.motor_base.max_speed,
                         self.mp.motor_base.max_speed)
        # 加速度限制(速度设定变化率限幅): 避免阶跃位置指令导致速度环积分饱和/震荡
        max_accel = self.mp.position_loop.max_accel
        if max_accel > 0.0:
            max_delta = max_accel * self.dt_pos
            delta = vel_sp - self.vel_setpoint
            if delta > max_delta:
                vel_sp = self.vel_setpoint + max_delta
            elif delta < -max_delta:
                vel_sp = self.vel_setpoint - max_delta
        self.vel_setpoint = vel_sp

    def run(self, ref: MotorRef, fb: CascadeFeedback) -> CascadeOutput:
        """速度环 + 入环分发, 输出 dq 电流参考。复刻 cascade_control_run。"""
        out = CascadeOutput()
        kt = self.mp.motor_base.kt
        peak_i = self.mp.motor_base.peak_current

        # 入环层级/配置变化: 无扰预装载
        if (ref.ctrl_type != self.last_ctrl_type or
                ref.pos_profile != self.last_pos_profile or
                ref.vel_profile != self.last_vel_profile):
            self._bumpless_preload(ref, fb)
            self.last_ctrl_type = ref.ctrl_type
            self.last_pos_profile = ref.pos_profile
            self.last_vel_profile = ref.vel_profile

        ct = ref.ctrl_type
        if ct == RefCtrlType.POSITION:
            p_vel = self.profile.get(ref.vel_profile)
            iq = PidController.calculate(self.pid_vel, p_vel,
                                          self.vel_setpoint, fb.vel, 0.0, self.dt_vel)
            out.iq_ref = _clamp(iq, -peak_i, peak_i)

        elif ct == RefCtrlType.VELOCITY:
            p_vel = self.profile.get(ref.vel_profile)
            iq_ff = (ref.torque_ff / kt) if kt > 0 else 0.0
            iq = PidController.calculate(self.pid_vel, p_vel,
                                          ref.vel, fb.vel, iq_ff, self.dt_vel)
            out.iq_ref = _clamp(iq, -peak_i, peak_i)

        elif ct == RefCtrlType.TORQUE:
            out.iq_ref = (_clamp(ref.torque / kt, -peak_i, peak_i)
                           if kt > 0 else 0.0)

        elif ct == RefCtrlType.CURRENT:
            out.id_ref = _clamp(ref.id, -peak_i, peak_i)
            out.iq_ref = _clamp(ref.iq, -peak_i, peak_i)

        elif ct == RefCtrlType.VOLTAGE:
            # 开环电压: 由电流环直通, iq_ref 不生效, FOC 直接用 ref.voltage
            pass
        elif ct == RefCtrlType.DUTY:
            pass
        # IDLE: 输出为零
        return out

    def _bumpless_preload(self, ref: MotorRef, fb: CascadeFeedback):
        """无扰预装载, 复刻 cascade_bumpless_preload。"""
        ct = ref.ctrl_type
        if ct == RefCtrlType.POSITION:
            # 位置环输出=速度设定, 用当前实际速度反推
            PidController.preload(self.pid_pos, fb.vel, fb.pos)
            # 速度环输出=iq参考, 用当前实际iq反推
            PidController.preload(self.pid_vel, fb.iq, fb.vel)
            self.vel_setpoint = fb.vel
        elif ct == RefCtrlType.VELOCITY:
            PidController.preload(self.pid_vel, fb.iq, fb.vel)
        # TORQUE/CURRENT/IDLE 无外环积分, 无需预装载


# ==================== 过渡器 (复刻 ctrl_transition.c) ====================
TRANSITION_IDLE = 0
TRANSITION_IN_PROGRESS = 1
TRANSITION_COMPLETED = 2


class Transition:
    """参考层过渡引擎, 复刻 ctrl_transition.c。

    同 ctrl_type(量纲一致): 对目标值线性混合, 输出连续无跳变;
    异 ctrl_type(量纲不同): 不混合, 直接切, 无扰交由下游预装载。
    """

    def __init__(self):
        self.state = TRANSITION_IDLE
        self.elapsed = 0
        self.duration = 0
        self.old_ref = MotorRef()
        self.ratio = 0.0

    def start(self, duration: int, old_ref: MotorRef):
        self.state = TRANSITION_IN_PROGRESS
        self.elapsed = 0
        self.duration = duration
        self.ratio = 0.0
        self.old_ref = MotorRef(**old_ref.__dict__)

    def update(self, new_ref: MotorRef) -> tuple:
        """返回 (mixed_ref, is_done)。"""
        if self.state != TRANSITION_IN_PROGRESS:
            return (new_ref, True)

        self.elapsed += 1
        if self.duration == 0 or self.elapsed >= self.duration:
            self.state = TRANSITION_COMPLETED
            self.ratio = 1.0
            return (new_ref, True)

        # 量纲不同: 直接采用新参考
        if self.old_ref.ctrl_type != new_ref.ctrl_type:
            return (new_ref, False)

        # 同量纲: 线性混合标量字段
        self.ratio = self.elapsed / self.duration
        r = self.ratio
        mixed = MotorRef(**new_ref.__dict__)
        mixed.pos = self._blend(self.old_ref.pos, new_ref.pos, r)
        mixed.vel = self._blend(self.old_ref.vel, new_ref.vel, r)
        mixed.torque = self._blend(self.old_ref.torque, new_ref.torque, r)
        mixed.id = self._blend(self.old_ref.id, new_ref.id, r)
        mixed.iq = self._blend(self.old_ref.iq, new_ref.iq, r)
        mixed.voltage = self._blend(self.old_ref.voltage, new_ref.voltage, r)
        mixed.duty = self._blend(self.old_ref.duty, new_ref.duty, r)
        return (mixed, False)

    def force_complete(self):
        self.state = TRANSITION_COMPLETED
        self.ratio = 1.0

    @staticmethod
    def _blend(old_v: float, new_v: float, ratio: float) -> float:
        return old_v * (1.0 - ratio) + new_v * ratio


# ==================== 控制核心整合 ====================
class ControllerCore:
    """整合级联控制 + 电流环(FOC), 输出 ud/uq 给物理模型。

    每个 FOC 周期调用 run_foc(); 位置环节拍调用 run_position()。
    位置环频率 < FOC 频率, 由外部按分频调用。
    """

    def __init__(self, mp: MotorParam, dt_foc: float, dt_pos_ratio: int = 5):
        """Args:
            dt_foc: 电流环周期 s (如 5e-5 = 50us)
            dt_pos_ratio: 位置环 = 电流环 × 此倍数 (如 5 → 位置环 250us)
        """
        self.mp = mp
        self.dt_foc = dt_foc
        self.dt_pos = dt_foc * dt_pos_ratio
        self.dt_vel = dt_foc  # 速度环与电流环同频

        self.profile = PidProfile(mp)
        self.cascade = CascadeController(mp, self.profile, self.dt_pos, self.dt_vel)
        self.foc = FocController(self.profile, dt_foc)

        self._pos_counter = 0
        self._pos_ratio = dt_pos_ratio

    def run(self, ref: MotorRef, fb: CascadeFeedback) -> tuple:
        """运行一个 FOC 周期: 级联 → 电流环 → ud/uq。

        Args:
            ref: 状态机输出的参考(MotorRef)
            fb: 物理模型反馈(位置/速度/电流)
        Returns:
            (ud, uq): dq轴电压命令 V
        """
        # 位置环分频执行
        self._pos_counter += 1
        if self._pos_counter >= self._pos_ratio:
            self._pos_counter = 0
            self.cascade.run_position(ref, fb)

        # 速度环 + 入环分发 → id_ref/iq_ref
        out = self.cascade.run(ref, fb)

        # 开环电压模式: 直接用 ref.voltage 作为 uq, 带 d轴解耦 + 电流软限位
        # (对齐真实驱动器: 开环模式也有电流保护; d轴解耦防止交叉耦合使 id 发散)
        if ref.ctrl_type == RefCtrlType.VOLTAGE:
            uq = self._open_loop_current_limit(ref.voltage, fb)
            ud = self._decouple_d(fb)
            return (ud, uq)
        if ref.ctrl_type == RefCtrlType.DUTY:
            vmax = self.mp.motor_base.rated_voltage * self.mp.current_loop.pwm_max_duty
            uq = self._open_loop_current_limit(ref.duty * vmax, fb)
            ud = self._decouple_d(fb)
            return (ud, uq)
        if ref.ctrl_type == RefCtrlType.IDLE:
            return (0.0, 0.0)

        # 电流环(FOC PI) → ud/uq
        ud, uq = self.foc.run(out.id_ref, out.iq_ref, fb.id, fb.iq,
                               self.mp.encoder_param.elec_angle_bias + fb.pos * self.mp.motor_base.pole_pairs)

        # TORQUE/CURRENT 模式: 无速度闭环, 空载小惯量下会持续加速超速。
        # 在 FOC 输出后施加速度软限位 (衰减 uq), 模拟真实驱动器的速度保护。
        if ref.ctrl_type in (RefCtrlType.TORQUE, RefCtrlType.CURRENT):
            uq = self._speed_soft_limit(uq, fb)

        return (ud, uq)

    def reset(self):
        self.cascade.reset()
        self.foc.reset()
        self._pos_counter = 0

    def _speed_soft_limit(self, uq_cmd: float, fb: CascadeFeedback) -> float:
        """速度软限位: 当转速接近 max_speed 时衰减 uq, 避免开环模式超速。

        soft_start (90% max_speed) → 1.0 倍输出
        soft_hard  (95% max_speed) → 0.0 倍输出 (停止加速)
        使稳态速度自然受限在 max_speed 附近, 不触发 OVER_SPEED 故障。
        """
        max_speed = self.mp.motor_base.max_speed
        soft_start = max_speed * 0.9
        soft_hard = max_speed * 0.95
        omega = abs(fb.vel)
        if omega <= soft_start:
            return uq_cmd
        if omega >= soft_hard:
            return 0.0
        ratio = 1.0 - (omega - soft_start) / (soft_hard - soft_start)
        return uq_cmd * max(0.0, ratio)

    def _open_loop_current_limit(self, uq_cmd: float, fb: CascadeFeedback) -> float:
        """开环模式(VOLTAGE/DUTY)电流+速度软限位。

        开环模式无电流环, 空载时瞬态电流冲击大且高速交叉耦合使 id 发散。
        当实测电流接近峰值时, 按比例衰减输出电压, 模拟真实驱动器的电流保护。

        速度软限位: 开环模式无速度闭环, 空载小惯量下反电动势未建立即超速。
        当转速接近 max_speed 时, 按比例衰减输出电压, 使稳态速度自然受限,
        避免触发 OVER_SPEED 故障 (模拟真实驱动器的速度保护)。
        """
        peak_i = self.mp.motor_base.peak_current
        # 1. 电流软限位: 峰值的 80% 开始衰减
        soft_limit = peak_i * 0.8
        i_mag = math.sqrt(fb.id ** 2 + fb.iq ** 2)
        if i_mag > soft_limit and i_mag > 1e-6:
            scale = soft_limit / i_mag
            uq_cmd *= scale

        # 2. 速度软限位
        uq_cmd = self._speed_soft_limit(uq_cmd, fb)
        return uq_cmd

    def _decouple_d(self, fb: CascadeFeedback) -> float:
        """d轴解耦电压: 抵消交叉耦合项 ωe·Lq·iq, 防止开环模式 id 发散。

        物理模型 dq方程: did/dt = (ud - R·id + ωe·Lq·iq) / Ld
        开环模式 ud=0 时, 交叉耦合项 ωe·Lq·iq 使 id 持续增长。
        注入 ud = R·id - ωe·Lq·iq 可抵消耦合, 使 did/dt≈0, id 趋于稳定。
        (对齐 FOC 解耦补偿: decoupling_gain=1.0)
        """
        pp = self.mp.motor_base.pole_pairs
        lq = self.mp.motor_base.lq
        r = self.mp.motor_base.r
        omega_e = pp * fb.vel
        return r * fb.id - omega_e * lq * fb.iq

    def reload_params(self):
        """重新加载控制器 PID 配置(PARAM_WRITE 后调用, 实时生效)。

        复用 PidProfile.reload(): 仅刷新增益/限幅, 保留积分状态, 可运行中调用。
        """
        self.profile.reload()
