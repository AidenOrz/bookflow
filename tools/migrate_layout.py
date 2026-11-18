#!/usr/bin/env python
"""把旧的平铺工作目录迁移成分层布局（tiered），**可回滚、可预演**。

    python tools/migrate_layout.py --work run-lacan            # 预演：只打印要搬什么
    python tools/migrate_layout.py --work run-lacan --apply     # 真搬
    python tools/migrate_layout.py --work run-lacan --rollback  # 按记录搬回去

## 为什么需要它，以及为什么默认不跑

paths.py 已经让**两种布局并存**：旧目录（pages/ out/ 直接躺第一层）照常能跑，
新书（runs/<id>/ 下的 work/ report/ review/）用新布局。所以迁移不是必须的——
它只解决 IO-CONVENTIONS §1 I4 说的那件事：仓库根同时是工作目录，
于是 .gitignore 要写一长串裸名规则，而"想只删中间产物"和"想只拷交付物"
都必须人工挑。

**默认预演，不 --apply 就一个字节都不动。** 理由是这批目录里有
run-lacan 的 2825 条裁决表（全项目唯一不可重建的产物）和四本黄金 book.md；
搬错一次没有第二次机会。

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

## 回滚

--apply 会先写一份 <work>/.migration-<ts>.json 记录每一步（源、目标、是否移动过），
--rollback 读它反向搬回去。搬完不删这份记录：它是"这批文件曾经在哪"的唯一证据。
"""
import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import jsonio        # noqa: E402
import paths         # noqa: E402
import stdio         # noqa: E402

# 中间的 OCR 目录：整体搬进 work/，内容不动（页级 JSON 里没有任何绝对路径依赖）
WORK_MOVES = ("pages", "ocr", "ocr_fix", "ocr_alt", "ocr_medium", "ocr_merged",
              "ocr_layout", "ocr_text", "ocr_text_raw")

# out/ 里属于「机器报告」的文件：脚本产出、可重生成、删了重跑即可
REPORT_FILES = ("book.md", "report.json", "qa.txt", "audit.txt", "audit.json",
                "disagree.txt", "disagree.json", "review.png", "review_queue.json",
                "cover.jpg")

# out/ 里属于「人工件」的：只有人写过，脚本只读。**永远不能自动重建**
REVIEW_FILES = ("decisions.json", "校对清单.md", "notes.md")

# 交付物：离开工作目录，进仓库级 dist/<book-id>/
DELIVERABLE_EXT = (".epub",)


def _rel(run_root, p):
    return os.path.relpath(p, run_root).replace("\\", "/")


def plan(run_root, book_id=None):
    """算出要做的每一步。返回 {"moves": [...], "unknown": [...], "book_id": ...}。"""
    run_root = os.path.abspath(run_root)
    book_id = book_id or os.path.basename(run_root)
    moves, unknown = [], []

    if paths.is_tiered(run_root):
        return {"already": True, "moves": [], "unknown": [], "book_id": book_id}

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
                              "to": "dist/%s/%s" % (book_id, f), "dist": True})
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
    return {"already": False, "moves": moves, "unknown": unknown, "book_id": book_id}


def _target_abs(run_root, to):
    """把 plan 里的 "to" 解析成绝对路径（dist/ 是相对仓库根的）。"""
    if to.startswith("dist/"):
        return os.path.join(paths.repo_root(), to.replace("/", os.sep))
    return os.path.join(run_root, to.replace("/", os.sep))


