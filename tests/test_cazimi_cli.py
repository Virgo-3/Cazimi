"""Offline terminal workflows, machine-readable output and worker lifecycle."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from cazimi.cli import CazimiTerminal, format_result
from cazimi.runtime import ResearchRuntime
from dao.engine import new_plan
from dao.provider import Completion


class CazimiTerminalTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        temporary_root = self.project / ".test-tmp"
        temporary_root.mkdir(exist_ok=True)
        temporary = TemporaryDirectory(dir=temporary_root)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("DAO_", "CAZIMI_"))}
        environment.update(TEMP=str(temporary_root), TMP=str(temporary_root), NO_COLOR="1")
        env_patch = patch.dict(os.environ, environment, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.root = self.directory / "research state"
        self.runtime = ResearchRuntime(self.root)
        self.addCleanup(self.runtime.close)
        self.terminal = CazimiTerminal(self.runtime)
        self.now = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.feed_path = self.directory / "observations.json"
        self.source_path = self.directory / "source config.json"
        self.watch_path = self.directory / "watch config.json"
        self.feed_path.write_text(json.dumps({"observations": [
            {"id": "margin", "ticker": "DEMO", "kind": "metric", "as_of": self.now.isoformat(),
             "metric": "margin", "value": .25, "unit": "fraction", "period": "FY2026",
             "citation": "https://issuer.example/filing", "title": "Issuer filing"},
            {"id": "quote", "ticker": "DEMO", "kind": "price", "as_of": self.now.isoformat(),
             "value": 100, "unit": "USD", "citation": "https://market.example/quote", "title": "Quote"},
        ]}), encoding="utf-8")
        self.source_path.write_text(json.dumps({
            "id": "filings", "kind": "json", "location": self.feed_path.name,
            "category": "company_filings_ir", "quality": "primary", "ticker": "DEMO", "request_cost": .25,
        }), encoding="utf-8")
        self.watch_path.write_text(json.dumps({
            "id": "demo", "ticker": "DEMO", "thesis": "Margin supports the supplied valuation.",
            "horizon_days": 1,
            "pillars": [{"id": "margin", "claim": "Margin remains at least 20 percent.",
                         "metric": "margin", "unit": "fraction", "period": "FY2026",
                         "source_ids": ["filings"], "confirm": {"op": "gte", "value": .2},
                         "warning": {"op": "lt", "value": .18}, "kill": {"op": "lt", "value": .1}}],
            "valuation": {"currency": "USD", "as_of": self.now.isoformat(),
                          "scenarios": [{"name": "bull", "probability": .25, "price": 160},
                                        {"name": "base", "probability": .5, "price": 120},
                                        {"name": "bear", "probability": .25, "price": 80}]},
            "position": {"current_weight": .02, "max_weight": .1},
        }), encoding="utf-8")

    def path_command(self, command, path):
        # Forward slashes work with shlex on Windows and POSIX, including spaces.
        return f'{command} "{path.as_posix()}"'

    def config_commands(self, approve=True):
        commands = [self.path_command("/source-add", self.source_path),
                    self.path_command("/watch-add", self.watch_path)]
        return commands + (["/approve demo"] if approve else [])

    def configure(self, approve=True):
        return [self.terminal.run_line(command) for command in self.config_commands(approve)]

    def subprocess(self, *arguments, input_text=None, extra_env=None):
        environment = dict(os.environ)
        environment.update(extra_env or {})
        return subprocess.run([sys.executable, "-m", "cazimi", *arguments], cwd=self.project,
                              env=environment, input=input_text, capture_output=True,
                              text=True, timeout=20, check=False)

    def batch(self, *commands, root=None, extra_env=None):
        arguments = ["--workspace", str(root or self.root), "--json"]
        for command in commands:
            arguments.extend(("-c", command))
        return self.subprocess(*arguments, extra_env=extra_env)

    def records(self, result):
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    def test_json_registration_approval_run_and_source_linked_reports(self):
        self.configure(approve=False)
        source = self.terminal.run_line("/sources")["items"][0]
        self.assertEqual(Path(source["location"]), self.feed_path)
        self.assertEqual(self.terminal.run_line("/watches")["items"][0]["id"], "demo")
        provisional = self.terminal.run_line("/run")["reports"]["demo"]
        self.assertEqual(provisional["security_readiness"], "not_decision_grade")
        self.terminal.run_line("/approve demo")
        cycle = self.terminal.run_line("/run")
        self.assertEqual(cycle["reports"]["demo"]["security_readiness"], "ready")
        reports = self.terminal.run_line("/reports demo")
        self.assertIn("Company: supported", format_result(reports))
        self.assertIn("Price: 100.0 USD", format_result(reports))
        self.assertEqual(self.terminal.run_line("/show")["type"], "reports")
        self.assertEqual(self.terminal.run_line("/evidence DEMO")["total"], 2)
        self.assertEqual(self.terminal.run_line("/evidence OTHER")["items"], [])
        self.assertEqual(self.terminal.run_line("/outcomes")["type"], "outcomes")
        self.assertIn("verified", self.terminal.run_line("/verify")["message"])

    def test_material_alerts_acknowledge_and_unchanged_poll_stays_quiet(self):
        self.configure()
        first = self.terminal.run_line("/run")
        alerts = self.terminal.run_line("/alerts")["items"]
        self.assertTrue(alerts)
        for alert in alerts:
            self.terminal.run_line("/ack " + str(alert["id"]))
        self.assertEqual(self.terminal.run_line("/alerts")["items"], [])
        self.assertEqual(len(self.terminal.run_line("/alerts --all")["items"]), len(alerts))
        second = self.terminal.run_line("/run")
        self.assertEqual(second["changed_theses"], [])
        self.assertEqual(second["snapshot"], first["snapshot"])
        self.assertEqual(self.terminal.run_line("/alerts")["items"], [])

    def test_schedule_jobs_pause_and_restart_preserve_explicit_worker_state(self):
        self.configure()
        scheduled = self.terminal.run_line("/schedule 60")["items"][0]
        self.assertEqual(scheduled["interval_seconds"], 60)
        self.assertEqual(scheduled["enabled"], 1)
        self.assertIn("enabled", format_result(self.terminal.run_line("/jobs")))
        self.terminal.run_line("/pause")
        self.runtime.close()
        restarted = ResearchRuntime(self.root)
        self.addCleanup(restarted.close)
        terminal = CazimiTerminal(restarted)
        self.assertEqual(terminal.run_line("/jobs")["items"][0]["enabled"], 0)
        self.assertIn("paused/manual", format_result(terminal.run_line("/jobs")))
        self.assertEqual(restarted.run(force=False)["type"], "idle")
        self.assertEqual(terminal.run_line("/verify")["type"], "message")

    def test_manual_goal_preserves_research_registry_and_reports(self):
        self.configure()
        self.terminal.run_line("/run")
        before = deepcopy(self.runtime.load()["research"])
        self.terminal.run_line("/goal Compare an alternative underwriting approach")
        state = self.runtime.load()
        self.assertEqual(state["research"], before)
        self.assertEqual(state["plan"]["goal"], "Compare an alternative underwriting approach")
        self.assertEqual(self.terminal.run_line("/show")["type"], "plan")
        self.assertEqual(self.terminal.run_line("/reports demo")["items"][0]["ticker"], "DEMO")

    def test_inherited_branch_restore_preserves_research_and_global_spending(self):
        self.configure()
        self.terminal.run_line("/goal Compare disciplined underwriting")
        baseline = self.runtime.head
        research = deepcopy(self.runtime.load()["research"])
        self.terminal.run_line("/branch alternative")
        self.terminal.run_line("/checkout alternative")
        self.terminal.run_line("/run")
        self.terminal.run_line("/goal A different provisional commitment")
        self.assertGreater(self.runtime.usage()["spent"], 0)
        spent = self.runtime.usage()["spent"]
        self.terminal.run_line("/restore " + baseline[:12])
        self.assertEqual(self.runtime.load()["research"], research)
        self.assertEqual(self.runtime.load()["plan"]["goal"], "Compare disciplined underwriting")
        self.assertEqual(self.runtime.usage()["spent"], spent)
        self.terminal.run_line("/checkout main")
        self.assertEqual(self.runtime.head, baseline)
        self.assertEqual(self.terminal.run_line("/verify")["type"], "message")

    def test_conversation_gets_reports_without_authority_to_change_research_facts(self):
        self.configure()
        self.terminal.run_line("/run")
        before = deepcopy(self.runtime.load()["research"])
        completion = Completion("Claim to change every observation is commentary only.",
                                new_plan("Consider a competing manual proposal"), 20, 10, "test-model")
        with patch.dict(os.environ, {"DAO_BASE_URL": "https://model.invalid/v1", "DAO_MODEL": "test-model",
                                     "DAO_REQUEST_COST": "0", "DAO_API_KEY": "private-test-key"}):
            with patch("dao.provider.ModelClient.complete", return_value=completion) as complete:
                result = self.terminal.run_line("Discuss the strongest opposing view")
        self.assertEqual(result["type"], "conversation")
        context = complete.call_args.args[1]
        self.assertEqual(context["research_reports"], before["reports"])
        self.assertIn("not trading instructions", context["authority"])
        self.assertEqual(self.runtime.load()["research"], before)
        self.assertEqual(len(self.runtime.observations()), 2)
        self.assertTrue(self.runtime.load()["draft"])
        self.terminal.run_line("/adopt")
        self.assertEqual(self.runtime.load()["research"], before)

    def test_subprocess_json_commands_are_independent_records_and_persist(self):
        result = self.batch(*self.config_commands(), "/run", "/reports demo", "/verify")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = self.records(result)
        self.assertEqual([record["type"] for record in records],
                         ["source", "watch", "watch", "research_cycle", "reports", "message"])
        self.assertEqual(records[-2]["items"][0]["security_readiness"], "ready")
        self.assertNotIn("\x1b", result.stdout)
        reopened = self.subprocess("reports", "--workspace", str(self.root), "--json")
        self.assertEqual(reopened.returncode, 0, reopened.stderr)
        self.assertEqual(self.records(reopened)[0]["items"][0]["watch_id"], "demo")

    def test_script_comments_blank_lines_and_commands_execute_in_documented_order(self):
        script = self.directory / "commands with spaces.txt"
        script.write_text("# Offline configuration\n\n" + "\n".join(self.config_commands()) +
                          "\n  # Review once\n/run\n/reports demo\n", encoding="utf-8")
        result = self.subprocess("--workspace", str(self.root), "--json", "-c", "/sources", "--script", str(script))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = self.records(result)
        self.assertEqual(records[0], {"type": "sources", "items": []})
        self.assertEqual(records[-1]["type"], "reports")
        self.assertEqual(records[-1]["items"][0]["company_thesis_status"], "supported")

    def test_finite_watcher_runs_due_schedule_then_exits_and_schedule_survives(self):
        configured = self.batch(*self.config_commands(), "/schedule 60")
        self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
        result = self.subprocess("watch", "--workspace", str(self.root), "--cycles", "2",
                                 "--poll-seconds", "0.1", "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = self.records(result)
        self.assertEqual([record["type"] for record in records], ["research_cycle", "idle"])
        jobs = self.batch("/jobs")
        job = self.records(jobs)[0]["items"][0]
        self.assertEqual(job["enabled"], 1)
        self.assertIsNone(job["owner"])
        self.assertEqual(job["failures"], 0)
        self.assertTrue(job["last_run"])

    def test_failed_source_run_exits_nonzero_without_response_or_environment_secrets(self):
        self.configure()
        secret = "private-response-secret-981827"
        self.feed_path.write_text(secret, encoding="utf-8")
        result = self.subprocess("run", "--workspace", str(self.root), "--json",
                                 extra_env={"DAO_API_KEY": secret})
        self.assertEqual(result.returncode, 1)
        record = self.records(result)[0]
        self.assertEqual(record["source_errors"], ["Source unavailable: filings"])
        self.assertNotIn(secret, result.stdout + result.stderr)
        audit = json.dumps(self.runtime.events(100))
        self.assertNotIn(secret, audit)
        self.assertNotEqual(record["reports"]["demo"]["security_readiness"], "ready")

    def test_invalid_commands_and_configuration_fail_cleanly_and_stop_batch(self):
        malformed = self.directory / "malformed.json"
        malformed.write_text("[]", encoding="utf-8")
        for command in ("/approve missing", "/run --unknown", "/schedule nan", "/schedule 0",
                        "/alerts --unknown", "/ack impossible", "/not-a-command",
                        self.path_command("/watch-add", malformed)):
            with self.subTest(command=command):
                result = self.batch(command, "/sources")
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("Traceback", result.stderr)
                records = self.records(result)
                self.assertEqual(len(records), 1)
                self.assertIn("message", records[0])

    def test_worker_rejects_invalid_bounds_and_conflicting_dispatch_flags(self):
        for arguments in (("--cycles", "0"), ("--poll-seconds", "nan"), ("--poll-seconds", "61")):
            with self.subTest(arguments=arguments):
                result = self.subprocess("watch", "--workspace", str(self.root), *arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
        result = self.subprocess("watch", "--workspace", str(self.root), "--cycles", "1",
                                 "--branch", "main", "--json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.records(result)[0]["type"], "error")

    def test_missing_model_configuration_fails_without_call_or_spending(self):
        with patch("dao.provider.ModelClient.complete") as complete:
            with self.assertRaisesRegex(ValueError, "DAO_MODEL"):
                self.terminal.run_line("Discuss this provisional thesis")
            complete.assert_not_called()
        self.assertEqual(self.runtime.usage()["spent"], 0)
        result = self.batch("/ask Discuss a competing interpretation")
        self.assertEqual(result.returncode, 1)
        self.assertIn("DAO_MODEL", self.records(result)[0]["message"])
        self.assertNotIn("Traceback", result.stderr)

    def test_help_and_empty_report_commands_explain_the_foreground_worker(self):
        help_result = self.terminal.run_line("/help")
        self.assertIn("only while that worker is running", help_result["text"])
        self.assertIn("no brokerage execution", help_result["text"])
        self.assertIn("No research reports yet", format_result(self.terminal.run_line("/reports")))
        self.assertIn("No schedules", format_result(self.terminal.run_line("/jobs")))
        self.assertEqual(self.terminal.run_line("/reviews")["items"], [])


if __name__ == "__main__":
    unittest.main()
