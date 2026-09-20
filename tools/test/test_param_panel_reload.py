"""ParamPanel reload + 描述列悬停提示 测试。

验证:
1. reload() 在 CSV 重新加载后按新 registry 重建表格。
2. 描述列/名称列挂了 tooltip, 内容含 中文名/单位/类型/范围/读写/说明。
3. reload 后 tooltip 仍正确(新参数的 tooltip 用新 spec 构建)。
"""
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, '..', '..')))

from PyQt6.QtWidgets import QApplication
from jmproto.registry import ProtocolRegistry
from ui.panels.param_panel import ParamPanel, COL_NAME, COL_DESC

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


reg = ProtocolRegistry()
panel = ParamPanel(reg, source="motor_config")
orig_count = len(panel._param_specs)
check(orig_count > 0, f"默认应填充表格, 实际 {orig_count} 个参数")

# ---- tooltip 验证(默认 motor_info 数据) ----
# phase_resistance (param_id=20): unit=ohm, dtype=float, min=0.001, max=50,
# desc 含"默认: <CSV DefaultValue>" 与 Remarks
spec20 = reg.motor_config_params[20]
tt20 = panel._build_param_tooltip(spec20)
check("相电阻" in tt20, f"tooltip 应含中文名, 实际: {tt20}")
check("ohm" in tt20, f"tooltip 应含单位, 实际: {tt20}")
check("0.001" in tt20 and "50" in tt20, f"tooltip 应含 min/max, 实际: {tt20}")
# 默认值从当前 CSV 动态取, 避免参数表更新数值后断言过期
import csv as _csv
_default20 = ""
with open(reg.motor_config_csv_path, encoding='utf-8-sig', newline='') as _f:
    for _row in _csv.DictReader(_f):
        if _row.get('VariableName') == 'phase_resistance':
            _default20 = (_row.get('DefaultValue') or '').strip()
            break
check(f"默认: {_default20}" in tt20,
      f"tooltip 应含默认值(来自Remarks拼接, CSV值={_default20!r}), 实际: {tt20}")
# 描述列与名称列都应挂上 tooltip
row20 = panel._row_of_pid[20]
desc_item = panel._table.item(row20, COL_DESC)
name_item = panel._table.item(row20, COL_NAME)
check(bool(desc_item.toolTip()), "描述列应挂 tooltip")
check(bool(name_item.toolTip()), "名称列应挂 tooltip")
check(desc_item.toolTip() == tt20, "描述列 tooltip 应等于 _build_param_tooltip 输出")

# ---- reload 验证 ----
tmp = tempfile.NamedTemporaryFile(
    'w', suffix='.csv', delete=False, encoding='utf-8-sig', newline='')
tmp.write("VariableName,NameZh,Category,Access,DataType,DefaultValue,Min,Max,Unit,BlockOffset,Index,Remarks\n")
tmp.write("only_one,唯一参数,TestGroup,RW,float,1.0,0,10,V,0x0040,300,仅一个备注\n")
tmp.close()
try:
    reg.reload_motor_config(tmp.name)
    check(300 in reg.motor_config_params, "registry 应已含 param_id=300")
    # reload 前前面板仍含旧参数
    check(len(panel._param_specs) == orig_count, "reload() 前面板应仍为旧行数")
    # 触发面板 reload
    panel.reload()
    check(len(panel._param_specs) == 1,
          f"reload() 后应只剩 1 个参数, 实际 {len(panel._param_specs)}")
    check(300 in panel._param_specs, "面板应含新 param_id=300")
    check(0 not in panel._param_specs, "面板不应再含旧 param_id=0")
    # reload 后新参数的 tooltip 用新 spec 构建
    row300 = panel._row_of_pid[300]
    tt300 = panel._table.item(row300, COL_DESC).toolTip()
    check("唯一参数" in tt300, f"reload 后 tooltip 应含新中文名, 实际: {tt300}")
    check("仅一个备注" in tt300, f"reload 后 tooltip 应含新 Remarks, 实际: {tt300}")
    check("0" in tt300 and "10" in tt300, f"reload 后 tooltip 应含新 min/max, 实际: {tt300}")
finally:
    os.unlink(tmp.name)

# reload 不存在的 param_id 不残留 (状态清空)
check(len(panel._pending_writes) == 0, "reload 应清空待写队列")
check(len(panel._fresh_ids) == 0, "reload 应清空 fresh 集合")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
