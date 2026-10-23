"""Offline text extraction for Emuera (the engine behind most era games).

Emuera has no DOM and no debug port: it paints characters onto a GDI canvas.
What it does have is its script, and every string the player reads is a literal
in an ERB file or a column in a CSV file. So the translation path for this
engine runs over the files, not over a running window.

    extract: ERB/CSV in, JSONL out (one row per translatable span)
    apply:   JSONL of translations in, ERB rewritten in place

The rewrite is byte-for-byte except for the spans that were translated, and it
keeps each file's encoding and line endings. Anything the parser does not
recognise is left alone: a half-rewritten script is worse than an untranslated
one.
"""

from __future__ import annotations

import codecs
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Commands whose remainder is displayed text. PRINTDATA opens a block of
#: candidate lines instead, handled separately.
PRINT_COMMANDS = (
    "PRINTFORMW", "PRINTFORML", "PRINTFORM",
    "PRINTW", "PRINTL", "PRINT",
)

_BUTTON = re.compile(r'^\s*(PRINTBUTTON[LW]?)\s+(.*)$', re.IGNORECASE)
_PRINT = re.compile(r'^\s*(%s)\b[ \t]?(.*)$' % "|".join(PRINT_COMMANDS), re.IGNORECASE)
_PRINTDATA = re.compile(r'^\s*PRINTDATA[LW]?\b', re.IGNORECASE)
_ENDDATA = re.compile(r'^\s*ENDDATA\b', re.IGNORECASE)
_CODE_LINE = re.compile(r'^\s*[;#@$\[\]{}]')
#: Inside PRINTDATA, each line is either bare text or wrapped in a data
#: command. The command is syntax; only what follows it is for the player.
_DATA_WRAPPER = re.compile(r'^\s*(DATAFORM|DATALIST|DATALINE)\b[ \t]?', re.IGNORECASE)

#: Everything the game substitutes at run time: %expr% (Emuera's own
#: interpolation), {name} (the mod framework's templates), backslash escapes
#: such as \n, and [[...]] markup. A translator must never see these as text.
_PLACEHOLDER = re.compile(r'%[^%]*%|\{[^{}]*\}|\[\[.*?\]\]|\\[a-zA-Z\\]')
_MASK_OPEN = "⟦"
_MASK_CLOSE = "⟧"


def protect(text: str) -> tuple[str, list[str]]:
    """Replace placeholders with inert markers; return (masked, originals)."""
    tokens: list[str] = []

    def swap(m: re.Match) -> str:
        tokens.append(m.group(0))
        return f"{_MASK_OPEN}{len(tokens) - 1}{_MASK_CLOSE}"

    return _PLACEHOLDER.sub(swap, text), tokens


def restore(text: str, tokens: list[str]) -> str:
    """Put the original placeholders back into a translated string."""
    def swap(m: re.Match) -> str:
        idx = int(m.group(1))
        return tokens[idx] if idx < len(tokens) else m.group(0)

    return re.sub(re.escape(_MASK_OPEN) + r"(\d+)" + re.escape(_MASK_CLOSE), swap, text)


def unprotected_problems(source: str, target: str) -> list[str]:
    """Why a translation would break the script, if it would."""
    src, tokens = protect(source)
    problems = []
    back = protect(target)[1]
    if len(back) != len(tokens):
        problems.append(f"placeholder count {len(back)} != {len(tokens)}")
    return problems

#: Anything with kana is text the player reads in Japanese. Lines without it
#: are either already translated (Chinese uses no kana) or not language.
#: The katakana block also holds ・ and ー, and a translated line keeps those
#: in names and bullets; counting them would send finished text back to the
#: model.
_KANA = re.compile(r'[ぁ-ゖァ-ヺヽ-ヿㇰ-ㇿ]')
_CJK = re.compile(r'[㐀-鿿]')
#: Simplified-only characters. Japanese uses none of them, so one hit means
#: the string was already translated, even inside an otherwise Japanese file.
_SIMPLIFIED = set("们这那个说时会里对还没现实过门么样点电后关开语话请谢让见觉声头张书车东马鸟龙汉语译别给为她您")


