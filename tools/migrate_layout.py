#!/usr/bin/env python
"""把旧的平铺工作目录迁移成分层布局（tiered），**可回滚、可预演**。

    python tools/migrate_layout.py --work run-lacan             # 预演：只打印要搬什么
    python tools/migrate_layout.py --work run-lacan --apply      # 真搬
    python tools/migrate_layout.py --work run-lacan --rollback   # 按记录搬回去
    python tools/migrate_layout.py --all                         # 批量预演：仓库根 + 所有 run-*/
    python tools/migrate_layout.py --all --apply                 # 批量真搬（每本一份记录）

## 为什么需要它，以及为什么默认不跑

paths.py 已经让**两种布局并存**：旧目录（pages/ out/ 直接躺第一层）照常能跑，
新书（runs/<id>/ 下的 work/ report/ review/）用新布局。所以迁移不是必须的——
它只解决 IO-CONVENTIONS §1 I4 说的那件事：仓库根同时是工作目录，
于是 .gitignore 要写一长串裸名规则，而"想只删中间产物"和"想只拷交付物"
都必须人工挑。

**默认预演，不 --apply 就一个字节都不动。** 理由是这批目录里有
run-lacan 的 2825 条裁决表（全项目唯一不可重建的产物）和四本黄金 book.md；
搬错一次没有第二次机会。

## 目标位置：**所有**源树都进 runs/<book-id>/

    <repo>/              -> <repo>/runs/hegel        （仓库根就是黑格尔那本的工作目录）
    <repo>/run-lacan/    -> <repo>/runs/lacan
    <repo>/run-guixun/   -> <repo>/runs/guixun
    <repo>/run-sengguan/ -> <repo>/runs/sengguan

这条是**一条规则**，不是四个特例：dest_root_of() 是全文件里唯一一处
「文件该搬到哪」的判断，plan/apply/rollback 都走它。仓库根不特殊——
它只是源根恰好等于仓库根的普通输入（代码得留在第一层，所以数据必须让开）。
搬完把空掉的源目录删掉：run-*/ 整个删，仓库根只删空掉的数据目录（pages/ out/ 等），
代码/文档/测试一个都不碰。工作树最后只剩 runs/<book-id>/ 一套布局。

## book id 怎么来（runs/<book-id>/ 与 dist/<book-id>/ 共用）

    run-lacan/   -> lacan     run-guixun/   -> guixun     run-sengguan/   -> sengguan
    <repo>/      -> hegel     （--repo-root-id 可改）

理由：runs/ 已经表达了「这是一次任务」，目录名再叫 run-lacan 就成了
runs/run-lacan（读起来重复），而 book-id 同时是 dist/<book-id>/ 的目录名——
交付物目录叫 dist/run-lacan/ 更没必要。去掉 run- 前缀后 id 仍然过
runs.RE_ID（^[A-Za-z0-9._-]+$），runs.resolve("runs/lacan") 照常放行。
显式给 --book-id 时以参数为准；非法 id（中文、带斜杠、空）直接退出，不猜。

## 搬什么（映射是**全函数**，没有隐式规则）

    pages/ ocr*/                   -> work/<同名>
    out/ 里的机器报告              -> report/<同名>
    out/decisions.json             -> review/decisions.json     ← 唯一的人工件
    out/images/                    -> report/images/
    out/*.epub                     -> dist/<book-id>/（交付物离开工作目录）
    scratch：out/校对清单.md 等     -> review/

「机器报告」与「人工件」怎么分只有一条判据：**是不是脚本生成的**。
脚本每日重跑都会覆盖的（book.md / report.json / qa.txt / audit.* / disagree.* /
review.png / cover.jpg）进 report/；只有人写过的进 review/。
不认识的文件一律**留在原处不动**并打进"未分类"清单——迁移工具不许替人做判断。

## 搬完补一份 manifest.json

旧树从来没有 manifest（那是分层布局才有的东西），于是迁移后 runs.summary()
只能靠目录推断，书的标题/作者得从 report.json 里挖。这里顺手把 manifest.json
补出来：input（title/author/publisher/pages）+ params（md_src）从旧 report.json 抄，
另加 _migrated 记这次搬迁的证据（时间、book.md 与 decisions.json 的 sha256）。
**不写 stages**——旧树没跑过的阶段就是没跑过，补一个假的 ok 只会让进度条骗人（O5）。

manifest 只是附加信息：写失败不影响搬迁结果（与 stage_manifest 同一条纪律）。

## 无损证明（哈希守卫）

搬之前记下 book.md / decisions.json / 校对清单.md 的 sha256，搬完逐个复核；
只要有一个字节不同就 **sys.exit**，提示用 --rollback 搬回去。
--all 还会打印每本迁移前后的文件数/体积，供人工对账。

## 回滚

--apply 会写一份 <dest>/.migration-<ts>.json 记录每一步（源、目标、有没有删源目录），
--rollback 读它反向搬回去（需要时把源目录重建出来）。
记录放在**目标根**：源目录搬完可能已经不在（run-*/ 会被整个删掉），
日志不能跟着它一起消失。（.gitignore 挡掉 .migration-*，不进提交。）
回滚会顺带删掉迁移时补出来的 manifest.json —— 但只在 _migrated.at 与这份记录
对得上时才删，不碰别人的。
"""
import argparse
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import jsonio        # noqa: E402
import manifest as mf  # noqa: E402
import paths         # noqa: E402
import runs          # noqa: E402
import stdio         # noqa: E402

