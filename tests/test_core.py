"""Core tests: no game, no network, no browser.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gt import games, settings  # noqa: E402
from gt.adapters.rpgmaker_mv_mz import RPGMakerAdapter  # noqa: E402
from gt.backends import build_chain  # noqa: E402
from gt.backends.base import Backend, Request  # noqa: E402
from gt.backends.local_llm import LocalLLMBackend  # noqa: E402
from gt.cache import TranslationCache  # noqa: E402
from gt.config import ConfigError, Profile  # noqa: E402
from gt.glossary import Glossary, GlossaryFile  # noqa: E402
from gt.pipeline import Pipeline  # noqa: E402
from gt.placeholders import problems  # noqa: E402
from gt.segment import Segment  # noqa: E402
from gt.text import is_translatable, normalize  # noqa: E402
from gt.tm import TranslationMemory, build  # noqa: E402


class TestText(unittest.TestCase):
    def test_normalize_collapses_whitespace(self):
        self.assertEqual(normalize("  a\n\n  b  "), "a b")
        self.assertEqual(normalize("​ a"), "a")

    def test_translatable(self):
        self.assertFalse(is_translatable("123 | ."))
        self.assertTrue(is_translatable("Next"))


class TestPlaceholders(unittest.TestCase):
    def test_tag_preserved(self):
        self.assertEqual(problems("Hi <b>you</b>", "你好 <b>你</b>"), [])

    def test_tag_dropped(self):
        self.assertTrue(problems("Hi <b>you</b>", "你好 你"))

    def test_slot_preserved(self):
        self.assertEqual(problems("Hello [name]", "你好[name]"), [])

    def test_printf(self):
        self.assertTrue(problems("You have %d gold", "你有金币"))
        self.assertEqual(problems("You have %d gold", "你有 %d 金币"), [])

    def test_pipe_count(self):
        self.assertTrue(problems("a | b", "甲乙"))

    def test_slot_contents_ignored(self):
        # Localisation often rewrites the inside of a bracket slot on purpose.
        self.assertEqual(problems("[tps|s]", "[复数|]"), [])


class TestTM(unittest.TestCase):
    def test_build_and_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "tm.jsonl"
            rows = [
                {"original": "Hello  there", "translation": "你好"},
                {"original": "Hello there", "translation": "你好啊"},  # same key, ignored
                {"original": "Goodbye", "translation": "再见"},
                {"original": "", "translation": "skip"},
            ]
            src.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
            db = Path(tmp) / "tm.sqlite"
            report = build(src, db)
            self.assertEqual(report["rows"], 2)
            self.assertEqual(report["conflicting_duplicates"], 1)

            memory = TranslationMemory(db)
            self.assertEqual(memory.lookup("Hello there"), "你好")
            self.assertEqual(memory.lookup("Goodbye"), "再见")
            self.assertIsNone(memory.lookup("nope"))
            memory.close()


class TestDocxImport(unittest.TestCase):
    """The document service returns prose; the memory needs the codes back."""

    def _docx(self, tmp: Path, paragraphs: list[str]) -> Path:
        from xml.sax.saxutils import escape
        import zipfile

        body = "".join(
            '<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % escape(p)
            for p in paragraphs
        )
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>%s</w:body></w:document>" % body
        )
        path = tmp / "back.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" ContentType="application/'
                'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
            )
            archive.writestr("word/document.xml", document)
        return path

    def test_refs_and_newlines_come_back(self):
        from scripts.import_docx import main as import_main
        import sys

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        rows = [
            {"id": "a", "category": "对话", "text": "体重〔0〕kg", "refs": ["\\V[1]"]},
            {"id": "b", "category": "物品", "text": "上\n下", "refs": []},
            {"id": "c", "category": "物品", "text": "様", "refs": []},
        ]
        mapping = tmp / "map.jsonl"
        mapping.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        # A header the service added shifts every paragraph down by one.
        docx = self._docx(tmp, ["标题", "体重〔0〕kg", "上\n下", ""])
        out = tmp / "out.jsonl"

        argv = sys.argv
        sys.argv = ["import_docx.py", str(mapping), str(docx), "-o", str(out)]
        try:
            self.assertEqual(import_main(), 0)
        finally:
            sys.argv = argv

        written = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([w["id"] if "id" in w else w["original"] for w in written],
                         ["体重\\V[1]kg", "上\n下"])
        report = json.loads((tmp / "out.report.json").read_text(encoding="utf-8"))
        # The empty paragraph is paired but refused, not carried into the memory.
        self.assertEqual([p["id"] for p in report["problems"]], ["c"])
        self.assertEqual(report["problems"][0]["issue"], "empty")


class TestCache(unittest.TestCase):
    def test_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cache.sqlite"
            first = TranslationCache(db, "demo")
            first.put("key", "orig", "译文", "fake")
            first.close()

            second = TranslationCache(db, "demo")
            self.assertEqual(second.get("key"), "译文")
            self.assertEqual(second.count(), 1)
            second.close()

    def test_clear_returns_count_and_empties_scope(self):
        # clear() used to call count() while holding the same non-reentrant
        # lock, which hung the CLI forever instead of failing. Run it on a
        # thread so a regression shows up as a timeout, not a stuck suite.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cache.sqlite"
            cache = TranslationCache(db, "demo")
            cache.put("a", "x", "甲", "fake")
            cache.put("b", "y", "乙", "fake")
            result: dict = {}

            worker = threading.Thread(target=lambda: result.setdefault("n", cache.clear()))
            worker.start()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "clear() deadlocked")
            self.assertEqual(result.get("n"), 2)
            self.assertEqual(cache.count(), 0)
            cache.close()

    def test_scopes_are_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cache.sqlite"
            a = TranslationCache(db, "game-a")
            a.put("k", "o", "甲", "fake")
            b = TranslationCache(db, "game-b")
            try:
                self.assertIsNone(b.get("k"))
            finally:
                a.close()
                b.close()


class PrefixBackend(Backend):
    """Translates by prefixing; optionally breaks a placeholder on purpose."""

    name = "fake"
    batch_size = 4

    def __init__(self, break_tags: bool = False):
        super().__init__({}, None)
        self.break_tags = break_tags
        self.calls = 0

    def translate(self, items: list[Request]) -> dict[str, str]:
        self.calls += 1
        out = {}
        for item in items:
            out[item.key] = ("ZH " + item.text) if not self.break_tags else item.text.replace("<b>", "")
        return out


class SlowBackend(Backend):
    """One item per call, a short sleep, and a record of which thread did it."""

    name = "slow"
    batch_size = 1

    def __init__(self):
        super().__init__({}, None)
        self.threads: set[str] = set()
        self._lock = threading.Lock()

    def translate(self, items: list[Request]) -> dict[str, str]:
        with self._lock:
            self.threads.add(threading.current_thread().name)
        time.sleep(0.1)
        return {item.key: "ZH " + item.text for item in items}


class TestPipeline(unittest.TestCase):
    def _workspace(self):
        """Temp dir registered for removal before any open handle is cleaned up."""
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        return tmp

    def _pipeline(self, tmp, backend, ready):
        cache = TranslationCache(tmp / "cache.sqlite", "demo")
        memory = TranslationMemory(tmp / "missing.sqlite")
        pipe = Pipeline(cache, memory, Glossary(), [backend], ready.append)
        pipe.start()
        self.addCleanup(cache.close)
        self.addCleanup(pipe.stop)
        return pipe, cache

    def _wait(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_memory_keyed_without_styling_codes_is_reached(self):
        """Offline translation loses \\C[17]; the lookup has to survive that."""
        tmp = self._workspace()
        source = tmp / "tm.jsonl"
        rows = [
            {"original": "えっちして妊娠させて", "translation": "做爱并怀孕"},
            {"original": "体重 \\V[1]kg", "translation": "体重 \\V[1]kg"},
        ]
        source.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        build(source, tmp / "tm.sqlite")
        cache = TranslationCache(tmp / "cache.sqlite", "demo")
        memory = TranslationMemory(tmp / "tm.sqlite")
        pipe = Pipeline(
            cache,
            memory,
            Glossary(),
            [],
            lambda _: None,
            strip_pattern=RPGMakerAdapter.DEFAULT_STRIP_PATTERN,
        )
        self.addCleanup(memory.close)
        self.addCleanup(cache.close)
        got = pipe.register([
            Segment(id="a", text="\\C[17]えっち\\c[0]して\\C[17]妊娠\\c[0]させて"),
            Segment(id="b", text="\\PX[30]体重 \\c[1]\\V[1]\\c[0]kg"),
            Segment(id="c", text="全く別の行"),
        ])
        self.assertEqual(got.get("a"), "做爱并怀孕")
        self.assertEqual(got.get("b"), "体重 \\V[1]kg")
        self.assertNotIn("c", got)

    def test_miss_then_hit_then_cache(self):
        tmp = self._workspace()
        ready: list[dict] = []
        pipe, cache = self._pipeline(tmp, PrefixBackend(), ready)
        self.assertEqual(pipe.register([Segment(id="1", text="Hello")]), {})
        self.assertTrue(pipe.wait_idle(10))
        self.assertTrue(self._wait(lambda: ready))
        self.assertEqual(ready[0], {"Hello": "ZH Hello"})
        self.assertEqual(cache.get("Hello"), "ZH Hello")

        # A fresh process against the same cache must answer instantly.
        ready2: list[dict] = []
        pipe2, _ = self._pipeline(tmp, PrefixBackend(), ready2)
        self.assertEqual(pipe2.register([Segment(id="9", text="Hello")]), {"9": "ZH Hello"})
        self.assertEqual(pipe2.stats["cache_hits"], 1)

    def test_backend_output_that_breaks_markup_is_dropped(self):
        tmp = self._workspace()
        ready: list[dict] = []
        pipe, cache = self._pipeline(tmp, PrefixBackend(break_tags=True), ready)
        pipe.register([Segment(id="1", text="Hi <b>you</b>")])
        self.assertTrue(pipe.wait_idle(10))
        time.sleep(0.3)
        self.assertEqual(ready, [])
        self.assertIsNone(cache.get("Hi <b>you</b>"))
        self.assertEqual(pipe.stats["rejected"], 1)

    def test_duplicate_text_is_translated_once(self):
        tmp = self._workspace()
        ready: list[dict] = []
        backend = PrefixBackend()
        pipe, _ = self._pipeline(tmp, backend, ready)
        pipe.register([Segment(id="1", text="Same"), Segment(id="2", text="Same")])
        self.assertTrue(pipe.wait_idle(10))
        time.sleep(0.3)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(len(ready), 1)

    def test_two_workers_drain_the_queue_in_parallel(self):
        tmp = self._workspace()
        ready: list[dict] = []
        backend = SlowBackend()
        cache = TranslationCache(tmp / "cache.sqlite", "demo")
        memory = TranslationMemory(tmp / "missing.sqlite")
        pipe = Pipeline(cache, memory, Glossary(), [backend], ready.append, workers=2)
        pipe.start()
        self.addCleanup(cache.close)
        self.addCleanup(pipe.stop)
        pipe.register([Segment(id=str(i), text=f"line {i}") for i in range(6)])
        self.assertTrue(pipe.wait_idle(10))
        self.assertGreaterEqual(len(backend.threads), 2)

    def test_default_is_one_worker(self):
        tmp = self._workspace()
        pipe, _ = self._pipeline(tmp, PrefixBackend(), [])
        self.assertEqual(pipe.workers, 1)

    def test_backend_only_strategy_skips_the_memory(self):
        tmp = self._workspace()
        source = tmp / "tm.jsonl"
        source.write_text('{"original": "Hello", "translation": "你好"}\n', encoding="utf-8")
        memory_db = tmp / "tm.sqlite"
        build(source, memory_db)

        ready: list[dict] = []
        backend = PrefixBackend()
        cache = TranslationCache(tmp / "cache.sqlite", "demo")
        memory = TranslationMemory(memory_db)
        pipe = Pipeline(cache, memory, Glossary(), [backend], ready.append, use_tm=False)
        pipe.start()
        self.addCleanup(cache.close)
        self.addCleanup(pipe.stop)

        self.assertEqual(pipe.register([Segment(id="1", text="Hello")]), {})
        self.assertTrue(pipe.wait_idle(10))
        self.assertEqual(backend.calls, 1)


class TestGameRegistry(unittest.TestCase):
    """Engine detection and profile management, against fake game folders."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profiles = self.root / "profiles"

    def make(self, *paths: str, exe: str = "Game.exe") -> Path:
        base = self.root / "game"
        for rel in paths:
            target = base / rel
            if rel.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("", encoding="utf-8")
        program = base / exe
        if not program.exists():
            program.parent.mkdir(parents=True, exist_ok=True)
            program.write_bytes(b"MZ")
        return program

    def test_detects_rpgmaker_mz(self):
        exe = self.make("js/rmmz_core.js", "data/System.json", "nw.dll")
        self.assertEqual(games.detect_engine(exe).id, "rpgmaker_mz")
        self.assertTrue(games.detect_engine(exe).supported)

    def test_detects_rpgmaker_mv(self):
        exe = self.make("www/js/rpg_core.js", "nw.dll")
        self.assertEqual(games.detect_engine(exe).id, "rpgmaker_mv")

    def test_detects_rpgmaker_vx_ace(self):
        exe = self.make("Game.rgss3a")
        self.assertEqual(games.detect_engine(exe).id, "rpgmaker_ace")

    def test_detects_electron(self):
        exe = self.make("resources/app.asar")
        self.assertEqual(games.detect_engine(exe).id, "electron")

    def test_detects_nwjs(self):
        exe = self.make("node.dll")
        self.assertEqual(games.detect_engine(exe).id, "nwjs")

    def test_detects_renpy(self):
        exe = self.make("renpy/", "game/script.rpyc")
        self.assertEqual(games.detect_engine(exe).id, "renpy")

    def test_detects_unity(self):
        exe = self.make("UnityPlayer.dll", "Game_Data/")
        self.assertEqual(games.detect_engine(exe).id, "unity")

    def test_detects_unreal(self):
        exe = self.make("Content/Paks/", "Binaries/Win64/Game-Win64-Shipping.exe",
                        exe="Binaries/Win64/Game-Win64-Shipping.exe")
        self.assertEqual(games.detect_engine(exe).id, "unreal")

    def test_unknown_when_nothing_matches(self):
        self.assertEqual(games.detect_engine(self.make()).id, "unknown")

    def test_add_game_writes_a_loadable_profile(self):
        exe = self.make("js/rmmz_core.js", "nw.dll")
        game_id, engine = games.add_game(exe, "Test Game", root=self.profiles)
        self.assertEqual(engine.id, "rpgmaker_mz")
        profile = Profile.load(game_id, self.profiles)
        self.assertEqual(profile.name, "Test Game")
        self.assertEqual(profile.adapter, "rpgmaker_mv_mz")
        self.assertEqual(Path(profile.adapter_config["exe"]), exe.resolve())
        self.assertIn("port", profile.adapter_config)
        self.assertTrue((self.profiles / game_id / "glossary.tsv").exists())

    def test_ids_and_ports_stay_unique(self):
        exe = self.make("js/rmmz_core.js", "nw.dll")
        first, _ = games.add_game(exe, "Same", root=self.profiles)
        second, _ = games.add_game(exe, "Same", root=self.profiles)
        self.assertNotEqual(first, second)
        self.assertTrue(second.startswith(first))
        ports = {
            Profile.load(game_id, self.profiles).adapter_config["port"]
            for game_id in (first, second)
        }
        self.assertEqual(len(ports), 2)

    def test_unsupported_engine_is_refused(self):
        exe = self.make("UnityPlayer.dll", "Game_Data/")
        with self.assertRaises(games.UnsupportedEngine):
            games.add_game(exe, "Unity Thing", root=self.profiles)
        self.assertFalse((self.profiles / "Unity-Thing").exists())

    def test_missing_executable_is_refused(self):
        with self.assertRaises(ConfigError):
            games.add_game(self.root / "nope.exe", "Nope", root=self.profiles)

    def test_rename_port_and_remove(self):
        exe = self.make("js/rmmz_core.js", "nw.dll")
        game_id, _ = games.add_game(exe, "Old", root=self.profiles)

        games.rename_game(game_id, "新名字", root=self.profiles)
        games.set_port(game_id, 9333, root=self.profiles)
        profile = Profile.load(game_id, self.profiles)
        self.assertEqual(profile.name, "新名字")
        self.assertEqual(profile.adapter_config["port"], 9333)

        info = [g for g in games.list_games(self.profiles) if g.id == game_id][0]
        self.assertEqual((info.name, info.port), ("新名字", 9333))
        self.assertTrue(info.exe_exists)

        games.remove_game(game_id, root=self.profiles)
        self.assertFalse((self.profiles / game_id).exists())

    def test_set_exe_rejects_a_different_engine(self):
        exe = self.make("js/rmmz_core.js", "nw.dll")
        game_id, _ = games.add_game(exe, "RPG", root=self.profiles)
        other = self.make("UnityPlayer.dll", "Game_Data/", exe="Other.exe")
        with self.assertRaises(games.UnsupportedEngine):
            games.set_exe(game_id, other, root=self.profiles)

    def test_mark_run_shows_up_in_the_list(self):
        exe = self.make("js/rmmz_core.js", "nw.dll")
        game_id, _ = games.add_game(exe, "Run", root=self.profiles)
        self.assertIsNone(games.list_games(self.profiles)[0].last_run)
        games.mark_run(Profile.load(game_id, self.profiles))
        self.assertIsNotNone(games.list_games(self.profiles)[0].last_run)

    def test_tm_count_reads_the_index(self):
        exe = self.make("js/rmmz_core.js", "nw.dll")
        game_id, _ = games.add_game(exe, "Count", root=self.profiles)
        source = self.root / "tm.jsonl"
        source.write_text(
            '{"original": "a", "translation": "甲"}\n'
            '{"original": "b", "translation": "乙"}\n',
            encoding="utf-8")
        profile = Profile.load(game_id, self.profiles)
        build(source, profile.tm_db)
        self.assertEqual(games.list_games(self.profiles)[0].tm_count, 2)

    def test_new_games_inherit_the_machine_chain(self):
        exe = self.make("js/rmmz_core.js", "nw.dll")
        game_id, _ = games.add_game(exe, "Inherit", root=self.profiles)
        profile = Profile.load(game_id, self.profiles)
        self.assertIsNone(profile.backends)
        with mock.patch.object(settings, "ROOT", self.root):
            settings.save({"active": ["deepseek"], "backends": {}}, root=self.root)
            self.assertEqual(profile.backend_names(), ["deepseek"])


