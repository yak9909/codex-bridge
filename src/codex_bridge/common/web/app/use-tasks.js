// The 依頼 tab of the use tab: the list, the detail with its timeline, and the composer's pickers.
import { esc, fmtTime, fmtDay } from "/app/static/api.js";
import { icon, elapsed, taskTitle, stateLozenge, ACTIVE } from "/app/static/common.js";

const EFFORT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"];
const COMMAND_TOOLS = /^(run_command|commandExecution|command_execution)$/i;
const FILE_TOOLS = /^(read_file|write_file|edit_file|move_path|delete_path|fileChange|file_change|apply_patch|patch)$/i;
const ENDINGS = {
  completed: ["circle-check-big", "完了しました"],
  failed: ["circle-x", "失敗しました"],
  cancelled: ["circle-x", "中止しました"],
  suspended_quota: ["clock", "使用量の上限で中断しました"],
  rejected_by_lender: ["circle-x", "相手が断りました"],
};

export function listHtml(tasks, selected, keyFor = (t) => t.task_id, side = "use") {
  const now = Date.now() / 1000;
  return tasks.map((t) => {
    const when = ACTIVE.includes(t.state) ? `<span>${esc(elapsed(now - t.created))}</span>` : `<span>${esc(fmtDay(t.created))}</span>`;
    const who = t.lender ? `<span>${esc(t.lender)}</span>` : "";
    return `<li data-id="${esc(keyFor(t))}" aria-selected="${selected === keyFor(t)}"><span class="t-title">${esc(taskTitle(t))}</span>` +
      `<span class="t-meta">${stateLozenge(t.state, side)}${who}${when}</span></li>`;
  }).join("");
}

export function newHeadHtml() {
  return '<div class="d-title"><h2>新しい依頼</h2></div>';
}

export function headHtml(r, readonly = false, side = "use") {
  const now = Date.now() / 1000;
  const active = ACTIVE.includes(r.state);
  const model = r.actual_model || r.model;
  const effort = r.actual_effort || r.effort;
  const used = [model, effort].filter(Boolean).join("・");
  let switched = "";
  if (r.rerouted && r.rerouted.from) {
    const why = r.rerouted.reason ? `（理由 ${r.rerouted.reason}）` : "";
    switched = `<span class="badge" title="${esc(`途中で ${r.rerouted.from} から切り替わりました${why}`)}">${esc(r.rerouted.from)} から切り替え</span>`;
  }
  const span = (r.finished || now) - r.created;
  const body = String(r.prompt || "").split(/\r?\n/).slice(1).join("\n");
  return `<div class="d-title"><h2>${esc(taskTitle(r))}</h2>` +
    `${active && !readonly ? `<button class="danger sm" data-act="cancel" data-id="${esc(r.task_id)}">中止</button>` : ""}</div>` +
    `<div class="d-meta">${stateLozenge(r.state, side)}${r.lender ? `<b>${esc(r.lender)}</b>` : ""}${used ? `<span>${esc(used)}</span>` : ""}${switched}` +
    `<span>${esc(elapsed(span))}</span></div>${body ? `<p class="d-body">${esc(body)}</p>` : ""}`;
}

function item(cls, iconId, body, ts) {
  return `<li class="${cls}"><span class="tl-badge">${icon(iconId)}</span><span class="tl-body">${body}</span>` +
    `<span class="time">${ts ? esc(fmtTime(ts)) : ""}</span></li>`;
}

const code = (text) => (text ? ` <code>${esc(String(text).slice(0, 160))}</code>` : "");

