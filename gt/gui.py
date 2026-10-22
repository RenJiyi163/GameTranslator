"""Double-click front end: pick a game, press one button.

The command line stays the canonical interface; this only wraps it so the
tool can be handed to someone who is not going to open a terminal. Every
piece of work is still done by TranslationRuntime, so the two paths cannot
drift apart.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import exchange, pack, settings, tray, uistate, update
from .config import (LOG_DIR, PROFILE_DIR, STRATEGIES, STRATEGY_LABELS, ConfigError,
                     Profile)
from .games import (GameInfo, add_game, detect_engine, game_size, list_games,
                    remove_game, rename_game, set_exe, set_port, set_strategy)
from .glossary import GlossaryFile
from .logview import LogWindow
from .overlay import StatsOverlay
from .wizard import WelcomeDialog

EXE_TYPES = [("Windows 程序", "*.exe"), ("所有文件", "*.*")]


def _setup_logging() -> logging.Logger:
    """File logging only: a windowed build has no stderr to write to."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("gt")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    handler = logging.FileHandler(LOG_DIR / "gt.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.setLevel(logging.DEBUG)
    log.addHandler(handler)
    return log


def _when(ts: float | None) -> str:
    if not ts:
        return "从未运行"
    moment = datetime.fromtimestamp(ts)
    if moment.date() == datetime.now().date():
        return moment.strftime("今天 %H:%M")
    return moment.strftime("%Y-%m-%d %H:%M")


def _mb(size: int) -> str:
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.1f} GB"
    return f"{size / 1024 ** 2:.1f} MB"


class EditDialog(tk.Toplevel):
    """Everything that belongs to one game: name, executable, port, strategy."""

    def __init__(self, parent: tk.Misc, info: GameInfo, backends: list[str] | None = None):
        super().__init__(parent)
        self.title("修改游戏")
        self.transient(parent)
        self.resizable(False, False)
        self.result: tuple[str, str, int, str, str] | None = None
        self.backend_ids = ["default"] + list(backends or [])

        self.name_var = tk.StringVar(value=info.name)
        self.path_var = tk.StringVar(value=info.exe)
        self.port_var = tk.StringVar(value=str(info.port) if info.port else "")
        self.hint = tk.StringVar(value="")
        self.strategy_var = tk.StringVar(value=STRATEGY_LABELS.get(info.strategy, "词库优先，后端兜底"))
        current = info.backend if info.backend in self.backend_ids else "default"
        self.backend_var = tk.StringVar(value="跟随默认" if current == "default" else current)

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="名称").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.name_var, width=42).grid(
            row=0, column=1, columnspan=2, sticky="we", pady=3)

        ttk.Label(frame, text="程序").grid(row=1, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.path_var, width=34).grid(
            row=1, column=1, sticky="we", pady=3)
        ttk.Button(frame, text="浏览…", command=self.browse).grid(row=1, column=2, padx=(6, 0))

        ttk.Label(frame, text="端口").grid(row=2, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.port_var, width=8).grid(
            row=2, column=1, sticky="w", pady=3)

        ttk.Label(frame, text="翻译策略").grid(row=3, column=0, sticky="w")
        ttk.Combobox(frame, textvariable=self.strategy_var, state="readonly",
                     values=[STRATEGY_LABELS[s] for s in STRATEGIES]).grid(
            row=3, column=1, columnspan=2, sticky="we", pady=3)

        ttk.Label(frame, text="后端").grid(row=4, column=0, sticky="w")
        ttk.Combobox(frame, textvariable=self.backend_var, state="readonly",
                     values=["跟随默认"] + self.backend_ids[1:]).grid(
            row=4, column=1, columnspan=2, sticky="we", pady=3)

        ttk.Label(frame, textvariable=self.hint, foreground="#555", wraplength=380,
                  justify="left").grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="保存", command=self.save).pack(side="right", padx=(0, 6))

        self.bind("<Return>", lambda _e: self.save())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()

    def browse(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="选择游戏程序", filetypes=EXE_TYPES)
        if not path:
            return
        self.path_var.set(path)
        engine = detect_engine(path)
        self.hint.set(f"检测到 {engine.label}。" + (f"（{engine.reason}）" if not engine.supported else ""))

    def save(self) -> None:
        name = self.name_var.get().strip()
        path = Path(self.path_var.get().strip())
        if not name:
            messagebox.showwarning("修改游戏", "名字不能为空。", parent=self)
            return
        if not path.is_file():
            messagebox.showwarning("修改游戏", f"找不到程序：{path}", parent=self)
            return
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showwarning("修改游戏", "端口要填数字。", parent=self)
            return
        if not 1024 <= port <= 65535:
            messagebox.showwarning("修改游戏", "端口要在 1024 到 65535 之间。", parent=self)
            return
        strategy = next((key for key, label in STRATEGY_LABELS.items()
                         if label == self.strategy_var.get()), "tm_first")
        backend = self.backend_var.get()
        if backend not in self.backend_ids:
            backend = "default"
        self.result = (name, str(path), port, strategy, backend)
        self.destroy()


