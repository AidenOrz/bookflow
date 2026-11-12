"""按 AI 裁决结果改 OCR 结果：只改差异段，不动整行。

裁决表 decisions：{条目序号: 'A'|'B'|('C', '替换文本')}
'A' 表示原样保留，'B' 表示采用第二版本，('C', x) 表示两者都不对、用人工给定的写法。
只替换 A/B 之间**单一差异段**，避免把空格、标点等无关差异一起带进来。

注意：本脚本会**原地覆写** OCR 目录。默认先写一份 .bak 备份；加 --dry-run 只看不改。
"""
import argparse
import json
import os
import shutil
from difflib import SequenceMatcher

# 默认输入；可用命令行覆盖
QUEUE = "out/review_queue.json"
SRC = "ocr_merged"

# 按 review_ai.png 的裁决结果（序号从 1 开始）
# 例：B = 采用第二版本；A = 保留原样；("C", "X") = 两者都不对，改成 X
DECISIONS = {
    1: "B",     # 申→由：它是由柏拉图提出来的
    2: "B",     # 一→——：意思——这两个词
    3: "B",     # 半角逗号 → 全角
    4: "A",     # 「裴颂」：原书即印作颂，通行写法是裴頠 → L3，不改，仅登记
    5: "B",     # 一—→——
    6: "A",     # 规律为己任
    7: None,    # 行尾破折号归属不明
    8: None,    # 上标脚注数字，另处理
    9: "A",     # 概念入手
    10: "B",    # 它已跳到你手上来
    11: None,   # 仅空格差异
    12: "B",    # 已实现出来
    13: "A",    # 决一步步得到提高
    14: "B",    # 我——必然是客观存在的
    15: "A",    # 堕入了另外一种不自由
    16: None,   # 仅空格差异
    17: "A",    # Horstmann.
    18: "B",    # 〈精神现象学〉
    19: "A",    # 「萧董父」：原书字形与通行写法 萧萣父 不符 → L3，不改，仅登记
    20: "A",
}

# 留给人工确认的条目（不改动正文）
NEEDS_HUMAN = [
    (4, "裴颂", "裴頠", "崇有论作者，通行写法为「裴頠」，但原书印的是「颂」"),
    (19, "萧董父", "萧萣父", "《中国哲学史》主编，通行写法待核，原书字形介于两者之间"),
    (20, "萧董父", "萧萣父", "同上"),
    (8, "已3", "已", "行末上标是脚注序号，应转成脚注而非正文数字"),
]


def build_patches(queue, decisions):
    """把裁决表翻译成 (page, y0) -> (原文, 新文) 的补丁表。

    'B' 与 ('C', x) 走同一条路径：定位 A/B 之间的单一差异段，只替换那一段。
    差别只在于换成 B 的对应片段还是人工给的字。
    """
    patches, skipped = {}, []
    for idx, act in decisions.items():
        if act in (None, "A"):
            continue
        c = queue[idx - 1]
        A, B = c["A"], c["B"]
        sm = SequenceMatcher(None, A, B, autojunk=False)
        ops = [o for o in sm.get_opcodes() if o[0] != "equal"]
        if len(ops) != 1:
            skipped.append((idx, "差异段 %d 个，非单点" % len(ops)))
            continue
        _, i1, i2, j1, j2 = ops[0]
        repl = B[j1:j2] if act == "B" else act[1]
        patches[(c["page"], c["y0"])] = (A, A[:i1] + repl + A[i2:])
    return patches, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=QUEUE, help="review_queue.json 路径")
    ap.add_argument("--src", default=SRC, help="要就地修改的 OCR 目录")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要做出的修改，不写文件")
    ap.add_argument("--no-backup", action="store_true", help="不生成 .bak 备份（默认会备份）")
    a = ap.parse_args()

    if not os.path.exists(a.queue):
        raise SystemExit("找不到裁决队列: %s（先跑 make_review.py）" % a.queue)
    if not os.path.isdir(a.src):
        raise SystemExit("找不到 OCR 目录: %s" % a.src)

    queue = json.load(open(a.queue, encoding="utf-8"))
    patches, skipped = build_patches(queue, DECISIONS)

    n = 0
    hit = set()
    for name in sorted(os.listdir(a.src)):
        if not name.endswith(".json"):
            continue
        p = os.path.join(a.src, name)
        d = json.load(open(p, encoding="utf-8"))
        page = name[:-5]
        changed = False
        for L in d["lines"]:
            k = (page, L["y0"])
            if k in patches and L["t"] == patches[k][0]:
                print("  %-12s %-28s -> %s" % (page, L["t"][:26], patches[k][1][:34]))
                L["t"] = patches[k][1]
                changed = True
                hit.add(k)
                n += 1
        if changed and not a.dry_run:
            if not a.no_backup:
                bak = p + ".bak"
                if not os.path.exists(bak):
                    shutil.copy2(p, bak)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)

    print("%s %d 处（队列 %d 条）" % ("将应用" if a.dry_run else "已应用", n, len(patches)))
    miss = [k for k in patches if k not in hit]
    if miss:
        print("未定位到（原文已变？）：", miss[:10])
    if skipped:
        print("跳过：", skipped)
    print("\n留给人工确认（未改动）：")
    for idx, got, want, note in NEEDS_HUMAN:
        print("  #%-2d 原样「%s」 疑似「%s」 —— %s" % (idx, got, want, note))


if __name__ == "__main__":
    main()
