// The plain pages of the use tab: shared folders, the allowlist, and the empty state before the host is set up.
import { esc } from "/app/static/api.js";
import { icon } from "/app/static/use-util.js";

const LEVELS = [["write", "書ける"], ["read", "読むだけ"], ["hidden", "見せない"]];

export function emptyHtml(error, starting = false) {
  if (starting && !error) return '<div class="pane"><div class="empty-pane"><p>使う側を起動しています…</p></div></div>';
  return `<div class="pane"><div class="empty-pane"><p>この PC で Codex を借りるには、最初に準備が要ります。</p>` +
    `${error ? `<p class="note-warn">${icon("triangle-alert")}<span>${esc(error)}</span></p>` : ""}` +
    `<button data-act="setup">準備を始める</button></div></div>`;
}

const norm = (p) => String(p).replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();

function select(value, act, path, extra = []) {
  const opts = [...LEVELS, ...extra].map(([v, t]) => `<option value="${v}"${v === value ? " selected" : ""}>${t}</option>`).join("");
  return `<select class="sm" data-act="${act}" data-path="${esc(path)}" aria-label="権限">${opts}</select>`;
}

export function foldersHtml(form, message) {
  const rules = [...(form.folders || [])].sort((a, b) => norm(a.path).localeCompare(norm(b.path)));
  const main = form.main ? norm(form.main) : null;
  const parentOf = (f) => rules.filter((g) => g !== f && norm(f.path).startsWith(norm(g.path) + "/"))
    .sort((a, b) => norm(b.path).length - norm(a.path).length)[0];
  const rows = rules.map((f) => {
    const parent = parentOf(f);
    const path = esc(f.path);
    if (parent) {
      const rel = f.path.slice(parent.path.replace(/[\\/]+$/, "").length + 1);
      return `<tr><td class="indent"><code>${esc(rel)}</code></td><td>${select(f.level, "folder-level", f.path, [["inherit", "親と同じ"]])}</td><td class="end"></td></tr>`;
    }
    const isMain = norm(f.path) === main;
    const makeMain = !isMain && f.level !== "hidden"
      ? `<button class="subtle sm" data-act="folder-main" data-path="${path}">メインにする</button>` : "";
    return `<tr><td><code>${path}</code>${isMain ? ' <span class="badge accent">メイン</span>' : ""}</td><td>${select(f.level, "folder-level", f.path)}</td>` +
      `<td class="end"><span class="row" style="justify-content:flex-end;flex-wrap:nowrap">${makeMain}<button class="subtle sm" data-act="folder-remove" data-path="${path}">${icon("trash-2")}外す</button></span></td></tr>`;
  }).join("");
  return `<p class="hint">Codex から見えるのは、ここに並べたフォルダだけです</p>` +
    `<table class="grid"><thead><tr><th>フォルダ</th><th>権限</th><th class="end"></th></tr></thead><tbody>${rows}</tbody></table>` +
    `${message ? `<p class="note-warn">${icon("triangle-alert")}<span>${esc(message)}</span></p>` : ""}` +
    `<div class="row"><button class="secondary sm" data-act="folder-add">${icon("folder-plus")}フォルダを追加</button></div>`;
}

export function allowHtml(form) {
  const ex = form.exec || {};
  return `<p class="hint">「許可リストだけ自動」の人が、確認なしで実行できるコマンド。<code>*</code> は名前の一部か任意の引数、<code>**</code> は下のフォルダすべて。<code>&amp;&amp;</code> や <code>|</code> を含むものは承認待ち。</p>` +
    `<textarea rows="4" spellcheck="false" data-act="allow-text" style="font-family:var(--mono)">${esc((ex.allow || []).join("\n"))}</textarea>` +
    `<label class="switch"><input type="checkbox" data-act="allow-confirm"${ex.confirm_modified_scripts ? " checked" : ""}><span class="track"></span><span>作業中に書き換えたスクリプトは承認待ちにする</span></label>` +
    `<p class="note-warn">${icon("triangle-alert")}<span>実行したプログラムは、共有フォルダの外も読めます</span></p>`;
}
