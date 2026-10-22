"""Pull every translatable string out of an RPG Maker MZ data directory.

Offline pre-translation needs a corpus before it needs a translator: this
walks the game's data files, collects the text the engine will draw, and
writes one JSONL row per unique string. Nothing here talks to a network or
knows which game it is.

Engine control codes are separated from the words:

  * dynamic references (`\\V[3]`, `\\N[1]`, `\\P[1]`, `\\G`) become 〔0〕, 〔1〕
    in the text handed to a translator, and are restored afterwards. They
    carry a value only at runtime, so translating them is meaningless and
    dropping them loses the number.
  * styling codes (`\\C[17]`, `\\c[0]`, `\\{`, `\\}`, `\\!`, `\\.`, `\\|`,
    `\\>`, `\\<`, `\\^`, `\\FS[28]`) are recorded per string but not sent:
    no translation API preserves them, and a misplaced `\\C[17]` is worse
    than plain text.

Usage:
    python scripts/extract_mz.py <game>/data -o corpus.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gt.adapters.rpgmaker_mv_mz import restore_codes, split_codes  # noqa: E402
from gt.text import is_translatable, normalize  # noqa: E402




class Corpus:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.seen_texts: set[str] = set()

    def add(self, category: str, raw: str, where: str = "") -> None:
        if not isinstance(raw, str) or not raw.strip():
            return
        plain, refs, styles = split_codes(raw)
        if not is_translatable(normalize(plain)):
            return
        dedup = normalize(plain)
        if dedup in self.seen_texts:
            return
        self.seen_texts.add(dedup)
        key = hashlib.sha1(dedup.encode("utf-8")).hexdigest()[:16]
        self.rows[key] = {
            "id": key,
            "category": category,
            "where": where,
            "raw": raw,
            "text": plain,
            "refs": refs,
            "styles": styles,
        }


def scan_commands(commands, corpus: Corpus, where: str) -> None:
    for cmd in commands or []:
        if not isinstance(cmd, dict):
            continue
        code = cmd.get("code")
        params = cmd.get("parameters") or []
        if code == 401 and params:
            corpus.add("对话", params[0], where)
        elif code == 405 and params:
            corpus.add("滚动文本", params[0], where)
        elif code == 102 and params and params[0]:
            for choice in params[0]:
                corpus.add("选项", choice, where)
        elif code == 320 and len(params) > 1 and params[1]:
            corpus.add("角色名", params[1], where)


DB_FIELDS = {
    "Items.json": ("name", "description", "物品"),
    "Weapons.json": ("name", "description", "武器"),
    "Armors.json": ("name", "description", "防具"),
    "Skills.json": ("name", "description", "技能", "message1", "message2"),
    "States.json": ("name", "message1", "message2", "message3", "message4", "状态"),
    "Actors.json": ("name", "nickname", "profile", "角色"),
    "Classes.json": ("name", "职业"),
    "Enemies.json": ("name", "敌人"),
    "MapInfos.json": ("name", "地图"),
    "CommonEvents.json": ("name", "公共事件名"),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("data", type=Path, help="the game's data directory")
    parser.add_argument("-o", "--out", type=Path, default=Path("corpus.jsonl"))
    args = parser.parse_args()

    corpus = Corpus()
    files = sorted(args.data.glob("*.json"))
    for path in files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(doc, list):
            continue
        stem = path.name
        for item in doc:
            if not isinstance(item, dict):
                continue
            for page in item.get("pages") or []:
                if isinstance(page, dict):
                    scan_commands(page.get("list"), corpus, stem)
            scan_commands(item.get("list"), corpus, stem)
            if stem in DB_FIELDS:
                fields = DB_FIELDS[stem]
                label = fields[-1]
                for field in fields[:-1]:
                    corpus.add(label, item.get(field), f"{stem}:{field}")
        if stem == "System.json" and isinstance(doc, dict):
            for key, value in (doc.get("terms") or {}).items():
                if isinstance(value, list):
                    for entry in value:
                        corpus.add("系统词条", entry, f"System.json:{key}")
                elif isinstance(value, str):
                    corpus.add("系统词条", value, f"System.json:{key}")

    rows = list(corpus.rows.values())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    by_cat: dict[str, int] = {}
    chars = 0
    for row in rows:
        by_cat[row["category"]] = by_cat.get(row["category"], 0) + 1
        chars += len(row["text"])
    for cat, count in sorted(by_cat.items(), key=lambda kv: -kv[1]):
        print(f"{cat:8s} {count:6d}")
    print(f"unique strings: {len(rows)}, characters: {chars}")
    print(f"written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
