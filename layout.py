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
import pagedoc

CJK = re.compile(r"[\u4e00-\u9fff]")
PUNCT_END = "。！？；：…”』」）)】》!?.;:"
PAGE_NO = re.compile(r"^[（(\[【]?\s*[-—–]?\s*(\d{1,4})\s*[-—–]?\s*[)）\]】]?$")
TOC_HEAD = re.compile(r"^目\s*录$|^目\s*次$|^contents$", re.I)

# 版面分析（run_layout.py）标出的、不该进正文的区域类型
DROP_REGIONS = {"footnote", "vision_footnote", "footer", "header",
                "page_header", "page_footer", "number", "page_number"}

# 图片/插图区。既不在 DROP_REGIONS 里、也没有任何裁图代码——于是插图整块
# 消失，区域里的 OCR 行（图注、示意图上的标注字）被当普通正文流式输出。
FIG_REGIONS = {"image", "figure"}
# 表格区。同样不在 DROP_REGIONS 里，于是每一行都独立成段：实测《中国僧官
# 制度史》4 个表格页产出 40 个 ≤6 字的碎片段（「共公僧」「池方僧官」「卜座」）。
TBL_REGIONS = {"table"}
# 表明「这一页确实有正文」的区域类型。图版页判定（见 plate_like）要用它：
# 一页若只有 header/footer/number 这类版式区、正文行全是噪声，那它多半是
# 整页插图/表格的照片，版面模型没认出来。
TEXTISH_REGIONS = {"text", "content", "paragraph_title", "doc_title",
                   "reference_content", "abstract", "figure_title", "algorithm"}

# 脚注区域：同样不进正文流，但要单独抽出来渲染成弹窗脚注
NOTE_REGIONS = {"footnote", "vision_footnote"}
# 扫描页底部的试用版水印，常被判成脚注区，必须剔掉
WATERMARK = re.compile(r"pdfFactory|fineprint|试用版本", re.I)
# 正文里的注号：①-⑳（U+2460..U+2473）最常见，㉑（U+3251..）起兼容，
# 另外兼容上标数字。它也会误伤正文里的 ① 列表符号——那种情况下注号数与
# 脚注条数对不上，plan_notes 会自动退化成页尾注释块，不会错配。
#
# 星号注（* / ** / ***）单独处理，见 extract_star_notes()：它的"编号"是
# 星号个数，而正文里 `*` 也可能出现在别的场合，所以不能并进 MARK_RE 一起
# 盲目配对，必须走那套更严的自校验。
MARK_RE = re.compile("[①-⑳㉑-㉟]|[⁰-ₜ]+")
STAR_RE = re.compile(r"\*{1,6}")

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


def split_note_blocks(rows, bw, k=1.0):
    """按左缩进把一个页尾的注区切成多条注释。

    扫描件的注区里，新的一条注往往只是比续行**多缩进一个字宽**——没有注号
    可依赖时，缩进是唯一的断点信号。返回 [[line,...], ...]，每个子列表是
    一条注（续行已并入前一条，不含注号）。
    """
    if not rows:
        return []
    rows = sorted(rows, key=lambda L: (L["y0"], L["x0"]))
    left = min(L["x0"] for L in rows)
    out = []
    for L in rows:
        if not out or L["x0"] > left + k * bw:
            out.append([L])
        else:
            out[-1].append(L)
    return out


