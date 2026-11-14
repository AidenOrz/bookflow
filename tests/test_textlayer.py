"""bookflow.textlayer 的回归测试 + 三个写入方的 schema 盖章。

用例全部对应实测踩过的坑，不是构造出来的假数据：

- TestParseBBox / TestParsePages：XHTML 解析与 --pages 边界（用户多写一个数字
  不该让整条流程失败；写错首页必须报错，不能静默产出 0 页）。
- TestGroupLines：word → line 聚合。同一行 yMin 抖动、两行被黏成一行的经典错误。
- TestJoin：中英空格还原。**这是本次最大的坑**：任务描述里"间距 > 0.3 字高才
  补空格"在真实英文 PDF 上会把整句拼成 "othercountries"（poppler 切英文时
  相邻框重叠、字距中位数只有 0.25em），在中文 PDF 上又会因为弯引号两侧的缝
  凭空插空格。判据改成"先看两侧字符宽度类别，再看间距"。
- TestDedupe：双图层 PDF 把同一段文字输出两遍，拼出来是
  「FigureFigure 1.1. FlowchartFlowchart…」；实测那篇英文论文 396 对重复框。
- TestEmptyPages：纯图片页/空白页要写空 lines 并计入 empty，不许静默产出 0 字节 JSON。
- TestCoords：PDF 点 → 像素的缩放（300dpi = 4.1667 倍），只能乘一次。
- TestUnusable：纯扫描 PDF 要报「文字层不可用」，一个文件都不许写。
- TestRealPdf（需要本机 PDF）：拿真实 PDF 跑一遍并喂给 layout.build。
- TestWritersStamp：run_ocr / repair / run_layout 三个写入端都盖上 schema 章，
  且**老 JSON（无三键）照常能读**——这是向后兼容的硬要求。

运行：& "E:\ocr-venv\Scripts\python.exe" -X utf8 -m unittest discover -s tests
"""
import contextlib
import glob
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import jsonio     # noqa: E402
import layout     # noqa: E402
import pagedoc    # noqa: E402
import textlayer  # noqa: E402

PY = sys.executable

# 真实 PDF 的位置由环境变量给（本机有书、CI 上通常没有 → 相关用例自动跳过）。
# 选这本书是因为它**有完整文字层**：本仓库另外两本验证书（规训与惩罚、僧官
# 制度史）实测文字层为空（pdftotext 只吐出 pdfFactory 水印），不能用来测这条路。
BOOK_PDF = os.environ.get("BOOKFLOW_TEXT_PDF",
                          r"E:\Downloads\德勒兹差异哲学与美学研究_胡新宇.pdf")


def bbox_xhtml(pages):
    """造一段 pdftotext -bbox 的 XHTML。pages 为 [(w, h, [(x0,y0,x1,y1,text)])]。"""
    out = ["<html><body><doc>"]
    for w, h, words in pages:
        out.append('<page width="%s" height="%s">' % (w, h))
        for x0, y0, x1, y1, t in words:
            out.append('<word xMin="%s" yMin="%s" xMax="%s" yMax="%s">%s</word>'
                       % (x0, y0, x1, y1, t))
        out.append("</page>")
    out.append("</doc></body></html>")
    return "\n".join(out)


def w(x0, y0, x1, y1, t):
    return (x0, y0, x1, y1, t)


def build_md(src, meta=None):
    """调 layout.build 并只取 md/report。

    build() 的返回值个数是 layout 的私有细节（历史上 3 个，加了图表降级后 4 个），
    而且它正由别的任务在改。这里只按**前两项**取，别把测试焊死在一个会变的
    元组长度上——实测有一次并行开发期间 build 临时返回 2 个值，本模块的测试
    就全线报 "not enough values to unpack"，那与被测的 textlayer 毫无关系。
    """
    r = layout.build(src, meta or {})
    return r[0], r[1]


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

