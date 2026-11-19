// 校对页（#/task/:id/proofread）—— 本工具最核心的界面。
// 契约见 webui/API.md 第 4 节：groups[] / unlocated[] / diff[] 全部按后端给的渲染，
// 前端不自己重算差异。设计规范见 docs/UI-DESIGN.md 第 5 节。
import { el, clear, fmt, toast, errorBar, emptyState, openModal, confirmDialog,
         progressBar, kbd, skeleton } from "./ui.js";
import { getDecisions, postDecisions, applyPreview, applyRun, pageUrl, cropUrl,
         describeError } from "./api.js";
import { matchKey, keyLabel, keys } from "./keys.js";

// 本地未提交状态：key -> {verdict, replacement}
// verdict: "" 未判定 / "A" / "B" / "C" / "?"
const VERDICT_LABEL = { A: "采用 A", B: "采用 B", C: "自行输入", "?": "跳过" };

function opClass(op) {
  if (op === "replace") return "d-replace";
  if (op === "insert") return "d-insert";
  if (op === "delete") return "d-delete";
  return "d-equal";
}

// 渲染一侧**完整文本**，并把差异区间高亮。
// 注意：后端 diff[] 只带差异片段（i1/i2 是它在 A 里的下标，j1/j2 是 B 里的），
// 不含中间的相同文字。所以不能「只渲染 diff」——那样一行只显示被换掉的那个字
// （实测就是这么渲染成 A="已" / B="己" 的）。必须用它当**区间标注**，套在全文上。
function renderDiffSide(diff, side, fullText) {
  const frag = document.createDocumentFragment();
  const list = Array.isArray(diff) ? diff : [];
  const text = String(fullText === null || fullText === undefined ? "" : fullText);
  if (!list.length) {
    frag.appendChild(document.createTextNode(text));
    return frag;
  }
  const key1 = side === "a" ? "i1" : "j1";
  const key2 = side === "a" ? "i2" : "j2";
  // 该侧没有对应内容的操作：A 侧没有 insert，B 侧没有 delete
  const skipOp = side === "a" ? "insert" : "delete";
  let pos = 0;
  let emitted = false;
  for (let i = 0; i < list.length; i++) {
    const d = list[i] || {};
    const op = d.op || "equal";
    if (op === skipOp) continue;
    let a = Number(d[key1]);
    let b = Number(d[key2]);
    if (!isFinite(a) || !isFinite(b)) continue;
    if (a < pos) a = pos;
    if (b < a) b = a;
    if (a > pos) frag.appendChild(document.createTextNode(text.slice(pos, a)));
    if (b > a) frag.appendChild(el("mark", { class: opClass(op), title: op }, text.slice(a, b)));
    pos = b;
    emitted = true;
  }
  if (pos < text.length) frag.appendChild(document.createTextNode(text.slice(pos)));
  if (!emitted && !frag.firstChild) frag.appendChild(document.createTextNode(text));
  return frag;
}

function plainSide(text) {
  return document.createTextNode(text === null || text === undefined ? "" : String(text));
}

// 任务 id 可能含斜杠，逐段编码
export function taskHash(id, view) {
  const parts = String(id || "").split("/").map(encodeURIComponent).join("/");
  return "#/task/" + parts + (view ? "/" + view : "");
}

