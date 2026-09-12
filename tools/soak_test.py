"""长时间稳定性压测。

单元测试证明不了"跑八小时会不会漏"这类问题，这个脚本补上那一块：

* 持续高频收发（默认 10 条/秒 × 全员）
* 客户端周期性进出，模拟真实的玩家来来走走
* 全程采样线程数、句柄数、内存，看有没有单调上涨的曲线
* 结束后断言：成员表清空、无幽灵成员、数据没错乱、线程和句柄回落

用法::

    python tools/soak_test.py            # 默认 20 分钟
    python tools/soak_test.py --minutes 60
    python tools/soak_test.py --minutes 5 --traffic-hz 30   # 快速过一遍

关于句柄数：这里**不手动调 gc.collect()**，因为真实进程也不会调。
所以数字反映的是"靠引用计数能回收多少"。如果一路单调上涨不回落，
说明代码里又出现了引用环，得去 Link._release_refs 看看。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import gc
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanlink import Client, Host, RelayServer  # noqa: E402

# ---------------------------------------------------------------- 资源采样


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


# 64 位下必须显式声明 argtypes：HANDLE 是指针宽度，不声明会被截成 32 位，
# 调用静默失败返回 0 —— 那样就会"测出"内存和句柄都是 0，白跑一场。
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.K32GetProcessMemoryInfo.argtypes = [
    wt.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wt.DWORD
]
_k32.K32GetProcessMemoryInfo.restype = wt.BOOL
_k32.GetProcessHandleCount.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
_k32.GetProcessHandleCount.restype = wt.BOOL


def rss_mb() -> float:
    """当前进程的工作集（MB）。测不到返回 -1。"""
    try:
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        ok = _k32.K32GetProcessMemoryInfo(
            _k32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        return counters.WorkingSetSize / 1024 / 1024 if ok else -1.0
    except Exception:
        return -1.0


def handle_count() -> int:
    """当前进程的句柄数。测不到返回 -1。"""
    try:
        n = wt.DWORD(0)
        ok = _k32.GetProcessHandleCount(_k32.GetCurrentProcess(), ctypes.byref(n))
        return n.value if ok else -1
    except Exception:
        return -1


def say(line: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {line}", flush=True)


# ---------------------------------------------------------------- 流量


class TrafficCounter:
    """收发计数 + 格式校验，用来证明长时间跑下来没有错乱。"""

    def __init__(self, node, label: str) -> None:
        self.label = label
        self.received = 0
        self.malformed = 0
        self._lock = threading.Lock()
        node.on("data", self._on_data)

    def _on_data(self, source, data, is_json) -> None:
        with self._lock:
            self.received += 1
            if not data.startswith(b"SOAK:"):
                self.malformed += 1


# ---------------------------------------------------------------- 主流程


def run(minutes: float, churn_every: float, sample_every: float, traffic_hz: float) -> int:
    say(f"压测开始，计划跑 {minutes:.0f} 分钟")

    relay = RelayServer("127.0.0.1", 0).start()
    host = Host("压测房", name="压测主机", port=0, advertise=False).start()
    host.attach_relay("127.0.0.1", relay.port, room_id="soak-room")
    say(f"中继 {relay.address} / 主机端口 {host.port}")

    host_traffic = TrafficCounter(host, "host")
    local_clients: list = []
    relay_clients: list = []
    stop = threading.Event()
    problems: list = []

    # 基线必须在客户端连进来之前采：那才是"什么都没跑"的状态。
    # 拿跑起来之后的数字当基线，收尾时就无从判断有没有回落。
    time.sleep(0.5)
    baseline = (rss_mb(), handle_count(), threading.active_count())
    say(f"基线（尚未接入任何客户端）：线程={baseline[2]} 句柄={baseline[1]} 内存={baseline[0]:.1f}MB")

    def spawn_local(idx: int) -> Client:
        c = Client.connect("127.0.0.1", host.port, name=f"本地{idx}")
        local_clients.append(c)
        return c

    def spawn_relay(idx: int) -> Client:
        c = Client.join_via_relay("127.0.0.1", relay.port, "soak-room", name=f"中继{idx}")
        relay_clients.append(c)
        return c

    for i in range(3):
        spawn_local(i)
        spawn_relay(i)
    say(f"初始成员：{host.player_count} 人（含主机）")

    def pump() -> None:
        n = 0
        while not stop.is_set():
            n += 1
            payload = f"SOAK:{n}:{'x' * 64}".encode()
            for c in list(local_clients) + list(relay_clients):
                if c.closed:
                    continue
                try:
                    if n % 2 == 0:
                        c.broadcast(payload)
                    else:
                        c.send(payload)
                except Exception as exc:  # 压测里任何异常都值得记下来
                    say(f"!! 发送异常 {exc!r}")
                    problems.append(f"发送异常：{exc!r}")
            try:
                host.broadcast(f"SOAK:host:{n}".encode())
            except Exception as exc:
                say(f"!! 主机广播异常 {exc!r}")
                problems.append(f"主机广播异常：{exc!r}")
            stop.wait(1.0 / traffic_hz)

    threading.Thread(target=pump, name="soak-pump", daemon=True).start()

    start = time.monotonic()
    last_churn = last_sample = start
    baseline = None
    churn_round = 0
    handles_series: list = []

    try:
        while time.monotonic() - start < minutes * 60:
            time.sleep(1.0)
            now = time.monotonic()

            if now - last_churn >= churn_every:
                last_churn = now
                churn_round += 1
                for pool, spawn in ((local_clients, spawn_local), (relay_clients, spawn_relay)):
                    if pool:
                        pool.pop(0).close()
                    spawn(1000 + churn_round * 10 + len(pool))
                time.sleep(2.0)
                expected = 1 + len(local_clients) + len(relay_clients)
                if host.player_count != expected:
                    msg = f"第 {churn_round} 轮换人后成员数不对：期望 {expected}，实际 {host.player_count}"
                    say(f"!! {msg}")
                    problems.append(msg)

            if now - last_sample >= sample_every:
                last_sample = now
                mem, hnd = rss_mb(), handle_count()
                threads_n = threading.active_count()
                handles_series.append(hnd)
                say(
                    f"{(now - start) / 60:5.1f}min  成员={host.player_count:>2}  "
                    f"线程={threads_n:>3}  句柄={hnd:>5}  内存={mem:6.1f}MB  "
                    f"收={host_traffic.received:>7}  坏={host_traffic.malformed}"
                )
        say("压测主体结束，开始收尾检查")
    except KeyboardInterrupt:
        say("被中断")
    finally:
        stop.set()
        time.sleep(1.0)
        say("")
        say("=" * 68)

        for c in local_clients + relay_clients:
            c.close()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and host.player_count > 1:
            time.sleep(0.3)
        if host.player_count != 1:
            ghosts = {p.peer_id: p.name for p in host.peers.values()}
            problems.append(f"客户端全断开后主机仍认为有 {host.player_count - 1} 个成员：{ghosts}")
            say(f"!! 幽灵成员：{ghosts}")
        else:
            say("成员表已清空（只剩主机自己）")

        host.close()
        relay.close()
        time.sleep(3.0)

        mem, hnd = rss_mb(), handle_count()
        threads_n = threading.active_count()
        say(f"收尾：线程={threads_n}  句柄={hnd}  内存={mem:.1f}MB")
        if baseline:
            bm, bh, bt = baseline
            say(f"基线：线程={bt}  句柄={bh}  内存={bm:.1f}MB")
            if threads_n > bt + 5:
                problems.append(f"线程没回收：基线 {bt}，现在 {threads_n}")
            if bh > 0 and hnd > bh + 60:
                problems.append(f"句柄收不回来：基线 {bh}，现在 {hnd}")
            if bm > 0 and mem > bm * 2.0 and mem - bm > 60:
                problems.append(f"内存疑似泄漏：基线 {bm:.1f}MB，现在 {mem:.1f}MB")

        # 句柄趋势：只看后半段，避开开头的预热抖动
        if len(handles_series) >= 6:
            tail = handles_series[len(handles_series) // 2:]
            drift = tail[-1] - tail[0]
            say(f"句柄趋势（后半段 {len(tail)} 个采样）：{tail[0]} -> {tail[-1]}（{drift:+d}）")
            if drift > max(50, tail[0] * 0.5):
                problems.append(
                    f"句柄在后半段仍单调上涨 {drift:+d} —— 大概率又出现引用环了，"
                    "检查 Link._release_refs"
                )

        say(f"流量：主机收到 {host_traffic.received} 条，畸形 {host_traffic.malformed} 条")
        if host_traffic.malformed:
            problems.append(f"有 {host_traffic.malformed} 条数据格式不对")
        if host_traffic.received == 0:
            problems.append("一条都没收到，压测没真正跑起来")

        say("=" * 68)
        if problems:
            say(f"发现 {len(problems)} 个问题：")
            for p in problems:
                say("  · " + p)
        else:
            say("压测通过：无泄漏、无幽灵成员、无数据损坏")
        say("=" * 68)

    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="lanlink 长时间稳定性压测")
    parser.add_argument("--minutes", type=float, default=20.0, help="跑多久（默认 20 分钟）")
    parser.add_argument("--churn-every", type=float, default=60.0, help="每隔多少秒换一批客户端")
    parser.add_argument("--sample-every", type=float, default=30.0, help="每隔多少秒采样一次")
    parser.add_argument("--traffic-hz", type=float, default=10.0, help="每秒发多少条")
    args = parser.parse_args()

    if not sys.platform.startswith("win"):
        print("提示：内存/句柄采样依赖 Windows API，其他平台上这两项会显示 -1。")

    return run(args.minutes, args.churn_every, args.sample_every, args.traffic_hz)


if __name__ == "__main__":
    sys.exit(main())
