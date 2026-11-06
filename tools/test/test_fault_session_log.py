"""Session diagnostics survive recovery, disconnect and device history reset."""
import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer
from types import SimpleNamespace
from jmproto import fault_diag as fd
import jmproto as jp
from core.fault_session import FaultSessionLog
from ui.panels.fault_info_panel import FaultInfoPanel, AC_STATUS, AC_LEVEL
from ui.main_window import MainWindow

app = QApplication.instance() or QApplication([])


def record(code, first=100, last=200, count=1, value=1.):
    return fd.FaultRecord(code=code, status=fd.STATUS_ACTIVE,
                          first_ms=first, last_ms=last, count=count, value=value)


class SessionTests(unittest.TestCase):
    def test_repeated_samples_update_one_event_and_preserve_first_seen(self):
        log = FaultSessionLog()
        first = log.observe(record(0x3102), active=True)
        wall = first.first_seen
        log.observe(record(0x3102, last=300, count=2), active=True)
        log.observe(record(0x3102, last=300, count=2))
        self.assertEqual(len(log.entries), 1)
        self.assertEqual(first.record.count, 2)
        self.assertEqual(first.first_seen, wall)
        self.assertTrue(first.history_seen)
        log.confirm_active(set())
        self.assertEqual(first.state, '已恢复')
        self.assertEqual(len(log.entries), 1)

    def test_device_reset_cannot_overwrite_an_old_event_with_same_ms(self):
        log = FaultSessionLog()
        old = log.observe(record(0x3102), active=True)
        log.disconnected()
        self.assertEqual(old.state, '待确认')
        log.new_device_epoch()
        new = log.observe(record(0x3102), active=True)
        self.assertNotEqual(old.key, new.key)
        self.assertEqual(old.state, '历史保留')
        self.assertEqual(len(log.entries), 2)
        self.assertFalse(FaultSessionLog().entries)  # 软件新会话不继承日志。

    def test_reset_is_detected_when_history_count_and_first_ms_are_reused(self):
        log = FaultSessionLog()
        old = log.observe(record(0x3102, last=5000, count=50), active=True)
        new = log.observe(record(0x3102, last=200, count=1), active=True)
        self.assertEqual(len(log.entries), 2)
        self.assertEqual(old.record.last_ms, 5000)
        self.assertEqual(new.record.last_ms, 200)
        self.assertEqual(old.state, '历史保留')


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.panel = FaultInfoPanel()
        self.panel.set_link_active(True)
        self.requests = []
        self.panel.fault_detail_requested.connect(lambda t, i: self.requests.append((t, i)))

    def tearDown(self):
        self.panel.set_link_active(False)
        self.panel.deleteLater()
        app.processEvents()

    def collect(self, active=(), history=()):
        p = self.panel
        p.on_summary_received(fd.FaultSummary(active_cnt=len(active), hist_cnt=len(history)))
        limit = 300
        while p._pull_active:
            limit -= 1
            self.assertGreater(limit, 0)
            kind, index = p._pull_request
            rows = active if kind == fd.TYPE_ACTIVE else history
            p.on_detail_received(kind, index, rows[index])

    def test_all_levels_remain_after_recovery_theme_disconnect_and_device_restart(self):
        codes = [int(next(r['code'] for r in jp.load_fault_csv() if r['level'] == level), 0)
                 for level in ('故障级', '异常级', '警告级')]
        rows = [record(code) for code in codes]
        self.collect(rows, rows)
        p = self.panel
        self.assertEqual(p._active_table.rowCount(), 3)
        self.assertEqual(p._hist_table.rowCount(), 3)
        self.assertEqual({p._active_table.item(i, AC_LEVEL).text() for i in range(3)},
                         {'故障级', '异常级', '警告级'})
        first_seen = [e.first_seen for e in p._session_log.entries.values()]
        self.collect((), rows)
        self.assertTrue(all(p._active_table.item(i, AC_STATUS).text() == '已恢复' for i in range(3)))
        p.apply_theme()
        p.set_link_active(False)
        self.assertEqual(p._active_table.rowCount(), 3)
        self.assertEqual(p._hist_table.rowCount(), 3)
        self.assertEqual([e.first_seen for e in p._session_log.entries.values()], first_seen)
        p.set_link_active(True)
        self.collect()  # 下位机重启后的空摘要和空历史。
        self.assertEqual(p._active_table.rowCount(), 3)
        self.assertEqual(p._hist_table.rowCount(), 3)
        self.collect([rows[0]], [rows[0]])
        self.assertEqual(p._active_table.rowCount(), 4)
        self.assertEqual(p._hist_table.rowCount(), 4)

    def test_history_backfills_short_warning_and_has_no_session_100_row_limit(self):
        rows = [record(0x3303, first=i+1, last=i+10) for i in range(100)]
        self.collect((), rows)  # 告警已恢复，不在活动列表中。
        self.assertEqual(self.panel._active_table.rowCount(), 100)
        self.collect((), [record(0x3303, first=1001, last=1002)])
        self.assertEqual(self.panel._active_table.rowCount(), 101)
        self.assertEqual(self.panel._hist_table.rowCount(), 101)
        for i in range(60):
            self.panel._append_recent(f'event {i}')
        self.assertEqual(len(self.panel._history), 60)

    def test_manual_local_clear_is_available_offline_and_does_not_clear_device(self):
        p = self.panel
        row = record(0x3303)
        self.collect((), [row])
        device_clears = []
        p.fault_clear_requested.connect(device_clears.append)
        p.set_link_active(False)
        self.assertTrue(p._btn_clear_log.isEnabled())
        p._btn_clear_log.click()
        self.assertEqual(p._active_table.rowCount(), 0)
        self.assertEqual(p._hist_table.rowCount(), 0)
        self.assertEqual(device_clears, [])
        p.set_link_active(True)
        self.collect((), [row])  # 不原样填回用户刚清空的设备旧历史。
        self.assertEqual(p._active_table.rowCount(), 0)
        self.collect((), [record(0x3303, last=300, count=2)])
        self.assertEqual(p._active_table.rowCount(), 1)

    def test_device_clear_and_read_empty_history_do_not_erase_local_log(self):
        self.collect((), [record(0x3303)])
        p = self.panel
        calls = []
        p.fault_clear_requested.connect(calls.append)
        p._btn_clear_hist.click()
        self.assertEqual(calls, [fd.CLEAR_FLAG_HISTORY])
        self.collect()
        p._on_load_history()
        self.assertEqual(p._active_table.rowCount(), 1)
        self.assertEqual(p._hist_table.rowCount(), 1)

    def test_background_collection_and_incomplete_pull_preserve_records(self):
        p = self.panel
        summaries = []
        p.state_requested.connect(lambda: summaries.append('state'))
        p.fault_summary_requested.connect(lambda: summaries.append('summary'))
        p.pause_auto_refresh()
        self.assertTrue(p._auto_timer.isActive())
        p._on_auto_tick()
        self.assertEqual(summaries, ['state', 'summary'])
        self.collect([record(0x3102)])
        p.on_summary_received(fd.FaultSummary(active_cnt=2))
        p.on_detail_received(fd.TYPE_ACTIVE, 0, record(0x3303))
        pending = p._active_codes_new
        p.on_summary_received(fd.FaultSummary(active_cnt=2))
        self.assertIs(p._active_codes_new, pending)
        summaries.clear()
        p._on_auto_tick()
        self.assertEqual(summaries, [])
        p._on_pull_timeout()
        self.assertEqual(p._active_table.rowCount(), 2)
        self.assertEqual(p._active_table.item(0, AC_STATUS).text(), '待确认')
        p.set_link_active(False)
        p.on_detail_received(fd.TYPE_ACTIVE, 1, record(0x4101))
        self.assertEqual(p._active_table.rowCount(), 2)  # 断线后的迟到应答不改日志。

    def test_warning_summary_has_warning_level(self):
        self.panel.on_summary_received(fd.FaultSummary(
            top_code=0x3303, active_cnt=1, level_active=fd.LEVEL_WARNING_BIT))
        self.assertIn('警告 (1 项)', self.panel._lbl_title.text())

    def test_background_read_requests_wait_for_can_node_confirmation(self):
        window = MainWindow()
        original = window._client
        original.stop()
        calls = []
        window._client = SimpleNamespace(
            is_open=lambda: True,
            query_state=lambda: calls.append('state'),
            fault_summary_get=lambda: calls.append('summary'),
            fault_detail_get=lambda t, i: calls.append(('detail', t, i)))
        panel = window._fault_info_panel
        for pending in (True, False):
            window._can_commissioning_required = pending
            panel.state_requested.emit()
            panel.fault_summary_requested.emit()
            panel.fault_detail_requested.emit(fd.TYPE_ACTIVE, 0)
            if pending:
                self.assertEqual(calls, [])
            else:
                self.assertEqual(calls, ['state', 'summary', ('detail', fd.TYPE_ACTIVE, 0)])
        window._client = original
        for timer in window.findChildren(QTimer):
            timer.stop()
        window.hide()
        window.deleteLater()
        app.processEvents()


if __name__ == '__main__':
    unittest.main(verbosity=2)
