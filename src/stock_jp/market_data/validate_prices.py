#!/usr/bin/env python3
"""Validate a PR #14 compatible daily-price archive and emit coverage metadata."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo


CSV_FIELDS = (
    "日付",
    "銘柄コード",
    "銘柄名",
    "市場・商品区分",
    "33業種区分",
    "始値",
    "高値",
    "安値",
    "終値",
    "前日比",
    "前日比％",
    "売買高(株)",
    "取得状態",
)
VALID_STATES = {"OK", "NO_QUOTE", "FETCH_ERROR"}
CODE_PATTERN = re.compile(r"^[0-9A-Z]{4,5}$")
PRICE_FIELDS = ("始値", "高値", "安値", "終値")
EMPTY_WHEN_MISSING = (*PRICE_FIELDS, "前日比", "前日比％", "売買高(株)")
JST = ZoneInfo("Asia/Tokyo")


class ArchiveValidationError(RuntimeError):
    """Raised when an archive does not satisfy the frozen CSV contract."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--source-label", default="private historical source")
    return parser.parse_args()


def _number(value: str, *, label: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise ArchiveValidationError(f"{label} is not numeric: {value!r}") from error
    if not math.isfinite(result):
        raise ArchiveValidationError(f"{label} must be finite")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_row(row: dict[str, str], expected_day: str, row_number: int) -> str:
    location = f"{expected_day}.csv:{row_number}"
    if row["日付"] != expected_day:
        raise ArchiveValidationError(f"{location}: 日付 does not match filename")
    code = row["銘柄コード"].strip().upper()
    if not CODE_PATTERN.fullmatch(code):
        raise ArchiveValidationError(f"{location}: invalid 銘柄コード {code!r}")
    state = row["取得状態"]
    if state not in VALID_STATES:
        raise ArchiveValidationError(f"{location}: invalid 取得状態 {state!r}")

    if state == "OK":
        values = {
            field: _number(row[field], label=f"{location} {field}")
            for field in PRICE_FIELDS
        }
        if values["始値"] <= 0 or values["終値"] <= 0:
            raise ArchiveValidationError(f"{location}: 始値 and 終値 must be positive")
        if values["高値"] < max(values["始値"], values["安値"], values["終値"]):
            raise ArchiveValidationError(f"{location}: 高値 violates OHLC ordering")
        if values["安値"] > min(values["始値"], values["高値"], values["終値"]):
            raise ArchiveValidationError(f"{location}: 安値 violates OHLC ordering")
        volume = _number(row["売買高(株)"], label=f"{location} 売買高(株)")
        if volume < 0 or not volume.is_integer():
            raise ArchiveValidationError(f"{location}: 売買高(株) must be a non-negative integer")
        for field in ("前日比", "前日比％"):
            if row[field]:
                _number(row[field], label=f"{location} {field}")
    elif any(row[field] for field in EMPTY_WHEN_MISSING):
        raise ArchiveValidationError(
            f"{location}: price fields must be empty when 取得状態={state}"
        )
    return code


def validate_archive(archive: Path, *, source_label: str) -> dict[str, Any]:
    files = sorted(archive.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9][0-9][0-9]-*.csv"))
    if not files:
        raise ArchiveValidationError(f"no daily CSV files found under {archive}")

    sessions: list[dict[str, Any]] = []
    for path in files:
        expected_day = path.stem
        parsed_day = date.fromisoformat(expected_day)
        expected_relative = Path(str(parsed_day.year)) / f"{expected_day}.csv"
        if path.relative_to(archive) != expected_relative:
            raise ArchiveValidationError(f"unexpected archive path: {path}")

        counts = {state: 0 for state in sorted(VALID_STATES)}
        codes: set[str] = set()
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != CSV_FIELDS:
                raise ArchiveValidationError(f"{path}: CSV header does not match PR #14")
            for row_number, row in enumerate(reader, start=2):
                code = _validate_row(row, expected_day, row_number)
                if code in codes:
                    raise ArchiveValidationError(f"{path}:{row_number}: duplicate code {code}")
                codes.add(code)
                counts[row["取得状態"]] += 1
        if not codes:
            raise ArchiveValidationError(f"{path}: no rows")
        sessions.append(
            {
                "date": expected_day,
                "file": expected_relative.as_posix(),
                "sha256": _sha256(path),
                "row_count": len(codes),
                "quote_count": counts["OK"],
                "no_quote_count": counts["NO_QUOTE"],
                "fetch_error_count": counts["FETCH_ERROR"],
            }
        )

    return {
        "schema_version": 1,
        "archive_schema": "daily-prices-pr14-v1",
        "source_label": source_label,
        "generated_at_jst": datetime.now(JST).isoformat(timespec="seconds"),
        "first_trading_date": sessions[0]["date"],
        "latest_trading_date": sessions[-1]["date"],
        "session_count": len(sessions),
        "total_rows": sum(item["row_count"] for item in sessions),
        "total_quotes": sum(item["quote_count"] for item in sessions),
        "total_no_quotes": sum(item["no_quote_count"] for item in sessions),
        "total_fetch_errors": sum(item["fetch_error_count"] for item in sessions),
        "sessions": sessions,
    }


def main() -> int:
    args = parse_args()
    try:
        manifest = validate_archive(args.archive.resolve(), source_label=args.source_label)
    except (ArchiveValidationError, OSError, ValueError) as error:
        print(f"ERROR: {error}")
        return 1
    output = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
