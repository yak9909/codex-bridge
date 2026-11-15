"use strict";
// Shared UI parts own their filters, selection and scroll state. This page supplies local API data.
const $ = (id) => document.getElementById(id);
const enc = encodeURIComponent;
const api = async (method, path, body) => {
  const options = { method, headers: { "X-Codex-Bridge": "1" } };
  if (body !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
  const response = await fetch("/api" + path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || response.statusText);
  return data;
};
function say(message, error = false) {
  $("message").textContent = message;
  $("message").classList.toggle("error", error);
  $("message").hidden = false;
}
const MODE_KEY = "codex-bridge.lender-detail-mode";
let selected = null, session = null, paused = false, refreshing = false;
let mode = localStorage.getItem(MODE_KEY) === "advanced" ? "advanced" : "simple";
let preEnabled = false;
const VIS = { summary: "道具の記録: 非表示", files: "道具の記録: 名前と対象パス", all: "道具の記録: 結果の本文まで" };

function drawOverview(data) {
  paused = data.paused;
  $("conn").className = "conn " + (data.connected ? "on" : "off");
  $("conn-text").textContent = `${data.host_name || "依頼主"} · ${data.connected ? "接続中" : "未接続"}`;
  $("host-name").textContent = data.host_name || "—";
  $("codex-version").textContent = data.codex_version || "—";
  $("agent-version").textContent = data.agent_version || "—";
  $("pause-btn").textContent = paused ? "一時停止を解除" : "一時停止";
  $("pause-btn").classList.toggle("active", paused);
  $("paused-banner").hidden = !paused;
  $("state").innerHTML = data.state === "idle" ? '<span class="state">待機中</span>'
    : data.state === "paused" ? '<span class="state cancelled">一時停止</span>' : UI.stateBadge(data.state);
  for (const [key, id] of [["five_hour", "usage-five"], ["weekly", "usage-week"]]) {
    UI.meter($(id), data.usage[key], [
      { value: data.thresholds[key], label: "依頼主", kind: "host" },
      { value: data.limits[key], label: "あなた", kind: "lender" },
    ]);
  }
  $("visibility").textContent = (VIS[data.log_visibility] || VIS.summary) + "（依頼主が設定）。依頼・発言・計画・最終報告はいつも表示します。";
}

function drawPending(pending) {
  $("pending-region").hidden = !preEnabled && !pending;
  const items = pending ? [{ id: `${pending.task_id}:${pending.kind}:${pending.created}`, body: pending.prompt,
    tag: pending.kind === "followup" ? "追加の指示" : "新しい依頼", meta: [pending.task_id, UI.fmtTime(pending.created)] }] : [];
  UI.approvals($("approvals"), items, async (id, approve) => {
    try {
      await api("POST", "/pending", { task_id: pending.task_id, created: pending.created, approve,
        reason: approve ? "" : $("reject-reason").value });
      $("reject-reason").value = "";
      await refresh();
    } catch (error) { say(error.message, true); await refresh(); }
  });
  $("reject-reason").parentElement.hidden = !pending;
}

async function drawTasks(list) {
  if (!list.some((t) => t.task_id === selected)) {
    selected = (list.find((t) => UI.ACTIVE_STATES.includes(t.state)) || list[0])?.task_id || null;
  }
  UI.taskList($("tasks"), list, selected, (id) => { selected = id; refresh().catch((error) => say(error.message, true)); });
  const id = selected;
  const task = id ? await api("GET", "/tasks/" + enc(id)) : null;
  if (selected !== id) return;
  UI.taskDetail($("task-detail"), task, { mode, showLender: false,
    onMode: (value) => { mode = value; localStorage.setItem(MODE_KEY, value); } });
}

function drawSessions(rows) {
  if (session && !rows.some((s) => s.thread_id === session)) session = null;
  $("session-count").textContent = rows.length;
  $("session-filter").hidden = !session;
  $("filter-name").textContent = session ? `セッション: ${session}` : "";
  const html = rows.map((s) => `<tr${s.thread_id === session ? ' class="selected"' : ""}>
    <td><button type="button" class="link session-id" data-session="${UI.esc(s.thread_id)}">${UI.esc(s.thread_id)}</button><div class="small">${UI.esc(new Date(s.created * 1000).toLocaleString("ja-JP"))}</div></td>
    <td>${s.task_ids.map(UI.esc).join("、") || "—"}</td><td>${s.active ? UI.stateBadge("running") : "待機中"}</td>
    <td>${s.disposable ? "使い捨て" : "残す"}</td><td><button class="bad mini" data-delete="${UI.esc(s.thread_id)}"${s.active ? " disabled" : ""}>削除</button></td></tr>`).join("")
    || '<tr><td colspan="5" class="empty">セッションはありません。</td></tr>';
  if ($("sessions")._html !== html) $("sessions").innerHTML = $("sessions")._html = html;
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const [overview, pending, sessions] = await Promise.all([api("GET", "/overview"), api("GET", "/pending"), api("GET", "/sessions")]);
    drawOverview(overview); drawPending(pending); drawSessions(sessions);
    await drawTasks(await api("GET", "/tasks" + (session ? "?thread_id=" + enc(session) : "")));
  } finally { refreshing = false; }
}

$("sessions").onclick = async (event) => {
  const pick = event.target.closest("button[data-session]");
  if (pick) {
    session = pick.dataset.session; selected = null;
    try { await refresh(); $("task-region").scrollIntoView({ behavior: "smooth", block: "start" }); }
    catch (error) { say(error.message, true); }
  }
  const remove = event.target.closest("button[data-delete]");
  if (!remove || remove.disabled || !confirm(`セッション ${remove.dataset.delete} を削除します。作業の記録は残ります。よろしいですか？`)) return;
  remove.disabled = true;
  try { await api("DELETE", "/sessions/" + enc(remove.dataset.delete)); await refresh(); }
  catch (error) { say(error.message, true); remove.disabled = false; }
};
$("filter-clear").onclick = () => { session = null; refresh().catch((error) => say(error.message, true)); };
$("pause-btn").onclick = async () => {
  $("pause-btn").disabled = true;
  try { await api("POST", "/pause", { paused: !paused }); await refresh(); }
  catch (error) { say(error.message, true); }
  finally { $("pause-btn").disabled = false; }
};
$("notify-btn").onclick = async () => {
  if (!("Notification" in window)) { $("notify-state").textContent = "このブラウザでは通知を使えません。"; return; }
  const permission = await Notification.requestPermission();
  $("notify-state").textContent = permission === "granted" ? "通知は有効です。" : "通知はブラウザの設定から許可できます。";
};

async function start() {
  try {
    await LenderSettings.init(api, say, async () => {
      const data = await api("GET", "/settings"); preEnabled = data.settings.pre_approval.enabled; await refresh();
    });
    preEnabled = (await api("GET", "/settings")).settings.pre_approval.enabled;
    await refresh();
  } catch (error) { say(error.message, true); }
  setInterval(() => refresh().catch((error) => say(`画面を更新できません: ${error.message}`, true)), 2000);
}
start();
