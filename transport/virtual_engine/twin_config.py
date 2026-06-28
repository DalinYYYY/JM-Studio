"""数字孪生参数配置: 从固件 motor_param.csv 加载 83 项参数。

忠实映射固件 motor_param_t 的全部子结构(motor_base/gearbox/encoder/...)。
孪生物理模型与控制器均从此处读取参数, 实现与固件"同参数源"。

数据源优先级:
  1. 固件 User/Tools/motor_param_gen/motor_param.csv (含 default_value, 权威)
  2. 上位机 resources/motor_info.csv
  3. 内置硬编码默认值(CSV 缺失时回退)

参数读写按 param_id(1~83), 与协议 PARAM_READ/PARAM_WRITE 对齐。
"""

import os
import csv
import json
from dataclasses import dataclass, field, asdict, is_dataclass


# ---- 固件 CSV 路径(相对上位机工程向上回溯到固件 User) ----
_HERE = os.path.dirname(os.path.abspath(__file__))
_FW_PARAM_CSV = os.path.normpath(os.path.join(
    _HERE, '..', '..', '..', '..', 'Tools', 'motor_param_gen', 'motor_param.csv'))
_GUI_PARAM_CSV = os.path.join(_HERE, '..', '..', '..', 'resources', 'motor_info.csv')


@dataclass
class MotorInstance:
    motor_id: int = 0
    motor_name: str = "TwinMotor"


@dataclass
class MotorBase:
    r: float = 0.1              # 定子相电阻 ohm
    ld: float = 0.0001           # d轴电感 H
    lq: float = 0.00012          # q轴电感 H
    flux: float = 0.001          # 磁链 Wb
    kt: float = 0.1              # 转矩常数 Nm/A
    pole_pairs: int = 7          # 极对数
    rated_current: float = 5.0   # 额定电流 A
    peak_current: float = 15.0   # 峰值电流 A
    max_speed: float = 300.0     # 最大转速 rad/s
    dead_time_ns: float = 500.0  # PWM死区 ns
    rated_voltage: float = 48.0  # 额定电压 V
    rated_speed_rpm: float = 3000.0
    rated_torque: float = 2.0     # 额定转矩 Nm
    peak_torque: float = 6.0      # 峰值转矩 Nm
    inertia: float = 1e-5         # 转子惯量 kg*m^2
    ke: float = 0.01              # 反电动势常数 V/(rad/s)
    pwm_freq_hz: int = 20000      # PWM载波 Hz
    foc_freq_hz: int = 20000      # FOC频率 Hz


@dataclass
class GearboxParam:
    gear_ratio: float = 100.0          # 减速比
    gear_efficiency: float = 0.85      # 效率
    output_torque_const: float = 10.0  # 输出转矩常数 Nm/A
    gear_backlash: float = 0.01       # 回程间隙 rad


@dataclass
class EncoderParam:
    enc_lines: int = 4000          # CPR
    enc_direction: int = 1         # 1=正向 -1=反向
    enc_offset: int = 0            # counts
    elec_angle_bias: float = 0.0   # rad
    pos_filter_alpha: float = 0.1
    enc_type: int = 0              # 0=ABZ 1=SPI 2=BiSS 3=霍尔
    enc_auto_calib: int = 1
    speed_obs_gain: float = 100.0  # PLL观测器增益


@dataclass
class PositionLimit:
    multiturn_enable: int = 1
    pos_min_limit: float = -12.566  # rad
    pos_max_limit: float = 12.566
    limit_sw_enable: int = 0


@dataclass
class HomingParam:
    homing_method: int = 0
    homing_speed_fast: float = 5.0   # rad/s
    homing_speed_slow: float = 0.5   # rad/s
    homing_offset: float = 0.0       # rad
    homing_current: float = 2.0       # A


@dataclass
class CurrentLoop:
    current_kp_d: float = 0.5         # V/A
    current_ki_d: float = 10.0         # V/(A*s)
    current_kp_q: float = 0.5
    current_ki_q: float = 10.0
    current_integral_limit: float = 10.0  # V
    decoupling_gain: float = 1.0
    deadtime_comp_v: float = 0.0       # V
    pwm_max_duty: float = 0.9
    current_bandwidth_hz: float = 1000.0
    current_filter_alpha: float = 0.1
    d_feedforward_gain: float = 1.0
    q_feedforward_gain: float = 1.0


