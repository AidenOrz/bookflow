# 输入 / 输出规范化建议

> 状态：**已落地**（2026-11-18）。实现说明见 [LAYOUT.md](LAYOUT.md)；本文保留论证与实测依据。
> §7 的三个决策：① 搬进 `runs/` —— 提供 `tools/migrate_layout.py`，**默认不搬**；
> ② 交付物出 `dist/` —— tiered 布局已强制，flat 保留兼容；③ 插图编号 —— 仍**延后**。
> 核对时间：2026-11-16。文中每个结论都注明了实测依据（`文件:行号` 或可直接复算的数字）。

---

## 0. 一句话结论

现在的产物**能跑不能用**：同一个"工作目录"里同时躺着可重生成的中间产物、机器报告、
人工校对清单、以及交付给读者的 EPUB；文件名一半是流水线契约、一半是人手起的，
两者混在同一个命名空间里。四本书各自长成不同的样子，没有任何一份文件能回答
"这本 EPUB 是哪次运行的、用什么参数、从哪个 PDF 来的"。

规范化分三件事，**优先级从高到低**：

| # | 做什么 | 解决什么 | 成本 |
|---|---|---|---|
| 1 | **输入落档**：`input/<book-id>/` + `manifest.json`（PDF 的 sha256 + 全阶段参数） | 产物无法溯源、无法复现 | 低 |
| 2 | **输出分家**：工作目录内 `work/ report/ review/`，交付物出到仓库级 `dist/` | 交付物与中间产物混放、人工件与机器件混放 | 中 |
| 3 | **命名收口**：一条规则管所有产物名；页名/图名/交付物名各一套模板 | 图名两套编号并存、文件名靠 title 原样 | 中（`pages/` 改名有回归成本，见 §6） |

---

## 1. 现状：输入侧

### I1. 源 PDF 的"身份"根本没有被记录，只有一条会失效的绝对路径

四本工作目录的 `report.json` 里**没有任何 pdf 字段**（实测：对四份 `report.json` 搜 `"pdf"` 全无命中）。
唯一记了源文件的地方，是**页级 JSON 里的绝对路径**：

| 目录 | 页文件数 | 内嵌 `source_pdf` 的页数 | 样例 |
|---|---|---|---|
| `run-lacan/ocr_text_raw` | 310 | **310** | `E:\\workspace\\bookflow\\.tmp-lacan.pdf` |
| `run-lacan/ocr_text` | 310 | **286** | 同上 |
| `run-sengguan/ocr_text_raw` | 350 | **350** | `C:\\Users\\Lenovo\\Desktop\\Books\\中国僧官制度史….pdf` |
| `run-sengguan/ocr_text` | 0 | 0 | — |

三个问题叠在一起：

1. **同一条信息重复了 310/350 份**，而且两份目录各存一份。改一次元数据要重写几百个文件。
2. **绝对路径已经悬空**：`run-lacan` 那个 `.tmp-lacan.pdf` 实测 `Test-Path` = False。
   它既不能用来复现，也不该被提交（泄露本机目录结构）。
3. **没有指纹**。同名不同版本的 PDF 换进来，所有下游产物看起来仍然"正常"——
   本项目最怕的静默出错。`stage_textlayer` 已经意识到这一点
   （`bookflow.py:652` 比对 `os.path.realpath`），但它比的是**路径**，不是**内容**。

> 规范要求：PDF 身份 = `sha256`，只写一次，写在 `manifest.json` 里（§4.4）。
> 页级 JSON 里的 `source_pdf` 应当删除或改记为 `source_id`（一个短哈希前缀）。

### I2. 参数没有落盘，最贵的目录体积差 10 倍却无从解释

实测 `report.json` 的 `params` 只有两个键：

```json
{"conservative_fix": false, "src": "ocr_layout"}
```

`--dpi` / `--model` / `--device` / `--procs` / `--text-layer` / `--no-layout` **一个都没有**
（四份 `report.json` 搜 `dpi` 全无命中）。后果是可见的：

| 工作目录 | 页数 | `pages/` 合计 | 单页均摊 | 页图尺寸（页级 JSON 的 `w×h`） |
|---|---|---|---|---|
| 根（黑格尔） | 306 | 454.9 MB | **1.49 MB** | 2480 × 3508 |
| `run-sengguan` | 350 | 373.2 MB | **1.07 MB** | 2480 × 3509 |
| `run-lacan` | 310 | 273.5 MB | **0.88 MB** | **2047 × 3246** |
| `run-guixun` | 383 | 53.0 MB | **0.14 MB** | **2083 × 3357** |

**单页体积差 10.6 倍，页图尺寸差 21%**。代码里 `--dpi` 默认 300，但四本的尺寸对不上——
说明实际用的不是同一个 dpi（或原始 PDF 页面尺寸不同）。产物里**没有任何字段能回答这个问题**。
而 `pages/` 合计 **1154.6 MB / 1349 页**（4 个工作目录），是项目里最贵的一类目录，
却恰恰是最没有书面说明的那个。

### I3. 输入元数据三处重复，且 title 同时充当文件名

`--title/--author/--publisher` → `report.json` 的 `book{}` → epub 命令行 → OPF 元数据。
回落链在 `bookflow.py:1164-1174`，逻辑本身没错；问题在 `bookflow.py:1175`：

```python
out = os.path.join(a.work, "out", title + ".epub")
```

**元数据一变，产物路径跟着变**，且没有任何清洗。实测交付物文件名：
`拉康式主体：在语言与享乐之间.epub`（含全角冒号，可以正常存在，但在 zip 解压、
URL 编码、命令行 quoting、老旧文件系统里都是隐患）。

### I4. 工作目录的根同时是 git 仓库的根

仓库根上并排躺着：`pages/` `ocr/` `ocr_alt/` `ocr_fix/` `ocr_merged/` `ocr_medium/` `out/`
（= 黑格尔那本书的工作目录）+ `run-guixun/` `run-lacan/` `run-sengguan/`（另外三本）。
`.gitignore` 因此要写 **18 条**非注释规则，其中 **9 条**专挡"产物目录"（`pages/` `ocr/`
`ocr_fix/` `ocr_alt/` `ocr_medium/` `ocr_merged/` `ocr_layout/` `run*/` `out/`）——
而 `ocr_text/` 与 `ocr_text_raw/` 恰恰**没写**。其中几条还**过度匹配**：

