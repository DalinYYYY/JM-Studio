# 关节电机通信协议参考手册

> **目标读者**: 需要独立实现一个波形显示上位机的开发者
> **配套代码**: `tools/waveform/protocol_reference.py` (可 import 的纯 Python 实现, 零外部依赖)
> **协议版本**: 与固件 `User/Protocol/joint_proto/jm_cmd_def.h` 一致

---

## 0. 速览

| 项目 | 说明 |
|------|------|
| 物理链路 | 串口 (RS232/UART) / CAN 总线 / 本机回环虚拟电机 |
| 串口帧格式 | `A5 5A + LEN(2B 大端) + HDR_CHK + CMD+Payload + CRC16(2B 小端)` |
| CAN 帧 ID | 扩展帧 29 位, `ID = (CMD << 8) \| motor_id` |
| 校验算法 | CRC16-CCITT/XMODEM (多项式 0x1021, 初值 0, MSB-first, 无反转, 无异或) |
| 字节序 | 多字节小端 (LE), 浮点 IEEE-754 f32 |
| 命令空间 | 0x00 ~ 0xFE, 按功能分 9 段 |
| 错误应答 | `NACK (0xFE) + cmd + err_code` |
| 反馈模型 | 主从轮询 (READ_*) 或 同步遥测订阅 (SET_TELEMETRY + 周期 TELEMETRY 帧) |

