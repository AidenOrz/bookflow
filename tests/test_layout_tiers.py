"""分层布局落地后新增的行为：交付物出 dist/、打样限页、页图补零容错。

这些用例全部来自**真跑一本 9 页的 PDF** 时撞出来的问题，不是构造数据：

- test_small_book_page_images_are_found：pdftoppm 对 9 页的书出 p-1.png，
  而页级 JSON 一律叫 p-001.png。三个模块（run_layout / repair / make_review）
  各自拼名字，于是各自在小书上崩或静默跳过：
    * make_review 直接 FileNotFoundError，整本 all 在最后一步失败；
    * run_layout 全部「跳过（缺页面图）」，**退出码 0**，脚注静默降级成页尾注。
- test_layout_guard_compares_page_numbers：同一件事的上游版本——bookflow 的
  自己的"文件名对不上就跳过版面分析"守卫用的是文件名交集，在 <100 页的书上
  永远是空集，于是**连 run_layout 都没被调用**。
- test_tiered_deliverable_goes_to_dist：交付物与中间产物必须分家（§3 四类边界）。
- test_limit_pages_renders_only_prefix：打样预设必须真的只渲染前 N 页。

运行：python -X utf8 -m unittest discover -s tests
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import bookflow          # noqa: E402
import jsonio            # noqa: E402
import paths             # noqa: E402
import run_layout        # noqa: E402
import runs              # noqa: E402


def _touch_png(d, name):
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    open(p, "wb").close()
    return p


class TestPageImageTolerance(unittest.TestCase):
    """页图名必须容忍补零位数不同（pdftoppm 按页数决定位宽）。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix=".tmp-pad-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.pages = os.path.join(self.d, "pages")
        _touch_png(self.pages, "p-1.png")
        _touch_png(self.pages, "p-9.png")

    def test_page_image_tolerates_padding(self):
        """页图名必须容忍补零差异：pdftoppm 按页数决定位宽（9 页的书出 p-1.png），
        而页级 JSON 一律叫 p-001.png。"""
        # 名字完全一致时直接用
        self.assertEqual(os.path.basename(run_layout._page_image(self.pages, "p-1.png")),
                         "p-1.png")
        # JSON 叫 p-001.png、页图叫 p-1.png：必须能对上
        self.assertEqual(os.path.basename(run_layout._page_image(self.pages, "p-001.png")),
                         "p-1.png")

    def test_page_image_returns_none_when_absent(self):
        self.assertIsNone(run_layout._page_image(self.pages, "p-042.png"))
        self.assertIsNone(run_layout._page_image(os.path.join(self.d, "nope"), "p-1.png"))

    def test_repair_and_review_share_the_same_rule(self):
        """三个模块必须同一口径：run_layout / repair / make_review 各自拼名字，
        就会各自在小书上崩或静默跳过（实测 make_review 直接 FileNotFoundError）。

        make_review 是脚本式模块（import 时就 parse_args），所以不强求 import 它；
        改为在源码里断言它调的是同一个容错函数——这条能挡住"以后又把名字拼回去"。
        """
        with open(os.path.join(ROOT, "make_review.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("def page_image(", src)
        self.assertNotIn('Image.open(os.path.join(a.pages, name))', src)
        self.assertNotIn('Image.open(os.path.join(a.pages, h["page"]))', src)
        with open(os.path.join(ROOT, "repair.py"), encoding="utf-8") as f:
            rep = f.read()
        self.assertIn("def _page_image(", rep)
        self.assertNotIn('Image.open(os.path.join(a.pages, page))', rep)


class TestLayoutGuard(unittest.TestCase):
    """all 流程里"文件名对不上就跳过版面分析"的守卫必须按**页号**比。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix=".tmp-guard-")
        self.addCleanup(shutil.rmtree, self.d, True)
        paths.ensure_tiered(self.d)

    def _args(self):
        return bookflow.build_args(work=self.d, text_layer="off")

    def _seed(self, json_names, png_names):
        # 必须**先清空**：同一个用例里会 seed 两轮（正例、反例），
        # 上一轮的 p-1.png 残留会让第二轮的页号交集非空，反例就测不出来了。
        js = paths.ocr_dir(self.d, "ocr_fix", create=True)
        pg = paths.pages_dir(self.d, create=True)
        for d in (js, pg):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, exist_ok=True)
        for n in json_names:
            jsonio.write(os.path.join(js, n + ".json"),
                         {"name": n, "w": 100, "h": 100, "lines": []}, indent=1)
        for n in png_names:
            _touch_png(pg, n)

    def _guarded_call(self):
        """跑一次 stage_layout_guarded，返回 (被调用的参数列表, 输出文本)。"""
        import io
        from contextlib import redirect_stdout
        a = self._args()
        a.force = True
        called = []
        buf = io.StringIO()
        with mock.patch.object(bookflow, "stage_layout", lambda x: called.append(x)), \
                mock.patch.object(bookflow, "ensure_textlayer_coverage", lambda x: None), \
                redirect_stdout(buf):
            bookflow.stage_layout_guarded(a)
        return called, buf.getvalue()

    def test_guard_compares_page_numbers_not_filenames(self):
        """守卫必须按**页号**比，不能按文件名交集。

        旧版用文件名交集：9 页的书 JSON 是 p-001..009、页图是 p-1..9，
        交集永远是空集，于是**连 run_layout 都没被调用**（退出码 0）。
        """
        self._seed(["p-001.png"], ["p-1.png"])
        called, _out = self._guarded_call()
        self.assertEqual(len(called), 1, "页号一致时必须仍然调用版面分析")
        self.assertEqual(called[0].work, self.d, "调用的必须是这本的工作目录")

        # 真对不上时才跳过，并把原因打出来
        self._seed(["p-001.png"], ["p-500.png"])
        called, out = self._guarded_call()
        self.assertEqual(called, [])
        self.assertIn("页号对不上", out)


class TestDeliverableName(unittest.TestCase):
    def test_deliverable_name_is_safe_and_dated(self):
        """交付物名：带日期段（两次运行不互相覆盖）+ 过滤文件系统非法字符。"""
        self.assertRegex(bookflow._deliverable_name("某书"), r"^某书__\d{8}\.epub$")
        for bad in ('a/b', 'a\\b', 'a:b', 'a*b', 'a?b', 'a"b', 'a<b', 'a>b', 'a|b'):
            with self.subTest(title=bad):
                n = bookflow._deliverable_name(bad)
                for ch in '/\\:*?"<>|':
                    self.assertNotIn(ch, n, bad)
        # 空标题 / 全空白退回 book__
        self.assertTrue(bookflow._deliverable_name("").startswith("book__"))
        self.assertTrue(bookflow._deliverable_name("   ").startswith("book__"))
        # 超长标题截断，别让文件名撑爆路径上限
        n = bookflow._deliverable_name("书" * 200)
        self.assertLessEqual(len(n), 60 + len("__20261118.epub"))


class TestTieredDeliverableRouting(unittest.TestCase):
    """tiered 出到 dist/，flat 仍出在 out/。两者不能同时出现两份 epub。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix=".tmp-epubrt-")
        self.addCleanup(shutil.rmtree, self.d, True)

    def _run_epub(self, work, title):
        paths.ensure_tiered(work) if paths.is_tiered(work) else None
        rep = paths.report_dir(work, create=True)
        with open(os.path.join(rep, "book.md"), "w", encoding="utf-8") as f:
            f.write("# 第一章\n\n正文。\n")
        a = bookflow.build_args(work=work, title=title, author="某人")
        captured = []

        def fake_sh(cmd, **kw):
            captured.append(cmd)
            # 假装产出：写一个空文件到 --out 指定的位置
            try:
                i = cmd.index("--out")
            except ValueError:
                return 0, ""
            with open(cmd[i + 1], "wb") as f:
                f.write(b"PK")
            return 0, ""

        with mock.patch.object(bookflow, "sh", fake_sh), \
                mock.patch.object(bookflow.subprocess, "run", lambda *x, **k: None), \
                mock.patch("sys.stdout"):
            bookflow.stage_epub(a)
        return captured

    def test_flat_stays_in_out_and_tiered_goes_to_dist(self):
        """两者不能同时出现两份 epub：flat 出在 out/，tiered 出到 dist/。"""
        flat = os.path.join(self.d, "flatbook")
        os.makedirs(os.path.join(flat, "out"))
        self.assertFalse(paths.is_tiered(flat))
        cmds = self._run_epub(flat, "平铺的书")
        out = cmds[0][cmds[0].index("--out") + 1]
        self.assertEqual(os.path.dirname(out), os.path.join(flat, "out"))

        tier = os.path.join(self.d, "tierbook")
        paths.ensure_tiered(tier)
        cmds = self._run_epub(tier, "分层的书")
        out = cmds[0][cmds[0].index("--out") + 1]
        self.assertTrue(out.startswith(paths.dist_dir("tierbook")), out)
        self.assertRegex(os.path.basename(out), r"^分层的书__\d{8}\.epub$")


class TestLimitPages(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix=".tmp-lim-")
        self.addCleanup(shutil.rmtree, self.d, True)
        os.makedirs(os.path.join(self.d, "pages"))

    def _render(self, limit):
        calls = []
        a = bookflow.build_args(work=self.d, pdf=os.path.join(self.d, "x.pdf"), dpi=300)
        a.limit_pages = limit
        with mock.patch.object(bookflow, "pdf_page_count", lambda p: None), \
                mock.patch.object(bookflow, "sh", lambda cmd, **kw: calls.append(cmd)), \
                mock.patch("sys.stdout"):
            bookflow.stage_render(a)
        return calls

    def test_limit_pages_controls_first_and_last(self):
        """打样必须真的只渲染前 N 页；不给限制时不能凭空加上 -f/-l。"""
        calls = self._render(20)
        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertIn("-f", cmd)
        self.assertEqual(cmd[cmd.index("-f") + 1], "1")
        self.assertEqual(cmd[cmd.index("-l") + 1], "20")

        cmd = self._render(0)[0]
        self.assertNotIn("-f", cmd)
        self.assertNotIn("-l", cmd)


class TestConservativeFixPassedThrough(unittest.TestCase):
    def test_flag_reaches_make_md(self):
        d = tempfile.mkdtemp(prefix=".tmp-cf-")
        self.addCleanup(shutil.rmtree, d, True)
        paths.ensure_tiered(d)
        paths.ocr_dir(d, "ocr_fix", create=True)
        calls = []
        a = bookflow.build_args(work=d, conservative_fix=True)
        with mock.patch.object(bookflow, "ocr_dir", lambda x: paths.ocr_dir(d, "ocr_fix")), \
                mock.patch.object(bookflow, "ensure_textlayer_coverage", lambda x: None), \
                mock.patch.object(bookflow, "sh", lambda cmd, **kw: calls.append(cmd)):
            bookflow.stage_md(a)
        self.assertIn("--conservative-fix", calls[0])



# ---------------------------------------------------------------------------
# 已知坑（这次 WebUI + 分层布局新增的，都是实测踩出来的）
# ---------------------------------------------------------------------------
class TestDiscoveryRegressions(unittest.TestCase):
    """新书必须出现在运行列表里。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix=".tmp-disc-")
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_discovery_finds_new_books_and_skips_code_dirs(self):
        """新书必须出现在任务列表里；代码目录不能被当成书。

        `runs` 不能进 _SKIP——它有自己的分支，一旦被跳过，界面就看不见新书
        （而 404 会被前端兜成空卡片，看起来像"全部未跑"）。
        """
        paths.ensure_tiered(os.path.join(self.d, "runs", "b1"))
        self.assertIn("runs/b1", runs.list_ids(self.d),
                      "`runs` 不能进 _SKIP：它有自己的分支，一旦被跳过界面就看不见新书")

        os.makedirs(os.path.join(self.d, "flatbook", "out"))
        ids = runs.list_ids(self.d)
        self.assertIn("runs/b1", ids)
        self.assertIn("flatbook", ids, "旧的 flat 目录也要能发现")

    def test_skipped_dirs_are_not_runs(self):
        for n in ("docs", "tests", "webui", "dist", "input"):
            os.makedirs(os.path.join(self.d, n, "pages"))
        self.assertEqual(runs.list_ids(self.d), [])


class TestRouterShapes(unittest.TestCase):
    """前端路由必须认多段 run id（`runs/<book-id>`）。

    这条是前后端契约的一部分，但两边都不会"报错"——
    前端只会掉进 404 页，实测就发生了（`#/run/runs/book-x/inspect` 被判成 404）。
    这里用 Python 复刻那条规则，让契约至少有一处可执行的描述。
    """

    @staticmethod
    def parse(path):
        seg = [s for s in path.split("/") if s]
        if not seg or seg[0] == "runs":
            return ("runs", None)
        if seg[0] == "setup":
            return ("setup", None)
        if seg[0] == "run" and len(seg) >= 2:
            rest = seg[1:]
            view = "run"
            if rest and rest[-1] in ("triage", "inspect"):
                view = rest.pop()
            if rest:
                return (view, "/".join(rest))
        return ("notfound", None)

    def test_route_shapes(self):
        """多段 id 是契约的一部分：末段是视图名就吃掉，剩余全部是 id。

        猜错的后果是把人送去 404 页，实测就发生了（旧的
        #/run/runs/book-x/inspect 被判成 404）。所以「末段不认识就当 id」
        不是偷懒，是唯一不会把人送错页面的规则。
        """
        for path, want in (
            ("/run/runs/book-x/inspect", ("inspect", "runs/book-x")),
            ("/run/runs/book-x/triage", ("triage", "runs/book-x")),
            ("/run/runs/book-x", ("run", "runs/book-x")),
            ("/run/run-lacan/inspect", ("inspect", "run-lacan")),
            ("/run/run-lacan", ("run", "run-lacan")),
            # 末段不是视图名 -> 不当作视图，原样留给 id，交给后端 404
            ("/run/run-lacan/bogus", ("run", "run-lacan/bogus")),
            ("/nothing/here", ("notfound", None)),
        ):
            with self.subTest(path=path):
                self.assertEqual(self.parse(path), want)


if __name__ == "__main__":
    unittest.main()
