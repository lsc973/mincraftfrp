"""TCP 端口转发隧道 —— 让任意服务穿过房间。

解决的问题：Minecraft 服务开在你家 25565，朋友在外地。你家没有公网 IP，
直接连不上；中继虽然能当中转站，但它只搬 lanlink 自己的协议帧，不认识
Minecraft 的协议。

这个模块在**房间的数据通道之上**再铺一层流复用：一条 lanlink 连接里可以
同时跑多条虚拟 TCP 连接，每条有独立的开关。于是::

    # 你这边（服务在 25565）
    lanlink tunnel --room 我的世界 --to 127.0.0.1:25565 --relay 1.2.3.4:9000

    # 朋友那边
    lanlink tunnel --room 我的世界 --listen 25565 --relay 1.2.3.4:9000

朋友在 Minecraft 里连 ``127.0.0.1:25565`` 就等于连到了你家的 25565。

**实现上没有改核心协议**：隧道帧就是普通的房间应用数据，前面加 5 字节
魔数区分：:

    +--------+--------------+--------+
    | "LLTK" | stream_id(4) | op(1)  | data...
    +--------+--------------+--------+

好处是心跳、断线检测、中继转发、二进制完整性这些全都自动复用现成的，
不用碰 protocol.py / link.py / relay.py 一行代码。

**已知限制**：所有流共用一条 TCP 连接，某条流把发送缓冲撑满时会短暂影响
其他流（发送是带锁的 sendall）。同时跑几十条大流量流才会明显，Minecraft
这种一两条连接的场景完全够用。
"""

from __future__ import annotations

import itertools
import logging
import socket
import struct
import threading
from typing import Dict, Optional

from .link import bind_reusable
from .node import Host, Node

__all__ = [
    "Tunnel",
    "TunnelError",
    "MAGIC",
    "OP_OPEN",
    "OP_DATA",
    "OP_CLOSE",
    "friend_command",
    "share_fields",
    "share_text",
    "check_room",
]


# ---------------------------------------------------------------- 连接信息


def friend_command(*, address=None, listen: int, password: str = "",
                   relay=None, room=None, server_default: bool = False) -> str:
    """对方该跑的那条命令。

    三种连法，看 ``address`` / ``relay`` / ``server_default`` 哪个给了：

    * ``relay`` —— 走中继，对方要填中继地址和房间号
    * ``server_default`` —— 地址已经在对方的 exe 里了，**只要房间号**
    * ``address`` —— 最老实的直连，对方要填完整地址
    """
    parts = ["lanlink-cli.exe", "tunnel"]
    if relay is not None:
        parts += ["--relay", relay, "--room", str(room)]
    elif server_default:
        parts += ["--room", str(room)]
    else:
        parts += ["--addr", str(address)]
    if password:
        # 带空格的密码不加引号会被 shell 切成两段，对面怎么都连不上
        parts += ["--password", f'"{password}"' if " " in password else password]
    parts += ["--listen", str(listen)]
    return " ".join(parts)


def share_fields(*, address=None, listen: int, password: str = "",
                 relay=None, room=None, server_default: bool = False) -> list:
    """对方要在界面上填哪几项，返回 ``[(标签, 值)]``。

    图形界面拿它往日志里打，``share_text`` 拿它拼整段话 —— 抽出来是为了
    这两处不会各写一份然后慢慢走样。
    """
    if relay is not None:
        fields = [("中继地址", str(relay)), ("房间名", str(room))]
    elif server_default:
        fields = [("房间号", str(room))]
    else:
        fields = [("对方地址", str(address))]
    fields.append(("本地监听", str(listen)))
    if password:
        fields.append(("房间密码", password))
    return fields


