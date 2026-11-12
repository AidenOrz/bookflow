"""run_layout — 用 PP-DocLayoutV3 给已有 OCR 结果补「版面区域」标签。

为什么需要这一步：
RapidOCR 只给出"行框"，不知道这一行是正文、脚注、页眉还是页码。layout.py 只能靠
位置猜，实测结果是脚注整段混进正文。版面分析能直接把这些区域标出来，
成本是每页约 0.3 秒。

设计：**纯增量**。它读现有的 ocr JSON，给每行加一个 "region" 字段，写到一个新目录。
老 JSON（没有 region 字段）在 layout.py 里行为完全不变，所以这是可选增强。

用法：
  python run_layout.py --src run/ocr_fix --pages run/pages --dst run/ocr_layout
之后用 --ocr-src ocr_layout 跑 md 阶段。
"""
import argparse
import glob
import os
import sys

import numpy as np
from PIL import Image

import jsonio

# 这些区域不进正文流
DROP_REGIONS = {"footnote", "vision_footnote", "footer", "header",
                "page_header", "page_footer", "number", "page_number"}


def load_engine(use_dml=True, model_type="pp_doc_layoutv3"):
    """版面分析用的是独立的 rapid_layout 包，不是 RapidOCR 的开关。"""
    from rapid_layout import RapidLayout
    cfg = {"use_dml": True} if use_dml else {}
    return RapidLayout(model_type=model_type, engine_cfg=cfg)


def page_regions(engine, img_path):
    im = Image.open(img_path).convert("RGB")
    r = engine(np.asarray(im))
    out = []
    boxes = getattr(r, "boxes", None)
    if boxes is None:
        return out
    names = (getattr(r, "class_names", None)
             or getattr(r, "txts", None) or [""] * len(boxes))
    scores = getattr(r, "scores", None) or [0.0] * len(boxes)
    for b, n, s in zip(boxes, names, scores):
        pts = np.asarray(b, dtype=float)
        if pts.ndim == 2:
            xs, ys = pts[:, 0], pts[:, 1]
        else:
            xs, ys = pts[0::2], pts[1::2]
        out.append({"x0": round(float(xs.min()), 1), "y0": round(float(ys.min()), 1),
                    "x1": round(float(xs.max()), 1), "y1": round(float(ys.max()), 1),
                    "cls": str(n), "s": round(float(s), 4)})
    return out


def tag_lines(lines, regions, pad=8):
    for L in lines:
        cx = (L["x0"] + L["x1"]) / 2
        cy = (L["y0"] + L["y1"]) / 2
        best, best_area = None, None
        for R in regions:
            if (R["x0"] - pad <= cx <= R["x1"] + pad
                    and R["y0"] - pad <= cy <= R["y1"] + pad):
                a = (R["x1"] - R["x0"]) * (R["y1"] - R["y0"])
                if best_area is None or a < best_area:
                    best, best_area = R, a
        L["region"] = best["cls"] if best else "unassigned"
        if best is not None:
            L["r"] = best["s"]
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="已有 OCR JSON 的目录")
    ap.add_argument("--pages", required=True, help="对应页面 PNG 目录")
    ap.add_argument("--dst", required=True, help="输出目录（写入带 region 的 JSON）")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "dml"])
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(a.src, "*.json")))
    if not files:
        sys.exit("源目录没有页级 JSON：%s" % a.src)
    os.makedirs(a.dst, exist_ok=True)

    use_dml = a.device in ("auto", "dml")
    engine = load_engine(use_dml)

    done = skipped = 0
    stats = {}
    for f in files:
        name = os.path.basename(f)[:-5]
        out_path = os.path.join(a.dst, os.path.basename(f))
        if not a.force and os.path.exists(out_path):
            skipped += 1
            continue
        img = os.path.join(a.pages, name)
        if not os.path.exists(img):
            print("  跳过（缺页面图）：%s" % name)
            skipped += 1
            continue
        d = jsonio.read(f)
        regions = page_regions(engine, img)
        tag_lines(d["lines"], regions)
        d["regions"] = regions
        jsonio.write(out_path, d)
        for R in regions:
            stats[R["cls"]] = stats.get(R["cls"], 0) + 1
        done += 1
        if done % 20 == 0:
            print("  %d/%d" % (done, len(files) - skipped), flush=True)

    print("完成 %d 页，跳过 %d 页" % (done, skipped))
    print("区域统计：" + ", ".join("%s=%d" % kv
                                for kv in sorted(stats.items(), key=lambda kv: -kv[1])))
    drop = sum(v for k, v in stats.items() if k in DROP_REGIONS)
    print("其中会被 layout.py 丢弃的区域行：%d" % drop)


if __name__ == "__main__":
    main()
