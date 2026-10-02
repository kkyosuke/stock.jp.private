from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.collect_daily_prices import (
    DailyBar,
    PriceCollectionError,
    SymbolResult,
    collect,
    parse_chart_payload,
    write_dataset,
)
from scripts.tenbagger_price_scan import Issue


def issue(code: str, name: str = "Example") -> Issue:
    return Issue(code=code, name=name, market="プライム（内国株式）", sector="輸送用機器")


class DailyPriceCollectionTest(unittest.TestCase):
    def test_parses_ohlcv_and_omits_zero_volume_placeholders(self) -> None:
        payload = {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {"exchangeTimezoneName": "Asia/Tokyo"},
                        "timestamp": [1787875200, 1788134400],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [100, 101],
                                    "high": [105, 106],
                                    "low": [99, 100],
                                    "close": [103, 104],
                                    "volume": [0, 1000],
                                }
                            ]
                        },
                    }
                ],
            }
        }

        bars = parse_chart_payload(payload)

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].day, date(2026, 8, 31))
        self.assertEqual(bars[0].open, 101)
        self.assertEqual(bars[0].close, 104)
        self.assertEqual(bars[0].volume, 1000)

    def test_writes_every_issue_and_calculates_change_from_prior_close(self) -> None:
        issues = [issue("1234"), issue("5678", "No Quote")]
        results = [
            SymbolResult(
                issue=issues[0],
                bars=(
                    DailyBar(date(2026, 8, 28), 98, 102, 97, 100, 500),
                    DailyBar(date(2026, 8, 31), 101, 111, 100, 110, 1000),
                ),
            ),
            SymbolResult(issue=issues[1], bars=()),
        ]
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            summary = write_dataset(
                output_dir=output,
                issues=issues,
                results=results,
                output_start=date(2026, 8, 31),
                through_date=date(2026, 8, 31),
            )
            content = (output / "2026/2026-08-31.csv").read_text(encoding="utf-8")
            manifest = json.loads((output / "latest.json").read_text(encoding="utf-8"))

        self.assertIn("日付,銘柄コード,銘柄名", content)
        self.assertIn("2026-08-31,1234,Example", content)
        self.assertIn(",110,10,10,1000,OK", content)
        self.assertIn("2026-08-31,5678,No Quote", content)
        self.assertIn(",NO_QUOTE", content)
        self.assertEqual(manifest["universe"]["count"], 2)
        self.assertEqual(manifest["latest_session"]["quote_count"], 1)
        self.assertIn("2026/2026-08-31.csv", summary["changed_files"])

    def test_does_not_write_when_fetch_coverage_is_too_low(self) -> None:
        issues = [issue("1234"), issue("5678")]

        def broken_requester(url: str, timeout: int) -> dict[str, object]:
            raise PriceCollectionError("offline")

        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "prices"
            with self.assertRaisesRegex(PriceCollectionError, "fetch coverage"):
                collect(
                    issues=issues,
                    output_dir=output,
                    through_date=date(2026, 8, 31),
                    lookback_days=7,
                    workers=2,
                    timeout=1,
                    max_attempts=1,
                    minimum_fetch_coverage=0.98,
                    requester=broken_requester,
                )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
