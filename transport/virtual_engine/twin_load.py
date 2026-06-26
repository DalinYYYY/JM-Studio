"""数字孪生关节负载模型: 连杆惯量 + 重力 + 外力 + 碰撞。

关节电机典型负载场景:
  - 连杆: 绕关节轴的等效惯量 + 连杆自身重力矩 m·g·L·sin(θ)
  - 外力: 操作员施加的外部力矩(示教/碰撞)
  - 碰撞: 硬限位/软停的弹性+阻尼接触力
  - 端点负载: 末端执行器质量

所有负载力矩折算到电机轴后输出: T_load_motor = T_load_out / gear_ratio。
孪生引擎调用 load.update(pos, vel, dt) 获取当前负载力矩, 传给物理模型。
"""

import math
from dataclasses import dataclass, field


def _sgn(x: float) -> float:
    return 1.0 if x > 0 else (-1.0 if x < 0 else 0.0)


@dataclass
class LinkConfig:
    """单连杆负载配置。"""
    mass: float = 0.0          # 连杆质量 kg
    length: float = 0.0        # 连杆质心到关节距离 m
    inertia: float = 0.0       # 绕关节轴惯量 kg·m² (不含质点)
    gravity: float = 9.81      # 重力加速度 m/s²
    # 连杆方向: 0=重力沿关节轴(无重力矩), 1=水平面外抬升(标准关节)
    gravity_axis: float = 1.0


@dataclass
class CollisionConfig:
    """碰撞/限位接触模型参数。"""
    enable: bool = False
    stiffness: float = 1e5      # 接触刚度 N·m/rad
    damping: float = 50.0       # 接触阻尼 N·m/(rad/s)
    pos_limit_min: float = -1e9  # 碰撞限位下限 rad(输出端)
    pos_limit_max: float = 1e9   # 碰撞限位上限 rad(输出端)


@dataclass
class LoadState:
    """负载模型当前状态。"""
    t_load_out: float = 0.0    # 输出端负载力矩 Nm (正值阻碍运动)
    t_gravity: float = 0.0     # 重力矩分量
    t_external: float = 0.0     # 外力矩分量
    t_collision: float = 0.0   # 碰撞力矩分量
    in_collision: bool = False  # 是否处于碰撞接触
    J_load: float = 0.0         # 当前总等效负载惯量 kg·m²


class JointLoad:
    """关节负载模型。

    用法:
      load = JointLoad(gear_ratio=100)
      load.link.mass = 1.0; load.link.length = 0.15
      t_load, J = load.update(pos_out, vel_out, dt)
      physics.set_load_inertia(J)
      physics.step(dt, ud, uq, t_load_motor=t_load / gear_ratio)
    """

    def __init__(self, gear_ratio: float = 100.0):
        self.gear_ratio = max(gear_ratio, 1.0)
        self.link = LinkConfig()
        self.collision = CollisionConfig()
        self.state = LoadState()

        # 外部施加的力矩(Nm, 输出端), 可被运行时设置(如示教/扰动)
        self.external_torque: float = 0.0
        # 末端附加质量 kg
        self.tip_mass: float = 0.0
        self.tip_length: float = 0.0  # 末端到关节距离 m

        # 扰动力矩(用于演示, 可叠加噪声/正弦)
        self.disturbance_enable: bool = False
        self.disturbance_amp: float = 0.0    # Nm
        self.disturbance_freq: float = 0.0    # Hz

        self._t = 0.0

    def update(self, pos_out: float, vel_out: float, dt: float):
        """计算当前负载力矩与等效惯量。

        Args:
            pos_out: 输出端位置 rad
            vel_out: 输出端速度 rad/s
            dt: 步长 s
        Returns:
            (t_load_motor, J_load_total_out)
            t_load_motor: 折算到电机轴的负载力矩 Nm
            J_load_total_out: 输出端总惯量 kg·m² (含连杆+末端)
        """
        self._t += dt
        st = self.state

        # ---- 1. 重力矩 ----
        # 连杆重力: T = m·g·L·sin(θ) + m_tip·g·L_tip·sin(θ)
        # (假设连杆在垂直平面内运动, θ=0为水平)
        if self.link.gravity_axis != 0:
            g = self.link.gravity
            T_link = self.link.mass * g * self.link.length * math.sin(pos_out)
            T_tip = self.tip_mass * g * self.tip_length * math.sin(pos_out)
            st.t_gravity = (T_link + T_tip) * self.link.gravity_axis
        else:
            st.t_gravity = 0.0

        # ---- 2. 外部力矩 ----
        st.t_external = self.external_torque

        # ---- 3. 扰动(演示用) ----
        if self.disturbance_enable and self.disturbance_amp > 0:
            st.t_external += self.disturbance_amp * math.sin(
                2.0 * math.pi * self.disturbance_freq * self._t)

        # ---- 4. 碰撞/限位接触力 ----
        st.t_collision = 0.0
        st.in_collision = False
        if self.collision.enable:
            col = self.collision
            if pos_out < col.pos_limit_min:
                # 撞下限位: 弹簧+阻尼推回正向
                depth = col.pos_limit_min - pos_out
                st.t_collision = col.stiffness * depth + col.damping * max(0.0, -vel_out)
                st.in_collision = True
            elif pos_out > col.pos_limit_max:
                depth = pos_out - col.pos_limit_max
                st.t_collision = -(col.stiffness * depth + col.damping * max(0.0, vel_out))
                st.in_collision = True

        # ---- 5. 总负载力矩(输出端) ----
        st.t_load_out = st.t_gravity + st.t_external + st.t_collision

        # ---- 6. 等效惯量(输出端) ----
        # 连杆惯量 + 质点惯量 m·L² + 末端惯量
        J_link = self.link.inertia
        J_point = self.link.mass * self.link.length ** 2
        J_tip = self.tip_mass * self.tip_length ** 2
        st.J_load = J_link + J_point + J_tip

        # ---- 7. 折算到电机轴 ----
        t_load_motor = st.t_load_out / self.gear_ratio

        return t_load_motor, st.J_load

    # ================== 便捷设置 ==================
    def set_link(self, mass: float, length: float, inertia: float = 0.0):
        """配置连杆参数。"""
        self.link.mass = mass
        self.link.length = length
        self.link.inertia = inertia

    def set_tip(self, mass: float, length: float):
        """配置末端负载。"""
        self.tip_mass = mass
        self.tip_length = length

    def set_collision_limit(self, pos_min: float, pos_max: float,
                             stiffness: float = 1e5, damping: float = 50.0):
        """配置碰撞限位。"""
        self.collision.enable = True
        self.collision.pos_limit_min = pos_min
        self.collision.pos_limit_max = pos_max
        self.collision.stiffness = stiffness
        self.collision.damping = damping

    def apply_external_torque(self, torque: float):
        """施加外部力矩(输出端, Nm), 如示教力。"""
        self.external_torque = torque

    def apply_disturbance(self, amplitude: float, freq: float):
        """施加正弦扰动(演示用)。"""
        self.disturbance_enable = amplitude > 0
        self.disturbance_amp = amplitude
        self.disturbance_freq = freq

    def reset(self):
        """复位负载状态。"""
        self.state = LoadState()
        self.external_torque = 0.0
        self._t = 0.0

    def snapshot(self) -> dict:
        st = self.state
        return {
            't_load_out': st.t_load_out, 't_gravity': st.t_gravity,
            't_external': st.t_external, 't_collision': st.t_collision,
            'in_collision': st.in_collision, 'J_load': st.J_load,
        }
