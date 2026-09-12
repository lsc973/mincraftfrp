"""端到端测试：真的起 socket，真的连，真的发数据。

覆盖三条路：局域网直连、公网中继、房间发现。
"""

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanlink import Client, Host, RelayServer, scan  # noqa: E402
from lanlink.discovery import Scanner  # noqa: E402
from lanlink.relay import list_relay_rooms  # noqa: E402

TIMEOUT = 6.0


class _Collector:
    """把事件收进队列，方便测试里阻塞等某个东西出现。"""

    def __init__(self, node):
        self.data = []
        self.joins = []
        self.leaves = []
        self.errors = []
        self._lock = threading.Lock()
        self._event = threading.Event()
        node.on("data", self._on_data)
        node.on("peer_join", self._on_join)
        node.on("peer_leave", self._on_leave)
        node.on("error", self._on_error)

    def _touch(self):
        self._event.set()
        self._event.clear()

    def _on_data(self, source, data, is_json):
        with self._lock:
            self.data.append((source, data, is_json))
        self._touch()

    def _on_join(self, info):
        with self._lock:
            self.joins.append(info)
        self._touch()

    def _on_leave(self, info, reason):
        with self._lock:
            self.leaves.append((info, reason))
        self._touch()

    def _on_error(self, exc):
        with self._lock:
            self.errors.append(exc)

    def wait_data(self, count=1, timeout=TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.data) >= count:
                    return list(self.data)
            self._event.wait(0.05)
        with self._lock:
            return list(self.data)

    def wait_join(self, count=1, timeout=TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.joins) >= count:
                    return list(self.joins)
            self._event.wait(0.05)
        with self._lock:
            return list(self.joins)

    def wait_leave(self, count=1, timeout=TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.leaves) >= count:
                    return list(self.leaves)
            self._event.wait(0.05)
        with self._lock:
            return list(self.leaves)


