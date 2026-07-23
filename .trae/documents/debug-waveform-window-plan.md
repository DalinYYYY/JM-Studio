# 调试通道独立波形窗口 — 实现方案

## 概述

在实时曲线面板顶部添加"调试通道"勾选框，勾选后弹出独立的调试波形显示窗口，展示遥测帧中的 `jm_dbg[]` 调试通道数据（f32 数组）。窗口关闭或未勾选时不分配缓冲区、不处理数据，以节约资源。

---

## 当前状态分析

### 调试通道数据来源
- 遥控模式（`TelemetryPanel`）勾选"调试通道"位（`JmTlmBit.DEBUG = 1<<9`）后，下位机在 0xCA 遥测帧末尾附加 `jm_dbg[]` 数据
- `jmproto/feedback.py:182-188` 解析：取帧内剩余字节，每 4 字节一个 f32，通道数由固件 `JM_DBG_CH` 决定（动态）
- 解析结果存入 `FeedbackData.debug`（f32 元组）
- 当前 `motor_client.py:325-327` 对 0xC9 READ_DEBUG 帧直接丢弃不做处理

### 现有波形系统 (`plot_panel.py`)
- 4 组固定子图：位置、速度、电流、机械角度
- 9 个固定 buffer：`pos, vel, id, iq, ibus, ia, ib, ic, angle`
- 工具栏已有：暂停、跟随、Y自适应、窗口(s)、电流曲线勾选、重置视图、清空
- 刷新机制：`QTimer(30ms)` → `_refresh()` → 脏标记驱动逐曲线重绘
- 数据喂入：`feed_feedback_batch(ts_list, fb_list)` 批量向量化写入
- 支持 `_extra_buffers` 动态扩展额外曲线

### 现有 MainWindow 架构 (`main_window.py`)
- `_on_ui_tick()` 每 50ms 执行：消费 `_feedback_pending` 队列 → 批量喂 PlotPanel + 更新 FeedbackPanel
- 顶层窗口管理模式：`_btn_virtual_mode` 开关由 MainWindow 管理，Panel 只负责 UI 展示
- 信号连接集中在 `_connect_signals()`

### 关键约束
- 调试通道数不固定（由固件 `JM_DBG_CH` 决定），首次收到数据后确定
- 需要在窗口未打开时完全不处理调试数据（不分配 buffer、不追加数据）
- 遥测帧中 DEBUG 位未勾选时，`fb.debug` 为空元组 `()`

---

## 方案设计

### 架构决策

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 窗口管理 | **MainWindow 统一管理** | 顶层窗口理应由 MainWindow 管理生命周期，与现有 `_btn_virtual_mode` 模式一致 |
| 窗口类型 | **QWidget + Window 标志** | 轻量，无需菜单栏/状态栏，与调试用途匹配 |
| 采样策略 | **逐帧全部绘制** | 保留完整波形细节，不丢数据点 |
| 数据通道 | **MainWindow 直接喂给 DebugPlotWindow** | 不经过 PlotPanel 中转，职责更清晰 |

### 组件职责划分

```
PlotPanel（复选框 + 信号）
  ├── 添加"调试通道" QCheckBox
  ├── 勾选变化 → emit debug_window_toggled(bool)
  └── 不持有 DebugPlotWindow 引用，不处理调试数据

MainWindow（窗口管理 + 数据分发）
  ├── 接收 debug_window_toggled 信号 → 创建/销毁 DebugPlotWindow
  ├── _on_ui_tick() 中提取 debug 数据 → 直接喂给 DebugPlotWindow
  ├── 窗口关闭事件 → 同步取消复选框勾选
  └── 持久化恢复：启动时若上次为勾选态，自动创建窗口

DebugPlotWindow（纯展示）
  ├── 独立的 QWidget+Window 窗口
  ├── 内部管理：工具栏、缓冲区、曲线、刷新定时器
  └── 关闭时 emit window_closed 信号
```

---

### 新增文件

#### `ui/panels/debug_plot_window.py` — 调试波形独立窗口

