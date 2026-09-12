"""GUI 的公共组件和平台适配。

这一层不依赖应用对象，只提供可复用的零件：

* ``enable_dpi_awareness`` / ``pick_ui_font`` —— Windows 高分屏和中文显示
* ``ChatView`` —— 聊天记录区，按消息类型上色
* ``MemberList`` —— 成员表
* ``Card`` / ``FormRow`` —— 简单排版
"""

from __future__ import annotations

import platform
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Dict, List, Optional

__all__ = [
    "enable_dpi_awareness",
    "pick_ui_font",
    "apply_theme",
    "ChatView",
    "MemberList",
    "RoomList",
    "Card",
]

#: 优先尝试的中文字体。Windows 上雅黑最稳，其次微软正黑/苹方，最后退回系统默认。
_FONT_CANDIDATES = [
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "微软雅黑",
    "PingFang SC",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "SimHei",
    "Segoe UI",
]


def enable_dpi_awareness() -> None:
    """Windows 上开启 DPI 感知。

    不开的话，在缩放 125%/150% 的屏幕上整个窗口会被系统拉伸，字是糊的。
    """
    if platform.system() != "Windows":
        return
    try:
        import ctypes

        # 1 = PROCESS_SYSTEM_DPI_AWARE；比 SetProcessDPIAware 更彻底
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[name-defined]
        except Exception:
            pass  # 老系统上没有就算了，不影响功能


def pick_ui_font(root: tk.Misc) -> str:
    """从候选里挑一个当前系统真的装了的字体。"""
    try:
        available = {name.lower() for name in tkfont.families(root)}
    except tk.TclError:
        return "TkDefaultFont"
    for name in _FONT_CANDIDATES:
        if name.lower() in available:
            return name
    return "TkDefaultFont"


def apply_theme(root: tk.Tk, family: str) -> None:
    """统一字体和 ttk 样式，顺手把默认字号调大一点。"""
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            tkfont.nametofont(name).configure(family=family, size=10)
        except tk.TclError:
            pass

    style = ttk.Style(root)
    # vista 在 Windows 上观感最好；没有就退回 clam
    for theme in ("vista", "clam", "default"):
        if theme in style.theme_names():
            style.theme_use(theme)
            break

    style.configure("Title.TLabel", font=(family, 16, "bold"))
    style.configure("Subtitle.TLabel", font=(family, 10), foreground="#666666")
    style.configure("Hint.TLabel", font=(family, 9), foreground="#888888")
    style.configure("Banner.TLabel", font=(family, 11, "bold"))
    style.configure("Big.TButton", font=(family, 11), padding=(16, 10))
    style.configure("Member.Treeview", rowheight=24)


# ---------------------------------------------------------------- 聊天区


class ChatView(ttk.Frame):
    """聊天记录。自己发的、别人发的、系统提示用不同颜色区分。"""

    _TAGS = {
        "me": {"foreground": "#0b6bcb"},
        "other": {"foreground": "#1a1a1a"},
        "system": {"foreground": "#8a8a8a"},
        "error": {"foreground": "#c0392b"},
        "join": {"foreground": "#2e7d32"},
        "leave": {"foreground": "#b26a00"},
    }

    def __init__(self, master: tk.Misc, family: str = "") -> None:
        super().__init__(master)
        self.text = tk.Text(
            self, wrap="word", state="disabled", height=12,
            font=(family, 10) if family else None,
            background="#ffffff", relief="flat", padx=10, pady=8,
            spacing1=1, spacing3=3,
        )
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        for tag, options in self._TAGS.items():
            self.text.tag_configure(tag, **options)
        self.text.tag_configure("who", font=(family, 10, "bold") if family else None)
        self.text.tag_configure("time", foreground="#aaaaaa")

    def add(self, who: str, text: str, kind: str = "other") -> None:
        """追加一条。``kind`` 见 ``_TAGS``。"""
        self.text.configure(state="normal")
        if kind == "system":
            self.text.insert("end", f"{text}\n", kind)
        else:
            self.text.insert("end", who, ("who", kind))
            self.text.insert("end", f"  {text}\n", kind)
        self.text.configure(state="disabled")
        self.text.see("end")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def lines(self) -> int:
        return int(self.text.index("end-1c").split(".")[0])


# ---------------------------------------------------------------- 成员表