def extract_star_notes(pages, cleaned, bw, bh):
    """抽取星号注（* / ** / ***…），返回 (notes, subs)。

    这本书的脚注用星号而不是圈号，且**注区里的星号在 OCR 后大量丢失**
    （168 行注文里只有 6 行残留纯星号），所以不能靠"注文带几个星号"来配对。
    改成从正文侧数星号、从注区侧按缩进切块，再用两条互相独立的证据校验：

      ① 块数 == 正文星号数；
      ② 每块（除末块）都以句末标点收尾——切错边界时几乎必然切在句中。

    两条都过才配对，否则整页退化成页尾注。宁可少弹窗，也不能把甲注挂到乙处，
    读者点开看到不相干的引文比不弹窗糟得多。

    返回 notes 结构与 extract_notes() 一致，可直接并入。
    """
    notes, subs = {}, {}
    seq = 0
    END = "。！？…”』」）)】》"
    for p in pages:
        rows = [L for L in p["lines"] if L.get("region") in NOTE_REGIONS
                and not WATERMARK.search(L["t"])]
        if not rows:
            continue
        body = [L for L in p["lines"] if L.get("region") not in NOTE_REGIONS]
        # 正文星号：跳过行首的（那是列表符号或残片）
        spots = []
        for li, L in enumerate(cleaned.get(p["idx"], [])):
            for m in STAR_RE.finditer(L["t"]):
                if m.start() > 0:
                    spots.append((li, m.start(), m.end(), len(m.group(0))))
        if not spots:
            continue
        blocks = split_note_blocks(rows, bw)
        texts = ["".join(L["t"] for L in b).strip() for b in blocks]
        texts = [t for t in texts if len(re.sub(r"[^\u4e00-\u9fff0-9]", "", t)) >= 2]
        if not texts:
            continue
        if len(texts) != len(spots):
            continue                      # 证据①不成立，整页放弃
        if len(blocks) > 1 and any(not t or t[-1] not in END for t in texts[:-1]):
            continue                      # 证据②不成立
        items = []
        for (li, s, e, nstar), t in zip(spots, texts):
            seq += 1
            nid = "fn%d" % seq
            items.append({"n": nstar, "mark": "*" * nstar, "text": t, "id": nid})
            subs.setdefault((p["idx"], li), []).append((s, e, nid))
        notes[p["idx"]] = items
    return notes, subs


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
    """读入全部页级 JSON。

    走 pagedoc 的版本检查：版本不认识时**明确报错**而不是带着错误的数据往下跑
    （下游会用它算 bw/bh、切章、配脚注，静默误读的后果是整本书结构错乱）。
    老工作目录里的 JSON 没有版本字段，pagedoc 按兼容处理，不会拦。
    """
    pages = []
    files = sorted(glob.glob(os.path.join(src, "*.json")))
    if files:
        pagedoc.warn_legacy(files, label="%s 下" % os.path.basename(os.path.normpath(src)))
    for f in files:
        d = pagedoc.read(f)
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

    命中「目录/目次」标题的页直接算。否则先挑出"有 ≥6 行以页码结尾"的候选页，
    再要求它满足下面任一条：

      A. 行尾页码**严格递增** —— 目录的页码是章节起始页，必然升序；
      B. 前后还有一页也是候选 —— 目录通常连续好几页。

    为什么两条都要：只靠 A，多页目录里某页 OCR 丢了几个页码就不再严格递增，
    会被漏掉；只靠 B，**只有一页目录的书**永远判不出来（实测《规训与惩罚》
    整本只有 1 页目录，老规则返回空集，目录解析跟着抛异常）。

    误判的代价很高：被误判的页会被 match_entries/assemble 整页跳过，正文会
    静默消失。所以候选门槛（≥6 行行尾页码）不能放松，版权页也有 7 个行尾
    数字，是靠 A 的严格递增把它排除掉的。
    """
    def tails(p):
        """行尾页码，按纵坐标自上而下排好返回。"""
        out = []
        for L in p["lines"]:
            t = L["t"].strip()
            m = re.search(r"[（(\[【]?\s*(\d{1,4})\s*[)）\]】]?\s*$", t)
            if m and elen(t) <= 40:
                out.append((L["y0"], int(m.group(1))))
        out.sort()
        return [v for _, v in out]

    def monotone(p):
        """该页行尾页码是否严格递增。

        单页目录（本书只有一页「目 录」）拿不到"相邻页也是目录"这条证据；只看
        "行尾数字 ≥6" 又会把版权页算进来——版权页也有 7 个行尾数字。区别在于
        目录的页码是**章节起始页**，自上而下严格递增；版权页的是一堆年份/开本/
        版次，顺序是乱的。实测：本书目录页 12 个数严格递增，版权页 7 个数只有
        一半升序。用这条信号把「单页目录」和「版权页」分开。
        """
        v = tails(p)
        return len(v) >= 6 and all(b > a for a, b in zip(v, v[1:]))

    def looks_toc(p):
        texts = [L["t"].strip() for L in p["lines"]]
        if any(TOC_HEAD.match(t.replace(" ", "")) for t in texts):
            return True
        return len(tails(p)) >= 6

    ids = set()
    cand = [p for p in pages[:scan] if looks_toc(p)]
    for i, p in enumerate(cand):
        # 标题命中的，单页即可认定
        texts = [L["t"].strip() for L in p["lines"]]
        if any(TOC_HEAD.match(t.replace(" ", "")) for t in texts):
            ids.add(p["idx"])
            continue
        # 页码严格递增也是够强的单页证据：版权页/正文页凑不出这种序列。
        # 少了这一条，只有一页目录的书（本书）会被"需要相邻目录页"的要求挡掉。
        if monotone(p):
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
        #
        # 例外：被标成 header 却**明显在页眉带以下**的行。版面模型会把居中
        # 大字的章标题误判成 header（本书「参考书目」y=20.4% 页高、左右居中，
        # 而真正的书眉都在 10~13%），照标签丢会把这个唯一的标题扔掉，
        # 该章在成书里就凭空消失。位置证据优先于区域标签。
        if L.get("region") in DROP_REGIONS:
            if not (L.get("region") == "header" and not keep_nums
                    and L["y0"] > max(band_top, 0.14) * H):
                continue
        # 边缘竖排字（书脊）：细高且贴边
        if hpx > 2.0 * max(wpx, 1) and (L["x1"] < 0.16 * W or L["x0"] > 0.84 * W):
            continue
        # 边栏单字（书脊竖排书名逐字识别）：按**版心边界**判断，不用固定比例。
        # 固定 0.85W 会在书脊字刚好压线时漏网（本书 2169 vs 2176，差 7px）。
        # 但目录页上"1 位数页码"长得和边栏单字一模一样（本书页码「3」x0=1827
        # 越过 br-0.5bw=1802），keep_nums 时不能删——删掉这一行，那条目录条目
        # 就拿不到页码，正文匹配只能靠猜。
        if (not keep_nums and elen(t) <= 2
                and (L["x1"] < bl - 0.5 * bw or L["x0"] > br - 0.5 * bw)):
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
# ---------------------------------------------------------------- 图 / 表降级
# 这两块刻意**不做结构重建**（列对齐、合并单元格、公式转 LaTeX 是另一类问题，
# 而且我们没有任何列对齐证据）。目标只有一条：**别把东西丢了**。
#
# 实测依据（《中国僧官制度史》350 页）：table region 4 个（138 行）、image region
# 4 个（22 行），既不在 DROP_REGIONS 里、也没有任何裁图/聚合代码，于是表格页把
# 每一行都独立成段，成书 1820 段里冒出 97 个 ≤6 字碎片（「共公僧」「卜座」），占
# 5.3%；插图页的标注字同样当正文流出去。


def page_regions(p):
    """页 JSON 的 regions 字段，没有就是空表。

    regions 是 run_layout.py 写进去的 [{x0,y0,x1,y1,cls,s}]。老工作目录
    （没跑过版面分析）没有这个字段，那本书的行为完全不变。
    """
    regs = p.get("regions")
    return regs if isinstance(regs, list) else []


def _area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _region_boxes(p, classes):
    """p 里 cls 属于 classes 的 region bbox 列表（已排除整页容器框）。

    整页框必须排除：实测封面那个 image region 是 (54,78)-(2415,3394)，几乎就是
    整张扫描页。照它裁出来是一张重复的封面，还会让书里凭空多一张假插图；
    照它聚合则会把整页正文并进一个「表格块」。判据取「面积 > 80% 页面积」——
    真插图再大也留在版心内，实测 (539,1522)-(2134,1708) 只占页面积 6.6%。
    """
    page_area = float(p["w"]) * float(p["h"])
    out = []
    for R in page_regions(p):
        if R.get("cls") not in classes:
            continue
        b = (float(R["x0"]), float(R["y0"]), float(R["x1"]), float(R["y1"]))
        if _area(b) <= 0 or _area(b) > 0.80 * page_area:
            continue
        out.append(b)
    out.sort(key=lambda b: (b[1], b[0]))
    return out


def find_figures(p):
    """这一页要被裁出来的插图区，按版面上从上到下的次序返回 bbox 列表。"""
    return _region_boxes(p, FIG_REGIONS)


def find_tables(p):
    """这一页的表格区 bbox，按从上到下的次序返回。"""
    return _region_boxes(p, TBL_REGIONS)


def _keep_raw(p, L):
    """这一行即使落在图/表区里也必须当正文保留吗？

    只有一种情况：region 被标成 header、位置却明显低于页眉带——那是版面模型把
    居中大字标题误判成书眉（clean_page 里那条同样的例外）。真书眉（页高 10% 上下）
    不在此列。
    """
    if L.get("region") != "header":
        return False
    return L["y0"] > 0.14 * float(p["h"])


PLATE_MARGIN = 0.02       # 图版页整页裁图时四周留 2% 页宽/页高，避免切到装订线阴影


def plate_bbox(p):
    """整页图版的裁切框（四周留一点边距）。"""
    W, H = float(p["w"]), float(p["h"])
    mx, my = PLATE_MARGIN * W, PLATE_MARGIN * H
    return (mx, my, W - mx, H - my)


def extract_figures(pages, cleaned=None):
    """收集全书插图区，并给每个区分配稳定文件名。

    返回 [{"page": 0 基页 idx, "name": "fig-p143-1.png", "bbox": (...)}]。
    文件名带页码，人工核对时能直接对回原书；序号按页内自上而下的次序从 1
    开始，同一页两张图不会互相覆盖。

    cleaned 给出来时，额外把「整页图版」页（见 plate_like）也算成一张插图。
    不给就只认 region 标签——旧调用方不受影响。
    """
    out = []
    for p in pages:
        boxes = find_figures(p)
        rows = (cleaned or {}).get(p["idx"])
        tagged_plate = False
        if rows is not None and plate_like(p, rows):
            boxes = [plate_bbox(p)]
            tagged_plate = True
        for k, b in enumerate(boxes, 1):
            out.append({"page": p["idx"],
                        "name": "fig-p%03d-%d.png" % (p["idx"] + 1, k),
                        "bbox": b,
                        "plate": tagged_plate})
    return out


def _visual_rows(rows, bw, bh):
    """把一组行聚成「视觉行」，并数出每行的列数。

    同一视觉行 = 中心线相差不超过 0.6 个行高。表格一行里各列是并排的，按 x0
    排好就是列序——这是聚合所需的全部信息，**不猜哪列是哪列**。

    列数 = 按字宽把 x0 聚类后的簇数。1.6 字宽是实测值：同一列的字常有十几像素
    的抖动（70 像素的字，「共公僧」x0=1759.6 与「监导」x0=1747.3 差 12px），
    小于 1.6 字宽必然同列，而真正的不同列至少隔着一个字宽。
    """
    rows = sorted(rows, key=lambda L: (L["y0"], L["x0"]))
    out = []
    for L in rows:
        cy = (L["y0"] + L["y1"]) / 2.0
        if out and abs(cy - out[-1]["cy"]) <= 0.6 * bh:
            out[-1]["lines"].append(L)
        else:
            out.append({"cy": cy, "lines": [L]})
    for r in out:
        r["lines"].sort(key=lambda L: L["x0"])
        cols = []
        for L in r["lines"]:
            if cols and L["x0"] - cols[-1][0] <= 1.6 * bw:
                cols[-1].append(L["x0"])
            else:
                cols.append([L["x0"]])
        r["ncol"] = len(cols)
        r["x0"] = r["lines"][0]["x0"]
    return out


# 表格行的「收句」判据比正文窄：只认真正的句末/分句标点。
# 不能直接复用 PUNCT_END——那里含「）」「)」，而表格单元格本来就常以括号收尾
# （实测 p120 沿革表的「鸿胪寺（崇玄署）」「暂归祠部检校」都带括号），
# 用宽判据会把表格从中间截成两段。
TABLE_END = "。！？；…"


def _row_is_tableish(row, bw):
    """这一视觉行像不像表格的一行：≥2 列，且列与列之间隔着一段空白。

    只数「列数」不够：目录页的行尾页码也是第 2 列（实测《规训与惩罚》目录页会
    被整页判成表格）。目录的页码紧贴着引导点，间隙很小；表格的列之间有明显的
    空白区。实测列间隙 > 3 字宽能把两者分开：表格行全部成立，目录行全部不到。
    """
    if row["ncol"] < 2:
        return False
    xs = [L["x0"] for L in row["lines"]]
    return max(b - a for a, b in zip(xs, xs[1:])) > 3.0 * bw


def _table_flags(p, rows, bw, bh):
    """逐行判定「这行属于表格块」，返回与 rows 等长的布尔列表。

    两路证据，取并集：

    ① region 标签（run_layout.py 给的）。有标签就直接信，这是最硬的证据。
    ② 没标签时按几何判：连续的「表格行」（多列 + 大列间隙 + 行距紧密 + 行尾
       不收句），至少 3 行才算一块。

    为什么必须有 ②：table 标签实测只覆盖 4 页 / 138 行，而同一本书里散在正文中
    的表格（p120 的沿革表、p150/p151 的牒文、p306/p320/p321 的喇嘛表）版面模型
    全标成了 unassigned，光靠标签会整片漏掉，碎片照旧。

    ② 的阈值是实测出来的，不是拍脑袋：
      · 行距紧密（> 1.6 行高就断）：正文行距恒定在 1.0~1.1 行高，只有段落之间
        才留空行；表格行之间几乎不留空。
      · 行尾不收句：表格单元格里极少出现句号。少了这条，正文里连续几行的短行
        也会被并进来（实测《规训与惩罚》p006 的章标题页有 3 行「章名 + 页码」）。
      · 至少 3 行：两行凑不出一个「块」。
    """
    tabs = find_tables(p)
    tagged = []
    for L in rows:
        if _keep_raw(p, L):
            tagged.append(False)
            continue
        if L.get("region") in TBL_REGIONS:
            tagged.append(True)
            continue
        cy = (L["y0"] + L["y1"]) / 2.0
        inside = any(b[1] <= cy <= b[3]
                     and b[0] - 0.5 * bw <= L["x0"] <= b[2] + 0.5 * bw for b in tabs)
        tagged.append(bool(inside))

    geo = [False] * len(rows)
    loose = [L for L, f in zip(rows, tagged) if not f]
    if loose:
        vis = _visual_rows(loose, bw, bh)
        segs, cur, prev_cy = [], [], None
        for r in vis:
            t = "".join(L["t"] for L in r["lines"]).strip()
            closed = bool(t) and t[-1] in TABLE_END
            tight = prev_cy is None or (r["cy"] - prev_cy) <= 1.6 * bh
            if _row_is_tableish(r, bw) and not closed and tight:
                cur.append(r)
            else:
                if len(cur) >= 3:
                    segs.append(cur)
                cur = []
            prev_cy = r["cy"]
        if len(cur) >= 3:
            segs.append(cur)
        pos = {id(L): i for i, L in enumerate(rows)}
        for seg in segs:
            for r in seg:
                for L in r["lines"]:
                    geo[pos[id(L)]] = True

    return [bool(a) or b for a, b in zip(tagged, geo)]



def table_blocks(p, rows, bw, bh):
    """一页里被并成「表格块」的行组（每组是一串行对象，按阅读顺序）。

    只有 ≥2 行才成块：单行的「表格」既没有可聚合的东西，也修不掉碎片。
    目录页在调用方（plan_regions）就整页跳过了——目录行和表格行几何上分不开，
    见那里的注释。
    """
    flag = _table_flags(p, rows, bw, bh)
    blocks, cur = [], []
    for L, f in zip(rows, flag):
        if f:
            cur.append(L)
        else:
            if len(cur) >= 2:
                blocks.append(cur)
            cur = []
    if len(cur) >= 2:
        blocks.append(cur)
    return blocks


def group_table_text(rows, bw, bh):
    """把表格区的行聚成一块**整段文字**，不拆碎、也不假装是 Markdown 表格。

    按视觉行分段，同一行内的单元格之间插一个全角空格（表格的列边界），行与行
    之间用换行——读者能看出这是个表，我们又没有伪造列对齐。
    行内用全角空格而不是空串拼接：单元格是独立的词（「共公僧」「池方僧官」），
    直接粘起来会造出「共公僧池方僧官」这种书里不存在的词。
    """
    lines = []
    for r in _visual_rows(rows, bw, bh):
        parts = [L["t"].strip() for L in r["lines"]]
        parts = [t for t in parts if t]
        if parts:
            lines.append("　".join(parts))
    return "\n".join(lines)


def plate_like(p, rows):
    """这一页是不是「一整页都是图/表」的图版，但版面模型没认出来？

    实测《中国僧官制度史》p053：页 JSON 里只有一个 header region，33 行 OCR
    结果全是噪声（t-- / J. / 二 / ÷），没有一行带 text/content/paragraph_title
    之类的正文区标签。这不是正文，是一整页插图的照片——照正文输出会在成书里
    留下 12 个 ≤6 字碎片。

    判据刻意保守（三条都要满足），因为误判会把整页正文换成一张图，那比丢字
    更糟：
      ① 页 JSON 确实跑过版面分析（有 regions 字段）。没跑过的老工作目录根本
         没有证据，一律不当图版。
      ② 清洗后的行里没有任何一行的 region 属于 TEXTISH_REGIONS。只要有一行
         被标成正文，就说明这页有真文字。
      ③ 清洗后至少 8 行。少于 8 行多半是空白页/章节扉页，不是图版。
      ④ 页内几乎没有「长行」（≥12 个汉字）。图版的 OCR 结果全是符号和单字；
         正文页即使整页被标成 unassigned，也必然有一批长行。这一条是兜底：
         万一版面模型整页失效，也不能把正文换掉。

    实测这条只命中僧官 1 页（p053），《规训与惩罚》0 页、黑格尔 0 页。
    """
    if not page_regions(p):
        return False
    if any(L.get("region") in TEXTISH_REGIONS for L in rows):
        return False
    if len(rows) < 8:
        return False
    # 再加一层：图版的 OCR 结果全是符号/单字（实测 p053 最长的一行只有 5 个字）。
    # 正文页即使全部被标成 unassigned，也必然有一批 ≥12 汉字的长行——
    # 少了这条保护，一次版面模型全军覆没就会把整页正文换成一张图。
    long_cjk = sum(1 for L in rows if len(CJK.findall(L["t"])) >= 12)
    return long_cjk <= 1


def _rows_of(pairs, cleaned):
    """[(页idx, 行idx), ...] → 行对象列表（保持顺序）。"""
    out = []
    for pi, li in pairs:
        rows = (cleaned or {}).get(pi) or []
        if 0 <= li < len(rows):
            out.append(rows[li])
    return out


def plan_regions(pages, cleaned, bw, bh, toc_ids=None):
    """把图 / 表区变成「在原文位置插入的块」，返回 (ins, report)。

      ins    {(页idx, 行idx): [块, ...]}，表示「在清洗后的这一行之前插入」。
             块是 ("fig"|"tbl", 内容, 页idx, [(页idx, 行idx), ...])，最后一项是
             这个块**取代**了哪些行。为什么把消费行塞进块里而不是单独返回一个
             集合：裁图可能失败，失败时这些行必须留在正文里（见 build()），
             调用方要能按块决定丢不丢。
      report 计数，写进 report.json 供核对。

    行索引按 **clean_page 之后** 的行序，因为 assemble 遍历的正是那份行。
    """
    ins = {}
    report = {"figures": 0, "tables": 0, "table_lines": 0, "figure_lines": 0,
              "plates": 0}

    def anchor_before(rows, y):
        """第一个 y0 >= y 的清洗行索引；没有就返回 len(rows)（= 插在页末）。"""
        for i, L in enumerate(rows):
            if L["y0"] >= y:
                return i
        return len(rows)

    toc_ids = toc_ids or set()
    for p in pages:
        pi = p["idx"]
        rows = cleaned.get(pi) or []
        if not rows:
            continue
        # 目录页整页跳过：目录本身是「一串短行 + 行尾页码」，几何上跟表格一模一样
        # （实测《规训与惩罚》目录页会被判成 2 块表，整个目录被并成两坨文字，
        # 章节结构直接消失）。这是零成本就能避开的误判——目录页本来就被单独处理。
        if pi in toc_ids:
            continue
        pos = {id(L): i for i, L in enumerate(rows)}

        # ---- 表格：整块聚成一段文字，插在块首行原来的位置
        for blk in table_blocks(p, rows, bw, bh):
            idxs = [pos[id(L)] for L in blk if id(L) in pos]
            if not idxs:
                continue
            text = group_table_text(blk, bw, bh).strip()
            if not text:
                continue
            rows_gone = [(pi, i) for i in idxs]
            ins.setdefault((pi, min(idxs)), []).append(("tbl", text, pi, rows_gone))
            report["tables"] += 1
            report["table_lines"] += len(idxs)

        # ---- 插图：把区域里的 OCR 行换成裁出来的图
        boxes = find_figures(p)
        if not boxes and plate_like(p, rows):
            # 整页图版：版面模型没打 image 标签，但这一页没有任何正文区，
            # 行全是噪声。整页裁成一张图，比往成书里灌 33 行乱码强。
            boxes = [plate_bbox(p)]
            report["plates"] = report.get("plates", 0) + 1
        for k, b in enumerate(boxes, 1):
            inside = []
            for i, L in enumerate(rows):
                cy = (L["y0"] + L["y1"]) / 2.0
                cx = (L["x0"] + L["x1"]) / 2.0
                if b[1] <= cy <= b[3] and b[0] <= cx <= b[2] and not _keep_raw(p, L):
                    inside.append(i)
            a = inside[0] if inside else anchor_before(rows, b[3])
            ins.setdefault((pi, a), []).append(
                ("fig", "fig-p%03d-%d.png" % (pi + 1, k), pi,
                 [(pi, i) for i in inside]))
            report["figures"] += 1
            report["figure_lines"] += len(inside)

    return ins, report


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
    W = pages[0]["w"] if pages else 0   # 目录页为空时 _mk_entry 也要有页宽可用
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
    # 「第X部/编/卷/篇」是比章更高的一层。它必须单独给一个名次，否则和
    # 「第X章」共用名次 10，两者被压成同一级——本书「第一部分」与「第一章」
    # 会被判成平级，16 条目录全变一级，章也全部挂到 part 下面看不出层次。
    # 只在本书这类**真有分部**的书里生效：僧官制度史没有「第X部」，
    # 名次表里少一档不影响它的 章/节/一、 三档映射。
    (re.compile(r"^第\s*[零一二三四五六七八九十百千两0-9\-—–iIl]{1,3}\s*[部编卷篇]"), 5),
    (re.compile(r"^第\s*[零一二三四五六七八九十百千两0-9\-—–iIl]{1,3}\s*[讲章]"), 10),
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


def assign_levels(entries, W=None):
    """把目录条目映射成 1/2/3 级。

    W（页宽）保留在签名里只是为了兼容旧调用，定层级并不需要它——信息全在标题
    前缀和条目顺序里。早先 parse_toc 把 W 当循环变量用，一页目录都没有时这个
    局部变量没被赋值，于是抛 UnboundLocalError；给个默认值兜住这类调用。

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
def page_offset(pages):
    """估计「PDF 页序 → 印刷页码」的整体偏移，返回 offset 或 None。

    关系是 pdf_idx = printed + offset。这一本实测 297 页样本里 295 页吻合
    （99.3%），比任何"标题相似度"都硬：目录条目本身就带页码，拿它换算
    出来的位置直接就是章节起始页。

    只认 region == "number" 的行——那是版面分析标注的页码区，而不是正文里
    偶然出现的数字。样本少于 20 页时不返回（小书/版式特殊，宁可不猜），
    且要求主偏移覆盖 ≥80% 的样本，避免把一本页码乱跳的书按错误偏移对齐。
    """
    from collections import Counter
    pairs = []
    for p in pages:
        for L in p["lines"]:
            if L.get("region") != "number":
                continue
            t = L["t"].strip()
            if re.fullmatch(r"\d{1,4}", t):
                pairs.append(p["idx"] - int(t))
    if len(pairs) < 20:
        return None
    off, n = Counter(pairs).most_common(1)[0]
    if n < 0.80 * len(pairs):
        return None
    return off