@dataclass
class PositionLoop:
    speed_kp: float = 0.1              # A/(rad/s)
    speed_ki: float = 1.0              # A/rad
    speed_integral_limit: float = 10.0  # A
    velocity_ff_gain: float = 1.0
    accel_ff_gain: float = 0.0
    position_kp: float = 10.0          # Hz
    position_integral_limit: float = 0.0  # rad
    friction_coulomb: float = 0.0      # Nm
    friction_viscous: float = 0.0      # Nm/(rad/s)
    notch_freq_hz: float = 200.0
    notch_width_hz: float = 20.0
    notch_depth_db: float = 40.0
    notch_enable: int = 0
    speed_bandwidth_hz: float = 100.0
    speed_filter_alpha: float = 0.05
    position_bandwidth_hz: float = 20.0
    follow_error_threshold: float = 5.0   # 跟随误差阈值 rad (堵转/失步检测, 正常运动不触发)
    max_accel: float = 0.0   # 加速度限制(0=禁用); 电机端反馈后带宽匹配, 无需限幅防震荡


@dataclass
class ImpedanceCtrl:
    impedance_kp: float = 10.0   # Nm/rad
    impedance_kd: float = 0.1    # Nm/(rad/s)
    iq_max: float = 10.0         # A


@dataclass
class ThermalModel:
    thermal_resistance: float = 1.0   # K/W
    thermal_time_const: float = 60.0   # s
    derating_temp_start: float = 70.0  # C
    fet_over_temp_threshold: float = 80.0      # FET过温阈值 C
    motor_over_temp_threshold: float = 90.0     # 电机过温阈值 C


@dataclass
class ProtectionParam:
    protect_over_current: float = 20.0    # A
    protect_over_voltage: float = 58.0     # V
    protect_under_voltage: float = 15.0   # V
    protect_over_speed: float = 400.0      # rad/s
    protect_over_temp: float = 85.0        # C
    protect_under_temp: float = -20.0      # C
    protect_pos_error: int = 1000          # counts
    protect_enable_mask: int = 0xFFFFFFFF


@dataclass
class MotorParam:
    """与固件 motor_param_t 一一对应的完整参数集。"""
    motor_instance: MotorInstance = field(default_factory=MotorInstance)
    motor_base: MotorBase = field(default_factory=MotorBase)
    gearbox_param: GearboxParam = field(default_factory=GearboxParam)
    encoder_param: EncoderParam = field(default_factory=EncoderParam)
    position_limit: PositionLimit = field(default_factory=PositionLimit)
    homing_param: HomingParam = field(default_factory=HomingParam)
    current_loop: CurrentLoop = field(default_factory=CurrentLoop)
    position_loop: PositionLoop = field(default_factory=PositionLoop)
    impedance_ctrl: ImpedanceCtrl = field(default_factory=ImpedanceCtrl)
    thermal_model: ThermalModel = field(default_factory=ThermalModel)
    protection_param: ProtectionParam = field(default_factory=ProtectionParam)


# ---- param_id -> (子结构属性名, 数据类型) 映射表 ----
# 与 motor_param.csv 的 id/param_name 严格对齐
_DTYPE_INT = {'uint8_t', 'int8_t', 'uint8', 'int8', 'uint16_t', 'int16_t',
              'uint16', 'int16', 'uint32_t', 'int32_t', 'uint32', 'int32'}
_DTYPE_FLOAT = {'float', 'float32', 'single'}


