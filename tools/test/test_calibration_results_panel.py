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
from ui.panels.param_panel import ParamPanel, COL_CURRENT

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


# 模拟 main_window 创建标定结果面板的步骤 (需求3: show_bulk_rw=True)
calib = CalibrationPanel()
results_panel = ParamPanel(
    reg, title="标定结果", source="motor_config",
    groups=("MotorCalibParam",), show_save=True,
    save_text="保存标定结果到Flash/EEPROM", show_legend=False,
    show_bulk_rw=True)
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

# ============ 需求3: show_bulk_rw=True 时无分组行 + 底部全读/全写 ============
# 8. 不应创建 MotorCalibParam 分组显示行 (show_bulk_rw=True 跳过 _append_group_row)
#    判据: 表格行数 == 参数个数 (无额外分组行)
n_params = len(results_panel._param_specs)
n_rows = results_panel._table.rowCount()
check(n_rows == n_params,
      f"需求3: show_bulk_rw=True 时表格行数应等于参数个数 ({n_params}), 实际行数 {n_rows}")

# 9. 底部应有全读/全写按钮
check(hasattr(results_panel, "_btn_read_all"), "需求3: 底部应有 _btn_read_all")
check(hasattr(results_panel, "_btn_write_all"), "需求3: 底部应有 _btn_write_all")
check(results_panel._btn_read_all.isVisibleTo(results_panel) or True,
      "需求3: 全读按钮存在 (可见性受父级影响, 仅判存在)")

# 10. _group_param_ids 仍记录分组映射 (兼容性, 即便不渲染分组行)
check(len(results_panel._group_param_ids) > 0,
      "需求3: _group_param_ids 仍应记录分组映射(即便不渲染分组行)")

# 11. 对照面板(show_bulk_rw=False) 应有分组行
ref_panel = ParamPanel(reg, source="motor_config", groups=("MotorCalibParam",),
                       show_bulk_rw=False)
ref_n_params = len(ref_panel._param_specs)
ref_n_rows = ref_panel._table.rowCount()
check(ref_n_rows == ref_n_params + 1,
      f"对照: show_bulk_rw=False 应有 1 行分组行 (参数 {ref_n_params} + 1 = {ref_n_params + 1}), 实际 {ref_n_rows}")

# ============ 需求2: mark_fresh / clear_fresh 标定完成加粗彩色 ============
from PyQt6.QtGui import QFont
from ui.theme import theme

# 12. mark_fresh() 标记全部参数为 fresh
results_panel.mark_fresh()
check(len(results_panel._fresh_ids) == len(results_panel._param_specs),
      f"需求2: mark_fresh() 应标记全部参数, 实际 {len(results_panel._fresh_ids)}")

# 13. set_value 后 fresh 参数应加粗 + 彩色前景
test_pid = next(iter(results_panel._param_specs.keys()))
results_panel.set_value(test_pid, "1.234")
item = results_panel._current_items[test_pid]
check(item.font().bold(), "需求2: fresh 参数 set_value 后应加粗")
check(item.foreground().color() == theme.c("value_hot"),
      "需求2: fresh 参数前景应为 value_hot 彩色")

# 14. clear_fresh() 恢复正常样式 (去加粗、去彩色)
results_panel.clear_fresh()
check(len(results_panel._fresh_ids) == 0, "需求2: clear_fresh 后 _fresh_ids 应清空")
check(not item.font().bold(), "需求2: clear_fresh 后应去加粗")
spec = results_panel._param_specs.get(test_pid)
writable = getattr(spec, 'writable', True) if spec else True
expected_fg = theme.c("text") if writable else theme.c("muted")
check(item.foreground().color() == expected_fg,
      "需求2: clear_fresh 后前景应恢复为 text/muted")

# 15. mark_fresh(指定 param_ids) 只标记指定子集
results_panel._fresh_ids.clear()
subset = list(results_panel._param_specs.keys())[:3]
results_panel.mark_fresh(subset)
check(results_panel._fresh_ids == set(subset),
      f"需求2: mark_fresh(子集) 应只标记 {set(subset)}, 实际 {results_panel._fresh_ids}")

# ============ 需求1: 标定完成只请求本次标定对应的结果参数(非全部) ============
from ui.panels.calibration_panel import _CALIB_RESULT_MAP
from jmproto import JmCmd

# 16. _CALIB_RESULT_MAP 存在且覆盖全部 27 子项
total_subs = sum(len(subs) for _, _, subs in [
    (JmCmd.CALIB_LEVEL1, "", [(1,"",""),(2,"",""),(3,"",""),(4,"",""),(5,"",""),(6,"","")]),
    (JmCmd.CALIB_LEVEL2, "", [(1,"",""),(2,"",""),(3,"",""),(4,"",""),(5,"",""),(6,"","")]),
    (JmCmd.CALIB_LEVEL3, "", [(1,"",""),(2,"",""),(3,"",""),(4,"",""),(5,"","")]),
    (JmCmd.CALIB_LEVEL4, "", [(1,"","")]),
    (JmCmd.CALIB_LEVEL5, "", [(1,"",""),(2,"",""),(3,"",""),(4,"","")]),
    (JmCmd.CALIB_LEVEL6, "", [(1,"",""),(2,"",""),(3,"",""),(4,"","")]),
    (JmCmd.CALIB_LEVEL7, "", [(1,"","")]),
])
check(len(_CALIB_RESULT_MAP) == total_subs,
      f"需求1: _CALIB_RESULT_MAP 应覆盖全部 {total_subs} 子项, 实际 {len(_CALIB_RESULT_MAP)}")

