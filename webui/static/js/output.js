// 成品（#/task/:id/output）。
// 契约见 webui/API.md §5：report / markdown / manifest / epub。
// 设计见 docs/UI-DESIGN.md §4.5。术语：成品、任务、校对、执行。
import { el, clear, fmt, errorBar, emptyState, skeleton,
         STAGES, STAGE_LABELS, STATUS_LABELS, statusClass } from "./ui.js";
import { getReport, getMarkdown, getManifest, epubUrl, describeError } from "./api.js";

// 任务 id 可能含斜杠，逐段编码
export function taskHash(id, view) {
  const parts = String(id || "").split("/").map(encodeURIComponent).join("/");
  return "#/task/" + parts + (view ? "/" + view : "");
}

const MD_HEAD_LINES = 200;
const QA_LEVEL = {
  error: { label: "错误", cls: "badge-danger" },
  warn: { label: "警告", cls: "badge-warn" },
  info: { label: "提示", cls: "badge-neutral" }
};

function qaBadge(level) {
  const cfg = QA_LEVEL[level] || QA_LEVEL.info;
  return el("span", { class: "badge " + cfg.cls, text: cfg.label });
}

function statCard(label, value, sub) {
  return el("div", { class: "card op-stat" },
    el("div", { class: "op-stat-value", text: value }),
    el("div", { class: "op-stat-label", text: label }),
    sub ? el("div", { class: "op-stat-sub", text: sub }) : null
  );
}

