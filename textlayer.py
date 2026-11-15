"""bookflow.textlayer — 把 PDF 自带的文字层转成我们既有的页级 JSON。

## 为什么要有这条路（实测数字，不是推测）

对有完好文字层的 PDF，我们过去仍然整页跑 OCR：**每页 17.6 秒**。同一本书用
`pdftotext` 抽文字层，**927 页只要 5.5 秒**（快 280 倍）。质量也不吃亏：
实测文字层 9,652 汉字 vs OCR 9,584 汉字，相似度 98.85%——OCR 反而少认了 68 个字。

所以对有文字层的书，先抽文字层、再用 OCR 做交叉验证（见 bookflow.py 的
`--text-layer verify|only|off`），比无条件 OCR 便宜得多。

## 输入输出

输入：`pdftotext -bbox -f A -l B <pdf> -` 的 XHTML，结构是

    <page width="595.000000" height="842.000000">
      <word xMin="18.000000" yMin="819.692000" xMax="36.000000" yMax="831.692000">PDF</word>
      ...
    </page>

坐标单位是 **PDF 点（72 dpi）**，而下游（layout/qa/make_review）全部按
`pages/*.png` 的像素坐标工作。必须乘 `dpi / 72` 换算（300 dpi → 4.1667）。

输出：`<out>/p-XXX.png.json`，与 run_ocr.py 的产物**同一个 schema**：

    {"name": "p-015.png", "w": 2480, "h": 3508,
     "lines": [{"x0": 75.0, "y0": 3415.4, "x1": 150.0, "y1": 3465.4, "t": "PDF", "s": 1.0}],
     "schema": "bookflow.pages", "schema_version": 1,
     "producer": {"name": "pdftotext", "version": "26.09.0"}, "source_pdf": "..."}

`layout.build(<out>, {})` 能直接吃下。实测：本模块产出的 60 页喂进去，
report 得到 pages=60 / chars=58,908，段落 267 段、平均 218.6 字/段，全程无异常；
另有 927 页的真书整本跑通，目录页 [3..12]、目录条目 203 命中 15。

## 四个关键判据（都来自实测）

1. **分行**：按 **yMax（基线）** 聚类，容差 3 个 PDF 点。不是按 yMin——中文字形
   框的上边缘随字形剧烈变化（实测同一行内 yMin 极差 p50=6.8pt，yMax 极差 p50=0），
   按 yMin 分行会把一整页切成 68 行（真实只有 33 行），拼出来的句子东一段西一段。
   容差单位是 PDF 点，**不能先乘 dpi/72**（那样 300dpi 下会变成 12.5pt，页眉和正文
   首行会被并成一行——实测踩过）。
2. **行内拼接**：同一行里的 word 是按 x 递增的，但中文会被切词（「学校代」+「码」），
   所以**先看两侧字符的宽度类别，再看间距**（判据见 join_words()）。纯"间距 >
   0.3 字高才补空格"这一条**不够用**：实测英文 PDF 里 poppler 会把整句切成一串
   词，相邻词的 xMax 与下一个的 xMin 几乎相等（间距中位数 0.25 em，p10 甚至是
   负数——相邻框有重叠），按 0.3 em 判就把整句拼成
   "othercountries,particularlyinaspects"。实测对比见 tests/test_textlayer.py
   的 TestJoin 与下面的数字。
3. **重复框去重**：有些 PDF（典型是 Word/LaTeX 转出来的双图层，实测那篇英文
   论文每页都有）在文字层里把同一段字输出了**两遍**，两份的坐标几乎重合：
   实测 396 对相邻同文本 word，水平重叠率 p50=0.84、322 对超过 50%。
   不去重的话拼出来就是「FigureFigure 1.1. FlowchartFlowchart ofof thethe…」；而中文
   PDF 实测 0 对，所以这条判据不会误伤中文。判据：**相邻同文本 + 水平重叠
   ≥ 50%** 时丢掉后一份。
4. **置信度**：文字层没有置信度，`s` 固定 1.0。这不表示"认对了"，
   只表示"数据本身没有不确定度"——质量由上层用字符数/汉字数判定。

## 失败必须显式（本项目头号风险是静默出错）

PDF 加密、纯扫描页（`pdftotext` 返回空 body）都不许静默产出 0 字节 JSON：

- 整份 PDF 一个可读字符都没有 → 报「文字层不可用」，**不写任何文件**，
  退出码 2（`SUMMARY` 里 `usable=false`）；
- `pdftotext` 本身失败（加密/文件坏/不在 PATH）→ 报「文字层不可用：<原因>」，
  退出码 3；
- 单页没文字（纯图片页/空白页）→ 该页写空 `lines`，计入 `SUMMARY.empty`，
  页数守恒（下游按文件名对齐 `pages/*.png`，少一页就整体错位）。

## CLI（被 bookflow.py 的 textlayer 阶段直接调用，契约固定）

    python textlayer.py --pdf <path> --out <dir> --dpi 300 [--pages A-B]

成功（文字层可用）退出码 0，并打印一行 `SUMMARY {...}`（JSON）；
字段含 `pages` / `with_text` / `empty` / `cjk_total` / `cjk_per_page`，
另带 `chars_total` / `usable` / `dpi` / `producer` / `elapsed`。
"""
import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time

