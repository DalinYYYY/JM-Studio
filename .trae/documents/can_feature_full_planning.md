# CAN 功能全面规划（含 UART 兼容性分析）

## 摘要

当前 CAN 传输实现（`can_transport.py`）已具备基础收发能力，但在**CAN/UART 协议兼容性、UI 兼容性、日志可观测性、错误感知、总线状态可视化、调试便利性**等方面存在多处缺失。本规划全面梳理发现的问题，按优先级分阶段（P0 修复/P1 状态可视化/P2 调试增强）给出方案。

**本次实施范围**：单帧路径相关功能 + 兼容性修复（P0-6 多帧白名单补全属兼容性修复，必须做）。多帧路径增强（超时清理/发送中断/队列状态展示）保留方案不实现。

---

## 零、CAN/UART 兼容性分析

### 0.1 协议层兼容性（已确认 OK）

| 维度 | UART | CAN | 兼容性 |
|---|---|---|---|
| 帧格式 | 帧头+len+cmd+payload+CRC16+帧尾 | 仲裁ID(cmd+motor_id)+data | CanTransport.send/recv 已做格式转换，jmproto 只看 (cmd,payload) 逻辑帧，OK |
| MIT 命令 | payload=5×float32=20B | data=8B 压缩 | [registry.py:220-222](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\jmproto\registry.py#L220-L222) 打包 5 float，[can_transport.py:304-315](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L304-L315) `_send_mit` 解包后压缩，OK |
| 反馈帧 | READ_FEEDBACK 22B | 8B 压缩 | [can_transport.py:284-302](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L284-L302) `_unpack_mit_feedback` 解压为 22B 兼容载荷，OK |
| CRC | 软件 CRC16-XMODEM | 硬件 CRC | CAN 硬件保证，无需软件 CRC，OK |

### 0.2 协议层兼容性问题（需修复）

#### C1：_MULTI_FRAME_CMDS 白名单严重不足（P0 兼容性 bug）

- **现状**：[can_transport.py:53-55](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L53-L55) `_MULTI_FRAME_CMDS = frozenset({JmCmd.MOTOR_INFO_READ})`，只含 0xE6
- **问题**：以下命令应答载荷可能 >8 字节，但不在白名单中，CAN 模式下会被当作单帧处理，**数据截断**：

| 命令 | 应答载荷 | 最小字节 | 是否 >8B | 当前处理 |
|---|---|---|---|---|
| TELEMETRY(0xCA) | mask(u16)+按位序数据 | 2+4~50 | 是（mask 多时）| **截断** |
| READ_DEV_INFO(0xD0) | hw(4)+fw(4)+uid(12) | 20 | 是 | **截断** |
| READ_DEV_NAME(0xD1) | UTF-8 字符串 | 变长 | 可能 | **截断** |
| MOTOR_INFO_READ_BULK(0xE8) | start(2)+count(1)+count×4 | 3+4×N | 是（N≥2）| **截断** |
| MOTOR_INFO_WRITE_BULK(0xE9) | 回显数据 | 变长 | 可能 | **截断** |
| PARAM_READ(0xE0) | id(2)+type(1)+value | 3+value | 字符串参数可能 | 部分截断 |

- **影响**：CAN 模式下遥测、设备信息、批量参数读写、设备名称等功能全部异常
- **修复**：补全白名单（见 P0-6）

#### C2：ACK/NACK 在 CAN 模式下的行为（需联调确认）

- **现状**：[motor_client.py:300-306](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\core\motor_client.py#L300-L306) `cmd <= SINGLE_STEP and len(payload) >= 1` 走 ACK 分支，参数写队列依赖 `ack_received`/`nack_received` 清理
- **问题**：
  - UART：固件回应用层 ACK/NACK 帧（cmd + status byte）
  - CAN：硬件 ACK 只表示总线收到，**不表示应用层处理成功**
  - 若固件 CAN 侧不回应用层 ACK，参数写队列 `_pending_param_writes` 永不清理，`_cmd_watchdog` 1s 超时频繁触发
- **待确认**：固件 CAN 侧是否回应用层 ACK？需联调确认
- **适配方案**（见 P0-7）：若固件不回 ACK，CAN 模式下禁用命令超时看门狗或改为"发后即忘"模式

#### C3：命令超时看门狗 CAN 模式策略

- **现状**：[main_window.py:100-103](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L100-L103) `_cmd_watchdog` 1s 超时，弹 pending 项
- **问题**：CAN 模式下若固件不回 ACK，看门狗会每秒触发一次，状态栏频繁报错
- **方案**：CAN 模式下若确认无应用层 ACK，看门狗间隔延长到 5s 或禁用（见 P0-7）

### 0.3 UI 兼容性问题

#### U1：状态栏统计单位不适用 CAN

- **现状**：[main_window.py:696-698](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L696-L698) 显示 `TX 1.2 KB/s`、`RX 1.2 KB/s`、`帧 TX:0 RX:0`
- **问题**：CAN 更关心**帧率（帧/s）和总线占用率（%）**，字节速率不直观
- **方案**：CAN 模式下状态栏增加 `LOAD: 12%` 和帧率显示（见 P1-3）

#### U2：反馈面板轮询 vs CAN 主动推送

- **现状**：[feedback_panel](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\feedback_panel.py) 有"轮询"复选框，周期发 READ_FEEDBACK
- **问题**：CAN 模式下电机可能主动发 MIT 反馈帧（无需轮询），但当前 UI 不区分
- **方案**：CAN 模式下提示"电机可能主动推送反馈，轮询可关闭"（见 P2-5）

#### U3：日志逻辑帧字节数与物理帧不一致

- **现状**：log_panel 显示 `▼ RX READ_FEEDBACK(0xC0) [22B]`（22B 是解压后载荷），但实际收到 8B 物理帧
- **问题**：用户困惑为何显示 22B 但总线只传 8B
- **方案**：物理帧日志（P0-1）显示 `DLC:8`，逻辑帧日志保持 `[22B]`，两者并列显示可区分

#### U4：连接标签 CAN 模式显示

- **现状**：[main_window.py:1087](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L1087) `_sb_link` 显示 `● PCAN_USBBUS1`
- **兼容性**：OK，`_cur_port = channel` 已处理

---

## 一、现状分析（缺失清单）

### 1.1 日志显示问题
- **L1**：日志只显示 `cmd+payload`，看不到 CAN ID、motor_id、DLC
- **L2**：多帧重组的中间帧不会出现在日志（只在末帧重组后才 emit）
- **L3**：错误帧/远程帧被静默丢弃（[can_transport.py:328-329](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L328-L329)）
- **L4**：发送失败无日志
- **L5**：多帧发送中间帧失败不中断

### 1.2 数据回复/错误处理问题
- **E1**：多帧重组缓冲无超时清理（末帧丢失内存泄漏）
- **E2**：总线状态变更无感知（bus-off/error-passive）
- **E3**：接收超时无提示
- **E4**：断开连接时 _rx_segments 清理（已处理）

### 1.3 总线状态问题
- **B1**：无总线占用率显示
- **B2**：无错误计数器（TEC/REC）显示
- **B3**：无总线状态指示（ERROR_ACTIVE/PASSIVE/BUS_OFF）
- **B4**：流量统计未计 CAN 帧开销

### 1.4 接收过滤问题
- **F1**：未按 motor_id 过滤
- **F2**：广播帧（motor_id=0）处理策略不明

### 1.5 调试便利性问题
- **D1**：无 CAN 原始监听模式
- **D2**：无手动发送原始 CAN 帧功能
- **D3**：未利用硬件时间戳

---

## 二、实施范围划分

**本次实现（单帧路径 + 兼容性修复）：**
- P0-1 日志增强（raw_can_frame 信号）
- P0-2 错误帧/远程帧日志
- P0-4 发送失败告警（单帧部分）
- P0-5 motor_id 过滤
- **P0-6 多帧白名单补全（兼容性修复，必须）**
- **P0-7 ACK/NACK CAN 模式适配（需联调确认）**
- P1-1 总线状态指示
- P1-2 错误计数器显示
- P1-3 总线占用率估算
- P2-1 CAN 原始监听模式
- P2-2 手动发送原始 CAN 帧
- P2-3 硬件时间戳利用
- P2-4 比特率热切换
- P2-5 反馈面板 CAN 模式提示

**方案保留不实现（多帧路径增强）：**
- P0-3 多帧重组超时清理
- P0-4 多帧发送中断部分
- P1-4 CAN 状态独立面板（多帧队列状态部分）

说明：P0-6 虽涉及多帧，但属"让现有功能在 CAN 下正常工作"的兼容性修复，不是多帧增强功能，故纳入本次实施。

---

## 三、本次实施方案

### P0-1 日志增强：显示 CAN ID / DLC / motor_id
- **文件**：[can_transport.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py)、[base.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\base.py)、[log_panel.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\log_panel.py)
- **what**：
  1. `Transport` 基类新增 `raw_can_frame = pyqtSignal(int, bytes, bool, float)` 信号 `(can_id, data, is_rx, timestamp)`
  2. `CanTransport._handle_rx_msg` 开头 emit raw_can_frame（在 motor_id 过滤之前）
  3. `_write_msg` 发送成功后 emit raw_can_frame
  4. log_panel 新增 `log_can_frame(can_id, data, is_rx, ts)` 方法，格式：`▼ CAN RX ID:0xC001 DLC:8 8A 3D ...`
  5. log_panel 新增"CAN 原始帧"开关（默认开）
- **why**：CAN 调试必须看到物理帧 ID/DLC，解决 U3 兼容性问题
- **how**：基类加信号（SerialTransport 不发射），MainWindow 连接到 log_panel

### P0-2 错误帧/远程帧日志记录
- **文件**：[can_transport.py:328-329](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L328-L329)
- **what**：删除静默 continue，错误帧 emit error_occurred，远程帧 emit raw_can_frame
- **why**：错误帧是总线异常首要信号，静默丢弃无法定位问题
- **how**：
  ```python
  if msg.is_error_frame:
      self.error_occurred.emit(f"CAN 错误帧 ID:0x{msg.id:X} DLC:{msg.dlc}")
      self.rx_frames += 1
      continue
  if msg.is_remote_frame:
      self.raw_can_frame.emit(int(msg.id), b'', True, msg.timestamp)
      self.rx_frames += 1
      continue
  ```

### P0-4 发送失败告警（单帧部分）
- **文件**：[can_transport.py:231-241](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L231-L241)
- **what**：`_write_msg` 失败时 emit error_occurred
- **why**：发送失败必须让用户知道
- **how**：except 分支 emit `f"CAN 发送失败: {e}"`
- 注：多帧发送中断（_send_multi 中 if not ok: break）属多帧增强，保留方案不实现

### P0-5 接收按 motor_id 过滤
- **文件**：[can_transport.py:348-371](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L348-L371)
- **what**：`_handle_rx_msg` 在 emit raw_can_frame 之后，判断 motor_id，非本机非广播则 return 不解析
- **why**：多电机总线场景下，其他电机应答帧不应污染本机反馈面板
- **how**：
  ```python
  rx_motor_id = can_get_motor_id(can_id)
  if rx_motor_id != 0 and rx_motor_id != self._motor_id:
      return  # 非本机非广播, 不解析
  ```

### P0-6 多帧白名单补全（兼容性修复）
- **文件**：[can_transport.py:53-55](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L53-L55)
- **what**：补全 `_MULTI_FRAME_CMDS` 白名单
- **why**：当前只含 MOTOR_INFO_READ，导致 TELEMETRY/DEV_INFO/DEV_NAME/BULK 等应答在 CAN 模式下被截断（见 0.2 节 C1）
- **how**：
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
- **风险**：白名单过宽可能导致短帧被误判为多帧。但 _handle_multi_frame 的逻辑是：首帧 ctrl=0x00 会被当作 seq=0 非末帧加入缓冲，等末帧。若固件实际发单帧（≤8B），则永远不会收到末帧，缓冲泄漏。
- **缓解**：对于 PARAM_READ 等可能单帧也可能多帧的命令，需联调确认固件行为。若固件 CAN 侧对这些命令统一走多帧格式（即使 ≤8B 也用控制字），则白名单正确；若固件对 ≤8B 应答走单帧、>8B 走多帧，则白名单会导致短应答被误判。
- **决策**：先按"固件 CAN 侧对这些命令统一走多帧格式"假设补全白名单，联调时验证。若发现短应答被误判，回退到只保留确定 >8B 的命令（TELEMETRY/DEV_INFO/DEV_NAME/BULK）。

### P0-7 ACK/NACK CAN 模式适配（需联调确认）
- **文件**：[main_window.py:100-103](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L100-L103)
- **what**：
  1. 联调确认固件 CAN 侧是否回应用层 ACK/NACK
  2. 若不回：CAN 模式下禁用 `_cmd_watchdog` 或改为"发后即忘"模式（参数写不等待 ACK）
  3. 若回：无需改动
- **why**：CAN 硬件 ACK 不等于应用层 ACK，若固件不回应用层 ACK，参数写队列会卡死
- **how**：
  ```python
  # _on_connected 中, CAN 模式调整看门狗
  if self._client.transport.name == "can":
      self._cmd_watchdog.setInterval(5000)  # 或 stop()
  ```
- **状态**：待联调，本次先实现"CAN 模式延长看门狗到 5s"的保守策略

### P1-1 总线状态指示
- **文件**：[can_transport.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py)、[main_window.py:407-424](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L407-L424)
- **what**：
  1. CanTransport 新增 `bus_state_changed = pyqtSignal(str)` 信号
  2. 收循环每 500ms 读 `bus.state`，变化时 emit
  3. 状态栏新增 `_sb_bus_state` 标签，显示 `BUS: ACTIVE`（绿）/`PASSIVE`（黄）/`BUS_OFF`（红）
  4. BUS_OFF 时 emit error_occurred 告警
- **why**：bus-off 是 CAN 致命错误，必须实时告警
- **how**：python-can `bus.state` 返回 `BusState` 枚举，PCAN 接口支持

### P1-2 错误计数器显示
- **文件**：[can_transport.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py)、[main_window.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py)
- **what**：
  1. CanTransport 新增 `get_bus_stats() -> dict` 方法
  2. 状态栏 `_sb_bus_state` 扩展显示 `BUS: ACTIVE TEC:0 REC:0`
  3. 更新频率 1Hz
- **why**：TEC/REC 是预判总线健康度的关键指标
- **how**：python-can 4.x `bus.get_stats(timeout=0)` 返回 `BusStatistics`。PCAN 接口支持需联调验证，不支持则捕获异常回退到只显示 state

### P1-3 总线占用率估算
- **文件**：[main_window.py:691-698](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\main_window.py#L691-L698)
- **what**：
  1. `_on_stats_tick` 中若 `transport.name == "can"`，按 `(tx_frames + rx_frames) * (47 + 8*avg_dlc + 15) bits` 估算
  2. 状态栏新增 `_sb_load` 标签显示 `LOAD: 12%`（按当前 bitrate 算）
  3. 占用率 >70% 变黄告警
- **why**：CAN 占用率超 70% 易丢帧，解决 U1 兼容性问题
- **how**：CanTransport 暴露 `bitrate` 属性，占用率 = 估算 bit 数 / (bitrate * period_s)

### P2-1 CAN 原始监听模式
- **文件**：[can_transport.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py)、[connection_panel.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\connection_panel.py)
- **what**：
  1. CanTransport 加 `set_listen_only(bool)` 方法
  2. `_handle_rx_msg` 在 emit raw_can_frame 后判断 `_listen_only`，True 则 return
  3. 连接面板 CAN 区加"监听模式"复选框
- **why**：排查总线冲突/其他设备干扰时只需看原始流量
- **how**：监听模式不影响 raw_can_frame 日志，只阻断 frame_received 协议解析

### P2-2 手动发送原始 CAN 帧
- **文件**：新建 `ui/panels/can_send_dialog.py`、[can_transport.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py)
- **what**：
  1. CanTransport 加 `send_raw(can_id, data, extended=True) -> bool` 方法
  2. 新建 CanSendDialog，输入 CAN ID（十六进制）+ DLC + 8 字节 Data + 扩展帧复选框
  3. MainWindow 菜单加"CAN 发帧"入口（仅 CAN 连接时启用）
- **why**：固件调试时需构造任意帧测试解析逻辑
- **how**：dialog 调用 `client.transport.send_raw()`

### P2-3 硬件时间戳利用
- **文件**：[log_panel.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\log_panel.py)
- **what**：`log_can_frame` 接收 ts 参数，格式化为 `HH:MM:SS.mmmuuu`
- **why**：软件时间戳抖动大，硬件时间戳精度到微秒
- **how**：ts 是相对值（python-can PCAN 返回从打开总线开始的秒数），简化处理：用 now() 整数秒 + ts 小数部分作微秒

### P2-4 比特率热切换
- **文件**：[connection_panel.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\connection_panel.py)
- **what**：CAN 已连接时改比特率，触发 `bitrate_changed(int)` 信号，MainWindow 断开并以新比特率重连
- **why**：调试时频繁切换比特率，手动断开重连繁琐
- **how**：`_combo_can_bitrate.currentTextChanged` 在已连接时 emit bitrate_changed，不禁用下拉

### P2-5 反馈面板 CAN 模式提示
- **文件**：[feedback_panel.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\ui\panels\feedback_panel.py)
- **what**：CAN 模式下反馈面板轮询复选框旁加提示文字"CAN 模式下电机可能主动推送反馈，轮询可关闭"
- **why**：解决 U2 兼容性问题，提示用户 CAN 与 UART 反馈机制差异
- **how**：MainWindow 在 `_on_connected` 中判断 transport.name=="can" 时，调用 feedback_panel 的提示方法

---

## 四、多帧路径增强方案（保留，本次不实现）

### P0-3 多帧重组超时清理
- **文件**：[can_transport.py](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py)
- **问题**：`_rx_segments` 缓冲无超时清理，末帧丢失时内存泄漏
- **方案**：
  ```python
  # _rx_segments 改为 dict[int, tuple[bytearray, float]]  # can_id -> (buf, last_ts)
  # _handle_multi_frame 开头清理超时项 (>500ms)
  now = time.time()
  expired = [k for k, (buf, ts) in self._rx_segments.items() if now - ts > 0.5]
  for k in expired:
      self.error_occurred.emit(f"多帧重组超时 can_id=0x{k:X}, 丢弃 {len(buf)}B")
      self._rx_segments.pop(k, None)
  ```
- **不实现原因**：属多帧路径增强

### P0-4 多帧发送中断
- **文件**：[can_transport.py:209-229](file:///d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui\transport\can_transport.py#L209-L229)
- **问题**：`_send_multi` 中途帧失败仍继续发后续帧
- **方案**：`if not ok: break` 后 emit error_occurred 告知已发帧数
- **不实现原因**：属多帧路径增强

### P1-4 CAN 状态独立面板（多帧队列状态部分）
- **文件**：新建 `ui/panels/can_status_panel.py`
- **问题**：状态栏空间有限，深度调试需多帧重组队列状态等详细信息
- **方案**：独立 Tab 显示总线状态/TEC/REC/占用率/最近 N 条错误帧/**多帧重组队列状态**
- **不实现原因**：含多帧队列状态展示

---

## 五、假设与决策

1. **python-can API 假设**：
   - `bus.state` 返回 `BusState` 枚举，PCAN 接口支持
   - `bus.get_stats(timeout=0)` 在 PCAN 接口的支持需联调验证，不支持的回退到只显示 state
   - `msg.timestamp` 是硬件时间戳（PCAN USB 支持，精度到微秒）

2. **向后兼容**：
   - `raw_can_frame` 信号加在 Transport 基类但 SerialTransport 不发射，串口路径零影响
   - log_panel 新增方法不破坏现有 `log_tx/log_rx` 接口
   - 所有 CAN 增强功能通过 `transport.name == "can"` 判断，串口路径不受影响

3. **信号重连问题**：
   - `set_transport` 切换传输时，MainWindow 需重连 `raw_can_frame` 和 `bus_state_changed` 信号
   - 现有 `JmClient.set_transport` 只重连 `frame_received/connected/error_occurred`，新增信号需在 MainWindow 层手动重连（封装 `_reconnect_can_signals()`）

4. **P0-6 白名单决策**：
   - 先按"固件 CAN 侧对白名单命令统一走多帧格式"假设补全
   - 联调时验证：若短应答被误判为多帧（缓冲泄漏），回退到只保留确定 >8B 的命令
   - PARAM_READ 是否加入白名单取决于是否有字符串类型参数（char[]），需查参数表确认

5. **P0-7 ACK 决策**：
   - 本次先实现"CAN 模式延长看门狗到 5s"的保守策略
   - 联调确认固件行为后，若回 ACK 则恢复 1s，若不回则改为发后即忘

6. **不修改协议层**：
   - 所有改动都在 transport 层和 UI 层，jmproto/JmClient 零改动（P0-6 只改 can_transport 常量，不动 JmClient）

---

## 六、验证步骤

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
13. **P2-3 时间戳**：CAN 帧日志时间戳精度到微秒
14. **P2-4 比特率热切换**：CAN 已连接时改比特率，自动断开重连
15. **P2-5 反馈提示**：CAN 模式下反馈面板显示提示文字

---

## 七、风险

1. **P0-6 白名单误判**：若固件对 ≤8B 应答走单帧、>8B 走多帧，白名单会导致短应答被误判为多帧（缓冲泄漏）。缓解：联调验证，必要时回退。
2. **P0-7 ACK 行为未知**：固件 CAN 侧是否回应用层 ACK 需联调确认。本次保守延长看门狗到 5s。
3. **python-can PCAN 接口 get_stats 支持不确定**：若不支持 TEC/REC，P1-2 降级为只显示 state。
4. **bus.state 读取可能阻塞**：PCAN 驱动在某些状态下读取 state 可能慢，需在收循环空闲周期读取。
5. **raw_can_frame 高频信号积压**：1Mbps 满载约 8000 帧/s，Qt 信号槽可能积压。log_panel 已有批量刷新兜底。
6. **set_transport 信号重连遗漏**：切换传输时若忘记重连 raw_can_frame/bus_state_changed，CAN 日志丢失。需封装 `_reconnect_can_signals()` 统一处理。

---

## 八、实施顺序（本次）

1. `transport/base.py`：加 `raw_can_frame` 信号
2. `transport/can_transport.py`：
   - 补全 `_MULTI_FRAME_CMDS` 白名单（P0-6）
   - emit raw_can_frame（RX/TX）
   - 错误帧/远程帧日志（P0-2）
   - 发送失败告警（P0-4）
   - motor_id 过滤（P0-5）
   - bus_state_changed 信号 + get_bus_stats 方法（P1-1/P1-2）
   - set_listen_only / send_raw 方法（P2-1/P2-2）
   - 暴露 bitrate 属性（P1-3）
3. `ui/panels/log_panel.py`：log_can_frame 方法 + CAN 原始帧开关（P0-1/P2-3）
4. `ui/panels/connection_panel.py`：监听模式复选框 + bitrate_changed 信号（P2-1/P2-4）
5. `ui/panels/can_send_dialog.py`（新建）：手动发帧对话框（P2-2）
6. `ui/panels/feedback_panel.py`：CAN 模式提示（P2-5）
7. `ui/main_window.py`：
   - 状态栏加 `_sb_bus_state` / `_sb_load` 标签（P1-1/P1-3）
   - `_connect_signals` 连接 raw_can_frame/bus_state_changed
   - `_reconnect_can_signals()` 在 set_transport 后调用
   - `_on_stats_tick` 加 CAN 状态/占用率更新（P1-1/P1-2/P1-3）
   - `_on_connected` CAN 模式延长看门狗（P0-7）
   - 菜单加"CAN 发帧"入口（P2-2）
   - bitrate_changed 信号处理（P2-4）
   - CAN 模式反馈面板提示（P2-5）
8. 语法检查 + 启动验证
