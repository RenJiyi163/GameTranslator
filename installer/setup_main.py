"""The installer that ships as GameTranslator_Setup.exe.

It is a PyInstaller onefile build with the application bundled as
`payload.zip`; running it unpacks that archive into the user's profile
directory, adds shortcuts and an entry in Windows' uninstall list. Nothing is
written outside HKCU and %LOCALAPPDATA%, so no administrator prompt appears.

The uninstaller is a batch file plus a PowerShell script written at install
time. The batch has to stay pure ASCII (cmd reads it as CP936 on this machine)
while the script is written as UTF-8 with a BOM so the Chinese shortcut names
survive.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
import winreg
import zipfile
from pathlib import Path
from tkinter import messagebox, ttk

APP_NAME = "GameTranslator"
DISPLAY = "游戏翻译器"
KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}"

# The copy in %TEMP% is not just so the script can run from anywhere: the last
# thing the PowerShell script does is delete the folder this batch lives in,
# and cmd.exe reads a batch file line by line. Handing over to the temp copy
# releases the original before it disappears. The temp copy stays behind (a
# few hundred bytes, overwritten by the next run): deleting a batch file while
# it runs is what makes cmd.exe report "找不到指定的文件" and exit 1.
UNINSTALL_CMD = """@echo off
setlocal
if /i "%~1"=="/fromtemp" goto work
set "HERE=%~dp0"
copy /y "%~f0" "%TEMP%\\gt-uninstall.cmd" >nul || exit /b 1
copy /y "%HERE%uninstall.ps1" "%TEMP%\\gt-uninstall.ps1" >nul || exit /b 1
"%TEMP%\\gt-uninstall.cmd" /fromtemp "%HERE%"
exit /b %errorlevel%
:work
powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\\gt-uninstall.ps1" -Dir "%~2."
set "RC=%errorlevel%"
del "%TEMP%\\gt-uninstall.ps1" >nul 2>&1
exit /b %RC%
"""

# Placeholders are `%NAME%` rather than `{}`: the script is full of PowerShell
# braces, and `str.format` cannot tell those from its own fields.
UNINSTALL_PS1 = """param([string]$Dir)
$ErrorActionPreference = "SilentlyContinue"
if (-not $Dir) { $Dir = "%INSTALL_DIR%" }
Stop-Process -Name "%APP%" -Force
Remove-Item (Join-Path $env:APPDATA "Microsoft\\Windows\\Start Menu\\Programs\\%DISPLAY%.lnk") -Force
Remove-Item (Join-Path ([Environment]::GetFolderPath("Desktop")) "%DISPLAY%.lnk") -Force
Remove-Item "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\%APP%" -Recurse -Force
Start-Sleep -Milliseconds 900
Remove-Item $Dir -Recurse -Force
if (Test-Path $Dir) {
    Start-Sleep -Milliseconds 800
    Remove-Item $Dir -Recurse -Force
}
# SilentContinue would let a failed removal look like a successful uninstall.
if (Test-Path $Dir) { exit 1 }
"""

SHORTCUT_PS = """$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut('{link}')
$link.TargetPath = '{exe}'
$link.WorkingDirectory = '{workdir}'
$link.IconLocation = '{exe}'
$link.Description = '{display}'
$link.Save()
"""


def payload_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / "payload.zip"


def payload_version() -> str:
    try:
        with zipfile.ZipFile(payload_path()) as zf:
            return zf.read("version.txt").decode("utf-8").strip()
    except (OSError, KeyError, zipfile.BadZipFile):
        return "0"


def default_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / APP_NAME


def run_powershell(script: str) -> bool:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return result.returncode == 0


def desktop_dir() -> Path:
    """The Desktop as Windows sees it (OneDrive moves it for some accounts)."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Environment]::GetFolderPath('Desktop')"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    text = (result.stdout or "").strip()
    if text:
        return Path(text)
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def make_shortcut(link: Path, exe: Path, workdir: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    run_powershell(SHORTCUT_PS.format(
        link=str(link), exe=str(exe), workdir=str(workdir), display=DISPLAY))


def write_uninstaller(target: Path) -> None:
    (target / "uninstall.cmd").write_text(UNINSTALL_CMD, encoding="ascii", newline="\r\n")
    script = (UNINSTALL_PS1.replace("%INSTALL_DIR%", str(target))
              .replace("%APP%", APP_NAME).replace("%DISPLAY%", DISPLAY))
    (target / "uninstall.ps1").write_text(script, encoding="utf-8-sig", newline="\r\n")


def register_uninstall(target: Path, version: str) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY) as key:
        values = {
            "DisplayName": DISPLAY,
            "DisplayVersion": version,
            "InstallLocation": str(target),
            "DisplayIcon": str(target / f"{APP_NAME}.exe"),
            "UninstallString": f'"{target / "uninstall.cmd"}"',
            "Publisher": "GameTranslator",
        }
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def install(target: Path, progress) -> None:
    target.mkdir(parents=True, exist_ok=True)
    exe = target / f"{APP_NAME}.exe"
    if exe.exists():
        # Overwriting a running image fails on Windows; close it first.
        subprocess.run(["taskkill", "/f", "/im", f"{APP_NAME}.exe"],
                       capture_output=True, shell=False)
    progress("正在解压…")
    with zipfile.ZipFile(payload_path()) as zf:
        zf.extractall(target)

    progress("正在创建快捷方式…")
    make_shortcut(Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs"
                  / f"{DISPLAY}.lnk", exe, target)
    make_shortcut(desktop_dir() / f"{DISPLAY}.lnk", exe, target)

    progress("正在登记卸载信息…")
    write_uninstaller(target)
    register_uninstall(target, payload_version())


