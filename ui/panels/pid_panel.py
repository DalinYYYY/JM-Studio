"""
PID 整定面板 (顶级 Tab)。

对标"电机标定" Tab 的三段式布局:
  1. 状态卡 — 连接状态 / 三环 source 徽章 / 最近操作
  2. 操作区 — 左:理论估计(0xA0) 右:来源切换(0xA1)
  3. 实时调试区 — 0xA5 写 / 0xA6 读 单个PID参数 (需对应环 source=DEBUG)
  4. 结果/历史 Splitter — ControlParam 参数表 + 操作历史

信号:
  pid_autotune_requested(ring_mask, cur_bw, vel_bw, pos_bw) — 主窗口连 client.pid_autotune
  pid_source_set_requested(ring_select, source)              — 主窗口连 client.pid_source_set
  pid_param_set_requested(ring, param_type, value)           — 主窗口连 client.pid_param_set
  pid_param_get_requested(ring, param_type)                  — 主窗口连 client.pid_param_get

IDLE 态守卫: 0xA0/0xA1 仅 TOP_FSM_IDLE(=3) 可执行。0xA5/0xA6 无状态限制。
"""
from collections import deque
from datetime import datetime
from enum import IntEnum

from PyQt6.QtCore import pyqtSignal, Qt, QTimer
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
    QPushButton, QSplitter, QTextEdit, QCheckBox, QDoubleSpinBox,
    QButtonGroup, QRadioButton, QSizePolicy, QComboBox, QSlider,
    QWidget, QScrollArea, QGridLayout,
)

from jmproto.cmd_def import JmCmd, TopFsm
from ui.theme import theme


# ---- 三环 source 枚举 (与固件 motor_pid_load.h pid_source_e 一致) ----
class PidSource(IntEnum):
    DEFAULT = 0    # motor_param.c 默认值
    FLASH = 1      # Flash ControlParam 工程值
    AUTOTUNE = 2   # 理论估计值
    DEBUG = 3      # 调试值: 0xA5 下发的参数 (不持久化, 重启消失)


# ---- 三环标识 (与固件 pid_ring_e 一致) ----
class PidRing(IntEnum):
    CURRENT = 0
    VELOCITY = 1
    POSITION = 2


_RING_CN = {PidRing.CURRENT: "电流环", PidRing.VELOCITY: "速度环", PidRing.POSITION: "位置环"}
_SOURCE_CN = {PidSource.DEFAULT: "默认", PidSource.FLASH: "Flash", PidSource.AUTOTUNE: "理论估计", PidSource.DEBUG: "调试"}
_SOURCE_COLOR_KEY = {PidSource.DEFAULT: "muted", PidSource.FLASH: "warn", PidSource.AUTOTUNE: "accent", PidSource.DEBUG: "ok"}


