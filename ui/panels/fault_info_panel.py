"""故障信息面板 (协议交互 + 静态码表参考)

故障管理协议 v1.9 (fault_mgr 分级故障管理) 对接:
  1. 工具栏 — 读取摘要(0xAA) / 自动刷新(1s 周期 0xAA) / 清除故障(0xB0 bit0) / 清除历史(0xB0 bit1) / 加载历史(0xAB type1)
  2. 摘要卡 — 最高优先级故障码+名称 / 活动数 / 降功率% / 三级状态灯(故障级/异常级/警告级)
  3. 诊断页 — 活动故障表(摘要后自动逐条 0xAB type0 拉取) + 历史记录表(手动加载, ≤100 条)
  4. 码表页 — 全量故障码表(实现列可编辑), CSV 可加载/保存/恢复默认, 行号=固件码表序
  5. 保护总览页 — 保护总使能(PID128) + 三层故障级别使能掩码 / 实现期
  6. 参考页 — 虚拟电机 Fault 位(演示模式) + 协议错误码 JmErr

compat_mask/warn_mask 为压缩位图(信息有损), 仅作摘要灯, 不用于定位具体码;
具体故障码列表一律以 0xAB 轮询为准。
"""
import os
from collections import deque
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QBrush, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QScrollArea, QSizePolicy, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

import jmproto as jp
from jmproto import fault_diag as fd
from ui.theme import theme


# ---------- 码表页列定义 ----------
TC_INDEX = 0
TC_CODE = 1
TC_NAME = 2
TC_LEVEL = 3
TC_ACTION = 4
TC_COND = 5
TC_SIGNAL = 6
TC_PARAM = 7
TC_STAGE = 8
TC_DERATE = 9
TC_ALIAS = 10
TC_HANDLING = 11
TC_SOURCE = 12

# ---------- 保护总览表列定义 ----------
PC_ENABLE, PC_STATUS, PC_CODE, PC_NAME, PC_LEVEL, PC_ACTION, PC_STAGE = range(7)

# 基础保护开关位映射 — 以固件 User\AppServices\StateMachine\system_state.h
# SYSTEM_PROTECT_* 宏与 fault_detect_fast.c 消费点实测为准:
#   bit0=过流(0x3102) bit1=过压(0x2102) bit2=欠压(0x2103) bit3=过温(0x3201, 固件暂未消费)
#   bit4=过速(0x4106) bit5=栅极驱动器nFAULT(0x3101; motor_info.csv 注释误写为"跟随误差",
#   跟随误差 0x8108 实由编译期开关 FAULT_DET_EN_FOLLOW_ERR 控制, 不受本掩码门控)
_PROTECT_ITEMS = (
    (0, 0x3102, "过流", "相电流矢量幅值 > 过流阈值 (fault_detect_fast 10kHz 消费)"),
    (1, 0x2102, "过压", "母线电压 > 过压阈值"),
    (2, 0x2103, "欠压", "母线电压 < 欠压阈值 (仅非 IDLE 态武装)"),
    (3, 0x3201, "过温", "固件暂未消费该位(预留语义, 过温当前走降功率路径)"),
    (4, 0x4106, "过速", "机械角速度 > 过速阈值 (仅 READY/RUN 态武装)"),
    (5, 0x3101, "栅极驱动器", "nFAULT 拉低 10ms 消抖 (OCP/UVLO/TSD 硬件保护)"),
)
# PID 128 = protect_comm.protect_enable (u32 总使能, 0xE6/0xE7 读写)
# PID 154~159 = 三级故障使能掩码, 每级低/高32位两个 u32 PID(合成64bit, 0=关闭, 非0=使能)
_FAULT_LEVEL_PARAMS = (("故障级", 154, 155), ("异常级", 156, 157), ("警告级", 158, 159))
_FAULT_LEVEL_BY_NAME = {name: lo for name, lo, _hi in _FAULT_LEVEL_PARAMS}
# 仪表盘级别名短显示(标签), 全名仍用于 CSV level 匹配
_LEVEL_SHORT_NAME = {"故障级": "故障", "异常级": "异常", "警告级": "警告"}

# ---------- 活动故障表列定义 ----------
(AC_CODE, AC_NAME, AC_CAUSE, AC_LEVEL, AC_STATUS, AC_COUNT,
 AC_FIRST, AC_LAST, AC_FIRST_DT, AC_LAST_DT, AC_VALUE) = range(11)

# ---------- 历史记录表列定义 ----------
HC_CODE, HC_NAME, HC_COUNT, HC_FIRST, HC_LAST, HC_VALUE = range(6)

# ---------- 协议错误码表列定义 ----------
COL_ERR = 0
COL_ERRNAME = 1
COL_ERRCN = 2

# 级别中文名 → 主题色键
_LEVEL_THEME_KEY = {
    '故障级': 'danger',
    '异常级': 'value_hot',
    '警告级': 'warn',
}

# 历史记录上限(固件环形缓冲深度 100)
_MAX_HIST_FETCH = 100


