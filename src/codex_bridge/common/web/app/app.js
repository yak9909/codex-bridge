// The page shell: topbar (tabs, connection text, pause) and the pane of the chosen tab.
import { get, send, onChange, showError, clearError, esc } from "/app/static/api.js";
import { renderLog } from "/app/static/log.js";

const ICONS = "/app/static/icons.svg";
const TABS = [["use", "使う"], ["lend", "貸す"], ["log", "ログ"]];

// use.js / lend.js come with later changes: until they exist the tab shows an empty pane
async function loadRenderer(name, exportName) {
  try {
    const module = await import(`/app/static/${name}.js`);
    return typeof module[exportName] === "function" ? module[exportName] : null;
  } catch (e) {
    return null;
  }
}

const RENDERERS = {
  use: async (root) => ((await loadRenderer("use", "renderUse")) || empty)(root),
  lend: async (root) => ((await loadRenderer("lend", "renderLend")) || empty)(root),
  log: (root) => renderLog(root),
};

function empty(root) {
  root.innerHTML = '<div class="pane"></div>';
}

const state = { console: false, paused: false, tab: "use", dispose: null, draw: 0 };
const frame = document.createElement("div");
frame.className = "frame";
frame.innerHTML = `
  <header class="topbar">
    <span class="brand">codex-bridge</span>
    <nav class="tabs" role="tablist"></nav>
    <span class="spacer"></span>
    <span class="conn cap"><span class="dot"></span><span class="conn-text"></span></span>
    <button class="secondary sm" id="pause"></button>
  </header>
  <div class="view"></div>`;
document.body.append(frame);
const tabsEl = frame.querySelector(".tabs");
const view = frame.querySelector(".view");
const dot = frame.querySelector(".dot");
const connText = frame.querySelector(".conn-text");
const pauseButton = frame.querySelector("#pause");

function tabFromHash() {
  const name = location.hash.replace("#", "");
  if (state.console) return "use";
  return TABS.some(([key]) => key === name) ? name : "use";
}

function buildTabs() {
  tabsEl.innerHTML = "";
  for (const [key, label] of TABS) {
    if (state.console && key !== "use") continue;
    const button = document.createElement("button");
    button.className = "tab";
    button.setAttribute("role", "tab");
    button.dataset.tab = key;
    button.innerHTML = `${esc(label)}`;
    button.addEventListener("click", () => {
      if (location.hash === `#${key}`) show();
      else location.hash = `#${key}`;
    });
    tabsEl.append(button);
  }
}

function markTabs() {
  for (const button of tabsEl.children) button.setAttribute("aria-selected", String(button.dataset.tab === state.tab));
}

function setCount(key, n) {
  const button = tabsEl.querySelector(`[data-tab="${key}"]`);
  if (!button) return;
  let badge = button.querySelector(".count");
  if (!n) {
    if (badge) badge.remove();
    return;
  }
  if (!badge) {
    badge = document.createElement("span");
    badge.className = "count";
    button.append(badge);
  }
  badge.textContent = String(n);
}

function setPause(paused) {
  state.paused = paused;
  pauseButton.innerHTML = paused
    ? `<svg class="ic"><use href="${ICONS}#i-play"/></svg>再開`
    : `<svg class="ic"><use href="${ICONS}#i-pause"/></svg>一時停止`;
}

async function show() {
  const token = ++state.draw;
  state.tab = tabFromHash();
  markTabs();
  if (typeof state.dispose === "function") state.dispose();
  state.dispose = null;
  view.replaceChildren();
  const result = await RENDERERS[state.tab](view);
  if (token !== state.draw) {
    if (typeof result === "function") result();
    return;
  }
  state.dispose = result;
}

async function tryGet(path, fallback) {
  try {
    return await get(path);
  } catch (e) {
    return fallback;
  }
}

async function refresh() {
  const lenders = await tryGet("/admin/api/lenders", []);
  const rows = Array.isArray(lenders) ? lenders : [];
  const a = rows.filter((r) => r.connected).length;
  const b = rows.length;
  let text;
  let connected = a;
  if (state.console) {
    text = `貸す人 ${a}/${b} とつながっています`;
    const overview = await tryGet("/admin/api/overview", null);
    if (overview) setPause(!!overview.paused);
    setCount("use", await pendingUse());
  } else {
    const connections = await tryGet("/lend/api/connections", []);
    const list = Array.isArray(connections) ? connections : [];
    const c = list.filter((r) => r.connected).length;
    const d = list.length;
    connected += c;
    text = `貸す人 ${a}/${b}・依頼主 ${c}/${d} とつながっています`;
    try {
      const s = await get("/app/api/state");
      setPause(!!s.paused);
      clearError("state");
    } catch (e) {
      showError("この画面の本体につながりません", "state");
    }
    setCount("use", await pendingUse());
    const pending = await tryGet("/lend/api/pending", null);
    setCount("lend", Array.isArray(pending) ? pending.length : pending ? 1 : 0);
  }
  connText.textContent = text;
  dot.className = connected ? "dot ok" : "dot";
}

async function pendingUse() {
  const list = await tryGet("/admin/api/approvals", []);
  return Array.isArray(list) ? list.length : 0;
}

pauseButton.addEventListener("click", async () => {
  try {
    const path = state.console ? "/admin/api/pause" : "/app/api/pause";
    const r = await send("POST", path, { paused: !state.paused });
    setPause(!!r.paused);
    clearError("pause");
  } catch (e) {
    showError(e.message, "pause");
  }
});

async function start() {
  try {
    await get("/app/api/state");
  } catch (e) {
    if (e.status === 404) state.console = true; // the console way has no resident app behind the page
  }
  if (state.console) frame.classList.add("console");
  buildTabs();
  setPause(false);
  window.addEventListener("hashchange", show);
  await show();
  await refresh();
  onChange(refresh);
}

start();
