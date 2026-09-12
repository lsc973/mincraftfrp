"""最小可运行例子 —— 一眼看清这套框架怎么用。

开两个终端，先跑主机再跑客户端::

    python examples/minimal.py host
    python examples/minimal.py client 192.168.1.10:50001

客户端不填地址的话会自动扫描局域网找房间。
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanlink import Client, Host, scan  # noqa: E402


def run_host() -> None:
    # 起一间房。port=0 让系统随便挑个空闲端口，start() 之后就能读到真实端口
    host = Host("示例房间", name="房主").start()
    print(f"房间号 {host.room_id}，局域网地址 {host.address}:{host.port}")
    print("等着客户端连进来…（Ctrl+C 退出）")

    @host.on("peer_join")
    def _(peer):
        print(f"[+] {peer} 来了")
        host.broadcast(f"欢迎 {peer.name}！".encode(), source=0)

    @host.on("peer_leave")
    def _(peer, reason):
        print(f"[-] {peer} 走了（{reason}）")

    @host.on("data")
    def _(source, data, is_json):
        print(f"[{source}] {data.decode(errors='replace')}")

    try:
        host.serve_forever()
    except KeyboardInterrupt:
        print("\n关房")
    finally:
        host.close()


def run_client(address: str = "") -> None:
    if address:
        host_part, _, port_part = address.rpartition(":")
        target = (host_part, int(port_part))
    else:
        print("扫描局域网…")
        rooms = scan(timeout=2.5)
        if not rooms:
            print("没找到房间。手动指定地址：python examples/minimal.py client 192.168.1.10:50001")
            return
        for room in rooms:
            print(f"  {room}")
        best = rooms[0]
        target = (best.address, best.port)

    client = Client.connect(target[0], target[1], name="客户端")
    print(f"已加入 {client.room.room_name}，我是 #{client.peer_id}")
    print("输入内容回车即广播，Ctrl+C 退出\n")

    @client.on("data")
    def _(source, data, is_json):
        who = "主机" if source == 0 else f"#{source}"
        print(f"[{who}] {data.decode(errors='replace')}")

    @client.on("close")
    def _():
        print("连接已断开")
        sys.exit(0)

    try:
        while True:
            line = input()
            if line.strip():
                client.broadcast(line.encode())
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        client.close()


def run_scan() -> None:
    rooms = scan(timeout=3.0)
    if not rooms:
        print("没发现房间")
    for room in rooms:
        print(f"  {room}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "host":
        run_host()
    elif mode == "client":
        run_client(sys.argv[2] if len(sys.argv) > 2 else "")
    elif mode == "scan":
        run_scan()
    else:
        print(__doc__)
        sys.exit(2)
