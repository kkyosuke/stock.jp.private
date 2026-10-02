from __future__ import annotations

from copy import deepcopy
from datetime import date
import unittest

from scripts.validate_scheduled_price_update import (
    ScheduledUpdateError,
    validate_summary,
)


def _valid_summary() -> dict:
    return {
        "universe": {"count": 3713},
        "fetch": {"success_count": 3713, "error_count": 0},
        "latest_trading_date": "2026-09-01",
        "latest_session": {
            "quote_count": 3669,
            "no_quote_count": 44,
            "fetch_error_count": 0,
        },
        "changed_files": [
            "2026/2026-08-31.csv",
            "2026/2026-09-01.csv",
            "latest.json",
        ],
    }


class ScheduledPriceUpdateValidationTest(unittest.TestCase):
    def validate(self, summary: dict, *, lookback_days: int = 7) -> None:
        validate_summary(
            summary,
            lookback_days=lookback_days,
            reference_date=date(2026, 9, 2),
        )

    def test_zero_error_recent_update_is_eligible(self) -> None:
        self.validate(_valid_summary())

    def test_successful_fetches_with_almost_no_quotes_are_rejected(self) -> None:
        summary = _valid_summary()
        summary["universe"]["count"] = 3707
        summary["fetch"]["success_count"] = 3707
        summary["latest_session"].update(quote_count=4, no_quote_count=3703)
        with self.assertRaisesRegex(ScheduledUpdateError, "quote coverage is below 98%"):
            self.validate(summary)

    def test_quote_coverage_boundary(self) -> None:
        for quote_count, allowed in ((0, False), (979, False), (980, True), (1000, True)):
            with self.subTest(quote_count=quote_count):
                summary = _valid_summary()
                summary["universe"]["count"] = 1000
                summary["fetch"]["success_count"] = 1000
                summary["latest_session"].update(
                    quote_count=quote_count, no_quote_count=1000 - quote_count
                )
                if allowed:
                    self.validate(summary)
                else:
                    with self.assertRaisesRegex(ScheduledUpdateError, "quote coverage"):
                        self.validate(summary)

    def test_any_fetch_error_requires_human_review(self) -> None:
        summary = deepcopy(_valid_summary())
        summary["fetch"] = {"success_count": 3712, "error_count": 1}
        summary["latest_session"]["fetch_error_count"] = 1
        with self.assertRaisesRegex(ScheduledUpdateError, "zero fetch errors"):
            self.validate(summary)

    def test_old_session_rewrite_is_rejected(self) -> None:
        summary = deepcopy(_valid_summary())
        summary["changed_files"].append("2026/2026-08-01.csv")
        with self.assertRaisesRegex(ScheduledUpdateError, "outside the lookback"):
            self.validate(summary)

    def test_unexpected_generated_path_is_rejected(self) -> None:
        summary = deepcopy(_valid_summary())
        summary["changed_files"].append("docs/report.md")
        with self.assertRaisesRegex(ScheduledUpdateError, "unexpected generated path"):
            self.validate(summary)

    def test_latest_counts_must_cover_the_universe(self) -> None:
        summary = deepcopy(_valid_summary())
        summary["latest_session"]["no_quote_count"] = 43
        with self.assertRaisesRegex(ScheduledUpdateError, "does not match"):
            self.validate(summary)

    def test_csv_change_requires_the_manifest(self) -> None:
        summary = deepcopy(_valid_summary())
        summary["changed_files"].remove("latest.json")
        with self.assertRaisesRegex(ScheduledUpdateError, "latest.json"):
            self.validate(summary)

    def test_future_latest_date_is_rejected(self) -> None:
        summary = deepcopy(_valid_summary())
        summary["latest_trading_date"] = "2026-09-03"
        with self.assertRaisesRegex(ScheduledUpdateError, "future"):
            self.validate(summary)

    def test_stale_latest_date_is_rejected(self) -> None:
        summary = deepcopy(_valid_summary())
        summary["latest_trading_date"] = "2026-08-20"
        with self.assertRaisesRegex(ScheduledUpdateError, "stale"):
            self.validate(summary)

    def test_boolean_error_count_is_rejected(self) -> None:
        summary = deepcopy(_valid_summary())
        summary["fetch"]["error_count"] = False
        with self.assertRaisesRegex(ScheduledUpdateError, "integers"):
            self.validate(summary)


if __name__ == "__main__":
    unittest.main()
