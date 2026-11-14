# HANDOFF — 交接现状

更新时间：2026-11-14

## 最近一轮：文字层入口 + 数据契约 + 图片表格（2026-11-14）

回应「我们与 Marker / MinerU 比缺什么」。三件事全部落地，全量 **205 测试通过**。

### ① 文字层入口（收益最大）

**问题**：有完好文字层的 PDF，我们仍整页跑 OCR。实测一本 927 页的书——
`pdftotext` **7.9 秒 / 623,310 汉字**，OCR 约 **4.5 小时**；抽样 13 页逐字对比，
**文字层 vs OCR 相似度 98.85%**。即 98.85% 的字本来免费在那儿。

**做法**：`textlayer.py`（`pdftotext -bbox` → 我们的页级 JSON）+ `--text-layer` 三档。

| 值 | 行为 |
|---|---|
| `verify`（默认） | 抽文字层 + 跑 OCR，交叉验证，分歧进 `decisions.json`，正文用文字层 |
| `only` | 只抽文字层不跑 OCR；覆盖不足 95% 时**拒绝**并提示改跑 verify |
| `off` | 行为与本功能上线前**逐字节一致** |

选源优先级 `ocr_layout > ocr_merged > ocr_text > ocr_fix > ocr`：ocr_merged 是人工裁决过的，
必须压住原始文字层；ocr_text 又必须高于 ocr_fix/ocr（文字层没有识别错误）。

开发中实测出三个真问题（都已修，带分布依据写进注释）：分行要按 **yMax 基线**而不是 yMin、
行内空格**不能只靠 0.3em 阈值**（英文会黏成 `othercountries`）、**重复框去重**（双图层 PDF）。

### ② 数据契约 `pagedoc.py`

页级 JSON 与 `report.json` 统一盖 `schema`/`schema_version`/`producer`。两条硬约束：
**1651 个现网老 JSON 全部兼容**（坏 0 个）、**版本不认识必须拒绝**（验证过 apply_decisions
遇到 `schema_version=99` 会在 plan 阶段抛错，不会「用新坐标改旧行」）。

### ③ 图片与表格不再静默丢弃

僧官成书：`≤6 字碎片 97（5.3%）→ 26（1.5%）`、table 块 `0 → 11`、
**内容零丢失**（原始 table region 文字 − 成书 table 块文字 = 0 字符）、
目录 103/103、脚注 453 全部不变。规训、黑格尔 `book.md` **逐字节一致**。

两个坑：**目录页和表格几何上分辨不出来**（规训目录页被判成表 → 章节结构静默消失，
改用现成 `toc_ids` 整页跳过）；**宽收句判据会把表切断**（`PEND` 含「）」，改用窄表）。

### 本轮数字

| 项 | 值 |
|---|---|
| 测试 | **205/205 通过**（原 84 → +textlayer 56 +cli 30 +figure 14 +pagedoc 17 +杂项） |
| 黑格尔 | `book.md` 逐字节不变（C33CEB…） |
| 规训 | `book.md` 逐字节不变（E5053B…） |
| 僧官 | 碎片 97→26、table 块 0→11、内容零丢失 |
| 文字层性能 | 927 页 7.9 秒 / 623,310 汉字 / 672.4 汉字每页 |

### 遗留

- `run-sengguan/pages/` 已 prune，僧官 4 个 image region 裁不出图（用 `run-guixun/pages` 验证链路正常）
- `--text-pages` 只允许配合 `textlayer` 单阶段，与 `all` 组合会被入口拒绝
- 中断 `all` 时子进程（如 `pdftoppm`）曾变孤儿继续跑 20 分钟，已加 `kill_tree`

## 上一轮：评估 DocLayout-YOLO（结论：不换，仅记录）

`rapid_layout` 1.2.1 已内置 4 个 DocLayout-YOLO，不需新依赖。实测结论**不换、不接入**：

- 它的「非正文」只有 `abandon` 一个类，`layout.py` 要分脚注/书眉/页码三条路走。
- 把结果原样喂给 `layout.extract_notes`：规训 60 页脚注 **11 → 0 条**，正文膨胀 **+3.0%**。
- 四个模型单页耗时都是 0.16~0.17 s，**不是速度问题，是标签体系问题**。
- 二值一致率 99.49%，但分歧两向都有，净收益 5 行且全在封面页。

