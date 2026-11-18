# 工作目录分层与输出规范（已实现）

> 状态：**已落地**（2026-11-18）。设计依据与实测数据见 [IO-CONVENTIONS.md](IO-CONVENTIONS.md)；
> 本文只讲**现在是什么样、怎么用**，不重复论证过程。

## 1. 三种布局并存，旧目录一个字节都不动

| 布局 | 长什么样 | 谁在用 |
|---|---|---|
| `tiered` | `<run>/work/ report/ review/ manifest.json` | 新书（`runs/<book-id>/`）、经 `tools/migrate_layout.py` 迁移过的书 |
| `flat` | `<run>/pages/ ocr*/ out/` | 四个既有工作目录（`.`、`run-guixun`、`run-lacan`、`run-sengguan`） |

判定只看**三个目录**在不在（`work/` `report/` `review/`），**不看 manifest.json**。
把 manifest 算进判定会导致：在 flat 目录上跑一次阶段 → 写下 manifest → 整棵树被判成 tiered
→ 下个阶段去 `work/` 找 `pages/` 找不到。表现为"跑一次 md 之后 out/ 整个消失"，
而每一步退出码都是 0。

## 2. 四类产物分家（这是本规范的核心）

| 类别 | tiered 位置 | flat 位置 | 谁能删 | 谁写 |
|---|---|---|---|---|
| 交付物 | `dist/<book-id>/*.epub` | 同（`out/*.epub` 保留兼容） | 只有人 | `epub` 阶段 |
| 机器报告 | `<run>/report/` | `<run>/out/` | 任何人，删了重跑 | 各阶段脚本 |
| 人工件 | `<run>/review/` | `<run>/out/` | **谁都不能碰** | **只有人**（脚本只读） |
| 中间产物 | `<run>/work/` | `<run>/`（平铺） | `prune` | 各阶段脚本 |

`report/` 里放：`book.md` `report.json` `qa.txt` `audit.txt/json` `disagree.txt/json` `review.png` `cover.jpg` `images/`。

`review/` 里放：`decisions.json`（**全项目唯一不可重建的产物**）、`notes.md` / `校对清单.md`。

两者怎么分只有一条判据：**是不是脚本每次重跑都会覆盖**。会覆盖的进 `report/`，只有人写过的进 `review/`。

## 3. 路径一律走 `paths.py`，不许手拼

~~~python
import paths
paths.work_dir(run_root)          # tiered -> <run>/work；flat -> <run>
paths.report_dir(run_root)        # tiered -> <run>/report；flat -> <run>/out
paths.review_dir(run_root)        # tiered -> <run>/review；flat -> <run>/out
paths.ocr_dir(run_root, "ocr_fix")
paths.pages_dir(run_root)
paths.decisions_path(run_root)
paths.backup_dir(run_root)        # tiered -> <run>/work/.backup（不再是 <src>/.bak）
~~~

**为什么备份要挪出来**：旧路径 `<src>/.bak/` 是"被备份的目录内部"。
`prune` 删整个 `ocr_merged` 时会连备份一起删（IO-CONVENTIONS §9.5 实测过）。
挪到 `work/.backup/` 之后，数据目录里只剩页文件，备份活过一次 prune。

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
   旧代码只比 `realpath`（`bookflow.py` 的 textlayer 阶段），
   同名不同版的 PDF 换进来，所有下游产物看起来仍然"正常"。
2. `stages[*].status` 取 `ok | degraded | failed | skipped | running`。
   **没有这条，「目录存在」就被当成「阶段跑完」**（`run-sengguan/ocr_text` 只剩一个
   `_textlayer.meta`，和「这一步还没跑」在文件系统上一模一样）。
3. 路径一律**相对工作目录根**、正斜杠。禁止绝对路径。

**manifest 不挡路**：写不进去只打一行告警，阶段照常跑完（`stage_manifest.py` 全部包了 try）。
**failed 不算做完**：进度条据此计算，否则一个阶段失败仍显示 100%。

## 5. 迁移既有工作目录（可选，默认预演）

~~~powershell
python tools/migrate_layout.py --work run-guixun            # 预演，一个字节都不动
python tools/migrate_layout.py --work run-guixun --apply    # 真搬，写 .migration-<ts>.json
python tools/migrate_layout.py --work run-guixun --rollback # 按记录搬回去
~~~

**不迁移也能用**：flat 布局永远可读。迁移只解决"仓库根同时是工作目录"这一条
（`.gitignore` 要写一长串裸名规则、想只删中间产物必须人工挑）。

实测（`run-guixun` 的临时副本）：13 项搬迁，`book.md` 内容逐字节不变；
搬完直接 `bookflow.py md` 能跑，输出在 `report/book.md`。

## 6. 新目录与文件

| 路径 | 是什么 |
|---|---|
| `paths.py` | 布局判定 + 角色到路径的解析（唯一入口） |
| `manifest.py` | manifest.json 的读写（缺/坏都不抛） |
| `stage_manifest.py` | 把阶段结果记进 manifest 的外包层（不挡路） |
| `runs.py` | 发现 run、解析 run id、给出阶段状态与进度 |
| `tools/migrate_layout.py` | 平铺 → 分层的一次性迁移，可回滚 |
| `webui/` | 本地面板（见 `webui/API.md`） |
| `input/<book-id>/source.pdf` | 输入落档（不入库） |
| `dist/<book-id>/` | 交付物（不入库） |
| `runs/<book-id>` | 新书的工作目录（不入库） |

## 7. 目录约定速查

~~~text
bookflow/
├─ paths.py manifest.py stage_manifest.py runs.py  ← 布局与运行事实
├─ *.py                                            ← 流水线模块（第一层仍是模块入口）
├─ tools/                                          ← 一次性/辅助脚本
├─ webui/
│  ├─ API.md                                       ← 前后端契约（唯一事实源）
│  ├─ server.py                                    ← 纯标准库后端
│  └─ static/                                      ← 纯静态前端（无构建）
├─ docs/  tests/
├─ input/<book-id>/source.pdf                      ← 只读输入
├─ runs/<book-id>/{work,report,review}/            ← 新书
└─ dist/<book-id>/*.epub                           ← 交付物
~~~

**刻意没有做的两件事**（理由见 IO-CONVENTIONS §6）：不改 `p-001.png.json` 的双后缀
（那是 `_page_jsons()` 的契约）、不靠改后缀去"修" pdftoppm 不补零。
