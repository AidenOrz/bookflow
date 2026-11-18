// 新建任务页（#/setup）：三步显式流程 —— 选输入 / 选预设 / 审批并启动。
import { el, clear, fmt, toast, errorBar } from "./ui.js";
import { uploadPdf, createRun, describeError } from "./api.js";

const EDITABLE_KEYS = ["dpi", "device", "model", "text_layer"];

// 可编辑参数的默认基底（预设没给的键用它兜底）
const PARAM_DEFAULTS = { dpi: 300, device: "auto", model: "small" };

const PARAM_HINTS = {
  dpi: "渲染分辨率，扫描本常用 300，低质图可降到 200",
  device: "OCR 计算设备（auto / cpu / cuda）",
  model: "OCR 模型档位（small / medium / large）",
  text_layer: "文字层策略：off 全程 OCR；verify 抽文字层与 OCR 对账"
};

const FIELD_LABELS = {
  dpi: "DPI",
  device: "设备",
  model: "模型",
  text_layer: "文字层",
  conservative_fix: "保守修形（conservative_fix）",
  no_layout: "跳过版面（no_layout）",
  limit_pages: "限制页数（limit_pages）"
};

// 不具信息量的文件名/目录名，猜 book_id 时跳过
const GENERIC_STEMS = ["source", "book", "input", "scan", "document", "file", "pdf", "tmp", "temp"];
const GENERIC_DIRS = ["workspace", "books", "book", "documents", "desktop", "downloads", "input", "tmp", "temp", "c", "d", "e", "f"];

// 稳定短哈希（同一文件名每次得到同一个 id）
function shortHash(s) {
  let h = 5381;
  const str = String(s || "");
  for (let i = 0; i < str.length; i++) h = ((h * 33) ^ str.charCodeAt(i)) >>> 0;
  return h.toString(36).slice(0, 6);
}

