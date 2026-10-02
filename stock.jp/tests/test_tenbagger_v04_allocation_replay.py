from datetime import date
import unittest

from scripts.tenbagger_v04_allocation_replay import (
    Bar,
    Candidate,
    build_fills,
    value_portfolio,
)


class TenbaggerV04AllocationReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions = [
            date(2024, 12, 30),
            date(2025, 1, 6),
            date(2025, 3, 31),
            date(2025, 4, 1),
            date(2025, 6, 30),
            date(2025, 7, 1),
            date(2025, 12, 30),
        ]
        self.candidates = [
            Candidate(
                rank=index + 1,
                code=f"{1000 + index}",
                name=f"Example {index}",
                sector=f"Sector {index // 2}",
                market="スタンダード（内国株式）",
                selection_day=date(2024, 12, 30),
                selection_close=100,
                momentum=0.5,
                average_turnover_20d=1_000_000_000,
                observations=140,
            )
            for index in range(12)
        ]
        self.bars = {
            candidate.code: {
                day: Bar(day, 100, 100, 100, 100, 1_000_000)
                for day in self.sessions
            }
            for candidate in self.candidates
        }
        self.schedule = (
            ("INITIAL", date(2025, 1, 6)),
            ("ADD_1", date(2025, 4, 1)),
            ("ADD_2", date(2025, 7, 1)),
        )

    def test_frozen_caps_reach_20_and_100_percent(self) -> None:
        results = {}
        for version in ("v0.2", "v0.4"):
            fills, skipped = build_fills(
                rule_version=version,
                candidates=self.candidates,
                sessions=self.sessions,
                bars=self.bars,
                initial_capital=10_000_000,
                scheduled_tranches=self.schedule,
                fee_rate=0,
            )
            run = value_portfolio(
                rule_version=version,
                candidates=self.candidates,
                sessions=self.sessions[1:],
                bars=self.bars,
                initial_capital=10_000_000,
                fills=fills,
                skipped=skipped,
            )
            results[version] = run

        self.assertEqual(results["v0.2"].summary["acquisition_cost_pct"], 20.0)
        self.assertEqual(results["v0.4"].summary["acquisition_cost_pct"], 100.0)
        self.assertEqual(results["v0.2"].summary["final_nav"], 10_000_000)
        self.assertEqual(results["v0.4"].summary["final_nav"], 10_000_000)

    def test_v04_never_exceeds_name_industry_or_pool_caps(self) -> None:
        fills, _ = build_fills(
            rule_version="v0.4",
            candidates=self.candidates,
            sessions=self.sessions,
            bars=self.bars,
            initial_capital=10_000_000,
            scheduled_tranches=self.schedule,
            fee_rate=0,
        )
        by_name = {}
        by_sector = {}
        for fill in fills:
            by_name[fill.code] = by_name.get(fill.code, 0) + fill.gross_cost
            by_sector[fill.sector] = by_sector.get(fill.sector, 0) + fill.gross_cost

        self.assertLessEqual(sum(fill.gross_cost for fill in fills), 10_000_000)
        self.assertTrue(all(value <= 1_000_000 for value in by_name.values()))
        self.assertTrue(all(value <= 2_000_000 for value in by_sector.values()))


if __name__ == "__main__":
    unittest.main()
