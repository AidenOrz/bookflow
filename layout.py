"""bookflow.layout — 版面清理、段落重组、印刷目录驱动的章节还原。

设计要点：
1. 清洗：去页眉页脚（按跨页重复频率学习）、去边缘竖排字（书脊字）、去页码
2. 合并：同一视觉行被切成多个框时按间距合并
3. 段落：首行缩进 > 0.9 字宽 起新段；行尾明显短于版心宽度 视为段尾
4. 章节：不靠字号猜，先用印刷目录页得到权威条目与层级，再在正文顺序匹配
   （匹配策略移植自 jarodise/pdf2epub-paddle，并加入"标题性"判定与页码约束）
"""
import os
import re
import json
import glob
import statistics
from difflib import SequenceMatcher
import jsonio

CJK = re.compile(r"[\u4e00-\u9fff]")
PUNCT_END = "。！？；：…”』」）)】》!?.;:"
PAGE_NO = re.compile(r"^[（(\[【]?\s*[-—–]?\s*(\d{1,4})\s*[-—–]?\s*[)）\]】]?$")
TOC_HEAD = re.compile(r"^目\s*录$|^目\s*次$|^contents$", re.I)

# 版面分析（run_layout.py）标出的、不该进正文的区域类型
DROP_REGIONS = {"footnote", "vision_footnote", "footer", "header",
                "page_header", "page_footer", "number", "page_number"}

# 脚注区域：同样不进正文流，但要单独抽出来渲染成弹窗脚注
NOTE_REGIONS = {"footnote", "vision_footnote"}
# 扫描页底部的试用版水印，常被判成脚注区，必须剔掉
WATERMARK = re.compile(r"pdfFactory|fineprint|试用版本", re.I)
# 正文里的注号：①-⑳（U+2460..U+2473）最常见，㉑（U+3251..）起兼容，
# 另外兼容上标数字。它也会误伤正文里的 ① 列表符号——那种情况下注号数与
# 脚注条数对不上，plan_notes 会自动退化成页尾注释块，不会错配。
MARK_RE = re.compile("[①-⑳㉑-㉟]|[⁰-ₜ]+")

L1_PAT = re.compile(
    r"^(?:第\s*[零一二三四五六七八九十百千两0-9]+\s*[讲章节篇卷部编]"
    r"|(?:序|序言|自序|代序|前言|绪论|导论|引言|楔子|后记|跋|结语|结论|尾声"
    r"|附录|附表|索引|参考书目|参考文献|书目|致谢|译后记|出版说明)"
    r"|(?:chapter|part|lecture|book|section|preface|introduction|prologue|epilogue|appendix|index|bibliography)\b)", re.I)
L2_PAT = re.compile(r"^[零一二三四五六七八九十]+\s*[、，,．.:：·～~]")
L3_PAT = re.compile(r"^\d{1,2}\s*[、，,．.:：·～~]")


def elen(s):
    """视觉长度：中文 1，其余 0.55。"""
    n = 0.0
    for c in s:
        n += 1.0 if CJK.match(c) else 0.55
    return max(n, 0.6)


def norm(s):
    """归一化用于相似度比较：只留中文数字字母。"""
    return re.sub(r"[^\u4e00-\u9fff0-9a-zA-Z]", "", s)


# ---------------------------------------------------------------- 脚注
NOTE_HEAD = re.compile(
    r"^\s*(?:[（(]\s*\d{1,2}\s*[)）]|\d{1,2}\s*[.、．)）]|[ilI]\s*[.、．)）]"
    r"|[\u2460-\u2473\u3251-\u325f]|[\u2070-\u209c]+)\s*")

# 注号被 OCR 吃成「1《晋书》」「(1《晋书》」这种"数字 + 无分隔符"的样子。
# 单独一条规则而不是并进 NOTE_HEAD：并进去会让「1980年」这类正文开头也匹配，
# 所以这里额外要求数字后面紧跟书名号/括号，且后面不是数字。
NOTE_HEAD2 = re.compile(r"^\s*[（(]?\s*(\d{1,2}|[ilI])\s*[.、．，,:：)）]?\s*(?=[《〈（(])")


def mark_value(s):
    """注号字符串 → 整数；认不出返回 None。"""
    s = s.strip()
    if not s:
        return None
    if len(s) == 1:
        o = ord(s)
        if 0x2460 <= o <= 0x2473:
            return o - 0x2460 + 1
        if 0x3251 <= o <= 0x325F:
            return o - 0x3251 + 21
        if 0x2070 <= o <= 0x209C:
            return "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079".index(s)
    # 数字注号可能带各种包裹/分隔符：「(1」「1)」「1.」「1、」「（12）」。
    # 前面已经处理过单字符（圈号/上标），到这里剩下的都是数字或 i/l 的形态。
    m = re.match(r"^[（(]?\s*(\d{1,2}|[iIl])\s*[.、．，,:：)）]?\s*$", s)
    if m:
        g = m.group(1)
        return 1 if g in "iIl" else int(g)
    return None


def split_note_head(t):
    """拆出注号：返回 (编号, 注号原文, 正文)。认不出返回 (None, "", t)。

    扫描件的注号常被叠字/串行污染，典型是「2、③《高僧传》…」——行首的 `2、`
    是上一行注文的尾巴，真正的注号是后面的 ③。判定顺序因此是：**先看圈号**
    （本书正文用的就是圈号），只有完全没有圈号时才认阿拉伯数字形态。
    反过来（见数字就用）会把 `2、` 当成注号，注文里就多出一个 `③` 前缀。
    """
    if not t:
        return None, "", t
    # ① 行首（可隔着 `2、` 这种垃圾前缀）的圈号优先
    m = re.search("[\u2460-\u2473\u3251-\u325f]", t[:6])
    if m and not re.search("[\u4e00-\u9fff]", t[:m.start()]):
        return mark_value(m.group(0)), m.group(0), t[m.end():]
    # ② 再认数字/字母形态
    for pat in (NOTE_HEAD, NOTE_HEAD2):
        mm = pat.match(t)
        if mm and len(t) > mm.end():
            n = mark_value(mm.group(0))
            if n is not None:
                return n, mm.group(0).strip(), t[mm.end():]
    return None, "", t


