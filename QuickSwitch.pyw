#!/usr/bin/env pythonw
"""QuickSwitch 启动入口（.pyw 用 pythonw 运行，不弹控制台窗口）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from quickswitch.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
