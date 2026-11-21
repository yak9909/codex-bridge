// The lender cards of the use tab: header, usage meters, and (open) the settings rows.
import { esc, fmtDay } from "/app/static/api.js";
import { icon, codeFieldHtml, monthDay, taskTitle, meter, row, toggle, logCombo as sharedLogCombo, OVER_LIMIT, CHEV } from "/app/static/common.js";

export const EXEC_MODES = [["off", "実行しない"], ["allowlist", "許可リストだけ自動"], ["ask", "毎回承認"], ["all", "すべて自動"]];

function stateOf(l) {
  if (l.update_waiting) return [l.connected ? "切り替え待ち" : "再起動中", "warn"];
  if (l.connected && l.current_task_id) return ["作業中", "ok"];
  if (l.connected && l.ready) return ["待機中", "idle"];
  if (l.connected) return ["準備中", "warn"];
  return ["未接続", "idle"];
}

function caption(l, tasks) {
  if (!l.connected) return `<span class="cap">${l.since ? `最後につながったのは ${esc(fmtDay(l.since))}` : "まだつながっていません"}</span>`;
  if (!l.ready) return '<span class="cap">使用量を読み込んでいます</span>';
  const count = l.active_task_ids?.length || (l.current_task_id ? 1 : 0);
  if (count > 1) return `<span class="cap low">${esc(count)} 件作業中</span>`;
  const t = l.current_task_id && tasks.get(l.current_task_id);
  if (t) return `<span class="cap ellipsis">${esc(taskTitle(t))}</span>`;
  if (l.current_task_id) return `<span class="cap ellipsis">${esc(l.current_task_id)}</span>`;
  return '<span class="cap low">依頼はありません</span>';
}

function head(l, ui, tasks) {
  const [label, cls] = stateOf(l);
  const warns = [];
  if (l.exec_mode === "all") warns.push("コマンド: すべて自動");
  if (l.capabilities && l.capabilities.model_select === false) warns.push("古い版: モデルを選べません");
  const badges = warns.map((w) => `<span class="badge warn">${esc(w)}</span>`).join("");
  return `<button class="ent-head" data-act="toggle" data-name="${esc(l.name)}" aria-expanded="${ui.open}">` +
    `<span class="ent-line"><span class="ent-name">${esc(l.name)}</span><span class="lz ${cls}">${esc(label)}</span>${badges}</span>` +
    `${caption(l, tasks)}${icon("chevron-right", "ic chev")}</button>`;
}

function sec(l) {
  const usage = l.usage || {};
  const limits = l.limits || {};
  const rows = meter("5時間", usage.five_hour, limits.five_hour) + meter("週間", usage.weekly, limits.weekly);
  return rows ? `<div class="ent-sec"><div class="meters">${rows}</div></div>` : "";
}

function execSelect(l) {
  const opts = EXEC_MODES.map(([v, t]) => `<option value="${v}"${v === l.exec_mode ? " selected" : ""}>${t}</option>`).join("");
  return `<select data-act="exec-mode" data-name="${esc(l.name)}">${opts}</select>`;
}

function logCombo(l) {
  return sharedLogCombo(l.log_visibility || "summary", `data-name="${esc(l.name)}"`);
}

function inviteRow(l, ui) {
  if (l.invite_created == null) return "";
  const cap = `${esc(monthDay(l.invite_created))} 発行`;
  const name = esc(l.name);
  let body;
  if (ui.confirm) {
    body = `<span>この招待コードを取り消しますか</span><button class="danger sm" data-act="revoke" data-name="${name}">取り消す</button>` +
      `<button class="secondary sm" data-act="revoke-cancel" data-name="${name}">やめる</button>`;
  } else {
    body = `<button class="secondary sm" data-act="reissue" data-name="${name}">発行し直す</button>` +
      `<button class="danger sm" data-act="revoke-ask" data-name="${name}">取り消す</button>`;
  }
  const code = ui.code ? codeFieldHtml(ui.code) : "";
  return `<div class="set"><span class="lbl">招待コード<span class="cap">${cap}</span></span><div class="ctl">${body}</div>${code}</div>`;
}

// ---- the lender's own settings, read-only: the same controls as on the lender's screen ----
const dash = () => '<select class="ro" aria-readonly="true" tabindex="-1"><option>—</option></select>';
const ro = (on) => toggle(on, "", true);

function modelsRow(view) {
  const all = view && view.models_all;
  if (!all) return dash();
  const def = all.find((m) => m.is_default && m.allowed) || all.find((m) => m.is_default) || all.find((m) => m.allowed);
  const items = all.map((m) => `<li${m.allowed ? "" : ' class="off"'}>${icon("check")}${esc(m.model)}${m.is_default ? '<span class="badge accent">既定</span>' : ""}</li>`).join("");
  return `<details class="combo"><summary class="combo-box ro"><span class="summary">${def ? `既定 ${esc(def.model)}` : "なし"}</span>${CHEV}</summary><ul class="pop">${items}</ul></details>`;
}

function preRow(view) {
  if (!view) return dash();
  const p = view.pre_approval;
  const on = [p.new_task ? "新しい依頼" : "", p.followup ? "追加の指示" : ""].filter(Boolean);
  const minutes = on.length && p.timeout_min != null
    ? `<input class="ro" type="number" value="${p.timeout_min}" readonly tabindex="-1"><span>分で取り消し</span>` : "";
  return `<div class="combo" style="flex:1"><div class="combo-box ro"><span class="summary">${on.length ? on.join("・") : "なし"}</span>${CHEV}</div></div>${minutes}`;
}

function viewGroup(l) {
  const v = l.lender_view;
  const flag = (on) => (v ? ro(on) : dash());
  const f = v && v.features;
  const rows = [
    row("使えるモデル", modelsRow(v)),
    row("推論の強さの上限", v ? `<select class="ro" aria-readonly="true" tabindex="-1"><option>${esc(v.max_effort)}</option></select>` : dash()),
    row("同時に受ける依頼", v ? `<select class="ro" aria-readonly="true" tabindex="-1"><option>${esc(v.max_parallel ?? 1)}</option></select>` : dash()),
    row("始める前の許可", preRow(v)),
    row("続きの依頼", flag(v && v.resumable)),
    row("Web 検索", flag(f && f.web_search)),
    row("画像の生成", flag(f && f.image_generation)),
    row("サブエージェント", flag(f && f.multi_agent)),
    row("目標", flag(f && f.goals)),
    row("上限を超えたとき", v ? `<select class="ro" aria-readonly="true" tabindex="-1"><option>${OVER_LIMIT[v.over_limit]}</option></select>` : dash()),
  ].join("");
  return `<div class="set-group"><p class="grp">${esc(l.name)} さんが決めた設定</p>${rows}</div>`;
}

function setBody(l, ui) {
  const del = toggle(l.delete_approval, `data-act="delete-approval" data-name="${esc(l.name)}"`);
  return `<div class="ent-set">${row("コマンド実行", execSelect(l), "拒否・確認リストはどのモードでも適用")}${row("削除の前に承認する", del)}${row("相手に見せる記録", logCombo(l))}` +
    `${inviteRow(l, ui)}${viewGroup(l)}</div>`;
}

// the three parts of a card, each a whole element (or "" when the card has none)
export function lenderParts(l, ui, tasks) {
  return { head: head(l, ui, tasks), sec: sec(l), set: ui.open ? setBody(l, ui) : "" };
}
