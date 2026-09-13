"""默认服务器地址 —— "对面要连哪儿"。

这个功能存在的意义只有一个：**让对面只填房间号和口令**。所以测试的重点是
"地址有没有被正确解析出来、优先级对不对"，以及"编进 exe 的那个不能被
配置文件的残留悄悄盖掉"。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanlink import _build_defaults, config, server  # noqa: E402


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("LANLINK_CONFIG_DIR")
        os.environ["LANLINK_CONFIG_DIR"] = self._tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("LANLINK_CONFIG_DIR", None)
        else:
            os.environ["LANLINK_CONFIG_DIR"] = self._old
        self._tmp.cleanup()

    def bake(self, address, label=""):
        """假装这个 exe 是带着某个地址打出来的。"""
        return mock.patch.multiple(_build_defaults,
                                   DEFAULT_SERVER=address, LABEL=label)


class TestResolution(ServerTestCase):
    def test_nothing_configured(self):
        with self.bake(""):
            self.assertIsNone(server.resolve())
            self.assertIsNone(server.load())
            self.assertIsNone(server.baked())

    def test_baked_only(self):
        with self.bake("me.dynv6.net:50001"):
            self.assertEqual(server.resolve(), ("me.dynv6.net", 50001))

    def test_config_only(self):
        with self.bake(""):
            server.save("other.example.com:9000")
            self.assertEqual(server.resolve(), ("other.example.com", 9000))

    def test_config_wins_over_baked(self):
        """换了机器/换了域名时不用重新打包，改配置就行。"""
        with self.bake("old.example.com:1111"):
            server.save("new.example.com:2222")
            self.assertEqual(server.resolve(), ("new.example.com", 2222))

    def test_clearing_config_falls_back_to_baked(self):
        with self.bake("baked.example.com:1111"):
            server.save("temp.example.com:2222")
            self.assertTrue(server.clear())
            self.assertEqual(server.resolve(), ("baked.example.com", 1111),
                             "配置删了之后该退回 exe 里编的那个")

    def test_clear_without_config(self):
        with self.bake(""):
            self.assertFalse(server.clear())

    def test_ipv6_address(self):
        with self.bake("[240e:354::1]:50001"):
            self.assertEqual(server.resolve(), ("240e:354::1", 50001))

    def test_broken_baked_value_is_ignored(self):
        """编进去的东西坏了就当没有，别让程序崩在这儿。"""
        with self.bake("这不是地址"):
            self.assertIsNone(server.baked())
            self.assertIsNone(server.resolve())

    def test_broken_config_value_is_ignored(self):
        with self.bake(""):
            config.set_section(server.SECTION, {"address": "坏掉的"})
            self.assertIsNone(server.load())
            self.assertIsNone(server.resolve())


class TestSave(ServerTestCase):
    def test_save_and_load(self):
        with self.bake(""):
            self.assertEqual(server.save("a.example.com:1234"), ("a.example.com", 1234))
            self.assertEqual(server.load(), ("a.example.com", 1234))

    def test_save_normalizes_ipv6_brackets(self):
        """存的时候统一成带方括号的写法，读回来才不会解析错。"""
        with self.bake(""):
            server.save("240e:354::1:50001" if False else "[240e::1]:50001")
            stored = config.get_section(server.SECTION)["address"]
            self.assertEqual(stored, "[240e::1]:50001")

    def test_rejects_bad_address(self):
        with self.bake(""):
            for bad in ("没有冒号", "", "host:不是数字", "[没闭合:123"):
                with self.assertRaises(ValueError, msg=f"{bad!r} 应该被拒绝"):
                    server.save(bad)

    def test_rejects_out_of_range_port(self):
        with self.bake(""):
            with self.assertRaises(ValueError):
                server.save("host:99999")

    def test_save_keeps_the_ddns_section(self):
        """跟免费域名共用一个配置文件，别互相冲掉。"""
        from lanlink import ddns

        with self.bake(""):
            ddns.save(ddns.DdnsConfig("dynv6", "me.dynv6.net", "tok"))
            server.save("me.dynv6.net:50001")
            self.assertIsNotNone(ddns.load(), "写服务器地址把域名配置冲掉了")


class TestDescribe(ServerTestCase):
    def test_says_where_it_came_from(self):
        with self.bake("baked.example.com:1111"):
            self.assertIn("打包时编进", server.describe())

        with self.bake(""):
            server.save("cfg.example.com:2222")
            self.assertIn("配置文件", server.describe())

    def test_explains_what_to_do_when_unset(self):
        with self.bake(""):
            text = server.describe()
            self.assertIn("lanlink server set", text)

    def test_shows_the_label(self):
        with self.bake("a.example.com:1", label="给小明的"):
            self.assertIn("给小明的", server.describe())


if __name__ == "__main__":
    unittest.main()
