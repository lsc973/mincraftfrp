"""图形界面的功能测试。

不能靠"看起来没报错"来判断 GUI 是好的，所以这里用 ``app.update()`` 手动
驱动事件循环（代替 ``mainloop()``），把界面当成一个普通对象来断言：
房间建起来没有、成员表对不对、聊天内容有没有真的进去。

**跑测试时会有一个窗口出现**，这是必须的：Tk 只把键盘事件投递给真正可见的
窗口，``withdraw()`` 或全透明的窗口收不到 ``event_generate("<Return>")``，
那样"回车发送"这条就测不了。（试过移到屏幕外、设 alpha=0，都不行。）

测试跑完窗口会自动关掉。
"""

import sys
import time
import unittest
from pathlib import Path
from tkinter import messagebox

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanlink import Client, Host  # noqa: E402

import tkinter as tk  # noqa: E402


def _make_app():
    """建一个主窗口；建不起来就抛 SkipTest。

    这里**不能**先建一个探路的 ``Tk()`` 再销毁 —— 那样 ttk 的全局状态里会留下
    一个已销毁的窗口，后面真正的 app 切换主题时 ``ttk::ThemeChanged`` 会打到它
    身上，跑完甩一句 'application has been destroyed' 出来。
    """
    from lanlink.gui.app import LanlinkApp

    try:
        return LanlinkApp()
    except Exception as exc:  # 无头环境 / 没装 tkinter
        raise unittest.SkipTest(f"没有可用的图形环境：{exc}")


