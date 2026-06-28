"""数字孪生引擎: 多速率仿真 + QThread 封装。

整合物理模型(MotorPhysics) + 负载(JointLoad) + 控制(ControllerCore)
+ 状态机(SystemStateMachine), 在独立 QThread 中按设定节拍步进。

多速率结构(对齐固件):
  电流环(FOC): 10kHz, dt_foc = 100us  → 物理积分 + 电流环PI + SVPWM
  速度环:      10kHz(与FOC同频)        → 速度PI → iq_ref
  位置环:      2kHz, dt_pos = 500us    → 位置P → vel_sp (每5个FOC周期)
  状态机:      2kHz(与位置环同频)      → 故障检测 + 状态迁移

线程模型:
  DigitalTwinThread(QThread) 在子线程运行, 用 perf_counter 对齐真实时间;
  通过 apply_cmd() 线程安全地接收命令, 通过 telemetry_ready 信号发出遥测。
"""

import time
import math
import threading
from dataclasses import dataclass
from typing import Optional

try:
    from PyQt6.QtCore import QThread, pyqtSignal
    _HAS_PYQT = True
except ImportError:
    _HAS_PYQT = False
    class _Dummy:
        def __init__(self, *a, **kw): pass
    QThread = _Dummy
    def pyqtSignal(*a, **kw):
        return None

from .twin_config import MotorParam, load_motor_param
from .twin_physics import MotorPhysics
from .twin_load import JointLoad
from .twin_control import ControllerCore, CascadeFeedback, RefCtrlType, MotorRef
from .twin_fsm import (SystemStateMachine, FaultDetector, MotorCmd,
                        ControlMode, Fault, SystemState, RunState)


