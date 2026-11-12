# -*- coding: utf-8 -*-
"""封面：从首页扫描图自动裁出书封，并注入已生成的 EPUB。

难点在于"哪些像素是封面"：
- 扫描件四周常有灰边/阴影，直接整页缩放会带进脏边；
- 首页不一定是封面（可能是扉页、插图、甚至空白），此时应当拒绝而不是硬裁；
- 油画类封面（如古画书封）整体偏暗，用"亮区"判据会裁丢。

策略：先用**墨迹占据的边界框**（非纸色像素的外接矩形）定位内容区，
再用宽高比与面积比判断这一页是否值得当封面；不值得就返回 None，
交给调用方决定（不注入封面，总比塞一张白图好）。
"""
import os
import sys
import zipfile

from PIL import Image

COVER_XHTML = '''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN" lang="zh-CN">
<head><title>封面</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
<section epub:type="cover" class="cover-page"><img src="cover.jpg" alt="{title}"/></section>
</body>
</html>
'''


def content_bbox(a, paper_pct=90, margin=2):
    """返回内容区 (x0, y0, x1, y1)：非纸色像素的外接矩形。

    a 是灰度 numpy 数组。纸色取高分位数（扫描页大片是纸），
    比纸色暗 margin 以上的像素算"有东西"。这比"找最长亮区"稳：
    后者在整页深色封面（油画、照片）上会把封面本身当成灰边裁掉。
    """
    import numpy as np
    paper = float(np.percentile(a, paper_pct))
    ink = a < (paper - margin)
    cols = np.where(ink.any(axis=0))[0]
    rows = np.where(ink.any(axis=1))[0]
    if len(cols) == 0 or len(rows) == 0:
        return None
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


def _trim_bottom_watermark(a, x0, x1, y1, H, ink_ratio=0.35):
    """把底部的稀疏水印带从 y1 里裁掉，返回新的 y1。

    判据不是"有没有墨"，而是**墨迹密度**：水印/页脚是一条稀疏的小字带
    （实测占该行像素 6%~13%），而封面图案/正文行是稠密的（>60%）。
    自下往上走，跳过空行后，把连续的"低密度行"整段去掉。

    不能用"上方有没有空白"来判断：整幅油画的封面，底部水印紧贴图案，
    中间没有空白，那样会漏裁（实测 p-001 就是这样）。
    """
    import numpy as np
    band = a[:, x0:x1]
    paper = float(np.percentile(band, 90))
    dens = (band < paper - 2).sum(axis=1) / float(max(band.shape[1], 1))
    y = min(y1, H) - 1
    # 跳过下方的空行
    while y > 0 and dens[y] < 0.005:
        y -= 1
    bottom = y
    # 往上吃低密度行（水印带）
    while y > 0 and dens[y] < ink_ratio:
        y -= 1
    top = y + 1
    seg_h = bottom - top + 1
    # 只裁"薄带"：太高的话那是正文/图案，不能动
    if seg_h <= 0 or seg_h > 0.06 * H:
        return y1
    return top


def make_cover(page_path, out_jpg, size=(1200, 1600)):
    """裁出封面本体。返回输出路径；判定这页不适合当封面时返回 None。"""
    import numpy as np
    im0 = Image.open(page_path).convert("RGB")
    a = np.array(im0.convert("L"), dtype=float)
    H, W = a.shape

    box = content_bbox(a)
    if box is None:
        print("封面: 首页没有可识别的内容（整页空白），跳过")
        return None
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    area_ratio = (bw * bh) / float(W * H)
    aspect = bh / float(max(bw, 1))

    # ① 内容区太小：多半是空白页或只有几行字（扉页），不适合当封面
    if area_ratio < 0.25:
        print("封面: 首页内容区只占 %.0f%% 面积，判定为扉页/空白页，跳过"
              % (area_ratio * 100))
        return None
    # ② 宽高比离谱：扫描歪了或只裁到一条
    if not (0.9 <= aspect <= 2.2):
        print("封面: 内容区宽高比 %.2f 异常，跳过" % aspect)
        return None

    # ③ 裁掉底部水印带：扫描件常在页脚压一行「PDF 文件使用 xxx 试用版本创建」。
    # 水印是整页最底部的窄条，内容是小的、低对比的文字；这里按"最后一段连续墨迹
    # 与上方留白分离"来裁，而不是写死高度。
    y1 = _trim_bottom_watermark(a, x0, x1, y1, H)

    im = im0.crop((x0, y0, x1, y1))
    tw, th = size
    s = min(tw / im.width, th / im.height)
    im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
    canvas = Image.new("RGB", (tw, th), "#FFFFFF")
    canvas.paste(im, ((tw - im.width) // 2, (th - im.height) // 2))
    canvas.save(out_jpg, "JPEG", quality=92)
    print("封面: %s (%d KB) 内容区 %s，占页面 %.0f%%"
          % (out_jpg, os.path.getsize(out_jpg) // 1024, (x0, y0, x1, y1),
             area_ratio * 100))
    return out_jpg


def inject(epub, title, cover_jpg):
    with zipfile.ZipFile(epub) as z:
        data = {i.filename: z.read(i.filename) for i in z.infolist()}
    if "cover.jpg" in data:
        print("已存在封面，跳过注入")
        return
    opf = data["content.opf"].decode("utf-8")
    opf = opf.replace("</manifest>",
                      '<item id="coverpage" href="cover.xhtml" media-type="application/xhtml+xml"/>'
                      '<item id="cover-image" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>'
                      "</manifest>")
    opf = opf.replace('<spine toc="ncx">', '<spine toc="ncx"><itemref idref="coverpage"/>')
    opf = opf.replace("</metadata>", '<meta name="cover" content="cover-image"/></metadata>')
    data["content.opf"] = opf.encode("utf-8")
    data["cover.xhtml"] = COVER_XHTML.format(title=title).encode("utf-8")
    with open(cover_jpg, "rb") as f:
        data["cover.jpg"] = f.read()

    order = ["mimetype"] + [n for n in data if n != "mimetype"]
    tmp = epub + ".tmp"
    with zipfile.ZipFile(tmp, "w") as z:
        for n in order:
            comp = zipfile.ZIP_STORED if n == "mimetype" else zipfile.ZIP_DEFLATED
            z.writestr(n, data[n], compress_type=comp)
    os.replace(tmp, epub)
    print("封面已注入:", epub)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", help="封面扫描页，如 pages/p-001.png")
    ap.add_argument("--jpg", default="cover.jpg")
    ap.add_argument("--epub")
    ap.add_argument("--title", default="封面")
    args = ap.parse_args()
    cover = args.jpg
    if args.page:
        cover = make_cover(args.page, args.jpg)
        if cover is None:
            sys.exit(0)          # 这页不适合当封面，安静跳过
    if args.epub and cover and os.path.exists(cover):
        inject(args.epub, args.title, cover)
