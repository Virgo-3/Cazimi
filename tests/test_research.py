import copy
from datetime import datetime, timedelta, timezone
import json
import unittest

from cazimi.research import evaluate_watch, validate_watch


NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def watch():
    return {
        "id": "example", "ticker": "exm", "thesis": "Recurring revenue supports the proposed valuation.",
        "pillars": [{"id": "growth", "claim": "Revenue growth remains above 10 percent.",
                     "metric": "revenue_growth", "unit": "fraction", "period": "2026-Q3",
                     "source_ids": ["filing"], "confirm": {"op": "gte", "value": .1},
                     "warning": {"op": "lt", "value": .05}, "kill": {"op": "lt", "value": 0},
                     "threshold_origin": "approved"}],
        "valuation": {"currency": "USD", "as_of": "2026-10-23T00:00:00Z",
                      "scenarios": [{"name": "bull", "probability": .25, "price": 160},
                                    {"name": "base", "probability": .5, "price": 120},
                                    {"name": "bear", "probability": .25, "price": 80}]},
    }


def metric(value=.12, **changes):
    result = {"id": "growth-1", "ticker": "EXM", "kind": "metric", "as_of": "2026-10-22T00:00:00Z",
              "source_id": "filing", "source_quality": "primary", "citation": "https://example.test/filing",
              "title": "Quarterly filing", "metric": "revenue_growth", "value": value,
              "unit": "fraction", "period": "2026-Q3"}
    result.update(changes)
    return result


def quote(value=100, **changes):
    result = {"id": "price-1", "ticker": "EXM", "kind": "price", "as_of": "2026-10-23T11:59:00Z",
              "source_id": "prices", "source_quality": "secondary", "citation": "https://example.test/quote",
              "title": "Market quote", "metric": "price", "value": value, "unit": "USD", "period": ""}
    result.update(changes)
    return result


def evaluate(config=None, records=None, **kwargs):
    return evaluate_watch(config or watch(), records if records is not None else [metric(), quote()], NOW, **kwargs)


