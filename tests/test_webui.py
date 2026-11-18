"""webui/server.py 的回归测试：契约、路径安全、审批闸门。

不起浏览器、不跑真 OCR：用真实的 ThreadingHTTPServer 起在随机端口上，
用 http.client 打请求。这样测的是**真的路由分发**，不是被 mock 掉的假对象。

重点验的不是"能不能返回 200"，而是三件真会咬人的事：

- test_path_traversal_blocked：run id / file 参数不许越出仓库（能读 /etc 就等于任意文件读）；
- test_apply_requires_confirm：回填会**原地覆写页级 JSON**，没有 confirm=true 必须 400；
- test_decisions_post_is_all_or_nothing：一条错就整份不写（与 table --import 同口径），
  否则会出现"改了一半"的裁决表，而它正是唯一不可重建的产物。

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
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import decisions as dec      # noqa: E402
import jsonio                # noqa: E402
import manifest as mf        # noqa: E402
import paths                 # noqa: E402
import runs                  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "webui"))
import server as ws          # noqa: E402


def _mk_run(base, name, lines=2, verdict="?"):
    """造一棵 tiered 工作目录：work/pages + work/ocr_fix + review/decisions.json。"""
    root = os.path.join(base, name)
    paths.ensure_tiered(root)
    pages = paths.pages_dir(root, create=True)
    ocr = paths.ocr_dir(root, "ocr_fix", create=True)
    doc = []
    for i in range(1, lines + 1):
        pn = "p-%03d.png" % i
        open(os.path.join(pages, pn), "wb").close()
        jsonio.write(os.path.join(ocr, pn + ".json"), {
            "name": pn, "w": 1000, "h": 1400,
            "lines": [{"x0": 10, "y0": 100.0 * i, "x1": 900, "y1": 100.0 * i + 40,
                       "t": "原文第%d行" % i, "s": 0.9}]}, indent=1)
        doc.append(dec.make_decision(pn, 100.0 * i, A="原文第%d行" % i,
                                     B="改后第%d行" % i, verdict=verdict,
                                     note="audit 词表建议 原→改；需看图确认"))
    dec.write(paths.decisions_path(root), dec.merge(doc))
    return root


class ServerCase(unittest.TestCase):
    """起一个真实 HTTServer，换掉 ROOT 指向临时目录。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix=".tmp-webui-")
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

    # ---- 小工具 ----
    def req(self, method, path, body=None, ctype="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        data = None
        headers = {}
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            headers["Content-Type"] = ctype
            headers["Content-Length"] = str(len(data))
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        head = dict(r.getheaders())
        conn.close()
        return r.status, head, raw

    def jreq(self, method, path, body=None):
        st, _h, raw = self.req(method, path, body)
        try:
            return st, json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return st, {"_raw": raw[:200]}


class TestState(ServerCase):
    def test_state_lists_presets_and_runs(self):
        _mk_run(self.tmp, "run-a")
        st, d = self.jreq("GET", "/api/state")
        self.assertEqual(st, 200)
        self.assertEqual(d["version"], ws.API_VERSION)
        self.assertEqual([p["id"] for p in d["presets"]], ["scan", "modern", "sample"])
        self.assertEqual([r["id"] for r in d["runs"]], ["run-a"])

    def test_preset_params_are_explicit(self):
        """预设必须给出**明确的**参数：界面上批准的就是这些值，不能靠默认值兜底。"""
        st, d = self.jreq("GET", "/api/state")
        by = {p["id"]: p["params"] for p in d["presets"]}
        self.assertEqual(by["scan"]["text_layer"], "off")
        self.assertTrue(by["scan"]["conservative_fix"])
        self.assertEqual(by["modern"]["text_layer"], "verify")
        self.assertEqual(by["sample"]["limit_pages"], 20)

    def test_resolve_params_merges_override(self):
        p = ws.resolve_params("modern", {"dpi": 200, "text_layer": "off"})
        self.assertEqual(p["dpi"], 200)
        self.assertEqual(p["text_layer"], "off")
        with self.assertRaises(ValueError):
            ws.resolve_params("nope", {})


class TestPathsSafety(ServerCase):
    def test_unknown_run_404(self):
        st, d = self.jreq("GET", "/api/runs/nope/report")
        self.assertEqual(st, 404)
        self.assertIn("error", d)

    def test_path_traversal_blocked(self):
        _mk_run(self.tmp, "run-a")
        for bad in ("..%2F..%2Fetc%2Fpasswd", ".%2E/.%2E/secret", "..%5C..%5Cwin.ini"):
            st, _h, _raw = self.req("GET", "/api/runs/run-a/file?path=" + bad)
            self.assertIn(st, (400, 404), bad)
            self.assertNotEqual(st, 200, bad)

    def test_run_id_traversal_blocked(self):
        # resolve() 是第一道闸；这里验它后面确实拦得住
        self.assertIsNone(runs.resolve("../../etc", self.tmp))
        self.assertIsNone(runs.resolve("a/b/c", self.tmp))

    def test_absolute_path_rejected(self):
        _mk_run(self.tmp, "run-a")
        st, _h, _raw = self.req("GET", "/api/runs/run-a/file?path=C:/Windows/win.ini")
        self.assertIn(st, (400, 404))


class TestDecisionsApi(ServerCase):
    def test_get_groups_and_unlocated(self):
        root = _mk_run(self.tmp, "run-a", lines=2)
        # 造一条未定位（没有 A/B、没有 y0）
        doc = dec.read(paths.decisions_path(root))
        doc["decisions"].append(dec.make_decision(
            "p-009.png", None, verdict="?", level="L3",
            note="未定位行：p-009.png 里没有含「自白」的行；建议人工翻原书核对「自白」→「自日」",
            tag=7))
        dec.write(paths.decisions_path(root), doc)
        st, d = self.jreq("GET", "/api/runs/run-a/decisions")
        self.assertEqual(st, 200)
        self.assertEqual(len(d["groups"]), 1)
        self.assertEqual(d["groups"][0]["count"], 2)
        self.assertEqual(len(d["unlocated"]), 1)
        self.assertEqual(d["unlocated"][0]["suggestion"], "自白→自日")
        self.assertEqual(d["stats"]["?"], 3)      # 2 条待裁 + 1 条未定位
        it = d["groups"][0]["items"][0]
        self.assertIn("/crop?", it["crop_url"])
        self.assertTrue(it["diff"])

    def test_missing_decisions_is_not_an_error(self):
        _mk_run(self.tmp, "run-a")
        os.remove(paths.decisions_path(os.path.join(self.tmp, "run-a")))
        st, d = self.jreq("GET", "/api/runs/run-a/decisions")
        self.assertEqual(st, 200)
        self.assertTrue(d["missing"])
        self.assertEqual(d["total"], 0)

    def test_decisions_post_is_all_or_nothing(self):
        """一条非法就整份不写：裁决表是唯一不可重建的产物。"""
        root = _mk_run(self.tmp, "run-a", lines=2)
        with open(paths.decisions_path(root), "rb") as f:
            before = f.read()
        st, d = self.jreq("POST", "/api/runs/run-a/decisions", {"updates": [
            {"key": "p-001.png@100", "verdict": "B"},        # 合法
            {"key": "p-002.png@200", "verdict": "C"},        # C 缺整行 -> 非法
        ]})
        self.assertEqual(st, 400)
        self.assertIn("error", d)
        with open(paths.decisions_path(root), "rb") as f:
            self.assertEqual(f.read(), before)

    def test_unknown_key_rejected(self):
        root = _mk_run(self.tmp, "run-a", lines=1)
        st, d = self.jreq("POST", "/api/runs/run-a/decisions", {"updates": [
            {"key": "p-999.png@1", "verdict": "A"}]})
        self.assertEqual(st, 400)
        self.assertEqual(dec.read(paths.decisions_path(root))["decisions"][0]["verdict"], "?")

    def test_valid_update_resets_status(self):
        root = _mk_run(self.tmp, "run-a", lines=1)
        doc = dec.read(paths.decisions_path(root))
        doc["decisions"][0]["status"] = "applied"
        dec.write(paths.decisions_path(root), doc)
        st, d = self.jreq("POST", "/api/runs/run-a/decisions", {"updates": [
            {"key": "p-001.png@100", "verdict": "B"}]})
        self.assertEqual(st, 200)
        self.assertEqual(d["changed"], 1)
        e = dec.read(paths.decisions_path(root))["decisions"][0]
        self.assertEqual(e["verdict"], "B")
        # 判定变了，上次的 applied 就不作数了，否则 apply 会跳过它
        self.assertEqual(e["status"], "pending")

    def test_idempotent_update_is_skipped(self):
        root = _mk_run(self.tmp, "run-a", lines=1, verdict="B")
        st, d = self.jreq("POST", "/api/runs/run-a/decisions", {"updates": [
            {"key": "p-001.png@100", "verdict": "B"}]})
        self.assertEqual(st, 200)
        self.assertEqual(d["changed"], 0)
        self.assertEqual(d["skipped"], 1)

    def test_b_without_y0_rejected(self):
        """未定位条目判 B 会被 apply 判 missing，这里必须先拦下并说清楚怎么修。"""
        root = _mk_run(self.tmp, "run-a", lines=1)
        doc = dec.read(paths.decisions_path(root))
        doc["decisions"].append(dec.make_decision("p-009.png", None, verdict="?",
                                                  note="未定位行：x", tag=7))
        dec.write(paths.decisions_path(root), doc)
        key = doc["decisions"][-1]["key"]
        st, d = self.jreq("POST", "/api/runs/run-a/decisions", {"updates": [
            {"key": key, "verdict": "B"}]})
        self.assertEqual(st, 400)
        self.assertIn("y0", d["detail"])


class TestApplyGate(ServerCase):
    def test_apply_requires_confirm(self):
        _mk_run(self.tmp, "run-a")
        st, d = self.jreq("POST", "/api/runs/run-a/apply", {})
        self.assertEqual(st, 400)
        self.assertIn("confirm", d["error"])

    def test_preview_is_dry_run(self):
        root = _mk_run(self.tmp, "run-a", lines=2, verdict="B")
        pj = os.path.join(paths.ocr_dir(root, "ocr_fix"), "p-001.png.json")
        with open(pj, "rb") as f:
            before = f.read()
        st, d = self.jreq("POST", "/api/runs/run-a/apply/preview", {})
        self.assertEqual(st, 200)
        self.assertEqual(d["counts"]["apply"], 2)
        self.assertEqual(d["src"], "ocr_fix")
        with open(pj, "rb") as f:
            after = f.read()
        self.assertEqual(before, after, "干跑不许写盘")


class TestReportView(ServerCase):
    def test_report_defaults_when_missing(self):
        _mk_run(self.tmp, "run-a")
        st, d = self.jreq("GET", "/api/runs/run-a/report")
        self.assertEqual(st, 200)
        self.assertEqual(d["pages"], 0)
        self.assertEqual(d["toc"]["tree"], [])

    def test_manifest_empty_when_missing(self):
        _mk_run(self.tmp, "run-a")
        st, d = self.jreq("GET", "/api/runs/run-a/manifest")
        self.assertEqual(st, 200)
        self.assertEqual(d.get("stages"), {})

    def test_epub_404_when_absent(self):
        _mk_run(self.tmp, "run-a")
        st, d = self.jreq("GET", "/api/runs/run-a/epub")
        self.assertEqual(st, 404)


class TestMultipart(ServerCase):
    def test_upload_rejects_non_multipart(self):
        """上传必须是 multipart：用 JSON 打过去要明确拒绝，不能当成空文件写盘。"""
        st, d = self.jreq("POST", "/api/upload", {"file": "x"})
        self.assertEqual(st, 400)
        self.assertIn("multipart", d["error"])

    def test_multipart_parser(self):
        raw = (b"--B\r\nContent-Disposition: form-data; name=\"book_id\"\r\n\r\nlacan\r\n"
               b"--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.pdf\"\r\n"
               b"Content-Type: application/pdf\r\n\r\nPDFBYTES\r\n--B--\r\n")
        out = ws._parse_multipart(raw, b"B")
        self.assertEqual(out["book_id"]["text"], "lacan")
        self.assertEqual(out["file"]["filename"], "x.pdf")
        self.assertEqual(out["file"]["data"], b"PDFBYTES")


class TestSanitize(unittest.TestCase):
    def test_book_id_is_ascii_slug(self):
        self.assertEqual(ws._sanitize_id("拉康式主体"), "")
        self.assertEqual(ws._sanitize_id("Lacan Subject"), "lacan-subject")
        self.assertEqual(ws._sanitize_id("a" * 100), "a" * 40)

    def test_ascii_fallback_name(self):
        self.assertEqual(ws._ascii_name("拉康.epub"), "__.epub")
        self.assertTrue(ws._ascii_name("a b.epub").endswith(".epub"))


if __name__ == "__main__":
    unittest.main()
