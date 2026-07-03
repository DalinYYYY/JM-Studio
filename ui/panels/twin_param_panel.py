"""数字孪生参数面板

集中展示虚拟电机的全部参数, 按三类分组:
  1. 给定参数(可配置)  —— 直接编辑 MotorParam 字段, 实时生效
  2. 推导参数(只读)    —— 从给定参数按物理公式实时推导
  3. 保护参数(可配置)  —— FaultDetector 阈值, 影响故障触发

仅在连接数字孪生引擎(VirtualTransport)时有效; 真实串口下展示为空并提示。
"""

import os
from dataclasses import dataclass, field
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
    QWidget,
)
from PyQt6.QtGui import QColor

from ui.theme import theme


# ==================== 参数条目定义 ====================
@dataclass
class FieldSpec:
    """字段规格: 子结构/属性名/中文/单位/最小/最大/步长/小数位。"""
    sub: str             # MotorParam 子结构字段名 (如 'motor_base')
    attr: str            # 子结构内属性名 (如 'r')
    cn: str              # 中文名
    unit: str = ''       # 单位
    vmin: float = -1e9
    vmax: float = 1e9
    step: float = 0.1
    decimals: int = 4
    is_int: bool = False


# ---- 给定参数(可配置): 电机物理本体参数 ----
_GIVEN_GROUPS = [
    ("电机本体", "motor_base", [
        FieldSpec("motor_base", "r", "定子电阻 R", "Ω", 0, 100, 0.01, 4),
        FieldSpec("motor_base", "ld", "d轴电感 Ld", "H", 0, 1, 0.0001, 6),
        FieldSpec("motor_base", "lq", "q轴电感 Lq", "H", 0, 1, 0.0001, 6),
        FieldSpec("motor_base", "flux", "磁链 ψf", "Wb", 0, 1, 0.001, 5),
        FieldSpec("motor_base", "kt", "转矩常数 Kt", "Nm/A", 0, 100, 0.01, 4),
        FieldSpec("motor_base", "ke", "反电动势常数 Ke", "V/(rad/s)", 0, 100, 0.01, 4),
        FieldSpec("motor_base", "pole_pairs", "极对数 p", "", 1, 50, 1, 0, True),
        FieldSpec("motor_base", "inertia", "转子惯量 J", "kg·m²", 0, 1, 1e-6, 7),
        FieldSpec("motor_base", "rated_current", "额定电流", "A", 0, 100, 0.1, 2),
        FieldSpec("motor_base", "peak_current", "峰值电流", "A", 0, 200, 0.1, 2),
        FieldSpec("motor_base", "max_speed", "最大转速", "rad/s", 0, 10000, 1, 1),
        FieldSpec("motor_base", "rated_speed_rpm", "额定转速", "RPM", 0, 50000, 1, 0, True),
        FieldSpec("motor_base", "rated_torque", "额定转矩", "Nm", 0, 1000, 0.1, 2),
        FieldSpec("motor_base", "peak_torque", "峰值转矩", "Nm", 0, 2000, 0.1, 2),
        FieldSpec("motor_base", "rated_voltage", "额定电压", "V", 0, 500, 0.1, 1),
        FieldSpec("motor_base", "pwm_freq_hz", "PWM频率", "Hz", 1000, 100000, 1000, 0, True),
        FieldSpec("motor_base", "foc_freq_hz", "FOC频率", "Hz", 1000, 100000, 1000, 0, True),
        FieldSpec("motor_base", "dead_time_ns", "死区时间", "ns", 0, 5000, 10, 0, True),
    ]),
    ("减速器", "gearbox_param", [
        FieldSpec("gearbox_param", "gear_ratio", "减速比", "", 1, 10000, 1, 1),
        FieldSpec("gearbox_param", "gear_efficiency", "效率", "", 0.1, 1.0, 0.01, 2),
        FieldSpec("gearbox_param", "output_torque_const", "输出转矩常数", "Nm/A", 0, 1000, 0.1, 2),
        FieldSpec("gearbox_param", "gear_backlash", "回程间隙", "rad", 0, 1, 0.001, 4),
    ]),
    ("编码器", "encoder_param", [
        # 单圈绝对值编码器: 无需类型/线数 CPR, 只需方向/偏置/电角度/滤波/PLL
        FieldSpec("encoder_param", "enc_direction", "方向(1/-1)", "", -1, 1, 1, 0, True),
        FieldSpec("encoder_param", "enc_offset", "偏移", "deg", -360.0, 360.0, 0.01, 4),
        FieldSpec("encoder_param", "elec_angle_bias", "电角度偏置", "rad", -6.28, 6.28, 0.01, 4),
        FieldSpec("encoder_param", "pos_filter_alpha", "位置滤波系数", "", 0, 1, 0.01, 3),
        FieldSpec("encoder_param", "enc_auto_calib", "自动校准", "", 0, 1, 1, 0, True),
        FieldSpec("encoder_param", "speed_obs_gain", "PLL增益", "", 0, 10000, 1, 1),
    ]),
    ("热模型", "thermal_model", [
        FieldSpec("thermal_model", "thermal_resistance", "热阻", "K/W", 0, 100, 0.01, 3),
        FieldSpec("thermal_model", "thermal_time_const", "热时间常数", "s", 0, 10000, 1, 1),
        FieldSpec("thermal_model", "derating_temp_start", "降额起始温度", "°C", -20, 150, 1, 1),
        FieldSpec("thermal_model", "fet_over_temp_threshold", "FET过温阈值", "°C", -20, 200, 1, 1),
        FieldSpec("thermal_model", "motor_over_temp_threshold", "电机过温阈值", "°C", -20, 250, 1, 1),
    ]),
]


