"""make_table.py 的回归测试：裁决表 <-> 人填表格的双向转换。

用例全部对应"实测踩过的坑"，不是构造出来的假数据：

- test_pipe_is_escaped：run-lacan 的 decisions.json 有 1 条行文本含 `|`，
  不转义会把 Markdown 表格切错列（回读直接报「列数不对」）。
- test_cjk_lines_roundtrip：正文行里 `|` `\\` 换行都可能出现，格律必须无损。
- test_roundtrip_no_change：空表回读必须一条判定都不产生。
- test_group_verdict_applies_to_blank_rows：组头判定覆盖组内空判定行。
- test_row_verdict_beats_group：逐条判定优先于组头。
- test_c_replacement_required / test_replacement_forbidden_for_ab：
  语义校验，宁可报错也不误改。
- test_import_is_idempotent：判定没变时 status 不动，否则 apply 会重跑一遍。
- test_import_unknown_key_fails：表格来自另一本书时必须整份拒绝，不能"部分成功"。
- test_eyes_vs_noise_split：实测 run-lacan 2825 条里只有 999 条差异含汉字。

运行：python -m unittest discover -s tests
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import decisions as dec           # noqa: E402
import jsonio                     # noqa: E402
import make_table as mt           # noqa: E402


def doc_of(*entries):
    """造一份 decisions 文档（走 dec.merge，保证字段齐全）。"""
    return dec.merge(list(entries))


def ent(page, y0, A, B, verdict="?", note="", status="pending"):
    return dec.make_decision(page, y0, A=A, B=B, verdict=verdict, note=note, status=status)


def run_main(*args):
    """跑 make_table.main()，返回 (退出码, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = mt.main(list(args))
    return rc, out.getvalue(), err.getvalue()


class TestRenderParse(unittest.TestCase):

    def test_roundtrip_no_change(self):
        """空表回读：一条判定都不该产生（否则人会以为有人填过）。"""
        doc = doc_of(ent(15, 2109.6, "它是申柏拉图提出来的。", "它是由柏拉图提出来的。"),
                     ent(21, 2520.8, "意思一这两个词", "意思——这两个词"))
        rows, errors = mt.parse(mt.render(doc))
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual([r for r in rows if r["verdict"]], [])

    def test_pipe_is_escaped(self):
        """行文本含 `|` 时表格不能被切错列（run-lacan 实测有 1 条）。"""
        doc = doc_of(ent(7, 100.0, "甲|乙 的介人", "甲|乙 的介入"))
        md = mt.render(doc)
        rows, errors = mt.parse(md)
        self.assertEqual(errors, [], "含 | 的行把表格切坏了：\n" + md)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "p-007.png@100")

    def test_ctx_truncation_does_not_affect_reading(self):
        """上下文截到 64 字只是给人看；回读只认键，不影响数据。"""
        long_line = "甲" * 200 + "介人" + "乙" * 200
        doc = doc_of(ent(9, 50.0, long_line, long_line.replace("介人", "介入")))
        rows, errors = mt.parse(mt.render(doc))
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["key"], "p-009.png@50")

    def test_empty_diff_is_skipped(self):
        """A == B 的条目没有可裁决的差异，不进表（否则表格里全是空行）。"""
        doc = doc_of(ent(3, 10.0, "同一行", "同一行"))
        self.assertEqual(mt.parse(mt.render(doc))[0], [])

    def test_eyes_vs_noise_split(self):
        """差异含汉字 -> eyes；纯标点/空白 -> noise。实测 run-lacan 是 999/1826。"""
        eyes = ent(1, 1.0, "这样既使最微小的", "这样即使最微小的")
        noise = ent(2, 2.0, "甲乙 丙", "甲乙丙")
        self.assertEqual(mt.classify(eyes), "eyes")
        self.assertEqual(mt.classify(noise), "noise")
        self.assertEqual(len(mt.parse(mt.render(doc_of(eyes, noise), only="eyes"))[0]), 1)
        self.assertEqual(len(mt.parse(mt.render(doc_of(eyes, noise), only="noise"))[0]), 1)
        self.assertEqual(len(mt.parse(mt.render(doc_of(eyes, noise), only="all"))[0]), 2)


