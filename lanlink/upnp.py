"""UPnP IGD 客户端 —— 跟自家路由器对话。

两件事：

1. **问路由器"你的 WAN 口 IP 是多少"**。这是判断有没有公网 IP 最直接的办法，
   而且不用请求任何外部服务。如果路由器报回来的是 100.64.x.x（运营商大内网）、
   10.x / 172.x / 192.168.x（多层 NAT），那说明你在 CGNAT 后面，
   端口映射救不了你 —— 只能靠中继或虚拟局域网。
   如果报的是公网地址，那恭喜，开个端口对面就能直连了。

2. **让路由器自动开端口**（AddPortMapping）。这样用户不用登路由器后台点来点去，
   点一下按钮就行。

全程标准库：SSDP 用 UDP 组播发现网关，然后 HTTP + SOAP 调它的接口。

之所以自己写而不是装个 `miniupnpc`：这个项目一直是零依赖的，为了一个功能
引入第三方包不划算。UPnP 这套协议本身不复杂。
"""

from __future__ import annotations

import http.client
import ipaddress
import logging
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Optional, Tuple

__all__ = [
    "is_public_address",
    "is_cgnat",
    "discover_gateway",
    "default_gateway",
    "get_external_ip",
    "add_port_mapping",
    "delete_port_mapping",
    "GatewayError",
]

log = logging.getLogger("lanlink.upnp")

#: SSDP 的组播地址。所有 UPnP 设备都在这儿等着被发现。
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

#: 我们要找的是"能管外网连接的网关设备"。
_SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
)

#: 负责端口映射的服务。WANIPConnection 是有公网 IP 的，WANPPPConnection 是拨号的。
_WAN_SERVICE_HINTS = ("WANIPConnection", "WANPPPConnection")

_SOAP_ENVELOPE = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:{action} xmlns:u="{service}">
      {args}
    </u:{action}>
  </s:Body>
</s:Envelope>"""


class GatewayError(Exception):
    """跟路由器对话失败。"""


# ---------------------------------------------------------------- 地址判定


def is_cgnat(address: str) -> bool:
    """是不是运营商大内网地址（100.64.0.0/10，RFC 6598）。

    这个段里的地址**不是**公网地址 —— 你在运营商的大内网里，
    外面的人根本路由不到你，端口映射没用。
    """
    try:
        return ipaddress.ip_address(address) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def is_public_address(address: str) -> bool:
    """是不是真正的公网地址。

    CGNAT 段（100.64/10）虽然是"全球唯一"的，但路由不到，所以不算。
    """
    if not address:
        return False
    if is_cgnat(address):
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


# ---------------------------------------------------------------- SSDP 发现


def default_gateway() -> Optional[str]:
    """从系统路由表里读出 IPv4 默认网关。拿不到返回 None。"""
    try:
        if sys.platform.startswith("win"):
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | "
                 "Select-Object -ExpandProperty NextHop"],
                capture_output=True, timeout=12, check=False,
            )
            for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
                candidate = line.strip()
                if candidate.count(".") == 3 and not candidate.startswith("127."):
                    return candidate
            return None

        # Linux：/proc/net/route 第二列是目的网络，第三列是网关（小端十六进制）
        with open("/proc/net/route", encoding="ascii", errors="replace") as handle:
            for line in handle.readlines()[1:]:
                fields = line.split()
                if len(fields) >= 3 and fields[1] == "00000000" and fields[2] != "00000000":
                    raw = int(fields[2], 16)
                    return ".".join(str((raw >> (8 * i)) & 0xFF) for i in range(4))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


def _candidate_gateways() -> list:
    """可能的路由器地址，按可能性排序。

    光靠系统路由表不够 —— 有时候拿不到，所以再用本机 IP 推一个 /24 网关
    （家用网络绝大多数是 x.y.z.1），最后兜几个常见地址。
    """
    from .discovery import local_ip

    candidates = []

    def add(address: str) -> None:
        if address and address not in candidates and not address.startswith("127."):
            candidates.append(address)

    add(default_gateway() or "")

    ip = local_ip()
    if ip.count(".") == 3 and not ip.startswith("127."):
        add(ip.rsplit(".", 1)[0] + ".1")

    for common in ("192.168.1.1", "192.168.0.1", "192.168.31.1", "10.0.0.1"):
        add(common)

    return candidates


def discover_gateway(timeout: float = 3.0) -> Optional[str]:
    """找到路由器上"设备描述"XML 的 URL。

    ::

        客户端 --M-SEARCH--> 路由器
        路由器 --200 OK----> LOCATION: http://192.168.1.1:49652/gatedesc.xml

    **组播和单播都要发**，这是踩过的坑：很多路由器（实测手上这台就是）
    只在收到**单播** M-SEARCH 时才回应，组播那条石沉大海。只发组播的话
    会得出"路由器不支持 UPnP"的错误结论。

    找不到返回 None。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.6)   # 单播是局域网内，毫秒级就该回；短超时好快速试下一个

    try:
        request = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 2\r\n"
            f"ST: {_SEARCH_TARGETS[0]}\r\n"
            "\r\n"
        ).encode("ascii")

        # 1) 组播（标准做法，多数路由器吃这套）
        try:
            sock.sendto(request, (SSDP_ADDR, SSDP_PORT))
        except OSError as exc:
            log.debug("SSDP 组播发送失败：%s", exc)

        # 2) 单播给每个候选网关（能救回那些不理组播的路由器）
        for gateway in _candidate_gateways():
            try:
                sock.sendto(request, (gateway, SSDP_PORT))
                log.debug("已向 %s 发单播 M-SEARCH", gateway)
            except OSError:
                continue

        # 3) 收响应
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            location = _parse_location(data)
            if location:
                log.debug("找到网关描述地址：%s（来自 %s）", location, addr[0])
                return location
    finally:
        sock.close()
    return None


