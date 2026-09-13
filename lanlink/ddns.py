"""免费动态域名（DDNS）—— 用一个短名字代替那一长串 IPv6。

解决的问题：IPv6 直连本来是最省事的路子（不用中继、对面不用装东西），
但地址是 ``240e:354:311:a200:f587:f2f5:4fb3:bae2`` 这种东西，四十个字符，
而且 Windows 默认用的还是**临时地址**，过一阵自己就变了。让对面记这个、
或者每变一次就重新通知一遍，都不现实。

免费动态域名服务给一个固定的短名字（``yourname.dynv6.net``），我们负责
在地址变化时把这个名字指过去。对面只要记住名字就行::

    lanlink-cli.exe tunnel --addr yourname.dynv6.net:50001 --listen 25565

**只用了标准库**（``http.client`` + ``ssl``），跟项目其余部分一致。

支持的服务商
------------

``dynv6``
    免费、专做 IPv6、不会过期催你续期，注册完在域名详情页拿 token。
    https://dynv6.com
``duckdns``
    老牌免费服务，注册后在首页就能看到 token。
    https://www.duckdns.org

两家的 API 都是"GET 一个带 token 的 URL，看返回的文本"。差别只在 URL
形状和成功时返回什么，所以用一个小类把这两点包起来就够了。

安全提醒
--------

token 等同于这个域名的写权限 —— 拿到它的人可以把这个名字指到任何地方。
所以配置文件按 ``0o600`` 存（POSIX 上有效，Windows 上靠用户目录的 ACL），
而且**报错信息里绝不能带上完整 URL**，否则 token 会顺着日志漏出去。
"""

from __future__ import annotations

import http.client
import socket
import urllib.parse
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Tuple

from . import config

__all__ = [
    "DdnsError",
    "DdnsConfig",
    "PROVIDERS",
    "config_path",
    "load",
    "save",
    "clear",
    "publish",
    "resolve",
    "verify",
    "describe_setup",
]

#: HTTP 超时。DNS 服务商偶尔慢，但也不能让界面卡太久。
TIMEOUT = 10.0


class DdnsError(Exception):
    """更新失败。消息是给用户看的，里面不能出现 token。"""


# ---------------------------------------------------------------- 服务商


class _Provider:
    """一家的 API 长什么样。"""

    key = ""
    label = ""
    #: 域名长什么样，用来给用户举例
    example = ""
    #: 在哪儿注册、在哪儿拿 token
    signup = ""

    def update_url(self, hostname: str, token: str, address: str) -> str:
        raise NotImplementedError

    def interpret(self, status: int, body: str) -> None:
        """成功就正常返回，失败抛 DdnsError。"""
        raise NotImplementedError

    def normalize_hostname(self, hostname: str) -> str:
        return hostname.strip().lower().rstrip(".")


class _DynV6(_Provider):
    key = "dynv6"
    label = "dynv6"
    example = "yourname.dynv6.net"
    signup = "到 https://dynv6.com 注册 → 建一个 zone → 在 zone 的详情页拿 HTTP token"

    def update_url(self, hostname, token, address):
        query = urllib.parse.urlencode({
            "hostname": hostname, "token": token, "ipv6": address,
        })
        return f"https://dynv6.com/api/update?{query}"

    def interpret(self, status, body):
        """dynv6 **以 HTTP 状态码为准**，不看正文。

        官方推荐的那个更新脚本用的就是 ``curl -fsS`` —— ``-f`` 让它在状态码
        非 2xx 时直接失败，正文压根不解析。一开始这里要求正文里必须出现
        "updated"/"unchanged"，那是个没验证过的假设：服务商哪天换个措辞，
        就会把更新成功报成失败，用户白折腾。实测坏 token 回的是 401，
        所以 200 就是成功。

        正文只在明确说"无效/被拒"的时候才拿来当失败依据 —— 这只是个额外的
        保险，不是主要判据。
        """
        text = body.strip()
        low = text.lower()

        if status in (401, 403):
            raise DdnsError(
                "dynv6 说令牌不对。到 dynv6.com 的 zone 详情页重新复制一次 HTTP token"
                "（注意别复制成别的 zone 的）。"
            )
        if status == 404:
            raise DdnsError(
                "dynv6 说找不到这个域名。确认域名拼写，"
                "而且要先在 dynv6 上把这个 zone 建出来。"
            )
        if status == 200:
            if any(word in low for word in ("invalid", "error", "denied", "forbidden")):
                raise DdnsError(f"dynv6 拒绝了这次更新：{_short(text)}")
            return
        raise DdnsError(f"dynv6 返回 {status}：{_short(text)}")


