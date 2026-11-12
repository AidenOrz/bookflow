"""bookflow.audit — 找出"字数正常、置信度也不低，只有语义才知道错"的字。

前置环节能抓"漏字"，抓不了「进人/进入」「自已/自己」「异已/异己」这类替换型错误。
这里用一条很硬的判据：**词表归属**。

  窗口「进人」不在词表里，把它的人换成形近的入得到「进入」，在词表里且是常用词
  → 可疑。

反过来，「自己」本身就在词表里，因此不会被误报；「深入的」因为「深入」是词，
也不会被误报。这比按字频打分稳得多（字频会被"的/有"这类高频字带偏）。

只输出清单，不自动改。形近字对是双向的，误报集中在"两个词都成立"的情况
（如「反映/反应」），需人工或模型裁决。
"""
import re, json, os, argparse
from collections import defaultdict
import jsonio

def _find_dict():
    """找 jieba 词典：先用项目自带 dict.txt，再从 jieba 的安装位置推。

    不写死绝对路径——换机器/换 venv 就失效。`jieba.__file__` 直接给出
    当前解释器里 jieba 的真实位置，跨平台也成立。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(here, "dict.txt")]
    try:
        import jieba
        cands.append(os.path.join(os.path.dirname(jieba.__file__), "dict.txt"))
    except Exception:
        pass
    for c in cands:
        if os.path.exists(c):
            return c
    return None


DICT_PATH = _find_dict()

# OCR 常见形近字对（双向）。刻意剔除"的/得""在/再""做/作"这类语法用字：
# 它们既不是主要 OCR 混淆源，两个词又都成立，放进来只会制造噪声。
CONFUSE = [
    ("人", "入"), ("己", "已"), ("己", "巳"), ("未", "末"), ("日", "曰"),
    ("土", "士"), ("干", "千"), ("白", "自"), ("木", "本"),
    ("力", "刀"), ("田", "由"), ("乌", "鸟"), ("侯", "候"), ("折", "析"),
    ("辨", "辩"), ("免", "兔"), ("止", "正"), ("令", "今"), ("天", "夭"),
    ("申", "由"), ("归", "日"), ("贝", "见"), ("且", "旦"),
    ("儿", "几"), ("迥", "回"), ("充", "冲"), ("微", "徽"), ("哀", "衰"),
    ("竭", "揭"), ("漫", "慢"), ("籍", "藉"), ("治", "冶"), ("形", "刑"),
    ("即", "既"), ("拔", "拨"), ("史", "吏"),
    ("析", "折"), ("斩", "渐"), ("概", "慨"), ("戊", "戌"), ("戌", "戍"),
    ("己", "已"), ("巳", "已"), ("祟", "崇"), ("茶", "荼"), ("奈", "捺"),
    ("搏", "博"), ("辩", "辨"), ("粟", "栗"), ("盲", "育"), ("肓", "盲"),
]
CJK = re.compile(r"[\u4e00-\u9fff]")


def load_dict(path, max_len=4):
    """词表：只保留长度 ≤ max_len 的词及其词频。"""
    d = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.split()
            if len(p) < 2:
                continue
            w = p[0]
            if len(w) > max_len or not all(CJK.match(ch) for ch in w):
                continue
            try:
                d[w] = int(p[1])
            except ValueError:
                continue
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", default="out/book.md")
    ap.add_argument("--out", default="out/audit.txt")
    ap.add_argument("--json", default="out/audit.json")
    ap.add_argument("--min-alt", type=int, default=200, help="替换后词的词频下限")
    ap.add_argument("--top", type=int, default=200)
    ap.add_argument("--ratio", type=float, default=3.0,
                    help="候选词频须为原写法词频的几倍")
    ap.add_argument("--veto", type=int, default=5,
                    help="错误写法在全书出现 >= 此次数则丢弃（说明它是本书正常用词）")
    a = ap.parse_args()

    dic = load_dict(DICT_PATH)
    print("词表 %s：%d 个词" % (DICT_PATH, len(dic)))
    text = jsonio.read_text(a.md)

    alt_of = defaultdict(set)
    for x, y in CONFUSE:
        alt_of[x].add(y)
        alt_of[y].add(x)

    from collections import Counter as _C
    bookfreq = _C()
    for i in range(len(text) - 3):
        for n in (2, 3):
            w = text[i:i + n]
            if all(CJK.match(c) for c in w):
                bookfreq[w] += 1

    hits, seen = [], set()
    for n in (2, 3):                     # 二字词、三字词（来得及/不得已 这类）
        for i in range(len(text) - n + 1):
            w = text[i:i + n]
            if not all(CJK.match(c) for c in w):
                continue
            # 词表里可能误收错写法（如「异已」），所以不能只看"在不在表里"，
            # 还要看它相对候选词是否足够强。
            cur_f = dic.get(w, 0)
            if cur_f >= a.min_alt:       # 本身就是常用词 → 放过
                continue
            # 边界判据：若窗口与相邻字能组成词（国人/光明/静止/即使/个人），
            # 说明这个窗口跨了词边界，即使替换后成词也是巧合，排除。
            prev_ch = text[i - 1] if i > 0 else ""
            next_ch = text[i + n] if i + n < len(text) else ""
            if prev_ch and CJK.match(prev_ch) and (prev_ch + w) in dic:
                continue
            if next_ch and CJK.match(next_ch) and (w + next_ch) in dic:
                continue
            if prev_ch and CJK.match(prev_ch) and (prev_ch + w[0]) in dic:
                continue
            if next_ch and CJK.match(next_ch) and (w[-1] + next_ch) in dic:
                continue
            if bookfreq.get(w, 0) >= a.veto:   # 全书高频 → 是本书正常写法
                continue
            for j, ch in enumerate(w):
                for alt in alt_of.get(ch, ()):
                    cand = w[:j] + alt + w[j + 1:]
                    f = dic.get(cand, 0)
                    if f < a.min_alt or f < a.ratio * max(cur_f, 1):
                        continue
                    key = (i, cand)
                    if key in seen:
                        continue
                    seen.add(key)
                    hits.append({
                        "pos": i, "len": n, "wrong": w, "right": cand, "freq": f,
                        "ctx": text[max(0, i - 20):i + n + 18].replace("\n", " "),
                    })
                    break
                else:
                    continue
                break

    hits.sort(key=lambda h: -h["freq"])
    lines = ["可疑字级错误 %d 处（判据：原词不在词表，形近字替换后成为词频≥%d 的常用词）"
             % (len(hits), a.min_alt), ""]
    for h in hits[:a.top]:
        lines.append("  %-46s  %s → %s   (词频 %d)" %
                     (h["ctx"], h["wrong"], h["right"], h["freq"]))
    txt = "\n".join(lines)
    print(txt[:4000])
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    open(a.out, "w", encoding="utf-8").write(txt)
    json.dump(hits, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n共 %d 处；明细 -> %s" % (len(hits), a.json))


if __name__ == "__main__":
    main()
