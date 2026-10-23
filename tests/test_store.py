"""Persistence, accounting, integrity, and real local action recovery contracts."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dao.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "workspace"
        self.store = self.open_store()

    def open_store(self):
        store = Store(self.root)
        self.addCleanup(store.close)
        return store

    def changed_state(self, message="A proposed future"):
        state = self.store.load()
        state["messages"].append({"role": "user", "content": message})
        return state

    def test_snapshots_survive_restart_and_load_returns_independent_values(self):
        original = self.store.load()
        original["messages"].append({"role": "user", "content": "Unsaved"})
        self.assertEqual(self.store.load()["messages"], [])
        state = self.changed_state()
        head = self.store.commit(state, "Record proposal", self.store.head)
        state["messages"][0]["content"] = "Changed after saving"
        self.store.close()
        reopened = self.open_store()
        self.assertEqual(reopened.head, head)
        self.assertEqual(reopened.load()["messages"][0]["content"], "A proposed future")
        self.assertEqual(reopened.verify(), [])

    def test_branches_diverge_and_restore_adds_history_without_rewriting_it(self):
        initial = self.store.head
        first = self.store.commit(self.changed_state("First"), "First proposal")
        self.store.create_branch("alternative")
        self.store.checkout("alternative")
        alternative = self.store.commit(self.changed_state("Alternative"), "Revise alternative")
        self.store.checkout("main")
        self.assertEqual(self.store.head, first)
        self.assertEqual(len(self.store.load()["messages"]), 1)
        self.store.restore(initial[:12])
        history = self.store.history()
        self.assertEqual([item["hash"] for item in history[1:]], [first, initial])
        self.assertEqual(history[0]["parent"], first)
        self.assertEqual(self.store.load()["messages"], [])
        self.assertEqual(self.store.resolve("alternative"), alternative)
        self.assertEqual(self.store.verify(), [])

    def test_diff_compares_branch_state_and_history_is_limited(self):
        self.store.create_branch("baseline")
        self.store.commit(self.changed_state("Evidence arrived"), "Record evidence")
        diff = self.store.diff("baseline")
        self.assertIn("--- baseline", diff)
        self.assertIn("+++ main", diff)
        self.assertIn('+      "content": "Evidence arrived"', diff)
        self.assertEqual(len(self.store.history(limit=1)), 1)
        self.assertEqual(self.store.diff("main"), "No differences.")

    def test_usage_is_workspace_wide_and_cannot_be_refunded_by_restore_or_restart(self):
        initial = self.store.head
        self.store.charge(2.25, "model", "Investigate", prompt_tokens=40, completion_tokens=20)
        self.store.create_branch("experiment")
        self.store.checkout("experiment")
        self.store.commit(self.changed_state(), "Propose")
        self.store.charge(3.25, "action", "Try local action")
        self.store.restore(initial)
        self.store.checkout("main")
        self.store.close()
        usage = self.open_store().usage()
        self.assertEqual(usage["spent"], 5.5)
        self.assertEqual(usage["remaining"], 94.5)
        self.assertEqual(usage["model_calls"], 1)
        self.assertEqual(usage["prompt_tokens"], 40)
        self.assertEqual(usage["completion_tokens"], 20)
        self.assertEqual(usage["calls_with_unknown_tokens"], 0)

    def test_model_usage_distinguishes_unknown_token_counts_from_zero(self):
        self.store.charge(1, "model", "Provider omitted usage")
        self.store.charge(1, "model", "Zero tokens", prompt_tokens=0, completion_tokens=0)
        usage = self.store.usage()
        self.assertEqual(usage["model_calls"], 2)
        self.assertEqual(usage["calls_with_unknown_tokens"], 1)
        self.assertEqual(usage["prompt_tokens"], 0)

    def test_completed_model_result_reconciles_reserved_usage_without_double_counting(self):
        self.store.charge(1, "model", "Request sent", request_id="completed")
        self.assertEqual(self.store.usage()["calls_with_unknown_tokens"], 1)
        self.store.record("model_result", {"request_id": "completed", "success": True,
                                           "prompt_tokens": 30, "completion_tokens": 10})
        self.store.charge(1, "model", "Request interrupted", request_id="interrupted")
        usage = self.store.usage()
        self.assertEqual(usage["spent"], 2)
        self.assertEqual(usage["model_calls"], 2)
        self.assertEqual(usage["prompt_tokens"], 30)
        self.assertEqual(usage["completion_tokens"], 10)
        self.assertEqual(usage["calls_with_unknown_tokens"], 1)

    def test_overspend_and_budget_below_spend_are_rejected_without_audit_changes(self):
        self.store.set_budget(3)
        self.store.charge(2.5, "model", "First attempt")
        events = self.store.events(100)
        with self.assertRaisesRegex(ValueError, "Insufficient"):
            self.store.charge(0.6, "model", "Unaffordable attempt")
        with self.assertRaisesRegex(ValueError, "already spent"):
            self.store.set_budget(2)
        self.assertEqual(self.store.events(100), events)
        self.assertEqual(self.store.usage()["remaining"], 0.5)

    def test_exhausted_budget_rejects_positive_subnanocredit_charges(self):
        self.store.set_budget(0)
        with self.assertRaisesRegex(ValueError, "Insufficient"):
            self.store.charge(1e-11, "model", "Tiny charge still spends credits")
        self.assertEqual(self.store.usage()["spent"], 0)

    def test_repeated_decimal_charges_use_the_full_budget_without_minting_credits(self):
        self.store.set_budget(0.3)
        for _ in range(3):
            self.store.charge(0.1, "model", "One tenth credit")
        self.assertEqual(self.store.usage()["spent"], 0.3)
        self.assertEqual(self.store.usage()["remaining"], 0)
        with self.assertRaisesRegex(ValueError, "Insufficient"):
            self.store.charge(1e-20, "model", "Still a positive cost")

    def test_budget_cannot_be_reduced_below_exact_spend_hidden_by_float_rounding(self):
        self.store.set_budget(1)
        self.store.charge(0.1, "model", "First cost")
        self.store.charge(1e-18, "model", "Small but real cost")
        with self.assertRaisesRegex(ValueError, "already spent"):
            self.store.set_budget(0.1)
        self.store.charge(0, "model", "A free request remains allowed")

    def test_credit_validation_rejects_invalid_and_unrepresentable_numbers(self):
        for value in (True, False, -1, "2", float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(value=str(value)[:30]), self.assertRaises(ValueError):
                self.store.set_budget(value)
        self.assertEqual(self.store.usage()["limit"], 100)

    def test_competing_terminals_cannot_overwrite_an_unseen_commit(self):
        competing = self.open_store()
        old_head = self.store.head
        stale = competing.load()
        saved = self.store.commit(self.changed_state("Newest"), "Newest evidence", old_head)
        with self.assertRaisesRegex(ValueError, "another terminal"):
            competing.commit(stale, "Overwrite", old_head)
        self.assertEqual(competing.head, saved)
        self.assertEqual(competing.load()["messages"][0]["content"], "Newest")
        self.assertEqual(self.store.verify(), [])

    def test_competing_terminals_share_the_latest_accounting_balance(self):
        competing = self.open_store()
        self.store.set_budget(1)
        self.store.charge(0.75, "model", "First terminal")
        with self.assertRaisesRegex(ValueError, "Insufficient"):
            competing.charge(0.5, "model", "Second terminal")
        self.assertEqual(competing.usage()["remaining"], 0.25)

    def test_snapshot_pairs_state_head_and_branch_and_guards_a_shared_head_checkout(self):
        state, head, branch = self.store.snapshot()
        self.assertEqual(state, self.store.load())
        self.assertEqual(head, self.store.head)
        self.assertEqual(branch, "main")
        self.store.create_branch("same-snapshot")
        competing = self.open_store()
        competing.checkout("same-snapshot")
        self.assertEqual(competing.head, head)
        with self.assertRaisesRegex(ValueError, "another terminal"):
            self.store.commit(state, "Unexpected branch", head, branch)
        with self.assertRaisesRegex(ValueError, "Adjudicate again"):
            self.store.execute_export({"goal": "Unexpected branch"}, 2, head, branch)
        self.assertEqual(self.store.usage()["spent"], 0)
        self.assertEqual(self.store.receipts(), [])
        self.assertEqual(self.store.verify(), [])

    def test_restore_guards_branch_switch_while_resolving_target(self):
        initial = self.store.head
        self.store.create_branch("same-snapshot")

        def change_branch_while_resolving(ref):
            self.store.checkout("same-snapshot")
            return initial

        with patch.object(self.store, "resolve", side_effect=change_branch_while_resolving):
            with self.assertRaisesRegex(ValueError, "another terminal"):
                self.store.restore(initial)
        self.assertEqual(len(self.store.history()), 1)
        self.assertEqual(self.store.verify(), [])

    def test_invalid_commit_rolls_back_every_snapshot_and_audit_change(self):
        before = self.store.head
        count = len(self.store.events(100))
        with self.assertRaises(ValueError):
            self.store.commit({"invalid": float("nan")}, "Invalid data", before)
        self.assertEqual(self.store.head, before)
        self.assertEqual(len(self.store.history()), 1)
        self.assertEqual(len(self.store.events(100)), count)
        self.assertEqual(self.store.verify(), [])

    def test_snapshot_tampering_is_detected_and_prevents_startup(self):
        self.store.db.execute("UPDATE commits SET state=? WHERE hash=?", ('{"messages":[]}', self.store.head))
        self.assertTrue(any("Snapshot hash mismatch" in error for error in self.store.verify()))
        self.store.close()
        with self.assertRaisesRegex(ValueError, "integrity check failed"):
            Store(self.root)

    def test_audit_tampering_is_detected_and_prevents_startup(self):
        self.store.db.execute("UPDATE events SET payload=? WHERE kind='budget'", ('{"limit":999}',))
        self.assertTrue(any("Audit chain mismatch" in error for error in self.store.verify()))
        self.store.close()
        with self.assertRaisesRegex(ValueError, "integrity check failed"):
            Store(self.root)

    def test_branch_pointer_tampering_is_detected_and_prevents_startup(self):
        initial = self.store.head
        self.store.commit(self.changed_state(), "Record proposal")
        self.store.db.execute("UPDATE branches SET head=? WHERE name='main'", (initial,))
        self.assertIn("Branch pointers differ from audit history", self.store.verify())
        self.store.close()
        with self.assertRaisesRegex(ValueError, "integrity check failed"):
            Store(self.root)

    def test_checkout_setting_tampering_is_detected(self):
        self.store.create_branch("alternative")
        self.store.db.execute("UPDATE settings SET value='alternative' WHERE key='active_branch'")
        self.assertIn("Active branch differs from audit history", self.store.verify())

    def test_missing_active_branch_setting_prevents_startup(self):
        self.store.db.execute("DELETE FROM settings WHERE key='active_branch'")
        self.store.close()
        with self.assertRaises(ValueError):
            Store(self.root)

    def test_export_writes_a_real_receipted_file_and_undo_preserves_spend(self):
        content = {"goal": "Accepted commitment", "evidence": ["Test passed"]}
        receipt = self.store.execute_export(content, 2, self.store.head)
        path = Path(receipt["path"])
        self.assertTrue(path.parent.samefile(self.root / "artifacts"))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), content)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), receipt["sha256"])
        self.assertEqual(self.store.receipts()[0]["status"], "completed")
        self.assertEqual(self.store.verify(), [])
        self.store.undo_export(receipt["id"])
        self.assertFalse(path.exists())
        self.assertEqual(self.store.receipts()[0]["status"], "undone")
        self.assertEqual(self.store.usage()["spent"], 2)
        self.assertEqual(self.store.verify(), [])
        with self.assertRaisesRegex(ValueError, "already undone"):
            self.store.undo_export(receipt["id"])

    def test_stale_export_is_denied_before_charging_or_creating_a_receipt(self):
        stale = self.store.head
        self.store.commit(self.changed_state(), "New evidence invalidates ruling")
        with self.assertRaisesRegex(ValueError, "Adjudicate again"):
            self.store.execute_export({"goal": "Stale"}, 2, stale)
        self.assertEqual(self.store.usage()["spent"], 0)
        self.assertEqual(self.store.receipts(), [])
        self.assertEqual(list((self.root / "artifacts").glob("*.json")), [])

    def test_edited_artifact_is_reported_and_undo_refuses_to_delete_it(self):
        receipt = self.store.execute_export({"goal": "Original"}, 1, self.store.head)
        path = Path(receipt["path"])
        path.write_text("External edits", encoding="utf-8")
        self.assertTrue(any("Artifact missing or changed" in error for error in self.store.verify()))
        with self.assertRaisesRegex(ValueError, "Refusing to delete"):
            self.store.undo_export(receipt["id"])
        self.assertEqual(path.read_text(encoding="utf-8"), "External edits")
        self.assertEqual(self.store.receipts()[0]["status"], "completed")

    def test_malformed_receipt_is_reported_as_corruption_without_crashing_verification(self):
        self.store.execute_export({"goal": "Original"}, 1, self.store.head)
        self.store.db.execute("UPDATE events SET payload='{}' WHERE kind='artifact_completed'")
        errors = self.store.verify()
        self.assertTrue(any("Audit chain mismatch" in error for error in errors))

    def test_missing_artifact_can_be_acknowledged_as_undone_without_refund(self):
        receipt = self.store.execute_export({"goal": "Original"}, 1, self.store.head)
        Path(receipt["path"]).unlink()
        self.assertTrue(any("Artifact missing or changed" in error for error in self.store.verify()))
        self.store.undo_export(receipt["id"])
        self.assertEqual(self.store.usage()["spent"], 1)
        self.assertEqual(self.store.verify(), [])

    def test_interrupted_completion_recovers_from_prepared_receipt_after_restart(self):
        with patch.object(self.store, "record", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.execute_export({"goal": "Interrupted"}, 3, self.store.head)
        receipt = self.store.receipts()[0]
        path = self.root / "artifacts" / (receipt["id"] + ".json")
        self.assertEqual(receipt["status"], "prepared")
        self.assertTrue(path.exists())
        self.store.close()
        reopened = self.open_store()
        reopened.undo_export(receipt["id"])
        self.assertFalse(path.exists())
        self.assertEqual(reopened.receipts()[0]["status"], "undone")
        self.assertEqual(reopened.usage()["spent"], 3)
        self.assertEqual(reopened.verify(), [])

    def test_failed_export_never_overwrites_an_existing_file_and_keeps_attempt_cost(self):
        artifact_id = "a" * 32
        folder = self.root / "artifacts"
        folder.mkdir()
        path = folder / (artifact_id + ".json")
        path.write_text("Preexisting contents", encoding="utf-8")
        with patch("dao.store.uuid4", return_value=SimpleNamespace(hex=artifact_id)):
            with self.assertRaisesRegex(ValueError, "attempted action remains accounted"):
                self.store.execute_export({"goal": "Collision"}, 4, self.store.head)
        self.assertEqual(path.read_text(encoding="utf-8"), "Preexisting contents")
        self.assertEqual(self.store.receipts()[0]["status"], "failed")
        self.assertEqual(self.store.usage()["spent"], 4)
        with self.assertRaisesRegex(ValueError, "Refusing to delete"):
            self.store.undo_export(artifact_id)

    def test_receipt_ids_cannot_escape_the_artifact_directory(self):
        for artifact_id in ("../outside", "x" * 32, "a" * 31, "a" * 33):
            with self.subTest(artifact_id=artifact_id), self.assertRaises(ValueError):
                self.store.undo_export(artifact_id)
        self.assertEqual(self.store.receipts(), [])


if __name__ == "__main__":
    unittest.main()