def _parse_location(data: bytes) -> Optional[str]:
    """从 SSDP 响应里抠出 LOCATION 头。"""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return None
    match = re.search(r"(?im)^LOCATION:\s*(\S+)\s*$", text)
    return match.group(1).strip() if match else None


# ---------------------------------------------------------------- 设备描述


def _fetch(url: str, timeout: float = 5.0) -> Tuple[int, str]:
    """GET 一个 http URL，返回 (状态码, 正文)。"""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", ""):
        raise GatewayError(f"只支持 http，收到 {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise GatewayError(f"地址里没有主机名：{url}")
    port = parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Connection": "close"})
        response = conn.getresponse()
        body = response.read().decode("utf-8", errors="replace")
        return response.status, body
    except OSError as exc:
        raise GatewayError(f"请求 {url} 失败：{exc}") from exc
    finally:
        conn.close()


def _find_wan_service(description_xml: str, base_url: str):
    """从设备描述里找出管端口映射的那个服务。

    返回 (服务类型, 控制地址) 或 None。

    UPnP 的描述 XML 是嵌套的（设备里还有子设备），所以直接遍历所有 service 节点，
    看哪个的类型名里带 WANIPConnection / WANPPPConnection。
    """
    try:
        root = ET.fromstring(description_xml)
    except ET.ParseError as exc:
        raise GatewayError(f"设备描述不是合法 XML：{exc}") from exc

    for service in root.iter():
        if not service.tag.endswith("service"):
            continue
        service_type = ""
        control_url = ""
        for child in service:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "serviceType":
                service_type = (child.text or "").strip()
            elif tag == "controlURL":
                control_url = (child.text or "").strip()
        if not service_type or not control_url:
            continue
        if any(hint in service_type for hint in _WAN_SERVICE_HINTS):
            return service_type, urllib.parse.urljoin(base_url, control_url)
    return None


# ---------------------------------------------------------------- SOAP


def _soap_call(control_url: str, service_type: str, action: str,
               args: str = "", timeout: float = 5.0) -> str:
    """调一次 UPnP 动作，返回响应正文。"""
    parts = urllib.parse.urlsplit(control_url)
    host = parts.hostname
    if not host:
        raise GatewayError(f"控制地址里没有主机名：{control_url}")
    port = parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    body = _SOAP_ENVELOPE.format(action=action, service=service_type, args=args)
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{service_type}#{action}"',
        "Content-Length": str(len(body.encode("utf-8"))),
        "Connection": "close",
    }

    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("POST", path, body=body.encode("utf-8"), headers=headers)
        response = conn.getresponse()
        text = response.read().decode("utf-8", errors="replace")
        if response.status != 200:
            raise GatewayError(_soap_error(text) or f"路由器返回 HTTP {response.status}")
        return text
    except OSError as exc:
        raise GatewayError(f"调用 {action} 失败：{exc}") from exc
    finally:
        conn.close()


