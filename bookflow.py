#!/usr/bin/env python
"""bookflow — 扫描版 PDF → 电纸书 EPUB 的可复用工作流。

阶段各自可单独重跑：

  0. textlayer : PDF → 行级 JSON（直接取 PDF 自带文字层）(textlayer.py)
  1. render : PDF → 300dpi 灰度 PNG           (pdftoppm)
  2. ocr    : PNG  → 行级 JSON（框+文本+置信度）(RapidOCR / PP-OCRv6)
  3. layout : JSON → 版面区域标签（脚注/页眉/页码）(rapid_layout / PP-DocLayoutV3)
  4. md     : JSON → 结构化 Markdown + 目录树 + [^id] 脚注 (layout.py)
  5. epub   : Markdown → EPUB3（弹窗脚注）     (epub3.py，项目自带)
  +. verify : 文字层 × OCR 交叉验证，分歧进 decisions 候选 (disagree.py)

**文字层**（PDF 自带的 text layer）是 OCR 的一条捷径，但**质量参差不齐**，
所以有三种模式（--text-layer）：

  verify（默认） 抽文字层 → 与 OCR 交叉验证 → 正文用文字层（它没有识别错误）；
                 交叉验证的分歧走 decisions 闭环人工裁决
  only           只抽文字层，完全不跑 OCR（快 40 倍），给确认有完好文字层的书
  off            完全不碰文字层，行为与本功能上线前**逐字节一致**
                 （纯扫描书 / 想复现旧结果时用）

用法示例：
  python bookflow.py all --pdf "D:/books/x.pdf" --work ./run1 \
      --title "书名" --author "作者" --publisher "出版社"

  python bookflow.py md --work ./run1 --title "书名"     # 只重排版
  python bookflow.py epub --work ./run1 --title "书名" --author "作者"
"""
import os
import re
import sys
import json
import shutil
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import jsonio                       # noqa: E402  页级 JSON 统一读写（项目自带）
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
    "textlayer": (),                   # 只要 poppler 的 pdftotext，textlayer.py 自己会检查
    "verify": (),                      # 复用 disagree.py（需 jieba，单独用 check_dict 查）
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


def check_pdftotext():
    """文字层走 poppler 的 pdftotext（与 pdftoppm 同一个包，通常一起装好）。"""
    if shutil.which("pdftotext") is None:
        sys.exit("文字层（--text-layer）需要 poppler 的 pdftotext，请把它加进 PATH。\n"
                 "  只要 OCR 的话用 --text-layer off 即可跳过对它的依赖。")


# ---------------------------------------------------------------------------
# 文字层（PDF 自带 text layer）
#
# 三条实测事实决定了下面的阈值与模式设计（本机 poppler 26.09，300 dpi）：
#
#   1. 有文字层的书，一页正常正文是 **200~1400 个汉字**：桌面 111 个 PDF 里抽 24 个
#      测前 10 页，纸质书的典型值是 205 / 237 / 253 / 275 / 481 / 1072 汉字/页。
#   2. 纯扫描件是 **0 汉字/页**（pdftotext 返回空）。本机实测 7 个扫描书全是 0。
#   3. 介于两者之间的是「残缺文字层」：只有页眉/页码/水印的 10 汉字/页
#      （中国僧官制度史）、或只在少数几页有文字的 35~50 汉字/页。
#
# 所以把「每页 50 个汉字」当**下限**而不是上限：低于它，文字层要么完全没有、
# 要么只剩边角料，拿它当正文会让整本书变成弃品，必须落回 OCR。
# 用汉字数而不是字符数：英文/纯符号页在本项目里不是正文（书都是中文书）。
# ---------------------------------------------------------------------------

# 一页正文至少有这么多汉字才算「这页有可用文字层」。低于此值该页视为空页。
TEXTLAYER_MIN_CJK_PER_PAGE = 50
# 全书平均每页汉字数低于它 → 文字层不可用（纯扫描件实测 0，残缺文字层实测 10）。
TEXTLAYER_MIN_AVG_CJK = 50
# 「有正文的页」至少占这么大比例，否则即便平均值够也判不可用。
# 这一条防的是**平均值骗人**：实测《SOC-V8-CN》平均 86.9 汉字/页（过了均值线），
# 但只有 30% 的页有字——它是扫描件插了几页数字页，拿它当正文会让七成页消失。
#
# 定 0.95（"几乎整本都有文字层"）而不是一个宽松值，有两条理由：
#
#   ① 实测分布是**双峰**的，中间地带几乎为空（桌面 111 个 PDF 抽样，前 10 页/本）：
#      ≥ 95%：《主义主义》100%、《谢林解读》90%→实测 8/9 有字、《2020-ACG》90%；
#      < 50%：《中国国家治理》40%、《SOC-V8》30%、《杨继绳》10%、
#           《中国僧官制度史》0%（只有页眉页码）。
#      也就是说一本书要么"基本整本都是文本 PDF"，要么"基本是扫描件"，
#      界划在 0.5 还是 0.95 对大多数书没有区别。
#
#   ② 更关键的是**不混源**：文字层按 y 聚类分行（3pt 容差），RapidOCR 按检测框出行，
#      两套分行口径不同，行高/左边界/行距的分布都不一样。layout.py 要做**跨页合段**
#      （判断上一页末行和下一页首行是不是同一段），整本书混着两种口径时，
#      页边界正好落在混源处就会误合或漏合。整本同源则完全避开这个风险。
#      这也与项目既有结论一致：跨页判据只敢用标点、不敢引入缩进，就是因为
#      "左右边界逐页独立计算"这类似的口径漂移会把标题并进正文（见 docs/architecture.md）。
#
# 代价是"只有部分页有文字层"的书用不上文字层——它们本来就得跑 OCR，走的是
# 已经逐字节验证过的老路径，不会更差。
TEXTLAYER_MIN_PAGE_RATIO = 0.95

TEXTLAYER_MODES = ("verify", "only", "off")

# 文字层的两个目录：
#   ocr_text_raw : textlayer.py 的原样输出（页数守恒，没文字的页是空 lines）
#   ocr_text     : 按页筛过、只留"这页真有正文"的页 —— 只有它参与 ocr_dir 选源。
# 分成两份是因为两者语义不同：raw 用来诊断"哪几页没有文字层"，ocr_text 用来出书。
# 若让 ocr_text 直接放全量，含空页的书会被选中当正文源，没字的那几页直接消失。
TEXTLAYER_DIR = "ocr_text"
TEXTLAYER_RAW_DIR = "ocr_text_raw"

# 文字层运行的元数据（不是页级 JSON，所以故意不用 .json 后缀，免得被当成一页）。
TEXTLAYER_META = "_textlayer.meta"


