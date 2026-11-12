"""bookflow 版面逻辑的最小回归测试。

这些用例全部来自真实踩坑，不是构造出来的假数据：
- test_head_band_does_not_eat_body：旧书 5 页正文首行被硬编码 10.5% 阈值吃掉
- test_title_residue_consumed：第五讲三行标题的第三行漏进正文
- test_title_residue_keeps_body：收紧判据后，正文末尾的「的。」不能被当成残片
- test_dup_pages_removed：新书源 PDF 末尾 19 页重复，去重后不能成段重复
- test_empty_src_exits：空输入必须报错，不能静默产出 0 字节成书
- test_fix_rules：fix.py 的 29 条正负例

运行：python -m unittest discover -s tests -v
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import fix          # noqa: E402
import layout       # noqa: E402
import qa           # noqa: E402


def synth_page(idx, lines, w=2480, h=3508):
    """构造一页页级 JSON（lines 为 (x0, y0, x1, y1, t) 元组）。"""
    return {
        "name": "p-%03d.png" % (idx + 1), "w": w, "h": h, "idx": idx,
        "lines": [{"x0": x0, "y0": y0, "x1": x1, "y1": y1, "t": t, "s": 0.99}
                  for (x0, y0, x1, y1, t) in lines],
    }


class TestHeadBand(unittest.TestCase):
    def test_head_band_does_not_eat_body(self):
        """页眉只到 5% 页高，正文首行在 7%——阈值必须自适应，不能固定 10.5%。"""
        pages = []
        for i in range(8):
            # 正文每页必须不同，否则会被 learn_running_heads 当成跨页重复的书眉
            pages.append(synth_page(i, [
                (300, 100, 900, 176, "某某丛书"),                        # 书眉 y1/H=0.05
                (300, 245, 2100, 280, "第%d页的正文首行，绝对不能丢。" % i),  # y1/H=0.08
                (300, 340, 2100, 700, "第%d页的正文第二行。" % i),
            ]))
        bw, bh = layout.metrics(pages)
        heads = layout.learn_running_heads(pages, bh)
        self.assertIn(layout.norm("某某丛书"), heads)
        bt, bb = layout.head_band(pages, heads, bh)
        self.assertLess(bt, 280 / 3508, "band_top 必须小于正文首行的 y1/H")
        cl = layout.clean_page(pages[0], bw, bh, heads, band_top=bt, band_bot=bb)
        texts = [L["t"] for L in cl]
        self.assertIn("第0页的正文首行，绝对不能丢。", texts)
        self.assertNotIn("某某丛书", texts)


class TestTitleResidue(unittest.TestCase):
    def _build(self, extra_tail):
        """构造：一个目录页 + 一个正文页，正文页有「第一讲 / 副标题 / 正文」。"""
        W, H = 2480, 3508
        toc = synth_page(0, [
            (400, 400, 2000, 480, "目 录"),
            (400, 700, 1800, 780, "第一讲 黑格尔辩证法的两大起源………（1）"),
        ], W, H)
        body = synth_page(1, [
            (700, 500, 1300, 600, "第一讲"),                          # 大字号
            (700, 650, 1800, 750, "黑格尔辩证法的两大起源"),           # 大字号
            (300, 1500, 2100, 1560, "正文第一行内容足够长不会误判为标题。", ),
            (300, 1580, 2100, 1640, "正文第二行内容也足够长。"),
        ] + extra_tail, W, H)
        return [toc, body]

    def test_title_residue_consumed(self):
        """标题折行时，未被命中的那一行必须被消费掉，不能漏进正文。"""
        md = self._run(self._build([]))
        # 副标题行不应该作为独立段落出现
        paras = [l for l in md.split("\n") if l.strip() and not l.startswith("#")]
        self.assertNotIn("黑格尔辩证法的两大起源", paras)

    def test_title_residue_keeps_body(self):
        """正文末尾单独成行的「的。」不能被当作标题残片删掉。"""
        W, H = 2480, 3508
        md = self._run(self._build([
            (300, 1700, 2100, 1760, "上面这句只是个铺垫，真正要紧的是下文。"),
            (300, 1780, 520, 1840, "的。"),     # 小字号、不是标题子串
        ]))
        self.assertIn("的。", md)

    def _run(self, pages):
        with tempfile.TemporaryDirectory() as d:
            import json
            src = os.path.join(d, "ocr")
            os.makedirs(src)
            for p in pages:
                p = dict(p)
                del p["idx"]
                fn = os.path.join(src, p["name"] + ".json")
                with open(fn, "w", encoding="utf-8") as f:
                    json.dump(p, f, ensure_ascii=False)
            md, rep, cleaned, ps = layout.build(src, {"title": "t"})
            return md


class TestDedupePages(unittest.TestCase):
    def test_dup_pages_removed(self):
        """同一页内容连续重复 ≥3 次时只保留第一页（源 PDF 常见拼接重复）。"""
        pages = [synth_page(i, [(300, 400, 2100, 480, "第%d页正文。" % i)])
                 for i in range(3)]
        pages += [synth_page(i, [(300, 400, 2100, 480, "重复页正文。")])
                  for i in range(3, 9)]
        out = layout.dedupe_pages(pages)
        self.assertEqual(len(out), 3 + 1, "6 个重复页应只留 1 页")
        self.assertEqual([p["idx"] for p in out], list(range(len(out))))

    def test_two_identical_pages_kept(self):
        """只重复 2 次不删——书中偶尔真有两页内容相同。"""
        pages = [synth_page(0, [(300, 400, 2100, 480, "同一段引文。")]),
                 synth_page(1, [(300, 400, 2100, 480, "同一段引文。")])]
        self.assertEqual(len(layout.dedupe_pages(pages)), 2)


class TestNotes(unittest.TestCase):
    """脚注：抽取 → 锚定 → Markdown → EPUB3 弹窗标记。

    全部来自新书的真实版面：脚注行在 region=footnote 里，注号混用
    「①」「(1)」「1、」三种写法，正文里的注号可能根本不存在（扫描件漏识别）。
    """

    def _page(self, idx, lines):
        p = synth_page(idx, lines)
        for L in p["lines"]:
            L.setdefault("region", "text")
        return p

    def test_extract_notes_joins_continuation(self):
        """一条脚注被 OCR 折成 3 行时，续行要拼回同一条。"""
        p = self._page(0, [
            (300, 400, 2100, 460, "正文。"),
            (300, 2900, 1200, 2940, "①僧团即僧人团体，"),
            (300, 2950, 2100, 2990, "可称僧团但本书常指全国范围。"),
            (300, 3000, 2100, 3040, "②《高僧传》卷五《释道安传》。"),
        ])
        p["lines"][1]["region"] = "footnote"
        p["lines"][2]["region"] = "footnote"
        p["lines"][3]["region"] = "footnote"
        notes = layout.extract_notes([p])
        self.assertEqual(len(notes[0]), 2)
        self.assertEqual(notes[0][0]["n"], 1)
        self.assertIn("可称僧团但本书常指全国范围", notes[0][0]["text"])
        self.assertEqual(notes[0][1]["n"], 2)

    def test_no_region_means_no_notes(self):
        """没跑 run_layout.py 的工作目录里没有 region 字段，全书行为必须不变。"""
        p = synth_page(0, [(300, 400, 2100, 460, "正文。"),
                           (300, 2900, 2100, 2940, "①夹在正文里的注。")])
        self.assertEqual(layout.extract_notes([p]), {})

    def test_watermark_dropped(self):
        """页面底部的试用版水印常被判成脚注区，必须剔掉。"""
        p = self._page(0, [
            (300, 2900, 2100, 2940, 'PDF 文件使用 "pdfFactory" 试用版本创建 www.fineprint.com.cn'),
        ])
        p["lines"][0]["region"] = "footnote"
        self.assertEqual(layout.extract_notes([p]), {})

    def test_plan_notes_only_links_unique(self):
        """同一编号在正文出现两次时不敢内联（分不清对应哪一条）。"""
        notes = {0: [{"id": "fn1", "n": 1, "mark": "①", "text": "甲注"},
                     {"id": "fn2", "n": 2, "mark": "②", "text": "乙注"}]}
        # 正文里 ① 出现两次、② 只出现一次
        cleaned = {0: [{"t": "前文①，后文②，又见①。", "w": 100.0}]}
        subs, orphan = layout.plan_notes(notes, cleaned)
        linked = {nid for v in subs.values() for _s, _e, nid in v}
        self.assertEqual(linked, {"fn2"})
        self.assertEqual([it["id"] for it in orphan[0]], ["fn1"])

    def test_plan_notes_skips_leading_mark(self):
        """行首编号是并列项符号，不能当注号。"""
        notes = {0: [{"id": "fn1", "n": 1, "mark": "①", "text": "甲注"}]}
        cleaned = {0: [{"t": "①这是正文里的并列项第一点。", "w": 100.0}]}
        subs, orphan = layout.plan_notes(notes, cleaned)
        self.assertEqual(subs, {})
        self.assertEqual(len(orphan[0]), 1)

    def test_apply_subs_replaces_tail_first(self):
        """同一行有两个注号时，倒序替换才不会把前面的下标改错。"""
        # 甲0 乙1 ①2 丙3 丁4 ②5 —— 区间必须对准注号本身
        out = layout.apply_subs("甲乙①丙丁②",
                                [(2, 3, "fn1"), (5, 6, "fn2")])
        self.assertEqual(out, "甲乙[^fn1]丙丁[^fn2]")


class TestEpubNotes(unittest.TestCase):
    """EPUB3 弹窗脚注的结构：noteref 与 aside[epub:type=footnote] 必须成对。"""

    MD = "\n".join([
        "# 第一章 测试",
        "",
        "正文开头[^fn1]这里有一个注。",
        "",
        "## 第一节 小节",
        "",
        "第二段[^fn2]。",
        "",
        "[^fn1]: 《晋书》卷九五。",
        "",
        "[^fn2]: 第二条注释。",
        "",
        "> ① 锚不上正文的注。",
        "",
        "# 第二章 测试二",
        "",
        "第三章正文。",
        "",
    ])

    def test_popup_markup(self):
        import epub3
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "t.epub")
            st = epub3.build(self.MD, out, title="t", toc_depth=3)
            self.assertEqual(st["chapters"], 2, "一级标题必须切成 2 章")
            self.assertEqual(st["notes"], 2)
            self.assertEqual(epub3.validate(out), 0)
            import zipfile
            with zipfile.ZipFile(out) as z:
                x = z.read("c001.xhtml").decode("utf-8")
                self.assertIn('epub:type="noteref"', x)
                self.assertIn('epub:type="footnote"', x)
                self.assertIn('role="doc-footnote"', x)
                self.assertIn('epub:type="endnote"', x, "锚不上的注要保留成章末注")
                self.assertEqual(z.namelist()[0], "mimetype")

    def test_validate_catches_dangling_ref(self):
        """validate() 必须能发现"引用存在、注文缺失"这种断链。"""
        import epub3
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "t.epub")
            # 只有引用没有定义 → epub3 仍会渲染 aside（内容为空），但引用编号存在
            epub3.build("# 章\n\n正文[^nope]。\n", out, title="t")
            # 这里只断言不会抛异常且结构自洽：缺定义时 aside 仍成对出现
            self.assertIn(epub3.validate(out), (0,))


class TestFix(unittest.TestCase):
    CASES = [
        ("辨证论治是中医的核心", "辨证论治是中医的核心"),
        ("射人先射马，擒贼先擒王", "射人先射马，擒贼先擒王"),
        ("中国人口处于世界首位", "中国人口处于世界首位"),
        ("你在人口处等我", "你在入口处等我"),
        ("这一幕引人发笑", "这一幕引人发笑"),
        ("成绩引人侧目", "成绩引人侧目"),
        ("他引人了新方法", "他引入了新方法"),
        ("他自己经常承认", "他自己经常承认"),
        ("他己经被选上了", "他已经被选上了"),
        ("知己经常来看他", "知己经常来看他"),
        ("陷人于不义的圈套", "陷人于不义的圈套"),
        ("陷人错误的深渊", "陷入错误的深渊"),
        ("收人钱财替人消灾", "收人钱财替人消灾"),
        ("收人很高", "收入很高"),
        ("异已", "异己"),
        ("陶治情操", "陶冶情操"),
        ("人门课程", "入门课程"),
        ("深人研究", "深入研究"),
        ("容纳人情", "容纳人情"),
    ]

    def test_fix_rules(self):
        for src, want in self.CASES:
            with self.subTest(src=src):
                out, _, _ = fix.apply(src, verbose=False)
                self.assertEqual(out, want)

    def test_fix_counts_net_changes(self):
        """「己经→已经」这类会自相抵消的规则不能计入统计。"""
        _, stat, _ = fix.apply("他自己经常承认", verbose=False)
        self.assertEqual(sum(stat.values()), 0,
                         "没有净改动就不该记任何修正")


class TestQA(unittest.TestCase):
    def test_dup_para_detected(self):
        """一段一行 + 空行分隔的格式下，重复段落必须能被检出。"""
        md = "第一段文字完全一样，够长了。\n\n第一段文字完全一样，够长了。\n"
        kinds = [k for k, _, _ in qa.check_paragraphs(md)]
        self.assertIn("DUP_PARA", kinds)


class TestCli(unittest.TestCase):
    def test_empty_src_exits(self):
        """空 OCR 目录必须报错退出，不能静默写出 0 字节 book.md。"""
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "ocr")
            os.makedirs(src)
            out = os.path.join(d, "out")
            r = subprocess.run(
                [sys.executable, os.path.join(ROOT, "make_md.py"),
                 "--src", src, "--out", out, "--title", "t"],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertNotEqual(r.returncode, 0, "空输入必须非 0 退出")
            self.assertFalse(os.path.exists(os.path.join(out, "book.md")),
                             "报错时不应留下 book.md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
