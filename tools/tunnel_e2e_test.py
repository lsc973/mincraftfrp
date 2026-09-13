"""隧道 CLI 的端到端验证 —— 用真实子进程模拟完整的跨网段场景。

单元测试是在一个进程里建的隧道，这里换成三个独立进程 + 一个真实中继，
走的是用户实际会走的那条路：

    1. 假服务（回声服务器，扮演 Minecraft 的 25565）
    2. lanlink tunnel --to   127.0.0.1:<服务端口>    <- "我在家里"
    3. lanlink tunnel --listen 127.0.0.1:<本地端口>   <- "朋友在外地"

然后以"朋友的 Minecraft"的身份连本地端口，验证数据真的穿过去了。

用法::

    python tools/tunnel_e2e_test.py
"""
import os
import socket
import subprocess
import sys
import threading
import time

ENV = dict(os.environ)
ENV["PYTHONIOENCODING"] = "utf-8"

RELAY_PORT = "53401"
TUNNEL_JOIN_PORT = "53402"


class FakeService:
    """冒充 Minecraft 服务：回声。"""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.got = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with conn:
            while True:
                try:
                    d = conn.recv(65536)
                except OSError:
                    return
                if not d:
                    return
                self.got.append(d)
                conn.sendall(d)


EXE = os.environ.get("LANLINK_EXE", "")
problems = []


def check(label, ok, detail=""):
    print(f"  {'[通过]' if ok else '[失败]'} {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        problems.append(label)


def wait_port(port, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def wait_until(pred, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


def spawn(args):
    base = [sys.executable, "-m", "lanlink"]
    if EXE:                      # --exe 时改用打包好的二进制
        base = [EXE]
    return subprocess.Popen(
        base + args,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=ENV,
        stdin=subprocess.PIPE,
    )


service = FakeService()
print(f"假服务（冒充 Minecraft）跑在 127.0.0.1:{service.port}")
print()

relay = spawn(["relay", "--port", RELAY_PORT])
server = None
client = None
try:
    time.sleep(4)

    print("=== 起隧道两端 ===")
    # 我这边：服务在本机，所以用 --to
    server = spawn([
        "tunnel", "--room", "minecraft", "--to", f"127.0.0.1:{service.port}",
        "--relay", f"127.0.0.1:{RELAY_PORT}", "--name", "房主",
    ])
    time.sleep(4)

    # 朋友那边：本地监听，走中继
    client = spawn([
        "tunnel", "--room", "minecraft", "--listen", f"127.0.0.1:{TUNNEL_JOIN_PORT}",
        "--relay", f"127.0.0.1:{RELAY_PORT}", "--name", "朋友",
    ])

    started = wait_port(int(TUNNEL_JOIN_PORT), timeout=20)
    check("朋友那边的本地端口已监听", started, f"127.0.0.1:{TUNNEL_JOIN_PORT}")
    if not started:
        raise SystemExit(1)

    print()
    print("=== 朋友在 Minecraft 里连 127.0.0.1:%s ===" % TUNNEL_JOIN_PORT)
    sock = socket.create_connection(("127.0.0.1", int(TUNNEL_JOIN_PORT)), timeout=15)
    sock.settimeout(15)

    # 模拟 Minecraft 握手 + 一些数据
    payload = b"\x10\x00\xff\x05localhost\x63\xdd\x01" + b"hello from the other side" * 50
    sock.sendall(payload)
    got = b""
    while len(got) < len(payload):
        chunk = sock.recv(65536)
        if not chunk:
            break
        got += chunk

    check("数据穿过隧道送到假服务并原样回来", got == payload,
          f"发 {len(payload)} 字节，回 {len(got)} 字节")
    check("假服务确实收到了字节",
          wait_until(lambda: any(b"hello from the other side" in d for d in service.got)))

    # 大块二进制
    blob = bytes(range(256)) * 512   # 128 KB
    sock.sendall(blob)
    got2 = b""
    while len(got2) < len(blob):
        chunk = sock.recv(65536)
        if not chunk:
            break
        got2 += chunk
    check("128 KB 二进制完整穿越", got2 == blob, f"回 {len(got2)} 字节")

    sock.close()

    # 再来一条 —— 多流复用
    sock2 = socket.create_connection(("127.0.0.1", int(TUNNEL_JOIN_PORT)), timeout=15)
    sock2.settimeout(15)
    sock2.sendall(b"second-connection")
    check("第二条连接也通", sock2.recv(4096) == b"second-connection")
    sock2.close()

finally:
    for p in (client, server, relay):
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
    time.sleep(1)
    for p in (client, server, relay):
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass
    service.sock.close()

print()
print("=" * 66)
if problems:
    print(f"  {len(problems)} 项没过：" + "、".join(problems))
else:
    print("  CLI 隧道端到端验证通过 —— Minecraft 那类服务确实能穿过去")
print("=" * 66)
sys.exit(1 if problems else 0)
