// 新建任务页（#/new）—— 三步向导：来源 / 书目与方案 / 确认。
// 契约见 webui/API.md（POST /api/upload、POST /api/runs）；设计见 docs/UI-DESIGN.md §4.2。
// 第三步是确认闸门：把方案与高级参数合并后的**最终生效参数**逐项摊开给人核对。
import { el, clear, fmt, toast, errorBar } from "./ui.js";
import { uploadPdf, createRun, describeError } from "./api.js";

const STEP_TITLES = ["来源", "书目与方案", "确认"];

const STAGE_ALL_TEXT = "全部 9 个阶段：渲染 → 文字层 → OCR → 修复 → 版面 → Markdown → 核对 → 审核 → 成品";

const PARAM_LABELS = {
  dpi: "渲染 DPI",
  device: "OCR 设备",
  model: "OCR 模型",
  limit_pages: "只渲染前 N 页",
  text_layer: "文字层策略",
  conservative_fix: "保守修改",
  no_layout: "跳过版面还原",
  procs: "并行进程数",
  toc_depth: "目录层级",
  force: "强制重跑"
};

const PARAM_DESC = {
  dpi: "页面渲染分辨率。扫描本 300 足够，低质扫描可降到 200 换速度。",
  device: "OCR 推理用的计算设备。auto 由运行时自己挑。",
  model: "OCR 模型档位。small 快，medium / server 更准但更慢。",
  limit_pages: "只处理前若干页，用来打样。留空表示整本。",
  text_layer: "off = 完全不信任 PDF 文字层，全程 OCR；verify = 抽文字层与 OCR 对账。",
  conservative_fix: "开启时形近字只出候选，不自动改写正文。",
  no_layout: "开启会跳过标题、段落、插图的版面还原，产出的是纯文本流。",
  procs: "并行处理的进程数，影响 CPU 占用与总用时。",
  toc_depth: "生成目录时保留到第几级标题。",
  force: "忽略已有的中间产物，全部重跑。"
};

// 参数在确认表格里的固定顺序：先界面可改的，再方案自带的
const PARAM_ORDER = ["dpi", "device", "model", "limit_pages", "text_layer",
                     "conservative_fix", "no_layout", "procs", "toc_depth", "force"];

function valueText(key, value) {
  if (value === null || value === undefined || value === "") return "（未设置）";
  if (typeof value === "boolean") return value ? "开启" : "关闭";
  if (key === "limit_pages") return String(value) + " 页";
  return String(value);
}

// book_id：转小写 → 非字母数字换成 - → 去掉首尾 - → 最多 40 字符。
// 后端白名单是 [a-z0-9-]+（见 webui/API.md §2 POST /api/runs），所以结果只可能含这三类字符。
function slugifyBookId(title) {
  return String(title || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40)
    .replace(/-+$/g, "");
}

function randomBookId() {
  let rand = "";
  const abc = "abcdefghijklmnopqrstuvwxyz0123456789";
  for (let i = 0; i < 6; i++) rand += abc.charAt(Math.floor(Math.random() * abc.length));
  return "book-" + rand;
}

function field(labelText, control, hintText) {
  return el("label", { class: "field" },
    el("span", { class: "field-label", text: labelText }),
    control,
    hintText ? el("span", { class: "field-hint", text: hintText }) : null
  );
}

