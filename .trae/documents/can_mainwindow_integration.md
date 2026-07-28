# CAN 传输 MainWindow 集成方案

## 摘要

本方案承接已完成的 `can_transport.py`（核心收发+多帧+MIT压缩）和 `connection_panel.py`（传输类型下拉+CAN配置区），完成剩余的 **MainWindow 编排层集成 + feature_flags 开关 + requirements 依赖 + 启动验证**，使 CAN 通信功能端到端可用。

目标：用户在连接面板选择"CAN"传输类型 → 选 PCAN 通道/比特率/电机ID → 点连接 → JmClient 热切换到 CanTransport → 上层业务（参数读写/反馈/遥测/MIT力控）零改动运行。所有设置（传输类型/CAN通道/比特率/电机ID）持久化到 `ui_layout.json`。

## 现状分析

### 已完成（无需改动）
- `transport/can_transport.py`：完整实现。`name="can"`，`open(channel, bitrate, motor_id)`，`send(cmd, payload)` 自动单帧/多帧/MIT压缩，反馈帧解压为 22 字节 READ_FEEDBACK 兼容载荷。
- `transport/base.py`：抽象基类已定义 `name` 属性（`"base"`），子类覆盖为 `"serial"`/`"can"`/`"virtual"`。
- `ui/panels/connection_panel.py`：已具备 `connect_can_requested = pyqtSignal(str, int, int)` 信号、CAN 配置区（通道下拉+刷新+比特率+电机ID）、`_detect_can_support()` 懒导入探测、`get/set_transport_type`、`get/set_can_bitrate`、`get/set_can_motor_id`、`get/set_can_channel_name` 接口、`set_connected()` 同步禁用所有 CAN 控件。

### 待改造
- `ui/main_window.py`：
  - `_connect_signals`（line 875-876）：缺 `connect_can_requested` 信号连接
  - `_on_connect`（line 962）：`isinstance(self._client.transport, SerialTransport)` 无法判断懒导入的 CanTransport，需改 `transport.name` 判断
  - 缺 `_on_connect_can` 方法
  - `_collect_settings`（line 476-509）：缺 CAN 持久化字段
  - `_apply_settings`（line 549+）：缺 CAN 设置恢复逻辑
- `ui/feature_flags.py`：`_DEFAULTS` 缺 `can_transport` 键（line 15-26）
- `requirements.txt`：文件不存在

## 方案设计

### 1. `ui/feature_flags.py` — 加默认开关

在 `_DEFAULTS` dict 末尾加一行 `"can_transport": True,`。

理由：与现有 `virtual_twin`/`log_panel` 等开关机制一致；`connection_panel._detect_can_support()` 已先查此开关再 try import，开关关闭时 CAN 选项不出现。

### 2. `ui/main_window.py` — 4 处改造

#### 2.1 `_connect_signals` 加 CAN 信号连接

在 line 875-876 之间（`connect_requested` 和 `disconnect_requested` 之后）追加：

```python
self._conn_panel.connect_can_requested.connect(self._on_connect_can)
```

#### 2.2 `_on_connect` 改 isinstance 为 name 判断

line 962 原代码：
```python
if not isinstance(self._client.transport, SerialTransport):
    self._client.set_transport(SerialTransport())
```
改为：
```python
if self._client.transport.name != "serial":
    self._client.set_transport(SerialTransport())
```

line 944 的 `isinstance(self._client.transport, VirtualTransport)` 同理改为 `self._client.transport.name != "virtual"`（VirtualTransport 虽已顶部导入可用 isinstance，但统一用 name 判断保持一致风格且避免后续重构引入歧义）。

#### 2.3 新增 `_on_connect_can` 方法

放在 `_on_connect` 方法之后、`_force_disable_virtual_mode` 之前：