没有改任何代码。评估过程与完整数据写进 `docs/README.md`「DocLayout-YOLO 实测」，
结论与「什么条件下才值得重评」写进 `docs/MEMORY.md` 第 16 条。

## 上一轮：代码审阅 3 项缺陷的复核（采纳 2、驳回 1）

docs/CODE_REVIEW_REPORT.md 对 P0/P1 改动集提出 3 处缺陷。逐条实测后：**采纳 2 项、
实证驳回 1 项**（那一条的建议改法会让成书变坏）。测试 84/84 通过。

| # | 审阅意见 | 处置 | 证据 |
|---|---|---|---|
| 1 | `fix.py` 排除表用字符类 `(?![...])` 而非词组分支 `(?!(?:...))`，属笔误，会过度排除 | **实证驳回，改为有意保留 + 补注释** | 换成分支后默认模式会把僧官成书「引人注日」改成「引入注日」（OCR 把「注目」认成「注日」，分支表里没有「注日」）。字符类按第二字「注」就挡住了。代价是「引人思乡/引人往返/引人思考」被顺带放过 = 漏改方向，不会改坏书 |
| 2 | `apply_decisions.plan()` 直接拼 `page + ".json"`，与 `decisions._page_path` 的补零容错不一致，会出现「能生成裁决、回填全部 missing」 | **采纳** | 复现：裁决页名 `p-1.png` + 磁盘 `p-001.png.json` → `missing`。`_page_path` 已提为公开 `page_path()`（留 `_page_path` 别名），`plan()` 改为复用并处理返回 None |
| 3 | `stage_layout` 的「排除 ocr_layout 自身」不该覆盖显式 `--ocr-src` | **采纳** | 加了 `not getattr(a, "ocr_src", "")` 前置判断；源与输出同为 ocr_layout 时提示需要 `--force`（run_layout 对已存在的页是跳过的） |

新增回归：`test_yinren_exclusion_is_char_class_not_phrase_alternation`（把"字符类是有意为之"
钉住，防止后人又"修正"回分支）、`test_page_path_tolerates_padding_mismatch`、
`test_missing_page_reports_not_crash`。三本书 `fix.apply` 输出仍逐字节一致。

## 更早一轮：按 IMPROVEMENT-PLAN 落地 P0 + P1（10 项）

以 [IMPROVEMENT-PLAN.md](IMPROVEMENT-PLAN.md) 为输入，完成 P0 全部 5 项与 P1 全部 5 项。
计划本身是对一份外部审阅意见**逐条核实**后重排的产物，其中 2 条建议被实测证伪
（跨页缩进优先、阈值外部化），详见该文 §5.1 / §5.3。

### P0（止血）

| # | 问题 | 修法 | 验证 |
|---|---|---|---|
| P0-1 | **fix.py 已把成书 3 处正确原文改错**：「引人人胜→引入人胜」「税收人说→税收入说」「不引人怀疑→不引入怀疑」，均已落盘在规训成书 | 规则分两级：AUTO_RULES（整词级，错写法本身不是词）+ CAND_RULES（单字级，带排除表）。默认两级都落笔但**先补掉排除表的已知漏洞**（新增「引人人胜」对子、「引人」排除表加「人/怀疑」、「收人」排除表加「说」）；--conservative-fix 可退回「单字级只出候选、正文不动」 | 规训重跑后三个错词均为 0；相对旧成书**只差 3 行，3 行全是修复** |
| P0-2 | ocr_layout 静默反压 ocr_merged；stage_layout 读自己上一次输出 | ocr_dir() 增加 mtime 新鲜度比较并告警；stage_layout 源排除 ocr_layout 自身；失败时列出每个候选目录的 [缺失]/[空]/[N json] | 造旧 mtime → 打印「ocr_layout 比 ocr_merged 旧」；layout 源自动改用 ocr_merged |
| P0-3 | check_env 漏检 rapid_layout，缺依赖时抛内部 traceback；md 阶段被误拦 | 改 BASE_PKGS + STAGE_PKGS 按阶段表；新增 check_dict()（jieba）与 check_pdftoppm() | anaconda 下 layout 得到人话提示（rc=1、无 Traceback）；md 不再被误拦（rc=0） |
| P0-4 | 无目录页时静默产出无章节结构的书，rc=0 | layout.build() 在 entries==[] 时打醒目告警并写入 report.json 的 toc_warning | 合成无目录书 → 告警 3 行 + toc_warning 字段存在 |
| P0-5 | --author/--publisher 在 md 阶段被丢弃 | make_md 传全三参数并把 book{title,author,publisher} 写进 report.json；stage_epub 缺参数时回落到它 | epub 不传 author/publisher 仍产出正确 dc:creator/dc:publisher |

