"""对照两套 OCR 结果：行级差异、字形级差异，并给出可疑修正清单。"""
import json, glob, os, sys, re
from difflib import SequenceMatcher
import jsonio

A = sys.argv[1] if len(sys.argv) > 1 else "ocr"
B = sys.argv[2] if len(sys.argv) > 2 else "ocr_medium"


def load(d):
    out = {}
    for f in sorted(glob.glob(os.path.join(d, "*.json"))):
        out[os.path.basename(f)] = jsonio.read(f)
    return out


a, b = load(A), load(B)
pages = sorted(set(a) & set(b))
print("对比 %d 页（%s %d / %s %d）" % (len(pages), A, len(a), B, len(b)))

tot_l = tot_c = 0
same_line = diff_line = 0
score_a = score_b = 0.0
n_score = 0      # A 版（通常是 small）的行数
n_b = 0          # B 版行数；两版行数不同，分母必须分开算

changed = []          # (page, small, medium, sa, sb)
for p in pages:
    la, lb = a[p]["lines"], b[p]["lines"]
    ta = [L["t"] for L in la]
    tb = [L["t"] for L in lb]
    tot_l += max(len(ta), len(tb))
    tot_c += max(sum(len(x) for x in ta), sum(len(x) for x in tb))
    for L in la:
        score_a += L.get("s", 0); n_score += 1
    n_b += len(lb)
    for L in lb:
        score_b += L.get("s", 0)
    if ta == tb:
        same_line += 1
        continue
    diff_line += 1
    sm = SequenceMatcher(None, ta, tb, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and (i2 - i1) == (j2 - j1):
            for k in range(i2 - i1):
                changed.append((p, ta[i1 + k], tb[j1 + k], la[i1 + k].get("s"), lb[j1 + k].get("s")))
        else:
            changed.append((p, " / ".join(ta[i1:i2])[:120], " / ".join(tb[j1:j2])[:120], None, None))

print("页级：完全相同 %d，有差异 %d" % (same_line, diff_line))
print("平均置信度：%s %.4f  %s %.4f" % (
    A, score_a / max(n_score, 1), B, score_b / max(n_b, 1)))
print("差异行数 %d" % len(changed))

# 只保留字形级小改动（同一行、长度接近）
charfix = [(p, x, y, sa, sb) for p, x, y, sa, sb in changed
           if sa and sb and abs(len(x) - len(y)) <= 3 and len(x) > 6]
print("字形级候选修正 %d 处" % len(charfix))

# 按变化类型归类
kinds = {}
for p, x, y, sa, sb in charfix:
    sm = SequenceMatcher(None, x, y, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace" and (i2 - i1) <= 2 and (j2 - j1) <= 2:
            key = (x[i1:i2], y[j1:j2])
            kinds.setdefault(key, []).append((p, x, y, sa, sb))
print("\n=== 高频字级替换（出现 >=2 次，按次数排序）===")
for (o, n), lst in sorted(kinds.items(), key=lambda kv: -len(kv[1]))[:25]:
    if len(lst) < 2:
        continue
    print("  %-4s -> %-4s  %2d 次   例：%s" % (repr(o), repr(n), len(lst), lst[0][1][:32]))

json.dump([{"page": p, "small": x, "medium": y, "s_small": sa, "s_medium": sb}
           for p, x, y, sa, sb in charfix],
          open("cmp_medium.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\n明细已写入 cmp_medium.json")
