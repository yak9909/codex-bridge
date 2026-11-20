"""Check the app's assets, routes and renderers without a browser."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from codex_bridge.host.web.app import admin_router
from codex_bridge.agent.ui import create_ui_app

APP = Path(__file__).parents[1] / "src" / "codex_bridge" / "common" / "web" / "app"


def use_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(APP.glob("use*.js"))}


def test_use_js_exports_renderUse():
    assert re.search(r"export\s+(async\s+)?function\s+renderUse\s*\(", (APP / "use.js").read_text(encoding="utf-8"))


def test_use_js_uses_only_existing_routes():
    routes = [r.path for r in admin_router(None).routes if r.path.startswith("/admin/api/")]
    assert routes
    patterns = [re.compile("^" + re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(p)) + "$") for p in routes]
    seen = set()
    for name, text in use_sources().items():
        for m in re.finditer(r"""["'`](/admin/api/[^"'`]*)["'`]""", text):
            path = re.sub(r"\$\{[^}]*\}", "x", m.group(1)).split("?")[0]
            seen.add(path)
            assert any(p.match(path) for p in patterns), f"{name}: no admin route for {m.group(1)}"
    assert seen, "the use tab names no /admin/api route"


def test_use_js_has_no_inline_handlers_or_scripts():
    for name, text in use_sources().items():
        assert not re.search(r"""\son[a-z]+\s*=""", text), f"{name}: inline event handler"
        assert "<script" not in text and "javascript:" not in text, f"{name}: inline script"


def test_invite_codes_use_a_shared_readonly_field_next_to_copy(run_app_js):
    run_app_js('''
import assert from "node:assert/strict";
import { codeFieldHtml } from "./common.js";
import { lenderParts } from "./use-lenders.js";
const code = 'cbx1.' + 'a'.repeat(240) + '&"<secret>';
const html = codeFieldHtml(code);
assert.match(html, /<div class="row code-row"><input readonly class="code-field"[^>]*><button[^>]*data-act="copy"[^>]*>/);
assert.ok(html.includes('value="cbx1.' + 'a'.repeat(240) + '&amp;&quot;&lt;secret&gt;"'));
assert.ok(!html.includes('<secret>') && !html.includes('<code'));
const parts = lenderParts({name: "scratch", invite_created: 1}, {open: true, code}, new Map());
assert.ok(parts.set.includes(html));
''')
    source = (APP / "use.js").read_text(encoding="utf-8")
    assert "codeFieldHtml(S.inviteCode)" in source
    for name, text in use_sources().items():
        assert not re.search(r'<code[^>]*class="[^"]*ellipsis', text), name


def test_use_js_reads_policy_warnings_from_overview():
    sources = "\n".join(use_sources().values())
    for field in ("policy_problem", "policy_error"):
        assert re.search(rf"\boverview\.{field}\b", sources), f"the use tab does not read {field}"


def test_lend_js_exports_renderLend():
    assert re.search(r"export\s+(async\s+)?function\s+renderLend\s*\(", (APP / "lend.js").read_text(encoding="utf-8"))


def test_lend_js_uses_only_existing_routes():
    app = create_ui_app(None, None, None, versions={}, data_root=Path("unused"))
    routes = ["/lend" + r.path for r in app.routes if r.path.startswith("/api/")]
    assert routes
    patterns = [re.compile("^" + re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(p)) + "$") for p in routes]
    seen = set()
    sources = sorted(APP.glob("lend*.js"))
    assert sources, "the lend tab has no script"
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for m in re.finditer(r"""["'`](/lend/api/[^"'`]*)["'`]""", text):
            path = re.sub(r"\$\{[^}]*\}", "x", m.group(1)).split("?")[0]
            seen.add(path)
            assert any(p.match(path) for p in patterns), f"{source.name}: no lender route for {m.group(1)}"
        assert not re.search(r"""\son[a-z]+\s*=""", text), f"{source.name}: inline event handler"
        assert "<script" not in text and "javascript:" not in text, f"{source.name}: inline script"
    assert seen, "the lend tab names no /lend/api route"


@pytest.fixture
def run_app_js(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the app renderers without a browser")
    # Use the real modules, with their browser URLs resolved inside this isolated directory.
    (tmp_path / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    for source in APP.glob("*.js"):
        text = source.read_text(encoding="utf-8").replace("/app/static/", "./")
        (tmp_path / source.name).write_text(text, encoding="utf-8", newline="\n")

    def run(script):
        result = subprocess.run([node, "--input-type=module", "-"], input=script, cwd=tmp_path,
                                text=True, encoding="utf-8", capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr

    return run


def test_folder_warnings_and_setup_errors_are_escaped(run_app_js):
    run_app_js('''
import assert from "node:assert/strict";
import { foldersHtml, emptyHtml } from "./use-pages.js";
const path = 'D:/work/<img src=x onerror="bad">';
const form = {main: path, folders: [{path, level: "write"}]};
const warning = "このフォルダは書き込めません <script>bad</script>";
const html = foldersHtml(form, warning);
assert.ok(html.includes("&lt;img"));
assert.ok(html.includes("&lt;script&gt;bad&lt;/script&gt;"));
assert.ok(!html.includes("<img") && !html.includes("<script>"));
assert.ok(html.includes('data-act="folder-remove"'));
assert.ok(!foldersHtml(form, "").includes('class="note-warn"'));
const error = emptyHtml(warning);
assert.ok(error.includes("&lt;script&gt;bad&lt;/script&gt;"));
assert.ok(error.includes("準備を始める"));
assert.ok(!emptyHtml(null).includes('class="note-warn"'));
''')


def test_policy_warnings_keep_wording_and_escape_folder_and_error(run_app_js):
    run_app_js('''
import assert from "node:assert/strict";
import { policyWarningsHtml } from "./use-pages.js";
const folder = 'D:/work/<img src=x onerror="bad">';
const html = policyWarningsHtml({policy_problem: [
  {folder, reason: "contains_data_dir"}, {folder: "D:/other", reason: "contains_data_dir"}
], policy_error: '<script>bad</script> & "error"'});
assert.ok(html.includes("共有フォルダの設定に、codex-bridge 自身のデータ（設定と記録）を含むフォルダがあります: "));
assert.ok(html.includes("。このままでは安全のため一部の操作ができないことがあります。"));
assert.equal((html.match(/このフォルダを共有から外す/g) || []).length, 2);
assert.ok(html.includes('data-act="folder-remove"'));
assert.ok(html.includes('data-path="D:/work/&lt;img src=x onerror=&quot;bad&quot;&gt;"'));
assert.ok(html.includes("policy.yaml を読めません（前の設定のまま動いています）: &lt;script&gt;bad&lt;/script&gt; &amp; &quot;error&quot;"));
assert.ok(!html.includes("<img") && !html.includes("<script>"));
assert.equal(policyWarningsHtml({policy_problem: [], policy_error: null}), "");
assert.equal(policyWarningsHtml({}), "");
''')


def test_shared_task_detail_escapes_both_sides_and_keeps_approval_perspective(run_app_js):
    run_app_js('''
import assert from "node:assert/strict";
import { headHtml, promptHtml, timelineHtml } from "./use-tasks.js";
const task = {task_id: "t", state: "awaiting_lender", created: 1,
  lender: '<img src=x>', prompt: '<script>title</script>\\n<script>body</script>'};
for (const side of ["use", "lend"]) {
  const head = headHtml(task, side === "lend", side);
  assert.ok(head.includes("&lt;img src=x&gt;"));
  assert.ok(head.includes("&lt;script&gt;title&lt;/script&gt;"));
  assert.ok(!head.includes("<script>") && !head.includes("<img"));
  const prompt = promptHtml(task);
  assert.ok(prompt.includes("&lt;script&gt;title&lt;/script&gt;"));
  assert.ok(prompt.includes("&lt;script&gt;body&lt;/script&gt;"));
  assert.ok(!prompt.includes("<script>"));
  const log = timelineHtml(task, [{kind:"agent_message", data:{text:"<script>message</script>"}}], "", side);
  assert.ok(log.includes("&lt;script&gt;message&lt;/script&gt;"));
  assert.ok(log.includes(side === "lend" ? "あなたの許可を待っています" : "相手の許可を待っています"));
}
''')


def test_command_rules_and_task_settings_escape_patterns(run_app_js):
    run_app_js('''
import assert from "node:assert/strict";
import { rulesHtml } from "./use-pages.js";
import { taskExecHtml, taskExecModeLabel } from "./use-tasks.js";
const pattern = '</textarea><img src=x onerror="bad">';
const global = rulesHtml({exec: {deny: [pattern], ask: [pattern], allow: [pattern]}});
const keys = Array.from(global.matchAll(/data-key="(deny|ask|allow)"/g), match => match[1]);
assert.deepEqual(keys, ["deny", "ask", "allow"]);
const task = {task_id: '<task>', state: "running", exec_mode: "ask", deny: [pattern], ask: [pattern]};
const active = taskExecHtml(task);
const finished = taskExecHtml({...task, state: "completed"});
for (const html of [global, active, finished]) {
  assert.ok(html.includes('&lt;/textarea&gt;&lt;img src=x onerror=&quot;bad&quot;&gt;'));
  assert.ok(!html.includes("<img"));
}
assert.ok(active.includes('data-id="&lt;task&gt;"'));
assert.match(active, /value="ask" selected/);
assert.equal(taskExecModeLabel("ask"), "毎回承認");
assert.equal((finished.match(/ readonly/g) || []).length, 2);
assert.ok(!finished.includes("<select"));
''')


def test_lender_setup_warning_is_escaped_and_shows_recovery_actions(run_app_js):
    run_app_js('''
import assert from "node:assert/strict";
import { setupErrorHtml } from "./lend-pages.js";
const html = setupErrorHtml('Codex <script>bad</script> & "offline"');
assert.ok(html.includes('class="note-warn"') && html.includes('role="alert"'));
assert.ok(html.includes('&lt;script&gt;bad&lt;/script&gt; &amp; &quot;offline&quot;'));
assert.ok(!html.includes('<script>'));
assert.ok(html.includes('招待コードを入れ直す') && html.includes('依頼主を追加'));
assert.equal(setupErrorHtml(null), '');
''')


def test_lender_tab_reads_setup_error_from_resident_state():
    text = (APP / "lend.js").read_text(encoding="utf-8")
    assert 'get("/app/api/state")' in text
    assert 'setupErrorHtml(appState.lend.error)' in text


def test_parallel_lender_cards_and_pending_items_are_escaped(run_app_js):
    run_app_js('''
import assert from "node:assert/strict";
import { lenderParts } from "./use-lenders.js";
import { connectionHead } from "./lend-cards.js";
import { pendingHtml } from "./lend.js";
const parts = lenderParts({name: "<alice>", connected:true, ready:true, current_task_id:"a",
  active_task_ids:["a","b"], lender_view: {max_parallel: 3, features:{}, pre_approval:{}, over_limit:"report"}},
  {open:true}, new Map());
assert.ok(parts.head.includes("2 件作業中"));
assert.ok(parts.set.includes("同時に受ける依頼"));
assert.ok(parts.set.includes('<option>3</option>'));
assert.ok(parts.set.includes("&lt;alice&gt;"));
const tasks = ["one","two"].map(task_id => ({task_id, connection_id:"c", state:"running", prompt:"task"}));
assert.ok(connectionHead({id:"c", host_name:"<host>", connected:true, busy:true}, false, tasks).includes("2 件作業中"));
const one = pendingHtml({task_id:'<one>', connection_id:'<host>', created:1, prompt:'<script>first</script>', kind:"start"});
const two = pendingHtml({task_id:'<two>', connection_id:'<host>', created:2, prompt:'<img src=x>', kind:"followup"});
for (const html of [one, two]) {
  assert.ok(!html.includes("<script>") && !html.includes("<img"));
  assert.equal((html.match(/data-act="decide"/g) || []).length, 2);
  assert.ok(html.includes('data-act="reason"') && html.includes('data-pending='));
}
assert.ok(one.includes("&lt;one&gt;") && two.includes("&lt;two&gt;"));
''')
    source = (APP / "lend.js").read_text(encoding="utf-8")
    assert 'data-act="parallel"' in source and 'length: 8' in source
    assert 'max_parallel: Number(input.value)' in source
    assert 'item.querySelector' in source and 'created: p.created' in source


def test_update_controls_use_external_handlers_and_escape_status():
    text = (APP / "log.js").read_text(encoding="utf-8")
    for control in ("update-mode", "update-check", "update-install"):
        assert f'id="{control}"' in text
    assert "message.innerHTML = esc(detail)" in text
    assert re.search(r"""\son[a-z]+\s*=""", text) is None
    assert "<script" not in text
    cards = (APP / "use-lenders.js").read_text(encoding="utf-8")
    assert 'l.connected ? "切り替え待ち" : "再起動中"' in cards
    assert "${esc(label)}</span>" in cards

