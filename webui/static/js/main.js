// 应用入口：全局 store、哈希路由、首屏 GET /api/state。
// 路由与术语见 docs/UI-DESIGN.md §1：
//   #/tasks  任务列表 | #/new  新建任务 | #/task/:id  处理进度
//   #/task/:id/proofread  校对 | #/task/:id/output  成品
import { el, clear, toast, errorBar, skeleton } from "./ui.js";
import { getState, describeError } from "./api.js";
import * as tasksView from "./tasks.js";
import * as newView from "./new.js";
import * as progressView from "./progress.js";
import * as proofreadView from "./proofread.js";
import * as outputView from "./output.js";
import { openSettings } from "./settings.js";

// ---- 全局 store ----
export const store = {
  state: null,
  loading: false,
  error: null,
  lastLoadedAt: 0
};

let loadPromise = null;

export function loadState(force) {
  if (store.loading && loadPromise) return loadPromise;
  if (!force && store.state && Date.now() - store.lastLoadedAt < 2000) {
    return Promise.resolve(store.state);
  }
  store.loading = true;
  loadPromise = getState().then((data) => {
    store.state = data || {};
    store.error = null;
    store.lastLoadedAt = Date.now();
    store.loading = false;
    loadPromise = null;
    renderHeader();
    return store.state;
  }).catch((err) => {
    store.loading = false;
    loadPromise = null;
    store.error = describeError(err);
    renderHeader();
    throw err;
  });
  return loadPromise;
}

export function findRun(taskId) {
  const list = (store.state && store.state.runs) || [];
  for (let i = 0; i < list.length; i++) {
    if (list[i] && list[i].id === taskId) return list[i];
  }
  return null;
}

export function findPreset(presetId) {
  const list = (store.state && store.state.presets) || [];
  for (let i = 0; i < list.length; i++) {
    if (list[i] && list[i].id === presetId) return list[i];
  }
  return null;
}

// ---- 顶栏 ----
const NAV = [
  { href: "#/tasks", label: "任务列表", match: ["/tasks", "/", ""] },
  { href: "#/new", label: "新建任务", match: ["/new"] }
];

function renderHeader() {
  const bar = document.getElementById("topbar");
  if (!bar) return;
  clear(bar);
  const st = store.state || {};
  const path = currentPath();

  bar.appendChild(el("div", { class: "brand" },
    el("span", { class: "brand-mark", text: "书" }),
    el("span", { class: "brand-text", text: "bookflow 工作台" })
  ));

  const nav = el("nav", { class: "nav" });
  NAV.forEach((item) => {
    const active = item.match.indexOf(path) >= 0;
    nav.appendChild(el("a", { class: "nav-link" + (active ? " active" : ""), href: item.href }, item.label));
  });
  bar.appendChild(nav);

  bar.appendChild(el("div", { class: "topbar-right" },
    st.cwd ? el("span", { class: "chip", title: "当前工作目录", text: st.cwd }) : null,
    el("button", {
      class: "btn btn-ghost btn-sm", type: "button", title: "重新拉取 /api/state",
      onclick: () => {
        loadState(true).then(() => { navigate(location.hash, true); })
          .catch((e) => toast(describeError(e), "error"));
      }
    }, "刷新"),
    // 设置放顶栏而不是塞进校对页：快捷键是跨任务的偏好，
    // 而校对页每次按键都要落到「判定」上，多一个入口就多一次误点。
    el("button", {
      class: "btn btn-ghost btn-sm", type: "button", title: "设置（快捷键等）",
      onclick: () => openSettings({
        // 改完键要立刻反映到界面：校对页的键帽显示的是当前绑定，
        // 不重渲染就会停在旧值上，看着像没生效
        onChange: () => {
          const path = currentPath();
          if (path.indexOf("/proofread") >= 0) navigate(location.hash, true);
        }
      })
    }, "设置")
  ));
}

// ---- 路由 ----
let currentCleanup = null;
let renderToken = 0;

