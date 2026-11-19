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

    def test_popup_markup_and_dangling_ref(self):
        """弹窗脚注的结构约定 + validate 必须能发现断链。

        旧版这里断言 validate==0——那正是被漏检的缺陷本身（引用无定义时
        aside 注文为空，阅读器弹出一个空浮层）。现在 validate 必须报错。
        """
        import epub3
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "t.epub")
            st = epub3.build(self.MD, out, title="t", toc_depth=3)
            self.assertEqual(st["chapters"], 2, "一级标题必须切成 2 章")
            self.assertEqual(st["notes"], 2)
            self.assertEqual(epub3.validate(out), 0)
            with zipfile.ZipFile(out) as z:
                x = z.read("c001.xhtml").decode("utf-8")
                self.assertIn('epub:type="noteref"', x)
                self.assertIn('epub:type="footnote"', x)
                self.assertIn('role="doc-footnote"', x)
                self.assertIn('epub:type="endnote"', x, "锚不上的注要保留成章末注")
                self.assertEqual(z.namelist()[0], "mimetype")

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


class TestPipelineFloor(unittest.TestCase):
    """md 阶段的两条底线：重复段落要能检出；空输入必须报错而不是写 0 字节。"""

    def test_dup_para_detected(self):
        """一段一行 + 空行分隔的格式下，重复段落必须能被检出。"""
        md = "第一段文字完全一样，够长了。\n\n第一段文字完全一样，够长了。\n"
        kinds = [k for k, _, _ in qa.check_paragraphs(md)]
        self.assertIn("DUP_PARA", kinds)

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

    def test_page_number_order_separates_toc_from_copyright(self):
        """只有一页目录的书：页码**严格递增**就是单页证据。

        真实案例《规训与惩罚》整本只有 1 页「目 录」，旧规则要求"相邻页也是目录"，
        于是永远判不出来，目录解析跟着抛异常。
        反例是版权页：它也有 ≥6 个行尾数字，但顺序乱——误判的代价是该页被整页
        跳过，正文静默消失。
        """
        pages = [self._page(i, [1, 2, 3]) for i in range(3)]
        pages.append(self._page(3, [3, 35, 81, 117, 153, 193, 219]))
        self.assertIn(3, layout.detect_toc_pages(pages, 60))

        pages = [self._page(i, [1, 2, 3]) for i in range(3)]
        pages.append(self._page(3, [9, 7, 59, 1975, 10, 1, 12]))
        self.assertNotIn(3, layout.detect_toc_pages(pages, 60))


class TestPartRank(unittest.TestCase):
    def test_rank_ordering(self):
        """层级名次：「第X部分」必须比「第X章」高一级，章 > 节 > 一、二、。

        部分与章曾共用名次 10，被压成同一级，16 条目录全变一级；
        而没有分部的书（僧官制度史）三档映射必须保持不变。
        """
        self.assertLess(layout._rank("第一部分 酷刑"),
                        layout._rank("第一章 犯人的肉体"))
        self.assertLess(layout._rank("第二编 某某"),
                        layout._rank("第二章 某某"))
        self.assertLess(layout._rank("第二章 南朝的僧官制度"),
                        layout._rank("第一节 中央和地方僧官"))
        self.assertLess(layout._rank("第一节 中央和地方僧官"),
                        layout._rank("一、尼僧僧官"))


