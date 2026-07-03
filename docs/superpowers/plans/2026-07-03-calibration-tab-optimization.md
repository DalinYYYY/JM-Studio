# 电机标定 Tab 优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把电机标定 Tab 从"下拉框选级别 + 单一启动按钮"改造为"按类型直接点击启动 + 内嵌标定结果表(下位机读回值/修改值/保存到Flash)"的直观界面。

**Architecture:** 复用现有 `ParamPanel(source="motor_config")` 的读/写/dirty/固化机制,通过新增可选 `groups` 过滤参数只展示 `MotorCalibParam` 段(Index 16~42,即标定结果)。`CalibrationPanel` 重构为:状态卡 + L1~L7 可点击任务网格(子项按钮一点即启动) + 通用操作(查询/中止/自动查询) + 内嵌标定结果 `ParamPanel` + 操作历史。`main_window` 复用既有参数读写队列(`_on_param_read`/`_on_param_result`)连接内嵌面板。

**Tech Stack:** PyQt6 (QGridLayout/QGroupBox/QPushButton/QTableWidget)、jmproto.registry (motor_info.csv → MotorCalibParam 分组)、jmproto.cmd_def (0x90~0x98 标定指令 + 0xE6~0xEA 配置读写/固化)、项目既有 `check()` 风格测试脚本。

---

## 背景与协议映射(实现前必读)

本工程存在两套参数通道,标定结果落点在 **motor_info** 通道:

| 通道 | 命令 | 用途 | CSV |
|---|---|---|---|
| 运行时参数 | 0xE0~0xE5 | RAM 参数,标定无关 | `joint_motor_param_index.csv` |
| 电机配置(motor_info) | 0xE6~0xEB | **Flash/EEPROM 持久化的校准数据** | `motor_info.csv` |

标定结果集中在 `motor_info.csv` 的 **`MotorCalibParam` 分组**(BlockOffset=0x0080, Index 16~42),含 `is_calibrated`/`pole_pairs`/`phase_resistance`/`phase_inductance_d/q`/`flux_linkage`/`torque_constant`/`enc_offset`/`elec_angle_bias`/`shunt_resistance`/`current_amp_gain` 等 27 项。

关键命令:
- `MOTOR_INFO_READ`(0xE6) 单读 / `MOTOR_INFO_READ_BULK`(0xE8) 批量读 → 回显"下位机的值"
- `MOTOR_INFO_WRITE`(0xE7) 单写(RAM 生效) → 修改标定值
- `MOTOR_INFO_SAVE`(0xEA) → **保存到 Flash/EEPROM**(用户核心诉求)
- `MOTOR_INFO_RESET`(0xEB) → 恢复默认

`JmClient` 已封装全部方法(`core/motor_client.py:148-180`),`ParamPanel(source="motor_config")` 已自动走 0xE6~0xEB 通道,`main_window._on_param_read/_on_param_result` 串行队列已支持 motor_config 源。本计划只需:① 给 ParamPanel 加分组过滤;② 重构 CalibrationPanel;③ 连线。

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `ui/panels/param_panel.py` | 参数表读/写/dirty/固化 | **Modify**: 新增 `groups` 可选过滤参数(向后兼容) |
| `ui/panels/calibration_panel.py` | 标定 Tab 主体 | **Rewrite**: 可点击任务网格 + 内嵌结果 ParamPanel + 历史 |
| `ui/main_window.py` | 信号连线 | **Modify**: 传 registry 给 CalibrationPanel + 连内嵌面板信号 |
| `tools/test/test_param_panel_group_filter.py` | 分组过滤回归 | **Create** |
| `tools/test/test_calibration_panel.py` | 标定面板行为 | **Create** |

设计边界:
- `ParamPanel` 只加一个可选构造参数,不改动既有列/dirty/写流程 → 零回归风险。
- `CalibrationPanel` 内嵌一个 `ParamPanel` 实例并作为公共属性 `config_panel` 暴露 → `main_window` 直接复用既有 `_on_param_read(panel, param_id)` 队列模式,无需新写读写逻辑。
- 标定指令(0x90~0x98)发送仍走 `CalibrationPanel.send_command` → `main_window._on_motion_command`(已存在)。

---

### Task 1: 给 ParamPanel 增加分组过滤参数

**Files:**
- Modify: `ui/panels/param_panel.py:69-82` (构造函数) 和 `ui/panels/param_panel.py:543-546` (`_params_by_group`)
- Test: `tools/test/test_param_panel_group_filter.py`

- [ ] **Step 1: 写失败测试**

Create `tools/test/test_param_panel_group_filter.py`:

```python
"""ParamPanel 分组过滤测试: 验证 groups 参数能限定展示的参数分组。"""

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
from ui.panels.param_panel import ParamPanel

app = QApplication.instance() or QApplication(sys.argv)
reg = get_registry()

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


# 1. 不传 groups(向后兼容): motor_config 应包含全部分组(>=2 个)
panel_all = ParamPanel(reg, source="motor_config")
check(len(panel_all._group_param_ids) >= 2,
      f"无过滤应含多个分组, 实际 {len(panel_all._group_param_ids)}")

# 2. 传 groups=("MotorCalibParam",): 只剩这一个分组
panel_filt = ParamPanel(reg, source="motor_config", groups=("MotorCalibParam",))
groups_seen = list(panel_filt._group_param_ids.keys())
check(groups_seen == ["MotorCalibParam"],
      f"过滤后应仅含 MotorCalibParam, 实际 {groups_seen}")

# 3. 过滤后面板应包含标定段 Index 16~42 的若干项, 不含 SystemParam 的 Index 0
pids = set(panel_filt._param_specs.keys())
check(16 in pids, "应包含 is_calibrated (Index 16)")
check(38 in pids, "应包含 elec_angle_bias (Index 38)")
check(0 not in pids, "不应包含 SystemParam 的 Index 0 (config_version)")

# 4. 传不存在的分组: 面板为空但不报错
panel_empty = ParamPanel(reg, source="motor_config", groups=("NotExist",))
check(len(panel_empty._param_specs) == 0,
      f"不存在分组应得到空面板, 实际 {len(panel_empty._param_specs)}")

# 5. 运行时参数源同样支持过滤
panel_rt = ParamPanel(reg, source="motor_param", groups=("电机本体",))
check(len(panel_rt._group_param_ids) == 1,
      f"运行时参数过滤应剩 1 组, 实际 {len(panel_rt._group_param_ids)}")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python tools/test/test_param_panel_group_filter.py`
Expected: FAIL — `TypeError: ParamPanel.__init__() got an unexpected keyword argument 'groups'`

- [ ] **Step 3: 修改 ParamPanel 构造函数接收 groups**

Edit `ui/panels/param_panel.py` 构造函数签名(约第 69-82 行),把 `groups` 加到形参末尾并保存。

定位当前签名:
```python
    def __init__(self, registry, parent=None, title="电机参数",
                 source="motor_param", show_save=False, save_text="保存到Flash"):
        super().__init__("", parent)
        self._panel_name = title
        self._reg = registry
        self._source = source
        self._show_save = bool(show_save)
        self._save_text = save_text
```

替换为:
```python
    def __init__(self, registry, parent=None, title="电机参数",
                 source="motor_param", show_save=False, save_text="保存到Flash",
                 groups=None):
        super().__init__("", parent)
        self._panel_name = title
        self._reg = registry
        self._source = source
        self._show_save = bool(show_save)
        self._save_text = save_text
        # 仅展示指定分组(按 motor_info.csv / param_index.csv 的 group 名);
        # None 表示不过滤(向后兼容)。
        self._groups = tuple(groups) if groups else None
```

- [ ] **Step 4: 修改 _params_by_group 应用过滤**

Edit `ui/panels/param_panel.py` 的 `_params_by_group`(约第 543-546 行)。

定位当前实现:
```python
    def _params_by_group(self):
        if self._source == "motor_config":
            return self._reg.motor_config_by_group()
        return self._reg.params_by_group()
```

替换为:
```python
    def _params_by_group(self):
        if self._source == "motor_config":
            all_groups = self._reg.motor_config_by_group()
        else:
            all_groups = self._reg.params_by_group()
        if self._groups:
            return {g: params for g, params in all_groups.items()
                    if g in self._groups}
        return all_groups
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python tools/test/test_param_panel_group_filter.py`
Expected: `PASS=5 FAIL=0`