### P1（结构）

| # | 事项 | 结果 |
|---|---|---|
| P1-1 | elen 统一 | repair.py / disagree.py 删本地副本，改 from layout import elen。实测统一后 repair 可疑行 49/33/15（唯一翻转在目录页、被 assemble 跳过），对成书零影响 |
| P1-2 | epub3 属性转义 + validate 扩展 + 独立测试 | 新增 esc_attr()，属性位（fnref href/id、img src、封面 alt）全部转义；validate() 补反向 fnref 配对、id 唯一、aside 注文非空、nav/ncx 锚点、img src 存在性。新建 tests/test_epub3.py 11 例 |
| P1-3 | apply_decisions 配置化 | 读 out/decisions.json，key=page@y0 稳定主键取代列表下标，verdict=C 支持整行覆盖（修掉 #3/#18 多差异段静默丢失），**默认 dry-run**，备份改时间戳目录只含本次改的页，三态判定（apply/already/missing）实现幂等 |
| P1-4 | disagree 接入 CLI | 新增 disagree 子命令 + --ocr-alt。**刻意不进 all**：需要第二版 OCR、带 --apply 会改 md 输入源、需 jieba。all 末尾检出 ocr_alt 时给提示 |
| P1-5 | decisions.py 适配层 | from_disagree/from_audit/from_queue/merge/load_any，把两套互不兼容的候选格式统一成 decisions.json；定位不到的 hit 保留为 ?+L3 不丢证据 |

### 本轮数字

| 项 | 值 |
|---|---|
| 测试 | **84/84 通过**（原 35 → layout 39 + epub3 11 + decisions 34） |
| 黑格尔 | 264,439 字符 / 233,812 汉字 / 44-44 目录 / EPUB 754.7 KB / validate 0 |
| 规训 | 250,812 字符 / 195,751 汉字 / 16-16 目录 / 95 注（75 弹窗 + 20 页尾）/ EPUB 473.2 KB / validate 0 |
| 僧官 | 200,230 字符 / 163,090 汉字 / 103-103 目录 / 453 注（165 弹窗 + 288 页尾）/ EPUB 557.1 KB / validate 0 |
| 三本书可复现 | 重跑 make_md.py 与现存 book.md **逐字节一致** |

### 清除的过时产物

out/book_before_fix.md、out/book_before_repair.md（旧 fix/repair 前快照，逻辑已换）、
out/review_ai.png（一次性人工裁决图，结论已进 decisions.json）、
out/review_queue.json（一次性队列，已由 out/decisions.json 取代）。
out/decisions.json 现为根目录那本书的正式裁决表（--export-legacy 从旧硬编码表迁移，20 条已逐条核对等价）。

### 下一步

1. **P2 未动**，优先级见 [IMPROVEMENT-PLAN.md](IMPROVEMENT-PLAN.md) §6：tools/bench.py（最高）→ 脚注 a′/c2 → logging 12 处 → 阈值常量集中。
2. out/report.json 现已是新版（含 page_map），旧书 audit 页码链路已恢复；建议顺手重跑一次 bookflow.py audit --work . 让 audit.json 也带上 PDF 页码。
3. --apply 会回写 decisions.json 的 status（幂等实现方式），CI 里复用同一份文件时要知道它有状态。

---

## 早前：《规训与惩罚》验证暴露的 6 个缺陷

用一本**排版结构完全不同**的 383 页扫描书（2012 三联修订译本，`Pdg2Pic` 制作，
无文本层，每个部分有扉页重列章名，脚注用星号 `*` 而非圈号）做端到端验证。
结果是：旧代码在这本书上**直接崩**，修完才能出书。这说明之前的"可复用"结论
只在两本同源扫描件上成立。

