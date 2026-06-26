"""数字孪生虚拟引擎(高保真仿真, 无需下位机)。

模块化分层:
  twin_config       —— 电机参数加载 (MotorParam, 对齐固件 motor_param_t)
  twin_physics      —— 物理模型 (dq电气方程 + 机械方程 + 热模型 + 编码器)
  twin_load         —— 关节负载模型 (连杆惯量/重力矩/外力扰动/碰撞检测)
  twin_control      —— 控制算法复刻 (PID + FOC + 级联控制 + 无扰切换)
  twin_fsm          —— 状态机复刻 (IDLE/READY/RUN/FAULT/SAFETY + 故障检测)
  digital_twin      —— 多速率仿真引擎 (电流环10kHz / 位置环2kHz + QThread)
  twin_responder    —— 协议应答器 (协议命令 <-> MotorCmd, 遥测帧构造)
  virtual_transport —— Transport 实现 (QTimer 驱动孪生仿真 + 帧回环)

旧版 motor_sim/responder 作为轻量后备保留。
上层 JmClient/UI 无需感知差异: 选择"数字孪生引擎"即用 VirtualTransport。
"""

# 高保真数字孪生
from .twin_config import MotorParam, load_motor_param
from .twin_physics import MotorPhysics
from .twin_load import JointLoad
from .twin_control import ControllerCore, CascadeFeedback, MotorRef, RefCtrlType
from .twin_fsm import (SystemStateMachine, FaultDetector, MotorCmd,
                        ControlMode, SystemState, RunState, Fault)
from .digital_twin import DigitalTwinEngine
from .twin_responder import TwinResponder
from .virtual_transport import VirtualTransport

# 旧版轻量仿真(后备)
from .motor_sim import MotorSim
from .responder import VirtualResponder

__all__ = [
    # 数字孪生核心
    "MotorParam", "load_motor_param",
    "MotorPhysics", "JointLoad",
    "ControllerCore", "CascadeFeedback", "MotorRef", "RefCtrlType",
    "SystemStateMachine", "FaultDetector", "MotorCmd",
    "ControlMode", "SystemState", "RunState", "Fault",
    "DigitalTwinEngine", "TwinResponder",
    "VirtualTransport",
    # 旧版后备
    "MotorSim", "VirtualResponder",
]
