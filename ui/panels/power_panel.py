"""功率监控面板: 母线电气量曲线 + 温度长时程监控曲线。

数据来源全部复用 feedback 统一数据流(JmClient.feedback_updated → 主窗口批量喂入),
无私有接收通道:
  - BUS 图: 遥测 0xCA BUS 组 / READ_BUS(0xC4) 应答, 按遥测实际频率刷新
  - TEMP 图: 面板周期请求 READ_TEMPERATURE(0xC5)(poll_temperature 信号 → 主窗口
    转发 client), 应答经 feedback_updated 流回; 遥测 TEMP 组数据同样被接受,
    统一按"配置采集周期"降采样(相邻点间隔 >= 周期/2 才入库), 保证存储点距
    与监控时长可预期, 长时程温度监控稳定。

后台运行(采集与绘图解耦):
  - 采集/入库不随 Tab 可见性启停: 温度周期请求与遥测数据入库始终运行,
    切到其他界面时数据照常累积到环形缓冲(切换期间曲线连续)。
  - 绘图仅在本面板可见时进行: 隐藏时停绘图定时器, 切回时一次性 force
    刷新, 把后台积累的数据全部绘出。

布局:
  顶部: 实时参数栏(Vbus/Ibus/Power/T_FET/T_Motor)
  中部: 母线图(电压/电流共左轴 + 功率右轴), 存储深度与显示窗口可配
  下部: 温度图(电机/FET), 采集周期(s)/存储深度/显示窗口可配

X 轴为相对时间(秒, 首个数据点为 0); 深度修改时重建环形缓冲并保留尾部旧数据。
"""

import time

import numpy as np

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QFrame, QGroupBox, QHBoxLayout, QLabel,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ui.theme import theme
from ui.panels.plot_panel import _SeriesBuffer

try:
    import pyqtgraph as pg
except Exception:  # pragma: no cover - 缺 pyqtgraph 时降级为纯数值显示
    pg = None

if pg is not None:
    try:
        pg.setConfigOptions(antialias=False, useOpenGL=True)
    except Exception:
        pg.setConfigOptions(antialias=False)