def apply(run_root, book_id=None, log_path=None):
    """执行迁移。**每一步都记进日志**，失败即停并提示怎么回滚。"""
    run_root = os.path.abspath(run_root)
    p = plan(run_root, book_id)
    if p["already"]:
        print("  已经是分层布局：%s" % run_root)
        return 0
    log = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "run": run_root,
           "book_id": p["book_id"], "moves": [], "created": []}
    # 先建骨架目录，回滚时按 created 逆序删空目录
    for t in (paths.TIER_WORK, paths.TIER_REPORT, paths.TIER_REVIEW):
        d = os.path.join(run_root, t)
        if not os.path.isdir(d):
            os.makedirs(d)
            log["created"].append(t)

    n = 0
    for mv in p["moves"]:
        src = os.path.join(run_root, mv["from"].replace("/", os.sep))
        dst = _target_abs(run_root, mv["to"])
        if not os.path.exists(src):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            sys.exit("目标已存在，拒绝覆盖：%s\n  这说明目录里已有分层布局的产物，"
                     "迁移必须人工判断保留哪一份。" % dst)
        shutil.move(src, dst)
        log["moves"].append({"from": mv["from"], "to": mv["to"]})
        n += 1
        print("  %-28s -> %s" % (mv["from"], mv["to"]))

    # out/ 只有搬空之后才删：还留着未分类文件时必须留下来
    out = os.path.join(run_root, "out")
    if os.path.isdir(out) and not os.listdir(out):
        os.rmdir(out)
        log["created"].append("out(removed)")

    log_path = log_path or os.path.join(run_root, ".migration-%s.json"
                                        % time.strftime("%Y%m%d-%H%M%S"))
    jsonio.write(log_path, log, indent=1)
    print("\n  搬了 %d 项；回滚记录 -> %s" % (n, log_path))
    if p["unknown"]:
        print("  未分类（**没动**，请人工决定）：")
        for u in p["unknown"]:
            print("    " + u)
    print("  下一步：python bookflow.py md --work %s   （验证仍能出书）" % run_root)
    print("  回滚：python tools/migrate_layout.py --work %s --rollback" % run_root)
    return 0


def rollback(run_root, log_path=None):
    """按日志反向搬回。找不到日志就明确报错，不猜。"""
    run_root = os.path.abspath(run_root)
    if not log_path:
        cands = sorted(f for f in os.listdir(run_root) if f.startswith(".migration-"))
        if not cands:
            sys.exit("找不到迁移记录 .migration-*.json，无法回滚（不会凭猜测搬文件）")
        log_path = os.path.join(run_root, cands[-1])
    log = jsonio.read(log_path)
    n = 0
    for mv in reversed(log.get("moves") or []):
        src = _target_abs(run_root, mv["to"])
        dst = os.path.join(run_root, mv["from"].replace("/", os.sep))
        if not os.path.exists(src):
            print("  跳过（源已不在）：%s" % mv["to"])
            continue
        if os.path.exists(dst):
            sys.exit("回滚目标已存在，拒绝覆盖：%s" % dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        n += 1
        print("  %-40s -> %s" % (mv["to"], mv["from"]))
    # 删掉迁移时新建的空目录（逆序）
    for t in reversed(log.get("created") or []):
        d = os.path.join(run_root, t)
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
    print("\n  回滚了 %d 项（记录保留：%s）" % (n, log_path))
    return 0


def main():
    ap = argparse.ArgumentParser(description="旧的平铺工作目录 -> 分层布局（默认只预演）")
    ap.add_argument("--work", required=True, help="工作目录")
    ap.add_argument("--book-id", default="", help="交付物目录名（默认取工作目录名）")
    ap.add_argument("--apply", action="store_true", help="真的搬（不加就是预演）")
    ap.add_argument("--rollback", action="store_true", help="按 .migration-*.json 搬回去")
    ap.add_argument("--log", default="", help="迁移记录的路径")
    a = ap.parse_args()

    if not os.path.isdir(a.work):
        sys.exit("工作目录不存在：%s" % a.work)
    if a.rollback:
        return rollback(a.work, a.log or None)
    p = plan(a.work, a.book_id or None)
    if p["already"]:
        print("已经是分层布局：%s" % os.path.abspath(a.work))
        return 0
    print("预演（不会改动任何文件）：%s" % os.path.abspath(a.work))
    for mv in p["moves"]:
        print("  %-28s -> %s" % (mv["from"], mv["to"]))
    if p["unknown"]:
        print("\n  未分类（**不会动**）：")
        for u in p["unknown"]:
            print("    " + u)
    print("\n  共 %d 项要搬。" % len(p["moves"]))
    if not a.apply:
        print("  真正执行加 --apply")
        return 0
    print("")
    return apply(a.work, a.book_id or None, a.log or None)


if __name__ == "__main__":
    stdio.utf8_stdio()
    sys.exit(main())
