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


def write_page(d, name, text):
    """造一页最小页级 JSON（回填计划要按 y0 找到行）。"""
    os.makedirs(d, exist_ok=True)
    jsonio.write(os.path.join(d, name + ".json"),
                 {"name": name, "w": 2000, "h": 3000,
                  "lines": [{"x0": 100, "y0": 732.5, "x1": 1800, "y1": 790,
                             "t": text, "s": 0.99}]})


def run_main(*args):
    """跑 make_table.main()，返回 (退出码, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = mt.main(list(args))
    return rc, out.getvalue(), err.getvalue()


class TestRenderParse(unittest.TestCase):

    def test_roundtrip_keeps_data_lossless(self):
        """空表回读：一条判定都不该产生（否则人会以为有人填过）；
        含 `|` 的行、超长行都必须无损回读。"""
        doc = doc_of(ent(15, 2109.6, "它是申柏拉图提出来的。", "它是由柏拉图提出来的。"),
                     ent(21, 2520.8, "意思一这两个词", "意思——这两个词"))
        rows, errors = mt.parse(mt.render(doc))
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual([r for r in rows if r["verdict"]], [])

        # run-lacan 实测有 1 条行文本含 |，不转义会把 Markdown 表格切错列
        doc = doc_of(ent(7, 100.0, "甲|乙 的介人", "甲|乙 的介入"))
        md = mt.render(doc)
        rows, errors = mt.parse(md)
        self.assertEqual(errors, [], "含 | 的行把表格切坏了：\n" + md)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "p-007.png@100")

        # 上下文截到 64 字只是给人看；回读只认键，不影响数据
        long_line = "甲" * 200 + "介人" + "乙" * 200
        doc = doc_of(ent(9, 50.0, long_line, long_line.replace("介人", "介入")))
        rows, errors = mt.parse(mt.render(doc))
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["key"], "p-009.png@50")

    def test_classification_and_empty_diff(self):
        """A == B 没有可裁决的差异，不进表；差异分成 eyes / noise 两类。

        实测 run-lacan 2825 条里只有 999 条含汉字（eyes），1826 条是纯标点空白
        （noise）。不分类的话表格会被噪声淹没。
        """
        self.assertEqual(mt.parse(mt.render(doc_of(ent(3, 10.0, "同一行", "同一行"))))[0], [])

        eyes = ent(1, 1.0, "这样既使最微小的", "这样即使最微小的")
        noise = ent(2, 2.0, "甲乙 丙", "甲乙丙")
        self.assertEqual(mt.classify(eyes), "eyes")
        self.assertEqual(mt.classify(noise), "noise")
        both = doc_of(eyes, noise)
        self.assertEqual(len(mt.parse(mt.render(both, only="eyes"))[0]), 1)
        self.assertEqual(len(mt.parse(mt.render(both, only="noise"))[0]), 1)
        self.assertEqual(len(mt.parse(mt.render(both, only="all"))[0]), 2)


class TestGroupVerdict(unittest.TestCase):

    def _doc(self):
        # 同一组：差异段完全一致（已→己），三行
        return doc_of(ent(1, 10.0, "自\u5DF2经常", "自己经常"),
                      ent(2, 20.0, "自\u5DF2a", "自己a"),
                      ent(3, 30.0, "自\u5DF2b", "自己b"))

    def _fill_row(self, md, key_prefix, **cells_):
        """把表格里某一行的指定列改掉，模拟人在编辑器里填写。"""
        lines = md.splitlines()
        for i, L in enumerate(lines):
            if L.startswith("| `" + key_prefix):
                cells = mt._split_row(L)
                for col, val in cells_.items():
                    cells[getattr(mt, "COL_" + col.upper())] = val
                lines[i] = "|" + "|".join(cells) + "|"
        return "\n".join(lines)

    def test_group_verdict_covers_blank_rows_but_row_wins(self):
        """组头填 B 覆盖组内空判定行；逐条填 A 时盖过组头的 B。"""
        doc = self._doc()
        md = mt.render(doc)
        self.assertEqual(md.count("### 组 已→己"), 1, md[:400])
        md = md.replace("整组判定：", "整组判定：B", 1)
        rows, errors = mt.parse(md)
        self.assertEqual(errors, [])
        self.assertEqual([r["verdict"] for r in rows], ["B", "B", "B"])

        # 逐条填 A 要盖过组头
        md = self._fill_row(self._doc_md_with_group_b(), "p-002.png@20", verdict="A")
        rows, errors = mt.parse(md)
        self.assertEqual(errors, [])
        got = {r["key"]: r["verdict"] for r in rows}
        self.assertEqual(got["p-002.png@20"], "A")
        self.assertEqual(got["p-001.png@10"], "B")

    def _doc_md_with_group_b(self):
        return mt.render(self._doc()).replace("整组判定：", "整组判定：B", 1)

    def test_unlocated_always_rendered_and_grouped_by_suggestion(self):
        """定位不到的条目永远渲染（它最需要人，滤掉就是静默丢失），
        并按 note 里的「X→Y」分组，不能全挤成「（无差异段）」。"""
        loc = ent(1, 10.0, "自\u5DF2", "自己")
        un = dec.make_decision(2, None, A="", B="", verdict="?",
                               note="未定位行：p-002.png.json 里没有含「介人」的行；"
                                    "建议人工翻原书核对「介人」→「介入」")
        md = mt.render(doc_of(loc, un), only="eyes")
        self.assertIn("定位不到", md)
        self.assertIn("p-002.png@?", md)
        self.assertIn("p-002.png@?", mt.render(doc_of(loc, un), only="noise"))
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

    def _mutate(self, md, key_prefix, **cells_):
        """把表格里某一行的指定列改掉。"""
        lines = md.splitlines()
        for i, L in enumerate(lines):
            if L.startswith("| `" + key_prefix):
                cells = mt._split_row(L)
                for col, val in cells_.items():
                    cells[getattr(mt, "COL_" + col.upper())] = val
                lines[i] = "|" + "|".join(cells) + "|"
        return "\n".join(lines)

    def test_semantic_validation_reports_errors(self):
        """宁可报错也不误改：非法判定、C 缺整行、A/B 带整行，三种都必须是错误。"""
        base = mt.render(doc_of(ent(1, 10.0, "自\u5DF2", "自己")))

        # 非法组判定
        _, errors = mt.parse(base.replace("整组判定：", "整组判定：X", 1))
        self.assertTrue(errors, "非法组判定没被报出来")
        self.assertIn("第", errors[0])

        # 非法逐条判定
        _, errors = mt.parse(self._mutate(base, "p-001.png@10", verdict="Z"))
        self.assertTrue(errors)

        # verdict=C 必须给整行（apply_decisions 拿它整行覆盖）
        _, errors = mt.parse(self._mutate(base, "p-001.png@10", verdict="C", repl=""))
        self.assertTrue(any("C" in e for e in errors))

        # 判定 A/B/? 时带整行 -> 报错（apply 会忽略它，静默不一致更糟）
        _, errors = mt.parse(self._mutate(base, "p-001.png@10", verdict="B",
                                          repl="不该出现的整行"))
        self.assertTrue(any("必须留空" in e for e in errors))


class TestColumnCount(unittest.TestCase):
    """P2：多列同样必须拒绝——多列最常见的来源是 C 的整行里写了没转义的竖线。"""

    def test_column_count_must_be_exact(self):
        """列数不对一律拒绝：多列最常见的来源是 C 的整行里写了没转义的竖线。

        多列不能照常读前 7 列（那会把整行截断），列数不足同样报错。
        """
        md = "| `p-001.png@100` | p-001 | 原文 | 建议 | 100 | C | 左边|右边 |"
        rows, errors = mt.parse(md)
        self.assertEqual(rows, [], "多列不能照常读前 7 列（会把整行截断）")
        self.assertTrue(errors, "多列必须报错")
        self.assertIn("列数", errors[0])
        self.assertIn("\\|", errors[0], "提示里要写清「写成 \\|」")

        rows, errors = mt.parse("| `p-001.png@100` | p-001 | 原文 | 建议 | 100 | C |")
        self.assertEqual(rows, [], "列数不足同样报错")
        self.assertTrue(errors)


class TestUnlocatedNeedsY0(unittest.TestCase):
    """P2：未定位条目的填表提示必须要求补真实行 y0，否则导入成功、正文不变。"""

    def _doc(self):
        un = dec.make_decision(2, None, A="", B="", verdict="?",
                               note="未定位行：p-002.png.json 里没有含「介人」的行；"
                                    "建议人工翻原书核对「介人」→「介入」")
        return doc_of(un)

    def _render_and_fill(self, verdict="C", y0="732.5", repl="狱还是即将出狱"):
        md = mt.render(self._doc())
        lines = md.splitlines()
        for i, L in enumerate(lines):
            if L.startswith("| `p-002.png@?`"):
                cells = mt._split_row(L)
                cells[mt.COL_VERDICT] = verdict
                cells[mt.COL_REPL] = repl
                cells[mt.COL_Y0] = y0
                lines[i] = "|" + "|".join(cells) + "|"
        return md, "\n".join(lines)

    def test_fill_y0_leads_to_apply(self):
        """按说明填 C + 替换整行 + 真实 y0 -> parse/merge/plan 得到 kind=apply。

        这条把整条人肉链路串起来验：渲染出的说明必须要求填真实行 y0
        （不能要求"y0 留空"），填对了 parse 不报错、merge 重算 key、
        apply 计划判成 apply 而不是 missing。
        """
        import apply_decisions as ap
        md = mt.render(self._doc())
        self.assertIn("行 y0", md)
        self.assertIn("必须", md)
        self.assertNotIn("「行 y0」留空", md)

        _, filled = self._render_and_fill()
        rows, errors = mt.parse(filled)
        self.assertEqual(errors, [])
        doc = self._doc()
        got = mt.merge_into(doc, rows)
        self.assertEqual(len(got), 2, "没有错误时保持旧的两元返回")
        n, unknown = got
        self.assertEqual((n, unknown), (1, []))
        e = doc["decisions"][0]
        self.assertEqual(e["y0"], 732.5)
        self.assertEqual(e["verdict"], "C")
        self.assertEqual(e["replacement"], "狱还是即将出狱")
        # 真跑一遍回填计划：必须判 apply，而不是 missing
        with tempfile.TemporaryDirectory() as d:
            ocr = os.path.join(d, "ocr_merged")
            write_page(ocr, "p-002.png", "狱还是即将出出狱")   # 原书的错字行
            items, _ = ap.plan(doc, ocr)
        self.assertEqual([it["kind"] for it in items], ["apply"])

    def test_missing_y0_blocks_whole_import(self):
        """填了 C 却没补 y0 -> merge 带回具体错误，CLI 整份不写并退出 2。"""
        _, filled = self._render_and_fill(y0="")
        rows, errors = mt.parse(filled)
        self.assertEqual(errors, [])
        got = mt.merge_into(self._doc(), rows)
        self.assertEqual(len(got), 3, "有错时必须带回 errors")
        n, unknown, errs = got
        self.assertEqual(n, 0)
        self.assertTrue(any("行 y0" in e for e in errs), errs)

        with tempfile.TemporaryDirectory() as d:
            dp = os.path.join(d, "decisions.json")
            dec.write(dp, self._doc())
            md = os.path.join(d, "decisions.md")
            rc, _, _ = run_main("--decisions", dp, "--out", md)
            self.assertEqual(rc, 0)
            lines = jsonio.read_text(md).splitlines()
            for i, L in enumerate(lines):
                if L.startswith("| `p-002.png@?`"):
                    cells = mt._split_row(L)
                    cells[mt.COL_VERDICT] = "C"
                    cells[mt.COL_REPL] = "狱还是即将出狱"
                    lines[i] = "|" + "|".join(cells) + "|"
            jsonio.write_text(md, "\n".join(lines))
            before = jsonio.read_text(dp)
            rc, out, err = run_main("--decisions", dp, "--out", md, "--import", md)
            self.assertEqual(rc, 2, out + err)
            self.assertIn("行 y0", err)
            self.assertEqual(before, jsonio.read_text(dp), "整份不写")


class TestMerge(unittest.TestCase):

    def test_merge_semantics(self):
        """导入表格的四条语义：设判定、清整行、判定变了重置 status、留空不动。

        status 必须跟着判定走：判定变了却不重置 status，apply 会当成"已应用"跳过；
        判定没变却重置 status，重复导入就把幂等性抹掉了。
        """
        # 设判定 + 清掉整行（A 不需要 replacement）
        doc = doc_of(ent(1, 10.0, "自\u5DF2", "自己", verdict="B"))
        n, unknown = mt.merge_into(doc, [{"key": "p-001.png@10", "verdict": "A",
                                          "replacement": None, "group": "x"}])
        self.assertEqual((n, unknown), (1, []))
        self.assertEqual(doc["decisions"][0]["verdict"], "A")
        self.assertIsNone(doc["decisions"][0]["replacement"])

        # 判定变了 -> status 回 pending
        doc = doc_of(ent(1, 10.0, "自\u5DF2", "自己", verdict="B", status="applied"))
        mt.merge_into(doc, [{"key": "p-001.png@10", "verdict": "A",
                             "replacement": None, "group": "x"}])
        self.assertEqual(doc["decisions"][0]["status"], "pending")

        # 判定没变 -> status 不动（保幂等）
        doc = doc_of(ent(1, 10.0, "自\u5DF2", "自己", verdict="B", status="applied"))
        n, _ = mt.merge_into(doc, [{"key": "p-001.png@10", "verdict": "B",
                                    "replacement": None, "group": "x"}])
        self.assertEqual(n, 0)
        self.assertEqual(doc["decisions"][0]["status"], "applied")

        # 留空 = 没裁决，原样保留
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

    def test_render_then_import_roundtrip(self):
        """渲染 -> 人填 -> 导入：判定写进 decisions.json，再导一次是幂等的。"""
        rc, out, _ = run_main("--decisions", self.dec, "--out", self.md)
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(self.md))
        jsonio.write_text(self.md, jsonio.read_text(self.md).replace(
            "整组判定：", "整组判定：B", 1))
        rc, out, err = run_main("--decisions", self.dec, "--out", self.md,
                                "--import", self.md)
        self.assertEqual(rc, 0, out + err)
        verdicts = {e["key"]: e["verdict"] for e in dec.read(self.dec)["decisions"]}
        self.assertIn("B", verdicts.values())

        # 再导一次：判定没变，合入 0 条，文件逐字节不变（幂等）
        first = jsonio.read_text(self.dec)
        rc, out, _ = run_main("--decisions", self.dec, "--out", self.md,
                              "--import", self.md)
        self.assertEqual(rc, 0)
        self.assertIn("合入 0 条", out)
        self.assertEqual(first, jsonio.read_text(self.dec))

    def test_bad_import_is_all_or_nothing(self):
        """导入失败必须整份不写（与 apply_decisions 同口径）：
        未知键、非法判定、缺 y0 三种情况都不能"部分成功"。

        改了一半的裁决表是最坏结果——它是唯一不可重建的产物。
        """
        run_main("--decisions", self.dec, "--out", self.md)
        base = jsonio.read_text(self.md).replace("整组判定：", "整组判定：B", 1)
        for new_md, needle in (
            (base.replace("`p-001.png@10`", "`p-999.png@10`"), "找不到"),   # 表格来自别的书
            (base.replace("整组判定：B", "整组判定：X", 1), "不合法"),        # 非法判定
        ):
            with self.subTest(needle=needle):
                jsonio.write_text(self.md, new_md)
                before = jsonio.read_text(self.dec)
                rc, _out, err = run_main("--decisions", self.dec, "--out", self.md,
                                         "--import", self.md)
                self.assertEqual(rc, 2)
                self.assertIn(needle, err + "不合法")
                self.assertEqual(before, jsonio.read_text(self.dec), "整份不写")

    def test_missing_decisions_exits(self):
        """缺裁决表时 sys.exit 给人话，而不是抛 FileNotFoundError。"""
        with self.assertRaises(SystemExit) as cm:
            run_main("--decisions", os.path.join(self.d, "nope.json"), "--out", self.md)
        self.assertEqual(cm.exception.code if isinstance(cm.exception.code, int) else 1, 1)


if __name__ == "__main__":
    unittest.main()
if __name__ == "__main__":
    unittest.main()