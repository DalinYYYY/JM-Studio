# 上位机 CAN 通信功能实现方案

## 一、概要

为上位机新增 CAN 通信能力，首期接入 PCAN-USB 硬件，架构上预留对 SocketCAN / Vector / Kvaser 等其他 CAN 硬件的兼容。UART 协议与 CAN 协议在 `jmproto` 层已设计为兼容（命令码 + 载荷语义一致），仅物理帧格式不同：UART 走 `A5 5A + LEN + CMD + PAYLOAD + CRC16` 串口帧，CAN 走 `(CMD << 8) | motor_id` 扩展仲裁 ID + 数据区。CAN 数据区对 `MIT(0x13)/IMPEDANCE(0x30)` 命令做定点压缩进 8 字节，对超 8 字节载荷做多帧分包。

## 二、当前状态分析

### 2.1 已就绪的基础设施（无需改动）

| 资产 | 位置 | 作用 |
|---|---|---|
| `Transport` 抽象基类 | [transport/base.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/transport/base.py) | 统一 `frame_received/connected/error_occurred` 信号 + `open/close/is_open/send/start/stop` 接口 |
| `JmClient.set_transport()` | [core/motor_client.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/core/motor_client.py) | 运行期热切换传输，UI 完全无感 |
| `CanTransport` 占位骨架 | [transport/can_transport.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/transport/can_transport.py) | 已有协议常量 + `can_make_id/can_get_cmd/can_get_motor_id` + MIT 范围常量 + 设计注释 |
| `JmCmd` 枚举 | [jmproto/cmd_def.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/jmproto/cmd_def.py) | 已含 `MIT=0x13` / `IMPEDANCE=0x30` / `SET_CAN_ID=0xF0` / `BROADCAST_SYNC=0xF2` |
| `VirtualTransport` | [transport/virtual_engine/virtual_transport.py](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/transport/virtual_engine/virtual_transport.py) | 协议层兼容性的活体证明：既非串口也非 CAN，但业务全通 |
| `MainWindow._on_connect` | [ui/main_window.py#L942](file:///d:/AAWorkSpace/001_JointMotor/SW/JointMotor/User/Tools/pyqt_gui/ui/main_window.py#L942) | 已有按端口前缀分流（"VIRTUAL" → VirtualTransport）的范式 |

### 2.2 需要改造的部分

| 序号 | 文件 | 改造内容 |
|---|---|---|
| 1 | `transport/can_transport.py` | 实现 `open/close/is_open/send/start/stop` + 后台收发线程 + MIT 压缩/解压 + 多帧分包 + 通道枚举 |
| 2 | `ui/panels/connection_panel.py` | 顶部加"传输类型"下拉(串口/CAN)，切换时显隐对应控件；新增 CAN 配置控件(通道/比特率/电机ID) |
| 3 | `ui/main_window.py` | 新增 `_on_connect_can(channel, bitrate, motor_id)` 分支；持久化加 `transport_type/can_bitrate/can_motor_id/can_channel_name` |
| 4 | `ui/feature_flags.py` | `_DEFAULTS` 加 `"can_transport": True` |
| 5 | `resources/feature_flags.json` | 加 `"can_transport": true`（若文件不存在则由 `_DEFAULTS` 兜底） |
| 6 | `requirements.txt` | 加 `python-can>=4.0.0` |

### 2.3 协议兼容性证明

`VirtualTransport` 已验证：只要 Transport 子类正确发出 `(cmd, payload)` 逻辑帧，整个上层（标定/PID/参数读写/遥测/绘图）一行不用改。CAN Transport 只需把 CAN 物理帧解出 `(cmd, payload)` 喂给 `frame_received`，业务分发逻辑 100% 复用。

## 三、方案设计

### 3.1 总体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                  ConnectionPanel (UI 改造)                        │
│  ┌─────────────┐  ┌──────────────────────────────────────────┐   │
│  │传输类型下拉  │→ │ 串口模式: 端口下拉+刷新+波特率            │   │
│  │ (串口/CAN)  │  │ CAN 模式: 通道下拉+刷新+比特率+电机ID     │   │
│  └─────────────┘  └──────────────────────────────────────────┘   │
│                       连接/断开按钮(复用)                          │
│  信号: connect_requested(port,baud) / connect_can_requested(...)  │
└────────────┬──────────────────────────────────┬──────────────────┘
             │ 串口                              │ CAN
             ▼                                  ▼
┌──────────────────────────┐    ┌──────────────────────────────────┐
│ MainWindow._on_connect   │    │ MainWindow._on_connect_can       │
│ (现有, 不动)              │    │ (新增)                            │
└────────────┬─────────────┘    └────────────┬─────────────────────┘
             │                                │
             ▼                                ▼
       client.open(port,baud)      client.set_transport(CanTransport())
                                     client.open(channel,bitrate,motor_id)
                                                │
                                                ▼
                            ┌──────────────────────────────────────┐
                            │ CanTransport (本次实现)               │
                            │  - python-can Bus(interface='pcan')   │
                            │  - 后台线程收发 + _io_lock 单锁        │
                            │  - CAN 帧拆装 → (cmd, payload)         │
                            │  - MIT 压缩/解压 (0x13/0x30)           │
                            │  - 多帧分包 (载荷>8字节)               │
                            └──────────────────────────────────────┘
                                                │
                                                ▼
                            ┌──────────────────────────────────────┐
                            │ JmClient._on_frame(cmd, payload)     │
                            │ (完全复用, 零改动)                    │
                            └──────────────────────────────────────┘
```

### 3.2 CanTransport 完整实现（核心文件）

文件：`transport/can_transport.py`

#### 3.2.1 依赖与导入

```python
import threading
import time
from typing import Optional

import can  # python-can

from .base import Transport
```

`import can` 失败时（python-can 未安装），模块顶层即抛 `ImportError`。`ConnectionPanel` 在切换到 CAN 模式时用 `try: from transport.can_transport import CanTransport; except ImportError: ...` 探测可用性，不可用时弹提示并禁用连接按钮。

#### 3.2.2 通道枚举（静态方法）

```python
@staticmethod
def list_channels() -> list:
    """列出可用 PCAN 通道: [(channel_name, description), ...]

    channel_name 形如 'PCAN_USBBUS1'，传给 can.Bus(channel=...) 使用。
    description 形如 'PCAN-USB (Ch.1) [FD]' 供 UI 显示。
    """
    try:
        configs = can.interface.detect_available_configs('pcan')
    except Exception:
        return []
    out = []
    for cfg in configs:
        # python-can 4.x 返回 dict: {'interface': 'pcan', 'channel': 'PCAN_USBBUS1', ...}
        ch = cfg.get('channel', '')
        if not ch:
            continue
        # 描述: 优先用 cfg 中已有信息, 否则用 channel 名
        desc = cfg.get('description') or ch.replace('PCAN_USBBUS', 'PCAN-USB Ch.')
        out.append((ch, desc))
    return out
```

#### 3.2.3 类结构与生命周期

参考 `SerialTransport` 的双标志 + 单锁线程模型：

```python
class CanTransport(Transport):
    name = "can"
    _READ_TIMEOUT_S = 0.001
    _IDLE_WAIT_S = 0.0005
    _CLOSED_WAIT_S = 0.01

    def __init__(self):
        super().__init__()
        self._bus: Optional[can.Bus] = None
        self._channel: str = ""
        self._bitrate: int = 1000000
        self._motor_id: int = 1
        self._alive = False       # 线程生命周期
        self._running = False     # CAN 总线打开标志
        self._io_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._wake_event = threading.Event()
        # 多帧重组缓冲: {src_can_id: {'seq': int, 'buf': bytearray}}
        self._rx_segments: dict = {}
        self._tx_seq: int = 0     # 发送侧多帧序号计数器
```

#### 3.2.4 打开/关闭

```python
def open(self, channel: str = "", bitrate: int = 1000000,
         motor_id: int = 1, **_) -> bool:
    try:
        # 静默关闭旧连接
        if self._bus is not None:
            with self._io_lock:
                self._running = False
                try:
                    self._bus.shutdown()
                except Exception:
                    pass
            self._wake_event.set()
        bus = can.Bus(
            interface='pcan',
            channel=channel,
            bitrate=bitrate,
        )
        with self._io_lock:
            self._bus = bus
            self._channel = channel
            self._bitrate = bitrate
            self._motor_id = motor_id & 0xFF
            self._rx_segments.clear()
            self._tx_seq = 0
            self.reset_stats()
            self._running = True
        self._wake_event.set()
        self.connected.emit(True)
        return True
    except Exception as e:
        self.error_occurred.emit(f"CAN 打开失败: {e}")
        return False

def close(self):
    was_running = self._running
    with self._io_lock:
        self._running = False
        if self._bus is not None:
            try:
                self._bus.shutdown()
            except Exception:
                pass
            self._bus = None
    self._wake_event.set()
    if was_running:
        self.connected.emit(False)

def is_open(self) -> bool:
    return self._bus is not None and self._running

def start(self):
    if self._thread is not None and self._thread.is_alive():
        return
    self._alive = True
    self._thread = threading.Thread(target=self._run, name="CanRx", daemon=True)
    self._thread.start()

def stop(self):
    self.close()
    self._alive = False
    if self._thread is not None:
        self._thread.join(timeout=2.0)
        self._thread = None
```

#### 3.2.5 后台接收线程

```python
def _run(self):
    while self._alive:
        try:
            if self._running and self._bus is not None:
                with self._io_lock:
                    if self._bus is None or not self._running:
                        continue
                    msg = self._bus.recv(timeout=self._READ_TIMEOUT_S)
                if msg is not None:
                    if msg.is_error_frame or msg.is_remote_frame:
                        # 忽略错误帧/RTR
                        continue
                    self.rx_bytes += msg.dlc
                    self._handle_rx_msg(msg)
                else:
                    self._wake_event.wait(self._IDLE_WAIT_S)
                    self._wake_event.clear()
            else:
                self._wake_event.wait(self._CLOSED_WAIT_S)
                self._wake_event.clear()
        except can.CanError as e:
            self.error_occurred.emit(f"CAN 异常: {e}")
            self.close()
            self._wake_event.wait(self._CLOSED_WAIT_S)
            self._wake_event.clear()
        except Exception as e:
            self.error_occurred.emit(f"CAN 未知错误: {e}")
            self._wake_event.wait(self._CLOSED_WAIT_S)
            self._wake_event.clear()
```

#### 3.2.6 接收帧分发（单帧/多帧/MIT 解压）

```python
def _handle_rx_msg(self, msg):
    """收到一个 CAN 物理帧 → 拆 ID → 单帧/重组多帧/MIT解压 → emit 逻辑帧"""
    can_id = int(msg.id)
    cmd = can_get_cmd(can_id)
    src_motor_id = can_get_motor_id(can_id)
    data = bytes(msg.data[:msg.dlc])

    if cmd == JmCmd.MIT or cmd == JmCmd.IMPEDANCE:
        # MIT/阻抗命令的反馈帧也走 8 字节压缩格式
        # 反馈帧结构: pos16 vel16 tq16 temp8 err8 (共 8 字节)
        payload = self._unpack_mit_feedback(data)
        self.rx_frames += 1
        self.frame_received.emit(cmd, payload)
        return

    if len(data) <= CAN_SINGLE_MAX:
        # 单帧: data 即为载荷(不含 CMD, CMD 走仲裁 ID)
        payload = data
        self.rx_frames += 1
        self.frame_received.emit(cmd, payload)
    else:
        # 不应发生: CAN 经典帧数据区最多 8 字节
        # 多帧分包由发送侧决定, 接收侧按控制字重组
        pass

    # 多帧重组: data[0]=控制字, data[1..]=片段
    if len(data) > 0 and (data[0] & CAN_SEG_LAST) == 0:
        # 非末帧
        seq = data[0] & CAN_SEG_SEQ_MASK
        seg = data[1:1+CAN_SEG_PAYLOAD]
        self._rx_segments.setdefault(can_id, {'seq': 0, 'buf': bytearray()})
        self._rx_segments[can_id]['buf'].extend(seg)
        self._rx_segments[can_id]['seq'] = seq
    elif len(data) > 0 and (data[0] & CAN_SEG_LAST):
        # 末帧
        seg = data[1:1+CAN_SEG_PAYLOAD]
        buf = self._rx_segments.get(can_id, {'buf': bytearray()})['buf']
        buf.extend(seg)
        payload = bytes(buf)
        self._rx_segments.pop(can_id, None)
        self.rx_frames += 1
        self.frame_received.emit(cmd, payload)
```

**注**：上述多帧重组逻辑需对照固件 `jm_proto_can.h` 的实际分包格式校准。固件侧是否在数据区首字节放控制字、是否对单帧也走控制字（如 `0x80` 表示单帧末帧），需联调时确认。方案中按注释预留的"`data[0]=控制字=(末帧标志<<7)|(序号&0x7F)`"实现，并提供 `_is_single_frame()` 辅助判断。

#### 3.2.7 发送（单帧/多帧/MIT 压缩）

```python
def send(self, cmd: int, payload: bytes = b'') -> bool:
    if cmd == JmCmd.MIT or cmd == JmCmd.IMPEDANCE:
        return self._send_mit(cmd, payload)
    # 普通命令: 载荷 <= 8 字节单帧, > 8 字节多帧分包
    if len(payload) <= CAN_SINGLE_MAX:
        return self._send_single(cmd, payload)
    return self._send_multi(cmd, payload)

def _send_single(self, cmd: int, payload: bytes) -> bool:
    can_id = can_make_id(cmd, self._motor_id)
    msg = can.Message(
        arbitration_id=can_id,
        data=payload,
        is_extended_id=True,  # 29 位扩展帧
    )
    return self._write_msg(msg)

def _send_multi(self, cmd: int, payload: bytes) -> bool:
    """多帧分包: 每帧 data[0]=控制字, data[1..]=片段(<=7字节)"""
    can_id = can_make_id(cmd, self._motor_id)
    offset = 0
    seq = 0
    total = len(payload)
    ok = True
    while offset < total:
        seg = payload[offset:offset+CAN_SEG_PAYLOAD]
        offset += CAN_SEG_PAYLOAD]
        is_last = offset >= total
        ctrl = (CAN_SEG_LAST if is_last else 0) | (seq & CAN_SEG_SEQ_MASK)
        frame_data = bytes([ctrl]) + seg
        msg = can.Message(
            arbitration_id=can_id,
            data=frame_data,
            is_extended_id=True,
        )
        ok = ok and self._write_msg(msg)
        seq += 1
    return ok

