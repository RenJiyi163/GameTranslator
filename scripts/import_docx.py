#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Turn a translated DOCX back into a JSONL translation memory.

The round trip is: `data/<batch>.jsonl` holds one row per line the game draws,
in the same order as the paragraphs of the DOCX that was handed to a document
translation service. The service returns a DOCX whose paragraphs are the
translations, and this script puts the two back together.

The pairing and repair rules live in `gt/exchange.py`, because the window
does the same thing with a file picker; this file is only the command line.

    python scripts/import_docx.py data/corpus.jsonl back.docx \\
        -o data/translated.jsonl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gt.exchange import import_translated  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("map", type=Path, help="the JSONL that was turned into the DOCX")
    parser.add_argument("docx", type=Path, help="the DOCX the service returned")
    parser.add_argument("-o", "--out", type=Path, default=Path("translated.jsonl"))
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    report = import_translated(args.map, args.docx, out_path=args.out)
    if args.report and Path(report["report"]) != args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(Path(report["report"]).read_text(encoding="utf-8"), encoding="utf-8")
        report["report"] = str(args.report)

    print(f"map rows {report['map_rows']}, docx paragraphs {report['docx_paragraphs']}, "
          f"paired {report['matched']}")
    print(f"written {report['written']} -> {args.out}")
    if report["repaired"]:
        print(f"repaired in place: {len(report['repaired'])} (brackets and/or added line breaks)")
    if report["problems"]:
        print(f"rows with problems: {len(report['problems'])} (see {report['report']})")
    if report["unpaired_source"] or report["unpaired_paragraphs"]:
        print(f"unpaired: {len(report['unpaired_source'])} source rows, "
              f"{len(report['unpaired_paragraphs'])} paragraphs (see {report['report']})")
    return 0 if report["written"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