def textlayer_meta(work):
    """读 <work>/ocr_text/_textlayer.meta；不存在或坏了返回 {}。"""
    p = os.path.join(work, TEXTLAYER_DIR, TEXTLAYER_META)
    if not os.path.exists(p):
        return {}
    try:
        return jsonio.read(p) or {}
    except (ValueError, OSError):
        return {}


# 汉字区间：基本区 + 扩展 A。用汉字数而不是字符数——英文/纯符号页在本项目里
# 不是正文（这几本都是中文书），而且中文字符密度才是判断"这页有没有正文"的稳定口径。
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def page_cjk(text):
    """一段文本里的汉字数。"""
    return len(_CJK.findall(text or ""))


def textlayer_stats(d):
    """扫 ocr_text/ 现算每页汉字数，返回 SUMMARY 形状的 dict。

    正常情况下用 textlayer.py 打印的 SUMMARY 行（契约固定），这里只是**兜底**：
    重跑时跳过了抽取（目录已存在）、或 SUMMARY 行没打出来，都不能让判定失明。
    """
    pages = with_text = empty = cjk_total = 0
    for f in _page_jsons(d):
        pages += 1
        try:
            doc = jsonio.read(os.path.join(d, f))
        except (ValueError, OSError):
            empty += 1
            continue
        n = page_cjk("".join((L.get("t") or "") for L in (doc.get("lines") or [])))
        cjk_total += n
        if n >= TEXTLAYER_MIN_CJK_PER_PAGE:
            with_text += 1
        elif n == 0:
            empty += 1
    return {"pages": pages, "with_text": with_text, "empty": empty,
            "cjk_total": cjk_total,
            "cjk_per_page": (cjk_total / pages) if pages else 0.0}


def page_image_index(work):
    """<work>/pages/ 下页图的 页号 -> 文件名 索引。

    必须按页图的实际名字给文字层产物命名：**pdftoppm 不补零**（17 页的书出
    p-01.png..p-17.png，300 页的书出 p-001..p-300），而 run_layout.py 是拿
    JSON 文件名去掉 .json 当页图名去 pages/ 找图的。两边对不上就整页跳过——
    实测 --text-layer only 跑第一本 17 页的书时，layout 17 页全部「跳过（缺页面图）」，
    region 一个都没打上，而且退出码 0（典型的静默出错）。
    """
    idx = {}
    d = os.path.join(work, "pages")
    if not os.path.isdir(d):
        return idx
    for f in sorted(os.listdir(d)):
        if not f.lower().endswith(".png"):
            continue
        m = re.search(r"(\d+)", f)
        if m:
            idx.setdefault(int(m.group(1)), f)
    return idx


def _page_no(name):
    m = re.search(r"(\d+)", name or "")
    return int(m.group(1)) if m else None


def _target_page_name(name, index):
    """按 pages/ 里的实际文件名给这一页命名；没有页图时退回 p-NNN.png。"""
    n = _page_no(name)
    if n is not None and n in index:
        return index[n]
    return _norm_page_name(name)


def _norm_page_name(name):
    """把页级 JSON 文件名归一到 p-NNN.png，用于跨目录对页。

    **这一条是实测踩出来的**：poppler 的 pdftoppm 不会补零（17 页的书出 p-01.png..
    p-17.png，300 页的书出 p-001..p-300），而 textlayer.py 统一按 p-001.png 输出。
    两边文件名对不上，页图就找不着——实测 --text-layer only 跑第一本 17 页的书时，
    layout 阶段 17 页全被"跳过（缺页面图）"，region 一个都没打上，且退出码 0。
    """
    m = re.search(r"p-0*(\d+)\.png", name or "")
    return ("p-%03d.png" % int(m.group(1))) if m else name


def page_has_text(doc):
    """一页的文字层是否有正文：汉字数达到 TEXTLAYER_MIN_CJK_PER_PAGE 才算。"""
    lines = (doc or {}).get("lines") or []
    return page_cjk("".join((L.get("t") or "") for L in lines)) >= TEXTLAYER_MIN_CJK_PER_PAGE


def copy_usable_pages(src, dst, overwrite=False, include_empty=False, index=None):
    """把 src 里"有正文"的页筛选到 dst，返回筛选出的页数。

    只复制有文字的页：没文字的页故意不写，让它在选源时落到下一顺位（ocr_fix/ocr），
    由 OCR 补上那一页的正文。**绝不写空页**——那等于用空白覆盖正文。

    include_empty=True 只给 --text-layer only 用：那条路没有任何 OCR 可补，
    不把空页写出去的话，页数会比原书少（下游按文件名对齐页图，缺页不会错位，
    但"少了一页"本身要能解释得清）。
    """
    os.makedirs(dst, exist_ok=True)
    index = page_image_index(os.path.dirname(os.path.abspath(dst))) if index is None else index
    keep = set()
    n = 0
    for f in _page_jsons(src):
        # 文件名与内层 name 都跟 pages/ 里的页图对齐：下游是拿 JSON 文件名去掉 .json
        # 去 pages/ 找图的，名字对不上那页就被跳过（实测整本 17 页全跳过、退出码 0）。
        name = _target_page_name(f, index)
        keep.add(name)
        out = os.path.join(dst, name + ".json")
        if not overwrite and os.path.exists(out):
            n += 1
            continue
        try:
            doc = jsonio.read(os.path.join(src, f))
        except (ValueError, OSError):
            continue
        if not include_empty and not page_has_text(doc):
            continue
        doc["name"] = name
        jsonio.write(out, doc)
        n += 1
    # 重跑时要清掉上一轮筛出来、这一轮已经不算有文字的页
    for f in _page_jsons(dst):
        if f[:-5] not in keep:
            os.remove(os.path.join(dst, f))
    return n


def complete_textlayer(work, ocr_src):
    """ocr_text 里还缺的页，从 OCR 结果补齐（**页数守恒是硬要求**）。

    缺页的后果不是"少一页图"，而是这本书少一段正文——下游 md 只读一个目录，
    目录里没有的页就等于不存在。文字的页用文字层、没文字的页用 OCR，
    每页 JSON 自带的 producer 字段会如实记录这一页是谁产的，不会含糊。
    返回补齐的页数；没有可补的来源时返回 -1（页数守恒做不到）。
    """
    dst = os.path.join(work, TEXTLAYER_DIR)
    raw = os.path.join(work, TEXTLAYER_RAW_DIR)
    if not (os.path.isdir(raw) and os.path.isdir(dst)):
        return -1
    index = page_image_index(work)
    # 按**页号**对齐，不按文件名：pages/ 里可能是 p-01.png，OCR 目录里可能叫 p-001.png.json，
    # 字符串相等在这里不可靠（实测踩过）。
    want = {_page_no(f) for f in _page_jsons(raw)} - {None}
    have = {_page_no(f) for f in _page_jsons(dst)} - {None}
    missing = sorted(want - have)
    if not missing:
        return 0
    for name in ("ocr_fix", "ocr_layout", "ocr_merged", "ocr"):
        src = os.path.join(work, name)
        if not (os.path.isdir(src) and _page_jsons(src)):
            continue
        by_no = {}
        for f in _page_jsons(src):
            n = _page_no(f)
            if n is not None:
                by_no.setdefault(n, f)
        n = 0
        for no in missing:
            f = by_no.get(no)
            if not f:
                continue
            doc = jsonio.read(os.path.join(src, f))
            dst_name = _target_page_name(f, index)
            doc["name"] = dst_name
            jsonio.write(os.path.join(dst, dst_name + ".json"), doc)
            n += 1
        if n:
            print("  %d 页没有文字层，用 %s 的识别结果补齐（页数守恒）" % (n, name))
            return n
    return -1