def _write_msg(self, msg) -> bool:
    with self._io_lock:
        try:
            if not (self._bus is not None and self._running):
                return False
            self._bus.send(msg, timeout=0.02)
            self.tx_bytes += msg.dlc
            self.tx_frames += 1
            return True
        except Exception:
            return False
```

#### 3.2.8 MIT 压缩/解压（本次实现）

依据 `can_transport.py` 顶部注释的位分配：`pos16 + vel12 + kp12 + kd12 + tff12 = 64 bit`。

```python
def _float_to_raw(self, value: float, min_val: float, max_val: float, bits: int) -> int:
    """定点量化: raw = (value-min)/(max-min) * (2^bits - 1), 饱和到 [0, 2^bits-1]"""
    span = max_val - min_val
    if span <= 0:
        return 0
    raw = int((value - min_val) / span * ((1 << bits) - 1) + 0.5)
    return max(0, min((1 << bits) - 1, raw))

def _raw_to_float(self, raw: int, min_val: float, max_val: float, bits: int) -> float:
    span = max_val - min_val
    return min_val + raw / ((1 << bits) - 1) * span

def _pack_mit(self, pos: float, vel: float, kp: float, kd: float, tff: float) -> bytes:
    """MIT 命令 8 字节压缩: pos16 vel12 kp12 kd12 tff12 (大端)"""
    p = self._float_to_raw(pos, MIT_POS_MIN, MIT_POS_MAX, 16)
    v = self._float_to_raw(vel, MIT_VEL_MIN, MIT_VEL_MAX, 12)
    kp = self._float_to_raw(kp, MIT_KP_MIN, MIT_KP_MAX, 12)
    kd = self._float_to_raw(kd, MIT_KD_MIN, MIT_KD_MAX, 12)
    tf = self._float_to_raw(tff, MIT_TFF_MIN, MIT_TFF_MAX, 12)
    # 大端拼 64 位
    val = (p << 48) | (v << 36) | (kp << 24) | (kd << 12) | tf
    return val.to_bytes(8, 'big')

