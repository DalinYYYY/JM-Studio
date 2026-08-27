"""UI 层: 主窗口与各面板

应用版本号集中定义在此, main.py 与 main_window.py 均从此处引用,
升级版本时只需修改 APP_VERSION 一处.
"""

APP_NAME = "JM Studio"
APP_VERSION = "1.3.1"

from .main_window import MainWindow

__all__ = ['MainWindow', 'APP_NAME', 'APP_VERSION']
