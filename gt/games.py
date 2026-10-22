"""Game registry: executable in, profile out.

Everything the user adds goes through here. Detection is a question about the
files next to the executable, not a registry lookup: games never announce
their engine, so we look for what each engine ships alongside it.

Adding a game writes `profiles/<id>/profile.json` and nothing else. The id is
derived from the name the user gives it, so the profile directory stays
readable instead of being a GUID.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters.electron_cdp import _port_open
from .config import PROFILE_DIR, STRATEGIES, ConfigError, Profile


class UnsupportedEngine(ConfigError):
    """Detected, but there is no adapter that can drive it yet."""


@dataclass(frozen=True)
class Engine:
    id: str
    label: str
    adapter: str | None
    reason: str = ""

    @property
    def supported(self) -> bool:
        return self.adapter is not None


RPGMAKER_MZ = Engine("rpgmaker_mz", "RPG Maker MZ", "rpgmaker_mv_mz")
RPGMAKER_MV = Engine("rpgmaker_mv", "RPG Maker MV", "rpgmaker_mv_mz")
RPGMAKER_ACE = Engine(
    "rpgmaker_ace", "RPG Maker VX Ace / VX / XP", None,
    "RGSS 引擎跑的是 Ruby 脚本，现在的适配器只认 MV/MZ 的 JavaScript 出字口")
ELECTRON = Engine(
    "electron", "Electron", None,
    "Electron 游戏的文字在页面里，但选择器要按游戏一一配置，自动探测不出来")
NWJS = Engine(
    "nwjs", "NW.js", None,
    "NW.js 打包的网页游戏，同样要先知道文字挂在哪些页面节点上")
RENPY = Engine("renpy", "Ren'Py", "renpy")
UNITY = Engine("unity", "Unity", None, "需要 BepInEx 插件，还没写")
UNREAL = Engine("unreal", "Unreal Engine", None, "需要 UE4SS 脚本，还没写")
UNKNOWN = Engine("unknown", "未知", None, "认不出引擎：程序旁边没有熟悉的文件")

ENGINES = {e.id: e for e in (
    RPGMAKER_MZ, RPGMAKER_MV, RPGMAKER_ACE, ELECTRON, NWJS, RENPY, UNITY, UNREAL, UNKNOWN)}

# Profiles written before the engine id was stored only say which adapter they
# use; this keeps their label as specific as the adapter allows.
ADAPTER_LABEL = {
    "rpgmaker_mv_mz": "RPG Maker MV/MZ",
    "electron_cdp": "Electron",
    "renpy": "Ren'Py",
    "unity_bepinex": "Unity",
    "unreal_ue4ss": "Unreal Engine",
    "ocr_overlay": "OCR",
}

# Timestamps and money change every frame; feeding them to a model is waste.
RPGMAKER_SKIP = [r"^[\d\s:：/\-.,年月日時分秒上午下午()]+$"]

PORT_RANGE = range(9222, 9400)


def detect_engine(exe: str | Path) -> Engine:
    """Work out what engine a program is built on, from its neighbours."""
    path = Path(exe)
    if not path.is_file():
        return UNKNOWN
    d = path.parent

    # Unreal keeps the shipping binary under Binaries/Win64, far from Content/.
    if (d.name.lower() in ("win64", "win32")
            and d.parent.name.lower() == "binaries"
            and (d.parent.parent / "Content" / "Paks").is_dir()):
        return UNREAL
    if ((d / "UnityPlayer.dll").exists()
            or (d / "GameAssembly.dll").exists()
            or any((d / f"{stem}_Data").is_dir() for stem in (path.stem, d.name))):
        return UNITY
    if ((d / "renpy").is_dir()
            or any((d / "lib").glob("py3-*"))
            or any((d / "game").glob("*.rpyc"))):
        return RENPY
    # MZ ships its engine at the top level, MV under www/.
    if ((d / "js" / "rmmz_core.js").exists()
            or ((d / "data" / "System.json").exists() and (d / "nw.dll").exists())):
        return RPGMAKER_MZ
    if (d / "www" / "js" / "rpg_core.js").exists() or (d / "www" / "data" / "System.json").exists():
        return RPGMAKER_MV
    if (any(d.glob("*.rgss3a")) or any(d.glob("*.rgss2a")) or any(d.glob("*.rgssad"))
            or any(d.glob("RGSS3*.dll")) or any(d.glob("RGSS10*.dll"))):
        return RPGMAKER_ACE
    if (d / "resources" / "app.asar").exists() or (d / "resources" / "app").is_dir():
        return ELECTRON
    if (d / "nw.dll").exists() or (d / "node.dll").exists() or (d / "package.nw").exists():
        return NWJS
    return UNKNOWN


# -- ids, ports --------------------------------------------------------------

_ILLEGAL = re.compile(r'[\\/:*?"<>|\s]+')


def _slug(text: str) -> str:
    slug = _ILLEGAL.sub("-", text.strip()).strip(".-")
    return slug[:40] or "game"


def _unique_id(root: Path, base: str) -> str:
    candidate, n = base, 2
    while (root / candidate).exists():
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _used_ports(root: Path) -> set[int]:
    ports: set[int] = set()
    if not root.exists():
        return ports
    for child in root.iterdir():
        cfg = child / "profile.json"
        if not cfg.exists():
            continue
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8"))
            ports.add(int(raw.get("adapterConfig", {}).get("port")))
        except (ValueError, TypeError, json.JSONDecodeError, OSError):
            continue
    return ports


def pick_port(root: Path | None = None) -> int:
    """A debug port no other profile claims and nothing is listening on."""
    root = Path(root or PROFILE_DIR)
    used = _used_ports(root)
    for port in PORT_RANGE:
        if port in used or _port_open(port, timeout=0.2):
            continue
        return port
    raise ConfigError("没有空闲的调试端口可用")


# -- profile templates -------------------------------------------------------

def _profile_dict(game_id: str, name: str, engine: Engine, exe: Path, port: int) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "id": game_id,
        "name": name,
        "engine": engine.id,
        "adapter": engine.adapter,
        "adapterConfig": {
            "exe": str(exe),
            "port": port,
            "urlMatch": "*",
            "startupTimeout": 90,
        },
        "selectors": [],
        "hotkey": "Ctrl+Shift+T",
        "tm": {"db": "tm.sqlite"},
        "glossary": "glossary.tsv",
        # No `backends` key on purpose: new games follow the machine-wide
        # chain in backends.json until someone pins one for this game.
        "backendConfig": {},
    }
    if engine.adapter == "rpgmaker_mv_mz":
        cfg["adapterConfig"]["restartMessage"] = True
        cfg["adapterConfig"]["skipPatterns"] = list(RPGMAKER_SKIP)
    return cfg


def _read_raw(game_id: str, root: Path) -> dict[str, Any]:
    path = root / game_id / "profile.json"
    if not path.exists():
        raise ConfigError(f"没有这个游戏：{game_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_raw(game_id: str, root: Path, raw: dict[str, Any]) -> None:
    path = root / game_id / "profile.json"
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# -- public operations -------------------------------------------------------

def add_game(exe: str | Path, name: str | None = None, root: Path | None = None) -> tuple[str, Engine]:
    """Create a profile for an executable; return (game id, detected engine)."""
    root = Path(root or PROFILE_DIR)
    path = Path(exe).expanduser()
    if not path.is_file():
        raise ConfigError(f"找不到程序：{path}")
    path = path.resolve()
    engine = detect_engine(path)
    if not engine.supported:
        raise UnsupportedEngine(f"检测到 {engine.label}，但{engine.reason}")

    display = (name or "").strip() or path.parent.name or path.stem
    root.mkdir(parents=True, exist_ok=True)
    game_id = _unique_id(root, _slug(display))
    port = pick_port(root)

    folder = root / game_id
    folder.mkdir(parents=True)
    _write_raw(game_id, root, _profile_dict(game_id, display, engine, path, port))
    (folder / "glossary.tsv").touch()
    return game_id, engine


def rename_game(game_id: str, name: str, root: Path | None = None) -> None:
    name = name.strip()
    if not name:
        raise ConfigError("名字不能为空")
    root = Path(root or PROFILE_DIR)
    raw = _read_raw(game_id, root)
    raw["name"] = name
    _write_raw(game_id, root, raw)


def set_exe(game_id: str, exe: str | Path, root: Path | None = None) -> Engine:
    """Point an existing profile at a (new) executable of the same engine."""
    root = Path(root or PROFILE_DIR)
    path = Path(exe).expanduser()
    if not path.is_file():
        raise ConfigError(f"找不到程序：{path}")
    path = path.resolve()
    engine = detect_engine(path)
    if not engine.supported:
        raise UnsupportedEngine(f"检测到 {engine.label}，但{engine.reason}")

    raw = _read_raw(game_id, root)
    if engine.adapter != raw.get("adapter"):
        raise UnsupportedEngine(
            f"这个路径是 {engine.label}，和原来的 {ADAPTER_LABEL.get(raw.get('adapter'), raw.get('adapter'))} 不是一类，"
            "要换引擎请删掉这个游戏重新添加")
    raw["adapterConfig"]["exe"] = str(path)
    raw["engine"] = engine.id
    _write_raw(game_id, root, raw)
    return engine


def set_port(game_id: str, port: int, root: Path | None = None) -> None:
    if not 1024 <= int(port) <= 65535:
        raise ConfigError(f"端口不在范围里：{port}")
    root = Path(root or PROFILE_DIR)
    raw = _read_raw(game_id, root)
    raw.setdefault("adapterConfig", {})["port"] = int(port)
    _write_raw(game_id, root, raw)


def set_strategy(game_id: str, strategy: str, backend: str | None = None,
                 root: Path | None = None) -> None:
    """Pin how one game resolves text, and which backend it may use.

    `backend=None` (or "default") means "follow the machine-wide chain"; the
    backends key is removed rather than written empty, because an empty list
    is itself the memory-only setting.
    """
    if strategy not in STRATEGIES:
        raise ConfigError(f"未知策略：{strategy}")
    root = Path(root or PROFILE_DIR)
    raw = _read_raw(game_id, root)
    raw["strategy"] = strategy
    if strategy == "memory":
        raw["backends"] = []
    elif backend and backend != "default":
        raw["backends"] = [backend]
    else:
        raw.pop("backends", None)
    _write_raw(game_id, root, raw)


def remove_game(game_id: str, root: Path | None = None) -> None:
    """Delete a profile, translation memory included."""
    root = Path(root or PROFILE_DIR).resolve()
    folder = (root / game_id).resolve()
    if folder.parent != root:
        raise ConfigError(f"拒绝删除：{folder} 不在 {root} 下面")
    if not folder.is_dir():
        raise ConfigError(f"没有这个游戏：{game_id}")
    shutil.rmtree(folder)


def game_size(game_id: str, root: Path | None = None) -> int:
    root = Path(root or PROFILE_DIR)
    total = 0
    for p in (root / game_id).rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def mark_run(profile: Profile) -> None:
    """Record that a game was started, for the 'last run' column."""
    try:
        profile.dir.mkdir(parents=True, exist_ok=True)
        (profile.dir / "state.json").write_text(
            json.dumps({"lastRun": time.time()}, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass  # a read-only install must not stop the translator from running


# -- listing -----------------------------------------------------------------

@dataclass
class GameInfo:
    id: str
    name: str
    engine: str
    adapter: str
    exe: str
    port: int | None
    tm_count: int
    last_run: float | None
    strategy: str = "tm_first"
    #: A pinned backend name, or "default" when the game follows settings.
    backend: str = "default"
    broken: str = ""

    @property
    def exe_exists(self) -> bool:
        return bool(self.exe) and Path(self.exe).is_file()


def _tm_count(db: Path) -> int:
    if not db.exists():
        return 0
    try:
        conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return -1
    try:
        return int(conn.execute("SELECT COUNT(*) FROM tm").fetchone()[0])
    except sqlite3.Error:
        return -1
    finally:
        conn.close()


def _last_run(folder: Path) -> float | None:
    path = folder / "state.json"
    if not path.exists():
        return None
    try:
        return float(json.loads(path.read_text(encoding="utf-8")).get("lastRun"))
    except (ValueError, TypeError, OSError):
        return None


def list_games(root: Path | None = None) -> list[GameInfo]:
    root = Path(root or PROFILE_DIR)
    if not root.exists():
        return []
    games: list[GameInfo] = []
    for folder in sorted(root.iterdir()):
        cfg = folder / "profile.json"
        if not cfg.is_file():
            continue
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8"))
            profile = Profile.load(folder.name, root)
        except (json.JSONDecodeError, OSError, ConfigError) as exc:
            games.append(GameInfo(folder.name, folder.name, "读取失败", "", "", None, 0, None, str(exc)))
            continue
        engine = ENGINES.get(raw.get("engine", ""))
        label = engine.label if engine else ADAPTER_LABEL.get(profile.adapter, profile.adapter)
        try:
            port = int(profile.adapter_config.get("port"))
        except (TypeError, ValueError):
            port = None
        games.append(GameInfo(
            id=profile.id,
            name=profile.name,
            engine=label,
            adapter=profile.adapter,
            exe=str(profile.adapter_config.get("exe", "")),
            port=port,
            tm_count=_tm_count(profile.tm_db),
            last_run=_last_run(folder),
            strategy=profile.effective_strategy(),
            backend=(profile.backends[0] if profile.backends else "default"),
        ))
    games.sort(key=lambda g: (g.last_run or 0), reverse=True)
    return games
