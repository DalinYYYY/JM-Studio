"""长时间仿真诊断: 找出运行一会后触发的具体故障位。
逐项监控: 过流/过压/欠压/过温/跟随误差/通信丢失/位置超限。
"""
import sys, os, types, struct

# PyQt stub (无 Qt 环境)
pkg_qt = types.ModuleType('PyQt6')
pkg_qtc = types.ModuleType('PyQt6.QtCore')
class _Q: pass
class _S:
    def __init__(self, *a, **kw): pass
    def emit(self, *a, **kw): pass
def _ps(*a, **kw): return _S()
pkg_qtc.QObject = _Q
pkg_qtc.pyqtSignal = _ps
pkg_qtc.QTimer = _Q
sys.modules['PyQt6'] = pkg_qt
sys.modules['PyQt6.QtCore'] = pkg_qtc

HERE = os.path.dirname(os.path.abspath(__file__))
PYQT_GUI = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, PYQT_GUI)

pkg_t = types.ModuleType('transport')
pkg_t.__path__ = [os.path.join(PYQT_GUI, 'transport')]
sys.modules['transport'] = pkg_t
pkg_ve = types.ModuleType('transport.virtual_engine')
pkg_ve.__path__ = [HERE]
sys.modules['transport.virtual_engine'] = pkg_ve

from transport.virtual_engine.twin_responder import TwinResponder
from transport.virtual_engine.digital_twin import DigitalTwinEngine
from transport.virtual_engine.twin_fsm import Fault
from jmproto import JmCmd

eng = DigitalTwinEngine()
resp = TwinResponder(eng)

# 打印阈值配置
th = eng.fault_detector.th
print(f'阈值: over_current={th.over_current}A, over_volt={th.over_voltage}V, '
      f'under_volt={th.under_voltage}V, fet={th.over_temp_fet}C, motor={th.over_temp_motor}C, '
      f'follow_err={th.follow_err}rad, comm_timeout={th.comm_timeout}s')
print(f'pos_limit=[{eng.fsm.pos_limit_min}, {eng.fsm.pos_limit_max}]')

# 使能 + 位置模式(目标 1.0 rad)
resp.on_command(int(JmCmd.ENABLE), b'')
# target_pos 单位=电机端 rad(对齐固件 multiturn=theta_m)
resp.on_command(int(JmCmd.POSITION), struct.pack('<f', 10.0))
print(f'启动: sys={eng.fsm.sys_state.name}, target_pos={eng.fsm.target_pos} (电机端 rad)')

# 长时间运行 5 秒, 每 0.5s 打印全部状态
print('\n[t]     sys     pos     vel    iq     vbus   fetC  motC  follw_err  fault')
last_fault = 0
fault_time = None
for k in range(50):
    resp.advance(0.1)
    t = eng._last_telemetry
    t_now = (k + 1) * 0.1
    if t["fault_flags"] != 0 and last_fault == 0:
        fault_time = t_now
        print(f'>>> 首次故障 @ {t_now:.1f}s: 0x{t["fault_flags"]:04X} '
              f'[{Fault.to_str(t["fault_flags"])}]')
        # 详细诊断
        print(f'    pos={t["pos"]:.4f}, vel={t["vel"]:.4f}, iq={t["iq"]:.4f}, '
              f'vbus={t["vbus"]:.2f}, fet={t["temp_fet"]:.1f}, motor={t["temp_motor"]:.1f}, '
              f'follow_err={t["follow_err"]:.4f}')
    last_fault = t["fault_flags"]
    if k % 5 == 4 or t["fault_flags"] != 0:
        snap = eng.physics.snapshot()
        print(f'{t_now:4.1f}s  {t["sys_state_name"]:6s}  pos_out={t["pos"]:+.3f}  '
              f'vel_out={t["vel"]:+.3f}  omega_m={snap["omega_m"]:+.2f}  '
              f'theta_m={snap["theta_m"]:+.3f}  '
              f'vel_sp={t["vel_setpoint"]:+.3f}  '
              f'int_v={eng.controller.cascade.pid_vel.integral:+.3f}  '
              f'iq={t["iq"]:+.3f}  '
              f'0x{t["fault_flags"]:04X}')

# 最终
t = eng._last_telemetry
print(f'\n最终: sys={t["sys_state_name"]}, pos={t["pos"]:.4f}, vel={t["vel"]:.4f}, '
      f'fault=0x{t["fault_flags"]:04X} [{Fault.to_str(t["fault_flags"])}]')
if fault_time:
    print(f'>>> 故障首次出现时间: {fault_time:.2f}s')
