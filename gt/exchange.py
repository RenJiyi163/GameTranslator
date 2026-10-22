"""Offline round trip: unmatched strings out as a DOCX, translations back in.

The live runtime only translates what it can reach. The rest of a game gets
translated offline: export the strings nothing could resolve, hand the
document to a translation service, and import what comes back into the
translation memory.

Three things have to be repaired on the way back:

  * sentence-internal newlines were written as ` ↵ ` so the service would not
    treat them as paragraph breaks; they become newlines again.
  * `〔0〕〔1〕` are runtime references (`\\V[3]`, `\\N[1]`). They carry a value
    only while the game runs, so they travel as markers and are restored
    positionally, in whatever order the translation happened to put them.
  * styling codes (`\\C[17]`, `\\PX[30]`) were dropped before the document was
    sent; the memory is keyed on the line without them, and the runtime's
    lookup strips the same codes before its second attempt.

Nothing here knows which game it is: the caller supplies rows that an adapter
has already prepared.
"""

from __future__ import annotations

import difflib
import json
import re
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .tm import merge as merge_tm

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

PLACEHOLDER = re.compile(r"〔(\d+)〕")
NEWLINE_MARK = " ↵ "
# The service does not reliably keep the spaces around the marker: it may come
# back as `。↵ 效果` with the leading space eaten. Only ASCII spaces adjacent
# to it are dropped, because the ideographic ones are the original indent.
NEWLINE_MARK_RE = re.compile(r"[ \t]*↵[ \t]*")
# What survives translation even when the words change: digits, latin runs and
# the reference markers. Used only to re-align a re-ordered document.
SIGNATURE = re.compile(r"〔\d+〕|[A-Za-z]+|\d+")


def read_docx(path: Path) -> list[str]:
    """Paragraph texts in document order, with manual breaks kept as breaks."""
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    paragraphs: list[str] = []
    for para in root.iter(W + "p"):
        pieces: list[str] = []
        for node in para.iter():
            if node.tag == W + "t":
                pieces.append(node.text or "")
            elif node.tag in (W + "br", W + "cr"):
                pieces.append(NEWLINE_MARK)
            elif node.tag == W + "tab":
                pieces.append(" ")
        paragraphs.append("".join(pieces))
    return paragraphs


CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

PACKAGE_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

