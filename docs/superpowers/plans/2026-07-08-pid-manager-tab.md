# PID 整定 Tab 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在上位机新增 "PID 整定" Tab，实现 0x9A 理论估计、0x9B 来源切换命令的收发，以及 ControlParam 段 PID 参数的读写，风格对标"电机标定" Tab。

**Architecture:** 新建 `PidPanel(QGroupBox)` 顶级面板，复用标定面板的三段式布局（状态卡 + 操作区 + 结果/历史 Splitter）。操作区左侧为理论估计面板（环选择 + 带宽输入 + 开始按钮），右侧为来源切换面板（三环独立 source 单选组 + 应用按钮）。结果区复用 `ParamPanel(source="motor_config", groups=("ControlParam",))` 展示/编辑 Flash 中的 PID 工程值。面板通过专用信号 `pid_autotune_requested` / `pid_source_set_requested` 与主窗口解耦，主窗口转发到 `JmClient.pid_autotune()` / `pid_source_set()`。

**Tech Stack:** PyQt6, Python 3.10+, 项目已有 `theme` 单例、`JmClient` 信号机制、`ParamPanel` 可复用组件。

---

## 文件结构

| 文件 | 职责 | 动作 |
|------|------|------|
| `ui/panels/pid_panel.py` | PID 整定面板主类 `PidPanel(QGroupBox)` | **新建** |
| `tools/test/test_pid_panel.py` | 面板实例化/布局结构冒烟测试 | **新建** |
| `ui/main_window.py` | 注册 Tab + 连接信号 + 转发应答 | **修改** |

### 设计决策

1. **参数通道选择**: ControlParam 段（motor_info 通道 0xE6/0xE7/0xEA），param_id 64-85。原因：0x9A autotune 写入 ControlParam，0x9B source=1/2 从 ControlParam 读取，0xE7 可编辑工程调试值，0xEA 固化 Flash。runtime motor_param (0xE0) 不直接展示（它是 source 路由后的"生效值"，非编辑入口）。

2. **信号设计**: 不复用标定面板的 `send_command(int, dict)` 通用信号，改用两个专用信号 `pid_autotune_requested(int, float, float, float)` 和 `pid_source_set_requested(int, int)`，主窗口直接连到 `client.pid_autotune()` / `client.pid_source_set()`。原因：0x9A/0x9B 有专用方法（已 struct.pack），不走 registry 通用打包。

3. **source 状态本地维护**: 协议无 source 查询命令，面板本地维护三环 source 状态（初始 DEFAULT=0），0x9B 成功后更新对应环，0x9A 成功后更新已整定环为 AUTOTUNE=2。重连时重置为 DEFAULT。

4. **IDLE 态守卫**: 仿标定面板 `update_state` 缓存 `top_fsm`，按钮回调检查 `== int(TopFsm.IDLE)`（即 == 3），非 IDLE 态提示并阻止发送。

5. **ParamPanel 复用**: 仿标定面板 `attach_results_panel` 模式，主窗口创建 `ParamPanel(source="motor_config", groups=("ControlParam",))` 并注入 PID 面板，信号路由复用 `_on_param_read/write/read_many/write_many` + `_on_config_save`。

---

## Task 1: PidPanel 骨架 + 状态卡

**Files:**
- Create: `ui/panels/pid_panel.py`
- Test: `tools/test/test_pid_panel.py`

- [ ] **Step 1: 编写冒烟测试（验证面板可实例化 + 基本结构）**

```python
"""PID 整定面板冒烟测试: 验证实例化/布局结构/公共 API。"""
import sys
from pathlib import Path

# 把项目根目录加入 sys.path (与 test_calibration_panel_layout.py 同模式)
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtWidgets import QApplication  # 或 PyQt6, 按项目实际
# 若项目用 PyQt6:
# from PyQt6.QtWidgets import QApplication, QGroupBox

from ui.panels.pid_panel import PidPanel


def test_pid_panel_instantiates():
    """PidPanel 可被实例化, 且是 QGroupBox 子类。"""
    app = QApplication.instance() or QApplication(sys.argv)
    from jmproto.registry import CommandRegistry
    reg = CommandRegistry()
    panel = PidPanel(registry=reg)
    assert isinstance(panel, QGroupBox)
    assert panel.title() == "PID 整定"


def test_pid_panel_has_public_api():
    """PidPanel 暴露主窗口所需的公共 API。"""
    app = QApplication.instance() or QApplication(sys.argv)
    from jmproto.registry import CommandRegistry
    reg = CommandRegistry()
    panel = PidPanel(registry=reg)
    for method in ('set_link_active', 'update_state', 'on_autotune_result',
                   'on_ack', 'on_nack', 'attach_params_panel',
                   'apply_theme', 'get_opts', 'set_opts'):
        assert hasattr(panel, method), f"缺少公共方法: {method}"


def test_pid_panel_has_signals():
    """PidPanel 定义了两个专用请求信号。"""
    app = QApplication.instance() or QApplication(sys.argv)
    from jmproto.registry import CommandRegistry
    reg = CommandRegistry()
    panel = PidPanel(registry=reg)
    assert hasattr(panel, 'pid_autotune_requested')
    assert hasattr(panel, 'pid_source_set_requested')
```

> **注意**: 先确认项目用 PyQt6 还是 PySide6。检查 `requirements.txt` 或现有 test 文件的 import。若 PyQt6，将 `from PySide6...` 改为 `from PyQt6...`，QGroupBox 从 `PyQt6.QtWidgets` 导入。

