"""Youdao document-translation backend (bulk, offline).

Status: interface only. Document translation is a file-in/file-out service, so
it does not fit the live runtime; it belongs to an offline pass that fills the
cache ahead of time (upload a JSONL of untranslated strings, download the
result, import with `gt cache import`).

Why it is still worth wiring up: the SVIP document quota is 2,000,000
characters per calendar month, which is by far the cheapest volume source
available for this project. Plain UI automation is the fallback, not this.

Blocked on: account tier. The free 300 credits are a one-off token and do not
buy document translation, so nothing here can be exercised yet.
"""

from __future__ import annotations

from .base import Backend, Request


class YoudaoDocBackend(Backend):
    name = "youdao_doc"
    batch_size = 50

    def available(self) -> bool:
        self.log.warning("youdao_doc backend is not implemented yet (account tier unverified)")
        return False

    def translate(self, items: list[Request]) -> dict[str, str]:
        raise NotImplementedError("youdao_doc: not implemented")
