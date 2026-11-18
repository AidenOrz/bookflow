# bookflow WebUI API 契约（v1，已冻结）

> 本文件是**前后端唯一的接口约定**。后端按它实现，前端按它消费。
> 改动必须先改这里，再改两端。
> 服务只绑 `127.0.0.1`，默认端口 8765（占用则递增）。

## 0. 运行方式

```powershell
python bookflow.py ui            # 起服务并打开浏览器
python bookflow.py ui --port 8800 --no-open
```

前端是**纯静态文件**（`webui/static/`），由后端在 `/` 直接托管：
无构建步骤、无 CDN、无 npm 依赖、可离线。用原生 ES module + fetch + EventSource。

## 1. 通用约定

- 所有响应 `application/json; charset=utf-8`，字段名 snake_case。
- 出错统一返回 HTTP 4xx/5xx + `{"error": "人话原因", "detail": "可选"}`。
- 路径参数里的 run id 是 `runs/<book-id>` 的目录名，或仓库根工作目录的名字
  （见 `GET /api/runs` 返回的 `id`）。前端**不要**自己拼路径。
- 时间一律 ISO8601 本地时间字符串。

## 2. 运行与预设

### GET /api/state
一次拿全，页面首屏只发这一个请求。

```json
{
  "version": "bookflow-webui/1",
  "cwd": "E:\\workspace\\bookflow",
  "presets": [
    {"id": "scan", "name": "古籍与纯扫描本", "description": "无文字层，全程 OCR，形近字只出候选",
     "params": {"text_layer": "off", "conservative_fix": true, "no_layout": false}},
    {"id": "modern", "name": "现代学术排版", "description": "文字层可用，抽出来与 OCR 对账",
     "params": {"text_layer": "verify", "conservative_fix": false, "no_layout": false}},
    {"id": "sample", "name": "前 20 页极速打样", "description": "只渲染前 20 页，几分钟出样书",
     "params": {"text_layer": "verify", "conservative_fix": false, "no_layout": false, "limit_pages": 20}}
  ],
  "runs": [
    {
      "id": "run-lacan",
      "path": "run-lacan",
      "layout": "flat",
      "title": "拉康式主体：在语言与享乐之间",
      "pages": 310,
      "has_epub": true,
      "epub_name": "拉康式主体：在语言与享乐之间.epub",
      "pending_decisions": 2380,
      "stages": [
        {"stage": "render", "status": "ok", "at": "2026-11-15T20:41:03", "count": 310, "detail": ""}
      ],
      "progress": {"done": 7, "total": 10, "percent": 70.0, "current": "epub"}
    }
  ]
}
```

- `layout`：`"tiered"`（`work/ report/ review/` 三层）或 `"flat"`（旧的单层 `pages/ out/`）。
- `stages`：**固定顺序** `render,textlayer,ocr,repair,layout,md,verify,audit,epub`；
  状态取 `ok | degraded | failed | skipped | missing`；`missing` 表示还没跑。
- `pending_decisions`：`decisions.json` 里 `verdict == "?"` 的条数；没有裁决表时为 0。

### POST /api/upload
`multipart/form-data`，字段名 `file`（PDF）。落盘到 `input/<book-id>/source.pdf`。

```json
{"upload_id": "lacan", "path": "input/lacan/source.pdf", "pages": 310,
 "meta": {"title": "拉康式主体", "author": "", "publisher": ""}}
```

`pages` 用 `pdfinfo` 读，读不到时为 `null`。`meta` 从 PDF 元信息尽力猜，全空也要返回。

### POST /api/runs
起一个新 run 并开始跑流水线。

请求体：
```json
{"pdf_path": "input/lacan/source.pdf", "book_id": "lacan",
 "preset": "modern", "title": "拉康式主体", "author": "", "publisher": "",
 "stages": ["all"], "params": {"dpi": 300, "device": "auto", "model": "small"}}
```

- `book_id` 只允许 `[a-z0-9-]+`；`pdf_path` 可以是 `upload_id` 生成的路径，也可以是绝对路径。
- `stages`：默认 `["all"]`；也可以传子集如 `["render","ocr","md"]` 逐段跑。
- 返回 `{"run_id": "lacan", "job_id": "job-1", "resolved": {"text_layer":"verify", ...}}`
  —— `resolved` 是把 preset 与显式 params 合并后的**最终生效参数**，
  前端必须在「审批」步骤把这整份原样展示给人看。

### POST /api/runs/{id}/stages/{stage}
只跑某个阶段（重跑用）。请求体同 params 片段，可空。返回 `{"job_id": "job-7"}`。

### POST /api/runs/{id}/abort
安全中断当前 job（后端调 `kill_tree`，清理整棵子进程树）。返回 `{"ok": true}`。

## 3. 作业流（SSE）

### GET /api/jobs/{job_id}/stream
`text/event-stream`。连接后**先补发最近 200 行日志**，再持续推送。

四种事件：

