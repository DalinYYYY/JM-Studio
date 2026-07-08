"""
PID 整定面板 (顶级 Tab)。

对标"电机标定" Tab 的三段式布局:
  1. 状态卡 — 连接状态 / 三环 source 徽章 / 最近操作
  2. 操作区 — 左:理论估计(0x9A) 右:来源切换(0x9B)
  3. 结果/历史 Splitter — ControlParam 参数表 + 操作历史

信号:
  pid_autotune_requested(ring_select, cur_bw, vel_bw, pos_bw) — 主窗口连 client.pid_autotune
  pid_source_set_requested(ring_select, source)              — 主窗口连 client.pid_source_set

IDLE 态守卫: 0x9A/0x9B 仅 TOP_FSM_IDLE(=3) 可执行。
"""
from collections import deque
from datetime import datetime
from enum import IntEnum

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
    QPushButton, QSplitter, QTextEdit, QCheckBox, QDoubleSpinBox,
    QButtonGroup, QRadioButton,
)

from jmproto.cmd_def import JmCmd, TopFsm
from ui.theme import theme


# ---- 三环 source 枚举 (与固件 motor_pid_load.h pid_source_e 一致) ----
class PidSource(IntEnum):
    DEFAULT = 0    # motor_param.c 默认值
    FLASH = 1      # Flash ControlParam 工程值
    AUTOTUNE = 2   # 理论估计值


# ---- 三环标识 (与固件 pid_ring_e 一致) ----
class PidRing(IntEnum):
    CURRENT = 0
    VELOCITY = 1
    POSITION = 2


_RING_CN = {PidRing.CURRENT: "电流环", PidRing.VELOCITY: "速度环", PidRing.POSITION: "位置环"}
_SOURCE_CN = {PidSource.DEFAULT: "默认", PidSource.FLASH: "Flash", PidSource.AUTOTUNE: "理论估计"}
_SOURCE_COLOR_KEY = {PidSource.DEFAULT: "muted", PidSource.FLASH: "warn", PidSource.AUTOTUNE: "accent"}


