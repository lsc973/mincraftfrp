"""地址解析。

看着琐碎，但 CLI、图形界面、默认服务器地址全走这一份 —— 这里错一点，
表现是"某个地方 IPv6 连不上"，而且很难定位到是解析的问题。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanlink.link import format_addr, parse_addr, parse_listen_addr  # noqa: E402


class TestParseAddr(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_addr("192.168.1.10:50001"), ("192.168.1.10", 50001))

    def test_hostname(self):
        self.assertEqual(parse_addr("me.dynv6.net:50001"), ("me.dynv6.net", 50001))

    def test_ipv6_needs_brackets(self):
        self.assertEqual(parse_addr("[240e:354::1]:50001"), ("240e:354::1", 50001))

    def test_bare_ipv6_is_rejected(self):
        """不加方括号的话 rpartition 会把地址切碎，必须拒掉而不是猜。"""
        self.assertIsNone(parse_addr("240e:354::1:50001"))

    def test_rejects_garbage(self):
        for bad in ("", "   ", "没有冒号", "host:abc", "[没闭合:123", "[]:80"):
            self.assertIsNone(parse_addr(bad), f"{bad!r} 应该被拒绝")


class TestPortRange(unittest.TestCase):
    """端口范围要在这里挡住。

    不挡的话 ``host:99999`` 会一路通过，直到 socket 那层才炸，报的是
    "指定的端口无效" 之类 —— 用户完全看不出是哪个框填错了。
    """

    def test_normal_ports(self):
        self.assertEqual(parse_addr("a.b:1"), ("a.b", 1))
        self.assertEqual(parse_addr("a.b:65535"), ("a.b", 65535))

    def test_too_large(self):
        for bad in ("a.b:65536", "a.b:99999", "[240e::1]:70000"):
            self.assertIsNone(parse_addr(bad), f"{bad!r} 应该被拒绝")

    def test_negative(self):
        self.assertIsNone(parse_addr("a.b:-1"))

    def test_zero_is_not_a_valid_destination(self):
        """连到端口 0 没有任何意义。"""
        self.assertIsNone(parse_addr("a.b:0"))
        self.assertIsNone(parse_addr("[240e::1]:0"))


class TestParseListenAddr(unittest.TestCase):
    def test_bare_port_binds_loopback(self):
        """只写端口时不监听 0.0.0.0 —— 免得顺手把服务开放给整个网段。"""
        self.assertEqual(parse_listen_addr("25565"), ("127.0.0.1", 25565))

    def test_explicit_host(self):
        self.assertEqual(parse_listen_addr("0.0.0.0:25565"), ("0.0.0.0", 25565))

    def test_ipv6(self):
        self.assertEqual(parse_listen_addr("[::]:25565"), ("::", 25565))

    def test_zero_is_allowed_here(self):
        """监听端口 0 = 让系统挑一个，是正经用法（隧道客户端就这么用）。

        连接目标不能是 0，监听可以 —— 这两件事混成一条规则的话，
        要么连不上，要么"随机端口"这个功能直接没了。
        """
        self.assertEqual(parse_listen_addr("0"), ("127.0.0.1", 0))
        self.assertEqual(parse_listen_addr("127.0.0.1:0"), ("127.0.0.1", 0))
        self.assertEqual(parse_listen_addr("[::]:0"), ("::", 0))

    def test_still_rejects_out_of_range(self):
        for bad in ("65536", "99999", "127.0.0.1:70000", "abc"):
            self.assertIsNone(parse_listen_addr(bad), f"{bad!r} 应该被拒绝")

    def test_empty(self):
        self.assertIsNone(parse_listen_addr(""))


class TestFormatAddr(unittest.TestCase):
    def test_ipv4(self):
        self.assertEqual(format_addr("192.168.1.10", 50001), "192.168.1.10:50001")

    def test_hostname(self):
        self.assertEqual(format_addr("me.dynv6.net", 50001), "me.dynv6.net:50001")

    def test_ipv6_gets_brackets(self):
        """不加方括号的话，这个字符串再解析回来就错了。"""
        self.assertEqual(format_addr("240e:354::1", 50001), "[240e:354::1]:50001")

    def test_round_trip(self):
        for host, port in (("1.2.3.4", 80), ("me.dynv6.net", 50001),
                           ("240e:354:311:a200::1", 50001)):
            text = format_addr(host, port)
            self.assertEqual(parse_addr(text), (host, port), f"{text} 转不回来")


if __name__ == "__main__":
    unittest.main()
