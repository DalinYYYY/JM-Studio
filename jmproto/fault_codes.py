"""故障码定义与解码模块。

三套故障体系映射:
  1. CSV 设备故障码 (0x1101~0xA302): 9 大系统 104 条, 含来源/级别/名称/触发条件/处理方式
  2. 虚拟电机 Fault 位标志 (bit 0~10, 15): 固件级故障标志, FaultDetector 实时检测
  3. JmErr 协议错误码 (0x00~0x0A): NACK 应答错误码

本模块提供:
  - load_fault_csv(): 加载 CSV 故障码表
  - fault_mask_decode(mask): 将虚拟电机 fault_mask 位标志解码为故障描述列表
  - err_name_cn(err): JmErr 错误码中文描述
  - FAULT_BIT_INFO: 虚拟电机 Fault 位详细信息
  - FAULT_BIT_TO_CSV: 虚拟电机 Fault 位到 CSV 故障码的映射
"""
import csv
import os
from typing import Optional

from .cmd_def import JmErr


# ==================== CSV 故障码加载 ====================
_CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'resources', '故障码定义_故障信息表_表格.csv')

_FAULT_CODES_CACHE = None  # list[dict]: 故障码记录缓存


def load_fault_csv() -> list:
    """加载 CSV 故障码定义表, 返回 dict 列表。

    每条记录:
      {'code': '0x1101', 'code_int': 4353, 'source': '安全系统',
       'level': '故障级', 'name': '急停按钮触发',
       'condition': '急停按钮按下', 'action': '立即停机，切断主电源',
       'alias': ''}
    """
    global _FAULT_CODES_CACHE
    if _FAULT_CODES_CACHE is not None:
        return _FAULT_CODES_CACHE

    records = []
    path = _CSV_PATH
    if not os.path.exists(path):
        _FAULT_CODES_CACHE = []
        return _FAULT_CODES_CACHE

    with open(path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            code_str = (row.get('故障码') or '').strip()
            if not code_str:
                continue
            try:
                code_int = int(code_str, 16)
            except ValueError:
                continue
            records.append({
                'code': code_str,
                'code_int': code_int,
                'source': (row.get('故障来源') or '').strip(),
                'level': (row.get('故障级别') or '').strip(),
                'name': (row.get('故障名称') or '').strip(),
                'condition': (row.get('触发条件') or '').strip(),
                'action': (row.get('处理方式') or '').strip(),
                'alias': (row.get('异常命名') or '').strip(),
            })
    _FAULT_CODES_CACHE = records
    return records


def fault_code_lookup(code_int: int) -> Optional[dict]:
    """按故障码整数值查找 CSV 记录。"""
    for r in load_fault_csv():
        if r['code_int'] == code_int:
            return r
    return None


def fault_codes_by_source() -> dict:
    """按故障来源分组, 返回 {source: [records]}。"""
    groups = {}
    for r in load_fault_csv():
        groups.setdefault(r['source'], []).append(r)
    return groups


# ==================== 虚拟电机 Fault 位定义 ====================
# 对齐 twin_fsm.py 的 Fault 类, 补充 CSV 映射与检测说明
# (bit, 数值, 名称, 检测方式, 触发条件, 对应CSV故障码列表)
FAULT_BIT_INFO = [
    (0,  1,     'OVER_CURRENT',    'FaultDetector.check',
     '相电流峰值 > peak_current×1.2',         [0x3102, 0x2104]),
    (1,  2,     'OVER_VOLTAGE',    'FaultDetector.check',
     'vbus > rated_voltage×1.1',              [0x2102]),
    (2,  4,     'UNDER_VOLTAGE',   'FaultDetector.check',
     'vbus < rated_voltage×0.5',              [0x2103]),
    (3,  8,     'OVER_TEMP_FET',   'FaultDetector.check',
     'MOSFET 结温 > fet_over_temp_threshold', [0x3201, 0x3103]),
    (4,  16,    'OVER_TEMP_MOTOR', 'FaultDetector.check',
     '电机温度 > motor_over_temp_threshold',  [0x4101, 0x4204]),
    (5,  32,    'POS_LIMIT',       '状态机迁移SAFETY(不置位)',
     '位置超出 pos_min/max_limit',            [0x1201, 0x1301]),
    (6,  64,    'FOLLOW_ERROR',    'FaultDetector.check(持续0.2s)',
     '跟随误差 > follow_error_threshold',     [0x8108]),
    (7,  128,   'COMM_LOST',       'FaultDetector.check',
     '通信超时 > 0.2s',                       [0x9102, 0x5105]),
    (8,  256,   'PHASE_LOSS',      '未实现检测',
     '缺相(预留)',                            [0x4104]),
    (9,  512,   'SAFETY_LIMIT',    '未实现检测',
     '安全限位(预留)',                        [0x1102]),
    (10, 1024,  'OVER_SPEED',      'FaultDetector.check',
     '电机角速度 > over_speed',               [0x4202]),
    (15, 32768, 'INJECTED',        'inject()注入',
     '演示用注入故障',                        []),
]

# 位数值 -> FAULT_BIT_INFO 索引
_FAULT_BIT_MAP = {info[1]: info for info in FAULT_BIT_INFO}


def fault_mask_decode(mask: int) -> list:
    """将虚拟电机 fault_mask 位标志解码为故障描述列表。

    返回 list[dict], 每项:
      {'bit': int, 'value': int, 'name': str, 'detect': str,
       'condition': str, 'csv_codes': list[int], 'csv_names': list[str]}
    """
    result = []
    for bit, value, name, detect, condition, csv_codes in FAULT_BIT_INFO:
        if mask & value:
            csv_names = []
            for c in csv_codes:
                rec = fault_code_lookup(c)
                csv_names.append(rec['name'] if rec else f'0x{c:04X}')
            result.append({
                'bit': bit, 'value': value, 'name': name,
                'detect': detect, 'condition': condition,
                'csv_codes': csv_codes, 'csv_names': csv_names,
            })
    return result


def fault_mask_to_str(mask: int) -> str:
    """将 fault_mask 解码为可读字符串 (单行, 分号分隔)。"""
    if mask == 0:
        return '无故障'
    parts = []
    for info in fault_mask_decode(mask):
        csv_desc = '/'.join(info['csv_names']) if info['csv_names'] else ''
        desc = info['name']
        if csv_desc:
            desc += f'({csv_desc})'
        parts.append(desc)
    return '; '.join(parts)


# ==================== JmErr 协议错误码中文描述 ====================
_JMERR_CN = {
    JmErr.OK:           '成功',
    JmErr.UNSUPPORTED:  '命令不支持',
    JmErr.OUT_OF_RANGE: '参数超出范围',
    JmErr.STATE_DENY:   '状态拒绝(当前状态不允许此操作)',
    JmErr.BAD_PARAM_ID: '参数ID无效',
    JmErr.CRC:          'CRC校验失败',
    JmErr.LENGTH:       '载荷长度错误',
    JmErr.READ_ONLY:    '参数只读',
    JmErr.FLASH:        'FLASH读写错误',
    JmErr.FAULT_STATE:  '故障状态(需先清除故障)',
    JmErr.CALIB_BUSY:   '标定忙(标定进行中)',
}


def err_name_cn(err: int) -> str:
    """JmErr 错误码中文描述。"""
    try:
        e = JmErr(int(err))
        cn = _JMERR_CN.get(e)
        if cn:
            return f'{e.name}({cn})'
        return e.name
    except ValueError:
        return f'0x{int(err):02X}'


def err_name_cn_short(err: int) -> str:
    """JmErr 错误码中文描述 (仅中文, 不含枚举名)。"""
    try:
        e = JmErr(int(err))
        return _JMERR_CN.get(e, e.name)
    except ValueError:
        return f'0x{int(err):02X}'
