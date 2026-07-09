"""CSV 路径持久化往返测试: 验证 _collect_settings/_apply_settings 透传 csv_paths。"""
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
from ui.main_window import MainWindow

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


# 准备一个合法的最小 motor_info CSV
tmp = tempfile.NamedTemporaryFile(
    'w', suffix='.csv', delete=False, encoding='utf-8-sig', newline='')
tmp.write("VariableName,NameZh,Category,Access,DataType,DefaultValue,Min,Max,Unit,BlockOffset,Index,Remarks\n")
tmp.write("p1,参数一,TestG,RW,float,1,0,10,V,0x0040,400,x\n")
tmp.close()

try:
    mw = MainWindow()
    # 1. 默认 collect 应含 csv_paths 且为打包默认
    s = mw._collect_settings()
    check("csv_paths" in s, "collect 应含 csv_paths 键")
    check(s["csv_paths"]["motor_config"] == mw._registry.motor_config_csv_path,
          "collect 的 motor_config 路径应与 registry 一致")

    # 2. apply 一个自定义路径, 应触发 reload + 面板重建
    saved = {"csv_paths": {"motor_config": tmp.name, "motor_param": None}}
    mw._apply_settings(saved)
    check(mw._registry.motor_config_csv_path == tmp.name,
          "apply 后 registry 路径应为自定义路径")
    check(400 in mw._registry.motor_config_params, "registry 应含 param_id=400")
    check(400 in mw._config_panel._param_specs, "config 面板应已重建含 400")

    # 3. 再次 collect 应透传新路径
    s2 = mw._collect_settings()
    check(s2["csv_paths"]["motor_config"] == tmp.name, "二次 collect 应透传新路径")

    mw.close()
finally:
    os.unlink(tmp.name)

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
