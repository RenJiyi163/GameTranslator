#!/usr/bin/env python3
"""Dev probe: attach to a running/launchable Electron app and dump its DOM.

Used to find out what the game actually renders before writing selectors or
matching rules. Not part of the runtime.

    python scripts/probe_dom.py --profile mygame [--attach] [--port 9222]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gt.adapters.electron_cdp import CDPClient, ElectronCDPAdapter  # noqa: E402
from gt.config import Profile  # noqa: E402

DUMP_JS = r"""
(() => {
  const out = {url: location.href, title: document.title, nodes: {}};
  const sels = %s;
  for (const s of sels) {
    let els = [];
    try { els = Array.from(document.querySelectorAll(s)); } catch (e) { continue; }
    out.nodes[s] = els.slice(0, 12).map(el => ({
      tag: el.tagName.toLowerCase(),
      id: el.id,
      cls: el.className && el.className.toString().slice(0, 80),
      text: (el.innerText || "").replace(/\s+/g, " ").trim().slice(0, 200),
      html: (el.innerHTML || "").replace(/\s+/g, " ").trim().slice(0, 240),
      textNodes: Array.from(el.childNodes).filter(n => n.nodeType === 3).length,
      childEls: el.children.length
    }));
  }
  out.buttons = Array.from(document.querySelectorAll("button")).slice(0, 20).map(b => ({
    cls: b.className, text: (b.innerText || "").trim().slice(0, 60), id: b.id
  }));
  return JSON.stringify(out);
})()
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="mygame")
    ap.add_argument("--attach", action="store_true")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()

    profile = Profile.load(args.profile)
    if args.attach:
        profile.adapter_config["attachOnly"] = True
    if args.port:
        profile.adapter_config["port"] = args.port

    adapter = ElectronCDPAdapter(profile)
    adapter.start()
    client: CDPClient = adapter.client  # type: ignore[assignment]

    import time

    ready = profile.adapter_config.get("readySelector") or profile.selectors[0].css
    deadline = time.time() + 90
    while time.time() < deadline:
        probe = client.evaluate(
            f"document.querySelectorAll({json.dumps(ready)}).length"
        )
        if int((probe.get("result") or {}).get("value") or 0) > 0:
            break
        time.sleep(0.5)
    else:
        print(f"warning: {ready} never appeared", file=sys.stderr)

    sels = [s.css for s in profile.selectors]
    js = DUMP_JS % json.dumps([s.css for s in profile.selectors] + ["body"])
    result = client.evaluate(js)
    value = (result.get("result") or {}).get("value")
    if not value:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    doc = json.loads(value)
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    print("\nselectors probed:", sels, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