def _build_param_map():
    """构造 param_id -> (dataclass实例引用, 属性名, dtype_str) 映射。"""
    # (csv param_name, 子结构对象变量名, 属性名)
    # 子结构变量名在 MotorParam 中的字段
    entries = [
        # MotorInstance
        (1, 'motor_id', 'motor_instance'), (2, 'motor_name', 'motor_instance'),
        # MotorBase
        (3, 'r', 'motor_base'), (4, 'ld', 'motor_base'), (5, 'lq', 'motor_base'),
        (6, 'flux', 'motor_base'), (7, 'kt', 'motor_base'), (8, 'pole_pairs', 'motor_base'),
        (9, 'rated_current', 'motor_base'), (10, 'peak_current', 'motor_base'),
        (11, 'max_speed', 'motor_base'), (12, 'dead_time_ns', 'motor_base'),
        (13, 'rated_voltage', 'motor_base'), (14, 'rated_speed_rpm', 'motor_base'),
        (15, 'rated_torque', 'motor_base'), (16, 'peak_torque', 'motor_base'),
        (17, 'inertia', 'motor_base'), (18, 'ke', 'motor_base'),
        (19, 'pwm_freq_hz', 'motor_base'), (20, 'foc_freq_hz', 'motor_base'),
        # Gearbox
        (21, 'gear_ratio', 'gearbox_param'), (22, 'gear_efficiency', 'gearbox_param'),
        (23, 'output_torque_const', 'gearbox_param'), (24, 'gear_backlash', 'gearbox_param'),
        # Encoder
        (25, 'enc_lines', 'encoder_param'), (26, 'enc_direction', 'encoder_param'),
        (27, 'enc_offset', 'encoder_param'), (28, 'elec_angle_bias', 'encoder_param'),
        (29, 'pos_filter_alpha', 'encoder_param'), (30, 'enc_type', 'encoder_param'),
        (31, 'enc_auto_calib', 'encoder_param'), (32, 'speed_obs_gain', 'encoder_param'),
        # PositionLimit
        (33, 'multiturn_enable', 'position_limit'), (34, 'pos_min_limit', 'position_limit'),
        (35, 'pos_max_limit', 'position_limit'), (36, 'limit_sw_enable', 'position_limit'),
        # Homing
        (37, 'homing_method', 'homing_param'), (38, 'homing_speed_fast', 'homing_param'),
        (39, 'homing_speed_slow', 'homing_param'), (40, 'homing_offset', 'homing_param'),
        (41, 'homing_current', 'homing_param'),
        # CurrentLoop
        (42, 'current_kp_d', 'current_loop'), (43, 'current_ki_d', 'current_loop'),
        (44, 'current_kp_q', 'current_loop'), (45, 'current_ki_q', 'current_loop'),
        (46, 'current_integral_limit', 'current_loop'), (47, 'decoupling_gain', 'current_loop'),
        (48, 'deadtime_comp_v', 'current_loop'), (49, 'pwm_max_duty', 'current_loop'),
        (50, 'current_bandwidth_hz', 'current_loop'), (51, 'current_filter_alpha', 'current_loop'),
        (52, 'd_feedforward_gain', 'current_loop'), (53, 'q_feedforward_gain', 'current_loop'),
        # PositionLoop
        (54, 'speed_kp', 'position_loop'), (55, 'speed_ki', 'position_loop'),
        (56, 'speed_integral_limit', 'position_loop'), (57, 'velocity_ff_gain', 'position_loop'),
        (58, 'accel_ff_gain', 'position_loop'), (59, 'position_kp', 'position_loop'),
        (60, 'position_integral_limit', 'position_loop'), (61, 'friction_coulomb', 'position_loop'),
        (62, 'friction_viscous', 'position_loop'), (63, 'notch_freq_hz', 'position_loop'),
        (64, 'notch_width_hz', 'position_loop'), (65, 'notch_depth_db', 'position_loop'),
        (66, 'notch_enable', 'position_loop'), (67, 'speed_bandwidth_hz', 'position_loop'),
        (68, 'speed_filter_alpha', 'position_loop'), (69, 'position_bandwidth_hz', 'position_loop'),
        # Impedance
        (70, 'impedance_kp', 'impedance_ctrl'), (71, 'impedance_kd', 'impedance_ctrl'),
        (72, 'iq_max', 'impedance_ctrl'),
        # Thermal
        (73, 'thermal_resistance', 'thermal_model'), (74, 'thermal_time_const', 'thermal_model'),
        (75, 'derating_temp_start', 'thermal_model'),
        # Protection
        (76, 'protect_over_current', 'protection_param'), (77, 'protect_over_voltage', 'protection_param'),
        (78, 'protect_under_voltage', 'protection_param'), (79, 'protect_over_speed', 'protection_param'),
        (80, 'protect_over_temp', 'protection_param'), (81, 'protect_under_temp', 'protection_param'),
        (82, 'protect_pos_error', 'protection_param'), (83, 'protect_enable_mask', 'protection_param'),
    ]
    return entries


