"""电机可视化面板 (motor_view_panel) 重构回归测试。

针对关节电机数字孪生大屏风格重构做精准回归:
  1. 归一化阈值从 engine.mp 读取 (保留)
  2. 阈值跟随场景切换 (保留)
  3. 母线仪表阈值从 protection_param 读取 (保留)
  4. 无 engine 时回退默认值 (保留)
  5. _tick 在 power != 0 / vel != 0 时置 dirty (粒子+转子动画)
  6. 无 if False 死代码 / 无 QPixmap 引用
  7. paintEvent 全状态无异常
  8. 加速度差分逻辑保持
  9. 功率历史缓冲 + 告警上升沿检测
 10. 转子角度跟随 vel 推进
 11. 5 分区布局 (顶部状态条/左侧KPI/中间3D电机/右侧告警曲线/底部数据表)
 12. 减速比从 engine.mp.gearbox_param 读取

用法: python -m tools.test.test_motor_view_refactor
"""
from __future__ import annotations

import os
import sys
import inspect
import math

# 让脚本可直接运行
if __name__ == '__main__' and __package__ is None:
    _here = os.path.dirname(os.path.abspath(__file__))
    _root = os.path.dirname(os.path.dirname(_here))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    __package__ = 'tools.test'

# 必须在 QApplication 之前
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from ui.theme import theme
theme.set('dark')
app.setStyleSheet(theme.qss())

from transport.virtual_engine import MotorCmd, ControlMode, Fault
from transport.virtual_engine.digital_twin import DigitalTwinEngine
from tools.twin.motor_view_panel import (
    MotorViewPanel, _STATE_STYLE,
    _FAULT_BITS, _DATA_ROWS,
    _FALLBACK_PEAK_CURRENT, _FALLBACK_MAX_SPEED,
    _FALLBACK_V_RATED, _FALLBACK_V_OVER, _FALLBACK_V_UNDER,
)


PASS = 0
FAIL = 0
_FAILS = []