# ---- 控制参数(可配置): PID/前馈/阻抗/回零 (单独 tab, 与物理本体分离便于调参) ----
# 每组细分为「基础」(调参必用) 与「高级」(特殊场景才用) 两子组,
# 用户调参时聚焦基础组即可, 高级组仅在抑制振荡/前馈/陷波等场景才调整。
_CONTROL_GROUPS = [
    # ===== 电流环 =====
    ("电流环 - 基础 PID", "current_loop", [
        FieldSpec("current_loop", "current_kp_d", "d轴 Kp", "V/A", -100, 100, 0.01, 4),
        FieldSpec("current_loop", "current_ki_d", "d轴 Ki", "V/(A·s)", -1000, 1000, 0.1, 3),
        FieldSpec("current_loop", "current_kp_q", "q轴 Kp", "V/A", -100, 100, 0.01, 4),
        FieldSpec("current_loop", "current_ki_q", "q轴 Ki", "V/(A·s)", -1000, 1000, 0.1, 3),
        FieldSpec("current_loop", "current_integral_limit", "积分限幅", "V", 0, 1000, 0.1, 2),
    ]),
    ("电流环 - 高级 (前馈/解耦/滤波)", "current_loop", [
        FieldSpec("current_loop", "decoupling_gain", "解耦增益", "", 0, 2, 0.01, 3),
        FieldSpec("current_loop", "deadtime_comp_v", "死区补偿", "V", 0, 10, 0.01, 3),
        FieldSpec("current_loop", "pwm_max_duty", "PWM最大占空比", "", 0.1, 1.0, 0.01, 3),
        FieldSpec("current_loop", "current_bandwidth_hz", "电流环带宽", "Hz", 10, 10000, 10, 1),
        FieldSpec("current_loop", "current_filter_alpha", "电流滤波系数", "", 0, 1, 0.01, 3),
        FieldSpec("current_loop", "d_feedforward_gain", "d轴前馈增益", "", 0, 2, 0.01, 3),
        FieldSpec("current_loop", "q_feedforward_gain", "q轴前馈增益", "", 0, 2, 0.01, 3),
    ]),

    # ===== 速度环 =====
    ("速度环 - 基础 PID", "position_loop", [
        FieldSpec("position_loop", "speed_kp", "速度环 Kp", "A/(rad/s)", -100, 100, 0.01, 4),
        FieldSpec("position_loop", "speed_ki", "速度环 Ki", "A/rad", -1000, 1000, 0.1, 3),
        FieldSpec("position_loop", "speed_integral_limit", "速度积分限幅", "A", 0, 100, 0.1, 2),
    ]),
    ("速度环 - 高级 (前馈/滤波/带宽)", "position_loop", [
        FieldSpec("position_loop", "velocity_ff_gain", "速度前馈增益", "", 0, 2, 0.01, 3),
        FieldSpec("position_loop", "accel_ff_gain", "加速度前馈增益", "", 0, 2, 0.01, 3),
        FieldSpec("position_loop", "speed_bandwidth_hz", "速度环带宽", "Hz", 0, 5000, 10, 1),
        FieldSpec("position_loop", "speed_filter_alpha", "速度滤波系数", "", 0, 1, 0.01, 3),
    ]),

    # ===== 位置环 =====
    ("位置环 - 基础", "position_loop", [
        FieldSpec("position_loop", "position_kp", "位置环 Kp", "Hz", 0, 1000, 0.1, 2),
        FieldSpec("position_loop", "position_integral_limit", "位置积分限幅", "rad", 0, 100, 0.01, 3),
        FieldSpec("position_loop", "follow_error_threshold", "跟随误差阈值", "rad", 0, 100, 0.01, 3),
        FieldSpec("position_loop", "max_accel", "最大加速度", "rad/s²", 0, 1e6, 1, 1),
    ]),
    ("位置环 - 高级 (摩擦补偿/陷波/带宽)", "position_loop", [
        FieldSpec("position_loop", "friction_coulomb", "库仑摩擦", "Nm", -50, 50, 0.001, 4),
        FieldSpec("position_loop", "friction_viscous", "粘滞摩擦", "Nm/(rad/s)", -50, 50, 0.001, 4),
        FieldSpec("position_loop", "notch_freq_hz", "陷波频率", "Hz", 0, 5000, 10, 1),
        FieldSpec("position_loop", "notch_width_hz", "陷波宽度", "Hz", 0, 1000, 1, 1),
        FieldSpec("position_loop", "notch_depth_db", "陷波深度", "dB", 0, 80, 1, 1),
        FieldSpec("position_loop", "notch_enable", "陷波使能", "", 0, 1, 1, 0, True),
        FieldSpec("position_loop", "position_bandwidth_hz", "位置环带宽", "Hz", 0, 2000, 1, 1),
    ]),

    # ===== 阻抗控制 (MIT) =====
    ("阻抗控制 (MIT)", "impedance_ctrl", [
        FieldSpec("impedance_ctrl", "impedance_kp", "刚度 Kp", "Nm/rad", 0, 1000, 1, 2),
        FieldSpec("impedance_ctrl", "impedance_kd", "阻尼 Kd", "Nm/(rad/s)", 0, 100, 0.01, 3),
        FieldSpec("impedance_ctrl", "iq_max", "iq最大值", "A", 0, 100, 0.1, 2),
    ]),

    # ===== 回零 =====
    ("回零", "homing_param", [
        FieldSpec("homing_param", "homing_method", "回零方式", "", 0, 20, 1, 0, True),
        FieldSpec("homing_param", "homing_speed_fast", "快速速度", "rad/s", 0, 100, 0.1, 2),
        FieldSpec("homing_param", "homing_speed_slow", "慢速速度", "rad/s", 0, 50, 0.1, 2),
        FieldSpec("homing_param", "homing_offset", "回零偏置", "rad", -100, 100, 0.01, 3),
        FieldSpec("homing_param", "homing_current", "回零电流", "A", 0, 50, 0.1, 2),
    ]),
]


# ---- 高级参数使能规格 ----
# 每个高级组对应一个使能勾选; 不勾选时, 对应字段被覆盖为"禁用值"(不影响控制)。
# 使能勾选放在对应基础组的末尾。
#
# 字段分组与禁用值依据:
#   - 前馈类 (velocity_ff_gain/accel_ff_gain/d_feedforward_gain/q_feedforward_gain): 禁用=0
#   - 解耦类 (decoupling_gain): 禁用=0 (完全不解耦)
#   - 死区补偿 (deadtime_comp_v): 禁用=0 (不补偿)
#   - 滤波类 (current_filter_alpha/speed_filter_alpha): 禁用=1.0 (alpha=1.0=不滤波, 全通)
#   - 摩擦补偿 (friction_coulomb/friction_viscous): 禁用=0 (不补偿)
#   - 陷波 (notch_enable): 禁用=0 (关闭陷波器)
#   - 带宽参数 (current_bandwidth_hz/speed_bandwidth_hz/position_bandwidth_hz):
#     仅用于理论推导参考, 不直接影响控制, 故不覆盖。
@dataclass
class AdvancedEnableSpec:
    """高级参数使能规格。"""
    enable_key: str        # 使能字段标识
    label: str             # 勾选框文字
    hint: str              # 说明文字
    advanced_group: str    # 对应的高级组名 (跟随勾选显隐)
    # 禁用时各字段覆盖值: (sub, attr) -> 禁用值
    disable_overrides: dict = field(default_factory=dict)


_ADVANCED_ENABLES = [
    AdvancedEnableSpec(
        enable_key='adv_current',
        label='电流环高级 (前馈/解耦/滤波)',
        hint='不勾选时: 高级组隐藏, 解耦=0, 死区补偿=0, 滤波关闭, 前馈=0',
        advanced_group='电流环 - 高级 (前馈/解耦/滤波)',
        disable_overrides={
            ('current_loop', 'decoupling_gain'): 0.0,
            ('current_loop', 'deadtime_comp_v'): 0.0,
            ('current_loop', 'current_filter_alpha'): 1.0,
            ('current_loop', 'd_feedforward_gain'): 0.0,
            ('current_loop', 'q_feedforward_gain'): 0.0,
        },
    ),
    AdvancedEnableSpec(
        enable_key='adv_velocity',
        label='速度环高级 (前馈/滤波)',
        hint='不勾选时: 高级组隐藏, 速度前馈=0, 加速度前馈=0, 速度滤波关闭',
        advanced_group='速度环 - 高级 (前馈/滤波/带宽)',
        disable_overrides={
            ('position_loop', 'velocity_ff_gain'): 0.0,
            ('position_loop', 'accel_ff_gain'): 0.0,
            ('position_loop', 'speed_filter_alpha'): 0.0,
        },
    ),
    AdvancedEnableSpec(
        enable_key='adv_position',
        label='位置环高级 (摩擦补偿/陷波)',
        hint='不勾选时: 高级组隐藏, 摩擦补偿=0, 陷波器关闭',
        advanced_group='位置环 - 高级 (摩擦补偿/陷波/带宽)',
        disable_overrides={
            ('position_loop', 'friction_coulomb'): 0.0,
            ('position_loop', 'friction_viscous'): 0.0,
            ('position_loop', 'notch_enable'): 0,
        },
    ),
]


