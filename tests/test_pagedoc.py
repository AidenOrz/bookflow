"""bookflow.pagedoc 的回归测试。

这一层的意义是「版本不认识就明确拒绝，老文件照常读」。
用例全部对应真实场景：

- test_legacy_document_is_accepted：三本验证书工作目录里几千个老 JSON（无 schema 字段）
  必须继续能跑，不能因为加了契约层就报错。
- test_version_mismatch_is_rejected：改了字段语义后，旧 JSON 要显式拒绝而不是静默误读
  （本项目头号风险是静默出错）。
- test_stamp_does_not_clobber_core_fields：写入方传多余字段时不能覆盖 w/h/lines。
- test_producer_is_never_rewritten：producer 表达真实来源，加载/回填都不许改写。

运行：python -m unittest discover -s tests
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
    def test_legacy_document_is_accepted(self):
        """无 schema 字段的老文件：兼容读取，不报问题。"""
        self.assertEqual(pagedoc.check(page()), [])

    def test_stamped_document_is_accepted(self):
        doc = pagedoc.stamp(page(), pagedoc.producer("rapidocr", "3.10.0"))
        self.assertEqual(pagedoc.check(doc), [])
        self.assertEqual(doc["schema"], pagedoc.SCHEMA)
        self.assertEqual(doc["schema_version"], pagedoc.SCHEMA_VERSION)

    def test_version_mismatch_is_rejected(self):
        """未来版本的数据不能被当前代码静默误读。"""
        doc = pagedoc.stamp(page(), None)
        doc["schema_version"] = pagedoc.SCHEMA_VERSION + 1
        probs = pagedoc.check(doc)
        self.assertTrue(probs, "版本不认识必须报错")
        self.assertIn("schema_version", probs[0])

    def test_foreign_schema_is_rejected(self):
        doc = pagedoc.stamp(page(), None)
        doc["schema"] = "docvortex.middle"
        self.assertTrue(pagedoc.check(doc))

    def test_missing_required_field_is_rejected(self):
        for gone in ("w", "h", "lines"):
            d = page()
            del d[gone]
            with self.subTest(gone=gone):
                probs = pagedoc.check(d)
                self.assertTrue(any(gone in p for p in probs))

    def test_non_dict_is_rejected(self):
        self.assertTrue(pagedoc.check([]))
        self.assertTrue(pagedoc.check("x"))

    def test_bad_version_type_is_rejected(self):
        doc = pagedoc.stamp(page(), None)
        doc["schema_version"] = "one"
        self.assertTrue(pagedoc.check(doc))


class TestStamp(unittest.TestCase):
    def test_stamp_does_not_clobber_core_fields(self):
        """写入方塞附加信息时，不允许覆盖核心字段（否则整页数据废掉）。"""
        doc = page()
        out = pagedoc.stamp(doc, None, w=999, h=0, lines=[], name="覆盖",
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

    def test_stamp_report(self):
        rep = pagedoc.stamp_report({"pages": 383}, pagedoc.producer("bookflow", "0.1"))
        self.assertEqual(rep["schema"], pagedoc.REPORT_SCHEMA)
        self.assertEqual(rep["pages"], 383, "既有字段不能被覆盖")

    def test_report_stamp_does_not_overwrite(self):
        rep = pagedoc.stamp_report({"pages": 383}, None, pages=1)
        self.assertEqual(rep["pages"], 383)


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
            self.assertIn(name, pagedoc.KNOWN_PRODUCERS)


class TestIO(unittest.TestCase):
    def test_write_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "p-001.png.json")
            doc = pagedoc.stamp(page(), pagedoc.producer("rapidocr", "3.10.0"))
            pagedoc.write(p, doc)
            back = pagedoc.read(p)
            self.assertEqual(back["schema_version"], pagedoc.SCHEMA_VERSION)
            self.assertEqual(back["lines"][0]["t"], "正文一行。")

    def test_read_rejects_bad_version(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "p-001.png.json")
            doc = pagedoc.stamp(page(), None)
            doc["schema_version"] = 99
            jsonio.write(p, doc)
            with self.assertRaises(ValueError):
                pagedoc.read(p)

    def test_read_accepts_legacy(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "p-001.png.json")
            jsonio.write(p, page())
            self.assertEqual(pagedoc.read(p)["name"], "p-001.png")


if __name__ == "__main__":
    unittest.main()