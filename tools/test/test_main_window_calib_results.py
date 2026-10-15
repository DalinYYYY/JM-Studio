"""Task 4: main_window 标定结果面板连接测试 (不实例化 MainWindow, 避免阻塞)。

验证:
  1. main_window 模块能 import (语法正确)
  2. _connect_signals 中存在 _calib_results_panel 的连接代码
  3. _build_ui 中创建了 _calib_results_panel (row_plan=CALIB_TABLE_PLAN, show_save=True)
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
from ui.panels.calibration_panel import CalibrationPanel, CALIB_TABLE_PLAN
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
check("row_plan=CALIB_TABLE_PLAN" in src,
      "标定结果面板应按 CALIB_TABLE_PLAN 行计划展示(标定流程排序+标定项列)")
check("save_text=\"固化参数\"" in src,
      "保存按钮文本应为 '固化参数'")
check("attach_results_panel(self._calib_results_panel)" in src,
      "应调用 attach_results_panel 注入标定 Tab")
check("self._calib_results_panel.read_param.connect(" in src,
      "应连接 read_param 信号")
check("self._calib_results_panel.save_all.connect(self._on_config_save)" in src,
      "应连接 save_all 到 _on_config_save (0xEA)")

# Task 5: Index 16 (is_calibrated) 读回值应转发到 calib_panel.set_calibrated_value
check('if panel.source() == "motor_config" and param_id == 16:' in src,
      "标定状态应只接受配置通道 ID16 应答")
check("self._calib_panel.set_calibrated_value(disp)" in src,
      "Index 16 读回值应转发到 calib_panel.set_calibrated_value")

# 3. 端到端: 模拟 main_window 的连接逻辑, 验证信号驱动队列
reg = get_registry()
calib = CalibrationPanel()
results = ParamPanel(
    reg, title="标定结果", source="motor_config",
    row_plan=CALIB_TABLE_PLAN, show_save=True,
    save_text="固化参数")
calib.attach_results_panel(results)

# 行计划行为: 行数与计划一致, 首列为"标定项", 按计划顺序排列
_tbl = results._table
check(_tbl.columnCount() == 10,
      f"行计划模式应为 10 列(标定项+基础9列), 实际 {_tbl.columnCount()}")
check(_tbl.rowCount() == len(CALIB_TABLE_PLAN),
      f"行数应等于计划行数 {len(CALIB_TABLE_PLAN)}, 实际 {_tbl.rowCount()}")
check(_tbl.horizontalHeaderItem(0).text() == "标定项",
      "首列列头应为 '标定项'")
plan_ids = [pid for pid, _tag in CALIB_TABLE_PLAN]
check(list(results._param_specs.keys()) == plan_ids,
      "参数行顺序应严格按 CALIB_TABLE_PLAN (不按 ID 排序)")
first_row = _tbl.item(0, 0)
check(first_row is not None and first_row.text() == "L1>3 电流传感器",
      f"首行标定项应为 'L1>3 电流传感器', 实际 {first_row.text() if first_row else None}")

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

# 4. save_all 信号应可触发(连接到 mock), 载荷为 flags(u8)
saved = []
results.save_all.connect(lambda flags: saved.append(flags))
results.save_all.emit(0)   # 默认仅写EEPROM
results.save_all.emit(1)   # bit0=1 追加写Flash备份
check(saved == [0, 1], f"save_all emit 应携带 flags, 实际 {saved}")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
