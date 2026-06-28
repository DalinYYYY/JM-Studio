"""
波形显示上位机入口。

用法:
    python -m tools.waveform.main
    或
    python tools/waveform/main.py
"""
from __future__ import annotations

import sys
import os

# 让脚本可以直接运行 (python tools/waveform/main.py)
if __name__ == '__main__' and __package__ is None:
    # 上溯到 pyqt_gui/ 加入 sys.path, 让 `from tools.waveform.xxx` 可用
    _here = os.path.dirname(os.path.abspath(__file__))
    _root = os.path.dirname(os.path.dirname(_here))  # pyqt_gui/
    if _root not in sys.path:
        sys.path.insert(0, _root)
    __package__ = 'tools.waveform'

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from .app import MainWindow


def main():
    # 高 DPI 支持
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("JointMotor Waveform")

    # 深色主题样式
    app.setStyleSheet("""
        QMainWindow { background: #2B2B2B; }
        QWidget { color: #E0E0E0; }
        QGroupBox {
            border: 1px solid #555;
            border-radius: 4px;
            margin-top: 8px;
            padding-top: 8px;
            font-weight: bold;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 6px;
            padding: 0 4px;
        }
        QPushButton {
            background: #3A3A3A;
            border: 1px solid #555;
            padding: 4px 10px;
            border-radius: 3px;
        }
        QPushButton:hover { background: #4A4A4A; }
        QPushButton:pressed { background: #2A2A2A; }
        QPushButton:disabled { color: #666; background: #2A2A2A; }
        QComboBox {
            background: #3A3A3A;
            border: 1px solid #555;
            padding: 2px 6px;
            border-radius: 2px;
        }
        QComboBox QAbstractItemView {
            background: #3A3A3A;
            selection-background-color: #4A6A9A;
        }
        QSpinBox, QDoubleSpinBox {
            background: #3A3A3A;
            border: 1px solid #555;
            padding: 2px 4px;
            border-radius: 2px;
        }
        QCheckBox { spacing: 6px; }
        QCheckBox::indicator {
            width: 14px; height: 14px;
            border: 1px solid #777;
            background: #2B2B2B;
            border-radius: 2px;
        }
        QCheckBox::indicator:checked {
            background: #4CAF50;
            border: 1px solid #4CAF50;
        }
        QStatusBar { background: #1E1E1E; }
        QStatusBar QLabel { padding: 0 8px; }
        QMenuBar { background: #2B2B2B; }
        QMenuBar::item:selected { background: #4A6A9A; }
        QMenu { background: #2B2B2B; border: 1px solid #555; }
        QMenu::item:selected { background: #4A6A9A; }
    """)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
