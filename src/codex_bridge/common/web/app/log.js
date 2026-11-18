// The log tab: side and level filters, search, follow-the-newest, export and open-folder.
import { get, send, showError, clearError, esc } from "/app/static/api.js";

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
  const view = root.querySelector(".log-view");
  const follow = root.querySelector("#log-follow");
  const q = root.querySelector("#log-q");
  let shown = null;
  let busy = false;
  let alive = true;

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
  const interval = setInterval(refresh, 2000);
  return () => {
    alive = false;
    clearInterval(interval);
    clearTimeout(timer);
    clearError("log-read");
    clearError("log-folder");
  };
}