# ---- 控制参数理论推导公式 ----
# 每项: (参数名, 公式描述, 计算函数(mp)->(理论值, 单位, 说明))
# 基于 PI 整定法: ωn = 2π·bandwidth, ζ=1.0 (临界阻尼)
#   电流环: Kp = 2·ζ·ωn·L, Ki = ωn²·L
#   速度环: Kp = 2·ζ·ωn·J/Kt, Ki = ωn²·J/Kt
#   位置环: Kp = ωn_pos (Hz, P 控制)
def _control_derived_rows(mp):
    """返回控制参数理论推导行列表: [(参数, 理论值, 单位, 公式, 说明)]。"""
    import math as _m
    mb = mp.motor_base
    cl = mp.current_loop
    pl = mp.position_loop

    def _safe_div(a, b, d=0.0):
        return a / b if abs(b) > 1e-12 else d

    rows = []
    zeta = 1.0  # 临界阻尼

    # ===== 电流环 (基于 current_bandwidth_hz) =====
    wn_i = 2 * _m.pi * cl.current_bandwidth_hz
    Ld = max(mb.ld, 1e-9)
    Lq = max(mb.lq, 1e-9)
    rows.append(("d轴电流环 Kp 理论", 2 * zeta * wn_i * Ld, "V/A",
                 "2·ζ·ωn·Ld, ωn=2π·bw_i",
                 f"ζ={zeta}, bw_i={cl.current_bandwidth_hz}Hz; 与实际 Kp={cl.current_kp_d} 比较"))
    rows.append(("d轴电流环 Ki 理论", wn_i ** 2 * Ld, "V/(A·s)",
                 "ωn²·Ld",
                 f"与实际 Ki={cl.current_ki_d} 比较; 偏差大则重整定"))
    rows.append(("q轴电流环 Kp 理论", 2 * zeta * wn_i * Lq, "V/A",
                 "2·ζ·ωn·Lq",
                 f"凸极电机 Lq>Ld, q 轴 Kp 应略大; 实际={cl.current_kp_q}"))
    rows.append(("q轴电流环 Ki 理论", wn_i ** 2 * Lq, "V/(A·s)",
                 "ωn²·Lq",
                 f"实际={cl.current_ki_q}"))

    # ===== 速度环 (基于 speed_bandwidth_hz) =====
    if pl.speed_bandwidth_hz > 0 and mb.kt > 0:
        wn_v = 2 * _m.pi * pl.speed_bandwidth_hz
        J = max(mb.inertia, 1e-12)
        rows.append(("速度环 Kp 理论", 2 * zeta * wn_v * J / mb.kt, "A/(rad/s)",
                     "2·ζ·ωn·J/Kt, ωn=2π·bw_v",
                     f"bw_v={pl.speed_bandwidth_hz}Hz; 实际={pl.speed_kp}"))
        rows.append(("速度环 Ki 理论", wn_v ** 2 * J / mb.kt, "A/rad",
                     "ωn²·J/Kt",
                     f"实际={pl.speed_ki}"))

    # ===== 位置环 (基于 position_bandwidth_hz) =====
    if pl.position_bandwidth_hz > 0:
        rows.append(("位置环 Kp 理论", pl.position_bandwidth_hz, "Hz",
                     "bw_pos (P 控制, Kp=带宽)",
                     f"实际={pl.position_kp}; 偏差大则位置响应不符预期"))

    # ===== 阻抗控制 (基于刚度/惯量推导阻尼) =====
    if mp.impedance_ctrl.impedance_kp > 0:
        J = max(mb.inertia, 1e-12)
        wn_imp = _m.sqrt(mp.impedance_ctrl.impedance_kp / J)
        zeta_actual = _safe_div(mp.impedance_ctrl.impedance_kd,
                                2 * _m.sqrt(mp.impedance_ctrl.impedance_kp * J), 0)
        kd_crit = 2 * _m.sqrt(mp.impedance_ctrl.impedance_kp * J)  # 临界阻尼 Kd
        rows.append(("阻抗环临界阻尼 Kd", kd_crit, "Nm/(rad/s)",
                     "2·sqrt(Kp·J)",
                     f"ζ=1.0 时的 Kd; 实际 Kd={mp.impedance_ctrl.impedance_kd} → ζ={zeta_actual:.3f}"))

    return rows


# ---- 保护参数(可配置): 故障阈值 + 位置限位 ----
@dataclass
class ProtectSpec:
    """保护参数规格: 名称/中文/单位/范围/步长/小数位/公式描述。

    src: 数据来源
      - "th"        -> FaultDetector.th 字段
      - "pos_limit" -> MotorParam.position_limit 字段
    """
    key: str
    cn: str
    unit: str
    vmin: float
    vmax: float
    step: float
    decimals: int
    formula: str   # 公式描述(展示用)
    is_int: bool = False
    src: str = "th"


_PROTECT_FIELDS = [
    # 故障检测阈值 (来自 FaultDetector.th)
    ProtectSpec("over_current", "过流阈值", "A", 0, 200, 0.1, 2, "protect_over_current / peak_current×1.2"),
    ProtectSpec("over_voltage", "过压阈值", "V", 0, 200, 0.1, 1, "protect_over_voltage / rated_voltage×1.1"),
    ProtectSpec("under_voltage", "欠压阈值", "V", 0, 200, 0.1, 1, "protect_under_voltage / rated_voltage×0.5"),
    ProtectSpec("over_speed", "超速阈值", "rad/s", 0, 10000, 1, 1, "min(protect_over_speed, max_speed×1.3)"),
    ProtectSpec("over_temp_fet", "FET过温", "°C", -20, 200, 1, 1, "protect_over_temp / fet_over_temp_threshold"),
    ProtectSpec("over_temp_motor", "电机过温", "°C", -20, 250, 1, 1, "protect_over_temp / motor_over_temp_threshold"),
    ProtectSpec("under_temp", "欠温阈值", "°C", -100, 0, 1, 1, "protect_under_temp"),
    ProtectSpec("follow_err", "跟随误差阈值", "rad", 0, 100, 0.01, 3, "follow_error_threshold"),
    ProtectSpec("follow_err_time", "跟随误差持续", "s", 0, 10, 0.01, 3, "固定 0.2"),
    ProtectSpec("comm_timeout", "通信超时", "s", 0, 10, 0.01, 3, "固定 0.2"),
    # 位置限位 (从给定参数移过来, 属于保护范畴)
    ProtectSpec("pos_min_limit", "最小位置限位", "rad", -1e6, 0, 0.1, 3,
                "position_limit.pos_min_limit", src="pos_limit"),
    ProtectSpec("pos_max_limit", "最大位置限位", "rad", 0, 1e6, 0.1, 3,
                "position_limit.pos_max_limit", src="pos_limit"),
    ProtectSpec("multiturn_enable", "多圈使能", "", 0, 1, 1, 0,
                "position_limit.multiturn_enable", is_int=True, src="pos_limit"),
    ProtectSpec("limit_sw_enable", "限位开关使能", "", 0, 1, 1, 0,
                "position_limit.limit_sw_enable", is_int=True, src="pos_limit"),
]


