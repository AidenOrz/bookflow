"""bookflow.paths — 工作目录的分层布局与「角色 → 路径」解析。

## 为什么需要这一层

现状（docs/IO-CONVENTIONS.md §1 I4、§2 O1/O6）是**仓库根同时是工作目录**：
pages/ ocr/ ocr_fix/ ocr_alt/ ocr_medium/ ocr_merged/ ocr_layout/
ocr_text/ ocr_text_raw/ out/ 十个目录全部平铺在第一层，而 out/ 里又混着
四类语义完全不同的产物（交付物 / 机器报告 / 人工件 / 可重生成的中间态）。

这不是审美问题。IO-CONVENTIONS 记录了两条实测后果：

* **删不掉**：想只清理中间产物，必须人工挑；想只拷交付物给读者，也必须人工挑。
  prune 的 targets 里没有 out/，因为 book.md（重打包 EPUB 的唯一输入）就在里面。
* **分不清**：decisions.json 是全项目**唯一不可重建**的产物，
  却和 disagree.json（脚本产物，删了重跑即可）并排躺在同一层。

所以引入三种布局，用**同一个解析入口**取路径，调用方只写角色名不写目录名：

    tiered（新，默认用于新书）
      <run>/manifest.json        ← 这次是怎么跑出来的
      <run>/work/pages|ocr*|.backup/
      <run>/report/book.md|report.json|audit.*|review.png|images/
      <run>/review/decisions.json|notes.md
      交付物出到 <repo>/dist/<book-id>/

    flat（旧，四个既有工作目录，永远可读）
      <run>/pages|ocr*|out/
      <run>/out/decisions.json

    legacy-root（仓库根那本黑格尔，等同于 flat，但 <run> 就是仓库根）

## 判定规则（宁可只读不改，也不猜）

    tiered  := <run>/manifest.json 存在，或 <run>/work 或 <run>/report 或 <run>/review 存在
    flat    := 其余

**判定只看目录是否出现，不看内容是否为空。** 这条与 O5 的教训一致：
run-sengguan/ocr_text 只剩一个 _textlayer.meta，目录存在但一个页 JSON 都没有——
「目录存在」不能当「阶段跑完」，那是 manifest 的事，不是布局的事。
这里只回答「文件该放哪」，不回答「跑没跑完」。
"""
import os

# 布局常量：拼字符串的地方一律引这里，别再散写 "work"/"report"/"review"。
TIER_WORK = "work"
TIER_REPORT = "report"
TIER_REVIEW = "review"
LAYOUT_TIERED = "tiered"
LAYOUT_FLAT = "flat"

# 中间产物目录：全部落在 <run>/work/ 下，prune 阶段可以整片删。
WORK_DIRS = ("pages", "ocr", "ocr_fix", "ocr_alt", "ocr_medium", "ocr_merged",
             "ocr_layout", "ocr_text", "ocr_text_raw")

# 四个既有工作目录里出现过的目录名。用于识别「这是不是一棵旧的平铺工作目录」。
FLAT_MARKERS = ("pages", "out", "ocr", "ocr_fix", "ocr_layout", "ocr_merged",
                "ocr_text", "ocr_text_raw", "ocr_alt", "ocr_medium")

# 本模块就放在仓库根，所以仓库根 == 模块所在目录。
# （写成 dirname(dirname(...)) 会指到仓库的**上一级**——实测第一次就是这么错的，
#  于是 runs 发现把 E:\workspace 当成了仓库根。）
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def repo_root():
    """仓库根：本模块所在目录。dist/ input/ runs/ 都相对它。"""
    return REPO_ROOT


def is_tiered(run_root):
    """这个工作目录是不是分层布局。

    **只看三个目录在不在，不看 manifest.json。** 一开始把 manifest 也算进来，
    结果是「在 flat 目录上跑一次 any 阶段 → 写下 manifest.json → 整棵树立刻被
    判成 tiered → 下一个阶段去 work/ 找 pages/，找不到」。

    实测后果：run-lacan 跑一次 md 之后，它的 out/ 目录整个"消失"了——
    其实是路径被解析到 report/ 去了，而 report/ 是空的。这比丢文件更难查，
    因为**每一步的退出码都是 0**。

    manifest 一律写在 run 根（两种布局都一样），所以它不能参与布局判定。
    """
    run_root = os.path.abspath(run_root)
    return any(os.path.isdir(os.path.join(run_root, t))
               for t in (TIER_WORK, TIER_REPORT, TIER_REVIEW))


def layout_of(run_root):
    return LAYOUT_TIERED if is_tiered(run_root) else LAYOUT_FLAT


