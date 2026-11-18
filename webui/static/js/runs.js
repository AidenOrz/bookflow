// 运行列表页（#/runs）：一张卡片一个 run。
import { el, fmt, emptyState, stageStrip } from "./ui.js";

function progressBar(p) {
  const percent = p && typeof p.percent === "number" ? Math.max(0, Math.min(100, p.percent)) : 0;
  const done = p && p.done !== undefined ? p.done : 0;
  const total = p && p.total !== undefined ? p.total : 0;
  return el("div", { class: "progress-wrap" },
    el("div", { class: "progress-bar" },
      el("div", { class: "progress-fill", style: { width: percent + "%" } })),
    el("div", { class: "progress-text" },
      el("span", { text: done + "/" + total + " 阶段" }),
      p && p.current ? el("span", { class: "muted", text: " · 当前 " + p.current }) : null,
      el("span", { class: "muted", text: " · " + fmt.percent(percent) })
    )
  );
}

function runCard(run) {
  const title = run.title || run.id || "(未命名)";
  const pending = Number(run.pending_decisions || 0);

  const card = el("div", { class: "run-card", dataset: { id: run.id || "" }, tabindex: "0" },
    el("div", { class: "run-card-head" },
      el("h3", { class: "run-title", text: title, title: title }),
      el("span", { class: "run-id mono", text: run.id || "-" })
    ),
    el("div", { class: "run-meta" },
      el("span", { text: "页数 " + (run.pages === null || run.pages === undefined ? "未知" : fmt.num(run.pages)) }),
      el("span", { class: "dot-sep" }),
      el("span", { text: "布局 " + (run.layout === "tiered" ? "三层" : run.layout === "flat" ? "单层" : (run.layout || "-")) }),
      run.path && run.path !== run.id ? el("span", { class: "dot-sep" }) : null,
      run.path && run.path !== run.id ? el("span", { class: "mono muted", text: run.path }) : null
    ),
    stageStrip(run),
    progressBar(run.progress || {}),
    el("div", { class: "run-card-foot" },
      pending > 0
        ? el("span", { class: "badge badge-pending", title: "decisions.json 中 verdict 为 ? 的条数", text: fmt.num(pending) + " 待裁决" })
        : el("span", { class: "badge badge-zero", text: "无待裁决" }),
      run.has_epub
        ? el("span", { class: "badge badge-epub", title: run.epub_name || "", text: "EPUB 就绪" })
        : el("span", { class: "badge badge-noepub", text: "暂无 EPUB" }),
      el("span", { class: "spacer" }),
      el("a", { class: "btn btn-sm", href: "#/run/" + encodeURIComponent(run.id) }, "看板"),
      el("a", { class: "btn btn-sm", href: "#/run/" + encodeURIComponent(run.id) + "/triage" }, "裁决"),
      el("a", { class: "btn btn-sm", href: "#/run/" + encodeURIComponent(run.id) + "/inspect" }, "检视")
    )
  );

  const go = (ev) => {
    const t = ev.target;
    if (t && t.closest && t.closest("a,button")) return;
    location.hash = "#/run/" + encodeURIComponent(run.id);
  };
  card.addEventListener("click", go);
  card.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      location.hash = "#/run/" + encodeURIComponent(run.id);
    }
  });
  return card;
}

export function mount(ctx) {
  const state = ctx.store.state || {};
  const runs = Array.isArray(state.runs) ? state.runs : [];
  const root = el("div", { class: "view" });

  root.appendChild(el("div", { class: "page-head" },
    el("div", null,
      el("h1", { class: "page-title", text: "运行列表" }),
      el("p", { class: "page-sub", text: "本地工作目录下的全部 bookflow 任务。点卡片进入流水线看板。" })
    ),
    el("div", { class: "page-head-actions" },
      el("a", { class: "btn btn-primary", href: "#/setup" }, "新建任务")
    )
  ));

  if (runs.length === 0) {
    root.appendChild(emptyState(
      "还没有任何运行记录",
      "工作目录里没有找到 runs/ 下的任务目录。先新建一个任务，上传 PDF 跑完整流水线。",
      el("a", { class: "btn btn-primary", href: "#/setup" }, "新建任务")
    ));
    return root;
  }

  const totalPending = runs.reduce((acc, r) => acc + Number(r.pending_decisions || 0), 0);
  const epubCount = runs.filter((r) => r.has_epub).length;
  root.appendChild(el("div", { class: "summary-bar" },
    el("span", { text: "共 " + runs.length + " 个任务" }),
    el("span", { class: "dot-sep" }),
    el("span", { text: "待裁决合计 " + fmt.num(totalPending) + " 条" }),
    el("span", { class: "dot-sep" }),
    el("span", { text: "已成书 " + epubCount + " 本" }),
    el("span", { class: "spacer" }),
    el("input", {
      class: "input input-sm", type: "search", placeholder: "按书名 / id 过滤…",
      oninput: (ev) => filter(ev.target.value)
    })
  ));

  const grid = el("div", { class: "run-grid" });
  const emptyBox = el("div", { class: "filter-empty muted", hidden: true, text: "没有匹配的任务。" });
  runs.forEach((r) => grid.appendChild(runCard(r)));
  root.appendChild(grid);
  root.appendChild(emptyBox);

  function filter(q) {
    const needle = String(q || "").trim().toLowerCase();
    let shown = 0;
    for (let i = 0; i < grid.children.length; i++) {
      const card = grid.children[i];
      const run = runs[i];
      const hay = ((run.title || "") + " " + (run.id || "")).toLowerCase();
      const hit = !needle || hay.indexOf(needle) >= 0;
      card.hidden = !hit;
      if (hit) shown++;
    }
    emptyBox.hidden = shown !== 0;
  }

  return root;
}
