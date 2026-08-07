"""PID DEBUG source draft and A2 lease lifecycle regression tests."""

import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..")))

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from jmproto import JmCmd, TopFsm
from ui.main_window import MainWindow
from ui.panels.pid_panel import PidRing, PidSource, _SOURCE_OPTIONS


class SpyClient:
    def __init__(self):
        self.calls = []
        self.transport = SimpleNamespace(name="serial")

    def is_open(self):
        return True

    def pid_source_get(self):
        self.calls.append(("source_get",))
        return True

    def pid_source_set(self, ring, source):
        self.calls.append(("source_set", int(ring), int(source)))
        return True

    def pid_param_get(self, ring, param_type):
        self.calls.append(("param_get", int(ring), int(param_type)))
        return True


    def query_state(self):
        self.calls.append(("state_get",))
        return True

    def set_telemetry(self, enable, mask, period_ms):
        self.calls.append(("telemetry", enable, mask, period_ms))
        return True

    def stop(self):
        pass


def option_id(ring, mode):
    return next(item[0] for item in _SOURCE_OPTIONS[ring]
                if item[3] == mode)


def click_source(panel, ring, option):
    button = panel._source_btn_groups[int(ring)].button(option)
    assert button is not None
    button.click()
    assert panel._source_btn_groups[int(ring)].checkedId() == option


app = QApplication.instance() or QApplication(sys.argv)
window = MainWindow()
window._client.stop()
window._stats_timer.stop()
window._ui_timer.stop()
window._reconnect_timer.stop()
spy = SpyClient()
window._client = spy
window._active_main_tab_widget = window._pid_panel
window._pid_panel.set_link_active(True)
window._pid_panel.update_state(int(TopFsm.IDLE), 0, 0, 0)
window._pid_panel.on_source_received(0, 0, 0)
assert not window._pid_panel.waveform_enabled()  # 波形窗口未开 -> 不采集, spy 只看 A1/A2

# A DEBUG radio selection is only a draft before "apply". Repeated A2 replies,
# including after one heartbeat interval, may update badges but not the draft.
debug_id = option_id(PidRing.CURRENT, "current_q")
click_source(window._pid_panel, PidRing.CURRENT, debug_id)
assert window._pid_panel.has_unapplied_source_selection()
assert not window._pid_debug_heartbeat_timer.isActive()
window._on_pid_source_received(0, 0, 0)
QTest.qWait(window._pid_debug_heartbeat_timer.interval() + 50)
window._on_pid_source_received(0, 0, 0)
assert window._pid_panel._source_btn_groups[0].checkedId() == debug_id
assert window._pid_panel._ring_source[PidRing.CURRENT] == PidSource.DEFAULT
assert not window._pid_debug_heartbeat_timer.isActive()

# Applying sends the original A1 source=DEBUG command. Its ACK establishes the
# first confirmed DEBUG ring and starts the periodic A2 lease.
window._pid_panel._on_source_apply_clicked()
assert spy.calls[-1] == ("source_set", int(PidRing.CURRENT), int(PidSource.DEBUG))
assert not window._pid_debug_heartbeat_timer.isActive()
window._on_ack(int(JmCmd.PID_SOURCE_SET))
assert window._pid_panel._ring_source[PidRing.CURRENT] == PidSource.DEBUG
assert window._pid_debug_heartbeat_timer.isActive()

# The A2 refresh requested after ACK must still preserve the selected IQ mode
# while the source transaction is being finalized.
window._on_pid_source_received(3, 0, 0)
assert window._pid_panel._source_btn_groups[0].checkedId() == debug_id

# Leaving the PID page explicitly relinquishes the lease. The device-side
# timeout then performs the safe rollback if DEBUG was still active.
other_index = next(i for i in range(window._tabs.count())
                   if window._tabs.widget(i) is not window._pid_panel)
window._on_main_tab_changed(other_index)
assert not window._pid_debug_heartbeat_timer.isActive()

# Re-enter, re-sync the confirmed DEBUG source, then switch the final DEBUG ring
# back to DEFAULT. The A1 ACK must stop the lease immediately.
window._active_main_tab_widget = window._pid_panel
window._sync_pid_debug_heartbeat()
assert window._pid_debug_heartbeat_timer.isActive()
click_source(window._pid_panel, PidRing.CURRENT, int(PidSource.DEFAULT))
window._pid_panel._on_source_apply_clicked()
assert spy.calls[-1] == ("source_set", int(PidRing.CURRENT), int(PidSource.DEFAULT))
window._on_ack(int(JmCmd.PID_SOURCE_SET))
assert window._pid_panel._ring_source[PidRing.CURRENT] == PidSource.DEFAULT
assert not window._pid_debug_heartbeat_timer.isActive()

window._pid_debug_heartbeat_timer.stop()
window._stats_timer.stop()
window._ui_timer.stop()
window._reconnect_timer.stop()
window.close()
print("PID DEBUG heartbeat: PASS")
