"""bookflow.disagree — 两版 OCR 交叉比对，产出"两模型不一致"的候选点。

两个独立模型很少在同一位置犯同一个错，所以**分歧点 ≈ 候选错误点**。
这是目前召回率最高的通道。

产出三态：
  A    词表倾向 A 对
  B    词表倾向 B 对
  ?    词表无法判定 → 交多模态模型看图裁决

用法：
  python disagree.py --a ocr_fix --b ocr_alt --out out/disagree.json
"""
import os, json, glob, sys, argparse, re
from difflib import SequenceMatcher

# 标点/符号差异不是错字，直接剔除（第二版模型偏好半角标点，这类分歧有 150+ 处）
PUNCT = re.compile(r"[，。、；：！？“”‘’（）《》〈〉—…·,.!?;:()\"']")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit import load_dict, DICT_PATH
import jsonio

WORD_MIN = 200          # 判定"是常用词"的词频门槛
RATIO = 3.0             # 两侧词频差多少倍才算分出胜负
CTX = 2                 # 差异点左右各取几字做窗口


# 与 layout.py 同口径（全角标点算 0.55 而非 1.0）。分歧过滤用 elen 的
# max_diff 闸门，口径不一会让「……」「①②③」这类标点密集行被误判为长度差超限。
from layout import elen  # noqa: E402


def best_word(s, lo, hi, dic):
    """s 中跨过 [lo,hi) 这段差异的所有 2/3-gram 里，最像个真词的那个的词频。

    注意不能数"有几个词"——词表收录了常见错写（「自已」263、「异已」3），
    所以要比**词频强弱**：自己 90933 对 自已 263，胜负一目了然。
    """
    best = 0
    for n in (2, 3):
        for k in range(lo - n + 1, hi):
            if k < 0 or k + n > len(s):
                continue
            if k + n <= lo or k >= hi:      # 不与差异段相交的窗口不算
                continue
            f = dic.get(s[k:k + n], 0)
            if f > best:
                best = f
    return best


def is_punct_only(ta, tb):
    """两行的差异是否只发生在标点/符号上。"""
    sm = SequenceMatcher(None, ta, tb, autojunk=False)
    diffs = [(ta[i1:i2], tb[j1:j2]) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]
    if not diffs:
        return True
    for x, y in diffs:
        if x and not all(PUNCT.match(c) for c in x):
            return False
        if y and not all(PUNCT.match(c) for c in y):
            return False
    return True


def judge(ta, tb, dic):
    sm = SequenceMatcher(None, ta, tb, autojunk=False)
    best = None
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "replace":
            continue
        if (i2 - i1) > 3 or (j2 - j1) > 3:
            continue
        ha = best_word(ta, i1, i2, dic)
        hb = best_word(tb, j1, j2, dic)
        if ha >= RATIO * max(hb, 1) and ha >= WORD_MIN:
            verdict = "A"
        elif hb >= RATIO * max(ha, 1) and hb >= WORD_MIN:
            verdict = "B"
        else:
            verdict = "?"
        info = ("A「%s」最强词频 %d / B「%s」最强词频 %d"
                % (ta[i1:i2], ha, tb[j1:j2], hb))
        if best is None or verdict != "?":
            best = (verdict, info)
        if verdict != "?":
            break
    return best or ("?", "无法定位差异")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default="ocr_fix", help="主用版本（通常是小模型）")
    ap.add_argument("--b", default="ocr_alt", help="第二意见版本")
    ap.add_argument("--out", default="out/disagree.json")
    ap.add_argument("--txt", default="out/disagree.txt")
    ap.add_argument("--max-diff", type=int, default=3, help="允许的字数差")
    ap.add_argument("--apply", default="", help="把词表已判定为 B 对的条目回填到该目录")
    a = ap.parse_args()

    dic = load_dict(DICT_PATH)
    fa = {os.path.basename(f): f for f in glob.glob(os.path.join(a.a, "*.json"))}
    fb = {os.path.basename(f): f for f in glob.glob(os.path.join(a.b, "*.json"))}
    common = sorted(set(fa) & set(fb))
    print("比对 %d 页（%s vs %s）" % (len(common), a.a, a.b))

    cands = []
    for name in common:
        da = jsonio.read(fa[name])
        db = jsonio.read(fb[name])
        la, lb = da["lines"], db["lines"]
        ta = [L["t"] for L in la]
        tb = [L["t"] for L in lb]
        if ta == tb:
            continue
        sm = SequenceMatcher(None, ta, tb, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            for k in range(min(i2 - i1, j2 - j1)):
                A, B = la[i1 + k], lb[j1 + k]
                if A["t"] == B["t"]:
                    continue
                if abs(elen(A["t"]) - elen(B["t"])) > a.max_diff:
                    continue
                if is_punct_only(A["t"], B["t"]):
                    continue
                verdict, why = judge(A["t"], B["t"], dic)
                cands.append({
                    "page": name.replace(".json", ""),
                    "y0": A["y0"], "y1": A["y1"], "x0": A["x0"], "x1": A["x1"],
                    "A": A["t"], "B": B["t"], "verdict": verdict, "why": why,
                    "sA": A.get("s"), "sB": B.get("s"),
                })

    n = len(cands)
    va = sum(1 for c in cands if c["verdict"] == "A")
    vb = sum(1 for c in cands if c["verdict"] == "B")
    vq = n - va - vb
    print("不一致 %d 处：词表判 A 对 %d，判 B 对 %d，需裁决 %d" % (n, va, vb, vq))

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    json.dump(cands, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    lines = ["两版 OCR 分歧 %d 处（A=%s，B=%s）" % (n, a.a, a.b), ""]
    for c in cands[:400]:
        lines.append("  [%s] %-12s %s\n        A: %s\n        B: %s" %
                     (c["verdict"], c["page"], c["why"], c["A"][:60], c["B"][:60]))
    open(a.txt, "w", encoding="utf-8").write("\n".join(lines))
    print("明细 -> %s" % a.out)

    if a.apply:
        os.makedirs(a.apply, exist_ok=True)
        fixed = {(c["page"], c["y0"], c["A"]): c["B"]
                 for c in cands if c["verdict"] == "B"}
        n_apply = 0
        for name in common:
            d = jsonio.read(fa[name])
            page = name.replace(".json", "")
            for L in d["lines"]:
                k = (page, L["y0"], L["t"])
                if k in fixed:
                    L["t"] = fixed[k]
                    n_apply += 1
            json.dump(d, open(os.path.join(a.apply, name), "w", encoding="utf-8"),
                      ensure_ascii=False)
        print("回填 %d 处（词表判定 B 对）-> %s" % (n_apply, a.apply))


if __name__ == "__main__":
    main()
