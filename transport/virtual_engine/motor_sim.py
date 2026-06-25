"""虚拟电机物理仿真(纯逻辑, 无 Qt 依赖)。

一个极简的一阶电机模型, 目的不是高保真, 而是给上位机演示提供"看起来合理且联动"
的反馈数据: 位置随速度积分、速度向目标一阶逼近、电流/力矩随负载摆动、温度缓升等。

被 VirtualResponder 持有, 按固定步长 step(dt) 推进, 随时可读 snapshot() 取全量状态。
线程模型: 仅在 VirtualTransport 的单一定时器线程上下文调用, 无需加锁。
"""

import math


class MotorSim:
    """关节电机物理状态仿真。"""

    def __init__(self):
        # ---- 运行设定(由控制命令更新) ----
        self.enabled = False
        self.ctrl_mode = 0          # ctrl_mode_e (命令码)
        self.target_pos = 0.0       # rad (位置/复合模式目标)
        self.target_vel = 0.0       # rad/s (速度模式目标)
        self.target_torque = 0.0    # Nm (力矩模式目标)

        # ---- 物理状态 ----
        self.pos = 0.0              # 输出端多圈位置 rad
        self.vel = 0.0              # rad/s
        self.torque = 0.0           # Nm
        self.id = 0.0               # d 轴电流 A
        self.iq = 0.0               # q 轴电流 A
        self.vbus = 24.0            # 母线电压 V
        self.ibus = 0.0             # 母线电流 A
        self.temp_fet = 30.0        # ℃
        self.temp_motor = 30.0      # ℃
        self.fault_mask = 0
        self.warn_mask = 0

        # ---- 电机本体参数(可被 PARAM_WRITE 修改, 供反馈面板显示) ----
        self.params = {
            'r': 0.085,            # 相电阻 Ω
            'ld': 0.00012,         # d 轴电感 H
            'lq': 0.00015,         # q 轴电感 H
            'flux': 0.0085,        # 磁链 Wb
            'kt': 0.092,           # 转矩常数 Nm/A
            'pole_pairs': 14,      # 极对数
            'ke': 0.061,           # 反电动势常数
        }

        self._t = 0.0               # 累计仿真时间 s
        self._vel_tau = 0.08        # 速度一阶时间常数 s
        self._pos_kp = 6.0          # 位置环等效比例(rad/s per rad)

    # ---------------- 控制输入 ----------------
    def set_enabled(self, en: bool):
        self.enabled = bool(en)
        if not en:
            # 失能: 目标清零, 电流回落, 但保留位置
            self.target_vel = 0.0
            self.target_torque = 0.0

    def set_mode(self, ctrl_mode: int):
        self.ctrl_mode = int(ctrl_mode)

    def command(self, ctrl_mode: int, values: dict):
        """运动命令: 按模式把载荷映射到目标量。values 为已解包的字段字典。

        兼容 CSV 字段命名: pos/pos_ref, vel/vel_ref/vel_ff, torque/tff, iq_ref。"""
        self.ctrl_mode = int(ctrl_mode)

        def pick(*names):
            for n in names:
                if n in values:
                    return float(values[n])
            return None

        v = pick('pos', 'pos_ref')
        if v is not None:
            self.target_pos = v
        v = pick('vel', 'vel_ref', 'vel_ff')
        if v is not None:
            self.target_vel = v
        v = pick('torque', 'tff')
        if v is not None:
            self.target_torque = v
        v = pick('iq_ref')
        if v is not None:
            self.target_torque = v * self.params['kt']

    # ---------------- 仿真步进 ----------------
    def step(self, dt: float):
        self._t += dt
        from jmproto import JmCmd

        if not self.enabled:
            # 失能: 自由停转(阻尼), 电流归零
            self.vel *= math.exp(-dt / 0.25)
            self.iq *= 0.6
            self.id *= 0.6
            self.torque *= 0.6
        else:
            mode = self.ctrl_mode
            # 决定目标速度
            if mode == int(JmCmd.POSITION) or mode == int(JmCmd.POSITION_VELOCITY):
                vel_cmd = self._pos_kp * (self.target_pos - self.pos)
                vel_cmd = max(-50.0, min(50.0, vel_cmd))
            elif mode == int(JmCmd.VELOCITY):
                vel_cmd = self.target_vel
            elif mode in (int(JmCmd.TORQUE), int(JmCmd.CURRENT), int(JmCmd.MIT)):
                # 力矩/电流模式: 力矩驱动, 速度由力矩与负载平衡近似
                vel_cmd = self.target_torque * 8.0
            else:
                vel_cmd = self.target_vel
            # 速度一阶逼近
            self.vel += (vel_cmd - self.vel) * min(1.0, dt / self._vel_tau)

        # 位置积分
        self.pos += self.vel * dt

        # 力矩/电流: 与加速度+负载相关, 叠加小幅纹波让演示更生动
        accel = vel_cmd - self.vel if self.enabled else -self.vel
        ripple = 0.05 * math.sin(self._t * 37.0)
        self.iq = (self.vel * 0.02 + accel * 0.1 + (self.target_torque / self.params['kt']
                   if self.enabled else 0.0)) + ripple
        self.id = 0.2 * math.sin(self._t * 23.0)
        self.torque = self.iq * self.params['kt']

        # 三相电流(由 dq 反 Park 近似重构, 仅供显示)
        theta_e = self.pos * self.params['pole_pairs']
        i_amp = math.hypot(self.id, self.iq)
        self.ia = i_amp * math.sin(theta_e)
        self.ib = i_amp * math.sin(theta_e - 2.0 * math.pi / 3.0)
        self.ic = i_amp * math.sin(theta_e + 2.0 * math.pi / 3.0)

        # 母线: 电压含轻微跌落, 电流随功率
        mech_p = abs(self.torque * self.vel)
        self.ibus = mech_p / max(1.0, self.vbus) + 0.05
        self.vbus = 24.0 - self.ibus * 0.15 + 0.02 * math.sin(self._t * 5.0)

        # 温度: 随损耗缓升, 上限附近饱和
        loss = self.iq * self.iq * self.params['r']
        self.temp_motor += (30.0 + loss * 4.0 - self.temp_motor) * min(1.0, dt / 5.0)
        self.temp_fet += (30.0 + loss * 3.0 - self.temp_fet) * min(1.0, dt / 4.0)

    # ---------------- 读出 ----------------
    @property
    def single_turn(self) -> float:
        """单圈位置 rad (0~2π)"""
        return self.pos % (2.0 * math.pi)

    @property
    def multiturn(self) -> int:
        return int(self.pos // (2.0 * math.pi))

    @property
    def power(self) -> float:
        return self.vbus * self.ibus

    def snapshot(self) -> dict:
        """返回全量状态字典(供应答器按需取用)。"""
        return {
            'pos': self.pos, 'vel': self.vel, 'torque': self.torque,
            'id': self.id, 'iq': self.iq,
            'ia': getattr(self, 'ia', 0.0), 'ib': getattr(self, 'ib', 0.0),
            'ic': getattr(self, 'ic', 0.0),
            'vbus': self.vbus, 'ibus': self.ibus, 'power': self.power,
            'temp_fet': self.temp_fet, 'temp_motor': self.temp_motor,
            'multiturn': self.multiturn, 'single': self.single_turn,
            'fault_mask': self.fault_mask, 'warn_mask': self.warn_mask,
            'enabled': self.enabled, 'ctrl_mode': self.ctrl_mode,
        }
