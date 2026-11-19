"""runs.stage_runnable 的回归测试：未跑阶段「点了会不会当场失败」。

这些用例针对两个**实测**的界面缺陷：

1. progress.js 原来只给已跑过的阶段画按钮，所有未跑阶段在界面上是死行；
2. 补上按钮还不够——runs/sengguan（《中国僧官制度史》）的 verify 是真的跑不了：
   它的文字层 usable=false（每页平均 10.0 个汉字），ocr_text 里一页 JSON 都没有，
   点下去只会 sys.exit("verify 需要先跑 textlayer 阶段")。那行报错说明缺什么文件，
   不说明这本书**永远**跑不出来。所以理由必须先算出来、先显示出来。

所以这里除了验 (bool, reason) 的取值，还验两件容易被顺手破坏的事：

- test_unrunnable_does_not_dirty_status：算「能不能跑」是**只读**的，不能顺手
  把 status 从 missing 改成别的，也不能写 manifest.json；
- test_never_raises：这条路径挂在 /api/state 的渲染链上，遇到坏 JSON、
  权限问题、run 不存在，都必须退化成「不可运行 + 一句理由」，不许抛。

运行：python -X utf8 -m unittest discover -s tests
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import unittest.mock as mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import jsonio                # noqa: E402
import manifest as mf        # noqa: E402
import paths                 # noqa: E402
import runs                  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "webui"))
import server as ws          # noqa: E402

# 与 runs/sengguan/work/ocr_text/_textlayer.meta 里的原文同形：判「理由有没有原文」
# 要靠一个**只可能来自 meta** 的片段，不能只匹配我们自己拼的那半句。
TL_REASON = ("每页平均只有 10.0 个汉字（阈值 50）：文字层要么是纯扫描件（实测 0），"
             "要么只剩页眉/页码（实测 10 汉字/页）")


def _pages_json(d, names):
    """在目录 d 里造若干页级 JSON（只造文件名，内容与本判定无关）。"""
    os.makedirs(d, exist_ok=True)
    for n in names:
        jsonio.write(os.path.join(d, n), {"name": n, "lines": []}, indent=1)


def _mk_tiered(base, name):
    root = os.path.join(base, name)
    paths.ensure_tiered(root)
    return root


def _textlayer_meta(root, usable, reason):
    p = paths.ocr_dir(root, "ocr_text", create=True)
    jsonio.write(os.path.join(p, "_textlayer.meta"),
                 {"usable": usable, "reason": reason, "kept_pages": 0}, indent=1)
    return p


def _stage(root, name):
    for st in runs.stage_list(root):
        if st["stage"] == name:
            return st
    raise AssertionError("阶段不存在：%s" % name)


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix=".tmp-stagerun-")
        self.addCleanup(shutil.rmtree, self.d, True)

    def run_root(self, name="book-a"):
        return _mk_tiered(self.d, name)


class TestVerify(unittest.TestCase):
    """verify 需要 A 侧（文字层）与 B 侧（ocr_fix）都有页 JSON。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix=".tmp-stagerun-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.root = _mk_tiered(self.d, "sengguan")
        _pages_json(paths.ocr_dir(self.root, "ocr_fix", create=True),
                    ["p-001.png.json", "p-002.png.json"])

    def test_unusable_textlayer_blocks_with_meta_reason(self):
        """文字层不可用 -> 不能跑，且理由必须带上 _textlayer.meta 里的原文。

        只写「先跑文字层」是不够的：这本的文字层**跑过了**，结论是不可用。
        用户照那句话去重跑，只会再得到一次 usable=false。
        """
        _textlayer_meta(self.root, False, TL_REASON)
        ok, why = runs.stage_runnable(self.root, "verify")
        self.assertFalse(ok)
        self.assertIn("汉字", why)
        self.assertIn(TL_REASON.split("：")[0], why)
        self.assertIn("文字层", why)
        # 必须说清「为什么这一侧是必需品」，而不是只说缺文件
        self.assertIn("A 侧", why)

    def test_blocks_when_textlayer_never_ran(self):
        """meta 都没有（压根没跑过文字层）：理由换成「先跑文字层」，不复制 meta 文案。"""
        ok, why = runs.stage_runnable(self.root, "verify")
        self.assertFalse(ok)
        self.assertIn("文字层", why)
        self.assertNotIn("汉字", why)

    def test_runnable_when_both_sides_have_pages(self):
        tl = _textlayer_meta(self.root, True, "350/350 页有文字，每页平均 620.0 个汉字")
        _pages_json(tl, ["p-001.png.json", "p-002.png.json"])
        ok, why = runs.stage_runnable(self.root, "verify")
        self.assertTrue(ok, why)
        self.assertEqual(why, "")

    def test_blocks_when_ocr_side_missing(self):
        """A 侧齐了但没有 ocr_fix：理由要指向 B 侧，不能笼统说「跑不了」。"""
        tl = _textlayer_meta(self.root, True, "可用")
        _pages_json(tl, ["p-001.png.json"])
        shutil.rmtree(paths.ocr_dir(self.root, "ocr_fix"), ignore_errors=True)
        ok, why = runs.stage_runnable(self.root, "verify")
        self.assertFalse(ok)
        self.assertIn("ocr_fix", why)

    def test_stage_list_carries_flags(self):
        _textlayer_meta(self.root, False, TL_REASON)
        st = _stage(self.root, "verify")
        self.assertFalse(st["can_run"])
        self.assertIn("汉字", st["why_not"])
        # 只加字段：原有用例靠的 status 仍是 missing
        self.assertEqual(st["status"], "missing")
        self.assertTrue(st["inferred"])


