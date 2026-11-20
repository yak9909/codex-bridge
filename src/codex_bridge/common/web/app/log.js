// The log tab: side and level filters, search, follow-the-newest, export and open-folder.
import { get, send, showError, clearError, esc } from "/app/static/api.js";

const PHASES = {
  idle: "まだ確かめていません",
  checking: "調べています",
  current: "新しい版はありません",
  available: "新しい版があります",
  preparing: "用意しています",
  waiting: "切り替え待ち",
  failed: "アップデートに失敗しました",
};

const SIDES = { use: "使う", lend: "貸す" };
const LEVELS = { info: ["情報", ""], warn: ["警告", " warn"], error: ["失敗", " err"] };
const two = (n) => String(n).padStart(2, "0");

function stamp(epoch) {
  const d = new Date(epoch * 1000);
  return `${two(d.getMonth() + 1)}/${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
}

function line(e) {
  const [label, cls] = LEVELS[e.level] || ["?", ""];
  return `<div class="ln${cls}"><span>${stamp(e.t)}</span><span>${esc(SIDES[e.side] || "?")}</span><span>${label}</span><span>${esc(e.text)}</span></div>`;
}

export function renderLog(root) {
  root.innerHTML = `
    <div class="pane">
      <section class="update-panel" aria-labelledby="update-title" hidden>
        <div class="update-summary">
          <strong id="update-title">アップデート</strong>
          <span class="cap" id="update-version"></span>
          <span class="lz idle" id="update-phase" role="status"></span>
        </div>
        <div class="update-controls">
          <select id="update-mode" aria-label="アップデートの設定">
            <option value="confirm">毎回確認する</option>
            <option value="auto">自動で更新する</option>
          </select>
          <button class="secondary sm" id="update-check">確かめる</button>
          <button class="sm" id="update-install">更新する</button>
        </div>
        <p class="cap update-message" role="status" hidden></p>
      </section>
      <div class="log-tools">
        <span class="seg"><label><input type="radio" name="src" value="" checked>すべて</label><label><input type="radio" name="src" value="use">使う側</label><label><input type="radio" name="src" value="lend">貸す側</label></span>
        <span class="seg"><label><input type="radio" name="lv" value="" checked>すべて</label><label><input type="radio" name="lv" value="warn">警告以上</label></span>
        <input class="sm" type="search" id="log-q" placeholder="検索" style="flex:1;min-width:10em;max-width:24em">
        <label class="check cap"><input type="checkbox" id="log-follow" checked>最新に追従</label>
        <span class="spacer"></span>
        <button class="secondary sm" id="log-export">書き出す</button>
        <button class="secondary sm" id="log-folder"><svg class="ic"><use href="/app/static/icons.svg#i-folder"/></svg>フォルダを開く</button>
      </div>
      <div class="log-view" role="log"></div>
    </div>`;
  const panel = root.querySelector(".update-panel");
  const mode = panel.querySelector("#update-mode");
  const check = panel.querySelector("#update-check");
  const install = panel.querySelector("#update-install");
  const version = panel.querySelector("#update-version");
  const phase = panel.querySelector("#update-phase");
  const message = panel.querySelector(".update-message");
  const view = root.querySelector(".log-view");
  const follow = root.querySelector("#log-follow");
  const q = root.querySelector("#log-q");
  let shown = null;
  let busy = false;
  let alive = true;

  let updateRequest = 0;
  let changingUpdate = false;
  let savedMode = "confirm";

  function showUpdate(update) {
    if (!alive || !update) return;
    panel.hidden = false;
    const label = PHASES[update.phase] || PHASES.idle;
    const tone = update.phase === "failed" ? "bad" : update.phase === "waiting" ? "warn" : "idle";
    version.innerHTML = esc(`この PC v${update.version}${update.latest ? `・新しい版 v${update.latest}` : ""}`);
    if (phase.textContent !== label) phase.innerHTML = esc(label);
    phase.className = `lz ${tone}`;
    const detail = update.message || (update.latest && !update.can_install ? "exe 版で更新できます" : "");
    if (message.textContent !== detail) message.innerHTML = esc(detail);
    message.hidden = !detail;
    savedMode = update.mode;
    mode.value = savedMode;
    check.disabled = ["checking", "preparing", "waiting"].includes(update.phase);
    install.disabled = !update.can_install || !update.latest || !["available", "failed"].includes(update.phase);
  }

  async function refreshUpdate() {
    if (changingUpdate) return;
    const request = ++updateRequest;
    try {
      const state = await get("/app/api/state");
      if (request === updateRequest) showUpdate(state.update);
    } catch {
      // The app may be restarting. Keep the last displayed state until it reconnects.
    }
  }

  async function changeUpdate(action, body) {
    if (changingUpdate) return;
    changingUpdate = true;
    ++updateRequest;
    try {
      showUpdate(await send("POST", `/app/api/update/${action}`, body));
      clearError("update");
    } catch (e) {
      mode.value = savedMode;
      if (alive) showError(e.message, "update");
    } finally {
      changingUpdate = false;
    }
  }

  mode.addEventListener("change", () => changeUpdate("mode", { mode: mode.value }));
  check.addEventListener("click", () => changeUpdate("check"));
  install.addEventListener("click", () => changeUpdate("install"));

  const choice = (name) => root.querySelector(`input[name="${name}"]:checked`).value;

  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const params = new URLSearchParams({ limit: "1000" });
      if (choice("src")) params.set("side", choice("src"));
      if (choice("lv")) params.set("level", choice("lv"));
      if (q.value.trim()) params.set("q", q.value.trim());
      const entries = await get(`/app/api/logs?${params}`);
      if (!alive) return;
      clearError("log-read");
      const html = entries.map(line).join("");
      if (html !== shown) {
        shown = html;
        view.innerHTML = html;
        if (follow.checked) view.scrollTop = view.scrollHeight;
      }
    } catch (e) {
      if (alive) showError(`ログを読めませんでした: ${e.message}`, "log-read");
    } finally {
      busy = false;
    }
  }

  root.querySelectorAll('input[type="radio"]').forEach((input) => input.addEventListener("change", refresh));
  let timer = null;
  q.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(refresh, 250);
  });
  follow.addEventListener("change", () => {
    if (follow.checked) view.scrollTop = view.scrollHeight;
  });
  root.querySelector("#log-export").addEventListener("click", () => {
    const a = document.createElement("a");
    a.href = "/app/api/logs/export";
    a.download = "codex-bridge-log.txt";
    root.append(a);
    a.click();
    a.remove();
  });
  root.querySelector("#log-folder").addEventListener("click", async () => {
    try {
      await send("POST", "/app/api/logs/open-folder");
      clearError("log-folder");
    } catch (e) {
      showError(e.message, "log-folder");
    }
  });

  refresh();
  refreshUpdate();
  const interval = setInterval(() => { refresh(); refreshUpdate(); }, 2000);
  return () => {
    alive = false;
    clearInterval(interval);
    clearTimeout(timer);
    clearError("log-read");
    clearError("log-folder");
    clearError("update");
  };
}