**波形上位机推荐方案**: 使用 `SET_TELEMETRY (0xCB)` 订阅周期推送, 通过 `TELEMETRY (0xCA)` 帧获取所需字段 (位置/速度/电流/力矩等), 单帧可携带多通道数据, 比逐项轮询效率高 10 倍以上。详见 [§7 同步遥测](#7-同步遥测-telemetry)。

---

## 1. 传输介质

### 1.1 串口 (RS232 / UART)

- 8N1, 默认 115200 bps (最高支持 921600)
- 二进制帧, 见 [§2 帧格式](#2-串口帧格式)
- 上位机 TX 一帧, 下位机 RX 一帧 (或主动周期推送遥测)
- 推荐 `pyserial` 库, 关键约束: 必须串行化对串口句柄的所有访问 (read/write/close 互斥), 否则 Windows 上会段错误

### 1.2 CAN 总线

- 扩展帧 (29 位 ID), 默认 1 Mbps
- 仲裁 ID = `(CMD << 8) | motor_id`, motor_id=0 为广播
- 单帧数据区 8B; >8B 走多帧分包协议
- MIT/IMPEDANCE 命令及反馈帧可定点压缩进 8B
- 推荐 `python-can` 库, 见 [§3 CAN 帧格式](#3-can-帧格式)

### 1.3 虚拟电机 (本机回环)

- 不依赖硬件, 用于上位机离线调试
- 协议层与串口完全一致, 下位机由 `VirtualMotor` 类模拟
- `tools/waveform/protocol_reference.py` 自带实现, 可直接复用
- 见 [§9 虚拟电机](#9-虚拟电机-本机回环)

---

## 2. 串口帧格式

```
┌──────┬──────┬────────┬────────┬─────────┬──────────────┬───────────┐
│ STX_H│ STX_L│ LEN_H  │ LEN_L  │ HDR_CHK │ CMD + Payload│  CRC_L    │  CRC_H  │
│ 0xA5 │ 0x5A │ (大端) │ (大端) │  (1B)   │   (LEN B)    │ (小端)   │ (小端)   │
└──────┴──────┴────────┴────────┴─────────┴──────────────┴───────────┘
```

| 字段 | 字节 | 说明 |
|------|------|------|
| STX_H, STX_L | 2 | 帧头固定 `A5 5A` |
| LEN | 2 (大端) | CMD(1) + Payload(n) 总字节数, 范围 1 ~ 1024 |
| HDR_CHK | 1 | 头校验 = `(STX_H + STX_L + LEN_H + LEN_L) & 0xFF` |
| CMD | 1 | 命令码, 见 [§5 命令表](#5-命令码全表) |
| Payload | n | 命令载荷 (可为 0 字节), 小端字节序 |
| CRC16 | 2 (小端) | CRC16-XMODEM, 覆盖 `CMD + Payload` |

**示例**: `ENABLE` 命令 (CMD=0x04, 无载荷)
```
A5 5A 00 01 00 04 <CRC_L> <CRC_H>
   │  │     │  │
   │  │     │  └─ HDR_CHK = (A5+5A+00+01) & FF = 00
   │  │     └──── LEN = 1 (仅 CMD)
   │  └────────── LEN_L
   └───────────── LEN_H
```

---

## 3. CAN 帧格式

### 3.1 仲裁 ID

```
bit28 ───────────────── bit8 bit7 ── bit0
   ┌────────────────────┬──────────┐
   │      CMD (8bit)    │ motor_id │
   └────────────────────┴──────────┘
```

- `can_id = (CMD << 8) | motor_id`
- `motor_id = 0`: 广播 (不应答)
- `motor_id = 1 ~ 127`: 单机寻址

### 3.2 单帧 (≤ 8B 载荷)

数据区 8 字节直接放命令载荷 (不含 CMD), 不足补 `0x00`。

### 3.3 多帧分包 (> 8B 载荷)

每帧 8 字节, 首字节为控制字:

```
data[0] = (is_last << 7) | (seq & 0x7F)
data[1..7] = 7B 分片
```

- `is_last = 1`: 末帧
- `seq`: 从 0 递增

重组: 按 `cmd` 维护缓冲区, 收到 `is_last=1` 时合并所有分片为完整 payload。

### 3.4 MIT / IMPEDANCE 命令定点压缩 (8B)

适用于 `CMD=0x13 (MIT)` 与 `CMD=0x30 (IMPEDANCE)`, 把 5 个浮点参数压进 8 字节:

```
┌─────────┬─────────┬─────────┬─────────┬─────────┐
│ pos(16) │ vel(12) │ kp (12) │ kd (12) │ tff(12) │
└─────────┴─────────┴─────────┴─────────┴─────────┘
       大端拼装, 共 64 bit
```

| 字段 | bits | 范围 |
|------|------|------|
| pos  | 16 | [-12.5, 12.5] rad |
| vel  | 12 | [-65, 65] rad/s |
| kp   | 12 | [0, 500] |
| kd   | 12 | [0, 5] |
| tff  | 12 | [-50, 50] Nm |

公式: `raw = (value - min) / (max - min) * (2^bits - 1)`

### 3.5 反馈帧压缩 (8B)

`READ_FEEDBACK` 的 CAN 版本, 把 5 个反馈量压进 8 字节:

```
┌─────────┬─────────┬─────────┬─────────┬────────┐
│ pos(16) │ vel(16) │ tq (16) │ temp(8) │ err(8) │
└─────────┴─────────┴─────────┴─────────┴────────┘
```

| 字段 | bits | 范围 |
|------|------|------|
| pos  | 16 | [-12.5, 12.5] rad |
| vel  | 16 | [-65, 65] rad/s |
| tq   | 16 | [-50, 50] Nm |
| temp | 8  | [-40, 215] °C |
| err  | 8  | 故障码 |

---

## 4. CRC16-XMODEM

```python
def crc16_calc(data: bytes) -> int:
    crc = 0x0000
    for b in data:
        crc = ((crc << 8) ^ table[((crc >> 8) ^ b) & 0xFF]) & 0xFFFF
    return crc
```

- 多项式: `0x1021`
- 初值: `0x0000`
- 输入/输出反转: 否
- 最终异或: `0x0000`
- 覆盖范围: `CMD + Payload` (不含帧头/长度/头校验)
- 落帧字节序: 小端 (CRC_L 在前, CRC_H 在后)

---

## 5. 命令码全表

完整 103 条命令, 数值与固件 `jm_cmd_def.h` 严格一致。下方按功能分类, 仅列出常用命令; 完整 CSV 见 `resources/joint_motor_command_list.csv`。

### 5.1 系统控制 (0x00 ~ 0x0F)

| CMD | 名称 | 请求载荷 | 应答 | 说明 |
|-----|------|----------|------|------|
| 0x00 | IDLE | 无 | `ACK{state:u8}` | 进入待机, 停止输出 |
| 0x01 | HOLD | 无 | `ACK{state:u8}` | 锁定当前位置 |
| 0x02 | BRAKE | 无 | `ACK{state:u8}` | 机械刹车, 短接相线 |
| 0x03 | ESTOP | 无 | `ACK{state:u8}` | 紧急停止, 最高优先级 |
| 0x04 | ENABLE | 无 | `ACK{state:u8}` | IDLE -> READY, 伺服使能 |
| 0x05 | DISABLE | 无 | `ACK{state:u8}` | READY/RUN -> IDLE, 失能 |
| 0x06 | STOP | 无 | `ACK{state:u8}` | RUN -> READY, 停运动保持使能 |

### 5.2 运动控制 (0x10 ~ 0x2F)

| CMD | 名称 | 请求载荷 | 字节数 | 单位 |
|-----|------|----------|--------|------|
| 0x10 | OPEN_LOOP | `{ud:f32; uq:f32}` | 8 | V |
| 0x11 | CURRENT | `{id_ref:f32; iq_ref:f32}` | 8 | A |
| 0x12 | TORQUE | `{torque:f32}` | 4 | Nm |
| 0x13 | MIT | `{pos:f32; vel:f32; kp:f32; kd:f32; tff:f32}` | 20 | rad / rad·s⁻¹ / Nm |
| 0x14 | VELOCITY | `{vel_ref:f32}` | 4 | rad/s |
| 0x15 | POSITION | `{pos_ref:f32}` | 4 | rad |
| 0x16 | POSITION_VELOCITY | `{pos:f32; vel_ff:f32}` | 8 | rad / rad·s⁻¹ |
| 0x17 | POSITION_TORQUE | `{pos:f32; tq_lim:f32}` | 8 | rad / Nm |
| 0x18 | VELOCITY_TORQUE | `{vel:f32; tq_lim:f32}` | 8 | rad·s⁻¹ / Nm |
| 0x19 | DUTY_CYCLE | `{duty:f32}` | 4 | -1 ~ 1 |
| 0x1A | VOLTAGE_VECTOR | `{u_alpha:f32; u_beta:f32}` | 8 | V |
| 0x1B | FIELD_WEAKENING | `{id_weak:f32; iq_ref:f32}` | 8 | A |
| 0x1C | SENSORLESS | `{vel_ref:f32}` | 4 | rad/s |

### 5.3 高级力控 (0x30 ~ 0x4F)

| CMD | 名称 | 请求载荷 |
|-----|------|----------|
| 0x30 | IMPEDANCE | `{pos:f32; vel:f32; kp:f32; kd:f32; tff:f32}` (CAN 同 MIT 压缩) |
| 0x31 | ADMITTANCE | `{force:f32; mass:f32; damp:f32; stiff:f32}` |
| 0x32 | FORCE_CONTROL | `{force:f32}` |
| 0x33 | FORCE_POSITION_HYBRID | `{pos:f32; force:f32; sel_mask:u32}` |
| 0x34 | GRAVITY_COMPENSATION | 无 |
| 0x35 | COLLISION_DETECTION | `{threshold:f32; enable:u8}` |
| 0x36 | ZERO_FORCE | 无 (拖动示教) |
| 0x37 | CONSTANT_FORCE | `{force:f32}` |
| 0x38 | VARIABLE_IMPEDANCE | `{kp:f32; kd:f32; rate:f32}` |
| 0x39 | ADAPTIVE_GRAVITY_COMP | `{gain:f32}` |
| 0x3A | LANDING_BUFFER | `{stiffness:f32; damp:f32}` |

### 5.4 轨迹同步 (0x50 ~ 0x6F)

| CMD | 名称 | 请求载荷 |
|-----|------|----------|
| 0x50 | PVT | `{pos:f32; vel:f32; time_ms:u32}` |
| 0x51 | CUBIC_SPLINE | `{seg_idx:u16; coef[4]:f32}` |
| 0x52 | TRAPEZOIDAL_TRAJ | `{target:f32; vmax:f32; acc:f32}` |
| 0x53 | S_CURVE_TRAJ | `{target:f32; vmax:f32; acc:f32; jerk:f32}` |
| 0x54 | HOMING | `{method:u8}` 应答 `{homed:u8}` |
| 0x55 | CANOPEN_SYNC | 无 (CANopen SYNC 对象) |
| 0x56 | ETHERCAT_CSP | `{pos:f32}` |
| 0x57 | ETHERCAT_CSV | `{vel:f32}` |
| 0x58 | ETHERCAT_CST | `{torque:f32}` |
| 0x59 | PP | `{pos:f32; vel:f32}` |
| 0x5A | PV | `{vel:f32; acc:f32}` |
| 0x5B | PT | `{torque:f32; slope:f32}` |
| 0x5C | ELECTRONIC_GEAR | `{ratio_num:i32; ratio_den:i32}` |
| 0x5D | ELECTRONIC_CAM | `{cam_table_id:u16}` |

### 5.5 特殊应用与测试 (0x70 ~ 0x8F)

| CMD | 名称 | 请求载荷 |
|-----|------|----------|
| 0x70 | STEP_DIR | `{pulse_per_rev:u32}` |
| 0x71 | ANALOG_INPUT | `{ch:u8; scale:f32}` |
| 0x72 | PWM_INPUT | `{min_us:u16; max_us:u16}` |
| 0x73 | JOG | `{dir:i8; speed:f32}` |
| 0x74 | SAFE_TEACH | 无 |
| 0x75 | TEST_AGING | `{cycles:u32}` |
| 0x76 | TEST_SWEEP_FREQ | `{f_start:f32; f_end:f32; amp:f32}` |
| 0x77 | TEST_COGGING | 无 |
| 0x78 | TEST_FRICTION | 无 |
| 0x79 | TEST_INERTIA | 无 |
| 0x7A | TEST_CURRENT_LOOP | `{amp:f32; freq:f32}` |
| 0x7B | TEST_VELOCITY_LOOP | `{amp:f32; freq:f32}` |

### 5.6 校准 (0x90 ~ 0xAF)

| CMD | 名称 | 应答载荷 |
|-----|------|----------|
| 0x90 | CALIB_MOTOR_PARAM | `{R:f32; Ld:f32; Lq:f32; flux:f32}` |
| 0x91 | CALIB_ENCODER_OFFSET | `{offset:i32; ebias:f32}` |
| 0x92 | CALIB_ENCODER_LINEARITY | `{progress:u8}` |
| 0x93 | CALIB_TORQUE_CONST | `{kt:f32}` |
| 0x94 | CALIB_COGGING_COMP | `{progress:u8}` |
| 0x95 | CALIB_FRICTION_COMP | `{coulomb:f32; viscous:f32}` |
| 0x96 | CALIB_INERTIA | `{inertia:f32}` |
| 0x97 | CALIB_ADC_OFFSET | `{ia_off:i16; ib_off:i16; ic_off:i16}` |
| 0x98 | CALIB_ADC_GAIN | `{gain:f32}` |
| 0x99 | CALIB_CURRENT_SENSOR | `{status:u8}` |
| 0x9A | CALIB_TEMPERATURE | 请求 `{ref_temp:f32}`, 应答 `{status:u8}` |
| 0x9B | CALIB_FULL_AUTO | `{progress:u8; step:u8}` |

### 5.7 系统诊断 (0xB0 ~ 0xBF)

| CMD | 名称 | 请求载荷 | 应答 |
|-----|------|----------|------|
| 0xB0 | CLEAR_FAULT | 无 | `ACK{state:u8}` (FAULT -> IDLE) |
| 0xB1 | DIAGNOSTIC | 无 | `{diag:bytes}` |
| 0xB2 | ENTER_BOOTLOADER | `{magic:u32=0xB00710AD}` | ACK (需魔数防误触) |
| 0xB3 | SAVE_CONFIG | 无 | `ACK{status:u8}` (写 Flash) |
| 0xB4 | FACTORY_RESET | `{magic:u32=0xFAC70F5F}` | `ACK{status:u8}` |
| 0xB5 | START_LOG | `{rate_hz:u16; mask:u32}` | ACK |
| 0xB6 | STOP_LOG | 无 | ACK |
| 0xB7 | HIGH_SPEED_DAQ | `{ch_mask:u32; rate_hz:u32}` | ACK |
| 0xB8 | SINGLE_STEP | 无 | ACK |

### 5.8 反馈查询 (0xC0 ~ 0xCF) — **波形显示核心**

| CMD | 名称 | 请求载荷 | 应答载荷 |
|-----|------|----------|----------|
| 0xC0 | READ_FEEDBACK | 无 | `{pos:f32; vel:f32; torque:f32; temp_motor:f32; vbus:f32; fault_mask:u16}` (22B) |
| 0xC1 | READ_STATE | 无 | `{top_fsm:u8; run_state:u8; ctrl_mode:u8; enable:u8}` (4B) |
| 0xC2 | READ_PHASE_CURRENT | 无 | `{ia:f32; ib:f32; ic:f32}` (12B) |
| 0xC3 | READ_DQ_CURRENT | 无 | `{id:f32; iq:f32}` (8B) |
| 0xC4 | READ_BUS | 无 | `{vbus:f32; ibus:f32; power:f32}` (12B) |
| 0xC5 | READ_TEMPERATURE | 无 | `{temp_fet:f32; temp_motor:f32}` (8B) |
| 0xC6 | READ_POS_VEL | 无 | `{pos:f32; vel:f32}` (8B) |
| 0xC7 | READ_MULTITURN | 无 | `{multiturn:i32; single:f32}` (8B) |
| 0xC8 | READ_FAULT | 无 | `{fault_mask:u32; warn_mask:u32}` (8B) |
| 0xC9 | READ_DEBUG | 无 | ACK (代码未实现专用返回) |
| 0xCA | **TELEMETRY** | 无 (下位机主动推) | `{mask:u16; data:bytes}` 变长, 见 [§7](#7-同步遥测-telemetry) |
| 0xCB | SET_TELEMETRY | `{enable:u8; mask:u16; period_ms:u16}` (5B) | `ACK{status:u8}` |

### 5.9 设备信息 (0xD0 ~ 0xDF)

| CMD | 名称 | 应答载荷 |
|-----|------|----------|
| 0xD0 | READ_DEV_INFO | `{hw_ver:u32; fw_ver:u32; uid:bytes12}` (20B) |
| 0xD1 | READ_DEV_NAME | `{name:char[16]}` |
| 0xD2 | HEARTBEAT | `{state:u8; err:u16; ts:u32}` (可周期主动上报) |

### 5.10 参数读写 (0xE0 ~ 0xEF)

| CMD | 名称 | 请求载荷 | 应答载荷 |
|-----|------|----------|----------|
| 0xE0 | PARAM_READ | `{param_id:u16}` | `{param_id:u16; type:u8; value:bytes}` |
| 0xE1 | PARAM_WRITE | `{param_id:u16; value:bytes}` | `ACK{param_id:u16; status:u8}` |
| 0xE2 | PARAM_READ_BULK | `{start_id:u16; count:u16}` | `{values:bytes}` |
| 0xE3 | PARAM_WRITE_BULK | `{start_id:u16; count:u16; values:bytes}` | `ACK{status:u8}` |
| 0xE4 | PARAM_SAVE | 无 | `ACK{status:u8}` |
| 0xE5 | PARAM_RESET | `{param_id:u16=0xFFFF}` | `ACK{status:u8}` |

`type` 字段取值: 0=u8, 1=i8, 2=u16, 3=i16, 4=u32, 5=i32, 6=f32, 7=str

### 5.11 CAN 管理 (0xF0 ~ 0xFF)

| CMD | 名称 | 请求载荷 | 应答 |
|-----|------|----------|------|
| 0xF0 | SET_CAN_ID | `{new_id:u8}` (1~127) | `ACK{new_id:u8}` |
| 0xF1 | SET_BAUDRATE | `{baud_code:u8}` (0=1M, 1=500K, 2=250K, 3=125K) | ACK |
| 0xF2 | BROADCAST_SYNC | 无 (ID=0 广播) | 无 (所有电机同步执行缓存指令) |
| 0xFE | NACK | — | `{cmd:u8; err_code:u8}` (任意命令失败时返回) |

---

## 6. ACK / NACK 约定

### 6.1 ACK

- 适用范围: CMD ∈ [0x00, 0xB8] 的命令 (系统控制 / 运动 / 力控 / 轨迹 / 特殊 / 校准 / 诊断)
- 载荷首字节为状态码 `status:u8`
  - `0x00 (JmErr.OK)`: 成功
  - 其他: 失败 (按 NACK 处理, 见 [§8 错误码](#8-错误码))
- 部分命令在 ACK 后追加返回值 (如校准结果), 见命令表

### 6.2 NACK (0xFE)

任意命令处理失败时下位机返回 NACK 帧:
```
payload = {cmd:u8; err_code:u8}
```
- `cmd`: 触发 NACK 的命令码
- `err_code`: 错误码, 见 [§8 错误码](#8-错误码)

---

## 7. 同步遥测 TELEMETRY

**这是波形显示上位机的核心机制**。下位机按配置周期主动推送一组数据, 单帧可携带多个通道, 比逐项 `READ_*` 轮询效率高 10 倍以上。

### 7.1 启停订阅 (0xCB)

```
请求载荷: {enable:u8; mask:u16; period_ms:u16}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| enable | u8 | 1=启动周期上报, 0=停止 |
| mask | u16 | 通道位掩码, 见 [§7.2](#72-通道位掩码) |
| period_ms | u16 | 上报周期 (ms), 推荐 5~20ms |

### 7.2 通道位掩码

| 位 | 值 | 名称 | 字段 | 字节数 | 单位 |
|----|----|------|------|--------|------|
| 0 | 0x0001 | POS_VEL | pos, vel | 4+4 | rad, rad/s |
| 1 | 0x0002 | DQ | id, iq | 4+4 | A |
| 2 | 0x0004 | PHASE | ia, ib, ic | 4+4+4 | A |
| 3 | 0x0008 | BUS | vbus, ibus, power | 4+4+4 | V, A, W |
| 4 | 0x0010 | TEMP | temp_fet, temp_motor | 4+4 | °C |
| 5 | 0x0020 | MULTITURN | multiturn, single | 4+4 | -, rad |
| 6 | 0x0040 | TORQUE | torque | 4 | Nm |
| 7 | 0x0080 | FAULT | fault_mask, warn_mask | 4+4 | -, - |
| 8 | 0x0100 | STATE | top_fsm, run_state, ctrl_mode, enable, motion_state | 1+1+1+1+1 | -, -, -, -, - |
| 9 | 0x0200 | DEBUG | debug[...] (变长 f32 数组, 通道数由固件 JM_DBG_CH 决定) | 4×N | - |

### 7.3 遥测帧格式 (0xCA)

```
payload = mask:u16 (小端) + 按位序拼接的数据组
```

- 位序与 [§7.2](#72-通道位掩码) 表格一致 (bit0 在前)
- 每个 mask 选中的组按顺序追加, 组内字段顺序亦按表
- 例: mask=0x0041 (POS_VEL + TORQUE)
  ```
  payload = [mask_L, mask_H, pos(4B), vel(4B), torque(4B)]
  ```
- DEBUG 组取帧剩余字节, 每 4B 一个 f32

### 7.4 推荐订阅组合

| 用途 | mask (hex) | 通道 |
|------|------------|------|
| 基础运动监控 | 0x0041 | POS_VEL + TORQUE (12B/帧) |
| 电流环调试 | 0x0042 | DQ + TORQUE (12B/帧) |
| FOC 全量 | 0x0047 | POS_VEL + DQ + PHASE (28B/帧) |
| 完整状态 | 0x01C1 | POS_VEL + TORQUE + FAULT + STATE (22B/帧) |
| 调试通道 | 0x0200 | DEBUG (变长, 含固件自定义通道) |
| 全开 | 0x03FF | 所有 10 组 (调试用, 流量大) |

### 7.5 解析示例 (Python)

```python
from tools.waveform.protocol_reference import (
    FrameCodec, FrameDecoder, JmCmd, JmTlmBit, parse_telemetry, FeedbackData
)

decoder = FrameDecoder()

def on_serial_rx(raw_bytes: bytes):
    for cmd, payload in decoder.feed(raw_bytes):
        if cmd == JmCmd.TELEMETRY:
            fb, filled = parse_telemetry(payload)
            # fb.pos / fb.vel / fb.torque / fb.id / fb.iq ... 已按 mask 填充
            # filled 是有效字段名元组, 用于决定是否更新对应曲线
            update_waveform(fb, filled)
```

---

## 8. 错误码

`JmErr` 枚举 (NACK 应答的 `err_code` 字段):

| 值 | 名称 | 中文 |
|----|------|------|
| 0x00 | OK | 成功 |
| 0x01 | UNSUPPORTED | 命令不支持 |
| 0x02 | OUT_OF_RANGE | 参数超出范围 |
| 0x03 | STATE_DENY | 状态拒绝 (当前状态不允许此操作) |
| 0x04 | BAD_PARAM_ID | 参数 ID 无效 |
| 0x05 | CRC | CRC 校验失败 |
| 0x06 | LENGTH | 载荷长度错误 |
| 0x07 | READ_ONLY | 参数只读 |
| 0x08 | FLASH | FLASH 读写错误 |
| 0x09 | FAULT_STATE | 故障状态 (需先清除故障) |
| 0x0A | CALIB_BUSY | 标定忙 |

---

## 9. 状态机

### 9.1 顶层状态 `top_fsm_e`

| 值 | 名称 | 中文 | 说明 |
|----|------|------|------|
| 0 | INIT | 初始化 | 系统初始化 |
| 1 | SAFETY | 急停/安全 | 最高优先级, 任意态可进入 |
| 2 | FAULT | 故障 | 任意态可进入 |
| 3 | IDLE | 待机 | 伺服失能 |
| 4 | READY | 就绪 | 已使能, 等待运行指令 |
| 5 | RUN | 运行 | 运行子状态生效 |
| 6 | CALIB | 校准 | — |
| 7 | CONFIG | 配置 | — |
| 8 | BOOTLOADER | 升级 | 固件升级 |

### 9.2 运行子状态 `run_state_e`

数值与 `JmCmd` 运动控制段一致 (0x10~0x7B), 见 `RunState` 枚举。常用:

| 值 | 名称 | 中文 |
|----|------|------|
| 0 | IDLE | 空闲 |
| 1 | OPEN_LOOP | 开环 |
| 2 | CURRENT | 电流环 |
| 3 | TORQUE | 力矩环 |
| 4 | MIT | MIT |
| 5 | VELOCITY | 速度环 |
| 6 | POSITION | 位置环 |
| 10 | DUTY_CYCLE | 占空比 |
| 14 | IMPEDANCE | 阻抗 |

### 9.3 运动子状态 (twin 扩展)

仅虚拟电机在 TELEMETRY STATE 组的第 5 字节上报:

| 值 | 含义 |
|----|------|
| 0 | STANDSTILL 静止 |
| 1 | MOVING 运动中 |
| 2 | DECEL 减速中 |
| 3 | HOLDING 保持中 |
| 4 | BRAKING 制动中 |

---

## 10. 字段编码格式

### 10.1 标量类型

| 类型 | 字节 | struct 格式 | 范围 |
|------|------|-------------|------|
| u8 | 1 | `<B` | 0 ~ 255 |
| i8 | 1 | `<b` | -128 ~ 127 |
| u16 | 2 | `<H` | 0 ~ 65535 |
| i16 | 2 | `<h` | -32768 ~ 32767 |
| u32 | 4 | `<I` | 0 ~ 2^32-1 |
| i32 | 4 | `<i` | -2^31 ~ 2^31-1 |
| f32 | 4 | `<f` | IEEE-754 单精度 |
| char[N] | N | utf-8 定长补零 | 字符串 |

所有多字节字段统一小端字节序。

### 10.2 命令载荷字段定义语法

CSV 表中字段串格式:
```
{字段名:类型[=默认值]; 字段名:类型; ...}
```

- 多字段以 `;` 分隔
- `=默认值` 表示固定值 (如魔数), UI 不显示输入框
- 例: `{magic:u32=0xB00710AD}` / `{pos:f32; vel:f32; kp:f32}`

---

## 11. 参数索引表 (节选)

完整 83 条参数见 `resources/joint_motor_param_index.csv`。参数 ID 与分组:

| ID 范围 | 分组 | 备注 |
|---------|------|------|
| 0 ~ 1 | 实例标识 | motor_id, motor_name |
| 2 ~ 19 | 电机本体 | R, Ld, Lq, flux, kt, pole_pairs, rated_*, max_speed, ... |
| 20 ~ 23 | 减速器 | gear_ratio, gear_efficiency, ... |
| 24 ~ 31 | 编码器 | enc_lines, enc_direction, enc_offset, ... |
| 32 ~ 35 | 位置限位 | multiturn_enable, pos_min/max_limit, ... |
| 36 ~ 40 | 回零 | homing_method, homing_speed_*, ... |
| 41 ~ 52 | 电流环 | current_kp_d/q, current_ki_d/q, ... |
| 53 ~ 68 | 位置速度环 | speed_kp/ki, position_kp, notch_*, ... |
| 69 ~ 71 | 阻抗控制 | impedance_kp/kd, iq_max |
| 72 ~ 74 | 热模型 | thermal_resistance, ... |
| 75 ~ 82 | 保护 | protect_over_*, protect_enable_mask |

参数读应答 `type` 字段: 0=u8, 1=i8, 2=u16, 3=i16, 4=u32, 5=i32, 6=f32, 7=str

---

## 12. 故障码体系 (三套映射)

| 体系 | 范围 | 来源 | 用途 |
|------|------|------|------|
| CSV 设备故障码 | 0x1101 ~ 0xA302 (104 条) | `resources/故障码定义_故障信息表_表格.csv` | 设备级故障分类, 9 大系统 |
| 虚拟电机 Fault 位 | bit 0 ~ 10, 15 (12 位) | `twin_fsm.py:FaultDetector` | 固件级故障标志, `fault_mask` 字段 |
| JmErr 协议错误码 | 0x00 ~ 0x0A (11 条) | NACK 应答 | 协议层错误 |

### 12.1 虚拟电机 Fault 位

| bit | 值 | 名称 | 触发条件 | CSV 映射 |
|-----|----|------|----------|----------|
| 0 | 0x0001 | OVER_CURRENT | 相电流 > peak_current × 1.2 | 0x3102, 0x2104 |
| 1 | 0x0002 | OVER_VOLTAGE | vbus > rated_voltage × 1.1 | 0x2102 |
| 2 | 0x0004 | UNDER_VOLTAGE | vbus < rated_voltage × 0.5 | 0x2103 |
| 3 | 0x0008 | OVER_TEMP_FET | MOSFET 结温超阈值 | 0x3201, 0x3103 |
| 4 | 0x0010 | OVER_TEMP_MOTOR | 电机温度超阈值 | 0x4101, 0x4204 |
| 5 | 0x0020 | POS_LIMIT | 位置超限 (状态机迁移 SAFETY) | 0x1201, 0x1301 |
| 6 | 0x0040 | FOLLOW_ERROR | 跟随误差 > 阈值持续 0.2s | 0x8108 |
| 7 | 0x0080 | COMM_LOST | 通信超时 > 0.2s | 0x9102, 0x5105 |
| 8 | 0x0100 | PHASE_LOSS | 缺相 (预留) | 0x4104 |
| 9 | 0x0200 | SAFETY_LIMIT | 安全限位 (预留) | 0x1102 |
| 10 | 0x0400 | OVER_SPEED | 角速度 > over_speed | 0x4202 |
| 15 | 0x8000 | INJECTED | 注入故障 (演示用) | — |

### 12.2 CSV 故障码分组

9 大系统: 安全系统 / 电源系统 / 驱动器 / 电机 / 编码器 / 通信 / 控制器 / 机械传动 / 环境

3 个级别: 故障级 (立即停机) / 异常级 (降功率运行) / 警告级 (仅记录)

---

## 13. 波形显示上位机实现建议

### 13.1 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                       UI 层 (PyQt6 + pyqtgraph)            │
│  连接面板  │  通道选择  │  波形显示  │  暂停/跟随/导出  │
└────────────┴────────────┴────────────┴─────────────────────┘
                              │
                       FeedbackData 信号
                              │
┌─────────────────────────────────────────────────────────────┐
│                      客户端层 (JmClient)                    │
│  帧分发 / FeedbackData 解析 / 状态机同步 / 信号发射         │
└─────────────────────────────────────────────────────────────┘
                              │
                       (cmd, payload) 信号
                              │
┌─────────────────────────────────────────────────────────────┐
│                      传输层 (Transport)                     │
│  SerialTransport  │  CanTransport  │  LoopbackBridge        │
└─────────────────────────────────────────────────────────────┘
                              │
                       串口字节 / CAN 帧 / 内存回环
                              │
                       ┌──────┴──────┐
                       │   真实电机   │ 或 VirtualMotor (本机回环)
                       └─────────────┘
```

### 13.2 性能优化要点 (来自现有 plot_panel.py 实战经验)

1. **环形缓冲区**: 每条曲线用预分配 `numpy` 数组环形缓冲, `append` O(1) 无内存分配
2. **惰性时间顺序视图**: 缓存满后才做一次 fancy-index copy, 避免每帧拷贝
3. **脏标记跳过重绘**: 无新数据时直接 return, 不触发 `pyqtgraph` 重绘
4. **Y 轴自适应节流**: 每 ~300ms 计算一次 Y 范围 (而非每帧), 避免抖动
5. **时间戳增量更新**: 维护 `_latest_t` 增量推进, 不每帧扫全部 buffer
6. **鼠标悬停读数**: 复用缓存数组做时间插值, 不重复转换

### 13.3 推荐实现路径

```python
# 1. 创建虚拟电机 (无硬件调试)
from tools.waveform.protocol_reference import VirtualMotor, LoopbackBridge
vm = VirtualMotor(period_ms=10)
bridge = LoopbackBridge(vm)
bridge.start()

# 2. 启动遥测订阅 (位置+速度+力矩+DQ)
from tools.waveform.protocol_reference import FrameCodec, JmCmd, JmTlmBit, wr_u8, wr_u16
mask = JmTlmBit.POS_VEL | JmTlmBit.TORQUE | JmTlmBit.DQ
payload = wr_u8(1) + wr_u16(mask) + wr_u16(10)  # enable=1, mask, period=10ms
bridge.write(FrameCodec.pack(JmCmd.SET_TELEMETRY, payload))

# 3. 接收并解析遥测帧
from tools.waveform.protocol_reference import FrameDecoder, parse_telemetry
decoder = FrameDecoder()
while True:
    rx = bridge.read(4096)
    if not rx:
        continue
    for cmd, pl in decoder.feed(rx):
        if cmd == JmCmd.TELEMETRY:
            fb, filled = parse_telemetry(pl)
            # fb.pos, fb.vel, fb.torque, fb.id, fb.iq 已填充
            plot.append(fb)
```

### 13.4 波形显示必备功能清单

- [ ] 通道勾选 (mask → 自动订阅)
- [ ] 时间窗调节 (1s / 5s / 30s / 60s)
- [ ] 暂停 / 继续 / 跟随最新
- [ ] Y 轴自适应 / 手动锁定
- [ ] 鼠标悬停读数 (时间 + 数值)
- [ ] 多曲线分屏 / 同屏叠加
- [ ] 导出 CSV / PNG
- [ ] 触发捕获 (满足条件冻结一段历史)
- [ ] FFT 分析 (针对电流/振动)
- [ ] 状态机叠加显示 (在波形上标注 top_fsm 切换点)

### 13.5 推荐技术栈

| 组件 | 选型 | 备注 |
|------|------|------|
| GUI 框架 | PyQt6 | 与现有项目一致 |
| 波形渲染 | pyqtgraph | 性能优于 matplotlib, 支持 OpenGL 加速 |
| 数组 | numpy | 环形缓冲必备 |
| 串口 | pyserial | 跨平台 |
| CAN | python-can | 支持 PCAN / SocketCAN / Kvaser |
| 导出 | pandas + openpyxl | CSV / Excel |

---

## 14. 数据驱动 CSV 资源

| 文件 | 用途 | 字段 |
|------|------|------|
| `resources/joint_motor_command_list.csv` | 完整 103 条命令定义 | CMD / 名称 / 方向 / 载荷 / 应答 / 长度 / CAN_ID / 备注 |
| `resources/joint_motor_param_index.csv` | 83 条参数索引 | param_id / 参数名 / 中文 / 类型 / 字节数 / 单位 / 分组 / 读写 |
| `resources/motor_info.csv` | 电机配置参数 (扩展) | VariableName / NameZh / Category / Access / DataType / DefaultValue / Unit / Index |
| `resources/故障码定义_故障信息表_表格.csv` | 104 条 CSV 故障码 | 故障码 / 来源 / 级别 / 名称 / 触发条件 / 处理方式 |

CSV 解析逻辑见项目内 `jmproto/registry.py`, 本目录的 `protocol_reference.py` 提供了不依赖 CSV 的内置最小命令集。

---

## 15. 协议层文件映射 (项目内对应位置)

| 本文件章节 | 项目源文件 | 说明 |
|-----------|-----------|------|
| §2 帧格式 | `jmproto/frame.py` | `FrameCodec` / `FrameDecoder` |
| §3 CAN | `transport/can_transport.py` | 占位实现, 设计预留完整 |
| §4 CRC | `jmproto/crc16.py` | 与固件 `crc16.c` 一致 |
| §5 命令表 | `jmproto/cmd_def.py` + CSV | `JmCmd` / `JmErr` / `JmTlmBit` 枚举 |
| §6 编解码 | `jmproto/codec.py` | 小端助手 + 类型映射 |
| §7 遥测 | `jmproto/feedback.py:parse_telemetry` | 位序与固件 `jm_telemetry_bit_e` 一致 |
| §11 参数 | `jmproto/registry.py:ProtocolRegistry` | 加载 CSV, 提供 `pack_command` / `pack_param_value` |
| §12 故障 | `jmproto/fault_codes.py` | 三套故障体系映射 |
| 客户端层 | `core/motor_client.py` | `JmClient` 高层封装, Qt 信号分发 |
| 传输层 | `transport/serial_transport.py` | `SerialTransport` (pyserial + QThread) |
| 虚拟电机 | `transport/virtual_engine/` | 完整数字孪生实现 (含 FSM / 物理 / 负载) |
| 波形面板 | `ui/panels/plot_panel.py` | 现有实现, 可作为参考 |

---

## 16. 快速验证

```bash
cd w:/Items/JointMotor/SW/jointmotor/User/Tools/pyqt_gui
python -c "
from tools.waveform.protocol_reference import (
    FrameCodec, FrameDecoder, JmCmd, JmTlmBit, parse_telemetry,
    VirtualMotor, LoopbackBridge, wr_u8, wr_u16
)

# 1. CRC + 帧编解码自洽
frame = FrameCodec.pack(JmCmd.ENABLE, b'')
print(f'ENABLE 帧: {frame.hex()}')

# 2. 虚拟电机回环 + 遥测订阅
vm = VirtualMotor()
bridge = LoopbackBridge(vm)
bridge.start()

# 启动遥测
mask = JmTlmBit.POS_VEL | JmTlmBit.TORQUE
bridge.write(FrameCodec.pack(JmCmd.ENABLE))
bridge.write(FrameCodec.pack(JmCmd.SET_TELEMETRY, wr_u8(1) + wr_u16(mask) + wr_u16(10)))

# 收一拍
import time; time.sleep(0.05)
rx = bridge.read(4096)
dec = FrameDecoder()
for cmd, pl in dec.feed(rx):
    if cmd == JmCmd.TELEMETRY:
        fb, filled = parse_telemetry(pl)
        print(f'遥测: filled={filled}, pos={fb.pos:.3f}, vel={fb.vel:.3f}, torque={fb.torque:.3f}')
print('OK')
"
```

预期输出:
```
ENABLE 帧: a55a0001000400XX
遥测: filled=('pos', 'vel', 'torque'), pos=..., vel=..., torque=...
OK
```

---

## 附录 A: 字段类型归一映射

固件 C 类型 → 协议短类型名:

| C 类型 | 协议类型 |
|--------|----------|
| uint8 / uint8_t | u8 |
| int8 / int8_t | i8 |
| uint16 / uint16_t | u16 |
| int16 / int16_t | i16 |
| uint32 / uint32_t | u32 |
| int32 / int32_t | i32 |
| float / single | f32 |
| char[N] | char[N] |

## 附录 B: 防误触魔数

| 命令 | 魔数 | 含义 |
|------|------|------|
| ENTER_BOOTLOADER (0xB2) | `0xB00710AD` | "BOOTLOAD" 谐音 |
| FACTORY_RESET (0xB4) | `0xFAC70F5F` | "FACTORY" 缩写 |

魔数作为命令载荷的首字段, 下位机校验不通过则忽略命令, 防止误触发不可逆操作。
