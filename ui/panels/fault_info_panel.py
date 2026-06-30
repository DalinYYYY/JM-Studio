"""故障信息面板

整合三套故障体系, 与电机参数/孪生参数同级:
  1. 设备故障码 (CSV 104 条, 9 大系统分组)
  2. 虚拟电机 Fault 位标志 (12 个位)
  3. 协议错误码 JmErr (11 个)

顶部状态栏显示当前实时故障解码结果; 三张子表分别展示完整故障定义。
软件报错时由主窗口调用 show_active_fault() 在状态栏加载完整描述。
"""
from collections import deque
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QBrush, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QSizePolicy, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

import jmproto as jp
from ui.theme import theme


# ---------- 设备故障码表列定义 ----------
COL_CODE = 0
COL_SOURCE = 1
COL_LEVEL = 2
COL_NAME = 3
COL_COND = 4
COL_ACTION = 5

# ---------- 虚拟电机 Fault 位表列定义 ----------
COL_BIT = 0
COL_BITVAL = 1
COL_BITNAME = 2
COL_DETECT = 3
COL_BITCOND = 4
COL_CSV = 5

# ---------- 协议错误码表列定义 ----------
COL_ERR = 0
COL_ERRNAME = 1
COL_ERRCN = 2


_LEVEL_COLOR = {
    '故障级': '#F44336',
    '异常级': '#FFB454',
    '警告级': '#FFEB3B',
}


