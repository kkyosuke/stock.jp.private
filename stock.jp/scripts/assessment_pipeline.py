"""Run formal MRS and company ten-bagger calculations during nightly operation."""

from __future__ import annotations

try:
    from scripts.market_data_storage import resolve_archive_root, MarketDataStorageError
except ModuleNotFoundError:
    from market_data_storage import resolve_archive_root, MarketDataStorageError

import csv
from datetime import date, datetime
import json
from pathlib import Path
import tempfile
from typing import Any
from zoneinfo import ZoneInfo

try:
    from scripts.investment_case import InvestmentCaseError, evaluate_investment_case
    from scripts.market_regime import derive_market_regime
    from scripts.market_regime_sources import (
        MarketRegimeSourceError,
        collect_market_regime_series,
    )
except ModuleNotFoundError:  # Direct execution from scripts/
    from investment_case import InvestmentCaseError, evaluate_investment_case
    from market_regime import derive_market_regime
    from market_regime_sources import (
        MarketRegimeSourceError,
        collect_market_regime_series,
    )


JST = ZoneInfo("Asia/Tokyo")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        json.dump(document, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(path)


def _active_targets(private: Path) -> list[dict[str, str]]:
    targets: dict[str, dict[str, str]] = {}
    for filename, holding in (
        ("portfolio-register.csv", True),
        ("watchlist.csv", False),
    ):
        path = private / filename
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", newline="") as source:
            for row in csv.DictReader(source):
                code = str(row.get("code", "")).strip()
                status = str(row.get("status", "")).strip().upper()
                if not code or status in {
                    "CLOSED",
                    "SOLD",
                    "EXITED",
                    "INACTIVE",
                    "REJECTED",
                }:
                    continue
                if not holding and str(row.get("active", "")).strip().lower() not in {
                    "1",
                    "true",
                    "yes",
                    "active",
                }:
                    continue
                targets.setdefault(
                    code, {"code": code, "company": str(row.get("company", "")).strip()}
                )
    return list(targets.values())


def _mrs_target_as_of(root: Path, run_date: date) -> date:
    candidates: list[date] = []
    for path in resolve_archive_root(root).glob("*/*.csv"):
        try:
            candidate = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if candidate < run_date.replace(day=1):
            candidates.append(candidate)
    if not candidates:
        raise ValueError("no archived trading session exists before the current month")
    return max(candidates)


def _existing_mrs(log_path: Path, as_of: str) -> dict[str, Any] | None:
    if not log_path.is_file():
        return None
    with log_path.open(encoding="utf-8", newline="") as source:
        matches = [
            row
            for row in csv.DictReader(source)
            if row.get("as_of_jst") == as_of and row.get("rule_version") == "MRS-v0.1"
        ]
    if not matches:
        return None
    row = matches[-1]
    return {
        "status": "CURRENT",
        "as_of": as_of,
        "state": row["state"],
        "score": int(row["score"]),
        "entry_multiplier": float(row["entry_multiplier"]),
        "log_recorded": True,
    }


def _append_mrs_log(
    log_path: Path,
    *,
    values: Any,
    result: Any,
    source_urls: list[str],
    calculated_at: str,
) -> None:
    fields = [
        "as_of_jst",
        "rule_version",
        "topix_close",
        "topix_ma200",
        "m1",
        "growth250_close",
        "growth250_ma200",
        "m2",
        "breadth_pct",
        "m3",
        "nikkei_vi",
        "nikkei_vi_p80_3y",
        "m4",
        "leading_ci",
        "leading_ci_3m_ago",
        "m5",
        "score",
        "state",
        "entry_multiplier",
        "source_urls",
        "calculated_at_jst",
    ]
    if log_path.is_file():
        with log_path.open(encoding="utf-8", newline="") as source:
            actual = next(csv.reader(source), [])
        if actual != fields:
            raise ValueError("market-regime-log.csv schema mismatch")
    else:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8", newline="") as destination:
            csv.writer(destination, lineterminator="\n").writerow(fields)
    if _existing_mrs(log_path, values.as_of):
        return
    components = result.components
    row = [
        values.as_of,
        result.rule_version,
        values.topix_close,
        values.topix_ma200,
        components["M1"],
        values.growth_close,
        values.growth_ma200,
        components["M2"],
        values.breadth_pct,
        components["M3"],
        values.nikkei_vi,
        values.nikkei_vi_p80_3y,
        components["M4"],
        values.leading_ci,
        values.leading_ci_3m_ago,
        components["M5"],
        result.score,
        result.state,
        result.entry_multiplier,
        "|".join(source_urls),
        calculated_at,
    ]
    with log_path.open("a", encoding="utf-8", newline="") as destination:
        csv.writer(destination, lineterminator="\n").writerow(row)


def _is_stale(
    case: dict[str, Any], sources_path: Path, code: str
) -> tuple[bool, list[str]]:
    if not sources_path.is_file() or not case.get("as_of_jst"):
        return False, []
    case_at = datetime.fromisoformat(str(case["as_of_jst"]).replace("Z", "+00:00"))
    disclosure_words = (
        "annual",
        "quarter",
        "通期",
        "四半期",
        "決算",
        "新株",
        "希薄",
        "warrant",
        "capital",
    )
    newer: list[str] = []
    with sources_path.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            if str(row.get("code", "")).strip() != code:
                continue
            if not any(
                word in str(row.get("title", "")).lower() for word in disclosure_words
            ):
                continue
            try:
                published = datetime.fromisoformat(
                    str(row.get("published_at_jst", "")).replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if published > case_at:
                newer.append(str(row.get("source_id", "")).strip())
    return bool(newer), sorted(set(value for value in newer if value))


def run_assessments(
    *,
    run_id: str,
    at: str,
    fixture_dir: Path | None,
    root: Path,
) -> dict[str, Any]:
    """Write the audit artifact used by the nightly review and order guard."""

    private = root / "operations/private"
    run_dir = private / "runs" / run_id
    input_root = private / "research-inputs"
    (input_root / "market-regime").mkdir(parents=True, exist_ok=True)
    (input_root / "investment-cases").mkdir(parents=True, exist_ok=True)
    calculated_at = (
        datetime.fromisoformat(at.replace("Z", "+00:00"))
        .astimezone(JST)
        .isoformat(timespec="seconds")
    )
    market_status: dict[str, Any]
    try:
        target = _mrs_target_as_of(root, date.fromisoformat(run_id))
        existing = _existing_mrs(private / "market-regime-log.csv", target.isoformat())
        if existing:
            market_status = existing
        else:
            fixture = fixture_dir / "market-regime-series.json" if fixture_dir else None
            if fixture and fixture.is_file():
                raw = _read_json(fixture)
                raw["as_of"] = target.isoformat()
            elif fixture_dir:
                raise MarketRegimeSourceError(
                    "market-regime-series.json fixture is absent"
                )
            else:
                raw = collect_market_regime_series(
                    as_of=target,
                    config=_read_json(private / "source-config.json"),
                )
            raw_path = input_root / "market-regime" / f"{target.isoformat()}.json"
            _atomic_json(raw_path, raw)
            values, result, derived = derive_market_regime(
                raw, archive_root=resolve_archive_root(root)
            )
            urls = [
                str(series.get("source_url", ""))
                for series in raw["series"].values()
                if isinstance(series, dict) and series.get("source_url")
            ]
            urls = list(dict.fromkeys(urls))
            _append_mrs_log(
                private / "market-regime-log.csv",
                values=values,
                result=result,
                source_urls=urls,
                calculated_at=calculated_at,
            )
            market_status = {
                "status": "CURRENT",
                "as_of": target.isoformat(),
                "state": result.state,
                "score": result.score,
                "entry_multiplier": result.entry_multiplier,
                "components": result.components,
                "derived": derived,
                "input_path": raw_path.relative_to(root).as_posix(),
                "log_recorded": True,
            }
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        MarketRegimeSourceError,
    ) as error:
        market_status = {
            "status": "UNAVAILABLE",
            "state": "UNAVAILABLE",
            "entry_multiplier": 0.0,
            "error": str(error),
            "log_recorded": False,
        }

    company_results: list[dict[str, Any]] = []
    result_root = run_dir / "investment-cases"
    for target in _active_targets(private):
        code = target["code"]
        input_path = input_root / "investment-cases" / f"{code}.json"
        if not input_path.is_file():
            company_results.append(
                {
                    **target,
                    "status": "INPUT_REQUIRED",
                    "entry_ready": False,
                    "missing_fields": [input_path.relative_to(root).as_posix()],
                }
            )
            continue
        try:
            case = _read_json(input_path)
            if str(case.get("code", "")).strip() != code:
                raise InvestmentCaseError(
                    f"input code {case.get('code')!r} does not match target {code}"
                )
            result = evaluate_investment_case(case)
            stale, newer = _is_stale(case, run_dir / "sources.csv", code)
            if stale:
                result["status"] = "STALE"
                result["entry_ready"] = False
                result["newer_source_ids"] = newer
            output_path = result_root / f"{code}.json"
            _atomic_json(output_path, result)
            company_results.append(
                {
                    "code": code,
                    "company": target["company"] or result.get("company", ""),
                    "status": result["status"],
                    "entry_ready": bool(result.get("entry_ready")),
                    "missing_fields": result.get("missing_fields", []),
                    "failures": result.get("failures", []),
                    "result_path": output_path.relative_to(root).as_posix(),
                }
            )
        except (OSError, json.JSONDecodeError, InvestmentCaseError) as error:
            company_results.append(
                {**target, "status": "ERROR", "entry_ready": False, "error": str(error)}
            )

    market_allows_entry = market_status.get("state") in {"NORMAL", "CAUTION"}
    for result in company_results:
        result["entry_ready"] = bool(result["entry_ready"] and market_allows_entry)
    document = {
        "schema_version": "1.0",
        "run_id": run_id,
        "calculated_at_jst": calculated_at,
        "market_regime": market_status,
        "companies": company_results,
        "entry_ready_codes": [
            result["code"] for result in company_results if result["entry_ready"]
        ],
        "broker_submission": "HUMAN_ONLY",
    }
    path = run_dir / "assessment-status.json"
    _atomic_json(path, document)
    assessment_blocker_count = int(market_status["status"] != "CURRENT") + sum(
        result.get("status") in {"INPUT_REQUIRED", "INCOMPLETE", "STALE", "ERROR"}
        for result in company_results
    )
    return {
        "assessment_status": path.relative_to(root).as_posix(),
        "market_regime_status": market_status["status"],
        "market_regime_state": market_status["state"],
        "assessment_target_count": len(company_results),
        "assessment_blocker_count": assessment_blocker_count,
        "entry_ready_codes": document["entry_ready_codes"],
    }
