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
    OVER_SPEED = 1 << 10        # 超速保护 (对齐固件 protect_over_speed)

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
            Fault.OVER_SPEED: 'OVER_SPEED',
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

    # 故障屏蔽(可选): None=不修改, int=设置 disable_mask
    set_fault_disable: Optional[int] = None


# ==================== 故障检测器 ====================
@dataclass
class Threshold:
    """故障阈值(从 protection_param 取, fallback 到 motor_base/thermal_model)。"""
    over_current: float = 30.0       # A
    over_voltage: float = 56.0      # V
    under_voltage: float = 24.0      # V
    over_temp_fet: float = 80.0      # C
    over_temp_motor: float = 90.0    # C
    under_temp: float = -20.0        # C (欠温保护)
    follow_err: float = 0.5          # rad
    follow_err_time: float = 0.2    # 跟随误差持续超限时间(避免瞬态误触发)
    comm_timeout: float = 0.2         # s
    over_speed: float = 400.0        # rad/s (对齐固件 protect_over_speed)


class FaultDetector:
    """故障检测器, 检查物理状态并输出故障标志。

    故障屏蔽(disable_mask):
      位掩码, 某位置1表示该故障被屏蔽(不触发, 不进 FAULT)。
      默认 0 表示全部启用。INJECTED 位也可屏蔽, 便于演示。
      设置方式: set_disable_mask() / MotorCmd.set_fault_disable
    """

    def __init__(self, mp):
        self.mp = mp
        self.th = Threshold()
        self._load_thresholds()
        self.injected_fault = 0
        self.last_comm_time = 0.0
        self._follow_err_timer = 0.0   # 跟随误差持续超限计时器
        self.disable_mask = 0          # 故障屏蔽掩码(位1=该故障不触发)

    def _load_thresholds(self):
        mp = self.mp
        pp = mp.protection_param
        # 优先使用 protection_param (固件保护阈值), fallback 到 motor_base/thermal_model
        # 过流: protection_param.protect_over_current, fallback peak_current*1.2
        self.th.over_current = getattr(pp, 'protect_over_current', 0) or \
                               getattr(mp.motor_base, 'peak_current', 30.0) * 1.2
        # 过压/欠压
        self.th.over_voltage = getattr(pp, 'protect_over_voltage', 0) or \
                               mp.motor_base.rated_voltage * 1.1
        self.th.under_voltage = getattr(pp, 'protect_under_voltage', 0) or \
                                mp.motor_base.rated_voltage * 0.5
        # 过温: protection_param.protect_over_temp 同时用于 FET 与电机
        # (固件 protect_over_temp 是统一阈值; thermal_model 提供更精细的分项)
        prot_temp = getattr(pp, 'protect_over_temp', 0)
        self.th.over_temp_fet = prot_temp or \
                                getattr(mp.thermal_model, 'fet_over_temp_threshold', 80.0)
        self.th.over_temp_motor = prot_temp or \
                                  getattr(mp.thermal_model, 'motor_over_temp_threshold', 90.0)
        # 欠温
        self.th.under_temp = getattr(pp, 'protect_under_temp', -20.0)
        # 跟随误差
        self.th.follow_err = getattr(mp.position_loop, 'follow_error_threshold', 0.5)
        # 超速阈值: 取 protection_param.protect_over_speed 与 motor_base.max_speed*1.3 的较小值
        # (双重保护: 固件保护阈值 + 机械安全裕度)
        protect_speed = getattr(pp, 'protect_over_speed', 400.0)
        mech_speed = getattr(mp.motor_base, 'max_speed', 300.0) * 1.3
        self.th.over_speed = min(protect_speed, mech_speed)

    def update_comm(self, t_now: float):
        self.last_comm_time = t_now

    def set_disable_mask(self, mask: int):
        """设置故障屏蔽掩码。位1 = 对应故障不触发。"""
        self.disable_mask = int(mask) & 0xFFFF

    def get_disable_mask(self) -> int:
        return self.disable_mask

    def check(self, state: dict, t_now: float, dt: float) -> int:
        """检查物理状态, 返回故障标志位(已应用屏蔽掩码)。

        屏蔽来源:
          1. self.disable_mask (演示用屏蔽, UI 勾选)
          2. self.mp.protection_param.protect_enable_mask 取反 (固件使能掩码)
          两者取或: dm = disable_mask | ~enable_mask
        """
        flags = 0
        th = self.th
        # 合并屏蔽: 演示屏蔽(disable_mask) + 固件使能掩码取反
        enable_mask = getattr(self.mp.protection_param, 'protect_enable_mask', 0xFFFFFFFF)
        dm = self.disable_mask | (~int(enable_mask) & 0xFFFF)

        # 过流: 任意相电流超限
        if not (dm & Fault.OVER_CURRENT):
            peak = max(abs(state.get('id', 0)), abs(state.get('iq', 0)),
                        abs(state.get('ia', 0)))
            if peak > th.over_current:
                flags |= Fault.OVER_CURRENT

        # 超速: 电机端角速度超限 (对齐固件 protect_over_speed)
        # 防止无速度闭环模式(力矩/电流/开环)空载无限加速导致数值发散
        if not (dm & Fault.OVER_SPEED):
            omega_m = abs(state.get('omega_m', 0.0))
            if omega_m > th.over_speed:
                flags |= Fault.OVER_SPEED

        # 过压/欠压
        vbus = state.get('vbus', 48.0)
        if not (dm & Fault.OVER_VOLTAGE) and vbus > th.over_voltage:
            flags |= Fault.OVER_VOLTAGE
        if not (dm & Fault.UNDER_VOLTAGE) and vbus < th.under_voltage:
            flags |= Fault.UNDER_VOLTAGE

        # 过温
        if not (dm & Fault.OVER_TEMP_FET) and state.get('temp_fet', 30) > th.over_temp_fet:
            flags |= Fault.OVER_TEMP_FET
        if not (dm & Fault.OVER_TEMP_MOTOR) and state.get('temp_motor', 30) > th.over_temp_motor:
            flags |= Fault.OVER_TEMP_MOTOR

        # 跟随误差: 必须持续超限超过 follow_err_time 才报故障(避免瞬态误触发)
        follow_err = state.get('follow_err', 0.0)
        if abs(follow_err) > th.follow_err:
            self._follow_err_timer += dt
            if self._follow_err_timer >= th.follow_err_time and not (dm & Fault.FOLLOW_ERROR):
                flags |= Fault.FOLLOW_ERROR
        else:
            self._follow_err_timer = 0.0

        # 通信丢失
        if not (dm & Fault.COMM_LOST):
            if t_now - self.last_comm_time > th.comm_timeout:
                flags |= Fault.COMM_LOST

        # 注入型故障(也受屏蔽掩码控制, 便于演示)
        if self.injected_fault:
            flags |= (self.injected_fault & ~dm)

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
        # 当前生效的目标(经斜坡过渡, 实际送入控制器)
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
        # 命令目标(用户/上位机直接写入, 斜坡过渡到 target_xxx)
        self.cmd_target_pos = 0.0
        self.cmd_target_vel = 0.0
        self.cmd_target_torque = 0.0
        self.cmd_target_id = 0.0
        self.cmd_target_iq = 0.0
        self.cmd_target_voltage = 0.0
        self.cmd_target_duty = 0.0
        # 目标斜坡速率(每秒变化量, 物理意义: 加速度/加加速度限制)
        # 位置 50 rad/s, 速度 200 rad/s², 力矩 50 Nm/s, 电流 100 A/s, 电压 200 V/s, 占空比 5 /s
        self.ramp_rate_pos = 50.0
        self.ramp_rate_vel = 200.0
        self.ramp_rate_torque = 50.0
        self.ramp_rate_current = 100.0
        self.ramp_rate_voltage = 200.0
        self.ramp_rate_duty = 5.0
        # 模式切换过渡: 切换时先把当前输出衰减到0再切新模式
        self._mode_transition = False       # 正在过渡
        self._transition_cnt = 0
        self._transition_total = 50         # 过渡拍数 (~FOC 50拍=5ms)
        self._pending_mode = ControlMode.NONE
        self._pending_targets_applied = False
        # 运动子状态检测: 跟踪上一拍速度/目标, 用于判断加减速
        self._prev_vel = 0.0
        self._prev_target_pos = 0.0
        self._prev_target_vel = 0.0
        self.pos_limit_min = mp.position_limit.pos_min_limit
        self.pos_limit_max = mp.position_limit.pos_max_limit
        self.gear_ratio = getattr(mp.gearbox_param, 'gear_ratio', 1.0)

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

        # 2. reset 命令优先: 任意状态强制回 IDLE
        #    (FAULT 由 _handle_fault 处理 reset; 其他状态在此处理)
        if cmd.reset and self.sys_state != SystemState.FAULT:
            self.reset()
            return

        # 3. 按当前状态处理命令
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
            self._apply_cmd_targets(cmd)
            # READY→RUN: 命令目标直接作为生效目标(首次启动无过渡)
            self._sync_cmd_to_target()
            self.sys_state = SystemState.RUN
            self.run_state = RunState.MOVING
            # 立即生成 MotorRef, 让第一拍控制器就能输出有效 ud/uq
            self._build_motor_ref(fb)

    def _handle_run(self, cmd: MotorCmd, fault_flags: int, fb: dict, dt: float):
        if cmd.disable:
            self.sys_state = SystemState.IDLE
            self.run_state = RunState.STANDSTILL
            self.motor_ref = MotorRef(ctrl_type=RefCtrlType.IDLE)
            # 清除过渡状态(避免下次启动残留)
            self._mode_transition = False
            self._pending_mode = ControlMode.NONE
            return
        if cmd.stop:
            self.sys_state = SystemState.READY
            self.run_state = RunState.STANDSTILL
            self.motor_ref = MotorRef(ctrl_type=RefCtrlType.IDLE)
            # 清除过渡状态(避免下次启动残留)
            self._mode_transition = False
            self._pending_mode = ControlMode.NONE
            return

        # 模式切换过渡处理
        if cmd.set_mode != ControlMode.NONE and cmd.set_mode != self.control_mode:
            # 发起模式切换: 启动过渡, 暂存新模式
            self._mode_transition = True
            self._transition_cnt = 0
            self._pending_mode = cmd.set_mode
            self._pending_targets_applied = False
            # 立即应用新模式的命令目标(但不切换 control_mode)
            self._apply_cmd_targets(cmd)

        if self._mode_transition:
            # 过渡阶段: 衰减当前模式输出目标到0
            self._ramp_down_during_transition(dt)
            self._transition_cnt += 1
            if self._transition_cnt >= self._transition_total:
                # 过渡完成: 切换到新模式
                self.control_mode = self._pending_mode
                self._mode_transition = False
                self._pending_mode = ControlMode.NONE
                # 命令目标已写入, 同步到生效目标
                self._sync_cmd_to_target()
        else:
            # 正常运行: 更新命令目标 + 斜坡过渡
            if cmd.set_mode != ControlMode.NONE and cmd.set_mode == self.control_mode:
                self._apply_cmd_targets(cmd)
            elif cmd.set_mode == ControlMode.NONE:
                # 仅更新目标(不改模式)
                self._apply_cmd_targets(cmd)
            self._ramp_targets(dt)

        # 软件位置限位 → SAFETY (仅位置相关模式检查: POSITION/IMPEDANCE)
        # 速度/力矩/电流/电压/占空比模式: 位置累积是正常物理现象, 不应触发限位
        if self.control_mode in (ControlMode.POSITION, ControlMode.IMPEDANCE):
            pos = fb.get('pos', 0.0)  # pos_out (输出端)
            # target_pos 在电机端坐标系, 转换到输出端与限位比较
            target_out = self.target_pos / max(self.gear_ratio, 1.0)
            # 仅当目标朝向/超过限位时才触发 SAFETY
            # (避免刚退出 SAFETY 反向运动时, 因仍位于限位处而反复触发)
            trigger = False
            if pos >= self.pos_limit_max and target_out >= self.pos_limit_max:
                trigger = True
            elif pos <= self.pos_limit_min and target_out <= self.pos_limit_min:
                trigger = True
            if trigger:
                self.sys_state = SystemState.SAFETY
                self.run_state = RunState.STANDSTILL
                self._in_safety = True
                # 进入 SAFETY 时清零 motor_ref, 让电机真正停转
                # (否则 controller.run 仍按旧目标输出 ud/uq, 电机在限位处震荡)
                self.motor_ref = MotorRef(ctrl_type=RefCtrlType.IDLE)
                return

        # 更新 run_state
        self._update_run_state(fb)

        # 生成 MotorRef
        self._build_motor_ref(fb)

    def _handle_fault(self, cmd: MotorCmd):
        if cmd.fault_clear or cmd.reset:
            self.reset()

    def _handle_safety(self, cmd: MotorCmd, fb: dict):
        # fb['pos'] = pos_out (输出端位置), 位置限位也在输出端坐标系
        pos = fb.get('pos', 0.0)
        # 反向运动命令可退出 SAFETY
        if cmd.start and cmd.set_mode != ControlMode.NONE:
            # 用新模式(cmd.set_mode)判断目标方向, 而非旧 control_mode
            new_mode = cmd.set_mode
            if new_mode == ControlMode.POSITION:
                # cmd.set_pos 是电机端目标, 需转换为输出端坐标再与 pos(pos_out) 比较
                new_target_motor = cmd.set_pos if cmd.set_pos is not None else self.target_pos
                new_target_out = new_target_motor / max(self.gear_ratio, 1.0)
                moving_away = (pos >= self.pos_limit_max and new_target_out < pos) or \
                               (pos <= self.pos_limit_min and new_target_out > pos)
                if not moving_away:
                    # 仍停留在 SAFETY: 保持电机停转
                    self.motor_ref = MotorRef(ctrl_type=RefCtrlType.IDLE)
                    return  # 不允许继续向限位运动
            # 非位置模式: 允许退出(由后续 SAFETY 检查防止再次越界)
            # SAFETY 退出: 电机已停, 直接同步命令目标(无过渡)
            self.sys_state = SystemState.RUN
            self.run_state = RunState.MOVING
            self.control_mode = new_mode
            self._apply_cmd_targets(cmd)
            self._sync_cmd_to_target()
            self._in_safety = False
            # 清除过渡状态(避免残留)
            self._mode_transition = False
            self._transition_cnt = 0
            self._pending_mode = ControlMode.NONE
            return
        if cmd.stop:
            self.sys_state = SystemState.READY
            self._in_safety = False
            return
        # 驻留 SAFETY: 强制 motor_ref=IDLE, 让电机真正停转
        # (digital_twin.step 每拍调用 controller.run(ref), 必须确保 ref=IDLE)
        self.motor_ref = MotorRef(ctrl_type=RefCtrlType.IDLE)

    def _update_run_state(self, fb: dict):
        """更新运动子状态 (STANDSTILL/MOVING/DECEL/HOLDING)。

        统一用物理反馈判断, 适用所有7个运动模式:
          STANDSTILL: 速度≈0 且目标≈0 (或位置已到位保持)
          MOVING:     速度在增大 (加速) 或目标非0
          DECEL:      速度在减小且非0 (减速中)
          HOLDING:    位置模式已到位且速度≈0

        注: 用电机端角速度 omega_m (而非输出端 vel_out) 判断,
            因减速器折算后输出端速度太小, 变化不易检测。
        """
        # 优先用电机端角速度 (数值大, 变化明显); fallback 到 vel
        vel = fb.get('omega_m', fb.get('vel', 0.0))
        abs_vel = abs(vel)
        prev_abs_vel = abs(self._prev_vel)
        # 速度阈值: 电机端 1.0 rad/s 以下视为静止 (输出端约0.01)
        VEL_STILL = 1.0
        # 减速判定: 速度幅值在下降 (用相对变化率避免与 dt 耦合)
        is_decelerating = (prev_abs_vel > abs_vel + max(0.01, prev_abs_vel * 0.001)
                           and abs_vel > VEL_STILL)
        mode = self.control_mode

        # 位置模式: 有 HOLDING 态 (到位保持)
        if mode == ControlMode.POSITION:
            # target_pos 在电机端坐标系, 用 theta_m (电机端角度) 算误差
            theta_m = fb.get('theta_m', fb.get('pos', 0.0))
            pos_err = abs(theta_m - self.target_pos)
            if pos_err < 0.02 and abs_vel < VEL_STILL:
                self.run_state = RunState.HOLDING
            elif is_decelerating:
                self.run_state = RunState.DECEL
            else:
                self.run_state = RunState.MOVING
        # 速度模式: 目标0→STANDSTILL/DECEL, 目标非0且未达速→MOVING
        elif mode == ControlMode.VELOCITY:
            target_vel = abs(self.target_vel)
            if target_vel < VEL_STILL and abs_vel < VEL_STILL:
                self.run_state = RunState.STANDSTILL
            elif target_vel < VEL_STILL and abs_vel >= VEL_STILL:
                # 目标为0但仍在转 → 减速中
                self.run_state = RunState.DECEL
            elif is_decelerating:
                # 目标非0但速度在下降 → 减速
                self.run_state = RunState.DECEL
            else:
                self.run_state = RunState.MOVING
        # 力矩/电流/电压/占空比/阻抗: 无位置保持概念
        # 目标≈0 且速度≈0 → STANDSTILL; 速度下降中 → DECEL; 否则 MOVING
        else:
            # 取当前模式的目标幅值
            if mode == ControlMode.TORQUE:
                tgt = abs(self.target_torque)
            elif mode == ControlMode.CURRENT:
                tgt = max(abs(self.target_id), abs(self.target_iq))
            elif mode == ControlMode.VOLTAGE:
                tgt = abs(self.target_voltage)
            elif mode == ControlMode.DUTY:
                tgt = abs(self.target_duty)
            else:  # IMPEDANCE 或其他
                tgt = abs(self.target_pos)  # 阻抗用位置目标

            if tgt < 1e-6 and abs_vel < VEL_STILL:
                self.run_state = RunState.STANDSTILL
            elif is_decelerating:
                self.run_state = RunState.DECEL
            else:
                self.run_state = RunState.MOVING

        # 记录本拍速度供下拍判断
        self._prev_vel = vel

    # ---------- 目标值管理 ----------
    def _apply_cmd_targets(self, cmd: MotorCmd):
        """应用命令中的目标值到 cmd_target_xxx(命令目标, 非直接生效)。

        命令目标经 _ramp_targets() 斜坡过渡后才写入 target_xxx(生效目标)。
        仅当字段非 None 时更新(支持运动中只改部分目标)。
        """
        if cmd.set_pos is not None:
            self.cmd_target_pos = cmd.set_pos
        if cmd.set_vel is not None:
            self.cmd_target_vel = cmd.set_vel
        if cmd.set_torque is not None:
            self.cmd_target_torque = cmd.set_torque
        if cmd.set_id is not None:
            self.cmd_target_id = cmd.set_id
        if cmd.set_iq is not None:
            self.cmd_target_iq = cmd.set_iq
        if cmd.set_voltage is not None:
            self.cmd_target_voltage = cmd.set_voltage
        if cmd.set_duty is not None:
            self.cmd_target_duty = cmd.set_duty
        # kp/kd/torque_ff/vel_ff 不做斜坡(配置参数, 非控制目标)
        if cmd.set_kp is not None:
            self.target_kp = cmd.set_kp
        if cmd.set_kd is not None:
            self.target_kd = cmd.set_kd
        if cmd.set_torque_ff is not None:
            self.target_torque_ff = cmd.set_torque_ff
        if cmd.set_vel_ff is not None:
            self.target_vel_ff = cmd.set_vel_ff

    def _sync_cmd_to_target(self):
        """命令目标直接同步到生效目标(无斜坡, 用于首次启动或过渡完成)。"""
        self.target_pos = self.cmd_target_pos
        self.target_vel = self.cmd_target_vel
        self.target_torque = self.cmd_target_torque
        self.target_id = self.cmd_target_id
        self.target_iq = self.cmd_target_iq
        self.target_voltage = self.cmd_target_voltage
        self.target_duty = self.cmd_target_duty

    def _ramp_step(self, current: float, target: float, rate: float, dt: float) -> float:
        """单值斜坡: 向 target 逼近, 每拍变化量 <= rate*dt。"""
        delta = target - current
        max_step = rate * dt
        if abs(delta) <= max_step:
            return target
        return current + max_step * (1.0 if delta > 0 else -1.0)

    def _ramp_targets(self, dt: float):
        """斜坡过渡: target_xxx 向 cmd_target_xxx 逼近(每拍调用)。"""
        self.target_pos = self._ramp_step(
            self.target_pos, self.cmd_target_pos, self.ramp_rate_pos, dt)
        self.target_vel = self._ramp_step(
            self.target_vel, self.cmd_target_vel, self.ramp_rate_vel, dt)
        self.target_torque = self._ramp_step(
            self.target_torque, self.cmd_target_torque, self.ramp_rate_torque, dt)
        self.target_id = self._ramp_step(
            self.target_id, self.cmd_target_id, self.ramp_rate_current, dt)
        self.target_iq = self._ramp_step(
            self.target_iq, self.cmd_target_iq, self.ramp_rate_current, dt)
        self.target_voltage = self._ramp_step(
            self.target_voltage, self.cmd_target_voltage, self.ramp_rate_voltage, dt)
        self.target_duty = self._ramp_step(
            self.target_duty, self.cmd_target_duty, self.ramp_rate_duty, dt)

    def _ramp_down_during_transition(self, dt: float):
        """模式切换过渡: 把当前生效目标衰减向0(经斜坡), 避免突变。

        过渡期间仍用当前 control_mode 输出, 但目标逐步归零,
        使电机输出力矩/电流平滑减小, 过渡完成后再切新模式。
        """
        # 衰减速率: 比正常斜坡快3倍, 确保过渡时间内归零
        fast = 3.0
        self.target_pos = self._ramp_step(self.target_pos, 0.0,
                                          self.ramp_rate_pos * fast, dt)
        self.target_vel = self._ramp_step(self.target_vel, 0.0,
                                          self.ramp_rate_vel * fast, dt)
        self.target_torque = self._ramp_step(self.target_torque, 0.0,
                                             self.ramp_rate_torque * fast, dt)
        self.target_id = self._ramp_step(self.target_id, 0.0,
                                         self.ramp_rate_current * fast, dt)
        self.target_iq = self._ramp_step(self.target_iq, 0.0,
                                         self.ramp_rate_current * fast, dt)
        self.target_voltage = self._ramp_step(self.target_voltage, 0.0,
                                              self.ramp_rate_voltage * fast, dt)
        self.target_duty = self._ramp_step(self.target_duty, 0.0,
                                           self.ramp_rate_duty * fast, dt)

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
            # MIT/阻抗控制: tau = kp*(pos_des-pos) + kd*(vel_des-vel) + tff
            # 直接输出力矩, 跳过位置环/速度环 (避免量纲错配:
            #   IMPEDANCE profile 的 output_limit 是力矩 Nm, 却被位置环当作速度 rad/s 限幅)
            pos_err = self.target_pos - fb.get('pos', 0.0)
            vel_err = self.target_vel - fb.get('vel', 0.0)
            torque = self.target_kp * pos_err + self.target_kd * vel_err + self.target_torque_ff
            ref.ctrl_type = RefCtrlType.TORQUE
            ref.torque = torque
        else:
            ref.ctrl_type = RefCtrlType.IDLE

        self.motor_ref = ref

    # ---------- 接口 ----------
    def get_ref(self) -> MotorRef:
        return self.motor_ref

    def reset(self):
        """完全复位状态机(外部 API 与内部 cmd.reset 共用)。"""
        self.sys_state = SystemState.IDLE
        self.run_state = RunState.STANDSTILL
        self.control_mode = ControlMode.NONE
        self.fault_flags = 0
        self.motor_ref = MotorRef(ctrl_type=RefCtrlType.IDLE)
        self._in_safety = False
        # 清零生效目标
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
        # 清零命令目标
        self.cmd_target_pos = 0.0
        self.cmd_target_vel = 0.0
        self.cmd_target_torque = 0.0
        self.cmd_target_id = 0.0
        self.cmd_target_iq = 0.0
        self.cmd_target_voltage = 0.0
        self.cmd_target_duty = 0.0
        # 清除模式切换过渡状态
        self._mode_transition = False
        self._transition_cnt = 0
        self._pending_mode = ControlMode.NONE
        self._pending_targets_applied = False

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
