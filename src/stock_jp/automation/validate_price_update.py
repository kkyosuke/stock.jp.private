#!/usr/bin/env python3
"""Fail closed before an unattended daily-price pull request can be merged."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import json
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo


class ScheduledUpdateError(RuntimeError):
    """Raised when a scheduled update requires human review."""


JST = ZoneInfo("Asia/Tokyo")


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ScheduledUpdateError("summary root must be an object")
    return value


def validate_summary(
    summary: dict[str, Any],
    *,
    lookback_days: int,
    reference_date: date | None = None,
) -> None:
    if lookback_days < 1:
        raise ScheduledUpdateError("lookback_days must be >= 1")
    universe = summary.get("universe")
    fetch = summary.get("fetch")
    latest = summary.get("latest_session")
    if not all(isinstance(value, dict) for value in (universe, fetch, latest)):
        raise ScheduledUpdateError("summary is missing universe, fetch, or latest_session")

    universe_count = universe.get("count")
    success_count = fetch.get("success_count")
    error_count = fetch.get("error_count")
    if type(universe_count) is not int or universe_count <= 0:
        raise ScheduledUpdateError("universe count must be a positive integer")
    if type(success_count) is not int or type(error_count) is not int:
        raise ScheduledUpdateError("fetch counts must be integers")
    if success_count != universe_count or error_count != 0:
        raise ScheduledUpdateError("unattended merge requires zero fetch errors")

    quote_count = latest.get("quote_count")
    no_quote_count = latest.get("no_quote_count")
    latest_fetch_errors = latest.get("fetch_error_count")
    if not all(type(value) is int for value in (quote_count, no_quote_count)):
        raise ScheduledUpdateError("latest quote counts must be integers")
    if quote_count < 0 or no_quote_count < 0:
        raise ScheduledUpdateError("latest quote counts must be non-negative")
    if type(latest_fetch_errors) is not int or latest_fetch_errors != 0:
        raise ScheduledUpdateError("latest session contains fetch errors")
    if quote_count + no_quote_count != universe_count:
        raise ScheduledUpdateError("latest session count does not match the universe")
    if quote_count / universe_count < 0.98:
        raise ScheduledUpdateError(
            "latest session quote coverage is below 98% "
            f"({quote_count}/{universe_count}); retry collection after prices are available"
        )

    try:
        latest_date = date.fromisoformat(str(summary["latest_trading_date"]))
    except (KeyError, ValueError) as error:
        raise ScheduledUpdateError("latest_trading_date is invalid") from error
    reference_date = reference_date or datetime.now(JST).date()
    age_days = (reference_date - latest_date).days
    if age_days < 0:
        raise ScheduledUpdateError("latest_trading_date cannot be in the future")
    if age_days > lookback_days:
        raise ScheduledUpdateError("latest_trading_date is stale")
    earliest_allowed = latest_date - timedelta(days=lookback_days - 1)

    changed_files = summary.get("changed_files")
    if not isinstance(changed_files, list) or not all(
        isinstance(value, str) for value in changed_files
    ):
        raise ScheduledUpdateError("changed_files must be a list of paths")
    csv_changed = False
    for value in changed_files:
        path = PurePosixPath(value)
        if path == PurePosixPath("latest.json"):
            continue
        if len(path.parts) != 2 or path.suffix != ".csv":
            raise ScheduledUpdateError(f"unexpected generated path: {value}")
        csv_changed = True
        try:
            session_date = date.fromisoformat(path.stem)
        except ValueError as error:
            raise ScheduledUpdateError(f"invalid generated session path: {value}") from error
        if path.parts[0] != str(session_date.year):
            raise ScheduledUpdateError(f"session year directory mismatch: {value}")
        if not earliest_allowed <= session_date <= latest_date:
            raise ScheduledUpdateError(
                f"generated session is outside the lookback window: {value}"
            )
    if csv_changed and "latest.json" not in changed_files:
        raise ScheduledUpdateError("CSV changes require a matching latest.json update")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--lookback-days", type=int, required=True)
    args = parser.parse_args()
    try:
        validate_summary(_read_object(args.summary), lookback_days=args.lookback_days)
    except (OSError, json.JSONDecodeError, ScheduledUpdateError) as error:
        print(f"ERROR: {error}")
        return 1
    print("scheduled daily-price update is eligible for unattended merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
