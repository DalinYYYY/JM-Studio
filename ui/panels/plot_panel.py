"""实时曲线面板.

使用 pyqtgraph 提供 4 组联动波形:
位置、速度、电流、机械角度。
支持时间窗、暂停、跟随、Y 轴自适应、曲线显隐与鼠标悬停读数。

性能优化要点:
- _SeriesBuffer 使用预分配 numpy 环形缓冲区, 零分配追加, 惰性时间顺序缓存
- _refresh() 通过脏标记跳过无新数据时的重绘
- _latest_t 增量更新, 避免每帧扫描全部 buffer
- Y 轴自适应每 ~300ms 计算一次(10 帧 @ 30ms)
- interpolate() 复用缓存数组, 避免鼠标悬停时的重复转换
"""

from dataclasses import dataclass
import math

import numpy as np

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QGroupBox, QHBoxLayout, QLabel, QMenu, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from ui.theme import theme

try:
    import pyqtgraph as pg
except Exception:  # pragma: no cover - fallback for environments without pyqtgraph
    pg = None


if pg is not None:
    pg.setConfigOptions(antialias=True)


@dataclass
class _SeriesBuffer:
    """预分配 numpy 环形缓冲区, append O(1) 无内存分配, 惰性时间顺序缓存."""

    maxlen: int

    def __post_init__(self):
        n = int(self.maxlen)
        self._ts = np.full(n, np.nan, dtype=np.float64)
        self._ys = np.full(n, np.nan, dtype=np.float64)
        self._head = 0          # 下一次写入位置
        self._count = 0         # 当前有效元素数
        self._maxlen = n
        self._cache_ts = None   # 惰性: 首次读取时构建时间顺序视图
        self._cache_ys = None
        self.dirty = False

    def append(self, t: float, value: float):
        """O(1) 写入, 失效缓存, 置脏标记."""
        self._ts[self._head] = float(t)
        self._ys[self._head] = float(value)
        self._head = (self._head + 1) % self._maxlen
        if self._count < self._maxlen:
            self._count += 1
        self._cache_ts = None
        self._cache_ys = None
        self.dirty = True

    def clear(self):
        """清空缓冲并保持预分配数组."""
        self._ts.fill(np.nan)
        self._ys.fill(np.nan)
        self._head = 0
        self._count = 0
        self._cache_ts = None
        self._cache_ys = None
        self.dirty = True

    def _build_cache(self):
        """惰性构建时间顺序数组(满缓冲时做一次 fancy-index copy)."""
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
    def _ts_view(self):
        """返回时间顺序数组(惰性构建, 无额外拷贝)."""
        if self._cache_ts is None:
            self._build_cache()
        return self._cache_ts

    @property
    def _ys_view(self):
        if self._cache_ys is None:
            self._build_cache()
        return self._cache_ys

    def arrays(self, t_min: float = None):
        """返回 (ts, ys) 拷贝, 安全用于 pyqtgraph setData."""
        ts = self._ts_view
        ys = self._ys_view
        if len(ts) == 0:
            return ts, ys
        if t_min is None:
            return ts.copy(), ys.copy()
        i = int(np.searchsorted(ts, t_min, side='left'))
        if i >= len(ts):
            return np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64)
        return ts[i:].copy(), ys[i:].copy()

    def last_time(self):
        """返回最新时间戳, O(1)."""
        if self._count == 0:
            return None
        return float(self._ts[(self._head - 1) % self._maxlen])

    def interpolate(self, t: float):
        """在时间 t 处线性插值, 复用缓存数组."""
        ts = self._ts_view
        ys = self._ys_view
        if len(ts) == 0:
            return None
        if len(ts) == 1:
            return float(ys[0])
        if t <= ts[0]:
            return float(ys[0])
        if t >= ts[-1]:
            return float(ys[-1])
        i = int(np.searchsorted(ts, t, side='left'))
        x0, x1 = ts[i - 1], ts[i]
        y0, y1 = ys[i - 1], ys[i]
        if x1 == x0:
            return float(y0)
        k = (t - x0) / (x1 - x0)
        return float(y0 + (y1 - y0) * k)