def _unpack_mit(self, data: bytes) -> tuple:
    """反向解压 MIT 命令 8 字节 → (pos, vel, kp, kd, tff)"""
    val = int.from_bytes(data[:8], 'big')
    p = (val >> 48) & 0xFFFF
    v = (val >> 36) & 0xFFF
    kp = (val >> 24) & 0xFFF
    kd = (val >> 12) & 0xFFF
    tf = val & 0xFFF
    return (
        self._raw_to_float(p, MIT_POS_MIN, MIT_POS_MAX, 16),
        self._raw_to_float(v, MIT_VEL_MIN, MIT_VEL_MAX, 12),
        self._raw_to_float(kp, MIT_KP_MIN, MIT_KP_MAX, 12),
        self._raw_to_float(kd, MIT_KD_MIN, MIT_KD_MAX, 12),
        self._raw_to_float(tf, MIT_TFF_MIN, MIT_TFF_MAX, 12),
    )

def _unpack_mit_feedback(self, data: bytes) -> bytes:
    """反馈帧 8 字节: pos16 vel16 tq16 temp8 err8 → 转为 jmproto READ_FEEDBACK 兼容载荷

    固件侧 READ_FEEDBACK 串口帧载荷结构(见 jmproto.feedback.parse_read_reply):
    pos(float32) + vel(float32) + torque(float32) + temp(int16) + vbus(float32) + fault(uint16) + ...
    此处把压缩反馈还原为同样的 bytes 序列, 让 JmClient 业务分发零改动。
    """
    val = int.from_bytes(data[:8], 'big')
    p = (val >> 48) & 0xFFFF
    v = (val >> 32) & 0xFFFF
    tq = (val >> 16) & 0xFFFF
    temp = (val >> 8) & 0xFF
    err = val & 0xFF
    pos = self._raw_to_float(p, MIT_POS_MIN, MIT_POS_MAX, 16)
    vel = self._raw_to_float(v, MIT_VEL_MIN, MIT_VEL_MAX, 16)
    torque = self._raw_to_float(tq, MIT_TFF_MIN, MIT_TFF_MAX, 16)
    temp_f = self._raw_to_float(temp, FB_TEMP_MIN, FB_TEMP_MAX, 8)
    # 按 jmproto.feedback 期望的载荷格式打包 (小端)
    import struct
    return struct.pack('<fffHB', pos, vel, torque, temp_f, err)
