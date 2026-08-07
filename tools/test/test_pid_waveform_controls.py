"""PID waveform window: source/rate control and DEBUG-independence regressions."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..")))

from PyQt6.QtWidgets import QApplication

from core.pid_debug_profile import get_pid_debug_profile
from ui.panels.pid_panel import PidPanel, PidSource


app = QApplication.instance() or QApplication(sys.argv)
panel = PidPanel()
panel.set_link_active(True)
panel.update_state(3, 0, 0, 0)  # IDLE

changes = []
panel.pid_waveform_config_changed.connect(
    lambda enabled, rate: changes.append((enabled, rate)))

# 面板上只有一个「打开波形窗口」按钮: 未打开=暗色(unchecked), 打开=高亮(checked)
assert panel._waveform_open_btn.isCheckable()
assert not panel._waveform_open_btn.isChecked()
# 窗口未打开 = 不采集
assert not panel.waveform_enabled()

# 波形与 DEBUG 整定解耦: 全部环都是 DEFAULT source 也要能采集并显示。
assert not panel.is_debug_active()
panel._open_pid_waveform_window()
assert panel._waveform_window is not None
assert panel._waveform_open_btn.isChecked()
assert panel.waveform_enabled()          # 开窗即请求采集
assert changes[-1] == (True, panel.waveform_rate_hz())

# 数据源在窗口顶部切换, 与 _tuning_mode 互不影响。
assert panel.waveform_source() == "current_d"
window = panel._waveform_window
window._source_combo.setCurrentIndex(window._source_combo.findData("velocity"))
assert panel.waveform_source() == "velocity"
assert panel.current_tuning_mode() == "current_d"   # 整定模式不被波形改动
assert panel.waveform_profile().capture_mask == \
    get_pid_debug_profile("velocity").capture_mask
assert changes[-1] == (True, panel.waveform_rate_hz())

# 切数据源要按该源的固件上限限幅 (速度环 1kHz)。
window._rate_spin.setValue(5000)
assert panel.waveform_rate_hz() == get_pid_debug_profile("velocity").capture_rate_hz
window._source_combo.setCurrentIndex(window._source_combo.findData("current_d"))
assert panel.waveform_rate_hz() == get_pid_debug_profile("current_d").capture_rate_hz

# 采样上传频率改在子窗口里设置, 变更需回传面板并转发给主窗口。
window._rate_spin.setValue(2400)
assert panel.waveform_rate_hz() == 2400
assert changes[-1] == (True, 2400)

# 反过来: 切整定模式不动波形数据源, 也不重启采集。
before = len(changes)
panel._ring_source[0] = PidSource.DEBUG
panel.set_tuning_mode("current_q")
assert panel.waveform_source() == "current_d"
assert len(changes) == before

# 非 DEBUG 状态下的 C9 分块也要正常入图。
panel.feed_capture_chunk({
    "mask": get_pid_debug_profile("current_d").capture_mask,
    "rate_hz": 1000,
    "samples": [[1.0, 0.8]],
})
assert len(panel._plot_points) == 1
assert len(window._points) == 1
if window._plot is not None:
    x_data, y_data = window._target_curve.getData()
    assert len(x_data) == 1 and len(y_data) == 1

panel.close_waveform_window()
assert panel._waveform_window is None
assert not panel._waveform_open_btn.isChecked()  # 关窗后按钮回到暗色
assert not panel.waveform_enabled()              # 关窗即停止采集
assert changes[-1][0] is False
# 关窗不丢配置: 下次开窗仍按上次的数据源与频率启动
assert panel.waveform_source() == "current_d"
assert panel.waveform_rate_hz() == 2400

print("PID waveform controls: PASS")
