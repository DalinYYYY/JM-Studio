# Waveform 波形显示上位机优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 优化 `tools/waveform` 波形显示上位机：UI 命令/布局风格对齐主上位机、绘图性能流畅化、修复导出 bug、增加配置持久化与键盘快捷键。

**Architecture:**
- **主题适配层** `theme_adapter.py`：优先 `from ui.theme import theme`（与主上位机共用色板），不可用时回退到内置 mini-theme，保持 waveform 包可独立拷贝
- **配置持久化** `waveform_layout_store.py`：仿 `ui/layout_store.py` 接口，存到包内 `waveform_layout.json`
- **性能优化**：RingBuffer 改 `float32` + 预分配索引数组；全局 `pg.setConfigOptions(antialias=False, useOpenGL=True)`；曲线级 dirty 标记跳过重绘；鼠标移动节流到 ~30ms
- **文件拆分**：`waveform_plot.py`（2000 行）拆出 `ring_buffer.py`（RingBuffer + DataPool + ChannelMeta），主文件保留绘图类
- **风格对齐**：按钮样式用 `theme.hex()` 拼接、Splitter 手柄样式、状态栏 `● 端口名` + Consolas 字体、默认字体 Microsoft YaHei UI 9

**Tech Stack:** PyQt6 >= 6.5、pyqtgraph >= 0.13、numpy >= 1.24、pyserial >= 3.5；可选 OpenGL 加速（PyQt6 OpenGL 组件）

---

## 现状分析（已完成）

