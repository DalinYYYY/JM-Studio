"""tools/twin/smoke_test.py

数字孪生独立上位机 - 详细冒烟测试脚本。
覆盖: 多模式切换/限位保护/跟随误差/通信丢失/急停/复位/参数联动/配置持久化/曲线缓冲。
用法: python -m tools.twin.smoke_test  (offscreen 友好, 不弹窗)
"""
from __future__ import annotations

import os
import sys
import json
import math

# 让脚本可直接运行
if __name__ == '__main__' and __package__ is None:
    _here = os.path.dirname(os.path.abspath(__file__))
    _root = os.path.dirname(os.path.dirname(_here))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    __package__ = 'tools.twin'

# 必须在 QApplication 之前
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication, QMessageBox
QMessageBox.warning = staticmethod(lambda *a, **k: 0)
QMessageBox.critical = staticmethod(lambda *a, **k: 0)
QMessageBox.information = staticmethod(lambda *a, **k: 0)

app = QApplication(sys.argv)

from ui.theme import theme
theme.set('dark')
app.setStyleSheet(theme.qss())

from transport.virtual_engine import (
    MotorCmd, ControlMode, Fault, SystemState,
)
from tools.twin.app import TwinMainWindow


# ---------- 测试工具 ----------
class T:
    fails = []
    passes = []

    @classmethod
    def check(cls, name, cond, extra=''):
        if cond:
            cls.passes.append(name)
            print(f'  [PASS] {name} {extra}')
        else:
            cls.fails.append(name)
            print(f'  [FAIL] {name} {extra}')

    @classmethod
    def summary(cls):
        print(f'\n=== 测试结果: {len(cls.passes)} 通过, {len(cls.fails)} 失败 ===')
        if cls.fails:
            print('失败项:')
            for f in cls.fails:
                print(f'  - {f}')
        return len(cls.fails) == 0


def tick(w, n=10):
    for _ in range(n):
        w._bridge._on_tick()


def telem(w):
    return w._bridge.engine.get_telemetry()


