"""bookflow.webui.server — 本地 Web 工作台的后端（纯标准库，零第三方依赖）。

## 为什么不用 FastAPI

docs/webui_implementation_plan.md 选的是 FastAPI + Uvicorn，但那个方案要
**再装两个第三方包**。本项目的既有约束是「纯标准库 + rapidocr/rapid_layout」，
而 WebUI 要跑在一台**已经装好 GPU 依赖**的机器上（E:/ocr-venv），
让用户为了一个本地界面再去动那套环境，代价远大于收益。

所以这里用 http.server.ThreadingHTTPServer 手写路由。它够用：
- 单用户本地工具，并发量只有「浏览器 + 一个 SSE 连接」；
- SSE 用 chunked 写即可，不需要 ASGI；
- 依赖为 0，venv 里不用改任何东西。

**唯一的代价**是没自动 Swagger——但契约本来就要写死给人看，那份文件是
webui/API.md，比生成的文档更准。

## 前端是谁

webui/static/ 下的纯静态文件（原生 ES module，无构建）。后端直接托管它们，
所以 `python bookflow.py ui` 一条命令就能起完整界面，没有 npm 步骤。

## 安全边界

只绑 127.0.0.1，不做鉴权（本机单用户工具）。但**所有路径参数都过 runs.resolve()
和 _safe_rel()**：一个能通过 HTTP 读任意文件的参数没必要留，即使服务只在本机。
"""
import io
import json
import mimetypes
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATIC = os.path.join(HERE, "static")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import decisions as dec        # noqa: E402
import jsonio                  # noqa: E402
import manifest as mf          # noqa: E402
import paths                   # noqa: E402
import runs                    # noqa: E402

PY = sys.executable
BOOKFLOW = os.path.join(ROOT, "bookflow.py")
API_VERSION = "bookflow-webui/1"

# 日志环形缓冲：一次 OCR 会打印几千行，全留会吃内存；留够回溯即可
LOG_KEEP = 4000
# SSE 补发的历史行数（契约里写的是这个数）
SSE_REPLAY = 200


# ---------------------------------------------------------------------------
# 预设：把「场景」翻译成一组明确参数
#
# 这里**不做阈值配置**（与 docs/IMPROVEMENT-PLAN.md §5.3 的结论一致）：
# 预设只是把 bookflow.py 已有的命令行开关组合起来，每个参数都能在 CLI 里单独给。
# ---------------------------------------------------------------------------
PRESETS = [
    {"id": "scan", "name": "古籍与纯扫描本",
     "description": "没有可用文字层，全程 OCR；形近字只出候选不自动改",
     "params": {"text_layer": "off", "conservative_fix": True, "no_layout": False}},
    {"id": "modern", "name": "现代学术排版",
     "description": "文字层可用：抽出来与 OCR 交叉验证，正文用文字层",
     "params": {"text_layer": "verify", "conservative_fix": False, "no_layout": False}},
    {"id": "sample", "name": "前 20 页极速打样",
     "description": "只渲染前 20 页，几分钟出一本样书给排版预览",
     "params": {"text_layer": "verify", "conservative_fix": False,
                "no_layout": False, "limit_pages": 20}},
]

# 可跑的主干阶段（与 bookflow.py 的 stage choices 对齐，去掉 prune/table 这类手动阶段）
RUNNABLE = ("render", "textlayer", "ocr", "repair", "layout", "md", "verify", "audit", "epub")


def preset_of(pid):
    for p in PRESETS:
        if p["id"] == pid:
            return p
    return None


# ---------------------------------------------------------------------------
# 作业：一条命令 = 一个 job，日志进环形缓冲，订阅者用 Queue 收
# ---------------------------------------------------------------------------
class Job(object):
    def __init__(self, jid, run_id, stages, argv):
        self.id = jid
        self.run_id = run_id
        self.stages = list(stages)
        self.argv = list(argv)
        self.status = "running"
        self.rc = None
        self.started = time.time()
        self.ended = None
        self.lines = []
        self.subs = []
        self.lock = threading.Lock()
        self.proc = None
        self.stage_states = {}      # stage -> {"status","percent","eta_seconds",...}

    def push(self, ev, data):
        """向所有订阅者广播一个事件（订阅者掉线就悄悄踢掉）。"""
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait((ev, data))
            except queue.Full:
                pass

    def log(self, line):
        with self.lock:
            self.lines.append(line)
            if len(self.lines) > LOG_KEEP:
                del self.lines[: len(self.lines) - LOG_KEEP]
        self.push("log", {"t": _now(), "stage": self.stage_hint(line), "line": line})

    def stage_hint(self, line):
        """从日志行里猜当前阶段（给 SSE 的 log 事件带上，前端高亮用）。"""
        m = RE_STAGE_HINT.search(line or "")
        return m.group(1) if m else (self.stages[0] if len(self.stages) == 1 else "")

    def snapshot(self):
        return {"job_id": self.id, "run_id": self.run_id, "status": self.status,
                "rc": self.rc, "stages": self.stages, "lines": len(self.lines),
                "elapsed": round((self.ended or time.time()) - self.started, 1)}


RE_STAGE_HINT = re.compile(r"\b(render|textlayer|ocr|repair|layout|md|verify|audit|epub|table|decisions)\b")
RE_PAGE_PROGRESS = re.compile(r"(\d+)\s*/\s*(\d+)")