```

**关键约束**：MIT 压缩的位字段顺序、字节序、反馈帧的字段顺序必须与固件 `jm_proto_can.h` 严格一致。上述实现按 MIT Cheetah 协议惯例（大端、`pos→vel→kp→kd→tff` 顺序）给出，**联调前必须对照固件源码校准**，否则会出现命令解析错位。

#### 3.2.9 MIT 命令发送路径

MIT 命令的 payload 在 `JmClient.send_command` 中是按 registry 字段打包的 bytes。CanTransport 需要把这些字段解出来再压缩：

```python
def _send_mit(self, cmd: int, payload: bytes) -> bool:
    """MIT/IMPEDANCE 命令: 把 payload(结构化字段) 解析为 5 个 float, 压缩成 8 字节发送"""
    import struct
    # 假设 payload 是 5 个 float32 (pos, vel, kp, kd, tff), 共 20 字节
    # 实际字段顺序/类型由 registry CommandSpec 决定, 联调时按 MIT 命令定义校准
    if len(payload) >= 20:
        pos, vel, kp, kd, tff = struct.unpack('<5f', payload[:20])
    else:
        pos = vel = kp = kd = tff = 0.0
    data = self._pack_mit(pos, vel, kp, kd, tff)
    return self._send_single(cmd, data)
