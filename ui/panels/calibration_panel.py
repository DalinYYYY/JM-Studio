"""电机标定面板

封装标定指令(0x90~0x98) 与标定结果读写(0xE6~0xEA)。

UI 布局 (紧凑, 状态卡仅 2 行 ~70px):
  1. 紧凑状态卡:
     - 主信息行(单行, 横向排列): [●状态点] [状态文本] · [最近操作] [弹簧]
       [已标定徽章][标记][清除] [查询][中止][自动查询]
     - 当前选中任务行(小字, 带左侧高亮条): 级别>子项 CMD=0xXX submode=N · 描述
  2. 标定任务: 顶部 QTabWidget, L1~L7 每级一个 Tab, 选中 Tab 才显示该级子项卡片,
     点击子项卡片即启动该标定
  3. 标定结果: 内嵌 ParamPanel(source="motor_config", row_plan=CALIB_TABLE_PLAN),
     按标定流程 L1~L7 排序展示(首列"标定项"标注来源),
     支持单读/单写/全读/全写/保存到Flash(0xEA)
  4. 操作历史: 时间戳 + TX/ACK/NACK 文本

数据来源:
  - registry "校准" 类命令 (joint_motor_command_list.csv 第 58~66 条)
  - motor_info.csv 标定相关段(MotorCalibParam + ControlParam PID 字段, 见 CALIB_TABLE_PLAN)
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

from jmproto import (
    JmCmd, JmErr, TopFsm, CalibState,
    cmd_name, err_name_cn, top_fsm_name,
    calib_state_name, calib_fail_reason_name, calib_level_submode_name,
)
from ui.theme import theme


# ==================== L1~L7 标定级别定义 ====================
# 每条: (cmd, 级别名称, [(sub_id, method, 子模式名, 描述), ...])
# L2 子模式按固件实际实现拆分(submode 3-6 分别对应 R/Ld/Lq/flux, 各有独立测试方法)
# method=0 表示该子项无方案参数; L5>1 齿槽三个方案卡片共用 submode=1,
# 方案经 0x94 payload[1] 传给固件(来源为运行参数 ID86，使能仍为配置 ID184)
_CALIB_LEVELS = [
    (JmCmd.CALIB_LEVEL1, "L1 驱动硬件底层", [
        (1, 0, "ADC偏置",     "电流/电压采样通道零点偏置校正"),
        (2, 0, "ADC增益",     "电流/电压采样通道增益校正"),
        (3, 0, "电流传感器",  "相电流传感器线性度与零漂"),
        (4, 0, "温度传感器",  "FET/电机 NTC 温度采样校正"),
        (5, 0, "母线电压",    "母线电压分压比与零点校正"),
        (6, 0, "死区特性",    "逆变器死区时间与管压降补偿"),
    ]),
    (JmCmd.CALIB_LEVEL2, "L2 电机电气身份", [
        (1, 0, "相序",          "U/V/W 相序方向辨识"),
        (2, 0, "极对数",        "电机极对数自动辨识"),
        (3, 0, "R 相电阻",      "相电阻辨识 (DC法)"),
        (4, 0, "Ld d轴电感",    "d轴电感辨识 (阶跃响应)"),
        (5, 0, "Lq q轴电感",    "q轴电感辨识 (阶跃响应)"),
        (6, 0, "flux 磁链",     "磁链辨识(反电势法，需零位正确、空载稳速)"),
    ]),
    (JmCmd.CALIB_LEVEL3, "L3 编码器校准", [
        (1, 0, "零位",          "编码器电角度零点对齐"),
        (2, 0, "方向",          "编码器计数方向校验"),
        (3, 0, "线性度",        "编码器非线性度扫描"),
        (4, 0, "正余弦/旋变",   "正余弦/旋变解码参数校准"),
        (5, 0, "多圈零点",      "多圈计数器零点校准"),
    ]),
    (JmCmd.CALIB_LEVEL4, "L4 转矩基础", [
        (1, 0, "力矩常数 Kt",   "磁链推算 Kt=1.5×极对数×磁链；无需转动"),
    ]),
    (JmCmd.CALIB_LEVEL5, "L5 非线性补偿", [
        # 仅齿槽三方案按钮单行短标题(文字过长), 其余子项保持 标题+描述 两行
        (1, 3, "齿槽 速度环匀速", "方案3: 临时理论参数速度闭环1rad/s; 需有效R/L/J/Kt; 正反各2圈; 通常约35s"),
        (1, 1, "齿槽 位置爬行",   "方案1: 当前三环参数, 位置轨迹0.25rad/s正反爬行; 需三环稳定; 通常约2~3min"),
        (1, 2, "齿槽 静态逐点",   "方案2: 当前三环参数, 正反逐点保持1008个bin中心; 稳定50ms后采样100ms; 最长15min"),
        (2, 0, "摩擦",          "摩擦模型(库仑+粘性)辨识"),
        (3, 0, "死区补偿",      "逆变器死区非线性补偿"),
        (4, 0, "磁饱和",        "dq 轴磁饱和电感曲线辨识"),
    ]),
    (JmCmd.CALIB_LEVEL6, "L6 负载系统级", [
        (1, 0, "惯量",          "负载转动惯量辨识"),
        (2, 0, "阻尼",          "负载粘性阻尼系数辨识"),
        (3, 0, "回程间隙",      "减速器回程间隙测量"),
        (4, 0, "PID 自整定",    "速度/位置环 PID 自动整定"),
    ]),
    (JmCmd.CALIB_LEVEL7, "L7 自动化集成", [
        (1, 0, "一键全自动",    "依次执行 L1~L6 全套标定"),
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
    (0x94, 1): [184, 185],           # 持久配置仅使能/增益；诊断走运行时参数85~89
    (0x94, 2): [26, 27],            # 摩擦 -> friction_coulomb, friction_viscous
    (0x94, 3): [],                  # 死区补偿 -> 固件内部曲线
    (0x94, 4): [],                  # 磁饱和 -> 固件内部曲线
    # L6 负载系统级 (0x95)
    (0x95, 1): [25],                # 惯量 -> rotor_inertia
    (0x95, 2): [27],                # 阻尼 -> friction_viscous (与 L5>2 共享字段)
    (0x95, 3): [28, 29],            # 回程间隙 -> gear_ratio, gear_efficiency
    (0x95, 4): [72, 73, 74, 80, 76],  # PID自整定 -> kp_s/ki_s/speed_integral_limit/kp_p/aff
                                      #   (固件 ring_mask=0x06 速度+位置环, 电流环不重算;
                                      #    aff=J/Kt 由 calib_level6_system 一并写入)
    # L7 自动化集成 (0x96): 全部产出参数 + is_calibrated 总标志位
    (0x96, 1): [16, 17, 19, 20, 21, 22, 23, 24, 25, 26, 27, 36, 37, 38, 40],
}


# ==================== 标定结果表行计划 ====================
# 标定页"标定结果"表按标定流程 L1~L7 顺序展示(不按参数 ID 排序):
#   - 每项 = (param_id, 标定项标注); 标注文本显示在表格最前"标定项"列
#   - 与标定无关的纯配置参数不展示(motor_type/enc_type/enc_lines/pwm_freq_hz/
#     current_lim/peak_current/max_speed)
#   - 标定产出但不在 MotorCalibParam 段的参数(L6.4 自整定写 ControlParam)也纳入
#   - 16 is_calibrated 为 L7 总标志位; 30/31 为标定输入配置(标定时使用)
# 参考: motor_info.csv + calib_level6_system.c/motor_pid_autotune.c 写入字段
CALIB_TABLE_PLAN = [
    # L1 驱动硬件底层 (0x90)
    (41, "L1>3 电流传感器"),          # shunt_resistance
    (42, "L1>3 电流传感器"),          # current_amp_gain
    (40, "L1>6 死区特性"),            # dead_time_ns
    # L2 电机电气身份 (0x91)
    (19, "L2>1 相序"),                # direction
    (17, "L2>2 极对数"),              # pole_pairs
    (20, "L2>3 R相电阻"),             # phase_resistance
    (21, "L2>4 Ld电感"),              # phase_inductance_d
    (22, "L2>5 Lq电感"),              # phase_inductance_q
    (23, "L2>6 flux磁链"),            # flux_linkage
    # L3 编码器校准 (0x92)
    (38, "L3>1 零位"),                # elec_angle_bias
    (37, "L3>1 零位"),                # enc_offset
    (36, "L3>2 方向"),                # enc_direction
    # L4 转矩基础 (0x93)
    (24, "L4>1 力矩常数Kt"),          # torque_constant
    # L5 非线性补偿 (0x94)
    (184, "L5>1 齿槽使能"),
    (185, "L5>1 齿槽增益"),          # cogging_comp_gain
    (26, "L5>2 摩擦"),                # friction_coulomb
    (27, "L5>2 摩擦; L6>2 阻尼"),     # friction_viscous (两标定共享)
    # L6 负载系统级 (0x95)
    (25, "L6>1 惯量"),                # rotor_inertia
    (28, "L6>3 回程间隙"),            # gear_ratio
    (29, "L6>3 回程间隙"),            # gear_efficiency
    (72, "L6>4 PID自整定"),           # kp_s
    (73, "L6>4 PID自整定"),           # ki_s
    (74, "L6>4 PID自整定"),           # speed_integral_limit
    (80, "L6>4 PID自整定"),           # kp_p
    (76, "L6>4 PID自整定"),           # aff
    # L7 自动化集成 (0x96)
    (16, "L7>1 一键全自动"),          # is_calibrated 总标志位
    # 标定输入配置 (标定时使用, 非产出)
    (30, "L2 标定输入"),              # calibration_current
    (31, "L2>3 R标定输入"),           # resistance_calib_max_voltage
]


class CalibrationPanel(QWidget):
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
        super().__init__(parent)
        self._panel_name = "电机标定"
        self._link_active = False
        self._current_top_fsm = None
        self._calib_running = False     # 本地推测: 标定是否进行中
        self._cogging_wait_flash = False
        self._last_op = ""              # 最近一次操作结果文本
        self._history = deque(maxlen=self._MAX_HISTORY)
        # 当前选中任务: (cmd, sub_id, level_name, sub_name, sub_desc)
        self._active_task_key = None
        self._active_task_text = "当前选中: —"
        # Task 3 注入的内嵌标定结果面板
        self._config_panel = None
        self._runtime_panel = None
        self._runtime_group = None
        self._runtime_read_key = None
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
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        self._build_status_card(layout)      # 状态卡(含已标定独占列)
        self._build_task_launcher(layout)    # L1~L7 顶部 QTabWidget + 右侧操作列(固定高度)
        # 标定结果 + 操作历史 用 QSplitter: 历史显示时可拖动调整与结果的垂直比例
        self._build_results_history_split(layout)

    def _build_status_card(self, parent_layout):
        """紧凑状态卡: 左侧主信息(2行) + 右侧已标定独占列(徽章+标记+清除), 总高 ~70px."""
        self._status_card = QFrame()
        self._status_card.setFrameShape(QFrame.Shape.StyledPanel)
        # 卡片底色 panel_bg 与 Tab 底 app_bg 区分, 仅保留 border 分隔
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
        # 裸 QWidget 会被全局 QSS 的 QWidget{background:app_bg} 命中而发黑, 置透明跟随状态卡底色
        left_wrap.setStyleSheet("background: transparent;")
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
        """右侧只读标定状态徽章。

        徽章文本由 Index 16 (is_calibrated) 读回值驱动:
          - "1"  -> "已标定" (绿/accent)
          - "0"  -> "未标定" (红/danger)
          - 其他 -> "未读取" (灰/muted)

        完成标定后由固件更新，清除使用专用重新标定命令。
        """
        col = QWidget()
        col.setStyleSheet("background: transparent;")
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
        self._btn_mark_calibrated.setToolTip("标定状态由固件维护，完成全自动标定后更新")
        self._btn_mark_calibrated.setStyleSheet(self._calib_action_btn_style())
        self._btn_mark_calibrated.clicked.connect(self._on_mark_calibrated)
        self._btn_mark_calibrated.setEnabled(False)
        btn_row.addWidget(self._btn_mark_calibrated)
        self._btn_mark_calibrated.setVisible(False)

        self._btn_clear_calibrated = QPushButton("清除")
        self._btn_clear_calibrated.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear_calibrated.setFixedHeight(22)
        self._btn_clear_calibrated.setToolTip("清除标定请使用电机配置页的重新标定功能")
        self._btn_clear_calibrated.setStyleSheet(self._calib_action_btn_style())
        self._btn_clear_calibrated.clicked.connect(self._on_clear_calibrated)
        self._btn_clear_calibrated.setEnabled(False)
        btn_row.addWidget(self._btn_clear_calibrated)
        self._btn_clear_calibrated.setVisible(False)
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

        self._task_buttons = {}  # (cmd, sub_id, method) -> QPushButton

        for cmd, level_name, submodes in _CALIB_LEVELS:
            page = QWidget()
            page.setStyleSheet("background: transparent;")
            page_layout = QHBoxLayout(page)
            page_layout.setContentsMargins(8, 8, 8, 8)
            page_layout.setSpacing(8)
            # 左对齐: 不加前导 stretch, 仅尾部 stretch
            for sub_id, method, sub_name, sub_desc in submodes:
                # 仅齿槽三方案文字过长: 单行短标题(详情看tooltip); 其余保持 标题+描述 两行
                cogging = (int(cmd) == int(JmCmd.CALIB_LEVEL5)
                           and int(sub_id) == 1 and int(method) != 0)
                btn = QPushButton(sub_name if cogging else f"{sub_name}\n{sub_desc}")
                method_txt = f"  method={method}" if method else ""
                btn.setToolTip(f"CMD=0x{int(cmd):02X}  submode={sub_id}{method_txt}\n{sub_desc}")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setMinimumWidth(130)
                btn.setMinimumHeight(48)
                btn.setStyleSheet(self._task_btn_style(active=False))
                btn.clicked.connect(
                    lambda _=False, c=int(cmd), s=int(sub_id), m=int(method),
                           ln=level_name, sn=sub_name, sd=sub_desc:
                    self._on_task_clicked(c, s, m, ln, sn, sd))
                self._task_buttons[(int(cmd), int(sub_id), int(method))] = btn
                page_layout.addWidget(btn)
            page_layout.addStretch()
            tab_title = f"{level_name}  0x{int(cmd):02X}"
            self._task_tabs.addTab(page, tab_title)

        self._task_tabs.currentChanged.connect(lambda _: self.refresh_runtime_panel())
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
        col.setStyleSheet("background: transparent;")
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
        lay.addWidget(panel, 1)

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

    def attach_runtime_panel(self, panel):
        """齿槽只读诊断按选中项显示，按内容高度贴在配置表下方。"""
        if self._runtime_panel is not None:
            return
        self._runtime_panel = panel
        group = QGroupBox("齿槽诊断（运行时，只读）", self._results_container)
        self._runtime_group = group
        group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(4, 6, 4, 4)
        layout.addWidget(panel)
        self._results_container.layout().addWidget(group, 0, Qt.AlignmentFlag.AlignBottom)
        self.refresh_runtime_panel()

    def refresh_runtime_panel(self, *, force_read=False):
        """仅选中 L5 齿槽三方案时显示/读取诊断；离线或标定页隐藏时不发读取。"""
        if self._runtime_panel is None:
            return
        key = self._active_task_key
        tab = self._task_tabs.currentIndex()
        selected = (key is not None and key[0] == int(JmCmd.CALIB_LEVEL5)
                    and key[1] == 1 and key[2] in (1, 2, 3)
                    and tab >= 0 and int(_CALIB_LEVELS[tab][0]) == key[0])
        self._runtime_group.setVisible(selected)
        if not selected:
            self._runtime_read_key = None
        elif (self._link_active and self.isVisible()
              and (force_read or self._runtime_read_key != key)):
            self._runtime_read_key = key
            self._runtime_panel.read_all()

    def _refresh_task_buttons_style(self):
        """刷新所有任务卡片样式 (按当前选中态)."""
        for key, btn in self._task_buttons.items():
            btn.setStyleSheet(self._task_btn_style(active=(key == self._active_task_key)))

    # ==================== 任务点击 / 选中态 ====================
    def _on_task_clicked(self, cmd: int, sub_id: int, method: int,
                         level_name: str, sub_name: str, sub_desc: str):
        """点击子项卡片: 只选中(高亮), 不发送命令; 命令由「开始」按钮统一发出."""
        self._set_active_task(cmd, sub_id, method, level_name, sub_name, sub_desc)
        method_txt = f" method={method}" if method else ""
        self._add_history(
            f"[选中] {level_name} > {sub_name}  CMD=0x{cmd:02X} submode={sub_id}{method_txt}")

    def _on_start_clicked(self):
        """点「开始」按钮: 发送当前选中任务的标定指令."""
        if not self._link_active:
            self._add_history("[未连接] 请先连接电机")
            self._set_last_op("未连接, 无法启动")
            return
        if self._active_task_key is None:
            self._add_history("[未选中] 请先选中一个子项")
            return
        if self._cogging_wait_flash:
            self._set_last_op("齿槽表待落盘，请先退出到待机")
            return
        cmd, sub_id, method = self._active_task_key
        level_name, sub_name, _ = self._lookup_task(cmd, sub_id, method)
        if level_name is None:
            return
        method_txt = f" method={method}" if method else ""
        self._add_history(
            f"[TX] 启动 {cmd_name(cmd)} submode={sub_id}{method_txt} ({level_name}>{sub_name})")
        values = {"submode": int(sub_id)}
        if method:
            values["method"] = int(method)  # 0x94 payload[1]: 标定方案
        self.send_command.emit(int(cmd), values)
        # 需求1: 开始后固定 500ms 轮询标定进度, 直到收到完成(ACK)或手动中止
        if self._link_active:
            self._poll_timer.start()

    def _set_active_task(self, cmd: int, sub_id: int, method: int,
                         level_name: str, sub_name: str, sub_desc: str):
        """更新当前选中任务显示与卡片高亮."""
        self._active_task_key = (int(cmd), int(sub_id), int(method))
        method_txt = f", method={method}" if method else ""
        self._active_task_text = (
            f"当前选中: {level_name} > {sub_name}  "
            f"CMD=0x{cmd:02X}, submode={sub_id}{method_txt}  ·  {sub_desc}")
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
        self.refresh_runtime_panel()

    def _lookup_task(self, cmd: int, sub_id: int, method: int = 0):
        """按 (cmd, sub_id, method) 反查 (level_name, sub_name, sub_desc), 找不到返回 (None,...).

        齿槽三方案共用 submode=1, method 才是区分键; 其余子项 method=0。
        """
        for c, level_name, submodes in _CALIB_LEVELS:
            if int(c) == int(cmd):
                for sid, m, sn, sd in submodes:
                    if int(sid) == int(sub_id) and int(m) == int(method):
                        return level_name, sn, sd
        return None, None, None

    # ==================== 状态接收 (由主窗口调用) ====================
    def set_link_active(self, active: bool):
        self._link_active = bool(active)
        if not active:
            self._calib_running = False
            self._cogging_wait_flash = False
            self._poll_timer.stop()
            self._runtime_read_key = None
        self.refresh_runtime_panel(force_read=bool(active))
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
            if not self._cogging_wait_flash:
                self._poll_timer.stop()
            self._add_history(f"[退出标定态] top_fsm={top_fsm_name(top_fsm)}")
        self._refresh_status()

    def on_ack(self, cmd: int):
        """标定相关命令 ACK (主窗口在 _on_ack 中调用, 内部按 cmd 过滤).

        注意: 0x97 CALIB_QUERY 不再走 ACK 通道, 改由 on_calib_status 处理详细状态。
        """
        if not self._is_calib_cmd(cmd):
            return
        if cmd == JmCmd.CALIB_ABORT:
            self._calib_running = False
            self._poll_timer.stop()
            self._set_last_op("已中止标定")
            self._add_history(f"[ACK] {cmd_name(cmd)}(0x{cmd:02X}) 已中止")
        else:
            # 启动类(0x90~0x96) ACK: 表示已进入标定态(由 state_updated 同步)
            if self._active_task_key is not None:
                cmd2, sub_id, method = self._active_task_key
                level_name, sub_name, _ = self._lookup_task(cmd2, sub_id, method)
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

    def on_calib_status(self, status: dict):
        """0x97 CALIB_QUERY 详细状态应答槽 (主窗口连到 calib_status_received 信号).

        status 由 jmproto.parse_calib_status 返回, 含:
          state/fail_reason/progress/level/submode/step/step_total (原始数值)
          state_cn/fail_reason_cn/target_cn (中文描述)

        - DONE:    标定完成 -> 读回本次结果参数, 停止轮询
        - RUNNING: 显示进度/当前子项 -> 保持轮询
        - FAILED:  显示失败原因 -> 停止轮询
        - IDLE:    未在标定 -> 停止轮询
        """
        state = int(status.get('state', 0))
        fail_reason = int(status.get('fail_reason', 0))
        progress = int(status.get('progress', 0))
        level = int(status.get('level', 0))
        submode = int(status.get('submode', 0))
        step = int(status.get('step', 0))
        step_total = int(status.get('step_total', 0))
        state_cn = status.get('state_cn', '')
        fail_cn = status.get('fail_reason_cn', '')
        target_cn = status.get('target_cn', '')
        detail = int(status.get('detail', status.get('reserved', 0)))
        detail_cn = status.get('detail_cn', '')
        detail_info = f" detail={detail_cn}(0x{detail:02X})" if detail else ""

        if state == int(CalibState.DONE):
            self._calib_running = False
            self._poll_timer.stop()
            self._set_last_op(f"标定完成 ({target_cn})")
            self._add_history(
                f"[ACK] 0x97 标定完成 level={level} submode={submode} ({target_cn})")
            if level == 5 and submode == 1 and self._runtime_panel is not None:
                self._cogging_wait_flash = True
                self._set_last_op("齿槽采集完成，退出到待机后写入 Flash")
                if self._link_active:
                    self._poll_timer.start()
                    self._runtime_panel.mark_fresh([85, 86, 87, 88, 89])
                    self._runtime_panel.read_params.emit([85, 86, 87, 88, 89])
            # 标定完成 -> 主动读回本次产出的结果参数(加粗彩色显示)
            if self._config_panel is not None and self._link_active:
                param_ids = self._result_param_ids_for_current_task()
                if param_ids:
                    names = self._param_names(param_ids)
                    self._add_history(
                        f"[TX] 主动读回本次标定结果 ({len(param_ids)} 项: {names})")
                    self._config_panel.mark_fresh(param_ids)
                    self._config_panel.read_params.emit(list(param_ids))
                else:
                    self._add_history(
                        "[TX] 本次标定子项无 motor_info 字段产出 (固件内部表格), 跳过读回")
        elif state == int(CalibState.RUNNING):
            self._cogging_wait_flash = False
            self._calib_running = True
            if self._link_active and not self._poll_timer.isActive():
                self._poll_timer.start()
            # L7 全自动时显示 "step/step_total", 单步标定时不显示 step(step_total=0)
            if step_total > 0:
                step_info = f" step={step}/{step_total}"
            else:
                step_info = ""
            self._set_last_op(
                f"{state_cn} {target_cn} 进度={progress}%{step_info}{detail_info}")
            self._add_history(
                f"[进度] 0x97 {state_cn} {target_cn} 进度={progress}% "
                f"fail_reason={fail_cn}{step_info}{detail_info}")
        elif state == int(CalibState.FAILED):
            self._cogging_wait_flash = False
            self._calib_running = False
            self._poll_timer.stop()
            self._set_last_op(f"标定失败: {detail_cn or fail_cn} ({target_cn})")
            self._add_history(
                f"[失败] 0x97 {state_cn} {target_cn} fail_reason={fail_cn}(0x{fail_reason:02X}) "
                f"step={step}/{step_total if step_total else '-'}{detail_info}")
        else:  # IDLE
            self._calib_running = False
            self._poll_timer.stop()
            self._set_last_op("空闲 (未标定)")
            self._add_history(
                f"[ACK] 0x97 {state_cn} 未在标定 level={level} submode={submode}")
        self._refresh_status()

    def on_nack(self, cmd: int, err: int):
        """标定相关命令 NACK (主窗口在 _on_nack 中调用, 内部按 cmd 过滤).

        注意: 0x97 CALIB_QUERY 不再走 NACK 通道(固件改为始终返回详细状态 ACK),
              故本函数只处理启动类(0x90~0x96)与中止(0x98)的 NACK。
        """
        if not self._is_calib_cmd(cmd):
            return
        cn = err_name_cn(err)
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
        cmd, sub_id, _method = self._active_task_key
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
            if self._cogging_wait_flash and self._runtime_panel is not None:
                if self._config_panel is not None:
                    self._config_panel.read_params.emit([184, 185])
                self._runtime_panel.read_params.emit([85, 86, 87, 88, 89])
            else:
                self.send_command.emit(int(JmCmd.CALIB_QUERY), {})

    def on_cogging_result(self, param_id: int, value):
        """0x97 DONE 是采集完成；运行参数ID89确认异步落盘结果。"""
        if not self._cogging_wait_flash or int(param_id) != 89:
            return
        try:
            status = int(value)
        except (ValueError, TypeError):
            return
        if status & (1 << 5):
            self._set_last_op("齿槽表待写 Flash，请退出到待机")
            return
        self._cogging_wait_flash = False
        self._poll_timer.stop()
        if status & (1 << 2):
            message = "齿槽表落盘失败，补偿已关闭"
        elif status & 1:
            method = (status >> 8) & 0xFF
            name = {1: "位置爬行", 2: "静态逐点", 3: "速度环匀速"}.get(method, "未知来源")
            message = f"齿槽表已写入并校验，方案：{name}"
        else:
            message = "齿槽采集完成，但 Flash 表无效"
        self._set_last_op(message)
        self._add_history(f"[齿槽结果] {message}")

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
        """刷新固件读回的徽章状态；旧写入入口保持禁用。"""
        self._lbl_calib_flag.setText(self._calib_flag_text)
        self._lbl_calib_flag.setStyleSheet(self._calib_flag_style())
        self._btn_mark_calibrated.setEnabled(False)
        self._btn_clear_calibrated.setEnabled(False)

    def _on_mark_calibrated(self):
        """兼容旧调用，标定状态只接受固件读回。"""
        return

    def _on_clear_calibrated(self):
        """兼容旧调用；清除使用重新标定专用命令。"""
        return

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
        if self._runtime_panel is not None:
            self._runtime_panel.apply_theme()
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
        if self._runtime_panel is not None:
            try:
                opts["runtime_panel"] = self._runtime_panel.get_opts()
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
                task = list(opts["active_task"])
                # 兼容旧 2 元组 [cmd, sub_id](method 视为 0)
                cmd, sub_id = int(task[0]), int(task[1])
                method = int(task[2]) if len(task) > 2 else 0
                lvl_name, sub_name, sub_desc = self._lookup_task(cmd, sub_id, method)
                if lvl_name:
                    self._set_active_task(cmd, sub_id, method, lvl_name, sub_name, sub_desc)
            except Exception:
                pass
        if self._config_panel is not None and isinstance(opts.get("config_panel"), dict):
            try:
                self._config_panel.set_opts(opts["config_panel"])
            except Exception:
                pass
        if self._runtime_panel is not None and isinstance(opts.get("runtime_panel"), dict):
            try:
                self._runtime_panel.set_opts(opts["runtime_panel"])
            except Exception:
                pass
