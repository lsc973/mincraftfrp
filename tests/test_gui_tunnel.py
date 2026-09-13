"""图形界面「端口转发隧道」页的测试。

跟命令行版一样，这里也是**真的起一个 TCP 服务、真的穿过去** ——
界面代码最容易骗过"看起来没报错"的检查。
"""

import socket
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui_helpers import NoDialogs, button_texts, make_app, pump, wait_for  # noqa: E402
from helpers import EchoServer, isolate_config  # noqa: E402
from lanlink import Client, Host  # noqa: E402
from lanlink.gui import tunnel_page  # noqa: E402
from lanlink.tunnel import Tunnel  # noqa: E402


def setUpModule():
    """这个模块里的测试一启动隧道就会去更新免费域名。

    不隔离的话，开发机上配了真域名，跑一遍测试就把它真的指来指去了 ——
    测试往第三方服务上写东西，绝对不能接受。
    """
    isolate_config()


class TestTunnelPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = make_app()
        cls.app.deiconify()
        pump(cls.app, 0.3)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app._on_close()
        except Exception:
            pass

    def setUp(self):
        self.dialogs = NoDialogs()
        self.dialogs.__enter__()
        self.app.nickname.set("隧道测试")
        self.page = self.app.pages["tunnel"]
        self.service = EchoServer()
        self.app.show_page("start")
        pump(self.app, 0.2)

    def tearDown(self):
        try:
            self.page._stop_tunnel("")
        except Exception:
            pass
        pump(self.app, 0.4)
        self.service.close()
        self.dialogs.__exit__()

    # ------------------------------------------------------------ 页面

    def test_start_page_has_tunnel_entry(self):
        self.assertIn("端口转发隧道", button_texts(self.app.pages["start"]))

    def test_page_registered(self):
        self.assertIn("tunnel", self.app.pages)

    def test_mode_toggles_field_visibility(self):
        page = self.page
        page.mode.set("server")
        page._toggle_mode()
        pump(self.app, 0.1)
        self.assertTrue(page._target_row.winfo_ismapped(), "服务端模式该显示「服务地址」")
        self.assertFalse(page._listen_row.winfo_ismapped(), "服务端模式不该显示「本地监听」")

        page.mode.set("client")
        page._toggle_mode()
        pump(self.app, 0.1)
        self.assertFalse(page._target_row.winfo_ismapped())
        self.assertTrue(page._listen_row.winfo_ismapped())

    # ------------------------------------------------------------ 表单校验

    def test_rejects_empty_room(self):
        self.page.vars["room"].set("")
        self.assertIsNone(self.page._read_form())
        pump(self.app, 0.1)
        self.assertTrue(self.dialogs.calls, "房间名为空时应该弹提示")

    def test_rejects_bad_target(self):
        self.page.mode.set("server")
        self.page.vars["target"].set("没有冒号")
        self.assertIsNone(self.page._read_form())

    def test_rejects_bad_relay(self):
        self.page.vars["relay"].set("没有冒号")
        self.assertIsNone(self.page._read_form())

    def test_parses_listen_bare_port(self):
        self.page.mode.set("client")
        self.page.vars["listen"].set("25565")
        options = self.page._read_form()
        self.assertIsNotNone(options)
        self.assertEqual(options["listen"], ("127.0.0.1", 25565))

    def test_parses_valid_server_form(self):
        self.page.mode.set("server")
        self.page.vars["target"].set("127.0.0.1:25565")
        self.page.vars["room"].set("测试房")
        options = self.page._read_form()
        self.assertIsNotNone(options)
        self.assertTrue(options["is_server"])
        self.assertEqual(options["target"], ("127.0.0.1", 25565))
        self.assertEqual(options["room"], "测试房")

    # ------------------------------------------------------------ 真跑

    def test_server_mode_forwards_to_local_service(self):
        """服务端模式建起来之后，房间里的另一个人能连到本机服务。"""
        page = self.page
        page.mode.set("server")
        page._toggle_mode()
        page.vars["target"].set(f"127.0.0.1:{self.service.port}")
        page.vars["room"].set("GUI隧道房")
        page.vars["relay"].set("")
        page._start()

        self.assertTrue(
            wait_for(self.app, lambda: page.tunnel is not None, timeout=20.0),
            "隧道没建起来",
        )
        pump(self.app, 0.3)
        self.assertIn("运行中", page.status.cget("text"))
        self.assertIn("服务端", page.status.cget("text"))

        # 外面的人进来，再从他那边开一条流连到"我"这里的服务
        outsider = Client.connect("127.0.0.1", page.node.port, name="外面的人")
        try:
            self.assertTrue(wait_for(self.app, lambda: page.node.player_count == 2))
            out_tunnel = Tunnel(outsider, role="client", listen=("127.0.0.1", 0)).start()
            try:
                port = out_tunnel._listener.getsockname()[1]
                time.sleep(0.4)
                with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
                    sock.settimeout(10)
                    sock.sendall(b"through-gui-tunnel")
                    self.assertEqual(sock.recv(4096), b"through-gui-tunnel")
                self.assertTrue(
                    wait_for(self.app, lambda: any(
                        b"through-gui-tunnel" in d for d in self.service.received)),
                    "本机服务没收到数据",
                )
            finally:
                out_tunnel.close()
        finally:
            outsider.close()

    def test_client_mode_listens_and_forwards(self):
        """客户端模式：本机开的端口能连到对面那台机器的服务。"""
        # 必须 advertise=True：客户端模式是靠局域网广播按房间名找房主的
        host = Host("GUI对面", port=0, advertise=True).start()
        server_side = Tunnel(
            host, role="server", target=("127.0.0.1", self.service.port)
        ).start()

        page = self.page
        page.mode.set("client")
        page._toggle_mode()
        page.vars["room"].set("GUI对面")
        page.vars["listen"].set("127.0.0.1:0")
        page.vars["relay"].set("")
        page._start()

        try:
            self.assertTrue(
                wait_for(self.app, lambda: page.tunnel is not None, timeout=20.0),
                "客户端隧道没建起来",
            )
            port = page.tunnel._listener.getsockname()[1]
            time.sleep(0.4)

            with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
                sock.settimeout(10)
                sock.sendall(b"client-mode-works")
                self.assertEqual(sock.recv(4096), b"client-mode-works")
        finally:
            page._stop_tunnel("")
            pump(self.app, 0.4)
            server_side.close()
            host.close()

    def test_stop_resets_ui(self):
        page = self.page
        page.mode.set("server")
        page._toggle_mode()
        page.vars["target"].set(f"127.0.0.1:{self.service.port}")
        page.vars["room"].set("停止测试")
        page._start()
        self.assertTrue(wait_for(self.app, lambda: page.tunnel is not None, timeout=20.0))

        page._stop_tunnel("测试停止")
        self.assertTrue(wait_for(self.app, lambda: page.tunnel is None))
        pump(self.app, 0.3)
        self.assertIn("未启动", page.status.cget("text"))
        self.assertEqual(page.toggle_button.cget("text"), "启动隧道")

    def test_leaving_page_stops_tunnel(self):
        """切走页面必须把隧道停掉，不然会留个看不见的监听端口在后台。"""
        page = self.page
        page.mode.set("server")
        page._toggle_mode()
        page.vars["target"].set(f"127.0.0.1:{self.service.port}")
        page.vars["room"].set("要切走的房")
        page._start()
        self.assertTrue(wait_for(self.app, lambda: page.tunnel is not None, timeout=20.0))

        self.app.show_page("start")
        pump(self.app, 0.6)
        self.assertIsNone(page.tunnel, "切走页面后隧道还在跑")

    def test_form_disabled_while_running(self):
        page = self.page
        page.mode.set("server")
        page._toggle_mode()
        page.vars["target"].set(f"127.0.0.1:{self.service.port}")
        page.vars["room"].set("禁用测试")
        page._start()
        self.assertTrue(wait_for(self.app, lambda: page.tunnel is not None, timeout=20.0))
        pump(self.app, 0.3)

        states = [str(entry.cget("state")) for entry in page._entries]
        self.assertTrue(all(s == "disabled" for s in states),
                        f"运行中表单该禁用，实际 {states}")

        page._stop_tunnel("")
        pump(self.app, 0.5)
        states = [str(entry.cget("state")) for entry in page._entries]
        self.assertTrue(all(s == "normal" for s in states),
                        f"停止后表单该恢复，实际 {states}")

    def test_failure_does_not_wedge_the_page(self):
        """建不起来的时候，按钮和表单要能恢复，不能卡在"启动中"。"""
        page = self.page
        page.mode.set("client")
        page._toggle_mode()
        page.vars["room"].set("根本不存在的房间")
        page.vars["listen"].set("127.0.0.1:0")
        page.vars["relay"].set("")
        page._start()

        # 局域网里搜不到这个房间，应该报错并恢复
        self.assertTrue(
            wait_for(self.app, lambda: bool(self.dialogs.errors()), timeout=25.0),
            "搜不到房间时应该弹错误",
        )
        pump(self.app, 0.3)
        self.assertIsNone(page.tunnel)
        self.assertEqual(page.toggle_button.cget("text"), "启动隧道")
        self.assertEqual(str(page.toggle_button.cget("state")), "normal")
        for entry in page._entries:
            self.assertEqual(str(entry.cget("state")), "normal", "失败后表单该恢复可用")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDoctorDialog(unittest.TestCase):
    """环境自检弹窗。

    专门盯一个真实踩过的坑：自检跑在后台线程里，把结果送回界面时
    用了 ``self.after()`` —— 那是 Tcl 调用，后台线程里调会抛
    "main thread is not in main loop"。必须走 app.post。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = make_app()
        cls.app.deiconify()
        pump(cls.app, 0.3)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app._on_close()
        except Exception:
            pass

    def test_dialog_opens_and_shows_result(self):
        from lanlink.gui.pages import DoctorDialog

        dialog = DoctorDialog(self.app, self.app)
        try:
            # 自检要跑一会儿（含一次真实的收发自测）
            self.assertTrue(
                wait_for(
                    self.app,
                    lambda: "正在检查" not in dialog.text.get("1.0", "end"),
                    timeout=40.0,
                ),
                "自检结果一直没显示出来 —— 多半是后台线程回主线程的方式不对",
            )
            content = dialog.text.get("1.0", "end")
            self.assertTrue(
                len(content.strip()) > 20, f"自检内容太短：{content[:80]!r}"
            )
        finally:
            dialog.destroy()
            pump(self.app, 0.2)


class TestTunnelPageLayout(unittest.TestCase):
    """布局回归测试。

    盯的是一类很容易犯、又完全不会报错的错：**控件建在了错误的 parent 上**。

    tkinter 里 ``widget.grid()`` 永远用控件**自己的 parent** 当几何主。
    所以「先 new 好控件、再让辅助函数把它摆进某个容器」这种写法是行不通的 ——
    控件会跑进它自己 parent 的格子里。踩过的具体表现：6 个输入框全被放进
    form 的 (0,1) 格，正好压住第 0 行的「我是哪一边」选项框。

    功能测试完全发现不了这种问题（控件都在、值也对），只能靠布局断言。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = make_app()
        cls.app.deiconify()
        pump(cls.app, 0.3)
        cls.page = cls.app.pages["tunnel"]
        cls.app.show_page("tunnel")
        pump(cls.app, 0.4)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app._on_close()
        except Exception:
            pass

    def test_entries_live_inside_their_row_frames(self):
        page = self.page
        for index, entry in enumerate(page._entries):
            self.assertIn(
                entry.master, page._rows,
                f"第 {index} 个输入框没住在行容器里 —— parent 是 {entry.master}，"
                "它会被 grid 到那个 parent 的格子里，跟别的东西重叠",
            )

    def test_row_frames_are_stacked_not_stacked_on(self):
        """看得见的行容器各占一个独立行号，不能挤在同一行。

        注意 grid_remove() 掉的控件 grid_info() 是空的 —— 那是"隐藏"，
        不是"布局错了"，要排除掉。
        """
        rows = [
            int(info["row"])
            for holder in self.page._rows
            if (info := holder.grid_info())
        ]
        self.assertGreaterEqual(len(rows), 2, "可见的行太少了，测试没意义")
        self.assertEqual(len(rows), len(set(rows)), f"行容器行号重复：{rows}")

    def test_no_grid_cell_collision(self):
        """form 的直接子控件不能有两个抢同一个格子 —— 那就是互相遮挡。"""
        page = self.page
        form = page._rows[0].master
        used = {}
        for child in form.winfo_children():
            info = child.grid_info()
            if not info:
                continue
            row = int(info["row"])
            col = int(info["column"])
            span = int(info.get("columnspan", 1))
            for cell_col in range(col, col + span):
                key = (row, cell_col)
                self.assertNotIn(
                    key, used,
                    f"格子 {key} 被 {used.get(key)} 和 {child} 同时占了",
                )
                used[key] = child

    def test_visible_fields_sit_below_the_role_box(self):
        """看得见的输入框必须在「我是哪一边」下面，不能压上去。"""
        page = self.page
        for mode in ("server", "client"):
            with self.subTest(mode=mode):
                page.mode.set(mode)
                page._toggle_mode()
                pump(self.app, 0.25)

                role = page._role_box
                role_bottom = role.winfo_rooty() + role.winfo_height()
                for index, entry in enumerate(page._entries):
                    if not entry.winfo_ismapped():
                        continue   # 隐藏的那一行，几何信息没意义
                    self.assertGreaterEqual(
                        entry.winfo_rooty(), role_bottom,
                        f"{mode} 模式下有输入框压在「我是哪一边」上面",
                    )

    def test_mode_switch_keeps_layout_sane(self):
        """来回切角色不能把布局搞乱（grid_remove / grid 要能正确还原）。"""
        page = self.page
        for _ in range(3):
            page.mode.set("server")
            page._toggle_mode()
            pump(self.app, 0.15)
            page.mode.set("client")
            page._toggle_mode()
            pump(self.app, 0.15)

        page.mode.set("server")
        page._toggle_mode()
        pump(self.app, 0.25)
        self.assertTrue(page._target_row.winfo_ismapped())
        self.assertFalse(page._listen_row.winfo_ismapped())

        rows = [
            int(info["row"])
            for holder in page._rows
            if (info := holder.grid_info())
        ]
        self.assertEqual(sorted(rows), sorted(set(rows)), "来回切换后行号乱了")
        # 隐藏的那一行回来了，且占的还是它原来的行号
        self.assertEqual(
            int(page._target_row.grid_info()["row"]), 1,
            "「服务地址」这一行切回来之后跑到别的行号上去了",
        )


