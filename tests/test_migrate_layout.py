"""tools/migrate_layout.py 的回归测试：plan / apply / rollback 三步必须无损。

为什么单独测这个工具：它搬的是**全项目唯一不可重建的产物**——
run-lacan 的 2825 条 decisions.json（1157481 字节）和四本黄金 book.md。
搬错一次没有第二次机会，所以这里不测"大概能跑"，测的是三件硬事：

1. **plan 是纯函数**：预演阶段一个字节都不能动（工具默认不 --apply 就是这个理由）。
2. **apply 无损**：搬完 book.md / decisions.json 的 sha256 必须与搬之前**逐字节相同**，
   且每个 OCR 目录的文件数守恒（页数守恒是本项目的老纪律）。
3. **rollback 能原样倒回去**：搬回去之后，源树的产物指纹与迁移前完全一致，
   目标树不留空壳（work/report/review、runs/<id> 都要清掉）。

另外三条来自真实迁移的坑，也在这里锁住：

- **所有源树都进 runs/<book-id>/**，仓库根也不例外。这条规则只能有
  dest_root_of() 一处——第一版把它写成"只有仓库根换位置，run-*/ 就地迁移"，
  迁移后工作树里出现了两套布局（runs/hegel + run-lacan 就地变 tiered），
  正是这次迁移要消灭的东西。所以这里把"四本都在 runs/ 下"单独锁一条。
- **源目录搬完要删**：run-*/ 整个删，仓库根只删空掉的数据目录（代码一个都不碰）。
- **book id 规则**：run-lacan -> lacan、仓库根 -> hegel，且必须过 runs.RE_ID
  （中文/带斜杠/空一律拒绝）。手册里的路径是 runs/<book-id>，
  写错了 WebUI 的 URL 那一侧会多出第二类失败面。

运行：python -X utf8 -m unittest discover -s tests
"""
import json as _stdlib_json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import jsonio           # noqa: E402
import migrate_layout as ml   # noqa: E402
import paths            # noqa: E402
import runs             # noqa: E402


def _json(obj):
    """测试数据用标准库 json：jsonio 只负责**落盘**（原子 + 句柄），不提供序列化。"""
    return _stdlib_json.dumps(obj, ensure_ascii=False)


def _write(p, text="x"):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return p


def _sha(p):
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def make_flat_workdir(root, epub_name="某书.epub"):
    """造一棵假的 flat 工作目录：形如 run-lacan/（pages + ocr* + out/）。"""
    for d in ("pages", "ocr", "ocr_fix", "ocr_layout"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    for i in (1, 2, 3):
        _write(os.path.join(root, "pages", "p-%03d.png" % i), "PNG%d" % i)
        for d in ("ocr", "ocr_fix", "ocr_layout"):
            _write(os.path.join(root, d, "p-%03d.png.json" % i),
                   _json({"name": "p-%03d.png" % i, "lines": []}))
    out = os.path.join(root, "out")
    os.makedirs(out, exist_ok=True)
    _write(os.path.join(out, "book.md"), "# 第一章\n\n正文一段。\n")
    _write(os.path.join(out, "report.json"),
           _json({"book": {"title": "某书", "author": "某人",
                           "publisher": "某社"}}))
    _write(os.path.join(out, "qa.txt"), "qa\n")
    # 唯一不可重建的人工件
    _write(os.path.join(out, "decisions.json"),
           _json({"schema": "bookflow.decisions", "decisions": [
               {"page": "p-001.png", "y0": 10.0, "A": "甲", "B": "乙",
                "verdict": "?"}]}))
    # 插图目录跟着 report/ 走（book.md 用相对路径 images/ 引用它）
    _write(os.path.join(out, "images", "fig-p001-1.png"), "IMG")
    _write(os.path.join(out, epub_name), "PK-epub")
    return root


class _TempRepo(unittest.TestCase):
    """把 paths.repo_root() 打成临时目录。

    真实仓库根不能拿来测（测试会把仓库搬坏），而 dist/、runs/ 都是相对
    repo_root() 解析的——不 patch 就会把产物写进**真实仓库**（踩过）。
    """

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix=".tmp-mig-")
        self.addCleanup(shutil.rmtree, self.repo, True)
        patch = mock.patch.object(paths, "repo_root", return_value=self.repo)
        patch.start()
        self.addCleanup(patch.stop)


