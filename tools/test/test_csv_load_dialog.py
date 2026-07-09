"""CsvLoadDialog 测试: 验证初始路径展示、恢复默认、chosen_paths 返回值。"""
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
from ui.csv_load_dialog import CsvLoadDialog
from jmproto.registry import DEFAULT_MOTOR_CONFIG_CSV, DEFAULT_PARAMS_CSV

app = QApplication.instance() or QApplication(sys.argv)

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


# 1. 初始路径应回显当前值
dlg = CsvLoadDialog("/tmp/custom_motor.csv", "/tmp/custom_param.csv")
paths = dlg.chosen_paths()
check(paths["motor_config"] == "/tmp/custom_motor.csv", "应回显当前 motor_config 路径")
check(paths["motor_param"] == "/tmp/custom_param.csv", "应回显当前 param 路径")

# 2. 恢复默认按钮应把路径改回打包内置默认
dlg._mc_edit.setText("/some/other.csv")
dlg._reset_mc.click()
check(dlg._mc_edit.text() == DEFAULT_MOTOR_CONFIG_CSV, "恢复默认应改回 DEFAULT_MOTOR_CONFIG_CSV")
dlg._pi_edit.setText("/some/other2.csv")
dlg._reset_pi.click()
check(dlg._pi_edit.text() == DEFAULT_PARAMS_CSV, "恢复默认应改回 DEFAULT_PARAMS_CSV")

# 3. 空路径返回 None
dlg2 = CsvLoadDialog("", "")
dlg2._mc_edit.setText("")
check(dlg2.chosen_paths()["motor_config"] is None, "空 motor_config 路径应返回 None")

dlg.close()
dlg2.close()

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
