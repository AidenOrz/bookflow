// 通用 UI 小工具：元素构造、提示条、弹窗、格式化。
// 全部使用原生 DOM API，不依赖任何第三方库。

// ---- 元素构造 ----
// el("div", {class: "x", onclick: fn}, "文本", 子节点...)
export function el(tag, attrs) {
  const node = document.createElement(tag);
  if (attrs) {
    const keys = Object.keys(attrs);
    for (let i = 0; i < keys.length; i++) {
      const key = keys[i];
      const val = attrs[key];
      if (val === null || val === undefined || val === false) continue;
      if (key === "class" || key === "className") {
        node.className = String(val);
      } else if (key === "text") {
        node.textContent = String(val);
      } else if (key === "html") {
        node.innerHTML = String(val);
      } else if (key === "style") {
        if (typeof val === "string") node.setAttribute("style", val);
        else {
          const sk = Object.keys(val);
          for (let j = 0; j < sk.length; j++) node.style.setProperty(sk[j], String(val[sk[j]]));
        }
      } else if (key === "dataset") {
        const dk = Object.keys(val);
        for (let j = 0; j < dk.length; j++) node.dataset[dk[j]] = String(val[dk[j]]);
      } else if (key === "value") {
        node.value = val;
      } else if (key === "checked" || key === "disabled" || key === "selected" ||
                 key === "hidden" || key === "readOnly" || key === "multiple") {
        const prop = key === "readOnly" ? "readOnly" : key;
        node[prop] = !!val;
      } else if (key.slice(0, 2) === "on" && typeof val === "function") {
        node.addEventListener(key.slice(2).toLowerCase(), val);
      } else {
        node.setAttribute(key, val === true ? "" : String(val));
      }
    }
  }
  for (let i = 2; i < arguments.length; i++) appendKid(node, arguments[i]);
  return node;
}

