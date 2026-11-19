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
import shutil

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


# 文字层目录名。bookflow.TEXTLAYER_DIR 是同一个值，但这里**不能** import bookflow：
# webui/server.py 有意不 import 它（见那个文件开头），runs 是两边共用的那一层，
# 一旦 import 就把 rapidocr 那串重量级依赖拖进了「只是想列一下书」的调用里。
TEXTLAYER_DIR = "ocr_text"

# 判断「md 能读到哪份 OCR 结果」时的查找顺序，与 bookflow.OCR_DIRS 同源
# （规则是唯一的，只是这份清单不能从 bookflow 里 import，理由同上）。
# ocr_merged 是回填过的结果，本模块只判断"有没有页"，不需要它的优先级语义，
# 但顺序照抄可以避免「界面说能跑、md 实际选了另一份」这种分歧。
OCR_PRIORITY = ("ocr_layout", "ocr_merged", "ocr_text", "ocr_fix", "ocr")


def _page_jsons(d):
    """目录下的页级 JSON 文件名。模块内自己数，不 import bookflow。"""
    if not os.path.isdir(d):
        return []
    try:
        return [f for f in os.listdir(d) if f.lower().endswith(".png.json")]
    except OSError:
        return []


def _textlayer_meta(run_root):
    """读 _textlayer.meta（文字层的自述），没有或坏了返回 {}。"""
    p = os.path.join(paths.ocr_dir(run_root, TEXTLAYER_DIR), "_textlayer.meta")
    if not os.path.exists(p):
        return {}
    try:
        meta = jsonio.read(p) or {}
    except (ValueError, OSError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _textlayer_status(run_root):
    """文字层的状态：读 _textlayer.meta 的 usable/reason。没跑过返回 (None, "")。"""
    meta = _textlayer_meta(run_root)
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


def _source_pdf(run_root):
    """(源 PDF 的绝对路径, "") 或 ("", 一句为什么找不到)。

    两条来源，顺序不能反：
      1. manifest.input.pdf —— textlayer/render 真的跑过时记下的那一份（相对 run 根）；
      2. input/<book-id>/source.pdf —— WebUI 上传的落点。新书刚建好、还没跑过任何
         阶段时 manifest 里连这条记录都没有，但 PDF 已经在磁盘上了。

    run_root **不一定**是 runs/<id>：测试会传仓库根或临时目录，所以 book-id 只能
    取目录名，路径必须经 paths.input_dir() 拼（与 paths.work_dir 同一套规矩）。
    """
    run_root = os.path.abspath(run_root)
    rel = str(((mf.read(run_root).get("input") or {}).get("pdf") or "")).strip()
    if rel:
        cand = rel if os.path.isabs(rel) else os.path.join(run_root, rel.replace("/", os.sep))
        if os.path.isfile(cand):
            return os.path.abspath(cand), ""
    book_id = os.path.basename(run_root)
    if book_id:
        cand = os.path.join(paths.input_dir(book_id), "source.pdf")
        if os.path.isfile(cand):
            return cand, ""
    return "", ("没有源 PDF：先在「新建任务」里上传，或把它放到 input/%s/source.pdf"
                % (book_id or "<book-id>"))


def _report_has(run_root, name, need_nonempty=False):
    """报告目录里有没有这个文件；need_nonempty 时连「是空文件」也算没有。"""
    p = os.path.join(paths.report_dir(run_root), name)
    if not os.path.isfile(p):
        return False
    if need_nonempty:
        try:
            return os.path.getsize(p) > 0
        except OSError:
            return False
    return True


def stage_runnable(run_root, stage):
    """这个阶段现在点「运行」会不会当场失败：返回 (能不能跑, 不能跑的人话理由)。

    ## 为什么要在界面上回答这个问题

    两个实测后果叠在一起（详见 task 记录与 IO-CONVENTIONS §2 O5）：
    一是 progress.js 原来只给**已跑过**的阶段画按钮，于是所有未跑阶段在界面上
    连按钮都没有；二是补上按钮之后更糟——《中国僧官制度史》（runs/sengguan）的
    文字层 usable=false（每页平均 10 个汉字），verify 的 A 侧根本没有页 JSON，
    点下去必然 sys.exit("verify 需要先跑 textlayer 阶段")。那行报错只说明「缺什么
    文件」，不说明「为什么这本永远跑不了」。所以理由必须**先写在界面上**。

    ## 三条约束

    1. **纯文件系统检查**：只看 os.path.exists / isfile / 数文件名，不读大 JSON、
       不跑子进程。summary() 会被 /api/state 对每一本书调用，这里一旦变重，
       整个界面就跟着变慢。唯一读的文件是 manifest.json 和 _textlayer.meta，
       两者都是小文件（前者本来就是 stage_list 要读的）。
    2. **绝不抛异常**：任何一个 OSError/ValueError 都吞成「不可运行 + 理由」，
       因为调用它的是一条渲染路径——一个列不出目录的权限问题没有资格让整页报错。
    3. 用 paths 的目录函数解析位置，**不写死 "work/"/"report/"**：这里拿到的
       run_root 既可能是 tiered 的 runs/<id>，也可能是 flat 的旧目录或仓库根。
    """
    try:
        return _stage_runnable(run_root, stage)
    except (OSError, ValueError) as e:
        return False, "检查前置条件时出错：%s" % e


def _stage_runnable(run_root, stage):
    run_root = os.path.abspath(run_root)
    # 目录一律经 paths 解析（ocr_dir/pages_dir 内部走 work_dir）：这里拿到的 run_root
    # 可能是 tiered 的 runs/<id>，也可能是 flat 的旧目录甚至仓库根，拼 "work/" 会三者全错。

    if stage == "render":
        pdf, why = _source_pdf(run_root)
        return (True, "") if pdf else (False, why)

    if stage == "textlayer":
        pdf, why = _source_pdf(run_root)
        if not pdf:
            return False, why
        # 文字层走 poppler 的 pdftotext，与 render 用的 pdftoppm 同包但不一定同装；
        # 缺它时 textlayer 会在 check_pdftotext() 里 sys.exit，所以先查 PATH。
        if shutil.which("pdftotext") is None:
            return False, ("需要 poppler 的 pdftotext（PATH 里找不到）：文字层靠它抽取，"
                           "装好 poppler 并加进 PATH 再来；只要 OCR 的话这本可以跳过文字层")
        return True, ""

    if stage == "ocr":
        if _count_suffix(paths.pages_dir(run_root), ".png"):
            return True, ""
        return False, "还没有页图（pages 目录是空的）：先跑「渲染」"

    if stage == "repair":
        if _page_jsons(paths.ocr_dir(run_root, "ocr")):
            return True, ""
        return False, "还没有 OCR 识别结果（ocr 目录里没有页 JSON）：先跑「OCR」"

    if stage == "layout":
        if (_page_jsons(paths.ocr_dir(run_root, "ocr_fix"))
                or _page_jsons(paths.ocr_dir(run_root, "ocr"))):
            return True, ""
        return False, "还没有可打标签的 OCR 结果（ocr_fix / ocr 都是空的）：先跑「修复」"

    if stage == "md":
        for name in OCR_PRIORITY:
            if _page_jsons(paths.ocr_dir(run_root, name)):
                return True, ""
        return False, "没有任何 OCR 结果目录里有页 JSON：先跑「OCR」"

    if stage == "verify":
        # A 侧 = 文字层，B 侧 = ocr_fix。两侧缺一不可，缺哪侧说哪侧——
        # 但文字层的缺失更值得展开：它不是「还没跑」，而是「跑了也跑不出来」
        # （扫描件没有文字层），这句话必须连同 _textlayer.meta 的原文理由一起给出，
        # 否则用户会一直以为是流程顺序问题、反复重试。
        meta = _textlayer_meta(run_root)
        if not _page_jsons(paths.ocr_dir(run_root, TEXTLAYER_DIR)):
            reason = str(meta.get("reason") or "").strip()
            if meta.get("usable") is False and reason:
                return False, ("核对需要文字层与 OCR 两侧逐行对账，文字层不可用就没有 A 侧"
                               "（%s 里一页 JSON 都没有）：%s" % (TEXTLAYER_DIR, reason))
            return False, ("核对需要文字层与 OCR 两侧逐行对账，%s 里没有页 JSON：先跑「文字层」"
                           % TEXTLAYER_DIR)
        if not _page_jsons(paths.ocr_dir(run_root, "ocr_fix")):
            return False, "核对要拿 OCR 结果做 B 侧对照，ocr_fix 里没有页 JSON：先跑「修复」"
        return True, ""

    if stage == "audit":
        if _report_has(run_root, "book.md"):
            return True, ""
        return False, "还没有成书正文（report/book.md）：先跑「Markdown」"

    if stage == "epub":
        # 比 audit 多查一次「非空」：stage_epub 对 0 字节的 book.md 是直接 sys.exit 的，
        # 不存在和空文件在那里是同一个失败，界面也照同一个口径说。
        if _report_has(run_root, "book.md", need_nonempty=True):
            return True, ""
        return False, "还没有非空的成书正文（report/book.md）：先跑「Markdown」"

    return False, "未知阶段：%s" % stage


def _annotate_runnable(run_root, stages):
    """就地给每个 stages[] 元素补 can_run / why_not（只加字段，不改既有字段）。"""
    for st in stages:
        ok, why = stage_runnable(run_root, st.get("stage") or "")
        st["can_run"] = bool(ok)
        st["why_not"] = "" if ok else why
    return stages


def stage_list(run_root):
    """固定顺序的 9 个阶段 + 状态。有 manifest 就用 manifest，缺的再用推断补。"""
    rec = mf.read(run_root).get("stages") or {}
    if not rec:
        # 没有 manifest 的旧书：整份状态都是推断的，但仍然要知道「现在点了能不能跑」，
        # 否则界面上未跑阶段全是死按钮（这正是本次要修的第一条根因）。
        return _annotate_runnable(run_root, _infer_stages(run_root))
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
    return _annotate_runnable(run_root, out)


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
