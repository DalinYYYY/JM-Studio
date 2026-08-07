"""PID waveform window: source/rate control and receive-state regressions."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..")))

from PyQt6.QtWidgets import QApplication

from core.pid_debug_profile import get_pid_debug_profile
from ui.main_window import MainWindow
from ui.panels.pid_panel import PidSource


app = QApplication.instance() or QApplication(sys.argv)
main_window = MainWindow()
main_window._client.stop()
panel = main_window._pid_panel
panel.set_link_active(True)
panel.update_state(3, 0, 0, 0)  # IDLE

changes = []
panel.pid_waveform_config_changed.connect(
    lambda enabled, rate: changes.append((enabled, rate)))

assert panel._waveform_open_btn.isCheckable()
assert not panel._waveform_open_btn.isChecked()
assert not panel.waveform_enabled()

# 打开窗口只显示界面，默认不占用设备采集和通信带宽。
assert not panel.is_debug_active()
panel._open_pid_waveform_window()
assert panel._waveform_window is not None
assert panel._waveform_open_btn.isChecked()
assert panel.waveform_window_open()
assert not panel.waveform_enabled()
assert not changes

window = panel._waveform_window
assert window._receive_btn.isCheckable()
assert not window._receive_btn.isChecked()
assert "未接收" in window._status_state_label.text()

# 顶部菜单固定为两行: 第一行采集，第二行显示与测量。
top_row = window.layout().itemAt(0).layout()
display_row = window.layout().itemAt(1).layout()
assert top_row.indexOf(window._receive_btn) < top_row.indexOf(window._buf_spin)
assert top_row.indexOf(window._pause_btn) == -1
assert display_row.indexOf(window._pause_btn) >= 0
assert display_row.indexOf(window._style_combo) >= 0
assert display_row.indexOf(window._clear_btn) >= 0
assert display_row.indexOf(window._style_combo) < \
    display_row.indexOf(window._follow_check) < \
    display_row.indexOf(window._y_auto_check) < \
    display_row.indexOf(window._err_thr_spin) < \
    display_row.indexOf(window._stats_check) < \
    display_row.indexOf(window._pause_btn) < \
    display_row.indexOf(window._fit_all_btn) < \
    display_row.indexOf(window._clear_btn)
assert top_row.itemAt(top_row.count() - 1).spacerItem() is not None
assert display_row.itemAt(display_row.count() - 1).spacerItem() is not None

# 单位使用独立标签，实时状态与统计字段固定宽度并位于底部状态区。
assert window._rate_spin.suffix() == ""
assert window._buf_spin.suffix() == ""
assert window._rate_unit_label.text() == "Hz"
assert window._buf_unit_label.text() == "点"
bottom_labels = (
    window._status_source_label, window._status_state_label,
    window._status_request_label, window._status_device_label,
    window._status_transport_label, window._stats_target_label,
    window._stats_feedback_label, window._stats_error_label,
)
assert all(label.minimumWidth() == label.maximumWidth() > 0
           for label in bottom_labels)
assert window._bottom_status_layout.indexOf(window._status_state_label) >= 0
assert window._bottom_stats_layout.indexOf(window._stats_target_label) >= 0

# 不依赖 resources/ui_layout.json 中持久化的数据源。
window._source_combo.setCurrentIndex(window._source_combo.findData("current_d"))

# 最大输入值在步进框的实际编辑区域中仍完整可见。
app.processEvents()
for spin in (window._rate_spin, window._buf_spin, window._err_thr_spin):
    original = spin.value()
    blocked = spin.blockSignals(True)
    spin.setValue(spin.maximum())
    line_edit = spin.lineEdit()
    assert line_edit.fontMetrics().horizontalAdvance(line_edit.text()) < \
        line_edit.contentsRect().width()
    spin.setValue(original)
    spin.blockSignals(blocked)

window._receive_btn.setChecked(True)
assert panel.waveform_enabled()
assert changes[-1] == (True, panel.waveform_rate_hz())

# 数据源在窗口顶部切换，与 _tuning_mode 互不影响。
assert panel.waveform_source() == "current_d"
tuning_mode_before = panel.current_tuning_mode()
window._source_combo.setCurrentIndex(window._source_combo.findData("velocity"))
assert panel.waveform_source() == "velocity"
assert panel.current_tuning_mode() == tuning_mode_before
assert panel.waveform_profile().capture_mask == \
    get_pid_debug_profile("velocity").capture_mask
assert changes[-1] == (True, panel.waveform_rate_hz())

# 频率控件上限随当前源同步，速度 1kHz、电流 5kHz。
assert window._rate_spin.maximum() == \
    get_pid_debug_profile("velocity").capture_rate_hz
window._source_combo.setCurrentIndex(window._source_combo.findData("current_d"))
assert window._rate_spin.maximum() == \
    get_pid_debug_profile("current_d").capture_rate_hz

window._rate_spin.setValue(2400)
assert panel.waveform_rate_hz() == 2400
assert changes[-1] == (True, 2400)

# 设备实际采样率与上位机有效接收率独立显示。
panel.set_capture_active(True)
panel.set_capture_metrics(2000, 372.5)
assert window._status_request_label.text() == "请求 2400 Hz"
assert window._status_device_label.text() == "设备 2000 Hz"
assert window._status_transport_label.text() == "上位机接收 372.5 Hz"
for label in (
        window._status_source_label, window._status_state_label,
        window._status_request_label, window._status_device_label,
        window._status_transport_label):
    assert label.fontMetrics().horizontalAdvance(label.text()) < label.width()

# 切整定模式不动波形数据源，也不重启采集。
before = len(changes)
panel._ring_source[0] = PidSource.DEBUG
panel._request_all_params = lambda *_args: None
panel.set_tuning_mode("current_q")
assert panel.waveform_source() == "current_d"
assert len(changes) == before

# 非 DEBUG 状态下的 TRACE 批量数据正常入图；暂停只冻结视图，仍持续保存历史。
panel.feed_trace_data({
    "channel_mask": get_pid_debug_profile("current_d").capture_mask,
    "actual_rate_hz": 2000,
    "first_sample_index": 0,
    "samples": [[1.0, 0.8]],
})
assert len(panel._plot_points) == 1
assert len(window._points) == 1
window._render()
for label in (
        window._stats_target_label, window._stats_feedback_label,
        window._stats_error_label):
    assert label.fontMetrics().horizontalAdvance(label.text()) < label.width()
window._pause_btn.setChecked(True)
panel.feed_trace_data({
    "channel_mask": get_pid_debug_profile("current_d").capture_mask,
    "actual_rate_hz": 2000,
    "first_sample_index": 1,
    "samples": [[1.1, 0.9]],
})
assert len(panel._plot_points) == 2
assert len(window._points) == 2
assert panel.waveform_enabled()
assert "显示已暂停" in window._status_state_label.text()

# 用户设置的存储深度同时约束面板历史和窗口历史，超限只丢最早点。
window._buf_spin.setValue(1000)
assert panel._plot_points.maxlen == 1000
assert window._points.maxlen == 1000
for first in range(2, 1006):
    panel.feed_trace_data({
        "channel_mask": get_pid_debug_profile("current_d").capture_mask,
        "actual_rate_hz": 2000,
        "first_sample_index": first,
        "samples": [[float(first), float(first) - 0.1]],
    })
assert len(panel._plot_points) == 1000
assert len(window._points) == 1000
assert panel._plot_points[0][0] == 6
window._history_rate_hz = 2000.0
window._update_history_label()
assert "历史 1000/1000 点" in window._history_label.text()
assert "6~1005" in window._history_label.text()

# 大视口回看历史前段/中段时，取样范围不能只固定在尾部。
window._follow_check.setChecked(False)
window._plot.setXRange(10, 30, padding=0)
visible = window._visible_points()
assert visible[0][0] <= 10 and visible[-1][0] >= 30
window._plot.setXRange(700, 730, padding=0)
visible = window._visible_points()
assert visible[0][0] <= 700 and visible[-1][0] >= 730

# 每个像素桶分别保留目标、反馈、误差的首/末/极值点，不随机抽样。
envelope_input = [
    (index, float(index), float(index), float(index))
    for index in range(16)]
envelope_input[2] = (2, -100.0, 50.0, 25.0)
envelope_input[5] = (5, 20.0, -100.0, 30.0)
envelope_input[9] = (9, 30.0, 40.0, -100.0)
envelope = window._envelope(envelope_input, 4)
assert len(envelope) <= 16
assert any(point[1] == -100.0 for point in envelope)
assert any(point[2] == -100.0 for point in envelope)
assert any(point[3] == -100.0 for point in envelope)

# X 轴缩放跨度不固定；回到最新仅恢复跟随，不重置用户缩放。
window._follow_check.setChecked(False)
window._plot.setXRange(100, 500, padding=0)
window._on_latest_clicked()
assert window._follow_check.isChecked()
window._render()
span = window._plot.viewRange()[0]
assert 350 <= span[1] - span[0] <= 450

panel.close_waveform_window()
assert panel._waveform_window is None
assert not panel._waveform_open_btn.isChecked()
assert not panel.waveform_enabled()
assert changes[-1][0] is False
assert panel.waveform_source() == "current_d"
assert panel.waveform_rate_hz() == 2400

main_window._pid_debug_heartbeat_timer.stop()
main_window._stats_timer.stop()
main_window._ui_timer.stop()
main_window._reconnect_timer.stop()
print("PID waveform controls: PASS")
