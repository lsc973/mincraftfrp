"""主窗口。

**线程模型**（这里最容易出 bug，所以写清楚）：

tkinter 只能在主线程里碰。而网络事件全都来自后台线程 —— 收到的数据、
有人加入、连接断开。所以约定一条铁律：

    所有来自后台线程的界面更新，一律走 ``app.post(fn, *args)``。

``post`` 把调用塞进队列，主线程每 40ms 取一次。直接在后台线程里动控件
会随出各种玄学崩溃（有时候还能跑，有时候直接卡死），很难查。
"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Optional

from ..discovery import Scanner
from ..node import Client, Host, default_name
from .pages import DoctorDialog, RelayPage, RoomPage, StartPage
from .widgets import RoomList, apply_theme, enable_dpi_awareness, pick_ui_font

__all__ = ["LanlinkApp", "main"]

#: 界面刷新间隔。40ms ≈ 25fps，人眼看不出延迟，也不会空转烧 CPU。
_UI_POLL_MS = 40


class LanlinkApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("lanlink —— 局域网联机工具")
        self.geometry("920x620")
        self.minsize(760, 520)

        self.font_family = pick_ui_font(self)
        apply_theme(self, self.font_family)

        self.nickname = tk.StringVar(value=default_name())
        self.node = None                 # 当前的 Host 或 Client
        self._queue: "queue.Queue" = queue.Queue()
        self._scanner: Optional[Scanner] = None
        self._closing = False

        self._build_pages()
        self._build_statusbar()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_job: Optional[str] = None
        self._drain_queue()

        self.show_page("start")

    # ------------------------------------------------------------ 布局

    def _build_pages(self) -> None:
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        self.pages = {}
        for name, factory in (
            ("start", lambda: StartPage(container, self)),
            ("room", lambda: RoomPage(container, self)),
            ("relay", lambda: RelayPage(container, self)),
        ):
            frame = factory()
            frame.grid(row=0, column=0, sticky="nsew")
            self.pages[name] = frame

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, padding=(10, 4))
        bar.pack(fill="x", side="bottom")
        self._status = ttk.Label(bar, text="就绪", style="Hint.TLabel")
        self._status.pack(side="left")

    def show_page(self, name: str) -> None:
        current = self.pages.get(name)
        if current is None:
            return
        # 离开中继页时把服务器停掉，免得后台还在监听
        relay_page = self.pages.get("relay")
        if name != "relay" and isinstance(relay_page, RelayPage):
            relay_page.on_leave()
        current.tkraise()

    def set_status(self, text: str) -> None:
        self._status.configure(text=text)

    # ------------------------------------------------------------ 线程安全

    def post(self, fn: Callable, *args) -> None:
        """把 ``fn(*args)`` 排到主线程执行。后台线程调界面必须走这里。"""
        self._queue.put((fn, args))

    def _drain_queue(self) -> None:
        while True:
            try:
                fn, args = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except tk.TclError:
                return  # 窗口已经销毁了，别再排下一次
            except Exception as exc:  # 单个回调出错不该拖垮整个界面
                self.set_status(f"界面回调出错：{exc}")
        if not self._closing:
            self._poll_job = self.after(_UI_POLL_MS, self._drain_queue)

    def _run_async(self, work: Callable, on_done: Callable, busy: str = "处理中…") -> None:
        """在后台线程跑 ``work``，完成后把结果丢回主线程给 ``on_done``。"""
        self.set_status(busy)

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:
                self.post(self._async_failed, exc)
                return
            self.post(on_done, result)

        threading.Thread(target=runner, daemon=True).start()

    def _async_failed(self, exc: Exception) -> None:
        self.set_status("操作失败")
        messagebox.showerror("出错了", f"{type(exc).__name__}: {exc}", parent=self)

    # ------------------------------------------------------------ 创建房间

    def open_create_room(self) -> None:
        CreateRoomDialog(self, self)

    def create_room(self, options: dict) -> None:
        nickname = self.nickname.get().strip() or default_name()

        def work():
            host = Host(
                options["room_name"],
                name=nickname,
                port=options["port"],
                password=options["password"],
                max_players=options["max_players"],
            ).start()
            if options.get("relay_addr"):
                host.attach_relay(
                    *options["relay_addr"],
                    room_id=options.get("relay_room") or host.room_id,
                    token=options.get("relay_token", ""),
                )
            return host

        def done(host) -> None:
            self.node = host
            self.pages["room"].bind_node(host, is_host=True)
            self.show_page("room")
            self.set_status(f"房间已创建，房间号 {host.room_id}")

        def work_with_relay_fallback():
            try:
                return work()
            except Exception as exc:
                # 中继挂了不该让整个房间起不来 —— 局域网内照样能用
                if options.get("relay_addr"):
                    host = Host(
                        options["room_name"],
                        name=nickname,
                        port=options["port"],
                        password=options["password"],
                        max_players=options["max_players"],
                    ).start()
                    self.post(
                        self.set_status,
                        f"中继连接失败（{exc}），房间仅在局域网内可用",
                    )
                    return host
                raise

        self._run_async(work_with_relay_fallback, done, "正在创建房间…")

    # ------------------------------------------------------------ 加入房间

    def open_join_room(self) -> None:
        JoinRoomDialog(self, self)

    def join_room(self, options: dict) -> None:
        nickname = self.nickname.get().strip() or default_name()

        def work():
            if options["mode"] == "relay":
                return Client.join_via_relay(
                    *options["relay_addr"],
                    options["room_id"],
                    name=nickname,
                    password=options["password"],
                    token=options["relay_token"],
                )
            return Client.connect(
                options["host"], options["port"], name=nickname, password=options["password"]
            )

        def done(client) -> None:
            self.node = client
            self.pages["room"].bind_node(client, is_host=False)
            self.show_page("room")
            self.set_status(f"已加入房间，我是 #{client.peer_id}")

        self._run_async(work, done, "正在连接…")

    # ------------------------------------------------------------ 中继 / 自检

    def open_relay(self) -> None:
        self.show_page("relay")

    def run_doctor(self) -> None:
        DoctorDialog(self, self)

    # ------------------------------------------------------------ 离开 / 关闭

    def leave_room(self) -> None:
        if self.node is None:
            self.show_page("start")
            return
        if not messagebox.askyesno("确认", "确定要离开房间吗？", parent=self):
            return
        node, self.node = self.node, None
        # 关闭可能要等心跳线程收尾，别卡住界面
        threading.Thread(target=node.close, daemon=True).start()
        self.pages["room"].chat.clear()
        self.show_page("start")
        self.set_status("已离开房间")

    def on_connection_lost(self) -> None:
        """连接意外断开（不是用户主动离开）。"""
        if self.node is None:
            return
        self.node = None
        self.set_status("连接已断开")
        messagebox.showwarning("连接断开", "和房间的连接已经断开。", parent=self)
        self.show_page("start")

    def _on_close(self) -> None:
        self._closing = True
        # 取消挂起的轮询任务。不取消的话，窗口销毁后那个 after 还会到期触发，
        # Tcl 会甩一句 'invalid command name ..._drain_queue' 到 stderr。
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        if self._scanner is not None:
            self._scanner.stop()
            self._scanner = None
        relay_page = self.pages.get("relay")
        if isinstance(relay_page, RelayPage):
            relay_page.on_leave()
        if self.node is not None:
            try:
                self.node.close()
            except Exception:
                pass
        self.destroy()


# ====================================================================== 创建房间对话框


class CreateRoomDialog(tk.Toplevel):
    def __init__(self, master: LanlinkApp, app: LanlinkApp) -> None:
        super().__init__(master)
        self.app = app
        self.title("创建房间")
        self.resizable(False, False)
        self.transient(master)

        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)

        self.room_name = tk.StringVar(value=f"{app.nickname.get()} 的房间")
        self.port = tk.StringVar(value="0")
        self.password = tk.StringVar()
        self.max_players = tk.StringVar(value="16")

        self._row(body, 0, "房间名", ttk.Entry(body, textvariable=self.room_name, width=30))
        self._row(body, 1, "端口", ttk.Entry(body, textvariable=self.port, width=12))
        self._row(body, 2, "房间密码", ttk.Entry(body, textvariable=self.password, width=30, show="•"))
        self._row(body, 3, "人数上限", ttk.Entry(body, textvariable=self.max_players, width=12))
        ttk.Label(
            body,
            text="端口填 0 让系统自动挑一个空闲的；密码留空表示不设密码。",
            style="Hint.TLabel",
        ).grid(row=4, column=1, sticky="w", pady=(0, 12))

        self.use_relay = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            body, text="挂到公网中继（让不在同一局域网的人也能进）",
            variable=self.use_relay, command=self._toggle_relay,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 6))

        self.relay_addr = tk.StringVar()
        self.relay_room = tk.StringVar()
        self.relay_token = tk.StringVar()
        self._relay_rows = [
            ("中继地址", ttk.Entry(body, textvariable=self.relay_addr, width=30)),
            ("中继房间号", ttk.Entry(body, textvariable=self.relay_room, width=30)),
            ("中继口令", ttk.Entry(body, textvariable=self.relay_token, width=30, show="•")),
        ]
        for index, (label, widget) in enumerate(self._relay_rows):
            self._row(body, 6 + index, label, widget)
        ttk.Label(
            body, text="中继地址格式 host:端口，比如 1.2.3.4:9000；房间号留空则用本机房间号。",
            style="Hint.TLabel",
        ).grid(row=9, column=1, sticky="w")
        self._toggle_relay()

        buttons = ttk.Frame(body)
        buttons.grid(row=10, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="创建", command=self._submit).pack(side="right")

        self.bind("<Return>", lambda _e: self._submit())
        self.bind("<Escape>", lambda _e: self.destroy())
        self._center(master)
        self.grab_set()

    @staticmethod
    def _row(parent: ttk.Frame, row: int, label: str, widget: tk.Widget) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
        widget.grid(row=row, column=1, sticky="w", pady=4)

    def _toggle_relay(self) -> None:
        state = "normal" if self.use_relay.get() else "disabled"
        for _label, widget in self._relay_rows:
            widget.configure(state=state)

    def _center(self, master: tk.Misc) -> None:
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _submit(self) -> None:
        name = self.room_name.get().strip()
        if not name:
            messagebox.showinfo("缺房间名", "给房间起个名字吧。", parent=self)
            return
        try:
            port = int(self.port.get() or 0)
            max_players = int(self.max_players.get() or 16)
        except ValueError:
            messagebox.showerror("参数不对", "端口和人数上限必须是数字。", parent=self)
            return
        if max_players < 2:
            messagebox.showerror("参数不对", "人数上限至少是 2。", parent=self)
            return

        options = {
            "room_name": name,
            "port": port,
            "password": self.password.get(),
            "max_players": max_players,
        }

        if self.use_relay.get():
            raw = self.relay_addr.get().strip()
            if ":" not in raw:
                messagebox.showerror("中继地址不对", "格式应该是 host:端口，比如 1.2.3.4:9000。", parent=self)
                return
            host, _, port_text = raw.rpartition(":")
            try:
                options["relay_addr"] = (host, int(port_text))
            except ValueError:
                messagebox.showerror("中继地址不对", f"端口不是数字：{port_text}", parent=self)
                return
            options["relay_room"] = self.relay_room.get().strip()
            options["relay_token"] = self.relay_token.get().strip()

        self.destroy()
        self.app.create_room(options)


# ====================================================================== 加入房间对话框


class JoinRoomDialog(tk.Toplevel):
    """三个标签页：局域网扫描 / 手动地址 / 公网中继。"""

    def __init__(self, master: LanlinkApp, app: LanlinkApp) -> None:
        super().__init__(master)
        self.app = app
        self.title("加入房间")
        self.geometry("560x460")
        self.transient(master)

        self.password = tk.StringVar()
        self.manual_addr = tk.StringVar()
        self.relay_addr = tk.StringVar()
        self.relay_room = tk.StringVar()
        self.relay_token = tk.StringVar()

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=14, pady=(14, 8))
        notebook.add(self._build_lan_tab(notebook), text="  局域网  ")
        notebook.add(self._build_manual_tab(notebook), text="  手动地址  ")
        notebook.add(self._build_relay_tab(notebook), text="  公网中继  ")

        bottom = ttk.Frame(self, padding=(14, 0, 14, 14))
        bottom.pack(fill="x")
        ttk.Label(bottom, text="房间密码").pack(side="left")
        ttk.Entry(bottom, textvariable=self.password, width=18, show="•").pack(side="left", padx=6)
        ttk.Button(bottom, text="取消", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(bottom, text="加入", command=lambda: self._submit(notebook.index("current"))).pack(
            side="right"
        )

        self._scanner: Optional[Scanner] = None
        self._start_scan()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.grab_set()

    # -------------------------------------------------------- 三个标签页

    def _build_lan_tab(self, parent: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=12)
        ttk.Label(
            frame, text="自动搜索同一局域网内正在广播的房间。", style="Hint.TLabel"
        ).pack(anchor="w", pady=(0, 8))
        self.rooms = RoomList(frame)
        self.rooms.pack(fill="both", expand=True)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(8, 0))
        self.scan_status = ttk.Label(row, text="正在搜索…", style="Hint.TLabel")
        self.scan_status.pack(side="left")
        ttk.Button(row, text="立即刷新", command=self._rescan).pack(side="right")
        self.rooms.tree.bind("<Double-1>", lambda _e: self._submit(0))
        return frame

    def _build_manual_tab(self, parent: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=20)
        ttk.Label(frame, text="直接填主机的地址。", style="Hint.TLabel").pack(anchor="w")
        ttk.Label(
            frame, text="主机开房时会显示“局域网地址”，照着填就行。", style="Hint.TLabel"
        ).pack(anchor="w", pady=(0, 14))
        row = ttk.Frame(frame)
        row.pack(anchor="w")
        ttk.Label(row, text="地址").pack(side="left")
        ttk.Entry(row, textvariable=self.manual_addr, width=30).pack(side="left", padx=8)
        ttk.Label(frame, text="格式 host:端口，比如 192.168.1.10:50001", style="Hint.TLabel").pack(
            anchor="w", pady=(8, 0)
        )
        return frame

    def _build_relay_tab(self, parent: tk.Misc) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=20)
        ttk.Label(
            frame, text="通过公网中继加入，适合不在同一局域网的玩家。", style="Hint.TLabel"
        ).pack(anchor="w", pady=(0, 14))
        for label, var, show in (
            ("中继地址", self.relay_addr, ""),
            ("房间号", self.relay_room, ""),
            ("中继口令", self.relay_token, "•"),
        ):
            row = ttk.Frame(frame)
            row.pack(anchor="w", pady=4)
            ttk.Label(row, text=label, width=10).pack(side="left")
            ttk.Entry(row, textvariable=var, width=26, show=show).pack(side="left")
        ttk.Label(
            frame, text="中继地址格式 host:端口；房间号是房主在那台中继上用的号。", style="Hint.TLabel"
        ).pack(anchor="w", pady=(10, 0))
        return frame

    # -------------------------------------------------------- 扫描

    def _start_scan(self) -> None:
        self._scanner = Scanner(ttl=6.0)
        self._scanner.start()
        self._poll()

    def _rescan(self) -> None:
        if self._scanner is not None:
            self._scanner.probe()
            self.scan_status.configure(text="正在搜索…")

    def _poll(self) -> None:
        if self._scanner is None:
            return
        try:
            rooms = self._scanner.rooms()
        except Exception:
            rooms = []
        self.rooms.set_rooms(rooms)
        self.scan_status.configure(
            text=f"找到 {len(rooms)} 个房间" if rooms else "还没找到房间，确认主机已开房、且在同一网段"
        )
        self._poll_job = self.after(1000, self._poll)

    def _close(self) -> None:
        # 先取消轮询再销毁，不然到期的 after 会撞上已经没了的控件
        job = getattr(self, "_poll_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
            self._poll_job = None
        if self._scanner is not None:
            self._scanner.stop()
            self._scanner = None
        self.destroy()

    # -------------------------------------------------------- 提交

    def _submit(self, tab_index: int) -> None:
        password = self.password.get()

        if tab_index == 0:
            room = self.rooms.selected_room()
            if room is None:
                messagebox.showinfo("还没选房间", "在列表里点一个房间，或者换到“手动地址”。", parent=self)
                return
            options = {
                "mode": "direct",
                "host": room.address,
                "port": room.port,
                "password": password,
            }
        elif tab_index == 1:
            raw = self.manual_addr.get().strip()
            if ":" not in raw:
                messagebox.showerror("地址不对", "格式应该是 host:端口，比如 192.168.1.10:50001。", parent=self)
                return
            host, _, port_text = raw.rpartition(":")
            try:
                port = int(port_text)
            except ValueError:
                messagebox.showerror("地址不对", f"端口不是数字：{port_text}", parent=self)
                return
            options = {"mode": "direct", "host": host, "port": port, "password": password}
        else:
            raw = self.relay_addr.get().strip()
            room_id = self.relay_room.get().strip()
            if ":" not in raw:
                messagebox.showerror("中继地址不对", "格式应该是 host:端口。", parent=self)
                return
            if not room_id:
                messagebox.showerror("缺房间号", "走中继必须填房间号。", parent=self)
                return
            host, _, port_text = raw.rpartition(":")
            try:
                port = int(port_text)
            except ValueError:
                messagebox.showerror("中继地址不对", f"端口不是数字：{port_text}", parent=self)
                return
            options = {
                "mode": "relay",
                "relay_addr": (host, port),
                "room_id": room_id,
                "relay_token": self.relay_token.get().strip(),
                "password": password,
            }

        self._close()
        self.app.join_room(options)


# ====================================================================== 入口


def log_file_path() -> str:
    """日志文件位置。和 ``_setup_logging`` 用的是同一个路径。"""
    import tempfile
    from pathlib import Path

    return str(Path(tempfile.gettempdir()) / "lanlink.log")


def _setup_logging() -> None:
    """把日志写到文件。

    打包成 exe 之后没有控制台，``sys.stdout`` / ``sys.stderr`` 都是 None。
    这时候 logging 默认那套（写 stderr）会出问题，而且用户报问题时也无从查起。
    所以统一落到临时目录的 lanlink.log，让用户能把它发过来。
    """
    import logging
    import os

    # 没有控制台时把标准流补成空设备，免得别处的 print 崩掉
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass

    try:
        logging.basicConfig(
            filename=log_file_path(),
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            encoding="utf-8",
        )
    except Exception:
        logging.basicConfig(level=logging.CRITICAL)  # 写不了文件也别让程序起不来


def main() -> int:
    _setup_logging()
    enable_dpi_awareness()
    try:
        app = LanlinkApp()
    except Exception as exc:
        # 双击运行时崩溃了屏幕上什么都没有，至少弹个框说一声。
        _report_fatal(exc)
        return 1
    try:
        app.mainloop()
    except KeyboardInterrupt:
        pass
    return 0


def _report_fatal(exc: Exception) -> None:
    import traceback

    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        from .. import __version__
    except Exception:
        __version__ = "?"

    message = (
        f"{type(exc).__name__}: {exc}\n\n"
        "常见原因：没装 tkinter，或者显卡/远程桌面环境下创建窗口失败。\n\n"
        f"完整日志：{log_file_path()}\n\n"
        f"{detail[-700:]}"
    )
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(f"lanlink {__version__} 启动失败", message)
        root.destroy()
    except Exception:
        try:
            print(detail, file=sys.stderr)
        except Exception:
            pass
