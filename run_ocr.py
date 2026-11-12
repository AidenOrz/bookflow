"""全量 OCR：PP-OCRv6（small/medium），多进程，输出每页行级 JSON。"""
import os, sys, json, glob, time
from multiprocessing import Pool

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "pages")
DST = os.path.join(HERE, "ocr")
PROCS = 5
THREADS = 4
DEVICE = os.environ.get("BOOKFLOW_DEVICE", "auto")
MODEL = os.environ.get("BOOKFLOW_MODEL", "small")


def engine_params(device, model="small"):
    """device: cpu / dml / cuda / auto；model: small / medium / server"""
    import onnxruntime as ort
    avail = ort.get_available_providers()
    if device == "auto":
        device = "dml" if "DmlExecutionProvider" in avail else (
            "cuda" if "CUDAExecutionProvider" in avail else "cpu")
    if device == "dml":
        p = {"EngineConfig.onnxruntime.use_dml": True}
    elif device == "cuda":
        p = {"EngineConfig.onnxruntime.use_cuda": True}
    else:
        p = {"EngineConfig.onnxruntime.intra_op_num_threads": THREADS,
             "EngineConfig.onnxruntime.inter_op_num_threads": 1}
    if model and model != "small":
        from rapidocr.utils.typings import OCRVersion, ModelType
        mt = {"medium": ModelType.MEDIUM, "server": ModelType.SERVER}[model]
        p.update({
            "Det.ocr_version": OCRVersion.PPOCRV6, "Det.model_type": mt,
            "Rec.ocr_version": OCRVersion.PPOCRV6, "Rec.model_type": mt,
        })
    return p


def parse_args():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--dst", default=DST)
    ap.add_argument("--procs", type=int, default=PROCS)
    ap.add_argument("--device", default=os.environ.get("BOOKFLOW_DEVICE", "auto"),
                    choices=["auto", "cpu", "dml", "cuda"])
    ap.add_argument("--model", default=os.environ.get("BOOKFLOW_MODEL", "small"),
                    choices=["small", "medium", "server"])
    ap.add_argument("--force", action="store_true",
                    help="重跑已存在的页（换模型/换设备重跑时必须加，否则会被静默跳过）")
    return ap.parse_args()


def work(job):
    path, dst_dir = job
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    import numpy as np
    from PIL import Image
    from rapidocr import RapidOCR

    global _E
    try:
        _E
    except NameError:
        _E = RapidOCR(params=engine_params(DEVICE, MODEL))

    im = Image.open(path).convert("RGB")
    w, h = im.size
    arr = np.array(im)
    r = _E(arr)
    lines = []
    if r is not None and r.txts:
        for box, txt, sc in zip(r.boxes, r.txts, r.scores):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            lines.append({
                "x0": round(float(min(xs)), 1), "y0": round(float(min(ys)), 1),
                "x1": round(float(max(xs)), 1), "y1": round(float(max(ys)), 1),
                "t": txt, "s": round(float(sc), 4),
            })
    lines.sort(key=lambda L: (round(L["y0"] / 10), L["x0"]))
    out = os.path.join(dst_dir, os.path.basename(path) + ".json")
    json.dump({"name": os.path.basename(path), "w": w, "h": h, "lines": lines},
              open(out, "w", encoding="utf-8"), ensure_ascii=False)
    return os.path.basename(path), len(lines)


if __name__ == "__main__":
    _a = parse_args()
    SRC, DST, PROCS, DEVICE, MODEL = _a.src, _a.dst, _a.procs, _a.device, _a.model
    if DEVICE in ("dml", "cuda"):
        PROCS = 2      # 单卡下多进程收益有限，2 个足够压满 GPU
    os.environ["BOOKFLOW_DEVICE"] = DEVICE      # 子进程（spawn）靠环境变量继承
    os.environ["BOOKFLOW_MODEL"] = MODEL
    os.makedirs(DST, exist_ok=True)
    files = sorted(glob.glob(os.path.join(SRC, "*.png")))
    if not files:
        sys.exit("源目录没有 PNG：%s（先跑 render 阶段）" % SRC)
    if _a.force:
        # 换模型重跑时旧结果必须作废，否则 todo 为空、什么都不做却报成功
        for f in files:
            out = os.path.join(DST, os.path.basename(f) + ".json")
            if os.path.exists(out):
                os.remove(out)
    todo = [f for f in files if not os.path.exists(os.path.join(DST, os.path.basename(f) + ".json"))]
    if not todo:
        print("全部 %d 页已存在结果，无需处理（加 --force 可重跑）" % len(files), flush=True)
        sys.exit(0)
    print("总页 %d，待处理 %d，进程 %d，设备 %s，模型 %s"
          % (len(files), len(todo), PROCS, DEVICE, MODEL), flush=True)
    t0 = time.time()
    done = 0
    with Pool(PROCS) as p:
        for name, n in p.imap_unordered(work, [(f, DST) for f in todo], chunksize=2):
            done += 1
            if done % 20 == 0 or done == len(todo):
                el = time.time() - t0
                print("  %d/%d  %.1fs  预计剩余 %.1fs" %
                      (done, len(todo), el, el / done * (len(todo) - done)), flush=True)
    print("完成，用时 %.1f 分钟" % ((time.time() - t0) / 60), flush=True)