- [ ] **Step 2: 运行测试确认失败（模块不存在）**

Run: `cd d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui && python -m pytest tools/test/test_pid_panel.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ui.panels.pid_panel'`

- [ ] **Step 3: 确认项目的 Qt 绑定**

Run: `cd d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui && python -c "import PyQt6; print('PyQt6')" 2>nul || python -c "import PySide6; print('PySide6')"`
Expected: 输出 `PyQt6`（根据项目 requirements.txt 确认）

- [ ] **Step 4: 实现 PidPanel 骨架（含信号 + 状态卡）**

```python
"""
PID 整定面板 (顶级 Tab)。

对标"电机标定" Tab 的三段式布局:
  1. 状态卡 — 连接状态 / 三环 source 徽章 / 最近操作
  2. 操作区 — 左:理论估计(0x9A) 右:来源切换(0x9B)
  3. 结果/历史 Splitter — ControlParam 参数表 + 操作历史

信号:
  pid_autotune_requested(ring_select, cur_bw, vel_bw, pos_bw) — 主窗口连 client.pid_autotune
  pid_source_set_requested(ring_select, source)              — 主窗口连 client.pid_source_set

IDLE 态守卫: 0x9A/0x9B 仅 TOP_FSM_IDLE(=3) 可执行。
"""
from collections import deque
from datetime import datetime
from enum import IntEnum

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
    QPushButton, QSplitter, QTextEdit, QCheckBox, QDoubleSpinBox,
    QButtonGroup, QRadioButton, QSizePolicy,
)
from PyQt6.QtCore import Qt

from jmproto.cmd_def import JmCmd, TopFsm
from ui.theme import theme


# ---- 三环 source 枚举 (与固件 motor_pid_load.h pid_source_e 一致) ----
class PidSource(IntEnum):
    DEFAULT = 0    # motor_param.c 默认值
    FLASH = 1      # Flash ControlParam 工程值
    AUTOTUNE = 2   # 理论估计值


# ---- 三环标识 (与固件 pid_ring_e 一致) ----
class PidRing(IntEnum):
    CURRENT = 0
    VELOCITY = 1
    POSITION = 2


_RING_CN = {PidRing.CURRENT: "电流环", PidRing.VELOCITY: "速度环", PidRing.POSITION: "位置环"}
_SOURCE_CN = {PidSource.DEFAULT: "默认", PidSource.FLASH: "Flash", PidSource.AUTOTUNE: "理论估计"}
_SOURCE_COLOR_KEY = {PidSource.DEFAULT: "muted", PidSource.FLASH: "warn", PidSource.AUTOTUNE: "accent"}


class PidPanel(QGroupBox):
    """PID 整定面板 (顶级 Tab)。"""

    # ---- 请求信号 (主窗口连接到 JmClient) ----
    pid_autotune_requested = pyqtSignal(int, float, float, float)  # ring_select, cur_bw, vel_bw, pos_bw
    pid_source_set_requested = pyqtSignal(int, int)                # ring_select, source

    _HISTORY_MAX = 200

    def __init__(self, registry=None, parent=None):
        super().__init__("PID 整定", parent)
        self._registry = registry
        self._link_active = False
        self._current_top_fsm = None
        # 三环 source 状态 (本地维护, 协议无查询命令)
        self._ring_source = {r: PidSource.DEFAULT for r in PidRing}
        # 操作历史
        self._history = deque(maxlen=self._HISTORY_MAX)
        # 内嵌参数面板引用 (由主窗口 attach_params_panel 注入)
        self._params_panel = None
        # 构建UI
        self._build()

    # ========================================================================
    #  UI 构建
    # ========================================================================
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self._build_status_card(layout)
        # 占位: 操作区和结果区在后续 Task 补充
        self._params_split = None
        self._history_view = None
        self._last_op_label = None

    def _build_status_card(self, parent_layout):
        """状态卡: 左(连接状态 + 最近操作) 右(三环 source 徽章)。"""
        card = QFrame()
        card.setObjectName("statusCard")
        h = QHBoxLayout(card)
        h.setContentsMargins(12, 8, 12, 8)
        h.setSpacing(12)

        # 左: 连接状态 + 最近操作
        left = QVBoxLayout()
        left.setSpacing(2)
        self._status_label = QLabel("未连接")
        self._status_label.setStyleSheet(
            f"font-weight:bold; color:{theme.hex('text_strong')};")
        self._last_op_label = QLabel("就绪")
        self._last_op_label.setStyleSheet(f"color:{theme.hex('muted')};")
        left.addWidget(self._status_label)
        left.addWidget(self._last_op_label)
        h.addLayout(left, 1)

        # 右: 三环 source 徽章
        right = QHBoxLayout()
        right.setSpacing(8)
        self._source_labels = {}
        for ring in PidRing:
            vbox = QVBoxLayout()
            vbox.setSpacing(0)
            name_lbl = QLabel(_RING_CN[ring])
            name_lbl.setStyleSheet(
                f"color:{theme.hex('muted')}; font-size:11px;")
            name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            src_lbl = QLabel(_SOURCE_CN[PidSource.DEFAULT])
            src_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            src_lbl.setStyleSheet(self._source_badge_qss(PidSource.DEFAULT))
            vbox.addWidget(name_lbl)
            vbox.addWidget(src_lbl)
            right.addLayout(vbox)
            self._source_labels[ring] = src_lbl
        h.addLayout(right, 0)

        parent_layout.addWidget(card)

    def _source_badge_qss(self, source: PidSource) -> str:
        """source 徽章 QSS: 按来源类型着色。"""
        color_key = _SOURCE_COLOR_KEY.get(source, "muted")
        bg = theme.hex(color_key)
        return (f"background:{bg}; color:{theme.hex('text_strong')};"
                f"padding:2px 8px; border-radius:8px; font-size:11px; font-weight:bold;")

    # ========================================================================
    #  公共 API (主窗口调用)
    # ========================================================================
    def set_link_active(self, active: bool):
        self._link_active = active
        if not active:
            self._ring_source = {r: PidSource.DEFAULT for r in PidRing}
            self._refresh_source_badges()
        self._refresh_status()

    def update_state(self, top_fsm: int, run_state: int, ctrl_mode: int, enable: int):
        self._current_top_fsm = top_fsm
        self._refresh_status()

    def on_autotune_result(self, result: dict):
        """0x9A PID_AUTOTUNE 应答 (8字节 ACK)。"""
        pass  # Task 3 实现

    def on_ack(self, cmd: int):
        """0x9B PID_SOURCE_SET 成功 ACK。"""
        pass  # Task 3 实现

    def on_nack(self, cmd: int, err: int):
        """0x9B PID_SOURCE_SET 失败 NACK。"""
        pass  # Task 3 实现

    def attach_params_panel(self, panel):
        """注入 ParamPanel 实例 (由主窗口创建, source='motor_config', groups=('ControlParam',))。"""
        self._params_panel = panel  # Task 4 完善: 放入 Splitter

    def apply_theme(self):
        self._status_label.setStyleSheet(
            f"font-weight:bold; color:{theme.hex('text_strong')};")
        self._last_op_label.setStyleSheet(f"color:{theme.hex('muted')};")
        self._refresh_source_badges()

    def get_opts(self) -> dict:
        return {}

    def set_opts(self, opts: dict):
        pass

    # ========================================================================
    #  内部辅助
    # ========================================================================
    def _refresh_status(self):
        if not self._link_active:
            self._status_label.setText("未连接")
            self._status_label.setStyleSheet(
                f"font-weight:bold; color:{theme.hex('danger')};")
            return
        if self._current_top_fsm == int(TopFsm.IDLE):
            self._status_label.setText("已连接 · IDLE 就绪")
            self._status_label.setStyleSheet(
                f"font-weight:bold; color:{theme.hex('ok')};")
        else:
            self._status_label.setText(
                f"已连接 · top_fsm={self._current_top_fsm}")
            self._status_label.setStyleSheet(
                f"font-weight:bold; color:{theme.hex('warn')};")

    def _refresh_source_badges(self):
        for ring, lbl in self._source_labels.items():
            src = self._ring_source[ring]
            lbl.setText(_SOURCE_CN[src])
            lbl.setStyleSheet(self._source_badge_qss(src))

    def _set_last_op(self, text: str):
        if self._last_op_label:
            self._last_op_label.setText(text)

    def _add_history(self, text: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._history.append(f"{ts}  {text}")
        if self._history_view:
            self._history_view.setPlainText("\n".join(self._history))
            sb = self._history_view.verticalScrollBar()
            if sb:
                sb.setValue(sb.maximum())
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui && python -m pytest tools/test/test_pid_panel.py -v`
Expected: 3 tests PASS