| 规则 | 实际后果（`git check-ignore` 实测） |
|---|---|
| `*.png` | `assets/icon.png`、`docs/n.png`、`tests/fixtures/golden.png` **全部被忽略** —— 将来任何需要入库的图片（图标、文档配图、测试夹具）都进不来 |
| `out/` `pages/` `ocr/` 等裸名 | 不带前导 `/`，匹配**任意层级**。方向正确（`run-x/out/` 也该挡）但过头了：`ocr_layout/` 整目录被挡，连它内部的 `.bak/` 备份（见 O10）也一起消失，`git status` 里完全看不见 |
| `.tmp-*` | 只挡"前缀式"临时名（符合项目约定），但 `jsonio.write` 的临时文件是**后缀式** `xxx.json.tmp`；实测 `book.md.tmp` 未被忽略，只是恰好落在 `out/` 里才被挡 |
| **缺** `ocr_text/` `ocr_text_raw/` | 规则表里根本没有这两条（`git check-ignore ocr_text/p-001.png.json` → **未忽略**）。而 `--text-layer` 的**默认值就是 `verify`**——一旦在仓库根跑一次 `all`（根目录正是黑格尔那本的工作目录），就会凭空多出两个未被忽略的目录，共 1.3 MB 中间产物 + 内嵌的绝对路径（I1），`git add -A` 会照单全收 |

---

## 2. 现状：输出侧

### O1. 交付物、机器报告、人工件全在同一个 `out/`

```
run-lacan/out/
  拉康式主体：在语言与享乐之间.epub   ← 交付物
  images/  cover.jpg                  ← 交付物的素材
  book.md                             ← 交付物的源
  report.json  qa.txt  audit.json  audit.txt  disagree.json  disagree.txt  decisions.json  review.png
                                      ← 8 份报告/中间态（除 decisions.json 外都可重生成）
```

`prune` 阶段**不清理 `out/` 目录**（`bookflow.py:1260-1264` 的 targets 只有 `pages/` 和 `ocr*/`）。
后果：想只拷交付物给读者，必须人工挑；想删中间产物，会连 `book.md` 一起删——而
`book.md` 是重打包 EPUB 的唯一输入（`bookflow.py:1159-1161`）。

### O2. 交付物名字里没有"哪一版"

四本 EPUB：`黑格尔辩证法讲演录.epub`、`规训与惩罚.epub`、`中国僧官制度史.epub`、
`拉康式主体：在语言与享乐之间.epub`。**没有日期、没有来源、没有版本**。
两次运行同名文件直接覆盖，回滚只能靠回忆时间点。

### O3. 插图有两套编号规则，产出到同一个目录

`layout.py` 里 `fig-p%03d-%d.png` 出现在**两个位置**：

| 位置 | 序号含义 |
|---|---|
| `layout.py:765` `extract_figures()` | **页内序号**（这一页第几张图，从 1 起） |
| `layout.py:1059` 列数乱跳的表格块 | **全书累计数**（`report["figures"] + 1`） |

两者都写进 `out/images/`。实测 `run-lacan/out/images/` 里同形并存的证据：

```
p050: fig-p050-1  fig-p050-2  fig-p050-6     ← 只有 3 张，编号却到 6（6 = 全书第 6 张）
p227: fig-p227-82                            ← 全书第 82 张
p247: fig-p247-1  fig-p247-2  fig-p247-83    ← 两套编号在同一页短兵相接
p248: fig-p248-1  fig-p248-2  fig-p248-86  fig-p248-87
p263: fig-p263-104  fig-p263-105             ← 连页内 1/2 都没有
```

（判定口径：编号 > 10 的必然是全书序号。121 个文件里这样的有 **8 页**：
p227、p247、p248、p254、p260、p261、p262、p263；其中 p247/p248/p262/p263 是
**同一页两套序号并存**。p050 的 `-6` 也属同一现象，只是数值小于 10、无法仅凭编号判出，
两套编号在 6 以内时会**静默撞名**。）

于是文件名**既不能按页排序、也不能按出现顺序排序**，序号还不连续。人看图只想知道
"这是哪一页的第几张"，现在得反查 `report.json`。

**更要紧的是它已经进成书了**：`run-lacan/out/book.md` 里 5 处引用用的是全书序号——
第 411 行 `![](images/fig-p050-6.png)`、2987 行 `fig-p227-82.png`、3275 行
`fig-p247-83.png`、3303 行 `fig-p248-86.png`、3634 行 `fig-p263-104.png`。
所以这不是"文件名的审美问题"，是**成书内容的一部分**。

### O4. 孤儿图：重跑不清目录，交付目录里留着上一轮的图

`run-sengguan/out/images/` 磁盘上 8 个文件，而 `report.json` 只登记 4 个、EPUB 里也只打了 4 个：

```
report.json / EPUB 里：fig-p053-1  fig-p143-1  fig-p148-1  fig-p157-1
磁盘上多出来的孤儿：   fig-p135-2  fig-p306-6  fig-p320-7  fig-p321-8
```

时序（`CreationTime` 实测）：4 张旧的建在 11-14，4 张孤儿建在 **11-16 20:33**；
说明上一轮产出过 5 张以上（含 p135 的第 2 张、以及 3 个全书序号的表格图），
这一轮只产出 4 张，**旧文件没有对账、没有删除**。

危害不在占 0.5 MB，而在于：**目录里的文件集 ≠ 报告里的文件集 ≠ 成书里的文件集。**
下一个接手的人（或 agent）按目录清点会得出"这本书有 8 张图"，按报告清点会得出 4 张。
缺的是"一次运行 = 一个干净目录"的约束。

### O5. 阶段目录没有"完成"标记，meta 与实际产物对不上

| 目录 | `.meta` 说什么 | 目录里实际有什么 | 一致吗 |
|---|---|---|---|
| `run-sengguan/ocr_text` | `usable=false`、`kept_pages=0`（"每页平均只有 10.0 个汉字，文字层不可用"） | **0 个页 JSON**，只剩 `_textlayer.meta` | ✅ |
| `run-lacan/ocr_text` | `usable=true`、`kept_pages=286`、summary `with_text=301` | **310 个页 JSON** | ❌ 比 `kept_pages` 多 24 |

