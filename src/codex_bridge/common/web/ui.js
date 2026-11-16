"use strict";
/* codex-bridge: the UI parts shared by the admin page (host) and the lender's page (agent).

   Every part draws into the element it is given and can be called again whenever the data changes.
   What the person is doing is kept across those calls: text being typed, the chosen filters, the
   simple/advanced switch, scroll positions. Texts from the other side are always escaped.

   Data
     usage window   {used_percent, resets_at_local: "2026-11-15T12:00+09:00"}   (null: nothing known yet)
     mark           {value, label, kind: "host" | "lender"}        one limit line on a meter
     approval       {id, tag, meta: [text, ...], body}
     task (list)    {task_id, state, prompt, lender?, created?}
     task (detail)  {task_id, state, prompt, report, lender?, created?, finished?, thread_id?, resume_at?,
                     changed_files?, events: [{ts, kind, data}], event_count?, progress?}
     event kinds    agent_message{text}   plan{steps: [{step, status}]}   reasoning_summary{text}
                    tool_started / tool_finished{tool, arguments, duration_ms, success, result_summary}
                    tool_call{tool, arguments, status} (v0.1)   token_usage{input, output}
                    progress{text}: the message being written; shown under "working", never in the record
                    (a task may carry it as task.progress instead)
                    anything else (started, status, turn_completed, error, ...) is a change of state
*/
const UI = (() => {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // Redraw only when something changed, so that scrolling and hovering are not disturbed.
  const setHtml = (el, html) => { if (el._html !== html) { el.innerHTML = html; el._html = html; } };
  const setText = (el, s) => { if (el.textContent !== s) el.textContent = s; };  // keeps a selection being copied
  const own = (map, key) => (typeof key === "string" && Object.hasOwn(map, key) ? map[key] : undefined);

  // ---- words and times ----------------------------------------------------
  const STATE_JA = { queued: "受付", running: "作業中", completed: "完了", suspended_quota: "中断（枠切れ）",
    failed: "失敗", cancelled: "中止", agent_offline: "接続待ち",
    awaiting_lender: "貸している人の承認待ち", rejected_by_lender: "貸している人が拒否" };
  const ACTIVE_STATES = ["queued", "running", "agent_offline", "awaiting_lender"];

  function stateBadge(state) {
    const word = own(STATE_JA, state);
    return `<span class="state ${word ? state : ""}">${esc(word || state)}</span>`;
  }

  const pad = (n) => String(n).padStart(2, "0");
  const isTime = (ts) => typeof ts === "number" && isFinite(ts);
  // "07:42" for today, "11/15 07:42" for another day, "" when not known.
  function fmtTime(ts) {
    if (!isTime(ts)) return "";
    const d = new Date(ts * 1000);
    const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    return d.toDateString() === new Date().toDateString() ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
  }
  function fmtClock(ts) {
    if (!isTime(ts)) return "";
    const d = new Date(ts * 1000);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  // "2026-11-15T12:00+09:00" (already local) -> "11/15 12:00"
  const fmtIso = (iso) => (typeof iso === "string" && iso.length >= 16
    ? `${+iso.slice(5, 7)}/${+iso.slice(8, 10)} ${iso.slice(11, 16)}` : "");
  function fmtDuration(sec) {
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h ? `${h} 時間 ${m} 分` : m ? `${m} 分 ${s} 秒` : `${s} 秒`;
  }

  // ---- notification -------------------------------------------------------
  function askNotify() {
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
  }
  function notify(title, body) {
    if ("Notification" in window && Notification.permission === "granted") new Notification(title, { body });
  }

  // ---- usage meter --------------------------------------------------------
  // el: <div data-label="5 時間枠">. Over the lowest line the bar changes colour and says until when.
  function meter(el, w, marks = []) {
    const lines = marks.filter((m) => typeof m.value === "number" && m.value > 0 && m.value <= 100);
    const kind = (m) => (m.kind === "lender" ? "lender" : "host");
    const pct = w ? Math.min(100, Math.max(0, w.used_percent)) : 0;
    const hot = !!w && lines.length > 0 && pct >= Math.min(...lines.map((m) => m.value));
    const back = w ? esc(fmtIso(w.resets_at_local)) : "";
    el.classList.add("meter");
    el.classList.toggle("hot", hot);
    setHtml(el, `
      <div class="m-label">${esc(el.dataset.label)}</div>
      <div class="m-bar"><div class="m-track"><div class="m-fill" style="width:${pct}%"></div></div>${
        lines.map((m) => `<div class="m-mark ${kind(m)}" style="left:${m.value}%" title="${esc(m.label)} ${m.value}%"></div>`).join("")}</div>
      <div class="m-val">${w ? `${Math.round(pct)}%${back ? `・${back} に戻る` : ""}` : "—"}</div>
      ${lines.length ? `<div class="m-note">${
        hot ? `<span class="m-stop">止めています${back ? `（${back} まで）` : ""}</span>` : ""}${
        lines.map((m) => `<span class="m-key ${kind(m)}">${esc(m.label)} ${m.value}%</span>`).join("")}</div>` : ""}`);
  }

  // ---- approvals ----------------------------------------------------------
  // el: <section class="card" data-title="承認待ち">. onDecide(id, approve).
  function approvals(el, items, onDecide) {
    const title = el.dataset.title || "承認待ち";
    el.classList.add("approvals");
    el.classList.toggle("has", items.length > 0);
    setHtml(el, `<h2>${esc(title)} <span class="count">${items.length}</span></h2>` + (items.map((a) => `
      <div class="ap"><div class="ap-what">
          <span class="small">${[a.tag, ...(a.meta || [])].filter(Boolean).map(esc).join("・")}</span>
          <code class="ap-body">${esc(a.body)}</code></div>
        <button type="button" class="ok" data-ap="${esc(a.id)}" data-ok="1">許可</button>
        <button type="button" class="bad" data-ap="${esc(a.id)}" data-ok="0">拒否</button></div>`).join("")
      || `<p class="empty">${esc(title)}はありません。</p>`));
    el.onclick = (ev) => {
      const b = ev.target.closest("button[data-ap]");
      if (!b) return;
      b.parentElement.querySelectorAll("button").forEach((x) => { x.disabled = true; });  // one answer per item
      el._html = null;  // the next call draws it again, whatever became of the answer
      onDecide(b.dataset.ap, b.dataset.ok === "1");
    };
    const seen = el._seen || (el._seen = new Set());
    const fresh = items.filter((a) => !seen.has(a.id));
    fresh.forEach((a) => seen.add(a.id));
    if (fresh.length) notify(`codex-bridge: ${title}`, fresh[0].body);
  }

  // ---- task list ----------------------------------------------------------
  function taskList(el, tasks, selectedId, onSelect) {
    el.classList.add("tasks");
    setHtml(el, tasks.map((t) => `
      <div class="t-item${t.task_id === selectedId ? " sel" : ""}" data-id="${esc(t.task_id)}" tabindex="0" title="${esc(t.task_id)}">
        <div class="t-top">${stateBadge(t.state)}${t.lender ? `<span class="t-who">${esc(t.lender)}</span>` : ""}
          <span class="t-when">${fmtTime(t.created)}</span></div>
        <div class="t-prompt">${esc(t.prompt)}</div></div>`).join("")
      || '<p class="empty">まだタスクはありません。</p>');
    const pick = (ev) => { const item = ev.target.closest(".t-item"); if (item) onSelect(item.dataset.id); };
    el.onclick = pick;
    el.onkeydown = (ev) => { if (ev.key === "Enter") pick(ev); };
  }

  // ---- task detail --------------------------------------------------------
  // The kinds of lines in the record. The simple view shows the first three (and only the latest plan).
  const KINDS = [{ id: "say", label: "発言", simple: true }, { id: "plan", label: "計画", simple: true },
    { id: "state", label: "状態", simple: true }, { id: "tool", label: "道具" },
    { id: "think", label: "考えの要約" }, { id: "tokens", label: "トークン" }];
  const KIND_JA = Object.fromEntries(KINDS.map((k) => [k.id, k.label]));
  const TURN_JA = { completed: "完了", interrupted: "中断", failed: "失敗" };
  const PLAN_MARK = { done: "✔", doing: "▶", todo: "・" };
  const WAITING = { queued: "Codex が始めるのを待っています。",
    awaiting_lender: "Codex を貸している人が、この依頼を許可するのを待っています。",
    agent_offline: "Codex を貸している人との接続が切れています。つながると、続きが届きます。" };

  function text(v, max = 400) {
    const s = typeof v === "string" ? v : v == null ? "" : JSON.stringify(v);
    return s.length > max ? s.slice(0, max) + "…" : s;
  }
  const count = (v) => (typeof v === "number" ? v.toLocaleString("ja-JP") : "?");
  const took = (ms) => (typeof ms !== "number" ? "" : ms < 1000 ? `（${Math.round(ms)} ミリ秒）` : `（${(ms / 1000).toFixed(1)} 秒）`);
  const planSteps = (data) => (Array.isArray(data && data.steps) ? data.steps : []).map((s) => ({
    step: text(s && s.step),
    cls: !s ? "todo" : s.status === "completed" ? "done" : /^in.?progress$/i.test(s.status) ? "doing" : "todo" }));

  // One event -> one line of the record: {kind, text, bad?}
  function line(e) {
    const d = e.data && typeof e.data === "object" ? e.data : {};
    switch (e.kind) {
      case "task_prompt": return { kind: "say", text: `依頼・追加の指示: ${text(d.text, 8000)}` };
      case "final_report": return { kind: "say", text: text(d.text, 8000) };
      case "state": return { kind: "state", text: own(STATE_JA, d.state) || text(d.state) };
      case "agent_message": return { kind: "say", text: text(d.text, 8000) };
      case "plan": {
        const steps = planSteps(d);
        return { kind: "plan", text: [`${steps.length} 件のうち ${steps.filter((s) => s.cls === "done").length} 件が済み`,
          ...steps.map((s) => `${PLAN_MARK[s.cls]} ${s.step}`)].join("\n") };
      }
      case "reasoning_summary": return { kind: "think", text: text(d.text, 4000) };
      case "tool_started": return { kind: "tool", text: `開始: ${text(d.tool)} ${text(d.arguments)}`.trim() };
      case "tool_finished": return { kind: "tool", bad: d.success === false,
        text: `${d.success === false ? "失敗" : "終了"}: ${text(d.tool)} ${text(d.arguments)}`.trim() + took(d.duration_ms)
          + (d.result_summary ? `\n→ ${text(d.result_summary, 1000)}` : "") };
      case "tool_call": return { kind: "tool", bad: d.status === "failed",
        text: `${text(d.tool)} ${text(d.arguments)}`.trim() + (d.status ? `（${text(d.status)}）` : "") };
      case "token_usage": return { kind: "tokens", text: `入力 ${count(d.input)}・出力 ${count(d.output)}` };
      case "started": return { kind: "state", text: "作業を始めました" };
      case "turn_completed": return { kind: "state", bad: d.status === "failed",
        text: `区切り（${own(TURN_JA, d.status) || text(d.status) || "終了"}）` };
      case "status": return { kind: "state", text: text(d.text || d.status, 2000) };
      default: return { kind: "state", bad: e.kind === "error",
        text: [text(e.kind), text(d.text || d.message || d.status, 2000)].filter(Boolean).join(": ") };
    }
  }

  const SKELETON = `
    <div class="td-head"><h2>タスクの詳細</h2><span data-td="badge"></span><span class="td-id" data-td="id"></span>
      <div class="seg" data-td="seg" title="簡易: 発言・計画・状態　詳細: 道具の呼び出し・考えの要約・トークンも">
        <button type="button" data-mode="simple">簡易</button><button type="button" data-mode="advanced">詳細</button></div></div>
    <div class="td-sum" data-td="sum"></div>
    <dl class="kv" data-td="meta"></dl>
    <h3>依頼文</h3><div class="report"><div class="td-text" data-td="prompt"></div></div>
    <button type="button" class="link" data-td="prompt-more" aria-expanded="false">すべて表示</button>
    <details class="working" data-td="working"><summary>作業中の様子</summary><div data-td="working-body"></div></details>
    <div class="followup" data-td="actions">
      <input data-td="fu" placeholder="作業中の Codex への追加の指示">
      <button type="button" class="mini" data-td="fu-send">送る</button>
      <button type="button" class="bad mini" data-td="cancel">中止</button></div>
    <h3>最終報告</h3><div class="report"><div class="td-text" data-td="report"></div></div>
    <button type="button" class="link" data-td="report-more" aria-expanded="false">すべて表示</button>
    <div class="row" data-td="undo-row"><button type="button" class="ghost" data-td="undo">このタスクのファイル変更を取り消す</button></div>
    <h3>記録（新しい順）<span class="small" data-td="count"></span></h3>
    <div class="td-filter" data-td="filter"><span class="chips" data-td="chips"></span>
      <input type="search" data-td="q" placeholder="記録を検索"></div>
    <div class="events" data-td="events"></div>
    <button type="button" class="ghost mini" data-td="more">さらに表示</button>`;

  // opts: {mode: "simple" | "advanced" (the view to start with), onMode?(mode), showLender?,
  //        onFollowup?(text) -> promise (the text is cleared when it resolves), onCancel?(), onUndo?()}
  function taskDetail(el, task, opts = {}) {
    el.classList.add("task-detail");
    if (!task) {
      el._td = null;
      setHtml(el, '<h2>タスクの詳細</h2><p class="empty">左の一覧から選んでください。</p>');
      return;
    }
    let st = el._td;
    if (!st || st.id !== task.task_id) {  // another task: new skeleton, filters back to "everything"
      st = el._td = { id: task.task_id, mode: (st && st.mode) || (opts.mode === "advanced" ? "advanced" : "simple"),
        off: new Set(), q: "", visible: 20, expanded: new Set() };
      el.scrollTop = 0;
      el.innerHTML = SKELETON;
      el._html = null;
      wireDetail(el);
    }
    st.task = task;
    st.opts = opts;
    drawDetail(el);
  }

  function wireDetail(el) {
    const part = (name) => el.querySelector(`[data-td="${name}"]`);
    part("seg").onclick = (ev) => {
      const b = ev.target.closest("button[data-mode]");
      if (!b || el._td.mode === b.dataset.mode) return;
      el._td.mode = b.dataset.mode;
      el._td.visible = 20;
      if (el._td.opts.onMode) el._td.opts.onMode(el._td.mode);
      drawDetail(el);
    };
    part("chips").onclick = (ev) => {
      const b = ev.target.closest("button[data-kind]");
      if (!b) return;
      const off = el._td.off;
      off.has(b.dataset.kind) ? off.delete(b.dataset.kind) : off.add(b.dataset.kind);
      el._td.visible = 20;
      drawDetail(el);
    };
    part("q").oninput = (ev) => { el._td.q = ev.target.value; el._td.visible = 20; drawDetail(el); };
    for (const name of ["prompt", "report"]) {
      part(`${name}-more`).onclick = () => {
        const expanded = el._td.expanded;
        expanded.has(name) ? expanded.delete(name) : expanded.add(name);
        drawDetail(el);
      };
    }
    part("more").onclick = () => { el._td.visible += 20; drawDetail(el); };
    const send = async () => {
      const value = part("fu").value.trim();
      if (!value || !el._td.opts.onFollowup) return;
      const id = el._td.id;
      try {
        await el._td.opts.onFollowup(value);
        if (el._td && el._td.id === id) part("fu").value = "";
      } catch (e) { /* the page has said what went wrong; what was typed stays */ }
    };
    part("fu-send").onclick = send;
    part("fu").onkeydown = (ev) => { if (ev.key === "Enter" && !ev.isComposing) send(); };
    part("cancel").onclick = () => el._td.opts.onCancel && el._td.opts.onCancel();
    part("undo").onclick = () => el._td.opts.onUndo && el._td.opts.onUndo();
  }

  function drawDetail(el) {
    const { task: t, opts, mode, off, q, visible, expanded } = el._td;
    const part = (name) => el.querySelector(`[data-td="${name}"]`);
    const active = ACTIVE_STATES.includes(t.state);
    const events = Array.isArray(t.events) ? t.events : [];

    setHtml(part("badge"), stateBadge(t.state));
    setText(part("id"), t.task_id);
    part("seg").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));

    // heading: who, from when to when, how long
    const sum = [];
    if (opts.showLender) sum.push(["担当", esc(t.lender || "—")]);
    sum.push(["開始", fmtTime(t.created) || "—"]);
    sum.push(["終了", fmtTime(t.finished) || "—"]);
    if (isTime(t.created) && (isTime(t.finished) || active)) {
      sum.push([isTime(t.finished) ? "かかった時間" : "経過", fmtDuration((isTime(t.finished) ? t.finished : Date.now() / 1000) - t.created)]);
    }
    setHtml(part("sum"), sum.map(([k, v]) => `<span><b>${k}</b>${v}</span>`).join(""));
    const rows = [];
    if (t.resume_at) rows.push(["再開できる時刻", esc(typeof t.resume_at === "number" ? fmtTime(t.resume_at) : fmtIso(t.resume_at) || t.resume_at)]);
    if (Array.isArray(t.changed_files)) rows.push(["変更したファイル", t.changed_files.map(esc).join("<br>") || "—"]);
    if (mode === "advanced") rows.push(["セッション", esc(t.thread_id || "—")]);
    setHtml(part("meta"), rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join(""));

    setText(part("prompt"), t.prompt || "");
    setText(part("report"), t.report || "（まだありません）");
    for (const name of ["prompt", "report"]) {
      const content = part(name), button = part(`${name}-more`), open = expanded.has(name);
      content.classList.toggle("expanded", open);
      button.hidden = !open && content.scrollHeight <= content.clientHeight;
      button.setAttribute("aria-expanded", String(open));
      setText(button, open ? "折りたたむ" : "すべて表示");
    }

    // working: the latest plan and the message being written
    const plan = events.findLast((e) => e.kind === "plan");
    const lastProgress = events.findLast((e) => e.kind === "progress");
    const writing = text(t.progress && typeof t.progress === "object" ? t.progress.text
      : t.progress || (lastProgress && lastProgress.data && lastProgress.data.text), 8000);
    const waiting = own(WAITING, t.state);
    const steps = plan ? planSteps(plan.data) : [];
    if (el._td.workingState !== t.state) {
      part("working").open = t.state === "running";
      el._td.workingState = t.state;
    }
    setHtml(part("working-body"), `
      ${waiting ? `<p class="empty">${esc(waiting)}</p>` : ""}
      ${steps.length ? `<div class="w-label">計画</div><ul class="plan">${steps.map((s) =>
        `<li class="${s.cls}"><span class="p-mark">${PLAN_MARK[s.cls]}</span><span>${esc(s.step)}</span></li>`).join("")}</ul>` : ""}
      ${writing ? `<div class="w-label">書きかけの発言</div><div class="progress">${esc(writing)}</div>` : ""}
      ${!waiting && !steps.length && !writing ? '<p class="empty">まだ途中経過は届いていません。</p>' : ""}`);
    const live = part("working").querySelector(".progress");
    if (live) live.scrollTop = live.scrollHeight;  // the newest words are at the end

    part("actions").hidden = !(active && (opts.onFollowup || opts.onCancel));
    part("fu").hidden = part("fu-send").hidden = !opts.onFollowup;
    part("cancel").hidden = !opts.onCancel;
    part("undo-row").hidden = !opts.onUndo;
    part("undo").disabled = active;

    // record
    let lines = events.filter((e) => e.kind !== "progress").map((e) => ({ ...line(e), ts: e.ts }));
    if (mode === "simple") {
      const latestPlan = lines.findLastIndex((l) => l.kind === "plan");
      lines = lines.filter((l, i) => KINDS.find((k) => k.id === l.kind).simple && (l.kind !== "plan" || i === latestPlan));
    }
    setHtml(part("chips"), KINDS.filter((k) => mode === "advanced" || k.simple).map((k) =>
      `<button type="button" data-kind="${k.id}" class="${off.has(k.id) ? "" : "on"}">${k.label} ${
        lines.filter((l) => l.kind === k.id).length}</button>`).join(""));
    part("filter").hidden = !lines.length;
    const needle = q.trim().toLowerCase();
    const shown = lines.filter((l) => !off.has(l.kind) && (!needle || l.text.toLowerCase().includes(needle))).reverse();
    setText(part("count"), ` ${Math.min(visible, shown.length)} / ${shown.length} 件` + (t.event_count > events.length
      ? `（全 ${t.event_count} 件のうち、新しい ${events.length} 件から）` : ""));
    part("more").hidden = visible >= shown.length;
    setHtml(part("events"), shown.slice(0, visible).map((l) => `
      <div class="ev k-${l.kind}${l.bad ? " bad" : ""}"><span class="ev-t">${fmtClock(l.ts)}</span>
        <span class="ev-k">${KIND_JA[l.kind]}</span><span class="ev-x">${esc(l.text)}</span></div>`).join("")
      || `<p class="empty">${lines.length ? "条件に合う記録はありません。" : "まだ記録はありません。"}</p>`);
  }

  // ---- choice cards -------------------------------------------------------
  // options: [{value, title, desc}]. onChange(value) when the person picks another one.
  function choiceCards(el, name, options, value, onChange) {
    el.classList.add("choices");
    setHtml(el, options.map((o) => `
      <label><input type="radio" name="${esc(name)}" value="${esc(o.value)}"><b>${esc(o.title)}</b><span>${esc(o.desc)}</span></label>`).join(""));
    el.querySelectorAll("input").forEach((i) => { i.checked = i.value === value; });
    el.onchange = (ev) => { if (onChange) onChange(ev.target.value); };
  }

  return { esc, STATE_JA, ACTIVE_STATES, stateBadge, fmtTime, fmtIso, fmtDuration, askNotify, notify,
    meter, approvals, taskList, taskDetail, choiceCards };
})();