class PidPanel(QGroupBox):
    """PID 整定面板 (顶级 Tab)。"""

    # ---- 请求信号 (主窗口连接到 JmClient) ----
    pid_autotune_requested = pyqtSignal(int, float, float, float)  # ring_mask(bit0=电流 bit1=速度 bit2=位置), cur_bw, vel_bw, pos_bw
    pid_source_set_requested = pyqtSignal(int, int)                # ring_select, source
    pid_param_set_requested = pyqtSignal(int, int, float)          # ring, param_type, value (0xA5)
    pid_param_get_requested = pyqtSignal(int, int)                 # ring, param_type (0xA6)

    _HISTORY_MAX = 200

    def __init__(self, registry=None, parent=None):
        super().__init__("", parent)
        self._panel_name = "PID 整定"
        self._registry = registry
        self._link_active = False
        self._current_top_fsm = None
        # 三环 source 状态 (连接后由 0xA2 查询回填, 本地仅做缓存)
        self._ring_source = {r: PidSource.DEFAULT for r in PidRing}
        # 操作历史
        self._history = deque(maxlen=self._HISTORY_MAX)
        # 内嵌参数面板引用 (由主窗口 attach_params_panel 注入)
        self._params_panel = None
        # DEBUG 滑条组: ring(0/1/2/3) -> _SliderGroup 实例
        self._debug_slider_groups = {}
        # 写入后延迟回读定时器: 每个 (ring, param_type) 一个单次 QTimer
        self._readback_timers = {}
        # 构建UI
        self._build()
        self._refresh_status()

    # ========================================================================
    #  UI 构建
    # ========================================================================
    def _build(self):
        """整个 PID 整定界面用 QScrollArea 包裹, 内容超出时整体滚动。"""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._main_scroll = QScrollArea()
        self._main_scroll.setWidgetResizable(True)
        self._main_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._main_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._main_scroll.setStyleSheet(self._main_scroll_qss())

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self._build_status_card(layout)
        self._build_operation_panel(layout)
        self._build_debug_slider_area(layout)
        self._build_results_history_split(layout)

        self._main_scroll.setWidget(content)
        outer.addWidget(self._main_scroll)

    def _main_scroll_qss(self) -> str:
        return (f"QScrollArea{{background:transparent; border:none;}}"
                f"QScrollBar:vertical{{background:transparent; width:10px; margin:0;}}"
                f"QScrollBar::handle:vertical{{background:{theme.hex('scroll_handle')};"
                f"border-radius:4px; min-height:24px;}}"
                f"QScrollBar::handle:vertical:hover{{background:{theme.hex('accent')};}}"
                f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}"
                f"QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{{background:transparent;}}")

    def _build_status_card(self, parent_layout):
        """状态卡: 左(连接状态 + 最近操作) 右(三环 source 徽章)。"""
        card = QFrame()
        card.setObjectName("statusCard")
        h = QHBoxLayout(card)
        h.setContentsMargins(12, 8, 12, 8)
        h.setSpacing(12)

        # 左: 连接状态 + 最近操作
        left = QVBoxLayout()
        left.setSpacing(2)
        self._status_label = QLabel("未连接")
        self._status_label.setStyleSheet(
            f"font-weight:bold; color:{theme.hex('text_strong')};")
        self._last_op_label = QLabel("就绪")
        self._last_op_label.setStyleSheet(f"color:{theme.hex('muted')};")
        left.addWidget(self._status_label)
        left.addWidget(self._last_op_label)
        h.addLayout(left, 1)

        # 右: 三环 source 徽章
        right = QHBoxLayout()
        right.setSpacing(8)
        self._source_labels = {}
        for ring in PidRing:
            vbox = QVBoxLayout()
            vbox.setSpacing(0)
            name_lbl = QLabel(_RING_CN[ring])
            name_lbl.setStyleSheet(
                f"color:{theme.hex('muted')}; font-size:11px;")
            name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            src_lbl = QLabel(_SOURCE_CN[PidSource.DEFAULT])
            src_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            src_lbl.setStyleSheet(self._source_badge_qss(PidSource.DEFAULT))
            vbox.addWidget(name_lbl)
            vbox.addWidget(src_lbl)
            right.addLayout(vbox)
            self._source_labels[ring] = src_lbl
        h.addLayout(right, 0)

        parent_layout.addWidget(card)

    def _source_badge_qss(self, source: PidSource) -> str:
        """source 徽章 QSS: 按来源类型着色。"""
        color_key = _SOURCE_COLOR_KEY.get(source, "muted")
        bg = theme.hex(color_key)
        return (f"background:{bg}; color:{theme.hex('text_strong')};"
                f"padding:2px 8px; border-radius:8px; font-size:11px; font-weight:bold;")

    def _build_operation_panel(self, parent_layout):
        """操作区: 左(理论估计 0xA0) 右(来源切换 0xA1)。"""
        box = QGroupBox("PID 操作")
        h = QHBoxLayout(box)
        h.setContentsMargins(8, 8, 8, 8)
        h.setSpacing(8)

        # ---- 左: 理论估计 ----
        autotune_box = QGroupBox("理论估计 (0xA0)")
        av = QVBoxLayout(autotune_box)
        av.setSpacing(6)

        # 环选择
        ring_row = QHBoxLayout()
        ring_row.addWidget(QLabel("整定环:"))
        self._autotune_ring_checks = {}
        for ring in PidRing:
            chk = QCheckBox(_RING_CN[ring])
            chk.setChecked(True)
            self._autotune_ring_checks[ring] = chk
            ring_row.addWidget(chk)
        ring_row.addStretch()
        av.addLayout(ring_row)

        # 带宽输入
        bw_row = QHBoxLayout()
        bw_row.addWidget(QLabel("电流环(Hz):"))
        self._cur_bw_spin = QDoubleSpinBox()
        self._cur_bw_spin.setRange(0.0, 10000.0)
        self._cur_bw_spin.setValue(0.0)
        self._cur_bw_spin.setSpecialValueText("默认(1000)")
        bw_row.addWidget(self._cur_bw_spin)
        bw_row.addWidget(QLabel("速度环(Hz):"))
        self._vel_bw_spin = QDoubleSpinBox()
        self._vel_bw_spin.setRange(0.0, 5000.0)
        self._vel_bw_spin.setValue(0.0)
        self._vel_bw_spin.setSpecialValueText("默认(100)")
        bw_row.addWidget(self._vel_bw_spin)
        bw_row.addWidget(QLabel("位置环(Hz):"))
        self._pos_bw_spin = QDoubleSpinBox()
        self._pos_bw_spin.setRange(0.0, 1000.0)
        self._pos_bw_spin.setValue(0.0)
        self._pos_bw_spin.setSpecialValueText("默认(20)")
        bw_row.addWidget(self._pos_bw_spin)
        av.addLayout(bw_row)

        # 开始按钮
        self._autotune_btn = QPushButton("开始理论估计")
        self._autotune_btn.setStyleSheet(self._ok_btn_qss())
        self._autotune_btn.clicked.connect(self._on_autotune_clicked)
        av.addWidget(self._autotune_btn)

        h.addWidget(autotune_box, 1)

        # ---- 右: 来源切换 (0xA1) ----
        source_box = QGroupBox("来源切换 (0xA1)")
        sv = QVBoxLayout(source_box)
        sv.setSpacing(6)

        self._source_btn_groups = {}
        for ring in PidRing:
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{_RING_CN[ring]}:"))
            grp = QButtonGroup(self)
            grp.setExclusive(True)
            for src in PidSource:
                rb = QRadioButton(_SOURCE_CN[src])
                grp.addButton(rb, int(src))
                if src == PidSource.DEFAULT:
                    rb.setChecked(True)
                row.addWidget(rb)
            self._source_btn_groups[int(ring)] = grp
            row.addStretch()
            sv.addLayout(row)

        self._source_apply_btn = QPushButton("应用来源切换")
        self._source_apply_btn.setStyleSheet(self._normal_btn_qss())
        self._source_apply_btn.clicked.connect(self._on_source_apply_clicked)
        sv.addWidget(self._source_apply_btn)

        h.addWidget(source_box, 1)

        parent_layout.addWidget(box)

    # 0xA5/0xA6 调试参数: ring 与 param_type 映射 (与固件 motor_pid_profile_set_param 一致)
    _DEBUG_RING_ITEMS = [("D轴", 0), ("Q轴", 1), ("速度", 2), ("位置", 3)]
    _DEBUG_PARAM_ITEMS = [
        ("比例KP", 1), ("积分KI", 2), ("微分KD", 3),
        ("输出限幅", 4), ("积分限幅", 5),
        ("滤波系数", 6), ("标志位", 7),
    ]

    # 滑条精度: int 0~_SLIDER_STEPS 映射到 [min, max]
    _SLIDER_STEPS = 10000

    # 各参数默认 min/max (首次 0xA6 读回前使用; 读回后按 读回值×2 自适应)
    _PARAM_DEFAULT_RANGE = {
        1: (0.0, 1.0),        # kp
        2: (0.0, 1.0),        # ki
        3: (0.0, 1.0),        # kd
        4: (0.0, 30.0),       # output_limit
        5: (0.0, 20.0),       # integral_limit
        6: (0.0, 1.0),        # output_filter_alpha
        7: (0.0, 65535.0),    # flags (uint32, 默认 0~0xFFFF)
    }

    # 写入后延迟回读时间 (ms)
    _READBACK_DELAY_MS = 100

    # ========================================================================
    #  DEBUG 滑条面板区 (source=DEBUG 时动态展开, 0xA5 写 / 0xA6 轮询读)
    # ========================================================================
    def _build_debug_slider_area(self, parent_layout):
        """滑条面板容器: 初始隐藏, 当某环 source 切换到 DEBUG 时动态添加对应滑条组。
        滑条组直接展开 (由外层 _main_scroll 统一提供滚动)。
        """
        self._slider_container = QGroupBox("DEBUG 实时调试滑条 (0xA5 写 / 0xA6 轮询读)")
        self._slider_container.setStyleSheet(self._slider_container_qss())
        v = QVBoxLayout(self._slider_container)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)

        # 滑条组动态挂载区 (直接布局, 不再用内部 QScrollArea)
        self._slider_area = QVBoxLayout()
        self._slider_area.setSpacing(6)
        self._slider_area.setContentsMargins(2, 2, 2, 2)
        v.addLayout(self._slider_area)

        parent_layout.addWidget(self._slider_container)
        # 初始隐藏 (无 DEBUG 组时不占空间)
        self._slider_container.setVisible(False)

    def _slider_container_qss(self) -> str:
        return (f"QGroupBox{{border:1px solid {theme.hex('accent')};"
                f"border-radius:6px; margin-top:8px;"
                f"background:{theme.hex('card_top')};"
                f"font-weight:bold; color:{theme.hex('text_strong')};}}"
                f"QGroupBox::title{{subcontrol-origin:margin; left:10px; padding:0 4px;"
                f"background:{theme.hex('card_top')};}}")

    def _ring_cn(self, ring: int) -> str:
        """ring(0~3) -> 中文名。"""
        if 0 <= ring < len(self._DEBUG_RING_ITEMS):
            return self._DEBUG_RING_ITEMS[ring][0]
        return f"ring{ring}"

    def _param_name(self, param_type: int) -> str:
        """param_type(1~7) -> 参数名。"""
        if 1 <= param_type <= len(self._DEBUG_PARAM_ITEMS):
            return self._DEBUG_PARAM_ITEMS[param_type - 1][0]
        return f"type{param_type}"

    def _ensure_slider_group(self, ring: int):
        """创建(若不存在)并显示指定 ring 的滑条组面板。"""
        if ring in self._debug_slider_groups:
            grp = self._debug_slider_groups[ring]
            grp.container.setVisible(True)
            return grp
        grp = _SliderGroup(ring, self)
        self._slider_area.addWidget(grp.container)
        self._debug_slider_groups[ring] = grp
        return grp

    def _remove_slider_group(self, ring: int):
        """移除指定 ring 的滑条组面板, 并取消该环的待发回读定时器。"""
        grp = self._debug_slider_groups.pop(ring, None)
        if grp is not None:
            self._slider_area.removeWidget(grp.container)
            grp.container.setParent(None)
            grp.container.deleteLater()
        # 取消该环所有待发的延迟回读
        for key in list(self._readback_timers.keys()):
            if key[0] == ring:
                t = self._readback_timers.pop(key)
                t.stop()
                t.deleteLater()

    def _sync_slider_groups(self):
        """根据当前三环 source 状态同步滑条面板显隐。

        D轴(ring=0)/Q轴(ring=1) 共享电流环 source;
        速度(ring=2) 对应速度环 source;
        位置(ring=3) 对应位置环 source。
        """
        ring_to_source_ring = {0: PidRing.CURRENT, 1: PidRing.CURRENT,
                               2: PidRing.VELOCITY, 3: PidRing.POSITION}
        new_rings = []
        for ring, src_ring in ring_to_source_ring.items():
            src = self._ring_source.get(src_ring, PidSource.DEFAULT)
            if src == PidSource.DEBUG:
                was_new = ring not in self._debug_slider_groups
                self._ensure_slider_group(ring)
                if was_new:
                    new_rings.append(ring)
            else:
                self._remove_slider_group(ring)
        # 容器显隐: 有 DEBUG 组时显示容器, 无时隐藏 (不占空间)
        has_debug = len(self._debug_slider_groups) > 0
        if hasattr(self, '_slider_container'):
            self._slider_container.setVisible(has_debug)
        # 新增 DEBUG 组时立即请求一次全部参数 (不再周期轮询)
        if new_rings:
            for ring in new_rings:
                self._request_all_params(ring)

    def _request_all_params(self, ring: int):
        """一次性请求指定环的全部 7 个参数 (0xA6)。"""
        for i in range(len(self._DEBUG_PARAM_ITEMS)):
            self.pid_param_get_requested.emit(ring, i + 1)

    def _schedule_readback(self, ring: int, param_type: int):
        """写入后延迟 100ms 请求一次该参数 (确认写入生效)。"""
        key = (ring, param_type)
        # 若已有定时器在等待, 取消旧的 (避免重复请求)
        old = self._readback_timers.pop(key, None)
        if old is not None:
            old.stop()
            old.deleteLater()
        t = QTimer(self)
        t.setSingleShot(True)
        t.setInterval(self._READBACK_DELAY_MS)
        t.timeout.connect(lambda r=ring, p=param_type, tmr=t:
                          self._on_readback_timeout(r, p, tmr))
        self._readback_timers[key] = t
        t.start()

    def _on_readback_timeout(self, ring: int, param_type: int, tmr):
        """延迟回读定时器到期: 发送一次 0xA6 请求。"""
        self._readback_timers.pop((ring, param_type), None)
        tmr.stop()
        tmr.deleteLater()
        self.pid_param_get_requested.emit(ring, param_type)

    def _on_slider_pressed(self, ring: int, param_type: int):
        """滑条开始拖动: 标记拖动中, 轮询时不覆盖。"""
        grp = self._debug_slider_groups.get(ring)
        if grp is not None:
            grp.set_dragging(param_type, True)

    def _on_slider_released(self, ring: int, param_type: int):
        """滑条松开: 触发 0xA5 写入, 100ms 后回读确认。需先勾选激活。"""
        grp = self._debug_slider_groups.get(ring)
        if grp is None:
            return
        grp.set_dragging(param_type, False)
        if not grp.is_enabled(param_type):  # 未勾选激活, 不写入
            return
        value = grp.slider_value(param_type)
        if value is None:
            return
        pname = self._param_name(param_type)
        ring_cn = self._ring_cn(ring)
        self._add_history(f"[TX] 滑条写入 {ring_cn}.{pname} = {value:.6g}")
        self.pid_param_set_requested.emit(ring, param_type, float(value))
        # 写入后延迟 100ms 回读该参数
        self._schedule_readback(ring, param_type)

    def _on_slider_range_changed(self, ring: int, param_type: int):
        """min/max 输入框修改: 重新映射滑条刻度 (不触发写入)。"""
        grp = self._debug_slider_groups.get(ring)
        if grp is not None:
            grp.remap_slider(param_type)

    def _on_slider_write_btn(self, ring: int, param_type: int):
        """写入按钮: 读取当前滑条值并发送 0xA5, 100ms 后回读确认。需先勾选激活。"""
        grp = self._debug_slider_groups.get(ring)
        if grp is None:
            return
        if not grp.is_enabled(param_type):  # 未勾选激活, 不写入
            return
        value = grp.slider_value(param_type)
        if value is None:
            return
        pname = self._param_name(param_type)
        ring_cn = self._ring_cn(ring)
        self._add_history(f"[TX] 按钮写入 {ring_cn}.{pname} = {value:.6g}")
        self.pid_param_set_requested.emit(ring, param_type, float(value))
        self._schedule_readback(ring, param_type)

    def _on_slider_read_all(self, ring: int):
        """全部读取按钮: 连续发送该环所有 7 个 0xA6 请求。"""
        if not self._link_active:
            return
        for i in range(len(self._DEBUG_PARAM_ITEMS)):
            self.pid_param_get_requested.emit(ring, i + 1)

    def _on_slider_write_all(self, ring: int):
        """全部写入按钮: 连续发送该环所有 7 个 0xA5 请求。"""
        if not self._link_active:
            return
        grp = self._debug_slider_groups.get(ring)
        if grp is None:
            return
        ring_cn = self._ring_cn(ring)
        for i in range(len(self._DEBUG_PARAM_ITEMS)):
            ptype = i + 1
            if not grp.is_enabled(ptype):  # 未勾选激活, 跳过
                continue
            value = grp.slider_value(ptype)
            if value is None:
                continue
            pname = self._param_name(ptype)
            self._add_history(f"[TX] 批量写入 {ring_cn}.{pname} = {value:.6g}")
            self.pid_param_set_requested.emit(ring, ptype, float(value))
            self._schedule_readback(ring, ptype)

    def _on_slider_restore_range(self, ring: int):
        """恢复默认范围按钮: 各参数恢复自适应初始范围。"""
        grp = self._debug_slider_groups.get(ring)
        if grp is None:
            return
        for ptype in range(1, len(self._DEBUG_PARAM_ITEMS) + 1):
            lo, hi = self._PARAM_DEFAULT_RANGE.get(ptype, (0.0, 1.0))
            grp.set_range(ptype, lo, hi)

    def _on_slider_read_all_click(self, ring: int):
        """全部读取按钮: 立即请求该环全部 7 个参数。"""
        self._request_all_params(ring)

    def _build_results_history_split(self, parent_layout):
        """结果/历史 Splitter: 上(PID 参数表) 下(操作历史)。

        - 历史默认隐藏, 隐藏时参数表 stretch 占满
        - 历史显示时 splitter 手柄可拖动, 两区皆可缩放
        - 历史开关按钮放到参数面板保存按钮同行最右(由 attach_params_panel 注入)
        - 输出框去掉 maxHeight, 填满整个历史子框
        """
        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(6)
        split.setStyleSheet(f"""
            QSplitter::handle:vertical {{
                background: {theme.hex('border')};
                margin: 1px 0;
            }}
            QSplitter::handle:vertical:hover {{
                background: {theme.hex('accent')};
            }}
        """)

        # 上: 参数表占位 (attach_params_panel 时替换)
        self._params_placeholder = QLabel("等待参数面板注入...")
        self._params_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._params_placeholder.setStyleSheet(
            f"color:{theme.hex('muted')}; padding:20px;")
        split.addWidget(self._params_placeholder)

        # 下: 操作历史
        self._history_grp = QGroupBox("操作历史")
        hv = QVBoxLayout(self._history_grp)
        hv.setContentsMargins(4, 4, 4, 4)
        hv.setSpacing(4)
        self._history_view = QTextEdit()
        self._history_view.setReadOnly(True)
        self._history_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._history_view.setStyleSheet(
            f"background:{theme.hex('panel_bg')}; color:{theme.hex('text')};"
            f"font-family: Consolas, 'Microsoft YaHei', monospace; font-size:12px;"
            f"border:1px solid {theme.hex('border')};")
        hv.addWidget(self._history_view, 1)

        op_row = QHBoxLayout()
        op_row.addStretch()
        self._btn_clear_history = QPushButton("清空历史")
        self._btn_clear_history.setFixedHeight(24)
        self._btn_clear_history.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear_history.setStyleSheet(self._normal_btn_qss())
        self._btn_clear_history.clicked.connect(self._on_clear_history)
        op_row.addWidget(self._btn_clear_history)
        hv.addLayout(op_row)

        split.addWidget(self._history_grp)

        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        split.setSizes([400, 100])
        split.setMinimumHeight(350)
        parent_layout.addWidget(split, 1)
        self._params_split = split

        # 默认隐藏历史, 让参数表占满
        self._history_grp.setVisible(False)
        self._history_visible = False

    # ========================================================================
    #  按钮样式
    # ========================================================================
    def _ok_btn_qss(self) -> str:
        return (f"QPushButton{{background:{theme.hex('ok')};"
                f"color:{theme.hex('ok_text')};"
                f"border:none;padding:6px 12px;border-radius:4px;font-weight:bold;}}"
                f"QPushButton:hover{{background:{theme.hex('accent')};}}")

    def _normal_btn_qss(self) -> str:
        return (f"QPushButton{{background:{theme.hex('input_bg')};"
                f"color:{theme.hex('text')};"
                f"border:1px solid {theme.hex('btn_border')};"
                f"padding:6px 12px;border-radius:4px;}}"
                f"QPushButton:hover{{border-color:{theme.hex('accent')};}}")

    # ========================================================================
    #  按钮回调
    # ========================================================================
    def _on_autotune_clicked(self):
        """开始理论估计按钮回调。"""
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接, 无法整定")
            return
        # 计算 ring_select 位掩码: bit0=电流 bit1=速度 bit2=位置 (可组合)
        ring_select = 0
        for ring, chk in self._autotune_ring_checks.items():
            if chk.isChecked():
                ring_select |= (1 << int(ring))
        if ring_select == 0:
            self._add_history("[参数错误] 请至少选择一个环")
            self._set_last_op("未选择整定环")
            return
        cur_bw = self._cur_bw_spin.value()
        vel_bw = self._vel_bw_spin.value()
        pos_bw = self._pos_bw_spin.value()
        rings_cn = "/".join(
            _RING_CN[r] for r, c in self._autotune_ring_checks.items() if c.isChecked())
        self._add_history(
            f"[TX] 理论估计 {rings_cn} 带宽=({cur_bw},{vel_bw},{pos_bw})Hz")
        self._set_last_op(f"理论估计中... ({rings_cn})")
        self.pid_autotune_requested.emit(ring_select, cur_bw, vel_bw, pos_bw)

    def _on_source_apply_clicked(self):
        """应用来源切换按钮回调: 逐环发送 0xA1。"""
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接")
            return
        sent = 0
        for ring_id, grp in self._source_btn_groups.items():
            src = grp.checkedId()
            if src < 0:
                continue
            self._add_history(
                f"[TX] 来源切换 {_RING_CN[PidRing(ring_id)]} -> {_SOURCE_CN[PidSource(src)]}")
            self.pid_source_set_requested.emit(ring_id, src)
            sent += 1
        if sent > 0:
            self._set_last_op(f"已发送 {sent} 个来源切换请求")
        else:
            self._set_last_op("无来源变更")

    # ========================================================================
    #  公共 API (主窗口调用)
    # ========================================================================
    def showEvent(self, event):
        """界面显示时: 无特殊处理 (不再周期轮询)。"""
        super().showEvent(event)

    def hideEvent(self, event):
        """界面隐藏(切换到其他tab)时: 取消所有待发的延迟回读定时器。"""
        super().hideEvent(event)
        self._cancel_all_readback()

    def _cancel_all_readback(self):
        """取消所有待发的延迟回读定时器。"""
        for key, t in list(self._readback_timers.items()):
            t.stop()
            t.deleteLater()
        self._readback_timers.clear()

    def set_link_active(self, active: bool):
        """连接状态变化: 断连时取消所有延迟回读, 不重置 source 状态。"""
        self._link_active = active
        if not active:
            self._cancel_all_readback()
        self._refresh_status()

    def update_state(self, top_fsm: int, run_state: int, ctrl_mode: int, enable: int):
        self._current_top_fsm = top_fsm
        self._refresh_status()

    def on_autotune_result(self, result: dict):
        """0xA0 PID_AUTOTUNE 应答 (8字节 ACK)。"""
        ok = bool(result.get('ok', False))
        fail_cn = result.get('fail_reason_cn', '未知')
        ring_done = int(result.get('ring_select_done', 0))
        if ok:
            rings_cn = []
            if ring_done & 0x01:  # bit0=电流环
                self._ring_source[PidRing.CURRENT] = PidSource.AUTOTUNE
                rings_cn.append(_RING_CN[PidRing.CURRENT])
            if ring_done & 0x02:  # bit1=速度环
                self._ring_source[PidRing.VELOCITY] = PidSource.AUTOTUNE
                rings_cn.append(_RING_CN[PidRing.VELOCITY])
            if ring_done & 0x04:  # bit2=位置环
                self._ring_source[PidRing.POSITION] = PidSource.AUTOTUNE
                rings_cn.append(_RING_CN[PidRing.POSITION])
            self._refresh_source_badges()
            self._add_history(f"[RX] 理论估计成功 ({'/'.join(rings_cn)})")
            self._set_last_op(f"理论估计完成 ({'/'.join(rings_cn)})")
            # 自动刷新参数表 (读回 autotune 写入的 ControlParam)
            if self._params_panel:
                try:
                    self._params_panel.read_all()
                except Exception:
                    pass
        else:
            self._add_history(f"[RX] 理论估计失败: {fail_cn}")
            self._set_last_op(f"理论估计失败: {fail_cn}")

    def on_source_received(self, cur_src: int, vel_src: int, pos_src: int):
        """0xA2 PID_SOURCE_GET 应答 (3字节: cur/vel/pos source)。

        连接建立后主窗口主动查询, 回填三环 source 状态并同步单选按钮。
        """
        sources = {PidRing.CURRENT: cur_src, PidRing.VELOCITY: vel_src, PidRing.POSITION: pos_src}
        for ring, src in sources.items():
            try:
                self._ring_source[ring] = PidSource(src)
            except ValueError:
                self._ring_source[ring] = PidSource.DEFAULT
            # 同步来源切换区单选按钮
            grp = self._source_btn_groups.get(int(ring))
            if grp is not None:
                btn = grp.button(int(self._ring_source[ring]))
                if btn is not None:
                    btn.setChecked(True)
        self._refresh_source_badges()
        self._add_history(
            f"[RX] PID来源 电流={_SOURCE_CN[self._ring_source[PidRing.CURRENT]]} "
            f"速度={_SOURCE_CN[self._ring_source[PidRing.VELOCITY]]} "
            f"位置={_SOURCE_CN[self._ring_source[PidRing.POSITION]]}")
        # 同步 DEBUG 滑条面板显隐
        self._sync_slider_groups()


    def on_ack(self, cmd: int):
        """0xA1 PID_SOURCE_SET / 0xA5 PID_PARAM_SET 成功 ACK。"""
        if cmd == int(JmCmd.PID_SOURCE_SET):
            self._add_history("[RX] 来源切换成功")
            self._set_last_op("来源切换完成")
            # 更新本地 source 状态 (从单选组读取)
            for ring_id, grp in self._source_btn_groups.items():
                src = grp.checkedId()
                if src >= 0:
                    self._ring_source[PidRing(ring_id)] = PidSource(src)
            self._refresh_source_badges()
            # 同步 DEBUG 滑条面板显隐
            self._sync_slider_groups()
        elif cmd == int(JmCmd.PID_PARAM_SET):
            self._add_history(f"[RX] 调试参数写入成功")

    def on_nack(self, cmd: int, err: int):
        """0xA1 PID_SOURCE_SET / 0xA5 PID_PARAM_SET 失败 NACK。"""
        if cmd == int(JmCmd.PID_SOURCE_SET):
            self._add_history(f"[RX] 来源切换失败 err=0x{err:02X}")
            self._set_last_op(f"来源切换失败 (0x{err:02X})")
        elif cmd == int(JmCmd.PID_PARAM_SET):
            if err == 0x03:  # STATE_DENY
                self._add_history("[RX] 调试参数写入失败: 对应环未处于 DEBUG 模式")
                self._set_last_op("需先切换到 DEBUG 模式")
            else:
                self._add_history(f"[RX] 调试参数写入失败 err=0x{err:02X}")
                self._set_last_op(f"写入失败 (0x{err:02X})")

    def on_pid_param_received(self, ring: int, param_type: int, value4: bytes):
        """0xA6 PID_PARAM_GET 应答 (6字节: ring + param_type + 4B value)。

        更新滑条面板位置。不更新状态栏 (轮询频繁, 避免刷屏)。
        """
        import struct as _struct
        if len(value4) >= 4:
            if param_type == 7:  # flags: uint32, 仅显示十进制
                val = _struct.unpack('<I', value4[:4])[0]
            else:
                val = _struct.unpack('<f', value4[:4])[0]
        else:
            val = None
        # 滑条面板更新
        if val is not None:
            grp = self._debug_slider_groups.get(ring)
            if grp is not None:
                grp.update_value(param_type, float(val), is_uint32=(param_type == 7))

    def attach_params_panel(self, panel):
        """注入 ParamPanel 实例, 替换占位控件, 并把「历史:显/隐」开关注入其 footer。"""
        self._params_panel = panel
        # 替换 Splitter 中的占位控件
        idx = self._params_split.indexOf(self._params_placeholder)
        if idx >= 0:
            self._params_placeholder.setParent(None)
            self._params_placeholder.deleteLater()
            self._params_placeholder = None
            self._params_split.insertWidget(idx, panel)

        # 历史开关按钮 — 注入到 ParamPanel 保存按钮同行最右 (同标定面板设计)
        self._btn_history_toggle = QPushButton("历史: 隐")
        self._btn_history_toggle.setCheckable(True)
        self._btn_history_toggle.setChecked(False)
        self._btn_history_toggle.setFixedHeight(24)
        self._btn_history_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_history_toggle.setToolTip("显示/隐藏 操作历史 (显示后可拖动手柄调整与参数表的高度比例)")
        self._btn_history_toggle.setStyleSheet(self._history_toggle_qss())
        self._btn_history_toggle.toggled.connect(self._on_history_toggle)
        panel.add_footer_widget(self._btn_history_toggle)
        # 恢复持久化的历史显隐态
        if getattr(self, "_history_visible", False):
            self._btn_history_toggle.setChecked(True)

        self._set_last_op("参数面板已加载")

    def _on_history_toggle(self, on: bool):
        """「历史:显/隐」开关 (注入到参数面板保存按钮同行右侧)。"""
        self._btn_history_toggle.setText("历史: 显" if on else "历史: 隐")
        self.set_history_visible(on)

    def set_history_visible(self, visible: bool):
        """显示/隐藏整个操作历史区(含 QGroupBox), 隐藏时参数表独占空间。"""
        self._history_visible = bool(visible)
        if hasattr(self, "_history_grp"):
            self._history_grp.setVisible(self._history_visible)
        if hasattr(self, "_params_split"):
            self._params_split.setStretchFactor(0, 4)
            self._params_split.setStretchFactor(1, 1 if self._history_visible else 0)

    def is_history_visible(self) -> bool:
        return self._history_visible

    def _on_clear_history(self):
        self._history.clear()
        if self._history_view:
            self._history_view.setPlainText("")

    def _history_toggle_qss(self) -> str:
        return (f"QPushButton{{background:{theme.hex('input_bg')};"
                f"color:{theme.hex('text')};"
                f"border:1px solid {theme.hex('border')};border-radius:3px;"
                f"padding:0 10px;font-size:12px;}}"
                f"QPushButton:hover{{border-color:{theme.hex('accent')};"
                f"color:{theme.hex('accent')};}}"
                f"QPushButton:checked{{background:{theme.hex('accent')};"
                f"color:{theme.hex('card_bottom')};"
                f"border-color:{theme.hex('accent')};font-weight:bold;}}")

    def apply_theme(self):
        """主题切换: 刷新所有控件样式。"""
        self._status_label.setStyleSheet(
            f"font-weight:bold; color:{theme.hex('text_strong')};")
        self._last_op_label.setStyleSheet(f"color:{theme.hex('muted')};")
        if hasattr(self, '_autotune_btn'):
            self._autotune_btn.setStyleSheet(self._ok_btn_qss())
        if hasattr(self, '_source_apply_btn'):
            self._source_apply_btn.setStyleSheet(self._normal_btn_qss())
        # splitter 手柄随主题刷新
        if hasattr(self, "_params_split"):
            self._params_split.setStyleSheet(f"""
                QSplitter::handle:vertical {{
                    background: {theme.hex('border')};
                    margin: 1px 0;
                }}
                QSplitter::handle:vertical:hover {{
                    background: {theme.hex('accent')};
                }}
            """)
        # 历史开关按钮主题刷新
        if hasattr(self, "_btn_history_toggle"):
            self._btn_history_toggle.setStyleSheet(self._history_toggle_qss())
        # 清空历史按钮
        if hasattr(self, "_btn_clear_history"):
            self._btn_clear_history.setStyleSheet(self._normal_btn_qss())
        # 历史输出框
        if self._history_view:
            self._history_view.setStyleSheet(
                f"background:{theme.hex('panel_bg')}; color:{theme.hex('text')};"
                f"font-family: Consolas, 'Microsoft YaHei', monospace; font-size:12px;"
                f"border:1px solid {theme.hex('border')};")
        # DEBUG 滑条面板主题刷新
        if hasattr(self, '_slider_container'):
            self._slider_container.setStyleSheet(self._slider_container_qss())
        if hasattr(self, '_main_scroll'):
            self._main_scroll.setStyleSheet(self._main_scroll_qss())
        for grp in self._debug_slider_groups.values():
            grp.apply_theme()
        self._refresh_source_badges()
        self._refresh_status()
        if self._params_panel and hasattr(self._params_panel, 'apply_theme'):
            self._params_panel.apply_theme()

    def get_opts(self) -> dict:
        """收集可持久化的 UI 配置: 历史显隐 + 内嵌参数面板列宽。"""
        opts = {}
        try:
            opts["history_visible"] = bool(self._history_visible)
        except Exception:
            pass
        if self._params_panel is not None:
            try:
                opts["params_panel"] = self._params_panel.get_opts()
            except Exception:
                pass
        return opts

    def set_opts(self, opts: dict):
        """启动时套用配置 (容错)。"""
        if not isinstance(opts, dict):
            return
        if "history_visible" in opts:
            try:
                vis = bool(opts["history_visible"])
                self._history_visible = vis
                if hasattr(self, "_btn_history_toggle"):
                    self._btn_history_toggle.setChecked(vis)
                else:
                    self.set_history_visible(vis)
            except Exception:
                pass
        if self._params_panel is not None and isinstance(opts.get("params_panel"), dict):
            try:
                self._params_panel.set_opts(opts["params_panel"])
            except Exception:
                pass

    # ========================================================================
    #  内部辅助
    # ========================================================================
    def _refresh_status(self):
        if not self._link_active:
            self._status_label.setText("未连接")
            self._status_label.setStyleSheet(
                f"font-weight:bold; color:{theme.hex('danger')};")
            return
        if self._current_top_fsm == int(TopFsm.IDLE):
            self._status_label.setText("已连接 · IDLE 就绪")
            self._status_label.setStyleSheet(
                f"font-weight:bold; color:{theme.hex('ok')};")
        else:
            self._status_label.setText(
                f"已连接 · top_fsm={self._current_top_fsm}")
            self._status_label.setStyleSheet(
                f"font-weight:bold; color:{theme.hex('warn')};")

    def _refresh_source_badges(self):
        for ring, lbl in self._source_labels.items():
            src = self._ring_source[ring]
            lbl.setText(_SOURCE_CN[src])
            lbl.setStyleSheet(self._source_badge_qss(src))

    def _set_last_op(self, text: str):
        if self._last_op_label:
            self._last_op_label.setText(text)

    def _add_history(self, text: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._history.append(f"{ts}  {text}")
        if self._history_view:
            self._history_view.setPlainText("\n".join(self._history))
            sb = self._history_view.verticalScrollBar()
            if sb:
                sb.setValue(sb.maximum())


# ============================================================================
#  _SliderGroup: 单个 ring 的调试滑条面板 (7 参数行 + 底部操作按钮)
# ============================================================================
class _SliderGroup:
    """单个 ring 的调试滑条组状态 + UI 构建。

    每行: 参数名 | QSlider | 当前值 | min输入 | max输入 | 写入按钮
    滑条 int 0~_SLIDER_STEPS 映射到 [min, max] 浮点值。
    """

    def __init__(self, ring: int, panel: 'PidPanel'):
        self.ring = ring
        self.panel = panel
        # param_type -> 行控件 dict
        self.rows = {}
        self._build_ui()

    def _build_ui(self):
        ring_cn = self.panel._ring_cn(self.ring)
        self.container = QFrame()
        self.container.setObjectName("debugSliderGroup")
        self.container.setStyleSheet(
            f"QFrame#debugSliderGroup{{border:1px solid {theme.hex('accent')};"
            f"border-radius:6px;"
            f"background:{theme.hex('card_top')};}}")
        v = QVBoxLayout(self.container)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)

        # ---- 标题栏 ----
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        title = QLabel(f"{ring_cn} · 实时调试")
        title.setStyleSheet(
            f"font-weight:bold; color:{theme.hex('text_strong')}; font-size:13px;")
        hdr.addWidget(title)
        badge = QLabel("DEBUG")
        badge.setStyleSheet(
            f"background:{theme.hex('accent')}; color:{theme.hex('card_top')};"
            f"padding:1px 8px; border-radius:8px; font-size:11px; font-weight:bold;")
        hdr.addWidget(badge)
        hdr.addStretch()
        self.poll_label = QLabel(f"写入后 {self.panel._READBACK_DELAY_MS}ms 回读确认")
        self.poll_label.setStyleSheet(f"color:{theme.hex('muted')}; font-size:11px;")
        hdr.addWidget(self.poll_label)
        v.addLayout(hdr)

        # ---- 表头 ----
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(2)
        headers = ["启用", "参数", "滑条 (拖动松开即写入)", "当前值", "min", "max", "写入"]
        col_widths = [40, 90, 0, 70, 60, 60, 48]
        for col, (htext, w) in enumerate(zip(headers, col_widths)):
            lbl = QLabel(htext)
            lbl.setStyleSheet(
                f"color:{theme.hex('muted')}; font-size:11px;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(lbl, 0, col)
            if w > 0:
                grid.setColumnMinimumWidth(col, w)

        # ---- 7 行滑条 ----
        for row_idx, (pname, ptype) in enumerate(self.panel._DEBUG_PARAM_ITEMS, start=1):
            lo, hi = self.panel._PARAM_DEFAULT_RANGE.get(ptype, (0.0, 1.0))

            # 激活勾选 (默认不勾选, 未勾选时滑条/写入按钮禁用, 保证安全)
            enable_chk = QCheckBox()
            enable_chk.setChecked(False)
            enable_chk.setStyleSheet(
                f"QCheckBox::indicator{{width:14px;height:14px;"
                f"border:1px solid {theme.hex('btn_border')};border-radius:3px;"
                f"background:{theme.hex('input_bg')};}}"
                f"QCheckBox::indicator:checked{{background:{theme.hex('accent')};"
                f"border:1px solid {theme.hex('accent')};}}")
            grid.addWidget(enable_chk, row_idx, 0)

            # 参数名
            name_lbl = QLabel(pname)
            name_lbl.setStyleSheet(
                f"color:{theme.hex('text_strong')};"
                f"font-size:12px; font-weight:bold;")
            grid.addWidget(name_lbl, row_idx, 1)

            # 滑条
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, self.panel._SLIDER_STEPS)
            slider.setValue(0)
            slider.setEnabled(False)  # 默认禁用, 勾选后启用
            slider.setStyleSheet(self._slider_qss())
            grid.addWidget(slider, row_idx, 2)

            # 当前值
            val_lbl = QLabel("--")
            val_lbl.setStyleSheet(
                f"color:{theme.hex('accent')}; font-family:Consolas,monospace;"
                f"font-size:12px; font-weight:bold;")
            val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val_lbl.setMinimumWidth(70)
            grid.addWidget(val_lbl, row_idx, 3)

            # min 输入 (移除箭头按钮, 节省空间给数字显示)
            min_spin = QDoubleSpinBox()
            min_spin.setRange(-1e9, 1e9)
            min_spin.setDecimals(6)
            min_spin.setValue(lo)
            min_spin.setMinimumWidth(68)
            min_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
            min_spin.setStyleSheet(self._spin_qss())
            grid.addWidget(min_spin, row_idx, 4)

            # max 输入 (移除箭头按钮)
            max_spin = QDoubleSpinBox()
            max_spin.setRange(-1e9, 1e9)
            max_spin.setDecimals(6)
            max_spin.setValue(hi)
            max_spin.setMinimumWidth(68)
            max_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
            max_spin.setStyleSheet(self._spin_qss())
            grid.addWidget(max_spin, row_idx, 5)

            # 写入按钮
            write_btn = QPushButton("0xA5")
            write_btn.setFixedWidth(48)
            write_btn.setEnabled(False)  # 默认禁用, 勾选后启用
            write_btn.setStyleSheet(self._write_btn_qss())
            grid.addWidget(write_btn, row_idx, 6)

            # 存储行控件
            self.rows[ptype] = {
                'enable_chk': enable_chk,
                'slider': slider,
                'val_lbl': val_lbl,
                'min_spin': min_spin,
                'max_spin': max_spin,
                'write_btn': write_btn,
                'dragging': False,
                'initialized': False,
            }

            # 信号绑定 (用默认参数捕获 ptype)
            enable_chk.toggled.connect(
                lambda checked, pt=ptype: self._on_enable_toggled(pt, checked))
            slider.sliderPressed.connect(
                lambda pt=ptype: self.panel._on_slider_pressed(self.ring, pt))
            slider.sliderReleased.connect(
                lambda pt=ptype: self.panel._on_slider_released(self.ring, pt))
            min_spin.valueChanged.connect(
                lambda _, pt=ptype: self.panel._on_slider_range_changed(self.ring, pt))
            max_spin.valueChanged.connect(
                lambda _, pt=ptype: self.panel._on_slider_range_changed(self.ring, pt))
            write_btn.clicked.connect(
                lambda _, pt=ptype: self.panel._on_slider_write_btn(self.ring, pt))

        grid.setColumnStretch(2, 1)  # 滑条列占满剩余空间
        v.addLayout(grid)

        # ---- 底部操作栏 ----
        footer = QHBoxLayout()
        footer.setSpacing(6)
        btn_read_all = QPushButton("全部读取 (0xA6×7)")
        btn_write_all = QPushButton("全部写入 (0xA5×7)")
        btn_restore = QPushButton("恢复默认范围")
        for btn in (btn_read_all, btn_write_all, btn_restore):
            btn.setStyleSheet(self.panel._normal_btn_qss())
            btn.setFixedHeight(26)
        btn_read_all.clicked.connect(lambda: self.panel._on_slider_read_all_click(self.ring))
        btn_write_all.clicked.connect(lambda: self.panel._on_slider_write_all(self.ring))
        btn_restore.clicked.connect(lambda: self.panel._on_slider_restore_range(self.ring))
        footer.addWidget(btn_read_all)
        footer.addWidget(btn_write_all)
        footer.addWidget(btn_restore)
        footer.addStretch()
        v.addLayout(footer)

    # ---- 值映射 ----
    def _on_enable_toggled(self, ptype: int, checked: bool):
        """激活勾选变化: 启用/禁用该行的滑条和写入按钮。"""
        row = self.rows.get(ptype)
        if row is None:
            return
        row['slider'].setEnabled(checked)
        row['write_btn'].setEnabled(checked)
        # 未勾选时清除拖动标记
        if not checked:
            row['dragging'] = False

    def is_enabled(self, ptype: int) -> bool:
        """该参数是否已勾选激活。"""
        row = self.rows.get(ptype)
        return row['enable_chk'].isChecked() if row else False

    def _value_to_slider(self, ptype: int, value: float) -> int:
        """浮点值 -> 滑条 int 位置。"""
        row = self.rows.get(ptype)
        if row is None:
            return 0
        lo = row['min_spin'].value()
        hi = row['max_spin'].value()
        if hi <= lo:
            return 0
        pos = int((value - lo) / (hi - lo) * self.panel._SLIDER_STEPS)
        return max(0, min(self.panel._SLIDER_STEPS, pos))

    def slider_value(self, ptype: int):
        """滑条当前位置 -> 浮点值 (flags 返回 uint32)。"""
        row = self.rows.get(ptype)
        if row is None:
            return None
        pos = row['slider'].value()
        lo = row['min_spin'].value()
        hi = row['max_spin'].value()
        if hi <= lo:
            return lo
        val = lo + pos / self.panel._SLIDER_STEPS * (hi - lo)
        if ptype == 7:  # flags: uint32
            return float(int(max(0, min(0xFFFFFFFF, val))))
        return val

    def remap_slider(self, ptype: int):
        """min/max 变更后重新映射滑条位置 (保持当前值不变)。"""
        row = self.rows.get(ptype)
        if row is None:
            return
        # 读取当前显示值
        txt = row['val_lbl'].text()
        if txt == "--":
            return
        try:
            val = float(txt)
        except ValueError:
            return
        pos = self._value_to_slider(ptype, val)
        # 临时阻塞信号避免触发 released
        slider = row['slider']
        was = slider.blockSignals(True)
        slider.setValue(pos)
        slider.blockSignals(was)

    def set_range(self, ptype: int, lo: float, hi: float):
        """设置 min/max (不触发 valueChanged 回调)。"""
        row = self.rows.get(ptype)
        if row is None:
            return
        row['min_spin'].blockSignals(True)
        row['max_spin'].blockSignals(True)
        row['min_spin'].setValue(lo)
        row['max_spin'].setValue(hi)
        row['min_spin'].blockSignals(False)
        row['max_spin'].blockSignals(False)
        self.remap_slider(ptype)

    def set_dragging(self, ptype: int, on: bool):
        row = self.rows.get(ptype)
        if row is not None:
            row['dragging'] = on

    def is_dragging(self, ptype: int) -> bool:
        row = self.rows.get(ptype)
        return row['dragging'] if row else False

    def update_value(self, ptype: int, value: float, is_uint32: bool = False):
        """0xA6 应答回调: 更新滑条位置 + 当前值显示。

        首次更新时按 读回值×2 自适应 max (不小于 0.001)。
        用户拖动中不覆盖滑条位置。
        """
        row = self.rows.get(ptype)
        if row is None:
            return
        # 显示值 (flags 仅显示十进制, 不显示16进制)
        if is_uint32:
            uval = int(max(0, min(0xFFFFFFFF, value)))
            val_str = f"{uval}"
        else:
            val_str = f"{value:.6g}"
        row['val_lbl'].setText(val_str)
        # 首次初始化: 按 读回值×2 自适应 max
        if not row['initialized']:
            row['initialized'] = True
            lo = row['min_spin'].value()
            if is_uint32:
                new_hi = max(value * 2, 65535.0, lo + 1)
            else:
                new_hi = max(abs(value) * 2, 0.001, lo + 0.001)
            self.set_range(ptype, lo, new_hi)
        # 更新滑条位置 (用户未拖动时)
        if not row['dragging']:
            pos = self._value_to_slider(ptype, value)
            slider = row['slider']
            was = slider.blockSignals(True)
            slider.setValue(pos)
            slider.blockSignals(was)

    # ---- 样式 ----
    def _slider_qss(self) -> str:
        return (f"QSlider::groove:horizontal{{"
                f"border:1px solid {theme.hex('border')};"
                f"height:4px;background:{theme.hex('card_bottom')};"
                f"border-radius:2px;}}"
                f"QSlider::handle:horizontal{{"
                f"background:{theme.hex('input_bg')};"
                f"border:2px solid {theme.hex('accent')};"
                f"width:12px;height:12px;margin:-6px 0;border-radius:8px;}}"
                f"QSlider::sub-page:horizontal{{"
                f"background:{theme.hex('accent')};border-radius:2px;}}")

    def _spin_qss(self) -> str:
        return (f"QDoubleSpinBox{{background:{theme.hex('input_bg')};"
                f"border:1px solid {theme.hex('btn_border')};"
                f"border-radius:3px;padding:1px 2px;"
                f"font-size:11px;color:{theme.hex('text')};}}")

    def _write_btn_qss(self) -> str:
        return (f"QPushButton{{background:{theme.hex('accent')};color:{theme.hex('card_top')};"
                f"border:none;border-radius:3px;padding:2px 6px;"
                f"font-size:11px;font-weight:bold;}}"
                f"QPushButton:hover{{opacity:0.85;}}")

    def apply_theme(self):
        """主题切换时刷新所有控件样式。"""
        self.container.setStyleSheet(
            f"QFrame#debugSliderGroup{{border:1px solid {theme.hex('accent')};"
            f"border-radius:6px;background:{theme.hex('card_top')};}}")
        for row in self.rows.values():
            row['slider'].setStyleSheet(self._slider_qss())
            row['min_spin'].setStyleSheet(self._spin_qss())
            row['max_spin'].setStyleSheet(self._spin_qss())
            row['write_btn'].setStyleSheet(self._write_btn_qss())
            row['val_lbl'].setStyleSheet(
                f"color:{theme.hex('accent')}; font-family:Consolas,monospace;"
                f"font-size:12px; font-weight:bold;")