`run-sengguan` 那一格是对的（文字层不可用 → 一页都没筛出来），但它同样危险：
**一个只含 meta 的空目录，与"这一步还没跑"在文件系统上长得一模一样。**
任何按"目录存在即已完成"来判断的人（包括下一个 agent）都会踩空。

`run-lacan` 那一格则是真的对不上：`kept_pages=286`，磁盘上 310 个页 JSON。
这 24 页来自后续的 `complete_textlayer`（用 OCR 补齐缺页，`bookflow.py:316-358`）——
它**只写页文件，不更新 meta 的 `kept_pages`**，于是 meta 变成一个会撒谎的快照。
（顺带：summary 里的 `with_text=301` 又是第三个数，meta 内部三个数字互相都对不上。）

**这正是本项目写在 docs 里的头号风险——静默出错。**

### O6. 报告全部平铺，用后缀而不是名字区分内容

`audit.txt` / `audit.json` 靠扩展名区分"给人看的"和"给程序读的"；同类还有
`disagree.txt` / `disagree.json`。`run-lacan/out/` 里**10 份非交付文件**（book.md、
report.json、qa.txt、audit.json、audit.txt、disagree.json、disagree.txt、decisions.json、
cover.jpg、review.png）**全部平铺在同一层**，没有任何分组；其中 `decisions.json` 是唯一
不该被重建的，却和可重建的报告并排。

### O7. 同一件事（校对清单）有两种名字、两种格式

脚本产出 `audit.txt`（固定格式、带 PDF 页码），人手写的叫 `校对清单.md`
（`run-guixun/out/校对清单.md`，4 KB，**不是任何脚本生成的**，内容写死"共 20 处疑点，
其中 9 处必然要改"）。前者随代码演进，后者是 2026-11-12 的一次性快照，
现在两者并列躺在同一个 `out/` 里，**没有任何标记说明哪个可信**。

### O8. 产物里写的是相对当前工作目录的路径

`report.json` 的 `images.dir` 实测值：

| 工作目录 | 记录值 | 换个 cwd 打开还成立吗 |
|---|---|---|
| 根 | `out\\images` | 否（要恰好在仓库根） |
| `run-lacan` | `run-lacan\\out\\images` | 否（要把 cwd 设成仓库根） |

同一份 `report.json`，在 `run-lacan/` 里跑和在仓库根跑，记录的值不一样，两者都只在
特定 cwd 下有效。规范上应当**只记相对工作目录根的路径**。

### O9. 中间态目录名与"版本"没有关系

`ocr` `ocr_fix` `ocr_layout` `ocr_merged` `ocr_text` `ocr_text_raw` `ocr_alt` `ocr_medium`
—— 8 个目录名，谁先谁后靠 `bookflow.py:918` 的 `OCR_DIRS` 元组定义，而这个顺序
**有实测踩坑史**（`ocr_layout` 静默反压 `ocr_merged`，见 docs/MEMORY.md 第 9 条）。
新增一档就得改元组、改 `prune` 的 targets、改 `.gitignore`、改文档——四处联动。

### O10. 备份目录藏在被备份的目录**内部**

`apply_decisions.backup_dir_for()`（`apply_decisions.py:270-279`）算出来的路径是：

```
<src>/.bak/<YYYYmmdd-HHMMSS>-<decisions 文件名>/
例如：ocr_layout/.bak/20261116-120042-decisions.json/p-001.png.json
```

> 说明：四个工作目录里目前**没有** `.bak/`（`Get-ChildItem -Recurse -Directory -Filter '.bak'`
> 与 `-Recurse -Filter '.bak'` 均无命中）——最近几轮的回填没走带备份的路径。
> 所以这是**代码必然产生的形态**，不是已发生的事实，这里如实标注。

问题在于它把备份写进**被备份的源目录内部**：

- `ocr_dir()` 的 `glob('*.json')` 只看顶层，暂时不会读错；
- 但 `_dir_freshness()` 同样只扫顶层，**备份的存在完全不体现在任何统计里**，
  而它会让 `ocr_layout/` 的目录体积悄悄变化；
- 更要紧的是：`ocr_layout/` 是"可直接喂给下一阶段的数据目录"，往里塞非页文件
  （哪怕在子目录里），就是把**数据目录**和**运行态**混成一个。`.gitignore` 里那条
  `ocr_layout/` 恰好把 `.bak/` 一起挡住了（`git check-ignore` 命中），
  但那是"顺带"，不是设计——一旦把备份挪到 `ocr_merged/`（同样是裸名规则）以外的
  位置，或将来放宽某条规则，备份就会突然出现在 `git status` 里。

---

## 3. 规范提案：目录与生命周期

```
bookflow/                          ← 仓库（只有代码/文档/测试入库）
├─ input/                          ← 【输入】人工放，只读
│  └─ <book-id>/
│     ├─ source.pdf                ← 有版权，不入库
│     └─ meta.json                 ← 书名/作者/出版社/可选 isbn，入库
├─ runs/                           ← 【工作目录】整体可删，可重生成，不入库
│  └─ <book-id>/
│     ├─ manifest.json             ← 本次运行的唯一事实源（见 §4.4）
│     ├─ work/                     ← 中间产物（可重生成，随时可删）
│     │  ├─ pages/                 ← 300dpi 页图，单页 ~0.5 MB
│     │  ├─ ocr/ ocr_fix/ ocr_layout/ ocr_merged/ ocr_text/ ocr_text_raw/   ← 阶段 1 先不改名
│     │  └─ .backup/<stage>/<ts>/  ← 备份从源目录里搬出来
│     ├─ report/                   ← 机器报告（脚本产出、可重生成、给人看）
│     │  ├─ book.md  qa.txt  report.json
│     │  ├─ audit.txt  audit.json  disagree.txt  disagree.json  review.png
│     │  └─ toc.json（可选）
│     └─ review/                   ← 【人工件】脚本只读不写
│        ├─ decisions.json         ← 人工裁决表（有状态，不能自动重建）
│        └─ notes.md               ← 人的笔记（原 `校对清单.md`，见 O7）
└─ dist/                           ← 【交付物】只含最终文件，不入库
   └─ <book-id>/
      ├─ <book-id>__<slug>__<YYYYMMDD>.epub
      └─ <book-id>__<slug>__<YYYYMMDD>.notes.md   ← 校对清单/已知问题
```

