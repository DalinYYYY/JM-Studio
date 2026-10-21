"""Cogging UI/client payload, schema and asynchronous Flash-result regression."""
import os
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from jmproto import JmCmd, CalibState, TopFsm, get_registry
from jmproto.registry import ProtocolRegistry
from core.motor_client import JmClient
from transport.base import Transport
from ui.panels.calibration_panel import CalibrationPanel, CALIB_TABLE_PLAN
from ui.panels.param_panel import ParamPanel
from ui.main_window import MainWindow

class CaptureTransport(Transport):
    def __init__(self):
        super().__init__()
        self.frames = []
    def send(self, cmd, payload=b''):
        self.frames.append((int(cmd), bytes(payload)))
        return True
    def is_open(self):
        return True

app = QApplication([])
transport = CaptureTransport()
client = JmClient(transport)
panel = CalibrationPanel()
panel.send_command.connect(client.send_motion_command)
panel.set_link_active(True)
for method in (1, 2, 3):
    panel._task_buttons[(int(JmCmd.CALIB_LEVEL5), 1, method)].click()
    panel._btn_start.click()
    assert transport.frames[-1] == (0x94, bytes((1, method)))
registry = get_registry()
for method in (0, 4, -1, 1.5, float('nan')):
    try:
        registry.pack_command(JmCmd.CALIB_LEVEL5, {'submode': 1, 'method': method})
    except ValueError:
        pass
    else:
        raise AssertionError(f'invalid method accepted: {method}')
for pid, name in ((184, 'cogging_comp_enable'), (185, 'cogging_comp_gain')):
    spec = registry.get_motor_config_param(pid)
    assert spec.code_name == name and spec.writable
names = ['cogging_comp_crc32', 'cogging_comp_mode', 'cogging_comp_rms_ma', 'cogging_comp_asym_ma', 'cogging_comp_status']
for pid, name in zip(range(85, 90), names):
    spec = registry.get_param(pid)
    assert spec.code_name == name and not spec.writable
assert all(registry.get_motor_config_param(pid) is None for pid in range(186, 191))
results = ParamPanel(registry, source='motor_config', row_plan=CALIB_TABLE_PLAN,
                     show_save=True, save_text='固化参数')
runtime = ParamPanel(registry, source='motor_param',
                     groups=('齿槽诊断', 'CoggingDiagnostics'), show_save=False)
panel.attach_results_panel(results)
panel.attach_runtime_panel(runtime)
assert runtime.param_ids() == list(range(85, 90))
assert all(not runtime._write_buttons[pid].isEnabled() for pid in range(85, 90))
assert all(not (runtime._edit_items[pid].flags() & Qt.ItemFlag.ItemIsEditable)
           for pid in range(85, 90))
assert not runtime._show_save
# 当前用户选择了生成器格式的 motor_param.csv；它也必须对齐线上的 0-based ID。
custom = ProtocolRegistry()
assert custom.reload_params(str(Path(__file__).resolve().parents[2] / 'resources' / 'motor_param.csv'))
assert list(custom.params) == list(range(90))
assert all(custom.get_param(pid).code_name == registry.get_param(pid).code_name for pid in range(90))
custom_panel = ParamPanel(custom, groups=('齿槽诊断', 'CoggingDiagnostics'))
assert custom_panel.param_ids() == list(range(85, 90))
assert all(not custom.get_param(pid).writable for pid in range(85, 90))
reads = []
runtime.read_params.connect(lambda ids: reads.append(list(ids)))
panel.update_state(int(TopFsm.CALIB), 0, 0x94, 1)
panel.on_calib_status({'state': int(CalibState.DONE), 'level': 5, 'submode': 1})
assert panel._cogging_wait_flash and panel._poll_timer.isActive()
panel.on_cogging_result(89, 1 | (1 << 5))
assert panel._cogging_wait_flash
before = len(transport.frames)
panel._btn_start.click()
assert len(transport.frames) == before
panel.update_state(int(TopFsm.READY), 0, 0, 0)
assert panel._poll_timer.isActive()
panel._on_poll_tick()
assert reads[-1] == [89]
panel.on_cogging_result(89, 3 | (3 << 8))
assert not panel._cogging_wait_flash and not panel._poll_timer.isActive()
assert '已写入并校验' in panel._last_op
panel.on_calib_status({'state': int(CalibState.DONE), 'level': 5, 'submode': 1})
panel.on_cogging_result(89, 1 << 2)
assert not panel._cogging_wait_flash and '失败' in panel._last_op
# 使用主窗口真实的发送/应答函数，验证运行时通道和 ID 范围重叠的配置应答隔离。
cogging_results = []
def capture_cogging_result(pid, value):
    cogging_results.append((pid, value))
    panel.on_cogging_result(pid, value)
router = SimpleNamespace(
    _client=client, _pending_param_reads=deque(), _protect_mask_pending=False,
    _fault_level_pending=set(), _pid_theory_pending=None, _pid_commit_phase='idle',
    _export_state=None, _MOTOR_PARAM_KEYS=set(),
    _calib_panel=SimpleNamespace(on_cogging_result=capture_cogging_result),
    _cmd_watchdog=SimpleNamespace(start=lambda: None, stop=lambda: None),
    _log_panel=SimpleNamespace(log=lambda message: None), _pump_param_read_queue=lambda: None)
panel.on_calib_status({'state': int(CalibState.DONE), 'level': 5, 'submode': 1})
router._pending_param_reads.append(results)
MainWindow._on_param_result(router, 88, 0, results.pack_value(88, '1'))
assert panel._cogging_wait_flash and not cogging_results
MainWindow._send_param_read_queued(router, runtime, 89)
assert transport.frames[-1] == (0xE0, bytes((89, 0)))
MainWindow._on_param_result(router, 89, 0, (3 | (2 << 8)).to_bytes(4, 'little'))
assert not panel._cogging_wait_flash and '静态逐点' in panel._last_op
assert cogging_results == [(89, 515)]
assert runtime._current_items[89].text() == '515'
panel.set_link_active(False)
print('PASS three payloads, malformed method rejection, both CSV schemas, readonly UI, runtime routing and Flash refresh')
