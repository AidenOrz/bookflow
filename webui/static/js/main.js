// 应用入口：全局 store、hash 路由、首屏 GET /api/state。

import { el, clear, toast, errorBar, skeleton } from "./ui.js";
import { getState, describeError } from "./api.js";
import * as runsView from "./runs.js";
import * as setupView from "./setup.js";
import * as pipelineView from "./pipeline.js";
import * as triageView from "./triage.js";
import * as inspectView from "./inspect.js";

// ---- 全局 store ----
export const store = {
  state: null,
  loading: false,
  error: null,
  lastLoadedAt: 0
};

let loadPromise = null;

// 首屏只发这一个请求（契约要求）
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

export function findRun(runId) {
  const list = (store.state && store.state.runs) || [];
  for (let i = 0; i < list.length; i++) {
    if (list[i] && list[i].id === runId) return list[i];
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
function renderHeader() {
  const bar = document.getElementById("topbar");
  if (!bar) return;
  clear(bar);
  const st = store.state || {};
  const path = currentPath();

  const navLink = (href, label, match) => el("a", {
    class: "nav-link" + (match ? " active" : ""),
    href: href
  }, label);

  bar.appendChild(el("div", { class: "brand" },
    el("span", { class: "brand-mark", text: "书" }),
    el("span", { class: "brand-text", text: "bookflow 工作台" })
  ));
  bar.appendChild(el("nav", { class: "nav" },
    navLink("#/runs", "运行列表", path === "/runs" || path === "/" || path === ""),
    navLink("#/setup", "新建任务", path === "/setup")
  ));
  bar.appendChild(el("div", { class: "topbar-right" },
    st.cwd ? el("span", { class: "cwd-chip", title: "当前工作目录", text: st.cwd }) : null,
    el("span", { class: "version-chip", text: st.version || "bookflow-webui/1" }),
    el("button", {
      class: "btn btn-ghost btn-sm", type: "button", title: "重新拉取 /api/state",
      onclick: () => {
        loadState(true).then(() => { navigate(location.hash, true); })
          .catch((e) => toast(describeError(e), "error"));
      }
    }, "刷新")
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
  const raw = (location.hash || "#/runs").replace(/^#/, "");
  const qIdx = raw.indexOf("?");
  const path = (qIdx >= 0 ? raw.slice(0, qIdx) : raw) || "/runs";
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
  if (seg.length === 0) return { name: "runs", params: {}, query: query };
  if (seg[0] === "runs") return { name: "runs", params: {}, query: query };
  if (seg[0] === "setup") return { name: "setup", params: {}, query: query };
  if (seg[0] === "run" && seg.length >= 2) {
    // run id 可以有多段：新布局的 id 是 "runs/<book-id>"。
    // 一开始这里只取 seg[1]，于是 #/run/runs/book-x/inspect 被拆成
    // id="runs" + 末段="book-x"，既不是 triage 也不是 inspect，直接掉进 404 页。
    // 规矩只有一条：末段是视图名就吃掉它，剩下全部是 id。
    const rest = seg.slice(1);
    let view = "run";
    const last = rest[rest.length - 1];
    if (last === "triage" || last === "inspect") {
      view = last;
      rest.pop();
    }
    if (rest.length) {
      const id = rest.map((s) => decodeURIComponent(s)).join("/");
      return { name: view, params: { id: id }, query: query };
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
    el("div", { class: "row-gap" },
      el("button", { class: "btn btn-primary", type: "button", onclick: retry }, "重试"))
  ));
}

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

  const mount = (mod) => {
    clear(main).appendChild(skeleton(4));
    // 首屏先确保 /api/state 到手
    loadState(false).then(() => {
      if (token !== renderToken) return;
      clear(main);
      const node = mod.mount(ctx);
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
  };

  window.scrollTo(0, 0);
  if (r.name === "runs") {
    setCrumbs("运行列表");
    mount(runsView);
  } else if (r.name === "setup") {
    setCrumbs("新建任务");
    mount(setupView);
  } else if (r.name === "run") {
    setCrumbs("流水线看板 · " + r.params.id);
    mount(pipelineView);
  } else if (r.name === "triage") {
    setCrumbs("裁决台 · " + r.params.id);
    mount(triageView);
  } else if (r.name === "inspect") {
    setCrumbs("成书检视 · " + r.params.id);
    mount(inspectView);
  } else {
    clear(main).appendChild(el("div", { class: "view" },
      el("h1", { class: "page-title", text: "页面不存在" }),
      el("p", { class: "muted", text: "地址 " + (r.params.path || "") + " 没有对应的页面。" }),
      el("a", { class: "btn btn-primary", href: "#/runs" }, "回到运行列表")
    ));
  }
}

// ---- 启动 ----
function boot() {
  if (!location.hash) {
    location.replace(location.pathname + location.search + "#/runs");
  }
  renderHeader();
  loadState(true).catch(() => { /* 首屏失败交给页面自己显示 */ });
  window.addEventListener("hashchange", route);
  // 顶层错误兜底，绝不静默
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