function slugify(s) {
  return String(s || "")
    .toLowerCase()
    .replace(/\.[a-z0-9]+$/, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function baseParams(preset) {
  const out = { dpi: PARAM_DEFAULTS.dpi, device: PARAM_DEFAULTS.device, model: PARAM_DEFAULTS.model };
  const p = (preset && preset.params) || {};
  const keys = Object.keys(p);
  for (let i = 0; i < keys.length; i++) out[keys[i]] = p[keys[i]];
  return out;
}

function chipList(params) {
  const keys = Object.keys(params || {});
  if (keys.length === 0) return el("span", { class: "muted", text: "（无显式参数）" });
  const wrap = el("div", { class: "chips" });
  for (let i = 0; i < keys.length; i++) {
    wrap.appendChild(el("span", { class: "chip" },
      el("span", { class: "chip-k", text: keys[i] }), el("span", { class: "chip-v", text: String(params[keys[i]]) })));
  }
  return wrap;
}

export function mount(ctx) {
  const state = ctx.store.state || {};
  const presets = Array.isArray(state.presets) ? state.presets : [];
  const root = el("div", { class: "view" });

  // ---- 本地草稿状态 ----
  const draft = {
    source: null,          // {kind: "upload"|"path", path, pages, meta, upload_id}
    bookId: "",
    title: "",
    author: "",
    publisher: "",
    presetId: presets.length ? presets[0].id : "",
    params: {},
    stageMode: "all",
    pickedStages: ["render", "textlayer", "ocr", "repair", "layout", "md", "verify", "audit", "epub"],
    acked: false
  };

  root.appendChild(el("div", { class: "page-head" },
    el("div", null,
      el("h1", { class: "page-title", text: "新建任务" }),
      el("p", { class: "page-sub", text: "上传 PDF → 选预设 → 核对最终参数后启动。三步都必须走完。" })
    )
  ));

  // ================= 步骤一 =================
  const step1State = el("span", { class: "step-state" });
  const step1Body = el("div", { class: "step-body" });
  const step1 = el("section", { class: "step", id: "step-1" },
    el("header", { class: "step-head" },
      el("span", { class: "step-no", text: "1" }),
      el("h2", { class: "step-title", text: "选输入" }),
      step1State
    ),
    step1Body
  );

  const fileInput = el("input", {
    type: "file", accept: ".pdf,application/pdf", class: "hidden-input",
    onchange: (ev) => {
      const f = ev.target.files && ev.target.files[0];
      if (f) doUpload(f);
      ev.target.value = "";
    }
  });

  const dropZone = el("div", { class: "dropzone" },
    el("div", { class: "dropzone-icon", text: "⇪" }),
    el("div", { class: "dropzone-title", text: "把 PDF 拖到这里" }),
    el("div", { class: "dropzone-desc", text: "或者点下面的按钮选择文件；也可以直接手输服务端可读的绝对路径。" }),
    el("button", { class: "btn btn-primary", type: "button", onclick: () => fileInput.click() }, "选择 PDF 文件"),
    fileInput,
    el("div", { class: "upload-progress", hidden: true },
      el("div", { class: "upload-progress-fill" }))
  );

  dropZone.addEventListener("dragover", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    dropZone.classList.add("dragging");
  });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragging"));
  dropZone.addEventListener("drop", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    dropZone.classList.remove("dragging");
    const f = ev.dataTransfer && ev.dataTransfer.files ? ev.dataTransfer.files[0] : null;
    if (!f) return;
    if (!/\.pdf$/i.test(f.name)) { toast("只接受 .pdf 文件", "error"); return; }
    doUpload(f);
  });

  const pathInput = el("input", {
    class: "input", type: "text", placeholder: "例如 E:\\workspace\\bookflow\\input\\lacan\\source.pdf",
    oninput: () => { manualError.hidden = true; }
  });
  const manualError = el("div", { class: "inline-error", hidden: true });
  const manualUse = el("button", {
    class: "btn", type: "button",
    onclick: () => {
      const v = pathInput.value.trim();
      if (!v) { showManualError("请先填写 PDF 的绝对路径。"); return; }
      if (!/\.pdf$/i.test(v)) { showManualError("路径必须以 .pdf 结尾。"); return; }
      // Windows 用户经常粘贴正斜杠路径，两种分隔符都接受
      if (!/^[A-Za-z]:[\\/]/.test(v) && !/^\\\\/.test(v) && !/^\//.test(v)) {
        showManualError("请填绝对路径（如 E:\\books\\x.pdf 或 E:/books/x.pdf）。");
        return;
      }
      useManualPath(v);
    }
  }, "使用这个路径");

  function showManualError(msg) {
    manualError.hidden = false;
    clear(manualError).appendChild(el("span", { text: msg }));
  }

  step1Body.appendChild(dropZone);
  step1Body.appendChild(el("div", { class: "or-sep" }, el("span", { text: "或" })));
  step1Body.appendChild(el("div", { class: "manual-row" }, pathInput, manualUse));
  step1Body.appendChild(manualError);

  const sourceCard = el("div", { class: "source-card", hidden: true });
  step1Body.appendChild(sourceCard);

  async function doUpload(file) {
    const fill = dropZone.querySelector(".upload-progress-fill");
    const box = dropZone.querySelector(".upload-progress");
    box.hidden = false;
    fill.style.width = "0%";
    dropZone.classList.add("busy");
    try {
      const res = await uploadPdf(file, (ratio) => {
        fill.style.width = Math.round(ratio * 100) + "%";
      });
      setSource({
        kind: "upload",
        path: res.path || "",
        pages: res.pages === undefined ? null : res.pages,
        meta: res.meta || {},
        upload_id: res.upload_id || ""
      });
      toast("上传完成：" + (res.path || file.name), "ok");
    } catch (err) {
      const msg = describeError(err);
      toast(msg, "error", 8000);
      const old = dropZone.querySelector(".errorbar");
      if (old) old.remove();
      dropZone.appendChild(errorBar(msg));
    } finally {
      box.hidden = true;
      dropZone.classList.remove("busy");
    }
  }

  function useManualPath(p) {
    // 正反斜杠都可能出现，统一按 / 切分
    const seg = p.replace(/[\\/]+/g, "/").split("/").filter((s) => s !== "");
    const fileName = (seg[seg.length - 1] || "").replace(/\.pdf$/i, "");
    const parent = seg.length >= 2 ? seg[seg.length - 2] : "";
    // book_id 只认「文件名」和「父目录名」，绝不拿整条路径去 slug（会得到 e-books 这种脏 id）
    let guessId = slugify(fileName);
    if (GENERIC_STEMS.indexOf(guessId) >= 0) guessId = "";       // source.pdf / book.pdf 之类不具信息量
    if (!guessId) {
      const parentSlug = slugify(parent);
      if (GENERIC_DIRS.indexOf(parentSlug) < 0 && !/^[a-z]$/.test(parentSlug)) guessId = parentSlug;
    }
    // 中文文件名 slug 后为空、父目录又是通用目录时，给一个稳定可编辑的占位 id
    if (!guessId) guessId = "book-" + shortHash(fileName);
    setSource({
      kind: "path",
      path: p,
      pages: null,
      meta: { title: fileName },
      upload_id: guessId
    });
    toast("已记录路径：" + p, "ok");
  }

  function setSource(src) {
    draft.source = src;
    const meta = src.meta || {};
    draft.title = meta.title || draft.title || "";
    draft.author = meta.author || "";
    draft.publisher = meta.publisher || "";
    const guess = src.upload_id || slugify(draft.title) || slugify(src.path) || "";
    draft.bookId = guess || ("book-" + Date.now().toString(36));
    step1State.textContent = "已完成";
    step1State.className = "step-state done";
    renderSourceCard();
    renderStep2();
    renderStep3();
  }

  function renderSourceCard() {
    const src = draft.source;
    if (!src) { sourceCard.hidden = true; return; }
    sourceCard.hidden = false;
    clear(sourceCard);
    sourceCard.appendChild(el("div", { class: "source-head" },
      el("span", { class: "badge badge-ok", text: src.kind === "upload" ? "已上传落盘" : "手动路径" }),
      el("span", { class: "mono source-path", text: src.path || "(未知)" })
    ));
    sourceCard.appendChild(el("div", { class: "source-meta" },
      el("span", { text: "页数：" + (src.pages === null || src.pages === undefined ? "读不到（pdfinfo 未返回）" : fmt.num(src.pages)) }),
      el("span", { class: "dot-sep" }),
      el("span", { text: "猜测书名：" + (src.meta && src.meta.title ? src.meta.title : "（空）") }),
      el("span", { class: "dot-sep" }),
      el("span", { text: "作者：" + (src.meta && src.meta.author ? src.meta.author : "（空）") })
    ));
    sourceCard.appendChild(el("div", { class: "form-grid" },
      field("书名 title", inputText(draft.title, (v) => { draft.title = v; renderStep3(); }), "显示在成书里"),
      field("作者 author", inputText(draft.author, (v) => { draft.author = v; renderStep3(); }), ""),
      field("出版方 publisher", inputText(draft.publisher, (v) => { draft.publisher = v; renderStep3(); }), ""),
      field("book_id", bookIdInput(), "只允许小写字母、数字和连字符")
    ));
  }

  function inputText(value, onChange) {
    return el("input", {
      class: "input", type: "text", value: value || "",
      oninput: (ev) => onChange(ev.target.value)
    });
  }

  function bookIdInput() {
    const hint = el("div", { class: "field-hint" });
    const input = el("input", {
      class: "input mono", type: "text", value: draft.bookId,
      oninput: (ev) => {
        const v = ev.target.value;
        const ok = /^[a-z0-9-]+$/.test(v);
        hint.textContent = ok ? "格式合法" : "只能用 a-z 0-9 和 -";
        hint.className = "field-hint " + (ok ? "ok" : "bad");
        if (ok) { draft.bookId = v; renderStep3(); }
      }
    });
    hint.textContent = /^[a-z0-9-]+$/.test(draft.bookId) ? "格式合法" : "只能用 a-z 0-9 和 -";
    hint.className = "field-hint " + (/^[a-z0-9-]+$/.test(draft.bookId) ? "ok" : "bad");
    return el("div", null, input, hint);
  }

  function field(label, control, hint) {
    return el("label", { class: "field" },
      el("span", { class: "field-label", text: label }),
      control,
      hint ? el("span", { class: "field-hint", text: hint }) : null
    );
  }

  // ================= 步骤二 =================
  const step2State = el("span", { class: "step-state" });
  const step2Body = el("div", { class: "step-body" });
  const step2 = el("section", { class: "step", id: "step-2" },
    el("header", { class: "step-head" },
      el("span", { class: "step-no", text: "2" }),
      el("h2", { class: "step-title", text: "选预设" }),
      step2State
    ),
    step2Body
  );

  let devBadge = null;
  let resetBtn = null;

  function currentPreset() {
    for (let i = 0; i < presets.length; i++) if (presets[i].id === draft.presetId) return presets[i];
    return null;
  }

  function deviationList() {
    const preset = currentPreset();
    const base = baseParams(preset);
    const out = [];
    for (let i = 0; i < EDITABLE_KEYS.length; i++) {
      const k = EDITABLE_KEYS[i];
      const mine = draft.params[k];
      const theirs = base[k];
      const empty = mine === "" || mine === undefined || mine === null;
      if (empty) continue;
      if (String(mine) !== String(theirs === undefined ? "" : theirs)) out.push(k);
    }
    return out;
  }

  function renderStep2() {
    clear(step2Body);
    const ready = !!draft.source;
    step2.classList.toggle("disabled", !ready);
    step2State.textContent = !ready ? "待完成 · 先选定输入" : (currentPreset() ? "已完成" : "待完成");
    step2State.className = "step-state " + (!ready ? "pending" : (currentPreset() ? "done" : "pending"));

    if (!ready) {
      step2Body.appendChild(el("p", { class: "muted", text: "选定输入后，这里会列出可用的预设。" }));
      return;
    }
    if (presets.length === 0) {
      step2Body.appendChild(errorBar("后端 /api/state 没有返回任何 presets，无法选择预设。"));
      return;
    }

    const cards = el("div", { class: "preset-grid" });
    presets.forEach((p) => {
      const active = p.id === draft.presetId;
      const card = el("div", {
        class: "preset-card" + (active ? " active" : ""),
        tabindex: "0", role: "radio", "aria-checked": active ? "true" : "false",
        onclick: () => { draft.presetId = p.id; draft.params = {}; renderStep2(); renderStep3(); },
        onkeydown: (ev) => {
          if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            draft.presetId = p.id; draft.params = {}; renderStep2(); renderStep3();
          }
        }
      },
        el("div", { class: "preset-head" },
          el("span", { class: "radio-dot" + (active ? " on" : "") }),
          el("span", { class: "preset-name", text: p.name || p.id }),
          el("span", { class: "preset-id mono", text: p.id })
        ),
        el("div", { class: "preset-desc", text: p.description || "（无描述）" }),
        chipList(p.params || {})
      );
      cards.appendChild(card);
    });
    step2Body.appendChild(cards);

    // 参数微调
    const preset = currentPreset();
    const base = baseParams(preset);
    const dev = deviationList();
    const tweak = el("div", { class: "tweak-box" });
    devBadge = el("span", {
      class: "badge " + (dev.length ? "badge-warn" : "badge-ok"),
      text: dev.length ? "已偏离预设：" + dev.join("、") : "与预设一致"
    });
    tweak.appendChild(el("div", { class: "tweak-head" },
      el("h3", { class: "sub-title", text: "参数微调" }),
      devBadge
    ));

    const grid = el("div", { class: "form-grid form-grid-4" });
    grid.appendChild(field("DPI", numberInput("dpi", base.dpi), PARAM_HINTS.dpi));
    grid.appendChild(field("设备 device", selectInput("device", base.device, ["auto", "cpu", "cuda"]), PARAM_HINTS.device));
    grid.appendChild(field("模型 model", selectInput("model", base.model, ["small", "medium", "large"]), PARAM_HINTS.model));
    grid.appendChild(field("文字层 text_layer", textLayerInput(base.text_layer), PARAM_HINTS.text_layer));
    tweak.appendChild(grid);

    const presetOnly = {};
    const pk = Object.keys((preset && preset.params) || {});
    for (let i = 0; i < pk.length; i++) if (EDITABLE_KEYS.indexOf(pk[i]) < 0) presetOnly[pk[i]] = preset.params[pk[i]];
    if (pk.length) {
      tweak.appendChild(el("div", { class: "tweak-note" },
        el("span", { class: "muted", text: "预设自带、不可微调的参数：" }),
        chipList(presetOnly)
      ));
    }

    resetBtn = el("button", {
      class: "btn btn-ghost btn-sm", type: "button",
      disabled: dev.length === 0,
      onclick: () => { draft.params = {}; renderStep2(); renderStep3(); }
    }, "恢复预设默认值");
    tweak.appendChild(el("div", { class: "row-gap" }, resetBtn));
    if (preset) step2Body.appendChild(tweak);
  }

  function numberInput(key, fallback) {
    return el("input", {
      class: "input", type: "number", min: "72", max: "600", step: "1",
      value: draft.params[key] === undefined ? String(fallback === undefined ? "" : fallback) : String(draft.params[key]),
      oninput: (ev) => { setParam(key, ev.target.value); refreshDeviation(); }
    });
  }

  function selectInput(key, fallback, options) {
    const cur = draft.params[key] === undefined ? (fallback === undefined ? "" : String(fallback)) : String(draft.params[key]);
    const sel = el("select", { class: "input", onchange: (ev) => { setParam(key, ev.target.value); refreshDeviation(); } });
    options.forEach((opt) => {
      sel.appendChild(el("option", { value: opt, selected: opt === cur }, opt));
    });
    if (options.indexOf(cur) < 0 && cur !== "") {
      sel.appendChild(el("option", { value: cur, selected: true }, cur));
    }
    return sel;
  }

  function textLayerInput(fallback) {
    const cur = draft.params.text_layer === undefined ? "" : String(draft.params.text_layer);
    const sel = el("select", { class: "input", onchange: (ev) => { setParam("text_layer", ev.target.value); refreshDeviation(); } });
    const follow = "跟随预设" + (fallback === undefined ? "（未指定）" : "（" + fallback + "）");
    sel.appendChild(el("option", { value: "", selected: cur === "" }, follow));
    sel.appendChild(el("option", { value: "off", selected: cur === "off" }, "off · 全程 OCR"));
    sel.appendChild(el("option", { value: "verify", selected: cur === "verify" }, "verify · 抽文字层对账"));
    return sel;
  }

  function setParam(key, value) {
    if (value === "" || value === undefined || value === null) delete draft.params[key];
    else draft.params[key] = key === "dpi" ? Number(value) : value;
    renderStep3();
  }

  // 参数改动后只局部刷新徽标与重置按钮，避免整块重建导致输入框失焦
  function refreshDeviation() {
    const dev = deviationList();
    if (devBadge) {
      devBadge.className = "badge " + (dev.length ? "badge-warn" : "badge-ok");
      devBadge.textContent = dev.length ? "已偏离预设：" + dev.join("、") : "与预设一致";
    }
    if (resetBtn) resetBtn.disabled = dev.length === 0;
  }

  // ================= 步骤三 =================
  const step3State = el("span", { class: "step-state" });
  const step3Body = el("div", { class: "step-body" });
  const step3 = el("section", { class: "step", id: "step-3" },
    el("header", { class: "step-head" },
      el("span", { class: "step-no", text: "3" }),
      el("h2", { class: "step-title", text: "审批并启动" }),
      step3State
    ),
    step3Body
  );

  function effectiveParams() {
    const preset = currentPreset();
    const base = baseParams(preset);
    const out = { dpi: base.dpi, device: base.device, model: base.model };
    const keys = Object.keys(base);
    for (let i = 0; i < keys.length; i++) {
      if (EDITABLE_KEYS.indexOf(keys[i]) >= 0 || out[keys[i]] === undefined) out[keys[i]] = base[keys[i]];
    }
    const mine = Object.keys(draft.params);
    for (let i = 0; i < mine.length; i++) out[mine[i]] = draft.params[mine[i]];
    return out;
  }

  function stagesToRun() {
    return draft.stageMode === "all" ? ["all"] : draft.pickedStages.slice();
  }

  let startBtn = null;
  let ackBox = null;

  function renderStep3() {
    clear(step3Body);
    const hasSource = !!draft.source;
    const preset = currentPreset();
    const ready = hasSource && !!preset && /^[a-z0-9-]+$/.test(draft.bookId || "");
    step3.classList.toggle("disabled", !ready);
    step3State.className = "step-state " + (!ready ? "pending" : (draft.acked ? "done" : "todo"));
    step3State.textContent = !ready ? "待完成 · 先完成前两步" : (draft.acked ? "已核对，可启动" : "待核对");

    if (!ready) {
      step3Body.appendChild(el("p", { class: "muted", text: "前两步完成后，这里会列出即将执行的全部内容供你签字确认。" }));
      return;
    }

    const eff = effectiveParams();
    const stages = stagesToRun();
    const stageText = stages.length === 1 && stages[0] === "all"
      ? "全部 9 个阶段（render → textlayer → ocr → repair → layout → md → verify → audit → epub）"
      : stages.join(" → ");

    const rows = [
      ["PDF 路径", draft.source.path],
      ["book_id", draft.bookId],
      ["书名", draft.title || "（空）"],
      ["作者", draft.author || "（空）"],
      ["出版方", draft.publisher || "（空）"],
      ["预设", (preset.name || preset.id) + "（" + preset.id + "）"],
      ["将跑阶段", stageText]
    ];

    const sheet = el("div", { class: "approval-sheet" });
    sheet.appendChild(el("div", { class: "approval-head" },
      el("span", { class: "approval-icon", text: "▣" }),
      el("div", null,
        el("div", { class: "approval-title", text: "即将执行的内容（请逐项核对）" }),
        el("div", { class: "approval-sub", text: "启动后后端按这份参数跑流水线，参数错误只能重跑，务必核对。" })
      )
    ));

    const table = el("table", { class: "kv-table" });
    rows.forEach(([k, v]) => {
      table.appendChild(el("tr", null,
        el("th", { text: k }),
        el("td", { class: "mono", text: v === null || v === undefined || v === "" ? "（空）" : String(v) })
      ));
    });
    sheet.appendChild(table);

    sheet.appendChild(el("div", { class: "approval-sub-title", text: "最终生效参数（预设与微调合并后）" }));
    const paramTable = el("table", { class: "kv-table kv-table-params" });
    const keys = Object.keys(eff).sort();
    keys.forEach((k) => {
      paramTable.appendChild(el("tr", null,
        el("th", { text: FIELD_LABELS[k] || k }),
        el("td", { class: "mono", text: String(eff[k]) })
      ));
    });
    sheet.appendChild(paramTable);

    const pages = draft.source.pages;
    const estimate = el("div", { class: "estimate" });
    if (pages === null || pages === undefined) {
      estimate.appendChild(el("span", { class: "muted", text: "预估：页数未知（pdfinfo 未返回），无法估算规模与用时。" }));
    } else {
      const n = Number(pages);
      const ocrSec = Math.round(n * 1.5);
      const renderSec = Math.round(n * 0.3);
      const totalMin = Math.max(1, Math.round((ocrSec + renderSec) / 60));
      estimate.appendChild(el("div", { class: "estimate-row" },
        el("span", { class: "estimate-k", text: "页数" }),
        el("span", { class: "estimate-v mono", text: fmt.num(n) + " 页" })
      ));
      estimate.appendChild(el("div", { class: "estimate-row" },
        el("span", { class: "estimate-k", text: "粗估用时" }),
        el("span", { class: "estimate-v mono", text: "约 " + totalMin + " 分钟" })
      ));
      estimate.appendChild(el("div", { class: "estimate-note muted", text: "按每页渲染 0.3 秒 + OCR 1.5 秒的经验值粗估，仅供参考，实际以后端日志为准。" }));
    }
    sheet.appendChild(estimate);

    // 阶段选择
    const stageBox = el("div", { class: "stage-picker" });
    stageBox.appendChild(el("div", { class: "approval-sub-title", text: "跑哪些阶段" }));
    const radioAll = el("input", {
      type: "radio", name: "stage-mode", checked: draft.stageMode === "all",
      onchange: () => { draft.stageMode = "all"; renderStep3(); }
    });
    const radioPart = el("input", {
      type: "radio", name: "stage-mode", checked: draft.stageMode === "part",
      onchange: () => { draft.stageMode = "part"; renderStep3(); }
    });
    stageBox.appendChild(el("label", { class: "radio-row" }, radioAll, el("span", { text: "全流程（stages: [\"all\"]）" })));
    stageBox.appendChild(el("label", { class: "radio-row" }, radioPart, el("span", { text: "只跑选定阶段" })));

    if (draft.stageMode === "part") {
      const pick = el("div", { class: "stage-pick-grid" });
      const names = ["render", "textlayer", "ocr", "repair", "layout", "md", "verify", "audit", "epub"];
      names.forEach((nm) => {
        const on = draft.pickedStages.indexOf(nm) >= 0;
        pick.appendChild(el("label", { class: "stage-pick" },
          el("input", {
            type: "checkbox", checked: on,
            onchange: (ev) => {
              const idx = draft.pickedStages.indexOf(nm);
              if (ev.target.checked && idx < 0) draft.pickedStages.push(nm);
              if (!ev.target.checked && idx >= 0) draft.pickedStages.splice(idx, 1);
              draft.pickedStages.sort((a, b) => names.indexOf(a) - names.indexOf(b));
              renderStep3();
            }
          }),
          el("span", { text: nm })
        ));
      });
      stageBox.appendChild(pick);
      if (draft.pickedStages.length === 0) {
        stageBox.appendChild(el("div", { class: "inline-error", text: "至少选一个阶段。" }));
      }
    }
    sheet.appendChild(stageBox);

    // 签字
    ackBox = el("input", {
      type: "checkbox", checked: draft.acked,
      onchange: (ev) => { draft.acked = ev.target.checked; updateStartState(); step3State.textContent = draft.acked ? "已核对，可启动" : "待核对"; }
    });
    sheet.appendChild(el("label", { class: "ack-row" }, ackBox,
      el("span", { text: "我已核对以上参数（路径、book_id、阶段、生效参数），确认无误。" })));

    const launchError = el("div", { class: "inline-error", hidden: true });
    startBtn = el("button", {
      class: "btn btn-primary btn-lg", type: "button",
      onclick: () => launch(launchError)
    }, "启动流水线");
    sheet.appendChild(el("div", { class: "row-gap" }, startBtn, launchError));

    step3Body.appendChild(sheet);
    updateStartState();
  }

  function updateStartState() {
    if (!startBtn) return;
    const stagesOk = draft.stageMode === "all" || draft.pickedStages.length > 0;
    startBtn.disabled = !draft.acked || !stagesOk;
    startBtn.title = draft.acked ? "" : "必须先勾选「我已核对以上参数」";
  }

  let launching = false;
  async function launch(errBox) {
    if (launching) return;
    if (!draft.acked) { toast("请先勾选确认框", "error"); return; }
    launching = true;
    startBtn.disabled = true;
    startBtn.textContent = "启动中…";
    errBox.hidden = true;

    const payload = {
      pdf_path: draft.source.path,
      book_id: draft.bookId,
      preset: draft.presetId,
      title: draft.title || "",
      author: draft.author || "",
      publisher: draft.publisher || "",
      stages: stagesToRun(),
      params: effectiveParams()
    };

    try {
      const res = await createRun(payload);
      const runId = res.run_id || draft.bookId;
      const jobId = res.job_id || "";
      toast("已启动：" + runId + (jobId ? "（" + jobId + "）" : ""), "ok");
      const q = jobId ? "?job=" + encodeURIComponent(jobId) : "";
      location.hash = "#/run/" + encodeURIComponent(runId) + q;
    } catch (err) {
      let msg = describeError(err);
      // 409 是契约里「有 job 正在跑 / 目录已存在」，给一句可操作的提示
      if (err && err.status === 409) {
        msg += "（该 book_id 已有任务在跑或目录已存在，换个 id 或在运行列表里打开原任务）";
      }
      errBox.hidden = false;
      clear(errBox).appendChild(el("span", { text: msg }));
      toast(msg, "error", 8000);
      launching = false;
      startBtn.disabled = false;
      startBtn.textContent = "启动流水线";
    }
  }

  // ---- 装配 ----
  step1State.textContent = "待完成";
  step1State.className = "step-state pending";
  renderStep2();
  renderStep3();

  root.appendChild(step1);
  root.appendChild(step2);
  root.appendChild(step3);

  return root;
}
