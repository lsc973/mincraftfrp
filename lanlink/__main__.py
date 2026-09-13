"""``python -m lanlink`` 的入口。

打包出来的 exe 走 ``packaging/launcher_cli.py``，源码运行走这里 ——
两边得先把标准流拾掇成一样的，否则同一个命令在源码下和 exe 下表现不同：
重定向时中文变成乱码，提示还会卡在缓冲区里不吐出来。
"""

import sys

from .cli import main
from .text import force_utf8_when_piped

if __name__ == "__main__":
    force_utf8_when_piped()
    sys.exit(main())
