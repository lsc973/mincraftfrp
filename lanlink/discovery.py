"""局域网房间发现。

用 UDP 广播，不需要任何额外依赖：

* 主机每 2 秒往广播地址喊一次 ``announce``，包里带着房间信息。
* 客户端平时被动听着；主动扫描时额外发一发 ``query``，
  主机收到立刻单播回一次 ``announce``。

两条路都留着是有原因的：很多交换机、AP 隔离模式或者 Windows 防火墙
会吃掉广播包，但单播往往还通。所以主动 query 能让"广播听不见"的
网络也照样被发现。

另外每台机器可能有多张网卡，广播地址一律同时往
``255.255.255.255`` 和 ``<本机IP前三段>.255`` 各发一份。
"""

from __future__ import annotations

import json
import select
import socket
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "DISCOVERY_PORT",
    "MAGIC",
    "RoomInfo",
    "local_ip",
    "broadcast_targets",
    "Beacon",
    "Scanner",
    "scan",
]

#: 默认发现端口。想在同一台机器上跑多个互不干扰的集群，改这个就行。
DISCOVERY_PORT = 47777

#: 报文里的版本标记，用来一眼认出"这是 lanlink 的包"。
MAGIC = "lanlink"

_PROTOCOL_VERSION = 1


@dataclass
class RoomInfo:
    """一个被发现（或被创建）的房间。"""

    room_id: str
    room_name: str
    host_name: str
    address: str
    port: int
    players: int = 1
    max_players: int = 16
    has_password: bool = False
    relay_room: str = ""
    relay_addr: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def direct_addr(self) -> str:
        """直连用的 ``host:port``。"""
        return f"{self.address}:{self.port}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RoomInfo":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def __str__(self) -> str:
        lock = " 🔒" if self.has_password else ""
        net = self.relay_room and f"中继 {self.relay_room}" or self.direct_addr
        return f"{self.room_name}{lock} [{self.room_id}] {self.players}/{self.max_players} @ {net}"


