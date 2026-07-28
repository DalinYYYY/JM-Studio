# CAN 单帧路径实施计划（含多帧保留方案）

## 摘要

本计划基于 [can_feature_full_planning.md](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\.trae\documents\can_feature_full_planning.md) 的全面规划，聚焦**本次实施范围**：所有单帧路径功能 + 兼容性修复 + 调试增强，**多帧路径增强方案保留不实现**（但完整给出方案供后续实施）。

**实施范围（用户已确认）**：
- P0-1 物理帧日志（raw_can_frame 信号）
- P0-2 错误帧/远程帧日志
- P0-4 发送失败告警（单帧部分）
- P0-5 motor_id 接收过滤
- **P0-6 多帧白名单补全（兼容性修复，纳入）**
- P0-7 ACK/NACK CAN 模式看门狗适配
- P1-1 总线状态指示（ERROR_ACTIVE/PASSIVE/BUS_OFF）
- P1-2 错误计数器显示（TEC/REC）
- P1-3 总线占用率估算
- P2-1 CAN 原始监听模式
- P2-2 手动发送原始 CAN 帧
- P2-3 硬件时间戳利用
- P2-4 比特率热切换
- P2-5 反馈面板 CAN 模式提示

**保留方案不实现（多帧路径增强）**：
- P0-3 多帧重组超时清理
- P0-4 多帧发送中断
- P1-4 CAN 状态独立面板（多帧队列状态部分）

---

## 一、现状分析

### 1.1 已具备的能力（探索确认）

