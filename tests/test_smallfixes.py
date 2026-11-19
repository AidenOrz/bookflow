"""四个"不会崩但会咬人"的小缺陷的回归测试。

每条用例对应一次静态审阅指出的真实缺陷，且都**在修复前的代码上验证过会红**：

- cover.inject 把书名直接塞进 alt="" 属性，书名含 & < > " 时 cover.xhtml 是坏 XML；
- repair.median_char_width 对空样本调 statistics.median([]) → StatisticsError；
- disagree.py 用 json.dump(obj, open(...)) 写产物，句柄不收、无原子写；
- apply_decisions.py 同一常量赋值写了两遍（改一处忘另一处的经典来源）。

运行：python -m unittest discover -s tests
"""
import contextlib
import io
import os
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import cover                        # noqa: E402
import jsonio                       # noqa: E402


def src_of(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# cover.inject：书名进 XML 属性必须转义
# ---------------------------------------------------------------------------

MIN_OPF = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
    'unique-identifier="uid">\n'
    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
    '<dc:title>T</dc:title></metadata>\n'
    '<manifest><item id="c1" href="c1.xhtml" '
    'media-type="application/xhtml+xml"/></manifest>\n'
    '<spine toc="ncx"><itemref idref="c1"/></spine>\n'
    '</package>\n')


def write_min_epub(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("content.opf", MIN_OPF.encode("utf-8"))
        z.writestr("c1.xhtml",
                   b'<?xml version="1.0" encoding="utf-8"?>'
                   b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>x</p>'
                   b'</body></html>')


class TestCoverTitleEscaping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix=".tmp-bfcover-")
        self.epub = os.path.join(self.tmp, "t.epub")
        self.jpg = os.path.join(self.tmp, "cover.jpg")
        write_min_epub(self.epub)
        with open(self.jpg, "wb") as f:
            f.write(b"\xff\xd8fakejpeg\xff\xd9")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cover_xhtml(self):
        with zipfile.ZipFile(self.epub) as z:
            return z.read("cover.xhtml").decode("utf-8")

    def img_alt(self, title):
        # 每次注入前重建一份**干净**的 epub：cover.inject 对已存在 cover.xhtml
        # 的包是跳过而不是覆盖，重复注入会安静地什么都不做（实测得到的是上一次的书名）。
        write_min_epub(self.epub)
        with contextlib.redirect_stdout(io.StringIO()):
            cover.inject(self.epub, title, self.jpg)
        xml = self.cover_xhtml()
        root = ET.fromstring(xml)                 # 坏 XML 会在这里抛
        img = root.iter("{http://www.w3.org/1999/xhtml}img")
        el = next(img)
        return el.get("alt")

    def test_hostile_titles_keep_xml_valid(self):
        """书名进 alt="" 属性必须转义，否则整本 EPUB 是坏 XML。

        旧版直接 COVER_XHTML.format(title=title)：一个 & 或 " 就把属性截断，
        cover.xhtml 解析失败。ET.fromstring 会在这里直接抛，不用额外断言。
        """
        for title in (
            "中国僧官制度史",            # 普通书名：不得被改写
            "制度 & 信仰",               # & -> &amp;
            '《读书"札记"》',            # " -> &quot;
            "a<b>c",                      # < > 转义后应原样还原
        ):
            with self.subTest(title=title):
                self.assertEqual(self.img_alt(title), title)

        # 四种敌意字符同时出现（含单引号）：整份 cover.xhtml 仍必须是良构 XML
        write_min_epub(self.epub)
        with contextlib.redirect_stdout(io.StringIO()):
            cover.inject(self.epub, '<x> & 引"号\'', self.jpg)
        ET.fromstring(self.cover_xhtml())


# ---------------------------------------------------------------------------
# repair.median_char_width：空样本要返回 None，不许抛
# ---------------------------------------------------------------------------

class TestMedianCharWidthEmpty(unittest.TestCase):
    def setUp(self):
        try:
            import repair
        except Exception as e:                    # 没装 rapidocr 的环境跳过
            self.skipTest("repair 不可 import：%s" % e)
        self.repair = repair
        self.tmp = tempfile.mkdtemp(prefix=".tmp-bfrep-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def page(self, name, lines, w=2480, h=3508):
        p = os.path.join(self.tmp, name + ".json")
        jsonio.write(p, {"name": name, "w": w, "h": h, "lines": lines})
        return p

    def test_median_char_width_returns_none_without_samples(self):
        """空样本不是不可能事件：旧版 statistics.median([]) 抛 StatisticsError，
        把 main() 里那句友好提示截胡了。目录页、整页插图、纯扫描件都会走到这里。"""
        # 没有任何文件
        self.assertIsNone(self.repair.median_char_width([]))
        # 只有短行（目录页）同样没有样本
        p = self.page("p-001", [{"x0": 100.0, "y0": 100.0, "x1": 400.0,
                                 "y1": 160.0, "t": "目　录", "s": 1.0}])
        self.assertIsNone(self.repair.median_char_width([p]))

    def test_long_lines_still_give_a_number(self):
        """正常页面不能被这次改动带偏：长行仍要算出字宽。"""
        p = self.page("p-001", [{"x0": 300.0, "y0": 400.0, "x1": 2100.0,
                                 "y1": 470.0, "t": "这是一行横跨版心的正文。" * 3,
                                 "s": 1.0}])
        cw = self.repair.median_char_width([p])
        self.assertIsInstance(cw, float)
        self.assertGreater(cw, 0)


# ---------------------------------------------------------------------------
# 写文件：产物必须走 jsonio（原子写 + 句柄即收）
# ---------------------------------------------------------------------------

class TestWritesGoThroughJsonio(unittest.TestCase):
    def test_no_writer_uses_bare_open_or_double_constant(self):
        """产物写入一律走 jsonio（原子写 + 句柄即收），常量只定义一次。

        disagree.py 旧版三处 json.dump(obj, open(...))：句柄不收（Windows 上紧跟的
        改名/删除会 PermissionError），且没有原子写（中途崩就是半个 JSON）。
        audit / cmp_ocr 是同一个病根，一起钉住。
        """
        src = src_of("disagree.py")
        self.assertNotIn("json.dump(", src)
        self.assertNotIn('open(a.out, "w"', src)
        self.assertNotIn('open(a.txt, "w"', src)
        self.assertIn("jsonio.write(", src)
        self.assertIn("jsonio.write_text(", src)

        for name in ("audit.py", "cmp_ocr.py"):
            with self.subTest(module=name):
                s = src_of(name)
                self.assertNotIn("json.dump(", s)
                self.assertNotRegex(s, r'=\s*open\([^)]*"w"')
                self.assertNotRegex(s, r'^\s*open\([^)]*"w"[^)]*\)\.write\(')
                self.assertIn("jsonio.write(", s)

        # 同一个常量赋值两遍，早晚会有一次只改了其中一处
        hits = re.findall(r"^LEGACY_TABLE\s*=", src_of("apply_decisions.py"), re.M)
        self.assertEqual(len(hits), 1, "LEGACY_TABLE 重复赋值 %d 次" % len(hits))


if __name__ == "__main__":
    unittest.main()