# ==================== 仿真引擎(纯逻辑) ====================
class DigitalTwinEngine:
    """数字孪生仿真引擎, 整合物理/负载/控制/状态机。

    单步接口: step() 推进一个 FOC 周期(dt_foc)。
    外部通过 apply_cmd() 投递命令, get_telemetry() 读取遥测。
    """

    def __init__(self, mp: Optional[MotorParam] = None,
                 foc_freq: float = 10000.0, pos_ratio: int = 5):
        """
        Args:
            mp: 电机参数(为None则从CSV加载)
            foc_freq: 电流环频率 Hz (默认10kHz)
            pos_ratio: 位置环 = 电流环 / pos_ratio (默认5 → 2kHz)
        """
        self.mp = mp or load_motor_param()
        self.dt_foc = 1.0 / foc_freq
        self.foc_freq = foc_freq
        self.pos_ratio = max(1, pos_ratio)

        self.physics = MotorPhysics(self.mp)
        self.load = JointLoad(self.mp.gearbox_param.gear_ratio)
        self.controller = ControllerCore(self.mp, self.dt_foc, self.pos_ratio)
        self.fsm = SystemStateMachine(self.mp)
        self.fault_detector = FaultDetector(self.mp)

        # 运行时
        self.t_sim = 0.0          # 仿真时间
        self._foc_count = 0
        self._cmd_queue = []       # 待处理命令队列
        self._cmd_lock = threading.Lock()
        self._current_cmd = MotorCmd()
        self._last_telemetry = {}
        self._follow_err = 0.0
        self._prev_ctrl_type = RefCtrlType.IDLE   # 上一拍参考类型(检测进入 IDLE 边沿)
        self._ref_pos = 0.0       # 位置环参考轨迹位置(vel_setpoint 积分, 电机端 rad)

    # ---------- 主步进 ----------
    def step(self):
        """推进一个 FOC 周期。"""
        dt = self.dt_foc

        # 0. 取出最新命令(线程安全)
        with self._cmd_lock:
            cmd = self._current_cmd
            self._current_cmd = MotorCmd()  # 命令消费后清零(一次性命令)

        # 0.5 故障屏蔽设置(在 check 之前生效, 当拍即应用)
        if cmd.set_fault_disable is not None:
            self.fault_detector.set_disable_mask(cmd.set_fault_disable)

        # 1. 取物理反馈
        fb_phys = self.physics.snapshot()
        # 跟随误差(位置模式): 实际位置 vs 位置环参考轨迹位置(_ref_pos), 而非终点目标。
        #   位置环输出速度设定受 max_speed 限幅, 大行程时终点目标与实际位置的瞬时差
        #   可达数百 rad, 若用终点目标算跟随误差会在正常加速段误触发 FOLLOW_ERROR。
        #   固件跟随误差同样针对插补/限速后的期望轨迹, 不是最终目标。
        #   控制环在电机端(theta_m), _ref_pos 亦为电机端积分。
        if self.fsm.control_mode == ControlMode.POSITION:
            self._follow_err = self._ref_pos - fb_phys['theta_m']
        else:
            self._follow_err = 0.0
            self._ref_pos = fb_phys['theta_m']   # 非位置模式: 参考位置跟随实际, 切回时无突跳
        fb_phys['follow_err'] = self._follow_err

        # 2. 故障检测(每个FOC周期)
        fault_flags = self.fault_detector.check(fb_phys, self.t_sim, dt)

        # 3. 状态机(每个FOC周期, 轻量)
        self.fsm.step(cmd, fault_flags, fb_phys, self.t_sim, dt)

        # 4. 取控制参考
        ref = self.fsm.get_ref()

        # 4.5 进入 IDLE 边沿(失能/停止/故障/安全): 清零控制器 PID 积分状态。
        #   IDLE 态 ControllerCore.run 直接返回 (0,0), FOC/级联 PID 不再被调用,
        #   积分项会冻结残留; 若不清零, 下次启动残留积分立即产生冲击电流/力矩。
        #   对齐真实驱动器: 失能(关断功率级)时电流环积分复位。
        if ref.ctrl_type == RefCtrlType.IDLE and self._prev_ctrl_type != RefCtrlType.IDLE:
            self.controller.reset()
        # 进入 POSITION 模式边沿: 参考轨迹位置对齐当前实际位置, 从当前点起算跟随误差,
        #   避免从其它模式切入时 _ref_pos 残留导致首拍跟随误差突跳误报。
        if ref.ctrl_type == RefCtrlType.POSITION and self._prev_ctrl_type != RefCtrlType.POSITION:
            self._ref_pos = fb_phys['theta_m']
        self._prev_ctrl_type = ref.ctrl_type

        # 5. 控制器: 位置环分频, 速度环+电流环
        # 控制环反馈用电机端(theta_m/omega_m), 对齐固件:
        #   fb.pos = multiturn.get_position = theta_m (电机端多圈 rad)
        #   fb.vel = slide_rad_s = omega_m (电机端 rad/s)
        # 固件参数(speed_kp/max_speed/position_kp)均按电机端标定,
        # 用输出端(pos_out/vel_out)会导致带宽失配(gear_ratio 倍)→ 震荡/过流
        fb_cascade = CascadeFeedback(
            pos=fb_phys['theta_m'], vel=fb_phys['omega_m'],
            id=fb_phys['id'], iq=fb_phys['iq'])
        ud, uq = self.controller.run(ref, fb_cascade)

        # 5.5 位置环参考轨迹位置积分(供跟随误差判定)。
        #   位置模式下 vel_setpoint 是位置环限速后的期望速度, 积分即期望轨迹位置。
        #   进入 IDLE(失能/停止/故障/安全)时 _ref_pos 重新对齐实际位置, 避免残留误差。
        if ref.ctrl_type == RefCtrlType.POSITION:
            self._ref_pos += self.controller.cascade.vel_setpoint * dt
        elif ref.ctrl_type == RefCtrlType.IDLE:
            self._ref_pos = fb_phys['theta_m']

        # 6. 负载模型
        t_load_motor, J_load = self.load.update(fb_phys['pos'], fb_phys['vel_out'], dt)
        self.physics.set_load_inertia(J_load)

        # 7. 物理积分
        self.physics.step(dt, ud, uq, t_load_motor)

        self.t_sim += dt
        self._foc_count += 1
        self._last_telemetry = self._build_telemetry(fb_phys, ud, uq, t_load_motor)

    def _build_telemetry(self, fb_phys: dict, ud: float, uq: float, t_load: float) -> dict:
        fsm_snap = self.fsm.snapshot()
        load_snap = self.load.snapshot()
        return {
            # 状态
            'sys_state': fsm_snap['sys_state'],
            'sys_state_name': fsm_snap['sys_state_name'],
            'run_state': fsm_snap['run_state'],
            'run_state_name': fsm_snap['run_state_name'],
            'control_mode': fsm_snap['control_mode'],
            'control_mode_name': fsm_snap['control_mode_name'],
            'fault_flags': fsm_snap['fault_flags'],
            'fault_str': fsm_snap['fault_str'],
            # 物理量: pos/vel 用电机端(theta_m/omega_m), 对齐固件 READ_FEEDBACK
            #   (固件 fb.pos = multiturn = theta_m, fb.vel = slide_rad_s = omega_m)
            #   与控制环反馈/转子视图(single 电机端单圈)/multiturn 全部同坐标系,
            #   避免输出端(gear_ratio 倍缩放)与电机端混用导致可视化不同步
            'pos': fb_phys['theta_m'], 'vel': fb_phys['omega_m'],
            'pos_out': fb_phys['pos'], 'vel_out': fb_phys['vel_out'],
            'torque': fb_phys['torque'], 'torque_out': fb_phys['torque_out'],
            'id': fb_phys['id'], 'iq': fb_phys['iq'],
            'ia': fb_phys['ia'], 'ib': fb_phys['ib'], 'ic': fb_phys['ic'],
            'vbus': fb_phys['vbus'], 'ibus': fb_phys['ibus'],
            'power': fb_phys['power'],
            'temp_fet': fb_phys['temp_fet'], 'temp_motor': fb_phys['temp_motor'],
            'multiturn': fb_phys['multiturn'], 'single': fb_phys['single'],
            'theta_m': fb_phys['theta_m'], 'omega_m': fb_phys['omega_m'],
            'enc_theta': fb_phys['enc_theta'], 'enc_vel': fb_phys['enc_vel'],
            # 控制量
            'ud': ud, 'uq': uq,
            'id_ref': self.controller.last_id_ref, 'iq_ref': self.controller.last_iq_ref,
            'id_meas': self.controller.foc.foc.id, 'iq_meas': self.controller.foc.foc.iq,
            'vel_setpoint': self.controller.cascade.vel_setpoint,
            # 负载
            't_load': t_load, 't_gravity': load_snap['t_gravity'],
            't_external': load_snap['t_external'], 't_collision': load_snap['t_collision'],
            'in_collision': load_snap['in_collision'], 'J_load': load_snap['J_load'],
            # 时间
            't_sim': self.t_sim, 'foc_count': self._foc_count,
            'follow_err': self._follow_err,
        }

    # ---------- 命令接口 ----------
    def apply_cmd(self, cmd: MotorCmd):
        """线程安全地投递命令。

        事件型(enable/start/stop/...)合并为True; 目标型(set_pos/...)仅在非None时覆盖。
        """
        with self._cmd_lock:
            c = self._current_cmd
            if cmd.enable: c.enable = True
            if cmd.disable: c.disable = True
            if cmd.start: c.start = True
            if cmd.stop: c.stop = True
            if cmd.fault_clear: c.fault_clear = True
            if cmd.reset: c.reset = True
            if cmd.set_mode != ControlMode.NONE: c.set_mode = cmd.set_mode
            if cmd.set_pos is not None: c.set_pos = cmd.set_pos
            if cmd.set_vel is not None: c.set_vel = cmd.set_vel
            if cmd.set_torque is not None: c.set_torque = cmd.set_torque
            if cmd.set_id is not None: c.set_id = cmd.set_id
            if cmd.set_iq is not None: c.set_iq = cmd.set_iq
            if cmd.set_voltage is not None: c.set_voltage = cmd.set_voltage
            if cmd.set_duty is not None: c.set_duty = cmd.set_duty
            if cmd.set_kp is not None: c.set_kp = cmd.set_kp
            if cmd.set_kd is not None: c.set_kd = cmd.set_kd
            if cmd.set_torque_ff is not None: c.set_torque_ff = cmd.set_torque_ff
            if cmd.set_vel_ff is not None: c.set_vel_ff = cmd.set_vel_ff
            if cmd.set_fault_disable is not None: c.set_fault_disable = cmd.set_fault_disable
        # 更新通信心跳
        self.fault_detector.update_comm(self.t_sim)

    def update_comm_heartbeat(self):
        """更新通信心跳(防 COMM_LOST)。"""
        self.fault_detector.update_comm(self.t_sim)

    def reload_params(self):
        """参数写入后重新加载控制器 PID 配置(实时生效)。

        仅刷新 PID 增益/限幅副本, 保留运行时积分状态, 可运行中安全调用。
        物理(电机本体)参数为固有只读量, 不在此刷新。
        """
        self.controller.reload_params()

    def rebuild(self):
        """重建物理模型/控制器/状态机/故障检测器 (保留 mp 引用)。

        用于导入给定参数(R/Ld/惯量等)后, 让物理模型用新参数生效。
        会重置电机运行状态(位置/速度/电流归零), 但保留 MotorParam 对象引用。
        """
        from .twin_physics import MotorPhysics
        from .twin_control import ControllerCore
        from .twin_fsm import SystemStateMachine, FaultDetector
        self.physics = MotorPhysics(self.mp)
        self.load = JointLoad(self.mp.gearbox_param.gear_ratio)
        self.controller = ControllerCore(self.mp, self.dt_foc, self.pos_ratio)
        self.fsm = SystemStateMachine(self.mp)
        self.fault_detector = FaultDetector(self.mp)
        # 运行时状态归零
        self.t_sim = 0.0
        self._foc_count = 0
        with self._cmd_lock:
            self._current_cmd = MotorCmd()
        self._last_telemetry = {}
        self._follow_err = 0.0

    # ---------- 故障注入 ----------
    def inject_fault(self, fault_bits: int):
        """注入故障(演示用)。"""
        self.fault_detector.inject(fault_bits)

    def clear_injected_fault(self):
        """清除注入的故障。"""
        self.fault_detector.clear_injected()

    # ---------- 故障屏蔽 ----------
    def set_fault_disable_mask(self, mask: int):
        """设置故障屏蔽掩码(位1=对应故障不触发)。"""
        self.fault_detector.set_disable_mask(mask)

    def get_fault_disable_mask(self) -> int:
        """获取当前故障屏蔽掩码。"""
        return self.fault_detector.get_disable_mask()

    # ---------- 负载配置 ----------
    def configure_load(self, mass: float = None, length: float = None,
                        tip_mass: float = None, tip_length: float = None,
                        gravity: float = None):
        """配置连杆负载参数。"""
        if mass is not None: self.load.link.mass = mass
        if length is not None: self.load.link.length = length
        if tip_mass is not None: self.load.tip_mass = tip_mass
        if tip_length is not None: self.load.tip_length = tip_length
        if gravity is not None: self.load.link.gravity = gravity

    def apply_external_torque(self, torque: float):
        """施加外部力矩(输出端, Nm), 如示教力。"""
        self.load.apply_external_torque(torque)

    def apply_disturbance(self, amplitude: float, freq: float):
        """施加正弦扰动(演示用)。"""
        self.load.apply_disturbance(amplitude, freq)

    # ---------- 遥测接口 ----------
    def get_telemetry(self) -> dict:
        return self._last_telemetry

    # ---------- 复位 ----------
    def reset(self):
        """复位整个孪生到初始状态。"""
        self.physics.reset()
        self.load.reset()
        self.controller.reset()
        self.fsm.reset()
        self.fault_detector.clear_injected()
        self.fault_detector.set_disable_mask(0)   # 清除故障屏蔽
        self.t_sim = 0.0
        self._foc_count = 0
        self._follow_err = 0.0
        with self._cmd_lock:
            self._current_cmd = MotorCmd()


