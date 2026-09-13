"""环境自检。

跨机器联机失败，十次里有九次不是代码问题，而是防火墙、网络类别、
或者两台机器根本不在一个网段。这个模块把这些环境因素逐条查一遍，
并且给出能直接照做的修复建议。

    python -m lanlink doctor
"""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Set

from .discovery import DISCOVERY_PORT, local_ip, scan
from .node import Client, Host

__all__ = ["Check", "run_checks", "render_report", "main"]

IS_WINDOWS = platform.system() == "Windows"


@dataclass
class Check:
    """一项检查的结果。"""

    name: str
    ok: bool
    detail: str
    advice: str = ""
    warn_only: bool = False

    @property
    def mark(self) -> str:
        if self.ok:
            return "[通过]"
        return "[警告]" if self.warn_only else "[失败]"


# ---------------------------------------------------------------- 网络信息


def all_local_ips() -> List[str]:
    """本机所有 IPv4 地址。"""
    ips: List[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for info in infos:
            ip = info[4][0]
            if ip not in ips and not ip.startswith("169.254."):  # 169.254 是没拿到 DHCP 时的自分配地址
                ips.append(ip)
    except (socket.gaierror, OSError):
        pass
    primary = local_ip()
    if primary not in ips:
        ips.insert(0, primary)
    return ips


def check_local_ips() -> Check:
    ips = all_local_ips()
    real = [ip for ip in ips if not ip.startswith("127.")]
    if not real:
        return Check(
            "本机 IP",
            False,
            "只找到回环地址 127.0.0.1",
            "没有可用的局域网地址，检查网线/WiFi 是否连上，或者是否拿到了 DHCP。",
        )
    return Check("本机 IP", True, "、".join(real))


# ---------------------------------------------------------------- 防火墙


def _powershell(script: str, timeout: float = 25.0) -> Optional[str]:
    """跑一段 PowerShell，拿回 stdout。失败返回 None。"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    # PowerShell 在中文 Windows 上默认输出 GBK，解不出来也不致命
    for encoding in ("utf-8", "gbk", "mbcs"):
        try:
            return proc.stdout.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return proc.stdout.decode("utf-8", errors="replace")


def _running_program() -> str:
    """当前进程的可执行文件路径。

    防火墙规则是**按程序**放行的，不是按进程名。``python.exe`` 的规则对打包
    出来的 ``lanlink.exe`` 一点都不生效 —— 只查 python 会让 exe 用户看到
    「防火墙通过」，实际入站却被挡在外面，这正是最难查的那类问题。
    """
    try:
        return sys.executable or ""
    except Exception:  # pragma: no cover - 解释器被拆掉时的兜底
        return ""


def _normalize_program(path: str) -> str:
    """统一的程序路径写法，用来比对。大小写和斜杠方向都可能不一样。"""
    path = (path or "").strip()
    return os.path.normcase(os.path.normpath(path)) if path else ""


def _profiles_from_values(values) -> Set[str]:
    """把 Profile 值解析成名字集合。

    Profile 是位标志枚举（Domain=1, Private=2, Public=4, 全选 0x7FFFFFFF），
    但 PowerShell 序列化成字符串时给的是 ``"Domain, Private"`` 这种逗号写法，
    两种都得认。
    """
    names: Set[str] = set()
    for value in values:
        if isinstance(value, str):
            for part in value.split(","):
                part = part.strip()
                if not part:
                    continue
                if part.lower() == "any":
                    names.update({"Domain", "Private", "Public"})
                else:
                    names.add(part)
            continue
        if not isinstance(value, int):
            continue
        if value == 0 or value >= 2147483647:
            names.update({"Domain", "Private", "Public"})
            continue
        if value & 1:
            names.add("Domain")
        if value & 2:
            names.add("Private")
        if value & 4:
            names.add("Public")
    return names


def _firewall_profiles_for(program: str) -> Optional[Set[str]]:
    """哪些防火墙配置文件放行了这个程序的入站。拿不到就返回 None。

    查法上有个坑：一条一条规则去取 ``Get-NetFirewallApplicationFilter`` 慢到
    不能用（本机几百条规则，三分钟都跑不完）。改成先把 ApplicationFilter
    一次性全捞出来建索引，再跟规则表在内存里对，整个查询三秒出头。
    """
    if not IS_WINDOWS:
        return None

    wanted = _normalize_program(program)
    if not wanted:
        return None

    script = (
        "$af = @{}; "
        "Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | "
        "ForEach-Object { $af[$_.InstanceID] = $_.Program }; "
        "Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True "
        "-ErrorAction SilentlyContinue | ForEach-Object { "
        "$p = $af[$_.InstanceID]; "
        "if ($p) { [PSCustomObject]@{ P = [string]$_.Profile; F = $p } } } | "
        "ConvertTo-Json -Compress"
    )
    raw = _powershell(script)
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if isinstance(data, dict):
        data = [data]
    elif not isinstance(data, list):
        return None

    matched = []
    for item in data:
        if not isinstance(item, dict):
            continue
        target = item.get("F")
        if not isinstance(target, str):
            continue
        # 只认明确点名了这个程序的规则。Program=Any 的要么是端口规则、要么
        # 绑在某个系统服务上，直接算数会造出「看着放行了其实没有」——
        # 而那正是这个检查要防的事。
        if _normalize_program(target) == wanted:
            matched.append(item.get("P"))
    return _profiles_from_values(matched)


#: PowerShell 的 NetworkCategory 枚举。ConvertTo-Json 会把它序列化成数字，
#: 必须自己映射回来 —— 不然 "Public" 会变成 "0"，检查逻辑直接判错。
_NETWORK_CATEGORIES = {
    0: "Public",
    1: "Private",
    2: "DomainAuthenticated",
}

#: 网络类别和防火墙配置文件的叫法不一样（DomainAuthenticated vs Domain），
#: 直接拿类别名去比对放行集合会误报失败。这里做一次归一。
_CATEGORY_TO_PROFILE = {
    "DomainAuthenticated": "Domain",
    "Domain": "Domain",
    "Private": "Private",
    "Public": "Public",
}


def _network_category() -> Optional[str]:
    """当前活动网络的类别：Public / Private / DomainAuthenticated。"""
    if not IS_WINDOWS:
        return None
    raw = _powershell(
        "Get-NetConnectionProfile -ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty NetworkCategory | ConvertTo-Json -Compress"
    )
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None

    raw_values = data if isinstance(data, list) else [data]
    names = []
    for value in raw_values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            names.append(_NETWORK_CATEGORIES.get(value, f"未知({value})"))
        else:
            names.append(str(value))
    if not names:
        return None

    # 可能同时连着多个网络（比如有线 + WiFi）。只要有一个不是 Public，
    # 就按那个来判 —— 因为那才是会挡掉入站连接的情况。
    for preferred in ("Private", "DomainAuthenticated"):
        if preferred in names:
            return preferred
    return names[0]


def _linux_firewall_hint() -> str:
    """Linux 上按装的哪个防火墙工具给出对应命令。"""
    import shutil

    if shutil.which("ufw"):
        return (
            "检测到 ufw。放行中继端口（TCP）和发现端口（UDP）：\n"
            "      sudo ufw allow 9000/tcp\n"
            "      sudo ufw allow 47777/udp\n"
            "    然后 sudo ufw status 确认一下。"
        )
    if shutil.which("firewall-cmd"):
        return (
            "检测到 firewalld。放行端口：\n"
            "      sudo firewall-cmd --permanent --add-port=9000/tcp\n"
            "      sudo firewall-cmd --permanent --add-port=47777/udp\n"
            "      sudo firewall-cmd --reload"
        )
    if shutil.which("nft"):
        return (
            "没找到 ufw/firewalld，看起来是直接管 nftables。确认 INPUT 链放行了端口：\n"
            "      sudo nft list ruleset | grep -E '9000|47777'"
        )
    if shutil.which("iptables"):
        return (
            "没找到 ufw/firewalld，看起来是直接管 iptables。确认放行了端口：\n"
            "      sudo iptables -L INPUT -n | grep -E '9000|47777'"
        )
    return "没检测到防火墙工具。如果连不上，先确认云服务商的安全组放行了对应端口。"


def check_firewall() -> Check:
    if not IS_WINDOWS:
        system = platform.system()
        if system == "Linux":
            return Check(
                "防火墙",
                True,
                "Linux：本机不查（不同发行版差异太大）",
                "跨机器连不上时，九成是防火墙或云安全组。\n" + _linux_firewall_hint(),
                warn_only=True,
            )
        return Check(
            "防火墙",
            True,
            f"非 Windows（{system}），跳过检查",
            "请自行确认防火墙放行了 TCP 和 UDP 的监听端口。",
            warn_only=True,
        )

    program = _running_program()
    name = os.path.basename(program) or "本程序"
    profiles = _firewall_profiles_for(program)
    category = _network_category()
    fix = (
        "以管理员身份打开 PowerShell，执行：\n"
        f"      New-NetFirewallRule -DisplayName 'lanlink' -Direction Inbound "
        f"-Program '{program}' -Action Allow -Profile Any"
    )
    if profiles is None:
        return Check(
            "防火墙",
            True,
            "查询失败（可能没有权限），无法确认",
            f"手动查一下：设置 → 网络和 Internet → Windows 防火墙 → 允许应用通过防火墙，\n"
            f"  确认 {name} 在「专用」和「公用」两栏都打了勾。",
            warn_only=True,
        )

    label = "、".join(sorted(profiles)) if profiles else "无"
    category_text = f"，当前网络类别 {category}" if category else ""
    wanted = _CATEGORY_TO_PROFILE.get(category, category) if category else None

    if not profiles:
        return Check(
            "防火墙",
            False,
            f"没有找到放行 {name} 入站的规则{category_text}",
            f"Windows 防火墙默认挡掉所有入站连接，而规则是绑程序的 —— "
            f"{name} 没被放行，别人就连不进来。\n"
            "首次监听端口时 Windows 一般会弹窗问，点「允许」就行；没弹过或者点了取消的话：\n"
            + fix + "\n"
            "  顺手确认一下：如果网络类别是「公用」，弹窗里只勾「专用」是不够的。",
        )
    if wanted and wanted not in profiles:
        return Check(
            "防火墙",
            False,
            f"{name} 已放行的配置文件：{label}{category_text} —— 当前类别没被覆盖",
            "这是跨机器连不上最常见的原因。\n" + fix + "\n"
            f"  或者手动到「允许应用通过防火墙」里，把 {name} 的「专用」和「公用」都勾上。",
        )
    return Check("防火墙", True, f"{name} 已放行：{label}{category_text}")


# ---------------------------------------------------------------- 端口与自测


def check_discovery_port(port: int = DISCOVERY_PORT) -> Check:
    """发现端口能不能绑上。绑不上说明有别的程序占着。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))
        return Check("发现端口", True, f"UDP {port} 可用")
    except OSError as exc:
        return Check(
            "发现端口",
            False,
            f"UDP {port} 绑定失败：{exc}",
            "可能被别的程序占用了。换一个端口：所有机器都加 --discovery-port <新端口>。",
        )
    finally:
        sock.close()


