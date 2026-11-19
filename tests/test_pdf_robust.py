"""两个「换一本书就炸」的鲁棒性缺陷（2026-11-16，实测《拉康式主体》）。

这本书（上海文艺 2024，PDFPatcher 1.0.3 生成，310 页）同时触发了两个与内容
无关的缺陷，都不在这套流程此前跑过的三本书的覆盖范围里：

1. **叠字图层让正文整段重复**。它的文字层里每页都有两套完整文字：一套是排版
   真实的整行文本（Droid Sans Fallback 16.5pt），一套是把同一行切碎成单个/几个
   字的小框（SimSun 11pt）。`dedupe_words` 是**逐对相邻**比较，两套的小框同基线
   并列、文本各不相同，整行大框又夹在这串之后且与紧邻的小框不等长——逐对比较
   永远判不出来。实测成书 261,359 汉字，而 OCR 认出的真实值是 146,866，**膨胀
   1.78 倍**，正文每句都念了两遍。

2. **打印中文时按 GBK 编码，遇到非 GBK 字符整个脚本崩掉**。这本书正文里有
   Übertragung 的 Ü、Écrits 的 É、ça 的 ç、∅ ∀ ∃、S₁ S₂，全书 155 行含这类
   字符。`repair.py` 把补识别对照行打给人看，崩在 `print()` 上；而它此时已经
   写完 129/310 页，`bookflow.py all` 看到退出码 1 判定整个阶段失败，**剩余
   181 页一个都没写**——识别全都成功了，只是话说不出口。

用例构造原则：全部用**这本真书的坐标形状**（字高 11 vs 16.5、基线差 5pt、
x 跨度差 1~2pt），不写"假想的漂亮数据"。两个缺陷的判据都要能被反例打穿：
把修复前的逻辑放回去，这些用例必须红。
"""
import io
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import layout      # noqa: E402
import stdio       # noqa: E402
import textlayer   # noqa: E402

PY = sys.executable


def word(x0, x1, y0, y1, t):
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "t": t}


def two_layer_page(pieces, whole, y_big=None, dy=5.0):
    """造一页「逐字小框 + 整行大框」的叠字图层，坐标用真书实测形状。

    pieces：[(x0, x1, 文本)]，字高 11pt（SimSun 那套）
    whole ：整行文本，字高 16.5pt（Droid Sans 那套），x 跨度覆盖所有 pieces
    """
    ws = []
    base = 105.8                       # 小框基线（实测 p-011 第一行）
    for x0, x1, t in pieces:
        ws.append(word(x0, x1, base - 11.0, base, t))
    big_base = base + dy               # 大框基线比小框高 4~7pt（实测 4.6~4.8 最常见）
    if y_big is not None:
        big_base = y_big
    x0 = min(p[0] for p in pieces) - 2.0
    x1 = max(p[1] for p in pieces) + 2.0
    ws.append(word(x0, x1, big_base - 16.5, big_base, whole))
    return {"w": 500.0, "h": 796.0, "words": ws}


# 真书 p-011 第一行的实际形状（缩略到 5 个片段 + 1 个整行框）
P011_PIECES = [(61.4, 72.4, "回到"), (72.4, 83.4, "精神"), (83.4, 94.4, "分析"),
               (94.4, 105.4, "本身"), (105.4, 116.4, "来说")]
P011_WHOLE = "回到精神分析本身来说"


