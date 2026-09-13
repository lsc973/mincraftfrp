"""各个页面和对话框。

页面都拿一个 ``app`` 引用，用到它的这几个能力：

* ``app.post(fn, *args)`` —— 把调用排到主线程（**网络回调必须走这里**）
* ``app.show_page(name)`` / ``app.set_status(text)``
* ``app.nickname`` —— 昵称 StringVar

tkinter 不是线程安全的：所有来自网络线程的更新都必须经过 ``app.post``，
否则会随出各种玄学崩溃。
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import List, Optional

from ..discovery import RoomInfo, scan
from ..node import PeerInfo
from ..text import encode_text
from .widgets import ChatView, MemberList, RoomList

__all__ = ["StartPage", "RoomPage", "RelayPage", "DoctorDialog"]


# ====================================================================== 起始页


class StartPage(ttk.Frame):
    """选择当房主、加入别人、还是当中继。"""

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, padding=32)
        self.app = app

        ttk.Label(self, text="lanlink", style="Title.TLabel").pack(pady=(8, 2))
        ttk.Label(
            self, text="局域网优先的联机工具", style="Subtitle.TLabel"
        ).pack(pady=(0, 24))

        nick_row = ttk.Frame(self)
        nick_row.pack(pady=(0, 24))
        ttk.Label(nick_row, text="我的昵称：").pack(side="left")
        ttk.Entry(nick_row, textvariable=app.nickname, width=22).pack(side="left")

        for text, command, style in (
            ("创建房间（当房主）", app.open_create_room, "Big.TButton"),
            ("加入房间", app.open_join_room, "Big.TButton"),
            ("我当中继服务器", app.open_relay, "Big.TButton"),
            ("端口转发隧道", app.open_tunnel, "Big.TButton"),
        ):
            ttk.Button(self, text=text, command=command, style=style, width=24).pack(pady=6)

        ttk.Label(
            self,
            text="隧道 = 把房间当成一根网线，让异地的人连上你本机的现有服务",
            style="Hint.TLabel",
        ).pack(pady=(10, 0))

        ttk.Separator(self).pack(fill="x", pady=20)
        ttk.Button(self, text="环境自检（连不上先点这里）", command=app.run_doctor).pack()
        ttk.Label(
            self,
            text="检查防火墙、网段、端口占用，并做一次真实的收发自测",
            style="Hint.TLabel",
        ).pack(pady=(6, 0))


# ====================================================================== 房间页


class RoomPage(ttk.Frame):
    """房间主界面：左边成员，右边聊天。"""

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, padding=10)
        self.app = app
        self._input_history: List[str] = []
        self._history_pos = 0

        self._build_header()
        self._build_body()
        self._build_input()

    # ------------------------------------------------------------ 布局

    def _build_header(self) -> None:
        header = ttk.Frame(self)
        header.pack(fill="x", pady=(0, 8))

        left = ttk.Frame(header)
        left.pack(side="left", fill="x", expand=True)
        self.room_label = ttk.Label(left, text="", style="Banner.TLabel")
        self.room_label.pack(anchor="w")
        self.addr_label = ttk.Label(left, text="", style="Hint.TLabel")
        self.addr_label.pack(anchor="w")

        right = ttk.Frame(header)
        right.pack(side="right")
        ttk.Button(right, text="复制房间号", command=self._copy_room_id).pack(side="left", padx=4)
        ttk.Button(right, text="离开房间", command=self.app.leave_room).pack(side="left")

    def _build_body(self) -> None:
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)

        left = ttk.Frame(panes, padding=(0, 0, 8, 0))
        ttk.Label(left, text="房间成员").pack(anchor="w")
        self.members = MemberList(left)
        self.members.pack(fill="both", expand=True)
        self.member_hint = ttk.Label(left, text="", style="Hint.TLabel")
        self.member_hint.pack(anchor="w", pady=(6, 0))
        self.kick_button = ttk.Button(left, text="踢出选中成员", command=self._kick_selected)

        right = ttk.Frame(panes)
        ttk.Label(right, text="消息").pack(anchor="w")
        self.chat = ChatView(right, family=self.app.font_family)
        self.chat.pack(fill="both", expand=True)

        panes.add(left, weight=1)
        panes.add(right, weight=3)

    def _build_input(self) -> None:
        box = ttk.Frame(self)
        box.pack(fill="x", pady=(8, 0))

        self.target = tk.StringVar(value="broadcast")
        ttk.Radiobutton(box, text="广播", variable=self.target, value="broadcast").pack(side="left")
        ttk.Radiobutton(box, text="私聊选中", variable=self.target, value="private").pack(
            side="left", padx=(6, 10)
        )

        self.entry = ttk.Entry(box)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda _event: self.send())
        self.entry.bind("<Up>", lambda _event: self._recall(-1))
        self.entry.bind("<Down>", lambda _event: self._recall(1))

        ttk.Button(box, text="发送", command=self.send).pack(side="left", padx=(8, 0))

    # ------------------------------------------------------------ 外部调用

    def bind_node(self, node, *, is_host: bool) -> None:
        """房间建立/加入之后调用，把事件接到界面上。"""
        self.is_host = is_host
        self.chat.clear()
        # 上一个房间没发出去的半截话不该带进新房间
        self.entry.delete(0, "end")

        if is_host:
            room = node.room_info()
            self.room_label.configure(text=f"{room.room_name}（我是房主）")
            extra = f"房间号 {room.room_id}   局域网地址 {room.address}:{room.port}"
            if node.relay is not None:
                extra += f"   中继 {node.relay.public_addr} / {node.relay.room_id}"
            self.addr_label.configure(text=extra)
            self.chat.add("", "房间已创建，把房间号告诉朋友就行了。", "system")
            self.kick_button.pack(anchor="w", pady=(6, 0))
        else:
            room = node.room
            self.room_label.configure(text=getattr(room, "room_name", "已加入房间"))
            self.addr_label.configure(
                text=f"房间号 {getattr(room, 'room_id', '?')}   我是 #{node.peer_id}"
            )
            self.chat.add("", f"已加入房间，我是 #{node.peer_id}。", "system")
            self.kick_button.pack_forget()

        node.on("peer_join", lambda peer: self.app.post(self._on_join, peer))
        node.on("peer_leave", lambda peer, reason: self.app.post(self._on_leave, peer, reason))
        node.on("data", lambda s, d, j: self.app.post(self._on_data, s, d, j))
        node.on("close", lambda: self.app.post(self._on_node_closed))
        self.refresh_members()
        self.entry.focus_set()

    def refresh_members(self) -> None:
        node = self.app.node
        if node is None:
            return
        self.members.set_members(
            node.peers,
            me_id=getattr(node, "peer_id", -1) if not self.is_host else 0,
            host_name=node.name if self.is_host else getattr(node.room, "host_name", "主机"),
            is_host=self.is_host,
        )
        self.member_hint.configure(text=f"共 {self.members.count()} 人")

    # ------------------------------------------------------------ 事件

    def _on_join(self, peer: PeerInfo) -> None:
        self.chat.add("", f"{peer.name} 加入了房间（{peer.via}）", "join")
        self.refresh_members()

    def _on_leave(self, peer: PeerInfo, reason: str) -> None:
        self.chat.add("", f"{peer.name} 离开了房间", "leave")
        self.refresh_members()

    def _on_data(self, source: int, data: bytes, is_json: bool) -> None:
        who = self._name_of(source)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = f"<{len(data)} 字节二进制数据>"
        self.chat.add(who, text, "me" if source == self._my_id() else "other")

    def _on_node_closed(self) -> None:
        self.chat.add("", "连接已断开。", "error")
        self.app.post(self.app.on_connection_lost)

    def _my_id(self) -> int:
        node = self.app.node
        if node is None:
            return -1
        return 0 if self.is_host else getattr(node, "peer_id", -1)

    def _name_of(self, peer_id: int) -> str:
        if peer_id == 0:
            node = self.app.node
            return node.name if self.is_host else getattr(node.room, "host_name", "主机")
        if peer_id == self._my_id():
            return "我"
        node = self.app.node
        info = node.peers.get(peer_id) if node else None
        return f"{info.name}#{peer_id}" if info else f"#{peer_id}"

    # ------------------------------------------------------------ 发送

    def send(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        node = self.app.node
        if node is None:
            return

        payload = encode_text(text)
        if self.target.get() == "private":
            target_id = self.members.selected_peer()
            if target_id is None:
                messagebox.showinfo("还没选人", "先在左边成员列表里点一个人，再发私聊。", parent=self)
                return
            if target_id == self._my_id():
                messagebox.showinfo("选错人了", "不能给自己发私聊。", parent=self)
                return
            ok = node.send_to(target_id, payload)
            if ok:
                self.chat.add(f"我 → {self._name_of(target_id)}", text, "me")
            else:
                self.chat.add("", f"发送失败：找不到成员 #{target_id}", "error")
        else:
            ok = node.broadcast(payload)
            if ok or self.is_host:
                self.chat.add("我", text, "me")
            else:
                self.chat.add("", "发送失败：连接已断开", "error")

        self._input_history.append(text)
        self._history_pos = len(self._input_history)
        self.entry.delete(0, "end")

    def _recall(self, step: int) -> None:
        """上下键翻历史输入。"""
        if not self._input_history:
            return
        self._history_pos = max(0, min(len(self._input_history), self._history_pos + step))
        self.entry.delete(0, "end")
        if self._history_pos < len(self._input_history):
            self.entry.insert(0, self._input_history[self._history_pos])

    def _kick_selected(self) -> None:
        peer_id = self.members.selected_peer()
        if peer_id in (None, 0):
            messagebox.showinfo("还没选人", "先选中一个成员。", parent=self)
            return
        node = self.app.node
        if node is None:
            return
        if messagebox.askyesno("确认", f"把 {self._name_of(peer_id)} 请出房间？", parent=self):
            node.kick(peer_id)

    def _copy_room_id(self) -> None:
        """复制"别人加入时要填的那个号"。

        挂了中继的时候，异地的人要填的是**中继房间号**，不是本机房间号 ——
        两个号可以不一样（创建房间时能自定义中继房间号）。以前这里无条件
        复制的本机号，主人一自定义，发给朋友的号就是错的。
        """
        node = self.app.node
        if node is None:
            return

        relay = getattr(node, "relay", None)
        if self.is_host and relay is not None and relay.room_id:
            room_id, where = relay.room_id, "中继房间号"
        elif self.is_host:
            room_id, where = node.room_id, "房间号"
        else:
            room_id = getattr(node.room, "room_id", "")
            where = "房间号"
            if not room_id:
                # 客户端自己就是经中继进来的，用自己看到的那个号
                room_id = getattr(node.room, "relay_room", "") or ""
        if not room_id:
            return

        self.clipboard_clear()
        self.clipboard_append(room_id)
        self.app.set_status(f"{where} {room_id} 已复制到剪贴板")


# ====================================================================== 中继页


class RelayPage(ttk.Frame):
    """跑一台中继服务器。"""

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, padding=24)
        self.app = app
        self.server = None
        self._monitor: Optional[threading.Thread] = None
        self._stop = threading.Event()

        ttk.Label(self, text="中继服务器", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            self,
            text="给不在同一局域网的玩家当中转站。放在有公网 IP 的机器上。",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 16))

        form = ttk.Frame(self)
        form.pack(fill="x")

        self.port = tk.StringVar(value="9000")
        self.token = tk.StringVar()
        self.max_clients = tk.StringVar(value="64")

        self._row(form, 0, "监听端口", ttk.Entry(form, textvariable=self.port, width=12))
        self._row(form, 1, "接入口令", ttk.Entry(form, textvariable=self.token, width=28))
        self._row(form, 2, "每房上限", ttk.Entry(form, textvariable=self.max_clients, width=12))

        ttk.Label(
            form,
            text="口令留空 = 任何人可接入。设了口令的话，主机和客户端两边都要填一样的。",
            style="Hint.TLabel",
        ).grid(row=3, column=1, sticky="w", pady=(2, 12))

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=(0, 12))
        self.toggle_button = ttk.Button(buttons, text="启动中继", command=self._toggle)
        self.toggle_button.pack(side="left")
        ttk.Button(buttons, text="返回", command=lambda: self.app.show_page("start")).pack(
            side="left", padx=8
        )

        self.status = ttk.Label(self, text="未启动", style="Banner.TLabel")
        self.status.pack(anchor="w", pady=(0, 8))

        ttk.Label(self, text="当前房间").pack(anchor="w")
        self.rooms = RoomList(self)
        self.rooms.pack(fill="both", expand=True)

    @staticmethod
    def _row(parent: ttk.Frame, row: int, label: str, widget: tk.Widget) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
        widget.grid(row=row, column=1, sticky="w", pady=4)

    def _toggle(self) -> None:
        if self.server is None:
            self._start()
        else:
            self._stop_server()

    def _start(self) -> None:
        from ..relay import RelayServer

        try:
            port = int(self.port.get())
            max_clients = int(self.max_clients.get())
        except ValueError:
            messagebox.showerror("参数不对", "端口和人数上限必须是数字。", parent=self)
            return

        try:
            self.server = RelayServer(
                "0.0.0.0", port, token=self.token.get().strip(), max_clients_per_room=max_clients
            ).start()
        except OSError as exc:
            messagebox.showerror("启动失败", f"端口 {port} 起不来：{exc}", parent=self)
            self.server = None
            return

        self.toggle_button.configure(text="停止中继")
        self.status.configure(text=f"运行中：0.0.0.0:{self.server.port}")
        self.app.set_status(f"中继已启动，监听 {self.server.port}")

        self._stop.clear()
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor.start()

    def _monitor_loop(self) -> None:
        while not self._stop.wait(2.0):
            server = self.server
            if server is None:
                return
            try:
                rooms = server.rooms
                stats = server.stats()
            except Exception:
                return
            self.app.post(self._refresh, rooms, stats["clients"])

    def _refresh(self, rooms, clients: int) -> None:
        if self.server is None:
            return
        self.rooms.set_rooms(rooms)
        self.status.configure(text=f"运行中：0.0.0.0:{self.server.port}    {len(rooms)} 间房 / {clients} 人在线")

    def _stop_server(self) -> None:
        self._stop.set()
        if self.server is not None:
            self.server.close()
            self.server = None
        self.toggle_button.configure(text="启动中继")
        self.status.configure(text="未启动")
        self.rooms.set_rooms([])
        self.app.set_status("中继已停止")

    def on_leave(self) -> None:
        """页面被切走时收尾。"""
        if self.server is not None:
            self._stop_server()


# ====================================================================== 自检


class DoctorDialog(tk.Toplevel):
    """跑环境自检并把结果摊开显示。"""

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master)
        self.app = app
        self.title("环境自检")
        self.geometry("620x460")
        self.transient(master)

        self.text = tk.Text(self, wrap="word", height=20, padx=12, pady=10, relief="flat")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.text.tag_configure("pass", foreground="#2e7d32")
        self.text.tag_configure("fail", foreground="#c0392b")
        self.text.tag_configure("warn", foreground="#b26a00")
        self.text.insert("end", "正在检查…\n\n", "warn")
        self.text.configure(state="disabled")

        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        from ..doctor import run_checks, render_report

        try:
            checks = run_checks()
            report = render_report(checks)
            marks = [(c.mark, c.name) for c in checks]
        except Exception as exc:  # 自检本身出错也要显示出来，不能白屏
            report = f"自检执行失败：{type(exc).__name__}: {exc}"
            marks = []
        # 必须走 app.post 回主线程。self.after() 也是 Tcl 调用，
        # 在后台线程里调同样会抛 "main thread is not in main loop"。
        self.app.post(self._show, report, marks)

    def _show(self, report: str, marks) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for line in report.splitlines():
            tag = ""
            if line.startswith("[通过]"):
                tag = "pass"
            elif line.startswith("[失败]"):
                tag = "fail"
            elif line.startswith("[警告]"):
                tag = "warn"
            self.text.insert("end", line + "\n", tag)
        self.text.configure(state="disabled")
