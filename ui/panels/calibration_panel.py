"""电机标定面板

封装标定指令(0x90~0x98) 与标定结果读写(0xE6~0xEA)。

UI 布局 (紧凑, 状态卡仅 2 行 ~70px):
  1. 紧凑状态卡:
     - 主信息行(单行, 横向排列): [●状态点] [状态文本] · [最近操作] [弹簧]
       [已标定徽章][标记][清除] [查询][中止][自动查询]
     - 当前选中任务行(小字, 带左侧高亮条): 级别>子项 CMD=0xXX submode=N · 描述
  2. 标定任务: 顶部 QTabWidget, L1~L7 每级一个 Tab, 选中 Tab 才显示该级子项卡片,
     点击子项卡片即启动该标定
  3. 标定结果: 内嵌 ParamPanel(source="motor_config", groups=["MotorCalibParam"]),
     展示下位机读回值/修改值/单位, 支持单读/单写/全读/全写/保存到Flash(0xEA)
  4. 操作历史: 时间戳 + TX/ACK/NACK 文本

数据来源:
  - registry "校准" 类命令 (joint_motor_command_list.csv 第 58~66 条)
  - motor_info.csv 的 MotorCalibParam 分段(Index 16~42, 标定结果)
  - cmd_def.JmCmd.CALIB_* / JmErr.CALIB_BUSY / TopFsm.CALIB
  - 主窗口转发的 ACK/NACK/state_updated/connected 信号
"""

from collections import deque
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QPushButton, QTabWidget, QTextEdit, QVBoxLayout,
    QWidget, QSizePolicy,
)

from jmproto import JmCmd, JmErr, TopFsm, cmd_name, err_name_cn, top_fsm_name
from ui.theme import theme


# ==================== L1~L7 标定级别定义 ====================
# 每条: (cmd, 级别名称, [(sub_id, 子模式名, 描述), ...])
# L2 子模式按固件实际实现拆分(submode 3-6 分别对应 R/Ld/Lq/flux, 各有独立测试方法)
_CALIB_LEVELS = [
    (JmCmd.CALIB_LEVEL1, "L1 驱动硬件底层", [
        (1, "ADC偏置",     "电流/电压采样通道零点偏置校正"),
        (2, "ADC增益",     "电流/电压采样通道增益校正"),
        (3, "电流传感器",  "相电流传感器线性度与零漂"),
        (4, "温度传感器",  "FET/电机 NTC 温度采样校正"),
        (5, "母线电压",    "母线电压分压比与零点校正"),
        (6, "死区特性",    "逆变器死区时间与管压降补偿"),
    ]),
    (JmCmd.CALIB_LEVEL2, "L2 电机电气身份", [
        (1, "相序",          "U/V/W 相序方向辨识"),
        (2, "极对数",        "电机极对数自动辨识"),
        (3, "R 相电阻",      "相电阻辨识 (DC法)"),
        (4, "Ld d轴电感",    "d轴电感辨识 (阶跃响应)"),
        (5, "Lq q轴电感",    "q轴电感辨识 (阶跃响应)"),
        (6, "flux 磁链",     "永磁体磁链辨识 (反电势法)"),
    ]),
    (JmCmd.CALIB_LEVEL3, "L3 编码器校准", [
        (1, "零位",          "编码器电角度零点对齐"),
        (2, "方向",          "编码器计数方向校验"),
        (3, "线性度",        "编码器非线性度扫描"),
        (4, "正余弦/旋变",   "正余弦/旋变解码参数校准"),
        (5, "多圈零点",      "多圈计数器零点校准"),
    ]),
    (JmCmd.CALIB_LEVEL4, "L4 转矩基础", [
        (1, "力矩常数 Kt",   "转矩常数 Kt 辨识"),
    ]),
    (JmCmd.CALIB_LEVEL5, "L5 非线性补偿", [
        (1, "齿槽",          "齿槽转矩纹波补偿表生成"),
        (2, "摩擦",          "摩擦模型(库仑+粘性)辨识"),
        (3, "死区补偿",      "逆变器死区非线性补偿"),
        (4, "磁饱和",        "dq 轴磁饱和电感曲线辨识"),
    ]),
    (JmCmd.CALIB_LEVEL6, "L6 负载系统级", [
        (1, "惯量",          "负载转动惯量辨识"),
        (2, "阻尼",          "负载粘性阻尼系数辨识"),
        (3, "回程间隙",      "减速器回程间隙测量"),
        (4, "PID 自整定",    "速度/位置环 PID 自动整定"),
    ]),
    (JmCmd.CALIB_LEVEL7, "L7 自动化集成", [
        (1, "一键全自动",    "依次执行 L1~L6 全套标定"),
    ]),
]