class TestTwoLayerPdf(unittest.TestCase):
    """缺陷 1：同一页两套完整文字 → 正文重复一遍。"""

    def _texts(self, page, dpi=72):
        return [L["t"] for L in textlayer.page_lines(page, dpi)]

    def test_two_layers_are_split_then_collapsed(self):
        """叠字图层的核心路径：先按字高拆成两条候选，再按几何合并成一条。

        不先拆的话 group_lines 会把两套并进同一行，拼出来是
        「…来说回到精神分析本身来说」这种黏连；不合并的话成书里每句话念两遍
        （实测真书 288/310 页中招，261,359 汉字 vs 真实值 146,866）。
        """
        page = two_layer_page(P011_PIECES, P011_WHOLE)
        texts = self._texts(page)
        self.assertEqual(texts, [P011_WHOLE], "叠字图层必须合并成一条，实际=%r" % (texts,))

        rows = textlayer.group_lines(textlayer.dedupe_words(page["words"]), 3.0)
        parts = []
        for r in rows:
            parts.extend(textlayer.split_mixed_height_row(r))
        self.assertEqual(len(parts), 2, "应在字高间隙处拆成两条候选，实际=%d 条" % len(parts))
        self.assertEqual(sorted(len(p) for p in parts), [1, 5], "小框簇 5 词、大框簇 1 词")

    def test_negative_cases_never_split(self):
        """拆行的两个反例：行尾注号、单层 PDF。拆错任何一条都会毁掉大量正文。"""
        # 行尾上标注号只占最右边一点点宽度，不许触发拆行
        ws = [word(60, 71, 94.8, 105.8, "回"), word(71, 82, 94.8, 105.8, "到"),
              word(82, 93, 94.8, 105.8, "精"), word(93, 104, 94.8, 105.8, "神"),
              # 与正文同基线（同基线才考验"按字高拆"这条规则本身）
              word(430, 436, 99.8, 105.8, "1")]
        row = textlayer.group_lines(textlayer.dedupe_words(ws), 3.0)[0]
        self.assertEqual(len(textlayer.split_mixed_height_row(row)), 1,
                         "行尾注号的 x 跨度远小于行宽，不许拆")

        # 单层 PDF（三本参照书的形状）：拆行与合并都必须零触发
        ws = [word(60 + i * 11, 71 + i * 11, 94.8, 105.8, ch)
              for i, ch in enumerate("回到精神分析本身来说")]
        page = {"w": 500.0, "h": 796.0, "words": ws}
        rows = textlayer.group_lines(textlayer.dedupe_words(ws), 3.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(textlayer.split_mixed_height_row(rows[0])), 1,
                         "同一字高的整行不是叠字图层，不许拆")
        self.assertEqual(len(textlayer.page_lines(page, 72)), 1)

    def test_collapse_guards(self):
        """合并的三个守卫：短行不合、跨度不够不合、合并时留信息多的那份。"""
        # 页码/注号这种短行不许合并——实测残留的 428 对几乎全是「VI」vs「VII」
        short = [
            {"x0": 60.0, "y0": 741.0, "x1": 84.0, "y1": 752.0, "t": "VI"},
            {"x0": 62.0, "y0": 741.0, "x1": 84.0, "y1": 767.0, "t": "VII"},
        ]
        self.assertEqual(len(textlayer.collapse_duplicate_rows(short)), 2)

        # 跨度过半不算叠字图层。门槛从 0.98 放宽到 0.90 只多合 36 对且全是假的：
        # 「1/0组合。」vs「1/0组合。」把正确文本和漏字文本混在一起
        half = [
            {"x0": 60.0, "y0": 100.0, "x1": 200.0, "y1": 116.5, "t": "同一行文字的第一段内容在这里"},
            {"x0": 150.0, "y0": 104.0, "x1": 300.0, "y1": 120.5, "t": "同一行文字的第二段内容在这里"},
        ]
        self.assertEqual(len(textlayer.collapse_duplicate_rows(half)), 2,
                         "两条只互相覆盖一半，不是叠字图层，不该合并")

        # 合并时留信息多的那份：实测大框（整行）漏字比小框少
        both = [
            {"x0": 60.0, "y0": 100.0, "x1": 400.0, "y1": 111.0,
             "t": "翻译在历史上就是紧密交织的。事20世纪初弗洛伊德"},          # 小框，漏字
            {"x0": 58.0, "y0": 96.0, "x1": 402.0, "y1": 112.5,
             "t": "翻译在历史上就是紧密交织的。事实上，早在20世纪初弗洛伊德"},   # 大框，完整
        ]
        out = textlayer.collapse_duplicate_rows(both)
        self.assertEqual(len(out), 1)
        self.assertIn("事实上", out[0]["t"])


