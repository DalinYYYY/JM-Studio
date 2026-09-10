"""
关节电机通信协议层(传输无关)

子模块:
- crc16   : CRC16-XMODEM(与固件一致)
- frame   : 串口帧编解码 FrameCodec / FrameDecoder
- cmd_def : JmCmd / JmErr / JmTlmBit / JmParamType 枚举
- codec   : 小端编解码助手 + 按类型打包
- feedback: FeedbackData + 各应答/遥测解析
- smoothstart: 缓启动渐变配置(0xA3/0xA4)编解码
- fault_diag: 故障管理诊断(0xAA/0xAB)编解码
- registry: CSV 数据驱动注册表(命令表/参数表)
"""

from .crc16 import crc16_calc
from .frame import FrameCodec, FrameDecoder, STX_H, STX_L, MAX_PACK_SIZE, PACK_OVERHEAD
from .cmd_def import (
    JmCmd, JmErr, JmTlmBit, JmParamType,
    TopFsm, RunState, TOP_FSM_CN, RUN_STATE_CN,
    CalibState, CalibFailReason, CALIB_STATE_CN, CALIB_FAIL_REASON_CN,
    CALIB_LEVEL_CN, CALIB_SUBMODE_CN,
    PidAutotuneFailReason, PID_AUTOTUNE_FAIL_REASON_CN,
    cmd_name, err_name, top_fsm_name, run_state_name, motion_state_name,
    calib_state_name, calib_fail_reason_name, calib_level_submode_name,
    pid_autotune_fail_reason_name,
    MAGIC_BOOTLOADER, MAGIC_FACTORY_RESET, MAGIC_SOFT_RESET,
)
from . import codec
from . import smoothstart
from . import fault_diag
from .feedback import (
    FeedbackData, parse_state, parse_read_reply, parse_telemetry,
    parse_calib_status, parse_pid_autotune_ack,
)
from .registry import (
    ProtocolRegistry, CommandSpec, ParamSpec, Field, get_registry,
    FAULT_PARAM_CSV,
)
from .fault_codes import (
    load_fault_csv, fault_code_lookup, fault_codes_by_source,
    fault_record_by_index, fault_index_of_code, clear_fault_cache,
    get_fault_csv_path, set_fault_csv_path, save_fault_csv,
    fault_mask_decode, fault_mask_to_str,
    err_name_cn, err_name_cn_short,
    FAULT_BIT_INFO, COMPAT_BIT_CODES,
)

__all__ = [
    'crc16_calc',
    'FrameCodec', 'FrameDecoder', 'STX_H', 'STX_L', 'MAX_PACK_SIZE', 'PACK_OVERHEAD',
    'JmCmd', 'JmErr', 'JmTlmBit', 'JmParamType',
    'TopFsm', 'RunState', 'TOP_FSM_CN', 'RUN_STATE_CN',
    'CalibState', 'CalibFailReason',
    'CALIB_STATE_CN', 'CALIB_FAIL_REASON_CN',
    'CALIB_LEVEL_CN', 'CALIB_SUBMODE_CN',
    'PidAutotuneFailReason', 'PID_AUTOTUNE_FAIL_REASON_CN',
    'cmd_name', 'err_name', 'top_fsm_name', 'run_state_name', 'motion_state_name',
    'calib_state_name', 'calib_fail_reason_name', 'calib_level_submode_name',
    'pid_autotune_fail_reason_name',
    'MAGIC_BOOTLOADER', 'MAGIC_FACTORY_RESET', 'MAGIC_SOFT_RESET',
    'codec',
    'FeedbackData', 'parse_state', 'parse_read_reply', 'parse_telemetry',
    'parse_calib_status', 'parse_pid_autotune_ack',
    'ProtocolRegistry', 'CommandSpec', 'ParamSpec', 'Field', 'get_registry',
    'load_fault_csv', 'fault_code_lookup', 'fault_codes_by_source',
    'fault_record_by_index', 'fault_index_of_code', 'clear_fault_cache',
    'get_fault_csv_path', 'set_fault_csv_path', 'save_fault_csv',
    'FAULT_PARAM_CSV',
    'fault_mask_decode', 'fault_mask_to_str',
    'err_name_cn', 'err_name_cn_short',
    'FAULT_BIT_INFO', 'COMPAT_BIT_CODES',
]
