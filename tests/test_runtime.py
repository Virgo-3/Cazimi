"""Offline integration contracts for durable, auditable research automation."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cazimi.feeds import fetch_source
from cazimi.runtime import ResearchRuntime, stamp


class ResearchRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.feed = self.directory / "evidence.json"
        self.root = self.directory / "state"
        self.now = datetime(2026, 10, 23, 15, tzinfo=timezone.utc)
        self.records = [self.metric(), self.quote()]
        self.write_feed()
        self.runtime = self.open_runtime()
        self.source = {"id": "filings", "kind": "json", "location": str(self.feed),
                       "category": "company_filings_ir", "quality": "primary",
                       "ticker": "DEMO", "request_cost": .5}
        self.watch = {
            "id": "demo", "ticker": "DEMO", "thesis": "Margins sustain the supplied valuation.",
            "horizon_days": 1,
            "pillars": [{"id": "margin", "claim": "Operating margin remains healthy.",
                         "metric": "operating_margin", "unit": "fraction", "period": "FY2026",
                         "source_ids": ["filings"], "confirm": {"op": "gte", "value": .2},
                         "warning": {"op": "lt", "value": .18}, "kill": {"op": "lt", "value": .1}}],
            "valuation": {"currency": "USD", "as_of": self.now.isoformat(),
                          "scenarios": [{"name": "bull", "probability": .25, "price": 160},
                                        {"name": "base", "probability": .5, "price": 130},
                                        {"name": "bear", "probability": .25, "price": 80}],
                          "hurdle_return": .1, "max_loss_fraction": .3},
            "position": {"current_weight": .02, "max_weight": .1}}
        self.runtime.add_source(self.source)
        self.runtime.add_watch(self.watch)
        self.runtime.approve("demo")

    def open_runtime(self):
        runtime = ResearchRuntime(self.root)
        self.addCleanup(runtime.close)
        return runtime

    def metric(self, value=.25, when=None, identifier="margin"):
        return {"id": identifier, "ticker": "DEMO", "kind": "metric",
                "as_of": (when or self.now).isoformat(), "metric": "operating_margin",
                "value": value, "unit": "fraction", "period": "FY2026",
                "citation": "https://issuer.example/filing", "title": "Issuer filing"}

    def quote(self, value=100, when=None, identifier="quote"):
        return {"id": identifier, "ticker": "DEMO", "kind": "price",
                "as_of": (when or self.now).isoformat(), "value": value, "unit": "USD",
                "citation": "https://issuer.example/quote", "title": "Observed quote"}

    def write_feed(self, records=None):
        self.feed.write_text(json.dumps({"observations": self.records if records is None else records}), encoding="utf-8")

    def assert_integrity(self):
        self.assertEqual(self.runtime.verify(), [])
        self.assertEqual(self.runtime.verify_research(), [])

    def test_real_file_cycle_persists_evidence_report_and_three_scenario_branches(self):
        result = self.runtime.run(now=self.now)
        self.assertEqual(result["new_evidence"], 2)
        self.assertEqual(result["changed_theses"], ["demo"])
        report = result["reports"]["demo"]
        self.assertEqual(report["company_thesis_status"], "supported")
        self.assertEqual(report["security_readiness"], "ready")
        self.assertEqual(report["position_action"], "consider_buy")
        self.assertTrue(report["research_only"])
        cases = self.runtime.db.execute("SELECT name FROM branches WHERE name LIKE 'cases/%'").fetchall()
        self.assertEqual(len(cases), 3)
        for case in cases:
            state, _ = self.runtime.branch_snapshot(case["name"])
            self.assertEqual(state["research"]["scenario_view"]["research_branch"], "main")
        self.assertEqual(self.runtime.usage()["spent"], .5)
        self.assert_integrity()

    def test_unchanged_poll_keeps_snapshot_alerts_and_model_calls_quiet(self):
        with patch.object(self.runtime, "_model_review") as reviewer:
            first = self.runtime.run(now=self.now, model_review=True)
            alerts = self.runtime.alerts(include_acknowledged=True)
            second = self.runtime.run(now=self.now + timedelta(minutes=1), model_review=True)
        self.assertEqual(reviewer.call_count, 1)
        self.assertEqual(second["new_evidence"], 0)
        self.assertEqual(second["changed_theses"], [])
        self.assertEqual(second["snapshot"], first["snapshot"])
        self.assertEqual(self.runtime.alerts(include_acknowledged=True), alerts)
        self.assertEqual(self.runtime.usage()["spent"], 1)
        self.assert_integrity()

    def test_schedule_survives_restart_and_does_not_fetch_before_next_due(self):
        self.runtime.schedule(60, now=self.now)
        self.runtime.run(now=self.now, force=False)
        self.runtime.close()
        self.runtime = self.open_runtime()
        with patch("cazimi.runtime.fetch_source", wraps=fetch_source) as fetch:
            idle = self.runtime.run(now=self.now + timedelta(seconds=59), force=False)
            self.assertEqual(idle["type"], "idle")
            fetch.assert_not_called()
            due = self.runtime.run(now=self.now + timedelta(seconds=60), force=False)
            self.assertEqual(due["type"], "research_cycle")
            self.assertEqual(fetch.call_count, 1)
        self.assertIsNone(self.runtime.job("main")["owner"])
        self.assert_integrity()

    def test_schedule_wakes_at_quote_expiry_before_a_long_poll_interval(self):
        self.watch["quote_max_age_seconds"] = 10
        self.runtime.add_watch(self.watch)
        self.runtime.approve("demo")
        self.runtime.schedule(3600, now=self.now)
        self.runtime.run(now=self.now, force=False)
        self.assertEqual(self.runtime.job("main")["next_run"], stamp(self.now + timedelta(seconds=10)))
        expired = self.runtime.run(now=self.now + timedelta(seconds=10), force=False)
        self.assertEqual(expired["reports"]["demo"]["security_readiness"], "not_decision_grade")

    def test_kill_criterion_cannot_be_erased_by_recovery_branching_or_restore(self):
        before_break = self.runtime.head
        self.write_feed([self.metric(.05), self.quote()])
        broken = self.runtime.run(now=self.now)
        self.assertEqual(broken["reports"]["demo"]["company_thesis_status"], "broken")
        later = self.now + timedelta(seconds=1)
        self.write_feed([self.metric(.25, later), self.quote(100, later)])
        recovered = self.runtime.run(now=later)
        self.assertEqual(recovered["reports"]["demo"]["security_readiness"], "re_underwrite")
        self.runtime.restore(before_break)
        self.runtime.create_branch("alternative")
        self.runtime.checkout("alternative")
        alternative = self.runtime.run(now=later)
        self.assertEqual(alternative["reports"]["demo"]["company_thesis_status"], "broken")
        self.watch["thesis"] = "Explicit revised underwriting following the documented break."
        self.runtime.add_watch(self.watch)
        self.runtime.approve("demo")
        revised = self.runtime.run(now=later)
        self.assertEqual(revised["reports"]["demo"]["security_readiness"], "ready")
        self.assert_integrity()

    def test_paused_schedule_stays_idle_while_manual_run_remains_available(self):
        self.runtime.schedule(60, now=self.now)
        self.runtime.pause()
        with patch("cazimi.runtime.fetch_source", wraps=fetch_source) as fetch:
            self.assertEqual(self.runtime.run(now=self.now, force=False)["type"], "idle")
            fetch.assert_not_called()
            self.assertEqual(self.runtime.run(now=self.now)["type"], "research_cycle")
        self.assertEqual(self.runtime.job("main")["enabled"], 0)
        self.assert_integrity()

    def test_scheduled_branch_remains_pinned_after_interactive_checkout(self):
        self.runtime.create_branch("other")
        other_head = self.runtime.resolve("other")
        self.runtime.schedule(60, branch="main", now=self.now)
        self.runtime.checkout("other")
        result = self.runtime.run(now=self.now, force=False)
        self.assertEqual(result["branch"], "main")
        self.assertEqual(self.runtime.branch, "other")
        self.assertEqual(self.runtime.head, other_head)
        main, _ = self.runtime.branch_snapshot("main")
        self.assertEqual(main["research"]["reports"]["demo"]["security_readiness"], "ready")
        self.assertEqual(len(self.runtime.observations("other")), 0)
        self.assert_integrity()

    def test_due_worker_honors_an_explicit_branch_filter(self):
        self.runtime.create_branch("alternative")
        self.runtime.schedule(60, branch="main", now=self.now)
        self.runtime.schedule(60, branch="alternative", now=self.now + timedelta(seconds=1))
        result = self.runtime.run(branch="alternative", now=self.now + timedelta(seconds=1), force=False)
        self.assertEqual(result["branch"], "alternative")
        self.assertEqual(len(self.runtime.observations("main")), 0)
        self.assertEqual(len(self.runtime.observations("alternative")), 2)
        self.assertIsNone(self.runtime.job("main")["last_run"])
        self.assert_integrity()

    def test_live_lease_prevents_second_worker_from_fetching_or_spending(self):
        competing = self.open_runtime()

        def fetch_while_competitor_attempts(source, now):
            with self.assertRaisesRegex(ValueError, "already owns"):
                competing.run(branch="main", now=now)
            return fetch_source(source, now)

        with patch("cazimi.runtime.fetch_source", side_effect=fetch_while_competitor_attempts) as fetch:
            result = self.runtime.run(now=self.now)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["new_evidence"], 2)
        self.assertEqual(self.runtime.usage()["spent"], .5)
        self.assert_integrity()

    def test_abandoned_lease_is_recoverable_after_expiry_and_restart(self):
        self.runtime.schedule(60, now=self.now)
        abandoned = self.runtime._claim("main", self.now, True, False)
        self.runtime.close()
        self.runtime = self.open_runtime()
        before = self.runtime.run(now=self.now + timedelta(seconds=599), force=False)
        self.assertEqual(before["type"], "idle")
        recovered = self.runtime.run(now=self.now + timedelta(seconds=601), force=False)
        self.assertEqual(recovered["type"], "research_cycle")
        self.assertIsNone(self.runtime.job("main")["owner"])
        self.assertNotEqual(abandoned["owner"], self.runtime.job("main")["owner"])
        self.assert_integrity()

    def test_configuration_change_during_fetch_retains_evidence_and_defers_decisions(self):
        def changing_fetch(source, now):
            changed = deepcopy(self.watch)
            changed["thesis"] = "A new thesis requiring another review."
            self.runtime.add_watch(changed)
            return fetch_source(source, now)

        with patch("cazimi.runtime.fetch_source", side_effect=changing_fetch):
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                self.runtime.run(now=self.now)
        self.assertEqual(len(self.runtime.observations()), 2)
        self.assertEqual(self.runtime.load()["research"]["reports"], {})
        self.assertEqual(self.runtime.usage()["spent"], .5)
        self.assertIsNone(self.runtime.job("main")["owner"])
        self.assert_integrity()

    def test_failed_fetch_attempt_cost_survives_restore_and_restart(self):
        original = self.runtime.head
        self.feed.write_text("not JSON", encoding="utf-8")
        result = self.runtime.run(now=self.now)
        self.assertEqual(result["source_errors"], ["Source unavailable: filings"])
        self.assertEqual(result["new_evidence"], 0)
        self.runtime.restore(original)
        self.runtime.close()
        self.runtime = self.open_runtime()
        self.assertEqual(self.runtime.usage()["spent"], .5)
        self.assertEqual(self.runtime.observations(), [])
        self.assert_integrity()

    def test_insufficient_budget_prevents_network_attempt_and_preserves_balance(self):
        self.runtime.set_budget(.25)
        with patch("cazimi.runtime.fetch_source") as fetch:
            result = self.runtime.run(now=self.now)
            fetch.assert_not_called()
        self.assertEqual(result["source_errors"], ["Source unavailable: filings"])
        self.assertNotEqual(result["reports"]["demo"]["security_readiness"], "ready")
        self.assertEqual(self.runtime.usage()["spent"], 0)
        self.assertEqual(self.runtime.usage()["remaining"], .25)
        self.assert_integrity()

    def test_source_failure_degrades_readiness_backs_off_and_recovers_once(self):
        self.runtime.schedule(60, now=self.now)
        self.runtime.run(now=self.now, force=False)
        self.feed.write_text("unavailable", encoding="utf-8")
        failed = self.runtime.run(now=self.now + timedelta(minutes=1))
        self.assertNotEqual(failed["reports"]["demo"]["security_readiness"], "ready")
        self.assertEqual(self.runtime.job("main")["failures"], 1)
        self.assertEqual(self.runtime.job("main")["next_run"], stamp(self.now + timedelta(minutes=3)))
        before = len(self.runtime.alerts(include_acknowledged=True))
        self.runtime.run(now=self.now + timedelta(minutes=2))
        self.assertEqual(len(self.runtime.alerts(include_acknowledged=True)), before)
        self.assertEqual(self.runtime.job("main")["failures"], 2)
        self.write_feed()
        recovered = self.runtime.run(now=self.now + timedelta(minutes=3))
        self.assertEqual(recovered["reports"]["demo"]["security_readiness"], "ready")
        self.assertEqual(self.runtime.job("main")["failures"], 0)
        health = [a["payload"]["errors"] for a in self.runtime.alerts(include_acknowledged=True)
                  if a["payload"]["kind"] == "source_health"]
        self.assertEqual(health, [[], ["Source unavailable: filings"], []])
        self.assert_integrity()

    def test_source_reconfiguration_cannot_reuse_old_revision_as_current_evidence(self):
        self.runtime.run(now=self.now)
        self.runtime.add_source({**self.source, "quality": "user"})
        sources = self.runtime.load()["research"]["sources"]
        self.assertEqual(self.runtime.observations(sources=sources, now=self.now), [])
        revised = self.runtime.run(now=self.now + timedelta(seconds=1))
        self.assertEqual(revised["new_evidence"], 2)
        self.assertEqual(len(self.runtime.observations()), 4)
        self.assertEqual(len(self.runtime.observations(sources=sources)), 2)
        self.assertEqual(revised["reports"]["demo"]["security_readiness"], "conditional")
        self.assert_integrity()

    def test_first_seen_cutoff_excludes_later_collected_old_dated_evidence(self):
        self.runtime.run(now=self.now + timedelta(hours=1))
        self.assertEqual(self.runtime.observations(now=self.now), [])
        self.assertEqual(len(self.runtime.observations(now=self.now + timedelta(hours=1))), 2)

    def test_future_dated_feed_fails_without_selectively_ingesting_valid_records(self):
        self.write_feed([self.metric(), self.quote(when=self.now + timedelta(seconds=1))])
        result = self.runtime.run(now=self.now)
        self.assertEqual(result["new_evidence"], 0)
        self.assertEqual(self.runtime.observations(), [])
        self.assertEqual(result["source_errors"], ["Source unavailable: filings"])
        self.assert_integrity()

    def test_quote_expiry_triggers_reassessment_without_new_evidence(self):
        self.runtime.run(now=self.now)
        result = self.runtime.run(now=self.now + timedelta(days=1))
        self.assertEqual(result["new_evidence"], 0)
        self.assertEqual(result["changed_theses"], ["demo"])
        self.assertNotEqual(result["reports"]["demo"]["security_readiness"], "ready")
        self.assert_integrity()

    def test_large_archive_retains_audit_evidence_but_evaluates_latest_relevant_metrics(self):
        source = self.runtime.load()["research"]["sources"]["filings"]
        template = fetch_source(source, self.now)[0]
        historical = [{**template, "id": "filings:historical-" + str(index),
                       "as_of": (self.now - timedelta(seconds=index + 1)).isoformat()}
                      for index in range(10001)]
        lease = self.runtime._claim("main", self.now, True, False)
        self.runtime._ingest(source, historical, "main", lease, self.now)
        self.runtime._finish(lease, self.now, None)
        result = self.runtime.run(now=self.now)
        self.assertEqual(result["reports"]["demo"]["security_readiness"], "ready")
        self.assertEqual(len(self.runtime.observations()), 10003)
        self.assertEqual(result["reports"]["demo"]["pillars"][0]["evidence_ids"], ["filings:margin"])
        self.assert_integrity()

    def test_new_document_requires_human_review_and_does_not_become_numeric_evidence(self):
        with patch.object(self.runtime, "_model_review") as reviewer:
            self.runtime.run(now=self.now, model_review=True)
            document = {"id": "earnings", "ticker": "DEMO", "kind": "document",
                        "as_of": self.now.isoformat(), "title": "Material issuer update",
                        "citation": "https://issuer.example/new-disclosure"}
            self.write_feed([*self.records, document])
            changed = self.runtime.run(now=self.now + timedelta(seconds=1), model_review=True)
            self.assertEqual(changed["new_evidence"], 1)
            self.assertEqual(changed["changed_theses"], ["demo"])
            report = changed["reports"]["demo"]
            self.assertEqual(report["security_readiness"], "conditional")
            self.assertEqual(report["position_action"], "re_underwrite")
            self.assertEqual(report["company_thesis_status"], "supported")
            self.assertEqual(report["pillars"][0]["evidence_ids"], ["filings:margin"])
            self.assertEqual(len(report["pending_documents"]), 1)
            pending = report["pending_documents"][0]
            self.assertEqual(pending["id"], "filings:earnings")
            self.assertEqual(pending["citation"], document["citation"])
            self.assertEqual(len(pending["revision"]), 64)
            unchanged = self.runtime.run(now=self.now + timedelta(seconds=2), model_review=True)
            self.assertEqual(unchanged["changed_theses"], [])
            self.assertEqual(reviewer.call_count, 2)
        self.runtime.review_document(pending["revision"], "Read disclosure; numeric thesis still requires cited metrics.")
        reviewed = self.runtime.run(now=self.now + timedelta(seconds=3))
        self.assertEqual(reviewed["reports"]["demo"]["pending_documents"], [])
        self.assertEqual(reviewed["reports"]["demo"]["security_readiness"], "ready")
        self.assertEqual(reviewed["reports"]["demo"]["pillars"][0]["evidence_ids"], ["filings:margin"])
        self.assert_integrity()

    def test_acknowledged_alerts_remain_in_history_and_do_not_repeat(self):
        self.runtime.run(now=self.now)
        alerts = self.runtime.alerts()
        for alert in alerts:
            self.runtime.acknowledge(alert["id"])
        self.assertEqual(self.runtime.alerts(), [])
        self.runtime.run(now=self.now + timedelta(seconds=1))
        self.assertEqual(self.runtime.alerts(), [])
        self.assertEqual(len(self.runtime.alerts(include_acknowledged=True)), len(alerts))
        with self.assertRaisesRegex(ValueError, "Unknown alert"):
            self.runtime.acknowledge(9999)
        self.assert_integrity()

    def test_evidence_tampering_is_detected_and_prevents_restart(self):
        self.runtime.run(now=self.now)
        self.runtime.db.execute("UPDATE research_observations SET payload='{}' WHERE source_id='filings'")
        self.assertTrue(self.runtime.verify_research())
        self.runtime.close()
        with self.assertRaisesRegex(ValueError, "Research integrity check failed"):
            ResearchRuntime(self.root)

    def test_research_membership_removal_is_detected_and_prevents_restart(self):
        self.runtime.run(now=self.now)
        self.runtime.db.execute("DELETE FROM research_seen")
        self.assertIn("Evidence memberships differ from audit history", self.runtime.verify_research())
        self.runtime.close()
        with self.assertRaisesRegex(ValueError, "Research integrity check failed"):
            ResearchRuntime(self.root)

    def test_forecast_branch_tampering_prevents_research_startup(self):
        self.runtime.run(now=self.now)
        self.runtime.create_branch("alternate-performance-history")
        self.runtime.db.execute("UPDATE research_forecasts SET branch='alternate-performance-history'")
        self.assertTrue(self.runtime.verify_research())
        self.runtime.close()
        with self.assertRaisesRegex(ValueError, "Research integrity check failed"):
            ResearchRuntime(self.root)

    def test_alert_deduplication_key_tampering_prevents_research_startup(self):
        self.runtime.run(now=self.now)
        self.runtime.db.execute("UPDATE research_alerts SET alert_key='silently-diverted'")
        self.assertTrue(self.runtime.verify_research())
        self.runtime.close()
        with self.assertRaisesRegex(ValueError, "Research integrity check failed"):
            ResearchRuntime(self.root)

    def test_unlogged_alert_acknowledgement_prevents_research_startup(self):
        self.runtime.run(now=self.now)
        self.runtime.db.execute("UPDATE research_alerts SET acknowledged=1")
        self.assertTrue(self.runtime.verify_research())
        self.runtime.close()
        with self.assertRaisesRegex(ValueError, "Research integrity check failed"):
            ResearchRuntime(self.root)

    def test_real_feed_forecast_resolves_only_after_horizon_with_observed_price(self):
        self.runtime.run(now=self.now)
        records = self.runtime.outcomes()
        self.assertEqual(len(records["forecasts"]), 1)
        forecast_id = records["forecasts"][0]["id"]
        self.assertEqual(records["outcomes"], [])
        self.runtime.run(now=self.now + timedelta(days=1))
        self.assertEqual(self.runtime.outcomes()["outcomes"], [])
        observed = self.now + timedelta(days=1, hours=1)
        self.write_feed([self.metric(), self.quote(), self.quote(110, observed, "later")])
        self.runtime.run(now=observed)
        outcomes = self.runtime.outcomes()["outcomes"]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["forecast_id"], forecast_id)
        self.assertEqual(outcomes[0]["observed_price"], 110)
        self.assertAlmostEqual(outcomes[0]["realized_price_return"], .1)
        self.assertIn("excludes dividends", outcomes[0]["limitation"])
        self.runtime.run(now=observed + timedelta(minutes=1))
        self.assertEqual(len(self.runtime.outcomes()["outcomes"]), 1)
        self.assert_integrity()

    def test_contradictory_horizon_prices_do_not_create_an_arbitrary_outcome(self):
        self.runtime.run(now=self.now)
        observed = self.now + timedelta(days=1, hours=1)
        self.write_feed([self.metric(), self.quote(110, observed, "a"), self.quote(120, observed, "b")])
        result = self.runtime.run(now=observed)
        self.assertEqual(result["new_outcomes"], 0)
        self.assertEqual(self.runtime.outcomes()["outcomes"], [])
        self.assertNotEqual(result["reports"]["demo"]["security_readiness"], "ready")
        self.assert_integrity()

    def test_model_failure_preserves_deterministic_report_and_attempt_accounting(self):
        with patch.dict("os.environ", {"DAO_API_KEY": "unused-local-test", "DAO_MODEL": "mock-model", "DAO_REQUEST_COST": "1"}), \
                patch("dao.provider.Config.from_env") as config, \
                patch("dao.provider.ModelClient") as client:
            config.return_value.model = "mock-model"
            client.return_value.complete.side_effect = OSError("sensitive remote details")
            result = self.runtime.run(now=self.now, model_review=True)
        self.assertEqual(result["reports"]["demo"]["security_readiness"], "ready")
        self.assertIn("model review failed", result["review_error"])
        self.assertNotIn("sensitive", json.dumps(self.runtime.alerts(include_acknowledged=True)))
        self.assertEqual(self.runtime.usage()["model_calls"], 1)
        self.assertEqual(self.runtime.usage()["spent"], 1.5)
        self.assertEqual(self.runtime.load()["research"]["reports"]["demo"]["security_readiness"], "ready")
        self.assert_integrity()

    def test_model_proposal_is_recorded_as_ignored_and_cannot_modify_research(self):
        reply = SimpleNamespace(reply="Opposing interpretation needs primary evidence.",
                                proposal={"action": "sell", "fabricated_fact": 999},
                                prompt_tokens=30, completion_tokens=10)
        with patch.dict("os.environ", {"DAO_REQUEST_COST": "1"}), \
                patch("dao.provider.Config.from_env") as config, \
                patch("dao.provider.ModelClient") as client:
            config.return_value.model = "mock-model"
            client.return_value.complete.return_value = reply
            result = self.runtime.run(now=self.now, model_review=True)
        self.assertEqual(result["reports"]["demo"]["position_action"], "consider_buy")
        self.assertEqual(len(self.runtime.observations()), 2)
        self.assertIsNone(self.runtime.load()["draft"])
        commentary = [event["payload"] for event in self.runtime.events(100)
                      if event["kind"] == "research_commentary"]
        self.assertEqual(len(commentary), 1)
        self.assertTrue(commentary[0]["proposal_ignored"])
        self.assertEqual(commentary[0]["reply"], reply.reply)
        self.assertEqual(self.runtime.usage()["prompt_tokens"], 30)
        self.assertEqual(self.runtime.usage()["completion_tokens"], 10)
        self.assert_integrity()

    def test_failed_model_review_retries_unchanged_report_once_then_stays_quiet(self):
        reply = SimpleNamespace(reply="The opposing case needs more evidence.", proposal=None,
                                prompt_tokens=12, completion_tokens=7)
        with patch.dict("os.environ", {"DAO_REQUEST_COST": "1"}), \
                patch("dao.provider.Config.from_env") as config, \
                patch("dao.provider.ModelClient") as client:
            config.return_value.model = "mock-model"
            complete = client.return_value.complete
            complete.side_effect = [OSError("transient provider failure"), reply]
            failed = self.runtime.run(now=self.now, model_review=True)
            retried = self.runtime.run(now=self.now + timedelta(seconds=1), model_review=True)
            quiet = self.runtime.run(now=self.now + timedelta(seconds=2), model_review=True)
        self.assertIsNotNone(failed["review_error"])
        self.assertIsNone(retried["review_error"])
        self.assertEqual(retried["changed_theses"], [])
        self.assertEqual(quiet["changed_theses"], [])
        self.assertEqual(complete.call_count, 2)
        self.assertEqual(self.runtime.usage()["model_calls"], 2)
        self.assertEqual(self.runtime.usage()["spent"], 3.5)
        self.assertEqual(self.runtime.usage()["prompt_tokens"], 12)
        completed = [event for event in self.runtime.events(100)
                     if event["kind"] == "research_review_completed"]
        self.assertEqual(len(completed), 1)
        self.assert_integrity()


if __name__ == "__main__":
    unittest.main()