@dataclass
class Span:
    """One translatable piece of one line."""

    path: Path
    line_no: int          # 1-based, counted over the file as it sits on disk
    start: int            # column offsets into the raw line text
    end: int
    text: str             # the source text, exactly as it appears
    kind: str = "text"    # text | button | data

    def key(self) -> str:
        return self.text


@dataclass
class Line:
    text: str                       # the line without its terminator
    terminator: str                 # "\r\n", "\n" or ""
    spans: list[Span] = field(default_factory=list)


@dataclass
class FileText:
    path: Path
    encoding: str
    bom: bool
    lines: list[Line]

    def to_bytes(self) -> bytes:
        out = "".join(line.text + line.terminator for line in self.lines)
        return (codecs.BOM_UTF8 if self.bom else b"") + out.encode(self.encoding)


def read_script(path: Path) -> FileText:
    """Decode an ERB file, remembering enough to write it back unchanged."""
    raw = path.read_bytes()
    bom = raw.startswith(codecs.BOM_UTF8)
    text = None
    for encoding in ("utf-8-sig", "cp932", "utf-8"):
        try:
            text = raw.decode(encoding)
            used = "utf-8" if encoding == "utf-8-sig" else encoding
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        used = "cp932"
        text = raw.decode("cp932", errors="replace")
    lines: list[Line] = []
    for chunk in text.splitlines(keepends=True):
        body = chunk.rstrip("\r\n")
        lines.append(Line(body, chunk[len(body):]))
    return FileText(path, used, bom, lines)


def _quoted(remainder: str, base: int) -> list[tuple[int, int, str, str]]:
    """String literals in a PRINTBUTTON argument list.

    Returns (start, end, text, quote_style) with quote_style one of
    '"' / '@"' so the writer puts back the same prefix.
    """
    out = []
    i = 0
    while i < len(remainder):
        c = remainder[i]
        if c == '"':
            j = remainder.find('"', i + 1)
            if j < 0:
                break
            out.append((base + i + 1, base + j, remainder[i + 1:j], '"'))
            i = j + 1
            continue
        if c == "@" and i + 1 < len(remainder) and remainder[i + 1] == '"':
            j = remainder.find('"', i + 2)
            if j < 0:
                break
            out.append((base + i + 2, base + j, remainder[i + 2:j], '@"'))
            i = j + 1
            continue
        i += 1
    return out


def _trim(text: str, start: int, end: int) -> tuple[int, int, str] | None:
    """Narrow a chunk to the text itself, leaving indentation out of the span.

    Emuera scripts indent with tabs and with the ideographic space; both read
    as layout, not content, and a translator should never see them.
    """
    lead = 0
    while lead < end - start and text[lead].isspace():
        lead += 1
    trail = end - start
    while trail > lead and text[trail - 1].isspace():
        trail -= 1
    if trail <= lead:
        return None
    return start + lead, start + trail, text[lead:trail]


def wants_translation(text: str, kana_file: bool = True) -> bool:
    """Is this chunk language the player reads in Japanese?

    Kana settles it. Kanji-only lines are the ambiguous case: 一行目 is
    Japanese, 今天天气很好 is already translated Chinese. The file decides:
    a script whose own text is full of kana is untranslated, so its kanji
    lines belong to the same language. A script that is already Chinese has
    no kana at all, and its kanji lines are left alone.
    """
    kana = bool(_KANA.search(text))
    if not kana and not (kana_file and _CJK.search(text)):
        return False
    if not kana and any(ch in _SIMPLIFIED for ch in text):
        return False
    stripped = protect(text)[0]
    letters = re.sub(r'[\W_]+', '', stripped, flags=re.UNICODE)
    if _KANA.search(stripped) or _CJK.search(stripped):
        return len(letters) >= 1
    return len(letters) >= 2


def _kana_file(lines: list[Line]) -> bool:
    """Whether this script reads as Japanese: kana make up a real share of it."""
    kana = cjk = 0
    for line in lines:
        kana += len(_KANA.findall(line.text))
        cjk += len(_CJK.findall(line.text))
    return kana >= 5 and kana / max(1, kana + cjk) >= 0.05