class TestParseBBox(unittest.TestCase):
    def test_words_and_size(self):
        pages = textlayer.parse_bbox(bbox_xhtml([
            (595.0, 842.0, [w(18, 819.692, 36, 831.692, "PDF"),
                            w(42, 819.692, 90, 831.692, "文件使用")])]))
        self.assertEqual(len(pages), 1)
        self.assertAlmostEqual(pages[0]["w"], 595.0)
        self.assertAlmostEqual(pages[0]["h"], 842.0)
        self.assertEqual([x["t"] for x in pages[0]["words"]], ["PDF", "文件使用"])
        self.assertAlmostEqual(pages[0]["words"][0]["y0"], 819.692)

    def test_entities_are_unescaped(self):
        """poppler 会把引号转成 &quot;，不还原的话正文里会出现 &quot;。"""
        pages = textlayer.parse_bbox(bbox_xhtml([
            (595, 842, [w(0, 0, 10, 10, "&quot;pdfFactory&quot;"),
                        w(0, 0, 10, 10, "a&amp;b")])]))
        self.assertEqual(pages[0]["words"][0]["t"], '"pdfFactory"')
        self.assertEqual(pages[0]["words"][1]["t"], "a&b")

    def test_whitespace_only_words_dropped(self):
        pages = textlayer.parse_bbox(bbox_xhtml([
            (595, 842, [w(0, 0, 10, 10, "  "), w(0, 0, 10, 10, "字")])]))
        self.assertEqual(len(pages[0]["words"]), 1)

    def test_empty_and_garbage_input(self):
        self.assertEqual(textlayer.parse_bbox(""), [])
        self.assertEqual(textlayer.parse_bbox("<html></html>"), [])
        # 有 page 但一个 word 都没有 → 一页、零行（不是零页！少一页会整体错位）
        pages = textlayer.parse_bbox(bbox_xhtml([(595, 842, [])]))
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["words"], [])


