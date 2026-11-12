"""bookflow.qa — 成书前的质量闸门（对应 pdf-set 的"OCR 查漏补缺"环节）。"""
import re
import json
from collections import Counter

CJK = re.compile(r"[\u4e00-\u9fff]")
END = "。！？；：…”』」）)】》"


def check_pages(pages, cleaned, bw, bh):
    issues = []
    for p in pages:
        ls = cleaned[p["idx"]]
        n = len(ls)
        chars = sum(len(L["t"]) for L in ls)
        if n == 0:
            issues.append(("EMPTY_PAGE", p["idx"], "整页无文字"))
        elif n < 4 and chars < 40:
            issues.append(("SPARSE_PAGE", p["idx"], "仅 %d 行 / %d 字" % (n, chars)))
        for L in ls:
            t = L["t"].strip()
            if len(t) <= 2 and CJK.search(t) and L["h"] < 3 * bh:
                issues.append(("SHORT_LINE", p["idx"], t))
            if L.get("s", 1) < 0.86:
                issues.append(("LOW_SCORE", p["idx"], "%.2f %s" % (L["s"], t[:28])))
    return issues


def check_paragraphs(md):
    """段落级检查。

    注意 book.md 的格式是「一段一行 + 空行分隔」，所以不能用「相邻两行是否相同」
    判重复——空行会把 prev 清掉，那样永远报不出来。这里维护全书的段落计数。
    """
    lines = [l.strip() for l in md.split("\n")]
    issues = []
    seen = {}
    last = None
    for i, l in enumerate(lines):
        if not l or l.startswith("#"):
            continue
        if len(l) < 8:
            issues.append(("SHORT_PARA", i, l))
        # 重复段落：同一段落在全书出现 ≥2 次（长段落才有意义，短句可能真是复述）
        if len(l) >= 20:
            if l in seen:
                issues.append(("DUP_PARA", i, l[:30]))
            else:
                seen[l] = i
        # 相邻重复（没有被空行分开的重复行）
        if last == l:
            issues.append(("DUP_PARA", i, l[:30]))
        if l[-1] not in END and len(l) < 40 and not re.search(r"[0-9A-Za-z]$", l):
            issues.append(("NO_END_PUNCT", i, l[:36]))
        last = l
    return issues


def check_coverage(pages, cleaned, bw=None):
    """跨页断裂检查。

    旧判据「上页末行不收句」在中文书里命中率 88%（分段跨页本来就如此），
    是个没有区分度的哨兵值。真正可疑的是两种**信号互相矛盾**的情况：

      A. 上页末行没说完（不收句） + 新页首行却缩进  → 段落在页尾被硬切断
      B. 上页末行已收句        + 新页首行顶格      → 可能是同一段被错误断开

    两种情况都要结合新页首行是否缩进来判断，所以这里先估每页的版心左边界。
    """
    left = {}
    for p in pages:
        ls = [L for L in cleaned[p["idx"]] if L["w"] > 0.3 * p["w"]]
        if ls:
            from collections import Counter
            left[p["idx"]] = Counter(round(L["x0"] / 5) * 5 for L in ls).most_common(1)[0][0]
        else:
            left[p["idx"]] = 0

    out = []
    for a, b in zip(pages, pages[1:]):
        la, lb = cleaned[a["idx"]], cleaned[b["idx"]]
        if not la or not lb or len(lb[0]["t"]) <= 3:
            continue
        ends_sentence = la[-1]["t"][-1] in END
        ind = lb[0]["x0"] - left.get(b["idx"], lb[0]["x0"])
        indented = bw is not None and ind > 1.2 * bw
        if not ends_sentence and indented:
            kind = "断句被切开"       # A
        elif ends_sentence and bw is not None and not indented and ind < 0.5 * bw:
            kind = "疑似连读"          # B
        else:
            continue
        out.append((a["idx"], kind, la[-1]["t"][-24:], lb[0]["t"][:24]))
    return out


FN_REF = re.compile(r"\[\^([^\]]+)\](?!:)")
FN_DEF = re.compile(r"^\[\^([^\]]+)\]:", re.M)


def check_notes(md):
    """脚注完整性：每个引用都要有定义，每个定义都要被引用。

    这条检查是必要的手术前核对：布局阶段算错一个下标，]就 会让正文里出现
    指向不存在注文的链接，阅读器弹出空白框——比不弹窗更糟。
    """
    refs = FN_REF.findall(md)
    defs = FN_DEF.findall(md)
    issues = []
    for r in sorted(set(refs) - set(defs)):
        issues.append(("FN_NO_DEF", 0, r))
    for d in sorted(set(defs) - set(refs)):
        issues.append(("FN_NO_REF", 0, d))
    dup = [k for k, n in Counter(refs).items() if n > 1]
    for k in sorted(dup):
        issues.append(("FN_DUP_REF", Counter(refs)[k], k))
    return issues


def run(md, pages, cleaned, bw, bh):
    pg = check_pages(pages, cleaned, bw, bh)
    par = check_paragraphs(md)
    cov = check_coverage(pages, cleaned, bw)
    fn = check_notes(md)
    cnt = Counter(k for k, _, _ in pg)
    text = CJK.findall(md)
    report = {
        "pages": len(pages),
        "cjk_chars": len(text),
        "total_chars": len(md),
        "issue_counts": dict(cnt),
        "low_score": [x for x in pg if x[0] == "LOW_SCORE"][:40],
        "short_lines": [x for x in pg if x[0] == "SHORT_LINE"][:40],
        "sparse_pages": [x for x in pg if x[0] in ("SPARSE_PAGE", "EMPTY_PAGE")],
        "para_issues": par[:60],
        "cross_page_breaks": len(cov),
        "cross_page_sample": cov[:40],
        "notes": {"refs": len(FN_REF.findall(md)),
                  "defs": len(FN_DEF.findall(md)),
                  "issues": fn},
    }
    return report


def to_text(rep):
    L = []
    L.append("页数 %d ｜ 汉字 %d ｜ 总字符 %d" % (rep["pages"], rep["cjk_chars"], rep["total_chars"]))
    L.append("问题计数: " + ", ".join("%s=%d" % kv for kv in sorted(rep["issue_counts"].items())))
    L.append("空缺页: %s" % (rep["sparse_pages"] or "无"))
    L.append("跨页可疑处: %d（A=段落被页尾切开 / B=疑似两段连读）" % rep["cross_page_breaks"])
    nt = rep.get("notes") or {}
    if nt:
        L.append("脚注: 引用 %d ｜ 定义 %d ｜ 完整性问题 %d"
                 % (nt["refs"], nt["defs"], len(nt["issues"])))
        for k, n, t in nt["issues"][:20]:
            L.append("   [%s] %s%s" % (k, t, "" if k != "FN_DUP_REF" else " ×%d" % n))
    for item in rep["cross_page_sample"]:
        pi, kind, t1, t2 = item
        L.append("   [%s] p%03d→p%03d  %r || %r" % (kind, pi + 1, pi + 2, t1, t2))
    if rep["short_lines"]:
        L.append("可疑短行（前 40）:")
        for k, p, t in rep["short_lines"]:
            L.append("   p%03d %r" % (p + 1, t))
    if rep["low_score"]:
        L.append("低置信行（前 40）:")
        for k, p, t in rep["low_score"]:
            L.append("   p%03d %s" % (p + 1, t))
    if rep["para_issues"]:
        L.append("段落异常（前 60）:")
        for k, i, t in rep["para_issues"]:
            L.append("   [%s] L%d %r" % (k, i, t))
    return "\n".join(L)
