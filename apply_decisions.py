"""按裁决表（decisions.json）回填 OCR 结果。

## 这一版改了什么、为什么（全部有实测依据）

旧版有三个硬伤，都是实测复现过的：

1. **不幂等**。旧版只认「当前行文本 == 裁决里的 A」，对已经回填过的目录再跑一次
   就是「将应用 0 处」+ 6 条「未定位到（原文已变？）」——后者是误导告警：原文
   不是"变了"，是上一次已经改对了。这里改成三态判定 A（原文）/B（目标）/
   其它（真异常），已等于目标的行记为「此前已应用」。
2. **静默丢失**。旧版 build_patches 只处理"单一差异段"，多差异段的条目直接 skip
   且只打一行提示——实测 20 条硬编码裁决里 #3（半角逗号+空格两处差异）、
   #18（三个差异段）因此**从未生效**。这里默认整行覆盖（verdict=B 直接用 B 整行，
   verdict=C 用 replacement 整行覆盖），只有显式 match.require_single_segment=true
   时才退回旧行为。
3. **下标寻址**。旧版 `queue[idx - 1]` 取条目，队列顺序一变裁决整体错位。这里用
   `key = page@y0` 稳定主键（见 decisions.py）。

另外：路径不再相对 cwd 硬编码（--work），JSON 统一走 jsonio，**默认 dry-run**
（旧版默认原地覆写），备份改为「一次运行一个时间戳目录、只放本次真要改的页」
（旧版的 .bak 只在第一次创建，第二次运行不刷新，回滚时拿到的是最老的那份）。

## 用法

    python apply_decisions.py --work .                       # 只看不改（默认）
    python apply_decisions.py --work . --apply               # 真改 + 备份 + 回写 status
    python apply_decisions.py --work . --src ocr_fix --apply # 换回填目标目录
    python apply_decisions.py --work . --queue out/review_queue.json   # 旧格式（弃用）

裁决表的生成见 decisions.py（from_disagree / from_audit / from_queue / merge）。
"""
import argparse
import os
import shutil
import sys
import time
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import jsonio                     # noqa: E402
import decisions as dec           # noqa: E402

# 回填目标目录：相对 --work
DEFAULT_SRC = "ocr_merged"

# ---------------------------------------------------------------------------
# 旧版硬编码裁决表 —— 只给 --export-legacy 用，日常回填完全不读它
#
# 这 20 条来自 review_ai.png 的人工裁决，序号对应 out/review_queue.json 的下标。
# 它们已经迁到 <work>/out/decisions.json（逐条与这张表核对过 verdict/A/B 等价），
# 日常回填只认 decisions.json；这里保留常量只是为了 --export-legacy 在任何工作目录
# 里都能自救（旧工作目录往往没有单独的迁移数据文件），以及后人做等价核验。
#
# 迁移背景（这条要留在代码里）：review_queue.json **不是任何脚本生成的**，它是
# out/disagree.json 的人工筛选子集（20/196 条，字段完全一致：page,y0,y1,x0,x1,
# A,B,verdict,why,sA,sB，page 带 .png），所以既没有生成脚本可查、也不能靠下标稳定
# 寻址——旧版正是栽在 queue[idx-1] 上。
#
# 格式：(verdict, replacement, 说明)
#   "A" = 保留原样（仅登记）；"B" = 采用第二版整行；"?" = 待裁决；
#   replacement 只在 verdict=C 时有值（整行覆盖）。
# ---------------------------------------------------------------------------
LEGACY_DECISIONS = {
    1: ("B", None, "申→由：它是由柏拉图提出来的"),
    2: ("B", None, "一→——：意思——这两个词"),
    3: ("B", None, "半角逗号 → 全角（两个差异段，旧版静默跳过）"),
    4: ("A", None, "「裴颂」：原书即印作颂，通行写法是裴頠 → L3，不改，仅登记"),
    5: ("B", None, "一—→——"),
    6: ("A", None, "规律为己任"),
    7: ("?", None, "行尾破折号归属不明"),
    8: ("?", None, "上标脚注数字，另处理"),
    9: ("A", None, "概念入手"),
    10: ("B", None, "它已跳到你手上来"),
    11: ("?", None, "仅空格差异"),
    12: ("B", None, "已实现出来"),
    13: ("A", None, "决一步步得到提高"),
    14: ("B", None, "我——必然是客观存在的"),
    15: ("A", None, "堕入了另外一种不自由"),
    16: ("?", None, "仅空格差异"),
    17: ("A", None, "Horstmann."),
    18: ("B", None, "〈精神现象学〉（三个差异段，旧版静默跳过）"),
    19: ("A", None, "「萧董父」：原书字形与通行写法 萧萣父 不符 → L3，不改，仅登记"),
    20: ("A", None, "「萧董父」同上"),
}