# 中间的 OCR 目录：整体搬进 work/，内容不动（页级 JSON 里没有任何绝对路径依赖）
WORK_MOVES = paths.WORK_DIRS

# out/ 里属于「机器报告」的文件：脚本产出、可重生成、删了重跑即可
REPORT_FILES = ("book.md", "report.json", "qa.txt", "audit.txt", "audit.json",
                "disagree.txt", "disagree.json", "review.png", "review_queue.json",
                "cover.jpg")

# out/ 里属于「人工件」的：只有人写过，脚本只读。**永远不能自动重建**
REVIEW_FILES = ("decisions.json", "校对清单.md", "notes.md")

# 交付物：离开工作目录，进仓库级 dist/<book-id>/
DELIVERABLE_EXT = (".epub",)

# 仓库根那本的默认 book id（配置项，不是散落在流程里的硬编码）
REPO_ROOT_BOOK_ID = "hegel"

# 搬完必须逐字节不变的产物：(角色, 文件名)。存在才校验，不存在不报错。
GUARD_FILES = (("report", "book.md"), ("review", "decisions.json"),
               ("review", "校对清单.md"))

# 批量模式的目录前缀：仓库下 run-xxx/ 都算候选
BATCH_PREFIX = "run-"


def _rel(run_root, p):
    return os.path.relpath(p, run_root).replace("\\", "/")


def _props(book_id):
    """book id 属性：必须过 runs 的白名单（WebUI 的 URL 与 dist/ 目录名共用）。"""
    if not book_id or not runs.RE_ID.match(book_id):
        sys.exit("非法 book id：%r\n  只允许 ^[A-Za-z0-9._-]+$（与 runs.RE_ID 同一口径）。"
                 "中文、带斜杠、空都会在 WebUI 的 URL 上多出失败面。" % (book_id,))
    return book_id


def is_repo_root(run_root):
    return os.path.abspath(run_root) == os.path.abspath(paths.repo_root())


def book_id_of(run_root, book_id=None, repo_root_id=REPO_ROOT_BOOK_ID):
    """工作目录 -> book id。规则见模块 docstring；显式参数优先。"""
    if book_id:
        return _props(book_id)
    run_root = os.path.abspath(run_root)
    if is_repo_root(run_root):
        return _props(repo_root_id)
    base = os.path.basename(run_root)
    if base.startswith(BATCH_PREFIX) and len(base) > len(BATCH_PREFIX):
        return _props(base[len(BATCH_PREFIX):])
    return _props(base)


