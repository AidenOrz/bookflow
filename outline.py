"""outline — 从 PDF 自带的书签（/Outlines）读目录。

## 为什么优先用书签而不是印刷目录页

印刷目录页要经过 OCR，而目录页恰好是 OCR 最难的版面：字号大、行距紧、
引导点和页码挤在一行。《拉康式主体》实测 23 条只解析出 15 条，还把
「附录1：无意识语言」和「附录2：追踪原因」粘成一条
（「附录1：无意识语言 1219录2：追踪原因」）。

而 PDF 自带的书签是**数字文本**：一个字都不会错，页码是精确的 PDF 页序，
层级就是书签的嵌套深度。本地 111 个 PDF 实测 59 个（53%）带可用书签。

所以只要 PDF 带了像样的书签，目录就**直接搬过来**，不再去 OCR 目录页
（见 layout.build 的 toc_source 分支）。

## 不用自写 PDF 解析器

书签对象可能藏在交叉引用流 / 对象流（ObjStm）里，自写解析器在 ObjStm 上
必然读不到，而读不到时它不会报错——它会安静地返回空目录，这正是本项目
最怕的静默出错。所以走 pypdf（纯 Python，无编译依赖）；没装就明确告警并
退回解析目录页，不猜。

## 页号语义

书签里的页号是 **0 基的 PDF 页序**。下游 layout 的 0 基 idx 是"清洗后
剩下的页"的下标——剔了封底、去了重复页之后两者可能对不上，所以
layout 侧按页图文件名里的页号（p-007.png → 7）还原，见 layout.build。
"""
import os
import re
import sys

import stdio

# 书签里这些条目是版式页，不是章节：留在目录里会在 EPUB 的导航里冒出
# 「封面」「书名」「题词」这类点进去只有一张图的一段。匹配的是**整条**
# 标题（去掉空白后），不做子串匹配——「封面设计」这种真章节名不该被误杀。
SKIP_TITLES = re.compile(
    r"^(?:封面|封底|封二|封三|书名|书名页|扉页|题词|献词|目录|目次|版权|版权页"
    r"|cover|title\s*page|titlepage|contents|copyright|half\s*title)\s*$", re.I)

# 少于这么多条就当"没有可用书签"。1~2 条书签的书（实测有只有「封面」
# 「正文」两三条的）搬过来只会让成书多出两章、其余全文挤在一章里,
# 不如老老实实解析目录页。
MIN_ENTRIES = 3

# 上限：有的工具书书签上千条（本地最多的 1196 条）。全搬进 EPUB 导航
# 没有意义，而且匹配阶段是 O(条目 x 窗口)，太大只是白烧时间。
MAX_ENTRIES = 2000

# 层级上限。本项目 layout 的层级体系是 1/2/3 三档（assemble 的标题块、
# toc_depth 都是按它写的），更深的书签压到 3。
MAX_LEVEL = 3


def available():
    """pypdf 装没装。没装时调用方给一次明确告警，而不是静默没有目录。"""
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


def _clean(title):
    """书签标题规整：去控件字符、折叠空白、去首尾空白。

    只做这一件事——**不猜错别字、不改标点**。书签是权威文本，唯一的问题
    是有的生成工具会留 U+200B 这类不可见字符，那会让下游的标题匹配和
    EPUB 的 nav 都出怪。
    """
    t = title or ""
    t = "".join(c for c in t if c == "\t" or ord(c) >= 0x20)
    t = re.sub(r"[\u200b-\u200f\u2028\u2029\ufeff]", "", t)
    t = re.sub(r"[ \u00a0\u3000]+", " ", t)
    return t.strip()


def read_outline(pdf):
    """读 PDF 书签，返回 (条目列表, 人话说明)。

    条目是 {"title": str, "pdf_page": int(0 基), "level": int(1..3)}，
    **按文档顺序**（书签本身是有序的）。

    返回空列表的原因都放在说明里（没装 pypdf / 没有书签 / 书签太少 /
    读失败），调用方据此决定是告警还是静默走目录页——不能一律静默，
    用户得知道"这本书明明有目录，为什么成书没章"。
    """
    if not pdf or not os.path.exists(pdf):
        return [], "没有 PDF 路径，跳过书签"
    if not available():
        return [], "没装 pypdf，读不了书签（pip install pypdf 即可）：%s" % pdf
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf)
        raw = reader.outline
    except Exception as e:                      # noqa: BLE001  坏 PDF 什么都能抛
        return [], "读 PDF 书签失败（%s）：%s" % (type(e).__name__, pdf)
    if not raw:
        return [], "PDF 没有书签（/Outlines 为空）"

    out = []

    def walk(items, depth):
        for it in items:
            if isinstance(it, list):
                # pypdf 用嵌套 list 表示"这几条是上一条的子节点"
                walk(it, depth + 1)
                continue
            title = _clean(getattr(it, "title", "") or "")
            if not title or SKIP_TITLES.match(title):
                continue
            try:
                page = reader.get_destination_page_number(it)
            except Exception:                   # noqa: BLE001
                # 页码解析不了的条目留着没用：没有落点就没法在正文里定位。
                continue
            if page is None or page < 0:
                continue
            out.append({"title": title,
                        "pdf_page": int(page),
                        "level": min(depth + 1, MAX_LEVEL)})
            if len(out) >= MAX_ENTRIES:
                return

    walk(raw, 0)
    if len(out) < MIN_ENTRIES:
        return [], ("PDF 只有 %d 条可用书签（下限 %d），改用解析目录页"
                    % (len(out), MIN_ENTRIES))
    # 层级归一：第一层永远是 1，否则「只有一级子节点」的书签会把所有条目
    # 压到 2 级（EPUB 导航的第一个 heading 变成容器，读者要多点一层）。
    top = min(e["level"] for e in out)
    if top > 1:
        for e in out:
            e["level"] = max(1, e["level"] - top + 1)
    return out, "PDF 书签 %d 条" % len(out)


def main(argv=None):
    import argparse
    import json
    ap = argparse.ArgumentParser(description="读 PDF 书签（调试用）")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--json", help="把条目写到这个 JSON 文件")
    a = ap.parse_args(argv)
    entries, why = read_outline(a.pdf)
    print(why)
    for e in entries:
        print("  %s%s" % ("  " * (e["level"] - 1), e["title"]),
              "-> pdf p%d" % (e["pdf_page"] + 1))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=1)
        print("写出 %s" % a.json)
    return 0


if __name__ == "__main__":
    stdio.utf8_stdio()
    sys.exit(main())
