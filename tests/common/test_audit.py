import json

from codex_bridge.common.audit import AuditLog


def test_audit_appends_json_lines(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    log.write("tool_call", tool="read_file", path="x.txt")
    log.write("tool_call", tool="grep")
    lines = (tmp_path / "a.jsonl").read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert len(lines) == 2 and first["tool"] == "read_file" and first["kind"] == "tool_call"
    assert "ts" in first


def test_audit_creates_parent_dir_and_keeps_japanese(tmp_path):
    log = AuditLog(tmp_path / "logs" / "a.jsonl")
    log.write("note", text="日本語")
    assert "日本語" in (tmp_path / "logs" / "a.jsonl").read_text(encoding="utf-8")
