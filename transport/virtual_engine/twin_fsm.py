"""数字孪生顶层状态机: 复刻 system_state.h/c 的 FSM + run_state 切换。

状态层级:
  顶层 system_state: IDLE → READY → RUN → (FAULT | SAFETY)
  运行态 run_state:   STANDSTILL → MOVING → DECEL → HOLDING / BRAKING

故障管理:
  检测: 过流/过压/欠压/过温/位置超限/跟随误差/通信丢失
  注入: 支持 fault_inject() 注入虚拟故障(演示用)
  清除: fault_clear() 复位故障

输出: MotorRef (电机控制参考), 供 ControllerCore 使用。
"""

import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

from .twin_control import MotorRef, RefCtrlType, PidProfileID


# ==================== 枚举 (对齐 system_state.h) ====================
class SystemState(IntEnum):
    IDLE = 0
    READY = 1
    RUN = 2
    FAULT = 3
    SAFETY = 4


class RunState(IntEnum):
    STANDSTILL = 0
    MOVING = 1
    DECEL = 2
    HOLDING = 3
    BRAKING = 4


class ControlMode(IntEnum):
    NONE = 0
    POSITION = 1
    VELOCITY = 2
    TORQUE = 3
    CURRENT = 4
    VOLTAGE = 5
    DUTY = 6
    IMPEDANCE = 7


# ==================== 故障标志位 ====================
class Fault:
    NONE = 0
    OVER_CURRENT = 1 << 0
    OVER_VOLTAGE = 1 << 1
    UNDER_VOLTAGE = 1 << 2
    OVER_TEMP_FET = 1 << 3
    OVER_TEMP_MOTOR = 1 << 4
    POS_LIMIT = 1 << 5          # 软件位置超限
    FOLLOW_ERROR = 1 << 6       # 跟随误差过大
    COMM_LOST = 1 << 7           # 通信丢失
    PHASE_LOSS = 1 << 8          # 缺相
    SAFETY_LIMIT = 1 << 9       # 安全限位触发

    # 注入型故障(演示用)
    INJECTED = 1 << 15

    @staticmethod
    def to_str(flags: int) -> str:
        names = []
        m = {
            Fault.OVER_CURRENT: 'OVER_CURRENT',
            Fault.OVER_VOLTAGE: 'OVER_VOLTAGE',
            Fault.UNDER_VOLTAGE: 'UNDER_VOLTAGE',
            Fault.OVER_TEMP_FET: 'OVER_TEMP_FET',
            Fault.OVER_TEMP_MOTOR: 'OVER_TEMP_MOTOR',
            Fault.POS_LIMIT: 'POS_LIMIT',
            Fault.FOLLOW_ERROR: 'FOLLOW_ERROR',
            Fault.COMM_LOST: 'COMM_LOST',
            Fault.PHASE_LOSS: 'PHASE_LOSS',
            Fault.SAFETY_LIMIT: 'SAFETY_LIMIT',
            Fault.INJECTED: 'INJECTED',
        }
        for bit, name in m.items():
            if flags & bit:
                names.append(name)
        return '|'.join(names) if names else 'NONE'


# ==================== 命令输入 ====================
@dataclass
class MotorCmd:
    """上位机/外部命令输入。

    目标字段用 Optional[float]: None=未设置(保持原值), 0.0=合法目标值。
    这样运动中可发送空命令(不覆盖目标), 也可显式设目标为0。
    """
    enable: bool = False         # IDLE→READY
    disable: bool = False        # →IDLE
    start: bool = False          # READY→RUN
    stop: bool = False           # RUN→READY
    fault_clear: bool = False    # FAULT→IDLE
    reset: bool = False          # 任意→IDLE

    # 运动目标设置(None=未设置, 不覆盖既有目标)
    set_mode: ControlMode = ControlMode.NONE
    set_pos: Optional[float] = None    # 目标位置 rad
    set_vel: Optional[float] = None    # 目标速度 rad/s
    set_torque: Optional[float] = None  # 目标力矩 Nm
    set_id: Optional[float] = None
    set_iq: Optional[float] = None
    set_voltage: Optional[float] = None
    set_duty: Optional[float] = None
    set_kp: Optional[float] = None     # MIT刚度
    set_kd: Optional[float] = None     # MIT阻尼
    set_torque_ff: Optional[float] = None  # 力矩前馈
    set_vel_ff: Optional[float] = None     # 速度前馈