class FaultInfoPanel(QGroupBox):
    """故障信息汇总面板 (顶级 tab)。

    提供:
      - update_fault_mask(mask): 实时刷新当前故障解码状态
      - show_active_fault(mask, source): 报错时在状态栏加载完整故障描述
      - show_nack_error(cmd, err): NACK 报错时显示中文错误信息
    """

    _MAX_HISTORY = 50

    def __init__(self, parent=None):
        super().__init__("", parent)
        self._panel_name = "故障信息"
        self._current_mask = 0
        self._history = deque(maxlen=self._MAX_HISTORY)
        self._engine = None              # 虚拟引擎句柄(由主窗口 set_engine 设置)
        self._disable_mask = 0           # 当前故障屏蔽掩码
        self._disable_checks = {}        # bit -> QCheckBox

        self._build()
        self._populate_csv_table()
        self._populate_fault_bit_table()
        self._populate_err_table()
        self._build_disable_checks()
        self._refresh_status()

    def panel_name(self) -> str:
        return self._panel_name

    # ==================== UI 构建 ====================
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # ----- 顶部: 当前故障状态卡 -----
        self._status_card = QFrame()
        self._status_card.setFrameShape(QFrame.Shape.StyledPanel)
        self._status_card.setStyleSheet(f"""
            QFrame {{
                background: {theme.hex('card_bottom')};
                border: 1px solid {theme.hex('border')};
                border-radius: 4px;
            }}
        """)
        sc_layout = QVBoxLayout(self._status_card)
        sc_layout.setContentsMargins(10, 8, 10, 8)
        sc_layout.setSpacing(4)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self._lbl_title = QLabel("● 当前故障状态")
        self._lbl_title.setStyleSheet(
            f"color:{theme.hex('title')}; font-size:13px; font-weight:bold; border:none;")
        self._lbl_mask = QLabel("Mask: 0x0000")
        self._lbl_mask.setStyleSheet(
            f"color:{theme.hex('muted')}; font-family:Consolas,monospace; "
            f"font-size:12px; border:none;")
        title_row.addWidget(self._lbl_title)
        title_row.addStretch()
        title_row.addWidget(self._lbl_mask)
        sc_layout.addLayout(title_row)

        self._lbl_desc = QLabel("无故障")
        self._lbl_desc.setWordWrap(True)
        self._lbl_desc.setStyleSheet(
            f"color:{theme.hex('ok_text')}; font-size:13px; border:none; padding:2px 0;")
        sc_layout.addWidget(self._lbl_desc)

        # 故障详情列表 (展开式)
        self._detail_table = QTableWidget(0, 4)
        self._detail_table.setHorizontalHeaderLabels(["Fault位", "触发条件", "对应故障", "处理方式"])
        self._detail_table.setAlternatingRowColors(True)
        self._detail_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._detail_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._detail_table.setWordWrap(True)
        self._detail_table.verticalHeader().setVisible(False)
        self._detail_table.verticalHeader().setDefaultSectionSize(24)
        self._detail_table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._detail_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._detail_table.setMaximumHeight(140)
        sc_layout.addWidget(self._detail_table)

        layout.addWidget(self._status_card)

        # ----- 故障历史 -----
        hist_row = QHBoxLayout()
        hist_row.setSpacing(6)
        lbl_hist = QLabel("最近故障:")
        lbl_hist.setStyleSheet(f"color:{theme.hex('muted')}; font-size:12px; border:none;")
        hist_row.addWidget(lbl_hist)
        self._lbl_history = QLabel("—")
        self._lbl_history.setStyleSheet(
            f"color:{theme.hex('text')}; font-family:Consolas,monospace; font-size:12px; border:none;")
        self._lbl_history.setWordWrap(True)
        hist_row.addWidget(self._lbl_history, 1)
        layout.addLayout(hist_row)

        # ----- 故障屏蔽区 (虚拟电机演示功能) -----
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
        info_lbl.setStyleSheet(f"color:{theme.hex('muted')}; font-size:11px; border:none;")
        dg_layout.addWidget(info_lbl)

        # 12 个勾选框: 4列 x 3行
        checks_grid = QGridLayout()
        checks_grid.setHorizontalSpacing(16)
        checks_grid.setVerticalSpacing(2)
        self._disable_container = QWidget()
        self._disable_container.setLayout(checks_grid)
        dg_layout.addWidget(self._disable_container)

        # 底部: 当前掩码 + 清除按钮
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

        layout.addWidget(self._disable_group)

        # ----- 子选项卡: 三张故障定义表 -----
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_csv_tab(), "设备故障码 (CSV)")
        # 保存虚拟电机 Fault 位子表 widget, 供虚拟电机模式开关控制显隐
        self._fault_bit_tab = self._build_fault_bit_tab()
        self._tabs.addTab(self._fault_bit_tab, "虚拟电机 Fault 位")
        self._tabs.addTab(self._build_err_tab(), "协议错误码 (JmErr)")
        layout.addWidget(self._tabs, 1)
        # 虚拟电机模式默认关闭: 初始隐藏"虚拟电机 Fault 位"子表与故障屏蔽区
        self._tabs.setTabVisible(self._tabs.indexOf(self._fault_bit_tab), False)
        self._disable_group.setVisible(False)

    # ---------- CSV 故障码表 ----------
    def _build_csv_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        info = QLabel(
            "共 104 条设备故障码, 来自 resources/故障码定义_故障信息表_表格.csv, "
            "覆盖 9 大系统(安全/电源/驱动器/电机本体/编码器/机械传动/抱闸/软件算法/通信/环境)。"
            "故障级别: 故障级(红)=立即停机; 异常级(橙)=降功率; 警告级(黄)=记录日志。")
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{theme.hex('muted')}; font-size:12px; border:none;")
        v.addWidget(info)

        self._csv_table = QTableWidget(0, 6)
        self._csv_table.setHorizontalHeaderLabels(
            ["故障码", "故障来源", "故障级别", "故障名称", "触发条件", "处理方式"])
        self._csv_table.setAlternatingRowColors(True)
        self._csv_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._csv_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._csv_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._csv_table.setWordWrap(True)
        self._csv_table.setShowGrid(True)
        self._csv_table.verticalHeader().setVisible(False)
        self._csv_table.verticalHeader().setDefaultSectionSize(24)
        self._csv_table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        hdr = self._csv_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_COND, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_ACTION, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self._csv_table, 1)

        # 操作行: 刷新/导出
        op_row = QHBoxLayout()
        op_row.addStretch()
        btn_refresh = QPushButton("重新加载")
        btn_refresh.setFixedHeight(24)
        btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_refresh.clicked.connect(self._on_reload_csv)
        op_row.addWidget(btn_refresh)
        v.addLayout(op_row)
        return w

    def _populate_csv_table(self):
        self._csv_table.setRowCount(0)
        records = jp.load_fault_csv()
        # 按来源分组展示
        groups = jp.fault_codes_by_source()
        # 按 CSV 文件顺序的来源去重
        ordered_sources = []
        seen = set()
        for r in records:
            if r['source'] not in seen:
                seen.add(r['source'])
                ordered_sources.append(r['source'])

        for src in ordered_sources:
            rows = groups.get(src, [])
            if not rows:
                continue
            # 分组分隔行
            self._append_csv_group_row(src, len(rows))
            for r in rows:
                self._append_csv_record_row(r)

        # 默认列宽
        self._csv_table.setColumnWidth(COL_CODE, 80)
        self._csv_table.setColumnWidth(COL_SOURCE, 130)
        self._csv_table.setColumnWidth(COL_LEVEL, 70)
        self._csv_table.resizeRowsToContents()

    def _append_csv_group_row(self, src: str, count: int):
        row = self._csv_table.rowCount()
        self._csv_table.insertRow(row)
        self._csv_table.setRowHeight(row, 26)

        item = QTableWidgetItem(f"▌ {src}  ({count} 条)")
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setForeground(QBrush(theme.c("title")))
        f = QFont(); f.setBold(True)
        item.setFont(f)
        item.setBackground(QBrush(theme.c("table_header")))
        item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self._csv_table.setItem(row, COL_CODE, item)
        self._csv_table.setSpan(row, COL_CODE, 1, 6)

    def _append_csv_record_row(self, r: dict):
        row = self._csv_table.rowCount()
        self._csv_table.insertRow(row)
        self._csv_table.setRowHeight(row, 24)

        code_item = QTableWidgetItem(r['code'])
        src_item = QTableWidgetItem(r['source'])
        lvl_item = QTableWidgetItem(r['level'])
        name_item = QTableWidgetItem(r['name'])
        cond_item = QTableWidgetItem(r['condition'])
        act_item = QTableWidgetItem(r['action'])

        # 级别颜色
        lvl_color = _LEVEL_COLOR.get(r['level'])
        if lvl_color:
            lvl_item.setForeground(QBrush(QColor(lvl_color)))
            f = QFont(); f.setBold(True)
            lvl_item.setFont(f)
        lvl_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        for col, it in enumerate([
            code_item, src_item, lvl_item, name_item, cond_item, act_item]):
            it.setTextAlignment(
                Qt.AlignmentFlag.AlignVCenter |
                (Qt.AlignmentFlag.AlignCenter if col in (COL_CODE, COL_LEVEL) else Qt.AlignmentFlag.AlignLeft))
            self._csv_table.setItem(row, col, it)

    # ---------- 虚拟电机 Fault 位表 ----------
    def _build_fault_bit_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        info = QLabel(
            "共 12 个 Fault 位标志(bit 0~10, 15), 来自 transport/virtual_engine/twin_fsm.py 的 Fault 类。"
            "FaultDetector 在每个 FOC 周期(100μs)实时检测; bit 5/8/9 由状态机迁移或预留; "
            "bit 15 为演示用注入。位值与 CSV 故障码关联。")
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
        hdr.setSectionResizeMode(COL_BITCOND, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_CSV, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self._bit_table, 1)
        return w

    def _populate_fault_bit_table(self):
        self._bit_table.setRowCount(0)
        for bit, value, name, detect, condition, csv_codes in jp.FAULT_BIT_INFO:
            row = self._bit_table.rowCount()
            self._bit_table.insertRow(row)
            self._bit_table.setRowHeight(row, 28)

            # CSV 故障码描述合并单元格文本
            csv_desc_parts = []
            for c in csv_codes:
                rec = jp.fault_code_lookup(c)
                if rec:
                    csv_desc_parts.append(f"0x{c:04X} {rec['name']}")
                else:
                    csv_desc_parts.append(f"0x{c:04X}")
            csv_text = "\n".join(csv_desc_parts) if csv_desc_parts else "—"

            items = [
                (COL_BIT, str(bit), True),
                (COL_BITVAL, f"0x{value:04X}", True),
                (COL_BITNAME, name, False),
                (COL_DETECT, detect, False),
                (COL_BITCOND, condition, False),
                (COL_CSV, csv_text, False),
            ]
            for col, text, is_mono in items:
                it = QTableWidgetItem(text)
                it.setTextAlignment(
                    Qt.AlignmentFlag.AlignVCenter |
                    (Qt.AlignmentFlag.AlignCenter if col in (COL_BIT, COL_BITVAL) else Qt.AlignmentFlag.AlignLeft))
                if is_mono:
                    f = QFont("Consolas"); f.setBold(True)
                    it.setFont(f)
                self._bit_table.setItem(row, col, it)

            # 未实现的检测用 muted 色
            if '未实现' in detect or '预留' in condition:
                for col in range(6):
                    it = self._bit_table.item(row, col)
                    if it:
                        it.setForeground(QBrush(theme.c("muted")))

        self._bit_table.setColumnWidth(COL_BIT, 50)
        self._bit_table.setColumnWidth(COL_BITVAL, 80)
        self._bit_table.setColumnWidth(COL_BITNAME, 140)
        self._bit_table.setColumnWidth(COL_DETECT, 200)
        self._bit_table.resizeRowsToContents()

    # ---------- 协议错误码表 ----------
    def _build_err_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        info = QLabel(
            "共 11 个协议错误码 JmErr(0x00~0x0A), 来自 jmproto/cmd_def.py, "
            "作为 NACK 应答返回。报错时软件会展示对应中文描述而非仅错误码。")
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

            f = QFont("Consolas"); f.setBold(True)
            code_item.setFont(f)
            code_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            name_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            # OK 绿色, 其他橙色/红色
            if e == JmErr.OK:
                code_item.setForeground(QBrush(QColor('#5FE6AC')))
                name_item.setForeground(QBrush(QColor('#5FE6AC')))
            else:
                code_item.setForeground(QBrush(QColor('#FFB454')))
                cn_item.setForeground(QBrush(theme.c("text")))

            self._err_table.setItem(row, COL_ERR, code_item)
            self._err_table.setItem(row, COL_ERRNAME, name_item)
            self._err_table.setItem(row, COL_ERRCN, cn_item)

        self._err_table.setColumnWidth(COL_ERR, 80)
        self._err_table.setColumnWidth(COL_ERRNAME, 160)

    # ==================== 故障屏蔽控制 ====================
    def _build_disable_checks(self):
        """构建 12 个故障位的屏蔽勾选框 (4列 x 3行)。"""
        grid = self._disable_container.layout()
        # 清空旧 widget
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
        """启用/禁用屏蔽控件区。"""
        for chk in self._disable_checks.values():
            chk.setEnabled(enabled)
        self._disable_group.setEnabled(enabled)

    def set_engine(self, engine):
        """主窗口连接虚拟引擎时调用, 传入 engine 句柄。"""
        self._engine = engine
        self._set_disable_enabled(engine is not None)
        # 同步当前 engine 的 disable_mask 到 UI
        if engine is not None:
            try:
                mask = engine.get_fault_disable_mask()
                self._sync_mask_to_ui(mask)
            except Exception:
                pass

    def set_virtual_mode(self, on: bool):
        """虚拟电机模式开关联动: 控制虚拟电机专属子表与屏蔽区显隐。

        与 set_engine() 正交:
        - set_engine 控制屏蔽区"可编辑性"(engine 句柄是否存在);
        - set_virtual_mode 控制"可见性"(是否处于虚拟电机模式)。
        两者同时生效, 关闭任一条件屏蔽区均不可用/不可见。
        """
        # "虚拟电机 Fault 位"子表显隐
        self._tabs.setTabVisible(self._tabs.indexOf(self._fault_bit_tab), on)
        # "故障屏蔽(虚拟电机演示)"区域显隐
        self._disable_group.setVisible(on)
        # 关闭模式时, 若当前正停在该子表, 切回首页(CSV 故障码)
        if not on and self._tabs.currentWidget() is self._fault_bit_tab:
            self._tabs.setCurrentIndex(0)

    def _sync_mask_to_ui(self, mask: int):
        """把 engine 的 disable_mask 同步到勾选框 UI。"""
        self._disable_mask = int(mask) & 0xFFFF
        for bit, value, name, _, _, _ in jp.FAULT_BIT_INFO:
            chk = self._disable_checks.get(bit)
            if chk is not None:
                chk.blockSignals(True)
                chk.setChecked(bool(self._disable_mask & value))
                chk.blockSignals(False)
        self._lbl_disable_mask.setText(f"屏蔽掩码: 0x{self._disable_mask:04X}")

    def _on_disable_toggled(self, bit: int, checked: bool):
        """某个故障位勾选状态变化。"""
        # 找到该 bit 对应的 value
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
        """清除所有屏蔽。"""
        for chk in self._disable_checks.values():
            chk.blockSignals(True)
            chk.setChecked(False)
            chk.blockSignals(False)
        self._disable_mask = 0
        self._lbl_disable_mask.setText("屏蔽掩码: 0x0000")
        self._apply_disable_mask()

    def _apply_disable_mask(self):
        """把当前 disable_mask 应用到 engine。"""
        if self._engine is None:
            return
        try:
            self._engine.set_fault_disable_mask(self._disable_mask)
        except Exception:
            pass

    # ==================== 配置持久化 ====================
    def get_opts(self) -> dict:
        """收集可持久化的 UI 配置: 故障屏蔽掩码 + 当前 tab 索引."""
        opts = {"disable_mask": 0, "current_tab": 0}
        try:
            opts["disable_mask"] = int(self._disable_mask)
        except Exception:
            pass
        try:
            opts["current_tab"] = int(self._tabs.currentIndex())
        except Exception:
            pass
        return opts

    def set_opts(self, opts: dict):
        """启动时套用配置 (容错). 仅恢复 UI 勾选状态;
        若已连接虚拟引擎, 会顺带把掩码应用到 engine."""
        if not isinstance(opts, dict):
            return
        try:
            mask = int(opts.get("disable_mask", 0)) & 0xFFFF
            self._sync_mask_to_ui(mask)
            self._apply_disable_mask()
        except Exception:
            pass
        try:
            idx = int(opts.get("current_tab", 0))
            if 0 <= idx < self._tabs.count():
                self._tabs.setCurrentIndex(idx)
        except Exception:
            pass

    # ==================== 实时故障状态 ====================
    def update_fault_mask(self, mask: int):
        """主窗口在每个反馈周期调用, 刷新当前故障状态。"""
        mask = int(mask) if mask else 0
        if mask == self._current_mask:
            return
        prev = self._current_mask
        self._current_mask = mask
        # 故障上升沿记录到历史
        if mask != 0 and mask != prev:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            desc = jp.fault_mask_to_str(mask)
            self._history.appendleft(f"[{ts}] 0x{mask:04X} {desc}")
            self._refresh_history_label()
        self._refresh_status()

    def show_active_fault(self, mask: int, source: str = ""):
        """报错时由主窗口调用: 加载完整故障描述到状态栏。"""
        mask = int(mask) if mask else 0
        self._current_mask = mask
        if mask != 0:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            desc = jp.fault_mask_to_str(mask)
            self._history.appendleft(f"[{ts}] {source or 'FAULT'} 0x{mask:04X} {desc}")
            self._refresh_history_label()
        self._refresh_status()

    def show_nack_error(self, cmd: int, err: int):
        """NACK 报错时记录到历史。"""
        try:
            cmd_str = jp.cmd_name(int(cmd))
        except Exception:
            cmd_str = f"0x{int(cmd):02X}"
        cn = jp.err_name_cn(int(err))
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._history.appendleft(f"[{ts}] NACK {cmd_str} err={cn}")
        self._refresh_history_label()

    def _refresh_history_label(self):
        if not self._history:
            self._lbl_history.setText("—")
            return
        # 只显示最近 3 条, 防止过长
        recent = list(self._history)[:3]
        self._lbl_history.setText("  |  ".join(recent))

    def _refresh_status(self):
        mask = self._current_mask
        self._lbl_mask.setText(f"Mask: 0x{mask:04X}")

        if mask == 0:
            self._lbl_title.setText("● 当前故障状态: 正常")
            self._lbl_title.setStyleSheet(
                f"color:{theme.hex('accent')}; font-size:13px; font-weight:bold; border:none;")
            self._lbl_desc.setText("无故障")
            self._lbl_desc.setStyleSheet(
                f"color:{theme.hex('accent')}; font-size:13px; border:none; padding:2px 0;")
            self._status_card.setStyleSheet(f"""
                QFrame {{
                    background: {theme.hex('card_bottom')};
                    border: 1px solid {theme.hex('accent')};
                    border-radius: 4px;
                }}
            """)
            self._detail_table.setRowCount(0)
            return

        # 故障解码
        decoded = jp.fault_mask_decode(mask)
        self._lbl_title.setText(f"● 当前故障状态: 故障 ({len(decoded)} 项)")
        self._lbl_title.setStyleSheet(
            "color:#F44336; font-size:13px; font-weight:bold; border:none;")
        self._lbl_desc.setText(jp.fault_mask_to_str(mask))
        self._lbl_desc.setStyleSheet(
            "color:#FF6B6B; font-size:13px; border:none; padding:2px 0;")
        self._status_card.setStyleSheet(f"""
            QFrame {{
                background: rgba(244,67,54,0.08);
                border: 1px solid #F44336;
                border-radius: 4px;
            }}
        """)

        # 详情表
        self._detail_table.setRowCount(0)
        for info in decoded:
            row = self._detail_table.rowCount()
            self._detail_table.insertRow(row)
            self._detail_table.setRowHeight(row, 24)
            csv_names = " / ".join(info['csv_names']) if info['csv_names'] else "—"
            # 处理方式: 取第一个 CSV 记录的 action
            action = "—"
            if info['csv_codes']:
                rec = jp.fault_code_lookup(info['csv_codes'][0])
                if rec:
                    action = rec['action']

            items = [
                (info['name'], True),
                (info['condition'], False),
                (csv_names, False),
                (action, False),
            ]
            for col, (text, is_bold) in enumerate(items):
                it = QTableWidgetItem(text)
                it.setTextAlignment(
                    Qt.AlignmentFlag.AlignVCenter |
                    (Qt.AlignmentFlag.AlignCenter if col == 0 else Qt.AlignmentFlag.AlignLeft))
                if is_bold:
                    f = QFont(); f.setBold(True)
                    it.setFont(f)
                    it.setForeground(QBrush(QColor('#FF6B6B')))
                self._detail_table.setItem(row, col, it)

    # ==================== 操作 ====================
    def _on_reload_csv(self):
        """重新加载 CSV (用户更新 CSV 后)。"""
        # 清缓存
        import jmproto.fault_codes as fc
        fc._FAULT_CODES_CACHE = None
        self._populate_csv_table()
        # Fault 位表里的 CSV 名称也需要刷新
        self._populate_fault_bit_table()
        self._refresh_status()

    # ==================== 主题 ====================
    def apply_theme(self):
        """主题切换刷新。"""
        self._status_card.setStyleSheet(f"""
            QFrame {{
                background: {theme.hex('card_bottom')};
                border: 1px solid {theme.hex('border')};
                border-radius: 4px;
            }}
        """)
        self._lbl_title.setStyleSheet(
            f"color:{theme.hex('title')}; font-size:13px; font-weight:bold; border:none;")
        self._lbl_mask.setStyleSheet(
            f"color:{theme.hex('muted')}; font-family:Consolas,monospace; "
            f"font-size:12px; border:none;")
        self._lbl_history.setStyleSheet(
            f"color:{theme.hex('text')}; font-family:Consolas,monospace; font-size:12px; border:none;")
        info_labels = self.findChildren(QLabel)
        for lbl in info_labels:
            if lbl.text().startswith("共 ") or lbl.text().startswith("最近"):
                lbl.setStyleSheet(f"color:{theme.hex('muted')}; font-size:12px; border:none;")
        # 故障屏蔽区
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
        self._refresh_status()