class TestPageOffset(unittest.TestCase):
    def _numbered(self, idx, printed, w=2083, h=3355):
        p = synth_page(idx, [(300, 600, 800, 660, "正文一行")], w, h)
        p["lines"].append({"x0": 155, "y0": 312, "x1": 230, "y1": 373,
                           "t": str(printed), "s": 0.99, "region": "number"})
        return p

    def test_offset_learning_and_guards(self):
        """页码偏移：样本够、且页码不跳时才敢算；否则返回 None 走常规匹配。"""
        pages = [self._numbered(i, i - 5) for i in range(5, 40)]
        self.assertEqual(layout.page_offset(pages), 5)
        # 样本太少（小书）不敢猜
        self.assertIsNone(layout.page_offset([self._numbered(i, i - 5) for i in range(5, 15)]))
        # 页码乱跳的书不能按错误偏移对齐
        self.assertIsNone(layout.page_offset(
            [self._numbered(i, (i * 7) % 23) for i in range(40)]))


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

    def _star(self, body_marks, notes):
        pages = [self._page(0, body_marks, notes)]
        cleaned = {0: [L for L in pages[0]["lines"]
                       if L.get("region") != "footnote"]}
        return layout.extract_star_notes(pages, cleaned, 63.0, 62.6)

    def test_star_notes_pair_when_counts_match(self):
        """正文 1 个星号 + 注区 1 条 -> 内联成 [^fn] 弹窗脚注。"""
        notes, subs = self._star(["*"], [(400, "注释甲的内容足够长。")])
        self.assertEqual(len(notes.get(0, [])), 1)
        self.assertEqual(len(subs.get((0, 0), [])), 1)

    def test_star_notes_refuse_to_guess(self):
        """三种不可靠形态一律整页放弃：条数不符、边界切错、行首星号不是注号。"""
        # 正文 2 星号但注区只切出 1 块
        notes, subs = self._star(["*", "**"], [(400, "只有一条注文内容。")])
        self.assertEqual(notes, {})
        self.assertEqual(subs, {})

        # 块数对得上，但非末块不收句 -> 切错了边界
        notes, _ = self._star(["*", "**"], [(400, "第一条注文没有结束"),
                                           (520, "第二条注文结束了。")])
        self.assertEqual(notes, {})

        # 行首星号是列表符号，不是注号
        pages = [self._page(0, ["*"], [(400, "注释甲的内容足够长。")])]
        pages[0]["lines"][0]["t"] = "*行文内容继续"
        cleaned = {0: [L for L in pages[0]["lines"]
                       if L.get("region") != "footnote"]}
        notes, _ = layout.extract_star_notes(pages, cleaned, 63.0, 62.6)
        self.assertEqual(notes, {})


class TestHeaderRescue(unittest.TestCase):
    def test_header_label_vs_position(self):
        """位置证据优先于区域标签：被标成 header 但位置在页眉带以下的标题必须保留，
        真正的书眉（在页眉带内）仍然要丢。

        真实案例：「参考书目」被版面模型标成 header（y=20.4% 页高），
        照标签丢会让这一章从成书里凭空消失。
        """
        p = synth_page(0, [(700, 600, 1300, 700, "参考书目")])
        p["lines"][0]["region"] = "header"
        out = layout.clean_page(p, 63.0, 62.6, set(), band_top=0.09)
        self.assertEqual([L["t"] for L in out], ["参考书目"])

        p = synth_page(0, [(300, 312, 900, 373, "规训与惩罚")])
        p["lines"][0]["region"] = "header"
        out = layout.clean_page(p, 63.0, 62.6, set(), band_top=0.09)
        self.assertEqual(out, [])


class TestPageMap(unittest.TestCase):
    def test_page_map_offsets_and_ordering(self):
        """md 偏移必须算上 join 插入的分隔符；页码 1 基；表必须升序（audit 用二分）。

        漏算分隔符时偏移随块数单调偏小（实测 1100 个块 ≈ 2200 个分隔符），
        最终页码整体偏 3 页——「土气」记成 p360、实为 p357。
        """
        blocks = [("p", "甲" * 4, [(0, 4)]), ("p", "乙" * 4, [(0, 5)])]
        md, pm = layout.to_markdown_with_pages(blocks, {"title": "t"})
        # 每块 emit(txt) + emit("")，join 再插一个 \n：
        #   块1 "甲甲甲甲" 占 [0,4)，emit("") 再加 1 个分隔 -> 块2 从 5 开始
        self.assertEqual(md, "甲甲甲甲\n\n乙乙乙乙\n")
        self.assertEqual(pm[0][0], 0)
        self.assertEqual(pm[1][0], 5, "第二块起点必须算上分隔符")
        self.assertEqual(pm[0][1], 5)          # 页 idx 4 -> 页码 5
        self.assertEqual(pm[1][1], 6)          # 页 idx 5 -> 页码 6

        # 块里存的是 0 基页 idx，输出必须 +1（人翻书从 1 开始）
        _, pm = layout.to_markdown_with_pages([("p", "正文内容", [(0, 0)])],
                                              {"title": "t"})
        self.assertEqual(pm[0][1], 1)

        # 升序：audit.page_of 用二分查找
        blocks = [("p", "甲" * 5, [(0, 3), (2, 2)]), ("h", 1, "标题", 9)]
        _, pm = layout.to_markdown_with_pages(blocks, {"title": "t"})
        self.assertEqual(pm, sorted(pm, key=lambda x: x[0]))


