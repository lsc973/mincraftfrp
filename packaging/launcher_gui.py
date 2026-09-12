"""PyInstaller 的图形版入口。

打包出来的 exe 就是"lanlink 图形界面"这一个东西：双击打开窗口，
不带任何命令行参数的概念。

（想要命令行版就用 packaging/launcher_cli.py，或者直接 python -m lanlink。）
"""

import os
import sys

# 打包后 sys.path[0] 是解包出来的临时目录，加这两个是为了让 import lanlink 找得到
if getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(sys.executable))
    sys.path.insert(0, getattr(sys, "_MEIPASS", ""))

from lanlink.text import force_utf8_when_piped  # noqa: E402

# 必须在任何输出之前调用：被管道/重定向时统一成 UTF-8，
# 否则中文会按系统 ANSI 代码页（GBK）输出，脚本拿到的就是乱码，
# 反过来喂 UTF-8 进去还会解出代理字符把程序弄崩。
force_utf8_when_piped()

from lanlink.gui import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
