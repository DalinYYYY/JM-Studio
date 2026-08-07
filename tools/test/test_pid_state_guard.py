"""PID source switching state-guard regression tests."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..")))

from PyQt6.QtWidgets import QApplication

from jmproto import JmCmd, JmErr, TopFsm
from transport.virtual_engine.twin_fsm import SystemState
from transport.virtual_engine.twin_responder import TwinResponder
from ui.panels.pid_panel import PidPanel, PidSource


app = QApplication.instance() or QApplication(sys.argv)


def make_panel(top_fsm=None):
    panel = PidPanel()
    panel.set_link_active(True)
    if top_fsm is not None:
        panel.update_state(int(top_fsm), 0, 0, 0)
    panel._source_btn_groups[0].button(int(PidSource.DEBUG)).setChecked(True)
    return panel


# Unknown state must request C1 and must not send A1 using stale assumptions.
panel = make_panel()
state_requests = []
source_requests = []
panel.pid_state_get_requested.connect(lambda: state_requests.append(True))
panel.pid_source_set_requested.connect(
    lambda ring, source: source_requests.append((ring, source)))
panel._on_source_apply_clicked()
assert state_requests == [True]
assert source_requests == []
panel.update_state(int(TopFsm.IDLE), 0, 0, 0)
assert source_requests == [(0, int(PidSource.DEBUG))]

# Clicking the visible tuning-mode button enters the matching DEBUG source.
panel = make_panel(TopFsm.READY)
panel._source_btn_groups[0].button(int(PidSource.DEFAULT)).setChecked(True)
source_requests = []
panel.pid_source_set_requested.connect(
    lambda ring, source: source_requests.append((ring, source)))
panel._on_tuning_mode_id_clicked(0)
assert source_requests == [(0, int(PidSource.DEBUG))]
panel.on_ack(int(JmCmd.PID_SOURCE_SET))
assert panel.is_debug_active()
assert 0 in panel._debug_slider_groups

# READY has zero PWM in firmware and is explicitly allowed for PID reload.
panel = make_panel(TopFsm.READY)
source_requests = []
panel.pid_source_set_requested.connect(
    lambda ring, source: source_requests.append((ring, source)))
panel._on_source_apply_clicked()
assert source_requests == [(0, int(PidSource.DEBUG))]

# RUN must be stopped first and should be rejected before sending A1.
panel = make_panel(TopFsm.RUN)
state_requests = []
source_requests = []
panel.pid_state_get_requested.connect(lambda: state_requests.append(True))
panel.pid_source_set_requested.connect(
    lambda ring, source: source_requests.append((ring, source)))
panel._on_source_apply_clicked()
assert source_requests == []
assert state_requests == [True]
assert "停止" in panel._last_op_label.text()

# The digital twin follows the same IDLE/READY allowlist as real firmware.
responder = TwinResponder()
payload = bytes([0, int(PidSource.DEBUG)])
responder.engine.fsm.sys_state = SystemState.READY
assert responder.on_command(int(JmCmd.PID_SOURCE_SET), payload)[0][1] == b"\x00"
responder.engine.fsm.sys_state = SystemState.RUN
cmd, nack = responder.on_command(int(JmCmd.PID_SOURCE_SET), payload)[0]
assert cmd == int(JmCmd.NACK)
assert nack == bytes([int(JmCmd.PID_SOURCE_SET), int(JmErr.STATE_DENY)])

print("PID state guard: PASS")