def local_ip() -> str:
    """拿到本机在默认出口网卡上的 IP。

    连一个外网地址（不会真的发包）就能让内核替我们选出网卡，
    比 hostname 解析靠谱得多 —— 后者常返回 127.0.0.1。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def global_ipv6() -> Optional[str]:
    """拿到本机全球可达的 IPv6 地址，没有就返回 None。

    **为什么值得单独拎出来**：国内运营商大范围上了 IPv6，而且 IPv6 通常
    **不做 NAT** —— 每台设备拿到的就是全球可路由的地址。所以哪怕宽带在
    CGNAT 后面（IPv4 没有公网地址），只要两边都有 IPv6，照样能直接互连，
    不需要中继、也不用装任何额外软件。

    会跳过链路本地（fe80::）、回环和唯一本地（fc00::/7）—— 那些出了本机就没用了。
    """
    import ipaddress

    seen = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6)
        seen.extend(info[4][0] for info in infos)
    except (socket.gaierror, OSError):
        pass

    # hostname 解析不一定全，再问问"连外网时用哪张网卡"
    probe = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    try:
        probe.connect(("2001:4860:4860::8888", 80))   # Google DNS 的 v6 地址
        seen.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    for raw in seen:
        address = raw.split("%")[0]   # 去掉 %eth0 这种 scope id
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_link_local or ip.is_loopback or ip.is_private or ip.is_multicast:
            continue
        return address
    return None


def broadcast_targets() -> List[str]:
    """所有值得发一份广播的目标地址。"""
    targets = ["255.255.255.255"]
    ip = local_ip()
    if ip.count(".") == 3 and not ip.startswith("127."):
        # 家用/办公网络基本是 /24，按 /24 推子网广播地址足够用；
        # 推错了也不要紧，255.255.255.255 那份仍然会到。
        subnet = ip.rsplit(".", 1)[0] + ".255"
        if subnet not in targets:
            targets.append(subnet)
    return targets


def probe_targets() -> List[str]:
    """探测包的目标地址。

    比广播多一个回环地址：同一台机器上同时跑着 host 和 list 时，
    广播包可能被交换机/防火墙吃掉，但回环这一份一定送得到。
    """
    return broadcast_targets() + ["127.0.0.1"]


def _make_socket(port: int, *, reuse: bool = True) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if reuse:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):  # Windows 上没有
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", port))
    return sock


class Beacon:
    """主机侧：周期性广播房间信息，并回应客户端的探测。

    ``info_provider`` 每次广播时都会被调用一次，所以玩家数之类的
    动态字段能实时反映出去，不用手动刷新。
    """

    def __init__(
        self,
        info_provider: Callable[[], Dict[str, Any]],
        port: int = DISCOVERY_PORT,
        interval: float = 2.0,
    ) -> None:
        self.info_provider = info_provider
        self.port = port
        self.interval = interval
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._sock = _make_socket(self.port)
        self._sock.settimeout(0.5)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="lanlink-beacon", daemon=True)
        self._thread.start()
        self.announce()

    def announce(self) -> None:
        """立刻广播一次。"""
        sock = self._sock  # stop() 可能随时把它置空，先抓住
        if sock is None:
            return
        payload = dict(self.info_provider())
        payload.update({"ll": MAGIC, "v": _PROTOCOL_VERSION, "t": "announce"})
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for target in broadcast_targets():
            try:
                sock.sendto(blob, (target, self.port))
            except OSError:
                pass

    def _loop(self) -> None:
        last_announce = 0.0
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            now = time.monotonic()
            if now - last_announce >= self.interval:
                self.announce()
                last_announce = now
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            reply = self._handle_probe(data)
            if reply is not None:
                try:
                    sock.sendto(reply, addr)
                except OSError:
                    pass

    def _handle_probe(self, data: bytes) -> Optional[bytes]:
        """收到 query 就回一份 announce（单播回去）。"""
        try:
            msg = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(msg, dict) or msg.get("ll") != MAGIC:
            return None
        if msg.get("t") != "query":
            return None
        payload = dict(self.info_provider())
        payload.update({"ll": MAGIC, "v": _PROTOCOL_VERSION, "t": "announce"})
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None


class Scanner:
    """客户端侧：持续收集局域网里冒出来的房间。

    房间有 TTL —— 主机停止广播超过 ``ttl`` 秒就从列表里消失，
    所以拔网线、关程序这类"没有告别"的离场也能被反映出来。

    这里用了两个 socket，是有原因的：

    * **查询 socket** 绑在系统随便给的临时端口上。探测包从这里发出去，
      主机的单播回复自然就回到这个端口 —— 不会跟别人抢。
    * **监听 socket** 绑在约定的发现端口上，专门用来收主机周期性发的广播。

    起先只用了一个 socket 绑发现端口，结果同一台机器上既开房又扫描时，
    回包会在两个同端口 socket 之间随机投递，扫描就时灵时不灵。
    """

    def __init__(
        self,
        port: int = DISCOVERY_PORT,
        ttl: float = 6.0,
        probe_interval: float = 1.5,
    ) -> None:
        self.port = port
        self.ttl = ttl
        self.probe_interval = probe_interval
        self._query_sock: Optional[socket.socket] = None
        self._listen_sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._rooms: Dict[str, tuple] = {}  # room_id -> (RoomInfo, 最后出现时间)
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            return
        # 查询走临时端口，监听走约定端口，两个 socket 各司其职
        self._query_sock = _make_socket(0, reuse=False)
        self._listen_sock = _make_socket(self.port)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="lanlink-scanner", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        last_probe = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_probe >= self.probe_interval:
                self.probe()
                last_probe = now
            socks = [s for s in (self._query_sock, self._listen_sock) if s is not None]
            if not socks:
                break
            try:
                readable, _, _ = select.select(socks, [], [], 0.5)
            except (OSError, ValueError):
                break
            for sock in readable:
                try:
                    data, addr = sock.recvfrom(4096)
                except OSError:
                    continue
                self._ingest(data, addr)

    def probe(self) -> None:
        """主动喊一嗓子：谁在？"""
        # 先取到本地变量再用：stop() 可能随时把字段置空
        sock = self._query_sock
        if sock is None:
            return
        blob = json.dumps({"ll": MAGIC, "v": _PROTOCOL_VERSION, "t": "query"}).encode("utf-8")
        for target in probe_targets():
            try:
                sock.sendto(blob, (target, self.port))
            except OSError:
                pass

    def _ingest(self, data: bytes, addr) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(msg, dict) or msg.get("ll") != MAGIC:
            return
        if msg.get("t") != "announce":
            return
        room_id = msg.get("room_id")
        if not room_id:
            return
        msg.setdefault("address", addr[0])
        try:
            info = RoomInfo.from_dict(msg)
        except TypeError:
            return
        if not info.address:
            info.address = addr[0]
        with self._lock:
            self._rooms[room_id] = (info, time.monotonic())

    def rooms(self) -> List[RoomInfo]:
        """当前活着的房间，按名字排序。"""
        now = time.monotonic()
        with self._lock:
            for room_id, (_, seen) in list(self._rooms.items()):
                if now - seen > self.ttl:
                    del self._rooms[room_id]
            items = [info for info, _ in self._rooms.values()]
        return sorted(items, key=lambda r: r.room_name)

    def find(self, room_id: str) -> Optional[RoomInfo]:
        """按房间号或房间名精确找一个房间。"""
        for info in self.rooms():
            if info.room_id == room_id or info.room_name == room_id:
                return info
        return None

    def stop(self) -> None:
        self._stop.set()
        for name in ("_query_sock", "_listen_sock"):
            sock = getattr(self, name)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                setattr(self, name, None)
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None


def scan(timeout: float = 1.5, port: int = DISCOVERY_PORT) -> List[RoomInfo]:
    """扫一次，等 ``timeout`` 秒后返回结果。"""
    scanner = Scanner(port=port, ttl=max(timeout + 1.0, 3.0))
    scanner.start()
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.05)
        return scanner.rooms()
    finally:
        scanner.stop()
