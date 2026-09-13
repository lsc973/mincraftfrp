"""免费动态域名的设置对话框。

跟 DoctorDialog 一个路子：网络操作全在后台线程里跑，回到主线程一律走
``app.post()``。

这个对话框有个额外的要求：**不能把令牌显示出来**。窗口经常被截图发出来
问问题，令牌等同于域名的写权限，漏出去别人就能把名字指到别处。
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .. import ddns

__all__ = ["DdnsDialog"]


class DdnsDialog(tk.Toplevel):
    """配置服务商 / 域名 / 令牌，并且当场测一次。"""

    def __init__(self, master: tk.Misc, app, on_saved=None) -> None:
        super().__init__(master)
        self.app = app
        self.on_saved = on_saved
        self.title("免费动态域名")
        self.geometry("560x430")
        self.transient(master)
        self.resizable(False, False)

        self.provider = tk.StringVar(value=ddns.DEFAULT_PROVIDER)
        self.hostname = tk.StringVar()
        self.token = tk.StringVar()

        self._build()
        self._load_existing()
        self._refresh_status()

    # ------------------------------------------------------------ 布局

    def _build(self) -> None:
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text="免费动态域名", style="Title.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text="本机的 IPv6 会变，而且有四十个字符。配一个固定的短域名之后，\n"
                 "开隧道时会自动把域名指过来，你只要把域名发给对方就行。",
            style="Subtitle.TLabel", justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(2, 12))

        form = ttk.Frame(frame)
        form.grid(row=2, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="服务商", width=8).grid(row=0, column=0, sticky="w", pady=3)
        self.provider_box = ttk.Combobox(
            form, textvariable=self.provider, state="readonly",
            values=list(ddns.PROVIDERS.keys()), width=14,
        )
        self.provider_box.grid(row=0, column=1, sticky="w", pady=3)
        self.provider_box.bind("<<ComboboxSelected>>", lambda _e: self._refresh_status())

        ttk.Label(form, text="域名", width=8).grid(row=1, column=0, sticky="w", pady=3)
        self.hostname_entry = ttk.Entry(form, textvariable=self.hostname)
        self.hostname_entry.grid(row=1, column=1, sticky="ew", pady=3)

        ttk.Label(form, text="令牌", width=8).grid(row=2, column=0, sticky="w", pady=3)
        self.token_entry = ttk.Entry(form, textvariable=self.token, show="•")
        self.token_entry.grid(row=2, column=1, sticky="ew", pady=3)

        self.hint = ttk.Label(frame, text="", style="Hint.TLabel",
                              justify="left", wraplength=510)
        self.hint.grid(row=3, column=0, sticky="w", pady=(8, 0))

        self.status = ttk.Label(frame, text="", style="Hint.TLabel",
                                justify="left", wraplength=510)
        self.status.grid(row=4, column=0, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        self.save_button = ttk.Button(buttons, text="保存并测试", command=self._save)
        self.save_button.pack(side="left")
        self.clear_button = ttk.Button(buttons, text="删除配置", command=self._clear)
        self.clear_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="关闭", command=self.destroy).pack(side="right")

    # ------------------------------------------------------------ 读取

    def _load_existing(self) -> None:
        config = ddns.load()
        if config is not None:
            self.provider.set(config.provider)
            self.hostname.set(config.hostname)
            # 令牌不回填 —— 界面上根本不该出现它。留空表示"不改"。
            self.token.set("")

    def _refresh_status(self) -> None:
        provider = ddns.PROVIDERS.get(self.provider.get())
        if provider is not None:
            self.hint.configure(text=ddns.describe_setup(provider.key))

        config = ddns.load()
        if config is None:
            if not self.token.get():
                self.status.configure(text="当前：还没配置。")
            return

        saved = f"当前已配置：{config.label} 的 {config.hostname}"
        if self.token.get():
            saved += "（令牌栏留空则表示沿用已保存的那个）"
        self.status.configure(text=saved)

    # ------------------------------------------------------------ 保存

    def _collect(self):
        provider = self.provider.get().strip().lower()
        hostname = self.hostname.get().strip()
        token = self.token.get().strip()

        if not hostname:
            messagebox.showinfo("缺域名", "请填你申请到的域名。", parent=self)
            return None
        if not token:
            # 留空表示沿用已有的。全新配置则必须填。
            existing = ddns.load()
            if existing is None:
                messagebox.showinfo("缺令牌", "请填服务商给你的 token。", parent=self)
                return None
            token = existing.token
        return ddns.DdnsConfig(provider, hostname, token)

    def _save(self) -> None:
        config = self._collect()
        if config is None:
            return
        try:
            ddns.save(config)
        except ddns.DdnsError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return

        self.token.set("")   # 存完就不留在输入框里
        self.status.configure(text="已保存，正在测试…")
        self.save_button.configure(state="disabled")
        self._refresh_status()

        if self.on_saved is not None:
            self.on_saved()

        threading.Thread(target=self._test, args=(config,), daemon=True).start()

    def _test(self, config) -> None:
        from ..discovery import global_ipv6

        address = global_ipv6()
        if not address:
            self.app.post(self._show_result, False,
                          "配置已保存，但本机现在没有全球可达的 IPv6，没法测。\n"
                          "等有了网络再开隧道，到时候会自动更新。")
            return
        try:
            ddns.publish(address, config)
        except ddns.DdnsError as exc:
            self.app.post(self._show_result, False, f"配置已保存，但更新失败：{exc}")
            return
        except Exception as exc:  # pragma: no cover - 兜底
            self.app.post(self._show_result, False, f"配置已保存，但更新失败：{exc}")
            return

        if ddns.verify(config.hostname, address, attempts=3, interval=1.0):
            self.app.post(self._show_result, True,
                          f"成功。{config.hostname} 现在指向 {address}\n"
                          f"以后把它发给对方就行。")
        else:
            self.app.post(self._show_result, True,
                          f"更新请求已提交，但本机 DNS 还没解析到。\n"
                          f"这通常是本机缓存没过期，等一两分钟，不影响对方。")

    def _show_result(self, ok: bool, text: str) -> None:
        self.save_button.configure(state="normal")
        self.status.configure(text=text)

    # ------------------------------------------------------------ 删除

    def _clear(self) -> None:
        if ddns.load() is None:
            messagebox.showinfo("没有配置", "本来就没配过。", parent=self)
            return
        if not messagebox.askyesno("删除配置", "删掉之后开隧道就不再自动更新域名了。要继续吗？",
                                   parent=self):
            return
        ddns.clear()
        self.hostname.set("")
        self.token.set("")
        self.status.configure(text="已删除配置。")
        if self.on_saved is not None:
            self.on_saved()
