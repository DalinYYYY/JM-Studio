"""PID DEBUG 兼容扩展：A1/A2/A5/A6/B7/B6/C9。"""

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

# B7 采样 + C9 分块，并验证客户端解析信号。
assert r.on_command(int(JmCmd.HIGH_SPEED_DAQ), struct.pack('<IIH', 0xFF, 1000, 16))[0][1] == b'\x00'
chunk_payload = r.on_command(int(JmCmd.READ_DEBUG), struct.pack('<HB', 0, 7))[0][1]
assert len(chunk_payload) == 15 + 7 * 8 * 4
client = JmClient(DummyTransport())
chunks = []
client.pid_capture_chunk_received.connect(chunks.append)
client._capture_read_pending = True
client._on_frame(int(JmCmd.READ_DEBUG), chunk_payload)
assert chunks and chunks[0]['mask'] == 0xFF
assert chunks[0]['count'] == 7 and chunks[0]['channels'] == 8

print('PID debug protocol: PASS')
