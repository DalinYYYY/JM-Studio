"""
协议数据驱动注册表: 运行时加载命令表/参数表 CSV, 解析成结构化规格供 UI 与打包使用。

数据源(随工具携带): resources/joint_motor_command_list.csv, resources/joint_motor_param_index.csv
CSV 缺失或解析异常时回退到内置最小命令集, 保证无 CSV 也能运行。
"""

import os
import re
import csv

from . import codec
from .cmd_def import JmCmd


_RES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'resources')
_CMD_CSV = os.path.join(_RES_DIR, 'joint_motor_command_list.csv')
_PARAM_CSV = os.path.join(_RES_DIR, 'joint_motor_param_index.csv')
_MOTOR_INFO_CSV = os.path.join(_RES_DIR, 'motor_info.csv')

# 载荷字段解析: 形如 pos:f32 / magic:u32=0xB00710AD / param_id:u16
_FIELD_RE = re.compile(r'([A-Za-z_]\w*)\s*:\s*([A-Za-z]\w*(?:\[\d+\])?)\s*(?:=\s*([^;]+))?')
_KNOWN_TYPES = {'u8', 'i8', 'u16', 'i16', 'u32', 'i32', 'f32'}


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


def _parse_payload_desc(desc: str):
    """把 '{pos:f32;vel:f32}' 解析成 [Field...]; '无'/'-'/空 返回 []"""
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
    return fields


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


class ProtocolRegistry:
    """命令/参数注册表"""

    def __init__(self):
        self.commands = {}    # cmd(int) -> CommandSpec
        self.params = {}      # param_id(int) -> ParamSpec
        self.motor_config_params = {}  # param_id(int) -> ParamSpec, 来自 motor_info.csv
        self.warnings = []    # 加载告警(供日志显示)
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
                    fields = _parse_payload_desc(row.get('串口请求载荷(CMD后)', ''))
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
        ]
        for cmd, name, cat, fields in fallback:
            self.commands[int(cmd)] = CommandSpec(
                int(cmd), name, "Host->Motor", cat, fields, "", "ACK", "", "")

    def _load_params(self):
        try:
            with open(_PARAM_CSV, encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    pid_str = (row.get('param_id') or '').strip()
                    if not pid_str.isdigit():
                        continue
                    pid = int(pid_str)
                    try:
                        nbytes = int((row.get('字节数') or '0').strip())
                    except ValueError:
                        nbytes = 0
                    self.params[pid] = ParamSpec(
                        param_id=pid,
                        code_name=(row.get('参数名(代码字段)') or '').strip(),
                        cn_name=(row.get('中文含义') or '').strip(),
                        dtype=_normalize_dtype(row.get('数据类型') or ''),
                        nbytes=nbytes,
                        unit=(row.get('单位') or '').strip(),
                        group=(row.get('所属分组') or '其他').strip(),
                        rw=(row.get('读写') or 'RW').strip(),
                        desc=(row.get('中文含义') or '').strip(),
                    )
        except FileNotFoundError:
            self.warnings.append(f"参数表 CSV 未找到, 参数面板将为空: {_PARAM_CSV}")
        except Exception as e:
            self.warnings.append(f"参数表解析失败({e})")

    def _load_motor_config_params(self):
        try:
            with open(_MOTOR_INFO_CSV, encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                for idx, row in enumerate(reader):
                    name = (row.get('VariableName') or '').strip()
                    if not name:
                        continue
                    index_text = (row.get('Index') or '').strip()
                    try:
                        pid = int(index_text, 0) if index_text else idx
                    except ValueError:
                        pid = idx
                    dtype = _normalize_dtype(row.get('DataType') or '')
                    nbytes = codec.type_nbytes(dtype)
                    remarks = (row.get('Remarks') or '').strip()
                    default = (row.get('DefaultValue') or '').strip()
                    desc_parts = []
                    if default:
                        desc_parts.append(f"默认: {default}")
                    if remarks:
                        desc_parts.append(remarks)
                    vmin = _parse_num(row.get('Min'))
                    vmax = _parse_num(row.get('Max'))
                    self.motor_config_params[pid] = ParamSpec(
                        param_id=pid,
                        code_name=name,
                        cn_name=(row.get('NameZh') or '').strip(),
                        dtype=dtype,
                        nbytes=nbytes,
                        unit=(row.get('Unit') or '').strip(),
                        group=(row.get('Category') or '其他').strip(),
                        rw=(row.get('Access') or 'RW').strip(),
                        desc='; '.join(desc_parts),
                        vmin=vmin,
                        vmax=vmax,
                    )
        except FileNotFoundError:
            self.warnings.append(f"电机配置表 CSV 未找到, 电机配置面板将为空: {_MOTOR_INFO_CSV}")
        except Exception as e:
            self.warnings.append(f"电机配置表解析失败({e})")

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
        spec = self.get_command(cmd)
        if spec is None or not spec.fields:
            return b''
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
