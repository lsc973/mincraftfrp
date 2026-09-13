"""一条已建立的 TCP 链路。

``Link`` 干四件事，别的地方就不用再操心了：

1. 后台收线程，把收到的帧回调出去；
2. 发送加锁，多个线程同时发不会把帧写串；
3. 心跳：定期发 ping，收到 ping 自动回 pong；
4. 判死：太久没收到任何东西就主动关掉。局域网拔网线时 TCP 不会
   立刻报错，靠这个超时才能及时发现对端已经没了。

心跳帧在 ``Link`` 内部就被吃掉了，不会冒到上层 —— 上层看到的
永远是真正的业务帧。
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import threading
import time
from typing import Callable, Optional, Tuple

from .protocol import (
    KIND_JSON,
    ProtocolError,
    pack_frame,
    recv_frame,
    unpack_json,
)

__all__ = [
    "Link",
    "LinkClosed",
    "bind_reusable",
    "connect",
    "format_addr",
    "parse_addr",
    "parse_listen_addr",
]

log = logging.getLogger("lanlink.link")


def format_addr(host: str, port: int) -> str:
    """把地址和端口拼成能看懂的形式。

    IPv6 必须加方括号：``240e::1`` 后面直接跟 ``:50001`` 会变成
    ``240e::1:50001``，谁也分不清哪段是端口 —— 而且这个字符串会被当地址
    解析回去，不加括号直接就解析错了。
    """
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def parse_addr(text: str, *, allow_zero: bool = False) -> Optional[Tuple[str, int]]:
    """解析 ``host:port``，支持 IPv6 的 ``[地址]:端口`` 写法。

    解析不了返回 None。CLI 和图形界面都走这里，免得各写一份、
    然后 IPv6 在其中一处漏掉。

    为什么 IPv6 非要方括号：地址自己就带冒号，``240e:354::1:9000``
    根本分不清哪段是端口。方括号是 URL 里通行的写法，大家也熟悉。

    ``allow_zero`` 是给**监听**地址用的：那里端口 0 表示"让系统随便挑一个"，
    是正经用法。而连接目标写 0 没有任何意义，所以默认不接受。
    """
    text = (text or "").strip()
    if not text:
        return None

    if text.startswith("["):
        host, sep, rest = text[1:].partition("]")
        if not sep or not host:
            return None
        port = _port(rest.lstrip(":"), allow_zero)
        return (host, port) if port is not None else None

    if ":" not in text:
        return None
    host, _, port_text = text.rpartition(":")
    if ":" in host:
        # 没加方括号的 IPv6，rpartition 会把地址切碎
        return None
    port = _port(port_text, allow_zero)
    return (host, port) if port is not None else None


def _port(text: str, allow_zero: bool = False) -> Optional[int]:
    """端口号得在范围内。

    不校验的话 ``host:99999`` 会一路通过，直到 socket 那里才炸 —— 报的是
    "指定的端口无效" 之类，完全看不出是哪儿填错了。
    """
    try:
        port = int(text)
    except (TypeError, ValueError):
        return None
    lowest = 0 if allow_zero else 1
    return port if lowest <= port <= 65535 else None

def parse_listen_addr(text: str) -> Optional[Tuple[str, int]]:
    """解析监听地址。允许只写端口，默认绑 127.0.0.1。

    只给端口时不监听 0.0.0.0：隧道客户端一般只给自己机器上的程序连，
    对外监听等于顺手把服务开放给整个网段，不该是默认行为。
    """
    text = (text or "").strip()
    if not text:
        return None
    if ":" not in text:
        # 只写端口的情况：0 是合法的，表示让系统挑一个
        port = _port(text, allow_zero=True)
        return ("127.0.0.1", port) if port is not None else None
    return parse_addr(text, allow_zero=True)


#: 心跳发送间隔（秒）。
HEARTBEAT_INTERVAL = 10.0

#: 多久收不到任何帧就认为对端已死（秒）。得明显大于心跳间隔，
#: 免得网络抖一下就误杀。
IDLE_TIMEOUT = 35.0

FrameHandler = Callable[["Link", int, bytes], None]
CloseHandler = Callable[["Link", str], None]
ErrorHandler = Callable[["Link", Exception], None]


class LinkClosed(Exception):
    """在已经关闭的链路上操作。"""


class Link:
    """把 socket 包成一条有生命周期的、线程安全的链路。"""

    def __init__(
        self,
        sock: socket.socket,
        addr: Optional[Tuple[str, int]] = None,
        *,
        name: str = "",
        on_frame: Optional[FrameHandler] = None,
        on_close: Optional[CloseHandler] = None,
        on_error: Optional[ErrorHandler] = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
        idle_timeout: float = IDLE_TIMEOUT,
    ) -> None:
        self.sock = sock
        self.addr = addr
        self.name = name
        self.on_frame = on_frame
        self.on_close = on_close
        self.on_error = on_error
        self.heartbeat_interval = heartbeat_interval
        self.idle_timeout = idle_timeout

        self.alive = True
        self.close_reason = ""
        self.last_recv = time.monotonic()
        self.created_at = self.last_recv

        self._send_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._close_fired = False
        self._recv_thread: Optional[threading.Thread] = None
        self._hb_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ 生命周期

    def start(self) -> "Link":
        """启动收线程和心跳线程。重复调用无副作用。"""
        if not self.alive:
            # 关闭时会把 _recv_thread 置空（为了断开引用环），所以不能再拿它
            # 当"启动过了"的标志 —— 否则会以为没启动过，跑到已关闭的 socket 上开线程。
            raise LinkClosed(f"链路已关闭（{self.close_reason}），无法重新启动")
        if self._recv_thread is not None:
            return self
        self._recv_thread = threading.Thread(
            target=self._recv_loop, name=f"lanlink-recv-{self.label}", daemon=True
        )
        self._recv_thread.start()
        if self.heartbeat_interval > 0:
            self._hb_thread = threading.Thread(
                target=self._heartbeat_loop, name=f"lanlink-hb-{self.label}", daemon=True
            )
            self._hb_thread.start()
        return self

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        return self.peer_addr

    @property
    def peer_addr(self) -> str:
        if not self.addr:
            return "?"
        return format_addr(self.addr[0], self.addr[1])

    def close(self, reason: str = "主动关闭") -> None:
        self._shutdown(reason)

    def __enter__(self) -> "Link":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------ 发送

    def send_frame(self, kind: int, payload: bytes = b"") -> bool:
        """发一帧。链路已死或写失败返回 False（不会抛异常）。"""
        blob = pack_frame(kind, payload)
        with self._send_lock:
            if not self.alive:
                return False
            try:
                self.sock.sendall(blob)
                return True
            except OSError as exc:
                self._shutdown(f"发送失败：{exc}")
                return False

    def send_json(self, obj) -> bool:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self.send_frame(KIND_JSON, body)

    # ------------------------------------------------------------ 收线程

    def _recv_loop(self) -> None:
        try:
            while self.alive:
                frame = recv_frame(self.sock)
                if frame is None:
                    self._shutdown("对端关闭了连接")
                    return
                kind, payload = frame
                self.last_recv = time.monotonic()
                if kind == KIND_JSON and self._is_heartbeat(payload):
                    continue
                if self.on_frame is not None:
                    try:
                        self.on_frame(self, kind, payload)
                    except Exception as exc:  # 业务回调炸了不该拖垮整条链路
                        self._report_error(exc)
        except ProtocolError as exc:
            self._shutdown(f"协议错误：{exc}")
        except OSError as exc:
            self._shutdown(f"连接中断：{exc}")
        except Exception as exc:  # pragma: no cover - 兜底
            self._shutdown(f"接收线程异常：{exc!r}")
        finally:
            self._shutdown("接收线程退出")

    def _is_heartbeat(self, payload: bytes) -> bool:
        """心跳帧就地处理掉，返回 True 表示不用往上抛。"""
        try:
            msg = unpack_json(payload)
        except ProtocolError:
            return False
        if not isinstance(msg, dict):
            return False
        kind = msg.get("t")
        if kind == "ping":
            # 回 pong 时不要带原样时间戳，回自己的就行
            self.send_json({"t": "pong"})
            return True
        if kind == "pong":
            return True
        return False

    # ------------------------------------------------------------ 心跳线程

    def _heartbeat_loop(self) -> None:
        while self.alive:
            # 用 wait 而不是 sleep，关闭时能立刻醒来退出
            if self._stop_event.wait(self.heartbeat_interval):
                return
            if not self.alive:
                return
            idle = time.monotonic() - self.last_recv
            if idle > self.idle_timeout:
                self._shutdown(f"对端 {idle:.0f} 秒无响应，判定掉线")
                return
            self.send_json({"t": "ping"})

    @property
    def _stop_event(self) -> threading.Event:
        # 懒创建，省得 __init__ 里再挂一个字段
        try:
            return self.__stop_event
        except AttributeError:
            with self._state_lock:
                try:
                    return self.__stop_event
                except AttributeError:
                    self.__stop_event = threading.Event()
                    return self.__stop_event

    # ------------------------------------------------------------ 关闭

    def _shutdown(self, reason: str) -> None:
        with self._state_lock:
            if not self.alive:
                return
            self.alive = False
            self.close_reason = reason
            self._stop_event.set()
        # 先 shutdown 再 close：让阻塞在 recv 上的收线程立刻醒过来
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self._fire_close()
        self._release_refs()

    def _release_refs(self) -> None:
        """断开后把回调摘掉，让对象靠引用计数就能回收。

        这些回调通常是闭包，捕获着 Host / RelayServer / 房间对象，而它们
        又通过成员表反向持有 Link —— 一来一回就成了引用环。引用成环的对象
        只能等循环 GC 来收，在持续高频收发的进程里长命对象会被晋升到 gen2，
        于是句柄数会一直往上涨，看着像泄漏（实测每分钟几十个）。

        断开时主动切断这些边，回收就变成确定性的，不用赌 GC 什么时候跑。
        """
        timer = getattr(self, "_handshake_timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:  # pragma: no cover
                pass
            self._handshake_timer = None
        self.on_frame = None
        self.on_close = None
        self.on_error = None
        self._recv_thread = None
        self._hb_thread = None

    def _fire_close(self) -> None:
        with self._state_lock:
            if self._close_fired:
                return
            self._close_fired = True
        if self.on_close is not None:
            try:
                self.on_close(self, self.close_reason)
            except Exception as exc:  # pragma: no cover
                self._report_error(exc)

    def _report_error(self, exc: Exception) -> None:
        if self.on_error is not None:
            try:
                self.on_error(self, exc)
                return
            except Exception:  # pragma: no cover
                pass
        # 没挂 on_error 也绝不能把异常吞掉 —— 静默失败比崩溃难查得多
        log.exception("链路 %s 的帧回调出错", self.label, exc_info=exc)

    def __repr__(self) -> str:
        state = "alive" if self.alive else f"closed({self.close_reason})"
        return f"<Link {self.label} {state}>"


def bind_reusable(sock: socket.socket) -> socket.socket:
    """给监听 socket 设好地址复用选项。**Windows 上跟 POSIX 完全不是一回事。**

    POSIX 的 ``SO_REUSEADDR`` 只影响 TIME_WAIT 状态的端口，好处是服务重启
    时不会被上一个连接的残留卡住。Windows 的同名选项却是"允许别人绑我正在
    用的端口"（hijack）—— 后果很具体：

    第二个房间会**开成功**，用户看到房间号、界面上一切正常，但连接全被
    第一个进程接走。查起来极其费劲，因为两边都没有任何报错。

    所以 Windows 上改用 ``SO_EXCLUSIVEADDRUSE``，明确拒绝重复绑定 ——
    宁可让用户看到"端口被占用"，也不要给他一个假装开好了的房间。
    """
    if sys.platform == "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return sock


def connect(host: str, port: int, timeout: float = 8.0, **kwargs) -> Link:
    """建立一个 outbound 连接，返回还没 start 的 Link。"""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(None)  # 之后交给心跳超时来判死，不用 socket 自己的超时
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return Link(sock, sock.getpeername()[:2], **kwargs)
