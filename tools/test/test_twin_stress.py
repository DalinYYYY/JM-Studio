"""多轮随机命令序列压力测试: 模拟上位机随机操作, 检测孪生引擎在
任意命令序列下不崩溃、不产生 NaN/Inf、状态机不卡死。

每轮随机选择: 使能/失能/启动各模式/改目标/停止/故障注入/清除/复位。
"""

import sys
import os
import math

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, '..', '..')))

from transport.virtual_engine.digital_twin import DigitalTwinEngine
from transport.virtual_engine.twin_fsm import (
    MotorCmd, ControlMode, SystemState, Fault)

# 固定种子的伪随机(不依赖 random 模块的全局状态, 可复现)
class LCG:
    def __init__(self, seed): self.s = seed
    def next(self):
        self.s = (self.s * 1103515245 + 12345) & 0x7FFFFFFF
        return self.s
    def choice(self, seq): return seq[self.next() % len(seq)]
    def uniform(self, a, b): return a + (b - a) * (self.next() / 0x7FFFFFFF)


def is_finite(*v):
    return all(math.isfinite(x) for x in v)


def run_round(seed, n_actions=40, steps_per_action=2000, verbose=False):
    rng = LCG(seed)
    e = DigitalTwinEngine()
    cp = max(1, int(0.05 / e.dt_foc))
    step_i = 0
    modes = [ControlMode.POSITION, ControlMode.VELOCITY, ControlMode.TORQUE,
             ControlMode.CURRENT, ControlMode.IMPEDANCE, ControlMode.VOLTAGE,
             ControlMode.DUTY]
    actions = ['enable', 'disable', 'start', 'stop', 'retarget',
               'inject', 'clear', 'reset', 'estop']

    for a in range(n_actions):
        act = rng.choice(actions)
        if act == 'enable':
            e.apply_cmd(MotorCmd(enable=True))
        elif act == 'disable':
            e.apply_cmd(MotorCmd(disable=True))
        elif act == 'start':
            m = rng.choice(modes)
            cmd = MotorCmd(start=True, set_mode=m)
            cmd.set_pos = rng.uniform(-30, 30)
            cmd.set_vel = rng.uniform(-50, 50)
            cmd.set_torque = rng.uniform(-3, 3)
            cmd.set_iq = rng.uniform(-8, 8)
            cmd.set_id = 0.0
            cmd.set_voltage = rng.uniform(-12, 12)
            cmd.set_duty = rng.uniform(-0.8, 0.8)
            cmd.set_kp = rng.uniform(0, 10)
            cmd.set_kd = rng.uniform(0, 0.5)
            e.apply_cmd(cmd)
        elif act == 'stop':
            e.apply_cmd(MotorCmd(stop=True))
        elif act == 'retarget':
            e.apply_cmd(MotorCmd(set_pos=rng.uniform(-30, 30),
                                 set_vel=rng.uniform(-50, 50),
                                 set_torque=rng.uniform(-3, 3)))
        elif act == 'inject':
            e.inject_fault(rng.choice([Fault.OVER_CURRENT, Fault.OVER_TEMP_MOTOR,
                                       Fault.OVER_VOLTAGE]))
        elif act == 'clear':
            e.clear_injected_fault()
            e.apply_cmd(MotorCmd(fault_clear=True))
        elif act == 'reset':
            e.apply_cmd(MotorCmd(reset=True))
        elif act == 'estop':
            e.apply_cmd(MotorCmd(stop=True, disable=True))

        for i in range(steps_per_action):
            if (step_i + i) % cp == 0:
                e.update_comm_heartbeat()
            e.step()
            t = e.get_telemetry()
            if t and not is_finite(t.get('pos', 0), t.get('vel', 0), t.get('iq', 0),
                                   t.get('id', 0), t.get('omega_m', 0), t.get('ud', 0),
                                   t.get('uq', 0), t.get('theta_m', 0), t.get('temp_motor', 0)):
                return False, f"seed={seed} action#{a}({act}): 非有限值 {dict(t)}"
            # 状态机必须始终是合法枚举
            if e.fsm.sys_state not in SystemState:
                return False, f"seed={seed} action#{a}: 非法状态 {e.fsm.sys_state}"
        step_i += steps_per_action
        if verbose:
            t = e.get_telemetry()
            print(f"    #{a:2d} {act:9s} -> {e.fsm.sys_state.name:7s} "
                  f"mode={e.fsm.control_mode.name:9s} omega_m={t.get('omega_m',0):8.2f} "
                  f"iq={t.get('iq',0):7.3f}")
    return True, None


def main():
    n_rounds = 20
    fails = []
    for r in range(n_rounds):
        seed = 1000 + r * 7919
        ok, msg = run_round(seed, n_actions=40, steps_per_action=1500,
                            verbose=(r == 0))
        status = "OK " if ok else "FAIL"
        print(f"[round {r:2d}] seed={seed} {status}")
        if not ok:
            fails.append(msg)
            print("   ", msg)
    print("\n" + "=" * 50)
    print(f"{n_rounds - len(fails)}/{n_rounds} rounds passed")
    for f in fails:
        print("  -", f)
    return 0 if not fails else 1


if __name__ == '__main__':
    sys.exit(main())
