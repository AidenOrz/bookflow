"""bookflow 入口：Markdown → EPUB3（含弹窗脚注）。

用法：
  python make_epub.py --md out/book.md --out out/book.epub \
      --title "书名" --author "作者" --publisher "出版社" --cover out/cover.jpg
"""
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import epub3


def main():
    HERE = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--author", default="")
    ap.add_argument("--publisher", default="")
    ap.add_argument("--lang", default="zh-CN")
    ap.add_argument("--cover", default="")
    ap.add_argument("--css", default="", help="自定义样式表；留空用内置样式")
    ap.add_argument("--toc-depth", type=int, default=3)
    a = ap.parse_args()

    if not os.path.exists(a.md):
        sys.exit("找不到 Markdown：%s" % a.md)
    css = a.css or os.path.join(HERE, "assets", "style.css")
    with open(a.md, encoding="utf-8") as f:
        md = f.read()

    # base_dir：md 里写的是 ![](images/fig-p123-1.png) 这种**相对 md 文件**的路径，
    # epub3 要按这个基准去磁盘上找真实文件再打进包。指到 md 所在目录即可，
    # 不依赖当前工作目录（用户可能在任意目录下调用）。
    st = epub3.build(md, a.out, title=a.title, author=a.author,
                     publisher=a.publisher, lang=a.lang,
                     toc_depth=a.toc_depth,
                     cover_jpg=a.cover or None,
                     css_path=css if os.path.exists(css) else None,
                     base_dir=os.path.dirname(os.path.abspath(a.md)))
    errs = epub3.validate(a.out)
    print("章节 %d ｜ 弹窗脚注 %d ｜ 目录项 %d ｜ 文件 %d ｜ 校验错误 %d"
          % (st["chapters"], st["notes"], st["nav"], st["files"], errs))
    print("输出: %s (%.1f KB)" % (a.out, os.path.getsize(a.out) / 1024))
    if errs:
        sys.exit(1)


if __name__ == "__main__":
    main()