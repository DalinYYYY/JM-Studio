"""电机标定面板

封装标定指令(0x90~0x98):
  - L1~L7 标定级别(0x90~0x96): 载荷 submode:u8, 进入 CALIB 态并启动标定
  - 进度查询 (0x97): ACK=完成, NACK(0x0A)=进行中, NACK(0x03)=未标定
  - 中止标定 (0x98): ACK

UI 由三部分组成:
  1. 顶部状态卡: 显示链路状态 / 当前 top_fsm / 标定是否进行中 / 最近操作结果
  2. 标定项选择: L1~L7 级别 + 子模式下拉, 含说明文字
  3. 操作按钮: 启动标定 / 查询进度 / 中止标定 / 自动查询开关
  4. 操作历史: 时间戳 + TX/ACK/NACK 文本, 滚动到最新

数据来源:
  - registry "校准" 类命令 (joint_motor_command_list.csv 第 58~66 条)
  - cmd_def.JmCmd.CALIB_* / JmErr.CALIB_BUSY / TopFsm.CALIB
  - 主窗口转发的 ACK/NACK/state_updated/connected 信号

后续优化方向(用户后续自行迭代):
  - 进度百分比展示(协议扩展后)
  - 各 L 子项独立的预设/参数表单
  - 标定结果验证(读 motor_info 校准数据并对比)
"""

from collections import deque
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget, QFormLayout,
    QSizePolicy,
)

from jmproto import JmCmd, JmErr, TopFsm, cmd_name, err_name_cn, top_fsm_name
from ui.theme import theme


