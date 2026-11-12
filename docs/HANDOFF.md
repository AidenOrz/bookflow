# HANDOFF — 交接现状

更新时间：2026-11-12

## 这一轮做了什么

以 `REVIEW.md` 的审阅结论为输入，完成第一批修复 + 实测驱动的改进，
并用一本**全新的 350 页扫描书**（《中国僧官制度史》，2005 年 pdfFactory 制作，
带书眉、脚注圈号、底部水印、末尾 19 页重复）做了端到端验证。

### 代码修复（全部有回归验证）

| # | 问题 | 位置 | 验证方式 |
|---|---|---|---|
| 1 | 页眉带阈值写死 10.5%，吃掉正文首行（旧书 5 行 145 字） | `layout.py` | 旧书汉字 233,688→233,812，差异恰好是恢复的 5 行 + 删除标题残片 |
| 2 | 标题残片只往前消费，漏「论的统一」 | `layout.py` | 成书里该残片消失 |
| 3 | 残片判据过宽，误吞正文「的。」 | `layout.py` | 新书汉字不再无故减少 |
| 4 | `fix.py` 两条规则互逆，报 2 处修正净改动 0 | `fix.py` | 29 条正负例测试全过 |
| 5 | `fix.py` 多条全局规则误伤 | `fix.py` | 同上 |
| 6 | 空输入静默成功、写出 0 字节成书 | `layout.py`/`make_md.py` | `TestCli.test_empty_src_exits` |
| 7 | `page=None` 时 `%d` 崩溃并留半成品 | `make_md.py` | 产物改原子写 |
| 8 | `ocr_dir()` 忽略 `ocr_merged`、缺目录崩溃 | `bookflow.py` | 新书验证选中 `ocr_layout` |
| 9 | `--force` 只对 render 生效 | `bookflow.py`/`run_ocr.py` | 透传 + 删除旧 JSON |
| 10 | `qa.py` DUP_PARA 永不触发 | `qa.py` | `TestQA.test_dup_para_detected` |
| 11 | qa 跨页断句 87.8% 无区分度 | `qa.py` | 267 → 17 条并输出明细 |
| 12 | `cmp_ocr.py` 置信度分母用错 | `cmp_ocr.py` | 两版分别计数 |
| 13 | 合并行丢弃低置信片段 | `layout.py` | 合并时 `s = min(...)` |
| 14 | 页码规则无版心约束 | `layout.py` | 加 `x1 < bl or x0 > br` |
| 15 | 目录页判定可能误判正文页 | `layout.py` | 加"连续 2 页"约束 |
| 16 | `left` 取众数会被引文块带偏 | `layout.py` | 改取 10% 分位 |
| 17 | 合并窗口 `merged[-4:]` 会饥饿 | `layout.py` | 改按 cy 回溯 |
| 18 | 目录页码列判据被全角空格打断 | `layout.py` | 放宽为"有引导点" |
| 19 | 目录条目粘连 | `layout.py` | `_looks_entry()` 分离 |
| 20 | 标题折行匹配失败（比值 0.70 被 0.72 门槛挡） | `layout.py` | 拼接相邻行再比 |
| 21 | 源 PDF 重复页导致成段重复 | `layout.py` | `dedupe_pages()` |
| 22 | 空白页水印漏进正文 | `layout.py` | `heads` 判断提前且不受行数保护 |
| 23 | 封面裁切对深色封面失效（生成全白图） | `cover.py` | 改墨迹外接矩形 + 密度裁水印 |
| 24 | JSON 句柄不释放 | 全部 | 统一 `jsonio.read/write` |

### 新增能力

- **版面分析阶段**（`run_layout.py` + `bookflow.py layout`）：
  PP-DocLayoutV3 给每行打 `region` 标签，脚注/页眉/页码不进正文流。
  纯增量设计：没有 `region` 字段的老 JSON 行为完全不变。
- **`--ocr-src`** 显式指定 OCR 来源；**`--no-layout`** 跳过版面分析。
- **`prune` 阶段**：删可重生成的中间产物（新书 `pages/` 约 520 MB）。
- **测试套件** `tests/test_layout.py`：17 个用例，全部来自真实踩坑。
- **`jsonio.py`**、**`requirements.txt`**、**`.gitignore`**。
- **文档迁移到 `docs/`**：architecture / README / HANDOFF / MEMORY。
- **弹窗脚注**（本轮）：`extract_notes`/`plan_notes`/`epub3.py`/`assets/style.css`，
  详见下面「本轮新增：弹窗脚注」。

