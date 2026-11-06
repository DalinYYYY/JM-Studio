"""Ring-buffer, history browsing, CSV and independent clearing regressions."""
import csv
from collections import deque
import os
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication
from ui.curve_export import export_curve_csv, write_curve_csv
from ui.panels.plot_panel import PlotPanel, _SeriesBuffer
from ui.panels.power_panel import PowerPanel
from ui.panels.debug_plot_window import DebugPlotWindow
from ui.panels.pid_waveform_window import PidWaveformWindow
from ui.plot_history import PointHistory

app = QApplication.instance() or QApplication([])


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


class HistoryTests(unittest.TestCase):
    def test_series_wrap_and_large_batches(self):
        rng = random.Random(17)
        actual = _SeriesBuffer(17)
        reference = deque(maxlen=17)
        index = 0
        for _ in range(40):
            size = rng.randrange(1, 50)
            xs = np.arange(index, index + size, dtype=np.float32)
            actual.append_batch(xs, xs * 2)
            reference.extend((float(x), float(2*x)) for x in xs)
            index += size
            xs, ys = actual.arrays_view()
            self.assertEqual(list(zip(xs.tolist(), ys.tolist())), list(reference))
            lo, hi = index - 12, index - 5
            xs, ys = actual.arrays_view(lo, hi)
            self.assertEqual(list(zip(xs.tolist(), ys.tolist())),
                             [p for p in reference if lo <= p[0] <= hi])
            self.assertAlmostEqual(actual.interpolate(index - 4.5), (index - 4.5)*2)

    def test_snapshot_is_stable_and_bounded(self):
        buf = _SeriesBuffer(300000)
        xs = np.arange(300000, dtype=np.float32)
        buf.append_batch(xs, xs)
        ts, ys = buf.arrays_view(299950, 299960)
        self.assertEqual(len(ts), 11)
        self.assertEqual(len(buf._cache_ts), 11)
        saved = ys.copy()
        buf.append_batch(xs + 300000, xs * -1)
        buf.clear()
        np.testing.assert_array_equal(ys, saved)
        self.assertEqual(len(buf.arrays_view()[0]), 0)
        with self.assertRaises(ValueError):
            _SeriesBuffer(0)

    def test_point_history_random_access_and_resize(self):
        history = PointHistory(maxlen=7)
        reference = deque(maxlen=7)
        for start in range(0, 100, 3):
            batch = [(i, i*2, i*3, i*4) for i in range(start, start+3)]
            history.extend(batch)
            reference.extend(batch)
            expected = list(reference)
            self.assertEqual(list(history), expected)
            self.assertEqual(history[-1], expected[-1])
            for sl in (slice(None), slice(1, -1), slice(None, None, -1), slice(2, 5)):
                self.assertEqual(history[sl], expected[sl])
        self.assertEqual(list(PointHistory(history, maxlen=3)), list(reference)[-3:])


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.hide()
            widget.deleteLater()
        app.processEvents()

    def panel(self, kind):
        panel = kind()
        self.widgets.append(panel)
        return panel

    def test_realtime_capacity_and_browsing(self):
        panel = self.panel(PlotPanel)
        self.assertEqual(panel._spin_buffer.maximum(), 2000000)
        self.assertGreater(panel._spin_window.maximum(), 50000)
        panel._buffers['pos'].append_batch(np.arange(100, dtype=np.float32), np.arange(100, dtype=np.float32))
        panel._latest_idx = 99
        panel._on_buffer_changed(1000)
        self.assertEqual(panel._buffers['pos'].last_time(), 99)
        panel._spin_buffer.setValue(600000)
        panel._spin_window.setValue(120000)
        self.assertEqual(panel.get_opts()['window_points'], 120000)
        self.assertEqual(panel.get_opts()['buffer_points'], 600000)
        self.assertEqual(panel._buffers['pos'].first_time(), 0)
        panel.show()
        app.processEvents()
        panel._timer.stop()
        self.assertTrue(panel._chk_follow.isChecked())
        panel._plots['pos']['plot'].setXRange(20, 40, padding=0)
        panel._plots['pos']['plot'].vb.sigRangeChangedManually.emit([True, False])
        self.assertFalse(panel._chk_follow.isChecked())
        panel._refresh(force=True)
        xs, _ = panel._plots['pos']['curves']['pos'].getData()
        self.assertGreaterEqual(min(xs), 20)
        self.assertLessEqual(max(xs), 40)
        panel.hide()
        self.assertFalse(panel._timer.isActive())
        panel._buffers['pos'].append(100, 999)
        panel._refresh()
        self.assertTrue(panel._buffers['pos'].dirty)
        panel._chk_follow.setChecked(True)
        panel.show()
        app.processEvents()
        self.assertFalse(panel._buffers['pos'].dirty)

    def test_csv_aligns_missing_fields_and_keeps_all_history(self):
        panel = self.panel(PlotPanel)
        panel._buffers['pos'].append(0, 1.25)
        panel._buffers['pos'].append(1, 2.5)
        panel._buffers['iq'].append(1, -.75)
        panel._buffers['pos_ref'].append(0, float('nan'))
        panel._current_checks['iq'].setChecked(False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'曲线.csv'
            panel._write_csv(path)
            rows = read_csv(path)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['pos_rad'], '1.25')
            self.assertEqual(rows[0]['iq_A'], '')
            self.assertEqual(rows[0]['pos_ref_rad'], '')
            self.assertEqual(rows[1]['iq_A'], '-0.75')
            panel._buffers['pos'].append(2, 3)
            # Empty export replaces a previous file with a header, not stale rows.
            write_curve_csv(path, [('empty', (np.array([]), np.array([])))])
            self.assertEqual(path.read_text(encoding='utf-8-sig').strip(), 'data_point,empty')

    def test_power_exports_and_independent_clear(self):
        panel = self.panel(PowerPanel)
        panel._append_bus(1, 24, -2, -48)
        panel._buf_temp_motor.append(1, 35)
        panel._buf_temp_fet.append(1, 31)
        panel._last_temp_t = 1
        panel._t0 = 1000
        with tempfile.TemporaryDirectory() as directory:
            bus = Path(directory)/'bus.csv'
            temp = Path(directory)/'temp.csv'
            panel._write_bus_csv(bus)
            panel._write_temperature_csv(temp)
            row = read_csv(bus)[0]
            self.assertEqual(row['power_W'], '-48')
            self.assertEqual(row['time_s'], '1')
            row = read_csv(temp)[0]
            self.assertEqual(row['temp_motor_C'], '35')
        panel._btn_bus_clear.click()
        self.assertIsNone(panel._buf_vbus.last_time())
        self.assertEqual(panel._buf_temp_motor.last_time(), 1)
        self.assertEqual(panel._t0, 1000)
        self.assertFalse(len(panel._curve_power.getData()[0] or []))
        panel._append_bus(2, 24, 1, 24)
        panel._btn_temp_clear.click()
        self.assertEqual(panel._buf_power.last_time(), 2)
        self.assertIsNone(panel._buf_temp_motor.last_time())
        self.assertIsNone(panel._last_temp_t)
        self.assertEqual(panel._t0, 1000)

    def test_background_export_uses_stable_snapshot(self):
        panel = self.panel(PlotPanel)
        panel._buffers['pos'].append(0, 1.25)
        loop = QEventLoop()
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'background.csv'
            with (patch('ui.curve_export.QFileDialog.getSaveFileName', return_value=(str(path), 'CSV')),
                  patch('ui.curve_export.QMessageBox.information', side_effect=lambda *a: loop.quit()),
                  patch('ui.curve_export.QMessageBox.warning', side_effect=lambda *a: loop.quit())):
                export_curve_csv(panel, panel._btn_export, 'test', panel._csv_snapshot)
                self.assertFalse(panel._btn_export.isEnabled())
                panel.clear()  # 正在导出的快照不能被清空或后续采集修改。
                timeout.start(10000)
                loop.exec()
                timeout.stop()
            self.assertTrue(panel._btn_export.isEnabled())
            self.assertEqual(read_csv(path)[0]['pos_rad'], '1.25')

    def test_pid_nearest_and_view_are_bounded(self):
        panel = self.panel(PidWaveformWindow)
        panel.set_points((i, 1., .9, .1) for i in range(200000))
        panel._follow = False
        panel._plot.setXRange(10000, 10010, padding=0)
        self.assertLessEqual(len(panel._visible_points()), 13)
        self.assertEqual(panel._nearest_sample(100000.4)[0], 100000)
        self.assertEqual(panel._nearest_sample(-5)[0], 0)
        self.assertEqual(panel._nearest_sample(300000)[0], 199999)
        self.assertFalse(panel._render_timer.isActive())

    def test_debug_browsing_and_hidden_timer(self):
        panel = self.panel(DebugPlotWindow)
        panel.feed_debug_batch([(float(i), float(i*2)) for i in range(500)])
        panel.show()
        app.processEvents()
        panel._timer.stop()
        panel._plot.setXRange(20, 40, padding=0)
        panel._plot.vb.sigRangeChangedManually.emit([True, False])
        self.assertFalse(panel._chk_follow.isChecked())
        xs, _ = panel._curves['dbg_0'].getData()
        self.assertGreaterEqual(xs[0], 20)
        self.assertLessEqual(xs[-1], 40)
        panel.hide()
        self.assertFalse(panel._timer.isActive())

    def test_power_toolbars_use_one_row(self):
        panel = self.panel(PowerPanel)
        panel.resize(1280, 720)
        panel.show()
        app.processEvents()
        for names in (('_spin_bus_depth', '_btn_bus_pause', '_btn_bus_export', '_btn_bus_clear'),
                      ('_spin_temp_depth', '_btn_temp_pause', '_btn_temp_export', '_btn_temp_clear')):
            buttons = [getattr(panel, name) for name in names]
            self.assertTrue(all(b.parentWidget() is buttons[0].parentWidget() for b in buttons))
            centers = [b.geometry().center().y() for b in buttons]
            self.assertLessEqual(max(centers) - min(centers), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