```python
def _on_connect_can(self, channel: str, bitrate: int, motor_id: int):
    """CAN 连接: 懒导入 CanTransport, 热切换后打开链路。"""
    try:
        from transport.can_transport import CanTransport
    except ImportError:
        QMessageBox.warning(self, "提示", "python-can 未安装, 无法使用 CAN 传输")
        return
    if self._client.transport.name != "can":
        self._client.set_transport(CanTransport())
    if self._client.open(channel=channel, bitrate=bitrate, motor_id=motor_id):
        self.statusBar().showMessage(f"已连接 {channel} @{bitrate} ID={motor_id}")
        self._cur_port = channel
        # CAN 连接真机: 虚拟电机模式开关必须关闭并禁用
        self._force_disable_virtual_mode()
```

要点：
- 懒导入避免 `python-can` 缺失时整个 main_window 模块加载失败
- 用 `transport.name != "can"` 判断避免 isinstance 对懒导入类失效
- 复用 `_force_disable_virtual_mode()` 保持与串口真机连接一致的行为
- `JmClient.set_transport()` 内部会 stop 旧 transport + start 新 transport + 重连信号，对上层透明

#### 2.4 `_collect_settings` 加 CAN 持久化字段

在 `_collect_settings` 的 `s = { ... }` dict 中（line 478-509），`"baud": ...` 之后追加：

```python
"transport_type": self._conn_panel.get_transport_type(),
"can_bitrate": self._conn_panel.get_can_bitrate(),
"can_motor_id": self._conn_panel.get_can_motor_id(),
"can_channel_name": self._conn_panel.get_can_channel_name(),
```

#### 2.5 `_apply_settings` 加 CAN 设置恢复

在 `_apply_settings` 中，`baud` 恢复之后（line 553-555 之后）追加：

```python
# 传输类型恢复 (先恢复类型, 再恢复对应配置项)
tt = d.get("transport_type")
if isinstance(tt, str) and tt:
    self._conn_panel.set_transport_type(tt)
can_br = d.get("can_bitrate")
if isinstance(can_br, str) and can_br:
    self._conn_panel.set_can_bitrate(can_br)
can_mid = d.get("can_motor_id")
if isinstance(can_mid, int):
    self._conn_panel.set_can_motor_id(can_mid)
# CAN 通道名恢复: 启动时通道列表可能尚未刷新, 延迟套用
can_ch = d.get("can_channel_name")
if isinstance(can_ch, str) and can_ch:
    QTimer.singleShot(100, lambda: self._conn_panel.set_can_channel_name(can_ch))
```

延迟 100ms 套用 channel_name 的原因：`_on_transport_changed` 在 `set_transport_type` 时会触发 `_refresh_can_channels()` 异步枚举，若立即 `set_can_channel_name` 下拉项尚未填充会匹配失败。延迟 100ms 让枚举完成。

### 3. 创建 `requirements.txt`

项目根目录无 `requirements.txt`。按"最小必要依赖"原则创建，只列 CAN 功能新增依赖：

```
python-can>=4.0.0
```

注意：不强行列举 pyserial/pyqtgraph/PyQt6 等已有依赖（避免与现有环境版本冲突，这些依赖已在环境中可用）。若后续需要完整依赖固化，可单独执行 `pip freeze`。

## 假设与决策

1. **CAN 传输不触发虚拟引擎路径**：CAN 连接走 `_on_connect_can`，与 `_on_connect` 中的 `"VIRTUAL"` 分支互斥。CAN 连接后 `_force_disable_virtual_mode()` 确保虚拟模式关闭。
2. **断开逻辑无需改动**：`_on_disconnect` 调用 `self._client.close()` 对 SerialTransport/CanTransport/VirtualTransport 都适用（均有 close 方法）。`_force_disable_virtual_mode()` 对 CAN 断开也正确。
3. **`_on_stats_tick` 无需改动**：已用 `self._client.transport` 读取 `tx_bytes/rx_bytes/tx_frames/rx_frames`，CanTransport 继承自 Transport 已维护这些字段。
4. **不修改 `_on_connect` 中 `"VIRTUAL"` 分支的 isinstance**：line 944 改为 name 判断仅为风格统一，功能等价（VirtualTransport 顶部已导入，isinstance 可用）。若担心改动范围扩大，可保留 isinstance 不改——但为避免"懒导入类无法 isinstance"的隐患扩散，统一用 name 判断更稳妥。
5. **CAN 通道持久化用 channel_name（如 "PCAN_USBBUS1"）而非 handle**：USB 插拔会改变 handle，channel_name 稳定可持久化。`connection_panel.set_can_channel_name` 已按 data 字段匹配。
6. **不处理 CAN 传输的"虚拟数据引擎"兼容**：CAN 是真机传输，不与虚拟引擎混用。`connection_panel._on_transport_changed` 切到 CAN 时只刷新 CAN 通道列表，不显示 VIRTUAL 选项。