function appendKid(node, kid) {
  if (kid === null || kid === undefined || kid === false) return;
  if (Array.isArray(kid)) {
    for (let i = 0; i < kid.length; i++) appendKid(node, kid[i]);
    return;
  }
  if (kid instanceof Node) {
    node.appendChild(kid);
    return;
  }
  node.appendChild(document.createTextNode(String(kid)));
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

// ---- 提示条 ----
function toastRoot() {
  let root = document.getElementById("toasts");
  if (!root) {
    root = el("div", { id: "toasts", class: "toasts" });
    document.body.appendChild(root);
  }
  return root;
}

export function toast(message, kind, ms) {
  const root = toastRoot();
  const k = kind || "info";
  const node = el("div", { class: "toast toast-" + k, role: "status" },
    el("span", { class: "toast-dot" }),
    el("span", { class: "toast-text", text: String(message) }),
    el("button", {
      class: "toast-close", title: "关闭", type: "button",
      onclick: () => remove()
    }, "×")
  );
  root.appendChild(node);
  // 最多同时保留 4 条，避免糊住右下角
  while (root.childNodes.length > 4) root.removeChild(root.firstChild);
  let timer = null;
  function remove() {
    if (timer) { clearTimeout(timer); timer = null; }
    if (node.parentNode) node.parentNode.removeChild(node);
  }
  timer = setTimeout(remove, ms || 4600);
  return remove;
}

// ---- 弹窗 ----
let openModals = [];

export function openModal(opts) {
  const o = opts || {};
  const root = document.getElementById("modal-root") || document.body;
  const previousFocus = document.activeElement;
  let closed = false;

  const bodyNode = el("div", { class: "modal-body" });
  if (o.body) {
    if (Array.isArray(o.body)) {
      for (let i = 0; i < o.body.length; i++) if (o.body[i]) bodyNode.appendChild(o.body[i]);
    } else if (o.body instanceof Node) {
      bodyNode.appendChild(o.body);
    } else {
      bodyNode.appendChild(el("p", { text: String(o.body) }));
    }
  }

  const foot = el("div", { class: "modal-foot" });
  const actions = o.actions || [{ label: "关闭", kind: "ghost" }];
  for (let i = 0; i < actions.length; i++) {
    const a = actions[i];
    foot.appendChild(el("button", {
      type: "button",
      class: "btn " + (a.kind === "primary" ? "btn-primary" : a.kind === "danger" ? "btn-danger" : "btn-ghost"),
      onclick: () => {
        if (typeof a.onClick === "function") {
          const keep = a.onClick(handle);
          if (keep === false) return;
        }
        handle.close();
      }
    }, a.label));
  }

  const panel = el("div", { class: "modal", role: "dialog", "aria-modal": "true",
      style: o.width ? { width: o.width + "px" } : null },
    el("div", { class: "modal-head" },
      el("h2", { class: "modal-title", text: o.title || "提示" }),
      el("button", { type: "button", class: "modal-x", title: "关闭 (Esc)",
        onclick: () => handle.close() }, "×")
    ),
    bodyNode,
    o.hideFoot ? null : foot
  );

  const backdrop = el("div", { class: "modal-backdrop" }, panel);
  if (o.dismissible !== false) {
    backdrop.addEventListener("mousedown", (ev) => {
      if (ev.target === backdrop) handle.close();
    });
  }

  function onKey(ev) {
    if (ev.key === "Escape") {
      ev.stopPropagation();
      closeReason("esc");
    }
  }
  document.addEventListener("keydown", onKey, true);

  const handle = {
    root: backdrop,
    panel: panel,
    body: bodyNode,
    close: () => closeReason("close")
  };

  function closeReason(reason) {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey, true);
    if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
    const idx = openModals.indexOf(handle);
    if (idx >= 0) openModals.splice(idx, 1);
    if (previousFocus && typeof previousFocus.focus === "function") {
      try { previousFocus.focus(); } catch (e) { /* 忽略 */ }
    }
    if (typeof o.onClose === "function") o.onClose(reason);
  }

  openModals.push(handle);
  root.appendChild(backdrop);
  const focusTarget = panel.querySelector("input, textarea, button.btn-primary") || panel;
  if (focusTarget && typeof focusTarget.focus === "function") {
    setTimeout(() => { try { focusTarget.focus(); } catch (e) { /* 忽略 */ } }, 0);
  }
  return handle;
}

// 通用确认框，返回 Promise<boolean>
export function confirmDialog(opts) {
  const o = opts || {};
  return new Promise((resolve) => {
    let settled = false;
    const finish = (val) => {
      if (settled) return;
      settled = true;
      resolve(val);
    };
    const body = [];
    if (o.message) body.push(el("p", { class: "modal-text", text: o.message }));
    if (o.extra) body.push(o.extra);
    openModal({
      title: o.title || "请确认",
      body: body,
      width: o.width || 560,
      actions: [
        { label: o.cancelText || "取消", kind: "ghost", onClick: () => finish(false) },
        { label: o.confirmText || "确认", kind: o.danger ? "danger" : "primary",
          onClick: () => finish(true) }
      ],
      onClose: () => finish(false)
    });
  });
}

// ---- 格式化 ----
function pad2(n) {
  return n < 10 ? "0" + n : String(n);
}

