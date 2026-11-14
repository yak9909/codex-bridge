"use strict";
const $ = (id) => document.getElementById(id);
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
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const STATE_JA = { queued: "受付", running: "作業中", completed: "完了", suspended_quota: "中断（枠切れ）",
  failed: "失敗", cancelled: "中止", agent_offline: "接続待ち" };
let selected = null, knownApprovals = new Set(), thresholdsDirty = false;

function meter(el, w, limit) {
  const pct = w ? w.used_percent : 0;
  el.querySelector(".fill").style.width = Math.min(100, pct) + "%";
  el.querySelector(".mark").style.left = `calc(${limit}% - 1px)`;
  el.classList.toggle("hot", !!w && pct >= limit);
  el.querySelector(".val").textContent = w ? `${pct.toFixed(0)}%・${(w.resets_at_local || "").replace("T", " ").slice(0, 16)} に戻る` : "—";
}

async function refreshOverview() {
  const o = await api("GET", "/overview");
  const a = o.agent || {};
  $("conn").className = "conn " + (a.connected ? "on" : "off");
  $("conn-text").textContent = a.connected ? "知人の agent に接続中" : "知人の agent は未接続";
  $("pause-btn").classList.toggle("active", o.paused);
  $("pause-btn").textContent = o.paused ? "一時停止を解除" : "全体を一時停止";
  $("paused-banner").hidden = !o.paused;
  $("policy-error").hidden = !o.policy_error;
  $("policy-error").textContent = o.policy_error ? "policy.yaml を読めません（前の設定のまま動いています）: " + o.policy_error : "";
  $("s-root").textContent = o.share_root;
  $("s-bind").textContent = "http://" + o.public_bind;
  $("s-agent").textContent = a.connected ? `接続中（agent ${a.agent_version || "?"}・codex ${a.codex_version || "?"}・${a.auth_mode || "?"}）` : "未接続";
  $("s-task").textContent = o.current_task || "なし";
  $("s-exec").textContent = MODE_JA[o.exec_mode] || o.exec_mode;
  if (document.activeElement !== $("log-vis")) $("log-vis").value = o.log_visibility;
  const u = o.usage, th = u.thresholds;
  meter($("m-five"), u.five_hour, th.five_hour);
  meter($("m-week"), u.weekly, th.weekly);
  if (!thresholdsDirty) { $("th-five").value = th.five_hour; $("th-week").value = th.weekly; }
  $("blocked").textContent = u.blocked_until ? `しきい値を超えているため、新しい依頼を止めています（${u.blocked_until.replace("T", " ")} まで）` : "";
}

async function refreshApprovals() {
  const list = await api("GET", "/approvals");
  $("ap-count").textContent = list.length;
  $("approvals-card").classList.toggle("has", list.length > 0);
  const box = $("approvals");
  if (!list.length) { box.innerHTML = '<p class="empty">確認待ちはありません。</p>'; return; }
  box.innerHTML = list.map((a) => `
    <div class="ap"><div class="what"><span class="small">${[a.kind === "command" ? "コマンド実行" : "削除", a.task_id, `${a.age_sec} 秒前`].filter(Boolean).map(esc).join("・")}</span>
      <code>${esc(a.summary)}</code></div>
      <button class="ok" data-id="${a.id}" data-ok="1">許可</button><button class="bad" data-id="${a.id}" data-ok="0">拒否</button></div>`).join("");
  const fresh = list.filter((a) => !knownApprovals.has(a.id));
  list.forEach((a) => knownApprovals.add(a.id));
  if (fresh.length && "Notification" in window && Notification.permission === "granted") {
    new Notification("codex-bridge: 確認待ち", { body: fresh[0].summary });
  }
}

async function refreshTasks() {
  const list = await api("GET", "/tasks");
  $("tasks").innerHTML = list.map((t) => `
    <li data-id="${t.task_id}" class="${t.task_id === selected ? "sel" : ""}">
      <div><span class="state ${t.state}">${STATE_JA[t.state] || t.state}</span> <span class="id">${t.task_id}</span></div>
      <div class="p">${esc(t.prompt)}</div></li>`).join("") || '<p class="empty">まだタスクはありません。</p>';
  if (selected) await showTask(selected);
}

function eventLine(e) {
  const d = e.data || {};
  const t = new Date(e.ts * 1000).toLocaleTimeString();
  if (e.kind === "agent_message") return `<span class="k">${t} 発言</span>${esc(d.text)}`;
  if (e.kind === "tool_call") return `<span class="k">${t} 道具</span>${esc(d.tool)} ${esc(d.arguments)}`;
  return `<span class="k">${t} ${esc(e.kind)}</span>${esc(d.text || d.status || "")}`;
}