def extract_notes(pages):
    """从带 region 标签的 OCR 结果里抽出脚注：[{pi: [ {id,n,mark,text} ]}]。

    只认 region 为 footnote/vision_footnote 的行——没有跑过 run_layout.py 的
    工作目录里一个 region 字段都没有，这个函数就返回空表，全书行为不变。

    一条脚注在版面上常折成 2~4 行，续行没有注号，要拼回去；拼的时候不加分隔符，
    因为中文折行处本来就没有空格（加了反而在字中间插入空格）。
    """
    from collections import OrderedDict
    out = OrderedDict()
    seq = 0
    for p in pages:
        rows = [L for L in p["lines"] if L.get("region") in NOTE_REGIONS]
        if not rows:
            continue
        rows.sort(key=lambda L: (L["y0"], L["x0"]))
        items = []
        for L in rows:
            t = L["t"].strip()
            if not t or WATERMARK.search(t):
                continue
            n, mark, rest = split_note_head(t)
            if mark and rest:
                items.append({"n": n, "mark": mark, "text": rest})
            elif items:
                items[-1]["text"] += t
            else:
                # 首行就没注号：多半是注号被 OCR 吃掉了，仍按一条落盘
                items.append({"n": None, "mark": "", "text": t})
        keep = []
        for it in items:
            it["text"] = it["text"].strip()
            if len(re.sub(r"[^\u4e00-\u9fff]", "", it["text"])) < 2:
                continue        # 只有符号/书名号的碎片，不要
            it["id"] = "fn%d" % (seq + 1)
            seq += 1
            keep.append(it)
        if keep:
            out[p["idx"]] = keep
    return dict(out)


def plan_notes(notes, cleaned):
    """决定每条注能不能内联锚定。

    返回 (subs, orphan)：
      subs   {(page, line): [(char_start, char_end, note_id)]}  按字符区间替换成 [^id]
      orphan {page: [note]}                                     无法定位的注

    判据只有一条，但很严格：**同一页里，正文恰好出现一个该编号的注号，且脚注区
    恰好有一条该编号的注**。相邻页也参与（脚注常因版面溢出挪到下一页），但只有
    在本页找不到时才启用。

    为什么不用"注数 == 注号数 → 按顺序配对"：正文里的 ①②③ 也可能是并列项
    编号（本书大量如此），一旦按顺序配对，会把并列项错接成脚注，读者点开看到
    完全不相干的引文。宁可少内联，也不能错配。
    """
    spots = {}
    for pi in sorted(cleaned):
        for li, L in enumerate(cleaned[pi]):
            for m in MARK_RE.finditer(L["t"]):
                # 行首的编号几乎必定是并列项符号，不是注号：正文的注号总是
                # 紧跟在引文/句子后面，不可能出现在一行的第一个字符。
                if m.start() == 0:
                    continue
                v = mark_value(m.group(0))
                if v is None:
                    continue
                spots.setdefault(pi, []).append((li, m.start(), m.end(), v))

    by_page_val = {}
    for pi, items in notes.items():
        for it in items:
            if it.get("n") is None:
                continue
            by_page_val.setdefault((pi, it["n"]), []).append(it)

    subs, used, seen_val = {}, set(), {}
    for pi in sorted(spots):
        for (li, s, e, v) in spots[pi]:
            seen_val.setdefault(pi, {}).setdefault(v, 0)
            seen_val[pi][v] += 1
    for pi in sorted(spots):
        for (li, s, e, v) in spots[pi]:
            if seen_val[pi].get(v, 0) != 1:
                continue          # 同一编号在本页正文出现多次 → 分不清，不内联
            cand = None
            for q in (pi, pi - 1, pi + 1):
                hit = [x for x in by_page_val.get((q, v), []) if x["id"] not in used]
                if len(hit) == 1:
                    cand = hit[0]
                    break
                if len(hit) > 1:
                    cand = None
                    break
            if cand is None:
                continue
            subs.setdefault((pi, li), []).append((s, e, cand["id"]))
            used.add(cand["id"])

    orphan = {}
    for pi, items in notes.items():
        rest = [it for it in items if it["id"] not in used]
        if rest:
            orphan[pi] = rest
    return subs, orphan


def apply_subs(t, subs):
    """按字符区间把注号替换成 [^id]；区间来自原文，必须倒序替换。"""
    for s, e, nid in sorted(subs, key=lambda x: -x[0]):
        t = t[:s] + "[^%s]" % nid + t[e:]
    return t


def load_pages(src):
    pages = []
    for f in sorted(glob.glob(os.path.join(src, "*.json"))):
        d = jsonio.read(f)
        d["idx"] = len(pages)
        pages.append(d)
    return pages


def metrics(pages):
    """估计版心字宽 bw 与行高 bh。"""
    bws, bhs = [], []
    for p in pages:
        W = p["w"]
        for L in p["lines"]:
            wpx = L["x1"] - L["x0"]
            hpx = L["y1"] - L["y0"]
            if wpx > 0.45 * W and len(L["t"]) >= 15:
                bws.append(wpx / elen(L["t"]))
                bhs.append(hpx)
    bw = statistics.median(bws) if bws else 20.0
    bh = statistics.median(bhs) if bhs else 30.0
    return bw, bh


def learn_running_heads(pages, bh):
    """跨页重复的页眉/页脚/水印文本：在 ≥5 页的顶部/底部出现即判为版式元素。

    长度上限放宽到 60：扫描件常带整行水印（如
    「PDF 文件使用 "pdfFactory" 试用版本创建 www.fineprint.com.cn」约 40 字），
    旧上限 30 会把它们漏掉，于是水印漏进正文。
    """
    from collections import defaultdict
    seen = defaultdict(set)
    for p in pages:
        H = p["h"]
        for L in p["lines"]:
            if L["y1"] < 0.11 * H or L["y0"] > 0.90 * H:
                t = L["t"].strip()
                k = norm(t)
                # 纯符号/单字的归一化结果不可靠，跳过
                if 2 <= len(k) <= 60 and len(t) <= 80:
                    seen[k].add(p["idx"])
    return {k for k, s in seen.items() if len(s) >= 5}