- [ ] **Step 6: 跑既有参数面板回归测试,确认零回归**

Run: `python tools/test/test_param_panel_highlight.py`
Expected: 全部 PASS(既有 `电机参数`/`电机配置` 面板未传 groups,行为不变)

- [ ] **Step 7: Commit**

```bash
git add ui/panels/param_panel.py tools/test/test_param_panel_group_filter.py
git commit -m "feat(param): ParamPanel 支持 groups 分组过滤参数"
```

---

### Task 2: 重写 CalibrationPanel — 可点击任务网格 + 状态/操作/历史

**Files:**
- Modify: `ui/panels/calibration_panel.py` (整体重写,保留 `_CALIB_LEVELS` 数据与既有信号契约)
- Modify: `ui/main_window.py:145` (传 registry)
- Test: `tools/test/test_calibration_panel.py`

本任务先交付"可点击任务网格 + 状态卡 + 通用操作 + 历史"(不含内嵌结果表,下一任务加)。构造函数改为接收 `registry`(为下一任务内嵌 ParamPanel 做准备,本任务先存为属性)。

- [ ] **Step 1: 写失败测试**

Create `tools/test/test_calibration_panel.py`:

```python
"""电机标定面板行为测试。

验证:
1. 构造接收 registry, 暴露 config_panel 属性(本任务可为 None, 下任务补)
2. L1~L7 任务按钮齐全, 数量与 _CALIB_LEVELS 子项总数一致
3. 点击子项按钮 → 发出 send_command(cmd, {"submode": N})
4. 未连接时点击 → 不发指令, 历史记录"未连接"
5. set_link_active / update_state / on_ack / on_nack 既有契约保留
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
from jmproto import JmCmd, JmErr, TopFsm
from ui.panels.calibration_panel import CalibrationPanel, _CALIB_LEVELS

app = QApplication.instance() or QApplication(sys.argv)
reg = get_registry()

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


panel = CalibrationPanel(reg)

# 1. config_panel 属性存在(本任务可为 None)
check(hasattr(panel, "config_panel"), "应暴露 config_panel 属性")

# 2. 任务按钮数量 = _CALIB_LEVELS 全部子项总数
total_subs = sum(len(subs) for _, _, subs in _CALIB_LEVELS)
check(len(panel._task_buttons) == total_subs,
      f"应有 {total_subs} 个任务按钮, 实际 {len(panel._task_buttons)}")

# 3. 未连接时点击 → 不发指令
received = []
panel.send_command.connect(lambda cmd, vals: received.append((cmd, vals)))
panel.set_link_active(False)
first_key = next(iter(panel._task_buttons))
first_btn = panel._task_buttons[first_key]
first_btn.click()
check(len(received) == 0, "未连接时点击不应发出指令")

# 4. 已连接时点击 → 发出正确 (cmd, submode)
panel.set_link_active(True)
first_btn.click()
cmd0, _, subs0 = _CALIB_LEVELS[0]
expected_sub = subs0[0][0]
check(len(received) == 1, "点击应触发一次 send_command")
if received:
    check(received[0][0] == int(cmd0), f"cmd 应为 0x{int(cmd0):02X}")
    check(received[0][1] == {"submode": expected_sub},
          f"submode 应为 {expected_sub}, 实际 {received[0][1]}")

# 5. 点击不同子项发出不同 submode
received.clear()
last_key = list(panel._task_buttons.keys())[-1]
panel._task_buttons[last_key].click()
check(len(received) == 1, "末位按钮点击应触发一次")
if received:
    check(received[0][0] == int(_CALIB_LEVELS[-1][0]), "末位按钮 cmd 应为 L7")

# 6. 既有契约: on_ack(查询完成) 清除 running 标志
panel._calib_running = True
panel.on_ack(int(JmCmd.CALIB_QUERY))
check(panel._calib_running is False, "ACK 查询完成应清除 running")

# 7. 既有契约: on_nack(查询, CALIB_BUSY) 置 running
panel.on_nack(int(JmCmd.CALIB_QUERY), int(JmErr.CALIB_BUSY))
check(panel._calib_running is True, "CALIB_BUSY 应置 running")

# 8. 既有契约: update_state 进入 CALIB 态
panel.update_state(int(TopFsm.CALIB), 0, 0, 0)
check(panel._calib_running is True, "进入 CALIB 态应置 running")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python tools/test/test_calibration_panel.py`
Expected: FAIL — `CalibrationPanel` 构造不接受 registry 参数 / 无 `_task_buttons` / 无 `config_panel` 属性。

- [ ] **Step 3: 重写 calibration_panel.py**

Replace the entire content of `ui/panels/calibration_panel.py` with:

