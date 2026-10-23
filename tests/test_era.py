"""Emuera script parsing and rewriting."""

from __future__ import annotations

import codecs
import tempfile
import unittest
from pathlib import Path

from gt import era


class EraExtractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "ERB").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, body: str, encoding: str = "utf-8", bom: bool = True) -> Path:
        path = self.root / "ERB" / name
        data = body.encode(encoding)
        if bom and encoding == "utf-8":
            data = codecs.BOM_UTF8 + data
        path.write_bytes(data)
        return path

    def texts(self) -> list[str]:
        return [s.text for s in era.extract(self.root)]

    def test_plain_print_line(self):
        self.write("A.ERB", "PRINTFORML おはようございます\r\n")
        self.assertEqual(self.texts(), ["おはようございます"])

    def test_interpolation_stays_in_one_span(self):
        # 切碎会让「の」这类片段单独成句，翻出来的东西拼不回去
        self.write("A.ERB", "PRINTFORML %CALLNAME%に会いに行く\r\n")
        self.assertEqual(self.texts(), ["%CALLNAME%に会いに行く"])

    def test_braced_placeholders_stay_in_the_span(self):
        self.write("A.ERB", "PRINTFORMW {対象}に{部位}を使う\r\n")
        self.assertEqual(self.texts(), ["{対象}に{部位}を使う"])

    def test_chinese_lines_are_left_alone(self):
        self.write("A.ERB", "PRINTFORML 今天天气很好\r\n")
        self.assertEqual(self.texts(), [])

    def test_translated_lines_with_bullets_are_left_alone(self):
        # ・ 与 ー 在假名区段里，但译文里到处是它们，不该把译文当原文再送一遍
        self.write("A.ERB",
                   "PRINTFORML ・即使测试出错，也可能只是环境不合适。\r\n"
                   "PRINTFORML 获得了技能“对空指挥・主力”。\r\n")
        self.assertEqual(self.texts(), [])

    def test_buttons_keep_their_quotes(self):
        self.write("A.ERB", 'PRINTBUTTON "[返回]", 999\r\nPRINTBUTTON @"よろしいですか、%X%", 1\r\n')
        self.assertEqual(self.texts(), ["[返回]", "よろしいですか、%X%"])

    def test_printdata_block(self):
        self.write("A.ERB",
                   "PRINTDATA\r\n"
                   "\tこんにちは\r\n"
                   "\tさようなら\r\n"
                   "ENDDATA\r\n"
                   "PRINTFORML 外の行\r\n")
        self.assertEqual(self.texts(), ["こんにちは", "さようなら", "外の行"])

    def test_dataform_wrapper_is_syntax_not_text(self):
        body = 'PRINTDATA\n\tDATAFORM しゅうまい\n\tDATAFORM ぎょうざ\nENDDATA\n'
        self.write("A.ERB", body)
        self.assertEqual(self.texts(), ["しゅうまい", "ぎょうざ"])
    def test_indentation_is_not_part_of_the_span(self):
        self.write("A.ERB", "\tPRINTFORML \t　これは本文\r\n")
        spans = era.extract(self.root)
        self.assertEqual(spans[0].text, "これは本文")

    def test_cp932_script(self):
        self.write("B.ERB", "PRINTL こんばんは\r\n", encoding="cp932", bom=False)
        self.assertEqual(self.texts(), ["こんばんは"])

    def test_escaped_newline_stays_in_the_text(self):
        # 汉字行在日文脚本里也要提；文件整体的假名比例决定这一点
        self.write("A.ERB",
                   "PRINTFORML そうですね\r\n"
                   "PRINTFORML 一行目\\n二行目\r\n")
        self.assertEqual(self.texts(), ["そうですね", "一行目\\n二行目"])


class EraProtectTest(unittest.TestCase):
    def test_masking_and_restoring(self):
        source = r"今日は%CLOTH_PART_NAME:LOCAL, 12, LEFT%の{GOLD}円\n"
        masked, tokens = era.protect(source)
        self.assertNotIn("%", masked)
        self.assertNotIn("{", masked)
        self.assertEqual(len(tokens), 3)
        self.assertEqual(era.restore(masked, tokens), source)

    def test_missing_placeholder_is_reported(self):
        self.assertTrue(era.unprotected_problems("A%X%B", "甲乙"))
        self.assertFalse(era.unprotected_problems("A%X%B", "甲%X%乙"))


class EraAcceptTest(unittest.TestCase):
    def test_placeholders_may_be_reordered(self):
        # 中文语序会把两个名字换位；名字查询不受求值顺序影响
        source = "%ANAME(MASTER)%は%ANAME(対象)%を抱いた"
        self.assertEqual(era._accept(source, "⟦1⟧被⟦0⟧抱住了"),
                         "%ANAME(対象)%被%ANAME(MASTER)%抱住了")

    def test_order_sensitive_placeholders_stay_in_order(self):
        source = "%RAND:6%と%ANAME(対象)%"
        self.assertIsNone(era._accept(source, "⟦1⟧和⟦0⟧"))

    def test_dropped_placeholder_is_rejected(self):
        self.assertIsNone(era._accept("A%X%B%Y%C", "甲⟦0⟧乙"))

    def test_unchanged_answer_is_rejected(self):
        self.assertIsNone(era._accept("A%X%B", "A⟦0⟧B"))


class EraCsvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "CSV").mkdir()
        (self.root / "CSV" / "Chara" / "Chara").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, body: str) -> Path:
        path = self.root / "CSV" / name
        path.write_bytes(codecs.BOM_UTF8 + body.encode("utf-8"))
        return path

    def texts(self) -> list[str]:
        return [s.text for s in era.extract(self.root)]

    def test_definition_name_is_translated_but_not_the_key(self):
        self.write("Train.csv", ";コメント行\r\n0,おっぱい愛撫\r\n1,胸を愛撫する\r\n")
        self.assertEqual(self.texts(), ["おっぱい愛撫", "胸を愛撫する"])

    def test_gamebase_only_takes_the_display_keys(self):
        self.write("GameBase.csv", "コード,4022264\r\nタイトル,eraAL K\r\n作者,テスト作者\r\n")
        self.assertEqual(self.texts(), ["テスト作者"])

    def test_chara_names_and_explanations_only(self):
        body = ("番号,5301\n"
                "名前,ルルマルス\n"
                "呼び名,ルルマルス\n"
                "CSTR,名字,ラドロア級\n"
                "CSTR,所属,ヴィシア\n"
                "CSTR,ＳＬＧ解説_0,見習い騎士だ。\n")
        self.write("Chara/Chara/Chara5301 ルルマルス.csv", body)
        self.assertEqual(self.texts(), ["ルルマルス", "ルルマルス", "ラドロア級", "見習い騎士だ。"])

    def test_apply_rewrites_only_the_value_cell(self):
        path = self.write("Train.csv", "0,おっぱい愛撫\r\n1,胸を愛撫する\r\n")
        stats = era.apply_translations(self.root, {"おっぱい愛撫": "胸部爱抚"})
        self.assertEqual(stats["spans"], 1)
        self.assertEqual(path.read_bytes().decode("utf-8-sig"),
                         "0,胸部爱抚\r\n1,胸を愛撫する\r\n")

    def test_numbers_are_not_offered(self):
        self.write("_Replace.csv", "PALAMLVの境界値 , 0/100/500\r\n発起人表示 , 巡視中だよ\r\n")
        self.assertEqual(self.texts(), ["巡視中だよ"])


class EraApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "ERB").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_rewrites_only_translated_spans(self):
        path = self.root / "ERB" / "A.ERB"
        body = "\tPRINTFORML おはよう\r\n\tPRINTFORML さようなら\r\n"
        path.write_bytes(codecs.BOM_UTF8 + body.encode("utf-8"))
        stats = era.apply_translations(self.root, {"おはよう": "早上好"})
        self.assertEqual(stats["spans"], 1)
        out = path.read_bytes()
        self.assertTrue(out.startswith(codecs.BOM_UTF8))
        self.assertEqual(out.decode("utf-8-sig"), "\tPRINTFORML 早上好\r\n\tPRINTFORML さようなら\r\n")

    def test_cp932_file_becomes_utf8_when_the_translation_needs_it(self):
        path = self.root / "ERB" / "B.ERB"
        path.write_bytes("PRINTL こんばんは\r\n".encode("cp932"))
        stats = era.apply_translations(self.root, {"こんばんは": "晚上好"})
        self.assertEqual(stats["converted"], 1)
        self.assertTrue(path.read_bytes().startswith(codecs.BOM_UTF8))
        self.assertEqual(path.read_bytes().decode("utf-8-sig"), "PRINTL 晚上好\r\n")

    def test_interpolation_survives_the_rewrite(self):
        path = self.root / "ERB" / "C.ERB"
        path.write_bytes("%s に会いに行く\r\n".replace("%s", "PRINTFORMW").encode("utf-8"))
        era.apply_translations(self.root, {"に会いに行く": "去见"})
        self.assertIn("PRINTFORMW 去见", path.read_text(encoding="utf-8"))

    def test_dry_run_touches_nothing(self):
        path = self.root / "ERB" / "D.ERB"
        body = b"PRINTL \x82\xb1\x82\xf1\x82\xc9\x82\xbf\x82\xcd\r\n"
        path.write_bytes(body)
        stats = era.apply_translations(self.root, {"こんにちは": "你好"}, dry_run=True)
        self.assertEqual(stats["spans"], 1)
        self.assertEqual(path.read_bytes(), body)

    def test_untranslated_files_are_not_rewritten(self):
        path = self.root / "ERB" / "E.ERB"
        path.write_bytes(b"PRINTL \x82\xb1\x82\xf1\x82\xc9\x82\xbf\x82\xcd\r\n")
        before = path.stat().st_mtime_ns
        stats = era.apply_translations(self.root, {"別の文": "别的"})
        self.assertEqual(stats["files"], 0)
        self.assertEqual(path.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
