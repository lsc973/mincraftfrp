"""图形界面（tkinter）。

不用记命令行参数，双击就能用：

    python -m lanlink gui

打包成 exe 之后，直接双击 exe 也会打开这个界面。
"""

from .app import LanlinkApp, main

__all__ = ["LanlinkApp", "main"]
