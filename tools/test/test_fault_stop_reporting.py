"""FAULT with zero masks/empty diagnostics must never be displayed as healthy."""
import os
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont, QFontDatabase
from jmproto import TopFsm
from jmproto.feedback import FeedbackData
from jmproto import fault_diag as fd
from ui.panels.fault_info_panel import FaultInfoPanel
from ui.panels.feedback_panel import FeedbackPanel
from ui.main_window import MainWindow
from ui.theme import theme

app = QApplication.instance() or QApplication(sys.argv)
font_file = Path("C:/Windows/Fonts/msyh.ttc")
if font_file.exists():
    QFontDatabase.addApplicationFont(str(font_file))
app.setFont(QFont("Microsoft YaHei UI", 9))
theme.set("dark")
app.setStyleSheet(theme.qss())
panel = FaultInfoPanel()
panel.set_link_active(True)
events = []
panel.active_faults_changed.connect(lambda values: events.append(values))
panel.on_summary_received(fd.FaultSummary())
assert "正常" not in panel._lbl_title.text()  # No state has been received yet.
panel.update_state(int(TopFsm.RUN), 7, 0x15, 1)
assert "正常" in panel._lbl_title.text()
panel.update_state(int(TopFsm.FAULT), 7, 0x15, 0)
assert "未上报故障码" in panel._lbl_title.text()
assert "未上报" in panel._lbl_top.text()
assert "无故障" not in panel._lbl_compat.text()
assert events[-1] == ["故障停机（未上报故障码）"]
panel.on_summary_received(fd.FaultSummary())
assert "正常" not in panel._lbl_title.text()
panel.update_fault_mask(0)
panel.apply_theme()
assert "未上报故障码" in panel._lbl_title.text()

# Genuine diagnostic records replace the missing-code message and remain queryable.
panel.on_summary_received(fd.FaultSummary(top_code=0x4106, active_cnt=1,
    hist_cnt=1, compat_mask=0x400, level_active=1))
assert "0x4106" in panel._lbl_top.text() and "故障 (1 项)" in panel._lbl_title.text()
panel.on_detail_received(fd.TYPE_ACTIVE, 0, fd.FaultRecord(code=0x4106, status=1, count=1))
assert 0x4106 in panel._active_codes
assert panel._HEAT_RED in panel._heat_cells[(154, 20)].styleSheet()
panel.on_fault_enable_mask_received(154, 0)
panel.on_fault_enable_mask_received(155, 0)
assert panel._HEAT_RED in panel._heat_cells[(154, 20)].styleSheet()
assert "常规检测已屏蔽" in panel._heat_cells[(154, 20)].toolTip()
panel.on_summary_received(fd.FaultSummary(hist_cnt=1))
assert "未上报故障码" in panel._lbl_title.text()  # FAULT has not cleared yet.
panel.update_state(int(TopFsm.IDLE), 0, 0, 0)
assert "正常" in panel._lbl_title.text()
panel.set_link_active(False)
assert panel._top_fsm is None and panel._summary is None
assert "正常" not in panel._lbl_title.text()

# A state arriving before the first summary must show the same warning.
panel.set_link_active(True)
panel.update_state(int(TopFsm.FAULT), 7, 0x15, 0)
assert "未上报故障码" in panel._lbl_title.text()
panel.on_summary_received(fd.FaultSummary())
assert "未上报故障码" in panel._lbl_title.text()

# Manual/automatic/clear refreshes query C1 as well as AA, so cached FAULT clears.
requests = []
panel.state_requested.connect(lambda: requests.append("state"))
panel.fault_summary_requested.connect(lambda: requests.append("summary"))
panel._tab_visible = True
panel._chk_auto.setChecked(True)
requests.clear()
panel._on_auto_tick()
assert requests == ["state", "summary"]
requests.clear()
panel.refresh_after_clear()
assert requests == ["state", "summary"]
requests.clear()
panel.auto_read_if_connected()
assert requests == ["state", "summary"]

# The FOC view also uses state when its legacy compatibility mask is zero.
feedback = FeedbackPanel()
feedback.update_state(int(TopFsm.FAULT), 7, 0x15, 0)
assert "无故障" not in feedback._card_state._val_labels["_fault"].text()
feedback.update_feedback(FeedbackData())
assert "故障态" in feedback._card_state._val_labels["_fault"].text()
feedback.update_state(int(TopFsm.IDLE), 0, 0, 0)
assert "无故障" in feedback._card_state._val_labels["_fault"].text()
feedback.update_state(int(TopFsm.FAULT), 7, 0x15, 0)
feedback.set_link_active(False)
assert "故障态" not in feedback._card_state._val_labels["_fault"].text()

# Exercise the real main-window dispatch without opening transports or saving UI settings.
seen = []
noop = SimpleNamespace(update_state=lambda *args: None)
fake = SimpleNamespace(_latest_state=(int(TopFsm.FAULT), 7, 0x15, 0),
    _feedback_panel=noop, _state_panel=noop, _calib_panel=noop, _pid_panel=noop,
    _fault_info_panel=SimpleNamespace(update_state=lambda *args: seen.append(args)),
    _control_panel=SimpleNamespace(set_enabled_state=lambda value: None),
    _feedback_pending=deque(), _feedback_dropped=0)
MainWindow._on_ui_tick(fake)
assert seen == [(int(TopFsm.FAULT), 7, 0x15, 0)]
assert fake._latest_state is None

output = os.environ.get("FAULT_UI_SCREENSHOT")
if output:
    panel.resize(1197, 520)
    panel.show()
    app.processEvents()
    assert panel.grab().save(output)
panel.set_link_active(False)
print("PASS fault UI: empty diagnostics/FAULT, record arrival, state clearing, reconnect, polling and dispatch")
