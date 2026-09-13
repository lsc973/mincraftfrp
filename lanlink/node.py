"""主机与客户端。

一个 ``Host`` 就是一间房：它监听 TCP、广播自己的存在、维护成员表，
并且是所有消息的转发中枢。一个 ``Client`` 是房间里的一名成员：
它只跟主机保持一条连接，靠主机跟别人说话。

两种接入方式对使用方是透明的：

* **直连** —— 客户端在同一个局域网里，直接连主机的 TCP 端口。
* **中继** —— 客户端在别的网段，连公网中继；中继把它跟主机对接起来。
  这时候主机看到的是一个"虚拟 peer"，接口跟直连 peer 完全一样。

事件（用 ``node.on(事件名, 回调)`` 订阅）::

    "peer_join"   (PeerInfo)                          有人进来了
    "peer_leave"  (PeerInfo, 原因)                     有人走了
    "data"        (来源 peer_id, 数据 bytes, 是否 JSON) 收到应用数据
    "close"       ()                                   节点已关闭
    "error"       (异常)                               内部出错（不影响运行）
"""

from __future__ import annotations

import hmac
import itertools
import json
import logging
import os
import platform
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .discovery import DISCOVERY_PORT, Beacon, RoomInfo, local_ip
from .link import Link, bind_reusable, connect
from .protocol import (
    KIND_APP,
    KIND_JSON,
    KIND_RELAY_CTRL,
    KIND_RELAY_DATA,
    ROUTE_BCAST,
    ROUTE_DELIVER,
    ROUTE_DIRECT,
    ROUTE_PEER,
    ProtocolError,
    pack_app,
    pack_relay_body,
    split_relay_data,
    unpack_app,
    unpack_json,
)

__all__ = [
    "PROTOCOL_VERSION",
    "RELAY_ID_BASE",
    "PeerInfo",
    "Node",
    "Host",
    "Client",
    "default_name",
]

log = logging.getLogger("lanlink")

#: 应用层握手协议版本。两边对不上就直接拒绝，免得后面报一堆莫名其妙的错。
PROTOCOL_VERSION = 1

#: 中继分配的 peer_id 从这个数开始，跟主机自己分配的 1、2、3… 天然错开。
RELAY_ID_BASE = 1_000_000

#: 握手阶段最多等多久（秒）。
HANDSHAKE_TIMEOUT = 10.0


#: 连不上时给出的排查方向。裸的 socket 异常只有一个 "timed out"，
#: 用户既不知道是哪一步失败，也不知道该去查什么。
_HINT_RELAY = (
    "排查方向：中继地址/端口是否写对、中继服务是否在运行、"
    "服务器防火墙和云安全组是否放行了这个端口。"
)
_HINT_HOST = (
    "排查方向：主机地址/端口是否写对、主机是否已经开房、"
    "两台机器是否在同一网段、主机防火墙是否放行。"
)


def _dial(host: str, port: int, timeout: float, what: str, hint: str):
    """建立一条 outbound 连接；失败时把上下文说清楚。

    直接往上抛 socket.timeout 的话，界面上只会显示 "timeout: timed out"，
    用户完全无从下手 —— 这个函数就是来解决这个的。
    """
    try:
        return connect(host, port, timeout=timeout)
    except socket.timeout:
        raise ConnectionError(
            f"连接{what} {host}:{port} 超时（等了 {timeout:.0f} 秒还没连上）。{hint}"
        ) from None
    except ConnectionRefusedError:
        raise ConnectionRefusedError(
            f"{what} {host}:{port} 拒绝连接 —— 地址是通的，但那个端口上没有服务在监听。{hint}"
        ) from None
    except OSError as exc:
        raise ConnectionError(f"连接{what} {host}:{port} 失败：{exc}。{hint}") from None


#: 绑定 "0.0.0.0" 时是否尽量用双栈（一个 socket 同时收 IPv4 和 IPv6）。
#: 关掉可以强制只监听 IPv4 —— 排查问题时有用。
DUAL_STACK = True


