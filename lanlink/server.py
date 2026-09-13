"""默认服务器 —— 也就是"对面要连哪儿"。

解决的问题：对面不该被要求填地址。他只填**房间号 + 口令**，剩下的这台
机器早就知道了。

服务器有两种，行为完全不同，所以必须分清楚：

``direct``
    直连。对面直接连到你机器上（你必须有公网可达的地址，比如 IPv6）。
    用的是 ``--addr`` 那套。
``relay``
    中继。两边都主动连到一台有公网 IP 的机器上，由它牵线。
    **你自己不需要能被外部访问** —— 家用宽带被 CGNAT 或防火墙挡住时，
    这是唯一还走得通的路。用的是 ``--relay`` 那套。

配错了会很迷惑：两种模式连的是同一个地址，但说的话完全不一样，直连模式
去连中继端口只会得到一个莫名其妙的失败。所以模式是显式存的，不靠猜。

地址从哪来，按优先级：

1. 配置文件里的 ``server`` 段（``lanlink server set ...``）
2. 打包时编进 exe 的（``packaging/build.py --server ...``）
3. 都没有 → 退回老路子，让用户自己填地址 / 搜局域网
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import config
from .link import format_addr, parse_addr

__all__ = [
    "SECTION",
    "MODE_DIRECT",
    "MODE_RELAY",
    "MODES",
    "Endpoint",
    "load",
    "save",
    "clear",
    "baked",
    "resolve",
    "describe",
    "label",
]

SECTION = "server"

#: 沿用 config 模块的路径逻辑（含 LANLINK_CONFIG_DIR 覆盖）。
config_dir = config.config_dir
config_path = config.config_path

MODE_DIRECT = "direct"
MODE_RELAY = "relay"
MODES = (MODE_DIRECT, MODE_RELAY)


@dataclass(frozen=True)
class Endpoint:
    """一个"对面该连哪儿"的答案。"""

    host: str
    port: int
    mode: str = MODE_DIRECT
    #: 中继口令。只有中继模式用得上；直连模式恒为空。
    token: str = ""

    @property
    def is_relay(self) -> bool:
        return self.mode == MODE_RELAY

    @property
    def address(self) -> str:
        return format_addr(self.host, self.port)

    @property
    def kind(self) -> str:
        return "中继" if self.is_relay else "直连"


def _endpoint(host: str, port: int, mode: str, token: str = "") -> Endpoint:
    if mode not in MODES:
        # 配置被人手改坏了：宁可按直连处理，也别让程序起不来
        mode = MODE_DIRECT
    return Endpoint(host, port, mode, token if mode == MODE_RELAY else "")


def baked() -> Optional[Endpoint]:
    """打包时编进 exe 的地址。没有就返回 None。"""
    from . import _build_defaults

    parsed = parse_addr(getattr(_build_defaults, "DEFAULT_SERVER", "") or "")
    if parsed is None:
        return None
    return _endpoint(parsed[0], parsed[1],
                     str(getattr(_build_defaults, "SERVER_MODE", MODE_DIRECT) or MODE_DIRECT),
                     str(getattr(_build_defaults, "RELAY_TOKEN", "") or ""))


def label() -> str:
    """打包时写的说明，没有就是空串。"""
    from . import _build_defaults

    return str(getattr(_build_defaults, "LABEL", "") or "").strip()


def load() -> Optional[Endpoint]:
    """配置文件里的地址。"""
    section = config.get_section(SECTION)
    if section is None:
        return None
    parsed = parse_addr(str(section.get("address") or ""))
    if parsed is None:
        return None
    return _endpoint(parsed[0], parsed[1],
                     str(section.get("mode") or MODE_DIRECT),
                     str(section.get("token") or ""))


def resolve() -> Optional[Endpoint]:
    """最终该用哪个：配置文件优先，其次打包时编进去的。"""
    return load() or baked()


def save(address: str, *, mode: str = MODE_DIRECT, token: str = "") -> Endpoint:
    """存下地址。格式或模式不对会抛 ValueError。"""
    if mode not in MODES:
        raise ValueError(f"模式只能是 {' 或 '.join(MODES)}，收到 {mode!r}")
    parsed = parse_addr(address)
    if parsed is None:
        raise ValueError(
            f"地址格式不对：{address!r}。应该是 host:端口，"
            f"比如 yourname.dynv6.net:50001；IPv6 要加方括号，比如 [240e::1]:50001。"
        )
    endpoint = _endpoint(parsed[0], parsed[1], mode, token.strip())
    section = {"address": endpoint.address, "mode": endpoint.mode}
    if endpoint.token:
        section["token"] = endpoint.token
    config.set_section(SECTION, section)
    return endpoint


def clear() -> bool:
    """删掉配置里的地址。注意：打包时编进去的那个还留着，会重新生效。"""
    return config.delete_section(SECTION)


def describe() -> str:
    """给用户看的一行说明：现在用的是哪个、哪种模式、从哪来的。"""
    configured = load()
    if configured is not None:
        return f"服务器：{configured.address}（{configured.kind}，配置文件里设的）"

    from_build = baked()
    if from_build is not None:
        text = f"服务器：{from_build.address}（{from_build.kind}，打包时编进 exe 的）"
        if from_build.is_relay and from_build.token:
            text += "\n中继口令：已编进去（不回显）"
        if label():
            text += f"\n这份 exe：{label()}"
        return text

    return ("还没设服务器 —— 对面就得自己填地址，或者两边在同一局域网里搜。\n"
            "  想让对面只填房间号和口令，两条路：\n"
            "    · 你有公网可达的地址（比如 IPv6）：\n"
            "        lanlink server set yourname.dynv6.net:50001\n"
            "    · 家里连不进来（CGNAT / 防火墙挡着），走中继：\n"
            "        lanlink server set 1.2.3.4:9000 --relay --token 中继口令")
