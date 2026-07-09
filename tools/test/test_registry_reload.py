"""ProtocolRegistry CSV 重新加载测试: 验证 reload_* 替换参数集合、跟踪路径、失败保留旧数据。"""
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

from jmproto.registry import (
    ProtocolRegistry, DEFAULT_MOTOR_CONFIG_CSV, DEFAULT_PARAMS_CSV,
)

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

# 0. 默认加载
check(len(reg.motor_config_params) > 0, f"默认应加载电机配置参数, 实际 {len(reg.motor_config_params)}")
check(len(reg.params) > 0, f"默认应加载运行时参数, 实际 {len(reg.params)}")
check(reg.motor_config_csv_path == DEFAULT_MOTOR_CONFIG_CSV, "默认 motor_config 路径应等于常量")
check(reg.params_csv_path == DEFAULT_PARAMS_CSV, "默认 params 路径应等于常量")

# 1. reload_motor_config 合法 CSV: 替换集合 + 更新路径
tmp = tempfile.NamedTemporaryFile(
    'w', suffix='.csv', delete=False, encoding='utf-8-sig', newline='')
tmp.write("VariableName,NameZh,Category,Access,DataType,DefaultValue,Min,Max,Unit,BlockOffset,Index,Remarks\n")
tmp.write("test_param,测试参数,TestGroup,RW,float,1.0,0,10,V,0x0040,200,测试用\n")
tmp.close()
try:
    ok = reg.reload_motor_config(tmp.name)
    check(ok, "reload_motor_config 合法 CSV 应返回 True")
    check(200 in reg.motor_config_params, "应包含新 param_id=200")
    check(len(reg.motor_config_params) == 1,
          f"应只剩 1 个参数, 实际 {len(reg.motor_config_params)}")
    check(reg.motor_config_csv_path == tmp.name, "路径应更新为临时文件")
    spec = reg.motor_config_params[200]
    check(spec.code_name == "test_param", "code_name 应正确解析")
    check(spec.group == "TestGroup", "group 应正确解析")
    check(spec.vmin == 0.0 and spec.vmax == 10.0, "Min/Max 应正确解析")
finally:
    os.unlink(tmp.name)

# 2. reload 失败: 不存在的路径, 返回 False 且保留上一次成功数据
ok = reg.reload_motor_config("nonexistent/path/x.csv")
check(not ok, "不存在路径应返回 False")
check(200 in reg.motor_config_params, "失败应保留上一次成功数据(param_id=200)")
check(reg.motor_config_csv_path != "nonexistent/path/x.csv",
      "失败不应把路径改成坏路径")

# 3. reload_params 合法 CSV
tmp2 = tempfile.NamedTemporaryFile(
    'w', suffix='.csv', delete=False, encoding='utf-8-sig', newline='')
tmp2.write("param_id,参数名(代码字段),中文含义,数据类型,字节数,单位,所属分组,读写\n")
tmp2.write("500,foo,测试,u8,1,-,测试组,RW\n")
tmp2.close()
try:
    ok = reg.reload_params(tmp2.name)
    check(ok, "reload_params 合法 CSV 应返回 True")
    check(500 in reg.params, "应包含 param_id=500")
    check(len(reg.params) == 1, f"应只剩 1 个, 实际 {len(reg.params)}")
    check(reg.params_csv_path == tmp2.name, "params 路径应更新")
finally:
    os.unlink(tmp2.name)

# 4. reload_params 失败保留旧数据
ok = reg.reload_params("nope.csv")
check(not ok and 500 in reg.params, "params reload 失败应保留旧数据")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
