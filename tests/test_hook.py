"""Hook notices and long waits use fake HTTP and monotonic clocks."""

import io
import json
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from codex_bridge import hook


class Clock:
    def __init__(self):
        self.value = 100.0
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def approval():
    return {"kind": "approval", "task_id": "t12", "lender": "yamada",
            "model": "gpt-6.1-sol", "effort": "high",
            "approval": {"id": "a3", "kind": "command", "summary": "npm test", "cwd": "web"}}


@pytest.fixture
def final():
    return {"kind": "final", "task_id": "t12", "lender": "yamada",
            "model": "gpt-6.1-sol", "effort": "high", "state": "completed",
            "report": "修正しました", "report_truncated": False, "resume_at": None}


def claude_stdin():
    return json.dumps({"tool_name": "mcp__codex-bridge__send_followup",
                       "tool_input": {"task_id": "t12"}}).encode("utf-8")


def test_notice_text_approval(approval):
    assert hook.notice_text([approval]) == (
        "codex-bridge: タスク t12（担当: yamada、gpt-6.1-sol / high）が承認待ちです。"
        "承認 a3: コマンド実行 `npm test`（場所: web）。decide_approval で許可か拒否をしてください。"
    )


def test_notice_text_final_truncated_points_to_get_result(final):
    final.update(state="suspended_quota", report_truncated=True, resume_at="2026-11-19T12:00:00+09:00")
    assert hook.notice_text([final]) == (
        "codex-bridge: タスク t12（担当: yamada、gpt-6.1-sol / high）が終わりました（使用量の上限で中断）。"
        "報告: 修正しました…（続きは get_result で読めます）。再開できる時刻: 2026-11-19T12:00:00+09:00。"
        "get_result で詳しい結果と変更したファイルを確かめられます。"
    )


@pytest.mark.parametrize("state,label", [("completed", "完了"), ("failed", "失敗"),
                                         ("cancelled", "中止"), ("rejected_by_lender", "貸している人が拒否"),
                                         ("suspended_quota", "使用量の上限で中断")])
def test_notice_text_final_states_and_empty_report(final, state, label):
    final.update(model=None, effort=None, report=None, state=state)
    assert hook.notice_text([final]) == (
        f"codex-bridge: タスク t12（担当: yamada）が終わりました（{label}）。報告: （なし）。"
        "get_result で詳しい結果と変更したファイルを確かめられます。"
    )


def test_notice_text_delete_without_model_or_cwd_and_multiple_notices(approval, final):
    approval.update(model=None, effort=None)
    approval["approval"].update(kind="delete", summary="old.txt", cwd=None)
    first = ("codex-bridge: タスク t12（担当: yamada）が承認待ちです。承認 a3: 削除 `old.txt`。"
             "decide_approval で許可か拒否をしてください。")
    assert hook.notice_text([approval, final]) == first + "\n" + hook.notice_text([final])


def test_claude_query_send_task_reads_double_encoded_response():
    payload = {"tool_name": "mcp__codex-bridge__send_task",
               "tool_response": [{"type": "text", "text": "{\n  \"task_id\": \"t1\"}"}]}
    assert hook.claude_query(payload) == {"task_id": "t1"}


@pytest.mark.parametrize("response", [
    {"content": [{"type": "text", "text": '{"task_id": "t1"}'}]},
    '{"task_id": "t1"}',
    [{"type": "image"}, {"type": "text", "text": "bad JSON"},
     {"type": "text", "text": "[]"}, {"type": "text", "text": '{"task_id": "t1"}'}],
])
@pytest.mark.parametrize("tool", ["send_task", "decide_approval"])
def test_claude_query_response_forms(response, tool):
    assert hook.claude_query({"tool_name": hook.TOOL_PREFIX + tool, "tool_response": response}) == {
        "task_id": "t1"}


def test_claude_query_takes_first_dict_even_without_task_id():
    response = [{"type": "text", "text": '{}'}, {"type": "text", "text": '{"task_id": "t1"}'}]
    assert hook.claude_query({"tool_name": hook.TOOL_PREFIX + "send_task", "tool_response": response}) == {
        "origin": "claude", "any": "1"}


def test_claude_query_followup_uses_input():
    assert hook.claude_query({"tool_name": "mcp__codex-bridge__send_followup",
                              "tool_input": {"task_id": "t2"},
                              "tool_response": '{"task_id": "wrong"}'}) == {"task_id": "t2"}