## 验证步骤

### 1. 语法检查（无硬件）
```powershell
cd d:\AAWorkSpace\001_JointMotor\SW\JointMotor\User\Tools\pyqt_gui
python -c "import ast; ast.parse(open('ui/main_window.py',encoding='utf-8').read()); print('main_window OK')"
python -c "import ast; ast.parse(open('ui/feature_flags.py',encoding='utf-8').read()); print('feature_flags OK')"
python -c "import ast; ast.parse(open('ui/panels/connection_panel.py',encoding='utf-8').read()); print('connection_panel OK')"
python -c "import ast; ast.parse(open('transport/can_transport.py',encoding='utf-8').read()); print('can_transport OK')"
```

### 2. 启动验证（无 PCAN 硬件）
- 确认 `python-can` 未安装时：连接面板"传输类型"下拉只有"串口"选项，无"CAN"项，程序正常启动无报错。
- 安装 `pip install python-can` 后重启：下拉出现"CAN"选项，切到 CAN 时串口区隐藏、CAN 区显示，点"刷新"枚举通道（无硬件时列表为空，不报错）。

### 3. 持久化验证
- 选"CAN"传输类型 → 设比特率/电机ID → 关窗 → 重启 → 确认传输类型恢复为"CAN"、比特率/电机ID 恢复。
- 有硬件时：选通道 → 关窗 → 重启 → 确认通道名恢复（延迟 100ms 套用）。

### 4. 联调验证（有 PCAN 硬件，需固件配合）
- 选 PCAN 通道 → 连接 → 状态栏显示 `已连接 PCAN_USBBUS1 @1000000 ID=1`。
- 发 READ_FEEDBACK → 反馈面板显示 pos/vel/torque/temp_motor（vbus 为 0，CAN 压缩帧无此字段）。
- 发 MIT 命令 → 电机响应力矩。
- 发 MOTOR_INFO_READ(0xE6) → 多帧重组成功，参数表回填。
- 断开 → 状态栏显示"已断开"，虚拟电机模式开关禁用。

## 风险缓解

1. **MIT 压缩位序风险**：`_pack_mit`/`_unpack_mit_feedback` 按 MIT Cheetah 惯例大端实现（pos16→vel12→kp12→kd12→tff12）。联调前必须对照固件 `jm_proto_can.h` 校准位序与范围常量。若不一致，修改 `can_transport.py` 顶部的 `MIT_*_MIN/MAX` 常量和 `_pack_mit` 移位顺序即可，不影响本方案的 MainWindow 集成。
2. **多帧分包白名单风险**：`_MULTI_FRAME_CMDS = {MOTOR_INFO_READ}`。若联调发现其他命令也走多帧，加入此集合即可。ACK/NACK 等短帧不会误判（白名单机制）。
3. **python-can 版本兼容**：`list_channels` 已兼容 dict 和对象两种 `detect_available_configs` 返回形式。python-can 4.x API 稳定。

## 实施顺序

1. `ui/feature_flags.py` 加 `can_transport: True`
2. `ui/main_window.py` 4 处改造（信号连接 → name 判断 → _on_connect_can → 持久化）
3. 创建 `requirements.txt`
4. 语法检查 4 个文件
5. 启动验证（无硬件场景）
