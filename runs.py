"""bookflow.runs — 发现工作目录、解析 run id、给出各阶段状态。

WebUI 和 CLI 都从这里取「有哪些书、每本跑到哪了」，避免两处各写一套判断。

## run id 是什么

一个 run = 一个工作目录。三种形态（见 paths.py）都能当 run：

    <repo>/runs/<book-id>      新书，tiered 布局（manifest.json 在根）
    <repo>/run-lacan           旧书，flat 布局（pages/ out/ 直接躺在里面）
    <repo>                     旧书的极端情况：仓库根**就是**黑格尔那本的工作目录

所以 run id 一律是「相对仓库根的路径」，`.` 表示仓库根。**不接受绝对路径**，
也不接受指到仓库外面去的 id —— WebUI 是本地服务，但「上传一个能改任意文件路径的
参数」这种口子没必要留。

## 阶段状态从哪来（两条来源，必须能区分）

1. **manifest.json**（新书）：阶段真的跑过就有一条记录，status 是 ok/degraded/failed/skipped。
   这是**权威**的。
2. **推断**（旧书，没有 manifest）：只能看目录和文件在不在。

推断必须被**标记出来**（返回值里 `inferred: true`），否则界面会把「目录存在」
当成「阶段跑完」展示给人——而 IO-CONVENTIONS §2 O5 记录的正是这件事：
run-sengguan/ocr_text 只剩一个 _textlayer.meta 文件、一个页 JSON 都没有，
它与「这一步还没跑」在文件系统上长得一模一样。
"""
import os
import re

import jsonio
import manifest as mf
import paths

# 目录名白名单：只允许这些字符，避免路径穿越，也避免中文目录名进来后
# URL 编码一路传下去（本项目的产物名本来就有全角冒号，那是另一件事）。
RE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

# 发现 run 时要排除的目录：代码、文档、测试、前端、以及交付物目录本身。
# 注意：**不能**把 "runs" 放进来。它在 list_ids 里有专门的分支（只认 runs/<id> 这层），
# 一旦列进 _SKIP，`if n in _SKIP: continue` 会先命中，那个分支永远走不到——
# 表现为新书建好了却不出现在界面上，而接口 404 又被前端兜成一张空卡片，
# 于是「明明跑完了却显示全部未跑」（实测就这么发生了）。
_SKIP = {"docs", "tests", "webui", "dist", "input", "assets", "node_modules",
         "__pycache__", ".git", ".venv", ".backup"}


def _is_run_dir(d):
    """这个目录像不像一个工作目录：有 pages/ 或 out/ 或 manifest.json 或 work/ 或 report/。"""
    if not os.path.isdir(d):
        return False
    for name in ("pages", "out", "manifest.json", paths.TIER_WORK, paths.TIER_REPORT):
        if os.path.exists(os.path.join(d, name)):
            return True
    return False


def list_ids(repo=None):
    """仓库里所有 run id（含 `.`）。runs/ 下的新书写成 runs/<id>，只认下一层。"""
    repo = os.path.abspath(repo or paths.repo_root())
    out = []
    if _is_run_dir(repo):
        out.append(".")
    try:
        names = sorted(os.listdir(repo))
    except OSError:
        return out
    for n in names:
        if n.startswith(".") or n in _SKIP:
            continue
        d = os.path.join(repo, n)
        if not os.path.isdir(d):
            continue
        if n == "runs":
            try:
                subs = sorted(os.listdir(d))
            except OSError:
                continue
            for sub in subs:
                if not sub.startswith(".") and _is_run_dir(os.path.join(d, sub)):
                    out.append("runs/" + sub)
            continue
        if _is_run_dir(d):
            out.append(n)
    return out


