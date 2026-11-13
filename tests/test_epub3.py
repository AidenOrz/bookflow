"""epub3.py 的独立回归测试：属性转义 + validate() 漏检项 + 产物结构约定。

为什么单独一个文件：tests/test_layout.py 里的 TestEpubNotes 只覆盖了
「弹窗标记存在」这一条 happy path，以及一句「validate 返回 0」的宽松断言。
下面每条用例都对应一次实测确认过的缺陷——用 11 组最小 EPUB 手工构造验证过，
旧的 validate() 对其中 4 类（重复引用、引用无定义、定义在别章、图片缺失）
全部返回 0，等于没查。

断言一律按修好后的正确行为写，不迁就现状。

运行：python -m unittest discover -s tests
"""
import os
import re
import sys
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import epub3  # noqa: E402

# epub:type 在 ElementTree 里是「命名空间 + 本地名」形式
OPS = "{http://www.idpf.org/2007/ops}type"


def build_epub(d, md, **kw):
    """在临时目录里构建一本 EPUB，返回 (路径, build 统计)。纯内存构造，不留文件。"""
    out = os.path.join(d, "t.epub")
    st = epub3.build(md, out, **kw)
    return out, st


def aside_text(el):
    """aside 里的注文（跳过回链编号那个 <a role=doc-backlink>）。

    必须跳过：注文为空时，aside 里只剩 <a class="fnback">1</a>，不跳过就会
    把编号当成注文，"注文为空"永远查不出来。
    """
    buf = [el.text or ""]

    def walk(e):
        for c in e:
            if c.get("role") == "doc-backlink":
                if c.tail:
                    buf.append(c.tail)
                continue
            buf.append(c.text or "")
            walk(c)
            if c.tail:
                buf.append(c.tail)

    walk(el)
    return "".join(buf).strip()


def elements(root, pred):
    return [el for el in root.iter() if pred(el)]


class TestMimetype(unittest.TestCase):
    def test_mimetype_first_and_stored(self):
        """mimetype 必须是第 0 条目、STORED 不压缩、内容精确等于 application/epub+zip。

        旧 validate() 只查了「首个条目」和「压缩方式」，没查内容——写错成
        application/epub 或带换行都能过检，而阅读器会直接拒绝打开。
        """
        with tempfile.TemporaryDirectory() as d:
            out, _ = build_epub(d, "# 第一章\n\n正文。\n", title="t")
            with zipfile.ZipFile(out) as z:
                self.assertEqual(z.namelist()[0], "mimetype")
                info = z.getinfo("mimetype")
                self.assertEqual(info.compress_type, zipfile.ZIP_STORED,
                                 "mimetype 必须 STORED（spec 要求不压缩）")
                self.assertEqual(z.read("mimetype"), b"application/epub+zip")


class TestPackageRefs(unittest.TestCase):
    MD = "\n".join([
        "# 第一章 测试",
        "",
        "## 第一节 小节",
        "",
        "正文[^a]有注。",
        "",
        "[^a]: 注文。",
        "",
    ])

    def test_opf_nav_ncx_refs_exist(self):
        """OPF manifest/spine、nav.xhtml、toc.ncx 里的每个目标都必须真实可达。

        带 #anchor 的要连锚点一起查：锚点写的是 h{章号}_{序号}，一旦切章或
        toc_depth 改动就会错位，只查文件会在目录里留下死链。
        """
        with tempfile.TemporaryDirectory() as d:
            out, _ = build_epub(d, self.MD, title="t", toc_depth=3)
            with zipfile.ZipFile(out) as z:
                names = set(z.namelist())
                opf = ET.fromstring(z.read("content.opf"))
                nav = ET.fromstring(z.read("nav.xhtml"))
                ncx = ET.fromstring(z.read("toc.ncx"))
                docs = {n: ET.fromstring(z.read(n))
                        for n in names if n.endswith(".xhtml")}
            items = {}
            for el in opf.iter():
                if el.tag.endswith("item"):
                    items[el.get("id")] = el.get("href")
            self.assertTrue(items, "manifest 不能为空")
            for iid, href in items.items():
                self.assertIn(href.split("#")[0], names, "manifest 指向包外文件 %s" % iid)
            for el in elements(opf, lambda e: e.tag.endswith("itemref")):
                self.assertIn(el.get("idref"), items, "spine 引用了不存在的 item")
            for el in elements(nav, lambda e: e.tag.endswith("a")):
                h = el.get("href")
                if not h:
                    continue
                f, _, frag = h.partition("#")
                self.assertIn(f, docs, "nav 指向包外文件 %s" % h)
                if frag:
                    self.assertIn(frag, {e.get("id") for e in docs[f].iter()},
                                  "nav 锚点不存在 %s" % h)
            for el in elements(ncx, lambda e: e.tag.endswith("content")):
                src = el.get("src")
                f, _, frag = src.partition("#")
                self.assertIn(f, docs, "ncx 指向包外文件 %s" % src)
                if frag:
                    self.assertIn(frag, {e.get("id") for e in docs[f].iter()},
                                  "ncx 锚点不存在 %s" % src)
            self.assertEqual(epub3.validate(out), 0)