class TestRenderAndTextlayer(_TmpCase):
    def test_render_needs_a_pdf(self):
        root = self.run_root()
        ok, why = runs.stage_runnable(root, "render")
        self.assertFalse(ok)
        self.assertIn("没有源 PDF", why)

    def test_render_runnable_with_manifest_pdf(self):
        """manifest.input.pdf 是相对 run 根的路径（manifest.py:rel），不能当仓库相对。"""
        root = self.run_root()
        pdf = os.path.join(root, "src", "book.pdf")
        os.makedirs(os.path.dirname(pdf))
        open(pdf, "wb").close()
        mf.set_input(root, pdf=pdf)
        self.assertEqual(mf.read(root)["input"]["pdf"], "src/book.pdf")
        ok, why = runs.stage_runnable(root, "render")
        self.assertTrue(ok, why)

    def test_manifest_pdf_that_no_longer_exists_is_not_runnable(self):
        """I1 的悬空 PDF：manifest 记着、盘上没了，不能算能跑。"""
        root = self.run_root()
        mf.set_input(root, pdf=os.path.join(self.d, "gone.pdf"))
        ok, why = runs.stage_runnable(root, "render")
        self.assertFalse(ok)
        self.assertIn("没有源 PDF", why)

    def test_render_accepts_uploaded_source_pdf(self):
        """WebUI 上传落点 input/<book-id>/source.pdf 也算有源（此时 manifest 还是空的）。"""
        root = self.run_root("uploaded")
        with mock.patch.object(paths, "repo_root", lambda: self.d):
            dst = os.path.join(paths.input_dir("uploaded", create=True), "source.pdf")
            open(dst, "wb").close()
            ok, why = runs.stage_runnable(root, "render")
        self.assertTrue(ok, why)

    def test_textlayer_needs_pdf_first(self):
        """没 PDF 时文字层的理由是「没有源 PDF」，不是「缺 poppler」——顺序不能反。"""
        root = self.run_root()
        ok, why = runs.stage_runnable(root, "textlayer")
        self.assertFalse(ok)
        self.assertIn("没有源 PDF", why)

    def test_textlayer_needs_pdftotext(self):
        root = self.run_root()
        pdf = os.path.join(root, "b.pdf")
        open(pdf, "wb").close()
        mf.set_input(root, pdf=pdf)
        with mock.patch.object(runs.shutil, "which", lambda _n: None):
            ok, why = runs.stage_runnable(root, "textlayer")
        self.assertFalse(ok)
        self.assertIn("pdftotext", why)

    def test_textlayer_runnable_with_pdf_and_poppler(self):
        root = self.run_root()
        pdf = os.path.join(root, "b.pdf")
        open(pdf, "wb").close()
        mf.set_input(root, pdf=pdf)
        with mock.patch.object(runs.shutil, "which", lambda _n: "C:/poppler/pdftotext.exe"):
            ok, why = runs.stage_runnable(root, "textlayer")
        self.assertTrue(ok, why)