def scan_file(path: Path) -> list[Span]:
    script = read_script(path)
    kana_file = _kana_file(script.lines)
    spans: list[Span] = []
    in_data = False
    for idx, line in enumerate(script.lines, start=1):
        raw = line.text
        if _PRINTDATA.match(raw):
            in_data = True
            continue
        if in_data:
            if _ENDDATA.match(raw):
                in_data = False
                continue
            if _CODE_LINE.match(raw) or not raw.strip():
                continue
            stripped = raw.rstrip()
            wrapper = _DATA_WRAPPER.match(stripped)
            body_start = wrapper.end() if wrapper else len(stripped) - len(stripped.lstrip())
            segment = raw[body_start:len(stripped)]
            trimmed = _trim(segment, body_start, len(stripped))
            if trimmed and wants_translation(trimmed[2], kana_file):
                spans.append(Span(path, idx, *trimmed, "data"))
            continue

        m = _PRINT.match(raw)
        if m:
            # The whole remainder is one span, placeholders included: a
            # sentence cut at every %expr% turns into fragments like 「の」,
            # and a fragment has no translation that survives being glued back.
            trimmed = _trim(m.group(2), m.start(2), len(raw.rstrip()))
            if trimmed and wants_translation(trimmed[2], kana_file):
                spans.append(Span(path, idx, *trimmed, "text"))
            continue

        b = _BUTTON.match(raw)
        if b:
            for start, end, chunk, _style in _quoted(b.group(2), b.start(2)):
                trimmed = _trim(chunk, start, end)
                if trimmed and wants_translation(trimmed[2], kana_file):
                    spans.append(Span(path, idx, *trimmed, "button"))
    return spans


def iter_files(game_dir: Path) -> list[Path]:
    files = [p for p in sorted(game_dir.rglob("*")) if p.is_file()]
    return [p for p in files if p.suffix.lower() in (".erb", ".erh")]


def iter_csv_files(game_dir: Path) -> list[Path]:
    files = [p for p in sorted(game_dir.rglob("*")) if p.is_file()]
    return [p for p in files if p.suffix.lower() == ".csv"]


#: CSV cells that hold display text. The key column stays Japanese: Emuera
#: looks definitions up by position and by these keys, so rewriting a key
#: would turn a name into a missing variable. Values are pure display, except
#: 種族 / 所属 / 初期タグ / CLOTH:* in character sheets, which scripts read back
#: as data; those are left alone.
_GAMEBASE_KEYS = {"ウィンドウタイトル", "タイトル", "作者", "追加情報"}
_CHARA_KEYS = {"名前", "呼び名"}
_CHARA_CSTR_KEYS = {"名字", "名前", "普通ネーム", "識別名"} | {
    f"ＳＬＧ解説_{n}" for n in range(10)
}
_NUMERIC = re.compile(r"[\s0-9０-９.,/+\-]*")


def _split_cells(text: str) -> list[tuple[int, int]]:
    """Column offsets of one CSV line. Emuera's CSV has no quoting."""
    cells: list[tuple[int, int]] = []
    start = 0
    for i, ch in enumerate(text):
        if ch == ",":
            cells.append((start, i))
            start = i + 1
    cells.append((start, len(text)))
    return cells


def _cell(raw: str, cells: list[tuple[int, int]], index: int) -> str:
    start, end = cells[index]
    return raw[start:end].strip()


def _is_chara_csv(path: Path) -> bool:
    return any(part.lower() == "chara" for part in path.parts) or path.stem.lower().startswith("chara")


def _csv_target(raw: str, cells: list[tuple[int, int]], chara: bool, gamebase: bool) -> list[int]:
    """Which cells of this line are display text."""
    if chara:
        key = _cell(raw, cells, 0)
        if key in _CHARA_KEYS and len(cells) > 1:
            return [1]
        if key == "CSTR" and len(cells) > 2 and _cell(raw, cells, 1) in _CHARA_CSTR_KEYS:
            return [2]
        return []
    if gamebase:
        if _cell(raw, cells, 0) in _GAMEBASE_KEYS and len(cells) > 1:
            return [1]
        return []
    return [1] if len(cells) > 1 else []


