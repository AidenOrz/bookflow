// 裁决台（#/run/:id/triage）——本工具最核心的界面。
// 契约见 webui/API.md 第 4 节：groups[] / unlocated[] / diff[] 全部按后端给的渲染，
// 前端不自己重算差异。
import { el, clear, fmt, toast, errorBar, emptyState, openModal, confirmDialog, skeleton } from "./ui.js";
import { getDecisions, postDecisions, applyPreview, applyRun, pageUrl, describeError } from "./api.js";

// 本地（未提交）状态：key -> {verdict, replacement}
// verdict 取值："" 表示未判定，"A" / "B" / "C" / "?" 表示已选
const VERDICT_TEXT = { A: "判 A", B: "判 B", C: "自定义", "?": "标 ?" };

function opClass(op) {
  if (op === "replace") return "d-replace";
  if (op === "insert") return "d-insert";
  if (op === "delete") return "d-delete";
  return "d-equal";
}

// 按 diff 数组渲染两行文本
// side = "a" | "b"
function renderDiffSide(diff, side) {
  const frag = document.createDocumentFragment();
  const list = Array.isArray(diff) ? diff : [];
  for (let i = 0; i < list.length; i++) {
    const d = list[i] || {};
    const op = d.op || "equal";
    if (side === "a") {
      // replace / delete / equal 在 A 侧有内容；insert 在 A 侧没有字
      if (op === "insert") continue;
      if (op === "equal") { frag.appendChild(document.createTextNode(d.a || "")); continue; }
      frag.appendChild(el("span", { class: "dl " + opClass(op), title: op }, d.a || ""));
    } else {
      if (op === "delete") continue;
      if (op === "equal") { frag.appendChild(document.createTextNode(d.b || "")); continue; }
      frag.appendChild(el("span", { class: "dl " + opClass(op), title: op }, d.b || ""));
    }
  }
  return frag;
}

// 兜底：后端万一没给 diff，就原样显示整行（绝不自己算差异）
function plainSide(text) {
  return document.createTextNode(text === null || text === undefined ? "" : String(text));
}

function apiPath(url) {
  // 后端可能返回绝对 URL；这里只取 path 部分，避免指向外部主机
  const s = String(url || "");
  const scheme = s.indexOf("://");
  if (scheme < 0) return s;
  const rest = s.slice(scheme + 3);
  const slash = rest.indexOf("/");
  return slash >= 0 ? rest.slice(slash) : "/";
}

