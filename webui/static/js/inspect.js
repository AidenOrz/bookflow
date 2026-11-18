// 成书检视（#/run/:id/inspect）：报告、目录树、脚注健康、EPUB 下载、阅读预览、manifest。
import { el, clear, fmt, toast, errorBar, emptyState, skeleton } from "./ui.js";
import { getReport, getMarkdown, getManifest, epubUrl, describeError } from "./api.js";

function statCard(num, label, extra) {
  return el("div", { class: "stat-card" },
    el("div", { class: "stat-num", text: num }),
    el("div", { class: "stat-label", text: label }),
    extra ? el("div", { class: "stat-extra muted", text: extra }) : null
  );
}

function ratioBar(parts) {
  // parts: [{value, cls, label}]
  const total = parts.reduce((a, p) => a + Number(p.value || 0), 0) || 1;
  const bar = el("div", { class: "ratio-bar" });
  parts.forEach((p) => {
    const w = (Number(p.value || 0) / total) * 100;
    if (w <= 0) return;
    bar.appendChild(el("div", {
      class: "ratio-seg " + p.cls,
      style: { width: w + "%" },
      title: p.label + "：" + fmt.num(p.value)
    }));
  });
  return bar;
}

function levelClass(level) {
  const l = String(level || "").toLowerCase();
  if (l === "error" || l === "fatal") return "qa-error";
  if (l === "warn" || l === "warning") return "qa-warn";
  return "qa-info";
}

