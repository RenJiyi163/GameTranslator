"""Always-on-top hit-rate window.

A borderless, draggable box the player can park in a corner while the game
runs. It shows one number (how much of what we have seen was answered from
memory or cache) plus the raw counters behind it, and nothing else: the heavy
controls stay in the main window.
"""

from __future__ import annotations

import tkinter as tk

BG = "#23272e"
BORDER = "#3c444f"
FG = "#e6e9ee"
DIM = "#9aa4b1"
ACCENT = "#7ee08a"

WIDTH = 208
HEIGHT = 126


def hit_rate(data: dict | None) -> float | None:
    """Assumed-translated share of everything seen; None before any traffic."""
    if not data:
        return None
    seen = int(data.get("seen") or 0)
    if seen <= 0:
        return None
    hits = int(data.get("tm_hits") or 0) + int(data.get("cache_hits") or 0)
    return min(1.0, max(0.0, hits / seen))


class StatsOverlay(tk.Toplevel):
    def __init__(self, root: tk.Misc, on_hide, position: tuple[int, int] | None = None):
        super().__init__(root)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", 0.96)
        except tk.TclError:
            pass
        self.configure(bg=BG)
        self.on_hide = on_hide
        self._drag_origin: tuple[int, int, int, int] | None = None

        frame = tk.Frame(self, bg=BG, highlightthickness=1, highlightbackground=BORDER)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)

        head = tk.Frame(frame, bg=BG)
        head.grid(row=0, column=0, sticky="we", padx=(10, 4), pady=(7, 0))
        head.columnconfigure(0, weight=1)
        title = tk.Label(head, text="游戏翻译器", bg=BG, fg=DIM,
                         font=("Microsoft YaHei UI", 8))
        title.grid(row=0, column=0, sticky="w")

        self.close_button = tk.Label(head, text="×", bg=BG, fg=DIM,
                                     font=("Segoe UI", 11, "bold"), cursor="hand2")
        self.close_button.grid(row=0, column=1, sticky="e")
        self.close_button.bind("<Button-1>", lambda _e: self.hide())
        self.close_button.bind("<Enter>", lambda _e: self.close_button.configure(fg="#ffffff"))
        self.close_button.bind("<Leave>", lambda _e: self.close_button.configure(fg=DIM))

        self.rate_var = tk.StringVar(value="—")
        self.rate_label = tk.Label(frame, textvariable=self.rate_var, bg=BG, fg=ACCENT,
                                   font=("Segoe UI", 20, "bold"))
        self.rate_label.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 0))

        self.count_var = tk.StringVar(value="未在翻译")
        self.count_label = tk.Label(frame, textvariable=self.count_var, bg=BG, fg=FG,
                                    font=("Microsoft YaHei UI", 9))
        self.count_label.grid(row=2, column=0, sticky="w", padx=10)

        self.detail_var = tk.StringVar(value="")
        self.detail_label = tk.Label(frame, textvariable=self.detail_var, bg=BG, fg=DIM,
                                     font=("Microsoft YaHei UI", 8))
        self.detail_label.grid(row=3, column=0, sticky="w", padx=10, pady=(2, 10))

        for widget in (frame, head, title, self.rate_label, self.count_label, self.detail_label):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)

        x, y = position or self.default_position()
        self.geometry(f"{WIDTH}x{HEIGHT}+{int(x)}+{int(y)}")

    # -- placement ---------------------------------------------------------

    def default_position(self) -> tuple[int, int]:
        x = max(0, self.winfo_screenwidth() - WIDTH - 28)
        return x, 28

    def position(self) -> tuple[int, int]:
        self.update_idletasks()
        return self.winfo_x(), self.winfo_y()

    def show(self, position: tuple[int, int] | None = None) -> None:
        if position:
            self.geometry(f"+{int(position[0])}+{int(position[1])}")
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)

    def hide(self) -> None:
        self.withdraw()
        if self.on_hide:
            self.on_hide()

    # -- dragging ----------------------------------------------------------

    def _drag_start(self, event) -> None:
        self._drag_origin = (event.x_root, event.y_root, self.winfo_x(), self.winfo_y())

    def _drag_move(self, event) -> None:
        if not self._drag_origin:
            return
        x0, y0, wx, wy = self._drag_origin
        self.geometry(f"+{wx + event.x_root - x0}+{wy + event.y_root - y0}")

    def _drag_end(self, _event) -> None:
        self._drag_origin = None

    # -- contents ----------------------------------------------------------

    def update_stats(self, data: dict | None) -> None:
        rate = hit_rate(data)
        if rate is None:
            self.rate_var.set("—")
            self.count_var.set("未在翻译" if not data else "还没看到文字")
            self.detail_var.set("")
            return
        self.rate_var.set(f"{rate * 100:.0f}%")
        seen = int(data.get("seen") or 0)
        self.count_var.set(f"已看 {seen} 条 · 已替换 {int(data.get('applied') or 0)}")
        self.detail_var.set("词库 %d　缓存 %d　后端 %d" % (
            int(data.get("tm_hits") or 0), int(data.get("cache_hits") or 0),
            int(data.get("backend_hits") or 0)))
