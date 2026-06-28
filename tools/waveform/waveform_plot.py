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
from PyQt6.QtGui import QAction, QColor, QPen, QFont, QIcon
from PyQt6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMenu, QPushButton,
    QSpinBox, QVBoxLayout, QWidget, QMessageBox, QSplitter, QFrame,
    QToolButton, QSizePolicy, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QTabWidget, QStyle,
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
# 自定义 ViewBox: 支持单轴缩放 (Simulink X-only / Y-only zoom)
# ============================================================================

class ScopeViewBox(pg.ViewBox):
    """Simulink 风格 ViewBox, 支持 X-only / Y-only 框选缩放

    模式:
    - 'xy' : 标准 XY 框选 (默认)
    - 'x'  : 仅 X 轴框选, Y 轴不变
    - 'y'  : 仅 Y 轴框选, X 轴不变
    - 'pan': 平移
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._scope_zoom_axis = 'xy'   # 'xy' / 'x' / 'y' / 'pan'

    def set_scope_zoom_axis(self, axis: str):
        self._scope_zoom_axis = axis
        if axis == 'pan':
            self.setMouseMode(pg.ViewBox.PanMode)
        else:
            self.setMouseMode(pg.ViewBox.RectMode)

    def mouseDragEvent(self, ev, axis=None):
        """重写拖动事件, 实现单轴缩放"""
        if self._scope_zoom_axis in ('x', 'y') and ev.button() == Qt.MouseButton.LeftButton:
            # 自定义单轴框选
            if ev.start():
                self._drag_start = ev.buttonDownPos()
                self._drag_rect = None
                from PyQt6.QtCore import QRectF
                self._rubber_band = pg.QtWidgets.QGraphicsRectItem()
                self._rubber_band.setPen(pg.mkPen('#FFFF00', width=1))
                self._rubber_band.setBrush(pg.mkBrush(255, 255, 0, 40))
                self._rubber_band.setZValue(1000)
                self.addItem(self._rubber_band, ignoreBounds=True)
            elif ev.finish():
                start = self._drag_start
                end = ev.pos()
                if self._scope_zoom_axis == 'x':
                    # 仅缩放 X
                    x0, x1 = sorted([start.x(), end.x()])
                    if abs(x1 - x0) > 1e-6:
                        self.setXRange(x0, x1, padding=0)
                elif self._scope_zoom_axis == 'y':
                    # 仅缩放 Y
                    y0, y1 = sorted([start.y(), end.y()])
                    if abs(y1 - y0) > 1e-6:
                        self.setYRange(y0, y1, padding=0)
                if self._rubber_band is not None:
                    self.removeItem(self._rubber_band)
                    self._rubber_band = None
            else:
                # 更新橡皮筋
                if self._rubber_band is not None:
                    start = self._drag_start
                    end = ev.pos()
                    from PyQt6.QtCore import QRectF
                    if self._scope_zoom_axis == 'x':
                        # 全高的竖条
                        vr = self.viewRect()
                        rect = QRectF(min(start.x(), end.x()), vr.y(),
                                      abs(end.x() - start.x()), vr.height())
                    else:
                        # 全宽的横条
                        vr = self.viewRect()
                        rect = QRectF(vr.x(), min(start.y(), end.y()),
                                      vr.width(), abs(end.y() - start.y()))
                    self._rubber_band.setRect(rect)
            ev.accept()
            return
        return super().mouseDragEvent(ev, axis)


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
    """单个波形显示面板 (Simulink 2026 Scope 风格)

    每个面板有:
    - 顶部工具栏: 缩放模式 / Pan / Auto-scale / Restore / 游标 / 统计 / 峰值 / 网格 / 图例 / 弹出
    - 主图形区: PlotItem + 右 Y 轴 ViewBox + 十字线 + 可拖动游标
    - 底部测量面板(可折叠): 数据游标表格 + 信号统计表格 + 峰值列表
    - 右键菜单: 通道设置 / 时间窗 / 缩放模式 / 弹出合并 / 清空

    所有面板共享同一个 DataPool (通过 WaveformPlot 注入)。
    """

    cursor_moved = pyqtSignal(float, dict)   # (x, {key: value})

    # 缩放模式常量
    ZOOM_AUTO = 'auto'        # 跟随 + 自适应
    ZOOM_X = 'zoom_x'         # 仅 X 轴框选缩放
    ZOOM_Y = 'zoom_y'         # 仅 Y 轴框选缩放
    ZOOM_XY = 'zoom_xy'       # XY 框选缩放
    ZOOM_PAN = 'pan'          # 平移

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

        # 缩放模式
        self._zoom_mode = self.ZOOM_AUTO
        # 缩放历史栈 (Simulink Zoom Back/Forward): 每项是 (x_range, y_range)
        self._zoom_history: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
        self._zoom_history_idx: int = -1
        self._suspend_history: bool = False

        # 弹出窗口引用 (None 表示在主容器内)
        self._float_window: Optional['FloatWaveformWindow'] = None
        # 容器回调 (弹出/合并时通知容器)
        self._on_popout: Optional[callable] = None
        self._on_dock_back: Optional[callable] = None

        # 数据游标 (Simulink Data Tips): 最多 2 个
        self._cursors: List[Optional[pg.InfiniteLine]] = [None, None]
        self._cursor_labels: List[Optional[pg.TextItem]] = [None, None]
        self._cursors_enabled = False
        # 游标吸附数据点 (Simulink Snap to Data)
        self._snap_to_data = False

        # 信号统计
        self._stats_enabled = False

        # 阶跃响应测量 (Simulink Step Response)
        self._step_enabled = False

        # 频率/周期测量 (Simulink Frequency)
        self._freq_enabled = False

        # 峰值查找
        self._peaks_enabled = False
        self._peak_items: List[pg.ScatterPlotItem] = []
        self._peak_labels: List[pg.TextItem] = []

        # 网格
        self._grid_visible = True

        # 图例
        self._legend_visible = True

        # Y 轴归一化 (Simulink Normalize)
        self._normalize_enabled = False

        self._setup_ui()

    # ---------------- UI ----------------
    def _setup_ui(self):
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        # ---------- 顶部工具栏 (Simulink Scope 风格) ----------
        self._toolbar = QFrame()
        self._toolbar.setStyleSheet(
            "QFrame { background: #3A3A3A; border-bottom: 1px solid #555; }"
            "QLabel { color: #FFD700; padding: 1px 6px; font-weight: bold; }"
            "QToolButton { background: #4A4A4A; border: none; padding: 2px 4px; "
            "  border-radius: 2px; color: #DDD; min-width: 24px; font-size: 13px; }"
            "QToolButton:hover { background: #5A5A5A; }"
            "QToolButton:pressed { background: #2A2A2A; }"
            "QToolButton:checked { background: #4A6A9A; color: #FFF; }"
        )
        self._toolbar.setMaximumHeight(26)
        tb = QHBoxLayout(self._toolbar)
        tb.setContentsMargins(2, 0, 2, 0)
        tb.setSpacing(1)

        # 标题 (左侧, 可伸缩)
        self._lbl_title = QLabel(self._title)
        self._lbl_title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(self._lbl_title)

        # 缩放模式按钮组 (互斥)
        self._btn_zoom_x = self._mk_btn("↔", "X 轴缩放", checkable=True, group='zoom')
        self._btn_zoom_y = self._mk_btn("↕", "Y 轴缩放", checkable=True, group='zoom')
        self._btn_zoom_xy = self._mk_btn("⤡", "XY 缩放", checkable=True, group='zoom')
        self._btn_pan = self._mk_btn("✋", "平移", checkable=True, group='zoom')
        self._btn_auto = self._mk_btn("A", "自动跟随", checkable=True, group='zoom')
        self._btn_auto.setChecked(True)

        # 视图操作
        self._btn_zoom_back = self._mk_btn("⏮", "缩放后退 (Zoom Back)")
        self._btn_zoom_fwd = self._mk_btn("⏭", "缩放前进 (Zoom Forward)")
        self._btn_restore = self._mk_btn("⤺", "恢复视图 (Restore View)")
        self._btn_grid = self._mk_btn("#", "网格", checkable=True)
        self._btn_grid.setChecked(True)
        self._btn_legend = self._mk_btn("L", "图例", checkable=True)
        self._btn_legend.setChecked(True)
        self._btn_normalize = self._mk_btn("≈", "Y 轴归一化 (Normalize)", checkable=True)

        # 测量工具
        self._btn_cursors = self._mk_btn("📊", "数据游标", checkable=True)
        self._btn_snap = self._mk_btn("⌖", "游标吸附 (Snap to Data)", checkable=True)
        self._btn_stats = self._mk_btn("📈", "信号统计", checkable=True)
        self._btn_step = self._mk_btn("⇗", "阶跃响应", checkable=True)
        self._btn_freq = self._mk_btn("ƒ", "频率/周期", checkable=True)
        self._btn_peaks = self._mk_btn("⌃", "峰值查找", checkable=True)

        # 弹出
        self._btn_popout = self._mk_btn("⬚", "弹出独立窗口")

        # 分隔符
        for btn in [self._btn_zoom_x, self._btn_zoom_y, self._btn_zoom_xy,
                    self._btn_pan, self._btn_auto]:
            tb.addWidget(btn)
        tb.addWidget(self._mk_sep())
        tb.addWidget(self._btn_zoom_back)
        tb.addWidget(self._btn_zoom_fwd)
        tb.addWidget(self._btn_restore)
        tb.addWidget(self._btn_grid)
        tb.addWidget(self._btn_legend)
        tb.addWidget(self._btn_normalize)
        tb.addWidget(self._mk_sep())
        tb.addWidget(self._btn_cursors)
        tb.addWidget(self._btn_snap)
        tb.addWidget(self._btn_stats)
        tb.addWidget(self._btn_step)
        tb.addWidget(self._btn_freq)
        tb.addWidget(self._btn_peaks)
        tb.addWidget(self._mk_sep())
        tb.addWidget(self._btn_popout)

        self._layout.addWidget(self._toolbar)

        # ---------- 主图形区 ----------
        self._gfx = pg.GraphicsLayoutWidget()
        # 主 ViewBox 用 ScopeViewBox (支持 X/Y 单轴缩放)
        self._plot = self._gfx.addPlot(viewBox=ScopeViewBox(enableMenu=False))
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._plot.setLabel('left', '数值')
        self._plot.setLabel('bottom', '时间', units='s')
        # 启用 XY 双轴鼠标交互 (Simulink 风格)
        self._plot.getViewBox().setMouseEnabled(x=True, y=True)
        self._plot.getViewBox().setMenuEnabled(False)
        # 默认框选放大模式
        self._plot.getViewBox().setMouseMode(pg.ViewBox.RectMode)

        # 右 Y 轴
        self._plot2 = ScopeViewBox(enableMenu=False)
        self._plot2.setMouseEnabled(x=True, y=True)
        self._plot2.setMouseMode(pg.ViewBox.RectMode)
        self._plot.scene().addItem(self._plot2)
        self._plot.getAxis('right').linkToView(self._plot2)
        self._plot2.setXLink(self._plot)

        def _update_views():
            self._plot2.setGeometry(self._plot.getViewBox().sceneBoundingRect())
            self._plot2.linkedViewChanged(self._plot.getViewBox(), self._plot2.XAxis)
        self._plot.getViewBox().sigResized.connect(_update_views)

        # 用户手动改变视图范围时: 切到 manual 模式 + 记录缩放历史
        def _on_range_changed():
            if self._suspend_history:
                return
            if self._zoom_mode == self.ZOOM_AUTO:
                self._set_zoom_mode(self.ZOOM_XY)
            self._push_zoom_history()
        self._plot.getViewBox().sigRangeChanged.connect(_on_range_changed)

        self._layout.addWidget(self._gfx, 1)

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

        # 图例
        self._legend = pg.LegendItem(offset=(0, 0))
        self._legend.setParentItem(self._plot.getViewBox())

        # ---------- 底部测量面板 (可折叠, 多 Tab) ----------
        self._measure_panel = QFrame()
        self._measure_panel.setStyleSheet(
            "QFrame { background: #2D2D2D; border-top: 1px solid #555; }"
            "QLabel { color: #CCC; padding: 1px 4px; }"
            "QTableWidget { background: #2D2D2D; color: #E0E0E0; "
            "  gridline-color: #555; border: none; }"
            "QHeaderView::section { background: #3A3A3A; color: #FFD700; "
            "  padding: 2px; border: 1px solid #555; font-weight: bold; }"
            "QTabWidget::pane { border: 1px solid #555; background: #2D2D2D; }"
            "QTabBar::tab { background: #3A3A3A; color: #DDD; padding: 2px 8px; "
            "  border: 1px solid #555; border-bottom: none; }"
            "QTabBar::tab:selected { background: #4A6A9A; color: white; }"
        )
        self._measure_panel.setMaximumHeight(200)
        self._measure_panel.setVisible(False)
        mp_layout = QVBoxLayout(self._measure_panel)
        mp_layout.setContentsMargins(2, 2, 2, 2)
        mp_layout.setSpacing(2)

        # Tab 容器
        self._measure_tabs = QTabWidget()
        mp_layout.addWidget(self._measure_tabs)

        # ---- Tab 1: 游标 ----
        self._cursor_table = QTableWidget(0, 4)
        self._cursor_table.setHorizontalHeaderLabels(["信号", "游标1", "游标2", "差值 Δ"])
        self._cursor_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._cursor_table.verticalHeader().setVisible(False)
        self._cursor_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._cursor_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._measure_tabs.addTab(self._cursor_table, "游标")

        # ---- Tab 2: 统计 ----
        self._stats_table = QTableWidget(0, 7)
        self._stats_table.setHorizontalHeaderLabels(
            ["信号", "Max", "Min", "Mean", "Std", "Pk-Pk", "RMS"]
        )
        self._stats_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._stats_table.verticalHeader().setVisible(False)
        self._stats_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._stats_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._measure_tabs.addTab(self._stats_table, "统计")

        # ---- Tab 3: 阶跃响应 ----
        self._step_table = QTableWidget(0, 7)
        self._step_table.setHorizontalHeaderLabels(
            ["信号", "上升时间", "稳定时间", "超调%", "欠调%", "峰值", "稳态值"]
        )
        self._step_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._step_table.verticalHeader().setVisible(False)
        self._step_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._step_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._measure_tabs.addTab(self._step_table, "阶跃响应")

        # ---- Tab 4: 频率/周期 ----
        self._freq_table = QTableWidget(0, 5)
        self._freq_table.setHorizontalHeaderLabels(
            ["信号", "周期", "频率", "过零数", "占空比%"]
        )
        self._freq_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._freq_table.verticalHeader().setVisible(False)
        self._freq_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._freq_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._measure_tabs.addTab(self._freq_table, "频率/周期")

        self._layout.addWidget(self._measure_panel)

        # ---------- 信号连接 ----------
        self._btn_zoom_x.clicked.connect(lambda: self._set_zoom_mode(self.ZOOM_X))
        self._btn_zoom_y.clicked.connect(lambda: self._set_zoom_mode(self.ZOOM_Y))
        self._btn_zoom_xy.clicked.connect(lambda: self._set_zoom_mode(self.ZOOM_XY))
        self._btn_pan.clicked.connect(lambda: self._set_zoom_mode(self.ZOOM_PAN))
        self._btn_auto.clicked.connect(lambda: self._set_zoom_mode(self.ZOOM_AUTO))
        self._btn_zoom_back.clicked.connect(self._zoom_back)
        self._btn_zoom_fwd.clicked.connect(self._zoom_forward)
        self._btn_restore.clicked.connect(self.restore_view)
        self._btn_grid.toggled.connect(self._toggle_grid)
        self._btn_legend.toggled.connect(self._toggle_legend)
        self._btn_normalize.toggled.connect(self._toggle_normalize)
        self._btn_cursors.toggled.connect(self._toggle_cursors)
        self._btn_snap.toggled.connect(self._toggle_snap)
        self._btn_stats.toggled.connect(self._toggle_stats)
        self._btn_step.toggled.connect(self._toggle_step)
        self._btn_freq.toggled.connect(self._toggle_freq)
        self._btn_peaks.toggled.connect(self._toggle_peaks)
        self._btn_popout.clicked.connect(self.popout)

        # 初始按钮状态
        self._update_zoom_history_buttons()

    def _mk_btn(self, text: str, tip: str, checkable: bool = False, group: str = None) -> QToolButton:
        """创建工具栏按钮"""
        btn = QToolButton(text=text)
        btn.setToolTip(tip)
        btn.setCheckable(checkable)
        btn.setFixedHeight(22)
        btn.setMinimumWidth(24)
        if group:
            btn.setProperty('group', group)
        return btn

    def _mk_sep(self) -> QFrame:
        """创建分隔符"""
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #555;")
        sep.setFixedWidth(1)
        return sep

    # ---------------- 缩放模式 ----------------
    def _set_zoom_mode(self, mode: str):
        """切换缩放模式 (Simulink 风格: X/Y/XY/Pan/Auto 互斥)"""
        if self._zoom_mode == mode:
            return
        self._zoom_mode = mode
        # 更新按钮状态
        self._btn_zoom_x.setChecked(mode == self.ZOOM_X)
        self._btn_zoom_y.setChecked(mode == self.ZOOM_Y)
        self._btn_zoom_xy.setChecked(mode == self.ZOOM_XY)
        self._btn_pan.setChecked(mode == self.ZOOM_PAN)
        self._btn_auto.setChecked(mode == self.ZOOM_AUTO)
        # 更新 ViewBox (用 ScopeViewBox 的 set_scope_zoom_axis)
        vb: ScopeViewBox = self._plot.getViewBox()
        vb2: ScopeViewBox = self._plot2
        if mode == self.ZOOM_AUTO:
            self._y_auto = True
            vb.enableAutoRange(pg.ViewBox.XYAxes, True)
            vb2.enableAutoRange(pg.ViewBox.XYAxes, True)
            vb.setMouseEnabled(x=True, y=True)
            vb.set_scope_zoom_axis('xy')
            vb2.set_scope_zoom_axis('xy')
        elif mode == self.ZOOM_PAN:
            self._y_auto = False
            vb.enableAutoRange(pg.ViewBox.XYAxes, False)
            vb2.enableAutoRange(pg.ViewBox.XYAxes, False)
            vb.setMouseEnabled(x=True, y=True)
            vb.set_scope_zoom_axis('pan')
            vb2.set_scope_zoom_axis('pan')
        elif mode == self.ZOOM_X:
            self._y_auto = False
            vb.enableAutoRange(pg.ViewBox.XYAxes, False)
            vb2.enableAutoRange(pg.ViewBox.XYAxes, False)
            vb.setMouseEnabled(x=True, y=False)
            vb.set_scope_zoom_axis('x')
            vb2.set_scope_zoom_axis('x')
        elif mode == self.ZOOM_Y:
            self._y_auto = False
            vb.enableAutoRange(pg.ViewBox.XYAxes, False)
            vb2.enableAutoRange(pg.ViewBox.XYAxes, False)
            vb.setMouseEnabled(x=False, y=True)
            vb.set_scope_zoom_axis('y')
            vb2.set_scope_zoom_axis('y')
        else:  # ZOOM_XY
            self._y_auto = False
            vb.enableAutoRange(pg.ViewBox.XYAxes, False)
            vb2.enableAutoRange(pg.ViewBox.XYAxes, False)
            vb.setMouseEnabled(x=True, y=True)
            vb.set_scope_zoom_axis('xy')
            vb2.set_scope_zoom_axis('xy')

    # ---------------- 缩放历史 (Zoom Back/Forward) ----------------
    def _push_zoom_history(self):
        """记录当前视图范围到历史栈"""
        if self._suspend_history:
            return
        vb = self._plot.getViewBox()
        xr = tuple(vb.viewRange()[0])
        yr = tuple(vb.viewRange()[1])
        # 截断 forward 部分
        self._zoom_history = self._zoom_history[:self._zoom_history_idx + 1]
        # 避免重复
        if self._zoom_history and self._zoom_history[-1] == (xr, yr):
            return
        self._zoom_history.append((xr, yr))
        # 限制历史长度 50
        if len(self._zoom_history) > 50:
            self._zoom_history.pop(0)
        self._zoom_history_idx = len(self._zoom_history) - 1
        self._update_zoom_history_buttons()

    def _update_zoom_history_buttons(self):
        self._btn_zoom_back.setEnabled(self._zoom_history_idx > 0)
        self._btn_zoom_fwd.setEnabled(self._zoom_history_idx < len(self._zoom_history) - 1)

    def _zoom_back(self):
        """后退到上一个缩放视图"""
        if self._zoom_history_idx <= 0:
            return
        self._zoom_history_idx -= 1
        self._apply_zoom_history()

    def _zoom_forward(self):
        """前进到下一个缩放视图"""
        if self._zoom_history_idx >= len(self._zoom_history) - 1:
            return
        self._zoom_history_idx += 1
        self._apply_zoom_history()

    def _apply_zoom_history(self):
        xr, yr = self._zoom_history[self._zoom_history_idx]
        self._suspend_history = True
        try:
            vb = self._plot.getViewBox()
            vb.blockSignals(True)
            vb.setRange(xRange=xr, yRange=yr, padding=0)
            vb.blockSignals(False)
        finally:
            self._suspend_history = False
        self._update_zoom_history_buttons()

    def restore_view(self):
        """恢复视图到自动跟随 (Simulink Restore view)"""
        self._zoom_history.clear()
        self._zoom_history_idx = -1
        self._update_zoom_history_buttons()
        self._set_zoom_mode(self.ZOOM_AUTO)
        self._last_y_calc = 0.0
        self.refresh(False)

    # ---------------- 网格 / 图例 / 归一化 ----------------
    def _toggle_grid(self, checked: bool):
        self._grid_visible = checked
        self._plot.showGrid(x=checked, y=checked, alpha=0.3)

    def _toggle_legend(self, checked: bool):
        self._legend_visible = checked
        self._legend.setVisible(checked)

    def _toggle_normalize(self, checked: bool):
        """Y 轴归一化: 把每个信号归一到 [0, 1] 或 [-1, 1]"""
        self._normalize_enabled = checked
        self.refresh(False)

    # ---------------- 游标吸附 ----------------
    def _toggle_snap(self, checked: bool):
        self._snap_to_data = checked
        if checked and self._cursors_enabled:
            # 立即吸附
            self._snap_cursors()
            self._update_cursor_table()

    def _snap_cursors(self):
        """把游标吸附到最近数据点 (用第一个可见信号的时间轴)"""
        if not self._cursors_enabled or not self._visible_keys:
            return
        # 选第一个可见信号作为参考时间轴
        ref_key = sorted(self._visible_keys)[0]
        buf = self._pool.buffers.get(ref_key)
        if buf is None or buf.count == 0:
            return
        ts, _ = buf.slice_last(self._window_s)
        if len(ts) == 0:
            return
        # x 在 [0, window_s], 对应 ts[-1]-window_s+x
        for i in range(2):
            c = self._cursors[i]
            if c is None:
                continue
            x = c.value()
            t_target = ts[-1] - (self._window_s - x)
            idx = int(np.searchsorted(ts, t_target))
            idx = max(0, min(len(ts) - 1, idx))
            x_new = ts[idx] - ts[-1] + self._window_s
            c.setPos(x_new)

    # ---------------- 数据游标 (Simulink Data Tips) ----------------
    def _toggle_cursors(self, checked: bool):
        self._cursors_enabled = checked
        if checked:
            # 创建 2 个游标, 默认放在窗口 30% 和 70% 位置
            vb = self._plot.getViewBox()
            x_range = vb.viewRange()[0]
            x1 = x_range[0] + (x_range[1] - x_range[0]) * 0.3
            x2 = x_range[0] + (x_range[1] - x_range[0]) * 0.7
            self._cursor_labels[0] = pg.TextItem(
                text="", color='#FFD700', anchor=(0.5, 0),
                fill=pg.mkBrush(0, 0, 0, 180))
            self._cursor_labels[1] = pg.TextItem(
                text="", color='#00FFFF', anchor=(0.5, 0),
                fill=pg.mkBrush(0, 0, 0, 180))
            self._cursors[0] = pg.InfiniteLine(
                angle=90, movable=True, pos=x1,
                pen=pg.mkPen('#FFD700', width=2))
            self._cursors[1] = pg.InfiniteLine(
                angle=90, movable=True, pos=x2,
                pen=pg.mkPen('#00FFFF', width=2))
            for i in range(2):
                self._plot.addItem(self._cursors[i], ignoreBounds=True)
                self._plot.addItem(self._cursor_labels[i], ignoreBounds=True)
                self._cursors[i].sigPositionChanged.connect(self._on_cursor_dragged)
            self._measure_panel.setVisible(True)
            # 默认切到游标 Tab
            self._measure_tabs.setCurrentIndex(0)
            if self._snap_to_data:
                self._snap_cursors()
            self._update_cursor_table()
        else:
            for i in range(2):
                if self._cursors[i] is not None:
                    self._plot.removeItem(self._cursors[i])
                    self._cursors[i] = None
                if self._cursor_labels[i] is not None:
                    self._plot.removeItem(self._cursor_labels[i])
                    self._cursor_labels[i] = None
            if not self._stats_enabled and not self._peaks_enabled \
                    and not self._step_enabled and not self._freq_enabled:
                self._measure_panel.setVisible(False)
            self._cursor_table.setRowCount(0)

    def _on_cursor_dragged(self):
        """游标拖动时: 吸附 + 更新表格"""
        if self._snap_to_data:
            self._snap_cursors()
        self._update_cursor_table()

    def _update_cursor_table(self):
        """更新游标表格 (Simulink Cursor Measurements)"""
        if not self._cursors_enabled or self._cursors[0] is None:
            return
        x1 = self._cursors[0].value()
        x2 = self._cursors[1].value() if self._cursors[1] is not None else x1
        dx = x2 - x1
        # 游标标签
        self._cursor_labels[0].setPos(x1, 0)
        self._cursor_labels[0].setText(f"t={x1:.3f}s")
        if self._cursors[1] is not None:
            self._cursor_labels[1].setPos(x2, 0)
            self._cursor_labels[1].setText(f"t={x2:.3f}s  Δt={dx:.3f}s")
        # 表格: 每个可见信号一行
        keys = sorted(self._visible_keys)
        self._cursor_table.setRowCount(len(keys))
        for row, key in enumerate(keys):
            meta = CHANNELS_BY_KEY.get(key)
            buf = self._pool.buffers.get(key)
            if meta is None or buf is None or buf.count == 0:
                continue
            ts, ys = buf.slice_last(self._window_s)
            if len(ts) == 0:
                continue
            t_target1 = ts[-1] - (self._window_s - x1)
            t_target2 = ts[-1] - (self._window_s - x2)
            def _interp(t):
                idx = np.searchsorted(ts, t)
                idx = max(0, min(len(ys) - 1, idx))
                return float(ys[idx])
            v1 = _interp(t_target1)
            v2 = _interp(t_target2)
            dv = v2 - v1
            # 频率提示 (如果 Δt > 0)
            freq_hint = ""
            if abs(dx) > 1e-6:
                freq_hint = f"  ({1.0/abs(dx):.2f}Hz)"
            self._cursor_table.setItem(row, 0, QTableWidgetItem(f"{meta.label} ({meta.unit})"))
            self._cursor_table.setItem(row, 1, QTableWidgetItem(f"{v1:.4f}"))
            self._cursor_table.setItem(row, 2, QTableWidgetItem(f"{v2:.4f}"))
            self._cursor_table.setItem(row, 3, QTableWidgetItem(f"{dv:+.4f}{freq_hint}"))

    # ---------------- 信号统计 (Simulink Statistics) ----------------
    def _toggle_stats(self, checked: bool):
        self._stats_enabled = checked
        if checked:
            self._measure_panel.setVisible(True)
            self._measure_tabs.setCurrentIndex(1)
            self._update_stats_table()
        else:
            if not self._cursors_enabled and not self._peaks_enabled \
                    and not self._step_enabled and not self._freq_enabled:
                self._measure_panel.setVisible(False)

    def _update_stats_table(self):
        """更新统计表格 (Max/Min/Mean/Std/Pk-Pk/RMS)"""
        if not self._stats_enabled:
            return
        keys = sorted(self._visible_keys)
        self._stats_table.setRowCount(len(keys))
        for row, key in enumerate(keys):
            meta = CHANNELS_BY_KEY.get(key)
            buf = self._pool.buffers.get(key)
            if meta is None or buf is None or buf.count == 0:
                continue
            _, ys = buf.slice_last(self._window_s)
            ys = ys[np.isfinite(ys)]
            if len(ys) == 0:
                continue
            ymax = float(np.max(ys))
            ymin = float(np.min(ys))
            ymean = float(np.mean(ys))
            ystd = float(np.std(ys))
            ypkpk = ymax - ymin
            yrms = float(np.sqrt(np.mean(ys ** 2)))
            self._stats_table.setItem(row, 0, QTableWidgetItem(f"{meta.label}"))
            self._stats_table.setItem(row, 1, QTableWidgetItem(f"{ymax:.4f}"))
            self._stats_table.setItem(row, 2, QTableWidgetItem(f"{ymin:.4f}"))
            self._stats_table.setItem(row, 3, QTableWidgetItem(f"{ymean:.4f}"))
            self._stats_table.setItem(row, 4, QTableWidgetItem(f"{ystd:.4f}"))
            self._stats_table.setItem(row, 5, QTableWidgetItem(f"{ypkpk:.4f}"))
            self._stats_table.setItem(row, 6, QTableWidgetItem(f"{yrms:.4f}"))

    # ---------------- 阶跃响应测量 (Simulink Step Response) ----------------
    def _toggle_step(self, checked: bool):
        self._step_enabled = checked
        if checked:
            self._measure_panel.setVisible(True)
            self._measure_tabs.setCurrentIndex(2)
            self._update_step_table()
        else:
            if not self._cursors_enabled and not self._peaks_enabled \
                    and not self._stats_enabled and not self._freq_enabled:
                self._measure_panel.setVisible(False)

    def _update_step_table(self):
        """计算阶跃响应指标: 上升时间/稳定时间/超调/欠调/峰值/稳态值"""
        if not self._step_enabled:
            return
        keys = sorted(self._visible_keys)
        self._step_table.setRowCount(len(keys))
        for row, key in enumerate(keys):
            meta = CHANNELS_BY_KEY.get(key)
            buf = self._pool.buffers.get(key)
            if meta is None or buf is None or buf.count < 10:
                continue
            ts, ys = buf.slice_last(self._window_s)
            if len(ys) < 10:
                continue
            ys = ys[np.isfinite(ys)]
            ts = ts[np.isfinite(ys)]
            if len(ys) < 10:
                continue
            y_start = float(ys[0])
            y_end = float(ys[-1])
            y_min = float(np.min(ys))
            y_max = float(np.max(ys))
            # 判断方向
            is_up = y_end > y_start
            y_init = y_start
            y_final = y_max if is_up else y_min
            y_peak = y_max if is_up else y_min
            y_settle = y_end
            span = abs(y_final - y_init)
            if span < 1e-9:
                # 没有阶跃, 全部 N/A
                for col in range(1, 7):
                    self._step_table.setItem(row, col, QTableWidgetItem("-"))
                self._step_table.setItem(row, 0, QTableWidgetItem(f"{meta.label}"))
                continue
            # 上升时间: 10% -> 90%
            t_10 = None
            t_90 = None
            for i, y in enumerate(ys):
                if is_up:
                    if t_10 is None and y >= y_init + 0.1 * span:
                        t_10 = ts[i]
                    if t_10 is not None and y >= y_init + 0.9 * span:
                        t_90 = ts[i]
                        break
                else:
                    if t_10 is None and y <= y_init - 0.1 * span:
                        t_10 = ts[i]
                    if t_10 is not None and y <= y_init - 0.9 * span:
                        t_90 = ts[i]
                        break
            t_rise = (t_90 - t_10) if (t_10 is not None and t_90 is not None) else None
            # 超调/欠调
            overshoot = ((y_peak - y_final) / span * 100.0) if is_up else ((y_final - y_min) / span * 100.0)
            undershoot = 0.0  # 简化: 不计算反向过冲
            # 稳定时间 (2% 容差)
            t_settle = None
            tol = 0.02 * span
            for i in range(len(ys) - 1, -1, -1):
                if abs(ys[i] - y_settle) > tol:
                    if i + 1 < len(ys):
                        t_settle = ts[i + 1] if i + 1 < len(ts) else ts[i]
                    break
            if t_settle is None:
                t_settle = ts[0]
            self._step_table.setItem(row, 0, QTableWidgetItem(f"{meta.label}"))
            self._step_table.setItem(row, 1, QTableWidgetItem(
                f"{t_rise:.4f}s" if t_rise is not None else "-"))
            self._step_table.setItem(row, 2, QTableWidgetItem(
                f"{(t_settle - ts[0]):.4f}s"))
            self._step_table.setItem(row, 3, QTableWidgetItem(
                f"{overshoot:.2f}"))
            self._step_table.setItem(row, 4, QTableWidgetItem(
                f"{undershoot:.2f}"))
            self._step_table.setItem(row, 5, QTableWidgetItem(f"{y_peak:.4f}"))
            self._step_table.setItem(row, 6, QTableWidgetItem(f"{y_settle:.4f}"))

    # ---------------- 频率/周期测量 (Simulink Frequency) ----------------
    def _toggle_freq(self, checked: bool):
        self._freq_enabled = checked
        if checked:
            self._measure_panel.setVisible(True)
            self._measure_tabs.setCurrentIndex(3)
            self._update_freq_table()
        else:
            if not self._cursors_enabled and not self._peaks_enabled \
                    and not self._stats_enabled and not self._step_enabled:
                self._measure_panel.setVisible(False)

    def _update_freq_table(self):
        """计算频率/周期: 通过均值过零检测"""
        if not self._freq_enabled:
            return
        keys = sorted(self._visible_keys)
        self._freq_table.setRowCount(len(keys))
        for row, key in enumerate(keys):
            meta = CHANNELS_BY_KEY.get(key)
            buf = self._pool.buffers.get(key)
            if meta is None or buf is None or buf.count < 10:
                continue
            ts, ys = buf.slice_last(self._window_s)
            if len(ys) < 10:
                continue
            mask = np.isfinite(ys)
            ys = ys[mask]
            ts = ts[mask]
            if len(ys) < 10:
                continue
            # 用均值作为过零基准
            baseline = float(np.mean(ys))
            y_rel = ys - baseline
            # 找过零点 (正向过零)
            sign = np.sign(y_rel)
            zero_crossings = []
            for i in range(1, len(sign)):
                if sign[i - 1] < 0 and sign[i] >= 0:
                    # 线性插值过零时间
                    t0, t1 = ts[i - 1], ts[i]
                    y0, y1 = y_rel[i - 1], y_rel[i]
                    if (y1 - y0) != 0:
                        t_zc = t0 + (0 - y0) / (y1 - y0) * (t1 - t0)
                        zero_crossings.append(t_zc)
            if len(zero_crossings) < 2:
                for col in range(1, 5):
                    self._freq_table.setItem(row, col, QTableWidgetItem("-"))
                self._freq_table.setItem(row, 0, QTableWidgetItem(f"{meta.label}"))
                continue
            # 周期 = 相邻过零间隔平均
            periods = np.diff(zero_crossings)
            period = float(np.mean(periods))
            freq = 1.0 / period if period > 0 else 0.0
            # 占空比: 高于均值的时间占比
            duty = float(np.mean(y_rel > 0) * 100.0)
            self._freq_table.setItem(row, 0, QTableWidgetItem(f"{meta.label}"))
            self._freq_table.setItem(row, 1, QTableWidgetItem(f"{period:.4f}s"))
            self._freq_table.setItem(row, 2, QTableWidgetItem(f"{freq:.3f}Hz"))
            self._freq_table.setItem(row, 3, QTableWidgetItem(f"{len(zero_crossings)}"))
            self._freq_table.setItem(row, 4, QTableWidgetItem(f"{duty:.1f}"))

    # ---------------- 峰值查找 (Simulink Find Peaks) ----------------
    def _toggle_peaks(self, checked: bool):
        self._peaks_enabled = checked
        if checked:
            self._measure_panel.setVisible(True)
        else:
            # 清除峰值标注
            for item in self._peak_items:
                self._plot.removeItem(item)
            for lbl in self._peak_labels:
                self._plot.removeItem(lbl)
            self._peak_items.clear()
            self._peak_labels.clear()
            if not self._cursors_enabled and not self._stats_enabled \
                    and not self._step_enabled and not self._freq_enabled:
                self._measure_panel.setVisible(False)

    def _update_peaks(self):
        """更新峰值标注 (每 500ms 一次)"""
        if not self._peaks_enabled:
            return
        # 清除旧标注
        for item in self._peak_items:
            self._plot.removeItem(item)
        for lbl in self._peak_labels:
            self._plot.removeItem(lbl)
        self._peak_items.clear()
        self._peak_labels.clear()
        # 为每个可见信号找峰值
        for key in self._visible_keys:
            meta = CHANNELS_BY_KEY.get(key)
            buf = self._pool.buffers.get(key)
            if meta is None or buf is None or buf.count < 10:
                continue
            ts, ys = buf.slice_last(self._window_s)
            if len(ys) < 10:
                continue
            color = self._color_overrides.get(key, meta.color)
            # 简单峰值检测: 局部最大值, 高于均值+1*std
            threshold = np.mean(ys) + np.std(ys)
            peaks = []
            for i in range(2, len(ys) - 2):
                if (ys[i] > ys[i-1] and ys[i] > ys[i+1] and
                    ys[i] > ys[i-2] and ys[i] > ys[i+2] and
                    ys[i] > threshold):
                    peaks.append(i)
            # 最多标注 5 个最高峰
            peaks = sorted(peaks, key=lambda i: ys[i], reverse=True)[:5]
            for i in peaks:
                x = ts[i] - ts[-1] + self._window_s
                y = float(ys[i])
                scatter = pg.ScatterPlotItem(
                    x=[x], y=[y], size=10, pen=pg.mkPen(color),
                    brush=pg.mkBrush(color), symbol='t')
                self._plot.addItem(scatter)
                self._peak_items.append(scatter)
                lbl = pg.TextItem(
                    text=f"{y:.3f}", color=color, anchor=(0.5, 1),
                    fill=pg.mkBrush(0, 0, 0, 180))
                lbl.setPos(x, y)
                self._plot.addItem(lbl, ignoreBounds=True)
                self._peak_labels.append(lbl)

    # ---------------- 弹出 / 合并 ----------------
    def popout(self):
        """弹出为独立窗口"""
        if self._float_window is not None:
            self._float_window.raise_()
            self._float_window.activateWindow()
            return
        if self._on_popout is not None:
            self._on_popout(self)
        else:
            win = FloatWaveformWindow(self, self._title)
            self._float_window = win
            win.show()

    def dock_back(self):
        """从独立窗口合回容器"""
        if self._float_window is not None:
            self._float_window.close()
            self._float_window = None

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
        self._lbl_title.setText("  |  ".join(parts))

    # ---------------- 刷新 ----------------
    def refresh(self, paused: bool):
        if paused:
            return
        # 更新曲线数据 (支持归一化)
        for key in self._visible_keys:
            pc = self._curves.get(key)
            if pc is None or pc.plot_item is None:
                continue
            buf = self._pool.buffers.get(key)
            if buf is None:
                continue
            ts, ys = buf.slice_last(self._window_s)
            if len(ts) > 0:
                x_data = ts - ts[-1] + self._window_s
                y_data = ys
                if self._normalize_enabled:
                    # 归一化: 每信号减去均值, 除以最大绝对值
                    finite_mask = np.isfinite(y_data)
                    if np.any(finite_mask):
                        y_finite = y_data[finite_mask]
                        y_mean = float(np.mean(y_finite))
                        y_max_abs = float(np.max(np.abs(y_finite - y_mean)))
                        if y_max_abs > 1e-9:
                            y_data = (y_data - y_mean) / y_max_abs
                pc.plot_item.setData(x_data, y_data, _callSync='off')

        # auto 模式: Y 轴自适应 (节流); 其他模式: 用户自由控制
        now = time.monotonic()
        if self._zoom_mode == self.ZOOM_AUTO and now - self._last_y_calc > 0.3:
            self._auto_y()
            self._last_y_calc = now

        # 周期更新测量面板 (游标/统计/阶跃/频率/峰值), 每 300ms
        if self._measure_panel.isVisible():
            if now - getattr(self, '_last_measure_update', 0) > 0.3:
                self._last_measure_update = now
                if self._cursors_enabled:
                    self._update_cursor_table()
                if self._stats_enabled:
                    self._update_stats_table()
                if self._step_enabled:
                    self._update_step_table()
                if self._freq_enabled:
                    self._update_freq_table()
                if self._peaks_enabled:
                    self._update_peaks()

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

        # Y 轴模式 -> 缩放模式 (Simulink 风格)
        y_menu = menu.addMenu("缩放模式")
        act_auto = QAction("自动跟随 (Auto)", y_menu, checkable=True)
        act_auto.setChecked(self._zoom_mode == self.ZOOM_AUTO)
        act_auto.triggered.connect(lambda: self._set_zoom_mode(self.ZOOM_AUTO))
        y_menu.addAction(act_auto)
        act_x = QAction("X 轴缩放", y_menu, checkable=True)
        act_x.setChecked(self._zoom_mode == self.ZOOM_X)
        act_x.triggered.connect(lambda: self._set_zoom_mode(self.ZOOM_X))
        y_menu.addAction(act_x)
        act_y = QAction("Y 轴缩放", y_menu, checkable=True)
        act_y.setChecked(self._zoom_mode == self.ZOOM_Y)
        act_y.triggered.connect(lambda: self._set_zoom_mode(self.ZOOM_Y))
        y_menu.addAction(act_y)
        act_xy = QAction("XY 缩放", y_menu, checkable=True)
        act_xy.setChecked(self._zoom_mode == self.ZOOM_XY)
        act_xy.triggered.connect(lambda: self._set_zoom_mode(self.ZOOM_XY))
        y_menu.addAction(act_xy)
        act_pan = QAction("平移 (Pan)", y_menu, checkable=True)
        act_pan.setChecked(self._zoom_mode == self.ZOOM_PAN)
        act_pan.triggered.connect(lambda: self._set_zoom_mode(self.ZOOM_PAN))
        y_menu.addAction(act_pan)
        y_menu.addSeparator()
        act_restore = QAction("恢复视图 (Restore)", y_menu)
        act_restore.triggered.connect(self.restore_view)
        y_menu.addAction(act_restore)

        menu.addSeparator()

        # 测量工具
        m_menu = menu.addMenu("测量工具")
        act_c = QAction("数据游标", m_menu, checkable=True)
        act_c.setChecked(self._cursors_enabled)
        act_c.triggered.connect(self._btn_cursors.toggle)
        m_menu.addAction(act_c)
        act_snap = QAction("游标吸附", m_menu, checkable=True)
        act_snap.setChecked(self._snap_to_data)
        act_snap.triggered.connect(self._btn_snap.toggle)
        m_menu.addAction(act_snap)
        act_s = QAction("信号统计", m_menu, checkable=True)
        act_s.setChecked(self._stats_enabled)
        act_s.triggered.connect(self._btn_stats.toggle)
        m_menu.addAction(act_s)
        act_step = QAction("阶跃响应", m_menu, checkable=True)
        act_step.setChecked(self._step_enabled)
        act_step.triggered.connect(self._btn_step.toggle)
        m_menu.addAction(act_step)
        act_freq = QAction("频率/周期", m_menu, checkable=True)
        act_freq.setChecked(self._freq_enabled)
        act_freq.triggered.connect(self._btn_freq.toggle)
        m_menu.addAction(act_freq)
        act_p = QAction("峰值查找", m_menu, checkable=True)
        act_p.setChecked(self._peaks_enabled)
        act_p.triggered.connect(self._btn_peaks.toggle)
        m_menu.addAction(act_p)

        # 显示选项
        d_menu = menu.addMenu("显示选项")
        act_g = QAction("网格", d_menu, checkable=True)
        act_g.setChecked(self._grid_visible)
        act_g.triggered.connect(self._btn_grid.toggle)
        d_menu.addAction(act_g)
        act_l = QAction("图例", d_menu, checkable=True)
        act_l.setChecked(self._legend_visible)
        act_l.triggered.connect(self._btn_legend.toggle)
        d_menu.addAction(act_l)
        act_n = QAction("Y 轴归一化", d_menu, checkable=True)
        act_n.setChecked(self._normalize_enabled)
        act_n.triggered.connect(self._btn_normalize.toggle)
        d_menu.addAction(act_n)

        # 缩放历史
        z_menu = menu.addMenu("缩放历史")
        act_zb = QAction("后退", z_menu)
        act_zb.setEnabled(self._zoom_history_idx > 0)
        act_zb.triggered.connect(self._zoom_back)
        z_menu.addAction(act_zb)
        act_zf = QAction("前进", z_menu)
        act_zf.setEnabled(self._zoom_history_idx < len(self._zoom_history) - 1)
        act_zf.triggered.connect(self._zoom_forward)
        z_menu.addAction(act_zf)

        menu.addSeparator()

        # 弹出/合并
        if self._float_window is None:
            act_pop = QAction("弹出为独立窗口", menu)
            act_pop.triggered.connect(self.popout)
            menu.addAction(act_pop)
        else:
            act_dock = QAction("合并回主窗口", menu)
            act_dock.triggered.connect(self.dock_back)
            menu.addAction(act_dock)

        menu.addSeparator()
        act_clear = QAction("清空显示", menu)
        act_clear.triggered.connect(self.clear_data)
        menu.addAction(act_clear)

        menu.exec(self.mapToGlobal(pos))

    def set_window(self, s: float):
        self._window_s = float(s)

    def set_y_mode(self, auto: bool):
        """兼容旧接口"""
        self._set_zoom_mode(self.ZOOM_AUTO if auto else self.ZOOM_XY)

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
# 弹出独立窗口 (Simulink 独立 Scope 窗口)
# ============================================================================

class FloatWaveformWindow(QWidget):
    """独立波形窗口: 把 WaveformPanel 放到独立的顶级窗口中

    特性:
    - 可拖动、缩放、最大化
    - 关闭时自动把面板合回容器 (通过 closeEvent)
    - 面板工具栏的弹出按钮变为"合并"按钮
    """

    def __init__(self, panel: 'WaveformPanel', title: str = "", parent=None):
        super().__init__(parent)
        self._panel = panel
        self._title = title or f"波形 - 窗口 {panel.panel_id}"

        self.setWindowTitle(self._title)
        self.resize(900, 560)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowMinMaxButtonsHint |
                            Qt.WindowType.WindowCloseButtonHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 把面板 reparent 到这里
        panel.setParent(self)
        layout.addWidget(panel)

        # 面板弹出按钮变为"合并"按钮
        panel._btn_popout.setText("⬓")
        panel._btn_popout.setToolTip("合并回主窗口")
        try:
            panel._btn_popout.clicked.disconnect()
        except Exception:
            pass
        panel._btn_popout.clicked.connect(panel.dock_back)

    def closeEvent(self, event):
        """关闭时通知容器把面板合回"""
        if self._panel._on_dock_back is not None:
            self._panel._on_dock_back(self._panel)
        self._panel._float_window = None
        # 恢复弹出按钮图标
        self._panel._btn_popout.setText("⬚")
        self._panel._btn_popout.setToolTip("弹出独立窗口")
        try:
            self._panel._btn_popout.clicked.disconnect()
        except Exception:
            pass
        self._panel._btn_popout.clicked.connect(self._panel.popout)
        super().closeEvent(event)


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
            # 设置弹出/合并回调
            p._on_popout = self._on_panel_popout
            p._on_dock_back = self._on_panel_dock_back

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    # ---------------- 弹出 / 合并 ----------------
    def _on_panel_popout(self, panel: 'WaveformPanel'):
        """面板弹出为独立窗口"""
        win = FloatWaveformWindow(panel, panel._title, parent=None)
        panel._float_window = win
        win.show()

    def _on_panel_dock_back(self, panel: 'WaveformPanel'):
        """面板从独立窗口合回容器"""
        panel.setParent(self._panel_container)
        # 找到现有的 splitter, 把面板加回去
        has_splitter = False
        for i in range(self._panel_layout.count()):
            w = self._panel_layout.itemAt(i).widget()
            if isinstance(w, QSplitter):
                has_splitter = True
                w.addWidget(panel)
                break
        if not has_splitter:
            self._panel_layout.addWidget(panel)
        if panel not in self._panels:
            self._panels.append(panel)
        panel.show()

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