- [ ] **Step 6: Commit**

```bash
git add ui/panels/pid_panel.py tools/test/test_pid_panel.py
git commit -m "feat(gui): add PidPanel skeleton with status card and source badges"
```

---

## Task 2: 理论估计操作面板 (0x9A)

**Files:**
- Modify: `ui/panels/pid_panel.py` — `_build_operation_panel` 方法 + `_on_autotune_clicked` 回调

- [ ] **Step 1: 编写测试（验证操作面板控件存在）**

在 `tools/test/test_pid_panel.py` 末尾追加:

```python
def test_pid_panel_has_autotune_controls():
    """面板含理论估计操作控件: 环选择/带宽输入/开始按钮。"""
    app = QApplication.instance() or QApplication(sys.argv)
    from jmproto.registry import CommandRegistry
    reg = CommandRegistry()
    panel = PidPanel(registry=reg)
    assert hasattr(panel, '_autotune_ring_checks')
    assert hasattr(panel, '_cur_bw_spin')
    assert hasattr(panel, '_vel_bw_spin')
    assert hasattr(panel, '_pos_bw_spin')
    assert hasattr(panel, '_autotune_btn')


def test_pid_panel_autotune_emits_signal():
    """点击开始理论估计按钮, 发出 pid_autotune_requested 信号。"""
    app = QApplication.instance() or QApplication(sys.argv)
    from jmproto.registry import CommandRegistry
    reg = CommandRegistry()
    panel = PidPanel(registry=reg)
    received = []
    panel.pid_autotune_requested.connect(
        lambda r, c, v, p: received.append((r, c, v, p)))
    # 勾选全部三环
    for chk in panel._autotune_ring_checks.values():
        chk.setChecked(True)
    panel._cur_bw_spin.setValue(1000.0)
    panel._on_autotune_clicked()
    assert len(received) == 1
    assert received[0][0] == 3  # ring_select=3 全部
    assert received[0][1] == 1000.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui && python -m pytest tools/test/test_pid_panel.py -v -k autotune`
Expected: FAIL (属性不存在)

- [ ] **Step 3: 实现理论估计操作面板**

在 `PidPanel._build` 方法中，`_build_status_card` 之后添加 `self._build_operation_panel(layout)` 调用，并实现该方法:

```python
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self._build_status_card(layout)
        self._build_operation_panel(layout)       # <-- 新增
        # Task 4 补充: self._build_results_history_split(layout)
        self._params_split = None
        self._history_view = None
```

新增方法（插入到 `_build_status_card` 之后）:

```python
    def _build_operation_panel(self, parent_layout):
        """操作区: 左(理论估计 0x9A) 右(来源切换 0x9B)。"""
        box = QGroupBox("PID 操作")
        h = QHBoxLayout(box)
        h.setContentsMargins(8, 8, 8, 8)
        h.setSpacing(8)

        # ---- 左: 理论估计 ----
        autotune_box = QGroupBox("理论估计 (0x9A)")
        av = QVBoxLayout(autotune_box)
        av.setSpacing(6)

        # 环选择
        ring_row = QHBoxLayout()
        ring_row.addWidget(QLabel("整定环:"))
        self._autotune_ring_checks = {}
        for ring in PidRing:
            chk = QCheckBox(_RING_CN[ring])
            chk.setChecked(True)
            self._autotune_ring_checks[ring] = chk
            ring_row.addWidget(chk)
        ring_row.addStretch()
        av.addLayout(ring_row)

        # 带宽输入
        bw_row = QHBoxLayout()
        bw_row.addWidget(QLabel("电流环带宽(Hz):"))
        self._cur_bw_spin = QDoubleSpinBox()
        self._cur_bw_spin.setRange(0.0, 10000.0)
        self._cur_bw_spin.setValue(0.0)
        self._cur_bw_spin.setSpecialValueText("默认(1000)")
        bw_row.addWidget(self._cur_bw_spin)
        bw_row.addWidget(QLabel("速度环(Hz):"))
        self._vel_bw_spin = QDoubleSpinBox()
        self._vel_bw_spin.setRange(0.0, 5000.0)
        self._vel_bw_spin.setValue(0.0)
        self._vel_bw_spin.setSpecialValueText("默认(100)")
        bw_row.addWidget(self._vel_bw_spin)
        bw_row.addWidget(QLabel("位置环(Hz):"))
        self._pos_bw_spin = QDoubleSpinBox()
        self._pos_bw_spin.setRange(0.0, 1000.0)
        self._pos_bw_spin.setValue(0.0)
        self._pos_bw_spin.setSpecialValueText("默认(20)")
        bw_row.addWidget(self._pos_bw_spin)
        av.addLayout(bw_row)

        # 开始按钮
        self._autotune_btn = QPushButton("开始理论估计")
        self._autotune_btn.setStyleSheet(self._ok_btn_qss())
        self._autotune_btn.clicked.connect(self._on_autotune_clicked)
        av.addWidget(self._autotune_btn)

        h.addWidget(autotune_box, 1)

        # ---- 右: 来源切换 (Task 3 补充) ----
        self._source_box = None  # 占位, Task 3 填充
        self._source_btn_groups = {}
        self._source_apply_btn = None

        parent_layout.addWidget(box)

    def _ok_btn_qss(self) -> str:
        return (f"QPushButton{{background:{theme.hex('ok')};"
                f"color:{theme.hex('ok_text')};"
                f"border:none;padding:6px 12px;border-radius:4px;font-weight:bold;}}"
                f"QPushButton:hover{{background:{theme.hex('accent')};}}")

    def _on_autotune_clicked(self):
        """开始理论估计按钮回调。"""
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接, 无法整定")
            return
        if self._current_top_fsm != int(TopFsm.IDLE):
            self._add_history("[状态拒绝] 仅 IDLE 态可执行理论估计")
            self._set_last_op("需切换到 IDLE 态")
            return
        # 计算 ring_select
        ring_select = 0
        for ring, chk in self._autotune_ring_checks.items():
            if chk.isChecked():
                ring_select |= (1 << int(ring))
        # 三环全选 = 3 (与固件约定: 0=电流 1=速度 2=位置 3=全部)
        if ring_select == 0b111:
            ring_select = 3
        if ring_select == 0:
            self._add_history("[参数错误] 请至少选择一个环")
            self._set_last_op("未选择整定环")
            return
        cur_bw = self._cur_bw_spin.value()
        vel_bw = self._vel_bw_spin.value()
        pos_bw = self._pos_bw_spin.value()
        rings_cn = "/".join(_RING_CN[r] for r, c in self._autotune_ring_checks.items() if c.isChecked())
        self._add_history(f"[TX] 理论估计 {rings_cn} 带宽=({cur_bw},{vel_bw},{pos_bw})Hz")
        self._set_last_op(f"理论估计中... ({rings_cn})")
        self.pid_autotune_requested.emit(ring_select, cur_bw, vel_bw, pos_bw)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui && python -m pytest tools/test/test_pid_panel.py -v -k autotune`
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ui/panels/pid_panel.py tools/test/test_pid_panel.py
git commit -m "feat(gui): add autotune operation panel with ring select and bandwidth inputs"
```

---

## Task 3: 来源切换面板 (0x9B) + 应答处理

**Files:**
- Modify: `ui/panels/pid_panel.py` — `_build_operation_panel` 右侧 + `on_autotune_result` / `on_ack` / `on_nack` + `_on_source_apply_clicked`

- [ ] **Step 1: 编写测试**

在 `tools/test/test_pid_panel.py` 末尾追加:

```python
def test_pid_panel_has_source_switch_controls():
    """面板含来源切换控件: 三环单选组 + 应用按钮。"""
    app = QApplication.instance() or QApplication(sys.argv)
    from jmproto.registry import CommandRegistry
    reg = CommandRegistry()
    panel = PidPanel(registry=reg)
    assert hasattr(panel, '_source_btn_groups')
    assert hasattr(panel, '_source_apply_btn')
    assert len(panel._source_btn_groups) == 3  # 三个环