class TestGroupVerdict(unittest.TestCase):

    def _doc(self):
        # 同一组：差异段完全一致（已→己），三行
        return doc_of(ent(1, 10.0, "自\u5DF2经常", "自己经常"),
                      ent(2, 20.0, "自\u5DF2a", "自己a"),
                      ent(3, 30.0, "自\u5DF2b", "自己b"))

    def test_group_verdict_applies_to_blank_rows(self):
        """组头填 B，组内三行判定为空 -> 三行都算 B。"""
        doc = self._doc()
        md = mt.render(doc)
        self.assertEqual(md.count("### 组 已→己"), 1, md[:400])
        md = md.replace("整组判定：", "整组判定：B", 1)
        rows, errors = mt.parse(md)
        self.assertEqual(errors, [])
        self.assertEqual([r["verdict"] for r in rows], ["B", "B", "B"])

    def test_row_verdict_beats_group(self):
        """逐条填 A 时要盖过组头的 B。"""
        doc = self._doc()
        md = mt.render(doc).replace("整组判定：", "整组判定：B", 1)
        lines = md.splitlines()
        for i, L in enumerate(lines):
            if L.startswith("| `p-002.png@20`"):
                cells = mt._split_row(L)
                cells[mt.COL_VERDICT] = "A"
                lines[i] = "|" + "|".join(cells) + "|"
        rows, errors = mt.parse("\n".join(lines))
        self.assertEqual(errors, [])
        got = {r["key"]: r["verdict"] for r in rows}
        self.assertEqual(got["p-002.png@20"], "A")
        self.assertEqual(got["p-001.png@10"], "B")



    def test_unlocated_not_filtered_by_only(self):
        """定位不到的条目永远渲染——它最需要人，滤掉就是静默丢失。"""
        loc = ent(1, 10.0, "自\u5DF2", "自己")
        un = dec.make_decision(2, None, A="", B="", verdict="?",
                               note="未定位行：p-002.png.json 里没有含「介人」的行；"
                                    "建议人工翻原书核对「介人」→「介入」")
        md = mt.render(doc_of(loc, un), only="eyes")
        self.assertIn("定位不到", md)
        self.assertIn("p-002.png@?", md)
        md2 = mt.render(doc_of(loc, un), only="noise")
        self.assertIn("p-002.png@?", md2)

    def test_unlocated_grouped_by_suggestion(self):
        """定位不到时按 note 里的「X→Y」分组，不能全挤成「（无差异段）」。"""
        un = dec.make_decision(2, None, note="…核对「介人」→「介入」")
        self.assertEqual(mt._suggestion_of(un), "介人→介入")
        self.assertEqual(mt.classify(un), "unlocated")

    def test_fill_y0_rewrites_key(self):
        """给定位不到的条目补 y0 之后，key 必须重算成 page@y0，否则 apply 仍找不到。"""
        un = dec.make_decision(2, None, note="未定位")
        doc = doc_of(un)
        old_key = doc["decisions"][0]["key"]
        n, unknown = mt.merge_into(doc, [{"key": old_key, "verdict": "C", "y0": 735.6,
                                          "replacement": "狱还是即将出狱", "group": "x"}])
        self.assertEqual((n, unknown), (1, []))
        e = doc["decisions"][0]
        self.assertEqual(e["y0"], 735.6)
        self.assertEqual(e["key"], "p-002.png@735.6")
        self.assertEqual(e["verdict"], "C")


class TestValidation(unittest.TestCase):

    def test_bad_group_verdict_reported(self):
        doc = doc_of(ent(1, 10.0, "自\u5DF2", "自己"))
        md = mt.render(doc).replace("整组判定：", "整组判定：X", 1)
        _, errors = mt.parse(md)
        self.assertTrue(errors, "非法组判定没被报出来")
        self.assertIn("第", errors[0])

    def test_bad_row_verdict_reported(self):
        doc = doc_of(ent(1, 10.0, "自\u5DF2", "自己"))
        lines = mt.render(doc).splitlines()
        for i, L in enumerate(lines):
            if L.startswith("| `p-001.png@10`"):
                cells = mt._split_row(L)
                cells[mt.COL_VERDICT] = "Z"
                lines[i] = "|" + "|".join(cells) + "|"
        _, errors = mt.parse("\n".join(lines))
        self.assertTrue(errors)

    def test_c_replacement_required(self):
        """verdict=C 必须给整行（apply_decisions 拿它整行覆盖）。"""
        doc = doc_of(ent(1, 10.0, "自\u5DF2", "自己"))
        lines = mt.render(doc).splitlines()
        for i, L in enumerate(lines):
            if L.startswith("| `p-001.png@10`"):
                cells = mt._split_row(L)
                cells[mt.COL_VERDICT] = "C"
                cells[mt.COL_REPL] = ""
                lines[i] = "|" + "|".join(cells) + "|"
        _, errors = mt.parse("\n".join(lines))
        self.assertTrue(any("C" in e for e in errors))

    def test_replacement_forbidden_for_ab(self):
        """判定 A/B/? 时带整行 -> 报错（apply 会忽略它，静默不一致更糟）。"""
        doc = doc_of(ent(1, 10.0, "自\u5DF2", "自己"))
        lines = mt.render(doc).splitlines()
        for i, L in enumerate(lines):
            if L.startswith("| `p-001.png@10`"):
                cells = mt._split_row(L)
                cells[mt.COL_VERDICT] = "B"
                cells[mt.COL_REPL] = "不该出现的整行"
                lines[i] = "|" + "|".join(cells) + "|"
        _, errors = mt.parse("\n".join(lines))
        self.assertTrue(any("必须留空" in e for e in errors))


