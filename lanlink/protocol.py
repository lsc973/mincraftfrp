"""lanlink 线协议。

TCP 连接上流动的一切都是帧：

    +-------------+-----------+----------------------+
    | length (4)  | kind (1)  | payload (length - 1) |
    +-------------+-----------+----------------------+

length 是大端 uint32，包含 kind 字节本身。kind 决定 payload 怎么解释：

    KIND_JSON        UTF-8 JSON 文本，用于握手 / 心跳 / 房间管理
    KIND_APP         应用数据，payload 开头是 6 字节 APP 路由头
    KIND_RELAY_CTRL  中继控制，只出现在 主机 <-> 中继 之间
    KIND_RELAY_DATA  中继转发，只出现在 主机 <-> 中继 之间

应用数据（KIND_APP）在用户字节前加一个路由头：

    +---------+---------+---------------+--------------+
    | route 1 | flags 1 | peer_id (4)   | 用户数据      |
    +---------+---------+---------------+--------------+

peer_id 的含义随 route 变化：ROUTE_PEER 时是目标，ROUTE_DELIVER 时是来源。
"""

from __future__ import annotations

import json
import struct
from typing import Any, Optional, Tuple

__all__ = [
    "KIND_JSON",
    "KIND_APP",
    "KIND_RELAY_CTRL",
    "KIND_RELAY_DATA",
    "ROUTE_DIRECT",
    "ROUTE_PEER",
    "ROUTE_BCAST",
    "ROUTE_DELIVER",
    "FLAG_JSON",
    "MAX_FRAME",
    "ProtocolError",
    "pack_frame",
    "pack_json",
    "pack_app",
    "unpack_app",
    "unpack_json",
    "pack_relay_body",
    "split_relay_data",
    "recv_frame",
    "decode_frames",
    "kind_name",
]

# ---------------------------------------------------------------- 帧类型

KIND_JSON = 0x01
KIND_APP = 0x02
KIND_RELAY_CTRL = 0x10
KIND_RELAY_DATA = 0x11

_KIND_NAMES = {
    KIND_JSON: "JSON",
    KIND_APP: "APP",
    KIND_RELAY_CTRL: "RELAY_CTRL",
    KIND_RELAY_DATA: "RELAY_DATA",
}

# ---------------------------------------------------------------- 路由

ROUTE_DIRECT = 0   # 客户端 -> 主机：只交给主机自己消费
ROUTE_PEER = 1     # 客户端 -> 主机：请主机转发给 peer_id 指定的那个人
ROUTE_BCAST = 2    # 客户端 -> 主机：请主机转发给房间里除了我以外的所有人
ROUTE_DELIVER = 3  # 主机 -> 客户端：投递，peer_id 标出这条数据是谁发的（0 = 主机本人）

FLAG_JSON = 0x01   # 应用数据本身是一段 JSON 文本

_HEADER = struct.Struct("!IB")
_APP_HEADER = struct.Struct("!BBI")
_RELAY_HEADER = struct.Struct("!I")

#: 单帧 payload 上限。防止对端报一个天文数字的 length 把内存吃光。
MAX_FRAME = 8 * 1024 * 1024


class ProtocolError(Exception):
    """对端发来的数据无法按协议解析。"""


# ---------------------------------------------------------------- 打包


def pack_frame(kind: int, payload: bytes = b"") -> bytes:
    """把 kind + payload 封成一个完整帧。"""
    return _HEADER.pack(len(payload) + 1, kind) + payload


def pack_json(obj: Any, kind: int = KIND_JSON) -> bytes:
    """把 Python 对象序列化成 JSON 帧。"""
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return pack_frame(kind, body)


def pack_app(route: int, peer_id: int, data: bytes, *, is_json: bool = False) -> bytes:
    """组一条 KIND_APP 的载荷：路由头 + 用户数据。

    注意这里只返回 payload，不含帧头 —— 交给 ``Link.send_frame(KIND_APP, ...)``
    去加。跟 ``pack_relay_body`` 保持同一套约定，免得套两层帧头。
    """
    flags = FLAG_JSON if is_json else 0
    header = _APP_HEADER.pack(route & 0xFF, flags, peer_id & 0xFFFFFFFF)
    return header + data


def pack_relay_body(peer_id: int, kind: int, payload: bytes) -> bytes:
    """组一条 KIND_RELAY_DATA 的载荷（不含帧头，交给 Link.send_frame 加）。"""
    return _RELAY_HEADER.pack(peer_id & 0xFFFFFFFF) + bytes([kind]) + payload


# ---------------------------------------------------------------- 解包


def unpack_json(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"JSON 帧解析失败：{exc}") from exc


def unpack_app(payload: bytes) -> Tuple[int, int, bool, bytes]:
    """拆开应用数据帧，返回 (route, peer_id, is_json, 用户数据)。"""
    if len(payload) < _APP_HEADER.size:
        raise ProtocolError("应用数据帧太短，缺少路由头")
    route, flags, peer_id = _APP_HEADER.unpack_from(payload, 0)
    return route, peer_id, bool(flags & FLAG_JSON), payload[_APP_HEADER.size:]


def split_relay_data(payload: bytes) -> Tuple[int, int, bytes]:
    """拆开中继转发帧，返回 (peer_id, kind, payload)。"""
    if len(payload) < _RELAY_HEADER.size + 1:
        raise ProtocolError("中继转发帧太短")
    (peer_id,) = _RELAY_HEADER.unpack_from(payload, 0)
    kind = payload[_RELAY_HEADER.size]
    return peer_id, kind, payload[_RELAY_HEADER.size + 1:]


def kind_name(kind: int) -> str:
    return _KIND_NAMES.get(kind, f"0x{kind:02x}")


# ---------------------------------------------------------------- 收帧


def _recv_exact(sock, n: int) -> Optional[bytes]:
    """精确读 n 字节。对端在读完前正常关闭则返回 None。"""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(sock) -> Optional[Tuple[int, bytes]]:
    """从 socket 阻塞读出一个完整帧。

    返回 (kind, payload)；对端正常关闭返回 None。
    数据不符合协议时抛 ProtocolError，socket 出错抛 OSError。
    """
    head = _recv_exact(sock, _HEADER.size)
    if head is None:
        return None
    length, kind = _HEADER.unpack(head)
    if length < 1:
        raise ProtocolError(f"非法帧长度 {length}")
    body_len = length - 1
    if body_len > MAX_FRAME:
        raise ProtocolError(f"帧过大：{body_len} 字节，上限 {MAX_FRAME}")
    if body_len == 0:
        return kind, b""
    payload = _recv_exact(sock, body_len)
    if payload is None:
        return None
    return kind, payload


def decode_frames(buffer: bytes):
    """增量解码一段字节流，返回 (帧列表, 剩余字节)。

    给不方便用阻塞 recv 的场景（测试、异步桥接）留的口子。
    遇到不完整的帧就把尾巴留在剩余字节里，下次接着喂。
    """
    frames = []
    offset = 0
    total = len(buffer)
    while total - offset >= _HEADER.size:
        length, kind = _HEADER.unpack_from(buffer, offset)
        if length < 1:
            raise ProtocolError(f"非法帧长度 {length}")
        body_len = length - 1
        if body_len > MAX_FRAME:
            raise ProtocolError(f"帧过大：{body_len} 字节，上限 {MAX_FRAME}")
        if total - offset - _HEADER.size < body_len:
            break
        start = offset + _HEADER.size
        frames.append((kind, buffer[start:start + body_len]))
        offset = start + body_len
    return frames, buffer[offset:]