def dest_root_of(src_root, book_id):
    """源工作目录 -> 目标 run 根 = **永远是** <repo>/runs/<book-id>/。

    这是全文件里唯一一处「文件该搬到哪」的判断。仓库根不特殊：
    它只是源根恰好等于仓库根的普通输入（代码得留在第一层，所以数据必须让开）。
    搬完「把空掉的源目录删掉」那一步在 apply() 里，与 run-*/ 走同一条路。
    """
    return os.path.join(os.path.abspath(paths.runs_root()), book_id)


def looks_like_flat_workdir(d):
    """这个目录像不像一棵**平铺**工作目录（而不是代码/文档/交付物目录）。

    只看第一层的目录名，不看内容——与 paths.is_tiered「只看目录在不在」同一纪律。
    仓库根也是靠这条认出「它就是黑格尔那本的工作目录」。
    """
    if not os.path.isdir(d) or paths.is_tiered(d):
        return False
    return any(os.path.isdir(os.path.join(d, n)) for n in paths.FLAT_MARKERS)


def discover_targets(repo=None, repo_root_id=REPO_ROOT_BOOK_ID):
    """批量模式的候选：[(绝对路径, book_id, 是否仓库根)]。

    顺序刻意让仓库根排**最后**：它搬完要删的源目录最多（数据目录都空出来），
    万一批量搬到一半失败，前面几本已可回滚，仓库根还没动。
    """
    repo = os.path.abspath(repo or paths.repo_root())
    out = []
    try:
        names = sorted(os.listdir(repo))
    except OSError:
        names = []
    for n in names:
        if n.startswith(".") or not n.startswith(BATCH_PREFIX):
            continue
        d = os.path.join(repo, n)
        if looks_like_flat_workdir(d):
            out.append((d, book_id_of(d, None, repo_root_id), False))
    if looks_like_flat_workdir(repo):
        out.append((repo, _props(repo_root_id), True))
    return out


def plan(run_root, book_id=None, repo_root_id=REPO_ROOT_BOOK_ID):
    """算出要做的每一步。

    返回 {"already", "moves", "unknown", "book_id", "dest_root"}。
    纯函数：不碰文件系统里除「读目录」之外的任何东西。
    """
    run_root = os.path.abspath(run_root)
    bid = book_id_of(run_root, book_id, repo_root_id)
    dest = dest_root_of(run_root, bid)
    moves, unknown = [], []

    if paths.is_tiered(run_root):
        return {"already": True, "moves": [], "unknown": [],
                "book_id": bid, "dest_root": run_root}
    if paths.is_tiered(dest):
        return {"already": True, "moves": [], "unknown": [],
                "book_id": bid, "dest_root": dest}

    for name in WORK_MOVES:
        src = os.path.join(run_root, name)
        if os.path.isdir(src):
            moves.append({"from": _rel(run_root, src),
                          "to": "/".join([paths.TIER_WORK, name])})

    out = os.path.join(run_root, "out")
    if os.path.isdir(out):
        for f in sorted(os.listdir(out)):
            p = os.path.join(out, f)
            if os.path.isdir(p):
                if f == "images":
                    moves.append({"from": _rel(run_root, p),
                                  "to": "/".join([paths.TIER_REPORT, "images"])})
                elif f == ".bak":
                    # 备份目录：搬到 work/.backup/ 下（O10），不删
                    moves.append({"from": _rel(run_root, p),
                                  "to": "/".join([paths.TIER_WORK, ".backup", "legacy-out"])})
                else:
                    unknown.append(_rel(run_root, p))
                continue
            low = f.lower()
            if low.endswith(DELIVERABLE_EXT):
                moves.append({"from": _rel(run_root, p),
                              "to": "dist/%s/%s" % (bid, f), "dist": True})
            elif f in REVIEW_FILES:
                moves.append({"from": _rel(run_root, p),
                              "to": "/".join([paths.TIER_REVIEW, f])})
            elif f in REPORT_FILES:
                moves.append({"from": _rel(run_root, p),
                              "to": "/".join([paths.TIER_REPORT, f])})
            else:
                # 不认识的一律不动：迁移工具不许替人判断这是什么
                unknown.append(_rel(run_root, p))
        # out/ 只在**空掉之后**才删，见 apply()
    return {"already": False, "moves": moves, "unknown": unknown,
            "book_id": bid, "dest_root": dest}


