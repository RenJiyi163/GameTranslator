"""Placeholder and markup protection.

A translation may reorder words but must not lose a slot the game will fill in
later (`[name]`, `%s`, `{0}`), a tag the renderer depends on (`<b>`), or a
separator the UI splits on (`|`). Everything here is game-agnostic: the rules
are about text shape, not about any particular engine.
"""

from __future__ import annotations

import re
from collections import Counter

# Markup that survives into the string itself (HTML-ish or engine tags).
_TAG = re.compile(r"</?[A-Za-z][^<>]{0,60}>")
# Bracketed slots: [name], [/x], {0}, {player_name}
_SLOT = re.compile(r"\[[^\[\]]{0,60}\]|\{[^{}]{0,60}\}")
# A slot whose body is a plain identifier is code (a variable or formatter
# name) and must survive verbatim. Anything else inside a slot - `[tps|s]`,
# `[rand|a|b]` - carries translatable words, so only its structure matters:
# the community memory legitimately rewrites `[tps|s]` as `[复数|]`.
_IDENT = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_.]*|\d+)")
# printf: %s %d %1$s %.2f %%
_PRINTF = re.compile(r"%(?:\d+\$)?[-+ #0]*\d*(?:\.\d+)?[sdfxXeEgGcb]|%%")
# Shell-ish: $var, ${var}
_DOLLAR = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*")


def _tokens(text: str) -> Counter:
    tokens: Counter = Counter()
    for pattern, prefix in (
        (_TAG, "tag:"),
        (_PRINTF, "fmt:"),
        (_DOLLAR, "var:"),
    ):
        for match in pattern.findall(text or ""):
            tokens[prefix + match] += 1
    for match in _SLOT.findall(text or ""):
        body = match[1:-1].strip()
        if _IDENT.fullmatch(body):
            tokens["slot:" + body] += 1
        else:
            tokens["slotmacro:" + str(body.count("|"))] += 1
    return tokens


def _skeleton(text: str) -> str:
    """Text with every protected span replaced by a marker, for `|` counting."""
    out = text or ""
    for pattern in (_TAG, _SLOT, _PRINTF, _DOLLAR):
        out = pattern.sub("\u0000", out)
    return out


def problems(original: str, translated: str) -> list[str]:
    """Return a list of protection violations; empty means safe to show."""
    issues: list[str] = []
    src, dst = _tokens(original), _tokens(translated)
    missing = src - dst
    extra = dst - src
    if missing:
        issues.append("missing:" + ",".join(sorted(missing.elements())))
    if extra:
        issues.append("extra:" + ",".join(sorted(extra.elements())))
    if _skeleton(original).count("|") != _skeleton(translated).count("|"):
        issues.append("pipe-count")
    # Line breaks carry layout in many engines; a translation that drops one
    # silently merges two blocks.
    if (original or "").count("\n") != (translated or "").count("\n"):
        issues.append("newline-count")
    return issues


def safe(original: str, translated: str) -> bool:
    return not problems(original, translated)
