"""故障码定义与解码模块。

三套故障体系映射:
  1. CSV 故障码表 (fault_param_gen 生成副本, 109 条, 12 列): 来源/级别/名称/触发/处理
  2. compat_mask 兼容位 (固件 fault_mgr 低 13 位旧 6 故障 + bit16-31 压缩位)
  3. JmErr 协议错误码 (0x00~0x16): NACK 应答错误码

本模块提供:
  - load_fault_csv(): 加载 12 列故障码表(行号 index = 固件码表序, 压缩位图 bit=index&15)
  - fault_code_lookup() / fault_codes_by_source() / fault_index_of_code()
  - fault_mask_decode(mask): compat_mask 位解码(低 13 位旧 6 故障精确映射,
    bit16-31 为 index&15 压缩位, 信息有损仅作摘要)
  - err_name_cn(err): JmErr 错误码中文描述
  - FAULT_BIT_INFO: 虚拟电机 Fault 位详细信息(含 CSV 故障码映射)
"""
import csv
import os
from typing import Optional

from .cmd_def import JmErr
from .registry import FAULT_PARAM_CSV


# ==================== CSV 故障码加载 ====================
_FAULT_CODES_CACHE = None     # list[dict]: 故障码记录缓存(按 CSV 行序)
_FAULT_BY_CODE_CACHE = None   # {code_int: record}
_FAULT_BY_INDEX_CACHE = None  # {index: record}
_CSV_PATH = FAULT_PARAM_CSV   # 当前加载/保存的码表路径(默认打包内置, 可被 UI 切换)

# CSV 12 列列序(写回用, 与 fault_param_generate.py REQUIRED_COLUMNS 一致)
_FAULT_CSV_COLUMNS = [
    '故障码', '故障来源', '故障级别', '故障名称', '触发条件', '处理方式',
    '异常命名', '处理动作', '实现期', '降功率档', '信号源', '参数Key',
]
_STAGE_CN_TO_VAL = {'已实现': '1', '未实现': '0'}


def get_fault_csv_path() -> str:
    """当前故障码表 CSV 路径(默认打包内置 resources/fault_param.csv)。"""
    return _CSV_PATH


def set_fault_csv_path(path: str):
    """切换故障码表 CSV 路径并清缓存(下次 load_fault_csv 生效)。"""
    global _CSV_PATH
    _CSV_PATH = str(path)
    clear_fault_cache()


def load_fault_csv() -> list:
    """加载 12 列故障码定义表, 返回 dict 列表(行号即 index, 与固件码表序对应)。

    每条记录:
      {'code': '0x1101', 'code_int': 4353, 'source': '安全系统',
       'level': '故障级', 'name': '急停触发', 'condition': '急停命令(0x03)或急停按钮',
       'action': '立即停机进入安全态', 'alias': 'ESTOP', 'handling': 'STOP_POWER',
       'stage': '1', 'derate': '0', 'signal': 'ctrl_cmd ESTOP', 'param_key': '-',
       'index': 0, '_raw': {...原始行...}}
    """
    global _FAULT_CODES_CACHE, _FAULT_BY_CODE_CACHE, _FAULT_BY_INDEX_CACHE
    if _FAULT_CODES_CACHE is not None:
        return _FAULT_CODES_CACHE

    records = []
    by_code = {}
    by_index = {}
    path = _CSV_PATH
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            index = 0
            for row in reader:
                code_str = (row.get('故障码') or '').strip()
                if not code_str:
                    continue
                try:
                    code_int = int(code_str, 16)
                except ValueError:
                    continue
                rec = {
                    'code': code_str,
                    'code_int': code_int,
                    'source': (row.get('故障来源') or '').strip(),
                    'level': (row.get('故障级别') or '').strip(),
                    'name': (row.get('故障名称') or '').strip(),
                    'condition': (row.get('触发条件') or '').strip(),
                    'action': (row.get('处理方式') or '').strip(),
                    'alias': (row.get('异常命名') or '').strip(),
                    'handling': (row.get('处理动作') or '').strip(),
                    'stage': (row.get('实现期') or '').strip(),
                    'derate': (row.get('降功率档') or '').strip(),
                    'signal': (row.get('信号源') or '').strip(),
                    'param_key': (row.get('参数Key') or '').strip(),
                    'index': index,
                    '_raw': dict(row),
                }
                index += 1
                records.append(rec)
                by_code[code_int] = rec
                by_index[rec['index']] = rec

    _FAULT_CODES_CACHE = records
    _FAULT_BY_CODE_CACHE = by_code
    _FAULT_BY_INDEX_CACHE = by_index
    return records


def fault_code_lookup(code_int: int) -> Optional[dict]:
    """按故障码整数值查找 CSV 记录。"""
    load_fault_csv()
    return _FAULT_BY_CODE_CACHE.get(int(code_int))


