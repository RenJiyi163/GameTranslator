"""Packaging and update tests: no game, no network.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gt import pack, update
from gt.tm import build


def make_profile(root: Path, game_id: str = "demo") -> Path:
    folder = root / game_id
    folder.mkdir(parents=True)
    (folder / "profile.json").write_text(json.dumps({
        "id": game_id,
        "name": "示例游戏",
        "adapter": "rpgmaker_mv_mz",
        "adapterConfig": {"exe": "D:/Games/demo/Game.exe", "port": 9227},
    }, ensure_ascii=False), encoding="utf-8")
    (folder / "glossary.tsv").write_text("魔王\tDemon Lord\n", encoding="utf-8")
    source = folder / "tm.jsonl"
    source.write_text(
        '{"original": "hello", "translation": "你好"}\n'
        '{"original": "bye", "translation": "再见"}\n', encoding="utf-8")
    build(source, folder / "tm.sqlite")
    return folder


def rows(db: Path) -> dict[str, str]:
    conn = sqlite3.connect(db)
    try:
        return dict(conn.execute("SELECT key, translation FROM tm").fetchall())
    finally:
        conn.close()


class PackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "profiles"
        self.root.mkdir()
        self.patches = [
            mock.patch.object(pack, "PROFILE_DIR", self.root),
            mock.patch("gt.config.PROFILE_DIR", self.root),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in self.patches:
            patcher.stop()
        self.tmp.cleanup()

    def test_round_trip(self) -> None:
        make_profile(self.root)
        out = Path(self.tmp.name) / "demo.gtpack"
        result = pack.export_pack("demo", out)
        self.assertEqual(result["counts"]["tm"], 2)
        self.assertEqual(result["counts"]["glossary"], 1)
        self.assertTrue(out.exists())

        manifest = pack.read_manifest(out)
        self.assertEqual(manifest["id"], "demo")
        self.assertIn("tm.sqlite", manifest["files"])
        self.assertEqual(len(manifest["files"]["profile.json"]["sha256"]), 64)

        # Wipe the local copy, then restore it from the pack alone.
        for item in (self.root / "demo").iterdir():
            item.unlink()
        (self.root / "demo").rmdir()

        back = pack.import_pack(out)
        self.assertEqual(back["profile"], "created")
        self.assertEqual(back["tm"]["added"], 2)
        self.assertEqual(rows(self.root / "demo" / "tm.sqlite"),
                         {"hello": "你好", "bye": "再见"})
        self.assertEqual((self.root / "demo" / "glossary.tsv").read_text(encoding="utf-8"),
                         "魔王\tDemon Lord\n")

    def test_import_merges_without_losing_local_rows(self) -> None:
        make_profile(self.root)
        out = Path(self.tmp.name) / "demo.gtpack"
        pack.export_pack("demo", out)
        local = self.root / "demo" / "tm.sqlite"
        conn = sqlite3.connect(local)
        try:
            conn.execute("INSERT INTO tm VALUES ('local-key', 'local', '本地新增', NULL, NULL)")
            conn.commit()
        finally:
            conn.close()

        result = pack.import_pack(out)
        self.assertEqual(result["profile"], "kept")
        merged = rows(local)
        self.assertIn("local-key", merged)
        self.assertEqual(merged["hello"], "你好")

    def test_rejects_a_file_that_is_not_a_pack(self) -> None:
        fake = Path(self.tmp.name) / "not-a-pack.gtpack"
        fake.write_bytes(b"hello")
        with self.assertRaises(pack.PackError):
            pack.read_manifest(fake)

    def test_manifest_lists_every_extracted_file(self) -> None:
        make_profile(self.root)
        out = Path(self.tmp.name) / "demo.gtpack"
        pack.export_pack("demo", out)
        manifest = pack.read_manifest(out)
        self.assertEqual(set(manifest["files"]),
                         {"profile.json", "glossary.tsv", "tm.sqlite"})


class UpdateTests(unittest.TestCase):
    def test_version_order(self) -> None:
        self.assertTrue(update.is_newer("0.2.0", "0.1.0"))
        self.assertFalse(update.is_newer("0.1.0", "0.1.0"))
        self.assertFalse(update.is_newer("0.1.0", "0.2.0"))
        self.assertTrue(update.is_newer("0.10.0", "0.9.9"))
        self.assertTrue(update.is_newer("v1.0", "0.9"))
        self.assertFalse(update.is_newer("1.0.0", "1.0"))

    def test_check_without_a_url_reports_why(self) -> None:
        with mock.patch.object(update, "load_config", return_value={"manifestUrl": ""}):
            result = update.check()
        self.assertFalse(result["ok"])
        self.assertIn("更新地址", result["reason"])

    def test_check_flags_an_older_manifest_as_current(self) -> None:
        manifest = {"version": "0.1.0", "url": "https://example.invalid/gt.exe"}
        with mock.patch.object(update, "load_config", return_value={"manifestUrl": "x"}), \
                mock.patch.object(update, "_fetch_json", return_value=manifest), \
                mock.patch.object(update, "save_config"):
            result = update.check()
        self.assertTrue(result["ok"])
        self.assertFalse(result["available"])


def load_installer():
    """The installer is a standalone script, not part of the package."""
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "installer" / "setup_main.py"
    spec = importlib.util.spec_from_file_location("gt_setup_main", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallerTests(unittest.TestCase):
    def test_uninstall_script_is_written_with_the_install_path(self) -> None:
        setup = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "Programs" / "GameTranslator"
            target.mkdir(parents=True)
            setup.write_uninstaller(target)
            script = (target / "uninstall.ps1").read_text(encoding="utf-8-sig")
            self.assertIn(str(target), script)
            self.assertNotIn("%INSTALL_DIR%", script)
            self.assertIn("Uninstall\\GameTranslator", script)
            # cmd.exe reads the batch as CP936 on this machine.
            (target / "uninstall.cmd").read_bytes().decode("ascii")

    def test_silent_target_reads_the_d_flag(self) -> None:
        setup = load_installer()
        self.assertEqual(setup.silent_target(["/S", "/D=C:\\Apps\\GT"]), Path("C:\\Apps\\GT"))
        self.assertEqual(setup.silent_target(["/s"]), setup.default_dir())


if __name__ == "__main__":
    unittest.main()