```python
"""电机标定面板

封装标定指令(0x90~0x98) 与标定结果读写(0xE6~0xEA)。

UI 布局 (紧凑, 状态卡仅 2 行 ~70px):
  1. 紧凑状态卡:
     - 主信息行(单行, 横向排列): [●状态点] [状态文本] · [最近操作] [弹簧]
       [已标定徽章][标记][清除] [查询][中止][自动查询]
     - 当前选中任务行(小字, 带左侧高亮条): 级别>子项 CMD=0xXX submode=N · 描述
  2. 标定任务: 顶部 QTabWidget, L1~L7 每级一个 Tab, 选中 Tab 才显示该级子项卡片,
     点击子项卡片即启动该标定
  3. 标定结果: 内嵌 ParamPanel(source="motor_config", groups=["MotorCalibParam"]),
     展示下位机读回值/修改值/单位, 支持单读/单写/全读/全写/保存到Flash(0xEA)
  4. 操作历史: 时间戳 + TX/ACK/NACK 文本

数据来源:
  - registry "校准" 类命令 (joint_motor_command_list.csv 第 58~66 条)
  - motor_info.csv 的 MotorCalibParam 分段(Index 16~42, 标定结果)
  - cmd_def.JmCmd.CALIB_* / JmErr.CALIB_BUSY / TopFsm.CALIB
  - 主窗口转发的 ACK/NACK/state_updated/connected 信号
"""

from collections import deque
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QPushButton, QTabWidget, QTextEdit, QVBoxLayout,
    QWidget, QSizePolicy,
)

from jmproto import JmCmd, JmErr, TopFsm, cmd_name, err_name_cn, top_fsm_name
from ui.theme import theme


# ==================== L1~L7 标定级别定义 ====================
# 每条: (cmd, 级别名称, [(sub_id, 子模式名, 描述), ...])
# 子模式范围严格对齐 CSV(joint_motor_command_list.csv 第 58~64 行备注)
_CALIB_LEVELS = [
    (JmCmd.CALIB_LEVEL1, "L1 驱动硬件底层", [
        (1, "ADC偏置",     "电流/电压采样通道零点偏置校正"),
        (2, "ADC增益",     "电流/电压采样通道增益校正"),
        (3, "电流传感器",  "相电流传感器线性度与零漂"),
        (4, "温度传感器",  "FET/电机 NTC 温度采样校正"),
        (5, "母线电压",    "母线电压分压比与零点校正"),
        (6, "死区特性",    "逆变器死区时间与管压降补偿"),
    ]),
    (JmCmd.CALIB_LEVEL2, "L2 电机电气身份", [
        (1, "相序",          "U/V/W 相序方向辨识"),
        (2, "极对数",        "电机极对数自动辨识"),
        (3, "R 相电阻",      "相电阻辨识 (DC法)"),
        (4, "Ld d轴电感",    "d轴电感辨识 (阶跃响应)"),
        (5, "Lq q轴电感",    "q轴电感辨识 (阶跃响应)"),
        (6, "flux 磁链",     "永磁体磁链辨识 (反电势法)"),
    ]),
    (JmCmd.CALIB_LEVEL3, "L3 编码器校准", [
        (1, "零位",          "编码器电角度零点对齐"),
        (2, "方向",          "编码器计数方向校验"),
        (3, "线性度",        "编码器非线性度扫描"),
        (4, "正余弦/旋变",   "正余弦/旋变解码参数校准"),
        (5, "多圈零点",      "多圈计数器零点校准"),
    ]),
    (JmCmd.CALIB_LEVEL4, "L4 转矩基础", [
        (1, "力矩常数 Kt",   "转矩常数 Kt 辨识"),
    ]),
    (JmCmd.CALIB_LEVEL5, "L5 非线性补偿", [
        (1, "齿槽",          "齿槽转矩纹波补偿表生成"),
        (2, "摩擦",          "摩擦模型(库仑+粘性)辨识"),
        (3, "死区补偿",      "逆变器死区非线性补偿"),
        (4, "磁饱和",        "dq 轴磁饱和电感曲线辨识"),
    ]),
    (JmCmd.CALIB_LEVEL6, "L6 负载系统级", [
        (1, "惯量",          "负载转动惯量辨识"),
        (2, "阻尼",          "负载粘性阻尼系数辨识"),
        (3, "回程间隙",      "减速器回程间隙测量"),
        (4, "PID 自整定",    "速度/位置环 PID 自动整定"),
    ]),
    (JmCmd.CALIB_LEVEL7, "L7 自动化集成", [
        (1, "一键全自动",    "依次执行 L1~L6 全套标定"),
    ]),
]


class CalibrationPanel(QGroupBox):
    """电机标定面板 (顶级 Tab)。

    信号:
      send_command(cmd, values): 发送标定指令, 主窗口连到 JmClient.send_command
        - 启动: cmd=0x90~0x96, values={"submode": N}
        - 查询: cmd=0x97, values={}
        - 中止: cmd=0x98, values={}

    公共属性:
      config_panel: 内嵌的标定结果 ParamPanel(motor_config 源), 由主窗口连接其
        read_param/write_param/read_params/write_params/save_all 信号。

    由主窗口调用的入口:
      set_link_active(active):        链路状态变化
      on_ack(cmd):                    标定命令 ACK (内部按 cmd 过滤)
      on_nack(cmd, err):              标定命令 NACK (内部按 cmd 过滤)
      update_state(top, run, mode, en): 状态机更新, 用于判断是否进入 CALIB 态
      apply_theme():                  主题切换
    """

    send_command = pyqtSignal(int, dict)

    _MAX_HISTORY = 200
    _POLL_PERIOD_MS = 500   # 标定进行中自动查询进度周期

    def __init__(self, registry, parent=None):
        super().__init__("", parent)
        self._reg = registry
        self._panel_name = "电机标定"
        self._link_active = False
        self._current_top_fsm = None
        self._calib_running = False     # 本地推测: 标定是否进行中
        self._last_op = ""              # 最近一次操作结果文本
        self._history = deque(maxlen=self._MAX_HISTORY)
        self._task_buttons = {}         # (cmd, sub_id) -> QPushButton
        self._active_task_key = None    # 当前选中任务 (cmd, sub_id)
        # 内嵌标定结果面板(下一任务填充, 本任务先置 None 保持属性契约)
        self._config_panel = None

        self._build()

        # 标定进行中周期查询进度
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self._POLL_PERIOD_MS)
        self._poll_timer.timeout.connect(self._on_poll_tick)

        self._refresh_status()

    @property
    def config_panel(self):
        """内嵌的标定结果 ParamPanel(下一任务创建); 未创建时为 None。"""
        return self._config_panel

    def panel_name(self) -> str:
        return self._panel_name

    # ==================== UI 构建 ====================
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self._build_status_card(layout)      # 含标定操作(查询/中止/自动查询)
        self._build_task_launcher(layout)    # L1~L7 顶部 QTabWidget
        self._build_results_placeholder(layout)
        self._build_history(layout)

    def _build_status_card(self, parent_layout):
        """紧凑状态卡: 主信息行(单行) + 当前选中任务行(小字), 总高 ~70px."""
        self._status_card = QFrame()
        self._status_card.setFrameShape(QFrame.Shape.StyledPanel)
        self._status_card.setStyleSheet(f"""
            QFrame {{
                background: {theme.hex('card_bottom')};
                border: 1px solid {theme.hex('border')};
                border-radius: 4px;
            }}
        """)
        sc_layout = QVBoxLayout(self._status_card)
        sc_layout.setContentsMargins(8, 5, 8, 5)
        sc_layout.setSpacing(3)

        # --- 主信息行(单行横向): 状态点+文本 · 最近操作 [弹簧] 已标定+按钮 + 操作按钮 ---
        main_row = QHBoxLayout()
        main_row.setContentsMargins(0, 0, 0, 0)
        main_row.setSpacing(6)

        self._lbl_status_dot = QLabel("●")
        self._lbl_status_dot.setStyleSheet(
            f"color: {theme.hex('muted')}; font-size: 14px; border:none;")
        self._lbl_status_dot.setFixedWidth(14)
        main_row.addWidget(self._lbl_status_dot)

        self._lbl_status_text = QLabel("未连接")
        self._lbl_status_text.setStyleSheet(
            f"font-family: Consolas, 'Microsoft YaHei', monospace; font-size: 12px; "
            f"font-weight: bold; color: {theme.hex('muted')}; border:none;")
        main_row.addWidget(self._lbl_status_text)

        self._lbl_sep = QLabel("·")
        self._lbl_sep.setStyleSheet(
            f"color: {theme.hex('muted')}; font-size: 11px; border:none;")
        main_row.addWidget(self._lbl_sep)

        self._lbl_last_op = QLabel("最近操作: —")
        self._lbl_last_op.setStyleSheet(
            f"color: {theme.hex('muted')}; font-size: 11px; border:none;")
        main_row.addWidget(self._lbl_last_op)

        main_row.addStretch()

        # 已标定指示器 + 设置/清除按钮(Task 5 在此槽位插入; 这里先占位)
        self._build_calib_flag_controls(main_row)

        # 标定操作嵌入状态卡右侧 (查询/中止/自动查询)
        self._btn_query = QPushButton("查询")
        self._btn_query.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_query.setFixedHeight(22)
        self._btn_query.setStyleSheet(
            f"QPushButton {{ background: {theme.hex('input_bg')}; color: {theme.hex('text')}; "
            f"border: 1px solid {theme.hex('border')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 11px; }}"
            f"QPushButton:hover {{ border-color: {theme.hex('accent')}; "
            f"color: {theme.hex('accent')}; }}")
        self._btn_query.clicked.connect(self._on_query_clicked)
        main_row.addWidget(self._btn_query)

        self._btn_abort = QPushButton("中止")
        self._btn_abort.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_abort.setFixedHeight(22)
        self._btn_abort.setStyleSheet(
            f"QPushButton {{ background-color: {theme.hex('danger')}; color: {theme.hex('danger_text')}; "
            f"border: 1px solid {theme.hex('danger')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 11px; font-weight: bold; }}"
            f"QPushButton:hover {{ opacity: 0.85; }}")
        self._btn_abort.clicked.connect(self._on_abort_clicked)
        main_row.addWidget(self._btn_abort)

        self._chk_auto_poll = QCheckBox("自动")
        self._chk_auto_poll.setChecked(True)
        self._chk_auto_poll.setStyleSheet(
            f"QCheckBox {{ color: {theme.hex('muted')}; font-size: 11px; spacing: 3px; }}")
        self._chk_auto_poll.toggled.connect(self._on_auto_poll_toggled)
        main_row.addWidget(self._chk_auto_poll)

        sc_layout.addLayout(main_row)

        # --- 当前选中任务行(小字, 带左侧高亮条) ---
        self._lbl_active_task = QLabel("当前选中: —")
        self._lbl_active_task.setWordWrap(False)
        self._lbl_active_task.setTextFormat(Qt.TextFormat.PlainText)
        self._lbl_active_task.setStyleSheet(
            f"color: {theme.hex('text')}; font-size: 11px; border:none; "
            f"background: {theme.hex('card_bottom')}; "
            f"border-left: 2px solid {theme.hex('accent')}; "
            f"padding: 2px 8px; border-radius: 2px;")
        sc_layout.addWidget(self._lbl_active_task)

        parent_layout.addWidget(self._status_card)

    def _build_calib_flag_controls(self, layout):
        """已标定指示器 + 设置/清除按钮占位(Task 5 实现, 这里先放空 widget 保持槽位)."""
        # Task 5 会在此插入 _lbl_calib_flag / _btn_mark_calibrated / _btn_clear_calibrated
        # 占位 widget 避免 layout 在 Task 5 之前为空
        placeholder = QWidget()
        layout.addWidget(placeholder)

    def _build_task_launcher(self, parent_layout):
        """L1~L7 顶部 QTabWidget, 每个 Tab 显示该级别的子项卡片, 点击卡片即启动."""
        grp = QGroupBox("标定任务  (选中级别 Tab → 点击子项启动)")
        v = QVBoxLayout(grp)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        self._task_tabs = QTabWidget()
        self._task_tabs.setDocumentMode(True)
        self._task_tabs.setStyleSheet(self._tab_style())

        for cmd, level_name, submodes in _CALIB_LEVELS:
            page = QWidget()
            page_layout = QHBoxLayout(page)
            page_layout.setContentsMargins(8, 8, 8, 8)
            page_layout.setSpacing(8)
            page_layout.addStretch()
            # 每个子项为可点击卡片(QPushButton 带描述), 横向排列
            for sub_id, sub_name, sub_desc in submodes:
                btn = QPushButton(f"{sub_name}\n{sub_desc}")
                btn.setToolTip(f"CMD=0x{int(cmd):02X}  submode={sub_id}\n{sub_desc}")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setMinimumWidth(130)
                btn.setMinimumHeight(54)
                btn.setStyleSheet(self._task_btn_style(active=False))
                btn.clicked.connect(
                    lambda _=False, c=int(cmd), s=int(sub_id),
                           ln=level_name, sn=sub_name, sd=sub_desc:
                    self._on_task_clicked(c, s, ln, sn, sd))
                self._task_buttons[(int(cmd), int(sub_id))] = btn
                page_layout.addWidget(btn)
            page_layout.addStretch()
            tab_title = f"{level_name}  0x{int(cmd):02X}"
            self._task_tabs.addTab(page, tab_title)

        v.addWidget(self._task_tabs)
        parent_layout.addWidget(grp)

    def _tab_style(self) -> str:
        return f"""
            QTabWidget::pane {{
                border: 1px solid {theme.hex('border')};
                border-radius: 4px;
                top: -1px;
            }}
            QTabBar::tab {{
                background: {theme.hex('input_bg')};
                color: {theme.hex('muted')};
                border: 1px solid {theme.hex('border')};
                border-bottom: none;
                padding: 4px 12px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                font-size: 12px;
            }}
            QTabBar::tab:selected {{
                background: {theme.hex('card_bottom')};
                color: {theme.hex('accent')};
                border-color: {theme.hex('border')};
                font-weight: bold;
            }}
            QTabBar::tab:hover:!selected {{
                color: {theme.hex('text')};
            }}
        """

    def _build_results_placeholder(self, parent_layout):
        """标定结果占位: 下一任务替换为内嵌 ParamPanel。"""
        self._results_container = QWidget()
        self._results_layout = QVBoxLayout(self._results_container)
        self._results_layout.setContentsMargins(0, 0, 0, 0)
        parent_layout.addWidget(self._results_container, 1)

    def _build_history(self, parent_layout):
        grp = QGroupBox("操作历史")
        v = QVBoxLayout(grp)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        self._history_view = QTextEdit()
        self._history_view.setReadOnly(True)
        self._history_view.setStyleSheet(
            f"background: {theme.hex('log_bg')}; color: {theme.hex('log_text')}; "
            f"font-family: Consolas, 'Microsoft YaHei', monospace; font-size: 12px; "
            f"border: 1px solid {theme.hex('border')};")
        self._history_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        v.addWidget(self._history_view, 1)

        op_row = QHBoxLayout()
        op_row.addStretch()
        self._btn_clear_history = QPushButton("清空历史")
        self._btn_clear_history.setFixedHeight(24)
        self._btn_clear_history.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear_history.clicked.connect(self._on_clear_history)
        op_row.addWidget(self._btn_clear_history)
        v.addLayout(op_row)

        parent_layout.addWidget(grp, 1)

    # ==================== 任务按钮样式 (卡片式) ====================
    def _task_btn_style(self, active: bool = False) -> str:
        bg = theme.hex('accent') if active else theme.hex('input_bg')
        fg = theme.hex('card_bottom') if active else theme.hex('text')
        border = theme.hex('accent') if active else theme.hex('input_border')
        return (
            f"QPushButton {{"
            f"  background-color: {bg}; color: {fg};"
            f"  border: 1px solid {border}; border-radius: 6px;"
            f"  padding: 6px 10px; font-size: 12px;"
            f"  text-align: left;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background-color: {theme.hex('accent')}; color: {theme.hex('card_bottom')};"
            f"  border-color: {theme.hex('accent')};"
            f"}}"
        )

    def _refresh_task_buttons_style(self):
        for key, btn in self._task_buttons.items():
            btn.setStyleSheet(self._task_btn_style(active=(key == self._active_task_key)))

    # ==================== 任务点击 ====================
    def _on_task_clicked(self, cmd, sub_id, level_name, sub_name, sub_desc):
        self._set_active_task(cmd, sub_id, level_name, sub_name, sub_desc)
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接, 无法启动")
            return
        self._add_history(
            f"[TX] 启动 {cmd_name(cmd)} submode={sub_id} ({level_name}>{sub_name})")
        self.send_command.emit(int(cmd), {"submode": int(sub_id)})

    def _set_active_task(self, cmd, sub_id, level_name, sub_name, sub_desc):
        self._active_task_key = (int(cmd), int(sub_id))
        self._lbl_active_task.setText(
            f"当前选中: [{level_name} > {sub_name}]  "
            f"CMD=0x{int(cmd):02X}, submode={sub_id}\n{sub_desc}")
        self._refresh_task_buttons_style()

    # ==================== 状态接收 (由主窗口调用) ====================
    def set_link_active(self, active: bool):
        self._link_active = bool(active)
        if not active:
            self._calib_running = False
            self._poll_timer.stop()
        self._refresh_status()

    def update_state(self, top_fsm: int, run_state: int, ctrl_mode: int, enable: int):
        """接收主窗口转发的状态机更新, 用于判断是否处于 CALIB 态."""
        prev = self._current_top_fsm
        self._current_top_fsm = top_fsm
        in_calib = top_fsm == int(TopFsm.CALIB)
        prev_in_calib = prev == int(TopFsm.CALIB) if prev is not None else False
        if in_calib and not prev_in_calib:
            self._calib_running = True
            if self._chk_auto_poll.isChecked() and self._link_active:
                self._poll_timer.start()
            self._add_history(f"[进入标定态] top_fsm={top_fsm_name(top_fsm)}")
        elif not in_calib and prev_in_calib:
            self._calib_running = False
            self._poll_timer.stop()
            self._add_history(f"[退出标定态] top_fsm={top_fsm_name(top_fsm)}")
        self._refresh_status()

    def on_ack(self, cmd: int):
        """标定相关命令 ACK (主窗口在 _on_ack 中调用, 内部按 cmd 过滤)."""
        if not self._is_calib_cmd(cmd):
            return
        if cmd == JmCmd.CALIB_QUERY:
            self._calib_running = False
            self._poll_timer.stop()
            self._set_last_op("查询: 标定完成")
            self._add_history(f"[ACK] {cmd_name(cmd)}(0x{cmd:02X}) 标定完成")
        elif cmd == JmCmd.CALIB_ABORT:
            self._calib_running = False
            self._poll_timer.stop()
            self._set_last_op("已中止标定")
            self._add_history(f"[ACK] {cmd_name(cmd)}(0x{cmd:02X}) 已中止")
        else:
            lvl_name, sub_name = self._active_task_label()
            self._set_last_op(f"已启动: {lvl_name} > {sub_name}")
            self._add_history(
                f"[ACK] {cmd_name(cmd)}(0x{cmd:02X}) 启动 {lvl_name}>{sub_name}")
        self._refresh_status()

    def on_nack(self, cmd: int, err: int):
        """标定相关命令 NACK (主窗口在 _on_nack 中调用, 内部按 cmd 过滤)."""
        if not self._is_calib_cmd(cmd):
            return
        cn = err_name_cn(err)
        if cmd == JmCmd.CALIB_QUERY:
            if err == JmErr.CALIB_BUSY:
                self._calib_running = True
                if self._chk_auto_poll.isChecked() and self._link_active:
                    self._poll_timer.start()
                self._set_last_op("查询: 标定进行中…")
                self._add_history(
                    f"[NACK] {cmd_name(cmd)}(0x{cmd:02X}) 标定进行中 (CALIB_BUSY)")
            elif err == JmErr.STATE_DENY:
                self._calib_running = False
                self._poll_timer.stop()
                self._set_last_op("查询: 未标定 (state_deny)")
                self._add_history(
                    f"[NACK] {cmd_name(cmd)}(0x{cmd:02X}) 未标定 (STATE_DENY)")
            else:
                self._set_last_op(f"查询: 错误 {cn}")
                self._add_history(
                    f"[NACK] {cmd_name(cmd)}(0x{cmd:02X}) err={cn}(0x{err:02X})")
        else:
            self._set_last_op(f"失败: {cn}")
            self._add_history(
                f"[NACK] {cmd_name(cmd)}(0x{cmd:02X}) err={cn}(0x{err:02X})")
        self._refresh_status()

    @staticmethod
    def _is_calib_cmd(cmd: int) -> bool:
        return int(JmCmd.CALIB_LEVEL1) <= cmd <= int(JmCmd.CALIB_ABORT)

    def _active_task_label(self):
        """返回 (level_name, sub_name), 取自当前选中任务; 未选则 ("", "")。"""
        if self._active_task_key is None:
            return "", ""
        cmd, sub_id = self._active_task_key
        for level_cmd, level_name, submodes in _CALIB_LEVELS:
            if int(level_cmd) != cmd:
                continue
            for sid, sname, _ in submodes:
                if sid == sub_id:
                    return level_name, sname
        return "", ""

    # ==================== 按钮回调 ====================
    def _on_query_clicked(self):
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接, 无法查询")
            return
        self._add_history("[TX] 查询标定进度 (0x97)")
        self.send_command.emit(int(JmCmd.CALIB_QUERY), {})

    def _on_abort_clicked(self):
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接, 无法中止")
            return
        self._add_history("[TX] 中止标定 (0x98)")
        self.send_command.emit(int(JmCmd.CALIB_ABORT), {})

    def _on_auto_poll_toggled(self, on: bool):
        if on and self._calib_running and self._link_active:
            self._poll_timer.start()
        else:
            self._poll_timer.stop()

    def _on_poll_tick(self):
        if self._link_active and self._calib_running:
            self.send_command.emit(int(JmCmd.CALIB_QUERY), {})

    # ==================== 历史记录 ====================
    def _add_history(self, text: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._history.append(f"{ts}  {text}")
        self._history_view.setPlainText("\n".join(self._history))
        sb = self._history_view.verticalScrollBar()
        if sb is not None:
            sb.setValue(sb.maximum())

    def _on_clear_history(self):
        self._history.clear()
        self._history_view.setPlainText("")

    def _set_last_op(self, text: str):
        self._last_op = text
        self._lbl_last_op.setText(f"最近操作: {text}")

    # ==================== 状态显示 ====================
    def _refresh_status(self):
        if not self._link_active:
            dot_color = theme.hex("muted")
            text = "未连接"
        elif self._calib_running:
            dot_color = theme.hex("warn")
            top_text = top_fsm_name(self._current_top_fsm) \
                if self._current_top_fsm is not None else "未知"
            text = f"标定进行中  (top_fsm={top_text})"
        elif self._current_top_fsm == int(TopFsm.CALIB):
            dot_color = theme.hex("accent")
            text = "标定态就绪"
        else:
            dot_color = theme.hex("accent")
            top_text = top_fsm_name(self._current_top_fsm) \
                if self._current_top_fsm is not None else "未知"
            text = f"已连接  (top_fsm={top_text})"
        self._lbl_status_dot.setStyleSheet(
            f"color: {dot_color}; font-size: 18px; border:none;")
        self._lbl_status_text.setStyleSheet(
            f"font-family: Consolas, 'Microsoft YaHei', monospace; "
            f"font-size: 13px; font-weight: bold; color: {dot_color}; border:none;")
        self._lbl_status_text.setText(text)

    # ==================== 主题 ====================
    def apply_theme(self):
        self._status_card.setStyleSheet(f"""
            QFrame {{
                background: {theme.hex('card_bottom')};
                border: 1px solid {theme.hex('border')};
                border-radius: 4px;
            }}
        """)
        self._task_tabs.setStyleSheet(self._tab_style())
        self._lbl_active_task.setStyleSheet(
            f"color: {theme.hex('text')}; font-size: 12px; border:none;")
        self._btn_abort.setStyleSheet(
            f"background-color: {theme.hex('danger')}; color: {theme.hex('danger_text')}; "
            f"font-weight: bold; padding: 4px 10px;")
        self._history_view.setStyleSheet(
            f"background: {theme.hex('log_bg')}; color: {theme.hex('log_text')}; "
            f"font-family: Consolas, 'Microsoft YaHei', monospace; font-size: 12px; "
            f"border: 1px solid {theme.hex('border')};")
        self._refresh_task_buttons_style()
        if self._config_panel is not None:
            fn = getattr(self._config_panel, "apply_theme", None)
            if callable(fn):
                fn()
        self._refresh_status()

    # ==================== 配置持久化 ====================
    def get_opts(self) -> dict:
        """收集可持久化的 UI 配置: 当前 Tab + 当前选中任务 + 自动查询开关 + 内嵌面板列宽。"""
        opts = {}
        try:
            opts["auto_poll"] = bool(self._chk_auto_poll.isChecked())
            opts["task_tab_index"] = int(self._task_tabs.currentIndex())
            if self._active_task_key is not None:
                opts["active_task"] = list(self._active_task_key)
        except Exception:
            pass
        if self._config_panel is not None:
            try:
                opts["config_panel"] = self._config_panel.get_opts()
            except Exception:
                pass
        return opts

    def set_opts(self, opts: dict):
        """启动时套用配置 (容错)。"""
        if not isinstance(opts, dict):
            return
        if "auto_poll" in opts:
            try:
                self._chk_auto_poll.setChecked(bool(opts["auto_poll"]))
            except Exception:
                pass
        if "task_tab_index" in opts:
            try:
                idx = int(opts["task_tab_index"])
                if 0 <= idx < self._task_tabs.count():
                    self._task_tabs.setCurrentIndex(idx)
            except Exception:
                pass
        if "active_task" in opts:
            try:
                cmd, sub_id = opts["active_task"]
                lvl_name, sub_name, sub_desc = self._lookup_task(int(cmd), int(sub_id))
                if lvl_name:
                    self._set_active_task(int(cmd), int(sub_id), lvl_name, sub_name, sub_desc)
            except Exception:
                pass
        if self._config_panel is not None and isinstance(opts.get("config_panel"), dict):
            try:
                self._config_panel.set_opts(opts["config_panel"])
            except Exception:
                pass

    @staticmethod
    def _lookup_task(cmd, sub_id):
        for level_cmd, level_name, submodes in _CALIB_LEVELS:
            if int(level_cmd) != cmd:
                continue
            for sid, sname, sdesc in submodes:
                if sid == sub_id:
                    return level_name, sname, sdesc
        return "", "", ""
```

