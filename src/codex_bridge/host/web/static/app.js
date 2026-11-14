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
  if (document.activeElement !== $("exec-mode")) $("exec-mode").value = o.exec_mode;
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

async function loadPolicy() {
  const p = await api("GET", "/policy");
  $("policy").value = p.text;
}

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
$("exec-mode").onchange = async () => { await api("PUT", "/exec-mode", { mode: $("exec-mode").value }); loadPolicy(); };
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
