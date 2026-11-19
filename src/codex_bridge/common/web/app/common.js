// Shared controls and formatters for the use and lend tabs.
import { esc, fmtTime, fmtDay } from "/app/static/api.js";

export const ICONS = "/app/static/icons.svg";
export const icon = (id, cls = "ic") => `<svg class="${cls}"><use href="${ICONS}#i-${id}"/></svg>`;

export function codeFieldHtml(code) {
  return `<div class="row code-row"><input readonly class="code-field" value="${esc(code)}" aria-label="招待コード" spellcheck="false">` +
    `<button class="secondary sm" data-act="copy">${icon("copy")}コピー</button></div>`;
}

const two = (n) => String(n).padStart(2, "0");

// "15:40 に戻る" for today, "11/19 03:00 に戻る" for another day
export function backAt(epoch) {
  const d = new Date(epoch * 1000);
  const now = new Date();
  const today = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return `${today ? fmtTime(epoch) : fmtDay(epoch)} に戻る`;
}

export function monthDay(epoch) {
  const d = new Date(epoch * 1000);
  return `${two(d.getMonth() + 1)}/${two(d.getDate())}`;
}

// "5 分", "1 時間 5 分"
export function elapsed(seconds) {
  const m = Math.max(0, Math.floor(seconds / 60));
  if (m < 1) return "1 分未満";
  if (m < 60) return `${m} 分`;
  return m % 60 ? `${Math.floor(m / 60)} 時間 ${m % 60} 分` : `${Math.floor(m / 60)} 時間`;
}

export function taskTitle(task) {
  const line = String(task.prompt || "").split("\n")[0].trim();
  return line;
}

export function onOff(on) {
  return on ? "オン" : "オフ";
}

export const STATES = {
  queued: ["順番待ち", "idle"],
  running: ["作業中", "ok"],
  completed: ["完了", "idle"],
  suspended_quota: ["中断（枠切れ）", "idle"],
  failed: ["失敗", "bad"],
  cancelled: ["中止", "idle"],
  agent_offline: ["接続待ち", "warn"],
  awaiting_lender: ["相手の許可待ち", "warn"],
  rejected_by_lender: ["拒否", "bad"],
};
export const ACTIVE = ["queued", "running", "agent_offline", "awaiting_lender"];

export function stateLozenge(state, side = "use") {
  const [label, cls] = side === "lend" && state === "awaiting_lender"
    ? ["あなたの許可待ち", "warn"] : STATES[state] || [state || "?", "idle"];
  return `<span class="lz ${cls}">${esc(label)}</span>`;
}

// the two ways to write a set of controls into a page and keep what the user is doing
export function busyInside(el) {
  if (!el) return false;
  const a = document.activeElement;
  if (a && el.contains(a) && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName)) return true;
  return !!el.querySelector("details[open]");
}

export function meter(label, win, limit) {
  if (!win) return "";
  const used = Math.max(0, Math.min(100, Math.round(win.used_percent)));
  const hot = used >= (limit ?? 90) ? " hot" : "";
  const help = [win.resets_at ? backAt(win.resets_at) : "", limit != null ? `上限 ${Math.round(limit)}%` : ""].filter(Boolean).join("・");
  const mark = limit != null ? `<div class="m-mark" style="left:${Math.min(100, limit)}%"></div>` : "";
  return `<span class="m-label">${label}</span><div class="m-bar"><div class="m-fill${hot}" style="width:${used}%"></div>${mark}</div>` +
    `<span class="m-val${hot}">${used}%</span>${help ? `<span class="m-help">${esc(help)}</span>` : ""}`;
}

export function row(label, control, cap = "") {
  return `<div class="set"><span class="lbl">${label}${cap ? `<span class="cap">${cap}</span>` : ""}</span><div class="ctl">${control}</div></div>`;
}

export const LOG_LABEL = { summary: "なし", files: "道具の名前と対象", all: "道具の名前と対象・道具の結果" };
export const OVER_LIMIT = { report: "止めて報告させる", stop: "すぐ強制終了" };
export const CHEV = icon("chevron-right");

export function combo(summary, items, attrs = "") {
  return `<details class="combo" ${attrs}><summary class="combo-box"><span class="summary">${esc(summary)}</span>${CHEV}</summary><ul class="pop">${items}</ul></details>`;
}

export function toggle(on, attrs = "", readonly = false) {
  return `<label class="switch${readonly ? " ro" : ""}"><input type="checkbox" ${attrs}${readonly ? " disabled" : ""}${on ? " checked" : ""}><span class="track"></span><span>${onOff(on)}</span></label>`;
}

export function logCombo(value, attrs = "") {
  const files = value === "files" || value === "all";
  return combo(LOG_LABEL[value] || LOG_LABEL.summary,
    `<li><label class="check"><input type="checkbox" data-act="log-files" ${attrs}${files ? " checked" : ""}>道具の名前と対象</label></li>` +
    `<li><label class="check"><input type="checkbox" data-act="log-result" ${attrs}${value === "all" ? " checked" : ""}>道具の結果</label></li>`);
}