- [ ] **Step 4: 修改 main_window 传 registry 给 CalibrationPanel**

Edit `ui/main_window.py` 第 145 行。

定位:
```python
        self._calib_panel = CalibrationPanel()
```

替换为:
```python
        self._calib_panel = CalibrationPanel(self._registry)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python tools/test/test_calibration_panel.py`
Expected: `PASS=8 FAIL=0`

- [ ] **Step 6: 跑既有回归,确认无破坏**

Run: `python tools/test/test_state_sync.py`
Expected: 全部 PASS(标定面板信号契约未变)

- [ ] **Step 7: Commit**

```bash
git add ui/panels/calibration_panel.py ui/main_window.py tools/test/test_calibration_panel.py
git commit -m "feat(calib): 标定面板改为可点击任务网格, 支持子项一点即启动"
```

---

### Task 3: 内嵌标定结果 ParamPanel,展示下位机读回值与保存到 Flash

**Files:**
- Modify: `ui/panels/calibration_panel.py` (`_build_results_placeholder` 替换为真实内嵌面板 + `__init__` 创建实例)
- Test: `tools/test/test_calibration_panel.py` (扩展断言)

本任务在标定面板内嵌入 `ParamPanel(source="motor_config", groups=("MotorCalibParam",))`,展示 Index 16~42 全部标定结果,提供 单读/单写/全读/全写/保存到Flash(0xEA)。

