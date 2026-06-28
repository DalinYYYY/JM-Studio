"""实时曲线面板: pyqtgraph 多曲线, 消费遥测字典。

提供 4 组联动子图: 位置/速度/力矩/电流(Iq), 共享时间轴。
环形 numpy 缓冲, 批量喂入, 性能优先。
"""

from dataclasses import dataclass
import numpy as np

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)
from ui.theme import theme

try:
    import pyqtgraph as pg
except Exception:
    pg = None

if pg is not None:
    try:
        pg.setConfigOptions(antialias=False, useOpenGL=True)
    except Exception:
        pg.setConfigOptions(antialias=False)


@dataclass
class _Buf:
    """预分配 numpy 环形缓冲, float32 节省带宽。"""
    n: int

    def __post_init__(self):
        self._t = np.full(self.n, np.nan, dtype=np.float32)
        self._y = np.full(self.n, np.nan, dtype=np.float32)
        self._head = 0
        self._cnt = 0

    def push(self, t: float, y: float):
        i = self._head
        self._t[i] = t
        self._y[i] = y
        self._head = (i + 1) % self.n
        self._cnt = min(self._cnt + 1, self.n)

    def arrays(self):
        """返回按时间顺序的有效视图(零拷贝)。"""
        if self._cnt < self.n:
            return self._t[:self._cnt], self._y[:self._cnt]
        # 环形回绕: 拼接两段(此处返回拷贝, 仅在 setData 时发生)
        i = self._head
        return np.concatenate([self._t[i:], self._t[:i]]), \
               np.concatenate([self._y[i:], self._y[:i]])


# (曲线key, 显示名, 颜色theme键, 子图索引)
# 实测值用实色, 命令目标用浅色/虚线对比
_CURVES = [
    # 子图0: 位置 (实测 + 命令目标 + 生效目标)
    ("pos", "位置实测 (rad)", "accent", 0),
    ("cmd_target_pos", "命令位置 (rad)", "muted", 0),
    ("target_pos", "生效位置 (rad)", "value", 0),
    # 子图1: 速度 (实测 + 命令目标)
    ("vel", "速度实测 (rad/s)", "value", 1),
    ("cmd_target_vel", "命令速度 (rad/s)", "muted", 1),
    ("target_vel", "生效速度 (rad/s)", "accent", 1),
    # 子图2: 力矩/电流 (实测 + 命令目标)
    ("torque", "力矩实测 (Nm)", "value_hot", 2),
    ("iq", "Iq 实测 (A)", "danger", 2),
    ("iq_ref", "Iq 参考 (A)", "muted", 2),
    ("cmd_target_torque", "命令力矩 (Nm)", "warn", 2),
    # 子图3: 跟随误差 + 母线电压
    ("follow_err", "跟随误差 (rad)", "accent", 3),
    ("vbus", "母线电压 (V)", "value", 3),
]

_SUBTITLES = ["位置 (实测/命令/生效)", "速度 (实测/命令/生效)",
              "力矩/电流 (实测/命令)", "跟随误差/电压"]


