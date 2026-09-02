"""功率监控面板: 母线电气量曲线 + 温度长时程监控曲线。

数据来源全部复用 feedback 统一数据流(JmClient.feedback_updated → 主窗口批量喂入),
无私有接收通道:
  - BUS 图: 遥测 0xCA BUS 组 / READ_BUS(0xC4) 应答, 按遥测实际频率刷新
  - TEMP 图: 面板周期请求 READ_TEMPERATURE(0xC5)(poll_temperature 信号 → 主窗口
    转发 client, 周期 0.1~3600s 可配), 应答经 feedback_updated 流回; 遥测 TEMP 组
    数据同样被接受, 统一按"配置采集周期"降采样(相邻点间隔 >= 周期/2 才入库),
    保证存储点距与监控时长可预期, 长时程温度监控稳定。

后台运行(采集与绘图解耦):
  - 采集/入库不随 Tab 可见性启停: 温度周期请求与遥测数据入库始终运行,
    切到其他界面时数据照常累积到环形缓冲(切换期间曲线连续)。
  - 绘图仅在本面板可见时进行: 隐藏时停绘图定时器, 切回时一次性 force
    刷新, 把后台积累的数据全部绘出。

布局:
  顶部: 实时参数栏(Vbus/Ibus/Power/T_FET/T_Motor)
  中部: 母线图(电压/电流共左轴 + 功率右轴), 配置行 = 左侧参数 + 右侧视图控件
  下部: 温度图(电机/FET), 配置行 = 左侧参数 + 右侧视图控件

每图独立一套视图控件(与配置同行, 右对齐等宽):
  暂停 / 全局 / 跟随 / Y自适应(BUS 图另有 功率Y自适应), 两图互不影响,
  BUS 图(默认 60s 窗口)与 TEMP 图(默认 3600s 窗口)周期不同各自独立。

交互(缩放不被自动刷新覆盖):
  - 跟随 = 右端固定最新时刻, 左端随显示窗口向前滑动。
  - 全局 = X 轴扩展到全部存储数据(整条曲线, 长度由存储深度决定),
    再点一次恢复跟随; 手动缩放自动退出全局。
  - 拖拽平移 / 滚轮缩放作用于左轴(电压/电流)与 X 轴(含上下拖动 Y):
    X 被手动改动自动停"跟随", 左 Y 被改动自动停"Y自适应"(按钮同步弹起);
    重新按下或修改"显示窗口"恢复。
  - 功率右轴: 鼠标置于轴上滚轮独立缩放 / 上下拖拽平移(仅功率 Y, 不动主视图),
    手动操作自动停"功率Y自适应"。
  - 手动缩放/平移到显示窗口外时, 数据按可视范围加载(不只喂固定窗口)。

X 轴为相对时间(秒, 首个数据点为 0); 深度修改时重建环形缓冲并保留尾部旧数据。
"""

import time

import numpy as np

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFrame, QGroupBox, QHBoxLayout, QLabel,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ui.theme import theme
from ui.panels.plot_panel import _SeriesBuffer

try:
    import pyqtgraph as pg
except Exception:  # pragma: no cover - 缺 pyqtgraph 时降级为纯数值显示
    pg = None

