"""Build everything a release needs.

    python tools/build_release.py              # app, setup, portable zip
    python tools/build_release.py --skip-app   # reuse dist/GameTranslator.exe

Outputs in dist/:
    GameTranslator.exe          the application
    GameTranslator_Setup.exe    installer (no admin needed, HKCU only)
    game-translator.zip         portable folder
    update.json                 manifest template for gt update
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
ICON = ROOT / "gt" / "assets" / "gt.ico"
APP = "GameTranslator"
SETUP = "GameTranslator_Setup"


def version() -> str:
    text = (ROOT / "gt" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def pyinstaller(*args: str) -> None:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", *args]
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)


def build_app() -> None:
    pyinstaller("--windowed", "--onefile", "--name", APP,
                "--icon", str(ICON),
                "--add-data", "gt/js;gt/js",
                "launch.py")


# No memory ships in a release. The entries come from community patches and
# MTool dictionaries, which are not ours to redistribute; the game still runs
# without them (every line goes to the backend and lands in the cache), and the
# game stays in the list because the profile itself does ship.
MEMORY_FILES = {"tm.sqlite", "tm.mtool-backup.sqlite"}


def fill_dist() -> None:
    """Things the exe needs beside it that PyInstaller does not know about."""
    DIST.mkdir(exist_ok=True)
    profiles = DIST / "profiles"
    profiles.mkdir(exist_ok=True)

    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = {name for name in names if name in MEMORY_FILES}
        for name in skipped:
            print(f"  skip {name} in {Path(directory).name} (not redistributable)")
        return skipped

    # Every profile in the working tree ships. Electron games need this: they
    # are refused by "添加游戏" by design (selectors have to be written per
    # game), so a pre-built profile is the only way in.
    source_root = ROOT / "profiles"
    for source in sorted(source_root.iterdir()):
        if not (source / "profile.json").is_file():
            continue
        target = profiles / source.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target, ignore=ignore)
    help_text = ROOT / "使用说明.txt"
    if help_text.exists():
        shutil.copy2(help_text, DIST / "使用说明.txt")
    (DIST / "logs").mkdir(exist_ok=True)


def build_payload() -> Path:
    BUILD.mkdir(exist_ok=True)
    payload = BUILD / "payload.zip"
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("version.txt", version())
        for path in sorted(DIST.rglob("*")):
            if path.is_dir():
                continue
            if path.name.startswith(SETUP):
                continue
            if path.suffix == ".zip":
                continue
            zf.write(path, path.relative_to(DIST).as_posix())
    return payload


def build_setup(payload: Path) -> None:
    pyinstaller("--windowed", "--onefile", "--name", SETUP,
                "--icon", str(ICON),
                "--add-data", f"{payload};.",
                str(ROOT / "installer" / "setup_main.py"))


def build_portable() -> Path:
    target = DIST / "game-translator.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(DIST.rglob("*")):
            if path.is_dir():
                continue
            if path.name.startswith(SETUP) or path.suffix == ".zip":
                continue
            zf.write(path, path.relative_to(DIST).as_posix())
    return target


# Releases live here; both names are stable across versions because GitHub
# redirects "latest" to whatever the newest release holds.
RELEASE_BASE = f"https://github.com/RenJiyi163/GameTranslator/releases/latest/download/"


def write_manifest(notes: str = "") -> Path:
    exe = DIST / f"{APP}.exe"
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    target = DIST / "update.json"
    # The file doubles as the app's local config, so keep whatever the user
    # pointed at (or the default) when regenerating it on a rebuild.
    previous: dict = {}
    if target.exists():
        try:
            previous = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    manifest = {
        "version": version(),
        "url": RELEASE_BASE + f"{APP}.exe",
        "sha256": digest,
        "size": exe.stat().st_size,
        "notes": notes,
        "manifestUrl": previous.get("manifestUrl") or RELEASE_BASE + "update.json",
        "autoCheck": previous.get("autoCheck", True),
    }
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-app", action="store_true", help="reuse dist/GameTranslator.exe")
    parser.add_argument("--skip-setup", action="store_true", help="do not rebuild the installer")
    parser.add_argument("--notes", default="", help="release notes copied into update.json")
    args = parser.parse_args()

    if not args.skip_app:
        build_app()
    if not (DIST / f"{APP}.exe").exists():
        print("dist/GameTranslator.exe missing; run without --skip-app", file=sys.stderr)
        return 2
    fill_dist()

    payload = build_payload()
    if not args.skip_setup:
        build_setup(payload)
    portable = build_portable()
    manifest = write_manifest(args.notes)

    for path in (DIST / f"{APP}.exe", DIST / f"{SETUP}.exe", portable, manifest):
        if path.exists():
            print(f"{path.name:32} {path.stat().st_size / 1024 / 1024:7.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
