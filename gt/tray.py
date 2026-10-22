"""System tray icon and global hotkey, on ctypes alone.

Two OS features, no new dependency: a packaged one-file build already carries
a Python runtime, and pystray would drag Pillow in behind it for a 16-pixel
icon. So this talks to shell32/user32 directly.

Everything Win32 lives on one background thread with its own message loop.
The Tk thread never touches a window handle: it reads the queue this fills
(`on_event`) and posts requests back through the public methods.

The tray window is also the hotkey window. RegisterHotKey swallows the key
before the foreground game sees it, so the in-page Ctrl+Shift+T listener does
not fire for the same press; that is why registering is best-effort and the
page keeps its own listener as the fallback.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_CONTEXTMENU = 0x007B
WM_HOTKEY = 0x0312
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_REGISTER_HOTKEY = WM_APP + 2
WM_UNREGISTER_HOTKEY = WM_APP + 3

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIF_INFO = 0x10
NIIF_INFO = 0x01

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
MF_GRAYED = 0x0001
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
IDI_APPLICATION = 32512

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

HOTKEY_ID = 0x4754  # "GT"

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.WORD
user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadIconW.restype = wintypes.HANDLE
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]

WINDOW_CLASS = "GameTranslatorTray"


def asset(name: str) -> Path:
    """Where bundled data files live, frozen or from a checkout."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "gt" / "assets" / name
    return Path(__file__).resolve().parent / "assets" / name


def parse_hotkey(spec: str) -> tuple[int, int] | None:
    """'Ctrl+Shift+T' -> (modifiers, virtual key), or None if unsupported.

    Only letters, digits and F1-F24: enough for the default and for anything a
    profile would reasonably ask for without a keyboard-layout table.
    """
    parts = [p.strip().lower() for p in str(spec or "").split("+") if p.strip()]
    if not parts:
        return None
    key = parts.pop()
    if len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x6F + int(key[1:])  # VK_F1 == 0x70
    else:
        return None
    mods = 0
    for part in parts:
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part == "alt":
            mods |= MOD_ALT
        elif part in ("win", "meta"):
            mods |= MOD_WIN
        else:
            return None
    return mods, vk


