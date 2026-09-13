"""测试用的公共小工具。

注意文件名是 ``helpers.py`` 而不是 ``test_*.py`` —— unittest 的自动发现
只认 ``test*.py``，所以这个文件不会被当成测试用例收集。
"""

from __future__ import annotations

import socket
import threading
import time


class EchoServer:
    """一个最小的 TCP 回声服务，给隧道测试当替身演员。

    纯回声：收到什么原样发回去，**不加任何前缀**。

    早先这里给每个 recv 到的分片都加了前缀，结果 256 KB 的测试多出来 30 字节
    —— 因为 TCP 没有消息边界，一次发送会被拆成好几个分片，每片都被加了前缀。
    这种坑正是隧道要面对的，测试里不能自己先踩一遍。
    """

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self.received: list = []
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            while not self._stop.is_set():
                try:
                    data = conn.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                self.received.append(data)
                try:
                    conn.sendall(data)   # 纯回声
                except OSError:
                    return

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def reserve_dead_port() -> int:
    """占一个端口再放掉，返回一个大概率没人监听的端口号。

    拿来测「服务不在时隧道怎么反应」。
    """
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


_config_tmp = None
_config_old = None


def isolate_config():
    """把 lanlink 的配置目录指到一个临时目录，返回那个目录。

    **不隔离会出真事**：隧道服务端一启动就会去更新免费域名。如果开发机上
    配了真域名，跑一遍测试套件就会把那个域名真的指来指去 —— 测试往第三方
    服务上写东西，这绝对不能接受。

    在测试模块里用 ``setUpModule()`` 调它。注意别写成类的 ``setUp``：
    类自己定义的 ``setUp`` 会盖掉父类的，很容易以为隔离上了其实没有。
    """
    global _config_tmp, _config_old
    import os
    import tempfile

    if _config_tmp is None:
        _config_old = os.environ.get("LANLINK_CONFIG_DIR")
        _config_tmp = tempfile.TemporaryDirectory()
    os.environ["LANLINK_CONFIG_DIR"] = _config_tmp.name

    from pathlib import Path as _Path

    return _Path(_config_tmp.name)
