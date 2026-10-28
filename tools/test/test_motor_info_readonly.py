"""Readonly UI, legacy CSV, JSON import and virtual-protocol regression."""
import csv
import json
import os
import sys
import tempfile
from collections import deque
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
gui = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(gui))
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox
from jmproto import JmCmd, JmErr, codec
from jmproto.registry import ProtocolRegistry
from ui.main_window import MainWindow
from ui.panels.param_panel import ParamPanel
from transport.virtual_engine.twin_responder import TwinResponder

app = QApplication([])
reg = ProtocolRegistry()
ro = {0, 4, 5, 127}
assert {pid for pid, spec in reg.motor_config_params.items() if not spec.writable} == ro
assert len(reg.motor_config_params) == 113
assert all(reg.get_motor_config_param(pid).writable for pid in (16, 20, 28, 49, 64, 176, 184, 185))
panel = ParamPanel(reg, source='motor_config', show_save=True)
assert panel._write_buttons[176].isEnabled()
assert panel._edit_items[176].flags() & Qt.ItemFlag.ItemIsEditable
writes = []
panel.write_param.connect(lambda pid, value: writes.append((pid, value)))
for pid in ro:
    assert not panel._write_buttons[pid].isEnabled()
    assert not (panel._edit_items[pid].flags() & Qt.ItemFlag.ItemIsEditable)
    panel._edit_items[pid].setText('1')
    panel._on_write_clicked(pid)
assert not writes
batch_writes = []
panel.write_params.connect(lambda values: batch_writes.extend(values))
panel.set_value(64, '0.5')
panel.set_edit_values({'kp_ld': 1.25})
panel._on_write_all_clicked()
assert batch_writes == [(64, '1.25')]
panel.set_value(176, '1')
panel.set_edit_values({'softstart_valid': 0})
panel._on_write_clicked(176)
assert writes[-1][0] == 176 and int(writes[-1][1], 0) == 0
batch_writes.clear()
panel._on_write_all_clicked()
assert [pid for pid, _ in batch_writes] == [64, 176]
assert int(batch_writes[-1][1], 0) == 0

logs = []
router = SimpleNamespace(
    _registry=reg, _config_panel=panel, _ensure_open=lambda: True,
    _panel_remote_supported=lambda p: True, _param_write_queue=deque(),
    _pump_param_write_queue=lambda: None,
    _log_panel=SimpleNamespace(log=logs.append, log_warn=logs.append))
router._on_param_write_many = lambda p, values: MainWindow._on_param_write_many(router, p, values)
for pid in ro:
    MainWindow._on_param_write(router, panel, pid, '1')
assert not router._param_write_queue
MainWindow._on_param_write_many(router, panel, [(pid, '1') for pid in ro] + [(64, '1.25')])
assert [pid for _, pid, _ in router._param_write_queue] == [64]
router._param_write_queue.clear()
MainWindow._on_param_write(router, panel, 176, '0')
assert [(pid, codec.rd_u32(value, 0)) for _, pid, value in router._param_write_queue] == [(176, 0)]
router._param_write_queue.clear()

