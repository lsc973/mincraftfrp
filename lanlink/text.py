"""文本编码的兜底工具。

集中在 Windows 上踩过的两个坑：

**坑一：终端编码。** 中文 Windows 控制台是 GBK。收到 GBK 里没有的字符
（emoji、生僻字）时 ``print`` 会抛 UnicodeEncodeError，把整个程序带走。

**坑二：孤立代理字符。** 当输入不是终端（管道、重定向）时，Python 按系统
ANSI 代码页解码，遇到解不通的字节会退化成 ``\\udc80`` 这类"孤立代理字符"。
这种东西在 Python 里能存能传，但 ``.encode("utf-8")`` 会直接抛异常 ——
用户往管道里喂 UTF-8 就会让程序崩掉。
"""

from __future__ import annotations

import sys

__all__ = ["safe_print", "safe_input", "encode_text", "force_utf8_when_piped"]


def safe_print(text: str = "") -> None:
    """往终端打字，但别因为编码问题把程序搞崩。"""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))
        except Exception:
            print(text.encode("ascii", errors="replace").decode("ascii"))


def safe_input(prompt: str = "") -> str:
    """读一行输入，解码失败也不要抛出去。"""
    try:
        return input(prompt)
    except UnicodeDecodeError:
        safe_print("  （这一行输入的解码方式跟当前终端对不上，已跳过）")
        return ""


def encode_text(text: str) -> bytes:
    """把用户输入变成可以发出去的字节。

    正常情况下就是 UTF-8。但如果字符串里混进了孤立代理字符（见模块开头的
    "坑二"），直接 ``.encode("utf-8")`` 会抛 UnicodeEncodeError 把聊天循环
    整个带走。这时候用 ``surrogateescape`` 能把它们还原成原始字节 ——
    本来就是从字节来的，原路还回去正好。
    """
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        try:
            return text.encode("utf-8", errors="surrogateescape")
        except UnicodeEncodeError:
            return text.encode("utf-8", errors="replace")


def force_utf8_when_piped() -> None:
    """被管道/重定向时把标准流切成 UTF-8。

    直接连终端的时候不用动：Python 在 Windows 上走的是控制台 Unicode 通道，
    中文本来就正常，强行改成 UTF-8 反而会让 GBK 控制台显示乱码。

    但一旦输出被重定向，Python 会退回系统 ANSI 代码页（中文 Windows 是 GBK），
    于是调脚本的人拿到的是一坨 GBK 字节，按 UTF-8 读全是乱码；反过来往里喂
    UTF-8 也会解出代理字符然后崩掉。所以只在"不是终端"的时候切成 UTF-8，
    让脚本调用有个可预期的编码。
    """
    for name in ("stdout", "stderr", "stdin"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # 老 Python 或特殊流，跳过就是