class PlotPanel(QGroupBox):
    """实时曲线面板。"""

    MAX_POINTS = 6000
    _AUTO_RANGE_EVERY = 10       # Y 轴自适应节流: 每 N 帧执行一次

    def __init__(self, parent=None):
        super().__init__("", parent)
        self._t0 = None
        self._paused = False
        self._window_seconds = 20
        self._syncing_x = False
        self._plot_states = {}
        self._buffers = {
            'pos': _SeriesBuffer(self.MAX_POINTS),
            'vel': _SeriesBuffer(self.MAX_POINTS),
            'id': _SeriesBuffer(self.MAX_POINTS),
            'iq': _SeriesBuffer(self.MAX_POINTS),
            'ibus': _SeriesBuffer(self.MAX_POINTS),
            'ia': _SeriesBuffer(self.MAX_POINTS),
            'ib': _SeriesBuffer(self.MAX_POINTS),
            'ic': _SeriesBuffer(self.MAX_POINTS),
            'angle': _SeriesBuffer(self.MAX_POINTS),
        }
        self._plots = {}
        self._extra_buffers = {}
        self._current_series_order = ('id', 'iq', 'ibus', 'ia', 'ib', 'ic')
        self._current_series_labels = {
            'id': 'Id',
            'iq': 'Iq',
            'ibus': 'IBus',
            'ia': 'Ia',
            'ib': 'Ib',
            'ic': 'Ic',
        }
        self._current_checks = {}
        self._latest_t = None          # 增量维护的最新时间戳
        self._auto_range_counter = 0
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        if pg is None:
            hint = QLabel(
                "实时曲线需要 pyqtgraph。\n"
                "当前环境缺少该依赖，无法显示交互波形。")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #888; padding: 20px;")
            layout.addWidget(hint)
            return

        toolbar = QWidget()
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(4)

        self._btn_pause = QPushButton("暂停")
        self._btn_pause.setCheckable(True)
        self._btn_pause.setFixedHeight(22)
        self._btn_pause.clicked.connect(self._on_pause_toggled)
        bar.addWidget(self._btn_pause)

        self._chk_follow = QCheckBox("跟随")
        self._chk_follow.setChecked(True)
        bar.addWidget(self._chk_follow)

        self._chk_auto_y = QCheckBox("Y自适应")
        self._chk_auto_y.setChecked(True)
        bar.addWidget(self._chk_auto_y)

        bar.addWidget(QLabel("窗口(s):"))
        self._spin_window = QSpinBox()
        self._spin_window.setRange(5, 120)
        self._spin_window.setValue(self._window_seconds)
        self._spin_window.setFixedWidth(58)
        self._spin_window.valueChanged.connect(self._on_window_changed)
        bar.addWidget(self._spin_window)

        bar.addWidget(QLabel("电流:"))
        for name in self._current_series_order:
            chk = QCheckBox(self._current_series_labels[name])
            chk.setChecked(True)
            chk.toggled.connect(self._sync_current_visibility)
            self._current_checks[name] = chk
            bar.addWidget(chk)

        self._btn_reset = QPushButton("重置视图")
        self._btn_reset.setFixedHeight(22)
        self._btn_reset.clicked.connect(self._reset_view)
        bar.addWidget(self._btn_reset)

        self._btn_clear = QPushButton("清空")
        self._btn_clear.setFixedHeight(22)
        self._btn_clear.clicked.connect(self.clear)
        bar.addWidget(self._btn_clear)

        bar.addStretch()
        layout.addWidget(toolbar)

        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground(theme.hex('plot_bg'))
        self._glw.ci.layout.setContentsMargins(0, 0, 0, 0)
        self._glw.ci.layout.setSpacing(2)
        self._glw.ci.layout.setRowStretchFactor(0, 1)
        self._glw.ci.layout.setRowStretchFactor(1, 1)
        self._glw.ci.layout.setColumnStretchFactor(0, 1)
        self._glw.ci.layout.setColumnStretchFactor(1, 1)
        layout.addWidget(self._glw, 1)

        self._build_plots()
        for cfg in self._plots.values():
            cfg['plot'].vb.sigXRangeChanged.connect(self._on_plot_x_range_changed)
        self._mouse_proxy = pg.SignalProxy(
            self._glw.scene().sigMouseMoved, rateLimit=60, slot=self._on_mouse_moved)
        self._click_proxy = pg.SignalProxy(
            self._glw.scene().sigMouseClicked, rateLimit=60, slot=self._on_mouse_clicked)

        self._timer = QTimer(self)
        self._timer.setInterval(30)   # ~33 FPS, 优化后单次刷新极轻量
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

        self._sync_current_visibility()

    # 曲线名 -> 主题色键
    _PEN_KEY = {
        'pos': 'plot_pos', 'vel': 'plot_vel', 'id': 'plot_id', 'iq': 'plot_iq',
        'ibus': 'plot_ibus', 'ia': 'plot_ia', 'ib': 'plot_ib', 'ic': 'plot_ic',
        'angle': 'plot_angle',
    }

    def _pen(self, name):
        style = Qt.PenStyle.DashLine if name == 'ibus' else Qt.PenStyle.SolidLine
        return pg.mkPen(theme.hex(self._PEN_KEY[name]), width=2, style=style)

    def _build_plots(self):
        pos_pen = self._pen('pos')
        vel_pen = self._pen('vel')
        id_pen = self._pen('id')
        iq_pen = self._pen('iq')
        ibus_pen = self._pen('ibus')
        ia_pen = self._pen('ia')
        ib_pen = self._pen('ib')
        ic_pen = self._pen('ic')
        angle_pen = self._pen('angle')

        self._plots['pos'] = self._create_plot(
            0, 0, "位置", "位置", "rad",
            [('pos', pos_pen, '位置')], show_bottom=True, show_left=True,
            menu_profile={'auto_y': True, 'legend': False, 'series': False})
        self._plots['vel'] = self._create_plot(
            0, 1, "速度", "速度", "rad/s",
            [('vel', vel_pen, '速度')], show_bottom=True, show_left=True,
            menu_profile={'auto_y': True, 'legend': False, 'series': False})
        self._plots['current'] = self._create_plot(
            1, 0, "电流", "电流", "A",
            [
                ('id', id_pen, 'Id'),
                ('iq', iq_pen, 'Iq'),
                ('ibus', ibus_pen, 'IBus'),
                ('ia', ia_pen, 'Ia'),
                ('ib', ib_pen, 'Ib'),
                ('ic', ic_pen, 'Ic'),
            ],
            show_bottom=True, show_left=True,
            menu_profile={'auto_y': True, 'legend': True, 'series': True})
        self._plots['angle'] = self._create_plot(
            1, 1, "机械角度", "机械角度", "rad",
            [('angle', angle_pen, '机械角度')],
            show_bottom=True, show_left=True,
            menu_profile={'auto_y': True, 'legend': False, 'series': False})

        anchor = self._plots['pos']['plot']
        for key in ('vel', 'current', 'angle'):
            self._plots[key]['plot'].setXLink(anchor)

    def apply_theme(self):
        """主题切换: 重设曲线背景/画笔/标题/坐标轴色。"""
        if pg is None:
            return
        self._glw.setBackground(theme.hex('plot_bg'))
        axis_pen = pg.mkPen(theme.hex('muted'))
        text_col = theme.c('text')
        for cfg in self._plots.values():
            plot = cfg['plot']
            plot.setTitle(cfg.get('title', ''), color=theme.hex('plot_title'), size='8pt')
            for ax_name in ('left', 'bottom'):
                ax = plot.getAxis(ax_name)
                ax.setPen(axis_pen)
                ax.setTextPen(text_col)
            for name, curve in cfg['curves'].items():
                if name in self._PEN_KEY:
                    curve.setPen(self._pen(name))

    def _create_plot(self, row, col, title, y_label, unit, series_defs,
                     show_bottom=True, show_left=True, menu_profile=None):
        menu_profile = dict(menu_profile or {})
        menu_profile.setdefault('auto_y', True)
        menu_profile.setdefault('legend', False)
        menu_profile.setdefault('series', False)

        plot = self._glw.addPlot(row=row, col=col, title=title)
        plot.setTitle(title, color=theme.hex('plot_title'), size='8pt')
        plot.layout.setContentsMargins(0, 0, 0, 0)
        plot.layout.setSpacing(0)
        plot.showGrid(x=True, y=True, alpha=0.25)
        plot.setLabel('left', y_label, units=unit)
        plot.setLabel('bottom', '时间', units='s')
        plot.showAxis('bottom', show_bottom)
        plot.showAxis('left', show_left)
        plot.getAxis('left').setWidth(50)
        plot.getAxis('bottom').setHeight(20)
        plot.setMenuEnabled(False)
        plot.vb.setMenuEnabled(False)
        plot.setClipToView(True)
        plot.setDownsampling(auto=True, mode='peak')
        plot.setDefaultPadding(0.01)
        plot.addLegend(offset=(4, 4))

        vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen((180, 180, 180, 160), style=Qt.PenStyle.DashLine))
        hline = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen((180, 180, 180, 160), style=Qt.PenStyle.DashLine))
        vline.setVisible(False)
        hline.setVisible(False)
        plot.addItem(vline, ignoreBounds=True)
        plot.addItem(hline, ignoreBounds=True)

        label = pg.TextItem(anchor=(1, 0), fill=(0, 0, 0, 160), color='#F5F5F5')
        label.setVisible(False)
        plot.addItem(label, ignoreBounds=True)

        curves = {}
        for name, pen, curve_label in series_defs:
            curve = plot.plot([], [], pen=pen, name=curve_label)
            curves[name] = curve

        legend_visible = bool(menu_profile.get('legend', False))
        if plot.legend is not None:
            plot.legend.setVisible(legend_visible)

        return {
            'plot': plot,
            'vline': vline,
            'hline': hline,
            'label': label,
            'curves': curves,
            'legend': plot.legend,
            'series_defs': series_defs,
            'menu_profile': menu_profile,
            'auto_y': True,
            'title': title,
        }

    def _on_mouse_clicked(self, evt):
        if pg is None:
            return
        event = evt[0] if isinstance(evt, tuple) else evt
        if event.button() != Qt.MouseButton.RightButton:
            return

        key_cfg = self._find_plot_by_scene_pos(event.scenePos())
        if key_cfg is None:
            return

        key, cfg = key_cfg
        menu = self._build_context_menu(key, cfg)
        if menu is None:
            return
        event.accept()
        pos = event.screenPos()
        menu.exec(pos.toPoint() if hasattr(pos, 'toPoint') else pos)

    def _find_plot_by_scene_pos(self, scene_pos):
        for key, cfg in self._plots.items():
            if cfg['plot'].vb.sceneBoundingRect().contains(scene_pos):
                return key, cfg
        return None

    def _build_context_menu(self, key: str, cfg):
        menu = QMenu(self)
        menu.setTitle("图表菜单")

        act_zoom_in_x = menu.addAction("放大时间轴")
        act_zoom_out_x = menu.addAction("缩小时间轴")
        act_zoom_in_y = menu.addAction("放大纵轴")
        act_zoom_out_y = menu.addAction("缩小纵轴")
        menu.addSeparator()
        act_auto = menu.addAction("自动缩放")
        act_reset = menu.addAction("重置视图")
        act_follow = menu.addAction("跟随尾部")
        act_follow.setCheckable(True)
        act_follow.setChecked(self._chk_follow.isChecked())
        menu.addSeparator()
        act_pause = menu.addAction("暂停更新" if not self._paused else "继续更新")
        act_clear = menu.addAction("清空曲线")

        profile = cfg.get('menu_profile', {})
        if profile.get('auto_y', True):
            act_autoy = menu.addAction("本图Y轴自适应")
            act_autoy.setCheckable(True)
            act_autoy.setChecked(cfg.get('auto_y', True))
        else:
            act_autoy = None

        if key == 'current' and profile.get('series', True):
            menu.addSeparator()
            for name in self._current_series_order:
                act = menu.addAction(f"显示 {self._current_series_labels[name]}")
                act.setCheckable(True)
                act.setChecked(self._current_checks[name].isChecked())
                act.toggled.connect(
                    lambda checked, series=name: self._current_checks[series].setChecked(checked))

        act_legend = None
        if profile.get('legend', True) and cfg.get('legend') is not None:
            menu.addSeparator()
            act_legend = menu.addAction("显示本图图例")
            act_legend.setCheckable(True)
            act_legend.setChecked(cfg['legend'].isVisible())

        def _set_x_scale(factor):
            cfg['plot'].getViewBox().scaleBy((factor, 1.0))

        def _set_y_scale(factor):
            cfg['plot'].getViewBox().scaleBy((1.0, factor))

        act_zoom_in_x.triggered.connect(lambda: _set_x_scale(0.8))
        act_zoom_out_x.triggered.connect(lambda: _set_x_scale(1.25))
        act_zoom_in_y.triggered.connect(lambda: _set_y_scale(0.8))
        act_zoom_out_y.triggered.connect(lambda: _set_y_scale(1.25))
        act_auto.triggered.connect(lambda: self._refresh(force=True))
        act_reset.triggered.connect(self._reset_view)
        act_follow.toggled.connect(self._chk_follow.setChecked)
        act_pause.triggered.connect(lambda: self._btn_pause.click())
        act_clear.triggered.connect(self.clear)
        if act_autoy is not None:
            act_autoy.toggled.connect(lambda checked, plot_key=key: self._set_plot_auto_y(plot_key, checked))
        if act_legend is not None:
            act_legend.toggled.connect(lambda checked, plot_key=key: self._set_plot_legend(plot_key, checked))
        return menu

    def _on_pause_toggled(self, checked: bool):
        self._paused = checked
        self._btn_pause.setText("继续" if checked else "暂停")
        self._btn_pause.setStyleSheet(
            "background-color: #F57C00; color: white; font-weight: bold;"
            if checked else "")

    def _on_window_changed(self, value: int):
        self._window_seconds = max(5, int(value))

    def _set_plot_auto_y(self, plot_key: str, enabled: bool):
        cfg = self._plots.get(plot_key)
        if cfg is None:
            return
        cfg['auto_y'] = bool(enabled)
        if enabled:
            self._refresh(force=True)

    def _set_plot_legend(self, plot_key: str, enabled: bool):
        cfg = self._plots.get(plot_key)
        if cfg is None or cfg.get('legend') is None:
            return
        cfg['legend'].setVisible(bool(enabled))

    def _sync_current_visibility(self):
        if pg is None:
            return
        for name in self._current_series_order:
            curve = self._plots['current']['curves'].get(name)
            chk = self._current_checks.get(name)
            if curve is not None and chk is not None:
                curve.setVisible(chk.isChecked())

    def _visible_current_series(self):
        return tuple(
            name for name in self._current_series_order
            if self._current_checks.get(name) is not None and self._current_checks[name].isChecked()
        )

    def _reset_view(self):
        self._chk_follow.setChecked(True)
        self._chk_auto_y.setChecked(True)
        if self._paused:
            self._btn_pause.click()
        self._refresh(force=True)

    def add_series(self, name: str):
        self._extra_buffers.setdefault(name, _SeriesBuffer(self.MAX_POINTS))

    def append(self, name: str, ts: float, value: float):
        if self._t0 is None:
            self._t0 = ts
        t = ts - self._t0
        if name in self._buffers:
            self._buffers[name].append(t, value)
        else:
            self._extra_buffers.setdefault(name, _SeriesBuffer(self.MAX_POINTS)).append(t, value)
        if self._latest_t is None or t > self._latest_t:
            self._latest_t = t

    def clear(self):
        self._t0 = None
        self._latest_t = None
        self._auto_range_counter = 0
        for buf in self._buffers.values():
            buf.clear()
        for buf in self._extra_buffers.values():
            buf.clear()
        if pg is None:
            return
        for cfg in self._plots.values():
            for curve in cfg['curves'].values():
                curve.setData([], [])
            cfg['vline'].setVisible(False)
            cfg['hline'].setVisible(False)
            cfg['label'].setVisible(False)

    def feed_feedback(self, ts: float, fb):
        if self._t0 is None:
            self._t0 = ts
        t = ts - self._t0
        self._buffers['pos'].append(t, getattr(fb, 'pos', 0.0))
        self._buffers['vel'].append(t, getattr(fb, 'vel', 0.0))
        self._buffers['id'].append(t, getattr(fb, 'id', 0.0))
        self._buffers['iq'].append(t, getattr(fb, 'iq', 0.0))
        self._buffers['ibus'].append(t, getattr(fb, 'ibus', 0.0))
        self._buffers['ia'].append(t, getattr(fb, 'ia', 0.0))
        self._buffers['ib'].append(t, getattr(fb, 'ib', 0.0))
        self._buffers['ic'].append(t, getattr(fb, 'ic', 0.0))
        multiturn = float(getattr(fb, 'multiturn', 0))
        single = float(getattr(fb, 'single', 0.0))
        self._buffers['angle'].append(t, multiturn * (2.0 * math.pi) + single)
        if self._latest_t is None or t > self._latest_t:
            self._latest_t = t

    # ------------------------------------------------------------------
    # 核心刷新
    # ------------------------------------------------------------------

    def _any_dirty(self) -> bool:
        """检查任一 buffer 自上次刷新后是否有新数据."""
        for buf in self._buffers.values():
            if buf.dirty:
                return True
        return False

    def _refresh(self, force=False):
        if pg is None or (self._paused and not force):
            return
        if not self._any_dirty() and not force:
            return

        t_end = self._latest_t
        if t_end is None:
            return
        t_min = max(0.0, t_end - float(self._window_seconds))

        self._update_curve('pos', t_min)
        self._update_curve('vel', t_min)
        self._update_curve('id', t_min)
        self._update_curve('iq', t_min)
        self._update_curve('ibus', t_min)
        self._update_curve('ia', t_min)
        self._update_curve('ib', t_min)
        self._update_curve('ic', t_min)
        self._update_curve('angle', t_min)

        if self._chk_follow.isChecked() or force:
            self._set_all_x_range(t_min, max(t_end, t_min + 0.1))

        # Y 轴自适应节流: 每 _AUTO_RANGE_EVERY 帧或 force 时执行
        if self._chk_auto_y.isChecked():
            self._auto_range_counter += 1
            if self._auto_range_counter >= self._AUTO_RANGE_EVERY or force:
                self._auto_range_counter = 0
                self._auto_range_y(t_min)

        # 清除脏标记
        for buf in self._buffers.values():
            buf.dirty = False

    def _update_curve(self, name: str, t_min: float):
        cfg = self._plots.get('current') if name in self._current_series_order else self._plots.get(name)
        if cfg is None:
            return
        buf = self._buffers[name]
        ts, ys = buf.arrays(t_min)
        cfg['curves'][name].setData(ts, ys)

    def _set_all_x_range(self, t_min: float, t_max: float):
        if pg is None:
            return
        self._syncing_x = True
        try:
            for cfg in self._plots.values():
                cfg['plot'].setXRange(t_min, t_max, padding=0.02)
        finally:
            self._syncing_x = False

    def _on_plot_x_range_changed(self, *args):
        if pg is None or self._syncing_x:
            return
        sender = self.sender()
        if sender is None:
            return
        try:
            x0, x1 = sender.viewRange()[0]
        except Exception:
            return
        self._syncing_x = True
        try:
            for cfg in self._plots.values():
                vb = cfg['plot'].vb
                if vb is sender:
                    continue
                vb.setXRange(x0, x1, padding=0)
        finally:
            self._syncing_x = False

    def _auto_range_y(self, t_min: float):
        def _range_from(names):
            arrays = []
            for n in names:
                buf = self._buffers[n]
                _ts, ys = buf.arrays(t_min)
                if len(ys):
                    arrays.append(ys)
            if not arrays:
                return None
            ys = np.concatenate(arrays)
            if not len(ys):
                return None
            y0 = float(np.min(ys))
            y1 = float(np.max(ys))
            pad = max((y1 - y0) * 0.15, 1e-3) if y0 != y1 else max(1.0, abs(y0) * 0.1)
            return y0 - pad, y1 + pad

        ranges = {
            'pos': _range_from(('pos',)),
            'vel': _range_from(('vel',)),
            'current': _range_from(self._visible_current_series()),
            'angle': _range_from(('angle',)),
        }
        for key, rng in ranges.items():
            if rng is not None and self._plots.get(key, {}).get('auto_y', True):
                self._plots[key]['plot'].setYRange(rng[0], rng[1], padding=0.02)

    def _on_mouse_moved(self, evt):
        if pg is None:
            return
        scene_pos = evt[0] if isinstance(evt, tuple) else evt
        hovered = None
        for key, cfg in self._plots.items():
            vb = cfg['plot'].vb
            if vb.sceneBoundingRect().contains(scene_pos):
                hovered = (key, cfg, vb.mapSceneToView(scene_pos))
                break

        for key, cfg in self._plots.items():
            active = hovered is not None and hovered[0] == key
            cfg['vline'].setVisible(active)
            cfg['hline'].setVisible(active)
            cfg['label'].setVisible(active)

        if hovered is None:
            return

        key, cfg, mouse = hovered
        x = float(mouse.x())
        y = float(mouse.y())
        cfg['vline'].setPos(x)
        cfg['hline'].setPos(y)
        cfg['label'].setHtml(self._hover_values(key, x, y))
        cfg['label'].setPos(x, y)

    def _hover_values(self, key: str, x: float, y: float) -> str:
        if key == 'current':
            parts = [f"<b>t</b> = {x:.2f} s"]
            for name in self._current_series_order:
                chk = self._current_checks.get(name)
                if chk is not None and not chk.isChecked():
                    continue
                val = self._buffers[name].interpolate(x)
                if val is not None:
                    parts.append(f"{self._current_series_labels[name]} = {val:.3f} A")
            return "<div style='color:#F5F5F5;'>" + "<br/>".join(parts) + "</div>"

        series_name = {'pos': 'pos', 'vel': 'vel', 'angle': 'angle'}.get(key)
        if series_name is None:
            return ""
        val = self._buffers[series_name].interpolate(x)
        labels = {'pos': '位置', 'vel': '速度', 'angle': '机械角度'}
        units = {'pos': 'rad', 'vel': 'rad/s', 'angle': 'rad'}
        val_text = "--" if val is None else f"{val:.4f}"
        return (
            "<div style='color:#F5F5F5;'>"
            f"<b>t</b> = {x:.2f} s<br/>"
            f"{labels[series_name]} = {val_text} {units[series_name]}"
            "</div>"
        )
