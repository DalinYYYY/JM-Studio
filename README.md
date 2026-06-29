# JM Studio — 关节电机调试上位机

基于 PyQt6 的关节电机（Joint Motor）调试与控制上位机。面向电机/伺服固件开发者、算法工程师与测试人员，提供从协议层到 UI 层的完整工具链，支持串口、CAN（占位）与本地数字孪生三种链路，覆盖命令下发、参数读写、实时反馈、状态机可视化、曲线绘制、故障诊断、波形采集等全流程。

> 应用名：**JM Studio** · 版本：`1.0.0` · 协议版本：与固件 `User/Protocol/joint_proto/jm_cmd_def.h` 一致

## Key Features

- **协议/传输解耦**：协议层（`jmproto`）零硬件依赖，可独立 import 做单元测试；传输层（`transport`）抽象出 `Transport` 基类，串口/CAN/虚拟引擎可插拔
- **数据驱动 UI**：100 条命令 + 83 个参数由 CSV 驱动渲染，新增协议项无需改 Python 代码
- **数字孪生引擎**：高保真物理仿真（dq 电气方程 + 机械方程 + 热模型 + 关节负载 + FOC 控制器 + 状态机），无硬件即可完整体验全部功能
- **遥测订阅**：按位掩码订阅 10 个通道（位置/速度/DQ/三相/母线/温度/多圈/力矩/故障/状态），单帧多通道，比逐项轮询效率高 10 倍以上
- **完整诊断**：实时反馈 + 状态机 + 故障历史 + NACK 中文解码 + 故障源分类
- **主题与持久化**：深/浅双主题、窗口布局、缓存配置、遥测掩码全部持久化，重启即恢复
- **附属工具**：独立数字孪生上位机、独立波形采集上位机（带 FFT、触发、导出）
- **一键打包**：PyInstaller 脚本封装为 Windows 可执行文件

---

## 目录

