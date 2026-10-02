"""Export private J-Quants V2 rows to the existing daily-price CSV contract.

Raw API pages remain the ingestion layer's responsibility. This adapter never
fetches data, substitutes adjusted prices, filters against today's master, or
relaxes the independent PAPER/LIVE readiness gates.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

try:
    from scripts.collect_daily_prices import CSV_FIELDS
    from scripts.market_data_storage import resolve_archive_root
    from scripts.price_snapshot import normalize_code
except ModuleNotFoundError:
    from collect_daily_prices import CSV_FIELDS
    from market_data_storage import resolve_archive_root
    from price_snapshot import normalize_code


SOURCE = {
    "provider": "J-Quants API V2",
    "official": True,
    "api_version": "v2",
    "price_basis": "unadjusted",
    "volume_unit": "shares",
}
UNIVERSE_PROVIDER = "J-Quants API V2 equities/master"
UNIVERSE_SCOPE = "point-in-time domestic stocks"


class JQuantsSnapshotError(ValueError):
    """A snapshot cannot truthfully satisfy the private price contract."""


def _number(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("missing or invalid number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("invalid number") from error
    if not number.is_finite():
        raise ValueError("number is not finite")
    return number


def _text_number(value: Decimal) -> str:
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else str(value)


def _bar_fields(bar: dict[str, Any] | None) -> tuple[dict[str, str], str]:
    fields = {name: "" for name in ("始値", "高値", "安値", "終値", "売買高(株)")}
    if bar is None or all(bar.get(key) is None for key in ("O", "H", "L", "C", "Vo")):
        return fields, "NO_QUOTE"
    try:
        op, hi, lo, cl, volume = (_number(bar.get(key)) for key in ("O", "H", "L", "C", "Vo"))
        if min(op, hi, lo, cl) <= 0 or hi < max(op, lo, cl) or lo > min(op, hi, cl):
            raise ValueError("invalid OHLC")
        if volume < 0 or volume != volume.to_integral_value():
            raise ValueError("invalid share volume")
    except (ValueError, TypeError):
        return fields, "FETCH_ERROR"
    return dict(zip(fields, (_text_number(value) for value in (op, hi, lo, cl, volume)))), "OK"


def _safe_path(archive: Path, relative: Path) -> Path:
    path = (archive / relative).resolve()
    if not path.is_relative_to(archive) or path == archive:
        raise JQuantsSnapshotError("snapshot path escapes the private archive")
    return path


def _atomic_bytes(archive: Path, relative: Path, content: bytes, *, immutable: bool = False) -> None:
    path = _safe_path(archive, relative)
    if immutable and path.exists():
        if path.read_bytes() != content:
            raise JQuantsSnapshotError("immutable snapshot revision was modified")
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for parent in (path.parent, *path.parent.parents):
        if not parent.is_relative_to(archive):
            break
        parent.chmod(0o700)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def export_daily_snapshot(
    root: Path,
    day: str,
    master_rows: list[dict[str, Any]],
    bar_rows: list[dict[str, Any]],
    retrieved_at: str,
    *,
    raw_provenance: Any = None,
) -> dict[str, Any]:
    """Export one complete date query and return its manifest.

    ``master_rows`` must be the full paginated master for ``day``, and
    ``bar_rows`` the full paginated daily-bars query for that same date.
    Missing quotes remain in the universe and reduce readiness coverage.
    Canonical year/date CSVs serve legacy readers; immutable revision paths
    retain prior content and anchor checksum-verified readiness evidence.
    A historical backfill never replaces a newer J-Quants latest manifest.
    """
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise JQuantsSnapshotError("day must be YYYY-MM-DD")
    timestamp = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise JQuantsSnapshotError("retrieved_at must include a UTC offset")
    archive = resolve_archive_root(root)
    config = json.loads((root / "operations/private/source-config.json").read_text(encoding="utf-8"))
    if config.get("price_source", {}).get("provider") != "jquants_v2_private_archive":
        raise JQuantsSnapshotError("J-Quants snapshot requires its explicit private price provider")
    if not isinstance(master_rows, list) or not isinstance(bar_rows, list):
        raise JQuantsSnapshotError("master and bar rows must be lists")

    issues: dict[str, dict[str, Any]] = {}
    master_codes: set[str] = set()
    for row in master_rows:
        if not isinstance(row, dict) or row.get("Date") != day:
            raise JQuantsSnapshotError("master must be a point-in-time snapshot for the price date")
        raw_code = str(row.get("Code") or "").strip().upper()
        if not re.fullmatch(r"[0-9A-Z]{4,5}", raw_code):
            raise JQuantsSnapshotError("invalid master code")
        code = normalize_code(raw_code)
        if code in master_codes:
            raise JQuantsSnapshotError(f"duplicate master code {code}")
        master_codes.add(code)
        if not row.get("ProdCat"):
            raise JQuantsSnapshotError("master ProdCat is required to identify domestic stocks")
        # Product category, not a current-market whitelist, excludes foreign
        # shares/ETFs/REITs while retaining delisted and pre-2022 common stocks.
        # https://jpx-jquants.com/en/spec/eq-master/product-category
        # https://jpx-jquants.com/en/help/data (common share reserve code is 0)
        if str(row["ProdCat"]) != "011" or (len(raw_code) == 5 and not raw_code.endswith("0")):
            continue
        issues[code] = row
    if not issues:
        raise JQuantsSnapshotError("point-in-time domestic stock universe is empty")

    bars: dict[str, dict[str, Any]] = {}
    for row in bar_rows:
        if not isinstance(row, dict) or row.get("Date") != day:
            raise JQuantsSnapshotError("bar date must match the price date")
        raw_code = str(row.get("Code") or "").strip().upper()
        if not re.fullmatch(r"[0-9A-Z]{4,5}", raw_code):
            raise JQuantsSnapshotError("invalid bar code")
        code = normalize_code(raw_code)
        if code in bars:
            raise JQuantsSnapshotError(f"duplicate bar code {code}")
        if code not in master_codes:
            raise JQuantsSnapshotError(f"bar code {code} is missing from the dated master")
        bars[code] = row

    rows: list[dict[str, str]] = []
    counts = {"OK": 0, "NO_QUOTE": 0, "FETCH_ERROR": 0}
    error_codes: list[str] = []
    for code, issue in sorted(issues.items()):
        fields, status = _bar_fields(bars.get(code))
        counts[status] += 1
        if status == "FETCH_ERROR":
            error_codes.append(code)
        rows.append({
            "日付": day,
            "銘柄コード": code,
            "銘柄名": str(issue.get("CoName") or ""),
            "市場・商品区分": str(issue.get("MktNm") or ""),
            "33業種区分": str(issue.get("S33Nm") or ""),
            **fields,
            # Neither a previous-day close nor a split basis is supplied here.
            "前日比": "",
            "前日比％": "",
            "取得状態": status,
        })
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    content = buffer.getvalue().encode("utf-8")
    checksum = hashlib.sha256(content).hexdigest()
    revision = Path("revisions") / day / checksum / f"{day}.csv"
    canonical = Path(day[:4]) / f"{day}.csv"
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "source": dict(SOURCE),
        "universe": {
            "provider": UNIVERSE_PROVIDER,
            "scope": UNIVERSE_SCOPE,
            "count": len(issues),
            "master_date": day,
            "product_category": "011",
            "common_share_reserve_code": "0",
        },
        "retrieved_at": retrieved_at,
        "latest_trading_date": day,
        "fetch": {
            "success_count": len(issues) - counts["FETCH_ERROR"],
            "error_count": counts["FETCH_ERROR"],
            "error_codes": error_codes,
        },
        "latest_session": {
            "date": day,
            "file": revision.as_posix(),
            "canonical_file": canonical.as_posix(),
            "sha256": checksum,
            "quote_count": counts["OK"],
            "no_quote_count": counts["NO_QUOTE"],
            "fetch_error_count": counts["FETCH_ERROR"],
        },
    }
    if raw_provenance is not None:
        manifest["raw_provenance"] = raw_provenance
    metadata_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    metadata_sha = hashlib.sha256(metadata_bytes).hexdigest()

    # Preserve even a pre-migration canonical CSV before replacing it.
    previous_path = _safe_path(archive, canonical)
    if previous_path.is_file():
        previous = previous_path.read_bytes()
        previous_sha = hashlib.sha256(previous).hexdigest()
        _atomic_bytes(archive, Path("revisions") / day / previous_sha / f"{day}.csv", previous, immutable=True)
    _atomic_bytes(archive, revision, content, immutable=True)
    _atomic_bytes(archive, revision.parent / f"manifest-{metadata_sha}.json", metadata_bytes, immutable=True)
    _atomic_bytes(archive, canonical, content)
    latest_path = _safe_path(archive, Path("latest.json"))
    previous_latest: dict[str, Any] = {}
    if latest_path.is_file():
        try:
            loaded = json.loads(latest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous_latest = loaded
        except (ValueError, OSError):
            pass
    previous_source = previous_latest.get("source", {})
    try:
        previous_day = date.fromisoformat(str(previous_latest.get("latest_trading_date", "")))
    except ValueError:
        previous_day = date.min
    is_newer_jquants = (
        isinstance(previous_source, dict)
        and all(previous_source.get(key) == value for key, value in SOURCE.items())
        and previous_day > date.fromisoformat(day)
    )
    if not is_newer_jquants:
        _atomic_bytes(archive, Path("latest.json"), metadata_bytes)
    return manifest