| 能力 | 位置 | 状态 |
|---|---|---|
| Transport 抽象基类 | [base.py:12-58](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\base.py#L12-L58) | 3 信号 `frame_received/connected/error_occurred` + 6 接口，OK |
| CanTransport 收发 | [can_transport.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py) | 双标志+单锁线程模型，OK |
| MIT 压缩/解压 | [can_transport.py:244-315](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L244-L315) | 8B↔22B 反馈帧解压伪装，JmClient 零改动，OK |
| 传输热切换 | [motor_client.py:60-84](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\core\motor_client.py#L60-L84) | `set_transport()` 重连 3 个核心信号，UI 透明，OK |
| CAN 连接入口 | [main_window.py:990-1003](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L990-L1003) | 懒导入 + 热切换，OK |
| CAN 配置区 UI | [connection_panel.py:65-92](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\connection_panel.py#L65-L92) | 通道/比特率/电机ID，OK |
| CAN 设置持久化 | [main_window.py:476-573](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L476-L573) | ui_layout.json 存取 + 100ms 延迟套用通道名，OK |
| 状态栏流量统计 | [main_window.py:689-698](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L689-L698) | 直接读 `transport.tx_bytes/rx_bytes`，传输无关，OK |

### 1.2 缺失清单（本次需实现）

**日志可观测性**：
- L1 看不到 CAN ID/DLC/motor_id（[log_panel.py:218-224](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\log_panel.py#L218-L224) 只显示逻辑帧）
- L3 错误帧/远程帧静默丢弃（[can_transport.py:328-329](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L328-L329) `continue`）
- L4 发送失败无日志（[can_transport.py:231-241](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L231-L241) `except: return False`）

**兼容性 bug**：
- C1 `_MULTI_FRAME_CMDS` 只含 0xE6（[can_transport.py:51-55](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L51-L55)），TELEMETRY/DEV_INFO 等应答被截断
- C2/C3 ACK/NACK 看门狗 CAN 模式策略（[main_window.py:100-103](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L100-L103) 1s 超时）
- F1 未按 motor_id 过滤（[can_transport.py:348-371](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L348-L371)）

**总线状态可视化**：
- B1 无占用率、B2 无 TEC/REC、B3 无 ERROR_ACTIVE/PASSIVE/BUS_OFF 指示

**调试便利性**：
- D1 无原始监听模式、D2 无手动发帧、D3 未用硬件时间戳
- U2 反馈面板无 CAN 模式提示
- P2-4 比特率需手动断开重连

---

## 二、实施方案（按文件组织）

### 改动 1：`transport/base.py` — 新增 raw_can_frame 信号

**what**：Transport 基类新增 `raw_can_frame` 信号，供 CanTransport 发射物理帧日志；SerialTransport 不发射，串口零影响。

**why**：CAN 调试必须看到物理帧 ID/DLC/data，逻辑帧日志（cmd+payload）无法定位总线层问题。信号放基类是为了让 `JmClient.set_transport` 切换传输时能统一断连重连。

**how**：在 [base.py:12-23](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\base.py#L12-L23) 信号区追加：
```python
# CAN 物理帧日志 (can_id, data, is_rx, timestamp); 仅 CanTransport 发射
raw_can_frame = pyqtSignal(int, bytes, bool, float)
```

### 改动 2：`transport/can_transport.py` — 核心改造（7 项）

**文件**：[can_transport.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py)

#### 2.1 P0-6 补全 `_MULTI_FRAME_CMDS` 白名单（兼容性修复）

**位置**：[can_transport.py:51-55](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L51-L55)

**what**：补全白名单为：
```python
_MULTI_FRAME_CMDS = frozenset({
    JmCmd.MOTOR_INFO_READ,        # 0xE6: motor_info 单读应答(可能>8B)
    JmCmd.MOTOR_INFO_READ_BULK,   # 0xE8: 批量读应答(3+4N>8B)
    JmCmd.MOTOR_INFO_WRITE_BULK,  # 0xE9: 批量写应答(可能回显)
    JmCmd.TELEMETRY,              # 0xCA: 遥测帧(mask多时>8B)
    JmCmd.READ_DEV_INFO,          # 0xD0: 设备信息(20B)
    JmCmd.READ_DEV_NAME,          # 0xD1: 设备名称(变长字符串)
    JmCmd.PARAM_READ,             # 0xE0: 参数读应答(字符串参数可能>8B)
})
```

**why**：当前只含 MOTOR_INFO_READ，导致 CAN 模式下遥测/设备信息/批量参数读写/设备名称等应答被当单帧处理，数据截断。

**风险与缓解**：白名单过宽可能导致短帧被误判为多帧（首帧 ctrl=0x00 加入缓冲等末帧，永远收不到末帧→缓冲泄漏）。先按"固件 CAN 侧对白名单命令统一走多帧格式"假设补全，联调验证。若短应答被误判，回退到只保留确定 >8B 的命令。

#### 2.2 P0-1/P0-2 物理帧日志 + 错误帧/远程帧日志

**位置**：[can_transport.py:318-346](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L318-L346) `_run` 方法 + [can_transport.py:348-371](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L348-L371) `_handle_rx_msg`

**what**：
1. `_run` 中删除 [行 328-329](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L328-L329) 的静默 `continue`，错误帧 emit `error_occurred`，远程帧 emit `raw_can_frame`
2. `_handle_rx_msg` 开头（解析前）emit `raw_can_frame`，让所有物理帧都进日志
3. `_write_msg` 发送成功后 emit `raw_can_frame`

**how**：
```python
# _run 中, 替换行 328-329:
if msg.is_error_frame:
    self.error_occurred.emit(f"CAN 错误帧 ID:0x{msg.id:X} DLC:{msg.dlc}")
    self.rx_frames += 1
    continue
if msg.is_remote_frame:
    self.raw_can_frame.emit(int(msg.id), b'', True, msg.timestamp or 0.0)
    self.rx_frames += 1
    continue

# _handle_rx_msg 开头追加:
def _handle_rx_msg(self, msg: can.Message):
    can_id = int(msg.id)
    cmd = can_get_cmd(can_id)
    data = bytes(msg.data[:msg.dlc])
    # P0-1: 物理帧日志(在过滤前发射, 让所有帧都进日志)
    self.raw_can_frame.emit(can_id, data, True, msg.timestamp or 0.0)
    # ... 后续解析逻辑 ...

# _write_msg 成功分支追加:
self._bus.send(msg, timeout=self._WRITE_TIMEOUT_S)
self.tx_bytes += msg.dlc
self.tx_frames += 1
self.raw_can_frame.emit(int(msg.id), bytes(msg.data[:msg.dlc]), False, 0.0)
return True
```

#### 2.3 P0-4 发送失败告警

**位置**：[can_transport.py:231-241](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L231-L241) `_write_msg`

**what**：`except` 分支 emit `error_occurred`
```python
except Exception as e:
    self.error_occurred.emit(f"CAN 发送失败: {e}")
    return False
```

#### 2.4 P0-5 motor_id 接收过滤

**位置**：[can_transport.py:348-371](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L348-L371) `_handle_rx_msg`

**what**：在 emit `raw_can_frame` 之后、解析之前，按 motor_id 过滤：
```python
# P0-5: motor_id 过滤(非本机非广播不解析, 但物理帧日志已发射)
rx_motor_id = can_get_motor_id(can_id)
if rx_motor_id != 0 and rx_motor_id != self._motor_id:
    return
```

**why**：多电机总线场景下，其他电机应答帧不应污染本机反馈面板。广播帧（motor_id=0）放行。

#### 2.5 P1-1/P1-2 总线状态 + 错误计数器

**what**：
1. CanTransport 新增 `bus_state_changed = pyqtSignal(str, int, int)` 信号 `(state_str, tec, rec)`
2. 新增 `get_bus_state() -> tuple` 方法，读取 `bus.state` 和 `bus.get_stats()`
3. `_run` 收循环每 500ms 读一次状态，变化时 emit
4. BUS_OFF 时额外 emit `error_occurred`

**how**：
```python
# 类信号区追加:
bus_state_changed = pyqtSignal(str, int, int)  # state, tec, rec

# __init__ 追加:
self._last_bus_state = ""
self._bus_state_check_ts = 0.0

# _run 循环内, recv 返回 None 的空闲分支追加:
now = time.time()
if now - self._bus_state_check_ts > 0.5:
    self._bus_state_check_ts = now
    self._check_bus_state()

# 新增方法:
def _check_bus_state(self):
    try:
        with self._io_lock:
            if self._bus is None or not self._running:
                return
            state = self._bus.state  # BusState 枚举
            stats = None
            try:
                stats = self._bus.get_stats(timeout=0)
            except Exception:
                pass  # 不支持 get_stats 的接口回退到只显示 state
        state_str = str(state).split('.')[-1] if state else "UNKNOWN"
        tec = stats.tec if stats else 0
        rec = stats.rec if stats else 0
        if state_str != self._last_bus_state:
            self._last_bus_state = state_str
            if state_str == "BUSOFF":
                self.error_occurred.emit("CAN 总线 BUS_OFF")
            self.bus_state_changed.emit(state_str, tec, rec)
    except Exception:
        pass

def get_bus_state(self) -> tuple:
    """供 UI 主动查询当前总线状态"""
    return (self._last_bus_state, 0, 0)
```

**风险**：`bus.state` 读取在某些 PCAN 驱动状态下可能慢，故只在收循环空闲周期读取，不阻塞 recv。

#### 2.6 P2-1 监听模式 + P2-2 手动发帧 + P1-3 bitrate 属性

**what**：
1. 新增 `set_listen_only(bool)` 方法，`_handle_rx_msg` 在 emit `raw_can_frame` 后判断 `_listen_only`，True 则 return（不解析）
2. 新增 `send_raw(can_id, data, extended=True) -> bool` 方法，构造 `can.Message` 走 `_write_msg`
3. 新增 `bitrate` 只读属性，供 UI 算占用率

**how**：
```python
# __init__ 追加:
self._listen_only = False

def set_listen_only(self, enabled: bool):
    self._listen_only = enabled

# _handle_rx_msg 中, raw_can_frame emit 之后, motor_id 过滤之前追加:
if self._listen_only:
    return  # 监听模式: 只记物理帧日志, 不解析

@property
def bitrate(self) -> int:
    return self._bitrate

def send_raw(self, can_id: int, data: bytes, extended: bool = True) -> bool:
    msg = can.Message(
        arbitration_id=can_id,
        data=bytearray(data[:8]),
        is_extended_id=extended,
    )
    return self._write_msg(msg)
```

### 改动 3：`ui/panels/log_panel.py` — CAN 物理帧日志方法

**文件**：[log_panel.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\log_panel.py)

**what**：
1. 新增 `log_can_frame(can_id, data, is_rx, ts)` 方法，格式 `▼ CAN RX ID:0xC001 DLC:8 8A 3D ...`
2. 新增"CAN 原始帧"开关复选框（默认开），关闭时不显示物理帧
3. P2-3 时间戳：ts 格式化为 `HH:MM:SS.mmm`（用 `time.time()` 整数秒 + ts 小数部分）

**how**：在 [log_panel.py:210-224](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\log_panel.py#L210-L224) `log_tx/log_rx` 之后追加：
```python
def log_can_frame(self, can_id: int, data: bytes, is_rx: bool, ts: float):
    """CAN 物理帧日志。ts 为 python-can 提供的硬件时间戳(相对值)。"""
    if not self.chk_can_raw.isChecked():
        return
    arrow = "▼ RX" if is_rx else "▲ TX"
    motor_id = can_id & 0xFF
    cmd = (can_id >> 8) & 0xFF
    # 时间戳: 用系统时间整数秒 + 硬件 ts 小数部分
    now = time.time()
    ts_combined = int(now) + (ts - int(ts)) if ts > 0 else now
    ts_str = time.strftime("%H:%M:%S", time.localtime(ts_combined)) + f".{int((ts_combined % 1) * 1000):03d}"
    hex_str = data.hex(' ').upper() if data else "(空)"
    line = (f'<span style="color:#9E9E9E">{ts_str}</span> '
            f'<span style="color:#{"00897B" if is_rx else "2962FF"}">{arrow} CAN</span> '
            f'ID:0x{can_id:X} DLC:{len(data)} CMD:0x{cmd:02X} MID:{motor_id} '
            f'<span style="color:#9E9E9E">{html.escape(hex_str)}</span>')
    self._queue('raw', line)
```

并在 `_build` 工具栏区追加复选框：
```python
self.chk_can_raw = QCheckBox("CAN 原始帧")
self.chk_can_raw.setChecked(True)
```

### 改动 4：`ui/panels/connection_panel.py` — 监听模式 + 比特率热切换

**文件**：[connection_panel.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\connection_panel.py)

**what**：
1. P2-1 新增"监听模式"复选框 + `listen_only_changed(bool)` 信号
2. P2-4 CAN 已连接时改比特率，触发 `bitrate_changed(int)` 信号（不禁用下拉）

**how**：在 [connection_panel.py:17-19](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\connection_panel.py#L17-L19) 信号区追加：
```python
bitrate_changed = pyqtSignal(int)           # P2-4: CAN 比特率热切换
listen_only_changed = pyqtSignal(bool)      # P2-1: 监听模式
```

在 CAN 配置区（[行 65-92](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\connection_panel.py#L65-L92)）追加监听模式复选框（放第 6 行）：
```python
self._chk_listen_only = QCheckBox("监听模式(只看帧不解析)")
self._chk_listen_only.toggled.connect(self.listen_only_changed.emit)
can_layout.addWidget(self._chk_listen_only, 6, 0, 1, 3)
```

比特率热切换：[行 80-85](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\connection_panel.py#L80-L85) `_combo_can_bitrate.currentTextChanged` 连接：
```python
self._combo_can_bitrate.currentTextChanged.connect(
    lambda text: self.bitrate_changed.emit(int(text)) if text.isdigit() and self._connected else None)
```

`set_connected` 中**不再禁用** `_combo_can_bitrate`（允许热切换），但仍禁用通道/电机ID。

### 改动 5：`ui/panels/can_send_dialog.py`（新建）— 手动发帧对话框

**what**：P2-2 新建对话框，输入 CAN ID（十六进制）+ DLC + 8 字节 Data + 扩展帧复选框，调用 `client.transport.send_raw()`

**how**：
```python
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QLineEdit,
                              QCheckBox, QDialogButtonBox, QLabel, QHBoxLayout)
from PyQt6.QtGui import QIntValidator
from PyQt6.QtCore import pyqtSignal

class CanSendDialog(QDialog):
    send_requested = pyqtSignal(int, bytes, bool)  # can_id, data, extended

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CAN 手动发帧")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._id_edit = QLineEdit("C001")
        self._id_edit.setMaxLength(8)
        form.addRow("CAN ID (hex):", self._id_edit)
        self._data_edits = []
        for i in range(8):
            edit = QLineEdit("00")
            edit.setMaxLength(2)
            edit.setFixedWidth(32)
            self._data_edits.append(edit)
        data_row = QHBoxLayout()
        for edit in self._data_edits:
            data_row.addWidget(edit)
        data_row.addStretch()
        form.addRow("Data (hex):", data_row)
        self._chk_extended = QCheckBox("扩展帧 (29-bit)")
        self._chk_extended.setChecked(True)
        form.addRow("", self._chk_extended)
        layout.addLayout(form)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _on_ok(self):
        try:
            can_id = int(self._id_edit.text(), 16)
            data = bytes(int(e.text(), 16) for e in self._data_edits if e.text().strip())
            self.send_requested.emit(can_id, data, self._chk_extended.isChecked())
            self.accept()
        except ValueError:
            self._id_edit.setFocus()
```

### 改动 6：`ui/panels/feedback_panel.py` — CAN 模式提示

**文件**：[feedback_panel.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\feedback_panel.py)

**what**：P2-5 新增 `set_can_mode(bool)` 方法，CAN 模式下在轮询复选框旁显示提示文字

**how**：在 `_build_poll_bar`（[行 1003-1040](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\feedback_panel.py#L1003-L1040)）追加一个 QLabel：
```python
self._lbl_can_hint = QLabel("CAN 模式: 电机可能主动推送反馈, 轮询可关闭")
self._lbl_can_hint.setStyleSheet("color: #F9A825; font-size: 11px;")
self._lbl_can_hint.setVisible(False)
# 加入 poll_bar 布局

def set_can_mode(self, enabled: bool):
    self._lbl_can_hint.setVisible(enabled)
```

### 改动 7：`ui/main_window.py` — 状态栏 + 信号连接 + 看门狗

**文件**：[main_window.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py)

#### 7.1 状态栏扩展（P1-1/P1-2/P1-3）

**位置**：[main_window.py:407-424](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L407-L424) `_build_statusbar`

**what**：新增 `_sb_bus_state`（BUS 状态+TEC/REC）和 `_sb_load`（占用率）两个标签
```python
self._sb_bus_state = _mk("")      # CAN 模式显示, 串口模式空
self._sb_load = _mk("")           # CAN 模式显示占用率
# 顺序: _sb_link, _sb_bus_state, _sb_tx, _sb_rx, _sb_frames, _sb_load
for w in (self._sb_link, self._sb_bus_state, self._sb_tx, self._sb_rx, self._sb_frames, self._sb_load):
    sb.addPermanentWidget(w)
```

#### 7.2 信号连接 + 传输切换重连

**位置**：[main_window.py:868-958](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L868-L958) `_connect_signals`

**what**：
1. 连接 `raw_can_frame` → `log_panel.log_can_frame`
2. 连接 `bus_state_changed` → `_on_bus_state_changed`
3. 连接 `connect_can_requested` 分支已有（[行 894](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L894)）
4. 连接 `bitrate_changed` → `_on_can_bitrate_changed`
5. 连接 `listen_only_changed` → `_on_listen_only_changed`
6. 新增 `_reconnect_can_signals()` 方法，在 `set_transport` 后调用

**how**：
```python
def _connect_signals(self):
    c = self._client
    # ... 现有连接 ...
    # CAN 专用信号(传输切换时需重连)
    self._reconnect_can_signals()
    # connection_panel CAN 增强信号
    self._conn_panel.bitrate_changed.connect(self._on_can_bitrate_changed)
    self._conn_panel.listen_only_changed.connect(self._on_listen_only_changed)

def _reconnect_can_signals(self):
    """传输切换后重连 CAN 专用信号。串口传输发射这些信号无效, 连接无害。"""
    tp = self._client.transport
    try:
        tp.raw_can_frame.disconnect()
    except TypeError:
        pass
    try:
        tp.bus_state_changed.disconnect()
    except TypeError:
        pass
    tp.raw_can_frame.connect(self._log_panel.log_can_frame)
    tp.bus_state_changed.connect(self._on_bus_state_changed)
```

在 `_on_connect_can`（[行 990-1003](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L990-L1003)）和 `_on_connect`（[行 961-988](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L961-L988)）中，`set_transport` 之后调用 `_reconnect_can_signals()`。

#### 7.3 状态栏更新（P1-1/P1-2/P1-3）

**位置**：[main_window.py:689-698](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L689-L698) `_on_stats_tick`

**what**：CAN 模式下额外更新 BUS 状态和占用率
```python
def _on_stats_tick(self):
    tp = self._client.transport
    dt = self._stats_period / 1000.0
    tx_rate = (tp.tx_bytes - self._last_tx_bytes) / dt
    rx_rate = (tp.rx_bytes - self._last_rx_bytes) / dt
    self._last_tx_bytes = tp.tx_bytes
    self._last_rx_bytes = tp.rx_bytes
    self._sb_tx.setText(f"TX {self._fmt_rate(tx_rate)}  Σ{self._fmt_bytes(tp.tx_bytes)}")
    self._sb_rx.setText(f"RX {self._fmt_rate(rx_rate)}  Σ{self._fmt_bytes(tp.rx_bytes)}")
    self._sb_frames.setText(f"帧 TX:{tp.tx_frames} RX:{tp.rx_frames}")
    # CAN 专用: 占用率估算
    if tp.name == "can":
        # 估算: 每帧约 (47 + 8*DLC + 15) bits, DLC 平均按 8 算
        total_frames = tp.tx_frames + tp.rx_frames
        # 本次周期增量帧数
        delta_frames = total_frames - getattr(self, '_last_total_frames', 0)
        self._last_total_frames = total_frames
        est_bits = delta_frames * (47 + 8 * 8 + 15)  # 70 bits/帧
        bitrate = getattr(tp, 'bitrate', 1000000)
        load_pct = est_bits / (bitrate * dt) * 100
        load_text = f"LOAD: {load_pct:.1f}%"
        color = "#E53935" if load_pct > 70 else ("#F9A825" if load_pct > 40 else "#616161")
        self._sb_load.setText(load_text)
        self._sb_load.setStyleSheet(f"font-family: Consolas; padding: 0 8px; color: {color};")
    else:
        self._sb_load.setText("")
        self._sb_bus_state.setText("")

def _on_bus_state_changed(self, state: str, tec: int, rec: int):
    color_map = {"ERRORACTIVE": "#43A047", "PASSIVE": "#F9A825", "BUSOFF": "#E53935"}
    color = color_map.get(state, "#616161")
    self._sb_bus_state.setText(f"BUS:{state} TEC:{tec} REC:{rec}")
    self._sb_bus_state.setStyleSheet(f"font-family: Consolas; padding: 0 8px; color: {color};")
```

#### 7.4 P0-7 看门狗 CAN 模式适配

**位置**：[main_window.py:100-103](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L100-L103) + [main_window.py:1066-1081](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L1066-L1081) `_on_connected`

**what**：CAN 模式下看门狗间隔延长到 5s（保守策略，待联调确认固件 ACK 行为后调整）

**how**：在 `_on_connected` 中追加：
```python
def _on_connected(self, connected: bool):
    # ... 现有逻辑 ...
    if connected:
        # P0-7: CAN 模式看门狗策略
        if self._client.transport.name == "can":
            self._cmd_watchdog.setInterval(5000)  # CAN: 5s (待联调确认)
        else:
            self._cmd_watchdog.setInterval(1000)  # 串口: 1s
```

#### 7.5 P2-2 菜单加"CAN 发帧"入口

**what**：菜单栏加"工具"菜单，含"CAN 手动发帧"动作，仅 CAN 连接时启用
```python
def _build_menu(self):
    menubar = self.menuBar()
    tool_menu = menubar.addMenu("工具")
    self._act_can_send = QAction("CAN 手动发帧", self)
    self._act_can_send.triggered.connect(self._on_can_send)
    tool_menu.addAction(self._act_can_send)

def _on_can_send(self):
    if self._client.transport.name != "can":
        QMessageBox.information(self, "提示", "请先连接 CAN 设备")
        return
    from ui.panels.can_send_dialog import CanSendDialog
    dlg = CanSendDialog(self)
    dlg.send_requested.connect(
        lambda cid, data, ext: self._client.transport.send_raw(cid, data, ext))
    dlg.exec()
```

#### 7.6 P2-4 比特率热切换 + P2-1 监听模式

**what**：
```python
def _on_can_bitrate_changed(self, bitrate: int):
    """CAN 已连接时改比特率: 断开并以新比特率重连。"""
    if self._client.transport.name != "can" or not self._client.is_open():
        return
    channel = self._conn_panel.get_can_channel_name()
    motor_id = self._conn_panel.get_can_motor_id()
    self._client.close()
    if self._client.open(channel=channel, bitrate=bitrate, motor_id=motor_id):
        self.statusBar().showMessage(f"已重连 {channel} @{bitrate}", 2000)

def _on_listen_only_changed(self, enabled: bool):
    if self._client.transport.name == "can":
        self._client.transport.set_listen_only(enabled)
```

#### 7.7 P2-5 反馈面板 CAN 提示

**位置**：`_on_connected`（[行 1066-1081](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L1066-L1081)）

**what**：
```python
if connected:
    is_can = (self._client.transport.name == "can")
    self._feedback_panel.set_can_mode(is_can)
```

---

## 三、多帧路径增强方案（保留，本次不实现）

> 用户要求"多帧的方案要给我"，以下方案完整给出，本次不实现。

### 保留项 P0-3：多帧重组超时清理

**问题**：[can_transport.py:373-389](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L373-L389) `_handle_multi_frame` 中 `_rx_segments` 缓冲无超时清理，末帧丢失时内存泄漏。

**方案**：
```python
# __init__ 中 _rx_segments 改为存 (buf, last_ts):
self._rx_segments: dict[int, tuple[bytearray, float]] = {}

# _handle_multi_frame 开头清理超时项 (>500ms):
def _handle_multi_frame(self, can_id: int, cmd: int, data: bytes):
    now = time.time()
    # 清理超时缓冲
    expired = [k for k, (buf, ts) in self._rx_segments.items() if now - ts > 0.5]
    for k in expired:
        old_buf, _ = self._rx_segments.pop(k, (b'', 0))
        self.error_occurred.emit(f"多帧重组超时 can_id=0x{k:X}, 丢弃 {len(old_buf)}B")

    if len(data) == 0:
        return
    ctrl = data[0]
    seg = data[1:1 + CAN_SEG_PAYLOAD]
    is_last = bool(ctrl & CAN_SEG_LAST)
    if not is_last:
        entry = self._rx_segments.get(can_id)
        if entry is None:
            self._rx_segments[can_id] = (bytearray(seg), now)
        else:
            buf, _ = entry
            buf.extend(seg)
            self._rx_segments[can_id] = (buf, now)
        return
    buf, _ = self._rx_segments.pop(can_id, (bytearray(), 0))
    buf.extend(seg)
    payload = bytes(buf)
    self.rx_frames += 1
    self.frame_received.emit(cmd, payload)
```

**不实现原因**：属多帧路径增强。

### 保留项 P0-4：多帧发送中断

**问题**：[can_transport.py:209-229](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L209-L229) `_send_multi` 中途帧失败仍继续发后续帧。

**方案**：
```python
def _send_multi(self, cmd: int, payload: bytes) -> bool:
    can_id = can_make_id(cmd, self._motor_id)
    total = len(payload)
    offset = 0
    seq = 0
    sent_frames = 0
    while offset < total:
        chunk = payload[offset:offset + CAN_SEG_PAYLOAD]
        is_last = (offset + CAN_SEG_PAYLOAD) >= total
        ctrl = (CAN_SEG_LAST if is_last else 0) | (seq & CAN_SEG_SEQ_MASK)
        data = bytes([ctrl]) + chunk
        msg = can.Message(arbitration_id=can_id, data=bytearray(data),
                          is_extended_id=True)
        if not self._write_msg(msg):
            # 多帧发送中断: 告知已发帧数
            self.error_occurred.emit(
                f"CAN 多帧发送中断 cmd=0x{cmd:02X}, 已发 {sent_frames}/{(total+6)//7} 帧")
            return False
        sent_frames += 1
        offset += CAN_SEG_PAYLOAD
        seq = (seq + 1) & CAN_SEG_SEQ_MASK
    return True
```

**不实现原因**：属多帧路径增强。

### 保留项 P1-4：CAN 状态独立面板

**问题**：状态栏空间有限，深度调试需多帧重组队列状态等详细信息。

**方案**：新建 `ui/panels/can_status_panel.py`，作为独立 Tab 显示：
- 总线状态（ERROR_ACTIVE/PASSIVE/BUS_OFF）
- TEC/REC 错误计数器
- 总线占用率（实时曲线）
- 最近 N 条错误帧记录
- **多帧重组队列状态**（每个 can_id 的缓冲字节数、首帧时间戳、等待时长）
- 流量统计（TX/RX 帧数、字节速率）

**不实现原因**：含多帧队列状态展示，属多帧路径增强。

---

## 四、假设与决策

1. **python-can API 假设**：
   - `bus.state` 返回 `BusState` 枚举，PCAN 接口支持
   - `bus.get_stats(timeout=0)` 在 PCAN 接口的支持需联调验证，不支持的回退到只显示 state（TEC/REC 显示 0）
   - `msg.timestamp` 是硬件时间戳（PCAN USB 支持，精度到微秒），是相对打开总线的秒数

2. **向后兼容**：
   - `raw_can_frame` 信号加在 Transport 基类但 SerialTransport 不发射，串口路径零影响
   - log_panel 新增方法不破坏现有 `log_tx/log_rx` 接口
   - 所有 CAN 增强功能通过 `transport.name == "can"` 判断，串口路径不受影响
   - `_reconnect_can_signals` 对串口传输也调用（连接无害，串口不发射这些信号）

3. **信号重连**：
   - `JmClient.set_transport` 只重连 3 个核心信号，新增的 `raw_can_frame/bus_state_changed` 由 MainWindow 的 `_reconnect_can_signals()` 统一处理
   - 在 `_on_connect` 和 `_on_connect_can` 中 `set_transport` 之后调用

4. **P0-6 白名单决策**（用户已确认纳入）：
   - 先按"固件 CAN 侧对白名单命令统一走多帧格式"假设补全
   - 联调时验证：若短应答被误判为多帧（缓冲泄漏），回退到只保留确定 >8B 的命令

5. **P0-7 ACK 决策**：
   - 本次先实现"CAN 模式延长看门狗到 5s"的保守策略
   - 联调确认固件行为后，若回 ACK 则恢复 1s，若不回则改为发后即忘

6. **P1-3 占用率估算**：
   - 按每帧 70 bits 估算（47 bit 帧头 + 64 bit 数据 + 15 bit 帧尾），DLC 平均按 8 算
   - 估算值仅供参考，不精确

7. **不修改协议层**：所有改动都在 transport 层和 UI 层，jmproto/JmClient 零改动。

---

## 五、验证步骤

### 兼容性验证
1. **P0-6 白名单**：CAN 模式下发 READ_DEV_INFO，应收到完整 20B 载荷（多帧重组），设备信息面板显示正确
2. **P0-6 白名单**：CAN 模式下发 TELEMETRY（mask 含多组），应收到完整遥测帧
3. **P0-7 ACK**：CAN 模式下发 PARAM_WRITE，观察是否回 ACK，看门狗是否误触发

### 单帧路径验证
4. **P0-1 日志**：发 ID=0xC001 DLC=8 反馈帧，log_panel 显示物理帧 `▼ CAN RX ID:0xC001 DLC:8 ...` + 逻辑帧 `▼ RX READ_FEEDBACK(0xC0) [22B] ...`
5. **P0-2 错误帧**：发错误帧，log_panel 显示 `✖ CAN 错误帧 ID:0xXXX`
6. **P0-4 发送失败**：拔掉 PCAN USB 后点连接，应弹错误
7. **P0-5 motor_id 过滤**：发 ID=0xC002（motor_id=2），log_panel 只记物理帧不触发反馈面板
8. **P1-1 总线状态**：短路 CAN_H/CAN_L，状态栏变红 BUS_OFF
9. **P1-2 TEC/REC**：错误累积时计数器递增
10. **P1-3 占用率**：高频发送时 LOAD% 上升，>70% 变黄
11. **P2-1 监听模式**：开启后只看物理帧，反馈面板不更新
12. **P2-2 手动发帧**：dialog 输入 ID=0xC001 Data=8A 3D 93 B1 8C CC 64 00，反馈面板更新
13. **P2-3 时间戳**：CAN 帧日志时间戳精度到毫秒
14. **P2-4 比特率热切换**：CAN 已连接时改比特率，自动断开重连
15. **P2-5 反馈提示**：CAN 模式下反馈面板显示提示文字

### 回归验证（串口不受影响）
16. 串口连接全功能正常，状态栏不显示 BUS/LOAD 标签
17. 串口模式下 log_panel 不显示 CAN 物理帧日志

---

## 六、风险

1. **P0-6 白名单误判**：若固件对 ≤8B 应答走单帧、>8B 走多帧，白名单会导致短应答被误判为多帧（缓冲泄漏）。缓解：联调验证，必要时回退。
2. **P0-7 ACK 行为未知**：固件 CAN 侧是否回应用层 ACK 需联调确认。本次保守延长看门狗到 5s。
3. **python-can PCAN 接口 get_stats 支持不确定**：若不支持 TEC/REC，P1-2 降级为只显示 state。
4. **bus.state 读取可能阻塞**：PCAN 驱动在某些状态下读取 state 可能慢，需在收循环空闲周期读取（已设计为 500ms 间隔）。
5. **raw_can_frame 高频信号积压**：1Mbps 满载约 8000 帧/s，Qt 信号槽可能积压。log_panel 已有批量刷新兜底（50ms 刷新，每次最多 200 行）。
6. **set_transport 信号重连遗漏**：切换传输时若忘记重连 raw_can_frame/bus_state_changed，CAN 日志丢失。已封装 `_reconnect_can_signals()` 统一处理。
7. **比特率热切换丢数据**：重连期间正在传输的帧会丢失。可接受（调试场景）。

---

## 七、实施顺序

1. `transport/base.py`：加 `raw_can_frame` 信号
2. `transport/can_transport.py`：
   - 补全 `_MULTI_FRAME_CMDS` 白名单（P0-6）
   - emit raw_can_frame（RX/TX）（P0-1）
   - 错误帧/远程帧日志（P0-2）
   - 发送失败告警（P0-4）
   - motor_id 过滤（P0-5）
   - bus_state_changed 信号 + _check_bus_state 方法（P1-1/P1-2）
   - set_listen_only / send_raw 方法（P2-1/P2-2）
   - 暴露 bitrate 属性（P1-3）
3. `ui/panels/log_panel.py`：log_can_frame 方法 + CAN 原始帧开关（P0-1/P2-3）
4. `ui/panels/connection_panel.py`：监听模式复选框 + bitrate_changed 信号（P2-1/P2-4）
5. `ui/panels/can_send_dialog.py`（新建）：手动发帧对话框（P2-2）
6. `ui/panels/feedback_panel.py`：CAN 模式提示（P2-5）
7. `ui/main_window.py`：
   - 状态栏加 `_sb_bus_state` / `_sb_load` 标签（P1-1/P1-3）
   - `_connect_signals` 连接 raw_can_frame/bus_state_changed + bitrate_changed/listen_only_changed
   - `_reconnect_can_signals()` 在 set_transport 后调用
   - `_on_stats_tick` 加 CAN 状态/占用率更新（P1-1/P1-2/P1-3）
   - `_on_bus_state_changed` 槽（P1-1/P1-2）
   - `_on_connected` CAN 模式延长看门狗 + 反馈面板提示（P0-7/P2-5）
   - 菜单加"CAN 发帧"入口 + `_on_can_send`（P2-2）
   - `_on_can_bitrate_changed` / `_on_listen_only_changed` 槽（P2-4/P2-1）
8. 语法检查 + 启动验证（不连硬件应能正常启动，CAN 选项出现）
