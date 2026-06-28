"""数字孪生引擎控制链路 + 状态机的行为级测试 harness。

不依赖 Qt, 直接驱动 DigitalTwinEngine, 模拟上位机命令序列,
检测异常: NaN/Inf、数值发散、超速、状态卡死、震荡、跟随误差等。

用法:
    python tools/test/test_twin_engine.py
"""

import sys
import os
import math

# 终端中文输出: 强制 UTF-8, 避免 GBK 控制台乱码
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, '..', '..')))

from transport.virtual_engine.digital_twin import DigitalTwinEngine
from transport.virtual_engine.twin_fsm import (
    MotorCmd, ControlMode, SystemState, RunState, Fault)


PASS = 0
FAIL = 0
_FAILS = []


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        _FAILS.append(msg)
        print(f"  [FAIL] {msg}")


def is_finite(*vals):
    return all(math.isfinite(v) for v in vals)


def run_steps(engine, n):
    """推进 n 个 FOC 周期, 返回最后一拍遥测; 全程检测 NaN/Inf。

    模拟真实链路(TwinResponder.advance): 周期性更新通信心跳, 避免误触发 COMM_LOST。
    """
    bad = None
    comm_period = max(1, int(0.05 / engine.dt_foc))  # 每 50ms 更新心跳
    for i in range(n):
        if i % comm_period == 0:
            engine.update_comm_heartbeat()
        engine.step()
        t = engine.get_telemetry()
        if t and not is_finite(t.get('pos', 0), t.get('vel', 0), t.get('iq', 0),
                               t.get('id', 0), t.get('omega_m', 0), t.get('ud', 0),
                               t.get('uq', 0)):
            bad = dict(t)
            break
    return engine.get_telemetry(), bad


def fresh_engine():
    return DigitalTwinEngine()


# ============================================================
def test_enable_start_position():
    """位置模式: 使能->启动->目标位置, 应收敛到目标且无 NaN。"""
    print("[TEST] 位置模式收敛")
    e = fresh_engine()
    e.apply_cmd(MotorCmd(enable=True))
    run_steps(e, 10)
    check(e.fsm.sys_state == SystemState.READY, f"使能后应 READY, got {e.fsm.sys_state.name}")

    # 启动位置模式, 目标 5 rad (电机端)
    e.apply_cmd(MotorCmd(start=True, set_mode=ControlMode.POSITION, set_pos=5.0))
    t, bad = run_steps(e, 30000)   # 3s
    check(bad is None, f"位置模式出现非有限值: {bad}")
    check(e.fsm.sys_state in (SystemState.RUN, SystemState.SAFETY),
          f"运行中状态异常: {e.fsm.sys_state.name}")
    if e.fsm.sys_state == SystemState.RUN:
        err = abs(t['pos'] - 5.0)
        check(err < 0.5, f"位置未收敛: target=5.0 actual={t['pos']:.3f} err={err:.3f}")
    print(f"    pos={t['pos']:.3f} vel={t['vel']:.3f} iq={t['iq']:.3f} state={e.fsm.sys_state.name}")


def test_velocity_mode():
    """速度模式: 应收敛到目标速度。"""
    print("[TEST] 速度模式收敛")
    e = fresh_engine()
    e.apply_cmd(MotorCmd(enable=True))
    run_steps(e, 10)
    e.apply_cmd(MotorCmd(start=True, set_mode=ControlMode.VELOCITY, set_vel=20.0))
    t, bad = run_steps(e, 20000)
    check(bad is None, f"速度模式非有限值: {bad}")
    if e.fsm.sys_state == SystemState.RUN:
        err = abs(t['vel'] - 20.0)
        check(err < 3.0, f"速度未收敛: target=20 actual={t['vel']:.3f}")
    print(f"    vel={t['vel']:.3f} iq={t['iq']:.3f} state={e.fsm.sys_state.name}")