def check_tcp_bind() -> Check:
    """能不能监听 TCP —— 主机的立身之本。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", 0))
        port = sock.getsockname()[1]
        return Check("TCP 监听", True, f"可以绑定（试了 0.0.0.0:{port}）")
    except OSError as exc:
        return Check("TCP 监听", False, f"绑定失败：{exc}", "检查是否有安全软件拦截了监听操作。")
    finally:
        sock.close()


def check_self_discovery(port: int = DISCOVERY_PORT) -> Check:
    """本机自测：起一间房，再用扫描器把它找出来。"""
    host = None
    try:
        host = Host("自检房间", name="doctor", port=0, discovery_port=port).start()
        rooms = scan(timeout=2.5, port=port)
        if any(r.room_id == host.room_id for r in rooms):
            return Check("房间发现", True, "广播和探测都通（本机自测）")
        return Check(
            "房间发现",
            False,
            "本机都没发现自己的房间",
            "广播被本机防火墙拦了。按上面防火墙那条建议处理。",
        )
    except OSError as exc:
        return Check("房间发现", False, f"自测失败：{exc}", "检查网络配置。")
    finally:
        if host is not None:
            host.close()


def check_round_trip() -> Check:
    """本机自测：起一间房，连上去，发一段数据再收回来。"""
    host = None
    client = None
    try:
        host = Host("自检房间", name="doctor", port=0, advertise=False).start()
        client = Client.connect("127.0.0.1", host.port, name="doctor-client", timeout=5.0)
        received = []
        host.on("data", lambda source, data, is_json: received.append(data))
        payload = b"lanlink-doctor"
        client.send(payload)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not received:
            time.sleep(0.02)
        if received and received[0] == payload:
            return Check("收发回环", True, "连接、发送、接收都正常")
        return Check("收发回环", False, "数据没回来", "本机都收不到，先解决防火墙问题。")
    except Exception as exc:  # noqa: BLE001 - 自检就该把所有异常都变成报告
        return Check("收发回环", False, f"{type(exc).__name__}: {exc}", "见上面的检查项。")
    finally:
        if client is not None:
            client.close()
        if host is not None:
            host.close()


#: 虚拟局域网工具的网卡名关键字 → 显示名。
#: 装了这些工具的话，两台机器就有了一条"第三条路"：
#: 用虚拟 IP 直连，既不用中继也不用公网 IP。
_VPN_ADAPTER_HINTS = {
    "tailscale": "Tailscale",
    "zerotier": "ZeroTier",
    "hamachi": "Hamachi",
    "radmin": "Radmin VPN",
    "wireguard": "WireGuard",
    "openvpn": "OpenVPN",
    "nebula": "Nebula",
}

def _is_cgnat_shared(address: str) -> bool:
    """是不是 100.64.0.0/10（RFC 6598 共享地址段）。

    Tailscale 默认就用这一段。注意是 /10 —— 第二段 64~127 都算，
    只匹配 "100.64." 会漏掉 100.100.x.x 之类的地址。
    """
    parts = address.split(".")
    if len(parts) != 4 or parts[0] != "100":
        return False
    try:
        return 64 <= int(parts[1]) <= 127
    except ValueError:
        return False


def _is_hamachi(address: str) -> bool:
    """Hamachi 用的是 25.0.0.0/8。"""
    return address.startswith("25.") and address.count(".") == 3


#: 按 IP 段兜底识别（网卡名拿不到时用）。
_VPN_IP_RANGES = (
    (_is_cgnat_shared, "Tailscale（100.64.0.0/10 共享地址段）"),
    (_is_hamachi, "Hamachi"),
)


def _virtual_lan_addresses() -> list:
    """找出虚拟局域网（Tailscale / ZeroTier 之类）的地址。

    返回 [(地址, 工具名), ...]。拿不到就返回空列表 —— 这只是锦上添花的信息，
    查不到不该影响自检结果。
    """
    found = []

    if IS_WINDOWS:
        raw = _powershell(
            "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
            "Select-Object IPAddress,InterfaceAlias | ConvertTo-Json -Compress"
        )
        if raw and raw.strip():
            try:
                items = json.loads(raw)
            except ValueError:
                items = None
            if isinstance(items, dict):
                items = [items]
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                alias = str(item.get("InterfaceAlias", ""))
                address = str(item.get("IPAddress", ""))
                if not address:
                    continue
                for keyword, label in _VPN_ADAPTER_HINTS.items():
                    if keyword in alias.lower():
                        found.append((address, label))
                        break

    known = {addr for addr, _ in found}
    for address in all_local_ips():
        if address in known:
            continue
        for matches, label in _VPN_IP_RANGES:
            if matches(address):
                found.append((address, label))
                break

    return found


def check_interfaces() -> Check:
    """把本机所有非回环地址列出来，方便跟对面机器比对网段。"""
    ips = all_local_ips()
    real = [ip for ip in ips if not ip.startswith("127.")]
    vpn = _virtual_lan_addresses()

    if vpn:
        detail = "、".join(f"{addr}（{label}）" for addr, label in vpn)
        return Check(
            "虚拟局域网",
            True,
            f"发现虚拟局域网地址：{detail}",
            "这是跨网段联机最省事的办法 —— 两台机器装同一个工具并登录，\n"
            "然后用「端口转发隧道」的「对方地址」直接填这个虚拟 IP，\n"
            "既不需要中继，也不需要公网 IP / 端口映射。\n"
            "（注意：对方也得装同一个工具，并且登录到同一个网络里。）",
            warn_only=True,
        )

    if len(real) > 1:
        return Check(
            "多网卡",
            True,
            f"有多个地址：{'、'.join(real)}",
            "如果两台机器连的是不同网段，广播到不了。确认两边在同一个网段。",
            warn_only=True,
        )
    return Check("多网卡", True, "只有一个局域网地址" if real else "没有局域网地址")


# ---------------------------------------------------------------- 汇总


def check_ipv6() -> Check:
    """有没有全球可达的 IPv6。

    **这条比 IPv4 那条更值得看**：国内运营商大范围上了 IPv6，而且 IPv6
    通常**不做 NAT** —— 每台设备拿到的就是全球可路由的地址。所以哪怕宽带
    在运营商大内网里（IPv4 没有公网地址），只要两边都有 IPv6，照样能直连，
    不需要中继、对方也不用装任何东西。
    """
    from .discovery import global_ipv6

    address = global_ipv6()
    if not address:
        return Check(
            "IPv6",
            True,
            "没有全球可达的 IPv6 地址",
            "如果 IPv4 那边也查出来在大内网，那「不用中继」这条路就走不通了，\n"
            "只能选：申请公网 IP / 两边装虚拟局域网 / 用中继。",
            warn_only=True,
        )

    return Check(
        "IPv6",
        True,
        f"有全球可达的 IPv6：{address}",
        "好消息：IPv6 一般不做 NAT，外面能直接连到这台机器。\n"
        "  用法：把隧道服务端跑起来，让对方在「对方地址」里填\n"
        f"        [{address}]:端口\n"
        "  对方只要有 IPv6 就行（国内运营商基本都铺了），不用装任何东西。\n"
        "  注意：如果连不上，先看路由器有没有开 IPv6 防火墙、\n"
        f"        Windows 防火墙有没有放行 {os.path.basename(_running_program()) or '本程序'}。",
        warn_only=True,
    )


def check_public_address() -> Check:
    """本机能不能被外网直接连上。

    这一项决定了"不用中继、对面也不装东西"这条路走不走得通：

    * 有公网 IP → 在路由器上做个端口映射就行，对面只跑 lanlink 就能直连
    * 运营商大内网（CGNAT）→ 外网根本路由不到你，端口映射也没用

    判断办法是问自家路由器"你的 WAN 口 IP 是多少"（UPnP），
    不依赖任何外部服务。路由器不开 UPnP 的话就问不到，那就退回让用户自己看。
    """
    from . import upnp
    from .discovery import global_ipv6

    try:
        wan = upnp.get_external_ip()
    except upnp.GatewayError as exc:
        return Check(
            "公网可达",
            True,
            f"查不到（{exc}）",
            "手动确认一下：登录路由器后台（一般是 192.168.1.1），看「WAN 口 / 外网 IP」。\n"
            "  · 100.64.x.x / 10.x / 172.x / 192.168.x → 运营商大内网或多层 NAT，\n"
            "    外网连不进来，只能靠中继或虚拟局域网\n"
            "  · 公网地址（比如 113.x.x.x、1.2.3.4）→ 可以做端口映射，\n"
            "    对面只跑 lanlink.exe 就能直连，不需要中继、也不用装别的东西",
            warn_only=True,
        )

    if upnp.is_cgnat(wan):
        v6 = global_ipv6()
        if v6:
            # 有全球 IPv6 就别急着判死刑 —— IPv4 这条路确实断了，但
            # 「对面什么都不装」在 IPv6 上是走得通的，只差路由器放不放行。
            return Check(
                "公网可达",
                True,
                f"IPv4 是运营商大内网（WAN 口 {wan}），但本机有全球 IPv6",
                "IPv4 这条路不通：没有公网 IP，外面路由不到你，端口映射也救不了。\n"
                f"  改走 IPv6 —— 地址是 {v6}，把隧道服务端跑起来，让对方在\n"
                f"  「对方地址」里填 [{v6}]:端口。对方只要有 IPv6 就行，不用装任何东西。\n"
                "  唯一没把握的是路由器/光猫的 IPv6 防火墙放不放行入站，这个只能实测。\n"
                "  万一不通：打运营商客服申请公网 IP / 两边装 Tailscale / 找台公网机器跑中继。",
                warn_only=True,
            )
        return Check(
            "公网可达",
            False,
            f"路由器 WAN 口是 {wan} —— 运营商大内网（CGNAT）",
            "你的宽带没有公网 IP，外面的人路由不到你，端口映射也救不了。三条出路：\n"
            "  · 打运营商客服申请公网 IP（电信/联通有时能给，移动基本不给）\n"
            "  · 两边都装 Tailscale 之类的虚拟局域网，然后直连\n"
            "  · 找台有公网 IP 的机器跑中继\n"
            "「对面只跑 lanlink.exe、什么都不装」这个要求，在 CGNAT 下做不到。",
        )

    if not upnp.is_public_address(wan):
        return Check(
            "公网可达",
            False,
            f"路由器 WAN 口是 {wan} —— 内网地址，说明上面还挂着一层路由",
            "多层 NAT，外网同样连不进来。把上层那台设备也做端口映射，"
            "或者改成光猫桥接 + 路由器拨号，才能拿到公网 IP。",
        )

    return Check(
        "公网可达",
        True,
        f"路由器 WAN 口是 {wan} —— 是公网地址",
        "好消息：你有公网 IP。\n"
        "在路由器上把某个端口映射到本机（比如外部 50001 → 本机 50001），\n"
        "然后让对面用「端口转发隧道」的「对方地址」填 <你的公网IP>:50001。\n"
        "对面只跑 lanlink.exe 就行，不需要中继、也不用装别的东西。",
    )


def run_checks(discovery_port: int = DISCOVERY_PORT) -> List[Check]:
    return [
        check_local_ips(),
        check_interfaces(),
        check_ipv6(),
        check_public_address(),
        check_firewall(),
        check_discovery_port(discovery_port),
        check_tcp_bind(),
        check_self_discovery(discovery_port),
        check_round_trip(),
    ]


def render_report(checks: List[Check], discovery_port: int = DISCOVERY_PORT) -> str:
    lines = [
        "=" * 62,
        "  lanlink 环境自检",
        "=" * 62,
    ]
    for check in checks:
        lines.append(f"{check.mark} {check.name}：{check.detail}")
        # 建议一律打出来，不只是失败的时候。
        # "查不到"（warn_only 且 ok）这类最需要指引 —— 比如 UPnP 问不到路由器时，
        # 用户正需要知道怎么手动去看 WAN 口 IP，藏起来就等于没说。
        if check.advice:
            for advice_line in check.advice.splitlines():
                # 不要 strip —— 建议里的缩进是有意义的（子条目、命令示例），
                # 抹掉之后层级就看不出来了
                lines.append(f"        {advice_line}".rstrip())

    failed = [c for c in checks if not c.ok and not c.warn_only]
    warned = [c for c in checks if not c.ok and c.warn_only]

    lines.append("=" * 62)
    if not failed:
        lines.append("  本机环境没问题。")
        lines.append("")
        lines.append("  如果对面机器还是连不上，在那台机器上也跑一次 lanlink doctor。")
        lines.append("  跨机器失败几乎都是对面机器的防火墙或网络类别问题，而不是本机。")
    else:
        lines.append(f"  有 {len(failed)} 项没通过，按上面的建议处理：")
        for check in failed:
            lines.append(f"    · {check.name}")
    if warned:
        lines.append(f"  （另外 {len(warned)} 项无法确认，不影响使用但值得留意）")
    lines.append("=" * 62)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="lanlink doctor", description="检查本机的联机环境是否就绪"
    )
    parser.add_argument("--discovery-port", type=int, default=DISCOVERY_PORT)
    args = parser.parse_args(argv)

    checks = run_checks(args.discovery_port)
    try:
        print(render_report(checks, args.discovery_port))
    except UnicodeEncodeError:  # 老终端编码兜底
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
        print(render_report(checks, args.discovery_port))
    return 0 if all(c.ok or c.warn_only for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
