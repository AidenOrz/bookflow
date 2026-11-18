// 后端 API 封装：所有 fetch 统一走这里。
// 契约见 webui/API.md，字段名一律照抄，不自己发明。

export class ApiError extends Error {
  constructor(message, status, detail, payload) {
    super(message);
    this.name = "ApiError";
    this.status = status || 0;
    this.detail = detail || "";
    this.payload = payload || null;
  }
}

const STATUS_HINT = {
  400: "参数不被接受",
  404: "对象不存在",
  409: "有作业正在运行，拒绝重复启动",
  500: "后端内部异常"
};

function humanize(status, payload) {
  const err = payload && typeof payload === "object" ? payload.error : "";
  const base = err ? String(err) : (STATUS_HINT[status] || ("请求失败（HTTP " + status + "）"));
  const detail = payload && typeof payload === "object" && payload.detail ? String(payload.detail) : "";
  return detail ? base + "：" + detail : base;
}

async function readPayload(res) {
  const ct = res.headers.get("content-type") || "";
  if (ct.indexOf("application/json") >= 0) {
    try { return await res.json(); } catch (e) { return null; }
  }
  const txt = await res.text();
  return txt ? { error: txt.slice(0, 300) } : null;
}

export async function request(path, opts) {
  const o = opts || {};
  const init = { method: o.method || "GET", headers: {} };
  if (o.body !== undefined && o.body !== null) {
    init.headers["Content-Type"] = "application/json; charset=utf-8";
    init.body = JSON.stringify(o.body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    throw new ApiError("无法连接本地服务，请确认后端仍在运行（python bookflow.py ui）", 0, String(e && e.message ? e.message : e));
  }
  if (!res.ok) {
    const payload = await readPayload(res);
    const msg = humanize(res.status, payload);
    const detail = payload && payload.detail ? String(payload.detail) : "";
    throw new ApiError(msg, res.status, detail, payload);
  }
  if (o.raw) return res;
  const payload = await readPayload(res);
  return payload === null ? {} : payload;
}

// ---- 2. 运行与预设 ----
export function getState() {
  return request("/api/state");
}

// 上传 PDF：用 XHR 才能拿到进度，仍是原生 API
export function uploadPdf(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.responseType = "text";
    xhr.upload.onprogress = (ev) => {
      if (onProgress && ev.lengthComputable) onProgress(ev.loaded / ev.total);
    };
    xhr.onload = () => {
      let payload = null;
      try { payload = xhr.responseText ? JSON.parse(xhr.responseText) : null; } catch (e) { payload = null; }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload || {});
      } else {
        reject(new ApiError(humanize(xhr.status, payload), xhr.status,
          payload && payload.detail ? payload.detail : "", payload));
      }
    };
    xhr.onerror = () => reject(new ApiError("上传失败：连接中断", 0, ""));
    xhr.onabort = () => reject(new ApiError("上传已取消", 0, ""));
    const fd = new FormData();
    fd.append("file", file);
    xhr.send(fd);
  });
}

export function createRun(payload) {
  return request("/api/runs", { method: "POST", body: payload });
}

export function rerunStage(runId, stage, params) {
  return request("/api/runs/" + encodeURIComponent(runId) + "/stages/" + encodeURIComponent(stage),
    { method: "POST", body: params || {} });
}

export function abortRun(runId) {
  return request("/api/runs/" + encodeURIComponent(runId) + "/abort", { method: "POST" });
}

// ---- 3. 作业流 ----
export function jobStatus(jobId) {
  return request("/api/jobs/" + encodeURIComponent(jobId));
}

// 返回 EventSource 句柄；handlers: {log, stage, done, job, error, open}
export function openJobStream(jobId, handlers) {
  const h = handlers || {};
  const es = new EventSource("/api/jobs/" + encodeURIComponent(jobId) + "/stream");
  const wrap = (name) => (ev) => {
    let data = null;
    try { data = ev.data ? JSON.parse(ev.data) : null; } catch (e) { data = { line: ev.data }; }
    if (typeof h[name] === "function") h[name](data, ev);
  };
  es.addEventListener("log", wrap("log"));
  es.addEventListener("stage", wrap("stage"));
  es.addEventListener("done", wrap("done"));
  es.addEventListener("job", wrap("job"));
  es.addEventListener("open", () => { if (typeof h.open === "function") h.open(es); });
  es.onerror = () => { if (typeof h.error === "function") h.error(es); };
  return es;
}

// ---- 4. 裁决 ----
export function getDecisions(runId, only) {
  return request("/api/runs/" + encodeURIComponent(runId) + "/decisions?only=" + encodeURIComponent(only || "eyes"));
}

export function postDecisions(runId, updates) {
  return request("/api/runs/" + encodeURIComponent(runId) + "/decisions",
    { method: "POST", body: { updates: updates } });
}

export function applyPreview(runId) {
  return request("/api/runs/" + encodeURIComponent(runId) + "/apply/preview", { method: "POST", body: {} });
}

export function applyRun(runId, thenStages) {
  return request("/api/runs/" + encodeURIComponent(runId) + "/apply",
    { method: "POST", body: { confirm: true, then: thenStages || ["layout", "md", "epub"] } });
}

// ---- 5. 检视 ----
export function getReport(runId) {
  return request("/api/runs/" + encodeURIComponent(runId) + "/report");
}

export async function getMarkdown(runId, path) {
  const p = path || "book.md";
  return request("/api/runs/" + encodeURIComponent(runId) + "/markdown?path=" + encodeURIComponent(p), { raw: true })
    .then((res) => {
      if (!res.ok) {
        return res.text().then((txt) => {
          let payload = null;
          try { payload = JSON.parse(txt); } catch (e) { payload = null; }
          throw new ApiError(humanize(res.status, payload), res.status, "", payload);
        });
      }
      return res.text();
    });
}

export function getManifest(runId) {
  return request("/api/runs/" + encodeURIComponent(runId) + "/manifest");
}

export function epubUrl(runId) {
  return "/api/runs/" + encodeURIComponent(runId) + "/epub";
}

export function pageUrl(runId, page, hz) {
  let url = "/api/runs/" + encodeURIComponent(runId) + "/page?page=" + encodeURIComponent(page);
  if (hz !== undefined && hz !== null) url += "&hz=" + encodeURIComponent(hz);
  return url;
}

// 把任意异常转成一句人话
export function describeError(err) {
  if (!err) return "未知错误";
  if (err instanceof ApiError) return err.message;
  if (err.message) return String(err.message);
  return String(err);
}