class StubEndpoint:
    """A tiny OpenAI-compatible server: /v1/models plus one chat completion."""

    def __init__(self):
        self.requests: list[tuple] = []
        self.system_prompts: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the test output clean
                pass

            def do_GET(self):
                owner.requests.append(("GET", self.path, self.headers.get("Authorization")))
                self._send({"data": [{"id": "model-a"}, {"id": "model-b"}]})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append(
                    ("POST", self.path, self.headers.get("Authorization"), body))
                for message in body.get("messages", []):
                    if message.get("role") == "system":
                        owner.system_prompts.append(message.get("content", ""))
                self._send({"choices": [{"message": {"content": '{"0": "你好"}'}}]})

            def _send(self, doc: dict):
                data = json.dumps(doc).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_defaults_when_no_file(self):
        data = settings.load(self.tmp)
        self.assertIn("local_llm", data["backends"])
        self.assertEqual(data["active"], settings.DEFAULT_ACTIVE)
        self.assertFalse((self.tmp / "backends.json").exists())

    def test_save_and_load_round_trip(self):
        data = settings.load(self.tmp)
        data["active"] = ["deepseek"]
        data["backends"]["deepseek"]["model"] = "deepseek-reasoner"
        settings.save(data, self.tmp)
        again = settings.load(self.tmp)
        self.assertEqual(again["active"], ["deepseek"])
        self.assertEqual(again["backends"]["deepseek"]["model"], "deepseek-reasoner")

    def test_fetch_models_and_probe_send_the_key(self):
        endpoint = StubEndpoint()
        self.addCleanup(endpoint.close)
        definition = {"base": endpoint.base, "apiKey": "sk-test"}
        self.assertEqual(settings.fetch_models(definition), ["model-a", "model-b"])
        ok, message = settings.probe(definition)
        self.assertTrue(ok)
        self.assertIn("2", message)
        self.assertEqual(endpoint.requests[0][2], "Bearer sk-test")

    def test_probe_reports_an_unreachable_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            import socket

            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            ok, message = settings.probe({"base": f"http://127.0.0.1:{port}/v1"}, timeout=0.5)
        self.assertFalse(ok)
        self.assertIn("连不上", message)


