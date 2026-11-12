# bookflow

扫描版 PDF → 可跳转、可重排的电纸书 EPUB 3.0。整条链路本地运行，不上传任何页面。

## 它解决什么问题

PDF→EPUB 其实是**两个异质任务**：像素→文字（OCR，占 80% 工作量与 95% 的失败率），
以及文字→结构（分章、合段、目录）。市面上的工具大多只做后半段，默认输入自带完好文字层；
遇到纯扫描书就直接放弃。

这个工作流把前半段也做实，并且 **章节层级交给书里印好的目录页决定，不靠字号猜**。

## 几个关键取舍

- **目录驱动分章**：先解析印刷目录页拿到权威条目与层级，再在正文里顺序单调匹配。
  比按字号/版式猜稳得多，`第X章 / 第X节 / 一、` 三级能稳定还原。
- **页眉页脚阈值自适应**：从数据里学书眉的位置，不写死常数。写死 10.5% 曾把正文首行整行删掉。
- **EPUB3 弹窗脚注**：正文渲染成 `<a epub:type="noteref">`，注文是 `<aside epub:type="footnote">`，
  Apple Books / Thorium / Kobo 直接浮层弹出。锚不上正文的注降级成章末注，**一条都不丢**。
- **宁可少连不可错连**：脚注只在本页"正文恰好一个该号 + 注区恰好一条同号"时才内联。
  盲目按顺序配对实测 26 对里 26 对错配。
- **不自动改错字**：fix 只收录"错误写法在中文里根本不是词"的对子；两种写法都成立的只出候选清单，
  交人裁决（见 [PROMPTS.md](PROMPTS.md) 的 L0~L3 分级）。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/README.md](docs/README.md) | 快速开始、阶段命令、实测数据、引擎选型、已知限制 |
| [docs/architecture.md](docs/architecture.md) | 系统目标与边界、架构图、模块清单、数据契约、关键设计决策 |
| [docs/HANDOFF.md](docs/HANDOFF.md) | 当前进度、已修问题清单、下一步 |
| [docs/MEMORY.md](docs/MEMORY.md) | 事实速查、约定、踩过的坑（21 条，每条都有真实案例） |
| [PROMPTS.md](PROMPTS.md) | 分级裁决策略与提示词（L0~L3） |
| [REVIEW.md](REVIEW.md) | 2026-11-12 的审阅报告（问题清单与证据） |

## 30 秒上手

先用 `requirements.txt` 装依赖（建议装进虚拟环境），另外需要 poppler 的 `pdftoppm` 在 PATH 上。

~~~powershell
# $py 换成装好依赖的 Python 解释器路径
$py = "python"

# 全流程：PDF → pages → ocr → repair → layout → md → audit → epub（含弹窗脚注）
& $py bookflow.py all --work ./run1 --pdf "D:/books/某书.pdf" `
    --title "书名" --author "作者" --publisher "出版社"

# 只重排版（改了 layout.py 之后，秒级）
& $py bookflow.py md --work ./run1 --title "书名"

# 跑测试
& $py -m unittest discover -s tests -v
~~~

## 目录

| 路径 | 说明 |
|---|---|
| `bookflow.py` | CLI 入口与阶段编排 |
| `run_ocr.py` / `repair.py` | OCR 与补识别 |
| `run_layout.py` | 版面分析（脚注/页眉/页码分流，可选） |
| `layout.py` | 版面清理、合段、目录驱动分章、脚注锚定（核心，最复杂） |
| `fix.py` / `qa.py` / `audit.py` | 形近字修正 / 质检 / 替换型错字候选 |
| `disagree.py` / `apply_decisions.py` | 双模型分歧召回与裁决回填 |
| `epub3.py` / `make_epub.py` | EPUB3 构建（弹窗脚注 `noteref`/`aside`）与 CLI |
| `assets/style.css` | 成书样式表 |
| `cover.py` / `check_epub.py` | 封面裁切 / EPUB 结构校验 |
| `jsonio.py` | JSON 读写工具（原子写、句柄安全） |
| `tests/` | 回归测试（17 个用例，全部来自真实踩坑） |
| `pages/` `ocr*/` `out/` | 运行产物（已 `.gitignore`，不入库） |

## 关于扫描书与版权

仓库里只有代码、文档和测试。**扫描页、OCR 中间结果、成书都不入库**，
它们有版权，只应留在你自己的机器上。

## 许可

MIT