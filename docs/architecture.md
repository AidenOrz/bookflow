# bookflow 架构

把中文扫描书还原成可跳转、可重排的 EPUB 3.0。整条链路本地运行，不上传任何页面。

## 1. 目标与边界

**做**：像素 → 文字（OCR 与查漏补缺）→ 结构（去版式噪声、合段、目录驱动分章）→ EPUB。
**不做**：双栏、表格、公式的结构化重建（这类书请改用 MinerU / PP-StructureV3）。
**不做**：全自动修错——凡是"两种写法都成立"的地方只出候选清单，交人裁决。

核心判断：**章节层级由书里印好的目录页决定，不靠字号猜。**
PDF→EPUB 其实是两个异质任务，像素→文字占 80% 工作量与 95% 失败率，
市面工具大多只做后半段（默认输入有完好文字层）。本流水线把前半段也做实。

## 2. 流水线

~~~
PDF ──render──► pages/*.png              300dpi 灰度（pdftoppm）
        │
      ──textlayer──► ocr_text_raw/*.json + ocr_text/*.json   PDF 自带文字层（pdftotext -bbox）【默认 verify】
        │
      ──ocr──► ocr/*.json                行级 {x0,y0,x1,y1,t,s}（RapidOCR PP-OCRv6 small）
        │
    ──repair──► ocr_fix/*.json           整页识别退化行逐行重识别
        │
   ──layout──► ocr_layout/*.json         版面分析打 region 标签（脚注/页眉/页码）【可选】
        │
        ▼  （以下给 make_md.py）
   ──assemble──► out/book.md             版面清理 → 同行合并 → 目录解析 → 顺序匹配 → 合段
        │
     ──audit──► out/audit.txt + review.png   词表+形近字置换找替换型错字（不自动改）
        │
     ──epub──► out/<书名>.epub               EPUB3 + 三级目录 + 弹窗脚注 + 封面（epub3.py）
~~~

## 3. 模块清单

| 文件 | 职责 | 关键点 |
|---|---|---|
| `bookflow.py` | CLI 编排 | 阶段入口（含 `disagree`/`decisions`）、**按阶段**依赖自检、OCR 源新鲜度告警 |
| `run_ocr.py` | 全量 OCR | 多进程；每进程一份引擎实例；GPU 自动用 DirectML |
| `repair.py` | 补识别 | 按「认出字数 ÷ 应有字数 < 0.62」找可疑行，单行重识别 |
| `run_layout.py` | 版面分析 | PP-DocLayoutV3，给每行加 `region` 字段，并写 `regions[]`（图/表/脚注区），可选增强 |
| `textlayer.py` | 文字层抽取 | `pdftotext -bbox` → 页级 JSON；按 **yMax** 聚行、行内空格判据、重复框去重；**叠字图层按字高拆行再合并**（`split_mixed_height_row` + `collapse_duplicate_rows`）；stdout 末尾的 `SUMMARY` 行是 CLI 契约 |
| `layout.py` | 版面与结构 | 去页眉/页码/书脊、同行合并、目录解析、顺序匹配、合段 |
| `fix.py` | 形近字修正 | `AUTO_RULES`（整词级，错写法本身不是词）+ `CAND_RULES`（单字级，带排除表）；统计净改动；`--conservative-fix` 可让单字级只出候选 |
| `qa.py` | 质检闸门 | 空缺页、短行、低置信、段落异常、跨页断裂 |
| `audit.py` | 替换型错字候选 | 词表归属 + 形近字置换 + 边界判据 + 全书高频否决 |
| `make_review.py` | 候选图 | 把可疑行从原图裁出拼成对照图，供人工/VLM 裁决 |
| `disagree.py` | 双模型分歧 | 两版 OCR 交叉比对，词频强弱比判胜负 |
| `apply_decisions.py` | 裁决回填 | 读 `decisions.json`，`key=page@y0` 稳定主键；**默认 dry-run**；整行覆盖支持多差异段；时间戳备份目录；三态判定（apply/already/missing）保证幂等 |
| `decisions.py` | 裁决表适配层 | `from_disagree`/`from_audit`/`from_queue`/`merge`，把两套互不兼容的候选格式统一成 `decisions.json` |
| `cover.py` | 封面 | 从首页裁封面，按墨迹密度去水印带；不合适的页返回 None；`alt` 属性走 `epub3.esc_attr` |
| `make_md.py` | md 阶段入口 | 调 layout → fix → qa，三个产物原子写 |
| `epub3.py` | EPUB3 构建 | 章节切分、nav/ncx、封面、**弹窗脚注渲染**、自校验 |
| `make_epub.py` | epub 阶段入口 | `epub3.py` 的 CLI |
| `qa.py` / `check_epub.py` | 校验 | 成书前闸门（含脚注完整性）/ EPUB 包结构自校验 |
| `stdio.py` | 输出编码 | `utf8_stdio()`：把 stdout/stderr 强制成 UTF-8 且 `errors="replace"`。**所有会打印用户文本的脚本入口都要调**（有测试扫源码）。Windows 子进程默认 GBK，一个非 GBK 字符就能让脚本崩在半路 |
| `jsonio.py` | IO 工具 | with 包住句柄；写临时文件再 rename。**所有**产物写入都该走它（含 `audit` / `disagree` / `cmp_ocr`），页级 JSON 走 `pagedoc.write` |
| `pagedoc.py` | **数据契约** | 页级 JSON 与 report.json 的 `schema`/`schema_version`/`producer`；老文件兼容、新版本拒绝；`stamp`/`read`/`check`/`describe` |

## 4. 数据契约

页级 JSON（`ocr/*.json`、`ocr_fix/*.json`、`ocr_layout/*.json`、`ocr_text/*.json` 同一 schema）：

~~~json
{
  "schema": "bookflow.pages", "schema_version": 1,
  "producer": {"name": "rapidocr", "version": "3.10.0"},
  "name": "p-020.png", "w": 2480, "h": 3509,
  "lines": [
    {"x0": 443, "y0": 767, "x1": 2168, "y1": 836,
     "t": "佛教势力发展…", "s": 0.9968, "region": "text", "r": 0.937}
  ],
  "regions": [{"x0":359,"y0":2787,"x1":2147,"y1":3072,"cls":"footnote","s":0.893}]
}
~~~

`region` / `r` / `regions` 是 **可选字段**，只有跑了 `run_layout.py` 才有。
`layout.py` 对缺失字段的行为与旧版完全一致。

### 4.1 版本字段（`pagedoc.py`）

四个写入方（`run_ocr.py` / `repair.py` / `run_layout.py` / `textlayer.py`）都必须调用
`pagedoc.stamp(doc, pagedoc.producer(name, version))` 再写盘。`producer.name` 取值：
`rapidocr` / `rapidocr-repair` / `pp_doc_layoutv3` / `pdftotext`。

`out/report.json` 用 `pagedoc.stamp_report()`，schema 为 `bookflow.report`。

**两条硬约束**：

1. **向后兼容**：没有 `schema` / `schema_version` 的老 JSON 必须照常读，
   `pagedoc.check()` 返回空列表。三本验证书工作目录里几千个老文件不能因此跑不动。
2. **版本不认识就拒绝**：`schema_version` 不等于当前值是**错误**而不是警告。
   本项目头号风险是静默出错，宁可停下来让人升级代码或降级数据。

`producer` 表达**真实生成来源**，任何加载/加工/回填步骤都不得改写它——
它是判断「这份数据该不该信」的唯一依据（文字层可信度 1.0，OCR 约 0.985）。

为什么不引 Pydantic：只有 4 个写入方、字段少，严格校验会把「兼容旧数据」变成负担。
只校验版本号能不能认，其余交给各阶段自己的断言。

## 5. 关键设计决策

| 决策 | 理由 |
|---|---|
| 用**印刷目录页**定章节层级 | 字号猜测在不同书/不同扫描下不稳；目录页是权威 |
| 顺序**单调匹配** + 允许二次补搜 | 章节在正文中必然有序；一次漏配不该拖垮后面全部 |
| 目录匹配优先**精确文本一致** | 相似度相近时，"去掉标点后完全相同"是最硬的证据 |
| 页眉带阈值**自适应** | 写死 10.5% 曾把正文首行当书眉删掉（实测 5 行）；改为从数据学 |
| 标题残片**双向**消费 + 字号判据 | 折行标题可能命中中间一行；只看长度会误吞正文的「的。」 |
| 脚注用**版面分析**而非位置猜 | 位置猜不准脚注与正文的分界；PP-DocLayoutV3 直接给出 footnote 区 |
| 脚注锚定**宁可少连不可错连** | 按顺序盲配会把正文并列项 ①②③ 错接成脚注，读者点开看到不相干引文；对不上就降级成页尾注 |
| EPUB 自己拼 XML，**不用 pandoc** | 弹窗脚注需要精确控制 `noteref`/`aside[epub:type]`，且章级切分按本书目录层级而非 `#` 数量；见第 7 节 |
| 目录页判定加**行尾页码严格递增** | 只有一页目录的书拿不到「相邻页」证据，旧规则永远判不出来（规训与惩罚整本只有 1 页目录）；版权页也有 7 个行尾数字，但顺序乱，这条恰好分开两者 |
| `第X部/编/卷/篇` **单独一档名次** | 与 `第X章` 共用名次会把整本目录拍平成一级；不分部的书不出现这档，映射自然回退 |
| 章节匹配**优先按印刷页码锚定** | 分部扉页会重列该部分的章名，纯文本相似度必然撞车，三章起始页被算成同一页、正文整段被吞；页码偏移实测 295/297 页吻合 |
| **位置证据优先于区域标签** | 版面模型会把居中大字的章标题标成 `header`（「参考书目」y=20.4%），照标签丢会让整章消失 |
| 星号注与圈号注**走两条通道** | 星号注的「编号」是星号个数，且注区星号大量丢失（168 行只剩 6 行）；只能从正文侧数、注区按缩进切块，再用「块数相等 + 非末块收句」双校验 |
| fix 只在**确定性规则**上自动改 | 两种写法都成立的场景，误改比漏改更伤 |
| fix 的**单字级规则靠排除表兜底** | 「X+人+动词」是开放集，排除表必然漏项：实测漏了「人胜/怀疑/说」，把成书里 3 处正确原文改错。现在补掉已知漏洞，并留 --conservative-fix 一键退回只出候选 |
| `引人` 排除表用**字符类**而非词组分支 | 看似笔误，实为更安全：OCR 常认错词的第二字（「注目」→「注日」），字符类按第二字拦得住，词组分支拦不住会改坏正文。代价是白名单外搭配被放过 = 漏改，不伤书 |
| 裁决**生成端与回填端共用 `page_path()`** | 生成端有补零容错、回填端直接拼 `page + ".json"`，会出现「能生成裁决、回填全 missing」。两边口径必须同源 |
| 跨页合段**只看标点，不引入缩进判据** | 实测「缩进优先」在三本书上翻转 22/32/53 处，且会把章标题（规训 p199「第二章」、p207「规范化裁决」）和页末注文并进上一段。根因是 left 逐页独立计算，跨页时等于拿两页各自的零点相减（黑格尔 75.7% 的页 left 偏离全书基准 >0.6 字宽） |
| 依赖自检**按阶段**列表 | 一个大元组会让 md 阶段被 rapidocr 误拦，而它根本不 import；layout 阶段真正缺的 rapid_layout 又没检查，用户只看到内部 traceback |
| OCR 源加**新鲜度告警** | 先跑 layout 再回填 ocr_merged 时，优先级顺序不变但选中的仍是旧 ocr_layout，裁决成果被静默丢弃。顺序不能乱改（会改变两本验证书的选源），所以只加告警 |
| 裁决表用 **page@y0 稳定主键** | 旧版按列表下标取条目，队列顺序一变裁决整体错位；且多差异段会被静默跳过（20 条硬编码里 2 条从未生效） |
| 裁决回填**默认 dry-run** | 它会原地覆写 OCR，而重跑一本 383 页的书要 4.3 分钟；一次误跑不可逆的成本远高于多打一个 --apply |
| 重复页**按内容去重** | 扫描件常见整段页重复；不去重会成段重复正文（实测 19 页） |
| 叠字图层**按字高拆行 + 按几何合并** | 逐对比较（`dedupe_words`）在这类 PDF 上永远判不出来：两套小框同基线并列、文本各不相同，整行大框与紧邻小框又不等长。改成「一行有两簇字高」+「两条候选 x 跨度几乎完全重合」两个纯几何判据。实测成书从 26.1 万汉字（+80.6%）回到 14.8 万（+0.9%） |
| 打印用户文本的脚本**自己修 stdio 编码**，不靠调用方 | 实测 `repair.py` 崩在 `print()` 上时已写完 129/310 页，退出码 1 让 `bookflow.py all` 判整个阶段失败、剩余 181 页一个字没写。业务全对、话说不出口，结果等于全错。用测试扫源码保证 14 个入口都调了 |
| `mark_value` 之类的字符分类函数**认不出就返回 None，不抛** | `NOTE_HEAD` 的 `[\u2070-\u209c]` 里有下标 ₀-₉、上标 i、上标运算符，全不是数字。旧实现 `表.index(s)` 遇到 S₁ 直接 ValueError，整本 `make_md` 失败。一个正区间当成「字符集」用，就埋下这颗雷 |
| 目录页码判据既要**位置**也要**文本形态** | 这本书整页是「标题 /页码」：整行一个框、x0 在版心左边、没有引导点，旧的「右半侧 + 引导点」两条全不成立，13 条目录被压成 12 条怪物、命中 0、成书无章节。补充「斜杠+数字 / 斜杠+罗马数字」，并加负向断言「斜杠前不能紧贴数字」挡住「比例 3/4」 |
| 产物**原子写** | 中途崩溃不能留下「book.md 有、report.json 没有」的半成品 |
| 清理路径**必须带超时且有兜底** | `taskkill` 卡在 WMI/RPC 上会让「Ctrl+C 收尸」永远回不来（比子进程变孤儿更糟）；而 `check=False` 又会让权限不足/进程已消失时静默什么都不做。两个失败方向相反，所以既要 `timeout`，也要在非 0 时补一次 `proc.kill()` |
| 属性位转义**只有一个入口** | `epub3.esc_attr()`。`cover.py` 曾自己 `format(title=...)` 拼进 `alt=""`，书名带引号就产出坏 XML；`esc()` 刻意 `quote=False`，专供文本节点，两者不能混用 |
| 「空样本」当成必然事件 | `statistics.median` / `max()` / `[0]` 都假设非空。`repair.median_char_width` 收不到样本时抛 `StatisticsError`，把 `main()` 里那句友好提示截胡了。统计函数返回 `None`，由调用方决定报错还是降级 |
| 文字层默认 **verify**（抽 + 跑 OCR 对账），而非只抽或不用 | 书的文字层质量参差不齐：完好的那批快 40 倍且有 0 识别错误，残缺的那批（只有页眉页码）直接用会让正文清空。对账是唯一同时拿到「快」和「不静默出错」的办法；`only` / `off` 留给确认过质量的书 |
| 交叉验证的 **A 侧必须是文字层** | `disagree.py` 的 verdict 是「倾向哪侧对」，`apply_decisions` 的 verdict=A 是「保留 A 侧不动作」。两侧对调会让更准的来源被判成该被覆盖 |
| 只有 **≥95% 页有正文**的书才让 `ocr_text` 参与选源 | 「部分页有文字」当正文 = 另一半页直接消失；且两套分行口径混在一本书里，跨页合段会在混源页边界误判 |
| 图/表**裁不出来就退回文字块**，不丢行 | 静默丢图只是「少了张图」，静默丢字是「少了段正文」。实测 p053 整页图版原样保留 45 行乱码当 table 块，虽难看但不丢 |
| 「书眉」的判断放在**学习阶段**，丢弃阶段只认「学到与否」 | 曾在丢弃阶段用「窄+短+在边缘」补判书眉，实测吃掉僧官 61 行/453 汉字、《黑格尔辩证法讲演录》20 行。用「与书眉/章名/书名的 Dice 相似度」去救也救不回来——这 61 行最高相似度只有 0.60（阈值 0.75 一行都过不了），全是正文。真书眉在上游已被 `region=header` 标签 + 跨页重复学习 + 自适应带状区三重删净，三重都没拦下的边缘窄短行实测 87/8/805 行（僧官/规训/黑格尔），逐行看过全是正文 |
| 书眉学习窗口取 **0.18H + 窄行守门**，而非 0.11H | 书眉坐多低因书而异：《规训与惩罚》章节书眉 y1 到 13.4% 页高，0.11H 只学到 2 条、漏 9 条，而 `head_band` 有 0.09 硬上限兜不住，残片（「第二章 断头台的场面第一章」）会拼进正文。但 0.18H 里绝大多数行是正文，所以顶部必须配「`w > 0.55W` 的行不参与学习」——学到的文本在 `clean_page` 里不看位置一律删，误学一个正文长句就是全书丢句 |

## 6. 为什么不用 pandoc

pandoc 3.8 本机可用，实测同一份 `book.md`（556 KB EPUB，0.68 s）它能正确产出
`epub:type="noteref"` / `aside[epub:type="footnote"]`，这一点和自建构建器等价。
但有三处必须自建才能控制：

| 差异 | pandoc | 自建 `epub3.py` |
|---|---|---|
| 脚注作用域 | `footnotes-end-of-document`：注文堆在**文件末尾**，长章要翻很远 | 按引用顺序就地渲染，`aside` 紧跟该章正文 |
| 页尾注（无锚点的注） | 不支持。`> ① …` 被当成**引用块**，其中以数字开头的还会被误判成**有序列表**（实测 41 处） | `aside[epub:type="endnote"]`，与弹窗脚注语义区分 |
| 章切分 | 按 Markdown `#` 数量（`--split-level`），章级别写死 | 按本书印刷目录给的层级，讲/章=1、节=2 |
| 输出目录 | `EPUB/text/`、`EPUB/styles/`，另加 `com.apple.ibooks.display-options.xml` | 扁平根目录，与 `cover.py` 的注入路径一致 |

结论：pandoc 在**纯文档转换**上更强（表格、公式、交叉引用、`--reference-doc`），
但本书流水线需要的是"按已确定的层级切章 + 精确的脚注语义 + 页尾注兜底"，
三样都要改它的默认行为，成本高于自己拼 600 行 XML。**如果将来要支持双栏/表格
结构化，应当考虑接 pandoc 而非继续自建。**

## 7. 已知限制

- 双栏、表格、公式不做结构化重建，按行流式输出。插图裁得出、表格聚成块，但不还原列结构。
- **插图裁切依赖 `pages/`**：`prune` 阶段删掉页图后重跑 `md`，图会降级成 table 块（文字不丢，图没了）。
- ~~页顶/页底窄短行规则会丢真正文~~ **已修**（2026-11-16）：删掉位置判据、只拦「非文字碎片」；
  判据与实测见第 5 节。修前僧官丢 61 行/453 汉字，修后回归正文。
- ~~`notes.linked` 比实际弹窗数多 1~~ **已修**：多出的那 1 条正是上面那条规则丢掉的锚点行
  （p278 `光绪帝，数年后"转僧录司正印"①。`），规则修好后自动对齐（165 = 165）。
- 目录页缺失或为图片的扫描件，会退化为按字号猜标题，准确率显著下降。
- 段落跨页时若正文首行无缩进，可能与上一段错误合并或断开。
- OCR 仍有 1% 左右的字级错误（人/入、己/已），引用原文前请对照原书。
- 版面分析（PP-DocLayoutV3）不是 100% 准，作为辅助判据而非真值。
- 脚注锚定率受 OCR 制约，不是算法问题：新书 110/243 个有脚注的页面，正文里
  一个圈号都没被识别出来，这些注只能整章排在末尾。

## 8. 模块拆分计划：`layout.py`

`layout.py` 现在 **2013 行 / 41 个函数**，是全项目唯一的单体大模块。它已经是结构瓶颈——
不是不能跑，而是改任何一个阶段都要先在大脑里维护一整本书的上下文。本轮的静态审阅也点了它。

### 拆法：按流水线阶段切，`layout.py` 只留门面

`build()` 本身就是现成的切分线（第 1897 行起，逐段调用各阶段）。按它切：

| 新模块 | 收哪些函数 | 行数约 |
|---|---|---|
| `layout_text.py` | `elen` `norm` `mark_value` `char_width` 与 `CJK` / `PUNCT_END` / `PAGE_NO` 常量（文本口径，被 repair / disagree 反向依赖） | 80 |
| `layout_heads.py` | `learn_running_heads` `head_band` `clean_page` `detect_toc_pages` `page_regions` `_region_boxes` | 330 |
| `layout_notes.py` | `split_note_head` `extract_notes` `split_note_blocks` `extract_star_notes` `plan_notes` `apply_subs` 与 `MARK_RE` / `STAR_RE` | 300 |
| `layout_figures.py` | `find_figures` `find_tables` `plate_bbox` `extract_figures` `plate_like` `table_blocks` `group_table_text` `_visual_rows` `_table_flags` `crop_figure` `emit_images` | 400 |
| `layout_toc.py` | `parse_toc` `heading_score` `unify_title` `split_marker` `assign_levels` `match_entries` `page_offset` `_looks_entry` `_mk_entry` `_rank` | 450 |
| `layout_assemble.py` | `assemble` `to_markdown` `to_markdown_with_pages` `plan_regions` | 500 |
| `layout.py`（保留） | `load_pages` `metrics` `dedupe_pages` `drop_back_cover` `build`，外加各子模块的 re-export | 200 |

### 三条硬规矩

1. **`layout.py` 对外 API 不变**。`from layout import elen`（`repair.py` / `disagree.py`）、
   `layout.build`（`make_md.py`）、`layout.load_pages`（两个测试文件）都有引用。
   在 `layout.py` 顶部 `from layout_text import elen, norm  # noqa` 转发即可，**调用方一行都不用改**。
2. **不拆 `assemble()` 内部**。它 260 行、十几条位置判据交织，「哪条判据先命中」本身就是
   最关键的信息，硬拆成小函数会把它藏起来。这一轮只挪文件边界，不动函数体。
3. **每步都用哈希验收**。三本书的 `book.md` 是现成的黄金样本：僧官 `0BA1E017…`、
   规训 `E5053BDD…`、黑格尔 `320A586E…`（文件字节口径）。拆完三本全部逐字节不变 = 零回归。
### 分三步走（每步一个提交，各自可回滚）

| 步骤 | 内容 | 验收 |
|---|---|---|
| 1 | 抽 `layout_text.py`（纯叶子、零依赖，且被 repair / disagree 反向引用） | 三本哈希不变 + 全量测试绿 |
| 2 | 抽 `layout_heads.py` / `layout_notes.py` / `layout_figures.py` | 同上 |
| 3 | 抽 `layout_toc.py` / `layout_assemble.py`，`layout.py` 收敛成门面 | 同上 |

### 为什么现在不做

它**不影响任何当前产出**，属于可读性与可维护性投资。而排在它前面的「注号识别率」是
**真实质量问题**（僧官 288 条注只能降级成页尾注）。先把收益大的做完。
另外大面积移动会打断 `git blame` 的连续性——没有明确的重构窗口时，这笔账不划算。