# 留给人的条目：不该自动改，但要留痕（旧版只是 print 到终端，输出完就丢了）
# 格式：{序号: (level, 说明)}
LEGACY_NEEDS_HUMAN = {
    4: ("L3", "裴颂/裴頠：崇有论作者，通行写法为「裴頠」，但原书印的是「颂」"),
    19: ("L3", "萧董父/萧萣父：《中国哲学史》主编，通行写法待核，原书字形介于两者之间"),
    20: ("L3", "萧董父/萧萣父：同上"),
    8: ("L3", "已3→已：行末上标是脚注序号，应转成脚注而非正文数字"),
}

# 可选的旧表数据文件（--legacy-table）：给了就用文件覆盖上面的常量。
# 留这个口子是为了让"换一本书"的迁移不改代码——但新书该走 decisions.json，
# 这里只是一次性迁移通道。
LEGACY_TABLE = "out/legacy_decisions.json"
LEGACY_TABLE = "out/legacy_decisions.json"


# ---------------------------------------------------------------------------
# 裁决 -> 期望文本
# ---------------------------------------------------------------------------

def diff_ops(a, b):
    """a/b 的非 equal opcode 列表（差异段）。"""
    sm = SequenceMatcher(None, a or "", b or "", autojunk=False)
    return [o for o in sm.get_opcodes() if o[0] != "equal"]


def desired_text(entry, require_single_segment=False):
    """把一条裁决翻译成「这一行最终该是什么」，或给出跳过原因。

    返回 (期望整行文本, None) 或 (None, 跳过原因)。

    verdict=A 不改（原样保留，只是一条登记）；verdict=? 还没裁决。
    verdict=B 是"采用第二版整行"，verdict=C 是"两者都不对，用 replacement 整行覆盖"。
    两者都走整行覆盖——这正是解决旧版"多差异段被静默跳过"的地方。
    require_single_segment=True 时才退回旧行为：只替换 A/B 之间的那一个差异段，
    多段的条目报"跳过"而不是悄悄丢掉。
    """
    v = entry.get("verdict")
    A = entry.get("A") or ""
    B = entry.get("B") or ""
    if v == "A":
        return None, "verdict=A 保留原样（仅登记）"
    if v == "?":
        return None, "verdict=? 未裁决"
    if v == "C":
        rep = entry.get("replacement")
        if not rep:
            return None, "verdict=C 但缺 replacement"
        return rep, None
    if v != "B":
        return None, "verdict 非法：%r" % (v,)
    if not B:
        return None, "verdict=B 但 B 为空"
    if require_single_segment:
        ops = diff_ops(A, B)
        if len(ops) != 1:
            return None, ("差异段 %d 个，非单点（match.require_single_segment=true "
                          "时退回旧行为，跳过）" % len(ops))
        _, i1, i2, j1, j2 = ops[0]
        return A[:i1] + B[j1:j2] + A[i2:], None
    return B, None


# ---------------------------------------------------------------------------
# 计划：算出这一趟要改什么（不写盘）
# ---------------------------------------------------------------------------

