"""
串口协议帧编解码

帧格式: A5 5A + Length(2B, 大端) + HeaderChk(1B) + Data(CMD+Payload) + CRC16(2B, 小端)
- LEN = CMD(1)+DATA(n) 的总字节数(大端)
- HDR_CHK = (STX_H+STX_L+LEN_H+LEN_L) & 0xFF
- CRC16(XMODEM) 覆盖数据区(CMD+DATA), 小端落帧
与固件 packer_parser + crc16(XMODEM) 一致。
"""

from enum import IntEnum

from .crc16 import crc16_calc


# ==================== 帧格式常量 ====================
STX_H = 0xA5
STX_L = 0x5A
MAX_PACK_SIZE = 1024
PACK_OVERHEAD = 7  # 帧头2 + 长度2 + 头校验1 + CRC2


class FrameCodec:
    """串口协议帧封包器"""

    @staticmethod
    def pack(cmd: int, payload: bytes = b'') -> bytes:
        """封装一帧: A5 5A + Len(大端) + HeadChk + CMD+Payload + CRC16(小端)"""
        data = bytes([cmd]) + payload
        length = len(data)
        head_chk = (STX_H + STX_L + ((length >> 8) & 0xFF) + (length & 0xFF)) & 0xFF
        crc = crc16_calc(data)

        frame = bytearray()
        frame.append(STX_H)
        frame.append(STX_L)
        frame.append((length >> 8) & 0xFF)
        frame.append(length & 0xFF)
        frame.append(head_chk)
        frame.extend(data)
        frame.append(crc & 0xFF)
        frame.append((crc >> 8) & 0xFF)
        return bytes(frame)


class FrameDecoder:
    """串口协议帧解包器(状态机)"""

    class State(IntEnum):
        HEADER_HIGH = 0
        HEADER_LOW = 1
        LEN_HIGH = 2
        LEN_LOW = 3
        HEADER_CHK = 4
        DATA_RECV = 5
        CRC_LOW = 6
        CRC_HIGH = 7

    def __init__(self):
        self._state = self.State.HEADER_HIGH
        self._calc = 0
        self._flen = 0
        self._cnt = 0
        self._data = bytearray(MAX_PACK_SIZE)
        self._rx_crc = 0

    def reset(self):
        """复位状态机(打开串口时调用, 清除残留半帧)"""
        self._state = self.State.HEADER_HIGH
        self._cnt = 0

    def feed(self, raw: bytes):
        """喂入原始字节, 解出完整帧后 yield (cmd, payload)"""
        for d in raw:
            result = self._decode(d)
            if result is not None:
                yield result

    def _decode(self, d: int):
        if self._state == self.State.HEADER_HIGH:
            if d == STX_H:
                self._calc = STX_H
                self._state = self.State.HEADER_LOW

        elif self._state == self.State.HEADER_LOW:
            if d == STX_L:
                self._calc += STX_L
                self._state = self.State.LEN_HIGH
            else:
                self._state = self.State.HEADER_HIGH

        elif self._state == self.State.LEN_HIGH:
            self._flen = (d << 8)
            self._calc += d
            self._state = self.State.LEN_LOW

        elif self._state == self.State.LEN_LOW:
            self._flen |= d
            self._calc += d
            if self._flen > MAX_PACK_SIZE:
                self._state = self.State.HEADER_HIGH
            else:
                self._state = self.State.HEADER_CHK

        elif self._state == self.State.HEADER_CHK:
            if d != (self._calc & 0xFF):
                self._state = self.State.HEADER_HIGH
                return None
            self._cnt = 0
            if self._flen == 0:
                self._state = self.State.CRC_LOW
            else:
                self._state = self.State.DATA_RECV

        elif self._state == self.State.DATA_RECV:
            self._data[self._cnt] = d
            self._cnt += 1
            if self._cnt >= self._flen:
                self._state = self.State.CRC_LOW

        elif self._state == self.State.CRC_LOW:
            self._rx_crc = d
            self._state = self.State.CRC_HIGH

        elif self._state == self.State.CRC_HIGH:
            self._rx_crc |= (d << 8)
            self._state = self.State.HEADER_HIGH
            data_bytes = bytes(self._data[:self._flen])
            calc_crc = crc16_calc(data_bytes)
            if calc_crc == self._rx_crc and self._flen > 0:
                cmd = self._data[0]
                payload = bytes(self._data[1:self._flen])
                return cmd, payload
            return None

        return None