def match_entries(pages, cleaned, entries, toc_ids, bw, bh, offset=None):
    """顺序单调匹配：每条目录条目在正文中向后搜索最相似的行。

    offset 给出时（见 page_offset），带印刷页码的条目先在"页码换算出来的那一页"
    上找标题；找到就直接采信。这是必需的：本书每个部分扉页都把该部分的章名
    重列一遍，纯文本匹配会把「第一章 驯顺的肉体」错配到扉页上，于是三章的
    起始页全被算成同一页，正文被整段吞掉。
    """
    stream = []  # (page_idx, line_idx)
    for p in pages:
        if p["idx"] in toc_ids:
            continue
        for j, L in enumerate(cleaned[p["idx"]]):
            stream.append((p["idx"], j))
    pos_of = {pl: k for k, pl in enumerate(stream)}

    matched = []
    cursor = 0
    for ei, e in enumerate(entries):
        tgt = norm(e["title"])
        if len(tgt) < 2:
            continue
        best = None
        best_score = 0.0
        # ① 页码锚定：条目自带印刷页码时，换算出的那一页就是它的起始页。
        #    这是本书唯一可靠的信号——每个部分扉页都把该部分的章名重列一遍，
        #    纯文本搜索会让三章全撞到扉页上，正文被整段吞掉。
        #    要求同页高相似（≥0.80）才采信，不像就退回常规搜索。
        if offset is not None and e.get("page") is not None:
            aim = e["page"] + offset
            if 0 <= aim < len(pages):
                for li, L in enumerate(cleaned.get(aim, [])):
                    cand = norm(L["t"])
                    if not cand or abs(len(cand) - len(tgt)) > 10:
                        continue
                    if SequenceMatcher(None, cand, tgt).ratio() >= 0.80:
                        k = pos_of.get((aim, li))
                        if k is not None:
                            best, best_score = k, 2.0
                        break
        # ② 常规搜索：从上次命中往后 900 行（页码已经定住就不再搜）
        rng = () if best is not None else range(cursor, min(cursor + 900, len(stream)))
        for k in rng:
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
             notes=None, subs=None, orphan=None, ins=None, gone=None):
    """把正文行拼成块：标题块 + 段落块 + 章末脚注块 + 图/表块。

    notes/subs/orphan 来自 extract_notes() + plan_notes()。三者全为空时（没跑
    run_layout.py 的工作目录就是如此）输出与从前逐字节一致。

    ins 来自 plan_regions()：{(页idx, 行idx): [块, ...]}，表示「在这一行之前插入
    图/表块」；gone 是「文字已被图/表块取代」的行。两者都为空（没有 regions
    字段的老工作目录）时整条路径不生效，输出与从前逐字节一致——这是硬回归的落点。
    """
    ins = ins or {}
    gone = gone or set()
    head_at = {}
    for m in matched:
        head_at[(m["page"], m["line"])] = m

    # 标题被拆成多行时，其余片段标记为已消费。
    # 印刷标题常折成 2~3 行（如「第五讲 / 黑格尔辩证法的逻辑学… / 论的统一」），
    # 命中行可能是中间那行，所以前后都要扫，不能只看前半截，也不能只管 level 1。
    # 图/表块已经吃掉了这些行：它们不再进正文流。set 里放的是 (页idx, 行idx)，
    # 与标题残片用的是同一套消费机制，跳过逻辑不必再写一遍。
    consumed = set(gone)
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
    buf_page = []                # [(段内偏移, 页idx)]，flush 时随段落块落盘
    buf_len = [0]                # 段内已累积字符数（避免每次 sum(buf) 变 O(n^2)）
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

    note_page = {}               # 注 id -> 它所在的 PDF 页码（1 基）
    for pi, items in (notes or {}).items():
        for it in items:
            note_page[it["id"]] = pi + 1

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
            # 带上第一条注所在页：pn 块里的注来自不同页，取首条即可代表
            # 这一批的起始位置（校对清单只需要能翻到附近）。
            blocks.append(("pn", [(it.get("mark") or "", it["text"]) for it in rest],
                           [note_page.get(it["id"]) for it in rest]))
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
            # 带上**行级**页码：[(段内字符偏移, PDF 页码), ...]。
            # 校对清单要能让人直接翻到原书那一页，只记"段落首页"不够——
            # 一段常跨 2~3 页，命中点落在段中时首页会差 1~2 页（实测偏差
            # 最大到 3 页）。逐行记偏移，命中在哪一行就给哪一行所在的页。
            # 必须传副本：buf_page.clear() 在下一次 flush 前会把已落盘的块改空
            # （实测 page_map 只剩标题那 16 条，段落一条都没进来）。
            blocks.append(("p", "".join(buf), list(buf_page)))
            buf.clear()
            buf_page.clear()
            buf_len[0] = 0

    placed = set()

    def put_regions(pi, li):
        """把「本来该在这一行之前」的图/表块落盘。

        位置必须按**行**算，不能按页批量后置：插图常夹在段落中间（实测 p143 的
        示意图在页面中部），表格后面紧跟的就是讨论该表的正文。整页后置会让图
        跑到下一章去。

        插入块会**主动断段**：图/表是块级元素，前后文本来就不连着。
        """
        items = ins.get((pi, li))
        if not items:
            return
        flush()
        for b in items:
            if b[0] == "fig":
                blocks.append(("img", b[1], b[2] + 1))
            else:
                blocks.append(("tbl", b[1], b[2] + 1))
        placed.add((pi, li))

    for (pi, li) in stream[start:]:
        L = cleaned[pi][li]
        m = head_at.get((pi, li))
        if ins:
            put_regions(pi, li)
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
                blocks.append(("h", m["level"], title, pi + 1))
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
        buf_page.append((buf_len[0], pi))
        buf_len[0] += len(t)
        buf.append(t)
        prev = (pi, L)
    flush()
    # 清洗后行数为 0 的页不在 stream 里，它的锚点遍历不到——补一遍，
    # 否则那些页的图/表会静默丢失（这正是本次要修的病）。
    for key in sorted(ins, key=lambda k: (k[0], k[1])):
        if key not in placed:
            put_regions(*key)
    flush()
    emit_notes()
    # 收尾：计划内联、但落点那一行被跳过的注，补成页尾注。
    stray = [it for items in (notes or {}).values() for it in items
             if it["id"] not in used and it["id"] not in emitted]
    if stray:
        blocks.append(("pn", [(it.get("mark") or "", it["text"]) for it in stray],
                       [note_page.get(it["id"]) for it in stray]))
    return blocks