def check(cond, msg, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [PASS] {msg} {extra}')
    else:
        FAIL += 1
        _FAILS.append(msg)
        print(f'  [FAIL] {msg} {extra}')


def fresh_panel(engine=None):
    p = MotorViewPanel()
    if engine is not None:
        p.set_engine(engine)
    # offscreen 模式下 isVisible() 恒为 False, _tick() 会直接 return,
    # 导致粒子相位/dirty 标记不被推进。单元测试需验证 _tick 内部逻辑,
    # 临时强制 isVisible 返回 True (不修改生产代码)。
    p.isVisible = lambda: True
    return p


# ============================================================
def test_params_read_from_engine():
    """归一化阈值从 engine.mp 读取, 不再硬编码。"""
    print('[TEST] 参数从 engine.mp 读取')
    e = DigitalTwinEngine()
    p = fresh_panel(e)

    pc = p._peak_current()
    ms = p._max_speed()
    check(abs(pc - e.mp.motor_base.peak_current) < 1e-9,
          'peak_current 从 engine.mp 读取',
          f'got={pc} mp={e.mp.motor_base.peak_current}')
    check(abs(ms - e.mp.motor_base.max_speed) < 1e-9,
          'max_speed 从 engine.mp 读取',
          f'got={ms} mp={e.mp.motor_base.max_speed}')

    v_under, v_over, v_rated = p._vbus_thresholds()
    check(abs(v_under - e.mp.protection_param.protect_under_voltage) < 1e-9,
          'v_under 从 protection_param 读取',
          f'got={v_under} mp={e.mp.protection_param.protect_under_voltage}')
    check(abs(v_over - e.mp.protection_param.protect_over_voltage) < 1e-9,
          'v_over 从 protection_param 读取',
          f'got={v_over} mp={e.mp.protection_param.protect_over_voltage}')
    check(abs(v_rated - e.mp.motor_base.rated_voltage) < 1e-9,
          'v_rated 从 motor_base 读取',
          f'got={v_rated} mp={e.mp.motor_base.rated_voltage}')

    ps = p._power_scale()
    expect_ps = e.mp.motor_base.rated_voltage * e.mp.motor_base.peak_current
    check(abs(ps - expect_ps) < 1e-6,
          'power_scale = rated_voltage * peak_current',
          f'got={ps} expect={expect_ps}')


def test_gear_ratio_from_engine():
    """减速比从 engine.mp.gearbox_param 读取。"""
    print('[TEST] 减速比从 gearbox_param 读取')
    e = DigitalTwinEngine()
    p = fresh_panel(e)
    gr = p._gear_ratio()
    check(abs(gr - e.mp.gearbox_param.gear_ratio) < 1e-9,
          'gear_ratio 从 gearbox_param 读取',
          f'got={gr} mp={e.mp.gearbox_param.gear_ratio}')

    # 无 engine 回退 100.0
    p2 = fresh_panel(engine=None)
    check(abs(p2._gear_ratio() - 100.0) < 1e-9,
          '无 engine 时减速比回退 100.0',
          f'got={p2._gear_ratio()}')


def test_params_follow_scenario():
    """切换场景后归一化阈值跟随变化。"""
    print('[TEST] 参数跟随场景切换')
    e = DigitalTwinEngine()
    p = fresh_panel(e)

    pc0 = p._peak_current()
    e.mp.motor_base.peak_current = 60.0
    e.mp.motor_base.max_speed = 50.0
    e.mp.protection_param.protect_over_voltage = 60.0
    e.mp.protection_param.protect_under_voltage = 20.0
    e.rebuild()
    pc1 = p._peak_current()
    ms1 = p._max_speed()
    check(abs(pc1 - 60.0) < 1e-9,
          '场景切换后 peak_current 跟随',
          f'before={pc0} after={pc1}')
    check(abs(ms1 - 50.0) < 1e-9,
          '场景切换后 max_speed 跟随',
          f'after={ms1}')

    v_under, v_over, _ = p._vbus_thresholds()
    check(abs(v_under - 20.0) < 1e-9 and abs(v_over - 60.0) < 1e-9,
          '场景切换后母线阈值跟随',
          f'under={v_under} over={v_over}')


def test_params_fallback_without_engine():
    """未注入 engine 时使用与 twin_config 默认一致的回退值。"""
    print('[TEST] 无 engine 时回退默认值')
    p = fresh_panel(engine=None)

    check(abs(p._peak_current() - _FALLBACK_PEAK_CURRENT) < 1e-9,
          '回退 peak_current 与 MotorBase 默认一致',
          f'got={p._peak_current()} expect={_FALLBACK_PEAK_CURRENT}')
    check(abs(p._max_speed() - _FALLBACK_MAX_SPEED) < 1e-9,
          '回退 max_speed 与 MotorBase 默认一致',
          f'got={p._max_speed()} expect={_FALLBACK_MAX_SPEED}')
    check(_FALLBACK_PEAK_CURRENT == 15.0,
          '回退 peak_current=15 (非旧值 30)')
    check(_FALLBACK_MAX_SPEED == 300.0,
          '回退 max_speed=300 (非旧值 1000)')


def test_power_history_buffer():
    """功率历史缓冲随 feed 增长, 上限 240。"""
    print('[TEST] 功率历史缓冲')
    e = DigitalTwinEngine()
    p = fresh_panel(e)

    check(len(p._power_hist) == 0, '初始功率历史为空')

    for i in range(10):
        p.feed({'power': float(i), 'vbus': 48.0, 'ibus': 0.1 * i,
                't_sim': i * 0.02, 'vel': 0.0})
    check(len(p._power_hist) == 10,
          'feed 10 次后历史有 10 点',
          f'len={len(p._power_hist)}')

    # 溢出测试
    for i in range(300):
        p.feed({'power': float(i), 'vbus': 48.0, 'ibus': 0.1,
                't_sim': (10 + i) * 0.02, 'vel': 0.0})
    check(len(p._power_hist) == 240,
          '功率历史上限 240 点',
          f'len={len(p._power_hist)}')


def test_alarm_rising_edge():
    """告警上升沿: 新故障位 → 新增告警条目。"""
    print('[TEST] 告警上升沿检测')
    e = DigitalTwinEngine()
    p = fresh_panel(e)

    # 初始无告警
    p.feed({'fault_flags': 0, 't_sim': 0.0, 'vel': 0.0})
    check(len(p._alarms) == 0, '初始无告警')

    # 注入过电流 (bit 0)
    p.feed({'fault_flags': int(Fault.OVER_CURRENT), 't_sim': 0.02, 'vel': 0.0})
    check(len(p._alarms) == 1,
          '过电流故障 → 1 条告警',
          f'len={len(p._alarms)}')
    check(p._alarms[-1][1] == '严重',
          '过电流告警等级=严重',
          f'level={p._alarms[-1][1]}')
    check('过电流' in p._alarms[-1][2],
          '告警描述含"过电流"',
          f'msg={p._alarms[-1][2]}')

    # 再注入过电压 (bit 1), 过电流保持
    p.feed({'fault_flags': int(Fault.OVER_CURRENT) | int(Fault.OVER_VOLTAGE),
            't_sim': 0.04, 'vel': 0.0})
    check(len(p._alarms) == 2,
          '新增过电压 → 2 条告警',
          f'len={len(p._alarms)}')

    # 清除所有故障 → "故障已清除" 提示
    p.feed({'fault_flags': 0, 't_sim': 0.06, 'vel': 0.0})
    check(len(p._alarms) == 3,
          '故障清除 → 新增"故障已清除"提示',
          f'len={len(p._alarms)}')
    check(p._alarms[-1][1] == '提示',
          '清除告警等级=提示',
          f'level={p._alarms[-1][1]}')


def test_tick_dirty_for_power_and_vel():
    """_tick 在 power!=0 或 vel!=0 时置 dirty (粒子+转子动画)。"""
    print('[TEST] _tick power/vel 置 dirty')
    e = DigitalTwinEngine()
    p = fresh_panel(e)

    # power != 0 → dirty
    p.feed({'power': 100.0, 'vbus': 48.0, 'ibus': 2.1,
            'sys_state_name': 'IDLE', 't_sim': 0.0, 'vel': 0.0})
    p._dirty = False
    p._tick()
    check(p._dirty is True,
          'power!=0 置 dirty (粒子流动)',
          f'dirty={p._dirty}')

    # vel != 0 → dirty (转子旋转)
    p._dirty = False
    p.feed({'power': 0.0, 'vbus': 48.0, 'ibus': 0.0,
            'sys_state_name': 'IDLE', 't_sim': 0.02, 'vel': 5.0})
    p._dirty = False
    p._tick()
    check(p._dirty is True,
          'vel!=0 置 dirty (转子旋转)',
          f'dirty={p._dirty}')

    # power=0 + vel=0 + IDLE → 不置 dirty
    p._dirty = False
    p.feed({'power': 0.0, 'vbus': 48.0, 'ibus': 0.0,
            'sys_state_name': 'IDLE', 't_sim': 0.04, 'vel': 0.0})
    p._dirty = False
    p._tick()
    check(p._dirty is False,
          'power=0 + vel=0 + IDLE 不置 dirty',
          f'dirty={p._dirty}')


def test_rotor_angle_advances():
    """转子角度跟随 vel 推进。"""
    print('[TEST] 转子角度推进')
    e = DigitalTwinEngine()
    p = fresh_panel(e)

    p.feed({'vel': 10.0, 't_sim': 0.0, 'power': 0.0, 'vbus': 48.0, 'ibus': 0.0})
    angle0 = p._rotor_angle
    p._tick()
    angle1 = p._rotor_angle
    check(angle1 != angle0,
          'vel!=0 时转子角度推进',
          f'before={angle0} after={angle1}')


def test_no_dead_code():
    """源码中不应残留 if False 死代码分支。"""
    print('[TEST] 源码无 if False 死代码')
    src = inspect.getsource(MotorViewPanel)
    check('if False' not in src,
          'MotorViewPanel 源码无 "if False" 死分支')


def test_no_unused_pixmaps():
    """_particle_pixmaps 与 _build_particle_pixmaps 应已移除。"""
    print('[TEST] 移除未使用的 _particle_pixmaps')
    p = fresh_panel()
    check(not hasattr(p, '_particle_pixmaps'),
          '_particle_pixmaps 字段已移除')
    check(not hasattr(MotorViewPanel, '_build_particle_pixmaps'),
          '_build_particle_pixmaps 方法已移除')
    src = inspect.getsource(MotorViewPanel)
    check('QPixmap' not in src,
          'MotorViewPanel 源码无 QPixmap 引用')


def test_layout_five_regions():
    """5 分区布局: 顶部状态条/左侧KPI/中间3D电机/右侧告警曲线/底部数据表。"""
    print('[TEST] 5 分区布局')
    p = fresh_panel()

    # 验证绘制方法存在
    for name in ('_paint_top_status', '_paint_left_kpi', '_paint_motor_3d',
                 '_paint_right_panel', '_paint_bottom_table'):
        check(hasattr(MotorViewPanel, name),
              f'绘制方法 {name} 存在')

    # 验证旧的导航标签常量已移除
    import tools.twin.motor_view_panel as mvp
    check(not hasattr(mvp, '_NAV_TABS'),
          '已移除 _NAV_TABS (无导航栏)')
    check(not hasattr(mvp, '_NAV_DEFAULT'),
          '已移除 _NAV_DEFAULT')

    # 数据表 8 行 (关节电机数据)
    check(len(_DATA_ROWS) == 8,
          '数据表 8 行',
          f'got={len(_DATA_ROWS)}')
    keys = {row[1] for row in _DATA_ROWS}
    expect_keys = {'pos', 'pos_out', 'vel', 'vel_out',
                   'torque', 'torque_out', 'iq', 'temp_motor'}
    check(keys == expect_keys,
          '数据表 key 含关节电机全部字段',
          f'got={keys}')

    # 故障位映射 10 项
    check(len(_FAULT_BITS) == 10,
          '故障位映射 10 项',
          f'got={len(_FAULT_BITS)}')


def test_paint_event_no_throw_all_states():
    """所有状态 + 多种功率下 paintEvent 不抛异常。"""
    print('[TEST] paintEvent 全状态无异常')
    e = DigitalTwinEngine()
    p = fresh_panel(e)
    p.resize(1200, 800)

    cases = [
        ('IDLE',  {'sys_state_name': 'IDLE',   'power': 0.0,    'vel': 0.0,
                   'vbus': 48.0, 'ibus': 0.0, 'ia': 0, 'ib': 0, 'ic': 0, 'id': 0,
                   'pos': 0.0, 'pos_out': 0.0, 'vel_out': 0.0,
                   'torque': 0.0, 'torque_out': 0.0, 'iq': 0.0,
                   't_sim': 0.0, 'temp_motor': 35.0}),
        ('READY', {'sys_state_name': 'READY',  'power': 0.0,    'vel': 0.0,
                   'vbus': 48.0, 'ibus': 0.0, 'ia': 0, 'ib': 0, 'ic': 0, 'id': 0,
                   'pos': 0.0, 'pos_out': 0.0, 'vel_out': 0.0,
                   'torque': 0.0, 'torque_out': 0.0, 'iq': 0.0,
                   't_sim': 0.0, 'temp_motor': 35.0}),
        ('RUN',   {'sys_state_name': 'RUN',    'power': 240.0,  'vel': 10.0,
                   'vbus': 48.0, 'ibus': 5.0, 'ia': 3, 'ib': -2, 'ic': -1, 'id': 0.5,
                   'pos': 1.5, 'pos_out': 0.015, 'vel_out': 0.1,
                   'torque': 0.5, 'torque_out': 5.0, 'iq': 2.5,
                   't_sim': 0.0, 'control_mode_name': 'VELOCITY',
                   'temp_motor': 55.0}),
        ('FAULT', {'sys_state_name': 'FAULT',  'power': 0.0,    'vel': 0.0,
                   'vbus': 48.0, 'ibus': 0.0, 'ia': 0, 'ib': 0, 'ic': 0, 'id': 0,
                   'pos': 0.0, 'pos_out': 0.0, 'vel_out': 0.0,
                   'torque': 0.0, 'torque_out': 0.0, 'iq': 0.0,
                   't_sim': 0.0, 'fault_flags': int(Fault.OVER_CURRENT),
                   'temp_motor': 45.0}),
        ('SAFETY',{'sys_state_name': 'SAFETY', 'power': -100.0, 'vel': -5.0,
                   'vbus': 48.0, 'ibus': -2.1, 'ia': -1, 'ib': 1, 'ic': 0, 'id': 0,
                   'pos': -2.0, 'pos_out': -0.02, 'vel_out': -0.05,
                   'torque': -0.3, 'torque_out': -3.0, 'iq': -1.5,
                   't_sim': 0.0, 'temp_motor': 50.0}),
        ('CALIB', {'sys_state_name': 'CALIB',  'power': 50.0,   'vel': 2.0,
                   'vbus': 48.0, 'ibus': 1.0, 'ia': 1, 'ib': 0.5, 'ic': -1.5, 'id': 0,
                   'pos': 0.5, 'pos_out': 0.005, 'vel_out': 0.02,
                   'torque': 0.1, 'torque_out': 1.0, 'iq': 0.5,
                   't_sim': 0.0, 'temp_motor': 40.0}),
    ]
    for state, t in cases:
        p.feed(t)
        p._dirty = True
        try:
            p.repaint()
            check(True, f'paintEvent ({state}) 不抛异常')
        except Exception as ex:
            check(False, f'paintEvent ({state}) 不抛异常', f'err={ex}')


def test_acc_differential_unchanged():
    """加速度差分逻辑应保持不变 (重构未触及)。"""
    print('[TEST] 加速度差分逻辑保持')
    e = DigitalTwinEngine()
    p = fresh_panel(e)
    # vel 0→10, dt=0.02 → acc=500
    p._prev_vel = 0.0
    p._prev_t = 0.0
    p._acc = 0.0
    p.feed({'vel': 0.0, 't_sim': 0.0})
    p.feed({'vel': 10.0, 't_sim': 0.02})
    check(abs(p._acc - 500.0) < 1.0,
          '加速度差分正确 (acc=500)',
          f'acc={p._acc}')
    # dt=0 不崩溃
    p._prev_t = 1.0
    p.feed({'vel': 5.0, 't_sim': 1.0})
    check(not math.isnan(p._acc),
          'dt=0 不崩溃 (acc 保持)', f'acc={p._acc}')


def test_state_style_preserved():
    """状态配色表保持 (smoke_test 依赖)。"""
    print('[TEST] 状态配色表保持')
    check(_STATE_STYLE['IDLE'][0] == '#3a4a5a',
          'IDLE 状态色 = #3a4a5a')
    check(_STATE_STYLE['RUN'][0] == '#00d4ff',
          'RUN 状态色 = #00d4ff')
    check(_STATE_STYLE['FAULT'][0] == '#ff3030',
          'FAULT 状态色 = #ff3030')
    check(_STATE_STYLE['SAFETY'][0] == '#ff6b35',
          'SAFETY 状态色 = #ff6b35')
    check(_STATE_STYLE['FAULT'][3] is True,
          'FAULT 是闪烁态')
    check(_STATE_STYLE['RUN'][2] == 6.0,
          'RUN 扫描频率=6Hz')


def main():
    tests = [
        test_params_read_from_engine,
        test_gear_ratio_from_engine,
        test_params_follow_scenario,
        test_params_fallback_without_engine,
        test_power_history_buffer,
        test_alarm_rising_edge,
        test_tick_dirty_for_power_and_vel,
        test_rotor_angle_advances,
        test_no_dead_code,
        test_no_unused_pixmaps,
        test_layout_five_regions,
        test_paint_event_no_throw_all_states,
        test_acc_differential_unchanged,
        test_state_style_preserved,
    ]
    for t in tests:
        try:
            t()
        except Exception as ex:
            import traceback
            global FAIL
            FAIL += 1
            _FAILS.append(f'{t.__name__} 抛异常: {ex}')
            print(f'  [EXCEPTION] {t.__name__}: {ex}')
            traceback.print_exc()

    print('\n' + '=' * 60)
    print(f'PASS={PASS}  FAIL={FAIL}')
    if _FAILS:
        print('失败项:')
        for f in _FAILS:
            print('  -', f)
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