class TestBackendChain(unittest.TestCase):
    def setUp(self):
        self.endpoint = StubEndpoint()
        self.addCleanup(self.endpoint.close)
        self.log = __import__("logging").getLogger("test")

    def test_profile_overrides_machine_defaults(self):
        defs = {"bots": {"type": "openai_compatible", "base": self.endpoint.base,
                         "apiKey": "k1", "model": "model-a", "maxTokens": 100}}
        chain = build_chain(["bots"], {"bots": {"maxTokens": 200}}, self.log, defs=defs)
        self.assertEqual(len(chain), 1)
        backend = chain[0]
        self.assertEqual(backend.name, "bots")
        self.assertEqual(backend.max_tokens, 200)
        self.assertEqual(backend.model, "model-a")
        self.assertEqual(backend.api_key, "k1")

    def test_unknown_type_is_skipped(self):
        chain = build_chain(["weird"], {}, self.log, defs={"weird": {"type": "nope"}})
        self.assertEqual(chain, [])

    def test_glossary_terms_reach_the_prompt(self):
        backend = LocalLLMBackend({"base": self.endpoint.base, "model": "model-a"})
        backend.glossary = Glossary({"狐妖": "kitsune", "夜行": "nightwalk"})
        out = backend.translate([Request(key="k", text="狐妖夜行")])
        self.assertEqual(out, {"k": "你好"})
        prompt = self.endpoint.system_prompts[-1]
        self.assertIn("术语表", prompt)
        self.assertIn("狐妖 => kitsune", prompt)
        self.assertIn("夜行 => nightwalk", prompt)

    def test_glossary_without_a_match_leaves_the_prompt_alone(self):
        backend = LocalLLMBackend({"base": self.endpoint.base, "model": "model-a"})
        backend.glossary = Glossary({"狐妖": "kitsune"})
        backend.translate([Request(key="k", text="Nothing here")])
        self.assertNotIn("术语表", self.endpoint.system_prompts[-1])


class TestGlossaryFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.tmp / "glossary.tsv"

    def test_edit_preserves_comments_and_notes(self):
        self.path.write_text("# 人名表\n狐妖\t狐狸精\t注：旧译\n", encoding="utf-8")
        editor = GlossaryFile(self.path)
        self.assertEqual(editor.entries(), [("狐妖", "狐狸精")])
        editor.set("狐妖", "狐仙")
        editor.set("夜行", "夜间")
        editor.save()

        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 人名表"))
        self.assertIn("狐妖\t狐仙\t注：旧译", text)
        self.assertEqual(GlossaryFile(self.path).entries(),
                         [("狐妖", "狐仙"), ("夜行", "夜间")])

    def test_delete_removes_one_line_only(self):
        self.path.write_text("#c\na\t甲\nb\t乙\n", encoding="utf-8")
        editor = GlossaryFile(self.path)
        self.assertTrue(editor.delete("a"))
        self.assertFalse(editor.delete("zzz"))
        editor.save()
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("a\t甲", text)
        self.assertIn("b\t乙", text)
        self.assertIn("#c", text)


class TestStrategies(unittest.TestCase):
    def test_inference_from_old_profiles(self):
        raw = {"id": "p", "name": "p", "adapter": "rpgmaker_mv_mz", "backends": []}
        self.assertEqual(Profile.from_dict(raw, Path(".")).effective_strategy(), "memory")
        raw["backends"] = ["local_llm"]
        self.assertEqual(Profile.from_dict(raw, Path(".")).effective_strategy(), "tm_first")
        raw.pop("backends")
        self.assertEqual(Profile.from_dict(raw, Path(".")).effective_strategy(), "tm_first")

    def test_explicit_strategy_wins(self):
        raw = {"id": "p", "name": "p", "adapter": "rpgmaker_mv_mz",
               "backends": ["local_llm"], "strategy": "backend_only"}
        self.assertEqual(Profile.from_dict(raw, Path(".")).effective_strategy(), "backend_only")

    def test_set_strategy_writes_the_backends_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game_dir = root / "demo"
            game_dir.mkdir()
            (game_dir / "profile.json").write_text(json.dumps(
                {"id": "demo", "name": "Demo", "adapter": "rpgmaker_mv_mz"},
                ensure_ascii=False), encoding="utf-8")

            games.set_strategy("demo", "memory", root=root)
            raw = json.loads((game_dir / "profile.json").read_text(encoding="utf-8"))
            self.assertEqual((raw["strategy"], raw["backends"]), ("memory", []))

            games.set_strategy("demo", "tm_first", "deepseek", root=root)
            raw = json.loads((game_dir / "profile.json").read_text(encoding="utf-8"))
            self.assertEqual((raw["strategy"], raw["backends"]), ("tm_first", ["deepseek"]))

            games.set_strategy("demo", "tm_first", None, root=root)
            raw = json.loads((game_dir / "profile.json").read_text(encoding="utf-8"))
            self.assertNotIn("backends", raw)


