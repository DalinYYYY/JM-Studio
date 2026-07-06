"""
关节电机通信协议层(传输无关)

子模块:
- crc16   : CRC16-XMODEM(与固件一致)
- frame   : 串口帧编解码 FrameCodec / FrameDecoder
- cmd_def : JmCmd / JmErr / JmTlmBit / JmParamType 枚举
- codec   : 小端编解码助手 + 按类型打包
- feedback: FeedbackData + 各应答/遥测解析
- registry: CSV 数据驱动注册表(命令表/参数表)
"""

from .crc16 import crc16_calc
from .frame import FrameCodec, FrameDecoder, STX_H, STX_L, MAX_PACK_SIZE, PACK_OVERHEAD
from .cmd_def import (
    JmCmd, JmErr, JmTlmBit, JmParamType,
    TopFsm, RunState, TOP_FSM_CN, RUN_STATE_CN,
    CalibState, CalibFailReason, CALIB_STATE_CN, CALIB_FAIL_REASON_CN,
    CALIB_LEVEL_CN, CALIB_SUBMODE_CN,
    cmd_name, err_name, top_fsm_name, run_state_name, motion_state_name,
    calib_state_name, calib_fail_reason_name, calib_level_submode_name,
    MAGIC_BOOTLOADER, MAGIC_FACTORY_RESET,
)
from . import codec
from .feedback import (
    FeedbackData, parse_state, parse_read_reply, parse_telemetry,
    parse_calib_status,
)
from .registry import (
    ProtocolRegistry, CommandSpec, ParamSpec, Field, get_registry,
)
from .fault_codes import (
    load_fault_csv, fault_code_lookup, fault_codes_by_source,
    fault_mask_decode, fault_mask_to_str,
    err_name_cn, err_name_cn_short,
    FAULT_BIT_INFO,
)

__all__ = [
    'crc16_calc',
    'FrameCodec', 'FrameDecoder', 'STX_H', 'STX_L', 'MAX_PACK_SIZE', 'PACK_OVERHEAD',
    'JmCmd', 'JmErr', 'JmTlmBit', 'JmParamType',
    'TopFsm', 'RunState', 'TOP_FSM_CN', 'RUN_STATE_CN',
    'CalibState', 'CalibFailReason',
    'CALIB_STATE_CN', 'CALIB_FAIL_REASON_CN',
    'CALIB_LEVEL_CN', 'CALIB_SUBMODE_CN',
    'cmd_name', 'err_name', 'top_fsm_name', 'run_state_name', 'motion_state_name',
    'calib_state_name', 'calib_fail_reason_name', 'calib_level_submode_name',
    'MAGIC_BOOTLOADER', 'MAGIC_FACTORY_RESET',
    'codec',
    'FeedbackData', 'parse_state', 'parse_read_reply', 'parse_telemetry',
    'parse_calib_status',
    'ProtocolRegistry', 'CommandSpec', 'ParamSpec', 'Field', 'get_registry',
    'load_fault_csv', 'fault_code_lookup', 'fault_codes_by_source',
    'fault_mask_decode', 'fault_mask_to_str',
    'err_name_cn', 'err_name_cn_short',
    'FAULT_BIT_INFO',
]