class MemberList(ttk.Frame):
    """房间成员列表。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        columns = ("pid", "name", "via")
        self.tree = ttk.Treeview(
            self, columns=columns, show="headings", style="Member.Treeview", height=10
        )
        self.tree.heading("pid", text="ID")
        self.tree.heading("name", text="昵称")
        self.tree.heading("via", text="接入")
        self.tree.column("pid", width=70, anchor="center", stretch=False)
        self.tree.column("name", width=150, anchor="w")
        self.tree.column("via", width=60, anchor="center", stretch=False)

        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.tree.tag_configure("me", foreground="#0b6bcb")
        self.tree.tag_configure("host", foreground="#2e7d32")
        self.tree.tag_configure("relay", foreground="#8a6d3b")

    def set_members(
        self,
        peers: Dict[int, object],
        *,
        me_id: int = -1,
        host_name: str = "",
        is_host: bool = False,
    ) -> None:
        """整表刷新。

        ``peers`` 是 ``{peer_id: PeerInfo}``。主机自己是 #0，不在 peers 里，
        所以这里手动补一行，免得看起来"房间里没有人"。
        """
        selected = self.tree.selection()
        keep = self.tree.item(selected[0], "values")[0] if selected else None

        self.tree.delete(*self.tree.get_children())
        self.tree.insert("", "end", values=("0", host_name or "主机", "—"), tags=("host",))

        for pid, info in sorted(peers.items()):
            tags: List[str] = []
            if pid == me_id:
                tags.append("me")
            if getattr(info, "is_relay", False):
                tags.append("relay")
            label = info.name  # type: ignore[attr-defined]
            if pid == me_id:
                label += "（我）"
            self.tree.insert(
                "", "end",
                values=(pid, label, "中继" if getattr(info, "is_relay", False) else "局域网"),
                tags=tuple(tags),
            )

        if keep is not None:
            for item in self.tree.get_children():
                if str(self.tree.item(item, "values")[0]) == str(keep):
                    self.tree.selection_set(item)
                    break

    def selected_peer(self) -> Optional[int]:
        """当前选中的成员 id，没选返回 None。"""
        selected = self.tree.selection()
        if not selected:
            return None
        try:
            return int(self.tree.item(selected[0], "values")[0])
        except (ValueError, IndexError):
            return None

    def count(self) -> int:
        """房间人数（含主机）。"""
        return len(self.tree.get_children())


# ---------------------------------------------------------------- 房间列表


class RoomList(ttk.Frame):
    """局域网 / 中继上发现的房间。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        columns = ("name", "players", "addr", "tags")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=8)
        self.tree.heading("name", text="房间名")
        self.tree.heading("players", text="人数")
        self.tree.heading("addr", text="地址")
        self.tree.heading("tags", text="备注")
        self.tree.column("name", width=180, anchor="w")
        self.tree.column("players", width=60, anchor="center", stretch=False)
        self.tree.column("addr", width=190, anchor="w")
        self.tree.column("tags", width=90, anchor="center", stretch=False)

        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self._rooms: List[object] = []

    def set_rooms(self, rooms: List[object]) -> None:
        """整表刷新。保持选中项不变，免得自动刷新时选中被清掉。"""
        selected = self.tree.selection()
        keep = self.tree.item(selected[0], "values")[0] if selected else None

        self._rooms = list(rooms)
        self.tree.delete(*self.tree.get_children())
        for room in self._rooms:
            tags = []
            if getattr(room, "has_password", False):
                tags.append("🔒 有密码")
            if getattr(room, "relay_room", ""):
                tags.append("中继")
            addr = getattr(room, "direct_addr", "")
            self.tree.insert(
                "", "end",
                values=(
                    getattr(room, "room_name", "?"),
                    f"{getattr(room, 'players', '?')}/{getattr(room, 'max_players', '?')}",
                    addr,
                    " ".join(tags),
                ),
            )
        if keep is not None:
            for item in self.tree.get_children():
                if self.tree.item(item, "values")[0] == keep:
                    self.tree.selection_set(item)
                    break

    def selected_room(self):
        selected = self.tree.selection()
        if not selected:
            return None
        index = self.tree.index(selected[0])
        if 0 <= index < len(self._rooms):
            return self._rooms[index]
        return None


# ---------------------------------------------------------------- 排版


class Card(ttk.Frame):
    """一块带内边距和标题的区域，用来分隔表单。"""

    def __init__(self, master: tk.Misc, title: str = "", padding: int = 12) -> None:
        super().__init__(master, padding=padding)
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)
        if title:
            ttk.Label(self, text=title, style="Banner.TLabel").pack(anchor="w", pady=(0, 8))
