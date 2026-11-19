// 处理进度（#/task/:id?job=<执行 id>）。
// 左栏：9 个阶段的纵向时间线；右栏：日志面板（SSE 实时）。
// 契约见 webui/API.md §2/§3，设计见 docs/UI-DESIGN.md §4.3。
import { el, clear, fmt, toast, confirmDialog, emptyState, progressBar,
         STAGES, STAGE_LABELS, STAGE_HINTS, STATUS_LABELS, statusClass,
         stageOf } from "./ui.js";
import { openJobStream, abortRun, rerunStage, jobStatus, describeError } from "./api.js";

const MAX_LOG_LINES = 3000;

// 任务 id 可能含斜杠（如 runs/lacan），逐段编码，不要整体 encodeURIComponent
export function taskHash(id, view) {
  const parts = String(id || "").split("/").map(encodeURIComponent).join("/");
  return "#/task/" + parts + (view ? "/" + view : "");
}

function setBar(bar, percent, cls) {
  const fill = bar.querySelector(".progress-fill");
  if (!fill) return;
  const p = Math.max(0, Math.min(100, Number(percent) || 0));
  fill.style.width = p + "%";
  fill.className = "progress-fill" + (cls ? " " + cls : "");
}

export function mount(ctx) {
  const taskId = ctx.params.id;
  let task = ctx.findRun(taskId);

  const root = el("div", { class: "view view-progress" });

  // ---------- 找不到任务 ----------
  if (!task) {
    root.appendChild(el("section", { class: "card" },
      el("div", { class: "card-body" },
        emptyState("找不到这个任务",
          "任务 " + taskId + " 不在当前工作目录下。可能是目录被移动，或者状态还没刷新。",
          el("button", {
            class: "btn btn-primary", type: "button",
            // 这里不能复用页面内的 reloadState：它在页面结构建好之后才可用，
            // 而这条分支根本没往下建。重新拉一次 state 再走一遍路由最干净。
            onclick: (ev) => {
              const btn = ev.currentTarget;
              btn.disabled = true;
              btn.textContent = "拉取中…";
              ctx.loadState(true).then(() => {
                ctx.navigate(location.hash, true);
              }).catch((err) => {
                btn.disabled = false;
                btn.textContent = "重新拉取状态";
                toast("刷新状态失败：" + describeError(err), "error", 8000);
              });
            }
          }, "重新拉取状态")),
        el("div", { class: "pg-back" },
          el("a", { class: "btn btn-ghost", href: "#/tasks" }, "回到任务列表"))
      )));
    return root;
  }

  // ---------- 执行期状态 ----------
  let jobId = ctx.query.job || "";
  let es = null;
  let closedByUs = false;
  let sawTerminal = false;
  let execActive = !!jobId;        // 这条执行还在跑吗（结束后就禁用「中断执行」）
  let tickTimer = null;
  let liveFrom = 0;                // 当前阶段在本页开始计时的时刻
  const liveStages = {};           // stage -> {status, percent, page, total}

  // ---------- 顶部 ----------
  const titleNode = el("h1", { class: "page-title", text: task.title || taskId });
  const idChip = el("span", { class: "chip", text: taskId });
  const metaNode = el("div", { class: "page-sub pg-head-meta" });
  const headBar = progressBar(0);
  const outLink = el("a", { class: "btn", href: taskHash(taskId, "output") }, "查看成品");
  const abortBtn = el("button", {
    class: "btn btn-danger", type: "button", title: "中断这个任务正在执行的阶段",
    onclick: () => doAbort()
  }, "中断执行");

  root.appendChild(el("div", { class: "page-head pg-head" },
    el("div", { class: "pg-head-main" },
      el("div", { class: "pg-head-title" }, titleNode, idChip),
      metaNode,
      el("div", { class: "pg-head-bar" }, headBar)
    ),
    el("div", { class: "page-head-actions pg-head-actions" },
      outLink,
      abortBtn
    )
  ));

  // ---------- 待校对提示卡 ----------
  const notice = el("section", { class: "pg-notice", hidden: true });
  const noticeText = el("div", { class: "pg-notice-text" });
  const noticeBtn = el("a", { class: "btn btn-primary", href: taskHash(taskId, "proofread") }, "去校对");
  notice.appendChild(el("span", { class: "pg-notice-icon", text: "!" }));
  notice.appendChild(noticeText);
  notice.appendChild(noticeBtn);

  function renderNotice() {
    const n = Number(task.pending_decisions) || 0;
    notice.hidden = n <= 0;
    clear(noticeText);
    if (n > 0) {
      noticeText.appendChild(el("div", { class: "pg-notice-title" },
        "这本书有 " + fmt.num(n) + " 条待校对，校对完再出成品质量更好"));
      noticeText.appendChild(el("div", { class: "pg-notice-sub" },
        "校对结果会在回填后重建版面与成品，早校对早受益。"));
    }
  }

  // ---------- 左栏：阶段时间线 ----------
  const timeline = el("div", { class: "pg-timeline" });
  const timelineCard = el("section", { class: "card pg-timeline-card" },
    el("div", { class: "card-head" },
      el("h2", { class: "card-title", text: "阶段时间线" }),
      el("span", { class: "card-sub", text: "共 9 个阶段" })
    ),
    timeline
  );

  function stageHint(name, rec) {
    const bits = [STAGE_LABELS[name] + " · " + (STATUS_LABELS[rec.status] || rec.status || "")];
    if (STAGE_HINTS[name]) bits.push(STAGE_HINTS[name]);
    if (rec.count !== null && rec.count !== undefined && rec.count !== "") {
      bits.push("件数 " + rec.count);
    }
    if (rec.elapsed) bits.push("耗时 " + fmt.dur(rec.elapsed));
    if (rec.detail) bits.push(rec.detail);
    // 跑不了时理由也要进 tooltip，不论跑过没有（跑过但现在跑不动的情况真实存在，
    // 见 stageRow 里的说明）。状态不在这里重复算：原来这里自己算了一遍
    // effective status，就为了判断要不要加这句——判据改掉之后它没有存在的理由了。
    if (!rec.can_run && rec.why_not) bits.push("跑不了：" + rec.why_not);
    return bits.join("\n");
  }

  function stageRow(name, idx) {
    const rec = stageOf(task, name);
    const live = liveStages[name] || {};
    const status = live.status || rec.status || "missing";
    const running = status === "running";
    const hint = stageHint(name, rec);

    const metaParts = [];
    if (rec.elapsed !== null && rec.elapsed !== undefined && rec.elapsed !== "") {
      metaParts.push("耗时 " + fmt.dur(rec.elapsed));
    }
    if (rec.count !== null && rec.count !== undefined && rec.count !== "") {
      metaParts.push("件数 " + fmt.num(rec.count));
    }
    if (live.percent !== undefined && live.percent !== null && !isNaN(Number(live.percent))) {
      metaParts.push(fmt.percent(live.percent));
    }
    if (live.page !== undefined && live.total) {
      metaParts.push("页 " + live.page + "/" + live.total);
    }
    if (live.eta_seconds !== undefined && live.eta_seconds !== null) {
      metaParts.push("剩余约 " + fmt.dur(live.eta_seconds));
    }
    if (rec.at) metaParts.push(fmt.datetime(rec.at));

    const metaNodeRow = el("div", { class: "pg-stage-meta" });
    metaParts.forEach((text, i) => {
      if (i > 0) metaNodeRow.appendChild(el("span", { class: "dot-sep", text: "·" }));
      metaNodeRow.appendChild(el("span", { text: text }));
    });

    // 按钮分三种，区别在「这一阶段现在点下去会不会当场失败」：
    //   已跑过 -> 「重跑」（产物会被覆盖，确认文案要说清）；
    //   未跑 + 能跑 -> 「运行」（本来一个按钮都没有，所有未跑阶段都是死行）；
    //   未跑 + 跑不了 -> 禁用的「运行」+ 行内理由。
    // 最后一种最要紧：理由只挂在 title 上等于没写——鼠标不悬停就永远看不见，
    // 而这条理由（例如「文字层每页只有 10 个汉字」）恰恰是这个阶段永远跑不了的原因。
    const isMissing = status === "missing";
    const label = STAGE_LABELS[name] || name;
    const whyNot = String(rec.why_not || "");
    // 老后端没有 can_run 字段时按「能跑」处理：宁可让人点一下看到真实报错，
    // 也不要因为字段缺失把所有阶段一次性做成死按钮。
    const canRun = rec.can_run !== false;

    // 判据只有一条：**现在点下去会不会当场失败**。
    // 不能按「跑过没有」来分——跑过的阶段同样可能已经跑不动了：
    // 四本书的 render 都跑过，但源 PDF 已不在本机（input/<id>/source.pdf 没有），
    // 重跑必然 sys.exit；hegel 的 verify 状态是 ok，可 ocr_text 里一页 JSON 都没有，
    // 重跑同样必然失败。给一个点了就报错的按钮，比不给按钮更浪费时间。
    const runLabel = isMissing ? "运行" : "重跑";
    let actionBtn = null;
    if (canRun) {
      actionBtn = el("button", {
        class: "btn btn-sm btn-ghost pg-rerun pg-run", type: "button",
        title: (isMissing ? "只跑" : "只重跑") + "「" + label + "」这一阶段",
        onclick: (ev) => doRerun(name, ev.currentTarget, isMissing ? "run" : "rerun")
      }, runLabel);
    } else {
      actionBtn = el("button", {
        class: "btn btn-sm btn-ghost pg-rerun", type: "button", disabled: true,
        title: whyNot || "这一阶段现在跑不了"
      }, runLabel);
    }

    // 理由的强调程度要分两种，否则整页都是红字：
    //   未跑 + 跑不了 = 这一阶段**永远**跑不出来，是要人处理的事，用 --warn；
    //   已跑 + 跑不了 = 产物齐全，只是现在重跑不了（如源 PDF 已不在本机），
    //   用 --fg-muted 就够了——那本书是好的，不需要人去修什么。
    const whyNode = (!canRun && whyNot)
      ? el("div", {
          class: "pg-stage-why" + (isMissing ? "" : " is-done"),
          text: (isMissing ? "跑不了：" : "现在无法重跑：") + whyNot
        })
      : null;

    return el("div", { class: "pg-stage" + (running ? " is-running" : "") },
      el("div", { class: "pg-rail" },
        el("span", {
          class: "stage-dot " + statusClass(status),
          title: hint,
          text: String(idx + 1)
        })
      ),
      el("div", { class: "pg-stage-body" },
        el("div", { class: "pg-stage-top" },
          el("span", { class: "pg-stage-name", title: STAGE_HINTS[name] || "", text: label }),
          running ? el("span", { class: "pg-spin", title: "正在执行" }) : null,
          el("span", { class: "spacer" }),
          el("span", { class: "pg-stage-status is-" + status, text: STATUS_LABELS[status] || status }),
          actionBtn
        ),
        metaNodeRow,
        rec.detail ? el("div", { class: "pg-stage-detail", text: rec.detail }) : null,
        whyNode
      )
    );
  }

  function renderTimeline() {
    clear(timeline);
    STAGES.forEach((name, idx) => timeline.appendChild(stageRow(name, idx)));
  }

  // ---------- 顶部信息 ----------
  function progressOf() {
    const p = task.progress || {};
    const list = task.stages || [];
    const done = typeof p.done === "number" ? p.done
      : list.filter((s) => s && (s.status === "ok" || s.status === "degraded" || s.status === "skipped")).length;
    const total = typeof p.total === "number" && p.total ? p.total : STAGES.length;
    const percent = typeof p.percent === "number" ? p.percent : (total ? (100 * done) / total : 0);
    return { done: done, total: total, percent: percent, current: p.current || "" };
  }

  function elapsedOf() {
    let sum = 0;
    (task.stages || []).forEach((rec) => {
      if (rec && typeof rec.elapsed === "number" && !isNaN(rec.elapsed)) sum += rec.elapsed;
    });
    let running = "";
    STAGES.forEach((name) => {
      const st = (liveStages[name] || {}).status;
      const rec = stageOf(task, name);
      if (!running && (st === "running" || rec.status === "running")) running = name;
    });
    if (running && liveFrom) {
      sum += Math.max(0, (Date.now() - liveFrom) / 1000);
    }
    return { total: sum, running: running };
  }

  function renderHead() {
    titleNode.textContent = task.title || taskId;
    idChip.textContent = taskId;
    const p = progressOf();
    const e = elapsedOf();
    const current = e.running || p.current;
    setBar(headBar, p.percent, p.done >= p.total && p.total ? "is-ok" : "");

    clear(metaNode);
    metaNode.appendChild(el("span", { text: "已完成 " + p.done + "/" + p.total + " 阶段" }));
    metaNode.appendChild(el("span", { class: "dot-sep", text: "·" }));
    metaNode.appendChild(el("span", {
      text: "已用时间 " + (e.total > 0 ? fmt.dur(e.total) : "—")
    }));
    if (current) {
      metaNode.appendChild(el("span", { class: "dot-sep", text: "·" }));
      metaNode.appendChild(el("span", {
        class: "pg-head-current",
        text: (e.running ? "正在执行 " : "当前阶段 ") + (STAGE_LABELS[current] || current)
      }));
    }
    metaNode.appendChild(el("span", { class: "dot-sep", text: "·" }));
    metaNode.appendChild(el("span", { text: fmt.percent(p.percent) }));

    if (task.has_epub) outLink.removeAttribute("hidden");
    else outLink.setAttribute("hidden", "");
    abortBtn.disabled = !execActive;
    abortBtn.title = execActive
      ? "中断这个任务正在执行的阶段"
      : "当前没有正在执行的阶段";
  }

  // ---------- 右栏：日志 ----------
  const logBody = el("div", { class: "pg-log-body" });
  const logScroll = el("div", { class: "pg-log-scroll" });
  const logCount = el("span", { class: "pg-log-count faint", text: "0 行" });
  const jumpBtn = el("button", {
    class: "btn btn-ghost btn-sm", type: "button", hidden: true,
    onclick: () => scrollToEnd(true)
  }, "↓ 回到底部");
  const streamState = el("span", { class: "pg-stream-state", text: "" });

  let stick = true;
  let lineCount = 0;

  function nearBottom() {
    return logScroll.scrollHeight - logScroll.scrollTop - logScroll.clientHeight < 48;
  }
  logScroll.addEventListener("scroll", () => {
    stick = nearBottom();
    jumpBtn.hidden = stick;
  });
  function scrollToEnd(force) {
    if (force) stick = true;
    logScroll.scrollTop = logScroll.scrollHeight;
    jumpBtn.hidden = stick;
  }

  logScroll.appendChild(logBody);

  const logCard = el("section", { class: "card pg-log-card" },
    el("div", { class: "card-head" },
      el("h2", { class: "card-title", text: "日志" }),
      streamState,
      el("span", { class: "spacer" }),
      logCount,
      jumpBtn
    ),
    logScroll
  );

  function appendRow(node) {
    logBody.appendChild(node);
    while (logBody.childNodes.length > MAX_LOG_LINES) logBody.removeChild(logBody.firstChild);
    lineCount++;
    logCount.textContent = lineCount + " 行";
    // 用户手动往上滚时不要抢滚动位置
    if (stick) scrollToEnd(false);
    else jumpBtn.hidden = false;
  }

  function appendLog(data) {
    if (!data) return;
    const stageName = data.stage ? (STAGE_LABELS[data.stage] || data.stage) : "";
    appendRow(el("div", { class: "pg-log-line" },
      el("span", { class: "pg-log-t", text: data.t ? fmt.time(data.t) : "" }),
      el("span", {
        class: "pg-log-tag" + (data.stage ? "" : " is-empty"),
        text: stageName || "—"
      }),
      el("span", { class: "pg-log-text", text: data.line === undefined || data.line === null ? "" : String(data.line) })
    ));
  }

  // 本地时间 HH:MM:SS。不能拿 toISOString 交给 fmt.time：它是 UTC，
  // 东八区会差 8 小时（实测页面上出现 16:04 而真实时间是 00:04）。
  function nowClock() {
    const d = new Date();
    const p = (n) => (n < 10 ? "0" + n : String(n));
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function appendSys(text, kind) {
    appendRow(el("div", { class: "pg-log-line is-sys" + (kind ? " is-" + kind : "") },
      el("span", { class: "pg-log-t", text: nowClock() }),
      el("span", { class: "pg-log-tag is-empty", text: "前端" }),
      el("span", { class: "pg-log-text", text: text })
    ));
  }

  function showNoJob() {
    clear(logBody);
    lineCount = 0;
    logCount.textContent = "0 行";
    streamState.textContent = "";
    streamState.className = "pg-stream-state";
    logBody.appendChild(el("div", { class: "pg-log-note" },
      el("div", { class: "pg-log-note-title", text: "这个任务当前没有正在执行的阶段" }),
      el("div", { class: "pg-log-note-sub",
        text: "从任务列表或校对页启动执行后会自动回到这里；也可以直接刷新状态看最新进度。" }),
      el("button", {
        class: "btn btn-primary", type: "button",
        onclick: (ev) => reloadState(ev.target)
      }, "重新拉取状态")
    ));
  }

  // ---------- 动作 ----------
  async function reloadState(btn) {
    if (btn) { btn.disabled = true; btn.textContent = "拉取中…"; }
    try {
      await ctx.loadState(true);
      if (!ctx.alive()) return;
      const found = ctx.findRun(taskId);
      if (found) task = found;
      renderAll();
      toast("状态已更新", "success");
    } catch (err) {
      if (ctx.alive()) toast("刷新状态失败：" + describeError(err), "error", 8000);
    } finally {
      if (btn && ctx.alive()) { btn.disabled = false; btn.textContent = "重新拉取状态"; }
    }
  }

  function refreshState() {
    return ctx.loadState(true).then(() => {
      if (!ctx.alive()) return;
      const found = ctx.findRun(taskId);
      if (found) task = found;
      renderAll();
    }).catch((err) => {
      if (ctx.alive()) toast("刷新状态失败：" + describeError(err), "error", 8000);
    });
  }

  async function doAbort() {
    if (!jobId) {
      toast("当前没有正在执行的阶段", "warn");
      return;
    }
    const ok = await confirmDialog({
      title: "中断执行",
      message: "中断会杀掉这一阶段的后台进程树，未写完的产物保持现状。" +
               "确定要中断「" + (task.title || taskId) + "」当前正在执行的阶段吗？",
      confirmText: "中断执行",
      cancelText: "继续跑",
      danger: true
    });
    if (!ok) return;
    if (!ctx.alive()) return;
    abortBtn.disabled = true;
    abortBtn.textContent = "中断中…";
    try {
      await abortRun(taskId);
      if (!ctx.alive()) return;
      closedByUs = true;
      closeStream();
      appendSys("已请求中断，后端正在清理子进程树", "warn");
      toast("已请求中断，后端正在清理子进程树", "success");
      await refreshState();
    } catch (err) {
      if (ctx.alive()) toast("中断失败：" + describeError(err), "error", 8000);
    } finally {
      if (ctx.alive()) {
        abortBtn.disabled = !jobId;
        abortBtn.textContent = "中断执行";
      }
    }
  }

  // mode: "rerun"（已有产物，会被覆盖）| "run"（未跑过，不覆盖任何东西）
  async function doRerun(stage, btn, mode) {
    const label = STAGE_LABELS[stage] || stage;
    const isRerun = mode !== "run";
    // 确认文案必须区分这两种：把「只跑这一阶段」说成「重跑」会让人以为
    // 自己上一次的产物要没了——而那一次根本没跑过。
    const ok = await confirmDialog({
      title: isRerun ? "重跑阶段" : "运行阶段",
      message: isRerun
        ? "将只重跑「" + label + "」这一阶段，沿用当前任务已确定的参数，" +
          "这一阶段已有的产物会被覆盖。确定继续吗？"
        : "将只跑这一阶段：「" + label + "」。沿用当前任务已确定的参数，" +
          "这一阶段还没有产物，不会覆盖任何东西。确定继续吗？",
      confirmText: isRerun ? "重跑" : "只跑这一阶段",
      cancelText: "取消",
      danger: isRerun
    });
    if (!ok || !ctx.alive()) return;
    const restLabel = isRerun ? "重跑" : "运行";
    if (btn) { btn.disabled = true; btn.textContent = "启动中…"; }
    try {
      const res = await rerunStage(taskId, stage, {});
      const jid = res && res.job_id ? String(res.job_id) : "";
      if (!jid) {
        if (btn) { btn.disabled = false; btn.textContent = restLabel; }
        toast("后端没有返回执行 id，无法跟踪日志", "error", 8000);
        return;
      }
      toast("已启动「" + label + "」，正在进入日志面板", "success");
      ctx.navigate(taskHash(taskId) + "?job=" + encodeURIComponent(jid));
    } catch (err) {
      if (btn) { btn.disabled = false; btn.textContent = restLabel; }
      if (ctx.alive()) toast(restLabel + "失败：" + describeError(err), "error", 9000);
    }
  }

  // ---------- 执行流 ----------
  function setStreamState(text, cls) {
    streamState.textContent = text || "";
    streamState.className = "pg-stream-state" + (cls ? " is-" + cls : "");
  }

  function closeStream(keepState) {
    sawTerminal = true;
    execActive = false;
    if (es) { try { es.close(); } catch (e) { /* 忽略 */ } }
    es = null;
    if (!keepState) setStreamState("", "");
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
  }

  function connect(id) {
    jobId = id;
    setStreamState("正在接收日志", "live");
    liveFrom = Date.now();
    if (!tickTimer) tickTimer = setInterval(() => { if (ctx.alive()) renderHead(); }, 1000);
    appendSys("连接执行流 " + id, "");

    es = openJobStream(id, {
      open: () => { setStreamState("正在接收日志", "live"); },
      log: (data) => appendLog(data),
      stage: (data) => {
        if (!data || !ctx.alive()) return;
        const prev = liveStages[data.stage] || {};
        liveStages[data.stage] = {
          status: data.status || "running",
          percent: data.percent,
          page: data.page,
          total: data.total,
          eta_seconds: data.eta_seconds
        };
        if (prev.status !== "running" && data.status === "running") {
          liveFrom = Date.now();
          appendSys("开始「" + (STAGE_LABELS[data.stage] || data.stage) + "」", "");
        }
        renderHead();
        renderTimeline();
      },
      done: (data) => {
        const status = data && data.status ? data.status : "";
        const label = data && data.stage ? (STAGE_LABELS[data.stage] || data.stage) : "阶段";
        if (data && data.stage) liveStages[data.stage] = { status: status || "ok" };
        appendSys(label + " 结束：" + (STATUS_LABELS[status] || status || "未知") +
          (data && data.elapsed !== undefined && data.elapsed !== null ? "，用时 " + fmt.dur(data.elapsed) : "") +
          (data && data.rc !== undefined ? "（退出码 " + data.rc + "）" : ""),
          status === "ok" ? "ok" : "warn");
        setStreamState("本阶段已结束", status === "ok" ? "ok" : "warn");
        // done 之后连接由后端关闭；这里主动关掉并刷新阶段状态
        closeStream();
        refreshState();
      },
      job: (data) => {
        const status = data && data.status ? data.status : "unknown";
        appendSys("执行结束：" + status + (data && data.rc !== undefined ? "（退出码 " + data.rc + "）" : ""),
          status === "done" ? "ok" : "warn");
        setStreamState("执行已结束", status === "done" ? "ok" : "warn");
        closeStream();
        refreshState();
      },
      error: () => {
        if (closedByUs || sawTerminal) {
          if (es) { try { es.close(); } catch (e) { /* 忽略 */ } }
          es = null;
          return;
        }
        setStreamState("日志流断开，正在确认执行状态", "warn");
        appendSys("日志流断开，正在确认执行状态…", "warn");
        jobStatus(jobId).then((info) => {
          if (!ctx.alive()) return;
          if (!info || info.status === "running") {
            setStreamState("正在接收日志", "live");
            return;
          }
          appendSys("执行状态为 " + info.status + "，连接关闭", "");
          setStreamState("执行已结束", info.status === "done" ? "ok" : "warn");
          closeStreamSafely();
          refreshState();
        }).catch(() => {
          if (!ctx.alive()) return;
          // 连执行记录都查不到（404）：这条执行已经不在后端了，别再假装还在收日志
          execActive = false;
          sawTerminal = true;
          setStreamState("这条执行已不存在", "warn");
          appendSys("找不到这条执行记录，可能服务重启过；页面显示的是当前阶段状态", "warn");
          renderHead();
        });
      }
    });
  }

  function closeStreamSafely() {
    if (es) { try { es.close(); } catch (e) { /* 忽略 */ } }
    es = null;
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
  }

  // ---------- 组装 ----------
  const layout = el("div", { class: "pg-layout" }, timelineCard, logCard);

  function renderAll() {
    renderHead();
    renderTimeline();
    renderNotice();
  }

  root.appendChild(notice);
  root.appendChild(layout);
  renderAll();

  if (jobId) {
    connect(jobId);
  } else {
    showNoJob();
  }

  // ---------- cleanup：切走页面必须关掉 EventSource ----------
  root.__cleanup = function () {
    closedByUs = true;
    if (es) { try { es.close(); } catch (e) { /* 忽略 */ } }
    es = null;
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
  };
  return root;
}
