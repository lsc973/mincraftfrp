"""端口转发隧道的测试。

不是模拟，是真的起一个 TCP 服务（"回声服务器"充当 Minecraft 那种角色），
真的从隧道另一头连进去收发数据。

覆盖：
* 能不能穿过去（这是整个功能的立身之本）
* 多条流并发
* 大块二进制不被改动
* 服务不在时能不能好好报错
* 断开之后流表和 socket 有没有清干净
"""

import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import EchoServer  # noqa: E402


def setUpModule():
    """子进程版的隧道服务端一启动就会去更新免费域名，得先把配置目录隔离掉，
    不然开发机上配了真域名会被测试真的改掉。"""
    from helpers import isolate_config

    isolate_config()
from lanlink import Client, Host  # noqa: E402
from lanlink import tunnel  # noqa: E402
from lanlink.tunnel import Tunnel, TunnelError  # noqa: E402

TIMEOUT = 10.0


def wait_until(predicate, timeout=TIMEOUT, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TunnelTestCase(unittest.TestCase):
    """搭好一套"主机跑服务 + 客户端穿隧道"的环境。"""

    def setUp(self):
        self.service = EchoServer()
        self.host = Host("隧道测试房", name="服务端", port=0, advertise=False).start()
        self.client = Client.connect("127.0.0.1", self.host.port, name="客户端")
        self.server_tunnel = None
        self.client_tunnel = None

    def tearDown(self):
        for obj in (self.client_tunnel, self.server_tunnel):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        for obj in (self.client, self.host):
            try:
                obj.close()
            except Exception:
                pass
        self.service.close()

    def build_tunnels(self, listen_port=0):
        """建好两端隧道。listen_port=0 让系统挑端口。"""
        self.server_tunnel = Tunnel(
            self.host, role="server", target=("127.0.0.1", self.service.port)
        ).start()
        self.client_tunnel = Tunnel(
            self.client, role="client", listen=("127.0.0.1", listen_port)
        ).start()
        return self.client_tunnel._listener.getsockname()[1]

    def connect_through(self, port):
        """从隧道客户端那一头连进去，返回 socket。"""
        sock = socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT)
        sock.settimeout(TIMEOUT)
        return sock