**类定义**：
```python
class DebugPlotWindow(QWidget):
    window_closed = pyqtSignal()  # 用户点击关闭按钮时发出

    MAX_POINTS = 300_000  # 与 PlotPanel 一致
    _AUTO_RANGE_EVERY = 10
    _PEN_COLORS = ['plot_pos', 'plot_vel', 'plot_id', 'plot_iq',
                   'plot_ibus', 'plot_ia', 'plot_ib', 'plot_ic', 'plot_angle']
```

**窗口属性**：
- `setWindowTitle("调试通道波形")`
- `setWindowFlags(Qt.WindowType.Window)` — 独立窗口
- `setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)` — 手动管理生命周期
- 最小尺寸：800×500

**内部结构**：
```
┌──────────────────────────────────────────────────────┐
│ 工具栏: [暂停] [跟随] [Y自适应] [窗口(s):20] [通道命名] [清空] [重置视图] │
├──────────────────────────────────────────────────────┤
│                                                      │
│          pyqtgraph GraphicsLayoutWidget              │
│          (单个 plot，所有通道叠加)                     │
│                                                      │
│  图例: 电流环Kp(红) 速度环Ki(蓝) 位置环Kd(绿) ...      │
│  Y轴: "调试值"   X轴: "时间 (s)"                      │
│                                                      │
└──────────────────────────────────────────────────────┘
```

**核心属性**：
- `self._channel_count: int = 0` — 首次收到数据后确定
- `self._channel_names: list[str]` — 用户自定义通道名称，持久化保存
- `self._buffers: dict[str, _SeriesBuffer]` — 动态创建，key 为 `"dbg_0"`, `"dbg_1"` ...
- `self._curves: dict[str, pg.PlotDataItem]` — 动态创建
- `self._t0: float | None` — 首个时间戳基准
- `self._paused: bool`
- `self._window_seconds: int = 20`
- `self._timer: QTimer` — 30ms 刷新

**关键方法**：
```python
def feed_debug(self, ts: float, debug_tuple: tuple):
    """接收单帧调试数据。首次调用时根据 len(debug_tuple) 动态创建通道。"""
    if self._paused:
        return
    if self._t0 is None:
        self._t0 = ts
    t = ts - self._t0
    # 首次确定通道数
    if self._channel_count == 0 and debug_tuple:
        self._init_channels(len(debug_tuple))
    # 追加到各通道 buffer
    for i, val in enumerate(debug_tuple):
        name = f"dbg_{i}"
        if name in self._buffers:
            self._buffers[name].append(t, float(val))

def feed_debug_batch(self, ts_list: list, debug_data_list: list):
    """批量接收调试数据（逐帧全部绘制策略）。"""
    # 收集每通道的 (ts, val) 数组，批量 append
    ...

def _init_channels(self, count: int):
    """动态创建 count 个通道的曲线和缓冲区。
    若 _channel_names 已有名称则使用，否则使用默认名 'DBG_0', 'DBG_1' ..."""

def _on_rename_channels(self):
    """打开通道命名对话框。
    对话框为无边框浮窗或 QDialog，列出所有通道的当前名称，用户可编辑并保存。
    修改后实时更新图例标签和鼠标悬停提示。"""

def _get_display_name(self, index: int) -> str:
    """获取第 index 个通道的显示名称。有自定义名用自定义名，否则用 'DBG_{index}'。"""

def clear(self):
    """清空所有缓冲区。"""

def get_opts(self) -> dict:
    """持久化：窗口大小、位置、工具栏状态、通道名称。"""
    # 返回 {"geo": [x,y,w,h], "window_seconds": 20, "follow": True,
    #         "auto_y": True, "channel_names": ["电流环Kp", ...]}

def set_opts(self, opts: dict):
    """恢复配置。包括通道名称（在 _init_channels 之前或之后套用）。"""

def apply_theme(self):
    """主题切换：更新背景、画笔、坐标轴颜色。"""

def closeEvent(self, event):
    """用户关闭窗口时发出 window_closed 信号，不销毁。"""
    self.window_closed.emit()
    event.accept()
```

