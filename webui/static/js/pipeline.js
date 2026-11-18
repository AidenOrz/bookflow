// 流水线实时看板（#/run/:id?job=<job_id>）。
import { el, clear, fmt, toast, stageStrip, STAGES, STAGE_LABELS, STATUS_LABELS } from "./ui.js";
import { openJobStream, abortRun, rerunStage, jobStatus, describeError, getState } from "./api.js";

const MAX_LOG_LINES = 4000;

function stageProgressPanel() {
  const bar = el("div", { class: "progress-fill", style: { width: "0%" } });
  const label = el("div", { class: "big-progress-text", text: "等待作业启动…" });
  const eta = el("div", { class: "big-progress-eta", text: "" });
  const panel = el("div", { class: "big-progress" },
    el("div", { class: "big-progress-bar" }, bar),
    label, eta
  );
  return {
    node: panel,
    set(info) {
      const percent = typeof info.percent === "number" ? Math.max(0, Math.min(100, info.percent)) : 0;
      bar.style.width = percent + "%";
      const parts = [];
      if (info.stage) parts.push((STAGE_LABELS[info.stage] || info.stage));
      if (info.page !== undefined && info.total !== undefined && info.total) {
        parts.push("页 " + info.page + "/" + info.total);
      }
      parts.push(percent.toFixed(1) + "%");
      label.textContent = parts.join(" · ");
      const bits = [];
      if (info.eta_seconds !== undefined && info.eta_seconds !== null) bits.push("剩余约 " + fmt.dur(info.eta_seconds));
      const elapsed = info.elapsed !== undefined ? info.elapsed : null;
      if (elapsed !== null) bits.push("本阶段耗时 " + fmt.dur(elapsed));
      eta.textContent = bits.join(" · ");
    },
    reset(text) {
      bar.style.width = "0%";
      label.textContent = text || "当前没有正在跑的作业";
      eta.textContent = "";
    }
  };
}

