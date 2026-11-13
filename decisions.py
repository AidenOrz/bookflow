"""decisions — 裁决表（out/decisions.json）的统一 schema 与适配层。

## 为什么需要这一层（实测依据，不是推测）

`out/review_queue.json` **不是任何脚本生成的**：实测它是 `out/disagree.json` 的
人工筛选子集（20/196 条，字段完全一致：page,y0,y1,x0,x1,A,B,verdict,why,sA,sB，
page 带 .png 后缀）。旧版 apply_decisions.py 把 20 条裁决硬编码在源码里、用
`queue[idx-1]` 寻址——重跑一次 disagree.py 队列顺序一变，整张裁决表就整体错位。

这里把三路来源统一成同一份 decisions.json：

    {"version": 1,
     "source": {"a": "ocr_fix", "b": "ocr_alt", "queue_from": "disagree"},
     "match": {"y_tol": 1.0, "require_single_segment": false},
     "decisions": [
       {"key": "p-015.png@2109.6", "page": "p-015.png", "y0": 2109.6,
        "A": "整行原文", "B": "整行新版",
        "verdict": "A|B|C|?", "replacement": null,
        "level": "L1|L3", "note": "说明",
        "status": "pending|applied|missing"}]}

  * `key = page + "@" + y0` 是**稳定主键**，取代列表下标；
  * `verdict=C` 时用 `replacement` **整行覆盖**；
  * `status` 由 apply_decisions.py 回写（applied/missing），让"跑过一遍"可观测、可幂等；
  * `match.require_single_segment` 只是兼容开关：true 退回旧版"只替换单一差异段"的行为，
    默认 false（整行覆盖）——旧版正是在这里静默丢掉了多差异段的那几条裁决。

本模块是**纯函数 + 读写**，不做任何裁决、不碰 OCR 目录里的文本，只做格式归一。
bookflow.py 的 decisions 阶段直接 import 它。
"""
import glob
import os
import re

import jsonio

SCHEMA_VERSION = 1
VERDICTS = ("A", "B", "C", "?")
LEVELS = ("L1", "L3")
STATUSES = ("pending", "applied", "missing")

# 默认来源标记：a/b 是两版 OCR 的目录名（disagree.py 的 --a/--b 默认值），
# queue_from 说明这份裁决表最初从哪路候选转来的（disagree / audit / queue）。
DEFAULT_SOURCE = {"a": "ocr_fix", "b": "ocr_alt", "queue_from": "disagree"}

# y_tol：disagree 记下的 y0 来自 A 侧目录（默认 ocr_fix），回填的目录（默认 ocr_merged）
# 里同一行的 y0 可能有亚像素级尾差，1.0 足够宽又不会跨到相邻行（正文字号的行距 ~80）。
DEFAULT_MATCH = {"y_tol": 1.0, "require_single_segment": False}


# ---------------------------------------------------------------------------
# 主键
# ---------------------------------------------------------------------------

def fmt_y(y0):
    """把 y0 归一成 key 里用的字符串：2109.6 -> "2109.6"，2109.0 -> "2109"。

    必须归一，否则 2109.6 与 2109.6000000000004（JSON 往返一次就可能产生这种
    尾差）会生成两个不同的 key，status 回写和合并去重都会失效。
    """
    if y0 is None:
        return "?"
    s = "%.3f" % float(y0)
    s = s.rstrip("0").rstrip(".")
    return s or "0"


def key_of(page, y0, tag=""):
    """稳定主键：page@y0（可选 #tag，用于 y0 未知的未定位条目）。

    tag 存在的理由：audit 的 hit 可能因为页面里找不到那一行而没有 y0，
    这类条目仍要保留（不能丢证据），但同一页可能有多条，必须有区分。
    """
    base = "%s@%s" % (page or "-", fmt_y(y0))
    return base + ("#" + str(tag) if tag != "" else "")


# ---------------------------------------------------------------------------
# 条目构造
# ---------------------------------------------------------------------------

def make_decision(page, y0, A="", B="", verdict="?", replacement=None,
                  level="L1", note="", status="pending", tag=""):
    """造一条裁决。字段顺序与 schema 一致，缺省值都补齐（apply 端不必再判 None）。"""
    page = page_name(page)
    return {
        "key": key_of(page, y0, tag),
        "page": page,
        "y0": y0,
        "A": A,
        "B": B,
        "verdict": verdict,
        "replacement": replacement,
        "level": level,
        "note": note,
        "status": status,
    }


