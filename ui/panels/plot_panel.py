"""实时曲线面板.

使用 pyqtgraph 提供 4 组联动波形:
位置、速度、电流、机械角度。
支持时间窗、暂停、跟随、Y 轴自适应、曲线显隐与鼠标悬停读数。
"""

from collections import deque
from dataclasses import dataclass
import math

import numpy as np

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QGroupBox, QHBoxLayout, QLabel, QMenu, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

try:
    import pyqtgraph as pg
except Exception:  # pragma: no cover - fallback for environments without pyqtgraph
    pg = None


if pg is not None:
    pg.setConfigOptions(antialias=True)


@dataclass
class _SeriesBuffer:
    maxlen: int

    def __post_init__(self):
        self.ts = deque(maxlen=self.maxlen)
        self.values = deque(maxlen=self.maxlen)

    def append(self, t: float, value: float):
        self.ts.append(float(t))
        self.values.append(float(value))

    def clear(self):
        self.ts.clear()
        self.values.clear()

    def arrays(self, t_min: float = None):
        if not self.ts:
            return np.empty(0, dtype=float), np.empty(0, dtype=float)
        ts = np.asarray(self.ts, dtype=float)
        ys = np.asarray(self.values, dtype=float)
        if t_min is None:
            return ts, ys
        idx = np.searchsorted(ts, t_min, side='left')
        return ts[idx:], ys[idx:]

    def last_time(self):
        return self.ts[-1] if self.ts else None

    def interpolate(self, t: float):
        if not self.ts:
            return None
        ts = np.asarray(self.ts, dtype=float)
        ys = np.asarray(self.values, dtype=float)
        if len(ts) == 1:
            return float(ys[0])
        if t <= ts[0]:
            return float(ys[0])
        if t >= ts[-1]:
            return float(ys[-1])
        idx = int(np.searchsorted(ts, t, side='left'))
        x0, x1 = ts[idx - 1], ts[idx]
        y0, y1 = ys[idx - 1], ys[idx]
        if x1 == x0:
            return float(y0)
        k = (t - x0) / (x1 - x0)
        return float(y0 + (y1 - y0) * k)