**通道颜色**：`_PEN_COLORS[i % len(_PEN_COLORS)]` 循环使用，与现有曲线颜色体系一致。

**图例**：`legend.setVisible(True)`，通道数 > 1 时显示图例，展示用户自定义名称。

**通道命名对话框设计**：
- 点击工具栏"通道命名"按钮 → 弹出 `QDialog`
- 对话框标题：`"调试通道命名"`
- 内容：一个 `QFormLayout`，每行 `"通道 N:  [可编辑文本框]"`，文本框预填当前名称
- 底部按钮：`[确定]` `[取消]`
- 确定后：更新 `_channel_names`，刷新图例标签和曲线 name 属性
- 取消：不保存，恢复原名称

**鼠标悬停提示**：悬停时显示 `<自定义名称> = <值>`（如 `电流环Kp = 0.0523`），而非 `DBG_0 = 0.0523`。

**名称持久化路径**：`ui_layout.json` → `plot.debug_window.channel_names`（字符串数组）。

**默认名称策略**：通道在收到数据前已有名称（从持久化恢复），若未恢复则默认 `"DBG_0"`, `"DBG_1"` ...

---

### 修改文件

#### 1. `ui/panels/plot_panel.py`

**新增控件**：在工具栏"清空"按钮左侧添加 `QCheckBox("调试通道")`

**新增信号**：
```python
debug_window_toggled = pyqtSignal(bool)  # 类级别信号
```

**新增属性**：
- `self._debug_checkbox: QCheckBox`

**修改 `_build()`**：
- 在 `bar.addWidget(self._btn_clear)` 之前：
  ```python
  self._debug_checkbox = QCheckBox("调试通道")
  self._debug_checkbox.setChecked(False)
  self._debug_checkbox.toggled.connect(self.debug_window_toggled.emit)
  bar.addWidget(self._debug_checkbox)
  ```

**新增方法**：
```python
def set_debug_checkbox(self, checked: bool):
    """外部（MainWindow）同步复选框状态，不触发信号。"""
    self._debug_checkbox.blockSignals(True)
    self._debug_checkbox.setChecked(checked)
    self._debug_checkbox.blockSignals(False)
```

**修改 `get_opts()`**：
```python
opts["debug_window"] = {
    "checked": self._debug_checkbox.isChecked(),
}
```

**修改 `set_opts()`**：
```python
dw = opts.get("debug_window")
if isinstance(dw, dict):
    try:
        self._debug_checkbox.setChecked(bool(dw.get("checked", False)))
    except Exception:
        pass
```

> **注意**：PlotPanel 不持有 DebugPlotWindow 引用，不处理调试数据。职责仅限复选框和信号。

#### 2. `ui/main_window.py`

**新增属性**：
```python
self._debug_window: DebugPlotWindow | None = None
```

**修改 `_build_ui()`**：
- 导入 `from ui.panels.debug_plot_window import DebugPlotWindow`

**修改 `_connect_signals()`**：
```python
# 调试窗口开关（PlotPanel 复选框 → MainWindow 管理窗口）
self._plot_panel.debug_window_toggled.connect(self._on_debug_window_toggled)
```

**新增方法**：
```python
def _on_debug_window_toggled(self, checked: bool):
    """PlotPanel 调试通道复选框变化 → 创建/关闭调试窗口。"""
    if checked:
        if self._debug_window is None:
            self._debug_window = DebugPlotWindow()
            self._debug_window.window_closed.connect(self._on_debug_window_closed)
            # 恢复上次窗口大小/位置（从持久化配置）
            settings = layout_store.get_meta("settings", {})
            plot_opts = settings.get("plot", {})
            dw_opts = plot_opts.get("debug_window", {})
            if isinstance(dw_opts, dict):
                self._debug_window.set_opts(dw_opts)
        self._debug_window.show()
    else:
        if self._debug_window is not None:
            self._debug_window.close()
            self._debug_window = None

def _on_debug_window_closed(self):
    """用户手动关闭调试窗口 → 同步取消 PlotPanel 复选框。"""
    self._plot_panel.set_debug_checkbox(False)
    self._debug_window = None
```