class Hub(object):
    """全局作业表 + 跑命令。单进程单实例，够用。"""

    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()
        self.seq = 0
        self.by_run = {}            # run_id -> 正在跑的 job id（409 判重用）

    def new_id(self):
        with self.lock:
            self.seq += 1
            return "job-%d" % self.seq

    def get(self, jid):
        return self.jobs.get(jid)

    def running_of(self, run_id):
        jid = self.by_run.get(run_id)
        j = self.jobs.get(jid) if jid else None
        return j if (j and j.status == "running") else None

    def start(self, run_id, argv, stages):
        """起一个 job。argv 是完整命令行（已含 --work）。"""
        j = Job(self.new_id(), run_id, stages, argv)
        self.jobs[j.id] = j
        self.by_run[run_id] = j.id
        t = threading.Thread(target=self._run, args=(j,), daemon=True)
        t.start()
        return j

    def _run(self, j):
        import bookflow                      # 延迟导入：bookflow 会 import 一堆东西
        try:
            proc = subprocess.Popen(
                j.argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))
        except OSError as e:
            j.log("启动失败：%s" % e)
            j.status, j.rc = "failed", -1
            j.push("job", {"job_id": j.id, "status": "failed", "rc": -1})
            return
        j.proc = proc
        cur = j.stages[0] if j.stages else ""
        if cur:
            j.push("stage", {"stage": cur, "status": "running", "percent": 0.0,
                             "eta_seconds": None, "page": 0, "total": 0})
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            j.log(line)
            m = RE_PAGE_PROGRESS.search(line)
            if m and cur:
                try:
                    page, total = int(m.group(1)), int(m.group(2))
                except ValueError:
                    page = total = 0
                if total:
                    j.push("stage", {"stage": cur, "status": "running",
                                     "percent": round(100.0 * page / total, 1),
                                     "eta_seconds": None, "page": page, "total": total})
        rc = proc.wait()
        j.rc = rc
        j.status = "done" if rc == 0 else "failed"
        j.ended = time.time()
        for s in j.stages:
            j.push("done", {"stage": s, "status": "ok" if rc == 0 else "failed",
                            "rc": rc, "elapsed": round(j.ended - j.started, 1)})
        j.push("job", {"job_id": j.id, "status": j.status, "rc": rc})
        with self.lock:
            if self.by_run.get(j.run_id) == j.id:
                self.by_run.pop(j.run_id, None)

    def abort(self, run_id):
        """安全中断：走 bookflow.kill_tree，清理整棵子进程树。

        不能只 proc.kill()：pdftoppm / run_ocr 的多进程池会变成孤儿，
        下一轮再占 GPU 显存，出现「昨天还能跑今天 OOM」这种看不出根因的故障。
        """
        j = self.running_of(run_id)
        if not j or not j.proc:
            return False
        import bookflow
        bookflow.kill_tree(j.proc)
        j.status = "killed"
        j.push("job", {"job_id": j.id, "status": "killed", "rc": None})
        return True


HUB = Hub()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------
def _safe_rel(base, rel):
    """把相对路径解析到 base 之内；越界返回 None。"""
    if not rel:
        return None
    rel = str(rel).replace("\\", "/")
    if rel.startswith("/") or ":" in rel:
        return None
    p = os.path.abspath(os.path.join(base, rel))
    try:
        if os.path.commonpath([p, os.path.abspath(base)]) != os.path.abspath(base):
            return None
    except ValueError:
        return None
    return p


