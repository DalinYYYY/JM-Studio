"""
传输层: 可插拔的物理链路实现

- Transport      : 抽象基类
- SerialTransport: 串口实现
- CanTransport   : CAN 实现(占位)
"""

from .base import Transport
from .serial_transport import SerialTransport
from .can_transport import CanTransport

__all__ = ['Transport', 'SerialTransport', 'CanTransport']
