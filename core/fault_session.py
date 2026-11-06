"""上位机会话内的告警日志；设备状态刷新不能删除已经收到的记录。"""

from dataclasses import dataclass, replace
from datetime import datetime
import math

from jmproto.fault_diag import FaultRecord


@dataclass
class FaultLogEntry:
    key: tuple
    record: FaultRecord
    first_seen: str
    last_seen: str
    state: str
    history_seen: bool = False


class FaultSessionLog:
    """同一设备事件重复轮询只更新一行；新事件追加，不设自动淘汰上限。"""

    def __init__(self, clock=None):
        self.entries = {}
        self.epoch = 0
        self._clock = clock or datetime.now

    @staticmethod
    def fingerprint(record):
        value = float(record.value)
        # NaN 不与自身相等；比较稳定表示，避免同一无效值被误认为新数据。
        value = value if math.isfinite(value) else repr(value)
        return record.count, record.first_ms, record.last_ms, value

    def observe(self, record, active=False):
        if not record.code:
            return None
        key = self.epoch, int(record.code), int(record.first_ms)
        now = self._clock().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        entry = self.entries.get(key)
        if entry is not None and active and (
                record.last_ms < entry.record.last_ms or record.count < entry.record.count):
            # 下位机复位后即使历史条数相同、首次触发 ms 恰好相同，也保留旧事件。
            # 历史应答可能比活动应答旧，不能单凭历史样本回退推断复位。
            self.new_device_epoch()
            key = self.epoch, int(record.code), int(record.first_ms)
            entry = None
        if entry is None:
            entry = FaultLogEntry(key, replace(record), now, now,
                                  '活动' if active else '历史记录')
            self.entries[key] = entry
        elif record.last_ms >= entry.record.last_ms:
            if self.fingerprint(record) != self.fingerprint(entry.record):
                entry.last_seen = now
            entry.record = replace(record)
        if active:
            entry.state = '活动'
        else:
            entry.history_seen = True
        return entry

    def confirm_active(self, keys, complete=True):
        for key, entry in self.entries.items():
            if key[0] != self.epoch:
                continue
            if key in keys:
                entry.state = '活动'
            elif entry.state in ('活动', '待确认'):
                entry.state = '已恢复' if complete else '待确认'

    def disconnected(self):
        for entry in self.entries.values():
            if entry.state == '活动':
                entry.state = '待确认'

    def new_device_epoch(self):
        """设备历史复位/换设备后保留旧记录，避免设备 ms 计时重用时覆盖旧事件。"""
        for entry in self.entries.values():
            if entry.state in ('活动', '待确认'):
                entry.state = '历史保留'
        self.epoch += 1

    def clear(self):
        self.entries.clear()