export function mount(ctx) {
  const taskId = ctx.params.id;
  const task = ctx.findRun(taskId);
  const title = (task && task.title) || taskId;

  let data = null;
  let flat = [];              // 拍平后的条目
  let index = 0;              // 当前条目下标
  let currentGroup = "全部";   // 当前分组
  let only = "eyes";          // eyes | noise | all
  let zoom = 1;               // 1 / 1.5 / 2 / 3
  let mode = "crop";          // crop | page
  let filterText = "";
  let alive = () => ctx.alive();
  let es = null;

  const local = new Map();    // key -> {verdict, replacement}
  const groupDone = new Set(); // 整组已采纳的组名

  // ---------- 骨架 ----------
  const root = el("div", { class: "view view-proofread" });

  const barName = el("div", { class: "pr-bar-name", text: title, title: title });
  const barSub = el("div", { class: "pr-bar-sub", text: taskId });
  const progDone = el("b", { text: "0" });
  const progTotal = el("span", { text: "0" });
  const progFill = progressBar(0);
  const progSub = el("span", { class: "faint", text: "" });
  const progMeta = el("div", { class: "pr-progress-meta" },
    el("span", null, "已校对 ", progDone, el("span", { class: "faint", text: " / " }), progTotal, " 条"),
    progSub
  );
  const pendingBadge = el("span", { class: "badge badge-neutral", text: "0 条待提交" });
  const submitBtn = el("button", {
    class: "btn btn-primary", type: "button", onclick: () => doSubmit()
  }, "提交校对");

  // 分类过滤：默认只显示「含汉字」的差异（eyes），纯标点空白差异（noise）
  // 数量往往是眼睛类的两倍，默认不显示以免淹没真问题；但**必须能切过去**，
  // 否则界面上不存在的东西等于不存在。
  const onlySeg = el("div", { class: "segmented" });
  function wireOnlySeg() {
    clear(onlySeg);
    [["eyes", "字形差异"], ["noise", "标点空白"], ["all", "全部"]].forEach(([v, label]) => {
      onlySeg.appendChild(el("button", {
        type: "button", class: only === v ? "active" : "",
        onclick: () => {
          if (only === v) return;
          only = v;
          index = 0;
          currentGroup = "全部";
          wireOnlySeg();
          clear(stage);
          stage.appendChild(skeleton(5));
          loadDecisions().catch((err) => toast(describeError(err), "error", 9000));
        }
      }, label));
    });
  }
  wireOnlySeg();

  const topBar = el("div", { class: "pr-bar" },
    el("div", { class: "pr-bar-title" }, barName, barSub),
    el("div", { class: "pr-progress" }, progMeta, progFill),
    onlySeg,
    el("div", { class: "pr-bar-actions" },
      pendingBadge,
      submitBtn,
      el("a", { class: "btn", href: taskHash(taskId) }, "返回进度")
    )
  );

  const banner = el("div", { class: "pr-banner", hidden: true });

  // ---------- 三栏容器 ----------
  const groupsPane = el("div", { class: "pr-pane" });
  const evidencePane = el("div", { class: "pr-pane pr-pane-evidence" });
  const decidePane = el("div", { class: "pr-pane pr-pane-decide" });
  const body = el("div", { class: "pr-body" }, groupsPane, evidencePane, decidePane);

  root.appendChild(topBar);
  root.appendChild(banner);
  root.appendChild(body);

  // ---------- 左栏：分组 ----------
  const filterInput = el("input", {
    class: "input input-sm", type: "search", placeholder: "过滤分组…",
    oninput: (ev) => { filterText = ev.target.value; renderGroups(); }
  });
  const groupsList = el("div", { class: "pr-groups" });
  groupsPane.appendChild(el("div", { class: "pr-filter" }, filterInput));
  groupsPane.appendChild(groupsList);

  // ---------- 中栏：证据 ----------
  const evMeta = el("span", { class: "pr-evidence-meta", text: "—" });
  const modeSeg = el("div", { class: "segmented" });
  const zoomSeg = el("div", { class: "segmented" });
  const evidenceBar = el("div", { class: "pr-evidence-bar" },
    evMeta,
    el("span", { class: "spacer" }),
    modeSeg,
    zoomSeg
  );
  const stage = el("div", { class: "pr-stage mode-crop" });
  evidencePane.appendChild(evidenceBar);
  evidencePane.appendChild(stage);

  function wireSegs() {
    clear(modeSeg);
    [["crop", "单行切片"], ["page", "整页"]].forEach(([m, label]) => {
      modeSeg.appendChild(el("button", {
        type: "button", class: mode === m ? "active" : "",
        onclick: () => { mode = m; renderEvidence(); }
      }, label));
    });
    clear(zoomSeg);
    [[1, "100%"], [1.5, "150%"], [2, "200%"], [3, "300%"]].forEach(([z, label]) => {
      zoomSeg.appendChild(el("button", {
        type: "button", class: zoom === z ? "active" : "",
        onclick: () => { zoom = z; renderEvidence(); }
      }, label));
    });
  }

  // ---------- 右栏：判定 ----------
  const decideHost = el("div", { class: "pr-decide" });
  decidePane.appendChild(decideHost);

  // ================= 渲染 =================

  function current() {
    return flat[index] || null;
  }

  // 这一条在后端到底判过没有（"?" + status=pending 表示没判过）
  function itemWasJudged(item) {
    if (!item) return false;
    const v = item.verdict || "?";
    if (v !== "?") return true;
    return !!(item.status && item.status !== "pending");
  }

  function verdictOf(item) {
    if (!item) return "";
    // "?" 是「明确跳过」，算已处理；但后端把没判过的也写成 "?"（pending），
    // 两者靠 status 区分：status 仍是 pending 且 verdict 是 "?" 就是**没判过**。
    const raw = local.has(item.key) ? (local.get(item.key).verdict || "")
                                    : (item.verdict || "");
    if (raw !== "?") return raw;
    if (local.has(item.key)) return "?";
    return (item.status && item.status !== "pending") ? "?" : "";
  }

  function localCount() {
    let n = 0;
    local.forEach((v) => { if (v && v.verdict) n++; });
    return n;
  }

  // ---- 顶栏进度 ----
  // 进度必须按**全书**算（后端给 total / pending），不能按当前加载的子集算：
  // 默认只拉 eyes 分类的 999 条，可全书有 2380 条待校对，
  // 按子集算就会显示「已校对 999 / 999 条」——看着全做完了，其实一件没做。
  function refreshProgress() {
    const total = Number((data && data.total) || flat.length || 0);
    const pendingAll = Number((data && data.pending) || 0);
    let done = Math.max(0, total - pendingAll);
    // 本地未提交的判定要即时反映到进度上，否则每判一条都要等提交才动
    local.forEach((v, key) => {
      const it = flat.find((x) => x.key === key);
      if (!it) return;
      const wasPending = !itemWasJudged(it);
      const nowJudged = !!(v && v.verdict);
      if (wasPending && nowJudged) done++;
      else if (!wasPending && !nowJudged) done--;
    });
    done = Math.max(0, Math.min(total, done));
    progDone.textContent = fmt.num(done);
    progTotal.textContent = fmt.num(total);
    const pct = total ? (100 * done / total) : 0;
    const fill = progFill.querySelector(".progress-fill");
    if (fill) fill.style.width = pct + "%";
    const n = localCount();
    pendingBadge.textContent = n > 0 ? fmt.num(n) + " 条待提交" : "无待提交";
    pendingBadge.className = "badge " + (n > 0 ? "badge-warn" : "badge-neutral");
    submitBtn.disabled = n === 0;
    // 0 / 0 不能说「全书已校对完」：那是「还没有清单」，两件事不能混。
    // 实测《中国僧官制度史》没有核对阶段产物时，顶栏显示「已校对 0 / 0 条 ·
    // 全书已校对完」——看着像干完了，其实是根本还没开始。
    const pend = Math.max(0, total - done);
    if (!total) progSub.textContent = "还没有校对清单";
    else if (pend > 0) progSub.textContent = "待校对 " + fmt.num(pend) + " 条";
    else progSub.textContent = "全书已校对完";
  }

  // ---- 左栏分组 ----
  function groupNames() {
    const names = [];
    const seen = new Set();
    const groups = (data && data.groups) || [];
    for (let i = 0; i < groups.length; i++) {
      const g = groups[i];
      if (!g || !g.group) continue;
      if (seen.has(g.group)) continue;
      seen.add(g.group);
      names.push({ name: g.group, count: Number(g.count || 0), unlocated: false });
    }
    const unloc = (data && data.unlocated) || [];
    if (unloc.length) names.unshift({ name: "未定位", count: unloc.length, unlocated: true });
    return names;
  }

  function renderGroups() {
    clear(groupsList);
    const names = groupNames();
    const totalAll = flat.length;
    const needle = filterText.trim().toLowerCase();

    groupsList.appendChild(el("div", { class: "pr-group-head", text: "分组" }));

    const mkItem = (name, count, unlocated) => {
      if (needle && name.toLowerCase().indexOf(needle) < 0) return;
      const isActive = currentGroup === name;
      const btn = el("button", {
        type: "button",
        class: "pr-group" + (isActive ? " active" : "") + (unlocated ? " is-unlocated" : ""),
        onclick: () => { currentGroup = name; index = 0; renderGroups(); selectFirstInGroup(); }
      },
        el("span", { class: "pr-group-label", text: name, title: name }),
        el("span", { class: "pr-group-count", text: fmt.num(count) })
      );
      groupsList.appendChild(btn);
    };

    // 「全部」显示**全书**条数（后端 total），不是当前 only 过滤后加载的子集：
    // 默认只加载 eyes 分类的 999 条，标成「全部 999」会让人以为全书只有 999 条。
    mkItem("全部", Number((data && data.total) || totalAll), false);
    names.forEach((g) => mkItem(g.name, g.count, g.unlocated));

    // 未定位说明
    if (currentGroup === "未定位") {
      groupsList.appendChild(el("div", { class: "pr-unloc-note" },
        el("span", { text: "⚠" }),
        el("span", { text: "这些建议在原页找不到对应行，必须人工核对并补真实坐标，不能直接采纳。" })
      ));
    }
  }

  // ---- 中栏证据图 ----
  function renderEvidence() {
    wireSegs();
    const item = current();
    clear(stage);
    stage.className = "pr-stage " + (mode === "page" ? "mode-page" : "mode-crop");

    if (!item) {
      evMeta.textContent = "—";
      stage.appendChild(el("div", { class: "pr-noitem" },
        el("div", { class: "empty-title", text: "没有选中的条目" }),
        el("div", { class: "empty-desc", text: "左侧选一个分组，或点「全部」从头开始。" })
      ));
      return;
    }

    const page = item.page || "";
    const y0 = item.y0;
    evMeta.textContent = page + (y0 !== undefined && y0 !== null ? " · y=" + y0 : "");

    const url = mode === "crop" ? cropUrl(taskId, page, y0) : pageUrl(taskId, page);

    const wrap = el("div", { class: "pr-shot-wrap" });
    const holder = el("div", { class: "pr-shot-holder" });

    // 缩放：100% 就是「铺满证据区」。切片图本来就窄，再按自然尺寸显示会看不清，
    // 所以 crop 模式 100% = 容器宽度；page 模式 100% = 原始像素宽度（放大后才滚动）。
    const img = el("img", {
      class: "pr-shot",
      src: url,
      alt: page + " 证据图",
      style: mode === "crop"
        ? { width: Math.round(zoom * 100) + "%" }
        : { width: Math.round(zoom * 100) + "%" }
    });
    img.addEventListener("error", () => {
      if (!alive()) return;
      clear(holder);
      holder.appendChild(el("div", { class: "pr-shot-fail" },
        el("div", { class: "empty-title", text: "证据图不可用" }),
        el("div", { class: "empty-desc", text: "这一页的图片可能还没渲染，或该条目缺少页码。" }),
        el("button", {
          class: "btn btn-sm", type: "button",
          onclick: () => { renderEvidence(); }
        }, "重试")
      ));
    });
    holder.appendChild(img);
    wrap.appendChild(holder);
    wrap.appendChild(el("div", { class: "pr-shot-hint",
      text: mode === "crop" ? "单行切片 · 上下各留 8px 留白" : "整页原图 · 拖动查看全页" }));
    stage.appendChild(wrap);
  }

  // ---- 右栏判定 ----
  function renderDecide() {
    clear(decideHost);
    const item = current();

    if (!item) {
      decideHost.appendChild(el("div", { class: "pr-noitem" },
        el("div", { class: "empty-title", text: "没有待校对的条目" }),
        el("div", { class: "empty-desc", text: "这个分组下已经没有条目了。" })
      ));
      return;
    }

    // 未定位条目：后端不认没有 y0 的 B/C（apply_updates 会拒绝），
    // 所以这里不给判定按钮，只让人看清它为什么定位失败、该去补什么。
    if (item.unlocated) {
      decideHost.appendChild(el("div", { class: "pr-loc" },
        el("span", { text: item.page || "?" }),
        el("span", { class: "spacer" }),
        el("span", { class: "badge badge-warn", text: "未定位" })
      ));
      decideHost.appendChild(el("div", { class: "pr-cand" },
        el("div", { class: "pr-cand-head" }, "机器建议"),
        el("div", { class: "pr-cand-body", text: item.suggestion || "（无建议）" })
      ));
      decideHost.appendChild(el("div", { class: "pr-cand" },
        el("div", { class: "pr-cand-head" }, "为什么没定位"),
        el("div", { class: "pr-cand-body", text: item.why || "原页里找不到含该文字的行。" })
      ));
      decideHost.appendChild(el("div", { class: "pr-unloc-note" },
        el("span", { text: "⚠" }),
        el("span", { text: "这一条没有行坐标 y0，回填会被跳过。要采纳它，必须在校对清单里补上真实 y0，本页面不能直接判定。" })
      ));
      decideHost.appendChild(el("div", { class: "pr-foot" },
        el("span", { class: "spacer" }),
        el("button", { class: "btn btn-sm", type: "button", onclick: () => move(-1) }, "上一条"),
        el("button", { class: "btn btn-sm", type: "button", onclick: () => move(1) }, "下一条")
      ));
      return;
    }

    const v = verdictOf(item);

    decideHost.appendChild(el("div", { class: "pr-loc" },
      el("span", { text: item.page || "?" }),
      item.y0 !== undefined && item.y0 !== null ? el("span", { text: "@" + item.y0 }) : null,
      el("span", { class: "spacer" }),
      v ? el("span", { class: "badge badge-ok", text: VERDICT_LABEL[v] || v }) : null
    ));

    // A 候选
    const aBody = el("div", { class: "pr-cand-body" });
    if (Array.isArray(item.diff) && item.diff.length) aBody.appendChild(renderDiffSide(item.diff, "a", item.A));
    else aBody.appendChild(plainSide(item.A));
    const aBox = el("div", { class: "pr-cand" + (v === "A" ? " is-chosen" : "") },
      el("div", { class: "pr-cand-head" }, "A 候选",
        el("span", { class: "spacer" }),
        v === "A" ? el("span", { class: "badge badge-accent", text: "已选" }) : null),
      aBody
    );
    decideHost.appendChild(aBox);

    // B 候选
    const bBody = el("div", { class: "pr-cand-body" });
    if (Array.isArray(item.diff) && item.diff.length) bBody.appendChild(renderDiffSide(item.diff, "b", item.B));
    else bBody.appendChild(plainSide(item.B));
    const bBox = el("div", { class: "pr-cand" + (v === "B" ? " is-chosen" : "") },
      el("div", { class: "pr-cand-head" }, "B 候选",
        el("span", { class: "spacer" }),
        v === "B" ? el("span", { class: "badge badge-accent", text: "已选" }) : null),
      bBody,
      item.note ? el("div", { class: "pr-cand-note", text: item.note }) : null
    );
    decideHost.appendChild(bBox);

    // 自行输入
    const customWrap = el("div", { class: "pr-custom", hidden: true });
    const customInput = el("textarea", {
      class: "textarea", rows: "3",
      placeholder: "输入这一行的正确文字，将整行覆盖"
    });
    customInput.value = v === "C" && local.has(item.key) ? (local.get(item.key).replacement || "")
                    : (item.B || item.A || "");
    customWrap.appendChild(customInput);
    customWrap.appendChild(el("div", { class: "field-hint", text: "整行覆盖：把这一行的完整内容写对，不只是改那个字。" }));

    // 判定按钮。键帽显示的是**当前生效**的绑定，所以在设置里改过键之后
    // 这里会跟着变——常量 "A"/"B"/"C"/"S" 会让改完键的界面直接说谎。
    const actions = el("div", { class: "pr-actions" });
    const btnA = el("button", { class: "pr-btn pr-btn-a", type: "button", onclick: () => judge("A") },
      el("span", { class: "pr-btn-label", text: "采用 A" }), kbd(keyLabel(keys().useA)));
    const btnB = el("button", { class: "pr-btn pr-btn-b", type: "button", onclick: () => judge("B") },
      el("span", { class: "pr-btn-label", text: "采用 B" }), kbd(keyLabel(keys().useB)));
    const btnC = el("button", { class: "pr-btn pr-btn-c", type: "button",
      onclick: () => { customWrap.hidden = false; customInput.focus(); } },
      el("span", { class: "pr-btn-label", text: "自行输入" }), kbd(keyLabel(keys().useC)));
    const btnS = el("button", { class: "pr-btn pr-btn-s", type: "button", onclick: () => judge("?") },
      el("span", { class: "pr-btn-label", text: "跳过这一条" }), kbd(keyLabel(keys().skip)));

    actions.appendChild(btnA);
    actions.appendChild(btnB);
    actions.appendChild(btnC);
    actions.appendChild(btnS);
    decideHost.appendChild(actions);
    decideHost.appendChild(customWrap);
    decideHost.appendChild(el("div", { class: "field-hint", text: "自行输入后按 Ctrl+Enter 确认" }));

    customInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
        ev.preventDefault();
        const text = customInput.value;
        if (!text) { toast("请先输入内容", "warn"); return; }
        judge("C", text);
      }
    });

    // 底部状态行
    // 「全部」这一组的条数要用全书总数，不是当前加载的子集：
    // 默认只拉 eyes 的 999 条，标成「本组 999 条」会让人以为全书只有 999 条
    const groupTotal = currentGroup === "全部"
      ? Number((data && data.total) || groupItems().length)
      : groupItems().length;
    decideHost.appendChild(el("div", { class: "pr-foot" },
      el("span", null, "本组 ", el("b", { text: fmt.num(groupTotal) }), " 条，已处理 ",
        el("b", { text: fmt.num(groupDoneCount()) }), " 条"),
      el("span", { class: "spacer" }),
      el("button", { class: "btn btn-sm", type: "button", onclick: () => move(-1) }, "上一条"),
      el("button", { class: "btn btn-sm", type: "button", onclick: () => move(1) }, "下一条")
    ));

    // 整组采纳：只在分组视图且有 >1 条时出现
    if (currentGroup !== "全部") {
      const n = groupItems().length;
      if (n > 1) {
        decideHost.appendChild(el("div", { class: "pr-foot" },
          el("button", {
            class: "btn btn-sm", type: "button",
            onclick: () => adoptWholeGroup()
          }, "整组采用 B（" + fmt.num(n) + " 条）")
        ));
      }
    }
  }

  function groupItems() {
    if (currentGroup === "全部") return flat;
    if (currentGroup === "未定位") return (data && data.unlocated) || [];
    return flat.filter((it) => (it.group || "") === currentGroup);
  }

  function groupDoneCount() {
    if (currentGroup === "全部") {
      const total = Number((data && data.total) || 0);
      const pend = Number((data && data.pending) || 0);
      return Math.max(0, total - pend);
    }
    return groupItems().filter((it) => verdictOf(it)).length;
  }

  function selectFirstInGroup() {
    const items = groupItems();
    if (!items.length) { renderDecide(); renderEvidence(); return; }
    const key = items[0].key;
    const idx = flat.findIndex((it) => it.key === key);
    index = idx >= 0 ? idx : 0;
    renderAll();
  }

  function renderAll() {
    renderGroups();
    renderEvidence();
    renderDecide();
    refreshProgress();
  }

  // ================= 交互 =================

  function move(delta) {
    if (!flat.length) return;
    index = (index + delta + flat.length) % flat.length;
    // 跟随分组：切到哪条就把分组同步过去，否则会出现
    // 「分组显示已→己，实际编辑的却是别的组的条目」
    const it = flat[index];
    if (it && it.group && currentGroup !== "全部" && currentGroup !== "未定位") {
      currentGroup = it.group;
    }
    renderAll();
  }

  function judge(verdict, replacement) {
    const item = current();
    if (!item) return;
    // 未定位条目没有 y0，后端 apply_updates 会整份拒绝（不是跳过这一条），
    // 所以键盘也不能判它 —— 否则一次按键就能让整次提交失败。
    if (item.unlocated) {
      toast("这一条未定位（缺行坐标 y0），不能直接判定；请先在校对清单里补 y0", "warn", 6000);
      return;
    }
    const prev = local.get(item.key);
    local.set(item.key, {
      verdict: verdict,
      replacement: verdict === "C" ? String(replacement || "") : null
    });
    if (prev && prev.verdict) {
      // 不改动，只是改判，不需要再算一次
    }
    refreshProgress();
    // 200ms 反馈后跳下一条
    const target = decideHost.querySelector(
      verdict === "A" ? ".pr-btn-a" : verdict === "B" ? ".pr-btn-b"
      : verdict === "?" ? ".pr-btn-s" : ".pr-btn-c");
    if (target) {
      target.classList.add("flash");
      setTimeout(() => {
        if (!alive()) return;
        target.classList.remove("flash");
        move(1);
      }, 200);
    } else {
      move(1);
    }
  }

  function adoptWholeGroup() {
    const items = groupItems();
    if (!items.length) return;
    confirmDialog({
      title: "整组采用 B",
      message: "把「" + currentGroup + "」这 " + fmt.num(items.length)
        + " 条全部标为采用 B 候选。这是批量操作，之后仍可在提交前逐条改判。",
      confirmText: "整组采用"
    }).then((ok) => {
      if (!ok || !alive()) return;
      let n = 0;
      items.forEach((it) => {
        if (!local.has(it.key) || !local.get(it.key).verdict) {
          local.set(it.key, { verdict: "B", replacement: null });
          n++;
        }
      });
      groupDone.add(currentGroup);
      toast("已标记 " + n + " 条为采用 B", "success");
      renderAll();
    });
  }

  // ================= 提交 =================

  function doSubmit() {
    const updates = [];
    local.forEach((v, key) => {
      if (v && v.verdict) {
        const u = { key: key, verdict: v.verdict };
        if (v.verdict === "C") u.replacement = v.replacement;
        updates.push(u);
      }
    });
    if (!updates.length) { toast("还没有任何校对结果可以提交", "warn"); return; }

    // 先把本地校对结果写进 decisions.json，再干跑预览——
    // 顺序不能反：预览读的是盘上的校对清单，先预览只会看到旧状态。
    const btn = submitBtn;
    btn.disabled = true;
    btn.textContent = "提交中…";
    postDecisions(taskId, updates).then((res) => {
      const changed = (res && res.changed) || 0;
      const skipped = (res && res.skipped) || 0;
      const errs = (res && res.errors) || [];
      if (errs.length) {
        toast("有 " + errs.length + " 条没能写入：" + String(errs[0]), "error", 9000);
      }
      toast("已写入 " + changed + " 条（跳过 " + skipped + " 条）", "success");
      local.clear();
      return refreshThenPreview();
    }).catch((err) => {
      toast(describeError(err), "error", 9000);
    }).then(() => {
      if (!alive()) return;
      btn.disabled = false;
      btn.textContent = "提交校对";
      refreshProgress();
    });
  }

  // 提交后：重新拉校对清单 + 干跑预览，让人看到「这次真的会改多少」
  function refreshThenPreview() {
    return loadDecisions().then(() => {
      if (!alive()) return;
      return applyPreview(taskId);
    }).then((plan) => {
      if (!alive() || !plan) return;
      showPreviewModal(plan);
    });
  }

  function showPreviewModal(plan) {
    const counts = (plan && plan.counts) || {};
    const items = (plan && plan.items) || [];
    const body = [];

    body.push(el("div", { class: "pr-preview-grid" },
      el("div", { class: "pr-preview-cell is-apply" },
        el("div", { class: "pr-preview-num", text: fmt.num(counts.apply || 0) }),
        el("div", { class: "pr-preview-lbl", text: "将改动" })),
      el("div", { class: "pr-preview-cell is-already" },
        el("div", { class: "pr-preview-num", text: fmt.num(counts.already || 0) }),
        el("div", { class: "pr-preview-lbl", text: "已是目标值" })),
      el("div", { class: "pr-preview-cell is-missing" },
        el("div", { class: "pr-preview-num", text: fmt.num(counts.missing || 0) }),
        el("div", { class: "pr-preview-lbl", text: "找不到对应行" })),
      el("div", { class: "pr-preview-cell" },
        el("div", { class: "pr-preview-num", text: fmt.num(counts.skip || 0) }),
        el("div", { class: "pr-preview-lbl", text: "忽略" }))
    ));

    body.push(el("p", { class: "modal-text",
      text: "确认后会把这些改动回填到 OCR 文本，并重新跑版面、Markdown 与成品三个阶段。"
          + "回填前会自动备份原文件到 work/.backup/。" }));

    if (items.length) {
      const shown = items.slice(0, 20);
      const tbl = el("table", { class: "table" },
        el("thead", null, el("tr", null,
          el("th", { text: "键" }), el("th", { text: "原" }), el("th", { text: "新" }), el("th", { text: "说明" }))),
        el("tbody", null, shown.map((it) => el("tr", null,
          el("td", { class: "mono", text: it.key || "" }),
          el("td", { text: fmt.truncate(it.old || "", 34) }),
          el("td", { text: fmt.truncate(it.new || "", 34) }),
          el("td", { class: "faint", text: it.reason || "" })
        )))
      );
      body.push(el("div", { class: "pr-preview-table" }, tbl));
      if (items.length > shown.length) {
        body.push(el("p", { class: "field-hint",
          text: "只显示前 20 条，共 " + fmt.num(items.length) + " 条。" }));
      }
    }

    openModal({
      title: "确认回填并重建成品",
      width: 780,
      body: body,
      actions: [
        { label: "取消", kind: "ghost" },
        { label: "确认回填并重建成品", kind: "primary", onClick: () => {
            doApply();
            return false;   // 保持弹窗，进度在页面顶部看
          } }
      ]
    });
  }

  function doApply() {
    applyRun(taskId).then((res) => {
      const jobId = (res && res.job_id) || "";
      toast("已开始回填，跳到处理进度看执行日志", "success");
      ctx.navigate(taskHash(taskId) + (jobId ? "?job=" + encodeURIComponent(jobId) : ""));
    }).catch((err) => {
      toast(describeError(err), "error", 9000);
    });
  }

  // ================= 数据 =================

  function buildFlat(doc) {
    const out = [];
    // 未定位条目也进 flat[]，否则「下一条」走到组尾就断开了；
    // 标记 unlocated=true，由 renderDecide 特殊处理（不给判定按钮）
    const unloc = (doc && doc.unlocated) || [];
    for (let i = 0; i < unloc.length; i++) {
      const it = unloc[i];
      if (!it) continue;
      it.group = "未定位";
      it.unlocated = true;
      out.push(it);
    }
    const groups = (doc && doc.groups) || [];
    for (let i = 0; i < groups.length; i++) {
      const g = groups[i] || {};
      const items = g.items || [];
      for (let j = 0; j < items.length; j++) {
        const it = items[j];
        if (!it) continue;
        it.group = g.group || "";
        out.push(it);
      }
    }
    return out;
  }

  function showBanner() {
    clear(banner);
    const unloc = (data && data.unlocated) || [];
    if (!unloc.length) { banner.hidden = true; return; }
    banner.hidden = false;
    banner.className = "pr-banner pr-banner-warn";
    banner.appendChild(el("span", { text: "⚠" }));
    banner.appendChild(el("span", null,
      el("b", { text: fmt.num(unloc.length) }),
      " 条建议在原页找不到对应行（未定位），必须人工补真实坐标才能采纳。点左侧「未定位」查看。"));
  }

  function loadDecisions() {
    return getDecisions(taskId, only).then((doc) => {
      if (!alive()) return;
      data = doc || {};
      flat = buildFlat(data);
      if (index >= flat.length) index = 0;
      currentGroup = "全部";
      renderAll();
      showBanner();
      if (!flat.length) {
        // 空态要**跨满三栏**：.pr-body 是 grid，直接 append 一个 div 会落在第一列
        // （实测被挤进 200px 的分组栏，正文竖排成一行三个字）。
        clear(body);
        body.appendChild(el("div", { class: "pr-emptywrap" },
          emptyState("这本书还没有校对条目",
            "没有找到待校对的差异。先跑完「核对」阶段生成分歧清单，或这本书本身就没有文字层可对账。",
            el("a", { class: "btn btn-primary", href: taskHash(taskId) }, "回到处理进度"))
        ));
      }
    });
  }

  // ================= 键盘 =================

  // 键盘绑定不写死在这里：哪颗键干什么由 keys.js 决定（用户可在「设置」里改），
  // 这里只把「动作 id」翻译成具体行为。
  function onKey(ev) {
    if (!alive()) return;
    const t = ev.target;
    const tag = t && t.tagName ? t.tagName.toLowerCase() : "";
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    // 内容可编辑的元素（将来若有）也要放过，否则在里面打字会被当成判定
    if (t && t.isContentEditable) return;
    // 弹窗开着时一律不接管：设置面板正在捕获按键，判定跳条会打断改键
    if (document.querySelector(".modal-backdrop")) return;

    const action = matchKey(ev);
    if (!action) return;

    if (action === "useA") { ev.preventDefault(); judge("A"); }
    else if (action === "useB") { ev.preventDefault(); judge("B"); }
    else if (action === "skip") { ev.preventDefault(); judge("?"); }
    else if (action === "useC") {
      ev.preventDefault();
      const wrap = decideHost.querySelector(".pr-custom");
      const ta = decideHost.querySelector(".pr-custom textarea");
      if (wrap) wrap.hidden = false;
      if (ta) ta.focus();
    }
    else if (action === "next") { ev.preventDefault(); move(1); }
    else if (action === "prev") { ev.preventDefault(); move(-1); }
    else if (action === "toggle") {
      ev.preventDefault();
      mode = mode === "crop" ? "page" : "crop";
      renderEvidence();
    }
    else if (action === "zoom100") { ev.preventDefault(); zoom = 1; renderEvidence(); }
    else if (action === "zoom150") { ev.preventDefault(); zoom = 1.5; renderEvidence(); }
    else if (action === "zoom200") { ev.preventDefault(); zoom = 2; renderEvidence(); }
    else if (action === "zoom300") { ev.preventDefault(); zoom = 3; renderEvidence(); }
    else if (action === "filter") { ev.preventDefault(); filterInput.focus(); }
  }

  document.addEventListener("keydown", onKey);

  // 首屏
  clear(stage);
  stage.appendChild(skeleton(5));
  loadDecisions().catch((err) => {
    if (!alive()) return;
    clear(body);
    body.appendChild(el("div", { style: { padding: "48px" } },
      errorBar(describeError(err)),
      el("div", { class: "row-gap" },
        el("button", { class: "btn btn-primary", type: "button",
          onclick: () => { location.reload(); } }, "重试"))
    ));
  });

  // cleanup：切走页面时必须解绑键盘，否则旧页面的快捷键会继续吃掉按键
  root.__cleanup = function () {
    document.removeEventListener("keydown", onKey);
    if (es) { try { es.close(); } catch (e) { /* 忽略 */ } es = null; }
  };
  return root;
}