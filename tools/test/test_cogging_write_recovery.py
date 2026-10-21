"""Slow/missing replies after cogging Flash: bounded polling and usable writes."""
import os
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
from jmproto import CalibState, JmCmd, TopFsm
from ui.main_window import MainWindow


class Client:
    def __init__(self):
        self.calls = []
        self.transport = SimpleNamespace(name='test')
    def is_open(self): return True
    def motor_info_read(self, pid):
        self.calls.append(('E6', pid)); return True
    def param_read(self, pid):
        self.calls.append(('E0', pid)); return True
    def motor_info_write(self, pid, value):
        self.calls.append(('E7', pid, value)); return True
    def param_write(self, pid, value):
        self.calls.append(('E1', pid, value)); return True


app = QApplication.instance() or QApplication([])
window = MainWindow()
window._client.stop()
for value in vars(window).values():
    if isinstance(value, QTimer): value.stop()
for timer in window.findChildren(QTimer): timer.stop()
window._client = client = Client()
panel = window._calib_panel
panel.set_link_active(True)
panel._set_active_task(int(JmCmd.CALIB_LEVEL5), 1, 1, 'L5', '位置爬行', '')
panel.update_state(int(TopFsm.CALIB), 0, 0x94, 0)
done = {'state': int(CalibState.DONE), 'level': 5, 'submode': 1}
panel.on_calib_status(done)
assert client.calls == [('E0', 89)]
for _ in range(100): panel._on_poll_tick()
assert len(window._pending_param_reads) == 1
assert len(window._param_read_queue) == 2
print('PASS 100 polling ticks with no response: one in-flight status, two queued config reads')

# An explicit write waits for the current read, then takes priority over polls.
window._on_param_write(window._calib_results_panel, 185, '1')
assert not window._pending_param_writes
window._on_param_result(89, 0, (1 << 5).to_bytes(4, 'little'), 'motor_param')
assert client.calls[-1][:2] == ('E7', 185)
assert not window._pending_param_reads and len(window._pending_param_writes) == 1
for _ in range(100): panel._on_poll_tick()
assert len(window._param_read_queue) == 3
window._on_ack(int(JmCmd.MOTOR_INFO_WRITE))
assert client.calls[-1] == ('E6', 184)
print('PASS writes take priority, read/write share one in-flight request, reads resume after ACK')

def clear_reads():
    window._pending_param_reads.clear()
    window._param_read_queue.clear()
    window._param_read_active = None
    window._cmd_watchdog.stop()

clear_reads()
window._on_param_read(window._cogging_diag_panel, 89)
window._on_param_result(89, 0, bytes(4), 'motor_config')
window._on_param_result(88, 0, bytes(4), 'motor_param')
assert len(window._pending_param_reads) == 1 and panel._cogging_wait_flash
window._on_param_result(89, 0, (3 | (1 << 8)).to_bytes(4, 'little'), 'motor_param')
assert not panel._cogging_wait_flash and '已写入并校验' in panel._last_op
assert not panel._poll_timer.isActive()
print('PASS late IDs/wrong namespace cannot consume status; valid status confirms Flash')

clear_reads()
panel.on_calib_status(done)
window._on_cmd_timeout()
assert panel._cogging_wait_flash and not panel._poll_timer.isActive()
assert '读取超时' in panel._last_op and '失败' not in panel._last_op
assert not window._param_read_queue and not window._pending_param_reads
window._on_param_write(window._calib_results_panel, 185, '1')
assert client.calls[-1][:2] == ('E7', 185)
window._on_ack(int(JmCmd.MOTOR_INFO_WRITE))
panel._on_query_clicked()
assert client.calls[-1] == ('E0', 89) and panel._poll_timer.isActive()
print('PASS lost status pauses polling without claiming write failure; write and manual retry remain usable')
panel.set_link_active(False)
window._cmd_watchdog.stop()