class TestNotePairing(unittest.TestCase):
    MD = "\n".join([
        "# 第一章",
        "",
        "正文开头[^fn1]有一个注。",
        "",
        "第二段[^fn2]。",
        "",
        "[^fn1]: 《晋书》卷九五。",
        "",
        "[^fn2]: 第二条注释。",
        "",
    ])

    def test_noteref_aside_one_to_one(self):
        """noteref 与 aside 必须严格一一对应，且 aside 注文非空。

        只查 "refs - asides" 这个单向差集是不够的：同一注号引用两次时
        （见 TestDuplicateRef）两个集合都只有一个元素，差集为空，旧 validate
        就这样放过了产出重复 id 的坏书。
        """
        with tempfile.TemporaryDirectory() as d:
            out, st = build_epub(d, self.MD, title="t")
            self.assertEqual(st["notes"], 2)
            with zipfile.ZipFile(out) as z:
                root = ET.fromstring(z.read("c001.xhtml"))
            refs = elements(root, lambda e: e.get(OPS) == "noteref")
            asides = elements(root, lambda e: e.get(OPS) == "footnote")
            self.assertEqual(len(refs), 2)
            self.assertEqual(len(asides), 2)
            hrefs = {el.get("href") for el in refs}
            targets = {"#" + el.get("id") for el in asides}
            self.assertEqual(hrefs, targets, "引用与注文必须双向对齐")
            for a in asides:
                self.assertTrue(aside_text(a), "每条注文的正文不能为空")
            self.assertEqual(epub3.validate(out), 0)


class TestDuplicateRef(unittest.TestCase):
    def test_duplicate_fnref_id_caught(self):
        """同一注号引用两次 [^x]…[^x]：aside 只有 1 条，id=fnref-x 出两次。

        旧 validate 返回 0（实测）。现在必须报错：重复 id 让阅读器的回跳
        行为未定义，且注文只剩一条，等于吞了另一处引用。
        """
        md = "# 第一章\n\n前文[^x]，后文又见[^x]。\n\n[^x]: 注文。\n"
        with tempfile.TemporaryDirectory() as d:
            out, _ = build_epub(d, md, title="t")
            with zipfile.ZipFile(out) as z:
                x = z.read("c001.xhtml").decode("utf-8")
            self.assertEqual(x.count('id="fnref-x"'), 2, "产物里确实出现了重复 id")
            self.assertEqual(x.count('id="fn-x"'), 1, "注文只渲染了一条")
            self.assertGreater(epub3.validate(out), 0,
                               "重复引用必须被 validate 检出")


