"""
小端编解码助手 + 按类型名打包/解包

多字节数值统一小端, 浮点为 IEEE-754 f32, 与固件 jm_proto.h 的 jm_rd_*/jm_wr_* 一致。
"""

import struct


# ==================== 读取(小端) ====================
def rd_u8(p: bytes, offset: int = 0) -> int:
    return p[offset]

def rd_i8(p: bytes, offset: int = 0) -> int:
    return struct.unpack_from('<b', p, offset)[0]

def rd_u16(p: bytes, offset: int = 0) -> int:
    return struct.unpack_from('<H', p, offset)[0]

def rd_i16(p: bytes, offset: int = 0) -> int:
    return struct.unpack_from('<h', p, offset)[0]

def rd_u32(p: bytes, offset: int = 0) -> int:
    return struct.unpack_from('<I', p, offset)[0]

def rd_i32(p: bytes, offset: int = 0) -> int:
    return struct.unpack_from('<i', p, offset)[0]

def rd_f32(p: bytes, offset: int = 0) -> float:
    return struct.unpack_from('<f', p, offset)[0]


# ==================== 写入(小端) ====================
def wr_u8(v: int) -> bytes:
    return struct.pack('<B', v & 0xFF)

def wr_i8(v: int) -> bytes:
    return struct.pack('<b', v)

def wr_u16(v: int) -> bytes:
    return struct.pack('<H', v & 0xFFFF)

def wr_i16(v: int) -> bytes:
    return struct.pack('<h', v)

def wr_u32(v: int) -> bytes:
    return struct.pack('<I', v & 0xFFFFFFFF)

def wr_i32(v: int) -> bytes:
    return struct.pack('<i', v)

def wr_f32(v: float) -> bytes:
    return struct.pack('<f', v)


# ==================== 类型名 -> 编解码 ====================
# 数据类型字符串(来自 CSV / 参数表)到 (字节数, 是否浮点, struct格式) 的映射
TYPE_INFO = {
    'u8':  (1, False, '<B'),
    'i8':  (1, False, '<b'),
    'u16': (2, False, '<H'),
    'i16': (2, False, '<h'),
    'u32': (4, False, '<I'),
    'i32': (4, False, '<i'),
    'f32': (4, True,  '<f'),
}


def type_nbytes(dtype: str) -> int:
    """返回类型字节数; char[N] 返回 N; 未知返回 4"""
    dtype = dtype.strip().lower()
    if dtype in TYPE_INFO:
        return TYPE_INFO[dtype][0]
    if dtype.startswith('char[') and dtype.endswith(']'):
        try:
            return int(dtype[5:-1])
        except ValueError:
            return 0
    return 4


def is_float_type(dtype: str) -> bool:
    info = TYPE_INFO.get(dtype.strip().lower())
    return bool(info and info[1])


def pack_value(dtype: str, value) -> bytes:
    """按类型名把单个数值打包成小端字节(char[N] 按 utf-8 定长补零)"""
    dtype = dtype.strip().lower()
    if dtype in TYPE_INFO:
        _, is_float, fmt = TYPE_INFO[dtype]
        if is_float:
            return struct.pack(fmt, float(value))
        return struct.pack(fmt, int(value))
    if dtype.startswith('char['):
        n = type_nbytes(dtype)
        raw = str(value).encode('utf-8')[:n]
        return raw.ljust(n, b'\x00')
    # 未知类型: 当 f32 处理
    return struct.pack('<f', float(value))


def unpack_value(dtype: str, raw: bytes):
    """按类型名从小端字节解出数值; char[N] 返回去尾零的字符串"""
    dtype = dtype.strip().lower()
    if dtype in TYPE_INFO:
        nbytes, _, fmt = TYPE_INFO[dtype]
        if len(raw) < nbytes:
            return None
        return struct.unpack(fmt, raw[:nbytes])[0]
    if dtype.startswith('char['):
        return raw.rstrip(b'\x00').decode('utf-8', errors='replace')
    return None
