"""In-app view of the run log and the unmatched list.

Both files already exist on disk (logs/gt.log for the whole program,
profiles/<id>/unmatched.jsonl for the selected game); this is a reader, not a
second writer, so nothing shown here can disagree with what was recorded.
"""

from __future__ import annotations

import json
import logging
import os
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from .config import LOG_DIR

TAIL_BYTES = 256_000
LOG_LEVELS = {
    "全部": None,
    "警告与错误": ("WARNING", "ERROR", "CRITICAL"),
    "只看错误": ("ERROR", "CRITICAL"),
}


def read_tail(path: Path, limit: int = TAIL_BYTES) -> str:
    """Last `limit` bytes of a text file, trimmed to whole lines."""
    path = Path(path)
    if not path.exists():
        return ""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - limit)
        fh.seek(start)
        data = fh.read()
    # The log is written in text mode on Windows, so the file has CRLF; Tk's
    # Text widget would draw the \r as a stray character.
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if start > 0:
        # The cut almost certainly landed mid-line; drop the fragment.
        text = text.split("\n", 1)[-1] if "\n" in text else ""
    return text


def filter_lines(text: str, levels: tuple[str, ...] | None) -> str:
    if not levels:
        return text
    kept = [line for line in text.splitlines()
            if any(f" {level} " in line for level in levels)]
    return "\n".join(kept)


def read_unmatched(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


class LogWindow(tk.Toplevel):
    def __init__(self, root: tk.Misc, log: logging.Logger, game, profile_dir: Path | None):
        super().__init__(root)
        self.title("运行记录")
        self.geometry("820x520")
        self.minsize(640, 380)
        self.log = log
        self.game = game
        self.profile_dir = profile_dir
        self._log_signature = None
        self._log_timer = None
        self._unmatched_timer = None

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)
        self.notebook = notebook
        self._build_log_tab(notebook)
        self._build_unmatched_tab(notebook)

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_log(force=True)
        self.refresh_unmatched()

    # -- log tab -----------------------------------------------------------

    def _build_log_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=8)
        notebook.add(tab, text="运行日志")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="we", pady=(0, 6))
        ttk.Label(bar, text="级别").pack(side="left")
        self.level_var = tk.StringVar(value="全部")
        combo = ttk.Combobox(bar, textvariable=self.level_var, state="readonly",
                             values=list(LOG_LEVELS), width=10)
        combo.pack(side="left", padx=(6, 0))
        combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_log(force=True))
        ttk.Button(bar, text="刷新", command=lambda: self.refresh_log(force=True)).pack(
            side="left", padx=(8, 0))
        ttk.Button(bar, text="打开日志文件夹", command=self.open_folder).pack(side="left", padx=(6, 0))
        self.log_hint = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.log_hint, foreground="#555").pack(side="left", padx=(10, 0))

        wrap = ttk.Frame(tab)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.log_text = tk.Text(wrap, wrap="none", font=("Consolas", 9),
                                background="#fbfbfb", relief="flat")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set, state="disabled")
        self.log_text.tag_configure("warn", foreground="#a15c00")
        self.log_text.tag_configure("error", foreground="#b00020")

    def _insert_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        for line in text.splitlines():
            tag = None
            if " ERROR " in line or " CRITICAL " in line:
                tag = "error"
            elif " WARNING " in line:
                tag = "warn"
            self.log_text.insert("end", line + "\n", tag or ())
        self.log_text.configure(state="disabled")

    def _cancel(self, timer) -> None:
        if timer:
            try:
                self.after_cancel(timer)
            except tk.TclError:
                pass

    def refresh_log(self, force: bool = False) -> None:
        if not self.winfo_exists():
            return
        self._cancel(self._log_timer)
        self._log_timer = None
        path = LOG_DIR / "gt.log"
        try:
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime)
        except OSError:
            signature = None
        if force or signature != self._log_signature:
            self._log_signature = signature
            text = read_tail(path)
            filtered = filter_lines(text, LOG_LEVELS.get(self.level_var.get()))
            at_bottom = self.log_text.yview()[1] >= 0.999
            self._insert_log(filtered)
            if at_bottom:
                self.log_text.see("end")
            lines = len(filtered.splitlines())
            self.log_hint.set(f"{lines} 行" if filtered else "日志还是空的")
        self._log_timer = self.after(2000, lambda: self.refresh_log())

    def open_folder(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(LOG_DIR))  # type: ignore[attr-defined]
        except OSError:
            self.log.exception("could not open the log folder")

    # -- unmatched tab -----------------------------------------------------

    def _build_unmatched_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=8)
        notebook.add(tab, text="未命中的句子")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="we", pady=(0, 6))
        ttk.Button(bar, text="刷新", command=self.refresh_unmatched).pack(side="left")
        ttk.Button(bar, text="复制原文", command=self.copy_selected).pack(side="left", padx=(6, 0))
        self.unmatched_hint = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.unmatched_hint, foreground="#555").pack(
            side="left", padx=(10, 0))

        columns = ("text", "where", "kind")
        self.unmatched_tree = ttk.Treeview(tab, columns=columns, show="headings",
                                           selectmode="browse", height=12)
        for key, text, width in (("text", "原文", 420), ("where", "位置", 180),
                                 ("kind", "种类", 100)):
            self.unmatched_tree.heading(key, text=text)
            self.unmatched_tree.column(key, width=width, anchor="w",
                                       stretch=(key == "text"))
        self.unmatched_tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.unmatched_tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.unmatched_tree.configure(yscrollcommand=scroll.set)
        self.unmatched_tree.bind("<Double-1>", lambda _e: self.copy_selected())

        self.unmatched_path = ((self.profile_dir or Path()) / "unmatched.jsonl") \
            if self.profile_dir else None
        if self.game is not None:
            self.unmatched_hint.set(f"{self.game.name}：还没读")
        else:
            self.unmatched_hint.set("先在主窗口里选一个游戏")

    def refresh_unmatched(self) -> None:
        if not self.winfo_exists():
            return
        self._cancel(self._unmatched_timer)
        self._unmatched_timer = None
        if self.unmatched_path is None:
            self._unmatched_timer = self.after(3000, self.refresh_unmatched)
            return
        rows = read_unmatched(self.unmatched_path)
        keep = self.unmatched_tree.focus()
        self.unmatched_tree.delete(*self.unmatched_tree.get_children())
        for index, row in enumerate(rows, 1):
            self.unmatched_tree.insert(
                "", "end", iid=str(index),
                values=(row.get("text") or row.get("raw") or "",
                        row.get("where") or "", row.get("kind") or ""))
        if keep and self.unmatched_tree.exists(keep):
            self.unmatched_tree.focus(keep)
        name = self.game.name if self.game is not None else ""
        if rows:
            self.unmatched_hint.set(f"{name}：{len(rows)} 条（每次停止翻译都会覆盖这份列表）")
        else:
            self.unmatched_hint.set(f"{name}：还没有未命中的句子")
        self._unmatched_timer = self.after(3000, self.refresh_unmatched)

    def copy_selected(self) -> None:
        item = self.unmatched_tree.focus()
        if not item:
            return
        values = self.unmatched_tree.item(item, "values")
        if not values:
            return
        self.clipboard_clear()
        self.clipboard_append(values[0])

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._cancel(self._log_timer)
        self._cancel(self._unmatched_timer)
        self._log_timer = None
        self._unmatched_timer = None
        self.destroy()
