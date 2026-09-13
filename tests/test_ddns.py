"""免费动态域名的测试。

**全部走 mock，一个包都不往外发** —— 测试不该依赖网络，更不该真去改
用户注册的域名。
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanlink import ddns  # noqa: E402

V6 = "240e:354:311:a200:f587:f2f5:4fb3:bae2"


class TempConfig(unittest.TestCase):
    """把配置目录指到临时目录，别碰用户真实的配置文件。"""

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


class TestProviderURLs(unittest.TestCase):
    def test_dynv6_url(self):
        url = ddns.PROVIDERS["dynv6"].update_url("me.dynv6.net", "tok", V6)
        self.assertTrue(url.startswith("https://dynv6.com/api/update?"))
        self.assertIn("hostname=me.dynv6.net", url)
        self.assertIn("token=tok", url)

    def test_duckdns_strips_the_suffix(self):
        """duckdns 的 domains 参数只要子域名，带后缀会被判错。"""
        url = ddns.PROVIDERS["duckdns"].update_url("me.duckdns.org", "tok", V6)
        self.assertIn("domains=me", url)
        self.assertNotIn("duckdns.org", url.split("?")[1])

    def test_both_use_https(self):
        """token 在查询串里，走 http 等于把它明文广播出去。"""
        for provider in ddns.PROVIDERS.values():
            url = provider.update_url("a.example.com", "tok", V6)
            self.assertTrue(url.startswith("https://"), f"{provider.key} 没走 https")

    def test_ipv6_gets_url_encoded(self):
        """地址里的冒号不编码的话，有些服务端会解析错。"""
        url = ddns.PROVIDERS["dynv6"].update_url("me.dynv6.net", "tok", V6)
        self.assertNotIn("ipv6=240e:", url)


class TestInterpret(unittest.TestCase):
    def test_dynv6_success(self):
        ddns.PROVIDERS["dynv6"].interpret(200, "addresses updated")

    def test_dynv6_unchanged_is_success(self):
        """地址本来就没变，服务商回 unchanged —— 这不是失败。"""
        ddns.PROVIDERS["dynv6"].interpret(200, "addresses unchanged")

    def test_dynv6_bad_token_is_actionable(self):
        """这条是拿真 API 试出来的：坏 token 回 401 + invalid authentication token。"""
        with self.assertRaises(ddns.DdnsError) as ctx:
            ddns.PROVIDERS["dynv6"].interpret(401, "invalid authentication token")
        self.assertIn("令牌", str(ctx.exception))
        self.assertIn("dynv6", str(ctx.exception).lower())

    def test_dynv6_does_not_depend_on_the_wording_of_success(self):
        """成功与否只看状态码，不看正文里出现了什么词。

        官方脚本用的是 `curl -fsS`（-f = 状态码非 2xx 就失败），正文根本不解析。
        早先这里要求正文含有 "updated"，那是个没验证过的假设 —— 服务商换个
        措辞就会把成功报成失败，用户白折腾半天。
        """
        for body in ("addresses updated", "addresses unchanged",
                     "OK", "", "whatever dynv6 feels like saying"):
            ddns.PROVIDERS["dynv6"].interpret(200, body)

    def test_dynv6_still_catches_an_error_smuggled_into_a_200(self):
        """保险：万一服务商 200 里塞了拒绝的话，不能当成功。"""
        with self.assertRaises(ddns.DdnsError):
            ddns.PROVIDERS["dynv6"].interpret(200, "invalid zone name")

    def test_duckdns_ok(self):
        ddns.PROVIDERS["duckdns"].interpret(200, "OK\n")

    def test_duckdns_ko_is_actionable(self):
        """DuckDNS 成功失败都是 200，只能看正文 —— 跟 dynv6 正好相反。"""
        with self.assertRaises(ddns.DdnsError) as ctx:
            ddns.PROVIDERS["duckdns"].interpret(200, "KO")
        self.assertIn("token", str(ctx.exception).lower())

    def test_duckdns_unexpected_body_is_an_error(self):
        with self.assertRaises(ddns.DdnsError):
            ddns.PROVIDERS["duckdns"].interpret(200, "<html>proxy error</html>")

    def test_error_body_is_truncated(self):
        """服务商抽风回一坨 HTML 时，别把整个页面塞进界面。"""
        with self.assertRaises(ddns.DdnsError) as ctx:
            ddns.PROVIDERS["dynv6"].interpret(500, "<html>" + "x" * 5000)
        self.assertLess(len(str(ctx.exception)), 300)


class TestConfig(TempConfig):
    def test_round_trip(self):
        ddns.save(ddns.DdnsConfig("dynv6", "me.dynv6.net", "tok123"))
        loaded = ddns.load()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.provider, "dynv6")
        self.assertEqual(loaded.hostname, "me.dynv6.net")
        self.assertEqual(loaded.token, "tok123")

    def test_load_without_config(self):
        self.assertIsNone(ddns.load())

    def test_load_ignores_incomplete_config(self):
        path = ddns.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ddns": {"provider": "dynv6", "hostname": "a.b"}}),
                        encoding="utf-8")
        self.assertIsNone(ddns.load(), "缺 token 不该当成配好了")

    def test_load_ignores_unknown_provider(self):
        path = ddns.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ddns": {
            "provider": "nope", "hostname": "a.b", "token": "t"}}), encoding="utf-8")
        self.assertIsNone(ddns.load())

    def test_load_survives_corrupt_json(self):
        path = ddns.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{不是 JSON", encoding="utf-8")
        self.assertIsNone(ddns.load())

    def test_save_rejects_bad_hostname(self):
        with self.assertRaises(ddns.DdnsError):
            ddns.save(ddns.DdnsConfig("dynv6", "没有点的名字", "tok123"))

    def test_save_rejects_empty_token(self):
        with self.assertRaises(ddns.DdnsError):
            ddns.save(ddns.DdnsConfig("dynv6", "me.dynv6.net", "   "))

    def test_save_rejects_unknown_provider(self):
        with self.assertRaises(ddns.DdnsError):
            ddns.save(ddns.DdnsConfig("nope", "me.dynv6.net", "tok"))

    def test_save_preserves_other_sections(self):
        """以后 config.json 里会有别的东西，别一写就把它们冲掉。"""
        path = ddns.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"别的设置": 42}), encoding="utf-8")
        ddns.save(ddns.DdnsConfig("dynv6", "me.dynv6.net", "tok123"))
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["别的设置"], 42)

    def test_clear(self):
        ddns.save(ddns.DdnsConfig("dynv6", "me.dynv6.net", "tok123"))
        self.assertTrue(ddns.clear())
        self.assertIsNone(ddns.load())
        self.assertFalse(ddns.clear(), "本来就没有，该返回 False")

    def test_public_view_hides_token(self):
        config = ddns.DdnsConfig("dynv6", "me.dynv6.net", "tok123")
        self.assertNotIn("tok123", json.dumps(config.public()))

    def test_config_dir_override(self):
        self.assertEqual(ddns.config_dir(), Path(self._tmp.name))


class TestPublish(TempConfig):
    def setUp(self):
        super().setUp()
        ddns.save(ddns.DdnsConfig("dynv6", "me.dynv6.net", "tok123"))

    def test_publish_hits_the_api_and_returns_hostname(self):
        with mock.patch.object(ddns, "_get", return_value=(200, "addresses updated")) as get:
            hostname = ddns.publish(V6)
        self.assertEqual(hostname, "me.dynv6.net")
        url = get.call_args[0][0]
        self.assertIn("me.dynv6.net", url)

    def test_publish_without_config_explains_what_to_do(self):
        ddns.clear()
        with self.assertRaises(ddns.DdnsError) as ctx:
            ddns.publish(V6)
        self.assertIn("ddns setup", str(ctx.exception))

    def test_publish_without_address(self):
        with self.assertRaises(ddns.DdnsError):
            ddns.publish("")

    def test_network_failure_is_wrapped(self):
        with mock.patch.object(ddns, "_get", side_effect=ddns.DdnsError("连不上")):
            with self.assertRaises(ddns.DdnsError):
                ddns.publish(V6)

    # ---- token 绝对不能漏进异常消息 ----

    def test_token_never_leaks_through_a_failing_api(self):
        """日志会被截图、会被贴出来问问题，token 漏出去等于域名被人接管。"""
        with mock.patch.object(
            ddns, "_get",
            return_value=(500, "your token tok123 is invalid, full url: ?token=tok123"),
        ):
            with self.assertRaises(ddns.DdnsError) as ctx:
                ddns.publish(V6)
        self.assertNotIn("tok123", str(ctx.exception))

    def test_token_never_leaks_when_the_request_itself_fails(self):
        with mock.patch.object(ddns, "_get", side_effect=OSError("connection to tok123 refused")):
            with self.assertRaises(ddns.DdnsError):
                ddns.publish(V6)


class TestRedact(unittest.TestCase):
    def test_removes_the_secret(self):
        self.assertEqual(ddns._redact("token=tok123 bad", "tok123"), "token=*** bad")

    def test_leaves_short_secrets_alone(self):
        """太短的串到处都是，抹了反而把正常文字打碎。"""
        self.assertEqual(ddns._redact("abc", "abc"), "abc")

    def test_no_secret_no_change(self):
        self.assertEqual(ddns._redact("hello", ""), "hello")


class TestResolve(unittest.TestCase):
    def test_resolve_returns_address(self):
        fake = [(2, 1, 6, "", (V6, 0, 0, 0))]
        with mock.patch.object(ddns.socket, "getaddrinfo", return_value=fake):
            self.assertEqual(ddns.resolve("me.dynv6.net"), V6)

    def test_resolve_strips_scope_id(self):
        fake = [(2, 1, 6, "", ("fe80::1%eth0", 0, 0, 0))]
        with mock.patch.object(ddns.socket, "getaddrinfo", return_value=fake):
            self.assertEqual(ddns.resolve("me.dynv6.net"), "fe80::1")

    def test_resolve_returns_none_when_missing(self):
        with mock.patch.object(ddns.socket, "getaddrinfo",
                               side_effect=ddns.socket.gaierror()):
            self.assertIsNone(ddns.resolve("nope.dynv6.net"))

    def test_resolve_restores_default_timeout(self):
        """查 DNS 设了全局超时，不还回去会影响后面所有 socket。"""
        import socket as real_socket

        before = real_socket.getdefaulttimeout()
        with mock.patch.object(ddns.socket, "getaddrinfo", return_value=[]):
            ddns.resolve("me.dynv6.net")
        self.assertEqual(real_socket.getdefaulttimeout(), before)


class TestVerify(unittest.TestCase):
    def test_matches_same_address_written_differently(self):
        """我们发的是完整写法，DNS 回的是压缩写法，得认成同一个。"""
        with mock.patch.object(ddns, "resolve", return_value="240e:354:311:a200::1"):
            self.assertTrue(ddns.verify(
                "me.dynv6.net", "240e:0354:0311:a200:0000:0000:0000:0001",
                attempts=1))

    def test_verify_retries_then_succeeds(self):
        with mock.patch.object(ddns, "resolve", side_effect=[None, None, V6]):
            self.assertTrue(ddns.verify("me.dynv6.net", V6, attempts=3, interval=0))

    def test_verify_gives_up(self):
        with mock.patch.object(ddns, "resolve", return_value="2001:db8::1"):
            self.assertFalse(ddns.verify("me.dynv6.net", V6, attempts=2, interval=0))


class TestSameAddress(unittest.TestCase):
    def test_compressed_forms(self):
        self.assertTrue(ddns._same_address("240e:354:311:a200::1",
                                           "240e:0354:0311:a200:0000:0000:0000:0001"))

    def test_case_insensitive(self):
        self.assertTrue(ddns._same_address("240E::1", "240e::1"))

    def test_different(self):
        self.assertFalse(ddns._same_address("240e::1", "240e::2"))

    def test_garbage_falls_back_to_string_compare(self):
        self.assertTrue(ddns._same_address("不是地址", "不是地址"))
        self.assertFalse(ddns._same_address("不是地址", "另一个"))


class TestSetupGuidance(unittest.TestCase):
    def test_every_provider_has_instructions(self):
        for key in ddns.PROVIDERS:
            text = ddns.describe_setup(key)
            self.assertIn(ddns.PROVIDERS[key].label, text)
            self.assertIn("http", text.lower(), f"{key} 没给出注册地址")

    def test_unknown_provider(self):
        self.assertIn("不认识", ddns.describe_setup("nope"))


if __name__ == "__main__":
    unittest.main()