def detect_toc_pages(pages, bh, scan=30):
    """在原始行上判定印刷目录页（清洗前，避免页码被删）。

    命中「目录/目次」标题的页直接算；否则要求该页有 ≥6 行以页码结尾，**并且**
    下一页也满足（或本身已是最后一页）。连续性的约束很关键：正文页偶尔会有
    引用年份「（1985）」这类行，单看一页容易误判；而目录总是连续好几页，
    且被误判的页会被 match_entries/assemble 整页跳过，正文会静默消失。
    """
    def looks_toc(p):
        texts = [L["t"].strip() for L in p["lines"]]
        if any(TOC_HEAD.match(t.replace(" ", "")) for t in texts):
            return True
        num = sum(1 for t in texts
                  if re.search(r"[（(\[【]?\s*\d{1,4}\s*[)）\]】]\s*$", t) and elen(t) <= 40)
        return num >= 6

    ids = set()
    cand = [p for p in pages[:scan] if looks_toc(p)]
    for i, p in enumerate(cand):
        # 标题命中的，单页即可认定
        texts = [L["t"].strip() for L in p["lines"]]
        if any(TOC_HEAD.match(t.replace(" ", "")) for t in texts):
            ids.add(p["idx"])
            continue
        # 靠"行尾页码"判定的，要求前后至少还有一页也像目录（连续 2 页）
        prev_ok = i > 0 and cand[i - 1]["idx"] == p["idx"] - 1
        next_ok = i + 1 < len(cand) and cand[i + 1]["idx"] == p["idx"] + 1
        if prev_ok or next_ok:
            ids.add(p["idx"])
    return ids


def head_band(pages, heads, bh):
    """从实际数据估计「页眉/页脚带状区」的上下边界（占页高比例）。

    书眉的位置因书而异：本书书眉止于 7.76% 页高，而正文首行最低到 9.98%，
    它们的间隙只有 2 个百分点。任何写死的常数（如 10.5%）迟早会切到正文。
    这里改为从数据里学：取所有命中书眉的行的 y 上界，再留一点余量。

    返回 (band_top, band_bot)，都是 0~1 的比例。
    """
    tops, bots = [], []
    for p in pages:
        H = p["h"]
        for L in p["lines"]:
            if norm(L["t"]) not in heads:
                continue
            if L["y1"] < 0.25 * H:
                tops.append(L["y1"] / H)
            elif L["y0"] > 0.75 * H:
                bots.append(L["y0"] / H)
    # 书眉下沿的实测上界，留 1 个百分点的余量（吸收扫描倾斜/OCR 框抖动）。
    # 上限压到 9%：本书正文首行实测最低到 9.98%，再高就会开始吃正文。
    if tops:
        band_top = min(max(tops) + 0.01, 0.09)
    else:
        band_top = 0.085
    if bots:
        band_bot = max(min(bots) - 0.01, 0.94)
    else:
        band_bot = 0.93
    return band_top, band_bot