class PowerPanel(QGroupBox):
    """功率监控面板。"""

    poll_temperature = pyqtSignal()   # 周期温度采集请求(主窗口转发 client.query_temperature)

    _AUTO_RANGE_EVERY = 10             # Y 轴自适应节流帧数

    def __init__(self, parent=None):
        super().__init__("功率监控", parent)
        # ---- 配置项 ----
        self._bus_depth = 30000        # BUS 存储深度(点)
        self._temp_depth = 7200        # TEMP 存储深度(点): 1s 周期≈2h, 10s≈20h
        self._temp_period_s = 1        # TEMP 采集周期(秒)
        self._temp_enabled = True      # TEMP 周期采集开关
        self._bus_window_s = 60        # BUS 显示窗口(秒)
        self._temp_window_s = 3600     # TEMP 显示窗口(秒)
        self._paused = False
        self._follow = True
        self._auto_y = True

        # ---- 数据缓冲(X=相对时间秒) ----
        self._t0 = None                # 首个数据点的单调时钟基准
        self._last_feed_t = None       # 上一批数据的到达时刻(monotonic)
        self._last_temp_t = None       # 上一个入库温度点的相对时刻(降采样)
        self._buf_vbus = _SeriesBuffer(self._bus_depth)
        self._buf_ibus = _SeriesBuffer(self._bus_depth)
        self._buf_power = _SeriesBuffer(self._bus_depth)
        self._buf_temp_motor = _SeriesBuffer(self._temp_depth)
        self._buf_temp_fet = _SeriesBuffer(self._temp_depth)

        self._build()

        # 图表刷新定时器(仅绘图; 后台运行时数据照常入库, 隐藏即停绘)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(33)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start()
        self._auto_range_counter = 0

        # 温度周期请求定时器(后台采集: 不随面板可见性启停, 只看开关)
        self._temp_poll_timer = QTimer(self)
        self._temp_poll_timer.setSingleShot(False)
        self._temp_poll_timer.timeout.connect(self._on_temp_poll_tick)
        self._restart_temp_poll()

    # ==================== UI 构建 ====================
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(3)

        root.addWidget(self._build_rt_bar())
        root.addWidget(self._build_toolbar())
        root.addWidget(self._build_bus_bar())
        if pg is None:
            hint = QLabel("功率监控曲线需要 pyqtgraph, 当前环境缺少该依赖, 仅显示实时数值。")
            hint.setStyleSheet("color: #888; padding: 20px;")
            root.addWidget(hint)
            self._glw = None
            return

        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground(theme.hex('plot_bg'))
        self._glw.ci.layout.setContentsMargins(0, 0, 0, 0)
        self._glw.ci.layout.setSpacing(2)
        self._glw.ci.layout.setRowStretchFactor(0, 1)
        self._glw.ci.layout.setRowStretchFactor(1, 1)
        root.addWidget(self._glw, 1)
        self._build_plots()

        root.addWidget(self._build_temp_bar())

    def _build_rt_bar(self) -> QFrame:
        """顶部实时参数栏: 5 个当前值。"""
        self._rt_bar = QFrame()
        self._rt_bar.setObjectName("pwRtBar")
        lay = QHBoxLayout(self._rt_bar)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(12)
        self._rt_labels = {}
        items = (
            ("母线电压", "vbus", "V"),
            ("母线电流", "ibus", "A"),
            ("功率", "power", "W"),
            ("MOS温度", "temp_fet", "°C"),
            ("电机温度", "temp_motor", "°C"),
        )
        for name, key, unit in items:
            cap = QLabel(name)
            cap.setStyleSheet("border:none; color:%s; font-size:11px;" % theme.hex('muted'))
            val = QLabel("--")
            val.setStyleSheet(
                "border:none; color:%s; font-size:16px; font-weight:bold;" % theme.hex('value'))
            unit_lbl = QLabel(unit)
            unit_lbl.setStyleSheet("border:none; color:%s; font-size:11px;" % theme.hex('unit'))
            wrap = QWidget()
            wl = QHBoxLayout(wrap)
            wl.setContentsMargins(0, 0, 0, 0)
            wl.setSpacing(4)
            wl.addWidget(cap)
            wl.addWidget(val)
            wl.addWidget(unit_lbl)
            lay.addWidget(wrap)
            self._rt_labels[key] = (val, unit)
        lay.addStretch()
        return self._rt_bar

    def _build_toolbar(self) -> QWidget:
        """公共工具栏: 暂停/跟随/Y自适应/清空。"""
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._btn_pause = QPushButton("暂停")
        self._btn_pause.setCheckable(True)
        self._btn_pause.setFixedHeight(22)
        self._btn_pause.clicked.connect(self._on_pause_toggled)
        lay.addWidget(self._btn_pause)

        self._chk_follow = QCheckBox("跟随")
        self._chk_follow.setChecked(True)
        lay.addWidget(self._chk_follow)

        self._chk_auto_y = QCheckBox("Y自适应")
        self._chk_auto_y.setChecked(True)
        lay.addWidget(self._chk_auto_y)

        self._btn_clear = QPushButton("清空")
        self._btn_clear.setFixedHeight(22)
        self._btn_clear.clicked.connect(self.clear)
        lay.addWidget(self._btn_clear)

        lay.addStretch()
        return bar

    def _build_bus_bar(self) -> QWidget:
        """BUS 图配置行: 存储深度 + 显示窗口。"""
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        tag = QLabel("母线电气量:")
        tag.setStyleSheet("border:none; font-weight:bold;")
        lay.addWidget(tag)

        lay.addWidget(QLabel("存储深度(点):"))
        self._spin_bus_depth = QSpinBox()
        self._spin_bus_depth.setRange(1000, 600000)
        self._spin_bus_depth.setSingleStep(1000)
        self._spin_bus_depth.setValue(self._bus_depth)
        self._spin_bus_depth.setFixedWidth(90)
        self._spin_bus_depth.valueChanged.connect(self._on_bus_depth_changed)
        lay.addWidget(self._spin_bus_depth)

        lay.addWidget(QLabel("显示窗口(s):"))
        self._spin_bus_window = QSpinBox()
        self._spin_bus_window.setRange(5, 86400)
        self._spin_bus_window.setValue(self._bus_window_s)
        self._spin_bus_window.setFixedWidth(80)
        self._spin_bus_window.valueChanged.connect(self._on_bus_window_changed)
        lay.addWidget(self._spin_bus_window)

        lay.addStretch()
        return bar

    def _build_temp_bar(self) -> QWidget:
        """TEMP 图配置行: 采集开关/周期/存储深度/显示窗口。"""
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        tag = QLabel("温度监控:")
        tag.setStyleSheet("border:none; font-weight:bold;")
        lay.addWidget(tag)

        self._chk_temp_poll = QCheckBox("周期采集")
        self._chk_temp_poll.setChecked(self._temp_enabled)
        self._chk_temp_poll.toggled.connect(self._on_temp_enabled_toggled)
        lay.addWidget(self._chk_temp_poll)

        lay.addWidget(QLabel("采集周期(s):"))
        self._spin_temp_period = QSpinBox()
        self._spin_temp_period.setRange(1, 3600)
        self._spin_temp_period.setValue(self._temp_period_s)
        self._spin_temp_period.setFixedWidth(70)
        self._spin_temp_period.valueChanged.connect(self._on_temp_period_changed)
        lay.addWidget(self._spin_temp_period)

        lay.addWidget(QLabel("存储深度(点):"))
        self._spin_temp_depth = QSpinBox()
        self._spin_temp_depth.setRange(100, 600000)
        self._spin_temp_depth.setSingleStep(100)
        self._spin_temp_depth.setValue(self._temp_depth)
        self._spin_temp_depth.setFixedWidth(90)
        self._spin_temp_depth.valueChanged.connect(self._on_temp_depth_changed)
        lay.addWidget(self._spin_temp_depth)

        lay.addWidget(QLabel("显示窗口(s):"))
        self._spin_temp_window = QSpinBox()
        self._spin_temp_window.setRange(10, 604800)
        self._spin_temp_window.setValue(self._temp_window_s)
        self._spin_temp_window.setFixedWidth(80)
        self._spin_temp_window.valueChanged.connect(self._on_temp_window_changed)
        lay.addWidget(self._spin_temp_window)

        lay.addStretch()
        return bar

    def _build_plots(self):
        axis_pen = pg.mkPen(theme.hex('muted'))
        text_col = theme.c('text')

        # ---- BUS 图: 电压/电流共左轴, 功率独立右轴 ----
        self._bus_plot = self._glw.addPlot(row=0, col=0, title="母线电压 / 电流 / 功率")
        self._bus_plot.setTitle("母线电压 / 电流 / 功率", color=theme.hex('plot_title'), size='8pt')
        self._bus_plot.showGrid(x=True, y=True, alpha=0.25)
        self._bus_plot.setLabel('left', "电压 V / 电流 A")
        self._bus_plot.setLabel('right', "功率", units="W")
        self._bus_plot.setLabel('bottom', '时间 (s)')
        self._bus_plot.addLegend(offset=(4, 4))
        self._bus_plot.setClipToView(True)
        self._bus_plot.setDownsampling(auto=True, mode='peak')
        self._bus_plot.setMenuEnabled(False)

        pen_vbus = pg.mkPen(theme.hex('plot_vbus'), width=2)
        pen_ibus = pg.mkPen(theme.hex('plot_ibus'), width=2, style=Qt.PenStyle.DashLine)
        pen_power = pg.mkPen(theme.hex('plot_power'), width=2)
        self._curve_vbus = self._bus_plot.plot([], [], pen=pen_vbus, name="Vbus (V)")
        self._curve_ibus = self._bus_plot.plot([], [], pen=pen_ibus, name="Ibus (A)")

        # 右轴 ViewBox(功率): pyqtgraph 双轴样板
        self._bus_vb2 = pg.ViewBox()
        self._bus_plot.addItem(self._bus_vb2, ignoreBounds=True)
        self._bus_plot.getAxis('right').linkToView(self._bus_vb2)
        self._bus_vb2.setXLink(self._bus_plot)
        self._bus_vb2.setMenuEnabled(False)
        self._curve_power = pg.PlotDataItem(pen=pen_power, name="Power (W)")
        self._bus_vb2.addItem(self._curve_power)
        if self._bus_plot.legend is not None:
            self._bus_plot.legend.addItem(self._curve_power, "Power (W)")
        self._bus_plot.vb.sigResized.connect(self._sync_bus_vb2_geometry)

        # ---- TEMP 图: 电机/MOS 温度同轴(℃) ----
        self._temp_plot = self._glw.addPlot(row=1, col=0, title="温度监控")
        self._temp_plot.setTitle("温度监控", color=theme.hex('plot_title'), size='8pt')
        self._temp_plot.showGrid(x=True, y=True, alpha=0.25)
        self._temp_plot.setLabel('left', "温度", units="°C")
        self._temp_plot.setLabel('bottom', '时间 (s)')
        self._temp_plot.addLegend(offset=(4, 4))
        self._temp_plot.setClipToView(True)
        self._temp_plot.setDownsampling(auto=True, mode='peak')
        self._temp_plot.setMenuEnabled(False)
        pen_tm = pg.mkPen(theme.hex('plot_temp_motor'), width=2)
        pen_tf = pg.mkPen(theme.hex('plot_temp_fet'), width=2)
        self._curve_temp_motor = self._temp_plot.plot(
            [], [], pen=pen_tm, name="电机温度 (°C)")
        self._curve_temp_fet = self._temp_plot.plot(
            [], [], pen=pen_tf, name="MOS温度 (°C)")

        for plot in (self._bus_plot, self._temp_plot):
            for ax_name in ('left', 'right', 'bottom'):
                ax = plot.getAxis(ax_name)
                ax.setPen(axis_pen)
                ax.setTextPen(text_col)

    def _sync_bus_vb2_geometry(self):
        """右轴 ViewBox 几何跟随主 ViewBox(双轴样板)。"""
        self._bus_vb2.setGeometry(self._bus_plot.vb.sceneBoundingRect())

    # ==================== 配置变更 ====================
    @staticmethod
    def _resize_buffer(old: _SeriesBuffer, new_len: int) -> _SeriesBuffer:
        """按新深度重建环形缓冲, 保留尾部旧数据。"""
        nb = _SeriesBuffer(new_len)
        ts, ys = old.arrays()
        if len(ts):
            take = min(len(ts), new_len)
            nb.append_batch(ts[-take:], ys[-take:])
        return nb

    def _on_bus_depth_changed(self, value: int):
        self._bus_depth = int(value)
        self._buf_vbus = self._resize_buffer(self._buf_vbus, self._bus_depth)
        self._buf_ibus = self._resize_buffer(self._buf_ibus, self._bus_depth)
        self._buf_power = self._resize_buffer(self._buf_power, self._bus_depth)

    def _on_temp_depth_changed(self, value: int):
        self._temp_depth = int(value)
        self._buf_temp_motor = self._resize_buffer(self._buf_temp_motor, self._temp_depth)
        self._buf_temp_fet = self._resize_buffer(self._buf_temp_fet, self._temp_depth)

    def _on_bus_window_changed(self, value: int):
        self._bus_window_s = int(value)
        self._refresh(force=True)

    def _on_temp_window_changed(self, value: int):
        self._temp_window_s = int(value)
        self._refresh(force=True)

    def _on_temp_period_changed(self, value: int):
        self._temp_period_s = int(value)
        self._restart_temp_poll()

    def _on_temp_enabled_toggled(self, checked: bool):
        self._temp_enabled = bool(checked)
        self._spin_temp_period.setEnabled(not checked)
        self._restart_temp_poll()

    def _restart_temp_poll(self):
        """温度周期采集启停: 只看采集开关, 与面板可见性无关(后台运行)。"""
        self._temp_poll_timer.stop()
        if self._temp_enabled:
            self._temp_poll_timer.start(self._temp_period_s * 1000)

    def _on_temp_poll_tick(self):
        if self._temp_enabled:
            self.poll_temperature.emit()

    def showEvent(self, evt):
        """切回本面板: 重启绘图定时器并 force 刷新, 绘出后台积累的数据。"""
        super().showEvent(evt)
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()
        self._refresh(force=True)

    def hideEvent(self, evt):
        """切走面板: 只停绘图, 采集与数据入库继续(后台运行)。"""
        super().hideEvent(evt)
        self._refresh_timer.stop()

    # ==================== 数据入口 ====================
    def feed_batch(self, fb_list):
        """主窗口 UI tick 批量喂入 feedback 数据(遥测帧 + 读命令应答混合)。

        时间戳: 取批内均分(上一批结束 ~ 本批结束), 遥测高频帧得到与帧距
        近似一致的 X 坐标; filled_fields 区分本帧携带的数据组。
        """
        n = len(fb_list)
        if n == 0:
            return
        t_end = time.monotonic()
        t_start = self._last_feed_t if self._last_feed_t is not None else t_end - 0.02 * (n - 1)
        self._last_feed_t = t_end
        step = (t_end - t_start) / n
        if step <= 0:
            step = 0.001

        for i, fb in enumerate(fb_list):
            t_monotonic = t_start + step * (i + 1)
            if self._t0 is None:
                self._t0 = t_monotonic
            t = t_monotonic - self._t0
            filled = getattr(fb, 'filled_fields', ())

            if 'vbus' in filled or 'ibus' in filled or 'power' in filled:
                self._append_bus(t,
                                 fb.vbus if 'vbus' in filled else float('nan'),
                                 fb.ibus if 'ibus' in filled else float('nan'),
                                 fb.power if 'power' in filled else float('nan'))
                bus_dirty = True
                if 'vbus' in filled:
                    self._update_rt('vbus', fb.vbus, "{:.2f}")
                if 'ibus' in filled:
                    self._update_rt('ibus', fb.ibus, "{:.3f}")
                if 'power' in filled:
                    self._update_rt('power', fb.power, "{:.2f}")

            if 'temp_fet' in filled or 'temp_motor' in filled:
                # 降采样: 与上一入库点间隔 >= 采集周期/2 才入库, 统一存储节拍
                if self._last_temp_t is None or (t - self._last_temp_t) >= self._temp_period_s * 0.5:
                    self._last_temp_t = t
                    self._buf_temp_motor.append(
                        t, fb.temp_motor if 'temp_motor' in filled else float('nan'))
                    self._buf_temp_fet.append(
                        t, fb.temp_fet if 'temp_fet' in filled else float('nan'))
                if 'temp_fet' in filled:
                    self._update_rt('temp_fet', fb.temp_fet, "{:.1f}")
                if 'temp_motor' in filled:
                    self._update_rt('temp_motor', fb.temp_motor, "{:.1f}")

    def _append_bus(self, t: float, vbus: float, ibus: float, power: float):
        self._buf_vbus.append(t, vbus)
        self._buf_ibus.append(t, ibus)
        self._buf_power.append(t, power)

    def _update_rt(self, key: str, value, fmt: str):
        entry = self._rt_labels.get(key)
        if entry is None:
            return
        try:
            entry[0].setText(fmt.format(float(value)))
        except (TypeError, ValueError):
            pass

    def clear(self):
        """清空全部缓冲与实时数值(X 轴时间基准重置)。"""
        self._t0 = None
        self._last_feed_t = None
        self._last_temp_t = None
        for buf in (self._buf_vbus, self._buf_ibus, self._buf_power,
                    self._buf_temp_motor, self._buf_temp_fet):
            buf.clear()
        for key, entry in self._rt_labels.items():
            entry[0].setText("--")
        if pg is None:
            return
        for curve in (self._curve_vbus, self._curve_ibus, self._curve_power):
            curve.setData([], [])
        self._curve_temp_motor.setData([], [])
        self._curve_temp_fet.setData([], [])
        self._refresh(force=True)

    # ==================== 刷新 ====================
    def _any_dirty(self) -> bool:
        return (self._buf_vbus.dirty or self._buf_ibus.dirty or self._buf_power.dirty
                or self._buf_temp_motor.dirty or self._buf_temp_fet.dirty)

    def _on_pause_toggled(self, checked: bool):
        self._paused = checked
        self._btn_pause.setText("继续" if checked else "暂停")
        self._btn_pause.setStyleSheet(
            "background-color: #F57C00; color: white; font-weight: bold;"
            if checked else "")

    def _set_data(self, curve, buf, t_min: float, t_max: float):
        ts, ys = buf.arrays(t_min if t_min > 0 else None)
        if len(ts) == 0:
            return
        try:
            curve.setData(ts, ys, avoidCopy=True)
        except TypeError:
            curve.setData(ts, ys)

    def _refresh(self, force: bool = False):
        # 可见性兜底: 后台运行时数据只入库不绘图(timer 已停, 此处防初始未激活等场景)
        if not self.isVisible():
            return
        if pg is None or (self._paused and not force):
            return
        if not self._any_dirty() and not force:
            return
        self._auto_range_counter += 1
        do_auto_y = self._auto_y and (self._auto_range_counter >= self._AUTO_RANGE_EVERY or force)
        if do_auto_y:
            self._auto_range_counter = 0

        # BUS 图: 窗口 = 最新时刻向前 _bus_window_s 秒
        t_bus_end = self._buf_vbus.last_time()
        if t_bus_end is not None:
            t_bus_start = max(0.0, t_bus_end - float(self._bus_window_s))
            self._set_data(self._curve_vbus, self._buf_vbus, t_bus_start, t_bus_end)
            self._set_data(self._curve_ibus, self._buf_ibus, t_bus_start, t_bus_end)
            self._set_data(self._curve_power, self._buf_power, t_bus_start, t_bus_end)
            if self._follow or force:
                self._bus_plot.setXRange(t_bus_start, max(t_bus_end, t_bus_start + 1.0),
                                         padding=0.02)
                self._sync_bus_vb2_geometry()
            if do_auto_y:
                self._auto_range_y_bus(t_bus_start)
        for buf in (self._buf_vbus, self._buf_ibus, self._buf_power):
            buf.dirty = False

        # TEMP 图
        t_temp_end = self._buf_temp_motor.last_time()
        if t_temp_end is not None:
            t_temp_start = max(0.0, t_temp_end - float(self._temp_window_s))
            self._set_data(self._curve_temp_motor, self._buf_temp_motor,
                           t_temp_start, t_temp_end)
            self._set_data(self._curve_temp_fet, self._buf_temp_fet,
                           t_temp_start, t_temp_end)
            if self._follow or force:
                self._temp_plot.setXRange(t_temp_start, max(t_temp_end, t_temp_start + 1.0),
                                          padding=0.02)
            if do_auto_y:
                self._auto_range_y_temp(t_temp_start)
        self._buf_temp_motor.dirty = False
        self._buf_temp_fet.dirty = False

    @staticmethod
    def _y_range(buf, t_min: float):
        _ts, ys = buf.arrays(t_min if t_min > 0 else None)
        if not len(ys):
            return None
        finite = ys[~np.isnan(ys)]
        if not len(finite):
            return None
        y0, y1 = float(finite.min()), float(finite.max())
        if y0 == y1:
            pad = max(1.0, abs(y0) * 0.1)
        else:
            pad = (y1 - y0) * 0.15
        return y0 - pad, y1 + pad

    def _auto_range_y_bus(self, t_start: float):
        r_left = None
        for buf in (self._buf_vbus, self._buf_ibus):
            r = self._y_range(buf, t_start)
            if r is None:
                continue
            r_left = r if r_left is None else (min(r_left[0], r[0]), max(r_left[1], r[1]))
        if r_left is not None:
            self._bus_plot.setYRange(r_left[0], r_left[1], padding=0.02)
        r_power = self._y_range(self._buf_power, t_start)
        if r_power is not None:
            self._bus_vb2.setYRange(r_power[0], r_power[1], padding=0.02)

    def _auto_range_y_temp(self, t_start: float):
        r = None
        for buf in (self._buf_temp_motor, self._buf_temp_fet):
            ri = self._y_range(buf, t_start)
            if ri is None:
                continue
            r = ri if r is None else (min(r[0], ri[0]), max(r[1], ri[1]))
        if r is not None:
            self._temp_plot.setYRange(r[0], r[1], padding=0.02)

    # ==================== 主题/持久化 ====================
    def apply_theme(self):
        """主题切换: 实时参数栏/图背景/轴色重设。"""
        if hasattr(self, "_rt_bar"):
            self._rt_bar.setStyleSheet(
                "QFrame#pwRtBar { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,"
                f" stop:0 {theme.hex('card_top')}, stop:1 {theme.hex('card_bottom')});"
                f" border:1px solid {theme.hex('border')}; border-radius:6px; }}")
        for key, entry in getattr(self, '_rt_labels', {}).items():
            color = theme.hex('value_hot') if key in ('temp_fet', 'temp_motor') \
                else theme.hex('value')
            entry[0].setStyleSheet(
                "border:none; color:%s; font-size:16px; font-weight:bold;" % color)
        if pg is None or self._glw is None:
            return
        self._glw.setBackground(theme.hex('plot_bg'))
        axis_pen = pg.mkPen(theme.hex('muted'))
        text_col = theme.c('text')
        for plot in (self._bus_plot, self._temp_plot):
            for ax_name in ('left', 'right', 'bottom'):
                ax = plot.getAxis(ax_name)
                ax.setPen(axis_pen)
                ax.setTextPen(text_col)
        self._curve_vbus.setPen(pg.mkPen(theme.hex('plot_vbus'), width=2))
        self._curve_ibus.setPen(
            pg.mkPen(theme.hex('plot_ibus'), width=2, style=Qt.PenStyle.DashLine))
        self._curve_power.setPen(pg.mkPen(theme.hex('plot_power'), width=2))
        self._curve_temp_motor.setPen(pg.mkPen(theme.hex('plot_temp_motor'), width=2))
        self._curve_temp_fet.setPen(pg.mkPen(theme.hex('plot_temp_fet'), width=2))
        self._refresh(force=True)

    def get_opts(self) -> dict:
        return {
            "bus_depth": int(self._bus_depth),
            "bus_window_s": int(self._bus_window_s),
            "temp_enabled": bool(self._temp_enabled),
            "temp_period_s": int(self._temp_period_s),
            "temp_depth": int(self._temp_depth),
            "temp_window_s": int(self._temp_window_s),
            "follow": bool(self._follow),
            "auto_y": bool(self._auto_y),
        }

    def set_opts(self, opts: dict):
        if not isinstance(opts, dict):
            return
        try:
            v = int(opts.get("bus_depth", self._bus_depth))
            if 1000 <= v <= 600000:
                self._spin_bus_depth.setValue(v)
        except Exception:
            pass
        try:
            v = int(opts.get("bus_window_s", self._bus_window_s))
            if 5 <= v <= 86400:
                self._spin_bus_window.setValue(v)
        except Exception:
            pass
        try:
            self._chk_temp_poll.setChecked(bool(opts.get("temp_enabled", True)))
        except Exception:
            pass
        try:
            v = int(opts.get("temp_period_s", self._temp_period_s))
            if 1 <= v <= 3600:
                self._spin_temp_period.setValue(v)
        except Exception:
            pass
        try:
            v = int(opts.get("temp_depth", self._temp_depth))
            if 100 <= v <= 600000:
                self._spin_temp_depth.setValue(v)
        except Exception:
            pass
        try:
            v = int(opts.get("temp_window_s", self._temp_window_s))
            if 10 <= v <= 604800:
                self._spin_temp_window.setValue(v)
        except Exception:
            pass
        try:
            self._follow = bool(opts.get("follow", True))
            self._chk_follow.setChecked(self._follow)
        except Exception:
            pass
        try:
            self._auto_y = bool(opts.get("auto_y", True))
            self._chk_auto_y.setChecked(self._auto_y)
        except Exception:
            pass
