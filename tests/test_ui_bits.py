"""Pure helpers behind the window chrome: hotkeys, tails, hit rate."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gt import settings
from gt.logview import filter_lines, read_tail, read_unmatched
from gt.overlay import hit_rate
from gt.tray import parse_hotkey


class TestHotkey(unittest.TestCase):
    def test_default_combo(self):
        self.assertEqual(parse_hotkey("Ctrl+Shift+T"), (0x02 | 0x04, ord("T")))

    def test_lowercase_and_spaces(self):
        self.assertEqual(parse_hotkey(" ctrl + alt + f9 "), (0x01 | 0x02, 0x78))

    def test_single_key_without_modifier(self):
        self.assertEqual(parse_hotkey("F5"), (0, 0x74))

    def test_rejects_things_registerhotkey_cannot_take(self):
        for spec in ("", "Ctrl+", "Ctrl+Shift+Enter", "Ctrl+F25", "Space", "Ctrl+é"):
            self.assertIsNone(parse_hotkey(spec), spec)


class TestHitRate(unittest.TestCase):
    def test_nothing_seen(self):
        self.assertIsNone(hit_rate(None))
        self.assertIsNone(hit_rate({"seen": 0}))

    def test_memory_and_cache_count_as_hits(self):
        data = {"seen": 100, "tm_hits": 60, "cache_hits": 15, "backend_hits": 25}
        self.assertAlmostEqual(hit_rate(data), 0.75)

    def test_never_exceeds_one(self):
        self.assertEqual(hit_rate({"seen": 2, "tm_hits": 5}), 1.0)


class TestLogTail(unittest.TestCase):
    def test_tail_keeps_whole_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gt.log"
            path.write_text("".join(f"line {i}\n" for i in range(100)), encoding="utf-8")
            text = read_tail(path, limit=60)
            self.assertNotIn("line 0\n", text)
            self.assertTrue(text.endswith("line 99\n"))
            self.assertTrue(text.startswith("line "))

    def test_missing_file_is_empty(self):
        self.assertEqual(read_tail(Path("no-such-file.log")), "")

    def test_level_filter(self):
        text = ("2026 INFO a\n2026 WARNING b\n2026 ERROR c\n"
                "2026 DEBUG d\n")
        self.assertEqual(filter_lines(text, ("ERROR", "CRITICAL")).splitlines(),
                         ["2026 ERROR c"])
        self.assertEqual(filter_lines(text, None), text)


class TestUnmatchedReader(unittest.TestCase):
    def test_skips_broken_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unmatched.jsonl"
            path.write_text('{"text": "a"}\n\nnot json\n{"text": "b"}\n', encoding="utf-8")
            rows = read_unmatched(path)
            self.assertEqual([row["text"] for row in rows], ["a", "b"])


class TestReadyBackends(unittest.TestCase):
    def test_local_address_needs_no_key(self):
        data = {"active": ["local_llm"], "backends": {
            "local_llm": {"base": "http://127.0.0.1:1234/v1", "apiKey": ""}}}
        self.assertEqual(settings.ready_backends(data), ["local_llm"])

    def test_remote_without_key_is_not_ready(self):
        data = {"active": ["deepseek"], "backends": {
            "deepseek": {"base": "https://api.deepseek.com/v1", "apiKey": ""}}}
        self.assertEqual(settings.ready_backends(data), [])

    def test_remote_with_key_is_ready(self):
        data = {"active": ["deepseek"], "backends": {
            "deepseek": {"base": "https://api.deepseek.com/v1", "apiKey": "sk-x"}}}
        self.assertEqual(settings.ready_backends(data), ["deepseek"])


if __name__ == "__main__":
    unittest.main()