def to_markdown(blocks, meta):
    """块列表 → Markdown。

    脚注用 Pandoc 的 [^id] 引用语法：正文里是可点击的引用，定义在章末。
    EPUB 侧由 epub3.py 渲染成 <a epub:type="noteref"> + <aside epub:type="footnote">，
    这是 EPUB3 标准的弹窗脚注结构，阅读器据此弹出注文并可跳回原处。
    """
    text, _ = to_markdown_with_pages(blocks, meta)
    return text


def to_markdown_with_pages(blocks, meta):
    """同 to_markdown，额外返回「md 字符偏移 → PDF 页码」映射。

    校对清单必须能让人**直接翻到原书那一页**：只给 book.md 里的字符位置，
    人工复核时无从下手（成书 1957 个段落，逐段找等于重读一遍）。
    块里带的页码是 assemble() 记下的（1 基，即 PDF 第几页），这里记下每个块
    在 md 里的起始偏移，audit 就能把命中位置换算成页码。

    返回 (md_text, page_map)，page_map 为 [(char_offset, page_no), ...] 升序。
    """
    out = []
    page_map = []
    pos = [0]                 # 当前已输出字符数（out 拼接后的长度）

    def emit(s):
        # join("\n") 会在相邻片段之间插入分隔符，pos 必须把它算进去。
        # 漏算时偏移会随块数单调偏小（1100 个块 × 每个两次 emit ≈ 2200 个分隔符），
        # 最终页码整体偏 3 页——这是实测「土气」记成 p360、实为 p357 的原因。
        if out:
            pos[0] += 1
        out.append(s)
        pos[0] += len(s)
    # 书名走 EPUB 元数据，不在正文里再放一个 h1，否则章节切分会把整本书当成一章
    for b in blocks:
        if b[0] == "h":
            pg = b[3] if len(b) > 3 else None
            if pg:
                page_map.append((pos[0], pg))
            emit("#" * min(b[1], 6) + " " + b[2])
            emit("")
        elif b[0] == "def":
            # 定义必须自成一段（前后各一个空行），否则会被并进上一段正文
            txt = (b[2] or "").strip().replace("\n", " ")
            if not txt:
                continue
            emit("[^%s]: %s" % (b[1], txt))
            emit("")
        elif b[0] == "img":
            # 图：md 里就是标准的 ![](images/xxx.png)。页码放进 page_map，
            # 让 audit 也能把插图换算成原书页。
            pg = b[2] if len(b) > 2 else None
            if pg:
                page_map.append((pos[0], pg))
            emit("![](images/%s)" % b[1])
            emit("")
        elif b[0] == "tbl":
            # 表：整块文字放进 ```table 围栏。**不假装是 Markdown 表格**——
            # 我们没有列对齐证据，把单元格用 | 拼起来等于凭空造结构。
            # 围栏里逐行原样保留（行内全角空格就是列边界），epub3 渲染成
            # <pre class="table-block">，等宽字体显示。
            pg = b[2] if len(b) > 2 else None
            if pg:
                page_map.append((pos[0], pg))
            emit("```table")
            for ln in (b[1] or "").split("\n"):
                emit(ln)
            emit("```")
            emit("")
        elif b[0] == "pn":
            # 没能锚定的注：排成引用块（视觉上区隔于正文），形如「① 《晋书》卷九五…」
            # 每条注各自带来源页（b[2] 是 [(mark, text, page)] 或旧式二元组），
            # 逐条映射才能保证「自白」这种落在第 2 条注里的词被指到正确页。
            pages_of = b[2] if len(b) > 2 else None
            for j, item in enumerate(b[1]):
                mark, txt = item[0], item[1]
                line = (mark + " " + (txt or "")).strip()
                if not line:
                    continue
                if pages_of and j < len(pages_of) and pages_of[j]:
                    page_map.append((pos[0], pages_of[j]))
                emit("> " + line)
                emit("")
        else:
            raw = b[1]
            txt = raw.strip()
            if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", txt):
                continue          # 纯符号行（OCR 噪声）
            if len(txt) <= 1:
                continue          # 单字符行一定是版边残留
            base = pos[0]
            # strip() 会把开头空白吃掉，段内偏移是按**未 strip** 的串记的，
            # 必须先换算掉这段前缀，否则整段的页码会整体偏几行（实测偏 4 页）。
            lead = len(raw) - len(raw.lstrip())
            txt = re.sub(r"^(#+)", r"\\\1", txt)   # 行首 # 会被当成标题，转义并留待人工核对
            # 段落块带行级 [(段内偏移, 页idx)]，逐行并入 page_map
            lines_pg = b[2] if len(b) > 2 else None
            if isinstance(lines_pg, list):
                for off, pi in lines_pg:
                    rel = off - lead
                    if rel < 0:
                        rel = 0
                    page_map.append((base + rel, pi + 1))
            emit(txt)
            emit("")
    # page_map 必须升序：audit.page_of() 用二分查找。标题块与段落块的偏移
    # 是各自追加的，跨块时天然有序；但段落内的 (off, page) 是本段偏移，
    # 加上 base 后仍升序。这里再排一次兜底，成本可忽略（千级）。
    page_map.sort(key=lambda x: x[0])
    return "\n".join(out), page_map


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