_PARAM_ENTRIES = _build_param_map()


def _parse_csv_value(raw: str, dtype: str):
    """把 CSV default_value 字符串按 dtype 转成 Python 值。"""
    dtype_lower = dtype.strip().lower()
    raw = raw.strip()
    if dtype_lower in _DTYPE_FLOAT:
        return float(raw) if raw else 0.0
    if dtype_lower in _DTYPE_INT:
        try:
            return int(raw, 0) if raw else 0
        except ValueError:
            return int(float(raw)) if raw else 0
    if dtype_lower.startswith('char'):
        return raw
    # 兜底: 尝试浮点
    try:
        return float(raw) if raw else 0.0
    except ValueError:
        return raw


def load_motor_param(csv_path: str = None) -> MotorParam:
    """从固件 motor_param.csv 加载参数, 失败则返回内置默认值。"""
    mp = MotorParam()

    path = csv_path or _FW_PARAM_CSV
    if not os.path.isfile(path):
        path = _GUI_PARAM_CSV
    if not os.path.isfile(path):
        return mp  # 内置默认值

    try:
        with open(path, encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            name_to_entry = {(pid, attr): (sub, attr) for pid, attr, sub in _PARAM_ENTRIES}
            pid_to_entry = {pid: (attr, sub) for pid, attr, sub in _PARAM_ENTRIES}
            for row in reader:
                pid_str = (row.get('id') or row.get('param_id') or '').strip()
                if not pid_str.isdigit():
                    continue
                pid = int(pid_str)
                if pid not in pid_to_entry:
                    continue
                attr, sub_name = pid_to_entry[pid]
                dtype = row.get('data_type', 'float')
                raw_val = row.get('default_value', '')
                try:
                    val = _parse_csv_value(raw_val, dtype)
                except (ValueError, TypeError):
                    continue
                sub_obj = getattr(mp, sub_name)
                setattr(sub_obj, attr, val)
    except Exception:
        pass  # 解析失败保留默认值
    return mp


# ==================== 参数分组定义(用于导入导出) ====================
# 给定参数: 电机物理本体相关子结构
GIVEN_SUBS = ('motor_base', 'gearbox_param', 'encoder_param', 'thermal_model')
# 控制参数: 控制环路相关子结构
CONTROL_SUBS = ('current_loop', 'position_loop', 'impedance_ctrl', 'homing_param')
# 保护参数: 保护与限位相关子结构
PROTECT_SUBS = ('protection_param', 'position_limit')

# 配置文件版本号
PROFILE_VERSION = 1


# ==================== 序列化/反序列化 ====================
def _sub_to_dict(sub_obj) -> dict:
    """单个子 dataclass → dict (仅保留实际字段, 过滤内部状态)。"""
    if not is_dataclass(sub_obj):
        return sub_obj
    out = {}
    for fname in sub_obj.__dataclass_fields__:
        out[fname] = getattr(sub_obj, fname)
    return out


def _sub_from_dict(sub_obj, data: dict):
    """dict → 单个子 dataclass (就地写入, 保留对象引用)。

    仅写入 dataclass 中已存在的字段; 忽略 data 中多余的字段。
    """
    if not is_dataclass(sub_obj) or not isinstance(data, dict):
        return
    for fname in sub_obj.__dataclass_fields__:
        if fname in data:
            setattr(sub_obj, fname, data[fname])


def mp_to_dict(mp: MotorParam, section: str = 'all') -> dict:
    """将 MotorParam 导出为 dict。

    Args:
        section: 'all' / 'given' / 'control' / 'protect'
    """
    if section == 'given':
        subs = GIVEN_SUBS
    elif section == 'control':
        subs = CONTROL_SUBS
    elif section == 'protect':
        subs = PROTECT_SUBS
    else:  # all
        subs = tuple(mp.__dataclass_fields__.keys())

    out = {'version': PROFILE_VERSION}
    if section == 'all':
        out['motor_name'] = mp.motor_instance.motor_name
    for sub_name in subs:
        sub_obj = getattr(mp, sub_name)
        out[sub_name] = _sub_to_dict(sub_obj)
    return out


def mp_from_dict(mp: MotorParam, data: dict, section: str = 'all') -> bool:
    """从 dict 导入参数到 MotorParam (就地写入, 保留对象引用)。

    Args:
        mp: 目标 MotorParam (就地修改)
        data: 字典数据
        section: 'all' / 'given' / 'control' / 'protect'
    Returns:
        True 表示成功写入至少一个字段
    """
    if not isinstance(data, dict):
        return False
    if section == 'given':
        subs = GIVEN_SUBS
    elif section == 'control':
        subs = CONTROL_SUBS
    elif section == 'protect':
        subs = PROTECT_SUBS
    else:  # all
        subs = tuple(mp.__dataclass_fields__.keys())

    written = False
    for sub_name in subs:
        if sub_name in data:
            sub_obj = getattr(mp, sub_name, None)
            if sub_obj is not None:
                _sub_from_dict(sub_obj, data[sub_name])
                written = True
    return written


def mp_to_json(mp: MotorParam, section: str = 'all', indent: int = 2) -> str:
    """导出为 JSON 字符串。"""
    return json.dumps(mp_to_dict(mp, section), indent=indent, ensure_ascii=False)


def mp_from_json(mp: MotorParam, json_str: str, section: str = 'all') -> bool:
    """从 JSON 字符串导入。"""
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return False
    return mp_from_dict(mp, data, section)


def mp_to_file(mp: MotorParam, path: str, section: str = 'all') -> bool:
    """导出到 JSON 文件。"""
    try:
        data = mp_to_dict(mp, section)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except (OSError, TypeError, ValueError):
        return False


def mp_from_file(mp: MotorParam, path: str, section: str = 'all') -> bool:
    """从 JSON 文件导入。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    return mp_from_dict(mp, data, section)


class ParamAccessor:
    """按 param_id 读写参数, 供协议应答器调用。"""

    def __init__(self, motor_param: MotorParam = None):
        self.mp = motor_param or load_motor_param()
        self._pid_map = {pid: (attr, sub) for pid, attr, sub in _PARAM_ENTRIES}

    def get(self, param_id: int):
        """按 param_id 读取参数值, 返回 (value, dtype_str) 或 None。"""
        if param_id not in self._pid_map:
            return None
        attr, sub_name = self._pid_map[param_id]
        sub_obj = getattr(self.mp, sub_name)
        val = getattr(sub_obj, attr)
        return val

    def set(self, param_id: int, value) -> bool:
        """按 param_id 写入参数值, 返回是否成功。"""
        if param_id not in self._pid_map:
            return False
        attr, sub_name = self._pid_map[param_id]
        sub_obj = getattr(self.mp, sub_name)
        # 类型转换: 整型字段强制转 int
        cur = getattr(sub_obj, attr)
        if isinstance(cur, int) and not isinstance(cur, bool):
            try:
                value = int(value)
            except (ValueError, TypeError):
                value = int(float(value))
        else:
            try:
                value = float(value)
            except (ValueError, TypeError):
                pass
        setattr(sub_obj, attr, value)
        return True

    def get_dtype(self, param_id: int) -> str:
        """返回参数的 dtype 短名(u8/u16/u32/i32/f32), 供协议打包。"""
        attr, sub_name = self._pid_map.get(param_id, (None, None))
        if attr is None:
            return 'f32'
        val = getattr(getattr(self.mp, sub_name), attr)
        if isinstance(val, bool) or isinstance(val, int):
            # 区分 8/16/32 位: 简化为 u32/i32
            return 'i32' if isinstance(val, int) and val < 0 else 'u32'
        return 'f32'

    def get_code_name(self, param_id: int) -> str:
        """返回参数代码名(如 r, ld, pole_pairs)。"""
        if param_id not in self._pid_map:
            return ''
        return self._pid_map[param_id][0]