import jsonio
import pagedoc

PDF_DPI = 72.0              # PDF 用户单位（点）的定义：1/72 英寸
DEFAULT_DPI = 300.0         # 与 bookflow.py render 阶段的默认 dpi 一致
DEFAULT_TIMEOUT = 1800.0    # 全书 pdftotext 实测 5.5 秒（927 页），给它 30 分钟余量

# 分行容差：同一行内 yMax（基线/下边缘）的极差 > 3 个 PDF 点即视为不同行。
# 实测依据（四本不同排版的书）：同一行内 yMax 极差 p50=0、p99≈3.5pt；
# 而**相邻行**的 yMax 差最小也有 27pt（中文正文行距约 13pt 字高 + 14pt 行距）。
# 3pt 落在"行内抖动"与"行间距"之间，离两边都有 3 倍以上余量。
# 注意这是 **PDF 点**，与坐标同单位，不要乘 dpi/72。
LINE_TOL_PT = 3.0

# ---- 行内拼接的三个阈值（单位：em，即"间距 / 该行字高"）----
# 实测方法：把一组 PDF 的相邻 word 间距按"两侧字符是否宽字"分桶统计。
#   英文论文（Global Regulatory…, 8 页 4380 对）：拉丁-拉丁间距 p10=-1.59、
#     p50=0.250、p90=0.319 —— 中位数就在 0.25，**用 0.3 当"补空格"的门槛会把
#     一半以上的英文单词黏在一起**（实测整句变成 "othercountries,particularly…"）。
#     所以拉丁-拉丁几乎总补空格，只在间距近乎 0（同一单词被切开的碎片）时不补。
#   中文（德勒兹 4653 对）：汉-汉间距 p10=0.134、p50=0.170、p90=0.244；
#     间或出现 0.30 的（实测是「中"的」这类把弯引号单独切出去的场景，
#     间距里含引号宽度，不是真空格）。所以汉-汉**一律不补空格**。
#   宽字/拉丁混排（德勒兹 1548 对）：p50=0.290、p90=0.847 —— 差异极大，
#     只能在"真的有缝"时补，取 MIXED_SPACE_EM。
# 下面三个阈值都落在各自实测分布的两簇之间，证据见每条注释。
#
# 拉丁-拉丁：**间距 >= 0 就补空格**，只有框重叠（gap < 0）才不补。
# 实测依据：英文论文 6369 对拉丁相邻词，gap 的 p10=0.19、p50=0.25，但它们
# 全都在 0.02 以上吗？不——实测有 578 对是负值（相邻框重叠，那是 poppler 把
# 一个单词切成两段），但**恰好等于 0 的只有 1 对**。按 "gap >= 0 补空格"
# 去对照 pdftotext 的纯文本：3626 对里**判错 0 对**。而按 "gap > 0.02" 判，
# 那一对零间隙的会黏成一个词。
LATIN_SPACE_EM = 0.0
# 宽字-拉丁混排：实测分布是**双峰**的（繁体论文混排对：
#   0.21~0.26 一簇（「與表」+「4-1-1」、「7,808」+「位受訪者」——排版里没空格，
#   只是拉丁字宽比汉字窄）、0.31~0.57 一簇（「TYPE-MOON」+「相關作品」——真空格）。
# 0.30 正好落在两簇之间：0.21~0.26 那批不会被插空格，0.31 以上会补上。
MIXED_SPACE_EM = 0.30
HUGE_GAP_EM = 3.0       # 超过 3 em 的缝（双栏/表格/目录页码列）一律补空格

