import copy
import math
import unittest

from dao.engine import add_evidence, evaluate, new_plan, validate_plan


def supported_plan():
    plan = new_plan("Move the service after its prerequisites are established")
    plan["rollback"] = "Restore the previous deployment and verify its health."
    plan["economics"].update(probability=0.6, gain=100, loss=120)
    for item in plan["requirements"]:
        item["evidence"].append({"verdict": "support", "note": "Reviewed the acceptance evidence", "source": "local test"})
    return plan


class PlanValidationTests(unittest.TestCase):
    def test_new_plan_is_provisional_with_unknown_prerequisites(self):
        plan = new_plan("  A proposed future  ")
        self.assertEqual(plan["goal"], "A proposed future")
        self.assertEqual(plan["status"], "provisional")
        self.assertEqual(len(plan["requirements"]), 3)
        self.assertTrue(all(item["evidence"] == [] for item in plan["requirements"]))
        self.assertEqual(evaluate(plan)["decision"], "investigate")

    def test_validation_is_a_deep_copy(self):
        original = supported_plan()
        actual = validate_plan(original)
        actual["requirements"][0]["evidence"][0]["note"] = "Changed"
        self.assertNotEqual(actual, original)

    def test_invalid_goal_and_oversized_input(self):
        for goal in (None, "", "  ", "x" * 8001):
            with self.subTest(goal=str(goal)[:20]), self.assertRaises(ValueError):
                new_plan(goal)

    def test_duplicate_ids_and_missing_dependencies(self):
        plan = supported_plan()
        plan["requirements"][1]["id"] = "success"
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_plan(plan)
        plan = supported_plan()
        plan["requirements"][0]["depends_on"] = ["missing"]
        with self.assertRaisesRegex(ValueError, "missing dependency"):
            validate_plan(plan)

    def test_cycle_and_self_dependency_are_invalid(self):
        plan = supported_plan()
        plan["requirements"][0]["depends_on"] = ["risks"]
        plan["requirements"][1]["depends_on"] = ["success"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            validate_plan(plan)
        plan["requirements"][0]["depends_on"] = ["success"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            validate_plan(plan)

    def test_booleans_and_nonfinite_economics_are_invalid(self):
        for value in (True, False, float("inf"), float("-inf"), float("nan"), "1", 10 ** 1000):
            with self.subTest(value=str(value)[:20]), self.assertRaises(ValueError):
                validate_plan({"goal": "Test", "economics": {"gain": value}})

    def test_probability_accuracy_and_cost_bounds(self):
        for field, value in (("probability", -0.1), ("probability", 1.1), ("signal_accuracy", 0.49), ("signal_accuracy", 1.01), ("loss", -1), ("delay_cost", -1)):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_plan({"goal": "Test", "economics": {field: value}})

    def test_optional_requirements_and_unknown_economics_are_invalid(self):
        plan = supported_plan()
        plan["requirements"][0]["optional"] = True
        with self.assertRaisesRegex(ValueError, "required"):
            validate_plan(plan)
        with self.assertRaisesRegex(ValueError, "unknown economics"):
            validate_plan({"goal": "Test", "economics": {"gian": 100}})

    def test_oversized_counts_and_invalid_evidence(self):
        plan = supported_plan()
        plan["requirements"] = [{"id": f"r{i}", "text": "Required"} for i in range(257)]
        with self.assertRaises(ValueError):
            validate_plan(plan)
        plan = supported_plan()
        plan["requirements"][0]["evidence"] = [{"verdict": "support", "note": "Evidence", "source": "Test"}] * 129
        with self.assertRaises(ValueError):
            validate_plan(plan)
        plan["requirements"][0]["evidence"] = [{"verdict": "approved", "note": "Evidence"}]
        with self.assertRaises(ValueError):
            validate_plan(plan)


class AdjudicationTests(unittest.TestCase):
    def test_supported_positive_action_can_act(self):
        result = evaluate(supported_plan())
        self.assertEqual(result["decision"], "act")
        self.assertAlmostEqual(result["metrics"]["act_value"], 12)

    def test_refutation_defeats_support_and_prior_approval(self):
        plan = supported_plan()
        self.assertEqual(evaluate(plan)["decision"], "act")
        plan["status"] = "approved"
        plan = add_evidence(plan, "risks", "refute", "The recovery test failed", "failure report")
        result = evaluate(plan)
        self.assertEqual(result["decision"], "reject")
        self.assertEqual(result["requirements"][1]["state"], "refuted")
        self.assertEqual(len(plan["requirements"][1]["evidence"]), 2)

    def test_satisfied_node_is_blocked_by_unknown_dependency(self):
        plan = supported_plan()
        plan["requirements"][0]["depends_on"] = ["risks"]
        plan["requirements"][1]["evidence"] = []
        result = evaluate(plan)
        self.assertEqual(result["decision"], "investigate")
        self.assertEqual(result["requirements"][0]["state"], "blocked")
        self.assertEqual(result["requirements"][1]["state"], "unknown")

    def test_empty_requirements_do_not_authorize_action(self):
        plan = supported_plan()
        plan["requirements"] = []
        self.assertEqual(evaluate(plan)["decision"], "investigate")

    def test_missing_recovery_path_blocks_action(self):
        plan = supported_plan()
        plan["rollback"] = "  "
        self.assertEqual(evaluate(plan)["decision"], "investigate")

    def test_unfavorable_or_zero_value_rejects_when_admissible(self):
        for probability in (0.0, 0.5):
            plan = supported_plan()
            plan["economics"].update(probability=probability, gain=100, loss=100)
            self.assertEqual(evaluate(plan)["decision"], "reject")

    def test_action_budget_is_enforced(self):
        plan = supported_plan()
        plan["economics"]["action_cost"] = 2
        self.assertEqual(evaluate(plan, 1)["decision"], "investigate")
        self.assertEqual(evaluate(plan, 2)["decision"], "act")

    def test_evaluation_does_not_mutate_plan(self):
        plan = supported_plan()
        before = copy.deepcopy(plan)
        result = evaluate(plan)
        result["requirements"][0]["evidence"][0]["note"] = "Mutated result"
        self.assertEqual(plan, before)

    def test_add_evidence_does_not_mutate_source(self):
        plan = new_plan("Propose a change")
        updated = add_evidence(plan, "success", "support", "Acceptance test passed", "test report")
        self.assertEqual(plan["requirements"][0]["evidence"], [])
        self.assertEqual(len(updated["requirements"][0]["evidence"]), 1)
        with self.assertRaises(ValueError):
            add_evidence(plan, "missing", "support", "Test", "Test")

    def test_invalid_budget(self):
        for budget in (-1, float("-inf"), float("nan"), True, "2", 10 ** 1000):
            with self.subTest(budget=str(budget)[:20]), self.assertRaises(ValueError):
                evaluate(supported_plan(), budget)


class WaitingValueTests(unittest.TestCase):
    def test_binary_signal_example(self):
        plan = supported_plan()
        plan["economics"].update(signal_accuracy=0.8, observation_cost=5, delay_cost=2, observation_available=True)
        result = evaluate(plan)
        self.assertEqual(result["decision"], "wait")
        self.assertAlmostEqual(result["metrics"]["act_value"], 12)
        self.assertAlmostEqual(result["metrics"]["expected_value_after_signal"], 38.4)
        self.assertAlmostEqual(result["metrics"]["wait_value"], 31.4)
        self.assertAlmostEqual(result["metrics"]["net_information_value"], 19.4)

    def test_unknown_prerequisites_can_wait_but_never_act(self):
        plan = supported_plan()
        plan["requirements"][0]["evidence"] = []
        plan["economics"].update(signal_accuracy=0.8, observation_cost=5, delay_cost=2, observation_available=True)
        self.assertEqual(evaluate(plan)["decision"], "wait")
        plan["economics"]["observation_available"] = False
        self.assertEqual(evaluate(plan)["decision"], "investigate")

    def test_refutation_prevents_waiting_for_favorable_information(self):
        plan = supported_plan()
        plan["economics"].update(signal_accuracy=0.8, observation_available=True)
        plan = add_evidence(plan, "success", "refute", "Acceptance failed", "test report")
        self.assertEqual(evaluate(plan)["decision"], "reject")

    def test_unavailable_or_unaffordable_observation_cannot_cause_wait(self):
        plan = supported_plan()
        plan["economics"].update(signal_accuracy=0.8, observation_cost=5, delay_cost=2)
        self.assertEqual(evaluate(plan)["decision"], "act")
        plan["economics"]["observation_available"] = True
        self.assertEqual(evaluate(plan, remaining_budget=4)["decision"], "act")

    def test_waiting_preserves_budget_for_subsequent_action(self):
        plan = supported_plan()
        plan["economics"].update(signal_accuracy=0.8, observation_cost=5, action_cost=2, observation_available=True)
        self.assertEqual(evaluate(plan, 6)["decision"], "act")
        self.assertEqual(evaluate(plan, 7)["decision"], "wait")

    def test_delay_and_observation_cost_can_remove_waiting_value(self):
        plan = supported_plan()
        plan["economics"].update(signal_accuracy=0.8, observation_cost=5, delay_cost=40, observation_available=True)
        self.assertEqual(evaluate(plan)["decision"], "act")

    def test_uninformative_signal_and_zero_cost_do_not_force_wait(self):
        plan = supported_plan()
        plan["economics"].update(signal_accuracy=0.5, observation_available=True)
        self.assertEqual(evaluate(plan)["decision"], "act")
        self.assertAlmostEqual(evaluate(plan)["metrics"]["net_information_value"], 0)

    def test_impossible_signals_are_skipped_at_certainty(self):
        for probability in (0.0, 1.0):
            plan = supported_plan()
            plan["economics"].update(probability=probability, signal_accuracy=1.0, observation_available=True)
            result = evaluate(plan)
            self.assertEqual(len(result["metrics"]["signals"]), 1)
            self.assertTrue(math.isfinite(result["metrics"]["wait_value"]))
            self.assertAlmostEqual(result["metrics"]["net_information_value"], 0)


if __name__ == "__main__":
    unittest.main()
