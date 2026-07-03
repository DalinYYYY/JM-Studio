"""Task 3: 内嵌标定结果 ParamPanel 注入测试。"""

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
from ui.panels.calibration_panel import CalibrationPanel
from ui.panels.param_panel import ParamPanel

app = QApplication.instance() or QApplication(sys.argv)
reg = get_registry()

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {msg}")


# 模拟 main_window 创建标定结果面板的步骤
calib = CalibrationPanel()
results_panel = ParamPanel(
    reg, title="标定结果", source="motor_config",
    groups=("MotorCalibParam",), show_save=True,
    save_text="保存标定结果到Flash/EEPROM")
calib.attach_results_panel(results_panel)

# 1. 注入后 calib._config_panel 指向 results_panel
check(calib._config_panel is results_panel,
      "attach_results_panel 后 _config_panel 应指向注入的面板")

# 2. 注入后面板含 MotorCalibParam 段(Index 16~42 的若干项)
pids = set(results_panel._param_specs.keys())
check(16 in pids, "标定结果面板应含 is_calibrated (Index 16)")
check(20 in pids, "标定结果面板应含 phase_resistance (Index 20)")
check(0 not in pids, "不应含 SystemParam 的 Index 0")
check(len(pids) >= 10, f"标定结果应含多项(>=10), 实际 {len(pids)}")

# 3. results_panel 暴露 read_param/write_param/read_params/write_params/save_all 信号
for sig in ("read_param", "write_param", "read_params", "write_params", "save_all"):
    check(hasattr(results_panel, sig), f"results_panel 应暴露 {sig} 信号")

# 4. show_save=True 时面板有保存按钮(检查 _btn_save 或类似)
#    ParamPanel 内部命名可能为 _save_btn / _btn_save, 容错检查
save_btn = getattr(results_panel, "_save_btn", None) or getattr(results_panel, "_btn_save", None)
check(save_btn is not None, "show_save=True 应创建保存按钮")

# 5. get_opts 返回 config_panel 子项(列宽等)
opts = calib.get_opts()
check("config_panel" in opts, f"get_opts 应含 config_panel, 实际 {list(opts.keys())}")

# 6. 重复 attach 应被忽略(防止重复注入)
other = ParamPanel(reg, source="motor_config", groups=("MotorCalibParam",))
calib.attach_results_panel(other)
check(calib._config_panel is results_panel,
      "重复 attach 应被忽略, _config_panel 不变")

# 7. apply_theme 传播到内嵌面板(不报错)
try:
    calib.apply_theme()
    check(True, "apply_theme 应传播到内嵌面板不报错")
except Exception as e:
    check(False, f"apply_theme 报错: {e}")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
