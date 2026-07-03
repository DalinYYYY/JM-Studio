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
    QLabel, QPushButton, QSpinBox, QSplitter, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget, QSizePolicy,
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


# ==================== 标定级别 -> 标定结果参数映射 ====================
# 需求1: 标定完成时只请求本次标定对应的参数(而非全部 MotorCalibParam)。
# 空 list 表示该子项产出在固件内部 RAM/表格, motor_info.csv 无对应字段。
# is_calibrated(16) 仅 L7 全自动完成后包含; 单子项标定不自动改写总标志位。
# 参考: motor_info.csv Index 16~42 的 MotorCalibParam 段。
_CALIB_RESULT_MAP = {
    # L1 驱动硬件底层 (0x90)
    (0x90, 1): [],                  # ADC偏置 -> 固件内部
    (0x90, 2): [],                  # ADC增益 -> 固件内部
    (0x90, 3): [41, 42],            # 电流传感器 -> shunt_resistance, current_amp_gain
    (0x90, 4): [],                  # 温度传感器 -> motor_info 无字段
    (0x90, 5): [],                  # 母线电压 -> motor_info 无字段
    (0x90, 6): [40],                # 死区特性 -> dead_time_ns
    # L2 电机电气身份 (0x91)
    (0x91, 1): [19],                # 相序 -> direction
    (0x91, 2): [17],                # 极对数 -> pole_pairs
    (0x91, 3): [20],                # R 相电阻 -> phase_resistance
    (0x91, 4): [21],                # Ld -> phase_inductance_d
    (0x91, 5): [22],                # Lq -> phase_inductance_q
    (0x91, 6): [23],                # flux -> flux_linkage
    # L3 编码器校准 (0x92)
    (0x92, 1): [38, 37],            # 零位 -> elec_angle_bias, enc_offset
    (0x92, 2): [36],                # 方向 -> enc_direction
    (0x92, 3): [],                  # 线性度 -> 固件内部表格
    (0x92, 4): [],                  # 正余弦/旋变 -> 固件内部
    (0x92, 5): [],                  # 多圈零点 -> motor_info 无字段
    # L4 转矩基础 (0x93)
    (0x93, 1): [24],                # Kt -> torque_constant
    # L5 非线性补偿 (0x94)
    (0x94, 1): [],                  # 齿槽 -> 固件内部表格
    (0x94, 2): [26, 27],            # 摩擦 -> friction_coulomb, friction_viscous
    (0x94, 3): [],                  # 死区补偿 -> 固件内部曲线
    (0x94, 4): [],                  # 磁饱和 -> 固件内部曲线
    # L6 负载系统级 (0x95)
    (0x95, 1): [25],                # 惯量 -> rotor_inertia
    (0x95, 2): [27],                # 阻尼 -> friction_viscous (与 L5>2 共享字段)
    (0x95, 3): [28, 29],            # 回程间隙 -> gear_ratio, gear_efficiency
    (0x95, 4): [33],                # PID 自整定 -> current_control_bandwidth
    # L7 自动化集成 (0x96): 全部产出参数 + is_calibrated 总标志位
    (0x96, 1): [16, 17, 19, 20, 21, 22, 23, 24, 25, 26, 27, 36, 37, 38, 40],
}


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
        # Task 5: 已标定徽章当前文本 ("未读取" / "已标定" / "未标定")
        # Index 16 (is_calibrated) 读回值驱动, 主窗口在 _on_param_result 中转发
        self._calib_flag_text = "未读取"

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

        self._build_status_card(layout)      # 状态卡(含已标定独占列)
        self._build_task_launcher(layout)    # L1~L7 顶部 QTabWidget + 右侧操作列(固定高度)
        # 标定结果 + 操作历史 用 QSplitter: 历史显示时可拖动调整与结果的垂直比例
        self._build_results_history_split(layout)

    def _build_status_card(self, parent_layout):
        """紧凑状态卡: 左侧主信息(2行) + 右侧已标定独占列(徽章+标记+清除), 总高 ~70px."""
        self._status_card = QFrame()
        self._status_card.setFrameShape(QFrame.Shape.StyledPanel)
        # 需求4: 容器背景统一为 panel_bg (与外层 QGroupBox 一致), 仅保留 border 分隔
        self._status_card.setStyleSheet(f"""
            QFrame {{
                background: {theme.hex('panel_bg')};
                border: 1px solid {theme.hex('border')};
                border-radius: 4px;
            }}
        """)
        outer = QHBoxLayout(self._status_card)
        outer.setContentsMargins(8, 5, 8, 5)
        outer.setSpacing(10)

        # ============ 左侧: 主信息(2行) ============
        left_wrap = QWidget()
        left_layout = QVBoxLayout(left_wrap)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(3)

        # 主信息行(单行): 状态点+文本 · 最近操作 [弹簧]
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
        left_layout.addLayout(main_row)

        # 当前选中任务行(小字, 带左侧高亮条)
        self._lbl_active_task = QLabel(self._active_task_text)
        self._lbl_active_task.setWordWrap(False)
        self._lbl_active_task.setTextFormat(Qt.TextFormat.PlainText)
        self._lbl_active_task.setStyleSheet(
            f"color: {theme.hex('text')}; font-size: 11px; border:none; "
            f"background: {theme.hex('panel_bg')}; "
            f"border-left: 2px solid {theme.hex('accent')}; "
            f"padding: 2px 8px; border-radius: 2px;")
        left_layout.addWidget(self._lbl_active_task)

        outer.addWidget(left_wrap, 1)

        # ============ 右侧: 已标定独占列 (徽章 + 标记/清除) ============
        self._build_calib_flag_controls(outer)

        parent_layout.addWidget(self._status_card)

    def _build_calib_flag_controls(self, outer_layout):
        """右侧已标定独占列: 徽章(大字醒目) + 标记/清除按钮横排.

        徽章文本由 Index 16 (is_calibrated) 读回值驱动:
          - "1"  -> "已标定" (绿/accent)
          - "0"  -> "未标定" (红/danger)
          - 其他 -> "未读取" (灰/muted)

        标记/清除按钮走 _config_panel.write_param(16, "1"/"0") -> 0xE7 motor_info_write,
        与标定结果面板共用同一通道, 写完后由主窗口读回 Index 16 同步实际状态。
        """
        col = QWidget()
        col.setFixedWidth(170)
        col_layout = QVBoxLayout(col)
        col_layout.setContentsMargins(12, 0, 0, 0)
        col_layout.setSpacing(5)
        col_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._lbl_calib_flag = QLabel(self._calib_flag_text)
        self._lbl_calib_flag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_calib_flag.setFixedHeight(26)
        self._lbl_calib_flag.setMinimumWidth(140)
        self._lbl_calib_flag.setStyleSheet(self._calib_flag_style())
        col_layout.addWidget(self._lbl_calib_flag)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(5)
        self._btn_mark_calibrated = QPushButton("标记")
        self._btn_mark_calibrated.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_mark_calibrated.setFixedHeight(22)
        self._btn_mark_calibrated.setToolTip("标记为已标定 (写 Index 16 = 1, 走 0xE7)")
        self._btn_mark_calibrated.setStyleSheet(self._calib_action_btn_style())
        self._btn_mark_calibrated.clicked.connect(self._on_mark_calibrated)
        self._btn_mark_calibrated.setEnabled(False)
        btn_row.addWidget(self._btn_mark_calibrated)

        self._btn_clear_calibrated = QPushButton("清除")
        self._btn_clear_calibrated.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear_calibrated.setFixedHeight(22)
        self._btn_clear_calibrated.setToolTip("清除已标定 (写 Index 16 = 0, 走 0xE7)")
        self._btn_clear_calibrated.setStyleSheet(self._calib_action_btn_style())
        self._btn_clear_calibrated.clicked.connect(self._on_clear_calibrated)
        self._btn_clear_calibrated.setEnabled(False)
        btn_row.addWidget(self._btn_clear_calibrated)
        col_layout.addLayout(btn_row)

        outer_layout.addWidget(col)
        self._refresh_calib_flag()

    def _build_task_launcher(self, parent_layout):
        """L1~L7 顶部 QTabWidget + 右侧操作列(开始/中止/查询/自动).

        子项左对齐排列; 点击子项只选中(高亮), 不发送命令;
        命令由右侧「开始」按钮统一发出。
        框体保持最小高度不被压缩, 额外垂直空间全部留给标定结果。
        """
        grp = QGroupBox("标定任务  (选中级别 Tab → 选中子项, 点「开始」启动)")
        # 需求3: 降低总高度(原 170 过高留白多), 固定高度不吃额外空间
        grp.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        grp.setMinimumHeight(140)
        outer = QHBoxLayout(grp)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(8)

        # ============ 左侧: Tab + 子项卡片(左对齐) ============
        self._task_tabs = QTabWidget()
        self._task_tabs.setDocumentMode(True)
        self._task_tabs.setStyleSheet(self._tab_style())

        self._task_buttons = {}  # (cmd, sub_id) -> QPushButton

        for cmd, level_name, submodes in _CALIB_LEVELS:
            page = QWidget()
            page_layout = QHBoxLayout(page)
            page_layout.setContentsMargins(8, 8, 8, 8)
            page_layout.setSpacing(8)
            # 左对齐: 不加前导 stretch, 仅尾部 stretch
            for sub_id, sub_name, sub_desc in submodes:
                btn = QPushButton(f"{sub_name}\n{sub_desc}")
                btn.setToolTip(f"CMD=0x{int(cmd):02X}  submode={sub_id}\n{sub_desc}")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setMinimumWidth(130)
                btn.setMinimumHeight(48)
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

        outer.addWidget(self._task_tabs, 1)

        # ============ 右侧: 操作列 (开始/中止/查询/自动) ============
        self._build_task_actions(outer)
        parent_layout.addWidget(grp)

    def _build_task_actions(self, outer_layout):
        """标定任务右侧操作列: 开始/中止/查询/自动 + 查询周期(ms), 全部小按钮竖排.

        A5: 整列上对齐(不拉伸, 顶部对齐到 Tab 内容区顶部)。
        A4: 自动查询复选框下方增加查询周期(ms) QSpinBox。
        """
        col = QWidget()
        col.setFixedWidth(120)
        col.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        col_layout = QVBoxLayout(col)
        col_layout.setContentsMargins(0, 4, 0, 0)
        col_layout.setSpacing(6)
        col_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # 开始按钮 (绿色, 启动当前选中任务)
        self._btn_start = QPushButton("▶ 开始")
        self._btn_start.setCursor(Qt.CursorShape.PointingHandCursor)
        # 需求3: 开始按钮增高(原 26 → 34), 更醒目易点击
        self._btn_start.setFixedHeight(34)
        self._btn_start.setEnabled(False)  # 未选中任务时禁用
        self._btn_start.setStyleSheet(
            f"QPushButton {{ background-color: {theme.hex('ok')}; color: {theme.hex('ok_text')}; "
            f"border: 1px solid {theme.hex('ok')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 12px; font-weight: bold; }}"
            f"QPushButton:hover {{ opacity: 0.85; }}"
            f"QPushButton:disabled {{ background: {theme.hex('input_bg')}; "
            f"color: {theme.hex('muted')}; border-color: {theme.hex('border')}; }}")
        self._btn_start.clicked.connect(self._on_start_clicked)
        col_layout.addWidget(self._btn_start)

        # 中止按钮 (红色)
        self._btn_abort = QPushButton("■ 中止")
        self._btn_abort.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_abort.setFixedHeight(34)
        self._btn_abort.setStyleSheet(
            f"QPushButton {{ background-color: {theme.hex('danger')}; color: {theme.hex('danger_text')}; "
            f"border: 1px solid {theme.hex('danger')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 12px; font-weight: bold; }}"
            f"QPushButton:hover {{ opacity: 0.85; }}")
        self._btn_abort.clicked.connect(self._on_abort_clicked)
        col_layout.addWidget(self._btn_abort)

        # 查询按钮
        self._btn_query = QPushButton("查询")
        self._btn_query.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_query.setFixedHeight(34)
        self._btn_query.setStyleSheet(
            f"QPushButton {{ background: {theme.hex('input_bg')}; color: {theme.hex('text')}; "
            f"border: 1px solid {theme.hex('border')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 12px; }}"
            f"QPushButton:hover {{ border-color: {theme.hex('accent')}; "
            f"color: {theme.hex('accent')}; }}")
        self._btn_query.clicked.connect(self._on_query_clicked)
        col_layout.addWidget(self._btn_query)

        # 需求1: 移除「自动」复选框与查询周期设置;
        # 改为点击「开始」后固定 500ms 轮询, 直到完成(ACK)或手动中止

        outer_layout.addWidget(col)

    def _tab_style(self) -> str:
        # 需求4: tab 头背景统一为 panel_bg (与外层 QGroupBox 一致),
        # 选中态用 accent 文字 + 顶部 accent 强调条区分, 不再换底色
        return f"""
            QTabWidget::pane {{
                border: 1px solid {theme.hex('border')};
                border-radius: 4px;
                top: -1px;
                background: {theme.hex('panel_bg')};
            }}
            QTabBar::tab {{
                background: {theme.hex('panel_bg')};
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
                background: {theme.hex('panel_bg')};
                color: {theme.hex('accent')};
                border-color: {theme.hex('accent')};
                border-top: 2px solid {theme.hex('accent')};
                font-weight: bold;
            }}
            QTabBar::tab:hover:!selected {{
                color: {theme.hex('text')};
                border-color: {theme.hex('accent')};
            }}
        """

    def _build_results_history_split(self, parent_layout):
        """A1+A3: 标定结果 + 操作历史 用垂直 QSplitter, 历史显示时可拖动调整比例.

        - 历史默认隐藏, 隐藏时标定结果 stretch 占满
        - 历史显示时 splitter 手柄可拖动, 两区皆可缩放
        - 历史开关按钮放到标定结果保存按钮同一行最右(由 attach_results_panel 注入)
        """
        self._results_split = QSplitter(Qt.Orientation.Vertical)
        self._results_split.setChildrenCollapsible(False)
        self._results_split.setHandleWidth(6)
        # 需求4: splitter 手柄用主题键(border/accent), 不再硬编码, 随主题切换
        self._results_split.setStyleSheet(f"""
            QSplitter::handle:vertical {{
                background: {theme.hex('border')};
                margin: 1px 0;
            }}
            QSplitter::handle:vertical:hover {{
                background: {theme.hex('accent')};
            }}
        """)

        # 标定结果容器(占位, attach_results_panel 注入实际 ParamPanel)
        self._build_results_placeholder()

        # 操作历史
        self._build_history()

        self._results_split.addWidget(self._results_container)
        self._results_split.addWidget(self._history_grp)
        # 默认比例: 结果 4 : 历史 1
        self._results_split.setStretchFactor(0, 4)
        self._results_split.setStretchFactor(1, 1)
        self._results_split.setSizes([400, 100])

        parent_layout.addWidget(self._results_split, 1)

    def _build_results_placeholder(self):
        """标定结果区占位 (Task 3 由 attach_results_panel 注入实际 ParamPanel)."""
        self._results_container = QGroupBox("标定结果  (下位机读回值 / 修改 / 保存)")
        v = QVBoxLayout(self._results_container)
        v.setContentsMargins(8, 6, 8, 6)
        self._results_placeholder = QLabel(
            "（Task 3 注入: 连接后自动读回 MotorCalibParam 段 Index 16~42）")
        self._results_placeholder.setStyleSheet(
            f"color: {theme.hex('muted')}; font-size: 12px; padding: 12px;")
        v.addWidget(self._results_placeholder)

    def attach_results_panel(self, panel):
        """Task 3: 注入内嵌 ParamPanel 替换占位 (Main_window 在初始化后调用).

        A3: 同时把「历史:显/隐」开关按钮注入到 ParamPanel 保存按钮所在行最右。
        需求6: 内嵌 ParamPanel 去掉自身边框/margin, 透明融入外层"标定结果"QGroupBox,
              消除"框中框"割裂感。
        """
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

        # A3: 历史开关按钮 — 注入到 ParamPanel 保存按钮同行最右
        self._btn_history_toggle = QPushButton("历史: 隐")
        self._btn_history_toggle.setCheckable(True)
        self._btn_history_toggle.setChecked(False)
        self._btn_history_toggle.setFixedHeight(24)
        self._btn_history_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_history_toggle.setToolTip("显示/隐藏 操作历史 (显示后可拖动手柄调整与标定结果的高度比例)")
        self._btn_history_toggle.setStyleSheet(
            f"QPushButton {{ background: {theme.hex('input_bg')}; color: {theme.hex('text')}; "
            f"border: 1px solid {theme.hex('border')}; border-radius: 3px; "
            f"padding: 0 10px; font-size: 12px; }}"
            f"QPushButton:hover {{ border-color: {theme.hex('accent')}; "
            f"color: {theme.hex('accent')}; }}"
            f"QPushButton:checked {{ background: {theme.hex('accent')}; color: {theme.hex('card_bottom')}; "
            f"border-color: {theme.hex('accent')}; font-weight: bold; }}")
        self._btn_history_toggle.toggled.connect(self._on_history_toggle)
        panel.add_footer_widget(self._btn_history_toggle)
        # 恢复持久化的历史显隐态
        if getattr(self, "_history_visible", False):
            self._btn_history_toggle.setChecked(True)

    def _build_history(self):
        """操作历史: 时间戳 + TX/ACK/NACK 文本, 滚动到最新.

        默认隐藏(让标定结果表格占满垂直空间); 由「历史:显/隐」开关控制。
        隐藏时仍记录日志, 重新显示后可见。
        历史显示时, 与标定结果用 QSplitter 分隔, 可拖动手柄调整高度比例。
        """
        self._history_grp = QGroupBox("操作历史")
        v = QVBoxLayout(self._history_grp)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        self._history_view = QTextEdit()
        self._history_view.setReadOnly(True)
        # 需求6: 输出框背景统一为 panel_bg (与外层 QGroupBox 一致), 仅用 border 分隔
        self._history_view.setStyleSheet(
            f"background: {theme.hex('panel_bg')}; color: {theme.hex('text')}; "
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

        # 默认隐藏: 让标定结果表格占满剩余空间
        self._history_grp.setVisible(False)
        self._history_visible = False

    def _on_history_toggle(self, on: bool):
        """A3: 保存按钮同行右侧的「历史:显/隐」开关."""
        self._btn_history_toggle.setText("历史: 显" if on else "历史: 隐")
        self.set_history_visible(on)

    def set_history_visible(self, visible: bool):
        """显示/隐藏操作历史区(由开关按钮调用)."""
        self._history_visible = bool(visible)
        self._history_grp.setVisible(self._history_visible)
        # splitter 在历史隐藏时, 让标定结果独占空间
        if hasattr(self, "_results_split"):
            self._results_split.setStretchFactor(0, 4 if not self._history_visible else 4)
            self._results_split.setStretchFactor(1, 1 if self._history_visible else 0)

    def is_history_visible(self) -> bool:
        return self._history_visible

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
        """点击子项卡片: 只选中(高亮), 不发送命令; 命令由「开始」按钮统一发出."""
        self._set_active_task(cmd, sub_id, level_name, sub_name, sub_desc)
        self._add_history(f"[选中] {level_name} > {sub_name}  CMD=0x{cmd:02X} submode={sub_id}")

    def _on_start_clicked(self):
        """点「开始」按钮: 发送当前选中任务的标定指令."""
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接, 无法启动")
            return
        if self._active_task_key is None:
            self._add_history("[未选中] 请先选中一个子项")
            return
        cmd, sub_id = self._active_task_key
        level_name, sub_name, _ = self._lookup_task(cmd, sub_id)
        if level_name is None:
            return
        self._add_history(
            f"[TX] 启动 {cmd_name(cmd)} submode={sub_id} ({level_name}>{sub_name})")
        self.send_command.emit(int(cmd), {"submode": int(sub_id)})
        # 需求1: 开始后固定 500ms 轮询标定进度, 直到收到完成(ACK)或手动中止
        if self._link_active:
            self._poll_timer.start()

    def _set_active_task(self, cmd: int, sub_id: int,
                         level_name: str, sub_name: str, sub_desc: str):
        """更新当前选中任务显示与卡片高亮."""
        self._active_task_key = (int(cmd), int(sub_id))
        self._active_task_text = (
            f"当前选中: {level_name} > {sub_name}  "
            f"CMD=0x{cmd:02X}, submode={sub_id}  ·  {sub_desc}")
        self._lbl_active_task.setText(self._active_task_text)
        self._refresh_task_buttons_style()
        # 「开始」按钮可用态: 已选中即可(未连接时点击会提示)
        self._btn_start.setEnabled(True)
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
        # Task 5: 标记/清除按钮可用态跟随连接
        self._refresh_calib_flag()

    def update_state(self, top_fsm: int, run_state: int, ctrl_mode: int, enable: int):
        """接收主窗口转发的状态机更新, 用于判断是否处于 CALIB 态."""
        prev = self._current_top_fsm
        self._current_top_fsm = top_fsm
        in_calib = top_fsm == int(TopFsm.CALIB)
        prev_in_calib = prev == int(TopFsm.CALIB) if prev is not None else False
        # 进入 CALIB 态: 启动周期查询
        if in_calib and not prev_in_calib:
            self._calib_running = True
            if self._link_active:
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
            # 需求1+2: 标定完成 -> 只请求本次标定对应的结果参数(非全部),
            # 收到的值加粗彩色显示(value_hot), 0xEA 保存 ACK 后 clear_fresh 恢复
            if self._config_panel is not None and self._link_active:
                param_ids = self._result_param_ids_for_current_task()
                if param_ids:
                    names = self._param_names(param_ids)
                    self._add_history(
                        f"[TX] 主动读回本次标定结果 ({len(param_ids)} 项: {names})")
                    self._config_panel.mark_fresh(param_ids)  # 只标记本次产出的参数
                    self._config_panel.read_params.emit(list(param_ids))
                else:
                    self._add_history(
                        "[TX] 本次标定子项无 motor_info 字段产出 (固件内部表格), 跳过读回")
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
                if self._link_active:
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

    def _result_param_ids_for_current_task(self):
        """需求1: 查 _CALIB_RESULT_MAP 返回当前选中任务对应的标定结果 param_id 列表.

        无选中任务或映射缺失时返回空 list(等价于不读回)。
        """
        if self._active_task_key is None:
            return []
        cmd, sub_id = self._active_task_key
        return list(_CALIB_RESULT_MAP.get((int(cmd), int(sub_id)), []))

    def _param_names(self, param_ids):
        """需求1: 把 param_id 列表转成 code_name 简短字符串(用于历史日志)."""
        if self._config_panel is None:
            return ""
        specs = self._config_panel._param_specs
        names = []
        for pid in param_ids:
            spec = specs.get(int(pid))
            names.append(getattr(spec, 'code_name', f'#{pid}') if spec else f'#{pid}')
        return ", ".join(names)

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

    def _on_poll_tick(self):
        # 需求1: 定时器在跑(开始后启动, 完成/中止后停止)且连接就发查询进度
        if self._link_active:
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

    # ==================== Task 5: 已标定徽章 ====================
    def set_calibrated_value(self, text):
        """主窗口在收到 param_id==16 (is_calibrated) 读回值时调用.

        容错: "1"/"1.0"/1.0 -> 已标定; "0"/"0.0"/0.0 -> 未标定;
              空串/非数字 -> 未读取.
        """
        try:
            v = str(text).strip()
        except Exception:
            v = ""
        if v in ("1", "1.0"):
            self._calib_flag_text = "已标定"
        elif v in ("0", "0.0"):
            self._calib_flag_text = "未标定"
        else:
            try:
                f = float(v)
                if f == 1.0:
                    self._calib_flag_text = "已标定"
                elif f == 0.0:
                    self._calib_flag_text = "未标定"
                else:
                    self._calib_flag_text = "未读取"
            except (ValueError, TypeError):
                self._calib_flag_text = "未读取"
        self._refresh_calib_flag()

    def _refresh_calib_flag(self):
        """刷新徽章文本/样式 + 按钮可用态 (跟随 _link_active)."""
        self._lbl_calib_flag.setText(self._calib_flag_text)
        self._lbl_calib_flag.setStyleSheet(self._calib_flag_style())
        enabled = self._link_active
        self._btn_mark_calibrated.setEnabled(enabled)
        self._btn_clear_calibrated.setEnabled(enabled)

    def _on_mark_calibrated(self):
        """点 "标记": 乐观更新为已标定 + 发 write_param(16, "1") 走 0xE7 通道."""
        if not self._link_active:
            return
        self._calib_flag_text = "已标定"
        self._refresh_calib_flag()
        if self._config_panel is not None:
            self._config_panel.write_param.emit(16, "1")
        self._add_history("[TX] 标记已标定 (write is_calibrated=1, Index 16, 0xE7)")

    def _on_clear_calibrated(self):
        """点 "清除": 乐观更新为未标定 + 发 write_param(16, "0")."""
        if not self._link_active:
            return
        self._calib_flag_text = "未标定"
        self._refresh_calib_flag()
        if self._config_panel is not None:
            self._config_panel.write_param.emit(16, "0")
        self._add_history("[TX] 清除已标定 (write is_calibrated=0, Index 16, 0xE7)")

    def _calib_flag_style(self) -> str:
        """徽章样式: 已标定绿 / 未标定红 / 未读取灰."""
        text = self._calib_flag_text
        if "已标定" in text:
            bg = theme.hex('accent')
            fg = theme.hex('card_bottom')
        elif "未标定" in text:
            bg = theme.hex('danger')
            fg = theme.hex('danger_text')
        else:  # 未读取
            bg = theme.hex('input_bg')
            fg = theme.hex('muted')
        return (f"QLabel {{ background: {bg}; color: {fg}; "
                f"border: 1px solid {theme.hex('border')}; border-radius: 3px; "
                f"padding: 1px 8px; font-size: 11px; font-weight: bold; }}")

    def _calib_action_btn_style(self) -> str:
        """标记/清除按钮样式 (与查询按钮同款, 主题色驱动)."""
        return (
            f"QPushButton {{ background: {theme.hex('input_bg')}; color: {theme.hex('text')}; "
            f"border: 1px solid {theme.hex('border')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 11px; }}"
            f"QPushButton:hover {{ border-color: {theme.hex('accent')}; "
            f"color: {theme.hex('accent')}; }}"
            f"QPushButton:disabled {{ color: {theme.hex('muted')}; "
            f"border-color: {theme.hex('border')}; background: {theme.hex('input_bg')}; }}"
        )

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
        # 需求4: 容器背景统一为 panel_bg
        self._status_card.setStyleSheet(f"""
            QFrame {{
                background: {theme.hex('panel_bg')};
                border: 1px solid {theme.hex('border')};
                border-radius: 4px;
            }}
        """)
        # 需求4: splitter 手柄随主题刷新(此前缺失)
        self._results_split.setStyleSheet(f"""
            QSplitter::handle:vertical {{
                background: {theme.hex('border')};
                margin: 1px 0;
            }}
            QSplitter::handle:vertical:hover {{
                background: {theme.hex('accent')};
            }}
        """)
        self._task_tabs.setStyleSheet(self._tab_style())
        self._lbl_active_task.setStyleSheet(
            f"color: {theme.hex('text')}; font-size: 11px; border:none; "
            f"background: {theme.hex('panel_bg')}; "
            f"border-left: 2px solid {theme.hex('accent')}; "
            f"padding: 2px 8px; border-radius: 2px;")
        # 操作列按钮(开始/中止/查询)
        self._btn_start.setStyleSheet(
            f"QPushButton {{ background-color: {theme.hex('ok')}; color: {theme.hex('ok_text')}; "
            f"border: 1px solid {theme.hex('ok')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 12px; font-weight: bold; }}"
            f"QPushButton:hover {{ opacity: 0.85; }}"
            f"QPushButton:disabled {{ background: {theme.hex('input_bg')}; "
            f"color: {theme.hex('muted')}; border-color: {theme.hex('border')}; }}")
        self._btn_abort.setStyleSheet(
            f"QPushButton {{ background-color: {theme.hex('danger')}; color: {theme.hex('danger_text')}; "
            f"border: 1px solid {theme.hex('danger')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 12px; font-weight: bold; }}"
            f"QPushButton:hover {{ opacity: 0.85; }}")
        self._btn_query.setStyleSheet(
            f"QPushButton {{ background: {theme.hex('input_bg')}; color: {theme.hex('text')}; "
            f"border: 1px solid {theme.hex('border')}; border-radius: 3px; "
            f"padding: 1px 8px; font-size: 12px; }}"
            f"QPushButton:hover {{ border-color: {theme.hex('accent')}; "
            f"color: {theme.hex('accent')}; }}")
        # 历史开关(注入到保存按钮行的)主题刷新
        if hasattr(self, "_btn_history_toggle"):
            self._btn_history_toggle.setStyleSheet(
                f"QPushButton {{ background: {theme.hex('input_bg')}; color: {theme.hex('text')}; "
                f"border: 1px solid {theme.hex('border')}; border-radius: 3px; "
                f"padding: 0 10px; font-size: 12px; }}"
                f"QPushButton:hover {{ border-color: {theme.hex('accent')}; "
                f"color: {theme.hex('accent')}; }}"
                f"QPushButton:checked {{ background: {theme.hex('accent')}; color: {theme.hex('card_bottom')}; "
                f"border-color: {theme.hex('accent')}; font-weight: bold; }}")
        self._history_view.setStyleSheet(
            f"background: {theme.hex('panel_bg')}; color: {theme.hex('text')}; "
            f"font-family: Consolas, 'Microsoft YaHei', monospace; font-size: 12px; "
            f"border: 1px solid {theme.hex('border')};")
        # Task 5: 徽章 + 标记/清除按钮
        self._lbl_calib_flag.setStyleSheet(self._calib_flag_style())
        self._btn_mark_calibrated.setStyleSheet(self._calib_action_btn_style())
        self._btn_clear_calibrated.setStyleSheet(self._calib_action_btn_style())
        self._refresh_task_buttons_style()
        if self._config_panel is not None:
            fn = getattr(self._config_panel, "apply_theme", None)
            if callable(fn):
                fn()
        self._refresh_status()

    # ==================== 配置持久化 ====================
    def get_opts(self) -> dict:
        """收集可持久化的 UI 配置: 当前 Tab + 当前选中任务 + 历史显隐 + 内嵌面板列宽。"""
        opts = {}
        try:
            opts["task_tab_index"] = int(self._task_tabs.currentIndex())
            opts["history_visible"] = bool(self._history_visible)
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
        if "task_tab_index" in opts:
            try:
                idx = int(opts["task_tab_index"])
                if 0 <= idx < self._task_tabs.count():
                    self._task_tabs.setCurrentIndex(idx)
            except Exception:
                pass
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
