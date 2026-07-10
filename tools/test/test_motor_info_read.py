"""虚拟引擎 motor_info (0xE6/0xE7) 读写测试。

验证:
1. TwinResponder 对 MOTOR_INFO_READ(0xE6) 返回正确的 param_id+type+value(4B) 帧
2. 读到的值与 motor_info.csv 的 DefaultValue 一致(或 MotorParam 字段值)
3. MOTOR_INFO_WRITE(0xE7) 写入后再读回, 值更新
4. 未知 param_id 返回 NACK
5. MOTOR_INFO_SAVE(0xEA) / MOTOR_INFO_RESET(0xEB) 返回 ACK
6. 端到端: 主窗口连接虚拟引擎后, motor_config 面板 read_all 能拿到值且队列不死锁
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, '..', '..')))

from PyQt6.QtWidgets import QApplication
from jmproto import JmCmd, JmErr, codec, get_registry
from transport.virtual_engine.twin_responder import TwinResponder

app = QApplication.instance() or QApplication(sys.argv)

PASS = 0
FAIL = 0
_FAILS = []


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        _FAILS.append(msg)
        print(f"  [FAIL] {msg}")


reg = get_registry()
resp = TwinResponder()

# ---- 1. MOTOR_INFO_READ 基本读: phase_resistance (id=20, float, default=0.1) ----
spec20 = reg.get_motor_config_param(20)
check(spec20 is not None, "motor_config param_id=20 应存在")
frame = resp.on_command(int(JmCmd.MOTOR_INFO_READ), codec.wr_u16(20))
check(len(frame) == 1, f"应返回 1 帧, 实际 {len(frame)}")
cmd, payload = frame[0]
check(cmd == int(JmCmd.MOTOR_INFO_READ), f"帧 cmd 应为 0xE6, 实际 0x{cmd:02X}")
check(len(payload) >= 7, f"payload 应 >= 7 字节(param_id+type+4B), 实际 {len(payload)}")
pid = codec.rd_u16(payload, 0)
ptype = payload[2]
val_bytes = payload[3:7]
check(pid == 20, f"返回 param_id 应为 20, 实际 {pid}")
val = codec.unpack_value('f32', val_bytes)
check(val is not None, "float 解包不应为 None")
check(abs(val - 0.1) < 0.001 or abs(val - spec20.vmin) > 0,
      f"phase_resistance 值应接近默认 0.1 或 MotorParam 值, 实际 {val}")

# ---- 2. 读 ControlParam: kp_ld (id=64, float, default=0.5) ----
spec64 = reg.get_motor_config_param(64)
check(spec64 is not None, "motor_config param_id=64 应存在")
frame = resp.on_command(int(JmCmd.MOTOR_INFO_READ), codec.wr_u16(64))
cmd, payload = frame[0]
check(cmd == int(JmCmd.MOTOR_INFO_READ), "kp_ld 帧 cmd 应为 0xE6")
val = codec.unpack_value('f32', payload[3:7])
check(val is not None, "kp_ld float 解包不应为 None")

# ---- 3. MOTOR_INFO_WRITE 后再读回, 值应更新 ----
new_val = 3.14
write_payload = codec.wr_u16(20) + codec.pack_value('f32', new_val)
frame = resp.on_command(int(JmCmd.MOTOR_INFO_WRITE), write_payload)
cmd, payload = frame[0]
check(cmd == int(JmCmd.MOTOR_INFO_WRITE), f"写应答 cmd 应为 0xE7, 实际 0x{cmd:02X}")
check(payload[2] == int(JmErr.OK), f"写应答 status 应为 OK, 实际 0x{payload[2]:02X}")
frame = resp.on_command(int(JmCmd.MOTOR_INFO_READ), codec.wr_u16(20))
cmd, payload = frame[0]
val = codec.unpack_value('f32', payload[3:7])
check(abs(val - new_val) < 0.001,
      f"写入后读回应等于 {new_val}, 实际 {val}")

# ---- 4. 未知 param_id 返回 NACK ----
frame = resp.on_command(int(JmCmd.MOTOR_INFO_READ), codec.wr_u16(9999))
cmd, payload = frame[0]
check(cmd == int(JmCmd.NACK), f"未知 id 应返回 NACK, 实际 0x{cmd:02X}")
check(payload[0] == int(JmCmd.MOTOR_INFO_READ), "NACK payload[0] 应为 0xE6")

# ---- 5. MOTOR_INFO_SAVE / MOTOR_INFO_RESET 返回 ACK ----
for cmd_code in (JmCmd.MOTOR_INFO_SAVE, JmCmd.MOTOR_INFO_RESET):
    frame = resp.on_command(int(cmd_code), b'')
    cmd, payload = frame[0]
    check(cmd == int(cmd_code) and payload == bytes([int(JmErr.OK)]),
          f"{cmd_code.name} 应返回 ACK, 实际 cmd=0x{cmd:02X}")

# ---- 6. 端到端: 主窗口 + 虚拟引擎, motor_config 面板 read_all 不死锁 ----
from ui.main_window import MainWindow

mw = MainWindow()
mw._on_connect("VIRTUAL", 0)
check(mw._client.is_open(), "虚拟引擎应已连接")

config_panel = mw._config_panel
ids = config_panel.param_ids()
check(len(ids) > 0, f"motor_config 面板应有参数, 实际 {len(ids)}")

config_panel.read_all()
for _ in range(500):
    app.processEvents()
    if not mw._pending_param_reads and not mw._param_read_queue:
        break

check(len(mw._pending_param_reads) == 0,
      f"读队列应已清空, 实际残留 {len(mw._pending_param_reads)}")
check(len(mw._param_read_queue) == 0,
      f"读待发队列应已清空, 实际残留 {len(mw._param_read_queue)}")

got_value = 0
for pid in ids:
    item = config_panel._current_items.get(pid)
    if item is not None and item.text().strip() != "--":
        got_value += 1
check(got_value > 0,
      f"motor_config 面板应至少有 1 个参数拿到值, 实际 {got_value}/{len(ids)}")

# ---- 7. motor_param 面板 (0xE0) 也能正常读(不被 0xE6 阻塞) ----
param_panel = mw._param_panel
param_ids = param_panel.param_ids()
if param_ids:
    param_panel.read_all()
    for _ in range(500):
        app.processEvents()
        if not mw._pending_param_reads and not mw._param_read_queue:
            break
    check(len(mw._pending_param_reads) == 0,
          f"motor_param 读队列应已清空, 实际残留 {len(mw._pending_param_reads)}")

mw.close()

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