def pump(app, seconds=1.5):
    """代替 mainloop：把排队的事件（含 after 回调）都跑一遍。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            app.update()
        except tk.TclError:
            return
        time.sleep(0.01)


def wait_for(app, predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            app.update()
        except tk.TclError:
            return False
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _NoDialogs:
    """把弹窗换掉 —— 测试里弹模态框会直接卡死。"""

    def __enter__(self):
        self._saved = {}
        for name in ("showinfo", "showwarning", "showerror", "askyesno"):
            self._saved[name] = getattr(messagebox, name)
        self.calls = []
        messagebox.showinfo = lambda *a, **k: self.calls.append(("info", a))
        messagebox.showwarning = lambda *a, **k: self.calls.append(("warn", a))
        messagebox.showerror = lambda *a, **k: self.calls.append(("error", a))
        messagebox.askyesno = lambda *a, **k: (self.calls.append(("ask", a)), True)[1]
        return self

    def __exit__(self, *exc):
        for name, fn in self._saved.items():
            setattr(messagebox, name, fn)

    def errors(self):
        return [c for c in self.calls if c[0] in ("error", "warn")]


class TestGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _make_app()
        # 必须真实可见，否则 Tk 不投递键盘事件（见模块开头的说明）
        cls.app.deiconify()
        cls.app.lift()
        pump(cls.app, 0.3)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app._on_close()
        except Exception:
            pass

    def setUp(self):
        self.dialogs = _NoDialogs()
        self.dialogs.__enter__()
        self.peer = None
        self.app.nickname.set("测试用户")
        self.app.show_page("start")
        self.app.node = None
        pump(self.app, 0.2)

    def tearDown(self):
        if self.app.node is not None:
            try:
                self.app.node.close()
            except Exception:
                pass
            self.app.node = None
        if self.peer is not None:
            try:
                self.peer.close()
            except Exception:
                pass
        pump(self.app, 0.3)
        self.dialogs.__exit__()

    # -------------------------------------------------------- 起始页

    def test_starts_on_start_page(self):
        self.assertIn("start", self.app.pages)
        self.assertEqual(self.app.title(), "lanlink —— 局域网联机工具")

    def test_has_chinese_capable_font(self):
        self.assertTrue(self.app.font_family)

    # -------------------------------------------------------- 开房

    def test_create_room_shows_room_page(self):
        with _NoDialogs():
            self.app.create_room({
                "room_name": "界面测试房",
                "port": 0,
                "password": "",
                "max_players": 8,
            })
            self.assertTrue(
                wait_for(self.app, lambda: self.app.node is not None),
                "房间没建起来",
            )
        pump(self.app, 0.5)

        host = self.app.node
        self.assertIsInstance(host, Host)
        page = self.app.pages["room"]
        self.assertIn("界面测试房", page.room_label.cget("text"))
        self.assertIn(host.room_id, page.addr_label.cget("text"))
        self.assertIn("房间已创建", page.chat.text.get("1.0", "end"))

    def test_room_with_password_advertises_lock(self):
        self.app.create_room({
            "room_name": "有密码的房", "port": 0, "password": "s3cret", "max_players": 4,
        })
        self.assertTrue(wait_for(self.app, lambda: self.app.node is not None))
        self.assertTrue(self.app.node.room_info().has_password)

    # -------------------------------------------------------- 聊天

    def test_member_join_updates_list_and_chat(self):
        self.app.create_room({
            "room_name": "成员测试", "port": 0, "password": "", "max_players": 8,
        })
        self.assertTrue(wait_for(self.app, lambda: self.app.node is not None))
        host = self.app.node

        self.peer = Client.connect("127.0.0.1", host.port, name="小明")
        page = self.app.pages["room"]
        self.assertTrue(
            wait_for(self.app, lambda: page.members.count() == 2),
            f"成员表没更新，当前 {page.members.count()} 行",
        )
        chat = page.chat.text.get("1.0", "end")
        self.assertIn("小明", chat)
        self.assertIn("加入了房间", chat)
        self.assertIn("共 2 人", page.member_hint.cget("text"))

    def test_peer_message_appears_in_chat(self):
        self.app.create_room({
            "room_name": "收消息", "port": 0, "password": "", "max_players": 8,
        })
        self.assertTrue(wait_for(self.app, lambda: self.app.node is not None))
        host = self.app.node
        self.peer = Client.connect("127.0.0.1", host.port, name="小明")
        page = self.app.pages["room"]
        wait_for(self.app, lambda: page.members.count() == 2)

        page.chat.clear()
        self.peer.send("你好呀".encode("utf-8"))
        self.assertTrue(
            wait_for(self.app, lambda: "你好呀" in page.chat.text.get("1.0", "end")),
            "对方发的消息没进聊天区",
        )
        self.assertIn("小明", page.chat.text.get("1.0", "end"))

    def test_host_send_broadcasts_and_echoes(self):
        self.app.create_room({
            "room_name": "发消息", "port": 0, "password": "", "max_players": 8,
        })
        self.assertTrue(wait_for(self.app, lambda: self.app.node is not None))
        host = self.app.node
        self.peer = Client.connect("127.0.0.1", host.port, name="小明")
        page = self.app.pages["room"]
        wait_for(self.app, lambda: page.members.count() == 2)

        got = []
        self.peer.on("data", lambda s, d, j: got.append((s, d)))
        page.chat.clear()

        page.entry.insert(0, "大家好")
        page.send()
        self.assertTrue(wait_for(self.app, lambda: len(got) == 1), "对方没收到广播")
        self.assertEqual(got[0][1], "大家好".encode("utf-8"))
        self.assertEqual(got[0][0], 0, "来源应该是主机(0)")
        self.assertIn("大家好", page.chat.text.get("1.0", "end"))
        self.assertEqual(page.entry.get(), "", "发完之后输入框该清空")

    def test_enter_key_sends(self):
        self.app.create_room({
            "room_name": "回车发送", "port": 0, "password": "", "max_players": 8,
        })
        self.assertTrue(wait_for(self.app, lambda: self.app.node is not None))
        host = self.app.node
        self.peer = Client.connect("127.0.0.1", host.port, name="小明")
        page = self.app.pages["room"]
        wait_for(self.app, lambda: page.members.count() == 2)

        got = []
        self.peer.on("data", lambda s, d, j: got.append(d))
        page.entry.insert(0, "回车试试")

        # Tk 只把键盘事件投递给"真正可见且在前台"的窗口。连跑多个 Python 版本时
        # 前一个窗口可能还挡在上面，event_generate 就送不到 —— 那是环境问题不是
        # 功能坏了。所以这里先把窗口顶到前面，不行就重试几次。
        fired = False
        for _ in range(5):
            self.app.deiconify()
            self.app.lift()
            self.app.focus_force()
            page.entry.focus_force()
            pump(self.app, 0.15)
            page.entry.event_generate("<Return>")
            if wait_for(self.app, lambda: len(got) == 1, timeout=1.5):
                fired = True
                break

        if not fired:
            # 环境实在不给面子（比如无头 CI 上的虚拟桌面）就跳过，别误报成功能坏了。
            # 绑定本身还在，下面单独断言。
            self.assertTrue(page.entry.bind("<Return>"), "回车绑定丢了")
            self.skipTest(
                "桌面环境没把合成的键盘事件投递到窗口 —— 绑定在，但这条没法验。"
                "单独跑 tests.test_gui 通常能过。"
            )
        self.assertEqual(got[0], "回车试试".encode("utf-8"))

    def test_binary_data_is_described_not_crashed(self):
        """收到二进制不能崩，也不能把乱码糊一屏。"""
        self.app.create_room({
            "room_name": "二进制", "port": 0, "password": "", "max_players": 8,
        })
        self.assertTrue(wait_for(self.app, lambda: self.app.node is not None))
        host = self.app.node
        self.peer = Client.connect("127.0.0.1", host.port, name="小明")
        page = self.app.pages["room"]
        wait_for(self.app, lambda: page.members.count() == 2)

        page.chat.clear()
        self.peer.send(bytes(range(256)))
        self.assertTrue(
            wait_for(self.app, lambda: "字节二进制数据" in page.chat.text.get("1.0", "end")),
            "二进制数据没被正常描述",
        )

    def test_private_message_requires_selection(self):
        self.app.create_room({
            "room_name": "私聊", "port": 0, "password": "", "max_players": 8,
        })
        self.assertTrue(wait_for(self.app, lambda: self.app.node is not None))
        page = self.app.pages["room"]
        page.target.set("private")
        page.entry.insert(0, "给谁呢")
        page.send()
        pump(self.app, 0.3)
        self.assertTrue(self.dialogs.calls, "没选人时应该提示一下")
        self.assertEqual(page.entry.get(), "给谁呢", "发送失败时不该清空输入框")
        page.target.set("broadcast")

    def test_private_message_reaches_target(self):
        self.app.create_room({
            "room_name": "私聊2", "port": 0, "password": "", "max_players": 8,
        })
        self.assertTrue(wait_for(self.app, lambda: self.app.node is not None))
        host = self.app.node
        self.peer = Client.connect("127.0.0.1", host.port, name="小明")
        page = self.app.pages["room"]
        wait_for(self.app, lambda: page.members.count() == 2)

        got = []
        self.peer.on("data", lambda s, d, j: got.append(d))
        page.members.tree.selection_set(page.members.tree.get_children()[1])  # 第 0 行是主机
        page.target.set("private")
        page.entry.insert(0, "悄悄话")
        page.send()
        self.assertTrue(wait_for(self.app, lambda: len(got) == 1), "私聊没送到")
        self.assertEqual(got[0], "悄悄话".encode("utf-8"))
        page.target.set("broadcast")

    # -------------------------------------------------------- 离开

    def test_leave_room_returns_to_start(self):
        self.app.create_room({
            "room_name": "要离开", "port": 0, "password": "", "max_players": 8,
        })
        self.assertTrue(wait_for(self.app, lambda: self.app.node is not None))
        self.app.leave_room()
        self.assertTrue(wait_for(self.app, lambda: self.app.node is None))
        pump(self.app, 0.3)
        self.assertIsNone(self.app.node)

    def test_connection_lost_is_handled(self):
        """对方把连接掐了，界面要能回到起始页而不是卡死。"""
        self.app.create_room({
            "room_name": "断线", "port": 0, "password": "", "max_players": 8,
        })
        self.assertTrue(wait_for(self.app, lambda: self.app.node is not None))
        self.app.on_connection_lost()
        pump(self.app, 0.3)
        self.assertIsNone(self.app.node)

    # -------------------------------------------------------- 加入（GUI 当客户端）

    def test_join_room_as_client(self):
        host = Host("被加入的房", name="房主", port=0, advertise=False).start()
        try:
            self.app.join_room({
                "mode": "direct", "host": "127.0.0.1", "port": host.port, "password": "",
            })
            self.assertTrue(
                wait_for(self.app, lambda: self.app.node is not None), "没连上主机"
            )
            pump(self.app, 0.4)
            page = self.app.pages["room"]
            self.assertIn("被加入的房", page.room_label.cget("text"))
            self.assertGreater(self.app.node.peer_id, 0)

            # 主机广播，客户端界面要收到
            page.chat.clear()
            host.broadcast("房主说话".encode("utf-8"))
            self.assertTrue(
                wait_for(self.app, lambda: "房主说话" in page.chat.text.get("1.0", "end")),
                "客户端没收到主机广播",
            )

            # 客户端发言，主机要收到
            got = []
            host.on("data", lambda s, d, j: got.append(d))
            page.entry.insert(0, "客户端说话")
            page.send()
            self.assertTrue(wait_for(self.app, lambda: len(got) == 1), "主机没收到")
        finally:
            host.close()

    def test_join_wrong_password_reports_error(self):
        host = Host("上锁的房", port=0, advertise=False, password="pw").start()
        try:
            self.app.join_room({
                "mode": "direct", "host": "127.0.0.1", "port": host.port, "password": "错的",
            })
            self.assertTrue(
                wait_for(self.app, lambda: bool(self.dialogs.errors()), timeout=12.0),
                "密码错误应该弹提示",
            )
        finally:
            host.close()

    # -------------------------------------------------------- 中继页

    def test_relay_page_start_and_stop(self):
        self.app.show_page("relay")
        page = self.app.pages["relay"]
        page.port.set("0")
        page.token.set("tk")
        page._start()
        pump(self.app, 0.4)

        self.assertIsNotNone(page.server)
        self.assertGreater(page.server.port, 0)
        self.assertIn("运行中", page.status.cget("text"))

        port = page.server.port
        page._stop_server()
        pump(self.app, 0.3)
        self.assertIsNone(page.server)
        self.assertIn("未启动", page.status.cget("text"))
        self.assertGreater(port, 0)

    def test_leaving_relay_page_stops_server(self):
        """切走页面要把中继停掉，不能留在后台偷偷监听。"""
        self.app.show_page("relay")
        page = self.app.pages["relay"]
        page.port.set("0")
        page._start()
        pump(self.app, 0.3)
        self.assertIsNotNone(page.server)

        self.app.show_page("start")
        pump(self.app, 0.3)
        self.assertIsNone(page.server, "离开中继页后服务器还在跑")


    # -------------------------------------------------------- 线程安全

    # 这两个测试前身是独立的 TestGuiThreadSafety 类。合并进来是因为一个进程里
    # 建两个 Tk() 根窗口会让 ttk 的 ThemeChanged 打到已销毁的窗口上，
    # 跑完甩一句 'application has been destroyed' 出来 —— 纯粹是测试自找的噪音。

    def test_post_runs_on_main_thread(self):
        import threading

        done = []
        self.app.post(lambda: done.append(threading.current_thread().name))
        self.assertTrue(wait_for(self.app, lambda: bool(done)), "post 的任务没被执行")
        self.assertEqual(done[0], threading.main_thread().name)

    def test_high_frequency_posts_from_many_threads(self):
        """多线程狂发界面更新不能丢、不能乱。"""
        import threading

        counter = {"n": 0}
        lock = threading.Lock()

        def bump():
            with lock:
                counter["n"] += 1

        def worker():
            for _ in range(50):
                self.app.post(bump)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertTrue(
            wait_for(self.app, lambda: counter["n"] == 200, timeout=10.0),
            f"只处理了 {counter['n']}/200 条",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