def test_pid_panel_source_apply_emits_signal():
    """点击应用来源切换, 发出 pid_source_set_requested 信号。"""
    app = QApplication.instance() or QApplication(sys.argv)
    from jmproto.registry import CommandRegistry
    reg = CommandRegistry()
    panel = PidPanel(registry=reg)
    received = []
    panel.pid_source_set_requested.connect(
        lambda r, s: received.append((r, s)))
    # 电流环选 Flash(1)
    panel._source_btn_groups[0].button(1).setChecked(True)
    panel._on_source_apply_clicked()
    assert len(received) >= 1
    assert (0, 1) in received


def test_pid_panel_autotune_result_updates_source():
    """0x9A 成功应答后, 对应环 source 更新为 AUTOTUNE。"""
    app = QApplication.instance() or QApplication(sys.argv)
    from jmproto.registry import CommandRegistry
    reg = CommandRegistry()
    panel = PidPanel(registry=reg)
    # 模拟全部三环整定成功
    panel.on_autotune_result({
        'ok': True, 'status': 0, 'fail_reason': 0,
        'ring_select_done': 3, 'status_cn': '成功',
        'fail_reason_cn': '无',
    })
    assert panel._ring_source[0] == 2  # AUTOTUNE
    assert panel._ring_source[1] == 2
    assert panel._ring_source[2] == 2


def test_pid_panel_autotune_result_failed():
    """0x9A 失败应答显示失败原因, source 不变。"""
    app = QApplication.instance() or QApplication(sys.argv)
    from jmproto.registry import CommandRegistry
    reg = CommandRegistry()
    panel = PidPanel(registry=reg)
    old_source = dict(panel._ring_source)
    panel.on_autotune_result({
        'ok': False, 'status': 1, 'fail_reason': 1,
        'ring_select_done': 0, 'status_cn': '失败',
        'fail_reason_cn': '辨识未就绪',
    })
    assert panel._ring_source == old_source  # 未改变
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui && python -m pytest tools/test/test_pid_panel.py -v -k "source or autotune_result"`
Expected: FAIL

- [ ] **Step 3: 实现来源切换面板**

在 `_build_operation_panel` 方法中，将右侧占位替换为实际来源切换面板:

```python
        # ---- 右: 来源切换 (0x9B) ----
        source_box = QGroupBox("来源切换 (0x9B)")
        sv = QVBoxLayout(source_box)
        sv.setSpacing(6)

        self._source_btn_groups = {}
        for ring in PidRing:
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{_RING_CN[ring]}:"))
            grp = QButtonGroup(self)
            grp.setExclusive(True)
            for src in PidSource:
                rb = QRadioButton(_SOURCE_CN[src])
                rb.setProperty("source", int(src))
                grp.addButton(rb, int(src))
                if src == PidSource.DEFAULT:
                    rb.setChecked(True)
                row.addWidget(rb)
            self._source_btn_groups[int(ring)] = grp
            row.addStretch()
            sv.addLayout(row)

        self._source_apply_btn = QPushButton("应用来源切换")
        self._source_apply_btn.setStyleSheet(self._normal_btn_qss())
        self._source_apply_btn.clicked.connect(self._on_source_apply_clicked)
        sv.addWidget(self._source_apply_btn)

        h.addWidget(source_box, 1)
        self._source_box = source_box
```

新增辅助方法和回调:

```python
    def _normal_btn_qss(self) -> str:
        return (f"QPushButton{{background:{theme.hex('input_bg')};"
                f"color:{theme.hex('text')};"
                f"border:1px solid {theme.hex('btn_border')};"
                f"padding:6px 12px;border-radius:4px;}}"
                f"QPushButton:hover{{border-color:{theme.hex('accent')};}}")

    def _on_source_apply_clicked(self):
        """应用来源切换按钮回调: 逐环发送 0x9B。"""
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接")
            return
        if self._current_top_fsm != int(TopFsm.IDLE):
            self._add_history("[状态拒绝] 仅 IDLE 态可切换来源")
            self._set_last_op("需切换到 IDLE 态")
            return
        sent = 0
        for ring_id, grp in self._source_btn_groups.items():
            src = grp.checkedId()
            if src < 0:
                continue
            self._add_history(
                f"[TX] 来源切换 {_RING_CN[PidRing(ring_id)]} -> {_SOURCE_CN[PidSource(src)]}")
            self.pid_source_set_requested.emit(ring_id, src)
            sent += 1
        if sent > 0:
            self._set_last_op(f"已发送 {sent} 个来源切换请求")
        else:
            self._set_last_op("无来源变更")
