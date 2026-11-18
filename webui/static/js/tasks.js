// 任务列表页（#/tasks）—— 每本书一张卡片。
// 数据来自 GET /api/state 的 runs[]（契约见 webui/API.md §2）。
// 设计规范见 docs/UI-DESIGN.md §4.1；本文件只做显示，不发明接口。
import { el, fmt, emptyState, stageStrip, STAGES, STAGE_LABELS } from "./ui.js";

// 任务标识可能含斜杠（新布局是 runs/<book-id>），逐段编码后拼哈希路由
function taskHref(runId, suffix) {
  const base = "#/task/" + String(runId || "").split("/").map(encodeURIComponent).join("/");
  return suffix ? base + "/" + suffix : base;
}

function layoutLabel(layout) {
  if (layout === "tiered") return "三层布局";
  if (layout === "flat") return "单层布局";
  return layout ? String(layout) : "布局未知";
}

function failedStages(run) {
  const list = Array.isArray(run && run.stages) ? run.stages : [];
  return list.filter((s) => s && s.status === "failed");
}

// 状态徽标：已出成品 = 绿；有失败阶段 = 红；否则灰
function statusBadge(run) {
  if (run && run.has_epub) {
    return el("span", {
      class: "badge badge-ok",
      title: run.epub_name ? "成品文件：" + run.epub_name : "已出成品"
    }, "已出成品");
  }
  const bad = failedStages(run);
  if (bad.length) {
    const names = bad.map((s) => STAGE_LABELS[s.stage] || s.stage).join("、");
    return el("span", { class: "badge badge-danger", title: "失败的阶段：" + names }, "有阶段失败");
  }
  const p = (run && run.progress) || {};
  const done = Number(p.done || 0);
  return el("span", { class: "badge badge-neutral", title: "还没跑完全部阶段" }, done > 0 ? "处理中" : "未开始");
}

function pendingBadge(run) {
  const pending = Number((run && run.pending_decisions) || 0);
  if (pending > 0) {
    return el("span", {
      class: "badge badge-warn",
      title: "校对清单里还没判定的条数（pending_decisions）"
    }, "待校对 " + fmt.num(pending) + " 条");
  }
  return el("span", { class: "badge badge-neutral", title: "没有待校对的条目" }, "无待校对");
}

function taskCard(run, navigate) {
  const title = run.title || run.id || "(未命名)";
  const pages = run.pages === null || run.pages === undefined ? "页数未知" : fmt.num(run.pages) + " 页";
  const p = run.progress || {};
  const done = Number(p.done || 0);
  const total = Number(p.total || STAGES.length);
  const href = taskHref(run.id);

  const card = el("article", {
    class: "card tk-card",
    tabindex: "0",
    role: "link",
    "aria-label": "打开任务：" + title,
    title: "打开处理进度：" + title,
    dataset: { id: run.id || "" }
  },
    el("div", { class: "tk-head" },
      el("h3", { class: "tk-title", text: title, title: title }),
      statusBadge(run)
    ),
    el("div", { class: "tk-meta" },
      el("span", { text: pages }),
      el("span", { class: "dot-sep", text: "·" }),
      el("span", { text: layoutLabel(run.layout) }),
      el("span", { class: "tk-rid mono", text: run.id || "-", title: "任务标识" })
    ),
    el("div", { class: "tk-stages" },
      stageStrip(run),
      el("span", { class: "tk-stage-count", text: done + "/" + total + " 阶段" })
    ),
    el("div", { class: "tk-badges" }, pendingBadge(run)),
    el("div", { class: "tk-actions" },
      el("a", { class: "btn btn-sm", href: href }, "打开"),
      el("a", { class: "btn btn-sm", href: taskHref(run.id, "proofread") }, "校对"),
      run.has_epub
        ? el("a", { class: "btn btn-sm", href: taskHref(run.id, "output") }, "成品")
        : el("button", {
            class: "btn btn-sm", type: "button", disabled: true,
            title: "这本书还没有成品，先跑完审核与打包阶段"
          }, "成品")
    )
  );

  const go = (ev) => {
    const t = ev.target;
    if (t && t.closest && t.closest("a,button")) return;
    navigate(href);
  };
  card.addEventListener("click", go);
  card.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      const t = ev.target;
      if (t && t.closest && t.closest("a,button")) return;
      ev.preventDefault();
      navigate(href);
    }
  });
  return card;
}

export function mount(ctx) {
  const state = ctx.store.state || {};
  const runs = Array.isArray(state.runs) ? state.runs : [];
  const navigate = (h) => ctx.navigate(h);

  const root = el("div", { class: "view view-tasks" });

  root.appendChild(el("div", { class: "page-head" },
    el("div", null,
      el("h1", { class: "page-title", text: "任务列表" }),
      el("p", { class: "page-sub", text: "工作目录下的全部书籍任务。点卡片进入处理进度。" })
    ),
    el("div", { class: "page-head-actions" },
      el("a", { class: "btn btn-primary", href: "#/new" }, "新建任务")
    )
  ));

  if (!runs.length) {
    root.appendChild(el("div", { class: "card" },
      el("div", { class: "empty" },
        el("div", { class: "empty-icon", text: "▤" }),
        el("div", { class: "empty-title", text: "还没有任何任务" }),
        el("div", { class: "empty-desc", text: "工作目录下没有找到书籍任务。上传一本 PDF，选好方案，就能开始处理。" }),
        el("a", { class: "btn btn-primary", href: "#/new" }, "新建任务")
      )
    ));
    return root;
  }

  const totalPending = runs.reduce((acc, r) => acc + Number((r && r.pending_decisions) || 0), 0);
  const epubCount = runs.filter((r) => r && r.has_epub).length;

  const search = el("input", {
    class: "input input-sm tk-search",
    type: "search",
    placeholder: "按书名 / id 搜索…",
    "aria-label": "按书名或 id 搜索任务",
    oninput: (ev) => filter(ev.target.value)
  });

  root.appendChild(el("div", { class: "tk-toolbar" },
    el("div", { class: "tk-summary" },
      el("span", null, "共 ", el("b", { text: fmt.num(runs.length) }), " 个任务"),
      el("span", { class: "dot-sep", text: "·" }),
      el("span", null, "待校对合计 ", el("b", { text: fmt.num(totalPending) }), " 条"),
      el("span", { class: "dot-sep", text: "·" }),
      el("span", null, "已出成品 ", el("b", { text: fmt.num(epubCount) }), " 本")
    ),
    search
  ));

  const grid = el("div", { class: "tk-grid" });
  const cards = [];
  runs.forEach((run) => {
    const card = taskCard(run, navigate);
    cards.push({
      node: card,
      hay: ((run.title || "") + " " + (run.id || "")).toLowerCase()
    });
    grid.appendChild(card);
  });

  const noResult = el("div", { class: "card tk-noresult", hidden: true },
    emptyState(
      "没有匹配的任务",
      "当前搜索词找不到任何书名或 id。换个关键词，或者清空搜索框看全部。",
      el("button", {
        class: "btn", type: "button",
        onclick: () => { search.value = ""; filter(""); search.focus(); }
      }, "清空搜索")
    )
  );

  root.appendChild(grid);
  root.appendChild(noResult);

  function filter(q) {
    const needle = String(q || "").trim().toLowerCase();
    let shown = 0;
    for (let i = 0; i < cards.length; i++) {
      const hit = !needle || cards[i].hay.indexOf(needle) >= 0;
      cards[i].node.hidden = !hit;
      if (hit) shown++;
    }
    grid.hidden = shown === 0;
    noResult.hidden = shown !== 0;
  }

  return root;
}