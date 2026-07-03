"""参数面板写按钮高亮逻辑回归测试。

验证需求:
1. 上电默认 "--", 写按钮不高亮
2. 读应答后修改值列保持 "--", 写按钮不高亮 (不跟随同步为读回值)
3. 未读取时用户改值, 写按钮不高亮 (无比较基准)
4. 读取后用户改值且 != 读回值, 写按钮高亮
5. float 归一化: 0.1 vs 0.10 不高亮; 0.5 vs 0.6 高亮
6. ACK 后写按钮归位不高亮 (修改值列保持用户输入)
7. NACK 后写按钮仍高亮
8. 全写只收集 dirty 行, 不收集 "--" 和未 dirty 行
9. get_opts 不返回 edit_values (不持久化修改值)
"""

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
from ui.panels.param_panel import ParamPanel, COL_EDIT, COL_CURRENT

app = QApplication.instance() or QApplication(sys.argv)
reg = get_registry()
panel = ParamPanel(reg, title="电机参数", source="motor_param", show_save=False)

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


def find_pid_by_dtype(need_float=True, writable=True):
    """找一个 float 或 int 类型的可写参数。"""
    for pid, spec in panel._param_specs.items():
        if not spec.writable:
            continue
        from jmproto.codec import is_float_type
        if need_float and is_float_type(spec.dtype):
            return pid, spec
        if not need_float and not is_float_type(spec.dtype) and not spec.dtype.startswith('char['):
            return pid, spec
    return None, None


def is_dirty_style(btn):
    # 需求4: _btn_dirty_style 改为方法(随主题切换), 比较时调用取值
    return btn.styleSheet() == panel._btn_dirty_style()


# --- 找一个可写的 float 参数和 int 参数
fpid, fspec = find_pid_by_dtype(need_float=True, writable=True)
ipid, ispec = find_pid_by_dtype(need_float=False, writable=True)
print(f"测试参数: float pid={fpid}({fspec.code_name if fspec else None}, dtype={fspec.dtype if fspec else None}), "
      f"int pid={ipid}({ispec.code_name if ispec else None}, dtype={ispec.dtype if ispec else None})")


# --- 1. 初始默认 "--", 写按钮不高亮
print("\n[TEST 1] 初始默认 '--', 写按钮不高亮")
if fpid is not None:
    edit_text = panel._edit_items[fpid].text().strip()
    current_text = panel._current_items[fpid].text().strip()
    btn = panel._write_buttons[fpid]
    check(edit_text == "--", f"初始修改值列应为 '--' got={edit_text!r}")
    check(current_text == "--", f"初始当前值列应为 '--' got={current_text!r}")
    check(not is_dirty_style(btn), "初始写按钮不应高亮")
    check(not panel._row_state[fpid]['dirty'], "初始 state.dirty 应为 False")


# --- 2. 读应答后修改值列保持 "--", 写按钮不高亮
print("\n[TEST 2] 读应答后修改值列保持 '--', 写按钮不高亮")
if fpid is not None:
    panel.set_value(fpid, "0.5")
    edit_text = panel._edit_items[fpid].text().strip()
    current_text = panel._current_items[fpid].text().strip()
    btn = panel._write_buttons[fpid]
    check(current_text == "0.5", f"当前值列应为读回值 '0.5' got={current_text!r}")
    check(edit_text == "--", f"修改值列应保持 '--' (不跟随同步) got={edit_text!r}")
    check(not is_dirty_style(btn), "读应答后写按钮不应高亮 (修改值未改)")


# --- 3. 未读取时用户改值, 写按钮不高亮 (用 int 参数, 未读取过)
print("\n[TEST 3] 未读取时用户改值, 写按钮不高亮 (无比较基准)")
if ipid is not None:
    edit_item = panel._edit_items[ipid]
    btn = panel._write_buttons[ipid]
    panel._syncing_table = True
    edit_item.setText("123")
    panel._syncing_table = False
    panel._on_item_changed(edit_item)
    check(not is_dirty_style(btn), "未读取时改值不应高亮 (无比较基准)")
    check(not panel._row_state[ipid]['dirty'], "未读取时 state.dirty 应为 False")


# --- 4. 读取后用户改值且 != 读回值, 写按钮高亮
print("\n[TEST 4] 读取后用户改值且 != 读回值, 写按钮高亮")
if fpid is not None:
    # 当前 fpid 已读取过 (值=0.5)
    edit_item = panel._edit_items[fpid]
    btn = panel._write_buttons[fpid]
    panel._syncing_table = True
    edit_item.setText("0.6")
    panel._syncing_table = False
    panel._on_item_changed(edit_item)
    check(is_dirty_style(btn), "改值 0.6 != 读回值 0.5 应高亮")
    check(panel._row_state[fpid]['dirty'], "state.dirty 应为 True")


