"""
CAN 传输实现: 基于 python-can + QThread 异步收发

与固件 jm_proto_can.h 一致:
- 仲裁 ID(扩展帧29位): ID = (CMD << 8) | motor_id
- 数据区直接是命令载荷(不含 CMD); 经典模式单帧最多 8 字节, FD 模式单帧最多 64 字节
- >single_max 字节多帧分包(data[0]=控制字, 末帧追加 CRC16)
- MIT(0x13/0x30) 与反馈帧用 8 字节定点压缩

运行期 CAN FD 模式切换(0xF3 SET_FD_MODE):
- 上电默认经典模式(兼容所有上位机硬件)
- 上位机检测自身 FD 能力后, 发 0xF3 enable=1 切换下位机到 FD 长帧模式
- 切换为纯软件操作, 不重新初始化 FDCAN 外设
- FD 模式下: 单帧最大 64B, 多帧片段 63B; 经典模式: 单帧 8B, 多帧片段 7B

关键约束:
- 单一 _io_lock 串行化对 self._bus 的所有访问, 避免 Windows 段错误
- _alive(线程生命周期) 与 _running(总线打开) 双标志分离
- 多帧重组: 基于控制字和 DLC 智能判断, 末帧 CRC16 校验

依赖: python-can>=4.0.0; Windows 使用 PCAN, Linux 使用系统 SocketCAN
"""

import queue
import struct
import sys
import threading
import time
from typing import Optional

import can  # python-can

from jmproto.cmd_def import JmCmd
from jmproto.crc16 import crc16_calc
from .base import Transport


# ---- MIT 定点压缩范围(与 jm_proto_can.h 一致) ----
MIT_POS_MIN, MIT_POS_MAX = -12.5, 12.5
MIT_VEL_MIN, MIT_VEL_MAX = -65.0, 65.0
MIT_KP_MIN, MIT_KP_MAX = 0.0, 500.0
MIT_KD_MIN, MIT_KD_MAX = 0.0, 5.0
MIT_TFF_MIN, MIT_TFF_MAX = -50.0, 50.0
FB_TEMP_MIN, FB_TEMP_MAX = -40.0, 215.0

# ---- 多帧分包常量(与固件 jm_proto_can.h 一致) ----
CAN_SEG_LAST = 0x80
CAN_SEG_SEQ_MASK = 0x7F
CAN_MULTI_FLAG = 1 << 16
# 经典模式
CAN_SEG_PAYLOAD_CLS = 7
CAN_SINGLE_MAX_CLS = 8
# FD 模式
CAN_SEG_PAYLOAD_FD = 63
CAN_SINGLE_MAX_FD = 64
CAN_FD_DLC_LENGTHS = frozenset((*range(0, 9), 12, 16, 20, 24, 32, 48, 64))
CAN_FD_SEG_LENGTHS = (63, 47, 31, 23, 19, 15, 11, 7, 6, 5, 4, 3, 2, 1)
# 多帧重组超时(ms)
CAN_MULTI_TIMEOUT_MS = 200
# 0xF3 SET_FD_MODE 同步等待 ACK 超时
SWITCH_FD_ACK_TIMEOUT_S = 0.3

# ---- 多帧分包常量 ----
# 固件协议(jm_proto_can.c can_emit_payload):
#   载荷≤single_max → 单帧, 数据直接是载荷(无控制字)
#   载荷>single_max → 多帧, data[0]=控制字(bit7=末帧, bit6:0=序号), data[1..]=片段
#   末帧末尾追加 2 字节 CRC16(小端, 覆盖整个载荷)
# 接收侧不依赖命令白名单, 基于 DLC 和控制字特征智能判断。


def can_make_id(cmd: int, motor_id: int) -> int:
    return ((cmd & 0xFF) << 8) | (motor_id & 0xFF)


def can_get_cmd(can_id: int) -> int:
    return (can_id >> 8) & 0xFF


def can_get_motor_id(can_id: int) -> int:
    return can_id & 0xFF