def _target_abs(dest_root, to):
    """把 plan 里的 "to" 解析成绝对路径（dist/ 是相对仓库根的）。"""
    if to.startswith("dist/"):
        return os.path.join(paths.repo_root(), to.replace("/", os.sep))
    return os.path.join(dest_root, to.replace("/", os.sep))


def tree_stats(d):
    """(文件数, 字节数)。给迁移前后对账用。"""
    n = size = 0
    for root, _dirs, files in os.walk(d):
        for f in files:
            n += 1
            try:
                size += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n, size


def _guards(run_root):
    """逐字节指纹：{"report/book.md": sha256, ...}。

    走 paths.report_dir / review_dir，所以 flat 树与 tiered 树都能用
    （迁移前后的两次调用分别落在两种布局上，这是"搬迁无损"的唯一证据）。
    """
    got = {}
    for tier, name in GUARD_FILES:
        base = paths.report_dir(run_root) if tier == "report" else paths.review_dir(run_root)
        p = os.path.join(base, name)
        if os.path.isfile(p):
            got["%s/%s" % (tier, name)] = mf.sha256_of(p)
    return got


def check_guards(before, after):
    """逐项比对指纹，返回不一致的清单（空 = 无损）。"""
    bad = []
    for k, v in before.items():
        if not v:
            continue
        w = after.get(k)
        if w != v:
            bad.append((k, v, w or "（新位置没有这个文件）"))
    return bad


def _report_meta(dest_root):
    """从旧 report.json 抄书元数据（迁移后 summary() 才拿得到书名）。"""
    p = os.path.join(paths.report_dir(dest_root), "report.json")
    try:
        doc = jsonio.read(p) or {}
    except (ValueError, OSError):
        return {}
    book = doc.get("book") or {}
    params = doc.get("params") or {}
    return {"title": (book.get("title") or "").strip(),
            "author": (book.get("author") or "").strip(),
            "publisher": (book.get("publisher") or "").strip(),
            "md_src": (params.get("src") or "").strip()}


def write_manifest(dest_root, book_id, moves, src_root=None, at=""):
    """搬完补一份 manifest.json：旧树从来没有过，新布局靠它记输入身份。

    **不写 stages**：旧树没跑过的阶段就是没跑过。
    返回 manifest 绝对路径；写失败返回 ""，不影响搬迁结果。

    at 必须由调用方传入（= 迁移日志的 at），**不能在这里自己 strftime**：
    回滚靠 _migrated.at == log["at"] 判断「这份 manifest 是本次迁移写的」，
    两处各取一次时间，跨过整秒边界就对不上——表现是回滚后
    runs/<id>/manifest.json 还在，于是 runs/<id>/ 这个空壳也删不掉（实测）。
    """
    try:
        n_pages, _ = tree_stats(paths.pages_dir(dest_root))
        m = _report_meta(dest_root)
        mf.set_input(dest_root, pdf="", title=m.get("title", ""),
                     author=m.get("author", ""),
                     publisher=m.get("publisher", ""),
                     pages=n_pages or None)
        if m.get("md_src"):
            mf.set_params(dest_root, md_src=m["md_src"])
        mf.update(dest_root, book_id=book_id, _migrated={
            "at": at or time.strftime("%Y-%m-%dT%H:%M:%S"),
            "from": ("." if (src_root is None or os.path.abspath(src_root)
                             == os.path.abspath(dest_root))
                     else _rel(paths.repo_root(), src_root)),
            "moves": len(moves),
            "guards": _guards(dest_root),
        })
        return paths.manifest_path(dest_root)
    except Exception as e:      # noqa: BLE001 —— 元数据没有资格挡住迁移
        sys.stderr.write("  ！manifest 写入失败（不影响搬迁结果）：%s\n" % e)
        return ""