DOCUMENT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="宋体"/>
<w:sz w:val="21"/>
</w:rPr></w:rPrDefault></w:docDefaults>
</w:styles>"""

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def write_docx(paragraphs: list[str], path: Path) -> None:
    """One paragraph per string, with newlines marked rather than kept.

    The document is assembled from the OOXML parts directly. The document is
    only ever read back by our own import and typed into by a translation
    service, so nothing here needs the features a full word processor library
    brings, which on Windows cost about a third of the download.
    """
    ElementTree.register_namespace("w", W_NS)
    body = ElementTree.Element(f"{{{W_NS}}}body")
    for text in paragraphs:
        paragraph = ElementTree.SubElement(body, f"{{{W_NS}}}p")
        run = ElementTree.SubElement(paragraph, f"{{{W_NS}}}r")
        node = ElementTree.SubElement(run, f"{{{W_NS}}}t")
        node.set(XML_SPACE, "preserve")
        node.text = text.replace("\n", NEWLINE_MARK)
    ElementTree.SubElement(body, f"{{{W_NS}}}sectPr")
    document = ElementTree.Element(f"{{{W_NS}}}document")
    document.append(body)
    xml = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + \
        ElementTree.tostring(document, encoding="utf-8", xml_declaration=False)

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", PACKAGE_RELS)
        archive.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS)
        archive.writestr("word/styles.xml", STYLES)
        archive.writestr("word/document.xml", xml)


def restore(text: str, refs: list[str]) -> str:
    for index in range(len(refs) - 1, -1, -1):
        text = text.replace("〔%d〕" % index, refs[index])
    return text


def _ref_shape(text: str) -> tuple[int, ...]:
    return tuple(sorted(int(m.group(1)) for m in PLACEHOLDER.finditer(text)))


def offset_pairs(rows: list[dict], paragraphs: list[str]) -> list[tuple[int, int]] | None:
    """Pair by a constant shift, if every reference marker lands where it should.

    A header or a footer added by the service moves every paragraph by the
    same amount, and the `〔n〕` markers that survive translation are an exact
    witness for whether a shift is right. Returns None when no shift makes
    all of them agree, which is what a paragraph inserted mid-document looks
    like.
    """
    # row["text"] still carries the 〔n〕 markers; restoring them first would
    # turn them into \V[3] and leave nothing to compare.
    wanted = [(i, _ref_shape(row["text"])) for i, row in enumerate(rows)]
    wanted = [(i, shape) for i, shape in wanted if shape]
    if not wanted:
        return None
    span = min(abs(len(paragraphs) - len(rows)) + 2, 50)
    for shift in [0] + [d for k in range(1, span + 1) for d in (k, -k)]:
        pairs: list[tuple[int, int]] = []
        ok = True
        for i in range(len(rows)):
            j = i + shift
            if 0 <= j < len(paragraphs):
                pairs.append((i, j))
        for i, shape in wanted:
            j = i + shift
            if not (0 <= j < len(paragraphs)) or _ref_shape(paragraphs[j]) != shape:
                ok = False
                break
        if ok and len(pairs) == min(len(rows), len(paragraphs)):
            return pairs
    return None


def pairing(rows: list[dict], paragraphs: list[str]) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Return (index pairs, unpaired source rows, unpaired returned paragraphs)."""
    if len(rows) == len(paragraphs):
        return [(i, i) for i in range(len(rows))], [], []

    shifted = offset_pairs(rows, paragraphs)
    if shifted is not None:
        paired_rows = {a for a, _ in shifted}
        paired_paras = {b for _, b in shifted}
        return (
            shifted,
            [i for i in range(len(rows)) if i not in paired_rows],
            [j for j in range(len(paragraphs)) if j not in paired_paras],
        )

    # Re-alignment runs on anchors only. Most lines are plain prose and carry
    # no digit, latin word or reference marker, so hundreds of paragraphs
    # would share the empty signature and any matcher would happily align
    # them in the wrong order. A line that does carry one of those is a much
    # better signpost; the prose between two signposts is then paired by
    # position, which is only safe when the gap lengths agree.
    source_sig = [tuple(SIGNATURE.findall(restore(row["text"], row["refs"]))) for row in rows]
    para_sig = [tuple(SIGNATURE.findall(text)) for text in paragraphs]
    anchors_a = [(i, sig) for i, sig in enumerate(source_sig) if sig]
    anchors_b = [(j, sig) for j, sig in enumerate(para_sig) if sig]

    pairs: list[tuple[int, int]] = []

    def fill(lo_a: int, hi_a: int, lo_b: int, hi_b: int) -> None:
        if hi_a - lo_a == hi_b - lo_b:
            pairs.extend(zip(range(lo_a, hi_a), range(lo_b, hi_b)))

    matcher = difflib.SequenceMatcher(
        a=[sig for _, sig in anchors_a], b=[sig for _, sig in anchors_b], autojunk=False
    )
    cursor_a = cursor_b = 0
    for block in matcher.get_matching_blocks():
        if block.size:
            a0, b0 = anchors_a[block.a][0], anchors_b[block.b][0]
            a1, b1 = anchors_a[block.a + block.size - 1][0], anchors_b[block.b + block.size - 1][0]
            fill(cursor_a, a0, cursor_b, b0)
            pairs.extend((a0 + k, b0 + k) for k in range(a1 - a0 + 1))
            cursor_a, cursor_b = a1 + 1, b1 + 1
        else:
            # The trailing sentinel: whatever is left is unpaired unless the
            # two tails happen to be the same length.
            fill(cursor_a, len(rows), cursor_b, len(paragraphs))

    paired_rows = {a for a, _ in pairs}
    paired_paras = {b for _, b in pairs}
    return (
        sorted(pairs),
        [i for i in range(len(rows)) if i not in paired_rows],
        [j for j in range(len(paragraphs)) if j not in paired_paras],
    )


def repair_brackets(translation: str, refs: list[str]) -> tuple[str, list[int]]:
    """Turn `[0]` back into `〔0〕` where the service narrowed the brackets.

    A full-width marker is unusual enough that some services rewrite it; the
    half-width form then looks like ordinary markup to the game and the value
    is lost. Only converted when the matching full-width marker is missing, so
    a genuine `[1]` in the prose is left alone.
    """
    fixed: list[int] = []
    for index in range(len(refs)):
        if "〔%d〕" % index in translation:
            continue
        half = "[%d]" % index
        if half in translation:
            translation = translation.replace(half, "〔%d〕" % index, 1)
            fixed.append(index)
    return translation, fixed


def repair_digits(translation: str, refs: list[str]) -> tuple[str, list[int]]:
    """Put back a marker the service turned into a bare number.

    `妊娠〔0〕ヶ月` comes back as `怀孕0个月`: the marker was read as the
    digit zero. Restored only when that digit appears exactly once on its own,
    with no digit or letter touching it, so `现役J0` (which is `Ｊ〇`, not a
    marker) and `10000` are both left alone. Applied only to rows that would
    otherwise be dropped, and every one of them is reported.
    """
    fixed: list[int] = []
    for index in range(len(refs)):
        if "〔%d〕" % index in translation:
            continue
        hits = list(re.finditer(r"(?<![\dA-Za-z])%d(?![\dA-Za-z])" % index, translation))
        if len(hits) != 1:
            continue
        hit = hits[0]
        translation = translation[: hit.start()] + "〔%d〕" % index + translation[hit.end():]
        fixed.append(index)
    return translation, fixed