**修改 `_on_ui_tick()`**：
在 `feed_feedback_batch` 之后，添加调试数据分发：

```python
# 调试通道数据分发（逐帧全部绘制）
if self._debug_window is not None and self._debug_window.isVisible():
    debug_ts = []
    debug_vals = []
    for ts, fb in batch:
        d = getattr(fb, 'debug', ())
        if d:
            debug_ts.append(ts)
            debug_vals.append(d)
    if debug_ts:
        self._debug_window.feed_debug_batch(debug_ts, debug_vals)
```

**修改 `_collect_settings()`**：
- `plot_panel.get_opts()` 已包含 `debug_window.checked` 状态
- 额外追加调试窗口运行时状态（只在窗口存在时收集）：
```python
if self._debug_window is not None:
    s["plot"]["debug_window"].update(self._debug_window.get_opts())
```

**修改 `_apply_settings()`**：
- `plot_panel.set_opts()` 恢复复选框状态后，若为 `checked=True`，自动触发 `_on_debug_window_toggled(True)` 创建窗口
- 但需注意：`_apply_settings` 中 `plot_panel.set_opts()` 直接设置复选框，可能触发信号。需要确保信号连接在 `_build_ui` → `_connect_signals` 之后、`_apply_settings` 之前完成。

**修改 `_on_disconnect()`**：
- 断开连接时关闭调试窗口：
```python
if self._debug_window is not None:
    self._debug_window.close()
    self._debug_window = None
self._plot_panel.set_debug_checkbox(False)
```

**修改 `_on_theme_changed()`**：
```python
if self._debug_window is not None:
    self._debug_window.apply_theme()
```

---

### 数据流图

```
下位机 0xCA 遥测帧 (含 DEBUG 组)
        │
        ▼
jmproto/feedback.py: parse_telemetry()
        │ fb.debug = (f32, f32, ...)
        ▼
motor_client.py: _merge_and_emit_feedback()
        │ feedback_updated 信号
        ▼
MainWindow._queue_feedback() → _on_ui_tick()
        │
        ├──▶ PlotPanel.feed_feedback_batch()  [现有逻辑，不处理debug]
        │
        └──▶ if _debug_window is not None:
                for each (ts, fb) in batch:
                    if fb.debug:
                        collect (ts, fb.debug)
                DebugPlotWindow.feed_debug_batch()
                        │
                        ├── 首次: 根据 len(debug_tuple) 动态创建通道
                        │
                        └── 逐通道追加到 _SeriesBuffer
                                │
                                ▼
                            QTimer(30ms) → _refresh() → 重绘曲线
```

---

### 控件交互流程

```
用户勾选"调试通道"复选框
        │
        ▼
PlotPanel.debug_window_toggled.emit(True)
        │
        ▼
MainWindow._on_debug_window_toggled(True)
        ├── 创建 DebugPlotWindow
        ├── 恢复上次窗口大小/位置
        └── show()

用户取消勾选
        │
        ▼
PlotPanel.debug_window_toggled.emit(False)
        │
        ▼
MainWindow._on_debug_window_toggled(False)
        ├── close() 调试窗口
        └── _debug_window = None

用户点击调试窗口 X 按钮
        │
        ▼
DebugPlotWindow.closeEvent() → window_closed.emit()
        │
        ▼
MainWindow._on_debug_window_closed()
        ├── PlotPanel.set_debug_checkbox(False)  [blockSignals 防循环]
        └── _debug_window = None
```

---

## 实现步骤