class SettingsDialog(tk.Toplevel):
    """Model endpoints (machine-wide) and the selected game's glossary."""

    NUMERIC = {"temperature": float, "maxTokens": int, "timeout": int, "concurrency": int}

    def __init__(self, parent: tk.Misc, log: logging.Logger, game: GameInfo | None):
        super().__init__(parent)
        self.title("模型设置")
        self.transient(parent)
        self.geometry("680x470")
        self.minsize(620, 420)

        self.log = log
        self.game = game
        self.data = settings.load()
        self.current: str | None = None
        self.glossary: GlossaryFile | None = None
        self.dirty = False
        self.term_dirty = False
        self.events: queue.Queue = queue.Queue()

        notebook = self.notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        notebook.add(self._build_backends(notebook), text="模型后端")
        notebook.add(self._build_glossary(notebook), text="术语表")

        bar = ttk.Frame(self, padding=(10, 0, 10, 10))
        bar.pack(fill="x")
        self.status = tk.StringVar(value="改完按「保存」。")
        ttk.Label(bar, textvariable=self.status, foreground="#555").pack(side="left")
        ttk.Button(bar, text="关闭", command=self.close).pack(side="right")
        ttk.Button(bar, text="保存", command=self.save).pack(side="right", padx=(0, 6))

        self._reload_list()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(150, self._drain)
        self.grab_set()

    # -- model endpoints ---------------------------------------------------

    def _build_backends(self, parent: ttk.Notebook) -> ttk.Frame:
        outer = ttk.Frame(parent, padding=10)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        left = ttk.Frame(outer)
        left.grid(row=0, column=0, sticky="ns")
        self.backend_list = tk.Listbox(left, width=20, height=10, exportselection=False)
        self.backend_list.pack(fill="y", expand=True)
        self.backend_list.bind("<<ListboxSelect>>", self._on_pick)
        buttons = ttk.Frame(left)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="新增", width=6, command=self._add_backend).pack(side="left")
        ttk.Button(buttons, text="删除", width=6, command=self._del_backend).pack(side="left", padx=(4, 0))

        form = ttk.LabelFrame(outer, text="配置", padding=10)
        form.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        form.columnconfigure(1, weight=1)

        self.vars = {key: tk.StringVar() for key, _ in settings.FIELDS}
        self.key_visible = tk.BooleanVar(value=False)
        self.model_combo: ttk.Combobox | None = None
        self.key_entry: ttk.Entry | None = None

        for row, (key, label) in enumerate(settings.FIELDS):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=3)
            if key == "type":
                widget: tk.Widget = ttk.Combobox(
                    form, textvariable=self.vars[key], state="readonly",
                    values=["openai_compatible", "youdao_doc", "youdao_ui"])
            elif key == "model":
                self.model_combo = ttk.Combobox(form, textvariable=self.vars[key])
                widget = self.model_combo
            elif key == "apiKey":
                self.key_entry = ttk.Entry(form, textvariable=self.vars[key], show="*")
                widget = self.key_entry
            else:
                widget = ttk.Entry(form, textvariable=self.vars[key])
            widget.grid(row=row, column=1, sticky="we", pady=3)

        keys = [key for key, _ in settings.FIELDS]
        ttk.Button(form, text="拉取模型", command=self._fetch_models).grid(
            row=keys.index("model"), column=2, padx=(6, 0))
        ttk.Checkbutton(form, text="显示", variable=self.key_visible,
                        command=self._toggle_key).grid(
            row=keys.index("apiKey"), column=2, sticky="w", padx=(6, 0))

        test = ttk.Frame(form)
        test.grid(row=len(settings.FIELDS), column=0, columnspan=3, sticky="we", pady=(8, 0))
        ttk.Button(test, text="测试连通", command=self._test).pack(side="left")
        self.probe_text = tk.StringVar(value="")
        ttk.Label(test, textvariable=self.probe_text, wraplength=340).pack(side="left", padx=(8, 0))

        active = ttk.Frame(outer)
        active.grid(row=1, column=0, columnspan=2, sticky="we", pady=(10, 0))
        ttk.Label(active, text="没有单独指定后端的游戏用：").pack(side="left")
        self.default_var = tk.StringVar()
        self.default_combo = ttk.Combobox(active, textvariable=self.default_var,
                                          state="readonly", width=18)
        self.default_combo.pack(side="left", padx=(6, 0))
        return outer

    def _reload_list(self, select: str | None = None) -> None:
        names = list(self.data["backends"])
        self.backend_list.delete(0, "end")
        for name in names:
            label = self.data["backends"][name].get("label") or name
            self.backend_list.insert("end", name if label == name else f"{name}｜{label}")
        target = select or self.current or (names[0] if names else None)
        self.current = None
        self._refresh_defaults(names)
        if target in names:
            index = names.index(target)
            self.backend_list.selection_clear(0, "end")
            self.backend_list.selection_set(index)
            self._load(target)

    def _refresh_defaults(self, names: list[str]) -> None:
        self.default_combo["values"] = ["只查词库"] + names
        active = self.data.get("active") or []
        self.default_var.set(active[0] if active and active[0] in names else "只查词库")

    def _on_pick(self, _event=None) -> None:
        selection = self.backend_list.curselection()
        if not selection:
            return
        names = list(self.data["backends"])
        index = selection[0]
        if index >= len(names):
            return
        name = names[index]
        if name == self.current:
            return
        if self.current is not None and not self._commit():
            # Bad number in the form: stay where we are instead of losing it.
            if self.current in names:
                self.backend_list.selection_clear(0, "end")
                self.backend_list.selection_set(names.index(self.current))
            return
        self._load(name)

    def _load(self, name: str) -> None:
        self.current = name
        definition = self.data["backends"].get(name, {})
        for key, _ in settings.FIELDS:
            value = definition.get(key, "")
            self.vars[key].set("" if value is None else str(value))
        if self.model_combo is not None:
            self.model_combo["values"] = []
        self.probe_text.set("")

    def _commit(self) -> bool:
        if self.current is None:
            return True
        definition = self.data["backends"].setdefault(self.current, {})
        for key, _ in settings.FIELDS:
            value = self.vars[key].get().strip()
            convert = self.NUMERIC.get(key)
            if convert is not None:
                if not value:
                    messagebox.showwarning("模型设置", f"「{key}」不能空着。", parent=self)
                    return False
                try:
                    value = convert(value)
                except ValueError:
                    messagebox.showwarning("模型设置", f"「{key}」要填数字。", parent=self)
                    return False
            definition[key] = value
        self.dirty = True
        return True

    def _toggle_key(self) -> None:
        if self.key_entry is not None:
            self.key_entry.configure(show="" if self.key_visible.get() else "*")

    def _add_backend(self) -> None:
        name = simpledialog.askstring(
            "新增后端", "给它起个名字（英文，例如 deepseek）：", parent=self)
        if not name:
            return
        name = name.strip()
        if not name or name in self.data["backends"]:
            messagebox.showwarning("新增后端", "名字空着，或者已经有同名的。", parent=self)
            return
        self.data["backends"][name] = {
            "type": "openai_compatible", "label": name, "base": "", "apiKey": "",
            "model": "", "temperature": 0.2, "maxTokens": 4096, "timeout": 120,
            "concurrency": 1,
        }
        self.dirty = True
        self.current = None
        self._reload_list(select=name)

    def _del_backend(self) -> None:
        name = self.current
        if not name:
            return
        if not messagebox.askyesno("删除后端", f"删除 {name} 的配置？", parent=self):
            return
        self.current = None
        self.data["backends"].pop(name, None)
        self.data["active"] = [n for n in self.data.get("active", []) if n != name]
        self.dirty = True
        self._reload_list()

    def _fetch_models(self) -> None:
        if self.current is None or not self._commit():
            return
        definition = dict(self.data["backends"][self.current])
        self.probe_text.set("正在拉模型列表……")

        def work() -> None:
            try:
                models = settings.fetch_models(definition)
            except Exception as exc:  # network errors are the normal failure here
                self.events.put(("models_err", str(exc)))
                return
            self.events.put(("models", models))

        threading.Thread(target=work, daemon=True).start()

    def _test(self) -> None:
        if self.current is None or not self._commit():
            return
        definition = dict(self.data["backends"][self.current])
        self.probe_text.set("正在连……")

        def work() -> None:
            ok, message = settings.probe(definition)
            self.events.put(("probe", (ok, message)))

        threading.Thread(target=work, daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "models":
                    if self.model_combo is not None:
                        self.model_combo["values"] = payload
                    self.probe_text.set(f"拿到 {len(payload)} 个模型，点「模型」那一栏的下拉箭选。")
                elif kind == "models_err":
                    self.probe_text.set(f"拉不到：{payload}")
                elif kind == "probe":
                    ok, message = payload
                    self.probe_text.set(("✓ " if ok else "✗ ") + message)
        except queue.Empty:
            pass
        self.after(150, self._drain)

    # -- glossary ----------------------------------------------------------

    def _build_glossary(self, parent: ttk.Notebook) -> ttk.Frame:
        outer = ttk.Frame(parent, padding=10)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        if self.game is None:
            ttk.Label(outer, text="先在主界面选一个游戏，再打开这里。").grid(row=0, column=0, sticky="w")
            return outer
        try:
            profile = Profile.load(self.game.id)
            self.glossary = GlossaryFile(profile.glossary_path or profile.dir / "glossary.tsv")
        except (ConfigError, OSError) as exc:
            ttk.Label(outer, text=f"读不到这个游戏的配置：{exc}").grid(row=0, column=0, sticky="w")
            return outer

        ttk.Label(outer, text=f"{self.game.name} 的术语表，翻译时按这里定死的说法走（重启游戏后生效）").grid(
            row=0, column=0, sticky="w")
        self.term_tree = ttk.Treeview(outer, columns=("source", "target"), show="headings", height=10)
        self.term_tree.heading("source", text="原文")
        self.term_tree.heading("target", text="译文")
        self.term_tree.column("source", width=280)
        self.term_tree.column("target", width=280)
        self.term_tree.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self.term_tree.bind("<<TreeviewSelect>>", self._on_term_pick)

        editor = ttk.Frame(outer)
        editor.grid(row=2, column=0, sticky="we", pady=(8, 0))
        self.source_var = tk.StringVar()
        self.target_var = tk.StringVar()
        ttk.Label(editor, text="原文").pack(side="left")
        ttk.Entry(editor, textvariable=self.source_var, width=20).pack(side="left", padx=(4, 8))
        ttk.Label(editor, text="译文").pack(side="left")
        ttk.Entry(editor, textvariable=self.target_var, width=20).pack(side="left", padx=(4, 8))
        ttk.Button(editor, text="添加 / 更新", command=self._set_term).pack(side="left")
        ttk.Button(editor, text="删除", command=self._del_term).pack(side="left", padx=(6, 0))

        self._reload_terms()
        return outer

    def _reload_terms(self) -> None:
        if self.glossary is None:
            return
        self.term_tree.delete(*self.term_tree.get_children())
        for source, target in self.glossary.entries():
            self.term_tree.insert("", "end", values=(source, target))

    def _on_term_pick(self, _event=None) -> None:
        selection = self.term_tree.selection()
        if not selection:
            return
        values = self.term_tree.item(selection[0], "values")
        self.source_var.set(values[0])
        self.target_var.set(values[1])

    def _set_term(self) -> None:
        if self.glossary is None:
            return
        source = self.source_var.get().strip()
        target = self.target_var.get().strip()
        if not source:
            messagebox.showwarning("术语表", "原文不能空着。", parent=self)
            return
        try:
            self.glossary.set(source, target)
        except ValueError as exc:
            messagebox.showwarning("术语表", str(exc), parent=self)
            return
        self._reload_terms()
        self.term_dirty = True
        self.status.set("术语表改过了，按「保存」写进文件。")

    def _del_term(self) -> None:
        if self.glossary is None:
            return
        source = self.source_var.get().strip()
        if not source or not self.glossary.delete(source):
            messagebox.showwarning("术语表", "先在表里选一条。", parent=self)
            return
        self._reload_terms()
        self.source_var.set("")
        self.target_var.set("")
        self.term_dirty = True
        self.status.set("术语表改过了，按「保存」写进文件。")

    # -- saving ------------------------------------------------------------

    def save(self) -> None:
        if not self._commit():
            return
        choice = self.default_var.get()
        self.data["active"] = [] if choice in ("", "只查词库") else [choice]
        try:
            settings.save(self.data)
            if self.term_dirty and self.glossary is not None:
                self.glossary.save()
        except OSError as exc:
            messagebox.showerror("模型设置", f"写不进去：{exc}", parent=self)
            return
        self.dirty = False
        self.term_dirty = False
        self.status.set("已保存。正在翻译的游戏要停一下再开，才会用上新设置。")
        self.log.info("settings saved: active=%s", self.data["active"])

    def close(self) -> None:
        if self.dirty or self.term_dirty:
            answer = messagebox.askyesnocancel(
                "模型设置", "改动还没保存，要保存吗？", parent=self)
            if answer is None:
                return
            if answer:
                self.save()
                if self.dirty:
                    return  # save failed and already said why
        self.grab_release()
        self.destroy()


class App:
    def __init__(self, root: tk.Tk, autostart: str | None = None, attach: bool = True):
        self.root = root
        self.autostart = autostart
        self.log = _setup_logging()
        self.thread: threading.Thread | None = None
        self.runtime = None
        self.stop_event = threading.Event()
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.games: list[GameInfo] = []
        self.running = False

        self.ui_state = uistate.load()
        self.tray: tray.TrayIcon | None = None
        # Tray callbacks arrive on the Win32 thread; Tk reads them here.
        self.tray_events: queue.Queue[str] = queue.Queue()
        self.tray_state = {"running": False, "can_start": False, "overlay": False}
        self.quitting = False
        self.welcome_shown = False
        self.log_window: LogWindow | None = None
        self.overlay: StatsOverlay | None = None
        self.overlay_visible = False

        root.title("游戏翻译器")
        root.geometry("760x470")
        root.minsize(680, 420)
        try:
            root.iconbitmap(default=str(tray.asset("gt.ico")))
        except tk.TclError:
            pass

        self._build()
        self._build_overlay()
        self._start_tray()
        self.refresh_games(select=autostart)
        if autostart:
            self.attach_var.set(attach)
            self.root.after(800, self.autostarted)
        self.root.after(700, self.maybe_welcome)
        self.root.after(2500, self.auto_update_check)

    # -- layout ------------------------------------------------------------

    def _build(self) -> None:
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        header = ttk.Frame(frame)
        header.grid(row=0, column=0, sticky="we")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="游戏", font=("", 10, "bold")).grid(row=0, column=0, sticky="w")

        buttons = ttk.Frame(header)
        buttons.grid(row=0, column=1, sticky="e")
        self.add_button = ttk.Button(buttons, text="添加游戏…", command=self.add)
        self.add_button.pack(side="left")
        self.edit_button = ttk.Button(buttons, text="修改…", command=self.edit, state="disabled")
        self.edit_button.pack(side="left", padx=(6, 0))
        self.remove_button = ttk.Button(buttons, text="删除", command=self.remove, state="disabled")
        self.remove_button.pack(side="left", padx=(6, 0))
        self.settings_button = ttk.Button(buttons, text="模型设置…", command=self.open_settings)
        self.settings_button.pack(side="left", padx=(6, 0))
        self.refresh_button = ttk.Button(buttons, text="刷新", command=lambda: self.refresh_games())
        self.refresh_button.pack(side="left", padx=(6, 0))

        columns = ("name", "engine", "strategy", "tm", "run", "state")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=9,
                                 selectmode="browse")
        for key, text, width, anchor in (
                ("name", "名称", 210, "w"),
                ("engine", "引擎", 130, "w"),
                ("strategy", "策略", 130, "w"),
                ("tm", "词库", 70, "e"),
                ("run", "上次运行", 120, "w"),
                ("state", "状态", 70, "w")):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "name"))
        self.tree.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        scroll.grid(row=1, column=1, sticky="ns", pady=(6, 0))
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_buttons())
        self.tree.bind("<Double-1>", lambda _e: self.start())

        run = ttk.LabelFrame(frame, text="翻译", padding=10)
        run.grid(row=2, column=0, columnspan=2, sticky="we", pady=(10, 0))
        run.columnconfigure(0, weight=1)
        self.attach_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(run, text="游戏已经开着（只附加，不重启它）",
                        variable=self.attach_var).grid(row=0, column=0, sticky="w")
        self.memory_only_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(run, text="只查词库，不调用模型",
                        variable=self.memory_only_var).grid(row=1, column=0, sticky="w", pady=2)

        self.start_button = ttk.Button(run, text="开始翻译", command=self.start, state="disabled")
        self.start_button.grid(row=0, column=1, rowspan=2, padx=(10, 0), sticky="ns")
        self.stop_button = ttk.Button(run, text="停止", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=2, rowspan=2, padx=(6, 0), sticky="ns")

        tools = ttk.Frame(run)
        tools.grid(row=2, column=0, columnspan=3, sticky="we", pady=(6, 0))
        ttk.Button(tools, text="导出未命中…", command=self.export_unmatched).pack(side="left")
        ttk.Button(tools, text="导入回传译文…", command=self.import_translations).pack(
            side="left", padx=(6, 0))
        ttk.Button(tools, text="运行记录…", command=self.open_logs).pack(side="left", padx=(6, 0))
        self.overlay_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tools, text="悬浮窗", variable=self.overlay_var,
                        command=self.toggle_overlay).pack(side="right")
        ttk.Label(tools, text="词库覆盖不到的句子导成 Word，翻完再导回来。",
                  foreground="#555").pack(side="left", padx=(10, 0))

        self.status = tk.StringVar(value="点「添加游戏…」选一个 exe，程序会自己认出引擎并建好配置。")
        ttk.Label(frame, textvariable=self.status, wraplength=700, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.counts = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.counts).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        menubar = tk.Menu(self.root)
        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="导出游戏包…", command=self.export_pack)
        tools_menu.add_command(label="导入游戏包…", command=self.import_pack)
        tools_menu.add_separator()
        tools_menu.add_command(label="检查更新…", command=self.check_updates)
        tools_menu.add_command(label="更新设置…", command=self.update_settings)
        menubar.add_cascade(label="工具", menu=tools_menu)
        self.root.configure(menu=menubar)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(300, self.drain_events)
        self.root.after(1000, self.refresh_stats)

    # -- list --------------------------------------------------------------

    def refresh_games(self, select: str | None = None) -> None:
        keep = select or self.selected_id()
        self.games = list_games()
        self.tree.delete(*self.tree.get_children())
        for game in self.games:
            self.tree.insert("", "end", iid=game.id, values=(
                game.name,
                game.engine,
                STRATEGY_LABELS.get(game.strategy, game.strategy),
                game.tm_count if game.tm_count >= 0 else "读不到",
                _when(game.last_run),
                "" if game.exe_exists else "路径失效",
            ))
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
            self.tree.see(keep)
        elif self.games:
            first = self.games[0].id
            self.tree.selection_set(first)
        self._sync_buttons()

    def selected_id(self) -> str | None:
        selection = self.tree.selection() if hasattr(self, "tree") else ()
        return selection[0] if selection else None

    def selected(self) -> GameInfo | None:
        game_id = self.selected_id()
        return next((g for g in self.games if g.id == game_id), None)

    def _sync_buttons(self) -> None:
        game = self.selected()
        running = self.running
        has = bool(game) and not game.broken
        for button in (self.edit_button, self.remove_button):
            button.configure(state="normal" if has and not running else "disabled")
        can_start = has and game.exe_exists and not running
        self.start_button.configure(state="normal" if can_start else "disabled")
        self.add_button.configure(state="disabled" if running else "normal")
        # The tray menu is built on another thread; only plain values cross.
        self.tray_state["running"] = running
        self.tray_state["can_start"] = bool(can_start)

    # -- add / edit / remove -----------------------------------------------

    def add(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, title="选择游戏程序", filetypes=EXE_TYPES)
        if not path:
            return
        engine = detect_engine(path)
        if not engine.supported:
            messagebox.showinfo(
                "添加游戏",
                f"检测到 {engine.label}。\n\n{engine.reason}\n\n这一版还不能翻译这类游戏。",
                parent=self.root)
            return
        default = Path(path).parent.name or Path(path).stem
        name = simpledialog.askstring(
            "添加游戏", f"检测到 {engine.label}。\n给这个游戏起个名字：",
            initialvalue=default, parent=self.root)
        if not name:
            return
        try:
            game_id, _ = add_game(path, name)
        except (ConfigError, OSError) as exc:
            messagebox.showerror("添加游戏", str(exc), parent=self.root)
            return
        self.log.info("gui: added game %s (%s)", game_id, path)
        self.refresh_games(select=game_id)
        self.status.set("已添加。双击游戏或按「开始翻译」启动。")

    def edit(self) -> None:
        game = self.selected()
        if not game:
            return
        dialog = EditDialog(self.root, game, list(settings.definitions()))
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        name, path, port, strategy, backend = dialog.result
        try:
            if name != game.name:
                rename_game(game.id, name)
            if Path(path) != Path(game.exe):
                set_exe(game.id, path)
            if port != game.port:
                set_port(game.id, port)
            if (strategy, backend) != (game.strategy, game.backend):
                set_strategy(game.id, strategy, None if backend == "default" else backend)
        except (ConfigError, OSError) as exc:
            messagebox.showerror("修改游戏", str(exc), parent=self.root)
        self.refresh_games(select=game.id)

    # -- portable packs and updates ----------------------------------------

    def export_pack(self) -> None:
        game = self.selected()
        if not game:
            messagebox.showinfo("导出游戏包", "先在列表里选一款游戏。", parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title="导出游戏包", defaultextension=".gtpack",
            initialfile=f"{game.id}.gtpack", initialdir=str(PROFILE_DIR),
            filetypes=[("游戏包", "*.gtpack"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            result = pack.export_pack(game.id, Path(path))
        except Exception as exc:
            self.log.exception("pack export failed")
            messagebox.showerror("导出游戏包", str(exc), parent=self.root)
            return
        self.status.set(
            f"已导出到 {result['pack']}：词条 {result['counts']['tm']} 条，"
            f"术语 {result['counts']['glossary']} 条，共 {_mb(result['size'])}。")

    def import_pack(self) -> None:
        if self.running:
            messagebox.showinfo("导入游戏包", "先停止当前翻译再导入。", parent=self.root)
            return
        path = filedialog.askopenfilename(
            parent=self.root, title="导入游戏包",
            filetypes=[("游戏包", "*.gtpack"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            manifest = pack.read_manifest(Path(path))
        except pack.PackError as exc:
            messagebox.showerror("导入游戏包", str(exc), parent=self.root)
            return
        game_id = manifest.get("id") or "imported"
        replace = True
        if (PROFILE_DIR / game_id / "profile.json").exists():
            keep = messagebox.askyesnocancel(
                "导入游戏包",
                f"本地已经有「{manifest.get('name') or game_id}」。\n\n"
                "「是」：保留本地的配置与术语表，只并入词库。\n"
                "「否」：配置与术语表也用包里的覆盖。\n"
                "「取消」：什么都不做。",
                parent=self.root)
            if keep is None:
                return
            replace = not keep
        try:
            result = pack.import_pack(Path(path), None, replace_profile=replace)
        except Exception as exc:
            self.log.exception("pack import failed")
            messagebox.showerror("导入游戏包", str(exc), parent=self.root)
            return
        tm = result["tm"]
        self.refresh_games(select=result["id"])
        self.status.set(
            f"已导入 {result['name'] or result['id']}：新增 {tm['added']} 条，"
            f"更新 {tm['updated']} 条，原样 {tm['unchanged']} 条。")

    def update_settings(self) -> None:
        config = update.load_config()
        url = simpledialog.askstring(
            "更新设置", "更新信息文件（manifest）的地址，留空表示不检查：",
            initialvalue=config.get("manifestUrl", ""), parent=self.root)
        if url is None:
            return
        config["manifestUrl"] = url.strip()
        config["autoCheck"] = messagebox.askyesno(
            "更新设置", "每次启动时自动检查更新吗？", parent=self.root)
        try:
            update.save_config(config)
        except OSError as exc:
            messagebox.showerror("更新设置", str(exc), parent=self.root)
            return
        self.status.set("更新设置已保存。")

    def auto_update_check(self) -> None:
        if not update.load_config().get("manifestUrl"):
            return
        threading.Thread(target=self._update_worker, args=(True,), daemon=True).start()

    def check_updates(self) -> None:
        if not update.load_config().get("manifestUrl"):
            self.update_settings()
            if not update.load_config().get("manifestUrl"):
                return
        self.status.set("正在检查更新…")
        threading.Thread(target=self._update_worker, args=(False,), daemon=True).start()

    def _update_worker(self, quiet: bool) -> None:
        try:
            info = update.auto_check() if quiet else update.check()
        except Exception as exc:  # a background check must never take the UI down
            info = {"ok": False, "reason": str(exc), "current": "?"}
        self.events.put(("update", {"info": info, "quiet": quiet}))

    def _on_update_result(self, payload: dict) -> None:
        info = payload["info"]
        if info is None:
            return
        if not info.get("ok"):
            if not payload["quiet"]:
                messagebox.showerror("检查更新", info.get("reason", "检查失败"), parent=self.root)
            return
        if not info.get("available"):
            if not payload["quiet"]:
                messagebox.showinfo("检查更新", f"已经是最新版本 {info['current']}。",
                                    parent=self.root)
            return
        if payload["quiet"]:
            self.status.set(
                f"有新版本 {info['latest']}（当前 {info['current']}），"
                "可从「工具 → 检查更新」安装。")
            return
        notes = (info.get("notes") or "").strip()
        message = f"有新版本 {info['latest']}（当前 {info['current']}）。"
        if notes:
            message += "\n\n" + notes
        message += "\n\n现在下载并安装吗？下载完成后程序退出并自动重开。"
        if not messagebox.askyesno("检查更新", message, parent=self.root):
            return
        try:
            downloaded = update.download(info)
            update.apply(downloaded)
        except Exception as exc:
            self.log.exception("update failed")
            messagebox.showerror("检查更新", str(exc), parent=self.root)
            return
        self.quit_app()

    def export_unmatched(self) -> None:
        game = self.selected()
        if not game:
            return
        try:
            profile = Profile.load(game.id)
        except (ConfigError, OSError) as exc:
            messagebox.showerror("导出未命中", f"读配置失败：{exc}", parent=self.root)
            return
        source = profile.dir / "unmatched.jsonl"
        if self.running and self.runtime is not None:
            try:
                self.runtime.dump_unmatched(source)
            except OSError as exc:
                messagebox.showerror("导出未命中", f"写不进未命中列表：{exc}", parent=self.root)
                return
        if not source.exists() or not source.read_text(encoding="utf-8").strip():
            messagebox.showinfo(
                "导出未命中",
                "还没有未命中的句子。先跑一段游戏再停下，程序会自动把它们记下来。",
                parent=self.root)
            return
        try:
            result = exchange.export_unmatched(source, profile.dir / "export")
        except Exception as exc:  # docx writing can fail for a few reasons
            self.log.exception("export failed")
            messagebox.showerror("导出未命中", str(exc), parent=self.root)
            return
        self.log.info("exported %d unmatched rows to %s", result["count"], result["docx"])
        self.status.set(f"导出 {result['count']} 条到 {result['docx'].parent}。")
        messagebox.showinfo(
            "导出未命中",
            f"导出 {result['count']} 条，一段一句。\n\nWord：{result['docx'].name}\n"
            f"同目录还有同名的 .jsonl 映射，导入时要用它。\n\n"
            "把 Word 交给文档翻译服务，翻完用「导入回传译文…」选回来。",
            parent=self.root)

    def import_translations(self) -> None:
        game = self.selected()
        if not game:
            return
        path = filedialog.askopenfilename(
            parent=self.root, title="选择翻译服务回传的 Word 文档",
            filetypes=[("Word 文档", "*.docx")])
        if not path:
            return
        docx_path = Path(path)
        try:
            profile = Profile.load(game.id)
        except (ConfigError, OSError) as exc:
            messagebox.showerror("导入译文", f"读配置失败：{exc}", parent=self.root)
            return

        mapping = docx_path.with_suffix(".jsonl")
        if not mapping.exists():
            export_dir = profile.dir / "export"
            candidates = sorted(export_dir.glob("unmatched_*.jsonl")) if export_dir.exists() else []
            if not candidates:
                messagebox.showwarning(
                    "导入译文",
                    "没找到这次导出的映射文件（和 Word 同名的 .jsonl）。\n"
                    "先在「导出未命中…」里导出，才会生成它。",
                    parent=self.root)
                return
            mapping = candidates[-1]

        self.status.set("正在对回译文……")
        self.root.update_idletasks()
        try:
            report = exchange.import_translated(mapping, docx_path, profile.tm_db)
        except Exception as exc:
            self.log.exception("import failed")
            messagebox.showerror("导入译文", str(exc), parent=self.root)
            self.status.set("导入失败，原来的词库没有动。")
            return
        merged = report.get("tm") or {}
        self.log.info("imported %s: written=%d tm=%s", docx_path.name, report["written"], merged)
        self.refresh_games(select=game.id)
        messagebox.showinfo(
            "导入译文",
            f"写入 {report['written']} 条，词库新增 {merged.get('added', 0)} 条、"
            f"更新 {merged.get('updated', 0)} 条。\n"
            f"对不上的 {len(report['problems'])} 条没有进库，明细在：\n{report['report']}\n\n"
            "下次开始翻译时生效。",
            parent=self.root)
        self.status.set(f"已导入 {report['written']} 条。")

    def open_settings(self) -> None:
        game = self.selected()
        dialog = SettingsDialog(self.root, self.log, game)
        self.root.wait_window(dialog)
        self.refresh_games(select=game.id if game else None)

    def remove(self) -> None:
        game = self.selected()
        if not game:
            return
        size = game_size(game.id)
        if not messagebox.askyesno(
                "删除游戏",
                f"删除「{game.name}」？\n\n配置和词库会一起删掉（约 {_mb(size)}），删了不能恢复。\n"
                "游戏本身不受影响。",
                parent=self.root):
            return
        try:
            remove_game(game.id)
        except (ConfigError, OSError) as exc:
            messagebox.showerror("删除游戏", str(exc), parent=self.root)
            return
        self.log.info("gui: removed game %s", game.id)
        self.status.set("已删除。")
        self.refresh_games()

    # -- run ---------------------------------------------------------------

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        game = self.selected()
        if not game:
            return
        try:
            profile = Profile.load(game.id)
        except (ConfigError, OSError) as exc:
            messagebox.showerror("游戏翻译器", f"读配置失败：{exc}", parent=self.root)
            return
        if self.attach_var.get():
            profile.adapter_config["attachOnly"] = True
            # "The game is already running" is the common case, but a stale
            # checkbox should not be a dead end: if nothing is listening on
            # the debug port, launch it instead.
            from .adapters.electron_cdp import _port_open

            port = int(profile.adapter_config.get("port", 9222))
            if not _port_open(port):
                profile.adapter_config["attachOnly"] = False
                self.status.set(f"{port} 端口没有游戏在跑，改为自己启动。")

        self.stop_event.clear()
        self.set_running(True)
        self.show_overlay()
        if self.tray is not None:
            # Registered from this thread on the tray window; the Win32 side
            # does the actual RegisterHotKey on its own thread.
            if not self.tray.register_hotkey(profile.hotkey):
                self.log.info("global hotkey unavailable; the in-page %s still works "
                              "while the game has focus", profile.hotkey)
        self.status.set(f"正在启动「{game.name}」，第一次连接要等游戏窗口出来……")
        thread = threading.Thread(target=self._work, args=(profile,), daemon=True, name="gt-gui")
        self.thread = thread
        thread.start()

    def _work(self, profile: Profile) -> None:
        try:
            from .runtime import TranslationRuntime

            self.runtime = TranslationRuntime(profile, self.log, use_backends=not self.memory_only_var.get())
            self.log.info("gui: starting profile=%s attach=%s", profile.id, profile.adapter_config.get("attachOnly"))
            self.events.put(("ready", "已连接，正在替换画面上的文字。按 %s 切换中文与原文。"
                             % profile.hotkey))
            self.runtime.run(duration=None, stats_every=30.0, stop_event=self.stop_event)
            self.events.put(("stopped", "已停止，画面已还原成原文。"))
        except Exception as exc:  # the GUI must survive anything the runtime throws
            self.log.exception("gui: runtime failed")
            self.events.put(("error", f"出错：{exc}"))

    def stop(self) -> None:
        self.status.set("正在停止……")
        self.stop_event.set()

    # -- tray, hotkey, overlay, log window ---------------------------------

    def _start_tray(self) -> None:
        self.tray = tray.TrayIcon(self.log, self.tray_events.put, self._tray_menu,
                                  icon_path=tray.asset("gt.ico"))
        if not self.tray.start():
            self.tray = None
            self.log.info("no tray icon; closing the window will quit")

    def _tray_menu(self) -> list:
        state = self.tray_state
        return [
            ("show", "显示主窗口", True),
            None,
            ("start", "开始翻译", bool(state.get("can_start"))),
            ("stop", "停止翻译", bool(state.get("running"))),
            None,
            ("overlay", "隐藏悬浮窗" if state.get("overlay") else "显示悬浮窗", True),
            ("logs", "运行记录…", True),
            None,
            ("quit", "退出", True),
        ]

    def drain_tray(self) -> None:
        try:
            while True:
                self._tray_action(self.tray_events.get_nowait())
        except queue.Empty:
            pass

    def _tray_action(self, action: str) -> None:
        if action == "show":
            self.show_window()
        elif action == "start":
            self.show_window()
            self.start()
        elif action == "stop":
            self.stop()
        elif action == "overlay":
            self.toggle_overlay()
        elif action == "logs":
            self.show_window()
            self.open_logs()
        elif action == "quit":
            self.quit_app()
        elif action == "hotkey":
            self.toggle_mode()

    def hide_to_tray(self) -> None:
        self.root.withdraw()
        if not self.ui_state.get("tray_hint"):
            self.tray.notify("游戏翻译器", "已缩到系统托盘。右键图标可以打开或退出。")
            self.ui_state["tray_hint"] = True
            uistate.save(self.ui_state)

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def toggle_mode(self) -> None:
        runtime = self.runtime
        if runtime is None:
            self.status.set("还没开始翻译，热键现在没有可切换的内容。")
            return

        def work() -> None:
            try:
                mode = runtime.toggle_mode()
                self.events.put(("mode", "已切换到原文。" if mode == "original"
                                 else "已切换到中文。"))
            except Exception as exc:
                self.log.exception("mode toggle failed")
                self.events.put(("status", f"切换失败：{exc}"))

        threading.Thread(target=work, daemon=True, name="gt-toggle").start()

    def _build_overlay(self) -> None:
        self.overlay = StatsOverlay(self.root, on_hide=self._overlay_closed,
                                    position=self._saved_overlay_pos())
        if self.ui_state.get("overlay"):
            self.overlay_visible = True
            self.overlay_var.set(True)
            self.tray_state["overlay"] = True
            self.overlay.show(self._saved_overlay_pos())

    def _saved_overlay_pos(self) -> tuple[int, int] | None:
        pos = self.ui_state.get("overlay_pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            try:
                return int(pos[0]), int(pos[1])
            except (TypeError, ValueError):
                return None
        return None

    def _overlay_closed(self) -> None:
        self.overlay_visible = False
        self.overlay_var.set(False)
        self.tray_state["overlay"] = False

    def show_overlay(self) -> None:
        if self.overlay is None:
            return
        self.overlay.show(self._saved_overlay_pos())
        self.overlay_visible = True
        self.overlay_var.set(True)
        self.tray_state["overlay"] = True

    def hide_overlay(self) -> None:
        if self.overlay is not None:
            self.overlay.hide()  # its on_hide callback clears the flags

    def toggle_overlay(self) -> None:
        if self.overlay_visible:
            self.hide_overlay()
        else:
            self.show_overlay()

    def open_logs(self) -> None:
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.deiconify()
            self.log_window.lift()
            return
        game = self.selected()
        self.log_window = LogWindow(
            self.root, self.log, game,
            (PROFILE_DIR / game.id) if game is not None else None)

    def autostarted(self) -> None:
        """Start the profile named on the command line, if it still exists."""
        if self.selected_id() != self.autostart:
            self.status.set(f"找不到游戏「{self.autostart}」，请从列表里选一个。")
            return
        self.log.info("gui: autostart %s", self.autostart)
        self.start()

    def maybe_welcome(self) -> None:
        if self.welcome_shown or self.games or self.quitting:
            return
        self.welcome_shown = True
        self.log.info("gui: no games yet, showing the first-run guide")
        WelcomeDialog(self.root, on_add=self.add, on_settings=self.open_settings)

    def _save_ui(self) -> None:
        self.ui_state["overlay"] = self.overlay_visible
        if self.overlay is not None:
            try:
                x, y = self.overlay.position()
                self.ui_state["overlay_pos"] = [x, y]
            except tk.TclError:
                pass
        uistate.save(self.ui_state)

    def on_close(self) -> None:
        if self.tray is not None and self.tray.alive and not self.quitting:
            self.hide_to_tray()
            return
        self.quit_app()

    def quit_app(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=6)
        self._save_ui()
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        self.root.destroy()

    # -- ui plumbing -------------------------------------------------------

    def set_running(self, running: bool) -> None:
        self.running = running
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.tree.configure(selectmode="none" if running else "browse")
        self._sync_buttons()

    def drain_events(self) -> None:
        self.drain_tray()
        try:
            while True:
                kind, message = self.events.get_nowait()
                if kind == "update":
                    self._on_update_result(message)
                    continue
                self.status.set(message)
                if kind in ("stopped", "error"):
                    self.set_running(False)
                    self.counts.set("")
                    self.runtime = None
                    if self.tray is not None:
                        self.tray.unregister_hotkey()
                    self.refresh_games()
        except queue.Empty:
            pass
        self.root.after(300, self.drain_events)

    def refresh_stats(self) -> None:
        runtime = self.runtime
        data = runtime.snapshot() if runtime is not None else None
        if data is not None:
            self.counts.set(
                "看到 %d 条，词库命中 %d，缓存命中 %d，已替换 %d" % (
                    data.get("seen", 0), data.get("tm_hits", 0),
                    data.get("cache_hits", 0), data.get("applied", 0)))
        if self.overlay_visible and self.overlay is not None:
            self.overlay.update_stats(data)
        self.root.after(1000, self.refresh_stats)


def main(autostart: str | None = None, attach: bool = True) -> int:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except tk.TclError:
        pass
    App(root, autostart=autostart, attach=attach)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