- [ ] **Step 1: 扩展测试,加内嵌面板断言**

Edit `tools/test/test_calibration_panel.py`,在文件末尾 `print(f"\nPASS={PASS} FAIL={FAIL}")` 之前插入以下断言:

```python
# 9. config_panel 应为 ParamPanel 实例, 源为 motor_config, 仅含 MotorCalibParam 分组
from ui.panels.param_panel import ParamPanel
cp = panel.config_panel
check(cp is not None, "config_panel 不应为 None")
if cp is not None:
    check(isinstance(cp, ParamPanel), "config_panel 应为 ParamPanel 实例")
    check(cp.source() == "motor_config", "config_panel source 应为 motor_config")
    groups_seen = list(cp._group_param_ids.keys())
    check(groups_seen == ["MotorCalibParam"],
          f"config_panel 应仅含 MotorCalibParam, 实际 {groups_seen}")
    # 应含关键标定结果项
    pids = set(cp._param_specs.keys())
    check(16 in pids and 38 in pids,
          f"应含 is_calibrated(16) 与 elec_angle_bias(38), 实际 {sorted(pids)[:5]}...")
    # show_save 应为 True(保存到 Flash)
    check(cp._show_save is True, "config_panel 应启用保存按钮")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python tools/test/test_calibration_panel.py`