# --- 5. float 归一化: 0.1 vs 0.10 不高亮; 0.5 vs 0.6 高亮 (已在4验证)
print("\n[TEST 5] float 归一化: 0.10 与读回值 0.5 不等 -> 高亮")
if fpid is not None:
    edit_item = panel._edit_items[fpid]
    btn = panel._write_buttons[fpid]
    panel._syncing_table = True
    edit_item.setText("0.10")  # 数值 0.1 != 读回值 0.5
    panel._syncing_table = False
    panel._on_item_changed(edit_item)
    check(is_dirty_style(btn), "0.10 != 0.5 应高亮")
    # 现在把读回值也改为 0.1, 验证 0.10 vs 0.1 不高亮
    panel.set_value(fpid, "0.1")
    check(not is_dirty_style(btn), "修改值 0.10 与读回值 0.1 归一化相等, 不应高亮")
    check(not panel._row_state[fpid]['dirty'], "归一化相等时 state.dirty 应为 False")


# --- 6. ACK 后写按钮归位不高亮
print("\n[TEST 6] ACK 后写按钮归位不高亮")
if fpid is not None:
    # 先改成与读回值不一致触发高亮
    edit_item = panel._edit_items[fpid]
    btn = panel._write_buttons[fpid]
    panel._syncing_table = True
    edit_item.setText("0.7")
    panel._syncing_table = False
    panel._on_item_changed(edit_item)
    check(is_dirty_style(btn), "改值 0.7 != 读回值 0.1 应高亮")
    # 模拟写发送 + ACK
    panel.note_write_sent(fpid)
    check(is_dirty_style(btn), "写发送后仍应高亮 (修改值未变)")
    panel.confirm_pending_write()
    check(not is_dirty_style(btn), "ACK 后写按钮应归位不高亮")
    # 修改值列应保持用户输入 (不重置为 "--")
    check(edit_item.text().strip() == "0.7", f"ACK 后修改值列应保持用户输入 got={edit_item.text()!r}")


# --- 7. NACK 后写按钮仍高亮
print("\n[TEST 7] NACK 后写按钮仍高亮")
if fpid is not None:
    edit_item = panel._edit_items[fpid]
    btn = panel._write_buttons[fpid]
    # 读回值已经是 0.7 (ACK 后 synced 更新), 再改值触发高亮
    panel._syncing_table = True
    edit_item.setText("0.9")
    panel._syncing_table = False
    panel._on_item_changed(edit_item)
    check(is_dirty_style(btn), "改值 0.9 != 读回值 0.7 应高亮")
    panel.note_write_sent(fpid)
    panel.reject_pending_write()
    check(is_dirty_style(btn), "NACK 后写按钮应仍高亮 (修改值未变)")


# --- 8. 全写只收集 dirty 行
print("\n[TEST 8] 全写只收集 dirty 行")
if fpid is not None and ipid is not None:
    # 收集所有参数 id (按分组)
    all_ids = []
    for grp, ids in panel._group_param_ids.items():
        all_ids.extend(ids)
    # 捕获 write_params 信号
    captured = []
    panel.write_params.connect(lambda lst: captured.append(list(lst)))
    panel._on_write_group_clicked(tuple(all_ids))
    # fpid 当前 dirty=True (0.9 vs 0.7), ipid 不 dirty (未读取)
    if captured:
        sent_pids = [pid for pid, _ in captured[0]]
        check(fpid in sent_pids, f"dirty 的 float 参数 {fpid} 应被全写收集")
        check(ipid not in sent_pids, f"未读取的 int 参数 {ipid} 不应被全写收集 (无比较基准)")
    else:
        check(False, "全写未发出 write_params 信号")
    panel.write_params.disconnect()


# --- 9. get_opts 不返回 edit_values
print("\n[TEST 9] get_opts 不返回 edit_values (不持久化修改值)")
opts = panel.get_opts()
check("edit_values" not in opts, "get_opts 不应包含 edit_values 字段")
check("column_widths" in opts, "get_opts 应包含 column_widths 字段")


# --- 10. set_opts 容错: 传入含 edit_values 的旧配置不会还原修改值
print("\n[TEST 10] set_opts 容错: 忽略旧版 edit_values 字段")
if ipid is not None:
    # 先把 ipid 修改值清成 "--"
    panel._syncing_table = True
    panel._edit_items[ipid].setText("--")
    panel._syncing_table = False
    # 传入旧版配置 (含 edit_values)
    legacy_opts = {
        "edit_values": {str(ipid): "999", str(fpid): "888"},
        "column_widths": {},
    }
    panel.set_opts(legacy_opts)
    check(panel._edit_items[ipid].text().strip() == "--",
          f"set_opts 应忽略旧版 edit_values, 修改值保持 '--' got={panel._edit_items[ipid].text()!r}")


print(f"\n{'='*60}")
print(f"PASS={PASS}  FAIL={FAIL}")
if _FAILS:
    print("失败项:")
    for m in _FAILS:
        print(f"  - {m}")
sys.exit(0 if FAIL == 0 else 1)