def fault_record_by_index(index: int) -> Optional[dict]:
    """按码表行号(index)查找 CSV 记录。"""
    load_fault_csv()
    return _FAULT_BY_INDEX_CACHE.get(int(index))


def fault_index_of_code(code_int: int) -> Optional[int]:
    """按故障码整数查码表行号(index); 未知码返回 None。"""
    rec = fault_code_lookup(code_int)
    return rec['index'] if rec else None


def fault_codes_by_source() -> dict:
    """按故障来源分组, 返回 {source: [records]}。"""
    groups = {}
    for r in load_fault_csv():
        groups.setdefault(r['source'], []).append(r)
    return groups


def clear_fault_cache():
    """清除 CSV 缓存(重新加载用)。"""
    global _FAULT_CODES_CACHE, _FAULT_BY_CODE_CACHE, _FAULT_BY_INDEX_CACHE
    _FAULT_CODES_CACHE = None
    _FAULT_BY_CODE_CACHE = None
    _FAULT_BY_INDEX_CACHE = None


def save_fault_csv(path: str = None) -> int:
    """把当前码表记录写回 CSV(12 列, 实现期列含 UI 修改)。

    path=None 时写回当前加载路径。基于缓存记录的 _raw 原始行逐行替换实现期,
    保持列序/未解析列内容不变; 返回写入行数。记录未加载时抛 RuntimeError。
    """
    if _FAULT_CODES_CACHE is None:
        raise RuntimeError('码表未加载, 无可保存数据')
    target = str(path) if path else _CSV_PATH
    with open(target, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, _FAULT_CSV_COLUMNS)
        writer.writeheader()
        for rec in _FAULT_CODES_CACHE:
            row = {k: rec.get('_raw', {}).get(k, '') for k in _FAULT_CSV_COLUMNS}
            row['故障码'] = rec['code']
            row['实现期'] = '1' if rec['stage'] == '1' else '0'
            writer.writerow(row)
    return len(_FAULT_CODES_CACHE)


# ==================== compat_mask 兼容位定义 ====================
# 与固件 fault_manager.c s_compat_map + system_state.h SYSTEM_FAULT_* 一致:
# 低 13 位为旧 6 故障兼容位(精确映射新码表码值), bit16-31 为其余故障级
# index&15 压缩位(信息有损, 仅可作摘要灯, 不可用于定位具体码)。
COMPAT_BIT_CODES = [
    (0,  0x3102, 'OVER_CURRENT',  '相电流峰值超过峰值过流阈值'),
    (1,  0x2102, 'OVER_VOLTAGE',  '母线电压超过过压阈值'),
    (2,  0x2103, 'UNDER_VOLTAGE', '母线电压低于欠压阈值'),
    (10, 0x4106, 'OVER_SPEED',    '电机转速超过超速阈值'),
    (11, 0x810A, 'NUMERIC',       '控制算法数值异常(NaN/Inf)'),
    (12, 0x3101, 'GATE_DRIVER',   '栅极驱动器 nFAULT 拉低'),
]

# 压缩位区起始(bit16-31, bit=0x10000<<(index&15))
_COMPAT_COMPRESSED_BASE = 16


# ==================== 虚拟电机 Fault 位定义 ====================
# 对齐 twin_fsm.py 的 Fault 类, 补充 CSV 映射与检测说明
# (bit, 数值, 名称, 检测方式, 触发条件, 对应CSV故障码列表)
FAULT_BIT_INFO = [
    (0,  1,     'OVER_CURRENT',    'FaultDetector.check',
     '相电流峰值 > peak_current×1.2',         [0x3102]),
    (1,  2,     'OVER_VOLTAGE',    'FaultDetector.check',
     'vbus > rated_voltage×1.1',              [0x2102]),
    (2,  4,     'UNDER_VOLTAGE',   'FaultDetector.check',
     'vbus < rated_voltage×0.5',              [0x2103]),
    (3,  8,     'OVER_TEMP_FET',   'FaultDetector.check',
     'MOSFET 结温 > fet_over_temp_threshold', [0x3201, 0x3103]),
    (4,  16,    'OVER_TEMP_MOTOR', 'FaultDetector.check',
     '电机温度 > motor_over_temp_threshold',  [0x4101, 0x4204]),
    (5,  32,    'POS_LIMIT',       '状态机迁移SAFETY(不置位)',
     '位置超出 pos_min/max_limit',            [0x1201]),
    (6,  64,    'FOLLOW_ERROR',    'FaultDetector.check(持续0.2s)',
     '跟随误差 > follow_error_threshold',     [0x8108]),
    (7,  128,   'COMM_LOST',       'FaultDetector.check',
     '通信超时 > 0.2s',                       [0x9102, 0x5105]),
    (8,  256,   'PHASE_LOSS',      '未实现检测',
     '缺相(预留)',                            [0x4104]),
    (9,  512,   'SAFETY_LIMIT',    '未实现检测',
     '安全限位(预留)',                        [0x1102]),
    (10, 1024,  'OVER_SPEED',      'FaultDetector.check',
     '电机角速度 > over_speed',               [0x4106]),
    (12, 4096,  'GATE_DRIVER',     'nFAULT引脚(固件透传)',
     '栅极驱动器 nFAULT 拉低(OCP/UVLO/TSD 硬件保护锁存)', [0x3101]),
    (15, 32768, 'INJECTED',        'inject()注入',
     '演示用注入故障',                        []),
]


