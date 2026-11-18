"""bookflow.make_table — 把裁决表投影成「人只需填一列」的 Markdown 表格，并能回读。

## 为什么需要它（实测，不是推演）

`decisions.json` 是给机器读的协议，不是给人编辑的界面。手改它的代价：

* 大：`run-lacan` 有 2825 条，其中 1325 条含**多个差异段**；
* 脆：`verdict=B` 要求 `B` 是**整行**（不是差异片段），写少一个字就整行覆盖错；
* 狠：`key` 重复、`verdict=C` 缺 `replacement`、`status` 写错，
  `decisions.validate()` 会直接拒绝**整张表**（宁可报错也不误改）。

而人真正要做的判断只有一句：「这一行该用哪个」。所以这里把人要碰的面积
压到两列（`判定` / `替换整行`），其余列都是只读投影。

## 三个设计约束（都由数据决定）

1. **回读只认 (键, 判定, 替换整行)**。原文永远从 `decisions.json` 取，
   所以表格里的上下文可以随便截断、加【】高亮、重排版，永远不会污染数据。
   这也是为什么表格里可以放心把长行截到 64 字——截断只影响阅读，不影响回读。
2. **按差异段分组**。实测 `run-lacan` 需人看字的 999 条压成 625 组，
   前 12 组就覆盖三分之一；组头的「整组判定」一次判定一组。
   纯标点/空白的差异（实测占 64.7%）本质是规则题，但同样分组，
   几十个组头就能带过上千条。
3. **`|` 必须转义**。实测 `run-lacan/out/decisions.json` 有 1 条行文本含 `|`，
   不转义会把 Markdown 表格切错列，回读直接报「列数不对」。

## 用法

    python make_table.py --decisions out/decisions.json --out out/decisions.md
    # ... 人工编辑 out/decisions.md 的「判定」列 ...
    python make_table.py --import out/decisions.md --decisions out/decisions.json

`bookflow.py table` 是它的编排入口（含 `--import`）。
"""
import argparse
import json
import os
import re
import sys
from collections import OrderedDict
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import jsonio                     # noqa: E402
import stdio                      # noqa: E402

CJK = re.compile("[\u4e00-\u9fff]")
VERDICTS = ("A", "B", "C", "?")
EMPTY = "\u2205"                 # ∅：差异段一侧为空时占位，避免表格里出现空白单元格
WIDE = 64                         # 上下文列的最大字数（超出截断，不影响回读）

# 表头与列序在这个模块里只有一处定义：render 与 parse 共用，改一处即可。
COLUMNS = ("键", "页", "上下文（【】标出差异）", "建议", "行 y0", "判定", "替换整行（仅 C 用）")
COL_KEY, COL_Y0, COL_VERDICT, COL_REPL = 0, 4, 5, 6


# ---------------------------------------------------------------------------
# 差异段与分类
# ---------------------------------------------------------------------------