def resolve(run_id, repo=None):
    """run id -> 绝对路径。非法或越界返回 None。"""
    repo = os.path.abspath(repo or paths.repo_root())
    if run_id in ("", None, "."):
        return repo
    if not isinstance(run_id, str):
        return None
    rid = run_id.replace("\\", "/")
    # 绝对路径一律拒绝（Windows 的 C:/... 带盘符，POSIX 风格以 / 开头）。
    # 不靠 commonpath 兜底："/abs/path" 去掉前导斜杠后会被拼成 <repo>/abs/path，
    # 恰好仍在仓库内——那等于把「绝对路径」悄悄当成相对路径用，是更坏的静默行为。
    if rid.startswith("/") or ":" in rid:
        return None
    parts = rid.strip("/").split("/")
    if not parts or len(parts) > 2:
        return None
    for p in parts:
        if p in ("", ".", "..") or not RE_ID.match(p):
            return None
    d = os.path.abspath(os.path.join(repo, *parts))
    if os.path.commonpath([d, repo]) != repo:
        return None
    return d


def _count_suffix(d, suffix):
    if not os.path.isdir(d):
        return 0
    try:
        return len([f for f in os.listdir(d) if f.lower().endswith(suffix)])
    except OSError:
        return 0


def _epub_of(run_root):
    """(有 EPUB?, 绝对路径)。先看 dist/<book-id>/，再看报告目录，最后看仓库级 dist/。"""
    run_root = os.path.abspath(run_root)
    book_id = os.path.basename(run_root)
    cands = []
    for d in (paths.dist_dir(book_id), paths.report_dir(run_root)):
        if os.path.isdir(d):
            cands += [os.path.join(d, f) for f in sorted(os.listdir(d))
                      if f.lower().endswith(".epub")]
    if not cands and run_root == os.path.abspath(paths.repo_root()):
        # 仓库根那本（旧布局）历史上把 epub 出在 out/ 里；dist/ 也可能有
        d0 = paths.dist_dir()
        if os.path.isdir(d0):
            cands += [os.path.join(d0, f) for f in sorted(os.listdir(d0))
                      if f.lower().endswith(".epub")]
    if not cands:
        return False, ""
    cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return True, cands[0]


def _report(run_root):
    p = os.path.join(paths.report_dir(run_root), "report.json")
    if not os.path.exists(p):
        return {}
    try:
        return jsonio.read(p) or {}
    except (ValueError, OSError):
        return {}


def _title(run_root):
    t = ((_report(run_root).get("book") or {}).get("title") or "").strip()
    if t:
        return t
    has, p = _epub_of(run_root)
    if has:
        return os.path.splitext(os.path.basename(p))[0]
    bid = os.path.basename(os.path.abspath(run_root)) or "（仓库根）"
    meta_p = os.path.join(paths.input_dir(bid), "meta.json")
    if os.path.exists(meta_p):
        try:
            t = (jsonio.read(meta_p) or {}).get("title") or ""
            if t:
                return t
        except (ValueError, OSError):
            pass
    return bid


def _textlayer_status(run_root):
    """文字层的状态：读 _textlayer.meta 的 usable/reason。没跑过返回 (None, "")。"""
    p = os.path.join(paths.work_dir(run_root), "ocr_text", "_textlayer.meta")
    if not os.path.exists(p):
        return None, ""
    try:
        meta = jsonio.read(p) or {}
    except (ValueError, OSError):
        return None, ""
    if not meta:
        return None, ""
    ok = bool(meta.get("usable"))
    return ("ok" if ok else "degraded"), (meta.get("reason") or "")