def fault_mask_decode(mask: int) -> list:
    """将 compat_mask 位标志解码为故障描述列表。

    低 13 位按旧 6 故障精确映射到新码表码值; bit16-31 为压缩位,
    解出候选码表名称(index&15), 供摘要展示(不保证唯一)。

    返回 list[dict], 每项:
      {'bit': int, 'value': int, 'name': str, 'condition': str,
       'csv_codes': list[int], 'csv_names': list[str], 'compressed': bool}
    """
    mask = int(mask) & 0xFFFFFFFF
    result = []
    # 低 13 位旧 6 故障精确映射
    for bit, code, name, condition in COMPAT_BIT_CODES:
        value = 1 << bit
        if mask & value:
            rec = fault_code_lookup(code)
            result.append({
                'bit': bit, 'value': value, 'name': name,
                'condition': condition, 'compressed': False,
                'csv_codes': [code],
                'csv_names': [rec['name'] if rec else f'0x{code:04X}'],
            })
    # bit16-31 压缩位: 候选 = 码表中 index&15 匹配的故障级记录(信息有损)
    for low in range(_COMPAT_COMPRESSED_BASE, 32):
        value = 1 << low
        if not (mask & value):
            continue
        idx_low = low - _COMPAT_COMPRESSED_BASE
        candidates = [r for r in load_fault_csv()
                      if r['level'] == '故障级' and (r['index'] & 0x0F) == idx_low]
        result.append({
            'bit': low, 'value': value, 'name': f'COMPAT_{idx_low}',
            'condition': '压缩兼容位(候选可能不唯一)', 'compressed': True,
            'csv_codes': [r['code_int'] for r in candidates],
            'csv_names': [r['name'] for r in candidates],
        })
    return result


def fault_mask_to_str(mask: int) -> str:
    """将 compat_mask 解码为可读字符串 (单行, 分号分隔)。"""
    if not mask:
        return '无故障'
    parts = []
    for info in fault_mask_decode(mask):
        csv_desc = '/'.join(info['csv_names']) if info['csv_names'] else ''
        desc = info['name']
        if csv_desc:
            desc += f'({csv_desc})'
        parts.append(desc)
    return '; '.join(parts) if parts else '无故障'


# ==================== JmErr 协议错误码中文描述 ====================
_JMERR_CN = {
    JmErr.OK:            '成功',
    JmErr.UNSUPPORTED:   '命令不支持',
    JmErr.OUT_OF_RANGE:  '参数超出范围',
    JmErr.STATE_DENY:    '状态拒绝(当前状态不允许此操作)',
    JmErr.BAD_PARAM_ID:  '参数ID无效',
    JmErr.CRC:           'CRC校验失败',
    JmErr.LENGTH:        '载荷长度错误',
    JmErr.READ_ONLY:     '参数只读',
    JmErr.FLASH:         'FLASH读写错误',
    JmErr.FAULT_STATE:   '故障状态(需先清除故障)',
    JmErr.CALIB_BUSY:    '标定忙(标定进行中)',
    JmErr.PENDING:       '异步已排队',
    JmErr.BUSY:          '忙(双通道主控被占用)',
    JmErr.UNAUTHORIZED:  '鉴权失败(令牌不匹配)',
    JmErr.RATE_LIMIT:    '速率限制(命令频率超限)',
    JmErr.NOT_FOUND:     '未找到(资源/文件/记录不存在)',
    JmErr.FLASH_ERASE:   'Flash擦除失败',
    JmErr.FLASH_WRITE:   'Flash写入失败',
    JmErr.FLASH_VERIFY:  'Flash校验失败(读回不匹配)',
    JmErr.NOT_SUPPORTED: '命令未实现(预留命令如OTA/SYNC)',
    JmErr.SAVE_LIMIT:     '固化次数超限(寿命保护)',
    JmErr.EEPROM_WRITE:   'EEPROM写入失败',
    JmErr.EEPROM_VERIFY:  'EEPROM回读校验失败',
    JmErr.STORAGE_NOT_INIT: '存储服务未初始化',
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