class TestFlatBookMigration(_TempRepo):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.repo, "run-lacan")
        make_flat_workdir(self.root)
        self.new = os.path.join(self.repo, "runs", "lacan")

    def test_plan_is_pure(self):
        """预演不许碰文件系统：工具默认不 --apply 的全部意义就在这里。"""
        before = sorted(os.walk(self.root))
        p = ml.plan(self.root)
        self.assertFalse(p["already"])
        self.assertEqual(sorted(os.walk(self.root)), before)
        self.assertEqual(p["book_id"], "lacan")
        self.assertEqual(p["dest_root"], self.new)
        tos = [m["to"] for m in p["moves"]]
        self.assertIn("work/pages", tos)
        self.assertIn("report/book.md", tos)
        self.assertIn("review/decisions.json", tos)
        self.assertIn("report/images", tos)
        self.assertTrue(any(t.startswith("dist/lacan/") for t in tos))

    def test_plan_is_empty_when_already_tiered(self):
        paths.ensure_tiered(self.new)
        p = ml.plan(self.root)
        self.assertTrue(p["already"])
        self.assertEqual(p["moves"], [])
        self.assertEqual(p["dest_root"], self.new)

    def test_apply_is_byte_lossless(self):
        """搬完 book.md / decisions.json 必须逐字节相同，页数守恒。"""
        md_before = _sha(os.path.join(self.root, "out", "book.md"))
        dec_before = _sha(os.path.join(self.root, "out", "decisions.json"))
        n_pages = len(os.listdir(os.path.join(self.root, "pages")))
        n_ocr = len(os.listdir(os.path.join(self.root, "ocr_layout")))

        with mock.patch("sys.stdout"):
            rc = ml.apply(self.root)
        self.assertEqual(rc, 0)

        self.assertTrue(paths.is_tiered(self.new))
        self.assertEqual(_sha(os.path.join(self.new, "report", "book.md")), md_before)
        self.assertEqual(_sha(os.path.join(self.new, "review", "decisions.json")),
                         dec_before)
        # 人工件与机器报告必须分家（decisions.json 不能躺在 report/ 里）
        self.assertFalse(os.path.exists(os.path.join(self.new, "report",
                                                     "decisions.json")))
        self.assertEqual(len(os.listdir(os.path.join(self.new, "work", "pages"))),
                         n_pages)
        self.assertEqual(len(os.listdir(os.path.join(self.new, "work", "ocr_layout"))),
                         n_ocr)
        # 交付物离开工作目录
        self.assertFalse(os.path.exists(os.path.join(self.new, "report", "某书.epub")))
        self.assertTrue(os.path.isfile(os.path.join(paths.dist_dir("lacan"),
                                                    "某书.epub")))
        # 源目录整个搬走：工作树只剩一套布局
        self.assertFalse(os.path.exists(self.root))

    def test_unknown_files_are_left_alone(self):
        """不认识的文件留在原处并进未分类清单——迁移工具不许替人判断。

        而且**源目录不能跟着删**：文件跟着目录一起消失是这个工具最不能犯的错。
        """
        _write(os.path.join(self.root, "out", "随手记.txt"), "人的笔记")
        p = ml.plan(self.root)
        self.assertIn("out/随手记.txt", p["unknown"])
        with mock.patch("sys.stdout"):
            ml.apply(self.root)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "out", "随手记.txt")))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "out")))

    def test_manifest_has_title_but_no_fake_stages(self):
        """迁移补的 manifest 只记输入身份，**不写 stages**——

        旧树没跑过的阶段就是没跑过，补一个假的 ok 会让进度条骗人（O5）。
        """
        with mock.patch("sys.stdout"):
            ml.apply(self.root)
        doc = jsonio.read(paths.manifest_path(self.new))
        self.assertEqual(doc["input"]["title"], "某书")
        self.assertEqual(doc["book_id"], "lacan")
        self.assertEqual(doc["stages"], {})
        self.assertIn("at", doc["_migrated"])
        self.assertEqual(doc["_migrated"]["guards"]["report/book.md"],
                         _sha(os.path.join(self.new, "report", "book.md")))

    def test_summary_reads_title_after_migration(self):
        """迁移后 runs.summary() 必须拿得到书名与页数（WebUI 的卡片就靠它）。"""
        with mock.patch("sys.stdout"):
            ml.apply(self.root)
        s = runs.summary("runs/lacan", repo=self.repo)
        self.assertIsNotNone(s)
        self.assertEqual(s["title"], "某书")
        self.assertEqual(s["pages"], 3)
        self.assertEqual(s["layout"], paths.LAYOUT_TIERED)

    def test_rollback_restores_flat_tree(self):
        """回滚要原样倒回去：源树指纹与迁移前一致，目标树不留空壳。"""
        md_before = _sha(os.path.join(self.root, "out", "book.md"))
        dec_before = _sha(os.path.join(self.root, "out", "decisions.json"))
        with mock.patch("sys.stdout"):
            ml.apply(self.root)
            # 源目录已经被整个删掉了，回滚要能用**原来的名字**找到它
            rc = ml.rollback(self.root)
        self.assertEqual(rc, 0)
        self.assertFalse(paths.is_tiered(self.root))
        self.assertEqual(_sha(os.path.join(self.root, "out", "book.md")), md_before)
        self.assertEqual(_sha(os.path.join(self.root, "out", "decisions.json")),
                         dec_before)
        for d in ("work", "report", "review"):
            self.assertFalse(os.path.exists(os.path.join(self.root, d)), d)
        # 迁移补的 manifest 要跟着删掉，否则回滚后 summary() 还在读它
        self.assertFalse(os.path.exists(os.path.join(self.new, "manifest.json")))
        # 目标树不留空壳：runs/lacan 连日志一起被清走
        self.assertFalse(os.path.exists(self.new))
        # 回滚记录本身一个字节都不丢（挪回源根）：它是"这批文件曾经在哪"的唯一证据
        self.assertTrue([f for f in os.listdir(self.root)
                         if f.startswith(".migration-")])

    def test_manifest_at_matches_log_even_across_second_boundary(self):
        """manifest 的 _migrated.at 必须**等于**日志的 at，不能各取一次时间。

        回滚靠 _migrated.at == log["at"] 判断「这份 manifest 是本次迁移写的」。
        两处各自 strftime，跨过整秒边界就对不上：回滚后 runs/<id>/manifest.json
        还在 -> runs/<id>/ 这个空壳也删不掉。这条**实测红过**（全套跑失败一次，
        单跑 25 次全绿的那种间歇性红），所以用假时钟把它钉死。
        """
        # 假时钟：日志先取时间，manifest 后取——旧实现在这里各取各的，会分叉。
        # 两种格式都要模拟：日志内容用 ISO（%Y-%m-%dT%H:%M:%S），
        # 日志**文件名**用 %Y%m%d-%H%M%S（带冒号的不能进文件名，Windows 会 EINVAL）。
        calls = {"n": 0}

        class FakeTime:
            @staticmethod
            def strftime(fmt):
                if "%Y%m%d-%H%M%S" == fmt:
                    return "20260101-000000"
                calls["n"] += 1
                # 第二秒起（manifest 写 _migrated.at 时）与日志的 at 分叉
                return "2026-01-01T00:00:00" if calls["n"] == 1 \
                    else "2026-01-01T00:00:01"

        with mock.patch.object(ml, "time", FakeTime), \
                mock.patch("sys.stdout"):
            ml.apply(self.root)
        log_path = [os.path.join(self.new, f) for f in os.listdir(self.new)
                    if f.startswith(".migration-")][0]
        log_at = jsonio.read(log_path)["at"]
        doc = jsonio.read(paths.manifest_path(self.new))
        self.assertEqual(doc["_migrated"]["at"], log_at)
        # 回滚必须能认出这份 manifest，并把目标树整个清掉
        with mock.patch("sys.stdout"):
            ml.rollback(self.root)
        self.assertFalse(os.path.exists(paths.manifest_path(self.new)))
        self.assertFalse(os.path.exists(self.new))

    def test_rollback_can_be_addressed_by_target_path(self):
        """回滚也可以直接给 runs/<book-id>（源目录已经不存在了）。"""
        with mock.patch("sys.stdout"):
            ml.apply(self.root)
            rc = ml.rollback(self.new)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "pages")))

    def test_second_apply_is_noop(self):
        """已经是 tiered 了就别再搬一次（幂等）。"""
        with mock.patch("sys.stdout") as out:
            ml.apply(self.root)
            ml.apply(self.root)
        self.assertTrue(any("已经是分层布局" in str(c) for c in out.write.call_args_list))