def _infer_stages(run_root):
    """没有 manifest 时的推断。每项都带 inferred=True，界面必须标出来。"""
    work = paths.work_dir(run_root)
    rep = paths.report_dir(run_root)
    stages = []

    def add(s, status, count=None, detail=""):
        stages.append({"stage": s, "status": status, "at": "",
                       "count": count, "detail": detail, "inferred": True})

    n_pages = _count_suffix(os.path.join(work, "pages"), ".png")
    add("render", "ok" if n_pages else "missing", n_pages)

    tl_status, tl_reason = _textlayer_status(run_root)
    if tl_status is None:
        add("textlayer", "missing")
    else:
        add("textlayer", tl_status, _count_suffix(os.path.join(work, "ocr_text"), ".png.json"),
            tl_reason)

    for s, d in (("ocr", "ocr"), ("repair", "ocr_fix"), ("layout", "ocr_layout")):
        n = _count_suffix(os.path.join(work, d), ".png.json")
        add(s, "ok" if n else "missing", n)

    add("md", "ok" if os.path.exists(os.path.join(rep, "book.md")) else "missing")
    add("verify", "ok" if os.path.exists(os.path.join(rep, "disagree.json")) else "missing")
    add("audit", "ok" if os.path.exists(os.path.join(rep, "audit.txt")) else "missing")
    has, _p = _epub_of(run_root)
    add("epub", "ok" if has else "missing")
    return stages


def stage_list(run_root):
    """固定顺序的 9 个阶段 + 状态。有 manifest 就用 manifest，缺的再用推断补。"""
    rec = mf.read(run_root).get("stages") or {}
    if not rec:
        return _infer_stages(run_root)
    inferred = {s["stage"]: s for s in _infer_stages(run_root)}
    out = []
    for s in mf.STAGES:
        r = rec.get(s) or {}
        if r.get("status"):
            out.append({"stage": s, "status": r.get("status"), "at": r.get("at") or "",
                        "count": r.get("count"), "detail": r.get("reason") or "",
                        "elapsed": r.get("elapsed"), "inferred": False})
        else:
            g = dict(inferred.get(s) or {"stage": s, "status": "missing"})
            g["inferred"] = True
            out.append(g)
    return out


def pending_decisions(run_root):
    """(未裁决条数, 总条数)。没有裁决表返回 (0, 0)。"""
    p = paths.decisions_path(run_root)
    if not os.path.exists(p):
        return 0, 0
    try:
        doc = jsonio.read(p) or {}
    except (ValueError, OSError):
        return 0, 0
    items = doc.get("decisions") or []
    return sum(1 for e in items if (e.get("verdict") or "?") == "?"), len(items)


def summary(run_id, repo=None):
    """一个 run 的概要（对应 webui/API.md 的 runs[] 元素）。run 不存在返回 None。"""
    d = resolve(run_id, repo)
    if not d or not os.path.isdir(d):
        return None
    work = paths.work_dir(d)
    has_epub, epub_p = _epub_of(d)
    pend, total = pending_decisions(d)
    stages = stage_list(d)
    # 进度按 stage_list 算，不按 manifest 算：旧书没有 manifest，
    # 只看 manifest 会让一本已经出完的书显示 0/9（实测就是这么显示的）。
    done = sum(1 for s in stages if s["status"] in ("ok", "degraded", "skipped"))
    cur = next((s["stage"] for s in stages
                if s["status"] not in ("ok", "degraded", "skipped")), "")
    return {
        "id": run_id,
        "path": "." if run_id == "." else run_id.replace("\\", "/"),
        "layout": paths.layout_of(d),
        "title": _title(d),
        "pages": _count_suffix(os.path.join(work, "pages"), ".png"),
        "has_epub": has_epub,
        "epub_name": os.path.basename(epub_p) if has_epub else "",
        "pending_decisions": pend,
        "total_decisions": total,
        "stages": stages,
        "progress": {"done": done, "total": len(stages),
                     "percent": round(100.0 * done / len(stages), 1) if stages else 0.0,
                     "current": cur},
    }


def all_summaries(repo=None):
    """全部 run。排序：已有 EPUB 的在前，然后按 id。"""
    out = []
    for rid in list_ids(repo):
        s = summary(rid, repo)
        if s:
            out.append(s)
    out.sort(key=lambda x: (not x["has_epub"], x["id"]))
    return out
