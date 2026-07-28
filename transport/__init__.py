"""
传输层: 可插拔的物理链路实现

- Transport      : 抽象基类
- SerialTransport: 串口实现
- CanTransport   : CAN 实现(python-can 未安装时不可用, 懒加载)
"""

from .base import Transport
from .serial_transport import SerialTransport

# CanTransport 依赖 python-can; 未安装时不影响包导入, 由 connection_panel 的
# _detect_can_support 捕获 ImportError 隐藏 CAN 选项。
try:
    from .can_transport import CanTransport  # noqa: F401
except ImportError:
    CanTransport = None  # type: ignore[assignment]

__all__ = ['Transport', 'SerialTransport', 'CanTransport']
