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
let selected = null, session = null, paused = false, refreshing = false, connectionFilter = "";
let mode = localStorage.getItem(MODE_KEY) === "advanced" ? "advanced" : "simple";
let connectionRows = [];

function drawConnections(rows) {
  connectionRows = rows;
  const body = $("connections");
  body.querySelector(".empty-row")?.remove();
  for (const tr of [...body.children]) {
    if (!rows.some((r) => r.id === tr.dataset.id)) { LenderSettings.removed(tr.dataset.id); tr.remove(); }
  }
  for (const row of rows) {
    let tr = [...body.children].find((el) => el.dataset.id === row.id);
    if (!tr) {
      tr = document.createElement("tr");
      tr.dataset.id = row.id;
      tr.innerHTML = `<td class="connection-host"></td><td class="connection-state"></td><td class="connection-busy"></td>
        <td><div class="row"><button class="ghost mini" data-settings>設定</button><button class="ghost mini" data-invite>招待コードを入れ直す</button><button class="bad mini" data-remove>外す</button></div>
          <p class="invite-error" hidden>招待コードが無効になっています。依頼主から新しいコードをもらって入れ直してください。</p>
          <details class="replace-invite"><summary>招待コードの入れ直し</summary><form class="row"><input class="grow" name="code" aria-label="入れ直す招待コード" placeholder="新しい招待コード" autocomplete="off" required><button class="mini" type="submit">入れ直す</button></form></details></td>`;
      body.append(tr);
    }
    tr.querySelector(".connection-host").textContent = row.host_name;
    tr.querySelector(".connection-state").textContent = row.connected ? "接続中" : "未接続";
    tr.querySelector(".connection-busy").textContent = row.busy ? "作業中・承認待ち" : "待機中";
    tr.classList.toggle("invite-rejected", row.invite_problem === "rejected");
    tr.querySelector(".invite-error").hidden = row.invite_problem !== "rejected";
    const replacement = tr.querySelector("details");
    if (row.invite_problem === "rejected" && tr.dataset.problem !== "rejected") replacement.open = true;
    tr.dataset.problem = row.invite_problem || "";
    tr.querySelectorAll("[data-invite], [data-remove], details button, details input").forEach((el) => { el.disabled = row.busy; });
  }
  if (!rows.length) body.innerHTML = '<tr class="empty-row"><td colspan="4" class="empty">接続先はありません。依頼主からもらった招待コードを下の欄に貼り付けてください。</td></tr>';
  const options = '<option value="">すべての接続先</option>' + rows.map((r) => `<option value="${UI.esc(r.id)}">${UI.esc(r.host_name)}</option>`).join("");
  if ($("connection-filter")._html !== options) {
    $("connection-filter").innerHTML = $("connection-filter")._html = options;
    $("connection-filter").value = connectionFilter;
  }
}

$("connections").onclick = async (event) => {
  const tr = event.target.closest("tr[data-id]");
  if (!tr) return;
  const row = connectionRows.find((r) => r.id === tr.dataset.id);
  const button = event.target.closest("button");
  if (!row || !button || button.disabled) return;
  try {
    if (button.hasAttribute("data-settings")) await LenderSettings.open(api, row.id, row.host_name);
    if (button.hasAttribute("data-invite")) { tr.querySelector("details").open = true; tr.querySelector("input").focus(); }
    if (button.hasAttribute("data-remove") && confirm(`${row.host_name} の接続先を外します。設定・セッションの一覧・作業の記録も消えます。よろしいですか？`)) {
      button.disabled = true;
      await api("DELETE", "/connections/" + enc(row.id));
      LenderSettings.removed(row.id);
      if (connectionFilter === row.id) connectionFilter = "";
      session = null;
      await refresh();
    }
  } catch (error) { say(error.message, true); button.disabled = false; }
};
$("connections").onsubmit = async (event) => {
  event.preventDefault();
  const form = event.target, tr = form.closest("tr"), button = form.querySelector("button");
  button.disabled = true;
  try {
    await api("PUT", `/connections/${enc(tr.dataset.id)}/invite`, { code: form.elements.code.value.trim() });
    form.reset(); tr.querySelector("details").open = false; await refresh();
  } catch (error) { say(error.message, true); }
  finally { button.disabled = false; }
};
$("add-connection").onsubmit = async (event) => {
  event.preventDefault();
  const form = event.target, button = form.querySelector("button");
  button.disabled = true;
  try { await api("POST", "/connections", { code: form.elements.code.value.trim() }); form.reset(); await refresh(); }
  catch (error) { say(error.message, true); }
  finally { button.disabled = false; }
};
$("connection-filter").onchange = () => {
  connectionFilter = $("connection-filter").value; session = null; selected = null;
  refresh().catch((error) => say(error.message, true));
};

function drawOverview(data) {
  paused = data.paused;
  $("conn").className = "conn " + (data.connected ? "on" : "off");
  $("conn-text").textContent = data.connected ? "接続中" : "未接続";
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
  $("visibility").textContent = "道具の記録の表示範囲は、接続先の「設定」で選べます。依頼・発言・計画・最終報告はいつも表示します。";
}

