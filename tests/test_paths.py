"""paths / manifest / runs 三个新模块的回归测试。

这三层是为「多级分层 + 可观测」加的骨架，测试重点不在"能不能跑"，
而在**三种布局必须同时成立**——四个既有工作目录（flat）一个字节都不能变，
新书（tiered）才能用上新目录。任何一条写错，就会变成
「旧书读不出来」或「新书又平铺回第一层」。

用例全部对应 IO-CONVENTIONS.md 里的编号：

- test_flat_layout_keeps_old_paths：flat 的 out/ 路径必须与改动前完全一致（O1/O6）。
- test_tiered_layout_splits_four_kinds：四类产物分家（§3 的四类边界表）。
- test_backup_is_outside_data_dir：备份不能落在被备份目录内部（O10）。
- test_manifest_missing_is_empty_not_error：manifest 缺失/损坏都不能挡住出书（§4.4）。
- test_failed_stage_is_not_done：failed 不算做完，否则进度条会骗人（O5）。
- test_run_id_rejects_traversal：run id 不许越出仓库。
- test_inferred_stages_are_marked：推断出来的阶段必须带 inferred 标记（O5）。

运行：python -X utf8 -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import jsonio        # noqa: E402
import manifest as mf   # noqa: E402
import paths         # noqa: E402
import runs          # noqa: E402


def touch(p, obj=None, text=""):
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    if obj is not None:
        jsonio.write(p, obj, indent=1)
    else:
        jsonio.write_text(p, text)
    return p


class TestLayout(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix=".tmp-paths-")
        self.addCleanup(__import__("shutil").rmtree, self.d, True)

    def test_empty_dir_is_flat(self):
        """空目录判成 flat：不能因为"还没建任何东西"就当成 tiered。"""
        self.assertEqual(paths.layout_of(self.d), paths.LAYOUT_FLAT)

    def test_flat_layout_keeps_old_paths(self):
        os.makedirs(os.path.join(self.d, "pages"))
        os.makedirs(os.path.join(self.d, "out"))
        self.assertEqual(paths.layout_of(self.d), paths.LAYOUT_FLAT)
        self.assertEqual(paths.work_dir(self.d), os.path.abspath(self.d))
        self.assertEqual(paths.report_dir(self.d), os.path.join(self.d, "out"))
        self.assertEqual(paths.review_dir(self.d), os.path.join(self.d, "out"))
        self.assertEqual(paths.decisions_path(self.d),
                         os.path.join(self.d, "out", "decisions.json"))
        self.assertEqual(paths.book_md(self.d),
                         os.path.join(self.d, "out", "book.md"))

    def test_tiered_layout_splits_four_kinds(self):
        dirs = paths.ensure_tiered(self.d)
        self.assertEqual(paths.layout_of(dirs), paths.LAYOUT_TIERED)
        self.assertEqual(paths.work_dir(dirs), os.path.join(self.d, "work"))
        self.assertEqual(paths.report_dir(dirs), os.path.join(self.d, "report"))
        self.assertEqual(paths.review_dir(dirs), os.path.join(self.d, "review"))
        # 裁决表（唯一不可重建的人工件）必须落在 review/，不能和机器报告同层
        self.assertEqual(paths.decisions_path(dirs),
                         os.path.join(self.d, "review", "decisions.json"))
        self.assertEqual(paths.pages_dir(dirs), os.path.join(self.d, "work", "pages"))
        self.assertEqual(paths.images_dir(dirs), os.path.join(self.d, "report", "images"))

    def test_manifest_alone_does_not_mark_tiered(self):
        """**只有 manifest.json 不算 tiered**。

        这条曾经写反过（把 manifest 也算进判定），代价是实打实的：
        在 flat 目录上跑一次阶段 -> 写下 manifest.json -> 整棵树立刻被判成 tiered
        -> 下一个阶段去 work/ 找 pages/，找不到。表现为「跑一次 md 之后
        run-lacan 的 out/ 整个消失」，而每一步的退出码都是 0。
        """
        touch(os.path.join(self.d, "manifest.json"), {"schema": mf.SCHEMA})
        self.assertFalse(paths.is_tiered(self.d))
        paths.ensure_tiered(self.d)
        self.assertTrue(paths.is_tiered(self.d))

    def test_work_dir_does_not_create_by_default(self):
        """只查询路径不该有副作用（否则 git status 会凭空多出空目录）。"""
        before = set(os.listdir(self.d))
        paths.work_dir(self.d, create=False)
        paths.report_dir(self.d, create=False)
        self.assertEqual(set(os.listdir(self.d)), before)

    def test_backup_is_outside_data_dir(self):
        """O10：备份必须落在 work/.backup，不能是 <src>/.bak。"""
        paths.ensure_tiered(self.d)
        bak = paths.backup_dir(self.d)
        ocr = paths.ocr_dir(self.d, "ocr_merged")
        self.assertFalse(os.path.abspath(bak).startswith(os.path.abspath(ocr) + os.sep))
        self.assertEqual(bak, os.path.join(self.d, "work", ".backup"))


class TestManifest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix=".tmp-man-")
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        paths.ensure_tiered(self.d)

    def test_manifest_missing_is_empty_not_error(self):
        doc = mf.read(self.d)
        self.assertEqual(doc["stages"], {})
        self.assertEqual(doc["input"], {})

    def test_corrupt_manifest_is_empty_not_error(self):
        """坏文件不能让流水线崩：元数据文件没有资格挡住出书。"""
        with open(os.path.join(self.d, "manifest.json"), "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        self.assertEqual(mf.read(self.d)["stages"], {})

    def test_set_input_records_sha256_and_relative_path(self):
        pdf = os.path.join(self.d, "work", "book.pdf")
        touch(pdf, None, "PDF-占位内容")
        inp = mf.set_input(self.d, pdf=pdf, title="某书", pages=3)
        self.assertEqual(inp["pdf"], "work/book.pdf")      # 相对、正斜杠
        self.assertEqual(len(inp["sha256"]), 64)
        self.assertEqual(inp["pages"], 3)

    def test_verify_input_detects_content_change(self):
        """路径相同、内容不同必须能判出来（I1：只比 realpath 是不够的）。"""
        pdf = os.path.join(self.d, "b.pdf")
        touch(pdf, None, "第一版")
        mf.set_input(self.d, pdf=pdf)
        self.assertEqual(mf.verify_input(self.d, pdf)[0], True)
        touch(pdf, None, "第二版内容不同")
        same, old, new = mf.verify_input(self.d, pdf)
        self.assertFalse(same)
        self.assertNotEqual(old, new)

    def test_set_stage_and_status(self):
        mf.set_stage(self.d, "render", "ok", count=310, out=os.path.join(self.d, "work", "pages"))
        rec = mf.read(self.d)["stages"]["render"]
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["count"], 310)
        self.assertEqual(rec["out"], "work/pages")
        self.assertEqual(mf.stage_status(self.d, "render"), "ok")
        self.assertEqual(mf.stage_status(self.d, "ocr"), "missing")

    def test_illegal_status_becomes_failed(self):
        mf.set_stage(self.d, "ocr", "weird")
        self.assertEqual(mf.stage_status(self.d, "ocr"), "failed")

    def test_failed_stage_is_not_done(self):
        """failed 不算做完：否则一个阶段失败，进度条仍显示 100%。"""
        mf.set_stage(self.d, "render", "ok")
        mf.set_stage(self.d, "ocr", "failed")
        done, total, pct, cur = mf.progress(self.d)
        self.assertEqual(done, 1)
        self.assertEqual(total, len(mf.STAGES))
        self.assertEqual(cur, "ocr")

    def test_degraded_and_skipped_count_as_done(self):
        mf.set_stage(self.d, "render", "ok")
        mf.set_stage(self.d, "textlayer", "degraded")
        mf.set_stage(self.d, "ocr", "skipped")
        done, _t, _p, cur = mf.progress(self.d)
        self.assertEqual(done, 3)
        self.assertEqual(cur, "")


class TestRunIds(unittest.TestCase):
    def test_resolve_rejects_traversal(self):
        for bad in ("../x", "a/b/c", "/abs/path", "C:/Windows", "..", "runs/../../x"):
            self.assertIsNone(runs.resolve(bad), bad)

    def test_resolve_accepts_dot_and_plain(self):
        repo = paths.repo_root()
        self.assertEqual(runs.resolve("."), repo)
        self.assertEqual(runs.resolve("run-lacan"),
                         os.path.join(repo, "run-lacan"))
        self.assertEqual(runs.resolve("runs/lacan"),
                         os.path.join(repo, "runs", "lacan"))

    def test_resolve_rejects_non_ascii_id(self):
        """中文目录名不接受：本项目的产物名已经带全角标点，再叠中文路径
        会在 URL 编码与命令行 quoting 上多出两类失败面。"""
        self.assertIsNone(runs.resolve("拉康"))


class TestRunSummary(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix=".tmp-run-")
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        self.root = os.path.join(self.d, "run-x")
        paths.ensure_tiered(self.root)

    def test_inferred_stages_are_marked(self):
        """没有 manifest 时，推断出来的每个阶段都必须带 inferred=True。"""
        touch(os.path.join(self.root, "work", "pages", "p-001.png"), None, "x")
        s = runs.summary("run-x", repo=self.d)
        self.assertEqual(s["pages"], 1)
        for st in s["stages"]:
            self.assertTrue(st["inferred"], st["stage"])
        self.assertEqual(s["stages"][0]["stage"], "render")
        self.assertEqual(s["stages"][0]["status"], "ok")

    def test_manifest_supplies_authoritative_status(self):
        mf.set_stage(self.root, "render", "degraded", count=2, detail="缺页 3")
        s = runs.summary("run-x", repo=self.d)
        r = [x for x in s["stages"] if x["stage"] == "render"][0]
        self.assertFalse(r["inferred"])
        self.assertEqual(r["status"], "degraded")
        self.assertEqual(r["detail"], "缺页 3")

    def test_pending_decisions_counts_question_marks(self):
        import decisions as dec
        doc = dec.merge([dec.make_decision("p-001.png", 10.0, A="a", B="b", verdict="?"),
                         dec.make_decision("p-002.png", 20.0, A="a", B="b", verdict="A")])
        dec.write(paths.decisions_path(self.root), doc)
        self.assertEqual(runs.pending_decisions(self.root), (1, 2))

    def test_unknown_run_returns_none(self):
        self.assertIsNone(runs.summary("no-such-book", repo=self.d))


if __name__ == "__main__":
    unittest.main()
