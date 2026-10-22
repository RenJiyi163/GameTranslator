"""Ren'Py bridge tests: no game, no GUI, just the HTTP path the filter uses."""

from __future__ import annotations

import http.client
import json
import logging
import unittest
from pathlib import Path

from gt.adapters import renpy
from gt.config import Profile


def make_profile() -> Profile:
    raw = {
        "id": "renpy-test",
        "name": "test",
        "adapter": "renpy",
        "adapterConfig": {"exe": "C:/nope/Game.exe"},
        "backends": [],
    }
    return Profile.from_dict(raw, Path("."))


class FakePipeline:
    def translate_text(self, text: str, timeout: float = 8.0):
        return f"译:{text}" if "hello" in text else None


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = renpy.RenPyAdapter(make_profile(), logging.getLogger("test"))
        self.adapter.pipeline = FakePipeline()
        self.adapter._start_server()

    def tearDown(self) -> None:
        self.adapter.stop()

    def post(self, text: str) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", self.adapter.port, timeout=5)
        try:
            body = json.dumps({"text": text}).encode("utf-8")
            conn.request("POST", "/translate", body, {"Content-Type": "application/json"})
            return json.loads(conn.getresponse().read().decode("utf-8"))
        finally:
            conn.close()

    def test_hit_returns_the_translation(self) -> None:
        self.assertEqual(self.post("hello there"), {"text": "译:hello there", "hit": True})

    def test_miss_returns_the_original(self) -> None:
        self.assertEqual(self.post("nothing here"), {"text": "nothing here", "hit": False})

    def test_counts_are_kept(self) -> None:
        self.post("hello")
        self.post("nope")
        self.assertEqual(self.adapter.stats()["requests"], 2)
        self.assertEqual(self.adapter.stats()["hits"], 1)

    def test_without_a_pipeline_the_original_wins(self) -> None:
        self.adapter.pipeline = None
        self.assertEqual(self.post("hello"), {"text": "hello", "hit": False})


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = renpy.RenPyAdapter(make_profile(), logging.getLogger("test"))

    def test_commands_ride_the_poll_and_are_drained_once(self) -> None:
        self.adapter.send_command("event", "dismiss")
        self.adapter.send_command("start")
        state = self.adapter.poll_state()
        self.assertEqual([c["cmd"] for c in state["cmds"]], ["event", "start"])
        self.assertEqual(state["cmds"][0]["arg"], "dismiss")
        self.assertEqual(self.adapter.poll_state()["cmds"], [])

    def test_a_poll_without_commands_says_so(self) -> None:
        self.assertEqual(self.adapter.poll_state()["cmds"], [])


class ScriptTests(unittest.TestCase):
    def test_the_injected_script_installs_the_filter(self) -> None:
        self.assertIn("_gt_translation.translate_string = gt_lookup", renpy.INJECT_SCRIPT)
        self.assertIn("GT_RENPY_PORT", renpy.INJECT_SCRIPT)

    def test_the_injected_script_takes_commands(self) -> None:
        # Navigation the harness uses: no cursor, no keyboard, no window raise.
        self.assertIn('"start"', renpy.INJECT_SCRIPT)
        self.assertIn("renpy.queue_event", renpy.INJECT_SCRIPT)
        self.assertNotIn("mouse_event", renpy.INJECT_SCRIPT)
        self.assertNotIn("SetCursorPos", renpy.INJECT_SCRIPT)

    def test_the_adapter_never_writes_into_the_game_directory(self) -> None:
        profile = make_profile()
        adapter = renpy.RenPyAdapter(profile, logging.getLogger("test"))
        self.assertNotIn(str(Path("C:/nope")), str(adapter.mod_dir))
        self.assertEqual(adapter.mod_dir.parent, profile.dir)


if __name__ == "__main__":
    unittest.main()