def scan_csv(path: Path) -> list[Span]:
    script = read_script(path)
    kana_file = _kana_file(script.lines)
    chara = _is_chara_csv(path)
    gamebase = path.name.lower() == "gamebase.csv"
    spans: list[Span] = []
    for idx, line in enumerate(script.lines, start=1):
        raw = line.text
        if not raw.strip() or raw.lstrip().startswith(";"):
            continue
        cells = _split_cells(raw)
        for pos in _csv_target(raw, cells, chara, gamebase):
            start, end = cells[pos]
            trimmed = _trim(raw[start:end], start, end)
            if not trimmed or _NUMERIC.fullmatch(trimmed[2]):
                continue
            if wants_translation(trimmed[2], kana_file):
                spans.append(Span(path, idx, *trimmed, "csv"))
    return spans


def scan(path: Path) -> list[Span]:
    return scan_csv(path) if path.suffix.lower() == ".csv" else scan_file(path)


def extract(game_dir: Path, skip_dirs: tuple[str, ...] = ()) -> list[Span]:
    spans: list[Span] = []
    for path in iter_files(Path(game_dir)) + iter_csv_files(Path(game_dir)):
        if any(part in skip_dirs for part in path.parts):
            continue
        try:
            spans.extend(scan(path))
        except OSError:
            continue
    return spans


def apply_translations(game_dir: Path, mapping: dict[str, str], dry_run: bool = False) -> dict:
    """Rewrite every ERB under `game_dir` whose spans are in `mapping`.

    Returns counters, and the list of files touched. Files whose spans are all
    untranslated are not rewritten at all, so mtimes stay meaningful.
    """
    stats = {"files": 0, "spans": 0, "missing": 0, "converted": 0, "files_touched": []}
    for path in iter_files(Path(game_dir)) + iter_csv_files(Path(game_dir)):
        by_line: dict[int, list[Span]] = {}
        for span in scan(path):
            by_line.setdefault(span.line_no, []).append(span)
        if not by_line:
            continue
        script = read_script(path)
        changed = False
        for line_no, spans in by_line.items():
            line = script.lines[line_no - 1]
            new_text = line.text
            for span in sorted(spans, key=lambda s: s.start, reverse=True):
                target = mapping.get(span.text)
                if not target:
                    stats["missing"] += 1
                    continue
                new_text = new_text[:span.start] + target + new_text[span.end:]
                stats["spans"] += 1
                changed = True
            line.text = new_text
        if changed:
            stats["files"] += 1
            stats["files_touched"].append(str(path))
            if not dry_run:
                try:
                    data = script.to_bytes()
                except UnicodeEncodeError:
                    # The file was cp932 and the translation is Chinese: the
                    # old encoding cannot hold it, so the file becomes UTF-8.
                    script.encoding, script.bom = "utf-8", True
                    data = script.to_bytes()
                    stats["converted"] += 1
                path.write_bytes(data)
    return stats


def write_jsonl(spans: list[Span], out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    seen: dict[str, int] = {}
    rows = []
    for span in spans:
        if span.text in seen:
            seen[span.text] += 1
            continue
        seen[span.text] = 1
        rows.append({"text": span.text, "kind": span.kind, "file": str(span.path), "line": span.line_no})
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


SYSTEM_PROMPT = (
    "你是日文到简体中文的游戏文本译者。用户给的是 JSON 数组，每行一条。逐条翻译成简体中文。"
    "文本里的 数字 是占位符，必须原样保留、数量不变，位置可以随语序调整。"
    "只输出 JSON 数组，长度与输入相同，不要解释。"
)


def _chat(base: str, key: str, model: str, batch: list[str], timeout: float = 180) -> list[str]:
    import urllib.request

    body = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": 8192,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(batch, ensure_ascii=False)},
        ],
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = base.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content = json.loads(resp.read())["choices"][0]["message"]["content"]
    start, end = content.find("["), content.rfind("]")
    if start < 0 or end < start:
        return []
    try:
        parsed = json.loads(content[start:end + 1])
    except json.JSONDecodeError:
        return []
    out = []
    for x in parsed:
        if x is None:
            out.append("")
        elif isinstance(x, list):
            out.append(str(x[0]) if x else "")
        else:
            text = str(x)
            # Some runtimes answer with a one-element list rendered as text.
            if len(text) > 3 and text[0] == "[" and text[-1] == "]" and text.count("'") == 2:
                text = text[2:-2]
            elif len(text) > 3 and text[0] == "[" and text[-1] == "]" and text.count('"') == 2:
                text = text[2:-2]
            out.append(text)
    return out