class TestAuditPageLookup(unittest.TestCase):
    def test_page_of_lookup(self):
        """偏移落在哪个块就取哪块的页；没有映射时返回 None，不能崩。"""
        import audit
        pm = [(0, 10), (100, 11), (200, 12)]
        self.assertEqual(audit.page_of(pm, 0), 10)
        self.assertEqual(audit.page_of(pm, 99), 10)
        self.assertEqual(audit.page_of(pm, 100), 11)
        self.assertEqual(audit.page_of(pm, 250), 12)
        self.assertIsNone(audit.page_of(None, 5))
        self.assertIsNone(audit.page_of([], 5))


class TestFigureRegions(unittest.TestCase):
    """图 / 表区：不再静默丢弃。

    实测背景（《中国僧官制度史》350 页）：table region 138 行、image region 22 行，
    既不在 DROP_REGIONS 里、也没有任何裁图/聚合代码，于是 4 个表格页把每一行都
    独立成段，成书 1820 段里冒出 97 个 ≤6 字碎片（「共公僧」「卜座」），占 5.3%；
    3 个插图页的标注字同样当正文流出去。下面每条用例都钉一个具体缺陷。
    """

    def _page(self, idx, lines, regions=None, w=2480, h=3508):
        p = synth_page(idx, lines, w, h)
        if regions is not None:
            p["regions"] = regions
        return p

    def _run(self, pages, meta=None, **kw):
        """把页写进临时目录跑 layout.build，返回 (md, report, 输出目录)。"""
        import json
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        src = os.path.join(d, "ocr_layout")
        os.makedirs(src)
        for p in pages:
            q = dict(p)
            del q["idx"]
            with open(os.path.join(src, q["name"] + ".json"), "w", encoding="utf-8") as f:
                json.dump(q, f, ensure_ascii=False)
        meta = dict(meta or {})
        meta.setdefault("title", "t")
        meta.setdefault("out_dir", os.path.join(d, "out"))
        pages_dir = kw.pop("pages_dir", None)
        if pages_dir:
            meta["pages_dir"] = pages_dir
        md, rep, cleaned, ps = layout.build(src, meta)
        return md, rep, os.path.join(d, "out")

    def test_illustrations_are_cropped_in_place(self):
        """插图的三个硬要求：整页容器框不算插图；正文中间的图按**行位置**插入；
        裁不出图时不许在 md 里留死链。

        整页框：实测封面 image region 是 (54,78)-(2415,3394)，照它裁出来是一张
        重复的封面，还让书里凭空多一张假插图。
        按行插：插图常夹在段落中间，整页后置会让图跑到下一章去。
        死链：epub3.validate 会把它报成「图片不存在」，成品里是一串碎图占位符。
        """
        from PIL import Image
        # 整页容器框不算插图
        p = self._page(0, [(300, 400, 2100, 480, "封面上的字。")],
                       regions=[{"x0": 54, "y0": 78, "x1": 2415, "y1": 3394,
                                 "cls": "image", "s": 0.97}])
        self.assertEqual(layout.find_figures(p), [])

        # 正文中间的插图：裁文件 + 原位插入 + 图区 OCR 行不当正文
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        imgs = os.path.join(d, "pages")
        os.makedirs(imgs)
        Image.new("RGB", (2480, 3508), "white").save(os.path.join(imgs, "p-001.png"))
        p = self._page(0, [
            (300, 400, 2100, 480, "插图前面的正文一行。"),
            (400, 900, 2000, 1400, "图里的标注字"),
            (300, 2000, 2100, 2080, "插图后面的正文一行。"),
        ], regions=[{"x0": 350, "y0": 850, "x1": 2050, "y1": 1450,
                     "cls": "image", "s": 0.9}])
        md, rep, out = self._run([p], pages_dir=imgs)
        self.assertEqual(rep["regions"]["figures"], 1)
        self.assertEqual(rep["regions"]["images"]["made"], 1)
        self.assertTrue(os.path.exists(os.path.join(out, "images", "fig-p001-1.png")))
        lines = [l for l in md.split("\n") if l.strip()]
        img_at = next(i for i, l in enumerate(lines) if l.startswith("![]("))
        before = next(i for i, l in enumerate(lines) if "插图前面" in l)
        after = next(i for i, l in enumerate(lines) if "插图后面" in l)
        self.assertLess(before, img_at, "图必须在「插图前面」那段之后")
        self.assertLess(img_at, after, "图必须在「插图后面」那段之前")
        self.assertNotIn("图里的标注字", md, "图区里的 OCR 行不能再当正文流出去")

        # 缺页面图：宁可这张图缺席并告警，也不能产出坏链
        p = self._page(0, [(300, 400, 2100, 480, "正文。")],
                       regions=[{"x0": 350, "y0": 850, "x1": 2050, "y1": 1450,
                                 "cls": "image", "s": 0.9}])
        md, rep, _out = self._run([p], pages_dir=os.path.join(
            tempfile.gettempdir(), "no-such-pages-dir"))
        self.assertNotIn("![](", md, "裁不出图时不许留下指向不存在文件的死链")
        self.assertEqual(rep["regions"]["images"]["made"], 0)
        self.assertEqual(len(rep["regions"]["images"]["failed_detail"]), 1,
                         "失败要逐个报出来，不能只给一个数字")

    def test_table_region_grouped_and_toc_not_table(self):
        """表格区必须聚成一块（97 个碎片的来源）；但目录页不能被当成表格。

        目录行「标题 + 行尾页码」几何上和表格行完全一样——实测《规训与惩罚》
        目录页会被判成 2 块表，整个目录被并成两坨文字。
        """
        rows = [(300, 1900 + i * 80, 600, 1960 + i * 80, t)
                for i, t in enumerate(["共公僧", "池方僧官", "监导", "左街僧录"])]
        rows += [(1100, 1900 + i * 80, 1500, 1960 + i * 80, t)
                 for i, t in enumerate(["承玄署", "州郡僧正", "卜座", "州僧王"])]
        p = self._page(0, [(300, 400, 2100, 480, "上表说明如下。")] + rows,
                       regions=[{"x0": 250, "y0": 1850, "x1": 2200, "y1": 2260,
                                 "cls": "table", "s": 0.9}])
        md, rep, _ = self._run([p])
        self.assertEqual(rep["regions"]["tables"], 1)
        self.assertIn("共公僧　承玄署", md, "表格块里的行要用全角空格连成一整块")
        paras = [l.strip() for l in md.split("\n") if l.strip()]
        self.assertNotIn("共公僧", paras, "被合并的行不再作为独立段落出现")

        # 目录页：先被识别成目录，plan_regions 才整页跳过
        rows2 = [(400, 400, 1200, 470, "目 录")]
        for i, (title, pg) in enumerate([("第一章　犯人的肉体", 35),
                                        ("第二章　断头台的场面", 81),
                                        ("第三章　全景敞视主义", 219),
                                        ("第四章　非法活动", 290)]):
            y = 1200 + i * 90
            rows2.append((300, y, 1500, y + 60, title))
            rows2.append((1790, y, 1880, y + 60, str(pg)))
        p = self._page(0, rows2, regions=[{"x0": 280, "y0": 1150, "x1": 1900,
                                           "y1": 1600, "cls": "table", "s": 0.9}])
        _, rep, _ = self._run([p])
        self.assertEqual(rep["regions"]["tables"], 0)

    def test_plate_page_rule(self):
        """整页图版：页内没有任何正文区、行全是噪声 -> 整页裁成一张图；
        但保护条款必须拦住「版面模型整页失效时把正文页换成图」。"""
        from PIL import Image
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        imgs = os.path.join(d, "pages")
        os.makedirs(imgs)
        Image.new("RGB", (2480, 3508), "white").save(os.path.join(imgs, "p-001.png"))
        noise = [(300 + i * 40, 400 + i * 90, 380 + i * 40, 460 + i * 90, t)
                 for i, t in enumerate(["t--", "J.", "II", "±二", "M片L", "1:",
                                        "主三", "25", "iv"])]
        p = self._page(0, noise, regions=[{"x0": 1193, "y0": 114, "x1": 1590,
                                           "y1": 181, "cls": "header", "s": 0.9}])
        md, rep, out = self._run([p], pages_dir=imgs)
        self.assertEqual(rep["regions"]["plates"], 1)
        self.assertTrue(os.path.exists(os.path.join(out, "images", "fig-p001-1.png")))
        self.assertNotIn("t--", md)

        # 保护：整页都是足够长的正文 -> 不能换成图
        p = self._page(0, [(300, 400 + i * 80, 2100, 480 + i * 80,
                            "第%d行这是一段足够长的正文内容，不该被当成图版。" % i)
                           for i in range(10)],
                       regions=[{"x0": 1193, "y0": 114, "x1": 1590, "y1": 181,
                                 "cls": "header", "s": 0.9}])
        self.assertFalse(layout.plate_like(p, layout.clean_page(p, 63.0, 62.6, set())))

    def test_no_regions_and_crop_scaling(self):
        """两个回归锚点：没有 regions 的老目录一个图表都不该产出；
        页 JSON 坐标系与 PNG 分辨率不一致时裁图必须按比例换算。

        换算这条实测过：ocr_merged 的页 JSON 是 2480×3508，同目录 pages/*.png
        是 2594×3588，差 4.6%，不换算就裁偏几十像素——肉眼几乎看不出来。
        """
        from PIL import Image
        p = synth_page(0, [(300, 400, 2100, 480, "正文第一行足够长。"),
                           (300, 500, 2100, 580, "唐初"),
                           (300, 600, 2100, 680, "祠部")])
        md, rep, out = self._run([p])
        self.assertEqual(rep["regions"]["figures"], 0)
        self.assertEqual(rep["regions"]["tables"], 0)
        self.assertNotIn("![](", md)
        self.assertFalse(os.path.isdir(os.path.join(out, "images")))
        self.assertIn("唐初", md, "没有版面证据时短行仍是正文，不许擅自吞掉")

        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        big = os.path.join(d, "big.png")
        Image.new("RGB", (2480 * 2, 3508 * 2), "white").save(big)
        dst = os.path.join(d, "crop.png")
        size = layout.crop_figure(big, (100, 100, 300, 250), dst, (2480, 3508))
        self.assertIsNotNone(size)
        # bbox 宽 200、高 150；2 倍图上是 400x300，再加边距
        self.assertEqual(size[0], 400 + 2 * layout.CROP_PAD_X)
        self.assertEqual(size[1], 300 + 2 * layout.CROP_PAD_Y)


