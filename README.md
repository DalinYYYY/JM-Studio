# JM Studio — 关节电机上位机

基于 PyQt6 的关节电机调试与控制上位机，提供串口/CAN/虚拟孪生多种链路、数据驱动的命令与参数面板、实时反馈/状态机/曲线/故障信息显示，以及独立的数字孪生与波形采集工具。

- 协议层与传输层完全解耦，新增命令/参数无需改 Python 代码
- 自带数字孪生虚拟引擎，无硬件即可完整体验全部功能
- 主题、布局、缓存、遥测配置全部持久化

---

## 目录

- [功能特性](#功能特性)
- [目录结构](#目录结构)
- [环境依赖](#环境依赖)
- [快速开始](#快速开始)
- [运行附属工具](#运行附属工具)
- [架构设计](#架构设计)
  - [分层架构](#分层架构)
  - [协议层 jmproto](#协议层-jmproto)
  - [传输层 transport](#传输层-transport)
  - [核心层 core](#核心层-core)
  - [UI 层 ui](#ui-层-ui)
  - [虚拟引擎 transport.virtual_engine](#虚拟引擎-transportvirtual_engine)
- [通信协议](#通信协议)
  - [串口帧格式](#串口帧格式)
  - [命令空间](#命令空间)
  - [遥测订阅](#遥测订阅)
- [数据驱动机制](#数据驱动机制)
- [主题与持久化](#主题与持久化)
- [打包为 EXE](#打包为-exe)
- [测试](#测试)
- [常见问题](#常见问题)

---

## 功能特性

| 模块 | 能力 |
|------|------|
| 连接管理 | 串口自动枚举、波特率可选、虚拟孪生引擎切换、连接状态实时显示 |
| 系统控制 | 使能/失能/急停/清障/心跳/读设备信息 |
| 运动控制 | 数据驱动渲染 100 条命令（位置/速度/力矩/MIT/阻抗/轨迹同步/校准/诊断…），按 CSV 字段动态生成输入框 |
| 参数读写 | 数据驱动渲染 83 个参数（按分组、类型、单位展示），支持单读/单写/批量读/批量写、保存到 Flash/EEPROM |
| 实时反馈 | 位置/速度/力矩/温度/母线电压/三相电流/DQ 电流/故障掩码，含转子示意图 |
| 状态机 | 顶层状态（INIT/SAFETY/FAULT/IDLE/READY/RUN/CALIB/CONFIG/BOOTLOADER）+ 运行子状态可视化 |
| 遥测订阅 | 按 `JmTlmBit` 位掩码勾选通道（POS_VEL/DQ/PHASE/BUS/TEMP/MULTITURN/TORQUE/FAULT/STATE/DEBUG），可配周期 |
| 实时曲线 | pyqtgraph 多通道绘制，向量化批量喂入，支持布局编辑与导出 |
| 故障信息 | 故障掩码实时解码、上升沿记入历史、NACK 错误中文化、故障源分类 |
| 通信日志 | TX/RX 帧着色显示，可显隐、可配置 flush 周期与最大缓存 |
| 主题 | 深色/浅色双主题，切换即时生效并持久化 |
| 流量统计 | 状态栏实时显示 TX/RX 速率（B/s）与累计字节/帧数 |
| 布局编辑 | 实时反馈/状态机面板支持拖拽方框自定义布局并导出 JSON |

---

## 目录结构

```
pyqt_gui/
├── main.py                       # 主程序入口
├── requirements.txt              # 运行依赖
├── __init__.py
├── .gitignore
│
├── jmproto/                      # 协议层（传输无关）
│   ├── __init__.py
│   ├── crc16.py                  # CRC16-XMODEM（与固件一致）
│   ├── frame.py                  # 串口帧编解码 FrameCodec / FrameDecoder
│   ├── cmd_def.py                # JmCmd / JmErr / JmTlmBit / JmParamType / TopFsm / RunState 枚举
│   ├── codec.py                  # 小端编解码助手 + 按类型打包
│   ├── feedback.py               # FeedbackData + 各 READ_* 应答/遥测解析
│   ├── registry.py               # CSV 数据驱动注册表（命令表/参数表）
│   └── fault_codes.py            # 故障码定义与解码
│
├── transport/                    # 传输层（可插拔）
│   ├── __init__.py
│   ├── base.py                   # Transport 抽象基类
│   ├── serial_transport.py       # 串口实现（QThread + 单锁串行化）
│   ├── can_transport.py          # CAN 实现（占位，含 MIT 压缩/分包常量）
│   └── virtual_engine/           # 数字孪生虚拟引擎
│       ├── __init__.py
│       ├── twin_config.py        # 电机参数加载（对齐固件 motor_param_t）
│       ├── twin_physics.py       # 物理模型（dq 方程 + 机械方程 + 热模型 + 编码器）
│       ├── twin_load.py          # 关节负载模型（连杆惯量/重力矩/外力/碰撞）
│       ├── twin_control.py      # 控制算法复刻（PID + FOC + 级联控制）
│       ├── twin_fsm.py           # 状态机复刻（IDLE/READY/RUN/FAULT/SAFETY）
│       ├── digital_twin.py       # 多速率仿真引擎（电流环 10kHz / 位置环 2kHz）
│       ├── twin_responder.py     # 协议应答器（命令 <-> MotorCmd，遥测帧构造）
│       ├── virtual_transport.py  # Transport 实现（QTimer 驱动孪生 + 帧回环）
│       ├── motor_sim.py          # 旧版轻量仿真（后备）
│       ├── responder.py          # 旧版应答器（后备）
│       └── twin_control.py
│
├── core/                         # 核心层
│   ├── __init__.py
│   └── motor_client.py           # JmClient：传输无关的高层命令/分发/遥测
│
├── ui/                           # UI 层
│   ├── __init__.py
│   ├── main_window.py            # 主窗口：组装面板 + 连接信号
│   ├── theme.py                   # 全局主题（深/浅双色板 + 单例 + QSS）
│   ├── layout_store.py           # 用户设置持久化（ui_layout.json）
│   └── panels/                    # 各功能面板
│       ├── __init__.py
│       ├── _edit_mixin.py         # 布局编辑混入
│       ├── connection_panel.py    # 连接面板
│       ├── control_panel.py       # 系统控制
│       ├── motion_panel.py        # 运动控制（数据驱动）
│       ├── param_panel.py         # 参数表（数据驱动）
│       ├── feedback_panel.py     # 实时反馈
│       ├── telemetry_panel.py    # 遥测订阅
│       ├── plot_panel.py          # 实时曲线
│       ├── state_machine_panel.py # 状态机可视化
│       ├── fault_info_panel.py   # 故障信息
│       ├── twin_param_panel.py   # 孪生参数
│       └── log_panel.py           # 通信日志
│
├── resources/                    # 运行时资源
│   ├── pic/
│   │   └── log_ioc.png            # 应用图标
│   ├── joint_motor_command_list.csv  # 命令表（100 条）
│   ├── joint_motor_param_index.csv   # 参数表（83 项）
│   ├── motor_info.csv                # MotorInfo 配置表
│   ├── 故障码定义_故障信息表_表格.csv  # 故障码定义
│   ├── ui_layout.json               # 用户设置/布局持久化
│   └── MotorInfo_readme.md           # MotorInfo 参数区结构说明
│
├── tools/                        # 附属独立工具
│   ├── test/                      # 集成测试
│   │   ├── test_state_sync.py
│   │   ├── test_twin_engine.py
│   │   └── test_twin_stress.py
│   ├── twin/                      # 数字孪生独立上位机
│   │   ├── main.py / app.py
│   │   ├── control_panel.py
│   │   ├── motor_view_panel.py
│   │   ├── feedback_panel.py
│   │   ├── plot_panel.py
│   │   ├── log_panel.py
│   │   ├── fault_history_panel.py
│   │   ├── engine_bridge.py
│   │   ├── smoke_test.py
│   │   └── twin_layout.json
│   └── waveform/                  # 波形显示上位机
│       ├── main.py / app.py
│       ├── waveform_plot.py
│       ├── fft_panel.py
│       ├── trigger.py
│       ├── exporter.py
│       ├── transport.py
│       ├── protocol_reference.py
│       ├── protocol_reference.md
│       └── requirements.txt
│
├── packaging/                    # 打包脚本
│   ├── build_exe.bat             # 入口批处理
│   ├── build_exe.ps1             # PowerShell 构建脚本（PyInstaller）
│   └── requirements-build.txt    # 构建依赖
│
├── doc/                          # 设计文档
│   └── PyQt上位机框架重构方案.md
│
└── LOG/                           # 运行日志输出
    └── jm_log_YYYYMMDD_HHMMSS.txt
```

---

## 环境依赖

- **Python**：3.9+（建议 3.10/3.11）
- **操作系统**：Windows（串口实测稳定）；Linux/macOS 串口可用，UI 兼容
- **运行依赖**（见 [requirements.txt](requirements.txt)）：

  ```
  PyQt6>=6.5.0
  pyserial>=3.5
  pyqtgraph>=0.14.0
  numpy>=1.24
  ```

- **可选依赖**：
  - `python-can>=4.0.0`：启用 CAN 传输（当前 `can_transport.py` 为占位实现）
  - 波形工具见 [tools/waveform/requirements.txt](tools/waveform/requirements.txt)

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动主程序

```bash
python main.py
```

### 3. 连接电机

#### 真实串口

1. 在左侧「连接」面板选择串口（自动枚举）与波特率（默认 115200，最高 921600）
2. 点击「连接」
3. 状态栏显示 `● COMx @波特率` 即连接成功

#### 虚拟孪生引擎（无需硬件）

1. 在「连接」面板的串口下拉中选择 `VIRTUAL`（或类似项）
2. 点击「连接」
3. 所有数据由本地数字孪生引擎生成，可完整体验命令下发、参数读写、遥测、曲线、故障

### 4. 典型操作流程

1. **上使能**：左侧「系统控制」→ 点击 `ENABLE`，状态机进入 READY
2. **下发运动**：在「运动控制」面板选择命令（如 POSITION 位置环），按字段输入目标值，点「发送」
3. **订阅遥测**：在「遥测订阅」面板勾选所需通道（建议至少 POS_VEL/DQ/PHASE/STATE），设置周期（ms），点「应用」
4. **读参数**：切到「电机参数」选项卡，点行尾「读」按钮，或顶部「批量读」
5. **看曲线**：切到「实时曲线」选项卡，选择通道即可实时绘制
6. **下使能**：点击 `DISABLE`，状态机回到 IDLE

---

## 运行附属工具

### 数字孪生独立上位机

脱离协议层/串口/JmClient，直接驱动 `DigitalTwinEngine` 运行状态机，提供独立的参数给定、控制参数、保护参数与推导参数面板。

```bash
python -m tools.twin.main
# 或
python tools/twin/main.py
```

### 波形显示上位机

独立的波形采集与显示工具，支持 FFT、触发、导出。自带协议参考实现（零外部依赖）。

```bash
pip install -r tools/waveform/requirements.txt
python -m tools.waveform.main
# 或
python tools/waveform/main.py
```

协议细节参见 [tools/waveform/protocol_reference.md](tools/waveform/protocol_reference.md)。

---

## 架构设计

### 分层架构

```
┌─────────────────────────────────────────────────┐
│  UI 层 (ui/)                                     │
│  MainWindow + 各 Panel                            │
└───────────────┬─────────────────────────────────┘
                │ Qt 信号槽
┌───────────────▼─────────────────────────────────┐
│  核心层 (core/)                                   │
│  JmClient: 命令下发 + 应答分发 + 遥测合并          │
└───────────────┬─────────────────────────────────┘
                │ frame_received / send
┌───────────────▼─────────────────────────────────┐
│  传输层 (transport/)                              │
│  Transport 抽象基类                              │
│  ├─ SerialTransport   (pyserial + QThread)       │
│  ├─ CanTransport      (占位)                      │
│  └─ VirtualTransport  (数字孪生引擎)              │
└───────────────┬─────────────────────────────────┘
                │ (cmd, payload) 逻辑帧
┌───────────────▼─────────────────────────────────┐
│  协议层 (jmproto/)                                │
│  CRC / 帧编解码 / 命令枚举 / 编解码助手 /         │
│  FeedbackData 解析 / CSV 注册表 / 故障码          │
└─────────────────────────────────────────────────┘
```

**信号流（Qt 线程安全）**：

```
Transport(后台线程) --frame_received--> JmClient._on_frame(主线程槽)
   --> 分发为 feedback_updated / ack / nack / param_read_result / tx_log
   --> 各 panel 槽更新 UI / plot_panel.append(...)
```

发送方向：`panel --> JmClient.send_command --> Transport.send`（持 `_io_lock` 写）。

### 协议层 jmproto

传输无关的纯协议实现，可独立 import（不依赖串口/PyQt）。

| 模块 | 职责 |
|------|------|
| [crc16.py](jmproto/crc16.py) | CRC16-CCITT/XMODEM（多项式 0x1021，初值 0，MSB-first，无反转） |
| [frame.py](jmproto/frame.py) | 串口帧封包 `FrameCodec.pack` + 状态机解包 `FrameDecoder.feed` |
| [cmd_def.py](jmproto/cmd_def.py) | `JmCmd`(100 条) / `JmErr`(11 种) / `JmTlmBit`(10 位) / `JmParamType` / `TopFsm` / `RunState` 枚举 + 中文名映射 |
| [codec.py](jmproto/codec.py) | `rd_u8/u16/u32/i32/f32` / `wr_*` 小端助手 |
| [feedback.py](jmproto/feedback.py) | `FeedbackData` 数据类 + `parse_state` / `parse_read_reply` / `parse_telemetry` |
| [registry.py](jmproto/registry.py) | **核心新增**：CSV 加载 → `CommandSpec` / `ParamSpec` 表 + `pack_command` / `pack_param_value` |
| [fault_codes.py](jmproto/fault_codes.py) | 故障码加载与解码，`fault_mask_to_str` / `err_name_cn` |

### 传输层 transport

#### Transport 抽象（[base.py](transport/base.py)）

```python
class Transport(QObject):
    frame_received = pyqtSignal(int, bytes)   # (cmd, payload) 逻辑帧
    connected      = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)
    def open(self, **cfg) -> bool: ...
    def close(self): ...
    def is_open(self) -> bool: ...
    def send(self, cmd: int, payload: bytes) -> bool: ...
    def start(self): ...   # 启动后台线程
    def stop(self):  ...   # 程序退出清理
```

#### SerialTransport（[serial_transport.py](transport/serial_transport.py)）

关键约束（踩过的坑，勿改）：

- **单一 `_io_lock`** 串行化对 `self._ser` 的所有访问（read/write/close），否则主线程发送与工作线程接收并发操作同一句柄，在 Windows 上触发底层访问冲突导致段错误崩溃
- **`_alive`（线程生命周期）与 `_running`（串口打开）双标志分离**：线程在整个程序存活，串口未开时空转休眠，连接/断开无需重建线程
- 打开串口时**复位 `FrameDecoder`**，清除残留半帧
- CRC 为 CRC16-XMODEM，必须与固件一致

#### CanTransport（[can_transport.py](transport/can_transport.py)）

占位实现，`open()` 抛 `NotImplementedError`。已预留：

- 仲裁 ID：`ID = (CMD << 8) | motor_id`，`motor_id=0` 为广播
- 多帧分包：`data[0] = (末帧标志 << 7) | (序号 & 0x7F)`，每帧 ≤ 7 字节载荷
- MIT 定点压缩范围（pos16/vel12/kp12/kd12/tff12 → 8 字节）
- 反馈帧压缩（pos16/vel16/tq16/temp8/err8 → 8 字节）

### 核心层 core

#### JmClient（[motor_client.py](core/motor_client.py)）

与传输解耦的高层客户端，持有 `Transport`，订阅 `frame_received` 做业务分发：

- **高层信号**：`feedback_updated` / `state_updated` / `ack_received` / `nack_received` / `dev_info_received` / `dev_name_received` / `param_read_result` / `raw_frame` / `tx_frame`
- **命令方法**：基于 registry 的 `send_command(cmd, values)` + 便捷方法（`cmd_enable` / `query_feedback` / `set_telemetry` / `param_read` / `param_write` / `param_save` …）
- **反馈合并**：`_last_feedback` 按 `filled_fields` 增量合并各 `READ_*` / `TELEMETRY` 帧，避免部分帧字段覆盖为默认 0
- **传输切换**：`set_transport()` 可运行时切换串口 ↔ 虚拟引擎，UI 无感知

### UI 层 ui

#### MainWindow（[main_window.py](ui/main_window.py)）

只负责：创建 `JmClient`（注入 `SerialTransport`）、组装面板、连接信号槽、管理连接状态与显示刷新定时器。

- **左栏**（滚动）：连接 / 系统控制 / 运动控制 / 遥测订阅 / 设备信息 / 菜单配置
- **右栏**（选项卡 + 日志）：实时反馈 / 状态机 / 电机参数 / 电机配置 / 孪生参数 / 故障信息 / 实时曲线
- **状态栏**：连接状态 + TX/RX 速率与累计 + 帧数

#### 面板清单

| 面板 | 文件 | 说明 |
|------|------|------|
| ConnectionPanel | [connection_panel.py](ui/panels/connection_panel.py) | 串口枚举/波特率/连接按钮 |
| ControlPanel | [control_panel.py](ui/panels/control_panel.py) | 使能/失能/急停/清障 |
| MotionPanel | [motion_panel.py](ui/panels/motion_panel.py) | **数据驱动**：按 CSV 渲染命令载荷输入 |
| ParamPanel | [param_panel.py](ui/panels/param_panel.py) | **数据驱动**：按 CSV 渲染 83 参数表，支持批量读写 |
| FeedbackPanel | [feedback_panel.py](ui/panels/feedback_panel.py) | 实时反馈 + 转子示意图 |
| TelemetryPanel | [telemetry_panel.py](ui/panels/telemetry_panel.py) | 按 `JmTlmBit` 位勾选 + 周期配置 |
| PlotPanel | [plot_panel.py](ui/panels/plot_panel.py) | pyqtgraph 多通道曲线 |
| StateMachinePanel | [state_machine_panel.py](ui/panels/state_machine_panel.py) | 顶层状态 + 运行子状态可视化 |
| FaultInfoPanel | [fault_info_panel.py](ui/panels/fault_info_panel.py) | 故障掩码解码 + 历史 + NACK 错误 |
| TwinParamPanel | [twin_param_panel.py](ui/panels/twin_param_panel.py) | 孪生引擎参数给定 |
| LogPanel | [log_panel.py](ui/panels/log_panel.py) | 通信日志着色显示 |

#### 高频遥测与 UI 刷新

遥测帧率可能远高于 UI 刷新率（如 1ms 周期 vs 50ms 显示），主窗口采用：

- `_feedback_pending` 有界 deque 缓存（默认 2000 帧）
- `_ui_timer` 固定周期（默认 50ms）批量出队
- 超出单 tick 上限（1000 帧）的旧帧丢弃，每 2 秒告警一次
- 批量向量化喂曲线（`feed_feedback_batch`），避免逐帧 Python 调用开销

### 虚拟引擎 transport.virtual_engine

高保真数字孪生，模块化分层：

```
twin_config  ── 电机参数加载 (MotorParam, 对齐固件 motor_param_t)
twin_physics ── 物理模型 (dq电气方程 + 机械方程 + 热模型 + 编码器)
twin_load     ── 关节负载模型 (连杆惯量/重力矩/外力扰动/碰撞检测)
twin_control ── 控制算法复刻 (PID + FOC + 级联控制 + 无扰切换)
twin_fsm      ── 状态机复刻 (IDLE/READY/RUN/FAULT/SAFETY + 故障检测)
digital_twin  ── 多速率仿真引擎 (电流环10kHz / 位置环2kHz + QThread)
twin_responder── 协议应答器 (协议命令 <-> MotorCmd, 遥测帧构造)
virtual_transport ── Transport 实现 (QTimer 驱动孪生仿真 + 帧回环)
```

上层 `JmClient` / UI 无需感知差异：选择「虚拟孪生引擎」即用 `VirtualTransport`。

---

## 通信协议

### 串口帧格式

```
A5 5A + LEN(2B 大端) + HDR_CHK(1B) + CMD+Payload + CRC16(2B 小端)
```

- `LEN = CMD(1) + Payload(n)` 总字节数（大端）
- `HDR_CHK = (STX_H + STX_L + LEN_H + LEN_L) & 0xFF`
- `CRC16` 覆盖数据区（CMD+Payload），XMODEM 多项式 `0x1021`，初值 0，MSB-first，无反转
- 字节序：多字节小端（LE），浮点 IEEE-754 f32
- 与固件 `packer_parser + crc16(XMODEM)` 一致

### 命令空间

`0x00 ~ 0xFE`，按功能分 9 段（详见 [jmproto/cmd_def.py](jmproto/cmd_def.py)）：

| 段 | 范围 | 类别 |
|----|------|------|
| 1 | 0x00~0x0F | 系统控制（IDLE/HOLD/BRAKE/ESTOP/ENABLE/DISABLE/STOP） |
| 2 | 0x10~0x2F | 运动控制（OPEN_LOOP/CURRENT/TORQUE/MIT/VELOCITY/POSITION…） |
| 3 | 0x30~0x4F | 高级力控（IMPEDANCE/ADMITTANCE/FORCE_CONTROL/GRAVITY_COMP…） |
| 4 | 0x50~0x6F | 轨迹同步（PVT/CUBIC_SPLINE/TRAPEZOIDAL/S_CURVE/HOMING…） |
| 5 | 0x70~0x8F | 特殊应用与测试（STEP_DIR/JOG/TEST_*） |
| 6 | 0x90~0xAF | 校准（CALIB_MOTOR_PARAM/CALIB_ENCODER_OFFSET…） |
| 7 | 0xB0~0xBF | 系统诊断（CLEAR_FAULT/ENTER_BOOTLOADER/SAVE_CONFIG…） |
| 8 | 0xC0~0xCF | 反馈查询（READ_FEEDBACK/READ_STATE/TELEMETRY/SET_TELEMETRY…） |
| 9 | 0xD0~0xFF | 设备信息与参数读写（READ_DEV_INFO/PARAM_READ/PARAM_WRITE…） |

错误应答：`NACK (0xFE) + cmd + err_code`，`err_code` 见 `JmErr`（OK/UNSUPPORTED/OUT_OF_RANGE/STATE_DENY/BAD_PARAM_ID/CRC/LENGTH/READ_ONLY/FLASH/FAULT_STATE/CALIB_BUSY）。

### 遥测订阅

通过 `SET_TELEMETRY (0xCB)` 订阅周期推送，由 `TELEMETRY (0xCA)` 帧返回所需字段。单帧可携带多通道数据，比逐项轮询效率高 10 倍以上。

位掩码（`JmTlmBit`）：

| 位 | 字段 |
|----|------|
| 0 | POS_VEL 位置/速度 |
| 1 | DQ DQ 电流 |
| 2 | PHASE 三相电流 |
| 3 | BUS 母线 |
| 4 | TEMP 温度 |
| 5 | MULTITURN 多圈 |
| 6 | TORQUE 力矩 |
| 7 | FAULT 故障/警告 |
| 8 | STATE 状态机 |
| 9 | DEBUG 调试通道 |

> 实时反馈面板的三相电流依赖 DQ + PHASE 位，启动时会强制补全这两位，否则遥测帧不含 ia/ib/ic，显示恒为 0。

---

## 数据驱动机制

**核心**：协议表以 CSV 形式随工具携带，运行时由 [jmproto/registry.py](jmproto/registry.py) 加载为结构化规格，UI 按规格动态渲染。

### 命令表（resources/joint_motor_command_list.csv）

100 条命令，列：序号/命令名称/CMD(hex)/方向/功能类别/串口请求载荷/串口应答载荷/数据长度/编码格式/单位范围/CAN_ID/CAN数据区/备注。

- 请求载荷字段语法：`{pos:f32;vel:f32}` → `[(pos, f32), (vel, f32)]`
- 固定值字段：`magic:u32=0xB00710AD` → UI 不显示输入框，自动填入
- MotionPanel 按类别（运动控制/高级力控…）填充下拉，选中后按 `fields` 动态生成对应输入框（f32 → DoubleSpinBox，u8/u32 → SpinBox）

### 参数表（resources/joint_motor_param_index.csv）

83 项参数，列：param_id/参数名/中文含义/数据类型/字节数/单位/所属分组/读写。

- ParamPanel 用 `QTableWidget` 按 `group` 分组列出（电机本体/电流环/速度环/位置环/保护…）
- 每行：中文名 / param_id / 类型·单位 / 当前值 / 读·写按钮
- 读写走 `PARAM_READ(0xE0)` / `PARAM_WRITE(0xE1)` + `pack_param_value` / `unpack_param_value`
- 支持批量读（队列 + 应答驱动）与批量写

### 健壮性

CSV 缺失或解析失败时回退到内置最小命令集（ENABLE/DISABLE/POSITION/VELOCITY/TORQUE 等），并在日志告警——保证无 CSV 也能跑。

---

## 主题与持久化

### 主题（[ui/theme.py](ui/theme.py)）

- 单例 `theme`，深色/浅色双色板，全局 QSS 生成
- 三类消费者：常规组件（`theme.qss()` 一次性铺底）/ 自绘图（`theme.c(key)`）/ 曲线（`theme.hex(key)`）
- 切换：`theme.set('light'/'dark')` → 持久化 → emit `changed` → 订阅者重铺/重绘

### 用户设置（[ui/layout_store.py](ui/layout_store.py)）

持久化到 [resources/ui_layout.json](resources/ui_layout.json)，包含：

- 波特率、显示刷新周期、最大缓存帧数
- 主窗口尺寸/位置、右侧 splitter 尺寸
- 遥测掩码与周期、反馈轮询配置
- 日志显隐与选项
- 各面板配置（plot/motion/motor_param/motor_config/twin_param/fault_info）
- 上次主题

退出时自动写入，启动时自动恢复。

---

## 打包为 EXE

使用 PyInstaller 打包为 Windows 可执行文件。

### 一键打包

```powershell
# 进入 packaging 目录执行（或在工程根目录）
packaging\build_exe.bat
```

### 进阶参数

```powershell
# 指定 Python 解释器
packaging\build_exe.bat -Python "C:\path\to\python.exe"

# 单文件模式
packaging\build_exe.bat -OneFile

# 跳过依赖安装（已装好）
packaging\build_exe.bat -SkipInstall

# 清理后重建
packaging\build_exe.bat -Clean

# 自定义输出名
packaging\build_exe.bat -Name "MyJointMotor"
```

### 输出

- onedir 模式：`dist\JointMotorController\JointMotorController.exe`
- onefile 模式：`dist\JointMotorController.exe`

打包脚本会自动收集 `resources` 目录、PyQt6、pyqtgraph、serial、`transport.virtual_engine` 子模块。

---

## 测试

### 协议层单元自测（无需硬件/PyQt）

```bash
python -c "import jmproto; print('OK')"
```

关键断言（可在 [tools/test/](tools/test/) 中扩展）：

- CRC 表前 4 项：`0x0000, 0x1021, 0x2042, 0x3063`，表[255]=`0x1ef0`
- `pack_command(0x16, {pos:1, vel:2})` 得 8 字节
- `pack_command(0x15, {pos:1.57})` 与规范一致
- 22B `READ_FEEDBACK` 帧 + 一帧 `TELEMETRY`(mask 含 POS_VEL|STATE) 喂入解析，字段正确

### 集成测试

```bash
python tools/test/test_state_sync.py
python tools/test/test_twin_engine.py
python tools/test/test_twin_stress.py
```

### 冒烟测试（数字孪生）

```bash
python tools/twin/smoke_test.py
```

### GUI 冒烟（本地执行）

1. 不连接即启动不报错
2. 连接虚拟孪生引擎后默认下发 `SET_TELEMETRY`，反馈面板刷新
3. MotionPanel 切换命令时输入框随 CSV 动态变化
4. ParamPanel 能读回某参数
5. 断开/重连不崩溃（验证 `_io_lock` + 线程生命周期）

---

## 常见问题

### Q1：连接串口后程序崩溃（段错误）

**A**：确认使用的是当前代码的 [serial_transport.py](transport/serial_transport.py)，且 `_io_lock` 单锁串行化未被破坏。早期版本主线程发送与工作线程接收并发操作同一句柄会触发 Windows 底层访问冲突。

### Q2：CRC 校验失败，固件 NACK

**A**：CRC 必须是 **CRC-16/XMODEM**（多项式 `0x1021`，初值 0，MSB-first，无反转，无异或）。见 [jmproto/crc16.py](jmproto/crc16.py)。

### Q3：三相电流显示恒为 0

**A**：遥测掩码必须包含 `DQ` + `PHASE` 位。主窗口启动恢复设置时会强制补全这两位，但若手动构造掩码请确保包含。

### Q4：CSV 缺失还能用吗

**A**：能。[registry.py](jmproto/registry.py) 在 CSV 缺失或解析异常时回退到内置最小命令集，并在日志面板告警。但完整功能需 `resources/*.csv` 就位。

### Q5：CAN 传输何时可用

**A**：[can_transport.py](transport/can_transport.py) 当前为占位，`open()` 抛 `NotImplementedError`。已预留 MIT 定点压缩、多帧分包、ID 拆装等常量，实现时安装 `python-can>=4.0` 并按文件头注释照搬固件 `jm_proto_can.h` 即可。

### Q6：高频遥测导致 UI 卡顿

**A**：通过「菜单配置 → 缓存设置」调整显示更新周期（默认 50ms）与最大缓存帧数（默认 2000）。超出缓存的旧帧会被丢弃，每 2 秒告警一次。

### Q7：如何恢复默认布局

**A**：删除 [resources/ui_layout.json](resources/ui_layout.json) 中的 `settings` 节，或整个文件，重启程序即恢复默认。

---

## 相关文档

- [doc/PyQt上位机框架重构方案.md](doc/PyQt上位机框架重构方案.md) — 重构背景与设计决策
- [resources/MotorInfo_readme.md](resources/MotorInfo_readme.md) — MotorInfo 1024B 参数区结构定义
- [tools/waveform/protocol_reference.md](tools/waveform/protocol_reference.md) — 协议参考手册（独立实现上位机用）

---

## 版本

- 应用版本：1.0.0
- 协议版本：与固件 `User/Protocol/joint_proto/jm_cmd_def.h` 一致