**四类产物的边界（这是本规范最重要的一条）**：

| 类别 | 位置 | 谁能删 | 谁写 |
|---|---|---|---|
| 交付物 | `dist/` | 只有人 | 只有 `epub` 阶段 |
| 机器报告 | `<work>/report/` | 任何人，删了重跑即可 | 各阶段脚本 |
| 人工件 | `<work>/review/` | **谁都不能碰** | **只有人**（脚本只读） |
| 中间产物 | `<work>/work/` | `prune` | 各阶段脚本 |

现状违反这四条的地方：`out/` 里四类混放（见 §2 的 O1 / O6 / O7），`decisions.json` 与
`disagree.json`（一个是人工裁决、一个是脚本产物）躺在同一层，无法区分"删了会丢成果"的东西。

### 3.1 为什么不是"一步到位改名 ocr → 10-ocr"

阶段顺序目前只存在于一处代码（`OCR_DIRS`），但也同时被 `prune`、`.gitignore`、
文档、以及**用户已有的 4 个工作目录**引用。改名会让这四个目录直接读不出来。
所以 §5 把它排在最后一步，且**默认不做**。

---

## 4. 规范提案：命名

### 4.1 一条通用规则

> **产物文件名只允许 `[a-z0-9._-]`，全小写，不含空格/中文/全角标点/大写。**
> 三段式：`<kind>-<scope>-<seq>.<ext>`，缺省段可省，但**段序不允许调换**。

理由（都是本项目真实踩过的）：

- 大写：Windows 与 git 都 `core.ignorecase=true`（实测 `git config`），
  `Fig.png` 与 `fig.png` 是同一个文件，改名/合并会静默丢文件。
- 空格：`bookflow.py` 的日志会把命令原样回显（`"  $ " + " ".join(cmd)`），
  带空格的路径回显出来后**不能直接复制粘贴复跑**。
- 中文/全角：`stdio.utf8_stdio()` 正是为这类崩溃而存在的（docs/MEMORY.md 的坑），
  能不产生就不产生。

### 4.2 各产物命名模板

| 产物 | 模板 | 示例 | 现状 |
|---|---|---|---|
| 页图 | `p-<4位>.<ext>` | `p-0007.png` | `p-007.png`（3 位，pdftoppm 决定） |
| 页级 JSON | `p-<4位>.<ext>.json` | `p-0007.png.json` | 同名 + `.json`（**保留双后缀，见 §6**） |
| 插图/表格裁图 | `fig-p<4位>-<2位>.<ext>` | `fig-p0143-02.png` | `fig-p143-1.png`，且**两套编号**（O3） |
| 阶段元数据 | `_<stage>.meta` | `_textlayer.meta` | 一致 ✅ |
| 机器报告 | `<stage>.<ext>` | `report.json` `audit.txt` | 一致 ✅ |
| 人工裁决 | `decisions.json` | — | 一致 ✅（但位置要挪） |
| 备份 | `.backup/<stage>/<YYYYmmddTHHMMSS>/` | `.backup/ocr_layout/20261116T204257/` | 在源目录内（O10） |
| 交付物 | `<book-id>__<slug>__<YYYYMMDD>.epub` | `lacan__lacan-subject__20261116.epub` | `title + ".epub"`（O2） |

**交付物名的三段各有硬用途**，不能删：

- `<book-id>`：同一本书多个版本的公共前缀，排序即分组。**人工指定**，一旦定下不改。
- `<slug>`：给人看的可读名。**中文书名不转拼音**——拼音转换要引入新依赖（违反项目
  零第三方依赖的约束），而且 `章太炎` → `zhang-taiyan` 对中文读者毫无可读性。
  做法是：人工在 `input/<book-id>/meta.json` 里给 `slug`（可留空，留空时用 `book-id`）；
  真正的书名/作者始终住在 EPUB 的 OPF 元数据和 `meta.json` 里，**不靠文件名传达**。
  这就是 `lacan` 那本书叫 `lacan__lacan-subject__20261116.epub` 而不是
  `拉康式主体：在语言与享乐之间.epub` 的原因。
- `<YYYYMMDD>`：版本。ISO 基本格式，**排序即时间序**，不用空格/冒号。

### 4.3 序号规范（专治 O3）

> **序号只表达"这一页的第几张"，从 01 起连续，不跨页累计。**

来源（region / plate / 列数乱跳的 table）**不进文件名**，写进 `report.json`：

```json
{"name": "fig-p0143-02.png", "page": 143, "kind": "region", "bbox": [..]}
```

这样文件名可以按 `(page, seq)` 直接排序、可以直接在成书里对齐原文。
代价是要改 `layout.py:1059` 那一处的编号来源——**这一处必须改，因为它已经在产
`fig-p050-6.png` 这种"页内只有 2 张图却编号到 6"的名字了**。

### 4.4 `manifest.json`（本次运行的事实源）

`report.json` 是"这本书长什么样"，`manifest.json` 是"这次是怎么跑出来的"。两者不能合并：
前者会被 `md` 阶段整体重写，后者要跨阶段累加。

```json
{
  "schema": "bookflow.manifest", "schema_version": 1,
  "book_id": "lacan",
  "input": {
    "pdf": "input/lacan/source.pdf",
    "sha256": "9f2c…",
    "pages": 310,
    "title": "拉康式主体", "author": "…", "publisher": "…"
  },
  "params": {"dpi": 300, "procs": 5, "device": "dml", "model": "small",
             "text_layer": "verify", "no_layout": false, "conservative_fix": false},
  "stages": {
    "render":    {"status": "ok",      "at": "2026-11-16T20:41:03", "out": "work/pages",    "count": 310},
    "ocr":       {"status": "ok",      "at": "2026-11-16T20:43:19", "out": "work/ocr",      "count": 310},
    "textlayer": {"status": "degraded","at": "2026-11-16T20:44:02", "out": "work/ocr_text", "count": 286,
                  "reason": "9 页无文字层，已用 OCR 补齐"},
    "epub":      {"status": "ok",      "at": "2026-11-16T21:10:44", "out": "dist/lacan/lacan__lacan-subject__20261116.epub"}
  }
}
```

**三条硬约束**（与 `pagedoc.py` 现有的纪律一致）：

