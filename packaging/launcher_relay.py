"""中继专用入口 —— 给 Linux 服务器用的。

打包出来的 ``lanlink-relay`` 不带参数就直接跑中继，参数原样透传::

    ./lanlink-relay --port 9000 --token-file /etc/lanlink/token
    ./lanlink-relay -v --port 9000
    ./lanlink-relay --help

跟 lanlink-cli 的区别只是省掉打子命令这一步 —— systemd 的 ExecStart
越短越不容易配错。想跑别的子命令也照常透传。
"""

import os
import sys

if getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(sys.executable))
    sys.path.insert(0, getattr(sys, "_MEIPASS", ""))

from lanlink.text import force_utf8_when_piped  # noqa: E402

force_utf8_when_piped()

from lanlink.cli import main  # noqa: E402

#: 顶层的全局标志。它们必须排在子命令**前面**，否则 argparse 会当成
#: 子命令的参数然后报"unrecognized arguments"。
_GLOBAL_FLAGS = {"-v", "--verbose"}

#: 已知的子命令。用户自己写了就照原样走，不再补 relay。
_SUBCOMMANDS = {"host", "list", "join", "relay", "doctor", "gui"}


def _build_argv(argv):
    """决定到底把什么交给 main()。

    这几种情况都要照顾到：

        []                       -> relay
        ["--port", "9000"]       -> relay --port 9000
        ["-v", "--port", "9000"] -> -v relay --port 9000   （-v 得放前面）
        ["relay", "--port", ...] -> 原样
        ["doctor"]               -> 原样
    """
    if argv and argv[0] in _SUBCOMMANDS:
        return list(argv)

    head = []
    rest = list(argv)
    while rest and rest[0] in _GLOBAL_FLAGS:
        head.append(rest.pop(0))
    return head + ["relay"] + rest


if __name__ == "__main__":
    sys.exit(main(_build_argv(sys.argv[1:])))
