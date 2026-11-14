import pytest

from codex_bridge.common.invite import Invite, make_invite, parse_invite
from codex_bridge.common import protocol


def test_invite_roundtrip():
    c = make_invite("http://100.64.0.1:8765", "tok", "friend")
    assert c.startswith("cbx1.") and parse_invite(c) == Invite("http://100.64.0.1:8765", "tok", "friend")


def test_invite_tolerates_whitespace():
    c = make_invite("http://h:1", "t", "n")
    assert parse_invite(f"  {c}\n") == Invite("http://h:1", "t", "n")


@pytest.mark.parametrize("bad", ["hello", "cbx1.", "cbx1.!!!", "cbx2.e30"])
def test_invite_garbage_is_clear_error(bad):
    with pytest.raises(ValueError, match="招待コード"):
        parse_invite(bad)


def test_protocol_roundtrip_and_validation():
    s = protocol.encode(protocol.msg("task_start", task_id="t1", prompt="p", continue_thread_id=None))
    m = protocol.decode(s)
    assert m == {"type": "task_start", "task_id": "t1", "prompt": "p", "continue_thread_id": None}
    with pytest.raises(ValueError):
        protocol.decode('{"type": "nope"}')
    with pytest.raises(ValueError):
        protocol.decode("not json")
