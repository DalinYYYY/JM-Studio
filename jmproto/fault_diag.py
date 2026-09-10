# -*- coding: utf-8 -*-
"""故障管理诊断 (0xAA/0xAB) 编解码

对应固件 FaultManager/fault_manager.c + jm_proto.c 故障诊断分支(协议 v1.9):
- 0xAA 故障摘要: 无载荷请求, 应答 18B
    compat_mask(u32) + warn_mask(u32) + level_active(u32)
    + top_code(u16) + active_cnt(u8) + derate_pct(u8) + hist_cnt(u8) + rsv(u8)
- 0xAB 故障详情: 请求 2B {type:u8; idx:u8}, 应答 20B
    code(u16) + status(u8) + rsv(u8) + count(u32) + first_ms(u32) + last_ms(u32) + value(f32)
"""
from dataclasses import dataclass

from .codec import rd_f32, rd_u16, rd_u32, rd_u8, wr_u16, wr_u8

# ---------------- 载荷长度 ----------------
SUMMARY_LEN = 18        # 0xAA 应答长度
DETAIL_LEN = 20         # 0xAB 应答长度

# ---------------- 详情记录类型 ----------------
TYPE_ACTIVE = 0    # 活动记录(按优先级序, idx ∈ [0, active_cnt))
TYPE_HISTORY = 1   # 历史记录(时间倒序, idx ∈ [0, hist_cnt))

# ---------------- level_active 位定义(bit0/1/2) ----------------
LEVEL_CRITICAL_BIT = 1 << 0   # 故障级有活动
LEVEL_EXCEPTION_BIT = 1 << 1  # 异常级有活动
LEVEL_WARNING_BIT = 1 << 2    # 警告级有活动

LEVEL_BITS = (
    (LEVEL_CRITICAL_BIT, "故障级"),
    (LEVEL_EXCEPTION_BIT, "异常级"),
    (LEVEL_WARNING_BIT, "警告级"),
)

# ---------------- 记录状态(fault_status_e) ----------------
STATUS_INACTIVE = 0
STATUS_ACTIVE = 1
STATUS_CLEARED = 2

STATUS_CN = {
    STATUS_INACTIVE: "未激活",
    STATUS_ACTIVE: "活动",
    STATUS_CLEARED: "已清除",
}

# ---------------- 0xB0 清除标志 ----------------
CLEAR_FLAG_LATCHED = 1 << 0   # 清锁存故障(走状态机路径, 仅 IDLE 允许)
CLEAR_FLAG_HISTORY = 1 << 1   # 清历史记录


@dataclass
class FaultSummary:
    """0xAA 故障摘要快照

    compat_mask: 兼容掩码(低13位=旧6故障, bit16-31=其余故障级压缩位, 信息有损)
    warn_mask:   警告压缩位图(异常级 bit=idx&0xF, 警告级 bit16-31=idx&0xF, 信息有损)
    level_active: bit0/1/2 = 故障级/异常级/警告级有活动
    top_code:    最高优先级活动故障码(0=无)
    active_cnt:  活动故障总数
    derate_pct:  当前降功率百分比 0~100 (100=不降额)
    hist_cnt:    历史记录条数
    """
    compat_mask: int = 0
    warn_mask: int = 0
    level_active: int = 0
    top_code: int = 0
    active_cnt: int = 0
    derate_pct: int = 100
    hist_cnt: int = 0

    def level_names(self) -> list:
        """返回有活动的级别中文名列表(故障级/异常级/警告级)。"""
        return [cn for bit, cn in LEVEL_BITS if self.level_active & bit]


@dataclass
class FaultRecord:
    """0xAB 故障详情记录

    ftype/idx: 请求上下文(应答本身不回显, 由客户端携带)
    status:    0=INACTIVE 1=ACTIVE 2=CLEARED(历史记录恒回 2)
    value:     触发时刻物理量(f32 原样, 量纲随码表信号源而定, 不解码)
    """
    ftype: int = 0
    idx: int = 0
    code: int = 0
    status: int = 0
    count: int = 0
    first_ms: int = 0
    last_ms: int = 0
    value: float = 0.0

    @property
    def status_cn(self) -> str:
        return STATUS_CN.get(self.status, str(self.status))


def unpack_summary(raw: bytes) -> FaultSummary:
    """解码 0xAA 故障摘要应答 (18B)"""
    if len(raw) < SUMMARY_LEN:
        raise ValueError(f"摘要应答不足 {SUMMARY_LEN}B: {len(raw)}")
    s = FaultSummary()
    s.compat_mask = rd_u32(raw, 0)
    s.warn_mask = rd_u32(raw, 4)
    s.level_active = rd_u32(raw, 8)
    s.top_code = rd_u16(raw, 12)
    s.active_cnt = rd_u8(raw, 14)
    s.derate_pct = rd_u8(raw, 15)
    s.hist_cnt = rd_u8(raw, 16)
    # raw[17] 保留
    return s


def pack_detail_request(ftype: int, idx: int) -> bytes:
    """编码 0xAB 读请求: type(1) + idx(1)"""
    if ftype not in (TYPE_ACTIVE, TYPE_HISTORY):
        raise ValueError(f"未知详情类型: {ftype}")
    return wr_u8(ftype) + wr_u8(idx)


def unpack_detail(raw: bytes, ftype: int = 0, idx: int = 0) -> FaultRecord:
    """解码 0xAB 故障详情应答 (20B), ftype/idx 为请求上下文"""
    if len(raw) < DETAIL_LEN:
        raise ValueError(f"详情应答不足 {DETAIL_LEN}B: {len(raw)}")
    r = FaultRecord()
    r.ftype = ftype
    r.idx = idx
    r.code = rd_u16(raw, 0)
    r.status = rd_u8(raw, 2)
    # raw[3] 保留
    r.count = rd_u32(raw, 4)
    r.first_ms = rd_u32(raw, 8)
    r.last_ms = rd_u32(raw, 12)
    r.value = rd_f32(raw, 16)
    return r


