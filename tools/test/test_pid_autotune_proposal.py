"""Host theoretical PID calculation remains an editable proposal only."""

import math
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..")))

from PyQt6.QtWidgets import QApplication

from core.pid_autotune import (
    DEFAULT_POSITION_BW_HZ,
    DEFAULT_VELOCITY_BW_HZ,
    estimate_pid_proposal,
)
from ui.main_window import MainWindow


values = {
    "phase_resistance": 1.0,
    "phase_inductance_d": 0.001,
    "phase_inductance_q": 0.0012,
    "torque_constant": 0.2,
    "rotor_inertia": 0.00001,
    "current_lim": 4.0,
    "current_control_bandwidth": 500.0,
}
proposal = estimate_pid_proposal(values, 0x07, 500.0, 50.0, 10.0)
assert set(("kp_ld", "ki_ld", "kp_lq", "ki_lq", "integral_limit",
            "kp_s", "ki_s", "speed_integral_limit", "kp_p")) <= proposal.keys()
assert math.isclose(proposal["kp_ld"], 2.0 * math.pi * 500.0 * 0.001)
assert math.isclose(proposal["kp_p"], 2.0 * math.pi * 10.0)

zero_proposal = estimate_pid_proposal(values, 0x06)
assert math.isclose(zero_proposal["kp_s"],
                    values["rotor_inertia"] * 2.0 * math.pi *
                    DEFAULT_VELOCITY_BW_HZ / values["torque_constant"])
assert math.isclose(zero_proposal["kp_p"],
                    2.0 * math.pi * DEFAULT_POSITION_BW_HZ)

# MainWindow must not call the legacy A0 path. It reads motor_info and only
# forwards generated values to ParamPanel.set_edit_values().
app = QApplication.instance() or QApplication(sys.argv)
window = MainWindow()
window._client.stop()
pid_panel = window._pid_panel
for spin in (pid_panel._cur_bw_spin,
             pid_panel._vel_bw_spin,
             pid_panel._pos_bw_spin):
    assert spin.value() == 0.0
    assert spin.specialValueText() == ""
window._ensure_open = lambda: True
window._config_panel.param_ids_by_name = lambda _names: [20]
window._config_panel.current_values_by_name = lambda _names=None: dict(values)
applied = []
window._pid_params_panel.set_edit_values = (
    lambda editable: applied.append(dict(editable)) or len(editable))

def complete_read(_panel, _ids):
    window._pid_theory_pending["waiting"].clear()
    window._finish_pid_theory()

window._on_param_read_many = complete_read
window._on_pid_autotune(0x07, 500.0, 50.0, 10.0)
assert applied and "kp_ld" in applied[0] and "kp_p" in applied[0]
assert window._pid_theory_pending is None

window._pid_debug_heartbeat_timer.stop()
window._stats_timer.stop()
window._ui_timer.stop()
window._reconnect_timer.stop()

print("PID autotune proposal: PASS")