class TestBookId(unittest.TestCase):
    def test_run_prefix_is_stripped(self):
        repo = tempfile.mkdtemp(prefix=".tmp-id-")
        self.addCleanup(shutil.rmtree, repo, True)
        d = os.path.join(repo, "run-guixun")
        os.makedirs(d)
        self.assertEqual(ml.book_id_of(d), "guixun")

    def test_repo_root_uses_configured_id(self):
        repo = tempfile.mkdtemp(prefix=".tmp-id-")
        self.addCleanup(shutil.rmtree, repo, True)
        with mock.patch.object(paths, "repo_root", return_value=repo):
            self.assertEqual(ml.book_id_of(repo), ml.REPO_ROOT_BOOK_ID)
            self.assertEqual(ml.book_id_of(repo, None, "hegel2"), "hegel2")

    def test_illegal_id_is_rejected(self):
        """中文/带斜杠/空一律拒绝（runs.RE_ID 同一口径）。"""
        for bad in ("拉康", "a/b", "", "../x"):
            with self.assertRaises(SystemExit):
                ml._props(bad)

    def test_book_id_passes_runs_whitelist(self):
        repo = tempfile.mkdtemp(prefix=".tmp-id-")
        self.addCleanup(shutil.rmtree, repo, True)
        d = os.path.join(repo, "run-sengguan")
        os.makedirs(d)
        bid = ml.book_id_of(d)
        self.assertTrue(runs.RE_ID.match(bid))
        self.assertIsNotNone(runs.resolve("runs/" + bid, repo))

    def test_every_book_lands_under_runs(self):
        """**四本都要进 runs/<id>/**——包括 run-*/ 和仓库根。

        第一版规则写成「只有仓库根换位置」，迁移后工作树同时有
        runs/hegel/ 和就地 tiered 的 run-lacan/，等于两套布局并存，
        正是这次迁移要消灭的东西。
        """
        repo = tempfile.mkdtemp(prefix=".tmp-id-")
        self.addCleanup(shutil.rmtree, repo, True)
        with mock.patch.object(paths, "repo_root", return_value=repo):
            for name, want in (("run-lacan", "lacan"), ("run-guixun", "guixun"),
                               ("run-sengguan", "sengguan")):
                src = os.path.join(repo, name)
                os.makedirs(src, exist_ok=True)
                self.assertEqual(ml.dest_root_of(src, want),
                                 os.path.join(repo, "runs", want))
            self.assertEqual(ml.dest_root_of(repo, "hegel"),
                             os.path.join(repo, "runs", "hegel"))


