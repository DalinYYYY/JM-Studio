# PyQt 上位机框架重构方案

## Context（为什么要做）

当前 `User/Tools/pyqt_gui` 是单层平铺结构：`jm_protocol.py`(协议+数据结构混在一起) + `jm_serial.py`(串口) + `main_window.py`(全部 UI 写在一个 600 行的类里)。已暴露三类问题：

1. **协议与传输耦合死**：`jm_serial.py` 里把"串口收发"和"帧分发/业务解析"绑在一起，后面接 CAN 必须重写一遍分发逻辑。
2. **UI 硬编码**：运动模式、命令按钮、参数读写都在代码里手写。固件协议有 **100 条命令 + 83 个参数**(见 `docs/joint_motor_command_list.csv` / `joint_motor_param_index.csv`)，手写无法覆盖，且每次协议扩展都要改 UI 代码。
3. **扩展点缺失**：没有遥测订阅(0xCB/0xCA)的完整支持，没有给绘图/CAN 留抽象。

目标：重构成**分层包结构 + 数据驱动 UI + 传输抽象**，让新增命令/参数无需改 Python，CAN 只需补一个 transport 实现，绘图只需接已留好的数据分发点。功能行为对用户保持等价或更强。

> 已修复并保留的两个阻断性 bug（本次重构需带入新结构）：CRC 必须是 **CRC-16/XMODEM**（见 [[crc16-xmodem-contract]]）；串口句柄必须**单锁串行化**收发，否则 Windows 段错误崩溃。

---

## 目标结构

```
User/Tools/pyqt_gui/
├── main.py                      # 入口(基本不变)
├── requirements.txt             # +可选 pyqtgraph(本次不加)
├── .gitignore                   # 已建
├── resources/                   # 协议数据(运行时加载)
│   ├── joint_motor_command_list.csv   # 从 docs/ 拷贝, 命令表
│   └── joint_motor_param_index.csv    # 从 docs/ 拷贝, 参数表
├── jmproto/                     # 协议层(传输无关)
│   ├── __init__.py
│   ├── crc16.py                 # XMODEM CRC(从现 jm_protocol.py 抽出)
│   ├── frame.py                 # FrameCodec + FrameDecoder(串口帧)
│   ├── cmd_def.py               # JmCmd / JmErr / JmTlmBit 枚举
│   ├── codec.py                 # rd_*/wr_* 小端助手 + payload 编解码
│   ├── feedback.py              # FeedbackData + 各 READ_* 应答解析
│   └── registry.py              # ★CSV 加载: CommandSpec / ParamSpec 表
├── transport/                   # 传输层(可插拔)
│   ├── __init__.py
│   ├── base.py                  # ★Transport 抽象基类(open/close/send/信号)
│   ├── serial_transport.py      # 串口实现(QThread, 单锁, 现 jm_serial 重构)
│   └── can_transport.py         # CAN 占位(后续实现, 先留 NotImplemented)
├── core/
│   ├── __init__.py
│   └── motor_client.py          # ★JmClient: transport无关的高层命令/分发/遥测
└── ui/
    ├── __init__.py
    ├── main_window.py           # 仅组装各面板 + 连接信号
    └── panels/
        ├── __init__.py
        ├── connection_panel.py  # 串口/CAN 连接选择
        ├── control_panel.py     # 系统控制(使能/急停/清障…)
        ├── motion_panel.py      # ★数据驱动: 按 CSV 渲染命令载荷输入
        ├── param_panel.py       # ★数据驱动: 按 CSV 渲染83参数表(分组/类型/单位)
        ├── feedback_panel.py    # 实时反馈 + 状态
        ├── telemetry_panel.py   # 遥测订阅勾选(按 JmTlmBit 位)
        ├── plot_panel.py        # ★绘图占位(预留接口, 暂不实现)
        └── log_panel.py         # 通信日志
```

---

## 关键设计

### 1. 传输抽象层 `transport/base.py`
定义统一接口，串口和 CAN 都实现它，`JmClient` 只依赖抽象：
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
- **串口实现**(`serial_transport.py`)：把现 `jm_serial.py` 的 `JmSerialWorker` 迁入，保留已修复的 `_io_lock` 单锁串行化 + `_alive/_running` 双标志线程生命周期 + 连接时复位 `FrameDecoder`。帧的封包/解包用 `jmproto.frame`。
- **CAN 实现**(`can_transport.py`)：本次只建文件 + 类骨架，`open()` 抛 `NotImplementedError("CAN 待实现")`。预留：CAN 收发 `jm_can_frame_t` 等价结构、`ID=(CMD<<8)|motor_id` 拆装、MIT 定点压缩(`JM_MIT_*` 范围)、多帧分包重组(`JM_CAN_SEG_*`)。这些常量在 `jm_proto_can.h` 已定义，实现时照搬。

### 2. 协议数据驱动 `jmproto/registry.py`（核心新增）
运行时加载 `resources/*.csv`，解析成结构化表：
- `CommandSpec`：`cmd, name, category, fields[(name,type,unit)], req_len, ack_kind, note`。`fields` 由 CSV「串口请求载荷(CMD后)」列解析，例如 `{pos:f32;vel:f32}` → `[(pos,f32),(vel,f32)]`。
- `ParamSpec`：`param_id, code_name, cn_name, dtype, nbytes, unit, group, rw`。直接映射 param 表 8 列。
- 提供 `pack_command(cmd, values:dict)->bytes`（按 fields 顺序用 `codec` 小端打包）和 `pack_param_value(param_id, text)->bytes` / `unpack_param_value(param_id, bytes)`（按 dtype）。
- 健壮性：CSV 缺失或解析失败时回退到内置最小命令集（ENABLE/DISABLE/POSITION/VELOCITY/TORQUE 等），并在日志告警——保证没有 CSV 也能跑。