def repair_newlines(source: str, translation: str) -> str:
    """Drop line breaks the service added to a line that had none.

    These paragraphs are status screens: several labelled values written one
    after another, laid out by the ``\\PX``/``\\PY`` codes rather than by
    newlines. A service that sees a long line splits it, and the break lands
    in the middle of what the game draws as one row. The source's own spacing
    survives in the translation, so removing the break and the spaces around
    it puts the line back together.
    """
    if "\n" in source or "\n" not in translation:
        return translation
    return re.sub(r"[ \t　]*\n[ \t　]*", "", translation)


def check(source: str, translation: str, refs: list[str]) -> str:
    """Return a problem label, or an empty string when the row is safe."""
    if not translation.strip():
        return "empty"
    missing = [n for n in range(len(refs)) if "〔%d〕" % n not in translation]
    if missing:
        return "missing-ref " + ",".join(str(n) for n in missing)
    unknown = [m.group(1) for m in PLACEHOLDER.finditer(translation) if int(m.group(1)) >= len(refs)]
    if unknown:
        return "unknown-ref " + ",".join(sorted(set(unknown)))
    if source.count("\n") != translation.count("\n"):
        return "newline-count"
    return ""


# -- the two ends of the round trip -------------------------------------------

def export_unmatched(rows_path: Path, out_dir: Path) -> dict:
    """Write the unmatched rows as a DOCX plus the mapping that undoes it."""
    rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    usable = [row for row in rows if (row.get("text") or "").strip()]
    stamp = time.strftime("%Y%m%d_%H%M")
    base = out_dir / f"unmatched_{stamp}"
    mapping = base.with_name(base.name + ".jsonl")
    docx = base.with_name(base.name + ".docx")
    text_file = base.with_name(base.name + ".txt")

    out_dir.mkdir(parents=True, exist_ok=True)
    with mapping.open("w", encoding="utf-8") as fh:
        for row in usable:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    write_docx([row["text"] for row in usable], docx)
    text_file.write_text("\n".join(row["text"].replace("\n", NEWLINE_MARK) for row in usable),
                         encoding="utf-8")
    return {"count": len(usable), "mapping": mapping, "docx": docx, "txt": text_file}


def import_translated(mapping: Path, docx_path: Path, db_path: Path | None = None,
                      out_path: Path | None = None) -> dict:
    """Pair a returned DOCX with its mapping, repair it, and optionally merge.

    `db_path` is optional because the command line sometimes only wants the
    repaired JSONL; the GUI always passes the profile's memory.
    """
    rows = [json.loads(line) for line in mapping.read_text(encoding="utf-8").splitlines() if line.strip()]
    paragraphs = read_docx(docx_path)

    pairs, lost_rows, lost_paras = pairing(rows, paragraphs)
    out: list[dict] = []
    problems: list[dict] = []
    repaired: list[dict] = []

    for src_index, out_index in pairs:
        row = rows[src_index]
        translation = NEWLINE_MARK_RE.sub("\n", paragraphs[out_index]).strip()
        translation, brackets = repair_brackets(translation, row.get("refs", []))
        before = translation
        translation = repair_newlines(row["text"], translation)
        source = restore(row["text"], row.get("refs", []))
        issue = check(source, translation, row.get("refs", []))
        digits: list[int] = []
        if issue.startswith("missing-ref"):
            # Only worth the risk when the row would be dropped otherwise.
            attempt, digits = repair_digits(translation, row.get("refs", []))
            if digits and not check(source, attempt, row.get("refs", [])):
                translation, issue = attempt, ""
        if brackets or digits or translation != before:
            repaired.append({
                "paragraph": out_index + 1,
                "id": row.get("id"),
                "brackets": brackets,
                "digits": digits,
                "newlines_removed": before.count("\n") - translation.count("\n"),
            })
        if issue:
            problems.append({
                "paragraph": out_index + 1,
                "id": row.get("id"),
                "issue": issue,
                "source": row["text"],
                "translation": translation,
            })
            continue
        out.append({
            "original": source,
            "translation": restore(translation, row.get("refs", [])),
            "category": row.get("category") or row.get("kind") or "",
            "source": "docx",
        })

    translated_path = out_path or (mapping.parent / "unmatched_translated.jsonl")
    translated_path.parent.mkdir(parents=True, exist_ok=True)
    with translated_path.open("w", encoding="utf-8") as fh:
        for item in out:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")

    merged = merge_tm(translated_path, db_path) if (db_path and out) else None
    report = {
        "map_rows": len(rows),
        "docx_paragraphs": len(paragraphs),
        "matched": len(pairs),
        "written": len(out),
        "repaired": repaired,
        "problems": problems,
        "unpaired_source": [{"id": rows[i].get("id"), "text": rows[i]["text"]} for i in lost_rows],
        "unpaired_paragraphs": [{"paragraph": j + 1, "text": paragraphs[j]} for j in lost_paras],
        "tm": merged,
        "translated": str(translated_path),
    }
    report_path = translated_path.with_suffix(".report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    report["report"] = str(report_path)
    return report
