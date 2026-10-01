# -*- coding: utf-8 -*-
"""LANBench 启动器（等价于 python -m lanbench）。

    python lanbench.py                 # GUI
    python lanbench.py serve           # 只跑服务端
    python lanbench.py test 192.168.1.20
"""

from lanbench.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
