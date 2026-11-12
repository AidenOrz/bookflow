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


def main():
    ap = argparse.ArgumentParser()
    HERE = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--src", default=os.path.join(HERE, "ocr"))
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    ap.add_argument("--title", default="")
    ap.add_argument("--author", default="")
    ap.add_argument("--publisher", default="")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    md, rep, cleaned, pages = layout.build(a.src, {"title": a.title})
    md, fixed, fix_detail = fix.apply(md)
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
    write_atomic(os.path.join(a.out, "report.json"), json.dumps(
        {"fix": fixed, "fix_detail": fix_detail,
         "layout": {k: v for k, v in rep.items() if k not in ("entries", "matched_detail")},
         "entries": rep["entries"], "matched": rep["matched_detail"],
         "qa": q}, ensure_ascii=False, indent=1))

    print("页数 %d ｜ 目录页 %s ｜ 条目 %d ｜ 命中 %d ｜ 层级 %s" % (
        rep["pages"], [p + 1 for p in rep["toc_pages"]], rep["toc_entries"],
        rep["matched"], rep["levels"]))
    print("字符 %d ｜ 汉字 %d ｜ 修正 %s" % (len(md), q["cjk_chars"], fixed or "无"))
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