export function mount(ctx) {
  const runId = ctx.params.id;
  const initialJob = ctx.query.job || "";
  let run = ctx.findRun(runId) || { id: runId, title: runId, stages: [], progress: {} };

  let es = null;
  let closedByUs = false;
  let pollTimer = null;

  const root = el("div", { class: "view" });

  // ---- 顶部 ----
  const titleNode = el("h1", { class: "page-title", text: run.title || runId });
  const idNode = el("span", { class: "run-id mono", text: runId });
  const stripBox = el("div", { class: "strip-box" });
  const abortBtn = el("button", {
    class: "btn btn-danger", type: "button",
    onclick: () => doAbort()
  }, "中断作业");
  const rerunAllBtn = el("button", {
    class: "btn btn-ghost", type: "button", title: "重新拉取 /api/state",
    onclick: () => refreshRun(true)
  }, "刷新状态");

  const head = el("div", { class: "page-head" },
    el("div", null,
      el("div", { class: "title-row" }, titleNode, idNode),
      el("div", { class: "page-sub", text: "流水线实时日志与阶段进度。" })
    ),
    el("div", { class: "page-head-actions" },
      el("a", { class: "btn btn-ghost", href: "#/run/" + encodeURIComponent(runId) + "/inspect" }, "下载报告 / 检视"),
      el("a", { class: "btn btn-ghost", href: "#/run/" + encodeURIComponent(runId) + "/triage" }, "去裁决台"),
      rerunAllBtn,
      abortBtn
    )
  );

  const runProgress = el("div", { class: "run-progress-line" });

  function renderStrip() {
    clear(stripBox).appendChild(stageStrip(run, "lg"));
    clear(runProgress);
    const p = run.progress || {};
    if (p.total) {
      runProgress.appendChild(el("span", { text: "已完成 " + (p.done || 0) + "/" + p.total + " 个阶段" }));
      if (p.current) runProgress.appendChild(el("span", { class: "dot-sep" }));
      if (p.current) runProgress.appendChild(el("span", { text: "阶段 " + (STAGE_LABELS[p.current] || p.current) }));
      runProgress.appendChild(el("span", { class: "dot-sep" }));
      runProgress.appendChild(el("span", { text: fmt.percent(p.percent) }));
    } else {
      runProgress.appendChild(el("span", { class: "muted", text: "后端未返回阶段进度。" }));
    }
    titleNode.textContent = run.title || runId;
  }

  // ---- 进度面板 ----
  const prog = stageProgressPanel();

  // ---- 日志 ----
  const logBody = el("div", { class: "log-body" });
  const logPanel = el("div", { class: "log-panel" });
  const backToBottom = el("button", {
    class: "btn btn-sm btn-backbottom", type: "button", hidden: true,
    onclick: () => scrollToBottom(true)
  }, "↓ 回到底部");
  const logCount = el("span", { class: "log-count muted", text: "0 行" });

  let stick = true;
  let lineCount = 0;

  function nearBottom() {
    return logPanel.scrollHeight - logPanel.scrollTop - logPanel.clientHeight < 40;
  }
  logPanel.addEventListener("scroll", () => {
    stick = nearBottom();
    backToBottom.hidden = stick;
  });
  function scrollToBottom(force) {
    if (force) stick = true;
    logPanel.scrollTop = logPanel.scrollHeight;
    backToBottom.hidden = stick;
  }

  function appendLine(text, kind) {
    const row = el("div", { class: "log-line" + (kind ? " log-" + kind : "") },
      el("span", { class: "log-text", text: text }));
    logBody.appendChild(row);
    lineCount++;
    while (logBody.childNodes.length > MAX_LOG_LINES) {
      logBody.removeChild(logBody.firstChild);
    }
    logCount.textContent = lineCount + " 行";
    // 只有用户没有手动往上滚时才自动跟随到底部
    if (stick) scrollToBottom(false);
    else backToBottom.hidden = false;
  }

  function appendLogEvent(data) {
    if (!data) return;
    const t = data.t ? fmt.time(data.t) : "";
    const stage = data.stage ? "<" + (STAGE_LABELS[data.stage] || data.stage) + "> " : "";
    appendLine((t ? t + " " : "") + stage + (data.line === undefined ? "" : String(data.line)));
  }

  // ---- 作业状态卡 ----
  const jobBox = el("div", { class: "job-box" });

  function renderJobBox(jobId) {
    clear(jobBox);
    if (jobId) {
      jobBox.appendChild(el("span", { class: "badge badge-ok", text: "作业 " + jobId }),
        el("span", { class: "muted", text: "正在通过 SSE 接收日志" }));
    } else {
      jobBox.appendChild(el("span", { class: "badge badge-noepub", text: "当前没有正在跑的作业" }),
        el("span", { class: "muted", text: "下面是每个阶段的重跑入口。" }));
    }
  }

  // ---- 阶段重跑区 ----
  const stageActions = el("div", { class: "stage-actions" });
  function renderStageActions() {
    clear(stageActions);
    stageActions.appendChild(el("div", { class: "approval-sub-title", text: "重跑某个阶段" }));
    const grid = el("div", { class: "stage-action-grid" });
    STAGES.forEach((name) => {
      const rec = (run.stages || []).filter((s) => s && s.stage === name)[0] || { status: "missing" };
      grid.appendChild(el("div", { class: "stage-action" },
        el("span", { class: "stage-action-name" }, STAGE_LABELS[name]),
        el("span", { class: "badge " + (rec.status === "ok" ? "badge-ok" : rec.status === "failed" ? "badge-bad" : rec.status === "missing" ? "badge-zero" : "badge-warn"),
          text: STATUS_LABELS[rec.status] || rec.status }),
        el("button", {
          class: "btn btn-sm", type: "button",
          onclick: (ev) => doRerun(name, ev.target)
        }, "重跑此阶段")
      ));
    });
    stageActions.appendChild(grid);
    stageActions.appendChild(el("div", { class: "muted stage-action-note",
      text: "重跑只执行该阶段；参数沿用当前 run 的已解析参数，需要改参数请回新建任务。" }));
  }

  // ---- 动作 ----
  let busy = false;
  async function doAbort() {
    if (busy) return;
    busy = true;
    abortBtn.disabled = true;
    abortBtn.textContent = "中断中…";
    try {
      await abortRun(runId);
      closedByUs = true;
      if (es) { try { es.close(); } catch (e) { /* 忽略 */ } }
      toast("已请求中断，后端正在清理子进程树", "ok");
      appendLine("[前端] 已请求中断", "sys");
      await refreshRun(true);
    } catch (err) {
      toast(describeError(err), "error", 8000);
    } finally {
      busy = false;
      abortBtn.disabled = false;
      abortBtn.textContent = "中断作业";
    }
  }

  async function doRerun(stage, btn) {
    if (btn) { btn.disabled = true; btn.textContent = "启动中…"; }
    try {
      const res = await rerunStage(runId, stage, {});
      const jobId = res && res.job_id ? res.job_id : "";
      toast("已启动阶段 " + (STAGE_LABELS[stage] || stage) + (jobId ? "（" + jobId + "）" : ""), "ok");
      if (jobId) {
        location.hash = "#/run/" + encodeURIComponent(runId) + "?job=" + encodeURIComponent(jobId);
      }
    } catch (err) {
      toast(describeError(err), "error", 8000);
      if (btn) { btn.disabled = false; btn.textContent = "重跑此阶段"; }
    }
  }

  async function refreshRun(full) {
    try {
      if (full) {
        const st = await getState();
        if (st) {
          ctx.store.state = st;
          ctx.store.lastLoadedAt = Date.now();
        }
      }
      const found = ctx.findRun(runId);
      if (found) run = found;
      renderStrip();
      renderStageActions();
    } catch (err) {
      toast("刷新状态失败：" + describeError(err), "error");
    }
  }

  // ---- SSE ----
  let jobId = initialJob;

  function connect(id) {
    jobId = id;
    renderJobBox(jobId);
    appendLine("[前端] 连接作业流 " + jobId, "sys");
    let sawTerminal = false;

    es = openJobStream(jobId, {
      open: () => appendLine("[前端] SSE 已连接", "sys"),
      log: (data) => appendLogEvent(data),
      stage: (data) => {
        if (!data) return;
        prog.set(data);
        if (data.stage) {
          const list = (run.stages || []).slice();
          let hit = false;
          for (let i = 0; i < list.length; i++) {
            if (list[i] && list[i].stage === data.stage) {
              list[i] = Object.assign({}, list[i], { status: "running" });
              hit = true;
            }
          }
          if (!hit) list.push({ stage: data.stage, status: "running", at: "", count: null, detail: "" });
          run = Object.assign({}, run, { stages: list });
          renderStrip();
        }
      },
      done: (data) => {
        sawTerminal = true;
        if (!data) return;
        const ok = data.status === "ok";
        appendLine("[前端] 阶段结束：" + (data.stage || "?") + " → " +
          (STATUS_LABELS[data.status] || data.status || "?") +
          (data.rc !== undefined ? "（rc=" + data.rc + "）" : "") +
          (data.elapsed !== undefined ? " 用时 " + fmt.dur(data.elapsed) : ""),
          ok ? "sys" : "warn");
        if (data.stage) {
          const list = (run.stages || []).slice();
          let hit = false;
          for (let i = 0; i < list.length; i++) {
            if (list[i] && list[i].stage === data.stage) {
              list[i] = Object.assign({}, list[i], { status: data.status, at: new Date().toISOString() });
              hit = true;
            }
          }
          if (!hit) list.push({ stage: data.stage, status: data.status, at: "", count: null, detail: "" });
          run = Object.assign({}, run, { stages: list });
          renderStrip();
        }
      },
      job: (data) => {
        sawTerminal = true;
        const status = data && data.status ? data.status : "unknown";
        appendLine("[前端] 作业结束：" + status + (data && data.rc !== undefined ? "（rc=" + data.rc + "）" : ""),
          status === "done" ? "sys" : "warn");
        prog.reset(status === "done" ? "作业已完成" : "作业已结束（" + status + "）");
        if (es) { try { es.close(); } catch (e) { /* 忽略 */ } }
        es = null;
        refreshRun(true);
      },
      error: () => {
        if (closedByUs) return;
        if (sawTerminal) {
          if (es) { try { es.close(); } catch (e) { /* 忽略 */ } }
          es = null;
          return;
        }
        appendLine("[前端] 日志流断开，尝试确认作业状态…", "warn");
        jobStatus(jobId).then((info) => {
          if (!info || info.status === "running") return;
          appendLine("[前端] 作业状态为 " + info.status + "，连接关闭", "sys");
          prog.reset("作业已结束（" + info.status + "）");
          if (es) { try { es.close(); } catch (e) { /* 忽略 */ } }
          es = null;
          refreshRun(true);
        }).catch(() => {
          appendLine("[前端] 无法确认作业状态，EventSource 将自动重连", "warn");
        });
      }
    });
  }

  // ---- 组装 ----
  root.appendChild(head);
  root.appendChild(el("section", { class: "card" }, stripBox, runProgress, prog.node));
  root.appendChild(el("section", { class: "card" },
    el("div", { class: "card-head" },
      el("h2", { class: "card-title", text: "作业" }),
      jobBox
    ),
    stageActions
  ));

  const logCard = el("section", { class: "card card-log" },
    el("div", { class: "card-head" },
      el("h2", { class: "card-title", text: "实时日志" }),
      el("span", { class: "spacer" }),
      logCount,
      el("button", {
        class: "btn btn-ghost btn-sm", type: "button",
        onclick: () => { clear(logBody); lineCount = 0; logCount.textContent = "0 行"; }
      }, "清屏")
    ),
    el("div", { class: "log-wrap" }, logPanel, backToBottom)
  );
  logPanel.appendChild(logBody);
  root.appendChild(logCard);

  renderStrip();
  renderJobBox(jobId);
  renderStageActions();

  if (jobId) {
    abortBtn.hidden = false;
    connect(jobId);
  } else {
    abortBtn.hidden = true;
    prog.reset("当前没有正在跑的作业");
    appendLine("[前端] 未提供 job 参数。若刚在别处启动过作业，请从启动结果跳转过来。", "sys");
  }

  // 无作业时也轮询一次状态，让阶段条不至于陈旧
  pollTimer = setInterval(() => {
    if (!jobId) refreshRun(false);
  }, 15000);

  root.__cleanup = () => {
    closedByUs = true;
    if (es) { try { es.close(); } catch (e) { /* 忽略 */ } }
    es = null;
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  };
  return root;
}
