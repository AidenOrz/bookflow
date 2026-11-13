"""bookflow 版面逻辑的最小回归测试。

这些用例全部来自真实踩坑，不是构造出来的假数据：
- test_head_band_does_not_eat_body：旧书 5 页正文首行被硬编码 10.5% 阈值吃掉
- test_title_residue_consumed：第五讲三行标题的第三行漏进正文
- test_title_residue_keeps_body：收紧判据后，正文末尾的「的。」不能被当成残片
- test_dup_pages_removed：新书源 PDF 末尾 19 页重复，去重后不能成段重复
- test_empty_src_exits：空输入必须报错，不能静默产出 0 字节成书
- test_fix_rules：fix.py 的两级规则（整词级自动改 / 单字级带排除表）

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
        """validate() 必须能发现"引用存在、注文缺失"这种断链。

        旧版这里断言 validate==0——那正是被漏检的缺陷本身（引用无定义时
        aside 注文为空，阅读器弹出一个空浮层）。现在 validate 必须报错。
        """
        import epub3
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "t.epub")
            epub3.build("# 章\n\n正文[^nope]。\n", out, title="t")
            self.assertGreater(epub3.validate(out), 0,
                               "引用无定义必须被 validate 检出，不能静默放过")


class TestFix(unittest.TestCase):
    """fix.py 分两级：整词级自动改，单字级只出候选。

    分级理由（实测）：单字级「引人/收人」的排除表是开放集，堵不住「引人人胜」
    「税收人说」「不引人怀疑」——这三处曾在《规训与惩罚》成书里被改坏并落盘。
    """

    # 自动组：错写法本身不是中文词，改是安全的
    AUTO_CASES = [
        ("人门课程", "入门课程"),
        ("异已", "异己"),
        ("陶治情操", "陶冶情操"),
        ("推祟", "推崇"),
        ("自已", "自己"),
        ("时侯", "时候"),
        # 「引人人胜」只有一个正确写法；必须先于「引人→引入」候选生效，
        # 否则会被建议改成更不存在的「引入人胜」。
        ("这本引人人胜的小册子", "这本引人入胜的小册子"),
    ]

    # 非自动组：只出候选，正文必须原样不动
    KEEP_CASES = [
        ("辨证论治是中医的核心", "辨证论治是中医的核心"),
        ("射人先射马，擒贼先擒王", "射人先射马，擒贼先擒王"),
        ("中国人口处于世界首位", "中国人口处于世界首位"),
        ("这一幕引人发笑", "这一幕引人发笑"),
        ("成绩引人侧目", "成绩引人侧目"),
        ("他引人了新方法", "他引人了新方法"),
        ("他自己经常承认", "他自己经常承认"),
        ("他己经被选上了", "他己经被选上了"),
        ("知己经常来看他", "知己经常来看他"),
        ("陷人于不义的圈套", "陷人于不义的圈套"),
        ("收人钱财替人消灾", "收人钱财替人消灾"),
        ("容纳人情", "容纳人情"),
        ("不引人怀疑", "不引人怀疑"),
        ("税收人说", "税收人说"),
    ]

    def test_auto_rules_apply(self):
        for src, want in self.AUTO_CASES:
            with self.subTest(src=src):
                out, _, _ = fix.apply(src, verbose=False)
                self.assertEqual(out, want)

    def test_conservative_never_touches_text(self):
        """conservative=True 时正文必须原样返回——遇到没见过的排版靠这个兜底。"""
        for src, want in self.KEEP_CASES:
            with self.subTest(src=src):
                out, _, _ = fix.apply(src, verbose=False, conservative=True)
                self.assertEqual(out, want)

    def test_known_bad_exclusions_are_fixed_by_default(self):
        """默认模式下，实测踩过的三处误改必须被修好（不是留着当候选）。"""
        self.assertEqual(fix.apply("那本引人人胜的小册子", verbose=False)[0],
                         "那本引人入胜的小册子")
        # 「不引人怀疑」「税收人说」默认不改（它们在候选组里、排除表已能挡住），
        # 关键是**不能**被改成「不引入怀疑」「税收入说」。
        self.assertEqual(fix.apply("不引人怀疑", verbose=False)[0], "不引人怀疑")
        self.assertEqual(fix.apply("税收人说", verbose=False)[0], "税收人说")

    def test_yinren_exclusion_is_char_class_not_phrase_alternation(self):
        """「引人」的排除表是**字符类**（按后一个字拦），不是词组分支，这是刻意保留的。

        起因是笔误，但实测下来字符类严格更安全：OCR 把「引人注目」认成「引人注日」时，
        词组分支 `(?:注目|入胜|...)` 拦不住（「注日」不在表里），默认模式会把正确原文
        改成「引入注日」——实测僧官成书变动 1 处就是这处。字符类按第二字「注」就挡住了。
        代价是白名单外的搭配一并被放过（引人思乡/引人往返），方向是漏改而非改坏。
        """
        pat = [p for p, _, label in fix.CAND_RULES if label == "引人→引入"][0]
        # 按第二字拦得住 OCR 认错的词
        self.assertFalse(pat.search("引人注日之处"), "字符类必须挡住 OCR 认错第二字")
        # 真正该改的仍然命中
        self.assertTrue(pat.search("他引人了新方法"))
        # 代价：白名单外的正确搭配被顺带放过（漏改方向，不会改坏正文）。
        # 「思乡」「往返」不在白名单里；「思考」也不是词组，但「思」是「深思」的首字，
        # 字符类不看词边界，所以一并放过——这正是可以接受的漏改。
        self.assertFalse(pat.search("引人思乡"))
        self.assertFalse(pat.search("引人往返"))
        self.assertFalse(pat.search("引人思考"))

    def test_candidates_survive_as_detail(self):
        """无论改不改，每一处命中都要留痕，供事后核对（suggest 说明会变成什么）。"""
        _, _, det = fix.apply("他引人了新方法", verbose=False, conservative=True)
        cand = fix.candidates(det)
        self.assertEqual(len(cand), 1)
        self.assertEqual(cand[0]["rule"], "引人→引入")
        self.assertIn("引入", cand[0]["suggest"])
        self.assertEqual(fix.auto_detail(det), [], "保守模式不该有 fix 条目")

        # 默认模式下它被真正改掉，明细里 action 变 fix，但 comment 仍保留建议
        out, stat, det2 = fix.apply("他引人了新方法", verbose=False)
        self.assertEqual(out, "他引入了新方法")
        self.assertEqual(stat.get("引人→引入"), 1)
        self.assertEqual(len(fix.auto_detail(det2)), 1)

    def test_fix_counts_net_changes(self):
        """「己经→已经」降级为候选后，统计里不能再出现净改动为 0 的条目。"""
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


class TestTocDetect(unittest.TestCase):
    def _page(self, idx, tails, w=2083, h=3355):
        """构造一页，行尾是数字（模拟页尾页码列）。"""
        lines = []
        for i, n in enumerate(tails):
            y = 1400 + i * 80
            lines.append((300, y, 800, y + 60, "某章某节"))
            lines.append((1760, y, 1870, y + 60, str(n)))
        return synth_page(idx, lines, w, h)

    def test_single_page_toc_is_detected(self):
        """只有一页目录的书：页码严格递增就是单页证据。

        真实案例《规训与惩罚》——整本只有 1 页「目 录」，旧规则要求
        "相邻页也是目录"，于是永远判不出来，目录解析跟着抛异常。
        """
        pages = [self._page(i, [1, 2, 3]) for i in range(3)]
        pages.append(self._page(3, [3, 35, 81, 117, 153, 193, 219]))
        toc = layout.detect_toc_pages(pages, 60)
        self.assertIn(3, toc)

    def test_copyright_page_not_toc(self):
        """版权页也有 ≥6 个行尾数字，但顺序是乱的，不能判成目录。

        误判的代价：该页会被整页跳过，正文静默消失。
        """
        pages = [self._page(i, [1, 2, 3]) for i in range(3)]
        pages.append(self._page(3, [9, 7, 59, 1975, 10, 1, 12]))
        self.assertNotIn(3, layout.detect_toc_pages(pages, 60))


class TestPartRank(unittest.TestCase):
    def test_part_outranks_chapter(self):
        """「第X部分」必须比「第X章」高一级。

        两者曾共用名次 10，被压成同一级，16 条目录全变一级。
        """
        self.assertLess(layout._rank("第一部分 酷刑"),
                        layout._rank("第一章 犯人的肉体"))
        self.assertLess(layout._rank("第二编 某某"),
                        layout._rank("第二章 某某"))

    def test_chapter_and_section_unchanged(self):
        """没有分部的书（僧官制度史）三档映射必须保持不变。"""
        r_ch = layout._rank("第二章 南朝的僧官制度")
        r_sec = layout._rank("第一节 中央和地方僧官")
        r_num = layout._rank("一、尼僧僧官")
        self.assertLess(r_ch, r_sec)
        self.assertLess(r_sec, r_num)


class TestPageOffset(unittest.TestCase):
    def _numbered(self, idx, printed, w=2083, h=3355):
        p = synth_page(idx, [(300, 600, 800, 660, "正文一行")], w, h)
        p["lines"].append({"x0": 155, "y0": 312, "x1": 230, "y1": 373,
                           "t": str(printed), "s": 0.99, "region": "number"})
        return p

    def test_offset_is_learned(self):
        """pdf_idx = printed + 5 时，offset 必须算成 5。"""
        pages = [self._numbered(i, i - 5) for i in range(5, 40)]
        self.assertEqual(layout.page_offset(pages), 5)

    def test_offset_needs_enough_samples(self):
        """样本太少时不敢猜，返回 None（小书宁可走常规匹配）。"""
        pages = [self._numbered(i, i - 5) for i in range(5, 15)]
        self.assertIsNone(layout.page_offset(pages))

    def test_offset_rejects_scattered_pages(self):
        """页码乱跳的书不能按错误偏移对齐。"""
        pages = [self._numbered(i, (i * 7) % 23) for i in range(40)]
        self.assertIsNone(layout.page_offset(pages))


class TestStarNotes(unittest.TestCase):
    def _page(self, idx, body_marks, notes, w=2083, h=3355):
        lines = [(300, 600 + i * 80, 1750, 660 + i * 80,
                  "行文内容%s继续" % mk)
                 for i, mk in enumerate(body_marks)]
        p = synth_page(idx, lines, w, h)
        for i, (x0, t) in enumerate(notes):
            p["lines"].append({"x0": x0, "y0": 2800 + i * 70, "x1": 1750,
                               "y1": 2860 + i * 70, "t": t, "s": 0.99,
                               "region": "footnote"})
        return p

    def test_star_notes_pair_when_counts_match(self):
        """正文 1 个星号 + 注区 1 条 -> 内联成 [^fn] 弹窗脚注。"""
        pages = [self._page(0, ["*"], [(400, "注释甲的内容足够长。")])]
        cleaned = {0: [L for L in pages[0]["lines"]
                       if L.get("region") != "footnote"]}
        notes, subs = layout.extract_star_notes(pages, cleaned, 63.0, 62.6)
        self.assertEqual(len(notes.get(0, [])), 1)
        self.assertEqual(len(subs.get((0, 0), [])), 1)

    def test_star_notes_skip_on_count_mismatch(self):
        """正文 2 星号但注区只切出 1 块 -> 整页放弃，不能瞎配。"""
        pages = [self._page(0, ["*", "**"], [(400, "只有一条注文内容。")])]
        cleaned = {0: [L for L in pages[0]["lines"]
                       if L.get("region") != "footnote"]}
        notes, subs = layout.extract_star_notes(pages, cleaned, 63.0, 62.6)
        self.assertEqual(notes, {})
        self.assertEqual(subs, {})

    def test_star_notes_skip_on_bad_boundary(self):
        """块数对得上，但非末块不收句 -> 切错了边界，整页放弃。"""
        pages = [self._page(0, ["*", "**"],
                            [(400, "第一条注文没有结束"),
                             (520, "第二条注文结束了。")])]
        cleaned = {0: [L for L in pages[0]["lines"]
                       if L.get("region") != "footnote"]}
        notes, subs = layout.extract_star_notes(pages, cleaned, 63.0, 62.6)
        self.assertEqual(notes, {})

    def test_leading_star_ignored(self):
        """行首星号是列表符号，不是注号。"""
        pages = [self._page(0, ["*"], [(400, "注释甲的内容足够长。")])]
        pages[0]["lines"][0]["t"] = "*行文内容继续"
        cleaned = {0: [L for L in pages[0]["lines"]
                       if L.get("region") != "footnote"]}
        notes, subs = layout.extract_star_notes(pages, cleaned, 63.0, 62.6)
        self.assertEqual(notes, {})


class TestHeaderRescue(unittest.TestCase):
    def test_title_below_band_survives_header_label(self):
        """被标成 header、但位置远在页眉带以下的标题必须保留。

        真实案例：「参考书目」被版面模型标成 header（y=20.4% 页高），
        照标签丢会让这一章从成书里凭空消失。
        """
        p = synth_page(0, [(700, 600, 1300, 700, "参考书目")])
        p["lines"][0]["region"] = "header"
        out = layout.clean_page(p, 63.0, 62.6, set(), band_top=0.09)
        self.assertEqual([L["t"] for L in out], ["参考书目"])

    def test_real_running_head_still_dropped(self):
        """真正的书眉（在页眉带内）仍然要丢。"""
        p = synth_page(0, [(300, 312, 900, 373, "规训与惩罚")])
        p["lines"][0]["region"] = "header"
        out = layout.clean_page(p, 63.0, 62.6, set(), band_top=0.09)
        self.assertEqual(out, [])


class TestPageMap(unittest.TestCase):
    def test_page_map_offsets_account_for_join_separators(self):
        """md 偏移必须算上 join 插入的分隔符。

        漏算时偏移随块数单调偏小（实测 1100 个块 ≈ 2200 个分隔符），
        最终页码整体偏 3 页——「土气」记成 p360、实为 p357。
        """
        blocks = [
            ("p", "甲" * 4, [(0, 4)]),
            ("p", "乙" * 4, [(0, 5)]),
        ]
        md, pm = layout.to_markdown_with_pages(blocks, {"title": "t"})
        # 每块 emit(txt) + emit("")，join 再插一个 \n：
        #   块1 "甲甲甲甲" 占 [0,4)，emit("") 再加 1 个分隔 -> 块2 从 5 开始
        self.assertEqual(md, "甲甲甲甲\n\n乙乙乙乙\n")
        self.assertEqual(pm[0][0], 0)
        self.assertEqual(pm[1][0], 5, "第二块起点必须算上分隔符")
        self.assertEqual(pm[0][1], 5)          # 页 idx 4 -> 页码 5
        self.assertEqual(pm[1][1], 6)          # 页 idx 5 -> 页码 6

    def test_page_number_is_one_based(self):
        """块里存的是 0 基页 idx，输出必须 +1（人翻书从 1 开始）。"""
        blocks = [("p", "正文内容", [(0, 0)])]
        _, pm = layout.to_markdown_with_pages(blocks, {"title": "t"})
        self.assertEqual(pm[0][1], 1)

    def test_page_map_is_sorted(self):
        """audit.page_of 用二分查找，必须升序。"""
        blocks = [("p", "甲" * 5, [(0, 3), (2, 2)]), ("h", 1, "标题", 9)]
        _, pm = layout.to_markdown_with_pages(blocks, {"title": "t"})
        self.assertEqual(pm, sorted(pm, key=lambda x: x[0]))


class TestAuditPageLookup(unittest.TestCase):
    def test_page_of_picks_covering_block(self):
        """偏移落在哪个块就取哪块的页。"""
        import audit
        pm = [(0, 10), (100, 11), (200, 12)]
        self.assertEqual(audit.page_of(pm, 0), 10)
        self.assertEqual(audit.page_of(pm, 99), 10)
        self.assertEqual(audit.page_of(pm, 100), 11)
        self.assertEqual(audit.page_of(pm, 250), 12)

    def test_page_of_without_map(self):
        """没有页码映射时返回 None，不能崩。"""
        import audit
        self.assertIsNone(audit.page_of(None, 5))
        self.assertIsNone(audit.page_of([], 5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
