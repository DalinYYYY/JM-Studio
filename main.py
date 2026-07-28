#!/usr/bin/env python3
"""
关节电机上位机 - 主程序入口

用法:
    python main.py
    或安装依赖后运行: pip install -r requirements.txt && python main.py
"""

import sys
import os
import warnings

# 确保当前目录在搜索路径中, 方便导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 屏蔽 python-can 的 uptime 库警告(不影响功能, 仅时间戳基准差异)
warnings.filterwarnings("ignore", message=".*uptime library not available.*")

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

# 启动时加载功能开关(触发单例缓存), 控制是否加载特定功能模块
from ui import feature_flags
feature_flags.get_enabled_features()  # 预热缓存

from ui import APP_NAME, APP_VERSION
from ui.main_window import MainWindow


def main():
    # 高DPI支持
    if hasattr(Qt, 'HighDpiScaleFactorRoundingPolicy'):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    # 设置默认字体(等宽字体用于数据显示)
    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)

    # 主题: 读取上次选择(默认深色)并铺设全局 QSS, 再建窗口
    from ui.theme import theme
    from ui import layout_store
    theme.set(layout_store.get_meta("theme", "dark"))
    app.setStyleSheet(theme.qss())

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