1. `input.sha256` 是**唯一**的输入身份。换了 PDF 必须能被检测到——现在
   `stage_textlayer` 只比 `realpath`（`bookflow.py:652`，路径相同 ≠ 内容相同），
   而且只写在被 `prune` 删掉的 `.meta` 和几百份页 JSON 里（I1）。
2. `stages[*].status` 取 `ok | degraded | failed | skipped`。**没有这条，
   目录存在 ≠ 阶段跑完**（O5 就是这么发生的）。
3. 路径一律**相对工作目录根**、正斜杠。禁止绝对路径（I1 的悬空路径就是这么来的）。

### 4.5 编码与换行

- 所有文本产物 UTF-8 无 BOM（现状已是）。
- `book.md` 在 Windows 上以 `\r\n` 落盘，而黄金哈希记的是**文件字节**口径
  （docs/MEMORY.md 已明确）。规范里必须写死：**产物统一 `\r\n`（平台默认），
  哈希一律用 `Get-FileHash` 的文件字节口径**，避免"内存字符串哈希"这个假比对。
- JSON 一律 `ensure_ascii=False, indent=1`（现状已是，`make_md.py:76`）。

---

## 5. 迁移路线

**每一步都可独立回滚、都带可执行验收。**

### 阶段 1（P0，不动任何文件名，风险最低）

1. 新增 `<work>/manifest.json`：`render` 阶段写 `input{sha256,pages}`，
   各阶段补 `params` 与 `stages[stage].status`。
2. 交付物改出到 `dist/<book-id>/<book-id>__<slug>__<YYYYMMDD>.epub`；
   `out/` 位置不变（保留兼容）。
3. 补 `.gitignore`：`ocr_text/` `ocr_text_raw/` `dist/` `runs/` `input/**/source.*`；
   把 `*.png` 收窄成 `runs/**/*.png` `pages/**/*.png`。
4. 备份目录从 `<src>/.bak/` 改到 `<work>/.backup/<stage>/<ts>/`（O10）。

**验收**（全部可复算，2026-11-16 实测通过）：

```powershell
python -m unittest discover -s tests          # 2026-11-16 实测 270 tests / OK (skipped=3)；11-18 加表功能后 298

Get-FileHash out\book.md              -Algorithm SHA256   # 320A586E…
Get-FileHash run-guixun\out\book.md   -Algorithm SHA256   # E5053BDD…
Get-FileHash run-sengguan\out\book.md -Algorithm SHA256   # 0BA1E017…
Get-FileHash run-lacan\out\book.md    -Algorithm SHA256   # 457413BA…（第四本，此前无基线）
```

> 阶段 1 **不碰 `book.md` 的内容，也不碰任何中间文件名**，所以这四条哈希必须
> 逐字节不变。这是"规范只是搬家、没有副作用"的唯一证明。

### 阶段 2（P1，命名收口）

1. 插图编号统一到"页内序号"（§4.3），改 `layout.py:1059` 一处。
   **只有带插图的会变**（实测 `report.json` 的 `regions.figures`）：`run-lacan` 121 张
   （5 处引用会从 `fig-p050-6.png` 变成 `fig-p050-03.png` 这类），`run-sengguan` 4 张
   （编号本来就是页内的，预计不变）。黑格尔与规训都是 `figures=0`，`book.md` **不受影响**。
   变的那本必须重新取基线并更新 docs/MEMORY.md 的哈希表。
2. `out/` 拆成 `report/` 与 `review/`；`decisions.json` 移入 `review/`。
3. 报告内路径一律相对工作目录根（O8）。

**验收**：新增 `tools/check_layout.py`，按本规范校验一个工作目录
（文件名正则、目录归属、manifest 完整性、孤儿产物），退出码非 0 即不合规。
先在四个现有目录上跑，**报出的每一条差异都要能对上本文的一节编号**——
对不上的说明规范漏了一条，先补文档再补代码。

### 阶段 3（P2，可选，默认不做）

`ocr` → `10-ocr` 之类的阶段序号前缀。收益是可读性，成本是 §3.1 说的四处联动。
**建议：只有在"再引入第三个 OCR 版本"时才做。**

---

## 6. 明确不建议做的三件事

### 6.1 不要把 `p-001.png.json` 改成 `p-0001.json`

看着更干净，但 `.png.json` 这个**双后缀是契约，不是笔误**：

- `_page_jsons()`（`bookflow.py:945-947`）正是靠 `endswith(".png.json")` 精确过滤页文件，
  从而让同目录的 `_textlayer.meta`（不带 `.json`）不被当成一页；
- 去掉 `.png` 就丢掉了"这份 JSON 对应哪张页图"的信息，`run_layout.py` 找页图
  要重新拼名字，而这正是历史上 17 页小册子全部"跳过（缺页面图）"的那个坑
  （`bookflow.py:1060-1080` 的告警就是为它写的）。

代价也不小（实测统计，单行命令见 §8）：页名相关字面量**源码 5 个文件 21 处 +
测试 5 个文件 25 处**；再叠加 `pages/` 改名，四条黄金哈希会全变（页名进了
`decisions.json` 的主键 `page@y0`，也进了 `report.json` 的 `page_map`）。
**收益是好看，成本是重写契约。**

### 6.2 不要靠改 `pages/` 的后缀去"修" pdftoppm 不补零

`pdftoppm` 按页数决定补零位数：17 页的书出 `p-01.png`，300 页的书出 `p-001.png`。
为了对齐，`bookflow.py` 现在写了 **3 个归一函数** + **2 处告警**（`page_image_index` /
`_norm_page_name` / `_target_page_name`，`bookflow.py:224-268` + `1060-1080`）。

正解不是继续加兼容，而是 **`render` 阶段渲染完就地重命名成 `p-0001.png`**（4 位定长，
位宽不再由页数决定），让下游只需要一种名字。但这会动到 `decisions.json` 的主键
（`page@y0`，含页名）、以及**四个现有工作目录的全部产物**——所以排在阶段 2，且必须
连同 `decisions.page_path()` 的补零容错一起评估（`decisions.py:188-207` 现在容忍
`p-15` / `p-015` / `p-00015` 三种写法，正是这个坑的化石）。

实测现状是四本**恰好都是 3 位**（`p-001.png`～`p-383.png`），所以今天没出问题；
但这不是保证，只是这四本都在 100~999 页之间。