def test_torque_mode_no_overspeed():
    """力矩模式: 空载施加力矩, 速度软限位应防止超速发散/触发 OVER_SPEED。"""
    print("[TEST] 力矩模式速度软限位")
    e = fresh_engine()
    e.apply_cmd(MotorCmd(enable=True))
    run_steps(e, 10)
    e.apply_cmd(MotorCmd(start=True, set_mode=ControlMode.TORQUE, set_torque=2.0))
    t, bad = run_steps(e, 30000)
    check(bad is None, f"力矩模式非有限值: {bad}")
    check(not (e.fsm.fault_flags & Fault.OVER_SPEED),
          f"力矩模式触发了 OVER_SPEED (软限位失效): omega_m={t['omega_m']:.1f}")
    check(abs(t['omega_m']) < e.mp.motor_base.max_speed * 1.3,
          f"电机端速度超机械限: {t['omega_m']:.1f}")
    print(f"    omega_m={t['omega_m']:.2f} iq={t['iq']:.3f} fault={Fault.to_str(e.fsm.fault_flags)}")


def test_current_mode():
    """电流模式: 给定 iq_ref。"""
    print("[TEST] 电流模式")
    e = fresh_engine()
    e.apply_cmd(MotorCmd(enable=True))
    run_steps(e, 10)
    e.apply_cmd(MotorCmd(start=True, set_mode=ControlMode.CURRENT, set_iq=3.0, set_id=0.0))
    t, bad = run_steps(e, 20000)
    check(bad is None, f"电流模式非有限值: {bad}")
    print(f"    iq={t['iq']:.3f} id={t['id']:.3f} omega_m={t['omega_m']:.2f} fault={Fault.to_str(e.fsm.fault_flags)}")


def test_mode_switch():
    """运行中模式切换: position -> velocity -> torque, 应平滑无 NaN 无卡死。"""
    print("[TEST] 运行中模式切换")
    e = fresh_engine()
    e.apply_cmd(MotorCmd(enable=True))
    run_steps(e, 10)
    e.apply_cmd(MotorCmd(start=True, set_mode=ControlMode.POSITION, set_pos=2.0))
    _, b1 = run_steps(e, 10000)
    e.apply_cmd(MotorCmd(set_mode=ControlMode.VELOCITY, set_vel=10.0))
    _, b2 = run_steps(e, 10000)
    e.apply_cmd(MotorCmd(set_mode=ControlMode.TORQUE, set_torque=1.0))
    t, b3 = run_steps(e, 10000)
    check(b1 is None and b2 is None and b3 is None, "模式切换中出现非有限值")
    check(e.fsm.control_mode == ControlMode.TORQUE,
          f"最终模式应为 TORQUE, got {e.fsm.control_mode.name}")
    print(f"    final mode={e.fsm.control_mode.name} state={e.fsm.sys_state.name} omega_m={t['omega_m']:.2f}")


def test_position_limit_safety():
    """位置超限: 目标超出软限位, 应进入 SAFETY 而非发散。"""
    print("[TEST] 位置限位->SAFETY")
    e = fresh_engine()
    e.apply_cmd(MotorCmd(enable=True))
    run_steps(e, 10)
    # pos_max=12.566 输出端; gear=100 => 电机端目标需要 *100 才能到限位
    big = e.mp.position_limit.pos_max_limit * e.mp.gearbox_param.gear_ratio * 2
    e.apply_cmd(MotorCmd(start=True, set_mode=ControlMode.POSITION, set_pos=big))
    t, bad = run_steps(e, 80000)
    check(bad is None, f"超限位运动出现非有限值: {bad}")
    # 超大目标会一路加速到输出端限位, 触发 SAFETY (限位保护), 不应是 FOLLOW_ERROR 误报
    check(e.fsm.sys_state == SystemState.SAFETY or
          (e.fsm.fault_flags & Fault.POS_LIMIT),
          f"超限位应进入 SAFETY, got {e.fsm.sys_state.name} fault={Fault.to_str(e.fsm.fault_flags)}")
    print(f"    pos_out={t['pos_out']:.3f} limit={e.mp.position_limit.pos_max_limit} "
          f"state={e.fsm.sys_state.name}")