async function showTask(id) {
  selected = id;
  const t = await api("GET", "/tasks/" + encodeURIComponent(id));
  $("task-detail").innerHTML = `
    <h2>タスクの詳細 <span class="state ${t.state}">${STATE_JA[t.state] || t.state}</span></h2>
    <dl class="kv"><dt>task_id</dt><dd>${esc(t.task_id)}</dd><dt>セッション</dt><dd>${esc(t.thread_id || "—")}</dd>
      ${t.resume_at ? `<dt>再開可能</dt><dd>${esc(t.resume_at.replace("T", " "))}</dd>` : ""}
      <dt>変更したファイル</dt><dd>${t.changed_files.map(esc).join("<br>") || "—"}</dd></dl>
    <h3>依頼文</h3><div class="report">${esc(t.prompt)}</div>
    <h3>最終報告</h3><div class="report">${esc(t.report || "（まだありません）")}</div>
    <div class="row"><button class="ghost" id="undo-btn">このタスクのファイル変更を取り消す</button></div>
    <div class="events">${t.events.slice().reverse().map((e) => `<div>${eventLine(e)}</div>`).join("")}</div>`;
  $("undo-btn").onclick = async () => {
    if (!confirm("このタスクがファイル操作の道具で変えたファイルを元に戻します。よろしいですか？")) return;
    try { const r = await api("POST", `/tasks/${encodeURIComponent(id)}/undo`); alert(`${r.restored.length} 件を元に戻しました`); }
    catch (e) { alert(e.message); }
  };
}

// ---- permissions editor ---------------------------------------------------
const MODE_JA = { off: "実行しない", allowlist: "許可リストだけ自動", ask: "毎回確認する", all: "すべて自動" };
const LEVEL_JA = { hidden: "見せない", read: "読むだけ", write: "書ける" };
let permForm = null, permSaved = null;
const expanded = new Set();

function ruleOf(path) {
  const f = permForm.folders.find((x) => x.path.toLowerCase() === path.toLowerCase());
  return f ? f.level : null;
}
function setRule(path, level) {
  permForm.folders = permForm.folders.filter((x) => x.path.toLowerCase() !== path.toLowerCase());
  if (level !== "inherit") permForm.folders.push({ path, level });
  markDirty();
}
// The level a folder gets from its own rule, else its parent's, else the default (hidden always wins upward).
function levelFor(path) {
  const parts = path.split("/");
  for (let i = 1; i <= parts.length; i++) if (ruleOf(parts.slice(0, i).join("/")) === "hidden") return "hidden";
  for (let i = parts.length; i >= 1; i--) { const r = ruleOf(parts.slice(0, i).join("/")); if (r) return r; }
  return permForm.default;
}
function markDirty() {
  const dirty = JSON.stringify(collectForm()) !== permSaved;
  $("perm-dirty").hidden = $("perm-reset").hidden = !dirty;
  renderLevels();
}
function collectForm() {
  if (!permForm) return null;
  const mode = document.querySelector('input[name="mode"]:checked');
  return { ...permForm, default: $("perm-default").value,
    exec: { mode: mode ? mode.value : permForm.exec.mode, allow: $("perm-allow").value.split("\n"),
            confirm_modified_scripts: $("perm-confirm-mod").checked },
    delete_needs_approval: $("perm-del").checked };
}
function renderLevels() {
  permForm.default = $("perm-default").value;
  document.querySelectorAll(".tnode").forEach((n) => {
    const path = n.dataset.path, own = ruleOf(path);
    const sel = n.querySelector("select");
    sel.value = own || "inherit";
    const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : null;
    sel.options[0].textContent = `上と同じ（${LEVEL_JA[parent ? levelFor(parent) : permForm.default]}）`;
    const eff = levelFor(path);
    const badge = n.querySelector(".lv");
    badge.className = "lv " + eff; badge.textContent = LEVEL_JA[eff];
  });
  $("perm-allow-box").style.opacity = (document.querySelector('input[name="mode"]:checked') || {}).value === "allowlist" ? 1 : 0.45;
}
async function loadChildren(path, ul) {
  const r = await api("GET", "/folders" + (path ? "?path=" + encodeURIComponent(path) : ""));
  ul.innerHTML = r.folders.map((f) => `
    <li><div class="tnode" data-path="${esc(f.path)}">
      <button class="tog" ${f.has_children ? "" : "disabled"}>${expanded.has(f.path) ? "▾" : "▸"}</button>
      <span class="nm">📁 ${esc(f.name)}</span>
      <span class="lv"></span>
      <select><option value="inherit"></option><option value="hidden">見せない</option>
        <option value="read">読むだけ</option><option value="write">書ける</option></select>
    </div><ul hidden></ul></li>`).join("") || (path ? "" : '<li class="small" style="padding:8px">フォルダがありません</li>');
  for (const li of ul.children) {
    const node = li.querySelector(".tnode"); if (!node) continue;
    if (expanded.has(node.dataset.path)) { const sub = li.querySelector("ul"); sub.hidden = false; await loadChildren(node.dataset.path, sub); }
  }
  renderLevels();
}
async function loadPermissions() {
  permForm = await api("GET", "/policy-form");
  $("perm-default").value = permForm.default;
  document.querySelectorAll('input[name="mode"]').forEach((r) => r.checked = r.value === permForm.exec.mode);
  $("perm-allow").value = permForm.exec.allow.join("\n");
  $("perm-confirm-mod").checked = permForm.exec.confirm_modified_scripts;
  $("perm-del").checked = permForm.delete_needs_approval;
  const adv = permForm.advanced_rules || [];
  $("perm-advanced").hidden = !adv.length;
  $("perm-advanced").textContent = adv.length ? `ほかに、詳しい設定で書かれた規則が ${adv.length} 件あります（${adv.map((r) => r.path + "=" + LEVEL_JA[r.level]).join("、")}）。` : "";
  permSaved = JSON.stringify(collectForm());
  await loadChildren("", $("tree"));
  markDirty();
  const p = await api("GET", "/policy");
  $("policy").value = p.text;
}
const loadPolicy = loadPermissions;