if pg is not None:
    # 不启用 OpenGL: GL 渲染与功率图的双 ViewBox(电压/电流主视图 + 功率右轴)不兼容,
    # 功率曲线无法绘出; 本图数据量(clipToView + 降采样)软件渲染足够
    pg.setConfigOptions(antialias=False)

    class _PowerViewBox(pg.ViewBox):
        """功率右轴 ViewBox(几何=主视图绘图区): 滚轮/拖拽全部转发主视图,
        使主视图(电压/电流左轴 + X 轴)可正常缩放平移。

        ViewBox.mouseDragEvent 开头即 ev.accept(), 会吞掉覆盖在主视图顶层的
        本 ViewBox 的拖拽事件, 导致主视图无法平移 —— 必须覆盖为 ignore 穿透。
        功率轴的独立缩放/平移由 _PowerAxisItem 在轴区域处理(见下)。
        """

        def __init__(self, host: 'pg.ViewBox', parent=None):
            super().__init__(parent)
            self._host = host

        def wheelEvent(self, ev, axis=None):
            # 滚轮直接转发主视图(X+Y 缩放), 不依赖 Qt 父链事件传播
            self._host.wheelEvent(ev, axis)

        def mouseDragEvent(self, ev, axis=None):
            # 拖拽穿透主视图(上下/左右平移)
            ev.ignore()

    class _PowerAxisItem(pg.AxisItem):
        """功率右轴: 鼠标置于轴上时滚轮独立缩放 / 上下拖拽平移(仅功率 Y 轴),
        手动操作广播 sigRangeChangedManually 使"功率Y自适应"自动退出。

        轴区域不属于任何 ViewBox 的几何范围(事件默认无人接收), 须由轴自身处理。
        """

        def __init__(self, parent=None):
            super().__init__(orientation='right', parent=parent)
            self._vb = None   # 绑定的功率 ViewBox(bind 后生效)

        def bind(self, vb: 'pg.ViewBox'):
            self._vb = vb

        def wheelEvent(self, ev, axis=None):
            if self._vb is None:
                ev.ignore()
                return
            ev.accept()
            s = 1.02 ** (ev.delta() * -1.0 / 8)   # 与 ViewBox 默认滚轮系数一致
            y0, y1 = self._vb.viewRange()[1]
            my = self._vb.mapSceneToView(ev.scenePos()).y()
            self._vb.setYRange(my - (my - y0) * s, my + (y1 - my) * s, padding=0)
            self._vb.sigRangeChangedManually.emit([False, True])

        def mouseDragEvent(self, ev, axis=None):
            if self._vb is None:
                ev.ignore()
                return
            ev.accept()
            if ev.button() not in (Qt.MouseButton.LeftButton,
                                   Qt.MouseButton.MiddleButton):
                return
            dif = ev.pos() - ev.lastPos()          # 像素位移(轴无旋转变换)
            h = self._vb.geometry().height()
            if h <= 0:
                return
            y0, y1 = self._vb.viewRange()[1]
            # 场景 Y 向下为正: 向下拖(dif.y>0)曲线跟随下移 = 可视范围上移(dy>0)
            dy = dif.y() * (y1 - y0) / h            # 像素 → 数据单位
            self._vb.setYRange(y0 + dy, y1 + dy, padding=0)
            self._vb.sigRangeChangedManually.emit([False, True])