```

- [ ] **Step 4: 实现应答处理方法**

替换 `on_autotune_result` / `on_ack` / `on_nack` 的 `pass` 实现:

```python
    def on_autotune_result(self, result: dict):
        """0x9A PID_AUTOTUNE 应答 (8字节 ACK)。"""
        ok = bool(result.get('ok', False))
        fail_cn = result.get('fail_reason_cn', '未知')
        ring_done = int(result.get('ring_select_done', 0))
        if ok:
            rings_cn = []
            if ring_done in (0, 3):  # 0=电流 或 3=全部
                self._ring_source[PidRing.CURRENT] = PidSource.AUTOTUNE
                rings_cn.append(_RING_CN[PidRing.CURRENT])
            if ring_done in (1, 3):
                self._ring_source[PidRing.VELOCITY] = PidSource.AUTOTUNE
                rings_cn.append(_RING_CN[PidRing.VELOCITY])
            if ring_done in (2, 3):
                self._ring_source[PidRing.POSITION] = PidSource.AUTOTUNE
                rings_cn.append(_RING_CN[PidRing.POSITION])
            self._refresh_source_badges()
            self._add_history(f"[RX] 理论估计成功 ({'/'.join(rings_cn)})")
            self._set_last_op(f"理论估计完成 ({'/'.join(rings_cn)})")
            # 自动刷新参数表 (读回 autotune 写入的 ControlParam)
            if self._params_panel:
                try:
                    self._params_panel.read_all()
                except Exception:
                    pass
        else:
            self._add_history(f"[RX] 理论估计失败: {fail_cn}")
            self._set_last_op(f"理论估计失败: {fail_cn}")

    def on_ack(self, cmd: int):
        """0x9B PID_SOURCE_SET 成功 ACK。"""
        if cmd == int(JmCmd.PID_SOURCE_SET):
            self._add_history("[RX] 来源切换成功")
            self._set_last_op("来源切换完成")
            # 更新本地 source 状态 (从单选组读取)
            for ring_id, grp in self._source_btn_groups.items():
                src = grp.checkedId()
                if src >= 0:
                    self._ring_source[PidRing(ring_id)] = PidSource(src)
            self._refresh_source_badges()

    def on_nack(self, cmd: int, err: int):
        """0x9B PID_SOURCE_SET 失败 NACK。"""
        if cmd == int(JmCmd.PID_SOURCE_SET):
            self._add_history(f"[RX] 来源切换失败 err=0x{err:02X}")
            self._set_last_op(f"来源切换失败 (0x{err:02X})")
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui && python -m pytest tools/test/test_pid_panel.py -v`
Expected: ALL tests PASS (8 tests)

- [ ] **Step 6: Commit**

```bash
git add ui/panels/pid_panel.py tools/test/test_pid_panel.py
git commit -m "feat(gui): add source switch panel and autotune/ack/nack handlers"
```

---

## Task 4: 结果/历史 Splitter + 参数面板注入

**Files:**
- Modify: `ui/panels/pid_panel.py` — `_build_results_history_split` + 完善 `attach_params_panel`

- [ ] **Step 1: 编写测试**

在 `tools/test/test_pid_panel.py` 末尾追加:

```python
def test_pid_panel_has_results_history_split():
    """面板含结果/历史 Splitter 和历史视图。"""
    app = QApplication.instance() or QApplication(sys.argv)
    from jmproto.registry import CommandRegistry
    reg = CommandRegistry()
    panel = PidPanel(registry=reg)
    assert hasattr(panel, '_params_split')
    assert panel._params_split is not None
    assert hasattr(panel, '_history_view')
    assert panel._history_view is not None


def test_pid_panel_attach_params_panel():
    """attach_params_panel 注入后, ParamPanel 被放入 Splitter。"""
    app = QApplication.instance() or QApplication(sys.argv)
    from jmproto.registry import CommandRegistry
    from ui.panels.param_panel import ParamPanel
    reg = CommandRegistry()
    panel = PidPanel(registry=reg)
    pp = ParamPanel(reg, title="PID 参数", source="motor_config",
                    groups=("ControlParam",), show_save=True, show_bulk_rw=True)
    panel.attach_params_panel(pp)
    assert panel._params_panel is pp
    # Splitter 应包含 ParamPanel
    assert panel._params_split.indexOf(pp) >= 0


def test_pid_panel_history_toggle():
    """历史显隐开关可切换 history_view 可见性。"""
    app = QApplication.instance() or QApplication(sys.argv)
    from jmproto.registry import CommandRegistry
    reg = CommandRegistry()
    panel = PidPanel(registry=reg)
    initial = panel._history_view.isVisible()
    panel.set_history_visible(not initial)
    # 仅验证不抛异常
    assert hasattr(panel, 'set_history_visible')
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui && python -m pytest tools/test/test_pid_panel.py -v -k "split or attach or history_toggle"`
Expected: FAIL

- [ ] **Step 3: 实现结果/历史 Splitter**

在 `_build` 方法中添加 `_build_results_history_split` 调用，并实现该方法:

```python
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self._build_status_card(layout)
        self._build_operation_panel(layout)
        self._build_results_history_split(layout)  # <-- 新增

    def _build_results_history_split(self, parent_layout):
        """结果/历史 Splitter: 上(PID 参数表) 下(操作历史)。"""
        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)

        # 上: 参数表占位 (attach_params_panel 时替换)
        self._params_placeholder = QLabel("等待参数面板注入...")
        self._params_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._params_placeholder.setStyleSheet(
            f"color:{theme.hex('muted')}; padding:20px;")
        split.addWidget(self._params_placeholder)

        # 下: 操作历史
        hist_box = QGroupBox("操作历史")
        hv = QVBoxLayout(hist_box)
        hv.setContentsMargins(4, 4, 4, 4)
        self._history_view = QTextEdit()
        self._history_view.setReadOnly(True)
        self._history_view.setMaximumHeight(150)
        hv.addWidget(self._history_view)
        split.addWidget(hist_box)

        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        parent_layout.addWidget(split, 1)
        self._params_split = split