### 6.3 不要引入 YAML/TOML 配置

与 docs/IMPROVEMENT-PLAN.md §5.3 的结论一致：**阈值不进配置文件**。
本规范只增加 `manifest.json`（纯 JSON、只记录不配置），不改变任何阈值的存放位置。

---

## 7. 需要拍板的三个决策

| # | 决策 | 选项 | 建议 |
|---|---|---|---|
| 1 | 仓库根那本书（黑格尔，`pages/ ocr*/ out/` 直接躺在仓库根）是否搬到 `runs/hegel/` | A 搬（之后仓库根只剩代码/文档/input/runs/dist）<br>B 不搬（保留现状，规范只管新书） | **A**。这是"仓库根同时是工作目录"（I4）的根源，不搬掉，规范就永远有两个例外 |
| 2 | 交付物是否必须出到 `dist/` | A 必须（out 只放中间产物）<br>B 可选（保留 `out/*.epub` 兼容） | **A**，但阶段 1 里两者并存一个版本周期，避免打断已有脚本 |
| 3 | 插图编号改成页内序号（会让 `run-lacan` 的哈希失效，其余三本不受影响） | A 现在就改（重取基线）<br>B 下一个重构窗口再改 | **B**。O3 的影响面是"文件名不好读"，不是"内容出错"；重取基线要重新验证 121 张图的裁切与 EPUB 内容 |

> 这三条确定后，本文的 §5 阶段 1 可以直接照做；阶段 2/3 建议各开一个单独提交，
> 按 docs/MEMORY.md 的纪律：**一个可验证的完整单元一个提交**。

---

## 8. 附：本文数字的复算方式

本文不写"估计"。每条结论都能用下面这些命令复算（PowerShell，仓库根执行）。

```powershell
# 目录体积与页数
foreach ($d in 'pages','run-lacan/pages','run-guixun/pages','run-sengguan/pages') {
  $f = Get-ChildItem $d -File
  "{0,-22} {1,4} 页 {2,8:N1} MB" -f $d, $f.Count, (($f | Measure-Object Length -Sum).Sum/1MB)
}

# 页图尺寸（顺带确认不是同一个 dpi）
python -c "from PIL import Image; print(Image.open('run-lacan/pages/p-001.png').size)"

# report.json 里到底记了什么参数
python -c "import json; print(json.load(open('run-lacan/out/report.json'))['params'])"

# 图片三处对账：目录 / report / 成书
python -c "import json; r=json.load(open('run-sengguan/out/report.json'))['layout']['regions']['images']; print(r['made'], r['files'])"
Get-ChildItem 'run-sengguan/out/images' -File | Select-Object -ExpandProperty Name

# 页级 JSON 里内嵌的绝对路径
python -c "import json; print(json.load(open('run-lacan/ocr_text_raw/p-001.png.json'))['source_pdf'])"

# 页名耦合面（§6.1 的 5 文件 21 处 / 5 文件 25 处 就是这么数出来的）
python -c "import glob,re; p=re.compile(r'p-%03d|p-\*\.png\.json|\.png\.json'); h=lambda fs:(len([1 for q in fs if '__pycache__' not in q and p.search(open(q,encoding='utf-8',errors='replace').read())]), sum(len(p.findall(open(q,encoding='utf-8',errors='replace').read())) for q in fs if '__pycache__' not in q)); print('src files/matches', h(glob.glob('*.py'))); print('test files/matches', h(glob.glob('tests/*.py')))"

# 黄金哈希（文件字节口径，不是内存字符串）
Get-FileHash out\book.md, run-guixun\out\book.md, run-sengguan\out\book.md, run-lacan\out\book.md -Algorithm SHA256
```

**核对基线（2026-11-16 实测，供后续比对）**：

| 项 | 值 |
|---|---|
| 单元测试 | 270 个，`OK (skipped=3)`（2026-11-16；11-18 加 `make_table` 后为 **298 个**） |
| `book.md` 文件字节 SHA256 前缀 | 黑格尔 `320A586E` / 规训 `E5053BDD` / 僧官 `0BA1E017` / 拉康 `457413BA` |
| `pages/` 总占用 | 4 本 1349 页 / 1154.6 MB（单个最贵：根 `pages/` 454.9 MB，306 页） |
| `*.epub` 交付物 | 4 个，全部命名为 `<title>.epub`，无版本段 |
| 现有页名位宽 | 四本都是 3 位（`p-001` ～ `p-383`），因为页数都在 100~999 之间（§6.2） |

---

## 9. 人工检查并修改之后，怎么合并回文件

> 本节全部是实测结论（2026-11-17，`run-guixun` 9 页样本 + 全流程复现），不是推演。

### 9.1 先分清三类「人工修改」，回路完全不同

| 你改了什么 | 有没有回路 | 谁会在下一次把它冲掉 | 结论 |
|---|---|---|---|
| 页级行文本（`ocr_*/p-xxx.png.json`） | 有，但很脆 | `layout --force` 会从别的源重建 `ocr_layout` | **不要手改页 JSON** |
| `decisions.json` 的 `verdict` | **有，官方回路** | 人工件，不会被覆盖 | 走 §9.2 |
| `book.md` 的正文 | **没有** | `md` 阶段 100% 重写 | 手改必丢，见下 |

实测（同一套输入，三种走法，看 `md` 之后的成书里还剩几个错字）：

| 走法 | 成书里的错字 | 结论 |
|---|---|---|
| 不回填，直接出书（基线） | 介人 2、归人 1、中士 1、既使 2、人狱 1、加人 1 = **8 处**（6 类） | — |
| 回填 `ocr_merged`，**只重跑 `md`** | **与基线完全相同，8 处一个没少** | 裁决被静默丢弃（docs/MEMORY.md 第 9 条） |
| 回填 `ocr_merged`，**再 `layout --force`**，再 `md` | **0 处** | ✅ 唯一有效路径 |

`book.md` 手改的实测：把「见勒」改成「贝勒」并追加一行注释，重跑 `md` 后**两个改动全部消失**
（`见勒` 回到 2 次，注释不见）。只有**跳过 `md`、单独跑 `bookflow.py epub`** 时手改能保留——
因为 `epub` 只读 `book.md` 不写它（实测 rc=0，手改在）。
这正好解释了 O2 的危害：没有版本段，你没法知道手上这本 EPUB 是「手改过的那次」还是「重跑后的那次」。

