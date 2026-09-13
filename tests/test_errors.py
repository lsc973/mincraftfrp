"""错误信息质量的测试。

背景：用户报告隧道建立失败，界面上只显示 ``timeout: timed out`` ——
那是 socket 的原始消息，既不说哪一步失败，也不说该去查什么。

这里把"错误信息必须能照着排查"变成可断言的要求：
* 要带上目标和端口
* 要说明是超时还是被拒绝（两者原因完全不同）
* 要给出排查方向
"""

import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanlink import Client, Host  # noqa: E402

#: RFC 5737 的 TEST-NET-1，保证不可路由 —— 拿来稳定地制造连接失败。
UNROUTABLE = "192.0.2.1"


def setUpModule():
    """这个模块里有起隧道服务端的用例，它一启动就会去更新免费域名 ——
    先把配置目录隔离掉，别让测试碰用户真实的域名。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from helpers import isolate_config

    isolate_config()


def reserve_dead_port() -> int:
    """占一个端口再放掉，得到一个没人监听的端口号。"""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class TestHostConnectErrors(unittest.TestCase):
    def test_unreachable_host_error_is_actionable(self):
        try:
            Client.connect(UNROUTABLE, 9000, timeout=2.0)
            self.fail("本该连不上")
        except OSError as exc:
            message = str(exc)
            self.assertIn(f"{UNROUTABLE}:9000", message, "错误里没写清连的是哪个地址")
            self.assertIn("排查方向", message, "没告诉用户该去查什么")
            self.assertIn("主机", message, "没说是连主机时出错的")
            self.assertNotEqual(message.strip(), "timed out", "还是裸的 socket 消息")

    def test_refused_is_distinguished_from_timeout(self):
        """端口没人监听 vs 连接超时 —— 原因完全不同，不能混为一谈。"""
        port = reserve_dead_port()
        try:
            Client.connect("127.0.0.1", port, timeout=3.0)
            self.fail("本该被拒绝")
        except ConnectionRefusedError as exc:
            message = str(exc)
            self.assertIn("拒绝连接", message)
            self.assertIn("没有服务在监听", message)
            self.assertIn("排查方向", message)
        except ConnectionError as exc:
            # 有的系统在没有监听时也会直接超时，那也算合理，但消息仍要可读
            self.assertIn("排查方向", str(exc))


class TestRelayConnectErrors(unittest.TestCase):
    def test_unreachable_relay_error_is_actionable(self):
        host = Host("错误信息测试", port=0, advertise=False).start()
        try:
            try:
                host.attach_relay(UNROUTABLE, 9000, room_id="x", timeout=2.0)
                self.fail("本该连不上中继")
            except OSError as exc:
                message = str(exc)
                self.assertIn("中继", message, "没说是连中继时出错的")
                self.assertIn(f"{UNROUTABLE}:9000", message)
                self.assertIn("排查方向", message)
                self.assertIn("安全组", message, "云端部署最容易漏的就是安全组")
        finally:
            host.close()

    def test_client_relay_join_error_is_actionable(self):
        try:
            Client.join_via_relay(UNROUTABLE, 9000, "room", timeout=2.0)
            self.fail("本该连不上中继")
        except OSError as exc:
            message = str(exc)
            self.assertIn("中继", message)
            self.assertIn("排查方向", message)


class TestTunnelFailureCleanup(unittest.TestCase):
    """建隧道失败时不能留下已经起了一半的节点。

    用户看不到那个残留的 Host：房间还开着、广播还在发、端口还占着，
    再点一次启动又会起一个，越堆越多。
    """

    def test_failed_relay_attach_closes_the_host(self):
        import lanlink.gui.tunnel_page as tp

        created = []
        real_host = tp.Host

        class RecordingHost(real_host):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)

        tp.Host = RecordingHost
        try:
            page = _make_page()
            page.mode.set("server")
            page._toggle_mode()
            page.vars["target"].set("127.0.0.1:1")
            page.vars["room"].set("失败清理测试")
            page.vars["relay"].set(f"{UNROUTABLE}:9000")
            page._start()

            _pump_until(page, lambda: bool(page.log.text.get("1.0", "end").strip().endswith("。"))
                        and "失败" in page.log.text.get("1.0", "end"), timeout=30.0)
        finally:
            tp.Host = real_host

        self.assertTrue(created, "Host 根本没被创建，测试没意义")
        for host in created:
            self.assertTrue(
                host.closed,
                f"隧道建立失败后 Host 还开着（房间 {getattr(host, 'room_name', '?')}）—— 节点泄漏了",
            )


def _make_page():
    """建一个 app，返回它的隧道页。GUI 环境的失跳由 gui_helpers 处理。"""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gui_helpers import make_app, pump

    app = make_app()
    app.deiconify()
    pump(app, 0.3)
    page = app.pages["tunnel"]
    app.show_page("tunnel")
    pump(app, 0.3)
    page._test_app = app
    return page


def _pump_until(page, predicate, timeout=20.0):
    import time

    from gui_helpers import pump

    app = page._test_app
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pump(app, 0.1)
        if predicate():
            return True
    return predicate()


if __name__ == "__main__":
    unittest.main(verbosity=2)