```

### 3.3 ConnectionPanel UI 改造

文件：`ui/panels/connection_panel.py`

#### 3.3.1 信号扩展

```python
class ConnectionPanel(QGroupBox):
    connect_requested = pyqtSignal(str, int)          # 串口
    connect_can_requested = pyqtSignal(str, int, int) # CAN: channel, bitrate, motor_id
    disconnect_requested = pyqtSignal()
```

#### 3.3.2 布局改造

```
┌─ 串口连接 ──────────────────────────────────────┐
│ 传输类型: [串口 ▼]              ← 新增, 顶部    │
│                                                  │
│ ── 串口配置区 (传输类型=串口时显示) ──           │
│ 串口号:   [COM3 ...]      [刷新]                 │
│ 波特率:   [115200 ▼]                             │
│                                                  │
│ ── CAN 配置区 (传输类型=CAN时显示) ──            │
│ CAN 通道: [PCAN_USBBUS1 ...] [刷新]              │
│ 比特率:   [1000000 ▼]                            │
│ 电机 ID:  [1]                                    │
│                                                  │
│ [        连接        ]                           │
└──────────────────────────────────────────────────┘
```

#### 3.3.3 关键实现

```python
def __init__(self, parent=None):
    super().__init__("串口连接", parent)
    self._connected = False
    self._can_available = False  # python-can 是否可用
    self._build()
    self._detect_can_support()
    self.refresh_ports()

def _detect_can_support(self):
    """探测 python-can 与 PCAN 后端可用性"""
    if not feature_flags.is_enabled("can_transport"):
        self._can_available = False
        return
    try:
        from transport.can_transport import CanTransport
        self._can_available = True
    except ImportError:
        self._can_available = False

def _build(self):
    self._layout = QGridLayout(self)
    self._layout.setColumnStretch(1, 1)

    # 传输类型下拉 (第 0 行)
    self._layout.addWidget(QLabel("传输类型:"), 0, 0)
    self._combo_transport = QComboBox()
    self._combo_transport.addItem("串口", "serial")
    # CAN 选项仅在 python-can 可用时加入
    self._can_available = False  # 先占位, __init__ 中重新探测
    self._combo_transport.currentIndexChanged.connect(self._on_transport_changed)
    self._layout.addWidget(self._combo_transport, 0, 1, 1, 2)

    # 串口配置区 (第 1-2 行)
    self._layout.addWidget(QLabel("串口号:"), 1, 0)
    self._combo_port = QComboBox()
    # ... (现有代码不变)
    self._btn_refresh = QPushButton("刷新")
    # ...
    self._layout.addWidget(QLabel("波特率:"), 2, 0)
    self._combo_baud = QComboBox()
    # ... (现有代码不变)

    # CAN 配置区 (第 3-5 行)
    self._layout.addWidget(QLabel("CAN 通道:"), 3, 0)
    self._combo_can_channel = QComboBox()
    self._combo_can_channel.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
    self._combo_can_channel.setMinimumContentsLength(18)
    self._layout.addWidget(self._combo_can_channel, 3, 1)
    self._btn_can_refresh = QPushButton("刷新")
    self._btn_can_refresh.setMinimumWidth(48)
    self._btn_can_refresh.clicked.connect(self._refresh_can_channels)
    self._layout.addWidget(self._btn_can_refresh, 3, 2)

    self._layout.addWidget(QLabel("比特率:"), 4, 0)
    self._combo_can_bitrate = QComboBox()
    self._combo_can_bitrate.addItems(["500000", "800000", "1000000"])
    self._combo_can_bitrate.setCurrentText("1000000")
    self._combo_can_bitrate.setEditable(True)
    self._combo_can_bitrate.lineEdit().setValidator(QIntValidator(10000, 8000000, self))
    self._layout.addWidget(self._combo_can_bitrate, 4, 1, 1, 2)

    self._layout.addWidget(QLabel("电机 ID:"), 5, 0)
    self._spin_can_motor_id = QSpinBox()
    self._spin_can_motor_id.setRange(0, 255)
    self._spin_can_motor_id.setValue(1)
    self._layout.addWidget(self._spin_can_motor_id, 5, 1, 1, 2)

    # 连接按钮 (第 6 行)
    self._btn_connect = QPushButton("连接")
    # ... (现有样式代码不变)
    self._layout.addWidget(self._btn_connect, 6, 0, 1, 3)

def _on_transport_changed(self, idx: int):
    """传输类型切换 → 显隐对应配置区"""
    is_serial = (self._combo_transport.currentData() == "serial")
    # 串口区
    for w in (self._combo_port, self._btn_refresh):
        w.setVisible(is_serial)
    self._layout.labelForField(self._combo_port).setVisible(is_serial)  # "串口号:" 标签
    self._combo_baud.setVisible(is_serial)
    self._layout.labelForField(self._combo_baud).setVisible(is_serial)   # "波特率:" 标签
    # CAN 区
    for w in (self._combo_can_channel, self._btn_can_refresh,
              self._combo_can_bitrate, self._spin_can_motor_id):
        w.setVisible(not is_serial)
    # CAN 标签显隐
    self._layout.labelForField(self._combo_can_channel).setVisible(not is_serial)
    self._layout.labelForField(self._combo_can_bitrate).setVisible(not is_serial)
    self._layout.labelForField(self._spin_can_motor_id).setVisible(not is_serial)
    # 切换时刷新对应设备列表
    if is_serial:
        self.refresh_ports()
    else:
        self._refresh_can_channels()