Expected: FAIL — `config_panel 不应为 None`(当前 `_config_panel` 仍为 None)。

- [ ] **Step 3: 在 __init__ 创建内嵌 ParamPanel 并替换占位容器**

Edit `ui/panels/calibration_panel.py` 的 `__init__`,在 `self._config_panel = None` 之后、`self._build()` 之前插入内嵌面板的创建。由于 `_build_results_placeholder` 会用到 `self._config_panel`,必须在 `_build()` 之前创建好。

定位 `__init__` 中的:
```python
        # 内嵌标定结果面板(下一任务填充, 本任务先置 None 保持属性契约)
        self._config_panel = None

        self._build()
```

替换为:
```python
        # 内嵌标定结果面板: motor_info 的 MotorCalibParam 段(Index 16~42),
        # 走 0xE6~0xEB 通道, 复用 ParamPanel 的读/写/dirty/固化逻辑。
        from ui.panels.param_panel import ParamPanel
        self._config_panel = ParamPanel(
            self._reg, title="标定结果 (下位机读回值 / 修改 / 保存)",
            source="motor_config", groups=("MotorCalibParam",),
            show_save=True, save_text="保存标定结果到Flash/EEPROM")

        self._build()
```

- [ ] **Step 4: 把占位容器替换为真实面板挂载**

Edit `ui/panels/calibration_panel.py` 的 `_build_results_placeholder`。

定位:
```python
    def _build_results_placeholder(self, parent_layout):
        """标定结果占位: 下一任务替换为内嵌 ParamPanel。"""
        self._results_container = QWidget()
        self._results_layout = QVBoxLayout(self._results_container)
        self._results_layout.setContentsMargins(0, 0, 0, 0)
        parent_layout.addWidget(self._results_container, 1)
```

替换为:
```python
    def _build_results_placeholder(self, parent_layout):
        """挂载内嵌标定结果 ParamPanel。"""
        if self._config_panel is not None:
            parent_layout.addWidget(self._config_panel, 1)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python tools/test/test_calibration_panel.py`
Expected: 全部 PASS(含新加的 config_panel 断言)

- [ ] **Step 6: 手动冒烟(可选,验证 UI 不崩)**

Run: `python main.py`(连接虚拟引擎后切到"电机标定"Tab)
Expected: 看到任务网格 + 标定结果表(含 is_calibrated/pole_pairs/phase_resistance... 共 27 行)+ "保存标定结果到Flash/EEPROM"按钮。关闭即可。

- [ ] **Step 7: Commit**

```bash
git add ui/panels/calibration_panel.py tools/test/test_calibration_panel.py
git commit -m "feat(calib): 标定面板内嵌 MotorCalibParam 结果表, 支持读回/写入/保存到Flash"
```

---

### Task 4: main_window 连接内嵌标定结果面板的读写/固化信号

**Files:**
- Modify: `ui/main_window.py:633` 附近 (`_connect_signals` 中,连接 `_calib_panel.config_panel` 的 5 个信号)
- Test: `tools/test/test_calibration_panel.py` (再加一条端到端连线断言)

`CalibrationPanel` 内嵌的 `config_panel` 是 `ParamPanel(source="motor_config")`,其 `read_param/write_param/read_params/write_params/save_all` 信号必须连到 `main_window` 既有的 `_on_param_read/_on_param_write/_on_param_read_many/_on_param_write_many/_on_config_save`。连线后,标定结果表的"读"按钮触发 `0xE6`、"写"按钮触发 `0xE7`、"保存"按钮触发 `0xEA`。

- [ ] **Step 1: 扩展测试,验证连线契约**

Edit `tools/test/test_calibration_panel.py`,在末尾断言块再补一条(模拟 main_window 的连线方式,确认 config_panel 信号可被外部连接):

```python
# 10. config_panel 的读写信号可被外部连接(模拟 main_window 连线契约)
if cp is not None:
    reads = []
    writes = []
    saves = []
    cp.read_param.connect(lambda pid: reads.append(pid))
    cp.write_param.connect(lambda pid, txt: writes.append((pid, txt)))
    cp.save_all.connect(lambda: saves.append(1))
    # 模拟 ParamPanel 内部发出信号(直接 emit)
    cp.read_param.emit(16)
    cp.write_param.emit(38, "0.05")
    cp.save_all.emit()
    check(reads == [16], f"read_param 应转发 id=16, 实际 {reads}")
    check(writes == [(38, "0.05")], f"write_param 应转发 (38,'0.05'), 实际 {writes}")
    check(saves == [1], f"save_all 应触发一次, 实际 {saves}")
```

- [ ] **Step 2: 运行测试确认通过(信号本就存在, 应直接 PASS)**

Run: `python tools/test/test_calibration_panel.py`
Expected: 全部 PASS(ParamPanel 已定义这些信号,本步验证契约不被破坏)

- [ ] **Step 3: 在 main_window _connect_signals 连接内嵌面板信号**

Edit `ui/main_window.py` 的 `_connect_signals`,定位第 633 行附近:

```python
        # 标定面板: 复用运动指令发送通道 (cmd=0x90~0x98, values={"submode":N} 或 {})
        self._calib_panel.send_command.connect(self._on_motion_command)
```

在其后追加内嵌标定结果面板的连线:

```python
        # 标定面板内嵌的标定结果 ParamPanel: 复用 motor_config 读写队列与固化
        _calib_cfg = self._calib_panel.config_panel
        if _calib_cfg is not None:
            _calib_cfg.read_param.connect(
                lambda param_id, panel=_calib_cfg: self._on_param_read(panel, param_id))
            _calib_cfg.write_param.connect(
                lambda param_id, text, panel=_calib_cfg: self._on_param_write(panel, param_id, text))
            _calib_cfg.read_params.connect(
                lambda param_ids, panel=_calib_cfg: self._on_param_read_many(panel, param_ids))
            _calib_cfg.write_params.connect(
                lambda writes, panel=_calib_cfg: self._on_param_write_many(panel, writes))
            _calib_cfg.save_all.connect(self._on_config_save)
```

> 说明:`_on_config_save` 调 `self._client.motor_info_save()`(0xEA),把整块 motor_info 写入 Flash/EEPROM。标定结果与电机配置共用同一 motor_info Flash 块,故复用同一槽函数。

