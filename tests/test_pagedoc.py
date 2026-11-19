"""bookflow.pagedoc 的回归测试。

这一层的意义是「版本不认识就明确拒绝，老文件照常读」。
每个用例都对应一个真实场景或一条真实边界：

- 三本验证书工作目录里几千个老 JSON（无 schema 字段）必须继续能跑；
- 改了字段语义后，旧 JSON 要显式拒绝而不是静默误读（本项目头号风险是静默出错）；
- 写入方传多余字段时不能覆盖 w/h/lines；
- producer 表达真实来源，加载/回填都不许改写。

**用例组织方式**：同一条规则的正例 + 各个反例合并进一个方法（subTest 分跑）。
合并的是方法个数，不是断言——每条断言都在，失败时 subTest 会报出是哪一组输入。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import jsonio       # noqa: E402
import pagedoc      # noqa: E402


def page(lines=None):
    return {"name": "p-001.png", "w": 2480, "h": 3509,
            "lines": lines if lines is not None else [
                {"x0": 10.0, "y0": 20.0, "x1": 200.0, "y1": 60.0, "t": "正文一行。", "s": 0.99}]}


class TestCheck(unittest.TestCase):
    def test_legacy_and_stamped_documents_are_accepted(self):
        # 无 schema 字段的老文件：兼容读取，不报问题
        self.assertEqual(pagedoc.check(page()), [])
        # 盖过章的同样通过，且章盖对了
        doc = pagedoc.stamp(page(), pagedoc.producer("rapidocr", "3.10.0"))
        self.assertEqual(pagedoc.check(doc), [])
        self.assertEqual(doc["schema"], pagedoc.SCHEMA)
        self.assertEqual(doc["schema_version"], pagedoc.SCHEMA_VERSION)

    def test_version_and_schema_mismatch_are_rejected(self):
        # 未来版本不能被静默误读
        doc = pagedoc.stamp(page(), None)
        doc["schema_version"] = pagedoc.SCHEMA_VERSION + 1
        probs = pagedoc.check(doc)
        self.assertTrue(probs, "版本不认识必须报错")
        self.assertIn("schema_version", probs[0])
        # 别的工具的 schema 也不是我们的
        doc = pagedoc.stamp(page(), None)
        doc["schema"] = "docvortex.middle"
        self.assertTrue(pagedoc.check(doc))
        # 版本号类型错了同样拒绝（"one" 不是数字）
        doc = pagedoc.stamp(page(), None)
        doc["schema_version"] = "one"
        self.assertTrue(pagedoc.check(doc))

    def test_malformed_documents_are_rejected(self):
        for gone in ("w", "h", "lines"):
            d = page()
            del d[gone]
            with self.subTest(missing=gone):
                self.assertTrue(any(gone in p for p in pagedoc.check(d)))
        for junk in ([], "x", None, 42):
            with self.subTest(junk=repr(junk)):
                self.assertTrue(pagedoc.check(junk))


class TestStamp(unittest.TestCase):
    def test_stamp_does_not_clobber_core_fields(self):
        """写入方塞附加信息时，不允许覆盖核心字段（否则整页数据废掉）。"""
        out = pagedoc.stamp(page(), None, w=999, h=0, lines=[], name="覆盖",
                            source_pdf="/tmp/a.pdf")
        self.assertEqual(out["w"], 2480)
        self.assertEqual(out["h"], 3509)
        self.assertEqual(out["name"], "p-001.png")
        self.assertTrue(out["lines"])
        self.assertEqual(out["source_pdf"], "/tmp/a.pdf", "非核心字段应当写入")

    def test_stamp_is_idempotent(self):
        d1 = pagedoc.stamp(page(), pagedoc.producer("rapidocr"))
        d2 = pagedoc.stamp(d1, pagedoc.producer("rapidocr"))
        self.assertEqual(d1, d2)

    def test_stamp_report_keeps_existing_fields(self):
        rep = pagedoc.stamp_report({"pages": 383}, pagedoc.producer("bookflow", "0.1"))
        self.assertEqual(rep["schema"], pagedoc.REPORT_SCHEMA)
        self.assertEqual(rep["pages"], 383, "既有字段不能被覆盖")
        # 显式传 pages= 也不能覆盖已存在的值
        self.assertEqual(pagedoc.stamp_report({"pages": 383}, None, pages=1)["pages"], 383)


class TestProducer(unittest.TestCase):
    def test_producer_is_never_rewritten(self):
        """producer 表达真实生成来源，任何加工步骤都不许改写它。"""
        doc = pagedoc.stamp(page(), pagedoc.producer("pdftotext", "26.09.0"))
        before = dict(doc["producer"])
        pagedoc.stamp(doc, None)          # 模拟下游再盖一次章（不传 producer）
        self.assertEqual(doc["producer"], before)

    def test_describe(self):
        doc = pagedoc.stamp(page(), pagedoc.producer("rapidocr", "3.10.0"))
        s = pagedoc.describe(doc)
        self.assertIn("rapidocr", s)
        self.assertIn("3.10.0", s)
        self.assertIn("无版本标识", pagedoc.describe(page()))

    def test_known_producers_cover_all_writers(self):
        """四个写入方都必须在这里登记，否则日志里会显示「未知来源」。"""
        for name in ("rapidocr", "rapidocr-repair", "pp_doc_layoutv3", "pdftotext"):
            with self.subTest(producer=name):
                self.assertIn(name, pagedoc.KNOWN_PRODUCERS)


class TestIO(unittest.TestCase):
    def test_write_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "p-001.png.json")
            pagedoc.write(p, pagedoc.stamp(page(), pagedoc.producer("rapidocr", "3.10.0")))
            back = pagedoc.read(p)
            self.assertEqual(back["schema_version"], pagedoc.SCHEMA_VERSION)
            self.assertEqual(back["lines"][0]["t"], "正文一行。")

    def test_read_rejects_bad_version_and_accepts_legacy(self):
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "bad.json")
            doc = pagedoc.stamp(page(), None)
            doc["schema_version"] = 99
            jsonio.write(bad, doc)
            with self.assertRaises(ValueError):
                pagedoc.read(bad)
            # 老文件（无三键）照常读——这是向后兼容的硬要求
            old = os.path.join(d, "old.json")
            jsonio.write(old, page())
            self.assertEqual(pagedoc.read(old)["name"], "p-001.png")


if __name__ == "__main__":
    unittest.main()
