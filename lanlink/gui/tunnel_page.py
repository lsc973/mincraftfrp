"""图形界面的端口转发隧道页。

跟 RelayPage 一样，所有跨线程的界面更新都走 ``app.post()``。
另外这个页面有个额外的坑：**离开页面时必须把隧道停掉**，否则后台会留着
一个监听端口和一条到中继的连接，用户看不见也关不掉。
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

from ..discovery import Scanner, global_ipv6
from ..node import Client, Host
from ..link import format_addr, parse_addr, parse_listen_addr
from .. import server as servercfg
from ..tunnel import Tunnel, TunnelError, check_room, share_fields, share_text
from .widgets import ChatView

__all__ = ["TunnelPage"]


class TunnelPage(ttk.Frame):
    """一端建隧道：要么把本机服务暴露出去，要么把对面的服务接到本地。"""

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, padding=20)
        self.app = app
        self.node = None
        self.tunnel = None
        self._running_options: Optional[dict] = None   # 复制连接信息要用
        self._share_host: Optional[str] = None         # 建隧道时算一次，别反复算
        self._share_domain: Optional[str] = None       # 配了免费域名就用它
        self._copy_reset_id: Optional[str] = None
        self._monitor: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._entries: list = []   # 所有输入框，启动后统一禁用
        self._rows: list = []      # 每行的容器，布局测试要检查层级
        self._row_labels: dict = {}   # 行容器 → 标签控件（有的标签要改字）

        self._build_header()
        self._build_form()
        self._build_status()
        self._toggle_mode()

    # ------------------------------------------------------------ 布局

    def _build_header(self) -> None:
        ttk.Label(self, text="端口转发隧道", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            self,
            text="让不在同一局域网的人连上你本机的服务（Minecraft、远程桌面、网页…）",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 14))

    def _build_form(self) -> None:
        form = ttk.Frame(self)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        # ---- 角色 ----
        self.mode = tk.StringVar(value="server")
        role = ttk.LabelFrame(form, text=" 我是哪一边 ", padding=10)
        role.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        self._role_box = role   # 布局测试要拿它比对位置
        ttk.Radiobutton(
            role, text="服务在我这里（把本机服务开放出去）",
            variable=self.mode, value="server", command=self._toggle_mode,
        ).pack(anchor="w")
        ttk.Radiobutton(
            role, text="服务在对面（在本机开个端口接过去）",
            variable=self.mode, value="client", command=self._toggle_mode,
        ).pack(anchor="w")

        # ---- 字段 ----
        self.vars = {
            "target": tk.StringVar(value="127.0.0.1:25565"),
            "listen": tk.StringVar(value="25565"),
            "peer": tk.StringVar(),
            "room": tk.StringVar(value="我的世界"),
            "relay": tk.StringVar(),
            "relay_token": tk.StringVar(),
            "password": tk.StringVar(),
        }

        self._target_row = self._labeled(form, 1, "服务地址", self.vars["target"])
        self._listen_row = self._labeled(form, 2, "本地监听", self.vars["listen"])
        self._peer_row = self._labeled(form, 3, "对方地址", self.vars["peer"])
        self._room_row = self._labeled(form, 4, "房间名", self.vars["room"])
        self._labeled(form, 5, "中继地址", self.vars["relay"])
        self._labeled(form, 6, "中继口令", self.vars["relay_token"], show="•")
        self._labeled(form, 7, "房间密码", self.vars["password"], show="•")

        self.hint = ttk.Label(form, text="", style="Hint.TLabel", wraplength=560, justify="left")
        self.hint.grid(row=8, column=1, sticky="w", pady=(4, 0))

        # ---- 按钮 ----
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=(12, 8))
        self.toggle_button = ttk.Button(buttons, text="启动隧道", command=self._toggle)
        self.toggle_button.pack(side="left")
        # 服务端起来之后才需要它 —— 客户端那边是别人把地址给你，没什么可发的
        self.copy_button = ttk.Button(
            buttons, text="复制连接信息", command=self._copy_share, state="disabled"
        )
        self.copy_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="免费域名…", command=self._open_ddns).pack(side="left")
        ttk.Button(buttons, text="返回", command=lambda: self.app.show_page("start")).pack(
            side="left", padx=8
        )

    def _labeled(self, parent: ttk.Frame, row: int, label: str, var: tk.StringVar,
                 *, show: str = "", width: int = 32) -> ttk.Frame:
        """一行「标签 + 输入框」，返回这一行的容器。

        输入框必须**以 holder 为 parent** 创建。tkinter 里 ``widget.grid()``
        永远用控件自己的 parent 当几何主 —— 先建好 Entry 再想 grid 进 holder
        是做不到的，它会被放进 parent 的格子里。

        这里踩过：Entry 的 parent 传的是 form，结果 6 个输入框全挤在 form 的
        (0,1) 格，正好压住第 0 行的「我是哪一边」选项框。
        """
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=0, columnspan=3, sticky="ew", pady=3)
        holder.columnconfigure(1, weight=1)
        label_widget = ttk.Label(holder, text=label, width=10)
        label_widget.grid(row=0, column=0, sticky="w")

        entry = ttk.Entry(holder, textvariable=var, width=width, show=show)
        entry.grid(row=0, column=1, sticky="w")
        self._entries.append(entry)
        self._rows.append(holder)
        # 记下标签控件：有几个字段的名字要跟着模式变（房间名 ↔ 房间号）
        self._row_labels[holder] = label_widget
        return holder

    def _build_status(self) -> None:
        self.status = ttk.Label(self, text="未启动", style="Banner.TLabel")
        self.status.pack(anchor="w", pady=(6, 2))
        self.traffic = ttk.Label(self, text="", style="Hint.TLabel")
        self.traffic.pack(anchor="w", pady=(0, 8))

        ttk.Label(self, text="运行日志").pack(anchor="w")
        self.log = ChatView(self, family=self.app.font_family)
        self.log.pack(fill="both", expand=True)

    def _toggle_mode(self) -> None:
        """按角色显示/隐藏对应的字段，并刷新跟着配置变的那几处文案。"""
        self._refresh_room_label()
        if self.mode.get() == "server":
            self._listen_row.grid_remove()
            self._peer_row.grid_remove()
            self._target_row.grid()
            if servercfg.resolve() is not None:
                self.hint.configure(
                    text="隧道会把你填的服务地址（本机上的服务）开放给房间里的人。\n"
                         "地址已经内置好了，启动之后把「房间号」和口令发给对方，"
                         "他只要填这两样就能连上，不用知道你的 IP。"
                )
            else:
                self.hint.configure(
                    text="隧道会把你填的服务地址（本机上的服务）开放给房间里的人。"
                         "房间名两端要填一样的；填了中继地址就走公网，留空则走局域网。\n"
                         "点「免费域名…」配一个固定的短名字，再设一次服务器地址"
                         "（lanlink server set 你的域名:端口），对方就只要填房间号了。"
                )
        else:
            self._target_row.grid_remove()
            self._listen_row.grid()
            self._peer_row.grid()
            if servercfg.resolve() is not None:
                # 地址已经内置了 —— 这是给"对面"用的那份 exe，他只填房间号和口令
                self._peer_row.grid_remove()
                self.hint.configure(
                    text="隧道会在本机开一个端口，连这个端口就等于连到了对面那台机器的服务。\n"
                         "服务器地址是内置好的，你只要填开房的人给你的**房间号**和口令。\n"
                         "端口只写数字时绑 127.0.0.1（只给本机程序连）。"
                )
            else:
                self.hint.configure(
                    text="隧道会在本机开一个端口，连这个端口就等于连到了对面那台机器的服务。\n"
                         "端口只写数字时绑 127.0.0.1（只给本机程序连）。\n"
                         "「对方地址」可选：只在局域网搜不到时才要填。装了 Tailscale / ZeroTier "
                         "之类的本地网络、或者对方有公网 IP，就填对方地址 —— 这样连中继都不用。"
                )

    # ------------------------------------------------------------ 启停

    def _toggle(self) -> None:
        if self.tunnel is None:
            self._start()
        else:
            self._stop_tunnel("已手动停止")

    def _read_form(self):
        """把表单读成参数，顺便校验。返回 None 表示校验没过。"""
        values = {k: v.get().strip() for k, v in self.vars.items()}
        is_server = self.mode.get() == "server"

        # 「对方地址」：填了就直连，跳过局域网搜索 —— 虚拟局域网
        # （Tailscale/ZeroTier）和公网 IP + 端口映射这两种情况都靠它，
        # 两者都不需要中继。
        peer = None
        if not is_server and values["peer"]:
            peer = parse_addr(values["peer"])
            if peer is None:
                messagebox.showerror(
                    "对方地址不对",
                    "格式应该是 host:端口，比如 192.168.1.10:50001。\n"
                    "IPv6 要加方括号，比如 [240e:354::1]:50001。",
                    parent=self)
                return None

        # 地址编在 exe 里（或配置里）的时候，用户什么都不用填 —— 直接用它。
        using_default = False
        if not is_server and peer is None and not values["relay"]:
            default = servercfg.resolve()
            if default is not None:
                peer, using_default = default, True

        # 房间名/房间号什么时候必须填：
        #   · 靠局域网搜索找房 —— 得按名字找
        #   · 用编好的地址连 —— 得靠房间号确认连对了没有
        # 自己填了完整地址时反而不用（对面就一间房，连上就是它）。
        if not values["room"] and (peer is None or using_default):
            if using_default:
                messagebox.showinfo(
                    "缺房间号",
                    "地址已经内置了，填上开房的人给你的房间号就能连。", parent=self)
            else:
                messagebox.showinfo("缺房间名", "房间名两端要填一样的。", parent=self)
            return None

        relay = None
        if values["relay"]:
            relay = parse_addr(values["relay"])
            if relay is None:
                messagebox.showerror(
                    "中继地址不对",
                    "格式应该是 host:端口，比如 1.2.3.4:9000；"
                    "IPv6 要加方括号，比如 [240e::1]:9000。",
                    parent=self)
                return None

        if is_server:
            if ":" not in values["target"]:
                messagebox.showerror("服务地址不对",
                                     "格式应该是 host:端口，比如 127.0.0.1:25565。", parent=self)
                return None
            target = parse_addr(values["target"])
            if target is None:
                messagebox.showerror("服务地址不对",
                                     "格式应该是 host:端口，比如 127.0.0.1:25565。",
                                     parent=self)
                return None
        else:
            listen = parse_listen_addr(values["listen"])
            if listen is None:
                messagebox.showerror("监听地址不对",
                                     "要么写端口（如 25565），要么写 地址:端口。", parent=self)
                return None

        return {
            "is_server": is_server,
            # 昵称必须在主线程读出来塞进来。tkinter 的 StringVar.get()
            # 会进 Tcl，在后台线程里调会抛 "main thread is not in main loop"。
            "nickname": self.app.nickname.get().strip() or "隧道",
            "room": values["room"],
            "relay": relay,
            "relay_token": values["relay_token"],
            "password": values["password"],
            "target": target if is_server else None,
            "listen": None if is_server else listen,
            "peer": peer,
        }

    def _start(self) -> None:
        options = self._read_form()
        if options is None:
            return

        self.log.clear()
        self.log.add("", "正在建立隧道…", "system")
        self.toggle_button.configure(state="disabled")
        self.app.set_status("正在建立隧道…")

        def work():
            return self._build_tunnel(options)

        def done(result):
            node, tunnel, domain = result
            self.node, self.tunnel = node, tunnel
            self._share_domain = domain
            self._on_started(options)

        def failed(exc):
            self.toggle_button.configure(state="normal")
            self.log.add("", f"建立失败：{type(exc).__name__}: {exc}", "error")
            self.app.set_status("隧道建立失败")
            messagebox.showerror("隧道建立失败", f"{type(exc).__name__}: {exc}", parent=self)

        # 建连可能要好几百毫秒到几秒，别卡住界面
        threading.Thread(
            target=lambda: self._run_guarded(work, done, failed), daemon=True
        ).start()

    def _run_guarded(self, work, done, failed) -> None:
        try:
            result = work()
        except Exception as exc:  # 网络、端口、参数都可能出问题
            self.app.post(failed, exc)
            return
        self.app.post(done, result)

    def _build_tunnel(self, options: dict):
        """跑在后台线程里：建 Node，再建 Tunnel。

        **任何一步失败都要把已经建起来的 Node 关掉**。否则房间还开着、
        广播还在发、端口还占着，而用户在界面上什么都看不见 ——
        下一次点启动又会再起一个，越堆越多。
        """
        # 这个函数跑在后台线程里，除了 app.post() 不要碰任何 tkinter 对象
        room = options["room"]
        relay = options["relay"]
        password = options["password"]
        nickname = options["nickname"]

        node = None
        domain = None
        try:
            if options["is_server"]:
                node = Host(room, name=f"{nickname} #tunnel",
                            port=0, password=password).start()
                self.app.post(self.log.add, "",
                              f"房间「{room}」已创建，房间号 {node.room_id}", "system")
                if relay:
                    node.attach_relay(*relay, room_id=room, token=options["relay_token"])
                    self.app.post(self.log.add, "", f"已挂中继 {relay[0]}:{relay[1]}", "system")
                domain = self._publish_domain()
                tunnel = Tunnel(node, role="server", target=options["target"]).start()
            else:
                if relay:
                    node = Client.join_via_relay(*relay, room, name=f"{nickname} #tunnel",
                                                 password=password,
                                                 token=options["relay_token"])
                elif options.get("peer"):
                    # 直接连指定地址，不搜局域网。虚拟局域网、端口映射、
                    # 以及"地址编在 exe 里"这三种都走这条。
                    host, port = options["peer"]
                    self.app.post(self.log.add, "", f"正在连接 {host}:{port}…", "system")
                    node = Client.connect(host, port, name=f"{nickname} #tunnel",
                                          password=password)
                    # 房间号填错了要当场说清楚，别让人莫名其妙进了别的房间
                    check_room(node, room)
                else:
                    self.app.post(self.log.add, "", f"正在局域网里找房间「{room}」…", "system")
                    info = self._find_room(room)
                    if info is None:
                        raise TunnelError(
                            f"局域网里没找到房间「{room}」。服务端起了吗？"
                            f"\n不在同一网段的话，三条路：\n"
                            f"  · 装了 Tailscale / ZeroTier 之类的虚拟局域网"
                            f" → 在「对方地址」填对方的虚拟 IP\n"
                            f"  · 对方有公网 IP 并做了端口映射 → 在「对方地址」填公网地址\n"
                            f"  · 都没有 → 得填中继地址"
                        )
                    node = Client.connect(info.address, info.port,
                                          name=f"{nickname} #tunnel", password=password)
                self.app.post(self.log.add, "", f"已加入房间，我是 #{node.peer_id}", "system")
                tunnel = Tunnel(node, role="client", listen=options["listen"]).start()
            return node, tunnel, domain
        except Exception:
            if node is not None:
                try:
                    node.close()
                except Exception:
                    pass
            raise

    def _publish_domain(self) -> Optional[str]:
        """把配好的免费域名指向本机当前的 IPv6。返回域名，没配或失败返回 None。

        跑在后台线程里（要发 HTTPS 请求），所以除了 ``app.post`` 不碰任何
        tkinter 对象。

        **失败绝不能挡住隧道** —— 域名只是个方便，用 IP 照样能连。所以这里
        最差也只是记一行日志，不往外抛。
        """
        from .. import ddns

        config = ddns.load()
        if config is None:
            return None

        # 用模块顶部那一份 global_ipv6，别在这里再 import 一次 ——
        # 两个来源意味着两处可能不一致，测试也没法用一个补丁盖住。
        address = global_ipv6()
        if not address:
            self.app.post(self.log.add, "",
                          f"配了域名 {config.hostname}，但本机没有全球 IPv6，这次用不上。",
                          "error")
            return None

        self.app.post(self.log.add, "", f"正在更新域名 {config.hostname} …", "system")
        try:
            ddns.publish(address, config)
        except ddns.DdnsError as exc:
            self.app.post(self.log.add, "", f"域名更新失败：{exc}", "error")
            self.app.post(self.log.add, "", "这次直接用 IP 地址，对方照样连得上。", "system")
            return None
        except Exception as exc:  # pragma: no cover - 兜底，同样不能挡住隧道
            self.app.post(self.log.add, "", f"域名更新失败：{exc}", "error")
            return None

        self.app.post(self.log.add, "",
                      f"域名 {config.hostname} 已指向 {address}", "join")
        return config.hostname

    def _find_room(self, name: str, timeout: float = 6.0):
        scanner = Scanner(ttl=4.0)
        scanner.start()
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                info = scanner.find(name)
                if info is not None:
                    return info
                time.sleep(0.2)
            return None
        finally:
            scanner.stop()

    def _on_started(self, options: dict) -> None:
        self.toggle_button.configure(state="normal", text="停止隧道")
        self._set_form_enabled(False)
        self._running_options = options
        if options["is_server"]:
            self._share_host = global_ipv6()

        if options["is_server"]:
            where = f"{options['target'][0]}:{options['target'][1]}"
            self.status.configure(text=f"运行中 · 服务端 · 转发到 {where}")
            # 把"该让对方填什么"直接摆出来 —— 用户下一步就是要去告诉对方。
            # 那一长串 IPv6 让他自己念给对面听是不现实的，所以给的是
            # 一按就能复制的整段话。
            self.copy_button.configure(state="normal")
            self.log.add("", "点「复制连接信息」，把复制到的整段发给对方就行。", "join")
            for label, value in self._share_fields(options):
                if label == "房间密码":
                    # 屏幕上不打密码 —— 这个窗口经常被截图发出去问问题
                    self.log.add("", f"  {label}：（已包含在复制的内容里）", "join")
                    continue
                self.log.add("", f"  {label}：{value}", "join")
        else:
            where = f"{options['listen'][0]}:{options['listen'][1]}"
            self.status.configure(text=f"运行中 · 客户端 · 本机监听 {where}")
        self.app.set_status("隧道已就绪")

        if not options["is_server"]:
            host, port = options["listen"]
            self.log.add("", f"把要连的程序指向 {host}:{port} 就行。", "join")

        self._stop.clear()
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor.start()

    def _advertised_host(self) -> str:
        """该让对方连哪个地址。

        优先级：配了免费域名就用域名（固定、短、不会变），否则用 IPv6
        （一般不做 NAT，外面能直接连进来），再没有就退回局域网地址。

        地址在 ``_on_started`` 里算一次就存着：``global_ipv6()`` 会走
        ``getaddrinfo``，偶尔卡一下，主线程上不该随手反复调。
        """
        if self._share_domain:
            return self._share_domain
        if self._share_host:
            return self._share_host
        return self.node.address if self.node is not None else ""

    def _refresh_room_label(self) -> None:
        """那个框到底该叫「房间名」还是「房间号」。

        两种连法要填的东西根本不是一回事：局域网靠名字找房，地址内置时
        你填的是要校验的房间号。名字不改的话，对面看到「房间名」会去填
        "我的世界"这种，然后被房间号校验拦下来，还不知道为什么。
        """
        label = self._row_labels.get(self._room_row)
        if label is None:
            return
        try:
            label.configure(text="房间号" if servercfg.resolve() is not None else "房间名")
        except tk.TclError:  # pragma: no cover
            pass

    def on_show(self) -> None:
        """从别的页面切回来时刷新 —— 服务器地址可能在别处被改过。"""
        self._toggle_mode()

    def _open_ddns(self) -> None:
        from .ddns_dialog import DdnsDialog

        DdnsDialog(self, self.app)

    def _share_args(self, options: dict) -> dict:
        """对方连过来要用的参数。三种连法各是一套，不能混着说。

        优先级跟客户端那边一致：中继 > 默认服务器 > 裸地址。
        """
        node = self.node
        listen = options["target"][1]
        common = {"listen": listen, "password": options.get("password", "")}
        relay = options.get("relay")
        if relay is not None:
            room = node.relay.room_id if node.relay else options["room"]
            return dict(common, relay=format_addr(*relay), room=room)
        if servercfg.resolve() is not None:
            # 地址已经编进对方的 exe 了 —— 他只要房间号 + 口令
            return dict(common, room=node.room_id, server_default=True)
        return dict(common, address=format_addr(self._advertised_host(), node.port))

    def _share_fields(self, options: dict) -> list:
        """对方要在界面上填什么。文案在 tunnel.share_fields 里，共用一份。"""
        return share_fields(**self._share_args(options))

    def _share_text(self, options: dict) -> str:
        """给对方的整段话。文案在 tunnel.share_text 里，命令行版用同一份。"""
        if self.node is None:
            return ""
        return share_text(**self._share_args(options))

    def _copy_share(self) -> None:
        options = self._running_options
        if not options:
            return
        text = self._share_text(options)
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        # 不加这句，程序一关剪贴板就空了 —— tkinter 的内容要 update 之后
        # 才真正交给系统
        self.update()

        self.copy_button.configure(text="已复制到剪贴板")
        self._cancel_copy_reset()
        self._copy_reset_id = self.after(2000, self._reset_copy_button)
        self.log.add("", "连接信息已复制 —— 粘到微信/QQ 里发给对方就行。", "system")

    def _reset_copy_button(self) -> None:
        self._copy_reset_id = None
        try:
            self.copy_button.configure(text="复制连接信息")
        except tk.TclError:
            pass   # 界面已经关掉了

    def _cancel_copy_reset(self) -> None:
        """把待触发的"还原按钮文字"取消掉。

        不取消的话，界面销毁之后那个定时回调还会醒过来，然后报
        ``invalid command name ...`` —— 不影响功能，但日志里很难看。
        """
        if self._copy_reset_id is not None:
            try:
                self.after_cancel(self._copy_reset_id)
            except (tk.TclError, ValueError):
                pass
            self._copy_reset_id = None

    def _set_form_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for entry in self._entries:
            try:
                entry.configure(state=state)
            except tk.TclError:
                pass

    # ------------------------------------------------------------ 停止

    def _stop_tunnel(self, reason: str) -> None:
        self._stop.set()
        tunnel, node = self.tunnel, self.node
        self.tunnel, self.node = None, None
        self._running_options = None
        self._share_host = None
        self._share_domain = None

        def closer():
            for obj in (tunnel, node):
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass

        threading.Thread(target=closer, daemon=True).start()

        self._cancel_copy_reset()
        self.toggle_button.configure(text="启动隧道")
        self.copy_button.configure(state="disabled", text="复制连接信息")
        self.status.configure(text="未启动")
        self.traffic.configure(text="")
        self._set_form_enabled(True)
        if reason:
            self.log.add("", reason, "system")
        self.app.set_status("隧道已停止")

    def on_leave(self) -> None:
        """页面被切走时收尾 —— 不然会留个看不见的监听端口在后台。"""
        self._cancel_copy_reset()
        if self.tunnel is not None:
            self._stop_tunnel("离开页面，隧道已停止")

    # ------------------------------------------------------------ 状态刷新

    def _monitor_loop(self) -> None:
        last_up = last_down = 0
        last_at = time.monotonic()
        while not self._stop.wait(1.0):
            tunnel = self.tunnel
            if tunnel is None:
                return
            stats = tunnel.stats()
            now = time.monotonic()
            span = max(now - last_at, 0.001)
            up_rate = (stats["up"] - last_up) / span
            down_rate = (stats["down"] - last_down) / span
            last_up, last_down, last_at = stats["up"], stats["down"], now
            self.app.post(self._refresh, stats, up_rate, down_rate)

    def _refresh(self, stats: dict, up_rate: float, down_rate: float) -> None:
        if self.tunnel is None:
            return
        self.traffic.configure(
            text=f"活跃流 {stats['streams']}    "
                 f"上行 {_rate(up_rate)}   下行 {_rate(down_rate)}    "
                 f"累计 ↑{_size(stats['up'])} ↓{_size(stats['down'])}"
        )


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _rate(bytes_per_sec: float) -> str:
    return _size(bytes_per_sec) + "/s"