export function mount(ctx) {
  const runId = ctx.params.id;
  let data = null;
  let pendingSubmit = false;
  let applying = false;

  // 本地未提交裁决
  const local = new Map();
  // 拍平后的条目列表
  let flat = [];
  let index = 0;
  let collapsed = new Set();     // 折叠的组
  let filterMode = "全部";
  let showPage = false;          // 证据区显示整页还是切片
  let customBtn = null;          // 中区「自定义」按钮，供 c 键触发

  const root = el("div", { class: "view view-triage" });

  // ---------------- 骨架 ----------------
  const banner = el("div", { class: "triage-banner", hidden: true });
  const statsBar = el("div", { class: "triage-stats" });
  const pendingBadge = el("span", { class: "badge badge-warn", text: "0 条待提交" });
  const submitBtn = el("button", { class: "btn btn-primary", type: "button", onclick: () => doSubmit() }, "提交全部裁决");
  const header = el("div", { class: "page-head triage-head" },
    el("div", null,
      el("h1", { class: "page-title", text: "裁决台" }),
      el("div", { class: "page-sub" },
        el("a", { class: "link", href: "#/run/" + encodeURIComponent(runId) }, "← 回到流水线看板"),
        el("span", { class: "dot-sep" }),
        el("span", { class: "mono", text: runId })
      )
    ),
    el("div", { class: "page-head-actions" },
      pendingBadge, submitBtn
    )
  );

  const layout = el("div", { class: "triage-layout" });
  const leftCol = el("aside", { class: "triage-left" });
  const centerCol = el("main", { class: "triage-center" });
  const rightCol = el("aside", { class: "triage-right" });
  layout.appendChild(leftCol);
  layout.appendChild(centerCol);
  layout.appendChild(rightCol);

  root.appendChild(header);
  root.appendChild(banner);
  root.appendChild(statsBar);
  root.appendChild(layout);

  // ---------------- 左区：证据图 ----------------
  const evidenceHost = el("div", { class: "card evidence-card" });

  function renderEvidence(item) {
    clear(evidenceHost);
    evidenceHost.appendChild(el("div", { class: "card-head" },
      el("h2", { class: "card-title", text: "证据图" }),
      el("span", { class: "spacer" }),
      item ? el("span", { class: "badge badge-zero mono", text: item.page || "" }) : null
    ));

    if (!item) {
      evidenceHost.appendChild(el("div", { class: "empty small" },
        el("div", { class: "empty-title", text: "没有选中条目" })));
      return;
    }

    const cropSrc = item.crop_url ? apiPath(item.crop_url) : "";
    const pageSrc = item.page_url ? apiPath(item.page_url) : pageUrl(runId, item.page, 1.0);

    if (!item.y0 && item.y0 !== 0) {
      evidenceHost.appendChild(el("div", { class: "warning-box" },
        el("div", { class: "warning-title", text: "该条目没有 y0 定位" }),
        el("div", { class: "warning-text", text: "需要人翻原书找到这一行，在右侧填写自定义整行文本后判 C，或先标为 ?。" })));
    }

    // 没有切片（未定位条目）时退回整页，避免给用户一个空图
    const usePage = showPage || !cropSrc;
    const img = el("img", {
      class: "evidence-img" + (usePage ? " is-page" : ""),
      alt: usePage ? "整页原始扫描" : "整行切片",
      src: usePage ? pageSrc : cropSrc
    });
    img.addEventListener("error", () => {
      img.replaceWith(el("div", { class: "image-fail" },
        el("div", { text: "图片加载失败" }),
        el("div", { class: "mono muted", text: usePage ? pageSrc : cropSrc })));
    });
    const frame = el("div", { class: "evidence-frame" }, img);

    const toggle = el("div", { class: "evidence-toggle" },
      el("button", {
        class: "btn btn-sm" + (usePage ? "" : " btn-primary"), type: "button",
        disabled: !cropSrc,
        title: cropSrc ? "只看这一行" : "该条目没有切片（缺 y0），只能看整页",
        onclick: () => { showPage = false; renderEvidence(current()); }
      }, "行切片"),
      el("button", {
        class: "btn btn-sm" + (usePage ? " btn-primary" : ""), type: "button",
        onclick: () => { showPage = true; renderEvidence(current()); }
      }, "看整页"),
      el("a", {
        class: "btn btn-sm btn-ghost",
        href: cropSrc || pageSrc, target: "_blank", rel: "noopener"
      }, "新标签打开")
    );

    evidenceHost.appendChild(toggle);
    evidenceHost.appendChild(frame);
    evidenceHost.appendChild(el("div", { class: "evidence-note muted mono",
      text: (usePage ? "整页：" : "切片：") + (usePage ? pageSrc : cropSrc) }));

    // 整页弹窗按钮（题目要求下面一行小按钮 + 弹窗）
    evidenceHost.appendChild(el("div", { class: "evidence-toggle" },
      el("button", {
        class: "btn btn-sm btn-ghost", type: "button",
        onclick: () => openPageModal(pageSrc, item)
      }, "在弹窗里看整页")
    ));
  }

  function openPageModal(src, item) {
    const img = el("img", { class: "modal-page-img", src: src, alt: "整页原始扫描" });
    img.addEventListener("error", () => {
      img.replaceWith(errorBar("整页图片加载失败：" + src));
    });
    openModal({
      title: "整页原图 · " + (item && item.page ? item.page : ""),
      width: 1100,
      body: [el("div", { class: "modal-page-wrap" }, img)],
      actions: [{ label: "关闭", kind: "ghost" }]
    });
  }

  // ---------------- 左区：组列表与未定位 ----------------
  const sideHost = el("div", { class: "card side-card" });

  function allItems() {
    const out = [];
    const unlocated = (data && data.unlocated) || [];
    for (let i = 0; i < unlocated.length; i++) {
      out.push({ item: unlocated[i], group: null, groupKey: "__unlocated__", unlocated: true });
    }
    const groups = (data && data.groups) || [];
    for (let g = 0; g < groups.length; g++) {
      const grp = groups[g] || {};
      const items = grp.items || [];
      for (let i = 0; i < items.length; i++) {
        out.push({ item: items[i], group: grp, groupKey: grp.group || ("__g" + g), unlocated: false });
      }
    }
    return out;
  }

  function verdictOf(item) {
    const rec = local.get(item.key);
    if (rec) return rec.verdict;
    return item.verdict || "";
  }

  function statusOf(item) {
    const v = verdictOf(item);
    if (v) return "decided";
    return item.status || "pending";
  }

  function isPending(item) {
    return !verdictOf(item);
  }

  // 「N 条待提交」= 本地已判定但尚未提交的条目数
  function countPending() {
    let n = 0;
    local.forEach((rec) => { if (rec && rec.verdict) n++; });
    return n;
  }

  function updatePendingBadge() {
    const n = countPending();
    pendingBadge.textContent = n + " 条待提交";
    pendingBadge.className = "badge " + (n > 0 ? "badge-warn" : "badge-ok");
    submitBtn.disabled = n === 0 || pendingSubmit || applying;
    submitBtn.textContent = pendingSubmit ? "提交中…" : "提交全部裁决";
  }

  function markLocal(item, verdict, replacement) {
    if (verdict === "" || verdict === null || verdict === undefined) local.delete(item.key);
    else local.set(item.key, { verdict: verdict, replacement: replacement === undefined ? null : replacement });
    updatePendingBadge();
    renderSidebar();
  }

  function renderSidebar() {
    clear(sideHost);
    const groups = (data && data.groups) || [];
    const unlocated = (data && data.unlocated) || [];

    sideHost.appendChild(el("div", { class: "card-head" },
      el("h2", { class: "card-title", text: "词对分组" }),
      el("span", { class: "spacer" }),
      el("span", { class: "muted", text: groups.length + " 组" })
    ));

    // 未定位区永远排最前
    const unBox = el("div", { class: "group-block unlocated-block" });
    unBox.appendChild(el("div", { class: "group-head" },
      el("button", {
        class: "group-toggle", type: "button",
        onclick: () => { toggleCollapse("__unlocated__"); }
      },
        el("span", { class: "caret", text: collapsed.has("__unlocated__") ? "▸" : "▾" }),
        el("span", { class: "group-name", text: "未定位条目" }),
        el("span", { class: "badge badge-bad", text: String(unlocated.length) })
      )
    ));
    if (!collapsed.has("__unlocated__") && unlocated.length) {
      const list = el("div", { class: "group-items" });
      unlocated.forEach((it) => {
        list.appendChild(entryRow(it, null, true));
      });
      unBox.appendChild(list);
    }
    sideHost.appendChild(unBox);

    if (unlocated.length === 0) {
      sideHost.appendChild(el("div", { class: "muted small-note", text: "没有未定位条目。" }));
    }

    // 组
    const groupList = el("div", { class: "group-list" });
    groups.forEach((grp, gi) => {
      const key = grp.group || ("__g" + gi);
      const isCollapsed = collapsed.has(key);
      const items = grp.items || [];
      const undone = items.filter(isPending).length;
      const block = el("div", { class: "group-block" });
      block.appendChild(el("div", { class: "group-head" },
        el("button", {
          class: "group-toggle", type: "button",
          onclick: () => toggleCollapse(key)
        },
          el("span", { class: "caret", text: isCollapsed ? "▸" : "▾" }),
          el("span", { class: "group-name mono", text: key }),
          el("span", { class: "badge " + (undone ? "badge-warn" : "badge-ok"), text: undone + "/" + items.length })
        ),
        el("button", {
          class: "btn btn-sm btn-batch", type: "button",
          title: "把组内未判定条目本地标记为 B（不提交）",
          onclick: (ev) => batchGroup(grp, "B", ev.target)
        }, "整组判 B"),
        undone === items.length ? null : el("button", {
          class: "btn btn-sm btn-ghost", type: "button",
          title: "撤销本组未提交的本地判定",
          onclick: () => { items.forEach((it) => local.delete(it.key)); updatePendingBadge(); renderSidebar(); renderCenter(); }
        }, "撤销本组")
      ));
      if (grp.note) block.appendChild(el("div", { class: "group-note muted", text: grp.note }));
      if (!isCollapsed) {
        const list = el("div", { class: "group-items" });
        items.forEach((it) => list.appendChild(entryRow(it, grp, false)));
        block.appendChild(list);
      }
      groupList.appendChild(block);
    });
    sideHost.appendChild(groupList);

    if (groups.length === 0 && unlocated.length === 0) {
      sideHost.appendChild(el("div", { class: "empty small" },
        el("div", { class: "empty-title", text: "没有需要裁决的条目" }),
        el("div", { class: "empty-desc", text: "换一个 only 过滤，或去跑 audit 阶段生成裁决表。" })));
    }
  }

  function toggleCollapse(key) {
    if (collapsed.has(key)) collapsed.delete(key);
    else collapsed.add(key);
    renderSidebar();
    renderFilterBar();
  }

  function entryRow(item, grp, unlocated) {
    const v = verdictOf(item);
    const myIdx = flat.findIndex((f) => f.item.key === item.key);
    const cur = myIdx === index;
    const row = el("div", {
      class: "entry-row" + (cur ? " active" : "") + (v ? " decided-" + v : ""),
      onclick: () => goTo(myIdx)
    },
      el("span", { class: "entry-state " + (v ? "v-" + v : "v-none"), text: v || "·" }),
      el("span", { class: "entry-text", text: fmt.truncate(item.B || item.A || item.key, 48) }),
      unlocated ? el("span", { class: "entry-flag", text: "未定位" }) : null
    );
    return row;
  }

  // 批量：整组判 B（只改本地状态，不提交）
  function batchGroup(grp, verdict, btn) {
    const items = grp.items || [];
    let n = 0;
    items.forEach((it) => {
      if (isPending(it)) { local.set(it.key, { verdict: verdict, replacement: null }); n++; }
    });
    updatePendingBadge();
    renderSidebar();
    renderCenter();
    if (n) toast("已本地标记 " + n + " 条为 " + verdict + "（尚未提交）", "ok");
    else toast("该组没有未判定的条目", "info");
  }

  // ---------------- 中区：候选对比 ----------------
  const centerHost = el("div", { class: "card compare-card" });
  const filterBar = el("div", { class: "filter-bar" });

  function renderFilterBar() {
    clear(filterBar);
    const opts = ["全部", "未判定", "已判定"];
    opts.forEach((o) => {
      filterBar.appendChild(el("button", {
        class: "chip-filter" + (filterMode === o ? " on" : ""), type: "button",
        onclick: () => { filterMode = o; rebuildFlat(); renderFilterBar(); renderCenter(); }
      }, o));
    });
    filterBar.appendChild(el("span", { class: "spacer" }));
    const idxText = flat.length ? (index + 1) + " / " + flat.length : "0 / 0";
    filterBar.appendChild(el("span", { class: "muted mono", text: idxText }));
  }

  function rebuildFlat() {
    const all = allItems();
    const keepKey = flat[index] ? flat[index].item.key : null;
    flat = all.filter((row) => {
      if (filterMode === "未判定") return isPending(row.item);
      if (filterMode === "已判定") return !isPending(row.item);
      return true;
    });
    if (!flat.length) { index = 0; return; }
    let found = keepKey ? flat.findIndex((f) => f.item.key === keepKey) : -1;
    index = found >= 0 ? found : Math.min(index, flat.length - 1);
  }

  function current() {
    return flat[index] ? flat[index].item : null;
  }

  function goTo(i) {
    if (i < 0 || i >= flat.length) return;
    index = i;
    renderCenter();
    renderSidebar();
  }

  function renderCenter() {
    clear(centerHost);
    if (!data) {
      centerHost.appendChild(skeleton(3));
      return;
    }
    if (!flat.length) {
      centerHost.appendChild(emptyState(
        filterMode === "全部" ? "没有需要裁决的条目" : "当前过滤下没有条目",
        filterMode === "全部"
          ? "这个 run 的 decisions.json 里没有待裁决项，或者 audit 阶段还没跑。"
          : "切换过滤条件试试。"
      ));
      return;
    }
    const item = current();
    if (!item) return;

    const myVerdict = verdictOf(item);
    const row = flat[index];
    const grp = row ? row.group : null;

    centerHost.appendChild(filterBar);

    // 卡片头
    centerHost.appendChild(el("div", { class: "compare-head" },
      el("span", { class: "compare-pos mono", text: (index + 1) + "/" + flat.length }),
      el("span", { class: "mono muted", text: item.page || "-" }),
      el("span", { class: "dot-sep" }),
      el("span", { class: "mono muted", text: item.y0 === null || item.y0 === undefined ? "y0 未知" : "y0=" + item.y0 }),
      el("span", { class: "spacer" }),
      grp ? el("span", { class: "badge badge-zero mono", text: "组 " + (grp.group || "") }) : null,
      item.status ? el("span", { class: "badge badge-st", text: "status: " + item.status }) : null,
      el("span", { class: "badge " + (myVerdict ? "badge-warn" : "badge-zero"),
        text: myVerdict ? "本地：判 " + myVerdict : "未判定" })
    ));

    const diff = Array.isArray(item.diff) ? item.diff : null;
    const aText = el("span", { class: "cand-text" });
    const bText = el("span", { class: "cand-text" });
    if (diff) {
      aText.appendChild(renderDiffSide(diff, "a"));
      bText.appendChild(renderDiffSide(diff, "b"));
    } else {
      aText.appendChild(plainSide(item.A));
      bText.appendChild(plainSide(item.B));
      aText.appendChild(el("span", { class: "muted small-note", text: "（后端未给 diff，原样显示）" }));
    }
    const aLine = el("div", { class: "cand-line cand-line-a" },
      el("span", { class: "cand-tag tag-a", text: "A" }),
      aText,
      el("button", {
        class: "cand-pick", type: "button", title: "采纳 A（键盘 a / 1）",
        onclick: () => { pick("A"); }
      }, "选 A")
    );
    const bLine = el("div", { class: "cand-line cand-line-b" },
      el("span", { class: "cand-tag tag-b", text: "B" }),
      bText,
      el("button", {
        class: "cand-pick", type: "button", title: "采纳 B（键盘 b / 2）",
        onclick: () => { pick("B"); }
      }, "选 B")
    );
    const noCandidates = !item.A && !item.B;
    if (noCandidates) {
      centerHost.appendChild(el("div", { class: "warning-box" },
        el("div", { class: "warning-title", text: "这一条还没定位到原书行（A / B 都为空）" }),
        el("div", { class: "warning-text",
          text: (item.suggestion ? "建议：" + item.suggestion + "。" : "") +
            (item.why ? item.why + "。" : "") +
            " 请翻原书找到这一行，按 c 直接写出改好的整行（记为 C），或按空格先标 ? 留待处理。" })
      ));
      if (grp && grp.note) {
        centerHost.appendChild(el("div", { class: "muted small-note", text: "组备注：" + grp.note }));
      }
    }
    centerHost.appendChild(el("div", { class: "cand-box" }, aLine, bLine));

    // 自定义替换行输入
    const customRow = el("div", { class: "custom-row", hidden: true });
    const customInput = el("input", {
      class: "input mono", type: "text", placeholder: "整行原文改好后的完整一行，Enter 确认 / Esc 取消"
    });
    const customTag = el("span", { class: "cand-tag tag-c", text: "C" });
    customRow.appendChild(customTag);
    customRow.appendChild(customInput);
    centerHost.appendChild(customRow);

    function openCustom() {
      customRow.hidden = false;
      const rec = local.get(item.key);
      customInput.value = rec && rec.replacement ? rec.replacement : (item.replacement || item.B || "");
      customInput.focus();
      customInput.select();
    }
    customInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        const v = customInput.value;
        if (!v.trim()) { toast("自定义整行不能为空", "error"); return; }
        local.set(item.key, { verdict: "C", replacement: v });
        customRow.hidden = true;
        updatePendingBadge();
        renderSidebar();
        renderCenter();
        toast("已记为 C（自定义整行），按 Enter 提交并前进", "ok");
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        customRow.hidden = true;
      }
      ev.stopPropagation();
    });

    // 操作条
    customBtn = el("button", {
      class: "btn" + (myVerdict === "C" ? " btn-primary" : ""), type: "button",
      onclick: () => { openCustom(); }
    }, "自定义 (c)");
    const actionBar = el("div", { class: "action-bar" },
      el("button", { class: "btn" + (myVerdict === "A" ? " btn-primary" : ""), type: "button", onclick: () => pick("A") }, "判 A (a)"),
      el("button", { class: "btn" + (myVerdict === "B" ? " btn-primary" : ""), type: "button", onclick: () => pick("B") }, "判 B (b)"),
      customBtn,
      el("button", { class: "btn" + (myVerdict === "?" ? " btn-primary" : ""), type: "button", onclick: () => pick("?") }, "标 ? (空格)"),
      el("span", { class: "spacer" }),
      el("button", { class: "btn btn-ghost", type: "button", onclick: () => { local.delete(item.key); updatePendingBadge(); renderSidebar(); renderCenter(); } }, "清除本条"),
      el("button", { class: "btn btn-primary", type: "button", onclick: () => commitCurrent() }, "提交并下一条 (Enter)")
    );
    centerHost.appendChild(actionBar);

    // 右区
    renderRight(item, grp, myVerdict);
    renderEvidence(item);
  }

  // ---------------- 右区 ----------------
  function renderRight(item, grp, myVerdict) {
    clear(rightCol);
    const card = el("div", { class: "card note-card" });
    card.appendChild(el("div", { class: "card-head" },
      el("h2", { class: "card-title", text: "证据与操作" })));

    const note = item.note || (grp && grp.note) || "";
    card.appendChild(el("div", { class: "note-block" },
      el("div", { class: "note-label", text: "词频证据 / 说明" }),
      el("div", { class: "note-text", text: note || "（后端未给出说明）" }),
      item.suggestion ? el("div", { class: "note-text" }, el("span", { class: "note-label", text: "建议 " }), item.suggestion) : null,
      item.why ? el("div", { class: "note-text muted", text: "原因：" + item.why }) : null
    ));

    card.appendChild(el("div", { class: "note-block" },
      el("div", { class: "note-label", text: "状态" }),
      el("div", { class: "note-row" },
        el("span", { class: "badge badge-st", text: "后端： " + (item.status || "未知") }),
        el("span", { class: "badge " + (myVerdict ? "badge-warn" : "badge-zero"),
          text: "本地： " + (myVerdict ? VERDICT_TEXT[myVerdict] || myVerdict : "未判定") })
      )
    ));

    if (item.replacement) {
      card.appendChild(el("div", { class: "note-block" },
        el("div", { class: "note-label", text: "已有自定义整行" }),
        el("div", { class: "note-text mono", text: item.replacement })
      ));
    }

    card.appendChild(el("div", { class: "note-block" },
      el("div", { class: "note-label", text: "A / B 原文" }),
      el("div", { class: "note-text mono" },
        el("div", { class: "kv-line" }, el("span", { class: "kv-k", text: "A" }), el("span", { text: item.A || "" })),
        el("div", { class: "kv-line" }, el("span", { class: "kv-k", text: "B" }), el("span", { text: item.B || "" })),
        el("div", { class: "kv-line" }, el("span", { class: "kv-k", text: "key" }), el("span", { text: item.key || "" }))
      )
    ));

    const help = el("div", { class: "kbd-help" });
    help.appendChild(el("div", { class: "note-label", text: "键盘" }));
    const rows = [
      ["a / 1", "判 A"], ["b / 2", "判 B"], ["c / 3", "自定义整行"],
      ["空格", "标 ?"], ["j / ↓", "下一条"], ["k / ↑", "上一条"],
      ["Enter", "提交并前进"], ["u", "跳到未定位区"]
    ];
    rows.forEach(([k, v]) => {
      help.appendChild(el("div", { class: "kbd-row" },
        el("kbd", { text: k }), el("span", { text: v })));
    });
    help.appendChild(el("div", { class: "muted small-note", text: "焦点在输入框内时快捷键全部禁用。" }));
    card.appendChild(help);

    rightCol.appendChild(card);
  }

  // ---------------- 本地判定动作 ----------------
  function pick(verdict) {
    const item = current();
    if (!item) return;
    if (verdict === "C") { local.set(item.key, { verdict: "C", replacement: item.B || "" }); }
    else local.set(item.key, { verdict: verdict, replacement: null });
    updatePendingBadge();
    renderSidebar();
    renderCenter();
  }

  async function commitCurrent() {
    const item = current();
    if (!item) return;
    if (!local.has(item.key) && item.verdict) {
      // 后端已有裁决，直接前进
      next();
      return;
    }
    if (!local.has(item.key)) {
      toast("这张卡还没判定，先按 a/b/c/空格 选一个", "info");
      return;
    }
    const rec = local.get(item.key);
    await submitUpdate([{ key: item.key, verdict: rec.verdict, replacement: rec.verdict === "C" ? rec.replacement : null }], true);
  }

  function next() {
    if (index < flat.length - 1) { index++; renderCenter(); renderSidebar(); }
    else toast("已经是最后一条了", "info");
  }

  function prev() {
    if (index > 0) { index--; renderCenter(); renderSidebar(); }
  }

  // 提交单条或多条
  async function submitUpdate(updates, advance) {
    if (pendingSubmit) return;
    pendingSubmit = true;
    updatePendingBadge();
    try {
      const payload = updates.map((u) => {
        const o = { key: u.key, verdict: u.verdict };
        if (u.verdict === "C") o.replacement = u.replacement || "";
        return o;
      });
      const res = await postDecisions(runId, payload);
      const changed = res && res.changed !== undefined ? res.changed : 0;
      updates.forEach((u) => local.delete(u.key));
      // 本地同步后端状态
      updates.forEach((u) => {
        const it = findItem(u.key);
        if (it) { it.verdict = u.verdict; it.status = "decided"; }
      });
      toast("已提交 " + changed + " 条", "ok");
      updatePendingBadge();
      renderSidebar();
      if (advance) next(); else renderCenter();
    } catch (err) {
      const msg = describeError(err);
      const errs = err && err.payload && err.payload.errors ? err.payload.errors : null;
      const extra = errs && errs.length ? el("pre", { class: "err-pre", text: errs.map((e) => typeof e === "string" ? e : JSON.stringify(e)).join("\n") }) : null;
      toast(msg, "error", 9000);
      if (extra) openModal({ title: "提交被拒绝", body: [errorBar(msg), extra], actions: [{ label: "知道了", kind: "ghost" }] });
    } finally {
      pendingSubmit = false;
      updatePendingBadge();
    }
  }

  function findItem(key) {
    for (let i = 0; i < flat.length; i++) if (flat[i].item.key === key) return flat[i].item;
    if (data) {
      const gs = data.groups || [];
      for (let g = 0; g < gs.length; g++) {
        const items = gs[g].items || [];
        for (let i = 0; i < items.length; i++) if (items[i].key === key) return items[i];
      }
      const us = data.unlocated || [];
      for (let i = 0; i < us.length; i++) if (us[i].key === key) return us[i];
    }
    return null;
  }

  // ---------------- 提交全部→审批闭环 ----------------
  async function doSubmit() {
    if (pendingSubmit || applying) return;
    const updates = [];
    local.forEach((rec, key) => {
      updates.push({ key: key, verdict: rec.verdict, replacement: rec.verdict === "C" ? rec.replacement : null });
    });
    if (!updates.length) { toast("没有待提交的裁决", "info"); return; }

    const byVerdict = { A: 0, B: 0, C: 0, "?": 0 };
    updates.forEach((u) => { byVerdict[u.verdict] = (byVerdict[u.verdict] || 0) + 1; });

    const list = el("div", { class: "confirm-list" });
    updates.slice(0, 200).forEach((u) => {
      const it = findItem(u.key);
      list.appendChild(el("div", { class: "confirm-row mono" },
        el("span", { class: "badge " + (u.verdict === "C" ? "badge-bad" : "badge-warn"), text: u.verdict }),
        el("span", { class: "confirm-key", text: u.key }),
        el("span", { class: "confirm-text", text: u.verdict === "C" ? (u.replacement || "") : fmt.truncate((it && (u.verdict === "A" ? it.A : it.B)) || "", 70) })
      ));
    });
    if (updates.length > 200) {
      list.appendChild(el("div", { class: "muted", text: "…另有 " + (updates.length - 200) + " 条未展开" }));
    }

    const ok = await confirmDialog({
      title: "确认提交这些裁决？",
      width: 760,
      message: "共 " + updates.length + " 条改动：判 A " + byVerdict.A + " 条、判 B " + byVerdict.B +
        " 条、自定义 C " + byVerdict.C + " 条、标 ? " + byVerdict["?"] + " 条。提交后会写入 decisions.json。",
      extra: list,
      confirmText: "确认提交",
      cancelText: "再改改"
    });
    if (!ok) return;

    pendingSubmit = true;
    updatePendingBadge();
    let changed = 0;
    try {
      const res = await postDecisions(runId, updates.map((u) => {
        const o = { key: u.key, verdict: u.verdict };
        if (u.verdict === "C") o.replacement = u.replacement || "";
        return o;
      }));
      changed = res && res.changed !== undefined ? res.changed : updates.length;
      updates.forEach((u) => {
        local.delete(u.key);
        const it = findItem(u.key);
        if (it) { it.verdict = u.verdict; it.status = "decided"; }
      });
      toast("已提交 " + changed + " 条裁决", "ok");
    } catch (err) {
      const msg = describeError(err);
      toast(msg, "error", 9000);
      const errs = err && err.payload && err.payload.errors ? err.payload.errors : null;
      const body = [errorBar(msg)];
      if (errs && errs.length) {
        const pre = el("pre", { class: "err-pre" });
        pre.textContent = errs.map((e) => typeof e === "string" ? e : JSON.stringify(e, null, 2)).join("\n");
        body.push(pre);
      }
      openModal({ title: "提交被拒绝（后端一个字节都没写）", body: body, actions: [{ label: "知道了", kind: "ghost" }] });
      pendingSubmit = false;
      updatePendingBadge();
      return;
    } finally {
      pendingSubmit = false;
      updatePendingBadge();
      renderSidebar();
      renderCenter();
    }

    // 关键：提交之后必须走 预览 → 确认 → 执行 三步
    await runApplyFlow(changed);
  }

  // ---------------- 回填审批闭环 ----------------
  async function runApplyFlow(changed) {
    if (applying) return;
    applying = true;
    let preview = null;
    try {
      preview = await applyPreview(runId);
    } catch (err) {
      applying = false;
      toast("预览失败：" + describeError(err), "error", 9000);
      return;
    }

    const counts = (preview && preview.counts) || {};
    const items = (preview && preview.items) || [];

    const countRow = el("div", { class: "apply-counts" });
    const keys = ["apply", "already", "missing", "skip"];
    const labels = { apply: "将回填", already: "已一致", missing: "找不到", skip: "跳过" };
    keys.forEach((k) => {
      countRow.appendChild(el("div", { class: "apply-count c-" + k },
        el("div", { class: "apply-count-num", text: fmt.num(counts[k] === undefined ? 0 : counts[k]) }),
        el("div", { class: "apply-count-label", text: labels[k] })
      ));
    });

    const previewList = el("div", { class: "preview-list" });
    if (!items.length) {
      previewList.appendChild(el("div", { class: "muted", text: "预览未返回任何回填条目。" }));
    } else {
      items.slice(0, 20).forEach((it) => {
        previewList.appendChild(el("div", { class: "preview-row " + (it.kind ? "k-" + it.kind : "") },
          el("span", { class: "badge badge-st", text: it.kind || "?" }),
          el("span", { class: "preview-key mono", text: it.key || "" }),
          el("div", { class: "preview-diff mono" },
            el("div", { class: "preview-old" }, el("span", { class: "row-tag", text: "旧" }), el("span", { text: it.old || "" })),
            el("div", { class: "preview-new" }, el("span", { class: "row-tag", text: "新" }), el("span", { text: it.new || "" }))
          ),
          it.reason ? el("span", { class: "preview-reason muted", text: it.reason }) : null
        ));
      });
      if (items.length > 20) {
        previewList.appendChild(el("div", { class: "muted", text: "…共 " + items.length + " 条，此处只列前 20 条。" }));
      }
    }

    const body = [
      el("p", { class: "modal-text", text: "这是干跑结果，还没有写任何文件。确认后才会回填并重建成书。" }),
      countRow,
      el("div", { class: "approval-sub-title", text: "前 20 条回填明细" }),
      previewList,
      el("div", { class: "warning-box" },
        el("div", { class: "warning-text", text: "重建顺序为 layout → md → epub；缺少 layout 这一步，裁决成果会被静默丢弃。" }))
    ];

    const doApply = async () => {
      try {
        applying = true;
        const res = await applyRun(runId, ["layout", "md", "epub"]);
        const jobId = res && res.job_id ? res.job_id : "";
        toast("已开始回填并重建" + (jobId ? "（" + jobId + "）" : ""), "ok");
        applying = false;
        const q = jobId ? "?job=" + encodeURIComponent(jobId) : "";
        location.hash = "#/run/" + encodeURIComponent(runId) + q;
      } catch (err) {
        applying = false;
        toast("回填失败：" + describeError(err), "error", 9000);
      }
    };

    openModal({
      title: "回填预览（干跑，尚未写盘）",
      width: 860,
      body: body,
      actions: [
        { label: "先不回填", kind: "ghost", onClick: () => { applying = false; } },
        { label: "确认回填并重建成书", kind: "primary", onClick: () => { doApply(); } }
      ],
      onClose: () => { if (applying) applying = false; }
    });
  }

  // ---------------- 键盘 ----------------
  function inEditable(el2) {
    if (!el2) return false;
    const tag = (el2.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return true;
    return !!(el2.isContentEditable);
  }

  function onKey(ev) {
    // 弹窗打开时不抢键
    const modalRoot = document.getElementById("modal-root");
    if (modalRoot && modalRoot.childNodes.length > 0) return;
    if (inEditable(ev.target)) return;
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;

    const k = ev.key;
    if (k === "a" || k === "1" || k === "A") { ev.preventDefault(); pick("A"); return; }
    if (k === "b" || k === "2" || k === "B") { ev.preventDefault(); pick("B"); return; }
    if (k === "c" || k === "3" || k === "C") {
      ev.preventDefault();
      if (customBtn && customBtn.click) customBtn.click();
      return;
    }
    if (k === " " || k === "Spacebar") { ev.preventDefault(); pick("?"); return; }
    if (k === "j" || k === "ArrowDown") { ev.preventDefault(); next(); return; }
    if (k === "k" || k === "ArrowUp") { ev.preventDefault(); prev(); return; }
    if (k === "Enter") { ev.preventDefault(); commitCurrent(); return; }
    if (k === "u" || k === "U") {
      ev.preventDefault();
      const target = flat.findIndex((f) => f.unlocated);
      if (target >= 0) { goTo(target); toast("已跳到未定位条目区", "info"); }
      else toast("没有未定位条目", "info");
      return;
    }
  }
  document.addEventListener("keydown", onKey);

  // ---------------- 数据加载 ----------------
  const loadBox = el("div", { class: "load-box" });

  function renderStats() {
    clear(statsBar);
    if (!data) return;
    const st = data.stats || {};
    const cells = [
      ["总条目", data.total], ["待裁决", data.pending],
      ["判 A", st.A], ["判 B", st.B], ["自定义 C", st.C], ["标 ?", st["?"]]
    ];
    cells.forEach(([k, v]) => {
      statsBar.appendChild(el("div", { class: "stat" },
        el("div", { class: "stat-num", text: fmt.num(v === undefined ? 0 : v) }),
        el("div", { class: "stat-label", text: k })
      ));
    });
    statsBar.appendChild(el("span", { class: "spacer" }));
    statsBar.appendChild(el("div", { class: "stat-actions" },
      el("span", { class: "muted", text: "only=" }),
      (function () {
        const sel = el("select", {
          class: "input input-sm",
          onchange: (ev) => load(ev.target.value)
        },
          el("option", { value: "eyes", selected: onlyMode === "eyes" }, "eyes"),
          el("option", { value: "noise", selected: onlyMode === "noise" }, "noise"),
          el("option", { value: "all", selected: onlyMode === "all" }, "all")
        );
        return sel;
      })(),
      el("button", {
        class: "btn btn-sm btn-ghost", type: "button",
        onclick: () => load(onlyMode)
      }, "重新加载")
    ));
  }

  let onlyMode = "eyes";

  async function load(only) {
    onlyMode = only || onlyMode;
    clear(banner);
    banner.hidden = true;
    clear(centerHost);
    centerHost.appendChild(skeleton(4));
    try {
      const res = await getDecisions(runId, onlyMode);
      data = res || { total: 0, pending: 0, stats: {}, unlocated: [], groups: [] };
      local.clear();
      rebuildFlat();
      index = 0;
      renderStats();
      renderSidebar();
      renderFilterBar();
      renderCenter();
      updatePendingBadge();
    } catch (err) {
      const msg = describeError(err);
      banner.hidden = false;
      clear(banner).appendChild(el("div", { class: "inline-error" },
        el("span", { text: "裁决数据加载失败：" + msg }),
        el("button", { class: "btn btn-sm", type: "button", onclick: () => load(onlyMode) }, "重试"))
      );
      clear(centerHost).appendChild(emptyState("暂时拿不到裁决数据", msg,
        el("button", { class: "btn btn-primary", type: "button", onclick: () => load(onlyMode) }, "重试")));
    }
  }

  // ---------------- 组装 ----------------
  clear(leftCol);
  leftCol.appendChild(evidenceHost);
  leftCol.appendChild(sideHost);
  clear(centerCol);
  centerCol.appendChild(centerHost);
  rightCol.appendChild(el("div", { class: "card note-card" },
    el("div", { class: "card-head" }, el("h2", { class: "card-title", text: "证据与操作" })),
    el("div", { class: "muted", text: "加载中…" })));
  renderFilterBar();
  renderEvidence(null);

  load("eyes");

  // 当前路由切换后卸载键盘监听
  const stop = () => {
    document.removeEventListener("keydown", onKey);
  };
  // 借 window 卸载事件兜底
  window.addEventListener("hashchange", stop, { once: true });

  return root;
}
