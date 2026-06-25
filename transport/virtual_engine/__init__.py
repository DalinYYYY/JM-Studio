"""虚拟数据引擎(演示用, 无需下位机)。

模块化分层:
  motor_sim.MotorSim        —— 纯逻辑物理仿真(位置/速度/电流/温度…)
  responder.VirtualResponder —— 纯逻辑协议应答器(命令->应答帧, 周期遥测帧)
  virtual_transport.VirtualTransport —— Transport 实现(QTimer 驱动, 回环收发)

上层 JmClient/UI 无需感知差异: 在连接面板选择"虚拟数据引擎"即用 VirtualTransport
取代 SerialTransport, 其余链路完全一致。
"""

from .motor_sim import MotorSim
from .responder import VirtualResponder
from .virtual_transport import VirtualTransport

__all__ = ["MotorSim", "VirtualResponder", "VirtualTransport"]
