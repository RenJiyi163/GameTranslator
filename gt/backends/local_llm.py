"""OpenAI-compatible chat backend.

The same code drives LM Studio, DeepSeek and any other endpoint that speaks
`/v1/models` + `/v1/chat/completions`; only `base`, `apiKey` and `model`
differ. Which of those a name points at lives in `backends.json`.

Deliberately talks HTTP directly instead of shelling out to a local CLI:
one process per batch would be slower than the model itself, and this keeps
the backend free of any dependency outside the stdlib.

Model loading is not this backend's job. Run `llm load <model>` (or let LM
Studio JIT-load) before starting; `available()` reports whether the endpoint
answers so the chain can skip it cleanly.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .base import Backend, Request

SYSTEM_PROMPT = (
    "你是游戏本地化译者。把用户给出的游戏文本（英文或日文）逐条译成简体中文。\n"
    "要求：\n"
    "1. 保留原文中的所有标记与占位符，原样放回：<b> <i> <a> [name] {0} %s $var 以及 | 分隔符、换行。\n"
    "   以反斜杠开头的控制码（如 \\C[3] \\V[1] \\N[2] \\FS[20] \\! \\|）也必须原样保留。\n"
    "2. 保持每条的原文意思与语气，不要合并、拆分或增删条目。\n"
    "3. 只输出 JSON 对象，键是条目编号，值是译文。不要输出任何解释。\n"
    "示例输入：[{\"i\":0,\"t\":\"Hello <b>world</b>\"}]\n"
    "示例输出：{\"0\":\"你好 <b>世界</b>\"}"
)


class LocalLLMBackend(Backend):
    name = "local_llm"
    batch_size = 6

    def __init__(self, config=None, log=None):
        super().__init__(config, log)
        self.base = self.config.get("base") or os.environ.get("LOCAL_LLM_BASE", "http://127.0.0.1:1234/v1")
        # LM Studio answers to whatever single model is loaded even when the
        # name does not match, so a wrong id fails silently instead of loudly.
        # LOCAL_LLM_MODEL wins over the profile: it is how you switch models
        # for one run without editing a profile.
        self.model = os.environ.get("LOCAL_LLM_MODEL") or self.config.get("model", "writer")
        self.api_key = (self.config.get("apiKey") or "").strip()
        self.timeout = float(self.config.get("timeout", 300))
        self.max_tokens = int(self.config.get("maxTokens", 4096))
        self.temperature = float(self.config.get("temperature", 0.2))
        #: How many requests this endpoint can usefully run at once; the
        #: pipeline starts that many workers (max over the chain).
        self.concurrency = max(1, int(self.config.get("concurrency", 1)))
        self.log.debug("local_llm base=%s model=%s maxTokens=%d concurrency=%d",
                       self.base, self.model, self.max_tokens, self.concurrency)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def available(self) -> bool:
        try:
            req = urllib.request.Request(self.base + "/models", headers=self._headers())
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _system_prompt(self, items: list[Request]) -> str:
        """Base prompt plus the glossary terms that occur in this batch.

        Only terms present in the batch go in: a 300-line glossary would eat
        the context for nothing, and the ones that matter are the ones on
        screen.
        """
        glossary = self.glossary
        if glossary is None or not len(glossary):
            return SYSTEM_PROMPT
        seen: dict[str, str] = {}
        for item in items:
            for source, target in glossary.hint(item.text):
                seen.setdefault(source, target)
        if not seen:
            return SYSTEM_PROMPT
        terms = "；".join(f"{s} => {t}" for s, t in list(seen.items())[:40])
        return SYSTEM_PROMPT + "\n术语表（必须照此翻译）：" + terms

    def translate(self, items: list[Request]) -> dict[str, str]:
        if not items:
            return {}
        payload = [{"i": idx, "t": item.text} for idx, item in enumerate(items)]
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": self._system_prompt(items)},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        req = urllib.request.Request(
            self.base + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            self.log.warning("local_llm request failed: %s", exc)
            return {}

        content = (doc.get("choices") or [{}])[0].get("message", {}).get("content", "")
        mapping = _parse_json_object(content)
        out: dict[str, str] = {}
        for idx, item in enumerate(items):
            value = mapping.get(str(idx))
            if isinstance(value, str) and value.strip():
                out[item.key] = value
        if len(out) < len(items):
            # The usual cause is maxTokens too small: these models think
            # before answering, and a truncated reply loses the JSON tail.
            # Without this line the loss is invisible - the pipeline just
            # treats those items as misses and leaves them in English.
            self.log.warning("local_llm returned %d/%d items (check maxTokens)",
                             len(out), len(items))
        return out


def _parse_json_object(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
