#!/usr/bin/env python3
"""
关节电机上位机 - 主程序入口

用法:
    python main.py                  开发版(全功能)
    python main.py --profile tester 测试员版(精简形态, 见 resources/app_profiles.json)
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


def _resolve_profile(argv):
    """解析 --profile 参数(优先) 与 JM_PROFILE 环境变量, 无则返回 None。
    返回 (profile, qt_argv): qt_argv 为剔除 profile 参数后的 argv, 供 QApplication 使用。"""
    profile = os.environ.get("JM_PROFILE") or None
    qt_argv = [argv[0]] if argv else []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--profile" and i + 1 < len(argv):
            profile = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--profile="):
            profile = arg.split("=", 1)[1]
            i += 1
            continue
        qt_argv.append(arg)
        i += 1
    return profile, qt_argv


def main():
    # 高DPI支持
    if hasattr(Qt, 'HighDpiScaleFactorRoundingPolicy'):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    profile_name, qt_argv = _resolve_profile(sys.argv)

    app = QApplication(qt_argv)
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

    # 按应用形态创建主窗口: 默认开发版; tester 形态为测试员精简版
    window = None
    if profile_name:
        from ui.app_profile import load_profile
        profile = load_profile(profile_name)
        if profile is not None:
            from ui.tester_main_window import TesterMainWindow
            window = TesterMainWindow(profile)
        else:
            print(f"[profile] 未找到 '{profile_name}', 回退开发版 "
                  f"(可用形态见 resources/app_profiles.json)")
    if window is None:
        from ui.main_window import MainWindow
        window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
