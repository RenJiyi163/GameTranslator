"""First-run guide.

Shown when the game list is empty, because an empty list with three buttons is
where a first-time user stalls. It is a guide, not a wizard with its own
state: every step runs the same handler the main window uses.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import settings


class WelcomeDialog(tk.Toplevel):
    def __init__(self, root: tk.Misc, on_add, on_settings):
        super().__init__(root)
        self.root = root
        self.on_add = on_add
        self.on_settings = on_settings
        self.title("开始使用游戏翻译器")
        self.resizable(False, False)
        self.transient(root)
        self.configure(padx=18, pady=16)

        ttk.Label(self, text="三步开始", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        ttk.Label(self, text="每一步都在主窗口里，做完这页就可以关掉。",
                  foreground="#555").pack(anchor="w", pady=(2, 12))

        steps = ttk.Frame(self)
        steps.pack(fill="x")
        for number, title, detail in (
                ("1", "添加游戏",
                 "点「添加游戏…」选游戏的 exe。程序按 exe 旁边的文件认引擎，"
                 "RPG Maker MV/MZ 可以直接用。"),
                ("2", "模型设置（可以跳过）",
                 "只用词库就不必配。要翻译没收录的句子，就在「模型设置…」里填本地模型"
                 "或云端接口的地址与密钥。"),
                ("3", "开始翻译",
                 "在列表里双击游戏，或选中后按「开始翻译」。游戏里按 Ctrl+Shift+T "
                 "切换中文与原文。")):
            row = ttk.Frame(steps)
            row.pack(fill="x", pady=(0, 10))
            ttk.Label(row, text=number, font=("Segoe UI", 14, "bold"),
                      foreground="#1f5fa9", width=2).pack(side="left", anchor="n")
            body = ttk.Frame(row)
            body.pack(side="left", fill="x", expand=True)
            ttk.Label(body, text=title, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
            ttk.Label(body, text=detail, wraplength=430, justify="left",
                      foreground="#444").pack(anchor="w")

        data = settings.load()
        ready = [str((data.get("backends", {}).get(name) or {}).get("label") or name)
                 for name in settings.ready_backends(data)]
        if ready:
            note = f"后端已配置：{'、'.join(ready)}。用本地模型的话，先把模型服务启动起来。"
            color = "#1a6b32"
        else:
            note = "模型还没配。只查词库可以先跳过；要用后端翻译时再回来配。"
            color = "#8a5a00"
        ttk.Label(self, text=note, foreground=color).pack(anchor="w", pady=(4, 12))

        buttons = ttk.Frame(self)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="稍后", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="模型设置…", command=lambda: self._then(self.on_settings)).pack(
            side="right", padx=(0, 6))
        ttk.Button(buttons, text="添加游戏…", command=lambda: self._then(self.on_add)).pack(
            side="right", padx=(0, 6))

        self.bind("<Escape>", lambda _e: self.destroy())
        self.update_idletasks()
        x = root.winfo_rootx() + max(0, (root.winfo_width() - self.winfo_width()) // 2)
        y = root.winfo_rooty() + 60
        self.geometry(f"+{x}+{y}")

    def _then(self, callback) -> None:
        self.destroy()
        # Let the dialog disappear before the file picker opens on top of it.
        self.root.after(120, callback)