def _refresh_can_channels(self):
    if self._connected or not self._can_available:
        return
    self._combo_can_channel.clear()
    try:
        from transport.can_transport import CanTransport
        for name, desc in CanTransport.list_channels():
            self._combo_can_channel.addItem(f"{name}  {desc}", name)
    except Exception as e:
        self._combo_can_channel.addItem(f"(枚举失败: {e})", "")

def _on_toggle(self):
    if self._connected:
        self.disconnect_requested.emit()
        return
    if self._combo_transport.currentData() == "can":
        channel = self._combo_can_channel.currentData() or ""
        if not channel:
            return
        try:
            bitrate = int(self._combo_can_bitrate.currentText())
        except ValueError:
            bitrate = 1000000
        motor_id = self._spin_can_motor_id.value()
        self.connect_can_requested.emit(channel, bitrate, motor_id)
        return
    # 串口分支 (现有逻辑)
    text = self._combo_port.currentText().strip()
    # ...
```

#### 3.3.4 设置读写接口（持久化用）

```python
def get_transport_type(self) -> str:
    return self._combo_transport.currentData() or "serial"

def set_transport_type(self, t: str):
    idx = self._combo_transport.findData(t)
    if idx >= 0:
        self._combo_transport.setCurrentIndex(idx)

def get_can_bitrate(self) -> str:
    return self._combo_can_bitrate.currentText()

def set_can_bitrate(self, text: str):
    if text:
        self._combo_can_bitrate.setCurrentText(str(text))

def get_can_motor_id(self) -> int:
    return self._spin_can_motor_id.value()

def set_can_motor_id(self, v: int):
    self._spin_can_motor_id.setValue(int(v))

def get_can_channel_name(self) -> str:
    return self._combo_can_channel.currentData() or ""

def set_can_channel_name(self, name: str):
    """按 channel 名匹配下拉项; 匹配不到则保持第一项"""
    idx = self._combo_can_channel.findData(name)
    if idx >= 0:
        self._combo_can_channel.setCurrentIndex(idx)
```

### 3.4 MainWindow 编排层改造

文件：`ui/main_window.py`

#### 3.4.1 新增 CAN 连接分支

```python
def _on_connect_can(self, channel: str, bitrate: int, motor_id: int):
    try:
        from transport.can_transport import CanTransport
    except ImportError:
        QMessageBox.warning(self, "提示", "python-can 未安装, 无法使用 CAN 传输")
        return
    if not isinstance(self._client.transport, CanTransport):
        self._client.set_transport(CanTransport())
    if self._client.open(channel=channel, bitrate=bitrate, motor_id=motor_id):
        self.statusBar().showMessage(f"已连接 {channel} @{bitrate} ID={motor_id}")
        self._cur_port = f"{channel}"
        self._force_disable_virtual_mode()
```

#### 3.4.2 信号连接

在 `_connect_signals` 中追加：

```python
self._conn_panel.connect_can_requested.connect(self._on_connect_can)
```

#### 3.4.3 导入语句

在文件顶部 import 区追加：

```python
from transport.can_transport import CanTransport  # 仅用于 isinstance 检查; 实际 import 在方法内做容错
```

为避免 python-can 未安装时整个 main_window 加载失败，`CanTransport` 的导入应放到方法内部（懒导入），顶部不导入。修改：

```python
# 顶部不导入 CanTransport
# _on_connect_can 内部:
from transport.can_transport import CanTransport
```

但 `_on_connect` 中已有的 `isinstance(self._client.transport, SerialTransport)` 检查需要扩展为"非串口也非 CAN 时才换传输"：

```python
def _on_connect(self, port: str, baud: int):
    if port == "VIRTUAL":
        # ... 现有虚拟分支不动
    # 真实串口: 若当前非 SerialTransport 则换回
    if not isinstance(self._client.transport, SerialTransport):
        self._client.set_transport(SerialTransport())
    # ...
```

由于 `CanTransport` 懒导入，`isinstance` 检查无法在顶部用，可改用 `transport.name` 属性判断：

```python
if self._client.transport.name != "serial":
    self._client.set_transport(SerialTransport())
```

同理 `_on_connect_can` 内：

```python
if self._client.transport.name != "can":
    self._client.set_transport(CanTransport())
```

`name` 属性已在 `Transport` 基类和各子类定义（`SerialTransport.name = "serial"`、`CanTransport.name = "can"`、`VirtualTransport.name = "virtual"`）。

### 3.5 持久化字段

文件：`ui/main_window.py` 的 `_collect_settings` / `_apply_settings`

#### 3.5.1 _collect_settings 追加

```python
s["transport_type"] = self._conn_panel.get_transport_type()
s["can_bitrate"] = self._conn_panel.get_can_bitrate()
s["can_motor_id"] = self._conn_panel.get_can_motor_id()
s["can_channel_name"] = self._conn_panel.get_can_channel_name()
```

#### 3.5.2 _apply_settings 追加

```python
tt = d.get("transport_type")
if tt:
    self._conn_panel.set_transport_type(tt)