- [Tech Stack](#tech-stack)
- [Prerequisites](#prerequisites)
- [Getting Started](#getting-started)
- [Running the Companion Tools](#running-the-companion-tools)
- [Architecture](#architecture)
  - [Directory Structure](#directory-structure)
  - [Layered Architecture](#layered-architecture)
  - [Request / Data Flow Lifecycle](#request--data-flow-lifecycle)
  - [Protocol Layer (jmproto)](#protocol-layer-jmproto)
  - [Transport Layer (transport)](#transport-layer-transport)
  - [Core Layer (core)](#core-layer-core)
  - [UI Layer (ui)](#ui-layer-ui)
  - [Digital Twin Engine](#digital-twin-engine)
- [Communication Protocol](#communication-protocol)
- [Data-Driven Mechanism](#data-driven-mechanism)
- [Configuration & Persistence](#configuration--persistence)
- [Available Scripts](#available-scripts)
- [Testing](#testing)
- [Deployment (EXE Packaging)](#deployment-exe-packaging)
- [Troubleshooting](#troubleshooting)
- [Related Documentation](#related-documentation)

---

## Tech Stack

| 类别 | 选型 |
|------|------|
| 语言 | Python 3.9+（建议 3.10/3.11） |
| GUI 框架 | PyQt6 ≥ 6.5.0 |
| 绘图 | pyqtgraph ≥ 0.14.0 |
| 数值计算 | numpy ≥ 1.24 |
| 串口 | pyserial ≥ 3.5 |
| CAN（可选） | python-can ≥ 4.0.0（当前为占位实现） |
| 打包 | PyInstaller ≥ 6.0 |
| 操作系统 | Windows（串口实测稳定）；Linux/macOS 可用 |
| 并发模型 | QThread + 单锁串行化（串口收发） |

---

## Prerequisites

开始前请确认本机已安装：

- **Python 3.9+**（推荐通过 [miniconda](https://docs.conda.io/en/latest/miniconda.html) 或 [官方安装包](https://www.python.org/downloads/) 安装）
  ```bash
  python --version    # 应输出 3.9 或更高
  ```

- **pip**（随 Python 一同安装）

- **串口驱动**（如使用 USB 转 TTL）：CH340/CP2102 等芯片的对应驱动

- **Git**（克隆仓库所需）

- **真实电机硬件**（可选）：固件需实现与 `jm_cmd_def.h` 一致的协议；否则可使用内置数字孪生引擎

---

## Getting Started

### 1. 克隆仓库

```bash
git clone git@gitee.com:DD-1024/jointmotorstudio.git
cd jointmotorstudio
```

### 2. （推荐）创建虚拟环境

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate
```

或使用 conda：

```bash
conda create -n jm python=3.11 -y
conda activate jm
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

`requirements.txt` 内容：

```
PyQt6>=6.5.0
pyserial>=3.5
pyqtgraph>=0.14.0
numpy>=1.24
# 可选: CAN 传输(can_transport 实现时启用)
# python-can>=4.0.0
```

> 若默认源慢，可加镜像：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`

### 4. 环境变量

本工程为桌面 GUI 应用，**无强制环境变量**。所有用户偏好持久化在 `resources/ui_layout.json`（详见 [Configuration & Persistence](#configuration--persistence)）。

### 5. 启动主程序

```bash
python main.py
```

首次启动后，状态栏显示 `● 未连接`，日志面板会打印协议表加载情况：

```
协议表加载: 命令 100 条, 参数 83 个
```

若看到上述输出，说明 CSV 加载成功，环境就绪。

### 6. 连接电机

#### 真实串口连接

1. 在左侧「连接」面板的串口下拉框中选择目标串口（自动枚举本机串口）
2. 选择波特率（默认 115200，最高支持 921600）
3. 点击「连接」按钮
4. 状态栏显示 `● COMx @波特率` 即连接成功

#### 虚拟孪生引擎连接（无需硬件）

1. 在串口下拉框中选择 `VIRTUAL`
2. 点击「连接」
3. 日志面板打印 `[SIM] 虚拟数据引擎已启动, 所有数据由本地仿真生成`
4. 所有命令下发、参数读写、遥测、曲线、故障功能均由本地数字孪生引擎响应

### 7. 典型操作流程

```
┌─────────────────────────────────────────────────────────────┐
│ 1. 上使能    系统控制 → ENABLE      状态机: IDLE → READY     │
│ 2. 下发运动  运动控制 → 选命令 → 填值 → 发送                  │
│ 3. 订阅遥测  遥测订阅 → 勾通道 → 设周期 → 应用               │
│ 4. 读参数    电机参数 → 行尾「读」或顶部「批量读」           │
│ 5. 看曲线    实时曲线 → 选通道                                 │
│ 6. 下使能    系统控制 → DISABLE     状态机: READY → IDLE     │
└─────────────────────────────────────────────────────────────┘
```

---

## Running the Companion Tools

本工程附带两个独立的子工具，分别用于无协议层的孪生调试与高频波形采集。

### 数字孪生独立上位机（tools/twin）

脱离协议层/串口/JmClient，直接驱动 `DigitalTwinEngine` 运行状态机，提供独立的参数给定、控制参数、保护参数与推导参数面板。

```bash
python -m tools.twin.main
# 或
python tools/twin/main.py
```

冒烟测试：

```bash
python tools/twin/smoke_test.py
```

### 波形显示上位机（tools/waveform）

独立的波形采集与显示工具，支持 FFT、触发、导出。自带协议参考实现（零外部依赖，可直接复用）。

```bash
pip install -r tools/waveform/requirements.txt
python -m tools.waveform.main
# 或
python tools/waveform/main.py
```

协议细节参见 [tools/waveform/protocol_reference.md](tools/waveform/protocol_reference.md)。

---

## Architecture

### Directory Structure

```
pyqt_gui/
├── main.py                          # 主程序入口
├── requirements.txt                 # 运行依赖
├── __init__.py
├── .gitignore
│
├── jmproto/                         # 协议层（传输无关，纯 Python）
│   ├── __init__.py                  #   统一导出
│   ├── crc16.py                     #   CRC16-XMODEM（与固件一致）
│   ├── frame.py                     #   串口帧编解码 FrameCodec / FrameDecoder
│   ├── cmd_def.py                   #   JmCmd/JmErr/JmTlmBit/JmParamType/TopFsm/RunState 枚举
│   ├── codec.py                     #   小端编解码助手 + 按类型打包
│   ├── feedback.py                  #   FeedbackData + 各 READ_*/TELEMETRY 解析
│   ├── registry.py                  #   ★ CSV 数据驱动注册表（命令表/参数表）
│   └── fault_codes.py               #   故障码定义与解码
│
├── transport/                       # 传输层（可插拔）
│   ├── __init__.py
│   ├── base.py                      #   Transport 抽象基类
│   ├── serial_transport.py          #   串口实现（QThread + 单锁串行化）
│   ├── can_transport.py             #   CAN 实现（占位，含 MIT 压缩/分包常量）
│   └── virtual_engine/             #   数字孪生虚拟引擎
│       ├── __init__.py
│       ├── twin_config.py           #     电机参数加载（对齐固件 motor_param_t）
│       ├── twin_physics.py           #     物理模型（dq 方程 + 机械方程 + 热模型 + 编码器）
│       ├── twin_load.py             #     关节负载模型（连杆惯量/重力矩/外力/碰撞）
│       ├── twin_control.py          #     控制算法复刻（PID + FOC + 级联控制）
│       ├── twin_fsm.py              #     状态机复刻（IDLE/READY/RUN/FAULT/SAFETY）
│       ├── digital_twin.py          #     多速率仿真引擎（电流环 10kHz / 位置环 2kHz）
│       ├── twin_responder.py        #     协议应答器（命令 <-> MotorCmd，遥测帧构造）
│       ├── virtual_transport.py     #     Transport 实现（QTimer 驱动孪生 + 帧回环）
│       ├── motor_sim.py             #     旧版轻量仿真（后备）
│       └── responder.py             #     旧版应答器（后备）
│
├── core/                            # 核心层
│   ├── __init__.py
│   └── motor_client.py              #   JmClient：传输无关的高层命令/分发/遥测
│
├── ui/                              # UI 层
│   ├── __init__.py
│   ├── main_window.py               #   主窗口：组装面板 + 连接信号
│   ├── theme.py                     #   全局主题（深/浅双色板 + 单例 + QSS）
│   ├── layout_store.py              #   用户设置持久化（ui_layout.json）
│   └── panels/                      #   各功能面板
│       ├── __init__.py
│       ├── _edit_mixin.py            #    布局编辑混入
│       ├── connection_panel.py       #    连接面板
│       ├── control_panel.py          #    系统控制
│       ├── motion_panel.py           #    运动控制（数据驱动）
│       ├── param_panel.py            #    参数表（数据驱动）
│       ├── feedback_panel.py         #    实时反馈 + 转子示意图
│       ├── telemetry_panel.py       #    遥测订阅
│       ├── plot_panel.py             #    实时曲线（pyqtgraph）
│       ├── state_machine_panel.py    #    状态机可视化
│       ├── fault_info_panel.py       #    故障信息
│       ├── twin_param_panel.py       #    孪生参数给定
│       └── log_panel.py              #    通信日志
│
├── resources/                       # 运行时资源（随工具携带）
│   ├── pic/
│   │   └── log_ioc.png               #   应用图标
│   ├── joint_motor_command_list.csv  #   命令表（100 条）
│   ├── joint_motor_param_index.csv   #   参数表（83 项）
│   ├── motor_info.csv                #   MotorInfo 配置表
│   ├── 故障码定义_故障信息表_表格.csv  #   故障码定义
│   ├── ui_layout.json                #   用户设置/布局持久化
│   └── MotorInfo_readme.md           #   MotorInfo 1024B 参数区结构说明
│
├── tools/                           # 附属独立工具
│   ├── test/                        #   集成测试
│   │   ├── test_state_sync.py
│   │   ├── test_twin_engine.py
│   │   └── test_twin_stress.py
│   ├── twin/                        #   数字孪生独立上位机
│   └── waveform/                    #   波形显示上位机
│
├── packaging/                       # 打包脚本
│   ├── build_exe.bat                #   入口批处理
│   ├── build_exe.ps1                #   PowerShell 构建脚本（PyInstaller）
│   └── requirements-build.txt       #   构建依赖（pyinstaller）
│
├── doc/                             # 设计文档
│   └── PyQt上位机框架重构方案.md
│
└── LOG/                             # 运行日志输出
    └── jm_log_YYYYMMDD_HHMMSS.txt
```

### Layered Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  UI 层  (ui/)                                                 │
│  MainWindow + 各 Panel                                        │
│  只依赖 JmClient 的高层信号, 不感知 Transport                  │
└─────────────────────────┬────────────────────────────────────┘
                          │ Qt 信号槽 (feedback_updated / ack / nack ...)
┌─────────────────────────▼────────────────────────────────────┐
│  核心层  (core/)                                              │
│  JmClient: 命令下发 + 应答分发 + 遥测合并                      │
│  持有一个 Transport, 订阅其 frame_received 做业务分发           │
└─────────────────────────┬────────────────────────────────────┘
                          │ frame_received / send
┌─────────────────────────▼────────────────────────────────────┐
│  传输层  (transport/)                                         │
│  Transport 抽象基类                                           │
│  ├─ SerialTransport   (pyserial + QThread + _io_lock 单锁)    │
│  ├─ CanTransport      (占位, NotImplementedError)             │
│  └─ VirtualTransport  (数字孪生引擎, QTimer 驱动)             │
│  职责: 物理链路收发字节/帧 -> 组帧拆帧 -> 逻辑帧 (cmd, payload)│
└─────────────────────────┬────────────────────────────────────┘
                          │ (cmd, payload) 逻辑帧
┌─────────────────────────▼────────────────────────────────────┐
│  协议层  (jmproto/)                                           │
│  CRC / 帧编解码 / 命令枚举 / 编解码助手 /                       │
│  FeedbackData 解析 / CSV 注册表 / 故障码                       │
│  纯 Python, 无 Qt/串口依赖, 可独立 import                     │
└──────────────────────────────────────────────────────────────┘
```

### Request / Data Flow Lifecycle

**接收方向（下位机 → UI）**：

```
下位机/孪生引擎
   │ 原始字节流
   ▼
Transport._run() [后台线程]
   │ FrameDecoder.feed(raw) 状态机拆帧
   │ CRC16-XMODEM 校验
   ▼
Transport.frame_received.emit(cmd, payload)  [后台线程]
   │ Qt 跨线程信号槽投递
   ▼
JmClient._on_frame(cmd, payload)  [主线程槽]
   │ 按 cmd 分发:
   │   ACK 类(0x00~0xB8)         → ack_received / nack_received
   │   READ_FEEDBACK (0xC0, 22B) → FeedbackData + _merge_and_emit_feedback
   │   READ_STATE (0xC1)         → state_updated(top,run,ctrl,enable)
   │   READ_PHASE_CURRENT~FAULT  → parse_read_reply → 合并反馈
   │   TELEMETRY (0xCA)          → parse_telemetry → 合并反馈
   │   READ_DEV_INFO (0xD0)      → dev_info_received(hw, fw, uid)
   │   PARAM_READ (0xE0)         → param_read_result(id, type, bytes)
   ▼
MainWindow._queue_feedback / _on_ack / _on_param_result ...
   │ 有界 deque 缓存 + 50ms 定时器批量出队
   ▼
各 Panel 槽函数更新 UI / PlotPanel.feed_feedback_batch(...)
```

**发送方向（UI → 下位机）**：

```
Panel 用户操作
   │ emit signal (send_command / read_param / write_param ...)
   ▼
MainWindow._on_motion_command / _on_param_read ...
   │ 检查 is_open + ensure_open
   ▼
JmClient.send_command(cmd, values)
   │ registry.pack_command(cmd, values) 按字段类型小端打包
   ▼
JmClient._send(cmd, payload)
   │ tx_frame.emit(cmd, payload)  [日志面板]
   ▼
Transport.send(cmd, payload)
   │ 持 _io_lock
   │ FrameCodec.pack(cmd, payload) 封帧
   │ serial.Serial.write(...)
   ▼
下位机
```

**关键约束（Qt 线程安全）**：

- 串口收发在 `SerialTransport._run` 后台线程中循环读取，通过 `frame_received` 信号跨线程投递到主线程槽
- `Transport.send` 由主线程调用，与后台线程的 `read` 通过 `_io_lock` 互斥
- `JmClient._last_feedback` 是被持续修改的同一对象，UI 侧只读不写，避免拷贝开销

### Protocol Layer (jmproto)

传输无关的纯协议实现，可独立 import（不依赖串口/PyQt）。

| 模块 | 职责 |
|------|------|
| [crc16.py](jmproto/crc16.py) | CRC16-CCITT/XMODEM（多项式 `0x1021`，初值 0，MSB-first，无反转，无异或） |
| [frame.py](jmproto/frame.py) | 串口帧封包 `FrameCodec.pack` + 状态机解包 `FrameDecoder.feed` |
| [cmd_def.py](jmproto/cmd_def.py) | `JmCmd`(100 条) / `JmErr`(11 种) / `JmTlmBit`(10 位) / `JmParamType` / `TopFsm` / `RunState` 枚举 + 中文名映射 |
| [codec.py](jmproto/codec.py) | `rd_u8/u16/u32/i32/f32` / `wr_*` 小端助手 + `TYPE_INFO` 类型表 |
| [feedback.py](jmproto/feedback.py) | `FeedbackData` 数据类 + `parse_state` / `parse_read_reply` / `parse_telemetry` |
| [registry.py](jmproto/registry.py) | **核心**：CSV 加载 → `CommandSpec` / `ParamSpec` 表 + `pack_command` / `pack_param_value` |
| [fault_codes.py](jmproto/fault_codes.py) | 故障码加载与解码，`fault_mask_to_str` / `err_name_cn` |

#### FeedbackData 字段表

| 字段 | 单位 | 来源 |
|------|------|------|
| `pos` | rad | READ_FEEDBACK / TELEMETRY(POS_VEL) |
| `vel` | rad/s | READ_FEEDBACK / TELEMETRY(POS_VEL) |
| `torque` | Nm | READ_FEEDBACK / TELEMETRY(TORQUE) |
| `id`/`iq` | A | READ_DQ_CURRENT / TELEMETRY(DQ) |
| `ia`/`ib`/`ic` | A | READ_PHASE_CURRENT / TELEMETRY(PHASE) |
| `vbus` | V | READ_FEEDBACK / TELEMETRY(BUS) |
| `ibus` | A | TELEMETRY(BUS) |
| `power` | W | TELEMETRY(BUS) |
| `temp_fet`/`temp_motor` | ℃ | READ_TEMPERATURE / TELEMETRY(TEMP) |
| `multiturn` | - | READ_MULTITURN / TELEMETRY(MULTITURN) |
| `single` | rad | TELEMETRY(MULTITURN) |
| `fault_mask`/`warn_mask` | bitmask | READ_FAULT / TELEMETRY(FAULT) |
| `top_fsm`/`run_state`/`ctrl_mode`/`enable` | enum | READ_STATE / TELEMETRY(STATE) |
| `motion_state` | 0~4 | TELEMETRY(STATE) |
| `debug` | f32 元组 | TELEMETRY(DEBUG) |

### Transport Layer (transport)

#### Transport 抽象基类（[base.py](transport/base.py)）

```python
class Transport(QObject):
    frame_received = pyqtSignal(int, bytes)   # (cmd, payload) 逻辑帧
    connected      = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)

    tx_bytes = rx_bytes = tx_frames = rx_frames = 0   # 流量统计

    def open(self, **cfg) -> bool: ...    # 串口: port/baudrate; CAN: channel/bitrate/motor_id
    def close(self): ...
    def is_open(self) -> bool: ...
    def send(self, cmd: int, payload: bytes = b'') -> bool: ...
    def start(self): ...   # 启动后台线程
    def stop(self):  ...   # 程序退出清理
```

#### SerialTransport（[serial_transport.py](transport/serial_transport.py)）

关键约束（踩过的坑，勿改）：

- **单一 `_io_lock`** 串行化对 `self._ser` 的所有访问（read/write/close）。否则主线程发送与工作线程接收并发操作同一句柄，在 Windows 上触发底层访问冲突导致进程段错误崩溃
- **`_alive`（线程生命周期）与 `_running`（串口打开）双标志分离**：线程在整个程序存活，串口未开时空转休眠，连接/断开无需重建线程
- 打开串口时**复位 `FrameDecoder`**，清除残留半帧
- CRC 必须为 CRC16-XMODEM，与固件一致
- 超时配置：读 1ms、闲时 0.5ms、关闭等待 10ms、写 20ms

#### CanTransport（[can_transport.py](transport/can_transport.py)）

占位实现，`open()` 抛 `NotImplementedError`。已预留：

- 仲裁 ID：`ID = (CMD << 8) | motor_id`，`motor_id=0` 为广播
- 多帧分包：`data[0] = (末帧标志 << 7) | (序号 & 0x7F)`，每帧 ≤ 7 字节载荷
- MIT 定点压缩范围：
  - pos16: [-12.5, 12.5] rad
  - vel12: [-65, 65] rad/s
  - kp12: [0, 500]
  - kd12: [0, 5]
  - tff12: [-50, 50] Nm
- 反馈帧压缩：pos16/vel16/tq16/temp8/err8 → 8 字节，temp 线性映射 [-40, 215] ℃
- 常量：`CAN_SEG_LAST=0x80` / `CAN_SEG_SEQ_MASK=0x7F` / `CAN_SEG_PAYLOAD=7` / `CAN_SINGLE_MAX=8`

实现时安装 `python-can>=4.0` 并按文件头注释照搬固件 `jm_proto_can.h` 即可。

### Core Layer (core)

#### JmClient（[motor_client.py](core/motor_client.py)）

与传输解耦的高层客户端，持有 `Transport`，订阅 `frame_received` 做业务分发。

**高层信号**：

| 信号 | 参数 | 触发 |
|------|------|------|
| `feedback_updated` | `FeedbackData` | READ_FEEDBACK / TELEMETRY / 各 READ_* 合并后 |
| `state_updated` | `(top, run, ctrl, enable)` | READ_STATE / TELEMETRY(STATE) |
| `ack_received` | `cmd` | ACK 类应答（0x00~0xB8, status=OK） |
| `nack_received` | `(cmd, err)` | NACK / ACK status≠OK |
| `dev_info_received` | `(hw, fw, uid)` | READ_DEV_INFO (20B) |
| `dev_name_received` | `name:str` | READ_DEV_NAME |
| `param_read_result` | `(id, type, bytes)` | PARAM_READ 应答 |
| `raw_frame` / `tx_frame` | `(cmd, payload)` | 日志面板入口 |

**命令方法**：

- 数据驱动：`send_command(cmd, values)` 按 registry 打包
- 便捷封装：`cmd_enable/disable/stop/idle/hold/brake/estop/clear_fault`
- 反馈查询：`query_feedback/state/pos_vel/bus/temperature/fault/dq_current/phase_current/multiturn`
- 设备信息：`query_dev_info/dev_name`、`heartbeat`
- 参数：`param_read(id)` / `param_write(id, bytes)` / `param_save` / `param_reset`
- 遥测：`set_telemetry(enable, mask, period_ms)` → 载荷 `<BHH`

**反馈合并**：`_merge_and_emit_feedback` 按 `filled_fields` 增量合并到 `_last_feedback`，避免部分帧字段覆盖为默认 0（如 READ_PHASE_CURRENT 帧的 pos=0 会覆盖曲线）。

**传输切换**：`set_transport()` 可运行时切换串口 ↔ 虚拟引擎，UI 信号连接无感知。

### UI Layer (ui)

#### MainWindow（[main_window.py](ui/main_window.py)）

只负责：创建 `JmClient`（注入 `SerialTransport`）、组装面板、连接信号槽、管理连接状态与显示刷新定时器。

- **左栏（滚动）**：连接 / 系统控制 / 运动控制 / 遥测订阅 / 设备信息 / 菜单配置
- **右栏（选项卡 + 日志）**：实时反馈 / 状态机 / 电机参数 / 电机配置 / 孪生参数 / 故障信息 / 实时曲线
- **状态栏**：连接状态 + TX/RX 速率与累计 + 帧数

#### 面板清单

| 面板 | 文件 | 说明 |
|------|------|------|
| ConnectionPanel | [connection_panel.py](ui/panels/connection_panel.py) | 串口枚举 / 波特率 / 连接按钮 / 虚拟引擎入口 |
| ControlPanel | [control_panel.py](ui/panels/control_panel.py) | 使能 / 失能 / 急停 / 清障 |
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

- `_feedback_pending` 有界 deque 缓存（默认 2000 帧，可通过菜单配置）
- `_ui_timer` 固定周期（默认 50ms）批量出队
- 超出单 tick 上限（1000 帧）的旧帧丢弃，每 2 秒告警一次
- 批量向量化喂曲线（`feed_feedback_batch`），避免逐帧 Python 调用开销

### Digital Twin Engine

高保真数字孪生，模块化分层：

```
twin_config   ── 电机参数加载 (MotorParam, 对齐固件 motor_param_t)
twin_physics  ── 物理模型 (dq电气方程 + 机械方程 + 热模型 + 编码器)
twin_load     ── 关节负载模型 (连杆惯量/重力矩/外力扰动/碰撞检测)
twin_control  ── 控制算法复刻 (PID + FOC + 级联控制 + 无扰切换)
twin_fsm       ── 状态机复刻 (IDLE/READY/RUN/FAULT/SAFETY + 故障检测)
digital_twin   ── 多速率仿真引擎 (电流环 10kHz / 位置环 2kHz + QThread)
twin_responder ── 协议应答器 (协议命令 <-> MotorCmd, 遥测帧构造)
virtual_transport ── Transport 实现 (QTimer 驱动孪生仿真 + 帧回环)
```

上层 `JmClient` / UI 无需感知差异：选择「虚拟孪生引擎」即用 `VirtualTransport`。

---

## Communication Protocol

### 串口帧格式

```
┌──────┬─────────┬──────────┬───────────────┬────────────┐
│ A5 5A│ LEN(2B) │ HDR_CHK  │ CMD + Payload │ CRC16(2B)  │
│ 帧头 │ 大端    │ 1B       │ LEN 字节      │ 小端       │
└──────┴─────────┴──────────┴───────────────┴────────────┘
```

- `LEN = CMD(1) + Payload(n)` 总字节数（大端）
- `HDR_CHK = (STX_H + STX_L + LEN_H + LEN_L) & 0xFF`
- `CRC16` 覆盖数据区（CMD + Payload），XMODEM 多项式 `0x1021`，初值 0，MSB-first，无反转，无异或
- 字节序：多字节小端（LE），浮点 IEEE-754 f32
- 与固件 `packer_parser + crc16(XMODEM)` 一致

### 命令空间

`0x00 ~ 0xFE`，按功能分 9 段（详见 [jmproto/cmd_def.py](jmproto/cmd_def.py)）：

| 段 | 范围 | 类别 | 代表命令 |
|----|------|------|----------|
| 1 | 0x00~0x0F | 系统控制 | IDLE/HOLD/BRAKE/ESTOP/ENABLE/DISABLE/STOP |
| 2 | 0x10~0x2F | 运动控制 | OPEN_LOOP/CURRENT/TORQUE/MIT/VELOCITY/POSITION |
| 3 | 0x30~0x4F | 高级力控 | IMPEDANCE/ADMITTANCE/FORCE_CONTROL/GRAVITY_COMP |
| 4 | 0x50~0x6F | 轨迹同步 | PVT/CUBIC_SPLINE/TRAPEZOIDAL/S_CURVE/HOMING |
| 5 | 0x70~0x8F | 特殊应用与测试 | STEP_DIR/JOG/TEST_* |
| 6 | 0x90~0xAF | 校准 | CALIB_MOTOR_PARAM/CALIB_ENCODER_OFFSET |
| 7 | 0xB0~0xBF | 系统诊断 | CLEAR_FAULT/ENTER_BOOTLOADER/SAVE_CONFIG |
| 8 | 0xC0~0xCF | 反馈查询 | READ_FEEDBACK/READ_STATE/TELEMETRY/SET_TELEMETRY |
| 9 | 0xD0~0xFF | 设备信息与参数 | READ_DEV_INFO/PARAM_READ/PARAM_WRITE |

错误应答：`NACK (0xFE) + cmd + err_code`，`err_code` 见 `JmErr`：

| 值 | 名称 | 含义 |
|----|------|------|
| 0x00 | OK | 成功 |
| 0x01 | UNSUPPORTED | 命令不支持 |
| 0x02 | OUT_OF_RANGE | 超范围 |
| 0x03 | STATE_DENY | 状态机拒绝 |
| 0x04 | BAD_PARAM_ID | 参数 ID 无效 |
| 0x05 | CRC | CRC 校验失败 |
| 0x06 | LENGTH | 长度错误 |
| 0x07 | READ_ONLY | 只读参数 |
| 0x08 | FLASH | Flash 操作失败 |
| 0x09 | FAULT_STATE | 故障态禁止 |
| 0x0A | CALIB_BUSY | 校准忙 |

### 遥测订阅

通过 `SET_TELEMETRY (0xCB)` 订阅周期推送，由 `TELEMETRY (0xCA)` 帧返回所需字段。

**载荷格式**：`enable(u8) + mask(u16) + period_ms(u16)`（共 5 字节，小端）

**位掩码（`JmTlmBit`）**：

| 位 | 字段 | 说明 |
|----|------|------|
| 0 | POS_VEL | 位置 / 速度 |
| 1 | DQ | DQ 电流 |
| 2 | PHASE | 三相电流 |
| 3 | BUS | 母线 |
| 4 | TEMP | 温度 |
| 5 | MULTITURN | 多圈 |
| 6 | TORQUE | 力矩 |
| 7 | FAULT | 故障 / 警告 |
| 8 | STATE | 状态机 |
| 9 | DEBUG | 调试通道 |

> **注意**：实时反馈面板的三相电流依赖 `DQ` + `PHASE` 位。主窗口启动恢复设置时会强制补全这两位，但若手动构造掩码请确保包含，否则遥测帧不含 ia/ib/ic，显示恒为 0。

---

## Data-Driven Mechanism

**核心**：协议表以 CSV 形式随工具携带，运行时由 [jmproto/registry.py](jmproto/registry.py) 加载为结构化规格，UI 按规格动态渲染。

### 命令表（resources/joint_motor_command_list.csv）

100 条命令，列：序号 / 命令名称 / CMD(hex) / 方向 / 功能类别 / 串口请求载荷 / 串口应答载荷 / 数据长度 / 编码格式 / 单位范围 / CAN_ID / CAN 数据区 / 备注。

- 请求载荷字段语法：`{pos:f32;vel:f32}` → `[(pos, f32), (vel, f32)]`
- 固定值字段：`magic:u32=0xB00710AD` → UI 不显示输入框，自动填入
- MotionPanel 按类别（运动控制 / 高级力控 …）填充下拉，选中后按 `fields` 动态生成对应输入框：
  - `f32` → QDoubleSpinBox
  - `u8` / `u32` → QSpinBox

### 参数表（resources/joint_motor_param_index.csv）

83 项参数，列：param_id / 参数名 / 中文含义 / 数据类型 / 字节数 / 单位 / 所属分组 / 读写。

- ParamPanel 用 `QTableWidget` 按 `group` 分组列出（电机本体 / 电流环 / 速度环 / 位置环 / 保护 …）
- 每行：中文名 / param_id / 类型·单位 / 当前值 / 读·写按钮
- 读写走 `PARAM_READ(0xE0)` / `PARAM_WRITE(0xE1)` + `pack_param_value` / `unpack_param_value`
- 支持批量读（队列 + 应答驱动）与批量写

### 类型支持（jmproto/codec.py）

| 类型 | 字节数 | struct 格式 |
|------|--------|-------------|
| u8 | 1 | `<B` |
| i8 | 1 | `<b` |
| u16 | 2 | `<H` |
| i16 | 2 | `<h` |
| u32 | 4 | `<I` |
| i32 | 4 | `<i` |
| f32 | 4 | `<f` |
| char[N] | N | UTF-8 定长补零 |

### 健壮性

CSV 缺失或解析失败时回退到内置最小命令集（ENABLE/DISABLE/POSITION/VELOCITY/TORQUE 等），并在日志告警——保证无 CSV 也能跑。

---

## Configuration & Persistence

本工程无环境变量，所有用户偏好持久化到 [resources/ui_layout.json](resources/ui_layout.json)（由 [ui/layout_store.py](ui/layout_store.py) 管理）。

### 主题（[ui/theme.py](ui/theme.py)）

- 单例 `theme`，深色 / 浅色双色板，全局 QSS 生成
- 三类消费者：
  1. **常规组件** — `main.py` 用 `theme.qss()` 一次性铺底，切换时重铺
  2. **自绘图** — `feedback_panel._T` / `state_machine_panel._Theme` 转发到 `theme.c(key)`
  3. **曲线** — `plot_panel` 用 `theme.hex(key)` 设 pyqtgraph 背景 / 画笔 / 轴色
- 切换流程：`theme.set('light'/'dark')` → 持久化 → emit `changed` → 订阅者重铺 / 重绘

### 持久化字段（ui_layout.json 的 `settings` 节）

| 字段 | 类型 | 说明 |
|------|------|------|
| `baud` | string | 波特率 |
| `display_period_ms` | int | 显示刷新周期（默认 50ms） |
| `display_buffer_max` | int | 最大缓存帧数（默认 2000） |
| `main_window` | {w,h,x,y} | 主窗口尺寸与位置 |
| `right_splitter` | [int,int] | 右侧 splitter 尺寸 |
| `log_splitter_sizes` | [int,int] | 日志显隐时记录的尺寸 |
| `layout_edit` | bool | 布局编辑开关 |
| `telemetry.mask` | int | 遥测位掩码 |
| `telemetry.period_ms` | int | 遥测周期 |
| `feedback_poll.enabled` | bool | 反馈轮询使能 |
| `feedback_poll.period_ms` | int | 反馈轮询周期 |
| `log_visible` | bool | 日志面板显隐 |
| `log_opts` | dict | 日志选项 |
| `theme` | string | 主题名（dark/light） |
| `plot`/`motion`/`motor_param`/`motor_config`/`twin_param`/`fault_info` | dict | 各面板配置 |

退出时自动写入，启动时自动恢复。

---

## Available Scripts

| 命令 | 说明 |
|------|------|
| `python main.py` | 启动主程序 |
| `python -m tools.twin.main` | 启动数字孪生独立上位机 |
| `python -m tools.waveform.main` | 启动波形显示上位机 |
| `python tools/twin/smoke_test.py` | 孪生引擎冒烟测试 |
| `python tools/test/test_state_sync.py` | 状态同步集成测试 |
| `python tools/test/test_twin_engine.py` | 孪生引擎集成测试 |
| `python tools/test/test_twin_stress.py` | 孪生引擎压力测试 |
| `python -c "import jmproto; print('OK')"` | 协议层导入自测 |
| `python -m py_compile main.py ui/main_window.py` | 编译检查 |
| `packaging\build_exe.bat` | 一键打包 EXE |
| `packaging\build_exe.bat -OneFile` | 单文件模式打包 |

---

## Testing

### 协议层单元自测（无需硬件 / PyQt）

```bash
python -c "import jmproto; print('OK')"
```

关键断言（可在 [tools/test/](tools/test/) 中扩展）：

- CRC 表前 4 项：`0x0000, 0x1021, 0x2042, 0x3063`，表[255]=`0x1ef0`
- `pack_command(0x16, {pos:1, vel:2})` 得 8 字节
- `pack_command(0x15, {pos:1.57})` 与规范一致
- 22B `READ_FEEDBACK` 帧 + 一帧 `TELEMETRY`(mask 含 POS_VEL\|STATE) 喂入解析，字段正确

示例断言脚本：

```python
from jmproto import crc16_calc, get_registry

# CRC 自测
assert crc16_calc(b'') == 0x0000
assert crc16_calc(bytes([0x15]) + (1.57).to_bytes(4, 'little')) is not None

# 注册表自测
reg = get_registry()
assert len(reg.commands) == 100, f"命令数: {len(reg.commands)}"
assert len(reg.params) == 83, f"参数数: {len(reg.params)}"

# 打包自测
payload = reg.pack_command(0x15, {'pos_ref': 1.57})
assert len(payload) == 4
print("ALL OK")
```

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
5. 断开 / 重连不崩溃（验证 `_io_lock` + 线程生命周期）

### 回归检查

确认 CRC、单锁两处修复在新结构中存在：

```bash
# 检查 CRC 多项式
# (在代码中搜索 0x1021)
# 检查 _io_lock
# (在 transport/serial_transport.py 中)
```

---

## Deployment (EXE Packaging)

使用 PyInstaller 打包为 Windows 可执行文件。

### 一键打包

```powershell
# 在工程根目录执行
packaging\build_exe.bat
```

### 进阶参数

```powershell
# 指定 Python 解释器
packaging\build_exe.bat -Python "C:\path\to\python.exe"

# 单文件模式（生成单个 exe）
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

### 打包脚本做了什么

[packaging/build_exe.ps1](packaging/build_exe.ps1) 会：

1. 安装运行时依赖（`requirements.txt`）与构建依赖（`pyinstaller>=6.0`）
2. 检查 PyInstaller 可用
3. 调用 PyInstaller，自动收集：
   - `resources` 目录（`--add-data`）
   - PyQt6 全部子模块（`--collect-all`）
   - pyqtgraph 全部子模块（`--collect-all`）
   - `serial` 子模块（`--collect-submodules`）
   - `transport.virtual_engine` 子模块（`--collect-submodules`）
   - 隐藏导入 `serial.tools.list_ports`
4. 应用图标 `resources/pic/log_ioc.ico`（若存在）
5. 输出到 `dist/`

---

## Troubleshooting

### Q1：连接串口后程序崩溃（段错误）

**原因**：主线程发送与工作线程接收并发操作同一串口句柄，在 Windows 上触发底层访问冲突。

**解决**：确认使用的是当前代码的 [serial_transport.py](transport/serial_transport.py)，且 `_io_lock` 单锁串行化未被破坏。早期版本未加锁会导致此问题。

### Q2：CRC 校验失败，固件 NACK

**原因**：CRC 算法不一致。

**解决**：CRC 必须是 **CRC-16/XMODEM**（多项式 `0x1021`，初值 0，MSB-first，无反转，无异或）。见 [jmproto/crc16.py](jmproto/crc16.py)。可用以下命令自测：

```python
from jmproto import crc16_calc
# 空字节应为 0
assert crc16_calc(b'') == 0x0000
```

### Q3：三相电流显示恒为 0

**原因**：遥测掩码未包含 `DQ` + `PHASE` 位。

**解决**：主窗口启动恢复设置时会强制补全这两位，但若手动构造掩码或修改 ui_layout.json，请确保 `telemetry.mask` 包含 `0x06`（DQ \| PHASE）。

### Q4：CSV 缺失还能用吗

**能**。[registry.py](jmproto/registry.py) 在 CSV 缺失或解析异常时回退到内置最小命令集，并在日志面板告警。但完整功能需 `resources/*.csv` 就位。

### Q5：CAN 传输何时可用

**当前状态**：[can_transport.py](transport/can_transport.py) 为占位，`open()` 抛 `NotImplementedError`。已预留 MIT 定点压缩、多帧分包、ID 拆装等常量。

**启用步骤**：

1. 安装 `python-can>=4.0`
2. 按文件头注释照搬固件 `jm_proto_can.h` 的实现
3. 实现 `open` / `close` / `send` / 后台接收线程

### Q6：高频遥测导致 UI 卡顿

**解决**：通过「菜单配置 → 缓存设置」调整：

- 显示更新周期（默认 50ms）
- 最大缓存帧数（默认 2000）

超出缓存的旧帧会被丢弃，每 2 秒告警一次。也可在 `ui_layout.json` 中直接修改 `display_period_ms` 与 `display_buffer_max`。

### Q7：如何恢复默认布局

**解决**：删除 [resources/ui_layout.json](resources/ui_layout.json) 中的 `settings` 节，或整个文件，重启程序即恢复默认。

### Q8：PyQt6 安装失败

**原因**：可能缺少 Visual C++ 运行时或 pip 版本过旧。

**解决**：

```bash
python -m pip install --upgrade pip
python -m pip install PyQt6 -i https://pypi.tuna.tsinghua.edu.cn/simple
```

如仍失败，检查 Python 版本是否 ≥ 3.9，并确保是 64 位。

### Q9：打包后启动找不到 resources

**原因**：PyInstaller 未正确收集数据文件。

**解决**：确认使用 [packaging/build_exe.ps1](packaging/build_exe.ps1) 打包，其中已配置 `--add-data "$Resources;resources"`。手动打包需加此参数。

---

## Related Documentation

- [doc/PyQt上位机框架重构方案.md](doc/PyQt上位机框架重构方案.md) — 重构背景与设计决策
- [resources/MotorInfo_readme.md](resources/MotorInfo_readme.md) — MotorInfo 1024B 参数区结构定义
- [tools/waveform/protocol_reference.md](tools/waveform/protocol_reference.md) — 协议参考手册（独立实现上位机用）

---

## 版本

- 应用版本：1.0.0
- 协议版本：与固件 `User/Protocol/joint_proto/jm_cmd_def.h` 一致
