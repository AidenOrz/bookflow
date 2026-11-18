# 工作目录分层与输出规范（已落地）

> 状态：**已落地并完成迁移**（2026-11-19）。设计依据见 [IO-CONVENTIONS.md](IO-CONVENTIONS.md)；
> 界面规范见 [UI-DESIGN.md](UI-DESIGN.md)；本文只讲**现在是什么样、怎么用**。

## 1. 统一布局：四本书已经全部迁完

**仓库第一层不再有数据目录。** 四本既有书已全部迁进 `runs/<book-id>/`，
旧位置（仓库根的 `pages/ ocr* out/`、`run-lacan/` `run-guixun/` `run-sengguan/`）已删除。

| 书 | 位置 | 迁移前 |
|---|---|---|
| 黑格尔辩证法讲演录 | `runs/hegel` | 仓库根（仓库根**就是**它的工作目录） |
| 拉康式主体：在语言与享乐之间 | `runs/lacan` | `run-lacan/` |
| 规训与惩罚 | `runs/guixun` | `run-guixun/` |
| 中国僧官制度史 | `runs/sengguan` | `run-sengguan/` |

book-id 用书名而不是旧的 `run-xxx`：`runs/` 已经表达了「这是一次任务」，
再叫 `runs/run-lacan` 读起来重复；而且同一个 id 会作为 `dist/<book-id>/` 的目录名。

`paths.py` 里 `flat` 布局的代码**保留不删**——它是一套已被验证过的读取路径，
将来接手别人的旧工作目录时还要用它。但仓库里现在只有 `tiered` 一种在跑。

## 2. 四类产物分家（这是本规范的核心）

| 类别 | 位置 | 谁能删 | 谁写 |
|---|---|---|---|
| 交付物 | `dist/<book-id>/*.epub` | 只有人 | `epub` 阶段 |
| 机器报告 | `<run>/report/` | 任何人，删了重跑 | 各阶段脚本 |
| 人工件 | `<run>/review/` | **谁都不能碰** | **只有人**（脚本只读） |
| 中间产物 | `<run>/work/` | `prune` | 各阶段脚本 |

`report/` 里放：`book.md` `report.json` `qa.txt` `audit.txt/json` `disagree.txt/json` `review.png` `cover.jpg` `images/`。

`review/` 里放：`decisions.json`（**全项目唯一不可重建的产物**）、`notes.md` / `校对清单.md`。

两者怎么分只有一条判据：**是不是脚本每次重跑都会覆盖**。会覆盖的进 `report/`，只有人写过的进 `review/`。

~~~text
runs/<book-id>/
├─ manifest.json          ← 这次是怎么跑出来的（唯一输入身份 + 全部参数 + 每阶段状态）
├─ work/                  ← 中间产物，prune 可整片删
│  ├─ pages/  ocr/  ocr_fix/  ocr_layout/  ocr_text/ …
│  └─ .backup/            ← 回填前的备份（活过一次 prune）
├─ report/                ← 机器报告，删了重跑即可
│  ├─ book.md  report.json  qa.txt  audit.*  disagree.*
│  ├─ review.png  cover.jpg
│  └─ images/
└─ review/                ← 人工件，脚本只读
   └─ decisions.json
~~~

## 3. 路径一律走 `paths.py`，不许手拼

~~~python
import paths
paths.work_dir(run_root)          # -> <run>/work
paths.report_dir(run_root)        # -> <run>/report
paths.review_dir(run_root)        # -> <run>/review
paths.ocr_dir(run_root, "ocr_fix")
paths.pages_dir(run_root)
paths.decisions_path(run_root)    # -> <run>/review/decisions.json
paths.backup_dir(run_root)        # -> <run>/work/.backup
paths.dist_dir(book_id)
~~~

**为什么备份要挪出被备份的目录**：旧路径 `<src>/.bak/` 在数据目录**内部**，
`prune` 删整个 `ocr_merged` 时会连备份一起删（IO-CONVENTIONS §9.5 实测过）。

## 4. `manifest.json`：这次是怎么跑出来的

~~~json
{"schema": "bookflow.manifest", "schema_version": 1, "book_id": "lacan",
 "input": {"pdf": "input/lacan/source.pdf", "sha256": "9f2c…", "pages": 310,
           "title": "…", "author": "…", "publisher": "…"},
 "params": {"dpi": 300, "procs": 5, "device": "auto", "model": "small",
            "text_layer": "verify", "no_layout": false, "conservative_fix": false},
 "stages": {"render": {"status": "ok", "at": "2026-11-18T12:01:03",
                       "count": 310, "out": "work/pages", "elapsed": 42.1}}}
~~~

**三条硬约束**：

1. `input.sha256` 是**唯一**的输入身份。路径相同不代表内容相同——
   旧代码只比 `realpath`，同名不同版的 PDF 换进来，下游产物看起来仍然"正常"。