class TestChain(_TmpCase):
    """上游产物决定下游能不能跑：这条链上任何一环都不能假定「目录在 = 产物在」。"""

    def test_ocr_needs_rendered_pages(self):
        root = self.run_root()
        self.assertFalse(runs.stage_runnable(root, "ocr")[0])
        open(os.path.join(paths.pages_dir(root, create=True), "p-001.png"), "wb").close()
        self.assertTrue(runs.stage_runnable(root, "ocr")[0])

    def test_repair_needs_ocr(self):
        root = self.run_root()
        self.assertFalse(runs.stage_runnable(root, "repair")[0])
        _pages_json(paths.ocr_dir(root, "ocr", create=True), ["p-001.png.json"])
        self.assertTrue(runs.stage_runnable(root, "repair")[0])

    def test_layout_falls_back_to_ocr(self):
        """ocr_fix 空但 ocr 有页：layout 读得到东西，就该说能跑（不是死按钮）。"""
        root = self.run_root()
        self.assertFalse(runs.stage_runnable(root, "layout")[0])
        _pages_json(paths.ocr_dir(root, "ocr", create=True), ["p-001.png.json"])
        self.assertTrue(runs.stage_runnable(root, "layout")[0])
        _pages_json(paths.ocr_dir(root, "ocr_fix", create=True), ["p-001.png.json"])
        self.assertTrue(runs.stage_runnable(root, "layout")[0])

    def test_md_accepts_any_ocr_dir(self):
        root = self.run_root()
        ok, why = runs.stage_runnable(root, "md")
        self.assertFalse(ok)
        self.assertIn("OCR", why)
        _pages_json(paths.ocr_dir(root, "ocr_text", create=True), ["p-001.png.json"])
        self.assertTrue(runs.stage_runnable(root, "md")[0])

    def test_md_ignores_dirs_that_exist_but_have_no_pages(self):
        """目录在、一个页 JSON 都没有（O5 那种状态）：不能当成「有产物」。"""
        root = self.run_root()
        for name in ("ocr_layout", "ocr_fix", "ocr", "ocr_text"):
            os.makedirs(paths.ocr_dir(root, name, create=True), exist_ok=True)
        self.assertFalse(runs.stage_runnable(root, "md")[0])
        _pages_json(paths.ocr_dir(root, "ocr_layout"), ["p-001.png.json"])
        self.assertTrue(runs.stage_runnable(root, "md")[0])

    def test_flat_layout_is_resolved_through_paths(self):
        """旧书是 flat 布局（pages/ 直接躺在 run 根）：不能因为写死 work/ 就判成没页图。"""
        root = os.path.join(self.d, "flatbook")
        os.makedirs(os.path.join(root, "pages"))
        open(os.path.join(root, "pages", "p-001.png"), "wb").close()
        _pages_json(os.path.join(root, "ocr"), ["p-001.png.json"])
        self.assertEqual(paths.layout_of(root), paths.LAYOUT_FLAT)
        self.assertTrue(runs.stage_runnable(root, "ocr")[0])
        self.assertTrue(runs.stage_runnable(root, "repair")[0])
        self.assertTrue(runs.stage_runnable(root, "md")[0])


class TestReportStages(_TmpCase):
    def test_audit_and_epub_need_book_md(self):
        root = self.run_root()
        for name in ("audit", "epub"):
            ok, why = runs.stage_runnable(root, name)
            self.assertFalse(ok, name)
            self.assertIn("book.md", why)
        rep = paths.report_dir(root, create=True)
        with open(os.path.join(rep, "book.md"), "w", encoding="utf-8") as f:
            f.write("# 第一章\n")
        self.assertTrue(runs.stage_runnable(root, "audit")[0])
        self.assertTrue(runs.stage_runnable(root, "epub")[0])

    def test_epub_rejects_empty_book_md(self):
        """stage_epub 对 0 字节的 book.md 直接 sys.exit，界面必须同口径。"""
        root = self.run_root()
        rep = paths.report_dir(root, create=True)
        open(os.path.join(rep, "book.md"), "wb").close()
        self.assertTrue(runs.stage_runnable(root, "audit")[0])
        ok, why = runs.stage_runnable(root, "epub")
        self.assertFalse(ok)
        self.assertIn("非空", why)


class TestNeverRaises(_TmpCase):
    def test_unknown_stage(self):
        root = self.run_root()
        ok, why = runs.stage_runnable(root, "prune")
        self.assertFalse(ok)
        self.assertIn("prune", why)

    def test_missing_run_root(self):
        ok, why = runs.stage_runnable(os.path.join(self.d, "no-such-run"), "verify")
        self.assertFalse(ok)
        self.assertTrue(why)

    def test_corrupt_manifest_and_meta(self):
        """manifest 与 _textlayer.meta 都是元数据：坏了只能退化成「不可运行」，不能抛。"""
        root = self.run_root()
        with open(paths.manifest_path(root), "w", encoding="utf-8") as f:
            f.write("{ 这不是 JSON")
        p = paths.ocr_dir(root, "ocr_text", create=True)
        with open(os.path.join(p, "_textlayer.meta"), "w", encoding="utf-8") as f:
            f.write("{ 也不是")
        for name in runs.mf.STAGES:
            ok, why = runs.stage_runnable(root, name)
            self.assertIsInstance(ok, bool, name)
            self.assertIsInstance(why, str, name)
        self.assertFalse(runs.stage_runnable(root, "verify")[0])

    def test_oserror_is_swallowed(self):
        root = self.run_root()
        with mock.patch.object(runs.paths, "work_dir", side_effect=OSError("盘掉了")):
            ok, why = runs.stage_runnable(root, "ocr")
        self.assertFalse(ok)
        self.assertIn("盘掉了", why)


