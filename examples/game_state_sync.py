"""更像真实联机游戏的例子：主机权威 + 状态广播 + 客户端输入 + 延迟统计。

演示的是这套框架设计时真正想解决的场景：

* 主机以固定频率（20Hz）广播一份游戏状态；
* 客户端只上报输入，不自己算结果；
* 每个客户端能测出自己到主机的往返延迟（RTT）。

跑法::

    python examples/game_state_sync.py host
    python examples/game_state_sync.py client 192.168.1.10:50001

客户端连上后每秒打印一次自己的 RTT。主机那边能看到每个玩家的输入。
"""

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanlink import Client, Host, scan  # noqa: E402

TICK_HZ = 20
TICK = 1.0 / TICK_HZ


class GameState:
    """一份假的世界状态，用来演示 —— 换成你自己的游戏数据即可。"""

    def __init__(self) -> None:
        self.tick = 0
        self.players: dict = {}

    def apply_input(self, peer_id: int, action: str) -> None:
        player = self.players.setdefault(peer_id, {"x": 0.0, "y": 0.0, "moves": 0})
        step = 1.0
        if action == "up":
            player["y"] -= step
        elif action == "down":
            player["y"] += step
        elif action == "left":
            player["x"] -= step
        elif action == "right":
            player["x"] += step
        player["moves"] += 1

    def snapshot(self) -> dict:
        return {"tick": self.tick, "players": self.players}


def run_host() -> None:
    host = Host("游戏房", name="主机").start()
    state = GameState()
    print(f"房间号 {host.room_id} @ {host.address}:{host.port}")
    print("20Hz 广播状态中… Ctrl+C 退出\n")

    @host.on("peer_join")
    def _(peer):
        print(f"[+] {peer} 加入，当前 {host.player_count} 人")

    @host.on("peer_leave")
    def _(peer, reason):
        state.players.pop(peer.peer_id, None)
        print(f"[-] {peer} 离开（{reason}）")

    @host.on("data")
    def _(source, data, is_json):
        if not is_json:
            return
        try:
            msg = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return
        kind = msg.get("t")
        if kind == "input":
            state.apply_input(source, str(msg.get("action", "")))
        elif kind == "ping":
            # 原样把客户端的时间戳发回去，让它自己算 RTT —— 这样不用假设两边时钟一致
            host.send_json_to(source, {"t": "pong", "ts": msg.get("ts")})

    stop = threading.Event()

    def ticker():
        next_at = time.monotonic()
        while not stop.is_set():
            state.tick += 1
            # 状态对所有客户端一视同仁，直接广播
            host.broadcast_json({"t": "state", "s": state.snapshot()})
            next_at += TICK
            delay = next_at - time.monotonic()
            if delay > 0:
                stop.wait(delay)
            else:
                next_at = time.monotonic()  # 落后了就重新对齐，别追帧

    threading.Thread(target=ticker, daemon=True).start()
    try:
        host.serve_forever()
    except KeyboardInterrupt:
        print("\n关房")
    finally:
        stop.set()
        host.close()


def run_client(address: str) -> None:
    host_part, _, port_part = address.rpartition(":")
    client = Client.connect(host_part, int(port_part), name="玩家")
    print(f"已加入 {client.room.room_name}，我是 #{client.peer_id}")

    stats = {"rtt": None, "states": 0, "last_tick": -1}
    stop = threading.Event()

    @client.on("data")
    def _(source, data, is_json):
        if not is_json:
            return
        try:
            msg = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return
        if msg.get("t") == "state":
            stats["states"] += 1
            stats["last_tick"] = msg["s"]["tick"]
        elif msg.get("t") == "pong":
            sent = msg.get("ts")
            if sent:
                # 用 time.time() 而不是 perf_counter()：后者是单调时钟，两个进程
                # 的起点各不相同，跨机器根本没法相减。
                # 代价是 Windows 上 time.time() 只有约 1ms 分辨率，所以同机回环
                # 测出来会在 0.0 / 1.0 ms 之间跳 —— 那是分辨率下限，不是真实值。
                # 真机局域网（1~10ms）测起来足够准。
                stats["rtt"] = (time.time() - sent) * 1000

    @client.on("close")
    def _():
        stop.set()

    def reporter():
        while not stop.wait(1.0):
            rtt = stats["rtt"]
            rtt_text = f"{rtt:.1f} ms" if rtt is not None else "—"
            print(
                f"  收到的状态数 {stats['states']:>4}  最新 tick {stats['last_tick']:>5}  "
                f"RTT {rtt_text}"
            )

    def pinger():
        # 每秒发一次心跳式探测。不这么做的话，屏幕上那个 RTT 只在玩家按键时
        # 刷新一次，之后就是冻结的旧值 —— 看着像实时数据，其实是骗人的。
        while not stop.wait(1.0):
            client.send_json({"t": "ping", "ts": time.time()})

    threading.Thread(target=reporter, daemon=True).start()
    threading.Thread(target=pinger, daemon=True).start()

    print("输入 up/down/left/right 移动，Ctrl+C 退出\n")
    try:
        while not stop.is_set():
            line = input().strip().lower()
            if line in ("up", "down", "left", "right"):
                client.send_json({"t": "input", "action": line})
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        stop.set()
        client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    if sys.argv[1] == "host":
        run_host()
    elif sys.argv[1] == "client":
        if len(sys.argv) > 2:
            run_client(sys.argv[2])
        else:
            rooms = scan(timeout=2.5)
            if not rooms:
                print("没扫到房间，请手动指定地址")
                sys.exit(1)
            run_client(rooms[0].direct_addr)
    else:
        print(__doc__)
        sys.exit(2)
