"""bookflow 入口：OCR 结果 → 结构化 Markdown + 质检报告。

用法：
  python make_md.py --src ./ocr --out ./out \
      --title "黑格尔辩证法讲演录" --author "邓晓芒" --publisher "北京大学出版社"
"""
import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import layout
import qa
import fix
import pagedoc


def main():
    ap = argparse.ArgumentParser()
    HERE = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--src", default=os.path.join(HERE, "ocr"))
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    ap.add_argument("--title", default="")
    ap.add_argument("--author", default="")
    ap.add_argument("--publisher", default="")
    ap.add_argument("--conservative-fix", action="store_true",
                    help="形近字一律只出候选、正文不动。默认会改（排除表的已知漏洞已补），"
                         "遇到没见过的排版或要逐条人工过 fix_candidates 时用这个")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    # 元数据要落进 report.json：下游 epub 阶段没带 --author 时回落到这里，
    # 否则重打包就得每次重新敲命令行（作者/出版社只存在人的记忆里）。
    # out_dir 一并交给 layout：它要用这个目录存从 pages/*.png 裁出来的插图
    # （layout 自己不知道 out 在哪，而图必须和 book.md 落在同一处，
    #  epub3 才能按相对路径把 images/fig-*.png 一起打进包）。
    meta = {"title": a.title, "author": a.author, "publisher": a.publisher,
            "out_dir": a.out}
    md, rep, cleaned, pages = layout.build(a.src, meta)
    md, fixed, fix_detail = fix.apply(md, conservative=a.conservative_fix)
    q = qa.run(md, pages, cleaned, rep["bw"], rep["bh"])

    # 三个产物要么全写出、要么一个都不写：先落 .tmp 再统一 rename。
    # 否则中途抛异常会留下「book.md 有、report.json 没有」的半成品，
    # 下游 stage_audit 会读到错配的产物。
    def write_atomic(path, text):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)

    md_path = os.path.join(a.out, "book.md")
    write_atomic(md_path, md)
    write_atomic(os.path.join(a.out, "qa.txt"), qa.to_text(q))
    # fix_detail 里混着两种条目：action=fix 已自动改写，action=candidate 只出候选
    # （单字 + 排除表的上下文规则，排除集是开放集必然漏项，改错比漏改更伤）。
    # 拆成两块写，report 的读者不用自己过滤。
    # report.json 也要有版本标识：它是 audit/decisions/epub 三个阶段的唯一输入，
    # 字段语义一变（例如 fix_candidates 从 list 改成 dict），下游会静默误读。
    # 用 pagedoc.stamp_report 只加 schema 三字段，不动任何既有键。
    report = pagedoc.stamp_report({
        "book": meta,
        "fix": fixed,
        "fix_detail": fix.auto_detail(fix_detail),
        "fix_candidates": fix.candidates(fix_detail),
        "layout": {k: v for k, v in rep.items() if k not in ("entries", "matched_detail")},
        "entries": rep["entries"], "matched": rep["matched_detail"],
        "qa": q,
    }, pagedoc.producer("make_md"),
        # 影响成书的阶段参数快照：换一个值就应该能解释「为什么这次成书不一样了」
        params={"conservative_fix": bool(a.conservative_fix),
                "src": os.path.basename(os.path.normpath(a.src))})
    write_atomic(os.path.join(a.out, "report.json"),
                 json.dumps(report, ensure_ascii=False, indent=1))

    print("页数 %d ｜ 目录页 %s ｜ 条目 %d ｜ 命中 %d ｜ 层级 %s" % (
        rep["pages"], [p + 1 for p in rep["toc_pages"]], rep["toc_entries"],
        rep["matched"], rep["levels"]))
    print("字符 %d ｜ 汉字 %d ｜ 修正 %s" % (len(md), q["cjk_chars"], fixed or "无"))
    n_cand = len(fix.candidates(fix_detail))
    if n_cand:
        print("待裁决形近字 %d 处（未改正文，见 report.json 的 fix_candidates）" % n_cand)
    nt = rep["notes"]
    if nt["total"]:
        print("脚注 %d 条 ｜ 可弹窗 %d ｜ 页尾注 %d ｜ 来自 %d 页"
              % (nt["total"], nt["linked"], nt["orphan"], nt["pages"]))
    print("未命中条目:")
    hit = {m["title"] for m in rep["matched_detail"]}
    for e in rep["entries"]:
        if e["title"] not in hit:
            # page 允许为 None（目录条目折行、页码丢失），不能直接 %d
            pg = e["page"] if e.get("page") is not None else "-"
            print("   L%d %s (印 %s)" % (e["level"], e["title"], pg))
    print("\n--- 章节结构 ---")
    for m in rep["matched_detail"]:
        print("   %s%s  [pdf p%s / 印 %s / %.2f]" % (
            "  " * (m["level"] - 1), m["title"],
            (m["page"] + 1) if m.get("page") is not None else "-",
            m.get("printed_page") if m.get("printed_page") is not None else "-",
            m.get("score", 0)))
    print("\n输出: %s" % md_path)


if __name__ == "__main__":
    main()