def _write_log(dest_root, log, log_path=None):
    """迁移记录写在**目标根**：源目录搬完可能已经不存在，日志不能跟着消失。"""
    log_path = log_path or os.path.join(dest_root, ".migration-%s.json"
                                        % time.strftime("%Y%m%d-%H%M%S"))
    jsonio.write(log_path, log, indent=1)
    return log_path


def _prune_src(src_root, log):
    """搬完之后清掉源树里空掉的目录。

    两种形态走同一个循环，区别只在最后一步：
      * run-lacan/ 这种容器目录：数据全搬走 -> 整个目录删掉（工作树只剩一套布局）；
      * 仓库根：只删空掉的数据目录（pages/ ocr*/ out/），代码/文档/测试一个都不碰。
    **只要还有未分类文件，目录就不会空，也就不会被删**——文件跟着目录一起消失
    是这个工具最不能犯的错。
    """
    removed = []
    for name in WORK_MOVES + ("out",):
        d = os.path.join(src_root, name)
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
            removed.append(name)
    log["src_dirs_removed"] = removed
    if not is_repo_root(src_root):
        try:
            os.rmdir(src_root)          # 只有整体空了才会成功
            log["src_root_removed"] = True
        except OSError:
            log["src_root_removed"] = False
    return removed


def apply(src_root, book_id=None, log_path=None, repo_root_id=REPO_ROOT_BOOK_ID):
    """执行迁移。**每一步都记进日志**，失败即停并提示怎么回滚。"""
    src_root = os.path.abspath(src_root)
    p = plan(src_root, book_id, repo_root_id)
    dest_root = p["dest_root"]
    if p["already"]:
        print("  已经是分层布局：%s" % dest_root)
        return 0
    if os.path.isdir(dest_root) and os.listdir(dest_root):
        sys.exit("目标目录已存在且非空，拒绝覆盖：%s\n"
                 "  这说明 runs/%s 里已经有东西（分层布局的产物或上一次迁移的残骸），"
                 "迁移必须人工判断保留哪一份。" % (dest_root, p["book_id"]))

    before = _guards(src_root)
    n_before = b_before = 0
    for name in WORK_MOVES:
        n, sz = tree_stats(os.path.join(src_root, name))
        n_before += n
        b_before += sz

    log = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "src": src_root,
           "dest": dest_root, "book_id": p["book_id"], "moves": [],
           "created": [], "dest_created": False, "runs_created": False,
           "src_dirs_removed": [], "src_root_removed": False,
           "manifest": "", "manifest_sha256": "", "guards_before": before}

    # 先建骨架目录，回滚时按 created 逆序删空目录
    runs_dir = os.path.dirname(dest_root)
    if not os.path.isdir(runs_dir):
        os.makedirs(runs_dir)
        log["runs_created"] = True
    if not os.path.isdir(dest_root):
        os.makedirs(dest_root)
        log["dest_created"] = True
    for t in (paths.TIER_WORK, paths.TIER_REPORT, paths.TIER_REVIEW):
        d = os.path.join(dest_root, t)
        if not os.path.isdir(d):
            os.makedirs(d)
            log["created"].append(t)

    n = 0
    for mv in p["moves"]:
        src = os.path.join(src_root, mv["from"].replace("/", os.sep))
        dst = _target_abs(dest_root, mv["to"])
        if not os.path.exists(src):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            sys.exit("目标已存在，拒绝覆盖：%s\n  这说明目录里已有分层布局的产物，"
                     "迁移必须人工判断保留哪一份。\n  已搬的 %d 项可以回滚："
                     "python tools/migrate_layout.py --work %s --rollback"
                     % (dst, n, src_root))
        shutil.move(src, dst)
        log["moves"].append({"from": mv["from"], "to": mv["to"]})
        n += 1
        print("  %-28s -> %s" % (mv["from"], mv["to"]))

    # 补 manifest（目标根此时已经是 tiered）
    mpath = write_manifest(dest_root, p["book_id"], log["moves"], src_root,
                           at=log["at"])
    log["manifest"] = _rel(dest_root, mpath) if mpath else ""
    log["manifest_sha256"] = mf.sha256_of(mpath) if mpath else ""

    # ---- 无损守卫：任何一个字节变了就停，别让人以为搬成功了 ----
    after = _guards(dest_root)
    log["guards_after"] = after
    bad = check_guards(before, after)
    if bad:
        log["guard_failures"] = [{"file": k, "before": v, "after": w} for k, v, w in bad]
        _write_log(dest_root, log, log_path)
        sys.exit("迁移破坏了产物字节，已停止：\n%s\n"
                 "  回滚：python tools/migrate_layout.py --work %s --rollback"
                 % ("\n".join("  %s  %s -> %s" % t for t in bad), dest_root))

    # 源树收尾：空目录删掉（有未分类文件时自然删不动）
    removed = _prune_src(src_root, log)

    n_after, b_after = tree_stats(dest_root)
    log["stats"] = {"before": {"files": n_before, "bytes": b_before},
                    "after": {"files": n_after, "bytes": b_after}}
    log_path = _write_log(dest_root, log, log_path)
    print("\n  搬了 %d 项；无损校验 %d 项通过" % (n, len(before)))
    if removed:
        print("  清掉空掉的源目录：%s" % "、".join(removed))
    if log.get("src_root_removed"):
        print("  源目录已空，整个删除：%s" % src_root)
    print("  回滚记录 -> %s" % log_path)
    if p["unknown"]:
        print("  未分类（**没动**，请人工决定）：")
        for u in p["unknown"]:
            print("    " + u)
    print("  下一步：python bookflow.py md --work %s   （验证仍能出书）" % dest_root)
    print("  回滚：python tools/migrate_layout.py --work %s --rollback" % dest_root)
    return 0


