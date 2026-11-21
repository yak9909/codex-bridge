"""Catalog parsing and lender-side model / effort choices."""

import copy

import pytest

from codex_bridge.agent.models import (ModelRefused, choose, effort_allowed, offered_models,
                                       parse_model_list)


def entry(model="m1", *, efforts=None, default="medium", is_default=True):
    return {"model": model, "name": model.upper(),
            "efforts": ["low", "medium", "high", "xhigh", "max"] if efforts is None else efforts,
            "default_effort": default, "is_default": is_default}


@pytest.mark.parametrize("config,defaults,efforts", [
    ({"model": "m2", "model_reasoning_effort": "high"}, [False, True], ["medium", "high"]),
    ({"model_reasoning_effort": "high"}, [True, False], ["high", "medium"]),
    ({"model": "unknown"}, [True, False], ["medium", "medium"]),
    ({}, [True, False], ["medium", "medium"]),
    ({"model": None, "model_reasoning_effort": None}, [True, False], ["medium", "medium"]),
    ({"model": 1, "model_reasoning_effort": 1}, [True, False], ["medium", "medium"]),
])
def test_apply_config_default(config, defaults, efforts):
    from codex_bridge.agent import models

    catalog = [entry(), entry("m2", is_default=False)]
    original = copy.deepcopy(catalog)
    result = models.apply_config_default(catalog, config)
    assert [m["is_default"] for m in result] == defaults
    assert [m["default_effort"] for m in result] == efforts
    assert catalog == original
    assert all(new is not old for new, old in zip(result, catalog))


def test_apply_config_default_still_respects_lender_limits():
    from codex_bridge.agent import models

    catalog = models.apply_config_default([entry(), entry("m2", is_default=False)],
                                         {"model": "m2", "model_reasoning_effort": "max"})
    assert choose(catalog, None, "high", None, None) == ("m2", "high")
    assert choose(catalog, ["m1"], "high", None, None) == ("m1", "medium")


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


@pytest.mark.parametrize("raw", [{}, {"model": ""}, {"model": None, "id": ""},
                               {"model": 3, "id": False}, {"id": []}])
def test_parse_skips_entries_without_a_nonempty_model_or_id(raw):
    assert parse_model_list([raw, {"model": "m1"}]) == [
        {"model": "m1", "name": "m1", "efforts": [], "default_effort": None, "is_default": False}]


@pytest.mark.parametrize("model", [None, "", 3, False])
def test_parse_falls_back_to_a_nonempty_string_id(model):
    assert parse_model_list([{"model": model, "id": "m1"}]) == [
        {"model": "m1", "name": "m1", "efforts": [], "default_effort": None, "is_default": False}]


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


@pytest.mark.parametrize("efforts", [[], ["high", "xhigh"], ["weird"]])
def test_offer_drops_models_without_efforts_under_limit(efforts):
    catalog = [entry(), entry("m2", efforts=efforts, is_default=False)]
    original = copy.deepcopy(catalog)
    assert offered_models(catalog, None, "low") == [{**catalog[0], "efforts": ["low"]}]
    assert catalog == original


def test_offer_default_moves_to_first_remaining_after_effort_cut():
    catalog = [entry(efforts=["high"]), entry("m2", is_default=False), entry("m3", is_default=False)]
    original = copy.deepcopy(catalog)
    offered = offered_models(catalog, ["m3", "m1", "m2"], "low")
    assert offered == [{**catalog[1], "efforts": ["low"], "is_default": True},
                       {**catalog[2], "efforts": ["low"]}]
    assert choose(catalog, None, "low", None, None) == ("m2", "low")
    assert catalog == original


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
    assert choose(None, None, "minimal", None, None) == (None, "minimal")
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


@pytest.mark.parametrize("catalog,allowed", [([], None), ([entry()], []), ([entry()], ["unknown"])])
@pytest.mark.parametrize("max_effort", ["xhigh", "minimal"])
def test_choose_refuses_empty_offer(catalog, allowed, max_effort):
    with pytest.raises(ModelRefused) as exc:
        choose(catalog, allowed, max_effort, None, None)
    assert str(exc.value) == "貸している人が使わせているモデルがありません。"


def test_choose_refuses_effort_unsupported_by_model():
    with pytest.raises(ModelRefused) as exc:
        choose([entry(efforts=["low", "high"])], None, "xhigh", None, "medium")
    assert str(exc.value) == "推論の強さ「medium」は使えません。m1 で使える推論の強さ: low・high"


@pytest.mark.parametrize("efforts,max_effort", [([], "xhigh"), (["low", "medium", "high"], "minimal")])
@pytest.mark.parametrize("allowed", [None, ["m1"]])
@pytest.mark.parametrize("model,effort", [(None, None), ("m1", "low"), ("unknown", None)])
def test_choose_refuses_when_effort_cut_removes_all_models(efforts, max_effort, allowed, model, effort):
    with pytest.raises(ModelRefused) as exc:
        choose([entry(efforts=efforts)], allowed, max_effort, model, effort)
    assert str(exc.value) == (f"推論の強さの上限（{max_effort}）以下で使えるモデルがありません。"
                              "貸している人に上限を見直してもらってください。")


@pytest.mark.parametrize("allowed", [None, ["m3", "m2", "m1"]])
def test_choose_refuses_named_model_removed_by_effort_cut(allowed):
    catalog = [entry(efforts=["high"]), entry("m2", is_default=False), entry("m3", is_default=False)]
    with pytest.raises(ModelRefused) as exc:
        choose(catalog, allowed, "low", "m1", None)
    assert str(exc.value) == ("指定のモデル（m1）には、推論の強さの上限（low）以下で使える段階がありません。"
                              "使えるモデル: m2、m3")


@pytest.mark.parametrize("model,allowed", [("unknown", None), ("m1", ["m2", "m3"])])
def test_choose_refuses_unknown_or_not_allowed_model_after_effort_cut(model, allowed):
    catalog = [entry(efforts=["high"]), entry("m2", is_default=False), entry("m3", is_default=False)]
    with pytest.raises(ModelRefused) as exc:
        choose(catalog, allowed, "low", model, None)
    assert str(exc.value) == f"指定のモデル（{model}）は、貸している人が使わせていません。使えるモデル: m2、m3"