class PidPanel(QGroupBox):
    """PID 整定面板 (顶级 Tab)。"""

    # ---- 请求信号 (主窗口连接到 JmClient) ----
    pid_autotune_requested = pyqtSignal(int, float, float, float)  # ring_select, cur_bw, vel_bw, pos_bw
    pid_source_set_requested = pyqtSignal(int, int)                # ring_select, source

    _HISTORY_MAX = 200

    def __init__(self, registry=None, parent=None):
        super().__init__("", parent)
        self._panel_name = "PID 整定"
        self._registry = registry
        self._link_active = False
        self._current_top_fsm = None
        # 三环 source 状态 (本地维护, 协议无查询命令)
        self._ring_source = {r: PidSource.DEFAULT for r in PidRing}
        # 操作历史
        self._history = deque(maxlen=self._HISTORY_MAX)
        # 内嵌参数面板引用 (由主窗口 attach_params_panel 注入)
        self._params_panel = None
        # 构建UI
        self._build()
        self._refresh_status()

    # ========================================================================
    #  UI 构建
    # ========================================================================
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self._build_status_card(layout)
        self._build_operation_panel(layout)
        self._build_results_history_split(layout)

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
        """操作区: 左(理论估计 0x9A) 右(来源切换 0x9B)。"""
        box = QGroupBox("PID 操作")
        h = QHBoxLayout(box)
        h.setContentsMargins(8, 8, 8, 8)
        h.setSpacing(8)

        # ---- 左: 理论估计 ----
        autotune_box = QGroupBox("理论估计 (0x9A)")
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

        # ---- 右: 来源切换 (0x9B) ----
        source_box = QGroupBox("来源切换 (0x9B)")
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

    def _build_results_history_split(self, parent_layout):
        """结果/历史 Splitter: 上(PID 参数表) 下(操作历史)。"""
        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)

        # 上: 参数表占位 (attach_params_panel 时替换)
        self._params_placeholder = QLabel("等待参数面板注入...")
        self._params_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._params_placeholder.setStyleSheet(
            f"color:{theme.hex('muted')}; padding:20px;")
        split.addWidget(self._params_placeholder)

        # 下: 操作历史
        hist_box = QGroupBox("操作历史")
        hv = QVBoxLayout(hist_box)
        hv.setContentsMargins(4, 4, 4, 4)
        self._history_view = QTextEdit()
        self._history_view.setReadOnly(True)
        self._history_view.setMaximumHeight(150)
        hv.addWidget(self._history_view)
        split.addWidget(hist_box)

        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        parent_layout.addWidget(split, 1)
        self._params_split = split

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
        if self._current_top_fsm != int(TopFsm.IDLE):
            self._add_history("[状态拒绝] 仅 IDLE 态可执行理论估计")
            self._set_last_op("需切换到 IDLE 态")
            return
        # 计算 ring_select: 0=电流 1=速度 2=位置 3=全部 (与固件约定)
        ring_select = 0
        for ring, chk in self._autotune_ring_checks.items():
            if chk.isChecked():
                ring_select |= (1 << int(ring))
        if ring_select == 0b111:
            ring_select = 3
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
        """应用来源切换按钮回调: 逐环发送 0x9B。"""
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接")
            return
        if self._current_top_fsm != int(TopFsm.IDLE):
            self._add_history("[状态拒绝] 仅 IDLE 态可切换来源")
            self._set_last_op("需切换到 IDLE 态")
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
    def set_link_active(self, active: bool):
        self._link_active = active
        if not active:
            self._ring_source = {r: PidSource.DEFAULT for r in PidRing}
            self._refresh_source_badges()
        self._refresh_status()

    def update_state(self, top_fsm: int, run_state: int, ctrl_mode: int, enable: int):
        self._current_top_fsm = top_fsm
        self._refresh_status()

    def on_autotune_result(self, result: dict):
        """0x9A PID_AUTOTUNE 应答 (8字节 ACK)。"""
        ok = bool(result.get('ok', False))
        fail_cn = result.get('fail_reason_cn', '未知')
        ring_done = int(result.get('ring_select_done', 0))
        if ok:
            rings_cn = []
            if ring_done in (0, 3):  # 0=电流 或 3=全部
                self._ring_source[PidRing.CURRENT] = PidSource.AUTOTUNE
                rings_cn.append(_RING_CN[PidRing.CURRENT])
            if ring_done in (1, 3):
                self._ring_source[PidRing.VELOCITY] = PidSource.AUTOTUNE
                rings_cn.append(_RING_CN[PidRing.VELOCITY])
            if ring_done in (2, 3):
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

    def on_ack(self, cmd: int):
        """0x9B PID_SOURCE_SET 成功 ACK。"""
        if cmd == int(JmCmd.PID_SOURCE_SET):
            self._add_history("[RX] 来源切换成功")
            self._set_last_op("来源切换完成")
            # 更新本地 source 状态 (从单选组读取)
            for ring_id, grp in self._source_btn_groups.items():
                src = grp.checkedId()
                if src >= 0:
                    self._ring_source[PidRing(ring_id)] = PidSource(src)
            self._refresh_source_badges()

    def on_nack(self, cmd: int, err: int):
        """0x9B PID_SOURCE_SET 失败 NACK。"""
        if cmd == int(JmCmd.PID_SOURCE_SET):
            self._add_history(f"[RX] 来源切换失败 err=0x{err:02X}")
            self._set_last_op(f"来源切换失败 (0x{err:02X})")

    def attach_params_panel(self, panel):
        """注入 ParamPanel 实例, 替换占位控件。"""
        self._params_panel = panel
        # 替换 Splitter 中的占位控件
        idx = self._params_split.indexOf(self._params_placeholder)
        if idx >= 0:
            self._params_placeholder.setParent(None)
            self._params_placeholder.deleteLater()
            self._params_placeholder = None
            self._params_split.insertWidget(idx, panel)
        self._set_last_op("参数面板已加载")

    def set_history_visible(self, visible: bool):
        """切换操作历史区显隐。"""
        if self._history_view:
            self._history_view.setVisible(visible)
            parent = self._history_view.parentWidget()
            if parent:
                parent.setVisible(visible)

    def apply_theme(self):
        """主题切换: 刷新所有控件样式。"""
        self._status_label.setStyleSheet(
            f"font-weight:bold; color:{theme.hex('text_strong')};")
        self._last_op_label.setStyleSheet(f"color:{theme.hex('muted')};")
        if hasattr(self, '_autotune_btn'):
            self._autotune_btn.setStyleSheet(self._ok_btn_qss())
        if hasattr(self, '_source_apply_btn'):
            self._source_apply_btn.setStyleSheet(self._normal_btn_qss())
        self._refresh_source_badges()
        self._refresh_status()
        if self._params_panel and hasattr(self._params_panel, 'apply_theme'):
            self._params_panel.apply_theme()

    def get_opts(self) -> dict:
        return {}

    def set_opts(self, opts: dict):
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