- [ ] **Step 4: 端到端冒烟测试(手动,验证读回+保存链路)**

Run: `python main.py`

操作步骤:
1. 连接 → 选 "VIRTUAL" 虚拟引擎 → 连接
2. 切到 "电机标定" Tab
3. 在"标定结果"表里点某行的"读"按钮 → 状态栏/日志出现 `[RX] 电机配置 xxx(id=16) = ...`
4. 点表头分组行的"全读" → 批量读回 MotorCalibParam 段
5. 改某行"修改值"列 → 该行"写"按钮高亮 → 点"写" → 日志 `[TX] ... MOTOR_INFO_WRITE`
6. 点"保存标定结果到Flash/EEPROM" → 日志 `[TX] 保存电机配置到Flash/EEPROM (0xEA)`
7. 点任务网格中"L1 > ADC偏置" → 日志 `[TX] 启动 CALIB_LEVEL1 submode=1`

Expected: 全部步骤日志与状态正常,无异常。

- [ ] **Step 5: 回归全部相关测试**

Run:
```
python tools/test/test_param_panel_group_filter.py
python tools/test/test_param_panel_highlight.py
python tools/test/test_calibration_panel.py
python tools/test/test_state_sync.py
```
Expected: 四个脚本全部 PASS=... FAIL=0

- [ ] **Step 6: Commit**

```bash
git add ui/main_window.py tools/test/test_calibration_panel.py
git commit -m "feat(calib): 主窗口连接标定结果面板读写/固化信号, 打通 0xE6~0xEA 链路"
```

---

### Task 5: 标定状态指示器 + 设置/清除已标定按钮

**Files:**
- Modify: `ui/panels/calibration_panel.py` (状态卡加指示器+按钮 + 新方法 + set_link_active/apply_theme 联动)
- Modify: `ui/main_window.py:881` (`_on_param_result` 加 is_calibrated 同步)
- Test: `tools/test/test_calibration_panel.py` (扩展断言)

`is_calibrated`(motor_info Index 16,uint32_t,0=未校准/1=已校准)是标定总标志位。本任务在状态卡增加醒目指示器(大字+语义色)和两个一键按钮:写 1(标记已标定)/写 0(清除标志),走 0xE7 写 + 0xE6 读回确认。

- [ ] **Step 1: 扩展测试,加指示器与按钮断言**

Edit `tools/test/test_calibration_panel.py`,在末尾 `print(f"\nPASS={PASS} FAIL={FAIL}")` 之前插入:

```python
# 11. 标定状态指示器与设置/清除按钮存在
check(hasattr(panel, "_lbl_calib_flag"), "应有标定状态指示器标签")
check(hasattr(panel, "_btn_mark_calibrated"), "应有'标记为已标定'按钮")
check(hasattr(panel, "_btn_clear_calibrated"), "应有'清除标定标志'按钮")

# 12. set_calibrated_value 更新指示器显示
panel.set_calibrated_value("1")
check("已标定" in panel._lbl_calib_flag.text(),
      f"值=1 应显示已标定, 实际 '{panel._lbl_calib_flag.text()}'")
panel.set_calibrated_value("0")
check("未标定" in panel._lbl_calib_flag.text(),
      f"值=0 应显示未标定, 实际 '{panel._lbl_calib_flag.text()}'")

# 13. 点击'标记为已标定' → config_panel.write_param 收到 (16, "1")
wp_received = []
cp.write_param.connect(lambda pid, txt: wp_received.append((pid, txt)))
panel._btn_mark_calibrated.click()
check(wp_received and wp_received[0] == (16, "1"),
      f"标记按钮应发 write_param(16,'1'), 实际 {wp_received}")

# 14. 点击'清除标定标志' → config_panel.write_param 收到 (16, "0")
wp_received.clear()
panel._btn_clear_calibrated.click()
check(wp_received and wp_received[0] == (16, "0"),
      f"清除按钮应发 write_param(16,'0'), 实际 {wp_received}")

# 15. 未连接时点击 → 不发指令
panel.set_link_active(False)
wp_received.clear()
panel._btn_mark_calibrated.click()
check(len(wp_received) == 0, "未连接时点击标记按钮不应发指令")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python tools/test/test_calibration_panel.py`
Expected: FAIL — `AttributeError: 'CalibrationPanel' object has no attribute '_lbl_calib_flag'`

- [ ] **Step 3: 实现 _build_calib_flag_controls(替换占位)**

Edit `ui/panels/calibration_panel.py` 的 `_build_calib_flag_controls` 方法(Task 2 留的占位)。

定位当前占位实现:
```python
    def _build_calib_flag_controls(self, layout):
        """已标定指示器 + 设置/清除按钮占位(Task 5 实现, 这里先放空 widget 保持槽位)."""
        # Task 5 会在此插入 _lbl_calib_flag / _btn_mark_calibrated / _btn_clear_calibrated
        # 占位 widget 避免 layout 在 Task 5 之前为空
        placeholder = QWidget()
        layout.addWidget(placeholder)
```

替换为(紧凑徽章+按钮, 嵌入主信息行弹簧之后):
```python
    def _build_calib_flag_controls(self, layout):
        """已标定指示器徽章 + 设置/清除按钮(嵌入状态卡主信息行, 紧凑)."""
        self._lbl_calib_flag = QLabel("● 未读取")
        self._lbl_calib_flag.setStyleSheet(
            f"font-size: 11px; font-weight: bold; color: {theme.hex('muted')}; "
            f"border:none; padding: 1px 6px;")
        layout.addWidget(self._lbl_calib_flag)

        self._btn_mark_calibrated = QPushButton("标记")
        self._btn_mark_calibrated.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_mark_calibrated.setFixedHeight(22)
        self._btn_mark_calibrated.setStyleSheet(
            f"QPushButton {{ background-color: {theme.hex('ok')}; color: {theme.hex('ok_text')}; "
            f"border: 1px solid {theme.hex('ok')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 11px; font-weight: bold; }}"
            f"QPushButton:hover {{ opacity: 0.85; }}")
        self._btn_mark_calibrated.clicked.connect(self._on_mark_calibrated)
        layout.addWidget(self._btn_mark_calibrated)

        self._btn_clear_calibrated = QPushButton("清除")
        self._btn_clear_calibrated.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear_calibrated.setFixedHeight(22)
        self._btn_clear_calibrated.setStyleSheet(
            f"QPushButton {{ background-color: {theme.hex('danger')}; color: {theme.hex('danger_text')}; "
            f"border: 1px solid {theme.hex('danger')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 11px; font-weight: bold; }}"
            f"QPushButton:hover {{ opacity: 0.85; }}")
        self._btn_clear_calibrated.clicked.connect(self._on_clear_calibrated)
        layout.addWidget(self._btn_clear_calibrated)

        self._calib_flag_text = ""
```

- [ ] **Step 4: 加指示器更新与按钮回调方法**

Edit `ui/panels/calibration_panel.py`,在 `_set_active_task` 方法之后(即 `# ==================== 状态接收` 注释之前)插入以下方法:

```python
    # ==================== 标定标志位 (is_calibrated, Index 16) ====================
    def set_calibrated_value(self, text: str):
        """更新标定状态指示器(由 main_window 在读回 is_calibrated 后调用, 或按钮乐观更新)."""
        self._calib_flag_text = str(text).strip()
        self._refresh_calib_flag()

    def _refresh_calib_flag(self):
        text = getattr(self, "_calib_flag_text", "")
        try:
            val = int(float(text)) if text else -1
        except ValueError:
            val = -1
        if val == 1:
            color = theme.hex("ok")
            label = "✅ 已标定"
        elif val == 0:
            color = theme.hex("warn")
            label = "⚠ 未标定"
        else:
            color = theme.hex("muted")
            label = "● 未读取"
        self._lbl_calib_flag.setText(label)
        self._lbl_calib_flag.setStyleSheet(
            f"font-size: 11px; font-weight: bold; color: {color}; "
            f"border:none; padding: 1px 6px;")

    def _on_mark_calibrated(self):
        """标记为已标定: 写 is_calibrated=1 (0xE7), 乐观更新 + 延迟读回确认."""
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接, 无法设置标定标志")
            return
        self._config_panel.write_param.emit(16, "1")
        self._add_history("[TX] 标记为已标定 (写 is_calibrated=1, 0xE7)")
        self._set_last_op("已标记为已标定")
        self.set_calibrated_value("1")
        # 写入应答后延迟读回(读写队列独立, 用延迟保证写先完成)
        QTimer.singleShot(300, lambda: self._config_panel.read_param.emit(16))

    def _on_clear_calibrated(self):
        """清除标定标志: 写 is_calibrated=0 (0xE7), 乐观更新 + 延迟读回确认."""
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接, 无法清除标定标志")
            return
        self._config_panel.write_param.emit(16, "0")
        self._add_history("[TX] 清除标定标志 (写 is_calibrated=0, 0xE7)")
        self._set_last_op("已清除标定标志")
        self.set_calibrated_value("0")
        QTimer.singleShot(300, lambda: self._config_panel.read_param.emit(16))
```

