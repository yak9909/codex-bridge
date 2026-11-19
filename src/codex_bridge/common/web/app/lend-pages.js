// The shared machine settings remain in the mock's rowset.
import { esc } from "/app/static/api.js";
import { row } from "/app/static/common.js";

export function pcHtml(data, overview, ui) {
  const editor = (kind, value, action) => `<label class="field grow"><span>${kind === "workdir" ? "作業フォルダ" : "データの場所"}</span><input data-act="${kind}-path" value="${esc(value)}" spellcheck="false"></label>` +
    `${kind === "data" ? '<span>データの場所を移しますか</span>' : ""}<button class="${kind === "data" ? "danger" : "secondary"} sm" data-act="${action}">${kind === "workdir" ? "変える" : "移す"}</button><button class="secondary sm" data-act="pc-cancel">やめる</button>`;
  return '<div class="rowset">' +
    row("作業フォルダ", ui.edit === "workdir" ? editor("workdir", ui.workdir, "workdir-apply") : '<button class="secondary sm" data-act="workdir-open">変える</button>', esc(data.workdir)) +
    row("記録を残す日数", `<input class="sm" type="number" min="1" max="3650" required data-act="retention" aria-label="記録を残す日数" value="${esc(data.settings.log_retention_days)}"><span>日</span>`) +
    row("データの場所", ui.edit === "data" ? editor("data", ui.path, "move") : '<button class="secondary sm" data-act="move-open">移す</button>', esc(data.data_root)) +
    row("codex-bridge", "", esc(overview.agent_version || "—")) + '</div>' +
    (ui.message ? `<p class="hint" role="status">${esc(ui.message)}</p>` : "");
}