cb = d.get("can_bitrate")
if cb:
    self._conn_panel.set_can_bitrate(cb)
cmi = d.get("can_motor_id")
if isinstance(cmi, (int, float)):
    self._conn_panel.set_can_motor_id(int(cmi))
# can_channel_name 需在 _refresh_can_channels 之后才能匹配, 故延迟到启动后
ccn = d.get("can_channel_name")
if ccn:
    QTimer.singleShot(100, lambda: self._conn_panel.set_can_channel_name(ccn))
```

`can_channel_name` 用 `QTimer.singleShot(100, ...)` 延迟套用，因为启动时 `refresh_ports` 异步枚举 CAN 通道，需等列表填充后再按名匹配。

### 3.6 feature_flags 开关

文件：`ui/feature_flags.py`

```python
_DEFAULTS = {
    # ... 现有项 ...
    "can_transport": True,
}
```

`ConnectionPanel._detect_can_support` 已检查 `feature_flags.is_enabled("can_transport")`，关闭时不下拉项里不出现 "CAN"，且 `CanTransport` 不被导入。

文件：`resources/feature_flags.json`（若存在）

```json
{
  "features": {
    "can_transport": true
  }
}
```

### 3.7 依赖与部署

#### 3.7.1 requirements.txt

```
python-can>=4.0.0
```

#### 3.7.2 PCANBasic.dll 部署

python-can 的 `pcan` 接口在 Windows 下通过 ctypes 加载 `PCANBasic.dll`，要求：

1. PCAN 驱动已安装（用户已装，资料在 `D:\Download\BaiduNetdiskDownload\PCAN上位机软件\PCAN-Basic-Windows`）
2. `PCANBasic.dll` 在系统 PATH 中（驱动安装时通常自动加入 `C:\Program Files\PEAK-System\PCAN-Basic\API\`）

**备选方案**（DLL 不在 PATH 时）：把 `D:\Download\BaiduNetdiskDownload\PCAN上位机软件\PCAN-Basic-Windows\Win64\VC_LIB\PCANBasic.dll`（或 `ARM64/` 下对应版本）复制到项目根目录或 venv 的 `Scripts/` 下，python-can 会自动找到。

#### 3.7.3 未来兼容其他 CAN 硬件

切换到 SocketCAN（Linux）/ Vector / Kvaser 只需在 `CanTransport.open` 中改 `interface=` 参数：

```python
bus = can.Bus(
    interface='socketcan',  # 或 'vector' / 'kvaser'
    channel=channel,
    bitrate=bitrate,
)
```

`list_channels()` 也需对应改为 `can.interface.detect_available_configs('socketcan')`。建议后续在 `CanTransport` 中增加 `_backend` 参数（如 `'pcan'`/`'socketcan'`）从配置读取，UI 通道下拉根据 backend 调用对应枚举函数。

## 四、假设与决策

| 假设/决策 | 说明 |
|---|---|
| 固件侧 CAN 协议已实现或即将实现 | 本方案只做上位机侧；固件 `jm_proto_can.h` 是协议对端，需联调 |
| CAN 帧用 29 位扩展帧 | `is_extended_id=True`，因为仲裁 ID = (CMD << 8) \| motor_id，最大 0xFFFF，超出 11 位 |
| 默认比特率 1 Mbit/s | 与固件 CAN 配置一致；UI 提供可选列表 + 手动输入 |
| 电机 ID 默认 1，范围 0-255 | motor_id=0 表示广播（不应答），UI 允许设 0 |
| MIT 压缩位序按 MIT Cheetah 协议惯例 | 大端、`pos16→vel12→kp12→kd12→tff12` 顺序；**联调前必须对照固件 `jm_proto_can.h` 校准** |
| 反馈帧压缩格式：`pos16 vel16 tq16 temp8 err8` | 8 字节，temp 线性映射 [-40, 215]；具体字段顺序需对照固件 |
| CAN 通道 handle 不持久化 | USB 重新插拔后 handle 可能变化，但 `channel_name`（如 "PCAN_USBBUS1"）稳定，持久化 name 而非 handle |
| python-can 4.x API | `can.Bus(...)`、`bus.recv(timeout=)`、`bus.send(msg, timeout=)`、`bus.shutdown()`、`can.interface.detect_available_configs('pcan')` |
| `CanTransport` 懒导入 | 顶部不导入，仅在 `_on_connect_can` 内 `from transport.can_transport import CanTransport`，避免 python-can 未安装时 main_window 加载失败 |
| 传输类型用 `transport.name` 属性判断 | 避免 isinstance 依赖懒导入的类；基类已定义 `name` 属性 |

## 五、验证步骤

### 5.1 单元测试（无硬件）

1. **MIT 压缩往返**：`_pack_mit(p,v,kp,kd,tf)` → `_unpack_mit(data)` 应在量化误差内还原原值（16 位精度约 1e-4，12 位约 1e-3）
2. **多帧分包重组**：构造 20 字节 payload → `_send_multi` 拆 3 帧（7+7+6）→ 喂给 `_handle_rx_msg` → 应重组出原 payload
3. **单帧收发**：`_send_single(cmd, b'\x01\x02\x03')` → 拦截 `bus.send` 调用 → 验证 `msg.arbitration_id == (cmd << 8) | motor_id`、`msg.data == b'\x01\x02\x03'`、`msg.is_extended_id == True`
4. **通道枚举**：mock `can.interface.detect_available_configs` 返回固定列表 → `list_channels()` 应返回正确 `[(name, desc), ...]`
5. **import 容错**：mock `import can` 失败 → `ConnectionPanel._detect_can_support` 应返回 False，传输类型下拉只有"串口"

### 5.2 集成测试（有 PCAN 硬件 + 固件）

1. **设备枚举**：插上 PCAN-USB → 启动上位机 → 传输类型切到 CAN → 通道下拉应列出 `PCAN_USBBUS1`
2. **链路建立**：选通道 + 1Mbit/s + motor_id=1 → 点连接 → 状态栏显示 `● PCAN_USBBUS1 @1000000 ID=1`
3. **参数读写**：连接后读一个参数（如位置环 KP）→ 应在面板显示值，与 UART 串口下读取结果一致
4. **反馈轮询**：开启反馈轮询 → 反馈面板应显示 pos/vel/torque/temp，值合理（不是 0 或乱码）
5. **遥测**：开启遥测 → 波形面板应实时刷新
6. **MIT 力控**：发 MIT 命令 → 电机应响应（位置/力矩按预期变化）；若不响应或行为异常，**优先排查 MIT 压缩位序与固件是否一致**
7. **断开重连**：点断开 → 状态栏"已断开" → 重新连接 → 应能恢复通信
8. **传输切换**：CAN 连接 → 断开 → 切到串口 → 连接 → 应正常工作；反之亦然
9. **持久化**：关闭程序 → 重启 → 传输类型/bitrate/motor_id 应恢复上次值；CAN 通道名匹配成功则选中上次通道

### 5.3 兼容性验证

1. **python-can 未安装**：卸载 python-can → 启动上位机 → 传输类型下拉只有"串口"，不报错
2. **PCAN 硬件未插**：传输类型切到 CAN → 通道下拉为空 → 点连接应提示"未选择通道"或不响应
3. **PCANBasic.dll 缺失**：模拟 DLL 不在 PATH → 切到 CAN → 通道枚举为空或连接时报错"加载 PCANBasic.dll 失败"

## 六、风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| MIT 压缩位序与固件不一致 | MIT/IMPEDANCE 命令解析错位，电机行为异常或无响应 | 联调前对照固件 `jm_proto_can.h` 严格校准；提供 `_pack_mit/_unpack_mit` 单元测试用固件侧测试向量验证 |
| 多帧分包格式与固件不一致 | 长载荷命令（如参数批量写）失败 | 联调时先用单帧命令（参数读、使能）验证链路，再测多帧；必要时加调试日志打印每帧 ID/DATA |
| python-can 版本差异 | `detect_available_configs` API 在 3.x/4.x 不同 | requirements.txt 锁定 `python-can>=4.0.0`；`list_channels` 加 try/except 兜底返回空列表 |
| PCANBasic.dll 不在 PATH | `can.Bus(interface='pcan')` 抛异常 | `open` 内 try/except → `error_occurred.emit` 提示用户；文档说明 DLL 部署位置 |
| CAN 总线错误（BusOff） | 通信中断 | `bus.state` 检查 + `bus.reset()` 自动恢复；或提示用户重新连接 |
| 与 VirtualTransport 切换冲突 | 状态残留 | `_on_connect_can` 强制调用 `_force_disable_virtual_mode()`，与串口分支一致 |

## 七、实施顺序建议

1. **第一步：CanTransport 核心实现**（不含 MIT）
   - `open/close/is_open/start/stop/send`
   - 后台线程 + 单帧收发
   - `list_channels()` 通道枚举
   - 用单元测试验证单帧往返

2. **第二步：UI + 编排层打通链路**
   - ConnectionPanel 传输类型下拉 + CAN 配置控件
   - MainWindow `_on_connect_can` 分支
   - 持久化字段
   - feature_flags 开关
   - 用真实 PCAN 硬件 + 固件验证参数读写、反馈、遥测

3. **第三步：多帧分包**
   - `_send_multi` + `_handle_rx_msg` 多帧重组
   - 验证长载荷命令（参数批量写、MOTOR_INFO_READ）

4. **第四步：MIT 压缩**
   - `_pack_mit/_unpack_mit/_unpack_mit_feedback/_send_mit`
   - 单元测试用固件测试向量校准位序
   - 真机验证 MIT 力控命令

5. **第五步：错误处理与稳定性**
   - BusOff 自动恢复
   - DLL 缺失/import 失败的优雅降级
   - 长时间通信稳定性测试

每一步完成后即可独立验证，逐步推进降低风险。
