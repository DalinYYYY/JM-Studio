# CSV 驱动参数表动态加载 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户在运行时加载新的 motor_info.csv / joint_motor_param_index.csv，自动重建电机配置 / PID参数 / 标定结果 / 运行时参数表格（数据与分组格式均来自CSV），并把所选CSV路径持久化到 ui_layout.json。同时鼠标悬停描述/名称列时淡淡显示参数详情（min/max/单位/Remarks等）。

**Architecture:** 项目已是 CSV 驱动：`ProtocolRegistry` 启动时加载 `resources/motor_info.csv`（含电机配置 + PID 的 ControlParam 段）和 `joint_motor_param_index.csv`（运行时参数），`ParamPanel` 按 registry 分组动态生成表格。当前缺口是「运行时加载新CSV + 全局刷新」。本计划在 registry 增加 `reload_motor_config/reload_params` 方法（解析逻辑抽公共helper，DRY），给 `ParamPanel` 加 public `reload()`，新增 `CsvLoadDialog` 选择路径，主窗口编排「reload registry → 刷新所有依赖面板 → 持久化路径」，并在启动时按持久化路径恢复。

**Tech Stack:** PyQt6, Python 3.10+, 项目已有 `theme` 单例、`layout_store` 配置持久化、`ParamPanel` 可复用组件。

---

## 现状关键事实（实现者必读）

