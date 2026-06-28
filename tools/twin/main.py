"""数字孪生独立上位机入口。

用法:
    python -m tools.twin.main
    或
    python tools/twin/main.py

脱离协议层/串口/JmClient, 直接驱动 DigitalTwinEngine 运行状态机,
保留数字孪生的参数给定/控制参数/保护参数/推导参数, 并提供独立的
系统控制、运动控制、实时反馈与曲线。
"""

from __future__ import annotations

import sys
import os

# 让脚本可直接运行 (python tools/twin/main.py)
if __name__ == '__main__' and __package__ is None:
    _here = os.path.dirname(os.path.abspath(__file__))
    _root = os.path.dirname(os.path.dirname(_here))   # pyqt_gui/
    if _root not in sys.path:
        sys.path.insert(0, _root)
    __package__ = 'tools.twin'

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication

from ui import layout_store
from ui.theme import theme

from .app import TwinMainWindow


def main():
    # 高 DPI 支持
    if hasattr(Qt, 'HighDpiScaleFactorRoundingPolicy'):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    app = QApplication(sys.argv)
    app.setApplicationName("JointMotor Twin Standalone")
    app.setApplicationVersion("1.0.0")

    # 默认字体
    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)

    # 主题: 默认深色, 铺全局 QSS
    theme.set(layout_store.get_meta("theme", "dark"))
    app.setStyleSheet(theme.qss())

    win = TwinMainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