# ==================== 主面板 ====================
class TwinParamPanel(QGroupBox):
    """数字孪生参数面板(给定/控制/推导/保护 四类)。

    Tab 分组:
      1. 给定参数  —— 电机物理本体(电机/减速器/编码器/热模型), 可配置
      2. 控制参数  —— PID/前馈/阻抗/回零 (调参专用), 可配置
      3. 推导参数  —— 从给定参数按物理公式实时推导, 只读, 含备注
      4. 保护参数  —— 故障检测阈值 + 位置限位, 可配置
    """

    params_changed = pyqtSignal()   # 任意参数修改后发出(主窗口可订阅触发 reload_params)

    def __init__(self, parent=None):
        super().__init__("", parent)
        self._panel_name = "孪生参数"
        self._engine = None          # DigitalTwinEngine 引用
        self._mp = None              # MotorParam
        self._given_widgets = {}     # (sub, attr) -> QWidget (给定参数: 电机本体/减速器/编码器/热模型)
        self._control_widgets = {}   # (sub, attr) -> QWidget (控制参数: PID/阻抗/回零)
        self._protect_widgets = {}   # key -> QWidget (保护参数: 故障阈值+位置限位)
        self._derived_labels = {}    # 推导参数 key -> QLabel
        self._adv_checks = {}        # enable_key -> QCheckBox (高级参数使能勾选)
        self._adv_enabled = {}       # enable_key -> bool (使能状态, 默认 False)
        self._adv_groups = {}        # enable_key -> QGroupBox (对应高级组, 跟随勾选显隐)
        self._guard = False          # 防止 setValue 触发回调
        self._build()

    def panel_name(self) -> str:
        return self._panel_name

    # ---------------- 生命周期 ----------------
    def set_engine(self, engine):
        """连接/切换引擎时由主窗口调用, 传入 DigitalTwinEngine。"""
        self._engine = engine
        self._mp = engine.mp if engine is not None else None
        self._guard = True
        self._reload_given()
        self._reload_control()
        self._reload_protect()
        self._guard = False
        # 首次连接: 应用高级参数覆盖 (默认未使能 → 禁用值生效)
        self._apply_advanced_overrides()
        self._refresh_derived()
        self._refresh_control_derived()
        self._hint_label.setVisible(self._mp is None)

    # ==================== 配置持久化 ====================
    def get_opts(self) -> dict:
        """收集可持久化的 UI 配置: 高级使能勾选 + 当前 tab 索引."""
        opts = {"adv_enabled": {}, "current_tab": 0}
        try:
            opts["current_tab"] = int(self._tabs.currentIndex())
        except Exception:
            pass
        for key, chk in self._adv_checks.items():
            try:
                opts["adv_enabled"][key] = bool(chk.isChecked())
            except Exception:
                pass
        return opts

    def set_opts(self, opts: dict):
        """启动时套用配置 (容错)."""
        if not isinstance(opts, dict):
            return
        ae = opts.get("adv_enabled")
        if isinstance(ae, dict):
            for key, val in ae.items():
                chk = self._adv_checks.get(key)
                if chk is None:
                    continue
                try:
                    chk.setChecked(bool(val))
                except Exception:
                    pass
        try:
            idx = int(opts.get("current_tab", 0))
            if 0 <= idx < self._tabs.count():
                self._tabs.setCurrentIndex(idx)
        except Exception:
            pass

    # ---------------- 构建 UI ----------------
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)

        # 顶部提示
        self._hint_label = QLabel("⚠ 仅在连接 [虚拟数据引擎] 时可用")
        self._hint_label.setStyleSheet(
            f"color: {theme.hex('warn')}; padding: 4px; font-weight: bold;")
        # 先 addWidget 父级化, 再 setVisible; 否则 widget 无 parent 时被设为可见
        # 会作为独立小窗口在 Windows 上短暂弹出 (setVisible(True) 本身冗余, 默认即可见)
        layout.addWidget(self._hint_label)
        self._hint_label.setVisible(True)

        # 顶部工具栏
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(4, 2, 4, 2)
        toolbar.setSpacing(8)
        lbl_title = QLabel("数字孪生参数")
        lbl_title.setStyleSheet(
            f"color: {theme.hex('title')}; font-weight: bold; font-size: 13px;")
        toolbar.addWidget(lbl_title)
        toolbar.addStretch()
        btn_import = QPushButton("导入")
        btn_import.clicked.connect(self._on_import)
        btn_import.setStyleSheet(self._btn_style())
        toolbar.addWidget(btn_import)
        btn_export = QPushButton("导出")
        btn_export.clicked.connect(self._on_export)
        btn_export.setStyleSheet(self._btn_style())
        toolbar.addWidget(btn_export)
        btn_reset = QPushButton("重置为默认")
        btn_reset.clicked.connect(self._on_reset_default)
        btn_reset.setStyleSheet(self._btn_style())
        toolbar.addWidget(btn_reset)
        btn_apply = QPushButton("应用并通知引擎")
        btn_apply.clicked.connect(self._on_apply)
        btn_apply.setStyleSheet(self._btn_accent_style())
        toolbar.addWidget(btn_apply)
        tb_w = QWidget()
        tb_w.setLayout(toolbar)
        layout.addWidget(tb_w)

        # 四类参数: 用 QTabWidget 分页
        tabs = QTabWidget()
        tabs.addTab(self._build_given_tab(), "给定参数 (可配置)")
        tabs.addTab(self._build_control_tab(), "控制参数 (PID/回零)")
        tabs.addTab(self._build_derived_tab(), "推导参数 (只读)")
        tabs.addTab(self._build_protect_tab(), "保护参数")
        self._tabs = tabs
        layout.addWidget(tabs, 1)

    def _build_given_tab(self) -> QWidget:
        """给定参数: 电机物理本体(电机/减速器/编码器/热模型) 4 组子结构。"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(6)

        for group_name, sub_name, fields in _GIVEN_GROUPS:
            grp = QGroupBox(group_name)
            form = QFormLayout(grp)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
            form.setContentsMargins(8, 6, 8, 6)
            form.setSpacing(4)
            for fs in fields:
                w = self._make_editor(fs)
                self._given_widgets[(fs.sub, fs.attr)] = w
                label_text = f"{fs.cn}"
                if fs.unit:
                    label_text += f"  [{fs.unit}]"
                form.addRow(label_text, w)
            v.addWidget(grp)
        v.addStretch()

        scroll.setWidget(host)
        return scroll

    def _build_control_tab(self) -> QWidget:
        """控制参数: 电流环/速度位置环 PID + 阻抗 + 回零 (调参专用)。

        布局:
          - 顶部一行: 3 个高级参数使能勾选框 (默认不勾选, 高级组隐藏)
          - 中部: 基础 PID 组 (始终显示) + 高级组 (跟随勾选显隐)
          - 底部: 理论推导与调参建议表格
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(6)

        info = QLabel("控制参数包含电流环/速度位置环 PID、阻抗控制 (MIT) 和回零参数。"
                      "高级参数(前馈/解耦/滤波/陷波/摩擦补偿)默认关闭; 勾选顶部对应选项后高级组才显示并生效。"
                      "修改后点 [应用并通知引擎] 让控制器重新加载增益。")
        info.setStyleSheet(f"color: {theme.hex('muted')}; padding: 4px;")
        info.setWordWrap(True)
        v.addWidget(info)

        # ===== 顶部一行: 高级参数使能勾选框 =====
        chk_bar = QHBoxLayout()
        chk_bar.setContentsMargins(4, 2, 4, 2)
        chk_bar.setSpacing(16)
        lbl_adv = QLabel("高级参数:")
        lbl_adv.setStyleSheet(
            f"color: {theme.hex('title')}; font-weight: bold; padding: 2px;")
        chk_bar.addWidget(lbl_adv)
        for spec in _ADVANCED_ENABLES:
            chk = QCheckBox(spec.label)
            chk.setChecked(False)   # 默认不使能 → 高级组隐藏
            chk.setToolTip(spec.hint)
            chk.setStyleSheet(
                f"QCheckBox {{ color: {theme.hex('accent')}; font-weight: bold; padding: 4px; }}"
                f"QCheckBox::indicator {{ width: 16px; height: 16px; }}")
            chk.stateChanged.connect(lambda state, k=spec.enable_key:
                                      self._on_adv_toggled(k, state))
            self._adv_checks[spec.enable_key] = chk
            self._adv_enabled[spec.enable_key] = False
            chk_bar.addWidget(chk)
        chk_bar.addStretch()
        chk_w = QWidget()
        chk_w.setLayout(chk_bar)
        v.addWidget(chk_w)

        # ===== 中部: 参数组 (基础组始终显示, 高级组跟随勾选) =====
        adv_by_group = {spec.advanced_group: spec for spec in _ADVANCED_ENABLES}
        for group_name, sub_name, fields in _CONTROL_GROUPS:
            grp = QGroupBox(group_name)
            form = QFormLayout(grp)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
            form.setContentsMargins(8, 6, 8, 6)
            form.setSpacing(4)
            for fs in fields:
                w = self._make_editor(fs)
                self._control_widgets[(fs.sub, fs.attr)] = w
                label_text = f"{fs.cn}"
                if fs.unit:
                    label_text += f"  [{fs.unit}]"
                form.addRow(label_text, w)
            v.addWidget(grp)

            # 高级组: 记录引用, 默认隐藏
            if group_name in adv_by_group:
                spec = adv_by_group[group_name]
                self._adv_groups[spec.enable_key] = grp
                grp.setVisible(False)   # 默认隐藏

        # ===== 底部: 理论推导与调参建议表格 =====
        v.addWidget(self._build_control_derived_section())
        v.addStretch()

        scroll.setWidget(host)
        return scroll

    def _build_control_derived_section(self) -> QWidget:
        """控制参数 tab 底部: 理论推导值与实际值对比表格。"""
        grp = QGroupBox("理论推导与调参建议 (基于带宽参数实时计算)")
        v = QVBoxLayout(grp)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        info = QLabel("理论值基于带宽参数 (current_bandwidth_hz / speed_bandwidth_hz / "
                      "position_bandwidth_hz) 与电机物理参数 (L/J/Kt) 按 PI 整定法推导。"
                      "ζ=1.0 临界阻尼。对比实际值, 偏差大则需重整定。")
        info.setStyleSheet(f"color: {theme.hex('muted')}; font-size: 11px;")
        info.setWordWrap(True)
        v.addWidget(info)

        self._ctrl_derived_table = QTableWidget(0, 5)
        self._ctrl_derived_table.setHorizontalHeaderLabels(
            ["参数", "理论值", "单位", "公式", "说明 (含实际值对比)"])
        self._style_derived_table(self._ctrl_derived_table)
        v.addWidget(self._ctrl_derived_table)
        return grp

    def _style_derived_table(self, tbl: QTableWidget):
        """统一表格样式: 列宽策略 / 行高 / 对齐 / 字体, 保证两个推导表格视觉一致。"""
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.setAlternatingRowColors(True)
        tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tbl.setWordWrap(True)
        tbl.verticalHeader().setVisible(False)
        tbl.verticalHeader().setDefaultSectionSize(24)
        # 列宽策略: 参数/数值/单位/公式 紧凑自适应, 最后一列(说明/备注)拉伸填充
        for col in range(4):
            tbl.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents)
        tbl.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch)
        tbl.horizontalHeader().setStretchLastSection(False)
        # 表头样式: 居中 + 加粗
        tbl.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)

    def _fill_derived_row(self, tbl: QTableWidget, r: int,
                          name: str, val: str, unit: str,
                          formula: str, remark: str):
        """统一填充一行: 数值/单位居中, 公式用 muted 色, 备注正常色。"""
        it_name = QTableWidgetItem(name)
        it_val = QTableWidgetItem(val)
        it_unit = QTableWidgetItem(unit)
        it_formula = QTableWidgetItem(formula)
        it_remark = QTableWidgetItem(remark)
        it_val.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        it_unit.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        it_val.setForeground(QColor(theme.hex("value")))
        it_formula.setForeground(QColor(theme.hex("muted")))
        it_remark.setForeground(QColor(theme.hex("text")))
        it_remark.setToolTip(remark)
        tbl.setItem(r, 0, it_name)
        tbl.setItem(r, 1, it_val)
        tbl.setItem(r, 2, it_unit)
        tbl.setItem(r, 3, it_formula)
        tbl.setItem(r, 4, it_remark)

    def _build_derived_tab(self) -> QWidget:
        """推导参数: 表格显示, 参数/数值/单位/公式/备注 5 列, 只读。"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(4, 4, 4, 4)

        # 说明
        info = QLabel("推导参数从给定参数按物理公式实时计算, 仅供只读参考。"
                      "「备注」列说明物理意义、典型范围或调参依据。")
        info.setStyleSheet(f"color: {theme.hex('muted')}; padding: 4px;")
        info.setWordWrap(True)
        v.addWidget(info)

        self._derived_table = QTableWidget(0, 5)
        self._derived_table.setHorizontalHeaderLabels(["参数", "数值", "单位", "公式", "备注"])
        self._style_derived_table(self._derived_table)
        v.addWidget(self._derived_table, 1)
        scroll.setWidget(host)
        return scroll

    def _build_protect_tab(self) -> QWidget:
        """保护参数: 故障检测阈值 + 位置限位, QFormLayout 排版 + 公式提示。"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(4, 4, 4, 4)

        info = QLabel("保护参数影响 FaultDetector 阈值与位置限位, 实时生效。"
                      "修改给定参数后公式默认值变化, 可点 [从给定参数重算默认] 同步。")
        info.setStyleSheet(f"color: {theme.hex('muted')}; padding: 4px;")
        info.setWordWrap(True)
        v.addWidget(info)

        # 顶部: 重新从给定参数推导默认值
        bar = QHBoxLayout()
        bar.addStretch()
        btn_recalc = QPushButton("从给定参数重算默认")
        btn_recalc.clicked.connect(self._on_recalc_protect)
        btn_recalc.setStyleSheet(self._btn_style())
        bar.addWidget(btn_recalc)
        bar_w = QWidget()
        bar_w.setLayout(bar)
        v.addWidget(bar_w)

        # 按 src 分组: 故障阈值 (th) + 位置限位 (pos_limit)
        groups = [("故障检测阈值", "th"), ("位置限位", "pos_limit")]
        for grp_name, src_tag in groups:
            grp = QGroupBox(grp_name)
            form = QFormLayout(grp)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
            form.setContentsMargins(8, 6, 8, 6)
            form.setSpacing(6)
            for spec in _PROTECT_FIELDS:
                if spec.src != src_tag:
                    continue
                row_w = QWidget()
                row = QHBoxLayout(row_w)
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(6)
                if spec.is_int:
                    w = QSpinBox()
                    w.setRange(int(spec.vmin), int(spec.vmax))
                    w.setSingleStep(int(spec.step))
                else:
                    w = QDoubleSpinBox()
                    w.setRange(spec.vmin, spec.vmax)
                    w.setSingleStep(spec.step)
                    w.setDecimals(spec.decimals)
                w.setMinimumWidth(120)
                w.valueChanged.connect(self._on_any_changed)
                self._protect_widgets[spec.key] = w
                row.addWidget(w)
                row.addSpacing(4)
                lbl_formula = QLabel(f"= {spec.formula}")
                lbl_formula.setStyleSheet(f"color: {theme.hex('muted')}; font-size: 11px;")
                row.addWidget(lbl_formula)
                row.addStretch()
                label_text = f"{spec.cn}"
                if spec.unit:
                    label_text += f"  [{spec.unit}]"
                form.addRow(label_text, row_w)
            v.addWidget(grp)
        v.addStretch()
        scroll.setWidget(host)
        return scroll

    # ---------------- 编辑器构造 ----------------
    def _make_editor(self, fs: FieldSpec):
        if fs.is_int:
            w = QSpinBox()
            w.setRange(int(fs.vmin), int(fs.vmax))
            w.setSingleStep(int(fs.step))
        else:
            w = QDoubleSpinBox()
            w.setRange(fs.vmin, fs.vmax)
            w.setSingleStep(fs.step)
            w.setDecimals(fs.decimals)
        w.setMinimumWidth(120)
        w.valueChanged.connect(self._on_any_changed)
        return w

    # ---------------- 数据加载 ----------------
    def _reload_given(self):
        """从 MotorParam 加载给定参数(电机本体/减速器/编码器/热模型)到编辑器。"""
        if self._mp is None:
            return
        for (sub, attr), w in self._given_widgets.items():
            try:
                sub_obj = getattr(self._mp, sub)
                val = getattr(sub_obj, attr)
            except AttributeError:
                continue
            self._guard = True
            if isinstance(w, QSpinBox):
                w.setValue(int(val))
            else:
                w.setValue(float(val))
            self._guard = False

    def _reload_control(self):
        """从 MotorParam 加载控制参数(电流环/速度位置环 PID/阻抗/回零)到编辑器。"""
        if self._mp is None:
            return
        for (sub, attr), w in self._control_widgets.items():
            try:
                sub_obj = getattr(self._mp, sub)
                val = getattr(sub_obj, attr)
            except AttributeError:
                continue
            self._guard = True
            if isinstance(w, QSpinBox):
                w.setValue(int(val))
            else:
                w.setValue(float(val))
            self._guard = False

    def _reload_protect(self):
        """从 FaultDetector.th 与 MotorParam.position_limit 加载保护参数。"""
        if self._engine is None:
            return
        th = self._engine.fault_detector.th
        pl = self._mp.position_limit
        # 按 src 分类的取值映射
        mapping = {
            # src="th"  -> FaultDetector.th 字段
            'over_current': th.over_current,
            'over_voltage': th.over_voltage,
            'under_voltage': th.under_voltage,
            'over_speed': th.over_speed,
            'over_temp_fet': th.over_temp_fet,
            'over_temp_motor': th.over_temp_motor,
            'under_temp': th.under_temp,
            'follow_err': th.follow_err,
            'follow_err_time': th.follow_err_time,
            'comm_timeout': th.comm_timeout,
            # src="pos_limit" -> MotorParam.position_limit 字段
            'pos_min_limit': pl.pos_min_limit,
            'pos_max_limit': pl.pos_max_limit,
            'multiturn_enable': pl.multiturn_enable,
            'limit_sw_enable': pl.limit_sw_enable,
        }
        self._guard = True
        for key, w in self._protect_widgets.items():
            if key in mapping:
                val = mapping[key]
                if isinstance(w, QSpinBox):
                    w.setValue(int(val))
                else:
                    w.setValue(float(val))
        self._guard = False

    def _refresh_derived(self):
        """重算所有推导参数并刷新表格(参数/数值/单位/公式/备注 5 列)。"""
        self._derived_table.setRowCount(0)
        if self._mp is None:
            return
        import math
        mb = self._mp.motor_base
        gb = self._mp.gearbox_param
        en = self._mp.encoder_param
        cl = self._mp.current_loop
        pl = self._mp.position_loop
        ic = self._mp.impedance_ctrl
        th = self._mp.thermal_model
        hm = self._mp.homing_param
        plm = self._mp.position_limit

        def safe_div(a, b, default=0.0):
            return a / b if abs(b) > 1e-12 else default

        rows = []  # 每项: (名称, 数值, 单位, 公式, 备注)

        # ===== 1. 电气时间常数 =====
        tau_d = safe_div(mb.ld, mb.r, 0)
        tau_q = safe_div(mb.lq, mb.r, 0)
        rows.append(("d轴电气时间常数 τd", tau_d, "s", "Ld / R",
                     "电流环带宽应 ≥ 10/τd; 决定电流响应快慢"))
        rows.append(("q轴电气时间常数 τq", tau_q, "s", "Lq / R",
                     "凸极电机 τq > τd, q 轴响应稍慢"))

        # ===== 2. 凸极率 =====
        salience = safe_div(mb.lq, mb.ld, 1.0)
        rows.append(("凸极率 ρ", salience, "", "Lq / Ld",
                     "ρ=1 隐极(SPMSM); ρ>1 凸极(IPMSM), 常见 1.5~3"))

        # ===== 3. 转矩常数与磁链关系 (验证一致性) =====
        kt_calc = 1.5 * mb.pole_pairs * mb.flux
        kt_mismatch = mb.kt - kt_calc
        rows.append(("由磁链推导 Kt", kt_calc, "Nm/A", "1.5 · p · ψf",
                     "应与配置 Kt 一致; 否则 MIT 开环力矩控制失配"))
        rows.append(("Kt 失配量", kt_mismatch, "Nm/A", "Kt − 1.5·p·ψf",
                     "=0 一致; ≠0 影响开环力矩精度, MIT 模式敏感"))

        # ===== 4. 反电动势常数 =====
        rows.append(("反电动势常数 Ke", mb.kt, "V/(rad/s)", "= Kt (SI 单位制)",
                     "SI 单位下 Ke = Kt 恒成立; 配置 Ke 仅用于观测器校验"))

        # ===== 5. 电气频率与感抗 (高速工况) =====
        fe_max = mb.pole_pairs * mb.max_speed / (2 * math.pi)
        we_max = 2 * math.pi * fe_max
        XL_d = mb.ld * we_max
        XL_q = mb.lq * we_max
        rows.append(("最大电气频率 fe", fe_max, "Hz", "p · ωmax / (2π)",
                     "FOC 频率应 ≥ 10·fe, 否则电流环失真"))
        rows.append(("d轴最大感抗", XL_d, "Ω", "Ld · ωe_max",
                     "高速时 d 轴感抗压降占比"))
        rows.append(("q轴最大感抗", XL_q, "Ω", "Lq · ωe_max",
                     "高速时 q 轴感抗压降, 决定弱磁需求"))

        # ===== 6. SVPWM 电压限幅与调制比 =====
        vmax = mb.rated_voltage * cl.pwm_max_duty / math.sqrt(3.0)
        rows.append(("电压限幅 Vmax", vmax, "V", "Vrated · pwm_max_duty / √3",
                     "SVPWM 线性区最大相电压幅值; 超出进入过调制"))
        v_back_emf = mb.kt * mb.max_speed
        v_resist = mb.r * mb.peak_current
        v_ind_q = XL_q * mb.peak_current
        v_ref = math.sqrt((v_back_emf + v_resist) ** 2 + v_ind_q ** 2)
        mod_index = safe_div(v_ref, vmax, 0)
        rows.append(("峰值工况调制比", mod_index, "", "Vref / Vmax",
                     "<1 线性区; >1 过调制(波形畸变); >1.15 失控"))

        # ===== 7. 电流环 PI 动态 (d/q 轴分别计算) =====
        Ld = max(mb.ld, 1e-9)
        Lq = max(mb.lq, 1e-9)
        if cl.current_ki_d > 0:
            wn_id = math.sqrt(cl.current_ki_d / Ld)
            zeta_id = cl.current_kp_d / (2 * math.sqrt(cl.current_ki_d * Ld))
            rows.append(("d轴电流环 ωn", wn_id, "rad/s", "sqrt(Ki/Ld)",
                         "目标带宽; 建议 ωn ≈ 2π·current_bandwidth_hz"))
            rows.append(("d轴电流环 ζ", zeta_id, "", "Kp / (2·sqrt(Ki·Ld))",
                         "阻尼比; 目标 0.7~1.0, <0.7 振荡, >1.0 响应迟钝"))
        if cl.current_ki_q > 0:
            wn_iq = math.sqrt(cl.current_ki_q / Lq)
            zeta_iq = cl.current_kp_q / (2 * math.sqrt(cl.current_ki_q * Lq))
            rows.append(("q轴电流环 ωn", wn_iq, "rad/s", "sqrt(Ki/Lq)",
                         "应与 d 轴接近 (凸极电机略低)"))
            rows.append(("q轴电流环 ζ", zeta_iq, "", "Kp / (2·sqrt(Ki·Lq))",
                         "阻尼比; 目标 0.7~1.0"))

        # ===== 8. 速度环 PI 动态 =====
        J = max(mb.inertia, 1e-12)
        if pl.speed_ki * mb.kt > 0:
            wn_vel = math.sqrt(pl.speed_ki * mb.kt / J)
            zeta_vel = pl.speed_kp / (2 * math.sqrt(pl.speed_ki * mb.kt * J))
            rows.append(("速度环 ωn", wn_vel, "rad/s", "sqrt(Ki·Kt/J)",
                         "应 ≤ 电流环 ωn / 5 (内环带宽隔离)"))
            rows.append(("速度环 ζ", zeta_vel, "", "Kp / (2·sqrt(Ki·Kt·J))",
                         "目标 0.7~1.0; <0.7 速度超调, >1.0 跟随迟钝"))

        # ===== 9. 位置环 P 动态 =====
        if pl.position_kp * mb.kt > 0:
            wn_pos = math.sqrt(pl.position_kp * mb.kt / J)
            rows.append(("位置环 ωn", wn_pos, "rad/s", "sqrt(Kp_pos·Kt/J)",
                         "应 ≤ 速度环 ωn / 5; 决定位置收敛速度"))

        # ===== 10. 阻抗控制 (MIT) 动态 =====
        if ic.impedance_kp > 0:
            wn_mit = math.sqrt(ic.impedance_kp / J)
            zeta_mit = safe_div(ic.impedance_kd, 2 * math.sqrt(ic.impedance_kp * J), 0)
            rows.append(("阻抗环 ωn", wn_mit, "rad/s", "sqrt(Kp_imp/J)",
                         "MIT 自然频率; 决定关节刚度"))
            rows.append(("阻抗环 ζ", zeta_mit, "", "Kd / (2·sqrt(Kp_imp·J))",
                         "<1 欠阻尼超调; >1 过阻尼无超调; 目标 0.7~1.0"))

        # ===== 11. 机械加速度与转速换算 =====
        max_accel = safe_div(mb.peak_torque, J, 0)
        rows.append(("最大理论加速度 α", max_accel, "rad/s²", "peak_torque / J",
                     "空载电机端理论值; 实际受电流环/摩擦限制"))
        max_rpm = mb.max_speed * 60 / (2 * math.pi)
        rows.append(("最大转速", max_rpm, "RPM", "max_speed · 60 / (2π)",
                     "电机端 RPM; 输出端需除以减速比"))

        # ===== 12. 减速器折算 =====
        out_max_speed = safe_div(mb.max_speed, gb.gear_ratio, 0)
        out_max_torque = mb.peak_torque * gb.gear_ratio * gb.gear_efficiency
        out_inertia = J * gb.gear_ratio ** 2
        rows.append(("输出端最大转速", out_max_speed, "rad/s", "max_speed / N",
                     "减速后输出轴转速; N=gear_ratio"))
        rows.append(("输出端最大转矩", out_max_torque, "Nm", "peak_torque · N · η",
                     "减速后输出转矩; η=gear_efficiency"))
        rows.append(("输出端惯量折算", out_inertia, "kg·m²", "J · N²",
                     "电机端惯量折算到输出端; 决定输出端响应"))

        # ===== 13. 热模型 =====
        i_peak = mb.peak_current
        p_cu = i_peak ** 2 * mb.r
        t_rise_steady = th.thermal_resistance * p_cu
        t_steady = t_rise_steady + 30.0
        temp_margin = th.motor_over_temp_threshold - t_steady
        rows.append(("峰值电流铜损", p_cu, "W", "I_peak² · R",
                     "电流热效应主要来源; 与电流平方成正比"))
        rows.append(("峰值电流稳态温升", t_rise_steady, "K", "Rth · I² · R",
                     "高于环境温度的稳态温升; 环境按 30°C"))
        rows.append(("峰值电流稳态温度", t_steady, "°C", "T_amb + Rth·I²·R",
                     "应 < 电机过温阈值, 否则峰值电流不可持续"))
        rows.append(("过温裕度", temp_margin, "K", "T_阈值 − T_稳态",
                     "<0 表示峰值电流下会过温; >0 安全裕度"))

        # ===== 14. 电源功率与效率上限 =====
        p_max_elec = mb.rated_voltage * mb.peak_current
        p_max_mech = mb.peak_torque * mb.max_speed
        eff_limit = safe_div(p_max_mech, p_max_elec, 0) * 100
        rows.append(("母线峰值功率", p_max_elec, "W", "Vrated · I_peak",
                     "电源最大输入功率; 决定电源选型"))
        rows.append(("机械峰值功率", p_max_mech, "W", "T_peak · ωmax",
                     "电机最大机械输出功率"))
        rows.append(("功率上限比", eff_limit, "%", "P_mech / P_elec × 100",
                     "理论最大效率上限; 实际效率 ≤ 此值"))

        # ===== 15. PWM 与死区 =====
        period_ns = 1e9 / mb.pwm_freq_hz
        dt_ratio = mb.dead_time_ns / period_ns * 100
        rows.append(("PWM 周期", period_ns, "ns", "1e9 / fpwm",
                     "载波周期; 20kHz → 50μs"))
        rows.append(("死区占比", dt_ratio, "%", "td / Tpwm × 100",
                     "<5% 为佳; 过大影响低压精度与线性度"))

        # ===== 16. 回零时间估算 =====
        travel = max(abs(plm.pos_min_limit), abs(plm.pos_max_limit)) / 2
        t_fast = safe_div(travel, max(hm.homing_speed_fast, 1e-6), 0)
        t_slow = safe_div(abs(hm.homing_offset), max(hm.homing_speed_slow, 1e-6), 0)
        t_homing = t_fast + t_slow
        rows.append(("回零时间估算", t_homing, "s", "行程/v_fast + 偏置/v_slow",
                     "粗略估算, 含快速移动 + 慢速对齐; 不含找原点信号时间"))

        # ===== 17. FOC 控制周期 =====
        foc_period_us = 1e6 / mb.foc_freq_hz
        rows.append(("FOC 周期", foc_period_us, "μs", "1e6 / fFOC",
                     "FOC 控制周期; 20kHz → 50μs; 越小电流环带宽越高"))

        # ===== 18. 跟随误差与位置环带宽匹配 =====
        if pl.position_kp > 0 and mb.max_speed > 0:
            follow_err_steady = safe_div(mb.max_speed, pl.position_kp, 0)
            rows.append(("最大转速稳态跟随误差", follow_err_steady, "rad", "ωmax / Kp_pos",
                         "P 位置环在最大转速下的稳态跟随误差; 应 < follow_err 阈值"))

        # 填充表格 (5 列: 参数 / 数值 / 单位 / 公式 / 备注)
        self._derived_table.setRowCount(len(rows))
        for r, (name, val, unit, formula, remark) in enumerate(rows):
            self._fill_derived_row(self._derived_table, r,
                                    name, f"{val:.4g}", unit, formula, remark)
        # 自适应行高(让备注可换行显示)
        self._derived_table.resizeRowsToContents()

    # ---------------- 回调 ----------------
    def _on_any_changed(self):
        """任意编辑器值变化: 即时写回 MotorParam (给定/控制) 或 FaultDetector/PositionLimit (保护)。

        高级参数使能未勾选时, 对应字段被覆盖为禁用值, 不影响控制。
        """
        if self._guard or self._mp is None:
            return
        # 给定参数写回 MotorParam
        self._writeback_to_motorparam(self._given_widgets)
        # 控制参数写回 MotorParam (PID/阻抗/回零 与给定同源, 只是 tab 不同)
        self._writeback_to_motorparam(self._control_widgets)
        # 高级参数覆盖: 未使能的高级参数强制设为禁用值
        self._apply_advanced_overrides()
        # 保护参数写回 (按 src 区分)
        if self._engine is not None:
            th = self._engine.fault_detector.th
            plm = self._mp.position_limit
            # 找到 spec.src 映射 (key -> src)
            key_to_src = {s.key: s.src for s in _PROTECT_FIELDS}
            for key, w in self._protect_widgets.items():
                src = key_to_src.get(key, "th")
                if isinstance(w, QSpinBox):
                    v = int(w.value())
                else:
                    v = float(w.value())
                if src == "th" and hasattr(th, key):
                    setattr(th, key, v)
                elif src == "pos_limit" and hasattr(plm, key):
                    setattr(plm, key, v)
                    # 同步更新状态机的限位 (SystemStateMachine 持有副本)
                    if self._engine is not None and hasattr(self._engine, 'fsm'):
                        fsm = self._engine.fsm
                        if hasattr(fsm, 'pos_limit_min'):
                            fsm.pos_limit_min = plm.pos_min_limit
                            fsm.pos_limit_max = plm.pos_max_limit
        # 刷新推导表 (孪生推导 + 控制参数理论推导)
        self._refresh_derived()
        self._refresh_control_derived()
        self.params_changed.emit()

    def _on_adv_toggled(self, enable_key: str, state: int):
        """高级参数使能勾选状态变化: 切换高级组显隐 + 触发写回。"""
        enabled = state == Qt.CheckState.Checked.value or state == 2
        self._adv_enabled[enable_key] = enabled
        # 切换对应高级组的可见性
        grp = self._adv_groups.get(enable_key)
        if grp is not None:
            grp.setVisible(enabled)
        # 立即触发写回 (会应用覆盖逻辑: 未使能 → 禁用值)
        self._on_any_changed()

    def _apply_advanced_overrides(self):
        """对未使能的高级参数, 把 MotorParam 中对应字段覆盖为禁用值。

        禁用值定义在 _ADVANCED_ENABLES.disable_overrides 中。
        覆盖发生在写回之后, 故 UI 仍保留用户输入值, 但 MotorParam 实际生效的是禁用值。
        """
        for spec in _ADVANCED_ENABLES:
            if not self._adv_enabled.get(spec.enable_key, False):
                # 未使能: 覆盖为禁用值
                for (sub, attr), disable_val in spec.disable_overrides.items():
                    try:
                        sub_obj = getattr(self._mp, sub)
                        setattr(sub_obj, attr, disable_val)
                    except AttributeError:
                        pass

    def _writeback_to_motorparam(self, widget_dict):
        """把 (sub, attr) -> widget 的字典写回 MotorParam 对应字段。"""
        for (sub, attr), w in widget_dict.items():
            try:
                sub_obj = getattr(self._mp, sub)
                if isinstance(w, QSpinBox):
                    setattr(sub_obj, attr, int(w.value()))
                else:
                    setattr(sub_obj, attr, float(w.value()))
            except AttributeError:
                pass

    def _refresh_control_derived(self):
        """刷新控制参数 tab 底部的理论推导表格。"""
        tbl = getattr(self, '_ctrl_derived_table', None)
        if tbl is None or self._mp is None:
            return
        rows = _control_derived_rows(self._mp)
        tbl.setRowCount(len(rows))
        for r, (name, val, unit, formula, remark) in enumerate(rows):
            self._fill_derived_row(tbl, r, name, f"{val:.4g}", unit, formula, remark)
        tbl.resizeRowsToContents()

    def _on_apply(self):
        """应用按钮: 触发引擎重新加载 PID 配置(让控制器增益/限幅实时生效)。"""
        if self._engine is None:
            return
        # 先应用高级参数覆盖 (未使能的高级参数 → 禁用值)
        self._apply_advanced_overrides()
        self._engine.reload_params()
        # 物理缓存(如 R/Ld)在 MotorPhysics.__init__ 中缓存, 部分需重建才生效;
        # 这里仅触发控制器侧 reload, 不重建物理模型(避免运行中状态丢失)
        self._refresh_derived()
        self._refresh_control_derived()

    def _on_recalc_protect(self):
        """从给定参数重新推导保护阈值默认值。"""
        if self._engine is None or self._mp is None:
            return
        self._engine.fault_detector._load_thresholds()
        self._reload_protect()
        self._on_any_changed()

    def _on_reset_default(self):
        """重置 MotorParam 为内置默认值。"""
        if self._engine is None:
            return
        from transport.virtual_engine.twin_config import MotorParam, load_motor_param
        # 重建默认 MotorParam (不读 CSV)
        new_mp = MotorParam()
        # 复制字段到现有 mp (保留对象引用, 避免引擎其它模块丢失引用)
        for fname in self._mp.__dataclass_fields__:
            sub_old = getattr(self._mp, fname)
            sub_new = getattr(new_mp, fname)
            if hasattr(sub_old, '__dataclass_fields__'):
                for a in sub_old.__dataclass_fields__:
                    setattr(sub_old, a, getattr(sub_new, a))
            else:
                setattr(self._mp, fname, getattr(new_mp, fname))
        # 重新加载阈值
        self._engine.fault_detector._load_thresholds()
        self._engine.reload_params()
        self._reload_given()
        self._reload_control()
        self._reload_protect()
        # 重置后应用高级覆盖 (默认未使能)
        self._apply_advanced_overrides()
        self._refresh_derived()
        self._refresh_control_derived()
        self.params_changed.emit()

    # ---------------- 导入/导出 ----------------
    _PROFILE_DIR = None  # 记忆上次目录(类变量, 跨实例共享)

    def _default_profile_dir(self) -> str:
        """默认配置文件目录: resources/twin_profiles/"""
        here = os.path.dirname(os.path.abspath(__file__))
        d = os.path.normpath(os.path.join(here, '..', '..', 'resources', 'twin_profiles'))
        return d

    def _on_export(self):
        """导出参数到 JSON 文件。"""
        if self._engine is None or self._mp is None:
            return
        from PyQt6.QtWidgets import QMessageBox
        # 选择导出范围
        items = ["全部参数 (给定+控制+保护)", "仅给定参数", "仅控制参数", "仅保护参数"]
        item, ok = QInputDialog.getItem(
            self, "导出参数", "选择导出范围:", items, 0, False)
        if not ok:
            return
        section_map = {
            items[0]: 'all', items[1]: 'given',
            items[2]: 'control', items[3]: 'protect',
        }
        section = section_map.get(item, 'all')
        # 默认文件名
        default_name = {
            'all': 'twin_profile.json',
            'given': 'given_params.json',
            'control': 'control_params.json',
            'protect': 'protect_params.json',
        }[section]
        start_dir = self._PROFILE_DIR or self._default_profile_dir()
        path, _ = QFileDialog.getSaveFileName(
            self, "导出参数文件", os.path.join(start_dir, default_name),
            "JSON 文件 (*.json)")
        if not path:
            return
        # 记忆目录
        TwinParamPanel._PROFILE_DIR = os.path.dirname(path)
        # 导出
        from transport.virtual_engine.twin_config import mp_to_file
        if mp_to_file(self._mp, path, section):
            QMessageBox.information(self, "导出成功", f"参数已导出到:\n{path}")
        else:
            QMessageBox.critical(self, "导出失败", f"写入文件失败:\n{path}")

    def _on_import(self):
        """从 JSON 文件导入参数。"""
        if self._engine is None or self._mp is None:
            return
        from PyQt6.QtWidgets import QMessageBox
        start_dir = self._PROFILE_DIR or self._default_profile_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, "导入参数文件", start_dir, "JSON 文件 (*.json)")
        if not path:
            return
        # 记忆目录
        TwinParamPanel._PROFILE_DIR = os.path.dirname(path)
        # 检测文件中包含哪些section(通过字段名自动判断)
        import json as _json
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = _json.load(f)
        except (OSError, _json.JSONDecodeError) as e:
            QMessageBox.critical(self, "导入失败", f"读取文件失败:\n{e}")
            return
        # 自动判断section: 检查文件包含哪些子结构
        from transport.virtual_engine.twin_config import (
            GIVEN_SUBS, CONTROL_SUBS, PROTECT_SUBS, mp_from_dict,
        )
        has_given = any(s in data for s in GIVEN_SUBS)
        has_control = any(s in data for s in CONTROL_SUBS)
        has_protect = any(s in data for s in PROTECT_SUBS)
        if not (has_given or has_control or has_protect):
            QMessageBox.critical(self, "导入失败", "文件中未识别到有效参数字段")
            return
        # 确认导入(会重建引擎)
        sections = []
        if has_given:
            sections.append("给定参数")
        if has_control:
            sections.append("控制参数")
        if has_protect:
            sections.append("保护参数")
        msg = (f"将导入: {' + '.join(sections)}\n"
               f"来源: {path}\n\n"
               f"注意: 导入给定参数会重建引擎(电机状态将归零)。\n"
               f"继续?")
        reply = QMessageBox.question(
            self, "确认导入", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        # 逐section导入(就地写入mp)
        ok = False
        if has_given:
            ok = mp_from_dict(self._mp, data, 'given') or ok
        if has_control:
            ok = mp_from_dict(self._mp, data, 'control') or ok
        if has_protect:
            ok = mp_from_dict(self._mp, data, 'protect') or ok
        if not ok:
            QMessageBox.critical(self, "导入失败", "参数写入失败")
            return
        # 生效处理
        # 保护参数: 重算阈值 (protection_param → th)
        self._engine.fault_detector._load_thresholds()
        # 同步 fsm 位置限位
        self._engine.fsm.pos_limit_min = self._mp.position_limit.pos_min_limit
        self._engine.fsm.pos_limit_max = self._mp.position_limit.pos_max_limit
        # 给定参数变更 → 重建引擎(物理模型用新参数)
        if has_given:
            self._engine.rebuild()
            # rebuild 后 fault_detector/fsm 是新对象, 重新引用
            # 面板的 _mp 仍是原 mp 对象(rebuild 保留引用)
        else:
            # 仅控制/保护参数: 热加载 PID 即可
            self._engine.reload_params()
        # 刷新UI
        self._guard = True
        self._reload_given()
        self._reload_control()
        self._reload_protect()
        self._guard = False
        self._apply_advanced_overrides()
        self._refresh_derived()
        self._refresh_control_derived()
        self.params_changed.emit()
        QMessageBox.information(
            self, "导入成功",
            f"已导入: {' + '.join(sections)}\n"
            f"{'引擎已重建(给定参数生效)' if has_given else '参数已热加载'}")

    # ---------------- 主题 ----------------
    def apply_theme(self):
        self._hint_label.setStyleSheet(
            f"color: {theme.hex('warn')}; padding: 4px; font-weight: bold;")
        # 表格也跟随
        self._refresh_derived()
        self._refresh_control_derived()

    # ---------------- 样式 ----------------
    def _btn_style(self) -> str:
        return f"""
            QPushButton {{
                padding: 4px 12px;
                border: 1px solid {theme.hex('btn_border')};
                border-radius: 4px;
                background: {theme.hex('btn_bg')};
                color: {theme.hex('btn_text')};
                font-size: 12px;
            }}
            QPushButton:hover {{
                background: {theme.hex('btn_hover')};
                border-color: {theme.hex('accent')};
            }}
        """

    def _btn_accent_style(self) -> str:
        return f"""
            QPushButton {{
                padding: 4px 12px;
                border: 1px solid {theme.hex('accent')};
                border-radius: 4px;
                background: #2E5C44;
                color: {theme.hex('accent')};
                font-weight: bold;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background: #3A7358;
            }}
        """