# 裁图的外扩边距，与 make_review.py 保持一致（8px 左右、6px 上下）。
# 不另写一套：同一本书的审查图与成书插图用同一个边距，人工比对时位置才对得上。
CROP_PAD_X = 8
CROP_PAD_Y = 6


def crop_figure(page_png, bbox, dst, page_wh=None):
    """按 region bbox 从整页 PNG 裁出插图，写到 dst。返回 (宽, 高) 或 None。

    边距逻辑直接复用 make_review.py 的那两行：外扩 8/6 像素后夹到图像范围内。
    不外扩的话，插图四周的细线会被切掉半像素，看着像被裁残了。

    page_wh 是页 JSON 里的 (w, h)，即 bbox 所在的坐标系。实际 PNG 的分辨率
    可能与它不同（换过 DPI 重渲、或工作目录是分几次跑出来的），此时必须按比例
    换算——否则裁出来的是「差不多位置的一张图」，肉眼很难发现裁歪了。
    实测：ocr_merged 的页 JSON 是 2480×3508，同目录的 pages/*.png 是 2594×3588，
    差 4.6%，不换算就会偏出去几十像素。

    裁剪失败（缺页面图 / PIL 装不上 / bbox 退化）返回 None，**绝不抛异常**：
    一张图裁不出来不该让整本书的成书阶段失败。调用方负责报数。
    """
    try:
        from PIL import Image
    except Exception:
        return None
    if not page_png or not os.path.exists(page_png):
        return None
    try:
        img = Image.open(page_png).convert("RGB")
    except Exception:
        return None
    x0, y0, x1, y1 = bbox
    if page_wh:
        sw = img.width / float(page_wh[0] or img.width)
        sh = img.height / float(page_wh[1] or img.height)
        if abs(sw - 1.0) > 0.01 or abs(sh - 1.0) > 0.01:
            x0, x1 = x0 * sw, x1 * sw
            y0, y1 = y0 * sh, y1 * sh
    box = (max(0, int(x0) - CROP_PAD_X), max(0, int(y0) - CROP_PAD_Y),
           min(img.width, int(x1) + CROP_PAD_X), min(img.height, int(y1) + CROP_PAD_Y))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return None
    d = os.path.dirname(os.path.abspath(dst))
    if d:
        os.makedirs(d, exist_ok=True)
    img.crop(box).save(dst, "PNG", optimize=True)
    return box[2] - box[0], box[3] - box[1]


