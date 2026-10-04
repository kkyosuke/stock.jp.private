from datetime import date
import csv
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stock_jp.research.allocation import (
    Bar, Candidate, Fill, Split, _read_bars, build_fills, load_replay_bars,
    load_splits, value_portfolio,
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

    def test_split_preserves_value_and_does_not_multiply_same_day_purchase(self):
        before, after = date(2026, 4, 27), date(2026, 4, 28)
        candidate = self.candidates[0]
        bars = {candidate.code: {day: Bar(day, price, price, price, price, 1000)
                                 for day, price in ((before, 300), (after, 100))}}
        fills = [Fill("v0.4", "INITIAL", before, before, candidate.code,
                      candidate.name, candidate.sector, 300, 100, 30000, 0),
                 Fill("v0.4", "ADD_1", before, after, candidate.code,
                      candidate.name, candidate.sector, 100, 100, 10000, 0)]
        run = value_portfolio(rule_version="v0.4", candidates=[candidate],
                              sessions=[before, after], bars=bars, initial_capital=100000,
                              fills=fills, skipped=[],
                              splits=(Split(candidate.code, after, 3, 1),))
        self.assertEqual(run.summary["final_nav"], 100000)
        self.assertEqual(run.summary["final_market_value"], 40000)
        self.assertEqual(run.summary["acquisition_cost_pct"], 40)

    def test_fractional_reverse_split_needs_settlement_evidence(self):
        day = self.sessions[-1]
        candidate = self.candidates[0]
        fill = Fill("v0.4", "INITIAL", day, self.sessions[1], candidate.code,
                    candidate.name, candidate.sector, 100, 100, 10000, 0)
        with self.assertRaisesRegex(ValueError, "fractional split shares"):
            value_portfolio(rule_version="v0.4", candidates=[candidate],
                            sessions=self.sessions[1:], bars=self.bars, initial_capital=100000,
                            fills=[fill], skipped=[], splits=(Split(candidate.code, day, 1, 3),))

    def test_order_limit_uses_post_split_evaluation_price(self):
        before, after = date(2026, 4, 27), date(2026, 4, 28)
        candidate = Candidate(1, "1000", "Company", "情報・通信業", "スタンダード",
                              before, 300, 0.5, 100000000, 120)
        fills, skipped = build_fills(rule_version="v0.4", candidates=[candidate],
             sessions=[after], bars={candidate.code: {after: Bar(after, 130, 135, 126, 130, 100000)}},
             initial_capital=1000000, scheduled_tranches=(("INITIAL", after),),
             splits=(Split(candidate.code, after, 3, 1),))
        self.assertEqual(fills, [])
        self.assertEqual(skipped[0].reason, "LIMIT_NOT_FILLED")

    def test_fees_reduce_nav_and_cash_on_flat_prices(self):
        fills, skipped = build_fills(rule_version="v0.4", candidates=self.candidates,
                                     sessions=self.sessions, bars=self.bars,
                                     initial_capital=10000000, scheduled_tranches=self.schedule,
                                     fee_rate=0.005)
        run = value_portfolio(rule_version="v0.4", candidates=self.candidates,
                              sessions=self.sessions[1:], bars=self.bars, initial_capital=10000000,
                              fills=fills, skipped=skipped)
        self.assertEqual(run.summary["final_nav"], 10000000 - sum(fill.fee for fill in fills))
        self.assertGreaterEqual(run.summary["final_cash"], 0)


class AllocationInputTest(unittest.TestCase):
    def prices(self, path, day, price):
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = ["日付", "銘柄コード", "銘柄名", "市場・商品区分", "33業種区分",
                  "始値", "高値", "安値", "終値", "売買高(株)", "取得状態"]
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            for code, market in (("1000", "スタンダード"), ("1001", "スタンダード（内国株式）"),
                                 ("1002", "スタンダード（外国株式）")):
                writer.writerow([day, code, code, market, "情報・通信業", price, price,
                                 price, price, 1000000, "OK"])

    def test_native_market_names_are_domestic_and_foreign_shares_stay_excluded(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "2026/2026-04-27.csv"
            self.prices(path, "2026-04-27", 300)
            self.assertEqual([row[0] for row in _read_bars(path)], ["1000", "1001"])

    def test_split_jump_requires_verified_event_instead_of_dropping_candidate(self):
        before, after = date(2026, 4, 27), date(2026, 4, 28)
        candidate = Candidate(1, "1000", "Company", "情報・通信業", "スタンダード",
                              before, 300, 0.5, 100000000, 120)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.prices(root / "2026/2026-04-27.csv", str(before), 300)
            self.prices(root / "2026/2026-04-28.csv", str(after), 100)
            arguments = dict(archive=root, candidates=[candidate], start=before, end=after)
            with self.assertRaisesRegex(ValueError, "possible unadjusted corporate action"):
                load_replay_bars(**arguments)
            sessions, _ = load_replay_bars(**arguments, splits=(Split("1000", after, 3, 1),))
            self.assertEqual(sessions, [before, after])

    def test_split_evidence_hash_dates_and_path_are_validated(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "notice.pdf"
            source.write_bytes(b"official notice fixture")
            event = dict(code="1000", source_path="notice.pdf",
                         source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                         announced_date="2026-04-13", price_adjustment_date="2026-04-28",
                         legal_effective_date="2026-05-01", ratio_numerator=3, ratio_denominator=1)
            manifest = root / "splits.json"
            def save(item):
                manifest.write_text(json.dumps(dict(schema_version="1.0", splits=[item])))
            with patch("stock_jp.research.allocation.project_root", return_value=root):
                save(event)
                self.assertEqual(load_splits(manifest), (Split("1000", date(2026, 4, 28), 3, 1),))
                for changes, error in ((dict(source_sha256="0" * 64), "hash mismatch"),
                                       (dict(announced_date="2026-04-28"), "dates are inconsistent"),
                                       (dict(source_path="../notice.pdf"), "project file")):
                    save({**event, **changes})
                    with self.assertRaisesRegex(ValueError, error):
                        load_splits(manifest)


if __name__ == "__main__":
    unittest.main()
