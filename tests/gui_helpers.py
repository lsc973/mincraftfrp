"""GUI 测试的公共脚手架。

非 ``test_*.py`` 命名，所以不会被 unittest 自动发现成用例。

**跑 GUI 测试时会真的出现窗口**，这是必须的：Tk 只把键盘事件投递给真正可见的
窗口，``withdraw()`` 或全透明的窗口收不到 ``event_generate("<Return>")``，
那样"回车发送"这类就测不了。（试过移到屏幕外、设 alpha=0，都不行。）
"""

from __future__ import annotations

import time
import unittest
from tkinter import messagebox

import tkinter as tk


def make_app():
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


class NoDialogs:
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


def button_texts(widget):
    """递归收集一个控件树里所有按钮的文字。"""
    from tkinter import ttk

    found = []

    def walk(node):
        for child in node.winfo_children():
            try:
                if isinstance(child, ttk.Button):
                    found.append(child.cget("text"))
            except tk.TclError:
                pass
            walk(child)

    walk(widget)
    return found


def find_grid_collisions(root):
    """找出所有「两个控件抢同一个网格格子」的地方。

    这类问题功能测试完全查不到 —— 控件都在、事件也正常，只是视觉上互相盖住。
    但它的成因很常见：tkinter 的 ``widget.grid()`` 永远用控件**自己的 parent**
    当几何主，所以"先把控件 new 出来、再让辅助函数摆进某个容器"是行不通的，
    控件会跑进它自己 parent 的格子里。

    返回 [(容器, 格子, 控件A, 控件B), ...]。
    """
    collisions = []

    def walk(node):
        # 同一个容器里，每个格子只能有一个控件
        used = {}
        for child in node.winfo_children():
            try:
                info = child.grid_info()
            except Exception:
                info = None
            if info:
                row = int(info["row"])
                col = int(info["column"])
                span = int(info.get("columnspan", 1))
                if span <= 0:      # columnspan=0 是"占满剩余"，跳过
                    continue
                for cell in range(col, col + span):
                    key = (row, cell)
                    if key in used:
                        collisions.append((node, key, used[key], child))
                    else:
                        used[key] = child
        for child in node.winfo_children():
            walk(child)

    walk(root)
    return collisions
