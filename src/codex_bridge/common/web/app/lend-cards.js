// Connection cards use the shared SettingsExpander controls.
import { esc } from "/app/static/api.js";
import { icon, row, combo, toggle, logCombo, taskTitle } from "/app/static/common.js";

export const FEATURES = [["web_search", "Web 検索"], ["image_generation", "画像の生成"], ["multi_agent", "サブエージェント"], ["goals", "目標"]];

export function modelSummary(data) {
  const s = data.settings;
  const all = data.models || [];
  const allowed = all.filter((m) => s.allowed_models === null || s.allowed_models.includes(m.model));
  const def = allowed.find((m) => m.is_default) || allowed[0];
  return def ? `${def.model}${allowed.length > 1 ? ` ほか ${allowed.length - 1} 個` : ""}` : "なし";
}

export function preSummary(p) {
  return [p.new_task ? "新しい依頼" : "", p.followup ? "追加の指示" : ""].filter(Boolean).join("・") || "なし";
}

export function connectionHead(c, open, tasks) {
  const [label, cls] = c.invite_problem ? ["招待コードが無効", "bad"] : !c.connected ? ["つなぎ直し中", "warn"] : c.busy ? ["作業中", "ok"] : ["待機中", "idle"];
  const active = tasks.filter((t) => t.connection_id === c.id && ["queued", "running", "awaiting_lender", "agent_offline"].includes(t.state));
  const caption = active.length > 1 ? `${active.length} 件作業中` : active.length ? taskTitle(active[0]) : "依頼はありません";
  return `<button class="ent-head" data-act="card" data-id="${esc(c.id)}" aria-expanded="${open}">` +
    `<span class="ent-line"><span class="ent-name ellipsis" title="${esc(c.host_name)}">${esc(c.host_name)}</span><span class="lz ${cls}">${label}</span></span>` +
    `<span class="cap low ellipsis">${esc(caption)}</span>${icon("chevron-right", "ic chev")}</button>`;
}

export function settingsHtml(c, data, count, ui) {
  if (!data) return "";
  const s = data.settings;
  const attr = `data-id="${esc(c.id)}"`;
  const models = (data.models || []).map((m) => `<li data-model="${esc(m.model)}"><label class="check"><input type="checkbox" data-act="model" ${attr} value="${esc(m.model)}"${s.allowed_models === null || s.allowed_models.includes(m.model) ? " checked" : ""}>${esc(m.model)}</label>${m.is_default ? '<span class="badge accent">既定</span>' : ""}</li>`).join("");
  const search = '<li class="model-search"><input class="sm" type="search" data-act="model-search" placeholder="検索" aria-label="モデルを検索"></li>';
  const opts = (data.efforts || []).map((e) => `<option value="${esc(e)}"${e === s.max_effort ? " selected" : ""}>${esc(e)}${e === "xhigh" ? "（おすすめ）" : ""}</option>`).join("");
  const p = s.pre_approval;
  const checks = [["new_task", "新しい依頼"], ["followup", "追加の指示"]].map(([k, label]) => `<li><label class="check"><input type="checkbox" data-act="pre" data-key="${k}" ${attr}${p[k] ? " checked" : ""}>${label}</label></li>`).join("");
  const timeout = `<span class="pre-timeout"${p.new_task || p.followup ? "" : " hidden"}><input type="number" min="1" max="1440" placeholder="なし" aria-label="取り消すまでの分数" data-act="timeout" ${attr} value="${esc(p.timeout_min ?? "")}"><span>分で取り消し</span></span>`;
  const confirm = (action, message, label) => `<span>${message}</span><button class="danger sm" data-act="${action}" ${attr}>${label}</button><button class="secondary sm" data-act="cancel-confirm" ${attr}>やめる</button>`;
  const logs = ui.confirm === "logs" ? confirm("clear-logs", "この依頼主の記録を消しますか", "記録を消す") : `<button class="danger sm" data-act="ask-logs" ${attr}>記録を消す</button>`;
  let connect = `<button class="secondary sm" data-act="replace-open" ${attr}>招待コードを入れ直す</button><button class="danger sm" data-act="ask-remove" ${attr}>外す</button>`;
  if (ui.confirm === "remove") connect = confirm("remove", "この依頼主を外しますか", "外す");
  // one line: the row heading already says what this is; the field has the small height of the buttons beside it
  if (ui.replace) connect = `<input class="sm grow" data-act="replace-code" ${attr} value="${esc(ui.code)}" placeholder="新しい招待コード" aria-label="新しい招待コード" autocomplete="off"><button class="sm" data-act="replace" ${attr}>つなぐ</button><button class="secondary sm" data-act="replace-cancel" ${attr}>やめる</button>`;
  return row("使えるモデル", combo(modelSummary(data), search + models, 'data-combo="models"')) +
    row("推論の強さの上限", `<select data-act="effort" ${attr}>${opts}</select>`) +
    row("始める前の許可", combo(preSummary(p), checks, 'data-combo="pre"') + timeout) +
    row("続きの依頼", toggle(!s.disposable, `data-act="resumable" ${attr}`)) +
    FEATURES.map(([key, label]) => row(label, toggle(s.features[key], `data-act="feature" data-key="${key}" ${attr}`))).join("") +
    row("この画面に出す記録", logCombo(s.log_visibility, attr)) + row("記録", logs, `${count} 件`) + row("つなぎ方", connect);
}