### 9.2 官方回路：七步，已逐步验证

```powershell
# 1. 候选 -> 裁决表（只做格式归一，不做裁决）
python bookflow.py decisions --work <work> --from disagree    # 或 audit / queue

# 2. 人工编辑裁决表的 verdict 字段（语义见 9.3）
#    现状路径是 <work>/out/decisions.json；按 §3 应挪到 <work>/review/decisions.json
#    这是整条流程里唯一「只能人做」的环节，也是唯一不可重生成的产物

# 3. 空跑：只打印计划，一个字节都不写
python apply_decisions.py --work <work> --src ocr_merged

# 4. 落盘 + 自动备份 + 回写 status
python apply_decisions.py --work <work> --src ocr_merged --apply

# 5. 关键一步：重建 ocr_layout（不加 --force 会把 383 页全部跳过，等于没跑）
python bookflow.py layout --work <work> --force

# 6. 重新出书
python bookflow.py md --work <work>

# 7. 重打包
python bookflow.py epub --work <work>
```

**第 5 步为什么不能省**：`md` 读的是 `OCR_DIRS = ocr_layout > ocr_merged > ocr_text > ocr_fix > ocr`
里的第一个存在目录（`bookflow.py:918`）。`ocr_layout` 一旦存在，`ocr_merged` 里的回填**永远轮不到**。
此时只会打印一行告警（`bookflow.py:1026`「ocr_layout 比 ocr_merged 旧……」），然后照旧用旧数据出书——
告警不改行为，这正是「静默出错」的典型形态。

**第 3/4 步的 `--src` 为什么必须点名**：`DEFAULT_SRC = "ocr_merged"`（`apply_decisions.py:48`），
但四本工作目录里只有仓库根那本有 `ocr_merged`。不写 `--src` 时 `run-lacan`、`run-guixun`
直接报「找不到 OCR 目录」（实测 exit 1）——报错还算好事，更糟的是下面这条。

**选错回填目标同样会丢**：把裁决回填到 `ocr_fix`，而 `layout --force` 的源是
`OCR_DIRS[1:]` 里第一个存在的目录。实测（`ocr_layout` + `ocr_text` 都在、`ocr_merged` 不在时）：

```
回填 6 处 -> ocr_fix            （applied 6, missing 0，看起来完全成功）
layout --force 布局源改用 ocr_text  <- 读的是没改过的那份
md 之后：介人 2、中士 1、既使 2   <- 6 处成果全丢，退出码全程为 0
```

所以规则只有一句：**回填目标必须是 `layout --force` 会选中的那个源**。
默认就是 `ocr_merged`——它是专门为回填存在的目录，排在 `ocr_layout` 之后、其余之前。

### 9.3 `verdict` 四种取值的准确语义

| verdict | 含义 | 落盘行为 |
|---|---|---|
| `A` | 保留 A 侧（原样） | **不改**，只登记 |
| `B` | 采用 B 侧 | **整行覆盖**成 `B`，不是替换差异段 |
| `C` | 两侧都不对 | 用 `replacement` **整行覆盖** |
| `?` | 还没裁决 | 不改 |

`B` 是**整行覆盖**——这是修掉旧版「多差异段静默跳过」的方式（`apply_decisions.py` 文件头）。
所以手写 `B` 时，`B` 必须是**完整的一整行**，不能只填改动的几个字。

**改判就改那一条，不要新增**：`decisions.validate()` 会拒绝 `key` 重复。实测新增一条同 key 的
`verdict=C` 后，整份裁决表被拒：「裁决表不合法（宁可报错也不误改）：decisions[10] key 重复：p-343.png@2423.2」。
这是有意的设计——宁可停下让人看，也不静默跳过一条裁决。

### 9.4 三种必须人工补的条目

1. **跨行词**。词被行长切断时 audit 定位不到，条目落成 `verdict=?` + `y0=null`。
   实测 p-272（「介」在行尾、「人」在下一行开头）与 p-287（「人」/「狱」）都是这种。
   补法：找到**含错字的那一行**，`A` 写该行完整原文、`B` 写改好的完整行，
   再把 `y0` 填上、`key` 改成 `page@y0`。实测按此法补的两条回填成功。
2. **误报**。`audit` 的判据是「原词不在词表 + 形近替换后是高频词」，碰专名和跨词必然误报
   （实测 p-340「见勒→贝勒」其实是「见勒贝伊」，跨词）。改成 `verdict=A` 并在 `note` 写清理由，
   **不要删条目**——删了下次 `decisions` 重新生成又会出现。
3. **两侧都不对**。`verdict=C` + `replacement` 写整行（实测 p-343「人将加人」两侧都不对，按图给整行）。

### 9.5 三个必须知道的副作用

1. **备份活不过一次清理**。备份落在 `<src>/.bak/<YYYYmmdd-HHMMSS>-decisions.json/`，
   而 `prune` 会把 `ocr_merged` 整个删掉、连 `.bak` 一起（实测：prune 后两个目录都不存在了）。
   这是 O10 的加重版，也是 §3 里要把备份挪到 `work/.backup/` 的直接理由。
2. **`decisions.json` 是唯一不能重建的产物，但它足够自足**。实测：`prune` 删掉 `ocr_merged` 之后，
   从 `ocr_fix` 重建该目录、再跑一次 `--apply`，得到 `applied 8 / already 1 / missing 0`，
   成果一字不差地回来了。**只要这份 JSON 还在，中间产物可以全删**——这是 §3 四类产物边界的经济学依据。
3. **回填不留痕**。`apply_decisions` 只改 `lines[].t`（`apply_decisions.py:300`），
   不动 `producer`、不写任何标记字段。实测回填后 p-213 的 `producer` 仍是 `None`。
   也就是说「这一行被人工改过」在下游数据里**查不出来**，只能靠 `decisions.json` 的 `status` 反推。
   **建议**（并入 §4.4）：回填时给该行加 `edited: "decision:<key>"`，或在页级 JSON 记 `edits` 数组。

### 9.6 这条回路目前的真实使用率

