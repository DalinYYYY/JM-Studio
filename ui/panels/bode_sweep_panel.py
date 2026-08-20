"""开环 Bode 扫频面板：配置、Bode 曲线、原始波形和频点质量表。"""

from __future__ import annotations

import math
import struct
import time
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from core.bode_analysis import closed_loop_metrics, fit_mechanical
from core.bode_session import BodeSession
from jmproto.cmd_def import SweepMode, SweepTestMode, TopFsm
from ui.theme import theme

try:
    import pyqtgraph as pg
except Exception:  # pragma: no cover
    pg = None


class BodeSweepPanel(QWidget):
    # 会话状态 → 状态栏中文文案
    _STATE_CN = {
        BodeSession.IDLE: "空闲",
        BodeSession.STARTING: "已启动，等待应答",
        BodeSession.RUNNING: "扫频进行中",
        BodeSession.DRAINING: "停止排空中",
        BodeSession.COMPLETE: "扫频完成",
        BodeSession.ABORTED: "已停止",
        BodeSession.ERROR: "出错",
    }

    # 测试模式 → 固件幅值限幅(motor_info 参数 ID; None=不在 motor_info 体系, 上位机读不到)
    # 固件规则: |偏置|+幅值 ≤ 模式对应限幅, 超限返回 OUT_OF_RANGE
    # 力矩模式限幅 peak_torque 是编译期参数(motor_profile.h), 不在 motor_info 参数表
    _LIMIT_INFO = {
        int(SweepTestMode.TORQUE_TO_VELOCITY): (None, "峰值转矩", "Nm"),
        int(SweepTestMode.CURRENT_TO_VELOCITY): (43, "峰值电流", "A"),
        int(SweepTestMode.CURRENT_TO_CURRENT): (43, "峰值电流", "A"),
        int(SweepTestMode.VELOCITY_TO_POSITION): (44, "最大转速", "rad/s"),
        int(SweepTestMode.VELOCITY_TO_VELOCITY): (44, "最大转速", "rad/s"),
        int(SweepTestMode.POSITION_TO_POSITION): (44, "最大转速", "rad/s"),
    }

    # 方向一(辨识整定)测试模式: 产出 G(s) 拟合与 PID 换算建议
    _IDENT_MODES = (
        int(SweepTestMode.TORQUE_TO_VELOCITY),
        int(SweepTestMode.CURRENT_TO_VELOCITY),
        int(SweepTestMode.VELOCITY_TO_POSITION),
    )
    # 方向二(验证闭环)测试模式: 产出带宽/Mr/PM/GM 验收指标
    _VERIFY_MODES = (
        int(SweepTestMode.CURRENT_TO_CURRENT),
        int(SweepTestMode.VELOCITY_TO_VELOCITY),
        int(SweepTestMode.POSITION_TO_POSITION),
    )

    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.client = client
        self.session = BodeSession(client, self)
        self._last_raw_refresh = 0.0
        self._top_fsm = None  # 最近一次上报的电机顶层状态(未收到过为 None)
        self._limits = {}    # motor_info 限幅被动缓存 {43: peak_current, 44: max_speed}
        self._build()
        self.session.status_changed.connect(self._on_status)
        self.session.point_updated.connect(self._on_point)
        self.session.raw_updated.connect(self._on_raw_update)
        self.session.finished.connect(self._on_finished)
        self.client.state_updated.connect(self._on_state_updated)
        # 被动缓存电机限幅(43/44): 只监听既有读应答, 不主动发读命令,
        # 避免干扰主窗口 _pending_param_reads 应答配对队列
        self.client.motor_info_read_result.connect(self._on_motor_info_result)
        self.client.motor_info_read_bulk_result.connect(self._on_motor_info_bulk)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)

        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        # 第一行：测试模式、点数、起始频率、结束频率
        form.addWidget(QLabel("测试模式"), 0, 0, alignment=align)
        self.mode = QComboBox()
        self.mode.addItem("【辨识】力矩/电流 → 速度（机械对象）", int(SweepTestMode.TORQUE_TO_VELOCITY))
        self.mode.addItem("【辨识】电流 → 速度（含电流环）", int(SweepTestMode.CURRENT_TO_VELOCITY))
        self.mode.addItem("【辨识】速度参考 → 位置反馈（位置对象）", int(SweepTestMode.VELOCITY_TO_POSITION))
        self.mode.addItem("【验证】电流参考 → 电流反馈（电流环闭环）", int(SweepTestMode.CURRENT_TO_CURRENT))
        self.mode.addItem("【验证】速度参考 → 速度反馈（速度环闭环）", int(SweepTestMode.VELOCITY_TO_VELOCITY))
        self.mode.addItem("【验证】位置参考 → 位置反馈（位置环闭环）", int(SweepTestMode.POSITION_TO_POSITION))
        self.mode.currentIndexChanged.connect(self._sync_parameter_labels)
        self.mode.currentIndexChanged.connect(self._on_mode_changed)
        form.addWidget(self.mode, 0, 1)
        self.point_cfg_label = QLabel()
        self.point_cfg_label.setAlignment(align)
        form.addWidget(self.point_cfg_label, 0, 2)
        self.point_cfg = QSpinBox()
        self.point_cfg.setRange(1, 255)
        self.point_cfg.setValue(10)
        form.addWidget(self.point_cfg, 0, 3)
        form.addWidget(QLabel("起始频率 (Hz)"), 0, 4, alignment=align)
        self.f_start = QDoubleSpinBox()
        self.f_start.setRange(0.1, 6553.5)
        self.f_start.setDecimals(1)
        self.f_start.setValue(0.5)
        form.addWidget(self.f_start, 0, 5)
        form.addWidget(QLabel("结束频率 (Hz)"), 0, 6, alignment=align)
        self.f_end = QDoubleSpinBox()
        self.f_end.setRange(0.1, 6553.5)
        self.f_end.setDecimals(1)
        self.f_end.setValue(30.0)
        form.addWidget(self.f_end, 0, 7)

        # 第二行：扫频方式、幅值、偏置
        form.addWidget(QLabel("扫频方式"), 1, 0, alignment=align)
        self.sweep_mode = QComboBox()
        self.sweep_mode.addItem("对数", int(SweepMode.LOG))
        self.sweep_mode.addItem("线性", int(SweepMode.LINEAR))
        self.sweep_mode.addItem("1Hz 步进", int(SweepMode.STEP))
        self.sweep_mode.currentIndexChanged.connect(self._sync_parameter_labels)
        form.addWidget(self.sweep_mode, 1, 1)
        self.amp_label = QLabel()
        self.amp_label.setAlignment(align)
        form.addWidget(self.amp_label, 1, 2)
        self.amp = QDoubleSpinBox()
        self.amp.setRange(0.001, 655.35)
        self.amp.setDecimals(3)
        self.amp.setValue(0.001)
        form.addWidget(self.amp, 1, 3)
        self.bias_label = QLabel()
        self.bias_label.setAlignment(align)
        form.addWidget(self.bias_label, 1, 4)
        self.bias = QDoubleSpinBox()
        self.bias.setRange(-327.68, 327.67)
        self.bias.setDecimals(2)
        self.bias.setValue(0.0)
        form.addWidget(self.bias, 1, 5)
        self.bias_hint = QLabel("扫频围绕该直流工作点叠加")
        self.bias_hint.setStyleSheet(f"color:{theme.hex('muted')};")
        form.addWidget(self.bias_hint, 1, 6, 1, 2)
        root.addLayout(form)

        self.status = QLabel("空闲")
        self.status.setStyleSheet(f"color:{theme.hex('muted')};")
        root.addWidget(self.status)

        self.tabs = QTabWidget()
        self._build_ident_tab()
        self._build_verify_tab()
        self._build_bode_tab()
        self._build_raw_tab()
        self._build_table_tab()
        self._sync_parameter_labels()
        self._on_mode_changed()  # 初始方向提示
        root.addWidget(self.tabs, 1)

        # 三个按钮等长，放到 tab 栏最右侧
        self.start_btn = QPushButton("启动扫频")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.session.stop)
        self.export_btn = QPushButton("导出 CSV")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._export)
        btn_width = max(btn.sizeHint().width() for btn in
                        (self.start_btn, self.stop_btn, self.export_btn))
        for btn in (self.start_btn, self.stop_btn, self.export_btn):
            btn.setMinimumWidth(btn_width)
            # NoFocus: 点击后按钮被禁用时焦点不会跳到参数输入框
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button_bar = QWidget()
        button_layout = QHBoxLayout(button_bar)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(6)
        button_layout.addWidget(self.start_btn)
        button_layout.addWidget(self.stop_btn)
        button_layout.addWidget(self.export_btn)
        self.tabs.setCornerWidget(button_bar, Qt.Corner.TopRightCorner)

    def _build_ident_tab(self):
        """方向一(辨识整定)：G(s) 拟合 + 带宽法 PID 换算建议。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        self.ident_hint = QLabel("")
        self.ident_hint.setWordWrap(True)
        self.ident_hint.setStyleSheet(f"color:{theme.hex('muted')};")
        layout.addWidget(self.ident_hint)

        fit_box = QGroupBox("对象拟合 G(s) = 1/(J·s + B)")
        fit_form = QFormLayout(fit_box)
        self.fit_J = QLabel("--")
        self.fit_B = QLabel("--")
        self.fit_corner = QLabel("--")
        self.fit_err = QLabel("--")
        self.fit_points = QLabel("--")
        fit_form.addRow("等效惯量 J (含 1/Kt)", self.fit_J)
        fit_form.addRow("等效粘性摩擦 B (含 1/Kt)", self.fit_B)
        fit_form.addRow("机械转折频率", self.fit_corner)
        fit_form.addRow("拟合误差 (RMS)", self.fit_err)
        fit_form.addRow("参与频点数", self.fit_points)
        layout.addWidget(fit_box)

        pid_box = QGroupBox("带宽法 PID 换算（修改目标带宽后自动重算）")
        pid_grid = QGridLayout(pid_box)
        pid_grid.addWidget(QLabel("目标速度环带宽"), 0, 0)
        self.vel_bw = QDoubleSpinBox()
        self.vel_bw.setRange(0.1, 500.0)
        self.vel_bw.setDecimals(1)
        self.vel_bw.setSingleStep(1.0)
        self.vel_bw.setSuffix(" Hz")
        self.vel_bw.setValue(20.0)
        self.vel_bw.valueChanged.connect(self._refresh_ident_results)
        pid_grid.addWidget(self.vel_bw, 0, 1)
        pid_grid.addWidget(QLabel("速度环 Kp"), 0, 2)
        self.vel_kp = QLabel("--")
        pid_grid.addWidget(self.vel_kp, 0, 3)
        pid_grid.addWidget(QLabel("速度环 Ki"), 0, 4)
        self.vel_ki = QLabel("--")
        pid_grid.addWidget(self.vel_ki, 0, 5)
        pid_grid.addWidget(QLabel("目标位置环带宽"), 1, 0)
        self.pos_bw = QDoubleSpinBox()
        self.pos_bw.setRange(0.1, 200.0)
        self.pos_bw.setDecimals(1)
        self.pos_bw.setSingleStep(1.0)
        self.pos_bw.setSuffix(" Hz")
        self.pos_bw.setValue(5.0)
        self.pos_bw.valueChanged.connect(self._refresh_ident_results)
        pid_grid.addWidget(self.pos_bw, 1, 1)
        pid_grid.addWidget(QLabel("位置环 Kp"), 1, 2)
        self.pos_kp = QLabel("--")
        pid_grid.addWidget(self.pos_kp, 1, 3)
        layout.addWidget(pid_box)

        note = QLabel("换算规则: 速度环 Kp = ωv·Ĵ、Ki/Kp = ωv/4(零点)；位置环 Kp = ωp。\n"
                      "约束: ωv ≤ 电流环带宽/5, ωp ≤ ωv/5(保守)~ωv/3。\n"
                      "电流激励模式拟合值为 J/Kt、B/Kt 等效, 对应速度环增益单位 A/(rad/s)。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.hex('muted')};")
        layout.addWidget(note)
        layout.addStretch(1)
        self.tabs.addTab(page, "辨识整定")

    def _build_verify_tab(self):
        """方向二(验证闭环)：带宽/Mr/PM/GM 验收指标。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        self.verify_hint = QLabel("")
        self.verify_hint.setWordWrap(True)
        self.verify_hint.setStyleSheet(f"color:{theme.hex('muted')};")
        layout.addWidget(self.verify_hint)
        self.verify_table = QTableWidget(0, 4)
        self.verify_table.setHorizontalHeaderLabels(["指标", "实测值", "验收线", "判定"])
        self.verify_table.verticalHeader().setVisible(False)
        self.verify_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.verify_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.verify_table)
        note = QLabel("PM/GM 由 L = T/(1−T) 从实测闭环反推: 低频段 T≈1 处 1−T 有效数字不足、"
                      "噪声被放大, 反推结果只在中高频(穿越区附近)可信;\n"
                      "常见验收线: 带宽与设计偏差 ±20% 内、电流环 Mr < 2dB、PM ≥ 45°、GM ≥ 6dB。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.hex('muted')};")
        layout.addWidget(note)
        layout.addStretch(1)
        self.tabs.addTab(page, "验证闭环")

    def _build_bode_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.bode_hint = QLabel("")
        self.bode_hint.setStyleSheet(f"color:{theme.hex('muted')};")
        layout.addWidget(self.bode_hint)
        # 最近一次绘制的有效曲线数据, 供阈值游标交点计算
        self._last_gain_data = ([], [])
        self._last_phase_data = ([], [])
        if pg is None:
            layout.addWidget(QLabel("未安装 pyqtgraph，Bode 数据仍可通过 CSV 导出。"))
            self.bode_gain = self.bode_phase = None
        else:
            self.bode_gain = pg.PlotWidget(title="幅频 |Y/X|")
            self.bode_gain.setLabel("left", "增益", units="dB")
            self.bode_gain.setLabel("bottom", "频率", units="Hz")
            self.bode_gain.setLogMode(x=True, y=False)
            self.bode_gain.showGrid(x=True, y=True, alpha=0.25)
            self._gain_curve = self.bode_gain.plot(pen=pg.mkPen("#5A8DFF", width=2), symbol="o", symbolSize=7)
            self._gain_invalid = self.bode_gain.plot(pen=None, symbol="x",
                                                     symbolPen=pg.mkPen("#8899AA", width=2),
                                                     symbolBrush=None, symbolSize=8)
            self.bode_phase = pg.PlotWidget(title="相频 angle(Y/X)")
            self.bode_phase.setLabel("left", "相位", units="deg")
            self.bode_phase.setLabel("bottom", "频率", units="Hz")
            self.bode_phase.setLogMode(x=True, y=False)
            self.bode_phase.showGrid(x=True, y=True, alpha=0.25)
            self._phase_curve = self.bode_phase.plot(pen=pg.mkPen("#E8B04B", width=2), symbol="o", symbolSize=7)
            # 可调阈值游标: 水平线可鼠标拖动, 数值框可精确输入, 自动标注曲线穿越频率
            cursor_bar = QHBoxLayout()
            cursor_bar.addWidget(QLabel("幅频阈值"))
            self.gain_cursor_spin = QDoubleSpinBox()
            self.gain_cursor_spin.setRange(-120.0, 40.0)
            self.gain_cursor_spin.setDecimals(1)
            self.gain_cursor_spin.setSingleStep(0.5)
            self.gain_cursor_spin.setValue(-3.0)
            self.gain_cursor_spin.setSuffix(" dB")
            cursor_bar.addWidget(self.gain_cursor_spin)
            self.gain_cross_label = QLabel("交点 -- Hz")
            self.gain_cross_label.setStyleSheet(f"color:{theme.hex('muted')};")
            cursor_bar.addWidget(self.gain_cross_label)
            cursor_bar.addSpacing(18)
            cursor_bar.addWidget(QLabel("相频阈值"))
            self.phase_cursor_spin = QDoubleSpinBox()
            self.phase_cursor_spin.setRange(-180.0, 180.0)
            self.phase_cursor_spin.setDecimals(1)
            self.phase_cursor_spin.setSingleStep(1.0)
            self.phase_cursor_spin.setValue(-90.0)
            self.phase_cursor_spin.setSuffix(" °")
            cursor_bar.addWidget(self.phase_cursor_spin)
            self.phase_cross_label = QLabel("交点 -- Hz")
            self.phase_cross_label.setStyleSheet(f"color:{theme.hex('muted')};")
            cursor_bar.addWidget(self.phase_cross_label)
            cursor_bar.addStretch(1)
            bar = QWidget()
            bar.setLayout(cursor_bar)
            layout.addWidget(bar)
            cursor_pen = pg.mkPen("#FF6B6B", width=1, style=Qt.PenStyle.DashLine)
            self._gain_cursor = pg.InfiniteLine(pos=-3.0, angle=0, movable=True,
                                                pen=cursor_pen, label="{value:.1f} dB",
                                                labelOpts={"position": 0.95,
                                                           "color": "#FF6B6B"})
            self.bode_gain.addItem(self._gain_cursor)
            self._phase_cursor = pg.InfiniteLine(pos=-90.0, angle=0, movable=True,
                                                 pen=cursor_pen, label="{value:.1f} °",
                                                 labelOpts={"position": 0.95,
                                                            "color": "#FF6B6B"})
            self.bode_phase.addItem(self._phase_cursor)
            self._gain_cursor.sigPositionChanged.connect(self._on_gain_cursor_moved)
            self.gain_cursor_spin.valueChanged.connect(self._on_gain_spin_changed)
            self._phase_cursor.sigPositionChanged.connect(self._on_phase_cursor_moved)
            self.phase_cursor_spin.valueChanged.connect(self._on_phase_spin_changed)
            layout.addWidget(self.bode_gain, 1)
            layout.addWidget(self.bode_phase, 1)
        self.tabs.addTab(page, "Bode 曲线")

    def _build_raw_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        if pg is None:
            layout.addWidget(QLabel("未安装 pyqtgraph，原始数据可通过 CSV 导出。"))
            self.raw_plot = None
        else:
            self.raw_plot = pg.PlotWidget(title="扫频全周期原始波形")
            self.raw_plot.setLabel("bottom", "时间", units="s")
            self._raw_legend = self.raw_plot.addLegend()
            self.raw_plot.showGrid(x=True, y=True, alpha=0.25)
            # 图例由 _update_raw_legend 按当前模式通道名动态维护, 创建时不带 name
            self._raw_in = self.raw_plot.plot(pen=pg.mkPen("#5A8DFF", width=2))
            self._raw_out = self.raw_plot.plot(pen=pg.mkPen("#E8B04B", width=2))
            # 右轴独立 ViewBox: 输入/输出异量纲时输出曲线挂右轴(双纵坐标)
            self._raw_dual_axis = False  # False=输出曲线在主(左)轴
            self._raw_vb_out = pg.ViewBox()
            self.raw_plot.showAxis("right")
            self.raw_plot.scene().addItem(self._raw_vb_out)
            self.raw_plot.getAxis("right").linkToView(self._raw_vb_out)
            self._raw_vb_out.setXLink(self.raw_plot.getViewBox())
            self.raw_plot.getViewBox().sigResized.connect(self._sync_raw_vb_geometry)
            self._sync_raw_vb_geometry()
            # 目标(输入)曲线绘制在反馈(输出)之上: 闭环增益接近 0dB 时两条曲线
            # 几乎重合, 后创建的反馈曲线会遮挡目标曲线, 导致目标值不可见。
            self._raw_in.setZValue(10)
            # 频点频率标注线: {sweep_point: InfiniteLine}, 随波形分段增量维护
            self._freq_lines = {}
            layout.addWidget(self.raw_plot)
        # 当前原始波形视图(None=全周期, 或频点号); 切回本 Tab 时按此恢复
        self._raw_view_point = None
        self._raw_tab_idx = self.tabs.addTab(page, "原始波形")
        # 控件隐藏期间 pyqtgraph 跳过重绘/自动量程, 双轴 ViewBox 几何同步
        # 也不再触发, 切回后波形视图失效呈现"空白"; 切回时恢复重绘
        self.tabs.currentChanged.connect(self._on_inner_tab_changed)

    def _build_table_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.table = QTableWidget(0, 11)
        self.table.setHorizontalHeaderLabels([
            "点", "实际频率", "输入 RMS", "输出 RMS", "增益 dB", "相位 deg",
            "SNR dB", "样本数", "估计器影响", "有效", "原因",
        ])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.currentCellChanged.connect(self._show_selected_raw)
        layout.addWidget(self.table)
        self.tabs.addTab(page, "频点表")

    def _start(self):
        error = self._prevalidate()
        if error:
            self._set_status(error, "warn")
            return
        if not self.client.is_open():
            self._set_status("启动失败：串口/总线未连接", "danger")
            return
        # 固件要求扫频在 READY 状态启动; 已知电机不在就绪态时提前提示
        # (未收到过状态上报时不拦截, 交由固件应答判定)
        if self._top_fsm is not None and self._top_fsm != int(TopFsm.READY):
            self._set_status("启动失败：电机未处于就绪状态(READY)，请先使能电机", "danger")
            return
        self.table.setRowCount(0)
        self.export_btn.setEnabled(False)
        self.bode_hint.setText("")
        # 新会话清空两个方向结果页
        for lbl in (self.fit_J, self.fit_B, self.fit_corner, self.fit_err,
                    self.fit_points, self.vel_kp, self.vel_ki, self.pos_kp):
            lbl.setText("--")
        self.verify_table.setRowCount(1)
        self.verify_table.setItem(0, 0, QTableWidgetItem("等待扫频数据…"))
        for c in range(1, 4):
            self.verify_table.setItem(0, c, QTableWidgetItem(""))
        if pg is not None:
            self._gain_curve.setData([], [])
            self._gain_invalid.setData([], [])
            self._phase_curve.setData([], [])
            self._raw_in.setData([], [])
            self._raw_out.setData([], [])
            # 新会话频点编号从 0 重新开始, 旧频率标注线必须显式清除
            for line in self._freq_lines.values():
                self.raw_plot.removeItem(line)
            self._freq_lines.clear()
            self._raw_base = None
        # 新会话频点号从 0 重排, 旧的单频点视图状态必须复位
        self._raw_view_point = None
        self._raw_render_key = None
        ok = self.session.start(self.mode.currentData(), self.sweep_mode.currentData(),
                                self.point_cfg.value(), self.f_start.value(),
                                self.f_end.value(), self.amp.value(), self.bias.value())
        if not ok:
            self._set_status("启动失败：参数超出范围（检查频率/幅值/点数）", "danger")

    def _set_status(self, text: str, level: str = "muted"):
        """状态栏文本 + 语义化配色(muted/ok/warn/danger)。"""
        self.status.setText(text)
        self.status.setStyleSheet(f"color:{theme.hex(level)};")

    def _on_state_updated(self, top_fsm: int, *_):
        self._top_fsm = int(top_fsm)

    def _on_motor_info_result(self, param_id: int, _type, value_bytes):
        self._cache_limit(int(param_id), value_bytes)

    def _on_motor_info_bulk(self, _start_id: int, items):
        for pid, value_bytes in items or ():
            self._cache_limit(int(pid), value_bytes)

    def _cache_limit(self, param_id: int, value_bytes):
        if param_id in (43, 44) and value_bytes and len(value_bytes) >= 4:
            try:
                self._limits[param_id] = struct.unpack(
                    '<f', bytes(value_bytes[:4]))[0]
            except (TypeError, ValueError, struct.error):
                pass

    def _prevalidate(self) -> str | None:
        """本地预检(镜像固件 motor_sweep_configure 校验规则), 返回错误文案或 None。

        覆盖可本地判定的规则: 频率范围/编码精度/频点数/预计时长/幅值限幅。
        固件 OUT_OF_RANGE 的所有拒绝路径都能在这里提前给出具体原因。
        """
        test_mode = int(self.mode.currentData())
        sweep_mode = int(self.sweep_mode.currentData())
        f_start = self.f_start.value()
        f_end = self.f_end.value()
        amp = self.amp.value()
        bias = self.bias.value()
        point_cfg = self.point_cfg.value()

        if f_end < f_start:
            return "结束频率必须不小于起始频率"
        if f_end > 1000.0:
            return f"结束频率 {f_end:g} Hz 超过上限 1000 Hz（控制频率 10kHz 的 1/10）"
        if amp <= 0.0:
            return "扫频幅值必须大于 0"
        # 幅值/偏置编码精度: 速度模式 0.01, 电流/力矩模式 0.001, 量化为 0 会被固件拒绝
        scale = 100.0 if test_mode >= 3 else 1000.0
        if int(round(amp * scale)) < 1:
            return f"幅值 {amp:g} 低于编码精度 {1.0 / scale:g}，会被固件拒绝，请增大"
        if int(round(abs(bias) * scale)) > 32767:
            return f"偏置 {bias:g} 超过编码范围 ±{32767.0 / scale:g}"

        # 频点数(镜像固件: 对数 ceil(log10跨度×点数/十倍频)+1, 1Hz步进 ceil(跨度)+1)
        if f_start == f_end:
            count = 1
        elif sweep_mode == int(SweepMode.LOG):
            count = math.ceil(math.log10(f_end / f_start) * point_cfg) + 1
        elif sweep_mode == int(SweepMode.STEP):
            count = math.ceil(f_end - f_start) + 1
        else:
            if point_cfg < 2:
                return "线性模式总频点数至少为 2"
            count = point_cfg
        if count > 1024:
            if sweep_mode == int(SweepMode.LOG):
                tip = "请减少点数/十倍频或收窄频率范围"
            elif sweep_mode == int(SweepMode.STEP):
                tip = "请收窄频率范围（跨度不超过 1023 Hz）"
            else:
                tip = "请减少总频点数"
            return f"频点数 {count} 超过上限 1024，{tip}"

        # 预计总时长(镜像固件: 每频点 (3稳定 + clamp(round(2f),1,8)测量) 个周期)
        total = 0.0
        for i in range(count):
            if i == count - 1 or count == 1:
                f = f_end
            elif sweep_mode == int(SweepMode.LINEAR):
                f = f_start + (f_end - f_start) * i / (count - 1)
            elif sweep_mode == int(SweepMode.LOG):
                f = min(f_end, f_start * 10.0 ** (i / point_cfg))
            else:
                f = f_start + i
            cycles = max(1, min(8, int(2.0 * f + 0.5)))
            total += (3 + cycles) / f
        if total > 600.0:
            return f"预计扫频时长约 {total:.0f}s 超过上限 600s，请收窄频率范围或减少频点"

        # 幅值限幅(已缓存限幅时): 固件规则 |偏置|+幅值 ≤ 模式对应电机限幅
        limit_id, limit_name, unit = self._LIMIT_INFO[test_mode]
        if limit_id is not None:
            limit = self._limits.get(limit_id)
            if limit is not None and abs(bias) + amp > limit:
                return (f"|偏置|+幅值 = {abs(bias) + amp:.4g} {unit} 超过电机{limit_name}"
                        f" {limit:g} {unit}，请减小偏置/幅值，或到电机配置面板调大{limit_name}")
        return None

    def _fw_version_diagnosis(self) -> str | None:
        """OUT_OF_RANGE 后的固件版本诊断: 旧固件会把 12B 偏置载荷按
        legacy float 解析而必然拒绝(0x02), 此时参数本身再合法也无济于事。
        固件 0xD0 应答携带协议版本, v1.4+ 才支持 12B 载荷与速度闭环扫频。"""
        ver = getattr(self.client, "fw_proto_version", None)
        if ver is None:
            return ("未获取到固件协议版本(重连一次可自动查询 0xD0)。"
                    "若固件不是最新编译产物(v1.4+)，本命令必被拒绝")
        major, minor = ver
        if (major, minor) < (1, 4):
            return (f"检测到固件协议版本 v{major}.{minor} < v1.4，"
                    f"不支持偏置扫频/速度闭环模式，请重新编译并烧录最新固件")
        return None  # 固件为 v1.4+，问题在参数侧，交给限幅诊断

    def _limit_diagnosis(self) -> str | None:
        """OUT_OF_RANGE 后的限幅诊断: 用缓存的电机限幅解释拒绝原因。"""
        test_mode = int(self.mode.currentData())
        limit_id, limit_name, unit = self._LIMIT_INFO[test_mode]
        amp = self.amp.value()
        bias = self.bias.value()
        total = abs(bias) + amp
        if limit_id is None:
            # 力矩模式: 峰值转矩是编译期参数(motor_profile.h), 不在 motor_info 体系
            return (f"力矩模式限幅为峰值转矩(编译期参数, 上位机不可读, "
                    f"默认配置 0.5 Nm)。当前 |偏置|+幅值={total:.4g} Nm, "
                    f"超过峰值转矩则固件拒绝; 也可改用【辨识】电流→速度模式"
                    f"(限幅=峰值电流, 可读可配)")
        limit = self._limits.get(limit_id)
        if limit is None:
            return (f"未缓存电机{limit_name}(打开电机配置面板读取一次即可)，"
                    f"请核对 |偏置|+幅值={total:.4g} {unit} 是否超过电机{limit_name}")
        if total > limit:
            return (f"当前 |偏置|({abs(bias):g})+幅值({amp:g})={total:.4g} {unit} "
                    f"超过电机{limit_name} {limit:g} {unit}")
        return (f"已核对: |偏置|+幅值={total:.4g} 未超过{limit_name} {limit:g} {unit}，"
                f"本地校验均已通过仍被拒绝，请确认固件为最新编译产物")

    def _on_status(self, state: str, data: dict):
        point = data.get("point")
        total = data.get("point_count", self.session.point_count)
        suffix = f" 频点 {int(point) + 1}/{total}" if point is not None else ""
        duration = data.get("duration_x100")
        estimate = f" 预计 {float(duration) / 100.0:.1f}s" if duration else ""
        self._set_status(f"{self._STATE_CN.get(state, state)}{suffix}{estimate}",
                         "ok" if state == BodeSession.COMPLETE else
                         ("danger" if state == BodeSession.ERROR else "muted"))
        active = self.session.active
        self.start_btn.setEnabled(not active)
        self.stop_btn.setEnabled(active)
        if state == BodeSession.COMPLETE:
            self.export_btn.setEnabled(True)

    def _on_point(self, result):
        if not hasattr(result, "point"):
            return
        self._refresh_direction_results()
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [
            result.point, f"{result.f_actual_hz:.3f}", f"{result.input_rms:.6g}",
            f"{result.output_rms:.6g}", f"{result.gain_db:.3f}",
            f"{result.phase_deg:.3f}", f"{result.snr_db:.2f}",
            result.sample_count, result.estimator_warning,
            "是" if result.valid else "否", result.valid_reason,
        ]
        for col, value in enumerate(values):
            self.table.setItem(row, col, QTableWidgetItem(str(value)))
        self._refresh_curves()
        # 扫频进行中只刷滚动窗口: 全量重采样是 O(总样本), 长扫频逐频点
        # 全量渲染累计成本 O(n^2); 全周期视图留给 _on_finished 一次渲染
        self._show_raw(streaming=True)

    def _update_diagnosis(self, points):
        """在 Bode 图顶部给出有效性诊断：有效点数 + 无效频点原因分布。"""
        total = len(points)
        if total == 0:
            self.bode_hint.setText("")
            return
        valid = [p for p in points if p.valid]
        if len(valid) == total:
            self.bode_hint.setText(f"共 {total} 个频点，全部有效")
            self.bode_hint.setStyleSheet(f"color:{theme.hex('ok')};")
            return
        from collections import Counter
        reason_count = Counter()
        bad_points = []
        for p in points:
            if p.valid:
                continue
            bad_points.append(int(p.point))
            for reason in str(p.valid_reason).split(";"):
                reason = reason.strip()
                if reason and reason != "未完成":
                    reason_count[reason] += 1
        reasons = "、".join(f"{k}×{v}" for k, v in reason_count.most_common(3))
        pt_text = ",".join(str(i) for i in bad_points[:8])
        if len(bad_points) > 8:
            pt_text += "…"
        self.bode_hint.setText(f"{len(valid)}/{total} 点有效；无效频点 {pt_text}：{reasons}")
        self.bode_hint.setStyleSheet(f"color:{theme.hex('warn')};")

    # ---------- 阈值游标: 拖动/输入双向同步 + 交点频率计算 ----------
    def _on_gain_cursor_moved(self, *_):
        val = float(self._gain_cursor.value())
        self.gain_cursor_spin.blockSignals(True)
        self.gain_cursor_spin.setValue(val)
        self.gain_cursor_spin.blockSignals(False)
        self._update_cursor_crossings()

    def _on_gain_spin_changed(self, val):
        self._gain_cursor.setPos(float(val))
        self._update_cursor_crossings()

    def _on_phase_cursor_moved(self, *_):
        val = float(self._phase_cursor.value())
        self.phase_cursor_spin.blockSignals(True)
        self.phase_cursor_spin.setValue(val)
        self.phase_cursor_spin.blockSignals(False)
        self._update_cursor_crossings()

    def _on_phase_spin_changed(self, val):
        self._phase_cursor.setPos(float(val))
        self._update_cursor_crossings()

    @staticmethod
    def _crossing_freq(fs, ys, level):
        """曲线自左向右首次穿越 level 的频率(相邻点线性插值), 无穿越返回 None。"""
        for i in range(1, len(fs)):
            y0, y1 = ys[i - 1], ys[i]
            if y0 != y1 and (y0 - level) * (y1 - level) <= 0.0:
                t = (level - y0) / (y1 - y0)
                return fs[i - 1] + t * (fs[i] - fs[i - 1])
        return None

    def _update_cursor_crossings(self):
        gain_fs, gain_ys = self._last_gain_data
        f = self._crossing_freq(gain_fs, gain_ys, float(self.gain_cursor_spin.value()))
        self.gain_cross_label.setText(f"交点 {f:.3g} Hz" if f else "交点 -- Hz")
        phase_fs, phase_ys = self._last_phase_data
        f = self._crossing_freq(phase_fs, phase_ys, float(self.phase_cursor_spin.value()))
        self.phase_cross_label.setText(f"交点 {f:.3g} Hz" if f else "交点 -- Hz")

    def _refresh_curves(self):
        if self.session.analyzer is None:
            return
        points = self.session.analyzer.ordered_points()
        self._update_diagnosis(points)
        if pg is None or self.bode_gain is None:
            return
        valid = [p for p in points
                 if p.valid and math.isfinite(p.gain_db) and math.isfinite(p.phase_deg)]
        invalid = [p for p in points
                   if not p.valid and math.isfinite(p.gain_db)]
        self._gain_curve.setData([p.f_actual_hz for p in valid],
                                 [p.gain_db for p in valid])
        self._gain_invalid.setData([p.f_actual_hz for p in invalid],
                                   [p.gain_db for p in invalid])
        phases = []
        last = None
        offset = 0.0
        for p in valid:
            phase = p.phase_deg
            if last is not None:
                while phase + offset - last > 180:
                    offset -= 360
                while phase + offset - last < -180:
                    offset += 360
            phases.append(phase + offset)
            last = phases[-1]
        phase_fs = [p.f_actual_hz for p in valid]
        self._phase_curve.setData(phase_fs, phases)
        # 供阈值游标交点计算(相位用展开后的连续曲线)
        self._last_gain_data = ([p.f_actual_hz for p in valid],
                                [p.gain_db for p in valid])
        self._last_phase_data = (phase_fs, phases)
        self._update_cursor_crossings()

    _RAW_MAX_BUCKETS = 4000  # 下采样桶数上限(每桶保留输入/输出极值, 共≤4点/桶)
    # 流式刷新滚动窗口: 扫频进行中只对最近样本做下采样, 成本恒定。
    # 百万级全周期样本若逐包全量重算(每桶4次min/max), 10Hz刷新必然卡死。
    _STREAM_WINDOW = 120_000  # 2kHz≈60s / 10kHz≈12s 的滚动窗口

    @classmethod
    def _downsample_rows(cls, rows):
        """min-max 分桶下采样: 每桶保留输入/输出各自的极值点。

        长扫频全周期可达数十万~百万样本, 直接 setData 会卡顿;
        均匀抽取会混叠丢失尖峰, 这里按桶保留极值, 波形包络不失真。
        每桶单次遍历同时求 4 个极值(输入/输出 × min/max)。
        """
        n = len(rows)
        if n <= cls._RAW_MAX_BUCKETS:
            return rows
        out = []
        bucket = n / cls._RAW_MAX_BUCKETS
        for k in range(cls._RAW_MAX_BUCKETS):
            lo = int(k * bucket)
            hi = max(lo + 1, int((k + 1) * bucket))
            if lo >= n:
                break
            seg = rows[lo:hi]
            if len(seg) == 1:
                out.append(seg[0])
                continue
            i_min_r = i_max_r = o_min_r = o_max_r = seg[0]
            i_min = i_max = float(seg[0].get("input") or 0.0)
            o_min = o_max = float(seg[0].get("output") or 0.0)
            for r in seg:
                iv = float(r.get("input") or 0.0)
                if iv < i_min:
                    i_min, i_min_r = iv, r
                elif iv > i_max:
                    i_max, i_max_r = iv, r
                ov = float(r.get("output") or 0.0)
                if ov < o_min:
                    o_min, o_min_r = ov, r
                elif ov > o_max:
                    o_max, o_max_r = ov, r
            for r in (i_min_r, i_max_r, o_min_r, o_max_r):
                if not out or r is not out[-1]:
                    out.append(r)
        out.sort(key=lambda r: int(r.get("sample_index") or 0))
        return out

    def _show_raw(self, point=None, streaming=False):
        if pg is None or self.raw_plot is None:
            return
        self._raw_view_point = point
        rows = self.session.raw_samples
        if point is not None:
            rows = [r for r in rows if r.get("sweep_point") == point]
        if not rows:
            return
        # 渲染键: 恢复显示时数据未变则跳过重采样(百万样本约0.8s),
        # 只需恢复视图几何与量程(隐藏期间 pyqtgraph 跳过了这些更新)
        self._raw_render_key = (point, len(rows))
        # 流式刷新只处理尾部滚动窗口: 全周期百万样本逐包全量重算会卡死 UI,
        # 窗口外数据在扫频结束后(_on_finished, streaming=False)全量展示一次
        windowed = streaming and point is None and len(rows) > self._STREAM_WINDOW
        if windowed:
            rows = rows[-self._STREAM_WINDOW:]
        shown = self._downsample_rows(rows)
        rate = max(1, int(rows[0].get("actual_rate_hz") or 1))
        base = int(rows[0].get("sample_index") or 0)
        # 时间零点变化(流式窗口滚动/视图切换): 旧频率标注线坐标全部失效, 清空重画
        if base != getattr(self, "_raw_base", None):
            for line in self._freq_lines.values():
                self.raw_plot.removeItem(line)
            self._freq_lines.clear()
            self._raw_base = base
        ts = [(int(r.get("sample_index") or 0) - base) / rate for r in shown]
        self._raw_in.setData(ts, [float(r.get("input") or 0.0) for r in shown])
        self._raw_out.setData(ts, [float(r.get("output") or 0.0) for r in shown])
        self._update_freq_markers(rows, base, rate)
        if point is not None:
            self.raw_plot.setTitle(f"频点 {int(point)} 测量段")
        elif windowed:
            self.raw_plot.setTitle(
                f"实时波形（最近 {len(rows) / rate:.0f}s，结束后显示全周期）")
        else:
            self.raw_plot.setTitle("扫频全周期原始波形")

    def _update_freq_markers(self, rows, base: int, rate: int):
        """按频点切换时间画频率标注线: 竖直点线分隔各频点段, 顶部标激励频率。

        频率取自各段样本携带的 f_actual_mhz 包元数据(目标激励的实际频率)。
        流式刷新时增量维护: 新频点补线, 视图切换时移除多余线, 避免 10Hz
        重绘反复重建上千个图元。频点多时标签自动稀疏防止重叠。
        """
        segs = []  # [(sweep_point, t_start, freq_hz)]
        last_pt = object()
        for r in rows:
            pt = r.get("sweep_point")
            if pt == last_pt:
                continue
            t = (int(r.get("sample_index") or 0) - base) / rate
            mhz = r.get("f_actual_mhz")
            segs.append((pt, t, float(mhz) / 1000.0 if mhz else None))
            last_pt = pt
        wanted = {pt for pt, _, _ in segs}
        for pt in list(self._freq_lines):
            if pt not in wanted:
                self.raw_plot.removeItem(self._freq_lines.pop(pt))
        label_step = max(1, len(segs) // 12)
        for i, (pt, t, freq) in enumerate(segs):
            if pt in self._freq_lines:
                continue
            show_label = freq is not None and (i % label_step == 0 or i == len(segs) - 1)
            line = pg.InfiniteLine(
                pos=t, angle=90, movable=False,
                pen=pg.mkPen(QColor(136, 153, 170, 128), width=1,
                             style=Qt.PenStyle.DotLine),
                label=f"{freq:.3g} Hz" if show_label else None,
                labelOpts={"position": 0.98,
                           "color": QColor(136, 153, 170, 128)})
            self.raw_plot.addItem(line)
            self._freq_lines[pt] = line

    # ---------- 方向一/方向二 结果刷新 ----------
    def _on_mode_changed(self, *_):
        """切换测试模式时更新两个结果 Tab 的方向提示。"""
        mode = int(self.mode.currentData())
        if mode in self._IDENT_MODES:
            self.ident_hint.setText("当前模式属于方向一(辨识整定): 被整定环断开、内环闭合, "
                                    "测量结果用于换算 PID 初值。扫频完成后本页自动拟合。")
        elif mode in self._VERIFY_MODES:
            self.ident_hint.setText("当前模式属于方向二(验证闭环), 本页不适用——"
                                    "请切换到【辨识】开头的测试模式, 或查看\"验证闭环\"页。")
        else:
            self.ident_hint.setText("")
        if mode in self._VERIFY_MODES:
            self.verify_hint.setText("当前模式属于方向二(验证闭环): 全环闭合、给定端激励, "
                                     "测量结果用于判定带宽与稳定裕度。扫频完成后本页自动计算。")
        elif mode in self._IDENT_MODES:
            self.verify_hint.setText("当前模式属于方向一(辨识整定), 本页不适用——"
                                     "请切换到【验证】开头的测试模式。")
        else:
            self.verify_hint.setText("")
        self._refresh_direction_results()

    def _refresh_direction_results(self):
        """按当前模式刷新辨识/验证两个结果页(非对应方向显示提示不计算)。"""
        analyzer = self.session.analyzer
        points = list(analyzer.points.values()) if analyzer else []
        if int(self.mode.currentData()) in self._IDENT_MODES:
            self._refresh_ident_results(points)
        if int(self.mode.currentData()) in self._VERIFY_MODES:
            self._refresh_verify_results(points)

    def _refresh_ident_results(self, points=None):
        """方向一: J/B 最小二乘拟合 + 带宽法 PID 换算建议。"""
        if points is None:  # 目标带宽输入变化时用已有数据重算
            analyzer = self.session.analyzer
            points = list(analyzer.points.values()) if analyzer else []
        fit = fit_mechanical(points) if points else None
        if fit is not None:
            self.fit_J.setText(f"{fit['J']:.6g}")
            self.fit_B.setText(f"{fit['B']:.6g}")
            self.fit_corner.setText(f"{fit['corner_hz']:.4g} Hz")
            err_style = "color:" + theme.hex(
                "ok" if fit["fit_err_pct"] < 15.0 else "warn") + ";"
            self.fit_err.setText(f"{fit['fit_err_pct']:.1f} %")
            self.fit_err.setStyleSheet(err_style)
            self.fit_points.setText(str(fit["point_count"]))
        else:
            valid_n = sum(1 for p in points if p.valid)
            tip = "--" if not points else \
                f"有效频点不足({valid_n}/至少3), 等待更多频点完成"
            for lbl in (self.fit_J, self.fit_B, self.fit_corner,
                        self.fit_err, self.fit_points):
                lbl.setText(tip)
        # PID 换算: Kp_v = ωv·Ĵ, Ki_v = Kp_v·ωv/4; Kp_p = ωp (与 J 无关)
        wv = 2.0 * math.pi * self.vel_bw.value()
        wp = 2.0 * math.pi * self.pos_bw.value()
        if fit is not None:
            kp_v = wv * fit["J"]
            self.vel_kp.setText(f"{kp_v:.6g}")
            self.vel_ki.setText(f"{kp_v * wv / 4.0:.6g}")
        else:
            self.vel_kp.setText("--")
            self.vel_ki.setText("--")
        self.pos_kp.setText(f"{wp:.6g}")

    def _refresh_verify_results(self, points=None):
        """方向二: -3dB 带宽 / Mr / PM / GM 验收指标表。"""
        if points is None:
            analyzer = self.session.analyzer
            points = list(analyzer.points.values()) if analyzer else []
        metrics = closed_loop_metrics(points) if points else None
        rows = []
        if metrics is not None:
            rows = [
                ("−3dB 带宽", f"{metrics['bandwidth_hz']:.4g} Hz"
                 if metrics["bandwidth_hz"] else "未穿越(带宽高于扫频范围)",
                 "与设计值偏差 ±20% 内", None),
                ("闭环峰值 Mr", f"{metrics['peak_db']:.2f} dB"
                 if metrics["peak_db"] is not None else "无峰值",
                 "电流环 < 2 dB", None),
                ("相位裕度 PM", f"{metrics['pm_deg']:.1f}°"
                 if metrics["pm_deg"] is not None else "--",
                 "≥ 45°", metrics["pm_deg"] is not None and metrics["pm_deg"] >= 45.0),
                ("增益裕度 GM", f"{metrics['gm_db']:.2f} dB"
                 if metrics["gm_db"] is not None else "--",
                 "≥ 6 dB", metrics["gm_db"] is not None and metrics["gm_db"] >= 6.0),
            ]
        table = self.verify_table
        table.setRowCount(len(rows))
        for r, (name, value, line, passed) in enumerate(rows):
            verdict, color = "--", theme.hex("muted")
            if passed is True:
                verdict, color = "PASS", theme.hex("ok")
            elif passed is False:
                verdict, color = "FAIL", theme.hex("danger")
            items = (QTableWidgetItem(name), QTableWidgetItem(value),
                     QTableWidgetItem(line), QTableWidgetItem(verdict))
            items[3].setForeground(QBrush(QColor(color)))
            items[3].setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            for c, item in enumerate(items):
                table.setItem(r, c, item)
        if not rows:
            valid_n = sum(1 for p in points if p.valid)
            table.setRowCount(1)
            tip = "等待扫频数据…" if not points else \
                f"有效频点不足({valid_n}/至少3), 等待更多频点完成"
            table.setItem(0, 0, QTableWidgetItem(tip))
            table.setItem(0, 1, QTableWidgetItem(""))
            table.setItem(0, 2, QTableWidgetItem(""))
            table.setItem(0, 3, QTableWidgetItem(""))

    def _on_raw_update(self, point):
        # 实时流式刷新：只显示尾部滚动窗口(_STREAM_WINDOW), 成本恒定;
        # 全周期(可能百万样本)留到扫频结束后 _on_finished 全量下采样一次。
        # 限频 2Hz: 实时波形只需观察趋势, 更快刷新徒增 CPU 占用。
        now = time.monotonic()
        if now - self._last_raw_refresh >= 0.5:
            self._last_raw_refresh = now
            self._show_raw(streaming=True)

    def _show_selected_raw(self, row, *_):
        if row >= 0:
            item = self.table.item(row, 0)
            if item is not None:
                self._show_raw(int(item.text()))

    def _on_inner_tab_changed(self, index: int):
        """内层 Tab 切回"原始波形"页时恢复波形显示。"""
        if index == getattr(self, "_raw_tab_idx", -1):
            self._restore_raw_view()

    def showEvent(self, event):
        """主窗口 Tab 切回本面板时恢复原始波形显示。"""
        super().showEvent(event)
        self._restore_raw_view()

    def _restore_raw_view(self):
        """重新可见时重绘原始波形: 控件隐藏期间 pyqtgraph 跳过重绘与
        自动量程更新, 双轴 ViewBox 的几何同步也不再触发, 直接切回会
        呈现"波形消失"; 按最后视图状态(全周期/单频点)重绘并恢复量程。"""
        if pg is None or self.raw_plot is None:
            return
        # 仅当内层"原始波形"页可见时才恢复, 其余页无绘制需求
        if getattr(self, "_raw_tab_idx", -1) != self.tabs.currentIndex():
            return
        if not self.session.raw_samples:
            return
        self._sync_raw_vb_geometry()
        # 扫频进行中沿用流式滚动窗口(成本恒定), 结束后全量展示;
        # 数据未变化时跳过重采样, 曲线数据仍在, 只恢复视图量程即可
        point = getattr(self, "_raw_view_point", None)
        rows = self.session.raw_samples
        if point is not None:
            rows = [r for r in rows if r.get("sweep_point") == point]
        if (point, len(rows)) != getattr(self, "_raw_render_key", None):
            self._show_raw(point, streaming=self.session.active)
        self.raw_plot.getViewBox().autoRange()
        if getattr(self, "_raw_dual_axis", False):
            self._raw_vb_out.autoRange()

    def _on_finished(self, success: bool, message: str):
        # OUT_OF_RANGE 时先排查固件版本(旧固件必拒 12B 载荷), 再做限幅归因
        if not success and "参数超出范围" in message:
            extra = self._fw_version_diagnosis() or self._limit_diagnosis()
            if extra:
                message = f"{message}。{extra}"
            # 版本未知时补查一次 0xD0, 下次诊断即可显示具体版本号
            if getattr(self.client, "fw_proto_version", None) is None:
                self.client.query_dev_info()
        self._set_status(message, "ok" if success else "danger")
        self.export_btn.setEnabled(bool(success and self.session.analyzer and
                                        self.session.analyzer.points))
        # 扫频结束强制刷新一次：避免节流导致最后一段波形/Bode 曲线未绘制。
        self._refresh_curves()
        self._show_raw()
        self._refresh_direction_results()

    def _update_raw_legend(self, spec):
        """按当前测试模式刷新原始波形图例为中文通道名。"""
        legend = getattr(self, "_raw_legend", None)
        if pg is None or self.raw_plot is None or legend is None:
            return
        legend.clear()
        legend.addItem(self._raw_in,
                       f"{spec['input_label']} ({spec['input']}, {spec['input_unit']})")
        legend.addItem(self._raw_out,
                       f"{spec['output_label']} ({spec['output']}, {spec['output_unit']})")

    def _sync_raw_vb_geometry(self, *_):
        """右轴独立 ViewBox 与主绘图区几何同步(pyqtgraph 双轴必需)。"""
        vb_out = getattr(self, "_raw_vb_out", None)
        if vb_out is not None and self.raw_plot is not None:
            vb_out.setGeometry(self.raw_plot.getViewBox().sceneBoundingRect())

    def _apply_raw_axis_layout(self, spec):
        """原始波形纵轴布局: 输入/输出同量纲(如速度→速度)共用左轴;
        异量纲(如电流→速度)输出曲线挂右轴双纵坐标, 两条曲线各自量程。"""
        if pg is None or self.raw_plot is None:
            return
        dual = spec["input_unit"] != spec["output_unit"]
        if dual != getattr(self, "_raw_dual_axis", False):
            self._raw_dual_axis = dual
            if dual:
                self.raw_plot.removeItem(self._raw_out)
                self._raw_vb_out.addItem(self._raw_out)
            else:
                self._raw_vb_out.removeItem(self._raw_out)
                self.raw_plot.addItem(self._raw_out)
        if dual:
            self.raw_plot.showAxis("right")
            self.raw_plot.setLabel("left", spec["input_label"],
                                   units=spec["input_unit"])
            self.raw_plot.getAxis("right").setLabel(
                spec["output_label"], units=spec["output_unit"])
        else:
            self.raw_plot.hideAxis("right")
            self.raw_plot.setLabel(
                "left", f"{spec['input_label']}/{spec['output_label']}",
                units=spec["input_unit"])

    def _sync_parameter_labels(self, *_):
        mode = int(self.mode.currentData())
        # 模式切换 = 注入通道量纲改变(rad/s/Nm/A/rad), 残留旧值必然误导,
        # 重置为该模式安全默认值(均低于对应电机限幅, 固件校验必通过)
        _SAFE_DEFAULTS = {
            int(SweepTestMode.TORQUE_TO_VELOCITY): (0.001, 0.0),
            int(SweepTestMode.CURRENT_TO_VELOCITY): (0.5, 0.0),
            int(SweepTestMode.CURRENT_TO_CURRENT): (0.5, 0.0),
            int(SweepTestMode.VELOCITY_TO_POSITION): (1.0, 0.0),
            int(SweepTestMode.VELOCITY_TO_VELOCITY): (0.5, 0.0),
            int(SweepTestMode.POSITION_TO_POSITION): (0.1, 0.0),
        }
        if mode in _SAFE_DEFAULTS:
            safe_amp, safe_bias = _SAFE_DEFAULTS[mode]
            if self.amp.value() != safe_amp:
                self.amp.setValue(safe_amp)
            if self.bias.value() != safe_bias:
                self.bias.setValue(safe_bias)
        if mode == int(SweepTestMode.VELOCITY_TO_POSITION):
            self.amp_label.setText("速度幅值 (rad/s)")
            self.bias_label.setText("速度直流偏置 (rad/s)")
            self.amp.setMaximum(655.35)
            self.bias.setMaximum(327.67)
            self.bias.setMinimum(-327.68)
        elif mode == int(SweepTestMode.VELOCITY_TO_VELOCITY):
            self.amp_label.setText("速度扰动幅值 (rad/s)")
            self.bias_label.setText("速度直流偏置 (rad/s)")
            self.amp.setMaximum(655.35)
            self.bias.setMaximum(327.67)
            self.bias.setMinimum(-327.68)
        elif mode == int(SweepTestMode.TORQUE_TO_VELOCITY):
            self.amp_label.setText("力矩幅值 (Nm)")
            self.bias_label.setText("力矩直流偏置 (Nm)")
            self.amp.setMaximum(65.535)
            self.bias.setMaximum(32.767)
            self.bias.setMinimum(-32.768)
        elif mode == int(SweepTestMode.POSITION_TO_POSITION):
            self.amp_label.setText("位置幅值 (rad)")
            self.bias_label.setText("位置偏置 (rad)")
            self.amp.setMaximum(655.35)
            self.bias.setMaximum(327.67)
            self.bias.setMinimum(-327.68)
        else:
            self.amp_label.setText("电流幅值 (A)")
            self.bias_label.setText("电流直流偏置 (A)")
            self.amp.setMaximum(65.535)
            self.bias.setMaximum(32.767)
            self.bias.setMinimum(-32.768)
        sweep_mode = int(self.sweep_mode.currentData())
        if sweep_mode == int(SweepMode.LOG):
            self.point_cfg_label.setText("点数 / 十倍频")
            self.point_cfg.setEnabled(True)
        elif sweep_mode == int(SweepMode.STEP):
            self.point_cfg_label.setText("频点 (步进 1Hz 自动)")
            self.point_cfg.setEnabled(False)
        else:
            self.point_cfg_label.setText("总频点数")
            self.point_cfg.setEnabled(True)
        spec = BodeSession.channel_spec(mode)
        if pg is not None:
            self.bode_gain.setTitle(f"幅频 {spec['transfer']}")
            self.bode_phase.setTitle(f"相频 {spec['transfer']}")
            # 先切轴布局再刷图例: 曲线在主图/右轴 ViewBox 间移动会注销图例项
            self._apply_raw_axis_layout(spec)
            self._update_raw_legend(spec)

    def _export(self):
        directory = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if directory:
            paths = self.session.export(directory)
            if paths:
                self._set_status(f"已导出 {Path(paths[0]).name}", "ok")

    def get_opts(self):
        return {
            "test_mode": int(self.mode.currentData()),
            "sweep_mode": int(self.sweep_mode.currentData()),
            "point_cfg": self.point_cfg.value(), "f_start": self.f_start.value(),
            "f_end": self.f_end.value(), "amp": self.amp.value(),
            "bias": self.bias.value(),
        }

    def set_opts(self, opts):
        if not isinstance(opts, dict):
            return
        for combo, key in ((self.mode, "test_mode"), (self.sweep_mode, "sweep_mode")):
            if key in opts:
                idx = combo.findData(int(opts[key]))
                if idx >= 0:
                    combo.setCurrentIndex(idx)
        if "point_cfg" in opts:
            self.point_cfg.setValue(int(opts["point_cfg"]))
        for widget, key in ((self.f_start, "f_start"),
                            (self.f_end, "f_end"), (self.amp, "amp"),
                            (self.bias, "bias")):
            if key in opts:
                widget.setValue(float(opts[key]))

    def apply_theme(self):
        self.status.setStyleSheet(f"color:{theme.hex('muted')};")