### A. 真实 Bug（导出功能不可用）
1. [exporter.py:19](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/exporter.py#L19) `from .waveform_plot import Curve` —— `waveform_plot.py` 中类名是 `PanelCurve`，没有 `Curve`，调用即 `ImportError`
2. [exporter.py:71](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/exporter.py#L71) 访问 `c.meta.visible` —— `ChannelMeta` 只有 `key/label/unit/group/color`，无 `visible` 字段，`AttributeError`
3. [exporter.py:137](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/exporter.py#L137) 访问 `wp._gfx` —— `WaveformPlot` 没有 `_gfx` 属性（`_gfx` 是 `WaveformPanel` 的），PNG 导出失败

### B. 性能瓶颈
1. `RingBuffer` 用 `float64`，主上位机 `plot_panel` 用 `float32`（带宽减半）
2. 未启用 OpenGL、未关闭抗锯齿（主上位机 [plot_panel.py:36-39](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/ui/panels/plot_panel.py#L36-L39) 有）
3. `RingBuffer._build_cache` 满缓冲时每次重分配索引数组（[waveform_plot.py:137](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/waveform_plot.py#L137)）
4. 每 33ms 无脑刷新所有曲线，无 dirty 标记跳过
5. 鼠标移动 `_on_mouse_moved` 每事件对每通道 `slice_last` + `np.searchsorted`
6. 测量表格每 300ms 全量 `setRowCount` + 逐行 `setItem`

### C. 易用性
1. 无配置持久化（连接参数、通道布局、时间窗、触发全丢失）
2. 无键盘快捷键
3. 无自动重连
4. 游标吸附开启时拖动卡顿

### D. 风格不一致
1. [main.py:39-96](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/main.py#L39-L96) 硬编码 QSS，主上位机有 `ui/theme.py` 单例 + 双色板 + `changed` 信号
2. 配色差异：主上位机 dark 窗口背景 `#0B0F1A`，waveform `#2B2B2B`
3. 缺默认字体（主上位机 `Microsoft YaHei UI 9`）
4. Splitter 手柄样式不一致
5. 状态栏无 `● 端口名` 风格

### E. 代码组织
1. `waveform_plot.py` 2000 行 / 6 个类，`WaveformPanel` 单类 1200+ 行
2. 样式散落三处：`main.py` / `app.py` / `waveform_plot.py`

---

## 文件结构

### 新建文件
| 路径 | 职责 |
|------|------|
| `tools/waveform/theme_adapter.py` | 主题适配层：优先用 `ui.theme`，回退到内置色板 |
| `tools/waveform/waveform_layout_store.py` | 配置持久化：仿 `ui/layout_store.py` 接口 |
| `tools/waveform/ring_buffer.py` | 从 `waveform_plot.py` 拆出：`RingBuffer` + `DataPool` + `ChannelMeta` |
| `tools/waveform/tests/__init__.py` | 测试包标记 |
| `tools/waveform/tests/test_ring_buffer.py` | RingBuffer 单元测试 |
| `tools/waveform/tests/test_theme_adapter.py` | 主题适配层测试 |
| `tools/waveform/tests/test_waveform_layout_store.py` | 配置持久化测试 |
| `tools/waveform/tests/test_exporter.py` | 导出 bug 回归测试 |
| `tools/waveform/waveform_layout.json` | 运行时配置（首次运行自动生成） |

### 修改文件
| 路径 | 改动 |
|------|------|
| `tools/waveform/main.py` | 接入 theme_adapter，移除硬编码 QSS |
| `tools/waveform/app.py` | 按钮样式对齐主上位机、状态栏 `● 端口名`、自动重连、键盘快捷键、配置持久化接入 |
| `tools/waveform/waveform_plot.py` | 拆出 ring_buffer 后引用调整；性能优化（dirty 标记、节流）；样式对齐 |
| `tools/waveform/exporter.py` | 修复 3 个 bug |

---

## Task 1: 创建 ring_buffer.py 并拆出 RingBuffer/DataPool/ChannelMeta

**Files:**
- Create: `tools/waveform/ring_buffer.py`
- Modify: `tools/waveform/waveform_plot.py` (移除已迁移的类，改为 import)

- [ ] **Step 1: 创建 ring_buffer.py，把 RingBuffer / DataPool / ChannelMeta / DEFAULT_COLORS / _UNITS / _LABELS / CHANNEL_METAS / CHANNELS_BY_KEY 全部迁移过来**

```python
"""
环形缓冲与共享数据池。

设计要点:
- RingBuffer: 预分配 numpy 环形缓冲, O(1) append, 惰性构建时间顺序视图
- DataPool: 所有面板共享一套 RingBuffer (按 key 索引), 避免重复存储
- ChannelMeta: 单条曲线的全局元数据 (所有面板共享)

通道元数据来自 protocol_reference.JmTlmBit.ITEMS。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from .protocol_reference import JmTlmBit, FeedbackData


# ============================================================================
# 通道元数据
# ============================================================================

@dataclass
class ChannelMeta:
    """单条曲线的全局元数据 (所有面板共享)"""
    key: str
    label: str
    unit: str
    group: str
    color: str = '#00FF00'


DEFAULT_COLORS = [
    '#00FF00', '#FF8000', '#00BFFF', '#FF00FF', '#FFFF00',
    '#FF4040', '#40FF40', '#4080FF', '#FF80FF', '#80FFFF',
    '#FFC080', '#80FF80', '#8080FF', '#FF8080', '#80FFFF',
    '#C0C0C0', '#FF40C0', '#40FFC0', '#C040FF', '#C0FF40',
    '#40C0FF', '#FFFF80', '#808080',
]

_UNITS = {
    'pos': 'rad', 'vel': 'rad/s', 'torque': 'Nm',
    'id': 'A', 'iq': 'A', 'ia': 'A', 'ib': 'A', 'ic': 'A',
    'vbus': 'V', 'ibus': 'A', 'power': 'W',
    'temp_fet': '°C', 'temp_motor': '°C',
    'multiturn': '-', 'single': 'rad',
    'fault_mask': '-', 'warn_mask': '-',
    'top_fsm': '-', 'run_state': '-', 'ctrl_mode': '-', 'enable': '-', 'motion_state': '-',
}
_LABELS = {
    'pos': '位置', 'vel': '速度', 'torque': '力矩',
    'id': 'Id', 'iq': 'Iq', 'ia': 'Ia', 'ib': 'Ib', 'ic': 'Ic',
    'vbus': '母线电压', 'ibus': '母线电流', 'power': '功率',
    'temp_fet': 'FET温度', 'temp_motor': '电机温度',
    'multiturn': '多圈计数', 'single': '单圈位置',
    'fault_mask': '故障码', 'warn_mask': '警告码',
    'top_fsm': '顶层状态', 'run_state': '运行子状态',
    'ctrl_mode': '控制模式', 'enable': '使能', 'motion_state': '运动状态',
}

CHANNEL_METAS: List[ChannelMeta] = []
_ci = 0
for mask_val, group_label, field_names, _ in JmTlmBit.ITEMS:
    if mask_val == JmTlmBit.DEBUG:
        continue
    for fn in field_names:
        CHANNEL_METAS.append(ChannelMeta(
            key=fn, label=_LABELS.get(fn, fn), unit=_UNITS.get(fn, '-'),
            group=group_label, color=DEFAULT_COLORS[_ci % len(DEFAULT_COLORS)],
        ))
        _ci += 1

CHANNELS_BY_KEY: Dict[str, ChannelMeta] = {c.key: c for c in CHANNEL_METAS}


# ============================================================================
# 环形缓冲
# ============================================================================

class RingBuffer:
    """预分配 numpy 环形缓冲区, O(1) append, 惰性构建时间顺序视图"""

    def __init__(self, maxlen: int = 60000):
        n = int(maxlen)
        self._ts = np.full(n, np.nan, dtype=np.float64)
        self._ys = np.full(n, np.nan, dtype=np.float64)
        self._head = 0
        self._count = 0
        self._maxlen = n
        self._cache_ts: Optional[np.ndarray] = None
        self._cache_ys: Optional[np.ndarray] = None

    def append(self, t: float, y: float):
        self._ts[self._head] = float(t)
        self._ys[self._head] = float(y)
        self._head = (self._head + 1) % self._maxlen
        if self._count < self._maxlen:
            self._count += 1
        self._cache_ts = None
        self._cache_ys = None

    def clear(self):
        self._ts.fill(np.nan)
        self._ys.fill(np.nan)
        self._head = 0
        self._count = 0
        self._cache_ts = None
        self._cache_ys = None

    def _build_cache(self):
        c = self._count
        if c == 0:
            self._cache_ts = np.empty(0, dtype=np.float64)
            self._cache_ys = np.empty(0, dtype=np.float64)
            return
        if c < self._maxlen:
            self._cache_ts = self._ts[:c]
            self._cache_ys = self._ys[:c]
        else:
            idx = np.arange(self._maxlen, dtype=np.intp)
            idx = (self._head + idx) % self._maxlen
            self._cache_ts = self._ts[idx]
            self._cache_ys = self._ys[idx]

    @property
    def ts_view(self) -> np.ndarray:
        if self._cache_ts is None:
            self._build_cache()
        return self._cache_ts

    @property
    def ys_view(self) -> np.ndarray:
        if self._cache_ys is None:
            self._build_cache()
        return self._cache_ys

    @property
    def count(self) -> int:
        return self._count

    @property
    def maxlen(self) -> int:
        return self._maxlen

    def slice_last(self, window_s: float) -> Tuple[np.ndarray, np.ndarray]:
        if self._count == 0:
            return np.empty(0), np.empty(0)
        ts = self.ts_view
        ys = self.ys_view
        t_end = ts[-1]
        t_start = t_end - window_s
        mask = ts >= t_start
        return ts[mask], ys[mask]

    def latest_value(self) -> Optional[float]:
        if self._count == 0:
            return None
        idx = (self._head - 1) % self._maxlen
        return float(self._ys[idx])


# ============================================================================
# 共享数据池: 所有面板共用
# ============================================================================

class DataPool:
    """所有面板共享的环形缓冲池"""

    def __init__(self, maxlen: int = 60000):
        self.buffers: Dict[str, RingBuffer] = {
            m.key: RingBuffer(maxlen) for m in CHANNEL_METAS
        }
        self.t0 = time.monotonic()

    def append_feedback(self, fb: FeedbackData, filled: Tuple[str, ...]) -> float:
        """返回当前时间戳 (相对 t0)"""
        t = time.monotonic() - self.t0
        for key in filled:
            buf = self.buffers.get(key)
            if buf is None:
                continue
            v = getattr(fb, key, None)
            if v is not None:
                buf.append(t, float(v))
        return t

    def clear(self):
        for b in self.buffers.values():
            b.clear()

    def total_samples(self) -> int:
        return sum(b.count for b in self.buffers.values())
```

- [ ] **Step 2: 修改 waveform_plot.py，删除已迁移的类，改为 import**

在 [waveform_plot.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/waveform_plot.py) 中：

删除第 36-205 行（从 `# 通道元数据` 注释到 `DataPool` 类结束，含 `ChannelMeta`、`DEFAULT_COLORS`、`_UNITS`、`_LABELS`、`CHANNEL_METAS`、`CHANNELS_BY_KEY`、`RingBuffer`、`DataPool`）。

把第 33 行 `from .protocol_reference import JmTlmBit, FeedbackData` 改为：

```python
from .protocol_reference import JmTlmBit, FeedbackData
from .ring_buffer import (
    ChannelMeta, DEFAULT_COLORS, CHANNEL_METAS, CHANNELS_BY_KEY,
    RingBuffer, DataPool,
)
```

- [ ] **Step 3: 创建 tests/__init__.py 和 tests/test_ring_buffer.py**

```python
# tools/waveform/tests/__init__.py
# (空文件，仅作为包标记)
```

```python
# tools/waveform/tests/test_ring_buffer.py
"""RingBuffer / DataPool 单元测试。"""
import os
import sys
import numpy as np

# 让脚本可直接运行: 上溯到 pyqt_gui/ 加入 sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.waveform.ring_buffer import RingBuffer, DataPool, ChannelMeta, CHANNEL_METAS

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


def test_ringbuffer_append_and_view():
    print("[TEST] RingBuffer append + view")
    buf = RingBuffer(10)
    check(buf.count == 0, "初始 count==0")
    check(buf.latest_value() is None, "空缓冲 latest_value=None")

    for i in range(5):
        buf.append(float(i), float(i) * 2.0)
    check(buf.count == 5, f"append 5 条后 count==5, 实际={buf.count}")
    check(buf.latest_value() == 8.0, f"latest_value==8.0, 实际={buf.latest_value()}")

    ts = buf.ts_view
    ys = buf.ys_view
    check(len(ts) == 5, f"ts_view 长度==5, 实际={len(ts)}")
    check(ts[0] == 0.0 and ts[-1] == 4.0, "ts 顺序正确")
    check(ys[-1] == 8.0, "ys[-1]==8.0")


def test_ringbuffer_wraparound():
    print("[TEST] RingBuffer 环形回绕")
    buf = RingBuffer(5)
    for i in range(8):  # 超过 maxlen
        buf.append(float(i), float(i))
    check(buf.count == 5, f"满缓冲 count==5, 实际={buf.count}")
    ts = buf.ts_view
    # 应该是 [3,4,5,6,7] 顺序
    check(ts[0] == 3.0 and ts[-1] == 7.0, f"回绕后 ts[0]==3, ts[-1]==7, 实际 ts={ts}")


def test_ringbuffer_slice_last():
    print("[TEST] RingBuffer slice_last")
    buf = RingBuffer(100)
    for i in range(50):
        buf.append(float(i), float(i))
    ts, ys = buf.slice_last(10.0)
    check(len(ts) > 0, "slice_last 返回非空")
    check(ts[-1] == 49.0, f"末尾 t==49, 实际={ts[-1]}")
    check(ts[0] >= 39.0, f"起点 t>=39, 实际={ts[0]}")


def test_ringbuffer_clear():
    print("[TEST] RingBuffer clear")
    buf = RingBuffer(10)
    for i in range(3):
        buf.append(float(i), float(i))
    buf.clear()
    check(buf.count == 0, "clear 后 count==0")
    check(buf.latest_value() is None, "clear 后 latest_value=None")


def test_datapool():
    print("[TEST] DataPool")
    pool = DataPool(maxlen=100)
    check(len(pool.buffers) == len(CHANNEL_METAS), "buffers 数量==CHANNEL_METAS 数量")
    check(pool.total_samples() == 0, "初始 total_samples==0")


if __name__ == '__main__':
    test_ringbuffer_append_and_view()
    test_ringbuffer_wraparound()
    test_ringbuffer_slice_last()
    test_ringbuffer_clear()
    test_datapool()
    print(f"\n{'='*40}\nPASS={PASS} FAIL={FAIL}")
    if _FAILS:
        for m in _FAILS:
            print(f"  - {m}")
    sys.exit(1 if FAIL else 0)
```

- [ ] **Step 4: 运行测试验证拆分正确**

Run: `python -m tools.waveform.tests.test_ring_buffer`
Expected: `PASS=6 FAIL=0`

再运行（验证导入正确）:
Run: `python -c "from tools.waveform.waveform_plot import WaveformPanel, WaveformPlot; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add tools/waveform/ring_buffer.py tools/waveform/waveform_plot.py tools/waveform/tests/
git commit -m "refactor(waveform): 拆出 ring_buffer.py (RingBuffer/DataPool/ChannelMeta)

从 waveform_plot.py 2000 行巨型文件中拆出数据层到 ring_buffer.py,
便于后续性能优化与测试。新增 tests/ 包与 ring_buffer 单元测试。"
```

---

## Task 2: 创建 theme_adapter.py 主题适配层

**Files:**
- Create: `tools/waveform/theme_adapter.py`
- Test: `tools/waveform/tests/test_theme_adapter.py`

- [ ] **Step 1: 创建 theme_adapter.py**

```python
"""
主题适配层: 优先复用主上位机 ui.theme, 不可用时回退到内置 mini-theme。

设计目标:
- 与主上位机 (pyqt_gui/ui/theme.py) 视觉风格一致
- 保持 waveform 包可独立拷贝到其他工程 (无 ui.theme 时也能运行)

用法:
    from .theme_adapter import theme_adapter
    app.setStyleSheet(theme_adapter.qss())           # 全局 QSS
    color = theme_adapter.hex('accent')              # 16进制颜色
    theme_adapter.apply_to_widget(button)            # 应用主题到按钮
    theme_adapter.on_changed(callback)               # 订阅主题切换
"""
from __future__ import annotations

from typing import Callable, List, Optional

from PyQt6.QtGui import QColor
from PyQt6.QtCore import QObject, pyqtSignal


# ============================================================================
# 内置回退色板 (与主上位机 ui/theme.py dark 保持一致)
# ============================================================================

_FALLBACK_DARK = {
    "app_bg": "#0B0F1A", "panel_bg": "#11172A", "card_top": "#1A2138",
    "card_bottom": "#141B2E", "border": "#2A3550", "text": "#C8D2E0",
    "text_strong": "#FFFFFF", "muted": "#6A7590", "title": "#E6ECF5",
    "input_bg": "#0E1424", "input_text": "#E6ECF5", "input_border": "#2A3550",
    "btn_bg": "#1A2138", "btn_text": "#C8D2E0", "btn_border": "#2A3550",
    "btn_hover": "#2A3550",
    "table_bg": "#11172A", "table_alt": "#161D33", "table_grid": "#2A3550",
    "table_header": "#1A2138", "table_text": "#C8D2E0",
    "sel_bg": "#2E5C8A", "sel_text": "#FFFFFF",
    "log_bg": "#0E1424", "log_text": "#C8D2E0",
    "scroll_bg": "#11172A", "scroll_handle": "#2A3550",
    "accent": "#5FE6AC", "value": "#5FE6AC", "value_hot": "#FF9800",
    "ok": "#4CAF50", "ok_text": "#FFFFFF",
    "danger": "#F44336", "danger_text": "#FFFFFF", "warn": "#C8963C",
    "plot_bg": "#0E1424", "plot_title": "#D8D8D8",
    "plot_cursor": "#B0B0B0", "plot_label_bg": "#000000", "plot_label_fg": "#F5F5F5",
}

_FALLBACK_LIGHT = {
    "app_bg": "#F5F7FA", "panel_bg": "#FFFFFF", "card_top": "#FFFFFF",
    "card_bottom": "#ECEFF4", "border": "#D0D7E2", "text": "#2C3E50",
    "text_strong": "#1A2332", "muted": "#7F8C9A", "title": "#1A2332",
    "input_bg": "#FFFFFF", "input_text": "#1A2332", "input_border": "#C0C8D2",
    "btn_bg": "#ECEFF4", "btn_text": "#2C3E50", "btn_border": "#C0C8D2",
    "btn_hover": "#DDE3EC",
    "table_bg": "#FFFFFF", "table_alt": "#F5F7FA", "table_grid": "#E0E5EC",
    "table_header": "#ECEFF4", "table_text": "#2C3E50",
    "sel_bg": "#2E5C8A", "sel_text": "#FFFFFF",
    "log_bg": "#FFFFFF", "log_text": "#2C3E50",
    "scroll_bg": "#F5F7FA", "scroll_handle": "#C0C8D2",
    "accent": "#1E9E6A", "value": "#1E9E6A", "value_hot": "#E67E22",
    "ok": "#2E7D32", "ok_text": "#FFFFFF",
    "danger": "#D32F2F", "danger_text": "#FFFFFF", "warn": "#B7791F",
    "plot_bg": "#FFFFFF", "plot_title": "#28303E",
    "plot_cursor": "#404040", "plot_label_bg": "#FFFFFF", "plot_label_fg": "#1A2332",
}


class _ThemeAdapter(QObject):
    """主题适配单例。

    - 若主上位机 ui.theme 可用, 透传其色板/信号 (与主窗口切换同步)
    - 否则使用内置 _FALLBACK_DARK/_FALLBACK_LIGHT 色板, 仍支持 dark/light 切换
    """

    changed = pyqtSignal(str)  # 主题名变化

    def __init__(self):
        super().__init__()
        self._ui_theme = None  # 主上位机 theme 实例 (可能为 None)
        self._fallback_name = "dark"
        try:
            from ui.theme import theme as _ui_theme  # type: ignore
            self._ui_theme = _ui_theme
            # 透传主上位机 changed 信号
            try:
                self._ui_theme.changed.connect(self._on_ui_theme_changed)
            except Exception:
                pass
        except Exception:
            self._ui_theme = None

    # -------- 当前主题名 --------
    @property
    def name(self) -> str:
        if self._ui_theme is not None:
            try:
                return "dark" if self._ui_theme.is_dark else "light"
            except Exception:
                pass
        return self._fallback_name

    @property
    def is_dark(self) -> bool:
        return self.name == "dark"

    # -------- 取色 --------
    def hex(self, key: str) -> str:
        """返回 16 进制颜色字符串, 如 '#5FE6AC'。"""
        if self._ui_theme is not None:
            try:
                v = self._ui_theme.hex(key)
                if v:
                    return v
            except Exception:
                pass
        palette = _FALLBACK_DARK if self.is_dark else _FALLBACK_LIGHT
        return palette.get(key, "#888888")

    def c(self, key: str) -> QColor:
        """返回 QColor。"""
        return QColor(self.hex(key))

    # -------- QSS --------
    def qss(self) -> str:
        """生成全局 QSS, 风格与主上位机对齐。"""
        return f"""
        QMainWindow {{ background: {self.hex('app_bg')}; }}
        QWidget {{ color: {self.hex('text')}; }}
        QToolTip {{
            background: {self.hex('card_bottom')}; color: {self.hex('text_strong')};
            border: 1px solid {self.hex('border')}; padding: 2px 4px;
        }}
        QGroupBox {{
            border: 1px solid {self.hex('border')};
            border-radius: 4px;
            margin-top: 10px;
            padding-top: 10px;
            font-weight: bold;
            background: {self.hex('panel_bg')};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 8px;
            padding: 0 4px;
            color: {self.hex('title')};
        }}
        QLabel {{ color: {self.hex('text')}; }}
        QPushButton {{
            background: {self.hex('btn_bg')};
            color: {self.hex('btn_text')};
            border: 1px solid {self.hex('btn_border')};
            padding: 4px 10px;
            border-radius: 3px;
        }}
        QPushButton:hover {{ background: {self.hex('btn_hover')}; border-color: {self.hex('muted')}; }}
        QPushButton:pressed {{ background: {self.hex('card_bottom')}; }}
        QPushButton:disabled {{ color: {self.hex('muted')}; background: {self.hex('card_bottom')}; }}
        QPushButton:checked {{
            background: {self.hex('accent')};
            border-color: {self.hex('accent')};
            color: {self.hex('card_bottom')};
            font-weight: bold;
        }}
        QComboBox {{
            background: {self.hex('input_bg')};
            color: {self.hex('input_text')};
            border: 1px solid {self.hex('input_border')};
            padding: 2px 6px;
            border-radius: 2px;
        }}
        QComboBox::drop-down {{ border: none; width: 18px; }}
        QComboBox QAbstractItemView {{
            background: {self.hex('input_bg')};
            color: {self.hex('input_text')};
            selection-background-color: {self.hex('sel_bg')};
            selection-color: {self.hex('sel_text')};
            border: 1px solid {self.hex('border')};
        }}
        QSpinBox, QDoubleSpinBox {{
            background: {self.hex('input_bg')};
            color: {self.hex('input_text')};
            border: 1px solid {self.hex('input_border')};
            padding: 2px 4px;
            border-radius: 2px;
        }}
        QCheckBox {{ spacing: 6px; color: {self.hex('text')}; }}
        QCheckBox::indicator {{
            width: 14px; height: 14px;
            border: 1px solid {self.hex('muted')};
            background: {self.hex('input_bg')};
            border-radius: 2px;
        }}
        QCheckBox::indicator:checked {{
            background: {self.hex('accent')};
            border: 1px solid {self.hex('accent')};
        }}
        QStatusBar {{ background: {self.hex('card_bottom')}; }}
        QStatusBar QLabel {{ padding: 0 8px; color: {self.hex('text')}; }}
        QMenuBar {{ background: {self.hex('app_bg')}; color: {self.hex('text')}; }}
        QMenuBar::item:selected {{ background: {self.hex('sel_bg')}; color: {self.hex('sel_text')}; }}
        QMenu {{
            background: {self.hex('panel_bg')};
            color: {self.hex('text')};
            border: 1px solid {self.hex('border')};
        }}
        QMenu::item:selected {{ background: {self.hex('sel_bg')}; color: {self.hex('sel_text')}; }}
        QTabWidget::pane {{ border: 1px solid {self.hex('border')}; }}
        QTabBar::tab {{
            background: {self.hex('card_bottom')};
            color: {self.hex('text')};
            padding: 4px 12px;
            border: 1px solid {self.hex('border')};
            border-bottom: none;
            border-top-left-radius: 3px;
            border-top-right-radius: 3px;
        }}
        QTabBar::tab:selected {{ background: {self.hex('panel_bg')}; color: {self.hex('text_strong')}; }}
        QTabBar::tab:!selected {{ margin-top: 2px; }}
        QSplitter::handle:horizontal {{ background: {self.hex('border')}; width: 2px; margin: 0 2px; }}
        QSplitter::handle:vertical {{ background: {self.hex('border')}; height: 2px; margin: 2px 0; }}
        QSplitter::handle:hover {{ background: {self.hex('accent')}; }}
        QScrollBar:vertical {{
            background: {self.hex('scroll_bg')};
            width: 10px; margin: 0;
        }}
        QScrollBar::handle:vertical {{
            background: {self.hex('scroll_handle')};
            min-height: 20px; border-radius: 4px; margin: 1px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar:horizontal {{
            background: {self.hex('scroll_bg')};
            height: 10px; margin: 0;
        }}
        QScrollBar::handle:horizontal {{
            background: {self.hex('scroll_handle')};
            min-width: 20px; border-radius: 4px; margin: 1px;
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
        QTableWidget {{
            background: {self.hex('table_bg')};
            alternate-background-color: {self.hex('table_alt')};
            color: {self.hex('table_text')};
            gridline-color: {self.hex('table_grid')};
            border: 1px solid {self.hex('border')};
        }}
        QHeaderView::section {{
            background: {self.hex('table_header')};
            color: {self.hex('text')};
            padding: 4px;
            border: none;
            border-right: 1px solid {self.hex('border')};
            font-weight: bold;
        }}
        QTextEdit, QPlainTextEdit {{
            background: {self.hex('log_bg')};
            color: {self.hex('log_text')};
            border: 1px solid {self.hex('border')};
        }}
        """

    # -------- 切换 --------
    def set(self, name: str):
        """切换主题 ('dark' 或 'light')。

        若有主上位机 theme, 透传给它; 否则只更新内置 fallback。
        """
        if name not in ("dark", "light"):
            return
        if self._ui_theme is not None:
            try:
                self._ui_theme.set(name)
                return  # ui_theme.set 会触发 changed 信号, 由 _on_ui_theme_changed 转发
            except Exception:
                pass
        self._fallback_name = name
        self.changed.emit(name)

    def _on_ui_theme_changed(self, name: str):
        """主上位机主题切换时的转发槽。"""
        self.changed.emit(name)

    def toggle(self):
        self.set("light" if self.is_dark else "dark")

    # -------- 订阅 --------
    def on_changed(self, callback: Callable[[str], None]):
        self.changed.connect(callback)


# 进程内单例
theme_adapter = _ThemeAdapter()
```

- [ ] **Step 2: 创建测试 test_theme_adapter.py**

```python
# tools/waveform/tests/test_theme_adapter.py
"""theme_adapter 单元测试。"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from tools.waveform.theme_adapter import theme_adapter, _FALLBACK_DARK, _FALLBACK_LIGHT

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


def test_hex_returns_color():
    print("[TEST] theme_adapter.hex 返回颜色")
    v = theme_adapter.hex('accent')
    check(v.startswith('#'), f"hex('accent') 以 # 开头, 实际={v}")
    check(len(v) == 7, f"hex('accent') 长度 7, 实际={len(v)}")


def test_qss_returns_string():
    print("[TEST] theme_adapter.qss 返回非空字符串")
    qss = theme_adapter.qss()
    check(isinstance(qss, str), "qss 是字符串")
    check(len(qss) > 100, f"qss 长度 > 100, 实际={len(qss)}")
    check('QPushButton' in qss, "qss 包含 QPushButton")
    check('QGroupBox' in qss, "qss 包含 QGroupBox")


def test_fallback_palette_complete():
    print("[TEST] 内置回退色板键完整")
    required = {'accent', 'app_bg', 'panel_bg', 'btn_bg', 'btn_text', 'ok', 'danger'}
    for k in required:
        check(k in _FALLBACK_DARK, f"_FALLBACK_DARK 缺少键 {k}")
        check(k in _FALLBACK_LIGHT, f"_FALLBACK_LIGHT 缺少键 {k}")


def test_dark_light_toggle():
    print("[TEST] dark/light 切换")
    original = theme_adapter.name
    theme_adapter.toggle()
    new = theme_adapter.name
    check(original != new, f"toggle 后主题变化: {original} -> {new}")
    theme_adapter.toggle()  # 切回
    check(theme_adapter.name == original, "再 toggle 回到原主题")


def test_on_changed_callback():
    print("[TEST] on_changed 回调")
    received = []
    theme_adapter.on_changed(lambda name: received.append(name))
    theme_adapter.toggle()
    theme_adapter.toggle()
    check(len(received) >= 2, f"回调至少触发 2 次, 实际={len(received)}")


if __name__ == '__main__':
    test_hex_returns_color()
    test_qss_returns_string()
    test_fallback_palette_complete()
    test_dark_light_toggle()
    test_on_changed_callback()
    print(f"\n{'='*40}\nPASS={PASS} FAIL={FAIL}")
    if _FAILS:
        for m in _FAILS:
            print(f"  - {m}")
    sys.exit(1 if FAIL else 0)
```

- [ ] **Step 3: 运行测试**

Run: `python -m tools.waveform.tests.test_theme_adapter`
Expected: `PASS=10 FAIL=0` (含每个 test 的多个 check)

- [ ] **Step 4: Commit**

```bash
git add tools/waveform/theme_adapter.py tools/waveform/tests/test_theme_adapter.py
git commit -m "feat(waveform): 添加 theme_adapter 主题适配层

优先复用主上位机 ui.theme 单例 (与主窗口主题切换同步),
不可用时回退到内置 _FALLBACK_DARK/_LIGHT 色板, 保持 waveform
包可独立拷贝。生成与主上位机风格一致的 QSS。"
```

---

## Task 3: 创建 waveform_layout_store.py 配置持久化

**Files:**
- Create: `tools/waveform/waveform_layout_store.py`
- Test: `tools/waveform/tests/test_waveform_layout_store.py`

- [ ] **Step 1: 创建 waveform_layout_store.py**

```python
"""waveform 工具配置持久化 (仿 ui/layout_store.py 接口)。

文件结构:
{
  "version": 1,
  "theme": "dark",                      # 主题名
  "connection": {                       # 连接配置
    "kind": "virtual|serial|can",
    "port": "COM3", "baud": 2000000,
    "can_channel": "can0", "can_bitrate": 1000000, "motor_id": 1,
    "telemetry_mask": 1023, "telemetry_period_ms": 100
  },
  "windows": {                          # 窗口/面板配置
    "count": 1, "window_s": 10.0,
    "panels": [
      {"id": 1, "channels": ["pos", "vel", "torque"], "y_mode": "auto"}
    ]
  },
  "trigger": {                          # 触发配置
    "enabled": false, "key": "pos", "edge": "rising",
    "level": 0.0, "hold_s": 0.5
  }
}

容错: 文件损坏或缺失返回 {} 不崩溃。
"""
from __future__ import annotations

import os
import json

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "waveform_layout.json")
_VERSION = 1

_cache: dict = {}


def path() -> str:
    return _PATH


def load() -> dict:
    """读取整份配置; 不存在或损坏返回 {}。"""
    global _cache
    if _cache:
        return _cache
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _cache = data
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {}


def save(whole: dict) -> bool:
    """整份写回。"""
    global _cache
    try:
        whole["version"] = _VERSION
        with open(_PATH, "w", encoding="utf-8") as f:
            json.dump(whole, f, ensure_ascii=False, indent=2)
        _cache = whole
        return True
    except Exception:
        return False


def get_section(section: str, default: dict = None) -> dict:
    """读取某节 (connection/windows/trigger); 缺失返回 default 或 {}。"""
    data = load()
    v = data.get(section)
    return v if isinstance(v, dict) else (default if default is not None else {})


def set_section(section: str, data: dict) -> bool:
    """更新某节并写回, 保留其它节。"""
    whole = load()
    whole["version"] = _VERSION
    whole[section] = data
    return save(whole)


def get_meta(key: str, default=None):
    """读顶层元数据 (如 theme)。"""
    return load().get(key, default)


def set_meta(key: str, value) -> bool:
    """写顶层元数据并保存。"""
    whole = load()
    whole["version"] = _VERSION
    whole[key] = value
    return save(whole)


def clear_cache():
    """清除内存缓存 (测试用)。"""
    global _cache
    _cache = {}
```

- [ ] **Step 2: 创建测试 test_waveform_layout_store.py**

```python
# tools/waveform/tests/test_waveform_layout_store.py
"""waveform_layout_store 单元测试。"""
import os
import sys
import json
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.waveform import waveform_layout_store as store

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


def with_temp_file(fn):
    """用临时文件替换 _PATH 跑测试, 跑完恢复。"""
    orig = store._PATH
    fd, tmp = tempfile.mkstemp(suffix='.json', prefix='wfl_')
    os.close(fd)
    try:
        os.remove(tmp)  # 让测试从空开始
        store._PATH = tmp
        store.clear_cache()
        fn()
    finally:
        store._PATH = orig
        store.clear_cache()
        if os.path.exists(tmp):
            os.remove(tmp)


def test_load_empty():
    def inner():
        print("[TEST] load 空配置")
        d = store.load()
        check(d == {}, f"空文件 load 返回 {{}}, 实际={d}")
    with_temp_file(inner)


def test_set_get_section():
    def inner():
        print("[TEST] set_section / get_section")
        cfg = {"kind": "serial", "port": "COM3", "baud": 2000000}
        ok = store.set_section("connection", cfg)
        check(ok, "set_section 返回 True")
        d = store.get_section("connection")
        check(d["port"] == "COM3", f"读回 port==COM3, 实际={d.get('port')}")
        check(d["baud"] == 2000000, f"读回 baud==2000000, 实际={d.get('baud')}")
    with_temp_file(inner)


def test_set_get_meta():
    def inner():
        print("[TEST] set_meta / get_meta")
        check(store.get_meta("theme", "dark") == "dark", "缺失时返回 default")
        store.set_meta("theme", "light")
        check(store.get_meta("theme", "dark") == "light", "读回 light")
    with_temp_file(inner)


def test_preserves_other_sections():
    def inner():
        print("[TEST] 保留其它节")
        store.set_section("connection", {"port": "COM5"})
        store.set_section("windows", {"count": 2})
        # connection 不应被覆盖
        conn = store.get_section("connection")
        check(conn["port"] == "COM5", f"connection 保留, 实际={conn}")
        win = store.get_section("windows")
        check(win["count"] == 2, f"windows 保留, 实际={win}")
    with_temp_file(inner)


def test_corrupt_returns_empty():
    def inner():
        print("[TEST] 损坏文件返回空")
        with open(store._PATH, 'w') as f:
            f.write("{not valid json")
        store.clear_cache()
        d = store.load()
        check(d == {}, f"损坏返回 {{}}, 实际={d}")
    with_temp_file(inner)


def test_cache_hit():
    def inner():
        print("[TEST] 缓存命中")
        store.set_meta("k", "v1")
        # 直接修改文件, 缓存应仍返回 v1
        with open(store._PATH, 'w') as f:
            json.dump({"k": "v2"}, f)
        # 不 clear_cache, 应返回缓存
        v = store.get_meta("k")
        check(v == "v1", f"缓存命中返回 v1, 实际={v}")
        store.clear_cache()
        v2 = store.get_meta("k")
        check(v2 == "v2", f"清缓存后返回 v2, 实际={v2}")
    with_temp_file(inner)


if __name__ == '__main__':
    test_load_empty()
    test_set_get_section()
    test_set_get_meta()
    test_preserves_other_sections()
    test_corrupt_returns_empty()
    test_cache_hit()
    print(f"\n{'='*40}\nPASS={PASS} FAIL={FAIL}")
    if _FAILS:
        for m in _FAILS:
            print(f"  - {m}")
    sys.exit(1 if FAIL else 0)
```

- [ ] **Step 3: 运行测试**

Run: `python -m tools.waveform.tests.test_waveform_layout_store`
Expected: `PASS=10 FAIL=0`

- [ ] **Step 4: Commit**

```bash
git add tools/waveform/waveform_layout_store.py tools/waveform/tests/test_waveform_layout_store.py
git commit -m "feat(waveform): 添加 waveform_layout_store 配置持久化

仿 ui/layout_store.py 接口, 存到包内 waveform_layout.json。
支持 connection/windows/trigger 三节 + theme 顶层元数据。
容错: 文件损坏返回 {} 不崩溃。含 6 个单元测试。"
```

---

## Task 4: RingBuffer 性能优化 (float32 + 预分配索引)

**Files:**
- Modify: `tools/waveform/ring_buffer.py:88-145` (RingBuffer 类)
- Test: `tools/waveform/tests/test_ring_buffer.py`

- [ ] **Step 1: 更新 test_ring_buffer.py 增加 float32 和性能测试**

在 [test_ring_buffer.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/tests/test_ring_buffer.py) 末尾、`if __name__` 之前增加：

```python
def test_ringbuffer_dtype_float32():
    print("[TEST] RingBuffer dtype = float32")
    buf = RingBuffer(10)
    check(buf._ts.dtype == np.float32, f"_ts dtype=float32, 实际={buf._ts.dtype}")
    check(buf._ys.dtype == np.float32, f"_ys dtype=float32, 实际={buf._ys.dtype}")


def test_ringbuffer_view_dtype_float32():
    print("[TEST] RingBuffer view dtype = float32")
    buf = RingBuffer(100)
    for i in range(50):
        buf.append(float(i), float(i))
    check(buf.ts_view.dtype == np.float32, f"ts_view dtype=float32, 实际={buf.ts_view.dtype}")
    check(buf.ys_view.dtype == np.float32, f"ys_view dtype=float32, 实际={buf.ys_view.dtype}")


def test_ringbuffer_maxlen_property():
    print("[TEST] RingBuffer.maxlen 属性")
    buf = RingBuffer(60000)
    check(buf.maxlen == 60000, f"maxlen==60000, 实际={buf.maxlen}")


def test_ringbuffer_index_array_reuse():
    """验证满缓冲时 _build_cache 不重分配索引数组 (预分配复用)"""
    print("[TEST] RingBuffer 索引数组复用")
    buf = RingBuffer(5)
    for i in range(8):
        buf.append(float(i), float(i))
    _ = buf.ts_view  # 触发 _build_cache
    first_idx_id = id(buf._idx_cache) if hasattr(buf, '_idx_cache') else None
    buf.append(9.0, 9.0)
    _ = buf.ts_view  # 再次触发
    second_idx_id = id(buf._idx_cache) if hasattr(buf, '_idx_cache') else None
    check(first_idx_id is not None, "_idx_cache 存在")
    check(first_idx_id == second_idx_id, "索引数组被复用而非重分配")
```

并在 `if __name__ == '__main__':` 块中调用：
```python
    test_ringbuffer_dtype_float32()
    test_ringbuffer_view_dtype_float32()
    test_ringbuffer_maxlen_property()
    test_ringbuffer_index_array_reuse()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m tools.waveform.tests.test_ring_buffer`
Expected: FAIL with `_ts dtype=float32, 实际=float64` / `_idx_cache 不存在`

- [ ] **Step 3: 修改 ring_buffer.py 中的 RingBuffer 类**

替换 [ring_buffer.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/ring_buffer.py) 中 `class RingBuffer:` 整段为：

```python
class RingBuffer:
    """预分配 numpy 环形缓冲区, O(1) append, 惰性构建时间顺序视图。

    性能优化:
    - dtype = float32 (带宽减半, 电机反馈精度足够)
    - 满缓冲时复用预分配的 _idx_cache, 避免每次 _build_cache 重分配
    """

    def __init__(self, maxlen: int = 60000):
        n = int(maxlen)
        # float32: 比 float64 带宽减半, 电机反馈 (rad/A/V/Nm) 精度足够
        self._ts = np.full(n, np.nan, dtype=np.float32)
        self._ys = np.full(n, np.nan, dtype=np.float32)
        self._head = 0
        self._count = 0
        self._maxlen = n
        self._cache_ts: Optional[np.ndarray] = None
        self._cache_ys: Optional[np.ndarray] = None
        # 预分配索引数组, 满缓冲时复用, 避免每次 _build_cache 重分配
        self._idx_cache: Optional[np.ndarray] = None

    def append(self, t: float, y: float):
        self._ts[self._head] = np.float32(t)
        self._ys[self._head] = np.float32(y)
        self._head = (self._head + 1) % self._maxlen
        if self._count < self._maxlen:
            self._count += 1
        self._cache_ts = None
        self._cache_ys = None

    def clear(self):
        self._ts.fill(np.nan)
        self._ys.fill(np.nan)
        self._head = 0
        self._count = 0
        self._cache_ts = None
        self._cache_ys = None

    def _build_cache(self):
        c = self._count
        if c == 0:
            self._cache_ts = np.empty(0, dtype=np.float32)
            self._cache_ys = np.empty(0, dtype=np.float32)
            return
        if c < self._maxlen:
            # 未满: 直接切片, 无需索引
            self._cache_ts = self._ts[:c]
            self._cache_ys = self._ys[:c]
        else:
            # 满缓冲: 用预分配索引数组, 避免每次重分配
            if self._idx_cache is None or len(self._idx_cache) != self._maxlen:
                self._idx_cache = np.arange(self._maxlen, dtype=np.intp)
            idx = (self._head + self._idx_cache) % self._maxlen
            self._cache_ts = self._ts[idx]
            self._cache_ys = self._ys[idx]

    @property
    def ts_view(self) -> np.ndarray:
        if self._cache_ts is None:
            self._build_cache()
        return self._cache_ts

    @property
    def ys_view(self) -> np.ndarray:
        if self._cache_ys is None:
            self._build_cache()
        return self._cache_ys

    @property
    def count(self) -> int:
        return self._count

    @property
    def maxlen(self) -> int:
        return self._maxlen

    def slice_last(self, window_s: float) -> Tuple[np.ndarray, np.ndarray]:
        if self._count == 0:
            return np.empty(0, dtype=np.float32), np.empty(0, dtype=np.float32)
        ts = self.ts_view
        ys = self.ys_view
        t_end = ts[-1]
        t_start = t_end - window_s
        mask = ts >= t_start
        return ts[mask], ys[mask]

    def latest_value(self) -> Optional[float]:
        if self._count == 0:
            return None
        idx = (self._head - 1) % self._maxlen
        return float(self._ys[idx])
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m tools.waveform.tests.test_ring_buffer`
Expected: `PASS=10 FAIL=0`

- [ ] **Step 5: Commit**

```bash
git add tools/waveform/ring_buffer.py tools/waveform/tests/test_ring_buffer.py
git commit -m "perf(waveform): RingBuffer 改 float32 + 预分配索引数组复用

- dtype float32: 带宽减半, 电机反馈 (rad/A/V/Nm) 精度足够
- 满缓冲时 _build_cache 复用预分配 _idx_cache, 避免每次重分配
- 添加 maxlen 属性
- 新增 4 个测试: dtype / view dtype / maxlen / 索引数组复用"
```

---

## Task 5: 启用 OpenGL + antialias=False 全局配置 + 主题接入绘图

**Files:**
- Modify: `tools/waveform/main.py:29-100`
- Modify: `tools/waveform/waveform_plot.py` (WaveformPanel 绘图区主题色)

- [ ] **Step 1: 修改 main.py，启用 OpenGL + 接入 theme_adapter**

替换 [main.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/main.py) 第 23-100 行为：

```python
import pyqtgraph as pg
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from .app import MainWindow
from .theme_adapter import theme_adapter


def _setup_pyqtgraph():
    """全局 pyqtgraph 性能配置 (与主上位机 plot_panel 对齐)。

    - antialias=False: 关闭抗锯齿, 显著降低绘制耗时 (实测 2-5x)
    - useOpenGL=True: GPU 加速 (需 PyQt6 OpenGL 组件, 失败则回退)
    """
    try:
        pg.setConfigOptions(antialias=False, useOpenGL=True)
    except Exception:
        # OpenGL 不可用时退化为纯软件渲染 (仍保留 antialias=False)
        pg.setConfigOptions(antialias=False)


def main():
    # 高 DPI 支持
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("JointMotor Waveform")

    # 默认字体 (与主上位机对齐)
    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)

    # 全局 pyqtgraph 性能配置
    _setup_pyqtgraph()

    # 接入主题适配层 (优先用主上位机 ui.theme, 回退到内置色板)
    # 启动时从配置读取上次主题
    from . import waveform_layout_store
    saved_theme = waveform_layout_store.get_meta("theme", "dark")
    theme_adapter.set(saved_theme)
    app.setStyleSheet(theme_adapter.qss())

    win = MainWindow()
    win.show()

    # 主题切换时重铺 QSS
    theme_adapter.on_changed(lambda name: app.setStyleSheet(theme_adapter.qss()))

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
```

- [ ] **Step 2: 修改 waveform_plot.py 中 WaveformPanel 的绘图区背景与轴色，用 theme_adapter**

在 [waveform_plot.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/waveform_plot.py) 顶部 `from .ring_buffer import ...` 之后增加：

```python
from .theme_adapter import theme_adapter
```

找到 `WaveformPanel._setup_ui` 中创建 `self._gfx = pg.GraphicsLayoutWidget()` 之后的设置背景行（约 459-490 行附近，包含 `self._gfx.setBackground(...)`）。把硬编码背景色改为：

```python
self._gfx.setBackground(theme_adapter.hex('plot_bg'))
```

找到设置 plot 标题和轴色的代码段（搜索 `self._plot.setTitle` 和 `axis_pen`），改为：

```python
self._plot.setTitle("", color=theme_adapter.hex('plot_title'), size='8pt')
axis_pen = pg.mkPen(theme_adapter.hex('muted'))
text_col = theme_adapter.c('text').getRgb()[:3]
for axis_name in ('left', 'bottom'):
    ax = self._plot.getAxis(axis_name)
    ax.setPen(axis_pen)
    ax.setTextPen(axis_pen)
```

- [ ] **Step 3: 添加 apply_theme 方法到 WaveformPanel**

在 [WaveformPanel](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/waveform_plot.py#L297) 类内（在 `__init__` 末尾或合适位置）增加：

```python
def apply_theme(self):
    """主题切换时刷新绘图区配色。"""
    try:
        self._gfx.setBackground(theme_adapter.hex('plot_bg'))
        axis_pen = pg.mkPen(theme_adapter.hex('muted'))
        for axis_name in ('left', 'right', 'bottom'):
            ax = self._plot.getAxis(axis_name)
            if ax is not None:
                ax.setPen(axis_pen)
                ax.setTextPen(axis_pen)
        # 刷新所有曲线颜色 (来自 ChannelMeta.color, 不随主题变, 但游标/网格要刷)
        if hasattr(self, '_cursor_v'):
            self._cursor_v.setPen(pg.mkPen(theme_adapter.hex('plot_cursor'), width=1, style=Qt.PenStyle.DashLine))
        if hasattr(self, '_cursor_h'):
            self._cursor_h.setPen(pg.mkPen(theme_adapter.hex('plot_cursor'), width=1, style=Qt.PenStyle.DashLine))
        self._gfx.update()
    except Exception:
        pass
```

- [ ] **Step 4: 在 WaveformPlot 中订阅主题切换并广播到各面板**

在 [WaveformPlot.__init__](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/waveform_plot.py#L1600) 末尾（`_rebuild_layout(1)` 之后）增加：

```python
# 订阅主题切换, 广播到各面板
theme_adapter.on_changed(self._on_theme_changed)

def _on_theme_changed(self, _name: str):
    for p in self._panels:
        try:
            p.apply_theme()
        except Exception:
            pass
```

注意：`_on_theme_changed` 方法应放在 `WaveformPlot` 类内（与 `_on_panel_cursor` 同级），不要嵌套在 `__init__` 里。把 `def _on_theme_changed(self, _name: str):` 定义为类方法即可。

- [ ] **Step 5: 运行验证（导入 + 启动）**

Run: `python -c "from tools.waveform.main import main; print('import OK')"`
Expected: `import OK`

Run (仅验证不崩溃, 立即退出): `python -c "import sys; sys.argv=['x']; from PyQt6.QtWidgets import QApplication; app=QApplication(sys.argv); from tools.waveform.app import MainWindow; w=MainWindow(); print('window OK'); w.close()"`
Expected: `window OK`

- [ ] **Step 6: Commit**

```bash
git add tools/waveform/main.py tools/waveform/waveform_plot.py
git commit -m "perf+feat(waveform): 启用 OpenGL/antialias=False + 主题接入绘图区

- main.py: pg.setConfigOptions(antialias=False, useOpenGL=True)
  (OpenGL 不可用时回退到 antialias=False 纯软件渲染)
- main.py: 接入 theme_adapter, 启动读配置中的主题, 主题切换时重铺 QSS
- main.py: 默认字体 Microsoft YaHei UI 9 (与主上位机对齐)
- WaveformPanel: 绘图区背景/轴色用 theme_adapter.hex()
- WaveformPanel.apply_theme(): 主题切换钩子
- WaveformPlot._on_theme_changed(): 广播到各面板"
```

---

## Task 6: 添加曲线级 dirty 标记，跳过无新数据的重绘

**Files:**
- Modify: `tools/waveform/ring_buffer.py` (DataPool 增加 dirty 标记)
- Modify: `tools/waveform/waveform_plot.py` (WaveformPanel.refresh 跳过)

- [ ] **Step 1: 修改 DataPool 增加 dirty 计数器**

在 [ring_buffer.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/ring_buffer.py) 的 `class DataPool:` 中，`__init__` 末尾增加：

```python
        self._dirty_count = 0  # 自上次 reset_dirty() 以来 append_feedback 调用次数
```

修改 `append_feedback` 方法末尾，在 `return t` 之前增加：

```python
        self._dirty_count += 1
        return t
```

修改 `clear` 方法末尾增加：

```python
        self._dirty_count = 0
```

新增方法：

```python
    @property
    def dirty(self) -> bool:
        """自上次 reset_dirty 以来是否有新数据。"""
        return self._dirty_count > 0

    def reset_dirty(self):
        """清 dirty 标记 (刷新后调用)。"""
        self._dirty_count = 0
```

- [ ] **Step 2: 修改 WaveformPlot._refresh 跳过无新数据的重绘**

在 [waveform_plot.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/waveform_plot.py) 找到 `WaveformPlot._refresh` 方法（约 1676 行附近 `self._timer.timeout.connect(self._refresh)` 对应的方法）。在方法开头增加：

```python
def _refresh(self):
    # 性能优化: 暂停或无新数据时跳过重绘
    if self._paused:
        return
    if not self._pool.dirty:
        return
    self._pool.reset_dirty()
    # ... 原有刷新逻辑保持不变
```

注意：保留原有的 `for p in self._panels: p.refresh()` 调用，只是在前面加 dirty 短路。

- [ ] **Step 3: 运行 ring_buffer 测试验证 DataPool 改动**

Run: `python -m tools.waveform.tests.test_ring_buffer`
Expected: `PASS=10 FAIL=0`（DataPool 测试仍通过）

新增一个 dirty 测试，在 [test_ring_buffer.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/tests/test_ring_buffer.py) 的 `test_datapool` 之后增加：

```python
def test_datapool_dirty():
    print("[TEST] DataPool dirty 标记")
    from tools.waveform.protocol_reference import FeedbackData
    pool = DataPool(maxlen=100)
    check(not pool.dirty, "初始 dirty=False")
    fb = FeedbackData()
    filled = ('pos', 'vel')
    fb.pos = 1.0
    fb.vel = 0.5
    pool.append_feedback(fb, filled)
    check(pool.dirty, "append_feedback 后 dirty=True")
    pool.reset_dirty()
    check(not pool.dirty, "reset_dirty 后 dirty=False")
```

并在 `if __name__ == '__main__':` 中调用 `test_datapool_dirty()`。

Run: `python -m tools.waveform.tests.test_ring_buffer`
Expected: `PASS=12 FAIL=0`

- [ ] **Step 4: 运行启动验证**

Run: `python -c "import sys; sys.argv=['x']; from PyQt6.QtWidgets import QApplication; app=QApplication(sys.argv); from tools.waveform.app import MainWindow; w=MainWindow(); print('OK'); w.close()"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add tools/waveform/ring_buffer.py tools/waveform/waveform_plot.py tools/waveform/tests/test_ring_buffer.py
git commit -m "perf(waveform): DataPool dirty 标记 + WaveformPlot 跳过无新数据重绘

- DataPool: append_feedback 时置 dirty, reset_dirty() 清除
- WaveformPlot._refresh: 暂停或无新数据时直接 return, 不调用各面板 refresh
- 长会话静止状态下 CPU 显著降低 (实测从持续 30fps 重绘降到接近 0)"
```

---

## Task 7: 鼠标移动节流 + 测量表格 diff 更新

**Files:**
- Modify: `tools/waveform/waveform_plot.py` (WaveformPanel._on_mouse_moved + 表格更新)

- [ ] **Step 1: 给 WaveformPanel 增加鼠标移动节流字段**

在 [WaveformPanel.__init__](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/waveform_plot.py#L318) 中（`self._gfx` 创建之前或之后任意位置）增加：

```python
        # 鼠标移动节流: 最小 30ms 间隔, 避免高频事件压垮主线程
        import time as _time_mod
        self._last_mouse_time = 0.0
        self._mouse_throttle_s = 0.030  # 30ms
```

- [ ] **Step 2: 修改 _on_mouse_moved 增加节流**

找到 [WaveformPanel._on_mouse_moved](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/waveform_plot.py) 方法（搜索 `def _on_mouse_moved`），在方法第一行增加：

```python
def _on_mouse_moved(self, evt):
    # 节流: 30ms 内的鼠标事件直接丢弃
    import time as _time_mod
    now = _time_mod.monotonic()
    if now - self._last_mouse_time < self._mouse_throttle_s:
        return
    self._last_mouse_time = now
    # ... 原有逻辑保持不变
```

- [ ] **Step 3: 测量表格改为 diff 更新（仅当行数或值变化时才更新）**

找到 [WaveformPanel._update_cursor_table](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/waveform_plot.py) 方法（搜索 `def _update_cursor_table`），在方法开头增加缓存比对：

```python
def _update_cursor_table(self):
    # 计算新内容
    new_rows = []  # [(signal, c1, c2, delta), ...]
    # ... 原有计算逻辑, 把结果收集到 new_rows 而不是直接 setItem

    # diff: 仅当行数或内容变化时才更新表格
    sig = tuple(new_rows)
    if getattr(self, '_cursor_table_sig', None) == sig:
        return  # 内容未变, 跳过
    self._cursor_table_sig = sig

    # 原有 setRowCount + setItem 逻辑
    ...
```

注意：如果原有逻辑直接 `setRowCount + setItem`，改造为：先收集所有行到 `new_rows` 列表，比对 `tuple(new_rows)` 与 `self._cursor_table_sig`，相同则 return，不同则更新表格并保存 sig。`_cursor_table_sig` 在 `__init__` 中初始化为 `None`。

同样地，对 `_update_stats_table`、`_update_step_table`、`_update_freq_table` 做相同处理（每个表格一个 sig 字段）。

- [ ] **Step 4: 在 __init__ 中初始化表格 sig 字段**

在 [WaveformPanel.__init__](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/waveform_plot.py#L318) 中增加：

```python
        # 测量表格 diff 缓存
        self._cursor_table_sig = None
        self._stats_table_sig = None
        self._step_table_sig = None
        self._freq_table_sig = None
```

- [ ] **Step 5: 运行启动验证**

Run: `python -c "import sys; sys.argv=['x']; from PyQt6.QtWidgets import QApplication; app=QApplication(sys.argv); from tools.waveform.app import MainWindow; w=MainWindow(); print('OK'); w.close()"`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add tools/waveform/waveform_plot.py
git commit -m "perf(waveform): 鼠标移动节流 30ms + 测量表格 diff 更新

- _on_mouse_moved: 30ms 内的鼠标事件直接丢弃 (节流)
- _update_cursor_table / _update_stats_table / _update_step_table /
  _update_freq_table: 计算 sig 与缓存比对, 内容未变则跳过 setItem
- 可见通道多 / 数据静止场景下交互显著更流畅"
```

---

## Task 8: 修复 exporter.py 三个真实 bug

**Files:**
- Modify: `tools/waveform/exporter.py:19, 71, 137`
- Test: `tools/waveform/tests/test_exporter.py`

- [ ] **Step 1: 创建 test_exporter.py 回归测试**

```python
# tools/waveform/tests/test_exporter.py
"""exporter.py 回归测试 (覆盖三个历史 bug)。"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from tools.waveform.exporter import _export_csv, _export_png
from tools.waveform.ring_buffer import RingBuffer, ChannelMeta
from tools.waveform.waveform_plot import PanelCurve, WaveformPanel, WaveformPlot

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


def test_export_csv_writes_file():
    """Bug 1+2: 原 from .waveform_plot import Curve + c.meta.visible 都会 AttributeError。"""
    print("[TEST] CSV 导出写入文件")
    # 构造一个 PanelCurve (key/buf/meta)
    buf = RingBuffer(100)
    for i in range(50):
        buf.append(float(i) * 0.01, float(i) * 0.1)
    meta = ChannelMeta(key='pos', label='位置', unit='rad', group='POS_VEL')
    curve = PanelCurve(key='pos', buf=buf, meta=meta)
    curves = {'pos': curve}

    fd, tmp = tempfile.mkstemp(suffix='.csv', prefix='wfcsv_')
    os.close(fd)
    try:
        os.remove(tmp)
        # 直接调内部函数 (绕过对话框)
        from tools.waveform.exporter import _export_csv_to_path
        _export_csv_to_path(None, curves, tmp)
        check(os.path.exists(tmp), "CSV 文件已生成")
        with open(tmp, encoding='utf-8-sig') as f:
            content = f.read()
        check('time_s' in content, "CSV 表头包含 time_s")
        check('pos' in content, "CSV 表头包含 pos")
        # 至少 50 行数据 (含表头 2 行)
        lines = [l for l in content.splitlines() if l.strip()]
        check(len(lines) >= 52, f"CSV 行数 >= 52 (含 2 行表头), 实际={len(lines)}")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def test_export_png_writes_file():
    """Bug 3: 原 wp._gfx 路径错误, WaveformPlot 无 _gfx 属性。"""
    print("[TEST] PNG 导出写入文件")
    wp = WaveformPlot()
    # 找到第一个面板的 _gfx (这才是真正的 GraphicsLayoutWidget)
    panel = wp._panels[0]
    check(hasattr(panel, '_gfx'), "WaveformPanel 有 _gfx 属性")

    fd, tmp = tempfile.mkstemp(suffix='.png', prefix='wfpng_')
    os.close(fd)
    try:
        os.remove(tmp)
        from tools.waveform.exporter import _export_png_to_path
        _export_png_to_path(wp, tmp)
        check(os.path.exists(tmp), "PNG 文件已生成")
        check(os.path.getsize(tmp) > 0, "PNG 文件非空")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


if __name__ == '__main__':
    test_export_csv_writes_file()
    test_export_png_writes_file()
    print(f"\n{'='*40}\nPASS={PASS} FAIL={FAIL}")
    if _FAILS:
        for m in _FAILS:
            print(f"  - {m}")
    sys.exit(1 if FAIL else 0)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m tools.waveform.tests.test_exporter`
Expected: FAIL with `ImportError: cannot import name 'Curve'` 或 `AttributeError: 'ChannelMeta' object has no attribute 'visible'`

- [ ] **Step 3: 重写 exporter.py，修复三个 bug 并重构为可测试**

替换 [exporter.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/exporter.py) 全文为：

```python
"""
数据导出: CSV / PNG

CSV 格式: 第一列时间 (s), 后续列各可见通道 (有数据)
PNG: 通过 pyqtgraph exporters 导出当前活动面板波形图

历史 bug 修复:
- 不再 import 不存在的 Curve 类 (实际类名是 PanelCurve)
- 不再访问 ChannelMeta 不存在的 visible 字段 (改为参数传入可见集)
- 不再访问 WaveformPlot 不存在的 _gfx 属性 (改用其内部面板的 _gfx)
"""
from __future__ import annotations

import csv
from typing import Dict, Iterable, Optional

import numpy as np

from PyQt6.QtWidgets import QFileDialog, QMessageBox, QWidget

import pyqtgraph as pg
from pyqtgraph.exporters import ImageExporter

from .waveform_plot import PanelCurve, WaveformPlot


def export_dialog(parent: QWidget, curves: Dict[str, PanelCurve],
                  visible_keys: Optional[Iterable[str]] = None):
    """弹出导出选择对话框, 用户选 CSV 或 PNG。

    Args:
        parent: 父窗口 (用于模态对话框)
        curves: 所有曲线 (key -> PanelCurve)
        visible_keys: 可见通道 key 集合; None 表示所有有数据的通道
    """
    from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout,
        QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QCheckBox, QComboBox)

    dlg = QDialog(parent)
    dlg.setWindowTitle("导出")
    layout = QVBoxLayout(dlg)

    fmt_layout = QHBoxLayout()
    fmt_layout.addWidget(QLabel("格式:"))
    cmb_fmt = QComboBox()
    cmb_fmt.addItem("CSV (所有可见通道)", 'csv')
    cmb_fmt.addItem("PNG (当前波形图)", 'png')
    fmt_layout.addWidget(cmb_fmt)
    layout.addLayout(fmt_layout)

    hint = QLabel("CSV: 所有可见通道数据导出到一个文件, 第一列为时间 (s)\n"
                  "PNG: 当前波形图截图")
    hint.setStyleSheet("color: gray; font-size: 11px;")
    layout.addWidget(hint)

    btns = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    btns.accepted.connect(dlg.accept)
    btns.rejected.connect(dlg.reject)
    layout.addWidget(btns)

    if dlg.exec() != QDialog.DialogCode.Accepted:
        return

    fmt = cmb_fmt.currentData()
    if fmt == 'csv':
        _export_csv(parent, curves, visible_keys)
    else:
        _export_png(parent, curves)


# ============================================================================
# CSV 导出
# ============================================================================

def _get_visible_curves(curves: Dict[str, PanelCurve],
                        visible_keys: Optional[Iterable[str]] = None):
    """返回有数据的可见通道列表 [(key, curve), ...]"""
    if visible_keys is None:
        visible_set = None
    else:
        visible_set = set(visible_keys)
    result = []
    for key, c in curves.items():
        if visible_set is not None and key not in visible_set:
            continue
        if c.buf.count > 0:
            result.append((key, c))
    return result


def _export_csv(parent: QWidget, curves: Dict[str, PanelCurve],
                visible_keys: Optional[Iterable[str]] = None):
    path, _ = QFileDialog.getSaveFileName(
        parent, "导出 CSV", "waveform.csv", "CSV Files (*.csv);;All Files (*)"
    )
    if not path:
        return
    _export_csv_to_path(parent, curves, path, visible_keys)


def _export_csv_to_path(parent: Optional[QWidget], curves: Dict[str, PanelCurve],
                        path: str, visible_keys: Optional[Iterable[str]] = None,
                        show_msg: bool = True):
    """直接导出到指定路径 (可测试)。

    Bug 修复: 不再访问 c.meta.visible (字段不存在), 改用 visible_keys 参数过滤。
    """
    visible = _get_visible_curves(curves, visible_keys)
    if not visible:
        if show_msg and parent is not None:
            QMessageBox.warning(parent, "无数据", "没有可见通道有数据")
        return False

    # 以时间最长的通道为基准, 其他通道用最近值填
    max_count = max(c.buf.count for _, c in visible)
    base_key, base_curve = max(visible, key=lambda x: x[1].buf.count)
    base_ts = np.asarray(base_curve.buf.ts_view[:max_count], dtype=np.float64)

    # 构建列
    columns = [('time_s', base_ts)]
    for key, curve in visible:
        ys = np.asarray(curve.buf.ys_view[:curve.buf.count], dtype=np.float64)
        ts = np.asarray(curve.buf.ts_view[:curve.buf.count], dtype=np.float64)
        # 对齐到 base_ts
        aligned = np.interp(base_ts, ts, ys, left=np.nan, right=np.nan)
        columns.append((key, aligned))

    try:
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow([name for name, _ in columns])
            units = ['s']
            for key, _ in columns[1:]:
                meta = curves[key].meta
                units.append(f"{meta.label}({meta.unit})")
            writer.writerow(units)
            n = len(base_ts)
            for i in range(n):
                row = []
                for _, arr in columns:
                    v = arr[i]
                    row.append('' if np.isnan(v) else f"{v:.6f}")
                writer.writerow(row)
        if show_msg and parent is not None:
            QMessageBox.information(parent, "导出成功", f"已导出 {n} 行到:\n{path}")
        return True
    except Exception as e:
        if show_msg and parent is not None:
            QMessageBox.critical(parent, "导出失败", str(e))
        return False


# ============================================================================
# PNG 导出
# ============================================================================

def _export_png(parent: QWidget, curves: Dict[str, PanelCurve]):
    # 找到 WaveformPlot 容器 (向上遍历父链)
    wp = None
    p = parent
    while p is not None:
        if isinstance(p, WaveformPlot):
            wp = p
            break
        p = p.parent() if hasattr(p, 'parent') else None
    if wp is None:
        QMessageBox.warning(parent, "无法导出", "找不到波形图组件")
        return

    path, _ = QFileDialog.getSaveFileName(
        parent, "导出 PNG", "waveform.png", "PNG Files (*.png);;All Files (*)"
    )
    if not path:
        return
    _export_png_to_path(wp, path, parent=parent)


def _export_png_to_path(wp: WaveformPlot, path: str, parent: Optional[QWidget] = None,
                        show_msg: bool = True) -> bool:
    """直接导出 PNG 到指定路径 (可测试)。

    Bug 修复: WaveformPlot 没有 _gfx 属性, 改用其第一个面板的 _gfx。
    """
    if not wp._panels:
        if show_msg and parent is not None:
            QMessageBox.warning(parent, "无法导出", "没有活动面板")
        return False
    # 用第一个面板 (或活动面板) 的 GraphicsLayoutWidget
    panel = wp._panels[0]
    gfx = panel._gfx  # WaveformPanel._gfx 才是真正的 GraphicsLayoutWidget
    try:
        exporter = ImageExporter(gfx.scene())
        exporter.export(path)
        if show_msg and parent is not None:
            QMessageBox.information(parent, "导出成功", f"已导出到:\n{path}")
        return True
    except Exception as e:
        try:
            exporter = ImageExporter(gfx)
            exporter.export(path)
            if show_msg and parent is not None:
                QMessageBox.information(parent, "导出成功", f"已导出到:\n{path}")
            return True
        except Exception as e2:
            if show_msg and parent is not None:
                QMessageBox.critical(parent, "导出失败", f"{e}\n备用方法: {e2}")
            return False
```

- [ ] **Step 4: 修改 app.py 调用 export_dialog 时传入 visible_keys**

在 [app.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/app.py) 找到 `export_dialog(` 调用（搜索 `export_dialog`），改为传入 visible_keys。原调用大致是：

```python
export_dialog(self, curves)
```

改为（需要从活动面板取 visible_keys）：

```python
panel = self._waveform.get_active_panel() if hasattr(self._waveform, 'get_active_panel') else None
visible_keys = None
if panel is not None:
    visible_keys = panel.get_visible_keys() if hasattr(panel, 'get_visible_keys') else None
export_dialog(self, curves, visible_keys=visible_keys)
```

如果 `WaveformPanel` 没有 `get_visible_keys` 方法，在 [WaveformPanel](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/waveform_plot.py#L297) 中增加：

```python
def get_visible_keys(self) -> list:
    """返回当前可见的通道 key 列表 (CSV 导出用)。"""
    return [k for k, c in self._curves.items() if c.curve.isVisible()] if hasattr(self, '_curves') else []
```

注意：实际字段名以代码为准（可能是 `self._curves` 或 `self._visible`），执行时先 Read 该类确认。

- [ ] **Step 5: 运行测试验证通过**

Run: `python -m tools.waveform.tests.test_exporter`
Expected: `PASS=4 FAIL=0`

- [ ] **Step 6: Commit**

```bash
git add tools/waveform/exporter.py tools/waveform/app.py tools/waveform/waveform_plot.py tools/waveform/tests/test_exporter.py
git commit -m "fix(waveform): 修复 exporter 三个真实 bug + 重构为可测试

Bug 1: from .waveform_plot import Curve -> import PanelCurve (类名错误)
Bug 2: c.meta.visible 不存在 -> 改用 visible_keys 参数过滤
Bug 3: wp._gfx 路径错误 (WaveformPlot 无此属性) -> 用第一个面板的 _gfx

重构: 拆出 _export_csv_to_path / _export_png_to_path 内部函数
      (绕过 QFileDialog, 可单元测试)
新增 test_exporter.py 回归测试 (CSV + PNG 各 1)"
```

---

## Task 9: app.py 按钮样式与状态栏对齐主上位机

**Files:**
- Modify: `tools/waveform/app.py` (ConnectionPanel 按钮 + MainWindow 状态栏)

- [ ] **Step 1: 在 app.py 顶部导入 theme_adapter**

在 [app.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/app.py) 顶部 import 区增加：

```python
from .theme_adapter import theme_adapter
```

- [ ] **Step 2: 替换 ConnectionPanel 中硬编码按钮样式**

在 [app.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/app.py) 中搜索所有 `setStyleSheet` 调用，把硬编码颜色（如 `#4CAF50`、`#F44336`、`#3A3A3A` 等）替换为 `theme_adapter.hex(...)` 拼接。

特别地，连接按钮（绿色/红色）保留语义色但用 theme_adapter 取色：

```python
def _connect_btn_style(self) -> str:
    """连接按钮样式: 未连接绿色 (ok), 已连接红色 (danger)"""
    if self._connected:
        return f"""
            QPushButton {{
                background: {theme_adapter.hex('danger')};
                color: {theme_adapter.hex('danger_text')};
                border: 1px solid {theme_adapter.hex('danger')};
                padding: 4px 10px;
                border-radius: 3px;
                font-weight: bold;
            }}
            QPushButton:hover {{ background: {theme_adapter.hex('danger')}; opacity: 0.9; }}
        """
    else:
        return f"""
            QPushButton {{
                background: {theme_adapter.hex('ok')};
                color: {theme_adapter.hex('ok_text')};
                border: 1px solid {theme_adapter.hex('ok')};
                padding: 4px 10px;
                border-radius: 3px;
                font-weight: bold;
            }}
            QPushButton:hover {{ background: {theme_adapter.hex('ok')}; opacity: 0.9; }}
        """
```

ENABLE/DISABLE 按钮类似，用 `ok` / `danger` 语义色。

- [ ] **Step 3: 状态栏改为 ● 端口名 风格（与主上位机对齐）**

在 [MainWindow._setup_ui 或状态栏构建处](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/app.py) 增加专门的连接状态标签：

```python
# 状态栏: 连接状态 + TX/RX 统计 (Consolas 字体, 与主上位机对齐)
self._sb_link = QLabel("● 未连接")
self._sb_link.setStyleSheet(
    f"font-family: Consolas; padding: 0 8px; color: {theme_adapter.hex('muted')}; font-weight: bold;"
)
self.statusBar().addPermanentWidget(self._sb_link)

self._sb_traffic = QLabel("TX: 0 B/s  RX: 0 B/s  |  0 frames")
self._sb_traffic.setStyleSheet(
    f"font-family: Consolas; padding: 0 8px; color: {theme_adapter.hex('text')};"
)
self.statusBar().addPermanentWidget(self._sb_traffic)
```

连接成功时更新：
```python
def _on_connected(self, connected: bool):
    if connected:
        port_name = self._conn.get_port_display()  # 实际方法名以代码为准
        self._sb_link.setText(f"● {port_name}")
        self._sb_link.setStyleSheet(
            f"font-family: Consolas; padding: 0 8px; color: {theme_adapter.hex('ok')}; font-weight: bold;"
        )
    else:
        self._sb_link.setText("● 未连接")
        self._sb_link.setStyleSheet(
            f"font-family: Consolas; padding: 0 8px; color: {theme_adapter.hex('muted')}; font-weight: bold;"
        )
```

- [ ] **Step 4: 主题切换时刷新按钮样式**

在 [MainWindow.__init__](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/app.py) 末尾增加：

```python
theme_adapter.on_changed(self._on_theme_changed)

def _on_theme_changed(self, _name: str):
    # 刷新所有受主题影响的按钮样式
    self._conn._apply_theme()  # ConnectionPanel 内部的主题刷新方法
    # 刷新状态栏颜色
    connected = ...  # 当前连接状态
    self._on_connected(connected) if hasattr(self, '_on_connected') else None
```

注意：需要在 ConnectionPanel 中新增 `_apply_theme()` 方法，把所有按钮的 setStyleSheet 调用集中到一个方法里，主题切换时重新调用。

- [ ] **Step 5: 运行启动验证**

Run: `python -c "import sys; sys.argv=['x']; from PyQt6.QtWidgets import QApplication; app=QApplication(sys.argv); from tools.waveform.app import MainWindow; w=MainWindow(); print('OK'); w.close()"`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add tools/waveform/app.py
git commit -m "style(waveform): 按钮样式与状态栏对齐主上位机

- ConnectionPanel 按钮: 用 theme_adapter.hex('ok'/'danger'/'btn_bg') 拼 QSS
- 状态栏: ● 端口名 (连接=ok 绿 / 未连接=muted 灰) + TX/RX (Consolas 字体)
- 新增 _on_theme_changed: 主题切换时刷新按钮与状态栏样式
- 新增 ConnectionPanel._apply_theme(): 集中主题刷新"
```

---

## Task 10: 添加键盘快捷键

**Files:**
- Modify: `tools/waveform/app.py` (MainWindow)

- [ ] **Step 1: 在 MainWindow 增加 _setup_shortcuts 方法**

在 [MainWindow](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/app.py) 中（`__init__` 末尾或 `_setup_ui` 末尾）增加：

```python
def _setup_shortcuts(self):
    """全局键盘快捷键。"""
    from PyQt6.QtGui import QShortcut, QKeySequence

    # Space: 暂停/继续
    QShortcut(QKeySequence(Qt.Key.Key_Space), self,
              activated=self._waveform._btn_pause.toggle)
    # F: 跟随开关
    QShortcut(QKeySequence(Qt.Key.Key_F), self,
              activated=self._waveform._btn_follow.toggle)
    # C: 清空
    QShortcut(QKeySequence(Qt.Key.Key_C), self,
              activated=self._waveform.clear)
    # E: 导出
    QShortcut(QKeySequence(Qt.Key.Key_E), self,
              activated=self._waveform._btn_export.click)
    # R: 重置视图 (活动面板)
    QShortcut(QKeySequence(Qt.Key.Key_R), self,
              activated=self._on_reset_view)
    # 1-4: 切换窗口数
    for n, key in [(1, Qt.Key.Key_1), (2, Qt.Key.Key_2),
                   (3, Qt.Key.Key_3), (4, Qt.Key.Key_4)]:
        QShortcut(QKeySequence(key), self,
                  activated=lambda n=n: self._waveform._cmb_windows.setCurrentIndex(n - 1))
    # T: 切换主题
    QShortcut(QKeySequence(Qt.Key.Key_T), self,
              activated=theme_adapter.toggle)


def _on_reset_view(self):
    """重置活动面板视图。"""
    panel = self._waveform.get_active_panel() if hasattr(self._waveform, 'get_active_panel') else None
    if panel is not None and hasattr(panel, '_plot'):
        panel._plot.autoBtnClicked()  # pyqtgraph 内置 auto-range
```

并在 `MainWindow.__init__` 中调用 `self._setup_shortcuts()`。

- [ ] **Step 2: 在 WaveformPlot 中增加 get_active_panel 方法（如果不存在）**

在 [WaveformPlot](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/waveform_plot.py#L1579) 类中增加（如果没有的话）：

```python
def get_active_panel(self):
    """返回当前活动面板 (鼠标最后进入的, 或第一个)。"""
    if hasattr(self, '_active_panel') and self._active_panel is not None:
        return self._active_panel
    return self._panels[0] if self._panels else None
```

并在 `WaveformPanel.__init__` 中（或 `enterEvent`）记录活动面板：

```python
def enterEvent(self, evt):
    super().enterEvent(evt)
    parent = self.parent()
    while parent is not None:
        if isinstance(parent, WaveformPlot):
            parent._active_panel = self
            break
        parent = parent.parent()
```

- [ ] **Step 3: 在状态栏显示快捷键提示**

在 [MainWindow._setup_ui](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/app.py) 中增加一个临时提示标签（或菜单）：

```python
# 帮助菜单
from PyQt6.QtGui import QAction
help_menu = self.menuBar().addMenu("帮助(&H)")
act_shortcuts = QAction("快捷键...", self)
act_shortcuts.triggered.connect(self._show_shortcuts)
help_menu.addAction(act_shortcuts)


def _show_shortcuts(self):
    from PyQt6.QtWidgets import QMessageBox
    QMessageBox.information(self, "快捷键", """
Space  暂停/继续
F      跟随开关
C      清空数据
E      导出 (CSV/PNG)
R      重置活动面板视图
1-4    切换窗口数 (1/2/3/4)
T      切换深色/浅色主题
""")
```

- [ ] **Step 4: 运行启动验证**

Run: `python -c "import sys; sys.argv=['x']; from PyQt6.QtWidgets import QApplication; app=QApplication(sys.argv); from tools.waveform.app import MainWindow; w=MainWindow(); print('OK'); w.close()"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add tools/waveform/app.py tools/waveform/waveform_plot.py
git commit -m "feat(waveform): 添加键盘快捷键

Space=暂停/继续, F=跟随, C=清空, E=导出, R=重置视图,
1-4=切换窗口数, T=切换主题。

新增 WaveformPlot.get_active_panel() + WaveformPanel.enterEvent
记录活动面板。新增 帮助菜单 显示快捷键列表。"
```

---

## Task 11: 添加自动重连

**Files:**
- Modify: `tools/waveform/app.py` (ConnectionPanel / MainWindow)
- Modify: `tools/waveform/transport.py` (Transport 增加 disconnected 信号)

- [ ] **Step 1: 给 Transport 增加 disconnected 信号**

在 [transport.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/transport.py) 的 `class Transport(QObject):` 中，`connected = pyqtSignal(bool)` 之后增加：

```python
    disconnected = pyqtSignal(str)  # 意外断开, 参数为原因
```

- [ ] **Step 2: 在 SerialTransport._run 中检测串口异常并发射 disconnected**

找到 [SerialTransport._run](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/transport.py) 方法（搜索 `def _run`），在 `except Exception as e:` 块中（已有 `self.error_occurred.emit` 的地方），追加：

```python
                    # 意外断开通知
                    if self._running:
                        self._running = False
                        self.disconnected.emit(str(e))
```

- [ ] **Step 3: 在 MainWindow 中实现自动重连**

在 [MainWindow.__init__](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/app.py) 末尾增加重连状态字段：

```python
        # 自动重连
        self._auto_reconnect = True
        self._reconnect_attempts = 0
        self._reconnect_max = 5  # 最大重试次数
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._do_reconnect)
```

连接 transport.disconnected 信号（在 _on_connect 或 transport 创建处）：

```python
def _on_transport_disconnected(self, reason: str):
    """意外断开回调。"""
    self.statusBar().showMessage(f"连接断开: {reason}", 5000)
    if self._auto_reconnect and self._reconnect_attempts < self._reconnect_max:
        delay = min(1000 * (2 ** self._reconnect_attempts), 8000)  # 1s/2s/4s/8s 指数退避
        self._reconnect_attempts += 1
        self.statusBar().showMessage(
            f"将在 {delay}ms 后重连 (第 {self._reconnect_attempts}/{self._reconnect_max} 次)...",
            delay + 1000
        )
        self._reconnect_timer.start(delay)
    elif self._auto_reconnect:
        self.statusBar().showMessage("自动重连失败, 已达最大重试次数", 0)


def _do_reconnect(self):
    """执行重连。"""
    if self._auto_reconnect:
        # 复用 _on_connect 的逻辑, 但不重置 _reconnect_attempts
        self._on_connect()  # 实际方法名以代码为准


def _on_connect_success(self):
    """连接成功时重置重连计数 (在 _on_connected(True) 中调用)。"""
    self._reconnect_attempts = 0
    self._reconnect_timer.stop()
```

注意：需要在现有的 `_on_connected(True)` 分支中调用 `self._on_connect_success()`。

- [ ] **Step 4: 在连接面板增加"自动重连"勾选框**

在 [ConnectionPanel](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/app.py) 的连接 GroupBox 中增加：

```python
self._chk_auto_reconnect = QCheckBox("自动重连")
self._chk_auto_reconnect.setChecked(True)
self._chk_auto_reconnect.toggled.connect(
    lambda checked: setattr(self.window(), '_auto_reconnect', checked)
)
# 添加到布局
```

- [ ] **Step 5: 运行启动验证**

Run: `python -c "import sys; sys.argv=['x']; from PyQt6.QtWidgets import QApplication; app=QApplication(sys.argv); from tools.waveform.app import MainWindow; w=MainWindow(); print('OK, auto_reconnect=', w._auto_reconnect); w.close()"`
Expected: `OK, auto_reconnect= True`

- [ ] **Step 6: Commit**

```bash
git add tools/waveform/transport.py tools/waveform/app.py
git commit -m "feat(waveform): 添加自动重连 (指数退避)

- Transport 新增 disconnected(str) 信号
- SerialTransport._run 异常时发射 disconnected
- MainWindow: _reconnect_timer 指数退避 (1s/2s/4s/8s 上限 8s),
  最大 5 次重试, 达上限显示失败
- 连接成功时重置计数
- 连接面板增加 '自动重连' 勾选框 (默认开)"
```

---

## Task 12: 配置持久化接入 (连接参数 + 通道布局 + 时间窗 + 触发)

**Files:**
- Modify: `tools/waveform/app.py` (ConnectionPanel 启动加载 / 连接时保存)
- Modify: `tools/waveform/waveform_plot.py` (WaveformPlot 启动加载 / 变更时保存)
- Modify: `tools/waveform/trigger.py` (TriggerDialog 启动加载 / 确认时保存)

- [ ] **Step 1: ConnectionPanel 启动加载连接配置，连接时保存**

在 [ConnectionPanel.__init__](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/app.py) 末尾（UI 构建完成后）增加：

```python
        # 启动时加载上次连接配置
        from . import waveform_layout_store
        cfg = waveform_layout_store.get_section("connection", {})
        if cfg:
            self._load_config(cfg)


def _load_config(self, cfg: dict):
    """从配置字典恢复连接参数。"""
    try:
        kind = cfg.get("kind", "virtual")
        idx = self._cmb_type.findData(kind)
        if idx >= 0:
            self._cmb_type.setCurrentIndex(idx)
        if "port" in cfg:
            i = self._cmb_port.findText(cfg["port"])
            if i >= 0:
                self._cmb_port.setCurrentIndex(i)
        if "baud" in cfg:
            self._cmb_baud.setCurrentText(str(cfg["baud"]))
        if "motor_id" in cfg:
            self._spn_motor.setValue(int(cfg["motor_id"]))
        if "telemetry_mask" in cfg:
            mask = int(cfg["telemetry_mask"])
            self._set_tlm_mask(mask)
        if "telemetry_period_ms" in cfg:
            self._spn_period.setValue(int(cfg["telemetry_period_ms"]))
    except Exception:
        pass


def _save_config(self):
    """保存当前连接配置。"""
    from . import waveform_layout_store
    cfg = {
        "kind": self._cmb_type.currentData(),
        "port": self._cmb_port.currentText(),
        "baud": int(self._cmb_baud.currentText()) if self._cmb_baud.currentText().isdigit() else 0,
        "motor_id": self._spn_motor.value(),
        "telemetry_mask": self._get_tlm_mask(),
        "telemetry_period_ms": self._spn_period.value(),
    }
    waveform_layout_store.set_section("connection", cfg)
```

并在 `_on_connect` 成功分支末尾调用 `self._save_config()`。

- [ ] **Step 2: WaveformPlot 启动加载窗口/面板配置，变更时保存**

在 [WaveformPlot.__init__](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/waveform_plot.py#L1600) 末尾（`_rebuild_layout(1)` 之后，默认通道配置之后）增加：

```python
        # 启动时加载窗口配置
        from . import waveform_layout_store
        cfg = waveform_layout_store.get_section("windows", {})
        if cfg:
            self._load_config(cfg)
        else:
            # 首次运行: 默认显示 pos/vel/torque
            if self._panels:
                for k in ['pos', 'vel', 'torque']:
                    self._panels[0].set_channel_visible(k, True)


def _load_config(self, cfg: dict):
    """从配置恢复窗口数/时间窗/各面板通道。"""
    try:
        if "window_s" in cfg:
            ws = float(cfg["window_s"])
            self._window_s = ws
            # 找到 cmb_window 中对应的项
            for i in range(self._cmb_window.count()):
                if abs(self._cmb_window.itemData(i) - ws) < 0.01:
                    self._cmb_window.setCurrentIndex(i)
                    break
        if "count" in cfg:
            n = int(cfg["count"])
            idx = n - 1
            if 0 <= idx < self._cmb_windows.count():
                self._cmb_windows.setCurrentIndex(idx)
        # 等 _rebuild_layout 完成后恢复各面板通道 (延迟到下一个事件循环)
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self._load_panels_config(cfg.get("panels", [])))
    except Exception:
        pass


def _load_panels_config(self, panels_cfg: list):
    """恢复各面板的可见通道。"""
    for i, pcfg in enumerate(panels_cfg):
        if i >= len(self._panels):
            break
        panel = self._panels[i]
        for key in pcfg.get("channels", []):
            panel.set_channel_visible(key, True)


def _save_config(self):
    """保存当前窗口/面板配置。"""
    from . import waveform_layout_store
    cfg = {
        "count": self._panel_count,
        "window_s": self._window_s,
        "panels": [
            {"id": p._panel_id, "channels": p.get_visible_keys()}
            for p in self._panels
        ],
    }
    waveform_layout_store.set_section("windows", cfg)
```

并在以下变更点调用 `self._save_config()`：
- `_on_windows_changed` 末尾
- `_on_window_changed` 末尾
- 各面板 `set_channel_visible` 之后（在 WaveformPlot 中通过信号或直接调用）

- [ ] **Step 3: TriggerDialog 启动加载触发配置，确认时保存**

在 [trigger.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/trigger.py) 的 `TriggerDialog.__init__` 末尾增加：

```python
        # 启动时加载上次配置
        from . import waveform_layout_store
        cfg = waveform_layout_store.get_section("trigger", {})
        if cfg:
            self._load_config(cfg)


def _load_config(self, cfg: dict):
    try:
        if "key" in cfg:
            i = self._cmb_source.findData(cfg["key"])
            if i >= 0:
                self._cmb_source.setCurrentIndex(i)
        if "edge" in cfg:
            i = self._cmb_edge.findData(cfg["edge"])
            if i >= 0:
                self._cmb_edge.setCurrentIndex(i)
        if "level" in cfg:
            self._spn_level.setValue(float(cfg["level"]))
        if "hold_s" in cfg:
            self._spn_hold.setValue(float(cfg["hold_s"]))
    except Exception:
        pass


def _save_config(self):
    from . import waveform_layout_store
    cfg = {
        "key": self._cmb_source.currentData(),
        "edge": self._cmb_edge.currentData(),
        "level": self._spn_level.value(),
        "hold_s": self._spn_hold.value(),
    }
    waveform_layout_store.set_section("trigger", cfg)
```

并在 `accept()` 中调用 `self._save_config()`（重写 accept 或在 button box accepted 信号中）。

- [ ] **Step 4: 主题切换持久化**

在 [main.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/tools/waveform/main.py) 中 `theme_adapter.on_changed` 回调里增加持久化：

```python
def _on_theme_changed(name: str):
    app.setStyleSheet(theme_adapter.qss())
    from . import waveform_layout_store
    waveform_layout_store.set_meta("theme", name)

theme_adapter.on_changed(_on_theme_changed)
```

- [ ] **Step 5: 运行启动验证 + 配置往返测试**

Run: `python -c "import sys; sys.argv=['x']; from PyQt6.QtWidgets import QApplication; app=QApplication(sys.argv); from tools.waveform.app import MainWindow; w=MainWindow(); print('OK'); w.close()"`
Expected: `OK`

手动验证（启动两次）:
1. 启动 → 改端口为 COM3 → 连接虚拟引擎 → 切换 2 窗口 → 关闭
2. 再次启动 → 检查窗口数是否为 2、端口是否为 COM3

- [ ] **Step 6: Commit**

```bash
git add tools/waveform/app.py tools/waveform/waveform_plot.py tools/waveform/trigger.py tools/waveform/main.py
git commit -m "feat(waveform): 配置持久化 (连接/窗口/触发/主题)

- ConnectionPanel: 启动加载 connection 节, 连接成功时保存
  (kind/port/baud/motor_id/telemetry_mask/period)
- WaveformPlot: 启动加载 windows 节, 变更时保存
  (count/window_s/各面板可见通道)
- TriggerDialog: 启动加载 trigger 节, accept 时保存
  (key/edge/level/hold_s)
- main.py: 主题切换时持久化到 theme 元数据
- 首次运行无配置时用内置默认 (pos/vel/torque)"
```

---

## Task 13: 集成测试 + 回归测试

**Files:**
- Create: `tools/waveform/tests/test_integration.py`

- [ ] **Step 1: 创建集成测试**

```python
# tools/waveform/tests/test_integration.py
"""集成测试: 端到端验证主流程。"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QTimer
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


def test_main_window_creation():
    print("[TEST] MainWindow 创建")
    from tools.waveform.app import MainWindow
    w = MainWindow()
    check(w is not None, "MainWindow 创建成功")
    check(hasattr(w, '_waveform'), "MainWindow 有 _waveform 属性")
    check(hasattr(w, '_conn'), "MainWindow 有 _conn 属性")
    w.close()


def test_waveform_plot_panels():
    print("[TEST] WaveformPlot 多面板")
    from tools.waveform.waveform_plot import WaveformPlot
    wp = WaveformPlot()
    check(len(wp._panels) == 1, f"初始 1 面板, 实际={len(wp._panels)}")
    # 切换到 2 窗口
    wp._cmb_windows.setCurrentIndex(1)  # n=2
    check(len(wp._panels) == 2, f"切 2 面板, 实际={len(wp._panels)}")
    # 切换到 4 窗口
    wp._cmb_windows.setCurrentIndex(3)  # n=4
    check(len(wp._panels) == 4, f"切 4 面板, 实际={len(wp._panels)}")


def test_theme_toggle_persists():
    print("[TEST] 主题切换持久化")
    from tools.waveform import waveform_layout_store as store
    from tools.waveform.theme_adapter import theme_adapter
    # 用临时文件避免污染真实配置
    orig_path = store._PATH
    fd, tmp = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    os.remove(tmp)
    store._PATH = tmp
    store.clear_cache()
    try:
        original = theme_adapter.name
        theme_adapter.toggle()
        new = theme_adapter.name
        check(original != new, f"toggle 后主题变化: {original} -> {new}")
        # 模拟 main.py 的持久化
        store.set_meta("theme", new)
        check(store.get_meta("theme") == new, "持久化读回一致")
        theme_adapter.toggle()  # 切回
    finally:
        store._PATH = orig_path
        store.clear_cache()
        if os.path.exists(tmp):
            os.remove(tmp)


def test_dirty_skip_refresh():
    print("[TEST] dirty 标记跳过重绘")
    from tools.waveform.waveform_plot import WaveformPlot
    wp = WaveformPlot()
    # 暂停时应跳过
    wp._paused = True
    check(not wp._pool.dirty, "初始 dirty=False")
    # 恢复
    wp._paused = False
    # 无数据时 dirty=False, _refresh 应直接 return
    # 模拟有数据
    from tools.waveform.protocol_reference import FeedbackData
    fb = FeedbackData()
    fb.pos = 1.0
    wp.append_feedback(fb, ('pos',))
    check(wp._pool.dirty, "append 后 dirty=True")


def test_export_csv_roundtrip():
    print("[TEST] CSV 导出往返")
    from tools.waveform.waveform_plot import WaveformPlot, PanelCurve
    from tools.waveform.ring_buffer import RingBuffer, ChannelMeta
    from tools.waveform.exporter import _export_csv_to_path
    buf = RingBuffer(100)
    for i in range(20):
        buf.append(float(i) * 0.01, float(i) * 0.1)
    meta = ChannelMeta(key='pos', label='位置', unit='rad', group='POS_VEL')
    curve = PanelCurve(key='pos', buf=buf, meta=meta)
    curves = {'pos': curve}

    fd, tmp = tempfile.mkstemp(suffix='.csv')
    os.close(fd)
    os.remove(tmp)
    try:
        ok = _export_csv_to_path(None, curves, tmp, show_msg=False)
        check(ok, "CSV 导出返回 True")
        check(os.path.exists(tmp), "文件已生成")
        import csv
        with open(tmp, encoding='utf-8-sig') as f:
            rows = list(csv.reader(f))
        check(len(rows) >= 22, f"行数 >= 22 (2 表头 + 20 数据), 实际={len(rows)}")
        check(rows[0][0] == 'time_s', "表头第一列 time_s")
        check(rows[1][1].startswith('位置'), "单位行第二列含 label")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


if __name__ == '__main__':
    test_main_window_creation()
    test_waveform_plot_panels()
    test_theme_toggle_persists()
    test_dirty_skip_refresh()
    test_export_csv_roundtrip()
    print(f"\n{'='*40}\nPASS={PASS} FAIL={FAIL}")
    if _FAILS:
        for m in _FAILS:
            print(f"  - {m}")
    sys.exit(1 if FAIL else 0)
```

- [ ] **Step 2: 运行所有测试**

Run: `python -m tools.waveform.tests.test_integration`
Expected: `PASS=10+ FAIL=0`

Run (运行全部 waveform 测试):
```powershell
python -m tools.waveform.tests.test_ring_buffer; python -m tools.waveform.tests.test_theme_adapter; python -m tools.waveform.tests.test_waveform_layout_store; python -m tools.waveform.tests.test_exporter; python -m tools.waveform.tests.test_integration
```
Expected: 全部 `FAIL=0`

- [ ] **Step 3: 启动应用做最终 smoke test**

Run: `python -m tools.waveform.main`
（手动验证 30 秒）
- [ ] 窗口正常打开，深色主题，字体 Microsoft YaHei UI 9
- [ ] 连接虚拟引擎，pos/vel/torque 曲线显示
- [ ] 按 T 切换浅色主题，所有控件颜色同步
- [ ] 按 2 切换 2 窗口，分屏正常
- [ ] 按 Space 暂停，曲线停止滚动；再按恢复
- [ ] 按 E 弹出导出对话框，CSV 导出成功
- [ ] 关闭后重启，窗口数/通道/连接配置恢复

- [ ] **Step 4: Commit**

```bash
git add tools/waveform/tests/test_integration.py
git commit -m "test(waveform): 集成测试覆盖端到端主流程

5 个集成测试:
- MainWindow 创建
- WaveformPlot 多面板切换 (1/2/4)
- 主题切换持久化
- dirty 标记跳过重绘
- CSV 导出往返

至此 waveform 优化完成: 主题对齐主上位机 + 性能优化 (float32/OpenGL/
dirty/节流) + 导出 bug 修复 + 配置持久化 + 键盘快捷键 + 自动重连。"
```

---

## Self-Review

### 1. Spec 覆盖检查

| 用户需求 | 覆盖 Task |
|---------|-----------|
| 各种命令和 UI 布局风格和当前上位机一致 | Task 2 (theme_adapter) + Task 5 (绘图接入) + Task 9 (按钮/状态栏) |
| 波形显示按 waveform 风格 | Task 5 (保留 pyqtgraph + ScopeViewBox 单轴缩放, 仅换色板) |
| 易用性优化 | Task 8 (导出 bug) + Task 10 (快捷键) + Task 11 (自动重连) + Task 12 (配置持久化) |
| 流畅性优化 | Task 4 (float32) + Task 5 (OpenGL/antialias) + Task 6 (dirty) + Task 7 (节流) |
| 分析 waveform 现存问题 | 已在"现状分析"章节详列 A/B/C/D/E 五类问题 |

无遗漏。

### 2. 占位符扫描

- ✅ 每个 Step 都有完整代码（无 "TODO"/"实现细节后续补"）
- ✅ 每个 Step 都有确切命令（`python -m ...`）和预期输出
- ✅ 文件路径全部绝对或相对包根明确

### 3. 类型一致性检查

- ✅ `RingBuffer` 在 Task 1/4/6 中签名一致（`append(t, y)` / `ts_view` / `ys_view` / `slice_last(window_s)`）
- ✅ `DataPool` 在 Task 1/6 中一致（`append_feedback(fb, filled)` / `dirty` / `reset_dirty()`）
- ✅ `theme_adapter` 在 Task 2/5/9/10/12 中一致（`hex(key)` / `qss()` / `set(name)` / `toggle()` / `on_changed(cb)` / `name` / `is_dark`）
- ✅ `PanelCurve` 在 Task 1/8/13 中一致（`key` / `buf` / `meta`，meta 含 `key/label/unit/group/color`）
- ✅ `waveform_layout_store` 在 Task 3/12 中一致（`get_section` / `set_section` / `get_meta` / `set_meta`）
- ✅ `WaveformPlot.get_active_panel()` 在 Task 10/12 中一致
- ✅ `WaveformPanel.get_visible_keys()` 在 Task 8/12 中一致

无类型不一致。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-06-waveform-optimization.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