# ==================== 故障检测器 ====================
@dataclass
class Threshold:
    """故障阈值(从 motor_param 取, 这里给默认值)。"""
    over_current: float = 30.0       # A
    over_voltage: float = 56.0      # V
    under_voltage: float = 24.0      # V
    over_temp_fet: float = 80.0      # C
    over_temp_motor: float = 90.0    # C
    follow_err: float = 0.5          # rad
    follow_err_time: float = 0.2    # 跟随误差持续超限时间(避免瞬态误触发)
    comm_timeout: float = 0.2         # s


class FaultDetector:
    """故障检测器, 检查物理状态并输出故障标志。"""

    def __init__(self, mp):
        self.mp = mp
        self.th = Threshold()
        self._load_thresholds()
        self.injected_fault = 0
        self.last_comm_time = 0.0
        self._follow_err_timer = 0.0   # 跟随误差持续超限计时器

    def _load_thresholds(self):
        mp = self.mp
        # 从 motor_param 提取阈值(若有对应字段)
        self.th.over_current = getattr(mp.motor_base, 'peak_current', 30.0) * 1.2
        self.th.over_voltage = mp.motor_base.rated_voltage * 1.1
        self.th.under_voltage = mp.motor_base.rated_voltage * 0.5
        self.th.over_temp_fet = getattr(mp.thermal_model, 'fet_over_temp_threshold', 80.0)
        self.th.over_temp_motor = getattr(mp.thermal_model, 'motor_over_temp_threshold', 90.0)
        self.th.follow_err = getattr(mp.position_loop, 'follow_error_threshold', 0.5)

    def update_comm(self, t_now: float):
        self.last_comm_time = t_now

    def check(self, state: dict, t_now: float, dt: float) -> int:
        """检查物理状态, 返回故障标志位。"""
        flags = 0
        th = self.th

        # 过流: 任意相电流超限
        peak = max(abs(state.get('id', 0)), abs(state.get('iq', 0)),
                    abs(state.get('ia', 0)))
        if peak > th.over_current:
            flags |= Fault.OVER_CURRENT

        # 过压/欠压
        vbus = state.get('vbus', 48.0)
        if vbus > th.over_voltage:
            flags |= Fault.OVER_VOLTAGE
        if vbus < th.under_voltage:
            flags |= Fault.UNDER_VOLTAGE

        # 过温
        if state.get('temp_fet', 30) > th.over_temp_fet:
            flags |= Fault.OVER_TEMP_FET
        if state.get('temp_motor', 30) > th.over_temp_motor:
            flags |= Fault.OVER_TEMP_MOTOR

        # 跟随误差: 必须持续超限超过 follow_err_time 才报故障(避免瞬态误触发)
        follow_err = state.get('follow_err', 0.0)
        if abs(follow_err) > th.follow_err:
            self._follow_err_timer += dt
            if self._follow_err_timer >= th.follow_err_time:
                flags |= Fault.FOLLOW_ERROR
        else:
            self._follow_err_timer = 0.0

        # 通信丢失
        if t_now - self.last_comm_time > th.comm_timeout:
            flags |= Fault.COMM_LOST

        # 注入型故障
        if self.injected_fault:
            flags |= self.injected_fault

        return flags

    def inject(self, fault_bits: int):
        """注入故障(演示用)。"""
        self.injected_fault |= fault_bits

    def clear_injected(self):
        self.injected_fault = 0