def _norm_entry(e):
    """把一条外部来的裁决补齐成 schema 形状。"""
    d = dict(e or {})
    d["page"] = page_name(d.get("page"))     # 页码统一成页名，key 才跨来源一致
    d["key"] = d.get("key") or key_of(d["page"], d.get("y0"))
    d["y0"] = d.get("y0")
    d["A"] = d.get("A") or ""
    d["B"] = d.get("B") or ""
    d["verdict"] = d.get("verdict") if d.get("verdict") in VERDICTS else "?"
    d["replacement"] = d.get("replacement")
    d["level"] = d.get("level") if d.get("level") in LEVELS else "L1"
    d["note"] = d.get("note") or ""
    d["status"] = d.get("status") if d.get("status") in STATUSES else "pending"
    return d


def _doc(entries, queue_from="disagree", match=None, source=None):
    """组一份 decisions.json 文档。"""
    src = dict(DEFAULT_SOURCE)
    if source:
        src.update(source)
    src["queue_from"] = queue_from
    m = dict(DEFAULT_MATCH)
    if match:
        m.update(match)
    return {
        "version": SCHEMA_VERSION,
        "source": src,
        "match": m,
        "decisions": [_norm_entry(e) for e in entries],
    }


# ---------------------------------------------------------------------------
# 三路来源 -> decisions
# ---------------------------------------------------------------------------

def from_disagree(disagree_list):
    """disagree.json（196 条两版分歧）-> decisions。

    verdict A/B 是词表已经判出胜负的，直接可用（A = 原样保留，B = 采用第二版）；
    `?` = 词表判不出、要看图，转成 verdict=? 待裁决。
    """
    out = []
    for c in disagree_list or []:
        v = c.get("verdict") if c.get("verdict") in VERDICTS else "?"
        why = c.get("why") or ""
        if v == "A":
            note = "词表判 A 对（原样保留，仅登记）：" + why
        elif v == "B":
            note = "词表判 B 对（采用第二版）：" + why
        else:
            note = "待看图裁决：" + why
        out.append(make_decision(c.get("page"), c.get("y0"),
                                 A=c.get("A") or "", B=c.get("B") or "",
                                 verdict=v, replacement=None,
                                 level="L1", note=note))
    return _doc(out, queue_from="disagree")


def from_queue(queue_list):
    """旧 review_queue.json -> decisions（字段与 disagree 完全一致，只是来源标记不同）。

    这份队列是人手工从 disagree.json 里筛出来的子集，没有任何脚本生成它，
    所以既不能靠下标寻址，也不能指望它可复现——转成 page@y0 主键才稳。
    """
    d = from_disagree(queue_list)
    d["source"]["queue_from"] = "queue"
    return d


def page_name(page):
    """把页码归一成页名：53 -> "p-053.png"。

    统一成页名有两个理由：① decisions.json 的 key 是 page@y0，disagree 那一路的 page
    本来就带 .png 后缀，audit 那一路是纯数字，不归一则同一行在两份裁决表里 key 不同、
    merge 去重失效；② apply_decisions 遍历 OCR 目录时拿到的是文件名，比对不必再转换。
    已经是页名（或非数字）的原样返回。
    """
    if page is None:
        return None
    if isinstance(page, int) or (isinstance(page, str) and page.isdigit()):
        return "p-%03d.png" % int(page)
    return page


def page_path(ocr_dir, page):
    """页名/页码 -> OCR 行级 JSON 路径。兼容 p-015.png.json / p-15.png.json / p-00015.png.json。

    公开函数：`apply_decisions.py` 回填时必须用同一套补零容错——它原先直接拼
    `page + ".json"`，遇到 4 位补零或没补零的页文件一律判 missing，而裁决表本身
    是靠这里的兼容逻辑生成的，两边口径不一致就会「能生成、不能回填」。
    """
    if page is None or not ocr_dir:
        return None
    pn = page_name(page)
    m = re.search(r"(\d+)", os.path.basename(pn or ""))
    nums = []
    if m:
        nums = ["%03d" % int(m.group(1)), "%d" % int(m.group(1))]
    for n in nums:
        p = os.path.join(ocr_dir, "p-%s.png.json" % n)
        if os.path.exists(p):
            return p
    if nums:
        for p in sorted(glob.glob(os.path.join(ocr_dir, "p-*.png.json"))):
            mm = re.search(r"p-0*(\d+)\.png\.json$", os.path.basename(p))
            if mm and int(mm.group(1)) == int(nums[0]):
                return p
    return None


# 旧名兼容：外部脚本/旧笔记里可能仍写 _page_path
_page_path = page_path


def _lines_with(ocr_dir, page, wrong):
    """在某一页里找所有含 wrong 的行。返回 (页文件路径, [行])。"""
    path = page_path(ocr_dir, page)
    if not path or not wrong:
        return path, []
    doc = jsonio.read(path)
    return path, [L for L in (doc.get("lines") or []) if wrong in (L.get("t") or "")]