class TestDirect(unittest.TestCase):
    """局域网直连。advertise=False，测试不想往网络上喷广播包。"""

    def setUp(self):
        self.host = Host("测试房间", name="房主", port=0, advertise=False).start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.host.close()

    def _join(self, name="玩家", password="", **kwargs):
        client = Client.connect(
            "127.0.0.1", self.host.port, name=name, password=password, **kwargs
        )
        self.clients.append(client)
        return client

    # -------------------------------------------------------- 基本

    def test_join_gets_identity_and_room(self):
        client = self._join("小明")
        self.assertGreater(client.peer_id, 0)
        self.assertIsNotNone(client.room)
        self.assertEqual(client.room.room_name, "测试房间")
        self.assertEqual(client.room.host_name, "房主")
        self.assertEqual(client.room.players, 2)  # 主机 + 小明

    def test_host_sees_join(self):
        watcher = _Collector(self.host)
        client = self._join("小明")
        joins = watcher.wait_join()
        self.assertEqual(len(joins), 1)
        self.assertEqual(joins[0].name, "小明")
        self.assertEqual(joins[0].peer_id, client.peer_id)
        self.assertFalse(joins[0].is_relay)

    def test_peer_ids_are_unique(self):
        a = self._join("A")
        b = self._join("B")
        self.assertNotEqual(a.peer_id, b.peer_id)

    def test_late_joiner_sees_existing_peers(self):
        a = self._join("A")
        b = self._join("B")
        self.assertIn(a.peer_id, b.peers)
        self.assertEqual(b.peers[a.peer_id].name, "A")

    def test_existing_client_notified_of_new_peer(self):
        a = self._join("A")
        watcher = _Collector(a)
        b = self._join("B")
        joins = watcher.wait_join()
        self.assertEqual([p.name for p in joins], ["B"])

    # -------------------------------------------------------- 数据

    def test_client_to_host(self):
        watcher = _Collector(self.host)
        client = self._join("小明")
        client.send("给主机的话".encode("utf-8"))
        data = watcher.wait_data()
        self.assertEqual(len(data), 1)
        source, payload, is_json = data[0]
        self.assertEqual(source, client.peer_id)
        self.assertEqual(payload, "给主机的话".encode("utf-8"))
        self.assertFalse(is_json)

    def test_host_to_client(self):
        client = self._join("小明")
        watcher = _Collector(client)
        self.host.send_to(client.peer_id, "主机的话".encode("utf-8"))
        data = watcher.wait_data()
        self.assertEqual(data[0][0], 0)  # 来源是主机
        self.assertEqual(data[0][1], "主机的话".encode("utf-8"))

    def test_client_broadcast_reaches_others_not_sender(self):
        a = self._join("A")
        b = self._join("B")
        wa, wb = _Collector(a), _Collector(b)
        wh = _Collector(self.host)
        a.broadcast("大家好".encode("utf-8"))
        self.assertEqual([d[1] for d in wb.wait_data()], ["大家好".encode("utf-8")])
        self.assertEqual(wb.data[0][0], a.peer_id)  # 来源标的是 A
        self.assertEqual([d[1] for d in wh.wait_data()], ["大家好".encode("utf-8")])
        time.sleep(0.3)
        self.assertEqual(wa.data, [])  # 自己不该收到自己的广播

    def test_private_message_only_reaches_target(self):
        a = self._join("A")
        b = self._join("B")
        c = self._join("C")
        wb, wc = _Collector(b), _Collector(c)
        a.send_to(b.peer_id, "悄悄话".encode("utf-8"))
        self.assertEqual([d[1] for d in wb.wait_data()], ["悄悄话".encode("utf-8")])
        time.sleep(0.3)
        self.assertEqual(wc.data, [])

    def test_host_broadcast(self):
        a = self._join("A")
        b = self._join("B")
        wa, wb = _Collector(a), _Collector(b)
        sent = self.host.broadcast("公告".encode("utf-8"))
        self.assertEqual(sent, 2)
        self.assertEqual([d[1] for d in wa.wait_data()], ["公告".encode("utf-8")])
        self.assertEqual([d[1] for d in wb.wait_data()], ["公告".encode("utf-8")])

    def test_json_roundtrip(self):
        watcher = _Collector(self.host)
        client = self._join("小明")
        client.send_json({"动作": "移动", "x": 3, "y": -7})
        data = watcher.wait_data()
        source, payload, is_json = data[0]
        self.assertTrue(is_json)
        import json

        self.assertEqual(json.loads(payload.decode("utf-8")), {"动作": "移动", "x": 3, "y": -7})

    def test_binary_payload_intact(self):
        """二进制数据不能在传输中被改动 —— 联机同步全靠这个。"""
        blob = bytes(range(256)) * 8
        watcher = _Collector(self.host)
        client = self._join("小明")
        client.send(blob)
        data = watcher.wait_data()
        self.assertEqual(data[0][1], blob)

    def test_large_payload(self):
        blob = bytes(512 * 1024)
        watcher = _Collector(self.host)
        client = self._join("小明")
        client.send(blob)
        data = watcher.wait_data(timeout=15.0)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0][1], blob)

    def test_send_to_nonexistent_peer_is_safe(self):
        a = self._join("A")
        b = self._join("B")
        wb = _Collector(b)
        self.assertFalse(a.send_to(999999, "查无此人".encode("utf-8")))
        time.sleep(0.3)
        self.assertEqual(wb.data, [])

    # -------------------------------------------------------- 离场

    def test_leave_notification(self):
        watcher = _Collector(self.host)
        client = self._join("小明")
        watcher.wait_join()
        client.close()
        leaves = watcher.wait_leave()
        self.assertEqual(leaves[0][0].name, "小明")

    def test_peer_leave_reaches_other_clients(self):
        a = self._join("A")
        b = self._join("B")
        wa = _Collector(a)
        b.close()
        leaves = wa.wait_leave()
        self.assertEqual(leaves[0][0].peer_id, b.peer_id)

    def test_host_close_disconnects_clients(self):
        client = self._join("小明")
        closed = threading.Event()
        client.on("close", lambda: closed.set())
        self.host.close()
        self.assertTrue(closed.wait(TIMEOUT), "主机关闭后客户端应收到 close")

    # -------------------------------------------------------- 准入

    def test_wrong_password_rejected(self):
        self.host.password = "s3cret"
        with self.assertRaises(ConnectionRefusedError) as ctx:
            self._join("坏人", password="猜的")
        self.assertIn("密码", str(ctx.exception))

    def test_correct_password_accepted(self):
        self.host.password = "s3cret"
        client = self._join("自己人", password="s3cret")
        self.assertGreater(client.peer_id, 0)

    def test_room_full(self):
        self.host.max_players = 2  # 主机自己占一个，还能进一个
        self._join("第一个")
        with self.assertRaises(ConnectionRefusedError) as ctx:
            self._join("第二个")
        self.assertIn("人数已满", str(ctx.exception))

    def test_kick(self):
        client = self._join("倒霉蛋")
        closed = threading.Event()
        client.on("close", lambda: closed.set())
        self.host.kick(client.peer_id, "请出去")
        self.assertTrue(closed.wait(TIMEOUT))
        # 主机这边的成员表也要清干净
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline and client.peer_id in self.host.peers:
            time.sleep(0.05)
        self.assertNotIn(client.peer_id, self.host.peers)

    def test_connect_to_dead_port_fails_fast(self):
        with self.assertRaises(OSError):
            Client.connect("127.0.0.1", 1, timeout=2.0)


