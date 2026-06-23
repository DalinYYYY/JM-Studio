"""
CRC16-CCITT/XMODEM 校验

必须与固件 User/Common/crc16.c 完全一致:
  多项式 0x1021, 初值 0x0000, 高位在前(MSB-first), 无输入/输出反转, 无最终异或。
固件核心: crc = (crc << 8) ^ table[((crc >> 8) ^ byte) & 0xFF]
"""

_CRC_TABLE = []


def _build_crc_table():
    global _CRC_TABLE
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
        _CRC_TABLE.append(crc)


_build_crc_table()


def crc16_calc(data: bytes) -> int:
    """计算 CRC16-CCITT/XMODEM, 返回16位校验值(与固件 crc16_calc 一致)"""
    crc = 0x0000
    for byte in data:
        crc = ((crc << 8) ^ _CRC_TABLE[((crc >> 8) ^ byte) & 0xFF]) & 0xFFFF
    return crc & 0xFFFF