class FaultInfoPanel(QWidget):
    """故障信息面板 (顶级 tab)。

    协议交互:
      - set_link_active(active) / auto_read_if_connected() / pause_auto_refresh()
      - on_summary_received(FaultSummary) / on_detail_received(ftype, idx, record)
      - on_fault_enable_mask_received(param_id, value) / refresh_after_clear()
    兼容接口 (主窗口调用):
      - update_fault_mask(mask) / show_active_fault(mask, source) / show_nack_error(cmd, err)
      - set_engine(engine) / set_virtual_mode(on) / get_opts() / set_opts()
    """

    # ---- 请求信号 (主窗口连接到 JmClient) ----
    fault_summary_requested = pyqtSignal()
    fault_detail_requested = pyqtSignal(int, int)      # ftype, idx
    fault_enable_masks_read_requested = pyqtSignal()
    fault_enable_mask_write_requested = pyqtSignal(int, int)  # 掩码 PID(154~159 区段), u32 值(每级低/高32位对分两次写)
    fault_clear_requested = pyqtSignal(int)            # 0xB0 flags
    fault_save_requested = pyqtSignal()                # 0xEA 固化(掩码参数持久化)
    protect_mask_read_requested = pyqtSignal()         # 0xE6 读 PID128 protect_enable
    protect_mask_write_requested = pyqtSignal(int)     # 0xE7 写 PID128 (1/0, RAM 即时生效)
    active_faults_changed = pyqtSignal(list)           # 活动故障名称列表(≤3, 主窗口状态栏摘要)

    _MAX_HISTORY = 50

    def __init__(self, parent=None):
        super().__init__(parent)
        self._panel_name = "故障信息"
        self._current_mask = 0           # 遥测 compat_mask(旧 6 故障兼容位)
        self._history = deque(maxlen=self._MAX_HISTORY)
        self._engine = None              # 虚拟引擎句柄(由主窗口 set_engine 设置)
        self._disable_mask = 0           # 当前虚拟电机故障屏蔽掩码
        self._disable_checks = {}        # bit -> QCheckBox
        self._link_active = False
        self._tab_visible = False        # 主窗口在 tab 切换时维护
        self._summary = None             # 最近一次 0xAA 摘要
        self._hist_load_pending = False  # 等摘要返回后拉历史
        # 0xAB 逐条拉取队列(单请求在途, 应答/NACK 驱动推进)
        self._pull_queue = deque()
        self._pull_active = False
        self._fault_masks = {pid: None for _n, lo, hi in _FAULT_LEVEL_PARAMS
                             for pid in (lo, hi)}  # 掩码 PID -> 设备 RAM u32(None=未读取)
        self._pending_masks = {lo: None for _n, lo, _hi in
                               _FAULT_LEVEL_PARAMS}  # 级别 lo PID -> 待下发 64bit 掩码(None=无修改)
        self._protect_mask = None        # PID128 protect_enable (None=未读取)
        self._active_codes = set()       # 最近一次 0xAB 活动拉取确认的活动故障码集合
        self._active_codes_new = None    # 重建中的暂存集合(None=非重建态)
        self._active_wall = {}           # 活动故障码 -> [首次出现时间, 最近一次时间](上位机墙钟, 会话内)
        self._heat_cells = {}            # 码表 index -> 热力图格 QFrame

        self._ensure_level_bits()
        self._build()
        self._populate_code_table()
        self._populate_protect_table()
        self._populate_fault_bit_table()
        self._populate_err_table()
        self._build_disable_checks()
        self._update_toolbar_state()

    def panel_name(self) -> str:
        return self._panel_name

    # ==================== UI 构建 ====================
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(4, 4, 4, 4)
        body_layout.setSpacing(6)
        scroll.setWidget(body)
        root.addWidget(scroll)

        body_layout.addWidget(self._build_toolbar())
        body_layout.addWidget(self._build_summary_card())
        body_layout.addWidget(self._disable_group_host())
        body_layout.addWidget(self._build_tabs(), 1)

        # 0xAB 单请求超时保护(应答/NACK 正常推进, 超时则放弃队列防卡死)
        self._pull_timer = QTimer(self)
        self._pull_timer.setSingleShot(True)
        self._pull_timer.setInterval(2500)
        self._pull_timer.timeout.connect(self._on_pull_timeout)

        # 自动刷新: 1s 周期 0xAA(仅连接 + 本 tab 可见时运行)
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(1000)
        self._auto_timer.timeout.connect(self._on_auto_tick)

        # 待下发掩码写队列: 0xE7 固件有 100ms 速率限制, 定时器逐笔下发
        self._apply_queue = deque()
        self._apply_timer = QTimer(self)
        self._apply_timer.setInterval(150)
        self._apply_timer.timeout.connect(self._on_apply_tick)

    def _build_toolbar(self):
        bar = QGroupBox()
        lay = QHBoxLayout(bar)

        self._btn_read = QPushButton("读取摘要")
        self._btn_read.setObjectName("primary")
        self._btn_read.clicked.connect(self._on_read_summary)

        self._chk_auto = QCheckBox("自动刷新(1s)")
        self._chk_auto.toggled.connect(self._on_auto_toggled)

        self._btn_clear_fault = QPushButton("清除故障")
        self._btn_clear_fault.clicked.connect(
            lambda: self.fault_clear_requested.emit(fd.CLEAR_FLAG_LATCHED))

        self._btn_clear_hist = QPushButton("清除历史")
        self._btn_clear_hist.clicked.connect(
            lambda: self.fault_clear_requested.emit(fd.CLEAR_FLAG_HISTORY))

        self._btn_load_hist = QPushButton("加载历史")
        self._btn_load_hist.clicked.connect(self._on_load_history)

        self._lbl_link = QLabel("未连接")
        self._lbl_link.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        for w in (self._btn_read, self._chk_auto, self._btn_clear_fault,
                  self._btn_clear_hist, self._btn_load_hist):
            lay.addWidget(w)
        lay.addStretch()
        lay.addWidget(self._lbl_link)
        return bar

    _CARD_QSS = ("QFrame {{ background: {bg}; border: 1px solid {bd}; "
                 "border-radius: 4px; }}")

    def _build_summary_card(self):
        """摘要区双卡左右分布: 左卡=设备故障状态, 右卡=保护总使能(徽章+按钮)。"""
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        outer = QHBoxLayout(host)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        card_qss = self._CARD_QSS.format(bg=theme.hex('card_bottom'),
                                         bd=theme.hex('border'))

        # ---- 左卡: 设备故障状态 ----
        self._status_card = QFrame()
        self._status_card.setFrameShape(QFrame.Shape.StyledPanel)
        self._status_card.setStyleSheet(card_qss)
        sc = QVBoxLayout(self._status_card)
        sc.setContentsMargins(10, 8, 10, 8)
        sc.setSpacing(4)

        # 行0: 标题 + 三级状态灯
        row0 = QHBoxLayout()
        row0.setSpacing(10)
        self._lbl_title = QLabel("● 设备故障状态")
        self._lbl_title.setStyleSheet(
            f"color:{theme.hex('title')}; font-size:13px; font-weight:bold; border:none;")
        row0.addWidget(self._lbl_title)
        row0.addStretch()
        self._lamps = {}
        for level_cn in ('故障级', '异常级', '警告级'):
            lamp = QLabel(f"● {level_cn}")
            lamp.setStyleSheet(f"color:{theme.hex('muted')}; font-size:12px; border:none;")
            row0.addWidget(lamp)
            self._lamps[level_cn] = lamp
        sc.addLayout(row0)

        # 行1: 摘要指标
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(2)
        self._lbl_top = QLabel("最高故障: —")
        self._lbl_top.setStyleSheet(
            f"color:{theme.hex('text')}; font-size:13px; border:none;")
        self._lbl_active = QLabel("活动数: —")
        self._lbl_derate = QLabel("降功率: —")
        self._lbl_hist = QLabel("历史: — 条")
        for i, lbl in enumerate((self._lbl_top, self._lbl_active,
                                 self._lbl_derate, self._lbl_hist)):
            lbl.setStyleSheet(f"color:{theme.hex('text')}; font-size:13px; border:none;")
            grid.addWidget(lbl, 0, i)
        grid.setColumnStretch(4, 1)
        sc.addLayout(grid)

        # 行2: 遥测兼容掩码(压缩位图, 仅摘要)
        self._lbl_compat = QLabel("遥测兼容掩码: 0x0000 无故障")
        self._lbl_compat.setWordWrap(True)
        self._lbl_compat.setStyleSheet(
            f"color:{theme.hex('muted')}; font-size:11px; border:none;")
        sc.addWidget(self._lbl_compat)

        # 行3: 最近事件(故障上升沿/NACK)
        self._lbl_recent = QLabel("最近: —")
        self._lbl_recent.setWordWrap(True)
        self._lbl_recent.setStyleSheet(
            f"color:{theme.hex('text')}; font-family:Consolas,monospace; "
            f"font-size:11px; border:none;")
        sc.addWidget(self._lbl_recent)
        outer.addWidget(self._status_card, 1)

        # ---- 右卡: 保护总使能(徽章 + 启用/禁用按钮) ----
        self._build_protect_flag_controls(outer, card_qss)
        return host

    def _build_protect_flag_controls(self, outer_layout, card_qss):
        """保护总使能卡: 徽章(大字醒目) + 启用/禁用按钮横排, 与状态卡左右分布。

        徽章由 PID128 (protect_enable) 读回值驱动:
          - 1      -> "启用" (绿/accent)
          - 0      -> "禁用" (红/danger)
          - 未读取 -> "未读取" (灰/muted)
        启用/禁用按钮走 0xE7 写 PID128(1/0), RAM 即时生效; 固化由用户手动 0xEA。
        """
        self._protect_card = QFrame()
        self._protect_card.setFrameShape(QFrame.Shape.StyledPanel)
        self._protect_card.setStyleSheet(card_qss)
        self._protect_card.setFixedWidth(170)
        col_layout = QVBoxLayout(self._protect_card)
        col_layout.setContentsMargins(8, 8, 8, 8)
        col_layout.setSpacing(5)
        col_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._protect_badge = QLabel("保护总使能: 未读取")
        self._protect_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._protect_badge.setFixedHeight(26)
        self._protect_badge.setMinimumWidth(140)
        self._protect_badge.setToolTip(
            "PID128 protect_enable: 1=底层快速保护启用 0=禁用(调试用, 谨慎)")
        col_layout.addWidget(self._protect_badge)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(5)
        self._btn_protect_on = QPushButton("启用")
        self._btn_protect_on.setFixedHeight(22)
        self._btn_protect_on.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_protect_on.setToolTip("0xE7 写 PID128=1, 保护总使能开启(RAM 立即生效)")
        self._btn_protect_on.setStyleSheet(self._protect_action_btn_style())
        self._btn_protect_on.clicked.connect(lambda: self._on_protect_enable_cmd(True))
        btn_row.addWidget(self._btn_protect_on)

        self._btn_protect_off = QPushButton("禁用")
        self._btn_protect_off.setFixedHeight(22)
        self._btn_protect_off.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_protect_off.setToolTip("0xE7 写 PID128=0, 保护总使能关闭(调试用, 谨慎)")
        self._btn_protect_off.setStyleSheet(self._protect_action_btn_style())
        self._btn_protect_off.clicked.connect(lambda: self._on_protect_enable_cmd(False))
        btn_row.addWidget(self._btn_protect_off)
        col_layout.addLayout(btn_row)

        outer_layout.addWidget(self._protect_card)
        self._set_protect_badge(None)

    def _protect_action_btn_style(self) -> str:
        """启用/禁用按钮样式 (同标定页标记/清除按钮, 主题色驱动)。"""
        return (
            f"QPushButton {{ background: {theme.hex('input_bg')}; color: {theme.hex('text')}; "
            f"border: 1px solid {theme.hex('border')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 11px; }}"
            f"QPushButton:hover {{ border-color: {theme.hex('accent')}; "
            f"color: {theme.hex('accent')}; }}"
            f"QPushButton:disabled {{ color: {theme.hex('muted')}; "
            f"border-color: {theme.hex('border')}; background: {theme.hex('input_bg')}; }}"
        )

    def _disable_group_host(self):
        """虚拟电机故障屏蔽区(仅演示模式可见, set_virtual_mode 控制)。"""
        self._disable_group = QGroupBox("故障屏蔽 (虚拟电机演示)")
        self._disable_group.setStyleSheet(f"""
            QGroupBox {{
                border: 1px solid {theme.hex('border')};
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 8px;
                font-size: 12px;
                color: {theme.hex('title')};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
            }}
        """)
        dg_layout = QVBoxLayout(self._disable_group)
        dg_layout.setContentsMargins(8, 14, 8, 8)
        dg_layout.setSpacing(4)

        info_lbl = QLabel("勾选对应故障位后, 该故障不再触发(不进 FAULT)。仅对虚拟电机生效。")
        info_lbl.setWordWrap(True)
        info_lbl.setObjectName("ve_hint")
        info_lbl.setStyleSheet(f"color:{theme.hex('muted')}; font-size:11px; border:none;")
        dg_layout.addWidget(info_lbl)

        checks_grid = QGridLayout()
        checks_grid.setHorizontalSpacing(16)
        checks_grid.setVerticalSpacing(2)
        self._disable_container = QWidget()
        # 裸 QWidget 会被全局 QSS 染色, 置透明
        self._disable_container.setStyleSheet("background: transparent;")
        self._disable_container.setLayout(checks_grid)
        dg_layout.addWidget(self._disable_container)

        mask_row = QHBoxLayout()
        mask_row.setSpacing(8)
        self._lbl_disable_mask = QLabel("屏蔽掩码: 0x0000")
        self._lbl_disable_mask.setStyleSheet(
            f"color:{theme.hex('muted')}; font-family:Consolas,monospace; font-size:11px; border:none;")
        mask_row.addWidget(self._lbl_disable_mask)
        mask_row.addStretch()
        btn_clear_disable = QPushButton("全部清除")
        btn_clear_disable.setFixedHeight(22)
        btn_clear_disable.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_clear_disable.clicked.connect(self._on_clear_disable)
        mask_row.addWidget(btn_clear_disable)
        dg_layout.addLayout(mask_row)

        self._disable_group.setVisible(False)
        return self._disable_group

    def _build_tabs(self):
        self._tabs = QTabWidget()
        self._tabs.tabBar().setExpanding(True)   # 4 个 tab 等宽平分
        # 保护总览放第一位: 一眼看清生效/屏蔽/未实现
        self._protect_tab = self._build_protect_tab()
        self._tabs.addTab(self._protect_tab, "保护总览")
        self._tabs.addTab(self._build_diag_tab(), "诊断")
        # 码表条数动态(可加载自定义 CSV), 由 _update_code_tab_title 维护
        self._code_tab_idx = self._tabs.addTab(
            self._build_code_tab(), f"码表 ({len(jp.load_fault_csv())})")
        # 保存虚拟电机 Fault 位子表 widget, 供虚拟电机模式开关控制显隐
        self._fault_bit_tab = self._build_fault_bit_tab()
        self._tabs.addTab(self._fault_bit_tab, "虚拟电机 Fault 位")
        self._tabs.addTab(self._build_err_tab(), "协议错误码")
        # 虚拟电机模式默认关闭: 初始隐藏"虚拟电机 Fault 位"子表与故障屏蔽区
        self._tabs.setTabVisible(self._tabs.indexOf(self._fault_bit_tab), False)
        # 子页切换: 进入保护总览时读取保护总使能(PID 128)
        self._tabs.currentChanged.connect(self._on_sub_tab_changed)
        return self._tabs

    def _update_code_tab_title(self):
        """码表 tab 标题同步当前条数(加载/保存/恢复默认码表后调用)。"""
        if hasattr(self, "_code_tab_idx"):
            self._tabs.setTabText(
                self._code_tab_idx, f"码表 ({len(jp.load_fault_csv())})")

    # ---------- 诊断页: 活动故障表 + 历史记录表 ----------
    def _build_diag_tab(self) -> QWidget:
        w = QWidget()
        # 裸 QWidget 容器置透明, 避免全局 QSS 染成黑块
        w.setStyleSheet("background: transparent;")
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        active_box = QGroupBox("活动故障 (0xAB type=0 优先级序)")
        av = QVBoxLayout(active_box)
        av.setContentsMargins(8, 14, 8, 8)
        self._active_table = QTableWidget(0, 11)
        self._active_table.setHorizontalHeaderLabels(
            ["码", "名称", "故障原因", "级别", "状态", "次数",
             "首次(ms)", "最近(ms)", "首次出现时间", "最近一次时间", "触发值"])
        self._config_proto_table(self._active_table)
        hdr = self._active_table.horizontalHeader()
        self._active_table.setColumnWidth(AC_CODE, 76)
        self._active_table.setColumnWidth(AC_NAME, 130)
        # 故障原因列为默认最大列宽(Stretch 吸收窗口多余宽度)
        hdr.setSectionResizeMode(AC_CAUSE, QHeaderView.ResizeMode.Stretch)
        self._active_table.setColumnWidth(AC_LEVEL, 56)
        self._active_table.setColumnWidth(AC_STATUS, 56)
        self._active_table.setColumnWidth(AC_COUNT, 52)
        self._active_table.setColumnWidth(AC_FIRST, 76)
        self._active_table.setColumnWidth(AC_LAST, 76)
        self._active_table.setColumnWidth(AC_FIRST_DT, 128)
        self._active_table.setColumnWidth(AC_LAST_DT, 128)
        self._active_table.setColumnWidth(AC_VALUE, 76)
        av.addWidget(self._active_table)
        v.addWidget(active_box, 3)

        hist_box = QGroupBox("历史记录 (0xAB type=1 时间倒序, 最多 100 条)")
        hv = QVBoxLayout(hist_box)
        hv.setContentsMargins(8, 14, 8, 8)
        self._hist_table = QTableWidget(0, 6)
        self._hist_table.setHorizontalHeaderLabels(
            ["码", "名称", "次数", "首次(ms)", "最近(ms)", "触发值"])
        self._config_proto_table(self._hist_table)
        hdr = self._hist_table.horizontalHeader()
        hdr.setSectionResizeMode(HC_NAME, QHeaderView.ResizeMode.Stretch)
        self._hist_table.setColumnWidth(HC_CODE, 76)
        self._hist_table.setColumnWidth(HC_COUNT, 56)
        self._hist_table.setColumnWidth(HC_FIRST, 92)
        self._hist_table.setColumnWidth(HC_LAST, 92)
        self._hist_table.setColumnWidth(HC_VALUE, 90)
        hv.addWidget(self._hist_table)
        v.addWidget(hist_box, 2)
        return w

    def _config_proto_table(self, table: QTableWidget):
        """诊断页两张记录表的公共配置。"""
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(24)
        table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

    # ---------- 码表页: 109 条全表(实现标志可编辑, CSV 可加载/保存) ----------
    def _build_code_tab(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        info = QLabel(
            "共 109 条故障码(按码值升序)。检测状态由故障级别使能掩码决定，"
            "请在\"保护总览\"页按行勾选逐故障使能/禁用；修改后点击\"下发修改\"写入设备, 需要重启保持再手动\"固化参数\"。"
            "\"实现\"列勾选=固件已实现该检测(可编辑), 修改后点\"保存CSV\"写回本地码表文件; "
            "固件侧需重跑 fault_param_generate.py 重新生成码表并编译。")
        info.setObjectName("code_hint")
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{theme.hex('muted')}; font-size:11px; border:none;")
        v.addWidget(info)

        self._code_table = QTableWidget(0, 13)
        self._code_table.setHorizontalHeaderLabels(
            ["Index", "故障码", "故障名称", "级别", "处理方式", "触发条件", "信号源",
             "参数Key", "实现", "降功率档", "异常命名", "处理动作", "来源"])
        self._code_table.setAlternatingRowColors(True)
        self._code_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._code_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._code_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._code_table.setWordWrap(False)
        self._code_table.setShowGrid(True)
        self._code_table.setSortingEnabled(False)   # 保持行号=码表 index, 勾选映射依赖
        self._code_table.verticalHeader().setVisible(False)
        self._code_table.verticalHeader().setDefaultSectionSize(24)
        self._code_table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        hdr = self._code_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)  # 全列可自由拖动
        # 窗口多余宽度全部给"触发条件"列(其余列固定, 仅手动调整)
        hdr.setSectionResizeMode(TC_COND, QHeaderView.ResizeMode.Stretch)
        widths = {
            TC_INDEX: 42, TC_CODE: 70, TC_LEVEL: 58, TC_ACTION: 110,
            TC_SIGNAL: 110, TC_PARAM: 120, TC_STAGE: 46, TC_DERATE: 58,
            TC_ALIAS: 110, TC_HANDLING: 92, TC_SOURCE: 66,
        }
        for col, wd in widths.items():
            self._code_table.setColumnWidth(col, wd)
        v.addWidget(self._code_table, 1)

        op_row = QHBoxLayout()
        op_row.addWidget(QLabel("分类:"))
        self._code_filter_source = QComboBox()
        self._code_filter_source.addItem("全部来源")
        for src in self._fault_sources():
            self._code_filter_source.addItem(src)
        self._code_filter_source.setFixedHeight(24)
        self._code_filter_source.setMinimumWidth(120)
        self._code_filter_source.currentTextChanged.connect(
            lambda _t: self._apply_code_filter())
        op_row.addWidget(self._code_filter_source)
        op_row.addStretch()
        self._lbl_csv_path = QLabel(jp.get_fault_csv_path())
        self._lbl_csv_path.setStyleSheet(
            f"color:{theme.hex('muted')}; font-size:10px; border:none;")
        self._lbl_csv_path.setToolTip("当前故障码表 CSV 路径(加载/保存目标)")
        op_row.addWidget(self._lbl_csv_path)
        btn_load_csv = QPushButton("加载CSV...")
        btn_load_csv.setFixedHeight(24)
        btn_load_csv.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_load_csv.setToolTip("选择本地故障码表 CSV 文件加载(12 列, 同 fault_param.csv 格式)")
        btn_load_csv.clicked.connect(self._on_load_fault_csv)
        op_row.addWidget(btn_load_csv)
        self._btn_save_csv = QPushButton("保存CSV")
        self._btn_save_csv.setFixedHeight(24)
        self._btn_save_csv.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_save_csv.setObjectName("primary")
        self._btn_save_csv.setToolTip("把当前码表(含\"实现\"列修改)写回当前 CSV 路径")
        self._btn_save_csv.clicked.connect(self._on_save_fault_csv)
        op_row.addWidget(self._btn_save_csv)
        btn_default_csv = QPushButton("恢复默认")
        btn_default_csv.setFixedHeight(24)
        btn_default_csv.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_default_csv.setToolTip("切回打包内置码表 resources/fault_param.csv 并重新加载")
        btn_default_csv.clicked.connect(self._on_restore_default_csv)
        op_row.addWidget(btn_default_csv)
        btn_refresh = QPushButton("重新加载")
        btn_refresh.setFixedHeight(24)
        btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_refresh.clicked.connect(self._on_reload_csv)
        op_row.addWidget(btn_refresh)
        v.addLayout(op_row)
        return w

    # ---------- 码表 CSV 加载/保存 ----------
    def _reload_all_tables(self):
        """码表数据变化后全量刷新: 码表/总览明细/热力图/图例/虚拟位表 + tab 标题。"""
        self._ensure_level_bits()
        self._populate_code_table()
        self._populate_protect_table()
        self._populate_fault_bit_table()
        self._update_heatmap()
        self._update_protect_chips()
        self._update_code_tab_title()
        self._refresh_compat()

    def _on_load_fault_csv(self):
        """加载本地故障码表 CSV(参考电机配置 CSV 加载方式)。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择故障码表 CSV", os.path.dirname(jp.get_fault_csv_path())
            if os.path.isabs(jp.get_fault_csv_path()) else "",
            "CSV 文件 (*.csv);;所有文件 (*)")
        if not path:
            return
        try:
            jp.set_fault_csv_path(path)
            records = jp.load_fault_csv()
        except Exception as e:
            self._append_recent(f"加载码表失败: {e}")
            return
        if not records:
            self._append_recent(f"码表为空或格式不符: {path}")
            return
        self._lbl_csv_path.setText(path)
        self._reload_all_tables()
        self._append_recent(f"故障码表已加载 {len(records)} 条: {path}")

    def _on_save_fault_csv(self):
        """保存码表(含实现列修改)到当前 CSV 路径。"""
        try:
            n = jp.save_fault_csv()
        except Exception as e:
            self._append_recent(f"保存码表失败: {e}")
            return
        self._reload_all_tables()
        self._append_recent(
            f"故障码表已保存 {n} 条: {jp.get_fault_csv_path()}"
            "(固件侧请重跑 fault_param_generate.py 并重新编译)")

    def _on_restore_default_csv(self):
        """恢复默认打包码表。"""
        jp.set_fault_csv_path(jp.FAULT_PARAM_CSV)
        self._lbl_csv_path.setText(jp.get_fault_csv_path())
        self._reload_all_tables()
        self._append_recent("已恢复默认故障码表")

    def _populate_code_table(self):
        self._code_table.setRowCount(0)
        records = jp.load_fault_csv()
        for rec in records:
            row = self._code_table.rowCount()
            self._code_table.insertRow(row)
            self._code_table.setRowHeight(row, 24)

            texts = {
                TC_INDEX: str(rec['index']),
                TC_CODE: rec['code'],
                TC_NAME: rec['name'],
                TC_LEVEL: rec['level'],
                TC_ACTION: rec['action'],
                TC_COND: rec['condition'],
                TC_SIGNAL: rec['signal'],
                TC_PARAM: rec['param_key'],
                TC_DERATE: rec['derate'],
                TC_ALIAS: rec['alias'],
                TC_HANDLING: rec['handling'],
                TC_SOURCE: rec['source'],
            }
            for col, text in texts.items():
                it = QTableWidgetItem(text)
                it.setTextAlignment(
                    Qt.AlignmentFlag.AlignVCenter |
                    (Qt.AlignmentFlag.AlignCenter
                     if col in (TC_INDEX, TC_CODE, TC_LEVEL, TC_DERATE)
                     else Qt.AlignmentFlag.AlignLeft))
                if col in (TC_INDEX, TC_CODE):
                    f = QFont("Consolas")
                    it.setFont(f)
                self._code_table.setItem(row, col, it)

            # "实现"列: 勾选框(固件已实现该检测), 修改仅入内存, 由"保存CSV"写回
            chk = QCheckBox()
            chk.setStyleSheet("QCheckBox::indicator { width: 16px; height: 16px; }")
            chk.setChecked(rec['stage'] == '1')
            chk.setToolTip(
                "勾选=固件已实现该检测(码表可触发)\n修改后点\"保存CSV\"写回本地码表文件")
            chk.toggled.connect(
                lambda checked, idx=rec['index']: self._on_stage_toggled(idx, checked))
            self._code_table.setCellWidget(row, TC_STAGE, chk)

            # 级别着色
            key = _LEVEL_THEME_KEY.get(rec['level'])
            if key:
                lvl_item = self._code_table.item(row, TC_LEVEL)
                lvl_item.setForeground(QBrush(theme.c(key)))
                f = QFont()
                f.setBold(True)
                lvl_item.setFont(f)

    def _on_stage_toggled(self, index: int, checked: bool):
        """码表"实现"勾选变化: 更新内存记录(stage=1/0), 由"保存CSV"持久化。"""
        rec = jp.fault_record_by_index(index)
        if rec is None:
            return
        rec['stage'] = '1' if checked else '0'
        self._append_recent(
            f"码表 {rec['code']} {rec['name']} 标记为"
            f"{'已实现' if checked else '未实现'}(点\"保存CSV\"写回)")

    def _on_reload_csv(self):
        """重新加载 CSV (用户更新 CSV 后)。"""
        jp.clear_fault_cache()
        self._reload_all_tables()



    # ---------- 保护总览页: 三层保护状态 + 一键使能 ----------
    def _build_protect_tab(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        v = QVBoxLayout(w)
        # 与 tab 边框保持间隔; 与仪表盘总框的间隔由 spacing 决定
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(10)

        # ---- 使能位仪表盘(GitHub 风格热力图): 总框 = 图例 + 三级[级别名|64bit 子框] ----
        v.addWidget(self._build_heatmap())

        # ---- 明细表总框: 筛选行 + 109 码表(与仪表盘总框同款样式) ----
        table_box = QFrame()
        table_box.setObjectName("dash_frame")
        table_box.setStyleSheet(self._BOX_QSS.format(bd=theme.hex('border')))
        tb = QVBoxLayout(table_box)
        tb.setContentsMargins(10, 8, 10, 8)
        tb.setSpacing(6)

        # ---- 筛选行: 状态(默认已实现) + 分类(来源域) + 批量操作 ----
        op_row = QHBoxLayout()
        op_row.addWidget(QLabel("显示:"))
        self._filter_combo = QComboBox()
        self._filter_combo.addItems(["全部", "生效中", "已禁用", "已修改", "未实现", "已实现"])
        self._filter_combo.setCurrentText("已实现")
        self._filter_combo.setFixedHeight(24)
        self._filter_combo.currentTextChanged.connect(
            lambda _t: self._apply_protect_filter())
        op_row.addWidget(self._filter_combo)
        op_row.addWidget(QLabel("分类:"))
        self._filter_source = QComboBox()
        self._filter_source.addItem("全部来源")
        for src in self._fault_sources():
            self._filter_source.addItem(src)
        self._filter_source.setFixedHeight(24)
        self._filter_source.setMinimumWidth(120)
        self._filter_source.currentTextChanged.connect(
            lambda _t: self._apply_protect_filter())
        op_row.addWidget(self._filter_source)
        op_row.addStretch()
        self._btn_enable_all = QPushButton("全部故障级别使能")
        self._btn_enable_all.setFixedHeight(26)
        self._btn_enable_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_enable_all.setToolTip(
            "三级待下发掩码置 0xFFFFFFFF(行高亮), 需点击\"下发修改\"写入设备")
        self._btn_enable_all.clicked.connect(self._on_enable_all)
        self._btn_apply_masks = QPushButton("下发修改")
        self._btn_apply_masks.setObjectName("primary")
        self._btn_apply_masks.setFixedHeight(26)
        self._btn_apply_masks.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_apply_masks.setToolTip(
            "把勾选产生的待下发掩码写入设备(0xE7, RAM 立即生效, 不固化)")
        self._btn_apply_masks.clicked.connect(self._on_apply_masks)
        self._btn_save_now = QPushButton("固化参数")
        self._btn_save_now.setFixedHeight(26)
        self._btn_save_now.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_save_now.setToolTip("0xEA 把当前 RAM 参数写入存储(重启保持), 由用户手动决定")
        self._btn_save_now.clicked.connect(self._on_save_now)
        self._btn_read_protect = QPushButton("读取全部状态")
        self._btn_read_protect.setFixedHeight(26)
        self._btn_read_protect.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_read_protect.setToolTip("0xE6 读取 PID128 保护总使能及 PID154~159 故障级别掩码")
        self._btn_read_protect.clicked.connect(self._on_read_all_status)
        op_row.addWidget(self._btn_enable_all)
        op_row.addWidget(self._btn_apply_masks)
        op_row.addWidget(self._btn_save_now)
        op_row.addWidget(self._btn_read_protect)
        tb.addLayout(op_row)

        # ---- 109 码明细表(只读, 状态由故障级别掩码决定) ----
        self._prot_table = QTableWidget(0, 7)
        self._prot_table.setHorizontalHeaderLabels(
            ["使能", "状态", "故障码", "名称", "级别", "处理方式", "实现"])
        self._prot_table.setAlternatingRowColors(True)
        self._prot_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._prot_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._prot_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._prot_table.setWordWrap(False)
        self._prot_table.setShowGrid(True)
        self._prot_table.setSortingEnabled(False)   # 保持行号=码表 index
        self._prot_table.verticalHeader().setVisible(False)
        self._prot_table.verticalHeader().setDefaultSectionSize(26)
        self._prot_table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        hdr = self._prot_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(PC_NAME, QHeaderView.ResizeMode.Stretch)
        for col, wd in ((PC_ENABLE, 44), (PC_STATUS, 82), (PC_CODE, 70),
                        (PC_LEVEL, 58), (PC_ACTION, 130), (PC_STAGE, 56)):
            self._prot_table.setColumnWidth(col, wd)
        tb.addWidget(self._prot_table, 1)
        v.addWidget(table_box, 1)
        return w

    _HEAT_GRAY = "#30363D"    # 未实现(固件预留)
    _HEAT_BLUE = "#3E7DB8"    # 实现但已禁用(暗蓝, 降低与绿格的视觉竞争)
    _HEAT_GREEN = "#3FB950"   # 实现且使能
    _HEAT_RED = "#F85149"     # 使能且报错活动
    _HEAT_PEND = "#E3B341"    # 待下发边框
    _HEAT_UNKNOWN = "#1B1F26"  # 掩码尚未读取(暗灰+虚框)

    # 总框样式(仪表盘/明细表共用, objectName=dash_frame)
    _BOX_QSS = ("QFrame#dash_frame {{ background: transparent; "
                "border: 1px solid {bd}; border-radius: 6px; }}")

    def _build_heatmap(self) -> QWidget:
        """GitHub 风格使能位仪表盘总框: 图例行 + 三级[竖排级别名 | 64bit 子框(低32/高32)],
        格子随窗口宽度均分拉伸填满右侧。"""
        box = QFrame()
        box.setObjectName("dash_frame")
        box.setStyleSheet(self._BOX_QSS.format(bd=theme.hex('border')))
        outer = QVBoxLayout(box)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(8)

        # ---- 图例行: 色块 + 说明 + 计数(计数由 _update_protect_chips 刷新) ----
        legend = QHBoxLayout()
        legend.setSpacing(4)
        self._legend_counts = {}
        for key, bg, border, text in (
                ("gray", self._HEAT_GRAY, None, "未实现"),
                ("blue", self._HEAT_BLUE, None, "已禁用"),
                ("green", self._HEAT_GREEN, None, "使能中"),
                ("red", self._HEAT_RED, None, "报错活动"),
                ("pend", "transparent", self._HEAT_PEND, "待下发")):
            sw = QFrame()
            sw.setFixedSize(11, 11)
            sw.setStyleSheet(
                f"background:{bg}; border:{'none' if border is None else f'1px solid {border}'}; "
                f"border-radius:2px;")
            legend.addWidget(sw)
            lab = QLabel(f"{text} 0")
            lab.setStyleSheet(f"color:{theme.hex('muted')}; font-size:11px; border:none;")
            legend.addWidget(lab)
            self._legend_counts[key] = lab
            legend.addSpacing(10)
        legend.addStretch()
        outer.addLayout(legend)

        self._heat_cells.clear()      # (pid, bit) -> QFrame
        self._level_name_labels = {}  # 级别名 -> QLabel(计数/PID 掩码信息在 tooltip)
        for name, lo, hi in _FAULT_LEVEL_PARAMS:
            row = QHBoxLayout()
            row.setSpacing(6)
            # 级别名竖排(列布局), 紧贴子框
            short = _LEVEL_SHORT_NAME.get(name, name)
            name_lbl = QLabel("\n".join(short))
            name_lbl.setFixedWidth(20)
            name_lbl.setAlignment(
                Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
            name_lbl.setStyleSheet(
                f"color:{theme.hex('text')}; font-size:12px; font-weight:600; border:none;")
            name_lbl.setToolTip(f"{name}  PID{lo}~{hi}")
            row.addWidget(name_lbl)
            self._level_name_labels[name] = name_lbl

            # 64bit 子框: 两行(低32/高32), 格子均分填满右侧空白, 可随窗口拉伸
            frame = QFrame()
            frame.setObjectName("heat_frame")
            frame.setStyleSheet(f"""
                QFrame#heat_frame {{
                    background: transparent;
                    border: 1px solid {theme.hex('border')};
                    border-radius: 4px;
                }}
            """)
            fl = QVBoxLayout(frame)
            fl.setContentsMargins(8, 6, 8, 6)
            fl.setSpacing(5)
            for half, pid in ((0, lo), (1, hi)):
                h = QHBoxLayout()
                h.setContentsMargins(0, 0, 0, 0)
                h.setSpacing(4)
                lab = QLabel("低32位" if half == 0 else "高32位")
                lab.setFixedWidth(42)
                lab.setAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                lab.setStyleSheet(f"color:{theme.hex('muted')}; font-size:10px; border:none;")
                h.addWidget(lab)
                for b in range(32):
                    bit = half * 32 + b
                    cell = QFrame()
                    cell.setFixedHeight(18)
                    cell.setMinimumWidth(10)
                    cell.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Fixed)
                    cell.setStyleSheet(
                        f"background:{self._HEAT_GRAY}; border:none; border-radius:2px;")
                    h.addWidget(cell, 1)
                    self._heat_cells[(pid, bit)] = cell
                fl.addLayout(h)
            row.addWidget(frame, 1)
            outer.addLayout(row)
        self._update_heatmap()
        self._update_level_mask_label()
        return box

    def _update_heatmap(self):
        """按 合并视图掩码 + 活动故障集合 刷新 3×64 格颜色与 tooltip。
        颜色: 灰=未实现/保留位  蓝=已禁用  绿=使能中  红=使能且报错活动;
        虚框=掩码尚未读取; 待下发修改的位加黄色边框。"""
        # 级别 lo PID -> 级别内 bit -> 故障记录
        bit_map = {}
        for rec in jp.load_fault_csv():
            pid = self._fault_level_pid(rec)
            if pid is not None:
                bit_map.setdefault(pid, {})[rec.get('level_bit', 0)] = rec
        for name, lo, hi in _FAULT_LEVEL_PARAMS:
            dev_lo = self._fault_masks.get(lo)
            dev_hi = self._fault_masks.get(hi)
            m_dev = None if (dev_lo is None or dev_hi is None) \
                else ((dev_hi << 32) | dev_lo)
            pend = self._pending_masks.get(lo)
            m = pend if pend is not None else m_dev
            for half, pid in ((0, lo), (1, hi)):
                for b in range(32):
                    bit = half * 32 + b
                    cell = self._heat_cells.get((pid, bit))
                    if cell is None:
                        continue
                    rec = bit_map.get(lo, {}).get(bit)
                    if rec is None:
                        cell.setStyleSheet(
                            f"background:{self._HEAT_GRAY}; border:none; border-radius:2px;")
                        cell.setToolTip(f"BIT{bit}  (PID{pid})\n保留位(码表未定义)")
                        continue
                    impl = rec['stage'] != '0'
                    active = rec['code_int'] in self._active_codes
                    tip = [f"BIT{bit}  (PID{pid})",
                           f"{rec['code']}  {rec['name']}",
                           f"级别: {rec['level']}"]
                    if not impl:
                        color, border, st = self._HEAT_GRAY, "none", "未实现(固件预留, 不会触发)"
                    elif m is None:
                        color = self._HEAT_UNKNOWN
                        border = f"1px dashed {theme.hex('border')}"
                        st = "尚未读取掩码(点\"读取全部状态\")"
                    elif active:
                        color, border, st = self._HEAT_RED, "none", "使能中且报错活动!"
                    elif (m >> bit) & 1:
                        color, border, st = self._HEAT_GREEN, "none", "使能中"
                    else:
                        color, border, st = self._HEAT_BLUE, "none", "已禁用"
                    tip.append(f"状态: {st}")
                    gate = self._protect_gate_note(rec)
                    if gate:
                        tip.append(gate)
                    if border == "none" and pend is not None and impl and \
                            (m_dev is None or ((pend ^ m_dev) >> bit) & 1):
                        tip.append("已修改(待下发)")
                        border = f"1px solid {self._HEAT_PEND}"
                    cell.setStyleSheet(
                        f"background:{color}; border:{border}; border-radius:2px;")
                    cell.setToolTip("\n".join(tip))

    def _set_protect_badge(self, enabled):
        """保护总使能徽章: None=未读取(灰), True=启用(绿), False=禁用(红), 样式同标定页。"""
        text = "未读取" if enabled is None else ("启用" if enabled else "禁用")
        self._protect_badge.setText(f"保护总使能: {text}")
        self._protect_badge.setStyleSheet(self._protect_badge_style(enabled))

    def _protect_badge_style(self, enabled) -> str:
        """徽章样式: 启用绿 / 禁用红 / 未读取灰 (与标定页已标定徽章同款)。"""
        if enabled is True:
            bg, fg = theme.hex('accent'), theme.hex('card_bottom')
        elif enabled is False:
            bg, fg = theme.hex('danger'), theme.hex('danger_text')
        else:
            bg, fg = theme.hex('input_bg'), theme.hex('muted')
        return (f"QLabel {{ background: {bg}; color: {fg}; "
                f"border: 1px solid {theme.hex('border')}; border-radius: 3px; "
                f"padding: 1px 8px; font-size: 11px; font-weight: bold; }}")

    def _on_protect_enable_cmd(self, checked: bool):
        """保护总使能设置按钮: 0xE7 写 PID128(1/0, RAM 即时生效); 固化由用户手动。"""
        if not self._link_active:
            return
        value = 1 if checked else 0
        self._protect_mask = value
        self._set_protect_badge(enabled=bool(value))
        self.protect_mask_write_requested.emit(value)
        self._append_recent(
            f"保护总使能 {'启用' if checked else '禁用'} → PID128={value}(固化请手动点击)")
        for rec in jp.load_fault_csv():
            if self._protect_gate_note(rec):
                self._set_protect_row_state(rec['index'])
        self._update_protect_chips()

    @staticmethod
    def _ensure_level_bits():
        """为码表每条记录计算级别内序号(64bit 掩码 bit 位)。
        规则与固件 fault_mgr_build_level_bits 一致: 同级别前驱计数(码表序)。"""
        cnt = {}
        for rec in jp.load_fault_csv():
            b = cnt.get(rec['level'], 0)
            rec['level_bit'] = b
            cnt[rec['level']] = b + 1

    @staticmethod
    def _fault_sources():
        """码表来源域列表(按出现序, 供分类下拉)。"""
        seen = []
        for rec in jp.load_fault_csv():
            src = rec.get('source')
            if src and src not in seen:
                seen.append(src)
        return seen

    @staticmethod
    def _fault_level_pid(rec: dict):
        return _FAULT_LEVEL_BY_NAME.get(rec.get("level"))

    def _level_mask64(self, lo_pid: int):
        """合成级别 64bit 掩码；任一半未读取返回 None。"""
        lo = self._fault_masks.get(lo_pid)
        hi = self._fault_masks.get(lo_pid + 1)
        if lo is None or hi is None:
            return None
        return (hi << 32) | lo

    def _fault_level_enabled(self, rec: dict):
        """返回故障是否使能(合并视图: 待下发掩码优先于设备 RAM)；None=尚未读取。"""
        pid = self._fault_level_pid(rec)
        if pid is None:
            return None
        cur = self._pending_masks.get(pid)
        if cur is None:
            cur = self._level_mask64(pid)
        if cur is None:
            return None
        return bool((cur >> rec['level_bit']) & 1)

    def _on_code_enable_toggled(self, index: int, checked: bool):
        """码表勾选变化: 仅更新本地待下发掩码并高亮该行, 由\"下发修改\"统一下发。"""
        rec = jp.fault_record_by_index(index)
        if rec is None or rec['stage'] == '0':
            return
        lo_pid = self._fault_level_pid(rec)
        if lo_pid is None:
            return
        cur = self._pending_masks.get(lo_pid)
        if cur is None:
            cur = self._level_mask64(lo_pid)
            if cur is None:
                # 设备掩码尚未读取: 无法计算位, 还原勾选并提示
                chk = self._prot_table.cellWidget(index, PC_ENABLE)
                if isinstance(chk, QCheckBox):
                    chk.blockSignals(True)
                    chk.setChecked(False)
                    chk.blockSignals(False)
                self._append_recent("请先点击\"读取全部状态\"获取设备掩码后再修改")
                return
        bit = 1 << rec['level_bit']
        new = (cur | bit) if checked else (cur & ~bit)
        self._pending_masks[lo_pid] = new & 0xFFFFFFFFFFFFFFFF
        name = next((name for name, lo, _hi in _FAULT_LEVEL_PARAMS if lo == lo_pid),
                    f"PID{lo_pid}")
        self._append_recent(
            f"0x{rec['code_int']:04X} {rec['name']} {'使能' if checked else '禁用'}(待下发, {name})")
        self._set_protect_row_state(index)
        self._update_heatmap()
        self._update_protect_chips()

    def _on_apply_masks(self):
        """下发修改: 把待下发掩码按 低/高32位对 逐笔写入设备(RAM 即时生效), 完成后读回确认。"""
        if not self._link_active:
            return
        self._apply_queue.clear()
        for _name, lo, hi in _FAULT_LEVEL_PARAMS:
            m = self._pending_masks.get(lo)
            if m is None:
                continue
            self._apply_queue.append((lo, int(m) & 0xFFFFFFFF))
            self._apply_queue.append((hi, (int(m) >> 32) & 0xFFFFFFFF))
        if not self._apply_queue:
            self._append_recent("没有待下发的修改")
            return
        self._apply_timer.start()
        self._append_recent(f"开始下发 {len(self._apply_queue)} 笔掩码写入...")

    def _on_apply_tick(self):
        """逐笔发送待下发掩码(150ms 间隔, 避开固件 0xE7 100ms 速率限制)。
        下发后不读回: 行保持"已修改"高亮, 待固化后重新加载才刷新状态。"""
        if not self._apply_queue:
            self._apply_timer.stop()
            self._append_recent("掩码下发完成(RAM 已生效, 未固化); 固化并重新加载后状态将刷新")
            return
        pid, value = self._apply_queue.popleft()
        self.fault_enable_mask_write_requested.emit(pid, value)

    def _apply_protect_filter(self):
        """按 状态+分类 双条件显隐总览表明细行(行号=码表 index)。"""
        if not hasattr(self, "_filter_combo"):
            return
        mode = self._filter_combo.currentText()
        src = self._filter_source.currentText()
        for idx in range(self._prot_table.rowCount()):
            rec = jp.fault_record_by_index(idx)
            if rec is None:
                continue
            enabled = self._fault_level_enabled(rec)
            impl = rec['stage'] != '0'
            # 按位判断"已修改"(与 _set_protect_row_state 一致)
            pend = self._pending_masks.get(self._fault_level_pid(rec) or -1) if impl else None
            if pend is None:
                modified = False
            else:
                m_dev = self._level_mask64(self._fault_level_pid(rec))
                modified = m_dev is None or bool(
                    (pend ^ m_dev) & (1 << rec['level_bit']))
            if mode == "生效中":
                show = impl and enabled is not False
            elif mode == "已禁用":
                show = impl and enabled is False and not modified
            elif mode == "已修改":
                show = impl and modified
            elif mode == "未实现":
                show = not impl
            elif mode == "已实现":
                show = impl
            else:
                show = True
            if src != "全部来源" and rec.get('source') != src:
                show = False
            self._prot_table.setRowHidden(idx, not show)

    def _apply_code_filter(self):
        """码表页按来源分类显隐行(行号=码表 index, 勾选映射依赖不变)。"""
        if not hasattr(self, "_code_filter_source"):
            return
        src = self._code_filter_source.currentText()
        for idx in range(self._code_table.rowCount()):
            rec = jp.fault_record_by_index(idx)
            show = (src == "全部来源") or (
                rec is not None and rec.get('source') == src)
            self._code_table.setRowHidden(idx, not show)

    def _populate_protect_table(self):
        """灌注 109 码总览表(行号=码表 index, 勾选列配置逐故障使能)。"""
        self._prot_table.setRowCount(0)
        for rec in jp.load_fault_csv():
            row = self._prot_table.rowCount()
            self._prot_table.insertRow(row)
            self._prot_table.setRowHeight(row, 24)

            # 逐故障使能勾选: 勾选仅改本地待下发掩码, 由"下发修改"按钮统一下发
            chk = QCheckBox()
            chk.setStyleSheet("QCheckBox::indicator { width: 18px; height: 18px; }")
            if rec['stage'] == '0':
                chk.setEnabled(False)
                chk.setToolTip("码表预留(未实现), 固件不触发")
            else:
                chk.setToolTip("勾选=使能该故障检测, 取消=禁用\n修改后请点击\"下发修改\"下发(RAM 生效), 再\"固化\"保持")
            chk.toggled.connect(
                lambda checked, idx=rec['index']: self._on_code_enable_toggled(idx, checked))
            self._prot_table.setCellWidget(row, PC_ENABLE, chk)

            stage_txt = "已实现" if rec['stage'] != '0' else "未实现"
            items = {
                PC_CODE: (rec['code'], True),
                PC_NAME: (rec['name'], False),
                PC_LEVEL: (rec['level'], False),
                PC_ACTION: (rec['action'], False),
                PC_STAGE: (stage_txt, False),
            }
            for col, (text, is_mono) in items.items():
                it = QTableWidgetItem(text)
                it.setTextAlignment(
                    Qt.AlignmentFlag.AlignVCenter |
                    (Qt.AlignmentFlag.AlignCenter
                     if col in (PC_CODE, PC_LEVEL, PC_STAGE)
                     else Qt.AlignmentFlag.AlignLeft))
                if is_mono:
                    it.setFont(QFont("Consolas"))
                self._prot_table.setItem(row, col, it)
            self._set_protect_row_state(rec['index'])
        self._update_protect_chips()

    def _set_protect_row_state(self, index: int):
        """刷新总览表单行: 勾选状态 + 状态列文本/配色 + 已修改行高亮。"""
        if index < 0 or index >= self._prot_table.rowCount():
            return
        rec = jp.fault_record_by_index(index)
        if rec is None:
            return
        lo_pid = self._fault_level_pid(rec)
        pending = self._pending_masks.get(lo_pid) if lo_pid is not None else None
        # 按位判断"已修改": 仅高亮该级内待下发值与设备值有差异的故障位行
        if pending is None:
            modified = False
        else:
            m_dev = self._level_mask64(lo_pid)
            modified = m_dev is None or bool((pending ^ m_dev) & (1 << rec['level_bit']))
        enabled = self._fault_level_enabled(rec)
        impl = rec['stage'] != '0'
        if not impl:
            text, key = "未实现", 'muted'
            tip = "码表预留(未实现), 固件不触发"
        else:
            if modified:
                text, key = "已修改", 'warn'
                tip = "勾选待下发: 点击\"下发修改\"写入设备(RAM 生效)"
            elif enabled is False:
                text, key = "已禁用", 'warn'
                tip = "掩码对应位=0, 该故障检测已禁用"
            else:
                text, key = "生效中" if enabled is True else "状态未知", \
                    'accent' if enabled is True else 'muted'
                tip = "掩码对应位=1, 检测已开启" if enabled is True else "尚未读取该故障级别掩码"
            gate = self._protect_gate_note(rec)
            if gate:
                tip += f"\n{gate}"
        # 勾选框同步(反映合并视图的使能状态)
        chk = self._prot_table.cellWidget(index, PC_ENABLE)
        if isinstance(chk, QCheckBox):
            chk.blockSignals(True)
            if impl and enabled is not None:
                chk.setChecked(enabled)
            chk.setToolTip(tip if impl else "预留码, 固件不触发")
            chk.blockSignals(False)
        status_item = QTableWidgetItem(text)
        status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        status_item.setForeground(QBrush(theme.c(key)))
        status_item.setToolTip(tip)
        self._prot_table.setItem(index, PC_STATUS, status_item)
        # 已修改行整行高亮; 未实现行整行弱化
        hl = QColor(theme.c('warn'))
        hl.setAlpha(46)
        for col in (PC_CODE, PC_NAME, PC_LEVEL, PC_ACTION, PC_STAGE):
            it = self._prot_table.item(index, col)
            if it is None:
                continue
            if not impl:
                it.setForeground(QBrush(theme.c('muted')))
                it.setBackground(QBrush(Qt.GlobalColor.transparent))
            elif modified:
                it.setForeground(QBrush(theme.c('warn')))
                it.setBackground(QBrush(hl))
            else:
                it.setBackground(QBrush(Qt.GlobalColor.transparent))
                if col == PC_LEVEL:
                    k = _LEVEL_THEME_KEY.get(rec['level'])
                    it.setForeground(QBrush(theme.c(k)) if k else QBrush(theme.c('text')))
                else:
                    it.setForeground(QBrush(theme.c('text')))

    def _protect_gate_note(self, rec: dict) -> str:
        """受 PID128 保护总使能门控的检测项: 总使能=0 时返回提示(否则空串)。"""
        if self._protect_mask is None:
            return ""
        for _bit, code, name, _desc in _PROTECT_ITEMS:
            if rec['code_int'] == code and not self._protect_mask:
                return f"注意: {name} 保护总使能已关闭, 实际不检测"
        return ""

    def _update_protect_chips(self):
        """重算图例计数(口径与 _update_heatmap 一致: 活动故障优先红色)并应用筛选。
        使能掩码/活动故障/PID128 更新后调用。"""
        buckets = {"gray": [], "blue": [], "green": [], "red": []}
        impl_bits = {}   # 级别 lo PID -> 实现项位掩码(64bit)
        for rec in jp.load_fault_csv():
            if rec['stage'] == '0':
                buckets['gray'].append(rec)
                continue
            pid = self._fault_level_pid(rec)
            if pid is not None:
                impl_bits[pid] = impl_bits.get(pid, 0) | (1 << rec['level_bit'])
            if rec['code_int'] in self._active_codes:
                buckets['red'].append(rec)
            elif self._fault_level_enabled(rec) is False:
                buckets['blue'].append(rec)
            else:
                buckets['green'].append(rec)
        tips = {"gray": "未实现", "blue": "已禁用", "green": "使能中", "red": "报错活动"}
        for key, recs in buckets.items():
            lbl = self._legend_counts[key]
            lbl.setText(f"{tips[key]} {len(recs)}")
            names = [f"0x{r['code_int']:04X} {r['name']}" for r in recs[:15]]
            body = "; ".join(names)
            if len(recs) > 15:
                body += f"; …等 {len(recs)} 项"
            lbl.setToolTip(body or "无")
        # 待下发格数: 各级 (待下发 ^ 设备) 与实现位掩码求交计数; 设备未读按待下发实现位计数
        n_pend = 0
        for _name, lo, _hi in _FAULT_LEVEL_PARAMS:
            pend = self._pending_masks.get(lo)
            if pend is None:
                continue
            m_dev = self._level_mask64(lo)
            impl64 = impl_bits.get(lo, 0)
            diff = pend if m_dev is None else (pend ^ m_dev)
            n_pend += bin(diff & impl64).count("1")
        lbl = self._legend_counts["pend"]
        lbl.setText(f"待下发 {n_pend}")
        lbl.setToolTip("已修改待下发的使能位数; 点\"下发修改\"写入设备 RAM")
        self._apply_protect_filter()

    def _on_sub_tab_changed(self, idx: int):
        """子页切换: 进入保护总览时读取保护总使能(PID 128)。"""
        if self._tabs.widget(idx) is self._protect_tab and self._link_active:
            self.protect_mask_read_requested.emit()

    def on_protect_mask_received(self, value: int):
        """0xE6 读 PID128 应答(主窗口按 param_id==128 过滤转发)。"""
        self._protect_mask = int(value) & 0xFFFFFFFF
        enabled = bool(self._protect_mask)
        self._set_protect_badge(enabled)
        self._append_recent(
            f"保护总使能(PID128): {'启用' if enabled else '禁用'}")
        for rec in jp.load_fault_csv():
            if self._protect_gate_note(rec):
                self._set_protect_row_state(rec['index'])
        self._update_protect_chips()
        self._update_protect_enabled()

    def _update_level_mask_label(self):
        """级别名标签固定短名, tooltip 附 全名/PID/掩码 hex/使能计数/待下发/禁用码清单。"""
        for name, lo, hi in _FAULT_LEVEL_PARAMS:
            lbl = self._level_name_labels.get(name)
            if lbl is None:
                continue
            short = _LEVEL_SHORT_NAME.get(name, name)
            m = self._level_mask64(lo)
            if m is None:
                lbl.setText("\n".join(short))
                lbl.setToolTip(f"{name}  PID{lo}~{hi}\n尚未读取")
                continue
            total, en, disabled = 0, 0, []
            for rec in jp.load_fault_csv():
                if self._fault_level_pid(rec) != lo:
                    continue
                total += 1
                if (m >> rec['level_bit']) & 1:
                    en += 1
                else:
                    disabled.append(f"0x{rec['code_int']:04X} {rec['name']}")
            lbl.setText("\n".join(short))
            tip = f"{name}  PID{lo}~{hi} = 0x{m:016X}  使能 {en}/{total}"
            pend = self._pending_masks.get(lo)
            if pend is not None:
                tip += f"\n待下发 0x{pend:016X}"
            if disabled:
                tip += "\n已禁用: " + "、".join(disabled)
            lbl.setToolTip(tip)

    def on_fault_enable_mask_received(self, param_id: int, value: int):
        """PID154~159 掩码读取应答(u32 半区)：更新设备 RAM 值并同步行勾选/高亮。"""
        pid = int(param_id)
        if pid not in self._fault_masks:
            return
        self._fault_masks[pid] = int(value) & 0xFFFFFFFF
        # 设备合成值与待下发掩码一致(下发读回闭环) → 清除该级待下发标记
        for _name, lo, _hi in _FAULT_LEVEL_PARAMS:
            if pid in (lo, lo + 1):
                m = self._level_mask64(lo)
                if m is not None and self._pending_masks.get(lo) == m:
                    self._pending_masks[lo] = None
                break
        self._update_level_mask_label()
        self._append_recent(f"故障级别掩码读取 PID{pid}: 0x{self._fault_masks[pid]:08X}")
        self._refresh_fault_level_ui()
        self._update_protect_enabled()

    def _refresh_fault_level_ui(self):
        for rec in jp.load_fault_csv():
            self._set_protect_row_state(rec['index'])
        self._update_heatmap()
        self._update_protect_chips()

    def _refresh_active_ui(self):
        """活动故障码集合变化后刷新: 热力图红格 + 图例报错活动计数 + 状态栏摘要信号。"""
        self._update_heatmap()
        self._update_protect_chips()
        names = []
        for code in list(self._active_codes)[:3]:
            rec = jp.fault_code_lookup(code)
            names.append(rec['name'] if rec else f"0x{code:04X}")
        self.active_faults_changed.emit(names)

    def _on_enable_all(self):
        """全部故障级别使能：三级待下发掩码置全 1, 行高亮, 由\"下发修改\"统一下发。"""
        for _name, lo_pid, _hi in _FAULT_LEVEL_PARAMS:
            self._pending_masks[lo_pid] = 0xFFFFFFFFFFFFFFFF
        self._refresh_fault_level_ui()
        self._append_recent("全部故障级别使能(待下发) → 点击\"下发修改\"生效")

    def _on_save_now(self):
        """手动固化: 0xEA 把当前 RAM 参数写入存储(重启保持)。
        固化完成后延时重新加载掩码, 行状态/高亮才动态刷新。"""
        if not self._link_active:
            return
        self.fault_save_requested.emit()
        self._append_recent("手动固化(0xEA)已发送, 稍后自动重新加载掩码状态")
        QTimer.singleShot(800, self._reload_after_save)

    def _reload_after_save(self):
        """固化后重新加载: 读回设备掩码, 与待下发一致的级别清除高亮并刷新勾选。"""
        if not self._link_active:
            return
        self.fault_enable_masks_read_requested.emit()
        self.protect_mask_read_requested.emit()

    def _on_read_all_status(self):
        """读取全部状态：PID128 保护总使能及 PID154~159 故障级别掩码。"""
        if not self._link_active:
            return
        self.fault_enable_masks_read_requested.emit()
        self.protect_mask_read_requested.emit()

    def _update_protect_enabled(self):
        """连接状态/读取状态联动：总使能按钮连接后可操作。"""
        known = self._protect_mask is not None
        self._btn_protect_on.setEnabled(self._link_active and known and not self._protect_mask)
        self._btn_protect_off.setEnabled(self._link_active and known and bool(self._protect_mask))
        self._btn_enable_all.setEnabled(self._link_active)
        self._btn_apply_masks.setEnabled(self._link_active)
        self._btn_save_now.setEnabled(self._link_active)
        self._btn_read_protect.setEnabled(self._link_active)

    # ---------- 虚拟电机 Fault 位表 ----------
    def _build_fault_bit_tab(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        info = QLabel(
            "共 12 个 Fault 位标志(bit 0~10, 15), 来自 transport/virtual_engine/twin_fsm.py 的 Fault 类。"
            "FaultDetector 在每个 FOC 周期(100μs)实时检测; bit 5/8/9 由状态机迁移或预留; "
            "bit 15 为演示用注入。位值与 CSV 故障码关联。")
        info.setObjectName("bit_hint")
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{theme.hex('muted')}; font-size:12px; border:none;")
        v.addWidget(info)

        self._bit_table = QTableWidget(0, 6)
        self._bit_table.setHorizontalHeaderLabels(
            ["Bit", "位值", "Fault 名称", "检测方式", "触发条件", "对应 CSV 故障码"])
        self._bit_table.setAlternatingRowColors(True)
        self._bit_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._bit_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._bit_table.setWordWrap(True)
        self._bit_table.verticalHeader().setVisible(False)
        self._bit_table.verticalHeader().setDefaultSectionSize(24)
        self._bit_table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        hdr = self._bit_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self._bit_table, 1)
        return w

    def _populate_fault_bit_table(self):
        self._bit_table.setRowCount(0)
        for bit, value, name, detect, condition, csv_codes in jp.FAULT_BIT_INFO:
            row = self._bit_table.rowCount()
            self._bit_table.insertRow(row)
            self._bit_table.setRowHeight(row, 28)

            csv_desc_parts = []
            for c in csv_codes:
                rec = jp.fault_code_lookup(c)
                if rec:
                    csv_desc_parts.append(f"0x{c:04X} {rec['name']}")
                else:
                    csv_desc_parts.append(f"0x{c:04X}")
            csv_text = "\n".join(csv_desc_parts) if csv_desc_parts else "—"

            items = [
                (0, str(bit), True),
                (1, f"0x{value:04X}", True),
                (2, name, False),
                (3, detect, False),
                (4, condition, False),
                (5, csv_text, False),
            ]
            for col, text, is_mono in items:
                it = QTableWidgetItem(text)
                it.setTextAlignment(
                    Qt.AlignmentFlag.AlignVCenter |
                    (Qt.AlignmentFlag.AlignCenter if col in (0, 1) else Qt.AlignmentFlag.AlignLeft))
                if is_mono:
                    f = QFont("Consolas")
                    f.setBold(True)
                    it.setFont(f)
                self._bit_table.setItem(row, col, it)

            if '未实现' in detect or '预留' in condition:
                for col in range(6):
                    it = self._bit_table.item(row, col)
                    if it:
                        it.setForeground(QBrush(theme.c("muted")))

        self._bit_table.setColumnWidth(0, 50)
        self._bit_table.setColumnWidth(1, 80)
        self._bit_table.setColumnWidth(2, 140)
        self._bit_table.setColumnWidth(3, 200)
        self._bit_table.resizeRowsToContents()

    # ---------- 协议错误码表 ----------
    def _build_err_tab(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        info = QLabel(
            "协议错误码 JmErr(0x00~0x16), 来自 jmproto/cmd_def.py, "
            "作为 NACK 应答返回。报错时软件会展示对应中文描述而非仅错误码。")
        info.setObjectName("err_hint")
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{theme.hex('muted')}; font-size:12px; border:none;")
        v.addWidget(info)

        self._err_table = QTableWidget(0, 3)
        self._err_table.setHorizontalHeaderLabels(["错误码", "枚举名", "中文描述"])
        self._err_table.setAlternatingRowColors(True)
        self._err_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._err_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._err_table.setWordWrap(True)
        self._err_table.verticalHeader().setVisible(False)
        self._err_table.verticalHeader().setDefaultSectionSize(24)
        self._err_table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        hdr = self._err_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(COL_ERRCN, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self._err_table, 1)
        return w

    def _populate_err_table(self):
        self._err_table.setRowCount(0)
        from jmproto.cmd_def import JmErr
        for e in JmErr:
            row = self._err_table.rowCount()
            self._err_table.insertRow(row)
            self._err_table.setRowHeight(row, 26)

            code_item = QTableWidgetItem(f"0x{int(e):02X}")
            name_item = QTableWidgetItem(e.name)
            cn_item = QTableWidgetItem(jp.err_name_cn_short(int(e)))

            f = QFont("Consolas")
            f.setBold(True)
            code_item.setFont(f)
            code_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            name_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            if e == JmErr.OK:
                color = theme.c("accent")
                code_item.setForeground(QBrush(color))
                name_item.setForeground(QBrush(color))
            else:
                code_item.setForeground(QBrush(theme.c("value_hot")))
                cn_item.setForeground(QBrush(theme.c("text")))

            self._err_table.setItem(row, COL_ERR, code_item)
            self._err_table.setItem(row, COL_ERRNAME, name_item)
            self._err_table.setItem(row, COL_ERRCN, cn_item)

        self._err_table.setColumnWidth(COL_ERR, 80)
        self._err_table.setColumnWidth(COL_ERRNAME, 160)

    # ==================== 摘要卡刷新 ====================
    def on_summary_received(self, s: fd.FaultSummary):
        """0xAA 摘要应答(由主窗口转发 client.fault_summary_received)。"""
        self._summary = s
        # 标题 + 卡片边框随故障态变化
        if s.active_cnt > 0:
            self._lbl_title.setText(f"● 设备故障状态: 故障 ({s.active_cnt} 项)")
            self._lbl_title.setStyleSheet(
                f"color:{theme.hex('danger')}; font-size:13px; font-weight:bold; border:none;")
            self._status_card.setStyleSheet(f"""
                QFrame {{
                    background: {theme.hex('card_bottom')};
                    border: 1px solid {theme.hex('danger')};
                    border-radius: 4px;
                }}
            """)
        else:
            self._lbl_title.setText("● 设备故障状态: 正常")
            self._lbl_title.setStyleSheet(
                f"color:{theme.hex('accent')}; font-size:13px; font-weight:bold; border:none;")
            self._status_card.setStyleSheet(f"""
                QFrame {{
                    background: {theme.hex('card_bottom')};
                    border: 1px solid {theme.hex('accent')};
                    border-radius: 4px;
                }}
            """)
        # 最高故障码 + 中文名
        if s.top_code:
            rec = jp.fault_code_lookup(s.top_code)
            name = rec['name'] if rec else f"未知码(0x{s.top_code:04X})"
            self._lbl_top.setText(f"最高故障: 0x{s.top_code:04X} {name}")
            self._lbl_top.setStyleSheet(
                f"color:{theme.hex('danger')}; font-size:13px; font-weight:bold; border:none;")
        else:
            self._lbl_top.setText("最高故障: 无")
            self._lbl_top.setStyleSheet(
                f"color:{theme.hex('accent')}; font-size:13px; border:none;")
        self._lbl_active.setText(f"活动数: {s.active_cnt}")
        # derate_pct 为剩余功率百分比(固件 derate×100), 100=不降额
        self._lbl_derate.setText(f"降功率: {s.derate_pct}%"
                                 if s.derate_pct < 100 else "降功率: 无")
        self._lbl_hist.setText(f"历史: {s.hist_cnt} 条")

        # 三级状态灯
        lamp_map = {'故障级': fd.LEVEL_CRITICAL_BIT,
                    '异常级': fd.LEVEL_EXCEPTION_BIT,
                    '警告级': fd.LEVEL_WARNING_BIT}
        for level_cn, bit in lamp_map.items():
            lamp = self._lamps[level_cn]
            active = bool(s.level_active & bit)
            color = theme.hex(_LEVEL_THEME_KEY[level_cn]) if active else theme.hex('muted')
            lamp.setStyleSheet(
                f"color:{color}; font-size:12px; "
                f"font-weight:{'bold' if active else 'normal'}; border:none;")

        # 活动故障表行数对齐摘要, 详情由 0xAB 应答逐行回填;
        # 活动码集合防闪烁重建: active_cnt>0 时暂存新集合, 拉取完成才替换生效(旧红格保持)
        if s.active_cnt > 0:
            self._active_codes_new = set()
        else:
            self._active_codes = set()
            self._active_codes_new = None
            self._active_wall.clear()
            self._refresh_active_ui()
        self._active_table.setRowCount(s.active_cnt)
        if s.active_cnt > 0 and not self._pull_active and not self._pull_queue:
            self._queue_pulls([(fd.TYPE_ACTIVE, i) for i in range(s.active_cnt)])

        # 挂起的历史拉取(加载历史时若尚无摘要, 先取摘要再拉)
        if self._hist_load_pending:
            self._hist_load_pending = False
            if s.hist_cnt > 0:
                self._start_history_pull(s.hist_cnt)

    def _refresh_summary(self):
        """无新摘要时按缓存刷新(重连/主题切换后)。"""
        if self._summary is not None:
            self.on_summary_received(self._summary)

    # ==================== 0xAB 详情拉取队列 ====================
    def _finalize_active_rebuild(self):
        """活动码集合重建收尾: 暂存集合转正并刷新红格/图例(防自动刷新周期红格闪烁)。"""
        if getattr(self, "_active_codes_new", None) is not None:
            self._active_codes = self._active_codes_new
            self._active_codes_new = None
            # 清掉已消失故障的墙钟记录(复现时重新计时)
            self._active_wall = {c: v for c, v in self._active_wall.items()
                                 if c in self._active_codes}
            self._refresh_active_ui()

    def _queue_pulls(self, items):
        self._pull_queue.extend(items)
        self._pump_pulls()

    def _pump_pulls(self):
        if self._pull_active or not self._pull_queue:
            if not self._pull_queue:
                self._pull_timer.stop()
                self._finalize_active_rebuild()
            return
        if not self._link_active:
            self._pull_queue.clear()
            return
        ftype, idx = self._pull_queue.popleft()
        self._pull_active = True
        self._pull_timer.start()
        self.fault_detail_requested.emit(ftype, idx)

    def _on_pull_timeout(self):
        """单请求超时: 放弃剩余队列, 防止在途标志卡死; 已收到部分转正防红格卡旧值。"""
        self._pull_active = False
        self._pull_queue.clear()
        self._finalize_active_rebuild()

    def on_detail_received(self, ftype: int, idx: int, rec: fd.FaultRecord):
        """0xAB 详情应答(由主窗口转发 client.fault_detail_received)。"""
        self._pull_active = False
        self._pull_timer.stop()
        if ftype == fd.TYPE_ACTIVE:
            self._fill_active_row(idx, rec)
        elif ftype == fd.TYPE_HISTORY:
            self._append_history_row(rec)
        self._pump_pulls()

    def _fill_active_row(self, idx: int, rec: fd.FaultRecord):
        table = self._active_table
        if idx < 0 or idx >= table.rowCount():
            return
        self._active_codes.add(rec.code)   # 供使能位仪表盘红格判定
        if self._active_codes_new is not None:
            # 重建中: 码进暂存集合, 由 _finalize_active_rebuild 统一替换生效
            self._active_codes_new.add(rec.code)
        else:
            self._refresh_active_ui()
        # 墙钟时间(上位机会话内观察): 首次见到该码活动记 first, 之后每拍刷新 last
        now_str = datetime.now().strftime("%m-%d %H:%M:%S")
        if rec.code in self._active_wall:
            self._active_wall[rec.code][1] = now_str
        else:
            self._active_wall[rec.code] = [now_str, now_str]
        first_dt, last_dt = self._active_wall[rec.code]
        rec_info = jp.fault_code_lookup(rec.code)
        name = rec_info['name'] if rec_info else "—"
        level = rec_info['level'] if rec_info else ""
        cause = rec_info['condition'] if rec_info else "—"
        status_color = theme.c('danger') if rec.status == fd.STATUS_ACTIVE else theme.c('muted')
        self._fill_record_row(table, idx, rec, rec_info, name, level,
                              [f"0x{rec.code:04X}", name, cause, level,
                               fd.STATUS_CN.get(rec.status, str(rec.status)),
                               str(rec.count), str(rec.first_ms), str(rec.last_ms),
                               first_dt, last_dt, f"{rec.value:.4g}"],
                              status_color=status_color,
                              center_cols={AC_CODE, AC_LEVEL, AC_STATUS})

    def _append_history_row(self, rec: fd.FaultRecord):
        table = self._hist_table
        if table.rowCount() >= _MAX_HIST_FETCH:
            return
        row = table.rowCount()
        table.insertRow(row)
        table.setRowHeight(row, 24)
        rec_info = jp.fault_code_lookup(rec.code)
        name = rec_info['name'] if rec_info else "—"
        level = rec_info['level'] if rec_info else ""
        self._fill_record_row(table, row, rec, rec_info, name, level,
                              [f"0x{rec.code:04X}", name, str(rec.count),
                               str(rec.first_ms), str(rec.last_ms), f"{rec.value:.4g}"])

    def _fill_record_row(self, table, row, rec, rec_info, name, level,
                         texts, status_color=None, center_cols=None):
        mono_cols = (0,)
        if center_cols is None:
            center_cols = {0, 2} if len(texts) > 6 else {0}
        for col, text in enumerate(texts):
            it = QTableWidgetItem(text)
            it.setTextAlignment(
                Qt.AlignmentFlag.AlignVCenter |
                (Qt.AlignmentFlag.AlignCenter if col in center_cols
                 else Qt.AlignmentFlag.AlignLeft))
            if col in mono_cols:
                f = QFont("Consolas")
                f.setBold(True)
                it.setFont(f)
            table.setItem(row, col, it)
        # 级别/码着色: 活动表按级别色, 名称列保持文本色
        key = _LEVEL_THEME_KEY.get(level)
        if key:
            level_col = AC_LEVEL if table is self._active_table else HC_CODE
            # 历史表无级别列, 用码列着色
            it = table.item(row, level_col)
            if it:
                it.setForeground(QBrush(theme.c(key)))
        if status_color is not None:
            status_col = AC_STATUS
            it = table.item(row, status_col)
            if it:
                it.setForeground(QBrush(status_color))

    def _start_history_pull(self, hist_cnt: int):
        self._hist_table.setRowCount(0)
        cnt = min(int(hist_cnt), _MAX_HIST_FETCH)
        if cnt > 0:
            self._queue_pulls([(fd.TYPE_HISTORY, i) for i in range(cnt)])

    def _on_load_history(self):
        """加载历史: 有摘要直接拉, 无摘要先取摘要(应答后自动续拉)。"""
        if self._summary is None:
            self._hist_load_pending = True
            self._append_recent("加载历史: 先读取摘要获取条数")
            self.fault_summary_requested.emit()
            return
        if self._summary.hist_cnt <= 0:
            self._hist_table.setRowCount(0)
            self._append_recent("历史记录为空")
            return
        self._start_history_pull(self._summary.hist_cnt)

    # ==================== 虚拟电机故障屏蔽控制 ====================
    def _build_disable_checks(self):
        """构建 12 个故障位的屏蔽勾选框 (4列 x 3行)。"""
        grid = self._disable_container.layout()
        while grid.count():
            item = grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._disable_checks.clear()

        for idx, (bit, value, name, detect, condition, _) in enumerate(jp.FAULT_BIT_INFO):
            chk = QCheckBox(f"bit{bit} {name}")
            chk.setToolTip(f"屏蔽 {name}\n位值: 0x{value:04X}\n触发条件: {condition}")
            chk.setStyleSheet(f"color:{theme.hex('text')}; font-size:11px; border:none;")
            chk.toggled.connect(lambda checked, b=bit: self._on_disable_toggled(b, checked))
            row = idx // 4
            col = idx % 4
            grid.addWidget(chk, row, col)
            self._disable_checks[bit] = chk

        self._set_disable_enabled(False)   # 默认禁用, 连接虚拟引擎后启用

    def _set_disable_enabled(self, enabled: bool):
        for chk in self._disable_checks.values():
            chk.setEnabled(enabled)
        self._disable_group.setEnabled(enabled)

    def set_engine(self, engine):
        """主窗口连接虚拟引擎时调用, 传入 engine 句柄。"""
        self._engine = engine
        self._set_disable_enabled(engine is not None)
        if engine is not None:
            try:
                mask = engine.get_fault_disable_mask()
                self._sync_mask_to_ui(mask)
            except Exception:
                pass

    def set_virtual_mode(self, on: bool):
        """虚拟电机模式开关联动: 控制虚拟电机专属子表与屏蔽区显隐。"""
        self._tabs.setTabVisible(self._tabs.indexOf(self._fault_bit_tab), on)
        self._disable_group.setVisible(on)
        if not on and self._tabs.currentWidget() is self._fault_bit_tab:
            self._tabs.setCurrentIndex(0)

    def _sync_mask_to_ui(self, mask: int):
        self._disable_mask = int(mask) & 0xFFFF
        for bit, value, name, _, _, _ in jp.FAULT_BIT_INFO:
            chk = self._disable_checks.get(bit)
            if chk is not None:
                chk.blockSignals(True)
                chk.setChecked(bool(self._disable_mask & value))
                chk.blockSignals(False)
        self._lbl_disable_mask.setText(f"屏蔽掩码: 0x{self._disable_mask:04X}")

    def _on_disable_toggled(self, bit: int, checked: bool):
        value = 0
        for b, v, _, _, _, _ in jp.FAULT_BIT_INFO:
            if b == bit:
                value = v
                break
        if checked:
            self._disable_mask |= value
        else:
            self._disable_mask &= ~value
        self._disable_mask &= 0xFFFF
        self._lbl_disable_mask.setText(f"屏蔽掩码: 0x{self._disable_mask:04X}")
        self._apply_disable_mask()

    def _on_clear_disable(self):
        for chk in self._disable_checks.values():
            chk.blockSignals(True)
            chk.setChecked(False)
            chk.blockSignals(False)
        self._disable_mask = 0
        self._lbl_disable_mask.setText("屏蔽掩码: 0x0000")
        self._apply_disable_mask()

    def _apply_disable_mask(self):
        if self._engine is None:
            return
        try:
            self._engine.set_fault_disable_mask(self._disable_mask)
        except Exception:
            pass

    # ==================== 配置持久化 ====================
    def get_opts(self) -> dict:
        opts = {"disable_mask": 0, "current_tab": 0, "auto_refresh": False}
        try:
            opts["disable_mask"] = int(self._disable_mask)
        except Exception:
            pass
        try:
            opts["current_tab"] = int(self._tabs.currentIndex())
        except Exception:
            pass
        try:
            opts["auto_refresh"] = bool(self._chk_auto.isChecked())
        except Exception:
            pass
        return opts

    def set_opts(self, opts: dict):
        if not isinstance(opts, dict):
            return
        try:
            mask = int(opts.get("disable_mask", 0)) & 0xFFFF
            self._sync_mask_to_ui(mask)
            self._apply_disable_mask()
        except Exception:
            pass
        try:
            self._chk_auto.setChecked(bool(opts.get("auto_refresh", False)))
        except Exception:
            pass
        try:
            idx = int(opts.get("current_tab", 0))
            if 0 <= idx < self._tabs.count():
                self._tabs.setCurrentIndex(idx)
        except Exception:
            pass

    # ==================== 遥测兼容掩码 / 事件记录 ====================
    def update_fault_mask(self, mask: int):
        """主窗口在每个反馈周期调用: 遥测 compat_mask 摘要(压缩位图, 仅作灯)。"""
        mask = int(mask) if mask else 0
        if mask == self._current_mask:
            return
        prev = self._current_mask
        self._current_mask = mask
        if mask != 0 and mask != prev:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            desc = jp.fault_mask_to_str(mask)
            self._history.appendleft(f"[{ts}] 0x{mask:08X} {desc}")
            self._refresh_recent()
        self._refresh_compat()

    def show_active_fault(self, mask: int, source: str = ""):
        """报错时由主窗口调用: 记录 compat 摘要与事件。"""
        mask = int(mask) if mask else 0
        self._current_mask = mask
        if mask != 0:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            desc = jp.fault_mask_to_str(mask)
            self._history.appendleft(f"[{ts}] {source or 'FAULT'} 0x{mask:08X} {desc}")
            self._refresh_recent()
        self._refresh_compat()

    def show_nack_error(self, cmd: int, err: int):
        """NACK 报错: 记录事件; 0xAB 拉取被拒(如越界竞态)时推进队列防卡死。"""
        try:
            cmd_str = jp.cmd_name(int(cmd))
        except Exception:
            cmd_str = f"0x{int(cmd):02X}"
        cn = jp.err_name_cn(int(err))
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._history.appendleft(f"[{ts}] NACK {cmd_str} err={cn}")
        self._refresh_recent()
        if int(cmd) == int(jp.JmCmd.FAULT_DETAIL) and self._pull_active:
            self._pull_active = False
            self._pull_timer.stop()
            self._pump_pulls()

    def _append_recent(self, text: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._history.appendleft(f"[{ts}] {text}")
        self._refresh_recent()

    def _refresh_recent(self):
        if not self._history:
            self._lbl_recent.setText("最近: —")
            return
        recent = list(self._history)[:3]
        self._lbl_recent.setText("最近: " + "  |  ".join(recent))

    def _refresh_compat(self):
        mask = self._current_mask
        if mask == 0:
            # 遥测反馈帧 fault_mask 为 u16(旧协议格式), 新故障级压缩位在
            # bit16+ 不在其中; 有活动故障时不能宣称"无故障"
            s = self._summary
            if s is not None and s.active_cnt > 0:
                self._lbl_compat.setText(
                    "遥测兼容掩码: 0x0000 (活动故障不在旧协议低 13 位映射, 见活动列表)")
                self._lbl_compat.setStyleSheet(
                    f"color:{theme.hex('muted')}; font-size:11px; border:none;")
            else:
                self._lbl_compat.setText("遥测兼容掩码: 0x0000 无故障")
                self._lbl_compat.setStyleSheet(
                    f"color:{theme.hex('accent')}; font-size:11px; border:none;")
        else:
            self._lbl_compat.setText(
                f"遥测兼容掩码: 0x{mask:08X}  {jp.fault_mask_to_str(mask)}")
            self._lbl_compat.setStyleSheet(
                f"color:{theme.hex('danger')}; font-size:11px; border:none;")

    # ==================== 连接状态 / tab 可见性 ====================
    def set_link_active(self, active: bool):
        """连接状态变化(主窗口 _on_connected 调用)。断连停轮询、禁用操作。"""
        self._link_active = active
        if active:
            # 已连接且本面板可见时读取基础保护掩码(否则等切回本 Tab/子页时读)
            if self._tab_visible:
                self.protect_mask_read_requested.emit()
        else:
            self._auto_timer.stop()
            self._apply_timer.stop()
            self._apply_queue.clear()
            self._pull_timer.stop()
            self._pull_active = False
            self._pull_queue.clear()
            self._summary = None
            self._hist_load_pending = False
            self._active_wall.clear()
            self._active_table.setRowCount(0)
            self._hist_table.setRowCount(0)
            self._protect_mask = None
            self._active_codes.clear()
            self._active_codes_new = None
            self.active_faults_changed.emit([])   # 断连清状态栏故障摘要
            self._set_protect_badge(None)
            for pid in self._fault_masks:
                self._fault_masks[pid] = None
            for _n, lo, _hi in _FAULT_LEVEL_PARAMS:
                self._pending_masks[lo] = None
            self._update_level_mask_label()
            self._update_heatmap()
            self._lbl_title.setText("● 设备故障状态")
            self._lbl_title.setStyleSheet(
                f"color:{theme.hex('title')}; font-size:13px; font-weight:bold; border:none;")
            self._status_card.setStyleSheet(f"""
                QFrame {{
                    background: {theme.hex('card_bottom')};
                    border: 1px solid {theme.hex('border')};
                    border-radius: 4px;
                }}
            """)
            self._lbl_top.setText("最高故障: —")
            self._lbl_top.setStyleSheet(
                f"color:{theme.hex('text')}; font-size:13px; border:none;")
            self._lbl_active.setText("活动数: —")
            self._lbl_derate.setText("降功率: —")
            self._lbl_hist.setText("历史: — 条")
            for lamp in self._lamps.values():
                lamp.setStyleSheet(
                    f"color:{theme.hex('muted')}; font-size:12px; border:none;")
        self._update_toolbar_state()

    def _update_toolbar_state(self):
        for w in (self._btn_read, self._chk_auto, self._btn_clear_fault,
                  self._btn_clear_hist, self._btn_load_hist):
            w.setEnabled(self._link_active)
        self._update_protect_enabled()
        self._lbl_link.setText("已连接" if self._link_active else "未连接")
        self._lbl_link.setStyleSheet(
            f"color:{theme.hex('accent') if self._link_active else theme.hex('muted')};")

    def auto_read_if_connected(self):
        """切到本 Tab 时由主窗口调用: 已连接则读摘要及全部故障使能掩码。"""
        self._tab_visible = True
        if not self._link_active:
            return
        self.fault_summary_requested.emit()
        self.fault_enable_masks_read_requested.emit()
        self.protect_mask_read_requested.emit()
        if self._chk_auto.isChecked():
            self._auto_timer.start()

    def pause_auto_refresh(self):
        """切离本 Tab 时由主窗口调用: 停止自动刷新, 省带宽。"""
        self._tab_visible = False
        self._auto_timer.stop()

    def refresh_after_clear(self):
        """0xB0 清除成功 ACK 后由主窗口调用: 立即重读摘要刷新计数/历史。"""
        if self._link_active:
            self.fault_summary_requested.emit()

    def _on_read_summary(self):
        self.fault_summary_requested.emit()

    def _on_auto_toggled(self, checked: bool):
        if checked and self._link_active and self._tab_visible:
            self.fault_summary_requested.emit()
            self._auto_timer.start()
        else:
            self._auto_timer.stop()

    def _on_auto_tick(self):
        if not (self._link_active and self._tab_visible and self._chk_auto.isChecked()):
            self._auto_timer.stop()
            return
        self.fault_summary_requested.emit()

    def _on_reload_csv(self):
        """重新加载 CSV (用户更新 CSV 后)。"""
        jp.clear_fault_cache()
        self._populate_code_table()
        self._populate_protect_table()
        self._populate_fault_bit_table()
        self._refresh_compat()

    # ==================== 主题 ====================
    def apply_theme(self):
        """主题切换刷新。"""
        card_qss = self._CARD_QSS.format(bg=theme.hex('card_bottom'),
                                         bd=theme.hex('border'))
        self._status_card.setStyleSheet(card_qss)
        self._protect_card.setStyleSheet(card_qss)
        self._lbl_title.setStyleSheet(
            f"color:{theme.hex('title')}; font-size:13px; font-weight:bold; border:none;")
        self._lbl_recent.setStyleSheet(
            f"color:{theme.hex('text')}; font-family:Consolas,monospace; font-size:11px; border:none;")
        self._disable_group.setStyleSheet(f"""
            QGroupBox {{
                border: 1px solid {theme.hex('border')};
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 8px;
                font-size: 12px;
                color: {theme.hex('title')};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
            }}
        """)
        for chk in self._disable_checks.values():
            chk.setStyleSheet(f"color:{theme.hex('text')}; font-size:11px; border:none;")
        self._lbl_disable_mask.setStyleSheet(
            f"color:{theme.hex('muted')}; font-family:Consolas,monospace; font-size:11px; border:none;")
        if hasattr(self, "_protect_badge"):
            self._set_protect_badge(
                None if self._protect_mask is None else bool(self._protect_mask))
        # 重刷提示行与表格配色
        for name in ("diag_hint", "code_hint", "bit_hint", "err_hint", "ve_hint"):
            for lbl in self.findChildren(QLabel):
                if lbl.objectName() == name:
                    lbl.setStyleSheet(
                        f"color:{theme.hex('muted')}; font-size:11px; border:none;")
        self._populate_code_table()
        self._populate_protect_table()
        self._populate_fault_bit_table()
        self._populate_err_table()
        # 刷新故障级别掩码驱动的总览状态
        self._refresh_fault_level_ui()
        self._refresh_compat()
        if self._summary is not None:
            self._refresh_summary()
            self._active_table.setRowCount(self._summary.active_cnt)
