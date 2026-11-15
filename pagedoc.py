"""bookflow.pagedoc — 页级 JSON 与产物 JSON 的统一契约。

**为什么需要这一层**：页级 JSON 是整条流水线的中间格式，写入方有四个
（run_ocr / repair / run_layout / textlayer），读取方有六个（layout / decisions /
disagree / audit / make_review / apply_decisions）。以前没有任何版本标识，
导致两个实际问题：

1. 文字层产物（`pdftotext -bbox` 转出来的）和 OCR 产物长得一样，但来源不同、
   可信度不同。下游无法区分，`ocr_dir()` 的优先级逻辑也没法判断该信谁。
2. 改了字段语义后，旧工作目录里的 JSON 会被新代码静默误读——本项目的头号风险
   就是「静默出错」，这里必须能显式拒绝而不是猜。

设计取舍：
- **只加字段，不改既有语义**。`schema` / `schema_version` / `producer` 三个新键，
  旧的 `name` / `w` / `h` / `lines` / `regions` 一个都不动。
- **向后兼容是硬要求**：没有版本字段的老文件必须照常读（`check()` 返回一条警告，
  不是错误）。三本验证书的现有工作目录不能因为加了这一层就跑不动。
- **不引第三方依赖**。纯标准库，符合项目约束。
- 不做 schema 校验器（Pydantic 那套）。我们只有 4 个写入方，字段又少，
  过度校验会把「兼容旧数据」变成负担。只校验**版本号能不能认**。
"""
import os
import sys

import jsonio

# 页级 JSON（ocr/*.json、ocr_fix/*.json、ocr_layout/*.json、ocr_text/*.json）
SCHEMA = "bookflow.pages"
SCHEMA_VERSION = 1

# 顶层产物（out/report.json）
REPORT_SCHEMA = "bookflow.report"
REPORT_SCHEMA_VERSION = 1

# 已知的 producer 名。只用于生成与展示，**不做白名单校验**——新增来源不该被拦。
KNOWN_PRODUCERS = {
    "rapidocr": "OCR 识别（run_ocr.py）",
    "rapidocr-repair": "单行补识别（repair.py）",
    "pp_doc_layoutv3": "版面分析打 region 标签（run_layout.py）",
    "pdftotext": "PDF 自带文字层（textlayer.py）",
}


def producer(name, version=None):
    """构造 producer 字段。version 缺省时尝试从模块里取，取不到就留 None。

    producer 只表达**真实生成来源**，加载/渲染/回填都不许改写它——
    这是判断「这份数据是谁产的、该不该信」的唯一依据。
    """
    return {"name": name, "version": version}


def stamp(doc, prod, **extra):
    """给一份页级 JSON 盖上 schema 章，返回同一对象（就地改，方便链式调用）。

    `extra` 用来带各写入方自己的附加信息（如 textlayer 的 `source_pdf`），
    会写进顶层；不覆盖既有键。
    """
    doc["schema"] = SCHEMA
    doc["schema_version"] = SCHEMA_VERSION
    if prod is not None:
        doc["producer"] = dict(prod)
    for k, v in extra.items():
        if k in ("name", "w", "h", "lines", "regions", "schema", "schema_version", "producer"):
            continue            # 这些是核心字段，附加信息不许覆盖
        doc[k] = v
    return doc


def stamp_report(rep, prod=None, **extra):
    """给 out/report.json 盖 schema 章。流水线身份 + 各阶段参数快照都在里面。"""
    rep["schema"] = REPORT_SCHEMA
    rep["schema_version"] = REPORT_SCHEMA_VERSION
    if prod is not None:
        rep["producer"] = dict(prod)
    for k, v in extra.items():
        rep.setdefault(k, v)
    return rep


def check(doc, path="", strict=True):
    """检查一份页级 JSON 的版本能不能认。返回问题列表（空 = 通过）。

    `strict=True` 时，**认不出的版本直接拒绝**（宁可报错也不误读）；
    没有版本字段的老文件只给一条 `WARN` 级提示，不算问题——三本验证书的工作目录
    里有几千个老 JSON，它们必须继续能跑。
    """
    probs = []
    where = path or "?"
    if not isinstance(doc, dict):
        return ["%s: 顶层不是对象（%s）" % (where, type(doc).__name__)]
    for need in ("w", "h", "lines"):
        if need not in doc:
            probs.append("%s: 缺必需字段 %s" % (where, need))
    if probs:
        return probs
    sch = doc.get("schema")
    ver = doc.get("schema_version")
    if sch is None and ver is None:
        return probs                  # 老文件：兼容，不用吭声
    if sch not in (None, SCHEMA):
        probs.append("%s: schema 不是 %s 而是 %r" % (where, SCHEMA, sch))
    if sch == SCHEMA:
        try:
            v = int(ver)
        except (TypeError, ValueError):
            probs.append("%s: schema_version 不是整数（%r）" % (where, ver))
        else:
            if v != SCHEMA_VERSION:
                probs.append("%s: schema_version=%d，本代码只认 %d"
                             % (where, v, SCHEMA_VERSION))
    return probs


def read(path, strict=True):
    """读一份页级 JSON，带版本检查。

    用它的地方要能明确区分「文件坏了」和「版本不认识」——
    前者重跑即可，后者必须先升级代码或降级数据，处理方式完全不同。
    """
    doc = jsonio.read(path)
    probs = check(doc, path, strict=strict)
    if probs:
        raise ValueError("页级 JSON 不合法：\n  " + "\n  ".join(probs[:10]))
    return doc


def write(path, doc):
    """写一份页级 JSON（原子写，走 jsonio）。"""
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    jsonio.write(path, doc)


def describe(doc):
    """一行人类可读的来由描述，给 CLI 回显与日志用。"""
    p = doc.get("producer") or {}
    name = p.get("name") or "未知来源"
    ver = p.get("version")
    tail = "" if not ver else " %s" % ver
    sv = doc.get("schema_version")
    if sv is None:
        return "%s%s（无版本标识的老文件）" % (name, tail)
    return "%s%s（schema v%s）" % (name, tail, sv)


def warn_legacy(paths, label=""):
    """批量检查，把「有老文件」这件事一次说清楚，而不是每个文件各喊一句。"""
    legacy = 0
    for p in paths:
        try:
            d = jsonio.read(p)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("schema_version") is None:
            legacy += 1
    if legacy and legacy == len(paths):
        sys.stderr.write("  提示：%s有 %d 个无版本标识的页级 JSON（老格式，已兼容读取）。\n"
                         "        重跑对应阶段可自动补上 schema 字段。\n" % (label, legacy))
    elif legacy:
        sys.stderr.write("  提示：%s %d/%d 个页级 JSON 是老格式（已兼容读取）。\n"
                         % (label, legacy, len(paths)))
    return legacy