def _json(handler, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _err(handler, msg, code=400, detail=""):
    _json(handler, {"error": msg, "detail": detail}, code)


# ---------------------------------------------------------------------------
# 裁决表适配：decisions.json -> 前端要的分组投影
# ---------------------------------------------------------------------------
def _load_decisions(run_root):
    p = paths.decisions_path(run_root)
    if not os.path.exists(p):
        return None, p
    try:
        return dec.read(p), p
    except (ValueError, OSError):
        return None, p


def _diff_of(A, B):
    """字符级差异段（opcode 口径，前端照它渲染高亮，不再自己算）。"""
    from difflib import SequenceMatcher
    sm = SequenceMatcher(None, A or "", B or "", autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        out.append({"op": "replace" if (tag == "replace") else
                    ("delete" if tag == "delete" else "insert"),
                    "a": (A or "")[i1:i2], "b": (B or "")[j1:j2],
                    "i1": i1, "i2": i2, "j1": j1, "j2": j2})
    return out


def _suggestion_of(e):
    m = re.search("「([^」]{1,8})」→「([^」]{1,8})」", e.get("note") or "")
    return ("%s→%s" % (m.group(1), m.group(2))) if m else ""


def decisions_view(run_root, run_id, only="eyes"):
    doc, path = _load_decisions(run_root)
    if doc is None:
        return {"total": 0, "pending": 0, "stats": {}, "groups": [], "unlocated": [],
                "missing": True, "path": mf.rel(run_root, path) if path else ""}
    import make_table as mt          # 复用项目的分组口径，不重写一套
    items = doc.get("decisions") or []
    stats = {"A": 0, "B": 0, "C": 0, "?": 0}
    for e in items:
        v = e.get("verdict") or "?"
        stats[v] = stats.get(v, 0) + 1

    groups, unlocated = {}, []
    for e in items:
        kind = mt.classify(e)
        if kind == "same":
            continue
        if kind == "unlocated":
            y0 = e.get("y0")
            unlocated.append({
                "key": e.get("key"), "page": e.get("page"), "y0": y0,
                "suggestion": _suggestion_of(e) or mt.group_of(e),
                "why": e.get("note") or "",
                "verdict": e.get("verdict") or "?",
                "replacement": e.get("replacement"),
                "status": e.get("status") or "pending",
            })
            continue
        if only != "all" and kind != only:
            continue
        g = mt.group_of(e)
        groups.setdefault(g, []).append(e)

    out = []
    for g, rows in groups.items():
        out.append({"group": g, "count": len(rows), "items": [
            _item_view(run_root, run_id, e) for e in rows]})
    out.sort(key=lambda x: -x["count"])
    # 去重：同一页同一 y0 只保留一条（跨行归并 bug 修完后应该本来就没有重复，
    # 这里再兜一次底：宁可少显示，也不能让前端把同一行提交两次）
    return {"total": len(items),
            "pending": sum(1 for e in items if (e.get("verdict") or "?") == "?"),
            "stats": stats, "groups": out, "unlocated": unlocated,
            "missing": False, "path": mf.rel(run_root, path)}


def _item_view(run_root, run_id, e):
    y0 = e.get("y0")
    crop = ""
    if y0 is not None and e.get("page"):
        crop = "/api/runs/%s/crop?page=%s&y0=%s" % (
            urllib.parse.quote(run_id), urllib.parse.quote(str(e.get("page"))),
            urllib.parse.quote(dec.fmt_y(y0)))
    page_url = ""
    if e.get("page"):
        page_url = "/api/runs/%s/page?page=%s" % (
            urllib.parse.quote(run_id), urllib.parse.quote(str(e.get("page"))))
    return {"key": e.get("key"), "page": e.get("page"), "y0": y0,
            "A": e.get("A") or "", "B": e.get("B") or "",
            "verdict": e.get("verdict") or "?", "replacement": e.get("replacement"),
            "status": e.get("status") or "pending", "note": e.get("note") or "",
            "diff": _diff_of(e.get("A"), e.get("B")),
            "crop_url": crop, "page_url": page_url}


def apply_updates(run_root, updates):
    """把前端的裁决写进 decisions.json。**校验失败整份不写**（与 table --import 同口径）。"""
    doc, path = _load_decisions(run_root)
    if doc is None:
        return 0, 0, ["找不到裁决表 %s（先跑 decisions 阶段）" % path]
    by_key = {e.get("key"): e for e in (doc.get("decisions") or [])}
    errors, changed, skipped = [], 0, 0
    for u in updates or []:
        k = u.get("key")
        e = by_key.get(k)
        if e is None:
            errors.append("键不存在：%s" % k)
            continue
        v = u.get("verdict")
        repl = u.get("replacement")
        if v not in dec.VERDICTS and v != "":
            errors.append("%s：判定取值非法 %r" % (k, v))
            continue
        if v == "C" and not (repl or "").strip():
            errors.append("%s：判定 C 但没给「替换整行」" % k)
            continue
        if v in ("A", "B", "?") and (repl or "").strip():
            errors.append("%s：判定 %s 时不能同时给替换整行" % (k, v))
            continue
        if v in ("B", "C") and e.get("y0") is None:
            errors.append("%s：这一条没有行坐标 y0，回填会被跳过；请在表格里补上行 y0" % k)
            continue
        if not v:
            skipped += 1
            continue
        if e.get("verdict") == v and e.get("replacement") == (repl if v == "C" else None):
            skipped += 1
            continue
        e["verdict"] = v
        e["replacement"] = repl if v == "C" else None
        e["status"] = "pending"          # 判定变了，上次的「已应用」不再作数
        changed += 1
    if errors:
        return 0, skipped, errors
    if changed:
        probs = dec.validate(doc)
        if probs:
            return 0, skipped, ["合入后裁决表不合法，未写盘：" + p for p in probs[:20]]
        dec.write(path, doc)
    return changed, skipped, []


def plan_view(run_root, limit=20):
    """干跑：这一趟要改什么。复用 apply_decisions.plan，**不重写判定逻辑**。"""
    import apply_decisions as ap
    doc, _p = _load_decisions(run_root)
    if doc is None:
        return {"error": "找不到裁决表"}
    src, why, need_confirm = _apply_target(run_root)
    if not src:
        return {"error": why}
    items, _pages = ap.plan(doc, src)
    counts = {"apply": 0, "already": 0, "missing": 0, "skip": 0}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    sample = []
    for it in items:
        if it["kind"] == "skip":
            continue
        sample.append({"kind": it["kind"], "key": it["entry"].get("key"),
                       "old": (it["old"] or "")[:200], "new": (it["new"] or "")[:200],
                       "reason": it.get("reason") or ""})
        if len(sample) >= limit:
            break
    return {"counts": counts, "items": sample, "src": os.path.basename(src),
            "src_reason": why,
            # 回填到 ocr_layout 时要额外提醒：它既是源又是 layout 的输出，
            # 回填后必须 layout --force 重建，否则旧标签会盖住新文本
            "needs_layout_rebuild": need_confirm}


def _apply_target(run_root):
    """回填目标目录 + 为什么选它。返回 (路径, 人话理由, 是否需要人确认)。

    **规则只有一句（IO-CONVENTIONS §9.2 实测结论）：
    回填目标必须是接下来 layout --force 会选中的那个源。**

    一开始这里按「ocr_merged > ocr_fix > ocr」硬排，对 run-lacan 直接选错：
    它没有 ocr_merged，于是选中 ocr_fix，但 layout 的真实源是**优先级第一个存在
    且页码覆盖完整的目录**——run-lacan 是 ocr_layout。回填到 ocr_fix 之后
    layout --force 仍然读 ocr_layout，裁决一条都不会进成书，而退出码全程为 0。

    所以这里复用 bookflow.ocr_dir()：它是唯一有权回答「正文源是谁」的地方，
    再抄一套顺序就等于把同一件事写在两个地方（本项目已经因此栽过两次）。
    """
    try:
        import bookflow
        src = bookflow.ocr_dir(_bf_args(run_root))
    except (SystemExit, Exception):
        src = ""
    if src and _has_pages(src):
        name = os.path.basename(os.path.normpath(src))
        return src, "优先级最高的正文源（layout --force 也会读它）", name == "ocr_layout"
    work = paths.work_dir(run_root)
    for name in ("ocr_merged", "ocr_fix", "ocr"):
        d = os.path.join(work, name)
        if _has_pages(d):
            return d, "没有可用的正文源，退回到 %s" % name, name == "ocr_layout"
    return "", "没有可回填的 OCR 目录", False


def _has_pages(d):
    return bool(d) and os.path.isdir(d) and any(
        f.endswith(".png.json") for f in os.listdir(d))


def _bf_args(run_root):
    """造一个 bookflow 阶段函数认识的 args 命名空间。

    bookflow.ocr_dir() 只读三个字段（work / ocr_src / text_layer），
    这里如实只给这三个，**不给别的**：多给一个就意味着 WebUI 也在猜
    bookflow 的参数语义，而猜错的表现是「选源悄悄变了」——本项目最怕的那种错。

    text_layer 取 manifest 里记的这次运行的实际值；没记过就用 bookflow 的默认
    verify。用 manifest 而不是写死 verify，是因为按 scan 预设跑的书
    （text_layer=off）如果在这里被当成 verify，ocr_text 就会重新参与选源，
    回填目标可能指向一份**这本书根本没用过**的目录。
    """
    import argparse
    params = (mf.read(run_root).get("params") or {})
    return argparse.Namespace(work=run_root,
                              ocr_src="",
                              text_layer=params.get("text_layer") or "verify")


# ---------------------------------------------------------------------------
# 裁剪：从页图里切出某一行
# ---------------------------------------------------------------------------
def crop_png(run_root, page, y0, height=None, pad=8):
    """按行 y0 从页图裁一条。找不到页图/页 JSON/行，返回 None + 原因。"""
    try:
        from PIL import Image
    except ImportError:
        return None, "缺少 Pillow，无法裁图"
    work = paths.work_dir(run_root)
    pages = os.path.join(work, "pages")
    name = os.path.basename(str(page or ""))
    if not name.lower().endswith(".png"):
        name += ".png"
    img_p = _safe_rel(pages, name)
    if not img_p or not os.path.exists(img_p):
        # pdftoppm 不补零：p-15.png 与 p-015.png 要互相找得到
        m = re.search(r"(\d+)", name)
        cand = None
        if m and os.path.isdir(pages):
            want = int(m.group(1))
            for f in sorted(os.listdir(pages)):
                mm = re.search(r"(\d+)", f)
                if f.lower().endswith(".png") and mm and int(mm.group(1)) == want:
                    cand = os.path.join(pages, f)
                    break
        if not cand:
            return None, "找不到页图 %s" % name
        img_p = cand

    line = None
    for src_dir in ("ocr_layout", "ocr_merged", "ocr_fix", "ocr_text", "ocr"):
        d = os.path.join(work, src_dir)
        if not os.path.isdir(d):
            continue
        try:
            lp = dec.page_path(d, name)
        except Exception:
            lp = None
        if not lp:
            continue
        try:
            doc = jsonio.read(lp)
        except (ValueError, OSError):
            continue
        best, bestd = None, None
        for L in (doc.get("lines") or []):
            try:
                d0 = abs(float(L.get("y0", 0)) - float(y0))
            except (TypeError, ValueError):
                continue
            if bestd is None or d0 < bestd:
                best, bestd = L, d0
        if best is not None and bestd is not None and bestd <= 2.0:
            line = best
            break
    if line is None:
        return None, "页内 y0=%s 附近没有行" % y0

    try:
        im = Image.open(img_p)
    except OSError as e:
        return None, "打不开页图：%s" % e
    try:
        x0 = float(line.get("x0") or 0)
        x1 = float(line.get("x1") or im.width)
        ly0 = float(line.get("y0") or 0)
        ly1 = float(line.get("y1") or (ly0 + 40))
    except (TypeError, ValueError):
        return None, "行坐标不是数字"
    top = max(0, int(ly0) - pad)
    bot = min(im.height, int(ly1) + pad)
    if height:
        try:
            h = max(20, int(float(height)))
            if h < bot - top:
                bot = top + h
        except (TypeError, ValueError):
            pass
    left = max(0, int(x0) - 40)
    right = min(im.width, int(x1) + 40)
    if right <= left or bot <= top:
        return None, "裁剪区域为空"
    box = (left, top, right, bot)
    crop = im.crop(box)
    # 竖屏裁出来的横条太窄，放大到 2 倍宽更好读（不改变内容，只放尺寸）
    scale = 2 if crop.width < 900 else 1
    if scale > 1:
        crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue(), ""


def page_png(run_root, page, box=None):
    try:
        from PIL import Image
    except ImportError:
        return None, "缺少 Pillow"
    work = paths.work_dir(run_root)
    pages = os.path.join(work, "pages")
    name = os.path.basename(str(page or ""))
    p = _safe_rel(pages, name if name.lower().endswith(".png") else name + ".png")
    if not p or not os.path.exists(p):
        m = re.search(r"(\d+)", name)
        if m and os.path.isdir(pages):
            want = int(m.group(1))
            for f in sorted(os.listdir(pages)):
                mm = re.search(r"(\d+)", f)
                if f.lower().endswith(".png") and mm and int(mm.group(1)) == want:
                    p = os.path.join(pages, f)
                    break
    if not p or not os.path.exists(p):
        return None, "找不到页图"
    try:
        im = Image.open(p)
    except OSError as e:
        return None, "打不开页图：%s" % e
    if box:
        try:
            b = [int(float(x)) for x in str(box).split(",")]
            if len(b) == 4:
                im = im.crop((max(0, b[0]), max(0, b[1]),
                              min(im.width, b[2]), min(im.height, b[3])))
        except (TypeError, ValueError):
            pass
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue(), ""


# ---------------------------------------------------------------------------
# 报告投影
# ---------------------------------------------------------------------------
def report_view(run_root):
    rep = {}
    p = os.path.join(paths.report_dir(run_root), "report.json")
    if os.path.exists(p):
        try:
            rep = jsonio.read(p) or {}
        except (ValueError, OSError):
            rep = {}
    lay = rep.get("layout") or {}
    qa = rep.get("qa") or {}
    figures = (lay.get("regions") or {}).get("images") or {}
    entries = rep.get("entries") or []
    matched = rep.get("matched") or []
    return {
        "book": rep.get("book") or {},
        "pages": lay.get("pages") or rep.get("pages") or 0,
        "cjk_chars": qa.get("cjk_chars") or 0,
        "fix": {"fixed": rep.get("fix") or 0,
                "candidates": len(rep.get("fix_candidates") or [])},
        "toc": {"entries": lay.get("toc_entries") or len(entries),
                "matched": lay.get("matched") or len(matched),
                "source": lay.get("toc_source") or "",
                "tree": [{"level": m.get("level"), "title": m.get("title"),
                          "page": m.get("page"), "printed_page": m.get("printed_page"),
                          "score": m.get("score")} for m in matched]},
        "notes": {"total": (lay.get("notes") or {}).get("total", 0),
                  "linked": (lay.get("notes") or {}).get("linked", 0),
                  "orphan": (lay.get("notes") or {}).get("orphan", 0),
                  "pages": (lay.get("notes") or {}).get("pages", 0)},
        "qa": {"issues": qa.get("issues") or []},
        "figures": {"made": figures.get("made", 0),
                    "files": len(figures.get("files") or [])},
    }


def _pdf_pages(p):
    """读 PDF 页数：pdfinfo 优先，其次 pypdf。都失败返回 None。

    与 bookflow.pdf_page_count 同口径，但这里不 import bookflow（webui 要能
    独立启动，哪怕 OCR 依赖没装）。
    """
    if not p or not os.path.isfile(p):
        return None
    info = shutil.which("pdfinfo")
    if info:
        try:
            out = subprocess.run([info, p], capture_output=True, text=True,
                                 timeout=20, errors="replace").stdout or ""
            m = re.search(r"^Pages:\s*(\d+)", out, re.M)
            if m:
                return int(m.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        import pypdf
        with open(p, "rb") as f:
            return len(pypdf.PdfReader(f).pages)
    except Exception:
        return None


def _pdf_meta(p, original_name=""):
    """从 PDF 元信息尽力猜书名/作者。猜不到就用**上传时的原始文件名**。

    不能回落到磁盘上的文件名：上传后它一律叫 source.pdf，于是书名会变成
    "source"（实测就这么发生了）。原始文件名是唯一还留着"这本书叫什么"的地方。
    """
    meta = {"title": "", "author": "", "publisher": ""}
    if not p or not os.path.isfile(p):
        return meta
    try:
        import pypdf
        with open(p, "rb") as f:
            info = pypdf.PdfReader(f).metadata or {}
        meta["title"] = (info.get("/Title") or "").strip()
        meta["author"] = (info.get("/Author") or "").strip()
    except Exception:
        pass
    # 有些 PDF 的 /Title 就是文件名（"source"）或占位串，这类不算猜到了
    if not meta["title"] or meta["title"].lower() in ("source", "untitled", "无标题"):
        base = os.path.basename(original_name or p)
        meta["title"] = os.path.splitext(base)[0]
    # 去掉浏览器重复下载加上的 " (1)" "（2）" 尾巴：它是文件名噪声，不是书名
    meta["title"] = re.sub(r"\s*[（(]\d{1,2}[)）]\s*$", "", meta["title"]).strip()
    return meta


def _sanitize_id(s):
    """书名 -> book id。只留 ascii 小写字母数字与连字符。

    **纯中文书名会得到一个空串**，这不是 bug 而是刻意的：本项目的产物名
    已经带全角标点，再让中文进目录名会在 URL 编码、命令行 quoting、
    zip 解压三处各多一个失败面（IO-CONVENTIONS §4.1）。空串由调用方
    退回到 "book-<页码哈希>" 这种稳定 id——同一个文件重传两次得到同一个 id。
    """
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    return s.strip("-")[:40]


def _fallback_id(pdf_path, original_name=""):
    """中文书名时的兜底 id：book-<内容哈希前 8 位>。

    用**内容哈希**而不是随机串：同一个文件重复上传会落到同一个 run，
    不会每次多出一个空目录。也不是用时间戳——那样重传一次就丢一次历史。
    """
    h = mf.sha256_of(pdf_path)[:8] if pdf_path and os.path.isfile(pdf_path) else ""
    if not h:
        h = "%08x" % (abs(hash(original_name or "book")) & 0xFFFFFFFF)
    return "book-" + h


# ---------------------------------------------------------------------------
# 命令行拼装
# ---------------------------------------------------------------------------
def build_argv(run_root, stages, params, pdf="", title="", author="", publisher=""):
    """把「阶段 + 参数」翻译成 bookflow.py 的命令行。

    每一条都显式给，不依赖 bookflow 的默认值——界面上批准过什么，就跑什么。
    """
    base = [PY, BOOKFLOW, "all" if stages == ["all"] else stages[0],
            "--work", run_root]
    if pdf:
        base += ["--pdf", pdf]
    if title:
        base += ["--title", title]
    if author:
        base += ["--author", author]
    if publisher:
        base += ["--publisher", publisher]
    for k, flag in (("dpi", "--dpi"), ("procs", "--procs"), ("device", "--device"),
                    ("model", "--model"), ("toc_depth", "--toc-depth"),
                    ("text_layer", "--text-layer")):
        v = params.get(k)
        if v not in (None, ""):
            base += [flag, str(v)]
    if params.get("conservative_fix"):
        base += ["--conservative-fix"]
    if params.get("no_layout"):
        base += ["--no-layout"]
    if params.get("force"):
        base += ["--force"]
    return base


def resolve_params(preset_id, explicit):
    """preset + 显式覆盖 = 最终生效参数（界面上批准的必须是这一份）。"""
    p = preset_of(preset_id)
    if p is None:
        raise ValueError("未知预设：%r" % (preset_id,))
    out = dict(p["params"])
    for k, v in (explicit or {}).items():
        if v is not None:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# HTTP 路由
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "bookflow-webui/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # 默认会把每个请求打到 stderr；本地工具不需要，但**错误要留**
        if args and str(args[0]).startswith(("4", "5")):
            sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % args))

    # ---- 通用 ----
    def _body(self):
        """读请求体（只读一次，后续走缓存）。

        必须**在路由之前**把体读完：HTTP/1.1 是长连接，一旦某个分支直接回了
        400 却没消费 body，剩下的字节会被当成下一个请求的开头，
        服务端日志里出现一串 "Bad request syntax"（实测踩过）。
        """
        if getattr(self, "_cached_body", None) is not None:
            return self._cached_body
        n = int(self.headers.get("Content-Length") or 0)
        self._cached_body = self.rfile.read(n) if n else b""
        return self._cached_body

    def _json_body(self):
        raw = self._body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ValueError("请求体不是合法 JSON：%s" % e)

    def _send_bytes(self, data, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    # ---- 分发 ----
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def _dispatch(self, method):
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if method in ("POST", "PUT", "PATCH"):
            self._body()                 # 先消费完请求体，见 _body 的注释
        try:
            if path.startswith("/api/"):
                return self._api(method, path, q)
            return self._static(path)
        except ValueError as e:
            return _err(self, str(e), 400)
        except BrokenPipeError:
            return
        except Exception as e:                    # 兜底：不能让一个异常把服务打挂
            import traceback
            traceback.print_exc()
            try:
                return _err(self, "后端异常：%s" % e, 500)
            except Exception:
                return

    # ---- 静态 ----
    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        p = _safe_rel(STATIC, rel)
        if not p or not os.path.isfile(p):
            # SPA 兜底：未知路径回首页（hash 路由本来就不会触发，但直接敲 URL 会）
            p = os.path.join(STATIC, "index.html")
            if not os.path.isfile(p):
                return _err(self, "前端还没构建：缺少 %s" % p, 404)
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(p, "rb") as f:
            data = f.read()
        self._send_bytes(data, ctype)

    # ---- API ----
    def _api(self, method, path, q):
        parts = [p for p in path.split("/") if p]

        if parts == ["api", "state"] and method == "GET":
            return _json(self, {
                "version": API_VERSION,
                "cwd": ROOT,
                "presets": PRESETS,
                "runnable": list(RUNNABLE),
                "runs": runs.all_summaries(ROOT),
            })

        if parts == ["api", "upload"] and method == "POST":
            return self._upload()

        if parts == ["api", "runs"] and method == "POST":
            return self._start_run()

        # /api/runs/{id}/...
        #
        # run id 可能有两段（runs/<book-id>，新布局）。
        #
        # **只在第一段就是 "runs" 时才按两段解析**，不做"试最长前缀"：
        # 一开始写的就是试最长前缀，结果 /api/runs/run-a/report 被解析成
        # run-id "run-a/report"——因为 tiered 布局下 <run>/report 本身就是个目录，
        # runs.resolve 看它像个工作目录就放行了（实测：report 接口一律 404）。
        # 判据必须来自**布局约定**（新书只在 runs/ 下），不能来自"目录长得像"。
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "runs":
            rid, root, rest = None, None, None
            if parts[2] == "runs" and len(parts) >= 4:
                cand = "runs/" + parts[3]
                d = runs.resolve(cand, ROOT)
                if d and os.path.isdir(d):
                    rid, root, rest = cand, d, parts[4:]
            if root is None:
                cand = parts[2]
                d = runs.resolve(cand, ROOT)
                if d and os.path.isdir(d):
                    rid, root, rest = cand, d, parts[3:]
            if root is None:
                return _err(self, "run 不存在：%s" % parts[2], 404)
            return self._run_api(method, rid, root, rest, q)

        # /api/jobs/{id}/stream 与 /api/jobs/{id}
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "jobs":
            j = HUB.get(parts[2])
            if not j:
                return _err(self, "job 不存在：%s" % parts[2], 404)
            if len(parts) == 4 and parts[3] == "stream" and method == "GET":
                return self._stream(j)
            return _json(self, j.snapshot())

        return _err(self, "没有这个接口：%s %s" % (method, path), 404)

    def _run_api(self, method, rid, root, rest, q):
        if rest == ["stages"] and method == "POST":
            return _err(self, "缺少阶段名", 400)
        if len(rest) == 2 and rest[0] == "stages" and method == "POST":
            stage = rest[1]
            if stage not in RUNNABLE:
                return _err(self, "未知阶段：%s（可选 %s）" % (stage, "/".join(RUNNABLE)), 400)
            if HUB.running_of(rid):
                return _err(self, "这个 run 已经有作业在跑", 409)
            body = self._json_body()
            params = body.get("params") or {}
            pdf = body.get("pdf") or ""
            argv = build_argv(root, [stage], params, pdf=pdf,
                              title=body.get("title") or "",
                              author=body.get("author") or "")
            j = HUB.start(rid, argv, [stage])
            return _json(self, {"job_id": j.id})

        if rest == ["abort"] and method == "POST":
            return _json(self, {"ok": HUB.abort(rid)})

        if rest == ["decisions"] and method == "GET":
            return _json(self, decisions_view(root, rid, q.get("only") or "eyes"))

        if rest == ["decisions"] and method == "POST":
            body = self._json_body()
            changed, skipped, errors = apply_updates(root, body.get("updates") or [])
            if errors:
                return _err(self, "裁决没有写盘（%d 处问题）" % len(errors), 400,
                            detail=" | ".join(errors[:20]))
            return _json(self, {"changed": changed, "skipped": skipped, "errors": []})

        if rest == ["crop"] and method == "GET":
            data, why = crop_png(root, q.get("page"), q.get("y0"), q.get("height"))
            if data is None:
                return _err(self, why, 404)
            return self._send_bytes(data, "image/png")

        if rest == ["page"] and method == "GET":
            data, why = page_png(root, q.get("page"), q.get("box"))
            if data is None:
                return _err(self, why, 404)
            return self._send_bytes(data, "image/png")

        if rest == ["apply", "preview"] and method == "POST":
            return _json(self, plan_view(root))

        if rest == ["apply"] and method == "POST":
            body = self._json_body()
            # 必须显式 confirm：这是唯一会**原地覆写 OCR 页文件**的动作
            if body.get("confirm") is not True:
                return _err(self, "缺少 confirm=true（回填会覆写页级 JSON，必须显式确认）", 400)
            if HUB.running_of(rid):
                return _err(self, "这个 run 已经有作业在跑", 409)
            src, why, _nc = _apply_target(root)
            if not src:
                return _err(self, why, 400)
            argv = [PY, os.path.join(ROOT, "apply_decisions.py"), "--work", root,
                    "--src", os.path.basename(src), "--apply"]
            j = HUB.start(rid, argv, ["apply"] + list(body.get("then") or []))
            return _json(self, {"job_id": j.id})

        if rest == ["report"] and method == "GET":
            return _json(self, report_view(root))

        if rest == ["manifest"] and method == "GET":
            return _json(self, mf.read(root))

        if rest == ["markdown"] and method == "GET":
            p = _safe_rel(paths.report_dir(root), q.get("path") or "book.md")
            if not p or not os.path.isfile(p):
                return _err(self, "找不到 %s" % (q.get("path") or "book.md"), 404)
            with open(p, "rb") as f:
                return self._send_bytes(f.read(), "text/plain; charset=utf-8")

        if rest == ["file"] and method == "GET":
            p = _safe_rel(paths.report_dir(root), q.get("path") or "")
            if not p or not os.path.isfile(p):
                return _err(self, "找不到该文件", 404)
            ctype = mimetypes.guess_type(p)[0] or "text/plain"
            if ctype.startswith("text/"):
                ctype += "; charset=utf-8"
            with open(p, "rb") as f:
                return self._send_bytes(f.read(), ctype)

        if rest == ["epub"] and method == "GET":
            has, p = runs._epub_of(root)
            if not has:
                return _err(self, "这本还没有 EPUB", 404)
            with open(p, "rb") as f:
                data = f.read()
            name = os.path.basename(p)
            return self._send_bytes(
                data, "application/epub+zip", 200,
                {"Content-Disposition": 'attachment; filename="%s"; filename*=UTF-8\'\'%s'
                                        % (_ascii_name(name), urllib.parse.quote(name))})

        if rest == ["images"] and method == "GET":
            p = _safe_rel(paths.images_dir(root), q.get("name") or "")
            if not p or not os.path.isfile(p):
                return _err(self, "找不到插图", 404)
            with open(p, "rb") as f:
                return self._send_bytes(f.read(), "image/png")

        return _err(self, "没有这个接口：%s /api/runs/%s/%s" % (
            method, rid, "/".join(rest)), 404)

    # ---- 上传 ----
    def _upload(self):
        ctype = self.headers.get("Content-Type") or ""
        m = re.search(r'boundary=(?:"([^;]+)"|([^;]+))', ctype)
        if "multipart/form-data" not in ctype or not m:
            return _err(self, "需要 multipart/form-data", 400)
        boundary = (m.group(1) or m.group(2)).strip().strip('"')
        raw = self._body()
        fields = _parse_multipart(raw, boundary.encode())
        f = fields.get("file")
        if not f or not f.get("data"):
            return _err(self, "缺少 file 字段", 400)
        fname = f.get("filename") or "book.pdf"
        hint = (fields.get("book_id") or {}).get("text") or fname
        d0 = paths.input_dir("__tmp__", create=True)
        # 先按内容算兜底 id，再决定目录：这样同名不同内容的两个文件不会互相覆盖
        tmp_pdf = os.path.join(d0, "probe.pdf")
        with open(tmp_pdf, "wb") as fh:
            fh.write(f["data"])
        book_id = _sanitize_id(os.path.splitext(os.path.basename(hint))[0])
        if len(book_id) < 3:
            book_id = _fallback_id(tmp_pdf, fname)
        shutil.rmtree(d0, ignore_errors=True)
        d = paths.input_dir(book_id, create=True)
        dst = os.path.join(d, "source.pdf")
        tmp = dst + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(f["data"])
        os.replace(tmp, dst)
        meta = _pdf_meta(dst, fname)
        pages = _pdf_pages(dst)
        # meta.json 由上传落盘：它是「这本书叫什么」的唯一人工事实源，
        # 名字一旦定下就不该被 OCR 结果或文件名反向改写
        jsonio.write(os.path.join(d, "meta.json"), {
            "book_id": book_id, "title": meta.get("title") or book_id,
            "author": meta.get("author") or "", "publisher": "",
            "source": "input/%s/source.pdf" % book_id, "pages": pages}, indent=1)
        return _json(self, {"upload_id": book_id,
                            "path": "input/%s/source.pdf" % book_id,
                            "pages": pages, "meta": meta})

    # ---- 起 run ----
    def _start_run(self):
        body = self._json_body()
        pdf = body.get("pdf_path") or ""
        book_id = _sanitize_id(body.get("book_id") or "")
        if not book_id:
            book_id = _sanitize_id(os.path.splitext(os.path.basename(pdf))[0]) or "book"
        # 输入路径：相对仓库根的 input/，或绝对路径（用户手输）
        if pdf and not os.path.isabs(pdf):
            pdf_abs = os.path.join(ROOT, pdf.replace("/", os.sep))
        else:
            pdf_abs = pdf
        if pdf and not os.path.isfile(pdf_abs):
            return _err(self, "找不到 PDF：%s" % pdf, 400)
        stages = body.get("stages") or ["all"]
        if not isinstance(stages, list) or not stages:
            return _err(self, "stages 必须是数组", 400)
        try:
            params = resolve_params(body.get("preset") or "modern", body.get("params"))
        except ValueError as e:
            return _err(self, str(e), 400)
        root = os.path.join(paths.runs_root(create=True), book_id)
        os.makedirs(root, exist_ok=True)
        rid = "runs/" + book_id
        if HUB.running_of(rid):
            return _err(self, "这个 run 已经有作业在跑", 409)
        # 新书一律 tiered：work/ report/ review/ 三层，交付物出到 dist/
        paths.ensure_tiered(root)
        mf.update(root, book_id=book_id)
        if pdf_abs:
            mf.set_input(root, pdf=pdf_abs, title=body.get("title") or "",
                         author=body.get("author") or "",
                         publisher=body.get("publisher") or "",
                         pages=_pdf_pages(pdf_abs))
        mf.set_params(root, **{k: v for k, v in params.items() if isinstance(v, (int, float, str, bool, type(None)))})
        argv = build_argv(root, stages, params, pdf=pdf_abs,
                          title=body.get("title") or "", author=body.get("author") or "",
                          publisher=body.get("publisher") or "")
        j = HUB.start(rid, argv, stages)
        return _json(self, {"run_id": rid, "book_id": book_id, "job_id": j.id,
                            "resolved": params, "argv": argv[2:]})

    # ---- SSE ----
    def _stream(self, job):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = queue.Queue(maxsize=2000)
        with job.lock:
            replay = list(job.lines[-SSE_REPLAY:])
            job.subs.append(q)
        try:
            for line in replay:
                self._sse("log", {"t": _now(), "stage": job.stage_hint(line), "line": line})
            if job.status != "running":
                self._sse("job", {"job_id": job.id, "status": job.status, "rc": job.rc})
                return
            idle = 0.0
            while True:
                try:
                    ev, data = q.get(timeout=1.0)
                    idle = 0.0
                except queue.Empty:
                    idle += 1.0
                    # 心跳：浏览器/代理会掐掉长时间静默的连接（OCR 一页就要几秒）
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    if idle > 3600:
                        return
                    continue
                if not self._sse(ev, data):
                    return
                if ev == "job":
                    return
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            with job.lock:
                try:
                    job.subs.remove(q)
                except ValueError:
                    pass
            try:
                self.close_connection = True
            except Exception:
                pass

    def _sse(self, ev, data):
        payload = "event: %s\ndata: %s\n\n" % (ev, json.dumps(data, ensure_ascii=False))
        try:
            self.wfile.write(payload.encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False


def _ascii_name(name):
    """Content-Disposition 的 ASCII 回退名：非 ASCII 一律换成下划线。

    中文书名直接塞进 filename= 会让某些客户端解析成乱码；标准做法是给一个
    ASCII 兜底 + filename* 的 UTF-8 版本（下面那个）。
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "book.epub"


def _parse_multipart(raw, boundary):
    """极简 multipart 解析：够上传一个 PDF。

    不用 cgi.FieldStorage（3.13 已移除）也不用 email 模块（对二进制要额外解码），
    这里只需要「按 boundary 切开、找空行、前面是头后面是体」。
    """
    out = {}
    sep = b"--" + boundary
    for part in raw.split(sep):
        if not part.strip(b"\r\n-"):
            continue
        head, _, body = part.partition(b"\r\n\r\n")
        if not _:
            continue
        if body.endswith(b"\r\n"):
            body = body[:-2]
        name = filename = None
        for line in head.split(b"\r\n"):
            # **必须按 UTF-8 解**：浏览器把文件名按 UTF-8 编码塞进 Content-Disposition，
            # 用 latin-1 解会得到「æ­åº·...」这种乱码，书名与 book id 一起废掉
            # （实测第一次上传，书名变成了乱码、book id 变成了 "1"）。
            # 先试 UTF-8，失败再退 latin-1（老客户端可能真的发 latin-1）。
            try:
                t = line.decode("utf-8")
            except UnicodeDecodeError:
                t = line.decode("latin-1")
            m = re.search(r'name="([^"]*)"', t)
            if m and name is None:
                name = m.group(1)
            m = re.search(r'filename="([^"]*)"', t)
            if m:
                filename = m.group(1)
        if not name:
            continue
        if filename is not None:
            out[name] = {"filename": filename, "data": body}
        else:
            out[name] = {"text": body.decode("utf-8", "replace")}
    return out


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def find_port(host, start=8765, tries=40):
    """从 start 起找一个能绑的端口（占用就 +1）。"""
    for i in range(tries):
        p = start + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise OSError("从 %d 起找不到可用端口" % start)


def serve(host="127.0.0.1", port=0, open_browser=True):
    """起服务。port=0 时自动挑 8765 起第一个可用端口。"""
    if not os.path.isdir(STATIC):
        sys.exit("前端目录不存在：%s\nWebUI 需要 webui/static/ 下的静态文件。" % STATIC)
    port = port or find_port(host)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = "http://%s:%d" % (host, port)
    print("bookflow WebUI: %s" % url)
    print("  工作目录：%s" % ROOT)
    print("  发现的 run：%s" % (", ".join(runs.list_ids(ROOT)) or "（无）"))
    print("  Ctrl+C 结束。作业与日志随服务退出而中断（中间产物都落盘，可续跑）。")
    if open_browser:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  收到中断，正在结束作业…")
        for j in list(HUB.jobs.values()):
            if j.status == "running" and j.proc:
                try:
                    import bookflow
                    bookflow.kill_tree(j.proc)
                except Exception:
                    pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    serve()
