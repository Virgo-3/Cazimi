"""Pure, falsifiable backward-planning decisions.

A proposed future has no authority over its own prerequisites. Every evaluation
rebuilds admissibility from current evidence; a prior approval is only history.
All values are in the same user-chosen utility unit. Budget limits apply to
action and observation expenditure; delay cost is an opportunity cost.
"""

from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any


MAX_REQUIREMENTS = 256
MAX_EVIDENCE = 128
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_DEFAULT_ECONOMICS = {
    "probability": 0.5,
    "gain": 0.0,
    "loss": 0.0,
    "action_cost": 0.0,
    "observation_cost": 0.0,
    "delay_cost": 0.0,
    "signal_accuracy": 0.5,
    "observation_available": False,
}


def _text(value: Any, label: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    if len(value) > maximum:
        raise ValueError(f"{label} must contain at most {maximum} characters")
    value = value.strip()
    if not empty and not value:
        raise ValueError(f"{label} cannot be empty")
    return value


def _number(value: Any, label: str, minimum: float = 0.0, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    if result < minimum or (maximum is not None and result > maximum):
        bound = f" between {minimum:g} and {maximum:g}" if maximum is not None else f" at least {minimum:g}"
        raise ValueError(f"{label} must be{bound}")
    return result


def new_plan(goal: str) -> dict:
    """Create an explicitly provisional proposal with editable prerequisites."""
    return validate_plan({
        "goal": goal,
        "requirements": [
            {"id": "success", "text": "Define the success criterion and establish that the proposed outcome meets it."},
            {"id": "risks", "text": "Identify material risks and establish that their consequences are acceptable."},
            {"id": "rollback", "text": "Establish an executable rollback or recovery path before commitment."},
        ],
        "rollback": "",
        "status": "provisional",
    })


def validate_plan(plan: dict) -> dict:
    """Validate and normalize a plan without mutating it or trusting its status.

    Unknown metadata is discarded. Economics uses a closed schema to catch
    misspelled decision inputs. No requirement can opt out of adjudication.
    """
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    goal = _text(plan.get("goal"), "goal", 8000)
    rollback = _text(plan.get("rollback", ""), "rollback", 8000, empty=True)
    status = _text(plan.get("status", "provisional"), "status", 64)
    raw_requirements = plan.get("requirements", [])
    if not isinstance(raw_requirements, list) or len(raw_requirements) > MAX_REQUIREMENTS:
        raise ValueError(f"requirements must be a list of at most {MAX_REQUIREMENTS} items")
    requirements: list[dict] = []
    by_id: dict[str, dict] = {}
    for index, raw in enumerate(raw_requirements):
        label = f"requirement {index + 1}"
        if not isinstance(raw, dict):
            raise ValueError(f"{label} must be an object")
        identifier = _text(raw.get("id"), f"{label} id", 64)
        if not _IDENTIFIER.fullmatch(identifier):
            raise ValueError(f"{label} id must use letters, digits, underscores, dots or hyphens")
        if identifier in by_id:
            raise ValueError(f"duplicate requirement id: {identifier}")
        if "optional" in raw and raw["optional"] is not False:
            raise ValueError("all requirements must be required; optional prerequisites are unsupported")
        text = _text(raw.get("text"), f"{label} text", 4000)
        dependencies = raw.get("depends_on", [])
        if not isinstance(dependencies, list) or len(dependencies) > MAX_REQUIREMENTS:
            raise ValueError(f"{label} depends_on must be a bounded list")
        dependencies = [_text(item, f"{label} dependency", 64) for item in dependencies]
        if len(set(dependencies)) != len(dependencies):
            raise ValueError(f"{label} has duplicate dependencies")
        raw_evidence = raw.get("evidence", [])
        if not isinstance(raw_evidence, list) or len(raw_evidence) > MAX_EVIDENCE:
            raise ValueError(f"{label} evidence must be a list of at most {MAX_EVIDENCE} items")
        evidence = []
        for item in raw_evidence:
            if not isinstance(item, dict):
                raise ValueError(f"{label} evidence must contain objects")
            verdict = item.get("verdict")
            if verdict not in ("support", "refute"):
                raise ValueError("evidence verdict must be support or refute")
            evidence.append({
                "verdict": verdict,
                "note": _text(item.get("note"), "evidence note", 8000),
                "source": _text(item.get("source", ""), "evidence source", 2000, empty=True),
            })
        requirement = {"id": identifier, "text": text, "depends_on": dependencies, "evidence": evidence}
        by_id[identifier] = requirement
        requirements.append(requirement)

    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            raise ValueError(f"dependency cycle involving {identifier}")
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in by_id[identifier]["depends_on"]:
            if dependency not in by_id:
                raise ValueError(f"missing dependency {dependency} required by {identifier}")
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in by_id:
        visit(identifier)

    raw_economics = plan.get("economics", {})
    if not isinstance(raw_economics, dict):
        raise ValueError("economics must be an object")
    unknown_economics = set(raw_economics) - set(_DEFAULT_ECONOMICS)
    if unknown_economics:
        raise ValueError("unknown economics fields: " + ", ".join(sorted(map(str, unknown_economics))))
    economics = dict(_DEFAULT_ECONOMICS)
    economics.update(raw_economics)
    for name in ("gain", "loss", "action_cost", "observation_cost", "delay_cost"):
        economics[name] = _number(economics[name], name)
    economics["probability"] = _number(economics["probability"], "probability", 0.0, 1.0)
    economics["signal_accuracy"] = _number(economics["signal_accuracy"], "signal_accuracy", 0.5, 1.0)
    if not isinstance(economics["observation_available"], bool):
        raise ValueError("observation_available must be a boolean")
    # This bound ensures every weighted payoff and cost sum stays representable.
    if not math.isfinite(sum(economics[name] for name in ("gain", "loss", "action_cost", "observation_cost", "delay_cost"))):
        raise ValueError("economics exceed the supported numeric range")
    return {"goal": goal, "requirements": requirements, "economics": economics, "rollback": rollback, "status": status}


def add_evidence(plan: dict, requirement_id: str, verdict: str, note: str, source: str) -> dict:
    """Return a validated new plan with an appended, attributed observation."""
    result = validate_plan(plan)
    for requirement in result["requirements"]:
        if requirement["id"] == requirement_id:
            requirement["evidence"].append({"verdict": verdict, "note": note, "source": source})
            return validate_plan(result)
    raise ValueError(f"unknown requirement: {requirement_id}")


def evaluate(plan: dict, remaining_budget: float = math.inf) -> dict:
    """Adjudicate current evidence and compare acting, abandonment and waiting.

    Waiting uses a binary symmetric signal: accuracy is P(positive | success)
    and P(negative | failure). Conditional decisions may abandon the action.
    Evidence is an explicit human or tool assertion, not independent truth.
    """
    plan = validate_plan(plan)
    if isinstance(remaining_budget, bool) or not isinstance(remaining_budget, (int, float)):
        raise ValueError("remaining_budget must be a nonnegative number or positive infinity")
    try:
        remaining_budget = float(remaining_budget)
    except (ValueError, OverflowError) as exc:
        raise ValueError("remaining_budget must be a nonnegative number or positive infinity") from exc
    if math.isnan(remaining_budget) or remaining_budget < 0:
        raise ValueError("remaining_budget must be a nonnegative number or positive infinity")

    economics = plan["economics"]
    p = economics["probability"]
    gain, loss, action_cost = economics["gain"], economics["loss"], economics["action_cost"]

    def action_value(probability: float) -> float:
        return probability * gain - (1.0 - probability) * loss - action_cost

    act_value = action_value(p)
    accuracy = economics["signal_accuracy"]
    expected_after_signal = 0.0
    signals = []
    for name, success_likelihood, failure_likelihood in (
        ("positive", accuracy, 1.0 - accuracy),
        ("negative", 1.0 - accuracy, accuracy),
    ):
        success_mass = p * success_likelihood
        probability = success_mass + (1.0 - p) * failure_likelihood
        if probability <= 0.0:
            continue
        posterior = min(1.0, max(0.0, success_mass / probability))
        conditional_value = action_value(posterior)
        expected_after_signal += probability * max(0.0, conditional_value)
        signals.append({"signal": name, "probability": probability, "posterior": posterior, "act_value": conditional_value})
    immediate_value = max(0.0, act_value)
    wait_value = expected_after_signal - economics["observation_cost"] - economics["delay_cost"]
    net_information_value = wait_value - immediate_value
    action_affordable = action_cost <= remaining_budget
    observation_affordable = economics["observation_cost"] <= remaining_budget
    # Waiting must preserve funding for the possible subsequent action too.
    followup_action_affordable = action_cost + economics["observation_cost"] <= remaining_budget
    wait_feasible = economics["observation_available"] and observation_affordable and followup_action_affordable

    by_id = {item["id"]: item for item in plan["requirements"]}
    states: dict[str, str] = {}

    def state(identifier: str) -> str:
        if identifier in states:
            return states[identifier]
        requirement = by_id[identifier]
        verdicts = {item["verdict"] for item in requirement["evidence"]}
        if "refute" in verdicts:
            result = "refuted"
        elif "support" not in verdicts:
            result = "unknown"
        elif any(state(dependency) != "satisfied" for dependency in requirement["depends_on"]):
            result = "blocked"
        else:
            result = "satisfied"
        states[identifier] = result
        return result

    requirements = []
    for requirement in plan["requirements"]:
        item = deepcopy(requirement)
        item["state"] = state(item["id"])
        requirements.append(item)
    refuted = [item["id"] for item in requirements if item["state"] == "refuted"]
    unresolved = [item["id"] for item in requirements if item["state"] != "satisfied"]
    all_satisfied = bool(requirements) and not unresolved
    metrics = {
        "act_value": act_value,
        "wait_value": wait_value,
        "net_information_value": net_information_value,
        "information_value": expected_after_signal - immediate_value,
        "expected_value_after_signal": expected_after_signal,
        "observation_cost": economics["observation_cost"],
        "delay_cost": economics["delay_cost"],
        "action_cost": action_cost,
        "probability": p,
        "signal_accuracy": accuracy,
        "observation_available": economics["observation_available"],
        "observation_affordable": observation_affordable,
        "followup_action_affordable": followup_action_affordable,
        "action_affordable": action_affordable,
        "requirements_satisfied": all_satisfied,
        "rollback_defined": bool(plan["rollback"]),
        "signals": signals,
    }
    reasons: list[str] = []
    if refuted:
        decision = "reject"
        reasons.append("Required prerequisite refuted: " + ", ".join(refuted) + ". A previous approval cannot override contradictory evidence.")
    else:
        if not requirements:
            reasons.append("No prerequisites are defined; establish falsifiable requirements before acting.")
        elif unresolved:
            reasons.append("Prerequisites need evidence or satisfied dependencies: " + ", ".join(unresolved) + ".")
        if not plan["rollback"]:
            reasons.append("Define an executable rollback or recovery path before acting.")
        if not action_affordable:
            reasons.append("The action cost exceeds the remaining budget.")
        if wait_feasible and net_information_value > 1e-12 * max(1.0, abs(immediate_value), abs(wait_value)):
            decision = "wait"
            reasons.append("An available, affordable observation has higher expected value after observation and delay costs than immediate commitment or abandonment.")
        elif not all_satisfied or not plan["rollback"] or not action_affordable:
            decision = "investigate"
            if economics["observation_available"] and not wait_feasible:
                reasons.append("The observation and possible follow-up action exceed the remaining budget.")
        elif act_value <= 0.0:
            decision = "reject"
            reasons.append("The action has no positive expected value; abandonment is worth at least as much.")
        else:
            decision = "act"
            reasons.append("Every required prerequisite is supported, a recovery path is recorded, the action is affordable and its expected value is positive.")
    return {"decision": decision, "reasons": reasons, "metrics": metrics, "requirements": requirements}