class TestUndefinedRef(unittest.TestCase):
    def test_undefined_ref_caught(self):
        """引用无定义 [^nope]：aside 照样生成，但注文为空。

        这是真实书的常见形态——OCR 把注文丢了、注号还在。旧 validate 返回 0，
        读者点开弹窗看到的是空白。现在必须报「注文为空」。
        """
        md = "# 第一章\n\n正文[^nope]。\n"
        with tempfile.TemporaryDirectory() as d:
            out, _ = build_epub(d, md, title="t")
            with zipfile.ZipFile(out) as z:
                root = ET.fromstring(z.read("c001.xhtml"))
            asides = elements(root, lambda e: e.get(OPS) == "footnote")
            self.assertEqual(len(asides), 1)
            self.assertEqual(aside_text(asides[0]), "", "缺定义时注文是空的")
            self.assertGreater(epub3.validate(out), 0,
                               "注文为空必须被 validate 检出")


class TestEndnote(unittest.TestCase):
    MD = "\n".join([
        "# 第一章",
        "",
        "正文一段。",
        "",
        "> 第一条锚不上正文的注。",
        "> 第二条锚不上正文的注。",
        "> 第三条锚不上正文的注。",
        "",
    ])

    def test_endnote_branch(self):
        """以 > 开头的行 → 章末 endnote，条数相等且带 role=doc-endnote。

        扫描件的上标注号常被 OCR 漏掉，注文锚不上正文，只能整条留在章末。
        用 epub:type=endnote 而不是 footnote：footnote 在阅读器里要弹窗，
        但没有对应的正文位置可弹，会被误当作坏脚注。
        """
        n_quotes = sum(1 for line in self.MD.split("\n") if line.startswith(">"))
        self.assertEqual(n_quotes, 3)
        with tempfile.TemporaryDirectory() as d:
            out, _ = build_epub(d, self.MD, title="t")
            with zipfile.ZipFile(out) as z:
                root = ET.fromstring(z.read("c001.xhtml"))
            ends = elements(root, lambda e: e.get(OPS) == "endnote")
            self.assertEqual(len(ends), n_quotes, "每条引用块都要保留成章末注")
            for e in ends:
                self.assertEqual(e.get("role"), "doc-endnote")
                self.assertTrue(aside_text(e))
            self.assertEqual(epub3.validate(out), 0)


