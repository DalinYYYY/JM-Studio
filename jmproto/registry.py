"""
协议数据驱动注册表: 运行时加载命令表/参数表 CSV, 解析成结构化规格供 UI 与打包使用。

数据源(随工具携带): resources/joint_motor_command_list.csv, resources/joint_motor_param_index.csv
CSV 缺失或解析异常时回退到内置最小命令集, 保证无 CSV 也能运行。
"""

import os
import re
import csv
import math

from . import codec
from .cmd_def import JmCmd


_RES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'resources')
_CMD_CSV = os.path.join(_RES_DIR, 'joint_motor_command_list.csv')
_PARAM_CSV = os.path.join(_RES_DIR, 'joint_motor_param_index.csv')

# 固件维护的配置字段；加载旧 CSV 时也不能把它们恢复成可写。
MOTOR_CONFIG_READONLY_NAMES = frozenset({
    'config_version', 'total_runtime_s', 'save_count', 'is_calibrated',
    'pid_source_mask',
})
MOTOR_CONFIG_READONLY_IDS = frozenset({0, 4, 5, 16, 127})
_MOTOR_INFO_CSV = os.path.join(_RES_DIR, 'motor_info.csv')
# 故障码表(fault_param_gen 生成副本, 12列, 行号=index 固件码表序)
_FAULT_PARAM_CSV = os.path.join(_RES_DIR, 'fault_param.csv')
# 公开默认 CSV 路径(供 UI 的"恢复默认"使用)
DEFAULT_MOTOR_CONFIG_CSV = _MOTOR_INFO_CSV
DEFAULT_PARAMS_CSV = _PARAM_CSV
FAULT_PARAM_CSV = _FAULT_PARAM_CSV

# 载荷字段解析: 形如 pos:f32 / magic:u32=0xB00710AD / param_id:u16
_FIELD_RE = re.compile(r'([A-Za-z_]\w*)\s*:\s*([A-Za-z]\w*(?:\[\d+\])?)\s*(?:=\s*([^;]+))?')
_KNOWN_TYPES = {'u8', 'i8', 'u16', 'i16', 'u32', 'i32', 'f32', 'u64', 'u32_bool'}


class Field:
    """命令载荷中的单个字段"""
    __slots__ = ['name', 'dtype', 'default']

    def __init__(self, name, dtype, default=None):
        self.name = name
        self.dtype = dtype.lower()
        self.default = default  # 固定值(如魔数), 非 None 时 UI 不显示输入框

    @property
    def is_fixed(self):
        return self.default is not None


class CommandSpec:
    """单条命令规格(来自命令表 CSV)"""
    __slots__ = ['cmd', 'name', 'direction', 'category', 'fields',
                 'req_desc', 'ack_desc', 'unit', 'note', 'has_payload']

    def __init__(self, cmd, name, direction, category, fields,
                 req_desc, ack_desc, unit, note):
        self.cmd = cmd
        self.name = name
        self.direction = direction
        self.category = category
        self.fields = fields          # list[Field]
        self.req_desc = req_desc
        self.ack_desc = ack_desc
        self.unit = unit
        self.note = note
        self.has_payload = len(fields) > 0

    @property
    def editable_fields(self):
        """需要用户输入的字段(排除固定魔数)"""
        return [f for f in self.fields if not f.is_fixed]


class ParamSpec:
    """单个参数规格(来自参数索引表 CSV)"""
    __slots__ = ['param_id', 'code_name', 'cn_name', 'dtype', 'nbytes',
                 'unit', 'group', 'rw', 'desc', 'vmin', 'vmax']

    def __init__(self, param_id, code_name, cn_name, dtype, nbytes, unit, group, rw, desc='',
                 vmin=None, vmax=None):
        self.param_id = param_id
        self.code_name = code_name
        self.cn_name = cn_name
        self.dtype = dtype.lower()
        self.nbytes = nbytes
        self.unit = unit
        self.group = group
        self.rw = rw
        self.desc = desc
        self.vmin = vmin    # 允许的最小值(数值), None 表示不限制
        self.vmax = vmax    # 允许的最大值(数值), None 表示不限制

    @property
    def writable(self):
        return 'W' in self.rw.upper()


