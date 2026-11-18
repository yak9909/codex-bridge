"use strict";
// The admin page. The parts it is built from (meter, badges, approvals, task list + detail, choice cards)
// are in /admin/common/ui.js and are shared with the lender's page.
const $ = (id) => document.getElementById(id);
const { esc } = UI;
const enc = encodeURIComponent;
const api = async (method, path, body, raw) => {
  const opt = { method, headers: { "X-Codex-Bridge": "1" } };
  if (body !== undefined) {
    if (raw) { opt.body = body; opt.headers["Content-Type"] = "text/plain; charset=utf-8"; }
    else { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  }
  const r = await fetch("/admin/api" + path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
};

const MODE_KEY = "codex-bridge.detail-mode";
let selected = null;        // the task shown on the right
let paused = false;
let thresholdsDirty = false;
let detailMode = localStorage.getItem(MODE_KEY) === "advanced" ? "advanced" : "simple";

// ---- top bar, the table of lenders, where a request goes ---------------------
function renderPolicyProblems(problems) {
  const banner = $("policy-problem");
  const html = (problems || []).map((p) => `<div>
    <span>共有フォルダの設定に、codex-bridge 自身のデータ（設定と記録）を含むフォルダがあります: ${esc(p.folder)}。このままでは安全のため一部の操作ができないことがあります。</span>
    <button type="button" class="ghost mini" data-remove-folder="${esc(p.folder)}">このフォルダを共有から外す</button>
  </div>`).join("");
  // Keep the focused button while overview refreshes in the background.
  if (banner.innerHTML !== html) banner.innerHTML = html;
  banner.hidden = !problems || !problems.length;
}
$("policy-problem").onclick = async (ev) => {
  const button = ev.target.closest("button[data-remove-folder]");
  if (!button) return;
  try {
    if (!permForm) await loadPermissions();
    const path = button.dataset.removeFolder;
    if (await folderOp("remove", { path }, removeQuestion(path))) await refreshTop();
  } catch (e) { folderSay("できませんでした: " + e.message); }
};

async function refreshTop() {
  const [o, rows] = await Promise.all([api("GET", "/overview"), api("GET", "/lenders")]);
  paused = o.paused;
  $("share-setup").hidden = !!o.share_root;
  const online = rows.filter((l) => l.connected).length;
  $("conn").className = "conn " + (online ? "on" : "off");
  $("conn-text").textContent = rows.length
    ? `Codex を貸している人: ${rows.length} 人のうち ${online} 人が接続中` : "Codex を貸している人: まだいません";
  $("pause-btn").classList.toggle("active", paused);
  $("pause-btn").textContent = paused ? "一時停止を解除" : "全体を一時停止";
  $("paused-banner").hidden = !paused;
  renderPolicyProblems(o.policy_problem);
  $("policy-error").hidden = !o.policy_error;
  $("policy-error").textContent = o.policy_error ? "policy.yaml を読めません（前の設定のまま動いています）: " + o.policy_error : "";
  $("s-bind").textContent = "http://" + o.public_bind;
  if (document.activeElement !== $("log-vis")) $("log-vis").value = o.log_visibility;
  const th = o.usage.thresholds;
  if (!thresholdsDirty) { $("th-five").value = th.five_hour; $("th-week").value = th.weekly; }
  drawLenders(rows, th);
  drawTargets(rows);
}

function lenderRow(l, i) {
  const seen = l.since != null;  // has connected since the host started
  const about = [[l.agent_version && `exe ${l.agent_version}`, l.codex_version && `Codex ${l.codex_version}`]
    .filter(Boolean).join("・"), l.auth_mode && `ログイン: ${l.auth_mode}`].filter(Boolean).map(esc).join("<br>");
  const conn = l.connected
    ? `<span class="lamp on"></span>${l.ready ? "接続中" : "接続中（準備中）"}`
    : `<span class="lamp"></span>未接続<div class="small">${seen ? `最後の接続 ${UI.fmtTime(l.since)}` : "まだ接続していません"}</div>`;
  const lim = l.limits;
  const pct = (v) => (v == null ? "なし" : `${esc(v)}%`);
  const limits = !lim ? "—" : lim.five_hour == null && lim.weekly == null ? "なし"
    : `5 時間枠 ${pct(lim.five_hour)}<br>週間枠 ${pct(lim.weekly)}<div class="small">超えたら${lim.force ? "すぐ止める" : "すぐ止めて報告させる"}</div>`;
  const task = l.busy_elsewhere ? "ほかの依頼主の作業中" : l.current_task_id
    ? `<button type="button" class="link" data-task="${esc(l.current_task_id)}">${esc(l.current_task_id)}</button>`
    : seen ? "なし" : "—";
  const caps = l.capabilities;
  return `<tr class="${l.connected ? "" : "off"}">
    <td class="nowrap"><b>${esc(l.name)}</b><div class="small">${about}</div></td>
    <td class="nowrap">${conn}</td>
    <td class="usage"><div data-m="${i}:five_hour" data-label="5 時間枠"></div><div data-m="${i}:weekly" data-label="週間枠"></div></td>
    <td class="nowrap">${limits}</td>
    <td class="nowrap">${task}</td>
    <td class="nowrap">${!caps ? "—" : caps.pre_approval ? "あり" : "なし"}</td>
    <td class="nowrap">${!caps ? "—" : caps.resumable ? "続けられる" : "使い捨て"}</td></tr>`;
}

function drawLenders(rows, th) {
  const body = $("lenders"), sig = JSON.stringify([rows, th]);
  if (body._sig === sig) return;
  body._sig = sig;
  body.innerHTML = rows.map(lenderRow).join("")
    || '<tr><td colspan="7" class="empty">まだ誰もいません。下の「招待コード」で、Codex を貸してくれる人ごとにコードを発行してください。</td></tr>';
  rows.forEach((l, i) => ["five_hour", "weekly"].forEach((k) => {
    const marks = [{ value: th[k], label: "依頼主", kind: "host" }];
    if (l.limits && l.limits[k] != null) marks.push({ value: l.limits[k], label: "貸している人", kind: "lender" });
    UI.meter(body.querySelector(`[data-m="${i}:${k}"]`), l.usage[k], marks);
  }));
}

// "自動" and every lender by name; those who cannot take a request now say why and cannot be picked.
// The one already chosen stays chosen whatever becomes of it (busy, stopped, gone from the list): a
// request must never go to somebody else than the person named, so the host is left to refuse it.
function drawTargets(rows) {
  const sel = $("req-lender"), chosen = sel.value;
  const why = (l) => (!l.connected ? "未接続" : !l.ready ? "準備中" : l.busy_elsewhere ? "ほかの依頼主の作業中" : l.current_task_id ? "作業中"
    : l.usage.blocked_until ? "上限のため停止中" : "");
  const option = (name, w) =>
    `<option value="${esc(name)}"${w && name !== chosen ? " disabled" : ""}>${esc(name)}${w ? `（${w}）` : ""}</option>`;
  const html = '<option value="">自動（使用量に余裕のある人）</option>' + rows.map((l) => option(l.name, why(l))).join("")
    + (chosen && !rows.some((l) => l.name === chosen) ? option(chosen, "一覧にいません") : "");
  if (sel._html === html) return;
  sel.innerHTML = sel._html = html;
  sel.value = chosen;
}

$("lenders").onclick = (ev) => {
  const b = ev.target.closest("button[data-task]");
  if (!b) return;
  select(b.dataset.task);
  $("task-detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
};

// ---- approvals -----------------------------------------------------------------
async function refreshApprovals() {
  const list = await api("GET", "/approvals");
  UI.approvals($("approvals"), list.map((a) => ({ id: a.id, tag: a.kind === "command" ? "コマンド実行" : "削除",
    meta: [a.task_id, `${a.age_sec} 秒前`], body: a.summary })), async (id, approve) => {
    try { await api("POST", "/approvals/" + enc(id), { approve }); } catch (e) { alert(e.message); }
    refreshApprovals();
  });
}

// ---- tasks ---------------------------------------------------------------------
async function refreshTasks() {
  const list = await api("GET", "/tasks");
  if (!list.some((t) => t.task_id === selected)) {  // nothing chosen yet: the one in progress, else the newest
    const first = list.find((t) => UI.ACTIVE_STATES.includes(t.state)) || list[0];
    selected = first ? first.task_id : null;
  }
  UI.taskList($("tasks"), list, selected, select);
  $("req-continue").disabled = !selected;
  if (!selected) $("req-continue").checked = false;
  $("req-continue-label").textContent = selected
    ? `続きとして依頼する（選んでいるタスク ${selected} の会話を続ける。送り先は、前と同じ人です）`
    : "続きとして依頼する（下のタスク一覧で選んだタスクの会話を続ける）";
  $("req-lender").disabled = $("req-continue").checked;
  await showTask();
}

function select(id) { selected = id; refreshTasks().catch((e) => console.error(e)); }

async function showTask() {
  const id = selected;
  if (!id) { UI.taskDetail($("task-detail"), null); return; }
  const t = await api("GET", "/tasks/" + enc(id));
  if (id !== selected) return;  // another one was chosen while this was loading
  UI.taskDetail($("task-detail"), t, {
    mode: detailMode, showLender: true,
    onMode: (m) => { detailMode = m; localStorage.setItem(MODE_KEY, m); },
    onFollowup: async (text) => {
      try { await api("POST", `/tasks/${enc(id)}/followup`, { text }); } catch (e) { alert(e.message); throw e; }
    },
    onCancel: async () => {
      if (!confirm("このタスクを中止しますか？")) return;
      try { await api("POST", `/tasks/${enc(id)}/cancel`); } catch (e) { alert(e.message); }
    },
    onUndo: async () => {
      if (!confirm("このタスクがファイル操作の道具で変えたファイルを元に戻します。よろしいですか？")) return;
      try { const r = await api("POST", `/tasks/${enc(id)}/undo`); alert(`${r.restored.length} 件を元に戻しました`); }
      catch (e) { alert(e.message); }
    },
  });
}

$("req-continue").onchange = () => { $("req-lender").disabled = $("req-continue").checked; };
$("req-send").onclick = async () => {
  const prompt = $("req-prompt").value.trim();
  if (!prompt) { $("req-msg").textContent = "依頼の内容を書いてください"; return; }
  const body = { prompt };
  if ($("req-continue").checked && selected) body.continue_task_id = selected;
  else if ($("req-lender").value) body.lender = $("req-lender").value;
  try {
    const r = await api("POST", "/tasks", body);
    $("req-prompt").value = ""; $("req-continue").checked = false;
    $("req-msg").textContent = `依頼しました（${r.task_id}・担当: ${r.lender}）`;
    select(r.task_id);
  } catch (e) { $("req-msg").textContent = "依頼できません: " + e.message; }
};

// ---- shared folders and permissions ----------------------------------------------
const LEVEL_JA = { hidden: "見せない", read: "読むだけ", write: "書ける" };
const EXEC_MODES = [
  { value: "off", title: "実行しない", desc: "ファイルの読み書きだけを許します。いちばん安全です。" },
  { value: "allowlist", title: "許可リストだけ自動", desc: "下のリストに合うコマンドは自動で実行し、それ以外は承認待ちにします。（おすすめ）" },
  { value: "ask", title: "毎回確認する", desc: "すべてのコマンドが承認待ちになります。" },
  { value: "all", title: "すべて自動", desc: "どんなコマンドでも確認なしで実行します。おすすめしません。" },
];
let permForm = null;     // the form being edited: {main, folders: [{path, level}], advanced_rules, exec, ...}
let permSaved = null;    // what the fields looked like when loaded (to tell unsaved changes)
let permLoaded = null;   // the form as the host sent it (to notice changes made elsewhere)
const expanded = new Set();
const norm = (p) => p.toLowerCase().replace(/\/+$/, "");
const samePath = (a, b) => norm(a) === norm(b);
const inside = (p, base) => norm(p) === norm(base) || norm(p).startsWith(norm(base) + "/");

function ruleOf(path) {
  const f = permForm.folders.find((x) => samePath(x.path, path));
  return f ? f.level : null;
}
function setRule(path, level) {
  const i = permForm.folders.findIndex((x) => samePath(x.path, path));
  if (level === "inherit") { if (i >= 0) permForm.folders.splice(i, 1); }
  else if (i >= 0) permForm.folders[i].level = level;
  else permForm.folders.push({ path, level });
  markDirty();
}
// What applies to a folder: "hidden" anywhere above it wins, else the deepest rule (null: not shared).
// skipOwn: as if the folder had no rule of its own, i.e. what "same as above" would give.
function levelFor(path, skipOwn) {
  const hits = permForm.folders.filter((f) => inside(path, f.path) && !(skipOwn && samePath(f.path, path)));
  if (!hits.length) return null;
  if (hits.some((f) => f.level === "hidden")) return "hidden";
  return hits.reduce((a, b) => (norm(b.path).length > norm(a.path).length ? b : a)).level;
}
function collectForm() {
  const mode = document.querySelector('input[name="mode"]:checked');
  return { ...permForm,
    exec: { mode: mode ? mode.value : permForm.exec.mode, allow: $("perm-allow").value.split("\n"),
            confirm_modified_scripts: $("perm-confirm-mod").checked },
    delete_needs_approval: $("perm-del").checked };
}
const isDirty = () => !!permForm && JSON.stringify(collectForm()) !== permSaved;
function markDirty() {
  $("perm-dirty").hidden = $("perm-reset").hidden = !isDirty();
  renderLevels();
}
function renderLevels() {
  document.querySelectorAll("#tree .tnode").forEach((n) => {
    const path = n.dataset.path, own = ruleOf(path), sel = n.querySelector("select");
    if (n.classList.contains("top")) sel.value = own || "read";
    else {
      sel.value = own || "inherit";
      sel.options[0].textContent = `上と同じ（${LEVEL_JA[levelFor(path, true)] || "見えない"}）`;
    }
    const eff = levelFor(path) || "hidden";
    const badge = n.querySelector(".lv");
    badge.className = "lv " + eff;
    badge.textContent = LEVEL_JA[eff];
  });
  const mode = document.querySelector('input[name="mode"]:checked');
  $("perm-allow-box").style.opacity = mode && mode.value === "allowlist" ? 1 : 0.45;
}
function folderRow(f, top) {
  const levels = (top ? "" : '<option value="inherit"></option>')
    + (top && f.rule !== "hidden" ? "" : '<option value="hidden">見せない</option>')
    + '<option value="read">読むだけ</option><option value="write">書ける</option>';
  return `<li><div class="tnode${top ? " top" : ""}" data-path="${esc(f.path)}">
      <button type="button" class="tog" ${f.has_children ? "" : "disabled"}>${expanded.has(f.path) ? "▾" : "▸"}</button>
      <span class="nm" title="${esc(f.path)}">📁 ${esc(f.name)}</span>
      ${f.main ? '<span class="main-mark" title="相対パスで書かれた場所の起点">メイン</span>' : ""}
      ${top && !f.exists ? '<span class="missing">フォルダが見つかりません</span>' : ""}
      <span class="lv"></span>
      <select>${levels}</select>
      ${top && !f.main && f.rule !== "hidden" ? '<button type="button" class="ghost mini" data-act="main">メインにする</button>' : ""}
      ${top ? '<button type="button" class="ghost mini" data-act="remove">削除</button>' : ""}
    </div><ul hidden></ul></li>`;
}
async function loadChildren(path, ul) {
  const r = await api("GET", "/folders" + (path ? "?path=" + enc(path) : ""));
  ul.innerHTML = r.folders.map((f) => folderRow(f, !path)).join("")
    || (path ? "" : '<li class="small pad">共有フォルダがありません。下の欄で追加してください。</li>');
  for (const li of ul.children) {
    const node = li.querySelector(".tnode");
    if (node && expanded.has(node.dataset.path)) {
      const sub = li.querySelector("ul");
      sub.hidden = false;
      await loadChildren(node.dataset.path, sub).catch(() => expanded.delete(node.dataset.path));  // folder gone
    }
  }
  renderLevels();
}
async function loadPermissions() {
  permForm = await api("GET", "/policy-form");
  permLoaded = JSON.stringify(permForm);
  UI.choiceCards($("perm-modes"), "mode", EXEC_MODES, permForm.exec.mode, markDirty);
  $("perm-allow").value = permForm.exec.allow.join("\n");
  $("perm-confirm-mod").checked = permForm.exec.confirm_modified_scripts;
  $("perm-del").checked = permForm.delete_needs_approval;
  const adv = permForm.advanced_rules || [];
  $("perm-advanced").hidden = !adv.length;
  $("perm-advanced").textContent = adv.length
    ? `ほかに、詳しい設定で書かれた規則が ${adv.length} 件あります（${adv.map((r) => r.path + "=" + LEVEL_JA[r.level]).join("、")}）。上の表示には含まれません。` : "";
  permSaved = JSON.stringify(collectForm());
  await loadChildren("", $("tree"));
  markDirty();
  const p = await api("GET", "/policy");
  if (document.activeElement !== $("policy")) $("policy").value = p.text;
}
// The rules can also change from elsewhere (the agent's tools, the pause button): take them over,
// unless something here is being edited.
async function syncPermissions() {
  if (!permForm || isDirty()) return;
  if (JSON.stringify(await api("GET", "/policy-form")) !== permLoaded) await loadPermissions();
}

const folderSay = (text) => { $("fd-msg").textContent = text; };
// Adding, removing and choosing the main folder take effect at once (the host checks them one by one).
async function folderOp(op, body, question) {
  if (isDirty()) { folderSay("権限に未保存の変更があります。先に「保存して反映」か「元に戻す」を押してください。"); return false; }
  if (question && !confirm(question)) return false;
  try {
    const result = await api("POST", "/folders/" + op, body);
    $("folder-warning").textContent = result.warning || "";
    $("folder-warning").hidden = !result.warning;
    folderSay(result.warning ? result.warning + ": " + body.path : "");
    await loadPermissions();
    return true;
  } catch (e) { folderSay("できませんでした: " + e.message); return false; }
}
function removeQuestion(path) {
  const kept = permForm.folders.filter((f) => inside(f.path, path) && !samePath(f.path, path));
  return `${path} を共有フォルダから外しますか？（フォルダそのものは消えません）`
    + (kept.length ? `\n\nこの中のフォルダに個別の設定が ${kept.length} 件あります。それらは残るので、要らなければ後で外してください:\n`
      + kept.map((f) => `${f.path}（${LEVEL_JA[f.level]}）`).join("\n") : "");
}

$("tree").addEventListener("change", (ev) => {
  const node = ev.target.closest(".tnode");
  if (!node) return;
  setRule(node.dataset.path, ev.target.value);
  node.classList.add("changed");
});
$("tree").addEventListener("click", async (ev) => {
  const act = ev.target.closest("button[data-act]");
  if (act) {
    const path = act.closest(".tnode").dataset.path;
    if (act.dataset.act === "remove") await folderOp("remove", { path }, removeQuestion(path));
    else await folderOp("main", { path }, `メインのフォルダを ${path} に変えますか？\n相対パスで書かれた場所の起点が、このフォルダになります。`);
    return;
  }
  const tog = ev.target.closest(".tog");
  if (!tog || tog.disabled) return;
  const node = tog.closest(".tnode"), sub = node.parentElement.querySelector("ul"), path = node.dataset.path;
  if (expanded.has(path)) { expanded.delete(path); sub.hidden = true; tog.textContent = "▸"; return; }
  expanded.add(path); tog.textContent = "▾"; sub.hidden = false;
  await loadChildren(path, sub);
});
$("share-pick").onclick = async () => {
  const button = $("share-pick"), message = $("share-setup-error");
  message.textContent = "";
  if (isDirty()) { message.textContent = "権限に未保存の変更があります。先に「保存して反映」か「元に戻す」を押してください。"; return; }
  button.disabled = true;
  try {
    const chosen = await api("POST", "/pick-folder");
    if (!chosen.path) return;
    const result = await api("PUT", "/share-root", { path: chosen.path });
    $("folder-warning").textContent = result.warning || "";
    $("folder-warning").hidden = !result.warning;
    await loadPermissions();
    await refreshTop();
  } catch (e) { message.textContent = "設定できませんでした: " + e.message; }
  finally { button.disabled = false; }
};
$("fd-pick").onclick = async () => {
  $("fd-pick").disabled = true;
  try { const r = await api("POST", "/pick-folder"); if (r.path) $("fd-path").value = r.path; }
  catch (e) { folderSay("フォルダを選ぶ画面を開けませんでした: " + e.message); }
  finally { $("fd-pick").disabled = false; }
};
$("fd-add").onclick = async () => {
  const path = $("fd-path").value.trim();
  if (!path) { folderSay("追加するフォルダのフルパスを入れてください"); return; }
  if (await folderOp("add", { path, level: $("fd-level").value })) $("fd-path").value = "";
};
["perm-allow", "perm-confirm-mod", "perm-del"].forEach((id) => $(id).addEventListener("input", markDirty));
$("perm-reset").onclick = () => loadPermissions();
$("perm-save").onclick = async () => {
  try {
    await api("PUT", "/policy-form", { ...collectForm(), paused });
    $("perm-save").textContent = "保存しました ✓"; setTimeout(() => { $("perm-save").textContent = "保存して反映"; }, 1500);
    await loadPermissions();
  } catch (e) { alert("保存できませんでした: " + e.message); }
};
$("policy-save").onclick = async () => {
  try {
    await api("PUT", "/policy", $("policy").value, true);
    $("policy-msg").textContent = "保存しました（すぐに反映されます）";
    await loadPermissions(); refreshTop();
  } catch (e) { $("policy-msg").textContent = "保存できません: " + e.message; }
};

// ---- pause, thresholds, what the lender sees ---------------------------------------
$("pause-btn").onclick = async () => {
  try { await api("POST", "/pause", { paused: !paused }); } catch (e) { alert(e.message); }
  refreshAll();
};
$("log-vis").onchange = () => api("PUT", "/log-visibility", { value: $("log-vis").value }).catch((e) => alert(e.message));
["th-five", "th-week"].forEach((id) => { $(id).oninput = () => { thresholdsDirty = true; }; });
$("th-save").onclick = async () => {
  try {
    await api("PUT", "/thresholds", { five_hour: +$("th-five").value, weekly: +$("th-week").value });
    thresholdsDirty = false; refreshTop();
  } catch (e) { alert(e.message); }
};

// ---- invites -------------------------------------------------------------------------
let invites = [];
let inviteCopyTimer;
function resetInviteCopy() {
  clearTimeout(inviteCopyTimer);
  $("inv-copy").textContent = "コピー";
  $("inv-copy-msg").textContent = "";
}
async function refreshInvites() {
  invites = await api("GET", "/invites");
  $("invites").innerHTML = invites.map((i) => `<li><span>${esc(i.name)} <span class="small">${new Date(i.created * 1000).toLocaleString()}</span></span>
    <button type="button" class="ghost mini" data-revoke="${esc(i.name)}">失効</button></li>`).join("") || '<li class="small">ありません</li>';
}
$("inv-issue").onclick = async () => {
  const name = $("inv-name").value.trim() || "friend";
  if (invites.some((i) => i.name === name)
      && !confirm(`「${name}」の招待コードは発行済みです。発行し直すと、前のコードは使えなくなります。よろしいですか？`)) return;
  try {
    const r = await api("POST", "/invites", { name });
    resetInviteCopy();
    $("inv-code").textContent = r.code; $("inv-out").hidden = false;
    refreshInvites(); refreshTop();
  } catch (e) { alert(e.message); }
};
$("inv-copy").onclick = async () => {
  const code = $("inv-code").textContent;
  resetInviteCopy();
  try {
    await navigator.clipboard.writeText(code);
    if ($("inv-code").textContent !== code) return;
    clearTimeout(inviteCopyTimer);
    $("inv-copy").textContent = "コピーしました ✓";
    inviteCopyTimer = setTimeout(resetInviteCopy, 2000);
  } catch (e) {
    if ($("inv-code").textContent !== code) return;
    resetInviteCopy();
    const range = document.createRange();
    range.selectNodeContents($("inv-code"));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    $("inv-copy-msg").textContent = "選択しました。Ctrl+C でコピーしてください";
  }
};
$("invites").onclick = async (ev) => {
  const b = ev.target.closest("button[data-revoke]");
  if (!b) return;
  if (!confirm(`${b.dataset.revoke} の招待コードを失効させますか？その人の agent は接続できなくなります。`)) return;
  await api("DELETE", "/invites/" + enc(b.dataset.revoke));
  refreshInvites(); refreshTop();
};

// ---- start ---------------------------------------------------------------------------
async function refreshAll() {
  await Promise.all([refreshTop(), refreshApprovals(), refreshTasks(), syncPermissions()]).catch((e) => console.error(e));
}
UI.askNotify();
refreshAll(); loadPermissions().catch((e) => console.error(e)); refreshInvites();
let timer = null;
if (!location.search.includes("nolive")) {  // ?nolive: a still page, for screenshots
  const es = new EventSource("/admin/api/events");
  es.onmessage = () => { clearTimeout(timer); timer = setTimeout(refreshAll, 150); };
  setInterval(refreshTop, 15000);
}