class Setup(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{DISPLAY} 安装程序")
        self.resizable(False, False)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"{DISPLAY}", font=("", 14, "bold")).pack(anchor="w")
        ttk.Label(frame, text=f"版本 {payload_version()}").pack(anchor="w", pady=(2, 0))

        box = ttk.LabelFrame(frame, text="安装位置", padding=10)
        box.pack(fill="x", pady=12)
        self.path_var = tk.StringVar(value=str(default_dir()))
        ttk.Entry(box, textvariable=self.path_var, width=58).pack(side="left", fill="x",
                                                                  expand=True)
        ttk.Button(box, text="浏览…", command=self.browse).pack(side="left", padx=(6, 0))

        self.status = tk.StringVar(value="安装到当前用户，不需要管理员权限。")
        ttk.Label(frame, textvariable=self.status, foreground="#555").pack(anchor="w")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))
        self.install_button = ttk.Button(buttons, text="安装", command=self.do_install)
        self.install_button.pack(side="right")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right", padx=(0, 6))

        self.launch_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="安装完成后运行", variable=self.launch_var).pack(
            anchor="w", pady=(8, 0))

    def browse(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(parent=self, title="选择安装位置")
        if chosen:
            self.path_var.set(chosen)

    def do_install(self) -> None:
        target = Path(self.path_var.get().strip() or default_dir())
        self.install_button.configure(state="disabled")

        def progress(text: str) -> None:
            self.status.set(text)
            self.update_idletasks()

        try:
            install(target, progress)
        except Exception as exc:  # permissions, disk, a corrupt payload
            self.install_button.configure(state="normal")
            messagebox.showerror("安装失败", str(exc), parent=self)
            return

        self.status.set("安装完成。")
        messagebox.showinfo("安装完成", f"已安装到\n{target}", parent=self)
        if self.launch_var.get():
            subprocess.Popen([str(target / f"{APP_NAME}.exe")], cwd=str(target))
        self.destroy()


def silent_target(args: list[str]) -> Path:
    for arg in args:
        if arg.upper().startswith("/D="):
            return Path(arg[3:].strip('"'))
    return default_dir()


def main() -> int:
    if not payload_path().exists():
        messagebox.showerror("安装失败", "安装包不完整：找不到 payload.zip")
        return 1
    args = sys.argv[1:]
    if any(arg.lower() in ("/s", "--silent") for arg in args):
        # No window: used by the release check and by anyone scripting a
        # deployment. /D=<dir> picks the location, as with most Windows
        # installers; without it the profile default applies.
        install(silent_target(args), lambda text: None)
        return 0
    Setup().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
