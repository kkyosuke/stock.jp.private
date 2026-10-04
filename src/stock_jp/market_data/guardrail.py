#!/usr/bin/env python3
"""Read-only validation of an already stored public price archive; never collect."""
from __future__ import annotations
from stock_jp.workspace import project_root

import argparse
from datetime import date, datetime
import json
from zoneinfo import ZoneInfo
from pathlib import Path

from stock_jp.market_data.validate_prices import validate_archive
from stock_jp.automation.validate_price_update import validate_summary


ROOT = project_root()


def validate_stored_archive(archive: Path, *, check_freshness: bool = True) -> None:
    manifest = json.loads((archive / "latest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported manifest schema_version")
    universe = manifest.get("universe", {})
    if (universe.get("provider") != "JPX TSE-listed issues monthly spreadsheet"
            or universe.get("scope") != "current domestic stocks"):
        raise ValueError("unexpected universe identity")
    actual = validate_archive(archive, source_label="stored public Yahoo archive")
    latest = actual["sessions"][-1]
    if manifest.get("latest_trading_date") != latest["date"]:
        raise ValueError("latest.json does not identify the newest stored session")
    recorded = manifest.get("latest_session", {})
    for key in ("date", "file", "sha256", "quote_count", "no_quote_count", "fetch_error_count"):
        if recorded.get(key) != latest[key]:
            raise ValueError(f"latest.json {key} does not match stored CSV")
    source = manifest.get("source", {})
    if source.get("provider") != "Yahoo Finance" or source.get("official") is not False:
        raise ValueError("manifest must identify Yahoo as an unofficial secondary source")
    latest_date = date.fromisoformat(latest["date"])
    if latest_date > datetime.now(ZoneInfo("Asia/Tokyo")).date():
        raise ValueError("latest_trading_date cannot be in the future")
    validate_summary(
        {**manifest, "changed_files": []},
        lookback_days=7,
        reference_date=None if check_freshness else date.fromisoformat(latest["date"]),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ROOT / "data/daily-prices")
    parser.add_argument("--check-freshness", choices=("true", "false"), default="true")
    args = parser.parse_args()
    validate_stored_archive(args.archive, check_freshness=args.check_freshness == "true")
    print("stored public price archive guardrail passed; no collection or state mutation")


if __name__ == "__main__":
    main()
