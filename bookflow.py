#!/usr/bin/env python
"""bookflow — 扫描版 PDF → 电纸书 EPUB 的可复用工作流。

阶段各自可单独重跑：

  1. render : PDF → 300dpi 灰度 PNG           (pdftoppm)
  2. ocr    : PNG  → 行级 JSON（框+文本+置信度）(RapidOCR / PP-OCRv6)
  3. layout : JSON → 版面区域标签（脚注/页眉/页码）(rapid_layout / PP-DocLayoutV3)
  4. md     : JSON → 结构化 Markdown + 目录树 + [^id] 脚注 (layout.py)
  5. epub   : Markdown → EPUB3（弹窗脚注）     (epub3.py，项目自带)

用法示例：
  python bookflow.py all --pdf "D:/books/x.pdf" --work ./run1 \
      --title "书名" --author "作者" --publisher "出版社"

  python bookflow.py md --work ./run1 --title "书名"     # 只重排版
  python bookflow.py epub --work ./run1 --title "书名" --author "作者"
"""
import os
import sys
import json
import shutil
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PY = sys.executable

# 依赖自检。本机常见坑：PATH 上的 python 是 anaconda，而依赖装在另一个 venv 里，
# 于是子进程报一长串 CalledProcessError，完全看不出根因。这里提前自检并给人话提示。
#
# 按**阶段**列表而不是一个大元组：旧版对所有阶段都要求 rapidocr，导致
# 「md」阶段在没装 rapicocr 的解释器上被误拦——而 make_md.py 根本不 import 它。
# 同理 layout 阶段真正缺的是 rapid_layout，旧版没检查，于是用户看到的是
# run_layout.py 内部 ModuleNotFoundError 的 traceback。
BASE_PKGS = ("PIL", "numpy")          # cover / make_review / run_layout / repair 都用得上
STAGE_PKGS = {
    "render": (),
    "ocr": ("rapidocr",),
    "repair": ("rapidocr",),
    "layout": ("rapid_layout",),
    "md": (),                          # layout/qa/fix 纯标准库 + difflib
    "audit": (),                       # 需要词典，单独检查
    "disagree": (),                    # 同上
    "decisions": (),
    "epub": (),                        # epub3.py 纯标准库
    "prune": (),
    "all": ("rapidocr", "rapid_layout"),
}