class TestReadOnly(_TmpCase):
    def test_unrunnable_does_not_dirty_status(self):
        """算「能不能跑」是只读操作：status 必须还是 missing，manifest 不能被写。"""
        root = self.run_root()
        before = json.dumps(mf.read(root), sort_keys=True, ensure_ascii=False)
        st = _stage(root, "verify")
        self.assertFalse(st["can_run"])
        self.assertEqual(st["status"], "missing")
        after = json.dumps(mf.read(root), sort_keys=True, ensure_ascii=False)
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(paths.manifest_path(root)))

    def test_summary_keeps_existing_fields(self):
        """只加字段：既有键一个都不能少（webui/API.md 已冻结）。"""
        root = self.run_root()
        _pages_json(paths.ocr_dir(root, "ocr", create=True), ["p-001.png.json"])
        s = runs.summary("book-a", repo=self.d)
        self.assertEqual([x["stage"] for x in s["stages"]], list(mf.STAGES))
        for st in s["stages"]:
            for k in ("stage", "status", "at", "count", "detail", "inferred"):
                self.assertIn(k, st)
            self.assertIsInstance(st["can_run"], bool)
            self.assertIsInstance(st["why_not"], str)
            if st["can_run"]:
                self.assertEqual(st["why_not"], "")
        # 进度口径没被这次改动带偏
        self.assertEqual(s["progress"]["total"], len(mf.STAGES))

    def test_summary_tolerant_of_garbage_input(self):
        self.assertIsNone(runs.summary("../../etc", repo=self.d))
        self.assertIsNone(runs.summary("no-such-book", repo=self.d))


class TestStateApi(unittest.TestCase):
    """起真实 ThreadingHTTPServer，验 /api/state 的 **JSON 里**带 can_run/why_not。

    不 mock 路由：前端读的就是这一份响应，字段没进 JSON 等于界面还是没有。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix=".tmp-stagerun-api-")
        cls._old_root = ws.ROOT
        ws.ROOT = cls.tmp
        cls.httpd = ws.ThreadingHTTPServer(("127.0.0.1", 0), ws.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.th = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        ws.ROOT = cls._old_root
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        for n in os.listdir(self.tmp):
            p = os.path.join(self.tmp, n)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)

    def _state(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request("GET", "/api/state")
            r = conn.getresponse()
            raw = r.read()
            self.assertEqual(r.status, 200)
            return json.loads(raw.decode("utf-8"))
        finally:
            conn.close()

    def test_state_stages_carry_can_run_and_why_not(self):
        root = _mk_tiered(self.tmp, "run-a")
        _pages_json(paths.ocr_dir(root, "ocr_fix", create=True), ["p-001.png.json"])
        _textlayer_meta(root, False, TL_REASON)

        d = self._state()
        self.assertEqual([r["id"] for r in d["runs"]], ["run-a"])
        stages = {s["stage"]: s for s in d["runs"][0]["stages"]}
        self.assertEqual(list(stages), list(mf.STAGES))
        for name, st in stages.items():
            self.assertIsInstance(st["can_run"], bool, name)
            self.assertIsInstance(st["why_not"], str, name)
        # 未跑 + 跑不了：状态仍是 missing，但理由已经在 JSON 里，前端画得出来
        self.assertEqual(stages["verify"]["status"], "missing")
        self.assertFalse(stages["verify"]["can_run"])
        self.assertIn("汉字", stages["verify"]["why_not"])
        # 没有源 PDF：render 也不该给活按钮，理由要能直接显示在行内
        self.assertFalse(stages["render"]["can_run"])
        self.assertIn("没有源 PDF", stages["render"]["why_not"])

    def test_state_reports_runnable_after_upload(self):
        """上传过 PDF 的书：render/textlayer 立刻变成能跑（manifest 还没写）。"""
        root = _mk_tiered(self.tmp, "book-b")
        with mock.patch.object(paths, "repo_root", lambda: self.tmp):
            dst = os.path.join(paths.input_dir("book-b", create=True), "source.pdf")
            open(dst, "wb").close()
            d = self._state()
        stages = {s["stage"]: s for s in d["runs"][0]["stages"]}
        self.assertTrue(stages["render"]["can_run"], stages["render"]["why_not"])
        self.assertEqual(stages["render"]["why_not"], "")
        self.assertFalse(stages["ocr"]["can_run"])


if __name__ == "__main__":
    unittest.main()
