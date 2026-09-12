"""环境自检。

跨机器联机失败，十次里有九次不是代码问题，而是防火墙、网络类别、
或者两台机器根本不在一个网段。这个模块把这些环境因素逐条查一遍，
并且给出能直接照做的修复建议。

    python -m lanlink doctor
"""

from __future__ import annotations

import json
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


def _firewall_profiles_for_python() -> Optional[Set[str]]:
    """哪些防火墙配置文件放行了 python.exe 的入站。拿不到就返回 None。"""
    if not IS_WINDOWS:
        return None
    script = (
        "Get-NetFirewallRule -ErrorAction SilentlyContinue | "
        "Where-Object { $_.DisplayName -like '*python*' -and $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' -and $_.Enabled -eq 'True' } | "
        "Select-Object -ExpandProperty Profile | ConvertTo-Json -Compress"
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
    if isinstance(data, int):
        data = [data]
    elif not isinstance(data, list):
        return None

    # Profile 是位标志枚举：Domain=1, Private=2, Public=4, All=0(实际输出 2147483647 之类)
    names: Set[str] = set()
    for value in data:
        if isinstance(value, str):
            names.add(value)
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

    profiles = _firewall_profiles_for_python()
    category = _network_category()
    if profiles is None:
        return Check(
            "防火墙",
            True,
            "查询失败（可能没有权限），无法确认",
            "手动查一下：设置 → 网络和 Internet → Windows 防火墙 → 允许应用通过防火墙，"
            "确认 Python 在“专用”和“公用”两栏都打了勾。",
            warn_only=True,
        )

    label = "、".join(sorted(profiles)) if profiles else "无"
    category_text = f"，当前网络类别 {category}" if category else ""
    wanted = _CATEGORY_TO_PROFILE.get(category, category) if category else None

    if wanted and wanted not in profiles:
        return Check(
            "防火墙",
            False,
            f"Python 已放行的配置文件：{label}{category_text} —— 当前类别没被覆盖",
            "这是跨机器连不上最常见的原因。以管理员身份运行 PowerShell，执行：\n"
            "      New-NetFirewallRule -DisplayName 'lanlink' -Direction Inbound "
            "-Program (Get-Command python).Source -Action Allow -Profile Any\n"
            "    或者手动到「允许应用通过防火墙」里，把 Python 的“专用”和“公用”都勾上。",
        )
    if not profiles:
        return Check(
            "防火墙",
            False,
            f"没有找到任何 Python 的入站放行规则{category_text}",
            "首次监听端口时 Windows 通常会弹窗询问，点“允许”即可。"
            "没弹过或点了取消的话，按上面的命令手动加一条。",
        )
    return Check("防火墙", True, f"Python 已放行：{label}{category_text}")


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


def check_interfaces() -> Check:
    """把本机所有非回环地址列出来，方便跟对面机器比对网段。"""
    ips = all_local_ips()
    real = [ip for ip in ips if not ip.startswith("127.")]
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


def run_checks(discovery_port: int = DISCOVERY_PORT) -> List[Check]:
    return [
        check_local_ips(),
        check_interfaces(),
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
        if check.advice and not check.ok:
            for advice_line in check.advice.splitlines():
                lines.append(f"        {advice_line.strip()}")

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
