// The lender's screen. Stable slots keep typing, popovers and scrolling intact during refresh.
import { get, send, onChange, showError, clearError, esc, ago } from "/app/static/api.js";
import { icon, meter, busyInside, ACTIVE, LOG_LABEL } from "/app/static/common.js";
import { listHtml, headHtml, timelineHtml } from "/app/static/use-tasks.js";
import { connectionHead, settingsHtml, modelSummary, preSummary } from "/app/static/lend-cards.js";
import { pcHtml, setupErrorHtml } from "/app/static/lend-pages.js";

const S = { open: new Set(), cards: new Map(), selected: null, filter: "", tab: "tasks", invite: false, code: "", pc: { edit: null, workdir: "", path: "", message: "" } };
const keyFor = (t) => JSON.stringify([t.connection_id, t.task_id]);
export function pendingMeta(p, now = Date.now() / 1000) {
  const remaining = p.timeout_min == null ? null : Math.ceil((p.created + p.timeout_min * 60 - now) / 60);
  return [p.host_name, p.model, p.effort, ago(p.created), remaining == null ? null : `あと ${remaining} 分で取り消し`]
    .filter((part) => part != null && part !== "").join("・");
}
const cardUi = (id) => {
  if (!S.cards.has(id)) S.cards.set(id, { replace: false, code: "", confirm: null });
  return S.cards.get(id);
};
const fromHtml = (html) => {
  const t = document.createElement("template");
  t.innerHTML = html;
  return t.content.firstElementChild;
};
const SHELL = `<div class="shell lend-shell">
  <aside class="pane side" aria-label="使用量と依頼主">
    <div data-slot="setup-error"></div>
    <div class="block"><h2>使用量</h2><div class="meters" data-slot="meters"></div><div class="limits" data-slot="limits"></div></div>
    <div class="pane-head"><h2>依頼主</h2><span class="count" data-slot="count">0</span><span class="cap">作業は同時に 1 つ</span><span class="spacer"></span><button class="secondary sm" data-act="invite-open">${icon("plug")}追加</button></div>
    <div data-slot="invite"></div><div class="pane-body scroll" data-slot="connections"></div>
  </aside>
  <div class="work no-alerts"><div class="alerts" data-slot="pending" aria-label="許可を待っている依頼" hidden></div>
    <div class="surface"><nav class="tablist" role="tablist"><button class="tab" role="tab" data-tab="tasks" aria-selected="true">依頼</button><button class="tab" role="tab" data-tab="pc" aria-selected="false">この PC</button></nav>
      <div class="lv" data-panel="tasks"><section class="list" aria-label="依頼の一覧"><div class="pane-head" data-slot="filter"></div><ul class="items" data-slot="items"></ul></section>
        <section class="detail" aria-label="依頼の詳細"><header class="d-head" data-slot="head"></header><div class="d-log scroll" data-slot="log"></div><div></div></section></div>
      <div class="page scroll" data-panel="pc" hidden></div>
    </div>
  </div></div>`;

