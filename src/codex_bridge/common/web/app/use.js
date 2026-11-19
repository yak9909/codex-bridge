// The use tab: the lenders on the left, approvals / requests / shared folders / allowlist on the right.
// Every part is drawn into a container that stays in the page, and a refresh only writes what changed and is not being used
// (a focused field, an open list), so scroll positions, open cards, the chosen request and typed text survive it.
import { get, send, onChange, showError, clearError, esc, ago } from "/app/static/api.js";
import { icon, codeFieldHtml, busyInside, taskTitle, ACTIVE } from "/app/static/common.js";
import { lenderParts } from "/app/static/use-lenders.js";
import { listHtml, newHeadHtml, headHtml, promptHtml, timelineHtml, pickerOptions } from "/app/static/use-tasks.js";
import { emptyHtml, foldersHtml, allowHtml, policyWarningsHtml } from "/app/static/use-pages.js";

// what the user has chosen; it outlives the tab being left and entered again
const S = {
  open: new Set(),
  cards: new Map(), // lender name -> { code, confirm }
  selected: null, // a task id, "new", or null (not chosen yet)
  rightTab: "tasks",
  inviteOpen: false,
  inviteName: "",
  inviteCode: null,
  drafts: new Map(), // composer text per request ("new" or a task id)
  pick: { lender: "", model: "", effort: "" },
  folderMessage: "",
};

const TABS = [["tasks", "依頼"], ["folders", "共有フォルダ"], ["allow", "許可リスト"]];
const norm = (p) => String(p).replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
const cardUi = (name) => {
  if (!S.cards.has(name)) S.cards.set(name, { code: null, confirm: false });
  return S.cards.get(name);
};
const fromHtml = (html) => {
  const t = document.createElement("template");
  t.innerHTML = html;
  return t.content.firstElementChild;
};

const SHELL = `
  <div class="shell">
    <aside class="pane side" aria-label="貸す人">
      <div class="pane-head"><h2>貸す人</h2><span class="count" data-slot="count">0</span><span class="spacer"></span>
        <button class="secondary sm" data-act="invite-open">${icon("key-round")}招待</button></div>
      <div data-slot="invite"></div>
      <div class="pane-body scroll" data-slot="lenders"></div>
    </aside>
    <div class="work">
      <div data-slot="notices" hidden>
        <div data-slot="policy-warnings" aria-label="設定の警告"></div>
        <div class="alerts" data-slot="approvals" aria-label="承認待ち" hidden></div>
      </div>
      <div class="surface">
        <nav class="tablist" role="tablist">${TABS.map(([k, t]) => `<button class="tab" role="tab" data-tab="${k}" aria-selected="false">${t}</button>`).join("")}</nav>
        <div class="lv" data-panel="tasks">
          <section class="list" aria-label="依頼の一覧">
            <div class="pane-head"><button class="sm" data-act="new">${icon("send")}新しい依頼</button></div>
            <ul class="items" data-slot="items"></ul>
          </section>
          <section class="detail" aria-label="依頼の詳細">
            <header class="d-head" data-slot="dhead"></header>
            <div class="d-req scroll" data-slot="dreq"></div>
            <div class="d-log scroll" data-slot="dlog"></div>
            <form class="composer"><textarea rows="2" data-slot="text"></textarea><div class="composer-bar" data-slot="bar"></div></form>
          </section>
        </div>
        <div class="page scroll" data-panel="folders" hidden></div>
        <div class="page scroll" data-panel="allow" hidden></div>
      </div>
    </div>
  </div>`;

