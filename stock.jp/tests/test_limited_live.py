import csv
import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.limited_live import (
    apply_limited_live,
    evaluate_limited_live_plan,
    validate_applied_limited_live,
    validate_limited_live_order,
)
from scripts.operation_state import initialize_or_migrate_workspace


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LimitedLiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "operations").mkdir()
        shutil.copytree(
            PROJECT_ROOT / "operations/templates", self.root / "operations/templates"
        )
        initialize_or_migrate_workspace(self.root)
        diagnostic_path = (
            self.root / "data/tenbagger-v0.4-allocation-replay-2025-summary.json"
        )
        diagnostic_path.parent.mkdir(parents=True)
        diagnostic_path.write_text(
            json.dumps(
                {
                    "status": "ALLOCATION_DIAGNOSTIC_ONLY",
                    "forward_paper_gate_satisfied": False,
                    "results": {"v0.4": {"maximum_drawdown_pct": -27.38132}},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.plan_path = (
            self.root / "operations/private/evidence/limited-live-plan.json"
        )
        self.plan_path.parent.mkdir(parents=True)
        template = json.loads(
            (
                self.root
                / "operations/templates/live-gate-evidence/limited-live-plan-template.json"
            ).read_text(encoding="utf-8")
        )
        template.update(
            {
                "status": "APPROVED",
                "decision": "START_LIMITED_LIVE",
                "capital_limit_jpy": 3_000_000,
                "maximum_total_loss_pct": 10.0,
                "approved_by": "portfolio-owner",
                "approved_at_jst": "2026-09-06T08:00:00+09:00",
            }
        )
        template["retrospective_diagnostic"].update(
            {
                "sha256": hashlib.sha256(diagnostic_path.read_bytes()).hexdigest(),
                "accepted_maximum_drawdown_pct": -27.38132,
            }
        )
        template["acknowledgements"] = {
            name: True for name in template["acknowledgements"]
        }
        self.plan_path.write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.capital_path = self.root / "operations/private/capital-ledger.csv"
        with self.capital_path.open("a", encoding="utf-8", newline="") as destination:
            csv.writer(destination).writerow(
                [
                    "limited-live-funding",
                    "2026-09-06T09:00:00+09:00",
                    "LIMITED_LIVE_FUNDING",
                    "3000000",
                    "3000000",
                    "",
                    "",
                    "operations/private/evidence/limited-live-plan.json",
                    "dedicated limited live sleeve",
                ]
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _eligible_gate(name: str) -> dict:
        metrics = (
            {"risk_limits_pct": {"maximum_total_loss_stop": 10.0}}
            if name == "personal"
            else {}
        )
        return {"eligible": True, "blockers": [], "metrics": metrics, "inputs": []}

    def _gate_patches(self):
        return (
            patch(
                "scripts.limited_live.evaluate_official_coverage",
                return_value=self._eligible_gate("official"),
            ),
            patch(
                "scripts.limited_live.evaluate_repository_recovery",
                return_value=self._eligible_gate("recovery"),
            ),
            patch(
                "scripts.limited_live.evaluate_personal_risk",
                return_value=self._eligible_gate("personal"),
            ),
        )

    def test_plan_binds_diagnostic_and_three_operational_gates(self) -> None:
        patches = self._gate_patches()
        with patches[0], patches[1], patches[2]:
            result = evaluate_limited_live_plan(root=self.root, plan_path=self.plan_path)

        self.assertTrue(result["eligible"])
        self.assertEqual(result["metrics"]["capital_limit_jpy"], 3_000_000)
        self.assertEqual(
            result["metrics"]["retrospective_maximum_drawdown_pct"], -27.38132
        )
        self.assertEqual(result["metrics"]["recorded_opening_cash_jpy"], 3_000_000)

    def test_plan_requires_one_funding_event_equal_to_capital_limit(self) -> None:
        rows = list(
            csv.reader(self.capital_path.read_text(encoding="utf-8").splitlines())
        )
        rows[-1][3] = "2990000"
        rows[-1][4] = "2990000"
        with self.capital_path.open("w", encoding="utf-8", newline="") as destination:
            csv.writer(destination).writerows(rows)
        patches = self._gate_patches()
        with patches[0], patches[1], patches[2]:
            result = evaluate_limited_live_plan(root=self.root, plan_path=self.plan_path)

        self.assertFalse(result["eligible"])
        self.assertIn(
            "limited live LIMITED_LIVE_FUNDING must equal capital_limit_jpy",
            result["blockers"],
        )

    def test_apply_is_atomic_and_plan_hash_is_revalidated(self) -> None:
        patches = self._gate_patches()
        with patches[0], patches[1], patches[2]:
            policy = apply_limited_live(root=self.root, plan_path=self.plan_path)
            self.assertEqual(
                validate_applied_limited_live(root=self.root, policy=policy), []
            )
        self.assertEqual(policy["operation_mode"], "LIMITED_LIVE")
        self.assertEqual(policy["limited_live"]["capital_limit_jpy"], 3_000_000)

        self.plan_path.write_text(
            self.plan_path.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )
        self.assertEqual(
            validate_applied_limited_live(root=self.root, policy=policy),
            ["limited_live evidence hash does not match"],
        )

    def test_order_guard_enforces_cash_cap_no_add_and_loss_stop(self) -> None:
        patches = self._gate_patches()
        with patches[0], patches[1], patches[2]:
            policy = apply_limited_live(root=self.root, plan_path=self.plan_path)
            allowed = validate_limited_live_order(
                root=self.root,
                policy=policy,
                run_id="2026-09-06",
                action="BUY",
                limit_price=1_000,
                quantity=100,
                position_pct=5.0,
            )
            add_blocked = validate_limited_live_order(
                root=self.root,
                policy=policy,
                run_id="2026-09-06",
                action="ADD",
                limit_price=1_000,
                quantity=100,
                position_pct=2.5,
            )
            over_declared = validate_limited_live_order(
                root=self.root,
                policy=policy,
                run_id="2026-09-06",
                action="BUY",
                limit_price=2_000,
                quantity=100,
                position_pct=5.0,
            )
            with self.capital_path.open(
                "a", encoding="utf-8", newline=""
            ) as destination:
                csv.writer(destination).writerow(
                    [
                        "pilot-loss",
                        "2026-09-07T09:00:00+09:00",
                        "FEE",
                        "-300000",
                        "2700000",
                        "",
                        "",
                        "",
                        "loss stop test",
                    ]
                )
            stopped = validate_limited_live_order(
                root=self.root,
                policy=policy,
                run_id="2026-09-06",
                action="BUY",
                limit_price=1_000,
                quantity=100,
                position_pct=5.0,
            )

        self.assertEqual(allowed, [])
        self.assertIn("LIMITED_LIVE additional purchases are disabled", add_blocked)
        self.assertIn(
            "LIMITED_LIVE order notional exceeds its declared position percentage",
            over_declared,
        )
        self.assertIn("LIMITED_LIVE total loss stop has been reached", stopped)


if __name__ == "__main__":
    unittest.main()