def plan(doc, src, y_tol=None, require_single_segment=None):
    """返回 (items, pages)。

    items 每条：{kind, entry, page, path, old, new, reason, line}
      kind = "apply"   本次要改
             "already" 当前行已经等于目标（此前已应用）
             "missing" 定位不到目标行 / 原文与裁决记录的 A 不符
             "skip"    裁决本身不需要动作（A / ? / 配置要求跳过）
    pages：{页名: (路径, 页 JSON)}，只含裁决涉及、且确实存在的页。
    """
    m = doc.get("match") or {}
    if y_tol is None:
        y_tol = float(m.get("y_tol", 1.0))
    if require_single_segment is None:
        require_single_segment = bool(m.get("require_single_segment", False))

    entries = list(doc.get("decisions") or [])
    # 同一页的多条裁决按 y0 升序处理：匹配结果与裁决表里的排列顺序无关
    # （旧版按列表下标 queue[idx-1] 取条目，队列顺序一变裁决整体错位）。
    entries.sort(key=lambda x: (x.get("page") or "", x.get("y0") is None,
                                x.get("y0") or 0))

    pages, items = {}, []
    claimed = {}        # 页名 -> 已被占用的行下标（同一行不能被两条裁决各改一次）
    for e in entries:
        page = e.get("page")
        path = os.path.join(src, page + ".json") if page else None

        # 先判裁决本身要不要动作：verdict=A/? 只是登记，不该再去关心页在不在，
        # 否则会给"仅登记"的条目刷出误导性的"未定位"。
        want, why = desired_text(e, require_single_segment)
        if want is None:
            items.append(dict(kind="skip", entry=e, page=page, path=path,
                              old="", new="", reason=why, line=None))
            continue
        if not page:
            items.append(dict(kind="missing", entry=e, page=None, path=None,
                              old="", new="", line=None, reason="裁决缺 page 字段"))
            continue
        if page not in pages:
            if not os.path.exists(path):
                items.append(dict(kind="missing", entry=e, page=page, path=path,
                                  old="", new="", line=None,
                                  reason="找不到页文件 %s.json" % page))
                continue
            pages[page] = (path, jsonio.read(path))
        pd = pages[page][1]
        lines = pd.get("lines") or []
        y0 = e.get("y0")
        if y0 is None:
            items.append(dict(kind="missing", entry=e, page=page, path=path,
                              old="", new="", line=None,
                              reason="裁决没有 y0（%s）" % (e.get("note") or "未定位")))
            continue
        idx, L, dist = _nearest_line(lines, float(y0), y_tol, claimed.setdefault(page, set()))
        if L is None:
            items.append(dict(kind="missing", entry=e, page=page, path=path,
                              old="", new="", line=None,
                              reason="页内 y0±%g 范围内没有行（最近差 %.1f）" % (y_tol, dist)))
            continue
        claimed[page].add(idx)
        cur = L.get("t") or ""
        if cur == want:
            items.append(dict(kind="already", entry=e, page=page, path=path,
                              old=cur, new=want, line=L, reason="当前行已是目标文本"))
        elif cur == (e.get("A") or ""):
            items.append(dict(kind="apply", entry=e, page=page, path=path,
                              old=cur, new=want, line=L, reason=""))
        else:
            # 既不是裁决记录的 A、也不是目标：不敢改。旧版把这种情况和"已应用"混成同一句
            # "未定位到（原文已变？）"，于是一次重跑刷出 6 条假告警；这里两态分开。
            items.append(dict(kind="missing", entry=e, page=page, path=path,
                              old=cur, new=want, line=L,
                              reason="原文与裁决记录的 A 不一致，且不等于目标"))
    return items, pages


def _nearest_line(lines, y0, tol, claimed):
    """在页内按 y0 找最近的一行（未被占用）。返回 (下标, 行, 距离)。"""
    best = (None, None, None)
    for i, L in enumerate(lines):
        if i in claimed:
            continue
        try:
            d = abs(float(L.get("y0", 0)) - y0)
        except (TypeError, ValueError):
            continue
        if d > tol:
            continue
        if best[2] is None or d < best[2]:
            best = (i, L, d)
    if best[1] is None:
        # 一个都没命中时也要给出"最近差多少"，方便人判断是页对错了还是 y0 漂了
        near = None
        for L in lines:
            try:
                d = abs(float(L.get("y0", 0)) - y0)
            except (TypeError, ValueError):
                continue
            near = d if near is None else min(near, d)
        return None, None, (near if near is not None else 0.0)
    return best


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

