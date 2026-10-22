"""Machine-wide model settings, stored in `backends.json` next to profiles/.

A model endpoint belongs to the machine, not to one game: the same LM Studio
install serves every profile, and the DeepSeek key is the same key. Games pin
their own values only when they need to (a bigger maxTokens, another model),
through `backendConfig` in their profile.

`active` is the default chain for games that do not name one. A profile with
the `backends` key wins over it; a profile without the key inherits.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from .config import ROOT

DEFAULT_BACKENDS: dict[str, dict] = {
    "local_llm": {
        "type": "openai_compatible",
        "label": "本地模型（LM Studio）",
        "base": "http://127.0.0.1:1234/v1",
        "apiKey": "",
        "model": "",
        "temperature": 0.2,
        "maxTokens": 8192,
        "timeout": 300,
        "concurrency": 1,
    },
    "deepseek": {
        "type": "openai_compatible",
        "label": "DeepSeek",
        "base": "https://api.deepseek.com/v1",
        "apiKey": "",
        "model": "deepseek-chat",
        "temperature": 0.3,
        "maxTokens": 4096,
        "timeout": 120,
        "concurrency": 2,
    },
}

DEFAULT_ACTIVE = ["local_llm"]

#: Field order and labels for the settings window.
FIELDS = [
    ("type", "类型"),
    ("base", "地址"),
    ("apiKey", "密钥"),
    ("model", "模型"),
    ("temperature", "温度"),
    ("maxTokens", "最大 token"),
    ("timeout", "超时（秒）"),
    ("concurrency", "并发"),
]


def config_path(root: Path | None = None) -> Path:
    return Path(root or ROOT) / "backends.json"


def load(root: Path | None = None) -> dict:
    """Settings as a dict; a missing file means the defaults."""
    path = config_path(root)
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    backends = {name: dict(defn) for name, defn in DEFAULT_BACKENDS.items()}
    backends.update({name: dict(defn) for name, defn in (data.get("backends") or {}).items()})
    active = data.get("active")
    if not isinstance(active, list):
        active = list(DEFAULT_ACTIVE)
    return {"active": active, "backends": backends}


def save(data: dict, root: Path | None = None) -> None:
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def definitions(root: Path | None = None) -> dict[str, dict]:
    return load(root)["backends"]


def active_backends(root: Path | None = None) -> list[str]:
    return list(load(root)["active"])


def ready_backends(data: dict | None = None) -> list[str]:
    """Active names that look usable without opening the settings window.

    A local address needs nothing else; anything remote needs a key. An empty
    model name is tolerated for the local server, which answers with whatever
    is loaded when none is named.
    """
    data = data or load()
    ready = []
    for name in data.get("active") or []:
        defn = (data.get("backends") or {}).get(name) or {}
        base = str(defn.get("base") or "").strip()
        if not base:
            continue
        local = "127.0.0.1" in base or "localhost" in base
        if local or str(defn.get("apiKey") or "").strip():
            ready.append(name)
    return ready


# -- talking to an endpoint ---------------------------------------------------

def _endpoint(base: str) -> str:
    base = (base or "").strip().rstrip("/")
    if not base:
        raise ValueError("没填地址")
    return base


def _headers(defn: dict) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = (defn.get("apiKey") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def fetch_models(defn: dict, timeout: float = 8.0) -> list[str]:
    """Model ids the endpoint reports; raises with a readable reason."""
    url = _endpoint(defn.get("base", "")) + "/models"
    req = urllib.request.Request(url, headers=_headers(defn))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
    items = doc.get("data") if isinstance(doc, dict) else None
    return [str(item["id"]) for item in (items or []) if isinstance(item, dict) and item.get("id")]


def probe(defn: dict, timeout: float = 8.0) -> tuple[bool, str]:
    """Is the endpoint reachable and does it accept the key?"""
    try:
        models = fetch_models(defn, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, f"连上了，但密钥被拒（HTTP {exc.code}）"
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"连不上：{getattr(exc, 'reason', exc)}"
    except (ValueError, json.JSONDecodeError) as exc:
        return False, f"答复看不懂：{exc}"
    if not models:
        return True, "连上了，但它说现在没有模型"
    return True, f"连上了，{len(models)} 个模型"
