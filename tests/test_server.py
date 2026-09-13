"""默认服务器 —— "对面要连哪儿"。

这个功能存在的意义只有一个：**让对面只填房间号和口令**。

服务器有两种，行为完全不同：

* ``direct`` —— 对面直接连到你机器上，要求你家能被外面连到
* ``relay``  —— 两边都连到一台公网机器上，由它牵线，你自己不需要能被访问

**分不清这两种是要出事的**：同一个地址，直连模式去连中继端口只会得到一个
莫名其妙的失败，而用户完全看不出问题在哪。所以模式是显式存的，不靠猜。
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

    def bake(self, address, label="", mode="direct", token=""):
        """假装这个 exe 是带着某个地址打出来的。"""
        return mock.patch.multiple(_build_defaults, DEFAULT_SERVER=address,
                                   LABEL=label, SERVER_MODE=mode, RELAY_TOKEN=token)


class TestResolution(ServerTestCase):
    def test_nothing_configured(self):
        with self.bake(""):
            self.assertIsNone(server.resolve())
            self.assertIsNone(server.load())
            self.assertIsNone(server.baked())

    def test_baked_only(self):
        with self.bake("me.dynv6.net:50001"):
            got = server.resolve()
            self.assertEqual((got.host, got.port), ("me.dynv6.net", 50001))
            self.assertFalse(got.is_relay)

    def test_config_only(self):
        with self.bake(""):
            server.save("other.example.com:9000")
            self.assertEqual(server.resolve().host, "other.example.com")

    def test_config_wins_over_baked(self):
        """换了机器/换了域名时不用重新打包，改配置就行。"""
        with self.bake("old.example.com:1111"):
            server.save("new.example.com:2222")
            self.assertEqual(server.resolve().host, "new.example.com")

    def test_clearing_config_falls_back_to_baked(self):
        with self.bake("baked.example.com:1111"):
            server.save("temp.example.com:2222")
            self.assertTrue(server.clear())
            self.assertEqual(server.resolve().host, "baked.example.com",
                             "配置删了之后该退回 exe 里编的那个")

    def test_clear_without_config(self):
        with self.bake(""):
            self.assertFalse(server.clear())

    def test_ipv6_address(self):
        with self.bake("[240e:354::1]:50001"):
            self.assertEqual(server.resolve().host, "240e:354::1")

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


class TestRelayMode(ServerTestCase):
    """中继和直连必须是两种东西，不能混。"""

    def test_baked_relay(self):
        with self.bake("1.2.3.4:9000", mode="relay", token="pw"):
            got = server.resolve()
            self.assertTrue(got.is_relay)
            self.assertEqual((got.host, got.port), ("1.2.3.4", 9000))
            self.assertEqual(got.token, "pw")

    def test_baked_direct_has_no_token(self):
        with self.bake("1.2.3.4:9000", mode="direct", token="pw"):
            self.assertEqual(server.resolve().token, "",
                             "直连模式不该带中继口令")

    def test_save_and_load_relay(self):
        with self.bake(""):
            endpoint = server.save("1.2.3.4:9000", mode="relay", token="pw")
            self.assertTrue(endpoint.is_relay)
            loaded = server.load()
            self.assertTrue(loaded.is_relay)
            self.assertEqual(loaded.token, "pw")

    def test_save_direct_clears_the_token(self):
        """从"中继"改成"直连"之后，上一个中继的口令不能留着。"""
        with self.bake(""):
            server.save("1.2.3.4:9000", mode="relay", token="pw")
            server.save("a.b:1", mode="direct")
            self.assertEqual(server.load().token, "")

    def test_stored_section_has_the_mode(self):
        with self.bake(""):
            server.save("1.2.3.4:9000", mode="relay", token="pw")
            section = config.get_section(server.SECTION)
            self.assertEqual(section["mode"], "relay")
            self.assertEqual(section["token"], "pw")

    def test_direct_mode_is_the_default(self):
        with self.bake(""):
            self.assertFalse(server.save("a.b:1").is_relay)

    def test_missing_mode_in_config_means_direct(self):
        """老版本的配置文件里没有 mode 这一项，得能读。"""
        with self.bake(""):
            config.set_section(server.SECTION, {"address": "a.b:1"})
            self.assertFalse(server.load().is_relay)

    def test_garbage_mode_in_config_falls_back_to_direct(self):
        """配置被人手改坏了，宁可当直连也别让程序起不来。"""
        with self.bake(""):
            config.set_section(server.SECTION, {"address": "a.b:1", "mode": "乱写的"})
            self.assertFalse(server.load().is_relay)

    def test_garbage_mode_baked_falls_back_to_direct(self):
        with self.bake("a.b:1", mode="乱写的"):
            self.assertFalse(server.resolve().is_relay)

    def test_save_rejects_unknown_mode(self):
        with self.bake(""):
            with self.assertRaises(ValueError) as ctx:
                server.save("a.b:1", mode="中继")
            self.assertIn("relay", str(ctx.exception))

    def test_config_relay_wins_over_baked_direct(self):
        """家里连不进来了，改配置切到中继 —— 不用重新打包。"""
        with self.bake("my.dynv6.net:50001", mode="direct"):
            server.save("1.2.3.4:9000", mode="relay", token="pw")
            self.assertTrue(server.resolve().is_relay)


class TestSave(ServerTestCase):
    def test_save_and_load(self):
        with self.bake(""):
            self.assertEqual(server.save("a.example.com:1234").host, "a.example.com")
            self.assertEqual(server.load().port, 1234)

    def test_save_normalizes_ipv6_brackets(self):
        """存的时候统一成带方括号的写法，读回来才不会解析错。"""
        with self.bake(""):
            server.save("[240e::1]:50001")
            self.assertEqual(config.get_section(server.SECTION)["address"],
                             "[240e::1]:50001")

    def test_rejects_bad_address(self):
        with self.bake(""):
            for bad in ("没有冒号", "", "host:不是数字", "[没闭合:123", "host:99999"):
                with self.assertRaises(ValueError, msg=f"{bad!r} 应该被拒绝"):
                    server.save(bad)

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

    def test_says_which_mode(self):
        with self.bake("1.2.3.4:9000", mode="relay"):
            self.assertIn("中继", server.describe())
        with self.bake("1.2.3.4:9000", mode="direct"):
            self.assertIn("直连", server.describe())

    def test_never_prints_the_relay_token(self):
        """令牌等同于中继的使用权，别摆在界面上。"""
        with self.bake("1.2.3.4:9000", mode="relay", token="secret-pw"):
            self.assertNotIn("secret-pw", server.describe())

    def test_explains_what_to_do_when_unset(self):
        with self.bake(""):
            text = server.describe()
            self.assertIn("lanlink server set", text)
            self.assertIn("--relay", text, "该告诉用户家里连不进来时还有中继这条路")

    def test_shows_the_label(self):
        with self.bake("a.example.com:1", label="给小明的"):
            self.assertIn("给小明的", server.describe())


if __name__ == "__main__":
    unittest.main()