function currentPath() {
  const raw = (location.hash || "").replace(/^#/, "");
  const q = raw.indexOf("?");
  return q >= 0 ? raw.slice(0, q) : raw;
}

function parseRoute() {
  const raw = (location.hash || "#/tasks").replace(/^#/, "");
  const qIdx = raw.indexOf("?");
  const path = (qIdx >= 0 ? raw.slice(0, qIdx) : raw) || "/tasks";
  const query = {};
  if (qIdx >= 0) {
    const parts = raw.slice(qIdx + 1).split("&");
    for (let i = 0; i < parts.length; i++) {
      const p = parts[i];
      if (!p) continue;
      const eq = p.indexOf("=");
      const k = decodeURIComponent(eq >= 0 ? p.slice(0, eq) : p);
      const v = eq >= 0 ? decodeURIComponent(p.slice(eq + 1)) : "";
      query[k] = v;
    }
  }

  const seg = path.split("/").filter((s) => s !== "");
  if (seg.length === 0) return { name: "tasks", params: {}, query: query };
  if (seg[0] === "tasks") return { name: "tasks", params: {}, query: query };
  if (seg[0] === "new") return { name: "new", params: {}, query: query };

  if (seg[0] === "task" && seg.length >= 2) {
    // 任务 id 可以多段（新布局是 "runs/<book-id>"）。
    // 规矩只有一条：末段是视图名就吃掉它，剩下全部是 id。
    // 猜错的后果是把人送去一个 404 页面，所以宁可把不认识的字当 id。
    const rest = seg.slice(1);
    let view = "progress";
    const last = rest[rest.length - 1];
    if (last === "proofread" || last === "output") {
      view = last;
      rest.pop();
    }
    if (rest.length) {
      const id = rest.map((s) => decodeURIComponent(s)).join("/");
      return { name: view, params: { id: id }, query: query };
    }
  }

  // 旧路由兜底：#/runs、#/setup、#/run/:id、.../triage、.../inspect
  if (seg[0] === "runs") return { name: "tasks", params: {}, query: query, legacy: true };
  if (seg[0] === "setup") return { name: "new", params: {}, query: query, legacy: true };
  if (seg[0] === "run" && seg.length >= 2) {
    const rest = seg.slice(1);
    let view = "progress";
    const last = rest[rest.length - 1];
    if (last === "triage") view = "proofread";
    else if (last === "inspect") view = "output";
    if (view !== "progress") rest.pop();
    if (rest.length) {
      return { name: view, params: { id: rest.map((s) => decodeURIComponent(s)).join("/") },
               query: query, legacy: true };
    }
  }

  return { name: "notfound", params: { path: path }, query: query };
}

export function navigate(hash, replace) {
  if (replace) {
    const url = location.pathname + location.search + hash;
    history.replaceState(null, "", url);
    route();
  } else if (location.hash === hash) {
    route();
  } else {
    location.hash = hash;
  }
}

function setCrumbs(label) {
  const crumb = document.getElementById("crumbs");
  if (crumb) clear(crumb).appendChild(el("span", { text: label }));
}

function renderError(err, retry) {
  const main = document.getElementById("main");
  if (!main) return;
  clear(main).appendChild(el("div", { class: "view" },
    el("h1", { class: "page-title", text: "出错了" }),
    errorBar(err),
    el("div", { class: "row-gap", style: { marginTop: "16px" } },
      el("button", { class: "btn btn-primary", type: "button", onclick: retry }, "重试"))
  ));
}

const VIEWS = {
  tasks: { mod: tasksView, crumb: "任务列表" },
  new: { mod: newView, crumb: "新建任务" },
  progress: { mod: progressView, crumb: "处理进度" },
  proofread: { mod: proofreadView, crumb: "校对" },
  output: { mod: outputView, crumb: "成品" }
};

function route() {
  const main = document.getElementById("main");
  if (!main) return;
  if (typeof currentCleanup === "function") {
    try { currentCleanup(); } catch (e) { /* 清理失败不阻塞导航 */ }
  }
  currentCleanup = null;

  const token = ++renderToken;
  const r = parseRoute();
  renderHeader();

  // 旧路由自动改写成新路由，#/run/x/triage -> #/task/x/proofread
  if (r.legacy) {
    const parts = [r.name === "progress" ? "" : r.name].filter(Boolean);
    let target = r.name === "tasks" ? "#/tasks" : r.name === "new" ? "#/new"
      : "#/task/" + String(r.params.id || "").split("/").map(encodeURIComponent).join("/")
        + (parts.length ? "/" + parts[0] : "");
    if (location.hash !== target) {
      history.replaceState(null, "", location.pathname + location.search + target);
    }
    return route();
  }

  const ctx = {
    params: r.params,
    query: r.query,
    store: store,
    loadState: loadState,
    findRun: findRun,
    findPreset: findPreset,
    navigate: navigate,
    alive: () => token === renderToken,
    setCrumbs: setCrumbs
  };

  const view = VIEWS[r.name];
  if (!view) {
    setCrumbs("页面不存在");
    clear(main).appendChild(el("div", { class: "view" },
      el("h1", { class: "page-title", text: "页面不存在" }),
      el("p", { class: "muted", text: "地址 " + (r.params.path || "") + " 没有对应的页面。" }),
      el("a", { class: "btn btn-primary", href: "#/tasks" }, "回到任务列表")
    ));
    return;
  }

  setCrumbs(view.crumb + (r.params.id ? " · " + r.params.id : ""));
  clear(main).appendChild(skeleton(4));
  loadState(false).then(() => {
    if (token !== renderToken) return;
    clear(main);
    let node;
    try {
      node = view.mod.mount(ctx);
    } catch (e) {
      renderError("页面渲染失败：" + describeError(e), () => route());
      return;
    }
    if (typeof node === "function") {
      currentCleanup = node;
    } else if (node) {
      main.appendChild(node);
      if (typeof node.__cleanup === "function") currentCleanup = node.__cleanup;
    }
  }).catch((err) => {
    if (token !== renderToken) return;
    renderError(describeError(err), () => { loadState(true).catch(() => {}); route(); });
  });

  window.scrollTo(0, 0);
}

// ---- 启动 ----
function boot() {
  if (!location.hash) {
    location.replace(location.pathname + location.search + "#/tasks");
  }
  renderHeader();
  loadState(true).catch(() => { /* 首屏失败交给页面自己显示 */ });
  window.addEventListener("hashchange", route);
  window.addEventListener("error", (ev) => {
    if (ev && ev.message) toast("脚本错误：" + ev.message, "error", 7000);
  });
  window.addEventListener("unhandledrejection", (ev) => {
    const reason = ev && ev.reason ? (ev.reason.message || ev.reason) : "未知";
    toast("操作失败：" + String(reason), "error", 7000);
  });
  route();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
