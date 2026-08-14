"""PID debug value persistence workflow tests."""

import os
import struct
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..")))

from PyQt6.QtWidgets import QApplication

from jmproto import JmCmd
from ui.main_window import MainWindow


class SpyClient:
    def __init__(self):
        self.calls = []
        self.transport = SimpleNamespace(name="test")

    def is_open(self):
        return True

    def motor_info_write(self, param_id, value):
        self.calls.append(("E7", int(param_id), bytes(value)))
        return True

    def motor_info_read(self, param_id):
        self.calls.append(("E6", int(param_id)))
        return True

    def pid_source_set(self, ring, source):
        self.calls.append(("A1", int(ring), int(source)))
        return True

    def pid_source_get(self):
        self.calls.append(("A2",))
        return True

    def motor_info_save(self, flags=0):
        self.calls.append(("EA", int(flags)))
        return True

    def set_telemetry(self, enable, mask, period_ms):
        return True

    def stop(self):
        pass


app = QApplication.instance() or QApplication(sys.argv)
window = MainWindow()
window._client.stop()
client = SpyClient()
window._client = client

# Current-loop D values persist as kp_ld, ki_ld and integral_limit.
window._on_pid_debug_commit(0, {1: 7.5, 2: 1250.0, 5: 8.0})
assert window._pid_commit_phase == "writing"
expected = dict(window._pid_commit_expected)
assert len(expected) == 3

# Every E7 write must be acknowledged before verification starts.
for _ in range(len(expected)):
    window._on_ack(int(JmCmd.MOTOR_INFO_WRITE))
assert window._pid_commit_phase == "verifying"
assert len([call for call in client.calls if call[0] == "E6"]) == 1

# Each E6 response drives the next read; only the final response sends A1.
for param_id, value in expected.items():
    window._on_param_result(param_id, 0, struct.pack("<f", value))
assert window._pid_commit_phase == "switching"
assert [call for call in client.calls if call[0] == "A1"] == [("A1", 0, 1)]
assert not [call for call in client.calls if call[0] == "EA"]

window._on_ack(int(JmCmd.PID_SOURCE_SET))
assert window._pid_commit_phase == "saving"
assert [call for call in client.calls if call[0] == "EA"] == [("EA", 1)]  # 显式Flash固化带FLASH标志

window._on_ack(int(JmCmd.MOTOR_INFO_SAVE))
assert window._pid_commit_phase == "idle"

# A failed E6 verification must not send A1 or EA, including after late ACKs.
client.calls.clear()
window._pid_commit_phase = "verifying"
window._pid_commit_expected = {next(iter(expected)): 1.0}
window._pid_commit_source_ring = 0
window._pending_param_reads.append(window._pid_params_panel)
window._on_nack(int(JmCmd.MOTOR_INFO_READ), 1)
assert window._pid_commit_phase == "idle"
window._on_ack(int(JmCmd.PID_SOURCE_SET))
assert not [call for call in client.calls if call[0] in ("A1", "EA")]

window._stats_timer.stop()
window._ui_timer.stop()
window._reconnect_timer.stop()
print("PID commit workflow: PASS")