# ==================== L1~L7 标定级别定义 ====================
# 每条: (cmd, 级别名称, [(sub_id, 子模式名, 描述), ...])
# 子模式范围严格对齐 CSV(joint_motor_command_list.csv 第 58~64 行备注)
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
        (3, "R/Ld/Lq/flux",  "定子电阻 / dq 电感 / 磁链辨识"),
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

        # ----- 顶部状态卡 -----
        self._status_card = QFrame()
        self._status_card.setFrameShape(QFrame.Shape.StyledPanel)
        self._status_card.setStyleSheet(f"""
            QFrame {{
                background: {theme.hex('card_bottom')};
                border: 1px solid {theme.hex('border')};
                border-radius: 4px;
            }}
        """)
        sc_layout = QGridLayout(self._status_card)
        sc_layout.setContentsMargins(12, 8, 12, 8)
        sc_layout.setHorizontalSpacing(8)
        sc_layout.setVerticalSpacing(4)

        self._lbl_status_dot = QLabel("●")
        self._lbl_status_dot.setStyleSheet(
            f"color: {theme.hex('muted')}; font-size: 18px; border:none;")
        self._lbl_status_dot.setFixedWidth(20)
        sc_layout.addWidget(self._lbl_status_dot, 0, 0)

        self._lbl_status_text = QLabel("未连接")
        self._lbl_status_text.setStyleSheet(
            f"font-family: Consolas, 'Microsoft YaHei', monospace; font-size: 13px; "
            f"font-weight: bold; color: {theme.hex('muted')}; border:none;")
        sc_layout.addWidget(self._lbl_status_text, 0, 1)

        self._lbl_last_op = QLabel("最近操作: —")
        self._lbl_last_op.setStyleSheet(
            f"color: {theme.hex('muted')}; font-size: 12px; border:none;")
        sc_layout.addWidget(self._lbl_last_op, 1, 0, 1, 2)

        layout.addWidget(self._status_card)

        # ----- 标定项选择 -----
        sel_grp = QGroupBox("标定项选择")
        sel_layout = QFormLayout(sel_grp)
        sel_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        sel_layout.setContentsMargins(8, 6, 8, 6)
        sel_layout.setSpacing(6)

        self._combo_level = QComboBox()
        for cmd, level_name, submodes in _CALIB_LEVELS:
            label = f"{level_name}  (0x{int(cmd):02X})"
            self._combo_level.addItem(label, (int(cmd), level_name, submodes))
        self._combo_level.currentIndexChanged.connect(self._on_level_changed)
        sel_layout.addRow("标定级别:", self._combo_level)

        self._combo_submode = QComboBox()
        self._combo_submode.currentIndexChanged.connect(self._on_submode_changed)
        sel_layout.addRow("子模式:", self._combo_submode)

        self._lbl_desc = QLabel("")
        self._lbl_desc.setWordWrap(True)
        self._lbl_desc.setMinimumHeight(56)
        self._lbl_desc.setTextFormat(Qt.TextFormat.PlainText)
        sel_layout.addRow("说明:", self._lbl_desc)

        layout.addWidget(sel_grp)

        # ----- 操作按钮 -----
        btn_grp = QGroupBox("标定操作")
        btn_layout = QGridLayout(btn_grp)
        btn_layout.setContentsMargins(8, 6, 8, 6)
        btn_layout.setSpacing(6)

        self._btn_start = QPushButton("启动标定")
        self._btn_start.setStyleSheet(
            f"background-color: {theme.hex('ok')}; color: {theme.hex('ok_text')}; "
            f"font-weight: bold; padding: 8px;")
        self._btn_start.clicked.connect(self._on_start_clicked)
        btn_layout.addWidget(self._btn_start, 0, 0)

        self._btn_query = QPushButton("查询进度")
        self._btn_query.clicked.connect(self._on_query_clicked)
        btn_layout.addWidget(self._btn_query, 0, 1)

        self._btn_abort = QPushButton("中止标定")
        self._btn_abort.setStyleSheet(
            f"background-color: {theme.hex('danger')}; color: {theme.hex('danger_text')}; "
            f"font-weight: bold; padding: 8px;")
        self._btn_abort.clicked.connect(self._on_abort_clicked)
        btn_layout.addWidget(self._btn_abort, 0, 2)

        self._chk_auto_poll = QCheckBox("标定进行中自动查询进度")
        self._chk_auto_poll.setChecked(True)
        self._chk_auto_poll.toggled.connect(self._on_auto_poll_toggled)
        btn_layout.addWidget(self._chk_auto_poll, 1, 0, 1, 3)

        layout.addWidget(btn_grp)

        # ----- 操作历史 -----
        hist_grp = QGroupBox("操作历史")
        hist_layout = QVBoxLayout(hist_grp)
        hist_layout.setContentsMargins(8, 6, 8, 6)
        hist_layout.setSpacing(4)

        self._history_view = QTextEdit()
        self._history_view.setReadOnly(True)
        self._history_view.setStyleSheet(
            f"background: {theme.hex('log_bg')}; color: {theme.hex('log_text')}; "
            f"font-family: Consolas, 'Microsoft YaHei', monospace; font-size: 12px; "
            f"border: 1px solid {theme.hex('border')};")
        self._history_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        hist_layout.addWidget(self._history_view, 1)

        op_row = QHBoxLayout()
        op_row.addStretch()
        self._btn_clear_history = QPushButton("清空历史")
        self._btn_clear_history.setFixedHeight(24)
        self._btn_clear_history.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear_history.clicked.connect(self._on_clear_history)
        op_row.addWidget(self._btn_clear_history)
        hist_layout.addLayout(op_row)

        layout.addWidget(hist_grp, 1)

        # 初始化子模式(触发 _on_level_changed -> _on_submode_changed)
        if self._combo_level.count() > 0:
            self._on_level_changed(0)

    # ==================== 级别/子模式选择 ====================
    def _on_level_changed(self, _idx: int):
        data = self._combo_level.currentData()
        if data is None:
            self._lbl_desc.setText("")
            return
        _cmd, _level_name, submodes = data
        self._combo_submode.blockSignals(True)
        self._combo_submode.clear()
        for sub_id, sub_name, sub_desc in submodes:
            self._combo_submode.addItem(
                f"{sub_name}  (sub={sub_id})", (sub_id, sub_name, sub_desc))
        self._combo_submode.blockSignals(False)
        # 手动触发一次说明刷新
        self._on_submode_changed(0)

    def _on_submode_changed(self, _idx: int):
        data = self._combo_submode.currentData()
        lvl_data = self._combo_level.currentData()
        if data is None or lvl_data is None:
            self._lbl_desc.setText("")
            return
        sub_id, sub_name, sub_desc = data
        cmd, level_name, _ = lvl_data
        text = (
            f"[{level_name} > {sub_name}]\n"
            f"CMD=0x{cmd:02X}, submode={sub_id}\n"
            f"{sub_desc}"
        )
        self._lbl_desc.setText(text)

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
            lvl_data = self._combo_level.currentData()
            sub_data = self._combo_submode.currentData()
            level_name = lvl_data[1] if lvl_data else ""
            sub_name = sub_data[1] if sub_data else ""
            self._set_last_op(f"已启动: {level_name} > {sub_name}")
            self._add_history(
                f"[ACK] {cmd_name(cmd)}(0x{cmd:02X}) 启动 {level_name}>{sub_name}")
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
    def _on_start_clicked(self):
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接, 无法启动")
            return
        lvl_data = self._combo_level.currentData()
        sub_data = self._combo_submode.currentData()
        if lvl_data is None or sub_data is None:
            return
        cmd, level_name, _ = lvl_data
        sub_id, sub_name, _ = sub_data
        self._add_history(
            f"[TX] 启动 {cmd_name(cmd)} submode={sub_id} ({level_name}>{sub_name})")
        self.send_command.emit(int(cmd), {"submode": int(sub_id)})

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
            f"color: {dot_color}; font-size: 18px; border:none;")
        self._lbl_status_text.setStyleSheet(
            f"font-family: Consolas, 'Microsoft YaHei', monospace; "
            f"font-size: 13px; font-weight: bold; color: {dot_color}; border:none;")
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
        self._lbl_desc.setStyleSheet(
            f"background: {theme.hex('input_bg')}; color: {theme.hex('text')}; "
            f"border: 1px solid {theme.hex('input_border')}; border-radius: 4px; "
            f"padding: 6px;")
        self._btn_start.setStyleSheet(
            f"background-color: {theme.hex('ok')}; color: {theme.hex('ok_text')}; "
            f"font-weight: bold; padding: 8px;")
        self._btn_abort.setStyleSheet(
            f"background-color: {theme.hex('danger')}; color: {theme.hex('danger_text')}; "
            f"font-weight: bold; padding: 8px;")
        self._history_view.setStyleSheet(
            f"background: {theme.hex('log_bg')}; color: {theme.hex('log_text')}; "
            f"font-family: Consolas, 'Microsoft YaHei', monospace; font-size: 12px; "
            f"border: 1px solid {theme.hex('border')};")
        self._refresh_status()

    # ==================== 配置持久化 ====================
    def get_opts(self) -> dict:
        """收集可持久化的 UI 配置: 当前级别/子模式选择 + 自动查询开关."""
        opts = {}
        try:
            opts["current_level"] = int(self._combo_level.currentIndex())
            opts["current_submode"] = int(self._combo_submode.currentIndex())
            opts["auto_poll"] = bool(self._chk_auto_poll.isChecked())
        except Exception:
            pass
        return opts

    def set_opts(self, opts: dict):
        """启动时套用配置 (容错)."""
        if not isinstance(opts, dict):
            return
        try:
            lvl = int(opts.get("current_level", 0))
            if 0 <= lvl < self._combo_level.count():
                self._combo_level.setCurrentIndex(lvl)
        except Exception:
            pass
        # 子模式依赖级别已加载, 但 setCurrentIndex 触发的 _on_level_changed
        # 会重置 submode 列表(选中第0项), 故需在信号处理后再恢复
        try:
            sub = int(opts.get("current_submode", 0))
            if 0 <= sub < self._combo_submode.count():
                self._combo_submode.setCurrentIndex(sub)
        except Exception:
            pass
        if "auto_poll" in opts:
            try:
                self._chk_auto_poll.setChecked(bool(opts["auto_poll"]))
            except Exception:
                pass
