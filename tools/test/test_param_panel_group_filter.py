"""ParamPanel 分组过滤测试: 验证 groups 参数能限定展示的参数分组。"""

import os
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, '..', '..')))

from PyQt6.QtWidgets import QApplication
from jmproto.registry import get_registry
from ui.panels.param_panel import ParamPanel

app = QApplication.instance() or QApplication(sys.argv)
reg = get_registry()

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


# 1. 不传 groups(向后兼容): motor_config 应包含全部分组(>=2 个)
panel_all = ParamPanel(reg, source="motor_config")
check(len(panel_all._group_param_ids) >= 2,
      f"无过滤应含多个分组, 实际 {len(panel_all._group_param_ids)}")

# 2. 传 groups=("MotorCalibParam",): 只剩这一个分组
panel_filt = ParamPanel(reg, source="motor_config", groups=("MotorCalibParam",))
groups_seen = list(panel_filt._group_param_ids.keys())
check(groups_seen == ["MotorCalibParam"],
      f"过滤后应仅含 MotorCalibParam, 实际 {groups_seen}")

# 3. 过滤后面板应包含标定段 Index 16~42 的若干项, 不含 SystemParam 的 Index 0
pids = set(panel_filt._param_specs.keys())
check(16 in pids, "应包含 is_calibrated (Index 16)")
check(38 in pids, "应包含 elec_angle_bias (Index 38)")
check(0 not in pids, "不应包含 SystemParam 的 Index 0 (config_version)")

# 4. 传不存在的分组: 面板为空但不报错
panel_empty = ParamPanel(reg, source="motor_config", groups=("NotExist",))
check(len(panel_empty._param_specs) == 0,
      f"不存在分组应得到空面板, 实际 {len(panel_empty._param_specs)}")

# 5. 运行时参数源同样支持过滤
panel_rt = ParamPanel(reg, source="motor_param", groups=("电机本体",))
check(len(panel_rt._group_param_ids) == 1,
      f"运行时参数过滤应剩 1 组, 实际 {len(panel_rt._group_param_ids)}")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