class TestStdioEncoding(unittest.TestCase):
    """缺陷 2：中文输出按 GBK 编码，遇到非 GBK 字符整个脚本崩掉。"""

    # 真书里出现的、GBK 编不进去的字符（逐个实测过）
    NON_GBK = "ÜÉç∅∀∃₁₂äï"

    def test_non_gbk_chars_and_stdio_survive(self):
        """先证明这些字符确实不在 GBK 里，再证明装了 utf8_stdio 之后打得出来。

        没有前半段，后半段就是空的：GBK 本来就编得进去的字符，怎么测都过。
        """
        for ch in self.NON_GBK:
            with self.subTest(ch=ch):
                with self.assertRaises(UnicodeEncodeError):
                    ch.encode("gbk")

        code = ("import sys; sys.path.insert(0, %r); import stdio; "
                "stdio.utf8_stdio(); "
                "print(%r)" % (ROOT, self.NON_GBK))
        cp = subprocess.run([PY, "-c", code], stdout=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
        self.assertEqual(cp.returncode, 0, "打印非 GBK 字符不许让进程失败")
        self.assertEqual(cp.stdout.strip(), self.NON_GBK)

    def test_entry_points_and_fallback(self):
        """每个打印用户文本的脚本都要在入口调一次；实在拿不走 UTF-8 时退化成 ?。

        这不是"约定"，是实测漏过：repair.py 没调，崩在第 129 页，剩下 181 页
        一个都没写。所以这里直接扫源码，少一个就红。
        """
        need = ["repair.py", "audit.py", "disagree.py", "apply_decisions.py",
                "make_review.py", "cmp_ocr.py", "check_epub.py", "cover.py",
                "make_md.py", "make_epub.py", "run_layout.py", "run_ocr.py"]
        for name in need:
            with self.subTest(script=name):
                with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                    src = f.read()
                self.assertIn("stdio.utf8_stdio()", src,
                              "%s 必须在入口调 stdio.utf8_stdio()" % name)

        # textlayer._utf8_stdio 这个名字有调用方在用，不能删
        self.assertTrue(callable(textlayer._utf8_stdio))

        # errors="replace" 兜底：老终端拿不走 UTF-8 也只是显示成 ?，不许抛
        buf = io.BytesIO()
        stream = io.TextIOWrapper(buf, encoding="gbk", errors="replace")
        stream.write("Ü")
        stream.flush()
        self.assertEqual(buf.getvalue(), b"?")


class TestTocSlashPageNumber(unittest.TestCase):
    """缺陷 3：目录页码写成「标题 /页码」时整页解析崩掉。

    这本书的目录页整页都是这种排版（「第3部分拉康式对象：爱、欲望、享乐 /113」
    「总序 /III」）。旧判据要求页码在页面右半侧（x0 > 0.5W）或前面有引导点，
    而这里整行是**一个** OCR 行框、x0 在版心左边界、也没有省略号，于是每条都
    被判成"没有页码的折行"，一层层拼到下一条上——13 条目录压成 12 条怪物，
    层级全错、命中 0，成书没有任何章节结构。
    """

    def _toc_page(self, titles):
        """造一页目录：每行一个独立行框，x0 在左侧，行尾「 /页码」或「/III」。"""
        lines = []
        y = 100.0
        for t in titles:
            lines.append({"x0": 96.0, "y0": y, "x1": 96.0 + 18 * len(t), "y1": y + 45.8,
                          "t": t, "s": 1.0})
            y += 80.0
        return lines

    def _parse(self, titles):
        lines = self._toc_page(titles)
        pages = [{"idx": 0, "w": 2480.0, "h": 3508.0, "lines": lines}]
        return layout.parse_toc(pages, {0: lines}, 45.8, 45.8, [0])

    def test_slash_page_numbers_are_recognized(self):
        r = self._parse(["第3部分拉康式对象：爱、欲望、享乐 /113",
                         "7 对象（a）：欲望原因/115",
                         "8 没有性关系这回事 /138"])
        self.assertEqual([e["page"] for e in r], [113, 115, 138],
                         "行尾「/页码」必须被认成页码，实际=%r" % (r,))
        # 标题里不该残留分隔符和页码
        for e in r:
            self.assertNotIn("/", e["title"])
            self.assertFalse(e["title"].rstrip().endswith(("113", "115", "138")),
                             "页码不能留在标题里：%r" % e["title"])

    def test_roman_numeral_page_numbers(self):
        """前言/序常编成「总序 /III」，罗马数字也要认。"""
        r = self._parse(["总序 /III", "致谢 /XI", "前言 /XIII"])
        self.assertEqual([e["page"] for e in r], [3, 11, 13])

    def test_slash_without_page_is_not_page_number(self):
        """正文行尾的普通斜杠不算页码——三本书 28,471 行里这条规则零误伤。

        注意这两行会被当成同一条（第二行没有层级前缀，视为折行续接），
        所以断言的是"没有任何条目拿到页码"，不是"条目数"。
        """
        for text in ("他写道 1/2 是这样的",
                     "参见 https://example.com/a",
                     "比例 3/4 与 5/6"):
            with self.subTest(text=text):
                r = self._parse([text])
                self.assertEqual([e["page"] for e in r], [None],
                                 "不是「斜杠+数字/罗马数字」结尾的行不许当页码")

    def test_roman_to_int(self):
        self.assertEqual(layout._roman_to_int("III"), 3)
        self.assertEqual(layout._roman_to_int("XI"), 11)
        self.assertEqual(layout._roman_to_int("XIII"), 13)
        self.assertEqual(layout._roman_to_int("XIV"), 14)
        self.assertIsNone(layout._roman_to_int("XYZ"))


class TestMarkValueNeverRaises(unittest.TestCase):
    """缺陷 4：plan_notes → mark_value 遇到数学下标直接抛 ValueError。

    这本书正文里有 S₁ S₂、∅、∀、∃（拉康的能指记号与逻辑量词）。NOTE_HEAD
    的正则范围 [\u2070-\u209c] 远宽于"上标数字 0-9"：U+2081-2089 是**下标**、
    U+2071 是上标 i、U+207A-207E 是上标运算符，全落在范围内却不是数字。
    旧实现用 "表.index(s)" 取下标，s 不在表里就抛，整条 make_md 阶段失败。
    """

    def test_range_never_raises_and_maps_digits(self):
        """整段 [\u2070-\u209c] 逐个喂进去都不许抛——这是整条 make_md 的命门。

        其中只有真上标数字应该映射成数字；下标 ₁（S₁ 的注号记号）、上标 i、
        上标运算符都在范围内却不是数字，必须返回 None 而不是抛。
        """
        for o in range(0x2070, 0x209D):
            with self.subTest(codepoint=hex(o)):
                layout.mark_value(chr(o))      # 不该抛

        digits = "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079"
        for i, ch in enumerate(digits):
            with self.subTest(digit=i):
                self.assertEqual(layout.mark_value(ch), i)

        # S₁ 的 ₁ 不是注号，认不出返回 None，不许抛
        self.assertIsNone(layout.mark_value("\u2081"))
        self.assertIsNone(layout.mark_value("\u2082"))

    def test_existing_forms_still_work(self):
        """原有判据不能因为改了实现而失效：圈号、普通数字、括号号、罗马数字。"""
        for text, want in (("\u2460", 1), ("\u2461", 2), ("12", 12),
                           ("(3)", 3), ("i", 1)):
            with self.subTest(text=text):
                self.assertEqual(layout.mark_value(text), want)


if __name__ == "__main__":
    unittest.main()