`run-lacan/out/decisions.json` 有 2825 条，其中 **2380 条是 `?`**（未裁决），`A` 214、`B` 231，
`status` 全部 `pending`——说明这本书的裁决表只由脚本生成过，**人还没真正裁过**。
仓库根那本相反：20 条，`B` 8 / `A` 8 / `?` 4，是历史上人工裁过的那批（`--export-legacy` 迁来的）。
换句话说，回路是通的，但**默认状态下它不产生任何效果**——§9.2 的第 2 步不做，后面六步都是空转。

---

## 10. 实现：把人要碰的表面从 JSON 压到一张表

> 本节记录 **2026-11-18 已落地的改动**（不再是建议稿）。代码：`make_table.py`（新增）、
> `decisions.py`（跨行定位）、`apply_decisions.py`（verdict=C 放宽）、`bookflow.py`（table 阶段）。

### 10.1 为什么不让人改 JSON

`decisions.json` 是给机器读的协议。人改它有三个硬伤（都能实测复现）：

| 硬伤 | 实测数据 |
|---|---|
| **量大** | `run-lacan` 2825 条，其中 **1325 条含多个差异段**；多段时手写 `B` 极易漏字 |
| **语义反直觉** | `verdict=B` 要求 `B` 是**整行**（不是差异片段），写少一个字就整行覆盖错 |
| **一错全废** | `key` 重复 / `C` 缺 `replacement` / `status` 非法 → `validate()` 拒绝**整份**裁决表 |

所以加了一层投影：**人只填两列，其余列是只读的**。

### 10.2 表长什么样

```powershell
python bookflow.py table --work run-lacan          # -> out/decisions.md
# ... 用任何编辑器改「判定」列 ...
python bookflow.py table --work run-lacan --import # -> 合回 out/decisions.json
```

```markdown
### 组 已→己 · 60 条

整组判定：B

| 键 | 页 | 上下文（【】标出差异） | 建议 | 行 y0 | 判定 | 替换整行（仅 C 用） |
|---|---|---|---|---|---|---|
| `p-021.png@1436.3` | p-021 | 他者的欲望所采取的立场，对象也首先被视为像自【已】一样的小他者 | 已→己 | 1436.3 |  |  |
```

四条设计约束，每条都由数据决定：

1. **回读只认「行 y0」「判定」「替换整行」三列**。原文永远从 `decisions.json` 取，
   所以上下文那列可以随便截断（默认 64 字）、随便加【】高亮、随便重排版，**永远不会污染数据**。
2. **按词对分组**，组头一行「整组判定」覆盖组内没单独填的行。
   实测 `run-lacan` 的 999 条「需看字」条目压成 **625 组**，前 12 组覆盖三分之一。
3. **纯标点/空白的差异进附表**。实测占 `run-lacan` 的 **1826/2825 = 64.7%**，
   把噪声混进主表，人要在两千多行里找那 999 行。
4. **`|` 必须转义**。实测 `run-lacan` 有 1 条行文本含 `|`，不转义会把表格切错列。

### 10.3 「定位不到」单独成节并排在最前

`audit` 定位不到的条目（`y0=null`、没有 A/B）**最需要人**，却最容易被漏掉：
如果按「没有差异就不渲染」过滤，它们会从表里彻底消失——那正是本项目的头号风险。

所以它们单独成一节、**不受 `--only` 影响、永远排在最前**，并且：

- 「建议」列从 `note` 里抠出词对（`介人→介入`）；
- 「上下文」列改成显示**为什么定位不到**（这两列本来就不参与回读，可以承载别的含义）；
- 「行 y0」列留空即可，或者填上——填了会把 `key` 从 `page@?#tag` **重算成 `page@y0`**。

### 10.4 顺带修掉的两个真问题

**(1) 跨行词现在能自动定位了。** `audit` 找的是行内子串，词被行长切断时必然找不到。
实测 `run-guixun` 的 20 条命中里有 2 条是这种。`decisions.find_cross_line()` 按
「前半截在行尾 + 后半截在行首」查找，并且**分辨错在哪一半**：

| 实测页 | 形态 | 该改哪一行 |
|---|---|---|
| p-287 | `…刚刚人` + `狱还是…` | **本行**（错字是前半截「人」，应改「入」） |
| p-272 | `…言语介` + `人可怕…` | **下一行**（「介」本来就对，错的是后半截） |

这两者不能搞反：行级裁决是整行覆盖，改错行等于把两行内容互换。
多个候选时**不给建议**（`return None`）——跨行是推断，猜错比不给更糟。

**(2) `verdict=C` 且 `A` 为空时现在会真正落盘。** 原先 `plan()` 要求
「当前行 == A」才敢改，而定位不到的条目 `A` 是空的，于是人补完 `y0` + 整行之后
**仍然回填不上**（实测 `missing 2`，退出码 0，看起来像成功）。
现在对 `C` 放宽：人看着原书给的整行本身就是权威。
`A/B` 不放宽——它们靠 `A` 做「别改错行」的校验。

### 10.5 完整回路（已逐步实测）

```powershell
python bookflow.py decisions --work <work> --from audit   # 1. 候选 -> 裁决表
python bookflow.py table     --work <work>                # 2. 渲染表格
# 3. 编辑 out/decisions.md 的「判定」列
python bookflow.py table     --work <work> --import       # 4. 合回 JSON（会校验，失败整份不写）
python apply_decisions.py    --work <work> --src ocr_merged --apply   # 5. 回填（默认 dry-run）
python bookflow.py layout    --work <work> --force        # 6. 必须：否则 md 读不到回填成果
python bookflow.py md        --work <work>                # 7. 出书
python bookflow.py epub      --work <work>                # 8. 重打包
```

实测一次完整的「填表 → 成书」（`run-guixun` 5 条样本）：组头填 `B` →
`applied 4 / already 1 / missing 0` → 成书里 `介人` 由 3 处降到 1 处、`人狱`/见勒 各消一处。

### 10.6 表格的边界（它**不**解决什么）

- 它不改 `A/B` 原文——那两列是投影。要改原文只能改 `decisions.json`（且应该重跑 `decisions` 阶段）。
- 它不替代看图。`verdict` 该填什么仍然要翻原书 / 看 `review.png`。
- 它不是配置格式。表格是**运行产物**，每次 `table` 重渲染都会覆盖；别在里面写笔记（写 `review/notes.md`）。

