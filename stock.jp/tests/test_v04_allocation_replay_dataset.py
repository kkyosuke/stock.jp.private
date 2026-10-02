import csv
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
PREFIX = ROOT / "data/tenbagger-v0.4-allocation-replay-2025"
REPORT = ROOT / "docs/research/tenbagger-v0.4-allocation-replay-2025.md"


class V04AllocationReplayDatasetTest(unittest.TestCase):
    def test_summary_is_a_diagnostic_and_not_a_forward_paper_gate(self) -> None:
        summary = json.loads(
            Path(f"{PREFIX}-summary.json").read_text(encoding="utf-8")
        )

        self.assertEqual(summary["status"], "ALLOCATION_DIAGNOSTIC_ONLY")
        self.assertEqual(summary["session_count"], 243)
        self.assertEqual(summary["candidate_count"], 12)
        self.assertFalse(summary["full_strategy_backtest"])
        self.assertFalse(summary["forward_paper_gate_satisfied"])

    def test_v04_deploys_more_and_has_larger_observed_drawdown(self) -> None:
        summary = json.loads(
            Path(f"{PREFIX}-summary.json").read_text(encoding="utf-8")
        )
        v02 = summary["results"]["v0.2"]
        v04 = summary["results"]["v0.4"]

        self.assertGreater(v04["acquisition_cost_pct"], 95)
        self.assertLess(v02["acquisition_cost_pct"], 20)
        self.assertLess(v04["maximum_drawdown_pct"], v02["maximum_drawdown_pct"])

    def test_candidate_trade_and_daily_row_counts_match_summary(self) -> None:
        with Path(f"{PREFIX}-candidates.csv").open(
            encoding="utf-8", newline=""
        ) as source:
            candidates = list(csv.DictReader(source))
        with Path(f"{PREFIX}-trades.csv").open(
            encoding="utf-8", newline=""
        ) as source:
            trades = list(csv.DictReader(source))
        with Path(f"{PREFIX}-daily.csv").open(
            encoding="utf-8", newline=""
        ) as source:
            daily = list(csv.DictReader(source))

        self.assertEqual(len(candidates), 12)
        self.assertEqual(len(trades), 52)
        self.assertEqual(len(daily), 243)
        self.assertEqual(daily[0]["date"], "2025-01-06")
        self.assertEqual(daily[-1]["date"], "2025-12-30")

    def test_report_links_resolve(self) -> None:
        text = REPORT.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^]]*\]\(([^)]+)\)", text):
            if "://" in target or target.startswith("#"):
                continue
            self.assertTrue((REPORT.parent / target).resolve().exists(), target)


if __name__ == "__main__":
    unittest.main()