def _parse_payload_desc(desc: str, warnings=None):
    """把 '{pos:f32;vel:f32}' 解析成 [Field...]; '无'/'-'/空 返回 []

    warnings: 可选 list, 遇到不支持的类型(如 char[])时追加告警信息。
    """
    fields = []
    if not desc:
        return fields
    desc = desc.strip()
    if desc in ('无', '-', '', '变长'):
        return fields
    # 去掉花括号
    body = desc.strip('{}')
    for m in _FIELD_RE.finditer(body):
        name, dtype, default = m.group(1), m.group(2), m.group(3)
        # 只接受已知标量类型(忽略 bytes/char[] 等变长, UI 不自动生成)
        base = dtype.lower()
        if base in _KNOWN_TYPES:
            dval = None
            if default is not None:
                try:
                    dval = int(default.strip(), 0)
                except ValueError:
                    dval = None
            fields.append(Field(name, base, dval))
        elif warnings is not None:
            warnings.append(f"命令字段 '{name}' 类型 '{dtype}' 不支持, 已跳过")
    return fields


def validate_motion_values(cmd: int, values: dict) -> None:
    """校验交接模式的载荷语义，避免把会被固件拒绝的指令发到电机。"""
    cmd = int(cmd)
    if cmd == JmCmd.CALIB_LEVEL5:
        try:
            submode = float(values.get('submode', 0))
            method = float(values.get('method', 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("L5 子模式和齿槽方案必须是整数") from exc
        if not math.isfinite(submode) or submode != int(submode) or not 1 <= submode <= 4:
            raise ValueError("L5 子模式必须为 1~4")
        if not math.isfinite(method) or method != int(method):
            raise ValueError("齿槽方案必须是整数")
        if (submode == 1 and method not in (1, 2, 3)) or (submode != 1 and method != 0):
            raise ValueError("齿槽方案必须为 1位置爬行、2静态逐点或3速度闭环")
        return
    if cmd not in (JmCmd.PP, JmCmd.PV, JmCmd.PT, JmCmd.JOG):
        return

    def number(name):
        try:
            value = float(values.get(name, 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须是数字") from exc
        if not math.isfinite(value):
            raise ValueError(f"{name} 必须是有限数")
        return value

    if cmd == JmCmd.PP:
        flags = number('flags')
        if flags != int(flags) or int(flags) & ~0x03:
            raise ValueError("PP flags 仅允许相对位置和 S 曲线两位")
        for name in ('pos', 'vel', 'acc', 'dec', 'jerk'):
            number(name)
        if any(number(name) <= 0 for name in ('vel', 'acc', 'dec')):
            raise ValueError("PP 的速度、加速度和减速度必须大于 0")
        if int(flags) & 0x02 and number('jerk') <= 0:
            raise ValueError("PP 选择 S 曲线时 jerk 必须大于 0")
    elif cmd == JmCmd.PV:
        for name in ('vel', 'acc', 'dec', 'current_lim'):
            number(name)
        if number('acc') <= 0 or number('dec') <= 0:
            raise ValueError("PV 的加速度和减速度必须大于 0")
        if number('current_lim') < 0:
            raise ValueError("PV 的电流上限不能小于 0")
    elif cmd == JmCmd.PT:
        for name in ('torque', 'slope', 'current_lim', 'vel_lim'):
            number(name)
        if number('slope') <= 0:
            raise ValueError("PT 的转矩斜率必须大于 0")
        if number('current_lim') < 0 or number('vel_lim') < 0:
            raise ValueError("PT 的监护上限需为非负数；0 表示使用电机默认值")
    else:
        direction = number('dir')
        if direction not in (-1, 0, 1):
            raise ValueError("JOG 方向仅允许 -1、0、1")
        for name in ('speed', 'acc', 'dec'):
            if number(name) < 0:
                raise ValueError(f"JOG 的 {name} 不能小于 0")


def _normalize_dtype(dtype: str) -> str:
    """把固件/C 表类型名归一到 codec 使用的短类型名。"""
    d = (dtype or '').strip().lower()
    aliases = {
        'uint8': 'u8',
        'uint8_t': 'u8',
        'int8': 'i8',
        'int8_t': 'i8',
        'uint16': 'u16',
        'uint16_t': 'u16',
        'int16': 'i16',
        'int16_t': 'i16',
        'uint32': 'u32',
        'uint32_t': 'u32',
        'uint64': 'u64',
        'uint64_t': 'u64',
        'int32': 'i32',
        'int32_t': 'i32',
        'float': 'f32',
        'single': 'f32',
    }
    return aliases.get(d, d)


def _parse_num(text):
    """把 CSV 的 Min/Max 单元格解析成 float; 空或非数字返回 None(表示不限制)"""
    text = (text or '').strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _col(row, *names):
    """从 CSV 行取第一个非空列值, 支持多种列名格式兼容(如 VariableName/param_name)。
    返回去空白字符串; 全空返回 ''。"""
    for n in names:
        v = row.get(n)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ''


class ProtocolRegistry:
    """命令/参数注册表"""

    def __init__(self):
        self.commands = {}    # cmd(int) -> CommandSpec
        self.params = {}      # param_id(int) -> ParamSpec
        self.motor_config_params = {}  # param_id(int) -> ParamSpec, 来自 motor_info.csv
        self.warnings = []    # 加载告警(供日志显示)
        # 当前参数表 CSV 路径(默认打包内置; 运行时可被 reload_* 覆盖)
        self.motor_config_csv_path = _MOTOR_INFO_CSV
        self.params_csv_path = _PARAM_CSV
        self._load_commands()
        self._load_params()
        self._load_motor_config_params()

    # ---------------- 加载 ----------------
    def _load_commands(self):
        try:
            with open(_CMD_CSV, encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cmd_hex = (row.get('CMD(hex)') or '').strip()
                    if not cmd_hex:
                        continue
                    try:
                        cmd = int(cmd_hex, 16)
                    except ValueError:
                        continue
                    fields = _parse_payload_desc(row.get('串口请求载荷(CMD后)', ''), self.warnings)
                    spec = CommandSpec(
                        cmd=cmd,
                        name=(row.get('命令名称') or '').strip(),
                        direction=(row.get('方向') or '').strip(),
                        category=(row.get('功能类别') or '').strip(),
                        fields=fields,
                        req_desc=(row.get('串口请求载荷(CMD后)') or '').strip(),
                        ack_desc=(row.get('串口应答载荷') or '').strip(),
                        unit=(row.get('单位/范围') or '').strip(),
                        note=(row.get('备注') or '').strip(),
                    )
                    self.commands[cmd] = spec
        except FileNotFoundError:
            self.warnings.append(f"命令表 CSV 未找到, 使用内置最小命令集: {_CMD_CSV}")
            self._load_fallback_commands()
        except Exception as e:
            self.warnings.append(f"命令表解析失败({e}), 使用内置最小命令集")
            self._load_fallback_commands()

    def _load_fallback_commands(self):
        """CSV 不可用时的内置最小命令集"""
        fallback = [
            (JmCmd.POSITION, "位置环", "运动控制", [Field('pos', 'f32')]),
            (JmCmd.VELOCITY, "速度环", "运动控制", [Field('vel', 'f32')]),
            (JmCmd.TORQUE, "力矩环", "运动控制", [Field('torque', 'f32')]),
            (JmCmd.CURRENT, "电流环", "运动控制",
             [Field('id_ref', 'f32'), Field('iq_ref', 'f32')]),
            (JmCmd.MIT, "MIT控制", "运动控制",
             [Field('pos', 'f32'), Field('vel', 'f32'), Field('kp', 'f32'),
              Field('kd', 'f32'), Field('tff', 'f32')]),
            (JmCmd.POSITION_VELOCITY, "位置+速度前馈", "运动控制",
             [Field('pos', 'f32'), Field('vel_ff', 'f32')]),
            (JmCmd.PP, "轮廓位置PP", "轨迹同步",
             [Field('flags', 'u8'), Field('pos', 'f32'),
              Field('profile_rev', 'u16'), Field('seq', 'u8')]),
            (JmCmd.PV, "轮廓速度PV", "轨迹同步",
             [Field('vel', 'f32'), Field('acc', 'f32'), Field('dec', 'f32'),
              Field('current_lim', 'f32')]),
            (JmCmd.PT, "轮廓力矩PT", "轨迹同步",
             [Field('torque', 'f32'), Field('slope', 'f32'),
              Field('current_lim', 'f32'), Field('vel_lim', 'f32')]),
            (JmCmd.JOG, "点动", "特殊应用",
             [Field('dir', 'i8'), Field('speed', 'f32'),
              Field('acc', 'f32'), Field('dec', 'f32')]),
        ]
        for cmd, name, cat, fields in fallback:
            self.commands[int(cmd)] = CommandSpec(
                int(cmd), name, "Host->Motor", cat, fields, "", "ACK", "", "")

    def _load_params(self):
        self._parse_params_csv(_PARAM_CSV, self.params)
        self.params_csv_path = _PARAM_CSV

    def _parse_params_csv(self, csv_path: str, out: dict) -> bool:
        """解析参数表 CSV 到 out(param_id -> ParamSpec)。
        motor_param.csv 的 id 列是 1-based (生成器约定: validate 返回 0=全部通过),
        加载时自动 -1 修正为 0-based, 与下位机 s_param_tbl 数组索引对齐。"""
        new_params = {}
        try:
            with open(csv_path, encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # 兼容两种列名: param_index.csv(param_id/参数名(代码字段)...)
                    # 和 motor_param.csv(id/param_name/description...)
                    pid_str = _col(row, 'param_id', 'id')
                    if not pid_str.isdigit():
                        continue
                    pid = int(pid_str)
                    # motor_param.csv 的 id 列 1-based → 0-based (与下位机 s_param_tbl 对齐)
                    if 'id' in row and 'param_id' not in row:
                        pid -= 1
                    dtype = _normalize_dtype(_col(row, '数据类型', 'data_type'))
                    arr = _col(row, 'array_size')
                    if dtype == 'char' and arr and arr != '1':
                        dtype = f'char[{arr}]'
                    # 优先用"字节数"列, 无则用 dtype 推算
                    nbytes_str = _col(row, '字节数')
                    try:
                        nbytes = int(nbytes_str) if nbytes_str else codec.type_nbytes(dtype)
                    except ValueError:
                        nbytes = codec.type_nbytes(dtype)
                    new_params[pid] = ParamSpec(
                        param_id=pid,
                        code_name=_col(row, '参数名(代码字段)', 'param_name'),
                        cn_name=_col(row, '中文含义', 'description'),
                        dtype=dtype,
                        nbytes=nbytes,
                        unit=_col(row, '单位', 'unit'),
                        group=_col(row, '所属分组', 'group') or '其他',
                        rw=_col(row, '读写', 'access') or 'RW',
                        desc=_col(row, '中文含义', 'description'),
                    )
        except FileNotFoundError:
            self.warnings.append(f"参数表 CSV 未找到, 参数面板将为空: {csv_path}")
            return False
        except Exception as e:
            self.warnings.append(f"参数表解析失败({e}): {csv_path}")
            return False
        out.clear()
        out.update(new_params)
        return True

    def reload_params(self, csv_path: str) -> bool:
        """运行时重新加载运行时参数表。成功替换, 失败保留旧数据。"""
        new_params = {}
        if self._parse_params_csv(csv_path, new_params):
            self.params = new_params
            self.params_csv_path = csv_path
            return True
        self.warnings.append(f"重新加载参数表失败, 保留旧数据: {csv_path}")
        return False

    def _load_motor_config_params(self):
        self._parse_motor_config_csv(_MOTOR_INFO_CSV, self.motor_config_params)
        self.motor_config_csv_path = _MOTOR_INFO_CSV

    def _parse_motor_config_csv(self, csv_path: str, out: dict) -> bool:
        """解析 motor_info.csv 到 out(param_id -> ParamSpec)。
        成功: 填充 out 并返回 True; 失败: out 保持原状, 追加 warning, 返回 False。
        """
        new_params = {}
        try:
            with open(csv_path, encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                for idx, row in enumerate(reader):
                    # 兼容两种列名: motor_info.csv(VariableName/Index/NameZh/Category...)
                    # 和 motor_param.csv(param_name/id/description/group/min_value/max_value...)
                    name = _col(row, 'VariableName', 'param_name')
                    if not name:
                        continue
                    index_text = _col(row, 'Index', 'id')
                    try:
                        pid = int(index_text, 0) if index_text else idx
                    except ValueError:
                        pid = idx
                    dtype = _normalize_dtype(_col(row, 'DataType', 'data_type'))
                    # motor_param.csv 的 char + array_size → char[N]
                    arr = _col(row, 'array_size')
                    if dtype == 'char' and arr and arr != '1':
                        dtype = f'char[{arr}]'
                    nbytes = codec.type_nbytes(dtype)
                    remarks = _col(row, 'Remarks', 'notes')
                    default = _col(row, 'DefaultValue', 'default_value')
                    desc_parts = []
                    if default:
                        desc_parts.append(f"默认: {default}")
                    if remarks:
                        desc_parts.append(remarks)
                    vmin = _parse_num(_col(row, 'Min', 'min_value'))
                    vmax = _parse_num(_col(row, 'Max', 'max_value'))
                    new_params[pid] = ParamSpec(
                        param_id=pid,
                        code_name=name,
                        cn_name=_col(row, 'NameZh', 'description'),
                        dtype=dtype,
                        nbytes=nbytes,
                        unit=_col(row, 'Unit', 'unit'),
                        group=_col(row, 'Category', 'group') or '其他',
                        rw='RO' if pid in MOTOR_CONFIG_READONLY_IDS or name in MOTOR_CONFIG_READONLY_NAMES else (_col(row, 'Access', 'access') or 'RW'),
                        desc='; '.join(desc_parts),
                        vmin=vmin,
                        vmax=vmax,
                    )
        except FileNotFoundError:
            self.warnings.append(f"电机配置表 CSV 未找到, 电机配置面板将为空: {csv_path}")
            return False
        except Exception as e:
            self.warnings.append(f"电机配置表解析失败({e}): {csv_path}")
            return False
        out.clear()
        out.update(new_params)
        return True

    def reload_motor_config(self, csv_path: str) -> bool:
        """运行时重新加载电机配置表。
        成功: 替换 motor_config_params, 更新 motor_config_csv_path, 返回 True。
        失败: 保留旧数据, 追加 warning, 返回 False。
        """
        new_params = {}
        if self._parse_motor_config_csv(csv_path, new_params):
            self.motor_config_params = new_params
            self.motor_config_csv_path = csv_path
            return True
        self.warnings.append(f"重新加载电机配置表失败, 保留旧数据: {csv_path}")
        return False

    # ---------------- 查询 ----------------
    def get_command(self, cmd: int):
        return self.commands.get(int(cmd))

    def get_param(self, param_id: int):
        return self.params.get(int(param_id))

    def get_motor_config_param(self, param_id: int):
        return self.motor_config_params.get(int(param_id))

    def commands_by_category(self, *categories):
        """按功能类别返回命令列表(按 cmd 升序)"""
        cats = set(categories)
        out = [c for c in self.commands.values() if c.category in cats]
        return sorted(out, key=lambda c: c.cmd)

    def params_by_group(self):
        """返回 OrderedDict 风格: {group: [ParamSpec...]}, 保持 param_id 升序"""
        return self._params_by_group(self.params)

    def motor_config_by_group(self):
        """返回电机配置参数分组, 来自 motor_info.csv。"""
        return self._params_by_group(self.motor_config_params)

    @staticmethod
    def _params_by_group(params: dict):
        groups = {}
        for p in sorted(params.values(), key=lambda x: x.param_id):
            groups.setdefault(p.group, []).append(p)
        return groups

    # ---------------- 打包 ----------------
    def pack_command(self, cmd: int, values: dict) -> bytes:
        """按命令的 fields 顺序打包载荷(values: {字段名: 数值}); 固定字段用其默认值"""
        if cmd == JmCmd.POSITION_CONFIG:
            from .position_config import pack_request
            return pack_request(values.get('op', 0), values.get('value', 0))
        spec = self.get_command(cmd)
        if spec is None or not spec.fields:
            return b''
        if cmd == JmCmd.PP:
            try:
                flags = float(values.get('flags', 0))
                pos = float(values.get('pos', 0))
                revision = float(values.get('profile_rev', 0))
                seq = float(values.get('seq', 0))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError('PP flags 或位置无效') from exc
            if (not math.isfinite(flags) or flags != int(flags) or
                    int(flags) & ~0x03 or not math.isfinite(pos)):
                raise ValueError('PP flags 或位置超出范围')
            if (not math.isfinite(revision) or revision != int(revision) or
                    not math.isfinite(seq) or seq != int(seq) or
                    not 1 <= revision <= 0xFFFF or not 1 <= seq <= 0xFF):
                raise ValueError('PP 必须先通过 0x5F 配置参数，再填写有效版本和序号')
        else:
            validate_motion_values(cmd, values)
        out = bytearray()
        for f in spec.fields:
            v = f.default if f.is_fixed else values.get(f.name, 0)
            out += codec.pack_value(f.dtype, v)
        return bytes(out)

    def pack_param_value(self, param_id: int, text: str) -> bytes:
        """按参数类型把文本打包成写入字节; 解析失败抛 ValueError"""
        spec = self.get_param(param_id)
        if spec is None:
            raise ValueError(f"未知 param_id={param_id}")
        return self._pack_spec_value(spec, text)

    def pack_motor_config_value(self, param_id: int, text: str) -> bytes:
        spec = self.get_motor_config_param(param_id)
        if spec is None:
            raise ValueError(f"未知 motor_config param_id={param_id}")
        return self._pack_spec_value(spec, text)

    @staticmethod
    def _pack_spec_value(spec: ParamSpec, text: str) -> bytes:
        """按类型打包(仅格式转换, 不做范围检查)。范围检查见 check_spec_range。"""
        dtype = spec.dtype
        if dtype.startswith('char['):
            return codec.pack_value(dtype, text)
        if codec.is_float_type(dtype):
            return codec.pack_value(dtype, float(text))
        # 整数类型, 支持 0x 前缀
        try:
            value = int(text, 0)
        except ValueError:
            value = int(float(text))
        return codec.pack_value(dtype, value)

    @staticmethod
    def check_spec_range(spec: ParamSpec, text: str):
        """检查文本值是否在 spec.vmin/vmax 范围内。

        返回 None 表示合法(或无范围约束); 返回 str 表示越界原因。
        注意: 仅做范围判断, 不做格式转换, 格式错误由 pack 阶段抛出。
        """
        if spec.vmin is None and spec.vmax is None:
            return None
        dtype = spec.dtype
        if dtype.startswith('char['):
            return None
        try:
            if codec.is_float_type(dtype):
                value = float(text)
            else:
                try:
                    value = int(text, 0)
                except ValueError:
                    value = int(float(text))
        except (ValueError, TypeError):
            # 格式问题交给 pack 报错, 这里不重复报
            return None
        name = spec.cn_name or spec.code_name
        if spec.vmin is not None and value < spec.vmin:
            return f"{name}={value} 低于最小值 {spec.vmin}"
        if spec.vmax is not None and value > spec.vmax:
            return f"{name}={value} 超过最大值 {spec.vmax}"
        return None

    def unpack_param_value(self, param_id: int, raw: bytes):
        """按参数类型解出数值"""
        spec = self.get_param(param_id)
        if spec is None:
            return None
        return codec.unpack_value(spec.dtype, raw)

    def unpack_motor_config_value(self, param_id: int, raw: bytes):
        spec = self.get_motor_config_param(param_id)
        if spec is None:
            return None
        return codec.unpack_value(spec.dtype, raw)


# 单例(进程内共享一份注册表)
_registry = None


def get_registry() -> ProtocolRegistry:
    global _registry
    if _registry is None:
        _registry = ProtocolRegistry()
    return _registry
