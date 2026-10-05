from datetime import date, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from stock_jp.research.forward_holdout import CODE, JST, MANIFEST, PLAN, freeze, observe
from stock_jp.research.historical_replay import evaluate_holdout


class Store:
    def __init__(self, root, generation):
        self.files = {"jquants/datasets/equities_daily/2026-10-02/latest.json": {}}
        self.used = {}

    def read(self, name):
        self.used[name] = "c" * 64
        return json.dumps({"status": "complete", "row_count": 4000, "completed_at": "2026-10-02T22:00:00+09:00"}).encode()


def fixture(root):
    for name in CODE:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("Frozen synthetic rule or execution code")
    p = root / PLAN
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema_version": "1.0", "status": "NOT_DECIDED", "rule_frozen_at_jst": "2026-09-01T22:21:26+09:00", "acceptance_criteria": {"minimum_monthly_evaluation_count": 6, "minimum_trade_count": 10, "maximum_drawdown_floor_pct": -30, "maximum_single_name_loss_floor_pct": -10, "maximum_industry_loss_floor_pct": -20}, "acknowledgements": {"period_was_unobserved_when_frozen": False, "inputs_and_execution_rules_frozen": False, "thresholds_frozen_before_results": False, "changes_restart_the_holdout": False}}))
    p = root / "config/policy.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"operation_mode": "PAPER", "active_rule_version": "v0.4"}))
    p = root / "data/market/jquants/current.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": 2, "generation": "b" * 64}))


class ForwardHoldoutTest(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 10, 5, 13, tzinfo=JST)

    def test_freeze_is_future_only_and_preserves_thresholds_no_live_approval(self):
        with TemporaryDirectory() as temporary, patch("stock_jp.research.forward_holdout.now", return_value=self.at):
            root = Path(temporary)
            fixture(root)
            with self.assertRaises(ValueError):
                freeze(root, start=date(2026, 10, 5), through=date(2027, 4, 1))
            plan = freeze(root, start=date(2026, 10, 6), through=date(2027, 4, 1))
            self.assertEqual(plan["acceptance_criteria"]["minimum_trade_count"], 10)
            self.assertFalse(plan["authorization"]["live_promotion_approved"])
            with self.assertRaises(ValueError):
                freeze(root, start=date(2026, 11, 1), through=date(2027, 5, 1))

    def test_observation_credits_no_snapshot_as_trade_or_evaluation_and_is_read_only(self):
        with TemporaryDirectory() as temporary, patch("stock_jp.research.forward_holdout.now", return_value=self.at), patch("stock_jp.research.forward_holdout.FrozenStore", Store):
            root = Path(temporary)
            fixture(root)
            freeze(root, start=date(2026, 10, 6), through=date(2027, 4, 1))
            before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            result = observe(root)
            self.assertEqual(result["status"], "PRE_PERIOD_SETUP")
            self.assertFalse(result["formal_inputs_ready"])
            self.assertEqual(result["formal_trades_credited"], 0)
            self.assertEqual(result["formal_monthly_evaluations_credited"], 0)
            self.assertFalse(result["forward_holdout_completed"])
            self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_modified_rule_invalidates_and_past_period_is_never_complete_by_elapsed_days(self):
        with TemporaryDirectory() as temporary, patch("stock_jp.research.forward_holdout.now", return_value=self.at), patch("stock_jp.research.forward_holdout.FrozenStore", Store):
            root = Path(temporary)
            fixture(root)
            freeze(root, start=date(2026, 10, 6), through=date(2027, 4, 1))
            (root / CODE[0]).write_text("Changed after freeze")
            self.assertEqual(observe(root)["status"], "INVALIDATED")
            (root / CODE[0]).write_text("Frozen synthetic rule or execution code")
            with patch("stock_jp.research.forward_holdout.now", return_value=datetime(2027, 4, 2, tzinfo=JST)):
                result = observe(root)
                self.assertEqual(result["status"], "AWAITING_FINAL_REPLAY")
                self.assertFalse(result["forward_holdout_completed"])

    def test_final_observer_uses_validated_engine_and_never_grants_live_approval(self):
        class CompleteStore(Store):
            def __init__(self, root, generation):
                super().__init__(root, generation)
                self.files = {"jquants/datasets/equities_daily/2027-04-01/latest.json": {},
                              "jquants/datasets/indices_daily/2027-04-01/latest.json": {}}

        with TemporaryDirectory() as temporary, patch("stock_jp.research.forward_holdout.now", return_value=self.at):
            root = Path(temporary)
            fixture(root)
            plan = freeze(root, start=date(2026, 10, 6), through=date(2027, 4, 1))
            path = root / MANIFEST
            path.parent.mkdir(parents=True)
            path.write_text("Synthetic manifest handled by the validator fixture")
            model = {"manifest": {"evaluation_kind": "FORWARD_HOLDOUT", "period": plan["period"]}}
            metrics = {"trade_count": 10, "maximum_drawdown_pct": -29,
                       "maximum_single_name_loss_contribution_pct": -9,
                       "maximum_industry_loss_contribution_pct": -19}
            with patch("stock_jp.research.forward_holdout.now", return_value=datetime(2027, 4, 2, tzinfo=JST)), \
                    patch("stock_jp.research.forward_holdout.FrozenStore", CompleteStore), \
                    patch("stock_jp.research.forward_holdout.validate_inputs", return_value=(model, {"monthly_evaluation_count": 6})), \
                    patch("stock_jp.research.forward_holdout.replay_version", return_value=SimpleNamespace(summary=metrics)) as engine:
                result = observe(root)
                engine.assert_called_once_with(model=model, rule_version="v0.4")
                self.assertTrue(result["forward_holdout_completed"])
                self.assertEqual(result["formal_monthly_evaluations_credited"], 6)
                self.assertEqual(result["formal_trades_credited"], 10)
                self.assertEqual(result["final_evaluation"]["outcome"], "PASS")
                self.assertFalse(result["live_promotion_approved"])

    def test_insufficient_count_is_inconclusive_and_losses_are_still_reported(self):
        plan = {"acceptance_criteria": {"minimum_monthly_evaluation_count": 6, "minimum_trade_count": 10,
                "maximum_drawdown_floor_pct": -30, "maximum_single_name_loss_floor_pct": -10,
                "maximum_industry_loss_floor_pct": -20}}
        metrics = {"trade_count": 0, "maximum_drawdown_pct": -31,
                   "maximum_single_name_loss_contribution_pct": -9,
                   "maximum_industry_loss_contribution_pct": -19}
        result = evaluate_holdout(plan=plan, quality={"monthly_evaluation_count": 6}, v04=metrics)
        self.assertEqual(result["outcome"], "INCONCLUSIVE")
        self.assertIn("maximum_drawdown_floor_pct", result["criterion_failures"])
        metrics["trade_count"] = 10
        self.assertEqual(evaluate_holdout(plan=plan, quality={"monthly_evaluation_count": 6}, v04=metrics)["outcome"], "FAIL")