def work_dir(run_root, create=False):
    """中间产物根：tiered -> <run>/work；flat -> <run>（平铺，保持旧行为）"""
    if is_tiered(run_root):
        p = os.path.join(os.path.abspath(run_root), TIER_WORK)
        if create:
            os.makedirs(p, exist_ok=True)
        return p
    return os.path.abspath(run_root)


def report_dir(run_root, create=False):
    """机器报告根：tiered -> <run>/report；flat -> <run>/out"""
    if is_tiered(run_root):
        p = os.path.join(os.path.abspath(run_root), TIER_REPORT)
        if create:
            os.makedirs(p, exist_ok=True)
        return p
    return os.path.join(os.path.abspath(run_root), "out")


def review_dir(run_root, create=False):
    """人工件根：tiered -> <run>/review；flat -> <run>/out（旧位置，不能换）"""
    if is_tiered(run_root):
        p = os.path.join(os.path.abspath(run_root), TIER_REVIEW)
        if create:
            os.makedirs(p, exist_ok=True)
        return p
    return os.path.join(os.path.abspath(run_root), "out")


def ocr_dir(run_root, name, create=False):
    """某个 OCR 产物目录（name 取 WORK_DIRS 里的值）。"""
    p = os.path.join(work_dir(run_root, create), name)
    if create:
        os.makedirs(p, exist_ok=True)
    return p


def pages_dir(run_root, create=False):
    return ocr_dir(run_root, "pages", create)


def backup_dir(run_root, create=False):
    """备份根：<work>/.backup/。

    旧版落在 <src>/.bak/，即**被备份的目录内部**（IO-CONVENTIONS §2 O10）：
    备份成了数据目录的子目录，_dir_freshness 之类只看顶层的统计完全看不见它，
    而 prune 删掉整个 ocr_merged 时会连备份一起删（§9.5 实测过）。
    挪到 work/.backup/ 之后，数据目录里只剩页文件，备份活过一次 prune。
    """
    p = os.path.join(work_dir(run_root), ".backup")
    if create:
        os.makedirs(p, exist_ok=True)
    return p


def manifest_path(run_root):
    """manifest.json 一律在工作目录根，两种布局相同（平铺布局不会撞名）。"""
    return os.path.join(os.path.abspath(run_root), "manifest.json")


def decisions_path(run_root):
    """裁决表：tiered -> review/decisions.json；flat -> out/decisions.json。"""
    return os.path.join(review_dir(run_root), "decisions.json")


def images_dir(run_root, create=False):
    """插图裁图目录：始终跟着报告目录走（book.md 用相对路径 images/ 引用它）。"""
    p = os.path.join(report_dir(run_root), "images")
    if create:
        os.makedirs(p, exist_ok=True)
    return p


def book_md(run_root):
    return os.path.join(report_dir(run_root), "book.md")


def dist_dir(book_id=None, create=False):
    """交付物目录 <repo>/dist[/<book-id>]。"""
    p = os.path.join(repo_root(), "dist")
    if book_id:
        p = os.path.join(p, book_id)
    if create:
        os.makedirs(p, exist_ok=True)
    return p


def input_dir(book_id=None, create=False):
    """输入目录 <repo>/input[/<book-id>]。PDF 放这里，不入库。"""
    p = os.path.join(repo_root(), "input")
    if book_id:
        p = os.path.join(p, book_id)
    if create:
        os.makedirs(p, exist_ok=True)
    return p


def runs_root(create=False):
    p = os.path.join(repo_root(), "runs")
    if create:
        os.makedirs(p, exist_ok=True)
    return p


def describe(run_root):
    """人话描述这个工作目录的布局与关键路径，给日志和 WebUI 用。"""
    run_root = os.path.abspath(run_root)
    return {
        "run": run_root,
        "layout": layout_of(run_root),
        "work": work_dir(run_root),
        "report": report_dir(run_root),
        "review": review_dir(run_root),
        "decisions": decisions_path(run_root),
        "manifest": manifest_path(run_root),
    }


def ensure_tiered(run_root):
    """把一棵工作目录变成 tiered 骨架（只建目录，**不搬文件**）。

    搬文件由 tools/migrate_layout.py 做：它要能回滚、要能逐项核对体积与页数，
    不适合藏在一个每次调用都会被 import 的函数里。
    """
    run_root = os.path.abspath(run_root)
    for t in (TIER_WORK, TIER_REPORT, TIER_REVIEW):
        os.makedirs(os.path.join(run_root, t), exist_ok=True)
    return run_root
