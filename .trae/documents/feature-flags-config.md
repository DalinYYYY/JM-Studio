# 功能开关配置文件方案

## 摘要

新增 `resources/feature_flags.json` 功能开关配置文件 + `ui/feature_flags.py` 加载模块。通过修改该 JSON 文件中的布尔值，控制上位机是否加载特定功能模块。**默认全功能版本**（所有开关默认 true），需要时改为 false 即可移除对应功能。打包时该文件随 `resources/` 自动包含，无需改打包脚本。

## 当前状态分析

### 现有配置机制
- 配置统一走 `ui/layout_store.py` + `resources/ui_layout.json`（运行时用户设置/主题/自绘图坐标），**无功能开关先例**。
- 资源定位模式：`os.path.dirname(os.path.abspath(__file__))` 上溯到包根的 `resources/`，PyInstaller onedir/onefile 均可用。
- `main.py` 无命令行参数、无环境变量开关。

### 功能模块装配点
主窗口 `MainWindow._build_ui()`（`ui/main_window.py:121-243`）：
- 左侧面板：`left_layout.addWidget(...)`（第 176-182 行）
- 右侧 Tab：`tabs.addTab(...)`（第 189-197 行，共 9 个 Tab）
- 信号连接：`_connect_signals()`（第 743-830 行）
- 主题广播：`_on_theme_changed`（第 705-708 行）
- 设置收集/恢复：`_collect_settings`/`_apply_settings`

### 打包
- `packaging/build_exe.ps1` 用 `--add-data "$Resources;resources"` 整体包含 resources/，新文件自动打包。

## 可开关的功能清单

基于模块独立性和用户使用场景，列出 10 个可开关功能（默认全部开启）：

| 开关键名 | 功能说明 | 对应模块 | 移除影响 |
|----------|---------|---------|---------|
| `motor_config` | 电机配置 Tab（含参数固化/全读全写/导入导出） | `_config_panel` (ParamPanel) | 无法读写电机配置参数 |
| `motor_params` | 电机参数 Tab（运行时参数 0xE0-0xE5） | `_param_panel` (ParamPanel) | 无法读写运行时参数 |
| `calibration` | 电机标定 Tab（含标定结果子面板） | `_calib_panel` + `_calib_results_panel` | 无法执行标定/查看标定结果 |
| `pid_tuning` | PID 整定 Tab（含 PID 参数子面板） | `_pid_panel` + `_pid_params_panel` | 无法 PID 自整定/调参 |
| `feedback_chart` | 实时反馈 Tab（FOC 框图/状态自绘图） | `_feedback_panel` | 无实时反馈可视化 |
| `state_machine` | 状态机 Tab | `_state_panel` | 无状态机可视化 |
| `waveform_plot` | 实时曲线 Tab（示波器） | `_plot_panel` | 无波形采集显示 |
| `fault_info` | 故障信息 Tab（含虚拟电机故障屏蔽） | `_fault_info_panel` | 无故障查看/屏蔽 |
| `virtual_twin` | 虚拟孪生功能（孪生参数 Tab + 虚拟电机开关） | `_twin_param_panel` + 虚拟模式开关 | 无数字孪生仿真 |
| `log_panel` | 通信日志面板 | `_log_panel` | 无通信报文日志 |

> 注：连接面板/控制面板/运动面板/遥测面板是基础通信能力，不作为可开关项（始终启用）。

## 配置文件设计

### 文件：`resources/feature_flags.json`

```json
{
  "version": 1,
  "features": {
    "motor_config": true,
    "motor_params": true,
    "calibration": true,
    "pid_tuning": true,
    "feedback_chart": true,
    "state_machine": true,
    "waveform_plot": true,
    "fault_info": true,
    "virtual_twin": true,
    "log_panel": true
  }
}
```

- 缺失文件 / 损坏 / 缺某键 → 对应开关默认 true（全功能）
- `features` 节缺失 → 全部默认 true

### 加载模块：`ui/feature_flags.py`

仿 `layout_store.py` 模式：
- 路径定位：包根 `resources/feature_flags.json`
- `load()` → 返回 dict（容错，损坏返回 `{}`）
- `is_enabled(key)` → 查 `features[key]`，缺失/非 bool 默认 True
- `get_enabled_features()` → 返回所有开关的 dict（供日志/调试）
- 启动时在 `main.py` 顶部加载一次，单例缓存

## 修改计划

### 1. 新建 `resources/feature_flags.json`
内容如上设计，所有开关默认 true。

