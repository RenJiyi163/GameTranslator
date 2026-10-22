#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a runtime translation memory from a community translation JSON.

Input rows are keyed by source-file offsets, which do not survive game updates,
so this script keeps only the text pairs and drops the offsets.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
HEAD_RE = re.compile(r"\[\s*([A-Za-z_][A-Za-z0-9_.]*)")


def count(ch: str, text: str) -> int:
    return text.count(ch)


def structural_ok(original: str, translation: str) -> bool:
    for ch in "[]|":
        if count(ch, original) != count(ch, translation):
            return False
    if TAG_RE.findall(original) != TAG_RE.findall(translation):
        return False
    if Counter(HEAD_RE.findall(original)) != Counter(HEAD_RE.findall(translation)):
        return False
    return True


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("out", type=Path)
    args = ap.parse_args()

    files = sorted(args.src.glob("*.json"))
    rows_seen = 0
    pairs = 0
    skipped_empty = 0
    skipped_struct = 0
    skipped_code = 0
    unique: dict[str, dict] = {}
    conflicts = 0

    for path in files:
        doc = json.loads(path.read_text(encoding="utf-8"))
        for row in doc.get("rows", []):
            rows_seen += 1
            original = row.get("original") or ""
            translation = row.get("translation") or ""
            if not translation.strip():
                skipped_empty += 1
                continue
            if row.get("translationStatus") == "failed" or row.get("translationError"):
                skipped_struct += 1
                continue
            if row.get("category") == "unknown" and not original.strip():
                skipped_code += 1
                continue
            if not structural_ok(original, translation):
                skipped_struct += 1
                continue
            key = normalize(original)
            if not key:
                skipped_empty += 1
                continue
            pairs += 1
            item = {
                "original": original,
                "translation": translation,
                "category": row.get("category", ""),
                "source": path.name,
                "id": row.get("id", ""),
            }
            prev = unique.get(key)
            if prev is None:
                unique[key] = item
            elif prev["translation"] != translation:
                conflicts += 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for item in unique.values():
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")

    chars_src = sum(len(v["original"]) for v in unique.values())
    chars_dst = sum(len(v["translation"]) for v in unique.values())
    print(json.dumps({
        "files": len(files),
        "rows_seen": rows_seen,
        "pairs": pairs,
        "unique": len(unique),
        "skipped_empty": skipped_empty,
        "skipped_struct": skipped_struct,
        "skipped_code": skipped_code,
        "conflicting_duplicates": conflicts,
        "unique_source_chars": chars_src,
        "unique_translation_chars": chars_dst,
        "out": str(args.out),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
