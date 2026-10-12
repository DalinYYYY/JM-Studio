"""经典 CAN 轮廓参数事务与短控制帧编码。所有多字节字段为小端。"""

import struct

from .cmd_def import JmCmd

WRITE, COMMIT, READ, STATUS, ABORT = range(1, 6)

PROFILE_FIELDS = {
    JmCmd.PP: ((1, 'vel'), (2, 'acc'), (3, 'dec'), (4, 'jerk')),
    JmCmd.PV: ((5, 'dec'), (6, 'current_lim')),
    JmCmd.PT: ((7, 'current_lim'), (8, 'vel_lim')),
    JmCmd.JOG: ((9, 'acc'), (10, 'dec')),
}


def settings_for(cmd: int, values: dict) -> tuple:
    """返回与设备 float32 位模式一致的设定值，供缓存比较与读回校验。"""
    return tuple((field, struct.pack('<f', float(values.get(name, 0))))
                 for field, name in PROFILE_FIELDS[JmCmd(cmd)])


def pack_write(field: int, token: int, value: bytes) -> bytes:
    if not 1 <= field <= 10 or not 1 <= token <= 0xFFFF or len(value) != 4:
        raise ValueError('轮廓配置写入参数无效')
    return struct.pack('<BBH', WRITE, field, token) + value


def pack_commit(token: int) -> bytes:
    return struct.pack('<BH', COMMIT, token)


def pack_read(field: int) -> bytes:
    if not 1 <= field <= 10:
        raise ValueError('轮廓配置字段无效')
    return bytes((READ, field))


def pack_pp_short(values: dict, revision: int, seq: int) -> bytes:
    if not 1 <= revision <= 0xFFFF or not 1 <= seq <= 0xFF:
        raise ValueError('PP 配置版本或序号无效')
    return struct.pack('<BfHB', int(values['flags']), float(values['pos']), revision, seq)


def pack_short(cmd: int, values: dict, revision: int = 0, seq: int = 0) -> bytes:
    cmd = JmCmd(cmd)
    if cmd == JmCmd.PP:
        return pack_pp_short(values, revision, seq)
    if cmd == JmCmd.PV:
        return struct.pack('<2f', float(values['vel']), float(values['acc']))
    if cmd == JmCmd.PT:
        return struct.pack('<2f', float(values['torque']), float(values['slope']))
    if cmd == JmCmd.JOG:
        return struct.pack('<bf', int(values['dir']), float(values['speed']))
    raise ValueError('该命令无经典 CAN 短帧')


def parse_reply(payload: bytes) -> dict:
    """0x5F 应答没有通用 ACK 状态字；错误由 0xFE NACK 表示。"""
    if not payload:
        raise ValueError('空轮廓配置应答')
    op = payload[0]
    if op == WRITE and len(payload) == 4:
        return {'op': op, 'field': payload[1], 'token': struct.unpack_from('<H', payload, 2)[0]}
    if op == COMMIT and len(payload) == 5:
        rev, token = struct.unpack_from('<HH', payload, 1)
        return {'op': op, 'revision': rev, 'token': token}
    if op == READ and len(payload) == 8:
        return {'op': op, 'field': payload[1],
                'revision': struct.unpack_from('<H', payload, 2)[0],
                'value': payload[4:8]}
    if op == STATUS and len(payload) == 4:
        return {'op': op, 'revision': struct.unpack_from('<H', payload, 1)[0],
                'pp_valid': bool(payload[3] & 1)}
    if op == ABORT and len(payload) == 4 and payload[3] == 0:
        return {'op': op, 'token': struct.unpack_from('<H', payload, 1)[0]}
    raise ValueError(f'轮廓配置应答长度/操作码无效: op={op}, len={len(payload)}')
