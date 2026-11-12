"""把候选错误行从页面图裁出来，拼成一张对照图，便于人工/VLM 批量裁决。"""
import json, glob, os, sys, argparse
import numpy as np
from PIL import Image, ImageDraw
import jsonio

ap = argparse.ArgumentParser()
ap.add_argument("--audit", default="out/audit.json")
ap.add_argument("--ocr", default="ocr_fix")
ap.add_argument("--pages", default="pages")
ap.add_argument("--out", default="out/review.png")
ap.add_argument("--n", type=int, default=10)
ap.add_argument("--from", dest="start", type=int, default=0)
ap.add_argument("--verdict", default="", help="disagree 模式：只取该判定的条目（A/B/?）")
a = ap.parse_args()

hits = jsonio.read(a.audit)
# 建索引：每页每行的文本
idx = []
for f in sorted(glob.glob(os.path.join(a.ocr, "*.json"))):
    d = jsonio.read(f)
    for L in d["lines"]:
        idx.append((os.path.basename(f).replace(".json", ""), L))

if a.verdict:
    hits = [h for h in hits if h.get("verdict") == a.verdict]

picked = hits[a.start:]
tiles, used = [], set()
for h in picked:
    if len(tiles) >= a.n:
        break
    if "A" in h and "page" in h:
        key = (h["page"], int(h["y0"]))
        if key in used:
            continue
        used.add(key)
        img = Image.open(os.path.join(a.pages, h["page"])).convert("RGB")
        crop = img.crop((max(0, int(h["x0"]) - 8), max(0, int(h["y0"]) - 6),
                         min(img.width, int(h["x1"]) + 8), min(img.height, int(h["y1"]) + 6)))
        tiles.append(({"tag": "%s | %s" % (h["A"][:34], h["B"][:34]),
                       "verdict": h.get("verdict", "?")}, h["page"], crop))
        continue
    target = h["wrong"]
    found = None
    for name, L in idx:
        if target in L["t"] and (name, int(L["y0"])) not in used:
            found = (name, L)
            break
    if not found:
        continue
    name, L = found
    used.add((name, int(L["y0"])))
    img = Image.open(os.path.join(a.pages, name)).convert("RGB")
    crop = img.crop((max(0, int(L["x0"]) - 8), max(0, int(L["y0"]) - 6),
                     min(img.width, int(L["x1"]) + 8), min(img.height, int(L["y1"]) + 6)))
    tiles.append((h, name, crop))

W = max((t[2].width for t in tiles if t[2]), default=1200)
rowh = [t[2].height if t[2] else 40 for t in tiles]
pad = 8
has_tag = any("tag" in t[0] for t in tiles)
labelh = 52 if has_tag else 26
H = sum(r + labelh + pad for r in rowh) + pad * 2
canvas = Image.new("RGB", (W + 120, H), "white")
dr = ImageDraw.Draw(canvas)
y = pad
for i, (h, name, crop) in enumerate(tiles):
    if "tag" in h:
        dr.text((10, y + 4), "%d) [%s]" % (i + 1, h["verdict"]), fill="red")
        dr.text((120, y + 4), h["tag"], fill="#333")
        dr.text((10, y + 26), "↓ 原图此行", fill="#999")
    else:
        dr.text((10, y + 4), "%d) %s→%s" % (i + 1, h["wrong"], h["right"]), fill="red")
    if crop:
        canvas.paste(crop, (110, y + (labelh - 20 if has_tag else labelh)))
    else:
        dr.text((110, y + labelh), "[未定位到原图]", fill="blue")
    y += labelh + rowh[i] + pad
canvas.save(a.out)
print("写出 %s（%d 条，%dx%d）" % (a.out, len(tiles), canvas.width, canvas.height))
for i, (h, name, crop) in enumerate(tiles):
    if "tag" in h:
        print("  %2d) [%s] %s" % (i + 1, h["verdict"], h["tag"]))
    else:
        print("  %2d) %-6s→%-6s  %s" % (i + 1, h["wrong"], h["right"], name or "?"))
