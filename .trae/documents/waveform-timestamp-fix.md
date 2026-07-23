# 波形时间戳均匀化修复方案

## 概述

波形显示存在数据点时间戳聚集 BUG：下位机按固定周期（如 20ms）发送遥测帧，但上位机主线程在处理 Qt 事件队列时，同一批多帧的 `time.monotonic()` 时间戳几乎相同（微秒级差异），导致波形上数据点聚集成簇而非按周期均匀分布。

## BUG 根因分析

### 数据流路径
```
下位机按 period_ms 周期发送 TELEMETRY(0xCA)
    ↓
SerialTransport._run()  ← 子线程读串口、解帧
    ↓
frame_received.emit(cmd, payload)  ← Qt QueuedConnection 投递到主线程
    ↓
JmClient._on_frame() → _merge_and_emit_feedback() → feedback_updated.emit(fb)
    ↓
MainWindow._queue_feedback(fb)     ← ★ 时间戳在此生成 ★
    self._feedback_pending.append((time.monotonic(), fb))
    ↓
_on_ui_tick() (50ms)  → 批量出队 → feed_feedback_batch(ts_list, fb_list)
```

### 问题所在

1. **遥测周期 20ms，UI tick 50ms**：两个 tick 之间累积约 2-3 帧
2. **Qt 事件队列批处理**：主线程从 UI 渲染返回事件循环后，连续快速处理所有排队帧
3. **时间戳几乎相同**：`_queue_feedback` 中每帧的 `time.monotonic()` 调用间隔仅几微秒
4. **结果**：同一批 2-3 帧在波形上聚集成一个点簇，而非按 20ms 间隔均匀展开

### 时序示例
```
下位机发送:  t=0ms    t=20ms    t=40ms
串口接收:    t≈1ms    t≈21ms    t≈41ms
Qt 队列:     [frame1] [frame2] [frame3]
主线程处理:  ──────── UI tick (50ms) ────────┐
             t=55.001ms  t=55.002ms  t=55.003ms  ← 三帧时间戳几乎相同！
```

## 修复方案

### 核心思路

在 `_on_ui_tick()` 批量出队时，**基于已知的遥测周期重建时间戳**，使数据点按下位机实际采样周期均匀分布。

### 修改文件

#### `ui/main_window.py`

**1. 新增属性存储遥测周期**

在 `__init__` 中添加：
```python
self._telemetry_period_ms = 20  # 遥测周期, 用于时间戳重建
```

**2. 在 `_on_apply_telemetry` 中记录遥测周期**

```python
def _on_apply_telemetry(self, enable: bool, mask: int, period_ms: int):
    ...
    self._telemetry_period_ms = max(1, int(period_ms))  # 记录遥测周期
    ...
```

**3. 在 `_on_ui_tick` 中重建时间戳**

将现有的：
```python
ts_list = [item[0] for item in batch]
fb_list = [item[1] for item in batch]
```

改为：
```python
fb_list = [item[1] for item in batch]
# 基于遥测周期重建时间戳, 使数据点按下位机采样周期均匀分布
ts_list = self._reconstruct_timestamps(batch)
```

**4. 新增 `_reconstruct_timestamps` 方法**

```python
def _reconstruct_timestamps(self, batch: list) -> list:
    """基于遥测周期重建时间戳, 使数据点均匀分布.

    下位机按 period_ms 周期采样, 但上位机主线程批量处理时
    time.monotonic() 时间戳几乎相同。使用最后一帧的接收时间
    作为锚点, 向前按 period_ms 间隔回溯分配时间戳。
    """
    n = len(batch)
    if n <= 1:
        return [item[0] for item in batch]

    period = self._telemetry_period_ms
    if period <= 0:
        return [item[0] for item in batch]

    # 最后一帧用实际接收时间戳(最准确), 前面的帧按周期回溯
    dt = period / 1000.0  # 转秒
    ts_last = batch[-1][0]
    ts_list = []
    for i in range(n):
        offset = (n - 1 - i) * dt
        ts_list.append(ts_last - offset)
    return ts_list
```

### 修复效果对比

```
修复前 (time.monotonic 原始时间戳):
  帧1: t=55.001ms   ┐
  帧2: t=55.002ms   ├─ 三帧聚集成一个点簇
  帧3: t=55.003ms   ┘

修复后 (基于周期重建):
  帧1: t=55.003 - 2*20ms = 15.003ms   ─ 均匀分布
  帧2: t=55.003 - 1*20ms = 35.003ms
  帧3: t=55.003 - 0*20ms = 55.003ms
```

### 调试窗口同步修复

调试窗口的 debug 数据分发也使用相同的 `batch`，需要同步使用重建后的 `ts_list`：

```python
# 修改 _on_ui_tick 中调试数据分发部分
if self._debug_window is not None and self._debug_window.isVisible():
    debug_ts = []
    debug_vals = []
    for ts, fb in zip(ts_list, fb_list):  # 使用重建后的 ts_list
        d = getattr(fb, 'debug', ())
        if d:
            debug_ts.append(ts)
            debug_vals.append(d)
    if debug_ts:
        self._debug_window.feed_debug_batch(debug_ts, debug_vals)
```

## 实现步骤

### Step 1: 修改 `ui/main_window.py`
1. `__init__` 中添加 `self._telemetry_period_ms = 20`
2. `_on_apply_telemetry` 中添加 `self._telemetry_period_ms = max(1, int(period_ms))`
3. 新增 `_reconstruct_timestamps(self, batch)` 方法
4. `_on_ui_tick` 中用 `ts_list = self._reconstruct_timestamps(batch)` 替换 `ts_list = [item[0] for item in batch]`
5. 调试数据分发改用 `zip(ts_list, fb_list)` 遍历

### Step 2: 验证
- 连接设备，开启遥测（周期 20ms）
- 观察实时曲线，确认数据点按 20ms 间隔均匀分布，不再聚集成簇
- 修改遥测周期为 10ms / 50ms，确认波形间隔相应变化
- 开启调试通道窗口，确认调试波形也均匀分布
- 高频场景（UI 繁忙时积压多帧）确认重建后时间戳连续无断裂

## 假设与决策

1. **使用最后一帧时间戳作为锚点**：最后一帧的 `time.monotonic()` 最接近当前时刻，向前回溯分配时间戳最准确。

2. **遥测周期从 `_on_apply_telemetry` 获取**：用户配置遥测时即记录周期值，确保与下位机实际采样周期一致。

3. **单帧批次不重建**：只有 1 帧时无需重建，直接用原始时间戳。

4. **period_ms 为 0 时回退**：安全兜底，避免除零或异常。

5. **不影响 `_t0` 基准**：`_t0` 在 `feed_feedback_batch` 中取 `ts_list[0]`，重建后的首帧时间戳仍是合理值。