def check_room(node, wanted: str) -> None:
    """确认连上的就是用户想进的那间房，不是就抛 :class:`TunnelError`。

    直连（地址 + 口令）本身**不校验房间号**：对面那台机器上就一间房，连上
    就是它。但用户既然填了房间号，就该按填的来 —— 填错了要当场说清楚，
    而不是让他莫名其妙进了别的房间、还以为连对了。

    能校验是因为握手时主机会把房间信息（``welcome`` 里的 ``room``）发给
    客户端，所以房间号不是个摆设。

    填错房间号会抛异常，**调用方负责关掉连接**。
    """
    wanted = (wanted or "").strip()
    if not wanted:
        return
    room = getattr(node, "room", None)
    if room is None:  # 对面没报房间信息（老版本？），没依据就不拦
        return

    known = {str(getattr(room, "room_id", "") or "").strip().lower(),
             str(getattr(room, "room_name", "") or "").strip().lower()}
    if wanted.lower() in known:
        return

    actual_id = getattr(room, "room_id", "?")
    actual_name = getattr(room, "room_name", "?")
    raise TunnelError(
        f"房间号不对：你填的是「{wanted}」，对面那间房是「{actual_id}」"
        f"（名字「{actual_name}」）。\n"
        f"  跟开房的人核对一下房间号。"
    )


def share_text(*, address=None, listen: int, password: str = "",
               relay=None, room=None, server_default: bool = False) -> str:
    """给对方的一整段话：复制走、粘到微信里发过去就行。

    为什么要整段给：IPv6 地址是 ``240e:354:311:a200:f587:f2f5:4fb3:bae2``
    这种四十个字符的东西，让对面照着念或者手抄根本不现实。对方整段粘过去
    照着做，就完全不用碰那个地址。

    图形界面和命令行版都从这里取文案，免得各写一份然后慢慢走样。
    """
    command = friend_command(address=address, listen=listen, password=password,
                             relay=relay, room=room, server_default=server_default)
    fields = share_fields(address=address, listen=listen, password=password,
                          relay=relay, room=room, server_default=server_default)

    if server_default:
        intro = "lanlink 隧道已开好。对方只要填房间号和口令就能连，不用知道地址："
    else:
        intro = "lanlink 隧道已开好，照着做就能连上："

    lines = [
        intro,
        "",
        "【命令行版】把下面这一行整个复制到终端里回车：",
        f"  {command}",
        "",
        "【图形版】打开 lanlink.exe → 端口转发隧道 → 选「服务在对面」，填：",
    ]
    lines += [f"  {label}：{value}" for label, value in fields]
    lines += [
        "",
        f"然后游戏（或别的程序）里连 127.0.0.1:{listen}",
    ]
    return "\n".join(lines)

#: 服务端在房间里的 peer_id。lanlink 里主机恒为 0，而 --to 那侧固定当主机。
SERVER_PEER_ID = 0

log = logging.getLogger("lanlink.tunnel")

#: 隧道帧的魔数。房间里的应用数据五花八门，靠这个把隧道帧认出来。
MAGIC = b"LLTK"

OP_OPEN = 0x01   # 请求打开一条流
OP_DATA = 0x02   # 流上的数据
OP_CLOSE = 0x03  # 关闭流，data 部分可以带一段 UTF-8 的关闭原因

_HEADER = struct.Struct("!4sIB")

#: 单帧最多搬多少字节。远小于协议的 8 MB 上限，免得一条大流把整条连接的
#: 发送缓冲长时间占住。
CHUNK_SIZE = 32 * 1024

#: 连本地服务时的超时。
DIAL_TIMEOUT = 8.0


class TunnelError(Exception):
    """隧道建立失败。"""


