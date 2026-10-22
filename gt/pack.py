"""Portable game packs.

A pack is one zip holding everything a game needs that the program itself
does not ship with: the profile, the glossary and the translation memory.
Moving a game to another machine or handing its dictionary to someone else is
then one file, not three directories.

The format is deliberately boring so a pack stays readable without this
program: `manifest.json` plus the files it lists, nothing nested.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import zipfile
from pathlib import Path
from typing import Any

from . import __version__
from .config import PROFILE_DIR, Profile
from .text import normalize

FORMAT = "gtpack"
FORMAT_VERSION = 1
MAX_UNPACKED = 4 * 1024 * 1024 * 1024  # a dictionary this large is not a typo


class PackError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _row_count(db: Path) -> int:
    if not db.exists():
        return 0
    conn = None
    try:
        uri = f"file:{db.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        return conn.execute("SELECT COUNT(*) FROM tm").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        # `with sqlite3.connect(...)` commits but leaves the handle open, which
        # on Windows keeps the file locked for deletion.
        if conn is not None:
            conn.close()


def _snapshot(db: Path, target: Path) -> None:
    """Copy a live SQLite file without tearing a write in half.

    The runtime keeps the memory open while translating; copying the file byte
    for byte can capture a half-applied transaction. The backup API asks the
    engine for a consistent copy instead.
    """
    source = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def export_pack(profile_id: str, out_path: Path | None = None) -> dict[str, Any]:
    profile = Profile.load(profile_id)
    created = time.strftime("%Y-%m-%d %H:%M:%S")
    out_path = Path(out_path) if out_path else PROFILE_DIR / f"{profile_id}.gtpack"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    files: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(profile.dir / "profile.json", "profile.json")
        files["profile.json"] = {
            "size": (profile.dir / "profile.json").stat().st_size,
            "sha256": _sha256(profile.dir / "profile.json"),
        }

        glossary = profile.dir / (profile.glossary or "glossary.tsv")
        if glossary.exists():
            zf.write(glossary, glossary.name)
            files[glossary.name] = {"size": glossary.stat().st_size, "sha256": _sha256(glossary)}

        tm_db = profile.dir / "tm.sqlite"
        tm_rows = 0
        if tm_db.exists():
            tmp = out_path.with_suffix(".sqlite.tmp")
            if tmp.exists():
                tmp.unlink()
            try:
                _snapshot(tm_db, tmp)
                zf.write(tmp, "tm.sqlite")
                files["tm.sqlite"] = {"size": tmp.stat().st_size, "sha256": _sha256(tmp)}
                tm_rows = _row_count(tmp)
            finally:
                if tmp.exists():
                    tmp.unlink()

    manifest = {
        "format": FORMAT,
        "formatVersion": FORMAT_VERSION,
        "app": "GameTranslator",
        "appVersion": __version__,
        "created": created,
        "id": profile.id,
        "name": profile.name,
        "adapter": profile.adapter,
        "engine": profile.adapter_config.get("engine"),
        "counts": {"tm": tm_rows, "glossary": _glossary_lines(glossary)},
        "files": files,
    }
    # The manifest has to be written last but cannot be appended to a finished
    # zip, so the archive is reopened and the entry added.
    with zipfile.ZipFile(out_path, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    return {"pack": str(out_path), "size": out_path.stat().st_size, "counts": manifest["counts"]}


def _glossary_lines(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    return sum(1 for line in lines if line.strip() and not line.lstrip().startswith("#"))


def read_manifest(pack: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(pack) as zf:
            raw = zf.read("manifest.json")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise PackError(f"不是游戏包：{exc}") from exc
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PackError(f"manifest.json 损坏：{exc}") from exc
    if manifest.get("format") != FORMAT:
        raise PackError("manifest.json 里没有 gtpack 标记，可能不是本程序的包")
    if int(manifest.get("formatVersion", 0)) > FORMAT_VERSION:
        raise PackError(
            f"这个包是版本 {manifest.get('formatVersion')} 写的，本程序只认到 {FORMAT_VERSION}")
    return manifest


def _safe_extract(pack: Path, dest: Path) -> dict[str, Path]:
    """Extract only the files the manifest lists, and only flat names."""
    manifest = read_manifest(pack)
    wanted = set(manifest.get("files", {}))
    extracted: dict[str, Path] = {}
    with zipfile.ZipFile(pack) as zf:
        total = 0
        for info in zf.infolist():
            name = info.filename
            if name not in wanted:
                continue
            if "/" in name or "\\" in name or name.startswith("."):
                raise PackError(f"包里出现了不允许的路径：{name}")
            total += info.file_size
            if total > MAX_UNPACKED:
                raise PackError("包解压后过大，拒绝继续")
            target = dest / name
            with zf.open(info) as src, target.open("wb") as out:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    out.write(chunk)
            expected = manifest["files"][name].get("sha256")
            if expected and _sha256(target) != expected:
                raise PackError(f"{name} 校验不通过，包可能损坏")
            extracted[name] = target
    if "profile.json" not in extracted:
        raise PackError("包里没有 profile.json")
    return extracted


def _merge_tm(source: Path, target: Path) -> dict[str, int]:
    """Upsert every row of the packed memory into the local one."""
    added = updated = unchanged = 0
    conn = sqlite3.connect(target)
    try:
        from .tm import SCHEMA  # table shape lives with the rest of the memory code

        conn.executescript(SCHEMA)
        rows = conn.execute("SELECT key, original, translation, kind, source FROM tm").fetchall()
        existing = {row[0]: row[2] for row in rows}
        conn.execute("ATTACH DATABASE ? AS pack", (str(source),))
        try:
            incoming = conn.execute(
                "SELECT key, original, translation, kind, source FROM pack.tm").fetchall()
        finally:
            conn.execute("DETACH DATABASE pack")
        for key, original, translation, kind, src in incoming:
            # Packs may come from an older key normaliser; recompute locally so
            # an entry lands on the same key the runtime looks up.
            key = normalize(original) or key
            if key not in existing:
                conn.execute("INSERT OR REPLACE INTO tm VALUES (?,?,?,?,?)",
                             (key, original, translation, kind, src))
                added += 1
            elif existing[key] != translation:
                conn.execute("UPDATE tm SET translation = ? WHERE key = ?", (translation, key))
                updated += 1
            else:
                unchanged += 1
        conn.commit()
    finally:
        conn.close()
    return {"added": added, "updated": updated, "unchanged": unchanged}


def import_pack(pack: Path, target_id: str | None = None,
                replace_profile: bool = False) -> dict[str, Any]:
    pack = Path(pack)
    manifest = read_manifest(pack)
    game_id = target_id or manifest.get("id") or "imported"
    if game_id != manifest.get("id") and target_id is None:
        raise PackError("包里没有游戏 id")

    dest = PROFILE_DIR / game_id
    fresh = not (dest / "profile.json").exists()
    dest.mkdir(parents=True, exist_ok=True)
    staging = dest / ".import"
    if staging.exists():
        for item in staging.iterdir():
            item.unlink()
    staging.mkdir(exist_ok=True)

    try:
        files = _safe_extract(pack, staging)
        if fresh:
            (staging / "profile.json").replace(dest / "profile.json")
            result = {"profile": "created"}
        elif replace_profile:
            (staging / "profile.json").replace(dest / "profile.json")
            result = {"profile": "replaced"}
        else:
            result = {"profile": "kept"}

        if "glossary.tsv" in files:
            # Glossaries are per line and merging them by hand would need the
            # editor's rules; the pack wins when the local one is missing, and
            # an existing one is left alone and reported instead.
            local = dest / "glossary.tsv"
            if not local.exists() or replace_profile:
                files["glossary.tsv"].replace(local)
                result["glossary"] = "written"
            else:
                result["glossary"] = "kept"

        if "tm.sqlite" in files:
            result["tm"] = _merge_tm(files["tm.sqlite"], dest / "tm.sqlite")
        else:
            result["tm"] = {"added": 0, "updated": 0, "unchanged": 0}
    finally:
        for item in staging.glob("*"):
            item.unlink(missing_ok=True)
        staging.rmdir()

    result.update({"id": game_id, "name": manifest.get("name"), "pack": str(pack)})
    return result