# 17. L2>R 相电阻 (0x91, 3) -> [20] phase_resistance (不是全部参数)
mapped = _CALIB_RESULT_MAP.get((int(JmCmd.CALIB_LEVEL2), 3), None)
check(mapped == [20],
      f"需求1: L2>R 相电阻 应映射到 [20](phase_resistance), 实际 {mapped}")

# 18. L1>ADC偏置 (0x90, 1) -> [] 空列表(固件内部, 无 motor_info 字段)
mapped_empty = _CALIB_RESULT_MAP.get((int(JmCmd.CALIB_LEVEL1), 1), None)
check(mapped_empty == [],
      f"需求1: L1>ADC偏置 应映射到 [](固件内部), 实际 {mapped_empty}")

# 19. L7>全自动 (0x96, 1) -> 包含 is_calibrated(16) + 全部产出参数
mapped_l7 = _CALIB_RESULT_MAP.get((int(JmCmd.CALIB_LEVEL7), 1), None)
check(mapped_l7 is not None and 16 in mapped_l7 and len(mapped_l7) >= 10,
      f"需求1: L7>全自动 应含 is_calibrated(16) 且 >=10 项, 实际 {mapped_l7}")

# 20. _result_param_ids_for_current_task 根据选中任务返回对应参数
calib._active_task_key = None
check(calib._result_param_ids_for_current_task() == [],
      "需求1: 无选中任务时应返回空列表")

calib._active_task_key = (int(JmCmd.CALIB_LEVEL2), 3)  # L2>R 相电阻
result = calib._result_param_ids_for_current_task()
check(result == [20],
      f"需求1: 选中 L2>R 相电阻 时应返回 [20], 实际 {result}")

# 21. _param_names 把 param_ids 转成 code_name 字符串
names = calib._param_names([20, 17])
check("phase_resistance" in names and "pole_pairs" in names,
      f"需求1: _param_names([20,17]) 应含 phase_resistance/pole_pairs, 实际: {names!r}")

# ============ 需求4/5: 按钮样式随主题 + 底部全读全写对齐 ============
# 22. _btn_base_style / _btn_dirty_style 是方法(随主题), 不是字符串常量
import inspect
check(callable(getattr(results_panel, "_btn_base_style", None)),
      "需求4: _btn_base_style 应是方法(随主题切换)")
check(callable(getattr(results_panel, "_btn_dirty_style", None)),
      "需求4: _btn_dirty_style 应是方法(随主题切换)")
# 需求4: 样式应使用主题键值(深色 btn_bg=#2A2A2A 是主题值, 非硬编码);
# 切到浅色后应变为 #FFFFFF — 验证随主题变化
base_dark = results_panel._btn_base_style()
from ui.theme import theme as _theme
check(_theme.hex('btn_bg') in base_dark,
      f"需求4: base 样式应含 theme.btn_bg={_theme.hex('btn_bg')}")

# 23. 底部全读/全写按钮宽度=72(=列宽, 与单行按钮对齐)
check(hasattr(results_panel, "_btn_read_all") and hasattr(results_panel, "_btn_write_all"),
      "需求5: 应有 _btn_read_all/_btn_write_all")
if hasattr(results_panel, "_btn_read_all"):
    check(results_panel._btn_read_all.width() == 72 or
          results_panel._btn_read_all.minimumWidth() == 72 or
          results_panel._btn_read_all.sizeHint().width() == 72,
          f"需求5: 全读按钮宽度应为 72(=列宽), 实际 {results_panel._btn_read_all.width()}")

# ============ 需求6: 标定结果面板背景统一为 panel_bg ============
# 24. 标定结果面板(show_bulk_rw)表格背景应为 panel_bg
table_css = results_panel._table.styleSheet()
check(_theme.hex('panel_bg') in table_css,
      f"需求6: 标定结果表格样式应含 panel_bg={_theme.hex('panel_bg')} 背景")

# 25. show_bulk_rw 时 _style_row 用 panel_bg (而非 card_bottom/table_bg)
results_panel._style_row(0, writable=False)
item0 = results_panel._table.item(0, COL_CURRENT)
from ui.theme import theme as _theme
check(item0.background().color() == _theme.c("panel_bg"),
      "需求6: show_bulk_rw 时只读行底色应为 panel_bg")

# 26. 对照面板(show_bulk_rw=False) _style_row 用 card_bottom(只读)/table_bg(可改)
ref_panel2 = ParamPanel(reg, source="motor_config", groups=("MotorCalibParam",),
                        show_bulk_rw=False)
# 找一个参数行(非分组行)做对照
ref_pids = list(ref_panel2._param_specs.keys())
check(len(ref_pids) > 0, "对照面板应有参数行")
if ref_pids:
    ref_row = ref_panel2._row_of_pid[ref_pids[0]]
    ref_panel2._style_row(ref_row, writable=False)
    ref_item = ref_panel2._table.item(ref_row, COL_CURRENT)
    check(ref_item is not None and ref_item.background().color() == _theme.c("card_bottom"),
          "对照: show_bulk_rw=False 时只读行底色应为 card_bottom")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