class TestCover(unittest.TestCase):
    def test_cover_injection(self):
        """有封面时：cover.jpg / cover.xhtml 进包、OPF 带 cover-image、
        spine 第一位是 coverpage、nav landmarks 里有封面入口。

        这四条是 Apple Books / Kobo 认封面的全部依据，少一条就变成一个
        灰底占位方框。
        """
        with tempfile.TemporaryDirectory() as d:
            jpg = os.path.join(d, "cover.jpg")
            with open(jpg, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
            out, _ = build_epub(d, "# 第一章\n\n正文。\n", title="t",
                                cover_jpg=jpg)
            with zipfile.ZipFile(out) as z:
                names = set(z.namelist())
                opf = z.read("content.opf").decode("utf-8")
                nav = z.read("nav.xhtml").decode("utf-8")
            self.assertIn("cover.jpg", names)
            self.assertIn("cover.xhtml", names)
            self.assertIn('properties="cover-image"', opf)
            self.assertIn('idref="coverpage"', opf)
            self.assertLess(opf.index('idref="coverpage"'), opf.index('idref="c001"'),
                            "封面必须是 spine 第一项")
            landmarks = nav.split('epub:type="landmarks"', 1)[1]
            self.assertIn('epub:type="cover"', landmarks)
            self.assertIn('href="cover.xhtml"', landmarks)
            self.assertEqual(epub3.validate(out), 0)


class TestEmptyChapter(unittest.TestCase):
    def test_empty_chapter_kept(self):
        """只有标题、没有正文的章也必须保留。

        真实场景：章首页单独一页，标题后直接翻页进正文；或者某章正文全在
        图版上。丢掉它会让目录少一项，全书页码跟着错位。
        """
        md = "# 第一章 空章\n\n# 第二章 有正文\n\n这里是正文。\n"
        with tempfile.TemporaryDirectory() as d:
            out, st = build_epub(d, md, title="t")
            self.assertEqual(st["chapters"], 2)
            with zipfile.ZipFile(out) as z:
                names = set(z.namelist())
                x1 = z.read("c001.xhtml").decode("utf-8")
            self.assertIn("c001.xhtml", names, "空章不能从产物里消失")
            self.assertIn('class="chapter-title"', x1)
            self.assertIn("第一章 空章", x1)
            self.assertEqual(epub3.validate(out), 0)


class TestAttributeEscaping(unittest.TestCase):
    def test_escape_in_attributes(self):
        """书名带 " 或注号带 " 时，产出的 XML 仍然必须可解析。

        旧代码用 esc()（quote=False）往 alt= / href= / id= 里塞值，一个 " 就
        提前闭合属性，整本 EPUB 解析失败。这条用例修 esc 之前是红的。
        """
        title = '书名"带引号'
        md = "\n".join([
            "# 第一章",
            "",
            '正文[^a"b]里有注。',
            "",
            '[^a"b]: 注文。',
            "",
        ])
        with tempfile.TemporaryDirectory() as d:
            jpg = os.path.join(d, "cover.jpg")
            with open(jpg, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
            out, _ = build_epub(d, md, title=title, cover_jpg=jpg)
            with zipfile.ZipFile(out) as z:
                for n in z.namelist():
                    if n.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                        ET.fromstring(z.read(n))     # 不抛异常才算过
                cover = z.read("cover.xhtml").decode("utf-8")
                c1 = z.read("c001.xhtml").decode("utf-8")
            # 属性里的双引号必须被转义；正文文本里的不需要（保持 quote=False）
            self.assertIn("&quot;", cover, "alt 里的双引号必须转义")
            self.assertNotIn('alt="%s"' % title, cover)
            self.assertIn("&quot;", c1, "注号出现在 id/href 里时必须转义")
            self.assertEqual(epub3.validate(out), 0)


class TestImgSrc(unittest.TestCase):
    def test_img_src_must_exist(self):
        """xhtml 里 img src 指向的文件必须在包内。

        旧 validate 只查 OPF manifest，不知道 xhtml 里还有 img：md 里写了
        ![](pic.png) 但图片没拷进 out/，成品里就是一个碎图占位符。
        实测旧 validate 返回 0。
        """
        md = "# 第一章\n\n![](pic.png)\n\n正文。\n"
        with tempfile.TemporaryDirectory() as d:
            out, _ = build_epub(d, md, title="t")
            with zipfile.ZipFile(out) as z:
                names = set(z.namelist())
                x = z.read("c001.xhtml").decode("utf-8")
            self.assertIn('<img src="pic.png"', x)
            self.assertNotIn("pic.png", names, "测试前提：图确实不在包内")
            self.assertGreater(epub3.validate(out), 0,
                               "包内缺失的图片必须被 validate 检出")


class TestTocDepth(unittest.TestCase):
    MD = "\n".join([
        "# 第一章",
        "",
        "## 第一节",
        "",
        "正文一。",
        "",
        "### 第一小节",
        "",
        "正文二。",
        "",
    ])

    def _nav_anchors(self, out):
        """只数 toc 那个 nav 里的 <a>，不含 landmarks。"""
        with zipfile.ZipFile(out) as z:
            nav = z.read("nav.xhtml").decode("utf-8")
        toc = nav.split('epub:type="toc"', 1)[1].split('epub:type="landmarks"', 1)[0]
        return re.findall(r'<a href="([^"]+)"', toc)

    def test_toc_depth_filters_nav(self):
        """toc_depth=1/2/3 → 目录条目数 1/2/3。

        层级是印刷目录给的：讲/章 = 1 级、节 = 2 级、一、= 3 级。书里标题
        有多深由书决定，目录显示到多深由 --toc-depth 决定，两者不能混。
        """
        for depth in (1, 2, 3):
            with self.subTest(toc_depth=depth):
                with tempfile.TemporaryDirectory() as d:
                    out, st = build_epub(d, self.MD, title="t", toc_depth=depth)
                    self.assertEqual(len(self._nav_anchors(out)), depth)
                    self.assertEqual(st["nav"], depth, "ncx 的 navPoint 数要同步")
                    self.assertEqual(epub3.validate(out), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
