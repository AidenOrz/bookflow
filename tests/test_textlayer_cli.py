"""bookflow 文字层（--text-layer）三模式与阈值判定的回归测试。

用例全部对应"实测踩过的坑"，不是构造出来的假数据：

- test_off_mode_excludes_ocr_text：--text-layer off 必须一个字节都不碰文字层（硬回归）。
- test_only_mode_reads_only_text_layer：only 模式没有 OCR 结果，不能回头去读 OCR。
- test_verify_prefers_ocr_layout_over_text：ocr_layout/ocr_merged 都优先于原始文字层。
- test_verify_drops_text_when_unusable：文字层是纯扫描件时，绝不能选它当正文源
  （实测中国僧官制度史 10 汉字/页，选它等于正文清空）。
- test_threshold_pure_scan / test_threshold_header_only / test_threshold_partial_pages：
  三条判据各自的边界（平均值、有文字页占比）。
- test_parse_summary_*：只依赖 CLI 契约里的 SUMMARY 行，缺了要退化成自己扫。
- test_copy_usable_pages_skips_empty / test_page_names_follow_pages_dir：
  实测 pdftoppm 不补零（17 页书出 p-01.png），文件名对不上会让 layout 整本跳过。
- test_complete_textlayer_fills_from_ocr：页数守恒（缺页 = 少一段正文）。
- test_merge_keep_status：重跑 verify 不能把已经 applied 的裁决打回 pending。
- test_cli_accepts_three_modes / test_cli_rejects_unknown_mode：CLI 契约。

不依赖真 PDF、不依赖 textlayer.py（它是并行开发的模块，契约边界是 CLI 而不是 import）。

运行：python -m unittest discover -s tests
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

import bookflow                     # noqa: E402
import jsonio                       # noqa: E402


# ---------------------------------------------------------------------------
# 造数据的小工具
# ---------------------------------------------------------------------------

def page_json(name, text, w=2480, h=3509):
    """一页页级 JSON。text 为空字符串时是一页"没有文字"的空页。"""
    return {"name": name, "w": w, "h": h,
            "lines": ([] if not text else
                      [{"x0": 300.0, "y0": 400.0, "x1": 2100.0, "y1": 470.0,
                        "t": text, "s": 1.0}])}


def write_page(d, name, text):
    os.makedirs(d, exist_ok=True)
    jsonio.write(os.path.join(d, name + ".json"), page_json(name, text))


def mk_work(prefix=".tmp-bftl-"):
    return tempfile.mkdtemp(prefix=prefix)


def args(work, mode="verify", **kw):
    """造一个足够 ocr_dir()/active_dirs() 用的假 argparse 命名空间。"""
    class A:
        pass
    a = A()
    a.work = work
    a.text_layer = mode
    a.ocr_src = ""
    a.pdf = kw.get("pdf", "")
    a.pages = kw.get("pages", "")
    a.force = kw.get("force", False)
    return a


def cjk(n):
    return "字" * n


# ---------------------------------------------------------------------------
# 模式选择
# ---------------------------------------------------------------------------

class TestModeSelection(unittest.TestCase):
    def setUp(self):
        self.work = mk_work()

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_off_mode_excludes_ocr_text(self):
        """--text-layer off 不参与选源：产物必须与加功能之前逐字节一致。"""
        dirs = bookflow.active_dirs(args(self.work, "off"))
        self.assertNotIn(bookflow.TEXTLAYER_DIR, dirs)
        self.assertEqual(dirs, tuple(d for d in bookflow.OCR_DIRS
                                     if d != bookflow.TEXTLAYER_DIR))

    def test_only_mode_reads_only_text_layer(self):
        """only 模式没有 OCR 结果，只能读文字层。"""
        self.assertEqual(bookflow.active_dirs(args(self.work, "only")),
                         (bookflow.TEXTLAYER_DIR,))

    def test_verify_includes_text_when_usable(self):
        """文字层可用时参与竞争，且排在 ocr_fix 之前（它没有识别错误）。"""
        write_page(os.path.join(self.work, bookflow.TEXTLAYER_DIR),
                   "p-001.png", cjk(300))
        dirs = bookflow.active_dirs(args(self.work, "verify"))
        self.assertIn(bookflow.TEXTLAYER_DIR, dirs)
        self.assertLess(dirs.index(bookflow.TEXTLAYER_DIR), dirs.index("ocr_fix"))

    def test_verify_drops_text_when_unusable(self):
        """纯扫描件：文字层没有字，选它就是正文清空，必须排除。"""
        write_page(os.path.join(self.work, bookflow.TEXTLAYER_DIR),
                   "p-001.png", cjk(10))
        dirs = bookflow.active_dirs(args(self.work, "verify"))
        self.assertNotIn(bookflow.TEXTLAYER_DIR, dirs)

    def test_priority_puts_merged_before_text_before_fix(self):
        """顺序论证：ocr_layout > ocr_merged > ocr_text > ocr_fix > ocr。

        人工裁决过的 ocr_merged 必须压倒原始文字层（实测：拿未回填的 ocr 出书，
        黑格尔成书 232,904 汉字 vs 基准 233,812，差的 908 字全是没生效的裁决）。
        """
        d = bookflow.OCR_DIRS
        self.assertLess(d.index("ocr_layout"), d.index(bookflow.TEXTLAYER_DIR))
        self.assertLess(d.index("ocr_merged"), d.index(bookflow.TEXTLAYER_DIR))
        self.assertLess(d.index(bookflow.TEXTLAYER_DIR), d.index("ocr_fix"))
        self.assertLess(d.index("ocr_fix"), d.index("ocr"))

    def test_ocr_dir_off_mode_never_picks_text(self):
        """目录都在时，off 模式仍必须选 ocr_fix，绝不选 ocr_text。"""
        write_page(os.path.join(self.work, bookflow.TEXTLAYER_DIR),
                   "p-001.png", cjk(300))
        write_page(os.path.join(self.work, "ocr_fix"), "p-001.png", cjk(300))
        picked = bookflow.ocr_dir(args(self.work, "off"))
        self.assertEqual(os.path.basename(os.path.normpath(picked)), "ocr_fix")

    def test_ocr_dir_verify_mode_reports_which_source(self):
        """选了文字层要明确打印"选了谁、为什么"，不能让用户猜。"""
        write_page(os.path.join(self.work, bookflow.TEXTLAYER_DIR),
                   "p-001.png", cjk(300))
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            picked = bookflow.ocr_dir(args(self.work, "verify"))
        out = buf.getvalue()
        self.assertEqual(os.path.basename(os.path.normpath(picked)),
                         bookflow.TEXTLAYER_DIR)
        self.assertIn("正文源", out)
        self.assertIn(bookflow.TEXTLAYER_DIR, out)


# ---------------------------------------------------------------------------
# 阈值判定
# ---------------------------------------------------------------------------

class TestThresholds(unittest.TestCase):
    def test_pure_scan_is_unusable(self):
        """纯扫描件：pdftotext 返回空，实测 7 个扫描书全是 0 汉字/页。"""
        st = {"pages": 300, "with_text": 0, "empty": 300,
              "cjk_total": 0, "cjk_per_page": 0.0}
        ok, why = bookflow.textlayer_usable(st)
        self.assertFalse(ok)
        self.assertIn("汉字", why)

    def test_header_only_is_unusable(self):
        """只剩页眉页码：实测中国僧官制度史 10 汉字/页。"""
        st = {"pages": 350, "with_text": 350, "empty": 0,
              "cjk_total": 3500, "cjk_per_page": 10.0}
        ok, why = bookflow.textlayer_usable(st)
        self.assertFalse(ok)
        self.assertIn("10.0", why)

    def test_partial_pages_is_unusable(self):
        """大部分页有字、少数页没有：同样判不可用。

        实测这本书是「扫描件里插了几页数字页」（《SOC-V8-CN》30% 有字但均值 86.9）。
        让它当正文源有**两个**独立问题：没字的那几页整页消失（虽然有 complete_textlayer
        用 OCR 补），以及文字层与 OCR 的分行口径不同、混源的页边界会让 layout 的
        跨页合段误判。所以阈值压到"几乎整本都有文字"才放行。
        """
        st = {"pages": 10, "with_text": 8, "empty": 2,
              "cjk_total": 3000, "cjk_per_page": 300.0}
        ok, why = bookflow.textlayer_usable(st)
        self.assertFalse(ok)
        self.assertIn("8/10", why)

    def test_average_already_high_is_still_rejected(self):
        """平均值过关也不能放行——这正是 PAGE_RATIO 这条判据存在的理由。"""
        st = {"pages": 10, "with_text": 3, "empty": 7,
              "cjk_total": 2000, "cjk_per_page": 200.0}
        ok, why = bookflow.textlayer_usable(st)
        self.assertFalse(ok)
        self.assertIn("3/10", why)

    def test_good_text_layer_is_usable(self):
        """真文字层：《主义主义》实测 100% 有字、481 汉字/页。"""
        st = {"pages": 200, "with_text": 200, "empty": 0,
              "cjk_total": 96000, "cjk_per_page": 480.0}
        ok, why = bookflow.textlayer_usable(st)
        self.assertTrue(ok)
        self.assertIn("200/200", why)

    def test_one_blank_page_still_usable(self):
        """个别空白页不影响判定：阈值 0.95 就是为「几乎整本有字」留的余量。"""
        st = {"pages": 200, "with_text": 199, "empty": 1,
              "cjk_total": 95000, "cjk_per_page": 475.0}
        ok, _ = bookflow.textlayer_usable(st)
        self.assertTrue(ok)

    def test_empty_summary_is_unusable(self):
        for st in ({}, None, {"pages": 0, "with_text": 0, "empty": 0,
                              "cjk_total": 0, "cjk_per_page": 0.0}):
            ok, why = bookflow.textlayer_usable(st)
            self.assertFalse(ok)
            self.assertTrue(why)

    def test_stats_agree_with_thresholds(self):
        """textlayer_stats 现算的结果要能直接喂给判定（SUMMARY 缺失时的兜底）。"""
        work = mk_work()
        try:
            d = os.path.join(work, bookflow.TEXTLAYER_DIR)
            write_page(d, "p-001.png", cjk(300))
            write_page(d, "p-002.png", "")
            st = bookflow.textlayer_stats(d)
            self.assertEqual(st["pages"], 2)
            self.assertEqual(st["with_text"], 1)
            self.assertEqual(st["empty"], 1)
            self.assertEqual(st["cjk_total"], 300)
            self.assertAlmostEqual(st["cjk_per_page"], 150.0)
        finally:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# SUMMARY 行（CLI 契约）
# ---------------------------------------------------------------------------

class TestParseSummary(unittest.TestCase):
    def test_parses_last_summary_line(self):
        out = ('文字层：17 页（有字 17 / 空页 0），5488 汉字\n'
               'SUMMARY {"pages": 17, "with_text": 17, "empty": 0, '
               '"cjk_total": 5488, "cjk_per_page": 322.8}\n')
        st = bookflow.parse_summary(out)
        self.assertEqual(st["pages"], 17)
        self.assertAlmostEqual(st["cjk_per_page"], 322.8)

    def test_missing_summary_returns_none(self):
        """没打 SUMMARY 时返回 None，让调用方退化成自己扫产物。"""
        self.assertIsNone(bookflow.parse_summary("完成 17 页\n"))
        self.assertIsNone(bookflow.parse_summary(""))

    def test_broken_json_returns_none(self):
        self.assertIsNone(bookflow.parse_summary("SUMMARY {not json}"))

    def test_usable_false_summary_still_parsed(self):
        """exit code 2 时 textlayer.py 也会打 SUMMARY，只是 usable=false。"""
        out = ('SUMMARY {"pages": 300, "with_text": 0, "empty": 300, '
               '"cjk_total": 0, "cjk_per_page": 0.0, "usable": false}')
        st = bookflow.parse_summary(out)
        self.assertEqual(st["pages"], 300)
        ok, _ = bookflow.textlayer_usable(st)
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# 按页筛选与页名对齐
# ---------------------------------------------------------------------------

class TestPageFiltering(unittest.TestCase):
    def setUp(self):
        self.work = mk_work()

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_copy_usable_pages_skips_empty(self):
        """没文字的页不许写进 ocr_text（写空页 = 用空白覆盖正文）。"""
        raw = os.path.join(self.work, bookflow.TEXTLAYER_RAW_DIR)
        dst = os.path.join(self.work, bookflow.TEXTLAYER_DIR)
        write_page(raw, "p-001.png", cjk(300))
        write_page(raw, "p-002.png", "")
        n = bookflow.copy_usable_pages(raw, dst)
        self.assertEqual(n, 1)
        self.assertTrue(os.path.exists(os.path.join(dst, "p-001.png.json")))
        self.assertFalse(os.path.exists(os.path.join(dst, "p-002.png.json")))

    def test_page_names_follow_pages_dir(self):
        """pdftoppm 不补零：17 页的书 pages/ 里是 p-01.png，JSON 必须同名，
        否则 run_layout 会「跳过（缺页面图）」且退出码 0（实测整本跳过）。"""
        pages = os.path.join(self.work, "pages")
        os.makedirs(pages)
        for i in (1, 2, 17):
            open(os.path.join(pages, "p-%02d.png" % i), "wb").close()
        raw = os.path.join(self.work, bookflow.TEXTLAYER_RAW_DIR)
        write_page(raw, "p-001.png", cjk(300))
        write_page(raw, "p-017.png", cjk(300))
        dst = os.path.join(self.work, bookflow.TEXTLAYER_DIR)
        bookflow.copy_usable_pages(raw, dst, index=bookflow.page_image_index(self.work))
        self.assertTrue(os.path.exists(os.path.join(dst, "p-01.png.json")))
        self.assertTrue(os.path.exists(os.path.join(dst, "p-17.png.json")))
        doc = jsonio.read(os.path.join(dst, "p-01.png.json"))
        self.assertEqual(doc["name"], "p-01.png")

    def test_complete_textlayer_fills_from_ocr(self):
        """页数守恒：没文字的页从 OCR 补回来，否则成书少一段正文。"""
        raw = os.path.join(self.work, bookflow.TEXTLAYER_RAW_DIR)
        dst = os.path.join(self.work, bookflow.TEXTLAYER_DIR)
        write_page(raw, "p-001.png", cjk(300))
        write_page(raw, "p-002.png", "")
        bookflow.copy_usable_pages(raw, dst)
        write_page(os.path.join(self.work, "ocr_fix"), "p-002.png", cjk(280))
        n = bookflow.complete_textlayer(self.work, "ocr_fix")
        self.assertEqual(n, 1)
        self.assertTrue(os.path.exists(os.path.join(dst, "p-002.png.json")))
        got, total = bookflow.textlayer_coverage(self.work)
        self.assertEqual((got, total), (2, 2))

    def test_complete_textlayer_reports_impossible(self):
        """没有 OCR 可补时返回 -1，调用方据此报错而不是静默产出残书。"""
        raw = os.path.join(self.work, bookflow.TEXTLAYER_RAW_DIR)
        dst = os.path.join(self.work, bookflow.TEXTLAYER_DIR)
        write_page(raw, "p-001.png", cjk(300))
        write_page(raw, "p-002.png", "")
        bookflow.copy_usable_pages(raw, dst)
        self.assertEqual(bookflow.complete_textlayer(self.work, "ocr_fix"), -1)

    def test_only_mode_keeps_empty_pages(self):
        """only 模式没有任何 OCR 可补，空页也要写出去，页数才对得上。"""
        raw = os.path.join(self.work, bookflow.TEXTLAYER_RAW_DIR)
        dst = os.path.join(self.work, bookflow.TEXTLAYER_DIR)
        write_page(raw, "p-001.png", cjk(300))
        write_page(raw, "p-002.png", "")
        n = bookflow.copy_usable_pages(raw, dst, include_empty=True)
        self.assertEqual(n, 2)


# ---------------------------------------------------------------------------
# 裁决表合并
# ---------------------------------------------------------------------------

class TestMergeKeepStatus(unittest.TestCase):
    def test_applied_status_survives_rerun(self):
        """重跑 verify 不能让已经 applied 的裁决打回 pending（否则下次又改一遍）。"""
        old = {"version": 1, "source": {}, "match": {},
               "decisions": [{"key": "p-001.png@100", "page": "p-001.png", "y0": 100.0,
                              "A": "甲", "B": "乙", "verdict": "B",
                              "replacement": None, "level": "L1", "note": "",
                              "status": "applied"}]}
        new = {"version": 1, "source": {}, "match": {},
               "decisions": [{"key": "p-001.png@100", "page": "p-001.png", "y0": 100.0,
                              "A": "甲", "B": "乙", "verdict": "B",
                              "replacement": None, "level": "L1", "note": "",
                              "status": "pending"}]}
        merged = bookflow._merge_keep_status(old, new)
        self.assertEqual(merged["decisions"][0]["status"], "applied")

    def test_new_entries_kept(self):
        old = {"decisions": [{"key": "p-001.png@100", "page": "p-001.png", "y0": 100.0,
                              "verdict": "B", "status": "applied"}]}
        new = {"decisions": [{"key": "p-002.png@200", "page": "p-002.png", "y0": 200.0,
                              "verdict": "?", "status": "pending"}]}
        merged = bookflow._merge_keep_status(old, new)
        keys = {e["key"] for e in merged["decisions"]}
        self.assertEqual(keys, {"p-001.png@100", "p-002.png@200"})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCli(unittest.TestCase):
    def run_bf(self, *argv):
        return subprocess.run([sys.executable, "-X", "utf8",
                               os.path.join(ROOT, "bookflow.py")] + list(argv),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)

    def test_help_lists_three_modes(self):
        cp = self.run_bf("--help")
        self.assertEqual(cp.returncode, 0)
        for mode in bookflow.TEXTLAYER_MODES:
            self.assertIn(mode, cp.stdout)

    def test_default_is_verify_and_accepts_off(self):
        """默认 verify（用户明确要求默认交叉验证），off/only 也必须能解析。"""
        work = mk_work()
        try:
            for mode in ("off", "only", "verify"):
                cp = self.run_bf("md", "--work", work, "--text-layer", mode)
                # 没跑过任何阶段，md 会因找不到 OCR 结果退出，但参数解析必须通过
                self.assertNotIn("invalid choice", cp.stderr)
                self.assertNotEqual(cp.returncode, 2)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_rejects_unknown_mode(self):
        cp = self.run_bf("md", "--work", ".", "--text-layer", "bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("invalid choice", cp.stderr)

    def test_text_pages_rejected_outside_textlayer_stage(self):
        """--text-pages 是试跑参数，和 all/下游阶段组合必然产残书（实测崩在 audit 缺页图）。

        入口就拒绝：既避免半路炸在一个看不懂的地方，也避免用户把"残书"当成程序坏了。
        """
        for stage in ("all", "md", "verify"):
            cp = self.run_bf(stage, "--work", "", "--pdf", "x.pdf",
                             "--text-pages", "40-140")
            self.assertNotEqual(cp.returncode, 0, stage)
            self.assertIn("--text-pages", cp.stderr if cp.stderr else cp.stdout)


# ---------------------------------------------------------------------------
# 子进程管理（失败可读 + 不留孤儿）
# ---------------------------------------------------------------------------

class TestSubprocessHandling(unittest.TestCase):
    def test_sh_failure_gives_human_message_not_traceback(self):
        """子进程失败时给人话：哪个脚本、退出码、完整命令。"""
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                bookflow.sh([sys.executable, "-c", "import sys; sys.exit(3)"])
        msg = str(cm.exception.code)
        self.assertIn("退出码 3", msg)
        self.assertIn("sys.exit(3)", msg)  # 命令原文在里面，能直接复制去跑
        self.assertNotIn("Traceback", msg)

    def test_sh_start_failure_is_reported(self):
        """命令根本起不来时也要说人话，不能抛 OSError。"""
        with self.assertRaises(SystemExit) as cm:
            bookflow.sh(["definitely-not-a-real-command-xyz"])
        self.assertIn("启动失败", str(cm.exception.code))

    def test_kill_tree_stops_child(self):
        """Ctrl+C 时子进程必须跟着死——实测 pdftoppm 会变孤儿继续跑 20 分钟、
        占着文件句柄让 pages/ 删不掉。"""
        import time
        cmd = (["ping", "-n", "60", "127.0.0.1"] if os.name == "nt"
               else ["sleep", "60"])
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.3)
            self.assertIsNone(p.poll())
            bookflow.kill_tree(p)
            time.sleep(0.8)
            self.assertIsNotNone(p.poll(), "kill_tree 没杀掉子进程")
        finally:
            if p.poll() is None:
                p.kill()

    def test_kill_tree_on_finished_process_is_noop(self):
        """已经退出的进程再 kill 一次不能抛（清理路径会被重复调用）。"""
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        bookflow.kill_tree(p)          # 不抛就算过
        bookflow.kill_tree(None)


if __name__ == "__main__":
    unittest.main()
