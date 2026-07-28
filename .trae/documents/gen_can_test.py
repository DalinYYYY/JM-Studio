"""CAN 接收测试命令生成器 - 计算测试用例的具体字节"""

# MIT 压缩范围 (与 can_transport.py 一致)
MIT_POS_MIN, MIT_POS_MAX = -12.5, 12.5
MIT_VEL_MIN, MIT_VEL_MAX = -65.0, 65.0
MIT_TFF_MIN, MIT_TFF_MAX = -50.0, 50.0
FB_TEMP_MIN, FB_TEMP_MAX = -40.0, 215.0


def f2r(v, lo, hi, bits):
    span = hi - lo
    raw = int((v - lo) / span * ((1 << bits) - 1) + 0.5)
    return max(0, min((1 << bits) - 1, raw))


def r2f(raw, lo, hi, bits):
    return lo + raw / ((1 << bits) - 1) * (hi - lo)


def hex_fmt(data):
    return data.hex(" ").upper()


# === 测试用例1: pos=1.0 vel=10.0 tq=5.0 temp=60 err=0 ===
pos, vel, tq, temp, err = 1.0, 10.0, 5.0, 60, 0
p = f2r(pos, MIT_POS_MIN, MIT_POS_MAX, 16)
v = f2r(vel, MIT_VEL_MIN, MIT_VEL_MAX, 16)
t = f2r(tq, MIT_TFF_MIN, MIT_TFF_MAX, 16)
tp = f2r(temp, FB_TEMP_MIN, FB_TEMP_MAX, 8)
val = (p << 48) | (v << 32) | (t << 16) | (tp << 8) | (err & 0xFF)
data = val.to_bytes(8, "big")
print("=== 测试1: MIT压缩反馈帧 (pos=1.0 vel=10.0 tq=5.0 temp=60 err=0) ===")
print("CAN ID: 0xC001 (READ_FEEDBACK, motor_id=1)")
print("DLC: 8")
print("Data:", hex_fmt(data))
# 验证解压
val2 = int.from_bytes(data, "big")
p2 = (val2 >> 48) & 0xFFFF
v2 = (val2 >> 32) & 0xFFFF
t2 = (val2 >> 16) & 0xFFFF
tp2 = (val2 >> 8) & 0xFF
e2 = val2 & 0xFF
print("解压验证: pos=%.3f vel=%.3f tq=%.3f temp=%.3f err=%d" % (
    r2f(p2, MIT_POS_MIN, MIT_POS_MAX, 16),
    r2f(v2, MIT_VEL_MIN, MIT_VEL_MAX, 16),
    r2f(t2, MIT_TFF_MIN, MIT_TFF_MAX, 16),
    r2f(tp2, FB_TEMP_MIN, FB_TEMP_MAX, 8),
    e2))
print()

# === 测试用例2: pos=0 vel=0 tq=0 temp=25 err=0 (零点) ===
pos, vel, tq, temp, err = 0.0, 0.0, 0.0, 25, 0
p = f2r(pos, MIT_POS_MIN, MIT_POS_MAX, 16)
v = f2r(vel, MIT_VEL_MIN, MIT_VEL_MAX, 16)
t = f2r(tq, MIT_TFF_MIN, MIT_TFF_MAX, 16)
tp = f2r(temp, FB_TEMP_MIN, FB_TEMP_MAX, 8)
val = (p << 48) | (v << 32) | (t << 16) | (tp << 8) | (err & 0xFF)
data = val.to_bytes(8, "big")
print("=== 测试2: MIT压缩反馈帧 (pos=0 vel=0 tq=0 temp=25 err=0 零点) ===")
print("CAN ID: 0xC001 (READ_FEEDBACK, motor_id=1)")
print("DLC: 8")
print("Data:", hex_fmt(data))
print()

# === 测试用例3: 多帧 MOTOR_INFO_READ (16字节载荷) ===
print("=== 测试3: 多帧 MOTOR_INFO_READ (16字节载荷) ===")
print("CAN ID: 0xE601 (MOTOR_INFO_READ, motor_id=1)")
payload = bytes(range(16))  # 00 01 02 ... 0F
print("目标载荷(16B):", hex_fmt(payload))
# 帧1: seq=0 非末帧
f1 = bytes([0x00]) + payload[0:7]
print("帧1: DLC=8 Data=", hex_fmt(f1))
# 帧2: seq=1 非末帧
f2 = bytes([0x01]) + payload[7:14]
print("帧2: DLC=8 Data=", hex_fmt(f2))
# 帧3: seq=2 末帧
f3 = bytes([0x80 | 0x02]) + payload[14:16]
print("帧3: DLC=3 Data=", hex_fmt(f3))
print()

# === 测试用例4: 单帧 READ_STATE ===
print("=== 测试4: 单帧 READ_STATE (4字节) ===")
print("CAN ID: 0xC101 (READ_STATE, motor_id=1)")
print("DLC: 4")
print("Data: 03 00 00 00  (top_fsm=IDLE run_state=IDLE ctrl_mode=0 enable=0)")
print()

# === 测试用例5: 单帧 NACK ===
print("=== 测试5: 单帧 NACK (2字节) ===")
print("CAN ID: 0xFE01 (NACK, motor_id=1)")
print("DLC: 2")
print("Data: E0 02  (对 PARAM_READ 的 OUT_OF_RANGE 拒绝)")
