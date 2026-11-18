"""bookflow.manifest — 一次运行的唯一事实源（manifest.json）。

## 为什么 report.json 不够

report.json 是「这本书长什么样」，它由 md 阶段**整体重写**（make_md.py:63），
所以任何「这次是怎么跑出来的」信息放进去都会被下一次 md 冲掉。
实测后果（IO-CONVENTIONS §1 I2）：四份 report.json 的 params 只有一个
{"conservative_fix": false, "src": "ocr_layout"}——dpi / model / device /
text_layer / no_layout **一个都没记**，于是「单页体积差 10.6 倍」「页图尺寸差 21%」
这种问题在产物里找不到任何解释。

manifest.json 只做一件事：**跨阶段累加地记录这次运行的输入身份与参数**。

## 三条硬约束

1. **input.sha256 是唯一的输入身份**。stage_textlayer 现在只比 realpath
   （bookflow.py:654，路径相同 ≠ 内容相同），而且那唯一一份记录还写在被 prune
   删掉的 _textlayer.meta 和几百份页 JSON 里。换了一本同名不同版的书，
   所有下游产物看起来仍然「正常」——这正是本项目最怕的静默出错。
2. **stages[*].status 取 ok | degraded | failed | skipped**。
   没有这条，「目录存在」就被当成了「阶段跑完」（IO-CONVENTIONS §2 O5：
   run-sengguan/ocr_text 只剩一个 meta，与「还没跑」在文件系统上一模一样）。
3. **路径一律相对工作目录根、正斜杠**。禁止绝对路径——I1 那个已经悬空的
   E:/workspace/bookflow/.tmp-lacan.pdf（已悬空，Test-Path 为 False）就是这么来的。

## 与 stages 的关系

manifest 记录「跑过没有、跑成什么样」，**不替代阶段自己的产物**。
它坏掉/缺失时必须让流水线照常跑（_read 里所有异常都吞掉返回空文档），
因为一个元数据文件不该有能力挡住出书。
"""
import datetime
import hashlib
import os

import jsonio

SCHEMA = "bookflow.manifest"
SCHEMA_VERSION = 1
STATUSES = ("ok", "degraded", "failed", "skipped", "running")

# 阶段顺序是**对外契约**：WebUI 的进度条、文档、测试都按这个顺序画。
# 与 bookflow.py 的 argparse choices 不完全相同（那边还含 prune/disagree/table 这类
# 手动阶段），这里只放主干。
STAGES = ("render", "textlayer", "ocr", "repair", "layout", "md",
          "verify", "audit", "epub")


def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def sha256_of(path, chunk=1 << 20):
    """文件内容指纹。读不到返回空串（不能因为算不出哈希就挡住流水线）。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
    except OSError:
        return ""
    return h.hexdigest()


def rel(run_root, p):
    """绝对路径 -> 相对工作目录根的正斜杠路径。跨盘符时退回文件名。"""
    if not p:
        return ""
    try:
        r = os.path.relpath(os.path.abspath(p), os.path.abspath(run_root))
    except ValueError:
        return os.path.basename(p)
    return r.replace("\\", "/")


def _read(path):
    """读 manifest；不存在/坏了都返回一个空骨架，**绝不抛**。"""
    if not os.path.exists(path):
        return _empty()
    try:
        doc = jsonio.read(path) or {}
    except (ValueError, OSError):
        return _empty()
    if not isinstance(doc, dict):
        return _empty()
    base = _empty()
    base.update(doc)
    base.setdefault("input", {})
    base.setdefault("params", {})
    base.setdefault("stages", {})
    return base


def _empty():
    return {"schema": SCHEMA, "schema_version": SCHEMA_VERSION,
            "book_id": "", "input": {}, "params": {}, "stages": {}}


def read(run_root):
    """公开读入口：<run>/manifest.json。"""
    import paths
    return _read(paths.manifest_path(run_root))


def write(run_root, doc):
    import paths
    doc = dict(doc or {})
    doc["schema"] = SCHEMA
    doc["schema_version"] = SCHEMA_VERSION
    p = paths.manifest_path(run_root)
    d = os.path.dirname(os.path.abspath(p))
    os.makedirs(d, exist_ok=True)
    jsonio.write(p, doc, indent=1)
    return p


def update(run_root, **kw):
    """读-改-写一次（跨阶段累加就靠这个）。"""
    doc = read(run_root)
    doc.update(kw)
    write(run_root, doc)
    return doc


def set_input(run_root, pdf="", title="", author="", publisher="", pages=None):
    """记输入身份。pdf 给绝对路径，内部存相对 + sha256。"""
    doc = read(run_root)
    inp = dict(doc.get("input") or {})
    if pdf:
        inp["pdf"] = rel(run_root, pdf)
        inp["sha256"] = sha256_of(pdf)
    for k, v in (("title", title), ("author", author), ("publisher", publisher)):
        if v:
            inp[k] = v
    if pages is not None:
        inp["pages"] = int(pages)
    doc["input"] = inp
    write(run_root, doc)
    return inp


def set_params(run_root, **params):
    """记参数。值为 None 的跳过（表示「这次没给，用默认」）。"""
    doc = read(run_root)
    cur = dict(doc.get("params") or {})
    for k, v in params.items():
        if v is not None:
            cur[k] = v
    doc["params"] = cur
    write(run_root, doc)
    return cur


def set_stage(run_root, stage, status, count=None, out=None, detail="", elapsed=None):
    """记一个阶段的结果。status 非法时就地纠正成 failed 而不是写脏数据。"""
    if status not in STATUSES:
        status, detail = "failed", (detail + " [status 非法: %r]" % (status,)).strip()
    doc = read(run_root)
    stages = dict(doc.get("stages") or {})
    rec = {"status": status, "at": _now()}
    if count is not None:
        rec["count"] = int(count)
    if out:
        rec["out"] = rel(run_root, out)
    if elapsed is not None:
        rec["elapsed"] = round(float(elapsed), 1)
    if detail:
        rec["reason"] = detail
    stages[stage] = rec
    doc["stages"] = stages
    write(run_root, doc)
    return rec


def stage_status(run_root, stage):
    """某阶段的状态；没记录过返回 "missing"（**不是** "ok"）。"""
    rec = (read(run_root).get("stages") or {}).get(stage) or {}
    return rec.get("status") or "missing"


def progress(run_root, stages=None):
    """(done, total, percent, current)。

    done 只数 ok/degraded/skipped（**failed 不算做完**，否则进度条会在
    一个阶段失败时仍然显示 100%，这正是「退出码 0 就当成功」的同类错误）。
    """
    want = stages or STAGES
    st = read(run_root).get("stages") or {}
    done, current = 0, ""
    for s in want:
        v = (st.get(s) or {}).get("status")
        if v in ("ok", "degraded", "skipped"):
            done += 1
        elif v and not current:
            current = s
    total = len(want)
    return done, total, (100.0 * done / total if total else 0.0), current


def verify_input(run_root, pdf):
    """换书检测：返回 (是否同一本, 旧 sha256 前缀, 新 sha256 前缀)。

    stage_textlayer 现在用 realpath 比（bookflow.py:654）——路径相同不代表内容相同。
    这里补上内容口径的比较，调用方负责在「不同」时清空重抽。
    """
    if not pdf or not os.path.isfile(pdf):
        return True, "", ""
    old = (read(run_root).get("input") or {}).get("sha256") or ""
    if not old:
        return True, "", ""
    new = sha256_of(pdf)
    return (old == new), old[:12], new[:12]