### 2. 新建 `ui/feature_flags.py`
```python
"""功能开关配置加载。resources/feature_flags.json 控制功能模块是否加载。
缺失/损坏/缺键默认全功能(true), 确保默认为完整版。"""
import os, json

_RES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources")
_PATH = os.path.join(_RES_DIR, "feature_flags.json")
_cache = None

def _load():
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
        _cache = data.get("features", {}) if isinstance(data, dict) else {}
    except Exception:
        _cache = {}
    return _cache

def is_enabled(key: str) -> bool:
    """返回某功能是否启用。缺失/非bool默认True(全功能)。"""
    v = _load().get(key, True)
    return bool(v) if isinstance(v, bool) else True

def get_enabled_features() -> dict:
    """返回所有开关状态(含默认值), 供日志输出。"""
    defaults = {
        "motor_config": True, "motor_params": True, "calibration": True,
        "pid_tuning": True, "feedback_chart": True, "state_machine": True,
        "waveform_plot": True, "fault_info": True, "virtual_twin": True,
        "log_panel": True,
    }
    out = dict(defaults)
    out.update(_load())
    return out
```

### 3. 修改 `ui/main_window.py`

在 `_build_ui()` 中，每个可开关模块的实例化 + addTab/addWidget 用 `if feature_flags.is_enabled("xxx"):` 包裹。

**实例化阶段**（第 140-173 行区域）：
- 每个 `_xxx_panel = XxxPanel(...)` 前加开关判断，关闭时设为 `None`

**装配阶段**：
- 左侧 `left_layout.addWidget(...)`：无需改（基础面板不开关）
- Tab 装配（第 189-197 行）：改为条件 addTab
  ```python
  if feature_flags.is_enabled("feedback_chart"):
      tabs.addTab(self._feedback_panel, "实时反馈")
  # ... 其余 Tab 同理
  ```

**虚拟孪生联动**（第 200 行 + `_on_virtual_mode_toggled`）：
- `virtual_twin` 关闭时：不创建 `_twin_param_panel`，不显示虚拟电机开关按钮（`_create_menu_config_group` 内的条件创建）

**信号连接**（`_connect_signals`）：
- 每个面板的信号连接前加 `if self._xxx_panel is not None:` 守卫

**主题广播 / 设置收集 / ACK/NACK 转发**：
- 所有 `self._xxx_panel.xxx()` 调用前加 `if self._xxx_panel is not None:` 守卫

**日志面板特殊处理**：
- `log_panel` 关闭时：不创建 `_log_panel`，`_toggle_log_panel` 按钮/快捷键失效，所有 `log/log_warn/log_err` 调用需空操作守卫（或保留 LogPanel 实例但隐藏——**决策：保留实例但隐藏，避免大量调用点加守卫**）

### 4. 修改 `main.py`
在 `from ui.main_window import MainWindow` 之前加载 feature_flags（触发单例缓存），可选打印启用功能列表到日志。

## 实施步骤

1. 新建 `resources/feature_flags.json`（默认全 true）
2. 新建 `ui/feature_flags.py`
3. 修改 `main.py`：启动时加载 feature_flags
4. 修改 `ui/main_window.py`：
   - 导入 feature_flags
   - `_build_ui`：实例化 + 装配加开关判断
   - `_connect_signals`：信号连接加 None 守卫
   - `_on_theme_changed`：主题广播加 None 守卫
   - `_collect_settings`/`_apply_settings`：opts 循环加 None 守卫
   - `_on_ack`/`_on_nack`/`_on_ui_tick`：转发加 None 守卫
   - `_on_connected`/`_on_disconnect`：set_link_active/set_engine 加 None 守卫
   - `_create_menu_config_group`：虚拟电机开关按钮条件创建
5. 验证：默认全功能回归 + 逐个关闭验证

## 假设与决策

1. **日志面板决策**：`log_panel=false` 时保留 LogPanel 实例但隐藏（不创建会导致全代码 `log/log_warn` 调用需加守卫，代价过大）。`_toggle_log_panel` 按钮隐藏，快捷键失效。
2. **虚拟孪生决策**：`virtual_twin=false` 时同时隐藏虚拟电机模式开关按钮（菜单配置组内），避免开关无效果。
3. **标定/PID 成对处理**：`calibration=false` 同时移除 `_calib_panel` 和 `_calib_results_panel`；`pid_tuning=false` 同时移除 `_pid_panel` 和 `_pid_params_panel`。
4. **基础功能不开关**：连接/控制/运动/遥测/设备信息/菜单配置组始终启用，不纳入开关。
5. **默认全功能**：文件缺失/损坏/缺键一律默认 true，确保不破坏现有行为。

## 验证步骤

1. **默认全功能**：不修改 feature_flags.json，启动上位机，9 个 Tab + 日志面板全部正常显示。
2. **逐个关闭验证**：将每个开关改为 false，重启确认对应 Tab/面板消失，其余功能正常。
3. **打包验证**：执行 `packaging/build_exe.ps1`，确认 `feature_flags.json` 被包含，打包版默认全功能。
4. **修改配置验证**：打包后修改 `resources/feature_flags.json`，重启确认功能按配置开关。
5. **语法编译**：`python -m py_compile ui/feature_flags.py ui/main_window.py main.py` 通过。