def _find_log(work, log_path=None):
    """找这个工作目录的迁移记录。work 给源目录或目标目录都能找到。

    记录写在 <dest>/.migration-*.json，所以扫描两处：
      ① work 自己（work 就是目标目录时）；
      ② <repo>/runs/*/（work 是已经被删掉的源目录时）。
    """
    if log_path:
        return log_path
    work = os.path.abspath(work)
    cands = []
    if os.path.isdir(work):
        cands += [os.path.join(work, f) for f in sorted(os.listdir(work))
                  if f.startswith(".migration-")]
    runs_dir = os.path.abspath(paths.runs_root())
    if os.path.isdir(runs_dir):
        for sub in sorted(os.listdir(runs_dir)):
            d = os.path.join(runs_dir, sub)
            if not os.path.isdir(d):
                continue
            cands += [os.path.join(d, f) for f in sorted(os.listdir(d))
                      if f.startswith(".migration-")]
    cands = [c for c in cands if _log_matches(c, work)]
    return cands[-1] if cands else None


def _log_matches(log_path, work):
    """这份记录是不是这个工作目录的（源或目标对得上都算）。"""
    try:
        doc = jsonio.read(log_path) or {}
    except (ValueError, OSError):
        return False
    src = os.path.abspath(doc.get("src") or "")
    dest = os.path.abspath(doc.get("dest") or "")
    return work in (src, dest)