# 收尾标点：**右词以它开头时一律左贴**，不看间距。
# 实测依据：四本书里"右词首字是收尾标点"的情形共 111 处，pdftotext 自己的纯文本
# 有 106 处不补空格、只有 5 处补——而那 5 处全在一个文字层本身已经坏掉的 PDF 上
# （它的纯文本是「主 义和 后 结 构」这样逐字加空格的乱码）。所以这条判据的
# 准确率是 106/106（排除坏样本），比任何间距阈值都硬。
# 拉丁的 ’ 尤其重要：poppler 常把 "devices’" 切成 "devices" + "’"，两者 box
# 首尾相接（gap=0），靠"gap>=0 就补空格"会拼出 "devices ’ classification"。
CLOSERS = frozenset("\u2019\u201d)'”）】》」』，。、；：？！,.!?;:")

# 兼容旧名（外部脚本可能 import 过）
SPACE_EM = MIXED_SPACE_EM

# 文字层「薄」的告警线：每页汉字数低于此值时提示需要交叉验证。
# 与 bookflow.py 的可用性阈值同源（每页 < 50 汉字视为不可用），这里只告警不拦截——
# 拦截与否由 bookflow.py 决定，textlayer 只负责如实报数。
MIN_CJK_PER_PAGE = 50

# 已知的 producer 名（登记在 pagedoc.KNOWN_PRODUCERS）
PRODUCER = "pdftotext"

_WORD_RE = re.compile(r"<word\b([^>]*)>(.*?)</word>", re.S)
_PAGE_RE = re.compile(r"<page\b([^>]*)>(.*?)</page>", re.S)
_ATTR_RE = re.compile(r'([A-Za-z][\w:.-]*)\s*=\s*"([^"]*)"')


class TextLayerError(RuntimeError):
    """文字层不可用（加密 PDF / pdftotext 缺失 / 输出无法解析）。"""


# ---------------------------------------------------------------------------
# pdftotext
# ---------------------------------------------------------------------------

def pdftotext_bin():
    """定位 pdftotext。找不到就抛 TextLayerError——不能静默产出空结果。"""
    exe = shutil.which("pdftotext")
    if not exe:
        raise TextLayerError("PATH 上找不到 pdftotext（装 poppler 后把它加进 PATH）")
    return exe


