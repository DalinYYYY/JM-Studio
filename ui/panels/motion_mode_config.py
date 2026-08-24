"""运动模式白名单配置加载(resources/motion_modes.txt)。

被 motion_panel(下拉框) 与 state_machine_panel(运行模式图) 共享,
一个文件统一管理所有运动模式的显示/屏蔽。

文件格式(每行一个模式条目):
    模式名 = True      显示
    模式名 = False     屏蔽
    模式名             缺省 = 显示
    # 行首 # 为整行注释, 行内 # 后为注释
条目写法支持: CSV中文命令名(力矩环) / 命令码(0x12/18) / 英文枚举名(TORQUE),
状态机图额外接受短标签别名(力矩), 由调用方经 extra_name_map 提供。

安全规则: 文件缺失/无显示条目(全屏蔽或全注释)返回 None(不过滤);
部分条目无法识别时忽略该条目; 重复条目去重(保首个出现顺序)。
该文件随 resources 嵌入 exe, 打包后对最终用户不可见。
"""

import os
import sys

from jmproto.cmd_def import JmCmd

_CFG_NAME = 'motion_modes.txt'

# bool 假值集合(不区分大小写); 其余值(含缺省)一律视为显示
_FALSE_WORDS = {'false', '0', 'off', 'no', '关', '否', '屏蔽'}

# CSV 命令名 → 命令码(与 resources/joint_motor_command_list.csv 一致,
# motion_modes.txt 默认模板即用这些名称)。内置静态映射保证不依赖
# registry 注入(测试/未注入场景)也能解析; registry 注入后另行支持
# CSV 未来新增的命令名。CSV 增删命令时同步维护此表。
_CMD_NAME_MAP = {
    # 运动控制 0x10~0x1C
    '开环电压': 0x10, '电流环': 0x11, '力矩环': 0x12, 'MIT控制': 0x13,
    '速度环': 0x14, '位置环': 0x15, '位置+速度前馈': 0x16, '位置+力矩限幅': 0x17,
    '速度+力矩限幅': 0x18, '占空比控制': 0x19, '电压矢量': 0x1A,
    '弱磁控制': 0x1B, '无感FOC': 0x1C,
    # 高级力控 0x30~0x3A
    '阻抗控制': 0x30, '导纳控制': 0x31, '纯力控制': 0x32, '力位混合': 0x33,
    '重力补偿': 0x34, '碰撞检测': 0x35, '零力模式': 0x36, '恒力控制': 0x37,
    '变阻抗': 0x38, '自适应重力补偿': 0x39, '落地缓冲': 0x3A,
    # 轨迹同步 0x50~0x5D
    'PVT插补': 0x50, '三次样条': 0x51, '梯形轨迹': 0x52, 'S型轨迹': 0x53,
    '回零': 0x54, 'CANopen同步': 0x55, 'EtherCAT CSP': 0x56,
    'EtherCAT CSV': 0x57, 'EtherCAT CST': 0x58, '轮廓位置PP': 0x59,
    '轮廓速度PV': 0x5A, '轮廓力矩PT': 0x5B, '电子齿轮': 0x5C, '电子凸轮': 0x5D,
    # 负载模拟 0x60~0x67
    '被动恒转矩': 0x60, '动态转矩': 0x61, '平方转矩负载': 0x62, '恒功率负载': 0x63,
    '摩擦负载': 0x64, '惯量模拟': 0x65, '工况谱复现': 0x66, '冲击过载负载': 0x67,
    # 测试 0x7A/0x7B(状态机图节点)
    '电流环测试': 0x7A, '速度环测试': 0x7B,
}


def _resource_dir():
    """定位 resources 目录。

    PyInstaller 单文件运行时资源解压在 sys._MEIPASS 临时目录;
    源码运行时在工程根/resources (本文件位于 ui/panels/ 下, 上三级)。
    """
    base = getattr(sys, '_MEIPASS', None)
    if base:
        d = os.path.join(base, 'resources')
        if os.path.isdir(d):
            return d
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, 'resources')


def _parse_entry(line):
    """拆 '名称 = bool' → (名称, show)。无 '=' 时 show=True。"""
    if '=' not in line:
        return line, True
    name, _, val = line.partition('=')
    val = val.strip().lower()
    return name.strip(), val not in _FALSE_WORDS


def _resolve_cmd(entry, registry, extra_name_map):
    """把单个条目名解析为命令码, 失败返回 None。"""
    # 1) 命令码: 0x.. 十六进制 / 十进制
    try:
        return int(entry, 0)
    except ValueError:
        pass
    # 2) 英文枚举名(如 TORQUE)
    member = JmCmd.__members__.get(entry.upper())
    if member is not None:
        return int(member)
    # 3) 内置 CSV 命令名(静态映射, 不依赖注入)
    cmd = _CMD_NAME_MAP.get(entry)
    if cmd is not None:
        return cmd
    # 4) registry 命令名(命令表 CSV name, 支持 CSV 未来新增命令)
    if registry is not None:
        for spec in registry.commands.values():
            if spec.name == entry:
                return spec.cmd
    # 5) 调用方提供的别名兜底(如状态机图短标签"力矩")
    if extra_name_map:
        return extra_name_map.get(entry)
    return None


def load_motion_mode_cmds(registry=None, extra_name_map=None, subset=None):
    """读取 motion_modes.txt, 返回有序命令码列表(仅含显示条目, 已去重)。

    返回 None 表示不过滤(文件缺失/全注释/无显示条目), 调用方回退默认集。
    registry: 用于把中文命令名(命令表 CSV 的 name)解析为命令码, 可为 None。
    extra_name_map: {名称: cmd} 额外别名兜底, 可为 None。
    subset: 应用形态的模式子集白名单(名称/命令码列表, 如 app_profiles.json 的
        motion_modes)。提供时优先于 motion_modes.txt, 按 subset 顺序解析,
        无法识别的条目忽略; 全部无法识别时返回 None(回退全局白名单)。
    """
    if subset is not None:
        cmds = []
        seen = set()
        for entry in subset:
            cmd = _resolve_cmd(str(entry), registry, extra_name_map)
            if cmd is not None and cmd not in seen:
                seen.add(cmd)
                cmds.append(cmd)
        return cmds or None

    path = os.path.join(_resource_dir(), _CFG_NAME)
    try:
        with open(path, encoding='utf-8-sig') as f:
            lines = f.readlines()
    except OSError:
        return None

    cmds = []
    seen = set()
    for raw in lines:
        line = raw.split('#', 1)[0].strip()  # 去行内注释/整行注释/空行
        if not line:
            continue
        entry, show = _parse_entry(line)
        if not entry:
            continue
        cmd = _resolve_cmd(entry, registry, extra_name_map)
        if cmd is not None and show and cmd not in seen:
            seen.add(cmd)
            cmds.append(cmd)
    return cmds or None
