"""User-visible terminal workflows, persistence, and model authority boundaries."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from dao.cli import Terminal, clean, main
from dao.engine import new_plan
from dao.provider import Completion
from dao.store import Store


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.store = Store(self.directory / "terminal state")
        self.addCleanup(self.store.close)
        self.terminal = Terminal(self.store)
        # Tests never inherit a real model server, key, or billable configuration.
        environment = {key: value for key, value in os.environ.items() if not key.startswith("DAO_")}
        self.env = patch.dict(os.environ, environment, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_commands(self, *commands):
        return [self.terminal.run_line(command) for command in commands]

    def prepared_plan(self, *, evidence=True):
        self.run_commands(
            "/goal Publish a reversible event plan",
            "/rollback Delete the exported plan; no external booking is made",
            "/economics probability=0.9 gain=20 loss=10 action_cost=2 observation_available=false",
        )
        if evidence:
            for identifier in ("success", "risks", "rollback"):
                self.terminal.run_line(f'/support {identifier} "review notes" "The prerequisite was checked"')

    def configured_model(self, **extra):
        return patch.dict(os.environ, {
            "DAO_BASE_URL": "https://explicit-test-model.invalid/v1",
            "DAO_MODEL": "test-model",
            "DAO_REQUEST_COST": "1.5",
            **extra,
        })

    def subprocess(self, *arguments, input_text=None):
        return subprocess.run(
            [sys.executable, "-m", "dao", *arguments],
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ), input=input_text, text=True,
            capture_output=True, timeout=15, check=False,
        )

    def test_manual_workflow_exports_and_undo_preserves_receipt_and_cost(self):
        self.prepared_plan()
        ruling = self.terminal.run_line("/decide")
        self.assertEqual(ruling["decision"], "act")
        result = self.terminal.run_line("/act")
        receipt = result["receipt"]
        artifact = Path(receipt["path"])
        self.assertTrue(artifact.is_file())
        content = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual(content["goal"], "Publish a reversible event plan")
        self.assertEqual(content["ruling"]["decision"], "act")
        self.assertEqual(content["snapshot"], receipt["snapshot"])
        self.assertEqual(self.terminal.run_line("/usage")["spent"], 2)
        self.assertEqual(self.terminal.run_line("/receipts")["items"][0]["status"], "completed")
        self.terminal.run_line("/undo " + receipt["id"])
        self.assertFalse(artifact.exists())
        self.assertEqual(self.terminal.run_line("/receipts")["items"][0]["status"], "undone")
        self.assertEqual(self.terminal.run_line("/usage")["spent"], 2)
        self.assertEqual(self.terminal.run_line("/verify")["type"], "message")

    def test_refutation_defeats_prior_approval_and_later_support(self):
        self.prepared_plan()
        self.assertEqual(self.terminal.run_line("/decide")["decision"], "act")
        self.run_commands(
            '/refute risks "new inspection" "A material risk is unacceptable"',
            '/support risks "old review" "The earlier review was positive"',
        )
        result = self.terminal.run_line("/decide")
        self.assertEqual(result["decision"], "reject")
        self.assertEqual(next(r for r in result["requirements"] if r["id"] == "risks")["state"], "refuted")
        with self.assertRaisesRegex(ValueError, "cannot act"):
            self.terminal.run_line("/act")
        self.assertEqual(self.store.receipts(), [])
        self.assertEqual(self.store.usage()["spent"], 0)
        adjudications = [event for event in self.store.events(100) if event["kind"] == "adjudication"]
        self.assertEqual(adjudications[0]["payload"]["ruling"]["decision"], "act")
        self.assertEqual(adjudications[-1]["payload"]["ruling"]["decision"], "reject")

    def test_amending_a_prerequisite_clears_all_stale_evidence(self):
        self.prepared_plan()
        before = self.store.head
        self.terminal.run_line("/amend success Success now requires a different measurable outcome")
        state = self.store.load()
        self.assertNotEqual(self.store.head, before)
        self.assertTrue(all(not item["evidence"] for item in state["plan"]["requirements"]))
        self.assertEqual(state["plan"]["economics"]["gain"], 20)
        self.assertEqual(self.terminal.run_line("/decide")["decision"], "investigate")

    def test_changing_recovery_path_clears_evidence_before_readjudication(self):
        self.prepared_plan()
        self.terminal.run_line("/rollback Recover using a newly designed procedure")
        self.assertTrue(all(not item["evidence"] for item in self.store.load()["plan"]["requirements"]))
        self.assertEqual(self.terminal.run_line("/decide")["decision"], "investigate")

    def test_branch_restore_keeps_workspace_usage_and_appends_history(self):
        self.prepared_plan(evidence=False)
        self.terminal.run_line("/economics probability=0.5 gain=100 loss=100 observation_cost=3 delay_cost=1 signal_accuracy=0.9 observation_available=true")
        baseline = self.store.head
        self.run_commands("/branch alternative", "/checkout alternative")
        self.assertEqual(self.terminal.run_line("/decide")["decision"], "wait")
        self.terminal.run_line('/observe positive "survey" "Positive signal received"')
        observed = self.store.head
        self.assertAlmostEqual(self.store.load()["plan"]["economics"]["probability"], 0.9)
        self.assertFalse(self.store.load()["plan"]["economics"]["observation_available"])
        self.assertEqual(self.store.usage()["spent"], 3)
        self.assertIn("probability", self.terminal.run_line("/diff main")["text"])
        self.terminal.run_line("/restore " + baseline[:12])
        self.assertEqual(self.store.history()[0]["parent"], observed)
        self.assertAlmostEqual(self.store.load()["plan"]["economics"]["probability"], 0.5)
        self.assertEqual(self.store.usage()["spent"], 3)
        self.terminal.run_line("/checkout main")
        self.assertEqual(self.store.head, baseline)
        self.assertEqual(self.store.usage()["spent"], 3)
        self.assertEqual(self.store.verify(), [])

    def test_invalid_economics_is_atomic_and_does_not_create_snapshots(self):
        self.prepared_plan(evidence=False)
        before, head, _ = self.store.snapshot()
        for command in (
            "/economics gain=999 probability=2",
            "/economics gain=nan", "/economics loss=-1",
            "/economics observation_available=maybe", "/economics gains=100",
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.terminal.run_line(command)
            self.assertEqual(self.store.head, head)
            self.assertEqual(self.store.load(), before)

    def test_requirement_dependencies_are_checked_before_saving(self):
        self.prepared_plan(evidence=False)
        self.terminal.run_line("/require venue Venue is suitable --after success,risks")
        self.terminal.run_line('/support venue "site report" "The venue passed its review"')
        venue = next(item for item in self.terminal.run_line("/show")["ruling"]["requirements"] if item["id"] == "venue")
        self.assertEqual(venue["state"], "blocked")
        head = self.store.head
        for command in ("/require missing Invalid dependency --after nonexistent", "/amend success A cyclic dependency --after venue"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.terminal.run_line(command)
            self.assertEqual(self.store.head, head)

    def test_unconfigured_conversation_fails_without_any_charge_or_state_change(self):
        before, head, _ = self.store.snapshot()
        for line in ("Hello, plan an event", "/ask Plan an event"):
            with self.subTest(line=line), self.assertRaisesRegex(ValueError, "DAO_MODEL and DAO_BASE_URL"):
                self.terminal.run_line(line)
        self.assertEqual(self.store.head, head)
        self.assertEqual(self.store.load(), before)
        self.assertEqual(self.store.usage()["model_calls"], 0)
        self.assertEqual(self.store.usage()["spent"], 0)

    def test_model_stages_only_and_adoption_cannot_keep_generated_authority(self):
        self.prepared_plan(evidence=False)
        original = deepcopy(self.store.load()["plan"])
        proposal = new_plan("A different proposed future")
        proposal["rollback"] = "Delete a reversible artifact"
        proposal["status"] = "approved"
        proposal["economics"].update({"probability": 1.0, "gain": 100.0})
        for requirement in proposal["requirements"]:
            requirement["evidence"] = [{"verdict": "support", "source": "model", "note": "Invented approval"}]
        completion = Completion("Review this suggestion.", proposal, 21, 8, "test-model")
        with self.configured_model(), patch("dao.provider.ModelClient") as model:
            model.return_value.complete.return_value = completion
            result = self.terminal.run_line("Suggest another outcome")
        self.assertTrue(result["draft_available"])
        self.assertEqual(self.store.load()["plan"], original)
        self.assertEqual(self.terminal.run_line("/draft")["plan"]["goal"], proposal["goal"])
        self.assertEqual(self.terminal.run_line("/decide")["decision"], "investigate")
        self.terminal.run_line("/adopt")
        adopted = self.store.load()["plan"]
        self.assertEqual(adopted["goal"], proposal["goal"])
        self.assertEqual(adopted["status"], "provisional")
        self.assertTrue(all(not requirement["evidence"] for requirement in adopted["requirements"]))
        self.assertEqual(adopted["economics"], new_plan("defaults")["economics"])
        self.assertIsNone(self.store.load()["draft"])
        self.assertNotEqual(self.terminal.run_line("/decide")["decision"], "act")

    def test_model_precharge_actual_tokens_and_failure_are_separate_audit_facts(self):
        count = 0

        def complete(*_args):
            nonlocal count
            count += 1
            self.assertEqual(self.store.usage()["spent"], 1.5 * count)
            if count == 3:
                raise ValueError("The test connection failed.")
            return Completion("What can disconfirm this?", None, 17 if count == 1 else None, 9 if count == 1 else None, "test-model")

        with self.configured_model(), patch("dao.provider.ModelClient") as model:
            model.return_value.complete.side_effect = complete
            self.terminal.run_line("First request")
            self.terminal.run_line("Second request")
            before, head, _ = self.store.snapshot()
            with self.assertRaisesRegex(ValueError, "test connection"):
                self.terminal.run_line("Failed third request")
        self.assertEqual(self.store.head, head)
        self.assertEqual(self.store.load(), before)
        usage = self.terminal.run_line("/usage")
        self.assertEqual(usage["spent"], 4.5)
        self.assertEqual(usage["model_calls"], 3)
        self.assertEqual(usage["prompt_tokens"], 17)
        self.assertEqual(usage["completion_tokens"], 9)
        self.assertEqual(usage["calls_with_unknown_tokens"], 2)
        events = self.store.events(100)
        charges = [event for event in events if event["kind"] == "usage"]
        outcomes = [event for event in events if event["kind"] == "model_result"]
        self.assertEqual([item["payload"]["request_id"] for item in charges], [item["payload"]["request_id"] for item in outcomes])
        self.assertFalse(outcomes[-1]["payload"]["success"])
        self.assertEqual(self.store.verify(), [])

    def test_model_request_is_not_sent_when_budget_cannot_cover_precharge(self):
        self.terminal.run_line("/budget 1")
        with self.configured_model(), patch("dao.provider.ModelClient") as model:
            with self.assertRaisesRegex(ValueError, "Insufficient workspace credits"):
                self.terminal.run_line("An unaffordable conversation")
            model.assert_not_called()
        self.assertEqual(self.store.usage()["spent"], 0)
        self.assertEqual(self.store.usage()["model_calls"], 0)

    def test_invalid_model_request_cost_does_not_charge_or_send(self):
        with self.configured_model(DAO_REQUEST_COST="nan"), patch("dao.provider.ModelClient") as model:
            with self.assertRaisesRegex(ValueError, "finite nonnegative"):
                self.terminal.run_line("Invalid cost")
            model.assert_not_called()
        self.assertEqual(self.store.usage()["spent"], 0)

    def test_import_validates_user_assertions_and_failed_import_keeps_state(self):
        self.prepared_plan(evidence=False)
        path = self.directory / "user plan.json"
        supplied = new_plan("Imported user plan")
        supplied["status"] = "approved"
        path.write_text(json.dumps(supplied), encoding="utf-8")
        self.terminal.run_line('/import "' + path.as_posix() + '"')
        self.assertEqual(self.store.load()["plan"]["goal"], "Imported user plan")
        self.assertNotEqual(self.terminal.run_line("/decide")["decision"], "act")
        head = self.store.head
        supplied["economics"]["probability"] = 2
        path.write_text(json.dumps(supplied), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.terminal.run_line('/import "' + path.as_posix() + '"')
        self.assertEqual(self.store.head, head)

    def test_observation_updates_probability_once_without_creating_support(self):
        self.prepared_plan(evidence=False)
        self.terminal.run_line("/economics probability=0.5 gain=100 loss=100 observation_cost=3 signal_accuracy=0.9 observation_available=true")
        self.terminal.run_line('/observe negative "experiment" "Negative outcome"')
        plan = self.store.load()["plan"]
        self.assertAlmostEqual(plan["economics"]["probability"], 0.1)
        self.assertTrue(all(not item["evidence"] for item in plan["requirements"]))
        self.assertEqual(self.store.usage()["spent"], 3)
        head = self.store.head
        with self.assertRaisesRegex(ValueError, "No observation"):
            self.terminal.run_line('/observe positive "experiment" "Try again"')
        self.assertEqual(self.store.usage()["spent"], 3)
        self.assertEqual(self.store.head, head)

    def test_impossible_observation_does_not_charge_or_change_probability(self):
        self.prepared_plan(evidence=False)
        self.terminal.run_line("/economics probability=1 gain=20 loss=10 signal_accuracy=1 observation_cost=3 observation_available=true")
        before, head, _ = self.store.snapshot()
        with self.assertRaisesRegex(ValueError, "signal is impossible"):
            self.terminal.run_line('/observe negative "experiment" "An impossible signal"')
        self.assertEqual(self.store.usage()["spent"], 0)
        self.assertEqual(self.store.head, head)
        self.assertEqual(self.store.load(), before)

    def test_subprocess_repeatable_commands_json_and_errors_stop_execution(self):
        workspace = self.directory / "batch state with spaces"
        result = self.subprocess("--workspace", str(workspace), "--json", "-c", "/goal Persisted from the real executable", "-c", "/show")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row["type"] for row in rows], ["saved", "plan"])
        self.assertEqual(rows[-1]["plan"]["goal"], "Persisted from the real executable")
        failed = self.subprocess("--workspace", str(workspace), "--json", "-c", "/unknown", "-c", "/goal Must not run")
        self.assertEqual(failed.returncode, 1)
        errors = [json.loads(line) for line in failed.stdout.splitlines()]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["type"], "error")
        reopened = Store(workspace)
        try:
            self.assertEqual(reopened.load()["plan"]["goal"], "Persisted from the real executable")
        finally:
            reopened.close()

    def test_subprocess_script_comments_and_quit_are_respected(self):
        workspace = self.directory / "script state"
        script = self.directory / "commands.dao"
        script.write_text("# A reviewable batch\n\n/goal A scripted future\n/show\n/quit\n/goal Must not run\n", encoding="utf-8")
        result = self.subprocess("--workspace", str(workspace), "--json", "--script", str(script))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["plan"]["goal"], "A scripted future")

    def test_subprocess_input_eof_exits_cleanly_without_model_configuration(self):
        result = self.subprocess("--workspace", str(self.directory / "eof state"), "--no-color", input_text="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("manual planning; model unconfigured", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_main_neutralizes_controls_and_json_escapes_them(self):
        goal = "Visible goal \x1b[2J\x9b31m\x07"
        for json_mode in (False, True):
            output, errors = StringIO(), StringIO()
            args = ["--workspace", str(self.directory / ("json controls" if json_mode else "text controls")), "--no-color", "-c", "/goal " + goal, "-c", "/show"]
            if json_mode:
                args.append("--json")
            with redirect_stdout(output), redirect_stderr(errors):
                status = main(args)
            self.assertEqual(status, 0, errors.getvalue())
            rendered = output.getvalue()
            self.assertNotIn("\x1b", rendered)
            self.assertNotIn("\x9b", rendered)
            self.assertNotIn("\x07", rendered)
            self.assertIn("Visible goal", rendered)
            if json_mode:
                self.assertEqual(json.loads(rendered.splitlines()[-1])["plan"]["goal"], goal)
        self.assertEqual(clean("line\n\t\r\x00\x1b\x9f"), "line\n\t????")

    def test_interactive_errors_allow_correction_and_quit_preserves_state(self):
        workspace = self.directory / "interactive state"
        output, errors = StringIO(), StringIO()
        with patch("builtins.input", side_effect=["/unknown", "/goal Corrected future", "/quit"]), redirect_stdout(output), redirect_stderr(errors):
            status = main(["--workspace", str(workspace), "--no-color"])
        self.assertEqual(status, 0)
        self.assertIn("Unknown command", errors.getvalue())
        self.assertIn("Corrected future", output.getvalue())
        reopened = Store(workspace)
        try:
            self.assertEqual(reopened.load()["plan"]["goal"], "Corrected future")
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
