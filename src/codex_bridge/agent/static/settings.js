"use strict";
// Settings have their own lifecycle: polling never replaces a form being edited.
const LenderSettings = (() => {
  const $ = (id) => document.getElementById(id);
  const flagChoices = (id, value, options) => UI.choiceCards($(id), id, options, String(value));
  const selected = (id) => $(id).querySelector("input:checked").value === "true";

  function fill(data) {
    const s = data.settings;
    $("limit-five").value = s.limits.five_hour ?? "";
    $("limit-week").value = s.limits.weekly ?? "";
    $("limit-force").checked = s.limits.force;
    $("approval-timeout").value = s.pre_approval.timeout_min;
    $("retention").value = s.log_retention_days;
    $("workdir").value = s.workdir || "";
    $("current-workdir").textContent = data.workdir;
    $("data-root").textContent = data.data_root;
    $("feature-options").innerHTML = Object.entries(data.feature_info).map(([key, f]) =>
      `<label class="check"><input type="checkbox" data-feature="${UI.esc(key)}"${s.features[key] ? "" : " checked"}>
        <span><b>${UI.esc(f.label)}を止める${f.best_effort ? "（ベストエフォート）" : ""}</b><br>
        <span class="small">${UI.esc(f.reason.replaceAll("使わせる側", "あなた"))}</span>
        ${f.note ? `<br><span class="small best-effort">${UI.esc(f.note)}</span>` : ""}</span></label>`).join("");
    flagChoices("pre-choices", s.pre_approval.enabled, [
      { value: "false", title: "そのまま始める", desc: "依頼主から届いた依頼を自動で始めます。" },
      { value: "true", title: "依頼主からの依頼を、私が許可してから始める", desc: "依頼文を見て、許可か拒否を選びます。" },
    ]);
    flagChoices("disposable-choices", s.disposable, [
      { value: "false", title: "セッションを残す", desc: "終わったあとの続きの依頼を受けられます。" },
      { value: "true", title: "セッションを使い捨てにする", desc: "Codex の会話の記録を作らず、依頼が終わったら一覧からも消します。" },
    ]);
  }

  async function init(api, say, refresh) {
    fill(await api("GET", "/settings"));
    $("settings-form").onsubmit = async (event) => {
      event.preventDefault();
      const button = $("save-settings");
      button.disabled = true;
      const percent = (id) => $(id).value === "" ? null : Number($(id).value);
      const patch = {
        limits: { five_hour: percent("limit-five"), weekly: percent("limit-week"), force: $("limit-force").checked },
        features: Object.fromEntries([...$("feature-options").querySelectorAll("input")].map((i) => [i.dataset.feature, !i.checked])),
        pre_approval: { enabled: selected("pre-choices"), timeout_min: Number($("approval-timeout").value) },
        disposable: selected("disposable-choices"), workdir: $("workdir").value.trim() || null,
        log_retention_days: Number($("retention").value),
      };
      try {
        await api("PUT", "/settings", patch);
        // Do not redraw the fields: the person may already be typing their next change.
        const fresh = await api("GET", "/settings");
        $("current-workdir").textContent = fresh.workdir;
        $("settings-message").textContent = "保存しました。機能・作業フォルダの変更は、作業が終わってから反映します。";
        await refresh();
      } catch (error) { say(error.message, true); }
      finally { button.disabled = false; }
    };
    $("clear-logs").onclick = async () => {
      if (!confirm("作業の記録をすべて消します。セッションの会話と設定は残ります。よろしいですか？")) return;
      try { const result = await api("POST", "/logs/clear"); say(`${result.cleared} 件のタスクの記録を消しました。`); await refresh(); }
      catch (error) { say(error.message, true); }
    };
    $("data-form").onsubmit = async (event) => {
      event.preventDefault();
      const path = $("new-data-root").value.trim();
      if (!path || !confirm(`データを ${path} に移して、exe を起動し直します。よろしいですか？`)) return;
      $("move-data").disabled = true;
      try {
        const result = await api("POST", "/settings/data-root", { path });
        say("データを移しました。exe を起動し直しています。" + (result.left.length ? ` 元の場所に残ったファイル: ${result.left.join("、")}` : ""));
      } catch (error) { say(error.message, true); $("move-data").disabled = false; }
    };
  }
  return { init };
})();