def clean_page(p, bw, bh, heads, keep_nums=False, band_top=0.085, band_bot=0.93):
    W, H = p["w"], p["h"]
    # 版心左右边界：取整行（宽度 > 60% 页宽）的中位位置，比固定百分比稳
    full = [L for L in p["lines"] if L["x1"] - L["x0"] > 0.60 * W]
    if full:
        bl = sorted(L["x0"] for L in full)[len(full) // 2]
        br = sorted(L["x1"] for L in full)[len(full) // 2]
    else:
        bl, br = 0.12 * W, 0.88 * W
    out = []
    for L in p["lines"]:
        t = L["t"].strip()
        if not t:
            continue
        wpx = L["x1"] - L["x0"]
        hpx = L["y1"] - L["y0"]
        # 版面分析给出的区域标签：脚注/页脚/页码/页眉一律不进正文流。
        # 这是**可选增强**——没有 region 字段的老 JSON 行为完全不变，
        # 所以历史产物和未跑 run_layout.py 的工作目录都照旧。
        if L.get("region") in DROP_REGIONS:
            continue
        # 边缘竖排字（书脊）：细高且贴边
        if hpx > 2.0 * max(wpx, 1) and (L["x1"] < 0.16 * W or L["x0"] > 0.84 * W):
            continue
        # 边栏单字（书脊竖排书名逐字识别）：按**版心边界**判断，不用固定比例。
        # 固定 0.85W 会在书脊字刚好压线时漏网（本书 2169 vs 2176，差 7px）。
        if elen(t) <= 2 and (L["x1"] < bl - 0.5 * bw or L["x0"] > br - 0.5 * bw):
            continue
        # 页码：只在版心上下边缘处丢弃（目录页保留）
        if not keep_nums and PAGE_NO.match(t) and (L["y1"] < 0.12 * H or L["y0"] > 0.88 * H):
            continue
        # 页面中部的纯数字短行只可能是页边页码，必须落在版心外才丢；
        # 否则居中引文的年份（「1985」）这类正文会被误删。
        if (not keep_nums and PAGE_NO.match(t) and len(t) <= 6
                and (L["x1"] < bl or L["x0"] > br)):
            continue
        # 跨页重复的版式文本（书眉、水印、出版社名）：命中即删，**不看页内行数**。
        # 旧代码把它放在位置判断之后，且整段受 len(p["lines"]) > 3 保护，于是
        # "整页只剩水印"的空白页（实测 p054）会把水印留下并输出成正文。
        if norm(t) in heads:
            continue
        # 但"位置带"判断不能在页行数很少时生效：只有标题/副标题的章节扉页
        # （如「黑格尔哲学与中国传统思维」独占页首）位于顶部带内，是正文的一部分。
        small_page = len(p["lines"]) <= 3
        # 页眉页脚：版心上下带状区域内的行（书名/章名/页码）一律视为版式元素。
        # 阈值不能硬编码：本书书眉实测只到 7.76% 页高，而正文首行最低可到 9.98%，
        # 固定 10.5% 会切在正文行中间（曾丢 5 行正文）。这里由 band_top/band_bot
        # 两个自适应参数给入（见 head_band()）。
        if (L["y1"] < band_top * H or L["y0"] > band_bot * H) and not small_page:
            continue
        out.append(dict(L, t=t, w=wpx, h=hpx))

    # 同一视觉行合并
    out.sort(key=lambda L: (L["y0"], L["x0"]))
    merged = []
    for L in out:
        placed = False
        # 回看窗口不能写死成 merged[-4:]：同一行被切成 5+ 个片段、或中间夹了别的行时，
        # 会"饥饿"到把本该合并的片段拆开。这里回看所有中心线够近的块（按 cy 过滤）。
        cy2 = (L["y0"] + L["y1"]) / 2
        for M in reversed(merged):
            cy1 = (M["y0"] + M["y1"]) / 2
            if cy1 < cy2 - bh:      # 已经回看到更早的行，不可能再匹配
                break
            if abs(cy1 - cy2) < 0.45 * bh:
                gap = L["x0"] - M["x1"]
                # 只合并右侧紧邻的片段；相隔很远（目录标题与页码）或左侧片段不合并
                if -0.4 * bw <= gap < 1.6 * bw:
                    sep = "　" if gap > 0.5 * bw else ""
                    M["t"] = M["t"] + sep + L["t"]
                    M["x1"] = max(M["x1"], L["x1"])
                    M["y1"] = max(M["y1"], L["y1"])
                    M["x0"] = min(M["x0"], L["x0"])
                    M["w"] = M["x1"] - M["x0"]
                    M["h"] = M["y1"] - M["y0"]
                    # 置信度取两段的最小值：否则低置信片段被高置信片段掩盖，
                    # qa 的 LOW_SCORE 永远报不出来
                    if "s" in M and "s" in L:
                        M["s"] = min(M["s"], L["s"])
                    placed = True
                    break
        if not placed:
            merged.append(L)
    merged.sort(key=lambda L: (L["y0"], L["x0"]))
    return merged


NUM_MARK = re.compile(r"^(第\s*[零一二三四五六七八九十百千两0-9]+\s*[讲章节篇卷部编]|[零一二三四五六七八九十]+|\d{1,2})")
PUNCT_TAIL = re.compile(r"^[\s、，,．.:：·～~\-\u3000]*")


def split_marker(s):
    """拆出标题的前导编号（第X讲 / 一 / 1）与其后正文。"""
    t = s.strip()
    m = NUM_MARK.match(t)
    if not m:
        return None, PUNCT_TAIL.sub("", t)
    rest = PUNCT_TAIL.sub("", t[m.end():])
    return re.sub(r"\s", "", m.group(1)), rest



def unify_title(toc, body):
    """合并目录条目与正文命中行：取更完整者，编号以目录为准。"""
    nt, nb = norm(toc), norm(body)
    if not nb:
        return toc
    if not nt:
        return body
    if nb == nt:
        return body
    # 一侧是另一侧的子串：取更长的
    if nb in nt and len(nt) > len(nb):
        return toc
    if nt in nb and len(nb) > len(nt):
        return body
    mt, rt = split_marker(toc)
    mb, rb = split_marker(body)
    if mt and mb and mt != mb:
        # 编号不一致：编号取目录（层级语义更可靠），正文取正文
        return (mt + ("、" if not re.match(r"^第", mt) else "　") + rb) if rb else toc
    if mt and not mb:
        return toc if len(nt) > len(nb) - 2 else body
    return body if len(nb) >= len(nt) else toc
def char_width(L):
    return L["w"] / elen(L["t"])


def heading_score(L, bw, bh):
    """0~1：越大越像标题。大字号 + 独占一行 + 短。"""
    cw = char_width(L)
    s = 0.0
    if cw > 1.35 * bw:
        s += 0.6
    elif cw > 1.12 * bw:
        s += 0.35
    if elen(L["t"]) <= 26:
        s += 0.25
    if L["t"][-1] not in PUNCT_END:
        s += 0.15
    return min(s, 1.0)


# ---------------------------------------------------------------- 目录页解析
def parse_toc(pages, cleaned, bw, bh, toc_ids):
    """解析已判定的印刷目录页，得到条目与层级。"""
    entries = []
    for p in pages:
        if p["idx"] not in toc_ids:
            continue
        lines = cleaned[p["idx"]]
        if not lines:
            continue
        W = p["w"]
        # 按行聚类（同一视觉行）
        rows, cur = [], []
        for L in lines:
            if TOC_HEAD.match(L["t"].replace(" ", "")):
                continue
            if cur and abs(L["y0"] - cur[0]["y0"]) > 0.6 * bh:
                rows.append(cur)
                cur = [L]
            else:
                cur.append(L)
        if cur:
            rows.append(cur)

        pend = None      # 累积未闭合的条目标题
        for row in rows:
            row.sort(key=lambda L: L["x0"])
            # 右侧页码
            page_no, title_parts = None, []
            for L in row:
                m = re.search(r"[（(\[【]?\s*(\d{1,4})\s*[)）\]】]?\s*$", L["t"])
                # 行尾是页码（可能带括号，也可能紧跟在引导点之后）即视为页码列
                # 页码列判据：① 位于页面右半侧；② 或页码前有引导点（………… / ……　·）
                # 注意不能要求"连续 ≥2 个引导点"：有些排版是「……　· ( 54 )」，
                # 全角空格会把点串打断，那样整行会被误当标题并粘到下一条上。
                if m and (L["x0"] > 0.5 * W
                          or re.search(r"[.…．·]", L["t"][:m.start()])):
                    page_no = int(m.group(1))
                    head = L["t"][:m.start()].strip(" .…．·")
                    if head:
                        title_parts.append((L["x0"], head))
                elif m and L["t"].strip().startswith(("(", "（")) and len(L["t"]) <= 8:
                    page_no = int(m.group(1))
                else:
                    title_parts.append((L["x0"], L["t"]))
            title_parts.sort()
            title = "".join(t for _, t in title_parts)
            # 引导点连写是版面装饰；单个的 `…` 是标题自带的分隔（「序…牟钟鉴」），
            # 只在它前后都是空白/行首行尾时才算装饰。
            title = re.sub(r"[.．·\-—_]{2,}|…{2,}", "", title)
            title = re.sub(r"^[.…．·\-—_]+|[.…．·\-—_]+$", "", title.strip())
            title = re.sub(r"[\s…·.．]+$", "", title).strip(" 、，,.．·:：-—")
            x0 = title_parts[0][0] if title_parts else 0
            if not title:
                continue
            # 「目 录」两个大字常被 OCR 拆成两行/两块，且字号大到会被单独聚成一行。
            # 它们不是条目，留着会把下一条真正条目标题粘成「目序…牟钟鉴」。
            if title.strip() in ("目", "录", "目次", "目录"):
                continue

            if page_no is None:
                # 无页码：要么是上一条的折行续接，要么是新条目的首行（印刷排版把页码放到折行上）
                looks_new = bool(L1_PAT.match(title) or L2_PAT.match(title) or L3_PAT.match(title))
                if pend is None:
                    pend = {"title": title, "x0": x0}
                elif looks_new and len(pend["title"]) >= 2:
                    # 上一条没有页码行，直接落盘（页码缺失）
                    entries.append(_mk_entry(pend, None, W, p["idx"]))
                    pend = {"title": title, "x0": x0}
                else:
                    pend["title"] += title
                continue

            # pend 可能是「上一条的折行续接」，也可能本身就是一条缺页码的完整条目
            # （如「第二节 基层僧官」的页码在 OCR 里丢了）。若 pend 已经匹配层级前缀、
            # 而当前行同样匹配层级前缀，则视为两条独立条目，不能拼成
            # 「第二节基层僧官第三节 尼僧僧官…」这种粘连标题。
            if pend and _looks_entry(pend["title"]) and _looks_entry(title):
                entries.append(_mk_entry(pend, None, W, p["idx"]))
                pend = None
            full_x0 = pend["x0"] if pend else x0
            full = (pend["title"] + title) if pend else title
            entries.append(_mk_entry({"title": full, "x0": full_x0}, page_no, W, p["idx"]))
            pend = None
        if pend is not None and len(pend["title"]) >= 2:
            entries.append(_mk_entry(pend, None, W, p["idx"]))
    return assign_levels(entries, W)


def _looks_entry(title):
    """该行是否像一条目录条目的开头（有层级前缀）。

    只把「第X章/节」「一、」「1.」这类前缀当作条目开头，避免把折行续接误判成新条目。
    """
    t = title.strip()
    return bool(L1_PAT.match(t) or L2_PAT.match(t) or L3_PAT.match(t))


def _mk_entry(pend, page_no, W, toc_page):
    return {"title": pend["title"], "page": page_no, "level": 0,
            "x0": pend["x0"], "toc_page": toc_page}


# 标题前缀的"层级名次"。名次只用来排序，真实层级由 assign_levels() 映射成
# 本书实际出现的几档。列表顺序即优先级：`第X节` 必须排在通用 L1 模式之前，
# 否则它先被 L1_PAT 命中，和 `第X章` 归成同一名次，章与节就被拍平了。
# 编号字符类里带上 `-—–iIl`：扫描件把「第一节」认成「第-节」是常事。
RANKS = [
    (re.compile(r"^第\s*[零一二三四五六七八九十百千两0-9\-—–iIl]{1,3}\s*节"), 20),
    (re.compile(r"^第\s*[零一二三四五六七八九十百千两0-9\-—–iIl]{1,3}\s*[讲章篇卷部编]"), 10),
    # 无编号的独立标题（序言/绪论/后记/参考书目…）与章同级：
    # 少了这一档，旧书的「参考书目」会因为没有编号而按缩进降到二级。
    (L1_PAT, 10),
    (re.compile(r"^[零一二三四五六七八九十]+\s*[、，,．.:：·～~]"), 30),
    (re.compile(r"^\d{1,2}\s*[、，,．.:.:：·～~]"), 40),
]


def _rank(t):
    for pat, r in RANKS:
        if pat.match(t.strip()):
            return r
    return None


def assign_levels(entries, W):
    """把目录条目映射成 1/2/3 级。

    不能只看前缀本身：`第X章` 和 `第X节` 都命中 L1_PAT，直接按 L1_PAT 判会把
    节也标成一级，整本书的层级被拍平成一个列表（实测新书 36 个节全部变成一级，
    目录里章与节并列）。正确做法是先按前缀名次排序，再映射成本书**实际出现**
    的几档。这样「讲 / 一、 / 1.」和「章 / 节 / 一、」都能各自映射成 1/2/3。

    没有前缀的条目（折行残留、`（附说）…` 这类）按缩进就近归级——缩进是
    印刷目录里最忠实的层级信号，比任何正则都可靠。
    """
    ranks = sorted({r for e in entries for r in [_rank(e["title"])] if r})
    lvl_of_rank = {r: min(i + 1, 3) for i, r in enumerate(ranks)}
    # 各级的缩进样板：只用有前缀的条目，避免被无前缀的杂项带偏
    samples = {}
    for e in entries:
        r = _rank(e["title"])
        if r is None:
            continue
        lv = lvl_of_rank[r]
        e["level"] = lv
        samples.setdefault(lv, []).append(e["x0"])
    # 没有前缀的条目（折行残片 `（附说）…`、跑到右侧页码列的尾巴）按"跟随上一个
    # 有前缀的条目"来定级。缩进在这里不可靠：占位标题居中排版，x0 会与缩进的
    # 节标题重合（实测「（附说）」x0=401 同时落在二级 310~422 和三级 368~526
    # 两个区间里），按缩进判只能靠运气。目录本身是**顺序**结构，前一条就是它
    # 的父级，这个信号比任何几何量都稳。
    last = 1
    for e in entries:
        if e["level"]:
            last = e["level"]
        else:
            e["level"] = last
    return entries


# ---------------------------------------------------------------- 正文匹配
def match_entries(pages, cleaned, entries, toc_ids, bw, bh):
    """顺序单调匹配：每条目录条目在正文中向后搜索最相似的行。"""
    stream = []  # (page_idx, line_idx)
    for p in pages:
        if p["idx"] in toc_ids:
            continue
        for j, L in enumerate(cleaned[p["idx"]]):
            stream.append((p["idx"], j))

    matched = []
    cursor = 0
    for ei, e in enumerate(entries):
        tgt = norm(e["title"])
        if len(tgt) < 2:
            continue
        best = None
        best_score = 0.0
        # 搜索窗口：从上次命中往后 900 行
        for k in range(cursor, min(cursor + 900, len(stream))):
            pi, li = stream[k]
            L = cleaned[pi][li]
            cand = norm(L["t"])
            if not cand:
                continue
            # 标题在正文里可能折成两行（「第三节都司的机构设置与都僧统、」+
            # 「都教授的职权及其运转机制」），单行比值只有 0.70，会被门槛挡掉。
            # 所以先判长度时就要把「本行 + 下一行」的拼接长度算进去，
            # 再拿拼接后的串做相似度比较。
            cand2 = cand
            if k + 1 < len(stream):
                pj, lj = stream[k + 1]
                if pj == pi:
                    cand2 = cand + norm(cleaned[pj][lj]["t"])
            if (len(cand) > len(tgt) + 12 or len(cand) < len(tgt) - 10) and len(cand2) > len(cand):
                # 单行被长度门槛挡掉时，用拼接行再试一次
                if len(cand2) > len(tgt) + 12 or len(cand2) < len(tgt) - 10:
                    continue
                r = SequenceMatcher(None, cand2, tgt).ratio()
                if r >= 0.62:
                    cand = cand2
            elif len(cand) > len(tgt) + 12 or len(cand) < len(tgt) - 10:
                continue
            else:
                r = SequenceMatcher(None, cand, tgt).ratio()
                if r < 0.62 and len(cand2) > len(cand):
                    r2 = SequenceMatcher(None, cand2, tgt).ratio()
                    if r2 > r:
                        r, cand = r2, cand2
            if r < 0.62:
                continue
            hs = heading_score(L, bw, bh)
            # 精确一致（去掉标点空白后完全相同）优先，且不受"比当前最佳高 0.06"
            # 的抑制——否则第一条合理命中一旦占位，后面分数更低的真命中就再也选不上。
            if cand == tgt:
                best_score = max(best_score, r + 0.55 * hs + 0.45 * hs + 0.5)
                best = k
                break
            score = r + 0.55 * hs
            if e["level"] == 1:
                score += 0.45 * hs
            if score > best_score + 0.06:
                best_score = score
                best = k
        if best is None or best_score < 0.72:
            continue
        pi, li = stream[best]
        matched.append({
            "ei": ei,
            "title": e["title"],
            "level": e["level"],
            "printed_page": e["page"],
            "page": pi,
            "line": li,
            "score": round(best_score, 3),
            "body_text": cleaned[pi][li]["t"],
            "pos": best,
        })
        cursor = best + 1

    # 第二遍：未命中的条目在相邻已命中项之间的小窗口内补搜（允许倒退）
    done = {m["ei"] for m in matched}
    for ei, e in enumerate(entries):
        if ei in done:
            continue
        tgt = norm(e["title"])
        if len(tgt) < 2:
            continue
        lo = max((m["pos"] for m in matched if m["ei"] < ei), default=0)
        hi = min((m["pos"] for m in matched if m["ei"] > ei), default=len(stream) - 1)
        lo = max(0, lo - 60)
        hi = min(len(stream) - 1, hi + 60)
        best, best_score = None, 0.0
        for k in range(lo, hi + 1):
            pi, li = stream[k]
            L = cleaned[pi][li]
            cand = norm(L["t"])
            if not cand or len(cand) > len(tgt) + 12 or len(cand) < len(tgt) - 10:
                continue
            r = SequenceMatcher(None, cand, tgt).ratio()
            if r < 0.72:
                continue
            sc = r + 0.55 * heading_score(L, bw, bh)
            if sc > best_score + 0.06:
                best, best_score = k, sc
        if best is not None:
            pi, li = stream[best]
            matched.append({
                "ei": ei, "title": e["title"], "level": e["level"],
                "printed_page": e["page"], "page": pi, "line": li,
                "score": round(best_score, 3),
                "body_text": cleaned[pi][li]["t"], "pos": best,
            })
    matched.sort(key=lambda m: (m["pos"], -m["score"]))
    seen, uniq = set(), []
    for m in matched:
        if m["pos"] in seen:
            continue
        seen.add(m["pos"])
        uniq.append(m)
    return stream, uniq


# ---------------------------------------------------------------- 组装
def assemble(pages, cleaned, stream, matched, toc_ids, bw, bh, start=0,
             notes=None, subs=None, orphan=None):
    """把正文行拼成块：标题块 + 段落块 + 章末脚注块。

    notes/subs/orphan 来自 extract_notes() + plan_notes()。三者全为空时（没跑
    run_layout.py 的工作目录就是如此）输出与从前逐字节一致。
    """
    head_at = {}
    for m in matched:
        head_at[(m["page"], m["line"])] = m

    # 标题被拆成多行时，其余片段标记为已消费。
    # 印刷标题常折成 2~3 行（如「第五讲 / 黑格尔辩证法的逻辑学… / 论的统一」），
    # 命中行可能是中间那行，所以前后都要扫，不能只看前半截，也不能只管 level 1。
    consumed = set()
    pos_of = {p: i for i, p in enumerate(stream)}
    for m in matched:
        k = pos_of.get((m["page"], m["line"]))
        if k is None:
            continue
        nt = norm(m["title"])
        if not nt:
            continue
        for step in (-1, 1):
            j = k + step
            for _ in range(3):
                if not (0 <= j < len(stream)):
                    break
                pi, li = stream[j]
                L = cleaned[pi][li]
                if pi != m["page"]:
                    break
                n = norm(L["t"])
                # 残片判据（三条都要满足）：
                #   ① 至少 2 个字——单字如「的」是正文断行，不是标题；
                #   ② 整行是标题的子串——标题折行/拆行时必然如此；
                #   ③ 字号明显大于正文——标题用大字号，正文折行不会变大。
                # ②③ 缺一不可：只看 ② 会误吞正文里恰好是标题子串的行；
                # 只看 ③ 会误吞章首页上任何大字行。
                is_residue = (n and 2 <= len(n) and n in nt
                              and char_width(L) > 1.12 * bw
                              and (pi, li) != (m["page"], m["line"]))
                if is_residue:
                    consumed.add((pi, li))
                    j += step
                else:
                    break

    blocks = []
    buf = []                     # 段落文本片段
    prev = None
    last_head = None
    note_defs = {it["id"]: it["text"] for items in (notes or {}).values() for it in items}
    subs = subs or {}
    subs_ids = {nid for v in subs.values() for _s, _e, nid in v}
    used = set()                 # 已真正写进正文的脚注 id
    emitted = set()              # 已作为页尾注落盘的 id
    pend = []                    # 本章已引用、尚未落盘的脚注 id（按引用顺序）

    # 把没锚定的注按"页 → 所属章"归组：章标题（level 1）是切分点。
    # 不归组的话，全书所有孤立注都会堆进第一章。
    page_pos = {}
    for k, (pi_, _li) in enumerate(stream):
        page_pos.setdefault(pi_, k)
    h1 = sorted(m["pos"] for m in matched if m["level"] == 1)

    def chapter_of(page):
        p = page_pos.get(page, -1)
        cur = -1
        for hp in h1:
            if hp <= p:
                cur = hp
            else:
                break
        return cur

    by_chapter = {}
    for pi in sorted(orphan or {}):
        by_chapter.setdefault(chapter_of(pi), []).extend(orphan[pi])

    # plan_notes 判为"可锚定"、但那一行最终被 assemble 跳过（目录页/标题残片/
    # 版心过滤）时，这条注既不出现在 subs 的落地点，也不在 orphan 里——
    # 它会凭空消失。这里按页补进所属章，保证一条注都不丢。
    owner_of = {}
    for pi, items in (notes or {}).items():
        for it in items:
            owner_of[it["id"]] = chapter_of(pi)
    in_orphan = {it["id"] for items in (orphan or {}).values() for it in items}
    for items in (notes or {}).values():
        for it in items:
            if it["id"] in in_orphan:
                continue
            if it["id"] in subs_ids:
                continue      # 计划内联，最后按 `used` 复核（见 assemble 收尾）
            by_chapter.setdefault(owner_of.get(it["id"], -1), []).append(it)

    def emit_notes(upto=None):
        """落盘本章脚注。

        先出被正文引用的注（内联锚定，读者点一下弹窗看注文），再出没能锚定的
        注。锚不上的注照样保留全文——学术书的注是正文的一部分，丢掉不可接受；
        但只排成页尾注，不塞进正文流，否则读者会在毫不相干的位置读到一段引文。
        upto 为章起点，None 表示全部落盘（书末）。
        """
        if pend:
            for nid in pend:
                blocks.append(("def", nid, note_defs.get(nid, "")))
            del pend[:]
        rest = []
        for ch in sorted(by_chapter):
            if upto is not None and ch >= upto:
                break
            rest.extend(by_chapter.pop(ch))
        if rest:
            emitted.update(it["id"] for it in rest)
            blocks.append(("pn", [(it.get("mark") or "", it["text"]) for it in rest]))
    # 版心常态左边界。
    # 不能用众数：页内有长引文块时，众数会落在引文缩进上（实测偏移可达 130px
    # ≈ 2 字宽），于是整页的「顶格行」都算出负缩进，该页再也断不出段。
    # 取 10% 分位更稳：正文顶格行总是页内最靠左的那一批。
    left = {}
    for p in pages:
        ls = sorted(L["x0"] for L in cleaned[p["idx"]] if L["w"] > 0.3 * p["w"])
        left[p["idx"]] = ls[int(len(ls) * 0.1)] if ls else 0

    def flush():
        if buf:
            blocks.append(("p", "".join(buf)))
            buf.clear()

    for (pi, li) in stream[start:]:
        L = cleaned[pi][li]
        m = head_at.get((pi, li))
        if m is not None:
            flush()
            # 章边界：把本章已引用的注和锚不上的注一次性落盘
            if m["level"] == 1:
                emit_notes(upto=m["pos"])
            title = unify_title(m["title"], m["body_text"])
            last_head = (pi, title)
            title = title.strip(" #*")
            mm = re.match(r"^(第\s*[零一二三四五六七八九十百千两0-9]+\s*[讲章节篇卷部编]|绪论|前言|导论|引言|后记|结语|结论|参考书目|参考文献|致谢|序言)(?=[^\s、，,.．·:：\-—　])", title)
            if mm:
                title = mm.group(1) + "　" + title[mm.end():]
            if title.strip():
                blocks.append(("h", m["level"], title))
            prev = None
            continue
        if pi in toc_ids or (pi, li) in consumed:
            continue
        # 章节标题被拆成"第一讲 + 副标题"时，前半截残留行丢弃
        if (elen(L["t"]) <= 8 and L1_PAT.match(L["t"].strip())
                and last_head and last_head[0] == pi
                and norm(L["t"]) in norm(last_head[1])):
            continue
        # 页顶/页底窄短行：书名、章名的书眉残留，未命中目录则丢弃
        H = pages[pi]["h"]
        if (L["w"] < 0.55 * pages[pi]["w"] and elen(L["t"]) <= 16
                and (L["y1"] < 0.18 * H or L["y0"] > 0.90 * H)):
            continue
        ind = L["x0"] - left.get(pi, L["x0"])
        newp = False
        if prev is None:
            newp = True
        elif prev[0] != pi:
            # 跨页：排版上续行也可能带缩进，只按上一页末行是否收句来判断
            newp = prev[1]["t"][-1] in "。！？…”』」）)】》!?"
        elif ind > 1.2 * bw:
            newp = True
        if newp:
            flush()
        t = L["t"]
        sp = subs.get((pi, li))
        if sp:
            t = apply_subs(t, sp)
            for _s, _e, nid in sp:
                if nid not in used:
                    used.add(nid)
                    pend.append(nid)
        buf.append(t)
        prev = (pi, L)
    flush()
    emit_notes()
    # 收尾：计划内联、但落点那一行被跳过的注，补成页尾注。
    stray = [it for items in (notes or {}).values() for it in items
             if it["id"] not in used and it["id"] not in emitted]
    if stray:
        blocks.append(("pn", [(it.get("mark") or "", it["text"]) for it in stray]))
    return blocks


def to_markdown(blocks, meta):
    """块列表 → Markdown。

    脚注用 Pandoc 的 [^id] 引用语法：正文里是可点击的引用，定义在章末。
    EPUB 侧由 epub3.py 渲染成 <a epub:type="noteref"> + <aside epub:type="footnote">，
    这是 EPUB3 标准的弹窗脚注结构，阅读器据此弹出注文并可跳回原处。
    """
    out = []
    # 书名走 EPUB 元数据，不在正文里再放一个 h1，否则章节切分会把整本书当成一章
    for b in blocks:
        if b[0] == "h":
            out.append("#" * min(b[1], 6) + " " + b[2])
            out.append("")
        elif b[0] == "def":
            # 定义必须自成一段（前后各一个空行），否则会被并进上一段正文
            txt = (b[2] or "").strip().replace("\n", " ")
            if not txt:
                continue
            out.append("[^%s]: %s" % (b[1], txt))
            out.append("")
        elif b[0] == "pn":
            # 没能锚定的注：排成引用块（视觉上区隔于正文），形如「① 《晋书》卷九五…」
            for mark, txt in b[1]:
                line = (mark + " " + (txt or "")).strip()
                if line:
                    out.append("> " + line)
                    out.append("")
        else:
            txt = b[1].strip()
            if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", txt):
                continue          # 纯符号行（OCR 噪声）
            if len(txt) <= 1:
                continue          # 单字符行一定是版边残留
            txt = re.sub(r"^(#+)", r"\\\1", txt)   # 行首 # 会被当成标题，转义并留待人工核对
            out.append(txt)
            out.append("")
    return "\n".join(out)


BACK_COVER = re.compile(r"ISBN|图书在版编目|定价|责任编辑")


def drop_back_cover(pages):
    """剥掉封底：末页含 ISBN/定价 且行数明显偏少时，整页丢弃（最多看末两页）。"""
    import statistics
    n_lines = [len(p["lines"]) for p in pages]
    med = statistics.median(n_lines) if n_lines else 0
    while len(pages) > 1:
        last = pages[-1]
        txt = "".join(L["t"] for L in last["lines"])
        if BACK_COVER.search(txt) and len(last["lines"]) < 0.6 * med:
            pages.pop()
        else:
            break
    return pages


def dedupe_pages(pages, min_dup=3):
    """去掉「内容完全重复」的页。

    扫描件常见两种重复：① 扫描仪卡纸/文件拼接，同一页被复制多份；
    ② 补扫时把一段页重复追加到末尾。它们会让成书出现成段的重复正文。
    判据用清洗前的行文本拼串——同一页重复扫描时 OCR 结果逐字相同。

    只在某个文本连续出现 ≥min_dup 次时才去重：偶尔两页正文恰好相同（如全书
    引同一段话）是正常的，不能动。
    """
    if not pages:
        return pages
    sig = ["".join(L["t"] for L in p["lines"]).strip() for p in pages]
    keep = [True] * len(pages)
    i = 0
    while i < len(pages):
        if not sig[i]:
            i += 1
            continue
        j = i + 1
        while j < len(pages) and sig[j] == sig[i]:
            j += 1
        n = j - i
        if n >= min_dup:
            for k in range(i + 1, j):
                keep[k] = False
            print("  去重：p%03d~p%03d 内容完全相同（%d 页），只保留第一页"
                  % (i + 1, j, n))
        i = j
    out = [p for p, k in zip(pages, keep) if k]
    for idx, p in enumerate(out):
        p["idx"] = idx
    return out


def build(src=None, meta=None):
    meta = meta or {}
    if src is None:
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr")
    pages = dedupe_pages(drop_back_cover(load_pages(src)))
    if not pages:
        raise SystemExit("源目录没有页级 JSON（%s）：先跑 ocr 阶段，或检查 --src" % src)
    bw, bh = metrics(pages)
    heads = learn_running_heads(pages, bh)
    band_top, band_bot = head_band(pages, heads, bh)
    toc_ids = detect_toc_pages(pages, bh)
    cleaned = {p["idx"]: clean_page(p, bw, bh, heads,
                                   keep_nums=(p["idx"] in toc_ids),
                                   band_top=band_top, band_bot=band_bot)
               for p in pages}
    entries = parse_toc(pages, cleaned, bw, bh, toc_ids)
    stream, matched = match_entries(pages, cleaned, entries, toc_ids, bw, bh)
    start = 0
    if meta.get("drop_front", True) and matched and matched[0]["level"] == 1:
        start = matched[0]["pos"]
    # 脚注：抽区域 → 判断能否内联锚定 → 组装时替换成 [^id]
    notes = extract_notes(pages)
    subs, orphan = plan_notes(notes, cleaned) if notes else ({}, {})
    blocks = assemble(pages, cleaned, stream, matched, toc_ids, bw, bh, start,
                      notes=notes, subs=subs, orphan=orphan)
    md = to_markdown(blocks, meta)
    n_notes = sum(len(v) for v in notes.values())
    n_linked = sum(len(v) for v in subs.values())
    report = {
        "pages": len(pages), "bw": round(bw, 2), "bh": round(bh, 2),
        "head_band": [round(band_top, 4), round(band_bot, 4)],
        "toc_pages": sorted(toc_ids),
        "toc_entries": len(entries),
        "matched": len(matched),
        "levels": {1: sum(1 for m in matched if m["level"] == 1),
                   2: sum(1 for m in matched if m["level"] == 2),
                   3: sum(1 for m in matched if m["level"] == 3)},
        "entries": entries,
        "matched_detail": matched,
        "chars": len(md),
        "notes": {"total": n_notes, "linked": n_linked,
                  "orphan": n_notes - n_linked,
                  "pages": len(notes)},
    }
    return md, report, cleaned, pages
