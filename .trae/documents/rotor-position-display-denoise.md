# 电机转子位置显示抖动优化方案

## 摘要

电机静止时编码器存在 0.01° 级别的微小波动，导致转子位置实时显示明显抖动。本方案采用**多策略组合 + UI 层为主 + 新增 DisplayFilter 工具类**的方式，针对数值卡片、转子示意图、波形图三个显示通道分别优化，不改动数据链路，保证日志/导出仍为原始值。

## 现状分析

### 数据流路径
```
Transport(串口/虚拟引擎) → JmClient._on_frame → _merge_and_emit_feedback
→ MainWindow._queue_feedback (deque 缓存, 50ms UI tick 批量消费)
→ FeedbackPanel.update_feedback(last_fb)  // 只用最后一帧
   ├─ _card_motion 数值卡片: 机械角度/电角度直接显示原始 single
   └─ _MotorView.set_feedback(single, vel, iq, enabled)  // _tgt_th = single 原始值
→ PlotPanel.feed_feedback_batch(ts_list, fb_list)  // angle 曲线直接写原始 single
```

### 三处显示通道的滤波现状
| 通道 | 位置 | 当前处理 | 问题 |
|---|---|---|---|
| 数值卡片(机械/电角度) | feedback_panel.py:1109-1114 | 无任何滤波，`f"{mech_deg:.2f}"` | 静止时末两位 0.01° 抖动 |
| 转子示意图 | feedback_panel.py:666-671, 686-696 | `_tgt_th=single`原始值；有60fps航位推算+25%柔和拉向 | 目标值本身抖动，视觉上指针小幅度摆动 |
| 波形图 angle 曲线 | plot_panel.py:775-779 | 直接写原始 single 进环形缓冲 | 静止段呈毛刺状抖动带 |

### 已有的平滑机制（保留不动）
- `_MotorView._tick()` 60fps 航位推算 + `err * min(1.0, dt*3.0) * 0.25` 柔和拉向真值 — 保留
- `_MotorView.showEvent` 切回面板时 `_disp_th = _tgt_th` 直接对齐 — 保留
- 虚拟引擎 `twin_physics.py:300-301` 编码器位置低通滤波 — 保留（仅虚拟引擎，真机不受影响）

## 改动清单

### 1. 新增 `ui/display_filter.py`（核心工具类）

提供三个可复用的角度滤波器，均考虑角度周期性（用 `atan2(sin, cos)` 处理 0/2π 跨边界）。

```python
import math
import numpy as np


class AngleDeadZone:
    """静止死区 + 动态跟随。

    速度低于阈值时锁定角度不动；速度超过阈值时正常跟随。
    适用于转子示意图的 _tgt_th，避免静止时目标值抖动。
    """

    def __init__(self, vel_threshold=0.05, lock_decay=0.0):
        """
        :param vel_threshold: 速度阈值(rad/s)，低于此值认为静止。
                              0.05 rad/s ≈ 0.48 rpm，覆盖编码器噪声段
        :param lock_decay: 锁定后的角度衰减系数(0=完全锁定)，保留扩展位
        """
        self._vel_thr = float(vel_threshold)
        self._locked_th = None        # 锁定时的角度
        self._last_th = 0.0          # 上次输出的角度
        self._lock_decay = float(lock_decay)

    def reset(self, th=0.0):
        self._locked_th = None
        self._last_th = float(th)

    def update(self, th_rad: float, vel_rad_s: float) -> float:
        th = float(th_rad)
        if abs(vel_rad_s) < self._vel_thr:
            # 静止: 锁定到首次进入静止时的角度
            if self._locked_th is None:
                self._locked_th = self._last_th
            return self._locked_th
        else:
            # 运动: 解锁，正常跟随
            self._locked_th = None
            self._last_th = th
            return th


class MovingAverageAngle:
    """环形缓冲移动平均，跨批次连续。

    适用于波形图 angle 曲线，平滑静止段毛刺。
    用 atan2(sin_avg, cos_avg) 计算圆形均值，避免 359°↔1° 跳变。
    """

    def __init__(self, window=5):
        self._n = max(1, int(window))
        self._buf = np.zeros(self._n, dtype=np.float32)
        self._idx = 0
        self._filled = 0

    def reset(self):
        self._idx = 0
        self._filled = 0

    def update_batch(self, th_arr: np.ndarray) -> np.ndarray:
        """批量更新，返回与输入等长的滤波后数组。跨批次状态连续。"""
        out = np.empty_like(th_arr)
        n = self._n
        for i, th in enumerate(th_arr):
            self._buf[self._idx] = th
            self._idx = (self._idx + 1) % n
            if self._filled < n:
                self._filled += 1
            # 圆形均值: atan2(mean(sin), mean(cos))
            cnt = self._filled
            sin_sum = float(np.sin(self._buf[:cnt]).sum())
            cos_sum = float(np.cos(self._buf[:cnt]).sum())
            out[i] = math.atan2(sin_sum / cnt, cos_sum / cnt)
        return out


class PrecisionRounder:
    """精度截断器。

    四舍五入到指定小数位，等效于显示死区。
    适用于数值卡片，简单可靠。
    """

    def __init__(self, decimals=1):
        self._dec = int(decimals)

    def round(self, value: float) -> float:
        return round(float(value), self._dec)
```

### 2. 修改 `ui/panels/feedback_panel.py`

#### 2a. `_MotorView` 增加目标角度死区（行 634-670）

在 `__init__` 中创建 `AngleDeadZone` 实例，在 `set_feedback` 中过滤 `_tgt_th`：