@pytest.mark.parametrize("response", [None, "not JSON", {"isError": True, "content": [
    {"type": "text", "text": '{"error": "busy"}'}]}, '{"task_id": 123}'])
def test_claude_send_task_error_uses_any_mode(response):
    assert hook.claude_query({"tool_name": "mcp__codex-bridge__send_task", "tool_response": response}) == {
        "origin": "claude", "any": "1"}


def test_claude_other_tool_any_mode():
    assert hook.claude_query({"tool_name": "mcp__codex-bridge__get_result"}) == {
        "origin": "claude", "any": "1"}


@pytest.mark.parametrize("name", ["Bash", "mcp__other__send_task", None, 12])
def test_claude_non_bridge_tool_no_wait(clock, name):
    payload = {"tool_name": name}
    get = Mock(side_effect=AssertionError("must not wait"))
    assert hook.claude_query(payload) == {}
    assert hook.run_claude(json.dumps(payload).encode(), 8770, get=get,
                           sleep=clock.sleep, now=clock.now) == (0, b"")
    get.assert_not_called()


@pytest.mark.parametrize("session", ["s & 日本語", None, 17])
def test_codex_query_uses_string_session(session):
    expected = {"origin": "codex"}
    if isinstance(session, str):
        expected["session"] = session
    assert hook.codex_query({"session_id": session}) == expected
    assert hook.codex_query({}) == {"origin": "codex"}


def test_claude_stderr_is_utf8(clock, approval):
    get = Mock(return_value={"status": "notice", "notices": [approval]})
    code, err = hook.run_claude(claude_stdin(), 8770, get=get, sleep=clock.sleep, now=clock.now)
    assert code == 2
    assert "承認待ち" in err.decode("utf-8")
    assert err == hook.notice_text([approval]).encode("utf-8")
    url = urlsplit(get.call_args.args[0])
    assert (url.scheme, url.netloc, url.path) == ("http", "127.0.0.1:8770", "/admin/api/notify/wait")
    assert parse_qs(url.query) == {"task_id": ["t12"], "timeout": ["3300"]}
    assert get.call_args.kwargs == {"timeout": 3330}
    assert clock.sleeps == []


def test_claude_no_notice_silent(clock):
    get = Mock(return_value={"status": "none", "notices": []})
    assert hook.run_claude(claude_stdin(), 8770, get=get, sleep=clock.sleep, now=clock.now) == (0, b"")


def test_timeout_loops_then_silent_exit(clock):
    requests = []

    def get(url, timeout):
        requests.append((int(parse_qs(urlsplit(url).query)["timeout"][0]), timeout))
        clock.value += hook.CHUNK
        return {"status": "timeout", "notices": []}

    assert hook.run_claude(claude_stdin(), 8770, get=get, sleep=clock.sleep, now=clock.now) == (0, b"")
    assert len(requests) == 27
    assert all(0 < wait <= hook.CHUNK and http == wait + 30 for wait, http in requests)
    assert requests[-1][0] == 480
    assert clock.sleeps == []


@pytest.mark.parametrize("failure", [OSError("offline"), ValueError("invalid JSON"),
                                     {"status": "closing", "notices": []}])
def test_host_down_at_start_exits_0_without_retry(clock, failure):
    get = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
    assert hook.run_codex(b"{}", 8770, get=get, sleep=clock.sleep, now=clock.now) == (0, b"")
    assert get.call_count == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("failure", [OSError("offline"), ValueError("invalid JSON"),
                                     {"status": "closing", "notices": []}])
def test_reconnect_after_contact_retries_up_to_window(clock, failure):
    calls = []

    def get(url, timeout):
        calls.append(clock.now())
        if len(calls) == 1:
            return {"status": "timeout", "notices": []}
        if isinstance(failure, Exception):
            raise failure
        return failure

    assert hook.wait_notices(8770, {"origin": "codex"}, deadline=clock.now() + hook.HOOK_TIMEOUT,
                             get=get, sleep=clock.sleep, now=clock.now) is None
    assert clock.sleeps and all(s == hook.RETRY_EVERY for s in clock.sleeps)
    assert sum(clock.sleeps) == hook.RETRY_WINDOW
    assert clock.now() - calls[1] == hook.RETRY_WINDOW


def test_reconnect_success_resets_failure_window(clock, final):
    attempts = 0

    def get(url, timeout):
        nonlocal attempts
        attempts += 1
        if attempts in (1, 10):
            return {"status": "timeout", "notices": []}
        if attempts == 19:
            return {"status": "notice", "notices": [final]}
        raise OSError("restarting")

    assert hook.wait_notices(8770, {"origin": "codex"}, deadline=clock.now() + hook.HOOK_TIMEOUT,
                             get=get, sleep=clock.sleep, now=clock.now) == [final]
    assert sum(clock.sleeps) > hook.RETRY_WINDOW


