"""Catalog parsing and lender-side model / effort choices."""

import copy

import pytest

from codex_bridge.agent.models import (ModelRefused, choose, effort_allowed, offered_models,
                                       parse_model_list)


def entry(model="m1", *, efforts=None, default="medium", is_default=True):
    return {"model": model, "name": model.upper(),
            "efforts": ["low", "medium", "high", "xhigh", "max"] if efforts is None else efforts,
            "default_effort": default, "is_default": is_default}


def test_parse_skips_hidden_and_reads_efforts():
    raw = [{"id": "other-id", "model": "m1", "displayName": "Model One", "hidden": False,
            "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": ""},
                                          {"reasoningEffort": "high", "description": ""}],
            "defaultReasoningEffort": "high", "isDefault": True},
           {"id": "hidden", "hidden": True},
           {"id": "m2", "displayName": "", "supportedReasoningEfforts": [], "isDefault": 1}]
    assert parse_model_list(raw) == [
        {"model": "m1", "name": "Model One", "efforts": ["low", "high"],
         "default_effort": "high", "is_default": True},
        {"model": "m2", "name": "m2", "efforts": [], "default_effort": None, "is_default": False}]


def test_effort_order_and_unknown_names():
    order = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
    for i, effort in enumerate(order):
        for j, limit in enumerate(order):
            assert effort_allowed(effort, limit) is (i <= j)
    assert effort_allowed("ultra", "xhigh") is False
    assert effort_allowed("weird", "ultra") is False
    assert effort_allowed("low", "weird") is False
    assert effort_allowed("low", "xhigh") is True


def test_offer_cuts_efforts_to_max():
    catalog = [entry(efforts=["low", "high", "xhigh", "max", "ultra", "weird"])]
    original = copy.deepcopy(catalog)
    assert offered_models(catalog, None, "xhigh") == [
        {**catalog[0], "efforts": ["low", "high", "xhigh"]}]
    assert catalog == original
    assert offered_models(None, ["m1"], "xhigh") is None
    assert offered_models(catalog, [], "xhigh") == []


def test_offer_default_falls_back_to_first_allowed():
    catalog = [entry(), entry("m2", is_default=False), entry("m3", is_default=False)]
    offered = offered_models(catalog, ["m2", "m3"], "xhigh")
    assert [m["model"] for m in offered] == ["m2", "m3"]
    assert [m["model"] for m in offered if m["is_default"]] == ["m2"]
    assert offered_models(catalog, ["m3", "m2"], "xhigh") == offered
    assert [m["model"] for m in offered_models(catalog, None, "xhigh") if m["is_default"]] == ["m1"]
    assert choose(catalog, ["m2", "m3"], "xhigh", None, None) == ("m2", "medium")


def test_choose_default_model_and_effort():
    catalog = [entry("m2", is_default=False), entry()]
    assert choose(catalog, None, "xhigh", None, None) == ("m1", "medium")
    assert choose(catalog, None, "xhigh", "m2", "low") == ("m2", "low")


def test_choose_caps_default_effort_to_max():
    assert choose([entry(default="max")], None, "xhigh", None, None) == ("m1", "xhigh")


@pytest.mark.parametrize("efforts,default,expected", [
    (["low", "medium", "high", "max"], "max", "high"),
    (["low", "high"], None, "high"),
])
def test_choose_never_picks_unoffered_effort(efforts, default, expected):
    assert choose([entry(efforts=efforts, default=default)], None, "xhigh", None, None) == ("m1", expected)


@pytest.mark.parametrize("model,allowed,available", [("unknown", None, "m1、m2"), ("m1", ["m2"], "m2")])
def test_choose_refuses_unknown_model(model, allowed, available):
    with pytest.raises(ModelRefused) as exc:
        choose([entry(), entry("m2", is_default=False)], allowed, "xhigh", model, None)
    assert str(exc.value) == f"指定のモデル（{model}）は、貸している人が使わせていません。使えるモデル: {available}"


@pytest.mark.parametrize("effort", ["max", "ultra", "weird"])
def test_choose_refuses_effort_above_limit(effort):
    with pytest.raises(ModelRefused) as exc:
        choose([entry()], None, "xhigh", None, effort)
    assert str(exc.value) == f"推論の強さ「{effort}」は使えません。m1 で使える推論の強さ: low・medium・high・xhigh"


@pytest.mark.parametrize("allowed", [[], ["m1"]])
def test_choose_no_catalog_limited_refuses(allowed):
    with pytest.raises(ModelRefused) as exc:
        choose(None, allowed, "xhigh", "m1", None)
    assert str(exc.value) == "この PC の Codex のモデルの一覧を取得できないため、モデルを制限している設定では依頼を受けられません。"


def test_choose_no_catalog_plain_request_gets_medium():
    assert choose(None, None, "xhigh", None, None) == (None, "medium")
    assert choose(None, None, "low", None, None) == (None, "low")
    assert choose(None, None, "high", None, "low") == (None, "low")


def test_choose_no_catalog_model_refused():
    with pytest.raises(ModelRefused) as exc:
        choose(None, None, "xhigh", "m1", None)
    assert str(exc.value) == ("この PC の Codex のモデルの一覧を取得できないため、モデルを指定した依頼は受けられません。"
                              "モデルを指定せずに依頼してください。")


@pytest.mark.parametrize("effort", ["max", "weird"])
def test_choose_no_catalog_refuses_disallowed_effort(effort):
    with pytest.raises(ModelRefused) as exc:
        choose(None, None, "xhigh", None, effort)
    assert str(exc.value) == f"推論の強さ「{effort}」は使えません。上限は xhigh です。"


@pytest.mark.parametrize("catalog,allowed", [([], None), ([entry()], [])])
def test_choose_refuses_empty_offer(catalog, allowed):
    with pytest.raises(ModelRefused) as exc:
        choose(catalog, allowed, "xhigh", None, None)
    assert str(exc.value) == "貸している人が使わせているモデルがありません。"


def test_choose_refuses_effort_unsupported_by_model():
    with pytest.raises(ModelRefused) as exc:
        choose([entry(efforts=["low", "high"])], None, "xhigh", None, "medium")
    assert str(exc.value) == "推論の強さ「medium」は使えません。m1 で使える推論の強さ: low・high"


def test_choose_empty_efforts_uses_limit_only_for_an_omitted_effort():
    catalog = [entry(efforts=[])]
    assert choose(catalog, None, "xhigh", None, None) == ("m1", "xhigh")
    with pytest.raises(ModelRefused) as exc:
        choose(catalog, None, "xhigh", None, "low")
    assert str(exc.value) == "推論の強さ「low」は使えません。m1 で使える推論の強さ: なし"
