import unittest
import csv
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.market_regime import (
    MarketRegimeInput,
    calculate_breadth,
    derive_market_regime,
    evaluate_market_regime,
)


def inputs(**overrides) -> MarketRegimeInput:
    values = {
        "as_of": "2026-08-31",
        "topix_close": 101,
        "topix_ma200": 100,
        "growth_close": 101,
        "growth_ma200": 100,
        "breadth_pct": 50,
        "nikkei_vi": 20,
        "nikkei_vi_p80_3y": 20,
        "leading_ci": 101,
        "leading_ci_3m_ago": 100,
    }
    values.update(overrides)
    return MarketRegimeInput(**values)


class MarketRegimeTest(unittest.TestCase):
    def test_five_positive_components_are_normal(self) -> None:
        result = evaluate_market_regime(inputs())

        self.assertEqual(result.components, {f"M{n}": 1 for n in range(1, 6)})
        self.assertEqual(result.score, 5)
        self.assertEqual(result.state, "NORMAL")
        self.assertEqual(result.entry_multiplier, 1.0)

    def test_three_positive_components_are_caution(self) -> None:
        result = evaluate_market_regime(inputs(growth_close=99, leading_ci=99))

        self.assertEqual(result.score, 3)
        self.assertEqual(result.state, "CAUTION")
        self.assertEqual(result.entry_multiplier, 0.5)

    def test_one_positive_component_is_stress(self) -> None:
        result = evaluate_market_regime(
            inputs(
                topix_close=99,
                growth_close=99,
                breadth_pct=49.9,
                nikkei_vi=21,
            )
        )

        self.assertEqual(result.score, 1)
        self.assertEqual(result.state, "STRESS")
        self.assertEqual(result.entry_multiplier, 0.0)

    def test_invalid_breadth_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "breadth_pct"):
            evaluate_market_regime(inputs(breadth_pct=100.1))

    def test_invalid_as_of_date_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_market_regime(inputs(as_of="2026-02-30"))

    def test_breadth_excludes_new_listings_without_200_sessions(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            start = date(2025, 1, 1)
            for offset in range(200):
                day = start + timedelta(days=offset)
                path = root / str(day.year) / f"{day.isoformat()}.csv"
                path.parent.mkdir(exist_ok=True)
                with path.open("w", encoding="utf-8", newline="") as destination:
                    writer = csv.DictWriter(
                        destination, fieldnames=["銘柄コード", "終値"]
                    )
                    writer.writeheader()
                    writer.writerow({"銘柄コード": "UP", "終値": offset + 1})
                    writer.writerow({"銘柄コード": "DOWN", "終値": 200 - offset})
                    if offset:
                        writer.writerow({"銘柄コード": "NEW", "終値": 100})

            result = calculate_breadth(
                root,
                as_of=start + timedelta(days=199),
                minimum_coverage=0.98,
            )

            self.assertEqual(result.eligible_code_count, 2)
            self.assertEqual(result.universe_code_count, 2)
            self.assertEqual(result.latest_code_count, 3)
            self.assertEqual(result.above_ma200_count, 1)
            self.assertEqual(result.breadth_pct, 50)
            self.assertEqual(result.excluded_code_count, 0)
            self.assertEqual(result.insufficient_history_code_count, 1)
            self.assertEqual(result.data_coverage_ratio, 1)
            self.assertEqual(result.archive_session_count, 200)

    def test_breadth_uses_latest_200_valid_closes_from_lookback(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            start = date(2025, 1, 1)
            for offset in range(201):
                day = start + timedelta(days=offset)
                path = root / str(day.year) / f"{day.isoformat()}.csv"
                path.parent.mkdir(exist_ok=True)
                with path.open("w", encoding="utf-8", newline="") as destination:
                    writer = csv.DictWriter(
                        destination, fieldnames=["銘柄コード", "終値"]
                    )
                    writer.writeheader()
                    writer.writerow(
                        {
                            "銘柄コード": "ONE-GAP",
                            "終値": "" if offset == 100 else offset + 1,
                        }
                    )

            result = calculate_breadth(
                root,
                as_of=start + timedelta(days=200),
                lookback_sessions=201,
            )

            self.assertEqual(result.eligible_code_count, 1)
            self.assertEqual(result.data_coverage_ratio, 1)
            self.assertEqual(result.breadth_pct, 100)

    def test_breadth_rejects_missing_history_for_long_listed_codes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            start = date(2025, 1, 1)
            for offset in range(200):
                day = start + timedelta(days=offset)
                path = root / str(day.year) / f"{day.isoformat()}.csv"
                path.parent.mkdir(exist_ok=True)
                with path.open("w", encoding="utf-8", newline="") as destination:
                    writer = csv.DictWriter(
                        destination, fieldnames=["銘柄コード", "終値"]
                    )
                    writer.writeheader()
                    writer.writerow({"銘柄コード": "A", "終値": 100})
                    writer.writerow({"銘柄コード": "B", "終値": 100})
                    writer.writerow(
                        {
                            "銘柄コード": "BROKEN",
                            "終値": "" if offset == 100 else 100,
                        }
                    )

            with self.assertRaisesRegex(ValueError, "breadth data coverage"):
                calculate_breadth(root, as_of=start + timedelta(days=199))

    def test_raw_series_derives_all_five_components(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            start = date(2024, 1, 1)
            market_points = []
            for offset in range(600):
                day = start + timedelta(days=offset)
                market_points.append({"date": day.isoformat(), "value": 100 + offset})
                if offset >= 400:
                    path = root / str(day.year) / f"{day.isoformat()}.csv"
                    path.parent.mkdir(exist_ok=True)
                    with path.open("w", encoding="utf-8", newline="") as destination:
                        writer = csv.DictWriter(
                            destination, fieldnames=["銘柄コード", "終値"]
                        )
                        writer.writeheader()
                        writer.writerow({"銘柄コード": "UP", "終値": offset})
            as_of = start + timedelta(days=599)
            document = {
                "schema_version": "1.0",
                "as_of": as_of.isoformat(),
                "minimum_vi_observations": 500,
                "series": {
                    "topix": {"points": market_points},
                    "growth250": {"points": market_points},
                    "nikkei_vi": {
                        "points": [
                            {
                                "date": (start + timedelta(days=offset)).isoformat(),
                                "value": 20,
                            }
                            for offset in range(600)
                        ]
                    },
                    "leading_ci": {
                        "available_at_jst": "2025-08-01T08:50:00+09:00",
                        "points": [
                            {"period": "2025-04", "value": 99},
                            {"period": "2025-07", "value": 100},
                        ],
                    },
                },
            }

            values, result, derived = derive_market_regime(document, archive_root=root)

            self.assertEqual(result.state, "NORMAL")
            self.assertEqual(result.score, 5)
            self.assertEqual(values.breadth_pct, 100)
            self.assertEqual(derived["topix"]["observation_count"], 200)


if __name__ == "__main__":
    unittest.main()
