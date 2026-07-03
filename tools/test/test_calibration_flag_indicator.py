"""Task 5: 标定状态指示器 + 设置/清除已标定按钮测试。"""

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
from ui.panels.calibration_panel import CalibrationPanel
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

# 1. 状态卡含已标定指示器 + 标记/清除按钮
check(hasattr(panel, "_lbl_calib_flag"), "应有 _lbl_calib_flag 指示器标签")
check(hasattr(panel, "_btn_mark_calibrated"), "应有 _btn_mark_calibrated 按钮")
check(hasattr(panel, "_btn_clear_calibrated"), "应有 _btn_clear_calibrated 按钮")

# 2. 初始为未读取态
check("未读取" in panel._lbl_calib_flag.text(),
      f"初始应为 '未读取', 实际 {panel._lbl_calib_flag.text()!r}")

# 3. set_calibrated_value("1") -> 显示 "已标定"
panel.set_calibrated_value("1")
check("已标定" in panel._lbl_calib_flag.text(),
      f"值=1 应显示 '已标定', 实际 {panel._lbl_calib_flag.text()!r}")

# 4. set_calibrated_value("0") -> 显示 "未标定"
panel.set_calibrated_value("0")
check("未标定" in panel._lbl_calib_flag.text(),
      f"值=0 应显示 '未标定', 实际 {panel._lbl_calib_flag.text()!r}")

# 5. set_calibrated_value("") -> 未读取
panel.set_calibrated_value("")
check("未读取" in panel._lbl_calib_flag.text(),
      f"空值应显示 '未读取', 实际 {panel._lbl_calib_flag.text()!r}")

# 6. set_calibrated_value("abc") -> 未读取(容错)
panel.set_calibrated_value("abc")
check("未读取" in panel._lbl_calib_flag.text(),
      f"非数字应显示 '未读取', 实际 {panel._lbl_calib_flag.text()!r}")

# 7. 点 "标记" 按钮: 乐观更新为已标定 + 发 _config_panel.write_param(16, "1")
#    先注入一个 mock results_panel 捕获 write_param
from jmproto.registry import get_registry
from ui.panels.param_panel import ParamPanel
reg = get_registry()
mock_results = ParamPanel(reg, source="motor_config", groups=("MotorCalibParam",))
panel.attach_results_panel(mock_results)

panel.set_link_active(True)
writes = []
mock_results.write_param.connect(lambda pid, text: writes.append((int(pid), text)))
panel._btn_mark_calibrated.click()
check("已标定" in panel._lbl_calib_flag.text(),
      f"点标记后应乐观显示 '已标定', 实际 {panel._lbl_calib_flag.text()!r}")
check(len(writes) == 1, f"点标记应发 1 条 write_param, 实际 {len(writes)}")
if writes:
    check(writes[0][0] == 16, f"应写 Index 16 (is_calibrated), 实际 {writes[0][0]}")
    check(writes[0][1] == "1", f"应写值 '1', 实际 {writes[0][1]!r}")

# 8. 点 "清除" 按钮: 乐观更新为未标定 + 发 write_param(16, "0")
writes.clear()
panel._btn_clear_calibrated.click()
check("未标定" in panel._lbl_calib_flag.text(),
      f"点清除后应乐观显示 '未标定', 实际 {panel._lbl_calib_flag.text()!r}")
check(len(writes) == 1, f"点清除应发 1 条 write_param, 实际 {len(writes)}")
if writes:
    check(writes[0] == (16, "0"), f"应写 (16, '0'), 实际 {writes[0]}")

# 9. 未连接时点按钮: 不发 write_param
panel.set_link_active(False)
writes.clear()
panel._btn_mark_calibrated.click()
check(len(writes) == 0, f"未连接点标记不应发 write_param, 实际 {len(writes)}")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
