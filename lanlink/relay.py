"""公网中继服务器。

什么时候需要它？两边不在同一个局域网、又没法直连的时候。中继做的事
很简单 —— 把字节搬来搬去，不解析业务内容：

    客户端 --TCP--> 中继 --TCP--> 主机

主机跟中继之间只有一条连接，所有通过中继进来的客户端都复用这一条，
靠帧里的 peer_id 区分。中继分配的 peer_id 从 ``RELAY_ID_BASE`` 起，
跟主机自己分配的 1、2、3… 不会撞车。

对主机来说，中继来的玩家和局域网来的玩家长得一模一样，业务代码
不需要任何分支。

跑起来::

    python -m lanlink relay --port 9000

想加个准入口令（防止被人白嫖带宽）就带 ``--token``，主机和客户端
两边填一样的。
"""

from __future__ import annotations

import itertools
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .link import Link, bind_reusable
from .node import PROTOCOL_VERSION, RELAY_ID_BASE
from .protocol import (
    KIND_JSON,
    KIND_RELAY_CTRL,
    KIND_RELAY_DATA,
    ProtocolError,
    pack_relay_body,
    split_relay_data,
    unpack_json,
)

__all__ = ["RelayServer", "RelayRoomInfo", "list_relay_rooms"]

log = logging.getLogger("lanlink.relay")

DEFAULT_RELAY_PORT = 9000

#: 没通过认证的连接最多挂多久（秒）。
AUTH_TIMEOUT = 10.0


@dataclass
class RelayRoomInfo:
    """中继上的一间房。"""

    room_id: str
    name: str
    clients: int
    host_addr: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "room_id": self.room_id,
            "name": self.name,
            "clients": self.clients,
            "host_addr": self.host_addr,
            "age": round(time.time() - self.created_at, 1),
        }

    def __str__(self) -> str:
        return f"{self.name} [{self.room_id}] {self.clients} 人"


class _Room:
    __slots__ = ("room_id", "name", "host_link", "host_addr", "clients", "_ids", "created_at")

    def __init__(self, room_id: str, host_link: Link, name: str) -> None:
        self.room_id = room_id
        self.name = name
        self.host_link = host_link
        self.host_addr = host_link.peer_addr
        self.clients: Dict[int, Link] = {}
        self.created_at = time.time()
        self._ids = itertools.count(RELAY_ID_BASE)

    def next_peer_id(self) -> int:
        return next(self._ids)

    def info(self) -> RelayRoomInfo:
        return RelayRoomInfo(
            room_id=self.room_id,
            name=self.name,
            clients=len(self.clients),
            host_addr=self.host_addr,
            created_at=self.created_at,
        )