class TestEdgeShortLines(unittest.TestCase):
    """页顶/页底边缘短行的处置。

    历史包袱：这里曾用「窄(w<0.55W) + 短(≤16 字) + 在页顶 18%/页底 10%」
    当书眉判据，实测吃掉《中国僧官制度史》61 行 / 453 汉字、《黑格尔辩证法
    讲演录》20 行——正文折行后只占半页宽、又恰好落在页顶，就中招。
    真书眉在上游已被 learn_running_heads + head_band 删干净，这一层只该
    拦「非文字碎片」（孤立页码、OCR 符号）。
    """

    def _run(self, pages):
        import json
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        src = os.path.join(d, "ocr")
        os.makedirs(src)
        for p in pages:
            q = dict(p)
            del q["idx"]
            with open(os.path.join(src, q["name"] + ".json"), "w", encoding="utf-8") as f:
                json.dump(q, f, ensure_ascii=False)
        md, rep, cleaned, ps = layout.build(src, {"title": "t", "out_dir": os.path.join(d, "out")})
        return md, rep

    def _body_pages(self, first_page_lines):
        """第 1 页用给定行，后两页各不相同（避免被当成跨页重复的书眉）。"""
        pages = [synth_page(0, first_page_lines)]
        for i in (1, 2):
            pages.append(synth_page(i, [
                (300, 400, 2100, 480, "第%d页的第一行正文内容，长度正常。" % i),
                (300, 520, 2100, 600, "第%d页的第二行正文内容，长度正常。" % i),
            ]))
        return pages

    def test_body_short_lines_at_page_edges_survive(self):
        """页顶/页底的正文短折行不能被当成书眉残留删掉。

        旧判据「窄 + 短 + 在边缘」实测吃掉僧官 61 行/453 汉字、黑格尔 20 行。
        试过用「与书眉/章名/书名的 Dice 相似度」去救，救不回——那 61 行最高
        相似度只有 0.60（阈值 0.75 一行都过不了），它们全是正文。
        """
        top = "乏真正的人文关怀。这个扯得远了。"
        self.assertIn(top, self._run(self._body_pages([
            (300, 400, 1200, 460, top),                 # y1=0.13H, w=900<0.55W
            (300, 700, 2100, 780, "这一页的正文第二行，宽度正常，内容完整。"),
        ]))[0], "页顶的正文短折行被当成书眉删掉了")

        bottom = "乏真正的人文关怀。这个扯得远了。"
        self.assertIn(bottom, self._run(self._body_pages([
            (300, 400, 2100, 460, "这一页的正文第一行，宽度正常，内容完整。"),
            (300, 3180, 1200, 3240, bottom),           # y0=0.906H>0.90H, w=900<0.55W
        ]))[0], "页底的正文短折行被当成书眉删掉了")

    def test_true_fragments_are_still_dropped(self):
        """守住另一头：孤立页码与单符号必须继续被拦下。

        页码留在流里会粘进上一段末尾（实测「……都归他管11」）；
        单符号是 OCR 噪声（实测僧官 p240 的「°」）。
        """
        # 孤立页码：y1 取 0.13H，高于 clean_page 的页码门限（0.12H），
        # 所以只能靠 assemble 这一层的碎片判据拦下
        md, _ = self._run(self._body_pages([
            (300, 400, 2100, 460, "这一页的正文第一行，宽度正常，内容完整。"),
            (1840, 400, 1900, 456, "11"),              # w=60, y1=0.13H
        ]))
        paras = [l for l in md.split("\n") if l.strip()]
        self.assertNotIn("11", paras)
        self.assertNotIn("内容完整。11", md)

        pages = self._body_pages([
            (300, 400, 2100, 460, "这一页的正文第一行，宽度正常，内容完整。"),
            (300, 400, 360, 456, "°"),                 # elen<=1
        ])
        self.assertNotIn("°", self._run([pages[0]])[0])


