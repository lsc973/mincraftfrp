"""doctor 的解析逻辑测试。

这些检查依赖 PowerShell 的输出格式，而 PowerShell 会把枚举序列化成
数字（Public 变成 0），很容易解析错。解析错了会误报，比没有自检更糟，
所以这里把各种返回形态都钉死。

不依赖真实的 PowerShell：把 _powershell 换成假的。
"""

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
    """Profile 是位标志：Domain=1 Private=2 Public=4 All=0x7FFFFFFF。"""

    def _parse(self, json_text):
        with mock.patch.object(doctor, "IS_WINDOWS", True):
            with mock.patch.object(doctor, "_powershell", return_value=json_text):
                return doctor._firewall_profiles_for_python()

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

    def test_non_windows_returns_none(self):
        with mock.patch.object(doctor, "IS_WINDOWS", False):
            self.assertIsNone(doctor._firewall_profiles_for_python())


class TestFirewallCheck(unittest.TestCase):
    """把解析结果和当前网络类别合起来判定的逻辑。"""

    def _check(self, profiles, category):
        with mock.patch.object(doctor, "IS_WINDOWS", True):
            with mock.patch.object(doctor, "_firewall_profiles_for_python", return_value=profiles):
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
            with mock.patch.object(doctor, "_firewall_profiles_for_python", return_value=None):
                with mock.patch.object(doctor, "_network_category", return_value="Public"):
                    result = doctor.check_firewall()
        self.assertTrue(result.ok)
        self.assertTrue(result.warn_only)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
