"""doctor 的解析逻辑测试。

这些检查依赖 PowerShell 的输出格式，而 PowerShell 会把枚举序列化成
数字（Public 变成 0），很容易解析错。解析错了会误报，比没有自检更糟，
所以这里把各种返回形态都钉死。

不依赖真实的 PowerShell：把 _powershell 换成假的。
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanlink import doctor  # noqa: E402


class TestNetworkCategory(unittest.TestCase):
    """NetworkCategory 枚举：0=Public 1=Private 2=DomainAuthenticated。"""

    def _parse(self, json_text):
        with mock.patch.object(doctor, "_powershell", return_value=json_text):
            return doctor._network_category()

    def test_public_is_zero_not_string(self):
        # 回归测试：以前这里直接 str(0) 变成 "0"，跟 "Public" 比不上，
        # 于是明明放行了却报"当前类别没被覆盖"。
        self.assertEqual(self._parse("0"), "Public")

    def test_private(self):
        self.assertEqual(self._parse("1"), "Private")

    def test_domain(self):
        self.assertEqual(self._parse("2"), "DomainAuthenticated")

    def test_single_element_array(self):
        self.assertEqual(self._parse("[0]"), "Public")

    def test_multiple_networks_prefers_stricter(self):
        """同时连着有线(WiFi)和公用网络时，只要有一个是专用就按专用算。"""
        self.assertEqual(self._parse("[0,1]"), "Private")
        self.assertEqual(self._parse("[0,2]"), "DomainAuthenticated")

    def test_string_form_still_supported(self):
        self.assertEqual(self._parse('"Public"'), "Public")

    def test_empty_returns_none(self):
        self.assertIsNone(self._parse(""))

    def test_garbage_returns_none(self):
        self.assertIsNone(self._parse("{不是JSON"))

    def test_unknown_enum_value(self):
        self.assertEqual(self._parse("99"), "未知(99)")


class TestFirewallProfiles(unittest.TestCase):
    """Profile 是位标志：Domain=1 Private=2 Public=4 All=0x7FFFFFFF。

    PowerShell 5 有时给的是 "Domain, Private" 这种字符串，两条路都要认。
    """

    def _parse(self, json_text):
        """只测位标志/字符串的解析，不掺程序匹配那层。"""
        data = json.loads(json_text) if json_text.strip() else []
        if not isinstance(data, list):
            data = [data]
        return doctor._profiles_from_values(data)

    def test_single_public(self):
        self.assertEqual(self._parse("4"), {"Public"})

    def test_single_private(self):
        self.assertEqual(self._parse("2"), {"Private"})

    def test_combined_private_and_public(self):
        self.assertEqual(self._parse("6"), {"Private", "Public"})

    def test_domain_bit(self):
        self.assertEqual(self._parse("1"), {"Domain"})

    def test_all_profiles_sentinel(self):
        self.assertEqual(self._parse("2147483647"), {"Domain", "Private", "Public"})

    def test_zero_means_all(self):
        self.assertEqual(self._parse("0"), {"Domain", "Private", "Public"})

    def test_list_of_profiles(self):
        self.assertEqual(self._parse("[4,4,2]"), {"Public", "Private"})

    def test_no_rules(self):
        self.assertEqual(self._parse(""), set())

    def test_string_profile(self):
        self.assertEqual(self._parse('"Public"'), {"Public"})

    def test_comma_joined_profile(self):
        """PowerShell 5 的字符串化写法。"""
        self.assertEqual(self._parse('"Domain, Private"'), {"Domain", "Private"})

    def test_string_any_means_all(self):
        self.assertEqual(self._parse('"Any"'), {"Domain", "Private", "Public"})

    def test_non_windows_returns_none(self):
        with mock.patch.object(doctor, "IS_WINDOWS", False):
            self.assertIsNone(doctor._firewall_profiles_for("C:\\python.exe"))

    # ---- 只认点名了这个程序的规则 ----

    def _rules(self, payload):
        with mock.patch.object(doctor, "IS_WINDOWS", True):
            with mock.patch.object(doctor, "_powershell", return_value=payload):
                return doctor._firewall_profiles_for("C:\\Program Files\\python\\python.exe")

    def test_matches_exact_program(self):
        payload = json.dumps([
            {"P": "Public", "F": "C:\\Program Files\\python\\python.exe"},
        ])
        self.assertEqual(self._rules(payload), {"Public"})

    def test_path_compare_ignores_case_and_slashes(self):
        payload = json.dumps([
            {"P": "Private", "F": "c:/program files/PYTHON/python.exe"},
        ])
        self.assertEqual(self._rules(payload), {"Private"})

    def test_other_programs_are_ignored(self):
        """别的 python.exe 副本放行了不代表当前这个被放行。"""
        payload = json.dumps([
            {"P": "Public", "F": "C:\\other\\venv\\Scripts\\python.exe"},
        ])
        self.assertEqual(self._rules(payload), set())

    def test_any_program_rules_are_ignored(self):
        """Program=Any 的多半是端口规则或者绑在系统服务上的，不能算作已放行。"""
        payload = json.dumps([
            {"P": "Public", "F": "Any"},
        ])
        self.assertEqual(self._rules(payload), set())


class TestFirewallCheck(unittest.TestCase):
    """把解析结果和当前网络类别合起来判定的逻辑。"""

    def _check(self, profiles, category, program="C:\\Program Files\\python\\python.exe"):
        with mock.patch.object(doctor, "IS_WINDOWS", True):
            with mock.patch.object(doctor, "_running_program", return_value=program):
                with mock.patch.object(doctor, "_firewall_profiles_for", return_value=profiles):
                    with mock.patch.object(doctor, "_network_category", return_value=category):
                        return doctor.check_firewall()

    def test_covered_passes(self):
        self.assertTrue(self._check({"Public"}, "Public").ok)
        self.assertTrue(self._check({"Private", "Public"}, "Private").ok)
        self.assertTrue(self._check({"Domain", "Private", "Public"}, "DomainAuthenticated").ok)

    def test_not_covered_fails(self):
        """只放行了公用，但当前网络是专用 —— 这就是跨机器连不上的典型原因。"""
        result = self._check({"Public"}, "Private")
        self.assertFalse(result.ok)
        self.assertFalse(result.warn_only)
        self.assertIn("New-NetFirewallRule", result.advice)

    def test_no_rules_at_all_fails(self):
        result = self._check(set(), "Public")
        self.assertFalse(result.ok)

    def test_unknown_category_does_not_cry_wolf(self):
        """查不到网络类别时不该报失败 —— 宁可不说，也别误导。"""
        result = self._check({"Public"}, None)
        self.assertTrue(result.ok)

    def test_query_failure_is_warning_only(self):
        with mock.patch.object(doctor, "IS_WINDOWS", True):
            with mock.patch.object(doctor, "_running_program", return_value="C:\\t\\lanlink.exe"):
                with mock.patch.object(doctor, "_firewall_profiles_for", return_value=None):
                    with mock.patch.object(doctor, "_network_category", return_value="Public"):
                        result = doctor.check_firewall()
        self.assertTrue(result.ok)
        self.assertTrue(result.warn_only)

    def test_advice_names_the_running_exe_not_python(self):
        """打包成 exe 之后跑的就是 lanlink.exe，防火墙规则也得按它来。

        这里踩过：检查写死成查 python 的规则，结果 exe 用户看到「防火墙通过」，
        实际入站被 Windows 挡着，对方怎么都连不上。
        """
        result = self._check(set(), "Public", program="H:\\dist\\lanlink-cli.exe")
        self.assertFalse(result.ok)
        self.assertIn("lanlink-cli.exe", result.detail)
        self.assertIn("lanlink-cli.exe", result.advice)
        self.assertNotIn("python", result.advice.lower())

    def test_non_windows_is_warning_only(self):
        with mock.patch.object(doctor, "IS_WINDOWS", False):
            result = doctor.check_firewall()
        self.assertTrue(result.ok)
        self.assertTrue(result.warn_only)


class TestReporting(unittest.TestCase):
    def test_marks(self):
        self.assertEqual(doctor.Check("x", True, "").mark, "[通过]")
        self.assertEqual(doctor.Check("x", False, "").mark, "[失败]")
        self.assertEqual(doctor.Check("x", False, "", warn_only=True).mark, "[警告]")

    def test_report_does_not_crash_on_mixed_checks(self):
        checks = [
            doctor.Check("好的", True, "一切正常"),
            doctor.Check("坏的", False, "出问题了", "这样修：\n  第一步\n  第二步"),
            doctor.Check("不确定的", False, "查不到", "手动看看", warn_only=True),
        ]
        text = doctor.render_report(checks)
        self.assertIn("好的", text)
        self.assertIn("第一步", text)
        self.assertIn("1 项没通过", text)
        self.assertIn("无法确认", text)

    def test_report_all_good_mentions_other_machine(self):
        text = doctor.render_report([doctor.Check("好的", True, "没问题")])
        self.assertIn("本机环境没问题", text)
        self.assertIn("对面", text)


class TestRunChecks(unittest.TestCase):
    def test_run_checks_returns_all_sections(self):
        """真的跑一遍完整自检 —— 本机应该能全过。"""
        checks = doctor.run_checks()
        names = [c.name for c in checks]
        for expected in ("本机 IP", "防火墙", "发现端口", "TCP 监听", "房间发现", "收发回环"):
            self.assertIn(expected, names)

        by_name = {c.name: c for c in checks}
        self.assertTrue(by_name["本机 IP"].ok, by_name["本机 IP"].detail)
        self.assertTrue(by_name["收发回环"].ok, by_name["收发回环"].detail)
        self.assertTrue(by_name["房间发现"].ok, by_name["房间发现"].detail)

    def test_checks_use_requested_discovery_port(self):
        checks = doctor.run_checks(discovery_port=47911)
        port_check = next(c for c in checks if c.name == "发现端口")
        self.assertIn("47911", port_check.detail)


class TestVirtualLanDetection(unittest.TestCase):
    """虚拟局域网地址检测。

    装了 Tailscale / ZeroTier 的话，两台机器就有了一条"第三条路"：
    用虚拟 IP 直连，既不用中继也不用公网 IP。自检应该把这个地址挑出来，
    不然用户根本不知道该往「对方地址」里填什么。
    """

    def _detect(self, powershell_output, local_ips):
        with mock.patch.object(doctor, "_powershell", return_value=powershell_output):
            with mock.patch.object(doctor, "all_local_ips", return_value=local_ips):
                return doctor._virtual_lan_addresses()

    def test_detects_by_adapter_name(self):
        payload = json.dumps([
            {"IPAddress": "100.101.102.103", "InterfaceAlias": "Tailscale"},
            {"IPAddress": "192.168.1.5", "InterfaceAlias": "WLAN"},
        ])
        found = self._detect(payload, ["192.168.1.5"])
        self.assertEqual(found, [("100.101.102.103", "Tailscale")])

    def test_detects_zerotier_and_hamachi(self):
        payload = json.dumps([
            {"IPAddress": "10.147.20.5", "InterfaceAlias": "ZeroTier One [abc]"},
            {"IPAddress": "25.3.4.5", "InterfaceAlias": "Hamachi"},
        ])
        found = dict(self._detect(payload, []))
        self.assertEqual(found.get("10.147.20.5"), "ZeroTier")
        self.assertEqual(found.get("25.3.4.5"), "Hamachi")

    def test_falls_back_to_ip_range_when_name_unavailable(self):
        """拿不到网卡名时，按 IP 段兜底。"""
        found = self._detect("", ["100.64.0.7", "192.168.1.5"])
        self.assertEqual([addr for addr, _ in found], ["100.64.0.7"])

    def test_plain_lan_address_is_not_flagged(self):
        """普通局域网地址不能误报 —— 误报会让用户白折腾。"""
        found = self._detect("", ["192.168.1.5", "10.0.0.3", "172.16.5.9"])
        self.assertEqual(found, [])

    def test_check_interfaces_reports_virtual_lan(self):
        with mock.patch.object(
            doctor, "_virtual_lan_addresses",
            return_value=[("100.64.0.7", "Tailscale")],
        ):
            result = doctor.check_interfaces()
        self.assertEqual(result.name, "虚拟局域网")
        self.assertIn("100.64.0.7", result.detail)
        self.assertIn("对方地址", result.advice)
        self.assertTrue(result.warn_only, "这只是提示，不该算失败")

    def test_check_interfaces_plain_when_no_vpn(self):
        with mock.patch.object(doctor, "_virtual_lan_addresses", return_value=[]):
            with mock.patch.object(doctor, "all_local_ips", return_value=["192.168.1.5"]):
                result = doctor.check_interfaces()
        self.assertEqual(result.name, "多网卡")
        self.assertTrue(result.ok)


class TestVpnIpRanges(unittest.TestCase):
    """网段判定的边界 —— 写错了要么误报要么漏报。"""

    def test_tailscale_range_boundaries(self):
        # 100.64.0.0/10 覆盖第二段 64~127
        self.assertTrue(doctor._is_cgnat_shared("100.64.0.1"))
        self.assertTrue(doctor._is_cgnat_shared("100.100.50.7"))
        self.assertTrue(doctor._is_cgnat_shared("100.127.255.254"))
        self.assertFalse(doctor._is_cgnat_shared("100.63.0.1"),
                         "100.63 在 /10 之外，只写 '100.64.' 前缀会漏掉后半段")
        self.assertFalse(doctor._is_cgnat_shared("100.128.0.1"))
        self.assertFalse(doctor._is_cgnat_shared("192.168.1.1"))
        self.assertFalse(doctor._is_cgnat_shared("100.64"))

    def test_hamachi_range(self):
        self.assertTrue(doctor._is_hamachi("25.1.2.3"))
        self.assertFalse(doctor._is_hamachi("25.1.2"))
        self.assertFalse(doctor._is_hamachi("192.25.1.3"))


class TestSuggestDirectConnection(unittest.TestCase):
    """如果本机有虚拟局域网地址，自检应该主动建议用直连而不是中继。"""

    def test_advice_mentions_no_relay_needed(self):
        with mock.patch.object(
            doctor, "_virtual_lan_addresses",
            return_value=[("100.64.0.7", "Tailscale")],
        ):
            result = doctor.check_interfaces()
        self.assertIn("不需要中继", result.advice)
        self.assertIn("也不需要公网 IP", result.advice)


class TestPublicAddressCheck(unittest.TestCase):
    """公网可达性检查 —— 决定"不用中继、对面也不装东西"这条路走不走得通。

    判断错了代价很大：把 CGNAT 误判成公网，用户会花半天在路由器上折腾
    端口映射，最后发现根本没用。
    """

    def _check(self, external_ip=None, error=None, ipv6=None):
        from lanlink import discovery, upnp

        if error is not None:
            upnp_patcher = mock.patch.object(upnp, "get_external_ip", side_effect=error)
        else:
            upnp_patcher = mock.patch.object(upnp, "get_external_ip", return_value=external_ip)
        # 默认当作没有 IPv6。不隔离的话，跑测试的机器上真有 IPv6 就会把
        # CGNAT 那条分支带到"有救"的分支去，测的东西就不是想测的了。
        with upnp_patcher, mock.patch.object(discovery, "global_ipv6", return_value=ipv6):
            return doctor.check_public_address()

    def test_public_ip_is_good_news(self):
        result = self._check(external_ip="113.87.1.1")
        self.assertTrue(result.ok)
        self.assertIn("113.87.1.1", result.detail)
        self.assertIn("端口映射", result.advice)
        self.assertIn("不需要中继", result.advice)

    def test_cgnat_is_reported_as_failure(self):
        result = self._check(external_ip="100.64.0.7")
        self.assertFalse(result.ok)
        self.assertFalse(result.warn_only)
        self.assertIn("大内网", result.detail)
        self.assertIn("申请公网 IP", result.advice)
        self.assertIn("做不到", result.advice,
                      "要明确告诉用户这个要求在 CGNAT 下实现不了")

    def test_cgnat_with_ipv6_points_at_the_way_out(self):
        """IPv4 断了但有全球 IPv6 —— 这时候还说"做不到"就是自相矛盾。

        用户的诉求是「对面只跑 lanlink.exe、什么都不装」，而这个诉求在 IPv6 上
        是能满足的，只是能不能通取决于路由器放不放行，所以判警告而不是失败。
        """
        result = self._check(external_ip="100.64.0.7", ipv6="240e:354:311:a200::1")
        self.assertTrue(result.ok)
        self.assertTrue(result.warn_only)
        self.assertIn("240e:354:311:a200::1", result.advice)
        self.assertIn("[240e:354:311:a200::1]", result.advice, "IPv6 要带方括号")
        self.assertNotIn("做不到", result.advice)

    def test_double_nat_is_reported(self):
        result = self._check(external_ip="192.168.1.1")
        self.assertFalse(result.ok)
        self.assertIn("一层路由", result.detail)

    def test_upnp_unavailable_falls_back_to_manual_instructions(self):
        from lanlink import upnp

        result = self._check(error=upnp.GatewayError("没找到路由器"))
        self.assertTrue(result.ok, "查不到不该算失败")
        self.assertTrue(result.warn_only)
        self.assertIn("路由器后台", result.advice)
        self.assertIn("100.64", result.advice, "要告诉用户怎么手动判断")

    def test_included_in_full_run(self):
        names = [c.name for c in doctor.run_checks()]
        self.assertIn("公网可达", names)


class TestAdviceVisibility(unittest.TestCase):
    """建议不能只在失败时才显示。

    "查不到"（warn_only 且 ok）这类最需要指引 —— 比如 UPnP 问不到路由器时，
    用户正需要知道怎么手动去看 WAN 口 IP。藏起来就等于没说。
    """

    def test_advice_shown_for_warn_only_checks(self):
        check = doctor.Check(
            "某项", True, "查不到", "手动看看这样那样", warn_only=True
        )
        text = doctor.render_report([check])
        self.assertIn("手动看看这样那样", text)

    def test_advice_indentation_preserved(self):
        """建议里的缩进是有意义的（子条目、命令示例），不能被抹掉。"""
        advice = "\n".join([
            "这样做：",
            "  · 第一条",
            "    这是续行",
            "  · 第二条",
        ])
        check = doctor.Check("某项", False, "出错了", advice)
        text = doctor.render_report([check])
        self.assertIn("  · 第一条", text)
        self.assertIn("    这是续行", text, "续行的缩进被抹掉了，层级就看不出来了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