def _find_pages_dir(src, meta):
    """页面 PNG 所在目录：优先 meta["pages_dir"]，否则 src 旁边/下面的 pages/。"""
    if meta.get("pages_dir"):
        return meta["pages_dir"]
    base = os.path.dirname(os.path.abspath(src))
    for cand in (os.path.join(base, "pages"), os.path.join(src, "pages"),
                 os.path.join(base, os.path.basename(os.path.normpath(src)), "pages")):
        if os.path.isdir(cand):
            return cand
    return os.path.join(base, "pages")


def _images_dir(src, meta):
    """插图落盘目录。

    优先 meta["out_dir"]（make_md.py 传的 --out），让图片和 book.md 落在同一个
    产物目录下，md 里写 images/xxx.png 就能直接相对引用；没给时退到
    <src 的父目录>/out/images（bookflow 的工作目录布局正是 <work>/out）。
    """
    if meta.get("images_dir"):
        return meta["images_dir"]
    if meta.get("out_dir"):
        return os.path.join(meta["out_dir"], "images")
    base = os.path.dirname(os.path.abspath(src))
    return os.path.join(base, "out", "images")


def emit_images(pages, src, meta, report, cleaned=None):
    """把每个插图区裁成 PNG，返回要插入 md 的 [(页idx, 裁图文件名)]。

    页面 PNG 用的是**未清洗的原始页**（p["name"]）：clean_page 会把页眉页脚删掉，
    但裁图要的是原始扫描像素，两者无关。
    """
    outdir = _images_dir(src, meta)
    pagesdir = _find_pages_dir(src, meta)
    made, failed = [], []
    for f in extract_figures(pages, cleaned):
        p = pages[f["page"]]
        png = os.path.join(pagesdir, p.get("name") or ("p-%03d.png" % (f["page"] + 1)))
        dst = os.path.join(outdir, f["name"])
        size = crop_figure(png, f["bbox"], dst, (p["w"], p["h"]))
        if size is None:
            failed.append({"page": f["page"] + 1, "name": f["name"],
                           "reason": "缺页面图或裁剪失败"})
            continue
        made.append(f)
    report["images"] = {
        "dir": outdir, "made": len(made), "failed": len(failed),
        "files": [f["name"] for f in made], "failed_detail": failed,
    }
    if failed:
        # 静默丢图正是这次要修的病，裁不出来必须吭声（但不中断成书）。
        print("  警告：%d 个插图区裁剪失败（缺页面图？），这些位置不会出现图片："
              % len(failed))
        for x in failed[:10]:
            print("    p%03d %s（%s）" % (x["page"], x["name"], x["reason"]))
    return made


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
    if not entries:
        # 目录页缺失时整条流水线会"成功"地产出一本无章节结构的书：
        # 全书拍成一个平面段落流，EPUB 只有一章、没有导航。rc=0、无告警，
        # 用户拿到成书只会以为"这书本来就这样"——必须显式警告。
        print("\n" + "!" * 68)
        print("! 未检测到目录条目：成书将没有章节结构（全书作为一章、无目录导航）。")
        print("! 如果原书确实有目录页，请检查：")
        print("!   1) 扫描件前 30 页是否有「目 录 / 目次 / Contents」页；")
        print("!   2) 目录页是否被 OCR 成了图片或整页乱码；")
        print("!   3) 目录条目的行尾页码是否被识别出来（parse_toc 靠它判条目）。")
        print("! 检测到的目录页索引：%s" % (sorted(toc_ids) or "无"))
        print("!" * 68 + "\n")
    offset = page_offset(pages)
    stream, matched = match_entries(pages, cleaned, entries, toc_ids, bw, bh, offset)
    start = 0
    if meta.get("drop_front", True) and matched and matched[0]["level"] == 1:
        start = matched[0]["pos"]
    # 脚注：抽区域 → 判断能否内联锚定 → 组装时替换成 [^id]
    # 两条通道：圈号注走 plan_notes 的严格配对；星号注走 extract_star_notes
    # 的"块数+句末"双校验。两条都产出同样结构的 notes/subs，可以合并。
    notes = extract_notes(pages)
    subs, orphan = plan_notes(notes, cleaned) if notes else ({}, {})
    star_notes, star_subs = extract_star_notes(pages, cleaned, bw, bh)
    if star_notes:
        # 星号通道接管这些页。extract_notes 把同一页注区合成了**一整条**无注号
        # 条目，留着会和星号切分出的注重复落盘（实测 83+75=158 条，同一条注
        # 出现两遍）。这些页只保留星号版本。
        for pi in list(star_notes):
            notes.pop(pi, None)
        for key in [k for k in subs if k[0] in star_notes]:
            del subs[key]
        notes.update(star_notes)
        for key, v in star_subs.items():
            subs.setdefault(key, []).extend(v)
    # 图 / 表：不再静默丢弃。plan_regions 决定「插在哪、吃掉哪些行」，
    # emit_images 负责真正把插图裁出来落盘。两者都只在页 JSON 带 regions 字段
    # 时有产出——没跑过版面分析的老工作目录，这一步是空操作（硬回归靠这个）。
    ins, reg_report = plan_regions(pages, cleaned, bw, bh, toc_ids)
    figs = emit_images(pages, src, meta, reg_report, cleaned)
    made = {f["name"] for f in figs}
    gone = set()
    for key in list(ins):
        kept = []
        for b in ins[key]:
            if b[0] == "fig" and b[1] not in made:
                # 图没裁出来（缺页面图 / PIL 不可用）。这时有两条烂路：
                #   ① 仍插 ![](images/xxx.png) → 成书里一串碎图占位符；
                #   ② 把这些行丢掉 → 图没出来、字也没了，两头落空。
                # 走第三条：把该区的 OCR 行按表格的方式**聚成一块**，
                # 既保住文字，又不再散成 30 个独立段落。
                plate = _rows_of(b[3], cleaned)
                text = group_table_text(plate, bw, bh).strip() if plate else ""
                if not text:
                    continue
                kept.append(("tbl", text, b[2]))
                reg_report["uncropped_figures"] = \
                    reg_report.get("uncropped_figures", 0) + 1
                gone.update(b[3])
                continue
            gone.update(b[3])
            kept.append(b[:3])
        if kept:
            ins[key] = kept
        else:
            del ins[key]
    blocks = assemble(pages, cleaned, stream, matched, toc_ids, bw, bh, start,
                      notes=notes, subs=subs, orphan=orphan, ins=ins, gone=gone)
    md, page_map = to_markdown_with_pages(blocks, meta)
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
        "toc_warning": ("未检测到目录条目，成书无章节结构" if not entries else ""),
        "entries": entries,
        "matched_detail": matched,
        "chars": len(md),
        # md 字符偏移 → PDF 页码（1 基）。audit.py 用它把命中的字换算成
        # 「PDF 第几页」，校对的人才能直接翻原书。见 to_markdown_with_pages()。
        "page_map": page_map,
        "notes": {"total": n_notes, "linked": n_linked,
                  "orphan": n_notes - n_linked,
                  "pages": len(notes)},
        # 图 / 表降级的明细：裁了几张、并了几块表、各自吃掉多少行。
        # 这几项是「这次成书为什么和图版对得上」的唯一证据，必须进 report。
        "regions": {
            "figures": reg_report.get("figures", 0),
            "plates": reg_report.get("plates", 0),
            "figure_lines": reg_report.get("figure_lines", 0),
            "tables": reg_report.get("tables", 0),
            "table_lines": reg_report.get("table_lines", 0),
            "images": reg_report.get("images", {}),
        },
    }
    return md, report, cleaned, pages