def pdftotext_version(exe=None):
    """取 poppler 版本，如 "26.09.0"。取不到返回 None（producer.version 允许为 null）。"""
    try:
        r = subprocess.run([exe or pdftotext_bin(), "-v"], capture_output=True,
                           timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    blob = (r.stdout or b"") + (r.stderr or b"")   # poppler 把版本打在 stderr
    m = re.search(rb"pdftotext version (\d+(?:\.\d+)+)", blob)
    return m.group(1).decode("ascii") if m else None


def pdf_page_count(pdf):
    """用 pdfinfo 取总页数；取不到返回 None（调用方自行容错）。

    只用来把 --pages A-B 的 B 夹到实际页数：超出范围时 pdftotext 会直接报
    "Invalid page range" 退出，整个流程失败，而这本来只是用户多写了一个数字。
    """
    exe = shutil.which("pdfinfo")
    if not exe or not os.path.exists(pdf):
        return None
    try:
        r = subprocess.run([exe, pdf], capture_output=True, timeout=120,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(rb"^Pages:\s+(\d+)", r.stdout or b"", re.M)
    return int(m.group(1)) if m else None


def run_pdftotext(pdf, first=None, last=None, timeout=DEFAULT_TIMEOUT, exe=None):
    """跑 pdftotext -bbox，返回 (XHTML 文本, 实际首页页码)。

    坐标是 PDF 点，不要在这里缩放——缩放只在 build_page() 里做一次，
    免得两处各乘一遍（这类错误在小样本上看不出来，全书坐标整体放大 4 倍）。
    """
    if not os.path.exists(pdf):
        raise TextLayerError("PDF 不存在：%s" % pdf)
    cmd = [exe or pdftotext_bin(), "-bbox"]
    if first is not None:
        cmd += ["-f", str(int(first))]
    if last is not None:
        cmd += ["-l", str(int(last))]
    cmd += [pdf, "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise TextLayerError("pdftotext 超时（%.0f 秒）：%s" % (timeout, pdf))
    except OSError as e:
        raise TextLayerError("pdftotext 启动失败：%s" % e)
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        tail = ("：" + err.splitlines()[-1]) if err else ""
        # 加密 PDF 的典型表现：需要口令，我们传了 DEVNULL 所以直接失败
        raise TextLayerError("pdftotext 失败（退出码 %d，PDF 可能加密或损坏）%s"
                             % (r.returncode, tail))
    return (r.stdout or b"").decode("utf-8", "replace"), (int(first) if first else 1)


# ---------------------------------------------------------------------------
# XHTML 解析
# ---------------------------------------------------------------------------

def _attrs(s):
    return dict(_ATTR_RE.findall(s or ""))


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_bbox(text):
    """pdftotext -bbox 的 XHTML → [{w, h, words}]，坐标仍是 PDF 点。

    words 里每个是 {"x0","y0","x1","y1","t"}；空白文本的 word 直接丢掉
    （poppler 偶尔会输出只含空格的 word）。
    """
    pages = []
    for attrs, body in _PAGE_RE.findall(text or ""):
        a = _attrs(attrs)
        words = []
        for wattrs, raw in _WORD_RE.findall(body):
            w = _attrs(wattrs)
            t = html.unescape(raw or "")
            if not t.strip():
                continue
            words.append({
                "x0": _f(w.get("xMin")), "y0": _f(w.get("yMin")),
                "x1": _f(w.get("xMax")), "y1": _f(w.get("yMax")),
                "t": t,
            })
        pages.append({"w": _f(a.get("width")), "h": _f(a.get("height")),
                      "words": words})
    return pages


# ---------------------------------------------------------------------------
# 行聚类 / 行内拼接
# ---------------------------------------------------------------------------

def dedupe_words(words, min_overlap=0.5):
    """丢掉文字层里的重复框（同一份文字被输出了两遍的 PDF）。

    判据（实测见模块头第 3 条）：相邻两个 word **文本完全相同**、且水平方向
    重叠率 ≥ min_overlap。为什么不能只看"文本相同"：正文里「的的」这种叠字
    是真实存在的一一但它是同一个 word 里的连续字符，不会是两个分开的框；
    而双图层的重复框在任何文本上都成对出现，且坐标几乎重合（p50 重叠 0.84）。
    为什么不能只看"重叠"：上标/注号和正文框本来就会重叠，文本却不同。

    同一份 PDF 里重复可能有 2 层也可能更多，所以用"已保留的最后一个"作比较对象。
    """
    out = []
    for w in words:
        if out:
            p = out[-1]
            if p["t"].strip() and p["t"].strip() == w["t"].strip():
                pw = (p["x1"] - p["x0"]) or 1e-6
                ww = (w["x1"] - w["x0"]) or 1e-6
                ov = min(p["x1"], w["x1"]) - max(p["x0"], w["x0"])
                if ov / min(pw, ww) >= min_overlap:
                    continue          # 重复框，丢掉后一份
        out.append(w)
    return out


def group_lines(words, tol):
    """按**基线**（yMax，即下边缘）聚成行。tol 是 PDF 点。

    为什么不用 yMin：中文字形框的上边缘随字形变化极大（「一」「国」「之」
    的 yMin 能差 2pt 以上，实测同一行内 yMin 极差 p50=6.8pt、p99=8.6pt），
    而下边缘（基线）几乎不动（yMax 极差 p50=0、p99≈0）。用 yMin 分行会把
    同一行拆成好几段——实测德勒兹那本书 1 页被切成 68 行（真实只有 33 行），
    拼出来的句子东一段西一段。改成 yMax 之后同一页正好 33 行，拼出的段落
    与 `pdftotext` 的纯文本逐行一致。

    英文 PDF 两者都稳（Y 方向极差 p90=0），所以这个改动只影响中文，不影响英文。

    排序后逐个比对该行**首词**的值：用首词而不是"上一词"做参照，是为了避免
    一行里连续几个词逐次漂移（链式累加）把两行黏成一行的经典错误。
    """
    rows, cur, ref = [], [], None
    for w in sorted(words, key=lambda x: (x["y1"], x["x0"])):
        if ref is None or w["y1"] - ref > tol:
            if cur:
                rows.append(cur)
            cur, ref = [w], w["y1"]
        else:
            cur.append(w)
    if cur:
        rows.append(cur)
    for r in rows:
        r.sort(key=lambda x: x["x0"])
    return rows


def line_height(words):
    """该行字高：取行内最高的 word（≈ 字号）。

    用最高而不是中位数：一行里夹上标/小字时（「①」、注号），中位数会被拉低，
    补空格的阈值跟着变小，正文里就会凭空多出空格。最高值等于正文字号。
    """
    hs = [w["y1"] - w["y0"] for w in words]
    return max(hs) if hs else 1.0


def is_wide(ch):
    """该字符是否"宽字"（占一个方块位置）——决定空格判据走哪条分支。

    范围：CJK 统一汉字/扩展A/兼容汉字、CJK 标点（、。《》）、全角区、圈号（①②）。
    这些字符在排版里前后不加词间空格。拉丁字母/数字/半角标点都不算宽字——
    它们之间要按单词边界补空格。
    弯引号/破折号/省略号**故意不在这里**，它们中西文共用，看 is_ambiguous_wide()。
    """
    if not ch:
        return False
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF          # CJK 统一汉字
            or 0x3400 <= o <= 0x4DBF       # 扩展 A
            or 0xF900 <= o <= 0xFAFF       # 兼容汉字
            or 0x3000 <= o <= 0x303F       # CJK 标点：、。《》【】
            or 0xFE30 <= o <= 0xFE4F       # CJK 兼容形式
            or 0xFF01 <= o <= 0xFF60       # 全角 ASCII
            or 0xFFE0 <= o <= 0xFFE6       # 全角符号
            or 0x2460 <= o <= 0x24FF)      # 圈号 ①-⑳
    # 注意：—（0x2014）、…（0x2026）、弯引号不在这里，它们中西文共用，
    # 见 is_ambiguous_wide()


def is_ambiguous_wide(ch):
    """弯引号 / 破折号 / 省略号 —— 中西文共用，宽度得看邻居才能定。

    同一个 U+201C 在中文排版里是占一格、在英文里紧贴单词的 "：
    本书中文行的实测是「提出了 “哪个德勒兹？ ” 这个问题」——引号两侧的缝
    比汉-汉的正常字距大 0.12em（引号本身带 side bearing），照 0.3em 阈值判
    会凭空插两个空格；而英文行「devices’ classification」的 ’ 到 c 间距只有
    0.25em，被判成宽字就会把词黏起来。所以它自己不定性，**由邻居决定**：
    相邻一侧是明确宽字（汉字/CJK 标点）就按宽字处理，否则按拉丁处理。
    """
    if not ch:
        return False
    o = ord(ch)
    return (0x2018 <= o <= 0x201F or o in (0x2010, 0x2011, 0x2014, 0x2015, 0x2026))


def join_words(words, mixed_em=None):
    """行内按 x 递增拼接，按两侧字符的宽度类别决定要不要补空格。

    五条判据，按优先级依次判断（分布数字见模块头的常量注释）：

    * **右词以收尾标点开头**（’ ” ) ， 。 …，见 CLOSERS）→ 一律不补空格。
      优先级最高：收尾标点天生左贴，`devices` + `’` 的 box 首尾相接（gap=0），
      只靠间距会拼出 "devices ’ classification"。
    * **宽-宽**（汉-汉、汉-中文标点）：一律不补。中文按词切开的缝 p50=0.17em，
      补空格会在正文里插出大量空格（实测德勒兹正文 262 处）。
    * **拉丁-拉丁**（英文单词）：间距 >= 0 就补，只有框重叠才不补（见
      LATIN_SPACE_EM）。poppler 切英文时相邻框常常首尾相接甚至重叠
      （p10 = -1.59em），字距中位数只有 0.25em；用 0.3em 当门槛会把
      "other countries" 拼成 "othercountries"（实测整句黏连）。
    * **宽-拉丁 混排**：只有间距 > MIXED_SPACE_EM(0.3em) 才补——混排里既有紧贴
      的（「图4-1-1」），也有排版真空格（「第 1 页」），实测分布是双峰的。
    * 间距 > HUGE_GAP_EM(3em)：双栏/目录页码列/表格列，一律补空格，别把两栏黏上。

    另做了防双空格保护：poppler 的 word 文本本身可能自带首尾空格。
    mixed_em 只为调参与测试留的口子，正常调用不要传。
    """
    if not words:
        return ""
    ws = sorted(words, key=lambda x: x["x0"])
    h = line_height(ws) or 1.0
    out = [ws[0]["t"]]
    prev = ws[0]
    for w in ws[1:]:
        gap = (w["x0"] - prev["x1"]) / h
        # 判宽度类别用的"外侧字符"：本 word 取首字，前一个 word 取末字（都先剥空白）
        left = prev["t"].rstrip()[-1:]
        right = w["t"].lstrip()[:1]
        # 弯引号/破折号自己是"中性"的（is_ambiguous_wide），要由**邻居**定性：
        # 前一个 word 的末字如果是中性，往它内侧（倒数第 2 字）看；本 word 的
        # 首字是中性，就往后（第 2 字）看。都不行就退化成拉丁——宁可漏一个空格，
        # 也不要在中文正文里凭空插空格（中文最常见的字符就是汉字，漏判率极低）。
        lw = _resolve_wide(prev, forward=False)
        rw = _resolve_wide(w, forward=True)
        if right and right in CLOSERS:
            # 收尾标点天生左贴（不挂到前一个字上，视觉上就错开了）。
            # 这一条优先于所有间距判据，实测依据见 CLOSERS 的注释。
            sp = False
        elif gap > HUGE_GAP_EM:
            sp = True
        elif lw and rw:
            sp = False
        elif not lw and not rw:
            # >= 而不是 >：poppler 切英文时相邻框常常**首尾正好相接**（gap 恰为 0），
            # 那是两个不同的词，必须补空格。只有框重叠（gap < 0，同一单词被切成
            # 两段）才不补。实测见 LATIN_SPACE_EM 的注释。
            sp = gap >= LATIN_SPACE_EM
        else:
            sp = gap > (MIXED_SPACE_EM if mixed_em is None else mixed_em)
        if (sp and out and not out[-1].endswith((" ", "\u3000"))
                and not w["t"].startswith((" ", "\u3000"))):
            out.append(" ")
        out.append(w["t"])
        prev = w
    return "".join(out)


def _resolve_wide(word, forward):
    """这个 word 的"外侧字符"算不算宽字（见 is_ambiguous_wide）。

    forward=True：看 word 的**首**字（它右侧的缝）；False：看**末**字（左侧的缝）。
    word 的外侧字是中性的（弯引号/破折号/省略号）时，往 word 内侧找第一个非
    中性字符定性——「他说“你好」里那个 “ 的前一个外侧字是「说」（宽字），
    于是 “ 也按宽字处理，不会在「说」和「“」之间插空格。
    整个 word 都是中性字符（只有引号的行）时按拉丁处理：那种行极少，
    而误插空格比漏插更伤正文。
    """
    body = word["t"].strip()
    if not body:
        return False
    chars = body if forward else body[::-1]
    for ch in chars:
        if is_wide(ch):
            return True
        if not is_ambiguous_wide(ch):
            return False
    return False


def page_lines(page, dpi, tol_pt=LINE_TOL_PT):
    """一页的 words（PDF 点）→ 行列表（像素坐标，与 pages/*.png 同坐标系）。

    返回 [{x0,y0,x1,y1,t,s}]，按 (y0, x0) 排序，s 固定 1.0。

    **聚类必须在 PDF 点上做**：容差 tol_pt 的单位是点，而坐标也是点。
    旧版在这里乘了 dpi/72（300dpi 就是 12.5pt），结果把相距 10pt 的两行
    （页眉「Appl. Sci. 2024」与正文首行）并成一行——实测那一段被拼成
    「…9304 MDR in 2017…」。缩放只在最后输出时做一次。
    """
    scale = float(dpi) / PDF_DPI
    words = dedupe_words(page["words"])
    rows = group_lines(words, tol_pt)
    out = []
    for ws in rows:
        t = join_words(ws)
        if not t.strip():
            continue
        out.append({
            "x0": round(min(w["x0"] for w in ws) * scale, 1),
            "y0": round(min(w["y0"] for w in ws) * scale, 1),
            "x1": round(max(w["x1"] for w in ws) * scale, 1),
            "y1": round(max(w["y1"] for w in ws) * scale, 1),
            "t": t, "s": 1.0,
        })
    out.sort(key=lambda L: (L["y0"], L["x0"]))
    return out


def page_stats(lines):
    """该页质量：chars = 去掉空白后的字符数，cjk = 汉字数。

    上层用 cjk / 页数 判断「文字层够不够用」（阈值见 bookflow.py 的
    TEXT_LAYER_MIN_CJK_PER_PAGE），textlayer 只如实报数，不做裁决。
    """
    chars = cjk = 0
    for L in lines:
        for c in L["t"]:
            if c.isspace():
                continue
            chars += 1
            if "\u4e00" <= c <= "\u9fff":
                cjk += 1
    return {"chars": chars, "cjk": cjk}


def build_page(page, page_no, dpi=DEFAULT_DPI, tol_pt=LINE_TOL_PT):
    """一页 → 页级 JSON（不含 schema 章；由调用方交给 pagedoc.stamp）。

    页宽页高按 dpi 换算并四舍五入，与 pdftoppm 渲染出的 PNG 尺寸一致
    （实测 A4：595.28pt × 4.1667 = 2480，841.89pt × 4.1667 = 3508）。
    """
    scale = float(dpi) / PDF_DPI
    lines = page_lines(page, dpi, tol_pt)
    doc = {
        "name": page_name(page_no),
        "w": int(round(page["w"] * scale)),
        "h": int(round(page["h"] * scale)),
        "lines": lines,
    }
    return doc, page_stats(lines)


def page_name(no):
    """页号 → 页名。与 render 阶段的 pdftoppm 命名一致（p-001.png），
    也兼容 decisions.py 的补零容错（p-001.png.json / p-1.png.json 都能读）。"""
    return "p-%03d.png" % int(no)


# ---------------------------------------------------------------------------
# 页码区间
# ---------------------------------------------------------------------------

def parse_pages(spec, total=None):
    """--pages A-B → (first, last)；空串返回 (None, None) = 全书。

    支持 "1-60" / "5" / "5-" / "-60" 四种写法。给出 total 时把 last 夹到
    实际页数（用户多写一个数字不该让整条流程失败），但 first 超界直接报错——
    那说明页码范围写错了，静默返回 0 页会让人以为"这本书没有文字层"。
    """
    s = (spec or "").strip()
    if not s:
        return None, None
    m = re.match(r"^(\d*)\s*-\s*(\d*)$", s)
    if m:
        a = int(m.group(1)) if m.group(1) else None
        b = int(m.group(2)) if m.group(2) else None
    elif s.isdigit():
        a = b = int(s)
    else:
        raise ValueError("页码范围写法不对：%r（要 A-B / A / A- / -B）" % spec)
    if a is not None and a < 1:
        raise ValueError("页码从 1 开始，不接受 %d" % a)
    if a is not None and b is not None and b < a:
        raise ValueError("页码范围倒序：%d-%d" % (a, b))
    if total is not None:
        if a is not None and a > total:
            raise ValueError("首页 %d 超出总页数 %d" % (a, total))
        if b is not None and b > total:
            print("  注意：页码上限 %d 超过总页数 %d，按 %d 处理" % (b, total, total))
            b = total
    return a, b


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def extract(pdf, out, pages="", dpi=DEFAULT_DPI, timeout=DEFAULT_TIMEOUT,
            quiet=False):
    """主流程：PDF → <out>/p-XXX.png.json，返回 summary dict。

    返回值的 usable 为 False 时**一个文件都没写**（纯扫描书不能产出垃圾）。
    """
    t0 = time.time()
    total_pages = pdf_page_count(pdf)
    first, last = parse_pages(pages, total_pages)
    if total_pages is not None and first is not None and last is None:
        last = total_pages
    text, _actual_first = run_pdftotext(pdf, first, last, timeout)
    raw_pages = parse_bbox(text)
    if not raw_pages:
        raise TextLayerError("pdftotext 没有输出任何 <page> 元素（PDF 可能损坏）")

    base = first if first is not None else 1
    docs = []
    for i, p in enumerate(raw_pages):
        doc, st = build_page(p, base + i, dpi)
        docs.append((doc, st))

    n = len(docs)
    with_text = sum(1 for _d, st in docs if st["chars"] > 0)
    chars_total = sum(st["chars"] for _d, st in docs)
    cjk_total = sum(st["cjk"] for _d, st in docs)
    summary = {
        "pages": n,
        "with_text": with_text,
        "empty": n - with_text,
        "chars_total": chars_total,
        "cjk_total": cjk_total,
        "cjk_per_page": round(cjk_total / n, 1) if n else 0.0,
        "dpi": dpi,
        "range": [base, base + n - 1] if n else [],
        "producer": pagedoc.producer(PRODUCER, pdftotext_version()),
        "usable": with_text > 0,
        "written": 0,
        "elapsed": 0.0,
    }

    # 纯扫描书：一个可读字符都没有 → 明确报「文字层不可用」，不落任何文件
    if not summary["usable"]:
        summary["elapsed"] = round(time.time() - t0, 2)
        return summary

    prod = summary["producer"]
    os.makedirs(out, exist_ok=True)
    for doc, _st in docs:
        # 文字层抽取本来就只有全书 5.5 秒，不做增量缓存：缓存反而会让
        # 「换了 dpi / 换了 PDF」的旧产物被静默复用
        pagedoc.stamp(doc, prod, source_pdf=os.path.abspath(pdf))
        jsonio.write(os.path.join(out, doc["name"] + ".json"), doc)
        summary["written"] += 1

    summary["elapsed"] = round(time.time() - t0, 2)
    if not quiet:
        print("  文字层：%d 页（有字 %d / 空页 %d），%d 汉字，%.1f 汉字/页，%.1f 秒"
              % (n, summary["with_text"], summary["empty"], cjk_total,
                 summary["cjk_per_page"], summary["elapsed"]), flush=True)
        if summary["cjk_per_page"] < MIN_CJK_PER_PAGE:
            print("  注意：文字层偏薄（%.1f 汉字/页 < %d），建议与 OCR 交叉验证"
                  % (summary["cjk_per_page"], MIN_CJK_PER_PAGE), flush=True)
    return summary


def parse_args(argv):
    ap = argparse.ArgumentParser(description="PDF 文字层 → 页级 JSON")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dpi", type=float, default=DEFAULT_DPI)
    ap.add_argument("--pages", default="", help="页码范围 A-B（1 基，含两端）")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--force", action="store_true",
                    help="兼容 bookflow 的阶段透传（本脚本总是全量重算）")
    ap.add_argument("--quiet", action="store_true")
    return ap.parse_args(argv)


def summary_line(obj):
    """把 summary 渲染成一行 **ASCII-only** 的 SUMMARY {...}。

    为什么强制 ASCII（这是接口契约的一部分，别改回 ensure_ascii=False）：

    bookflow.py 用 subprocess.run(..., encoding="utf-8") 捕获这一行。Windows 上
    子进程 stdout 默认是 GBK，本模块虽然自己 reconfigure 了，但**调用方的一致性
    不能指望**——实测父进程拿 GBK 字节按 utf-8 解码会直接抛
    UnicodeDecodeError（不是乱码，是异常，整个阶段崩掉）。
    把机器可读的那一行做成纯 ASCII（非 ASCII 一律 \\uXXXX 转义，json 解析回来
    仍是原来的中文），就与编码彻底解耦：GBK、UTF-8、ASCII 下字节完全相同。

    人类可读的中文照旧走 stderr / 普通 print（那些行只给人看，编码坏了不影响流程）。
    """
    return "SUMMARY " + json.dumps(obj, ensure_ascii=True)


def main(argv=None):
    _utf8_stdio()
    a = parse_args(argv)
    try:
        summary = extract(a.pdf, a.out, pages=a.pages, dpi=a.dpi,
                          timeout=a.timeout, quiet=a.quiet)
    except TextLayerError as e:
        print("文字层不可用：%s" % e, file=sys.stderr)
        print(summary_line(
            {"pages": 0, "with_text": 0, "empty": 0, "cjk_total": 0,
             "cjk_per_page": 0.0, "usable": False, "written": 0,
             "error_code": error_code(e), "error": str(e)}))
        return 3
    except ValueError as e:
        print("参数错误：%s" % e, file=sys.stderr)
        print(summary_line(
            {"pages": 0, "with_text": 0, "empty": 0, "cjk_total": 0,
             "cjk_per_page": 0.0, "usable": False, "written": 0,
             "error_code": "BAD_ARGS", "error": str(e)}))
        return 4
    if not summary["usable"]:
        print("文字层不可用：%d 页里没有一个可读字符（纯扫描件？请走 OCR）"
              % summary["pages"], file=sys.stderr)
    print(summary_line(summary))
    return 0 if summary["usable"] else 2


# 机器可读的错误码：让上层脚本不必去猜中文文案，也不必做字符串匹配。
_ERROR_CODES = (
    ("\u627e\u4e0d\u5230 pdftotext", "NO_PDFTOTEXT"),
    ("PDF \u4e0d\u5b58\u5728", "PDF_MISSING"),
    ("\u8d85\u65f6", "TIMEOUT"),
    ("\u542f\u52a8\u5931\u8d25", "SPAWN_FAILED"),
    ("\u6ca1\u6709\u8f93\u51fa\u4efb\u4f55", "EMPTY_OUTPUT"),
    ("\u5931\u8d25", "PDFTOTEXT_FAILED"),
)


def error_code(exc):
    """把 TextLayerError 的文案归成稳定的 ASCII 错误码。"""
    msg = str(exc)
    for needle, code in _ERROR_CODES:
        if needle in msg:
            return code
    return "UNKNOWN"


def _utf8_stdio():
    """把本进程的 stdout/stderr 强制成 UTF-8。

    必须做：bookflow.py 用 `subprocess.run(..., encoding="utf-8")` 捕获我们的
    stdout，而 Windows 上子进程默认继承的是 **GBK**（cp936），中文输出会被父进程
    按 UTF-8 解码成乱码（实测「文字层：40 页…」整行变成「���ֲ㣺40 ҳ…」）。
    SUMMARY 行虽然本身是纯 ASCII，但失败分支里的 `error` 字段是中文，同样会坏。
    这里自己修，不指望调用方记得加 -X utf8。
    """
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass          # 被重定向成不可重配的对象时忽略（测试里就是这样）


if __name__ == "__main__":
    _utf8_stdio()
    sys.exit(main())