export const fmt = {
  num(n) {
    if (n === null || n === undefined || n === "" || isNaN(Number(n))) return "-";
    return Number(n).toLocaleString();
  },
  time(iso) {
    if (!iso) return "-";
    const s = String(iso);
    const t = s.indexOf("T") >= 0 ? s.slice(s.indexOf("T") + 1) : s;
    return t.slice(0, 8);
  },
  datetime(iso) {
    if (!iso) return "-";
    const s = String(iso);
    const t = s.indexOf("T") >= 0 ? s.replace("T", " ") : s;
    return t.slice(0, 19);
  },
  bytes(n) {
    if (n === null || n === undefined || n === "" || isNaN(Number(n))) return "-";
    let v = Number(n);
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v = v / 1024; i++; }
    return (i === 0 ? String(Math.round(v)) : v.toFixed(1)) + " " + units[i];
  },
  dur(sec) {
    if (sec === null || sec === undefined || isNaN(Number(sec))) return "-";
    let s = Math.max(0, Math.round(Number(sec)));
    if (s < 60) return s + " 秒";
    const m = Math.floor(s / 60);
    s = s % 60;
    if (m < 60) return m + " 分 " + pad2(s) + " 秒";
    const h = Math.floor(m / 60);
    return h + " 时 " + pad2(m % 60) + " 分";
  },
  percent(v, digits) {
    if (v === null || v === undefined || isNaN(Number(v))) return "-";
    return Number(v).toFixed(digits === undefined ? 1 : digits) + "%";
  },
  truncate(s, n) {
    const t = String(s === null || s === undefined ? "" : s);
    const max = n || 60;
    return t.length > max ? t.slice(0, max) + "…" : t;
  }
};

// 简单骨架屏
export function skeleton(rows) {
  const box = el("div", { class: "skeleton-box" });
  const count = rows || 3;
  for (let i = 0; i < count; i++) box.appendChild(el("div", { class: "skeleton-line" }));
  return box;
}

// 空状态
export function emptyState(title, desc, actionNode) {
  return el("div", { class: "empty" },
    el("div", { class: "empty-title", text: title }),
    desc ? el("div", { class: "empty-desc", text: desc }) : null,
    actionNode || null
  );
}

// 错误条
export function errorBar(message) {
  return el("div", { class: "errorbar", role: "alert" },
    el("span", { class: "errorbar-icon", text: "⚠" }),
    el("span", { text: String(message) })
  );
}

// ---- 流水线阶段（契约固定顺序，见 API.md 第 2 节）----
export const STAGES = ["render", "textlayer", "ocr", "repair", "layout", "md", "verify", "audit", "epub"];

export const STAGE_LABELS = {
  render: "渲染",
  textlayer: "文字层",
  ocr: "OCR",
  repair: "修复",
  layout: "版面",
  md: "成文",
  verify: "校验",
  audit: "审计",
  epub: "EPUB"
};

export const STATUS_LABELS = {
  ok: "正常",
  degraded: "降级",
  failed: "失败",
  skipped: "跳过",
  missing: "未跑",
  running: "进行中",
  pending: "排队中"
};

// 把后端 status 翻成统一的 class 后缀
export function statusClass(status) {
  const s = String(status || "missing");
  if (s === "ok") return "st-ok";
  if (s === "degraded") return "st-degraded";
  if (s === "failed") return "st-failed";
  if (s === "skipped") return "st-skipped";
  if (s === "running") return "st-running";
  return "st-missing";
}

// 从 run.stages 里取某阶段记录（没有就返回 missing 占位）
export function stageOf(run, stage) {
  const list = (run && run.stages) || [];
  for (let i = 0; i < list.length; i++) {
    if (list[i] && list[i].stage === stage) return list[i];
  }
  return { stage: stage, status: "missing", at: "", count: null, detail: "" };
}

// 9 格阶段状态条
export function stageStrip(run, size) {
  const wrap = el("div", { class: "stage-strip" + (size === "lg" ? " stage-strip-lg" : "") });
  for (let i = 0; i < STAGES.length; i++) {
    const name = STAGES[i];
    const rec = stageOf(run, name);
    let tip = STAGE_LABELS[name] + " · " + (STATUS_LABELS[rec.status] || rec.status);
    if (rec.count !== null && rec.count !== undefined && rec.count !== "") tip += " · " + rec.count;
    if (rec.at) tip += " · " + fmt.datetime(rec.at);
    if (rec.detail) tip += "\n" + rec.detail;
    wrap.appendChild(el("div", {
      class: "stage-cell " + statusClass(rec.status),
      title: tip
    }, el("span", { class: "stage-cell-name", text: STAGE_LABELS[name] })));
  }
  return wrap;
}