class RelayServer:
    """中继服务器本体。

    ::

        server = RelayServer(port=9000)
        server.start()
        server.serve_forever()
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = DEFAULT_RELAY_PORT,
        *,
        token: str = "",
        max_clients_per_room: int = 64,
        max_rooms: int = 256,
    ) -> None:
        self.bind_host = host
        self.port = port
        self.token = token
        self.max_clients_per_room = max_clients_per_room
        self.max_rooms = max_rooms

        self._server: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._rooms: Dict[str, _Room] = {}
        self._lock = threading.RLock()
        self._closed = False
        self.address = ""

    # ------------------------------------------------------------ 生命周期

    def start(self) -> "RelayServer":
        if self._server is not None:
            return self
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bind_reusable(server)
        server.bind((self.bind_host, self.port))
        server.listen(64)
        self._server = server
        self.port = server.getsockname()[1]
        self.address = f"{self.bind_host}:{self.port}"
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="lanlink-relay-accept", daemon=True
        )
        self._accept_thread.start()
        log.info("中继已启动，监听 %s", self.address)
        return self

    def serve_forever(self) -> None:
        if self._server is None:
            self.start()
        while not self._closed:
            time.sleep(0.2)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            rooms = list(self._rooms.values())
            self._rooms.clear()
        for room in rooms:
            self._teardown_room(room, "中继服务器已关闭")
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None

    def __enter__(self) -> "RelayServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------ 房间

    @property
    def rooms(self) -> List[RelayRoomInfo]:
        with self._lock:
            return [room.info() for room in self._rooms.values()]

    def _accept_loop(self) -> None:
        while not self._closed and self._server is not None:
            try:
                sock, addr = self._server.accept()
            except OSError:
                return
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            link = Link(sock, addr, name=f"auth-{addr[0]}", on_frame=self._on_pending_frame)
            timer = threading.Timer(AUTH_TIMEOUT, self._auth_expired, args=(link,))
            timer.daemon = True
            link._auth_timer = timer  # type: ignore[attr-defined]
            timer.start()
            link.start()

    @staticmethod
    def _auth_expired(link: Link) -> None:
        if link.alive and link.name.startswith("auth-"):
            link.close("认证超时")

    def _on_pending_frame(self, link: Link, kind: int, payload: bytes) -> None:
        if kind != KIND_JSON:
            link.close("认证阶段只接受 JSON 消息")
            return
        try:
            msg = unpack_json(payload)
        except ProtocolError:
            link.close("认证消息无法解析")
            return
        if not isinstance(msg, dict):
            link.close("认证消息格式不对")
            return

        action = msg.get("t")
        if action == "list":
            link.send_json({"t": "rooms", "rooms": [r.to_dict() for r in self.rooms]})
            link.close("查询完成")
            return
        if action != "auth":
            link.close("期望 auth 消息")
            return
        self._authenticate(link, msg)

    def _authenticate(self, link: Link, msg: Dict[str, Any]) -> None:
        timer = getattr(link, "_auth_timer", None)
        if timer is not None:
            timer.cancel()

        if self.token and str(msg.get("token", "")) != self.token:
            self._deny(link, "中继口令不正确")
            return

        role = msg.get("role")
        room_id = str(msg.get("room") or "").strip()
        if not room_id:
            self._deny(link, "缺少房间号")
            return

        if role == "host":
            self._admit_host(link, room_id, msg)
        elif role == "client":
            self._admit_client(link, room_id, msg)
        else:
            self._deny(link, f"未知角色 {role!r}")

    def _deny(self, link: Link, message: str) -> None:
        link.send_json({"t": "error", "reason": message, "message": message})
        link.close(f"拒绝：{message}")

    # ------------------------------------------------------------ 主机接入

    def _admit_host(self, link: Link, room_id: str, msg: Dict[str, Any]) -> None:
        with self._lock:
            existing = self._rooms.get(room_id)
            if existing is not None:
                self._deny(link, f"房间 {room_id} 已被占用")
                return
            if len(self._rooms) >= self.max_rooms:
                self._deny(link, "中继房间数已达上限")
                return
            room = _Room(room_id, link, str(msg.get("name") or room_id))
            self._rooms[room_id] = room

        link.name = f"host-{room_id}"
        link.on_frame = lambda l, k, p: self._from_host(room, l, k, p)
        link.on_close = lambda l, reason: self._host_gone(room, reason)
        link.send_json({
            "t": "auth_ok",
            "role": "host",
            "room": room_id,
            "v": PROTOCOL_VERSION,
        })
        log.info("房间 %s 上线（主机 %s）", room_id, link.peer_addr)

    def _host_gone(self, room: _Room, reason: str) -> None:
        with self._lock:
            if self._rooms.get(room.room_id) is room:
                del self._rooms[room.room_id]
        self._teardown_room(room, f"主机已下线（{reason}）")
        log.info("房间 %s 下线：%s", room.room_id, reason)

    def _teardown_room(self, room: _Room, reason: str) -> None:
        for client in list(room.clients.values()):
            client.send_json({"t": "error", "reason": "host_gone", "message": reason})
            client.close(reason)
        room.clients.clear()

    # ------------------------------------------------------------ 客户端接入

    def _admit_client(self, link: Link, room_id: str, msg: Dict[str, Any]) -> None:
        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                self._deny(link, f"房间 {room_id} 不存在，可能主机还没上线")
                return
            if len(room.clients) >= self.max_clients_per_room:
                self._deny(link, "房间人数已满")
                return
            peer_id = room.next_peer_id()
            room.clients[peer_id] = link

        link.name = f"client-{peer_id}"
        link.on_frame = lambda l, k, p: self._from_client(room, peer_id, l, k, p)
        link.on_close = lambda l, reason: self._client_gone(room, peer_id, reason)
        link.send_json({
            "t": "auth_ok",
            "role": "client",
            "room": room_id,
            "peer_id": peer_id,
            "v": PROTOCOL_VERSION,
        })
        # 让主机决定收不收：密码校验、人数上限都是主机的事
        self._to_host(room, {
            "t": "join",
            "peer_id": peer_id,
            "name": str(msg.get("name") or ""),
            "password": str(msg.get("password") or ""),
            "addr": link.peer_addr,
        })
        log.info("客户端 #%s 接入房间 %s（%s）", peer_id, room_id, link.peer_addr)

    def _client_gone(self, room: _Room, peer_id: int, reason: str) -> None:
        with self._lock:
            gone = room.clients.pop(peer_id, None)
        if gone is None:
            return
        self._to_host(room, {"t": "leave", "peer_id": peer_id, "reason": reason})
        log.info("客户端 #%s 离开房间 %s：%s", peer_id, room.room_id, reason)

    # ------------------------------------------------------------ 数据搬运

    def _to_host(self, room: _Room, message: Dict[str, Any]) -> None:
        room.host_link.send_frame(
            KIND_RELAY_CTRL,
            json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    def _from_client(self, room: _Room, peer_id: int, link: Link, kind: int, payload: bytes) -> None:
        """客户端发来的帧 —— 原样打包塞进主机那条连接。"""
        room.host_link.send_frame(KIND_RELAY_DATA, pack_relay_body(peer_id, kind, payload))

    def _from_host(self, room: _Room, link: Link, kind: int, payload: bytes) -> None:
        if kind == KIND_RELAY_DATA:
            try:
                peer_id, inner_kind, inner_payload = split_relay_data(payload)
            except ProtocolError:
                return
            with self._lock:
                client = room.clients.get(peer_id)
            if client is not None:
                client.send_frame(inner_kind, inner_payload)
            return

        if kind == KIND_JSON:
            # 主机发来的普通 JSON 是给中继的控制指令
            try:
                msg = unpack_json(payload)
            except ProtocolError:
                return
            if not isinstance(msg, dict):
                return
            if msg.get("t") == "reject":
                self._kick_client(room, int(msg.get("peer_id", 0)), str(msg.get("reason") or "被主机拒绝"))

    def _kick_client(self, room: _Room, peer_id: int, reason: str) -> None:
        with self._lock:
            client = room.clients.pop(peer_id, None)
        if client is None:
            return
        client.send_json({"t": "reject", "reason": "host_rejected", "message": reason})
        client.close(f"被主机拒绝：{reason}")

    # ------------------------------------------------------------ 状态

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "rooms": len(self._rooms),
                "clients": sum(len(r.clients) for r in self._rooms.values()),
                "address": self.address,
                "uptime_rooms": [r.info().to_dict() for r in self._rooms.values()],
            }

    def __repr__(self) -> str:
        return f"<RelayServer {self.address or '未启动'} {len(self._rooms)} 间房>"


def list_relay_rooms(
    host: str,
    port: int = DEFAULT_RELAY_PORT,
    *,
    timeout: float = 6.0,
) -> List[RelayRoomInfo]:
    """问一下中继上都有哪些房间。"""
    from .link import connect

    done = threading.Event()
    result: List[RelayRoomInfo] = []

    def on_frame(link: Link, kind: int, payload: bytes) -> None:
        if kind != KIND_JSON:
            return
        try:
            msg = unpack_json(payload)
        except ProtocolError:
            return
        if isinstance(msg, dict) and msg.get("t") == "rooms":
            for item in msg.get("rooms") or []:
                try:
                    result.append(RelayRoomInfo(
                        room_id=item["room_id"],
                        name=item.get("name", ""),
                        clients=int(item.get("clients", 0)),
                        host_addr=item.get("host_addr", ""),
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
            done.set()

    link = connect(host, port, timeout=timeout, on_frame=on_frame)
    link.start()
    try:
        link.send_json({"t": "list"})
        done.wait(timeout)
    finally:
        link.close("查询结束")
    return result
