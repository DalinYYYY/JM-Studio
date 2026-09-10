"""实时曲线面板.

使用 pyqtgraph 提供 4 组联动波形:
位置、速度、电流、机械角度。
支持窗口点数、暂停、跟随、Y 轴自适应、曲线显隐与鼠标悬停读数。

性能优化要点:
- _SeriesBuffer 使用预分配 numpy 环形缓冲区, 零分配追加, 惰性时间顺序缓存
- _refresh() 通过脏标记跳过无新数据时的重绘
- _latest_idx 增量更新, 避免每帧扫描全部 buffer
- Y 轴自适应每 ~300ms 计算一次(10 帧 @ 30ms)
- interpolate() 复用缓存数组, 避免鼠标悬停时的重复转换
"""

from dataclasses import dataclass
import math

import numpy as np

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QMenu, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from ui.theme import theme
from ui.display_filter import MovingAverageAngle, MovingAverage

try:
    import pyqtgraph as pg
except Exception:  # pragma: no cover - fallback for environments without pyqtgraph
    pg = None


if pg is not None:
    # 大数据量性能优化: 关闭抗锯齿(绘制耗时降低 2-5 倍), 尝试启用 OpenGL(GPU 加速)
    try:
        pg.setConfigOptions(antialias=False, useOpenGL=True)
    except Exception:
        pg.setConfigOptions(antialias=False)


