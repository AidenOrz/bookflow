// 快捷键绑定：默认值 + 用户覆盖（localStorage）。
//
// 为什么单独一个模块：校对页是本工具唯一键盘密集的界面，但「哪颗键干哪件事」
// 是使用者的偏好，不该硬编码在视图里。抽出来之后视图只问 matchKey()，
// 设置面板只问 setKey()，两边互不知情。
//
// 有一条纪律：**一个动作只对应一颗键，一颗键只对应一个动作**。
// 所以原先 ↑↓ 与 j/k 的双绑被收掉了——双绑会让「设置面板里当前生效的是哪颗」
// 说不清，也让人没法改掉自己不想用的那颗。缩放保留 4 颗独立键而不是「一颗键循环」：
// 直接跳 300% 只需要一次按键，循环要按三次还得盯着画面数。
//
// 存不下偏好不能崩：隐私模式、磁盘配额、被禁用的 storage 都会抛异常，
// 而「快捷键存不住」的代价远小于「整页白屏」。

const STORE_KEY = "bookflow.keys.v1";

// 顺序即设置面板里的显示顺序：先判定，再导航，再视图，最后过滤
export const ACTIONS = [
  { id: "useA",    label: "采用 A 候选",       def: "a" },
  { id: "useB",    label: "采用 B 候选",       def: "b" },
  { id: "useC",    label: "自行输入",         def: "c" },
  { id: "skip",    label: "跳过这一条",        def: "s" },
  { id: "next",    label: "下一条",           def: "ArrowDown" },
  { id: "prev",    label: "上一条",           def: "ArrowUp" },
  { id: "toggle",  label: "切换 整页 / 切片",   def: "z" },
  { id: "zoom100", label: "缩放到 100%",      def: "1" },
  { id: "zoom150", label: "缩放到 150%",      def: "2" },
  { id: "zoom200", label: "缩放到 200%",      def: "3" },
  { id: "zoom300", label: "缩放到 300%",      def: "4" },
  { id: "filter",  label: "聚焦分组过滤",      def: "/" }
];

// 这几个不接：Esc 要留给弹窗关闭，Tab 是焦点遍历，Enter 会误触当前聚焦的按钮。
// 允许绑定它们的后果是「设置完就再也关不掉弹窗」，比不让绑更糟。
const RESERVED = {
  Escape: "Esc 用于关闭弹窗",
  Tab: "Tab 用于切换焦点",
  Enter: "Enter 会误触当前聚焦的按钮"
};

function defaults() {
  const map = {};
  for (let i = 0; i < ACTIONS.length; i++) map[ACTIONS[i].id] = ACTIONS[i].def;
  return map;
}

function storage() {
  try {
    return window.localStorage || null;
  } catch (e) {
    return null;
  }
}

let cache = null;

export function keys() {
  if (cache) return Object.assign({}, cache);
  const base = defaults();
  const s = storage();
  if (s) {
    try {
      const raw = s.getItem(STORE_KEY);
      if (raw) {
        const saved = JSON.parse(raw);
        for (let i = 0; i < ACTIONS.length; i++) {
          const id = ACTIONS[i].id;
          const v = saved ? saved[id] : null;
          if (typeof v === "string" && v) base[id] = v;
        }
      }
    } catch (e) {
      // 存的内容坏了就当没存过：默认值一定可用，坏数据不该挡住整个界面
    }
  }
  cache = base;
  return Object.assign({}, cache);
}

export function isDefault() {
  const cur = keys();
  const def = defaults();
  for (let i = 0; i < ACTIONS.length; i++) {
    const id = ACTIONS[i].id;
    if (cur[id] !== def[id]) return false;
  }
  return true;
}

function save(map) {
  cache = Object.assign({}, map);
  const s = storage();
  if (!s) return false;
  try {
    s.setItem(STORE_KEY, JSON.stringify(map));
    return true;
  } catch (e) {
    return false;
  }
}

// 恢复默认。返回是否落盘（false 表示这次会话生效但没存住）
export function resetKeys() {
  return save(defaults());
}

// 返回 {ok, conflict, reason, persisted}
// conflict 是占用了这颗键的另一个动作（**不替换**它，让人自己先清空）
export function setKey(actionId, key) {
  const k = normalizeKey(key);
  if (!k) return { ok: false, conflict: null, reason: "没有识别到按键" };
  if (RESERVED[k]) return { ok: false, conflict: null, reason: RESERVED[k] + "，换一颗键" };
  const map = keys();
  for (let i = 0; i < ACTIONS.length; i++) {
    const a = ACTIONS[i];
    if (a.id !== actionId && map[a.id] === k) {
      return { ok: false, conflict: a, reason: "这颗键已经给了「" + a.label + "」，先把它改掉" };
    }
  }
  map[actionId] = k;
  const persisted = save(map);
  return { ok: true, conflict: null, persisted: persisted };
}

// 把 KeyboardEvent.key 归一化：字母不分大小写，其余（ArrowDown / " " / F2）原样。
// 不归一化的大小写会让「按了 A 没反应、按 a 才有」这种问题极难自查。
export function normalizeKey(raw) {
  const k = String(raw === undefined || raw === null ? "" : raw);
  if (!k) return "";
  if (k.length === 1) return k.toLowerCase();
  return k;
}

// 键帽上显示什么：↓ 比 ArrowDown 好认，Esc 比 Escape 短
export function keyLabel(k) {
  const s = String(k || "");
  if (!s) return "—";
  if (s === " ") return "Space";
  if (s === "ArrowDown") return "↓";
  if (s === "ArrowUp") return "↑";
  if (s === "ArrowLeft") return "←";
  if (s === "ArrowRight") return "→";
  if (s === "Escape") return "Esc";
  if (s === "PageDown") return "PgDn";
  if (s === "PageUp") return "PgUp";
  if (s.length === 1) return s.toUpperCase();
  return s;
}

// 事件 -> 动作 id（没有绑定返回 ""）。
// 带 Ctrl/Meta/Alt 的组合键一律不匹配：那些是浏览器和系统的，抢了会出事。
export function matchKey(ev) {
  if (!ev) return "";
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return "";
  const k = normalizeKey(ev.key);
  if (!k) return "";
  const map = keys();
  for (let i = 0; i < ACTIONS.length; i++) {
    if (map[ACTIONS[i].id] === k) return ACTIONS[i].id;
  }
  return "";
}
