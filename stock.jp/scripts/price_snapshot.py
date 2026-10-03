"""Validate an explicitly identified private daily archive for PAPER readiness."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from scripts.market_data_storage import resolve_archive_root, MarketDataStorageError
except ModuleNotFoundError:
    from market_data_storage import resolve_archive_root, MarketDataStorageError


REQUIRED_FIELDS = {
    "日付",
    "銘柄コード",
    "始値",
    "高値",
    "安値",
    "終値",
    "売買高(株)",
    "取得状態",
}


def normalize_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    if re.fullmatch(r"[0-9A-Z]{4}0", code):
        return code[:4]
    return code


def active_target_codes(private: Path) -> set[str]:
    result: set[str] = set()
    for filename, requires_active_flag in (
        ("portfolio-register.csv", False),
        ("watchlist.csv", True),
    ):
        with (private / filename).open(encoding="utf-8", newline="") as source:
            for row in csv.DictReader(source):
                status = str(row.get("status", "")).strip().upper()
                if status in {"CLOSED", "SOLD", "EXITED", "INACTIVE", "REJECTED"}:
                    continue
                if requires_active_flag and str(row.get("active", "")).strip().upper() not in {
                    "TRUE",
                    "1",
                    "YES",
                    "ACTIVE",
                }:
                    continue
                code = normalize_code(row.get("code"))
                if code:
                    result.add(code)
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite_ratio(value: Any) -> float:
    ratio = float(value)
    if not math.isfinite(ratio) or not 0 <= ratio <= 1:
        raise ValueError("ratio must be finite and between zero and one")
    return ratio


def validate_tracked_price_snapshot(
    *,
    root: Path,
    active_targets: set[str],
    cutoff: datetime,
    config: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return checksum-verified archive evidence or fail-closed reasons."""

    price = config.get("price_source", {})
    if not isinstance(price, dict):
        return None, ["private price source configuration is invalid"]
    provider = price.get("provider", "jquants_v2_private_archive")
    is_jquants = provider == "jquants_v2_private_archive"
    label = "J-Quants" if is_jquants else "tracked Yahoo"
    if not price.get("enabled"):
        return None, [f"{label} price archive is disabled"]
    if provider not in {
        "jquants_v2_private_archive", "yahoo_finance_unofficial_tracked_archive"
    }:
        return None, ["private price source identity is invalid"]
    try:
        archive_root = resolve_archive_root(root, config)
    except MarketDataStorageError as error:
        return None, [str(error)]
    if price.get("manifest_path") != "latest.json":
        return None, ["private manifest_path must be latest.json"]
    manifest = (archive_root / "latest.json").resolve()
    if manifest.parent != archive_root:
        return None, ["private manifest path escapes the daily-price archive"]
    if not manifest.is_file():
        return None, [f"{label} latest.json is missing"]
    try:
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        if metadata.get("schema_version") != 1:
            raise ValueError("unexpected manifest schema")
        source = metadata["source"]
        universe = metadata["universe"]
        if not isinstance(source, dict) or not isinstance(universe, dict):
            raise ValueError("invalid source or universe")
        if is_jquants:
            if (
                source.get("provider") != "J-Quants API V2"
                or source.get("official") is not True
                or source.get("api_version") != "v2"
                or source.get("price_basis") != "unadjusted"
                or source.get("volume_unit") != "shares"
            ):
                raise ValueError("unexpected J-Quants source identity or units")
            if (
                universe.get("provider") != "J-Quants API V2 equities/master"
                or universe.get("scope") != "point-in-time domestic common stocks excluding PRO MARKET"
                or universe.get("excluded_market_codes") != ["0105"]
            ):
                raise ValueError("unexpected J-Quants universe identity")
        else:
            if source.get("provider") != "Yahoo Finance" or source.get("official") is not False:
                raise ValueError("unexpected source identity")
            if (
                universe.get("provider") != "JPX TSE-listed issues monthly spreadsheet"
                or universe.get("scope") != "current domestic stocks"
            ):
                raise ValueError("unexpected universe identity")
        price_date = datetime.fromisoformat(str(metadata["latest_trading_date"])).date()
        session = metadata["latest_session"]
        if datetime.fromisoformat(str(session["date"])).date() != price_date:
            raise ValueError("session date mismatch")
        universe_count = int(universe["count"])
        quote_count = int(session["quote_count"])
        required_archive = _finite_ratio(
            price.get("minimum_daily_archive_coverage", 0.98)
        )
        required_targets = _finite_ratio(
            price.get("minimum_active_target_coverage", 1.0)
        )
        maximum_age = int(price.get("maximum_latest_price_age_days", 7))
        if universe_count < 1 or quote_count < 0 or quote_count > universe_count:
            raise ValueError("invalid universe counts")
        if maximum_age < 0:
            raise ValueError("maximum age must be non-negative")
    except (OSError, AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None, [f"{label} latest.json has invalid readiness evidence"]

    failures: list[str] = []
    archive_coverage = quote_count / universe_count
    if archive_coverage < required_archive:
        failures.append(f"{label} full-universe coverage is below the daily minimum")
    if price_date > cutoff.date():
        failures.append(f"{label} price date is in the future")
    elif (cutoff.date() - price_date).days > maximum_age:
        failures.append(f"{label} price date is stale")

    data_file = (archive_root / str(session.get("file", ""))).resolve()
    if not data_file.is_relative_to(archive_root):
        failures.append(f"{label} session path escapes the daily-price archive")
    elif not data_file.is_file():
        failures.append(f"{label} latest session CSV is missing")
    elif data_file.stem != price_date.isoformat():
        failures.append(f"{label} session filename does not match the price date")
    else:
        expected_sha = str(session.get("sha256") or "")
        if not expected_sha or _sha256(data_file) != expected_sha:
            failures.append(f"{label} session checksum does not match")

    target_failures: list[str] = []
    target_rows: dict[str, dict[str, str]] = {}
    if not failures and data_file.is_file():
        try:
            with data_file.open(encoding="utf-8-sig", newline="") as source_file:
                reader = csv.DictReader(source_file)
                if not REQUIRED_FIELDS.issubset(set(reader.fieldnames or [])):
                    raise ValueError("daily-price CSV schema is incomplete")
                seen: set[str] = set()
                ok_count = 0
                row_count = 0
                for row in reader:
                    code = normalize_code(row.get("銘柄コード"))
                    if not code:
                        raise ValueError("blank code")
                    if code in seen:
                        raise ValueError(f"duplicate code {code}")
                    seen.add(code)
                    row_count += 1
                    if row.get("日付") != price_date.isoformat():
                        raise ValueError("row date mismatch")
                    if row.get("取得状態") == "OK":
                        ok_count += 1
                    if code in active_targets:
                        target_rows[code] = row
                if row_count != universe_count or ok_count != quote_count:
                    raise ValueError("manifest counts do not match CSV")
        except (OSError, ValueError):
            failures.append(f"{label} latest session CSV is invalid")

    if not failures:
        for code in sorted(active_targets):
            row = target_rows.get(code)
            if row is None or row.get("取得状態") != "OK":
                target_failures.append(code)
                continue
            try:
                open_price, high, low, close = (
                    float(row[field]) for field in ("始値", "高値", "安値", "終値")
                )
                prices = [open_price, high, low, close]
                volume = float(row["売買高(株)"])
                if not all(math.isfinite(value) and value > 0 for value in prices):
                    raise ValueError
                if high < max(open_price, low, close) or low > min(
                    open_price, high, close
                ):
                    raise ValueError
                if not math.isfinite(volume) or volume < 0 or not volume.is_integer():
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                target_failures.append(code)
        target_coverage = (
            (len(active_targets) - len(target_failures)) / len(active_targets)
            if active_targets
            else 1.0
        )
        if target_coverage < required_targets:
            failures.append(f"{label} archive does not cover every active target")
    else:
        target_coverage = 0.0

    if failures:
        return None, sorted(set(failures))
    return {
        "status": "COMPLETED",
        "provider": provider,
        "manifest_path": (Path(price["archive_root"]) / "latest.json").as_posix(),
        "session_path": (Path(price["archive_root"]) / data_file.relative_to(archive_root)).as_posix(),
        "price_date": price_date.isoformat(),
        "session_sha256": str(session["sha256"]),
        "universe_count": universe_count,
        "quote_count": quote_count,
        "acquisition": metadata.get("acquisition") if is_jquants else None,
        "price_diagnostics": session.get("diagnostics") if is_jquants else None,
        "coverage_ratio": archive_coverage,
        "target_codes": sorted(active_targets),
        "target_count": len(active_targets),
        "target_coverage_ratio": target_coverage,
        "target_failures": target_failures,
        "decision_use": (
            "PAPER_OFFICIAL_PRICE_REQUIRES_CORPORATE_ACTION_CONFIRMATION"
            if is_jquants
            else "PAPER_SECONDARY_SOURCE_REQUIRES_OFFICIAL_CONFIRMATION"
        ),
    }, []
