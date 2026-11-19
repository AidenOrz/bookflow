// 设置面板：目前只有一项——校对页的快捷键。
//
// 为什么放在顶栏而不是校对页里：设置是全局的（存 localStorage，跨任务保留），
// 而校对页是「一条一条判」的密集操作界面，多一个入口就多一次误点。
//
// 改键的交互是「点键帽 -> 按新键」，不用下拉框：
// 下拉框要把 60 多个键名列出来，人还得在列表里找「我刚刚按的那颗」，
// 而捕获按键只要一次点击 + 一次按键。
import { el, toast, openModal } from "./ui.js";
import { ACTIONS, keys, setKey, resetKeys, isDefault, keyLabel, normalizeKey } from "./keys.js";

// 一个键帽按钮：默认显示当前键，点击后进入「等待按键」状态。
// 捕获阶段用捕获（capture=true）并 stopPropagation，否则校对页的
// document 级 keydown 会先把它当成判定指令吃掉——改键改到一半跳出确认框。
function keyCapButton(action, onChanged) {
  const cur = keys()[action.id] || "";
  const btn = el("button", {
    class: "keycap", type: "button",
    title: "点击后按一个新键",
    onclick: () => capture()
  }, keyLabel(cur));

  function capture() {
    btn.classList.add("is-capturing");
    btn.textContent = "按键…";
    let done = false;

    function finish() {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey, true);
      const now = keys()[action.id] || "";
      btn.classList.remove("is-capturing");
      btn.textContent = keyLabel(now);
      btn.blur();
    }

    function onKey(ev) {
      ev.preventDefault();
      ev.stopPropagation();
      // Esc 用来放弃本次改键（此时 Esc 还没绑给任何动作，安全）
      if (ev.key === "Escape") { finish(); return; }
      const raw = normalizeKey(ev.key);
      // 单独按修饰键不算：Shift/Ctrl 常常是组合键的前半段
      if (raw === "Shift" || raw === "Control" || raw === "Alt" || raw === "Meta") return;
      const res = setKey(action.id, raw);
      if (!res.ok) {
        toast(res.reason, "warn", 5200);
        return;
      }
      if (res.persisted === false) {
        toast("快捷键本次有效，但没能存到本地（浏览器禁用了存储）", "warn", 6000);
      }
      finish();
      if (typeof onChanged === "function") onChanged();
    }

    document.addEventListener("keydown", onKey, true);
  }

  return btn;
}

function buildBody(onChanged) {
  const wrap = el("div", { class: "keys-panel" });

  wrap.appendChild(el("p", { class: "modal-text" },
    "校对页的键盘绑定。点键帽后直接按你想用的那个键，改完立即生效，存在本机浏览器里。"));

  const table = el("div", { class: "keys-table" });
  const head = el("div", { class: "keys-row keys-head" },
    el("span", { text: "动作" }),
    el("span", { text: "按键" })
  );
  table.appendChild(head);

  ACTIONS.forEach((action) => {
    table.appendChild(el("div", { class: "keys-row" },
      el("span", { class: "keys-label", text: action.label }),
      keyCapButton(action, onChanged)
    ));
  });
  wrap.appendChild(table);

  wrap.appendChild(el("div", { class: "keys-note" },
    el("div", null, "Esc 用于关闭弹窗、Tab 用于切换焦点、Enter 会误触当前按钮，这三颗键不开放绑定。"),
    el("div", null, "带 Ctrl / Alt / Cmd 的组合键一律不匹配，它们留给浏览器和系统。"),
    el("div", null, "同一颗键不能同时给两个动作：要换用请先把原动作改掉。")
  ));

  return wrap;
}

export function openSettings(opts) {
  const o = opts || {};
  let body;

  const resetBtn = el("button", {
    class: "btn btn-ghost", type: "button",
    onclick: () => {
      const saved = resetKeys();
      toast(saved ? "快捷键已恢复默认" : "已恢复默认（本次有效，没能存到本地）",
            saved ? "success" : "warn", 5000);
      // 重建整张表最省事，也避免逐个键帽去同步状态导致漏掉某个
      rebuild();
      if (typeof o.onChange === "function") o.onChange();
    }
  }, "恢复默认");

  const doneBtn = el("button", { class: "btn btn-primary", type: "button" }, "关闭");

  function rebuild() {
    const fresh = buildBody(() => {
      if (typeof o.onChange === "function") o.onChange();
    });
    if (body.parentNode) body.parentNode.replaceChild(fresh, body);
    body = fresh;
    resetBtn.disabled = isDefault();
  }

  body = buildBody(() => {
    resetBtn.disabled = isDefault();
    if (typeof o.onChange === "function") o.onChange();
  });
  resetBtn.disabled = isDefault();

  // 按钮自己建，不走 openModal 的 actions 数组：那样拿不到 handle 就没法从内部关闭
  const modal = openModal({
    title: "设置",
    body: body,
    width: 560,
    hideFoot: true
  });

  const foot = el("div", { class: "modal-foot" }, resetBtn, el("span", { class: "spacer" }));
  doneBtn.addEventListener("click", () => modal.close());
  foot.appendChild(doneBtn);
  modal.panel.appendChild(foot);

  return modal;
}