class WatchValidationTests(unittest.TestCase):
    def test_normalized_defaults_are_closed_schema_and_independent(self):
        raw = watch()
        raw["ignored"] = "untrusted"
        result = validate_watch(raw)
        self.assertEqual(result["ticker"], "EXM")
        self.assertEqual(result["horizon_days"], 365)
        self.assertEqual(result["position"], {"current_weight": 0, "max_weight": .1})
        self.assertEqual(result["quote_max_age_seconds"], 86400)
        self.assertNotIn("ignored", result)
        result["pillars"][0]["confirm"]["value"] = 100
        self.assertEqual(raw["pillars"][0]["confirm"]["value"], .1)
        self.assertEqual(validate_watch(result), result)

    def test_thresholds_default_to_draft(self):
        raw = watch()
        raw["pillars"][0].pop("threshold_origin")
        self.assertEqual(validate_watch(raw)["pillars"][0]["threshold_origin"], "draft")

    def test_rejects_unbounded_text_invalid_ids_and_controls(self):
        for key, value in (("id", "bad id"), ("ticker", "bad!"), ("thesis", "x" * 8001),
                           ("thesis", "bad\x1b[31m"), ("thesis", "")):
            raw = watch()
            raw[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_watch(raw)

    def test_rejects_bool_nonfinite_and_overflow_numbers(self):
        for value in (True, False, float("nan"), float("inf"), float("-inf"), 10 ** 1000, "1"):
            raw = watch()
            raw["valuation"]["scenarios"][0]["price"] = value
            with self.subTest(value=str(value)[:30]), self.assertRaises(ValueError):
                validate_watch(raw)

    def test_probabilities_require_all_three_scenarios_and_sum_one(self):
        changes = [lambda value: value.pop(),
                   lambda value: value[0].update(name="base"),
                   lambda value: value[0].update(probability=.4),
                   lambda value: value[0].update(probability=-.1),
                   lambda value: value[0].update(price=0)]
        for change in changes:
            raw = watch()
            change(raw["valuation"]["scenarios"])
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_watch(raw)

    def test_requires_aware_dates(self):
        for value in (None, "2026-10-23", "2026-10-23T00:00:00", "invalid"):
            raw = watch()
            raw["valuation"]["as_of"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_watch(raw)
        raw = watch()
        raw["valuation"]["as_of"] = "2026-10-22T19:00:00-05:00"
        self.assertEqual(validate_watch(raw)["valuation"]["as_of"], "2026-10-23T00:00:00Z")

    def test_bounds_and_duplicates(self):
        for key, value in (("horizon_days", True), ("horizon_days", 1.5), ("horizon_days", 0),
                           ("horizon_days", 3651), ("quote_max_age_seconds", 0)):
            raw = watch()
            raw[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_watch(raw)
        raw = watch()
        raw["pillars"].append(copy.deepcopy(raw["pillars"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_watch(raw)
        raw = watch()
        raw["pillars"][0]["source_ids"] *= 2
        with self.assertRaisesRegex(ValueError, "unique"):
            validate_watch(raw)

    def test_malformed_nested_enums_raise_value_error(self):
        for target, key in (("pillar", "threshold_origin"), ("scenario", "name"), ("threshold", "op")):
            raw = watch()
            item = {"pillar": raw["pillars"][0], "scenario": raw["valuation"]["scenarios"][0],
                    "threshold": raw["pillars"][0]["confirm"]}[target]
            item[key] = []
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_watch(raw)


class EvidenceEvaluationTests(unittest.TestCase):
    def test_ready_result_has_separate_business_security_and_position_assessments(self):
        result = evaluate()
        self.assertEqual(result["company_thesis_status"], "supported")
        self.assertEqual(result["security_readiness"], "ready")
        self.assertEqual(result["position_action"], "consider_buy")
        self.assertEqual(result["gaps"], [])
        self.assertTrue(result["research_only"])
        self.assertEqual(result["pillars"][0]["evidence_ids"], ["growth-1"])
        self.assertEqual(result["next_review_at"], "2026-10-24T11:59:00Z")
        json.dumps(result, allow_nan=False)

    def test_draft_or_inherited_hits_never_break_or_approve_thesis(self):
        for origin in ("draft", "inherited"):
            for value in (-1, .2):
                config = watch()
                config["pillars"][0]["threshold_origin"] = origin
                result = evaluate(config, [metric(value), quote()])
                self.assertEqual(result["company_thesis_status"], "unproven")
                self.assertEqual(result["security_readiness"], "not_decision_grade")
                self.assertEqual(result["pillars"][0]["status"], "review")
                self.assertTrue(result["review_items"][0]["threshold_hits"])

    def test_approved_kill_and_warning_differ(self):
        config = watch()
        config["position"] = {"current_weight": .05}
        killed = evaluate(config, [metric(-.01), quote()])
        warning = evaluate(config, [metric(.01), quote()])
        self.assertEqual(killed["company_thesis_status"], "broken")
        self.assertEqual(killed["position_action"], "review_exit")
        self.assertEqual(warning["company_thesis_status"], "impaired")
        self.assertEqual(warning["position_action"], "review_trim")
        self.assertEqual(killed["security_readiness"], "re_underwrite")

    def test_noncore_warning_does_not_break_business(self):
        config = watch()
        optional = copy.deepcopy(config["pillars"][0])
        optional.update(id="noncore", core=False, metric="retention")
        config["pillars"].append(optional)
        result = evaluate(config, [metric(), metric(.01, id="retention", metric="retention"), quote()])
        self.assertEqual(result["company_thesis_status"], "watch")
        self.assertEqual(result["security_readiness"], "ready")

    def test_noncore_unknown_is_conditional_and_no_core_is_never_ready(self):
        config = watch()
        optional = copy.deepcopy(config["pillars"][0])
        optional.update(id="noncore", core=False, metric="retention")
        config["pillars"].append(optional)
        result = evaluate(config)
        self.assertEqual(result["security_readiness"], "conditional")
        config["pillars"] = [optional]
        result = evaluate(config)
        self.assertEqual(result["security_readiness"], "not_decision_grade")
        self.assertEqual(result["company_thesis_status"], "unproven")

    def test_stale_or_future_metrics_cannot_support(self):
        for when in ("2020-01-01T00:00:00Z", "2026-10-24T00:00:00Z"):
            result = evaluate(records=[metric(as_of=when), quote()])
            self.assertEqual(result["company_thesis_status"], "unproven")
            self.assertEqual(result["security_readiness"], "not_decision_grade")

    def test_metric_is_stale_exactly_at_age_limit(self):
        config = watch()
        config["max_observation_age_seconds"] = 60
        result = evaluate(config, [metric(as_of="2026-10-23T11:59:00Z"), quote()])
        self.assertEqual(result["security_readiness"], "not_decision_grade")

    def test_wrong_ticker_source_period_unit_or_metric_cannot_support(self):
        for changes in ({"ticker": "OTHER"}, {"source_id": "other"}, {"period": "2025-Q3"},
                        {"unit": "percent"}, {"metric": "other"}):
            with self.subTest(changes=changes):
                result = evaluate(records=[metric(**changes), quote()])
                self.assertEqual(result["pillars"][0]["status"], "unknown")

    def test_missing_sources_and_confirmation_stay_unknown(self):
        for changes in ({"source_ids": []}, {"confirm": None}):
            config = watch()
            config["pillars"][0].update(changes)
            self.assertEqual(evaluate(config)["security_readiness"], "not_decision_grade")

    def test_current_sources_conflict_and_explicit_tolerance(self):
        config = watch()
        config["pillars"][0]["source_ids"].append("other")
        records = [metric(), metric(.14, id="growth-2", source_id="other"), quote()]
        self.assertEqual(evaluate(config, records)["pillars"][0]["status"], "conflict")
        config["pillars"][0]["conflict_tolerance"] = .03
        result = evaluate(config, records)
        self.assertEqual(result["pillars"][0]["status"], "supported")
        self.assertEqual(result["pillars"][0]["source_ids"], ["filing", "other"])
        self.assertNotIn("confidence", result["pillars"][0])

    def test_same_source_equal_time_conflicting_revisions_cannot_disappear(self):
        result = evaluate(records=[metric(), metric(-.1, id="revision"), quote()])
        self.assertEqual(result["pillars"][0]["status"], "conflict")
        self.assertEqual(result["pillars"][0]["evidence_ids"], ["growth-1", "revision"])

    def test_latest_per_source_and_fractional_seconds_are_chronological(self):
        records = [metric(-.1, as_of="2026-10-23T11:00:00Z"),
                   metric(.12, id="new", as_of="2026-10-23T11:00:00.500000Z"), quote()]
        result = evaluate(records=records)
        self.assertEqual(result["pillars"][0]["status"], "supported")
        self.assertEqual(result["pillars"][0]["evidence_ids"], ["new"])

    def test_documents_never_become_numeric_authority(self):
        document = metric(kind="document", value=None, title="Growth is 100 percent", metric="", unit="", period="")
        result = evaluate(records=[document, quote()])
        self.assertEqual(result["security_readiness"], "not_decision_grade")
        self.assertEqual(result["pillars"][0]["evidence"], [])

    def test_secondary_and_user_metrics_require_primary_review(self):
        for quality in ("secondary", "user"):
            result = evaluate(records=[metric(source_quality=quality), quote()])
            self.assertEqual(result["company_thesis_status"], "supported")
            self.assertEqual(result["security_readiness"], "conditional")
            self.assertEqual(result["pillars"][0]["evidence"][0]["source_quality"], quality)

    def test_source_errors_and_invalid_observations_cannot_silently_ready(self):
        result = evaluate(source_errors=["Filing fetch failed"])
        self.assertEqual(result["security_readiness"], "conditional")
        result = evaluate(records=[metric(), quote(), {"kind": []}])
        self.assertEqual(result["security_readiness"], "conditional")
        self.assertIn("Observation 3 rejected", " ".join(result["gaps"]))

    def test_empty_citation_is_not_evidence(self):
        result = evaluate(records=[metric(citation=""), quote()])
        self.assertEqual(result["security_readiness"], "not_decision_grade")

    def test_future_extra_record_requires_review_even_when_old_record_supports(self):
        result = evaluate(records=[metric(), quote(), metric(-.1, id="future", as_of="2026-10-24T00:00:00Z")])
        self.assertEqual(result["company_thesis_status"], "supported")
        self.assertEqual(result["security_readiness"], "conditional")


class ValuationAndAlternativesTests(unittest.TestCase):
    def test_explicit_probability_weighted_arithmetic_and_forecast_contract(self):
        result = evaluate()
        valuation = result["valuation"]
        self.assertEqual(valuation["current_price"], 100)
        self.assertEqual(valuation["expected_price"], 120)
        self.assertAlmostEqual(valuation["expected_return"], .2)
        self.assertAlmostEqual(valuation["downside_fraction"], .2)
        self.assertEqual(valuation["quote_as_of"], "2026-10-23T11:59:00Z")
        self.assertEqual(valuation["horizon_days"], 365)
        self.assertEqual(len(valuation["scenario_views"]), 3)
        self.assertEqual({item["action"] for item in result["alternatives"]}, {"buy", "hold", "trim", "exit", "wait"})

    def test_missing_valuation_or_quote_cannot_be_ready(self):
        config = watch()
        config["valuation"] = None
        self.assertEqual(evaluate(config)["security_readiness"], "not_decision_grade")
        self.assertEqual(evaluate(records=[metric()])["security_readiness"], "not_decision_grade")

    def test_stale_and_future_valuation_require_review(self):
        for when in ("2020-01-01T00:00:00Z", "2026-10-24T00:00:00Z"):
            config = watch()
            config["valuation"]["as_of"] = when
            result = evaluate(config)
            self.assertEqual(result["security_readiness"], "not_decision_grade")
            self.assertEqual(result["company_thesis_status"], "supported")

    def test_stale_future_wrong_currency_quote_does_not_supply_price(self):
        for changes in ({"as_of": "2020-01-01T00:00:00Z"}, {"as_of": "2026-10-24T00:00:00Z"}, {"unit": "EUR"}):
            with self.subTest(changes=changes):
                result = evaluate(records=[metric(), quote(**changes)])
                self.assertIsNone(result["valuation"]["current_price"])
                self.assertEqual(result["security_readiness"], "not_decision_grade")

    def test_quotes_conflict_across_sources_or_equal_timestamp_revisions(self):
        for other in (quote(101, id="revision"), quote(101, id="other", source_id="other")):
            result = evaluate(records=[metric(), quote(), other])
            self.assertEqual(result["valuation"]["status"], "conflict")
            self.assertIsNone(result["valuation"]["current_price"])
            self.assertEqual(result["security_readiness"], "not_decision_grade")

    def test_nonpositive_and_nonfinite_quotes_rejected(self):
        for value in (0, -1, float("nan"), float("inf"), True):
            result = evaluate(records=[metric(), quote(value)])
            self.assertIsNone(result["valuation"]["current_price"])
            self.assertEqual(result["security_readiness"], "not_decision_grade")

    def test_user_quote_is_labeled_conditional(self):
        result = evaluate(records=[metric(), quote(source_quality="user")])
        self.assertEqual(result["security_readiness"], "conditional")
        self.assertEqual(result["valuation"]["quote_evidence"][0]["source_quality"], "user")

    def test_finite_inputs_with_overflowing_returns_do_not_escape_as_infinity(self):
        result = evaluate(records=[metric(), quote(5e-324)])
        self.assertEqual(result["valuation"]["status"], "invalid")
        self.assertEqual(result["security_readiness"], "not_decision_grade")
        json.dumps(result, allow_nan=False)

    def test_zero_probability_extreme_scenario_still_requires_finite_report(self):
        config = watch()
        config["valuation"]["scenarios"] = [
            {"name": "bull", "probability": 0, "price": 1e308},
            {"name": "base", "probability": 1, "price": 1},
            {"name": "bear", "probability": 0, "price": .5},
        ]
        result = evaluate(config, [metric(), quote(1e-100)])
        self.assertEqual(result["valuation"]["status"], "invalid")
        json.dumps(result, allow_nan=False)

    def test_price_can_make_supported_business_unattractive(self):
        result = evaluate(records=[metric(), quote(200)])
        self.assertEqual(result["company_thesis_status"], "supported")
        self.assertEqual(result["security_readiness"], "ready")
        self.assertEqual(result["position_action"], "re_underwrite")
        config = watch()
        config["position"] = {"current_weight": .05}
        self.assertEqual(evaluate(config, [metric(), quote(200)])["position_action"], "review_trim")

    def test_position_caps_and_downside_limits(self):
        config = watch()
        config["position"] = {"current_weight": .1, "max_weight": .1}
        self.assertEqual(evaluate(config)["position_action"], "hold")
        config["position"]["current_weight"] = .15
        self.assertEqual(evaluate(config)["position_action"], "review_trim")
        config["position"]["current_weight"] = 0
        config["valuation"]["max_loss_fraction"] = .1
        self.assertEqual(evaluate(config)["position_action"], "re_underwrite")

    def test_hard_break_precedes_valuation_or_wait(self):
        config = watch()
        config["position"] = {"current_weight": .05}
        config["catalyst"] = catalyst(100)
        result = evaluate(config, [metric(-.2), quote()])
        self.assertEqual(result["position_action"], "review_exit")
        self.assertFalse(result["alternatives"][-1]["preferred"])
        self.assertIn("bear scenario", result["strongest_opposing_view"])
        self.assertEqual(result["kill_criteria"][0]["threshold_origin"], "approved")


def catalyst(information=30, **changes):
    result = {"name": "Earnings", "due_at": "2026-10-24T00:00:00Z", "information_value_per_share": information,
              "cost_per_share": 2, "delay_cost_per_share": 3, "threshold_origin": "approved"}
    result.update(changes)
    return result


class WaitingTests(unittest.TestCase):
    def test_incremental_information_is_not_charged_against_existing_upside(self):
        config = watch()
        config["catalyst"] = catalyst(10)
        result = evaluate(config)
        self.assertEqual(result["position_action"], "wait_for_proof")
        self.assertEqual(result["alternatives"][-1]["wait_value_per_share"], 25)

    def test_approved_future_net_information_value_can_favor_waiting(self):
        config = watch()
        config["catalyst"] = catalyst()
        result = evaluate(config)
        self.assertEqual(result["position_action"], "wait_for_proof")
        waiting = result["alternatives"][-1]
        self.assertEqual(waiting["net_information_value_per_share"], 25)
        self.assertEqual(waiting["immediate_expected_profit_per_share"], 20)
        self.assertEqual(waiting["wait_value_per_share"], 45)
        self.assertTrue(waiting["preferred"])
        self.assertEqual(result["next_review_at"], "2026-10-24T00:00:00Z")

    def test_nonpositive_incremental_information_value_does_not_favor_wait(self):
        for information in (0, 5):
            config = watch()
            config["catalyst"] = catalyst(information)
            result = evaluate(config)
            self.assertEqual(result["position_action"], "consider_buy")
            self.assertFalse(result["alternatives"][-1]["preferred"])

    def test_draft_and_inherited_waiting_estimates_are_review_items(self):
        for origin in ("draft", "inherited"):
            config = watch()
            config["catalyst"] = catalyst(100, threshold_origin=origin)
            result = evaluate(config)
            self.assertEqual(result["position_action"], "consider_buy")
            self.assertTrue(result["review_items"])
            self.assertFalse(result["alternatives"][-1]["available"])

    def test_due_or_expired_catalyst_cannot_be_assumed_observed(self):
        for due in ("2026-10-23T12:00:00Z", "2026-10-22T00:00:00Z"):
            config = watch()
            config["catalyst"] = catalyst(100, due_at=due)
            result = evaluate(config)
            self.assertEqual(result["security_readiness"], "conditional")
            self.assertEqual(result["position_action"], "re_underwrite")
            self.assertFalse(result["alternatives"][-1]["available"])

    def test_waiting_cannot_override_missing_evidence_or_source_failure(self):
        config = watch()
        config["catalyst"] = catalyst(100)
        result = evaluate(config, [quote()])
        self.assertEqual(result["position_action"], "re_underwrite")
        result = evaluate(config, source_errors=["filing fetch failed"])
        self.assertEqual(result["position_action"], "re_underwrite")

    def test_evaluation_never_mutates_config_or_observations(self):
        config, records = watch(), [metric(), quote()]
        original_config, original_records = copy.deepcopy(config), copy.deepcopy(records)
        evaluate(config, records)
        self.assertEqual(config, original_config)
        self.assertEqual(records, original_records)

    def test_now_requires_timezone(self):
        with self.assertRaises(ValueError):
            evaluate_watch(watch(), [], datetime(2026, 10, 23))

    def test_overflowing_information_value_requires_review(self):
        config = watch()
        config["catalyst"] = catalyst(0, cost_per_share=1e308, delay_cost_per_share=1e308)
        result = evaluate(config)
        self.assertEqual(result["security_readiness"], "conditional")
        self.assertIsNone(result["alternatives"][-1]["net_information_value_per_share"])
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