$("tree").addEventListener("change", (ev) => {
  const node = ev.target.closest(".tnode"); if (!node) return;
  setRule(node.dataset.path, ev.target.value);
  node.classList.add("changed");
});
$("tree").addEventListener("click", async (ev) => {
  const tog = ev.target.closest(".tog"); if (!tog || tog.disabled) return;
  const node = tog.closest(".tnode"), sub = node.parentElement.querySelector("ul"), path = node.dataset.path;
  if (expanded.has(path)) { expanded.delete(path); sub.hidden = true; tog.textContent = "▸"; return; }
  expanded.add(path); tog.textContent = "▾"; sub.hidden = false; await loadChildren(path, sub);
});
["perm-default", "perm-allow", "perm-confirm-mod", "perm-del"].forEach((id) => $(id).addEventListener("input", markDirty));
$("perm-modes").addEventListener("change", markDirty);
$("perm-reset").onclick = loadPermissions;
$("perm-save").onclick = async () => {
  try {
    await api("PUT", "/policy-form", collectForm());
    $("perm-save").textContent = "保存しました ✓"; setTimeout(() => $("perm-save").textContent = "保存して反映", 1500);
    await loadPermissions(); refreshOverview();
  } catch (e) { alert("保存できませんでした: " + e.message); }
};

async function refreshInvites() {
  const list = await api("GET", "/invites");
  $("invites").innerHTML = list.map((i) => `<li><span>${esc(i.name)} <span class="small">${new Date(i.created * 1000).toLocaleString()}</span></span>
    <button class="ghost" data-revoke="${esc(i.name)}">失効</button></li>`).join("") || '<li class="small">ありません</li>';
}

async function refreshAll() {
  await Promise.all([refreshOverview(), refreshApprovals(), refreshTasks()]).catch((e) => console.error(e));
}

$("approvals").onclick = async (ev) => {
  const b = ev.target.closest("button[data-id]"); if (!b) return;
  try { await api("POST", "/approvals/" + b.dataset.id, { approve: b.dataset.ok === "1" }); } catch (e) { alert(e.message); }
  refreshApprovals();
};
$("tasks").onclick = (ev) => { const li = ev.target.closest("li[data-id]"); if (li) { selected = li.dataset.id; refreshTasks(); } };
$("pause-btn").onclick = async () => {
  const paused = !$("pause-btn").classList.contains("active");
  await api("POST", "/pause", { paused }); refreshOverview(); loadPolicy();
};
$("log-vis").onchange = () => api("PUT", "/log-visibility", { value: $("log-vis").value });
["th-five", "th-week"].forEach((id) => $(id).oninput = () => { thresholdsDirty = true; });
$("th-save").onclick = async () => {
  try { await api("PUT", "/thresholds", { five_hour: +$("th-five").value, weekly: +$("th-week").value }); thresholdsDirty = false; refreshOverview(); }
  catch (e) { alert(e.message); }
};
$("policy-save").onclick = async () => {
  try { await api("PUT", "/policy", $("policy").value, true); $("policy-msg").textContent = "保存しました（すぐに反映されます）"; refreshOverview(); }
  catch (e) { $("policy-msg").textContent = "保存できません: " + e.message; }
};
$("inv-issue").onclick = async () => {
  const r = await api("POST", "/invites", { name: $("inv-name").value });
  $("inv-code").textContent = r.code; $("inv-out").hidden = false; refreshInvites();
};
$("inv-copy").onclick = () => navigator.clipboard.writeText($("inv-code").textContent);
$("invites").onclick = async (ev) => {
  const b = ev.target.closest("button[data-revoke]"); if (!b) return;
  if (!confirm(`${b.dataset.revoke} の招待コードを失効させますか？知人の agent は接続できなくなります。`)) return;
  await api("DELETE", "/invites/" + encodeURIComponent(b.dataset.revoke)); refreshInvites();
};

if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
refreshAll(); loadPolicy(); refreshInvites();
let timer = null;
if (!location.search.includes("nolive")) {  // ?nolive: static view for screenshots
  const es = new EventSource("/admin/api/events");
  es.onmessage = () => { clearTimeout(timer); timer = setTimeout(refreshAll, 150); };
  setInterval(refreshOverview, 15000);
}
