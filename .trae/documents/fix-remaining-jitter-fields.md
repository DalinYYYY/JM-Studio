# 修复转子位置显示残余抖动（3 个漏网字段）

## 摘要

前序改动已正确滤波 `_mech_deg`/`_elec_deg`/`vel`/转子示意图，但有 **3 个字段绕过了滤波**直接用原始 `fb` 值显示，导致静止时仍抖动：
1. `_card_motion` 的 `pos`（电机位置）— `{:+.4f}` 4 位小数，原始 `fb.pos`
2. `_card_motion` 的 `multiturn`（多圈计数）— 原始 `fb.multiturn`，int 跳变
3. FOC 框图 `thw` — 原始 `fb.single` + `fb.vel`

## 现状分析

### update_feedback 调用顺序（feedback_panel.py:1107-1151）

```
1109: self._card_motion.update_values(fb)     # ← pos/multiturn 在此用原始值写入
1119: raw_single = fb.single
1121: single = self._single_filter.update(...)  # 滤波
1122: disp_vel = 0.0 if is_locked else vel     # 速度置零
1126: set_text("_mech_deg", ...)               # ← 覆盖 ✓
1127: set_text("_elec_deg", ...)              # ← 覆盖 ✓
1129: set_text("vel", ...)                     # ← 覆盖 ✓
1132: _motor.set_feedback(single, disp_vel, ...) # ✓
1150: "thw": f("{f('single','.3f')}rad / {f('vel','.2f')}")  # ← 原始 fb！✗
```

**pos 和 multiturn 被 update_values 写入后，没有 set_text 覆盖**，滤波完全失效。

### 字段关系
- `single` = 单圈角度 [0, 2π) rad — 已滤波
- `multiturn` = 多圈计数（int）— `fb.multiturn = int(total / 2π)`，编码器量化噪声会让 total 在 2π 边界抖动 → multiturn 跳 ±1
- `pos` = 多圈位置（rad，浮点）— `pos = multiturn * 2π + single`，精度 4 位小数（0.0001rad ≈ 0.0057°），远超编码器分辨率

### _MotorView 已确认正确
`_tick()` 锁定时（omega=0, tgt=常量）：`_disp_th += 0`、`err=0`、`_disp_th += 0` → 完全静止。问题不在示意图，在 3 个漏网字段。

## 改动清单

### 1. feedback_panel.py — pos / multiturn 用滤波后值覆盖

在 `update_feedback` 中，`update_values(fb)` 之后用 `set_text` 覆盖 `pos` 和 `multiturn`：

```python
# 在 1129 行 set_text("vel", ...) 之后追加:

# pos(多圈位置) 与 multiturn: 锁定时保持上次值不变, 避免编码器噪声跳变。
# pos 与 single/multiturn 的关系: pos = multiturn * 2π + single,
# 锁定时 single 已锁定, pos/multiturn 也应锁定。
if self._single_filter.is_locked:
    self._card_motion.set_text("pos", f"{self._disp_pos:+.4f}")
    self._card_motion.set_text("multiturn", str(self._disp_multiturn))
else:
    self._disp_pos = float(getattr(fb, "pos", self._disp_pos) or self._disp_pos)
    self._disp_multiturn = int(getattr(fb, "multiturn", self._disp_multiturn) or 0)
    self._card_motion.set_text("pos", f"{self._disp_pos:+.4f}")
    self._card_motion.set_text("multiturn", str(self._disp_multiturn))
```

在 `FeedbackPanel.__init__` 中新增成员变量（约 931 行附近）：
```python
self._disp_pos = 0.0        # 解锁时缓存的 pos, 锁定时保持不变
self._disp_multiturn = 0    # 解锁时缓存的 multiturn, 锁定时保持不变
```

### 2. feedback_panel.py — FOC 框图 thw 用滤波后变量

将第 1150 行从原始 fb 改为已滤波的局部变量：

```python
# 原: "thw": f"{f('single','{:.3f}')}rad / {f('vel','{:.2f}')}",
# 改: 用 1121-1122 行已算好的 single 和 disp_vel
"thw": f"{single:.3f}rad / {disp_vel:+.2f}",
```

### 3. feedback_panel.py — set_link_active 重置缓存

在 `set_link_active`（约 1218 行）已有 `_single_filter.reset(0.0)`，追加缓存重置：

```python
self._single_filter.reset(0.0)
self._disp_pos = 0.0
self._disp_multiturn = 0
```

## 不改动的部分

- `_MotorView._tick()` / `set_feedback` / `showEvent` — 已确认锁定时完全静止
- `AngleDeadZone` — 逻辑正确（pos_threshold=0.05, vel_threshold=0.5, is_locked）
- `MovingAverageAngle` — 波形图 window=8 保持（次要优化，用户主要抱怨是数值卡片和示意图）
- `_card_bus` / `_card_state` — 不含位置字段，无需改

## 假设与决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| pos 锁定策略 | 缓存上次解锁值, 锁定时不变 | pos 是多圈累积值, 与 single 锁定状态一致 |
| multiturn 锁定策略 | 同 pos | multiturn 跳变最明显(int ±1) |
| thw 数据源 | 用滤波后的 single/disp_vel 局部变量 | 变量已存在, 无需额外计算 |
| pos 精度 | 保持 4 位小数 | 锁定后值不变, 精度不再导致抖动 |

## 验证步骤

1. **静止验证**：电机静止，观察：
   - 电机位置 pos 数值稳定不跳
   - 多圈计数 multiturn 不跳
   - FOC 框图 thw 文本稳定
   - 机械角度/电角度/速度/转子示意图均静止（已验证）
2. **运动验证**：电机旋转，观察 pos/multiturn/thw 正常更新跟随
3. **跨边界验证**：角度经过 0/360° 边界，multiturn 正常进位/退位
4. **断开重连验证**：断开后重连，pos/multiturn 从 0 开始正常累积
