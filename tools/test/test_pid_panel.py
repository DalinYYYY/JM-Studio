"""PidPanel 面板结构/公共 API/信号冒烟测试。"""

import os
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, '..', '..')))

from PyQt6.QtWidgets import QApplication, QGroupBox

from ui.panels.pid_panel import PidPanel, PidSource, PidRing

app = QApplication.instance() or QApplication(sys.argv)

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {msg}")


# ---- 构造 ----
panel = PidPanel()

# 1. 是 QGroupBox 子类
check(isinstance(panel, QGroupBox), f"应为 QGroupBox 子类, 实际 {type(panel)}")

# 2. 公共 API 存在
for method in ('set_link_active', 'update_state', 'on_autotune_result',
               'on_ack', 'on_nack', 'attach_params_panel',
               'apply_theme', 'get_opts', 'set_opts',
               'set_history_visible'):
    check(hasattr(panel, method), f"缺少公共方法: {method}")

# 3. 信号存在
check(hasattr(panel, 'pid_autotune_requested'), "缺少信号 pid_autotune_requested")
check(hasattr(panel, 'pid_source_set_requested'), "缺少信号 pid_source_set_requested")

# ---- 理论估计操作控件 ----
check(hasattr(panel, '_autotune_ring_checks'), "缺少 _autotune_ring_checks")
check(len(panel._autotune_ring_checks) == 3, f"应有 3 个环勾选框, 实际 {len(panel._autotune_ring_checks)}")
check(hasattr(panel, '_cur_bw_spin'), "缺少 _cur_bw_spin")
check(hasattr(panel, '_vel_bw_spin'), "缺少 _vel_bw_spin")
check(hasattr(panel, '_pos_bw_spin'), "缺少 _pos_bw_spin")
check(hasattr(panel, '_autotune_btn'), "缺少 _autotune_btn")

# ---- 来源切换控件 ----
check(hasattr(panel, '_source_btn_groups'), "缺少 _source_btn_groups")
check(len(panel._source_btn_groups) == 3, f"应有 3 个环单选组, 实际 {len(panel._source_btn_groups)}")
check(hasattr(panel, '_source_apply_btn'), "缺少 _source_apply_btn")

# ---- 结果/历史 Splitter ----
check(hasattr(panel, '_params_split'), "缺少 _params_split")
check(panel._params_split is not None, "_params_split 不应为 None")
check(hasattr(panel, '_history_view'), "缺少 _history_view")
check(panel._history_view is not None, "_history_view 不应为 None")


# ---- 信号发射测试: 理论估计 ----
received_autotune = []
panel.pid_autotune_requested.connect(
    lambda r, c, v, p: received_autotune.append((r, c, v, p)))
# 模拟已连接 IDLE 态
panel._link_active = True
panel._current_top_fsm = int(3)  # TopFsm.IDLE = 3
# 全部三环勾选
for chk in panel._autotune_ring_checks.values():
    chk.setChecked(True)
panel._cur_bw_spin.setValue(1000.0)
panel._on_autotune_clicked()
check(len(received_autotune) == 1, f"理论估计应发出 1 个信号, 实际 {len(received_autotune)}")
if received_autotune:
    check(received_autotune[0][0] == 3, f"ring_select 应为 3(全部), 实际 {received_autotune[0][0]}")
    check(received_autotune[0][1] == 1000.0, f"cur_bw 应为 1000.0, 实际 {received_autotune[0][1]}")


# ---- 信号发射测试: 来源切换 ----
received_source = []
panel.pid_source_set_requested.connect(
    lambda r, s: received_source.append((r, s)))
# 电流环选 Flash(1)
panel._source_btn_groups[0].button(1).setChecked(True)
panel._on_source_apply_clicked()
check(len(received_source) >= 1, f"来源切换应发出 >=1 个信号, 实际 {len(received_source)}")
check((0, 1) in received_source, f"(0,1) 应在发出的信号中, 实际 {received_source}")


# ---- 0x9A 成功应答: source 更新为 AUTOTUNE ----
old_source = dict(panel._ring_source)
panel.on_autotune_result({
    'ok': True, 'status': 0, 'fail_reason': 0,
    'ring_select_done': 3, 'status_cn': '成功',
    'fail_reason_cn': '无',
})
check(panel._ring_source[0] == 2, f"电流环 source 应为 2(AUTOTUNE), 实际 {panel._ring_source[0]}")
check(panel._ring_source[1] == 2, f"速度环 source 应为 2(AUTOTUNE), 实际 {panel._ring_source[1]}")
check(panel._ring_source[2] == 2, f"位置环 source 应为 2(AUTOTUNE), 实际 {panel._ring_source[2]}")


# ---- 0x9A 失败应答: source 不变 ----
panel2 = PidPanel()
old_source2 = dict(panel2._ring_source)
panel2.on_autotune_result({
    'ok': False, 'status': 1, 'fail_reason': 1,
    'ring_select_done': 0, 'status_cn': '失败',
    'fail_reason_cn': '辨识未就绪',
})
check(panel2._ring_source == old_source2, "失败应答后 source 不应改变")


# ---- attach_params_panel 测试 ----
from jmproto.registry import ProtocolRegistry
from ui.panels.param_panel import ParamPanel
reg = ProtocolRegistry()
pp = ParamPanel(reg, title="PID 参数", source="motor_config",
                groups=("ControlParam",), show_save=True, show_bulk_rw=True)
panel3 = PidPanel()
panel3.attach_params_panel(pp)
check(panel3._params_panel is pp, "attach 后 _params_panel 应为注入的 pp")
check(panel3._params_split.indexOf(pp) >= 0, "ParamPanel 应在 Splitter 中")


# ---- 主窗口 Tab 注册测试 ----
from ui.main_window import MainWindow
win = MainWindow()
tab_titles = []
for i in range(win._tabs.count()):
    tab_titles.append(win._tabs.tabText(i))
check("PID 整定" in tab_titles, f"主窗口应含 'PID 整定' Tab, 实际 {tab_titles}")
check(hasattr(win, '_pid_panel'), "主窗口应有 _pid_panel 属性")
check(hasattr(win, '_pid_params_panel'), "主窗口应有 _pid_params_panel 属性")


print(f"\n{'='*40}")
print(f"PID Panel 测试: {PASS} passed, {FAIL} failed")
print(f"{'='*40}")
if FAIL > 0:
    sys.exit(1)