```python
# __init__ 中新增(行 644 附近):
from ui.display_filter import AngleDeadZone
self._tgt_filter = AngleDeadZone(vel_threshold=0.05)

# set_feedback 改为(行 666-670):
def set_feedback(self, single_rad, omega, iq, enabled):
    filtered = self._tgt_filter.update(float(single_rad), float(omega))
    self._tgt_th = filtered
    self._omega = float(omega)
    self._iq = float(iq)
    self._enabled = bool(enabled)

# showEvent 中重置死区(行 654-660), 避免切回时锁定到旧值:
def showEvent(self, e):
    super().showEvent(e)
    self._tgt_filter.reset(self._tgt_th)
    self._disp_th = self._tgt_th
    self._last_ms = self._clock.elapsed()
    self._timer.start()
    self.update()
```

**效果**：静止时 `_tgt_th` 锁定，60fps 平滑拉向真值的 `err` 为 0，指针完全不动；运动时正常跟随。

#### 2b. 数值卡片显示精度优化（行 1109-1114）

机械角度/电角度从 2 位小数改为 1 位小数，等效 0.1° 显示死区：

```python
single = float(getattr(fb, "single", 0.0) or 0.0)
mech_deg = math.degrees(single) % 360.0
elec_rad = (single * self._pole_pairs + self._elec_bias) % (2.0 * math.pi)
elec_deg = math.degrees(elec_rad) % 360.0
self._card_motion.set_text("_mech_deg", f"{mech_deg:.1f}")   # 2f → 1f
self._card_motion.set_text("_elec_deg", f"{elec_deg:.1f}")  # 2f → 1f
```

**效果**：0.01° 级别抖动被四舍五入吸收，静止时数值稳定不跳。1 位小数对机械角度足够（0.1° 分辨率高于肉眼可感知的转动）。

**不使用 `PrecisionRounder` 类**：数值卡片用 f-string 精度控制已足够，无需引入工具类增加复杂度。

#### 2c. 速度卡片显示优化（行 976）

速度卡片同样静止时抖动，从 4 位小数改为 3 位：

```python
("电机速度", "vel", "{:+.3f}", "rad/s"),   # 4f → 3f
```

### 3. 修改 `ui/panels/plot_panel.py`

#### 3a. angle 曲线加移动平均（行 775-779）

在 `PlotPanel.__init__` 中创建 `MovingAverageAngle` 实例，在 `feed_feedback_batch` 中对 angle 数组做滤波：

```python
# __init__ 中新增(行 360 附近):
from ui.display_filter import MovingAverageAngle
self._angle_ma = MovingAverageAngle(window=5)

# feed_feedback_batch 中 angle 段改为(行 775-779):
ys_angle = np.empty(n, dtype=np.float32)
for i in range(n):
    ys_angle[i] = getattr(fb_list[i], 'single', 0.0)
ys_angle = self._angle_ma.update_batch(ys_angle)   # 移动平均
self._buffers['angle'].append_batch(ts_arr, ys_angle)
```

**效果**：窗口 N=5，50Hz 遥测下等效 100ms 平滑，静止段毛刺显著减弱，运动段响应延迟仅 2 帧（40ms）可忽略。

#### 3b. 单帧路径 `feed_feedback`（行 745-746）保持一致

```python
single = float(getattr(fb, 'single', 0.0))
single = float(self._angle_ma.update_batch(np.array([single], dtype=np.float32))[0])
self._buffers['angle'].append(t, single)
```

### 4. 不改动的部分

- `core/motor_client.py` 的 `_merge_and_emit_feedback` — 数据合并层保持原始值，日志/导出/其他面板不受影响
- `jmproto/feedback.py` 的 `parse_telemetry` — 协议解析层不动
- `transport/virtual_engine/twin_physics.py` — 虚拟引擎物理层不动
- `_MotorView._tick()` 的 60fps 航位推算和平滑系数 — 保留现有视觉平滑

## 假设与决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 死区速度阈值 | 0.05 rad/s (≈0.48 rpm) | 覆盖编码器噪声段，低于人工感知的"动"的阈值 |
| 数值卡片精度 | 1 位小数 | 0.1° 分辨率足够，肉眼无法感知更小转动 |
| 波形图移动平均窗口 | N=5 (100ms @ 50Hz) | 平衡平滑度与响应延迟 |
| 滤波层级 | UI 显示层 | 不污染数据链路，日志/导出仍为原始值 |
| 工具类位置 | `ui/display_filter.py` | UI 专用滤波逻辑，与 theme.py 同层 |
| 电角度是否单独滤波 | 否，跟随 single 滤波后计算 | elec = single*pp+bias，single 平滑后 elec 自然平滑 |

## 验证步骤

1. **静止抖动验证**：
   - 连接虚拟引擎或真机，电机静止
   - 观察数值卡片：机械角度/电角度数值应稳定不跳（1 位小数）
   - 观察转子示意图：指针完全不动（死区锁定）
   - 观察波形图：angle 曲线静止段为直线，无毛刺

2. **运动跟随验证**：
   - 启动电机匀速旋转
   - 转子示意图指针匀速顺滑旋转（航位推算+死区解锁）
   - 数值卡片正常更新
   - 波形图曲线平滑跟随，延迟可接受（<100ms）

3. **跨边界验证**：
   - 让角度经过 0°/360° 边界
   - 确认数值卡片正确显示（不出现 359°→0° 跳变异常）
   - 确认波形图移动平均不因跨边界产生错误（atan2 圆形均值）

4. **面板切换验证**：
   - 切到其他 tab 再切回反馈面板
   - 转子示意图应直接对齐到当前角度，不缓慢旋转（showEvent reset 生效）

5. **速度死区边界验证**：
   - 电机极低速旋转（刚超过 0.05 rad/s）
   - 确认死区正确解锁，指针开始跟随