class CanTransport(Transport):
    """CAN 传输: Windows 使用 PCAN, Linux 使用 SocketCAN。"""

    name = "can"
    _READ_TIMEOUT_S = 0.001
    _IDLE_WAIT_S = 0.0005
    _CLOSED_WAIT_S = 0.01
    _WRITE_TIMEOUT_S = 0.02
    # Linux 的 PCAN-USB 由内核 SocketCAN 驱动提供 can0 等网络接口。
    _INTERFACE = "socketcan" if sys.platform.startswith("linux") else "pcan"
    # 发送队列容量(满时丢弃, 计入 tx_dropped_count)
    _TX_QUEUE_SIZE = 256
    _TX_THREAD_NAME = "CanTx"

    def __init__(self):
        super().__init__()
        self._bus: Optional[can.BusABC] = None
        self._channel: str = ""
        self._bitrate: int = 1000000
        self._motor_id: int = 1
        self._alive = False      # 线程生命周期
        self._running = False    # 总线打开标志
        self._io_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._wake_event = threading.Event()
        # 多帧重组缓冲: {can_id: bytearray}
        self._rx_segments: dict = {}
        # 多帧缓冲时间戳: {can_id: timestamp}, 超时100ms未完成则回退为单帧
        self._rx_segments_ts: dict = {}
        # 多帧期望序号: {can_id: expected_seq}, 用于 seq 连续性检查
        self._rx_expected_seq: dict = {}
        self._tx_seq: int = 0
        # P2-1 监听模式: 只记物理帧日志, 不解析协议帧
        self._listen_only = False
        # 节点发现模式: 额外接收任意 1~127 节点的 READ_DEV_INFO 响应
        self._discovery_mode = False
        # P1-1/P1-2 总线状态检查(收循环空闲周期读取, 不阻塞 recv)
        self._last_bus_state = ""
        self._bus_state_check_ts = 0.0
        # 非阻塞发送队列: UI 线程入队, _tx_loop 线程出队写总线
        self._tx_queue: queue.Queue = queue.Queue(maxsize=self._TX_QUEUE_SIZE)
        self._tx_thread: Optional[threading.Thread] = None
        self._tx_stop = threading.Event()
        self.tx_dropped_count = 0  # 队列满丢弃计数
        # 运行期 FD 模式(与下位机同步): False=经典(默认), True=FD长帧
        self._fd_mode: bool = False
        # 上位机硬件 FD 能力(open 时由 fd 参数决定)
        self._fd_capable: bool = False
        # 0xF3 SET_FD_MODE 同步等待 ACK 的事件与结果
        self._fd_ack_event = threading.Event()       # rx 线程 set, tx 线程 wait
        self._fd_switch_done = threading.Event()     # tx 线程 set, UI 线程 wait
        self._fd_ack_result: Optional[tuple] = None  # (ack_enable, cap) 或 None(超时)
        # 多帧 CRC 校验失败统计
        self.rx_crc_errors: int = 0
        # 多帧 seq 不连续统计(帧丢失或乱序)
        self.rx_seq_errors: int = 0
        # 总线负载估算专用计数器: 累计总线所有帧(RX+TX)的位长(含帧头+填充+ITF)
        # 在收帧最早期(过滤前)累加, 确保 RX 计入总线上所有帧, 与其他 CAN 工具一致。
        # 由 _bus_load_lock 保护(rx/tx/ui 三线程访问)
        self._bus_load_bits: int = 0
        self._bus_load_lock = threading.Lock()

    # ---------------- 通道枚举 ----------------
    @staticmethod
    def list_channels() -> list:
        """列出当前平台可用 CAN 通道: [(channel_name, description), ...]

        channel_name 形如 'PCAN_USBBUS1', 传给 can.Bus(channel=...) 使用。
        description 形如 'PCAN-USB Ch.1' 供 UI 显示。
        无硬件或驱动未装时返回空列表。
        """
        try:
            configs = can.interface.detect_available_configs(CanTransport._INTERFACE)
        except Exception:
            return []
        out = []
        for cfg in configs:
            # python-can 4.x 返回 dict: {'interface': 'pcan', 'channel': 'PCAN_USBBUS1', ...}
            if isinstance(cfg, dict):
                ch = cfg.get('channel', '')
            else:
                # 兼容部分版本返回对象
                ch = getattr(cfg, 'channel', '')
            if not ch:
                continue
            desc = (cfg.get('description') if isinstance(cfg, dict)
                    else getattr(cfg, 'description', None)) or ch.replace('PCAN_USBBUS', 'PCAN-USB Ch.')
            out.append((ch, desc))
        return out

    # ---------------- 线程生命周期 ----------------
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

    # ---------------- 链路操作 ----------------
    def open(self, channel: str = "", bitrate: int = 1000000,
             motor_id: int = 1, fd: bool = False, data_bitrate: int = 0,
             **_) -> bool:
        """打开 CAN 总线。Linux 通道的波特率及 FD 能力需预先通过 ip link 配置。"""
        if not 1 <= int(motor_id) <= 127:
            self.error_occurred.emit(f"CAN 节点 ID 越界: {motor_id}, 应在 1~127 范围内")
            return False
        try:
            # 静默关闭旧连接, 不发 connected(False) 避免状态闪烁
            if self._bus is not None:
                self._stop_tx_thread()
                with self._io_lock:
                    self._running = False
                    try:
                        self._bus.shutdown()
                    except Exception:
                        pass
                self._wake_event.set()

            if self._INTERFACE == "socketcan":
                # SocketCAN 的位时序由内核网络接口管理, 不向后端传 PCAN 参数。
                bus = can.Bus(interface="socketcan", channel=channel, fd=bool(fd))
            # PCAN FD 模式: 需要驱动 4.x+ 和 PCAN-USB FD 设备
            elif fd:
                try:
                    bus = can.Bus(
                        interface=self._INTERFACE,
                        channel=channel,
                        bitrate=bitrate,
                        data_bitrate=data_bitrate if data_bitrate > 0 else None,
                        fd=True,
                    )
                except (can.CanInterfaceNotImplementedError, can.CanInitializationError,
                        Exception) as e:
                    # FD 不可用: 给出明确的降级提示
                    self.error_occurred.emit(
                        f"CAN FD 模式不可用, 请确认:\n"
                        f"  1. PCAN-USB FD 设备已连接\n"
                        f"  2. PCAN Driver 版本 >= 4.x (下载: peak-system.com)\n"
                        f"  3. python-can 版本 >= 4.0\n"
                        f"原始错误: {e}"
                    )
                    return False
                # 检测实际设备类型: PCAN-USB(经典) 不支持 FD
                if hasattr(bus, 'channel_info') and bus.channel_info:
                    info = str(bus.channel_info)
                    if 'FD' not in info and 'fd' not in info.lower():
                        try:
                            bus.shutdown()
                        except Exception:
                            pass
                        self.error_occurred.emit(
                            f"设备 {info} 不支持 FD, 请使用 PCAN-USB FD"
                        )
                        return False
            else:
                bus = can.Bus(
                    interface=self._INTERFACE,
                    channel=channel,
                    bitrate=bitrate,
                )

            with self._io_lock:
                self._bus = bus
                self._channel = channel
                self._bitrate = bitrate
                self._motor_id = motor_id & 0xFF
                self._discovery_mode = False
                self._rx_segments.clear()
                self._rx_segments_ts.clear()
                self._rx_expected_seq.clear()
                self._tx_seq = 0
                self.reset_stats()
                self.tx_dropped_count = 0
                # 总线负载计数器清零(重连后重新统计)
                with self._bus_load_lock:
                    self._bus_load_bits = 0
                self._fd_capable = bool(fd)  # 硬件 FD 能力(由 UI 勾选决定)
                self._fd_mode = False        # 上电默认经典模式(与下位机一致)
                self._fd_ack_event.clear()
                self._fd_switch_done.clear()
                self._fd_ack_result = None
                self.rx_crc_errors = 0
                self.rx_seq_errors = 0
                self._running = True
            self._wake_event.set()
            # 启动发送线程
            self._start_tx_thread()
            self.connected.emit(True)
            return True
        except Exception as e:
            hint = ("\nLinux SocketCAN 通道需先配置并启动; "
                    "波特率由系统 ip link 设置, 然后在此选择 can0 等通道。"
                    if self._INTERFACE == "socketcan" else "")
            self.error_occurred.emit(f"CAN 打开失败: {e}{hint}")
            return False

    def close(self):
        was_running = self._running
        # 停止发送线程(先停发送, 再关总线, 避免发送线程访问已关闭的 bus)
        self._stop_tx_thread()
        with self._io_lock:
            self._running = False
            if self._bus is not None:
                try:
                    self._bus.shutdown()
                except Exception:
                    pass
                self._bus = None
            self._rx_segments.clear()
            self._rx_segments_ts.clear()
            self._discovery_mode = False
        self._wake_event.set()
        if was_running:
            self.connected.emit(False)

    def is_open(self) -> bool:
        return self._bus is not None and self._running

    # ---------------- 发送 ----------------
    def send(self, cmd: int, payload: bytes = b'') -> bool:
        """非阻塞发送: 入队后立即返回, 实际发送由 _tx_loop 线程完成。
        返回 True=入队成功; False=总线未开或队列满(丢弃并计数)。"""
        if not self.is_open():
            return False
        try:
            self._tx_queue.put_nowait((cmd, payload, self._motor_id))
            return True
        except queue.Full:
            self.tx_dropped_count += 1
            return False

    def send_to(self, node_id: int, cmd: int, payload: bytes = b'') -> bool:
        """向指定节点发送协议帧, 目标地址在入队时固定。"""
        if not self.is_open() or not 0 <= int(node_id) <= 127:
            return False
        try:
            self._tx_queue.put_nowait((cmd, payload, int(node_id)))
            return True
        except queue.Full:
            self.tx_dropped_count += 1
            return False

    def _start_tx_thread(self):
        """启动发送线程(open 时调用)。"""
        self._tx_stop.clear()
        # 清空残留队列(旧连接可能残留)
        while not self._tx_queue.empty():
            try:
                self._tx_queue.get_nowait()
            except queue.Empty:
                break
        self._tx_thread = threading.Thread(
            target=self._tx_loop, name=self._TX_THREAD_NAME, daemon=True
        )
        self._tx_thread.start()

    def _stop_tx_thread(self):
        """停止发送线程(close 时调用)。"""
        self._tx_stop.set()
        if self._tx_thread is not None:
            self._tx_thread.join(timeout=1.0)
            self._tx_thread = None

    def _tx_loop(self):
        """发送线程主循环: 阻塞取队列 → 组帧 → 写总线。"""
        while not self._tx_stop.is_set():
            try:
                cmd, payload, node_id = self._tx_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            # 0xF3 SET_FD_MODE 走同步发送(等待 ACK), 不走队列
            if cmd == JmCmd.SET_FD_MODE:
                self._send_fd_mode_sync(payload, node_id)
                continue
            # 实际发送(阻塞调用, 不影响 UI 线程)
            if cmd == JmCmd.MIT or cmd == JmCmd.IMPEDANCE:
                self._send_mit(cmd, payload, node_id)
            else:
                single_max = self._single_max()
                if len(payload) <= single_max and self._is_valid_single_length(len(payload)):
                    self._send_single(cmd, payload, node_id)
                else:
                    self._send_multi(cmd, payload, node_id)

    @property
    def fd_mode(self) -> bool:
        """当前运行期 FD 模式(与下位机同步)"""
        return self._fd_mode

    @property
    def fd_capable(self) -> bool:
        """上位机硬件 FD 能力"""
        return self._fd_capable

    def _single_max(self) -> int:
        return CAN_SINGLE_MAX_FD if self._fd_mode else CAN_SINGLE_MAX_CLS

    def _seg_payload(self) -> int:
        return CAN_SEG_PAYLOAD_FD if self._fd_mode else CAN_SEG_PAYLOAD_CLS

    def _is_valid_single_length(self, length: int) -> bool:
        return length <= CAN_SINGLE_MAX_CLS if not self._fd_mode else length in CAN_FD_DLC_LENGTHS

    def _make_fd_msg(self, can_id: int, data: bytes) -> can.Message:
        """根据当前 FD 模式构造 CAN 帧"""
        return can.Message(
            arbitration_id=can_id,
            data=data,
            is_extended_id=True,        # 29 位扩展帧
            is_fd=self._fd_mode,        # FD 模式下启用 FD 帧
            bitrate_switch=self._fd_mode,  # FD 模式下启用比特率切换
        )

    def switch_fd_mode(self, enable: bool) -> bool:
        """切换上下位机 FD 模式(同步等待 ACK)。
        返回 True=切换成功(上下位机均已切换), False=失败(超时或硬件不支持)。
        切换流程:
          1. UI 线程入队 (SET_FD_MODE, enable)
          2. _tx_loop 调用 _send_fd_mode_sync: 用当前模式发帧, 等待 ACK
          3. _handle_rx_msg 收到 0xF3 ACK 后 set _fd_ack_event
          4. _send_fd_mode_sync 读取 ACK, 切换自身 _fd_mode, set _fd_switch_done
          5. switch_fd_mode 返回最终结果
        注: ACK 用旧模式发出(下位机在 ACK 后才切换), 上位机收到 ACK 后切换。"""
        if not self.is_open():
            return False
        if enable and not self._fd_capable:
            return False
        # 清空完成事件, 入队由 _tx_loop 处理
        self._fd_switch_done.clear()
        try:
            self._tx_queue.put_nowait(
                (JmCmd.SET_FD_MODE, bytes([1 if enable else 0]), self._motor_id))
        except queue.Full:
            self.tx_dropped_count += 1
            return False
        # 等待 _tx_loop 完成切换(发帧 + 等 ACK + 切换模式)
        ok = self._fd_switch_done.wait(timeout=SWITCH_FD_ACK_TIMEOUT_S + 0.1)
        self._fd_switch_done.clear()
        if not ok or self._fd_ack_result is None:
            return False
        ack_enable, cap = self._fd_ack_result
        return ack_enable == (1 if enable else 0) and (cap == 1 if enable else True)

    def _send_fd_mode_sync(self, payload: bytes, node_id: int) -> None:
        """同步发送 0xF3 SET_FD_MODE 并等待 ACK(在 _tx_loop 中调用)。
        ACK 由 _handle_rx_msg 捕获并 set _fd_ack_event。
        完成后切换自身 _fd_mode 并 set _fd_switch_done 通知 UI 线程。"""
        # 清空旧结果, 准备等待新 ACK
        self._fd_ack_result = None
        self._fd_ack_event.clear()
        # 用当前模式发帧(切换前)
        self._send_single(JmCmd.SET_FD_MODE, payload, node_id)
        # 等待 ACK(由 _handle_rx_msg 捕获 0xF3 应答后 set 事件)
        ok = self._fd_ack_event.wait(timeout=SWITCH_FD_ACK_TIMEOUT_S)
        if ok and self._fd_ack_result is not None:
            ack_enable, _cap = self._fd_ack_result
            # 切换自身模式(与下位机 ack_enable 同步)
            self._fd_mode = bool(ack_enable)
        # 通知 UI 线程切换完成
        self._fd_switch_done.set()

    def _send_single(self, cmd: int, payload: bytes, node_id: int | None = None) -> bool:
        """单帧: 数据区直接是载荷(无控制字), 经典≤8B, FD≤64B"""
        target = self._motor_id if node_id is None else node_id
        can_id = can_make_id(cmd, target)
        msg = self._make_fd_msg(can_id, payload)
        return self._write_msg(msg)

    def _send_multi(self, cmd: int, payload: bytes, node_id: int | None = None) -> bool:
        """多帧分包: 每帧 data[0]=控制字, data[1..]=片段(经典7B/FD63B);
        末帧末尾追加 2 字节 CRC16(小端, 覆盖整个载荷)"""
        target = self._motor_id if node_id is None else node_id
        can_id = can_make_id(cmd, target) | CAN_MULTI_FLAG
        seg_payload = self._seg_payload()
        crc = crc16_calc(payload)
        payload_with_crc = payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
        offset = 0
        seq = 0
        total = len(payload_with_crc)
        ok = True
        while offset < total:
            remaining = total - offset
            chunk = seg_payload
            if self._fd_mode:
                chunk = next(size for size in CAN_FD_SEG_LENGTHS if size <= remaining)
            seg = payload_with_crc[offset:offset + chunk]
            offset += chunk
            is_last = offset >= total
            ctrl = (CAN_SEG_LAST if is_last else 0) | (seq & CAN_SEG_SEQ_MASK)
            frame_data = bytes([ctrl]) + seg
            msg = self._make_fd_msg(can_id, frame_data)
            ok = ok and self._write_msg(msg)
            seq += 1
        return ok

    def _write_msg(self, msg: can.Message) -> bool:
        with self._io_lock:
            try:
                if not (self._bus is not None and self._running):
                    return False
                self._bus.send(msg, timeout=self._WRITE_TIMEOUT_S)
                self.tx_bytes += msg.dlc
                self.tx_frames += 1
                # 总线负载统计: TX 帧也占用总线带宽
                self._accumulate_bus_load(
                    msg.dlc, msg.is_extended_id, getattr(msg, 'is_fd', False))
                # 物理帧日志(TX)
                self.raw_can_frame.emit(int(msg.arbitration_id), bytes(msg.data[:msg.dlc]), False, 0.0)
                return True
            except Exception as e:
                # P0-4: 发送失败告警
                self.error_occurred.emit(f"CAN 发送失败: {e}")
                return False

    # ---------------- MIT 压缩 ----------------
    @staticmethod
    def _float_to_raw(value: float, min_val: float, max_val: float, bits: int) -> int:
        """定点量化: raw = (value-min)/(max-min) * (2^bits - 1), 饱和到 [0, 2^bits-1]"""
        span = max_val - min_val
        if span <= 0:
            return 0
        raw = int((value - min_val) / span * ((1 << bits) - 1) + 0.5)
        return max(0, min((1 << bits) - 1, raw))

    @staticmethod
    def _raw_to_float(raw: int, min_val: float, max_val: float, bits: int) -> float:
        span = max_val - min_val
        return min_val + raw / ((1 << bits) - 1) * span

    def _pack_mit(self, pos: float, vel: float, kp: float, kd: float, tff: float) -> bytes:
        """MIT 命令 8 字节压缩: pos16 vel12 kp12 kd12 tff12 (大端)"""
        p = self._float_to_raw(pos, MIT_POS_MIN, MIT_POS_MAX, 16)
        v = self._float_to_raw(vel, MIT_VEL_MIN, MIT_VEL_MAX, 12)
        kp = self._float_to_raw(kp, MIT_KP_MIN, MIT_KP_MAX, 12)
        kd = self._float_to_raw(kd, MIT_KD_MIN, MIT_KD_MAX, 12)
        tf = self._float_to_raw(tff, MIT_TFF_MIN, MIT_TFF_MAX, 12)
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
        """反馈帧 8 字节: pos16 vel16 tq16 temp8 err8 → 还原为 READ_FEEDBACK 兼容载荷

        READ_FEEDBACK(0xC0) 串口帧载荷 22 字节(见 jmproto.feedback):
            pos(f32) + vel(f32) + torque(f32) + temp_motor(f32) + vbus(f32) + fault_mask(u16)
        CAN 压缩反馈帧无 vbus 字段, vbus 填 0.0; fault_mask 用 err 字节扩展。
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
        temp_motor = self._raw_to_float(temp, FB_TEMP_MIN, FB_TEMP_MAX, 8)
        # 小端打包: <5fH (5 个 float32 + 1 个 uint16) = 22 字节
        return struct.pack('<5fH', pos, vel, torque, temp_motor, 0.0, err)

    def _send_mit(self, cmd: int, payload: bytes, node_id: int | None = None) -> bool:
        """MIT/IMPEDANCE 命令: 把 payload 解析为 5 个 float, 压缩成 8 字节发送

        payload 假设为 5 个 float32 (pos, vel, kp, kd, tff), 共 20 字节。
        实际字段顺序/类型由 registry CommandSpec 决定, 联调时按 MIT 命令定义校准。
        """
        if len(payload) >= 20:
            pos, vel, kp, kd, tff = struct.unpack('<5f', payload[:20])
        else:
            pos = vel = kp = kd = tff = 0.0
        data = self._pack_mit(pos, vel, kp, kd, tff)
        return self._send_single(cmd, data, node_id)

    # ---------------- 后台收循环 ----------------
    @staticmethod
    def _estimate_frame_bits(dlc: int, extended: bool, fd: bool = False) -> int:
        """估算单个 CAN 帧在总线上的占用位长(含帧头+位填充+帧间隔)。

        用于总线负载率估算, 与 PCAN-View 等工具的口径对齐:
        - 经典扩展帧(29-bit ID), DLC=8: 约 154 位
        - 经典标准帧(11-bit ID), DLC=8: 约 128 位
        公式(CAN 2.0B, 含位填充):
            可填充位 = (扩展:49 + 8*DLC) | (标准:34 + 8*DLC)
              含 SOF/仲裁/IDE/r1/r0/DLC/数据/CRC
            填充位 ≈ (可填充位 - 1) // 4   (每 5 个相同位插 1 填充位)
            非填充固定 = 13   (CRC定界符1 + ACK1 + ACK定界符1 + EOF7 + ITF3)
            总位长 = 可填充位 + 填充位 + 非填充固定
        CAN FD 长帧公式不同, 此处粗略按经典公式估算(FD 场景负载本就高, 仅供参考)。
        """
        if fd:
            # FD 帧: 简化估算, 数据段按 64B 上限粗算(实际应按 BRS/header 另算)
            dlc_eff = min(dlc, 64)
            stuffable = 65 + 8 * dlc_eff
        elif extended:
            stuffable = 49 + 8 * dlc
        else:
            stuffable = 34 + 8 * dlc
        stuff_bits = max(0, (stuffable - 1) // 4)
        return stuffable + stuff_bits + 13

    def _accumulate_bus_load(self, dlc: int, extended: bool, fd: bool = False):
        """累加一帧到总线负载计数器(rx/tx 线程调用)"""
        bits = self._estimate_frame_bits(dlc, extended, fd)
        with self._bus_load_lock:
            self._bus_load_bits += bits

    def get_and_reset_bus_load_bits(self) -> int:
        """取样并清零总线负载累计位长(UI 线程周期调用)"""
        with self._bus_load_lock:
            val = self._bus_load_bits
            self._bus_load_bits = 0
        return val

    def _run(self):
        while self._alive:
            try:
                if self._running and self._bus is not None:
                    # 持锁内只做最小句柄访问(recv), 解帧/emit 放锁外
                    with self._io_lock:
                        if self._bus is None or not self._running:
                            continue
                        msg = self._bus.recv(timeout=self._READ_TIMEOUT_S)
                    if msg is not None:
                        # 过滤 TX 回环帧: PCAN 驱动可能回放本机发送的帧(MSGTYPE.ECHO 位),
                        # 若不过滤, 自己发的 MIT/IMPEDANCE 命令会被误当反馈帧/ACK 处理,
                        # 导致状态乱闪。python-can Message.is_rx 基于该位判断方向。
                        if not getattr(msg, 'is_rx', True):
                            continue
                        # 错误帧/远程帧不再静默丢弃
                        if msg.is_error_frame:
                            self.error_occurred.emit(
                                f"CAN 错误帧 ID:0x{msg.arbitration_id:X} DLC:{msg.dlc}")
                            self.rx_frames += 1
                            continue
                        if msg.is_remote_frame:
                            self.raw_can_frame.emit(
                                int(msg.arbitration_id), b'', True, msg.timestamp or 0.0)
                            self.rx_frames += 1
                            # 远程帧也占总线: DLC=0, 但有帧头开销
                            self._accumulate_bus_load(
                                0, msg.is_extended_id, getattr(msg, 'is_fd', False))
                            continue
                        # 总线负载统计: 在 motor_id 过滤前累加, 计入总线上所有 RX 帧
                        self._accumulate_bus_load(
                            msg.dlc, msg.is_extended_id, getattr(msg, 'is_fd', False))
                        self.rx_bytes += msg.dlc
                        self._handle_rx_msg(msg)
                    else:
                        # 空闲时周期检查总线状态(每 500ms) + 清理超时多帧缓冲
                        now = time.time()
                        if now - self._bus_state_check_ts > 0.5:
                            self._bus_state_check_ts = now
                            self._check_bus_state()
                        self._cleanup_stale_segments()
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

    def _handle_rx_msg(self, msg: can.Message):
        """收到一个 CAN 物理帧 → 拆 ID → 单帧/重组多帧/MIT解压 → emit 逻辑帧

        单帧/多帧判断(与固件 jm_proto_can.c can_emit_payload 一致):
        - 载荷≤single_max: 单帧, 数据直接是载荷(无控制字)
        - 载荷>single_max: 多帧, data[0]=控制字(bit7=末帧, bit6:0=序号), data[1..]=片段
        接收侧不依赖命令白名单, 基于 DLC 和控制字特征智能判断。
        """
        can_id = int(msg.arbitration_id)
        cmd = can_get_cmd(can_id)
        data = bytes(msg.data[:msg.dlc])

        # 物理帧日志(在过滤前发射, 让所有帧都进日志)
        self.raw_can_frame.emit(can_id, data, True, msg.timestamp or 0.0)

        # 监听模式 — 只记物理帧日志, 不解析协议帧
        if self._listen_only:
            return

        # 地址过滤: 正常模式仅接收当前节点/广播; 发现模式额外接收各节点设备信息。
        rx_motor_id = can_get_motor_id(can_id)
        discovery_reply = (self._discovery_mode
                           and cmd in (JmCmd.READ_DEV_INFO,
                                       JmCmd.CAN_DI_DISCOVER,
                                       JmCmd.CAN_DI_SET_ID,
                                       JmCmd.CAN_DI_IDENTIFY)
                           and 1 <= rx_motor_id <= 127)
        if rx_motor_id != 0 and rx_motor_id != self._motor_id and not discovery_reply:
            return

        # 0xF3 SET_FD_MODE 应答捕获: ACK{ack_enable, cap} (2 字节)
        # 由 switch_fd_mode 同步等待, 不走 frame_received 信号
        if cmd == JmCmd.SET_FD_MODE and len(data) >= 2:
            self._fd_ack_result = (data[0], data[1])
            self._fd_ack_event.set()
            self.rx_frames += 1
            return

        # READ_FEEDBACK(0xC0) 压缩反馈帧: 8 字节压缩 → 解压为 22 字节兼容载荷
        # 注: MIT(0x13)/IMPEDANCE(0x30) 命令帧也是 8 字节, 但那是主机→电机的控制帧,
        # 固件不回发(CSV 协议表 Motor->Host 方向只有 0xCA/0xFE)。
        # 若 PCAN 开了本地回环, 收到的 0x13/0x30 是自己发的命令帧, 误当反馈帧解压会
        # 把命令目标值当成实际位置/速度, 还产生假故障码 → 状态乱闪。故只认 0xC0。
        if cmd == JmCmd.READ_FEEDBACK and len(data) == 8:
            payload = self._unpack_mit_feedback(data)
            self.rx_frames += 1
            self._emit_protocol_frame(can_id, JmCmd.READ_FEEDBACK, payload)
            return

        # 多帧重组: 基于控制字和 DLC 智能判断(不依赖命令白名单)
        if (can_id & CAN_MULTI_FLAG) and self._handle_multi_frame(can_id, cmd, data):
            return

        # 单帧: data 即为载荷(不含 CMD, CMD 走仲裁 ID)
        self.rx_frames += 1
        self._emit_protocol_frame(can_id, cmd, data)

    def _emit_protocol_frame(self, can_id: int, cmd: int, payload: bytes):
        """保留 CAN 源节点；扫描响应不进入当前设备的普通业务分发。"""
        node_id = can_get_motor_id(can_id)
        self.addressed_frame_received.emit(node_id, int(cmd), bytes(payload))
        is_current_target = node_id == 0 or node_id == self._motor_id
        commissioning_cmd = cmd in (JmCmd.CAN_DI_DISCOVER,
                                    JmCmd.CAN_DI_SET_ID,
                                    JmCmd.CAN_DI_IDENTIFY)
        # Commissioning responses have their own addressed signals. A normal
        # READ_DEV_INFO for the selected node must remain usable while the
        # non-modal device manager keeps discovery mode enabled.
        if is_current_target and not commissioning_cmd:
            self.frame_received.emit(int(cmd), bytes(payload))

    def _handle_multi_frame(self, can_id: int, cmd: int, data: bytes) -> bool:
        """多帧重组状态机, 返回 True=已处理, False=单帧。

        多帧格式: data[0]=控制字(bit7=末帧, bit6:0=序号), data[1..]=片段
        - 经典: 每帧片段7B, 单帧DLC=8
        - FD: 每帧片段63B, 单帧DLC可达64
        - 末帧末尾追加 2 字节 CRC16(小端), 校验通过后去除 CRC 再 emit

        seq 连续性检查: 收到乱序帧(seq != expected_seq)时丢弃整个多帧,
        避免帧丢失导致数据错乱和 CRC 必然失败。
        """
        if len(data) == 0:
            return False

        ctrl = data[0]
        is_last = bool(ctrl & CAN_SEG_LAST)
        seq = ctrl & CAN_SEG_SEQ_MASK
        seg_payload = self._seg_payload()
        seg = data[1:1 + seg_payload]

        # seq==0 且非末帧且DLC==single_max: 新一轮多帧开始
        # (旧缓冲未完成则覆盖, 避免乱序帧污染)
        # CAN_MULTI_FLAG removes single-frame ambiguity. A transfer can start
        # and finish in one frame when that produces a valid CAN FD DLC.
        if seq == 0 and is_last:
            buf = bytearray(seg)
            if len(buf) < 2:
                return True
            recv_crc = buf[-2] | (buf[-1] << 8)
            calc_crc = crc16_calc(bytes(buf[:-2]))
            if recv_crc != calc_crc:
                self.rx_crc_errors += 1
                return True
            self.rx_frames += 1
            self._emit_protocol_frame(can_id, cmd, bytes(buf[:-2]))
            return True

        if seq == 0:
            self._rx_segments[can_id] = bytearray(seg)
            self._rx_segments_ts[can_id] = time.time()
            self._rx_expected_seq[can_id] = 1
            return True

        # 有正在重组的缓冲 → 检查 seq 连续性
        if can_id in self._rx_segments:
            expected_seq = self._rx_expected_seq.get(can_id, 0)

            # seq 不连续 → 帧丢失, 丢弃整个多帧
            if seq != expected_seq:
                self.rx_seq_errors += 1
                self._rx_segments.pop(can_id, None)
                self._rx_segments_ts.pop(can_id, None)
                self._rx_expected_seq.pop(can_id, None)
                return True

            if is_last:
                buf = self._rx_segments.pop(can_id, bytearray())
                self._rx_segments_ts.pop(can_id, None)
                self._rx_expected_seq.pop(can_id, None)
                buf.extend(seg)
                # CRC 校验(末 2 字节小端)
                if len(buf) >= 2:
                    recv_crc = buf[-2] | (buf[-1] << 8)
                    calc_crc = crc16_calc(bytes(buf[:-2]))
                    if recv_crc != calc_crc:
                        self.rx_crc_errors += 1
                        return True
                    buf = buf[:-2]
                self.rx_frames += 1
                self._emit_protocol_frame(can_id, cmd, bytes(buf))
                return True
            else:
                self._rx_segments[can_id].extend(seg)
                self._rx_segments_ts[can_id] = time.time()
                self._rx_expected_seq[can_id] = seq + 1
                return True

        # 无缓冲: 单帧(包括孤立末帧), 返回 False 让调用方当单帧处理
        return False

    def _cleanup_stale_segments(self):
        """清理超时的多帧缓冲(100ms未完成则回退为单帧emit)。

        防止误判为多帧第一帧导致数据永久滞留。
        超时后把缓冲内容作为单帧载荷 emit(补偿 data[0]=0x00 + fragment)。
        """
        if not self._rx_segments_ts:
            return
        now = time.time()
        stale_cids = [cid for cid, ts in self._rx_segments_ts.items()
                      if now - ts > 0.1]
        for cid in stale_cids:
            buf = self._rx_segments.pop(cid, bytearray())
            self._rx_segments_ts.pop(cid, None)
            self._rx_expected_seq.pop(cid, None)
            if buf:
                cmd = can_get_cmd(cid)
                self.rx_frames += 1
                self._emit_protocol_frame(cid, cmd, bytes([0x00]) + bytes(buf))

    # ---------------- 总线状态(P1-1/P1-2) ----------------
    def _check_bus_state(self):
        """收循环空闲时读取总线状态, 变化时 emit。不阻塞 recv。"""
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
            tec = getattr(stats, 'tec', 0) if stats else 0
            rec = getattr(stats, 'rec', 0) if stats else 0
            if state_str != self._last_bus_state:
                self._last_bus_state = state_str
                if state_str == "BUSOFF":
                    self.error_occurred.emit("CAN 总线 BUS_OFF")
                self.bus_state_changed.emit(state_str, tec, rec)
        except Exception:
            pass

    def get_bus_state(self) -> tuple:
        """供 UI 主动查询当前总线状态 (state_str, tec, rec)"""
        return (self._last_bus_state, 0, 0)

    # ---------------- 调试辅助(P2-1/P2-2) ----------------
    def set_listen_only(self, enabled: bool):
        """P2-1 监听模式: True=只记物理帧日志, 不解析协议帧"""
        self._listen_only = enabled

    def set_discovery_mode(self, enabled: bool):
        """允许/禁止接收任意节点的设备发现和CAN-DI响应。"""
        self._discovery_mode = bool(enabled)

    def set_motor_id(self, node_id: int) -> bool:
        """切换当前单播目标和接收软件过滤地址, 不重启物理总线。"""
        if not 1 <= int(node_id) <= 127:
            return False
        self._motor_id = int(node_id)
        return True

    @property
    def bitrate(self) -> int:
        """P1-3 暴露当前比特率, 供 UI 算总线占用率"""
        return self._bitrate

    def send_raw(self, can_id: int, data: bytes, extended: bool = True) -> bool:
        """P2-2 手动发送原始 CAN 帧(调试用), 不走协议组帧。
        经典模式截断到 8 字节, FD 模式截断到 64 字节。"""
        max_len = 64 if self._fd_mode else 8
        msg = self._make_fd_msg(can_id, bytearray(data[:max_len]))
        # send_raw 允许覆盖 is_extended_id(调试用)
        msg.is_extended_id = extended
        return self._write_msg(msg)