1. **CSV 已定义数据 + 格式**：[resources/motor_info.csv](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\resources\motor_info.csv) 列 schema 固定（VariableName/NameZh/Category/Access/DataType/DefaultValue/Min/Max/Unit/BlockOffset/Index/Remarks），按 Category 分组。`ControlParam` 段即 PID 参数，`MotorCalibParam` 段即标定结果。
2. **registry 单例**：[jmproto/registry.py:146](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\jmproto\registry.py#L146) `ProtocolRegistry` 启动加载一次，CSV 路径硬编码在模块常量 `_MOTOR_INFO_CSV` / `_PARAM_CSV`。`JmClient.registry` 持有同一单例。
3. **ParamPanel 完全数据驱动**：[ui/panels/param_panel.py:337](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\param_panel.py#L337) `_populate()` 按 registry 分组重建表格。已有4个实例：
   - `_param_panel`（source="motor_param"，运行时参数 0xE0-0xE5）
   - `_config_panel`（source="motor_config"，全部分组 0xE6-0xEB）
   - `_calib_results_panel`（source="motor_config"，groups=("MotorCalibParam",)，注入标定Tab）
   - `_pid_params_panel`（source="motor_config"，groups=("ControlParam",)，注入PID Tab）
4. **持久化机制**：[ui/layout_store.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\layout_store.py) `set_meta("settings", dict)` / `get_meta("settings", {})`。主窗口 `_collect_settings()` 收集、`_apply_settings()` 恢复，关窗时写回（[main_window.py:1219](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L1219)）。
5. **测试风格**：`tools/test/test_*.py` 为直接 python 脚本，用 `check(cond,msg)` + `sys.exit`，`QT_QPA_PLATFORM=offscreen`。

---

## 文件结构

| 文件 | 职责 | 动作 |
|------|------|------|
| `jmproto/registry.py` | 协议注册表；新增 CSV 重新加载能力 + 路径跟踪 + 公开默认路径常量 | **修改** |
| `ui/panels/param_panel.py` | 参数表格面板；新增 public `reload()` + 描述/名称列悬停 tooltip（min/max/单位/Remarks） | **修改** |
| `ui/csv_load_dialog.py` | CSV 路径选择对话框（选 motor_info / param_index + 恢复默认） | **新建** |
| `ui/main_window.py` | 加载按钮 + 对话框编排 + 全局面板刷新 + 路径持久化(收集/恢复) | **修改** |
| `tools/test/test_registry_reload.py` | registry reload 单元测试 | **新建** |
| `tools/test/test_param_panel_reload.py` | ParamPanel reload + 描述列悬停 tooltip 单元测试 | **新建** |
| `tools/test/test_csv_load_dialog.py` | 对话框路径选择逻辑测试 | **新建** |

### 设计决策

1. **解析逻辑 DRY**：把 `_load_motor_config_params` / `_load_params` 的解析体抽成 `_parse_motor_config_csv(path, out_dict) -> bool` / `_parse_params_csv(path, out_dict) -> bool`，原 `_load_*` 改为调用它们（行为不变），新增 `reload_*` 也复用同一解析器。失败时**不清空** out_dict 之外的全局状态，保留旧数据。

2. **reload 原子性**：`reload_motor_config(path)` 先解析到临时 dict，成功才替换 `self.motor_config_params` 引用（dict 引用替换在 CPython 是原子操作），失败保留旧 dict。失败追加 warning 并返回 False。

3. **面板刷新保留列宽**：`_reload_all_param_panels()` 在 reload 前用 `get_opts()` 备份各面板列宽，reload 后用 `set_opts()` 恢复（`_populate()` 重建表格会清空列宽）。

4. **启动恢复顺序**：`_apply_settings` 中 CSV 路径恢复块插在面板 opts 循环**之前**——先 reload registry + `panel.reload()`，再由后续 opts 循环恢复列宽。面板在 `__init__._build_ui()` 已用默认 CSV 建好，此处按持久化路径覆盖重建。

5. **路径有效性**：持久化的是绝对路径；启动恢复时用 `Path(mc).is_file()` 守卫，路径失效则回落默认 CSV（不报错）。符合「换机器/换目录也不崩」。

6. **并发说明**：reload 在 UI 线程触发；JmClient 在串口回调线程用 registry 打包/解包。dict 引用替换原子，且现有 pack/unpack 代码对 `spec is None` 有守卫（未知 param_id 抛 ValueError 被上层捕获）。建议用户在断开连接时换 CSV，但不硬性阻断。

---

## Task 1: Registry 重新加载能力

**Files:**
- Modify: `jmproto/registry.py`（新增公开常量 + 路径属性 + 解析helper + reload方法，重构 `_load_motor_config_params` / `_load_params`）
- Test: `tools/test/test_registry_reload.py`

- [ ] **Step 1: 编写失败测试（验证 reload_* 方法与路径跟踪）**

Create `tools/test/test_registry_reload.py`:

```python
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
check(reg.motor_config_csv_path == tmp.name or reg.motor_config_csv_path != "nonexistent/path/x.csv",
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python tools/test/test_registry_reload.py`
Expected: FAIL — `AttributeError: 'ProtocolRegistry' object has no attribute 'motor_config_csv_path'`（或 `reload_motor_config` 不存在）

- [ ] **Step 3: 在 registry.py 顶部新增公开默认路径常量**

在 `jmproto/registry.py` 中找到这两行（约第 18-19 行）：

```python
_CMD_CSV = os.path.join(_RES_DIR, 'joint_motor_command_list.csv')
_PARAM_CSV = os.path.join(_RES_DIR, 'joint_motor_param_index.csv')
_MOTOR_INFO_CSV = os.path.join(_RES_DIR, 'motor_info.csv')
```

在 `_MOTOR_INFO_CSV = ...` 这行之后、`_FIELD_RE` 之前，新增两行公开常量（保留原三行不动）：

```python
# 公开默认 CSV 路径(供 UI 的"恢复默认"使用)
DEFAULT_MOTOR_CONFIG_CSV = _MOTOR_INFO_CSV
DEFAULT_PARAMS_CSV = _PARAM_CSV
```

- [ ] **Step 4: 在 `ProtocolRegistry.__init__` 增加路径跟踪属性**

把 `jmproto/registry.py` 中 `ProtocolRegistry.__init__` 改为：

```python
    def __init__(self):
        self.commands = {}    # cmd(int) -> CommandSpec
        self.params = {}      # param_id(int) -> ParamSpec
        self.motor_config_params = {}  # param_id(int) -> ParamSpec, 来自 motor_info.csv
        self.warnings = []    # 加载告警(供日志显示)
        # 当前参数表 CSV 路径(默认打包内置; 运行时可被 reload_* 覆盖)
        self.motor_config_csv_path = _MOTOR_INFO_CSV
        self.params_csv_path = _PARAM_CSV
        self._load_commands()
        self._load_params()
        self._load_motor_config_params()
```

- [ ] **Step 5: 把 `_load_motor_config_params` 重构为调用解析helper**

替换 `jmproto/registry.py` 中整个 `_load_motor_config_params` 方法为以下两个方法（解析helper + 原加载入口）：

```python
    def _load_motor_config_params(self):
        self._parse_motor_config_csv(_MOTOR_INFO_CSV, self.motor_config_params)
        self.motor_config_csv_path = _MOTOR_INFO_CSV

    def _parse_motor_config_csv(self, csv_path: str, out: dict) -> bool:
        """解析 motor_info.csv 到 out(param_id -> ParamSpec)。
        成功: 填充 out 并返回 True; 失败: out 保持原状, 追加 warning, 返回 False。
        """
        new_params = {}
        try:
            with open(csv_path, encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                for idx, row in enumerate(reader):
                    name = (row.get('VariableName') or '').strip()
                    if not name:
                        continue
                    index_text = (row.get('Index') or '').strip()
                    try:
                        pid = int(index_text, 0) if index_text else idx
                    except ValueError:
                        pid = idx
                    dtype = _normalize_dtype(row.get('DataType') or '')
                    nbytes = codec.type_nbytes(dtype)
                    remarks = (row.get('Remarks') or '').strip()
                    default = (row.get('DefaultValue') or '').strip()
                    desc_parts = []
                    if default:
                        desc_parts.append(f"默认: {default}")
                    if remarks:
                        desc_parts.append(remarks)
                    vmin = _parse_num(row.get('Min'))
                    vmax = _parse_num(row.get('Max'))
                    new_params[pid] = ParamSpec(
                        param_id=pid,
                        code_name=name,
                        cn_name=(row.get('NameZh') or '').strip(),
                        dtype=dtype,
                        nbytes=nbytes,
                        unit=(row.get('Unit') or '').strip(),
                        group=(row.get('Category') or '其他').strip(),
                        rw=(row.get('Access') or 'RW').strip(),
                        desc='; '.join(desc_parts),
                        vmin=vmin,
                        vmax=vmax,
                    )
        except FileNotFoundError:
            self.warnings.append(f"电机配置表 CSV 未找到, 电机配置面板将为空: {csv_path}")
            return False
        except Exception as e:
            self.warnings.append(f"电机配置表解析失败({e}): {csv_path}")
            return False
        out.clear()
        out.update(new_params)
        return True

    def reload_motor_config(self, csv_path: str) -> bool:
        """运行时重新加载电机配置表。
        成功: 替换 motor_config_params, 更新 motor_config_csv_path, 返回 True。
        失败: 保留旧数据, 追加 warning, 返回 False。
        """
        new_params = {}
        if self._parse_motor_config_csv(csv_path, new_params):
            self.motor_config_params = new_params
            self.motor_config_csv_path = csv_path
            return True
        self.warnings.append(f"重新加载电机配置表失败, 保留旧数据: {csv_path}")
        return False
```

> 注意：`_parse_motor_config_csv` 内部先解析到 `new_params`，全部成功才 `out.clear(); out.update(new_params)`，保证失败时 out 原状不变。

- [ ] **Step 6: 同样重构 `_load_params` + 新增 `reload_params`**

替换 `jmproto/registry.py` 中整个 `_load_params` 方法为：

```python
    def _load_params(self):
        self._parse_params_csv(_PARAM_CSV, self.params)
        self.params_csv_path = _PARAM_CSV

    def _parse_params_csv(self, csv_path: str, out: dict) -> bool:
        """解析 joint_motor_param_index.csv 到 out(param_id -> ParamSpec)。"""
        new_params = {}
        try:
            with open(csv_path, encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    pid_str = (row.get('param_id') or '').strip()
                    if not pid_str.isdigit():
                        continue
                    pid = int(pid_str)
                    try:
                        nbytes = int((row.get('字节数') or '0').strip())
                    except ValueError:
                        nbytes = 0
                    new_params[pid] = ParamSpec(
                        param_id=pid,
                        code_name=(row.get('参数名(代码字段)') or '').strip(),
                        cn_name=(row.get('中文含义') or '').strip(),
                        dtype=_normalize_dtype(row.get('数据类型') or ''),
                        nbytes=nbytes,
                        unit=(row.get('单位') or '').strip(),
                        group=(row.get('所属分组') or '其他').strip(),
                        rw=(row.get('读写') or 'RW').strip(),
                        desc=(row.get('中文含义') or '').strip(),
                    )
        except FileNotFoundError:
            self.warnings.append(f"参数表 CSV 未找到, 参数面板将为空: {csv_path}")
            return False
        except Exception as e:
            self.warnings.append(f"参数表解析失败({e}): {csv_path}")
            return False
        out.clear()
        out.update(new_params)
        return True

    def reload_params(self, csv_path: str) -> bool:
        """运行时重新加载运行时参数表。成功替换, 失败保留旧数据。"""
        new_params = {}
        if self._parse_params_csv(csv_path, new_params):
            self.params = new_params
            self.params_csv_path = csv_path
            return True
        self.warnings.append(f"重新加载参数表失败, 保留旧数据: {csv_path}")
        return False
```

- [ ] **Step 7: 运行测试确认通过**

Run: `python tools/test/test_registry_reload.py`
Expected: `PASS=14 FAIL=0`（或类似，全部通过）

- [ ] **Step 8: 回归 - 确认默认加载行为不变**

Run: `python tools/test/test_param_panel_group_filter.py`
Expected: 全部 PASS（原分组过滤测试仍通过，证明重构未破坏默认加载）

- [ ] **Step 9: Commit**

```bash
git add jmproto/registry.py tools/test/test_registry_reload.py
git commit -m "feat(registry): add runtime CSV reload for motor_config and params"
```

---

## Task 2: ParamPanel.reload() + 描述列悬停提示

**目标：** 两件事一起做（都改 `ParamPanel`，逻辑同属面板层）：
1. 新增 public `reload()` —— CSV 重新加载后按新 registry 重建表格。
2. 鼠标悬停**描述列**或**名称列**时，淡淡显示该参数的详细信息（中文名/单位/类型/范围[min,max]/读写/说明含默认值与Remarks）。

**Files:**
- Modify: `ui/panels/param_panel.py`（新增 `_build_param_tooltip()` helper + `reload()` 方法 + 在 `_append_param_row` 给描述/名称列挂 tooltip）
- Test: `tools/test/test_param_panel_reload.py`

> 现状：[_append_param_row](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\param_panel.py#L386) 当前只设描述列文本，没设 tooltip。`ParamSpec` 已有 `vmin/vmax/unit/desc(含默认值+Remarks)/dtype/rw` 等字段，tooltip 只需汇总这些字段。motor_info.csv 加载时 `desc` 已拼接「默认: x; Remarks」。

- [ ] **Step 1: 编写失败测试（reload + tooltip 一起验证）**

Create `tools/test/test_param_panel_reload.py`:

```python
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
# desc 含"默认: 0.1" 与 Remarks
spec20 = reg.motor_config_params[20]
tt20 = panel._build_param_tooltip(spec20)
check("相电阻" in tt20, f"tooltip 应含中文名, 实际: {tt20}")
check("ohm" in tt20, f"tooltip 应含单位, 实际: {tt20}")
check("0.001" in tt20 and "50" in tt20, f"tooltip 应含 min/max, 实际: {tt20}")
check("默认: 0.1" in tt20, f"tooltip 应含默认值(来自Remarks拼接), 实际: {tt20}")
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
    # reload 前面板仍含旧参数
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python tools/test/test_param_panel_reload.py`
Expected: FAIL — `AttributeError: 'ParamPanel' object has no attribute 'reload'`（以及 `_build_param_tooltip` 不存在）

- [ ] **Step 3: 在 ParamPanel 增加 `_build_param_tooltip()` helper**

在 `ui/panels/param_panel.py` 的 `_append_param_row` 方法**之前**（约第 386 行，`_append_group_row` 方法结束后），新增：

```python
    def _build_param_tooltip(self, p) -> str:
        """构建参数详细信息的悬停提示文本(鼠标悬停描述/名称列时淡入显示)。

        汇总 ParamSpec 全部可读字段: 中文名/单位/类型/范围[min,max]/读写/说明(含默认值与Remarks)。
        范围行仅当存在 vmin/vmax 时显示; 说明行含 motor_info.csv 的默认值与 Remarks(加载时已拼入 desc)。
        """
        lines = []
        if p.cn_name:
            lines.append(f"中文名: {p.cn_name}")
        if p.unit and p.unit != '-':
            lines.append(f"单位: {p.unit}")
        if p.dtype:
            lines.append(f"类型: {p.dtype}")
        # 范围: 仅当存在 vmin/vmax 时显示, 无约束则不显示该行
        if p.vmin is not None or p.vmax is not None:
            lo = p.vmin if p.vmin is not None else "-∞"
            hi = p.vmax if p.vmax is not None else "+∞"
            lines.append(f"范围: [{lo}, {hi}]")
        if p.rw:
            lines.append(f"读写: {p.rw}")
        if p.desc:
            lines.append(f"说明: {p.desc}")
        return "\n".join(lines)
```

- [ ] **Step 4: 在 `_append_param_row` 给描述列/名称列挂 tooltip**

在 `ui/panels/param_panel.py` 的 `_append_param_row` 方法中，找到这两行：

```python
        desc_item = self._make_cell_item(desc)

        self._table.setItem(row, COL_NAME, name_item)
```

在它们之间插入 tooltip 挂载代码，改为：

```python
        desc_item = self._make_cell_item(desc)

        # 鼠标悬停描述列/名称列时淡入显示参数详细信息(含 min/max/单位/Remarks)
        tooltip = self._build_param_tooltip(p)
        if tooltip:
            desc_item.setToolTip(tooltip)
            name_item.setToolTip(tooltip)

        self._table.setItem(row, COL_NAME, name_item)
```

- [ ] **Step 5: 在 ParamPanel 增加 `reload()` 方法**

在 `ui/panels/param_panel.py` 的 `read_all` 方法之后（约第 124 行 `read_all` 方法结束后），新增：

```python
    def reload(self):
        """从 registry 重新构建表格(用于运行时 CSV 重新加载后刷新)。

        清空待写队列与 fresh 标记(它们可能指向已失效的 param_id),
        然后调用 _populate() 按 registry 当前内容重建表格。
        _populate → _append_param_row 会重新构建 tooltip, 故 reload 后 tooltip 自动更新。
        """
        self._pending_writes.clear()
        self._fresh_ids.clear()
        self._populate()
```

- [ ] **Step 6: 运行测试确认通过**

Run: `python tools/test/test_param_panel_reload.py`
Expected: 全部 PASS（reload + tooltip 两部分均通过）

- [ ] **Step 7: 回归 - 确认既有面板结构未破坏**

Run: `python tools/test/test_param_panel_group_filter.py`
Expected: 全部 PASS（原分组过滤测试仍通过，证明新增 tooltip 未影响表格结构）

- [ ] **Step 8: Commit**

```bash
git add ui/panels/param_panel.py tools/test/test_param_panel_reload.py
git commit -m "feat(param_panel): add reload() and detailed tooltip on desc/name columns"
```

---

## Task 3: CsvLoadDialog 对话框组件

**Files:**
- Create: `ui/csv_load_dialog.py`
- Test: `tools/test/test_csv_load_dialog.py`

- [ ] **Step 1: 编写失败测试（验证对话框路径选择与恢复默认逻辑）**

Create `tools/test/test_csv_load_dialog.py`:

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python tools/test/test_csv_load_dialog.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'ui.csv_load_dialog'`

- [ ] **Step 3: 创建对话框组件**

Create `ui/csv_load_dialog.py`:

```python
"""CSV 参数表加载对话框: 选择 motor_info.csv / param_index.csv 路径并刷新。

主窗口调用流程:
    dlg = CsvLoadDialog(reg.motor_config_csv_path, reg.params_csv_path, self)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        chosen = dlg.chosen_paths()
        # 按 chosen['motor_config'] / chosen['motor_param'] reload registry + 刷新面板
"""

from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QHBoxLayout, QPushButton,
    QLabel, QLineEdit, QFileDialog,
)

from jmproto.registry import DEFAULT_MOTOR_CONFIG_CSV, DEFAULT_PARAMS_CSV
from ui.theme import theme


class CsvLoadDialog(QDialog):
    """选择参数表 CSV 路径。accept 后通过 chosen_paths() 取结果。"""

    def __init__(self, current_motor_info: str, current_param_index: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("加载参数表 CSV")
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)

        info = QLabel(
            "选择新的参数表 CSV 后, 电机配置 / PID参数 / 标定结果 / 运行时参数表格"
            " 将立即按新 CSV 重建(数据与分组格式均来自CSV)。路径会记住, 下次启动自动加载。")
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{theme.hex('muted')}; font-size:12px;")
        v.addWidget(info)

        form = QFormLayout()
        form.setSpacing(8)

        self._mc_edit = QLineEdit(current_motor_info or "")
        self._mc_edit.setReadOnly(True)
        self._reset_mc = QPushButton("恢复默认")
        self._reset_mc.clicked.connect(
            lambda: self._mc_edit.setText(DEFAULT_MOTOR_CONFIG_CSV))
        mc_row = self._make_row(self._mc_edit, self._reset_mc)
        form.addRow("电机配置表\n(motor_info.csv):", mc_row)

        self._pi_edit = QLineEdit(current_param_index or "")
        self._pi_edit.setReadOnly(True)
        self._reset_pi = QPushButton("恢复默认")
        self._reset_pi.clicked.connect(
            lambda: self._pi_edit.setText(DEFAULT_PARAMS_CSV))
        pi_row = self._make_row(self._pi_edit, self._reset_pi)
        form.addRow("运行时参数表\n(param_index.csv):", pi_row)

        v.addLayout(form)

        btns = QHBoxLayout()
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_apply = QPushButton("应用并刷新")
        btn_apply.setStyleSheet(self._ok_qss())
        btn_apply.clicked.connect(self.accept)
        btns.addStretch()
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_apply)
        v.addLayout(btns)

    def _make_row(self, edit: QLineEdit, reset_btn: QPushButton) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        browse = QPushButton("浏览...")
        browse.clicked.connect(lambda: self._pick(edit))
        row.addWidget(edit, 1)
        row.addWidget(browse)
        row.addWidget(reset_btn)
        return row

    def _pick(self, edit: QLineEdit):
        cur = edit.text().strip()
        start_dir = str(Path(cur).parent) if cur else str(Path.cwd())
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 CSV 文件", start_dir, "CSV 文件 (*.csv);;所有文件 (*)")
        if path:
            edit.setText(path)

    def chosen_paths(self) -> dict:
        """返回 {'motor_config': str|None, 'motor_param': str|None}。空串视为 None。"""
        mc = self._mc_edit.text().strip()
        pi = self._pi_edit.text().strip()
        return {
            "motor_config": mc or None,
            "motor_param": pi or None,
        }

    def _ok_qss(self) -> str:
        return (f"QPushButton{{background:{theme.hex('ok')};"
                f"color:{theme.hex('ok_text')};border:none;padding:6px 14px;"
                f"border-radius:4px;font-weight:bold;}}"
                f"QPushButton:hover{{background:{theme.hex('accent')};}}")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python tools/test/test_csv_load_dialog.py`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add ui/csv_load_dialog.py tools/test/test_csv_load_dialog.py
git commit -m "feat(ui): add CsvLoadDialog for selecting parameter table CSVs"
```

---

## Task 4: 主窗口加载按钮 + 对话框编排 + 全局面板刷新

**Files:**
- Modify: `ui/main_window.py`（导入对话框 + 菜单按钮 + 编排方法 + 刷新方法）

- [ ] **Step 1: 新增导入**

在 `ui/main_window.py` 第 38 行 `from ui.panels.fault_info_panel import FaultInfoPanel` 之后，新增一行：

```python
from ui.csv_load_dialog import CsvLoadDialog
```

- [ ] **Step 2: 在菜单配置组增加"加载参数表CSV"按钮**

在 `ui/main_window.py` 的 `_create_menu_config_group` 方法中，`self._btn_virtual_mode` 那段（`layout.addWidget(self._btn_virtual_mode, 2, 0, 1, 2)`）之后、`return grp` 之前，新增：

```python
        # 加载参数表 CSV: 运行时换 motor_info.csv / param_index.csv, 刷新全部参数表格
        btn_load_csv = QPushButton("加载参数表CSV")
        btn_load_csv.setToolTip(
            "加载新的 motor_info.csv 或 param_index.csv, "
            "电机配置/PID参数/标定结果/运行时参数表格将按新CSV重建(数据与格式均来自CSV)")
        btn_load_csv.clicked.connect(self._open_csv_load_dialog)
        self._apply_menu_button_style(btn_load_csv)
        layout.addWidget(btn_load_csv, 3, 0, 1, 2)
```

- [ ] **Step 3: 新增对话框编排与全局面板刷新方法**

在 `ui/main_window.py` 的 `_on_config_save` 方法（约第 1034 行）之前，新增两个方法：

```python
    def _open_csv_load_dialog(self):
        """打开 CSV 加载对话框: 选择 motor_info / param_index CSV 并刷新全部参数表格。"""
        dlg = CsvLoadDialog(
            self._registry.motor_config_csv_path,
            self._registry.params_csv_path,
            self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dlg.chosen_paths()
        mc_path = chosen["motor_config"]
        pi_path = chosen["motor_param"]
        reloaded = []
        if mc_path and mc_path != self._registry.motor_config_csv_path:
            if self._registry.reload_motor_config(mc_path):
                reloaded.append("电机配置表")
            else:
                self._log_panel.log_warn(f"[CSV] 电机配置表加载失败: {mc_path}")
        if pi_path and pi_path != self._registry.params_csv_path:
            if self._registry.reload_params(pi_path):
                reloaded.append("运行时参数表")
            else:
                self._log_panel.log_warn(f"[CSV] 运行时参数表加载失败: {pi_path}")
        if reloaded:
            self._reload_all_param_panels()
            # 持久化(含新CSV路径), 复用关窗同款写回
            layout_store.set_meta("settings", self._collect_settings())
            msg = f"已加载 {' / '.join(reloaded)}, 参数表格已刷新"
            self._log_panel.log(f"[CSV] {msg}")
            self.statusBar().showMessage(msg, 4000)
        else:
            self._log_panel.log("[CSV] 无变更(路径未变或加载失败)")

    def _reload_all_param_panels(self):
        """CSV 重新加载后, 重建所有依赖 registry 的 ParamPanel 表格(保留列宽)。"""
        panels = [self._param_panel, self._config_panel,
                  self._calib_results_panel, self._pid_params_panel]
        # reload 重建表格会清空列宽, 先备份
        backups = []
        for p in panels:
            try:
                backups.append(p.get_opts())
            except Exception:
                backups.append({})
        for p in panels:
            p.reload()
        for p, opt in zip(panels, backups):
            try:
                if opt:
                    p.set_opts(opt)
            except Exception:
                pass
```

- [ ] **Step 4: 冒烟验证（手动启动主窗口，点按钮开对话框）**

Run: `python main.py`
手动操作: 左侧"菜单配置"组点击"加载参数表CSV" → 对话框弹出，回显当前路径 → 取消。
Expected: 对话框正常弹出，两个路径行 + 浏览/恢复默认按钮齐全，取消无副作用。

- [ ] **Step 5: 回归 - 运行主窗口冒烟测试**

Run: `python tools/test/test_main_window_calib_results.py`
Expected: 全部 PASS（主窗口能正常构建，新按钮不影响既有结构）

- [ ] **Step 6: Commit**

```bash
git add ui/main_window.py
git commit -m "feat(main_window): add CSV load button and global panel refresh orchestration"
```

---

## Task 5: CSV 路径持久化（收集 + 启动恢复）

**Files:**
- Modify: `ui/main_window.py`（`_collect_settings` 加 csv_paths；`_apply_settings` 加启动恢复块）

- [ ] **Step 1: 在 `_collect_settings` 收集 csv_paths**

在 `ui/main_window.py` 的 `_collect_settings` 方法中，找到 `"virtual_motor_mode": bool(self._btn_virtual_mode.isChecked()),` 这一行（约第 399 行），在它之后、`# 各面板配置` 注释之前，新增：

```python
            # 参数表 CSV 路径(运行时换表后记住, 下次启动自动加载)
            "csv_paths": {
                "motor_config": self._registry.motor_config_csv_path,
                "motor_param": self._registry.params_csv_path,
            },
```

- [ ] **Step 2: 在 `_apply_settings` 增加启动恢复块**

在 `ui/main_window.py` 的 `_apply_settings` 方法中，找到虚拟电机模式恢复块（约第 491-495 行）：

```python
        # 虚拟电机模式开关: 仅记录期望状态, 待连接虚拟引擎后套用
        # (开关可用性取决于是否连接虚拟引擎, 启动时未连接故保持禁用)
        vm = d.get("virtual_motor_mode")
        if isinstance(vm, bool):
            self._pending_virtual_mode = vm
```

在它之后、`# 各面板配置 (容错: 单面板失败不影响其他)` 注释之前，新增：

```python
        # 参数表 CSV 路径: 启动时按持久化路径重新加载 registry 并刷新依赖面板
        # (面板在 __init__._build_ui 已用默认 CSV 建好, 此处按自定义 CSV 重建;
        #  必须在下面"各面板配置"opts 循环之前执行, 以便 opts 循环恢复列宽)
        csv_paths = d.get("csv_paths")
        if isinstance(csv_paths, dict):
            reloaded_any = False
            mc = csv_paths.get("motor_config")
            if mc and Path(mc).is_file() and mc != self._registry.motor_config_csv_path:
                if self._registry.reload_motor_config(mc):
                    reloaded_any = True
            pi = csv_paths.get("motor_param")
            if pi and Path(pi).is_file() and pi != self._registry.params_csv_path:
                if self._registry.reload_params(pi):
                    reloaded_any = True
            if reloaded_any:
                for panel in (self._param_panel, self._config_panel,
                              self._calib_results_panel, self._pid_params_panel):
                    panel.reload()
```

- [ ] **Step 3: 编写持久化往返测试**

Create `tools/test/test_csv_paths_persistence.py`:

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python tools/test/test_csv_paths_persistence.py`
Expected: 全部 PASS

- [ ] **Step 5: 端到端手动验证**

Run: `python main.py`
手动操作:
1. 点"加载参数表CSV" → 电机配置表"浏览"选一个新 CSV → 应用并刷新 → 表格重建。
2. 关闭主窗口。
3. 重新 `python main.py` → 表格应自动用上次的 CSV（验证持久化恢复）。

Expected: 表格按新 CSV 重建，重启后自动恢复，状态栏提示加载成功。

- [ ] **Step 6: 回归全部相关测试**

Run（顺序执行）:
```
python tools/test/test_registry_reload.py
python tools/test/test_param_panel_reload.py
python tools/test/test_csv_load_dialog.py
python tools/test/test_csv_paths_persistence.py
python tools/test/test_param_panel_group_filter.py
python tools/test/test_main_window_calib_results.py
```
Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add ui/main_window.py tools/test/test_csv_paths_persistence.py
git commit -m "feat(main_window): persist CSV paths and restore on startup"
```

---

## Self-Review

**1. Spec coverage:**
- 「电机的配置参数和PID参数…通过加载一个CSV动态生成数据和格式」→ Task 1（registry reload）+ Task 2（面板reload）+ Task 4（UI加载+刷新）。motor_info.csv 同时含电机配置与PID(ControlParam)，加载后 `_config_panel`/`_pid_params_panel`/`_calib_results_panel` 全部重建。✓
- 「加载一个新的CSV自动就修改表格的参数」→ Task 4 `_reload_all_param_panels()` 自动刷新4个依赖面板。✓
- 「覆盖 motor_info + param_index 两张CSV」→ Task 1 同时实现 `reload_motor_config` 与 `reload_params`；Task 3 对话框两行；Task 4 两路 reload。✓
- 「持久化到 ui_layout.json」→ Task 5 `_collect_settings` 写 csv_paths + `_apply_settings` 启动恢复。✓
- 「鼠标放描述上淡淡显示最大值/最小值，描述更详细含单位/Remarks」→ Task 2 `_build_param_tooltip()` + 在 `_append_param_row` 给描述/名称列挂 tooltip。tooltip 内容含 中文名/单位/类型/范围[min,max]/读写/说明(含默认值+Remarks)。reload 后 tooltip 自动更新。✓

**2. Placeholder scan:** 无 TBD/TODO/"add error handling"等占位；每个代码步均含完整代码与命令。✓

**3. Type一致性:**
- registry 方法名：`reload_motor_config` / `reload_params` / 属性 `motor_config_csv_path` / `params_csv_path` / 常量 `DEFAULT_MOTOR_CONFIG_CSV` / `DEFAULT_PARAMS_CSV` — Task1定义，Task3/4/5引用一致。✓
- ParamPanel 方法：`reload()` / `_build_param_tooltip(p)` — Task2定义，Task4/5引用 `reload()` 一致。✓
- 对话框：`CsvLoadDialog(current_motor_info, current_param_index, parent)` / `chosen_paths() -> {'motor_config':..., 'motor_param':...}` / 内部 `_mc_edit`/`_pi_edit`/`_reset_mc`/`_reset_pi` — Task3定义，Task3测试与Task4引用一致。✓
- 主窗口方法：`_open_csv_load_dialog` / `_reload_all_param_panels` — Task4定义，Task5不新增方法仅改两个既有方法。✓

无 spec 缺口，类型一致，无占位。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-09-csv-driven-param-tables.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
