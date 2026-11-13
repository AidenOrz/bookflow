"""decisions.py 适配层与 apply_decisions.py 配置化回填的回归测试。

用例全部对应"实测踩过的坑"，不是构造出来的假数据：

- test_from_disagree_real_repo：仓库里 out/disagree.json 196 条（A=37/B=27/?=132）
  转换后 verdict 分布必须不变——这份文件是 review_queue.json 的唯一上游。
- test_dry_run_does_not_touch_disk / test_dry_run_is_idempotent：旧版默认原地覆写、
  且对已回填目录再跑会刷出 6 条"未定位（原文已变？）"的假告警。
- test_apply_is_idempotent_via_status：旧版不幂等，跑第二遍分不清"没改"和"改错了"。
- test_multi_segment_is_applied：旧版把多差异段的裁决静默跳过（硬编码 #3/#18 从未生效）。
- test_require_single_segment_falls_back：显式打开时退回旧行为，但要报"跳过"而不是悄悄丢。
- test_backup_dir_only_touched_pages：旧版 .bak 只在首次创建、第二次运行不刷新。
- test_legacy_export：硬编码裁决一次性迁到 decisions.json，之后代码里不再留裁决。

运行：python -m unittest discover -s tests
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import apply_decisions as ap      # noqa: E402
import decisions as dec           # noqa: E402
import jsonio                     # noqa: E402


# ---------------------------------------------------------------------------
# 造数据的小工具
# ---------------------------------------------------------------------------

def page_json(name, lines):
    """一页 OCR 行级 JSON：lines 为 (y0, t) 列表。"""
    return {"name": name, "w": 2518, "h": 3535,
            "lines": [{"x0": 668.8, "y0": float(y0), "x1": 2108.5,
                       "y1": float(y0) + 60, "t": t, "s": 0.99}
                      for (y0, t) in lines]}


def read_bytes(p):
    """整份读成 bytes，用哈希以外最直接的方式判断"文件到底有没有动过"。"""
    with open(p, "rb") as f:
        return f.read()


def write_pages(d, pages):
    """把 {页名: page_json} 落盘到目录 d（页名带 .png，文件名再加 .json）。"""
    os.makedirs(d, exist_ok=True)
    for name, p in pages.items():
        jsonio.write(os.path.join(d, name + ".json"), p)


def run_ap(*args):
    """跑 apply_decisions.main()，返回 (退出码, 标准输出)。"""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ap.main(list(args))
    return rc, buf.getvalue()


def summary(out):
    """从输出里取机器可读的 SUMMARY 行，转成 dict。"""
    m = re.search(r"^SUMMARY (\{.*\})$", out, re.M)
    assert m, "输出里没有 SUMMARY 行：\n" + out
    return json.loads(m.group(1))


# ---------------------------------------------------------------------------
# decisions.py：三路来源 -> decisions
# ---------------------------------------------------------------------------

class TestFromDisagree(unittest.TestCase):
    def test_verdicts_carried_over(self):
        """verdict A/B 直接可用，? 保留成待裁决。"""
        src = [
            {"page": "p-001.png", "y0": 100.0, "A": "甲甲", "B": "甲乙",
             "verdict": "B", "why": "B 强"},
            {"page": "p-001.png", "y0": 200.0, "A": "丙丙", "B": "丙丙",
             "verdict": "?", "why": "判不出"},
        ]
        d = dec.from_disagree(src)
        self.assertEqual([e["verdict"] for e in d["decisions"]], ["B", "?"])
        self.assertEqual(d["decisions"][0]["status"], "pending")
        self.assertEqual(d["decisions"][0]["replacement"], None)
        self.assertEqual(dec.validate(d), [])

    def test_real_repo_disagree(self):
        """仓库现有 out/disagree.json：196 条，A=37 / B=27 / ?=132。

        out/ 被 .gitignore 排掉，换了机器就没有——缺失时跳过，不算失败。
        """
        p = os.path.join(ROOT, "out", "disagree.json")
        if not os.path.exists(p):
            self.skipTest("out/disagree.json 不存在（运行产物，未入库）")
        raw = jsonio.read(p)
        self.assertEqual(len(raw), 196)
        d = dec.from_disagree(raw)
        self.assertEqual(len(d["decisions"]), 196)
        self.assertEqual(dec.counts(d), {"A": 37, "B": 27, "?": 132})
        self.assertEqual(dec.validate(d), [])
        # key 必须在表内唯一，否则 merge/回写会互相覆盖
        keys = [e["key"] for e in d["decisions"]]
        self.assertEqual(len(keys), len(set(keys)))


class TestFromQueue(unittest.TestCase):
    def test_queue_is_same_shape_as_disagree(self):
        """review_queue.json 就是 disagree 的人工筛选子集，字段完全一致。

        它没有任何生成脚本，所以只能靠 page@y0 主键、不能靠下标。
        """
        q = [{"page": "p-048.png", "y0": 2573.1, "A": "abc", "B": "abd",
              "verdict": "?"}]
        d = dec.from_queue(q)
        self.assertEqual(d["source"]["queue_from"], "queue")
        self.assertEqual(d["decisions"][0]["key"], "p-048.png@2573.1")


class TestFromAudit(unittest.TestCase):
    def _audit_dir(self, d):
        """造一份最小的 ocr 目录 + report.json（page_map 把 md 偏移映射到 PDF 页）。"""
        ocr = os.path.join(d, "ocr")
        write_pages(ocr, {
            "p-003.png": page_json("p-003.png", [
                (600.0, "上一行的正文内容。"),
                (659.5, "的结合，在刑罚的实施过程中，既使证据得到确认。"),
            ]),
        })
        report = os.path.join(d, "report.json")
        jsonio.write(report, {"layout": {"page_map": [[0, 3]]}})
        return ocr, report

    def test_hit_resolves_to_line(self):
        """反查链：pos -> page_map -> p-003.png.json -> 含 wrong 的行 -> y0 + 整行。"""
        with tempfile.TemporaryDirectory() as d:
            ocr, report = self._audit_dir(d)
            hits = [{"pos": 5, "len": 2, "wrong": "既使", "right": "即使",
                     "freq": 6462, "ctx": "…"}]
            doc = dec.from_audit(hits, report, ocr)
            e = doc["decisions"][0]
            self.assertEqual(e["verdict"], "B")
            self.assertEqual(e["page"], "p-003.png")
            self.assertEqual(e["y0"], 659.5)
            self.assertEqual(e["A"], "的结合，在刑罚的实施过程中，既使证据得到确认。")
            self.assertEqual(e["B"], "的结合，在刑罚的实施过程中，即使证据得到确认。")
            self.assertIn("既使", e["note"])
            self.assertEqual(dec.validate(doc), [])

    def test_hit_with_page_field_skips_page_map(self):
        """hit 自己带 page 时不该再走 pos 换算（老书 report.json 没有 page_map 也能用）。"""
        with tempfile.TemporaryDirectory() as d:
            ocr, _ = self._audit_dir(d)
            hits = [{"pos": 5, "len": 2, "wrong": "既使", "right": "即使",
                     "freq": 1, "page": 3, "ctx": "…"}]
            doc = dec.from_audit(hits, None, ocr)
            self.assertEqual(doc["decisions"][0]["page"], "p-003.png")
            self.assertEqual(doc["decisions"][0]["verdict"], "B")

    def test_unlocatable_hit_is_kept(self):
        """定位不到的 hit 不能丢：保留成 verdict=? + L3，note 写明未定位行。"""
        with tempfile.TemporaryDirectory() as d:
            ocr, report = self._audit_dir(d)
            hits = [
                # 页里没有这个词
                {"pos": 5, "len": 2, "wrong": "自白", "right": "白白",
                 "freq": 9, "ctx": "…"},
                # 完全没有页码信息（无 page、无 page_map）
                {"pos": 999, "len": 2, "wrong": "介人", "right": "介入",
                 "freq": 9, "ctx": "…"},
            ]
            doc = dec.from_audit(hits, report, ocr)
            self.assertEqual(len(doc["decisions"]), 2, "定位不到也必须留下证据")
            for e in doc["decisions"]:
                self.assertEqual(e["verdict"], "?")
                self.assertEqual(e["level"], "L3")
                self.assertIn("未定位行", e["note"])
            # 两条都在 p-003.png 上、y0 为 None，key 必须仍然互不相同
            self.assertNotEqual(doc["decisions"][0]["key"],
                                doc["decisions"][1]["key"])


class TestMergeAndIO(unittest.TestCase):
    def test_merge_later_wins(self):
        """按 key 合并，后者覆盖前者（重新裁决时以新为准），且保序。"""
        a = dec.from_disagree([
            {"page": "p-001.png", "y0": 100.0, "A": "甲", "B": "乙", "verdict": "?"},
            {"page": "p-002.png", "y0": 200.0, "A": "丙", "B": "丁", "verdict": "?"},
        ])
        b = dec.from_disagree([
            {"page": "p-001.png", "y0": 100.0, "A": "甲", "B": "乙", "verdict": "B"},
        ])
        m = dec.merge(a, b)
        self.assertEqual(len(m["decisions"]), 2)
        self.assertEqual([e["key"] for e in m["decisions"]],
                         ["p-001.png@100", "p-002.png@200"])
        self.assertEqual(m["decisions"][0]["verdict"], "B")
        self.assertEqual(m["decisions"][1]["verdict"], "?")

    def test_merge_accepts_bare_lists(self):
        """bookflow 的 decisions 阶段是 dec.merge(dec.read(dst), picked) —— 两者都是整份文档。"""
        m = dec.merge([dec.make_decision("p-001.png", 1.0, B="乙", verdict="B")],
                      {"version": 1, "source": {}, "match": {},
                       "decisions": [dec.make_decision("p-001.png", 1.0, B="丙",
                                                       verdict="B")]})
        self.assertEqual(len(m["decisions"]), 1)
        self.assertEqual(m["decisions"][0]["B"], "丙")

    def test_write_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "out", "decisions.json")
            src = dec.from_disagree([{"page": "p-001.png", "y0": 1.0,
                                      "A": "甲", "B": "乙", "verdict": "B"}])
            dec.write(p, src)
            self.assertTrue(os.path.exists(p))
            back = dec.read(p)
            self.assertEqual(back["decisions"][0]["key"], "p-001.png@1")
            self.assertEqual(back["decisions"][0]["B"], "乙")

    def test_load_any_dispatches_by_content(self):
        """同一入口按内容分派：audit 的 hit 有 wrong/right，disagree 有 A/B+why。"""
        with tempfile.TemporaryDirectory() as d:
            pa = os.path.join(d, "audit.json")
            jsonio.write(pa, [{"pos": 1, "len": 2, "wrong": "甲", "right": "乙"}])
            doc = dec.load_any(pa, ocr=os.path.join(d, "nope"))
            self.assertEqual(doc["source"]["queue_from"], "audit")
            self.assertEqual(doc["decisions"][0]["verdict"], "?")   # 定位不到 -> 保留

            pd = os.path.join(d, "disagree.json")
            jsonio.write(pd, [{"page": "p-001.png", "y0": 1.0, "A": "甲", "B": "乙",
                               "verdict": "B", "why": "w"}])
            doc = dec.load_any(pd)
            self.assertEqual(doc["source"]["queue_from"], "disagree")

    def test_key_normalizes_float_noise(self):
        """y0 的浮点尾差不能造出两个 key（否则 status 回写与 merge 去重全失效）。"""
        self.assertEqual(dec.key_of("p-001.png", 2109.6), "p-001.png@2109.6")
        self.assertEqual(dec.key_of("p-001.png", 2109.6000000000004),
                         "p-001.png@2109.6")
        self.assertEqual(dec.key_of("p-001.png", 2109.0), "p-001.png@2109")

    def test_page_number_normalized_to_page_name(self):
        """audit 给的是数字页码、disagree 给的是页名，key 必须落在同一个空间。"""
        self.assertEqual(dec.page_name(53), "p-053.png")
        self.assertEqual(dec.page_name("53"), "p-053.png")
        self.assertEqual(dec.page_name("p-053.png"), "p-053.png")

    def test_validate_rejects_bad_entries(self):
        """裁决表写错宁可报错停下，也不能像旧版那样静默跳过某条裁决。"""
        bad = {"version": 1, "source": {}, "match": {},
               "decisions": [
                   {"key": "a", "page": "p-001.png", "y0": 1.0, "A": "", "B": "",
                    "verdict": "X", "replacement": None, "level": "L1",
                    "note": "", "status": "pending"},
                   {"key": "b", "page": "p-001.png", "y0": 2.0, "A": "", "B": "",
                    "verdict": "C", "replacement": None, "level": "L1",
                    "note": "", "status": "pending"},
                   {"key": "a", "page": "p-001.png", "y0": 3.0, "A": "", "B": "",
                    "verdict": "A", "replacement": None, "level": "L1",
                    "note": "", "status": "pending"},
               ]}
        probs = dec.validate(bad)
        self.assertTrue(any("verdict 非法" in p for p in probs))
        self.assertTrue(any("缺 replacement" in p for p in probs))
        self.assertTrue(any("key 重复" in p for p in probs))


# ---------------------------------------------------------------------------
# apply_decisions.py：配置驱动的回填
# ---------------------------------------------------------------------------

def _fixture(d, extra_lines=None):
    """造一份"两个多差异段 + 一个单差异段"的 OCR 目录，返回 (ocr 目录, decisions 路径)。"""
    ocr = os.path.join(d, "ocr_merged")
    write_pages(ocr, {
        "p-048.png": page_json("p-048.png", extra_lines or [
            (100.0, "无关的上一行。"),
            (2573.1, "fang istTat,就是这个意思。"),
            (2600.0, "无关的下一行。"),
        ]),
        "p-303.png": page_json("p-303.png", [
            (1889.4, "mie Verlag，Berlin1964.(哥特弗利德"),
        ]),
    })
    decisions_path = os.path.join(d, "out", "decisions.json")
    dec.write(decisions_path, dec.from_disagree([
        {"page": "p-048.png", "y0": 2573.1,
         "A": "fang istTat,就是这个意思。", "B": "fang ist Tat，就是这个意思。",
         "verdict": "B", "why": "两处差异"},
        {"page": "p-303.png", "y0": 1889.4,
         "A": "mie Verlag，Berlin1964.(哥特弗利德",
         "B": "mie Verlag，Berlin 1964.（哥特弗利德",
         "verdict": "B", "why": "三处差异"},
    ]))
    return ocr, decisions_path


class TestDryRun(unittest.TestCase):
    def test_dry_run_does_not_touch_disk(self):
        """默认必须是 dry-run：旧版默认原地覆写，跑错一次就污染 OCR 结果。"""
        with tempfile.TemporaryDirectory() as d:
            ocr, dp = _fixture(d)
            before = {n: read_bytes(os.path.join(ocr, n))
                      for n in os.listdir(ocr)}
            rc, out = run_ap("--work", d, "--decisions", dp)
            self.assertEqual(rc, 0)
            after = {n: read_bytes(os.path.join(ocr, n))
                     for n in os.listdir(ocr)}
            self.assertEqual(before, after, "dry-run 绝不能改文件")
            self.assertFalse(os.path.exists(os.path.join(ocr, ".bak")))
            s = summary(out)
            self.assertTrue(s["dry_run"])
            self.assertEqual(s["will_apply"], 2)
            self.assertEqual(s["applied"], 0)

    def test_dry_run_is_idempotent(self):
        """同一目录连跑两次 dry-run，四个数字必须完全一致（旧版会刷假告警）。"""
        with tempfile.TemporaryDirectory() as d:
            ocr, dp = _fixture(d)
            _, out1 = run_ap("--work", d, "--decisions", dp)
            _, out2 = run_ap("--work", d, "--decisions", dp)
            self.assertEqual(summary(out1), summary(out2))


class TestApply(unittest.TestCase):
    def test_apply_writes_backup_and_status(self):
        """--apply：备份只含本次改的页、status 回写 applied、再跑一次将应用 0 处。"""
        with tempfile.TemporaryDirectory() as d:
            ocr, dp = _fixture(d)
            _, out = run_ap("--work", d, "--decisions", dp, "--apply")
            s = summary(out)
            self.assertEqual(s["applied"], 2)
            self.assertEqual(s["missing"], 0)

            # 文本真的改了（整行覆盖，两处/三处差异段都生效）
            got = jsonio.read(os.path.join(ocr, "p-048.png.json"))
            texts = [L["t"] for L in got["lines"]]
            self.assertIn("fang ist Tat，就是这个意思。", texts)
            got = jsonio.read(os.path.join(ocr, "p-303.png.json"))
            self.assertIn("mie Verlag，Berlin 1964.（哥特弗利德",
                          [L["t"] for L in got["lines"]])

            # 备份：一次运行一个时间戳目录，且只放本次真要改的页
            bakroot = os.path.join(ocr, ".bak")
            subs = os.listdir(bakroot)
            self.assertEqual(len(subs), 1)
            files = sorted(os.listdir(os.path.join(bakroot, subs[0])))
            self.assertEqual(files, ["p-048.png.json", "p-303.png.json"])
            # 备份内容是改动前的原文
            old = jsonio.read(os.path.join(bakroot, subs[0], "p-048.png.json"))
            self.assertIn("fang istTat,就是这个意思。", [L["t"] for L in old["lines"]])

            # status 回写，让"落没落地"可观测
            doc = dec.read(dp)
            self.assertEqual({e["status"] for e in doc["decisions"]}, {"applied"})

            # 幂等：第二遍 将应用 0 处、此前已应用 2 处
            _, out2 = run_ap("--work", d, "--decisions", dp)
            s2 = summary(out2)
            self.assertEqual(s2["will_apply"], 0)
            self.assertEqual(s2["already_applied"], 2)
            self.assertEqual(s2["applied"], 0)

    def test_no_backup(self):
        with tempfile.TemporaryDirectory() as d:
            ocr, dp = _fixture(d)
            run_ap("--work", d, "--decisions", dp, "--apply", "--no-backup")
            self.assertFalse(os.path.exists(os.path.join(ocr, ".bak")))

    def test_backup_dir_override(self):
        with tempfile.TemporaryDirectory() as d:
            ocr, dp = _fixture(d)
            bak = os.path.join(d, "baks")
            run_ap("--work", d, "--decisions", dp, "--apply", "--backup-dir", bak)
            self.assertEqual(len(os.listdir(bak)), 1)

    def test_verdict_c_overrides_whole_line(self):
        """verdict=C 用 replacement 整行覆盖（两者都不对时人工给的写法）。"""
        with tempfile.TemporaryDirectory() as d:
            ocr = os.path.join(d, "ocr_merged")
            write_pages(ocr, {"p-001.png": page_json("p-001.png",
                                                     [(10.0, "原行文本甲。")])})
            dp = os.path.join(d, "out", "decisions.json")
            dec.write(dp, {"version": 1, "source": {}, "match": {},
                           "decisions": [dec.make_decision(
                               "p-001.png", 10.0, A="原行文本甲。", B="原行文本乙。",
                               verdict="C", replacement="人工定的写法丙。")]})
            run_ap("--work", d, "--decisions", dp, "--apply")
            got = jsonio.read(os.path.join(ocr, "p-001.png.json"))
            self.assertEqual(got["lines"][0]["t"], "人工定的写法丙。")

    def test_require_single_segment_falls_back(self):
        """打开 require_single_segment 时退回旧行为：多差异段跳过，但要报出来而非静默。"""
        with tempfile.TemporaryDirectory() as d:
            ocr, dp = _fixture(d)
            doc = dec.read(dp)
            doc["match"]["require_single_segment"] = True
            dec.write(dp, doc)
            _, out = run_ap("--work", d, "--decisions", dp)
            s = summary(out)
            self.assertEqual(s["will_apply"], 0)
            self.assertEqual(s["skipped"], 2)
            self.assertIn("差异段", out)

    def test_missing_and_mismatch_are_counted(self):
        """定位不到 / 原文与 A 不符，必须分开计数，不能混成一句"未定位（原文已变？）"。"""
        with tempfile.TemporaryDirectory() as d:
            ocr = os.path.join(d, "ocr_merged")
            write_pages(ocr, {"p-001.png": page_json("p-001.png", [
                (10.0, "被告文本已被别的规则改过。"),
                (500.0, "别的一行。"),
            ])})
            dp = os.path.join(d, "out", "decisions.json")
            dec.write(dp, {"version": 1, "source": {}, "match": {}, "decisions": [
                dec.make_decision("p-001.png", 10.0, A="原行文本。", B="新行文本。",
                                  verdict="B"),
                dec.make_decision("p-001.png", 900.0, A="很远的一行。", B="新的。",
                                  verdict="B"),
                dec.make_decision("p-404.png", 10.0, A="缺页里的行。", B="新的。",
                                  verdict="B"),
            ]})
            _, out = run_ap("--work", d, "--decisions", dp)
            s = summary(out)
            self.assertEqual(s["missing"], 3)
            self.assertEqual(s["will_apply"], 0)

    def test_verdict_a_and_question_never_touch_page_files(self):
        """A（保留原样）/ ?（未裁决）只是登记：页文件不存在也不该报"未定位"。"""
        with tempfile.TemporaryDirectory() as d:
            ocr = os.path.join(d, "ocr_merged")
            write_pages(ocr, {"p-001.png": page_json("p-001.png", [(10.0, "甲。")])})
            dp = os.path.join(d, "out", "decisions.json")
            dec.write(dp, {"version": 1, "source": {}, "match": {}, "decisions": [
                dec.make_decision("p-999.png", 10.0, A="甲。", B="乙。", verdict="A"),
                dec.make_decision("p-998.png", 10.0, A="甲。", B="乙。", verdict="?"),
            ]})
            _, out = run_ap("--work", d, "--decisions", dp)
            s = summary(out)
            self.assertEqual(s["skipped"], 2)
            self.assertEqual(s["missing"], 0)

    def test_missing_decisions_file_exits(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                run_ap("--work", d)

    def test_invalid_decisions_exits(self):
        with tempfile.TemporaryDirectory() as d:
            ocr = os.path.join(d, "ocr_merged")
            write_pages(ocr, {"p-001.png": page_json("p-001.png", [(10.0, "甲。")])})
            os.makedirs(os.path.join(d, "out"), exist_ok=True)
            dp = os.path.join(d, "out", "decisions.json")
            jsonio.write(dp, {"version": 1, "source": {}, "match": {},
                              "decisions": [{"key": "k", "page": "p-001.png",
                                             "y0": 10.0, "A": "甲。", "B": "乙。",
                                             "verdict": "C", "replacement": None,
                                             "level": "L1", "note": "",
                                             "status": "pending"}]})
            with self.assertRaises(SystemExit):
                run_ap("--work", d, "--decisions", dp)


class TestLegacyMigration(unittest.TestCase):
    """旧"硬编码裁决表"的迁移通道。

    表已经不写在源码里了（写在源码里 = 改一条裁决要改代码，也没法 diff 出改了哪条），
    改成一个数据文件；这里验证"按旧表的形状喂进来 -> 等价的 decisions.json"仍然成立。
    """

    QUEUE = [{"page": "p-%03d.png" % i, "y0": float(i * 100), "A": "A%d 行" % i,
              "B": "B%d 行" % i, "verdict": "?"} for i in range(1, 21)]

    # 形状与旧 DECISIONS 一致：{序号: (verdict, replacement, 说明)}
    LEGACY = {1: ("B", None, "第一处"), 2: ("B", None, "第二处"),
              3: ("B", None, "第三处（两个差异段）"), 4: ("A", None, "第四处"),
              5: ("B", None, ""), 6: ("A", None, ""), 7: ("?", None, "行尾破折号"),
              8: ("?", None, "上标脚注数字"), 9: ("A", None, ""),
              10: ("B", None, ""), 11: ("?", None, "仅空格差异"),
              12: ("B", None, ""), 13: ("A", None, ""), 14: ("B", None, ""),
              15: ("A", None, ""), 16: ("?", None, "仅空格差异"),
              17: ("A", None, ""), 18: ("B", None, "三个差异段"),
              19: ("A", None, ""), 20: ("A", None, "")}
    # {序号: (level, 说明)}
    NEEDS_HUMAN = {4: ("L3", "裴頠：原书印作颂"),
                   19: ("L3", "萧萣父：字形介于两者之间"),
                   20: ("L3", "萧萣父：同上"),
                   8: ("L3", "行末上标是脚注序号，应转成脚注")}

    def _table_file(self, d):
        p = os.path.join(d, "out", "legacy_decisions.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        jsonio.write(p, {
            "decisions": {str(k): list(v) for k, v in self.LEGACY.items()},
            "needs_human": {str(k): list(v) for k, v in self.NEEDS_HUMAN.items()},
        })
        return p

    def test_legacy_export_preserves_verdicts(self):
        """内置旧表 20 条 -> decisions.json：verdict 分布不变，下标 -> page@y0 主键。"""
        doc = ap.legacy_to_decisions(self.QUEUE, ap.LEGACY_DECISIONS,
                                     ap.LEGACY_NEEDS_HUMAN)
        self.assertEqual(len(doc["decisions"]), 20)
        self.assertEqual(dec.counts(doc), {"A": 8, "B": 8, "?": 4})
        self.assertEqual(doc["decisions"][0]["key"], "p-001.png@100")
        self.assertEqual(doc["decisions"][0]["verdict"], "B")
        self.assertEqual(doc["decisions"][2]["key"], "p-003.png@300")
        self.assertEqual(doc["source"]["queue_from"], "queue")
        self.assertEqual(dec.validate(doc), [])

    def test_legacy_marks_l3_entries(self):
        """留给人的条目迁成 level=L3 + 长 note，不能只剩一句终端打印（旧版打印完就丢了）。"""
        doc = ap.legacy_to_decisions(self.QUEUE, ap.LEGACY_DECISIONS,
                                     ap.LEGACY_NEEDS_HUMAN)
        l3 = [e for e in doc["decisions"] if e["level"] == "L3"]
        self.assertEqual(len(l3), 4)
        notes = " ".join(e["note"] for e in l3)
        for kw in ("裴頠", "萧萣父", "脚注"):
            self.assertIn(kw, notes, "留给人的条目必须带上判断依据，否则迁移后就丢了")

    def test_export_legacy_cli_uses_builtin_table(self):
        """CLI 不给 --legacy-table 时用内置旧表（部署目录里没有迁移数据文件也能跑）。"""
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            os.makedirs(out)
            jsonio.write(os.path.join(out, "review_queue.json"), self.QUEUE)
            dst = os.path.join(d, "legacy.json")
            rc, txt = run_ap("--work", d, "--export-legacy", dst)
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(dst))
            self.assertIn("20 条", txt)
            self.assertEqual(len(dec.read(dst)["decisions"]), 20)

    def test_export_legacy_cli_reads_table_file(self):
        """显式给 --legacy-table 时以文件为准（换一本书的迁移不改代码）。"""
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            os.makedirs(out)
            jsonio.write(os.path.join(out, "review_queue.json"), self.QUEUE)
            self._table_file(d)
            dst = os.path.join(d, "legacy.json")
            rc, _ = run_ap("--work", d, "--export-legacy", dst,
                           "--legacy-table", "out/legacy_decisions.json")
            self.assertEqual(rc, 0)
            self.assertEqual(len(dec.read(dst)["decisions"]), 20)

    def test_export_legacy_missing_table_file_exits(self):
        """显式指定了不存在的表文件时必须报错，不能静默退回内置表。"""
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            os.makedirs(out)
            jsonio.write(os.path.join(out, "review_queue.json"), self.QUEUE)
            with self.assertRaises(SystemExit):
                run_ap("--work", d, "--export-legacy", os.path.join(d, "x.json"),
                       "--legacy-table", "out/nope.json")


class TestCliCompat(unittest.TestCase):
    def test_queue_flag_still_works(self):
        """--queue 保留向后兼容：能用旧队列跑（只是没有裁决，全是待裁决）。"""
        with tempfile.TemporaryDirectory() as d:
            ocr = os.path.join(d, "ocr_merged")
            write_pages(ocr, {"p-001.png": page_json("p-001.png", [(10.0, "甲。")])})
            q = os.path.join(d, "review_queue.json")
            jsonio.write(q, [{"page": "p-001.png", "y0": 10.0, "A": "甲。",
                              "B": "乙。", "verdict": "?"}])
            rc, out = run_ap("--work", d, "--queue", q)
            self.assertEqual(rc, 0)
            self.assertEqual(summary(out)["skipped"], 1)

    def test_dry_run_flag_still_works(self):
        """--dry-run 保留（旧脚本就带这个参数，被别处调用时不能报错）。"""
        with tempfile.TemporaryDirectory() as d:
            ocr, dp = _fixture(d)
            rc, out = run_ap("--work", d, "--decisions", dp, "--dry-run")
            self.assertEqual(rc, 0)
            self.assertTrue(summary(out)["dry_run"])

    def test_cli_subprocess_default_dry_run(self):
        """进程级确认：不带 --apply 时退出码 0 且一个字节都没改。"""
        with tempfile.TemporaryDirectory() as d:
            ocr, dp = _fixture(d)
            p = os.path.join(ocr, "p-048.png.json")
            before = read_bytes(p)
            r = subprocess.run(
                [sys.executable, os.path.join(ROOT, "apply_decisions.py"),
                 "--work", d, "--decisions", dp],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("SUMMARY", r.stdout)
            self.assertEqual(before, read_bytes(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