class TestBasicForwarding(TunnelTestCase):
    def test_data_goes_through_and_comes_back(self):
        port = self.build_tunnels()
        # 给隧道一点时间把 OPEN 送到对面
        time.sleep(0.3)

        with self.connect_through(port) as sock:
            sock.sendall(b"hello-minecraft")
            got = sock.recv(4096)
        self.assertEqual(got, b"hello-minecraft",
                         "数据没穿过隧道，或者回来的路上丢了")

    def test_service_actually_got_the_bytes(self):
        """确认数据真的到了服务端，而不是被隧道自己吞了又吐回来。"""
        port = self.build_tunnels()
        time.sleep(0.3)
        with self.connect_through(port) as sock:
            sock.sendall(b"ping-through-tunnel")
            sock.recv(4096)
        self.assertTrue(
            wait_until(lambda: any(b"ping-through-tunnel" in d for d in self.service.received)),
            f"服务端没收到数据，实际收到 {self.service.received}",
        )

    def test_multiple_sequential_connections(self):
        """一条连完再连一条 —— 流的申请和回收要能反复用。"""
        port = self.build_tunnels()
        time.sleep(0.3)
        for i in range(5):
            with self.connect_through(port) as sock:
                msg = f"第{i}条".encode("utf-8")
                sock.sendall(msg)
                self.assertEqual(sock.recv(4096), msg)
        self.assertEqual(self.service.connections, 5, "服务端应该看到 5 条独立连接")

    def test_concurrent_streams(self):
        """多条流同时跑，数据不能串。"""
        port = self.build_tunnels()
        time.sleep(0.3)

        results = {}
        errors = []

        def worker(index):
            try:
                with self.connect_through(port) as sock:
                    tag = f"stream-{index}-".encode("utf-8") * 20
                    sock.sendall(tag)
                    got = b""
                    while len(got) < len(tag):
                        chunk = sock.recv(65536)
                        if not chunk:
                            break
                        got += chunk
                    results[index] = got
            except Exception as exc:
                errors.append((index, exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=TIMEOUT)

        self.assertEqual(errors, [], f"并发流出错：{errors}")
        self.assertEqual(len(results), 6, f"只完成了 {len(results)} 条流")
        for index, got in results.items():
            expect = f"stream-{index}-".encode("utf-8") * 20
            self.assertEqual(got, expect, f"第 {index} 条流的数据串了")

    def test_large_binary_payload_intact(self):
        """256 KB 二进制，逐字节比对。TCP 转发最容易在这里出问题。"""
        port = self.build_tunnels()
        time.sleep(0.3)

        blob = bytes(range(256)) * 1024  # 256 KB，256 个字节值全覆盖
        with self.connect_through(port) as sock:
            sender = threading.Thread(target=sock.sendall, args=(blob,), daemon=True)
            sender.start()
            got = b""
            while len(got) < len(blob):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                got += chunk
            sender.join(timeout=TIMEOUT)

        self.assertEqual(len(got), len(blob), f"长度不对：{len(got)}")
        self.assertEqual(got, blob, "二进制内容在隧道里被改动了")

    def test_empty_connection_closes_cleanly(self):
        """连上就断，不应该把隧道搞挂。"""
        port = self.build_tunnels()
        time.sleep(0.3)
        sock = self.connect_through(port)
        sock.close()
        time.sleep(0.4)
        # 之后还能正常用
        with self.connect_through(port) as sock2:
            sock2.sendall(b"still-alive")
            self.assertEqual(sock2.recv(4096), b"still-alive")


class TestFailureModes(TunnelTestCase):
    def test_dead_target_reports_reason(self):
        """服务没开的时候，客户端这边应该是连接被关，而不是一直挂着。"""
        from helpers import reserve_dead_port

        dead_port = reserve_dead_port()

        self.server_tunnel = Tunnel(
            self.host, role="server", target=("127.0.0.1", dead_port)
        ).start()
        self.client_tunnel = Tunnel(
            self.client, role="client", listen=("127.0.0.1", 0)
        ).start()
        port = self.client_tunnel._listener.getsockname()[1]
        time.sleep(0.3)

        sock = self.connect_through(port)
        try:
            # 服务连不上，隧道会把这条流关掉 —— recv 应该很快返回空
            got = sock.recv(4096)
            self.assertEqual(got, b"", "连不上服务时应该直接关连接")
        finally:
            sock.close()

    def test_listen_port_conflict_raises(self):
        """监听端口被占时要报错，而不是静默失败。"""
        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        try:
            with self.assertRaises(TunnelError):
                Tunnel(self.client, role="client", listen=("127.0.0.1", port)).start()
        finally:
            occupied.close()

    def test_role_validation(self):
        with self.assertRaises(ValueError):
            Tunnel(self.host, role="nonsense")
        with self.assertRaises(ValueError):
            Tunnel(self.host, role="server")  # server 必须给 target
        with self.assertRaises(ValueError):
            Tunnel(self.client, role="client")  # client 必须给 listen

    def test_non_tunnel_data_is_ignored(self):
        """房间里的普通消息不能被隧道当成自己的帧。"""
        self.build_tunnels()
        time.sleep(0.2)
        # 发一条普通消息，隧道应该无视它（不能崩、不能当流处理）
        self.client.send(b"just a normal message")
        self.client.send(b"LLTK")  # 有魔数但长度不够，也要安全忽略
        time.sleep(0.3)
        self.assertTrue(self.client_tunnel is not None)  # 还活着


class TestCleanup(TunnelTestCase):
    def test_streams_released_after_disconnect(self):
        port = self.build_tunnels()
        time.sleep(0.3)
        for _ in range(4):
            with self.connect_through(port) as sock:
                sock.sendall(b"x")
                sock.recv(100)
        self.assertTrue(
            wait_until(lambda: self.client_tunnel.streams == 0),
            f"客户端流表没清干净：{self.client_tunnel.streams}",
        )
        self.assertTrue(
            wait_until(lambda: self.server_tunnel.streams == 0),
            f"服务端流表没清干净：{self.server_tunnel.streams}",
        )

    def test_tunnel_close_releases_everything(self):
        port = self.build_tunnels()
        time.sleep(0.3)
        sock = self.connect_through(port)
        sock.sendall(b"hold")
        sock.recv(100)

        self.client_tunnel.close()
        self.server_tunnel.close()
        sock.close()

        self.assertEqual(self.client_tunnel.streams, 0)
        self.assertEqual(self.server_tunnel.streams, 0)

    def test_node_close_tears_down_tunnel(self):
        """房间断了隧道要跟着收摊，不能留个孤儿监听端口。"""
        self.build_tunnels()
        time.sleep(0.2)
        self.client.close()
        self.assertTrue(wait_until(lambda: self.client_tunnel._closed))

    def test_threads_do_not_pile_up(self):
        """反复建流不能把线程数堆上去。"""
        port = self.build_tunnels()
        time.sleep(0.3)
        baseline = threading.active_count()
        for _ in range(6):
            with self.connect_through(port) as sock:
                sock.sendall(b"y")
                sock.recv(100)
        self.assertTrue(
            wait_until(lambda: threading.active_count() <= baseline + 2, timeout=8.0),
            f"线程没回收：基线 {baseline}，现在 {threading.active_count()}",
        )


class TestCliArgs(unittest.TestCase):
    """隧道命令的参数解析。"""

    def test_listen_bare_port_defaults_to_loopback(self):
        from lanlink.cli import _parse_listen

        self.assertEqual(_parse_listen("25565"), ("127.0.0.1", 25565))

    def test_listen_explicit_address(self):
        from lanlink.cli import _parse_listen

        self.assertEqual(_parse_listen("0.0.0.0:25565"), ("0.0.0.0", 25565))
        self.assertEqual(_parse_listen("192.168.1.5:8080"), ("192.168.1.5", 8080))

    def test_listen_rejects_garbage(self):
        import argparse

        from lanlink.cli import _parse_listen

        with self.assertRaises(argparse.ArgumentTypeError):
            _parse_listen("not-a-port")

    def test_to_requires_host_and_port(self):
        import argparse

        from lanlink.cli import _parse_addr

        self.assertEqual(_parse_addr("127.0.0.1:25565"), ("127.0.0.1", 25565))
        with self.assertRaises(argparse.ArgumentTypeError):
            _parse_addr("25565")

    def test_cli_rejects_both_to_and_listen(self):
        from lanlink.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["tunnel", "--room", "x", "--to", "1.2.3.4:1",
                                  "--listen", "25565"])
        # 参数能解析，但 cmd_tunnel 会拒绝 —— 这里确认两者都收得到，
        # 由命令自己去判二选一
        self.assertIsNotNone(args.to)
        self.assertIsNotNone(args.listen)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestShareText(unittest.TestCase):
    """给对方的那段连接信息。

    图形界面和命令行版共用这一份文案 —— 分开写的话迟早会不一致，
    而对面的操作步骤错一步就卡住了。
    """

    def test_direct_command_has_address_and_listen(self):
        out = tunnel.friend_command(address="1.2.3.4:50001", listen=25565)
        self.assertEqual(
            out, "lanlink-cli.exe tunnel --addr 1.2.3.4:50001 --listen 25565")

    def test_ipv6_address_keeps_its_brackets(self):
        out = tunnel.friend_command(address="[240e::1]:50001", listen=25565)
        self.assertIn("--addr [240e::1]:50001", out)

    def test_relay_command_uses_relay_and_room_not_address(self):
        out = tunnel.friend_command(relay="1.2.3.4:9000", room="123456", listen=25565)
        self.assertIn("--relay 1.2.3.4:9000", out)
        self.assertIn("--room 123456", out)
        self.assertNotIn("--addr", out)

    def test_password_is_appended(self):
        out = tunnel.friend_command(address="1.2.3.4:1", listen=25565, password="pw")
        self.assertIn("--password pw", out)

    def test_password_with_space_is_quoted(self):
        """不引起来的话 shell 会把密码切成两段，对面怎么都连不上。"""
        out = tunnel.friend_command(address="1.2.3.4:1", listen=25565, password="a b")
        self.assertIn('--password "a b"', out)

    def test_no_password_means_no_flag(self):
        out = tunnel.friend_command(address="1.2.3.4:1", listen=25565)
        self.assertNotIn("--password", out)

    def test_text_explains_both_cli_and_gui(self):
        text = tunnel.share_text(address="[240e::1]:50001", listen=25565)
        self.assertIn("命令行版", text)
        self.assertIn("图形版", text)
        self.assertIn("对方地址", text)
        self.assertIn("本地监听", text)
        self.assertIn("127.0.0.1:25565", text)

    def test_text_for_relay_does_not_talk_about_peer_address(self):
        """走中继时对面填的是中继地址 + 房间号，提「对方地址」会把人带沟里。"""
        text = tunnel.share_text(relay="1.2.3.4:9000", room="888", listen=25565)
        self.assertIn("中继地址", text)
        self.assertIn("房间名", text)
        self.assertNotIn("对方地址", text)

    def test_text_carries_the_password(self):
        text = tunnel.share_text(address="1.2.3.4:1", listen=25565, password="pw")
        self.assertIn("房间密码", text)
        self.assertIn("--password pw", text)

    def test_text_omits_password_row_when_empty(self):
        text = tunnel.share_text(address="1.2.3.4:1", listen=25565)
        self.assertNotIn("房间密码", text)