def backup_dir_for(src, decisions_path, backup_dir=None):
    """一次运行一个时间戳目录：<src>/.bak/<timestamp>-<decisions 文件名>/。

    旧版是 p-xxx.json.bak、且只在第一次创建——第二次运行不刷新，回滚拿到的是
    最老的一份，等于没有备份。时间戳目录保证每次运行的"改动前快照"都在。
    """
    root = backup_dir or os.path.join(src, ".bak")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = os.path.basename(decisions_path) or "decisions.json"
    return os.path.join(root, "%s-%s" % (stamp, name))


def apply_items(items, pages, src, backup_dir=None, no_backup=False,
                decisions_path=""):
    """把 plan 出来的 apply 项真正写进页文件，返回 (改动页数, 备份目录)。"""
    by_page = {}
    for it in items:
        if it["kind"] == "apply":
            by_page.setdefault(it["page"], []).append(it)
    if not by_page:
        return 0, None

    bak = None
    if not no_backup:
        bak = backup_dir_for(src, decisions_path, backup_dir)
        os.makedirs(bak, exist_ok=True)

    for page, its in by_page.items():
        path, pd = pages[page]
        for it in its:
            it["line"]["t"] = it["new"]
        if bak:
            # 只备份「本次真正要改的页」（旧版给所有改动页各留一个 .bak，且只留第一次的）
            shutil.copy2(path, os.path.join(bak, os.path.basename(path)))
        jsonio.write(path, pd)          # 走 jsonio：先 .tmp 再 replace，不留半个文件
    return len(by_page), bak


def writeback(doc, items, decisions_path):
    """把本次结果回写到 decisions.json 的 status（pending/applied/missing）。

    这是"可观测"的一半：光凭文本比对已经能幂等，status 只是让人一眼看出
    "这条到底落没落地"。dry-run 不写，避免干跑也改文件。
    """
    updated = {}
    for it in items:
        if it["kind"] in ("apply", "already"):
            updated[it["entry"]["key"]] = "applied"
        elif it["kind"] == "missing":
            updated[it["entry"]["key"]] = "missing"
    for e in doc.get("decisions") or []:
        if e.get("key") in updated:
            e["status"] = updated[e["key"]]
        elif e.get("verdict") in ("A", "?"):
            e["status"] = e.get("status") or "pending"
    dec.write(decisions_path, doc)


# ---------------------------------------------------------------------------
# 迁移：旧硬编码表 -> decisions.json
# ---------------------------------------------------------------------------

def load_legacy_table(path):
    """读旧裁决表数据文件，返回 (legacy, needs_human) 两个字典（键为 int 序号）。

    文件格式（JSON）：
        {"decisions": {"1": ["B", null, "说明"], ...},
         "needs_human": {"4": ["L3", "说明"], ...}}

    这个文件是给"--legacy-table"用的可选输入：换一本书要迁旧表时，把旧表丢成数据文件
    再跑 --export-legacy，就不用为了迁移去改源码（改源码也没法 diff 出"这次改了哪条裁决"）。
    默认不读它——没有这个文件时走模块里的 LEGACY_DECISIONS / LEGACY_NEEDS_HUMAN。
    """
    raw = jsonio.read(path)
    decisions = {}
    for k, v in (raw.get("decisions") or {}).items():
        repl = v[1] if len(v) > 1 else None
        decisions[int(k)] = (v[0], repl, v[2] if len(v) > 2 else "")
    needs_human = {}
    for k, v in (raw.get("needs_human") or {}).items():
        needs_human[int(k)] = (v[0], v[1] if len(v) > 1 else "")
    return decisions, needs_human


