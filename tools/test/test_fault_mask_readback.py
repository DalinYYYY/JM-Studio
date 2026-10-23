"""Protection masks must travel from E6 replies to the dashboard, independently of E0."""
import os
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication
import jmproto as jp
from core.motor_client import JmClient
from jmproto import fault_diag as fd
from transport.base import Transport
from ui.main_window import MainWindow
from ui.panels.fault_info_panel import FaultInfoPanel
from ui.theme import theme


class CaptureTransport(Transport):
    def __init__(self):
        super().__init__()
        self.frames = []

    def is_open(self):
        return True

    def send(self, cmd, payload=b""):
        self.frames.append((int(cmd), bytes(payload)))
        return True


app = QApplication.instance() or QApplication(sys.argv)
font_file = Path("C:/Windows/Fonts/msyh.ttc")
if font_file.exists():
    QFontDatabase.addApplicationFont(str(font_file))
app.setFont(QFont("Microsoft YaHei UI", 9))
theme.set("dark")
app.setStyleSheet(theme.qss())
panel = FaultInfoPanel()
transport = CaptureTransport()
client = JmClient(transport)
logs = []
router = SimpleNamespace(
    _client=client, _ensure_open=lambda: True, _protect_mask_pending=False,
    _fault_level_pending=set(), _pending_param_reads=deque(),
    _pending_param_writes=deque(), _cmd_watchdog=QTimer(),
    _fault_info_panel=panel, _log_panel=SimpleNamespace(log=logs.append))
router._cmd_watchdog.setSingleShot(True)
router._cmd_watchdog.setInterval(5000)
client.motor_info_read_result.connect(
    lambda pid, ptype, value: MainWindow._on_param_result(
        router, pid, ptype, value, "motor_config"))
client.param_read_result.connect(
    lambda pid, ptype, value: MainWindow._on_param_result(
        router, pid, ptype, value, "motor_param"))
panel.protect_mask_read_requested.connect(lambda: MainWindow._on_protect_mask_read(router))
panel.fault_enable_masks_read_requested.connect(lambda: MainWindow._on_fault_enable_masks_read(router))
panel.set_link_active(True)

implemented = [r for r in jp.load_fault_csv() if r['stage'] != '0']
unimplemented = [r for r in jp.load_fault_csv() if r['stage'] == '0']


def count(key):
    return int(panel._legend_counts[key].text().rsplit(" ", 1)[1])


def reply(cmd, pid, value):
    transport.frame_received.emit(int(cmd), pid.to_bytes(2, 'little') +
                                  bytes((int(jp.JmParamType.U32),)) + value.to_bytes(4, 'little'))


assert count('unknown') == len(implemented) and count('green') == 0
assert count('gray') == len(unimplemented)
panel.auto_read_if_connected()
expected = {128, 154, 155, 156, 157, 158, 159}
assert {int.from_bytes(payload, 'little') for cmd, payload in transport.frames} == expected
assert all(cmd == int(jp.JmCmd.MOTOR_INFO_READ) for cmd, _ in transport.frames)
assert router._protect_mask_pending and router._fault_level_pending == expected - {128}

# E0 IDs can overlap, but they must not satisfy any protection E6 request.
for pid in expected:
    reply(jp.JmCmd.PARAM_READ, pid, 0)
assert panel._protect_mask is None and all(v is None for v in panel._fault_masks.values())
assert router._fault_level_pending == expected - {128}

# Reproduce the reported screen: an active overspeed is red even before masks arrive.
panel.update_state(int(jp.TopFsm.FAULT), 7, 0x15, 0)
panel.on_summary_received(fd.FaultSummary(top_code=0x4106, active_cnt=1,
    hist_cnt=1, compat_mask=0x400, level_active=1))
panel.on_detail_received(fd.TYPE_ACTIVE, 0, fd.FaultRecord(code=0x4106, status=1, count=1))
assert count('unknown') == len(implemented) - 1 and count('red') == 1

# Receive the total gate first, then mask halves out of order; watchdog stays armed.
values = {pid: 0xFFFFFFFF for pid in expected - {128}}
overcurrent = jp.fault_code_lookup(0x3102)
assert overcurrent['stage'] != '0'
values[154] &= ~(1 << overcurrent['level_bit'])
reply(jp.JmCmd.MOTOR_INFO_READ, 128, 1)
assert panel._protect_mask == 1 and "启用" in panel._protect_badge.text()
assert panel._btn_protect_off.isEnabled() and router._cmd_watchdog.isActive()
for pid in (159, 154, 157, 158, 155, 156):
    reply(jp.JmCmd.MOTOR_INFO_READ, pid, values[pid])
    if router._fault_level_pending:
        assert router._cmd_watchdog.isActive()
assert not router._protect_mask_pending and not router._fault_level_pending
assert not router._cmd_watchdog.isActive()
assert panel._fault_masks == values and count('unknown') == 0
assert count('blue') == 1 and count('red') == 1
assert count('green') == len(implemented) - 2
assert panel._HEAT_BLUE in panel._heat_cells[(154, overcurrent['level_bit'])].styleSheet()
assert panel._HEAT_RED in panel._heat_cells[(154, 20)].styleSheet()

# A repeated/unrequested mask reply must not steal a configuration FIFO response.
config = SimpleNamespace(source=lambda: 'motor_config')
router._pending_param_reads.append(config)
router._param_read_active = (config, 28)
router._fault_level_pending = {155}
reply(jp.JmCmd.MOTOR_INFO_READ, 154, 0)
assert panel._fault_masks[154] == values[154]
assert list(router._pending_param_reads) == [config] and router._fault_level_pending == {155}
router._pending_param_reads.clear()
router._param_read_active = None
router._fault_level_pending.clear()

# Manual read covers the opposite ordering: total gate arrives after all six halves.
transport.frames.clear()
panel._on_read_all_status()
for pid in range(154, 160):
    reply(jp.JmCmd.MOTOR_INFO_READ, pid, values[pid])
assert router._protect_mask_pending and router._cmd_watchdog.isActive()
reply(jp.JmCmd.MOTOR_INFO_READ, 128, 1)
assert not router._cmd_watchdog.isActive()
assert len(transport.frames) == 7
assert all(cmd == int(jp.JmCmd.MOTOR_INFO_READ) for cmd, _ in transport.frames)
panel.apply_theme()
assert count('unknown') == 0 and count('green') == len(implemented) - 2

output = os.environ.get("FAULT_MASK_UI_SCREENSHOT")
if output:
    panel.resize(1477, 760)
    panel.show()
    app.processEvents()
    assert panel.grab().save(output)

panel.set_link_active(False)
assert count('unknown') == len(implemented) and count('green') == 0 and count('red') == 0
assert panel._protect_mask is None and all(v is None for v in panel._fault_masks.values())
assert not panel._btn_protect_off.isEnabled()
print("PASS protection readback: E6/E0 isolation, automatic/manual reads, out-of-order halves, "
      "watchdog, config FIFO, colors/counts, disconnect; no writes sent")
