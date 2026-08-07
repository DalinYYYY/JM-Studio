"""Main-window PID capture lifecycle tests."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..")))

from PyQt6.QtWidgets import QApplication

from core.pid_debug_profile import get_pid_debug_profile
from ui.main_window import MainWindow
from ui.panels.pid_panel import PidRing, PidSource


class SpyClient:
    def __init__(self):
        self.calls = []

    def is_open(self):
        return True

    def set_telemetry(self, enable, mask, period_ms):
        self.calls.append(("telemetry", enable, mask, period_ms))
        return True

    def pid_capture_stop(self):
        self.calls.append(("stop",))
        return True

    def pid_capture_start(self, mask, rate_hz, samples):
        self.calls.append(("start", mask, rate_hz, samples))
        return True

    def stop(self):
        pass


app = QApplication.instance() or QApplication(sys.argv)
window = MainWindow()
window._client.stop()
spy = SpyClient()
window._client = spy
window._active_main_tab_widget = window._pid_panel
window._pid_telemetry_active = True
panel = window._pid_panel

# 波形采集与 DEBUG 无关: 全环 DEFAULT source 下开窗也要起采集。
assert not panel.is_debug_active()
spy.calls.clear()
panel._open_pid_waveform_window()
assert spy.calls[-1] == (
    "start", panel.waveform_profile().capture_mask,
    panel.waveform_rate_hz(), panel.waveform_profile().capture_samples)
assert panel.waveform_enabled()

# 顶部数据源切换 -> 停旧会话, 用新掩码/新限幅重开。
plot_window = panel._waveform_window
for source in ("current_q", "velocity", "position", "current_d"):
    spy.calls.clear()
    plot_window._source_combo.setCurrentIndex(
        plot_window._source_combo.findData(source))
    profile = get_pid_debug_profile(source)
    assert panel.waveform_source() == source
    assert spy.calls[-2:] == [
        ("stop",),
        ("start", profile.capture_mask, panel.waveform_rate_hz(),
         profile.capture_samples),
    ]

# 整定模式只切遥测组, 不再动采集会话。
for mode, ring in (("current_d", PidRing.CURRENT),
                   ("current_q", PidRing.CURRENT),
                   ("velocity", PidRing.VELOCITY),
                   ("position", PidRing.POSITION)):
    panel._tuning_mode = mode
    panel._ring_source[ring] = PidSource.DEBUG
    spy.calls.clear()
    window._on_pid_tuning_mode_changed(mode)
    profile = get_pid_debug_profile(mode)
    telemetry = [call for call in spy.calls if call[0] == "telemetry"]
    assert telemetry and telemetry[0][2] == profile.telemetry_mask
    assert not any(call[0] in ("stop", "start") for call in spy.calls)

# A delayed chunk from the previous source must not advance the new read cursor.
plot_window._source_combo.setCurrentIndex(
    plot_window._source_combo.findData("velocity"))
window._pid_capture_offset = 19
window._on_pid_capture_chunk({
    "mask": get_pid_debug_profile("current_q").capture_mask,
    "offset": 0,
    "count": 7,
    "total": 256,
    "state": 1,
    "samples": [],
})
assert window._pid_capture_offset == 19

# 关窗停止采集。
spy.calls.clear()
panel.close_waveform_window()
assert spy.calls[-1] == ("stop",)
assert not panel.waveform_enabled()

window._pid_capture_timer.stop()
window._pid_debug_heartbeat_timer.stop()
window._stats_timer.stop()
window._ui_timer.stop()
window._reconnect_timer.stop()
print("PID debug session: PASS")