class TestExchange(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_docx_round_trip_keeps_newline_marks(self):
        from gt.exchange import read_docx, write_docx

        path = self.tmp / "a.docx"
        write_docx(["一行", "上\n下"], path)
        self.assertEqual(read_docx(path), ["一行", "上 ↵ 下"])

    def test_export_then_import_reaches_the_memory(self):
        from gt.exchange import export_unmatched, import_translated

        rows = [
            {"id": "u1", "kind": "text", "where": "x", "raw": "体重\\V[1]kg",
             "text": "体重〔0〕kg", "refs": ["\\V[1]"]},
            {"id": "u2", "kind": "text", "where": "y", "raw": "上\\n下",
             "text": "第一行\n第二行", "refs": []},
        ]
        source = self.tmp / "unmatched.jsonl"
        source.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                          encoding="utf-8")
        export = export_unmatched(source, self.tmp / "export")
        self.assertEqual(export["count"], 2)
        self.assertTrue(export["docx"].exists())
        self.assertTrue(export["mapping"].exists())

        # Stand in for the translation service: same paragraphs, translated.
        back = self.tmp / "export" / "back.docx"
        from gt.exchange import write_docx

        write_docx(["体重〔0〕公斤", "第一行\n第二行"], back)
        db = self.tmp / "tm.sqlite"
        report = import_translated(export["mapping"], back, db)
        self.assertEqual(report["written"], 2)
        self.assertEqual(report["tm"]["added"], 2)

        memory = TranslationMemory(db)
        self.addCleanup(memory.close)
        self.assertEqual(memory.lookup("体重\\V[1]kg"), "体重\\V[1]公斤")
        self.assertEqual(memory.lookup("第一行 第二行"), "第一行\n第二行")

    def test_import_refuses_a_row_that_lost_its_reference(self):
        from gt.exchange import import_translated, write_docx

        rows = [{"id": "u1", "kind": "text", "where": "x", "raw": "体重\\V[1]kg",
                 "text": "体重〔0〕kg", "refs": ["\\V[1]"]}]
        mapping = self.tmp / "map.jsonl"
        mapping.write_text(json.dumps(rows[0], ensure_ascii=False), encoding="utf-8")
        back = self.tmp / "back.docx"
        write_docx(["体重公斤"], back)

        report = import_translated(mapping, back, self.tmp / "tm.sqlite")
        self.assertEqual(report["written"], 0)
        self.assertEqual(report["problems"][0]["issue"], "missing-ref 0")


class TestTmMerge(unittest.TestCase):
    def test_merge_keeps_existing_rows(self):
        from gt.tm import merge

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "a.jsonl"
            first.write_text('{"original": "one", "translation": "一"}\n', encoding="utf-8")
            db = root / "tm.sqlite"
            build(first, db)

            second = root / "b.jsonl"
            second.write_text(
                '{"original": "one", "translation": "壹"}\n'
                '{"original": "two", "translation": "二"}\n'
                '{"original": "three", "translation": ""}\n',
                encoding="utf-8")
            result = merge(second, db)
            self.assertEqual((result["added"], result["updated"], result["skipped"]), (1, 1, 1))

            memory = TranslationMemory(db)
            try:
                self.assertEqual(memory.lookup("one"), "壹")
                self.assertEqual(memory.lookup("two"), "二")
            finally:
                memory.close()


if __name__ == "__main__":
    unittest.main()
