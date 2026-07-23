# 调试通道图示命名修复 & BUG 清理方案

## 概述

调试通道波形窗口（`debug_plot_window.py`）和主波形面板（`plot_panel.py`）在数据点编号改造后存在多处残留 BUG 和使用不便。本方案逐一修复。

---

## 问题清单与修复方案

### P0：用户可见的严重 BUG

#### 1. 调试通道图例重复（debug_plot_window.py L327-329）
**问题**：`_on_rename_channels` 中 `legend.removeItem(name)` 传入内部键 `"dbg_0"`，但图例实际存储的是显示名（如 `"DBG_0"`），字符串匹配失败，旧图例项不移除，导致重命名后图例出现重复条目。

**修复**：改为按 curve 对象移除：
```python
if self._plot.legend is not None:
    self._plot.legend.removeItem(curve)
    self._plot.legend.addItem(curve, display)
```

#### 2. `feed_debug_batch` 数据错位（debug_plot_window.py L255-263）
**问题**：当某帧某通道缺失时（`ch >= len(d)`），`ys_arr[:valid_count]` 收集的是所有有效帧的值，但 `ts_arr[:valid_count]` 取的是前 N 个时间戳，两者不对应，导致 X 轴错位。

**修复**：用 mask 同步过滤 ts_arr：
```python
mask = np.fromiter((ch < len(d) for d in debug_data_list), dtype=bool, count=n)
if mask.any():
    buf.append_batch(ts_arr[mask], np.array([float(d[ch]) for d, m in zip(debug_data_list, mask) if m], dtype=np.float32))
```

#### 3. 通道显示名空串未回退（debug_plot_window.py L291, L309-312）
**问题**：`_init_channels` L291 和 `_on_rename_channels` L309-312 直接用 `_channel_names[i]`，当其为空串时图例显示空白。已有 `_get_display_name(i)` 方法可正确回退但未被调用。

**修复**：两处都改为调用 `self._get_display_name(i)`。

### P1：容错性与资源问题

#### 4. `_init_channels` / `feed_debug` / `feed_debug_batch` 缺 pg is None 防护（debug_plot_window.py）
**问题**：`pg is None` 时 `_build` 提前 return 不创建控件，但喂数据时仍调用 `_init_channels`，其中 `pg.mkPen` 和 `self._plot.plot` 会 AttributeError 崩溃。

**修复**：
- `_init_channels` 入口加 `if pg is None: return`
- `feed_debug` 和 `feed_debug_batch` 入口加 `if pg is None: return`

#### 5. `_reset_view` / `get_opts` / `set_opts` 缺 pg is None 防护（两文件）
**问题**：pg is None 时访问未创建的控件（`_chk_follow`, `_spin_window`, `_debug_checkbox` 等）会崩溃。

**修复**：
- `debug_plot_window.py`：`_reset_view` 入口加 `if pg is None: return`；`get_opts` 入口加 `if pg is None: return {"channel_names": list(self._channel_names)}`；`set_opts` 中用 `hasattr` 保护控件访问
- `plot_panel.py`：`_reset_view` 入口加 `if pg is None: return`；`get_opts` 入口加 `if pg is None: return {"window_points": int(self._window_points)}`；`set_opts` 中用 `hasattr` 保护

#### 6. `closeEvent` 未停止 timer（debug_plot_window.py L542-544）
**问题**：窗口关闭后 timer 仍每 30ms 触发 `_refresh`，浪费 CPU。

**修复**：`closeEvent` 中 emit 前加 `self._timer.stop()`。

### P2：代码清理

#### 7. docstring 残留 `_latest_t`（plot_panel.py L10）
**修复**：改为 `_latest_idx`。

#### 8. `_SeriesBuffer` 参数名 `t_min` 残留（plot_panel.py L172, L185）
**修复**：改为 `idx_min`，内部引用同步修改。

#### 9. `int(x)` NaN 保护（两文件）
**问题**：`int(nan)` 抛 ValueError。

**修复**：改为 `int(x) if x == x else 0`（NaN != NaN 特性）。

---

## 实现步骤

### Step 1: 修复 debug_plot_window.py
1. `feed_debug` / `feed_debug_batch` 入口加 `if pg is None: return`
2. `_init_channels` 入口加 `if pg is None: return`；L291 改用 `self._get_display_name(i)`
3. `_on_rename_channels` L309-312 改用 `self._get_display_name(i)`；L328 改为 `removeItem(curve)`
4. `feed_debug_batch` L255-263 改用 mask 过滤
5. `_reset_view` 入口加 `if pg is None: return`
6. `get_opts` / `set_opts` 加 pg is None 防护
7. `closeEvent` 加 `self._timer.stop()`
8. `_hover_values` 中 `int(x)` 加 NaN 保护

### Step 2: 修复 plot_panel.py
1. L10 docstring `_latest_t` → `_latest_idx`
2. `_SeriesBuffer.arrays` / `arrays_view` 参数名 `t_min` → `idx_min`
3. `_reset_view` / `get_opts` / `set_opts` 加 pg is None 防护
4. `_hover_values` 中 `int(x)` 加 NaN 保护

### Step 3: 编译验证
- `python -c "import py_compile; py_compile.compile(...)"` 三个文件

---

## 验证
- 连接设备，开启调试通道，勾选"调试通道"复选框
- 确认波形正常显示，通道数正确
- 点击"通道命名"，修改名称，确认图例无重复条目
- 清空某通道名称，确认回退显示 `DBG_N` 而非空白
- 关闭调试窗口，确认 timer 停止（CPU 降回）
- 重新打开，确认通道名称已持久化恢复
- 悬停鼠标在波形上，确认提示正常显示"点 = N"
