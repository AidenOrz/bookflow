"""paths / manifest / runs 三个新模块的回归测试。

这三层是为「多级分层 + 可观测」加的骨架，测试重点不在"能不能跑"，
而在**三种布局必须同时成立**——四个既有工作目录（flat）一个字节都不能变，
新书（tiered）才能用上新目录。任何一条写错，就会变成
「旧书读不出来」或「新书又平铺回第一层」。

用例对应 IO-CONVENTIONS.md 的编号：flat 的 out/ 路径与改动前完全一致（O1/O6）、
四类产物分家、备份不能在数据目录内部（O10）、manifest 缺失/损坏不挡出书、
failed 不算做完（O5）、run id 不许越界、推断阶段必须带 inferred 标记。

**用例组织方式**：同一个对象的规则合并进少数几个方法，用 subTest 分跑各条输入。
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
        """flat 的所有角色路径必须与改动前逐字一致——四个既有工作目录靠这条活着。"""
        os.makedirs(os.path.join(self.d, "pages"))
        os.makedirs(os.path.join(self.d, "out"))
        self.assertEqual(paths.layout_of(self.d), paths.LAYOUT_FLAT)
        for got, want in (
            (paths.work_dir(self.d), os.path.abspath(self.d)),
            (paths.report_dir(self.d), os.path.join(self.d, "out")),
            (paths.review_dir(self.d), os.path.join(self.d, "out")),
            (paths.decisions_path(self.d), os.path.join(self.d, "out", "decisions.json")),
            (paths.book_md(self.d), os.path.join(self.d, "out", "book.md")),
        ):
            with self.subTest(want=want):
                self.assertEqual(got, want)

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

    def test_queries_have_no_side_effects_and_backup_is_outside(self):
        """两条「不该发生的事」一起验：查询不建目录；备份不在数据目录内部。

        前者：只查路径却建出空目录，git status 会凭空多出东西。
        后者（O10）：备份放在被备份目录里，prune 删整个 ocr_merged 时连备份一起删。
        """
        before = set(os.listdir(self.d))
        paths.work_dir(self.d, create=False)
        paths.report_dir(self.d, create=False)
        self.assertEqual(set(os.listdir(self.d)), before)

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

    def test_missing_or_corrupt_manifest_is_empty_not_error(self):
        """元数据文件没有资格挡住出书：缺失与损坏都退回空骨架。"""
        doc = mf.read(self.d)
        self.assertEqual(doc["stages"], {})
        self.assertEqual(doc["input"], {})
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
        self.assertTrue(mf.verify_input(self.d, pdf)[0])
        touch(pdf, None, "第二版内容不同")
        same, old, new = mf.verify_input(self.d, pdf)
        self.assertFalse(same)
        self.assertNotEqual(old, new)

    def test_set_stage_records_and_corrects_illegal_status(self):
        mf.set_stage(self.d, "render", "ok", count=310,
                     out=os.path.join(self.d, "work", "pages"))
        rec = mf.read(self.d)["stages"]["render"]
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["count"], 310)
        self.assertEqual(rec["out"], "work/pages")
        self.assertEqual(mf.stage_status(self.d, "render"), "ok")
        self.assertEqual(mf.stage_status(self.d, "ocr"), "missing")
        # 非法状态就地纠正成 failed，不写脏数据
        mf.set_stage(self.d, "ocr", "weird")
        self.assertEqual(mf.stage_status(self.d, "ocr"), "failed")

    def test_progress_counts_only_finished_statuses(self):
        """failed 不算做完（否则一个阶段失败，进度条仍显示 100%）；
        degraded / skipped 算做完。"""
        mf.set_stage(self.d, "render", "ok")
        mf.set_stage(self.d, "textlayer", "degraded")
        mf.set_stage(self.d, "ocr", "skipped")
        done, total, _pct, cur = mf.progress(self.d)
        self.assertEqual(done, 3)
        self.assertEqual(total, len(mf.STAGES))
        self.assertEqual(cur, "")

        mf2 = tempfile.mkdtemp(prefix=".tmp-man2-")
        self.addCleanup(__import__("shutil").rmtree, mf2, True)
        mf.set_stage(mf2, "render", "ok")
        mf.set_stage(mf2, "ocr", "failed")
        done, total, _pct, cur = mf.progress(mf2)
        self.assertEqual(done, 1)
        self.assertEqual(cur, "ocr")


class TestRunIds(unittest.TestCase):
    def test_resolve_accepts_valid_shapes(self):
        repo = paths.repo_root()
        for rid, want in (
            (".", repo),
            ("run-lacan", os.path.join(repo, "run-lacan")),
            ("runs/lacan", os.path.join(repo, "runs", "lacan")),
        ):
            with self.subTest(run_id=rid):
                self.assertEqual(runs.resolve(rid), want)

    def test_resolve_rejects_traversal_and_non_ascii(self):
        """越界与中文 id 都不接受：产物名已带全角标点，再叠中文路径会在
        URL 编码与命令行 quoting 上多出两类失败面。"""
        for bad in ("../x", "a/b/c", "/abs/path", "C:/Windows", "..", "runs/../../x", "拉康"):
            with self.subTest(run_id=bad):
                self.assertIsNone(runs.resolve(bad))


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
        self.assertIsNone(runs.summary("no-such-book", repo=self.d))


if __name__ == "__main__":
    unittest.main()
