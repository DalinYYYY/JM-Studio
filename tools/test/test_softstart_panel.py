"""缓启动面板：配置编辑、信号与自适应布局回归。"""

import os
from pathlib import Path
import sys
import struct
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication, QAbstractSpinBox, QCheckBox, QComboBox, QMessageBox, QPushButton,
)

from jmproto import smoothstart as ss
from ui.panels.softstart_panel import SoftstartPanel
from ui.theme import theme


APP = QApplication.instance() or QApplication([])
font_file = Path("C:/Windows/Fonts/msyh.ttc")
if font_file.exists():
    QFontDatabase.addApplicationFont(str(font_file))
    APP.setFont(QFont("Microsoft YaHei", 9))
APP.setStyleSheet(theme.qss())


class SoftstartPanelTest(unittest.TestCase):
    def setUp(self):
        self.panel = SoftstartPanel()
        self.panel.resize(1300, 800)
        self.panel.show()
        APP.processEvents()
        self.sent = []
        self.reads = []
        self.saves = []
        self.panel.smooth_cfg_set_requested.connect(lambda pid, value: self.sent.append((pid, value)))
        self.panel.smooth_cfg_read_requested.connect(lambda: self.reads.append(True))
        self.panel.smooth_cfg_save_requested.connect(lambda: self.saves.append(True))

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        APP.processEvents()

    def read_config(self, **kwargs):
        self.panel.set_link_active(True)
        kwargs.setdefault('shape', 0)  # 使用明确的线性读回样本，与编译期默认独立。
        self.panel.apply_config(ss.SmoothConfig(**kwargs))

    def test_disconnected_actions_and_initial_values(self):
        for button in (self.panel._read_btn, self.panel._send_btn, self.panel._save_btn):
            self.assertFalse(button.isEnabled())
            button.click()
        self.panel.auto_read_if_connected()
        self.assertEqual((self.sent, self.reads, self.saves), ([], [], []))
        cfg = self.panel._current_config()
        for pid, value in ss.DEFAULTS.items():
            self.assertEqual(cfg.get_by_pid(pid), value)
        self.assertIn("50 ms", self.panel._duration_ms.text())
        self.assertIn("100 ms", self.panel._fallback_ms.text())

    def test_read_fourteen_fields_but_send_only_seven_writable_fields(self):
        self.read_config()
        self.assertEqual(self.panel._dirty_pids(), [])
        for field in self.panel._fields.values():
            field.spin.setValue(field.spin.value() + 1)
        self.panel._enable_check.click()
        self.panel._shape_buttons.button(1).click()
        expected = {pid: value for pid, value, _ in self.panel._dirty_pids()}
        self.assertEqual(set(expected), set(ss.WRITABLE_PIDS))
        self.assertIn("7", self.panel._state_label.text())
        self.assertTrue(self.panel._send_btn.isEnabled())
        self.assertEqual(self.sent, [])
        self.panel._send_btn.click()
        self.assertEqual(dict(self.sent), expected)
        QTest.qWait(350)
        self.assertEqual(len(self.reads), 1)
        self.panel.apply_config(self.panel._current_config())
        self.assertEqual(self.panel._dirty_pids(), [])
        self.assertFalse(self.panel._send_btn.isEnabled())
        self.assertTrue(all(not f.spin.styleSheet() for f in self.panel._fields.values()))

    def test_shape_alone_updates_pending_state_and_estimate(self):
        self.read_config(pos_rate=10.0)
        self.assertEqual(self.panel._estim_result.text(), "5.00 s")
        self.panel._shape_buttons.button(1).click()
        self.assertEqual(self.panel._dirty_pids(), [(ss.PID_SHAPE, 1, 0)])
        self.assertTrue(self.panel._send_btn.isEnabled())
        self.assertEqual(self.panel._estim_result.text(), "7.50 s")
        self.assertEqual(self.panel._curve_preview._total_seconds, 7.5)
        self.panel._estim_delta.setValue(20)
        self.assertEqual(self.panel._estim_result.text(), "3.00 s")
        self.panel._fields["pos_rate"].spin.setValue(0)
        self.panel._duration_spin.setValue(2000)
        self.assertEqual(self.panel._estim_result.text(), "—")
        self.assertAlmostEqual(self.panel._curve_preview._total_seconds, .2)
        self.assertIn("200 ms", self.panel._duration_ms.text())

    def test_float32_readback_does_not_mark_unchanged_fields(self):
        cfg = ss.SmoothConfig()
        raw = struct.pack("<BB2xI10fI", cfg.enable, cfg.shape, cfg.duration,
                          cfg.pos_rate, cfg.vel_rate, cfg.torque_rate, cfg.current_rate,
                          cfg.pos_thresh, cfg.vel_thresh, cfg.torque_thresh,
                          cfg.current_thresh, cfg.voltage_thresh, cfg.duty_thresh,
                          cfg.mode_sw_fallback)
        self.panel.set_link_active(True)
        self.panel.apply_config(ss.unpack_block(raw))
        self.assertEqual(self.panel._dirty_pids(), [])
        self.panel._fields["pos_rate"].spin.setValue(5.1)
        self.assertEqual(self.panel._dirty_pids(), [(ss.PID_POS_RATE, 5.1, 5.0)])
        self.panel._fields["pos_rate"].spin.setValue(5.0)
        self.assertEqual(self.panel._dirty_pids(), [])

    def test_defaults_remain_local_and_save_requires_confirmation(self):
        self.read_config(shape=0, duration=800, mode_sw_fallback=3000, pos_rate=12)
        self.panel._reset_btn.click()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.saves, [])
        cfg = self.panel._current_config()
        for pid in ss.WRITABLE_PIDS:
            value = ss.DEFAULTS[pid]
            self.assertEqual(cfg.get_by_pid(pid), value)
        self.assertEqual(cfg.mode_sw_fallback, 3000)  # 保留固件读回的只读值。
        self.assertEqual({pid for pid, _, _ in self.panel._dirty_pids()},
                         {ss.PID_SHAPE, ss.PID_DURATION, ss.PID_POS_RATE})
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
            self.panel._save_btn.click()
        self.assertEqual((self.sent, self.saves), ([], []))
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.panel._save_btn.click()
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.saves, [True])

    def test_readonly_thresholds_and_trigger_aware_preview(self):
        self.read_config()
        for name, field in self.panel._fields.items():
            if not field.writable:
                self.assertTrue(field.spin.isReadOnly(), name)
                field.spin.setValue(field.current() + 1)
        self.assertEqual(self.panel._dirty_pids(), [])
        self.read_config()
        for delta in (0.05, 0.1):
            self.panel._estim_delta.setValue(delta)
            self.assertEqual(self.panel._estim_result.text(), '0.00 s')
            self.assertIn('阈值', self.panel._estim_note.text())
        self.panel._estim_delta.setValue(1.0)
        self.assertNotEqual(self.panel._estim_result.text(), '0.00 s')
        self.panel._enable_check.click()
        self.assertEqual(self.panel._estim_result.text(), '0.00 s')
        self.assertIn('关闭', self.panel._estim_note.text())
        for pid in set(ss.DEFAULTS) - ss.WRITABLE_PIDS:
            with self.assertRaises(ValueError):
                ss.pack_set_request(pid, ss.DEFAULTS[pid])

    def test_help_and_responsive_layout_without_overlapping_controls(self):
        self.assertFalse(self.panel._help_panel.isVisible())
        self.panel._help_btn.click()
        APP.processEvents()
        self.assertTrue(self.panel._help_panel.isVisible())
        self.panel._help_btn.click()
        for width in (1300, 1024, 760, 1300):
            with self.subTest(width=width):
                self.panel.resize(width, 900)
                APP.processEvents()
                APP.processEvents()
                self.assertEqual(self.panel._scroll.horizontalScrollBar().maximum(), 0)
                parameters = self.panel._parameter_panel.geometry()
                preview = self.panel._estimator_panel.geometry()
                if width >= 960:
                    self.assertLess(parameters.right(), preview.left())
                else:
                    self.assertLess(parameters.bottom(), preview.top())
                controls = [w for w in self.panel.findChildren(
                    (QAbstractSpinBox, QCheckBox, QComboBox, QPushButton)) if w.isVisible()]
                for i, first in enumerate(controls):
                    first_rect = first.rect().translated(first.mapTo(self.panel, QPoint()))
                    for second in controls[i + 1:]:
                        second_rect = second.rect().translated(second.mapTo(self.panel, QPoint()))
                        self.assertFalse(first_rect.intersects(second_rect),
                                         f"Overlapping controls: {first.accessibleName()} / {second.accessibleName()}")


if __name__ == "__main__":
    unittest.main()