class TestTunnelPageViaRelay(unittest.TestCase):
    """图形界面 + 公网中继这条路径。

    用户报的 "timeout: timed out" 多半就走的是这条 —— 之前在界面测试里
    只覆盖了局域网模式，中继模式没测过。这里补上，用真的中继服务器。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = make_app()
        cls.app.deiconify()
        pump(cls.app, 0.3)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app._on_close()
        except Exception:
            pass

    def setUp(self):
        from lanlink import RelayServer

        self.dialogs = NoDialogs()
        self.dialogs.__enter__()
        self.app.nickname.set("中继测试")
        self.page = self.app.pages["tunnel"]
        self.service = EchoServer()
        self.relay = RelayServer("127.0.0.1", 0).start()
        self.app.show_page("start")
        pump(self.app, 0.2)

    def tearDown(self):
        try:
            self.page._stop_tunnel("")
        except Exception:
            pass
        pump(self.app, 0.4)
        self.relay.close()
        self.service.close()
        self.dialogs.__exit__()

    def test_server_mode_with_relay(self):
        """服务端挂中继：房间要真的出现在中继上。"""
        page = self.page
        page.mode.set("server")
        page._toggle_mode()
        page.vars["target"].set(f"127.0.0.1:{self.service.port}")
        page.vars["room"].set("中继隧道房")
        page.vars["relay"].set(f"127.0.0.1:{self.relay.port}")
        page._start()

        self.assertTrue(
            wait_for(self.app, lambda: page.tunnel is not None, timeout=25.0),
            "服务端挂中继失败：" + page.log.text.get("1.0", "end")[-300:],
        )
        pump(self.app, 0.4)
        self.assertTrue(
            wait_for(self.app, lambda: len(self.relay.rooms) == 1),
            f"中继上看不到房间，实际 {self.relay.rooms}",
        )

    def test_client_joins_via_relay_and_forwards(self):
        """完整路径：GUI 服务端挂中继 + 外部客户端经中继穿进来。"""
        page = self.page
        page.mode.set("server")
        page._toggle_mode()
        page.vars["target"].set(f"127.0.0.1:{self.service.port}")
        page.vars["room"].set("relay-room")
        page.vars["relay"].set(f"127.0.0.1:{self.relay.port}")
        page._start()
        self.assertTrue(
            wait_for(self.app, lambda: page.tunnel is not None, timeout=25.0),
            "服务端没建起来：" + page.log.text.get("1.0", "end")[-300:],
        )
        self.assertTrue(wait_for(self.app, lambda: len(self.relay.rooms) == 1))

        # 模拟"外地的那台机器"：经中继加入，再从他那边开一条流
        outsider = Client.join_via_relay(
            "127.0.0.1", self.relay.port, "relay-room", name="外地的人"
        )
        try:
            out_tunnel = Tunnel(outsider, role="client", listen=("127.0.0.1", 0)).start()
            try:
                port = out_tunnel._listener.getsockname()[1]
                time.sleep(0.5)
                with socket.create_connection(("127.0.0.1", port), timeout=15) as sock:
                    sock.settimeout(15)
                    sock.sendall(b"through-relay-tunnel")
                    self.assertEqual(sock.recv(4096), b"through-relay-tunnel")
                self.assertTrue(
                    wait_for(self.app, lambda: any(
                        b"through-relay-tunnel" in d for d in self.service.received)),
                    "本地服务没收到数据",
                )
            finally:
                out_tunnel.close()
        finally:
            outsider.close()


class TestCopyRoomId(unittest.TestCase):
    """「复制房间号」要复制对的那个号。

    挂了中继的时候，异地的人要填的是**中继房间号**，不是本机房间号 ——
    两个号可以不一样（创建房间时能自定义中继房间号）。
    以前这里无条件复制本机号，主人一自定义，发给朋友的号就是错的，
    对方会一直报"房间不存在"。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = make_app()
        cls.app.deiconify()
        pump(cls.app, 0.3)
        cls.app.nickname.set("复制测试")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app._on_close()
        except Exception:
            pass

    def _clipboard(self):
        try:
            return self.app.clipboard_get()
        except Exception:
            return ""

    def test_copies_local_room_id_without_relay(self):
        host = Host("无中继房", port=0, advertise=False).start()
        try:
            self.app.node = host
            self.app.pages["room"].is_host = True
            self.app.pages["room"]._copy_room_id()
            pump(self.app, 0.1)
            self.assertEqual(self._clipboard(), host.room_id)
        finally:
            self.app.node = None
            host.close()

    def test_copies_relay_room_id_when_custom(self):
        """自定义了中继房间号时，复制的必须是中继那个号。"""
        from lanlink import RelayServer

        relay = RelayServer("127.0.0.1", 0).start()
        host = Host("有中继房", port=0, advertise=False).start()
        try:
            host.attach_relay("127.0.0.1", relay.port, room_id="CUSTOM-ID")
            self.app.node = host
            self.app.pages["room"].is_host = True
            self.app.pages["room"]._copy_room_id()
            pump(self.app, 0.1)

            copied = self._clipboard()
            self.assertEqual(copied, "CUSTOM-ID",
                             "复制的应该是中继房间号，而不是本机房间号")
            self.assertNotEqual(copied, host.room_id,
                                "本机房间号和中继房间号不一样，复制错了对方就进不来")
        finally:
            self.app.node = None
            host.close()
            relay.close()

    def test_relay_room_id_defaults_to_local_when_left_blank(self):
        """中继房间号留空时，用的就是本机房间号 —— 这时候两个号本来就一样。"""
        from lanlink import RelayServer

        relay = RelayServer("127.0.0.1", 0).start()
        host = Host("留空房", port=0, advertise=False).start()
        try:
            host.attach_relay("127.0.0.1", relay.port, room_id=host.room_id)
            self.app.node = host
            self.app.pages["room"].is_host = True
            self.app.pages["room"]._copy_room_id()
            pump(self.app, 0.1)
            self.assertEqual(self._clipboard(), host.room_id)
        finally:
            self.app.node = None
            host.close()
            relay.close()


