"""数字孪生独立上位机: 脱离协议层, 直接驱动 DigitalTwinEngine 运行状态机。

复用 pyqt_gui 的:
  - transport.virtual_engine (DigitalTwinEngine / SystemStateMachine / MotorCmd)
  - ui.panels.twin_param_panel (给定/控制/推导/保护 四类参数)
  - ui.theme (深/浅主题)

新增:
  - engine_bridge  GUI 线程 QTimer 驱动仿真 + 信号转发
  - control_panel  系统控制 + 运动控制 (直接构造 MotorCmd)
  - feedback_panel 遥测字典展示
  - plot_panel     pyqtgraph 实时曲线
"""
