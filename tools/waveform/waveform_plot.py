"""
波形显示核心: 多窗口分屏 + 每窗口独立通道配置 + 实时图例数值。

设计要点:
- 共享数据: 所有 WaveformPanel 共用一套 RingBuffer (按 key 索引), 避免重复存储
- 独立视图: 每个 WaveformPanel 有自己的 PlotItem + 左右 Y 轴 + 可见通道集 + 图例
- 分屏布局: WaveformPlot 容器支持 1/2/3/4 窗口, 网格自适应
- 高性能: 预分配 numpy 环形缓冲 + 30fps 刷新 + 脏标记跳过
- 右键菜单: 每个窗口独立配置通道/颜色/Y轴/时间窗/Y轴模式

通道元数据来自 protocol_reference.JmTlmBit.ITEMS。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QPointF
from PyQt6.QtGui import QAction, QColor, QPen, QFont
from PyQt6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMenu, QPushButton,
    QSpinBox, QVBoxLayout, QWidget, QMessageBox, QSplitter,
)

import pyqtgraph as pg

from .protocol_reference import JmTlmBit, FeedbackData


# ============================================================================
# 通道元数据
# ============================================================================

@dataclass
class ChannelMeta:
    """单条曲线的全局元数据 (所有面板共享)"""
    key: str
    label: str
    unit: str
    group: str
    color: str = '#00FF00'


DEFAULT_COLORS = [
    '#00FF00', '#FF8000', '#00BFFF', '#FF00FF', '#FFFF00',
    '#FF4040', '#40FF40', '#4080FF', '#FF80FF', '#80FFFF',
    '#FFC080', '#80FF80', '#8080FF', '#FF8080', '#80FFFF',
    '#C0C0C0', '#FF40C0', '#40FFC0', '#C040FF', '#C0FF40',
    '#40C0FF', '#FFFF80', '#808080',
]

_UNITS = {
    'pos': 'rad', 'vel': 'rad/s', 'torque': 'Nm',
    'id': 'A', 'iq': 'A', 'ia': 'A', 'ib': 'A', 'ic': 'A',
    'vbus': 'V', 'ibus': 'A', 'power': 'W',
    'temp_fet': '°C', 'temp_motor': '°C',
    'multiturn': '-', 'single': 'rad',
    'fault_mask': '-', 'warn_mask': '-',
    'top_fsm': '-', 'run_state': '-', 'ctrl_mode': '-', 'enable': '-', 'motion_state': '-',
}
_LABELS = {
    'pos': '位置', 'vel': '速度', 'torque': '力矩',
    'id': 'Id', 'iq': 'Iq', 'ia': 'Ia', 'ib': 'Ib', 'ic': 'Ic',
    'vbus': '母线电压', 'ibus': '母线电流', 'power': '功率',
    'temp_fet': 'FET温度', 'temp_motor': '电机温度',
    'multiturn': '多圈计数', 'single': '单圈位置',
    'fault_mask': '故障码', 'warn_mask': '警告码',
    'top_fsm': '顶层状态', 'run_state': '运行子状态',
    'ctrl_mode': '控制模式', 'enable': '使能', 'motion_state': '运动状态',
}

CHANNEL_METAS: List[ChannelMeta] = []
_ci = 0
for mask_val, group_label, field_names, _ in JmTlmBit.ITEMS:
    if mask_val == JmTlmBit.DEBUG:
        continue
    for fn in field_names:
        CHANNEL_METAS.append(ChannelMeta(
            key=fn, label=_LABELS.get(fn, fn), unit=_UNITS.get(fn, '-'),
            group=group_label, color=DEFAULT_COLORS[_ci % len(DEFAULT_COLORS)],
        ))
        _ci += 1

CHANNELS_BY_KEY: Dict[str, ChannelMeta] = {c.key: c for c in CHANNEL_METAS}


# ============================================================================
# 环形缓冲
# ============================================================================

class RingBuffer:
    """预分配 numpy 环形缓冲区, O(1) append, 惰性构建时间顺序视图"""

    def __init__(self, maxlen: int = 60000):
        n = int(maxlen)
        self._ts = np.full(n, np.nan, dtype=np.float64)
        self._ys = np.full(n, np.nan, dtype=np.float64)
        self._head = 0
        self._count = 0
        self._maxlen = n
        self._cache_ts: Optional[np.ndarray] = None
        self._cache_ys: Optional[np.ndarray] = None

    def append(self, t: float, y: float):
        self._ts[self._head] = float(t)
        self._ys[self._head] = float(y)
        self._head = (self._head + 1) % self._maxlen
        if self._count < self._maxlen:
            self._count += 1
        self._cache_ts = None
        self._cache_ys = None

    def clear(self):
        self._ts.fill(np.nan)
        self._ys.fill(np.nan)
        self._head = 0
        self._count = 0
        self._cache_ts = None
        self._cache_ys = None

    def _build_cache(self):
        c = self._count
        if c == 0:
            self._cache_ts = np.empty(0, dtype=np.float64)
            self._cache_ys = np.empty(0, dtype=np.float64)
            return
        if c < self._maxlen:
            self._cache_ts = self._ts[:c]
            self._cache_ys = self._ys[:c]
        else:
            idx = np.arange(self._maxlen, dtype=np.intp)
            idx = (self._head + idx) % self._maxlen
            self._cache_ts = self._ts[idx]
            self._cache_ys = self._ys[idx]

    @property
    def ts_view(self) -> np.ndarray:
        if self._cache_ts is None:
            self._build_cache()
        return self._cache_ts

    @property
    def ys_view(self) -> np.ndarray:
        if self._cache_ys is None:
            self._build_cache()
        return self._cache_ys

    @property
    def count(self) -> int:
        return self._count

    def slice_last(self, window_s: float) -> Tuple[np.ndarray, np.ndarray]:
        if self._count == 0:
            return np.empty(0), np.empty(0)
        ts = self.ts_view
        ys = self.ys_view
        t_end = ts[-1]
        t_start = t_end - window_s
        mask = ts >= t_start
        return ts[mask], ys[mask]

    def latest_value(self) -> Optional[float]:
        if self._count == 0:
            return None
        idx = (self._head - 1) % self._maxlen
        return float(self._ys[idx])


# ============================================================================
# 共享数据池: 所有面板共用
# ============================================================================

class DataPool:
    """所有面板共享的环形缓冲池"""

    def __init__(self):
        self.buffers: Dict[str, RingBuffer] = {
            m.key: RingBuffer(60000) for m in CHANNEL_METAS
        }
        self.t0 = time.monotonic()

    def append_feedback(self, fb: FeedbackData, filled: Tuple[str, ...]) -> float:
        """返回当前时间戳 (相对 t0)"""
        t = time.monotonic() - self.t0
        for key in filled:
            buf = self.buffers.get(key)
            if buf is None:
                continue
            v = getattr(fb, key, None)
            if v is not None:
                buf.append(t, float(v))
        return t

    def clear(self):
        for b in self.buffers.values():
            b.clear()

    def total_samples(self) -> int:
        return sum(b.count for b in self.buffers.values())


# ============================================================================
# 单个图表面板
# ============================================================================

@dataclass
class PanelCurve:
    """面板内一条曲线的本地状态 (PlotDataItem 由面板创建)"""
    key: str
    plot_item: Optional[pg.PlotDataItem] = None
    y_axis: int = 0          # 0=左, 1=右
    legend_label: Optional[pg.LabelItem] = None


class WaveformPanel(QWidget):
    """单个波形显示面板

    每个面板有:
    - 自己的 PlotItem + 右 Y 轴 ViewBox
    - 自己的可见通道集 (从全局 CHANNEL_METAS 选)
    - 自己的十字线 + 图例 (实时数值)
    - 自己的右键菜单

    所有面板共享同一个 DataPool (通过 WaveformPlot 注入)。
    """

    cursor_moved = pyqtSignal(float, dict)   # (x, {key: value})

    def __init__(self, panel_id: int, pool: DataPool, parent=None):
        super().__init__(parent)
        self.panel_id = panel_id
        self._pool = pool

        # 面板独立状态
        self._visible_keys: Set[str] = set()
        self._curves: Dict[str, PanelCurve] = {}
        self._y_auto = True
        self._window_s = 10.0
        self._last_y_calc = 0.0
        self._title = f"窗口 {panel_id}"

        # 颜色覆盖 (key -> color), 默认用全局色
        self._color_overrides: Dict[str, str] = {}

        self._setup_ui()

    # ---------------- UI ----------------
    def _setup_ui(self):
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        # 标题栏 (显示窗口名 + 通道数)
        self._title_bar = QLabel(self._title)
        self._title_bar.setStyleSheet(
            "background: #3A3A3A; color: #FFD700; padding: 2px 6px; "
            "font-weight: bold; border-bottom: 1px solid #555;"
        )
        self._title_bar.setMaximumHeight(20)
        self._layout.addWidget(self._title_bar)

        # 图形
        self._gfx = pg.GraphicsLayoutWidget()
        self._plot = self._gfx.addPlot()
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._plot.setLabel('left', '数值')
        self._plot.setLabel('bottom', '时间', units='s')
        self._plot.getViewBox().setMouseEnabled(x=False, y=True)
        self._plot.getViewBox().setMenuEnabled(False)

        # 右 Y 轴
        self._plot2 = pg.ViewBox()
        self._plot2.setMouseEnabled(x=False, y=True)
        self._plot.scene().addItem(self._plot2)
        self._plot.getAxis('right').linkToView(self._plot2)
        self._plot2.setXLink(self._plot)

        def _update_views():
            self._plot2.setGeometry(self._plot.getViewBox().sceneBoundingRect())
            self._plot2.linkedViewChanged(self._plot.getViewBox(), self._plot2.XAxis)
        self._plot.getViewBox().sigResized.connect(_update_views)

        self._layout.addWidget(self._gfx)

        # 十字线
        self._vline = pg.InfiniteLine(angle=90, movable=False,
                                       pen=pg.mkPen('#FFFF00', width=1, style=Qt.PenStyle.DashLine))
        self._hline = pg.InfiniteLine(angle=0, movable=False,
                                       pen=pg.mkPen('#FFFF00', width=1, style=Qt.PenStyle.DashLine))
        self._plot.addItem(self._vline, ignoreBounds=True)
        self._plot.addItem(self._hline, ignoreBounds=True)
        self._plot.scene().sigMouseMoved.connect(self._on_mouse_moved)

        # 右键菜单
        self._gfx.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._gfx.customContextMenuRequested.connect(self._show_context_menu)

        # 图例 (放在右上角, 显示通道名+颜色+实时数值)
        self._legend = pg.LegendItem(offset=(0, 0))
        self._legend.setParentItem(self._plot.getViewBox())

    # ---------------- 通道管理 ----------------
    def set_channel_visible(self, key: str, visible: bool):
        if visible:
            self._visible_keys.add(key)
            self._ensure_curve(key)
        else:
            self._visible_keys.discard(key)
            self._remove_curve(key)
        self._update_title()
        self._rebuild_legend()

    def _ensure_curve(self, key: str):
        if key in self._curves:
            return
        meta = CHANNELS_BY_KEY.get(key)
        if meta is None:
            return
        color = self._color_overrides.get(key, meta.color)
        pen = pg.mkPen(color=color, width=1)
        plot_item = self._plot.plot(pen=pen, name=meta.label)
        pc = PanelCurve(key=key, plot_item=plot_item, y_axis=0)
        self._curves[key] = pc

    def _remove_curve(self, key: str):
        pc = self._curves.pop(key, None)
        if pc is None:
            return
        if pc.plot_item is not None:
            if pc.plot_item in self._plot.listDataItems():
                self._plot.removeItem(pc.plot_item)
            if pc.plot_item in self._plot2.listDataItems():
                self._plot2.removeItem(pc.plot_item)

    def set_channel_color(self, key: str, color: str):
        self._color_overrides[key] = color
        pc = self._curves.get(key)
        if pc is not None and pc.plot_item is not None:
            pc.plot_item.setPen(pg.mkPen(color=color, width=1))
        self._rebuild_legend()

    def set_channel_y_axis(self, key: str, axis: int):
        pc = self._curves.get(key)
        if pc is None or pc.plot_item is None:
            return
        pc.y_axis = axis
        # 重新挂载
        if axis == 0:
            if pc.plot_item in self._plot2.listDataItems():
                self._plot2.removeItem(pc.plot_item)
            if pc.plot_item not in self._plot.listDataItems():
                self._plot.addItem(pc.plot_item)
        else:
            if pc.plot_item in self._plot.listDataItems():
                self._plot.removeItem(pc.plot_item)
            if pc.plot_item not in self._plot2.listDataItems():
                self._plot2.addItem(pc.plot_item)

    def clear_data(self):
        # 数据是共享的, 不在这里清; 只清显示
        for pc in self._curves.values():
            if pc.plot_item is not None:
                pc.plot_item.setData([], [])

    # ---------------- 图例 (实时数值) ----------------
    def _rebuild_legend(self):
        """重建图例, 每条曲线一项"""
        self._legend.clear()
        for key in self._visible_keys:
            pc = self._curves.get(key)
            if pc is None or pc.plot_item is None:
                continue
            meta = CHANNELS_BY_KEY.get(key)
            if meta is None:
                continue
            self._legend.addItem(pc.plot_item, f"{meta.label}")

    def _update_legend_values(self):
        """更新图例文字, 加上实时数值"""
        # pyqtgraph LegendItem 不支持动态改文字, 用 TextItem 替代方案
        # 这里采用: 重新 addItem 带数值
        # 性能考虑: 每 200ms 更新一次
        pass  # 实时数值用顶部 title_bar 显示更高效

    def _update_title(self):
        """标题栏显示窗口名 + 可见通道 + 各自最新值"""
        parts = [f"窗口 {self.panel_id}"]
        if self._visible_keys:
            val_parts = []
            for key in self._visible_keys:
                meta = CHANNELS_BY_KEY.get(key)
                if meta is None:
                    continue
                v = self._pool.buffers[key].latest_value()
                if v is not None:
                    val_parts.append(f"{meta.label}={v:.3f}{meta.unit}")
            if val_parts:
                parts.append("  |  ".join(val_parts))
        self._title_bar.setText("  |  ".join(parts))

    # ---------------- 刷新 ----------------
    def refresh(self, paused: bool):
        if paused:
            return
        # 更新曲线
        for key in self._visible_keys:
            pc = self._curves.get(key)
            if pc is None or pc.plot_item is None:
                continue
            buf = self._pool.buffers.get(key)
            if buf is None:
                continue
            ts, ys = buf.slice_last(self._window_s)
            if len(ts) > 0:
                # 时间轴对齐: x = 0 表示 window_s 前, x = window_s 表示现在
                pc.plot_item.setData(ts - ts[-1] + self._window_s, ys, _callSync='off')

        # Y 轴自适应 (节流)
        now = time.monotonic()
        if self._y_auto and now - self._last_y_calc > 0.3:
            self._auto_y()
            self._last_y_calc = now

        # 更新标题栏 (最新数值)
        self._update_title()

    def _auto_y(self):
        ys_left = []
        ys_right = []
        for key in self._visible_keys:
            pc = self._curves.get(key)
            if pc is None:
                continue
            buf = self._pool.buffers.get(key)
            if buf is None:
                continue
            _, ys = buf.slice_last(self._window_s)
            if len(ys) > 0:
                if pc.y_axis == 0:
                    ys_left.append(ys)
                else:
                    ys_right.append(ys)
        if ys_left:
            all_y = np.concatenate(ys_left)
            all_y = all_y[np.isfinite(all_y)]
            if len(all_y) > 0:
                lo, hi = float(np.min(all_y)), float(np.max(all_y))
                if hi - lo < 1e-6:
                    hi = lo + 1e-3
                margin = (hi - lo) * 0.1
                self._plot.setYRange(lo - margin, hi + margin, padding=0)
        if ys_right:
            all_y = np.concatenate(ys_right)
            all_y = all_y[np.isfinite(all_y)]
            if len(all_y) > 0:
                lo, hi = float(np.min(all_y)), float(np.max(all_y))
                if hi - lo < 1e-6:
                    hi = lo + 1e-3
                margin = (hi - lo) * 0.1
                self._plot2.setYRange(lo - margin, hi + margin, padding=0)

    # ---------------- 鼠标 ----------------
    def _on_mouse_moved(self, pos):
        if not self._plot.sceneBoundingRect().contains(pos):
            return
        mouse_point = self._plot.vb.mapSceneToView(pos)
        x = mouse_point.x()
        y = mouse_point.y()
        self._vline.setPos(x)
        self._hline.setPos(y)
        # 找每个通道最近值
        values = {}
        for key in self._visible_keys:
            buf = self._pool.buffers.get(key)
            if buf is None or buf.count == 0:
                continue
            ts, ys = buf.slice_last(self._window_s)
            if len(ts) == 0:
                continue
            t_target = ts[-1] - (self._window_s - x)
            idx = np.searchsorted(ts, t_target)
            idx = max(0, min(len(ys) - 1, idx))
            values[key] = float(ys[idx])
        self.cursor_moved.emit(x, values)

    # ---------------- 右键菜单 ----------------
    def _show_context_menu(self, pos):
        menu = QMenu(self)
        menu.setWindowTitle(f"窗口 {self.panel_id} 设置")

        # 通道设置
        ch_menu = menu.addMenu("通道设置")
        for meta in CHANNEL_METAS:
            sub = ch_menu.addMenu(f"{meta.label} ({meta.key})")
            # 显隐
            act_vis = QAction("显示", sub, checkable=True)
            act_vis.setChecked(meta.key in self._visible_keys)
            act_vis.triggered.connect(
                lambda _, k=meta.key: self.set_channel_visible(k, k not in self._visible_keys)
            )
            sub.addAction(act_vis)
            # 颜色
            act_color = QAction("颜色...", sub)
            act_color.triggered.connect(lambda _, k=meta.key: self._pick_color(k))
            sub.addAction(act_color)
            # Y 轴
            pc = self._curves.get(meta.key)
            act_axis = QAction("使用右Y轴", sub, checkable=True)
            act_axis.setChecked(pc is not None and pc.y_axis == 1)
            act_axis.triggered.connect(
                lambda _, k=meta.key, c=pc:
                    self.set_channel_y_axis(k, 1 if (c is None or c.y_axis == 0) else 0)
            )
            sub.addAction(act_axis)

        # 时间窗
        win_menu = menu.addMenu("时间窗")
        for s in [1, 2, 5, 10, 30, 60, 120]:
            act = QAction(f"{s}s", win_menu, checkable=True)
            act.setChecked(self._window_s == s)
            act.triggered.connect(lambda _, s=s: self.set_window(s))
            win_menu.addAction(act)

        # Y 轴模式
        y_menu = menu.addMenu("Y轴模式")
        act_auto = QAction("自适应", y_menu, checkable=True)
        act_auto.setChecked(self._y_auto)
        act_auto.triggered.connect(lambda: self.set_y_mode(True))
        y_menu.addAction(act_auto)
        act_manual = QAction("手动", y_menu, checkable=True)
        act_manual.setChecked(not self._y_auto)
        act_manual.triggered.connect(lambda: self.set_y_mode(False))
        y_menu.addAction(act_manual)

        menu.addSeparator()
        act_clear = QAction("清空显示", menu)
        act_clear.triggered.connect(self.clear_data)
        menu.addAction(act_clear)

        menu.exec(self.mapToGlobal(pos))

    def set_window(self, s: float):
        self._window_s = float(s)

    def set_y_mode(self, auto: bool):
        self._y_auto = auto
        self._plot.getViewBox().setMouseEnabled(y=not auto)

    def _pick_color(self, key: str):
        meta = CHANNELS_BY_KEY.get(key)
        if meta is None:
            return
        cur = self._color_overrides.get(key, meta.color)
        color = QColorDialog.getColor(QColor(cur), self, f"选择 {meta.label} 颜色")
        if color.isValid():
            self.set_channel_color(key, color.name())

    # ---------------- 公共 ----------------
    def get_snapshot(self, key: str) -> Tuple[np.ndarray, np.ndarray]:
        buf = self._pool.buffers.get(key)
        if buf is None:
            return np.empty(0), np.empty(0)
        return buf.slice_last(self._window_s)

    def get_visible_keys(self) -> List[str]:
        return sorted(self._visible_keys)


# ============================================================================
# 波形显示容器 (多窗口分屏)
# ============================================================================

class WaveformPlot(QWidget):
    """多窗口分屏波形显示

    特性:
    - 1/2/3/4 窗口分屏 (1=单图, 2=上下, 3=左1右2 或 上1下2, 4=2x2)
    - 每窗口独立配置通道/颜色/Y轴/时间窗
    - 全局: 暂停/跟随/清空/导出/触发/FFT
    - 共享 DataPool, 避免重复存储
    - 鼠标悬停读数 (从活动面板冒泡到状态栏)
    """

    cursor_moved = pyqtSignal(float, dict)
    capture_armed = pyqtSignal(dict)

    LAYOUTS = {
        1: (1, 1),    # 1x1
        2: (1, 2),    # 1行2列 (左右)
        3: (2, 2),    # 2x2 但只用 3 格 (左上+左下+右)
        4: (2, 2),    # 2x2
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = DataPool()
        self._panels: List[WaveformPanel] = []
        self._panel_count = 1
        self._paused = False
        self._follow = True
        self._window_s = 10.0

        # 触发
        self._trigger_enabled = False
        self._trigger_key: Optional[str] = None
        self._trigger_edge = 'rising'
        self._trigger_level = 0.0
        self._trigger_hold_s = 0.5
        self._trigger_armed = False
        self._trigger_last_fire = 0.0

        self._setup_ui()
        self._setup_timer()
        self._setup_connections()
        self._rebuild_layout(1)

        # 默认在第一个窗口显示 位置/速度/力矩
        if self._panels:
            for k in ['pos', 'vel', 'torque']:
                self._panels[0].set_channel_visible(k, True)

    # ---------------- UI ----------------
    def _setup_ui(self):
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)

        # 顶部工具栏
        self._toolbar = QHBoxLayout()
        self._btn_pause = QPushButton("⏸ 暂停")
        self._btn_pause.setCheckable(True)
        self._btn_follow = QPushButton("▶ 跟随")
        self._btn_follow.setCheckable(True)
        self._btn_follow.setChecked(True)
        self._btn_clear = QPushButton("✕ 清空")
        self._btn_export = QPushButton("↓ 导出")

        self._lbl_windows = QLabel("窗口数:")
        self._cmb_windows = QComboBox()
        for n in [1, 2, 3, 4]:
            self._cmb_windows.addItem(f"{n}", n)
        self._cmb_windows.setCurrentIndex(0)

        self._lbl_window = QLabel("时间窗:")
        self._cmb_window = QComboBox()
        for s in [1, 2, 5, 10, 30, 60, 120]:
            self._cmb_window.addItem(f"{s}s", s)
        self._cmb_window.setCurrentIndex(3)

        self._lbl_status = QLabel("  0 fps  |  0 samples")
        self._lbl_status.setStyleSheet("color: gray;")

        for w in [self._btn_pause, self._btn_follow, self._btn_clear, self._btn_export,
                  self._lbl_windows, self._cmb_windows,
                  self._lbl_window, self._cmb_window,
                  self._lbl_status]:
            self._toolbar.addWidget(w)
        self._toolbar.addStretch()
        self._layout.addLayout(self._toolbar)

        # 面板容器 (用 QSplitter 嵌套实现分屏)
        self._panel_container = QWidget()
        self._panel_layout = QVBoxLayout(self._panel_container)
        self._panel_layout.setContentsMargins(0, 0, 0, 0)
        self._layout.addWidget(self._panel_container, 1)

    def _setup_timer(self):
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(33)

    def _setup_connections(self):
        self._btn_pause.toggled.connect(self._on_pause_toggled)
        self._btn_follow.toggled.connect(self._on_follow_toggled)
        self._btn_clear.clicked.connect(self.clear)
        self._btn_export.clicked.connect(self._on_export)
        self._cmb_windows.currentIndexChanged.connect(self._on_windows_changed)
        self._cmb_window.currentIndexChanged.connect(self._on_window_changed)

    # ---------------- 分屏布局 ----------------
    def _rebuild_layout(self, n: int):
        """重建 N 窗口分屏"""
        # 清掉旧面板
        for p in self._panels:
            p.setParent(None)
            p.deleteLater()
        self._panels.clear()

        # 清掉旧 layout
        while self._panel_layout.count():
            item = self._panel_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

        if n == 1:
            p = WaveformPanel(1, self._pool, self)
            self._panels.append(p)
            self._panel_layout.addWidget(p)
        elif n == 2:
            # 左右分屏
            splitter = QSplitter(Qt.Orientation.Horizontal)
            for i in range(2):
                p = WaveformPanel(i + 1, self._pool, self)
                self._panels.append(p)
                splitter.addWidget(p)
            splitter.setSizes([500, 500])
            self._panel_layout.addWidget(splitter)
        elif n == 3:
            # 左侧大 + 右侧上下两小
            h_split = QSplitter(Qt.Orientation.Horizontal)
            p1 = WaveformPanel(1, self._pool, self)
            self._panels.append(p1)
            h_split.addWidget(p1)
            v_split = QSplitter(Qt.Orientation.Vertical)
            p2 = WaveformPanel(2, self._pool, self)
            p3 = WaveformPanel(3, self._pool, self)
            self._panels.append(p2)
            self._panels.append(p3)
            v_split.addWidget(p2)
            v_split.addWidget(p3)
            v_split.setSizes([300, 300])
            h_split.addWidget(v_split)
            h_split.setSizes([600, 400])
            self._panel_layout.addWidget(h_split)
        elif n == 4:
            # 2x2
            v_split = QSplitter(Qt.Orientation.Vertical)
            top = QSplitter(Qt.Orientation.Horizontal)
            bot = QSplitter(Qt.Orientation.Horizontal)
            for i in range(2):
                p = WaveformPanel(i + 1, self._pool, self)
                self._panels.append(p)
                top.addWidget(p)
            top.setSizes([500, 500])
            for i in range(2):
                p = WaveformPanel(i + 3, self._pool, self)
                self._panels.append(p)
                bot.addWidget(p)
            bot.setSizes([500, 500])
            v_split.addWidget(top)
            v_split.addWidget(bot)
            v_split.setSizes([300, 300])
            self._panel_layout.addWidget(v_split)

        # 绑定每个面板的 cursor 信号 -> 冒泡
        for p in self._panels:
            p.cursor_moved.connect(self._on_panel_cursor)
            # 同步全局时间窗
            p.set_window(self._window_s)

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    # ---------------- 公共 API ----------------
    def append_feedback(self, fb: FeedbackData, filled: Tuple[str, ...]):
        if self._paused:
            return
        t = self._pool.append_feedback(fb, filled)
        # 触发检测
        if self._trigger_enabled and self._trigger_key:
            self._check_trigger(t)

    def clear(self):
        self._pool.clear()
        for p in self._panels:
            p.clear_data()

    def set_trigger(self, enabled: bool, key: Optional[str] = None,
                    edge: str = 'rising', level: float = 0.0, hold_s: float = 0.5):
        self._trigger_enabled = enabled
        self._trigger_key = key
        self._trigger_edge = edge
        self._trigger_level = level
        self._trigger_hold_s = hold_s
        self._trigger_armed = enabled
        self._trigger_last_fire = 0.0

    def get_snapshot(self, key: str, window_s: Optional[float] = None) -> Tuple[np.ndarray, np.ndarray]:
        buf = self._pool.buffers.get(key)
        if buf is None:
            return np.empty(0), np.empty(0)
        w = window_s if window_s is not None else self._window_s
        return buf.slice_last(w)

    def get_active_panel(self) -> Optional[WaveformPanel]:
        """返回第一个面板 (用于 FFT/触发的默认通道选择)"""
        return self._panels[0] if self._panels else None

    # ---------------- 刷新 ----------------
    def _refresh(self):
        # FPS
        self._frame_count = getattr(self, '_frame_count', 0) + 1
        now = time.monotonic()
        if now - getattr(self, '_last_fps_t', now) >= 1.0:
            fps = self._frame_count / (now - self._last_fps_t)
            self._frame_count = 0
            self._last_fps_t = now
            self._lbl_status.setText(
                f"  {fps:.0f} fps  |  {self._pool.total_samples()} samples"
            )

        for p in self._panels:
            p.refresh(self._paused)

    # ---------------- 触发 ----------------
    def _check_trigger(self, t: float):
        if self._trigger_key is None or not self._trigger_armed:
            return
        if t - self._trigger_last_fire < self._trigger_hold_s:
            return
        buf = self._pool.buffers.get(self._trigger_key)
        if buf is None or buf.count < 2:
            return
        ts, ys = buf.slice_last(self._window_s)
        if len(ys) < 2:
            return
        n_scan = min(100, len(ys))
        y_last = ys[-n_scan:]
        for i in range(1, n_scan):
            prev, curr = y_last[i - 1], y_last[i]
            if self._trigger_edge == 'rising' and prev < self._trigger_level <= curr:
                self._fire_trigger(t)
                return
            elif self._trigger_edge == 'falling' and prev > self._trigger_level >= curr:
                self._fire_trigger(t)
                return

    def _fire_trigger(self, t: float):
        self._trigger_last_fire = t
        self._trigger_armed = False
        snapshots = {}
        for key, buf in self._pool.buffers.items():
            ts, ys = buf.slice_last(self._window_s)
            if len(ts) > 0:
                snapshots[key] = (ts.copy(), ys.copy())
        self.capture_armed.emit(snapshots)
        QTimer.singleShot(int(self._trigger_hold_s * 1000), self._rearm_trigger)

    def _rearm_trigger(self):
        self._trigger_armed = True

    # ---------------- 事件 ----------------
    def _on_panel_cursor(self, x: float, values: dict):
        self.cursor_moved.emit(x, values)

    def _on_pause_toggled(self, checked: bool):
        self._paused = checked
        self._btn_pause.setText("▶ 继续" if checked else "⏸ 暂停")

    def _on_follow_toggled(self, checked: bool):
        self._follow = checked

    def _on_window_changed(self, idx: int):
        self._window_s = float(self._cmb_window.itemData(idx))
        for p in self._panels:
            p.set_window(self._window_s)

    def _on_windows_changed(self, idx: int):
        n = self._cmb_windows.itemData(idx)
        self._rebuild_layout(n)

    def _on_export(self):
        from .exporter import export_dialog
        # 构造兼容 exporter 接口的 curves dict
        class _ProxyCurve:
            def __init__(self, key, buf, meta):
                self.meta = meta
                self.buf = buf
                self.plot_item = None
        curves = {}
        for key in self._pool.buffers:
            meta = CHANNELS_BY_KEY.get(key)
            if meta:
                curves[key] = _ProxyCurve(key, self._pool.buffers[key], meta)
        export_dialog(self, curves)

    # ---------------- 右键菜单 (容器级, 弹出在非面板区域) ----------------
    def contextMenuEvent(self, event):
        menu = QMenu(self)

        # 窗口数
        win_menu = menu.addMenu("窗口数")
        for n in [1, 2, 3, 4]:
            act = QAction(f"{n} 窗口", win_menu, checkable=True)
            act.setChecked(self._panel_count == n)
            act.triggered.connect(lambda _, n=n: self._set_window_count(n))
            win_menu.addAction(act)

        # 时间窗
        tw_menu = menu.addMenu("时间窗")
        for s in [1, 2, 5, 10, 30, 60, 120]:
            act = QAction(f"{s}s", tw_menu, checkable=True)
            act.setChecked(self._window_s == s)
            act.triggered.connect(lambda _, s=s: self._set_window(s))
            tw_menu.addAction(act)

        menu.addSeparator()

        # 触发设置
        act_trig = QAction("触发设置...", menu)
        act_trig.triggered.connect(self._show_trigger_dialog)
        menu.addAction(act_trig)

        # FFT
        act_fft = QAction("FFT 分析...", menu)
        act_fft.triggered.connect(self._show_fft_dialog)
        menu.addAction(act_fft)

        menu.addSeparator()

        act_clear = QAction("清空", menu)
        act_clear.triggered.connect(self.clear)
        menu.addAction(act_clear)
        act_pause = QAction("暂停" if not self._paused else "继续", menu)
        act_pause.triggered.connect(lambda: self._btn_pause.toggle())
        menu.addAction(act_pause)

        menu.addSeparator()
        act_export = QAction("导出 CSV/PNG...", menu)
        act_export.triggered.connect(self._on_export)
        menu.addAction(act_export)

        menu.exec(event.globalPos())

    def _set_window_count(self, n: int):
        self._panel_count = n
        idx = self._cmb_windows.findData(n)
        if idx >= 0:
            self._cmb_windows.setCurrentIndex(idx)
        self._rebuild_layout(n)

    def _set_window(self, s: int):
        idx = self._cmb_window.findData(s)
        if idx >= 0:
            self._cmb_window.setCurrentIndex(idx)

    def _show_trigger_dialog(self):
        from .trigger import TriggerDialog
        dlg = TriggerDialog(self, [m.key for m in CHANNEL_METAS])
        dlg.set_config(
            enabled=self._trigger_enabled,
            key=self._trigger_key,
            edge=self._trigger_edge,
            level=self._trigger_level,
            hold_s=self._trigger_hold_s,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            cfg = dlg.get_config()
            self.set_trigger(**cfg)

    def _show_fft_dialog(self):
        from .fft_panel import FftDialog
        dlg = FftDialog(self, [m.key for m in CHANNEL_METAS])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            key = dlg.get_channel()
            if key:
                ts, ys = self.get_snapshot(key)
                meta = CHANNELS_BY_KEY.get(key)
                if meta:
                    dlg.show_fft(ts, ys, meta.label, meta.unit, meta.color)