class TestHeadLearnWindow(unittest.TestCase):
    """learn_running_heads 的扫描窗口与「窄行守门」。

    《规训与惩罚》的章节书眉落在 y1=10.1%~13.4% 页高，旧窗口 0.11H 只学到
    「书名」那一条，11 条章节书眉漏掉 9 条；而 head_band 有 0.09 硬上限，
    这些书眉在带状区里也无处可拦，于是「第二章 断头台的场面第一章」这种
    残片拼进了正文。窗口放宽到 0.18H 后能全部学到。
    """

    def _pages_with_head(self, head, y1, n=6, w=700):
        """n 页，每页顶部都有同一窄行书眉，正文各不相同。"""
        pages = []
        for i in range(n):
            pages.append(synth_page(i, [
                (300, y1 - 56, 300 + w, y1, head),
                (300, 700, 2100, 780, "第%d页的正文第一行，长度正常。" % i),
                (300, 820, 2100, 900, "第%d页的正文第二行，长度正常。" % i),
            ]))
        return pages

    def test_window_and_wide_line_guard(self):
        """书眉学习：窗口要够深（0.18H），但宽行绝不能学成书眉。

        窗口：《规训与惩罚》的章节书眉落在 y1=10.1%~13.4% 页高，旧窗口 0.11H
        只学到「书名」那一条，11 条章节书眉漏掉 9 条；head_band 有 0.09 硬上限，
        这些书眉在带状区里也无处可拦，于是「第二章 断头台的场面第一章」这种
        残片拼进了正文。
        守门：学到的文本在 clean_page 里是**不看位置一律删**的，误学一个正文
        长句就是全书丢句。书眉实测占 0.23~0.44 页宽，正文整行占 0.67~0.75。
        """
        head = "第一部分第一章 犯人的肉体"
        pages = self._pages_with_head(head, 456)          # 456/3508 = 0.13
        _bw, bh = layout.metrics(pages)
        heads = layout.learn_running_heads(pages, bh)
        self.assertIn(layout.norm(head), heads, "y1=13% 页高的书眉也要学到")

        # 学到之后要真的从正文流里消失（学而不删等于没学）
        bw, bh = layout.metrics(pages)
        bt, bb = layout.head_band(pages, heads, bh)
        self.assertLess(bt, 456 / 3508, "书眉不在带状区内——本用例的前提")
        cl = layout.clean_page(pages[0], bw, bh, heads, band_top=bt, band_bot=bb)
        texts = [L["t"] for L in cl]
        self.assertNotIn(head, texts)
        self.assertIn("第0页的正文第一行，长度正常。", texts)

        # 宽行哪怕跨页重复也不能学成书眉
        wide = "这一行是正文整行，占满版心，跨页重复也只可能是正文。"
        wide_pages = []
        for i in range(6):
            wide_pages.append(synth_page(i, [
                (300, 400, 2200, 460, wide),           # w=1900 > 0.55*2480
                (300, 700, 2100, 780, "第%d页的正文第一行，长度正常。" % i),
            ]))
        _bw, bh = layout.metrics(wide_pages)
        self.assertNotIn(layout.norm(wide),
                         layout.learn_running_heads(wide_pages, bh))


if __name__ == "__main__":
    unittest.main(verbosity=2)
if __name__ == "__main__":
    unittest.main(verbosity=2)