@dataclass
class _SeriesBuffer:
    """预分配 numpy 环形缓冲区, append O(1) 无内存分配, 惰性时间顺序缓存.

    性能优化:
      - float32 替代 float64 (带宽减半, 精度对电机反馈足够)
      - append_batch 批量写入 (向量化, 单次失效缓存)
      - arrays_view 零拷贝读取 (供 pyqtgraph setData avoidCopy)
    """

    maxlen: int

    def __post_init__(self):
        n = int(self.maxlen)
        # float32: 精度对电机反馈(rad/A/V)足够, 带宽减半
        self._ts = np.full(n, np.nan, dtype=np.float32)
        self._ys = np.full(n, np.nan, dtype=np.float32)
        self._head = 0          # 下一次写入位置
        self._count = 0         # 当前有效元素数
        self._maxlen = n
        self._cache_ts = None   # 惰性: 首次读取时构建时间顺序视图
        self._cache_ys = None
        self.dirty = False

    def append(self, t: float, value: float):
        """O(1) 写入, 失效缓存, 置脏标记."""
        self._ts[self._head] = t
        self._ys[self._head] = value
        self._head = (self._head + 1) % self._maxlen
        if self._count < self._maxlen:
            self._count += 1
        self._cache_ts = None
        self._cache_ys = None
        self.dirty = True

    def append_batch(self, ts_arr: np.ndarray, ys_arr: np.ndarray):
        """批量写入 (向量化), 单次失效缓存.

        Args:
            ts_arr, ys_arr: 长度相同的一维数组, 顺序写入
        """
        n_in = len(ts_arr)
        if n_in == 0:
            return
        # 确保输入是 float32 (必要时拷贝转换, 但通常已是 float32)
        if ts_arr.dtype != np.float32:
            ts_arr = ts_arr.astype(np.float32)
        if ys_arr.dtype != np.float32:
            ys_arr = ys_arr.astype(np.float32)
        cap = self._maxlen
        head = self._head
        # 情况1: 一次写满不超过尾部, 直接切片拷贝
        if head + n_in <= cap:
            self._ts[head:head + n_in] = ts_arr
            self._ys[head:head + n_in] = ys_arr
        else:
            # 情况2: 跨越尾部, 分两段
            first = cap - head
            self._ts[head:cap] = ts_arr[:first]
            self._ys[head:cap] = ys_arr[:first]
            second = n_in - first
            if second <= cap:
                self._ts[:second] = ts_arr[first:]
                self._ys[:second] = ys_arr[first:]
            else:
                # 输入超过容量: 只保留最后 cap 个
                self._ts[:] = ts_arr[n_in - cap:]
                self._ys[:] = ys_arr[n_in - cap:]
                self._head = 0
                self._count = cap
                self._cache_ts = None
                self._cache_ys = None
                self.dirty = True
                return
        self._head = (head + n_in) % cap
        new_count = min(self._count + n_in, cap)
        self._count = new_count
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
        """惰性构建时间顺序数组.

        优化:
          - 未满缓冲时返回 view (零拷贝)
          - 满缓冲时用 np.concatenate 拼接两段 (比 fancy-index 快 6 倍:
            0.23ms vs 1.48ms @ 30万点)
        """
        c = self._count
        if c == 0:
            self._cache_ts = np.empty(0, dtype=np.float32)
            self._cache_ys = np.empty(0, dtype=np.float32)
            return
        if c < self._maxlen:
            # 未满: 直接切片 view, 零拷贝
            self._cache_ts = self._ts[:c]
            self._cache_ys = self._ys[:c]
        else:
            # 满缓冲: concatenate 两段 (head: 和 :head), 时间顺序正确
            # 比 fancy-index (idx = (head+arange)%cap; ts[idx]) 快 6 倍
            # 注: concatenate 总是返回新数组 (独立内存), 安全供 avoidCopy 使用
            h = self._head
            self._cache_ts = np.concatenate([self._ts[h:], self._ts[:h]])
            self._cache_ys = np.concatenate([self._ys[h:], self._ys[:h]])

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

    def arrays(self, idx_min: float = None):
        """返回 (ts, ys) 拷贝, 安全用于 pyqtgraph setData."""
        ts = self._ts_view
        ys = self._ys_view
        if len(ts) == 0:
            return ts, ys
        if idx_min is None:
            return ts.copy(), ys.copy()
        i = int(np.searchsorted(ts, idx_min, side='left'))
        if i >= len(ts):
            return np.empty(0, dtype=np.float32), np.empty(0, dtype=np.float32)
        return ts[i:].copy(), ys[i:].copy()

    def arrays_view(self, idx_min: float = None):
        """返回 (ts, ys) 视图 (零拷贝), 仅供主线程同步调用且不修改返回数组.

        优化策略:
          - 满缓冲时 _ts_view 是 fancy-index 出的独立数组 (setData 安全, avoidCopy 可用)
          - 未满缓冲时切片 view 指向底层 ring buffer, 后续 append 会修改该内存,
            故此时退化为 copy (避免 setData 后数据被改写)
        """
        ts = self._ts_view
        ys = self._ys_view
        if len(ts) == 0:
            return ts, ys
        # 满缓冲时 cache 是独立数组, 未满时是切片 view (不安全, 需 copy)
        need_copy = self._count < self._maxlen
        if idx_min is None:
            if need_copy:
                return ts.copy(), ys.copy()
            return ts, ys
        i = int(np.searchsorted(ts, idx_min, side='left'))
        if i >= len(ts):
            return np.empty(0, dtype=np.float32), np.empty(0, dtype=np.float32)
        if need_copy:
            return ts[i:].copy(), ys[i:].copy()
        return ts[i:], ys[i:]

    def last_time(self):
        """返回最新时间戳, O(1)."""
        if self._count == 0:
            return None
        return float(self._ts[(self._head - 1) % self._maxlen])

    def first_time(self):
        """返回最早时间戳, O(1). 未满时最旧在索引0, 满时最旧在写入位置 _head."""
        if self._count == 0:
            return None
        if self._count < self._maxlen:
            return float(self._ts[0])
        return float(self._ts[self._head])

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