def _make_listener(bind_host: str, port: int) -> socket.socket:
    """建一个监听 socket，能用 IPv6 就用双栈。

    **为什么要双栈**：很多宽带在运营商大内网（CGNAT）后面，IPv4 根本没有
    公网地址，但 IPv6 是有的 —— 而且 IPv6 通常不做 NAT，外面能直接连进来。
    只监听 IPv4 的话，这条路就白白浪费了。

    双栈的关键是 ``IPV6_V6ONLY=0``：设上之后一个绑在 ``::`` 上的 socket
    能同时接受 IPv4 和 IPv6 连接。设不上（少数系统不支持）就退回纯 IPv4，
    免得出现"绑了 :: 结果 IPv4 客户端全连不上"这种更难查的问题。
    """
    wants_any = bind_host in ("0.0.0.0", "", "::")

    if DUAL_STACK and wants_any and socket.has_ipv6:
        try:
            dual = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            bind_reusable(dual)
            dual.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            dual.bind(("::", port))
            return dual
        except OSError as exc:
            log.debug("双栈监听不可用（%s），退回 IPv4", exc)
            try:
                dual.close()
            except OSError:
                pass

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    bind_reusable(server)
    server.bind((bind_host if bind_host != "::" else "0.0.0.0", port))
    return server


def default_name() -> str:
    """给个能一眼认出是谁的默认昵称。"""
    who = platform.node() or "未知设备"
    return f"{who}-{os.getpid() % 1000:03d}"


@dataclass
class PeerInfo:
    """房间里的一名成员。"""

    peer_id: int
    name: str
    addr: str = ""
    is_relay: bool = False
    joined_at: float = field(default_factory=time.time)

    @property
    def via(self) -> str:
        return "中继" if self.is_relay else "局域网"

    def to_dict(self) -> Dict[str, Any]:
        return {"peer_id": self.peer_id, "name": self.name, "via": self.via}

    def __str__(self) -> str:
        return f"#{self.peer_id} {self.name}（{self.via}）"


# ====================================================================== 基类


class Node:
    """主机和客户端的共同部分：事件订阅 + 成员表。"""

    def __init__(self, name: Optional[str] = None) -> None:
        self.name = name or default_name()
        self._handlers: Dict[str, List[Callable]] = {}
        self._peers: Dict[int, PeerInfo] = {}
        self._closed = False
        self._state_lock = threading.RLock()

    # ------------------------------------------------------------ 事件

    def on(self, event: str, handler: Optional[Callable] = None):
        """订阅事件。也可以当装饰器用::

            @node.on("data")
            def _(source, data, is_json):
                ...
        """
        if handler is None:
            def decorator(fn: Callable) -> Callable:
                self.on(event, fn)
                return fn
            return decorator
        with self._state_lock:
            self._handlers.setdefault(event, []).append(handler)
        return handler

    def off(self, event: str, handler: Callable) -> None:
        with self._state_lock:
            handlers = self._handlers.get(event)
            if handlers and handler in handlers:
                handlers.remove(handler)

    def _emit(self, event: str, *args) -> None:
        with self._state_lock:
            handlers = list(self._handlers.get(event, ()))
        for handler in handlers:
            try:
                handler(*args)
            except Exception as exc:
                log.exception("事件 %s 的回调出错", event)
                if event != "error":
                    self._emit("error", exc)

    # ------------------------------------------------------------ 成员

    @property
    def peers(self) -> Dict[int, PeerInfo]:
        """当前房间成员快照（含主机自己，主机是 #0）。"""
        with self._state_lock:
            return dict(self._peers)

    @property
    def player_count(self) -> int:
        """房间里一共几个人 —— 包括自己。"""
        with self._state_lock:
            return len(self._peers) + 1

    @property
    def closed(self) -> bool:
        return self._closed

    def _put_peer(self, info: PeerInfo) -> None:
        with self._state_lock:
            self._peers[info.peer_id] = info

    def _drop_peer(self, peer_id: int) -> Optional[PeerInfo]:
        with self._state_lock:
            return self._peers.pop(peer_id, None)

    def __enter__(self) -> "Node":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        raise NotImplementedError

    def __repr__(self) -> str:
        state = "已关闭" if self._closed else f"{self.player_count} 人"
        return f"<{type(self).__name__} {self.name} {state}>"


