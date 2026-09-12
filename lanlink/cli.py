"""命令行入口。

    python -m lanlink host  --room 我的房间
    python -m lanlink list
    python -m lanlink join  --room 3f2a1b9c
    python -m lanlink relay --port 9000

``host`` 和 ``join`` 之后会进一个简易聊天室：直接打字回车就是广播，
``/w 2 内容`` 是私聊 #2，``/who`` 看有谁，``/quit`` 退出。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .discovery import DISCOVERY_PORT, Scanner, scan
from .node import Client, Host, PeerInfo, default_name
from .relay import DEFAULT_RELAY_PORT, RelayServer, list_relay_rooms
from .text import encode_text, safe_input, safe_print


def _setup_logging(verbose: bool, level: Optional[str] = None) -> None:
    """配置日志。

    中继跑在服务器上时日志要进 stderr（systemd 会收进 journald），
    默认给 WARNING 免得交互用的时候刷屏；relay 子命令会自己调高到 INFO。
    """
    if level:
        resolved = level.upper()
    else:
        resolved = "DEBUG" if verbose else "WARNING"
    logging.basicConfig(
        level=resolved,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )


def _parse_addr(text: str) -> tuple:
    if ":" not in text:
        raise argparse.ArgumentTypeError("地址要写成 host:port，比如 1.2.3.4:9000")
    host, _, port = text.rpartition(":")
    try:
        return host, int(port)
    except ValueError:
        raise argparse.ArgumentTypeError(f"端口不是数字：{port!r}")


# ------------------------------------------------------------------ 聊天循环


class _ChatShell:
    """一个极简的交互式聊天室，纯粹为了让人能直观试出框架是通的。"""

    def __init__(self, node, title: str) -> None:
        self.node = node
        self.title = title
        self._stop = threading.Event()

    def banner(self, lines) -> None:
        print("=" * 56)
        print(f"  {self.title}")
        for line in lines:
            print(f"  {line}")
        print("=" * 56)
        print("  直接打字回车 = 广播；/w <id> <内容> = 私聊；/who = 成员；/quit = 退出")
        print("-" * 56)

    def _on_data(self, source: int, data: bytes, is_json: bool) -> None:
        who = "主机" if source == 0 else self._name_of(source)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = f"<{len(data)} 字节二进制>"
        safe_print(f"\r[{who}] {text}")

    def _name_of(self, peer_id: int) -> str:
        info = self.node.peers.get(peer_id)
        return f"{info.name}#{peer_id}" if info else f"#{peer_id}"

    def _on_join(self, info: PeerInfo) -> None:
        safe_print(f"\r* {info} 进来了")

    def _on_leave(self, info: PeerInfo, reason: str) -> None:
        safe_print(f"\r* {info} 离开了（{reason}）")

    def run(self) -> None:
        self.node.on("data", self._on_data)
        self.node.on("peer_join", self._on_join)
        self.node.on("peer_leave", self._on_leave)
        try:
            while not self._stop.is_set():
                try:
                    line = safe_input()
                except EOFError:
                    break
                line = line.strip()
                if not line:
                    continue
                if line in ("/quit", "/exit"):
                    break
                if line == "/who":
                    peers = self.node.peers
                    if not peers:
                        print("  房间里只有你")
                    for info in peers.values():
                        print(f"  {info}")
                    continue
                if line.startswith("/w "):
                    parts = line.split(" ", 2)
                    if len(parts) < 3:
                        print("  用法：/w <id> <内容>")
                        continue
                    try:
                        target = int(parts[1].lstrip("#"))
                    except ValueError:
                        print("  id 得是数字")
                        continue
                    self.node.send_to(target, encode_text(parts[2]))
                    print(f"  -> {self._name_of(target)}: {parts[2]}")
                    continue
                self.node.broadcast(encode_text(line))
        except KeyboardInterrupt:
            print()
        finally:
            self._stop.set()
            self.node.close()


# ------------------------------------------------------------------ 子命令


def cmd_host(args) -> int:
    host = Host(
        args.room,
        name=args.name or default_name(),
        port=args.port,
        discovery_port=args.discovery_port,
        advertise=not args.no_advertise,
        password=args.password or "",
        max_players=args.max_players,
    ).start()

    lines = [
        f"房间名：{host.room_name}",
        f"房间号：{host.room_id}",
        f"局域网地址：{host.address}:{host.port}",
    ]

    if args.relay:
        relay_host, relay_port = args.relay
        try:
            host.attach_relay(
                relay_host,
                relay_port,
                room_id=args.relay_room or host.room_id,
                token=args.relay_token or "",
            )
            lines.append(f"中继地址：{relay_host}:{relay_port} 房间号 {host.relay.room_id}")
        except Exception as exc:
            print(f"挂中继失败：{exc}", file=sys.stderr)
            print("（房间仍在局域网内可用）", file=sys.stderr)

    lines.append(f"本机昵称：{host.name}")

    # 接待客户端由后台线程负责，主线程空出来跑聊天循环
    shell = _ChatShell(host, "lanlink 主机")
    shell.banner(lines)
    try:
        shell.run()
    finally:
        host.close()
    return 0


def cmd_list(args) -> int:
    if args.relay:
        relay_host, relay_port = args.relay
        try:
            rooms = list_relay_rooms(relay_host, relay_port)
        except OSError as exc:
            print(f"连不上中继 {relay_host}:{relay_port} —— {exc}", file=sys.stderr)
            return 1
        print(f"中继 {relay_host}:{relay_port} 上的房间：")
        if not rooms:
            print("  （没有）")
        for room in rooms:
            print(f"  {room}")
        return 0

    print(f"正在扫描局域网（{args.timeout:.1f} 秒）…")
    rooms = scan(timeout=args.timeout, port=args.discovery_port)
    if not rooms:
        print("  没发现房间。")
        print("  排查：主机是否已启动？是否同一网段？Windows 防火墙是否放行？")
        return 0
    for room in rooms:
        print(f"  {room}")
    return 0


def cmd_join(args) -> int:
    password = args.password or ""
    room_id = args.room

    if args.relay:
        relay_host, relay_port = args.relay
        if not room_id:
            print("走中继时必须用 --room 指定房间号", file=sys.stderr)
            return 2
        try:
            client = Client.join_via_relay(
                relay_host,
                relay_port,
                room_id,
                name=args.name or default_name(),
                password=password,
                token=args.relay_token or "",
            )
        except Exception as exc:
            print(f"加入失败：{exc}", file=sys.stderr)
            return 1
    else:
        address: Optional[tuple] = args.addr
        if address is None:
            if not room_id:
                print("要么 --room <房间号>，要么 --addr <host:port>", file=sys.stderr)
                return 2
            print(f"正在局域网里找房间 {room_id} …")
            info = _find_room(room_id, args.discovery_port, args.timeout)
            if info is None:
                print(f"没找到房间 {room_id}", file=sys.stderr)
                return 1
            address = (info.address, info.port)
            print(f"找到了：{info}")
        try:
            client = Client.connect(
                address[0], address[1], name=args.name or default_name(), password=password
            )
        except Exception as exc:
            print(f"加入失败：{exc}", file=sys.stderr)
            return 1

    room = client.room
    lines = [f"你的 id：#{client.peer_id}", f"你的昵称：{client.name}"]
    if room:
        lines.insert(0, f"已加入：{room.room_name}（{room.room_id}）")
        lines.append(f"主机：{room.host_name}")
    shell = _ChatShell(client, "lanlink 客户端")
    shell.banner(lines)
    try:
        shell.run()
    finally:
        client.close()
    return 0


def _find_room(room_id: str, port: int, timeout: float):
    """在局域网里等一个房间出现，找不到就返回 None。"""
    scanner = Scanner(port=port, ttl=4.0)
    scanner.start()
    try:
        deadline = time.monotonic() + max(timeout, 2.0)
        while time.monotonic() < deadline:
            info = scanner.find(room_id)
            if info is not None:
                return info
            time.sleep(0.1)
        return None
    finally:
        scanner.stop()


#: 中继绑定失败时的退出码。
#:
#: 专门给 systemd 用的：端口被占、地址不可用这类是"配置错了"，重试一万次也没用，
#: 所以单元文件里配 RestartPreventExitStatus=2，别让它无限重启刷日志。
#: 其它异常走默认的 1，那种是可能自愈的，让它重试。
EXIT_RELAY_BIND_FAILED = 2


def _load_relay_token(args) -> str:
    """口令的来源，按优先级：命令行 > 文件 > 环境变量。

    命令行传口令会被 ``ps`` 看到，生产环境建议用 ``--token-file`` 或环境变量。
    """
    if args.token:
        return args.token
    if getattr(args, "token_file", None):
        try:
            return Path(args.token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"读不了口令文件 {args.token_file}：{exc}", file=sys.stderr)
            raise SystemExit(EXIT_RELAY_BIND_FAILED)
    return os.environ.get("LANLINK_RELAY_TOKEN", "")


def cmd_relay(args) -> int:
    if getattr(args, "log_level", None):
        logging.getLogger().setLevel(args.log_level.upper())

    token = _load_relay_token(args)

    try:
        server = RelayServer(
            host=args.bind,
            port=args.port,
            token=token,
            max_clients_per_room=args.max_clients,
        ).start()
    except OSError as exc:
        print(f"中继启动失败：{args.bind}:{args.port} 起不来 —— {exc}", file=sys.stderr)
        print("端口是不是被占了？换个 --port，或者先停掉占用的进程。", file=sys.stderr)
        return EXIT_RELAY_BIND_FAILED

    print("=" * 56)
    print("  lanlink 中继服务器")
    print(f"  监听：{server.address}")
    print(f"  口令：{'已设置' if token else '无（任何人可接入）'}")
    print("=" * 56)
    print("  Ctrl+C 或 SIGTERM 退出")

    # systemd 停止/重启服务时发的是 SIGTERM，Docker 的 docker stop 也是。
    # 不接这个信号的话进程会被直接干掉，所有连接粗暴断开。
    stop = threading.Event()

    def on_signal(signum, _frame):
        print(f"\n收到信号 {signum}，正在关闭…", flush=True)
        stop.set()

    # SIGTERM 是 Linux（systemd / docker stop）用的；SIGINT 是 Ctrl+C。
    # SIGBREAK 只在 Windows 上有，但值得一并注册：它是唯一能在 Windows 上
    # 真正触发"优雅退出"的信号（os.kill 在那边是强杀），注册了才测得到这条路径。
    for signame in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass  # 不在主线程之类的场景，忽略

    last_report = time.monotonic()
    try:
        while not stop.is_set():
            # 这里必须用 time.sleep，不能用 stop.wait(30)。
            # Event.wait 底层是锁等待，Windows 上信号要等它整个超时之后才会被
            # 处理 —— 表现就是按了 Ctrl+C 要等半分钟才反应。time.sleep 会被
            # 信号打断，能秒级响应。
            time.sleep(0.5)
            if stop.is_set():
                break
            now = time.monotonic()
            if now - last_report >= 30.0:
                last_report = now
                stats = server.stats()
                if stats["rooms"]:
                    print(
                        f"  [中继] {stats['rooms']} 间房 / {stats['clients']} 个客户端",
                        flush=True,
                    )
    except KeyboardInterrupt:
        print("\n正在关闭…", flush=True)
    finally:
        server.close()
        print("已停止。", flush=True)
    return 0


def cmd_doctor(args) -> int:
    from .doctor import main as doctor_main

    return doctor_main(["--discovery-port", str(args.discovery_port)])


def cmd_gui(args) -> int:
    from .gui import main as gui_main

    return gui_main()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lanlink",
        description="局域网优先的通用联机框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="打开调试日志")
    sub = parser.add_subparsers(dest="command", required=True)

    # host
    p = sub.add_parser("host", help="开一间房")
    p.add_argument("--room", required=True, help="房间名（别人看到的名字）")
    p.add_argument("--name", help="你的昵称")
    p.add_argument("--port", type=int, default=0, help="监听端口，0 = 自动挑一个")
    p.add_argument("--password", help="房间密码")
    p.add_argument("--max-players", type=int, default=16, help="人数上限")
    p.add_argument("--discovery-port", type=int, default=DISCOVERY_PORT, help="发现端口")
    p.add_argument("--no-advertise", action="store_true", help="不广播房间（只能靠地址直连）")
    p.add_argument("--relay", type=_parse_addr, help="挂到公网中继，格式 host:port")
    p.add_argument("--relay-room", help="中继上的房间号，默认跟本机房间号一致")
    p.add_argument("--relay-token", help="中继口令")
    p.set_defaults(func=cmd_host)

    # list
    p = sub.add_parser("list", help="扫描局域网或中继上的房间")
    p.add_argument("--timeout", type=float, default=2.0, help="扫描时长（秒）")
    p.add_argument("--discovery-port", type=int, default=DISCOVERY_PORT)
    p.add_argument("--relay", type=_parse_addr, help="改查中继上的房间")
    p.set_defaults(func=cmd_list)

    # join
    p = sub.add_parser("join", help="加入一间房")
    p.add_argument("--room", help="房间号或房间名（局域网内会自动搜）")
    p.add_argument("--addr", type=_parse_addr, help="直接指定主机地址 host:port")
    p.add_argument("--name", help="你的昵称")
    p.add_argument("--password", help="房间密码")
    p.add_argument("--timeout", type=float, default=4.0, help="搜索房间的时长（秒）")
    p.add_argument("--discovery-port", type=int, default=DISCOVERY_PORT)
    p.add_argument("--relay", type=_parse_addr, help="走公网中继，格式 host:port")
    p.add_argument("--relay-token", help="中继口令")
    p.set_defaults(func=cmd_join)

    # doctor
    p = sub.add_parser("doctor", help="检查本机联机环境（防火墙、网段、端口）")
    p.add_argument("--discovery-port", type=int, default=DISCOVERY_PORT)
    p.set_defaults(func=cmd_doctor)

    # gui
    p = sub.add_parser("gui", help="打开图形界面")
    p.set_defaults(func=cmd_gui)

    # relay
    p = sub.add_parser("relay", help="跑一台公网中继服务器")
    p.add_argument("--bind", default="0.0.0.0", help="绑定地址")
    p.add_argument("--port", type=int, default=DEFAULT_RELAY_PORT, help="监听端口")
    p.add_argument("--token", help="接入口令（注意：会出现在 ps 输出里，生产环境建议用 --token-file）")
    p.add_argument(
        "--token-file", help="从文件读口令，文件内容去掉首尾空白即可。比 --token 安全"
    )
    p.add_argument("--max-clients", type=int, default=64, help="每间房的人数上限")
    p.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="日志级别，默认 INFO（房间上下线、客户端进出都会记到 stderr）",
    )
    p.set_defaults(func=cmd_relay)

    return parser


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv and getattr(sys, "frozen", False):
        # 打包成 exe 之后双击运行是没有参数的。这时候直接开图形界面，
        # 而不是甩一句 usage 错误出去 —— 用户双击就是想要个窗口。
        return cmd_gui(None)

    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose, getattr(args, "log_level", None))
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