def _importable(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


def check_env(stage, no_layout=False):
    """按阶段检查依赖；缺什么就说清楚缺什么、换哪个解释器。"""
    pkgs = list(BASE_PKGS) + list(STAGE_PKGS.get(stage, ()))
    if stage == "all" and no_layout:
        pkgs = [p for p in pkgs if p != "rapid_layout"]
    missing = [m for m in pkgs if not _importable(m)]
    if missing:
        # 用当前解释器的真实路径给提示，不写死某个 venv——换机器就失效了
        sys.exit("缺少依赖 %s（当前解释器：%s）。\n"
                 "请先安装依赖（pip install -r requirements.txt），或用装好依赖的\n"
                 "Python 解释器运行，例如：\n"
                 "  <装好依赖的 python> bookflow.py %s ..."
                 % (", ".join(missing), PY, stage))


def check_dict():
    """audit / disagree 需要 jieba 词典。

    旧版不检查，缺 jieba 时崩在 audit.py 的 load_dict：一行
    "TypeError: expected str, bytes or os.PathLike object, not NoneType"，
    完全看不出根因。
    """
    if os.path.exists(os.path.join(HERE, "dict.txt")):
        return
    try:
        import jieba  # noqa: F401
    except ImportError:
        sys.exit("审计/交叉验证阶段需要中文词表：pip install jieba，\n"
                 "或把 jieba 的 dict.txt 放到项目根目录 %s" % HERE)


def check_pdftoppm():
    if shutil.which("pdftoppm") is None:
        sys.exit("render 阶段需要 poppler 的 pdftoppm，请把它加进 PATH。")




def sh(cmd, **kw):
    print("  $ " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def stage_render(a):
    pages = os.path.join(a.work, "pages")
    os.makedirs(pages, exist_ok=True)
    if os.listdir(pages) and not a.force:
        print("  pages 已存在，跳过（--force 可重做）")
        return
    sh(["pdftoppm", "-r", str(a.dpi), "-gray", "-png", a.pdf, os.path.join(pages, "p")])
    print("  页数 %d" % len(os.listdir(pages)))


def stage_ocr(a):
    ocr = os.path.join(a.work, "ocr")
    os.makedirs(ocr, exist_ok=True)
    env = dict(os.environ)
    cmd = [PY, os.path.join(HERE, "run_ocr.py"),
           "--src", os.path.join(a.work, "pages"), "--dst", ocr,
           "--procs", str(a.procs), "--device", a.device, "--model", a.model]
    if a.force:
        cmd.append("--force")
    sh(cmd, env=env)


def stage_repair(a):
    """OCR 查漏补缺：整页识别会让异常长的行退化成几个乱字，这里逐行补识别。"""
    src = os.path.join(a.work, "ocr")
    if not os.path.isdir(src):
        sys.exit("repair 需要先跑 ocr 阶段：找不到 %s" % src)
    dst = os.path.join(a.work, "ocr_fix")
    cmd = [PY, os.path.join(HERE, "repair.py"),
           "--src", src, "--pages", os.path.join(a.work, "pages"),
           "--dst", dst, "--device", a.device]
    if a.force:
        cmd.append("--force")
    sh(cmd)


# OCR 结果目录的优先级：越靠前越"加工过"。ocr_merged 是 disagree/apply_decisions
# 回填过的结果，包含第二模型交叉验证的收益；ocr_layout 又在其上加了 region 标签。
# 这个顺序本身是对的，但**必须配合新鲜度告警**（见 ocr_dir）：
# 先跑 layout 再从 ocr_fix 回填 ocr_merged，顺序没变，选中的还是那份旧的 ocr_layout，
# 裁决成果会被静默丢掉——实测踩过。
OCR_DIRS = ("ocr_layout", "ocr_merged", "ocr_fix", "ocr")


def _dir_freshness(d):
    """目录里最新一个 json 的 mtime；空目录返回 None。"""
    try:
        ts = [os.path.getmtime(os.path.join(d, f))
              for f in os.listdir(d) if f.endswith(".json")]
    except OSError:
        return None
    return max(ts) if ts else None


def _describe_dirs(work):
    """把每个候选目录的状态列出来，失败时让人一眼看出缺哪一环。"""
    rows = []
    for name in OCR_DIRS:
        d = os.path.join(work, name)
        if not os.path.isdir(d):
            rows.append("[缺失] %s" % name)
        else:
            n = len([f for f in os.listdir(d) if f.endswith(".json")])
            rows.append("[空]" if not n else "[%d json]" % n)
            rows[-1] = "%s %s" % (rows[-1], name)
    return "  ".join(rows)


def ocr_dir(a):
    """按优先级挑 OCR 结果目录；显式 --ocr-src 优先。

    除优先级外还做**新鲜度**检查：如果选中的目录比顺位更低的目录还旧，
    说明中间做过回填（disagree/apply_decisions 写 ocr_merged）却没重跑 layout，
    此时必须告警——否则用户以为裁决生效了，实际 md 还在读旧数据。
    """
    if getattr(a, "ocr_src", ""):
        d = os.path.join(a.work, a.ocr_src)
        if not os.path.isdir(d):
            sys.exit("--ocr-src 指定的目录不存在: %s" % d)
        print("  OCR 源: %s（--ocr-src 指定）" % d)
        return d
    picked = None
    for name in OCR_DIRS:
        d = os.path.join(a.work, name)
        if not os.path.isdir(d):
            continue
        jsons = [f for f in os.listdir(d) if f.endswith(".json")]
        if not jsons:
            print("  警告：%s 是空目录，跳过" % d)
            continue
        picked = (name, d)
        break
    if picked is None:
        sys.exit("work 目录下没有可用的 OCR 结果（需要 %s 之一）：%s\n  现状：%s"
                 % ("/".join(OCR_DIRS), a.work, _describe_dirs(a.work)))

    name, d = picked
    mine = _dir_freshness(d)
    for other in OCR_DIRS[OCR_DIRS.index(name) + 1:]:
        od = os.path.join(a.work, other)
        if not os.path.isdir(od):
            continue
        theirs = _dir_freshness(od)
        if mine and theirs and theirs > mine:
            import datetime
            fmt = lambda t: datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
            print("  警告：%s 比 %s 旧（%s < %s），仍按优先级选中 %s。\n"
                  "        若刚回填过裁决，请先重跑 layout，或用 --ocr-src %s"
                  % (name, other, fmt(mine), fmt(theirs), name, other))
            break
    print("  OCR 源: %s（按优先级）" % d)
    return d


def stage_layout(a):
    """版面分析：给 OCR 结果补 region 标签（脚注/页眉/页码），让正文更干净。

    它是**可选增强**：不跑这一步也能出书，只是脚注会混进正文。
    输出到 ocr_layout/，因为它优先级最高，md 阶段会自动选中。
    """
    src = ocr_dir(a)
    # 源不能是 ocr_layout 自己：那是上一次本阶段的输出，读它等于原地空转，
    # 而且会让 ocr_layout 的 mtime 永远最新、永久压住更新的 ocr_merged。
    if os.path.basename(os.path.normpath(src)) == "ocr_layout":
        for name in OCR_DIRS[1:]:
            cand = os.path.join(a.work, name)
            if os.path.isdir(cand) and [f for f in os.listdir(cand) if f.endswith(".json")]:
                print("  布局源改用 %s（避免读自己上一次的输出）" % cand)
                src = cand
                break
    dst = os.path.join(a.work, "ocr_layout")
    cmd = [PY, os.path.join(HERE, "run_layout.py"),
           "--src", src, "--pages", os.path.join(a.work, "pages"),
           "--dst", dst, "--device", a.device]
    if a.force:
        cmd.append("--force")
    sh(cmd)


def stage_md(a):
    src = ocr_dir(a)
    sh([PY, os.path.join(HERE, "make_md.py"),
        "--src", src, "--out", os.path.join(a.work, "out"),
        "--title", a.title, "--author", a.author, "--publisher", a.publisher])


def stage_audit(a):
    """审核：列出"字数正常但字认错"的可疑处，并生成可批量核对的对照图。"""
    out = os.path.join(a.work, "out")
    if not os.path.exists(os.path.join(out, "book.md")):
        sys.exit("audit 需要先跑 md 阶段：找不到 %s" % os.path.join(out, "book.md"))
    sh([PY, os.path.join(HERE, "audit.py"),
        "--md", os.path.join(out, "book.md"),
        "--out", os.path.join(out, "audit.txt"),
        "--json", os.path.join(out, "audit.json"),
        "--report", os.path.join(out, "report.json")])
    js = os.path.join(out, "audit.json")
    if os.path.exists(js) and os.path.getsize(js) > 2:
        sh([PY, os.path.join(HERE, "make_review.py"),
            "--audit", js, "--ocr", ocr_dir(a),
            "--pages", os.path.join(a.work, "pages"),
            "--out", os.path.join(out, "review.png"), "--n", "40"])
    print("  审核清单 -> out/audit.txt，对照图 -> out/review.png（不自动改，需人工/VLM 裁决）")


def stage_epub(a):
    """Markdown → EPUB3。用项目自带的 epub3.py，不用外部脚本。

    外部的 md-to-ebook / pandoc 也能出 EPUB，但两者都把脚注渲染成"章末列表"：
    正文里是 <sup>1</sup> 链接，注文排在章尾，读者要来回翻。EPUB3 有专门的
    弹窗语义（noteref → aside[epub:type=footnote]），Apple Books / Thorium /
    Kobo 会直接浮层显示，必须自己拼 XML 才能用上。
    """
    md = os.path.join(a.work, "out", "book.md")
    if not os.path.exists(md) or os.path.getsize(md) == 0:
        sys.exit("epub 需要非空的 book.md：%s" % md)
    # 元数据回落：md 阶段已把 title/author/publisher 写进 report.json，
    # 这里在命令行没给的时候捡回来——重打包不必每次重新敲作者和出版社。
    book = {}
    rep_p = os.path.join(a.work, "out", "report.json")
    if os.path.exists(rep_p):
        try:
            with open(rep_p, encoding="utf-8") as f:
                book = (json.load(f) or {}).get("book") or {}
        except (ValueError, OSError):
            book = {}
    title = a.title or book.get("title") or "未命名"
    author = a.author or book.get("author") or ""
    publisher = a.publisher or book.get("publisher") or ""
    out = os.path.join(a.work, "out", title + ".epub")

    # 封面先备好，再一次性交给 epub3.py 打包（避免二次改写 zip）
    cover_jpg = os.path.join(a.work, "out", "cover.jpg")
    if a.cover and os.path.exists(a.cover):
        cover_jpg = a.cover
    elif not os.path.exists(cover_jpg):
        first = os.path.join(a.work, "pages", "p-001.png")
        if os.path.exists(first):
            subprocess.run([PY, os.path.join(HERE, "cover.py"), "--page", first,
                            "--jpg", cover_jpg], check=False)

    cmd = [PY, os.path.join(HERE, "make_epub.py"),
           "--md", md, "--out", out,
           "--title", title,
           "--toc-depth", str(a.toc_depth)]
    if author:
        cmd += ["--author", author]
    if publisher:
        cmd += ["--publisher", publisher]
    if os.path.exists(cover_jpg):
        cmd += ["--cover", cover_jpg]
    sh(cmd)
    print("  成品 %s (%.1f KB)" % (out, os.path.getsize(out) / 1024))


def stage_disagree(a):
    """双模型交叉比对：找出两版 OCR 不一致的地方（分歧点 ≈ 候选错误点）。

    **刻意不放进 all**：它需要第二版 OCR（ocr_alt），不是每本书都有；
    它带 --apply 会改写回填目录，塞进 all 会改变 md 的输入源、破坏回归基线；
    它还需要 jieba 词表，缺依赖时会让跑了半小时 OCR 的全流程死在最后一步。
    """
    check_dict()
    out = os.path.join(a.work, "out")
    os.makedirs(out, exist_ok=True)
    src = ocr_dir(a)
    alt = os.path.join(a.work, a.ocr_alt) if a.ocr_alt else ""
    if not alt or not os.path.isdir(alt):
        sys.exit("disagree 需要第二版 OCR 目录：用 --ocr-alt 指定子目录（相对 --work）。\n"
                 "  现状：%s" % _describe_dirs(a.work))
    sh([PY, os.path.join(HERE, "disagree.py"),
        "--a", src, "--b", alt,
        "--out", os.path.join(out, "disagree.json"),
        "--txt", os.path.join(out, "disagree.txt")])
    print("  分歧明细 -> out/disagree.json；用 bookflow.py decisions --from disagree 转成裁决表")


def stage_decisions(a):
    """把各路候选（disagree / audit / 旧 review_queue）统一成 decisions.json。

    裁决表是 apply_decisions 的唯一输入。这里只做格式归一，**不做任何裁决**。
    """
    import decisions as dec
    out = os.path.join(a.work, "out")
    os.makedirs(out, exist_ok=True)
    dst = a.out or os.path.join(out, "decisions.json")
    src_map = {
        "disagree": os.path.join(out, "disagree.json"),
        "audit": os.path.join(out, "audit.json"),
        "queue": os.path.join(out, "review_queue.json"),
    }
    p = src_map[a.from_]
    if not os.path.exists(p):
        sys.exit("找不到候选来源 %s（先跑 %s 阶段）" % (p, a.from_))
    picked = dec.load_any(p, kind=a.from_,
                          report=os.path.join(out, "report.json"),
                          ocr=ocr_dir(a))
    if a.merge and os.path.exists(dst):
        picked = dec.merge(dec.read(dst), picked)
    dec.write(dst, picked)
    n = len(picked["decisions"])
    print("  裁决表 -> %s（%d 条，来源 %s）" % (dst, n, a.from_))
    print("  下一步：python apply_decisions.py --work %s --decisions %s [--apply]" % (a.work, dst))


def stage_prune(a):
    """删掉体积大、可重生成的中间产物（pages/ 占绝大部分）。

    一本 300 页的书 pages/ 就有 450 MB，五份 OCR 结果 8 MB。成书交付后
    中间产物没必要长期留着；但 md/epub 阶段重跑要靠它们，所以只清理指定项。
    """
    targets = {
        "pages": 450,
        "ocr": 2, "ocr_fix": 2, "ocr_alt": 2, "ocr_medium": 2, "ocr_merged": 2,
    }
    want = a.keep.split(",") if a.keep else []
    freed = 0.0
    for name, mb in targets.items():
        if name in want:
            continue
        d = os.path.join(a.work, name)
        if not os.path.isdir(d):
            continue
        size = sum(os.path.getsize(os.path.join(d, f))
                   for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))
        shutil.rmtree(d)
        freed += size / 1024 / 1024
        print("  删除 %-12s %.1f MB" % (name, size / 1024 / 1024))
    print("  共释放 %.1f MB（保留：%s）" % (freed, ", ".join(want) or "无"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["render", "ocr", "repair", "layout", "md", "audit",
                                      "epub", "all", "prune", "disagree", "decisions"])
    ap.add_argument("--pdf", help="源 PDF")
    ap.add_argument("--work", required=True, help="工作目录")
    ap.add_argument("--title", default="")
    ap.add_argument("--author", default="")
    ap.add_argument("--publisher", default="")
    ap.add_argument("--cover", default="", help="封面图路径（留空则用书中图片）")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--procs", type=int, default=5)
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "dml", "cuda"],
                    help="OCR 设备：auto(有 GPU 就用) / cpu / dml / cuda")
    ap.add_argument("--model", default="small", choices=["small", "medium", "server"],
                    help="识别模型档位；中文印刷书 small 实测优于 medium")
    ap.add_argument("--toc-depth", type=int, default=3)
    ap.add_argument("--ocr-src", default="",
                    help="指定 OCR 结果子目录（相对 --work）。留空则按 "
                         "ocr_layout > ocr_merged > ocr_fix > ocr 自动挑")
    ap.add_argument("--force", action="store_true",
                    help="重跑已完成的阶段（含 OCR：会重新识别已存在的页）")
    ap.add_argument("--keep", default="",
                    help="prune 阶段保留的中间目录，逗号分隔，如 pages,ocr_merged")
    ap.add_argument("--no-layout", action="store_true",
                    help="all 阶段跳过版面分析（脚注会混进正文，但速度快 1.8 分钟/350页）")
    ap.add_argument("--ocr-alt", default="",
                    help="第二版 OCR 结果子目录（相对 --work），disagree 阶段必需，如 ocr_alt。"
                         "它**不参与** OCR_DIRS 选源竞争，只作 --b 侧输入")
    ap.add_argument("--from", dest="from_", default="disagree",
                    choices=["disagree", "audit", "queue"],
                    help="decisions 阶段的候选来源")
    ap.add_argument("--out", default="", help="decisions 阶段的输出路径")
    ap.add_argument("--merge", action="store_true",
                    help="decisions 阶段合并进已有 decisions.json（按 page@y0 去重）")
    a = ap.parse_args()

    os.makedirs(a.work, exist_ok=True)
    if a.stage in ("render", "ocr", "all") and not a.pdf:
        sys.exit("render/ocr 阶段需要 --pdf")
    if a.stage == "render":
        check_pdftoppm()
    check_env(a.stage, no_layout=a.no_layout)
    if a.stage == "all":
        stage_render(a)
        stage_ocr(a)
        stage_repair(a)
        if not a.no_layout:
            stage_layout(a)     # 可选增强：脚注/页眉/页码分流
        stage_md(a)
        stage_audit(a)      # 只出清单，不改文本
        stage_epub(a)
        # disagree 不自动跑（需要第二版 OCR），但检出时提醒一声，否则这条通道等于不存在
        if os.path.isdir(os.path.join(a.work, "ocr_alt")):
            print("\n  提示：检出 ocr_alt，可跑双模型交叉验证：\n"
                  "    python bookflow.py disagree --work %s --ocr-alt ocr_alt" % a.work)
        return
    {"render": stage_render, "ocr": stage_ocr, "repair": stage_repair,
     "layout": stage_layout, "md": stage_md, "audit": stage_audit,
     "epub": stage_epub, "prune": stage_prune,
     "disagree": stage_disagree, "decisions": stage_decisions}[a.stage](a)


if __name__ == "__main__":
    main()