class TestRelay(unittest.TestCase):
    """公网中继：主机挂上去，客户端从"外面"连进来。"""

    def setUp(self):
        self.relay = RelayServer("127.0.0.1", 0).start()
        self.host = Host("中继房间", name="房主", port=0, advertise=False).start()
        self.host.attach_relay("127.0.0.1", self.relay.port, room_id="relay-test")
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.host.close()
        self.relay.close()

    def _join(self, name="玩家", password="", room="relay-test"):
        client = Client.join_via_relay(
            "127.0.0.1", self.relay.port, room, name=name, password=password
        )
        self.clients.append(client)
        return client

    def test_client_gets_welcome_and_id(self):
        client = self._join("远端玩家")
        self.assertGreater(client.peer_id, 0)
        self.assertIsNotNone(client.room)
        self.assertEqual(client.room.room_name, "中继房间")

    def test_host_sees_relay_peer_as_normal_peer(self):
        watcher = _Collector(self.host)
        client = self._join("远端玩家")
        joins = watcher.wait_join()
        self.assertEqual(joins[0].name, "远端玩家")
        self.assertTrue(joins[0].is_relay)
        self.assertEqual(joins[0].peer_id, client.peer_id)

    def test_relay_peer_id_distinct_from_local(self):
        a = Client.connect("127.0.0.1", self.host.port, name="本地玩家")
        self.clients.append(a)
        b = self._join("远端玩家")
        self.assertNotEqual(a.peer_id, b.peer_id)

    def test_data_client_to_host_through_relay(self):
        watcher = _Collector(self.host)
        client = self._join("远端玩家")
        client.send("穿过中继".encode("utf-8"))
        data = watcher.wait_data()
        self.assertEqual(data[0][0], client.peer_id)
        self.assertEqual(data[0][1], "穿过中继".encode("utf-8"))

    def test_host_to_relay_client(self):
        client = self._join("远端玩家")
        watcher = _Collector(client)
        self.host.send_to(client.peer_id, "给远端的你".encode("utf-8"))
        data = watcher.wait_data()
        self.assertEqual(data[0][0], 0)
        self.assertEqual(data[0][1], "给远端的你".encode("utf-8"))

    def test_two_relay_clients_talk(self):
        a = self._join("远端A")
        b = self._join("远端B")
        wa, wb = _Collector(a), _Collector(b)
        a.broadcast("中继里的广播".encode("utf-8"))
        self.assertEqual([d[1] for d in wb.wait_data()], ["中继里的广播".encode("utf-8")])
        self.assertEqual(wb.data[0][0], a.peer_id)
        time.sleep(0.3)
        self.assertEqual(wa.data, [])

    def test_local_and_relay_clients_mixed(self):
        """本地直连的和中继进来的在同一个房间里，互相要能看见、能说话。"""
        local = Client.connect("127.0.0.1", self.host.port, name="本地")
        self.clients.append(local)
        # 得先订阅再让远端进来，否则 peer_join 事件在我们装好监听前就飞过去了
        wl = _Collector(local)
        remote = self._join("远端")
        wr = _Collector(remote)

        self.assertEqual(wl.wait_join()[0].name, "远端")
        self.assertIn(local.peer_id, remote.peers)

        local.broadcast("本地喊话".encode("utf-8"))
        self.assertEqual([d[1] for d in wr.wait_data()], ["本地喊话".encode("utf-8")])
        remote.broadcast("远端回话".encode("utf-8"))
        self.assertEqual([d[1] for d in wl.wait_data()], ["远端回话".encode("utf-8")])

    def test_relay_client_leave_notifies_host(self):
        watcher = _Collector(self.host)
        client = self._join("要走的人")
        watcher.wait_join()
        client.close()
        leaves = watcher.wait_leave()
        self.assertEqual(leaves[0][0].name, "要走的人")

    def test_host_close_drops_relay_clients(self):
        client = self._join("远端")
        closed = threading.Event()
        client.on("close", lambda: closed.set())
        self.host.close()
        self.assertTrue(closed.wait(TIMEOUT), "主机关闭后中继客户端应收到关闭")

    def test_relay_password_enforced_by_host(self):
        host = Host("有密码的房间", port=0, advertise=False, password="pw").start()
        host.attach_relay("127.0.0.1", self.relay.port, room_id="locked")
        try:
            with self.assertRaises(ConnectionRefusedError):
                Client.join_via_relay("127.0.0.1", self.relay.port, "locked", password="错的")
            ok = Client.join_via_relay("127.0.0.1", self.relay.port, "locked", password="pw")
            ok.close()
        finally:
            host.close()

    def test_duplicate_room_id_rejected(self):
        other = Host("抢房间的", port=0, advertise=False).start()
        try:
            with self.assertRaises(ConnectionRefusedError):
                other.attach_relay("127.0.0.1", self.relay.port, room_id="relay-test")
        finally:
            other.close()

    def test_join_nonexistent_room(self):
        with self.assertRaises(ConnectionRefusedError) as ctx:
            Client.join_via_relay("127.0.0.1", self.relay.port, "根本没这房间")
        self.assertIn("不存在", str(ctx.exception))

    def test_relay_token(self):
        relay = RelayServer("127.0.0.1", 0, token="k3y").start()
        host = Host("要口令的房间", port=0, advertise=False).start()
        try:
            with self.assertRaises(ConnectionRefusedError):
                host.attach_relay("127.0.0.1", relay.port, room_id="t1", token="错的")
            host.attach_relay("127.0.0.1", relay.port, room_id="t1", token="k3y")
            self.assertTrue(host.relay.connected)
        finally:
            host.close()
            relay.close()

    def test_list_relay_rooms(self):
        self._join("某人")
        rooms = list_relay_rooms("127.0.0.1", self.relay.port)
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0].room_id, "relay-test")
        self.assertEqual(rooms[0].clients, 1)
        self.assertEqual(rooms[0].name, "中继房间")

    def test_relay_stats(self):
        self._join("甲")
        self._join("乙")
        stats = self.relay.stats()
        self.assertEqual(stats["rooms"], 1)
        self.assertEqual(stats["clients"], 2)


