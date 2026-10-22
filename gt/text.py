"""Text normalisation shared by the TM builder, the cache and lookups.

The key rule matters more than it looks: the translation memory is keyed on a
whitespace-collapsed form, so a paragraph that the game renders with different
line breaks or trailing spaces still matches.
"""

from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")
# Zero-width characters appear in some localisation dumps and would otherwise
# make two visually identical strings miss each other.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍﻿"), None)


def normalize(text: str) -> str:
    """Return the lookup key for a piece of source text."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text).translate(_INVISIBLE)
    return _WS.sub(" ", text).strip()


def is_translatable(key: str) -> bool:
    """Skip pure punctuation, digits and symbol runs; they never need a model."""
    if not key:
        return False
    return any(ch.isalpha() for ch in key)