### 3. 高层客户端 `core/motor_client.py`
等价于现 `JmSerial` 的高层部分，但**与传输解耦**：
- 持有一个 `Transport`，订阅其 `frame_received`，复用现 `jm_serial.py:_on_frame` / `_parse_telemetry` 的完整分发逻辑（搬过来即可，已经写得很全）。
- 暴露高层信号：`feedback_updated / state_updated / ack_received / nack_received / dev_info / param_read_result / tx_log` 等（沿用现有）。
- 命令方法改为基于 registry：`send_command(cmd, values)`、`param_read/write`、`set_telemetry(mask, period)`。
- 兜底轮询：`enable_polling(period)` 仍保留 `READ_FEEDBACK/READ_STATE`，但默认走遥测订阅(0xCB)，仅在用户关闭遥测或固件 NACK 时启用。

### 4. 数据驱动面板
- **motion_panel**：模式下拉由 `registry` 中"运动控制/高级力控"类命令填充；选中某命令后，根据其 `fields` **动态生成对应输入框**（f32→DoubleSpinBox，u8/u32→SpinBox），点发送时 `pack_command` 打包。新增命令只要 CSV 加一行即可出现。
- **param_panel**：用 `QTableWidget` 按 `group` 分组列出 83 个参数，每行：中文名、param_id、类型/单位、当前值、读/写按钮。读写走 `PARAM_READ/WRITE` + `pack_param_value`。
- **telemetry_panel**：按 `JmTlmBit` 各位生成勾选框，组合成 mask 调 `set_telemetry`。
- **plot_panel**：定义 `add_series/append(ts, name, value)` 接口签名，内部暂用占位 Label（"绘图功能预留"）。`feedback_updated` 已可接入，后续换 pyqtgraph 不动其他层。

### 5. 信号流（保持 Qt 线程安全）
```
Transport(后台线程) --frame_received--> JmClient._on_frame(主线程槽)
   --> 分发为 feedback_updated / ack / nack / param_read_result / tx_log
   --> 各 panel 槽更新 UI / plot_panel.append(...)
```
发送方向：panel --> JmClient.send_command --> Transport.send（持 `_io_lock` 写）。

---

## 改造的关键文件 / 复用点

| 动作 | 文件 | 说明 |
|------|------|------|
| 抽出 | 现 `jm_protocol.py` | CRC→`jmproto/crc16.py`；帧机→`frame.py`；枚举→`cmd_def.py`；助手→`codec.py`；FeedbackData→`feedback.py`。**逻辑不变，仅拆分** |
| 重构 | 现 `jm_serial.py` | `JmSerialWorker`→`transport/serial_transport.py`（保留 `_io_lock` 修复）；高层分发→`core/motor_client.py`（搬 `_on_frame`/`_parse_telemetry`） |
| 新建 | `jmproto/registry.py` | CSV 加载与打包，**最核心新增** |
| 新建 | `transport/base.py` `can_transport.py` | 抽象 + CAN 占位 |
| 拆分 | 现 `main_window.py` | 按面板拆到 `ui/panels/`，`main_window.py` 只做组装 |
| 拷贝 | `docs/*.csv` → `resources/*.csv` | 让工具自带数据，不依赖 docs 相对路径 |

复用：`jm_proto_can.h` 的 `JM_MIT_*`/`JM_CAN_SEG_*` 常量（CAN 实现时）；`jm_proto.c:pack_feedback` 的 22 字节布局（已与现解析一致）。

---

## 验证方式

1. **协议层纯单元自测**（无需硬件，无需 PyQt）：
   - CRC 表前4项 `0x0000,0x1021,0x2042,0x3063`、表[255]=`0x1ef0`；规范示例 0x15+1.57f 整帧 pack→decode 往返还原。
   - `registry` 加载 CSV：断言命令数=100、参数数=83；`pack_command(0x16,{pos:1,vel:2})` 得 8 字节；`pack_command(0x15,{pos:1.57})` 与规范一致。
   - 模拟 22B READ_FEEDBACK 帧 + 一帧 TELEMETRY(mask 含 POS_VEL|STATE) 喂入解析，字段正确。
   - 命令：`python -c` 跑上述断言，全绿。
2. **导入/编译**：`python -m py_compile` 所有 `.py`；`jmproto`、`transport.base`、`core.motor_client` 不依赖串口可独立 import。
3. **GUI 冒烟**（你本地，需 `pip install -r requirements.txt`）：
   - 不连接即启动不报错；连接真实电机后默认下发 `SET_TELEMETRY`，反馈面板刷新；
   - motion_panel 切换命令时输入框随 CSV 动态变化；param_panel 能读回某参数；
   - 断开/重连不崩溃（验证 `_io_lock` + 线程生命周期）。
4. **回归**：确认 CRC、单锁两处修复在新结构中存在（grep `0x1021`、`_io_lock`）。

> 注：当前环境无 `pyserial`/`PyQt6`，第 3 步由你本地执行；第 1/2 步我可直接在重构后运行。