def test_reconnect_does_not_sleep_past_deadline(clock):
    get = Mock(side_effect=[{"status": "timeout", "notices": []}, OSError("offline")])
    deadline = clock.now() + hook.MARGIN + 5
    assert hook.wait_notices(8770, {"origin": "codex"}, deadline=deadline,
                             get=get, sleep=clock.sleep, now=clock.now) is None
    assert clock.now() <= deadline - hook.MARGIN
    assert get.call_count == 2


def test_codex_output_is_ascii_json(clock, final):
    get = Mock(return_value={"status": "notice", "notices": [final]})
    code, out = hook.run_codex(json.dumps({"session_id": "s & 日本語"}).encode(), 9123,
                               get=get, sleep=clock.sleep, now=clock.now)
    assert code == 0
    assert out.isascii()
    result = json.loads(out)
    assert result["decision"] == "block"
    assert "終わりました" in result["reason"]
    assert result["reason"] == hook.notice_text([final])
    url = urlsplit(get.call_args.args[0])
    assert url.netloc == "127.0.0.1:9123"
    assert parse_qs(url.query) == {"origin": ["codex"], "session": ["s & 日本語"], "timeout": ["3300"]}


@pytest.mark.parametrize("status", ["none", "gone"])
def test_codex_none_exit0(clock, status):
    get = Mock(return_value={"status": status, "notices": []})
    assert hook.run_codex(b"{}", 8770, get=get, sleep=clock.sleep, now=clock.now) == (0, b"")
    get.assert_called_once()


@pytest.mark.parametrize("runner", ["run_claude", "run_codex"])
@pytest.mark.parametrize("stdin", [b"", b"{", b"\xff", b"null", b"[]", b'"text"'])
def test_bad_stdin_exit0(clock, runner, stdin):
    get = Mock(side_effect=AssertionError("must not wait"))
    assert getattr(hook, runner)(stdin, 8770, get=get, sleep=clock.sleep, now=clock.now) == (0, b"")
    get.assert_not_called()


@pytest.mark.parametrize("caller,code", [("claude", 2), ("codex", 0)])
def test_main_writes_and_flushes_only_callers_binary_stream(monkeypatch, caller, code):
    stdin = SimpleNamespace(buffer=io.BytesIO(b"{}"))
    stdout = SimpleNamespace(buffer=Mock())
    stderr = SimpleNamespace(buffer=Mock())
    monkeypatch.setattr(hook.sys, "stdin", stdin)
    monkeypatch.setattr(hook.sys, "stdout", stdout)
    monkeypatch.setattr(hook.sys, "stderr", stderr)
    runner = Mock(return_value=(code, b"notice"))
    monkeypatch.setattr(hook, "run_" + caller, runner)
    assert hook.main([caller, "--port", "9123"]) == code
    runner.assert_called_once_with(b"{}", 9123)
    target, other = (stderr, stdout) if caller == "claude" else (stdout, stderr)
    target.buffer.write.assert_called_once_with(b"notice")
    target.buffer.flush.assert_called_once_with()
    other.buffer.write.assert_not_called()


@pytest.mark.parametrize("argv", [[], ["other"], ["codex", "--port", "bad"],
                                  ["codex", "--port"], ["codex", "--unknown"], ["--help"]])
def test_main_bad_arguments_silent(argv, capsys):
    assert hook.main(argv) == 0
    assert capsys.readouterr() == ("", "")


def test_main_unexpected_exception_silent(monkeypatch, capsys):
    monkeypatch.setattr(hook.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}")))
    monkeypatch.setattr(hook, "run_codex", Mock(side_effect=RuntimeError("unexpected")))
    assert hook.main(["codex"]) == 0
    assert capsys.readouterr() == ("", "")


def test_http_get_loads_json_with_timeout(monkeypatch):
    response = io.BytesIO(b'{"status": "none", "notices": []}')
    urlopen = Mock(return_value=response)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert hook._http_get("http://127.0.0.1:8770/admin/api/notify/wait?origin=codex", 3330) == {
        "status": "none", "notices": []}
    urlopen.assert_called_once_with("http://127.0.0.1:8770/admin/api/notify/wait?origin=codex", timeout=3330)
    assert response.closed