class _DuckDNS(_Provider):
    key = "duckdns"
    label = "DuckDNS"
    example = "yourname.duckdns.org"
    signup = "到 https://www.duckdns.org 用 Google/GitHub 登录，首页就能看到 token"

    def normalize_hostname(self, hostname: str) -> str:
        name = super().normalize_hostname(hostname)
        # duckdns 的 domains 参数只要子域名，不要 .duckdns.org 后缀
        if name.endswith(".duckdns.org"):
            name = name[: -len(".duckdns.org")]
        return name

    def update_url(self, hostname, token, address):
        # 注意这里传的是剥掉后缀的子域名
        query = urllib.parse.urlencode({
            "domains": self.normalize_hostname(hostname),
            "token": token,
            "ipv6": address,
        })
        return f"https://www.duckdns.org/update?{query}"

    def interpret(self, status, body):
        """DuckDNS 跟 dynv6 不一样：成功失败**都是 200**，只能看正文。

        它整个 API 就两个返回值 —— ``OK`` 和 ``KO``，所以这里必须解析正文，
        没有别的办法。
        """
        text = body.strip()
        upper = text.upper()
        if upper.startswith("KO"):
            raise DdnsError(
                "DuckDNS 拒绝了这次更新（返回 KO）—— 通常是 token 或子域名不对。"
                "到 duckdns.org 首页核对一下。"
            )
        if status == 200 and upper.startswith("OK"):
            return
        if status != 200:
            raise DdnsError(f"DuckDNS 返回 {status}：{_short(text)}")
        raise DdnsError(f"DuckDNS 返回了看不懂的响应：{_short(text)}")


PROVIDERS = {p.key: p for p in (_DynV6(), _DuckDNS())}

DEFAULT_PROVIDER = "dynv6"


def _short(text: str, limit: int = 120) -> str:
    text = " ".join((text or "").split())
    if not text:
        return "（空响应）"
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------- 配置


@dataclass
class DdnsConfig:
    provider: str
    hostname: str
    token: str

    def public(self) -> dict:
        """给界面/日志看的版本 —— 不带 token。"""
        return {"provider": self.provider, "hostname": self.hostname,
                "token_set": bool(self.token)}

    @property
    def label(self) -> str:
        return PROVIDERS[self.provider].label if self.provider in PROVIDERS else self.provider


#: 配置存在 config.json 的哪一段里。读写都走 config 模块，别自己开文件 ——
#: 服务器地址存在同一个文件的另一段，各写一份会互相覆盖。
SECTION = "ddns"

#: 沿用 config 模块的目录逻辑（含 LANLINK_CONFIG_DIR 覆盖）。
config_dir = config.config_dir
config_path = config.config_path


def load() -> Optional[DdnsConfig]:
    """读出配置。没配过或者配置不完整都返回 None。"""
    section = config.get_section(SECTION)
    if section is None:
        return None
    provider = str(section.get("provider") or DEFAULT_PROVIDER).strip().lower()
    hostname = str(section.get("hostname") or "").strip()
    token = str(section.get("token") or "").strip()
    if not hostname or not token:
        return None
    if provider not in PROVIDERS:
        return None
    return DdnsConfig(provider=provider, hostname=hostname, token=token)


def save(config_: DdnsConfig) -> Path:
    """写下配置。其他段落原样保留。"""
    if config_.provider not in PROVIDERS:
        raise DdnsError(f"不认识的服务商：{config_.provider}")

    hostname = config_.hostname.strip()
    if not hostname or "." not in hostname:
        raise DdnsError(
            f"域名看起来不对：{hostname!r}（应该像 {PROVIDERS[config_.provider].example}）")
    if not config_.token.strip():
        raise DdnsError("token 不能为空")

    try:
        return config.set_section(SECTION, asdict(DdnsConfig(
            config_.provider, hostname, config_.token.strip())))
    except OSError as exc:
        raise DdnsError(f"写配置文件失败：{exc}") from exc


def clear() -> bool:
    """删掉 DDNS 配置。本来就没有则返回 False。"""
    try:
        return config.delete_section(SECTION)
    except OSError as exc:
        raise DdnsError(f"写配置文件失败：{exc}") from exc