export function mount(ctx) {
  const taskId = ctx.params.id;
  const task = ctx.findRun(taskId);
  const title = (task && task.title) || taskId;
  const backHash = taskHash(taskId);

  const root = el("div", { class: "view view-output" });
  let alive = () => ctx.alive();

  // ---------- 顶部 ----------
  const fileName = (task && task.epub_name) || "";
  const headSub = el("div", { class: "page-sub", text: "成品是这本书的交付物。" });
  const downloadBtn = el("a", {
    class: "btn btn-primary btn-lg", href: epubUrl(taskId), download: fileName || "book.epub"
  }, "下载 EPUB");
  const headTitle = el("h1", { class: "page-title", text: title });

  const head = el("div", { class: "page-head op-head" },
    el("div", { class: "op-head-main" },
      el("div", { class: "op-head-title" }, headTitle,
        el("span", { class: "chip", text: taskId })),
      headSub
    ),
    el("div", { class: "page-head-actions op-head-actions" },
      el("a", { class: "btn", href: backHash }, "回到处理进度"),
      downloadBtn
    )
  );
  root.appendChild(head);

  const body = el("div", { class: "op-body" });
  root.appendChild(body);

  // ---------- 没有成品 ----------
  if (!task || !task.has_epub) {
    downloadBtn.setAttribute("hidden", "");
    headSub.textContent = task ? "这本书还没有成品。" : "找不到这个任务。";
    clear(body).appendChild(el("section", { class: "card" },
      el("div", { class: "card-body op-empty" },
        task
          ? emptyState("这本书还没有成品，先跑完审核与成品阶段",
              "成品阶段（EPUB）跑完后，这里会出现下载入口、目录树、质检问题与执行记录。",
              el("div", { class: "op-empty-actions" },
                el("a", { class: "btn btn-primary", href: backHash }, "回到处理进度")))
          : emptyState("找不到这个任务",
              "任务 " + taskId + " 不在当前工作目录下。可能是目录被移动，或者状态还没刷新。",
              el("div", { class: "op-empty-actions" },
                el("a", { class: "btn btn-primary", href: "#/tasks" }, "回到任务列表")))
      )));
    return root;
  }

  // ---------- EPUB 条 ----------
  const epubBar = el("section", { class: "card op-epub" },
    el("div", { class: "op-epub-main" },
      el("span", { class: "op-epub-icon", text: "EPUB" }),
      el("div", { class: "op-epub-text" },
        el("div", { class: "op-epub-name mono", text: fileName || "（未命名成品）" }),
        el("div", { class: "op-epub-sub faint", text: "点击右侧按钮下载到本地" })
      )
    ),
    el("div", { class: "op-epub-actions" },
      el("a", { class: "btn btn-primary", href: epubUrl(taskId), download: fileName || "book.epub" },
        "下载 EPUB")
    )
  );
  body.appendChild(epubBar);

  // ---------- 统计卡 ----------
  const statsHost = el("div", { class: "op-stats" });
  body.appendChild(statsHost);

  // ---------- 目录树 ----------
  const tocHost = el("section", { class: "card op-card" });
  body.appendChild(tocHost);

  // ---------- 质检问题 ----------
  const qaHost = el("section", { class: "card op-card" });
  body.appendChild(qaHost);

  // ---------- 执行记录 ----------
  const manifestHost = el("section", { class: "card op-card" });
  body.appendChild(manifestHost);

  // ---------- Markdown 预览 ----------
  const mdHost = el("section", { class: "card op-card" });
  body.appendChild(mdHost);

  // ============ 渲染：统计卡 ============
  function renderStats(rep) {
    clear(statsHost);
    const notes = rep.notes || {};
    const figures = rep.figures || {};
    statsHost.appendChild(statCard("页数", fmt.num(rep.pages), "整本书的页数"));
    statsHost.appendChild(statCard("汉字数", fmt.num(rep.cjk_chars), "正文汉字总量"));
    statsHost.appendChild(statCard("脚注",
      fmt.num(notes.total),
      "已关联 " + fmt.num(notes.linked || 0) + " · 孤立 " + fmt.num(notes.orphan || 0)));
    statsHost.appendChild(statCard("插图", fmt.num(figures.made),
      figures.files ? "图片文件 " + fmt.num(figures.files) + " 个" : "已生成的插图数量"));
  }

  // ============ 渲染：目录树 ============
  function renderToc(rep) {
    clear(tocHost);
    const toc = rep.toc || {};
    const tree = Array.isArray(toc.tree) ? toc.tree : [];
    const head = el("div", { class: "card-head" },
      el("h2", { class: "card-title", text: "目录" }),
      el("span", { class: "card-sub",
        text: "共 " + fmt.num(toc.entries || tree.length) + " 条 · 已匹配 " + fmt.num(toc.matched || 0)
          + (toc.source ? " · 来源 " + toc.source : "") })
    );
    tocHost.appendChild(head);

    if (!tree.length) {
      tocHost.appendChild(el("div", { class: "card-body" },
        emptyState("这本书没有目录树", "report.json 里没有 toc.tree，可能是版面阶段没有识别到目录。")));
      return;
    }

    const toggleBtn = el("button", { class: "btn btn-sm btn-ghost", type: "button" }, "全部折叠");
    head.appendChild(el("span", { class: "spacer" }));
    head.appendChild(toggleBtn);

    const list = el("div", { class: "op-toc" });
    let collapsed = false;

    function lineFor(entry, idx) {
      const level = Math.max(1, Math.min(6, Number(entry.level) || 1));
      const score = entry.score === null || entry.score === undefined ? null : Number(entry.score);
      const low = score !== null && !isNaN(score) && score < 1;
      const pageText = entry.page === null || entry.page === undefined ? "—" : "第 " + entry.page + " 页";
      const printedText = entry.printed_page === null || entry.printed_page === undefined
        ? "" : "（书内页 " + entry.printed_page + "）";

      const row = el("div", {
        class: "op-toc-row" + (collapsed && level > 1 ? " is-hidden" : ""),
        dataset: { level: String(level) },
        style: { paddingLeft: "calc(" + (level - 1) + " * 18px)" }
      },
        el("span", { class: "op-toc-title", text: entry.title || "（无标题）", title: entry.title || "" }),
        el("span", { class: "op-toc-dots" }),
        el("span", { class: "op-toc-page mono", text: pageText + printedText }),
        el("span", {
          class: "op-toc-score" + (low ? " is-low" : ""),
          title: low ? "目录标题与正文的匹配度偏低，建议人工核对" : "目录标题与正文的匹配度",
          text: score === null || isNaN(score) ? "—" : score.toFixed(2)
        })
      );
      row.dataset.idx = String(idx);
      return row;
    }

    function paint() {
      clear(list);
      tree.forEach((entry, idx) => list.appendChild(lineFor(entry, idx)));
    }
    paint();

    toggleBtn.addEventListener("click", () => {
      collapsed = !collapsed;
      toggleBtn.textContent = collapsed ? "全部展开" : "全部折叠";
      paint();
    });

    tocHost.appendChild(list);
    tocHost.appendChild(el("div", { class: "card-foot" },
      el("span", { class: "faint xs",
        text: "匹配度低于 1 的条目标黄，说明目录标题与正文里找到的位置不完全一致。" })));
  }

  // ============ 渲染：质检问题 ============
  function renderQa(rep) {
    clear(qaHost);
    const issues = (rep.qa && Array.isArray(rep.qa.issues)) ? rep.qa.issues : [];
    const counts = { error: 0, warn: 0, info: 0 };
    issues.forEach((it) => {
      const lv = it && it.level ? it.level : "info";
      counts[lv] = (counts[lv] || 0) + 1;
    });
    qaHost.appendChild(el("div", { class: "card-head" },
      el("h2", { class: "card-title", text: "质检问题" }),
      el("span", { class: "card-sub",
        text: "共 " + fmt.num(issues.length) + " 条"
          + (issues.length ? "（错误 " + counts.error + " · 警告 " + counts.warn + " · 提示 " + counts.info + "）" : "") })
    ));

    if (!issues.length) {
      qaHost.appendChild(el("div", { class: "card-body" },
        emptyState("没有质检问题", "report.json 的 qa.issues 是空的，说明这本书没有检出异常。")));
      return;
    }

    const table = el("table", { class: "table op-qa-table" },
      el("thead", null, el("tr", null,
        el("th", { style: { width: "80px" }, text: "等级" }),
        el("th", { style: { width: "90px" }, text: "页码" }),
        el("th", { text: "说明" })
      )),
      el("tbody", null, issues.map((it) => el("tr", null,
        el("td", null, qaBadge(it && it.level)),
        el("td", { class: "mono faint", text: it && it.page !== undefined && it.page !== null ? String(it.page) : "—" }),
        el("td", { text: it && it.text ? String(it.text) : "" })
      )))
    );
    qaHost.appendChild(el("div", { class: "op-table-wrap" }, table));
  }

  // ============ 渲染：执行记录 ============
  function renderManifest(doc) {
    clear(manifestHost);
    const stages = (doc && doc.stages) || {};
    const keys = Object.keys(stages);
    manifestHost.appendChild(el("div", { class: "card-head" },
      el("h2", { class: "card-title", text: "执行记录" }),
      el("span", { class: "card-sub", text: "这本书是怎么跑出来的" })
    ));

    const input = (doc && doc.input) || {};
    const params = (doc && doc.params) || {};
    const metaBits = [];
    if (input.pdf) metaBits.push("来源 PDF " + input.pdf);
    if (input.sha256) metaBits.push("指纹 " + String(input.sha256).slice(0, 12));
    Object.keys(params).forEach((k) => {
      const v = params[k];
      if (v === null || v === undefined || v === "") return;
      metaBits.push(k + " = " + (typeof v === "object" ? JSON.stringify(v) : String(v)));
    });

    if (!keys.length) {
      manifestHost.appendChild(el("div", { class: "card-body" },
        emptyState("这本书是旧布局，没有执行记录",
          "manifest.json 里没有阶段记录。旧布局的书在迁移到新布局之前，只能从产物反推跑过哪些阶段。")));
      return;
    }

    if (metaBits.length) {
      manifestHost.appendChild(el("div", { class: "op-manifest-meta" },
        metaBits.map((t) => el("span", { class: "chip", text: t }))));
    }

    const order = STAGES.filter((s) => keys.indexOf(s) >= 0)
      .concat(keys.filter((s) => STAGES.indexOf(s) < 0));
    const table = el("table", { class: "table op-manifest-table" },
      el("thead", null, el("tr", null,
        el("th", { text: "阶段" }),
        el("th", { style: { width: "110px" }, text: "状态" }),
        el("th", { style: { width: "180px" }, text: "结束时间" }),
        el("th", { style: { width: "110px" }, text: "耗时" }),
        el("th", { style: { width: "90px" }, text: "件数" }),
        el("th", { text: "说明" })
      )),
      el("tbody", null, order.map((name, idx) => {
        const rec = stages[name] || {};
        const status = rec.status || "missing";
        return el("tr", null,
          el("td", null,
            el("span", { class: "stage-dot " + statusClass(status), title: STATUS_LABELS[status] || status,
              style: { marginRight: "8px" }, text: String(idx + 1) }),
            el("span", { text: STAGE_LABELS[name] || name })),
          el("td", null, el("span", {
            class: "badge " + (status === "ok" ? "badge-ok" : status === "failed" ? "badge-danger"
              : status === "degraded" ? "badge-warn" : "badge-neutral"),
            text: STATUS_LABELS[status] || status
          })),
          el("td", { class: "mono faint", text: rec.at ? fmt.datetime(rec.at) : "—" }),
          el("td", { class: "mono", text: rec.elapsed !== undefined && rec.elapsed !== null ? fmt.dur(rec.elapsed) : "—" }),
          el("td", { class: "mono", text: rec.count !== undefined && rec.count !== null ? fmt.num(rec.count) : "—" }),
          el("td", { class: "muted", text: rec.reason ? String(rec.reason) : "" })
        );
      }))
    );
    manifestHost.appendChild(el("div", { class: "op-table-wrap" }, table));
  }

  // ============ 渲染：Markdown 预览 ============
  function renderMarkdown() {
    clear(mdHost);
    let showAll = false;
    let text = "";
    const pre = el("pre", { class: "op-md" });
    const info = el("span", { class: "card-sub", text: "" });
    const toggle = el("button", { class: "btn btn-sm", type: "button", hidden: true }, "全部");

    mdHost.appendChild(el("div", { class: "card-head" },
      el("h2", { class: "card-title", text: "Markdown 预览" }),
      info,
      el("span", { class: "spacer" }),
      toggle
    ));
    const scroll = el("div", { class: "op-md-scroll" });
    scroll.appendChild(pre);
    mdHost.appendChild(scroll);

    function paint() {
      const lines = text.split("\n");
      const shown = showAll ? lines : lines.slice(0, MD_HEAD_LINES);
      pre.textContent = shown.join("\n");
      info.textContent = "book.md · 共 " + fmt.num(lines.length) + " 行，当前显示前 "
        + fmt.num(shown.length) + " 行";
      toggle.hidden = lines.length <= MD_HEAD_LINES;
      toggle.textContent = showAll ? "只看前 200 行" : "全部（可能很慢）";
    }

    toggle.addEventListener("click", () => { showAll = !showAll; paint(); });

    clear(scroll);
    scroll.appendChild(skeleton(6));
    getMarkdown(taskId, "book.md").then((t) => {
      if (!alive()) return;
      text = t === null || t === undefined ? "" : String(t);
      clear(scroll);
      scroll.appendChild(pre);
      if (!text) {
        clear(scroll).appendChild(el("div", { class: "card-body" },
          emptyState("没有 Markdown 正文", "book.md 是空的，或者还没有生成。")));
        return;
      }
      paint();
    }).catch((err) => {
      if (!alive()) return;
      clear(scroll);
      scroll.appendChild(el("div", { class: "card-body" },
        errorBar("Markdown 读取失败：" + describeError(err)),
        el("div", { class: "row-gap", style: { marginTop: "12px" } },
          el("button", { class: "btn", type: "button", onclick: () => renderMarkdown() }, "重试"))));
    });
  }

  // ============ 拉数据 ============
  clear(statsHost).appendChild(el("div", { class: "op-loading" }, skeleton(3)));
  renderMarkdown();

  getReport(taskId).then((rep) => {
    if (!alive()) return;
    const r = rep || {};
    renderStats(r);
    renderToc(r);
    renderQa(r);
  }).catch((err) => {
    if (!alive()) return;
    clear(statsHost).appendChild(el("section", { class: "card" },
      el("div", { class: "card-body" },
        errorBar("读取报告失败：" + describeError(err)),
        el("div", { class: "row-gap", style: { marginTop: "12px" } },
          el("button", { class: "btn btn-primary", type: "button",
            onclick: () => location.reload() }, "重试")))));
    renderToc({});
    renderQa({});
  });

  getManifest(taskId).then((doc) => {
    if (!alive()) return;
    renderManifest(doc || {});
  }).catch((err) => {
    if (!alive()) return;
    clear(manifestHost).appendChild(el("div", { class: "card-head" },
      el("h2", { class: "card-title", text: "执行记录" })));
    manifestHost.appendChild(el("div", { class: "card-body" },
      errorBar("读取执行记录失败：" + describeError(err))));
  });

  root.__cleanup = () => { alive = () => false; };
  return root;
}