class PlotPanel(QGroupBox):
    """实时曲线面板。"""

    MAX_POINTS = 6000

    def __init__(self, parent=None):
        super().__init__("实时曲线", parent)
        self._t0 = None
        self._paused = False
        self._window_seconds = 20
        self._buffers = {
            'pos': _SeriesBuffer(self.MAX_POINTS),
            'vel': _SeriesBuffer(self.MAX_POINTS),
            'iq': _SeriesBuffer(self.MAX_POINTS),
            'ibus': _SeriesBuffer(self.MAX_POINTS),
            'angle': _SeriesBuffer(self.MAX_POINTS),
        }
        self._plots = {}
        self._extra_buffers = {}
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

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
        bar.setSpacing(6)

        self._btn_pause = QPushButton("暂停")
        self._btn_pause.setCheckable(True)
        self._btn_pause.setFixedHeight(24)
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
        self._spin_window.setFixedWidth(64)
        self._spin_window.valueChanged.connect(self._on_window_changed)
        bar.addWidget(self._spin_window)

        self._chk_iq = QCheckBox("Iq")
        self._chk_iq.setChecked(True)
        self._chk_iq.toggled.connect(self._sync_toolbar_state)
        bar.addWidget(self._chk_iq)

        self._chk_ibus = QCheckBox("Ibus")
        self._chk_ibus.setChecked(False)
        self._chk_ibus.toggled.connect(self._sync_toolbar_state)
        bar.addWidget(self._chk_ibus)

        self._btn_reset = QPushButton("重置视图")
        self._btn_reset.setFixedHeight(24)
        self._btn_reset.clicked.connect(self._reset_view)
        bar.addWidget(self._btn_reset)

        self._btn_clear = QPushButton("清空")
        self._btn_clear.setFixedHeight(24)
        self._btn_clear.clicked.connect(self.clear)
        bar.addWidget(self._btn_clear)

        bar.addStretch()
        layout.addWidget(toolbar)

        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground('#202020')
        self._glw.ci.layout.setContentsMargins(0, 0, 0, 0)
        self._glw.ci.layout.setSpacing(4)
        layout.addWidget(self._glw, 1)

        self._build_plots()
        self._mouse_proxy = pg.SignalProxy(
            self._glw.scene().sigMouseMoved, rateLimit=60, slot=self._on_mouse_moved)
        self._click_proxy = pg.SignalProxy(
            self._glw.scene().sigMouseClicked, rateLimit=60, slot=self._on_mouse_clicked)

        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

        self._sync_toolbar_state()

    def _build_plots(self):
        pos_pen = pg.mkPen('#00BCD4', width=2)
        vel_pen = pg.mkPen('#4CAF50', width=2)
        iq_pen = pg.mkPen('#FF9800', width=2)
        ibus_pen = pg.mkPen('#E91E63', width=2, style=Qt.PenStyle.DashLine)
        angle_pen = pg.mkPen('#FFC107', width=2)

        self._plots['pos'] = self._create_plot(
            0, 0, "位置 pos (rad)", "位置", "rad",
            [('pos', pos_pen, '位置')])
        self._plots['vel'] = self._create_plot(
            0, 1, "速度 vel (rad/s)", "速度", "rad/s",
            [('vel', vel_pen, '速度')], link_x=self._plots['pos']['plot'])
        self._plots['current'] = self._create_plot(
            1, 0, "电流 (A)", "电流", "A",
            [('iq', iq_pen, 'Iq'), ('ibus', ibus_pen, 'Ibus')],
            link_x=self._plots['pos']['plot'])
        self._plots['angle'] = self._create_plot(
            1, 1, "机械角度 angle (rad)", "机械角度", "rad",
            [('angle', angle_pen, '机械角度')],
            link_x=self._plots['pos']['plot'])

    def _create_plot(self, row, col, title, y_label, unit, series_defs, link_x=None):
        plot = self._glw.addPlot(row=row, col=col, title=title)
        plot.setTitle(title, color='#D8D8D8', size='10pt')
        plot.showGrid(x=True, y=True, alpha=0.25)
        plot.setLabel('left', y_label, units=unit)
        plot.setLabel('bottom', '时间', units='s')
        plot.setMenuEnabled(False)
        plot.vb.setMenuEnabled(False)
        plot.setClipToView(True)
        plot.setDownsampling(auto=True, mode='peak')
        plot.setDefaultPadding(0.02)
        plot.addLegend(offset=(6, 6))
        if link_x is not None:
            plot.setXLink(link_x)

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

        return {
            'plot': plot,
            'vline': vline,
            'hline': hline,
            'label': label,
            'curves': curves,
            'legend': plot.legend,
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
        act_autoy = menu.addAction("Y轴自适应")
        act_autoy.setCheckable(True)
        act_autoy.setChecked(self._chk_auto_y.isChecked())
        menu.addSeparator()
        act_pause = menu.addAction("暂停更新" if not self._paused else "继续更新")
        act_clear = menu.addAction("清空曲线")

        if key == 'current':
            menu.addSeparator()
            act_iq = menu.addAction("显示 Iq")
            act_iq.setCheckable(True)
            act_iq.setChecked(self._chk_iq.isChecked())
            act_ibus = menu.addAction("显示 Ibus")
            act_ibus.setCheckable(True)
            act_ibus.setChecked(self._chk_ibus.isChecked())
        else:
            act_iq = act_ibus = None

        act_legend = None
        if cfg.get('legend') is not None:
            menu.addSeparator()
            act_legend = menu.addAction("显示图例")
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
        act_autoy.toggled.connect(self._chk_auto_y.setChecked)
        act_pause.triggered.connect(lambda: self._btn_pause.click())
        act_clear.triggered.connect(self.clear)
        if act_iq is not None:
            act_iq.toggled.connect(self._chk_iq.setChecked)
        if act_ibus is not None:
            act_ibus.toggled.connect(self._chk_ibus.setChecked)
        if act_legend is not None:
            act_legend.toggled.connect(cfg['legend'].setVisible)
        return menu

    def _on_pause_toggled(self, checked: bool):
        self._paused = checked
        self._btn_pause.setText("继续" if checked else "暂停")
        self._btn_pause.setStyleSheet(
            "background-color: #F57C00; color: white; font-weight: bold;"
            if checked else "")

    def _on_window_changed(self, value: int):
        self._window_seconds = max(5, int(value))

    def _sync_toolbar_state(self):
        self._plots['current']['curves']['iq'].setVisible(self._chk_iq.isChecked())
        self._plots['current']['curves']['ibus'].setVisible(self._chk_ibus.isChecked())

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

    def clear(self):
        self._t0 = None
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
        self._buffers['iq'].append(t, getattr(fb, 'iq', 0.0))
        self._buffers['ibus'].append(t, getattr(fb, 'ibus', 0.0))
        multiturn = float(getattr(fb, 'multiturn', 0))
        single = float(getattr(fb, 'single', 0.0))
        self._buffers['angle'].append(t, multiturn * (2.0 * math.pi) + single)

    def _refresh(self, force=False):
        if pg is None or (self._paused and not force):
            return
        t_end = self._latest_time()
        if t_end is None:
            return
        t_min = max(0.0, t_end - float(self._window_seconds))

        self._update_curve('pos', t_min)
        self._update_curve('vel', t_min)
        self._update_curve('iq', t_min)
        self._update_curve('ibus', t_min)
        self._update_curve('angle', t_min)

        if self._chk_follow.isChecked() or force:
            for cfg in self._plots.values():
                cfg['plot'].setXRange(t_min, max(t_end, t_min + 0.1), padding=0.02)

        if self._chk_auto_y.isChecked():
            self._auto_range_y(t_min)

    def _latest_time(self):
        latest = None
        for buf in self._buffers.values():
            t = buf.last_time()
            if t is not None and (latest is None or t > latest):
                latest = t
        return latest

    def _update_curve(self, name: str, t_min: float):
        cfg = self._plots.get('current') if name in ('iq', 'ibus') else self._plots.get(name)
        if cfg is None:
            return
        buf = self._buffers[name]
        ts, ys = buf.arrays(t_min)
        cfg['curves'][name].setData(ts, ys)

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
            'current': _range_from(tuple(n for n, c in (('iq', self._chk_iq), ('ibus', self._chk_ibus)) if c.isChecked())),
            'angle': _range_from(('angle',)),
        }
        for key, rng in ranges.items():
            if rng is not None:
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
            iq = self._buffers['iq'].interpolate(x)
            ibus = self._buffers['ibus'].interpolate(x)
            parts = [f"<b>t</b> = {x:.2f} s"]
            if self._chk_iq.isChecked() and iq is not None:
                parts.append(f"Iq = {iq:.3f} A")
            if self._chk_ibus.isChecked() and ibus is not None:
                parts.append(f"Ibus = {ibus:.3f} A")
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