class TestDirectAddressMode(unittest.TestCase):
    """「对方地址」直连模式 —— 不用中继也不用局域网广播。

    这是给这两种场景准备的：
      · 装了 Tailscale / ZeroTier 之类的虚拟局域网
      · 对方有公网 IP 并做了端口映射
    两者都能直连，完全不需要中继。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = make_app()
        cls.app.deiconify()
        pump(cls.app, 0.3)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app._on_close()
        except Exception:
            pass

    def setUp(self):
        self.dialogs = NoDialogs()
        self.dialogs.__enter__()
        self.app.nickname.set("直连测试")
        self.page = self.app.pages["tunnel"]
        self.service = EchoServer()
        self.app.show_page("start")
        pump(self.app, 0.2)

    def tearDown(self):
        try:
            self.page._stop_tunnel("")
        except Exception:
            pass
        pump(self.app, 0.4)
        self.service.close()
        self.dialogs.__exit__()

    def test_parses_peer_address(self):
        page = self.page
        page.mode.set("client")
        page._toggle_mode()
        page.vars["peer"].set("100.64.0.7:50001")
        options = page._read_form()
        self.assertIsNotNone(options)
        self.assertEqual(options["peer"], ("100.64.0.7", 50001))

    def test_rejects_bad_peer_address(self):
        page = self.page
        page.mode.set("client")
        page._toggle_mode()
        page.vars["peer"].set("没有冒号")
        self.assertIsNone(page._read_form())

    def test_room_name_not_required_when_peer_given(self):
        """直连模式用不到房间名 —— 不该拦着不让走。"""
        page = self.page
        page.mode.set("client")
        page._toggle_mode()
        page.vars["room"].set("")
        page.vars["peer"].set("127.0.0.1:12345")
        self.assertIsNotNone(page._read_form())

    def test_room_name_still_required_without_peer(self):
        page = self.page
        page.mode.set("client")
        page._toggle_mode()
        page.vars["room"].set("")
        page.vars["peer"].set("")
        self.assertIsNone(page._read_form())

    def test_connects_directly_and_forwards(self):
        """真跑一遍：填对方地址，不填中继、不靠广播，数据要能穿过去。"""
        host = Host("直连对端", port=0, advertise=False).start()   # 故意关广播
        server_side = Tunnel(
            host, role="server", target=("127.0.0.1", self.service.port)
        ).start()

        page = self.page
        page.mode.set("client")
        page._toggle_mode()
        page.vars["room"].set("")
        page.vars["peer"].set(f"127.0.0.1:{host.port}")
        page.vars["relay"].set("")
        page.vars["listen"].set("127.0.0.1:0")
        page._start()

        try:
            self.assertTrue(
                wait_for(self.app, lambda: page.tunnel is not None, timeout=20.0),
                "直连模式没建起来：" + page.log.text.get("1.0", "end")[-300:],
            )
            port = page.tunnel._listener.getsockname()[1]
            time.sleep(0.4)

            with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
                sock.settimeout(10)
                sock.sendall(b"direct-address-mode")
                self.assertEqual(sock.recv(4096), b"direct-address-mode")
        finally:
            page._stop_tunnel("")
            pump(self.app, 0.4)
            server_side.close()
            host.close()


class TestCopyConnectionInfo(unittest.TestCase):
    """「复制连接信息」按钮。

    起因：IPv6 地址是 ``240e:354:311:a200:f587:f2f5:4fb3:bae2`` 这种四十个字符
    的东西，让对面照着念或者手抄根本不现实。所以这里要保证复制出来的东西
    对方**整段粘过去就能跑**，不需要自己拼地址、也不需要从别处找端口号。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = make_app()
        cls.app.deiconify()
        pump(cls.app, 0.3)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app._on_close()
        except Exception:
            pass

    def setUp(self):
        self.dialogs = NoDialogs()
        self.dialogs.__enter__()
        self.app.nickname.set("隧道测试")
        self.page = self.app.pages["tunnel"]
        self.service = EchoServer()
        self.app.show_page("start")
        pump(self.app, 0.2)

    def tearDown(self):
        try:
            self.page._stop_tunnel("")
        except Exception:
            pass
        pump(self.app, 0.4)
        self.service.close()
        self.dialogs.__exit__()

    # ------------------------------------------------------------ 工具

    def _start_server(self, password="", relay="", ipv6="240e:354:311:a200::1"):
        """起服务端。IPv6 是写死的 —— 不然复制出来的内容取决于跑测试的机器。"""
        page = self.page
        page.mode.set("server")
        page._toggle_mode()
        page.vars["target"].set(f"127.0.0.1:{self.service.port}")
        page.vars["room"].set("复制测试房")
        page.vars["relay"].set(relay)
        page.vars["password"].set(password)
        with mock.patch.object(tunnel_page, "global_ipv6", return_value=ipv6):
            page._start()
            self.assertTrue(
                wait_for(self.app, lambda: page.tunnel is not None, timeout=20.0),
                "隧道没建起来：" + page.log.text.get("1.0", "end")[-300:],
            )
        pump(self.app, 0.3)

    def _copied(self):
        self.page._copy_share()
        pump(self.app, 0.2)
        return self.page.clipboard_get()

    def _log_text(self):
        return self.page.log.text.get("1.0", "end")

    # ------------------------------------------------------------ 按钮状态

    def test_button_disabled_before_start(self):
        self.assertEqual(str(self.page.copy_button.cget("state")), "disabled")

    def test_button_enabled_while_running_and_disabled_again_after(self):
        self._start_server()
        self.assertEqual(str(self.page.copy_button.cget("state")), "normal")

        self.page._stop_tunnel("")
        pump(self.app, 0.4)
        self.assertEqual(str(self.page.copy_button.cget("state")), "disabled")

    # ------------------------------------------------------------ 复制内容

    def test_copied_text_is_a_command_the_peer_can_run_as_is(self):
        self._start_server()
        text = self._copied()

        self.assertIn("lanlink-cli.exe tunnel", text)
        # IPv6 必须带方括号，不然对面粘进去会被解析成端口的一部分
        self.assertIn("--addr [240e:354:311:a200::1]:", text)
        self.assertIn(f"--listen {self.service.port}", text)
        self.assertIn(f"127.0.0.1:{self.service.port}", text,
                      "还得告诉对面游戏里连哪个本地端口")

    def test_copied_address_uses_the_running_tunnel_port(self):
        """端口是随机分配的，复制的必须是真正在用的那个，不能是表单里那个。"""
        self._start_server()
        text = self._copied()
        self.assertIn(f"]:{self.page.node.port}", text)

    def test_password_is_in_the_copied_text(self):
        self._start_server(password="hunter2")
        text = self._copied()
        self.assertIn("--password hunter2", text)
        self.assertIn("hunter2", text)

    def test_password_with_space_is_quoted(self):
        """不加引号的话 shell 会把密码切成两段，对面怎么都连不上。"""
        self._start_server(password="two words")
        self.assertIn('--password "two words"', self._copied())

    def test_password_is_not_echoed_into_the_visible_log(self):
        """这个窗口经常被截图发出来问问题，密码别摆在上面。"""
        self._start_server(password="hunter2")
        self._copied()
        self.assertNotIn("hunter2", self._log_text())
        self.assertIn("房间密码", self._log_text())

    def test_copied_text_mentions_both_ways_to_connect(self):
        """对面可能用命令行版，也可能用图形版，两边都得说清楚。"""
        self._start_server()
        text = self._copied()
        self.assertIn("命令行版", text)
        self.assertIn("图形版", text)
        self.assertIn("对方地址", text)
        self.assertIn("本地监听", text)

    # ------------------------------------------------------------ 中继

    def test_relay_mode_tells_the_peer_to_fill_the_relay_not_an_address(self):
        """走中继时对面填的是中继地址 + 房间号，给成对方地址就全错了。"""
        from lanlink import RelayServer

        relay = RelayServer("127.0.0.1", 0).start()
        try:
            self._start_server(relay=f"127.0.0.1:{relay.port}")
            text = self._copied()

            self.assertIn("--relay 127.0.0.1:", text)
            self.assertIn("--room ", text)
            self.assertIn("中继地址", text)
            self.assertIn("房间名", text)
            self.assertNotIn("对方地址", text)
            self.assertNotIn("--addr", text)
        finally:
            relay.close()


