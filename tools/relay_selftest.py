#!/usr/bin/env python3
"""中继服务器自检 —— 在部署的机器上跑一遍，确认它真的能中转数据。

不需要起两个终端，也不需要 Windows 客户端：脚本自己在**本进程内**起一个中继、
一个主机、两个客户端（一个直连、一个走中继），让它们互相收发，然后逐项报告。

    python3 tools/relay_selftest.py
    python3 tools/relay_selftest.py --port 9000     # 顺便验证某个端口能不能绑

只依赖标准库，Python 3.8+ 即可。

注意：这个脚本是"自测"，验证的是中继逻辑本身没问题。跨机器连不上时，
先看防火墙 / 云安全组有没有放行端口 —— 那才是九成的问题所在。
"""

from __future__ import annotations

import argparse
import os
import platform
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanlink import Client, Host, RelayServer  # noqa: E402

OK = "[通过]"
BAD = "[失败]"
WARN = "[注意]"

problems: list = []
warnings: list = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {OK if ok else BAD} {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        problems.append(label)
    return ok


def note(label: str, detail: str = "") -> None:
    print(f"  {WARN} {label}" + (f" —— {detail}" if detail else ""))
    warnings.append(label)


def section(title: str) -> None:
    print()
    print("=" * 64)
    print(f"  {title}")
    print("=" * 64)


def wait_until(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def main() -> int:
    parser = argparse.ArgumentParser(description="中继服务器自检")
    parser.add_argument("--port", type=int, default=0,
                        help="顺便验证这个端口能不能绑（0 = 只做逻辑自检）")
    parser.add_argument("--token", default="", help="设置口令跑一遍，验证鉴权")
    args = parser.parse_args()

    section("0. 环境")
    print(f"  Python {sys.version.split()[0]}    {platform.system()} {platform.machine()}")
    print(f"  lanlink 来自 {Path(__file__).resolve().parent.parent}")

    # 如果指定了端口，先确认它能绑上 —— 这是部署时最常见的坑
    if args.port:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", args.port))
            check(f"端口 {args.port} 可以绑定", True)
        except OSError as exc:
            check(f"端口 {args.port} 可以绑定", False, str(exc))
            print("\n  端口被占了，或者权限不够（1024 以下的端口需要 root）。")
            return 1
        finally:
            probe.close()

    section("1. 启动中继")
    try:
        relay = RelayServer("0.0.0.0", 0, token=args.token).start()
    except OSError as exc:
        check("中继启动", False, str(exc))
        return 1
    check("中继启动", True, f"0.0.0.0:{relay.port}")

    host = None
    direct = None
    remote = None
    try:
        section("2. 主机挂到中继上")
        host = Host("自检房", name="自检主机", host="127.0.0.1", port=0, advertise=False).start()
        host.attach_relay("127.0.0.1", relay.port, room_id="selftest", token=args.token)
        check("主机已挂上中继", host.relay is not None and host.relay.connected,
              f"房间号 selftest")
        check("中继侧看得到这间房", wait_until(lambda: len(relay.rooms) == 1))

        section("3. 局域网直连客户端")
        direct = Client.connect("127.0.0.1", host.port, name="直连客户端")
        check("直连成功", direct.peer_id > 0, f"我是 #{direct.peer_id}")

        section("4. 中继客户端")
        remote = Client.join_via_relay("127.0.0.1", relay.port, "selftest",
                                       name="中继客户端", token=args.token)
        check("经中继加入成功", remote.peer_id > 0, f"我是 #{remote.peer_id}")
        check("中继分配的 id 跟直连的不冲突", remote.peer_id != direct.peer_id,
              f"直连 #{direct.peer_id} / 中继 #{remote.peer_id}")

        section("5. 收发数据")
        check("主机看到两个客户端", wait_until(lambda: host.player_count == 3),
              f"当前 {host.player_count} 人")

        host_got = []
        host.on("data", lambda s, d, j: host_got.append((s, d)))
        direct.send(b"from-direct")
        check("直连 -> 主机", wait_until(lambda: any(d == b"from-direct" for _, d in host_got)))

        host_got.clear()
        remote.send(b"from-relay")
        ok = wait_until(lambda: any(d == b"from-relay" for _, d in host_got))
        check("中继 -> 主机（关键路径）", ok)
        if ok:
            source = next(s for s, d in host_got if d == b"from-relay")
            check("来源标的是中继客户端", source == remote.peer_id,
                  f"标的是 #{source}，应为 #{remote.peer_id}")

        section("6. 中继 -> 客户端（反方向）")
        direct_got = []
        direct.on("data", lambda s, d, j: direct_got.append((s, d)))
        host.send_to(direct.peer_id, b"to-direct")
        check("主机 -> 直连客户端", wait_until(lambda: any(d == b"to-direct" for _, d in direct_got)))

        remote_got = []
        remote.on("data", lambda s, d, j: remote_got.append((s, d)))
        host.send_to(remote.peer_id, b"to-relay")
        ok = wait_until(lambda: any(d == b"to-relay" for _, d in remote_got))
        check("主机 -> 中继客户端（关键路径）", ok)

        section("7. 广播与二进制完整性")
        remote_got.clear()
        direct.broadcast(b"broadcast-test")
        check("直连广播能到中继客户端",
              wait_until(lambda: any(d == b"broadcast-test" for _, d in remote_got)))

        blob = bytes(range(256)) * 400  # 100 KB，256 个字节值全覆盖
        host_got.clear()
        remote.send(blob)
        ok = wait_until(lambda: len(host_got) == 1, timeout=15.0)
        check("100 KB 二进制经中继完整送达",
              ok and host_got[0][1] == blob,
              f"收到 {len(host_got[0][1]) if host_got else 0} 字节")

        section("8. 中继统计")
        stats = relay.stats()
        check("统计里房间数正确", stats["rooms"] == 1, f"{stats['rooms']} 间房")
        check("统计里客户端数正确", stats["clients"] == 1, f"{stats['clients']} 个中继客户端")

        section("9. 断开处理")
        remote.close()
        check("中继客户端离开后主机成员表更新",
              wait_until(lambda: host.player_count == 2), f"当前 {host.player_count} 人")
        check("中继侧房间还在（主机没走）", wait_until(lambda: len(relay.rooms) == 1))

        section("10. 主机断开")
        host.close()
        check("主机关闭后中继清掉房间", wait_until(lambda: len(relay.rooms) == 0))

        # 主机已经关了，finally 里别再关一次
        host = None

    finally:
        for node, name in ((remote, "中继客户端"), (direct, "直连客户端"), (host, "主机")):
            if node is not None:
                try:
                    node.close()
                except Exception:
                    pass
        relay.close()

    section("结果")
    if problems:
        print(f"  {len(problems)} 项没通过：")
        for item in problems:
            print(f"    · {item}")
    else:
        print("  中继自检全部通过 —— 这台机器上跑中继没问题。")
    if warnings:
        print(f"  （{len(warnings)} 项提示）")
    print()
    print("  提醒：自检通过只说明中继逻辑没问题。外面的机器连不上，")
    print("        八成是防火墙或云安全组没放行端口：")
    print(f"          sudo ufw allow {args.port or 9000}/tcp        # Ubuntu/Debian")
    print(f"          sudo firewall-cmd --permanent --add-port={args.port or 9000}/tcp  # CentOS/RHEL")
    print("=" * 64)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