def from_audit(audit_list, report_json_path=None, ocr_dir=""):
    """audit.json（词表可疑字）-> decisions。audit 只出清单、不自动改，这里补上回路。

    反查链：hit.page（可由 report.json 的 layout.page_map 用 pos 换算）→
    p-<page>.png.json → 在 lines 里找含 wrong 的行 → 该行 y0 + 整行 t = A，
    t.replace(wrong, right, 1) = B。

    定位不到的 hit **不能丢**：它们同样是"这里可疑"的证据，只是暂时没法机械回填，
    于是保留成 verdict=? + level=L3，note 写明为什么定位不到（缺页码映射 / 缺页文件 /
    页里没这行），交人去翻原书。

    注意 verdict 定成 B 只表示"采用把 wrong 换成 right 的那一整行"，`note` 里写明
    这是词表的建议、需看图确认——audit 的实测精度并不高（根目录那份 audit.txt 的
    2 条命中都是误报），所以 apply_decisions 默认干跑、必须显式 --apply 才会落盘。
    """
    try:
        import audit as _audit
        page_map = _audit.load_page_map(report_json_path) if report_json_path else None
    except Exception:      # audit.py 不在 / jieba 缺失：页码链路降级，不整体失败
        _audit, page_map = None, None

    out = []
    # 多条 hit 可能落到同一行：audit 按"字窗口"出候选，同一行里的同一处会被 2/3-gram
    # 命中多次（实测 run-guixun 的 p-185 有 3 条 hit 全指着同一行）。按 key 归并、
    # 逐个替换叠加，才能既保证 key 唯一、又不丢任何一处替换建议。
    grouped = {}          # key -> {"page","y0","A","B","note":[...],"freq":[...]}
    for i, h in enumerate(audit_list or []):
        wrong = h.get("wrong") or ""
        right = h.get("right") or ""
        pos = h.get("pos")
        page = h.get("page")
        # page 可选：老版 audit 没有 page_map 时只有 pos，靠二分换算成页码
        if page is None and pos is not None and page_map and _audit is not None:
            page = _audit.page_of(page_map, int(pos))
        path, hits = _lines_with(ocr_dir, page, wrong) if page is not None else (None, [])

        if not hits:
            if page is None:
                why = "缺页码映射（report.json 的 layout.page_map），无法定位到页"
            elif not path:
                why = "找不到页文件 p-%s.png.json" % (("%03d" % page) if page is not None else "?")
            else:
                why = "%s 里没有含「%s」的行" % (os.path.basename(path), wrong)
            out.append(make_decision(
                page, None, A="", B="", verdict="?", replacement=None,
                level="L3",
                note="未定位行：" + why + "；建议人工翻原书核对「%s」→「%s」" % (wrong, right),
                tag=pos if pos is not None else i))
            continue

        L = hits[0]
        t = L.get("t") or ""
        page = page_name(page)
        k = key_of(page, L.get("y0"))
        g = grouped.get(k)
        if g is None:
            g = grouped[k] = {"page": page, "y0": L.get("y0"), "A": t, "B": t,
                              "pairs": [], "freq": []}
        if wrong in g["B"]:            # 同一处被重复命中，只叠一次替换
            g["B"] = g["B"].replace(wrong, right, 1)
            g["pairs"].append("%s→%s" % (wrong, right))
            g["freq"].append(h.get("freq"))
        if len(hits) > 1:
            g.setdefault("multi", len(hits))

    for k, g in grouped.items():
        pairs = "、".join(g["pairs"])
        note = "audit 词表建议 %s；需看图确认" % pairs
        if g.get("multi"):
            note += "；本页有 %d 行含该词，取首行" % g["multi"]
        out.append(make_decision(
            g["page"], g["y0"], A=g["A"], B=g["B"],
            verdict="B", replacement=None, level="L1", note=note))
    return _doc(out, queue_from="audit")


# ---------------------------------------------------------------------------
# 合并 / 读写
# ---------------------------------------------------------------------------

def guess_kind(items):
    """猜一份候选列表是哪一路来源：audit 的 hit 有 wrong/right，disagree 有 A/B + why。"""
    for it in items or []:
        if isinstance(it, dict) and "wrong" in it and "right" in it:
            return "audit"
    for it in items or []:
        if isinstance(it, dict) and ("why" in it or "sA" in it or ("A" in it and "B" in it)):
            return "disagree"
    return "queue"