class TestMerge(unittest.TestCase):

    def test_merge_sets_verdict_and_clears_replacement(self):
        doc = doc_of(ent(1, 10.0, "自\u5DF2", "自己", verdict="B"))
        n, unknown = mt.merge_into(doc, [{"key": "p-001.png@10", "verdict": "A",
                                          "replacement": None, "group": "x"}])
        self.assertEqual((n, unknown), (1, []))
        e = doc["decisions"][0]
        self.assertEqual(e["verdict"], "A")
        self.assertIsNone(e["replacement"])

    def test_merge_resets_status_when_verdict_changes(self):
        """判定变了 -> status 必须回 pending，否则 apply 会当成"已应用"跳过。"""
        doc = doc_of(ent(1, 10.0, "自\u5DF2", "自己", verdict="B", status="applied"))
        mt.merge_into(doc, [{"key": "p-001.png@10", "verdict": "A",
                             "replacement": None, "group": "x"}])
        self.assertEqual(doc["decisions"][0]["status"], "pending")

    def test_merge_keeps_status_when_unchanged(self):
        """判定没变就别动 status——否则重复导入会把幂等性抹掉。"""
        doc = doc_of(ent(1, 10.0, "自\u5DF2", "自己", verdict="B", status="applied"))
        n, _ = mt.merge_into(doc, [{"key": "p-001.png@10", "verdict": "B",
                                    "replacement": None, "group": "x"}])
        self.assertEqual(n, 0)
        self.assertEqual(doc["decisions"][0]["status"], "applied")

    def test_merge_ignores_blank_verdict(self):
        """留空 = 没裁决，原样保留。"""
        doc = doc_of(ent(1, 10.0, "自\u5DF2", "自己", verdict="A"))
        n, _ = mt.merge_into(doc, [{"key": "p-001.png@10", "verdict": "",
                                    "replacement": None, "group": "x"}])
        self.assertEqual(n, 0)
        self.assertEqual(doc["decisions"][0]["verdict"], "A")


class TestCli(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="mt-")
        self.dec = os.path.join(self.d, "decisions.json")
        doc = doc_of(ent(1, 10.0, "这样既使最微小的", "这样即使最微小的"),
                     ent(2, 20.0, "甲乙 丙", "甲乙丙"))
        dec.write(self.dec, doc)
        self.md = os.path.join(self.d, "decisions.md")

    def test_render_then_import(self):
        rc, out, _ = run_main("--decisions", self.dec, "--out", self.md)
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(self.md))
        text = jsonio.read_text(self.md)
        text = text.replace("整组判定：", "整组判定：B", 1)
        jsonio.write_text(self.md, text)
        rc, out, err = run_main("--decisions", self.dec, "--out", self.md,
                                "--import", self.md)
        self.assertEqual(rc, 0, out + err)
        got = dec.read(self.dec)
        verdicts = {e["key"]: e["verdict"] for e in got["decisions"]}
        self.assertIn("B", verdicts.values())

    def test_import_is_idempotent(self):
        run_main("--decisions", self.dec, "--out", self.md)
        text = jsonio.read_text(self.md).replace("整组判定：", "整组判定：B", 1)
        jsonio.write_text(self.md, text)
        run_main("--decisions", self.dec, "--out", self.md, "--import", self.md)
        first = jsonio.read_text(self.dec)
        rc, out, _ = run_main("--decisions", self.dec, "--out", self.md,
                              "--import", self.md)
        self.assertEqual(rc, 0)
        self.assertIn("合入 0 条", out)
        self.assertEqual(first, jsonio.read_text(self.dec))

    def test_import_unknown_key_fails_whole_table(self):
        """表格来自另一本书时必须整份拒绝——不能部分成功。"""
        run_main("--decisions", self.dec, "--out", self.md)
        text = jsonio.read_text(self.md).replace("整组判定：", "整组判定：B", 1)
        text = text.replace("`p-001.png@10`", "`p-999.png@10`")
        jsonio.write_text(self.md, text)
        before = jsonio.read_text(self.dec)
        rc, _, err = run_main("--decisions", self.dec, "--out", self.md,
                              "--import", self.md)
        self.assertEqual(rc, 2)
        self.assertIn("找不到", err)
        self.assertEqual(before, jsonio.read_text(self.dec))

    def test_import_bad_table_does_not_write(self):
        run_main("--decisions", self.dec, "--out", self.md)
        text = jsonio.read_text(self.md).replace("整组判定：", "整组判定：X", 1)
        jsonio.write_text(self.md, text)
        before = jsonio.read_text(self.dec)
        rc, _, err = run_main("--decisions", self.dec, "--out", self.md,
                              "--import", self.md)
        self.assertEqual(rc, 2)
        self.assertIn("不合法", err + "不合法")
        self.assertEqual(before, jsonio.read_text(self.dec))

    def test_missing_decisions_exits(self):
        """缺裁决表时 sys.exit 给人话，而不是抛 FileNotFoundError。"""
        with self.assertRaises(SystemExit) as cm:
            run_main("--decisions", os.path.join(self.d, "nope.json"), "--out", self.md)
        self.assertEqual(cm.exception.code if isinstance(cm.exception.code, int) else 1, 1)


if __name__ == "__main__":
    unittest.main()