# ====================================================================== 主机


class _Peer:
    """主机视角下的一个成员：可能挂在直连 socket 上，也可能挂在中继上。"""

    __slots__ = ("info", "link", "attachment")

    def __init__(self, info: PeerInfo, link: Optional[Link] = None, attachment=None) -> None:
        self.info = info
        self.link = link
        self.attachment = attachment

    def send_frame(self, kind: int, payload: bytes) -> bool:
        if self.link is not None:
            return self.link.send_frame(kind, payload)
        if self.attachment is not None:
            return self.attachment.send_to_peer(self.info.peer_id, kind, payload)
        return False

    def close(self, reason: str) -> None:
        if self.link is not None:
            self.link.close(reason)


class Host(Node):
    """一间房。

    ::

        host = Host("我的房间", port=0)
        host.start()
        print(host.room_id, host.port)

        @host.on("data")
        def _(source, data, is_json):
            print(source, data)

        host.broadcast(b"hello")
    """

    def __init__(
        self,
        room_name: str,
        *,
        name: Optional[str] = None,
        host: str = "0.0.0.0",
        port: int = 0,
        discovery_port: int = DISCOVERY_PORT,
        advertise: bool = True,
        password: str = "",
        max_players: int = 16,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(name)
        self.room_name = room_name
        self.room_id = secrets.token_hex(4)
        self.password = password
        self.max_players = max_players
        self.extra = dict(extra or {})
        self.bind_host = host
        self.port = port
        self.address = local_ip()
        self.discovery_port = discovery_port
        self.advertise = advertise

        self._server: Optional[socket.socket] = None
        self._beacon: Optional[Beacon] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._ids = itertools.count(1)
        self._members: Dict[int, _Peer] = {}
        self._attachment: Optional["RelayAttachment"] = None
        self._started = False

    # ------------------------------------------------------------ 启动

    def start(self) -> "Host":
        """绑定端口、开始监听和广播。返回 self，方便链式调用。"""
        if self._started:
            return self
        server = _make_listener(self.bind_host, self.port)
        server.listen(32)
        self._server = server
        self.port = server.getsockname()[1]
        self._started = True

        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="lanlink-accept", daemon=True
        )
        self._accept_thread.start()

        if self.advertise:
            self._beacon = Beacon(self._beacon_payload, port=self.discovery_port)
            self._beacon.start()
        log.info("房间 %s 已开在 %s:%s", self.room_name, self.address, self.port)
        return self

    def serve_forever(self) -> None:
        """阻塞住，直到 ``close()`` 被调用。"""
        if not self._started:
            self.start()
        while not self._closed:
            time.sleep(0.2)

    # ------------------------------------------------------------ 房间信息

    def room_info(self, **overrides: Any) -> RoomInfo:
        info = RoomInfo(
            room_id=self.room_id,
            room_name=self.room_name,
            host_name=self.name,
            address=self.address,
            port=self.port,
            players=self.player_count,
            max_players=self.max_players,
            has_password=bool(self.password),
            relay_room=self._attachment.room_id if self._attachment else "",
            relay_addr=self._attachment.public_addr if self._attachment else "",
            extra=dict(self.extra),
        )
        for key, value in overrides.items():
            setattr(info, key, value)
        return info

    def _beacon_payload(self) -> Dict[str, Any]:
        return self.room_info().to_dict()

    def announce(self) -> None:
        """立刻重播一次房间广播（房间信息变了想马上让人看到时用）。"""
        if self._beacon is not None:
            self._beacon.announce()

    # ------------------------------------------------------------ 接入

    def _accept_loop(self) -> None:
        while not self._closed and self._server is not None:
            try:
                sock, addr = self._server.accept()
            except OSError:
                return
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            link = Link(sock, addr, name=f"pending-{addr[0]}", on_frame=self._on_pending_frame)
            # 握手超时保护：连上却不说话的连接不该一直占着名额
            timer = threading.Timer(HANDSHAKE_TIMEOUT, self._handshake_expired, args=(link,))
            timer.daemon = True
            link._handshake_timer = timer  # type: ignore[attr-defined]
            timer.start()
            link.start()

    @staticmethod
    def _handshake_expired(link: Link) -> None:
        if link.alive and link.name.startswith("pending-"):
            link.close("握手超时")

    def _on_pending_frame(self, link: Link, kind: int, payload: bytes) -> None:
        if kind != KIND_JSON:
            link.close("握手阶段只接受 JSON 消息")
            return
        try:
            msg = unpack_json(payload)
        except ProtocolError:
            link.close("握手消息无法解析")
            return
        if not isinstance(msg, dict) or msg.get("t") != "hello":
            link.close("期望 hello 消息")
            return
        self._admit_direct(link, msg)

    def _admit_direct(self, link: Link, msg: Dict[str, Any]) -> None:
        version = msg.get("v", 0)
        if version != PROTOCOL_VERSION:
            self._reject(link, "version", f"协议版本不匹配（对方 {version}，本机 {PROTOCOL_VERSION}）")
            return
        if not self._password_ok(msg.get("password", "")):
            self._reject(link, "password", "房间密码不正确")
            return
        if self.player_count >= self.max_players:
            self._reject(link, "full", "房间人数已满")
            return

        peer_id = next(self._ids)
        name = str(msg.get("name") or f"玩家{peer_id}")[:32]
        info = PeerInfo(peer_id=peer_id, name=name, addr=link.peer_addr, is_relay=False)
        peer = _Peer(info, link=link)

        # 换掉握手用的回调，之后这条链路走正常业务
        link.name = f"peer-{peer_id}"
        link.on_frame = self._make_peer_handler(peer_id)
        link.on_close = self._make_peer_close_handler(peer_id)
        timer = getattr(link, "_handshake_timer", None)
        if timer is not None:
            timer.cancel()

        self._members[peer_id] = peer
        self._put_peer(info)
        link.send_json(self._welcome_payload(peer_id))
        self._announce_join(info)
        log.info("%s 加入了房间", info)

    def _password_ok(self, given: Any) -> bool:
        if not self.password:
            return True
        # 必须比字节：compare_digest 碰到非 ASCII 的 str 会直接抛 TypeError
        return hmac.compare_digest(
            str(given).encode("utf-8"), self.password.encode("utf-8")
        )

    def _reject(self, link: Link, reason: str, message: str) -> None:
        link.send_json({"t": "reject", "reason": reason, "message": message})
        link.close(f"拒绝接入：{message}")

    def _welcome_payload(self, peer_id: int) -> Dict[str, Any]:
        others = [p.to_dict() for p in self.peers.values() if p.peer_id != peer_id]
        return {
            "t": "welcome",
            "v": PROTOCOL_VERSION,
            "peer_id": peer_id,
            "you": self.name,
            "room": self.room_info().to_dict(),
            "peers": others,
        }

    def _announce_join(self, info: PeerInfo) -> None:
        payload = {"t": "peer_join", "peer": info.to_dict()}
        self._fanout(payload, exclude=info.peer_id)
        self.announce()  # 人数变了，广播里同步一下
        self._emit("peer_join", info)

    def _announce_leave(self, info: PeerInfo, reason: str) -> None:
        self._fanout({"t": "peer_leave", "peer_id": info.peer_id, "reason": reason})
        self.announce()
        self._emit("peer_leave", info, reason)

    def _fanout(self, message: Dict[str, Any], exclude: Optional[int] = None) -> None:
        blob_payload = _json_body(message)
        for peer_id, peer in list(self._members.items()):
            if peer_id == exclude:
                continue
            peer.send_frame(KIND_JSON, blob_payload)

    # ------------------------------------------------------------ 成员收发

    def _make_peer_handler(self, peer_id: int):
        def handler(link: Link, kind: int, payload: bytes) -> None:
            self._on_peer_frame(peer_id, kind, payload)
        return handler

    def _make_peer_close_handler(self, peer_id: int):
        def handler(link: Link, reason: str) -> None:
            self._remove_member(peer_id, reason)
        return handler

    def _on_peer_frame(self, peer_id: int, kind: int, payload: bytes) -> None:
        if kind != KIND_APP:
            return  # 成员发来的 JSON 控制帧一律忽略，房间管理是主机的事
        try:
            route, target, is_json, data = unpack_app(payload)
        except ProtocolError:
            return

        if route == ROUTE_DIRECT:
            self._emit("data", peer_id, data, is_json)
        elif route == ROUTE_PEER:
            self.send_to(target, data, is_json=is_json, source=peer_id)
        elif route == ROUTE_BCAST:
            # 主机也是房间里的一员，广播它同样收得到
            self._emit("data", peer_id, data, is_json)
            self.broadcast(data, is_json=is_json, source=peer_id, exclude=peer_id)
        # ROUTE_DELIVER 是主机发给成员的方向，成员不该发过来

    def _remove_member(self, peer_id: int, reason: str) -> None:
        peer = self._members.pop(peer_id, None)
        if peer is None:
            return
        info = self._drop_peer(peer_id)
        if info is not None:
            log.info("%s 离开了房间：%s", info, reason)
            self._announce_leave(info, reason)

    # ------------------------------------------------------------ 发送

    def send_to(
        self,
        peer_id: int,
        data: bytes,
        *,
        is_json: bool = False,
        source: int = 0,
    ) -> bool:
        """给某个成员发数据。``source=0`` 表示这条是主机本人发的。"""
        peer = self._members.get(peer_id)
        if peer is None:
            return False
        payload = pack_app(ROUTE_DELIVER, source, data, is_json=is_json)
        return peer.send_frame(KIND_APP, payload)

    def send_json_to(self, peer_id: int, obj: Any, *, source: int = 0) -> bool:
        return self.send_to(peer_id, _json_body(obj), is_json=True, source=source)

    def broadcast(
        self,
        data: bytes,
        *,
        is_json: bool = False,
        source: int = 0,
        exclude: Optional[int] = None,
    ) -> int:
        """给房间里所有人发数据，返回成功发出的份数。"""
        payload = pack_app(ROUTE_DELIVER, source, data, is_json=is_json)
        sent = 0
        for peer_id, peer in list(self._members.items()):
            if peer_id == exclude:
                continue
            if peer.send_frame(KIND_APP, payload):
                sent += 1
        return sent

    def broadcast_json(self, obj: Any, *, source: int = 0, exclude: Optional[int] = None) -> int:
        return self.broadcast(_json_body(obj), is_json=True, source=source, exclude=exclude)

    def kick(self, peer_id: int, reason: str = "被主机请出房间") -> None:
        """把某人请出去。"""
        peer = self._members.get(peer_id)
        if peer is None:
            return
        if peer.link is not None:
            peer.link.send_json({"t": "kick", "reason": reason})
            peer.link.close(reason)
        elif self._attachment is not None:
            self._attachment.reject_peer(peer_id, reason)
            self._remove_member(peer_id, reason)

    # ------------------------------------------------------------ 中继

    def attach_relay(
        self,
        relay_host: str,
        relay_port: int,
        *,
        room_id: Optional[str] = None,
        token: str = "",
        name: Optional[str] = None,
        timeout: float = 10.0,
    ) -> "RelayAttachment":
        """把房间挂到公网中继上，让跨网段的玩家也能进来。

        中继上的房间号默认跟本机的 ``room_id`` 一致。
        """
        attachment = RelayAttachment(
            self,
            relay_host,
            relay_port,
            room_id=room_id or self.room_id,
            token=token,
            name=name or self.room_name,
            timeout=timeout,
        )
        attachment.connect()
        if self._attachment is not None:
            self._attachment.close()
        self._attachment = attachment
        self.announce()
        return attachment

    @property
    def relay(self) -> Optional["RelayAttachment"]:
        return self._attachment

    def _admit_relay_peer(self, peer_id: int, name: str, password: str, addr: str) -> None:
        """中继那边有人要进来，主机这边决定收不收。"""
        if not self._password_ok(password):
            self._attachment.reject_peer(peer_id, "房间密码不正确")  # type: ignore[union-attr]
            return
        if self.player_count >= self.max_players:
            self._attachment.reject_peer(peer_id, "房间人数已满")  # type: ignore[union-attr]
            return
        if peer_id in self._members:
            return

        info = PeerInfo(
            peer_id=peer_id,
            name=(name or f"玩家{peer_id}")[:32],
            addr=addr or "relay",
            is_relay=True,
        )
        self._members[peer_id] = _Peer(info, attachment=self._attachment)
        self._put_peer(info)
        # 中继链路是共享的，没法给这个虚拟成员单独发，直接推给它
        self._attachment.send_to_peer(peer_id, KIND_JSON, _json_body(self._welcome_payload(peer_id)))  # type: ignore[union-attr]
        self._announce_join(info)

    def _on_relay_frame(self, link: Link, kind: int, payload: bytes) -> None:
        if kind == KIND_RELAY_DATA:
            # 中继替某个虚拟成员搬来的帧，拆开当成普通成员帧处理
            try:
                peer_id, inner_kind, inner_payload = split_relay_data(payload)
            except ProtocolError:
                return
            return self._on_peer_frame(peer_id, inner_kind, inner_payload)

        if kind != KIND_RELAY_CTRL:
            return
        try:
            msg = unpack_json(payload)
        except ProtocolError:
            return
        if not isinstance(msg, dict):
            return
        action = msg.get("t")
        if action == "join":
            self._admit_relay_peer(
                int(msg.get("peer_id", 0)),
                str(msg.get("name", "")),
                str(msg.get("password", "")),
                str(msg.get("addr", "relay")),
            )
        elif action == "leave":
            self._remove_member(int(msg.get("peer_id", 0)), "中继连接断开")

    # ------------------------------------------------------------ 关闭

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._beacon is not None:
            self._beacon.stop()
            self._beacon = None
        for peer in list(self._members.values()):
            peer.close("主机关闭了房间")
        self._members.clear()
        with self._state_lock:
            self._peers.clear()
        if self._attachment is not None:
            self._attachment.close()
            self._attachment = None
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        self._emit("close")


