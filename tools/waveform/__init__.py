"""
波形显示上位机工具子包。

一个独立运行的关节电机波形显示上位机, 不依赖项目内 jmproto / transport / ui,
可整体拷贝到其他工程使用。

模块:
- protocol_reference: 通信协议实现 (串口/CAN/虚拟电机)
- transport: 传输层 Qt 适配 (后台线程 + 信号驱动)
- waveform_plot: 波形显示核心 (环形缓冲 + 多通道 + 右键菜单 + 触发 + FFT)
- trigger: 触发捕获对话框
- fft_panel: FFT 频谱分析
- exporter: CSV/PNG 导出
- app: 主窗口 + 连接面板
- main: 程序入口

运行:
    python -m tools.waveform.main
    或
    python tools/waveform/main.py

依赖: PyQt6, pyqtgraph, numpy, pyserial (串口) / python-can (CAN, 可选)
"""