export function mount(ctx) {
  const state0 = ctx.store.state || {};
  const presets = Array.isArray(state0.presets) ? state0.presets : [];
  const root = el("div", { class: "view view-new" });

  const draft = {
    step: 1,
    maxStep: 1,
    source: null,          // { upload_id, path, pages, meta, file_name }
    uploading: false,
    title: "",
    author: "",
    publisher: "",
    presetId: presets.length ? presets[0].id : "",
    adv: { dpi: "300", device: "auto", model: "small", limitPages: "" },
    // 书名里没有可用字母数字（纯中文）时的兜底标识。
    // 必须在本视图内保持不变，否则每敲一个字标识就变一次，确认页没法核对。
    fallbackBookId: randomBookId()
  };

  // 方案里如果写死了 limit_pages，就把它摆进高级参数，免得「留空=全部」变成暗改。
  // 但用户一旦手改过这一项，就不再拿方案的值覆盖回去。
  let limitTouched = false;
  function syncLimitFromPreset() {
    if (limitTouched) return;
    const p = presets.filter((x) => x.id === draft.presetId)[0];
    const lp = p && p.params ? p.params.limit_pages : undefined;
    draft.adv.limitPages = lp === undefined || lp === null ? "" : String(lp);
  }
  syncLimitFromPreset();

  function currentPreset() {
    for (let i = 0; i < presets.length; i++) if (presets[i].id === draft.presetId) return presets[i];
    return null;
  }

  // preset.params 与高级参数合并 —— 这就是确认表里展示、也是发给后端的 params
  let dpiAdjusted = false;

  function effectiveParams() {
    const out = {};
    const p = currentPreset();
    const base = (p && p.params) || {};
    const ok = Object.keys(base);
    for (let i = 0; i < ok.length; i++) out[ok[i]] = base[ok[i]];

    const dpiRaw = String(draft.adv.dpi === undefined || draft.adv.dpi === null ? "" : draft.adv.dpi).trim();
    const dpiOk = /^[0-9]+$/.test(dpiRaw) && Number(dpiRaw) >= 72 && Number(dpiRaw) <= 600;
    dpiAdjusted = dpiRaw !== "" && !dpiOk;
    out.dpi = dpiOk ? Number(dpiRaw) : 300;
    out.device = draft.adv.device || "auto";
    out.model = draft.adv.model || "small";

    const limRaw = String(draft.adv.limitPages === undefined || draft.adv.limitPages === null ? "" : draft.adv.limitPages).trim();
    if (limRaw === "") delete out.limit_pages;
    else if (/^[0-9]+$/.test(limRaw) && Number(limRaw) > 0) out.limit_pages = Number(limRaw);

    return out;
  }

  function orderedKeys(params) {
    const keys = Object.keys(params);
    const head = PARAM_ORDER.filter((k) => keys.indexOf(k) >= 0);
    const rest = keys.filter((k) => PARAM_ORDER.indexOf(k) < 0).sort();
    return head.concat(rest);
  }

  // ================= 页头 =================
  root.appendChild(el("div", { class: "page-head" },
    el("div", null,
      el("h1", { class: "page-title", text: "新建任务" }),
      el("p", { class: "page-sub", text: "上传 PDF → 填书目、选方案 → 核对最终参数后开始处理。" })
    ),
    el("div", { class: "page-head-actions" },
      el("a", { class: "btn", href: "#/tasks" }, "返回任务列表")
    )
  ));

  // ================= 步骤指示器 =================
  const stepperItems = [];
  const stepper = el("ol", { class: "nw-steps" });
  STEP_TITLES.forEach((label, i) => {
    const no = i + 1;
    const item = el("li", { class: "nw-step", dataset: { step: String(no) } },
      el("span", { class: "nw-step-no", text: String(no) }),
      el("span", { class: "nw-step-label", text: label })
    );
    item.addEventListener("click", () => {
      if (no <= draft.maxStep && no !== draft.step) goStep(no);
    });
    stepperItems.push(item);
    stepper.appendChild(item);
    if (i < STEP_TITLES.length - 1) stepper.appendChild(el("li", { class: "nw-step-sep", "aria-hidden": "true" }, "›"));
  });
  root.appendChild(stepper);

  // ================= 步骤 1：来源 =================
  // tabindex=-1 让每一步的面板本身可被聚焦：切换步骤后把焦点放在面板顶部，
  // 而不是聚焦页面底部的「开始处理」——那会把页面滚到底，刚进这一步就看不到标题
  const panel1 = el("section", { class: "card nw-panel", tabindex: "-1", dataset: { step: "1" } });
  const panel2 = el("section", { class: "card nw-panel", tabindex: "-1", dataset: { step: "2" }, hidden: true });
  const panel3 = el("section", { class: "card nw-panel", tabindex: "-1", dataset: { step: "3" }, hidden: true });

  // --- 1.1 上传区 ---
  const fileInput = el("input", {
    type: "file", accept: ".pdf,application/pdf", class: "nw-file-input", hidden: true,
    onchange: (ev) => {
      const f = ev.target.files && ev.target.files[0];
      if (f) doUpload(f);
      ev.target.value = "";
    }
  });

  const dropTitle = el("div", { class: "nw-drop-title", text: "把 PDF 拖到这里" });
  const dropDesc = el("div", { class: "nw-drop-desc", text: "或者点击选择文件。只接受 .pdf，上传后会落到工作目录的 input/ 下。" });

  const progressBox = el("div", { class: "nw-progress", hidden: true },
    el("div", { class: "progress" },
      el("div", { class: "progress-fill", style: { width: "0%" } })
    ),
    el("div", { class: "nw-progress-text", text: "上传中… 0%" })
  );

  const dropZone = el("div", { class: "nw-drop", tabindex: "0", role: "button", "aria-label": "选择或拖入 PDF 文件" },
    el("div", { class: "nw-drop-icon", text: "⇪" }),
    dropTitle,
    dropDesc,
    el("button", { class: "btn btn-primary", type: "button", onclick: () => fileInput.click() }, "选择 PDF 文件"),
    fileInput,
    progressBox
  );
  dropZone.addEventListener("click", (ev) => {
    if (ev.target && ev.target.closest && ev.target.closest("button")) return;
    fileInput.click();
  });
  dropZone.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); fileInput.click(); }
  });
  dropZone.addEventListener("dragover", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    dropZone.classList.add("is-drag");
  });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("is-drag"));
  dropZone.addEventListener("drop", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    dropZone.classList.remove("is-drag");
    const f = ev.dataTransfer && ev.dataTransfer.files ? ev.dataTransfer.files[0] : null;
    if (!f) return;
    if (!/\.pdf$/i.test(f.name) && f.type !== "application/pdf") {
      toast("只接受 .pdf 文件", "error");
      return;
    }
    doUpload(f);
  });

  // --- 1.2 已上传结果 ---
  const sourceCard = el("div", { class: "nw-source", hidden: true });

  const step1Error = el("div", { class: "nw-inline-error", hidden: true });

  const next1 = el("button", { class: "btn btn-primary", type: "button", onclick: () => { if (validateStep1()) goStep(2); } }, "下一步");

  panel1.appendChild(el("div", { class: "card-head" },
    el("h2", { class: "card-title", text: "1 · 来源" }),
    el("span", { class: "card-sub", text: "上传本次要处理的 PDF" })
  ));
  panel1.appendChild(el("div", { class: "card-body" }, dropZone, sourceCard, step1Error));
  panel1.appendChild(el("div", { class: "nw-foot" },
    el("span", { class: "spacer" }),
    next1
  ));

  const pctText = progressBox.querySelector(".nw-progress-text");
  const pctFill = progressBox.querySelector(".progress-fill");

  async function doUpload(file) {
    if (draft.uploading) return;
    draft.uploading = true;
    step1Error.hidden = true;
    progressBox.hidden = false;
    pctFill.style.width = "0%";
    pctText.textContent = "上传中… 0%";
    dropZone.classList.add("is-busy");
    dropTitle.textContent = "正在上传 " + file.name;
    dropDesc.textContent = "文件正在写入工作目录，请不要关闭页面。";
    try {
      const res = await uploadPdf(file, (ratio) => {
        const pct = Math.max(0, Math.min(100, Math.round(ratio * 100)));
        pctFill.style.width = pct + "%";
        pctText.textContent = "上传中… " + pct + "%";
      });
      if (!ctx.alive()) return;
      draft.source = {
        upload_id: res.upload_id || "",
        path: res.path || "",
        pages: res.pages === undefined ? null : res.pages,
        meta: res.meta || {},
        file_name: file.name
      };
      const metaTitle = draft.source.meta && draft.source.meta.title ? String(draft.source.meta.title) : "";
      if (metaTitle && !draft.title.trim()) {
        draft.title = metaTitle;
        titleInput.value = metaTitle;
      }
      toast("上传完成：" + (res.path || file.name), "success");
      renderSource();
      renderStep2Gates();
    } catch (err) {
      if (!ctx.alive()) return;
      const msg = describeError(err);
      toast(msg, "error", 8000);
      clear(step1Error).appendChild(errorBar(msg));
      step1Error.hidden = false;
    } finally {
      draft.uploading = false;
      progressBox.hidden = true;
      dropZone.classList.remove("is-busy");
      dropTitle.textContent = "把 PDF 拖到这里";
      dropDesc.textContent = "或者点击选择文件。只接受 .pdf，上传后会落到工作目录的 input/ 下。";
      if (ctx.alive()) { renderSource(); renderStep2Gates(); }
    }
  }

  function renderSource() {
    const s = draft.source;
    if (!s) { sourceCard.hidden = true; return; }
    sourceCard.hidden = false;
    clear(sourceCard);
    const meta = s.meta || {};
    sourceCard.appendChild(el("div", { class: "nw-source-head" },
      el("span", { class: "badge badge-ok", text: "已上传" }),
      el("span", { class: "nw-source-name", text: s.file_name || "source.pdf", title: s.file_name || "" }),
      el("span", { class: "spacer" }),
      el("button", {
        class: "btn btn-sm btn-ghost", type: "button",
        onclick: () => { draft.source = null; renderSource(); renderStep2Gates(); fileInput.click(); }
      }, "换一个文件")
    ));
    const rows = [
      ["落盘路径", s.path || "（空）"],
      ["页数", s.pages === null || s.pages === undefined ? "读不到（pdfinfo 未返回）" : fmt.num(s.pages) + " 页"],
      ["任务标识", s.upload_id || "（空）"],
      ["PDF 内书名", meta.title || "（空）"],
      ["PDF 内作者", meta.author || "（空）"]
    ];
    const dl = el("dl", { class: "nw-kv" });
    rows.forEach((row) => {
      dl.appendChild(el("dt", { text: row[0] }));
      dl.appendChild(el("dd", { class: "mono", text: row[1] }));
    });
    sourceCard.appendChild(dl);
  }

  function validateStep1() {
    if (draft.uploading) { toast("文件还在上传，请稍候", "warn"); return false; }
    if (!draft.source) {
      clear(step1Error).appendChild(errorBar("还没有可用的 PDF。请先上传一个文件再进入下一步。"));
      step1Error.hidden = false;
      return false;
    }
    step1Error.hidden = true;
    return true;
  }

  // ================= 步骤 2：书目与方案 =================
  const titleInput = el("input", {
    class: "input", type: "text", placeholder: "例如：拉康式主体：在语言与享乐之间",
    oninput: (ev) => { draft.title = ev.target.value; refreshBookIdHint(); }
  });
  const authorInput = el("input", {
    class: "input", type: "text", placeholder: "可留空",
    oninput: (ev) => { draft.author = ev.target.value; }
  });
  const publisherInput = el("input", {
    class: "input", type: "text", placeholder: "可留空",
    oninput: (ev) => { draft.publisher = ev.target.value; }
  });
  const bookIdHint = el("span", { class: "field-hint" });

  function bookIdOf(title) {
    return slugifyBookId(title) || draft.fallbackBookId;
  }

  function refreshBookIdHint() {
    clear(bookIdHint);
    bookIdHint.appendChild(el("span", { text: "将作为成品的书名。任务标识：" }));
    bookIdHint.appendChild(el("span", { class: "mono tk-id", text: bookIdOf(draft.title) }));
  }
  refreshBookIdHint();

  const presetGrid = el("div", { class: "nw-presets" });
  const presetError = el("div", { class: "nw-inline-error", hidden: true });

  function renderPresets() {
    clear(presetGrid);
    if (!presets.length) {
      clear(presetError).appendChild(errorBar("后端 /api/state 没有返回任何方案（presets），无法新建任务。"));
      presetError.hidden = false;
      return;
    }
    presetError.hidden = true;
    presets.forEach((p) => {
      const active = p.id === draft.presetId;
      const params = p.params || {};
      const keys = Object.keys(params);
      const paramBox = el("div", { class: "nw-preset-params" });
      if (!keys.length) {
        paramBox.appendChild(el("span", { class: "faint xs", text: "（方案未附带参数）" }));
      } else {
        keys.forEach((k) => {
          paramBox.appendChild(el("span", { class: "nw-preset-param" },
            el("span", { class: "nw-preset-key", text: k }),
            el("span", { class: "nw-preset-val", text: valueText(k, params[k]) })
          ));
        });
      }
      const card = el("div", {
        class: "nw-preset" + (active ? " is-active" : ""),
        role: "radio", tabindex: "0",
        "aria-checked": active ? "true" : "false",
        onclick: () => { choosePreset(p.id); },
        onkeydown: (ev) => {
          if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); choosePreset(p.id); }
        }
      },
        el("div", { class: "nw-preset-head" },
          el("span", { class: "nw-radio" + (active ? " is-on" : "") }),
          el("span", { class: "nw-preset-name", text: p.name || p.id }),
          el("span", { class: "nw-preset-id mono", text: p.id })
        ),
        el("div", { class: "nw-preset-desc", text: p.description || "（无说明）" }),
        paramBox
      );
      presetGrid.appendChild(card);
    });
  }

  function choosePreset(id) {
    draft.presetId = id;
    syncLimitFromPreset();
    limitInput.value = draft.adv.limitPages;
    renderPresets();
  }

  // --- 高级参数（可折叠） ---
  const dpiInput = el("input", {
    class: "input", type: "number", min: "72", max: "600", step: "1", value: draft.adv.dpi,
    oninput: (ev) => {
      draft.adv.dpi = ev.target.value;
      dpiInput.classList.toggle("input-invalid", !/^[0-9]+$/.test(String(ev.target.value).trim()));
    }
  });
  const deviceSelect = el("select", { class: "select", onchange: (ev) => { draft.adv.device = ev.target.value; } },
    ["auto", "cpu", "dml", "cuda"].map((v) => el("option", { value: v, selected: draft.adv.device === v }, v))
  );
  const modelSelect = el("select", { class: "select", onchange: (ev) => { draft.adv.model = ev.target.value; } },
    ["small", "medium", "server"].map((v) => el("option", { value: v, selected: draft.adv.model === v }, v))
  );
  const limitInput = el("input", {
    class: "input", type: "number", min: "1", step: "1", placeholder: "留空 = 整本",
    value: draft.adv.limitPages,
    oninput: (ev) => { limitTouched = true; draft.adv.limitPages = ev.target.value; }
  });

  const advBody = el("div", { class: "nw-adv-body", hidden: true },
    el("div", { class: "nw-adv-grid" },
      field("DPI", dpiInput, "渲染分辨率，默认 300"),
      field("OCR 设备", deviceSelect, "auto 由运行时选择"),
      field("OCR 模型", modelSelect, "越大越准，也越慢"),
      field("只渲染前 N 页", limitInput, "留空表示整本，打样时填几十页")
    )
  );
  const advToggle = el("button", { class: "nw-adv-toggle", type: "button", "aria-expanded": "false",
    onclick: () => {
      const open = advBody.hidden;
      advBody.hidden = !open;
      advToggle.setAttribute("aria-expanded", open ? "true" : "false");
      advToggle.classList.toggle("is-open", open);
    }
  }, "高级参数");

  const step2Error = el("div", { class: "nw-inline-error", hidden: true });
  const back2 = el("button", { class: "btn", type: "button", onclick: () => goStep(1) }, "上一步");
  const next2 = el("button", { class: "btn btn-primary", type: "button", onclick: () => { if (validateStep2()) goStep(3); } }, "下一步");

  panel2.appendChild(el("div", { class: "card-head" },
    el("h2", { class: "card-title", text: "2 · 书目与方案" }),
    el("span", { class: "card-sub", text: "书名会写进成品；方案决定处理策略" })
  ));
  panel2.appendChild(el("div", { class: "card-body" },
    el("div", { class: "nw-form" },
      el("label", { class: "field nw-field-wide" },
        el("span", { class: "field-label", text: "书名（必填）" }),
        titleInput,
        bookIdHint
      ),
      field("作者", authorInput, "可留空"),
      field("出版社", publisherInput, "可留空")
    ),
    el("div", { class: "nw-section-title", text: "方案" }),
    presetGrid,
    presetError,
    el("div", { class: "nw-adv" }, advToggle, advBody),
    step2Error
  ));
  panel2.appendChild(el("div", { class: "nw-foot" }, back2, el("span", { class: "spacer" }), next2));

  let step2Touched = false;

  // 上传完成后把 PDF 元信息里的书名填进输入框（用户已手填就不覆盖）
  function renderStep2Gates() {
    if (draft.source) {
      const meta = draft.source.meta || {};
      if (!String(draft.title || "").trim() && meta.title) {
        draft.title = String(meta.title);
        titleInput.value = draft.title;
        refreshBookIdHint();
      }
    }
    titleInput.classList.toggle("input-invalid", step2Touched && !String(draft.title || "").trim());
  }

  function validateStep2() {
    step2Touched = true;
    if (!presets.length) {
      clear(step2Error).appendChild(errorBar("没有任何可用方案，无法开始处理。"));
      step2Error.hidden = false;
      return false;
    }
    if (!String(draft.title).trim()) {
      clear(step2Error).appendChild(errorBar("书名是必填项。它决定成品的书名与 book_id。"));
      step2Error.hidden = false;
      titleInput.classList.add("input-invalid");
      titleInput.focus();
      return false;
    }
    step2Error.hidden = true;
    titleInput.classList.remove("input-invalid");
    return true;
  }

  // ================= 步骤 3：确认 =================
  const back3 = el("button", { class: "btn", type: "button", onclick: () => goStep(2) }, "上一步");
  const startBtn = el("button", { class: "btn btn-primary btn-lg", type: "button", onclick: () => submit() }, "开始处理");
  const step3Body = el("div", { class: "card-body" });
  const step3Error = el("div", { class: "nw-inline-error", hidden: true });

  panel3.appendChild(el("div", { class: "card-head" },
    el("h2", { class: "card-title", text: "3 · 确认" }),
    el("span", { class: "card-sub", text: "核对最终生效参数" })
  ));
  panel3.appendChild(step3Body);
  panel3.appendChild(el("div", { class: "nw-foot" }, back3, el("span", { class: "spacer" }), startBtn));

  function renderStep3() {
    clear(step3Body);
    const preset = currentPreset();
    const eff = effectiveParams();
    const src = draft.source;
    const rid = bookIdOf(draft.title);

    step3Body.appendChild(el("div", { class: "nw-gate" },
      el("div", { class: "nw-gate-icon", text: "▣" }),
      el("div", null,
        el("div", { class: "nw-gate-title", text: "这些是本次执行的最终参数，请核对后开始" }),
        el("div", { class: "nw-gate-sub", text: "点「开始处理」后，后端就按下面这一份参数跑完整流程。参数不对只能重跑，所以请逐项看一眼。" })
      )
    ));

    const info = el("dl", { class: "nw-kv nw-kv-info" });
    const infoRows = [
      ["书名", draft.title || "（空）"],
      ["作者", draft.author || "（空）"],
      ["出版社", draft.publisher || "（空）"],
      ["方案", preset ? (preset.name || preset.id) + "（" + preset.id + "）" : "（未选择）"],
      ["来源 PDF", src ? (src.path || "（空）") : "（未上传）"],
      ["页数", src && src.pages !== null && src.pages !== undefined ? fmt.num(src.pages) + " 页" : "未知"],
      ["任务标识", rid],
      ["将跑阶段", STAGE_ALL_TEXT]
    ];
    infoRows.forEach((row) => {
      info.appendChild(el("dt", { text: row[0] }));
      info.appendChild(el("dd", { text: row[1] }));
    });
    step3Body.appendChild(info);

    step3Body.appendChild(el("div", { class: "nw-section-title", text: "最终生效参数" }));
    const table = el("table", { class: "table nw-param-table" });
    table.appendChild(el("thead", null,
      el("tr", null,
        el("th", { text: "参数名" }),
        el("th", { text: "值" }),
        el("th", { text: "说明" })
      )
    ));
    const tbody = el("tbody");
    orderedKeys(eff).forEach((k) => {
      tbody.appendChild(el("tr", null,
        el("td", null,
          el("span", { class: "nw-param-label", text: PARAM_LABELS[k] || k }),
          el("span", { class: "nw-param-key mono", text: k })
        ),
        el("td", { class: "mono nw-param-value", text: valueText(k, eff[k]) }),
        el("td", { class: "nw-param-desc", text: PARAM_DESC[k] || "由所选方案提供，界面不单独调整。" })
      ));
    });
    table.appendChild(tbody);
    step3Body.appendChild(table);

    if (dpiAdjusted) {
      step3Body.appendChild(el("div", { class: "nw-warn" },
        el("span", { class: "nw-warn-mark", text: "!" }),
        el("span", { text: "DPI 只接受 72-600 的整数，你填的值不在范围内，将按默认 300 执行。要改请回上一步。" })
      ));
    }

    step3Body.appendChild(el("div", { class: "nw-note" },
      el("span", { class: "nw-note-dot" }),
      el("span", { text: "改动方案或高级参数后，这张表会立刻按新的合并结果更新；看到的就是会执行的。" })
    ));
    step3Body.appendChild(step3Error);
  }

  let launching = false;
  async function submit() {
    if (launching) return;
    // 校验放在这里再做一遍：确认页上的「开始处理」必须自己兜住，
    // 不能假设用户一定是按顺序点过来的
    if (!draft.source) {
      toast("还没有上传 PDF", "error");
      goStep(1);
      return;
    }
    if (!validateStep2()) {
      toast("书名是必填项", "error");
      goStep(2);
      return;
    }
    const preset = currentPreset();
    if (!preset) { toast("没有可用的方案", "error"); return; }

    const params = effectiveParams();
    const payload = {
      pdf_path: draft.source.path,
      book_id: bookIdOf(draft.title),
      preset: preset.id,
      title: String(draft.title).trim(),
      author: String(draft.author || "").trim(),
      publisher: String(draft.publisher || "").trim(),
      stages: ["all"],
      params: params
    };

    launching = true;
    startBtn.disabled = true;
    startBtn.textContent = "正在创建任务…";
    step3Error.hidden = true;

    try {
      const res = await createRun(payload);
      if (!ctx.alive()) return;
      const runId = res.run_id || payload.book_id;
      toast("任务已创建，开始处理", "success");
      const href = "#/task/" + String(runId).split("/").map(encodeURIComponent).join("/");
      ctx.navigate(href);
    } catch (err) {
      if (!ctx.alive()) return;
      let msg = describeError(err);
      if (err && err.status === 409) {
        msg += "（这个任务标识已有执行在跑，换个书名或在任务列表里打开原任务）";
      }
      toast(msg, "error", 8000);
      clear(step3Error).appendChild(errorBar(msg));
      step3Error.hidden = false;
      launching = false;
      startBtn.disabled = false;
      startBtn.textContent = "开始处理";
    }
  }

  // ================= 步骤切换 =================
  function goStep(n) {
    if (n < 1 || n > 3) return;
    if (n > draft.maxStep) draft.maxStep = n;
    draft.step = n;
    panel1.hidden = n !== 1;
    panel2.hidden = n !== 2;
    panel3.hidden = n !== 3;
    if (n === 2) { renderPresets(); }
    if (n === 3) { renderStep3(); }
    renderStepper();
    const panel = n === 1 ? panel1 : n === 2 ? panel2 : panel3;
    if (typeof panel.focus === "function") {
      try { panel.focus({ preventScroll: true }); } catch (e) { panel.focus(); }
    }
    window.scrollTo(0, 0);
  }

  function renderStepper() {
    for (let i = 0; i < stepperItems.length; i++) {
      const no = i + 1;
      const item = stepperItems[i];
      item.classList.toggle("is-current", no === draft.step);
      item.classList.toggle("is-done", no < draft.step);
      item.classList.toggle("is-reachable", no <= draft.maxStep);
      if (no === draft.step) item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
    }
  }

  root.appendChild(panel1);
  root.appendChild(panel2);
  root.appendChild(panel3);

  renderPresets();
  renderSource();
  renderStepper();

  return root;
}