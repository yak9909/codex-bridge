// Small helpers shared by the parts of the use tab.
import { esc, fmtTime, fmtDay } from "/app/static/api.js";

export const ICONS = "/app/static/icons.svg";
export const icon = (id, cls = "ic") => `<svg class="${cls}"><use href="${ICONS}#i-${id}"/></svg>`;

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

export function stateLozenge(state) {
  const [label, cls] = STATES[state] || [state || "?", "idle"];
  return `<span class="lz ${cls}">${esc(label)}</span>`;
}

// the two ways to write a set of controls into a page and keep what the user is doing
export function busyInside(el) {
  if (!el) return false;
  const a = document.activeElement;
  if (a && el.contains(a) && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName)) return true;
  return !!el.querySelector("details[open]");
}