class _Stream:
    """一条虚拟连接：一端是本地 socket，另一端是房间里的某个成员。"""

    __slots__ = ("stream_id", "peer_id", "sock", "tunnel", "closed", "_lock")

    def __init__(self, stream_id: int, peer_id: int, sock: socket.socket, tunnel: "Tunnel"):
        self.stream_id = stream_id
        self.peer_id = peer_id
        self.sock = sock
        self.tunnel = tunnel
        self.closed = False
        self._lock = threading.Lock()

    # ---------------------------------------------------------- 发

    def send_frame(self, op: int, data: bytes = b"") -> bool:
        return self.tunnel._send(self.peer_id, self.stream_id, op, data)

    def feed(self, data: bytes) -> None:
        """收到远端数据，写进本地 socket。"""
        if self.closed or not data:
            return
        try:
            self.sock.sendall(data)
        except OSError as exc:
            log.debug("流 #%s 写本地 socket 失败：%s", self.stream_id, exc)
            self.shutdown("本地连接已断开")

    def shutdown(self, reason: str = "") -> None:
        """双向关掉，并通知对端。"""
        with self._lock:
            if self.closed:
                return
            self.closed = True
        self.send_frame(OP_CLOSE, reason.encode("utf-8")[:200])
        self._close_socket()
        self.tunnel._forget(self.stream_id)

    def close_quietly(self) -> None:
        """只关本地，不通知对端（对端已经先关了的时候用）。"""
        with self._lock:
            if self.closed:
                return
            self.closed = True
        self._close_socket()
        self.tunnel._forget(self.stream_id)

    def _close_socket(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    # ---------------------------------------------------------- 收

    def pump(self) -> None:
        """把本地 socket 的数据搬给远端。跑在自己的线程里。"""
        try:
            while not self.closed:
                chunk = self.sock.recv(CHUNK_SIZE)
                if not chunk:
                    break
                if not self.send_frame(OP_DATA, chunk):
                    break
        except OSError as exc:
            log.debug("流 #%s 读本地 socket 失败：%s", self.stream_id, exc)
        finally:
            # 本地这边读完了（对端关闭了连接），告诉远端可以收了
            self.shutdown("本地连接已关闭")

    def __repr__(self) -> str:
        state = "已关闭" if self.closed else "活跃"
        return f"<流 #{self.stream_id} ↔ #{self.peer_id} {state}>"


class Tunnel:
    """隧道的一端。

    两种角色：

    * ``role="server"`` —— 服务在这边（``--to``）。收到 OPEN 就去连本地服务。
    * ``role="client"`` —— 服务在对面（``--listen``）。本地监听，每条进来的
      连接开一条流。

    两个角色共用同一套收发逻辑，区别只在 OPEN 由谁发起、谁去 dial。

    拓扑是固定的：``server`` 那侧是 lanlink 主机（peer_id 0），``client``
    那侧是房间成员。流只在"成员 ↔ 主机"之间走，不做成员对成员。
    """

    def __init__(
        self,
        node: Node,
        *,
        role: str,
        target: Optional[tuple] = None,
        listen: Optional[tuple] = None,
    ) -> None:
        if role not in ("server", "client"):
            raise ValueError(f"role 只能是 server 或 client，收到 {role!r}")
        if role == "server" and target is None:
            raise ValueError("server 角色必须给 target")
        if role == "client" and listen is None:
            raise ValueError("client 角色必须给 listen")

        self.node = node
        self.role = role
        self.target = target
        self.listen_addr = listen

        self._streams: Dict[int, _Stream] = {}
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._closed = False
        self._listener: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None

        self.bytes_up = 0    # 从本地流向远端
        self.bytes_down = 0  # 从远端流向本地
        self._stats_lock = threading.Lock()

        node.on("data", self._on_data)
        node.on("close", self._on_node_close)

    # ------------------------------------------------------------ 启停

    def start(self) -> "Tunnel":
        if self.role == "client":
            self._start_listener()
        return self

    def _start_listener(self) -> None:
        assert self.listen_addr is not None
        host, port = self.listen_addr
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bind_reusable(listener)
        try:
            listener.bind((host, port))
            listener.listen(16)
        except OSError as exc:
            listener.close()
            raise TunnelError(f"监听 {host}:{port} 失败：{exc}") from exc
        self._listener = listener
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="lanlink-tunnel-accept", daemon=True
        )
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        while not self._closed and self._listener is not None:
            try:
                conn, addr = self._listener.accept()
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            stream = self._open(SERVER_PEER_ID, conn)
            log.info("新连接 %s:%s -> 流 #%s", addr[0], addr[1], stream.stream_id)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        with self._lock:
            streams = list(self._streams.values())
            self._streams.clear()
        for stream in streams:
            stream.shutdown("隧道关闭")

    def _on_node_close(self) -> None:
        self.close()

    # ------------------------------------------------------------ 流的登记

    def _forget(self, stream_id: int) -> None:
        with self._lock:
            self._streams.pop(stream_id, None)

    def _open(self, peer_id: int, sock: socket.socket) -> _Stream:
        stream_id = next(self._ids)
        stream = _Stream(stream_id, peer_id, sock, self)
        with self._lock:
            self._streams[stream_id] = stream
        stream.send_frame(OP_OPEN)
        threading.Thread(
            target=stream.pump, name=f"lanlink-tunnel-{stream_id}", daemon=True
        ).start()
        return stream

    # ------------------------------------------------------------ 收

    def _on_data(self, source: int, data: bytes, is_json: bool) -> None:
        if is_json or len(data) < _HEADER.size or not data.startswith(MAGIC):
            return  # 不是隧道帧，是房间里的普通消息，不理
        _, stream_id, op = _HEADER.unpack_from(data, 0)
        payload = data[_HEADER.size:]

        if op == OP_OPEN:
            self._handle_open(source, stream_id)
        elif op == OP_DATA:
            with self._stats_lock:
                self.bytes_down += len(payload)
            with self._lock:
                stream = self._streams.get(stream_id)
            # stream 是 None 说明对端开了我们没接住（正常竞态），丢掉就是
            if stream is not None and stream.peer_id == source:
                stream.feed(payload)
        elif op == OP_CLOSE:
            with self._lock:
                stream = self._streams.get(stream_id)
            if stream is not None and stream.peer_id == source:
                if payload:
                    log.info("流 #%s 被对端关闭：%s", stream_id,
                             payload.decode("utf-8", errors="replace"))
                stream.close_quietly()

    def _handle_open(self, peer_id: int, stream_id: int) -> None:
        if self.role != "server":
            # 只有 --to 那边接受流。收到说明对面配反了。
            log.warning("收到流 #%s 的打开请求，但本端是 --listen 角色，拒绝", stream_id)
            self._send(peer_id, stream_id, OP_CLOSE, b"this side does not accept streams")
            return

        assert self.target is not None
        host, port = self.target
        try:
            sock = socket.create_connection((host, port), timeout=DIAL_TIMEOUT)
        except OSError as exc:
            log.warning("连不上本地服务 %s:%s —— %s", host, port, exc)
            self._send(peer_id, stream_id, OP_CLOSE,
                       f"无法连接 {host}:{port}（{exc}）".encode("utf-8")[:200])
            return

        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        stream = _Stream(stream_id, peer_id, sock, self)
        with self._lock:
            self._streams[stream_id] = stream
        threading.Thread(
            target=stream.pump, name=f"lanlink-tunnel-{stream_id}", daemon=True
        ).start()
        log.info("流 #%s 已连到本地服务 %s:%s", stream_id, host, port)

    # ------------------------------------------------------------ 发

    def _send(self, peer_id: int, stream_id: int, op: int, data: bytes = b"") -> bool:
        payload = _HEADER.pack(MAGIC, stream_id, op) + data
        if op == OP_DATA:
            with self._stats_lock:
                self.bytes_up += len(data)

        node = self.node
        # 拓扑是固定的：--to 那侧是交换机（Host），--listen 那侧是客户端。
        # 所以主机给成员发走 send_to(peer_id)，客户端永远发给主机（peer_id 0）。
        if isinstance(node, Host):
            return node.send_to(peer_id, payload)
        return node.send(payload)  # type: ignore[union-attr]

    # ------------------------------------------------------------ 状态

    @property
    def streams(self) -> int:
        with self._lock:
            return len(self._streams)

    def stats(self) -> dict:
        with self._stats_lock:
            return {
                "streams": self.streams,
                "up": self.bytes_up,
                "down": self.bytes_down,
                "role": self.role,
            }

    def __repr__(self) -> str:
        return f"<Tunnel {self.role} 流={self.streams}>"
