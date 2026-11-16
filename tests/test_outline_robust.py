#!/usr/bin/env python
"""本轮新增行为（2026-11-16）：PDF 书签当目录 / 叠字图层择优 / 图形区裁图。

全部用例的证据都来自《拉康式主体》《规训与惩罚》《中国僧官制度史》的实测，
不是"假想的漂亮数据"。三条判据都各有一个反例用例——把实现改回直觉写法，
那些用例必须红。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import layout      # noqa: E402
import outline     # noqa: E402
import textlayer   # noqa: E402


def row(t, x0=0.0, x1=100.0, y0=0.0, y1=16.5):
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "t": t}


class TestOutlineClean(unittest.TestCase):
    """书签标题规整：只去掉不可见字符和多余空白，不改词。"""

    def test_invisible_chars_removed(self):
        self.assertEqual(outline._clean("第1部分\u200b结构"), "第1部分结构")

    def test_ideographic_space_folded(self):
        self.assertEqual(outline._clean("第2\u3000部分"), "第2 部分")

    def test_control_chars_removed(self):
        self.assertEqual(outline._clean("\x00标题\x07"), "标题")

    def test_text_is_not_rewritten(self):
        # 「部分」是一个词，书签里就是「第1部分结构」，不能被拆成「第1部　分结构」
        self.assertEqual(outline._clean("第1部分结构：异化与他者"),
                         "第1部分结构：异化与他者")


class TestOutlineSkipTitles(unittest.TestCase):
    """版式页不进目录；但真章节名不能被误杀。"""

    def test_layout_pages_skipped(self):
        for t in ("封面", "书名页", "目录", "版权", "Cover", "Contents"):
            self.assertTrue(outline.SKIP_TITLES.match(t), t)

    def test_real_chapters_kept(self):
        for t in ("封面设计：张三", "第一部分 酷刑", "索引", "参考书目"):
            self.assertFalse(outline.SKIP_TITLES.match(t), t)


class TestOutlineEntryFallback(unittest.TestCase):
    """书签不可用时不抛异常，返回空表 + 人话原因。"""

    def test_missing_pdf(self):
        e, why = outline.read_outline("no-such-file.pdf")
        self.assertEqual(e, [])
        self.assertIn("没有 PDF 路径", why)

    def test_no_outline_reports_reason(self):
        e, why = outline.read_outline(__file__)   # 不是 PDF
        self.assertEqual(e, [])
        self.assertTrue(why)


class TestTableQuality(unittest.TestCase):
    """表格可读性：列数一致才当文字读，否则该裁图。"""

    def test_stable_columns_are_readable(self):
        # 僧官 p120 沿革表：每行 2 列，一致率 1.0
        rows = []
        for i in range(4):
            rows.append({"x0": 100.0, "y0": i * 60.0, "x1": 300.0,
                         "y1": i * 60.0 + 30.0, "t": "甲%d" % i, "w": 200.0, "h": 30.0})
            rows.append({"x0": 400.0, "y0": i * 60.0, "x1": 600.0,
                         "y1": i * 60.0 + 30.0, "t": "乙%d" % i, "w": 200.0, "h": 30.0})
        ratio, modal, n = layout.table_quality(rows, 60.0, 60.0)
        self.assertEqual(n, 4)
        self.assertEqual(modal, 2)
        self.assertGreaterEqual(ratio, layout.TABLE_MIN_RATIO)

    def test_jittering_columns_not_readable(self):
        # 拉康 p260 的字母矩阵：列数在 1~4 之间乱跳
        rows = []
        for i, n in enumerate((1, 4, 2, 1, 3)):
            for k in range(n):
                rows.append({"x0": 100.0 + k * 300.0, "y0": i * 60.0,
                             "x1": 200.0 + k * 300.0, "y1": i * 60.0 + 30.0,
                             "t": "x", "w": 100.0, "h": 30.0})
        ratio, modal, n = layout.table_quality(rows, 60.0, 60.0)
        self.assertLess(ratio, layout.TABLE_MIN_RATIO)

    def test_single_row_never_readable(self):
        rows = [{"x0": 0.0, "y0": 0.0, "x1": 100.0, "y1": 20.0,
                 "t": "甲", "w": 100.0, "h": 20.0}]
        ratio, modal, n = layout.table_quality(rows, 60.0, 60.0)
        self.assertEqual(ratio, 0.0)


class TestLayerRank(unittest.TestCase):
    """叠字图层择优：碎词少的优先，字数只排第二。"""

    def test_whole_word_beats_fragments(self):
        whole = row("康奈尔大学的理查德·克莱因（Richard Klein）则让我初次", y1=16.5)
        frag = row("康奈尔大学的理查德·克莱因（Richard K le in）则让我初次", y1=11.0)
        self.assertGreater(textlayer._layer_rank(whole),
                           textlayer._layer_rank(frag))

    def test_char_count_breaks_tie_when_no_fragments(self):
        longer = row("他们使用的象征符（symbols）什么意义都没", y1=11.0)
        shorter = row("他们使用的象征符（symbols）什么意义都", y1=16.5)
        self.assertGreater(textlayer._layer_rank(longer),
                           textlayer._layer_rank(shorter))

    def test_space_count_is_not_information(self):
        # 「K le in」比「Klein」多 4 个空格，但空格不是信息
        dense = row("术语上拉康将德语Trieb驱力译作冲动", y1=11.0)
        spaced = row("术语上拉康将德语T r i e b驱力译作冲动", y1=16.5)
        self.assertGreater(textlayer._layer_rank(dense),
                           textlayer._layer_rank(spaced))


class TestFigureRegions(unittest.TestCase):
    """图区类别：display_formula / chart 必须和 image 一样被裁出来。"""

    def test_formula_counts_as_figure(self):
        self.assertIn("display_formula", layout.FIG_REGIONS)

    def test_chart_counts_as_figure(self):
        self.assertIn("chart", layout.FIG_REGIONS)

    def test_formula_not_in_drop_regions(self):
        # 直接删掉的话，成书里那一块会凭空消失，读者不知道原处有什么
        self.assertNotIn("display_formula", layout.DROP_REGIONS)


class TestRunningHeadTitleException(unittest.TestCase):
    """章标题即使等于书眉文字，也不能被当成版式删掉。"""

    def setUp(self):
        self.page = {"name": "p-266.png", "w": 2100, "h": 3309,
                     "lines": [], "regions": []}

    def test_title_kept_when_it_is_also_running_head(self):
        p = dict(self.page)
        p["lines"] = [{"x0": 299.0, "y0": 853.1, "x1": 966.0, "y1": 920.0,
                       "t": "附录2：追踪原因", "region": "paragraph_title"}]
        heads = {layout.norm("附录2：追踪原因")}
        out = layout.clean_page(p, 53.4, 59.3, heads, band_top=0.09, band_bot=0.94)
        self.assertEqual([L["t"] for L in out], ["附录2：追踪原因"])

    def test_running_head_without_title_region_still_dropped(self):
        p = dict(self.page)
        p["lines"] = [{"x0": 299.0, "y0": 853.1, "x1": 966.0, "y1": 920.0,
                       "t": "拉康式主体", "region": "text"}]
        heads = {layout.norm("拉康式主体")}
        out = layout.clean_page(p, 53.4, 59.3, heads, band_top=0.09, band_bot=0.94)
        self.assertEqual(out, [])

    def test_title_in_header_band_still_dropped(self):
        # 位置证据优先：真书眉在页眉带里，标题不在
        p = dict(self.page)
        p["lines"] = [{"x0": 299.0, "y0": 60.0, "x1": 966.0, "y1": 120.0,
                       "t": "索引", "region": "paragraph_title"}]
        heads = {layout.norm("索引")}
        out = layout.clean_page(p, 53.4, 59.3, heads, band_top=0.09, band_bot=0.94)
        self.assertEqual(out, [])


class TestBodyTitleUntouchedForOutline(unittest.TestCase):
    """书签标题原样使用；目录页标题才做「第X部　分」那种插空格。"""

    def test_body_title_inserts_space(self):
        got = layout._body_title("第一章犯人的肉体", "第一章犯人的肉体")
        self.assertEqual(got, "第一章　犯人的肉体")

    def test_body_title_keeps_whole_word(self):
        # 「部分」是一个词，目录页那行没有分隔，不该插入空格
        got = layout._body_title("第1部分结构：异化与他者", "")
        self.assertNotIn("部　分", got)


class TestMultipleHeadsAtSamePosition(unittest.TestCase):
    """同一页同一行可以挂多条标题（分部 + 首章同页）。"""

    def test_both_titles_emitted(self):
        # 《规训与惩罚》:「第一部分 酷刑」和「第一章 犯人的肉体」都指第 9 页首行
        pages = [{"idx": 0, "name": "p-009.png", "w": 1000, "h": 1400, "lines": []}]
        cleaned = {0: [{"x0": 100.0, "y0": 100.0, "x1": 800.0, "y1": 160.0,
                        "t": "第一章", "w": 700.0, "h": 60.0, "region": "paragraph_title"},
                       {"x0": 100.0, "y0": 200.0, "x1": 800.0, "y1": 260.0,
                        "t": "正文第一行", "w": 700.0, "h": 60.0, "region": "text"}]}
        stream = [(0, 0), (0, 1)]
        matched = [
            {"ei": 0, "title": "第一部分 酷刑", "level": 1, "page": 0, "line": 0,
             "score": 1.0, "body_text": "第一章", "pos": 0, "authoritative": True},
            {"ei": 1, "title": "第一章 犯人的肉体", "level": 2, "page": 0, "line": 0,
             "score": 1.0, "body_text": "第一章", "pos": 0, "authoritative": True},
        ]
        blocks = layout.assemble(pages, cleaned, stream, matched, set(), 45.0, 45.0)
        heads = [b for b in blocks if b[0] == "h"]
        self.assertEqual([h[2] for h in heads], ["第一部分 酷刑", "第一章 犯人的肉体"])


if __name__ == "__main__":
    unittest.main()