#: Placeholders whose value depends on how often, or in what order, they run.
#: Chinese reorders clauses, so a model will move markers around; that is safe
#: for a name lookup and not safe for a dice roll or a read of RESULT, which
#: the expression before it writes.
_ORDER_SENSITIVE = re.compile(r"(?i)\b(RAND|RANDOM|RESULT|GETCHARA|GETSPCHARA)\b")


def _accept(source: str, answer: str) -> str | None:
    """A translation is usable only if every placeholder came back.

    The answer is checked for our own markers, not for the game's placeholder
    syntax: a model that invents an extra %...% or drops a {name} shows up as
    a marker count mismatch once the markers are expanded and re-protected.
    Markers may come back in another order; that is rejected only when one of
    them is order-sensitive.
    """
    if not answer:
        return None
    # A bare % is an expression delimiter to Emuera, so a translation that
    # writes "3.0%" would break the line it lands in. The source dodges this by
    # spelling the word out (3.0パーセント) or using ％; do the same here. The
    # markers stand in for real placeholders, so every % still visible is prose.
    answer = answer.replace("%", "％")
    tokens = protect(source)[1]
    found = [int(x) for x in re.findall(re.escape(_MASK_OPEN) + r"(\d+)" + re.escape(_MASK_CLOSE), answer)]
    if sorted(found) != list(range(len(tokens))):
        return None
    text = restore(answer, tokens)
    restored = protect(text)[1]
    if sorted(restored) != sorted(tokens):
        return None
    if restored != tokens and any(_ORDER_SENSITIVE.search(token) for token in tokens):
        return None
    if not text.strip() or text.strip() == source.strip():
        return None
    return text


def translate(texts: list[str], base: str, key: str = "", model: str = "",
              out: Path | None = None, batch: int = 8, retries: int = 2,
              log=None) -> dict:
    """Translate unique strings, keeping only those whose placeholders survive.

    Rows land in `out` as they succeed, so an interrupted run resumes instead
    of restarting: strings already in the file are skipped.
    """
    done: dict[str, str] = {}
    if out and out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("original") and row.get("translation"):
                done[row["original"]] = row["translation"]
    todo = [t for t in texts if t not in done]
    stats = {"todo": len(todo), "done": len(done), "kept": 0, "dropped": 0}
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        fh = out.open("a", encoding="utf-8")
    else:
        fh = None
    try:
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            masked = [protect(t)[0] for t in chunk]
            answers: list[str] = []
            for attempt in range(retries + 1):
                try:
                    answers = _chat(base, key, model, masked)
                except Exception as exc:  # network, timeout, bad gateway
                    if log:
                        log(f"batch failed ({exc}); retry {attempt + 1}")
                    answers = []
                if len(answers) == len(chunk):
                    break
                # Short or malformed answers: retry the lines one by one.
                if attempt == retries:
                    answers = answers + [""] * (len(chunk) - len(answers))
            for source, answer in zip(chunk, answers):
                text = _accept(source, answer)
                if text is None:
                    # One more try on its own: a batch answer that loses a
                    # marker usually comes out right when the model sees
                    # nothing but that line.
                    for _ in range(retries):
                        try:
                            single = _chat(base, key, model, [protect(source)[0]])
                        except Exception:
                            single = []
                        text = _accept(source, single[0] if single else "")
                        if text is not None:
                            break
                if text is None:
                    stats["dropped"] += 1
                    continue
                done[source] = text
                stats["kept"] += 1
                if fh:
                    fh.write(json.dumps({"original": source, "translation": text},
                                        ensure_ascii=False) + "\n")
                    fh.flush()
            if log:
                log(f"{min(i + batch, len(todo))}/{len(todo)} kept={stats['kept']} dropped={stats['dropped']}")
    finally:
        if fh:
            fh.close()
    stats["rows"] = done
    return stats
