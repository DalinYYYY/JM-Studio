"""
CAN 传输实现(占位, 后续实现)

设计预留(依据固件 jm_proto_can.h):
- 仲裁 ID(扩展帧29位): ID = (CMD << 8) | motor_id; CMD = (id >> 8) & 0xFF; motor_id = id & 0xFF
- 数据区直接是命令载荷(不含 CMD), 单帧最多 8 字节
- >8 字节载荷多帧分包: data[0]=控制字=(末帧标志<<7)|(序号&0x7F), data[1..]=片段(每帧≤7字节)
  常量: JM_CAN_SEG_LAST=0x80, JM_CAN_SEG_SEQ_MASK=0x7F, JM_CAN_SEG_PAYLOAD=7, JM_CAN_SINGLE_MAX=8
- MIT(0x13/0x30) 定点压缩进 8 字节: pos16 vel12 kp12 kd12 tff12
  范围: pos[-12.5,12.5] vel[-65,65] kp[0,500] kd[0,5] tff[-50,50]
  raw = (value-min)/(max-min)*(2^bits-1)
- 反馈帧压缩进 8 字节: pos16 vel16 tq16 temp8 err8, temp 线性映射 [-40,215]
- 广播地址 motor_id=0(不应答)

实现时: 用 python-can(或厂商 SDK) 打开通道, 收到 CAN 帧 -> 拆 ID 取 CMD -> 单帧/重组多帧/
MIT 解压 -> frame_received.emit(cmd, payload); 发送时按 cmd 决定压缩/分包后写 CAN。
"""

from .base import Transport


# ---- MIT 定点压缩范围(与 jm_proto_can.h 一致, 实现时使用) ----
MIT_POS_MIN, MIT_POS_MAX = -12.5, 12.5
MIT_VEL_MIN, MIT_VEL_MAX = -65.0, 65.0
MIT_KP_MIN, MIT_KP_MAX = 0.0, 500.0
MIT_KD_MIN, MIT_KD_MAX = 0.0, 5.0
MIT_TFF_MIN, MIT_TFF_MAX = -50.0, 50.0
FB_TEMP_MIN, FB_TEMP_MAX = -40.0, 215.0

# ---- 多帧分包常量 ----
CAN_SEG_LAST = 0x80
CAN_SEG_SEQ_MASK = 0x7F
CAN_SEG_PAYLOAD = 7
CAN_SINGLE_MAX = 8


def can_make_id(cmd: int, motor_id: int) -> int:
    return ((cmd & 0xFF) << 8) | (motor_id & 0xFF)


def can_get_cmd(can_id: int) -> int:
    return (can_id >> 8) & 0xFF


def can_get_motor_id(can_id: int) -> int:
    return can_id & 0xFF


class CanTransport(Transport):
    """CAN 传输(待实现)"""

    name = "can"

    def __init__(self):
        super().__init__()
        self._open = False
        self._motor_id = 1

    def start(self):
        # 线程在实现时启动; 占位阶段空操作
        pass

    def stop(self):
        self.close()

    def open(self, channel: str = "", bitrate: int = 1000000, motor_id: int = 1, **_) -> bool:
        raise NotImplementedError("CAN 传输待实现(见本文件顶部设计预留)")

    def close(self):
        self._open = False

    def is_open(self) -> bool:
        return self._open

    def send(self, cmd: int, payload: bytes = b'') -> bool:
        raise NotImplementedError("CAN 传输待实现")
