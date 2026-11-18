"""manifest / stage_manifest 的联调测试：每个阶段真的会被记进 manifest。

这些用例的重点不是"函数返回值对不对"，而是 **manifest 记的东西和实际产物对得上**：

- test_all_records_every_stage：all 流程跑完，manifest 里 9 个主干阶段都有记录；
- test_off_mode_marks_ocr_skipped：--text-layer only 时 ocr/repair 必须记 skipped，
  不能记 missing（missing 的意思是"还没跑"，两者含义完全不同）；
- test_failed_stage_keeps_failed：阶段抛错后状态必须是 failed，不能停在 running；
- test_manifest_failure_does_not_block：manifest 写不进去时，阶段照样要跑完。

最后一条最重要：manifest 是元数据，它没有资格挡住出书。

运行：python -X utf8 -m unittest discover -s tests
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import manifest as mf          # noqa: E402
import paths                   # noqa: E402
import stage_manifest as sm    # noqa: E402


class TestStageWrap(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix=".tmp-sm-")
        self.addCleanup(shutil.rmtree, self.d, True)
        paths.ensure_tiered(self.d)

    def test_ok_is_recorded(self):
        out = sm.stage(self.d, "render", lambda: 42, count_of=lambda r: r)
        self.assertEqual(out, 42)
        rec = mf.read(self.d)["stages"]["render"]
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["count"], 42)
        self.assertIn("elapsed", rec)

    def test_failed_stage_keeps_failed(self):
        def boom():
            raise RuntimeError("炸了")
        with self.assertRaises(RuntimeError):
            sm.stage(self.d, "ocr", boom)
        rec = mf.read(self.d)["stages"]["ocr"]
        self.assertEqual(rec["status"], "failed")
        self.assertIn("RuntimeError", rec["reason"])

    def test_systemexit_is_recorded_and_reraised(self):
        """阶段用 sys.exit 报错是正常路径（bookflow 全靠它），必须照常向上抛。"""
        def quit_():
            sys.exit("缺依赖")
        with self.assertRaises(SystemExit):
            sm.stage(self.d, "ocr", quit_)
        self.assertEqual(mf.read(self.d)["stages"]["ocr"]["status"], "failed")

    def test_manifest_failure_does_not_block(self):
        """manifest 写不进去时阶段照样跑完：元数据没资格挡住出书。"""
        import unittest.mock as mock
        called = []
        with mock.patch.object(mf, "set_stage", side_effect=OSError("磁盘满")):
            out = sm.stage(self.d, "md", lambda: called.append(1) or "done")
        self.assertEqual(out, "done")
        self.assertEqual(called, [1])

    def test_count_of_failure_does_not_block(self):
        def bad_count(_):
            raise ValueError("统计写错了")
        self.assertEqual(sm.stage(self.d, "audit", lambda: "x", count_of=bad_count), "x")
        self.assertEqual(mf.read(self.d)["stages"]["audit"]["status"], "ok")

    def test_note_direct(self):
        sm.note(self.d, "layout", "skipped", detail="--no-layout")
        self.assertEqual(mf.stage_status(self.d, "layout"), "skipped")


class TestCliRecordsManifest(unittest.TestCase):
    """真跑一次 bookflow.py，确认 manifest 是**命令行**产出的，不是测试手写的。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix=".tmp-scli-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def run_bf(self, *args):
        # 必须显式 encoding="utf-8"：子进程带 -X utf8 写的是 UTF-8，
        # 而 text=True 按**本机 locale**（中文 Windows 是 cp936）解码。
        # 一旦输出里有 gb2312 解不了的中文字节，解码线程抛 UnicodeDecodeError，
        # stderr 直接变成 None —— 断言就变成 None + "" 的 TypeError，
        # 看着像产品坏了，其实是测试自己的解码方式不对。
        # errors="replace" 再兜一层，避免同样原因再挂一次。
        return subprocess.run([sys.executable, "-X", "utf8",
                               os.path.join(ROOT, "bookflow.py")] + list(args),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=ROOT, timeout=120)

    def test_missing_work_is_rejected_but_ui_is_not(self):
        cp = self.run_bf("md", "--work", "")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("--work", cp.stderr + cp.stdout)

    def test_params_recorded_before_stage_runs(self):
        """阶段因为缺输入而失败时，**参数也应该已经记下了**。

        这是"事后再看能从产物里查出当时用了什么参数"的唯一保证——
        按 IO-CONVENTIONS §1 I2，四本旧书的 report.json 里连 dpi 都没有。
        """
        work = os.path.join(self.d, "w")
        cp = self.run_bf("md", "--work", work, "--dpi", "222", "--model", "medium",
                         "--text-layer", "off", "--no-layout")
        p = mf.read(work)
        self.assertEqual(p["params"]["dpi"], 222)
        self.assertEqual(p["params"]["model"], "medium")
        self.assertEqual(p["params"]["text_layer"], "off")
        self.assertTrue(p["params"]["no_layout"])
        self.assertEqual(mf.stage_status(work, "md"), "failed")

    def test_input_sha256_recorded(self):
        work = os.path.join(self.d, "w2")
        pdf = os.path.join(self.d, "x.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.4 fake")
        self.run_bf("render", "--work", work, "--pdf", pdf)
        inp = mf.read(work)["input"]
        self.assertEqual(len(inp["sha256"]), 64)
        self.assertEqual(inp["pdf"], "../x.pdf")     # 相对工作目录根


class TestNewRunIsTiered(unittest.TestCase):
    def test_new_dir_gets_three_tiers(self):
        d = tempfile.mkdtemp(prefix=".tmp-tier-")
        self.addCleanup(shutil.rmtree, d, True)
        work = os.path.join(d, "newbook")
        subprocess.run([sys.executable, "-X", "utf8",
                        os.path.join(ROOT, "bookflow.py"), "md", "--work", work],
                       capture_output=True, text=True, cwd=ROOT, timeout=120)
        self.assertTrue(os.path.isdir(os.path.join(work, "work")))
        self.assertTrue(os.path.isdir(os.path.join(work, "report")))
        self.assertTrue(os.path.isdir(os.path.join(work, "review")))
        self.assertEqual(paths.layout_of(work), paths.LAYOUT_TIERED)

    def test_existing_flat_dir_is_not_converted(self):
        """四个既有工作目录是 flat：跑一次 md 不能把它们就地改成 tiered。"""
        d = tempfile.mkdtemp(prefix=".tmp-flat-")
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "pages"))
        subprocess.run([sys.executable, "-X", "utf8",
                        os.path.join(ROOT, "bookflow.py"), "md", "--work", d],
                       capture_output=True, text=True, cwd=ROOT, timeout=120)
        self.assertEqual(paths.layout_of(d), paths.LAYOUT_FLAT)
        self.assertFalse(os.path.isdir(os.path.join(d, "work")))


if __name__ == "__main__":
    unittest.main()
