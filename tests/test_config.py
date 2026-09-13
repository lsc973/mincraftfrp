"""配置文件的读写。

这个模块存在的唯一理由是**避免互相覆盖**：DDNS 和服务器地址存在同一个
文件的不同段里，如果各写各的，A 读出来改一个键再写回去，正好把 B 刚存的
东西冲掉。所以测试重点就在"只动自己那一段"。
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanlink import config  # noqa: E402


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("LANLINK_CONFIG_DIR")
        os.environ["LANLINK_CONFIG_DIR"] = self._tmp.name
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("LANLINK_CONFIG_DIR", None)
        else:
            os.environ["LANLINK_CONFIG_DIR"] = self._old
        self._tmp.cleanup()


class TestBasics(ConfigTestCase):
    def test_dir_override(self):
        self.assertEqual(config.config_dir(), self.dir)

    def test_path_is_inside_the_dir(self):
        self.assertEqual(config.config_path(), self.dir / "config.json")

    def test_read_without_a_file(self):
        self.assertEqual(config.read(), {})

    def test_round_trip(self):
        config.write({"a": 1, "b": {"c": 2}})
        self.assertEqual(config.read(), {"a": 1, "b": {"c": 2}})

    def test_creates_the_directory(self):
        nested = self.dir / "深" / "更深"
        os.environ["LANLINK_CONFIG_DIR"] = str(nested)
        config.write({"a": 1})
        self.assertTrue((nested / "config.json").exists())


class TestBrokenFiles(ConfigTestCase):
    """配置坏了不该让程序起不来，最差就当没配过。"""

    def _write_raw(self, text):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "config.json").write_text(text, encoding="utf-8")

    def test_corrupt_json(self):
        self._write_raw("{不是 JSON")
        self.assertEqual(config.read(), {})

    def test_json_but_not_an_object(self):
        self._write_raw("[1, 2, 3]")
        self.assertEqual(config.read(), {})

    def test_empty_file(self):
        self._write_raw("")
        self.assertEqual(config.read(), {})

    def test_get_section_on_a_non_dict_section(self):
        self._write_raw(json.dumps({"ddns": "不是对象"}))
        self.assertIsNone(config.get_section("ddns"))


class TestSections(ConfigTestCase):
    def test_set_and_get(self):
        config.set_section("server", {"address": "a.b:1"})
        self.assertEqual(config.get_section("server"), {"address": "a.b:1"})

    def test_missing_section(self):
        self.assertIsNone(config.get_section("没有这段"))

    def test_setting_one_section_keeps_the_other(self):
        """这就是把配置读写集中到这里的全部理由。"""
        config.set_section("ddns", {"hostname": "me.dynv6.net", "token": "t"})
        config.set_section("server", {"address": "a.b:1"})
        self.assertEqual(config.get_section("ddns"),
                         {"hostname": "me.dynv6.net", "token": "t"},
                         "写 server 段把 ddns 段冲掉了")

    def test_overwriting_a_section(self):
        config.set_section("server", {"address": "old:1"})
        config.set_section("server", {"address": "new:2"})
        self.assertEqual(config.get_section("server"), {"address": "new:2"})

    def test_none_deletes_the_section(self):
        config.set_section("server", {"address": "a.b:1"})
        config.set_section("server", None)
        self.assertIsNone(config.get_section("server"))

    def test_none_keeps_the_other_section(self):
        config.set_section("ddns", {"hostname": "me.dynv6.net"})
        config.set_section("server", {"address": "a.b:1"})
        config.set_section("server", None)
        self.assertIsNotNone(config.get_section("ddns"))

    def test_delete_section(self):
        config.set_section("server", {"address": "a.b:1"})
        self.assertTrue(config.delete_section("server"))
        self.assertFalse(config.delete_section("server"), "本来就没有，该返回 False")

    def test_delete_missing_section_does_not_create_a_file(self):
        config.delete_section("server")
        self.assertFalse(config.config_path().exists(), "删一段不存在的配置不该建出文件来")


class TestSharedFileWithDdns(ConfigTestCase):
    """真拿 ddns 模块一起过一遍 —— 两边共用一个文件是这次重构的重点。"""

    def test_ddns_and_server_coexist(self):
        from lanlink import ddns

        ddns.save(ddns.DdnsConfig("dynv6", "me.dynv6.net", "tok123"))
        config.set_section("server", {"address": "me.dynv6.net:50001"})

        self.assertIsNotNone(ddns.load(), "写 server 段把 ddns 配置弄丢了")
        self.assertEqual(config.get_section("server"),
                         {"address": "me.dynv6.net:50001"})

        ddns.clear()
        self.assertIsNone(ddns.load())
        self.assertIsNotNone(config.get_section("server"),
                             "清 ddns 把 server 段一起清掉了")


if __name__ == "__main__":
    unittest.main()