def ensure_textlayer_coverage(a):
    """verify 模式下，ocr_text 里缺的页从 OCR 补回来；补不齐就明确报错。

    **页数守恒是硬要求**：下游 md 只读一个目录，目录里没有的页等于这本书少了那段正文。
    文字的页用文字层、没文字的页用 OCR，补齐后每页 JSON 的 producer 会如实记录来源。
    """
    if getattr(a, "text_layer", "verify") != "verify":
        return
    if not os.path.isdir(os.path.join(a.work, TEXTLAYER_RAW_DIR)):
        return                                  # 这本书压根没跑过文字层
    ok, _ = textlayer_state(a.work)
    if not ok:
        # 文字层整体不可用：ocr_text 保持空，正文源会落到 OCR。
        # 这时**不要**用 OCR 去"补齐" ocr_text —— 那等于把 OCR 结果挂上文字层的名字，
        # 下次看目录的人会以为这本来是 PDF 自带的文字层。
        return
    if os.path.isdir(os.path.join(a.work, TEXTLAYER_DIR)):
        complete_textlayer(a.work, "ocr_fix")
    got, total = textlayer_coverage(a.work)
    if total and got < total:
        sys.exit("ocr_text 只有 %d/%d 页：缺的页既没有文字层也没有 OCR 结果可补。\n"
                 "  缺页会让成书直接少掉那些页的正文，这里停下而不是静默产出残书。\n"
                 "  先跑 bookflow.py ocr + repair，或加 --text-layer off。"
                 % (got, total))


def textlayer_coverage(work):
    """(已覆盖页数, 原书页数)：ocr_text 与 raw 的页数对比。"""
    raw = os.path.join(work, TEXTLAYER_RAW_DIR)
    dst = os.path.join(work, TEXTLAYER_DIR)
    return (len(_page_jsons(dst)) if os.path.isdir(dst) else 0,
            len(_page_jsons(raw)) if os.path.isdir(raw) else 0)


def textlayer_usable(st):
    """判定文字层能不能当正文用。返回 (是否可用, 人话理由)。

    三条判据都要过（理由见上面 TEXTLAYER_* 常量的实测注释）：
      ① 有页；
      ② 平均每页汉字数 ≥ TEXTLAYER_MIN_AVG_CJK（纯扫描件实测 0，只剩页眉页码的实测 10）；
      ③ 有正文的页占比 ≥ TEXTLAYER_MIN_PAGE_RATIO（防「平均值被少数满页拉高」：
         实测《SOC-V8》平均 86.9 汉字/页却只有 30% 的页有字，是扫描件插了数字页）。
    """
    if not st or not st.get("pages"):
        return False, "没有产出任何页（文字层为空或 pdftotext 失败）"
    per = float(st.get("cjk_per_page") or 0.0)
    pages = int(st.get("pages") or 1)
    ratio = float(st.get("with_text") or 0) / pages
    if per < TEXTLAYER_MIN_AVG_CJK:
        return False, ("每页平均只有 %.1f 个汉字（阈值 %d）：文字层要么是纯扫描件（实测 0），"
                       "要么只剩页眉/页码（实测 10 汉字/页）"
                       % (per, TEXTLAYER_MIN_AVG_CJK))
    if ratio < TEXTLAYER_MIN_PAGE_RATIO:
        return False, ("只有 %d/%d 页有正文（阈值 %.0f%%）：其余页是扫描图，"
                       "拿文字层当正文会让那些页整页消失"
                       % (st.get("with_text", 0), pages,
                          TEXTLAYER_MIN_PAGE_RATIO * 100))
    return True, ("%d/%d 页有文字，每页平均 %.1f 个汉字"
                  % (st.get("with_text", 0), pages, per))


def textlayer_state(work):
    """文字层现在能不能用：先看 marker，没有 marker 就现扫一遍目录。"""
    meta = textlayer_meta(work)
    if meta:
        return bool(meta.get("usable")), meta.get("reason") or ""
    for name in (TEXTLAYER_DIR, TEXTLAYER_RAW_DIR):
        d = os.path.join(work, name)
        if os.path.isdir(d) and _page_jsons(d):
            return textlayer_usable(textlayer_stats(d))
    return False, "还没有跑过 textlayer 阶段"


def active_dirs(a):
    """按 --text-layer 过滤选源候选目录。

    * off   ：完全排除 ocr_text —— 硬回归要求，产物必须与加功能之前逐字节一致；
    * only  ：只有文字层可读（没有 OCR 结果，也不该去读）；
    * verify：默认顺序；但文字层实测不可用时必须排除，否则会选到一堆空行，
              正文直接清空——这是本项目最怕的静默出错。
    """
    mode = getattr(a, "text_layer", "verify")
    if mode == "off":
        return tuple(d for d in OCR_DIRS if d != TEXTLAYER_DIR)
    if mode == "only":
        return (TEXTLAYER_DIR,)
    ok, _ = textlayer_state(a.work)
    if not ok:
        return tuple(d for d in OCR_DIRS if d != TEXTLAYER_DIR)
    return OCR_DIRS


def parse_summary(text):
    """从 textlayer.py 的 stdout 里取最后一行 SUMMARY {...}。没有/坏了解析返回 None。"""
    got = None
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("SUMMARY"):
            continue
        try:
            got = json.loads(line[len("SUMMARY"):].strip())
        except ValueError:
            continue
    return got if isinstance(got, dict) else None


def _script_name(cmd):
    """从命令行里挑出被跑的脚本名（报错时比整条命令好读）。"""
    for part in cmd:
        b = os.path.basename(str(part))
        if b.endswith(".py"):
            return b
    return "子进程"