2. `stages[*].status` 取 `ok | degraded | failed | skipped | running`。
   **没有这条，「目录存在」就被当成「阶段跑完」**（迁移前 `run-sengguan/ocr_text`
   只剩一个 `_textlayer.meta`，与「这一步还没跑」在文件系统上一模一样）。
3. 路径一律**相对工作目录根**、正斜杠。禁止绝对路径。

**manifest 不挡路**：写不进去只打一行告警，阶段照常跑完（`stage_manifest.py` 全部包了 try）。
**failed 不算做完**：进度条据此计算，否则一个阶段失败仍显示 100%。

**manifest 不参与布局判定**：判定只看 `work/` `report/` `review/` 三个目录。
把 manifest 算进去会导致"在 flat 目录跑一次阶段 → 写下 manifest → 整棵树被判成 tiered →
下个阶段去 work/ 找 pages/ 找不到"，而每一步退出码都是 0。

## 5. 迁移工具（本次已用完，保留备将来）

~~~powershell
python tools/migrate_layout.py --all                   # 预演：仓库根 + 所有 run-*/
python tools/migrate_layout.py --all --apply           # 真搬，每本独立写 .migration-<ts>.json
python tools/migrate_layout.py --work runs/lacan --rollback   # 按记录搬回去
~~~

- **默认预演**，不加 `--apply` 一个字节都不动。
- 内置**无损守卫**：搬前记 `book.md` / `decisions.json` / `校对清单.md` 的 sha256，搬后复核，不一致就退出并提示回滚。本次 4 本 7 项守卫全部通过。
- 不认识的文件**一律留在原处**并打进"未分类"清单——迁移工具不替人判断这文件是什么。本次四本书的未分类清单全程为空。
- `.migration-*.json` 回滚凭据**保留不删**，它记录"这批文件曾经在哪"。确认不再需要回滚后可手工删。

**回滚日志留在 `runs/<id>/` 下**（不是源目录——源目录搬完就删了）。

## 6. 迁移后的验收证据

搬迁的无损性是用**迁移前后直接比对字节**证明的，不是用重跑证明的：

| 书 | `book.md` SHA256 前 8 位 | 页数守恒 |
|---|---|---|
| hegel | `320A586E`（迁移前 `320A586E`） | pages/ocr×5 各 306 |
| lacan | `457413BA`（迁移前 `457413BA`） | pages/ocr/ocr_fix/ocr_layout 各 310 |
| guixun | `E5053BDD`（迁移前 `E5053BDD`） | pages/ocr/ocr_fix/ocr_layout 各 383 |
| sengguan | `0BA1E017`（迁移前 `0BA1E017`） | pages 等各 350 |

`runs/lacan/review/decisions.json` = `87547EA9`、1157481 字节，逐字节完好。

**重跑 `md` 不会复现这三本的旧 `book.md`，这是预期的**：基线由更早版本的代码生成，
之后 `layout`/`fix`/`outline` 都有实质改动（PDF 书签当目录、图表不再丢弃等）。
尤其 lacan 那本源 PDF 已不在本机，书签通道读不到数据，只能退回目录页解析——
所以重跑得到的目录树条目数必然不同。**这不是迁移造成的，也不该被"修"**。

## 7. 新目录与文件

| 路径 | 是什么 |
|---|---|
| `paths.py` | 布局判定 + 角色到路径的解析（唯一入口） |
| `manifest.py` | manifest.json 的读写（缺/坏都不抛） |
| `stage_manifest.py` | 把阶段结果记进 manifest 的外包层（不挡路） |
| `runs.py` | 发现 run、解析 run id、给出阶段状态与进度 |
| `tools/migrate_layout.py` | 平铺 → 分层的一次性迁移，可回滚、可批量 |
| `webui/` | 本地面板（契约见 `webui/API.md`，界面见 `docs/UI-DESIGN.md`） |
| `input/<book-id>/source.pdf` | 输入落档（不入库） |
| `dist/<book-id>/` | 交付物（不入库） |
| `runs/<book-id>` | 每本书的工作目录（不入库） |

## 8. 目录约定速查

~~~text
bookflow/
├─ paths.py manifest.py stage_manifest.py runs.py  ← 布局与运行事实
├─ *.py                                            ← 流水线模块（第一层仍是模块入口）
├─ tools/                                          ← 一次性/辅助脚本
├─ webui/
│  ├─ API.md                                       ← 前后端契约（唯一事实源）
│  ├─ server.py                                    ← 纯标准库后端
│  └─ static/                                      ← 纯静态前端（css/ + js/，无构建）
├─ docs/  tests/
├─ input/<book-id>/source.pdf                      ← 只读输入
├─ runs/<book-id>/{work,report,review}/manifest.json
└─ dist/<book-id>/*.epub                           ← 交付物
~~~

**刻意没有做的两件事**（理由见 IO-CONVENTIONS §6）：不改 `p-001.png.json` 的双后缀
（那是 `_page_jsons()` 的契约）、不靠改后缀去"修" pdftoppm 不补零。