class TestDiscovery(unittest.TestCase):
    """房间发现。用独立端口，避免跟真实运行的实例串味。"""

    PORT = 47901

    def test_scan_finds_host(self):
        host = Host("能被找到的房间", name="房主", port=0, discovery_port=self.PORT).start()
        try:
            rooms = scan(timeout=3.0, port=self.PORT)
            found = [r for r in rooms if r.room_id == host.room_id]
            self.assertEqual(len(found), 1, f"没扫到房间，扫到的是 {rooms}")
            info = found[0]
            self.assertEqual(info.room_name, "能被找到的房间")
            self.assertEqual(info.host_name, "房主")
            self.assertEqual(info.port, host.port)
            self.assertEqual(info.players, 1)
            self.assertTrue(info.address)
        finally:
            host.close()

    def test_room_info_reflects_player_count(self):
        host = Host("人数测试", port=0, discovery_port=self.PORT).start()
        client = None
        try:
            client = Client.connect("127.0.0.1", host.port, name="访客")
            deadline = time.monotonic() + 6.0
            players = 0
            scanner = Scanner(port=self.PORT, ttl=4.0)
            scanner.start()
            try:
                while time.monotonic() < deadline:
                    info = scanner.find(host.room_id)
                    if info and info.players == 2:
                        players = info.players
                        break
                    time.sleep(0.2)
            finally:
                scanner.stop()
            self.assertEqual(players, 2, "广播里的在线人数没跟着更新")
        finally:
            if client:
                client.close()
            host.close()

    def test_password_flag_advertised(self):
        host = Host("上锁的", port=0, discovery_port=self.PORT, password="pw").start()
        try:
            rooms = scan(timeout=3.0, port=self.PORT)
            found = [r for r in rooms if r.room_id == host.room_id]
            self.assertTrue(found and found[0].has_password)
        finally:
            host.close()

    def test_stopped_host_disappears(self):
        host = Host("马上就没", port=0, discovery_port=self.PORT).start()
        rooms = scan(timeout=3.0, port=self.PORT)
        self.assertTrue(any(r.room_id == host.room_id for r in rooms))
        host.close()

        scanner = Scanner(port=self.PORT, ttl=2.0)
        scanner.start()
        try:
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if scanner.find(host.room_id) is None:
                    return
                time.sleep(0.3)
            self.fail("主机关闭后房间仍留在列表里")
        finally:
            scanner.stop()

    def test_scan_on_empty_network_returns_list(self):
        self.assertIsInstance(scan(timeout=0.5, port=47902), list)

    def test_probe_reply_beats_broadcast_interval(self):
        """探测要立刻有回音，不能干等主机的下一次周期广播。

        回归测试：以前扫描器和主机绑同一个端口，回包会被两个同端口
        socket 抢，导致短超时的扫描时灵时不灵。
        """
        host = Host("即时响应", port=0, discovery_port=47903).start()
        try:
            found = scan(timeout=0.5, port=47903)
            self.assertEqual(
                [r.room_id for r in found], [host.room_id],
                "0.5 秒内就该靠探测拿到房间，不该等周期广播",
            )
        finally:
            host.close()

    def test_host_and_scanner_share_one_machine(self):
        """同一台机器上开房 + 扫描必须稳定 —— 这是最常见的自测姿势。"""
        host = Host("本机自测", port=0, discovery_port=47904).start()
        try:
            hits = sum(
                1 for _ in range(5)
                if any(r.room_id == host.room_id for r in scan(timeout=0.4, port=47904))
            )
            self.assertEqual(hits, 5, f"5 次扫描只成功了 {hits} 次")
        finally:
            host.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
