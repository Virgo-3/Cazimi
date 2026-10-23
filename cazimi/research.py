"""Pure, source-aware investment research decisions.

The engine evaluates explicitly supplied numerical observations. Document prose
and model output are never promoted into facts. Every suggestion is a research
conclusion; this module does not submit orders or claim investment skill.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import re
from typing import Any


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_TICKER = re.compile(r"[A-Z0-9][A-Z0-9.^=-]{0,23}\Z")
_OPS = {"gte", "gt", "lte", "lt"}
_ORIGINS = {"draft", "approved", "inherited"}
_QUALITIES = {"primary", "secondary", "user"}


def _text(value: Any, label: str, maximum: int = 4000, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    value = value.strip()
    if len(value) > maximum or (not value and not empty):
        raise ValueError(f"{label} must contain {'0' if empty else '1'} to {maximum} characters")
    if any(ord(char) < 32 and char not in "\n\t\r" for char in value):
        raise ValueError(f"{label} contains control characters")
    return value


def _identifier(value: Any, label: str) -> str:
    value = _text(value, label, 64)
    if not _ID.fullmatch(value):
        raise ValueError(f"{label} must use letters, digits, dots, underscores or hyphens")
    return value


def _number(value: Any, label: str, minimum: float | None = None,
            maximum: float | None = None, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    if ((minimum is not None and result < minimum)
            or (maximum is not None and result > maximum)
            or (positive and result <= 0)):
        raise ValueError(f"{label} is outside its permitted range")
    return result


def _date(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f"{label} must be an ISO date with a timezone")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date with a timezone") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    try:
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{label} is outside the supported UTC date range") from exc


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _expiry(when: datetime, seconds: float) -> datetime:
    try:
        return when + timedelta(seconds=seconds)
    except OverflowError:
        return datetime.max.replace(tzinfo=timezone.utc)


def _object(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _origin(value: Any, label: str) -> str:
    if not isinstance(value, str) or value not in _ORIGINS:
        raise ValueError(f"{label} must be draft, approved or inherited")
    return value


def _threshold(value: Any, label: str) -> dict | None:
    if value is None:
        return None
    value = _object(value, label)
    if not isinstance(value.get("op"), str) or value.get("op") not in _OPS:
        raise ValueError(f"{label} op must be gte, gt, lte or lt")
    return {"op": value["op"], "value": _number(value.get("value"), f"{label} value")}


def validate_watch(raw: dict) -> dict:
    """Return an independent, bounded, closed-schema watch configuration.

    All thresholds start as drafts. Approval is an explicit configuration
    decision; neither imported observations nor model text can approve one.
    """
    raw = _object(raw, "watch")
    ticker = _text(raw.get("ticker"), "ticker", 24).upper()
    if not _TICKER.fullmatch(ticker):
        raise ValueError("ticker has unsupported characters")
    raw_pillars = raw.get("pillars", [])
    if not isinstance(raw_pillars, list) or len(raw_pillars) > 64:
        raise ValueError("pillars must be a list of at most 64 items")
    pillars, seen = [], set()
    for index, item in enumerate(raw_pillars):
        item = _object(item, f"pillar {index + 1}")
        identifier = _identifier(item.get("id"), "pillar id")
        if identifier in seen:
            raise ValueError(f"duplicate pillar id: {identifier}")
        seen.add(identifier)
        core = item.get("core", True)
        if not isinstance(core, bool):
            raise ValueError("pillar core must be a boolean")
        source_ids = item.get("source_ids", [])
        if not isinstance(source_ids, list) or len(source_ids) > 32:
            raise ValueError("pillar source_ids must be a list of at most 32 items")
        sources = [_identifier(source, "pillar source_id") for source in source_ids]
        if len(sources) != len(set(sources)):
            raise ValueError("pillar source_ids must be unique")
        pillars.append({
            "id": identifier, "claim": _text(item.get("claim"), "pillar claim"),
            "metric": _text(item.get("metric"), "pillar metric", 128),
            "unit": _text(item.get("unit"), "pillar unit", 64),
            "period": _text(item.get("period"), "pillar period", 128),
            "source_ids": sources, "core": core,
            "confirm": _threshold(item.get("confirm"), "confirm threshold"),
            "warning": _threshold(item.get("warning"), "warning threshold"),
            "kill": _threshold(item.get("kill"), "kill threshold"),
            "threshold_origin": _origin(item.get("threshold_origin", "draft"), "threshold_origin"),
            "conflict_tolerance": _number(item.get("conflict_tolerance", 0), "conflict_tolerance", 0),
        })
    valuation = raw.get("valuation")
    if valuation is not None:
        valuation = _object(valuation, "valuation")
        currency = _text(valuation.get("currency"), "valuation currency", 3).upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("valuation currency must be a three-letter code")
        scenarios = valuation.get("scenarios")
        if not isinstance(scenarios, list) or len(scenarios) != 3:
            raise ValueError("valuation must have exactly bull, base and bear scenarios")
        by_name = {}
        for scenario in scenarios:
            scenario = _object(scenario, "scenario")
            name = scenario.get("name")
            if not isinstance(name, str) or name not in {"bull", "base", "bear"} or name in by_name:
                raise ValueError("scenario names must be unique bull, base and bear")
            by_name[name] = {
                "name": name,
                "probability": _number(scenario.get("probability"), "scenario probability", 0, 1),
                "price": _number(scenario.get("price"), "scenario price", positive=True),
            }
        if not math.isclose(math.fsum(item["probability"] for item in by_name.values()), 1.0,
                            rel_tol=0, abs_tol=1e-9):
            raise ValueError("scenario probabilities must sum to 1")
        valuation = {
            "currency": currency, "as_of": _iso(_date(valuation.get("as_of"), "valuation as_of")),
            "scenarios": [by_name[name] for name in ("bull", "base", "bear")],
            "hurdle_return": _number(valuation.get("hurdle_return", .1), "hurdle_return", 0),
            "max_loss_fraction": _number(valuation.get("max_loss_fraction", .3), "max_loss_fraction", 0, 1),
        }
    position = _object(raw.get("position", {}), "position")
    position = {
        "current_weight": _number(position.get("current_weight", 0), "current_weight", 0, 1),
        "max_weight": _number(position.get("max_weight", .1), "max_weight", 0, 1),
    }
    catalyst = raw.get("catalyst")
    if catalyst is not None:
        catalyst = _object(catalyst, "catalyst")
        catalyst = {
            "name": _text(catalyst.get("name"), "catalyst name", 1000),
            "due_at": _iso(_date(catalyst.get("due_at"), "catalyst due_at")),
            "information_value_per_share": _number(catalyst.get("information_value_per_share", 0), "information_value_per_share", 0),
            "cost_per_share": _number(catalyst.get("cost_per_share", 0), "cost_per_share", 0),
            "delay_cost_per_share": _number(catalyst.get("delay_cost_per_share", 0), "delay_cost_per_share", 0),
            "threshold_origin": _origin(catalyst.get("threshold_origin", "draft"), "catalyst threshold_origin"),
        }
    result = {
        "id": _identifier(raw.get("id"), "watch id"), "ticker": ticker,
        "thesis": _text(raw.get("thesis"), "thesis", 8000), "pillars": pillars,
        "valuation": valuation, "position": position, "catalyst": catalyst,
    }
    horizon = raw.get("horizon_days", 365)
    if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 3650:
        raise ValueError("horizon_days must be an integer between 1 and 3650")
    result["horizon_days"] = horizon
    for key, default in (("quote_max_age_seconds", 86400),
                         ("valuation_max_age_seconds", 2592000),
                         ("max_observation_age_seconds", 7776000)):
        result[key] = _number(raw.get(key, default), key, positive=True,
                              maximum=315576000)
    return result


def _observation(raw: Any) -> dict:
    raw = _object(raw, "observation")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in {"metric", "price", "document"}:
        raise ValueError("observation kind must be metric, price or document")
    quality = raw.get("source_quality")
    if not isinstance(quality, str) or quality not in _QUALITIES:
        raise ValueError("observation source_quality must be primary, secondary or user")
    result = {
        "id": _text(raw.get("id"), "observation id", 256),
        "ticker": _text(raw.get("ticker"), "observation ticker", 24).upper(),
        "kind": kind, "as_of": _iso(_date(raw.get("as_of"), "observation as_of")),
        "source_id": _identifier(raw.get("source_id"), "observation source_id"),
        "source_quality": quality,
        "citation": _text(raw.get("citation"), "observation citation", 2048),
        "title": _text(raw.get("title", ""), "observation title", 2000, empty=True),
        "metric": _text(raw.get("metric", ""), "observation metric", 128, empty=kind == "document"),
        "unit": _text(raw.get("unit", ""), "observation unit", 64, empty=kind == "document"),
        "period": _text(raw.get("period", ""), "observation period", 128, empty=kind != "metric"),
        "value": None if kind == "document" else _number(raw.get("value"), "observation value"),
    }
    if kind == "price" and result["value"] <= 0:
        raise ValueError("price observation value must be positive")
    return result


def _hit(value: float, threshold: dict | None) -> bool:
    if threshold is None:
        return False
    target = threshold["value"]
    return {"gte": value >= target, "gt": value > target,
            "lte": value <= target, "lt": value < target}[threshold["op"]]


def _latest(items: list[dict]) -> list[dict]:
    """Retain all tied latest records per source so ties cannot hide conflicts."""
    latest: dict[str, datetime] = {}
    for item in items:
        when = _date(item["as_of"], "observation as_of")
        previous = latest.get(item["source_id"])
        if previous is None or when > previous:
            latest[item["source_id"]] = when
    return [item for item in items if _date(item["as_of"], "observation as_of") == latest[item["source_id"]]]


def _citations(items: list[dict]) -> list[dict]:
    return [{key: item[key] for key in ("id", "source_id", "source_quality", "as_of",
                                        "citation", "title", "value", "unit", "period")}
            for item in sorted(items, key=lambda item: (item["source_id"], item["id"]))]


def evaluate_watch(watch: dict, observations: list[dict], now: datetime,
                   source_errors: list[str] | None = None) -> dict:
    """Rebuild research conclusions from present evidence, without side effects.

    Conflicting values are never averaged. Corroboration does not increase an
    invented confidence score. The scenario weights and information value are
    user assumptions, not forecasts learned by this engine.
    """
    watch = validate_watch(watch)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    now = now.astimezone(timezone.utc)
    if not isinstance(observations, list) or len(observations) > 10000:
        raise ValueError("observations must be a list of at most 10000 items")
    if source_errors is None:
        source_errors = []
    if not isinstance(source_errors, list) or len(source_errors) > 100:
        raise ValueError("source_errors must be a list of at most 100 items")
    errors = [_text(error, "source error", 2000) for error in source_errors]
    valid = []
    for index, raw in enumerate(observations):
        try:
            observation = _observation(raw)
        except ValueError as exc:
            errors.append(f"Observation {index + 1} rejected: {exc}")
            continue
        if observation["ticker"] == watch["ticker"]:
            valid.append(observation)
    reasons, gaps, review_items = [], [], []
    blocking, conditional = False, bool(errors)
    deadlines = []
    if errors:
        gaps.extend(f"Source/integrity error: {error}" for error in errors)
    future = [item for item in valid if _date(item["as_of"], "as_of") > now]
    if future:
        conditional = True
        gaps.append(f"{len(future)} future-dated observation(s) excluded.")
    present = [item for item in valid if _date(item["as_of"], "as_of") <= now]
    evaluated = []
    if not any(pillar["core"] for pillar in watch["pillars"]):
        blocking = True
        gaps.append("At least one core thesis pillar with approved confirmation criteria is required.")
    for pillar in watch["pillars"]:
        matches = [item for item in present if item["kind"] == "metric"
                   and item["source_id"] in pillar["source_ids"]
                   and item["metric"] == pillar["metric"] and item["unit"] == pillar["unit"]
                   and item["period"] == pillar["period"]]
        current = [item for item in _latest(matches)
                   if (now - _date(item["as_of"], "as_of")).total_seconds()
                   < watch["max_observation_age_seconds"]]
        status, note = "unknown", "No fresh, cited metric matches the configured source, unit and period."
        if current:
            values = [item["value"] for item in current]
            if max(values) - min(values) > pillar["conflict_tolerance"]:
                status, note = "conflict", "Current configured sources disagree beyond the absolute conflict tolerance."
            elif pillar["threshold_origin"] != "approved":
                status, note = "review", "Thresholds are not approved and cannot authorize a decision."
                hits = [name for name in ("kill", "warning", "confirm")
                        if any(_hit(value, pillar[name]) for value in values)]
                review_items.append({"pillar_id": pillar["id"], "origin": pillar["threshold_origin"],
                                     "threshold_hits": hits, "reason": note})
            elif any(_hit(value, pillar["kill"]) for value in values):
                status, note = "broken", "An approved thesis kill criterion is met."
            elif any(_hit(value, pillar["warning"]) for value in values):
                status, note = "impaired" if pillar["core"] else "watch", "An approved warning criterion is met."
            elif pillar["confirm"] is not None and all(_hit(value, pillar["confirm"]) for value in values):
                status, note = "supported", "Fresh evidence satisfies the approved confirmation criterion."
            else:
                note = "Fresh evidence does not establish the approved confirmation criterion."
            if any(item["source_quality"] != "primary" for item in current):
                conditional = True
                gaps.append(f"{pillar['id']}: metric evidence includes secondary or user assertions; primary-source review remains required.")
            for item in current:
                deadlines.append(_expiry(_date(item["as_of"], "as_of"), watch["max_observation_age_seconds"]))
        if status in {"unknown", "conflict", "review"}:
            gaps.append(f"{pillar['id']}: {note}")
            blocking = blocking or pillar["core"]
            conditional = conditional or not pillar["core"]
        evaluated.append({
            "id": pillar["id"], "claim": pillar["claim"], "core": pillar["core"],
            "status": status, "reason": note,
            "source_ids": sorted({item["source_id"] for item in current}),
            "evidence_ids": sorted({item["id"] for item in current}),
            "evidence": _citations(current), "threshold_origin": pillar["threshold_origin"],
        })
    statuses = {item["status"] for item in evaluated}
    if "broken" in statuses:
        company = "broken"
    elif "impaired" in statuses:
        company = "impaired"
    elif (any(item["core"] and item["status"] != "supported" for item in evaluated)
          or not any(item["core"] for item in evaluated)):
        company = "unproven"
    elif "watch" in statuses or any(item["status"] != "supported" for item in evaluated):
        company = "watch"
    else:
        company = "supported"
    reasons.append(f"Business thesis is {company}; stock valuation and portfolio action are assessed separately.")

    configured = watch["valuation"]
    valuation: dict = {"status": "missing", "currency": configured["currency"] if configured else None,
                       "quote": None, "current_price": None, "quote_as_of": None,
                       "expected_price": None, "expected_return": None,
                       "downside_fraction": None, "scenarios": configured["scenarios"] if configured else [],
                       "scenario_views": [], "horizon_days": watch["horizon_days"],
                       "assumptions": ["Scenario probabilities and prices are user estimates, not demonstrated predictive skill.",
                                       "Returns are unannualized price changes; taxes, fees, distributions and execution costs are not modeled.",
                                       f"Scenario prices use the explicitly configured {watch['horizon_days']}-day horizon."],
                       "quote_evidence": []}
    if configured is None:
        blocking = True
        gaps.append("A dated valuation with bull, base and bear scenarios is missing.")
    else:
        valuation.update(as_of=configured["as_of"], hurdle_return=configured["hurdle_return"],
                         max_loss_fraction=configured["max_loss_fraction"])
        valuation_date = _date(configured["as_of"], "valuation as_of")
        valuation_current = 0 <= (now - valuation_date).total_seconds() < watch["valuation_max_age_seconds"]
        if not valuation_current:
            blocking = True
            gaps.append("Valuation assumptions are stale or future-dated and require review.")
        else:
            deadlines.append(_expiry(valuation_date, watch["valuation_max_age_seconds"]))
        prices = [item for item in _latest([item for item in present if item["kind"] == "price"
                                          and item["unit"].upper() == configured["currency"]])
                  if (now - _date(item["as_of"], "as_of")).total_seconds() < watch["quote_max_age_seconds"]]
        valuation["quote_evidence"] = _citations(prices)
        if not prices:
            blocking = True
            gaps.append(f"A fresh cited {configured['currency']} quote is missing; other currencies are not converted.")
        elif max(item["value"] for item in prices) != min(item["value"] for item in prices):
            blocking = True
            valuation["status"] = "conflict"
            gaps.append("Fresh source quotes conflict; no quote was selected or averaged.")
        else:
            quote = prices[0]["value"]
            try:
                expected = math.fsum(item["probability"] * item["price"] for item in configured["scenarios"])
                expected_return = expected / quote - 1
                downside = max(0.0, 1 - min(item["price"] for item in configured["scenarios"]) / quote)
                scenario_views = [{**item, "return_fraction": item["price"] / quote - 1}
                                  for item in configured["scenarios"]]
                arithmetic_valid = all(math.isfinite(value) for value in
                                       (expected, expected_return, downside,
                                        *(item["return_fraction"] for item in scenario_views)))
            except (OverflowError, ValueError):
                arithmetic_valid = False
            if not arithmetic_valid:
                blocking = True
                gaps.append("Scenario arithmetic overflowed; revise the valuation inputs.")
                valuation["status"] = "invalid"
            else:
                valuation.update(status="current" if valuation_current else "stale", quote=quote, current_price=quote,
                                 quote_as_of=max(prices, key=lambda item: _date(item["as_of"], "as_of"))["as_of"],
                                 expected_price=expected, expected_return=expected_return,
                                 downside_fraction=downside,
                                 scenario_views=scenario_views)
            for item in prices:
                deadlines.append(_expiry(_date(item["as_of"], "as_of"), watch["quote_max_age_seconds"]))
            if any(item["source_quality"] == "user" for item in prices):
                conditional = True
                gaps.append("The quote includes a user assertion; market-source verification remains required.")

    catalyst = watch["catalyst"]
    waiting: dict = {"available": False, "preferred": False,
                    "reason": "No approved future information event has established a comparative value of waiting."}
    if catalyst:
        due = _date(catalyst["due_at"], "catalyst due_at")
        net_information = catalyst["information_value_per_share"] - catalyst["cost_per_share"] - catalyst["delay_cost_per_share"]
        waiting.update(name=catalyst["name"], due_at=catalyst["due_at"],
                       threshold_origin=catalyst["threshold_origin"],
                       net_information_value_per_share=net_information if math.isfinite(net_information) else None,
                       assumption="Information value and delay costs are explicit user estimates of improved decisions, not calibrated predictions.")
        if due > now:
            deadlines.append(due)
        if not math.isfinite(net_information):
            conditional = True
            gaps.append("Catalyst information-value arithmetic overflowed; revise its assumptions.")
        elif catalyst["threshold_origin"] != "approved":
            review_items.append({"catalyst": catalyst["name"], "origin": catalyst["threshold_origin"],
                                 "reason": "Waiting estimates need explicit approval before influencing action."})
        elif due <= now:
            conditional = True
            gaps.append("The approved catalyst is due or expired; review its actual outcome and retire or replace the catalyst.")
            waiting["reason"] = "The catalyst is due or expired; its expected information is not assumed to have arrived."
        elif valuation["status"] == "current":
            immediate_profit = max(0.0, valuation["expected_price"] - valuation["quote"])
            waiting.update(available=net_information > 0,
                           preferred=net_information > 0,
                           immediate_expected_profit_per_share=immediate_profit,
                           wait_value_per_share=immediate_profit + net_information,
                           reason="Estimated incremental information value exceeds observation and delay costs.")

    readiness = "not_decision_grade" if blocking else "conditional" if conditional else "ready"
    weight, maximum = watch["position"]["current_weight"], watch["position"]["max_weight"]
    favorable = (valuation["status"] == "current" and valuation["expected_return"] >= configured["hurdle_return"]
                  and valuation["downside_fraction"] <= configured["max_loss_fraction"])
    if company in {"broken", "impaired"}:
        readiness = "re_underwrite"
        action = ("review_exit" if company == "broken" else "review_trim") if weight > 0 else "re_underwrite"
        reasons.append("A thesis break or impairment requires fresh underwriting before adding exposure.")
    elif weight > maximum:
        action = "review_trim"
        reasons.append("Current position weight exceeds its explicit maximum.")
    elif readiness != "ready":
        action = "re_underwrite"
        reasons.append("Unresolved evidence or assumption gaps prevent a decision-grade security conclusion.")
    elif waiting["preferred"]:
        action = "wait_for_proof"
        reasons.append("Approved incremental information value exceeds observation and delay costs before the future catalyst.")
    elif favorable:
        action = "consider_buy" if weight < maximum else "hold"
        reasons.append("User scenario estimates meet the return hurdle and downside limit within the position cap.")
    else:
        action = "review_trim" if weight > 0 else "re_underwrite"
        reasons.append("The current price fails the configured return hurdle or downside limit.")
    if action != "wait_for_proof":
        waiting["preferred"] = False
    alternatives = [
        {"action": "buy", "preferred": action == "consider_buy", "available": readiness == "ready" and favorable and weight < maximum,
         "reason": "Requires supported evidence, attractive stated valuation, and capacity below the position limit."},
        {"action": "hold", "preferred": action == "hold", "available": weight > 0,
         "reason": "Existing exposure requires continuing underwriting; a hold is not automatic approval of additions."},
        {"action": "trim", "preferred": action == "review_trim", "available": weight > 0,
         "reason": "Review concentration, thesis impairment, or insufficient return at the current price."},
        {"action": "exit", "preferred": action == "review_exit", "available": weight > 0,
         "reason": "Review an approved thesis break and actual trading costs before any disposal."},
        {"action": "wait", **waiting},
    ]
    opposing = []
    if configured:
        bear = next(item for item in configured["scenarios"] if item["name"] == "bear")
        opposing.append(f"The supplied bear scenario values a share at {bear['price']:g} {configured['currency']} with user probability {bear['probability']:g}.")
    kills = [{"pillar_id": pillar["id"], "claim": pillar["claim"],
              "metric": pillar["metric"], "unit": pillar["unit"], "period": pillar["period"],
              "threshold": pillar["kill"], "threshold_origin": pillar["threshold_origin"]}
             for pillar in watch["pillars"] if pillar["kill"] is not None]
    opposing.extend(f"{item['claim']}: {item['reason']}" for item in evaluated if item["status"] != "supported")
    if not opposing:
        opposing.append("No explicit opposing scenario or supported disconfirmation has been supplied; seek contrary evidence.")
    return {
        "watch_id": watch["id"], "ticker": watch["ticker"], "as_of": _iso(now),
        "company_thesis_status": company, "security_readiness": readiness, "position_action": action,
        "reasons": reasons, "gaps": gaps, "pillars": evaluated, "valuation": valuation,
        "alternatives": alternatives, "review_items": review_items,
        "strongest_opposing_view": " ".join(opposing), "kill_criteria": kills,
        "next_review_at": _iso(min(deadline for deadline in deadlines if deadline > now))
                          if any(deadline > now for deadline in deadlines) else None,
        "research_only": True,
        "reversibility": "Research history can be restored. Market executions, price changes, taxes and transaction costs cannot be undone by restoring state.",
    }
