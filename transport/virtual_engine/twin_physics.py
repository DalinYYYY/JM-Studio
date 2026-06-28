"""数字孪生物理模型: PMSM 电气+机械+热+编码器 仿真。

忠实复刻固件物理链路:
  电气(dq方程) ← 对应 foc_core 的 Park/反Park 后的电机响应
  机械(牛顿方程) ← 对应 motion_param/multiturn 的位置/速度
  热(一阶模型) ← 对应 thermal_model_t
  编码器(量化+PLL) ← 对应 encoder_param_t / motion_param PLL观测器

电机模型为表贴/内嵌永磁同步电机(PMSM/SPMSM/IPMSM), dq轴电压方程:
  ud = R·id + Ld·did/dt - ωe·Lq·iq        (d轴: 电阻+自感-交叉耦合)
  uq = R·iq + Lq·diq/dt + ωe·Ld·id + ωe·ψf  (q轴: 电阻+自感+交叉耦合+反电动势)
电磁转矩:
  Te = 1.5·p·(ψf·iq + (Ld-Lq)·id·iq)      (SPM时 Ld=Lq, 退化为 Te=kt·iq)
机械方程(折算到电机转子侧):
  J_total·dωm/dt = Te - T_load - B·ωm - Tc·sgn(ωm)
  dθm/dt = ωm

积分用前向欧拉; 步长由调用方(电流环周期)决定, 建议 ≤ 50us 保证 L/R 时间常数稳定。
"""

import math
from dataclasses import dataclass

from .twin_config import MotorParam


def _sgn(x: float) -> float:
    return 1.0 if x > 0 else (-1.0 if x < 0 else 0.0)


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


@dataclass
class PhysicsState:
    """物理仿真状态快照。"""
    # 电气(dq)
    id: float = 0.0          # d轴电流 A
    iq: float = 0.0          # q轴电流 A
    ud: float = 0.0          # d轴电压 V (上一拍控制器输出)
    uq: float = 0.0          # q轴电压 V
    # 机械(转子侧)
    theta_m: float = 0.0     # 机械角度 rad (多圈)
    omega_m: float = 0.0      # 机械角速度 rad/s
    theta_e: float = 0.0     # 电角度 rad
    # 输出端(经减速器)
    pos_out: float = 0.0      # 输出位置 rad
    vel_out: float = 0.0      # 输出速度 rad/s
    # 力矩
    torque: float = 0.0       # 电机电磁转矩 Nm
    torque_out: float = 0.0   # 输出端转矩 Nm
    # 三相(显示用, 由dq反Park重构)
    ia: float = 0.0
    ib: float = 0.0
    ic: float = 0.0
    # 母线
    vbus: float = 48.0        # V
    ibus: float = 0.0         # A
    power: float = 0.0        # W
    # 温度
    temp_motor: float = 30.0   # C
    temp_fet: float = 30.0     # C
    # 编码器量化后读数(含噪声)
    enc_theta: float = 0.0     # 量化后机械角 rad
    enc_vel: float = 0.0       # PLL观测速度 rad/s
    enc_count: int = 0        # 编码器原始计数
    multiturn: int = 0         # 多圈圈数
    single_turn: float = 0.0   # 单圈角度 rad