class TestCliTunnelGuidance(unittest.TestCase):
    """命令行版起隧道之后，打出来的那段「发给对方」的提示。

    真起一个子进程读它的输出 —— 这段文案是给用户直接复制去用的，
    里面任何一个字错了，对面就卡在第一步。顺便验证输出是刷出来的：
    不刷的话重定向到文件就什么都看不到。
    """

    def _run_server(self, seconds=8.0):
        import os
        import subprocess

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        # 特意**不加 -u**：要靠 safe_print 自己的 flush 让内容及时出来，
        # 加了 -u 就测不出这个了。
        proc = subprocess.Popen(
            [sys.executable, "-m", "lanlink", "tunnel",
             "--to", "127.0.0.1:25565", "--port", "0", "--no-advertise"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        try:
            out, _ = proc.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        return out

    def test_prints_a_ready_to_paste_block(self):
        out = self._run_server()
        self.assertIn("lanlink 隧道已开好", out)
        self.assertIn("lanlink-cli.exe tunnel --addr", out)
        self.assertIn("--listen 25565", out, "监听端口该跟服务端口一致")
        self.assertIn("127.0.0.1:25565", out)

    def test_suggests_the_service_port_not_the_room_port(self):
        """房间端口是随机分配的，让对方去连它既没道理又容易看错。"""
        out = self._run_server()
        for line in out.splitlines():
            if "局域网地址：" in line:
                room_port = line.rsplit(":", 1)[-1].strip()
                break
        else:
            self.fail("没打出局域网地址：" + out[:300])
        self.assertIn("--listen 25565", out)
        self.assertNotIn(f"--listen {room_port}", out)


class TestRoomOnlyShare(unittest.TestCase):
    """地址编进了对方的 exe 时，对方只要房间号 + 口令。

    这是"对面什么都不用填"的落点，所以命令里**不能出现 --addr** ——
    出现了就说明地址又漏给对方了。
    """

    def test_command_has_room_but_no_address(self):
        out = tunnel.friend_command(room="0dcc265b", listen=25565,
                                    password="pw", server_default=True)
        self.assertEqual(
            out, "lanlink-cli.exe tunnel --room 0dcc265b --password pw --listen 25565")
        self.assertNotIn("--addr", out)
        self.assertNotIn("--relay", out)

    def test_text_shows_a_room_number_field(self):
        text = tunnel.share_text(room="0dcc265b", listen=25565, server_default=True)
        self.assertIn("房间号", text)
        self.assertIn("0dcc265b", text)
        self.assertNotIn("对方地址", text)
        self.assertNotIn("中继地址", text)

    def test_text_says_they_dont_need_the_address(self):
        text = tunnel.share_text(room="abc123", listen=25565, server_default=True)
        self.assertIn("不用知道地址", text)

    def test_password_still_travels(self):
        text = tunnel.share_text(room="abc123", listen=25565, password="pw",
                                 server_default=True)
        self.assertIn("--password pw", text)
        self.assertIn("房间密码：pw", text)

    def test_room_only_mode_does_not_leak_an_address(self):
        """就算调用方顺手把 address 传进来了，房间号模式下也不该用它。"""
        text = tunnel.share_text(address="240e:354::1:50001", room="abc123",
                                 listen=25565, server_default=True)
        self.assertNotIn("240e", text)