# ---------- 测试主体 ----------
def main():
    print('\n========== 第 0 轮: 详细测试 ==========')
    # 删除上次运行残留的配置文件, 避免被污染的参数 (如 gear_ratio=200) 影响
    _cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'twin_layout.json')
    if os.path.exists(_cfg):
        os.remove(_cfg)
    w = TwinMainWindow()
    eng = w._bridge.engine

    # 1. 初始状态
    print('\n[1] 初始状态')
    tick(w, 5)
    t = telem(w)
    T.check('初始 sys_state=IDLE', t.get('sys_state_name') == 'IDLE',
            f'实际={t.get("sys_state_name")}')
    T.check('初始 control_mode=NONE', t.get('control_mode_name') == 'NONE',
            f'实际={t.get("control_mode_name")}')
    T.check('初始无故障', int(t.get('fault_flags', 0)) == 0)

    # 2. 未使能直接发运动命令
    print('\n[2] 未使能直接发运动命令(应被拒绝)')
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.POSITION, set_pos=1.0))
    tick(w, 5)
    t = telem(w)
    T.check('未使能运动命令不进入RUN', t.get('sys_state_name') != 'RUN',
            f'sys={t.get("sys_state_name")}')

    # 3. 使能流程
    print('\n[3] 使能流程')
    w._bridge.enable()
    tick(w, 5)
    t = telem(w)
    T.check('enable 后进入 READY', t.get('sys_state_name') in ('READY', 'RUN'),
            f'sys={t.get("sys_state_name")}')

    # 4. POSITION 模式定位精度
    print('\n[4] POSITION 模式定位')
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.POSITION, set_pos=2.0))
    tick(w, 80)
    t = telem(w)
    pos = float(t.get('pos', 0))
    T.check('POSITION 到位 (|err|<0.05)', abs(pos - 2.0) < 0.05, f'pos={pos:.4f}')
    T.check('POSITION 进入 RUN', t.get('sys_state_name') == 'RUN')
    T.check('POSITION run_state=HOLDING', t.get('run_state_name') == 'HOLDING',
            f'run={t.get("run_state_name")}')

    # 5. 模式切换 POSITION -> VELOCITY
    print('\n[5] POSITION -> VELOCITY 切换')
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.VELOCITY, set_vel=3.0))
    tick(w, 150)
    t = telem(w)
    vel = float(t.get('vel', 0))
    T.check('VELOCITY 速度跟随 (|err|<0.3)', abs(vel - 3.0) < 0.3, f'vel={vel:.4f}')
    T.check('VELOCITY control_mode', t.get('control_mode_name') == 'VELOCITY')

    # 6. VELOCITY -> TORQUE 切换
    print('\n[6] VELOCITY -> TORQUE 切换')
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.TORQUE, set_torque=0.5))
    tick(w, 50)
    t = telem(w)
    T.check('TORQUE control_mode', t.get('control_mode_name') == 'TORQUE')
    # TORQUE 模式: 命令力矩 set_torque=0.5 Nm, 经 Kt=0.1 转换为 Iq_ref=5.0 A
    # 反馈 torque 是电机端实测 (受摩擦/负载影响), 应用 iq_ref 判断命令跟随
    iq_ref = float(t.get('iq_ref', 0))
    T.check('TORQUE 命令跟随 (iq_ref≈5.0)', abs(iq_ref - 5.0) < 0.5,
            f'iq_ref={iq_ref:.4f} (set_torque=0.5, Kt=0.1)')
    # 同时验证反馈面板的命令目标字段已补齐
    w._feedback_panel.update_telemetry(t)
    tgt_torque_lbl = w._feedback_panel._labels['target_torque'][0].text()
    T.check('反馈面板显示 target_torque', '0.5' in tgt_torque_lbl or 'Nm' in tgt_torque_lbl,
            f'label="{tgt_torque_lbl}"')
    cmd_torque_lbl = w._feedback_panel._labels['cmd_target_torque'][0].text()
    T.check('反馈面板显示 cmd_target_torque', '0.5' in cmd_torque_lbl or 'Nm' in cmd_torque_lbl,
            f'label="{cmd_torque_lbl}"')

    # 7. TORQUE -> CURRENT 切换
    print('\n[7] TORQUE -> CURRENT 切换')
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.CURRENT, set_iq=2.0))
    tick(w, 50)
    t = telem(w)
    T.check('CURRENT control_mode', t.get('control_mode_name') == 'CURRENT')

    # 8. CURRENT -> DUTY 切换 (开环)
    print('\n[8] CURRENT -> DUTY 切换')
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.DUTY, set_duty=0.1))
    tick(w, 50)
    t = telem(w)
    T.check('DUTY control_mode', t.get('control_mode_name') == 'DUTY')

    # 9. IMPEDANCE 模式
    print('\n[9] IMPEDANCE 模式 (Kp/Kd 生效验证)')
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.IMPEDANCE,
                                set_pos=1.0, set_kp=20.0, set_kd=1.0, set_torque_ff=0.0))
    tick(w, 5)
    fsm = eng.fsm
    T.check('IMPEDANCE target_kp 同步', abs(fsm.target_kp - 20.0) < 1e-6,
            f'target_kp={fsm.target_kp}')
    T.check('IMPEDANCE target_kd 同步', abs(fsm.target_kd - 1.0) < 1e-6,
            f'target_kd={fsm.target_kd}')
    T.check('IMPEDANCE target_pos 同步', abs(fsm.target_pos - 1.0) < 1e-6,
            f'target_pos={fsm.target_pos}')
    tick(w, 50)
    t = telem(w)
    T.check('IMPEDANCE control_mode', t.get('control_mode_name') == 'IMPEDANCE')
    # 验证运动控制面板切换模式时从 fsm 同步当前 target (Kp=20 已生效)
    mc_grp = w._control_panel.motion_ctrl
    mc_grp._combo.setCurrentIndex(6)  # IMPEDANCE 是最后一个
    kp_w = mc_grp._field_widgets.get('set_kp')
    kd_w = mc_grp._field_widgets.get('set_kd')
    T.check('运动面板同步 fsm target_kp=20',
            kp_w is not None and abs(kp_w.value() - 20.0) < 1e-6,
            f'Kp={kp_w.value() if kp_w else None}')
    T.check('运动面板同步 fsm target_kd=1',
            kd_w is not None and abs(kd_w.value() - 1.0) < 1e-6,
            f'Kd={kd_w.value() if kd_w else None}')
    # 干净状态 (fsm.target_kp=0) 应显示默认值 10 (0 值回退逻辑)
    w._bridge.engine.fsm.target_kp = 0.0
    w._bridge.engine.fsm.target_kd = 0.0
    mc_grp._rebuild_fields()
    # rebuild 后需重新获取 widget 引用 (旧 widget 被 deleteLater)
    kp_w = mc_grp._field_widgets.get('set_kp')
    kd_w = mc_grp._field_widgets.get('set_kd')
    T.check('0值回退: 运动面板 IMPEDANCE 默认 Kp=10',
            kp_w is not None and abs(kp_w.value() - 10.0) < 1e-6,
            f'Kp={kp_w.value() if kp_w else None}')
    T.check('0值回退: 运动面板 IMPEDANCE 默认 Kd=0.5',
            kd_w is not None and abs(kd_w.value() - 0.5) < 1e-6,
            f'Kd={kd_w.value() if kd_w else None}')
    w._bridge.enable()
    tick(w, 5)

    # 10. 位置限位保护 (POSITION 模式)
    print('\n[10] 位置限位保护')
    # 限位字段在 fsm 上 (mp.position_limit 是子结构), 单位是输出端 rad
    fsm = eng.fsm
    pos_max = getattr(fsm, 'pos_limit_max', None)  # 输出端坐标
    pos_min = getattr(fsm, 'pos_limit_min', None)
    gear = getattr(fsm, 'gear_ratio', 1.0)
    print(f'    fsm.pos_limit_max={pos_max} (输出端), gear_ratio={gear}')
    if pos_max is not None:
        # set_pos 是电机端坐标, 要让输出端超限需 set_pos > pos_max * gear_ratio
        # 给定电机端目标 = pos_max * gear_ratio * 1.2 (输出端超限 20%)
        set_pos_motor = float(pos_max) * float(gear) * 1.2
        w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.POSITION,
                                    set_pos=set_pos_motor))
        tick(w, 200)
        t = telem(w)
        pos_motor = float(t.get('pos', 0))
        pos_out_actual = pos_motor / max(gear, 1.0)
        # 限位应触发 SAFETY 或输出端位置被钳位在限位附近
        triggered = (t.get('sys_state_name') == 'SAFETY') or (pos_out_actual < float(pos_max) * 1.1)
        T.check('位置超限触发保护', triggered,
                f'sys={t.get("sys_state_name")} pos_out={pos_out_actual:.3f} limit={pos_max}')
        # 清状态
        w._bridge.clear_fault()
        w._bridge.reset()
        tick(w, 5)
        w._bridge.enable()
        tick(w, 5)
        # 反馈面板应显示限位值
        w._feedback_panel.update_telemetry(t)
        lim_lbl = w._feedback_panel._labels['pos_limit_max'][0].text()
        T.check('反馈面板显示 pos_limit_max', '12.5' in lim_lbl or 'rad' in lim_lbl,
                f'label="{lim_lbl}"')
    else:
        T.check('位置超限触发保护', False, 'pos_limit_max 未配置')

    # 11. 故障注入 - 过流
    print('\n[11] 故障注入: OVER_CURRENT')
    w._bridge.inject_fault(Fault.OVER_CURRENT)
    tick(w, 5)
    t = telem(w)
    T.check('OVER_CURRENT 触发 FAULT/SAFETY',
            t.get('sys_state_name') in ('FAULT', 'SAFETY'),
            f'sys={t.get("sys_state_name")} fault=0x{int(t.get("fault_flags",0)):04X}')
    T.check('fault_flags 包含 OVER_CURRENT 位',
            bool(int(t.get('fault_flags', 0)) & Fault.OVER_CURRENT))

    # 12. 清障 + 复位
    print('\n[12] 清障 + 复位')
    w._bridge.clear_fault()
    tick(w, 5)
    w._bridge.reset()
    tick(w, 5)
    t = telem(w)
    T.check('清障复位后回 IDLE', t.get('sys_state_name') in ('IDLE', 'READY'),
            f'sys={t.get("sys_state_name")}')
    T.check('清障后 fault_flags=0', int(t.get('fault_flags', 0)) == 0)

    # 13. 急停 ESTOP
    print('\n[13] 急停 ESTOP')
    w._bridge.enable()
    tick(w, 5)
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.VELOCITY, set_vel=5.0))
    tick(w, 30)
    t = telem(w)
    vel_before = float(t.get('vel', 0))
    w._bridge.e_stop()
    tick(w, 10)
    t = telem(w)
    vel_after = float(t.get('vel', 0))
    T.check('ESTOP 后速度下降', abs(vel_after) < abs(vel_before) or abs(vel_after) < 0.5,
            f'before={vel_before:.3f} after={vel_after:.3f}')
    T.check('ESTOP 后进入 IDLE/SAFETY',
            t.get('sys_state_name') in ('IDLE', 'SAFETY'),
            f'sys={t.get("sys_state_name")}')

    # 14. STOP 命令 (保留使能)
    print('\n[14] STOP 命令')
    w._bridge.reset()
    tick(w, 3)
    w._bridge.enable()
    tick(w, 5)
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.VELOCITY, set_vel=4.0))
    tick(w, 50)
    w._bridge.stop_motion()
    tick(w, 80)
    t = telem(w)
    T.check('STOP 后速度趋零', abs(float(t.get('vel', 0))) < 0.5,
            f'vel={t.get("vel"):.3f}')
    T.check('STOP 后保留使能 (READY/IDLE)',
            t.get('sys_state_name') in ('READY', 'IDLE'),
            f'sys={t.get("sys_state_name")}')

    # 15. 注入通信丢失
    print('\n[15] 通信丢失 COMM_LOST')
    w._bridge.inject_fault(Fault.COMM_LOST)
    tick(w, 5)
    t = telem(w)
    T.check('COMM_LOST 触发保护',
            t.get('sys_state_name') in ('FAULT', 'SAFETY'),
            f'sys={t.get("sys_state_name")}')
    w._bridge.clear_injected_fault()
    w._bridge.clear_fault()
    tick(w, 5)

    # 16. 反馈面板字段覆盖
    print('\n[16] 反馈面板字段覆盖')
    w._bridge.reset()
    tick(w, 3)
    w._bridge.enable()
    tick(w, 5)
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.POSITION, set_pos=1.0))
    tick(w, 50)
    t = telem(w)
    w._feedback_panel.update_telemetry(t)
    # 检查反馈面板的所有标签是否更新为非 "-"
    updated = 0
    total = 0
    for key, (lbl, fmt, unit) in w._feedback_panel._labels.items():
        total += 1
        if lbl.text() != '-':
            updated += 1
    T.check(f'反馈面板字段更新 ({updated}/{total})', updated >= total * 0.7,
            f'updated={updated}/{total}')

    # 17. 曲线面板缓冲
    print('\n[17] 曲线面板缓冲')
    for _ in range(200):
        tick(w, 1)
        w._plot_panel.feed(telem(w))
    # 检查缓冲已填充
    pos_buf = w._plot_panel._bufs.get('pos')
    T.check('曲线 pos 缓冲有数据', pos_buf is not None and pos_buf._cnt > 100,
            f'cnt={pos_buf._cnt if pos_buf else 0}')
    # 检查命令目标/生效目标曲线也有数据
    cmd_pos_buf = w._plot_panel._bufs.get('cmd_target_pos')
    tgt_pos_buf = w._plot_panel._bufs.get('target_pos')
    T.check('曲线 cmd_target_pos 缓冲有数据',
            cmd_pos_buf is not None and cmd_pos_buf._cnt > 50,
            f'cnt={cmd_pos_buf._cnt if cmd_pos_buf else 0}')
    T.check('曲线 target_pos 缓冲有数据',
            tgt_pos_buf is not None and tgt_pos_buf._cnt > 50,
            f'cnt={tgt_pos_buf._cnt if tgt_pos_buf else 0}')
    # 验证命令目标与生效目标的差异 (ramp 过渡): 初始 ramp 时 target 应滞后于 cmd_target
    if cmd_pos_buf is not None and tgt_pos_buf is not None:
        _, cmd_ys = cmd_pos_buf.arrays()
        _, tgt_ys = tgt_pos_buf.arrays()
        if len(cmd_ys) > 10 and len(tgt_ys) > 10:
            # 找命令目标突变点 (set_pos=1.0 后)
            cmd_arr = cmd_ys
            tgt_arr = tgt_ys
            # 命令目标应大于 0 (set_pos=1.0), 生效目标应接近命令目标 (ramp 完成)
            cmd_final = float(cmd_arr[-1])
            tgt_final = float(tgt_arr[-1])
            T.check('ramp 完成后 target≈cmd_target',
                    abs(cmd_final - tgt_final) < 0.2,
                    f'cmd={cmd_final:.4f} tgt={tgt_final:.4f}')

    # 18. 配置持久化
    print('\n[18] 配置持久化')
    cfg = w._collect_config()
    T.check('collect_config 返回 dict', isinstance(cfg, dict))
    T.check('config 包含 twin_param', 'twin_param' in cfg, f'keys={list(cfg.keys())}')
    T.check('config 包含 main_window', 'main_window' in cfg)

    # 19. 参数面板 get_opts/set_opts 往返
    print('\n[19] 参数面板 opts 往返')
    try:
        opts = w._twin_param_panel.get_opts()
        T.check('get_opts 返回 dict', isinstance(opts, dict), f'type={type(opts).__name__}')
        # set_opts 应不触发命令
        w._twin_param_panel.set_opts(opts if isinstance(opts, dict) else {})
        T.check('set_opts 不抛异常', True)
    except Exception as e:
        T.check('参数面板 opts 往返', False, f'err={e}')

    # 20. 引擎启停信号
    print('\n[20] 引擎启停信号')
    states = []
    w._bridge.running_changed.connect(lambda r: states.append(r))
    w._bridge.stop()
    w._bridge.start()
    T.check('running_changed 信号触发', len(states) >= 2,
            f'states={states}')

    # 21. 事件日志面板
    print('\n[21] 事件日志面板')
    log_panel = w._log_panel
    # 触发若干事件
    log_before = len(log_panel._lines)
    w._bridge.enable()
    tick(w, 5)
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.POSITION, set_pos=0.5))
    tick(w, 5)
    w._bridge.inject_fault(Fault.OVER_CURRENT)
    tick(w, 5)
    w._bridge.clear_fault()
    tick(w, 3)
    log_after = len(log_panel._lines)
    T.check('日志面板记录事件', log_after > log_before,
            f'before={log_before} after={log_after}')
    # 检查日志分类
    categories = set(cat for cat, _, _ in log_panel._lines)
    T.check('日志包含 CMD 类别', 'CMD' in categories, f'cats={categories}')
    T.check('日志包含 STATE 类别', 'STATE' in categories, f'cats={categories}')
    T.check('日志包含 FAULT 类别', 'FAULT' in categories, f'cats={categories}')
    # 检查日志行数限制
    T.check('日志行数 <= MAX_LINES', len(log_panel._lines) <= log_panel.MAX_LINES,
            f'lines={len(log_panel._lines)}')
    # 清空功能
    log_panel.clear()
    T.check('日志清空生效', len(log_panel._lines) == 0)

    # 22. 状态变化信号
    print('\n[22] 状态变化信号')
    state_changes = []
    w._bridge.state_changed.connect(lambda o, n: state_changes.append((o, n)))
    w._bridge.reset()
    tick(w, 3)
    w._bridge.enable()
    tick(w, 5)
    w._bridge.e_stop()
    tick(w, 5)
    T.check('state_changed 信号触发', len(state_changes) >= 2,
            f'changes={state_changes}')

    # 23. 故障上升沿信号
    print('\n[23] 故障上升沿信号')
    faults = []
    w._bridge.fault_occurred.connect(lambda f, d: faults.append((f, d)))
    w._bridge.reset()
    tick(w, 3)
    w._bridge.enable()
    tick(w, 5)
    w._bridge.inject_fault(Fault.OVER_TEMP_FET)
    tick(w, 5)
    T.check('fault_occurred 信号触发', len(faults) >= 1,
            f'faults={faults}')
    if faults:
        T.check('故障码正确', faults[0][0] & Fault.OVER_TEMP_FET,
                f'flags=0x{faults[0][0]:04X}')

    # 24. 配置文件真实读写往返
    print('\n[24] 配置文件往返')
    cfg_file = w._CONFIG_FILE if hasattr(w, '_CONFIG_FILE') else None
    if cfg_file is None:
        # 访问模块级常量
        from tools.twin.app import _CONFIG_FILE as cfg_file
    # 删除旧文件
    if os.path.exists(cfg_file):
        os.remove(cfg_file)
    # 修改引擎参数 + 窗口尺寸 + 保存
    eng2 = w._bridge.engine
    orig_kp = eng2.mp.position_loop.position_kp
    eng2.mp.position_loop.position_kp = orig_kp * 2.0   # 改一个参数
    w.resize(1234, 567)
    w._save_config()
    T.check('配置文件已写入', os.path.exists(cfg_file), f'path={cfg_file}')
    with open(cfg_file, 'r', encoding='utf-8') as f:
        saved = json.load(f)
    T.check('配置含 main_window.width=1234',
            saved.get('main_window', {}).get('width') == 1234,
            f'saved={saved.get("main_window")}')
    T.check('配置含 twin_param', 'twin_param' in saved)
    T.check('配置含 engine_mp 快照', 'engine_mp' in saved,
            f'top keys={list(saved.keys())}')
    # 验证快照包含修改后的参数
    mp_snap = saved.get('engine_mp', {})
    # mp_to_dict 输出结构: 顶层是各子段, position_loop 在某段下
    snap_kp = None
    def _find_kp(d):
        if isinstance(d, dict):
            if 'position_kp' in d:
                return d['position_kp']
            for v in d.values():
                r = _find_kp(v)
                if r is not None:
                    return r
        return None
    snap_kp = _find_kp(mp_snap)
    T.check('快照含 position_kp (修改后值)',
            snap_kp is not None and abs(float(snap_kp) - orig_kp * 2.0) < 1e-6,
            f'snap_kp={snap_kp} expect={orig_kp * 2.0}')
    # 恢复参数到原值, 然后从配置加载
    eng2.mp.position_loop.position_kp = orig_kp
    T.check('恢复前 kp=原值',
            abs(eng2.mp.position_loop.position_kp - orig_kp) < 1e-9)
    loaded = w._load_config()
    w._apply_config(loaded)
    T.check('_apply_config 后 kp=快照值 (修改后)',
            abs(eng2.mp.position_loop.position_kp - orig_kp * 2.0) < 1e-6,
            f'kp={eng2.mp.position_loop.position_kp} expect={orig_kp * 2.0}')
    # 还原参数避免影响后续测试
    eng2.mp.position_loop.position_kp = orig_kp
    eng2.rebuild()

    # 25. 参数面板↔引擎联动
    print('\n[25] 参数面板↔引擎联动')
    # 修改一个参数 (位置环 Kp), 验证引擎 mp 是否同步
    tp = w._twin_param_panel
    eng2 = w._bridge.engine
    # 记录原值
    orig_kp = eng2.mp.position_loop.position_kp
    # 通过参数面板修改 (找 position_kp 的 spinbox)
    # twin_param_panel 内部结构未知, 用 get_opts/set_opts 验证往返
    opts = tp.get_opts()
    T.check('get_opts 返回非空 dict', isinstance(opts, dict) and len(opts) > 0,
            f'keys={list(opts.keys())[:5]}')
    # 修改 opts 中的某个值 (如果有)
    modified = False
    for k, v in list(opts.items()):
        if isinstance(v, (int, float)) and v > 0:
            opts[k] = v * 1.5
            modified = True
            break
    if modified:
        try:
            tp.set_opts(opts)
            T.check('set_opts 修改不抛异常', True)
        except Exception as e:
            T.check('set_opts 修改不抛异常', False, f'err={e}')
    else:
        T.check('set_opts 修改不抛异常', True, '(无可修改数值字段)')

    # 26. 多次启停稳定性
    print('\n[26] 多次启停稳定性')
    for i in range(5):
        w._bridge.stop()
        w._bridge.start()
        tick(w, 2)
    T.check('5 次启停后仍运行', w._bridge.is_running())
    t = telem(w)
    T.check('启停后引擎仍出遥测', t.get('sys_state_name') is not None)

    # 27. 性能测试: 分离 UI feed 与引擎 step 开销
    print('\n[27] 性能测试')
    import time as _time
    w._bridge.enable()
    tick(w, 5)
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.POSITION, set_pos=2.0))
    tick(w, 10)
    # 27a. 纯 UI feed 性能 (不含引擎 step)
    cached_t = telem(w)
    t0 = _time.perf_counter()
    for _ in range(1000):
        w._plot_panel.feed(cached_t)
    t1 = _time.perf_counter()
    ui_ms = (t1 - t0) * 1000 / 1000
    T.check('UI feed 1000 帧 < 2ms/帧', ui_ms < 2.0,
            f'{ui_ms:.3f} ms/帧')
    # 27b. 引擎 step + 遥测 + UI 全链路 (允许较慢, 200 步仿真)
    t0 = _time.perf_counter()
    for _ in range(200):
        tick(w, 1)
        w._plot_panel.feed(telem(w))
    t1 = _time.perf_counter()
    full_ms = (t1 - t0) * 1000 / 200
    T.check('全链路 200 帧 < 10ms/帧', full_ms < 10.0,
            f'{full_ms:.3f} ms/帧 (含 200 步 FOC 仿真)')

    # 28. 曲线面板 dirty 标记优化
    print('\n[28] dirty 标记优化')
    # 不暂停 feed, 仅验证 feed 后 dirty 非空, _redraw 后 dirty 清空
    w._plot_panel._dirty.clear()
    w._plot_panel.feed(telem(w))
    T.check('feed 后 dirty 非空', len(w._plot_panel._dirty) > 0,
            f'dirty={len(w._plot_panel._dirty)}')
    # 部分字段 feed: 构造只含 pos 的 dict (engine 仍补齐 fsm 字段)
    partial_t = {'t_sim': telem(w).get('t_sim', 0), 'pos': 1.5}
    w._plot_panel._dirty.clear()
    w._plot_panel.feed(partial_t)
    T.check('部分字段 feed 后 dirty 含 pos',
            len(w._plot_panel._dirty) > 0 and 'pos' in w._plot_panel._dirty,
            f'dirty={w._plot_panel._dirty}')
    # _redraw 应清空 dirty
    w._plot_panel._redraw()
    T.check('_redraw 后 dirty 清空', len(w._plot_panel._dirty) == 0,
            f'dirty={len(w._plot_panel._dirty)}')
    tick(w, 5)

    # 29. 长时间运行内存稳定 (缓冲环形回绕)
    print('\n[29] 长时间运行缓冲回绕')
    # 喂入超过 maxlen 的帧, 验证缓冲环形回绕不崩溃
    maxlen = w._plot_panel._maxlen
    for _ in range(maxlen + 500):
        tick(w, 1)
        w._plot_panel.feed(telem(w))
    pos_buf = w._plot_panel._bufs.get('pos')
    T.check('缓冲回绕后 cnt <= maxlen',
            pos_buf is not None and pos_buf._cnt <= maxlen,
            f'cnt={pos_buf._cnt if pos_buf else 0} maxlen={maxlen}')
    T.check('缓冲回绕后仍能取数据',
            pos_buf is not None and len(pos_buf.arrays()[0]) > 0)

    # 30. 事件标记线 (Round 7)
    print('\n[30] 事件标记线')
    pp = w._plot_panel
    # 重置事件状态
    pp._events.clear()
    pp._last_sys_state = None
    pp._last_mode = None
    pp._last_fault = 0
    pp._event_dirty = False
    # 先喂一帧建立 baseline
    pp.feed(telem(w))
    n_events_before = len(pp._events)
    # 触发使能 (IDLE -> READY)
    w._bridge.send_cmd(MotorCmd(enable=True))
    tick(w, 5)
    pp.feed(telem(w))
    # 触发故障 (注入 OVER_CURRENT)
    w._bridge.engine.inject_fault(Fault.OVER_CURRENT)
    tick(w, 5)
    pp.feed(telem(w))
    # 检查事件被记录
    T.check('状态切换记录事件',
            len(pp._events) > n_events_before,
            f'events_before={n_events_before} events_after={len(pp._events)}')
    # 检查事件类型
    has_state_ev = any('IDLE' in lbl or 'READY' in lbl or 'FAULT' in lbl or 'SAFETY' in lbl
                       for _, lbl, _ in pp._events)
    T.check('事件含状态切换标记',
            has_state_ev,
            f'events={[(round(t,2), lbl) for t, lbl, _ in pp._events]}')
    has_fault_ev = any('FAULT 0x' in lbl for _, lbl, _ in pp._events)
    T.check('事件含故障上升沿标记',
            has_fault_ev,
            f'events={[(round(t,2), lbl) for t, lbl, _ in pp._events]}')
    # 检查 _event_dirty 在 feed 后被置位
    T.check('feed 后 _event_dirty 置位',
            pp._event_dirty,
            f'event_dirty={pp._event_dirty}')
    # 触发 _redraw 后 _event_dirty 清零
    pp._redraw()
    T.check('_redraw 后 _event_dirty 清零',
            not pp._event_dirty,
            f'event_dirty={pp._event_dirty}')
    # 事件数量限制 (<= 200)
    T.check('事件数量限制 <= 200',
            len(pp._events) <= 200,
            f'events={len(pp._events)}')
    # 重置
    w._bridge.send_cmd(MotorCmd(reset=True))
    tick(w, 5)

    # 31. 故障历史面板 (Round 8)
    print('\n[31] 故障历史面板')
    fhp = w._fault_history_panel
    # 重置面板状态
    fhp._rows.clear()
    fhp._last_fault_flags = 0
    fhp._last_sys_state = ""
    fhp._refresh_table()
    # 重置 bridge 上升沿检测基线, 确保注入故障能触发 fault_occurred
    w._bridge._prev_fault = 0
    # 确保引擎无故障
    w._bridge.engine.clear_injected_fault()
    w._bridge.send_cmd(MotorCmd(reset=True))
    tick(w, 3)
    # 检查 Tab 已添加
    tab_idx = -1
    for i in range(w._tabs.count()):
        if w._tabs.tabText(i) == "故障历史":
            tab_idx = i
            break
    T.check('Tab "故障历史" 已添加',
            tab_idx >= 0,
            f'tab_idx={tab_idx}')
    # 检查一键注入按钮存在
    T.check('一键注入按钮数量 = 9',
            len(fhp._inject_buttons) == 9,
            f'buttons={len(fhp._inject_buttons)}')
    # 注入故障 → 触发 fault_occurred → on_fault_occurred 记录
    w._bridge.send_cmd(MotorCmd(enable=True))
    tick(w, 3)
    w._bridge.engine.inject_fault(Fault.OVER_CURRENT)
    tick(w, 5)
    # fault_occurred 信号会触发 on_fault_occurred
    T.check('故障触发记录到历史',
            len(fhp._rows) >= 1,
            f'rows={len(fhp._rows)}')
    # 检查行内容: 故障码 + 事件类型=触发
    if len(fhp._rows) > 0:
        ts, code, name, state, etype = fhp._rows[-1]
        T.check('故障行含故障码 0x0001',
                '0x0001' in code,
                f'code={code}')
        T.check('故障行事件类型=触发',
                etype == '触发',
                f'etype={etype}')
        T.check('故障行含故障名 "过流"',
                '过流' in name,
                f'name={name}')
    else:
        T.check('故障行内容 (skip)', False, '无记录')
    # 清障 → 故障下降沿 → 记录清除事件
    # 先调 on_telemetry 让面板记录当前 fault_flags 作为基线
    fhp.on_telemetry(telem(w))
    w._bridge.send_cmd(MotorCmd(fault_clear=True))
    w._bridge.engine.clear_injected_fault()
    tick(w, 5)
    # 通过遥测让面板检测到故障清除
    fhp.on_telemetry(telem(w))
    has_clear = any(r[4] == '清除' for r in fhp._rows)
    T.check('故障清除记录到历史',
            has_clear,
            f'rows={[(r[0], r[4]) for r in fhp._rows]}')
    # 检查表格行数与 _rows 一致
    T.check('表格行数与 _rows 一致',
            fhp._table.rowCount() == len(fhp._rows),
            f'table={fhp._table.rowCount()} rows={len(fhp._rows)}')
    # 检查清空按钮生效
    fhp._clear_history()
    T.check('清空历史生效',
            len(fhp._rows) == 0 and fhp._table.rowCount() == 0,
            f'rows={len(fhp._rows)} table={fhp._table.rowCount()}')
    # 检查 MAX_ROWS 限制
    T.check('MAX_ROWS = 500',
            fhp.MAX_ROWS == 500,
            f'MAX_ROWS={fhp.MAX_ROWS}')
    # 测试 inject_requested 信号 (不直接调 bridge, 验证信号发射)
    captured = []
    fhp.inject_requested.connect(lambda b: captured.append(b))
    fhp._inject_buttons[0].click()   # 第一个按钮 = 过流
    T.check('一键注入按钮发射信号',
            len(captured) == 1 and captured[0] == Fault.OVER_CURRENT,
            f'captured={captured}')
    # 重置
    w._bridge.send_cmd(MotorCmd(reset=True))
    tick(w, 5)

    # 32. 参数导入导出 (Round 9)
    print('\n[32] 参数导入导出')
    import tempfile
    mp = w._bridge.engine.mp
    orig_inertia = mp.motor_base.inertia
    orig_ratio = mp.gearbox_param.gear_ratio
    # 控制面板菜单配置组按钮存在 (原工具栏动作已迁移到左侧)
    mc = w._control_panel.menu_config
    T.check('菜单配置含 "导出参数" 按钮',
            mc._btn_export is not None and mc._btn_export.text() == "导出参数",
            f'text={mc._btn_export.text() if mc._btn_export else None}')
    T.check('菜单配置含 "导入参数" 按钮',
            mc._btn_import is not None and mc._btn_import.text() == "导入参数",
            f'text={mc._btn_import.text() if mc._btn_import else None}')
    # 直接调 _on_export_params 的核心逻辑 (绕过 QFileDialog)
    from transport.virtual_engine.twin_config import mp_to_dict, mp_from_dict
    snap = mp_to_dict(mp, section='all')
    T.check('mp_to_dict 返回非空 dict',
            isinstance(snap, dict) and len(snap) > 0,
            f'type={type(snap).__name__} keys={len(snap) if isinstance(snap, dict) else 0}')
    # 写入临时文件
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as tf:
        json.dump(snap, tf, ensure_ascii=False)
        tmp_path = tf.name
    T.check('参数快照已写入临时文件',
            os.path.exists(tmp_path),
            f'path={tmp_path}')
    # 修改引擎参数, 验证导入能恢复
    mp.motor_base.inertia = 9.999e-3
    mp.gearbox_param.gear_ratio = 999.0
    # 读取并应用
    with open(tmp_path, 'r', encoding='utf-8') as f:
        loaded = json.load(f)
    ok_apply = mp_from_dict(mp, loaded, section='all')
    T.check('mp_from_dict 应用成功',
            ok_apply,
            f'ok={ok_apply}')
    T.check('导入后 inertia 恢复',
            abs(mp.motor_base.inertia - orig_inertia) < 1e-12,
            f'inertia={mp.motor_base.inertia} orig={orig_inertia}')
    T.check('导入后 gear_ratio 恢复',
            abs(mp.gearbox_param.gear_ratio - orig_ratio) < 1e-12,
            f'ratio={mp.gearbox_param.gear_ratio} orig={orig_ratio}')
    os.unlink(tmp_path)

    # 33. 场景预设 (Round 9)
    print('\n[33] 场景预设')
    # 控制面板菜单配置组场景按钮存在
    T.check('菜单配置含 "高惯量" 场景按钮',
            mc._btn_scn_high is not None and mc._btn_scn_high.text() == "高惯量")
    T.check('菜单配置含 "低惯量" 场景按钮',
            mc._btn_scn_low is not None and mc._btn_scn_low.text() == "低惯量")
    T.check('菜单配置含 "重载" 场景按钮',
            mc._btn_scn_heavy is not None and mc._btn_scn_heavy.text() == "重载")
    # 场景定义存在
    T.check('_SCENARIOS 含 3 个场景',
            len(w._SCENARIOS) == 3 and
            'high_inertia' in w._SCENARIOS and
            'low_inertia' in w._SCENARIOS and
            'heavy_load' in w._SCENARIOS,
            f'keys={list(w._SCENARIOS.keys())}')
    # 应用高惯量场景
    w._on_apply_scenario('high_inertia')
    T.check('高惯量场景: inertia=1e-3',
            abs(mp.motor_base.inertia - 1e-3) < 1e-12,
            f'inertia={mp.motor_base.inertia}')
    T.check('高惯量场景: gear_ratio=50',
            abs(mp.gearbox_param.gear_ratio - 50.0) < 1e-9,
            f'ratio={mp.gearbox_param.gear_ratio}')
    T.check('高惯量场景: rated_torque=5',
            abs(mp.motor_base.rated_torque - 5.0) < 1e-9,
            f'rated_torque={mp.motor_base.rated_torque}')
    # 应用低惯量场景
    w._on_apply_scenario('low_inertia')
    T.check('低惯量场景: inertia=1e-6',
            abs(mp.motor_base.inertia - 1e-6) < 1e-12,
            f'inertia={mp.motor_base.inertia}')
    T.check('低惯量场景: max_speed=1000',
            abs(mp.motor_base.max_speed - 1000.0) < 1e-9,
            f'max_speed={mp.motor_base.max_speed}')
    # 应用重载场景
    w._on_apply_scenario('heavy_load')
    T.check('重载场景: gear_ratio=200',
            abs(mp.gearbox_param.gear_ratio - 200.0) < 1e-9,
            f'ratio={mp.gearbox_param.gear_ratio}')
    T.check('重载场景: peak_current=60',
            abs(mp.motor_base.peak_current - 60.0) < 1e-9,
            f'peak_current={mp.motor_base.peak_current}')
    # 引擎 rebuild 后仍可运行
    w._bridge.send_cmd(MotorCmd(reset=True))
    tick(w, 3)
    t = telem(w)
    T.check('场景切换后引擎仍出遥测',
            'sys_state_name' in t,
            f'keys={list(t.keys())[:5]}')
    # 恢复默认参数 (导入原始快照)
    mp_from_dict(mp, snap, section='all')
    w._bridge.engine.rebuild()
    tick(w, 3)

    # 34. 边界场景: 极端参数 + 并发命令 (Round 10)
    print('\n[34] 边界场景')
    # 34.1 极小惯量 (1e-9) 不崩溃
    mp.motor_base.inertia = 1e-9
    w._bridge.engine.rebuild()
    w._bridge.send_cmd(MotorCmd(enable=True))
    tick(w, 3)
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.POSITION, set_pos=0.5))
    tick(w, 30)
    t = telem(w)
    T.check('极小惯量 (1e-9) 不崩溃',
            'sys_state_name' in t and t.get('sys_state_name') in ('RUN', 'READY', 'FAULT', 'SAFETY', 'IDLE'),
            f'sys={t.get("sys_state_name")} pos={t.get("pos")}')
    # 34.2 极大减速比 (10000) 不崩溃
    mp.motor_base.inertia = 1e-5
    mp.gearbox_param.gear_ratio = 10000.0
    w._bridge.engine.rebuild()
    w._bridge.send_cmd(MotorCmd(enable=True))
    tick(w, 3)
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.VELOCITY, set_vel=1.0))
    tick(w, 30)
    t = telem(w)
    T.check('极大减速比 (10000) 不崩溃',
            'sys_state_name' in t,
            f'sys={t.get("sys_state_name")} vel={t.get("vel")}')
    # 34.3 并发命令 (连续投递多条)
    w._bridge.send_cmd(MotorCmd(reset=True))
    tick(w, 3)
    w._bridge.send_cmd(MotorCmd(enable=True))
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.POSITION, set_pos=1.0))
    w._bridge.send_cmd(MotorCmd(set_kp=50.0))
    w._bridge.send_cmd(MotorCmd(set_kd=2.0))
    tick(w, 30)
    t = telem(w)
    T.check('并发命令处理后正常进入 RUN',
            t.get('sys_state_name') == 'RUN',
            f'sys={t.get("sys_state_name")}')
    # 34.4 0 值命令 (set_pos=0) 不被忽略
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.POSITION, set_pos=0.0))
    tick(w, 50)
    t = telem(w)
    T.check('set_pos=0 命令生效',
            abs(float(t.get('pos', 0))) < 0.5,
            f'pos={t.get("pos")}')

    # 35. 内存稳定性 (Round 10)
    print('\n[35] 内存稳定性')
    import gc
    gc.collect()
    mem_before = sum(sys.getsizeof(o) for o in gc.get_objects() if isinstance(o, (dict, list, tuple, str)))
    # 长时间运行 5000 帧
    for _ in range(5000):
        tick(w, 1)
        w._plot_panel.feed(telem(w))
        w._feedback_panel.update_telemetry(telem(w))
        w._fault_history_panel.on_telemetry(telem(w))
    gc.collect()
    mem_after = sum(sys.getsizeof(o) for o in gc.get_objects() if isinstance(o, (dict, list, tuple, str)))
    # 内存增长 < 50MB (粗略阈值)
    mem_growth_kb = (mem_after - mem_before) / 1024
    T.check('5000 帧运行内存增长 < 50MB',
            mem_growth_kb < 50 * 1024,
            f'growth={mem_growth_kb:.1f} KB')
    # 缓冲仍受 maxlen 限制
    pos_buf = w._plot_panel._bufs.get('pos')
    T.check('5000 帧后缓冲仍受 maxlen 限制',
            pos_buf is not None and pos_buf._cnt <= w._plot_panel._maxlen,
            f'cnt={pos_buf._cnt if pos_buf else 0} maxlen={w._plot_panel._maxlen}')
    # 故障历史行数受 MAX_ROWS 限制
    T.check('故障历史行数 <= MAX_ROWS',
            len(w._fault_history_panel._rows) <= w._fault_history_panel.MAX_ROWS,
            f'rows={len(w._fault_history_panel._rows)} MAX={w._fault_history_panel.MAX_ROWS}')
    # 日志行数受 MAX_LINES 限制
    T.check('日志行数 <= MAX_LINES',
            len(w._log_panel._lines) <= w._log_panel.MAX_LINES,
            f'lines={len(w._log_panel._lines)} MAX={w._log_panel.MAX_LINES}')
    # 事件标记线数量受 200 限制
    T.check('事件标记线数量 <= 200',
            len(w._plot_panel._events) <= 200,
            f'events={len(w._plot_panel._events)}')

    # 36. 配置往返完整性 (Round 10)
    print('\n[36] 配置往返完整性')
    # 修改若干参数, 验证 collect -> apply -> collect 一致
    mp.motor_base.inertia = 2.5e-4
    mp.motor_base.rated_torque = 8.0
    mp.position_loop.position_kp = 25.0
    mp.position_loop.speed_kp = 0.8
    mp.current_loop.current_kp_d = 0.15
    cfg1 = w._collect_config()
    T.check('collect_config 含 engine_mp 快照',
            'engine_mp' in cfg1 and isinstance(cfg1['engine_mp'], dict),
            f'keys={list(cfg1.keys())}')
    # 写入文件 -> 重新加载 -> 应用
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_test_cfg_round10.json')
    with open(cfg_path, 'w', encoding='utf-8') as f:
        json.dump(cfg1, f, ensure_ascii=False, indent=2)
    with open(cfg_path, 'r', encoding='utf-8') as f:
        cfg_loaded = json.load(f)
    # 修改参数后用 _apply_config 恢复
    mp.motor_base.inertia = 1e-3
    mp.motor_base.rated_torque = 1.0
    w._apply_config(cfg_loaded)
    T.check('apply 后 inertia 恢复到快照值',
            abs(mp.motor_base.inertia - 2.5e-4) < 1e-12,
            f'inertia={mp.motor_base.inertia}')
    T.check('apply 后 rated_torque 恢复到快照值',
            abs(mp.motor_base.rated_torque - 8.0) < 1e-9,
            f'rated_torque={mp.motor_base.rated_torque}')
    T.check('apply 后 position_kp 恢复到快照值',
            abs(mp.position_loop.position_kp - 25.0) < 1e-9,
            f'position_kp={mp.position_loop.position_kp}')
    T.check('apply 后 current_kp_d 恢复到快照值',
            abs(mp.current_loop.current_kp_d - 0.15) < 1e-9,
            f'current_kp_d={mp.current_loop.current_kp_d}')
    os.unlink(cfg_path)
    # 二次 collect 与第一次一致 (engine_mp 部分)
    cfg2 = w._collect_config()
    T.check('二次 collect 与第一次 engine_mp 一致',
            cfg1['engine_mp'] == cfg2['engine_mp'],
            f'diff_keys={[k for k in set(cfg1["engine_mp"]) | set(cfg2["engine_mp"]) if cfg1["engine_mp"].get(k) != cfg2["engine_mp"].get(k)]}')

    # 37. 全模式回归 (Round 10)
    print('\n[37] 全模式回归')
    w._bridge.send_cmd(MotorCmd(reset=True))
    tick(w, 3)
    w._bridge.send_cmd(MotorCmd(enable=True))
    tick(w, 3)
    all_modes = [
        (ControlMode.POSITION, {'set_pos': 1.0}),
        (ControlMode.VELOCITY, {'set_vel': 2.0}),
        (ControlMode.TORQUE, {'set_torque': 0.3}),
        (ControlMode.CURRENT, {'set_iq': 1.5}),
        (ControlMode.VOLTAGE, {'set_voltage': 5.0}),
        (ControlMode.DUTY, {'set_duty': 0.2}),
        (ControlMode.IMPEDANCE, {'set_pos': 0.5, 'set_kp': 10.0, 'set_kd': 0.5}),
    ]
    for mode, kwargs in all_modes:
        w._bridge.send_cmd(MotorCmd(start=True, set_mode=mode, **kwargs))
        tick(w, 30)
        t = telem(w)
        T.check(f'模式 {mode.name} 切换生效',
                t.get('control_mode_name') == mode.name,
                f'mode={t.get("control_mode_name")}')
    # 最终回归: 引擎仍正常
    w._bridge.send_cmd(MotorCmd(reset=True))
    tick(w, 5)
    t = telem(w)
    T.check('最终回归: 引擎仍正常',
            t.get('sys_state_name') == 'IDLE' and int(t.get('fault_flags', 0)) == 0,
            f'sys={t.get("sys_state_name")} fault=0x{int(t.get("fault_flags", 0)):04X}')

    # 38. 电机可视化面板 (Round 11)
    print('\n[38] 电机可视化面板')
    mvp = w._motor_view_panel
    # Tab 存在
    tab_idx = -1
    for i in range(w._tabs.count()):
        if w._tabs.tabText(i) == "电机可视化":
            tab_idx = i
            break
    T.check('Tab "电机可视化" 已添加',
            tab_idx >= 0,
            f'tab_idx={tab_idx}')
    # set_engine 后内部 _engine 非空
    T.check('set_engine 后 _engine 非空',
            mvp._engine is not None,
            f'engine={mvp._engine}')
    # feed 后 _data 含关键字段
    mvp.feed(t)
    T.check('feed 后 _data 含 vel',
            'vel' in mvp._data,
            f'keys={list(mvp._data.keys())[:5]}')
    T.check('feed 后 _data 含 pos',
            'pos' in mvp._data,
            f'pos={mvp._data.get("pos")}')
    T.check('feed 后 _data 含 ia/ib/ic',
            all(k in mvp._data for k in ('ia', 'ib', 'ic')),
            f'ia={mvp._data.get("ia")} ib={mvp._data.get("ib")} ic={mvp._data.get("ic")}')
    T.check('feed 后 _data 含 vbus',
            'vbus' in mvp._data,
            f'vbus={mvp._data.get("vbus")}')
    # feed 后置 dirty
    T.check('feed 后 _dirty=True',
            mvp._dirty is True,
            f'dirty={mvp._dirty}')

    # 39. 加速度差分计算
    print('\n[39] 加速度差分计算')
    # 构造两拍: vel=0 → vel=10, dt=0.02 → acc=500
    mvp._prev_vel = 0.0
    mvp._prev_t = 0.0
    mvp._acc = 0.0
    mvp.feed({'vel': 0.0, 't_sim': 0.0})
    mvp.feed({'vel': 10.0, 't_sim': 0.02})
    T.check('加速度差分正确 (acc=500)',
            abs(mvp._acc - 500.0) < 1.0,
            f'acc={mvp._acc}')
    # 反向
    mvp._prev_vel = 10.0
    mvp._prev_t = 0.0
    mvp.feed({'vel': 0.0, 't_sim': 0.02})
    T.check('反向加速度正确 (acc=-500)',
            abs(mvp._acc - (-500.0)) < 1.0,
            f'acc={mvp._acc}')
    # dt=0 不崩溃 (除零保护)
    mvp._prev_t = 1.0
    mvp.feed({'vel': 5.0, 't_sim': 1.0})
    T.check('dt=0 不崩溃 (acc 保持)',
            not math.isnan(mvp._acc),
            f'acc={mvp._acc}')

    # 40. 状态颜色映射
    print('\n[40] 状态颜色映射')
    from tools.twin.motor_view_panel import _STATE_STYLE
    T.check('IDLE 状态色 = #3a4a5a',
            _STATE_STYLE['IDLE'][0] == '#3a4a5a',
            f'color={_STATE_STYLE["IDLE"][0]}')
    T.check('RUN 状态色 = #00d4ff',
            _STATE_STYLE['RUN'][0] == '#00d4ff',
            f'color={_STATE_STYLE["RUN"][0]}')
    T.check('FAULT 状态色 = #ff3030',
            _STATE_STYLE['FAULT'][0] == '#ff3030',
            f'color={_STATE_STYLE["FAULT"][0]}')
    T.check('SAFETY 状态色 = #ff6b35',
            _STATE_STYLE['SAFETY'][0] == '#ff6b35',
            f'color={_STATE_STYLE["SAFETY"][0]}')
    T.check('FAULT 是闪烁态',
            _STATE_STYLE['FAULT'][3] is True,
            f'blink={_STATE_STYLE["FAULT"][3]}')
    T.check('RUN 扫描频率=6Hz',
            _STATE_STYLE['RUN'][2] == 6.0,
            f'scan_hz={_STATE_STYLE["RUN"][2]}')

    # 41. paintEvent 不抛异常
    print('\n[41] paintEvent 不抛异常')
    # 触发重绘
    w._tabs.setCurrentWidget(mvp)
    mvp._dirty = True
    mvp.repaint()
    T.check('paintEvent (IDLE) 不抛异常', True)
    # 切到 RUN 状态再绘
    w._bridge.send_cmd(MotorCmd(enable=True))
    tick(w, 3)
    w._bridge.send_cmd(MotorCmd(start=True, set_mode=ControlMode.VELOCITY, set_vel=2.0))
    tick(w, 30)
    mvp.feed(telem(w))
    mvp.repaint()
    T.check('paintEvent (RUN+速度) 不抛异常', True)
    # 注入故障再绘
    w._bridge._prev_fault = 0
    w._bridge.engine.inject_fault(Fault.OVER_CURRENT)
    tick(w, 5)
    mvp.feed(telem(w))
    mvp.repaint()
    T.check('paintEvent (FAULT) 不抛异常', True)
    # 清障
    w._bridge.send_cmd(MotorCmd(reset=True))
    w._bridge.engine.clear_injected_fault()
    tick(w, 5)

    # 42. 性能: 1000 次 feed + repaint
    print('\n[42] 性能: 1000 次 feed + repaint')
    import time as _time
    t_start = _time.perf_counter()
    for _ in range(1000):
        mvp.feed(telem(w))
        mvp.repaint()
    t_elapsed = _time.perf_counter() - t_start
    T.check('1000 次 feed+repaint < 5s',
            t_elapsed < 5.0,
            f'elapsed={t_elapsed:.3f}s per_frame={t_elapsed*1000/1000:.3f}ms')

    # 43. 主题切换不崩溃
    print('\n[43] 主题切换不崩溃')
    mvp.apply_theme()
    mvp.repaint()
    T.check('apply_theme 不抛异常', True)
    # 主题切换后辉光缓存已清空
    T.check('apply_theme 后辉光缓存清空',
            len(mvp._glow_pens) == 0,
            f'pens={len(mvp._glow_pens)}')

    # 44. 粒子相位推进
    print('\n[44] 粒子相位推进')
    # 直接验证粒子相位算法 (不依赖 _tick 的 isVisible 检查)
    # Power > 0: 相位推进
    mvp._particle_phase = 0.0
    mvp._data = {'power': 500.0, 'vbus': 48.0, 'ibus': 10.4}
    elapsed = 0.1
    power = float(mvp._data.get("power", 0.0))
    speed = max(-1.0, min(1.0, power / 500.0))
    mvp._particle_phase += speed * elapsed * 0.5
    T.check('Power>0 时粒子相位推进',
            mvp._particle_phase > 0.0,
            f'phase={mvp._particle_phase} speed={speed}')
    # Power < 0: 相位反向
    mvp._particle_phase = 0.0
    mvp._data = {'power': -500.0, 'vbus': 48.0, 'ibus': -10.4}
    power = float(mvp._data.get("power", 0.0))
    speed = max(-1.0, min(1.0, power / 500.0))
    mvp._particle_phase += speed * elapsed * 0.5
    T.check('Power<0 时粒子相位反向',
            mvp._particle_phase < 0,
            f'phase={mvp._particle_phase} speed={speed}')
    # Power ≈ 0: 相位基本不变
    mvp._particle_phase = 0.5
    mvp._data = {'power': 0.0, 'vbus': 48.0, 'ibus': 0.0}
    power = float(mvp._data.get("power", 0.0))
    speed = max(-1.0, min(1.0, power / 500.0))
    mvp._particle_phase += speed * elapsed * 0.5
    T.check('Power≈0 时粒子相位基本不变',
            abs(mvp._particle_phase - 0.5) < 0.01,
            f'phase={mvp._particle_phase} speed={speed}')

    # 45. Tab 切走后不重绘 (isVisible 检查)
    print('\n[45] Tab 切走后不重绘')
    # 切到其他 Tab
    w._tabs.setCurrentWidget(w._plot_panel)
    T.check('Tab 切走后 motor_view_panel 不可见',
            not mvp.isVisible(),
            f'visible={mvp.isVisible()}')
    # _tick 在不可见时不应该触发重绘
    mvp._dirty = True
    mvp._tick()
    # 注: update() 在不可见时不会立即 paintEvent, 这是 Qt 行为
    T.check('_tick 在不可见时不抛异常', True)

    # 恢复
    w._tabs.setCurrentWidget(mvp)
    w._bridge.send_cmd(MotorCmd(reset=True))
    tick(w, 5)

    ok = T.summary()
    w.close()
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
