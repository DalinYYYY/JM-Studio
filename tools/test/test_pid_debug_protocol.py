"""PID DEBUG 与 TRACE 协议回归测试。"""

import os
import struct
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..')))

from PyQt6.QtCore import QCoreApplication

from core.motor_client import JmClient
from jmproto import JmCmd
from transport.base import Transport
from transport.virtual_engine.twin_responder import TwinResponder


class DummyTransport(Transport):
    def start(self): pass
    def stop(self): pass
    def open(self, **cfg): return True
    def close(self): pass
    def is_open(self): return True
    def send(self, cmd, payload=b''): return True


app = QCoreApplication.instance() or QCoreApplication(sys.argv)
r = TwinResponder()

# DEBUG 来源与旧版单字段写保持兼容。
assert r.on_command(int(JmCmd.PID_SOURCE_SET), bytes([0, 3]))[0][1] == b'\x00'
assert r.on_command(int(JmCmd.PID_SOURCE_GET), b'')[0][1] == bytes([3, 0, 0])
assert r.on_command(int(JmCmd.PID_PARAM_SET), struct.pack('<BBf', 0, 1, 7.5))[0][1] == b'\x00'
value = r.on_command(int(JmCmd.PID_PARAM_GET), bytes([0, 1]))[0][1]
assert abs(struct.unpack_from('<f', value, 2)[0] - 7.5) < 1e-6

# param_type=0 原子批量格式。
batch = bytes([0, 0, 2, 1]) + struct.pack('<f', 8.0) + bytes([2]) + struct.pack('<f', 1200.0)
assert r.on_command(int(JmCmd.PID_PARAM_SET), batch)[0][1] == b'\x00'
assert abs(r._pid_profiles[0][1] - 8.0) < 1e-6
assert abs(r._pid_profiles[0][2] - 1200.0) < 1e-6

# TRACE 配置应答 + 主动批量数据，并验证客户端严格解析。
config = struct.pack('<BHII BB', 1, 7, 0x03, 1000, 4, 0)
config_reply = r.on_command(int(JmCmd.TRACE_CONFIG), config)[0][1]
assert len(config_reply) == 9
client = JmClient(DummyTransport())
status = []
packets = []
client.trace_config_received.connect(status.append)
client.trace_data_received.connect(packets.append)
client._on_frame(int(JmCmd.TRACE_CONFIG), config_reply)
assert status[0]['session_id'] == 7
packet = r.on_tick()[-1][1]
assert len(packet) == 25 + 4 * 2 * 4
client._on_frame(int(JmCmd.TRACE_DATA), packet)
assert packets and packets[0]['channel_mask'] == 0x03
assert packets[0]['sample_count'] == 4

print('PID debug/TRACE protocol: PASS')