def load_any(path, kind=None, report=None, ocr_dir="", ocr=""):
    """按文件内容自动分派到 from_disagree / from_audit / from_queue。

    `ocr` 是 `ocr_dir` 的别名——bookflow.py 的 decisions 阶段用 dec.load_any(p, kind=…,
    report=…, ocr=…) 调用，参数名对齐调用方。
    """
    data = jsonio.read(path)
    if isinstance(data, dict) and "decisions" in data:
        return normalize(data)
    kind = kind or guess_kind(data)
    if kind == "audit":
        rp = report or os.path.join(os.path.dirname(os.path.abspath(path)), "report.json")
        return from_audit(data, rp, ocr or ocr_dir)
    if kind == "disagree":
        return from_disagree(data)
    return from_queue(data)


def merge(*objs):
    """按 key 合并多份裁决表，**后者覆盖前者**（同一条被重新裁决时以新为准）。

    每个参数可以是 decisions.json 的整份 dict，也可以直接是 decisions 列表——
    bookflow 的 decisions 阶段就是 `dec.merge(dec.read(dst), picked)` 这种形态。
    """
    merged = {}          # dict 保序：键首次出现的位置不变，值被后者整条替换
    source = dict(DEFAULT_SOURCE)
    match = dict(DEFAULT_MATCH)
    for o in objs:
        if o is None:
            continue
        if isinstance(o, dict):
            entries = o.get("decisions") or []
            source.update(o.get("source") or {})
            match.update(o.get("match") or {})
        else:
            entries = o
        for e in entries:
            e = _norm_entry(e)
            merged[e["key"]] = e
    return {
        "version": SCHEMA_VERSION,
        "source": source,
        "match": match,
        "decisions": list(merged.values()),
    }


def normalize(data):
    """补齐 schema 缺省字段。列表输入按 from_queue 处理（兼容直接喂队列文件）。"""
    if isinstance(data, list):
        return from_queue(data)
    out = dict(data or {})
    try:
        out["version"] = int(out.get("version") or SCHEMA_VERSION)
    except (TypeError, ValueError):
        out["version"] = SCHEMA_VERSION
    src = dict(DEFAULT_SOURCE)
    src.update(out.get("source") or {})
    out["source"] = src
    m = dict(DEFAULT_MATCH)
    m.update(out.get("match") or {})
    out["match"] = m
    out["decisions"] = [_norm_entry(e) for e in (out.get("decisions") or [])]
    return out


def validate(data):
    """检查 schema 基本约束，返回问题列表（空列表 = 通过）。

    apply_decisions 拿它当"宁可报错也不误改"的闸门：裁决表写错一个字段，
    与其静默跳过一条裁决（旧版正是静默丢了 #3/#18），不如直接停下让人看。
    """
    probs = []
    if not isinstance(data, dict):
        return ["顶层必须是对象"]
    if not isinstance(data.get("decisions"), list):
        return ["缺少 decisions 列表"]
    if not isinstance(data.get("match"), dict):
        probs.append("缺少 match 块")
    seen = set()
    for i, e in enumerate(data["decisions"]):
        where = "decisions[%d]" % i
        if not isinstance(e, dict):
            probs.append(where + " 不是对象")
            continue
        k = e.get("key")
        if not k:
            probs.append(where + " 缺 key")
        elif k in seen:
            probs.append(where + " key 重复：%s" % k)
        else:
            seen.add(k)
        if e.get("verdict") not in VERDICTS:
            probs.append(where + " verdict 非法：%r" % (e.get("verdict"),))
        if e.get("status") not in STATUSES:
            probs.append(where + " status 非法：%r" % (e.get("status"),))
        if e.get("level") not in LEVELS:
            probs.append(where + " level 非法：%r" % (e.get("level"),))
        if e.get("verdict") == "C" and not e.get("replacement"):
            probs.append(where + " verdict=C 但缺 replacement")
        if e.get("verdict") in ("A", "B", "?") and e.get("replacement") is not None:
            probs.append(where + " verdict=%s 时 replacement 必须为 null" % e.get("verdict"))
    return probs


def read(path):
    """读 decisions.json（统一走 jsonio）。文件里直接是队列列表时按旧队列格式转换。"""
    data = jsonio.read(path)
    if isinstance(data, list):
        return load_any_list_as_queue(data)
    return normalize(data)


def load_any_list_as_queue(items):
    """给 read() 兜底用：列表文件按内容猜来源（旧文件可能直接是 disagree/queue）。"""
    kind = guess_kind(items)
    if kind == "disagree":
        return from_disagree(items)
    if kind == "audit":
        return from_audit(items, None, "")
    return from_queue(items)


def write(path, data):
    """写 decisions.json（统一走 jsonio：先落 .tmp 再 rename，中途崩溃不留半个文件）。"""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    jsonio.write(path, normalize(data), indent=1)


def counts(doc):
    """按 verdict 统计条目数（给命令行回显/测试用）。"""
    out = {}
    for e in doc.get("decisions") or []:
        out[e["verdict"]] = out.get(e["verdict"], 0) + 1
    return out