def rollback(work, log_path=None):
    """按日志反向搬回。找不到日志就明确报错，不猜。"""
    work = os.path.abspath(work)
    fp = _find_log(work, log_path or None)
    if not fp:
        sys.exit("找不到 %s 的迁移记录 .migration-*.json，无法回滚"
                 "（不会凭猜测搬文件）" % work)
    log = jsonio.read(fp)
    src_root = os.path.abspath(log.get("src") or work)
    dest_root = os.path.abspath(log.get("dest") or work)
    was_removed = bool(log.get("src_root_removed"))
    n = 0
    for mv in reversed(log.get("moves") or []):
        cur = _target_abs(dest_root, mv["to"])
        dst = os.path.join(src_root, mv["from"].replace("/", os.sep))
        if not os.path.exists(cur):
            print("  跳过（源已不在）：%s" % mv["to"])
            continue
        if os.path.exists(dst):
            sys.exit("回滚目标已存在，拒绝覆盖：%s" % dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(cur, dst)
        n += 1
        print("  %-40s -> %s" % (mv["to"], mv["from"]))
    # 删掉迁移时新建的空目录（逆序）：先删 work/report/review，再删目标根与 runs/
    for t in reversed(log.get("created") or []):
        d = os.path.join(dest_root, t)
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
    # manifest 是迁移时补的，回滚要删掉；但只在它确实是本次迁移写的时才删
    mp = paths.manifest_path(dest_root)
    if os.path.isfile(mp):
        try:
            mine = (jsonio.read(mp) or {}).get("_migrated") or {}
        except (ValueError, OSError):
            mine = {}
        if mine.get("at") == log.get("at"):
            os.remove(mp)
    if was_removed and not os.path.isdir(src_root):
        os.makedirs(src_root, exist_ok=True)
    # 目标根若只剩下这份迁移记录，就把它搬回源根再删掉目标根——
    # 回滚的意义就是"当作没迁移过"，留一个 runs/<id>/ 只装日志的空壳不算。
    # 记录本身一个字节都不丢（只换位置），它仍是"这批文件曾经在哪"的唯一证据。
    fp = _sweep_dest(dest_root, src_root, fp)
    print("\n  回滚了 %d 项（记录保留：%s）" % (n, fp))
    return 0


def _sweep_dest(dest_root, src_root, fp):
    """目标根里若只剩 .migration-*.json，把这些记录挪回源根并删掉空壳。"""
    if os.path.abspath(dest_root) == os.path.abspath(src_root):
        return fp
    if not os.path.isdir(dest_root):
        return fp
    left = os.listdir(dest_root)
    if any(not f.startswith(".migration-") for f in left):
        return fp
    os.makedirs(src_root, exist_ok=True)
    new_fp = fp
    for f in left:
        dst = os.path.join(src_root, f)
        # 源根已有同名记录时不覆盖（宁可两份并存，也不能丢证据）
        if os.path.exists(dst):
            base, ext = os.path.splitext(f)
            dst = os.path.join(src_root, base + "-1" + ext)
        shutil.move(os.path.join(dest_root, f), dst)
        if os.path.abspath(f) == os.path.abspath(os.path.basename(fp)) or                 os.path.abspath(os.path.join(dest_root, f)) == os.path.abspath(fp):
            new_fp = dst
    try:
        os.rmdir(dest_root)
        os.rmdir(os.path.dirname(dest_root))     # <repo>/runs/ 空了就一起删
    except OSError:
        pass
    return new_fp


def _print_plan(p, src_root):
    if p["already"]:
        print("  已经是分层布局：%s" % p["dest_root"])
        return
    print("  预演（不会改动任何文件）")
    print("  源   %s" % src_root)
    print("  目标 %s   （book-id=%s）" % (p["dest_root"], p["book_id"]))
    for mv in p["moves"]:
        print("    %-28s -> %s" % (mv["from"], mv["to"]))
    if p["unknown"]:
        print("    未分类（**不会动**）：")
        for u in p["unknown"]:
            print("      " + u)
    print("    共 %d 项要搬。" % len(p["moves"]))


def run_batch(apply_it=False, repo_root_id=REPO_ROOT_BOOK_ID, repo=None):
    """批量：仓库根 + 所有 run-*/。每本独立写 .migration-<ts>.json。

    单独一本失败**不吞掉**：打印它的失败原因，继续处理剩下的，最后返回失败本数。
    迁移是可回滚的独立事务，一本失败不该让另外三本停在半路；
    但退出码必须非 0，否则脚本调用方会以为全成功了。
    """
    repo = os.path.abspath(repo or paths.repo_root())
    targets = discover_targets(repo, repo_root_id)
    if not targets:
        print("没有找到需要迁移的平铺工作目录（仓库根也不是一本工作目录）。")
        return 0
    print("批量迁移：找到 %d 本平铺工作目录（仓库根排最后）\n" % len(targets))
    failed = []
    for root, bid, at_repo in targets:
        tag = "（仓库根）" if at_repo else ""
        print("=" * 78)
        print("[%s] %s%s" % (bid, root, tag))
        p = plan(root, bid if at_repo else None, repo_root_id)
        if p["already"]:
            print("  已经是分层布局：%s" % p["dest_root"])
            continue
        for mv in p["moves"]:
            print("  %-28s -> %s" % (mv["from"], mv["to"]))
        if p["unknown"]:
            print("  未分类（**不会动**）：")
            for u in p["unknown"]:
                print("    " + u)
        print("  共 %d 项要搬。" % len(p["moves"]))
        if not apply_it:
            continue
        try:
            apply(root, bid if at_repo else None, repo_root_id=repo_root_id)
        except SystemExit as e:
            print("  ！这一本失败：%s" % (e.code if e.code else e))
            failed.append((root, e.code))
        print("")
    if not apply_it:
        print("=" * 78)
        print("真正执行加 --apply")
        return 0
    if failed:
        print("\n%d 本失败（其余已搬完，可对每一本单独 --rollback）：" % len(failed))
        for root, msg in failed:
            print("  %s\n    %s" % (root, msg))
        return 1
    print("全部完成。")
    return 0


def _locate(work):
    """--work 既可以是源目录（run-lacan），也可以是目标（runs/lacan 或 lacan）。

    源目录在迁移成功后已经被删掉了，回滚时还能用名字找到它——
    找不到就把原样交给 rollback()，让它报出「没有迁移记录」而不是「目录不存在」。
    """
    w = os.path.abspath(work)
    if os.path.isdir(w):
        return w
    cand = os.path.join(os.path.abspath(paths.runs_root()),
                        os.path.basename(w.rstrip("/\\")))
    return cand if os.path.isdir(cand) else w


def main():
    ap = argparse.ArgumentParser(description="旧的平铺工作目录 -> 分层布局（默认只预演）")
    ap.add_argument("--work", default="", help="工作目录（--all 时省略）")
    ap.add_argument("--all", action="store_true",
                    help="批量：仓库根 + 仓库下所有 run-*/（每本独立记录、独立回滚）")
    ap.add_argument("--book-id", default="",
                    help="交付物目录名（默认：run-xxx -> xxx；仓库根 -> hegel）")
    ap.add_argument("--repo-root-id", default=REPO_ROOT_BOOK_ID,
                    help="仓库根那本的 book id（默认 %s）" % REPO_ROOT_BOOK_ID)
    ap.add_argument("--apply", action="store_true", help="真的搬（不加就是预演）")
    ap.add_argument("--rollback", action="store_true", help="按 .migration-*.json 搬回去")
    ap.add_argument("--log", default="", help="迁移记录的路径")
    a = ap.parse_args()

    # 回滚时源目录可能已经被整个删掉（迁移成功就会删），所以**不能**先检查 --work 存在。
    # 允许直接写目标：--work runs/lacan 或 --work lacan。
    if a.rollback:
        if not a.work:
            sys.exit("--rollback 需要 --work（源目录或 runs/<book-id> 都行）")
        if a.all:
            sys.exit("--rollback 是逐本的：批量回滚请对每一本分别执行 --work <目录> --rollback")
        return rollback(_locate(a.work), a.log or None)
    if a.all:
        if a.work:
            sys.exit("--all 与 --work 不能同时给：--all 自己发现候选目录")
        return run_batch(a.apply, a.repo_root_id)
    if not a.work:
        sys.exit("要么给 --work <工作目录>，要么给 --all（批量）")
    if not os.path.isdir(a.work):
        sys.exit("工作目录不存在：%s" % a.work)
    p = plan(a.work, a.book_id or None, a.repo_root_id)
    _print_plan(p, os.path.abspath(a.work))
    if p["already"]:
        return 0
    if not a.apply:
        print("  真正执行加 --apply")
        return 0
    print("")
    return apply(a.work, a.book_id or None, a.log or None, a.repo_root_id)


if __name__ == "__main__":
    stdio.utf8_stdio()
    sys.exit(main())