def test_position_large_target_no_false_follow_error():
    """限位内的大行程位置目标: 正常加速段不应误触发 FOLLOW_ERROR。"""
    print("[TEST] 大行程位置目标无跟随误差误报")
    e = fresh_engine()
    e.apply_cmd(MotorCmd(enable=True))
    run_steps(e, 10)
    # 电机端 50 rad (输出端 0.5 rad, 远在限位内), 大行程
    e.apply_cmd(MotorCmd(start=True, set_mode=ControlMode.POSITION, set_pos=50.0))
    t, bad = run_steps(e, 60000)
    check(bad is None, f"大行程位置出现非有限值: {bad}")
    check(not (e.fsm.fault_flags & Fault.FOLLOW_ERROR),
          f"限位内大目标误触发 FOLLOW_ERROR: follow_err={t['follow_err']:.2f}")
    check(e.fsm.sys_state == SystemState.RUN,
          f"应保持 RUN, got {e.fsm.sys_state.name} fault={Fault.to_str(e.fsm.fault_flags)}")
    err = abs(t['theta_m'] - 50.0)
    check(err < 0.5, f"大行程位置未收敛: target=50 theta_m={t['theta_m']:.3f}")
    print(f"    theta_m={t['theta_m']:.3f} follow_err={t['follow_err']:.3f} state={e.fsm.sys_state.name}")


def test_follow_error_real_stall():
    """真实堵转(超大负载): 跟随误差检测仍应正确触发 FOLLOW_ERROR。"""
    print("[TEST] 真实堵转触发 FOLLOW_ERROR")
    e = fresh_engine()
    e.apply_cmd(MotorCmd(enable=True))
    run_steps(e, 10)
    e.configure_load(mass=500.0, length=2.0)   # 超大负载, 电机带不动
    e.apply_cmd(MotorCmd(start=True, set_mode=ControlMode.POSITION, set_pos=50.0))
    t, bad = run_steps(e, 60000)
    check(bad is None, f"堵转出现非有限值: {bad}")
    check(e.fsm.sys_state == SystemState.FAULT,
          f"堵转应进入 FAULT, got {e.fsm.sys_state.name}")
    print(f"    fault={Fault.to_str(e.fsm.fault_flags)} follow_err={t['follow_err']:.2f}")


def test_impedance_mode_coordinate():
    """阻抗/MIT 模式: 目标位置是电机端量, theta_m 应收敛到目标(坐标系一致)。"""
    print("[TEST] 阻抗模式坐标系")
    e = fresh_engine()
    e.apply_cmd(MotorCmd(enable=True))
    run_steps(e, 10)
    e.apply_cmd(MotorCmd(start=True, set_mode=ControlMode.IMPEDANCE,
                         set_pos=1.0, set_kp=8.0, set_kd=0.2))
    t, bad = run_steps(e, 40000)
    check(bad is None, f"阻抗模式出现非有限值: {bad}")
    # theta_m 应收敛到目标 1.0 附近 (重力/摩擦下有稳态误差, 放宽到 0.5)
    err = abs(t['theta_m'] - 1.0)
    check(err < 0.5, f"阻抗模式 theta_m 未收敛到电机端目标: target=1.0 theta_m={t['theta_m']:.4f}")
    print(f"    theta_m={t['theta_m']:.4f} pos_out={t['pos_out']:.5f} err={err:.4f}")


def test_fault_recovery():
    """故障注入 -> FAULT -> 清除 -> 应可恢复。"""
    print("[TEST] 故障注入与恢复")
    e = fresh_engine()
    e.apply_cmd(MotorCmd(enable=True))
    run_steps(e, 10)
    e.apply_cmd(MotorCmd(start=True, set_mode=ControlMode.VELOCITY, set_vel=10.0))
    run_steps(e, 5000)
    e.inject_fault(Fault.OVER_CURRENT)
    run_steps(e, 100)
    check(e.fsm.sys_state == SystemState.FAULT, f"注入故障后应 FAULT, got {e.fsm.sys_state.name}")
    e.clear_injected_fault()
    e.apply_cmd(MotorCmd(fault_clear=True))
    run_steps(e, 100)
    check(e.fsm.sys_state == SystemState.IDLE, f"清除故障后应 IDLE, got {e.fsm.sys_state.name}")
    print(f"    recovered state={e.fsm.sys_state.name}")


def test_disable_stops():
    """失能: 运行中失能应回 IDLE 且电机停转。"""
    print("[TEST] 运行中失能")
    e = fresh_engine()
    e.apply_cmd(MotorCmd(enable=True))
    run_steps(e, 10)
    e.apply_cmd(MotorCmd(start=True, set_mode=ControlMode.VELOCITY, set_vel=30.0))
    run_steps(e, 10000)
    e.apply_cmd(MotorCmd(disable=True))
    t, bad = run_steps(e, 20000)
    check(bad is None, "失能后非有限值")
    check(e.fsm.sys_state == SystemState.IDLE, f"失能后应 IDLE, got {e.fsm.sys_state.name}")
    print(f"    state={e.fsm.sys_state.name} omega_m={t['omega_m']:.3f}")


