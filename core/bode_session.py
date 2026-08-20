"""0x76 Bode 会话编排：命令、TRACE 所有权、状态和导出。"""

from __future__ import annotations

import csv
import time
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from jmproto import JmCmd, JmErr, err_name_cn_short
from .bode_analysis import BodeAnalyzer, BodePoint


# 0x76 启动被拒的错误码 → 处理建议(中文, 供状态栏直接提示用户)
_NACK_HINTS = {
    int(JmErr.STATE_DENY): "请先使能电机进入就绪状态(READY)后再启动扫频",
    int(JmErr.BUSY): "扫频或 TRACE 会话占用中，请先停止",
    int(JmErr.OUT_OF_RANGE): "固件校验未通过，请核对: ①|偏置|+幅值 ≤ 电机限幅"
                             "(速度模式: 最大转速 / 电流模式: 峰值电流 / 力矩模式: 峰值转矩)"
                             " ②频点数 ≤ 1024 ③结束频率 ≤ 1000Hz ④预计时长 ≤ 600s",
    int(JmErr.LENGTH): "固件载荷格式不匹配，请更新固件后重试",
}


class BodeSession(QObject):
    status_changed = pyqtSignal(str, dict)
    point_updated = pyqtSignal(object)
    raw_updated = pyqtSignal(int)          # 当前频点原始样本有新增(实时波形)
    finished = pyqtSignal(bool, str)

    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    COMPLETE = "COMPLETE"
    ABORTED = "ABORTED"
    ERROR = "ERROR"

    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.client = client
        self.state = self.IDLE
        self.config = {}
        self.session_id = 0
        self.point_count = 0
        self.duration_x100 = 0
        self.started_at = 0.0
        self.received_packets = 0
        self.raw_samples = []
        self.analyzer: BodeAnalyzer | None = None
        client.bode_sweep_ack_received.connect(self._on_ack)
        client.trace_data_received.connect(self._on_trace)
        client.nack_received.connect(self._on_nack)

    @property
    def active(self):
        return self.state in (self.STARTING, self.RUNNING, self.DRAINING)

    @staticmethod
    def channel_mask(test_mode: int) -> int:
        if int(test_mode) == 2:
            return (1 << 2) | (1 << 3)
        if int(test_mode) == 3:
            return (1 << 4) | (1 << 7)
        if int(test_mode) == 4:
            return (1 << 4) | (1 << 5)
        if int(test_mode) == 5:
            return (1 << 6) | (1 << 7)
        return (1 << 2) | (1 << 5)

    @staticmethod
    def channel_spec(test_mode: int) -> dict:
        mode = int(test_mode)
        if mode == 2:
            return {"input": "IQ_REF", "input_label": "目标电流",
                    "input_unit": "A", "output": "IQ", "output_label": "反馈电流",
                    "output_unit": "A", "transfer": "IQ/IQ_REF", "excitation_unit": "A"}
        if mode == 3:
            return {"input": "VEL_REF", "input_label": "目标速度",
                    "input_unit": "rad/s", "output": "POS", "output_label": "位置",
                    "output_unit": "rad", "transfer": "POS/VEL_REF", "excitation_unit": "rad/s"}
        if mode == 4:
            return {"input": "VEL_REF", "input_label": "目标速度",
                    "input_unit": "rad/s", "output": "VEL", "output_label": "反馈速度",
                    "output_unit": "rad/s", "transfer": "VEL/VEL_REF", "excitation_unit": "rad/s"}
        if mode == 5:
            return {"input": "POS_REF", "input_label": "目标位置",
                    "input_unit": "rad", "output": "POS", "output_label": "反馈位置",
                    "output_unit": "rad", "transfer": "POS/POS_REF", "excitation_unit": "rad"}
        return {"input": "IQ_REF", "input_label": "目标电流",
                "input_unit": "A", "output": "VEL", "output_label": "反馈速度",
                "output_unit": "rad/s", "transfer": "VEL/IQ_REF",
                "excitation_unit": "Nm" if mode == 0 else "A",
                "torque_conversion": "requires kt and gear_ratio" if mode == 0 else "not_applicable"}

    def start(self, test_mode: int, sweep_mode: int, point_cfg: int,
              f_start_hz: float, f_end_hz: float, amp: float,
              bias: float = 0.0) -> bool:
        if self.active or not self.client.is_open():
            return False
        self.config = {
            "test_mode": int(test_mode), "sweep_mode": int(sweep_mode),
            "point_cfg": int(point_cfg), "f_start_hz": float(f_start_hz),
            "f_end_hz": float(f_end_hz), "amp": float(amp),
            "bias": float(bias),
            "channels": self.channel_spec(test_mode),
        }
        self.session_id = 0
        self.point_count = 0
        self.duration_x100 = 0
        self.raw_samples.clear()
        self.received_packets = 0
        self.analyzer = BodeAnalyzer(self.channel_mask(test_mode))
        if not self.client.sweep_start(test_mode, sweep_mode, point_cfg,
                                       f_start_hz, f_end_hz, amp, bias):
            self._set_state(self.ERROR, {"message": "0x76发送失败"})
            return False
        self.started_at = time.monotonic()
        self._set_state(self.STARTING, {})
        return True

    def stop(self) -> bool:
        if not self.active:
            return False
        self._set_state(self.DRAINING, {})
        return self.client.sweep_stop()

    def _set_state(self, state: str, data: dict):
        self.state = state
        self.status_changed.emit(state, dict(data))

    def _on_ack(self, ack: dict):
        if not isinstance(ack, dict):
            return
        if not ack.get("running", False):
            if self.state == self.DRAINING:
                self._set_state(self.ABORTED, {})
                self.finished.emit(False, "用户停止")
            return
        self.session_id = int(ack.get("session_id", 0))
        self.point_count = int(ack.get("point_count", 0))
        self.duration_x100 = int(ack.get("duration_x100", 0))
        self._set_state(self.RUNNING, dict(ack))

    def _on_nack(self, cmd: int, err: int):
        if int(cmd) != int(JmCmd.TEST_SWEEP_FREQ) or not self.active:
            return
        err = int(err)
        message = f"启动被拒绝：{err_name_cn_short(err)}(0x{err:02X})"
        hint = _NACK_HINTS.get(err)
        if hint:
            message += f"——{hint}"
        self._set_state(self.ERROR, {"error": err})
        self.finished.emit(False, message)

    def _on_trace(self, packet: dict):
        if not self.active or not isinstance(packet, dict):
            return
        if int(packet.get("session_id", -1)) != self.session_id:
            return
        if not (int(packet.get("flags", 0)) & 0x08):
            return
        if (self.analyzer is None or
                int(packet.get("channel_mask", 0)) != self.analyzer.channel_mask):
            self._set_state(self.ERROR, {"message": "TRACE通道与测试模式不匹配"})
            self.finished.emit(False, "TRACE通道与测试模式不匹配")
            return
        self.received_packets += 1
        point = packet.get("sweep_point")
        for idx, row in enumerate(packet.get("samples", ())):
            self.raw_samples.append({
                "session_id": self.session_id,
                "sequence": int(packet.get("sequence", 0)),
                "sample_index": int(packet.get("first_sample_index", 0)) + idx,
                "actual_rate_hz": int(packet.get("actual_rate_hz", 0)),
                "sweep_point": point,
                "f_actual_mhz": packet.get("f_actual_mhz"),
                "input": row[0] if len(row) > 0 else None,
                "output": row[1] if len(row) > 1 else None,
            })
        if point is not None:
            self.raw_updated.emit(int(point))
        completed = self.analyzer.feed_packet(packet) if self.analyzer else []
        for result in completed:
            self.point_updated.emit(result)
        flags = int(packet.get("flags", 0))
        self.status_changed.emit(self.state, {
            "session_id": self.session_id,
            "point": point,
            "point_count": self.point_count,
            "received_packets": self.received_packets,
            "overflow_count": int(packet.get("overflow_count", 0)),
            "last": bool(flags & 0x04),
        })
        if flags & 0x04:
            if self.analyzer is not None:
                for result in self.analyzer.finish():
                    self.point_updated.emit(result)
            self._set_state(self.COMPLETE, {})
            self.finished.emit(True, "扫频完成")

    def export(self, directory: str | Path) -> tuple[Path, Path, Path] | None:
        if self.analyzer is None or not self.analyzer.points:
            return None
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stem = directory / f"bode_session_{self.session_id:04X}"
        bode_path = stem.with_name(stem.name + "_bode.csv")
        raw_path = stem.with_name(stem.name + "_raw.csv")
        meta_path = stem.with_name(stem.name + "_meta.json")
        self.analyzer.export_csv(bode_path)
        if self.raw_samples:
            with raw_path.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=list(self.raw_samples[0]))
                writer.writeheader()
                writer.writerows(self.raw_samples)
        self.analyzer.export_meta(meta_path, {
            "session_id": self.session_id,
            "point_count": self.point_count,
            "duration_x100": self.duration_x100,
            "config": self.config,
            "received_packets": self.received_packets,
            "lost_packets": self.analyzer.lost_packets,
            "overflow_count": self.analyzer.overflow_count,
        })
        return bode_path, raw_path, meta_path