def _soap_error(body: str) -> str:
    """把 SOAP 错误正文变成一句人话。UPnP 的错误码很晦涩。"""
    match = re.search(r"<errorCode>\s*(\d+)\s*</errorCode>", body)
    if not match:
        return ""
    code = match.group(1)
    # 对照 UPnP IGD 规范里的错误码。这些码很晦涩，翻译错了会把用户
    # 往完全错误的方向引 —— 比如把"端口被占"说成"路由器不让映射"，
    # 用户就会去翻路由器的安全设置，其实只要换个端口就行。
    reasons = {
        "401": "路由器不支持这个操作",
        "402": "参数不对（可能是端口号或内网地址格式有问题）",
        "501": "路由器执行失败了",
        "606": "路由器没授权这次操作 —— 检查一下 UPnP 开关是不是关着",
        "714": "要删除的端口映射不存在",
        "715": "外部 IP 不允许留空（本机路由器比较少见）",
        "716": "外部端口不允许留空",
        "718": "这个端口已经被映射到别的设备了 —— 换一个端口试试",
        "724": "内外端口必须填一样的",
        "725": "路由器只支持永久映射（租期要设成 0）",
        "726": "「远端主机」这一项必须留空",
        "727": "外部端口必须留空（这种路由器很少见）",
        "728": "路由器的端口映射表满了 —— 删掉几条不用的再试",
        "729": "跟路由器上已有的映射规则冲突了",
    }
    return f"{reasons.get(code, f'路由器返回错误码 {code}')}（错误码 {code}）"


# ---------------------------------------------------------------- 对外接口


def _gateway(timeout: float = 3.0):
    """发现网关并找出它的 WAN 服务。返回 (服务类型, 控制地址, 描述地址)。"""
    location = discover_gateway(timeout=timeout)
    if not location:
        raise GatewayError(
            "没找到支持 UPnP 的路由器。可能是：路由器没开 UPnP、"
            "或者网络环境不允许组播（比如在公司/学校网络里）。"
        )
    status, description = _fetch(location)
    if status != 200:
        raise GatewayError(f"取路由器描述失败：HTTP {status}")
    found = _find_wan_service(description, location)
    if not found:
        raise GatewayError("路由器没有提供端口映射服务（可能不支持 UPnP IGD）")
    service_type, control_url = found
    return service_type, control_url, location


def get_external_ip(timeout: float = 3.0) -> str:
    """问路由器：你的 WAN 口 IP 是多少。

    这是判断有没有公网 IP 最靠谱的办法 —— 不依赖任何外部服务，
    而且 CGNAT 会直接暴露出来：路由器会报回一个 100.64.x.x 之类的地址。
    """
    service_type, control_url, _ = _gateway(timeout=timeout)
    response = _soap_call(control_url, service_type, "GetExternalIPAddress")
    match = re.search(r"<NewExternalIPAddress>\s*([^<\s]+)\s*</NewExternalIPAddress>", response)
    if not match:
        raise GatewayError("路由器没有返回外网 IP")
    return match.group(1)


def add_port_mapping(
    external_port: int,
    internal_port: int,
    internal_client: str,
    *,
    protocol: str = "TCP",
    description: str = "lanlink",
    lease_seconds: int = 0,
    timeout: float = 3.0,
) -> None:
    """让路由器把 external_port 映射到内网的 internal_client:internal_port。

    ``lease_seconds=0`` 表示永久（直到路由器重启或手动删除）。
    """
    service_type, control_url, _ = _gateway(timeout=timeout)
    args = (
        f"<NewRemoteHost></NewRemoteHost>"
        f"<NewExternalPort>{int(external_port)}</NewExternalPort>"
        f"<NewProtocol>{protocol}</NewProtocol>"
        f"<NewInternalPort>{int(internal_port)}</NewInternalPort>"
        f"<NewInternalClient>{internal_client}</NewInternalClient>"
        f"<NewEnabled>1</NewEnabled>"
        f"<NewPortMappingDescription>{description}</NewPortMappingDescription>"
        f"<NewLeaseDuration>{int(lease_seconds)}</NewLeaseDuration>"
    )
    _soap_call(control_url, service_type, "AddPortMapping", args)


def delete_port_mapping(external_port: int, *, protocol: str = "TCP",
                        timeout: float = 3.0) -> None:
    """撤掉之前加的映射。"""
    service_type, control_url, _ = _gateway(timeout=timeout)
    args = (
        f"<NewRemoteHost></NewRemoteHost>"
        f"<NewExternalPort>{int(external_port)}</NewExternalPort>"
        f"<NewProtocol>{protocol}</NewProtocol>"
    )
    _soap_call(control_url, service_type, "DeletePortMapping", args)