```
event: log
data: {"t":"2026-11-18T11:41:03","stage":"ocr","line":"  页 12/310"}

event: stage
data: {"stage":"ocr","status":"running","percent":12.3,"eta_seconds":120,"page":38,"total":310}

event: done
data: {"stage":"ocr","status":"ok","rc":0,"elapsed":183.2}

event: job
data: {"job_id":"job-1","status":"done","rc":0}
```

`done` 之后连接关闭。前端 `EventSource` 断开即视为终态（除非主动 abort）。

### GET /api/jobs/{job_id}
非流式状态：`{"job_id":"job-1","status":"running|done|failed|killed","rc":0,"lines":120}`。

## 4. 裁决（核心）

### GET /api/runs/{id}/decisions?only=eyes
`only` ∈ `eyes|noise|all`，默认 `eyes`。

```json
{
  "total": 2825, "pending": 2380,
  "stats": {"A": 214, "B": 231, "C": 0, "?": 2380},
  "unlocated": [ {"key":"p-088.png@?#7","page":"p-088.png","suggestion":"自白→自日","why":"p-088.png 里没有含「自白」的行"} ],
  "groups": [
    {"group": "已→己", "count": 60, "verdict": "", "note": "",
     "items": [
       {"key":"p-021.png@1436.3","page":"p-021.png","y0":1436.3,
        "A":"他者的欲望…像自已一样的小他者，","B":"他者的欲望…像自己一样的小他者，",
        "verdict":"","replacement":null,"status":"pending",
        "note":"audit 词表建议 已→己；需看图确认",
        "diff":[{"op":"replace","a":"已","b":"己"}],
        "crop_url":"/api/runs/run-lacan/crop?page=p-021.png&y0=1436.3,1436.3",
        "page_url":"/api/runs/run-lacan/page?page=p-021.png&hz=1.0"}
     ]}
  ]
}
```

- `groups` 按条数降序，`unlocated` 永远单独给、永远排在最前（`groups` 里不含它们）。
- `diff[].op` ∈ `equal|replace|insert|delete`。

### POST /api/runs/{id}/decisions
提交裁决。**这是审批动作**，只允许在用户确认后调用。

```json
{"updates": [{"key":"p-021.png@1436.3","verdict":"B"},
             {"key":"p-031.png@994.0","verdict":"C","replacement":"整行原文改好后的完整一行"}]}
```

返回 `{"changed": 4, "skipped": 21, "errors": []}`。
校验失败（key 不存在、C 缺 replacement、B/C 缺 y0）→ 400，`errors` 里逐条说明，**一个字节都不写**。

### GET /api/runs/{id}/crop?page=p-021.png&y0=1436.3&height=70
按行 y 坐标从 `pages/p-021.png` 裁一条高分辨率切片（上下各留 8px 视觉留白）。
返回 `image/png`。`height` 缺省按行高，可给上限。

### GET /api/runs/{id}/page?page=p-021.png
整页原图，`image/png`。可选 `box=x0,y0,x1,y1` 只取一块。

### POST /api/runs/{id}/apply/preview
**干跑**，不写盘。返回回填计划：

```json
{"counts": {"apply": 4, "already": 21, "missing": 2, "skip": 233},
 "items": [{"kind":"apply","key":"p-021.png@1436.3","old":"…已…","new":"…己…","reason":""}]}
```

### POST /api/runs/{id}/apply
真正回填并重建成书。请求体：

```json
{"confirm": true, "then": ["layout", "md", "epub"]}
```

`confirm` 必须为 `true`，否则 400（防止误点）。
返回 `{"job_id": "job-9"}`，用 SSE 看进度。
默认 `then` 就是 `["layout","md","epub"]`——**缺了 layout 那一环裁决成果会被静默丢弃**。

## 5. 检视与成书

### GET /api/runs/{id}/report
`report.json` 的精简投影：

```json
{"book": {"title":"","author":"","publisher":""},
 "pages": 310, "cjk_chars": 233812,
 "fix": {"fixed": 12, "candidates": 3},
 "toc": {"entries": 103, "matched": 101, "source": "bookmarks",
         "tree": [{"level":1,"title":"第一部分 酷刑","page":15,"printed_page":3,"score":0.98}]},
 "notes": {"total": 243, "linked": 110, "orphan": 133, "pages": 88},
 "qa": {"issues": [{"level":"warn","page":52,"text":"整页空缺"}]},
 "figures": {"made": 121, "files": 121}}
```

### GET /api/runs/{id}/markdown?path=book.md
返回纯文本（`text/plain; charset=utf-8`），用于「阅读预览」。

### GET /api/runs/{id}/file?path=qa.txt
按 `path`（相对该 run 的报告目录）返回文本文件内容，越界 404。

### GET /api/runs/{id}/epub
返回 `application/epub+zip` 附件（`Content-Disposition: attachment`）。没有则 404。

### GET /api/runs/{id}/manifest
返回 `manifest.json`（没有返回 `{}`）。前端「这次是怎么跑出来的」面板直接展示。

## 6. 状态码约定

| 场景 | 状态码 |
|---|---|
| 参数非法 / 契约不符 | 400 |
| run 不存在 | 404 |
| 有 job 正在跑，重复启动 | 409 |
| 后端内部异常 | 500 + `{"error": ...}` |
