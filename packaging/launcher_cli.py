"""PyInstaller 的命令行版入口。

给服务器上用：``lanlink.exe relay --port 9000`` 这种。必须用
``--console`` 打包，否则没有控制台，输出全看不见。
"""

import os
import sys

if getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(sys.executable))
    sys.path.insert(0, getattr(sys, "_MEIPASS", ""))

from lanlink.text import force_utf8_when_piped  # noqa: E402

# 必须在任何输出之前调用：被管道/重定向时统一成 UTF-8，
# 否则中文会按系统 ANSI 代码页（GBK）输出，脚本拿到的就是乱码，
# 反过来喂 UTF-8 进去还会解出代理字符把程序弄崩。
force_utf8_when_piped()

from lanlink.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