class CalibrationPanel(QGroupBox):
    """电机标定面板 (顶级 Tab)。

    信号:
      send_command(cmd, values): 发送标定指令, 主窗口连到 JmClient.send_command
        - 启动: cmd=0x90~0x96, values={"submode": N}
        - 查询: cmd=0x97, values={}
        - 中止: cmd=0x98, values={}

    由主窗口调用的入口:
      set_link_active(active):        链路状态变化
      on_ack(cmd):                    标定命令 ACK (内部按 cmd 过滤)
      on_nack(cmd, err):              标定命令 NACK (内部按 cmd 过滤)
      update_state(top, run, mode, en): 状态机更新, 用于判断是否进入 CALIB 态
      apply_theme():                  主题切换
    """

    send_command = pyqtSignal(int, dict)

    _MAX_HISTORY = 200
    _POLL_PERIOD_MS = 500   # 标定进行中自动查询进度周期

    def __init__(self, parent=None):
        super().__init__("", parent)
        self._panel_name = "电机标定"
        self._link_active = False
        self._current_top_fsm = None
        self._calib_running = False     # 本地推测: 标定是否进行中
        self._last_op = ""              # 最近一次操作结果文本
        self._history = deque(maxlen=self._MAX_HISTORY)
        # 当前选中任务: (cmd, sub_id, level_name, sub_name, sub_desc)
        self._active_task_key = None
        self._active_task_text = "当前选中: —"
        # Task 3 注入的内嵌标定结果面板
        self._config_panel = None

        self._build()

        # 标定进行中周期查询进度
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self._POLL_PERIOD_MS)
        self._poll_timer.timeout.connect(self._on_poll_tick)

        self._refresh_status()

    def panel_name(self) -> str:
        return self._panel_name

    # ==================== UI 构建 ====================
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self._build_status_card(layout)      # 含标定操作(查询/中止/自动查询)
        self._build_task_launcher(layout)    # L1~L7 顶部 QTabWidget
        self._build_results_placeholder(layout)
        self._build_history(layout)

    def _build_status_card(self, parent_layout):
        """紧凑状态卡: 主信息行(单行) + 当前选中任务行(小字), 总高 ~70px."""
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
        sc_layout.setContentsMargins(8, 5, 8, 5)
        sc_layout.setSpacing(3)

        # --- 主信息行(单行横向): 状态点+文本 · 最近操作 [弹簧] 已标定+按钮 + 操作按钮 ---
        main_row = QHBoxLayout()
        main_row.setContentsMargins(0, 0, 0, 0)
        main_row.setSpacing(6)

        self._lbl_status_dot = QLabel("●")
        self._lbl_status_dot.setStyleSheet(
            f"color: {theme.hex('muted')}; font-size: 14px; border:none;")
        self._lbl_status_dot.setFixedWidth(14)
        main_row.addWidget(self._lbl_status_dot)

        self._lbl_status_text = QLabel("未连接")
        self._lbl_status_text.setStyleSheet(
            f"font-family: Consolas, 'Microsoft YaHei', monospace; font-size: 12px; "
            f"font-weight: bold; color: {theme.hex('muted')}; border:none;")
        main_row.addWidget(self._lbl_status_text)

        self._lbl_sep = QLabel("·")
        self._lbl_sep.setStyleSheet(
            f"color: {theme.hex('muted')}; font-size: 11px; border:none;")
        main_row.addWidget(self._lbl_sep)

        self._lbl_last_op = QLabel("最近操作: —")
        self._lbl_last_op.setStyleSheet(
            f"color: {theme.hex('muted')}; font-size: 11px; border:none;")
        main_row.addWidget(self._lbl_last_op)

        main_row.addStretch()

        # 已标定指示器 + 设置/清除按钮(Task 5 在此槽位插入; 这里先占位)
        self._build_calib_flag_controls(main_row)

        # 标定操作嵌入状态卡右侧 (查询/中止/自动查询)
        self._btn_query = QPushButton("查询")
        self._btn_query.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_query.setFixedHeight(22)
        self._btn_query.setStyleSheet(
            f"QPushButton {{ background: {theme.hex('input_bg')}; color: {theme.hex('text')}; "
            f"border: 1px solid {theme.hex('border')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 11px; }}"
            f"QPushButton:hover {{ border-color: {theme.hex('accent')}; "
            f"color: {theme.hex('accent')}; }}")
        self._btn_query.clicked.connect(self._on_query_clicked)
        main_row.addWidget(self._btn_query)

        self._btn_abort = QPushButton("中止")
        self._btn_abort.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_abort.setFixedHeight(22)
        self._btn_abort.setStyleSheet(
            f"QPushButton {{ background-color: {theme.hex('danger')}; color: {theme.hex('danger_text')}; "
            f"border: 1px solid {theme.hex('danger')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 11px; font-weight: bold; }}"
            f"QPushButton:hover {{ opacity: 0.85; }}")
        self._btn_abort.clicked.connect(self._on_abort_clicked)
        main_row.addWidget(self._btn_abort)

        self._chk_auto_poll = QCheckBox("自动")
        self._chk_auto_poll.setChecked(True)
        self._chk_auto_poll.setStyleSheet(
            f"QCheckBox {{ color: {theme.hex('muted')}; font-size: 11px; spacing: 3px; }}")
        self._chk_auto_poll.toggled.connect(self._on_auto_poll_toggled)
        main_row.addWidget(self._chk_auto_poll)

        sc_layout.addLayout(main_row)

        # --- 当前选中任务行(小字, 带左侧高亮条) ---
        self._lbl_active_task = QLabel(self._active_task_text)
        self._lbl_active_task.setWordWrap(False)
        self._lbl_active_task.setTextFormat(Qt.TextFormat.PlainText)
        self._lbl_active_task.setStyleSheet(
            f"color: {theme.hex('text')}; font-size: 11px; border:none; "
            f"background: {theme.hex('card_bottom')}; "
            f"border-left: 2px solid {theme.hex('accent')}; "
            f"padding: 2px 8px; border-radius: 2px;")
        sc_layout.addWidget(self._lbl_active_task)

        parent_layout.addWidget(self._status_card)

    def _build_calib_flag_controls(self, layout):
        """已标定指示器 + 设置/清除按钮占位(Task 5 实现, 这里先放空 widget 保持槽位)."""
        # Task 5 会在此插入 _lbl_calib_flag / _btn_mark_calibrated / _btn_clear_calibrated
        # 占位 widget 避免 layout 在 Task 5 之前为空
        placeholder = QWidget()
        layout.addWidget(placeholder)

    def _build_task_launcher(self, parent_layout):
        """L1~L7 顶部 QTabWidget, 每个 Tab 显示该级别的子项卡片, 点击卡片即启动."""
        grp = QGroupBox("标定任务  (选中级别 Tab → 点击子项启动)")
        v = QVBoxLayout(grp)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        self._task_tabs = QTabWidget()
        self._task_tabs.setDocumentMode(True)
        self._task_tabs.setStyleSheet(self._tab_style())

        self._task_buttons = {}  # (cmd, sub_id) -> QPushButton

        for cmd, level_name, submodes in _CALIB_LEVELS:
            page = QWidget()
            page_layout = QHBoxLayout(page)
            page_layout.setContentsMargins(8, 8, 8, 8)
            page_layout.setSpacing(8)
            page_layout.addStretch()
            # 每个子项为可点击卡片(QPushButton 带描述), 横向排列
            for sub_id, sub_name, sub_desc in submodes:
                btn = QPushButton(f"{sub_name}\n{sub_desc}")
                btn.setToolTip(f"CMD=0x{int(cmd):02X}  submode={sub_id}\n{sub_desc}")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setMinimumWidth(130)
                btn.setMinimumHeight(54)
                btn.setStyleSheet(self._task_btn_style(active=False))
                btn.clicked.connect(
                    lambda _=False, c=int(cmd), s=int(sub_id),
                           ln=level_name, sn=sub_name, sd=sub_desc:
                    self._on_task_clicked(c, s, ln, sn, sd))
                self._task_buttons[(int(cmd), int(sub_id))] = btn
                page_layout.addWidget(btn)
            page_layout.addStretch()
            tab_title = f"{level_name}  0x{int(cmd):02X}"
            self._task_tabs.addTab(page, tab_title)

        v.addWidget(self._task_tabs)
        parent_layout.addWidget(grp)

    def _tab_style(self) -> str:
        return f"""
            QTabWidget::pane {{
                border: 1px solid {theme.hex('border')};
                border-radius: 4px;
                top: -1px;
            }}
            QTabBar::tab {{
                background: {theme.hex('input_bg')};
                color: {theme.hex('muted')};
                border: 1px solid {theme.hex('border')};
                border-bottom: none;
                padding: 4px 12px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                font-size: 12px;
            }}
            QTabBar::tab:selected {{
                background: {theme.hex('card_bottom')};
                color: {theme.hex('accent')};
                border-color: {theme.hex('border')};
                font-weight: bold;
            }}
            QTabBar::tab:hover:!selected {{
                color: {theme.hex('text')};
            }}
        """

    def _build_results_placeholder(self, parent_layout):
        """标定结果区占位 (Task 3 由 attach_results_panel 注入实际 ParamPanel)."""
        self._results_container = QGroupBox("标定结果  (下位机读回值 / 修改 / 保存)")
        v = QVBoxLayout(self._results_container)
        v.setContentsMargins(8, 6, 8, 6)
        self._results_placeholder = QLabel(
            "（Task 3 注入: 连接后自动读回 MotorCalibParam 段 Index 16~42）")
        self._results_placeholder.setStyleSheet(
            f"color: {theme.hex('muted')}; font-size: 12px; padding: 12px;")
        v.addWidget(self._results_placeholder)
        parent_layout.addWidget(self._results_container)

    def attach_results_panel(self, panel):
        """Task 3: 注入内嵌 ParamPanel 替换占位 (Main_window 在初始化后调用)."""
        if self._config_panel is not None:
            return
        self._config_panel = panel
        # 清除占位
        lay = self._results_container.layout()
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        lay.addWidget(panel)

    def _build_history(self, parent_layout):
        """操作历史: 时间戳 + TX/ACK/NACK 文本, 滚动到最新."""
        grp = QGroupBox("操作历史")
        v = QVBoxLayout(grp)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        self._history_view = QTextEdit()
        self._history_view.setReadOnly(True)
        self._history_view.setStyleSheet(
            f"background: {theme.hex('log_bg')}; color: {theme.hex('log_text')}; "
            f"font-family: Consolas, 'Microsoft YaHei', monospace; font-size: 12px; "
            f"border: 1px solid {theme.hex('border')};")
        self._history_view.setSizePolicy(QSizePolicy.Policy.Expanding,
                                         QSizePolicy.Policy.Expanding)
        v.addWidget(self._history_view, 1)

        op_row = QHBoxLayout()
        op_row.addStretch()
        self._btn_clear_history = QPushButton("清空历史")
        self._btn_clear_history.setFixedHeight(24)
        self._btn_clear_history.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear_history.clicked.connect(self._on_clear_history)
        op_row.addWidget(self._btn_clear_history)
        v.addLayout(op_row)

        parent_layout.addWidget(grp, 1)

    # ==================== 任务按钮样式 (卡片式) ====================
    def _task_btn_style(self, active: bool = False) -> str:
        bg = theme.hex('accent') if active else theme.hex('input_bg')
        fg = theme.hex('card_bottom') if active else theme.hex('text')
        border = theme.hex('accent') if active else theme.hex('input_border')
        return (
            f"QPushButton {{"
            f"  background-color: {bg}; color: {fg};"
            f"  border: 1px solid {border}; border-radius: 6px;"
            f"  padding: 6px 10px; font-size: 12px;"
            f"  text-align: left;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background-color: {theme.hex('accent')}; color: {theme.hex('card_bottom')};"
            f"  border-color: {theme.hex('accent')};"
            f"}}"
        )

    def _refresh_task_buttons_style(self):
        """刷新所有任务卡片样式 (按当前选中态)."""
        for key, btn in self._task_buttons.items():
            btn.setStyleSheet(self._task_btn_style(active=(key == self._active_task_key)))

    # ==================== 任务点击 / 选中态 ====================
    def _on_task_clicked(self, cmd: int, sub_id: int,
                         level_name: str, sub_name: str, sub_desc: str):
        """点击子项卡片: 启动标定并更新选中态."""
        self._set_active_task(cmd, sub_id, level_name, sub_name, sub_desc)
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接, 无法启动")
            return
        self._add_history(
            f"[TX] 启动 {cmd_name(cmd)} submode={sub_id} ({level_name}>{sub_name})")
        self.send_command.emit(int(cmd), {"submode": int(sub_id)})

    def _set_active_task(self, cmd: int, sub_id: int,
                         level_name: str, sub_name: str, sub_desc: str):
        """更新当前选中任务显示与卡片高亮."""
        self._active_task_key = (int(cmd), int(sub_id))
        self._active_task_text = (
            f"当前选中: {level_name} > {sub_name}  "
            f"CMD=0x{cmd:02X}, submode={sub_id}  ·  {sub_desc}")
        self._lbl_active_task.setText(self._active_task_text)
        self._refresh_task_buttons_style()
        # 切到对应 Tab
        for i in range(self._task_tabs.count()):
            if int(_CALIB_LEVELS[i][0]) == int(cmd):
                if self._task_tabs.currentIndex() != i:
                    self._task_tabs.setCurrentIndex(i)
                break

    def _lookup_task(self, cmd: int, sub_id: int):
        """根据 (cmd, sub_id) 反查 (level_name, sub_name, sub_desc), 找不到返回 (None,...)."""
        for c, level_name, submodes in _CALIB_LEVELS:
            if int(c) == int(cmd):
                for sid, sn, sd in submodes:
                    if int(sid) == int(sub_id):
                        return level_name, sn, sd
        return None, None, None

    # ==================== 状态接收 (由主窗口调用) ====================
    def set_link_active(self, active: bool):
        self._link_active = bool(active)
        if not active:
            self._calib_running = False
            self._poll_timer.stop()
        self._refresh_status()

    def update_state(self, top_fsm: int, run_state: int, ctrl_mode: int, enable: int):
        """接收主窗口转发的状态机更新, 用于判断是否处于 CALIB 态."""
        prev = self._current_top_fsm
        self._current_top_fsm = top_fsm
        in_calib = top_fsm == int(TopFsm.CALIB)
        prev_in_calib = prev == int(TopFsm.CALIB) if prev is not None else False
        # 进入 CALIB 态: 启动周期查询
        if in_calib and not prev_in_calib:
            self._calib_running = True
            if self._chk_auto_poll.isChecked() and self._link_active:
                self._poll_timer.start()
            self._add_history(f"[进入标定态] top_fsm={top_fsm_name(top_fsm)}")
        # 离开 CALIB 态: 停止查询
        elif not in_calib and prev_in_calib:
            self._calib_running = False
            self._poll_timer.stop()
            self._add_history(f"[退出标定态] top_fsm={top_fsm_name(top_fsm)}")
        self._refresh_status()

    def on_ack(self, cmd: int):
        """标定相关命令 ACK (主窗口在 _on_ack 中调用, 内部按 cmd 过滤)."""
        if not self._is_calib_cmd(cmd):
            return
        if cmd == JmCmd.CALIB_QUERY:
            self._calib_running = False
            self._poll_timer.stop()
            self._set_last_op("查询: 标定完成")
            self._add_history(f"[ACK] {cmd_name(cmd)}(0x{cmd:02X}) 标定完成")
        elif cmd == JmCmd.CALIB_ABORT:
            self._calib_running = False
            self._poll_timer.stop()
            self._set_last_op("已中止标定")
            self._add_history(f"[ACK] {cmd_name(cmd)}(0x{cmd:02X}) 已中止")
        else:
            # 启动类(0x90~0x96) ACK: 表示已进入标定态(由 state_updated 同步)
            if self._active_task_key is not None:
                cmd2, sub_id = self._active_task_key
                level_name, sub_name, _ = self._lookup_task(cmd2, sub_id)
                if level_name:
                    self._set_last_op(f"已启动: {level_name} > {sub_name}")
                    self._add_history(
                        f"[ACK] {cmd_name(cmd)}(0x{cmd:02X}) 启动 {level_name}>{sub_name}")
                else:
                    self._set_last_op("已启动")
                    self._add_history(f"[ACK] {cmd_name(cmd)}(0x{cmd:02X}) 启动")
            else:
                self._set_last_op("已启动")
                self._add_history(f"[ACK] {cmd_name(cmd)}(0x{cmd:02X}) 启动")
        self._refresh_status()

    def on_nack(self, cmd: int, err: int):
        """标定相关命令 NACK (主窗口在 _on_nack 中调用, 内部按 cmd 过滤)."""
        if not self._is_calib_cmd(cmd):
            return
        cn = err_name_cn(err)
        if cmd == JmCmd.CALIB_QUERY:
            if err == JmErr.CALIB_BUSY:
                self._calib_running = True
                if self._chk_auto_poll.isChecked() and self._link_active:
                    self._poll_timer.start()
                self._set_last_op("查询: 标定进行中…")
                self._add_history(
                    f"[NACK] {cmd_name(cmd)}(0x{cmd:02X}) 标定进行中 (CALIB_BUSY)")
            elif err == JmErr.STATE_DENY:
                self._calib_running = False
                self._poll_timer.stop()
                self._set_last_op("查询: 未标定 (state_deny)")
                self._add_history(
                    f"[NACK] {cmd_name(cmd)}(0x{cmd:02X}) 未标定 (STATE_DENY)")
            else:
                self._set_last_op(f"查询: 错误 {cn}")
                self._add_history(
                    f"[NACK] {cmd_name(cmd)}(0x{cmd:02X}) err={cn}(0x{err:02X})")
        else:
            self._set_last_op(f"失败: {cn}")
            self._add_history(
                f"[NACK] {cmd_name(cmd)}(0x{cmd:02X}) err={cn}(0x{err:02X})")
        self._refresh_status()

    @staticmethod
    def _is_calib_cmd(cmd: int) -> bool:
        return int(JmCmd.CALIB_LEVEL1) <= cmd <= int(JmCmd.CALIB_ABORT)

    # ==================== 按钮回调 ====================
    def _on_query_clicked(self):
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接, 无法查询")
            return
        self._add_history("[TX] 查询标定进度 (0x97)")
        self.send_command.emit(int(JmCmd.CALIB_QUERY), {})

    def _on_abort_clicked(self):
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接, 无法中止")
            return
        self._add_history("[TX] 中止标定 (0x98)")
        self.send_command.emit(int(JmCmd.CALIB_ABORT), {})

    def _on_auto_poll_toggled(self, on: bool):
        if on and self._calib_running and self._link_active:
            self._poll_timer.start()
        else:
            self._poll_timer.stop()

    def _on_poll_tick(self):
        if self._link_active and self._calib_running:
            self.send_command.emit(int(JmCmd.CALIB_QUERY), {})

    # ==================== 历史记录 ====================
    def _add_history(self, text: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._history.append(f"{ts}  {text}")
        self._history_view.setPlainText("\n".join(self._history))
        # 滚动到末尾
        sb = self._history_view.verticalScrollBar()
        if sb is not None:
            sb.setValue(sb.maximum())

    def _on_clear_history(self):
        self._history.clear()
        self._history_view.setPlainText("")

    def _set_last_op(self, text: str):
        self._last_op = text
        self._lbl_last_op.setText(f"最近操作: {text}")

    # ==================== 状态显示 ====================
    def _refresh_status(self):
        if not self._link_active:
            dot_color = theme.hex("muted")
            text = "未连接"
        elif self._calib_running:
            dot_color = theme.hex("warn")
            top_text = top_fsm_name(self._current_top_fsm) \
                if self._current_top_fsm is not None else "未知"
            text = f"标定进行中  (top_fsm={top_text})"
        elif self._current_top_fsm == int(TopFsm.CALIB):
            dot_color = theme.hex("accent")
            text = "标定态就绪"
        else:
            dot_color = theme.hex("accent")
            top_text = top_fsm_name(self._current_top_fsm) \
                if self._current_top_fsm is not None else "未知"
            text = f"已连接  (top_fsm={top_text})"
        self._lbl_status_dot.setStyleSheet(
            f"color: {dot_color}; font-size: 14px; border:none;")
        self._lbl_status_text.setStyleSheet(
            f"font-family: Consolas, 'Microsoft YaHei', monospace; "
            f"font-size: 12px; font-weight: bold; color: {dot_color}; border:none;")
        self._lbl_status_text.setText(text)

    # ==================== 主题 ====================
    def apply_theme(self):
        self._status_card.setStyleSheet(f"""
            QFrame {{
                background: {theme.hex('card_bottom')};
                border: 1px solid {theme.hex('border')};
                border-radius: 4px;
            }}
        """)
        self._task_tabs.setStyleSheet(self._tab_style())
        self._lbl_active_task.setStyleSheet(
            f"color: {theme.hex('text')}; font-size: 11px; border:none; "
            f"background: {theme.hex('card_bottom')}; "
            f"border-left: 2px solid {theme.hex('accent')}; "
            f"padding: 2px 8px; border-radius: 2px;")
        self._btn_abort.setStyleSheet(
            f"QPushButton {{ background-color: {theme.hex('danger')}; color: {theme.hex('danger_text')}; "
            f"border: 1px solid {theme.hex('danger')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 11px; font-weight: bold; }}"
            f"QPushButton:hover {{ opacity: 0.85; }}")
        self._history_view.setStyleSheet(
            f"background: {theme.hex('log_bg')}; color: {theme.hex('log_text')}; "
            f"font-family: Consolas, 'Microsoft YaHei', monospace; font-size: 12px; "
            f"border: 1px solid {theme.hex('border')};")
        self._refresh_task_buttons_style()
        if self._config_panel is not None:
            fn = getattr(self._config_panel, "apply_theme", None)
            if callable(fn):
                fn()
        self._refresh_status()

    # ==================== 配置持久化 ====================
    def get_opts(self) -> dict:
        """收集可持久化的 UI 配置: 当前 Tab + 当前选中任务 + 自动查询开关 + 内嵌面板列宽。"""
        opts = {}
        try:
            opts["auto_poll"] = bool(self._chk_auto_poll.isChecked())
            opts["task_tab_index"] = int(self._task_tabs.currentIndex())
            if self._active_task_key is not None:
                opts["active_task"] = list(self._active_task_key)
        except Exception:
            pass
        if self._config_panel is not None:
            try:
                opts["config_panel"] = self._config_panel.get_opts()
            except Exception:
                pass
        return opts

    def set_opts(self, opts: dict):
        """启动时套用配置 (容错)."""
        if not isinstance(opts, dict):
            return
        if "auto_poll" in opts:
            try:
                self._chk_auto_poll.setChecked(bool(opts["auto_poll"]))
            except Exception:
                pass
        if "task_tab_index" in opts:
            try:
                idx = int(opts["task_tab_index"])
                if 0 <= idx < self._task_tabs.count():
                    self._task_tabs.setCurrentIndex(idx)
            except Exception:
                pass
        if "active_task" in opts:
            try:
                cmd, sub_id = opts["active_task"]
                lvl_name, sub_name, sub_desc = self._lookup_task(int(cmd), int(sub_id))
                if lvl_name:
                    self._set_active_task(int(cmd), int(sub_id), lvl_name, sub_name, sub_desc)
            except Exception:
                pass
        if self._config_panel is not None and isinstance(opts.get("config_panel"), dict):
            try:
                self._config_panel.set_opts(opts["config_panel"])
            except Exception:
                pass
