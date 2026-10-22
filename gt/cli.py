"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .config import LOG_DIR, ConfigError, Profile, list_profiles
from .tm import build as build_tm

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(verbose: bool) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("gt")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(stream)

    file_handler = logging.FileHandler(LOG_DIR / "gt.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    return root


def cmd_run(args) -> int:
    from .runtime import TranslationRuntime

    log = setup_logging(args.verbose)
    profile = Profile.load(args.profile)
    if args.attach:
        profile.adapter_config["attachOnly"] = True
    if args.port:
        profile.adapter_config["port"] = args.port

    runtime = TranslationRuntime(profile, log, use_backends=not args.no_backends)
    unmatched = Path(args.unmatched_out) if args.unmatched_out else None
    runtime.run(duration=args.duration, stats_every=args.stats_every, unmatched_out=unmatched)
    print(json.dumps(runtime.snapshot(), ensure_ascii=False, indent=2))
    return 0


def cmd_tm_build(args) -> int:
    log = setup_logging(args.verbose)
    profile = Profile.load(args.profile)
    source = Path(args.source) if args.source else profile.tm_source
    if source is None:
        log.error("profile has no tm.source and --source was not given")
        return 2
    result = build_tm(source, args.db or profile.tm_db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_tm_stats(args) -> int:
    from .tm import TranslationMemory

    profile = Profile.load(args.profile)
    memory = TranslationMemory(profile.tm_db)
    print(json.dumps(memory.stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_cache(args) -> int:
    from .cache import TranslationCache

    profile = Profile.load(args.profile)
    cache = TranslationCache(profile.cache_db, profile.id)
    if args.action == "clear":
        print(json.dumps({"cleared": cache.clear()}, ensure_ascii=False))
    else:
        print(json.dumps({"rows": cache.count(), "db": str(profile.cache_db)}, ensure_ascii=False))
    cache.close()
    return 0


def cmd_doctor(args) -> int:
    """Check the pieces a profile needs before a run, in the order they fail."""
    profile = Profile.load(args.profile)
    report: dict = {"profile": profile.id, "adapter": profile.adapter}

    exe = profile.adapter_config.get("exe")
    report["exe"] = exe or None
    report["exe_exists"] = bool(exe) and Path(exe).exists()

    report["tm_db"] = str(profile.tm_db)
    report["tm_built"] = profile.tm_db.exists()

    report["cache_db"] = str(profile.cache_db)
    report["cache_exists"] = profile.cache_db.exists()

    glossary = profile.glossary_path
    report["glossary"] = str(glossary) if glossary and glossary.exists() else None

    from .backends import REGISTRY as BACKENDS
    from .settings import definitions

    defs = definitions()
    for name in profile.backend_names():
        merged = dict(defs.get(name, {}))
        merged.update(profile.backend_config.get(name, {}))
        cls = BACKENDS.get(merged.get("type") or name)
        if cls is None:
            report.setdefault("backends", {})[name] = "unknown"
            continue
        backend = cls(merged, logging.getLogger(f"gt.backend.{name}"))
        report.setdefault("backends", {})[name] = "ready" if backend.available() else "unavailable"

    problems = []
    if profile.adapter == "electron_cdp" and not report["exe_exists"]:
        problems.append("executable missing")
    # A profile without a memory source is legitimate: the game has no
    # community translation, so everything goes through the backends.
    if not report["tm_built"] and profile.tm_source is not None:
        problems.append("translation memory not built (run: gt tm build)")
    report["problems"] = problems

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not problems else 1


def cmd_export(args) -> int:
    from . import exchange

    profile = Profile.load(args.profile)
    source = profile.dir / "unmatched.jsonl"
    if not source.exists() or not source.read_text(encoding="utf-8").strip():
        print("no unmatched strings to export")
        return 1
    out_dir = Path(args.dir) if args.dir else profile.dir / "export"
    result = exchange.export_unmatched(source, out_dir)
    print(json.dumps({"count": result["count"], "docx": str(result["docx"]),
                      "mapping": str(result["mapping"])}, ensure_ascii=False, indent=2))
    return 0


def cmd_import(args) -> int:
    from . import exchange

    profile = Profile.load(args.profile)
    docx = Path(args.docx)
    mapping = Path(args.map) if args.map else docx.with_suffix(".jsonl")
    if not mapping.exists():
        print(f"mapping not found next to the docx: {mapping}", file=sys.stderr)
        return 2
    report = exchange.import_translated(mapping, docx, profile.tm_db)
    print(json.dumps({"written": report["written"], "problems": len(report["problems"]),
                      "tm": report["tm"], "report": report["report"]},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_gui(args) -> int:
    from . import gui

    return gui.main(autostart=args.start, attach=not args.no_attach)


def cmd_pack(args) -> int:
    from . import pack

    if args.action == "export":
        result = pack.export_pack(args.profile, Path(args.out) if args.out else None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    result = pack.import_pack(Path(args.file), args.id, replace_profile=args.replace_profile)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_update(args) -> int:
    from . import update

    if args.action == "check":
        print(json.dumps(update.check(args.url), ensure_ascii=False, indent=2))
        return 0
    if args.action == "config":
        if args.url:
            config = update.load_config()
            config["manifestUrl"] = args.url
            update.save_config(config)
        print(json.dumps(update.load_config(), ensure_ascii=False, indent=2))
        return 0
    info = update.check()
    if not info.get("ok"):
        print(info.get("reason", "检查失败"), file=sys.stderr)
        return 2
    if not info.get("available"):
        print(f"已是最新版本 {info['current']}")
        return 0
    print(f"发现 {info['latest']}（当前 {info['current']}），下载中…")
    downloaded = update.download(info)
    script = update.apply(downloaded)
    print(f"已下载到 {downloaded}，退出后自动替换并重启。替换脚本：{script}")
    return 0


def cmd_game(args) -> int:
    from . import games

    if args.action == "add":
        game_id, engine = games.add_game(args.exe, args.name)
        print(f"{game_id}\t{engine.label}\t{args.exe}")
    elif args.action == "list":
        for game in games.list_games():
            last = "-" if not game.last_run else time.strftime("%Y-%m-%d %H:%M", time.localtime(game.last_run))
            print(f"{game.id}\t{game.name}\t{game.engine}\t{game.tm_count}\t{last}")
    else:
        games.remove_game(args.id)
        print(f"removed {args.id}")
    return 0


def cmd_profiles(_args) -> int:
    for name in list_profiles():
        try:
            profile = Profile.load(name)
        except ConfigError as exc:
            print(f"{name}\t<broken: {exc}>")
            continue
        print(f"{name}\t{profile.name}\t{profile.adapter}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gt", description="generic game translation runtime")
    parser.add_argument("--version", action="version", version=f"gt {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="start the translator for a profile")
    run.add_argument("--profile", required=True)
    run.add_argument("--attach", action="store_true", help="do not launch; attach to a running app")
    run.add_argument("--port", type=int, help="override the debug port")
    run.add_argument("--no-backends", action="store_true", help="memory and cache only")
    run.add_argument("--duration", type=float, help="stop after N seconds")
    run.add_argument("--stats-every", type=float, default=30.0)
    run.add_argument("--unmatched-out", help="write unresolved source strings here (JSONL)")
    run.add_argument("-v", "--verbose", action="store_true")
    run.set_defaults(func=cmd_run)

    tm = sub.add_parser("tm", help="translation memory")
    tm_sub = tm.add_subparsers(dest="action", required=True)
    build = tm_sub.add_parser("build", help="build the SQLite index from a JSONL memory")
    build.add_argument("--profile", required=True)
    build.add_argument("--source")
    build.add_argument("--db", type=Path)
    build.add_argument("-v", "--verbose", action="store_true")
    build.set_defaults(func=cmd_tm_build)
    stats = tm_sub.add_parser("stats")
    stats.add_argument("--profile", required=True)
    stats.set_defaults(func=cmd_tm_stats)

    cache = sub.add_parser("cache", help="runtime translation cache")
    cache_sub = cache.add_subparsers(dest="action", required=True)
    for action in ("stats", "clear"):
        item = cache_sub.add_parser(action)
        item.add_argument("--profile", required=True)
        item.set_defaults(func=cmd_cache, action=action)

    doctor = sub.add_parser("doctor", help="check that a profile can run")
    doctor.add_argument("--profile", required=True)
    doctor.set_defaults(func=cmd_doctor)

    profiles = sub.add_parser("profiles", help="list profiles")
    profiles.set_defaults(func=cmd_profiles)

    export = sub.add_parser("export", help="write unmatched strings as a DOCX for translation")
    export.add_argument("--profile", required=True)
    export.add_argument("--dir", help="where to write (default: profiles/<id>/export)")
    export.set_defaults(func=cmd_export)

    imp = sub.add_parser("import", help="read a returned DOCX into the translation memory")
    imp.add_argument("--profile", required=True)
    imp.add_argument("docx")
    imp.add_argument("--map", help="mapping JSONL (default: same stem as the DOCX)")
    imp.set_defaults(func=cmd_import)

    gui_cmd = sub.add_parser("gui", help="open the window, optionally starting a game at once")
    gui_cmd.add_argument("--start", help="profile id to start translating immediately")
    gui_cmd.add_argument("--no-attach", action="store_true",
                         help="launch the game instead of attaching to a running one")
    gui_cmd.set_defaults(func=cmd_gui)

    pack = sub.add_parser("pack", help="move one game's profile, glossary and memory as a file")
    pack_sub = pack.add_subparsers(dest="action", required=True)
    pack_export = pack_sub.add_parser("export")
    pack_export.add_argument("--profile", required=True)
    pack_export.add_argument("--out", help="where to write (default: profiles/<id>.gtpack)")
    pack_export.set_defaults(func=cmd_pack)
    pack_import = pack_sub.add_parser("import")
    pack_import.add_argument("file")
    pack_import.add_argument("--id", help="install under this id instead of the one in the pack")
    pack_import.add_argument("--replace-profile", action="store_true",
                             help="overwrite the local profile.json and glossary")
    pack_import.set_defaults(func=cmd_pack)

    upd = sub.add_parser("update", help="check for or install a newer build")
    upd_sub = upd.add_subparsers(dest="action", required=True)
    upd_check = upd_sub.add_parser("check")
    upd_check.add_argument("--url")
    upd_check.set_defaults(func=cmd_update)
    upd_apply = upd_sub.add_parser("apply", help="download, verify and swap in the new build")
    upd_apply.set_defaults(func=cmd_update)
    upd_config = upd_sub.add_parser("config", help="read or set update.json")
    upd_config.add_argument("--url", help="manifest URL to store")
    upd_config.set_defaults(func=cmd_update)

    game = sub.add_parser("game", help="add, list or remove a game")
    game_sub = game.add_subparsers(dest="action", required=True)
    game_add = game_sub.add_parser("add", help="detect the engine and create a profile")
    game_add.add_argument("exe")
    game_add.add_argument("--name")
    game_add.set_defaults(func=cmd_game)
    game_list = game_sub.add_parser("list")
    game_list.set_defaults(func=cmd_game)
    game_remove = game_sub.add_parser("remove", help="delete a profile and its translation memory")
    game_remove.add_argument("id")
    game_remove.set_defaults(func=cmd_game)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