def segs(A, B):
    """A→B 的差异段列表 [(原, 新)]。空串表示该侧没有内容（纯增/纯删）。"""
    sm = SequenceMatcher(None, A or "", B or "", autojunk=False)
    return [(A[i1:i2], B[j1:j2])
            for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def classify(e):
    """条目分类：unlocated / eyes / noise / same。

    * `unlocated`：没有 A/B（或没有 y0）——**定位不到的条目**。
      实测 `run-guixun` 19 条里有 3 条是这种（跨行词：p-272「介|人」、p-287「人|狱」）。
      它们恰恰最需要人，所以必须**单独成组、排在表的最前面**；
      如果按"没有差异就不渲染"把它们过滤掉，人永远看不到它们
      （这正是本项目最怕的那种静默丢失）。
    * `eyes`：差异含汉字，必须看原图。
    * `noise`：纯标点/空白差异，多数能按规则批量定。
      实测 `run-lacan` 里 noise 占 64.7%、eyes 只占 35%——把 noise 塞进主表，
      人要在两千多行里找那 999 行，所以主表只放 eyes，noise 挪到附表。
    * `same`：A 与 B 完全一致，没有可裁决的内容（apply 时是 no-op）。
    """
    if not (e.get("A") or e.get("B")):
        return "unlocated"
    if e.get("y0") is None:
        return "unlocated"
    ss = segs(e.get("A"), e.get("B"))
    if not ss:
        return "same"
    if any(CJK.search(a) or CJK.search(b) for a, b in ss):
        return "eyes"
    return "noise"


def group_of(e):
    """分组键：优先用 note 里记的**词对**，没有才退回差异段。

    为什么优先 note：跨行条目（p-287）的 A/B 差异段只有一个「人→入」，
    但语义是「人狱→入狱」；按差异段分组会把它和「介人→介入」混成一组，
    组头写一个判定就同时改了两件事——而它们的对错理由并不相同。
    """
    s = _suggestion_of(e)
    if s:
        return s
    ss = segs(e.get("A"), e.get("B"))
    return ",".join("%s→%s" % (a or EMPTY, b or EMPTY) for a, b in ss) or "（无差异）"


# ---------------------------------------------------------------------------
# Markdown 单元格
# ---------------------------------------------------------------------------

def esc(s):
    """只转义 Markdown 表格里真有歧义的字符。

    实测全量数据里只有 `|` 出现（1 处），反斜杠和换行都是 0；
    仍然一并处理，因为下一次 OCR 换本书就可能出现。
    """
    return (s or "").replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", " ")


def _split_row(line):
    """按未转义的 `|` 切单元格（`\\|` 还原成字面 `|`）。"""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    cells, cur, i = [], [], 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body) and body[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if c == "|":
            cells.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    cells.append("".join(cur))
    return [c.strip() for c in cells]


def _unbacktick(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == "`" and s[-1] == "`":
        return s[1:-1]
    return s


def highlight(A, B, maxlen=WIDE):
    """把 A 里的差异段用【】括起来。只影响阅读，回读不依赖这一列。

    必须用 opcodes 给出的**精确下标**，不能用 A.find(差异段)——
    单字差异在同一行里重复出现时（「…这个犯人在…刚刚人」里的两个「人」），
    find() 会指到第一个，把标记打在正确位置之前的字上。
    """
    A, B = A or "", B or ""
    sm = SequenceMatcher(None, A, B, autojunk=False)
    out, last = [], 0
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        out.append(A[last:i1])
        out.append("【%s】" % (A[i1:i2] if i1 != i2 else EMPTY))
        last = i2
    out.append(A[last:])
    s = "".join(out)
    if len(s) > maxlen:
        s = s[: maxlen - 8] + "……" + s[-6:]
    return esc(s)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _short(s, n):
    s = (s or "").replace("\r", "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 6] + "……"


def _suggestion_of(e):
    """从 note 里抠出「X→Y」建议，给定位不到的条目一个可读的「建议」列。

    note 的写法来自 decisions.from_audit：'未定位行：…；建议人工翻原书核对「介人」→「介入」'。
    抠不到就留空——宁可空着，也不编一个看起来像建议的东西。
    """
    # 必须限制单侧长度，并且用 [^」] 而不是 .+?：
    # note 里前后有两个「」，用的又是全角」→「，.+? 会从第一个「 一路贪到第二个 」
    # （实测把 "里没有含「自白」的行；建议…核对「自白」→「白白」" 整个吃进第一组）。
    m = re.search("「([^」]{1,8})」→「([^」]{1,8})」", e.get("note") or "")
    return ("%s→%s" % (m.group(1), m.group(2))) if m else ""


HEADER = [
    "# 裁决表（人填表，机器回读）",
    "",
    "> 改完**不要手工改 JSON**——用 `python bookflow.py table --work <work> --import` 合回去。",
    ">",
    "> - **只有「行 y0」「判定」「替换整行」三列会被读取**，其余列是投影，改了不影响数据。",
    "> - 判定取值：`A` 保留原文 / `B` 采用建议 / `C` 用「替换整行」整行覆盖 / `?` 待定 / **留空 = 未裁决**。",
    "> - 组头的「整组判定」覆盖组内**判定为空**的条目；某条自己填了判定，以该条为准。",
    "> - `C` 必须同时填「替换整行」（整行文本，不是片段）；`A/B/?` 必须留空。",
    "",
]


def row_of(e, ctx=None, sug=None):
    """一行表格。三处渲染（unlocated / eyes / noise）共用这一个出口。

    列序必须与 COLUMNS 严格一致——历史上这里复制过三份，加列时漏改一处
    会让 parse 报「列数不对」，所以收敛成一个函数。
    """
    if sug is None:
        # 与 group_of 同口径：note 里记了词对就用它（跨行条目的差异段只是半个词）
        sug = _suggestion_of(e)
        if not sug:
            ss = segs(e.get("A"), e.get("B"))
            sug = "；".join("%s→%s" % (a or EMPTY, b or EMPTY) for a, b in ss)
    y0 = e.get("y0")
    return "| `%s` | %s | %s | %s | %s |  |  |" % (
        e.get("key") or "?",
        esc((e.get("page") or "").replace(".png", "")),
        ctx if ctx is not None else highlight(e.get("A"), e.get("B")),
        esc(sug[:60]),
        "" if y0 is None else ("%.1f" % float(y0)).rstrip("0").rstrip("."),
    )


def _head(L):
    """表头 + 分隔行。列数永远跟 COLUMNS 走。"""
    L.append("| " + " | ".join(COLUMNS) + " |")
    L.append("|" + "---|" * len(COLUMNS))


def render(doc, title="", source="", only="eyes"):
    """decisions 文档 -> Markdown 表格。

    `only`：eyes=只渲染需人看字的（默认）/ noise=只渲染纯标点空白 / all=两者都渲染。
    """
    ds = doc.get("decisions") or []
    buckets = OrderedDict()
    for e in ds:
        kind = classify(e)
        if kind == "same":
            continue
        if only != "all" and kind != only and kind != "unlocated":
            continue
        g = _suggestion_of(e) if kind == "unlocated" else group_of(e)
        buckets.setdefault((kind, g or "（无建议）"), []).append(e)

    # 组按条目数降序：人先做覆盖面最大的组（实测前 12 组覆盖三分之一）
    order = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))
    # unlocated 永远排最前、且不受 --only 过滤：它们是定位不到、最需要人的条目
    unlocated = [(k, v) for k, v in order if k[0] == "unlocated"]
    eyes = [(k, v) for k, v in order if k[0] == "eyes"]
    noise = [(k, v) for k, v in order if k[0] == "noise"]

    L = list(HEADER)
    if title:
        # 只替换标题行本身，不 insert/pop——insert 会挤掉 HEADER 里的空行
        L[0] = "# 裁决表 — " + title
    if source:
        L.append("> 来源：`%s`" % source)
        L.append("")

    def section(items, heading, note=""):
        """一个分节：标题 + 可选的说明块 + 若干组（每组一个「整组判定」+ 表）。"""
        L.append("## %s：%d 条 / %d 组" % (heading, sum(len(v) for _, v in items), len(items)))
        L.append("")
        if note:
            for ln in note.split("\n"):
                L.append("> " + ln if ln else ">")
            L.append("")
        for (kind, g), rows in items:
            L.append("### %s · %d 条"
                     % ((g if kind == "unlocated" else "组 " + g), len(rows)))
            L.append("")
            L.append("整组判定：")
            L.append("")
            _head(L)
            for e in rows:
                if kind == "unlocated":
                    # 定位不到的条目没有 A/B，「上下文」列改成放「为什么定位不到」，
                    # 这一列本来就不参与回读，可以承载不同含义。
                    L.append(row_of(e, ctx=_short(e.get("note") or "（无说明）", 60),
                                    sug=_suggestion_of(e)))
                else:
                    L.append(row_of(e))
            L.append("")

    if unlocated:
        section(unlocated, "⚠ 定位不到（必须人工补，否则回填时会被跳过）",
                "这些条目**没有 A/B**（audit 在页里找不到这个词），没法机械回填：\n"
                "跨行词（词被行长切断，如「介」在行尾、「人」在下一行开头），\n"
                "或者 OCR 把字认错后连原词都不在了。\n"
                "\n"
                "补法（IO-CONVENTIONS.md §9.4）：翻原书 → 找到**含错字的那一行** →\n"
                "把「替换整行」填成改好的**完整一行**，判定填 `C`，**并且必须把\n"
                "「行 y0」填成那一行的真实 y0**（照抄原图里同一行左侧的坐标数，\n"
                "或从页级 JSON 的 lines[].y0 里取）。\n"
                "\n"
                "为什么不能留空：回填要先按 y0 在页里**找到那一行**才能整行覆盖，\n"
                "没有坐标就只能判 missing —— 导入看着成功，正文一个字都不会改。\n"
                "判不动就留空——条目会被原样保留，不会消失（但填了 C 就必须有 y0，\n"
                "否则导入会整份拒绝并报错）。")
    if eyes:
        section(eyes, "待裁决（差异含汉字，必须看原图）")
    if noise:
        section(noise, "附表：纯标点/空白差异",
                "这些差异**不含汉字**，多数能按规则批量定（空格、全半角、破折号）。\n"
                "同样可编辑；判不动的填 `?`，别留空——留空和「还没看」分不开。")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 回读
# ---------------------------------------------------------------------------

# 组标题有两种写法：普通组是「组 已→己 · 60 条」，定位不到的组是「人狱→入狱 · 1 条」
# （后者没有「组」前缀，因为组名本身就是建议词对）。前缀可选，才能两种都认。
RE_GROUP = re.compile("^###\\s*(?:组\\s*)?(.+?)\\s*·\\s*(\\d+)\\s*条\\s*$")
RE_GROUP_VERDICT = re.compile("^整组判定：\\s*(\\S*)\\s*$")
RE_ROW = re.compile("^\\|\\s*`([^`]+)`\\s*\\|")


def parse(md_text):
    """Markdown 表格 -> (rows, errors)。

    rows 每项：{key, verdict, replacement, group}。verdict 为空表示这一条没填。
    errors 是给人看的「第 N 行错在哪」，不是异常——一次报全，别让人改一处跑一次。
    """
    rows, errors = [], []
    group, group_verdict = None, ""
    for lineno, line in enumerate((md_text or "").splitlines(), 1):
        line = line.rstrip()
        m = RE_GROUP.match(line)
        if m:
            group, group_verdict = m.group(1), ""
            continue
        m = RE_GROUP_VERDICT.match(line)
        if m:
            v = m.group(1)
            if v and v not in VERDICTS:
                errors.append("第 %d 行：整组判定取值非法 %r（只允许 A/B/C/?）" % (lineno, v))
            group_verdict = v
            continue
        m = RE_ROW.match(line)
        if not m:
            continue
        key = m.group(1)
        cells = _split_row(line)
        if len(cells) != len(COLUMNS):
            # 旧版只拒绝"列数不足"，多列照常读前 7 列。而多列最常见的来源正是
            # **人在 C 的「替换整行」里写了一个没转义的竖线**：后半段被当成第 8 列丢掉，
            # 而回填是整行覆盖——正文后半句就这么静默没了（实测复现：整行 '左边|右边'
            # 读回来变成 '左边'，errors 还是空的）。所以多列同样必须整份拒绝。
            more = ("；字面竖线要写成 `\\|`（否则后半段会被当成额外一列）"
                    if len(cells) > len(COLUMNS) else "")
            errors.append("第 %d 行：列数 %d，应为 %d（%s）%s"
                          % (lineno, len(cells), len(COLUMNS), key, more))
            continue
        cell_v = _unbacktick(cells[COL_VERDICT])
        repl = _unbacktick(cells[COL_REPL])
        cell_y0 = _unbacktick(cells[COL_Y0])
        if cell_y0:
            try:
                cell_y0 = float(cell_y0)
            except ValueError:
                errors.append("第 %d 行：「行 y0」不是数字 %r（%s）"
                              % (lineno, cell_y0, key))
                continue
        else:
            cell_y0 = None
        v = cell_v or group_verdict
        if v and v not in VERDICTS:
            errors.append("第 %d 行：判定取值非法 %r（只允许 A/B/C/? 或留空）" % (lineno, v))
            continue
        if v == "C" and not repl:
            errors.append("第 %d 行：判定 C 但「替换整行」为空（%s）" % (lineno, key))
            continue
        if v and v != "C" and repl:
            errors.append("第 %d 行：判定 %s 时「替换整行」必须留空（%s）" % (lineno, v, key))
            continue
        rows.append({"key": key, "verdict": v, "y0": cell_y0,
                     "replacement": repl if v == "C" else None, "group": group})
    return rows, errors


def merge_into(doc, rows):
    """把表格里填的判定合回 decisions 文档（按 key）。

    **返回值保持旧签名**：先返回 (改了几条, 未知键列表)——CLI 早就按这两个值报错，
    改签名会把调用方和测试一起打断。第三个返回值是 errors（需要回填但缺坐标之类的
    **具体**错误），只在真出现时才有内容；只想拿前两个值的调用方继续解两个即可。

    只动 verdict / replacement / status 三个字段：A/B 原文一律以 JSON 为准，
    表格里那一列是投影，不回写。
    `status` 重置成 pending 是有意的：判定变了，之前那次「已应用」就不作数了，
    否则 apply_decisions 会把它当成「此前已应用」直接跳过。
    """
    import decisions as dec
    by_key = {e.get("key"): e for e in (doc.get("decisions") or [])}
    unknown, errors, n = [], [], 0
    for r in rows:
        if not r["verdict"]:
            continue
        e = by_key.get(r["key"])
        if e is None:
            unknown.append(r["key"])
            continue
        # 「行 y0」是给**定位不到**的条目补坐标用的：这类条目 key 是 page@?#tag，
        # 没有 y0 就没法回填（apply_decisions 会判 missing）。人填上 y0 之后，
        # key 必须跟着重算成 page@y0 —— 否则下一次 apply 仍按旧 key 找不到它。
        if r.get("y0") is not None and e.get("y0") is None:
            e["y0"] = r["y0"]
            e["key"] = dec.key_of(e.get("page"), r["y0"])
            by_key[e["key"]] = e
        # 判定已填、而且这一条**需要回填**（A/B/C 里的 A 什么都不做、? 是没裁决），
        # 但坐标仍然没有 -> 回填端根本找不到那一行，只会判 missing。
        # 旧版这里一声不响，人按表里的老提示"y0 留空"照做，导入成功、正文没变
        # （实测复现：plan.kind == "missing"，reason「裁决没有 y0（未定位）」）。
        # 与其静默丢，不如在导入这一步就具体报出来，整份不写。
        if r["verdict"] in ("B", "C") and e.get("y0") is None:
            errors.append("键 %s：判定 %s 需要回填，但这一条还没有「行 y0」"
                          "（翻原书找到含错字的那一行，把它的真实 y0 填进「行 y0」列）"
                          % (r["key"], r["verdict"]))
            continue
        if e.get("verdict") == r["verdict"] and e.get("replacement") == r["replacement"]:
            continue                      # 没改就别动 status，保住幂等
        e["verdict"] = r["verdict"]
        e["replacement"] = r["replacement"] if r["verdict"] == "C" else None
        e["status"] = "pending"
        n += 1
    if errors:
        return n, unknown, errors
    return n, unknown


def stats(doc):
    """按 verdict 统计，给 CLI 回显（与 decisions.counts 同口径，但多分了个类）。"""
    out = {}
    for e in doc.get("decisions") or []:
        v = e.get("verdict") or "?"
        out[v] = out.get(v, 0) + 1
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把 decisions.json 投影成可编辑的 Markdown 表格（--import 反向合回）")
    ap.add_argument("--decisions", default="out/decisions.json",
                    help="裁决表路径（默认 out/decisions.json）")
    ap.add_argument("--out", default="out/decisions.md",
                    help="表格路径（默认 out/decisions.md）")
    ap.add_argument("--title", default="", help="书名，只写进标题行")
    ap.add_argument("--only", default="eyes", choices=["eyes", "noise", "all"],
                    help="渲染哪些条目：eyes=差异含汉字（默认）/ noise=纯标点空白 / all")
    ap.add_argument("--import", dest="do_import", default="", metavar="MD",
                    help="把这张表里填的判定合回 --decisions（不给则只渲染）")
    a = ap.parse_args(argv)

    if not os.path.exists(a.decisions):
        sys.exit("找不到裁决表 %s。先生成它：\n"
                 "  python bookflow.py decisions --work <work> --from audit|disagree" % a.decisions)
    doc = jsonio.read(a.decisions)

    # ---- 导入分支 ----
    if a.do_import:
        if not os.path.exists(a.do_import):
            sys.exit("找不到表格 %s（先跑 bookflow.py table 渲染出来）" % a.do_import)
        text = jsonio.read_text(a.do_import)
        rows, errors = parse(text)
        if errors:
            sys.stderr.write("表格有 %d 处问题，一条都没写回（宁可报错也不误改）：\n" % len(errors))
            for e in errors[:30]:
                sys.stderr.write("  " + e + "\n")
            return 2
        got = merge_into(doc, rows)
        n, unknown, errors = got[0], got[1], (got[2] if len(got) > 2 else [])
        if errors:
            # 和 parse/validate 的错误同一待遇：**整份不写**。
            # 只提醒不拦的话，人看到"合入 N 条"就会以为这条已经生效，
            # 而它其实连坐标都还没有，下一阶段只会判 missing。
            sys.stderr.write("表格有 %d 条需要回填但缺「行 y0」，一条都没写回：\n"
                             % len(errors))
            for e in errors[:30]:
                sys.stderr.write("  " + e + "\n")
            return 2
        if unknown:
            sys.stderr.write("表格里有 %d 个键在裁决表中找不到（可能来自另一本书/旧表）：\n"
                             % len(unknown))
            for k in unknown[:10]:
                sys.stderr.write("  " + k + "\n")
            return 2
        import decisions as dec
        probs = dec.validate(doc)
        if probs:
            sys.stderr.write("合入后裁决表不合法，未写盘：\n  " + "\n  ".join(probs[:20]) + "\n")
            return 2
        dec.write(a.decisions, doc)
        print("合入 %d 条 -> %s（当前 %s）" % (n, a.decisions, stats(doc)))
        print("下一步：python apply_decisions.py --work <work> --src ocr_merged --apply")
        return 0

    # ---- 渲染分支 ----
    md = render(doc, title=a.title, source=a.decisions, only=a.only)
    d = os.path.dirname(os.path.abspath(a.out))
    os.makedirs(d, exist_ok=True)
    jsonio.write_text(a.out, md)
    rows, errors = parse(md)
    n_eyes = sum(1 for e in doc.get("decisions") or [] if classify(e) == "eyes")
    n_noise = sum(1 for e in doc.get("decisions") or [] if classify(e) == "noise")
    print("裁决表 %d 条（需看字 %d / 纯标点空白 %d）" % (
        len(doc.get("decisions") or []), n_eyes, n_noise))
    print("表格 -> %s（%d 行可填；渲染自检回读 %d 行）" % (a.out, len(rows), len(rows)))
    if errors:
        sys.stderr.write("  ！自检回读报错 %d 处（说明渲染有 bug）：%s\n"
                         % (len(errors), errors[:3]))
    print("编辑后回传：python bookflow.py table --work <work> --import")
    return 0


if __name__ == "__main__":
    stdio.utf8_stdio()
    sys.exit(main())