export function mount(ctx) {
  const runId = ctx.params.id;
  const run = ctx.findRun(runId);
  let drawer = null;
  let mdToken = 0;

  const root = el("div", { class: "view" });

  // ---- 头部 ----
  const epubHref = epubUrl(runId);
  // 后端没返回该 run 时按「有」处理，避免误判成未生成
  const hasEpub = run ? run.has_epub !== false : true;
  const epubBtn = el("a", {
    class: "btn btn-primary" + (hasEpub ? "" : " is-disabled"),
    href: hasEpub ? epubHref : "javascript:void(0)",
    download: hasEpub ? "" : null,
    title: hasEpub ? "下载 EPUB" : "这个 run 还没有生成 EPUB"
  }, "下载 EPUB");
  if (!hasEpub) {
    epubBtn.addEventListener("click", (ev) => {
      ev.preventDefault();
      toast("这个 run 还没有 EPUB。先把 epub 阶段跑完。", "error");
    });
  }

  const previewBtn = el("button", {
    class: "btn", type: "button",
    onclick: () => openReader()
  }, "阅读预览");

  root.appendChild(el("div", { class: "page-head" },
    el("div", null,
      el("h1", { class: "page-title", text: (run && run.title) || runId }),
      el("div", { class: "page-sub" },
        el("a", { class: "link", href: "#/run/" + encodeURIComponent(runId) }, "← 回到流水线看板"),
        el("span", { class: "dot-sep" }),
        el("span", { class: "mono", text: runId })
      )
    ),
    el("div", { class: "page-head-actions" }, previewBtn, epubBtn)
  ));

  const reportHost = el("div", { class: "report-host" }, skeleton(5));
  root.appendChild(reportHost);

  // ---- 阅读预览抽屉 ----
  function openReader() {
    if (drawer) return;
    const pre = el("pre", { class: "reader-text", text: "加载中…" });
    const info = el("div", { class: "reader-info muted", text: "GET /api/runs/" + runId + "/markdown?path=book.md" });
    const panel = el("div", { class: "drawer" },
      el("div", { class: "drawer-head" },
        el("h2", { class: "drawer-title", text: "阅读预览（纯文本，保留换行）" }),
        el("span", { class: "spacer" }),
        el("button", {
          class: "btn btn-sm btn-ghost", type: "button",
          onclick: () => { panel.classList.add("closing"); }
        }, "关闭")
      ),
      info,
      el("div", { class: "drawer-body" }, pre)
    );
    const mask = el("div", { class: "drawer-mask" });
    mask.addEventListener("click", () => panel.classList.add("closing"));
    panel.addEventListener("animationend", () => {});
    const close = () => {
      const t = ++mdToken;
      setTimeout(() => {
        if (t !== mdToken) return;
        if (mask.parentNode) mask.parentNode.removeChild(mask);
        if (panel.parentNode) panel.parentNode.removeChild(panel);
        drawer = null;
        document.removeEventListener("keydown", onKey);
      }, 160);
    };
    const onKey = (ev) => { if (ev.key === "Escape" && drawer) panel.classList.add("closing"); };
    panel.addEventListener("transitionend", () => {});
    const observer = setInterval(() => {
      if (panel.classList.contains("closing")) { clearInterval(observer); close(); }
    }, 80);
    document.addEventListener("keydown", onKey);
    document.body.appendChild(mask);
    document.body.appendChild(panel);
    requestAnimationFrame(() => panel.classList.add("open"));
    drawer = panel;

    getMarkdown(runId, "book.md").then((text) => {
      pre.textContent = text || "（空文件）";
      info.textContent = "共 " + String(text || "").length + " 字符";
    }).catch((err) => {
      const msg = describeError(err);
      pre.textContent = "";
      pre.appendChild(errorBar("读取 book.md 失败：" + msg));
      toast("阅读预览失败：" + msg, "error");
    });
  }

  // ---- 报告渲染 ----
  function renderReport(rep) {
    clear(reportHost);
    const book = rep.book || {};
    const toc = rep.toc || {};
    const notes = rep.notes || {};
    const qa = rep.qa || {};
    const fix = rep.fix || {};
    const figures = rep.figures || {};

    // 书信息
    reportHost.appendChild(el("section", { class: "card" },
      el("div", { class: "card-head" },
        el("h2", { class: "card-title", text: "书目信息" }),
        el("span", { class: "spacer" }),
        toc.source ? el("span", { class: "badge badge-zero", text: "目录来源 " + toc.source }) : null
      ),
      el("div", { class: "book-line" },
        el("div", { class: "book-field" }, el("span", { class: "k", text: "书名" }), el("span", { class: "v", text: book.title || "（空）" })),
        el("div", { class: "book-field" }, el("span", { class: "k", text: "作者" }), el("span", { class: "v", text: book.author || "（空）" })),
        el("div", { class: "book-field" }, el("span", { class: "k", text: "出版方" }), el("span", { class: "v", text: book.publisher || "（空）" }))
      )
    ));

    // 概览数字
    const stats = el("div", { class: "stat-row" });
    stats.appendChild(statCard(fmt.num(rep.pages), "页数"));
    stats.appendChild(statCard(fmt.num(rep.cjk_chars), "汉字数"));
    stats.appendChild(statCard(fmt.num(fix.fixed), "已修正", fix.candidates !== undefined ? "候选 " + fmt.num(fix.candidates) : ""));
    stats.appendChild(statCard(fmt.num(toc.entries), "目录条目", (toc.matched !== undefined ? "命中 " + fmt.num(toc.matched) : "")));
    stats.appendChild(statCard(fmt.num(figures.made), "插图", (figures.files !== undefined ? "文件 " + fmt.num(figures.files) : "")));
    reportHost.appendChild(stats);

    // 脚注健康
    const total = Number(notes.total || 0);
    const linked = Number(notes.linked || 0);
    const orphan = Number(notes.orphan || 0);
    const linkedRatio = total ? (linked / total * 100) : 0;
    reportHost.appendChild(el("section", { class: "card" },
      el("div", { class: "card-head" },
        el("h2", { class: "card-title", text: "脚注健康" }),
        el("span", { class: "spacer" }),
        el("span", { class: "badge " + (linkedRatio >= 70 ? "badge-ok" : linkedRatio >= 40 ? "badge-warn" : "badge-bad"),
          text: "可弹窗率 " + fmt.percent(linkedRatio) })
      ),
      el("div", { class: "notes-grid" },
        el("div", { class: "notes-num" }, el("div", { class: "stat-num", text: fmt.num(total) }), el("div", { class: "stat-label", text: "脚注总数 total" })),
        el("div", { class: "notes-num ok" }, el("div", { class: "stat-num", text: fmt.num(linked) }), el("div", { class: "stat-label", text: "已链接 linked" })),
        el("div", { class: "notes-num bad" }, el("div", { class: "stat-num", text: fmt.num(orphan) }), el("div", { class: "stat-label", text: "断链 orphan" })),
        el("div", { class: "notes-num" }, el("div", { class: "stat-num", text: fmt.num(notes.pages) }), el("div", { class: "stat-label", text: "涉及页数 pages" }))
      ),
      ratioBar([
        { value: linked, cls: "seg-ok", label: "已链接" },
        { value: orphan, cls: "seg-bad", label: "断链" }
      ])
    ));

    // 目录树
    const tree = Array.isArray(toc.tree) ? toc.tree : [];
    const tocCard = el("section", { class: "card" },
      el("div", { class: "card-head" },
        el("h2", { class: "card-title", text: "目录树" }),
        el("span", { class: "spacer" }),
        el("span", { class: "muted", text: tree.length + " 条" })
      ));
    if (tree.length === 0) {
      tocCard.appendChild(el("div", { class: "muted small-note", text: "report.json 里没有 toc.tree。" }));
    } else {
      const list = el("div", { class: "toc-tree" });
      tree.forEach((node) => {
        const level = Math.max(1, Math.min(6, Number(node.level || 1)));
        list.appendChild(el("div", { class: "toc-row lv" + level },
          el("span", { class: "toc-indent", text: "　".repeat(level - 1) }),
          el("span", { class: "toc-title", text: node.title || "(无标题)" }),
          el("span", { class: "spacer" }),
          node.printed_page !== null && node.printed_page !== undefined
            ? el("span", { class: "badge badge-zero mono", text: "印刷页 " + node.printed_page }) : null,
          node.page !== null && node.page !== undefined
            ? el("span", { class: "badge badge-st mono", text: "PDF 页 " + node.page }) : null,
          node.score !== null && node.score !== undefined
            ? el("span", { class: "toc-score muted mono", text: "score " + Number(node.score).toFixed(2) }) : null
        ));
      });
      tocCard.appendChild(list);
    }
    reportHost.appendChild(tocCard);

    // QA 问题
    const issues = (qa.issues || []).slice();
    const qaCard = el("section", { class: "card" },
      el("div", { class: "card-head" },
        el("h2", { class: "card-title", text: "QA 问题" }),
        el("span", { class: "spacer" }),
        el("span", { class: "badge " + (issues.length ? "badge-warn" : "badge-ok"), text: issues.length + " 条" })
      ));
    if (!issues.length) {
      qaCard.appendChild(el("div", { class: "muted small-note", text: "没有 QA 问题，成书质检干净。" }));
    } else {
      const list = el("div", { class: "qa-list" });
      issues.forEach((it) => {
        list.appendChild(el("div", { class: "qa-row " + levelClass(it.level) },
          el("span", { class: "qa-level", text: it.level || "info" }),
          it.page !== null && it.page !== undefined ? el("span", { class: "qa-page mono", text: "第 " + it.page + " 页" }) : null,
          el("span", { class: "qa-text", text: it.text || "" })
        ));
      });
      qaCard.appendChild(list);
    }
    reportHost.appendChild(qaCard);

    // manifest
    reportHost.appendChild(renderManifestCard());
  }

  // ---- manifest ----
  function renderManifestCard() {
    const pre = el("pre", { class: "manifest-pre", text: "加载中…" });
    const card = el("section", { class: "card" },
      el("div", { class: "card-head" },
        el("h2", { class: "card-title", text: "这次是怎么跑出来的" }),
        el("span", { class: "spacer" }),
        el("button", {
          class: "btn btn-sm btn-ghost", type: "button",
          onclick: () => {
            const txt = pre.textContent || "";
            if (navigator.clipboard && navigator.clipboard.writeText) {
              navigator.clipboard.writeText(txt).then(() => toast("manifest 已复制", "ok"))
                .catch(() => toast("复制失败，请手动选中", "error"));
            } else {
              toast("当前浏览器不支持剪贴板 API", "error");
            }
          }
        }, "复制")
      ),
      pre
    );
    getManifest(runId).then((mf) => {
      const obj = mf && typeof mf === "object" ? mf : {};
      const isEmpty = Object.keys(obj).length === 0;
      pre.textContent = JSON.stringify(obj, null, 2);
      if (isEmpty) {
        pre.textContent = "{}";
        pre.classList.add("is-empty");
        card.insertBefore(el("div", { class: "muted small-note", text: "后端没有返回 manifest.json（返回了 {}），说明这次运行没留下运行清单。" }), pre);
      }
    }).catch((err) => {
      pre.textContent = "";
      pre.appendChild(errorBar("manifest 加载失败：" + describeError(err)));
    });
    return card;
  }

  // ---- 加载 ----
  getReport(runId).then((rep) => {
    renderReport(rep || {});
  }).catch((err) => {
    const msg = describeError(err);
    clear(reportHost).appendChild(emptyState(
      "拿不到 report.json",
      msg + "（这个 run 可能还没跑完 verify / audit 阶段）",
      el("button", {
        class: "btn btn-primary", type: "button",
        onclick: () => { location.reload(); }
      }, "重新加载")
    ));
    toast("报告加载失败：" + msg, "error", 8000);
  });

  return root;
}