# ==================== 顶层状态机 ====================
class SystemStateMachine:
    """顶层状态机, 复刻 system_state.c。

    每个仿真周期调用 step(cmd, fault_flags, fb, t_now, dt):
      1. 处理命令(enable/disable/start/stop/fault_clear)
      2. 检查故障, 必要时迁移到 FAULT
      3. 根据 system_state + 控制模式生成 MotorRef
      4. 更新 run_state
    """

    def __init__(self, mp):
        self.mp = mp
        self.sys_state = SystemState.IDLE
        self.run_state = RunState.STANDSTILL
        self.control_mode = ControlMode.NONE
        self.fault_flags = 0
        self.motor_ref = MotorRef(ctrl_type=RefCtrlType.IDLE)
        self._in_safety = False
        self._prev_target_set = False
        # 当前生效的目标
        self.target_pos = 0.0
        self.target_vel = 0.0
        self.target_torque = 0.0
        self.target_id = 0.0
        self.target_iq = 0.0
        self.target_voltage = 0.0
        self.target_duty = 0.0
        self.target_kp = 0.0
        self.target_kd = 0.0
        self.target_torque_ff = 0.0
        self.target_vel_ff = 0.0
        self.pos_limit_min = mp.position_limit.pos_min_limit
        self.pos_limit_max = mp.position_limit.pos_max_limit

    # ---------- 状态迁移 ----------
    def step(self, cmd: MotorCmd, fault_flags: int, fb: dict, t_now: float, dt: float):
        """推进一个状态机周期。"""
        # 1. 故障优先: 任何状态检测到故障都进入 FAULT
        if fault_flags and self.sys_state != SystemState.FAULT:
            self.sys_state = SystemState.FAULT
            self.fault_flags = fault_flags
            self.run_state = RunState.STANDSTILL
            self.motor_ref = MotorRef(ctrl_type=RefCtrlType.IDLE)
            return

        # 2. 按当前状态处理命令
        st = self.sys_state
        if st == SystemState.IDLE:
            self._handle_idle(cmd)
        elif st == SystemState.READY:
            self._handle_ready(cmd, fb)
        elif st == SystemState.RUN:
            self._handle_run(cmd, fault_flags, fb, dt)
        elif st == SystemState.FAULT:
            self._handle_fault(cmd)
        elif st == SystemState.SAFETY:
            self._handle_safety(cmd, fb)

    def _handle_idle(self, cmd: MotorCmd):
        if cmd.enable:
            self.sys_state = SystemState.READY
            self.run_state = RunState.STANDSTILL
        elif cmd.reset:
            pass  # 已在 IDLE

    def _handle_ready(self, cmd: MotorCmd, fb: dict):
        if cmd.disable:
            self.sys_state = SystemState.IDLE
            return
        if cmd.start and cmd.set_mode != ControlMode.NONE:
            self.control_mode = cmd.set_mode
            self._apply_targets(cmd)
            self.sys_state = SystemState.RUN
            self.run_state = RunState.MOVING
            # 立即生成 MotorRef, 让第一拍控制器就能输出有效 ud/uq
            self._build_motor_ref(fb)

    def _handle_run(self, cmd: MotorCmd, fault_flags: int, fb: dict, dt: float):
        if cmd.stop:
            self.sys_state = SystemState.READY
            self.run_state = RunState.STANDSTILL
            self.motor_ref = MotorRef(ctrl_type=RefCtrlType.IDLE)
            return

        # 实时更新目标(运动中可改目标)
        if cmd.set_mode != ControlMode.NONE:
            self.control_mode = cmd.set_mode
        self._apply_targets(cmd)

        # 软件位置限位 → SAFETY
        pos = fb.get('pos', 0.0)
        if pos >= self.pos_limit_max or pos <= self.pos_limit_min:
            self.sys_state = SystemState.SAFETY
            self.run_state = RunState.STANDSTILL
            self._in_safety = True
            return

        # 更新 run_state
        self._update_run_state(fb)

        # 生成 MotorRef
        self._build_motor_ref(fb)

    def _handle_fault(self, cmd: MotorCmd):
        if cmd.fault_clear or cmd.reset:
            self.sys_state = SystemState.IDLE
            self.fault_flags = 0
            self.run_state = RunState.STANDSTILL
            self.control_mode = ControlMode.NONE
            self.motor_ref = MotorRef(ctrl_type=RefCtrlType.IDLE)

    def _handle_safety(self, cmd: MotorCmd, fb: dict):
        pos = fb.get('pos', 0.0)
        # 反向运动命令可退出 SAFETY
        if cmd.start and cmd.set_mode != ControlMode.NONE:
            new_target = cmd.set_pos if self.control_mode == ControlMode.POSITION else 0
            moving_away = (pos >= self.pos_limit_max and new_target < pos) or \
                           (pos <= self.pos_limit_min and new_target > pos)
            if moving_away or self.control_mode != ControlMode.POSITION:
                self.sys_state = SystemState.RUN
                self.run_state = RunState.MOVING
                self.control_mode = cmd.set_mode
                self._apply_targets(cmd)
                self._in_safety = False
                return
        if cmd.stop:
            self.sys_state = SystemState.READY
            self._in_safety = False

    def _update_run_state(self, fb: dict):
        """更新运行子状态。"""
        vel = abs(fb.get('vel', 0.0))
        target_vel = abs(self.target_vel)
        mode = self.control_mode

        if mode in (ControlMode.POSITION,):
            # 位置模式: 接近目标 → HOLDING, 否则 MOVING
            pos_err = abs(fb.get('pos', 0.0) - self.target_pos)
            if pos_err < 0.01 and vel < 0.05:
                self.run_state = RunState.HOLDING
            else:
                self.run_state = RunState.MOVING
        elif mode == ControlMode.VELOCITY:
            self.run_state = RunState.MOVING if target_vel > 0.01 else RunState.STANDSTILL
        else:
            self.run_state = RunState.MOVING

    # ---------- MotorRef 生成 ----------
    def _apply_targets(self, cmd: MotorCmd):
        """应用命令中的目标值(仅当字段非None时更新)。"""
        if cmd.set_pos is not None:
            self.target_pos = cmd.set_pos
        if cmd.set_vel is not None:
            self.target_vel = cmd.set_vel
        if cmd.set_torque is not None:
            self.target_torque = cmd.set_torque
        if cmd.set_id is not None:
            self.target_id = cmd.set_id
        if cmd.set_iq is not None:
            self.target_iq = cmd.set_iq
        if cmd.set_voltage is not None:
            self.target_voltage = cmd.set_voltage
        if cmd.set_duty is not None:
            self.target_duty = cmd.set_duty
        if cmd.set_kp is not None:
            self.target_kp = cmd.set_kp
        if cmd.set_kd is not None:
            self.target_kd = cmd.set_kd
        if cmd.set_torque_ff is not None:
            self.target_torque_ff = cmd.set_torque_ff
        if cmd.set_vel_ff is not None:
            self.target_vel_ff = cmd.set_vel_ff

    def _build_motor_ref(self, fb: dict):
        """根据当前模式生成 MotorRef。"""
        ref = MotorRef()
        m = self.control_mode

        if m == ControlMode.POSITION:
            ref.ctrl_type = RefCtrlType.POSITION
            ref.pos = self.target_pos
            ref.vel_ff = self.target_vel_ff
            ref.pos_profile = PidProfileID.POSITION
            ref.vel_profile = PidProfileID.VELOCITY
        elif m == ControlMode.VELOCITY:
            ref.ctrl_type = RefCtrlType.VELOCITY
            ref.vel = self.target_vel
            ref.torque_ff = self.target_torque_ff
            ref.vel_profile = PidProfileID.VELOCITY
        elif m == ControlMode.TORQUE:
            ref.ctrl_type = RefCtrlType.TORQUE
            ref.torque = self.target_torque
            ref.torque_ff = self.target_torque_ff
        elif m == ControlMode.CURRENT:
            ref.ctrl_type = RefCtrlType.CURRENT
            ref.id = self.target_id
            ref.iq = self.target_iq
        elif m == ControlMode.VOLTAGE:
            ref.ctrl_type = RefCtrlType.VOLTAGE
            ref.voltage = self.target_voltage
        elif m == ControlMode.DUTY:
            ref.ctrl_type = RefCtrlType.DUTY
            ref.duty = self.target_duty
        elif m == ControlMode.IMPEDANCE:
            ref.ctrl_type = RefCtrlType.POSITION
            ref.pos = self.target_pos
            ref.vel = self.target_vel
            ref.kp = self.target_kp
            ref.kd = self.target_kd
            ref.torque_ff = self.target_torque_ff
            ref.pos_profile = PidProfileID.IMPEDANCE
        else:
            ref.ctrl_type = RefCtrlType.IDLE

        self.motor_ref = ref

    # ---------- 接口 ----------
    def get_ref(self) -> MotorRef:
        return self.motor_ref

    def reset(self):
        self.sys_state = SystemState.IDLE
        self.run_state = RunState.STANDSTILL
        self.control_mode = ControlMode.NONE
        self.fault_flags = 0
        self.motor_ref = MotorRef(ctrl_type=RefCtrlType.IDLE)
        self._in_safety = False

    def snapshot(self) -> dict:
        return {
            'sys_state': int(self.sys_state),
            'sys_state_name': self.sys_state.name,
            'run_state': int(self.run_state),
            'run_state_name': self.run_state.name,
            'control_mode': int(self.control_mode),
            'control_mode_name': self.control_mode.name,
            'fault_flags': self.fault_flags,
            'fault_str': Fault.to_str(self.fault_flags),
        }