- [ ] **Step 5: set_link_active 连接后自动读 + apply_theme 刷新样式**

Edit `ui/panels/calibration_panel.py` 的 `set_link_active`。

定位:
```python
    def set_link_active(self, active: bool):
        self._link_active = bool(active)
        if not active:
            self._calib_running = False
            self._poll_timer.stop()
        self._refresh_status()
```

替换为:
```python
    def set_link_active(self, active: bool):
        self._link_active = bool(active)
        if not active:
            self._calib_running = False
            self._poll_timer.stop()
            self._calib_flag_text = ""
            self._refresh_calib_flag()
        else:
            # 连接后自动读取标定标志位(is_calibrated, Index 16)
            if self._config_panel is not None:
                QTimer.singleShot(200, lambda: self._config_panel.read_param.emit(16))
        self._refresh_status()
```

Edit `ui/panels/calibration_panel.py` 的 `apply_theme`,在 `self._refresh_task_buttons_style()` 之后插入:

```python
        self._btn_mark_calibrated.setStyleSheet(
            f"QPushButton {{ background-color: {theme.hex('ok')}; color: {theme.hex('ok_text')}; "
            f"border: 1px solid {theme.hex('ok')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 11px; font-weight: bold; }}"
            f"QPushButton:hover {{ opacity: 0.85; }}")
        self._btn_clear_calibrated.setStyleSheet(
            f"QPushButton {{ background-color: {theme.hex('danger')}; color: {theme.hex('danger_text')}; "
            f"border: 1px solid {theme.hex('danger')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 11px; font-weight: bold; }}"
            f"QPushButton:hover {{ opacity: 0.85; }}")
        self._refresh_calib_flag()
```

- [ ] **Step 6: main_window 在读回 is_calibrated 时同步指示器**

Edit `ui/main_window.py` 的 `_on_param_result`(约第 881 行),在 `self._pump_param_read_queue()` 之前插入 is_calibrated 同步。

定位:
```python
        if spec is not None and val is not None and spec.code_name in self._MOTOR_PARAM_KEYS:
            self._motor_params[spec.code_name] = val
            self._feedback_panel.set_motor_params(self._motor_params)
        self._pump_param_read_queue()
```

替换为:
```python
        if spec is not None and val is not None and spec.code_name in self._MOTOR_PARAM_KEYS:
            self._motor_params[spec.code_name] = val
            self._feedback_panel.set_motor_params(self._motor_params)
        # is_calibrated (Index 16) 同步到标定面板的状态指示器
        if panel is self._calib_panel.config_panel and int(param_id) == 16:
            self._calib_panel.set_calibrated_value(disp)
        self._pump_param_read_queue()
```

- [ ] **Step 7: 运行测试确认通过**

Run: `python tools/test/test_calibration_panel.py`
Expected: 全部 PASS(含 Step 1 新加的 11~15 条断言)

- [ ] **Step 8: 端到端冒烟(手动)**

Run: `python main.py`

操作步骤:
1. 连接虚拟引擎 → 切到"电机标定"Tab
2. 连接后 ~200ms 状态卡指示器自动显示"✅ 已标定"或"⚠ 未标定"(取决于下位机当前值)
3. 点"清除标定标志" → 指示器立即变橙"⚠ 未标定" → 300ms 后读回确认
4. 点"标记为已标定" → 指示器立即变绿"✅ 已标定" → 300ms 后读回确认
5. 标定结果表里 is_calibrated 行(Index 16)的"当前值"列同步更新

Expected: 指示器颜色与表格值一致,无异常。

- [ ] **Step 9: 回归全部测试**

Run:
```
python tools/test/test_param_panel_group_filter.py
python tools/test/test_param_panel_highlight.py
python tools/test/test_calibration_panel.py
python tools/test/test_state_sync.py
```
Expected: 四个脚本全部 PASS=... FAIL=0

- [ ] **Step 10: Commit**

```bash
git add ui/panels/calibration_panel.py ui/main_window.py tools/test/test_calibration_panel.py
git commit -m "feat(calib): 状态卡增加已标定指示器与设置/清除按钮, 连接后自动读回"
```

---

## Self-Review

**1. Spec coverage(对照用户诉求):**
- "将不同的标定按类型做成可以直接点击功能" → Task 2 `_build_task_launcher`:L1~L7 做成顶部 **QTabWidget**,每级一个 Tab,选中 Tab 才显示该级子项卡片,点击卡片即启动;状态卡的"当前选中任务"行同步显示选中级别+子项+CMD+submode+描述。✅
- "标定任务里弄做成顶部tab,选中tab才看到对应的标定子项" → Task 2 `_build_task_launcher` 用 `QTabWidget`,每 Tab 一个 page,page 内横向排列该级子项卡片;`get_opts/set_opts` 持久化 `task_tab_index`。✅
- "标定操作嵌入到状态卡中" → Task 2 `_build_status_card` 第 0 行右侧嵌入 查询进度/中止标定/自动查询开关 三个控件,删除独立 `_build_ops`。✅
- "显示各种模式的标定值和下位机的值" → Task 3 内嵌 `ParamPanel(source="motor_config", groups=("MotorCalibParam",))`,"当前值"列即下位机 0xE6 读回值,"修改值"列为待写入值,覆盖 Index 16~42 全部标定结果。✅
- "也可以设置存储参数到flash或者EEprom" → Task 3 `show_save=True` + Task 4 连 `save_all → _on_config_save → motor_info_save(0xEA)`。✅
- "更加直观展示标定功能和结果" → Task 2 状态卡增加"当前选中任务"行 + 任务卡片高亮 + 描述;Task 3 结果表带单位/类型/描述列与 dirty 高亮。✅
- "可以设置或者清除已标定的按钮" → Task 5 `_btn_mark_calibrated`/`_btn_clear_calibrated` 一键写 is_calibrated(Index 16)=1/0,走 0xE7。✅
- "在标定界面可以很明显看到是否已标定状态的指示" → Task 5 状态卡 `_lbl_calib_flag` 14px 大字+语义色(绿 ✅已标定 / 橙 ⚠未标定 / 灰 未读取),连接后 200ms 自动读回,写后 300ms 读回确认。✅

**2. Placeholder scan:** 全部步骤含完整代码或确切命令,无 TBD/TODO/"类似上文"。✅

**3. Type consistency:** `config_panel` 在 Task 2 定义为 `self._config_panel`,通过 `@property config_panel` 暴露,Task 3 创建实例,Task 4 用 `self._calib_panel.config_panel` 读取——名称一致。`_task_buttons` 键为 `(int(cmd), int(sub_id))`,Task 2 测试与实现一致。`_CALIB_LEVELS` 元组结构 `(cmd, level_name, [(sub_id, sub_name, sub_desc)...])` 在原文件与新文件、测试中完全一致。`groups=("MotorCalibParam",)` 与 motor_info.csv 的 `Category` 列值精确匹配(已用 Grep 验证)。✅

**4. 回归风险:** Task 1 的 `groups` 默认 None = 不过滤,既有 `电机参数`/`电机配置` 面板零影响(已加 test_param_panel_highlight 回归步骤)。Task 2 保留 `send_command/set_link_active/update_state/on_ack/on_nack/apply_theme/get_opts/set_opts` 全部公共契约,`_on_motion_command` 连线不变。✅