# ====================================================================== 客户端


class Client(Node):
    """房间成员。

    ::

        client = Client.connect("192.168.1.10", 50001, name="小明")
        client.send(b"hi")
        client.broadcast(b"大家好")
    """

    def __init__(self, name: Optional[str] = None) -> None:
        super().__init__(name)
        self._link: Optional[Link] = None
        self.peer_id: int = 0
        self.room: Optional[RoomInfo] = None
        self._ready = threading.Event()
        self._failure: Optional[str] = None

    # ------------------------------------------------------------ 连接

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        *,
        name: Optional[str] = None,
        password: str = "",
        timeout: float = 8.0,
        link: Optional[Link] = None,
    ) -> "Client":
        """直连一台主机。"""
        client = cls(name)
        client._link = link or _dial(host, port, timeout, "主机", _HINT_HOST)
        client._link.name = "host"
        client._link.on_frame = client._on_frame
        client._link.on_close = client._on_link_close
        client._link.start()
        client._link.send_json(
            {"t": "hello", "v": PROTOCOL_VERSION, "name": client.name, "password": password}
        )
        client._await_welcome(timeout)
        return client

    @classmethod
    def join_via_relay(
        cls,
        relay_host: str,
        relay_port: int,
        room_id: str,
        *,
        name: Optional[str] = None,
        password: str = "",
        token: str = "",
        timeout: float = 10.0,
    ) -> "Client":
        """通过公网中继加入一间房。"""
        link = _dial(relay_host, relay_port, timeout, "中继", _HINT_RELAY)
        link.name = "relay"
        client = cls(name)
        client._link = link
        link.on_frame = client._on_frame
        link.on_close = client._on_link_close
        link.start()
        link.send_json({
            "t": "auth",
            "role": "client",
            "room": room_id,
            "name": client.name,
            "password": password,
            "token": token,
            "v": PROTOCOL_VERSION,
        })
        # 中继先回 auth_ok，主机随后才发 welcome；两个都要等到
        client._await_welcome(timeout)
        return client

    def _await_welcome(self, timeout: float) -> None:
        if not self._ready.wait(timeout):
            self.close()
            raise TimeoutError(self._failure or "等待主机响应超时")
        if self._failure:
            reason = self._failure
            self.close()
            raise ConnectionRefusedError(reason)

    # ------------------------------------------------------------ 收

    def _on_frame(self, link: Link, kind: int, payload: bytes) -> None:
        if kind == KIND_JSON:
            self._on_control(payload)
        elif kind == KIND_APP:
            self._on_app(payload)

    def _on_control(self, payload: bytes) -> None:
        try:
            msg = unpack_json(payload)
        except ProtocolError:
            return
        if not isinstance(msg, dict):
            return
        action = msg.get("t")

        if action == "welcome":
            self._handle_welcome(msg)
        elif action == "peer_join":
            peer = msg.get("peer") or {}
            info = PeerInfo(
                peer_id=int(peer.get("peer_id", 0)),
                name=str(peer.get("name", "?")),
                is_relay=str(peer.get("via", "")) == "中继",
            )
            self._put_peer(info)
            self._emit("peer_join", info)
        elif action == "peer_leave":
            peer_id = int(msg.get("peer_id", 0))
            info = self._drop_peer(peer_id)
            if info is not None:
                self._emit("peer_leave", info, str(msg.get("reason", "")))
        elif action in ("reject", "kick", "error"):
            self._failure = str(msg.get("message") or msg.get("reason") or "被主机拒绝")
            self._ready.set()
            self._emit("error", ConnectionRefusedError(self._failure))
            self.close()

    def _handle_welcome(self, msg: Dict[str, Any]) -> None:
        self.peer_id = int(msg.get("peer_id", 0))
        room = msg.get("room") or {}
        try:
            self.room = RoomInfo.from_dict(room)
        except TypeError:
            self.room = None
        for peer in msg.get("peers") or []:
            pid = int(peer.get("peer_id", 0))
            if pid:
                self._put_peer(PeerInfo(
                    peer_id=pid,
                    name=str(peer.get("name", "?")),
                    is_relay=str(peer.get("via", "")) == "中继",
                ))
        self._ready.set()

    def _on_app(self, payload: bytes) -> None:
        try:
            route, source, is_json, data = unpack_app(payload)
        except ProtocolError:
            return
        if route == ROUTE_DELIVER:
            self._emit("data", source, data, is_json)
        elif route == ROUTE_DIRECT:
            # 主机自己发来的定向消息，来源就是主机
            self._emit("data", 0, data, is_json)

    def _on_link_close(self, link: Link, reason: str) -> None:
        self._failure = self._failure or reason
        self._ready.set()
        if not self._closed:
            self._closed = True
            self._emit("close")

    # ------------------------------------------------------------ 发

    def send(self, data: bytes, *, is_json: bool = False) -> bool:
        """发给主机本人。"""
        return self._send_route(ROUTE_DIRECT, 0, data, is_json)

    def send_to(self, peer_id: int, data: bytes, *, is_json: bool = False) -> bool:
        """请主机转发给指定成员。

        目标不在成员表里就直接返回 False —— 与其丢进黑洞，不如当场说清楚。
        ``peer_id=0`` 表示主机本人，总是允许。
        """
        if peer_id != 0 and peer_id not in self.peers:
            return False
        return self._send_route(ROUTE_PEER, peer_id, data, is_json)

    def broadcast(self, data: bytes, *, is_json: bool = False) -> bool:
        """请主机转发给房间里其他所有人。"""
        return self._send_route(ROUTE_BCAST, 0, data, is_json)

    def _send_route(self, route: int, target: int, data: bytes, is_json: bool) -> bool:
        if self._link is None or not self._link.alive:
            return False
        return self._link.send_frame(KIND_APP, pack_app(route, target, data, is_json=is_json))

    def send_json(self, obj: Any) -> bool:
        return self.send(_json_body(obj), is_json=True)

    def send_json_to(self, peer_id: int, obj: Any) -> bool:
        return self.send_to(peer_id, _json_body(obj), is_json=True)

    def broadcast_json(self, obj: Any) -> bool:
        return self.broadcast(_json_body(obj), is_json=True)

    # ------------------------------------------------------------ 关闭

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._link is not None:
            self._link.close("客户端主动离开")
        self._ready.set()
        self._emit("close")