class TestParsePages(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(textlayer.parse_pages("1-60"), (1, 60))
        self.assertEqual(textlayer.parse_pages("5"), (5, 5))
        self.assertEqual(textlayer.parse_pages("5-"), (5, None))
        self.assertEqual(textlayer.parse_pages("-60"), (None, 60))
        self.assertEqual(textlayer.parse_pages(""), (None, None))

    def test_clamped_to_total(self):
        """上限超出实际页数 → 夹到总页数（用户多写一个数字不该让流程失败）。"""
        self.assertEqual(textlayer.parse_pages("1-999", total=100), (1, 100))

    def test_bad_spec_raises(self):
        for spec in ("abc", "0-10", "10-5", "1-2-3"):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    textlayer.parse_pages(spec)

    def test_first_beyond_total_raises(self):
        """首页超界必须报错：静默返回 0 页会让人以为"这本书没有文字层"。"""
        with self.assertRaises(ValueError):
            textlayer.parse_pages("500-", total=100)


# ---------------------------------------------------------------------------
# 分行
# ---------------------------------------------------------------------------

class TestGroupLines(unittest.TestCase):
    def _ws(self, *rows):
        """rows 为 (基线 y1, [(x0, x1, text)])。字高固定 10，y0 = y1 - 10。"""
        out = []
        for y1, items in rows:
            for x0, x1, t in items:
                out.append({"x0": x0, "y0": y1 - 10, "x1": x1, "y1": y1, "t": t})
        return out

    def test_same_baseline_groups(self):
        rows = textlayer.group_lines(self._ws((110, [(10, 40, "甲"), (60, 90, "乙")])), 3)
        self.assertEqual(len(rows), 1)
        self.assertEqual([x["t"] for x in rows[0]], ["甲", "乙"])

    def test_baseline_jitter_within_tolerance(self):
        """同一行内基线抖 2pt 是常态，不能被拆成两行。"""
        rows = textlayer.group_lines(self._ws((110, [(10, 40, "甲")]),
                                             (111.5, [(60, 90, "乙")])), 3)
        self.assertEqual(len(rows), 1)

    def test_far_baseline_splits(self):
        rows = textlayer.group_lines(self._ws((110, [(10, 40, "甲")]),
                                             (130, [(10, 40, "乙")])), 3)
        self.assertEqual(len(rows), 2)

    def test_grouping_uses_baseline_not_top_edge(self):
        """上下边缘对不齐的中文字形必须落在同一行（本次修的真实 bug）。

        实测德勒兹那本书同一行里「存不存在一种」(y0=67.0) 与「巴」(y0=68.7)
        yMin 差 1.7pt、yMax 差却只有 0.3pt；按 yMin 分会把一行拆成三段，
        拼出来的句子东一段西一段（1 页 68 行 vs 真实 33 行）。
        这里给出 yMin 差 8pt、yMax 完全对齐的一组词——必须成 1 行。
        """
        ws = [{"x0": 0, "y0": 60.0, "x1": 10, "y1": 77.6, "t": "存"},
              {"x0": 10, "y0": 68.7, "x1": 20, "y1": 77.6, "t": "巴"},
              {"x0": 20, "y0": 67.0, "x1": 30, "y1": 77.6, "t": "乌"}]
        rows = textlayer.group_lines(ws, 3.0)
        self.assertEqual(len(rows), 1, "基线一致就必须是一行")
        self.assertEqual(textlayer.join_words(rows[0]), "存巴乌")

    def test_sorting_is_by_x_within_row(self):
        """输入顺序打乱也必须按 x 排：poppler 的输出顺序不保证。"""
        ws = [{"x0": 90, "y0": 100, "x1": 120, "y1": 110, "t": "后"},
              {"x0": 10, "y0": 100, "x1": 40, "y1": 110, "t": "前"}]
        rows = textlayer.group_lines(ws, 3)
        self.assertEqual([x["t"] for x in rows[0]], ["前", "后"])

    def test_chain_drift_does_not_glue_rows(self):
        """基线逐词缓慢漂移时，不能靠"和上一个词比"把两行黏成一行。"""
        ws = [{"x0": i * 10, "y0": 100 + i * 2.0, "x1": i * 10 + 8,
               "y1": 110 + i * 2.0, "t": str(i)} for i in range(10)]
        rows = textlayer.group_lines(ws, 2.0)
        self.assertGreater(len(rows), 1, "链式漂移必须分行")


# ---------------------------------------------------------------------------
# 行内拼接（本次最大的坑）
# ---------------------------------------------------------------------------

def line_of(*items):
    """把 (x0,x1,text) 组成一行 words；字高固定 10（1em = 10pt）。"""
    return [{"x0": x0, "y0": 100.0, "x1": x1, "y1": 110.0, "t": t}
            for x0, x1, t in items]


class TestJoin(unittest.TestCase):
    def test_cjk_never_gets_space(self):
        """中文按词切开的缝（实测 p50=0.17em）不能补空格。"""
        self.assertEqual(textlayer.join_words(line_of((0, 5, "学校代"), (5.5, 10, "码"))),
                         "学校代码")
        # 0.25em 的缝也不补（实测汉-汉 p90=0.244）
        self.assertEqual(textlayer.join_words(line_of((0, 5, "中"), (7.5, 12, "的"))),
                         "中的")

    def test_cjk_punctuation_no_space(self):
        self.assertEqual(textlayer.join_words(line_of((0, 5, "正文"), (5, 10, "。"))),
                         "正文。")

    def test_english_words_keep_spaces(self):
        """英文相邻词的字距只有 0.25em，必须补空格——这是原判据的致命缺陷。"""
        self.assertEqual(
            textlayer.join_words(line_of((0, 30, "other"), (32.5, 80, "countries"))),
            "other countries")

    def test_english_touching_boxes_still_get_space(self):
        """poppler 切英文时相邻框首尾相接（gap≈0），仍然要补空格。"""
        self.assertEqual(textlayer.join_words(line_of((0, 30, "of"), (30, 50, "the"))),
                         "of the")

    def test_split_word_fragment_not_spaced(self):
        """同一单词被切开时两段坐标完全重叠 → 不补空格。"""
        self.assertEqual(textlayer.join_words(line_of((0, 20, "regu"), (15, 40, "lation"))),
                         "regulation")

    def test_mixed_wide_latin(self):
        """混排缝隙是双峰的：0.25em 不补（实测「圖4-1-1」），0.4em 补（真空格）。"""
        self.assertEqual(textlayer.join_words(line_of((0, 10, "圖"), (12.5, 40, "4-1-1"))),
                         "圖4-1-1")
        self.assertEqual(textlayer.join_words(line_of((0, 10, "對"), (14, 40, "TYPE"))),
                         "對 TYPE")

    def test_chinese_quotes_do_not_break_lines(self):
        """弯引号是中性字符，由邻居定性：中文里贴着汉字，不补空格。

        实测原文「提出了 “哪个德勒兹？ ” 这个问题」——引号两侧的缝比汉-汉
        正常字距大 0.12em，照"间距>0.3em"判会凭空插两个空格。
        """
        self.assertEqual(
            textlayer.join_words(line_of((0, 10, "提出了"), (12.2, 15, "“"),
                                         (15.2, 30, "哪个"))),
            "提出了“哪个")

    def test_english_apostrophe(self):
        """英文撇号紧贴单词（gap≈0），不能被当成宽字插空格。"""
        self.assertEqual(
            textlayer.join_words(line_of((0, 40, "devices"), (40, 44, "’"),
                                         (44.5, 90, "classification"))),
            "devices’ classification")

    def test_huge_gap_gets_space(self):
        """双栏/目录页码列：间距 5em 必须补空格，否则两栏黏在一起。"""
        out = textlayer.join_words(line_of((0, 10, "第一章"), (60, 70, "3")))
        self.assertEqual(out, "第一章 3")

    def test_no_double_space(self):
        """word 文本自带空格时不能拼出双空格。"""
        self.assertEqual(textlayer.join_words(line_of((0, 30, "PDF "), (32, 60, "文件"))),
                         "PDF 文件")

    def test_empty(self):
        self.assertEqual(textlayer.join_words([]), "")

    def test_line_height_uses_max(self):
        """行内夹小字（注号）时，字高按最高的取——中位数会把阈值拉低。"""
        ws = [{"x0": 0, "y0": 100, "x1": 10, "y1": 110, "t": "正"},
              {"x0": 12, "y0": 104, "x1": 20, "y1": 107, "t": "①"}]
        self.assertAlmostEqual(textlayer.line_height(ws), 10.0)


# ---------------------------------------------------------------------------
# 重复框
# ---------------------------------------------------------------------------

class TestDedupe(unittest.TestCase):
    def _w(self, x0, x1, t):
        return {"x0": x0, "y0": 100, "x1": x1, "y1": 110, "t": t}

    def test_duplicate_layer_removed(self):
        """双图层 PDF：同一段字两份、坐标几乎重合（实测重叠率 p50=0.84）。"""
        ws = [self._w(0, 40, "Figure"), self._w(0.5, 40.5, "Figure"),
              self._w(42, 90, "1.1."), self._w(42.2, 90.2, "1.1.")]
        out = textlayer.dedupe_words(ws)
        self.assertEqual([x["t"] for x in out], ["Figure", "1.1."])

    def test_real_repeat_text_kept(self):
        """正文里真实的重复词（「的的」）不是重叠框，不能删。"""
        ws = [self._w(0, 10, "的"), self._w(10, 20, "的")]
        self.assertEqual(len(textlayer.dedupe_words(ws)), 2)

    def test_overlap_but_different_text_kept(self):
        """上标/注号本来就和正文框重叠，但文本不同，必须保留。"""
        ws = [self._w(0, 40, "正文①"), self._w(20, 24, "①")]
        self.assertEqual(len(textlayer.dedupe_words(ws)), 2)

    def test_three_layers(self):
        ws = [self._w(0, 40, "x"), self._w(0, 40, "x"), self._w(0, 40, "x")]
        self.assertEqual(len(textlayer.dedupe_words(ws)), 1)


# ---------------------------------------------------------------------------
# 坐标缩放 / 空页 / 统计
# ---------------------------------------------------------------------------

class TestCoords(unittest.TestCase):
    def test_scale_300dpi(self):
        """PDF 点 × dpi/72：595.28pt@300dpi = 2480px（与 pdftoppm 的产物一致）。"""
        page = {"w": 595.28, "h": 841.89,
                "words": [{"x0": 10.0, "y0": 20.0, "x1": 30.0, "y1": 32.0, "t": "字"}]}
        doc, _st = textlayer.build_page(page, 1, dpi=300)
        self.assertEqual(doc["w"], 2480)
        self.assertEqual(doc["h"], 3508)
        self.assertAlmostEqual(doc["lines"][0]["x0"], 41.7, places=1)   # 10 × 4.1667
        self.assertAlmostEqual(doc["lines"][0]["y1"], 133.3, places=1)  # 32 × 4.1667

    def test_scale_is_applied_exactly_once(self):
        """dpi 翻倍 → 坐标翻倍（乘两次会变成 4 倍，这种 bug 小样本看不出来）。"""
        page = {"w": 100.0, "h": 100.0,
                "words": [{"x0": 10.0, "y0": 10.0, "x1": 20.0, "y1": 20.0, "t": "字"}]}
        d300, _ = textlayer.build_page(page, 1, dpi=300)
        d600, _ = textlayer.build_page(page, 1, dpi=600)
        self.assertAlmostEqual(d600["lines"][0]["x0"], 2 * d300["lines"][0]["x0"], places=0)

    def test_page_name_and_confidence(self):
        page = {"w": 100.0, "h": 100.0,
                "words": [{"x0": 1.0, "y0": 1.0, "x1": 9.0, "y1": 9.0, "t": "字"}]}
        doc, _ = textlayer.build_page(page, 7, dpi=72)
        self.assertEqual(doc["name"], "p-007.png")
        self.assertEqual(doc["lines"][0]["s"], 1.0, "文字层没有置信度，固定 1.0")

    def test_line_tolerance_is_dpi_independent(self):
        """聚类发生在 **PDF 点空间**，所以行数不能随 dpi 变化。

        这是实测踩过的坑：早先在 page_lines 里把容差乘了 dpi/72（300dpi 下
        3pt 变成 12.5pt），于是页眉「Appl. Sci. 2024」和正文首行（相距 10pt）
        被并成一行，拼出「…9304 MDR in 2017…」。缩放只能在最后输出坐标时做。
        """
        apart4 = [{"x0": 0, "y0": 100, "x1": 10, "y1": 110, "t": "甲"},
                  {"x0": 0, "y0": 104, "x1": 10, "y1": 114, "t": "乙"}]
        for dpi in (72, 300, 600):
            with self.subTest(dpi=dpi):
                self.assertEqual(len(textlayer.page_lines({"words": apart4}, dpi)), 2,
                                 "4pt > 3pt 容差，任何 dpi 下都该是两行")
        # 2pt 的抖动在同一行内，三档 dpi 都必须合成一行
        apart2 = [{"x0": 0, "y0": 100, "x1": 10, "y1": 110, "t": "甲"},
                  {"x0": 12, "y0": 102, "x1": 22, "y1": 112, "t": "乙"}]
        for dpi in (72, 300, 600):
            with self.subTest(dpi=dpi):
                self.assertEqual(len(textlayer.page_lines({"words": apart2}, dpi)), 1)


class TestEmptyPages(unittest.TestCase):
    def test_empty_page_has_empty_lines(self):
        """纯图片页 → 空 lines，但**页必须在**（少一页会让后续全部错位）。"""
        doc, st = textlayer.build_page({"w": 595.0, "h": 842.0, "words": []}, 3)
        self.assertEqual(doc["lines"], [])
        self.assertEqual(doc["name"], "p-003.png")
        self.assertEqual(st, {"chars": 0, "cjk": 0})

    def test_stats_chars_and_cjk(self):
        lines = [{"t": "汉字 abc 123"}, {"t": "。！"}]
        st = textlayer.page_stats(lines)
        self.assertEqual(st["chars"], 10)     # 汉字abc123。！ = 2+3+3+2
        self.assertEqual(st["cjk"], 2)

    def test_blank_only_words_make_no_line(self):
        doc, st = textlayer.build_page({"w": 595.0, "h": 842.0,
                                        "words": [{"x0": 1, "y0": 1, "x1": 2,
                                                   "y1": 2, "t": " "}]}, 1)
        self.assertEqual(doc["lines"], [])
        self.assertEqual(st["chars"], 0)


# ---------------------------------------------------------------------------
class TestUnusable(unittest.TestCase):
    """纯扫描件 / pdftotext 失败的处理。

    注意：**不要造一个假的 exe 去 spawn**。Windows 上 subprocess 拿到不可执行的
    桩会抛 [WinError 193]，真实错误会被"启动失败"盖掉（实测踩过，测试里因此
    永远匹配不到「加密」）。正确做法是把 subprocess.run 换成返回假结果的桩——
    被测的逻辑（怎么判空、怎么报错）和"能不能启动进程"本来就没关系。
    """

    def _pdf(self, tmp):
        p = os.path.join(tmp, "x.pdf")
        with open(p, "wb") as f:
            f.write(b"%PDF-1.4")
        return p

    def _patch_run(self, **kw):
        """替掉 textlayer 里的 subprocess.run。

        注意 textlayer 模块里 `import subprocess` 用的是同一个模块对象，所以
        patch.object(textlayer.subprocess, "run", ...) 就是全局地换掉它；
        这里显式写 textlayer.subprocess 是为了让读者一眼看出作用域。
        """
        std = kw.get("stdout", b"")
        rc = kw.get("returncode", 0)
        err = kw.get("stderr", b"")

        def fake(cmd, **kwargs):
            out = mock.Mock()
            out.stdout, out.stderr, out.returncode = std, err, rc
            return out
        return mock.patch.object(textlayer.subprocess, "run", fake)

    def test_scan_only_pdf_writes_nothing(self):
        """纯扫描件：每页都有 <page> 但零 word → usable=False，一个文件都不写。"""
        body = bbox_xhtml([(595, 842, []), (595, 842, [])]).encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out")
            pdf = self._pdf(tmp)
            with self._patch_run(stdout=body),                     mock.patch.object(textlayer, "pdf_page_count", lambda p: 2):
                summary = textlayer.extract(pdf, out, quiet=True)
            self.assertFalse(summary["usable"])
            self.assertEqual(summary["pages"], 2)
            self.assertEqual(summary["empty"], 2)
            self.assertEqual(summary["written"], 0)
            self.assertFalse(os.path.exists(os.path.join(out, "p-001.png.json")),
                             "纯扫描件不许产出空 JSON")

    def test_no_words_means_unusable_cli_exit_code(self):
        """CLI 上要给出退出码 2（「整本无字」）——与 3（pdftotext 失败）区分开。"""
        body = bbox_xhtml([(595, 842, []), (595, 842, [])]).encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out")
            pdf = self._pdf(tmp)
            buf, err = io.StringIO(), io.StringIO()
            with self._patch_run(stdout=body),                     mock.patch.object(textlayer, "pdf_page_count", lambda p: 2),                     mock.patch.object(textlayer, "pdftotext_version", lambda *a: "26.09.0"),                     contextlib.redirect_stdout(buf),                     contextlib.redirect_stderr(err):
                rc = textlayer.main(["--pdf", pdf, "--out", out, "--quiet"])
            self.assertEqual(rc, 2)
            self.assertIn("文字层不可用", err.getvalue())
            line = [l for l in buf.getvalue().splitlines() if l.startswith("SUMMARY ")][0]
            summary = json.loads(line[len("SUMMARY "):])
            self.assertFalse(summary["usable"])
            self.assertEqual(summary["written"], 0)

    def test_pdftotext_failure_is_reported(self):
        """pdftotext 退出码非 0（加密 PDF）→ 退出码 3 + 人话，不是 traceback。"""
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self._pdf(tmp)
            out = os.path.join(tmp, "out")
            buf, err = io.StringIO(), io.StringIO()
            with self._patch_run(returncode=1,
                                 stderr="Syntax Error: may be encrypted".encode()), \
                    contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(err):
                rc = textlayer.main(["--pdf", pdf, "--out", out])
            self.assertEqual(rc, 3)
            e = err.getvalue()
            self.assertIn("文字层不可用", e)
            self.assertIn("加密", e)
            self.assertFalse(os.path.isdir(out), "失败时一个文件都不该写")

    def test_pdftotext_missing_is_reported(self):
        """PATH 上没有 pdftotext 也要报人话，不是 FileNotFoundError 的裸 traceback。"""
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self._pdf(tmp)
            with mock.patch.object(textlayer.shutil, "which", lambda name: None), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = textlayer.main(["--pdf", pdf, "--out", os.path.join(tmp, "o")])
            self.assertEqual(rc, 3)

    def test_missing_pdf_raises(self):
        with self.assertRaises(textlayer.TextLayerError):
            textlayer.run_pdftotext(os.path.join(tempfile.gettempdir(), "不存在的.pdf"))

    def test_error_codes_are_stable_ascii(self):
        """错误码必须是 ASCII 且稳定，上层不必去匹配中文文案。"""
        cases = [
            ("PATH 上找不到 pdftotext（装 poppler 后把它加进 PATH）", "NO_PDFTOTEXT"),
            ("PDF 不存在：x.pdf", "PDF_MISSING"),
            ("pdftotext 超时（30 秒）：x.pdf", "TIMEOUT"),
            ("pdftotext 启动失败：WinError 193", "SPAWN_FAILED"),
            ("pdftotext 没有输出任何 <page> 元素（PDF 可能损坏）", "EMPTY_OUTPUT"),
            ("pdftotext 失败（退出码 1，PDF 可能加密或损坏）", "PDFTOTEXT_FAILED"),
            ("谁知道呢", "UNKNOWN"),
        ]
        for msg, want in cases:
            with self.subTest(msg=msg):
                self.assertEqual(textlayer.error_code(textlayer.TextLayerError(msg)), want)
                self.assertTrue(want.isascii())

    def test_summary_line_is_pure_ascii(self):
        """SUMMARY 行必须纯 ASCII：调用方按 UTF-8 解码不会炸。

        实测：父进程用 \`encoding="utf-8"\` 解码 GBK 字节会抛 UnicodeDecodeError
        （不是乱码，是异常），整个阶段直接失败。所以机器可读那一行必须与编码无关，
        中文用 \\uXXXX 转义——json 解析回来仍是原字。
        """
        line = textlayer.summary_line({"usable": False, "error": "文字层不可用：加密"})
        self.assertTrue(line.isascii(), "SUMMARY 行必须是 ASCII：%r" % line)
        self.assertTrue(line.startswith("SUMMARY "))
        back = json.loads(line[len("SUMMARY "):])
        self.assertEqual(back["error"], "文字层不可用：加密")
        self.assertFalse(back["usable"])

    def test_failure_summary_carries_error_code(self):
        """走 CLI 的失败分支时，stdout 上仍要有完整的 ASCII SUMMARY。"""
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self._pdf(tmp)
            buf, err = io.StringIO(), io.StringIO()
            with self._patch_run(returncode=1,
                                 stderr=b"Syntax Error: may be encrypted"), \
                    contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(err):
                rc = textlayer.main(["--pdf", pdf, "--out", os.path.join(tmp, "o")])
            self.assertEqual(rc, 3)
            self.assertTrue(buf.getvalue().isascii(), "机器可读那一行必须 ASCII")
            line = [l for l in buf.getvalue().splitlines()
                    if l.startswith("SUMMARY ")][0]
            summary = json.loads(line[len("SUMMARY "):])
            self.assertEqual(summary["error_code"], "PDFTOTEXT_FAILED")
            self.assertFalse(summary["usable"])


# ---------------------------------------------------------------------------
# 端到端（需要真 PDF）
# ---------------------------------------------------------------------------

@unittest.skipUnless(os.path.exists(BOOK_PDF), "没有可用的文字层 PDF（设 BOOKFLOW_TEXT_PDF）")
class TestRealPdf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.summary = textlayer.extract(BOOK_PDF, cls.tmp.name, pages="1-12",
                                        dpi=300, quiet=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_summary_counts(self):
        s = self.summary
        self.assertTrue(s["usable"])
        self.assertEqual(s["pages"], 12)
        self.assertEqual(s["written"], 12)
        self.assertEqual(s["with_text"] + s["empty"], s["pages"])
        self.assertGreater(s["cjk_per_page"], 300, "这本书每页有大量汉字")
        self.assertEqual(s["producer"]["name"], "pdftotext")

    def test_json_has_schema_and_layout_builds_it(self):
        files = sorted(glob.glob(os.path.join(self.tmp.name, "*.json")))
        self.assertEqual(len(files), 12)
        doc = jsonio.read(files[0])
        self.assertEqual(pagedoc.check(doc), [], "textlayer 的产物必须符合页级 schema")
        self.assertEqual(doc["schema"], pagedoc.SCHEMA)
        self.assertEqual(doc["schema_version"], pagedoc.SCHEMA_VERSION)
        self.assertEqual(doc["producer"]["name"], "pdftotext")
        self.assertTrue(doc["producer"]["version"], "poppler 版本应当取到")
        self.assertIn("source_pdf", doc)
        # 下游真的能吃下（这是验收标准里最重要的一条）
        md, report = build_md(self.tmp.name)
        self.assertEqual(report["pages"], 12)
        self.assertGreater(report["chars"], 1000)
        self.assertTrue(md.strip())

    def test_lines_are_readable(self):
        doc = jsonio.read(sorted(glob.glob(os.path.join(self.tmp.name, "*.json")))[3])
        text = "".join(L["t"] for L in doc["lines"])
        self.assertRegex(text, r"[\u4e00-\u9fff]{8,}", "应当拼出成段的汉字")
        # 空格还原：英文单词之间必须有空格（否则整本英文会黏成一片）
        for L in doc["lines"]:
            self.assertNotRegex(L["t"], r"[a-zA-Z]{25,}",
                                "整行英文黏连说明空格还原失效：%r" % L["t"][:60])


class TestStdioEncoding(unittest.TestCase):
    def test_stdout_is_utf8_when_captured(self):
        """bookflow 用 encoding="utf-8" 捕获我们的 stdout。

        Windows 上子进程默认是 GBK（cp936），不加 -X utf8 或 reconfigure 的话，
        父进程拿到的中文全是乱码（实测「文字层：12 页…」变成「���ֲ㣺12 ҳ…」）。
        这里真的起一个子进程，用和 bookflow 完全相同的方式解码，验证中文可读。
        """
        script = os.path.join(ROOT, "textlayer.py")
        code = ("import sys; sys.path.insert(0, %r); import textlayer; "
                "textlayer._utf8_stdio(); print('\\u6587\\u5b57\\u5c42\\uff1a12 \\u9875')"
                % ROOT)
        cp = subprocess.run([PY, "-c", code], stdout=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
        self.assertTrue(cp.stdout.startswith("文字层：12 页"),
                        "子进程 stdout 必须能被 UTF-8 正确解码，实际=%r" % cp.stdout)
        self.assertTrue(os.path.exists(script))

    def test_reconfigure_tolerates_redirected_streams(self):
        """测试里 stdout 被 redirect 成 StringIO（没有 reconfigure）时不能炸。"""
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            textlayer._utf8_stdio()      # 不该抛异常
            print("ok")
        self.assertEqual(buf.getvalue().strip(), "ok")


# ---------------------------------------------------------------------------
# 三个写入端的 schema 盖章（向后兼容是硬要求）
# ---------------------------------------------------------------------------

class TestWritersStamp(unittest.TestCase):
    def test_rapidocr_producer_registered(self):
        for name in ("rapidocr", "rapidocr-repair", "pp_doc_layoutv3", "pdftotext"):
            self.assertIn(name, pagedoc.KNOWN_PRODUCERS)
        self.assertEqual(textlayer.PRODUCER, "pdftotext")

    def test_run_ocr_writes_stamped_doc(self):
        """run_ocr.work() 写出的 JSON 必须带 schema 三键，且名字是 rapidocr。"""
        try:
            import run_ocr
        except Exception as e:            # 没装 rapidocr 的环境跳过
            self.skipTest("run_ocr 不可 import：%s" % e)
        doc = pagedoc.stamp({"name": "p-001.png", "w": 10, "h": 10, "lines": []},
                            pagedoc.producer(run_ocr.PRODUCER_NAME,
                                             run_ocr.rapidocr_version()))
        self.assertEqual(doc["schema"], pagedoc.SCHEMA)
        self.assertEqual(doc["producer"]["name"], "rapidocr")
        self.assertIsInstance(doc["producer"]["version"], (str, type(None)))

    def test_writer_modules_reference_pagedoc(self):
        """三个写入端都必须 import pagedoc 并盖章（防止有人改回去）。"""
        for name in ("run_ocr.py", "repair.py", "run_layout.py"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                src = f.read()
            with self.subTest(module=name):
                self.assertIn("import pagedoc", src)
                self.assertIn("pagedoc.stamp(", src)
                self.assertIn("pagedoc.producer(", src)

    def test_stamp_preserves_existing_fields(self):
        """盖章只新增键：regions/region/r 这些既有字段一个都不能丢。"""
        doc = {"name": "p-001.png", "w": 100, "h": 200,
               "lines": [{"x0": 1, "y0": 2, "x1": 3, "y1": 4, "t": "字", "s": 0.9,
                          "region": "text", "r": 0.93}],
               "regions": [{"x0": 1, "y0": 2, "x1": 3, "y1": 4, "cls": "text"}]}
        out = pagedoc.stamp(doc, pagedoc.producer("pp_doc_layoutv3", "1.2.1"),
                            upstream_producer={"name": "rapidocr", "version": "3.10.0"})
        self.assertEqual(out["regions"], doc["regions"])
        self.assertEqual(out["lines"][0]["region"], "text")
        self.assertEqual(out["upstream_producer"]["name"], "rapidocr")
        self.assertEqual(pagedoc.check(out), [])

    def test_legacy_json_still_readable(self):
        """老 JSON 没有 schema 三键 → 所有读取方照常工作（本任务的硬要求）。"""
        with tempfile.TemporaryDirectory() as tmp:
            legacy = {"name": "p-001.png", "w": 2480, "h": 3508,
                      "lines": [{"x0": 300, "y0": 400, "x1": 2100, "y1": 470,
                                 "t": "这是老格式的一行正文，没有 schema 字段。",
                                 "s": 0.98}]}
            path = os.path.join(tmp, "p-001.png.json")
            jsonio.write(path, legacy)          # 注意：直接 jsonio 写，不带章
            self.assertEqual(pagedoc.check(jsonio.read(path)), [])
            self.assertEqual(pagedoc.read(path)["lines"][0]["t"],
                             legacy["lines"][0]["t"])
            # layout.load_pages 与 layout.build 都必须能读
            pages = layout.load_pages(tmp)
            self.assertEqual(len(pages), 1)
            md, report = build_md(tmp)
            self.assertIsInstance(md, str)

    def test_decisions_reads_legacy_pages(self):
        """decisions.page_path / apply_decisions 也读页级 JSON，老文件不能报错。"""
        import decisions as dec
        with tempfile.TemporaryDirectory() as tmp:
            name = dec.page_name(1)
            jsonio.write(os.path.join(tmp, name + ".json"),
                         {"name": name, "w": 100, "h": 200,
                          "lines": [{"x0": 1, "y0": 2, "x1": 3, "y1": 4,
                                     "t": "错字行", "s": 0.9}]})
            path = dec.page_path(tmp, 1)
            self.assertTrue(path, "补零容错必须能找到 p-001.png.json")
            doc = jsonio.read(path)
            self.assertEqual(doc["lines"][0]["t"], "错字行")


if __name__ == "__main__":
    unittest.main()
