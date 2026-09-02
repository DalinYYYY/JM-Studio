"""应用形态(Profile)配置加载(resources/app_profiles.json)。

同一代码库按 profile 裁剪出不同发行形态:
    python main.py                 → 开发版 MainWindow(默认, 行为不变)
    python main.py --profile tester → 测试员版 TesterMainWindow

profile 定义左栏面板、右侧 Tab、运动模式白名单与主动轮询组,
新增形态只需在 app_profiles.json 加一节, 无需改代码。
文件损坏/条目非法时逐项回退默认值, 不阻塞启动。
"""

import json
import os
import sys

from jmproto.cmd_def import JmCmd

_CFG_NAME = 'app_profiles.json'

# 各字段默认值(条目缺失/非法时回退)
_DEFAULTS = {
    'title': 'JM Studio',
    'window_size': [1180, 800],
    'left_panels': ['connection', 'control', 'motion'],
    'tabs': ['feedback_chart', 'waveform_plot'],
    'feedback_diagram': 'foc',     # 实时反馈上图: 'foc' 电流环框图 / 'top_fsm' 主状态机
    'motion_modes': None,          # None = 不覆盖, 用 motion_modes.txt 全局白名单
    'poll_group': [
        {'cmd': 'READ_FEEDBACK', 'period_ms': 20},
        {'cmd': 'READ_DQ_CURRENT', 'period_ms': 20},
        {'cmd': 'READ_STATE', 'period_ms': 100},
        {'cmd': 'READ_BUS', 'period_ms': 200},
        {'cmd': 'READ_TEMPERATURE', 'period_ms': 200},
    ],
}

# 合法面板名(调用方按此分发)
_VALID_LEFT = {'connection', 'control', 'motion'}
_VALID_TABS = {'feedback_chart', 'waveform_plot', 'power_monitor'}


def _resource_dir():
    """定位 resources 目录(兼容 PyInstaller 解包与源码运行)。"""
    base = getattr(sys, '_MEIPASS', None)
    if base:
        d = os.path.join(base, 'resources')
        if os.path.isdir(d):
            return d
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, 'resources')


def _resolve_cmd(name):
    """轮询命令名 → 命令码。支持英文枚举名 / 0x 十六进制 / 十进制, 失败返回 None。"""
    try:
        return int(name, 0)
    except (TypeError, ValueError):
        pass
    member = JmCmd.__members__.get(str(name).upper())
    return int(member) if member is not None else None


def _norm_poll_group(raw):
    """校验并规整轮询组: 剔除非法条目, 周期夹到 [5, 5000] ms。"""
    if not isinstance(raw, list):
        return list(_DEFAULTS['poll_group'])
    group = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cmd = _resolve_cmd(item.get('cmd'))
        if cmd is None:
            continue
        try:
            period = max(5, min(5000, int(item.get('period_ms', 20))))
        except (TypeError, ValueError):
            period = 20
        group.append({'cmd': cmd, 'period_ms': period})
    return group or list(_DEFAULTS['poll_group'])


def load_profile(name):
    """按名称读取 profile, 返回规整后的 dict; 不存在/损坏返回 None。"""
    if not name:
        return None
    path = os.path.join(_resource_dir(), _CFG_NAME)
    try:
        with open(path, encoding='utf-8-sig') as f:
            data = json.load(f)
        raw = data.get('profiles', {}).get(name)
        if not isinstance(raw, dict):
            return None
    except (OSError, ValueError):
        return None

    prof = dict(_DEFAULTS)
    prof['title'] = str(raw.get('title', prof['title']))

    size = raw.get('window_size')
    if (isinstance(size, (list, tuple)) and len(size) == 2
            and all(isinstance(v, int) and v >= 600 for v in size)):
        prof['window_size'] = [int(size[0]), int(size[1])]

    left = raw.get('left_panels')
    if isinstance(left, list):
        picked = [p for p in left if p in _VALID_LEFT]
        prof['left_panels'] = picked or list(_DEFAULTS['left_panels'])

    tabs = raw.get('tabs')
    if isinstance(tabs, list):
        picked = [t for t in tabs if t in _VALID_TABS]
        prof['tabs'] = picked or list(_DEFAULTS['tabs'])

    if raw.get('feedback_diagram') == 'top_fsm':
        prof['feedback_diagram'] = 'top_fsm'

    modes = raw.get('motion_modes')
    if isinstance(modes, list) and modes:
        prof['motion_modes'] = [str(m) for m in modes]

    prof['poll_group'] = _norm_poll_group(raw.get('poll_group'))
    return prof