# ====================================================================== 中继挂载


class RelayAttachment:
    """主机侧挂在一条中继连接上。

    每个通过中继进来的玩家都是主机眼里的一个虚拟 peer，收发走
    ``KIND_RELAY_DATA`` 转交给中继，由中继分发给对应的人。
    """

    def __init__(
        self,
        host_node: Host,
        relay_host: str,
        relay_port: int,
        *,
        room_id: str,
        token: str = "",
        name: str = "",
        timeout: float = 10.0,
    ) -> None:
        self.host_node = host_node
        self.relay_host = relay_host
        self.relay_port = relay_port
        self.room_id = room_id
        self.token = token
        self.name = name
        self.timeout = timeout
        self.link: Optional[Link] = None
        self.public_addr = f"{relay_host}:{relay_port}"
        self._closing = False

    def connect(self) -> "RelayAttachment":
        ready = threading.Event()
        failure: List[str] = []

        def on_frame(link: Link, kind: int, payload: bytes) -> None:
            if kind != KIND_JSON:
                return
            try:
                msg = unpack_json(payload)
            except ProtocolError:
                return
            if not isinstance(msg, dict):
                return
            if msg.get("t") == "auth_ok":
                ready.set()
            elif msg.get("t") in ("error", "reject"):
                failure.append(str(msg.get("message") or msg.get("reason") or "中继拒绝接入"))
                ready.set()

        link = _dial(self.relay_host, self.relay_port, self.timeout, "中继", _HINT_RELAY)
        link.on_frame = on_frame
        link.name = "relay"
        link.start()
        link.send_json({
            "t": "auth",
            "role": "host",
            "room": self.room_id,
            "token": self.token,
            "name": self.name or self.host_node.room_name,
            "v": PROTOCOL_VERSION,
        })
        if not ready.wait(self.timeout):
            link.close("中继认证超时")
            raise TimeoutError("中继认证超时")
        if failure:
            link.close("中继拒绝接入")
            raise ConnectionRefusedError(failure[0])

        link.on_frame = self.host_node._on_relay_frame
        link.on_close = self._on_relay_close
        self.link = link
        log.info("房间已挂上中继 %s，房间号 %s", self.public_addr, self.room_id)
        return self

    def send_to_peer(self, peer_id: int, kind: int, payload: bytes) -> bool:
        if self.link is None or not self.link.alive:
            return False
        return self.link.send_frame(KIND_RELAY_DATA, pack_relay_body(peer_id, kind, payload))

    def reject_peer(self, peer_id: int, reason: str) -> None:
        """让中继把某个客户端踢掉（通常是密码不对或房间满了）。"""
        if self.link is not None:
            self.link.send_json({"t": "reject", "peer_id": peer_id, "reason": reason})

    def _on_relay_close(self, link: Link, reason: str) -> None:
        if not self._closing:
            # 正常关房时也会走到这里，那种情况不值得报警告
            log.warning("中继连接断开：%s", reason)
        for peer_id in [p.peer_id for p in self.host_node.peers.values() if p.is_relay]:
            self.host_node._remove_member(peer_id, f"中继断开：{reason}")

    def close(self) -> None:
        self._closing = True
        if self.link is not None:
            self.link.close("主机主动断开中继")
            self.link = None

    @property
    def connected(self) -> bool:
        return self.link is not None and self.link.alive


def _json_body(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
