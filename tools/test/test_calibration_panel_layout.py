"""CalibrationPanel 紧凑状态卡 + QTabWidget 任务网格测试。"""

import os
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, '..', '..')))

from PyQt6.QtWidgets import QApplication, QTabWidget
from ui.panels.calibration_panel import CalibrationPanel, _CALIB_LEVELS
from jmproto import JmCmd

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


panel = CalibrationPanel()

# 1. L1~L7 共 7 个级别, 子项总数 29(L2 拆分 6 子项 + L5.1 齿槽拆 3 方案)
check(len(_CALIB_LEVELS) == 7, f"应有 7 个级别, 实际 {len(_CALIB_LEVELS)}")
total_subs = sum(len(subs) for _, _, subs in _CALIB_LEVELS)
check(total_subs == 29, f"子项总数应为 29, 实际 {total_subs}")

# 2. L2 已拆为 6 子项, submode 3/4/5/6 分别对应 R/Ld/Lq/flux (元组: sub_id, method, 名, 描述)
l2 = _CALIB_LEVELS[1]
check(l2[0] == JmCmd.CALIB_LEVEL2, "L2 应对应 CALIB_LEVEL2")
l2_subs = {sid: name for sid, _m, name, _d in l2[2]}
check(l2_subs.get(3) == "R 相电阻", f"L2 submode=3 应为 'R 相电阻', 实际 {l2_subs.get(3)}")
check(l2_subs.get(4) == "Ld d轴电感", f"L2 submode=4 应为 'Ld d轴电感', 实际 {l2_subs.get(4)}")
check(l2_subs.get(5) == "Lq q轴电感", f"L2 submode=5 应为 'Lq q轴电感', 实际 {l2_subs.get(5)}")
check(l2_subs.get(6) == "flux 磁链", f"L2 submode=6 应为 'flux 磁链', 实际 {l2_subs.get(6)}")

# 3. _task_tabs 是 QTabWidget, 共 7 个 Tab
check(isinstance(panel._task_tabs, QTabWidget),
      f"_task_tabs 应为 QTabWidget, 实际 {type(panel._task_tabs)}")
check(panel._task_tabs.count() == 7,
      f"Tab 数应为 7, 实际 {panel._task_tabs.count()}")

# 4. _task_buttons 字典 key 为 (cmd, sub_id, method), 全部 29 个
check(len(panel._task_buttons) == 29,
      f"任务按钮数应为 29, 实际 {len(panel._task_buttons)}")

# 4b. L5.1 齿槽三方案共用 submode=1, method=1/2/3 区分; 按钮单行短标题, 详细方案在 tooltip
l5 = _CALIB_LEVELS[4]
check(l5[0] == JmCmd.CALIB_LEVEL5, "L5 应对应 CALIB_LEVEL5")
cog = {(sid, m): (name, desc) for sid, m, name, desc in l5[2] if sid == 1}
check(set(cog.keys()) == {(1, 1), (1, 2), (1, 3)},
      f"齿槽三方案 method 应为 1/2/3, 实际 {sorted(cog.keys())}")
check(cog[(1, 3)][0] == "齿槽 速度环匀速", f"方案3名应为 '齿槽 速度环匀速', 实际 {cog[(1, 3)][0]}")
for (sid, m), (name, desc) in cog.items():
    b = panel._task_buttons[(int(JmCmd.CALIB_LEVEL5), sid, m)]
    check(b.text() == name, f"按钮应只显示短标题 {name!r}, 实际 {b.text()!r}")
    check(desc in b.toolTip(), f"tooltip 应含详细方案描述: {name!r}")
# 非齿槽子项保持 标题+描述 两行
b_friction = panel._task_buttons[(int(JmCmd.CALIB_LEVEL5), 2, 0)]
check(b_friction.text() == "摩擦\n摩擦模型(库仑+粘性)辨识",
      f"非齿槽按钮应保持两行文本, 实际 {b_friction.text()!r}")

# 5. 点击子项按钮只选中(不发送), 由「开始」按钮统一发送
panel.set_link_active(True)
sent = []
panel.send_command.connect(lambda c, v: sent.append((int(c), dict(v))))
btn = panel._task_buttons[(int(JmCmd.CALIB_LEVEL2), 3, 0)]  # R 相电阻
btn.click()
check(sent == [], f"点击卡片只选中, 不应发命令, 实际 {sent}")
check(panel._active_task_key == (int(JmCmd.CALIB_LEVEL2), 3, 0),
      f"点击后应选中 (0x91, 3, 0), 实际 {panel._active_task_key}")
