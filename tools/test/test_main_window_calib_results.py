"""Task 4: main_window 标定结果面板连接测试 (不实例化 MainWindow, 避免阻塞)。

验证:
  1. main_window 模块能 import (语法正确)
  2. _connect_signals 中存在 _calib_results_panel 的连接代码
  3. _build_ui 中创建了 _calib_results_panel (groups=MotorCalibParam, show_save=True)
  4. 端到端: 手动模拟 main_window 的连接逻辑, 验证 calib_results_panel 信号能驱动
     一个 mock 队列 (复刻 _on_param_read 的行为)
"""

import os
import sys
from collections import deque

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
import ui.main_window as mw_mod

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


# 1. main_window 模块能 import
check(hasattr(mw_mod, "MainWindow"), "main_window 模块应有 MainWindow 类")

# 2. 源码包含 _calib_results_panel 的创建与连接
src_path = mw_mod.__file__
with open(src_path, 'r', encoding='utf-8') as f:
    src = f.read()
check("_calib_results_panel = ParamPanel(" in src,
      "源码应创建 _calib_results_panel")
check('groups=("MotorCalibParam",)' in src,
      "标定结果面板应过滤 MotorCalibParam 段")
check("save_text=\"保存标定结果到Flash/EEPROM\"" in src,
      "保存按钮文本应为 '保存标定结果到Flash/EEPROM'")
check("attach_results_panel(self._calib_results_panel)" in src,
      "应调用 attach_results_panel 注入标定 Tab")
check("self._calib_results_panel.read_param.connect(" in src,
      "应连接 read_param 信号")
check("self._calib_results_panel.save_all.connect(self._on_config_save)" in src,
      "应连接 save_all 到 _on_config_save (0xEA)")

# Task 5: Index 16 (is_calibrated) 读回值应转发到 calib_panel.set_calibrated_value
check("if param_id == 16:" in src,
      "_on_param_result 应有 param_id == 16 分支")
check("self._calib_panel.set_calibrated_value(disp)" in src,
      "Index 16 读回值应转发到 calib_panel.set_calibrated_value")

# 3. 端到端: 模拟 main_window 的连接逻辑, 验证信号驱动队列
reg = get_registry()
calib = CalibrationPanel()
results = ParamPanel(
    reg, title="标定结果", source="motor_config",
    groups=("MotorCalibParam",), show_save=True,
    save_text="保存标定结果到Flash/EEPROM")
calib.attach_results_panel(results)

# 模拟 main_window._on_param_read 的最小行为
queue = deque()


def mock_on_param_read(panel, param_id):
    queue.append((panel, int(param_id)))


results.read_param.connect(lambda pid: mock_on_param_read(results, pid))

# emit read_param(16) 应入队
results.read_param.emit(16)
check(len(queue) == 1, f"read_param emit 应入队 1 项, 实际 {len(queue)}")
if queue:
    check(queue[-1] == (results, 16),
          f"队列末项应为 (results, 16), 实际 {queue[-1]}")

# 4. save_all 信号应可触发(连接到 mock)
saved = []
results.save_all.connect(lambda: saved.append(True))
results.save_all.emit()
check(saved == [True], "save_all emit 应触发回调")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