class TestRepoRootIsSpecial(_TempRepo):
    """仓库根：数据搬进 runs/<book-id>/，代码必须留第一层。

    真实仓库根不能拿来测（会把仓库搬坏），这里把 paths.repo_root 打成临时目录
    来复刻真实形态。
    """

    def setUp(self):
        super().setUp()
        make_flat_workdir(self.repo, epub_name="黑格尔.epub")
        # 让仓库根"有代码"：这正是它的数据必须让开的原因
        _write(os.path.join(self.repo, "paths.py"), "# 代码\n")
        self.new = os.path.join(self.repo, "runs", "hegel")

    def test_apply_moves_repo_root_into_runs(self):
        md_before = _sha(os.path.join(self.repo, "out", "book.md"))
        with mock.patch("sys.stdout"):
            rc = ml.apply(self.repo)
        self.assertEqual(rc, 0)
        self.assertTrue(paths.is_tiered(self.new))
        self.assertEqual(_sha(os.path.join(self.new, "report", "book.md")), md_before)
        # 代码必须还在第一层，数据目录必须不在
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "paths.py")))
        for d in ("pages", "ocr", "out"):
            self.assertFalse(os.path.exists(os.path.join(self.repo, d)), d)
        # run id 变成 runs/hegel，且能被 runs.list_ids 发现
        self.assertIn("runs/hegel", runs.list_ids(self.repo))

    def test_rollback_restores_repo_root(self):
        with mock.patch("sys.stdout"):
            ml.apply(self.repo)
            ml.rollback(self.repo)
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "pages")))
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "out", "book.md")))
        self.assertFalse(os.path.isdir(os.path.join(self.repo, "runs")))
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "paths.py")))

    def test_apply_refuses_non_empty_dest(self):
        """目标已存在且非空时必须拒绝覆盖，不能把两份产物搅在一起。"""
        _write(os.path.join(self.new, "残骸.txt"), "上一次迁移剩下的东西")
        with mock.patch("sys.stdout"), self.assertRaises(SystemExit):
            ml.apply(self.repo)


class TestDiscover(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix=".tmp-disc-mig-")
        self.addCleanup(shutil.rmtree, self.repo, True)

    def test_finds_run_dirs_and_repo_root_last(self):
        make_flat_workdir(os.path.join(self.repo, "run-lacan"))
        make_flat_workdir(os.path.join(self.repo, "run-guixun"))
        make_flat_workdir(self.repo)     # 仓库根本身也是一本
        os.makedirs(os.path.join(self.repo, "docs"))
        patch = mock.patch.object(paths, "repo_root", return_value=self.repo)
        patch.start()
        self.addCleanup(patch.stop)
        got = ml.discover_targets(self.repo)
        ids = [b for _d, b, _r in got]
        self.assertEqual(sorted(ids), ["guixun", "hegel", "lacan"])
        # 仓库根必须排最后：它搬完要删的源目录最多
        self.assertEqual(ids[-1], "hegel")
        self.assertTrue(got[-1][2])

    def test_tiered_dirs_are_not_candidates(self):
        paths.ensure_tiered(os.path.join(self.repo, "run-done"))
        with mock.patch.object(paths, "repo_root", return_value=self.repo):
            self.assertEqual(ml.discover_targets(self.repo), [])


if __name__ == "__main__":
    unittest.main()