class TwinPlotPanel(QWidget):
    """数字孪生实时曲线面板。"""

    def __init__(self, maxlen: int = 4000, parent=None):
        super().__init__(parent)
        self._maxlen = maxlen
        self._paused = False
        self._t0 = None
        self._engine = None   # 由 set_engine 注入, 用于读 cmd_target_*/target_*
        self._dirty = set()   # 曲线级 dirty 标记, 仅重绘有新数据的曲线
        # 状态/模式切换标记线 (Round 7)
        # _events: list[(t_rel, label, color_key)]
        self._events = []
        self._last_sys_state = None
        self._last_mode = None
        self._last_fault = 0
        self._event_dirty = False   # 有新事件需要重绘标记线
        self._build()

    def set_engine(self, engine):
        """注入引擎句柄, 用于补齐 telemetry 缺失的命令/生效目标字段。"""
        self._engine = engine

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(2, 2, 2, 2)

        if pg is None:
            v.addWidget(QLabel("未安装 pyqtgraph, 无法显示曲线"))
            return

        # 工具栏
        bar = QHBoxLayout()
        bar.addWidget(QLabel("时间窗:"))
        self._spin_win = QSpinBox()
        self._spin_win.setRange(1, 60)
        self._spin_win.setValue(10)
        self._spin_win.setSuffix(" s")
        bar.addWidget(self._spin_win)
        self._btn_pause = QPushButton("暂停")
        self._btn_pause.setCheckable(True)
        self._btn_pause.toggled.connect(self._on_pause)
        bar.addWidget(self._btn_pause)
        bar.addStretch()
        self._lbl_fps = QLabel("")
        self._lbl_fps.setStyleSheet(f"color: {theme.hex('muted')};")
        bar.addWidget(self._lbl_fps)
        v.addLayout(bar)

        # 绘图区: 垂直 4 个子图, 共享 X 轴
        self._plot = pg.GraphicsLayoutWidget()
        self._plot.setBackground(theme.hex("panel_bg"))
        self._curves = {}
        self._bufs = {}
        self._plots = []
        for i, title in enumerate(_SUBTITLES):
            p = self._plot.addPlot(row=i, col=0, title=title)
            p.showGrid(x=True, y=True, alpha=0.25)
            p.getAxis("left").setPen(theme.hex("muted"))
            p.getAxis("bottom").setPen(theme.hex("muted"))
            p.getAxis("left").setTextPen(theme.hex("text"))
            p.getAxis("bottom").setTextPen(theme.hex("text"))
            if i < len(_SUBTITLES) - 1:
                p.setXLink(self._plots[0]) if self._plots else None
            self._plots.append(p)

        for key, name, color_key, sub in _CURVES:
            buf = _Buf(self._maxlen)
            self._bufs[key] = buf
            pen = pg.mkPen(color=theme.hex(color_key), width=1.4)
            cv = self._plots[sub].plot(pen=pen, name=name)
            self._curves[key] = cv

        # 子图 X 轴联动
        for p in self._plots[1:]:
            p.setXLink(self._plots[0])

        # 状态/模式/故障标记线容器 (Round 7)
        # 每个子图独立一组 InfiniteLine, 共享时间轴
        self._event_lines = []   # list[list[pg.InfiniteLine]]
        for _ in self._plots:
            self._event_lines.append([])

        v.addWidget(self._plot, 1)

        # 刷新定时器(30Hz 显示刷新, 与数据喂入解耦)
        self._refresh = QTimer(self)
        self._refresh.timeout.connect(self._redraw)
        self._refresh.start(33)

        # FPS 统计
        self._fps_cnt = 0
        self._fps_t = QTimer(self)
        self._fps_t.timeout.connect(self._update_fps)
        self._fps_t.start(1000)

    def feed(self, t: dict):
        """喂入一帧遥测。"""
        if self._paused or pg is None:
            return
        # 合并: telemetry 优先, 缺失字段从 engine.fsm 补
        merged = t
        if self._engine is not None:
            fsm = getattr(self._engine, "fsm", None)
            if fsm is not None:
                extra = {}
                for attr in ("target_pos", "target_vel", "target_torque",
                             "target_iq", "target_id", "target_voltage", "target_duty",
                             "cmd_target_pos", "cmd_target_vel", "cmd_target_torque",
                             "cmd_target_iq", "cmd_target_id"):
                    if hasattr(fsm, attr):
                        extra[attr] = getattr(fsm, attr)
                merged = {**extra, **t}
        ts = float(merged.get("t_sim", 0.0))
        if self._t0 is None:
            self._t0 = ts
        ts -= self._t0
        # 检测状态/模式/故障变化 → 记录事件标记线 (Round 7)
        self._detect_events(merged, ts)
        # 只对有新数据的曲线置 dirty, _redraw 仅重绘 dirty 曲线
        for key, buf in self._bufs.items():
            val = merged.get(key)
            if val is None:
                continue
            try:
                buf.push(ts, float(val))
                self._dirty.add(key)   # 标记该曲线有新数据
            except (ValueError, TypeError):
                pass
        self._fps_cnt += 1

    # ---------- 事件标记线 (Round 7) ----------
    def _detect_events(self, merged: dict, ts: float):
        """检测 sys_state / control_mode / fault_flags 变化并记录事件。"""
        sys_state = merged.get("sys_state_name") or merged.get("sys_state")
        mode = merged.get("control_mode_name") or merged.get("control_mode")
        fault = int(merged.get("fault_flags", 0) or 0)

        # 系统状态变化
        if sys_state != self._last_sys_state:
            if self._last_sys_state is not None:
                self._events.append((ts, f"→{sys_state}", "warn"))
                self._event_dirty = True
            self._last_sys_state = sys_state

        # 控制模式变化
        if mode != self._last_mode:
            if self._last_mode is not None:
                self._events.append((ts, f"→{mode}", "accent"))
                self._event_dirty = True
            self._last_mode = mode

        # 故障上升沿 (新增的故障位)
        new_fault = fault & (~self._last_fault)
        if new_fault:
            self._events.append((ts, f"FAULT 0x{new_fault:04X}", "danger"))
            self._event_dirty = True
        self._last_fault = fault

        # 限制事件数量, 避免无限增长
        if len(self._events) > 200:
            self._events = self._events[-200:]

    def _redraw_event_lines(self):
        """重绘所有子图的事件标记线 (dirty 时调用)。"""
        # 清除旧线
        for lines in self._event_lines:
            for ln in lines:
                try:
                    ln.scene().removeItem(ln)
                except Exception:
                    pass
            lines.clear()
        # 仅绘制当前时间窗内的事件
        win = float(self._spin_win.value())
        if not self._events:
            return
        if len(self._bufs["pos"]._t) == 0:
            return
        tmax = float(self._bufs["pos"]._t.max())
        if np.isnan(tmax):
            return
        tmin = tmax - win
        for t_ev, label, color_key in self._events:
            if t_ev < tmin:
                continue
            for sub_idx, p in enumerate(self._plots):
                ln = pg.InfiniteLine(
                    pos=t_ev, angle=90,
                    pen=pg.mkPen(color=theme.hex(color_key), width=1.0,
                                 style=Qt.PenStyle.DashLine),
                    label=label, labelOpts={
                        "color": theme.hex(color_key),
                        "position": 0.97,
                        "rotateAxis": (1, 0),
                    },
                )
                p.addItem(ln, ignoreBounds=True)
                self._event_lines[sub_idx].append(ln)

    def _redraw(self):
        if pg is None or self._paused:
            return
        win = float(self._spin_win.value())
        # 只重绘有新数据的曲线 (dirty 标记优化)
        if self._dirty:
            for key in list(self._dirty):
                cv = self._curves.get(key)
                buf = self._bufs.get(key)
                if cv is not None and buf is not None:
                    ts, ys = buf.arrays()
                    cv.setData(ts, ys, _callSync="off")
            self._dirty.clear()
        # 事件标记线重绘 (Round 7): 新事件到达或时间窗滚动
        if self._event_dirty:
            self._redraw_event_lines()
            self._event_dirty = False
        # X 轴滚动窗口 (始终更新, 保证时间轴滚动)
        if len(self._bufs["pos"]._t) > 0:
            tmax = self._bufs["pos"]._t.max()
            if not np.isnan(tmax):
                self._plots[0].setXRange(max(0, tmax - win), tmax, padding=0.02)

    def _on_pause(self, on: bool):
        self._paused = on
        self._btn_pause.setText("继续" if on else "暂停")

    def _update_fps(self):
        self._lbl_fps.setText(f"{self._fps_cnt} fps")
        self._fps_cnt = 0

    def apply_theme(self):
        if pg is None:
            return
        self._plot.setBackground(theme.hex("panel_bg"))
        for key, name, color_key, sub in _CURVES:
            cv = self._curves.get(key)
            if cv is not None:
                cv.setPen(pg.mkPen(color=theme.hex(color_key), width=1.4))
        for p in self._plots:
            p.getAxis("left").setPen(theme.hex("muted"))
            p.getAxis("bottom").setPen(theme.hex("muted"))
            p.getAxis("left").setTextPen(theme.hex("text"))
            p.getAxis("bottom").setTextPen(theme.hex("text"))
        self._lbl_fps.setStyleSheet(f"color: {theme.hex('muted')};")
