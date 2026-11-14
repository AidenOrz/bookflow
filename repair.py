"""bookflow.repair — 补识别失败的行（OCR 查漏补缺）。

问题现象：一行的检测框完全正确（横跨整行、高度正常），但识别结果只吐出
1~3 个乱字（如「和垒」「器采器使」），导致整行正文丢失。

根因：RapidOCR 整页识别时，识别阶段会把同一批次的多个行裁片按最长者补齐
宽度再送模型。异常长的行会被压扁，模型退化成输出几个无意义字符。
同样的裁片单独送识别则完全正确（实测 6/6 复原）。

对策：按"认出字数 / 应有字数"找出可疑行，把该行单独裁出、单独识别，拼回。

用法：
  python repair.py --src ./ocr --pages ./pages --dst ./ocr_fix
非可疑行原样复制，可反复运行。
"""
import os, json, glob, argparse, statistics
import numpy as np
from PIL import Image
from rapidocr import RapidOCR
import jsonio
import pagedoc

SUSPECT = 0.62        # 认出比例低于此值视为失败行
ACCEPT = 0.70         # 重识别结果需达到应有字数的此比例才采纳

# 页级 JSON 的来由标记。repair 是"在 OCR 结果上做单行重识别"，产出仍属 OCR 家族，
# 所以 producer.name 用 rapidocr-repair 而不是 rapidocr——下游据此知道这一版
# 经过补识别，排查"某页为什么和 ocr/ 不一样"时不必再猜。
PRODUCER_NAME = "rapidocr-repair"


def rapidocr_version():
    """RapidOCR 版本；取不到返回 None（producer.version 允许为 null）。"""
    try:
        import importlib.metadata as md
        return md.version("rapidocr")
    except Exception:
        return None


# 视觉长度必须与 layout.py 同口径：repair 的 SUSPECT 阈值比较的是
# "认出的字 / 应有的字"，而"应有字数"来自 layout 那套长度口径。
# 旧版本地实现把全角标点也算 1.0 字宽（layout 算 0.55），
# 一条「《高僧传》卷五」在两处能差 20%，同一行会在不同阶段被判成不同长度。
from layout import elen  # noqa: E402


def median_char_width(files):
    ws = []
    for f in files:
        d = jsonio.read(f)
        W = d["w"]
        for L in d["lines"]:
            n = elen(L["t"])
            if n >= 10 and L["x1"] - L["x0"] > 0.55 * W:
                ws.append((L["x1"] - L["x0"]) / n)
    return statistics.median(ws)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="ocr")
    ap.add_argument("--pages", default="pages")
    ap.add_argument("--dst", default="ocr_fix")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "dml", "cuda"])
    ap.add_argument("--dry", action="store_true", help="只报告不写文件")
    ap.add_argument("--force", action="store_true",
                    help="兼容 bookflow 的 --force 透传（本脚本总是全量重算，无需额外处理）")
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(a.src, "*.json")))
    if not files:
        raise SystemExit("源目录没有页级 JSON：%s（先跑 ocr 阶段）" % a.src)
    cw = median_char_width(files)
    if not cw:
        raise SystemExit("找不到足够的长行来估计字宽（%s），无法判断可疑行" % a.src)
    print("正文字符宽度 %.1f px ｜ 扫描 %d 页 ｜ 可疑阈值 %.2f" % (cw, len(files), SUSPECT))

    params = {}
    if a.device in ("dml", "auto"):
        import onnxruntime as ort
        if "DmlExecutionProvider" in ort.get_available_providers():
            params = {"EngineConfig.onnxruntime.use_dml": True}
    ocr = RapidOCR(params=params)

    os.makedirs(a.dst, exist_ok=True)
    n_fix = n_page = 0
    for f in files:
        d = jsonio.read(f)
        W, H = d["w"], d["h"]
        page = os.path.basename(f).replace(".json", "")
        img = None
        changed = 0
        for L in d["lines"]:
            w, h = L["x1"] - L["x0"], L["y1"] - L["y0"]
            n = elen(L["t"])
            if n == 0 or h <= 0 or w <= 0.5 * W:
                continue
            expect = w / cw
            if n >= SUSPECT * expect:
                continue
            if img is None:
                img = Image.open(os.path.join(a.pages, page)).convert("RGB")
            crop = img.crop((max(0, int(L["x0"])), max(0, int(L["y0"])),
                             min(W, int(L["x1"])), min(H, int(L["y1"]))))
            r = ocr(np.array(crop))
            new = "".join(r.txts) if r is not None and r.txts else ""
            if elen(new) >= ACCEPT * expect and elen(new) > n * 1.2:
                print("  %-16s y=%5d  认出%2.0f/应有%2.0f | %-12s -> %s"
                      % (page, L["y0"], n, expect, L["t"][:12], new[:44]))
                L["t"] = new
                changed += 1
        if changed:
            n_fix += changed
            n_page += 1
        if not a.dry:
            # 只**新增**字段，其余（name/w/h/lines/region/regions）原样带过去。
            # producer 在这里必须改写：这份数据的内容已经被 repair 改过，
            # 再标 rapidocr 就是谎报来由。上一手的来源记到 upstream_producer，
            # 否则「这页原文是哪个模型认的」这条线索就永久丢了。
            # 已存在 upstream_producer 时以它为准（重复跑 repair 不冲掉源头）。
            prev = d.get("upstream_producer") or d.get("producer")
            extra = {"upstream_producer": prev} if prev else {}
            pagedoc.stamp(d, pagedoc.producer(PRODUCER_NAME, rapidocr_version()),
                          **extra)
            pagedoc.write(os.path.join(a.dst, os.path.basename(f)), d)
    print("修复 %d 行，涉及 %d 页%s" % (n_fix, n_page, "（dry run，未写文件）" if a.dry else ""))


if __name__ == "__main__":
    main()