class TrayIcon:
    """Tray icon plus the hidden window that global hotkeys report to.

    `on_event(name)` is called from the Win32 thread. Hand it a
    queue.Queue.put, not a Tk call.
    """

    def __init__(self, log: logging.Logger, on_event, menu, icon_path: Path | None = None):
        self.log = log
        self.on_event = on_event
        self.menu = menu
        self.icon_path = Path(icon_path) if icon_path else asset("gt.ico")

        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._hwnd = None
        self._ready = threading.Event()
        self._failed: str | None = None

        self._proc = None          # ctypes callback, kept alive for the window
        self._nid: NOTIFYICONDATAW | None = None
        self._nid_lock = threading.Lock()
        self._taskbar_created = 0
        self._hotkey: str | None = None
        # RegisterHotKey only works on the thread that owns the window
        # (ERROR_WINDOW_OF_OTHER_THREAD otherwise), so requests hop over.
        self._hotkey_lock = threading.Lock()
        self._hotkey_request: tuple[str, threading.Event, list, tuple[int, int]] | None = None
        self._hotkey_spec: tuple[int, int] | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._hwnd is not None and self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        self._thread = threading.Thread(target=self._run, name="gt-tray", daemon=True)
        self._thread.start()
        if not self._ready.wait(6):
            self._failed = "创建托盘窗口超时"
        if self._failed or self._hwnd is None:
            self.log.warning("tray unavailable: %s", self._failed or "no window")
            return False
        return True

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None
        self._hwnd = None

    # -- public actions ----------------------------------------------------

    def register_hotkey(self, spec: str) -> bool:
        parsed = parse_hotkey(spec)
        if not parsed:
            self.log.info("hotkey %r not supported by the global registrar", spec)
            return False
        return self._ask_tray_thread(spec, parsed)

    def unregister_hotkey(self) -> None:
        if self._thread_id and (self._hotkey or self._hotkey_spec):
            user32.PostThreadMessageW(self._thread_id, WM_UNREGISTER_HOTKEY, 0, 0)

    def _ask_tray_thread(self, spec: str, parsed: tuple[int, int]) -> bool:
        if not self._hwnd or not self._thread_id:
            return False
        done = threading.Event()
        result: list[bool] = []
        with self._hotkey_lock:
            self._hotkey_request = (spec, done, result, parsed)
        if not user32.PostThreadMessageW(self._thread_id, WM_REGISTER_HOTKEY, 0, 0):
            self._hotkey_request = None
            return False
        if not done.wait(3):
            self.log.warning("hotkey registration timed out")
            return False
        return bool(result and result[0])

    def _do_register_hotkey(self) -> None:
        with self._hotkey_lock:
            request = self._hotkey_request
            self._hotkey_request = None
        if request is None:
            return
        spec, done, result, parsed = request
        mods, vk = parsed
        if self._hotkey_spec is not None:
            user32.UnregisterHotKey(self._hwnd, HOTKEY_ID)
            self._hotkey_spec = None
        if user32.RegisterHotKey(self._hwnd, HOTKEY_ID, mods, vk):
            self._hotkey_spec = parsed
            self._hotkey = spec
            result.append(True)
            self.log.info("global hotkey registered: %s", spec)
        else:
            result.append(False)
            self.log.warning("RegisterHotKey(%s) failed: %d", spec, ctypes.get_last_error())
        done.set()

    def _do_unregister_hotkey(self) -> None:
        if self._hotkey_spec is not None and self._hwnd:
            user32.UnregisterHotKey(self._hwnd, HOTKEY_ID)
            self.log.info("global hotkey released: %s", self._hotkey)
        self._hotkey_spec = None
        self._hotkey = None

    def notify(self, title: str, text: str) -> None:
        """A balloon message; silently ignored when the tray is not up."""
        with self._nid_lock:
            nid = self._nid
            if nid is None:
                return
            nid.uFlags = NIF_INFO
            nid.szInfoTitle = title[:63]
            nid.szInfo = text[:255]
            nid.dwInfoFlags = NIIF_INFO
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    # -- the Win32 thread --------------------------------------------------

    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        hinst = kernel32.GetModuleHandleW(None)
        try:
            self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = self._proc = WNDPROC(self._wndproc)
            wc.hInstance = hinst
            wc.lpszClassName = WINDOW_CLASS
            if not user32.RegisterClassExW(ctypes.byref(wc)):
                raise OSError(ctypes.get_last_error(), "RegisterClassExW")
            hwnd = user32.CreateWindowExW(
                0, WINDOW_CLASS, "GameTranslator", 0, 0, 0, 0, 0,
                None, None, hinst, None)
            if not hwnd:
                raise OSError(ctypes.get_last_error(), "CreateWindowExW")
            self._hwnd = hwnd
            self._add_icon()
        except Exception as exc:  # no tray is not fatal: closing just quits
            self._failed = f"{type(exc).__name__}: {exc}"
            self.log.exception("tray thread failed")
        finally:
            self._ready.set()
        if self._failed or self._hwnd is None:
            if self._hwnd is not None:
                user32.DestroyWindow(self._hwnd)
                self._hwnd = None
            return

        msg = wintypes.MSG()
        try:
            while True:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got == 0 or got == -1:
                    break
                if not msg.hWnd and msg.message == WM_REGISTER_HOTKEY:
                    self._do_register_hotkey()
                    continue
                if not msg.hWnd and msg.message == WM_UNREGISTER_HOTKEY:
                    self._do_unregister_hotkey()
                    continue
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._remove_icon()
            user32.DestroyWindow(self._hwnd)
            user32.UnregisterClassW(WINDOW_CLASS, hinst)
            self._hwnd = None

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            if lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._show_menu(hwnd)
            elif lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._emit("show")
            return 0
        if msg == WM_HOTKEY:
            self._emit("hotkey")
            return 0
        if self._taskbar_created and msg == self._taskbar_created:
            # Explorer restarted and dropped every icon; put ours back.
            try:
                self._add_icon()
            except Exception:
                self.log.exception("could not re-add the tray icon")
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # -- tray icon ---------------------------------------------------------

    def _add_icon(self) -> None:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._load_icon()
        nid.szTip = "游戏翻译器"
        with self._nid_lock:
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                raise OSError(ctypes.get_last_error(), "Shell_NotifyIcon")
            self._nid = nid

    def _remove_icon(self) -> None:
        with self._nid_lock:
            if self._nid is not None:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
                self._nid = None

    def _load_icon(self):
        if self.icon_path and self.icon_path.exists():
            handle = user32.LoadImageW(
                None, str(self.icon_path), IMAGE_ICON, 0, 0,
                LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if handle:
                return handle
            self.log.warning("could not load %s, using the default icon", self.icon_path)
        return user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def _show_menu(self, hwnd) -> None:
        try:
            items = list(self.menu() or [])
        except Exception:
            self.log.exception("tray menu failed")
            items = [("quit", "退出", True)]
        menu = user32.CreatePopupMenu()
        actions: dict[int, str] = {}
        try:
            for item in items:
                if item is None:
                    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                    continue
                name, label, enabled = item
                ident = len(actions) + 1
                actions[ident] = name
                flags = MF_STRING | (0 if enabled else MF_GRAYED)
                user32.AppendMenuW(menu, flags, ident, label)
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            # Without the foreground call the menu stays up when the user
            # clicks elsewhere; the WM_NULL after it is the documented dance.
            user32.SetForegroundWindow(hwnd)
            choice = user32.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y, 0, hwnd, None)
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)
        if choice in actions:
            self._emit(actions[choice])

    def _emit(self, name: str) -> None:
        try:
            self.on_event(name)
        except Exception:
            self.log.exception("tray event handler failed")
