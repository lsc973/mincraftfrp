"""全局布局检查：任何页面上都不该有控件互相盖住。

这类 bug 功能测试**查不出来** —— 控件都在、值也对、事件也正常，只是视觉上
重叠了。成因通常是「控件建在了错误的 parent 上」：

    tkinter 的 ``widget.grid()`` 永远用控件**自己的 parent** 当几何主。
    所以"先把控件 new 出来、再指望辅助函数把它摆进某个容器"是行不通的，
    控件会跑进它自己 parent 的格子里，跟别的控件抢位置。

隧道页就这么踩过一次：6 个输入框全挤进 form 的 (0,1) 格，正好压住第 0 行的
「我是哪一边」单选按钮。功能测试全绿，只有肉眼能看出来。

这个文件用几何方式把它兜住。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui_helpers import find_grid_collisions, make_app, pump  # noqa: E402


class TestNoLayoutCollisions(unittest.TestCase):
    """一个进程只建一个 Tk 根窗口。

    建两个的话，ttk 的 ThemeChanged 会打到已销毁的那个上，跑完甩一句
    'application has been destroyed' —— 纯属测试自找的噪音。
    所以这里把"查页面"和"查工具本身"合并成一个类，共用同一个 app。
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

    def test_every_page_has_no_grid_collisions(self):
        for name in self.app.pages:
            with self.subTest(page=name):
                self.app.show_page(name)
                pump(self.app, 0.3)
                collisions = find_grid_collisions(self.app.pages[name])
                if collisions:
                    detail = "\n".join(
                        f"    容器 {container.__class__.__name__} 的格子 {cell}："
                        f"{first.__class__.__name__} 和 {second.__class__.__name__} 重叠"
                        for container, cell, first, second in collisions
                    )
                    self.fail(f"「{name}」页有控件重叠：\n{detail}")
        self.app.show_page("start")

    def test_dialogs_have_no_grid_collisions(self):
        """弹窗也要查 —— 创建房间 / 加入房间的字段更多，更容易挤。"""
        from lanlink.gui.app import CreateRoomDialog, JoinRoomDialog

        for factory, label in (
            (lambda: CreateRoomDialog(self.app, self.app), "创建房间"),
            (lambda: JoinRoomDialog(self.app, self.app), "加入房间"),
        ):
            dialog = None
            try:
                dialog = factory()
                pump(self.app, 0.4)
                collisions = find_grid_collisions(dialog)
                if collisions:
                    detail = "\n".join(
                        f"    容器 {container.__class__.__name__} 的格子 {cell}："
                        f"{first.__class__.__name__} 和 {second.__class__.__name__} 重叠"
                        for container, cell, first, second in collisions
                    )
                    self.fail(f"「{label}」弹窗有控件重叠：\n{detail}")
            finally:
                if dialog is not None:
                    try:
                        dialog.destroy()
                    except Exception:
                        pass
                pump(self.app, 0.2)


    # ---- 先确认这个检查工具本身有用：不然它永远"通过"就等于没测 ----

    def test_detects_a_deliberate_collision(self):
        import tkinter as tk
        from tkinter import ttk

        frame = ttk.Frame(self.app)
        try:
            a = ttk.Label(frame, text="A")
            b = ttk.Label(frame, text="B")
            a.grid(row=0, column=0)
            b.grid(row=0, column=1)          # 正常：不同列
            self.assertEqual(find_grid_collisions(frame), [])

            c = ttk.Label(frame, text="C")
            c.grid(row=0, column=1)          # 故意撞 B
            collisions = find_grid_collisions(frame)
            self.assertEqual(len(collisions), 1, "没检测出人为制造的格子冲突")
            self.assertEqual(collisions[0][1], (0, 1))
        finally:
            frame.destroy()

    def test_detects_collision_across_columnspan(self):
        """跨列控件也要能被检测到 —— 这正是隧道页那次的情形。"""
        from tkinter import ttk

        frame = ttk.Frame(self.app)
        try:
            wide = ttk.LabelFrame(frame, text="占满整行")
            wide.grid(row=0, column=0, columnspan=3)
            intruder = ttk.Label(frame, text="挤进来")
            intruder.grid(row=0, column=1)   # 落在 wide 的跨度里
            collisions = find_grid_collisions(frame)
            self.assertTrue(collisions, "跨列重叠没被检测出来")
        finally:
            frame.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
