"""UPnP 模块的测试。

SSDP 发现和 SOAP 调用都需要真实路由器，没法在单测里跑（而且很多路由器
根本没开 UPnP）。所以这里测的是**纯逻辑部分**：地址判定和响应解析 ——
恰恰也是最容易出错、错了会导致给出完全错误结论的部分。

比如把 CGNAT 地址误判成公网地址，用户就会以为"我有公网 IP"，
然后花半天时间在路由器上折腾端口映射，最后发现根本没用。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanlink import upnp  # noqa: E402


class TestCgnatDetection(unittest.TestCase):
    """100.64.0.0/10 是 RFC 6598 的运营商共享地址段。

    这个段里的地址看着像公网（不是 10./172./192.168. 开头的私网段），
    但外面根本路由不到 —— 判断错了会给用户错误的希望。
    """

    def test_inside_range(self):
        for address in ("100.64.0.1", "100.100.50.7", "100.127.255.254"):
            with self.subTest(address=address):
                self.assertTrue(upnp.is_cgnat(address))

    def test_outside_range(self):
        for address in ("100.63.255.255", "100.128.0.1", "100.0.0.1", "192.168.1.1"):
            with self.subTest(address=address):
                self.assertFalse(upnp.is_cgnat(address))

    def test_malformed_is_not_cgnat(self):
        for address in ("", "100.64", "不是地址", "100.64.1.2.3"):
            with self.subTest(address=address):
                self.assertFalse(upnp.is_cgnat(address))


class TestPublicAddress(unittest.TestCase):
    def test_real_public_addresses(self):
        for address in ("8.8.8.8", "113.87.1.1", "1.2.3.4"):
            with self.subTest(address=address):
                self.assertTrue(upnp.is_public_address(address))

    def test_cgnat_is_not_public(self):
        """最关键的一条：CGNAT 地址不算公网。

        它确实不是私网段，光看 is_private 会误判成公网。
        """
        self.assertFalse(upnp.is_public_address("100.64.0.7"))
        self.assertFalse(upnp.is_public_address("100.100.1.1"))

    def test_private_addresses(self):
        for address in ("192.168.1.1", "10.0.0.1", "172.16.5.5"):
            with self.subTest(address=address):
                self.assertFalse(upnp.is_public_address(address))

    def test_special_addresses(self):
        for address in ("127.0.0.1", "0.0.0.0", "169.254.1.1", "224.0.0.1", ""):
            with self.subTest(address=address):
                self.assertFalse(upnp.is_public_address(address))


class TestSsdpResponseParsing(unittest.TestCase):
    """从 SSDP 响应里抠 LOCATION。格式五花八门，大小写也不统一。"""

    def test_standard_response(self):
        data = (
            b"HTTP/1.1 200 OK\r\n"
            b"CACHE-CONTROL: max-age=120\r\n"
            b"LOCATION: http://192.168.1.1:1900/igd.xml\r\n"
            b"ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
            b"\r\n"
        )
        self.assertEqual(
            upnp._parse_location(data), "http://192.168.1.1:1900/igd.xml"
        )

    def test_lowercase_header(self):
        data = b"HTTP/1.1 200 OK\r\nlocation: http://10.0.0.1/rootDesc.xml\r\n\r\n"
        self.assertEqual(upnp._parse_location(data), "http://10.0.0.1/rootDesc.xml")

    def test_no_location_returns_none(self):
        data = b"HTTP/1.1 200 OK\r\nST: something\r\n\r\n"
        self.assertIsNone(upnp._parse_location(data))

    def test_garbage_returns_none(self):
        self.assertIsNone(upnp._parse_location(b"\x00\x01\x02 not http"))


class TestServiceDiscovery(unittest.TestCase):
    """从设备描述 XML 里找管端口映射的服务。

    UPnP 的描述是嵌套的（根设备里套子设备），服务藏在里面，
    而且类型名有 WANIPConnection / WANPPPConnection 两种（后者是拨号上网的）。
    """

    DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
    <friendlyName>我的路由器</friendlyName>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>
        <serviceList>
          <service>
            <serviceType>urn:schemas-upnp-org:service:Layer3Forwarding:1</serviceType>
            <controlURL>/ctl/L3F</controlURL>
          </service>
        </serviceList>
        <deviceList>
          <device>
            <deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:1</deviceType>
            <serviceList>
              <service>
                <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
                <controlURL>/ctl/IPConn</controlURL>
              </service>
            </serviceList>
          </device>
        </deviceList>
      </device>
    </deviceList>
  </device>
</root>
"""

    def test_finds_nested_wan_service(self):
        found = upnp._find_wan_service(self.DESCRIPTION, "http://192.168.1.1:1900/igd.xml")
        self.assertIsNotNone(found, "没找到嵌套在子设备里的 WANIPConnection")
        service_type, control_url = found
        self.assertIn("WANIPConnection", service_type)
        self.assertEqual(control_url, "http://192.168.1.1:1900/ctl/IPConn")

    def test_finds_ppp_variant(self):
        xml = self.DESCRIPTION.replace("WANIPConnection", "WANPPPConnection")
        found = upnp._find_wan_service(xml, "http://192.168.1.1/igd.xml")
        self.assertIsNotNone(found)
        self.assertIn("WANPPPConnection", found[0])

    def test_returns_none_when_absent(self):
        xml = self.DESCRIPTION.replace("WANIPConnection", "SomethingElse")
        self.assertIsNone(upnp._find_wan_service(xml, "http://192.168.1.1/igd.xml"))

    def test_absolute_control_url_kept(self):
        xml = self.DESCRIPTION.replace(
            "<controlURL>/ctl/IPConn</controlURL>",
            "<controlURL>http://192.168.1.1:5000/ctl/IPConn</controlURL>",
        )
        _type, control_url = upnp._find_wan_service(xml, "http://192.168.1.1/igd.xml")
        self.assertEqual(control_url, "http://192.168.1.1:5000/ctl/IPConn")

    def test_bad_xml_raises(self):
        with self.assertRaises(upnp.GatewayError):
            upnp._find_wan_service("这不是 XML", "http://192.168.1.1/igd.xml")


class TestSoapErrorMessages(unittest.TestCase):
    """UPnP 的错误码很晦涩，要翻译成人话。"""

    def _error(self, code):
        body = (
            '<?xml version="1.0"?><s:Envelope '
            'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            "<s:Fault><detail><UPnPError>"
            f"<errorCode>{code}</errorCode>"
            "<errorDescription>Unknown</errorDescription>"
            "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
        )
        return upnp._soap_error(body)

    def test_port_conflict_is_explained(self):
        """718 = ConflictInMappingEntry，端口被别的设备占了。

        之前这里被我翻译成"只允许映射到局域网内的地址"，完全是另一回事 ——
        用户会跑去翻路由器的安全设置，其实换个端口就好了。
        """
        message = self._error(718)
        self.assertIn("已经被映射到别的设备", message)
        self.assertIn("换一个端口", message)
        self.assertNotIn("只允许映射到局域网内", message, "这是错误的解释")
        self.assertIn("718", message, "错误码要留着，方便搜")

    def test_unauthorized_points_at_upnp_switch(self):
        self.assertIn("UPnP", self._error(606))

    def test_table_full_suggests_cleanup(self):
        self.assertIn("删掉", self._error(728))

    def test_unknown_code_still_reports_code(self):
        message = self._error(999)
        self.assertIn("999", message)

    def test_no_error_code_returns_empty(self):
        self.assertEqual(upnp._soap_error("<html>没出错</html>"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
