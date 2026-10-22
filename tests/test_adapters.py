"""Adapter plumbing that can be checked without a game.

A mode switch is a string handed to the page; getting that string wrong does
not raise anywhere, the page just falls back to "translated". These tests pin
the exact expression.
"""

from __future__ import annotations

import json
import logging
import unittest
import urllib.request
from pathlib import Path

from gt.adapters.electron_cdp import ElectronCDPAdapter
from gt.adapters.renpy import RenPyAdapter
from gt.adapters.rpgmaker_mv_mz import RPGMakerAdapter
from gt.config import Profile

LOG = logging.getLogger("test")


class FakeClient:
    def __init__(self) -> None:
        self.expressions: list[str] = []

    def evaluate(self, expression: str, timeout: float | None = None):
        self.expressions.append(expression)
        return None


def rpgmaker_profile() -> Profile:
    raw = {"id": "p", "name": "p", "adapter": "rpgmaker_mv_mz", "backends": []}
    return Profile.from_dict(raw, Path("."))


def electron_profile() -> Profile:
    raw = {"id": "e", "name": "e", "adapter": "electron_cdp",
           "selectors": [{"css": "body"}], "backends": []}
    return Profile.from_dict(raw, Path("."))


class SetModeTests(unittest.TestCase):
    def test_rpgmaker_passes_a_plain_string(self) -> None:
        adapter = RPGMakerAdapter(rpgmaker_profile(), LOG)
        adapter.client = FakeClient()
        adapter.set_mode("original")
        self.assertEqual(adapter.client.expressions,
                         ['window.__GT && window.__GT.setMode("original")'])

    def test_electron_passes_a_plain_string(self) -> None:
        adapter = ElectronCDPAdapter(electron_profile(), LOG)
        adapter.client = FakeClient()
        adapter.set_mode("translated")
        self.assertEqual(adapter.client.expressions,
                         ['window.__GT && window.__GT.setMode("translated")'])

    def test_round_trip_does_not_wrap_the_string_twice(self) -> None:
        adapter = RPGMakerAdapter(rpgmaker_profile(), LOG)
        adapter.client = FakeClient()
        adapter.set_mode("original")
        expression = adapter.client.expressions[0]
        self.assertNotIn('\\"', expression)


def renpy_profile() -> Profile:
    raw = {"id": "r", "name": "r", "adapter": "renpy",
           "adapterConfig": {"exe": "renpy.exe"}, "backends": []}
    return Profile.from_dict(raw, Path("."))


class FakePipeline:
    """Stands in for the real pipeline behind the Ren'Py bridge."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def translate_text(self, text: str, timeout: float | None = None) -> str | None:
        self.asked.append(text)
        return self.answers.get(text)


class RenPyBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = RenPyAdapter(renpy_profile(), LOG)
        self.adapter.pipeline = FakePipeline({"こんにちは": "你好"})
        self.adapter._start_server()

    def tearDown(self) -> None:
        self.adapter.stop()

    def post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.adapter.port, path),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_translate_answers_from_the_pipeline(self) -> None:
        self.assertEqual(self.post("/translate", {"text": "こんにちは"}),
                         {"text": "你好", "hit": True})
        self.assertEqual(self.post("/translate", {"text": "unknown"}),
                         {"text": "unknown", "hit": False})

    def test_poll_is_idempotent_until_the_mode_moves(self) -> None:
        first = self.post("/poll", {})
        self.assertEqual(first["mode"], "translated")
        self.assertEqual(self.post("/poll", {}), first)
        self.adapter.set_mode("original")
        moved = self.post("/poll", {})
        self.assertEqual(moved["mode"], "original")
        self.assertEqual(moved["seq"], first["seq"] + 1)
        self.assertEqual(self.post("/poll", {}), moved)

    def test_setting_the_same_mode_does_not_bump_the_sequence(self) -> None:
        before = self.post("/poll", {})["seq"]
        self.adapter.set_mode("translated")
        self.assertEqual(self.post("/poll", {})["seq"], before)

    def test_in_game_toggle_reaches_the_app(self) -> None:
        seen: list[str] = []
        self.adapter.on_mode_change = seen.append
        answer = self.post("/mode", {"mode": "original"})
        self.assertEqual(answer["mode"], "original")
        self.assertEqual(seen, ["original"])
        # The app already knows; polling must not look like a fresh change.
        self.assertEqual(self.post("/poll", {})["seq"], answer["seq"])


if __name__ == "__main__":
    unittest.main()