### Step 1: 创建 `ui/panels/debug_plot_window.py`
- 实现 `DebugPlotWindow(QWidget)` 类
- 复用 `plot_panel._SeriesBuffer`（从 plot_panel import）
- 内部工具栏：暂停、跟随、Y自适应、窗口(s)、**通道命名**、清空、重置视图
- 单个 pyqtgraph GraphicsLayoutWidget + plot，所有调试通道叠加显示
- 通道数动态确定（首次 `feed_debug` 时根据 len(debug_tuple) 创建）
- `_channel_names` 列表存储用户自定义名称，图例和悬停提示使用自定义名称
- "通道命名"按钮 → QDialog 弹窗编辑所有通道名称，确认后刷新图例
- `feed_debug_batch()` 支持批量喂入（逐帧全部绘制策略）
- `apply_theme()` 支持主题切换
- `get_opts()` / `set_opts()` 支持窗口大小/位置/工具栏状态/通道名称持久化
- `closeEvent()` 发出 `window_closed` 信号

### Step 2: 修改 `ui/panels/plot_panel.py`
- 添加 `debug_window_toggled = pyqtSignal(bool)` 类级别信号
- 在工具栏"清空"按钮左侧添加 `QCheckBox("调试通道")`
- 实现 `set_debug_checkbox(checked)` 供 MainWindow 同步状态
- 修改 `get_opts()` / `set_opts()` 增补 `debug_window.checked` 字段

### Step 3: 修改 `ui/main_window.py`
- 导入 `DebugPlotWindow`
- 添加 `_debug_window` 属性
- 实现 `_on_debug_window_toggled()`, `_on_debug_window_closed()`
- 修改 `_connect_signals()` 连接 `debug_window_toggled` 信号
- 修改 `_on_ui_tick()` 添加调试数据批量分发
- 修改 `_on_disconnect()` 关闭调试窗口
- 修改 `_on_theme_changed()` 转发主题到调试窗口
- 修改 `_collect_settings()` 追加调试窗口运行时配置

### Step 4: 验证
- 启动上位机，连接设备，在遥控面板勾选"调试通道"使能 DEBUG 位
- 在实时曲线面板勾选"调试通道"复选框，确认弹出独立窗口
- 确认窗口中显示调试通道波形，通道数正确，所有通道颜色不同
- 逐帧数据完整绘制，无丢点
- 点击"通道命名"按钮，修改通道名称（如"电流环Kp"），确认图例和悬停提示同步更新
- 关闭调试窗口再重新打开，确认通道名称已恢复
- 关闭主窗口再重新打开，勾选复选框，确认通道名称持久化有效
- 取消勾选，确认窗口关闭
- 点击窗口 X 按钮，确认复选框同步取消
- 在遥控面板取消"调试通道"勾选，确认调试窗口曲线不再增长
- 断开连接，确认调试窗口自动关闭
- 切换主题（深色/浅色），确认调试窗口跟随变化

---

## 假设与决策

1. **调试数据采样策略**：逐帧全部绘制，每批反馈数据中所有含 debug 数据的帧都追加到缓冲区。虽然开销大于取最后一帧，但能保留完整波形细节，符合用户需求。

2. **通道数动态确定**：首次收到非空 debug_tuple 时创建对应数量的曲线和缓冲区，后续通道数不变（固件 `JM_DBG_CH` 是编译期常量）。

3. **窗口生命周期**：调试窗口由 MainWindow 创建和管理。关闭时仅隐藏/销毁 C++ 窗口对象，Python 引用置 `None`。复选框取消勾选时同步关闭窗口。

4. **颜色分配**：复用 `_PEN_KEY` 颜色键循环，通道数超过 9 时循环使用。

5. **持久化范围**：复选框状态持久化到 `ui_layout.json` → `plot.debug_window.checked`；窗口大小/位置/工具栏状态持久化到 `plot.debug_window` 中。缓冲区数据不持久化。

6. **主题支持**：调试窗口通过 `apply_theme()` 跟随主窗口主题切换，由 MainWindow 在 `_on_theme_changed()` 中转发。

7. **断开连接行为**：断开连接时自动关闭调试窗口并取消复选框，避免无数据时窗口残留。

8. **通道命名**：用户可通过工具栏"通道命名"按钮自定义每个通道的显示名称。名称在通道创建之后可随时修改（修改后图例和悬停提示实时更新），并持久化保存。首次收到数据前若已从持久化恢复名称，则直接使用恢复的名称；否则使用默认名 `"DBG_0"`, `"DBG_1"` ...