export function renderLend(root) {
  const shell = fromHtml(SHELL);
  root.replaceChildren(shell);
  const q = (slot) => shell.querySelector(`[data-slot="${slot}"]`);
  const el = Object.fromEntries(["meters", "limits", "count", "invite", "connections", "pending", "filter", "items", "head", "log", "setup-error"].map((k) => [k, q(k)]));
  el.pc = shell.querySelector('[data-panel="pc"]');
  const view = { connections: [], tasks: [], settings: new Map(), pending: null, shared: null, overview: {}, logFor: null, pendingKey: null };
  let alive = true, reading = false, again = false, revision = 0, writing = 0;
  let writes = Promise.resolve();

  function patch(node, html, protect = false) {
    if (node._html === html || (protect && busyInside(node))) return;
    const top = node.scrollTop;
    node.innerHTML = html;
    node._html = html;
    node.scrollTop = top;
  }

  function drawInvite() {
    const open = S.invite || !view.connections.length;
    const html = open ? `<form class="block invite-form"><label class="field"><span>招待コード</span><input data-act="invite-code" value="${esc(S.code)}" autocomplete="off" required></label><div class="row"><button class="sm" type="submit">つなぐ</button>${view.connections.length ? '<button class="secondary sm" type="button" data-act="invite-close">やめる</button>' : ""}</div></form>` : "";
    patch(el.invite, html, true);
  }

  function drawLimits() {
    const limits = view.shared.settings.limits;
    const usage = view.overview.usage || {};
    patch(el.meters, meter("5時間", usage.five_hour, limits.five_hour) + meter("週間", usage.weekly, limits.weekly));
    const html = [["five_hour", "5時間の上限"], ["weekly", "週間の上限"]].map(([k, label]) => `<label class="lim"><span class="lbl">${label}</span><input class="sm" type="number" min="1" max="100" step="any" placeholder="なし" data-act="limit" data-key="${k}" value="${esc(limits[k] ?? "")}"><span>%</span></label>`).join("") +
      `<label class="lim"><span class="lbl">上限を超えたとき</span><select class="sm" data-act="force"><option value="report"${!limits.force ? " selected" : ""}>止めて報告させる</option><option value="stop"${limits.force ? " selected" : ""}>すぐ強制終了</option></select></label>`;
    patch(el.limits, html, true);
  }

  function drawCards(forceId = null) {
    const top = el.connections.scrollTop;
    const seen = new Set();
    let prev = null;
    for (const c of view.connections) {
      seen.add(c.id);
      let card = [...el.connections.children].find((n) => n.dataset.id === c.id);
      if (!card) {
        card = fromHtml(`<div class="ent" data-id="${esc(c.id)}"><div data-part="head"></div></div>`);
      }
      patch(card.firstElementChild, connectionHead(c, S.open.has(c.id), view.tasks));
      let body = card.querySelector('.ent-set');
      if (S.open.has(c.id)) {
        if (!body) { body = document.createElement("div"); body.className = "ent-set"; card.append(body); }
        const data = view.settings.get(c.id);
        const html = settingsHtml(c, data, view.tasks.filter((t) => t.connection_id === c.id).length, cardUi(c.id));
        const temp = document.createElement("div"); temp.innerHTML = html;
        // Update rows independently: typing in an invite does not freeze the other settings.
        [...temp.children].forEach((row, i) => {
          const old = body.children[i];
          if (!old) { body.append(row); return; }
          if (old.outerHTML !== row.outerHTML && (forceId === c.id || !busyInside(old))) old.replaceWith(row);
        });
      } else if (body) body.remove();
      const place = prev ? prev.nextSibling : el.connections.firstChild;
      if (card.parentNode !== el.connections || card !== place) el.connections.insertBefore(card, place);
      prev = card;
    }
    for (const card of [...el.connections.children]) if (!seen.has(card.dataset.id)) card.remove();
    el.connections.scrollTop = top;
    el.count.textContent = String(view.connections.length);
  }

  function drawPending(p) {
    const key = p ? JSON.stringify([p.connection_id, p.task_id, p.created]) : null;
    if (key !== view.pendingKey) {
      const meta = p ? pendingMeta(p) : "";
      patch(el.pending, p ? `<div class="alert multi"><span class="lz warn">${p.kind === "start" ? "新しい依頼" : "追加の指示"}</span><div class="body"><span class="pending-prompt">${esc(p.prompt)}</span><span class="cap" data-pending-meta>${esc(meta)}</span><input class="sm" data-act="reason" placeholder="拒否する理由（任意）" aria-label="拒否する理由（任意）"></div><span class="decide"><button class="sm" data-act="decide" data-yes="1">許可</button><button class="danger sm" data-act="decide" data-yes="0">拒否</button></span></div>` : "");
      view.pendingKey = key;
    } else if (p) {
      el.pending.querySelector('[data-pending-meta]').innerHTML = esc(pendingMeta(p));
    }
    view.pending = p;
    el.pending.hidden = !p;
    shell.querySelector('.work').classList.toggle("no-alerts", !p);
  }

  function drawTabs() {
    shell.querySelectorAll('[data-tab]').forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === S.tab)));
    shell.querySelector('[data-panel="tasks"]').hidden = S.tab !== "tasks";
    el.pc.hidden = S.tab !== "pc";
  }

  function filtered() { return view.tasks.filter((t) => !S.filter || t.connection_id === S.filter); }
  function drawTasks(result) {
    const options = [["", "すべての依頼主"], ...view.connections.map((c) => [c.id, c.host_name])];
    patch(el.filter, `<select class="sm" data-act="filter" aria-label="依頼主">${options.map(([id, text]) => `<option value="${esc(id)}"${S.filter === id ? " selected" : ""}>${esc(text)}</option>`).join("")}</select>`, true);
    patch(el.items, listHtml(filtered().map((t) => ({ ...t, lender: t.host_name })), S.selected, keyFor, "lend"));
    // The lender's task projection carries model choices in events rather than top-level fields.
    const models = (result?.events || []).filter((e) => e.kind === "model" && e.data && typeof e.data === "object");
    const latest = models.at(-1)?.data;
    const rerouted = models.findLast((e) => e.data.rerouted_from)?.data;
    const detail = result ? { ...result, lender: result.host_name,
      actual_model: latest?.model || result.actual_model, actual_effort: latest?.effort || result.actual_effort,
      rerouted: result.rerouted || (rerouted ? { from: rerouted.rerouted_from, reason: rerouted.reason } : null) } : null;
    patch(el.head, detail ? headHtml(detail, true, "lend") : "");
    const fresh = view.logFor !== S.selected;
    const atEnd = el.log.scrollHeight - el.log.scrollTop - el.log.clientHeight < 24;
    patch(el.log, result ? timelineHtml(result, result.events, result.progress, "lend") : "");
    if (fresh || atEnd) el.log.scrollTop = el.log.scrollHeight;
    view.logFor = S.selected;
  }

  async function cycle() {
    if (writing) return;
    const rev = revision;
    const [overview, connections, tasks, shared, pending, appState] = await Promise.all([
      get("/lend/api/overview"), get("/lend/api/connections"), get("/lend/api/tasks"), get("/lend/api/settings"), get("/lend/api/pending"),
      get("/app/api/state"),
    ]);
    const settings = await Promise.all(connections.filter((c) => S.open.has(c.id)).map(async (c) => [c.id, await get(`/lend/api/connections/${encodeURIComponent(c.id)}/settings`)]));
    if (!alive || rev !== revision) { again = alive; return; }
    Object.assign(view, { overview, connections, tasks, shared });
    for (const [id, data] of settings) view.settings.set(id, data);
    if (S.filter && !connections.some((c) => c.id === S.filter)) S.filter = "";
    const rows = filtered();
    if (!rows.some((t) => keyFor(t) === S.selected)) S.selected = keyFor(rows.find((t) => ACTIVE.includes(t.state)) || rows[0] || {});
    const selected = rows.find((t) => keyFor(t) === S.selected);
    let result = null;
    if (selected) result = await get(`/lend/api/tasks/${encodeURIComponent(selected.task_id)}?connection_id=${encodeURIComponent(selected.connection_id)}`);
    if (!alive || rev !== revision) { again = alive; return; }
    drawLimits(); drawCards(); drawInvite(); drawPending(pending); drawTabs(); drawTasks(result);
    patch(el["setup-error"], setupErrorHtml(appState.lend.error));
    patch(el.pc, pcHtml(shared, overview, S.pc), true);
    clearError("lend-read");
  }

  async function refresh() {
    if (reading) { again = true; return; }
    reading = true;
    try {
      do {
        again = false;
        try { await cycle(); } catch (e) { if (alive) showError(`貸す側の状態を読めません: ${e.message}`, "lend-read"); }
      } while (again && alive);
    } finally { reading = false; }
  }

  function write(action, button) {
    revision++; writing++;
    if (button) button.disabled = true;
    writes = writes.then(async () => {
      try { await action(); if (alive) clearError("lend-write"); }
      catch (e) { if (alive) showError(e.message, "lend-write"); }
      finally {
        writing--;
        if (button) button.disabled = false;
        if (alive && !writing) await refresh();
      }
    });
    return writes;
  }

  function saveConnection(id, patch) {
    return write(() => send("PUT", `/lend/api/connections/${encodeURIComponent(id)}/settings`, patch));
  }
  function saveShared(patch) { return write(() => send("PUT", "/lend/api/settings", patch)); }

  function click(event) {
    const b = event.target.closest('button');
    if (!b || !shell.contains(b)) return;
    const { act, id } = b.dataset;
    if (b.dataset.tab) { S.tab = b.dataset.tab; drawTabs(); return; }
    const ui = id ? cardUi(id) : null;
    if (act === "card") { S.open.has(id) ? S.open.delete(id) : S.open.add(id); revision++; drawCards(); refresh(); }
    if (act === "invite-open" || act === "invite-close") { S.invite = act === "invite-open"; el.invite._html = null; drawInvite(); }
    if (act === "replace-open" || act === "replace-cancel") { ui.replace = act === "replace-open"; ui.confirm = null; drawCards(id); }
    if (act === "ask-remove" || act === "ask-logs" || act === "cancel-confirm") { ui.confirm = act === "ask-remove" ? "remove" : act === "ask-logs" ? "logs" : null; drawCards(id); }
    if (act === "replace" && ui.code.trim()) {
      const code = ui.code.trim();
      write(async () => { await send("PUT", `/lend/api/connections/${encodeURIComponent(id)}/invite`, { code }); ui.replace = false; ui.code = ""; drawCards(id); }, b);
    }
    if (act === "remove") write(async () => { await send("DELETE", `/lend/api/connections/${encodeURIComponent(id)}`); S.open.delete(id); view.settings.delete(id); S.cards.delete(id); }, b);
    if (act === "clear-logs") write(async () => { await send("POST", `/lend/api/connections/${encodeURIComponent(id)}/logs/clear`); ui.confirm = null; }, b);
    if (act === "decide" && view.pending) {
      const p = view.pending;
      const reason = el.pending.querySelector('[data-act="reason"]').value;
      write(() => send("POST", "/lend/api/pending", { task_id: p.task_id, connection_id: p.connection_id, created: p.created, approve: b.dataset.yes === "1", reason }), b);
    }
    if (act === "workdir-open" || act === "move-open") {
      S.pc.edit = act === "workdir-open" ? "workdir" : "data";
      S.pc.workdir = view.shared.workdir; S.pc.path = view.shared.data_root;
      patch(el.pc, pcHtml(view.shared, view.overview, S.pc));
    }
    if (act === "pc-cancel") { S.pc.edit = null; patch(el.pc, pcHtml(view.shared, view.overview, S.pc)); }
    if (act === "workdir-apply" && S.pc.workdir.trim()) write(async () => { await send("PUT", "/lend/api/settings", { workdir: S.pc.workdir.trim() }); S.pc.edit = null; }, b);
    if (act === "move" && S.pc.path.trim()) write(async () => {
      await send("POST", "/lend/api/settings/data-root", { path: S.pc.path.trim() });
      S.pc.edit = null; S.pc.message = "データの場所を移しました。起動し直すのを待ってください";
      patch(el.pc, pcHtml(view.shared, view.overview, S.pc));
    }, b);
  }

  function change(event) {
    const input = event.target;
    const { act, id, key } = input.dataset;
    if (!act || (input.validity && !input.validity.valid)) { input.reportValidity?.(); return; }
    if (act === "filter") { S.filter = input.value; revision++; refresh(); return; }
    if (act === "limit") saveShared({ limits: { [key]: input.value === "" ? null : Number(input.value) } });
    if (act === "force") saveShared({ limits: { force: input.value === "stop" } });
    if (act === "retention") saveShared({ log_retention_days: Number(input.value) });
    if (!id) return;
    const data = view.settings.get(id);
    if (!data) return;
    const s = data.settings;
    const row = input.closest('.set');
    if (act === "model") {
      s.allowed_models = [...row.querySelectorAll('[data-act="model"]:checked')].map((n) => n.value);
      row.querySelector('.summary').textContent = modelSummary(data);
      saveConnection(id, { allowed_models: s.allowed_models });
    }
    if (act === "effort") saveConnection(id, { max_effort: input.value });
    if (act === "pre") {
      s.pre_approval[key] = input.checked;
      row.querySelector('.summary').textContent = preSummary(s.pre_approval);
      row.querySelector('.pre-timeout').hidden = !(s.pre_approval.new_task || s.pre_approval.followup);
      saveConnection(id, { pre_approval: { [key]: input.checked } });
    }
    if (act === "timeout") saveConnection(id, { pre_approval: { timeout_min: input.value === "" ? null : Number(input.value) } });
    if (act === "resumable" || act === "feature") {
      input.parentElement.lastElementChild.textContent = input.checked ? "オン" : "オフ";
      saveConnection(id, act === "resumable" ? { disposable: !input.checked } : { features: { [key]: input.checked } });
    }
    if (act === "log-files" || act === "log-result") {
      const files = row.querySelector('[data-act="log-files"]');
      const result = row.querySelector('[data-act="log-result"]');
      if (act === "log-files" && !files.checked) result.checked = false;
      if (act === "log-result" && result.checked) files.checked = true;
      s.log_visibility = result.checked ? "all" : files.checked ? "files" : "summary";
      row.querySelector('.summary').textContent = LOG_LABEL[s.log_visibility];
      saveConnection(id, { log_visibility: s.log_visibility });
    }
  }

  function input(event) {
    const n = event.target;
    if (n.dataset.act === "invite-code") S.code = n.value;
    if (n.dataset.act === "replace-code") cardUi(n.dataset.id).code = n.value;
    if (n.dataset.act === "workdir-path") S.pc.workdir = n.value;
    if (n.dataset.act === "data-path") S.pc.path = n.value;
    if (n.dataset.act === "model-search") {
      const search = n.value.toLowerCase();
      n.closest('.pop').querySelectorAll('[data-model]').forEach((li) => { li.hidden = !li.dataset.model.toLowerCase().includes(search); });
    }
  }

  function submit(event) {
    if (!event.target.matches('.invite-form')) return;
    event.preventDefault();
    const code = S.code.trim();
    if (code) write(async () => {
      await send("POST", "/lend/api/connections", { code });
      S.code = ""; S.invite = false; el.invite._html = null; el.invite.replaceChildren();
    }, event.target.querySelector('button[type="submit"]'));
  }
  function select(event) {
    const li = event.target.closest('.items li');
    if (!li) return;
    S.selected = li.dataset.id; revision++; refresh();
  }
  shell.addEventListener("click", click); shell.addEventListener("click", select);
  shell.addEventListener("change", change); shell.addEventListener("input", input); shell.addEventListener("submit", submit);
  const off = onChange(refresh);
  refresh();
  return () => { alive = false; off(); clearError("lend-read"); clearError("lend-write"); };
}