```

- [ ] **Step 4: 完善 attach_params_panel**

替换 `attach_params_panel` 方法:

```python
    def attach_params_panel(self, panel):
        """注入 ParamPanel 实例, 替换占位控件。"""
        self._params_panel = panel
        # 替换 Splitter 中的占位控件
        idx = self._params_split.indexOf(self._params_placeholder)
        if idx >= 0:
            self._params_placeholder.setParent(None)
            self._params_placeholder.deleteLater()
            self._params_placeholder = None
            self._params_split.insertWidget(idx, panel)
        self._set_last_op("参数面板已加载")
```

- [ ] **Step 5: 添加 set_history_visible 方法**

```python
    def set_history_visible(self, visible: bool):
        """切换操作历史区显隐。"""
        if self._history_view:
            self._history_view.setVisible(visible)
            # 找到 history 所在的 QGroupBox 并同步
            parent = self._history_view.parentWidget()
            if parent:
                parent.setVisible(visible)
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui && python -m pytest tools/test/test_pid_panel.py -v`
Expected: ALL tests PASS (11 tests)

- [ ] **Step 7: Commit**

```bash
git add ui/panels/pid_panel.py tools/test/test_pid_panel.py
git commit -m "feat(gui): add results/history splitter and ParamPanel injection"
```

---

## Task 5: 主窗口注册 Tab + 信号连接

**Files:**
- Modify: `ui/main_window.py` — import + 实例化 + addTab + 信号连接 + 转发方法

- [ ] **Step 1: 编写测试（验证 Tab 已注册）**

在 `tools/test/test_pid_panel.py` 末尾追加:

```python
def test_main_window_has_pid_tab():
    """主窗口含 'PID 整定' Tab。"""
    app = QApplication.instance() or QApplication(sys.argv)
    from ui.main_window import MainWindow
    win = MainWindow()
    tab_titles = []
    for i in range(win._tabs.count()):
        tab_titles.append(win._tabs.tabText(i))
    assert "PID 整定" in tab_titles
    assert hasattr(win, '_pid_panel')
    assert hasattr(win, '_pid_params_panel')
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui && python -m pytest tools/test/test_pid_panel.py -v -k "main_window"`
Expected: FAIL (无 _pid_panel 属性)

- [ ] **Step 3: 添加 import**

在 `ui/main_window.py` line 37 后添加:

```python
from ui.panels.pid_panel import PidPanel
```

- [ ] **Step 4: 实例化 PID 面板和参数面板**

在 `_build_ui` 方法中（line 154 `self._twin_param_panel = TwinParamPanel()` 之前）添加:

```python
        self._pid_panel = PidPanel(self._registry)
        self._pid_params_panel = ParamPanel(
            self._registry, title="PID 参数 (Flash)", source="motor_config",
            groups=("ControlParam",), show_save=True,
            save_text="保存PID参数到Flash", show_legend=False,
            show_bulk_rw=True)
```

- [ ] **Step 5: 注册 Tab**

在 `tabs.addTab(self._calib_panel, "电机标定")` (line 174) 之后添加:

```python
        tabs.addTab(self._pid_panel, "PID 整定")
```

- [ ] **Step 6: 连接信号**

在 `_connect_signals` 方法中（line 689 `c.calib_status_received.connect(...)` 之后）添加:

```python
        c.pid_autotune_result_received.connect(self._on_pid_autotune_result)
```

在标定面板信号连接块之后（line 720 `self._calib_results_panel.save_all.connect(self._on_config_save)` 之后）添加 PID 面板信号连接:

```python
        # PID 面板: 专用信号直连 client
        self._pid_panel.pid_autotune_requested.connect(
            lambda r, c_bw, v_bw, p_bw: self._on_pid_autotune(r, c_bw, v_bw, p_bw))
        self._pid_panel.pid_source_set_requested.connect(
            lambda r, s: self._on_pid_source_set(r, s))
        # PID 参数面板: 注入 PID Tab 内嵌, 复用 motor_config 通道
        self._pid_panel.attach_params_panel(self._pid_params_panel)
        self._pid_params_panel.read_param.connect(
            lambda param_id, panel=self._pid_params_panel:
                self._on_param_read(panel, param_id))
        self._pid_params_panel.write_param.connect(
            lambda param_id, text, panel=self._pid_params_panel:
                self._on_param_write(panel, param_id, text))
        self._pid_params_panel.read_params.connect(
            lambda param_ids, panel=self._pid_params_panel:
                self._on_param_read_many(panel, param_ids))
        self._pid_params_panel.write_params.connect(
            lambda writes, panel=self._pid_params_panel:
                self._on_param_write_many(panel, writes))
        self._pid_params_panel.save_all.connect(self._on_config_save)
```

- [ ] **Step 7: 添加状态转发**

在 `_on_ui_tick` (line 1023 `self._calib_panel.update_state(...)`) 之后添加:

```python
            self._pid_panel.update_state(*self._latest_state)
```

在 `_on_connected` (line 803 `self._calib_panel.set_link_active(connected)`) 之后添加:

```python
        self._pid_panel.set_link_active(connected)
```

- [ ] **Step 8: 添加 ACK/NACK 转发**

在 `_on_ack` (line 1060 `self._calib_panel.on_ack(cmd)`) 之后添加:

```python
        self._pid_panel.on_ack(cmd)
```

在 `_on_nack` (line 1081 `self._calib_panel.on_nack(cmd, err)`) 之后添加:

```python
        self._pid_panel.on_nack(cmd, err)