function drawPending(pending) {
  $("pending-region").hidden = !pending;
  const items = pending ? [{ id: `${pending.task_id}:${pending.kind}:${pending.created}`, body: pending.prompt,
    tag: pending.kind === "followup" ? "追加の指示" : "新しい依頼", meta: [pending.host_name, pending.task_id, UI.fmtTime(pending.created)] }] : [];
  UI.approvals($("approvals"), items, async (id, approve) => {
    try {
      await api("POST", "/pending", { task_id: pending.task_id, connection_id: pending.connection_id, created: pending.created, approve,
        reason: approve ? "" : $("reject-reason").value });
      $("reject-reason").value = "";
      await refresh();
    } catch (error) { say(error.message, true); await refresh(); }
  });
  $("reject-reason").parentElement.hidden = !pending;
}

async function drawTasks(list) {
  const key = (t) => `${t.connection_id}:${t.task_id}`;
  if (!list.some((t) => key(t) === selected)) {
    const first = list.find((t) => UI.ACTIVE_STATES.includes(t.state)) || list[0];
    selected = first ? key(first) : null;
  }
  // Keep a connection in the selection key, since two hosts can use the same task id.
  UI.taskList($("tasks"), list.map((t) => ({ ...t, task_id: key(t), lender: t.host_name })), selected,
    (id) => { selected = id; refresh().catch((error) => say(error.message, true)); });
  $("tasks").querySelectorAll(".t-item").forEach((el, i) => { if (list[i]) el.title = list[i].task_id; });
  const id = selected;
  const row = list.find((t) => key(t) === id);
  const task = row ? await api("GET", "/tasks/" + enc(row.task_id) + "?connection_id=" + enc(row.connection_id)) : null;
  if (selected !== id) return;
  if (task) task.lender = task.host_name;
  UI.taskDetail($("task-detail"), task, { mode, showLender: true, lenderLabel: "依頼主",
    onMode: (value) => { mode = value; localStorage.setItem(MODE_KEY, value); } });
}

function drawSessions(rows) {
  if (session && !rows.some((s) => s.thread_id === session.thread_id && s.connection_id === session.connection_id)) session = null;
  $("session-count").textContent = rows.length;
  $("session-filter").hidden = !session;
  $("filter-name").textContent = session ? `セッション: ${session.thread_id}` : "";
  const html = rows.map((s) => `<tr${s.thread_id === session?.thread_id && s.connection_id === session?.connection_id ? ' class="selected"' : ""}>
    <td>${UI.esc(s.host_name)}</td><td><button type="button" class="link session-id" data-connection="${UI.esc(s.connection_id)}" data-session="${UI.esc(s.thread_id)}">${UI.esc(s.thread_id)}</button><div class="small">${UI.esc(new Date(s.created * 1000).toLocaleString("ja-JP"))}</div></td>
    <td>${s.task_ids.map(UI.esc).join("、") || "—"}</td><td>${s.active ? UI.stateBadge("running") : "待機中"}</td>
    <td>${s.disposable ? "使い捨て" : "残す"}</td><td><button class="bad mini" data-connection="${UI.esc(s.connection_id)}" data-delete="${UI.esc(s.thread_id)}"${s.active ? " disabled" : ""}>削除</button></td></tr>`).join("")
    || '<tr><td colspan="6" class="empty">セッションはありません。</td></tr>';
  if ($("sessions")._html !== html) $("sessions").innerHTML = $("sessions")._html = html;
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const query = connectionFilter ? "?connection_id=" + enc(connectionFilter) : "";
    const [overview, pending, sessions, connections] = await Promise.all([api("GET", "/overview"), api("GET", "/pending" + query), api("GET", "/sessions" + query), api("GET", "/connections")]);
    drawOverview(overview); drawPending(pending); drawConnections(connections); drawSessions(sessions);
    const params = new URLSearchParams();
    if (connectionFilter) params.set("connection_id", connectionFilter);
    if (session) { params.set("connection_id", session.connection_id); params.set("thread_id", session.thread_id); }
    await drawTasks(await api("GET", "/tasks?" + params));
  } finally { refreshing = false; }
}

$("sessions").onclick = async (event) => {
  const pick = event.target.closest("button[data-session]");
  if (pick) {
    session = { thread_id: pick.dataset.session, connection_id: pick.dataset.connection }; selected = null;
    try { await refresh(); $("task-region").scrollIntoView({ behavior: "smooth", block: "start" }); }
    catch (error) { say(error.message, true); }
  }
  const remove = event.target.closest("button[data-delete]");
  if (!remove || remove.disabled || !confirm(`セッション ${remove.dataset.delete} を削除します。作業の記録は残ります。よろしいですか？`)) return;
  remove.disabled = true;
  try { await api("DELETE", "/sessions/" + enc(remove.dataset.delete) + "?connection_id=" + enc(remove.dataset.connection)); await refresh(); }
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
    await LenderSettings.init(api, say, refresh);
    await refresh();
  } catch (error) { say(error.message, true); }
  setInterval(() => refresh().catch((error) => say(`画面を更新できません: ${error.message}`, true)), 2000);
}
start();