def kill_tree(proc):
    """杀掉子进程**及其派生的所有进程**。

    为什么需要它（实测，不是理论）：本次开发中我中断了 all --text-pages 40-140，
    bookflow 进程退了，但 pdftoppm 没跟着死 —— 它又跑了 20 分钟、渲染了 425 页，
    一直占着文件句柄，导致 pages/ 删不掉（「另一个进程正在使用此文件」）、重跑报错。
    渲染 900 页的书要十几分钟，用户中途 Ctrl+C 是常态，这个坑一定会被踩到。

    Windows 上 terminate() 只杀直接子进程，pdftoppm 这类 poppler 工具会自己再拉
    子进程/线程，所以这里用 taskkill /T 杀整棵树；POSIX 上 terminate 后兜底 kill。
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False)
        else:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except (OSError, ValueError):
        try:
            proc.kill()
        except OSError:
            pass


def sh(cmd, **kw):
    """跑子进程：失败给人话；被中断时**连同子进程一起收尸**。

    这里刻意不用 subprocess.run(check=True)，两个理由：

    1. 失败时不要吐 traceback。实测 all 流程里 audit 因缺页图崩了，用户看到的是
       subprocess.CalledProcessError + 一整段栈，栈帧里还显示 anaconda 的
       subprocess.py —— 那只是 **venv 复用了 base 解释器的标准库**
       （sys.prefix=ocr-venv、sys.base_prefix=anaconda3），不是"跑错解释器"。
       光凭那段谁都判断不出来。这里翻译成「哪个脚本、什么命令、退出码多少」。
    2. Ctrl+C 时子进程不能变孤儿（见 kill_tree 的实测记录）。
    """
    print("  $ " + " ".join(str(c) for c in cmd), flush=True)
    try:
        proc = subprocess.Popen(cmd, **kw)
    except OSError as e:
        sys.exit("%s 启动失败：%s\n  命令：%s"
                 % (_script_name(cmd), e, " ".join(str(c) for c in cmd)))
    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        print("\n  收到中断，正在结束子进程…", flush=True)
        kill_tree(proc)
        sys.exit(130)
    if rc != 0:
        sys.exit("%s 失败（退出码 %d）。\n"
                 "  命令：%s\n"
                 "  上方是该脚本自己的输出；若看不懂，先单独跑这条命令看完整报错。"
                 % (_script_name(cmd), rc, " ".join(str(c) for c in cmd)))


def sh_capture(cmd, **kw):
    """跑子进程并收回 stdout（其余阶段都是一条管道透传到终端）。

    只有文字层这一步需要捕获：textlayer.py 的 SUMMARY 行是它契约的一部分，
    不带它就只能靠猜判可用性。stderr 仍然透传，失败时用户能看到真实原因。
    返回 (退出码, 完整 stdout)。
    """
    print("  $ " + " ".join(str(c) for c in cmd), flush=True)
    # errors="replace" 是刻意的加固：textlayer.py 的 SUMMARY 行现在保证纯 ASCII，
    # 但万一将来某行漏了这个保证，也只是变成 U+FFFD，不会抛 UnicodeDecodeError
    # 把整个阶段炸掉（Windows 上子进程默认 GBK，实测过这个崩溃）。
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", **kw)
    except OSError as e:
        sys.exit("%s 启动失败：%s\n  命令：%s"
                 % (_script_name(cmd), e, " ".join(str(c) for c in cmd)))
    try:
        out, _ = proc.communicate()
    except KeyboardInterrupt:
        print("\n  收到中断，正在结束子进程…", flush=True)
        kill_tree(proc)
        sys.exit(130)
    out = out or ""
    for line in out.splitlines():
        print("  " + line, flush=True)
    return proc.returncode, out


def stage_render(a):
    pages = os.path.join(a.work, "pages")
    os.makedirs(pages, exist_ok=True)
    if os.listdir(pages) and not a.force:
        print("  pages 已存在，跳过（--force 可重做）")
        return
    sh(["pdftoppm", "-r", str(a.dpi), "-gray", "-png", a.pdf, os.path.join(pages, "p")])
    print("  页数 %d" % len(os.listdir(pages)))


def stage_textlayer(a):
    """抽取 PDF 自带文字层 -> <work>/ocr_text/（不跑 OCR）。

    与 textlayer.py 的全部接口只有两个：**退出码**和 **stdout 末尾的 SUMMARY 行**。
    刻意不 import 它——它是并行开发的模块，CLI 契约才是稳定边界。

    PDF 换了必须重抽：页级 JSON 与页图一一对应，拿上一本书的文字层去配这一本的
    页图，下游会一本正经地排出别人的书（本项目最怕的静默出错）。
    """
    check_pdftotext()
    if not getattr(a, "pdf", ""):
        sys.exit("textlayer 阶段需要 --pdf")
    pdf = os.path.realpath(a.pdf)
    if not os.path.isfile(pdf):
        sys.exit("找不到 PDF：%s" % a.pdf)
    # textlayer.py 原样写到 ocr_text_raw/，我们再按页筛一份到 ocr_text/。
    # **不能让它直接写 ocr_text**：一旦目录里有残缺的页，「部分页有文字」的书就会被
    # ocr_dir 当成正文源，没文字的那半本书直接消失（实测《一页纸学习法》10 页里 4 页
    # 是纯图片，直接用会丢 40% 正文）。raw 目录留着是为了能按页诊断"哪几页没文字"。
    raw = os.path.join(a.work, TEXTLAYER_RAW_DIR)
    dst = os.path.join(a.work, TEXTLAYER_DIR)
    meta = textlayer_meta(a.work)
    same_pdf = bool(meta.get("pdf")) and os.path.realpath(meta["pdf"]) == pdf
    have = os.path.isdir(raw) and bool(_page_jsons(raw))

    if have and same_pdf and not a.force:
        st = meta.get("summary") or textlayer_stats(raw)
        print("  文字层已存在（%d 页），跳过（--force 可重做）" % (st.get("pages") or 0))
    else:
        if have and not same_pdf:
            # 换书：残留页会把页数搞错（旧书比新书长就多出若干页），直接清空重抽。
            print("  检测到 PDF 已更换（%s），清空旧文字层重抽" % meta.get("pdf"))
            shutil.rmtree(raw, ignore_errors=True)
        os.makedirs(raw, exist_ok=True)
        cmd = [PY, os.path.join(HERE, "textlayer.py"), "--pdf", a.pdf,
               "--out", raw, "--dpi", str(a.dpi)]
        pgs = getattr(a, "pages", "") or ""
        if pgs:
            cmd += ["--pages", pgs]
        rc, out = sh_capture(cmd)
        st = parse_summary(out)
        if st is None:
            # 契约没兑现（没打 SUMMARY）不能让判定失明：就地扫一遍产物自己算。
            # 扫的是 raw（textlayer.py 真正的输出目录），不是还没生成的 ocr_text。
            print("  警告：textlayer.py 没有打印 SUMMARY 行，改为直接扫描产物统计")
            st = textlayer_stats(raw)

        # 退出码是契约的一部分，必须分情况处理（与 textlayer-dev 敲定的语义）：
        #   0 = 成功
        #   2 = 整本一个可读字符都没有（纯扫描件；raw 目录页数守恒、全是空行）
        #   3 = pdftotext 本身失败（加密/坏文件/PATH 缺失，**一个文件都没写**）
        #   4 = 参数错（页码倒序、首页超界，同样的**一个文件都没写**）
        # 2 与 3/4 的区别很关键：2 的 raw 目录页数守恒、可以当"全是空行"继续判
        # （下面靠 PAGE_RATIO 判掉，不会拿去当正文）；3/4 的目录是空的，
        # 拿它当源等于正文清空。
        #
        # 文案区分靠 SUMMARY 里的 error_code（ASCII 稳定），不去匹配中文——中文文案
        # 随时可能被改写，匹配文案等于把两边的字符串绑死在一起。
        if rc not in (0, 2):
            code = (st or {}).get("error_code") or ""
            hint = {
                "NO_PDFTOTEXT": "PATH 上找不到 pdftotext（装 poppler 并加进 PATH）",
                "PDF_MISSING": "PDF 文件不存在（检查 --pdf 路径）",
                "BAD_ARGS": "参数不合法（检查 --text-pages 的页码范围/顺序）",
                "PDFTOTEXT_FAILED": "pdftotext 读不了这本 PDF（加密或文件损坏）",
                "TIMEOUT": "pdftotext 超时",
                "EMPTY_OUTPUT": "pdftotext 没有任何输出",
                "SPAWN_FAILED": "pdftotext 进程起不来",
            }.get(code)
            msg = ("文字层抽取失败（退出码 %d%s）%s"
                   % (rc, ("：" + code) if code else "",
                      ("；原因：" + hint) if hint else ""))
            explicit = getattr(a, "_explicit_textlayer", False)
            if a.text_layer in ("verify", "off") and not explicit:
                # 默认模式下文字层只是**加速通道**，不是必需品：抽不出来就老老实实跑 OCR，
                # 不能让一个可选增强把整本书的产出挡死（加密 PDF 是常态，不是异常）。
                print("  " + "!" * 68)
                print("  " + msg)
                print("  已自动退化：本轮直接跑 OCR（等价于 --text-layer off）。")
                print("  " + "!" * 68)
                return False, {"pages": 0}, msg
            # only / 用户显式点名 textlayer 阶段：没有任何退路，必须明确失败。
            sys.exit(msg + "\n  若这本是纯扫描件、只想跑 OCR，加 --text-layer off 跳过文字层。")
        if rc == 2 and not _page_jsons(raw):
            # 退出码 2 声称"raw 页数守恒"，但目录其实是空的（契约没兑现）：
            # 当成 0 页处理，让下面的可用性判定直接判死的，而不是拿着怪数据往下走。
            print("  警告：textlayer.py 返回 2（整本无字），但 %s 里没有页文件" % raw)
            st = dict(st or {})
            st.update({"pages": 0, "with_text": 0, "empty": 0, "cjk_per_page": 0.0})

    # 只有 PASS 级结论才允许进入 OCR_DIRS 竞争。
    # 「部分页有文字」本身是合法的（实测《一页纸学习法》10 页里 6 页有字），
    # 但它绝不能当正文：正文用部分页文字 = 另外一半的页直接消失；而且文字层按 y 聚类
    # 分行、OCR 按检测框出行，两套口径混在一本书里，layout 的跨页合段会在混源页边界误判。
    # 所以只有"几乎整本都有文字"（TEXTLAYER_MIN_PAGE_RATIO）的书才让 ocr_text 参与选源。
    ok, why = textlayer_usable(st)
    if ok and (st.get("empty") or 0) > 0:
        why += "；另有 %d 页没有文字" % (st.get("empty") or 0)

    # 按页筛：只拷贝"这页真的有正文"的页。剩下没文字的页**故意不写**，
    # 页码就会落到下一顺位（ocr_fix/ocr），那一页仍由 OCR 出正文。
    # 文件名按 pages/ 里的实际名字来（pdftoppm 不补零，见 page_image_index）。
    # force 时连文件内容也重写：raw 重抽过，旧的内层 name 可能还是旧名字。
    n_keep = copy_usable_pages(raw, dst, overwrite=a.force,
                               include_empty=(a.text_layer == "only"),
                               index=page_image_index(a.work))
    if n_keep:
        print("  按页筛出 %d/%d 页（有正文的页优先用文字层，其余页仍走 OCR）"
              % (n_keep, st.get("pages") or len(_page_jsons(raw))))

    jsonio.write(os.path.join(dst, TEXTLAYER_META), {
        "pdf": os.path.realpath(a.pdf),
        "dpi": a.dpi,
        "summary": st,
        "usable": ok,
        "reason": why,
        "kept_pages": n_keep,
        "thresholds": {"min_avg_cjk": TEXTLAYER_MIN_AVG_CJK,
                       "min_page_ratio": TEXTLAYER_MIN_PAGE_RATIO,
                       "min_cjk_per_page": TEXTLAYER_MIN_CJK_PER_PAGE},
    })
    print("  文字层：%d 页 / 有文字 %s 页 / 空页 %s / 汉字 %s / 每页 %.1f"
          % (st.get("pages", 0), st.get("with_text", 0), st.get("empty", 0),
             st.get("cjk_total", 0), float(st.get("cjk_per_page") or 0.0)))
    if ok:
        print("  文字层可用（%s）" % why)
    else:
        print("  " + "!" * 68)
        print("  文字层不可用：%s" % why)
        if a.text_layer == "only":
            print("  当前是 --text-layer only：没有任何 OCR 结果可退回，请改跑默认的")
            print("  --text-layer verify（或 off）让 OCR 当正文来源。")
        else:
            print("  已自动退化：本轮正文仍用 OCR 结果（与 --text-layer off 等价）。")
        print("  " + "!" * 68)
    if a.text_layer == "only" and not ok:
        sys.exit("文字层不可用，only 模式无法继续（见上方告警）")
    return ok, st, why


def _merge_keep_status(old_doc, new_doc):
    """合并两份裁决表时**保留旧条目的 status**。

    dec.merge 是「后者覆盖前者」，直接拿它合并会把已经 applied 的条目打回 pending，
    于是下一次 apply 又"重新应用"一遍（幂等性在数据里被抹掉）。这里只覆盖 A/B/verdict
    这些内容字段，status 是运行状态，不能凭空重置。
    """
    import decisions as dec
    merged = dec.merge(old_doc, new_doc)
    before = {e.get("key"): e for e in (old_doc or {}).get("decisions") or []}
    for e in merged["decisions"]:
        old = before.get(e["key"])
        if old and old.get("status") in ("applied", "missing"):
            e["status"] = old["status"]
    return merged


def stage_verify_guarded(a):
    """all 流程里的交叉验证：**失败也不能挡住出书**。

    它是"给人工裁决备料"的可选环节，不是正文通路。缺 jieba 词表、disagree.py 崩、
    目录对不上，全都降级成一行告警——书照出，人想补验证再单独跑
    `bookflow.py verify --work <work>`。
    """
    try:
        stage_verify(a)
    except SystemExit as e:
        msg = e.code if isinstance(e.code, str) else str(e.code)
        print("  " + "!" * 68)
        print("  交叉验证跳过：%s" % msg)
        print("  成书不受影响（正文已用文字层出完）。补齐后可单独重跑：")
        print("    python bookflow.py verify --work %s" % a.work)
        print("  " + "!" * 68)
    except (subprocess.CalledProcessError, OSError) as e:
        print("  " + "!" * 68)
        print("  交叉验证失败（%s），本轮跳过；成书不受影响。" % e)
        print("  " + "!" * 68)


def stage_verify(a):
    """文字层 × OCR 交叉验证。

    **比对逻辑一行都不重写**：直接调 disagree.py 的 --a/--b。它本来就是"两版结果逐行比，
    词表判 A/B/?，差异段过滤标点"的通用实现，喂两版 OCR 和喂"文字层 vs OCR"是同一件事。

      A 侧 = ocr_text（PDF 自带的文字层：没有识别错误，是这一轮的事实基准）
      B 侧 = ocr_fix  （OCR 实际读出来的：错在识别过程本身）

    ## 为什么 A 侧必须是文字层（这条别改，改了 verdict 语义就反了）

    disagree.py 的判定是「词表倾向哪一侧更像真词」，输出 verdict：
        A = 倾向 A 侧对    B = 倾向 B 侧对    ? = 词表判不出，要人看图

    而 apply_decisions 对 verdict 的解释是：
        A = 保留 A 侧原文（只登记，**不动作**）
        B = 采用 B 侧整行覆盖

    把文字层放在 A 侧，这两套语义就正好咬合：
        verdict=A → 保留文字层（本来就是对的，无需动作）
        verdict=B → 采用 OCR 那版（文字层在这里丢了字/串行了，OCR 更好）
    反过来说，如果 A 侧放 OCR、B 侧放文字层，那 verdict=A 就变成"保留 OCR"——
    等于把文字层这个更准的来源判成"该被 OCR 覆盖"，语义彻底反了。

    **所以这里的 A 不是"OCR"，是文字层。** 下游任何按 verdict 处理的地方，
    都要按这个口径理解（decisions.json 的 note 里也已经写明「A 对/B 对」是相对哪两侧）。

    候选落进 out/disagree.json，再由本阶段转成 out/decisions.json（同一份 schema、
    同一套 page@y0 主键），之后就是既有的 apply_decisions 闭环——人裁决、回填
    ocr_merged、md 优先读它（ocr_merged 在 OCR_DIRS 里排在 ocr_text 之前，见那里）。
    """
    check_dict()
    text_dir = os.path.join(a.work, TEXTLAYER_DIR)
    if not (os.path.isdir(text_dir) and _page_jsons(text_dir)):
        sys.exit("verify 需要先跑 textlayer 阶段：找不到文字层结果 %s" % text_dir)
    ocr_side = os.path.join(a.work, "ocr_fix")
    if not (os.path.isdir(ocr_side) and _page_jsons(ocr_side)):
        sys.exit("verify 需要 OCR 结果做对照：找不到 %s\n"
                 "  纯文字层不想跑 OCR 的话用 --text-layer only。" % ocr_side)
    out = os.path.join(a.work, "out")
    os.makedirs(out, exist_ok=True)
    sh([PY, os.path.join(HERE, "disagree.py"),
        "--a", text_dir, "--b", ocr_side,
        "--out", os.path.join(out, "disagree.json"),
        "--txt", os.path.join(out, "disagree.txt")])

    import decisions as dec
    dst = a.out or os.path.join(out, "decisions.json")
    picked = dec.load_any(os.path.join(out, "disagree.json"), kind="disagree",
                          report=os.path.join(out, "report.json"),
                          ocr=text_dir)
    if os.path.exists(dst):
        doc = _merge_keep_status(dec.read(dst), picked)
    else:
        doc = picked
    dec.write(dst, doc)
    n = len(doc["decisions"])
    print("  交叉验证候选 -> %s（共 %d 条：%s）"
          % (dst, n, ", ".join("%s=%d" % kv for kv in sorted(dec.counts(doc).items()))))
    # verdict 是相对 A/B 两侧的，这里把两侧是谁再喊一次——别让下一个人以为 A 还是 OCR。
    print("  口径：A 侧 = %s（文字层，没有识别错误）；B 侧 = %s（OCR 识别结果）。"
          "verdict=A 保留文字层、verdict=B 采用 OCR、? 要人看图"
          % (TEXTLAYER_DIR, os.path.basename(ocr_side)))
    print("  分歧明细 -> out/disagree.json；裁决后回填："
          "python apply_decisions.py --work %s --apply" % a.work)


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
#
# ocr_text（文字层）插在 ocr_merged 之后、ocr_fix 之前，理由：
#   * 它必须**低于 ocr_merged**：ocr_merged 是人工裁决 / 双模型交叉验证**回填过**的结果，
#     那是最贵的信号（人看过的）。文字层再准也只是"机器读出来的原样"，
#     不能反过来压住人已经判过的结论——否则跑完 decisions --apply，md 还在读原始文字层，
#     人工裁决被静默丢弃（与上面 ocr_layout/ocr_merged 踩过的坑是同一类）。
#   * 它必须**高于 ocr_fix / ocr**：这两份是 OCR 识别结果，错字来自识别过程本身
#     （形近字、生僻字、标点），而文字层没有识别错误这一说。同一条流水线上两者都齐的时候，
#     文字层永远是更好的正文来源——这正是 --text-layer verify 的默认前提。
#   * 它**低于 ocr_layout**：ocr_layout 是"同一份文本 + region 标签"，标签才是下游要的东西
#     （脚注/页眉/页码分流）；文字层没有 region 标签，跑过 layout 的结果结构上更完整。
OCR_DIRS = ("ocr_layout", "ocr_merged", "ocr_text", "ocr_fix", "ocr")


def _dir_freshness(d):
    """目录里最新一个 json 的 mtime；空目录返回 None。"""
    try:
        ts = [os.path.getmtime(os.path.join(d, f))
              for f in os.listdir(d) if f.endswith(".json")]
    except OSError:
        return None
    return max(ts) if ts else None


def _describe_dirs(work, dirs=None):
    """把每个候选目录的状态列出来，失败时让人一眼看出缺哪一环。"""
    rows = []
    for name in (dirs or OCR_DIRS):
        d = os.path.join(work, name)
        if not os.path.isdir(d):
            rows.append("[缺失] %s" % name)
        else:
            n = len([f for f in os.listdir(d) if f.endswith(".json")])
            rows.append("[空]" if not n else "[%d json]" % n)
            rows[-1] = "%s %s" % (rows[-1], name)
    return "  ".join(rows)


def _page_jsons(d):
    """目录下的页级 JSON 文件名（按页序），不含任何非页文件。"""
    return sorted(f for f in os.listdir(d) if f.endswith(".png.json"))


def _same_text(d1, d2, sample=3):
    """两个页级目录的正文文本是否一致（抽样比 name 对应的行文本）。

    用来判断某个目录是不是"另一份目录加了 region 标签的派生物"——
    run_layout.py 只加字段不改文本，所以文本一致即同源。
    """
    if not (os.path.isdir(d1) and os.path.isdir(d2)):
        return False
    f1, f2 = _page_jsons(d1), _page_jsons(d2)
    if not f1 or not f2:
        return False
    by_no1 = {}
    for f in f1:
        n = _page_no(f)
        if n is not None:
            by_no1.setdefault(n, f)
    by_no2 = {}
    for f in f2:
        n = _page_no(f)
        if n is not None:
            by_no2.setdefault(n, f)
    common = sorted(set(by_no1) & set(by_no2))[:sample]
    if not common:
        return False
    for n in common:
        try:
            t1 = [L.get("t") for L in (jsonio.read(os.path.join(d1, by_no1[n])).get("lines") or [])]
            t2 = [L.get("t") for L in (jsonio.read(os.path.join(d2, by_no2[n])).get("lines") or [])]
        except (ValueError, OSError):
            return False
        if t1 != t2:
            return False
    return True


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
    # 选源前先按 --text-layer 过滤：off 模式下**一个字节都不碰文字层**（硬回归要求，
    # 产物必须与加功能之前逐字节一致）；only 模式没有 OCR 结果，只能读文字层。
    dirs = active_dirs(a)
    picked = None
    for name in dirs:
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
                 % ("/".join(dirs), a.work, _describe_dirs(a.work, dirs)))

    name, d = picked
    mine = _dir_freshness(d)
    for other in dirs[dirs.index(name) + 1:]:
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
    # 文字层和 OCR 结果同名同格式（都是 p-001.png.json），选错了用户根本看不出来。
    # 实测踩过：拿没回填过的 ocr 当源，成书 232,904 汉字 vs 基准 233,812 —— 差 908 字
    # 完全是裁决没生效，但终端上一行提示都没有。所以选中文字层、或该用文字层却没用时，
    # 必须明确说选的是谁、为什么。
    if name == TEXTLAYER_DIR:
        why = []
        for lower in ("ocr_merged", "ocr_fix", "ocr"):
            od = os.path.join(a.work, lower)
            why.append("%s %s" % (lower, "存在" if os.path.isdir(od) else "未生成"))
        print("  正文源: %s（PDF 自带文字层；优先级高于 %s，且没有人工裁决过的 ocr_merged）"
              % (TEXTLAYER_DIR, "、".join(why)))
    elif (getattr(a, "text_layer", "verify") == "verify"
          and os.path.isdir(os.path.join(a.work, TEXTLAYER_DIR))
          and textlayer_state(a.work)[0]):
        # 选中的若是从文字层派生的目录，得说清楚"其实用的还是文字层的文本"，
        # 否则用户看到"正文源是 ocr_layout"会以为文字层没生效。
        same = _same_text(d, os.path.join(a.work, TEXTLAYER_DIR))
        if same:
            print("  正文源: %s（由文字层 %s 加 region 标签派生，正文文本仍是文字层的）"
                  % (name, TEXTLAYER_DIR))
        else:
            print("  提示：正文源是 %s，不是文字层 %s（文字层在优先级里低于人工裁决过的 "
                  "ocr_merged；要强制用文字层加 --ocr-src %s）"
                  % (name, TEXTLAYER_DIR, TEXTLAYER_DIR))
    print("  OCR 源: %s（按优先级）" % d)
    return d


def stage_layout_guarded(a):
    """all 流程里的版面分析：页图对不上就**不要让整轮白跑**。

    实测踩过：17 页的小册子 pages/ 里是 p-01.png..p-17.png，而别处的页级 JSON 是
    p-001.png.json —— run_layout.py 按 JSON 文件名去 pages/ 找图，于是 17 页全部
    「跳过（缺页面图）」，region 一个都没打上，**退出码仍是 0**。
    版面分析是可选增强，这里把它降级成告警而不是让 all 中断。
    """
    pages = os.path.join(a.work, "pages")
    src = ocr_dir(a)
    have = set(os.listdir(pages)) if os.path.isdir(pages) else set()
    js = _page_jsons(src)
    # _page_jsons 给的是 "p-01.png.json"，pages/ 里是 "p-01.png"：必须去掉 .json 再比，
    # 否则两边永远不相交，版面分析会被误判成"文件名对不上"而整本跳过（实测踩过）。
    want = {f[:-5] for f in js}
    if have and want and not (want & have):
        print("  " + "!" * 68)
        print("  版面分析跳过：%s 里的页级 JSON 与 pages/ 里的页图文件名对不上" % os.path.basename(src))
        print("    JSON 例：%s" % js[0])
        print("    页图例：%s" % next(iter(sorted(have))))
        print("  版面分析只是可选增强（脚注/页眉/页码分流），本轮继续出书，只是没有 region。")
        print("  " + "!" * 68)
        return
    try:
        stage_layout(a)
    except subprocess.CalledProcessError as e:
        print("  " + "!" * 68)
        print("  版面分析失败（退出码 %s），本轮继续出书，只是没有 region 标签。" % e.returncode)
        print("  " + "!" * 68)


def stage_layout(a):
    """版面分析：给 OCR 结果补 region 标签（脚注/页眉/页码），让正文更干净。

    它是**可选增强**：不跑这一步也能出书，只是脚注会混进正文。
    输出到 ocr_layout/，因为它优先级最高，md 阶段会自动选中。
    """
    # 必须先把 ocr_text 补齐再打标签：版面分析的输出 ocr_layout 优先级最高，
    # 拿一份缺页的 ocr_text 去打标签，就会得到一份缺页的 ocr_layout，
    # md 选中它 → 那些页的正文直接从成书里消失（实测 17 页小册子会丢封面页）。
    ensure_textlayer_coverage(a)
    src = ocr_dir(a)
    # 源不能是 ocr_layout 自己：那是上一次本阶段的输出，读它等于原地空转，
    # 而且会让 ocr_layout 的 mtime 永远最新、永久压住更新的 ocr_merged。
    # 但显式 --ocr-src 是用户的明确意图（例如拿旧结果做重入实验），不替他改。
    if not getattr(a, "ocr_src", "") and os.path.basename(os.path.normpath(src)) == "ocr_layout":
        for name in OCR_DIRS[1:]:
            cand = os.path.join(a.work, name)
            if os.path.isdir(cand) and [f for f in os.listdir(cand) if f.endswith(".json")]:
                print("  布局源改用 %s（避免读自己上一次的输出）" % cand)
                src = cand
                break
    elif getattr(a, "ocr_src", "") and os.path.basename(os.path.normpath(src)) == "ocr_layout":
        print("  布局源按 --ocr-src 使用 %s（原地重跑，不自动换源）" % src)
    dst = os.path.join(a.work, "ocr_layout")
    if os.path.abspath(src) == os.path.abspath(dst) and not a.force:
        print("  提示：源与输出同为 ocr_layout，run_layout 会跳过已存在的页；"
              "要真正重跑请加 --force")
    cmd = [PY, os.path.join(HERE, "run_layout.py"),
           "--src", src, "--pages", os.path.join(a.work, "pages"),
           "--dst", dst, "--device", a.device]
    if a.force:
        cmd.append("--force")
    sh(cmd)


def stage_md(a):
    ensure_textlayer_coverage(a)
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
        # 用 _merge_keep_status 而不是裸 dec.merge：apply_decisions 会把 status 回写成
        # applied/missing，而 dec.merge 是「后者覆盖前者」，合并会把已应用的条目打回
        # pending —— 下次 apply 又"重新应用"一遍，幂等性从数据里被抹掉。
        picked = _merge_keep_status(dec.read(dst), picked)
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
        "ocr_text": 2, "ocr_text_raw": 2,   # 文字层重抽只要几秒（不跑识别），删掉最划算
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
    ap.add_argument("stage", choices=["render", "textlayer", "ocr", "repair", "layout",
                                      "md", "audit", "epub", "all", "prune", "disagree",
                                      "decisions", "verify"])
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
                         "ocr_layout > ocr_merged > ocr_text > ocr_fix > ocr 自动挑"
                         "（ocr_text 是文字层；--text-layer off 时它不参与选源）")
    ap.add_argument("--force", action="store_true",
                    help="重跑已完成的阶段（含 OCR：会重新识别已存在的页）")
    ap.add_argument("--keep", default="",
                    help="prune 阶段保留的中间目录，逗号分隔，如 pages,ocr_merged")
    ap.add_argument("--no-layout", action="store_true",
                    help="all 阶段跳过版面分析（脚注会混进正文，但速度快 1.8 分钟/350页）")
    ap.add_argument("--text-layer", dest="text_layer", default="verify",
                    choices=list(TEXTLAYER_MODES),
                    help="文字层（PDF 自带 text layer）的用法："
                         "verify=抽文字层并与 OCR 交叉验证，正文用文字层（默认，因为书的"
                         "文字层质量参差不齐，必须对账）；"
                         "only=只抽文字层、完全不跑 OCR（快 40 倍，给确认有完好文字层的书）；"
                         "off=不碰文字层，行为与本功能上线前逐字节一致（纯扫描书 / 复现旧结果）")
    ap.add_argument("--text-pages", dest="pages", default="",
                    help="textlayer 阶段只抽这些页，如 1-60（留空=整本）")
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

    # 参数校验放在建目录之前：参数错了就一个字节都别写，
    # 否则用户看到的是"多了一个空工作目录 + 一条报错"，还得手工清。
    if a.stage in ("render", "ocr", "textlayer", "all") and not a.pdf:
        sys.exit("render/ocr/textlayer 阶段需要 --pdf")
    # --text-pages 只给 textlayer 单阶段做试跑用。**不能和 all / 下游阶段组合**：
    # 它只让文字层覆盖一部分页，而 render 渲染整本、audit/make_review 又按全书页名
    # 去 pages/ 找图 —— 实测 all --text-pages 40-140 会在 audit 崩在
    # FileNotFoundError: pages\p-082.png，且产出的"只有几十页的书"极易被误解成程序坏了。
    # 与其让它半路炸在一个看不懂的地方，不如在入口就拒绝并说清楚。
    if a.pages and a.stage != "textlayer":
        sys.exit("--text-pages 只能配合 textlayer 单阶段使用（试跑用），不能和 %s 组合。\n"
                 "  局部页跑全流程会产出一本残书，而且 audit/make_review 要按全书页名\n"
                 "  去 pages/ 找图，必然找不到。整本跑请留空 --text-pages。" % a.stage)

    os.makedirs(a.work, exist_ok=True)
    # 用户显式点名 textlayer 阶段时，"抽不出来"必须报错而不是悄悄退化——
    # 他本来就是奔着文字层来的，静默降级等于骗他。all 流程里才允许退化（那是加速通道）。
    a._explicit_textlayer = (a.stage == "textlayer")
    if a.stage == "render":
        check_pdftoppm()
    check_env(a.stage, no_layout=a.no_layout)
    if a.stage == "all":
        stage_render(a)
        # 文字层：默认 verify（抽出来与 OCR 交叉验证）；off 时这一段一个字节都不碰。
        tl_ok = False
        if a.text_layer != "off":
            tl_ok, _st, _why = stage_textlayer(a)
        if a.text_layer == "only":
            # 只抽文字层，完全不跑 OCR：这是"快 40 倍"的那条路。
            print("  --text-layer only：跳过 ocr / repair（不跑识别）")
        else:
            if a.text_layer == "verify" and not tl_ok:
                print("  文字层不可用 → 本轮自动退化为跑 OCR（等价于 --text-layer off）")
            stage_ocr(a)
            stage_repair(a)
        if not a.no_layout:
            stage_layout_guarded(a)     # 可选增强：脚注/页眉/页码分流
        stage_md(a)
        # 交叉验证要在 md 之后：md 已经能出，验证只是给人工裁决备料，不能让它挡住出书。
        # 因此这里**兜住所有失败**：缺 jieba、disagree.py 崩、页面对不齐……都只告警。
        # 默认模式把 verify 变成必跑项，一旦它能把整本书挡死，就把"默认安全"变成了
        # "默认更脆"——那正是本条需求最不能接受的方向。
        if a.text_layer == "verify" and tl_ok:
            stage_verify_guarded(a)
        stage_audit(a)      # 只出清单，不改文本
        stage_epub(a)
        # disagree 不自动跑（需要第二版 OCR），但检出时提醒一声，否则这条通道等于不存在
        if os.path.isdir(os.path.join(a.work, "ocr_alt")):
            print("\n  提示：检出 ocr_alt，可跑双模型交叉验证：\n"
                  "    python bookflow.py disagree --work %s --ocr-alt ocr_alt" % a.work)
        return
    {"render": stage_render, "ocr": stage_ocr, "repair": stage_repair,
     "layout": stage_layout, "md": stage_md, "audit": stage_audit,
     "epub": stage_epub, "prune": stage_prune, "textlayer": stage_textlayer,
     "disagree": stage_disagree, "decisions": stage_decisions,
     "verify": stage_verify}[a.stage](a)


if __name__ == "__main__":
    main()