with tempfile.TemporaryDirectory(prefix='jointmotor-ro-gui-') as work:
    work = Path(work)
    # 旧 CSV 的 RW 标记及字段重命名都不能撤销固定 ID 的只读权限。
    with (gui / 'resources/motor_info.csv').open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        fields, rows = reader.fieldnames, list(reader)
    for row in rows:
        if int(row['Index']) == 16:
            row['Access'] = 'RO'  # 已开放的 ID16 兼容旧权限表。
        if int(row['Index']) in ro:
            row['Access'] = 'RW'
            row['VariableName'] = 'legacy_alias_' + row['VariableName']
    legacy = work / 'legacy.csv'
    with legacy.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fields); writer.writeheader(); writer.writerows(rows)
    old = ProtocolRegistry()
    assert old.reload_motor_config(str(legacy))
    assert all(not old.get_motor_config_param(pid).writable for pid in ro)
    assert old.get_motor_config_param(16).writable
    assert old.get_motor_config_param(176).writable
    backup = work / 'backup.json'
    backup.write_text(json.dumps({'_meta': {'config_version': 0},
                                 'fields': {**{str(pid): 1 for pid in ro}, '64': 1.25, '176': 0}}), encoding='utf-8')
    saved_open = QFileDialog.getOpenFileName
    saved_question, saved_info = QMessageBox.question, QMessageBox.information
    try:
        QFileDialog.getOpenFileName = lambda *a, **k: (str(backup), '')
        QMessageBox.question = lambda *a, **k: QMessageBox.StandardButton.Yes
        QMessageBox.information = lambda *a, **k: QMessageBox.StandardButton.Ok
        MainWindow._on_motor_info_import(router)
        panel.clear_edit_values()
        backup.write_text(json.dumps({'params': [{'param_id': 176, 'value': '0'}]}), encoding='utf-8')
        panel._on_import_clicked()
        assert panel._edit_items[176].text() == '0'
        batch_writes.clear()
        panel._on_write_all_clicked()
        assert batch_writes == [(176, '0')]
    finally:
        QFileDialog.getOpenFileName = saved_open
        QMessageBox.question, QMessageBox.information = saved_question, saved_info
    assert [pid for _, pid, _ in router._param_write_queue] == [64, 176]
    assert codec.rd_u32(router._param_write_queue[-1][2], 0) == 0

resp = TwinResponder()
for pid in ro:
    frame = resp.on_command(int(JmCmd.MOTOR_INFO_WRITE), codec.wr_u16(pid) + b'\0' * 4)[0]
    assert frame == (int(JmCmd.NACK), bytes((int(JmCmd.MOTOR_INFO_WRITE), int(JmErr.READ_ONLY))))
    frame = resp.on_command(int(JmCmd.MOTOR_INFO_RESET), codec.wr_u16(pid))[0]
    assert frame == (int(JmCmd.NACK), bytes((int(JmCmd.MOTOR_INFO_RESET), int(JmErr.READ_ONLY))))
before = resp._motor_config_value(resp.reg.get_motor_config_param(5))
resp.on_command(int(JmCmd.MOTOR_INFO_SAVE), b'')
assert resp._motor_config_value(resp.reg.get_motor_config_param(5)) == before + 1
for valid in (1, 0):
    frame = resp.on_command(int(JmCmd.MOTOR_INFO_WRITE), codec.wr_u16(176) + codec.wr_u32(valid))[0]
    assert frame == (int(JmCmd.MOTOR_INFO_WRITE), codec.wr_u16(176) + bytes((int(JmErr.OK),)))
    frame = resp.on_command(int(JmCmd.MOTOR_INFO_READ), codec.wr_u16(176))[0]
    assert frame[0] == int(JmCmd.MOTOR_INFO_READ) and codec.rd_u32(frame[1], 3) == valid
frame = resp.on_command(int(JmCmd.MOTOR_INFO_WRITE), codec.wr_u16(176) + codec.wr_u32(2))[0]
assert frame == (int(JmCmd.NACK), bytes((int(JmCmd.MOTOR_INFO_WRITE), int(JmErr.OUT_OF_RANGE))))
assert resp._motor_config_value(resp.reg.get_motor_config_param(176)) == 0
resp.on_command(int(JmCmd.MOTOR_INFO_WRITE), codec.wr_u16(176) + codec.wr_u32(1))
frame = resp.on_command(int(JmCmd.MOTOR_INFO_RESET), codec.wr_u16(176))[0]
assert frame == (int(JmCmd.MOTOR_INFO_RESET), bytes((int(JmErr.OK),)))
assert resp._motor_config_value(resp.reg.get_motor_config_param(176)) == 0
for pid in range(85, 90):
    frame = resp.on_command(int(JmCmd.PARAM_WRITE), codec.wr_u16(pid) + b'\0' * 4)[0]
    assert frame == (int(JmCmd.NACK), bytes((int(JmCmd.PARAM_WRITE), int(JmErr.READ_ONLY))))
print('PASS four readonly fields remain protected; is_calibrated writable with legacy CSV; softstart_valid editable, queued, imported, written/read/reset and range-checked')