// The runner sends arguments as an object or JSON text; capped/legacy calls may omit them.
function toolArguments(value) {
  if (typeof value === "string") {
    try { value = JSON.parse(value); } catch {
      // _cap can cut JSON mid-value. Keep only complete fields before the cut.
      if (value.startsWith("{") && value.endsWith("…")) {
        for (let end = value.lastIndexOf(","); end > 0; end = value.lastIndexOf(",", end - 1)) {
          try { return JSON.parse(value.slice(0, end) + "}"); } catch { /* still inside the cut value */ }
        }
      }
      return {};
    }
  }
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function filePaths(args) {
  const paths = [args.path, ...(Array.isArray(args.paths) ? args.paths : []), args.src, args.dst, args.source, args.destination]
    .filter((p) => typeof p === "string" && p);
  // apply_patch carries its targets in patch headers, never in a top-level path field.
  if (typeof args.patch === "string") {
    for (const m of args.patch.matchAll(/^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$/gm)) paths.push(m[1].trim());
  }
  return [...new Set(paths)];
}

function eventItem(ev, r, side) {
  const d = ev.data;
  const text = typeof d === "string" ? d : d && typeof d.text === "string" ? d.text : "";
  switch (ev.kind) {
    case "started":
      return item("ev", "play", `${side !== "lend" && r.lender ? `${esc(r.lender)} が` : ""}受け付けました`, ev.ts);
    case "lender_approved":
      return side === "lend" ? item("ev", "shield-check", "あなたが許可しました", ev.ts) : "";
    case "agent_message":
      return text ? item("msg", "message-square-text", esc(text), ev.ts) : "";
    case "tool_finished": {
      const tool = d && d.tool ? String(d.tool) : "";
      const failed = d && d.success === false ? "（失敗）" : "";
      const args = toolArguments(d && d.arguments);
      if (COMMAND_TOOLS.test(tool)) {
        const command = typeof args.command === "string" ? args.command : tool;
        return item("ev", "terminal", `コマンド実行${failed}${code(command)}`, ev.ts);
      }
      if (FILE_TOOLS.test(tool)) {
        const label = tool.toLowerCase() === "read_file" ? "ファイルを読む" : "ファイルを変更";
        const paths = filePaths(args);
        return item("ev", "folder", `${label}${failed}${paths.length ? paths.map(code).join("、") : code(tool)}`, ev.ts);
      }
      return item("ev", "settings", `道具を実行${failed}${code(tool)}`, ev.ts);
    }
    case "plan":
      return d && Array.isArray(d.steps) ? item("ev", "list-checks", `計画を更新しました（${d.steps.length} 件）`, ev.ts) : "";
    case "status":
      return text ? item("ev", "circle-alert", esc(text), ev.ts) : "";
    case "model":
      return d && d.rerouted_from
        ? item("ev", "cpu", `モデルが ${esc(d.rerouted_from)} から ${esc(d.model || "")} に切り替わりました`, ev.ts) : "";
    default:
      return "";
  }
}

export function timelineHtml(r, events, progress, side = "use") {
  const items = (events || []).map((ev) => eventItem(ev, r, side)).filter(Boolean);
  if (progress && ACTIVE.includes(r.state)) items.push(item("msg", "message-square-text", esc(progress.slice(-600)), 0));
  if (r.state === "awaiting_lender") items.push(item("ev", "clock", side === "lend" ? "あなたの許可を待っています" : "相手の許可を待っています", 0));
  const ending = side === "lend" && r.state === "rejected_by_lender" ? ["circle-x", "あなたが断りました"] : ENDINGS[r.state];
  if (r.report && ending) {
    const last = [...(events || [])].reverse().find((e) => e.kind === "agent_message");
    const lastText = last && last.data && last.data.text;
    if (lastText !== r.report) items.push(item("msg", "message-square-text", esc(r.report), r.finished));
  }
  if (ending && r.finished) items.push(item("ev", ending[0], ending[1], r.finished));
  return items.length ? `<h3>経過</h3><ol class="tl">${items.join("")}</ol>` : "<h3>経過</h3>";
}

// ---- the pickers of a new request: 相手 / モデル / 推論, from what the chosen lender offers ----
export function pickerOptions(lenders, pick) {
  const ready = lenders.filter((l) => l.connected && l.ready);
  const asked = ready.filter((l) => l.name === pick.lender);
  const among = asked.length ? asked : ready;
  const lender = asked.length ? pick.lender : "";
  const models = new Map();
  for (const l of among) for (const m of l.models || []) if (!models.has(m.model)) models.set(m.model, m);
  const model = models.has(pick.model) ? pick.model : "";
  const efforts = new Set();
  for (const m of model ? [models.get(model)] : models.values()) for (const e of m.efforts || []) efforts.add(e);
  const sorted = [...efforts].sort((a, b) => EFFORT_ORDER.indexOf(a) - EFFORT_ORDER.indexOf(b));
  const effort = efforts.has(pick.effort) ? pick.effort : "";
  const opts = (rows, value) => rows.map(([v, t]) => `<option value="${esc(v)}"${v === value ? " selected" : ""}>${esc(t)}</option>`).join("");
  const html =
    `<select class="ghost" data-pick="lender" aria-label="頼む相手">${opts([["", "相手: 自動"], ...ready.map((l) => [l.name, `相手: ${l.name}`])], lender)}</select>` +
    `<select class="ghost" data-pick="model" aria-label="モデル">${opts([["", "モデル: 既定"], ...[...models.values()].map((m) => [m.model, `モデル: ${m.name || m.model}`])], model)}</select>` +
    `<select class="ghost" data-pick="effort" aria-label="推論の強さ">${opts([["", "推論: 既定"], ...sorted.map((e) => [e, `推論: ${e}`])], effort)}</select>`;
  return { html, pick: { lender, model, effort } };
}
