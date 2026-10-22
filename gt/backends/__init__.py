"""Backend registry and chain construction."""

from __future__ import annotations

import logging

from .base import Backend, Request
from .local_llm import LocalLLMBackend
from .youdao_doc import YoudaoDocBackend
from .youdao_ui import YoudaoUIBackend

REGISTRY: dict[str, type[Backend]] = {
    "local_llm": LocalLLMBackend,          # kept for older profiles
    "openai_compatible": LocalLLMBackend,  # LM Studio, DeepSeek, anything /v1
    "youdao_doc": YoudaoDocBackend,
    "youdao_ui": YoudaoUIBackend,
}


def build_chain(names: list[str], config: dict, log: logging.Logger,
                defs: dict[str, dict] | None = None) -> list[Backend]:
    """Wire up the named backends.

    Each name is looked up in the machine-wide definitions first and then
    overridden by the profile's `backendConfig`, so a game can pin a model or
    a maxTokens without touching the shared settings.
    """
    if defs is None:
        from ..settings import definitions

        defs = definitions()
    chain: list[Backend] = []
    for name in names:
        merged = dict(defs.get(name, {}))
        merged.update(config.get(name, {}))
        kind = merged.get("type") or name
        cls = REGISTRY.get(kind)
        if cls is None:
            log.warning("unknown backend type %r for %r", kind, name)
            continue
        backend = cls(merged, log.getChild(name))
        backend.name = name
        if not backend.available():
            log.warning("backend unavailable, skipped: %s", name)
            continue
        log.info("backend ready: %s (%s, batch=%d)", name, kind, backend.batch_size)
        chain.append(backend)
    return chain


__all__ = ["Backend", "Request", "build_chain", "REGISTRY"]
