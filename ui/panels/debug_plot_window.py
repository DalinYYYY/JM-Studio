"""调试通道独立波形窗口.

提供一个独立的 QWidget+Window 窗口, 使用 pyqtgraph 显示遥测帧中的调试通道数据
(jm_dbg[] f32 数组)。支持动态通道数、用户自定义通道命名、工具栏控制, 以及与主窗口
一致的脏标记驱动刷新和主题切换。
"""

import colorsys
import csv

import numpy as np

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout,
    QWidget,
)

from ui.theme import theme
from ui.panels.plot_panel import _SeriesBuffer

try:
    import pyqtgraph as pg
except Exception:
    pg = None


def _gen_debug_palette():
    """按黄绿蓝取对角中间值方法生成颜色表.

    排列顺序按层级: 主色(黄绿蓝) → 一级中点 → 二级中点 → ...
    前几个通道覆盖黄绿蓝及其主中点, 颜色差异最大化.
    每轮在色环相邻色相之间插入短弧中点, 新中点追加到结果末尾.
    """
    ring = [60.0, 120.0, 240.0]  # 黄、绿、蓝 (色环顺序)
    result = list(ring)
    for _ in range(4):  # 3 + 3 + 6 + 12 + 24 = 48 个色相
        new_ring = []
        new_mids = []
        n = len(ring)
        for i in range(n):
            new_ring.append(ring[i])
            h1 = ring[i]
            h2 = ring[(i + 1) % n]
            diff = (h2 - h1) % 360
            if diff > 180:
                diff -= 360
            mid = (h1 + diff / 2) % 360
            new_ring.append(mid)
            new_mids.append(mid)
        ring = new_ring
        result.extend(new_mids)
    palette = []
    for h in result:
        # 饱和度 0.85, 亮度 0.95 保证颜色鲜艳且不刺眼
        r, g, b = colorsys.hsv_to_rgb(h / 360.0, 0.85, 0.95)
        palette.append((int(r * 255), int(g * 255), int(b * 255)))
    return palette


# 调试通道颜色表: 黄绿蓝对角中间值分布, 48 色循环
_DEBUG_PALETTE = _gen_debug_palette()


def _debug_color(i: int) -> tuple:
    """获取第 i 个调试通道的 RGB 颜色元组."""
    return _DEBUG_PALETTE[i % len(_DEBUG_PALETTE)]


