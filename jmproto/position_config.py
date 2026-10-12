"""0x08 设备零位协议：位置均为输出轴 rad，请求 1/5B、应答 8B，经典 CAN 单帧。

CONFIGURED 表示零位偏移有效（默认 0 rad 合法）；VALID 表示当前计算坐标可用，
不代表单圈编码器已经恢复断电前的累计圈数。
"""
import math
import struct

STATUS = 0
ZERO_CURRENT = 1
ZERO_AT = 2
RESTORE = 3
CONFIGURED = 1
VALID = 2
READY = 4
SAVED = 8


def pack_request(op: int, value: float = 0.0) -> bytes:
    """打包操作；op0/1 只发送操作码，op2/3 追加有限 float32 输出轴位置。"""
    if op not in (STATUS, ZERO_CURRENT, ZERO_AT, RESTORE):
        raise ValueError('未知零位操作')
    if op < ZERO_AT:
        return bytes((op,))
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('位置必须为有限数值')
    try:
        return struct.pack('<Bf', op, value)
    except (OverflowError, struct.error) as exc:
        raise ValueError('位置超出 float32 范围') from exc


def parse_reply(payload: bytes) -> tuple[int, int, float]:
    """校验完整应答并返回操作、状态和 rad 值；op3 的值是位置，其余是零位偏移。"""
    if len(payload) != 8:
        raise ValueError('零位应答长度必须为8字节')
    op, flags, reserved, value = struct.unpack('<BBHf', payload)
    if op > RESTORE or flags & ~15 or reserved or not math.isfinite(value):
        raise ValueError('零位应答数据无效')
    return op, flags, value
