#!/usr/bin/env python3
"""测试员版打包入口: 以 tester 形态启动(JM_PROFILE 环境变量注入)。

    python tester_entry.py                      # 源码运行
    PyInstaller 打包后双击 exe 即为测试员精简版

形态配置见 resources/app_profiles.json 的 profiles.tester 节。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["JM_PROFILE"] = "tester"

from main import main

if __name__ == "__main__":
    main()
