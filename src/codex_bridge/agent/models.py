"""Codex's model catalog and the lender's per-connection model / effort limits."""

from __future__ import annotations

from ..common.protocol import EFFORT_ORDER


class ModelRefused(Exception):
    pass


def parse_model_list(entries: list[dict]) -> list[dict]:
    catalog = []
    for e in entries:
        if e.get("hidden") is True:
            continue
        model = e.get("model")
        if not isinstance(model, str) or not model:
            model = e.get("id")
        if not isinstance(model, str) or not model:
            continue
        catalog.append({"model": model, "name": e.get("displayName") or model,
                        "efforts": [o["reasoningEffort"] for o in e.get("supportedReasoningEfforts", [])],
                        "default_effort": e.get("defaultReasoningEffort"),
                        "is_default": e.get("isDefault") is True})
    return catalog


def apply_config_default(catalog: list[dict], config: dict) -> list[dict]:
    result = [dict(m) for m in catalog]
    model = config.get("model")
    if isinstance(model, str) and any(m["model"] == model for m in result):
        for m in result:
            m["is_default"] = m["model"] == model
    effort = config.get("model_reasoning_effort")
    if isinstance(effort, str):
        for m in result:
            if m["is_default"]:
                m["default_effort"] = effort
    return result


def effort_allowed(effort: str, max_effort: str) -> bool:
    return (effort in EFFORT_ORDER and max_effort in EFFORT_ORDER
            and EFFORT_ORDER.index(effort) <= EFFORT_ORDER.index(max_effort))


def offered_models(catalog: list[dict] | None, allowed: list[str] | None, max_effort: str) -> list[dict] | None:
    if catalog is None:
        return None
    offered = [{**m, "efforts": [e for e in m["efforts"] if effort_allowed(e, max_effort)]}
               for m in catalog if allowed is None or m["model"] in allowed]
    offered = [m for m in offered if m["efforts"]]
    default = next((i for i, m in enumerate(offered) if m["is_default"]), 0)
    for i, m in enumerate(offered):
        m["is_default"] = i == default
    return offered


def choose(catalog: list[dict] | None, allowed: list[str] | None, max_effort: str,
           model: str | None, effort: str | None) -> tuple[str | None, str]:
    if catalog is None:
        if allowed is not None:
            raise ModelRefused("この PC の Codex のモデルの一覧を取得できないため、モデルを制限している設定では依頼を受けられません。")
        if model is not None:
            raise ModelRefused("この PC の Codex のモデルの一覧を取得できないため、モデルを指定した依頼は受けられません。"
                               "モデルを指定せずに依頼してください。")
        if effort is not None:
            if not effort_allowed(effort, max_effort):
                raise ModelRefused(f"推論の強さ「{effort}」は使えません。上限は {max_effort} です。")
        else:
            effort = "medium" if effort_allowed("medium", max_effort) else max_effort
        return None, effort

    permitted = [m for m in catalog if allowed is None or m["model"] in allowed]
    if not permitted:
        raise ModelRefused("貸している人が使わせているモデルがありません。")
    offered = offered_models(permitted, None, max_effort)
    if not offered:
        raise ModelRefused(f"推論の強さの上限（{max_effort}）以下で使えるモデルがありません。"
                           "貸している人に上限を見直してもらってください。")
    if model is None:
        target = next(m for m in offered if m["is_default"])
    else:
        target = next((m for m in offered if m["model"] == model), None)
        if target is None:
            if any(m["model"] == model for m in permitted):
                raise ModelRefused(f"指定のモデル（{model}）には、推論の強さの上限（{max_effort}）以下で使える段階がありません。"
                                   "使えるモデル: " + "、".join(m["model"] for m in offered))
            raise ModelRefused(f"指定のモデル（{model}）は、貸している人が使わせていません。使えるモデル: "
                               + "、".join(m["model"] for m in offered))
    if effort is not None:
        if effort not in target["efforts"]:
            available = "・".join(target["efforts"]) or "なし"
            raise ModelRefused(f"推論の強さ「{effort}」は使えません。{target['model']} で使える推論の強さ: {available}")
    else:
        effort = target["default_effort"] if target["default_effort"] is not None else "medium"
        if effort not in target["efforts"]:
            effort = target["efforts"][-1]
    return target["model"], effort