```

- [ ] **Step 9: 添加 0x9A 应答转发方法**

在 `_on_calib_status` 方法之后添加:

```python
    def _on_pid_autotune_result(self, result: dict):
        """0x9A PID_AUTOTUNE 应答 (JmClient.pid_autotune_result_received 信号)。"""
        ok = result.get('ok', False)
        fail_cn = result.get('fail_reason_cn', '')
        ring_done = int(result.get('ring_select_done', 0))
        if ok:
            self._log_panel.log(f"[RX] PID_AUTOTUNE 成功 ring={ring_done}")
        else:
            self._log_panel.log_warn(
                f"[RX] PID_AUTOTUNE 失败: {fail_cn}")
        self._pid_panel.on_autotune_result(result)

    def _on_pid_autotune(self, ring_select: int, cur_bw: float, vel_bw: float, pos_bw: float):
        """PID 面板请求理论估计 -> 调用 client.pid_autotune。"""
        if not self._ensure_open():
            return
        try:
            self._client.pid_autotune(ring_select, cur_bw, vel_bw, pos_bw)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"发送异常: {e}")

    def _on_pid_source_set(self, ring_select: int, source: int):
        """PID 面板请求来源切换 -> 调用 client.pid_source_set。"""
        if not self._ensure_open():
            return
        try:
            self._client.pid_source_set(ring_select, source)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"发送异常: {e}")
```

- [ ] **Step 10: 添加 Tab 切换自动刷新**

在 `_on_main_tab_changed` (line 858) 中，标定面板检查之后添加 PID 面板检查:

```python
    def _on_main_tab_changed(self, index: int):
        """切到标定/PID Tab 且已连接时, 自动读一次参数。"""
        try:
            w = self._tabs.widget(index)
        except Exception:
            return
        if w is self._calib_panel:
            if not self._client.is_open():
                return
            try:
                self._calib_results_panel.read_all()
            except Exception:
                pass
            return
        if w is self._pid_panel:
            if not self._client.is_open():
                return
            try:
                self._pid_params_panel.read_all()
            except Exception:
                pass
            return
```

- [ ] **Step 11: 运行测试确认通过**

Run: `cd d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui && python -m pytest tools/test/test_pid_panel.py -v`
Expected: ALL tests PASS (12 tests)

- [ ] **Step 12: Commit**

```bash
git add ui/main_window.py tools/test/test_pid_panel.py
git commit -m "feat(gui): register PID tab in MainWindow with signal routing"
```

---

## Task 6: apply_theme 完善 + 端到端验证

**Files:**
- Modify: `ui/panels/pid_panel.py` — `apply_theme` 刷新所有控件样式

- [ ] **Step 1: 完善 apply_theme 方法**

替换 `apply_theme` 方法:

```python
    def apply_theme(self):
        """主题切换: 刷新所有控件样式。"""
        self._status_label.setStyleSheet(
            f"font-weight:bold; color:{theme.hex('text_strong')};")
        self._last_op_label.setStyleSheet(f"color:{theme.hex('muted')};")
        self._autotune_btn.setStyleSheet(self._ok_btn_qss())
        self._source_apply_btn.setStyleSheet(self._normal_btn_qss())
        self._refresh_source_badges()
        self._refresh_status()
        if self._params_panel and hasattr(self._params_panel, 'apply_theme'):
            self._params_panel.apply_theme()
```

- [ ] **Step 2: 运行全部测试**

Run: `cd d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui && python -m pytest tools/test/test_pid_panel.py -v`
Expected: ALL tests PASS

- [ ] **Step 3: 运行现有标定测试确保无回归**

Run: `cd d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui && python -m pytest tools/test/ -v`
Expected: ALL tests PASS (含标定相关测试)

- [ ] **Step 4: 手动启动验证**

Run: `cd d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui && python main.py`
Expected: 主窗口出现 "PID 整定" Tab，点击可看到: 状态卡 + 理论估计面板 + 来源切换面板 + 参数表占位 + 历史区

- [ ] **Step 5: Commit**

```bash
git add ui/panels/pid_panel.py
git commit -m "feat(gui): complete PidPanel theme support and end-to-end verification"
```

---

## 验证清单

- [ ] "PID 整定" Tab 出现在主窗口 Tab 栏，位于"电机标定"之后
- [ ] 未连接时状态卡显示"未连接"(红色)，按钮点击提示"请先连接电机"
- [ ] 已连接 IDLE 态时状态卡显示"已连接 · IDLE 就绪"(绿色)
- [ ] 已连接非 IDLE 态时按钮点击提示"需切换到 IDLE 态"
- [ ] 理论估计面板: 三环勾选框默认全选，带宽 SpinBox 默认 0(显示"默认")
- [ ] 点击"开始理论估计"发送 0x9A，历史区记录 `[TX] 理论估计 ...`
- [ ] 0x9A 成功应答: 历史区记录 `[RX] 理论估计成功`，对应环 source 徽章变绿(理论估计)，参数表自动刷新
- [ ] 0x9A 失败应答: 历史区记录 `[RX] 理论估计失败: {原因}`，source 不变
- [ ] 来源切换面板: 三环各有 3 个单选(默认/Flash/理论估计)
- [ ] 点击"应用来源切换"逐环发送 0x9B，成功 ACK 后 source 徽章更新
- [ ] PID 参数表显示 ControlParam 段(param_id 64-85)，含单读/单写/全读/全写按钮
- [ ] 0xEA 保存按钮可将 PID 参数固化到 Flash
- [ ] 切到 PID Tab 时自动读一次参数
- [ ] 主题切换(light/dark)后所有控件样式正确刷新
- [ ] 现有标定/参数/反馈等 Tab 功能不受影响(回归测试通过)