check(panel._btn_start.isEnabled(), "选中后「开始」按钮应可用")
# 点「开始」才发命令
panel._btn_start.click()
check(sent == [(int(JmCmd.CALIB_LEVEL2), {"submode": 3})],
      f"点开始应发 (0x91, submode=3), 实际 {sent}")

# 5b. 齿槽·静态逐点(method=2) 开始应发 submode=1 + method=2 (0x94 payload: 01 02)
panel._task_buttons[(int(JmCmd.CALIB_LEVEL5), 1, 2)].click()
panel._btn_start.click()
check(sent[-1] == (int(JmCmd.CALIB_LEVEL5), {"submode": 1, "method": 2}),
      f"齿槽静态逐点应发 submode=1 method=2, 实际 {sent[-1]}")

# 6. 状态卡存在且含 "当前选中" 标签
check(hasattr(panel, "_lbl_active_task"), "应有 _lbl_active_task 标签")
check(hasattr(panel, "_status_card"), "应有 _status_card")

# 7. 点击后 _lbl_active_task 文本含 "L2" + "submode=3" + "R 相电阻"
# (5b 切到了齿槽, 先恢复 L2 选中再校验)
panel._task_buttons[(int(JmCmd.CALIB_LEVEL2), 3, 0)].click()
txt = panel._lbl_active_task.text()
check("L2" in txt and "submode=3" in txt and "R 相电阻" in txt,
      f"当前选中任务文本应含 L2/submode=3/R 相电阻, 实际: {txt!r}")

# 8. 标定操作嵌入状态卡: _btn_query / _btn_abort 存在
# 需求1: 移除 自动查询 复选框(_chk_auto_poll), 改为开始后固定 500ms 轮询
check(hasattr(panel, "_btn_query") and hasattr(panel, "_btn_abort"),
      "状态卡应嵌入 查询/中止 按钮")
check(not hasattr(panel, "_chk_auto_poll"), "需求1: 不应再有 自动查询 开关(_chk_auto_poll)")
check(hasattr(panel, "_poll_timer"), "需求1: 应有 _poll_timer(开始后固定 500ms 轮询)")
check(not hasattr(panel, "_spin_poll_period"), "需求1: 不应再有 查询周期 SpinBox(_spin_poll_period)")

# 9. get_opts 含 task_tab_index + active_task
opts = panel.get_opts()
check("task_tab_index" in opts, f"get_opts 应含 task_tab_index, 实际 {opts}")
check(opts["task_tab_index"] == panel._task_tabs.currentIndex(),
      "task_tab_index 应等于当前 Tab 索引")

# 10. set_opts 恢复 task_tab_index
panel._task_tabs.setCurrentIndex(0)
panel.set_opts({"task_tab_index": 2})
check(panel._task_tabs.currentIndex() == 2,
      f"set_opts 应恢复 Tab 索引为 2, 实际 {panel._task_tabs.currentIndex()}")

# 11. set_opts 恢复 active_task (cmd, sub_id)
panel.set_opts({"active_task": [int(JmCmd.CALIB_LEVEL3), 2]})
txt2 = panel._lbl_active_task.text()
check("L3" in txt2 and "submode=2" in txt2,
      f"set_opts active_task 应恢复 L3 submode=2, 实际: {txt2!r}")

# 12. 状态卡高度应紧凑(无 _combo_level / _combo_submode / QFormLayout)
check(not hasattr(panel, "_combo_level"), "不应再有 _combo_level(改用 Tab)")
check(not hasattr(panel, "_combo_submode"), "不应再有 _combo_submode(改用卡片)")
check(hasattr(panel, "_btn_start"), "应有 _btn_start(右侧操作列, 统一启动)")

# 13. main_window 契约方法仍在
check(callable(getattr(panel, "set_link_active", None)), "应有 set_link_active")
check(callable(getattr(panel, "update_state", None)), "应有 update_state")
check(callable(getattr(panel, "on_ack", None)), "应有 on_ack")
check(callable(getattr(panel, "on_nack", None)), "应有 on_nack")
check(callable(getattr(panel, "apply_theme", None)), "应有 apply_theme")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