def legacy_table(work, table_path=""):
    """取旧裁决表：显式给了 --legacy-table 就只认文件，否则用模块常量。

    默认走常量是有意的：旧工作目录里往往没有单独的迁移数据文件，而 --export-legacy
    是一次性自救通道，不能因为缺一个文件就跑不起来（部署时那个文件根本不存在）。
    给了文件却读不到则明确报错，不静默退回常量——否则"我明明改了这个文件"会白改。
    """
    if table_path:
        p = table_path if os.path.isabs(table_path) else os.path.join(work, table_path)
        if not os.path.exists(p):
            raise SystemExit("找不到旧裁决表数据文件 %s（--legacy-table 指定路径不存在）" % p)
        return load_legacy_table(p)
    return dict(LEGACY_DECISIONS), dict(LEGACY_NEEDS_HUMAN)


def legacy_to_decisions(queue, legacy, needs_human=None):
    """旧 DECISIONS + NEEDS_HUMAN + review_queue -> decisions 文档。

    `legacy` 是 {序号: (verdict, replacement, 说明)}，序号从 1 开始、对应 review_queue 的
    下标（这正是旧版的寻址方式，也是它一旦队列重排就整体错位的原因，所以导出后一律
    换成 page@y0 主键）。`needs_human` 是 {序号: (level, 说明)}，给 L3 条目。

    `legacy` 由 legacy_table() 提供：默认取模块内置的 LEGACY_DECISIONS（就那 20 条），
    也可以用 --legacy-table 换成数据文件。日常回填完全不经过这里——只有 --export-legacy
    会调它，历史上那 20 条正是这样迁进 out/decisions.json 的（逐条核对过 verdict/A/B 等价）。
    """
    out = []
    for idx in sorted(legacy):
        verdict, repl, comment = legacy[idx]
        if idx - 1 >= len(queue):
            raise SystemExit("review_queue.json 只有 %d 条，裁决 #%d 越界" % (len(queue), idx))
        c = queue[idx - 1]
        note = comment
        level = "L1"
        if idx in needs_human:
            level, extra = needs_human[idx]
            note = (note + "；" if note else "") + extra
        out.append(dec.make_decision(
            c.get("page"), c.get("y0"), A=c.get("A") or "", B=c.get("B") or "",
            verdict=verdict, replacement=repl, level=level, note=note))
    doc = dec.merge(out)
    doc["source"]["queue_from"] = "queue"
    doc["match"]["require_single_segment"] = False
    return doc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="按 decisions.json 回填 OCR 行文本（默认 dry-run，不动文件）")
    ap.add_argument("--work", default=".", help="工作目录（默认当前目录）")
    ap.add_argument("--decisions", default="",
                    help="裁决表路径（默认 <work>/out/decisions.json）")
    ap.add_argument("--src", default=DEFAULT_SRC,
                    help="回填目标 OCR 目录，相对 --work（默认 %s）" % DEFAULT_SRC)
    ap.add_argument("--apply", action="store_true",
                    help="真正写盘；不加此参数一律 dry-run（只打印将要做出的修改）")
    ap.add_argument("--dry-run", action="store_true",
                    help="兼容旧参数：等同于不加 --apply（默认已是 dry-run）")
    ap.add_argument("--backup-dir", default="",
                    help="备份根目录（默认 <src>/.bak）")
    ap.add_argument("--no-backup", action="store_true", help="不生成备份（危险）")
    ap.add_argument("--queue", default="",
                    help="[弃用] 旧 review_queue.json 路径；给定时按 from_queue 转换后使用")
    ap.add_argument("--require-single-segment", action="store_true",
                    help="退回旧行为：只替换单一差异段，多差异段条目跳过")
    ap.add_argument("--y-tol", type=float, default=None,
                    help="按 y0 匹配行的容差（默认取裁决表 match.y_tol，缺省 1.0）")
    ap.add_argument("--export-legacy", nargs="?", const="", default=None,
                    metavar="PATH",
                    help="迁移：把旧裁决表导出成 decisions.json（PATH 省略时写 "
                         "<work>/out/decisions.json）")
    ap.add_argument("--legacy-table", default="",
                    help="[仅 --export-legacy] 旧裁决表数据文件；不给就用脚本内置的旧表")
    a = ap.parse_args(argv)

    work = os.path.abspath(a.work)
    src = a.src if os.path.isabs(a.src) else os.path.join(work, a.src)
    decisions_path = a.decisions or os.path.join(work, "out", "decisions.json")

    # ---- 迁移分支 ----
    if a.export_legacy is not None:
        dst = a.export_legacy or decisions_path
        qpath = a.queue or os.path.join(work, "out", "review_queue.json")
        if not os.path.exists(qpath):
            raise SystemExit("找不到旧队列 %s（它本是 out/disagree.json 的人工筛选子集，"
                             "见 decisions.py 的 from_queue/from_disagree）" % qpath)
        legacy, needs_human = legacy_table(work, a.legacy_table)
        queue = jsonio.read(qpath)
        doc = legacy_to_decisions(queue, legacy, needs_human)
        dec.write(dst, doc)
        n = dec.counts(doc)
        print("已导出旧裁决 -> %s（%d 条：%s）" % (dst, len(doc["decisions"]), n))
        print("核对无误后，裁决只以 decisions.json 为准（内置旧表仅供本迁移通道使用）。")
        return 0

    if not os.path.isdir(src):
        raise SystemExit("找不到 OCR 目录: %s（--src 相对 --work=%s）" % (src, work))

    # ---- 读裁决表 ----
    if a.queue:
        print("警告：--queue 已弃用（旧队列没有稳定主键，顺序一变裁决整体错位）。",
              "建议：bookflow.py decisions --work <work> --from queue 先生成 decisions.json，",
              "再对本脚本用 --decisions。本次按 from_queue 临时转换。", file=sys.stderr)
        doc = dec.from_queue(jsonio.read(a.queue))
    elif os.path.exists(decisions_path):
        doc = dec.read(decisions_path)
    else:
        raise SystemExit("找不到裁决表 %s。" % decisions_path
                         + "生成方式：bookflow.py decisions --work <work> --from disagree|audit|queue")
    probs = dec.validate(doc)
    if probs:
        raise SystemExit("裁决表不合法（宁可报错也不误改）：\n  " + "\n  ".join(probs[:20]))

    dry_run = not a.apply or a.dry_run
    items, pages = plan(doc, src, y_tol=a.y_tol,
                        require_single_segment=True if a.require_single_segment else None)

    # ---- 打印计划 ----
    for it in items:
        if it["kind"] == "apply":
            print("  %-12s %-28s -> %s" % (it["page"], (it["old"] or "")[:26],
                                           (it["new"] or "")[:34]))
    n_apply = sum(1 for it in items if it["kind"] == "apply")
    n_already = sum(1 for it in items if it["kind"] == "already")
    n_missing = sum(1 for it in items if it["kind"] == "missing")
    n_skip = sum(1 for it in items if it["kind"] == "skip")

    applied = 0
    bak = None
    if not dry_run:
        _, bak = apply_items(items, pages, src,
                             backup_dir=a.backup_dir or None,
                             no_backup=a.no_backup,
                             decisions_path=decisions_path)
        applied = n_apply
        writeback(doc, items, decisions_path)

    if bak:
        print("备份 -> %s（只含本次改动的页）" % bak)
    elif dry_run and n_apply:
        print("（dry-run，未写盘；加 --apply 才会改文件并备份）")

    for it in items:
        if it["kind"] == "missing":
            e = it["entry"]
            print("未定位 %-28s %s" % (e.get("key", "?"), it["reason"]))
    skipped = [(it["entry"], it["reason"]) for it in items if it["kind"] == "skip"]
    if skipped:
        print("跳过 %d 条：" % len(skipped))
        for e, why in skipped[:20]:
            print("  %-28s %s" % (e.get("key", "?"), why))

    print("%s %d 处；本次落盘 %d 处；此前已应用 %d 处；未定位 %d 处；跳过 %d 处"
          % ("将应用" if dry_run else "已应用", n_apply, applied, n_already,
             n_missing, n_skip))
    # 机器可读：CI / 测试按这行取数，别去解析上面那句中文
    print('SUMMARY {"will_apply": %d, "applied": %d, "already_applied": %d, '
          '"missing": %d, "skipped": %d, "dry_run": %s}'
          % (n_apply, applied, n_already, n_missing, n_skip,
             "true" if dry_run else "false"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
