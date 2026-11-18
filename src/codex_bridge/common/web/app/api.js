// Helpers shared by the page: JSON calls, the change feed, the one-line error bar, and small formatters.

export async function get(path) {
  return call("GET", path);
}

export async function send(method, path, body) {
  return call(method, path, body);
}

async function call(method, path, body) {
  const init = { method, headers: {} };
  if (method !== "GET") init.headers["X-Codex-Bridge"] = "1";
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(path, init);
  } catch (e) {
    throw new Error("つながりません");
  }
  let data = null;
  try {
    data = await r.json();
  } catch (e) {
    data = null;
  }
  if (!r.ok) {
    const error = new Error(describe(data) || `エラーです（${r.status}）`);
    error.status = r.status;
    throw error;
  }
  return data;
}

function describe(data) {
  if (!data || data.detail === undefined) return "";
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) return data.detail.map((d) => d.msg || "").filter(Boolean).join(" ");
  return "";
}

// ---- the error bar: one line per failing source under the topbar; a source's error goes on that source's next success ----
const errors = new Map();

function drawErrors() {
  const frame = document.querySelector(".frame");
  if (!frame) return;
  let bar = frame.querySelector(":scope > .note-warn");
  if (!errors.size) {
    if (bar) bar.remove();
    frame.classList.remove("has-note");
    return;
  }
  if (!bar) {
    bar = document.createElement("p");
    bar.className = "note-warn";
    bar.setAttribute("role", "alert");
    frame.querySelector(":scope > .topbar").after(bar);
    frame.classList.add("has-note");
  }
  const text = [...errors.values()].join(" / ");
  bar.innerHTML = `<svg class="ic"><use href="/app/static/icons.svg#i-triangle-alert"/></svg><span>${esc(text)}</span>`;
}

export function showError(text, key = "general") {
  errors.set(key, text);
  drawErrors();
}

export function clearError(key = "general") {
  if (errors.delete(key)) drawErrors();
}

// ---- the change feed: the borrower's event stream when it runs, and a 3 s timer in any case ----
const listeners = new Set();
let started = false;
let source = null;
let pending = null;

function fire() {
  if (pending) return;
  pending = setTimeout(() => {
    pending = null;
    listeners.forEach((fn) => {
      try {
        fn();
      } catch (e) {
        /* one listener must not stop the others */
      }
    });
  }, 150);
}

function openStream() {
  try {
    source = new EventSource("/admin/api/events");
  } catch (e) {
    return;
  }
  source.onmessage = fire;
  source.onerror = () => {
    // 503 while the use side is not running closes the stream for good: try again later
    if (source && source.readyState === EventSource.CLOSED) {
      source = null;
      setTimeout(openStream, 10000);
    }
  };
}

export function onChange(fn) {
  listeners.add(fn);
  if (!started) {
    started = true;
    openStream();
    setInterval(fire, 3000);
  }
  return () => listeners.delete(fn);
}

// ---- formatters (epoch seconds) ----
export function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const two = (n) => String(n).padStart(2, "0");

export function fmtTime(epoch) {
  const d = new Date(epoch * 1000);
  return `${two(d.getHours())}:${two(d.getMinutes())}`;
}

export function fmtDay(epoch) {
  const d = new Date(epoch * 1000);
  return `${two(d.getMonth() + 1)}/${two(d.getDate())} ${fmtTime(epoch)}`;
}

export function ago(epoch) {
  const s = Math.max(0, Date.now() / 1000 - epoch);
  if (s < 60) return "たった今";
  if (s < 3600) return `${Math.floor(s / 60)} 分前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 時間前`;
  return `${Math.floor(s / 86400)} 日前`;
}