class PlotPanel(QWidget):
    """实时曲线面板。"""

    debug_window_toggled = pyqtSignal(bool)  # 调试通道复选框状态变化

    # 缓冲区容量: 30 万点/曲线 (30 万 × 9 曲线 × 8B = 21.6 MB, 内存可接受)
    # 以 1kHz 采样可保留 300 秒历史, 以 10kHz 保留 30 秒
    MAX_POINTS = 300_000
    _AUTO_RANGE_EVERY = 10       # Y 轴自适应节流: 每 N 帧执行一次

    def __init__(self, parent=None):
        super().__init__(parent)
        self._point_index = 0       # 全局数据点编号计数器
        self._paused = False
        self._window_points = 2000  # 窗口显示的数据点数
        self._syncing_x = False
        self._plot_states = {}
        self._buffers = {
            'pos': _SeriesBuffer(self.MAX_POINTS),
            'pos_ref': _SeriesBuffer(self.MAX_POINTS),
            'vel': _SeriesBuffer(self.MAX_POINTS),
            'vel_ref': _SeriesBuffer(self.MAX_POINTS),
            'id': _SeriesBuffer(self.MAX_POINTS),
            'id_ref': _SeriesBuffer(self.MAX_POINTS),
            'iq': _SeriesBuffer(self.MAX_POINTS),
            'iq_ref': _SeriesBuffer(self.MAX_POINTS),
            'ibus': _SeriesBuffer(self.MAX_POINTS),
            'ia': _SeriesBuffer(self.MAX_POINTS),
            'ib': _SeriesBuffer(self.MAX_POINTS),
            'ic': _SeriesBuffer(self.MAX_POINTS),
            'angle': _SeriesBuffer(self.MAX_POINTS),
        }
        self._plots = {}
        self._extra_buffers = {}
        self._current_series_order = (
            'id', 'id_ref', 'iq', 'iq_ref', 'ibus', 'ia', 'ib', 'ic')
        self._current_series_labels = {
            'id': 'Id',
            'id_ref': 'Id目标',
            'iq': 'Iq',
            'iq_ref': 'Iq目标',
            'ibus': 'IBus',
            'ia': 'Ia',
            'ib': 'Ib',
            'ic': 'Ic',
        }
        self._current_checks = {}
        self._latest_idx = None          # 增量维护的最新数据点编号
        self._auto_range_counter = 0
        self._angle_ma = MovingAverageAngle(window=8)   # angle 曲线移动平均, 平滑 0.01rad 编码器噪声
        self._pos_ma = MovingAverage(window=8)          # pos 曲线移动平均, 平滑多圈位置噪声
        self._vel_ma = MovingAverage(window=8)          # vel 曲线移动平均, 平滑速度噪声(PLL观测器)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
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

        bar.addWidget(QLabel("窗口(点):"))
        self._spin_window = QSpinBox()
        self._spin_window.setRange(100, 50000)
        self._spin_window.setValue(self._window_points)
        self._spin_window.setFixedWidth(108)
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

        self._debug_checkbox = QCheckBox("调试通道")
        self._debug_checkbox.setChecked(False)
        self._debug_checkbox.toggled.connect(self.debug_window_toggled.emit)
        bar.addWidget(self._debug_checkbox)

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
        'pos': 'plot_pos', 'pos_ref': 'plot_target',
        'vel': 'plot_vel', 'vel_ref': 'plot_target',
        'id': 'plot_id', 'id_ref': 'plot_target',
        'iq': 'plot_iq', 'iq_ref': 'plot_target',
        'ibus': 'plot_ibus', 'ia': 'plot_ia', 'ib': 'plot_ib', 'ic': 'plot_ic',
        'angle': 'plot_angle',
    }

    def _pen(self, name):
        style = (Qt.PenStyle.DashLine
                 if name == 'ibus' or name.endswith('_ref')
                 else Qt.PenStyle.SolidLine)
        return pg.mkPen(theme.hex(self._PEN_KEY[name]), width=2, style=style)

    def _build_plots(self):
        pos_pen = self._pen('pos')
        pos_ref_pen = self._pen('pos_ref')
        vel_pen = self._pen('vel')
        vel_ref_pen = self._pen('vel_ref')
        id_pen = self._pen('id')
        id_ref_pen = self._pen('id_ref')
        iq_pen = self._pen('iq')
        iq_ref_pen = self._pen('iq_ref')
        ibus_pen = self._pen('ibus')
        ia_pen = self._pen('ia')
        ib_pen = self._pen('ib')
        ic_pen = self._pen('ic')
        angle_pen = self._pen('angle')

        self._plots['pos'] = self._create_plot(
            0, 0, "位置", "位置", "rad",
            [('pos', pos_pen, '位置'), ('pos_ref', pos_ref_pen, '位置目标')],
            show_bottom=True, show_left=True,
            menu_profile={'auto_y': True, 'legend': True, 'series': False})
        self._plots['vel'] = self._create_plot(
            0, 1, "速度", "速度", "rad/s",
            [('vel', vel_pen, '速度'), ('vel_ref', vel_ref_pen, '速度目标')],
            show_bottom=True, show_left=True,
            menu_profile={'auto_y': True, 'legend': True, 'series': False})
        self._plots['current'] = self._create_plot(
            1, 0, "电流", "电流", "A",
            [
                ('id', id_pen, 'Id'),
                ('id_ref', id_ref_pen, 'Id目标'),
                ('iq', iq_pen, 'Iq'),
                ('iq_ref', iq_ref_pen, 'Iq目标'),
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
        plot.setLabel('bottom', '数据点')
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

        act_zoom_in_x = menu.addAction("放大X轴")
        act_zoom_out_x = menu.addAction("缩小X轴")
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
        self._window_points = max(100, int(value))

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

    def set_debug_checkbox(self, checked: bool):
        """外部（MainWindow）同步调试通道复选框状态，不触发信号。"""
        if not hasattr(self, "_debug_checkbox"):
            return
        self._debug_checkbox.blockSignals(True)
        self._debug_checkbox.setChecked(checked)
        self._debug_checkbox.blockSignals(False)

    def set_debug_channel_visible(self, visible: bool):
        """调试通道复选框显隐。测试员版不接入调试窗口, 隐藏入口。"""
        self._debug_checkbox.setVisible(visible)

    def _visible_current_series(self):
        return tuple(
            name for name in self._current_series_order
            if self._current_checks.get(name) is not None and self._current_checks[name].isChecked()
        )

    def _reset_view(self):
        if pg is None:
            return
        self._chk_follow.setChecked(True)
        self._chk_auto_y.setChecked(True)
        if self._paused:
            self._btn_pause.click()
        self._refresh(force=True)

    def add_series(self, name: str):
        self._extra_buffers.setdefault(name, _SeriesBuffer(self.MAX_POINTS))

    def append(self, name: str, value: float):
        idx = float(self._point_index)
        if name in self._buffers:
            self._buffers[name].append(idx, value)
        else:
            self._extra_buffers.setdefault(name, _SeriesBuffer(self.MAX_POINTS)).append(idx, value)
        if self._latest_idx is None or idx > self._latest_idx:
            self._latest_idx = idx

    def clear(self):
        self._point_index = 0
        self._latest_idx = None
        self._auto_range_counter = 0
        self._angle_ma.reset()
        self._pos_ma.reset()
        self._vel_ma.reset()
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

    # ==================== 配置持久化 ====================
    def get_opts(self) -> dict:
        """收集可持久化的 UI 配置 (关闭时由主窗口调用)."""
        if pg is None:
            return {"window_points": int(self._window_points)}
        opts = {
            "window_points": int(self._window_points),
            "follow": self._chk_follow.isChecked(),
            "auto_y": self._chk_auto_y.isChecked(),
            "current_visibility": {
                name: chk.isChecked() for name, chk in self._current_checks.items()
            },
            "per_plot": {},
            "debug_window": {
                "checked": self._debug_checkbox.isChecked(),
            },
        }
        # 各子图 auto_y / legend 显隐
        for key, cfg in self._plots.items():
            profile = cfg.get('menu_profile', {})
            entry = {}
            if profile.get('auto_y', True):
                entry['auto_y'] = bool(cfg.get('auto_y', True))
            if profile.get('legend', False) and cfg.get('legend') is not None:
                entry['legend'] = bool(cfg['legend'].isVisible())
            if entry:
                opts['per_plot'][key] = entry
        return opts

    def set_opts(self, opts: dict):
        """启动时套用配置 (容错: 单项失败不影响其他)."""
        if not isinstance(opts, dict):
            return
        try:
            wp = int(opts.get("window_points", self._window_points))
            if 100 <= wp <= 50000:
                self._window_points = wp
        except Exception:
            pass
        if pg is None:
            return
        try:
            self._spin_window.setValue(self._window_points)
        except Exception:
            pass
        try:
            self._chk_follow.setChecked(bool(opts.get("follow", True)))
        except Exception:
            pass
        try:
            self._chk_auto_y.setChecked(bool(opts.get("auto_y", True)))
        except Exception:
            pass
        cv = opts.get("current_visibility")
        if isinstance(cv, dict):
            for name, chk in self._current_checks.items():
                try:
                    chk.setChecked(bool(cv.get(name, True)))
                except Exception:
                    pass
            try:
                self._sync_current_visibility()
            except Exception:
                pass
        pp = opts.get("per_plot")
        if isinstance(pp, dict):
            for key, entry in pp.items():
                if not isinstance(entry, dict):
                    continue
                cfg = self._plots.get(key)
                if cfg is None:
                    continue
                if 'auto_y' in entry:
                    try:
                        self._set_plot_auto_y(key, bool(entry['auto_y']))
                    except Exception:
                        pass
                if 'legend' in entry and cfg.get('legend') is not None:
                    try:
                        self._set_plot_legend(key, bool(entry['legend']))
                    except Exception:
                        pass
        # 调试通道复选框状态
        dw = opts.get("debug_window")
        if isinstance(dw, dict):
            try:
                self._debug_checkbox.setChecked(bool(dw.get("checked", False)))
            except Exception:
                pass

    def feed_feedback(self, fb):
        idx = float(self._point_index)
        self._point_index += 1
        filled = getattr(fb, 'filled_fields', ())

        def target_value(field):
            return getattr(fb, field, 0.0) if field in filled else float('nan')

        # pos/vel 移动平均平滑编码器/PLL噪声
        pos_v = float(self._pos_ma.update_batch(
            np.array([getattr(fb, 'pos', 0.0)], dtype=np.float32))[0])
        vel_v = float(self._vel_ma.update_batch(
            np.array([getattr(fb, 'vel', 0.0)], dtype=np.float32))[0])
        self._buffers['pos'].append(idx, pos_v)
        self._buffers['pos_ref'].append(idx, target_value('pos_ref'))
        self._buffers['vel'].append(idx, vel_v)
        self._buffers['vel_ref'].append(idx, target_value('vel_ref'))
        self._buffers['id'].append(idx, getattr(fb, 'id', 0.0))
        self._buffers['id_ref'].append(idx, target_value('id_ref'))
        self._buffers['iq'].append(idx, getattr(fb, 'iq', 0.0))
        self._buffers['iq_ref'].append(idx, target_value('iq_ref'))
        self._buffers['ibus'].append(idx, getattr(fb, 'ibus', 0.0))
        self._buffers['ia'].append(idx, getattr(fb, 'ia', 0.0))
        self._buffers['ib'].append(idx, getattr(fb, 'ib', 0.0))
        self._buffers['ic'].append(idx, getattr(fb, 'ic', 0.0))
        # 机械角度: 显示单圈角度 [0, 2π) rad (对应 0~360°), 与电机机械相一致
        # 移动平均(圆形均值)平滑编码器噪声, 避免静止段毛刺
        single = float(getattr(fb, 'single', 0.0))
        single = float(self._angle_ma.update_batch(
            np.array([single], dtype=np.float32))[0])
        self._buffers['angle'].append(idx, single)
        if self._latest_idx is None or idx > self._latest_idx:
            self._latest_idx = idx

    # 字段提取顺序 (与 _buffers 对齐, pos/vel 单独滤波不在此循环)
    _FEEDBACK_FIELDS = (
        'pos_ref', 'vel_ref', 'id', 'id_ref', 'iq', 'iq_ref',
        'ibus', 'ia', 'ib', 'ic',
    )

    def feed_feedback_batch(self, fb_list):
        """批量向量化写入 (P4 优化): 一次构建 numpy 数组, 9 条曲线各一次 append_batch.

        X 轴使用数据点编号 (0, 1, 2, ...), 每个数据点等间距, 与下位机采样周期无关.
        """
        n = len(fb_list)
        if n == 0:
            return
        # 数据点编号数组 (float32, 一次构建, 9 条曲线复用)
        start_idx = self._point_index
        ts_arr = np.arange(start_idx, start_idx + n, dtype=np.float32)
        self._point_index += n
        # pos/vel 单独滤波(移动平均平滑编码器/PLL噪声), 其余字段直接写入
        ys_pos = np.empty(n, dtype=np.float32)
        ys_vel = np.empty(n, dtype=np.float32)
        for i in range(n):
            ys_pos[i] = getattr(fb_list[i], 'pos', 0.0)
            ys_vel[i] = getattr(fb_list[i], 'vel', 0.0)
        ys_pos = self._pos_ma.update_batch(ys_pos)
        ys_vel = self._vel_ma.update_batch(ys_vel)
        self._buffers['pos'].append_batch(ts_arr, ys_pos)
        self._buffers['vel'].append_batch(ts_arr, ys_vel)
        # 其余字段(id/iq/ibus/ia/ib/ic)
        for field in self._FEEDBACK_FIELDS:
            ys = np.empty(n, dtype=np.float32)
            for i in range(n):
                if field.endswith('_ref'):
                    filled = getattr(fb_list[i], 'filled_fields', ())
                    ys[i] = (getattr(fb_list[i], field, 0.0)
                             if field in filled else np.nan)
                else:
                    ys[i] = getattr(fb_list[i], field, 0.0)
            self._buffers[field].append_batch(ts_arr, ys)
        # 机械角度 (单圈) - 移动平均(圆形均值)平滑编码器噪声
        ys_angle = np.empty(n, dtype=np.float32)
        for i in range(n):
            ys_angle[i] = getattr(fb_list[i], 'single', 0.0)
        ys_angle = self._angle_ma.update_batch(ys_angle)
        self._buffers['angle'].append_batch(ts_arr, ys_angle)
        # 增量维护最新数据点编号 (避免 _refresh 全扫描)
        last_idx = float(ts_arr[-1])
        if self._latest_idx is None or last_idx > self._latest_idx:
            self._latest_idx = last_idx

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

        idx_end = self._latest_idx
        if idx_end is None:
            return
        idx_min = max(0.0, idx_end - float(self._window_points))

        # P2 优化: 曲线级脏标记, 仅重绘有新数据的曲线 (避免全量重绘)
        for name in (
                'pos', 'pos_ref', 'vel', 'vel_ref',
                'id', 'id_ref', 'iq', 'iq_ref',
                'ibus', 'ia', 'ib', 'ic', 'angle'):
            buf = self._buffers[name]
            if force or buf.dirty:
                self._update_curve(name, idx_min)
                buf.dirty = False

        # 额外曲线 (extra_buffers)
        for name, buf in self._extra_buffers.items():
            if force or buf.dirty:
                self._update_curve(name, idx_min)
                buf.dirty = False

        if self._chk_follow.isChecked() or force:
            self._set_all_x_range(idx_min, max(idx_end, idx_min + 1.0))

        # Y 轴自适应节流: 每 _AUTO_RANGE_EVERY 帧或 force 时执行
        if self._chk_auto_y.isChecked():
            self._auto_range_counter += 1
            if self._auto_range_counter >= self._AUTO_RANGE_EVERY or force:
                self._auto_range_counter = 0
                self._auto_range_y(idx_min)

    def _update_curve(self, name: str, idx_min: float):
        if name in self._current_series_order:
            cfg = self._plots.get('current')
        elif name == 'pos_ref':
            cfg = self._plots.get('pos')
        elif name == 'vel_ref':
            cfg = self._plots.get('vel')
        else:
            cfg = self._plots.get(name)
        if cfg is None:
            return
        buf = self._buffers.get(name) if name in self._buffers else self._extra_buffers.get(name)
        if buf is None:
            return
        # P3 优化: 零拷贝读取 (arrays_view), avoidCopy=True 让 pyqtgraph 直接使用该数组
        ts, ys = buf.arrays_view(idx_min)
        if len(ts) == 0:
            return
        try:
            cfg['curves'][name].setData(ts, ys, avoidCopy=True)
        except TypeError:
            # 兼容旧版 pyqtgraph (无 avoidCopy 参数)
            cfg['curves'][name].setData(ts, ys)

    def _set_all_x_range(self, idx_min: float, idx_max: float):
        if pg is None:
            return
        self._syncing_x = True
        try:
            for cfg in self._plots.values():
                cfg['plot'].setXRange(idx_min, idx_max, padding=0.02)
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

    def _auto_range_y(self, idx_min: float):
        def _range_from(names):
            # P5 优化: 使用 arrays_view (满缓冲时零拷贝) + 增量 min/max
            # 避免 np.concatenate 分配大临时数组 (6 曲线 × 30万点 = 7.2MB)
            y0 = None
            y1 = None
            for n in names:
                buf = self._buffers[n]
                _ts, ys = buf.arrays_view(idx_min)
                if not len(ys):
                    continue
                finite = ys[np.isfinite(ys)]
                if not len(finite):
                    continue
                cur_min = float(np.min(finite))
                cur_max = float(np.max(finite))
                if y0 is None:
                    y0 = cur_min
                    y1 = cur_max
                else:
                    if cur_min < y0:
                        y0 = cur_min
                    if cur_max > y1:
                        y1 = cur_max
            if y0 is None:
                return None
            pad = max((y1 - y0) * 0.15, 1e-3) if y0 != y1 else max(1.0, abs(y0) * 0.1)
            return y0 - pad, y1 + pad

        ranges = {
            'pos': _range_from(('pos', 'pos_ref')),
            'vel': _range_from(('vel', 'vel_ref')),
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
            parts = [f"<b>点</b> = {int(x) if x == x else 0}"]
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
            f"<b>点</b> = {int(x) if x == x else 0}<br/>"
            f"{labels[series_name]} = {val_text} {units[series_name]}"
            "</div>"
        )
