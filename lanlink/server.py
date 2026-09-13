"""默认服务器地址 —— 也就是"对面要连哪儿"。

解决的问题：对面不该被要求填地址。他只填**房间号 + 口令**，剩下的这台
机器早就知道了。

地址从哪来，按优先级：

1. 配置文件里的 ``server`` 段（``lanlink server set host:port``）
2. 打包时编进 exe 的（``python packaging/build.py --server host:port``）
3. 都没有 → 退回老路子，让用户自己填地址 / 搜局域网

第 2 条是关键：你把地址编进 exe，把那个 exe 发给对面，对面就什么都不用配。
第 1 条是给你自己用的 —— 换了机器、换了域名，不用重新打包。

``host`` 可以是域名（配合 ``lanlink ddns`` 用，这样地址变了也不用重新打包），
也可以是 IP。IPv6 要加方括号，跟命令行别处一致。
"""

from __future__ import annotations

from typing import Optional, Tuple

from . import config
from .link import format_addr, parse_addr

__all__ = [
    "SECTION",
    "load",
    "save",
    "clear",
    "baked",
    "resolve",
    "describe",
    "label",
]

SECTION = "server"


def baked() -> Optional[Tuple[str, int]]:
    """打包时编进 exe 的地址。没有就返回 None。"""
    from . import _build_defaults

    return parse_addr(getattr(_build_defaults, "DEFAULT_SERVER", "") or "")


def label() -> str:
    """打包时写的说明，没有就是空串。"""
    from . import _build_defaults

    return str(getattr(_build_defaults, "LABEL", "") or "").strip()


def load() -> Optional[Tuple[str, int]]:
    """配置文件里的地址。"""
    section = config.get_section(SECTION)
    if section is None:
        return None
    return parse_addr(str(section.get("address") or ""))


def resolve() -> Optional[Tuple[str, int]]:
    """最终该用哪个地址：配置文件优先，其次打包时编进去的。"""
    return load() or baked()


def save(address: str) -> Tuple[str, int]:
    """存下地址。格式不对会抛 ValueError。"""
    parsed = parse_addr(address)
    if parsed is None:
        raise ValueError(
            f"地址格式不对：{address!r}。应该是 host:端口，"
            f"比如 yourname.dynv6.net:50001；IPv6 要加方括号，比如 [240e::1]:50001。"
        )
    config.set_section(SECTION, {"address": format_addr(*parsed)})
    return parsed


def clear() -> bool:
    """删掉配置里的地址。注意：打包时编进去的那个还留着，会重新生效。"""
    return config.delete_section(SECTION)


def describe() -> str:
    """给用户看的一行说明：现在用的是哪个、从哪来的。"""
    configured = load()
    if configured is not None:
        return f"服务器地址：{format_addr(*configured)}（配置文件里设的）"

    from_build = baked()
    if from_build is not None:
        text = f"服务器地址：{format_addr(*from_build)}（打包时编进 exe 的）"
        if label():
            text += f"\n这份 exe：{label()}"
        return text

    return ("还没设服务器地址 —— 对面就得自己填地址，或者两边在同一局域网里搜。\n"
            "  想让对面只填房间号和口令：lanlink server set yourname.dynv6.net:50001")