# ==================== QThread 封装 ====================
class DigitalTwinThread(QThread):
    """数字孪生仿真线程。

    在子线程中按设定节拍调用 engine.step(), 通过信号发出遥测。
    支持实时模式(对齐真实时间)和加速模式(虚拟时间)。

    信号:
        telemetry_ready(dict): 每个遥测周期发出一次遥测
        state_changed(str, str): 系统状态/运行状态变化
    """

    telemetry_ready = pyqtSignal(dict)
    state_changed = pyqtSignal(str, str)
    fault_occurred = pyqtSignal(int, str)

    def __init__(self, mp: Optional[MotorParam] = None,
                 foc_freq: float = 10000.0, parent=None):
        super().__init__(parent)
        self.engine = DigitalTwinEngine(mp, foc_freq=foc_freq)
        self._running = False
        self._realtime = True         # True=实时模式, False=最快虚拟时间
        self._speed_factor = 1.0      # 仿真速度倍率(1.0=实时, 2.0=2倍速)
        # 遥测降采样: 每 N 个 FOC 周期发一次遥测(避免信号过载)
        self._telem_decimation = max(1, int(foc_freq / 200))  # 默认 ~200Hz 遥测
        self._prev_sys_state = -1
        self._prev_fault = 0

    def run(self):
        """QThread 主循环。"""
        self._running = True
        self._prev_sys_state = self.engine.fsm.sys_state
        next_t = time.perf_counter()
        dt_wall_target = self.engine.dt_foc / max(self._speed_factor, 0.01)

        while self._running:
            # 执行一个FOC周期
            self.engine.step()

            # 遥测降采样发送
            count = self.engine._foc_count
            if count % self._telem_decimation == 0:
                telem = self.engine.get_telemetry()
                self.telemetry_ready.emit(telem)

            # 状态变化信号
            cur_state = self.engine.fsm.sys_state
            if cur_state != self._prev_sys_state:
                self.state_changed.emit(
                    self._prev_sys_state.name if isinstance(self._prev_sys_state, SystemState) else str(self._prev_sys_state),
                    cur_state.name)
                self._prev_sys_state = cur_state

            # 故障信号
            cur_fault = self.engine.fsm.fault_flags
            if cur_fault != self._prev_fault and cur_fault != 0:
                self.fault_occurred.emit(cur_fault, Fault.to_str(cur_fault))
                self._prev_fault = cur_fault
            elif cur_fault == 0:
                self._prev_fault = 0

            # 实时对齐
            if self._realtime:
                next_t += dt_wall_target
                now = time.perf_counter()
                sleep_time = next_t - now
                if sleep_time > 0:
                    self.msleep(int(sleep_time * 1000))
                elif sleep_time < -dt_wall_target * 10:
                    # 落后太多, 重置基准
                    next_t = time.perf_counter()
            # 非实时模式: 不 sleep, 尽快步进

    def stop(self):
        """停止仿真线程。"""
        self._running = False
        self.wait(2000)

    # ---------- 配置接口 ----------
    def set_realtime(self, realtime: bool):
        self._realtime = realtime

    def set_speed(self, factor: float):
        """设置仿真速度倍率(1.0=实时)。"""
        self._speed_factor = max(0.01, factor)

    def set_telemetry_rate(self, rate_hz: float):
        """设置遥测发送频率。"""
        self._telem_decimation = max(1, int(self.engine.foc_freq / rate_hz))

    # ---------- 命令转发 ----------
    def apply_cmd(self, cmd: MotorCmd):
        self.engine.apply_cmd(cmd)

    def update_comm_heartbeat(self):
        self.engine.update_comm_heartbeat()

    def inject_fault(self, fault_bits: int):
        self.engine.inject_fault(fault_bits)

    def clear_injected_fault(self):
        self.engine.clear_injected_fault()

    def set_fault_disable_mask(self, mask: int):
        self.engine.set_fault_disable_mask(mask)

    def get_fault_disable_mask(self) -> int:
        return self.engine.get_fault_disable_mask()

    def configure_load(self, **kwargs):
        self.engine.configure_load(**kwargs)

    def apply_external_torque(self, torque: float):
        self.engine.apply_external_torque(torque)

    def apply_disturbance(self, amplitude: float, freq: float):
        self.engine.apply_disturbance(amplitude, freq)

    def reset(self):
        self.engine.reset()

    def get_telemetry(self) -> dict:
        return self.engine.get_telemetry()

    def get_engine(self) -> DigitalTwinEngine:
        return self.engine