class _ChannelNameDialog(QDialog):
    """通道命名对话框: 列出所有通道, 用户可编辑每个通道的名称."""

    def __init__(self, channel_names: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("调试通道命名")
        self.setMinimumWidth(360)
        self.resize(420, min(520, 160 + 32 * len(channel_names)))
        self._edits: list[QLineEdit] = []

        layout = QVBoxLayout(self)
        # 通道数由固件决定，使用滚动区避免 16/24 通道时命名对话框超出屏幕。
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(140)
        scroll.setMaximumHeight(360)
        form_widget = QWidget()
        form = QFormLayout(form_widget)
        for i, name in enumerate(channel_names):
            edit = QLineEdit(name)
            edit.setPlaceholderText(f"DBG_{i}")
            self._edits.append(edit)
            form.addRow(f"通道 {i}:", edit)
        scroll.setWidget(form_widget)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_names(self) -> list:
        """返回编辑后的名称列表."""
        return [e.text().strip() for e in self._edits]


class DebugPlotWindow(QWidget):
    """调试通道独立波形窗口.

    通过 MainWindow 管理生命周期, 关闭时发出 window_closed 信号通知外部。
    """

    window_closed = pyqtSignal()

    MAX_POINTS = 300_000
    _AUTO_RANGE_EVERY = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("调试通道波形")
        self.setWindowFlags(Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumSize(800, 500)

        self._point_index: int = 0
        self._paused: bool = False
        self._window_points: int = 2000
        self._channel_count: int = 0
        self._channel_names: list[str] = []
        self._buffers: dict[str, _SeriesBuffer] = {}
        self._curves: dict[str, pg.PlotDataItem] = {}
        self._latest_idx: float | None = None
        self._auto_range_counter: int = 0
        self._setting_x = False

        self._build()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        if pg is None:
            hint = QLabel(
                "调试通道波形需要 pyqtgraph。\n"
                "当前环境缺少该依赖，无法显示交互波形。")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #888; padding: 20px;")
            layout.addWidget(hint)
            return

        # 工具栏
        toolbar = QWidget()
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(4)

        self._btn_pause = QPushButton("暂停")
        self._btn_pause.setCheckable(True)
        self._btn_pause.setToolTip("暂停画面刷新，数据仍继续缓存；点击继续查看暂停期间波形")
        self._btn_pause.setFixedHeight(22)
        self._btn_pause.clicked.connect(self._on_pause_toggled)
        bar.addWidget(self._btn_pause)

        self._chk_follow = QCheckBox("跟随")
        self._chk_follow.setChecked(True)
        self._chk_follow.setToolTip("跟随最新数据；取消后可拖动和缩放查看历史波形")
        self._chk_follow.stateChanged.connect(
            lambda _state: self._refresh(force=True))
        bar.addWidget(self._chk_follow)

        self._chk_auto_y = QCheckBox("Y自适应")
        self._chk_auto_y.setChecked(True)
        self._chk_auto_y.setToolTip("根据当前可见波形自动调整 Y 轴范围")
        bar.addWidget(self._chk_auto_y)

        self._chk_legend = QCheckBox("图例")
        self._chk_legend.setChecked(True)
        self._chk_legend.setToolTip("点击图例颜色线段可切换曲线显示；Y 自适应只统计可见曲线")
        self._chk_legend.toggled.connect(self._on_legend_toggled)
        bar.addWidget(self._chk_legend)

        bar.addWidget(QLabel("窗口(点):"))
        self._spin_window = QSpinBox()
        self._spin_window.setRange(100, 50000)
        self._spin_window.setValue(self._window_points)
        self._spin_window.setToolTip("跟随模式下显示的最新数据点数")
        self._spin_window.setFixedWidth(72)
        self._spin_window.valueChanged.connect(self._on_window_changed)
        bar.addWidget(self._spin_window)

        self._btn_rename = QPushButton("通道命名")
        self._btn_rename.setFixedHeight(22)
        self._btn_rename.clicked.connect(self._on_rename_channels)
        bar.addWidget(self._btn_rename)

        self._btn_export = QPushButton("导出 CSV")
        self._btn_export.setFixedHeight(22)
        self._btn_export.setToolTip("导出全部缓存通道，包括隐藏曲线；每行记录数据点、通道号、名称和值")
        self._btn_export.clicked.connect(self._on_export_csv)
        bar.addWidget(self._btn_export)

        self._btn_clear = QPushButton("清空")
        self._btn_clear.setFixedHeight(22)
        self._btn_clear.clicked.connect(self.clear)
        bar.addWidget(self._btn_clear)

        self._btn_reset = QPushButton("重置视图")
        self._btn_reset.setFixedHeight(22)
        self._btn_reset.clicked.connect(self._reset_view)
        bar.addWidget(self._btn_reset)

        bar.addStretch()
        layout.addWidget(toolbar)

        # 绘图区
        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground(theme.hex('plot_bg'))
        self._glw.ci.layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._glw, 1)

        self._plot = self._glw.addPlot(title="调试通道")
        self._plot.setTitle("调试通道", color=theme.hex('plot_title'), size='8pt')
        self._plot.showGrid(x=True, y=True, alpha=0.25)
        self._plot.setLabel('left', '调试值')
        self._plot.setLabel('bottom', '数据点')
        self._plot.getAxis('left').setWidth(50)
        self._plot.getAxis('bottom').setHeight(20)
        self._plot.setMenuEnabled(False)
        self._plot.vb.setMenuEnabled(False)
        self._plot.setClipToView(True)
        self._plot.setDownsampling(auto=True, mode='peak')
        self._plot.disableAutoRange()
        self._plot.setDefaultPadding(0.01)
        self._plot.addLegend(offset=(4, 4))
        if self._plot.legend is not None:
            self._plot.legend.setVisible(False)

        # 鼠标悬停线
        self._vline = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen((180, 180, 180, 160), style=Qt.PenStyle.DashLine))
        self._hline = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen((180, 180, 180, 160), style=Qt.PenStyle.DashLine))
        self._vline.setVisible(False)
        self._hline.setVisible(False)
        self._plot.addItem(self._vline, ignoreBounds=True)
        self._plot.addItem(self._hline, ignoreBounds=True)

        self._hover_label = pg.TextItem(
            anchor=(1, 0), fill=(0, 0, 0, 160), color='#F5F5F5')
        self._hover_label.setVisible(False)
        self._plot.addItem(self._hover_label, ignoreBounds=True)

        # 鼠标移动信号
        self._mouse_proxy = pg.SignalProxy(
            self._glw.scene().sigMouseMoved, rateLimit=30,
            slot=self._on_mouse_moved)

        # 刷新定时器
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._refresh)
        self._plot.vb.sigRangeChangedManually.connect(self._on_manual_range)

    # ------------------------------------------------------------------
    # 数据喂入
    # ------------------------------------------------------------------

    def feed_debug(self, debug_tuple: tuple):
        """接收单帧调试数据, 首次调用时动态创建通道.

        暂停只冻结显示刷新，仍继续把收到的数据写入环形缓冲；这样恢复后
        可以看到暂停期间的完整波形，并与实时曲线面板的暂停语义保持一致。
        """
        if pg is None or not debug_tuple:
            return
        idx = float(self._point_index)
        self._point_index += 1
        # 固件通道数通常固定，但允许运行期间增加通道，避免新通道被静默丢弃。
        if len(debug_tuple) > self._channel_count:
            self._init_channels(len(debug_tuple))
        for i, val in enumerate(debug_tuple):
            name = f"dbg_{i}"
            if name not in self._buffers:
                continue
            try:
                value = float(val)
            except (TypeError, ValueError):
                continue
            # 无效值不写入缓冲，避免破坏 Y 轴范围和悬停插值。
            if np.isfinite(value):
                self._buffers[name].append(idx, value)
        if self._latest_idx is None or idx > self._latest_idx:
            self._latest_idx = idx

    def feed_debug_batch(self, debug_data_list: list):
        """批量接收调试数据 (逐帧全部绘制策略)."""
        if pg is None:
            return
        n = len(debug_data_list)
        if n == 0:
            return

        # 数据点编号按收到的帧数连续推进，即使批次中包含空帧；否则下一批
        # 数据会回退到错误的 X 位置，导致暂停/断续采样后曲线时间轴跳变。
        start_idx = self._point_index
        ts_arr = np.arange(start_idx, start_idx + n, dtype=np.float32)
        self._point_index += n

        # 首次确定通道数：不能只看批次首帧，UI 队列合并后首帧可能为空。
        # 同时支持运行期间通道数增加，保留已有曲线和缓冲区。
        max_count = max((len(d) for d in debug_data_list if d), default=0)
        if max_count > self._channel_count:
            self._init_channels(max_count)

        # 按通道收集数据
        ch_count = self._channel_count
        if ch_count == 0:
            return

        for ch in range(ch_count):
            name = f"dbg_{ch}"
            buf = self._buffers.get(name)
            if buf is None:
                continue
            # 用 mask 同步过滤 ts 和 ys, 避免通道缺失时 X 轴错位
            mask = np.fromiter(
                (ch < len(d) for d in debug_data_list),
                dtype=bool, count=n)
            if mask.any():
                ts_in = ts_arr[mask]
                ys_in = np.array(
                    [float(d[ch]) for d, m in zip(debug_data_list, mask) if m],
                    dtype=np.float32)
                # 无效浮点值会破坏 Y 轴自适应和悬停读数，直接丢弃对应样本。
                finite = np.isfinite(ys_in)
                if finite.any():
                    buf.append_batch(ts_in[finite], ys_in[finite])

        last_idx = float(ts_arr[-1])
        if self._latest_idx is None or last_idx > self._latest_idx:
            self._latest_idx = last_idx

    # ------------------------------------------------------------------
    # 通道管理
    # ------------------------------------------------------------------

    def _init_channels(self, count: int):
        """动态创建 count 个通道的曲线和缓冲区."""
        if pg is None or count <= 0:
            return
        self._channel_count = count

        # 确保 _channel_names 长度匹配
        while len(self._channel_names) < count:
            idx = len(self._channel_names)
            self._channel_names.append(f"DBG_{idx}")

        for i in range(count):
            name = f"dbg_{i}"
            if name in self._buffers:
                continue
            self._buffers[name] = _SeriesBuffer(self.MAX_POINTS)
            pen = pg.mkPen(_debug_color(i), width=2)
            display = self._get_display_name(i)
            # plot(name=display) 会自动将曲线添加到 legend
            curve = self._plot.plot([], [], pen=pen, name=display)
            self._curves[name] = curve

        # 通道数 >= 1 时显示图例
        if self._plot.legend is not None:
            self._plot.legend.setVisible(count >= 1 and self._chk_legend.isChecked())

    def _get_display_name(self, index: int) -> str:
        """获取第 index 个通道的显示名称."""
        if index < len(self._channel_names) and self._channel_names[index]:
            return self._channel_names[index]
        return f"DBG_{index}"

    def _on_rename_channels(self):
        """打开通道命名对话框."""
        if self._channel_count == 0:
            return
        names = [self._get_display_name(i) for i in range(self._channel_count)]
        dlg = _ChannelNameDialog(names, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_names = dlg.get_names()
        self._channel_names = new_names[:self._channel_count]
        while len(self._channel_names) < self._channel_count:
            self._channel_names.append("")
        # pyqtgraph 0.14 PlotDataItem 无 setName, 需重建曲线并刷新 legend
        if self._plot.legend is not None:
            self._plot.legend.clear()
        for i in range(self._channel_count):
            name = f"dbg_{i}"
            old_curve = self._curves.get(name)
            if old_curve is not None:
                self._plot.removeItem(old_curve)
            display = self._get_display_name(i)
            pen = pg.mkPen(_debug_color(i), width=2)
            curve = self._plot.plot([], [], pen=pen, name=display)
            self._curves[name] = curve
        if self._plot.legend is not None:
            self._plot.legend.setVisible(
                self._channel_count >= 1 and self._chk_legend.isChecked())
        # 重建后立即重绘一次
        self._refresh(force=True)

    # ------------------------------------------------------------------
    # 工具栏控制
    # ------------------------------------------------------------------

    def _on_pause_toggled(self, checked: bool):
        self._paused = checked
        self._btn_pause.setText("继续" if checked else "暂停")
        self._btn_pause.setStyleSheet(
            "background-color: #F57C00; color: white; font-weight: bold;"
            if checked else "")

    def _on_window_changed(self, value: int):
        self._window_points = max(100, int(value))
        # 参数变化后立即收缩/扩展视图，避免必须等下一帧数据才生效。
        if pg is not None and self._latest_idx is not None:
            self._refresh(force=True)

    def _on_legend_toggled(self, checked: bool):
        if pg is not None and self._plot.legend is not None:
            self._plot.legend.setVisible(bool(checked) and self._channel_count >= 1)

    def _on_export_csv(self):
        if self._latest_idx is None:
            QMessageBox.information(self, "导出 CSV", "当前没有调试数据。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出调试通道", "homing_debug.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            self._write_csv(path)
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    def _write_csv(self, path):
        """按通道导出全部缓存，保留缺帧时各通道独立的数据点编号。"""
        with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(('data_point', 'channel', 'name', 'value'))
            for ch in range(self._channel_count):
                buf = self._buffers.get(f"dbg_{ch}")
                if buf is None:
                    continue
                ts, ys = buf.arrays_view(None)
                display = self._get_display_name(ch)
                for idx, val in zip(ts, ys):
                    writer.writerow((format(float(idx), '.0f'), ch, display,
                                     format(float(val), '.9g')))

    def _reset_view(self):
        if pg is None:
            return
        self._chk_follow.setChecked(True)
        self._chk_auto_y.setChecked(True)
        if self._paused:
            self._btn_pause.click()
        self._refresh(force=True)

    def clear(self):
        """清空所有缓冲区."""
        self._point_index = 0
        self._latest_idx = None
        self._auto_range_counter = 0
        for buf in self._buffers.values():
            buf.clear()
        if pg is None:
            return
        for curve in self._curves.values():
            curve.setData([], [])

    # ------------------------------------------------------------------
    # 核心刷新
    # ------------------------------------------------------------------

    def _any_dirty(self) -> bool:
        for buf in self._buffers.values():
            if buf.dirty:
                return True
        return False

    @staticmethod
    def _slice_x_range(buf: _SeriesBuffer, x_min=None, x_max=None):
        """读取指定 X 范围的数据，支持非跟随模式回看历史。"""
        return buf.arrays_view(x_min, x_max)

    def _refresh(self, force: bool = False):
        if pg is None or not self.isVisible() or (self._paused and not force):
            return
        if not self._any_dirty() and not force:
            return

        idx_end = self._latest_idx
        if idx_end is None:
            return
        follow = self._chk_follow.isChecked()
        tail_min = max(0.0, idx_end - float(self._window_points))
        if follow:
            view_min, view_max = tail_min, max(idx_end, tail_min + 1.0)
        else:
            # 非跟随时保留用户当前 X 视图，并把完整历史交给 pyqtgraph，
            # 这样拖动/缩放后不会因为只提交尾部窗口而出现空白。
            try:
                view_min, view_max = self._plot.viewRange()[0]
            except Exception:
                view_min, view_max = None, None
            if view_min is not None and view_max is not None and view_max < view_min:
                view_min, view_max = view_max, view_min

        for name, buf in self._buffers.items():
            if force or buf.dirty:
                curve = self._curves.get(name)
                if curve is None or not curve.isVisible():
                    continue
                ts, ys = self._slice_x_range(
                    buf, view_min, view_max)
                if len(ts) == 0:
                    curve.setData([], [])
                    buf.dirty = False
                    continue
                try:
                    curve.setData(ts, ys, avoidCopy=True)
                except TypeError:
                    curve.setData(ts, ys)
                buf.dirty = False

        if follow:
            self._setting_x = True
            try:
                self._plot.setXRange(view_min, view_max, padding=0.02)
            finally:
                self._setting_x = False

        if self._chk_auto_y.isChecked():
            self._auto_range_counter += 1
            if self._auto_range_counter >= self._AUTO_RANGE_EVERY or force:
                self._auto_range_counter = 0
                self._auto_range_y(
                    tail_min if follow else view_min,
                    view_max if not follow else view_max)

    def _on_manual_range(self, *_):
        if not self._setting_x:
            self._chk_follow.setChecked(False)
            self._refresh(force=True)

    def _auto_range_y(self, idx_min: float | None, idx_max: float | None = None):
        y0 = None
        y1 = None
        for name, buf in self._buffers.items():
            curve = self._curves.get(name)
            if curve is None or not curve.isVisible():
                continue
            _ts, ys = self._slice_x_range(buf, idx_min, idx_max)
            if not len(ys):
                continue
            # 缺失通道或无效遥测值以 NaN 表示，不能参与坐标范围计算。
            finite = ys[np.isfinite(ys)]
            if not len(finite):
                continue
            cur_min = float(np.min(finite))
            cur_max = float(np.max(finite))
            if y0 is None:
                y0, y1 = cur_min, cur_max
            else:
                if cur_min < y0:
                    y0 = cur_min
                if cur_max > y1:
                    y1 = cur_max
        if y0 is None:
            return
        pad = max((y1 - y0) * 0.15, 1e-3) if y0 != y1 else max(1.0, abs(y0) * 0.1)
        self._plot.setYRange(y0 - pad, y1 + pad, padding=0.02)

    # ------------------------------------------------------------------
    # 鼠标悬停
    # ------------------------------------------------------------------

    def _on_mouse_moved(self, evt):
        if pg is None:
            return
        scene_pos = evt[0] if isinstance(evt, tuple) else evt
        vb = self._plot.vb
        if not vb.sceneBoundingRect().contains(scene_pos):
            self._vline.setVisible(False)
            self._hline.setVisible(False)
            self._hover_label.setVisible(False)
            return

        mouse = vb.mapSceneToView(scene_pos)
        x = float(mouse.x())
        y = float(mouse.y())
        self._vline.setPos(x)
        self._hline.setPos(y)
        self._vline.setVisible(True)
        self._hline.setVisible(True)
        self._hover_label.setVisible(True)
        self._hover_label.setHtml(self._hover_values(x, y))
        self._hover_label.setPos(x, y)

    def _hover_values(self, x: float, _y: float) -> str:
        parts = [f"<b>点</b> = {int(x) if x == x else 0}"]
        for i in range(self._channel_count):
            name = f"dbg_{i}"
            buf = self._buffers.get(name)
            if buf is None:
                continue
            val = buf.interpolate(x)
            display = self._get_display_name(i)
            val_text = "--" if val is None or not np.isfinite(val) else f"{val:.4f}"
            parts.append(f"{display} = {val_text}")
        return "<div style='color:#F5F5F5;'>" + "<br/>".join(parts) + "</div>"

    # ------------------------------------------------------------------
    # 配置持久化
    # ------------------------------------------------------------------

    def get_opts(self) -> dict:
        """持久化: 窗口大小/位置、工具栏状态、通道名称."""
        if pg is None:
            return {"channel_names": list(self._channel_names)}
        geo = self.geometry()
        return {
            "geo": [geo.x(), geo.y(), geo.width(), geo.height()],
            "window_points": int(self._window_points),
            "follow": self._chk_follow.isChecked(),
            "auto_y": self._chk_auto_y.isChecked(),
            "legend": self._chk_legend.isChecked(),
            "channel_names": list(self._channel_names),
        }

    def set_opts(self, opts: dict):
        """恢复配置."""
        if not isinstance(opts, dict):
            return
        # 通道名称 (不依赖 pg)
        names = opts.get("channel_names")
        if isinstance(names, list):
            self._channel_names = list(names)
        if pg is None:
            return
        # 窗口大小/位置
        geo = opts.get("geo")
        if isinstance(geo, list) and len(geo) >= 4:
            try:
                self.setGeometry(int(geo[0]), int(geo[1]),
                                 max(800, int(geo[2])), max(500, int(geo[3])))
            except Exception:
                pass
        # 窗口点数
        try:
            wp = int(opts.get("window_points", self._window_points))
            if 100 <= wp <= 50000:
                self._window_points = wp
                self._spin_window.setValue(wp)
        except Exception:
            pass
        # 跟随
        try:
            self._chk_follow.setChecked(bool(opts.get("follow", True)))
        except Exception:
            pass
        # Y 自适应
        try:
            self._chk_auto_y.setChecked(bool(opts.get("auto_y", True)))
        except Exception:
            pass
        # 图例显隐
        try:
            self._chk_legend.setChecked(bool(opts.get("legend", True)))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 主题切换
    # ------------------------------------------------------------------

    def apply_theme(self):
        """主题切换: 更新背景、画笔、坐标轴颜色."""
        if pg is None:
            return
        self._glw.setBackground(theme.hex('plot_bg'))
        axis_pen = pg.mkPen(theme.hex('muted'))
        text_col = theme.c('text')
        self._plot.setTitle("调试通道", color=theme.hex('plot_title'), size='8pt')
        for ax_name in ('left', 'bottom'):
            ax = self._plot.getAxis(ax_name)
            ax.setPen(axis_pen)
            ax.setTextPen(text_col)
        for i, (name, curve) in enumerate(self._curves.items()):
            curve.setPen(pg.mkPen(_debug_color(i), width=2))

    # ------------------------------------------------------------------
    # 窗口关闭
    # ------------------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, '_timer'):
            self._timer.start()
            self._refresh(force=True)

    def hideEvent(self, event):
        super().hideEvent(event)
        if hasattr(self, '_timer'):
            self._timer.stop()

    def closeEvent(self, event):
        if hasattr(self, '_timer') and self._timer is not None:
            self._timer.stop()
        self.window_closed.emit()
        event.accept()