### OCR 引擎选型（已实测，结论：不换）

详见表见 `docs/README.md`。要点：
- PP-OCRv6 small 精度最高（CER 2.85%）、最快（0.73 s/页），加大模型不提精度。
- PaddleOCR 在 Windows 上官方轮子只有 CPU 版（无 DirectML/CUDA），单页 77 s，
  6GB 显卡用不上，本机是负收益。
- 真正该补的是**版面分析**，已接入。

## 当前状态

| 项 | 状态 |
|---|---|
| 旧书（306 页）全流程 | 可复现，成书 233,812 汉字，44/44 目录命中，EPUB 754.7 KB |
| 新书（350 页）全流程 | 成书 163,090 汉字，103/103 目录命中，453 条脚注（165 弹窗 / 288 页尾注），EPUB 557.1 KB |
| 两书 EPUB 校验 | `check_epub.py` 均 0 XML 错误、封面存在、mimetype 首位 |
| 测试 | 17/17 通过 |
| 文档 | `docs/` 下四份齐全，已按本轮改动更新 |

产物位置（本地，**不进仓库**，见 `.gitignore`）：
- 旧书：`out/`（`book.md` / `report.json` / `qa.txt` / `黑格尔辩证法讲演录.epub`）
- 新书：`run-sengguan/out/`（`book.md` / `report.json` / `qa.txt` / `中国僧官制度史.epub`）
- 审阅报告：`REVIEW.md`（在仓库里）

> 扫书本身有版权，扫描页和成书都只留在本地。仓库里只有代码、文档和测试。

## 本轮新增：弹窗脚注

详见 `docs/architecture.md` 第 6 节。要点：

- `layout.py`：`extract_notes()` 从 `region=footnote` 抽注并按圈号/数字定编号；
  `plan_notes()` 只在"本页正文恰好一个该号 + 注区恰好一条"时内联；
  `assemble()` 输出 `("def", id, text)` 与 `("pn", [...])` 两种块，`to_markdown()` 转成
  `[^id]` 引用与 `> ① …` 页尾注。
- `epub3.py`（新）+ `make_epub.py`（新）：项目自带的 EPUB3 构建器，渲染
  `noteref` / `aside[epub:type=footnote]` 弹窗结构，锚不上的注渲染成 `endnote`。
- `assets/style.css`（新）：正文上标 + 弹窗注文 + `:target` 兜底浮层。
- `bookflow.py` 的 epub 阶段改调 `make_epub.py`，不再依赖外部的 md-to-ebook 脚本。
- `qa.check_notes()`：引用/定义必须一一对应，防断链。

新书实测：453 条注 → 165 可弹窗 + 288 页尾注，引用/定义双向一致 0 问题。
旧书无脚注区，**成书与改动前逐字节相同**。

## 下一步（建议优先级）

1. **提高注号识别率（收益最大）**：现在降级为页尾注的 288 条，主因是正文上标
   ①②③ 根本没被 OCR 认出来（110 页整页无圈号）。可行方向：OCR 前对页面底部
   /小字号区域单独放大重识别，或用版面分析的 `text` 区做二次 pass。
   这是识别问题，**不是匹配算法问题**，改 `plan_notes` 没用。
2. **把实测数字接到 CI**：现在数字是手工填的，容易再次漂移。
   建议写 `tools/bench.py` 跑固定样本并输出机器可读结果。
3. **`disagree.py` 接入 CLI**：流水线图里有"双模型分歧"，但 `bookflow.py all` 从不调用它，
   也没有 CLI 入口。要么加阶段，要么从图里删掉。
4. **跨页合段的 47 处可疑（新书）**：还没逐条人工核对。
   建议用 `run-sengguan/out/qa.txt` 的明细配合 `make_review.py` 出对照图逐条看。
5. **`REVIEW.md` 收敛**：它记录的 23 条陈旧声明已经不再对应现状（根 README 已改成入口），
   可以归档到 `docs/` 或直接删。