class MotorPhysics:
    """PMSM 物理仿真引擎。

    调用方在每个电流环周期调用 step(dt, ud, uq, t_load),
    内部积分电气/机械/热/编码器, 更新 state。
    """

    def __init__(self, param: MotorParam):
        self.p = param
        mb = param.motor_base
        gb = param.gearbox_param
        en = param.encoder_param
        th = param.thermal_model
        pl = param.position_loop

        # 缓存常用参数(避免每次属性链查找)
        self.R = mb.r
        self.Ld = mb.ld
        self.Lq = mb.lq
        self.flux = mb.flux
        self.kt = mb.kt
        self.pp = int(mb.pole_pairs)
        self.J_rotor = mb.inertia
        self.J_total = self.J_rotor  # 负载惯量由 set_load_inertia 折算后累加
        self.gear_ratio = max(gb.gear_ratio, 1.0)
        self.gear_eff = _clamp(gb.gear_efficiency, 0.1, 1.0)
        self.backlash = gb.gear_backlash

        # 摩擦(从 position_loop 取)
        self.Tc = pl.friction_coulomb      # 库仑摩擦 Nm
        self.B = pl.friction_viscous       # 粘滞摩擦 Nm/(rad/s)

        # 热模型
        self.Rth = th.thermal_resistance   # K/W
        self.tau_th = max(th.thermal_time_const, 1.0)
        self.T_amb = 30.0                   # 环境温度 C
        self.derating_start = th.derating_temp_start

        # 编码器
        self.enc_lines = max(int(en.enc_lines), 1)
        self.enc_res = 2.0 * math.pi / self.enc_lines  # 每个计数对应 rad
        self.enc_dir = en.enc_direction if en.enc_direction != 0 else 1
        self.elec_bias = en.elec_angle_bias
        self.pos_alpha = en.pos_filter_alpha
        self.pll_gain = en.speed_obs_gain

        # 母线
        self.vbus_nominal = mb.rated_voltage
        self.vbus_internal = mb.rated_voltage

        # 限位
        self.pos_min = param.position_limit.pos_min_limit
        self.pos_max = param.position_limit.pos_max_limit

        # 状态
        self.s = PhysicsState(vbus=self.vbus_nominal, temp_motor=self.T_amb, temp_fet=self.T_amb)
        self._t = 0.0

        # 编码器 PLL 观测器状态(复刻 motion_param VEL_METHOD_PLL)
        self._pll_theta = 0.0     # 观测角度
        self._pll_omega = 0.0     # 观测速度
        self._pll_integral = 0.0   # 积分项

        # 位置滤波
        self._pos_filt = 0.0

        # 减速器回程间隙状态
        self._backlash_pos = 0.0   # 间隙内位置

    # ================== 主步进 ==================
    def step(self, dt: float, ud: float, uq: float, t_load_motor: float = 0.0):
        """推进一个仿真步。

        Args:
            dt: 步长 s (建议 ≤ 50us, 即电流环周期)
            ud, uq: dq轴电压命令 V (来自电流环 PI 输出)
            t_load_motor: 折算到电机轴的负载力矩 Nm (正值阻碍运动)
        """
        s = self.s
        s.ud = ud
        s.uq = uq
        omega_e = self.pp * s.omega_m  # 电角速度 rad/s

        # ---- 1. 电气: dq轴电流积分(前向欧拉) ----
        # did/dt = (ud - R·id + ωe·Lq·iq) / Ld
        # diq/dt = (uq - R·iq - ωe·Ld·id - ωe·ψf) / Lq
        Ld = max(self.Ld, 1e-7)
        Lq = max(self.Lq, 1e-7)
        did_dt = (ud - self.R * s.id + omega_e * Lq * s.iq) / Ld
        diq_dt = (uq - self.R * s.iq - omega_e * Ld * s.id - omega_e * self.flux) / Lq
        s.id += did_dt * dt
        s.iq += diq_dt * dt

        # 电流限幅(峰值电流保护)
        peak_i = self.p.motor_base.peak_current
        s.id = _clamp(s.id, -peak_i, peak_i)
        s.iq = _clamp(s.iq, -peak_i, peak_i)

        # ---- 2. 电磁转矩 ----
        # Te = 1.5·p·(ψf·iq + (Ld-Lq)·id·iq)
        # 对于 SPM(Ld≈Lq): Te ≈ 1.5·p·ψf·iq = kt·iq
        s.torque = 1.5 * self.pp * (self.flux * s.iq + (self.Ld - self.Lq) * s.id * s.iq)

        # ---- 3. 机械: 转子运动方程 ----
        # J_total·dωm/dt = Te - T_load - B·ωm - Tc·sgn(ωm)
        # 摩擦: 库仑+粘滞
        friction = self.B * s.omega_m + self.Tc * _sgn(s.omega_m)
        # 注: t_load_motor 已折算到电机轴
        net_torque = s.torque - t_load_motor - friction
        J = self.J_total  # J_rotor + 负载折算(set_load_inertia 更新)
        s.omega_m += (net_torque / max(J, 1e-8)) * dt
        s.theta_m += s.omega_m * dt
        s.theta_e = (s.theta_m * self.pp + self.elec_bias) % (2.0 * math.pi)

        # ---- 4. 减速器输出(含回程间隙) ----
        self._update_gear_output()

        # 位置限位(软件限位)
        if s.pos_out > self.pos_max:
            s.pos_out = self.pos_max
            if s.vel_out > 0:
                s.omega_m = 0.0
        elif s.pos_out < self.pos_min:
            s.pos_out = self.pos_min
            if s.vel_out < 0:
                s.omega_m = 0.0

        # ---- 5. 三相电流重构(显示用, 由dq反Park) ----
        self._reconstruct_phase_currents()

        # ---- 6. 母线 ----
        self._update_bus(dt)

        # ---- 7. 热模型 ----
        self._update_thermal(dt)

        # ---- 8. 编码器量化 + PLL速度观测 ----
        self._update_encoder(dt)

        self._t += dt

    # ================== 子模型 ==================
    def _update_gear_output(self):
        """减速器输出(含回程间隙模型)。"""
        s = self.s
        ratio = self.gear_ratio
        # 电机端 -> 输出端
        raw_pos = s.theta_m / ratio
        raw_vel = s.omega_m / ratio

        # 回程间隙: 用死区模型
        if self.backlash > 0:
            # 间隙内运动不传递到输出
            delta = raw_pos - self._backlash_pos
            if abs(delta) < self.backlash:
                # 在间隙内, 输出不跟随
                s.vel_out = 0.0
            else:
                # 越过间隙, 输出跟随
                self._backlash_pos = raw_pos - _sgn(delta) * self.backlash
                s.pos_out = self._backlash_pos
                s.vel_out = raw_vel
        else:
            s.pos_out = raw_pos
            s.vel_out = raw_vel

        # 输出转矩 = 电机转矩 × 减速比 × 效率
        s.torque_out = s.torque * ratio * self.gear_eff

    def _reconstruct_phase_currents(self):
        """由 dq 反 Park 重构三相电流(显示用)。"""
        s = self.s
        theta = s.theta_e
        sin_t = math.sin(theta)
        cos_t = math.cos(theta)
        # 反Park: αβ = [cos -sin; sin cos] · [id; iq]
        ialpha = cos_t * s.id - sin_t * s.iq
        ibeta = sin_t * s.id + cos_t * s.iq
        # 反Clarke(等幅值): ia = iα; ib = -iα/2 + √3/2·iβ; ic = -iα/2 - √3/2·iβ
        s.ia = ialpha
        s.ib = -0.5 * ialpha + (math.sqrt(3) / 2.0) * ibeta
        s.ic = -0.5 * ialpha - (math.sqrt(3) / 2.0) * ibeta

    def _update_bus(self, dt: float):
        """母线电压/电流: 含轻载跌落 + 纹波。"""
        s = self.s
        # 母线电流 = 电机功率 / 母线电压 (近似)
        mech_power = s.torque * s.omega_m
        elec_power = abs(s.ud * s.id + s.uq * s.iq)  # 输入电功率
        s.power = mech_power
        s.ibus = mech_power / max(self.vbus_internal, 1.0) + 0.05
        # 母线电压跌落(内阻 ~0.15Ω) + 轻微纹波
        target_v = self.vbus_nominal - s.ibus * 0.15 + 0.02 * math.sin(self._t * 5.0)
        # 一阶跟踪
        s.vbus += (target_v - s.vbus) * min(1.0, dt / 0.01)

    def _update_thermal(self, dt: float):
        """热模型: 一阶 RC, 损耗 = 铜损(I²R)。"""
        s = self.s
        # 铜损
        p_cu = (s.id ** 2 + s.iq ** 2) * self.R
        # 铁损(近似, 与速度²成正比)
        p_fe = 0.001 * s.omega_m ** 2
        p_loss = p_cu + p_fe
        # 电机绕组温度: T = T_amb + Rth·P_loss, 一阶趋近
        tau = self.tau_th
        target_temp = self.T_amb + self.Rth * p_loss
        alpha = min(1.0, dt / tau)
        s.temp_motor += (target_temp - s.temp_motor) * alpha
        # FET 温度(更快响应)
        s.temp_fet += (self.T_amb + self.Rth * 0.8 * p_loss - s.temp_fet) * min(1.0, dt / (tau * 0.3))
        # 温度上限 clamp: 防止数值发散时温度爆炸 (物理上也有熔断保护)
        s.temp_motor = _clamp(s.temp_motor, -40.0, 200.0)
        s.temp_fet = _clamp(s.temp_fet, -40.0, 150.0)

    def _update_encoder(self, dt: float):
        """编码器量化 + PLL速度观测(复刻 motion_param VEL_METHOD_PLL)。"""
        s = self.s
        # 量化: 真实角度 -> 编码器计数 -> 反算角度
        raw_count = s.theta_m * self.enc_lines / (2.0 * math.pi) * self.enc_dir
        s.enc_count = int(raw_count)
        # 反算量化后角度
        s.enc_theta = s.enc_count * 2.0 * math.pi / self.enc_lines * self.enc_dir
        # 单圈 + 多圈
        total = s.enc_theta
        s.multiturn = int(total // (2.0 * math.pi))
        s.single_turn = total % (2.0 * math.pi)

        # 位置低通滤波(对应 pos_filter_alpha)
        self._pos_filt += (s.enc_theta - self._pos_filt) * min(1.0, self.pos_alpha)

        # PLL速度观测器(二阶龙伯格, 对应 motion_param PLL)
        # 误差 = 实际角度 - 观测角度
        err = s.enc_theta - self._pll_theta
        # PLL增益(比例+积分), 对应 speed_obs_gain
        kp = self.pll_gain * 0.01
        ki = self.pll_gain * 0.0001
        self._pll_integral += ki * err * dt
        self._pll_integral = _clamp(self._pll_integral, -1e4, 1e4)
        self._pll_omega += (kp * err + self._pll_integral) * dt
        # 观测角度积分
        self._pll_theta += self._pll_omega * dt
        # 归一化观测角度(避免数值漂移)
        self._pll_theta = self._pll_theta % (2.0 * math.pi * 1e6)
        s.enc_vel = self._pll_omega

    # ================== 接口 ==================
    def reset(self):
        """复位物理状态到初始。"""
        self.s = PhysicsState(vbus=self.vbus_nominal, temp_motor=self.T_amb, temp_fet=self.T_amb)
        self._t = 0.0
        self._pll_theta = 0.0
        self._pll_omega = 0.0
        self._pll_integral = 0.0
        self._pos_filt = 0.0
        self._backlash_pos = 0.0

    def snapshot(self) -> dict:
        """返回全量状态字典(供应答器/遥测使用)。"""
        s = self.s
        return {
            'pos': s.pos_out, 'vel': s.vel_out, 'vel_out': s.vel_out,
            'torque': s.torque, 'torque_out': s.torque_out,
            'id': s.id, 'iq': s.iq,
            'ia': s.ia, 'ib': s.ib, 'ic': s.ic,
            'vbus': s.vbus, 'ibus': s.ibus, 'power': s.power,
            'temp_fet': s.temp_fet, 'temp_motor': s.temp_motor,
            'multiturn': s.multiturn, 'single': s.single_turn,
            'theta_m': s.theta_m, 'omega_m': s.omega_m,
            'enc_theta': s.enc_theta, 'enc_vel': s.enc_vel,
        }

    def set_load_inertia(self, J_load_out: float):
        """设置负载惯量(输出端), 折算到电机轴。

        折算公式: J_reflected = J_load / gear_ratio²
        总惯量 = J_rotor + J_reflected
        """
        self.J_total = self.J_rotor + J_load_out / (self.gear_ratio ** 2)