def test_run_state_transitions():
    """运动子状态: 位置模式到位后应进入 HOLDING。"""
    print("[TEST] 运动子状态 HOLDING")
    e = fresh_engine()
    e.apply_cmd(MotorCmd(enable=True))
    run_steps(e, 10)
    e.apply_cmd(MotorCmd(start=True, set_mode=ControlMode.POSITION, set_pos=3.0))
    saw_moving = False
    saw_holding = False
    comm_period = max(1, int(0.05 / e.dt_foc))
    for i in range(40000):
        if i % comm_period == 0:
            e.update_comm_heartbeat()
        e.step()
        rs = e.fsm.run_state
        if rs == RunState.MOVING:
            saw_moving = True
        if rs == RunState.HOLDING:
            saw_holding = True
    check(saw_moving, "位置运动过程未出现 MOVING")
    check(saw_holding, "位置到位后未出现 HOLDING")
    print(f"    saw_moving={saw_moving} saw_holding={saw_holding} final={e.fsm.run_state.name}")


def test_telemetry_ref_labels():
    """遥测 iq_ref/id_ref 应为电流环参考值(非实测滤波值)。"""
    print("[TEST] 遥测参考标签正确性")
    e = fresh_engine()
    e.apply_cmd(MotorCmd(enable=True))
    run_steps(e, 10)
    e.apply_cmd(MotorCmd(start=True, set_mode=ControlMode.CURRENT, set_iq=5.0, set_id=0.0))
    t, bad = run_steps(e, 500)
    check(bad is None, "电流模式非有限值")
    check(abs(t['iq_ref'] - 5.0) < 0.01,
          f"iq_ref 应为参考值 5.0, got {t['iq_ref']:.3f} (可能误用了实测值)")
    check('iq_meas' in t and 'id_meas' in t, "遥测应包含 iq_meas/id_meas 实测字段")
    print(f"    iq_ref={t['iq_ref']:.3f} iq_meas={t['iq_meas']:.3f} id_ref={t['id_ref']:.3f}")


def test_no_integral_windup_after_disable():
    """失能后控制器 PID 积分应清零, 避免重启冲击。"""
    print("[TEST] 失能后积分清零")
    e = fresh_engine()
    e.apply_cmd(MotorCmd(enable=True))
    run_steps(e, 10)
    e.apply_cmd(MotorCmd(start=True, set_mode=ControlMode.VELOCITY, set_vel=50.0))
    run_steps(e, 10000)
    e.apply_cmd(MotorCmd(disable=True))
    run_steps(e, 200)
    check(abs(e.controller.foc.pid_iq.integral) < 1e-6,
          f"失能后 FOC iq 积分应清零, got {e.controller.foc.pid_iq.integral:.4f}")
    check(abs(e.controller.cascade.pid_vel.integral) < 1e-6,
          f"失能后速度环积分应清零, got {e.controller.cascade.pid_vel.integral:.4f}")
    print(f"    iq积分={e.controller.foc.pid_iq.integral:.6f} "
          f"vel积分={e.controller.cascade.pid_vel.integral:.6f}")


def main():
    tests = [
        test_enable_start_position,
        test_velocity_mode,
        test_torque_mode_no_overspeed,
        test_current_mode,
        test_mode_switch,
        test_position_limit_safety,
        test_position_large_target_no_false_follow_error,
        test_follow_error_real_stall,
        test_impedance_mode_coordinate,
        test_telemetry_ref_labels,
        test_no_integral_windup_after_disable,
        test_fault_recovery,
        test_disable_stops,
        test_run_state_transitions,
    ]
    for t in tests:
        try:
            t()
        except Exception as ex:
            import traceback
            global FAIL
            FAIL += 1
            _FAILS.append(f"{t.__name__} 抛异常: {ex}")
            print(f"  [EXCEPTION] {t.__name__}: {ex}")
            traceback.print_exc()

    print("\n" + "=" * 50)
    print(f"PASS={PASS}  FAIL={FAIL}")
    if _FAILS:
        print("失败项:")
        for f in _FAILS:
            print("  -", f)
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