export function renderUse(root) {
  const shell = fromHtml(SHELL);
  const emptyEl = document.createElement("div");
  emptyEl.style.display = "contents";
  const q = (slot) => shell.querySelector(`[data-slot="${slot}"]`);
  const el = {
    count: q("count"), invite: q("invite"), lenders: q("lenders"), approvals: q("approvals"), items: q("items"),
    dhead: q("dhead"), dreq: q("dreq"), dlog: q("dlog"), text: q("text"), bar: q("bar"), form: shell.querySelector("form.composer"),
    folders: shell.querySelector('[data-panel="folders"]'), allow: shell.querySelector('[data-panel="allow"]'),
    tasks: shell.querySelector('[data-panel="tasks"]'), notices: q("notices"), warnings: q("policy-warnings"),
  };
  const cache = { warnings: null, approvals: null, items: null, dhead: null, dreq: null, dlog: null, bar: null, folders: null, allow: null, empty: null };
  const view = { mode: null, sel: null, selKey: undefined, lenders: [], tasks: [], logFor: null };
  let alive = true;
  let busy = false;
  let again = false;

  // ---- small drawing helpers ----
  function setMode(mode) {
    if (view.mode === mode) return;
    view.mode = mode;
    root.replaceChildren(mode === "shell" ? shell : emptyEl);
  }

  function keepFocus(oldNode, newNode, a = document.activeElement) {
    if (!a || !oldNode.contains(a)) return;
    const sel = ["data-act", "data-name", "data-path", "data-id", "data-pick"].filter((k) => a.hasAttribute(k)).map((k) => `[${k}="${CSS.escape(a.getAttribute(k))}"]`).join("");
    const target = sel ? newNode.querySelector(sel) || (newNode.matches(sel) ? newNode : null) : null;
    if (target) target.focus();
  }

  function patchCard(card, parts) {
    const order = ["head", "sec", "set"];
    for (const slot of order) {
      const html = parts[slot];
      if (card._h[slot] === html) continue;
      const old = card._els[slot];
      if (slot === "set" && old && busyInside(old)) continue; // being edited: try again next time
      card._h[slot] = html;
      if (!html) {
        if (old) old.remove();
        card._els[slot] = null;
        continue;
      }
      const node = fromHtml(html);
      if (old) {
        const active = document.activeElement;
        old.replaceWith(node);
        keepFocus(old, node, active);
      }
      card._els[slot] = node;
    }
    const want = order.map((s) => card._els[s]).filter(Boolean);
    if (want.some((n, i) => card.children[i] !== n) || card.children.length !== want.length) card.append(...want);
  }

  function drawLenders() {
    const tasks = new Map(view.tasks.map((t) => [t.task_id, t]));
    const seen = new Set();
    let prev = null;
    for (const l of view.lenders) {
      seen.add(l.name);
      let card = [...el.lenders.children].find((c) => c.dataset.name === l.name);
      if (!card) {
        card = document.createElement("div");
        card.className = "ent";
        card.dataset.name = l.name;
        card._h = {};
        card._els = {};
      }
      const ui = { ...cardUi(l.name), open: S.open.has(l.name) };
      patchCard(card, lenderParts(l, ui, tasks));
      const place = prev ? prev.nextSibling : el.lenders.firstChild;
      if (card.parentNode !== el.lenders || place !== card) el.lenders.insertBefore(card, place);
      prev = card;
    }
    for (const c of [...el.lenders.children]) if (!seen.has(c.dataset.name)) c.remove();
    el.count.textContent = String(view.lenders.length);
  }

  function syncNotices() {
    const visible = !!cache.warnings || !el.approvals.hidden;
    el.notices.hidden = !visible;
    shell.querySelector(".work").classList.toggle("no-alerts", !visible);
  }

  function drawPolicyWarnings(overview) {
    const html = policyWarningsHtml(overview);
    // Preserve the focused removal button while the same warning is refreshed.
    if (html !== cache.warnings) {
      cache.warnings = html;
      el.warnings.innerHTML = html;
    }
    syncNotices();
  }

  function drawApprovals(list) {
    const byTask = new Map(view.tasks.map((t) => [t.task_id, t]));
    const now = Date.now() / 1000;
    const html = list.map((a) => {
      const t = byTask.get(a.task_id);
      const title = t ? taskTitle(t) : "";
      const meta = [t && t.lender, ago(now - (a.age_sec || 0))].filter(Boolean).join("・");
      return `<div class="alert multi"><span class="lz warn">${a.kind === "command" ? "コマンド実行" : "削除"}</span><div class="body approval-body"><code>${esc(a.summary)}</code>` +
        `<span class="pending-prompt" tabindex="0">${esc(title)}</span><span class="cap">${esc(meta)}</span></div><span class="decide"><button class="sm" data-act="approve" data-id="${esc(a.id)}" data-yes="1">許可</button>` +
        `<button class="danger sm" data-act="approve" data-id="${esc(a.id)}" data-yes="0">拒否</button></span></div>`;
    }).join("");
    el.approvals.hidden = !list.length;
    syncNotices();
    if (html !== cache.approvals) {
      cache.approvals = html;
      el.approvals.innerHTML = html;
    }
  }

  function drawTabs() {
    shell.querySelectorAll(".surface > .tablist .tab").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === S.rightTab)));
    el.tasks.hidden = S.rightTab !== "tasks";
    el.folders.hidden = S.rightTab !== "folders";
    el.allow.hidden = S.rightTab !== "allow";
  }

  function drawItems() {
    const html = listHtml(view.tasks, S.selected);
    if (html === cache.items) return;
    const top = el.items.scrollTop;
    cache.items = html;
    el.items.innerHTML = html;
    el.items.scrollTop = top;
  }

  function drawComposer(force = false) {
    const key = S.selected;
    if (view.selKey !== key) {
      if (view.selKey !== undefined) S.drafts.set(view.selKey, el.text.value);
      view.selKey = key;
      el.text.value = S.drafts.get(key) || "";
      cache.bar = null;
    }
    const isNew = key === "new";
    el.text.rows = isNew ? 4 : 2;
    el.text.placeholder = isNew
      ? "例: analysis/report.md を読み、要点を 5 行で analysis/summary.md に書いてください。"
      : "追加の指示";
    let html;
    if (isNew) {
      const picked = pickerOptions(view.lenders, S.pick);
      S.pick = picked.pick;
      html = `${picked.html}<span class="spacer"></span><button class="sm" type="submit">${icon("send")}依頼する</button>`;
    } else {
      html = `<span class="spacer"></span><button class="sm" type="submit">${icon("send")}送る</button>`;
    }
    if (html !== cache.bar && (force || !busyInside(el.bar))) {
      const old = el.bar;
      const active = document.activeElement;
      const bar = old.cloneNode(false);
      bar.innerHTML = html;
      cache.bar = html;
      old.replaceWith(bar);
      el.bar = bar;
      keepFocus(old, bar, active);
    }
  }

  function drawDetail(result) {
    const isNew = S.selected === "new";
    const head = isNew ? newHeadHtml() : result ? headHtml(result) : "";
    if (head !== cache.dhead) {
      cache.dhead = head;
      el.dhead.innerHTML = head;
    }
    const req = isNew || !result ? "" : promptHtml(result);
    if (req !== cache.dreq || view.logFor !== S.selected) {
      if (req !== cache.dreq) el.dreq.innerHTML = req;
      cache.dreq = req;
      if (view.logFor !== S.selected) el.dreq.scrollTop = 0;
    }
    const body = isNew || !result ? "" : timelineHtml(result, result.events, result.progress);
    const fresh = view.logFor !== S.selected;
    if (body !== cache.dlog || fresh) {
      const log = el.dlog;
      const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
      const top = log.scrollTop;
      cache.dlog = body;
      log.innerHTML = body;
      log.scrollTop = fresh || atEnd ? log.scrollHeight : top;
    }
    view.logFor = S.selected;
    drawComposer();
  }

  async function drawFolders() {
    if (S.rightTab !== "folders") return;
    const form = await get("/admin/api/policy-form");
    const html = foldersHtml(form, S.folderMessage);
    if (html !== cache.folders && !busyInside(el.folders)) {
      cache.folders = html;
      const top = el.folders.scrollTop;
      el.folders.innerHTML = html;
      el.folders.scrollTop = top;
    }
  }

  async function drawAllow() {
    if (S.rightTab !== "allow") return;
    const form = await get("/admin/api/policy-form");
    const html = allowHtml(form);
    if (html !== cache.allow && !busyInside(el.allow)) {
      cache.allow = html;
      el.allow.innerHTML = html;
    }
  }

  function drawInvite() {
    if (!S.inviteOpen) {
      el.invite.innerHTML = "";
      return;
    }
    const code = S.inviteCode ? codeFieldHtml(S.inviteCode) : "";
    el.invite.innerHTML = `<div class="block"><h2>貸す人を招待</h2><label class="field"><span>名前</span>` +
      `<input data-slot="invite-name" placeholder="例: kimura" value="${esc(S.inviteName)}"></label>` +
      `<div class="row"><button class="sm" data-act="invite-issue">招待コードを発行</button><button class="secondary sm" data-act="invite-close">やめる</button></div>${code}</div>`;
  }

  // ---- reading ----
  async function cycle() {
    let overview;
    try {
      overview = await get("/admin/api/overview");
      clearError("use-read");
    } catch (e) {
      showError("使う側の状態を読めません", "use-read");
      return;
    }
    if (!alive) return;
    if (!overview || overview.configured === false || overview.running === false || overview.error) {
      setMode("empty");
      const html = emptyHtml(overview && overview.error, overview && overview.configured === true && overview.running === false);
      if (html !== cache.empty) {
        cache.empty = html;
        emptyEl.innerHTML = html;
      }
      return;
    }
    setMode("shell");
    drawPolicyWarnings(overview);
    let lenders, approvals, tasks;
    try {
      [lenders, approvals, tasks] = await Promise.all([
        get("/admin/api/lenders"), get("/admin/api/approvals"), get("/admin/api/tasks"),
      ]);
    } catch (e) {
      showError(`使う側の状態を読めません: ${e.message}`, "use-read");
      return;
    }
    if (!alive) return;
    view.lenders = Array.isArray(lenders) ? lenders : [];
    view.tasks = Array.isArray(tasks) ? tasks : [];
    if (S.selected !== "new" && !view.tasks.some((t) => t.task_id === S.selected)) {
      const first = view.tasks.find((t) => ACTIVE.includes(t.state)) || view.tasks[0];
      S.selected = first ? first.task_id : "new";
    }
    let result = null;
    if (S.selected !== "new") {
      try {
        result = await get(`/admin/api/tasks/${encodeURIComponent(S.selected)}`);
      } catch (e) {
        result = null;
      }
    }
    if (!alive) return;
    drawTabs();
    drawLenders();
    drawApprovals(Array.isArray(approvals) ? approvals : []);
    drawItems();
    drawDetail(result);
    view.sel = result;
    try {
      await drawFolders();
      await drawAllow();
    } catch (e) {
      showError(`設定を読めません: ${e.message}`, "use-read");
    }
  }

  async function refresh() {
    if (busy) {
      again = true;
      return;
    }
    busy = true;
    try {
      do {
        again = false;
        await cycle();
      } while (again && alive);
    } finally {
      busy = false;
    }
  }

  // run a write, show its error, and read again either way
  async function write(fn) {
    try {
      await fn();
      clearError("use-write");
    } catch (e) {
      showError(e.message, "use-write");
    }
    await refresh();
  }

  async function editForm(mutate) {
    const form = await get("/admin/api/policy-form");
    mutate(form);
    await send("PUT", "/admin/api/policy-form", form);
  }

  async function copy(button) {
    const field = button.previousElementSibling;
    try {
      await navigator.clipboard.writeText(field.value);
      clearError("use-copy");
    } catch (e) {
      field.focus();
      field.select();
      showError("コピーできませんでした。選択された文字を Ctrl+C でコピーしてください", "use-copy");
      return;
    }
    const before = button.innerHTML;
    button.textContent = "コピーしました";
    setTimeout(() => { if (button.isConnected) button.innerHTML = before; }, 1500);
  }

  // ---- clicks ----
  async function onClick(ev) {
    const tab = ev.target.closest("[data-tab]");
    if (tab && shell.contains(tab)) {
      S.rightTab = tab.dataset.tab;
      cache.folders = cache.allow = null;
      drawTabs();
      refresh();
      return;
    }
    const li = ev.target.closest("li[data-id]");
    if (li && el.items.contains(li)) {
      S.selected = li.dataset.id;
      cache.items = null;
      refresh();
      return;
    }
    const b = ev.target.closest("[data-act]");
    if (!b || b.matches("select, input, textarea")) return;
    const act = b.dataset.act;
    const name = b.dataset.name;
    switch (act) {
      case "setup":
        try {
          await send("POST", "/app/api/host/setup");
          clearError("use-setup");
        } catch (e) {
          showError(e.message, "use-setup");
        }
        break;
      case "toggle":
        if (S.open.has(name)) S.open.delete(name);
        else S.open.add(name);
        drawLenders();
        break;
      case "invite-open":
        S.inviteOpen = true;
        drawInvite();
        el.invite.querySelector("input")?.focus();
        break;
      case "invite-close":
        S.inviteOpen = false;
        S.inviteCode = null;
        S.inviteName = "";
        drawInvite();
        break;
      case "invite-issue":
        await issueInvite();
        break;
      case "copy":
        await copy(b);
        break;
      case "reissue":
        await write(async () => {
          const r = await send("POST", `/admin/api/invites/${encodeURIComponent(name)}/reissue`);
          Object.assign(cardUi(name), { code: r.code, confirm: false });
        });
        break;
      case "revoke-ask":
      case "revoke-cancel":
        cardUi(name).confirm = act === "revoke-ask";
        drawLenders();
        break;
      case "revoke":
        await write(async () => {
          await send("DELETE", `/admin/api/invites/${encodeURIComponent(name)}`);
          S.cards.delete(name);
        });
        break;
      case "approve":
        await write(() => send("POST", `/admin/api/approvals/${encodeURIComponent(b.dataset.id)}`, { approve: b.dataset.yes === "1" }));
        break;
      case "new":
        S.selected = "new";
        cache.items = null;
        refresh();
        break;
      case "cancel":
        await write(() => send("POST", `/admin/api/tasks/${encodeURIComponent(b.dataset.id)}/cancel`));
        break;
      case "folder-add":
        await write(async () => {
          const picked = await send("POST", "/admin/api/pick-folder");
          if (!picked || !picked.path) return;
          const r = await send("POST", "/admin/api/folders/add", { path: picked.path, level: "read" });
          S.folderMessage = (r && r.warning) || "";
        });
        break;
      case "folder-remove":
        await write(async () => {
          await send("POST", "/admin/api/folders/remove", { path: b.dataset.path });
          S.folderMessage = "";
        });
        break;
      case "folder-main":
        await write(() => send("POST", "/admin/api/folders/main", { path: b.dataset.path }));
        break;
      default:
        break;
    }
  }

  async function issueInvite() {
    const input = el.invite.querySelector("input");
    const name = input.value.trim();
    S.inviteName = input.value;
    if (!name) {
      input.focus();
      return;
    }
    try {
      const r = await send("POST", "/admin/api/invites", { name });
      S.inviteCode = r.code;
      clearError("use-write");
    } catch (e) {
      showError(e.message, "use-write");
    }
    drawInvite();
    refresh();
  }

  // ---- changes of a control ----
  function selectCode(ev) {
    if (ev.target.matches("input.code-field")) ev.target.select();
  }

  async function onChange_(ev) {
    const t = ev.target;
    const act = t.dataset.act;
    const name = t.dataset.name;
    if (t.dataset.pick) {
      S.pick = { ...S.pick, [t.dataset.pick]: t.value };
      cache.bar = null;
      drawComposer(true);
      return;
    }
    switch (act) {
      case "exec-mode":
        await write(() => send("PUT", `/admin/api/lenders/${encodeURIComponent(name)}/exec-mode`, { mode: t.value }));
        break;
      case "delete-approval":
        t.nextElementSibling.nextElementSibling.textContent = t.checked ? "オン" : "オフ";
        await write(() => send("PUT", `/admin/api/lenders/${encodeURIComponent(name)}/delete-approval`, { value: t.checked }));
        break;
      case "log-files":
      case "log-result": {
        const box = t.closest(".pop");
        const files = box.querySelector('[data-act="log-files"]');
        const result = box.querySelector('[data-act="log-result"]');
        if (act === "log-result" && result.checked) files.checked = true;
        if (act === "log-files" && !files.checked) result.checked = false;
        const value = result.checked ? "all" : files.checked ? "files" : "summary";
        const label = { summary: "なし", files: "道具の名前と対象", all: "道具の名前と対象・道具の結果" }[value];
        t.closest("details").querySelector(".summary").textContent = label;
        await write(() => send("PUT", `/admin/api/lenders/${encodeURIComponent(name)}/log-visibility`, { value }));
        break;
      }
      case "folder-level": {
        const path = norm(t.dataset.path);
        await write(() => editForm((form) => {
          form.folders = form.folders.flatMap((f) => (norm(f.path) !== path ? [f] : t.value === "inherit" ? [] : [{ ...f, level: t.value }]));
        }));
        break;
      }
      case "allow-confirm":
        await write(() => editForm((form) => { form.exec = { ...form.exec, confirm_modified_scripts: t.checked }; }));
        break;
      default:
        break;
    }
  }

  async function onFocusOut(ev) {
    if (ev.target.dataset && ev.target.dataset.act === "allow-text") {
      const lines = ev.target.value.split("\n").map((s) => s.trim()).filter(Boolean);
      cache.allow = null;
      await write(() => editForm((form) => { form.exec = { ...form.exec, allow: lines }; }));
    }
  }

  function onInput(ev) {
    if (ev.target === el.text) S.drafts.set(S.selected, el.text.value);
    if (ev.target.dataset && ev.target.dataset.slot === "invite-name") S.inviteName = ev.target.value;
  }

  function onKey(ev) {
    if (ev.target.dataset && ev.target.dataset.slot === "invite-name" && ev.key === "Enter") {
      ev.preventDefault();
      issueInvite();
    }
    if (ev.target === el.text && ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      el.form.requestSubmit();
    }
  }

  async function onSubmit(ev) {
    ev.preventDefault();
    const text = el.text.value.trim();
    if (!text) return;
    const key = S.selected;
    const button = el.form.querySelector("button[type=submit]");
    if (button) button.disabled = true;
    try {
      let id = key;
      if (key === "new") {
        const body = { prompt: text };
        for (const k of ["lender", "model", "effort"]) if (S.pick[k]) body[k] = S.pick[k];
        id = (await send("POST", "/admin/api/tasks", body)).task_id;
      } else if (view.sel && ACTIVE.includes(view.sel.state)) {
        await send("POST", `/admin/api/tasks/${encodeURIComponent(key)}/followup`, { text });
      } else {
        const body = { prompt: text, continue_task_id: key };
        if (view.sel && view.sel.lender) body.lender = view.sel.lender;
        id = (await send("POST", "/admin/api/tasks", body)).task_id;
      }
      clearError("use-send");
      S.drafts.delete(key);
      el.text.value = "";
      S.selected = id;
    } catch (e) {
      showError(e.message, "use-send");
    }
    if (button) button.disabled = false;
    await refresh();
  }

  function closeLists(ev) {
    document.querySelectorAll("details.combo[open]").forEach((d) => {
      if (ev.type === "keydown" ? ev.key === "Escape" : !d.contains(ev.target)) d.open = false;
    });
  }

  root.addEventListener("click", onClick);
  shell.addEventListener("change", onChange_);
  shell.addEventListener("focusout", onFocusOut);
  shell.addEventListener("focusin", selectCode);
  shell.addEventListener("click", selectCode);
  shell.addEventListener("input", onInput);
  shell.addEventListener("keydown", onKey);
  el.form.addEventListener("submit", onSubmit);
  document.addEventListener("click", closeLists);
  document.addEventListener("keydown", closeLists);

  drawInvite();
  refresh();
  const stop = onChange(refresh);
  return () => {
    alive = false;
    stop();
    root.removeEventListener("click", onClick);
    document.removeEventListener("click", closeLists);
    document.removeEventListener("keydown", closeLists);
    ["use-read", "use-write", "use-send", "use-setup", "use-copy"].forEach(clearError);
  };
}
