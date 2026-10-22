"""Run the in-page RPG Maker runtime under Node with a fake engine.

The dialogue window has two shapes the adapter has to tell apart:

* the page is still being typed  -> `_textState` is set
* the page is drawn and waiting  -> `_textState` is null again

A translation can arrive in either one, and the second used to be dropped
because the restart path bailed on the null state. These tests pin both, plus
the two cases where restarting is wrong: a choice window on top of the page,
and a window that is closing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent / "gt" / "js" / "rpgmaker.js"

HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const path = process.argv[2];
    const src = fs.readFileSync(path, "utf8").replace(
      "/*__GT_CONFIG__*/ null",
      '{reportName: "__gt_report", restartMessage: true, skipPatterns: []}'
    );

    const reports = [];
    const results = [];
    function check(name, ok, detail) {
      results.push({name: name, ok: !!ok, detail: detail === undefined ? null : detail});
    }

    function fresh(opts) {
      const win = {
        visible: opts.visible !== false,
        openness: opts.openness === undefined ? 255 : opts.openness,
        _textState: opts.textState === undefined ? null : opts.textState,
        isClosing: function () { return !!opts.closing; },
        isAnySubWindowActive: function () { return !!opts.subActive; },
        startMessage: function () { win.starts = (win.starts || 0) + 1; },
        refresh: function () {},
      };
      global.window = global;
      global.__gt_report = function (payload) { reports.push(payload); };
      global.Window_Base = function () {};
      Window_Base.prototype.drawTextEx = function (t) { return t; };
      Window_Base.prototype.drawText = function (t) { return t; };
      global.Game_Message = function () {};
      Game_Message.prototype.add = function (t) { return t; };
      const scene = { _windowLayer: { children: [win] } };
      global.SceneManager = { _scene: scene };
      global.$gameMessage = {
        _texts: (opts.texts || []).slice(),
        _gtOrig: null,
        allText: function () { return this._texts.join("\\n"); },
      };
      eval(src);
      return {win: win, msg: global.$gameMessage};
    }

    const PAYLOAD = JSON.stringify([{id: "こんにちは", text: "你好"}]);

    // 1. Page finished drawing, waiting for the player: the translation lands.
    let s = fresh({texts: ["こんにちは"], textState: null});
    window.__GT.apply(PAYLOAD);
    check("finished page is rewritten", s.msg._texts[0] === "你好", s.msg._texts);
    check("finished page is redrawn", s.win.starts === 1, s.win.starts);

    // 2. Page still typing: same result, through the original path.
    s = fresh({texts: ["こんにちは"], textState: {index: 0}});
    window.__GT.apply(PAYLOAD);
    check("typing page is rewritten", s.msg._texts[0] === "你好", s.msg._texts);

    // 3. Choice window on top: reprinting would desynchronise the selection.
    s = fresh({texts: ["こんにちは"], textState: null, subActive: true});
    window.__GT.apply(PAYLOAD);
    check("open choice is left alone", s.msg._texts[0] === "こんにちは", s.msg._texts);
    check("open choice is not redrawn", !s.win.starts, s.win.starts);

    // 4. Window closing: restarting leaves an empty box.
    s = fresh({texts: ["こんにちは"], textState: null, closing: true});
    window.__GT.apply(PAYLOAD);
    check("closing window is left alone", s.msg._texts[0] === "こんにちは", s.msg._texts);

    // 5. Window hidden: nothing on screen to redraw.
    s = fresh({texts: ["こんにちは"], textState: null, visible: false});
    window.__GT.apply(PAYLOAD);
    check("hidden window is left alone", !s.win.starts, s.win.starts);

    // 6. Restoring the original drops the translation from the rows.
    s = fresh({texts: ["こんにちは"], textState: null});
    window.__GT.apply(PAYLOAD);
    window.__GT.setMode("original");
    check("restore brings the source back", s.msg._texts[0] === "こんにちは", s.msg._texts);

    console.log(JSON.stringify(results));
    """
)


class RuntimeRestartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("node is not installed")
        if not RUNTIME.exists():
            raise unittest.SkipTest("runtime script is missing")

    def run_harness(self) -> dict[str, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "harness.js"
            script.write_text(HARNESS, encoding="utf-8")
            proc = subprocess.run(
                [self.node, str(script), str(RUNTIME)],
                capture_output=True, text=True, timeout=60, encoding="utf-8")
        if proc.returncode != 0:
            self.fail("node harness failed:\n" + proc.stderr)
        return {item["name"]: item for item in json.loads(proc.stdout)}

    def test_message_restart_cases(self) -> None:
        results = self.run_harness()
        for name, item in results.items():
            with self.subTest(name):
                self.assertTrue(item["ok"], "%s -> %s" % (name, item["detail"]))


if __name__ == "__main__":
    unittest.main()
