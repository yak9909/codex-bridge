import json

import pytest

from codex_bridge.common import protocol

NAN, INF = float("nan"), float("inf")


def test_lender_models_is_a_known_type():
    assert protocol.decode('{"type":"lender_models","models":[]}')["type"] == "lender_models"


def test_protocol_4_carries_the_lender_view():
    assert protocol.PROTOCOL == 4
    view = {"features": {"goals": True}, "pre_approval": {"new_task": True, "followup": False, "timeout_min": None},
            "resumable": True, "over_limit": "report", "max_effort": "high", "models_all": None}
    raw = protocol.encode(protocol.msg("lender_models", models=None, max_effort="high", lender_view=view))
    assert protocol.decode(raw)["lender_view"] == view


def test_effort_order():
    assert protocol.EFFORT_ORDER == ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999", "1E+400", "123456789e999"])
def test_decode_lets_no_number_through_that_json_cannot_carry(number):
    # json.loads accepts NaN / Infinity, and turns 1e999 into inf: none of them may reach a page or a file
    m = protocol.decode('{"type": "event", "seq": 1, "task_id": "t1", "kind": "status", "ts": %s, '
                        '"data": {"text": %s, "list": [1, %s, {"deep": [%s]}], "ok": 1.5}}' % ((number,) * 4))
    assert m["ts"] is None and m["data"] == {"text": None, "list": [1, None, {"deep": [None]}], "ok": 1.5}
    assert m["type"] == "event" and m["seq"] == 1 and m["task_id"] == "t1"
    json.dumps(m, allow_nan=False)


def test_decode_keeps_ordinary_values():
    raw = {"type": "usage", "five_hour": {"used_percent": 12.5, "resets_at": 1_800_000_000}, "weekly": None,
           "ts": 0, "text": "NaN と Infinity という文字", "big": 10 ** 30, "small": -1e-300, "flag": True}
    assert protocol.decode(protocol.encode(raw)) == raw


@pytest.mark.parametrize("text", ["", "{", "[1, 2]", '"hello"', "NaN", '{"type": "nope"}', '{"type": NaN}',
                                  '{"type": "event", "n": ' + "9" * 5000 + "}",   # too many digits to read
                                  '{"type": "event", "x": ' + "[" * 100_000 + "]" * 100_000 + "}"])
def test_decode_refuses_what_is_not_a_message(text):
    with pytest.raises(ValueError):
        protocol.decode(text)


def test_finite_replaces_only_the_numbers_json_cannot_carry():
    value = {"a": NAN, "b": [INF, -INF, 1.0, 2, "x", None, True], "c": {"d": {"e": NAN, "f": "NaN"}}, "g": 10 ** 400}
    assert protocol.finite(value) == {"a": None, "b": [None, None, 1.0, 2, "x", None, True],
                                      "c": {"d": {"e": None, "f": "NaN"}}, "g": 10 ** 400}
    assert protocol.finite(NAN) is None and protocol.finite(3.5) == 3.5 and protocol.finite("x") == "x"
    assert value["a"] is not None  # the given value itself is left alone