# ---------------------------------------------------------------- 发请求


def _get(url: str, timeout: float = TIMEOUT) -> Tuple[int, str]:
    """发一个 GET，返回 (状态码, 正文)。

    **异常信息里绝不能带上 url** —— token 就在查询串里。
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    if not host:
        raise DdnsError("API 地址不对（没有主机名）")
    https = parts.scheme == "https"
    port = parts.port or (443 if https else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    conn_cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
    conn = conn_cls(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={
            "User-Agent": "lanlink", "Connection": "close",
        })
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8", errors="replace")
    except socket.timeout:
        raise DdnsError(
            f"连 {host} 超时。检查网络，或者这个服务在你那儿是不是被挡了。"
        ) from None
    except OSError as exc:
        raise DdnsError(f"连 {host} 失败：{exc}") from None
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 对外动作


def publish(address: str, config: Optional[DdnsConfig] = None,
            timeout: float = TIMEOUT) -> str:
    """把域名指向这个地址。返回域名。

    成功与否以服务商的回答为准 —— 有的服务商（dynv6）地址没变时返回
    "unchanged"，那也算成功。
    """
    config = config or load()
    if config is None:
        raise DdnsError("还没配置免费域名。先跑一次 lanlink ddns setup。")
    if not address:
        raise DdnsError("没有可用的地址可以发布")

    provider = PROVIDERS.get(config.provider)
    if provider is None:
        raise DdnsError(f"不认识的服务商：{config.provider}")

    url = provider.update_url(config.hostname, config.token, address)
    try:
        status, body = _get(url, timeout=timeout)
        provider.interpret(status, body)
    except DdnsError as exc:
        # 兜底：万一条路径（或者服务商回显了请求）把 token 带进了消息里，
        # 这里再抹一道。日志是会被人看到、被贴出来问问题的。
        raise DdnsError(_redact(str(exc), config.token)) from None
    except Exception as exc:
        # 连 DdnsError 都不是的意外（ssl 报错、服务商回了个畸形响应把我们
        # 自己的代码搞崩……）也得拦住 —— 这种消息最不可控，最容易把整条
        # URL 连着 token 一起带出来。
        raise DdnsError(_redact(f"更新域名失败：{exc}", config.token)) from None
    return provider.normalize_hostname(config.hostname)


def _redact(text: str, secret: str) -> str:
    """把消息里的 token 抹掉。太短的不抹 —— 那多半是误伤正常文字。"""
    if not secret or len(secret) < 6 or secret not in text:
        return text
    return text.replace(secret, "***")


def resolve(hostname: str, timeout: float = 5.0) -> Optional[str]:
    """查这个域名当前的 IPv6 地址。查不到返回 None。"""
    host = hostname.strip().lower().rstrip(".")
    if not host:
        return None
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET6)
    except (socket.gaierror, OSError):
        return None
    finally:
        socket.setdefaulttimeout(previous)

    for info in infos:
        address = info[4][0].split("%")[0]
        if address:
            return address
    return None


def _same_address(left: str, right: str) -> bool:
    """比两个 IPv6 是不是同一个地址。写法可能不一样（压缩、大小写）。"""
    import ipaddress

    try:
        return ipaddress.ip_address(left) == ipaddress.ip_address(right)
    except ValueError:
        return left.strip().lower() == right.strip().lower()


def verify(hostname: str, address: str, attempts: int = 5,
           interval: float = 1.0) -> bool:
    """确认域名真的解析到这个地址了。

    DNS 生效可能要几秒，所以重试几次。**返回 False 不代表更新失败** ——
    多半是本机 DNS 缓存还没过期。调用方该照实说，别一口咬定是坏的。
    """
    import time

    for attempt in range(max(1, attempts)):
        found = resolve(hostname)
        if found and _same_address(found, address):
            return True
        if attempt < attempts - 1:
            time.sleep(interval)
    return False


def describe_setup(provider_key: str = DEFAULT_PROVIDER) -> str:
    """给用户看的注册指引。"""
    provider = PROVIDERS.get(provider_key)
    if provider is None:
        return f"不认识的服务商：{provider_key}"
    return (
        f"{provider.label}：{provider.signup}\n"
        f"  域名长这样：{provider.example}"
    )