class PowerPanel(QGroupBox):
    """功率监控面板。"""

    poll_temperature = pyqtSignal()   # 周期温度采集请求(主窗口转发 client.query_temperature)

    _AUTO_RANGE_EVERY = 10             # Y 轴自适应节流帧数
    _BTN_W = 84                        # 视图按钮统一宽度(px)
    _STYLE_ON = "background-color: #37474F; color: white; font-weight: bold;"
    _STYLE_PAUSE = "background-color: #F57C00; color: white; font-weight: bold;"
    _STYLE_GLOBAL = "background-color: #1565C0; color: white; font-weight: bold;"

    def __init__(self, parent=None):
        super().__init__("功率监控", parent)
        # ---- 配置项 ----
        self._bus_depth = 30000        # BUS 存储深度(点)
        self._temp_depth = 7200        # TEMP 存储深度(点): 0.1s 周期≈12min, 1s≈2h, 10s≈20h
        self._temp_period_s = 1        # TEMP 采集周期(秒, 最小 0.1)
        self._temp_enabled = True      # TEMP 周期采集开关
        self._bus_window_s = 60        # BUS 显示窗口(秒)
        self._temp_window_s = 3600     # TEMP 显示窗口(秒)

        # ---- BUS 图视图状态(独立于 TEMP 图) ----
        self._bus_paused = False
        self._bus_follow = True
        self._bus_global = False       # 全局显示: X 轴覆盖全部存储数据
        self._bus_auto_y = True        # 左轴(电压/电流)Y 自适应
        self._bus_auto_power_y = True  # 功率右轴 Y 自适应

        # ---- TEMP 图视图状态(独立于 BUS 图) ----
        self._temp_paused = False
        self._temp_follow = True
        self._temp_global = False
        self._temp_auto_y = True

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
        self._side_range_counter = 0   # 功率右轴 Y 自适应节流
        # 程序最近一次设定的视图范围: 手动缩放检测时区分被改动的轴
        self._bus_set_x = None
        self._bus_set_y = None
        self._temp_set_x = None
        self._temp_set_y = None

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
        root.addWidget(self._build_bus_bar())
        if pg is None:
            hint = QLabel("功率监控曲线需要 pyqtgraph, 当前环境缺少该依赖, 仅显示实时数值。")
            hint.setStyleSheet("color: #888; padding: 20px;")
            root.addWidget(hint)
            self._glw = None
            return

        self._glw = pg.GraphicsLayoutWidget()
        # GL 渲染与双 ViewBox 不兼容(功率曲线不显示), 本图强制软件渲染
        self._glw.useOpenGL(False)
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

    def _view_button(self, text: str, tooltip: str = "") -> QPushButton:
        """视图控件按钮: 统一宽度/可勾选。"""
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setFixedHeight(22)
        btn.setFixedWidth(self._BTN_W)
        if tooltip:
            btn.setToolTip(tooltip)
        return btn

    def _build_bus_bar(self) -> QWidget:
        """BUS 图配置行: 左侧参数 + 右侧视图控件(右对齐等宽)。"""
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

        self._btn_bus_pause = self._view_button("暂停")
        self._btn_bus_pause.toggled.connect(self._on_bus_pause_toggled)
        lay.addWidget(self._btn_bus_pause)

        self._btn_bus_global = self._view_button("全局", "显示全部存储数据(整条曲线), 再点一次恢复跟随")
        self._btn_bus_global.toggled.connect(self._on_bus_global_toggled)
        lay.addWidget(self._btn_bus_global)

        self._btn_bus_follow = self._view_button("跟随", "右端固定最新时刻, 左端随显示窗口滑动")
        self._btn_bus_follow.setChecked(True)
        self._btn_bus_follow.setStyleSheet(self._STYLE_ON)
        self._btn_bus_follow.toggled.connect(self._on_bus_follow_toggled)
        lay.addWidget(self._btn_bus_follow)

        self._btn_bus_auto_y = self._view_button("Y自适应", "电压/电流左轴自动适配可视区")
        self._btn_bus_auto_y.setChecked(True)
        self._btn_bus_auto_y.setStyleSheet(self._STYLE_ON)
        self._btn_bus_auto_y.toggled.connect(
            lambda on: self._set_bus_auto_y(on, style=True))
        lay.addWidget(self._btn_bus_auto_y)

        self._btn_bus_auto_power_y = self._view_button(
            "功率Y自适应", "功率右轴自动适配可视区(在功率轴上滚轮缩放/拖拽平移后自动停)")
        self._btn_bus_auto_power_y.setChecked(True)
        self._btn_bus_auto_power_y.setStyleSheet(self._STYLE_ON)
        self._btn_bus_auto_power_y.toggled.connect(
            lambda on: self._set_bus_auto_power_y(on, style=True))
        lay.addWidget(self._btn_bus_auto_power_y)
        return bar

    def _build_temp_bar(self) -> QWidget:
        """TEMP 图配置行: 左侧参数 + 右侧视图控件(右对齐等宽)。"""
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
        self._spin_temp_period = QDoubleSpinBox()
        self._spin_temp_period.setRange(0.1, 3600)
        self._spin_temp_period.setDecimals(1)
        self._spin_temp_period.setSingleStep(0.1)
        self._spin_temp_period.setValue(self._temp_period_s)
        self._spin_temp_period.setFixedWidth(70)
        self._spin_temp_period.setToolTip(
            "温度采集与入库节拍(0.1~3600s); 遥测 TEMP 数据同样按此周期降采样")
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

        self._btn_temp_pause = self._view_button("暂停")
        self._btn_temp_pause.toggled.connect(self._on_temp_pause_toggled)
        lay.addWidget(self._btn_temp_pause)

        self._btn_temp_global = self._view_button("全局", "显示全部存储数据(整条曲线), 再点一次恢复跟随")
        self._btn_temp_global.toggled.connect(self._on_temp_global_toggled)
        lay.addWidget(self._btn_temp_global)

        self._btn_temp_follow = self._view_button("跟随", "右端固定最新时刻, 左端随显示窗口滑动")
        self._btn_temp_follow.setChecked(True)
        self._btn_temp_follow.setStyleSheet(self._STYLE_ON)
        self._btn_temp_follow.toggled.connect(self._on_temp_follow_toggled)
        lay.addWidget(self._btn_temp_follow)

        self._btn_temp_auto_y = self._view_button("Y自适应", "温度轴自动适配可视区")
        self._btn_temp_auto_y.setChecked(True)
        self._btn_temp_auto_y.setStyleSheet(self._STYLE_ON)
        self._btn_temp_auto_y.toggled.connect(
            lambda on: self._set_temp_auto_y(on, style=True))
        lay.addWidget(self._btn_temp_auto_y)
        return bar

    def _build_plots(self):
        axis_pen = pg.mkPen(theme.hex('muted'))
        text_col = theme.c('text')

        # ---- BUS 图: 电压/电流共左轴, 功率独立右轴(pyqtgraph 双轴样板) ----
        # 右轴用自定义 _PowerAxisItem: 轴上滚轮/拖拽独立操作功率 Y
        self._power_axis = _PowerAxisItem()
        self._bus_plot = self._glw.addPlot(
            row=0, col=0, title="母线电压 / 电流 / 功率",
            axisItems={'right': self._power_axis})
        self._bus_plot.setTitle("母线电压 / 电流 / 功率", color=theme.hex('plot_title'), size='8pt')
        self._bus_plot.showGrid(x=True, y=True, alpha=0.25)
        self._bus_plot.addLegend(offset=(4, 4))
        self._bus_plot.setClipToView(True)
        self._bus_plot.setDownsampling(auto=True, mode='peak')
        self._bus_plot.setMenuEnabled(False)

        pen_vbus = pg.mkPen(theme.hex('plot_vbus'), width=2)
        pen_ibus = pg.mkPen(theme.hex('plot_ibus'), width=2, style=Qt.PenStyle.DashLine)
        pen_power = pg.mkPen(theme.hex('plot_power'), width=2)
        self._curve_vbus = self._bus_plot.plot([], [], pen=pen_vbus, name="Vbus (V)")
        self._curve_ibus = self._bus_plot.plot([], [], pen=pen_ibus, name="Ibus (A)")

        # 功率右轴: 绘图区拖拽/滚轮转发主视图, 轴上独立缩放/平移功率 Y
        # vb_power 直接加入 scene(官方双轴样板): 若 addItem 到 plot, parent 会是
        # 主视图 childGroup(带 view-range 缩放变换), setGeometry 的场景坐标被
        # 二次变换, 曲线映射与右轴刻度错位; scene 顶层本地坐标=场景坐标, 无此问题
        self._vb_power = _PowerViewBox(self._bus_plot.vb)
        self._bus_plot.scene().addItem(self._vb_power)
        self._bus_plot.getAxis('right').linkToView(self._vb_power)
        self._vb_power.setXLink(self._bus_plot)
        self._vb_power.setMenuEnabled(False)
        self._vb_power.setMouseEnabled(False, False)
        self._power_axis.bind(self._vb_power)
        self._curve_power = pg.PlotDataItem(pen=pen_power, name="Power (W)")
        self._vb_power.addItem(self._curve_power)
        if self._bus_plot.legend is not None:
            self._bus_plot.legend.addItem(self._curve_power, "Power (W)")
        self._bus_plot.vb.sigResized.connect(self._sync_bus_vb_geometry)

        # ---- TEMP 图: 电机/MOS 温度同轴(℃) ----
        self._temp_plot = self._glw.addPlot(row=1, col=0, title="温度监控")
        self._temp_plot.setTitle("温度监控", color=theme.hex('plot_title'), size='8pt')
        self._temp_plot.showGrid(x=True, y=True, alpha=0.25)
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

        self._apply_axis_labels()
        for plot in (self._bus_plot, self._temp_plot):
            for ax_name in ('left', 'right', 'bottom'):
                ax = plot.getAxis(ax_name)
                ax.setPen(axis_pen)
                ax.setTextPen(text_col)

        # 手动缩放/拖拽检测: 被改动的轴自动退出对应图的自动模式
        self._bus_plot.vb.sigRangeChangedManually.connect(
            lambda *_: self._on_manual_range_bus())
        self._temp_plot.vb.sigRangeChangedManually.connect(
            lambda *_: self._on_manual_range_temp())
        self._vb_power.sigRangeChangedManually.connect(
            lambda *_: self._set_bus_auto_power_y(False))

    def _apply_axis_labels(self):
        """轴标签设置(功率右轴用对应曲线颜色标注), 主题切换时重设。"""
        self._bus_plot.setLabel('left', "电压 V / 电流 A")
        self._bus_plot.setLabel('right', "功率", units="W", color=theme.hex('plot_power'))
        self._bus_plot.setLabel('bottom', '时间 (s)')
        self._temp_plot.setLabel('left', "温度", units="°C")
        self._temp_plot.setLabel('bottom', '时间 (s)')

    def _sync_bus_vb_geometry(self):
        """功率右轴 ViewBox 几何跟随主 ViewBox(双轴样板)。"""
        self._vb_power.setGeometry(self._bus_plot.vb.sceneBoundingRect())

    # ==================== 视图模式(每图独立: 暂停/全局/跟随/Y自适应) ====================
    def _on_bus_pause_toggled(self, checked: bool):
        self._bus_paused = bool(checked)
        self._btn_bus_pause.setText("继续" if checked else "暂停")
        self._btn_bus_pause.setStyleSheet(self._STYLE_PAUSE if checked else "")
        if not checked:
            self._refresh(force=True)

    def _on_temp_pause_toggled(self, checked: bool):
        self._temp_paused = bool(checked)
        self._btn_temp_pause.setText("继续" if checked else "暂停")
        self._btn_temp_pause.setStyleSheet(self._STYLE_PAUSE if checked else "")
        if not checked:
            self._refresh(force=True)

    def _on_bus_global_toggled(self, checked: bool):
        """BUS 全局显示: X 轴扩展到全部存储数据, 取消后恢复跟随。"""
        self._bus_global = bool(checked)
        self._btn_bus_global.setStyleSheet(self._STYLE_GLOBAL if checked else "")
        self._set_bus_follow(not checked)
        self._refresh(force=True)

    def _on_temp_global_toggled(self, checked: bool):
        """TEMP 全局显示: X 轴扩展到全部存储数据, 取消后恢复跟随。"""
        self._temp_global = bool(checked)
        self._btn_temp_global.setStyleSheet(self._STYLE_GLOBAL if checked else "")
        self._set_temp_follow(not checked)
        self._refresh(force=True)

    def _on_bus_follow_toggled(self, checked: bool):
        """用户操作 BUS"跟随"按钮: 按下时退出全局。"""
        self._bus_follow = bool(checked)
        self._btn_bus_follow.setStyleSheet(self._STYLE_ON if checked else "")
        if checked:
            self._exit_bus_global()

    def _on_temp_follow_toggled(self, checked: bool):
        """用户操作 TEMP"跟随"按钮: 按下时退出全局。"""
        self._temp_follow = bool(checked)
        self._btn_temp_follow.setStyleSheet(self._STYLE_ON if checked else "")
        if checked:
            self._exit_temp_global()

    def _exit_bus_global(self):
        """退出 BUS 全局模式(不改变跟随状态), 同步按钮。"""
        if not self._bus_global:
            return
        self._bus_global = False
        self._btn_bus_global.blockSignals(True)
        self._btn_bus_global.setChecked(False)
        self._btn_bus_global.setStyleSheet("")
        self._btn_bus_global.blockSignals(False)

    def _exit_temp_global(self):
        """退出 TEMP 全局模式(不改变跟随状态), 同步按钮。"""
        if not self._temp_global:
            return
        self._temp_global = False
        self._btn_temp_global.blockSignals(True)
        self._btn_temp_global.setChecked(False)
        self._btn_temp_global.setStyleSheet("")
        self._btn_temp_global.blockSignals(False)

    def _set_bus_follow(self, on: bool):
        """设置 BUS 跟随并同步按钮(阻断信号); 恢复跟随时退出全局。"""
        self._bus_follow = bool(on)
        self._btn_bus_follow.blockSignals(True)
        self._btn_bus_follow.setChecked(self._bus_follow)
        self._btn_bus_follow.setStyleSheet(self._STYLE_ON if self._bus_follow else "")
        self._btn_bus_follow.blockSignals(False)
        if on:
            self._exit_bus_global()

    def _set_temp_follow(self, on: bool):
        """设置 TEMP 跟随并同步按钮(阻断信号); 恢复跟随时退出全局。"""
        self._temp_follow = bool(on)
        self._btn_temp_follow.blockSignals(True)
        self._btn_temp_follow.setChecked(self._temp_follow)
        self._btn_temp_follow.setStyleSheet(self._STYLE_ON if self._temp_follow else "")
        self._btn_temp_follow.blockSignals(False)
        if on:
            self._exit_temp_global()

    def _set_bus_auto_y(self, on: bool, style: bool = False):
        """设置 BUS 左轴 Y 自适应并同步按钮(阻断信号)。"""
        self._bus_auto_y = bool(on)
        self._btn_bus_auto_y.blockSignals(True)
        self._btn_bus_auto_y.setChecked(self._bus_auto_y)
        if style:
            self._btn_bus_auto_y.setStyleSheet(self._STYLE_ON if self._bus_auto_y else "")
        self._btn_bus_auto_y.blockSignals(False)

    def _set_temp_auto_y(self, on: bool, style: bool = False):
        """设置 TEMP Y 自适应并同步按钮(阻断信号)。"""
        self._temp_auto_y = bool(on)
        self._btn_temp_auto_y.blockSignals(True)
        self._btn_temp_auto_y.setChecked(self._temp_auto_y)
        if style:
            self._btn_temp_auto_y.setStyleSheet(self._STYLE_ON if self._temp_auto_y else "")
        self._btn_temp_auto_y.blockSignals(False)

    def _set_bus_auto_power_y(self, on: bool, style: bool = False):
        """设置功率右轴 Y 自适应并同步按钮(阻断信号)。"""
        self._bus_auto_power_y = bool(on)
        self._btn_bus_auto_power_y.blockSignals(True)
        self._btn_bus_auto_power_y.setChecked(self._bus_auto_power_y)
        if style:
            self._btn_bus_auto_power_y.setStyleSheet(
                self._STYLE_ON if self._bus_auto_power_y else "")
        self._btn_bus_auto_power_y.blockSignals(False)

    # ==================== 手动缩放检测 ====================
    @staticmethod
    def _range_changed(cur, ref, rel_tol: float = 0.02) -> bool:
        """当前范围是否偏离程序设定参考(判定该轴被用户手动改动)。"""
        if ref is None:
            return False
        span = max(1e-9, abs(cur[0]) + abs(cur[1]), abs(ref[0]) + abs(ref[1]))
        return (abs(cur[0] - ref[0]) > rel_tol * span
                or abs(cur[1] - ref[1]) > rel_tol * span)

    def _on_manual_range_bus(self):
        """BUS 图手动缩放/拖拽: X 偏离停跟随/全局, Y 偏离停 Y自适应。"""
        (vx0, vx1), (vy0, vy1) = self._bus_plot.viewRange()
        if self._range_changed((vx0, vx1), self._bus_set_x):
            self._exit_bus_global()     # 手动缩放退出全局(不回跟随, 停在手动浏览)
            self._set_bus_follow(False)
        if self._range_changed((vy0, vy1), self._bus_set_y):
            self._set_bus_auto_y(False)

    def _on_manual_range_temp(self):
        """TEMP 图手动缩放/拖拽: X 偏离停跟随/全局, Y 偏离停 Y自适应。"""
        (vx0, vx1), (vy0, vy1) = self._temp_plot.viewRange()
        if self._range_changed((vx0, vx1), self._temp_set_x):
            self._exit_temp_global()
            self._set_temp_follow(False)
        if self._range_changed((vy0, vy1), self._temp_set_y):
            self._set_temp_auto_y(False)

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
        self._set_bus_follow(True)   # 修改显示窗口 = 回到跟随模式
        self._refresh(force=True)

    def _on_temp_window_changed(self, value: int):
        self._temp_window_s = int(value)
        self._set_temp_follow(True)
        self._refresh(force=True)

    def _on_temp_period_changed(self, value: float):
        self._temp_period_s = float(value)
        self._restart_temp_poll()

    def _on_temp_enabled_toggled(self, checked: bool):
        self._temp_enabled = bool(checked)
        self._spin_temp_period.setEnabled(not checked)
        self._restart_temp_poll()

    def _restart_temp_poll(self):
        """温度周期采集启停: 只看采集开关, 与面板可见性无关(后台运行)。"""
        self._temp_poll_timer.stop()
        if self._temp_enabled:
            self._temp_poll_timer.start(int(round(self._temp_period_s * 1000)))

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
        """清空全部缓冲与实时数值(X 轴时间基准重置, 视图模式恢复自动)。"""
        self._t0 = None
        self._last_feed_t = None
        self._last_temp_t = None
        for buf in (self._buf_vbus, self._buf_ibus, self._buf_power,
                    self._buf_temp_motor, self._buf_temp_fet):
            buf.clear()
        for key, entry in self._rt_labels.items():
            entry[0].setText("--")
        self._set_bus_follow(True)          # 同时退出全局(互斥)
        self._set_temp_follow(True)
        self._set_bus_auto_y(True)
        self._set_temp_auto_y(True)
        self._set_bus_auto_power_y(True)
        if pg is None:
            return
        for curve in (self._curve_vbus, self._curve_ibus, self._curve_power):
            curve.setData([], [])
        self._curve_temp_motor.setData([], [])
        self._curve_temp_fet.setData([], [])
        self._refresh(force=True)

    # ==================== 刷新 ====================
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
        if pg is None:
            return
        bus_dirty = (self._buf_vbus.dirty or self._buf_ibus.dirty
                     or self._buf_power.dirty)
        temp_dirty = self._buf_temp_motor.dirty or self._buf_temp_fet.dirty
        if not (bus_dirty or temp_dirty) and not force:
            return

        self._auto_range_counter += 1
        do_auto_y = self._auto_range_counter >= self._AUTO_RANGE_EVERY or force
        if do_auto_y:
            self._auto_range_counter = 0
        self._side_range_counter += 1
        do_side_y = self._side_range_counter >= self._AUTO_RANGE_EVERY or force
        if do_side_y:
            self._side_range_counter = 0

        # ---- BUS 图: 暂停时只入库不绘图(dirty 保持, 恢复后一次绘出) ----
        if not (self._bus_paused and not force) and (bus_dirty or force):
            # 全局=整条曲线 / 跟随=右端固定最新时刻左端滑动 / 手动=按可视范围
            t_bus_end = self._buf_vbus.last_time()
            if t_bus_end is not None:
                win_start = max(0.0, t_bus_end - float(self._bus_window_s))
                if self._bus_global:
                    t_bus_start = self._buf_vbus.first_time()
                    self._bus_plot.setXRange(t_bus_start,
                                             max(t_bus_end, t_bus_start + 1.0), padding=0.02)
                    self._bus_set_x = tuple(self._bus_plot.viewRange()[0])
                elif self._bus_follow:
                    t_bus_start = win_start
                    self._bus_plot.setXRange(t_bus_start,
                                             max(t_bus_end, t_bus_start + 1.0), padding=0.02)
                    self._bus_set_x = tuple(self._bus_plot.viewRange()[0])
                else:
                    # 手动浏览: 数据起点取可视起点与窗口起点的较早者, 窗口外也有数据
                    t_bus_start = min(self._bus_plot.viewRange()[0][0], win_start)
                self._set_data(self._curve_vbus, self._buf_vbus, t_bus_start, t_bus_end)
                self._set_data(self._curve_ibus, self._buf_ibus, t_bus_start, t_bus_end)
                self._set_data(self._curve_power, self._buf_power, t_bus_start, t_bus_end)
                # Y 自适应基于本帧最新可视范围(setXRange 之后)
                vx0, vx1 = self._bus_plot.viewRange()[0]
                if self._bus_auto_y and do_auto_y:
                    self._auto_range_y_bus(vx0, vx1)
                # 功率右轴独立自适应可视区(Ctrl+滚轮手动缩放后自动停)
                if self._bus_auto_power_y and do_side_y:
                    self._auto_range_y_power(vx0, vx1)
            for buf in (self._buf_vbus, self._buf_ibus, self._buf_power):
                buf.dirty = False

        # ---- TEMP 图(独立暂停/跟随/全局/Y自适应, 周期与 BUS 图互不相干) ----
        if not (self._temp_paused and not force) and (temp_dirty or force):
            t_temp_end = self._buf_temp_motor.last_time()
            if t_temp_end is not None:
                win_start = max(0.0, t_temp_end - float(self._temp_window_s))
                if self._temp_global:
                    t_temp_start = self._buf_temp_motor.first_time()
                    self._temp_plot.setXRange(t_temp_start,
                                              max(t_temp_end, t_temp_start + 1.0), padding=0.02)
                    self._temp_set_x = tuple(self._temp_plot.viewRange()[0])
                elif self._temp_follow:
                    t_temp_start = win_start
                    self._temp_plot.setXRange(t_temp_start,
                                              max(t_temp_end, t_temp_start + 1.0), padding=0.02)
                    self._temp_set_x = tuple(self._temp_plot.viewRange()[0])
                else:
                    t_temp_start = min(self._temp_plot.viewRange()[0][0], win_start)
                self._set_data(self._curve_temp_motor, self._buf_temp_motor,
                               t_temp_start, t_temp_end)
                self._set_data(self._curve_temp_fet, self._buf_temp_fet,
                               t_temp_start, t_temp_end)
                vx0, vx1 = self._temp_plot.viewRange()[0]
                if self._temp_auto_y and do_auto_y:
                    self._auto_range_y_temp(vx0, vx1)
            self._buf_temp_motor.dirty = False
            self._buf_temp_fet.dirty = False

    @staticmethod
    def _y_range(buf, t_min: float, t_max: float = None):
        ts, ys = buf.arrays(t_min if t_min > 0 else None)
        if not len(ys):
            return None
        if t_max is not None:
            ys = ys[ts <= t_max]
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

    def _auto_range_y_bus(self, t_min: float, t_max: float):
        """左轴(电压/电流)Y 自适应: 取两曲线可视区范围的并集。"""
        r_left = None
        for buf in (self._buf_vbus, self._buf_ibus):
            r = self._y_range(buf, t_min, t_max)
            if r is None:
                continue
            r_left = r if r_left is None else (min(r_left[0], r[0]), max(r_left[1], r[1]))
        if r_left is not None:
            self._bus_plot.setYRange(r_left[0], r_left[1], padding=0.02)
            self._bus_set_y = tuple(self._bus_plot.viewRange()[1])

    def _auto_range_y_power(self, t_min: float, t_max: float):
        """功率右轴 Y 自适应可视区数据(独立于左轴开关)。"""
        r = self._y_range(self._buf_power, t_min, t_max)
        if r is not None:
            self._vb_power.setYRange(r[0], r[1], padding=0.02)

    def _auto_range_y_temp(self, t_min: float, t_max: float):
        r = None
        for buf in (self._buf_temp_motor, self._buf_temp_fet):
            ri = self._y_range(buf, t_min, t_max)
            if ri is None:
                continue
            r = ri if r is None else (min(r[0], ri[0]), max(r[1], ri[1]))
        if r is not None:
            self._temp_plot.setYRange(r[0], r[1], padding=0.02)
            self._temp_set_y = tuple(self._temp_plot.viewRange()[1])

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
        self._apply_axis_labels()
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
            "temp_period_s": float(self._temp_period_s),
            "temp_depth": int(self._temp_depth),
            "temp_window_s": int(self._temp_window_s),
            "bus_follow": bool(self._bus_follow),
            "temp_follow": bool(self._temp_follow),
            "bus_auto_y": bool(self._bus_auto_y),
            "temp_auto_y": bool(self._temp_auto_y),
            "bus_auto_power_y": bool(self._bus_auto_power_y),
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
            v = float(opts.get("temp_period_s", self._temp_period_s))
            if 0.1 <= v <= 3600:
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
        # 旧版单套配置键(follow/auto_y)兼容: 无分图键时两图共用
        try:
            self._set_bus_follow(bool(opts.get("bus_follow", opts.get("follow", True))))
        except Exception:
            pass
        try:
            self._set_temp_follow(bool(opts.get("temp_follow", opts.get("follow", True))))
        except Exception:
            pass
        try:
            self._set_bus_auto_y(bool(opts.get("bus_auto_y", opts.get("auto_y", True))))
        except Exception:
            pass
        try:
            self._set_temp_auto_y(bool(opts.get("temp_auto_y", opts.get("auto_y", True))))
        except Exception:
            pass
        try:
            self._set_bus_auto_power_y(
                bool(opts.get("bus_auto_power_y", opts.get("auto_power_y", True))))
        except Exception:
            pass