class TestFreeDomain(unittest.TestCase):
    """免费域名和隧道的配合。

    核心要求：配了域名就用域名（短、不会变），**更新失败也必须照常把隧道
    建起来** —— 域名只是方便，用 IP 一样能连，不能让一个第三方服务挂了
    就把整个功能挡住。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = make_app()
        cls.app.deiconify()
        pump(cls.app, 0.3)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app._on_close()
        except Exception:
            pass

    def setUp(self):
        from lanlink import ddns

        ddns.clear()          # 每个用例都从"没配过"开始
        self.dialogs = NoDialogs()
        self.dialogs.__enter__()
        self.app.nickname.set("隧道测试")
        self.page = self.app.pages["tunnel"]
        self.service = EchoServer()
        self.app.show_page("start")
        pump(self.app, 0.2)

    def tearDown(self):
        from lanlink import ddns

        try:
            self.page._stop_tunnel("")
        except Exception:
            pass
        pump(self.app, 0.4)
        ddns.clear()
        self.service.close()
        self.dialogs.__exit__()

    def _configure(self, hostname="test.dynv6.net"):
        from lanlink import ddns

        ddns.save(ddns.DdnsConfig("dynv6", hostname, "tok123"))

    def _start_server(self, publish=None):
        """起服务端隧道，并把域名更新拦下来（默认当作成功）。"""
        page = self.page
        page.mode.set("server")
        page._toggle_mode()
        page.vars["target"].set(f"127.0.0.1:{self.service.port}")
        page.vars["room"].set("域名测试房")
        page.vars["relay"].set("")
        page.vars["password"].set("")
        page._start()
        self.assertTrue(
            wait_for(self.app, lambda: page.tunnel is not None, timeout=20.0),
            "隧道没建起来：" + page.log.text.get("1.0", "end")[-300:],
        )
        pump(self.app, 0.3)

    # ------------------------------------------------------------ 隔离守卫

    def test_tests_never_touch_the_real_config(self):
        """跑测试绝不能碰到用户真实的域名配置。

        隧道服务端一启动就会去更新域名，真碰上了就是拿用户的域名乱指。
        """
        import os
        from pathlib import Path

        from lanlink import ddns

        real = Path(os.environ.get("APPDATA") or Path.home()) / "lanlink" / "config.json"
        self.assertNotEqual(ddns.config_path(), real,
                            "测试用的配置目录没被隔离，可能改到用户的真域名")

    # ------------------------------------------------------------ 界面

    def test_page_has_a_button_for_it(self):
        self.assertIn("免费域名…", button_texts(self.page))

    def test_dialog_hides_the_saved_token(self):
        """窗口会被截图发出来问问题，令牌等同于域名的写权限。"""
        from lanlink.gui.ddns_dialog import DdnsDialog

        self._configure()
        dialog = DdnsDialog(self.page, self.app)
        try:
            pump(self.app, 0.2)
            self.assertEqual(dialog.hostname.get(), "test.dynv6.net")
            self.assertEqual(dialog.token.get(), "", "令牌不该回填到输入框里")
            self.assertIn("test.dynv6.net", dialog.status.cget("text"))
            self.assertNotIn("tok123", dialog.status.cget("text"))
        finally:
            dialog.destroy()
            pump(self.app, 0.1)

    # ------------------------------------------------------------ 接线

    def test_domain_wins_over_the_raw_ipv6(self):
        from lanlink import ddns

        with mock.patch.object(ddns, "publish", return_value="test.dynv6.net"), \
                mock.patch.object(tunnel_page, "global_ipv6", return_value="240e::1"):
            self._configure()
            self._start_server()

        self.assertEqual(self.page._share_domain, "test.dynv6.net")
        self.assertEqual(self.page._advertised_host(), "test.dynv6.net",
                         "配了域名就该用域名，不该再甩那一长串 IPv6 给对面")

    def test_share_text_uses_the_domain(self):
        from lanlink import ddns

        with mock.patch.object(ddns, "publish", return_value="test.dynv6.net"), \
                mock.patch.object(tunnel_page, "global_ipv6", return_value="240e::1"):
            self._configure()
            self._start_server()

        text = self.page._share_text(self.page._running_options)
        self.assertIn("test.dynv6.net:", text)
        self.assertNotIn("240e::1", text, "有域名就不该再出现原始 IPv6")

    def test_domain_is_verified_before_being_advertised(self):
        """更新完要确认真的解析过去了，不能光看请求返回成功。"""
        from lanlink import ddns

        with mock.patch.object(ddns, "publish") as publish, \
                mock.patch.object(tunnel_page, "global_ipv6", return_value="240e::1"):
            self._configure()
            self._start_server()
        self.assertTrue(publish.called)
        self.assertEqual(publish.call_args[0][0], "240e::1",
                         "该把本机当前的 IPv6 发布出去")

    # ------------------------------------------------------------ 失败不能挡住隧道

    def test_update_failure_still_builds_the_tunnel(self):
        """第三方服务挂了不能让整个隧道功能不可用。"""
        from lanlink import ddns

        with mock.patch.object(ddns, "publish",
                               side_effect=ddns.DdnsError("dynv6 说令牌不对")), \
                mock.patch.object(tunnel_page, "global_ipv6", return_value="240e::1"):
            self._configure()
            self._start_server()   # 这里没抛异常就是过了

        self.assertIsNone(self.page._share_domain)
        self.assertIn("240e::1", self.page._advertised_host(),
                      "域名没更新成功时该退回用 IPv6")
        self.assertIn("令牌不对", self.page.log.text.get("1.0", "end"))

    def test_unexpected_exception_does_not_escape_either(self):
        """更新代码里冒出个没预料到的异常，同样不能把隧道带崩。"""
        from lanlink import ddns

        with mock.patch.object(ddns, "publish", side_effect=RuntimeError("天塌了")), \
                mock.patch.object(tunnel_page, "global_ipv6", return_value="240e::1"):
            self._configure()
            self._start_server()

        self.assertIsNone(self.page._share_domain)
        self.assertIn("天塌了", self.page.log.text.get("1.0", "end"))

    def test_no_ipv6_means_no_domain_update_and_no_crash(self):
        from lanlink import ddns

        with mock.patch.object(ddns, "publish") as publish, \
                mock.patch.object(tunnel_page, "global_ipv6", return_value=None):
            self._configure()
            self._start_server()

        self.assertFalse(publish.called, "没有 IPv6 就没有可发布的地址")
        self.assertIsNone(self.page._share_domain)

    def test_unconfigured_domain_is_not_published(self):
        from lanlink import ddns

        with mock.patch.object(ddns, "publish") as publish, \
                mock.patch.object(tunnel_page, "global_ipv6", return_value="240e::1"):
            self._start_server()   # 没配置

        self.assertFalse(publish.called)
        self.assertIsNone(self.page._share_domain)


class TestRoomOnlyMode(unittest.TestCase):
    """地址内置时，界面上只要填房间号 + 口令。

    这是整个功能的落点 —— 对面拿到 exe 之后不该被要求知道任何地址。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = make_app()
        cls.app.deiconify()
        pump(cls.app, 0.3)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app._on_close()
        except Exception:
            pass

    def setUp(self):
        from lanlink import server

        server.clear()
        self.dialogs = NoDialogs()
        self.dialogs.__enter__()
        self.app.nickname.set("房间号测试")
        self.page = self.app.pages["tunnel"]
        self.service = EchoServer()
        self.app.show_page("start")
        pump(self.app, 0.2)

    def tearDown(self):
        from lanlink import server

        try:
            self.page._stop_tunnel("")
        except Exception:
            pass
        pump(self.app, 0.4)
        server.clear()
        self.service.close()
        self.dialogs.__exit__()

    def _client_form(self, room="", peer=""):
        page = self.page
        page.mode.set("client")
        page._toggle_mode()
        page.vars["room"].set(room)
        page.vars["peer"].set(peer)
        page.vars["relay"].set("")
        return page._read_form()

    # ------------------------------------------------------------ 表单

    def test_without_a_server_the_peer_field_is_still_needed(self):
        options = self._client_form(room="某房", peer="1.2.3.4:50001")
        self.assertIsNotNone(options)
        self.assertEqual(options["peer"], ("1.2.3.4", 50001))

    def test_with_a_server_the_address_comes_from_config(self):
        from lanlink import server

        server.save("me.dynv6.net:50001")
        options = self._client_form(room="0dcc265b")
        self.assertIsNotNone(options, "地址内置了就不该再要求填地址")
        self.assertEqual(options["peer"], ("me.dynv6.net", 50001))

    def test_room_number_is_required_when_using_the_builtin_address(self):
        """房间号必须填 —— 否则连上的是哪间房根本无从校验。"""
        from lanlink import server

        server.save("me.dynv6.net:50001")
        self.assertIsNone(self._client_form(room=""))
        self.assertTrue(self.dialogs.calls, "该弹个提示说缺房间号")

    def test_explicit_address_still_wins_over_the_default(self):
        from lanlink import server

        server.save("me.dynv6.net:50001")
        options = self._client_form(room="某房", peer="10.0.0.5:60001")
        self.assertEqual(options["peer"], ("10.0.0.5", 60001))

    def test_peer_field_is_hidden_when_the_address_is_built_in(self):
        from lanlink import server

        server.save("me.dynv6.net:50001")
        page = self.page
        page.mode.set("client")
        page._toggle_mode()
        pump(self.app, 0.1)
        self.assertFalse(page._peer_row.winfo_ismapped(),
                         "地址内置了还让用户看见「对方地址」，他会以为要填")

    # ------------------------------------------------------------ 服务端分享

    def test_server_shares_a_room_number_not_an_address(self):
        from lanlink import server

        server.save("me.dynv6.net:50001")
        page = self.page
        page.mode.set("server")
        page._toggle_mode()
        page.vars["target"].set(f"127.0.0.1:{self.service.port}")
        page.vars["room"].set("分享测试房")
        page.vars["relay"].set("")
        page.vars["password"].set("pw")
        page._start()
        self.assertTrue(
            wait_for(self.app, lambda: page.tunnel is not None, timeout=20.0),
            "隧道没建起来：" + page.log.text.get("1.0", "end")[-300:])
        pump(self.app, 0.3)

        args = page._share_args(page._running_options)
        self.assertTrue(args.get("server_default"))
        self.assertEqual(args["room"], page.node.room_id)
        self.assertNotIn("address", args, "内置地址时不该把地址塞给对方")

        text = page._share_text(page._running_options)
        self.assertIn("房间号", text)
        self.assertIn(page.node.room_id, text)
        self.assertNotIn("--addr", text)

    def test_without_a_server_it_still_shares_the_address(self):
        page = self.page
        page.mode.set("server")
        page._toggle_mode()
        page.vars["target"].set(f"127.0.0.1:{self.service.port}")
        page.vars["room"].set("老模式房")
        page.vars["relay"].set("")
        page._start()
        self.assertTrue(wait_for(self.app, lambda: page.tunnel is not None, timeout=20.0))
        pump(self.app, 0.3)

        args = page._share_args(page._running_options)
        self.assertFalse(args.get("server_default"))
        self.assertIn("address", args)

    def test_field_says_room_number_not_room_name_when_address_is_built_in(self):
        """名字得跟着连法变。

        地址内置时填的是要校验的房间号；名字不改的话对面会去填"我的世界"
        这种房间名，然后被房间号校验拦下来，还不知道为什么。
        """
        from lanlink import server

        label = self.page._row_labels[self.page._room_row]
        # 页面是共用的，标签只在"切回该页"时刷新 —— 先手动刷一次，
        # 免得读到上一个用例留下的字
        self.page.on_show()
        pump(self.app, 0.1)
        self.assertEqual(label.cget("text"), "房间名")

        server.save("me.dynv6.net:50001")
        self.page.on_show()
        pump(self.app, 0.1)
        self.assertEqual(label.cget("text"), "房间号")

        server.clear()
        self.page.on_show()
        pump(self.app, 0.1)
        self.assertEqual(label.cget("text"), "房间名")

    def test_switching_pages_refreshes_the_label(self):
        """配置可能在别处被改过（命令行 server set），切回来要显示新的。"""
        from lanlink import server

        label = self.page._row_labels[self.page._room_row]
        server.save("me.dynv6.net:50001")
        self.app.show_page("tunnel")
        pump(self.app, 0.2)
        self.assertEqual(label.cget("text"), "房间号")