| # | 问题 | 位置 | 修法 / 验证 |
|---|---|---|---|
| 1 | `UnboundLocalError: W` —— 一页目录都没有时 `parse_toc` 直接崩溃 | `layout.py` | `W` 提到循环外并给 `assign_levels` 默认值 |
| 2 | 单页目录判不出来（旧规则要求"相邻页也是目录"） | `layout.py` | 加"行尾页码严格递增"作单页证据，避开版权页（版权页也有 7 个行尾数字，但顺序乱） |
| 3 | `第X部分` 与 `第X章` 共用层级名次，16 条目录全变一级 | `layout.py` | `第X部/编/卷/篇` 单独占一档（名次 5，高于章的 10） |
| 4 | 目录页上 1 位页码被"边栏单字"规则删掉 | `layout.py` | 边栏规则加 `not keep_nums` 前置条件 |
| 5 | **章节撞到分部扉页**，三章起始页算成同一页，正文整段被吞 | `layout.py` | 新增 `page_offset()` + 页码锚定：偏移 6 覆盖 295/297 页，命中 11/16 → 16/16 |
| 6 | 「参考书目」被版面模型标成 `header`，整章从成书消失 | `layout.py` | 位置证据优先：`header` 区域但 `y0 > 14%` 页高时保留 |
| 7 | 星号注完全无法弹窗（83 页 0 条） | `layout.py` | 新增 `extract_star_notes()`：注区按缩进切块 + 双重自校验 |
| 8 | 星号注与圈号注重复落盘（158 条 = 83+75） | `layout.py` | 星号通道接管的页，从圈号通道里剔除 |

**这一本的开箱质量**：383 页 / 195,751 汉字 / 16-16 目录全命中 / 95 条注
（75 弹窗 + 20 页尾）/ EPUB 473.3 KB / 校验 0 错误 / 30 测试全过。
旧书与僧官制度史的产出与改动前**逐字节/逐项一致**（回归已验证）。

## 早前：以 REVIEW.md 为输入的批量修复

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
- **测试套件** `tests/test_layout.py`：全部来自真实踩坑（本轮已扩到 84 个用例，见文首）。
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
| 僧官制度史（350 页）全流程 | 成书 163,090 汉字，103/103 目录命中，453 条脚注（165 弹窗 / 288 页尾注），EPUB 557.1 KB |
| 规训与惩罚（383 页）全流程 | 成书 195,751 汉字，16/16 目录命中，95 条脚注（75 弹窗 / 20 页尾注），EPUB 473.3 KB |
| 三书 EPUB 校验 | `epub3.validate()` 均 0 错误、封面存在、mimetype 首位 |
| 测试 | 30/30 通过 |
| 文档 | `docs/` 下四份齐全，已按本轮改动更新 |

产物位置（本地，**不进仓库**，见 `.gitignore`）：
- 旧书：`out/`（`book.md` / `report.json` / `qa.txt` / `黑格尔辩证法讲演录.epub`）
- 新书：`run-sengguan/out/`（`book.md` / `report.json` / `qa.txt` / `中国僧官制度史.epub`）
- 规训与惩罚：`run-guixun/out/`（同上 + `规训与惩罚.epub`）
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

1. **提高注号识别率（收益最大）**：僧官制度史降级为页尾注的 288 条，主因是正文
   上标 ①②③ 根本没被 OCR 认出来（110 页整页无圈号）。可行方向：OCR 前对页面
   底部/小字号区域单独放大重识别，或用版面分析的 `text` 区做二次 pass。
   这是识别问题，**不是匹配算法问题**，改 `plan_notes` 没用。
   
   规训与惩罚的瓶颈不同：星号注的注区在 OCR 后大量丢星号（168 行里只剩 6 行），
   只能靠"注区缩进切块数 == 正文星号数"配对，83 页中 63 页成功、20 页降级。
   要提高得让版面分析把每条注单独成框（现在整片注区是一批 `footnote` 行）。
2. **把实测数字接到 CI**：现在数字是手工填的，容易再次漂移。
   建议写 `tools/bench.py` 跑固定样本并输出机器可读结果。
3. **`disagree.py` 接入 CLI**：流水线图里有"双模型分歧"，但 `bookflow.py all` 从不调用它，
   也没有 CLI 入口。要么加阶段，要么从图里删掉。
4. **跨页合段可疑处**：僧官制度史 47 处、规训与惩罚 37 处，都还没逐条人工核对。
   建议用各自的 `out/qa.txt` 明细配合 `make_review.py` 出对照图逐条看。
   注意其中大量是**参考书目/索引页**的正常分条（每条本身就是独立文献），不是错误。
5. **`REVIEW.md` 收敛**：它记录的 23 条陈旧声明已经不再对应现状（根 README 已改成入口），
   可以归档到 `docs/` 或直接删。