"""生命周期与资源回收测试。

回归测试针对一个真实踩过的坑：``Link`` 的回调是闭包，捕获着 Host / 中继，
而 Host 又通过成员表反向持有 Link，形成引用环。引用成环的对象只能等循环 GC
来收 —— 在持续高频收发的进程里，长命对象会被晋升到 gen2，等不到回收，句柄数
就一直往上涨（实测每分钟几十个），看着像泄漏。

修法是断开时主动摘掉回调引用，让引用计数就能回收。这里把它钉住。
"""

import gc
import sys
import threading
import time
import unittest
import weakref
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanlink import Client, Host  # noqa: E402
from lanlink.link import Link, LinkClosed  # noqa: E402

TIMEOUT = 6.0


def wait_until(predicate, timeout=TIMEOUT, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TestLinkRelease(unittest.TestCase):
    """Link 关闭后必须把回调引用交出去。"""

    def test_close_clears_callbacks(self):
        host = Host("回调清理", port=0, advertise=False).start()
        try:
            client = Client.connect("127.0.0.1", host.port, name="c")
            link = client._link
            self.assertIsNotNone(link.on_frame)

            client.close()

            self.assertIsNone(link.on_frame, "on_frame 没摘掉，会跟 Host 形成引用环")
            self.assertIsNone(link.on_close, "on_close 没摘掉")
            self.assertIsNone(link.on_error, "on_error 没摘掉")
            self.assertIsNone(link._recv_thread)
            self.assertIsNone(link._hb_thread)
        finally:
            host.close()

    def test_host_side_link_releases_callbacks(self):
        host = Host("回调清理", port=0, advertise=False).start()
        client = Client.connect("127.0.0.1", host.port, name="c")
        peer_id = client.peer_id
        link = host._members[peer_id].link

        client.close()
        self.assertTrue(wait_until(lambda: peer_id not in host._members))

        self.assertTrue(
            wait_until(lambda: link.on_close is None),
            "主机侧 Link 关闭后没摘掉回调",
        )
        host.close()

    def test_on_close_still_fires_before_cleanup(self):
        """摘回调不能把 on_close 本身给弄丢了 —— 它得先跑完。"""
        host = Host("回调清理", port=0, advertise=False).start()
        try:
            client = Client.connect("127.0.0.1", host.port, name="c")
            fired = []
            client._link.on_close = lambda link, reason: fired.append(reason)
            client.close()
            self.assertEqual(len(fired), 1, "on_close 被漏掉了")
        finally:
            host.close()

    def test_restarting_a_closed_link_raises(self):
        """关掉的链路不能重新 start —— 那会跑到已关闭的 socket 上开线程。"""
        host = Host("重启保护", port=0, advertise=False).start()
        try:
            client = Client.connect("127.0.0.1", host.port, name="c")
            link = client._link
            link.close("测试用")
            with self.assertRaises(LinkClosed):
                link.start()
        finally:
            host.close()

    def test_handshake_timer_cleaned_up(self):
        host = Host("定时器清理", port=0, advertise=False).start()
        try:
            client = Client.connect("127.0.0.1", host.port, name="c")
            peer_id = client.peer_id
            link = host._members[peer_id].link
            self.assertIsNotNone(getattr(link, "_handshake_timer", None))

            client.close()
            self.assertTrue(wait_until(lambda: peer_id not in host._members))
            self.assertTrue(
                wait_until(lambda: getattr(link, "_handshake_timer", None) is None),
                "握手定时器没被释放",
            )
        finally:
            host.close()


class TestNoReferenceCycle(unittest.TestCase):
    """核心断言：客户端反复进出后，Link 不需要循环 GC 也能被回收。"""

    def test_links_are_collected_by_refcount_alone(self):
        host = Host("引用环", port=0, advertise=False).start()
        try:
            refs = []

            def connect_and_drop(i):
                # 放在函数里，确保客户端对象出了作用域就没人再持有它 ——
                # 否则循环变量会一直引用最后一个，那个 Link 是可达的，
                # GC 收不掉是理所当然，会误判成泄漏。
                client = Client.connect("127.0.0.1", host.port, name=f"c{i}")
                refs.append(weakref.ref(client._link))
                client.close()

            for i in range(8):
                connect_and_drop(i)

            # 等主机侧成员表清空，确保所有 Link 都已断开
            self.assertTrue(wait_until(lambda: host.player_count == 1))

            # 关键：这里不调 gc.collect()。如果还有引用环，弱引用就不会失效。
            deadline = time.monotonic() + 5.0
            alive = [r for r in refs if r() is not None]
            while alive and time.monotonic() < deadline:
                time.sleep(0.1)
                alive = [r for r in refs if r() is not None]

            if alive:
                # 退一步：至少确认循环 GC 能收掉（说明只是回收慢，不是真泄漏）
                gc.collect()
                time.sleep(0.2)
                still = [r for r in refs if r() is not None]
                self.assertEqual(
                    len(still), 0,
                    f"{len(alive)} 个 Link 连循环 GC 都收不掉，是真的泄漏了",
                )
                self.fail(
                    f"{len(alive)} 个 Link 需要循环 GC 才回收 —— 说明又出现引用环了。"
                    "检查是不是有回调没在 _release_refs 里摘掉。"
                )
        finally:
            host.close()

    def test_thread_count_returns_to_baseline(self):
        """每个连接会开收线程和心跳线程，断开后必须收干净。"""
        host = Host("线程回收", port=0, advertise=False).start()
        try:
            baseline = threading.active_count()
            for i in range(6):
                client = Client.connect("127.0.0.1", host.port, name=f"c{i}")
                client.send(b"x")
                client.close()
            self.assertTrue(wait_until(lambda: host.player_count == 1))
            self.assertTrue(
                wait_until(lambda: threading.active_count() <= baseline + 2, timeout=8.0),
                f"线程没回收：基线 {baseline}，现在 {threading.active_count()}",
            )
        finally:
            host.close()


class TestRepeatedJoinLeave(unittest.TestCase):
    """反复进出不能留下幽灵成员，也不能把成员表搞乱。"""

    def test_member_table_stays_consistent(self):
        host = Host("反复进出", port=0, advertise=False).start()
        try:
            for i in range(15):
                client = Client.connect("127.0.0.1", host.port, name=f"c{i}")
                self.assertEqual(host.player_count, 2, f"第 {i} 轮加入后人数不对")
                client.close()
                self.assertTrue(
                    wait_until(lambda: host.player_count == 1),
                    f"第 {i} 轮离开后留下幽灵成员: {host.peers}",
                )
            self.assertEqual(host.peers, {})
        finally:
            host.close()

    def test_player_ids_do_not_collide_across_churn(self):
        host = Host("id 不复用", port=0, advertise=False).start()
        try:
            seen = set()
            for i in range(10):
                client = Client.connect("127.0.0.1", host.port, name=f"c{i}")
                self.assertNotIn(client.peer_id, seen, "peer_id 被复用了")
                seen.add(client.peer_id)
                client.close()
                wait_until(lambda: host.player_count == 1)
        finally:
            host.close()

    def test_broadcast_still_works_after_churn(self):
        """折腾一通之后功能还得正常 —— 别修好了泄漏弄坏了收发。"""
        host = Host("折腾后可用", port=0, advertise=False).start()
        try:
            for i in range(5):
                c = Client.connect("127.0.0.1", host.port, name=f"old{i}")
                c.close()
                wait_until(lambda: host.player_count == 1)

            survivor = Client.connect("127.0.0.1", host.port, name="留下的人")
            received = []
            survivor.on("data", lambda s, d, j: received.append((s, d)))

            host.broadcast("还在吗".encode("utf-8"))
            self.assertTrue(wait_until(lambda: len(received) == 1), "折腾之后广播失灵了")
            self.assertEqual(received[0][1], "还在吗".encode("utf-8"))

            host_got = []
            host.on("data", lambda s, d, j: host_got.append(d))
            survivor.send("在的".encode("utf-8"))
            self.assertTrue(wait_until(lambda: len(host_got) == 1), "折腾之后上行失灵了")
            survivor.close()
        finally:
            host.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
