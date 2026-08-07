"""Main-window PID TRACE lifecycle and session isolation tests."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..")))

from PyQt6.QtWidgets import QApplication

from core.pid_debug_profile import get_pid_debug_profile
from ui.main_window import MainWindow


class SpyClient:
    def __init__(self):
        self.calls = []

    def is_open(self):
        return True

    def trace_config(self, enable, session_id=0, channel_mask=0,
                     rate_hz=1000, packet_samples=4, flags=0):
        self.calls.append(("trace_config", bool(enable), int(session_id),
                           int(channel_mask), int(rate_hz), int(packet_samples)))
        return True

    def trace_stop(self, session_id=0):
        self.calls.append(("trace_stop", int(session_id)))
        return True

    def set_telemetry(self, enable, mask, period_ms):
        self.calls.append(("telemetry", enable, mask, period_ms))
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

    def stop(self):
        pass


app = QApplication.instance() or QApplication(sys.argv)
window = MainWindow()
window._client.stop()
window._stats_timer.stop()
window._ui_timer.stop()
window._reconnect_timer.stop()
spy = SpyClient()
window._client = spy
window._active_main_tab_widget = window._pid_panel
panel = window._pid_panel
panel.set_link_active(True)
panel.update_state(3, 0, 0, 0)

assert not panel.waveform_enabled()
spy.calls.clear()
panel._open_pid_waveform_window()
assert not spy.calls
plot_window = panel._waveform_window
plot_window._receive_btn.setChecked(True)
profile = panel.waveform_profile()
assert spy.calls[-1][0] == "trace_config"
assert spy.calls[-1][1] is True
assert spy.calls[-1][3] == profile.capture_mask
assert panel.waveform_enabled()

# 数据源切换必须先使旧 session 失效，再用新 mask 开启新 session。
spy.calls.clear()
plot_window._source_combo.setCurrentIndex(
    plot_window._source_combo.findData("velocity"))
profile = get_pid_debug_profile("velocity")
assert [call[0] for call in spy.calls[-2:]] == ["trace_stop", "trace_config"]
assert spy.calls[-1][3] == profile.capture_mask

# 同一缺口只按 sequence 统计一次，样本索引不再重复累加。
window._on_trace_data({
    "session_id": window._trace_session_id,
    "sequence": 0,
    "first_sample_index": 0,
    "actual_rate_hz": 1000,
    "channel_mask": profile.capture_mask,
    "sample_count": 2,
    "channels": 2,
    "flags": 0,
    "overflow_count": 0,
    "samples": [[1.0, 0.8], [1.1, 0.9]],
})
window._on_trace_data({
    "session_id": window._trace_session_id,
    "sequence": 2,
    "first_sample_index": 4,
    "actual_rate_hz": 1000,
    "channel_mask": profile.capture_mask,
    "sample_count": 2,
    "channels": 2,
    "flags": 2,
    "overflow_count": 1,
    "samples": [[1.2, 1.0], [1.3, 1.1]],
})
assert window._trace_lost_packets == 1
assert window._trace_overflow_count == 1

# 乱序旧包不得推进游标或污染统计；旧 session 直接丢弃。
window._on_trace_data({
    "session_id": window._trace_session_id,
    "sequence": 1,
    "first_sample_index": 2,
    "actual_rate_hz": 1000,
    "channel_mask": profile.capture_mask,
    "sample_count": 1,
    "channels": 2,
    "flags": 0,
    "overflow_count": 0,
    "samples": [[9.0, 9.0]],
})
assert window._trace_lost_packets == 1
assert window._trace_last_sample_index == 6

old_session = window._trace_session_id
window._on_trace_config_received({"session_id": (old_session - 1) & 0xFFFF,
                                  "actual_rate_hz": 10})
assert window._trace_device_rate_hz == 1000

spy.calls.clear()
panel.close_waveform_window()
assert spy.calls and spy.calls[-1][0] == "trace_stop"
assert not panel.waveform_enabled()

window._pid_debug_heartbeat_timer.stop()
window._stats_timer.stop()
window._ui_timer.stop()
window._reconnect_timer.stop()
print("PID TRACE session: PASS")
