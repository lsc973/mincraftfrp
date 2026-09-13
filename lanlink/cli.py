"""命令行入口。

    python -m lanlink host  --room 我的房间
    python -m lanlink list
    python -m lanlink join  --room 3f2a1b9c
    python -m lanlink relay --port 9000
    python -m lanlink ddns  setup

``host`` 和 ``join`` 之后会进一个简易聊天室：直接打字回车就是广播，
``/w 2 内容`` 是私聊 #2，``/who`` 看有谁，``/quit`` 退出。

``ddns`` 配一个免费动态域名，把本机不断变化的 IPv6 绑到一个固定的短名字上。
开隧道时会自动更新，你只要把域名发给对方，不用每次去翻那四十个字符的地址。
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

from .discovery import DISCOVERY_PORT, Scanner, global_ipv6, scan
from .node import Client, Host, PeerInfo, default_name
from .relay import DEFAULT_RELAY_PORT, RelayServer, list_relay_rooms
from .link import format_addr, parse_addr
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
    """argparse 用的地址解析，实际逻辑在 link.parse_addr。

    这里只负责把 None 变成 argparse 能显示的错误。
    """
    parsed = parse_addr(text)
    if parsed is None:
        raise argparse.ArgumentTypeError(
            "地址要写成 host:端口，比如 192.168.1.10:50001；"
            "IPv6 要加方括号，比如 [240e:354::1]:50001"
        )
    return parsed


def _parse_listen(text: str) -> tuple:
    """监听地址。允许只写端口，默认绑 127.0.0.1。

    只给端口的话默认不对外暴露 —— 隧道客户端一般只给自己机器上的程序连，
    监听 0.0.0.0 等于顺手把服务开放给同网段所有人，不该是默认行为。
    """
    text = text.strip()
    if ":" not in text:
        try:
            return ("127.0.0.1", int(text))
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"要么写端口（如 25565），要么写 地址:端口（如 0.0.0.0:25565），收到 {text!r}"
            )
    return _parse_addr(text)


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


def cmd_tunnel(args) -> int:
    """端口转发隧道：把房间当成一条虚拟网线。

    ``--to`` 那侧是有服务的那台，``--listen`` 那侧是想连过来的那台。
    """
    from . import server as servercfg
    from .tunnel import Tunnel, TunnelError, check_room, share_text

    if bool(args.to) == bool(args.listen):
        print("必须二选一：--to <服务地址>（服务在这边）"
              "或 --listen <端口>（服务在对面）", file=sys.stderr)
        return 2

    is_server = args.to is not None
    nickname = args.name or default_name()

    # 房间名在这些情况下用不到：服务端（房间名就是它自己起的）、
    # 客户端且直接填了对方地址。只有"走中继"和"局域网按名字搜"才必须给。
    needs_room = (not is_server) and not args.addr
    if needs_room and not args.room:
        print("客户端要么用 --room <房间名> 搜局域网，"
              "要么用 --addr <地址> 直接连，要么用 --relay 走中继", file=sys.stderr)
        return 2

    # 打包时编进去的 / 配置文件里设的服务器。服务端和客户端都要用。
    default_server = servercfg.resolve()

    #: 这个服务器是不是"本来就配好、对面也知道"的那一个。
    #: 是的话就不用把地址写给对方 —— 他那边已经有了。
    share_by_room_only = False

    # ---- 先把本地这头准备好，再去连房间 ----
    tunnel = None
    node = None
    try:
        if is_server:
            node = Host(
                args.room or "tunnel",
                name=f"{nickname} #tunnel",
                port=args.port,
                advertise=not args.no_advertise,
                discovery_port=args.discovery_port,
                password=args.password or "",
                max_players=args.max_players,
            ).start()
            print(f"房间已创建：{node.room_name}（房间号 {node.room_id}）")
            print(f"局域网地址：{format_addr(node.address, node.port)}")

            # 该挂哪台中继。命令行上明说的优先；没说的话，看默认服务器是不是
            # 中继 —— **主机也必须挂上去**，否则对方连到中继上，
            # 而主机还在局域网里开着房，两个人永远碰不到面。
            relay_target = None
            relay_token = ""
            if args.relay:
                relay_target = args.relay
                relay_token = args.relay_token or ""
            elif default_server is not None and default_server.is_relay:
                relay_target = (default_server.host, default_server.port)
                relay_token = args.relay_token or default_server.token
                share_by_room_only = True
                print(f"用内置的中继：{default_server.address}")

            relay_arg = None
            room_arg = None
            if relay_target is not None:
                host, port = relay_target
                room_arg = args.room or node.room_id
                node.attach_relay(host, port, room_id=room_arg, token=relay_token)
                relay_arg = format_addr(host, port)
                print(f"已挂中继：{relay_arg}   房间号 {room_arg}")

            # 把"该发给对方什么"整段打出来。IPv6 地址四十个字符，让用户自己
            # 去翻、去念给对面听是不现实的 —— 整段复制粘到微信里发过去即可。
            #
            # 建议对方监听的端口用**服务端口**（25565），不是这边的房间端口。
            # 房间端口是随机分配的，让对方去连个 54003 既没道理也容易看错。
            print()
            listen_hint = args.to[1]
            password = args.password or ""

            if share_by_room_only or (relay_arg is None and default_server is not None):
                # 地址已经在对方的 exe 里（直连或中继都算），所以他只要
                # 房间号 + 口令。房间号就是本机的 room_id。
                print(share_text(listen=listen_hint, password=password,
                                 room=node.room_id, server_default=True))
            elif relay_arg is not None:
                # 命令行上临时指定的中继 —— 对方不知道这台，得把地址告诉他
                print(share_text(listen=listen_hint, password=password,
                                 relay=relay_arg, room=room_arg))
            else:
                v6 = global_ipv6()
                share_host = _publish_ddns(v6)
                if share_host is None and v6 is None:
                    print("注意：没检测到全球 IPv6，下面这个局域网地址出了这个网段就没人连得上。")
                print(share_text(
                    address=format_addr(share_host or v6 or node.address, node.port),
                    listen=listen_hint, password=password,
                ))
        else:
            if args.relay:
                host, port = args.relay
                if not args.room:
                    print("走中继时必须用 --room 指定房间号", file=sys.stderr)
                    return 2
                node = Client.join_via_relay(
                    host, port, args.room, name=f"{nickname} #tunnel",
                    password=args.password or "", token=args.relay_token or "",
                )
            elif args.addr:
                # 直接连指定地址。用于这两种"搜不到"的情况：
                #   1. 装了 Tailscale / ZeroTier 之类的虚拟局域网 —— 填对方虚拟 IP
                #   2. 对方有公网 IP 并做了端口映射 —— 填公网地址
                # 这两种都不需要中继。
                node = Client.connect(args.addr[0], args.addr[1],
                                      name=f"{nickname} #tunnel",
                                      password=args.password or "")
                check_room(node, args.room)
            elif default_server is not None:
                # 地址是编在 exe 里 / 配置里设好的，用户只给了房间号和口令。
                # 两种模式说的话完全不一样：中继要 join_via_relay，
                # 直连才是 Client.connect。搞混了只会得到一个莫名其妙的失败。
                if default_server.is_relay:
                    if not args.room:
                        print(f"内置的服务器是台中继，必须用 --room 指定房间号",
                              file=sys.stderr)
                        return 2
                    token = args.relay_token or default_server.token
                    print(f"正在通过中继 {default_server.address} "
                          f"加入房间「{args.room}」…")
                    node = Client.join_via_relay(
                        default_server.host, default_server.port, args.room,
                        name=f"{nickname} #tunnel",
                        password=args.password or "", token=token)
                    check_room(node, args.room)
                else:
                    print(f"正在连接 {default_server.address} …")
                    node = Client.connect(default_server.host, default_server.port,
                                          name=f"{nickname} #tunnel",
                                          password=args.password or "")
                    check_room(node, args.room)
            else:
                print(f"正在局域网里找房间「{args.room}」…")
                info = _find_room(args.room, args.discovery_port, args.timeout)
                if info is None:
                    print(f"没找到房间「{args.room}」。", file=sys.stderr)
                    print("  服务端起了吗？不在同一网段的话，两条路：", file=sys.stderr)
                    print("    · 装了 Tailscale / ZeroTier 之类的虚拟局域网"
                          " → 用 --addr <对方的虚拟IP>:<端口>", file=sys.stderr)
                    print("    · 有公网 IP 并做了端口映射 → 用 --addr <公网地址>", file=sys.stderr)
                    print("    · 都没有 → 得用中继（--relay）", file=sys.stderr)
                    return 1
                node = Client.connect(info.address, info.port,
                                      name=f"{nickname} #tunnel",
                                      password=args.password or "")
            print(f"已加入房间，我是 #{node.peer_id}")

        # ---- 建隧道 ----
        if is_server:
            target_host, target_port = args.to
            tunnel = Tunnel(node, role="server", target=(target_host, target_port)).start()
            print()
            print("=" * 58)
            print("  隧道已就绪（服务端）")
            print(f"  把流量转发到：{target_host}:{target_port}")
            print("=" * 58)
        else:
            bind_host, bind_port = args.listen
            tunnel = Tunnel(node, role="client", listen=(bind_host, bind_port)).start()
            print()
            print("=" * 58)
            print(f"  隧道已就绪（客户端）")
            print(f"  本机监听：{bind_host}:{bind_port}")
            print(f"  把要连的程序指向这个地址即可")
            print("=" * 58)
    except TunnelError as exc:
        print(f"隧道建立失败：{exc}", file=sys.stderr)
        if node is not None:
            node.close()
        return 1
    except Exception as exc:
        print(f"出错了：{exc}", file=sys.stderr)
        if node is not None:
            node.close()
        return 1

    print("  Ctrl+C 或 SIGTERM 退出")
    print()

    stop = threading.Event()

    def on_signal(signum, _frame):
        print(f"\n收到信号 {signum}，正在关闭…", flush=True)
        stop.set()

    for signame in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass

    last_report = time.monotonic()
    last_up = last_down = 0
    try:
        while not stop.is_set():
            # 跟 relay 一个道理：用 time.sleep 而不是 Event.wait，
            # 后者在 Windows 上要等整个超时才处理信号。
            time.sleep(0.5)
            if stop.is_set():
                break
            now = time.monotonic()
            if now - last_report >= 30.0:
                last_report = now
                stats = tunnel.stats()
                up = stats["up"] - last_up
                down = stats["down"] - last_down
                last_up, last_down = stats["up"], stats["down"]
                if stats["streams"] or up or down:
                    print(
                        f"  [隧道] 活跃流 {stats['streams']}    "
                        f"上行 {up / 1024:.1f} KB/30s  下行 {down / 1024:.1f} KB/30s",
                        flush=True,
                    )
    except KeyboardInterrupt:
        print("\n正在关闭…", flush=True)
    finally:
        tunnel.close()
        node.close()
        total = tunnel.stats()
        print(f"已停止。累计上行 {total['up'] / 1024:.1f} KB / 下行 {total['down'] / 1024:.1f} KB",
              flush=True)
    return 0


def cmd_server(args) -> int:
    """默认服务器地址 —— 也就是"对面要连哪儿"。"""
    from . import server

    action = getattr(args, "server_action", None) or "show"

    if action == "set":
        mode = server.MODE_RELAY if args.relay else server.MODE_DIRECT
        try:
            endpoint = server.save(args.address, mode=mode, token=args.token or "")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"好了，对面现在只要填房间号和口令就能连上"
              f"（走{endpoint.kind} {endpoint.address}）。")
        if endpoint.is_relay:
            print("中继模式下，你自己不需要能被外面连到 —— 两边都主动连出去。")
            if not endpoint.token:
                print("提示：你的中继如果不设口令，谁都能拿来中转。"
                      "建议给中继加个口令，再用 --token 填在这儿。")
        print(f"（存在 {server.config_path()}；"
              f"打包时也可以用 --server 或 --server-relay 直接编进 exe）")
        return 0

    if action == "clear":
        if server.clear():
            print("已删掉配置文件里的服务器地址。")
        else:
            print("配置文件里本来就没设。")
        from_build = server.baked()
        if from_build is not None:
            print(f"注意：打包时编进 exe 的 {from_build.address}"
                  f"（{from_build.kind}）还在，会重新生效。")
        return 0

    print(server.describe())
    return 0


def _publish_ddns(address) -> "str | None":
    """开隧道时顺手把域名指过来。返回域名，没配或者失败都返回 None。

    **失败绝不能挡住隧道启动** —— 域名只是个方便，隧道本身用 IP 照样能用。
    所以这里把异常全吃掉，只打一行提示。
    """
    from . import ddns

    config = ddns.load()
    if config is None or not address:
        return None

    print(f"正在更新域名 {config.hostname} …")
    try:
        ddns.publish(address, config)
    except ddns.DdnsError as exc:
        print(f"域名更新失败：{exc}")
        print("  这次直接用 IP 地址，对方照样连得上。修好之后跑 lanlink ddns update 重试。")
        return None
    except Exception as exc:  # pragma: no cover - 兜底，同样不能挡住隧道
        print(f"域名更新失败：{exc}")
        return None

    if not ddns.verify(config.hostname, address, attempts=3, interval=1.0):
        # 请求成功了但本机 DNS 还没跟上，对面多半已经能解析到。照实说，
        # 别吓得用户以为白配了。
        print(f"（{config.hostname} 的更新已提交，本机 DNS 还没跟上，这个不影响对方）")
    return config.hostname


def cmd_ddns(args) -> int:
    """免费动态域名：用一个短名字代替那一长串 IPv6。"""
    from . import ddns

    action = getattr(args, "ddns_action", None) or "show"

    if action == "clear":
        if ddns.clear():
            print("已删掉免费域名配置。")
        else:
            print("本来就没配过。")
        return 0

    if action == "show":
        return _ddns_show(ddns)

    if action == "setup":
        return _ddns_setup(ddns, args)

    if action == "update":
        config = ddns.load()
        if config is None:
            print("还没配置免费域名。先跑一次：lanlink ddns setup", file=sys.stderr)
            return 2
        address = global_ipv6()
        if not address:
            print("本机没有全球可达的 IPv6 地址，没有可以发布的东西。", file=sys.stderr)
            print("  跑 lanlink doctor 看看网络情况。", file=sys.stderr)
            return 1
        print(f"正在把 {config.hostname} 指向 {address} …")
        try:
            ddns.publish(address, config)
        except ddns.DdnsError as exc:
            print(f"更新失败：{exc}", file=sys.stderr)
            return 1
        if ddns.verify(config.hostname, address):
            print(f"好了。{config.hostname} 现在指向 {address}")
        else:
            # 更新请求本身成功了，只是本机 DNS 还没跟上。别把它说成失败 ——
            # 对面多半已经能解析到了。
            print(f"更新请求已提交，但本机 DNS 还没解析到 {address}。")
            print("  这通常是本机 DNS 缓存还没过期，等一两分钟；不影响对面。")
        return 0

    return 2


def _ddns_show(ddns) -> int:
    config = ddns.load()
    path = ddns.config_path()
    if config is None:
        print("还没配置免费域名。")
        print()
        print("配了之后，你就有个固定的短名字，不用再每次把那一长串 IPv6 发给对方：")
        print("  lanlink ddns setup")
        print()
        print(ddns.describe_setup())
        return 0

    print(f"服务商：{config.label}")
    print(f"域名：  {config.hostname}")
    print(f"令牌：  已保存（{len(config.token)} 个字符，不回显）")
    print(f"配置：  {path}")

    address = global_ipv6()
    print()
    if not address:
        print("本机当前没有全球可达的 IPv6 —— 域名暂时指不过来。")
        return 0
    print(f"本机 IPv6：{address}")
    found = ddns.resolve(config.hostname)
    if found is None:
        print(f"{config.hostname} 解析不到 —— 可能还没更新过，或者刚配好还没生效。")
        print("  跑 lanlink ddns update 更新一下。")
    elif ddns._same_address(found, address):
        print(f"{config.hostname} → {found}（对得上）")
    else:
        print(f"{config.hostname} → {found}（跟本机地址对不上，跑一次 lanlink ddns update）")
    return 0


def _ddns_setup(ddns, args) -> int:
    provider = (getattr(args, "provider", None) or "").strip().lower()
    hostname = (getattr(args, "hostname", None) or "").strip()
    token = (getattr(args, "token", None) or "").strip()

    if not provider:
        print("先选一家免费动态域名服务商：")
        for key, item in ddns.PROVIDERS.items():
            print(f"  {key:<8} {item.label}　例：{item.example}")
        print()
        print(ddns.describe_setup())
        print()
        provider = safe_input(f"服务商（回车默认 {ddns.DEFAULT_PROVIDER}）：").strip()
        provider = provider.lower() or ddns.DEFAULT_PROVIDER
    if provider not in ddns.PROVIDERS:
        print(f"不认识的服务商：{provider}", file=sys.stderr)
        return 2

    if not hostname:
        example = ddns.PROVIDERS[provider].example
        hostname = safe_input(f"你申请到的域名（例：{example}）：").strip()
    if not token:
        token = safe_input("token / 密钥：").strip()

    try:
        path = ddns.save(ddns.DdnsConfig(provider, hostname, token))
    except ddns.DdnsError as exc:
        print(f"配置有问题：{exc}", file=sys.stderr)
        return 2

    print(f"已保存到 {path}")
    print("（这个文件里有你的令牌，等同于域名的写权限，别发给别人）")
    print()

    address = global_ipv6()
    if not address:
        print("本机现在没有全球可达的 IPv6，等你有了再跑 lanlink ddns update。")
        return 0
    print(f"正在把 {hostname} 指向 {address} …")
    try:
        ddns.publish(address, ddns.load())
    except ddns.DdnsError as exc:
        print(f"更新失败：{exc}", file=sys.stderr)
        print("配置已经存下来了，改好之后可以直接跑 lanlink ddns update 重试。", file=sys.stderr)
        return 1

    if ddns.verify(hostname, address):
        print(f"成功。{hostname} 现在指向 {address}")
    else:
        print(f"更新请求已提交，但本机 DNS 还没解析到。等一两分钟再看。")
    print()
    print("以后开隧道的时候会自动更新，你只要把域名发给对方就行：")
    print(f"  {hostname}")
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

    # tunnel
    p = sub.add_parser(
        "tunnel",
        help="端口转发隧道：让不在同一局域网的人连上你本机的服务",
        description=(
            "把房间当成一条虚拟网线，任意 TCP 服务都能穿过去。\n"
            "服务在哪台机器上，就在哪台机器上用 --to。"
        ),
    )
    p.add_argument("--room", help="房间名（局域网模式）/ 中继上的房间号（中继模式）")
    p.add_argument("--addr", type=_parse_addr,
                   help="客户端：直接连这个地址，跳过局域网搜索。"
                        "装了虚拟局域网（Tailscale/ZeroTier）或对方有公网 IP 时用")
    p.add_argument("--to", type=_parse_addr,
                   help="服务端：本地服务的地址，比如 127.0.0.1:25565")
    p.add_argument("--listen", type=_parse_listen, metavar="[地址:]端口",
                   help="客户端：本地监听的地址，比如 127.0.0.1:25565 或 25565")
    p.add_argument("--name", help="你的昵称")
    p.add_argument("--password", help="房间密码")
    p.add_argument("--port", type=int, default=0, help="服务端监听端口，0 = 自动")
    p.add_argument("--no-advertise", action="store_true",
                   help="不在局域网广播房间（只能靠地址直连）")
    p.add_argument("--discovery-port", type=int, default=DISCOVERY_PORT)
    p.add_argument("--timeout", type=float, default=4.0, help="搜索房间的时长（秒）")
    p.add_argument("--max-players", type=int, default=16)
    p.add_argument("--relay", type=_parse_addr, help="走公网中继，格式 host:port")
    p.add_argument("--relay-token", help="中继口令")
    p.set_defaults(func=cmd_tunnel)

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

    # ddns
    p = sub.add_parser(
        "ddns",
        help="配置免费动态域名（用一个短名字代替那一长串 IPv6）",
        description=(
            "把本机不断变化的 IPv6 绑定到一个固定的短域名上。\n"
            "配好之后开隧道会自动更新，你只要把域名发给对方就行，\n"
            "不用再每次去翻 IP，对方也不用记那一长串地址。\n\n"
            "不带子命令时显示当前配置。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ddns_sub = p.add_subparsers(dest="ddns_action")

    d = ddns_sub.add_parser("show", help="看看现在配的是什么（默认）")
    d.set_defaults(func=cmd_ddns)

    d = ddns_sub.add_parser("setup", help="配置域名（不带参数就一步步问）")
    d.add_argument("--provider", choices=["dynv6", "duckdns"], help="服务商")
    d.add_argument("--hostname", help="申请到的域名，如 yourname.dynv6.net")
    d.add_argument("--token", help="服务商给的 token（等同于域名的写权限，注意别泄露）")
    d.set_defaults(func=cmd_ddns)

    d = ddns_sub.add_parser("update", help="立刻把域名指向本机当前的 IPv6")
    d.set_defaults(func=cmd_ddns)

    d = ddns_sub.add_parser("clear", help="删掉配置")
    d.set_defaults(func=cmd_ddns)

    p.set_defaults(func=cmd_ddns, ddns_action="show")

    # server
    p = sub.add_parser(
        "server",
        help="设置默认服务器地址（让对面只填房间号和口令）",
        description=(
            "设好之后，对面开隧道时只要填房间号和口令，不用知道地址。\n\n"
            "想让**对面**也省事，打包时加 --server 把地址直接编进 exe：\n"
            "  python packaging/build.py --mode all --server yourname.dynv6.net:50001\n"
            "把打出来的 exe 发给他，他就什么地址都不用填了。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    server_sub = p.add_subparsers(dest="server_action")

    s = server_sub.add_parser("show", help="看看现在用的是哪个地址（默认）")
    s.set_defaults(func=cmd_server)

    s = server_sub.add_parser(
        "set", help="设置地址（默认是直连；加 --relay 表示这是台中继）",
        description="直连要求你家能被外面连到（公网 IP 或 IPv6）。"
                    "被 CGNAT / 防火墙挡住时用 --relay 指向一台公网机器。",
    )
    s.add_argument("address", help="host:端口，如 yourname.dynv6.net:50001")
    s.add_argument("--relay", action="store_true",
                   help="这是个中继地址（两边都连它，你自己不需要能被外部访问）")
    s.add_argument("--token", help="中继口令，配合 --relay 用")
    s.set_defaults(func=cmd_server)

    s = server_sub.add_parser("clear", help="删掉（打包时编进去的会重新生效）")
    s.set_defaults(func=cmd_server)

    p.set_defaults(func=cmd_server, server_action="show")

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
