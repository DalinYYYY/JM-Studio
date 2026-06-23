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
                 'unit', 'group', 'rw']

    def __init__(self, param_id, code_name, cn_name, dtype, nbytes, unit, group, rw):
        self.param_id = param_id
        self.code_name = code_name
        self.cn_name = cn_name
        self.dtype = dtype.lower()
        self.nbytes = nbytes
        self.unit = unit
        self.group = group
        self.rw = rw

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


class ProtocolRegistry:
    """命令/参数注册表"""

    def __init__(self):
        self.commands = {}    # cmd(int) -> CommandSpec
        self.params = {}      # param_id(int) -> ParamSpec
        self.warnings = []    # 加载告警(供日志显示)
        self._load_commands()
        self._load_params()

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
                        dtype=(row.get('数据类型') or '').strip(),
                        nbytes=nbytes,
                        unit=(row.get('单位') or '').strip(),
                        group=(row.get('所属分组') or '其他').strip(),
                        rw=(row.get('读写') or 'RW').strip(),
                    )
        except FileNotFoundError:
            self.warnings.append(f"参数表 CSV 未找到, 参数面板将为空: {_PARAM_CSV}")
        except Exception as e:
            self.warnings.append(f"参数表解析失败({e})")

    # ---------------- 查询 ----------------
    def get_command(self, cmd: int):
        return self.commands.get(int(cmd))

    def get_param(self, param_id: int):
        return self.params.get(int(param_id))

    def commands_by_category(self, *categories):
        """按功能类别返回命令列表(按 cmd 升序)"""
        cats = set(categories)
        out = [c for c in self.commands.values() if c.category in cats]
        return sorted(out, key=lambda c: c.cmd)

    def params_by_group(self):
        """返回 OrderedDict 风格: {group: [ParamSpec...]}, 保持 param_id 升序"""
        groups = {}
        for p in sorted(self.params.values(), key=lambda x: x.param_id):
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
        dtype = spec.dtype
        if dtype.startswith('char['):
            return codec.pack_value(dtype, text)
        if codec.is_float_type(dtype):
            return codec.pack_value(dtype, float(text))
        # 整数类型, 支持 0x 前缀
        return codec.pack_value(dtype, int(text, 0))

    def unpack_param_value(self, param_id: int, raw: bytes):
        """按参数类型解出数值"""
        spec = self.get_param(param_id)
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
