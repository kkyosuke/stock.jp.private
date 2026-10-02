#!/usr/bin/env python3
"""Validate private point-in-time inputs and replay v0.2 versus v0.4.

The replay never downloads market data.  It accepts only a hash-bound private
manifest whose datasets are explicitly marked official and authorised for this
replay.  J-Quants and unofficial price providers are rejected.  Missing rows,
future-published assessment inputs and incomplete universe coverage stop the
run instead of being replaced by optimistic defaults.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import tempfile
from typing import Any, Iterable
from zoneinfo import ZoneInfo

try:
    from scripts.position_sizing import allocation_caps
except ModuleNotFoundError:  # Direct execution: python scripts/<file>.py
    from position_sizing import allocation_caps


PROJECT_ROOT = Path(__file__).resolve().parents[1]
JST = ZoneInfo("Asia/Tokyo")
REQUIRED_DATASETS = {
    "source_register",
    "security_master",
    "trading_calendar",
    "daily_prices",
    "corporate_actions",
    "review_events",
    "assessments",
    "market_regime",
    "benchmark",
}
PROHIBITED_PROVIDER_TOKENS = ("j-quants", "jquants", "yahoo")
IMMEDIATE_EXIT_RULES = {f"S-A{index}" for index in range(1, 7)}
QUARTERLY_EXIT_RULES = {f"S-B{index}" for index in range(1, 6)}
PERMANENT_REBUY_BLOCK_RULES = {"S-A1", "S-A2", "S-A4"}
BOARD_LOT_DEFAULT = 100


class ReplayInputError(ValueError):
    """One or more replay inputs violate the frozen point-in-time contract."""

    def __init__(self, blockers: Iterable[str]):
        self.blockers = list(dict.fromkeys(str(item) for item in blockers))
        super().__init__("; ".join(self.blockers))


@dataclass(frozen=True)
class Dataset:
    role: str
    path: Path
    relative_path: str
    sha256: str
    provider: str


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    turnover: float
    status: str


@dataclass(frozen=True)
class Assessment:
    evaluation_day: date
    decision_at: datetime
    review_type: str
    issue_id: str
    code: str
    sector: str
    status: str
    hard_gates_passed: bool
    score: float
    market_score: float
    reverse_score: float
    liquidity_passed: bool
    entry_ready: bool
    add_ready: bool
    required_revenue_cagr_pct: float
    dilution_outlook_pct: float
    other_score: float
    major_kpi_missed: bool
    exit_rule: str
    source_ids: tuple[str, ...]
    latest_source_published_at: datetime


@dataclass
class Position:
    issue_id: str
    code: str
    sector: str
    quantity: int = 0
    q0: float = 0.0
    acquisition_cost: float = 0.0
    average_price: float = 0.0
    initial_session_index: int | None = None
    add_count: int = 0
    add_banned: bool = False
    highest_close: float = 0.0
    highest_ma20: float | None = None
    five_x_done: bool = False
    ten_x_done: bool = False
    ten_x_day: date | None = None
    quarterly_rule: str = ""
    quarterly_streak: int = 0
    c6_checked: bool = False
    permanent_rebuy_block: bool = False
    rebuy_after_index: int = 0
    purchase_gross: float = 0.0
    sale_gross: float = 0.0
    fees: float = 0.0
    approved_required_revenue_cagr_pct: float | None = None
    approved_dilution_outlook_pct: float | None = None


@dataclass
class Order:
    issue_id: str
    side: str
    rule_id: str
    decision_day: date
    execute_index: int
    requested_quantity: int | None = None
    tranche: str = ""
    immediate: bool = False


@dataclass
class ReplayRun:
    rule_version: str
    trades: list[dict[str, Any]] = field(default_factory=list)
    daily: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        json.dump(value, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def _write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as temporary:
        writer = csv.DictWriter(temporary, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def _read_csv(path: Path) -> tuple[list[dict[str, str]], set[str]]:
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        return list(reader), set(reader.fieldnames or [])


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"line {line_number}: {error}") from error
        if not isinstance(row, dict):
            raise ValueError(f"line {line_number}: JSON value must be an object")
        rows.append(row)
    return rows


def _aware_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must include a UTC offset")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a UTC offset")
    return parsed.astimezone(JST)


def _finite(value: Any, field_name: str, *, positive: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be numeric") from error
    if not math.isfinite(parsed) or (positive and parsed <= 0):
        qualifier = "positive and " if positive else ""
        raise ValueError(f"{field_name} must be {qualifier}finite")
    return parsed


def _integer(value: Any, field_name: str, *, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be an integer") from error
    if str(parsed) != str(value).strip() or parsed < minimum:
        raise ValueError(f"{field_name} must be an integer >= {minimum}")
    return parsed


def _boolean(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError(f"{field_name} must be boolean")


def _private_root(root: Path) -> Path:
    path = (root / "operations/private").resolve()
    if not path.is_dir():
        raise ReplayInputError([f"private operation root is missing: {path}"])
    return path


def _private_relative(root: Path, path: Path) -> str:
    private = _private_root(root)
    return (Path("operations/private") / path.resolve().relative_to(private)).as_posix()


def _provider_is_prohibited(provider: str) -> bool:
    lowered = provider.lower()
    compact = "".join(character for character in lowered if character.isalnum())
    return "jquants" in compact or "yahoo" in compact or any(
        token in lowered for token in PROHIBITED_PROVIDER_TOKENS
    )


def _dataset_contract(
    *, root: Path, manifest: dict[str, Any]
) -> tuple[dict[str, Dataset], list[str]]:
    blockers: list[str] = []
    private = _private_root(root)
    documents = manifest.get("datasets")
    if not isinstance(documents, list):
        return {}, ["datasets must be an array"]
    result: dict[str, Dataset] = {}
    for index, item in enumerate(documents):
        prefix = f"datasets[{index}]"
        if not isinstance(item, dict):
            blockers.append(f"{prefix} must be an object")
            continue
        role = str(item.get("role", "")).strip()
        relative = str(item.get("path", "")).strip()
        provider = str(item.get("provider", "")).strip()
        if not role or role in result:
            blockers.append(f"{prefix}.role is missing or duplicated")
            continue
        path = (private / relative).resolve()
        try:
            path.relative_to(private)
        except ValueError:
            blockers.append(f"{prefix}.path must stay under operations/private")
            continue
        if not path.is_file():
            blockers.append(f"dataset is missing: {relative}")
            continue
        expected_hash = str(item.get("sha256", ""))
        actual_hash = _sha256(path)
        if expected_hash != actual_hash:
            blockers.append(f"dataset hash mismatch: {relative}")
        if item.get("official") is not True:
            blockers.append(f"{prefix}.official must be true")
        if item.get("replay_authorized") is not True:
            blockers.append(f"{prefix}.replay_authorized must be true")
        if not provider:
            blockers.append(f"{prefix}.provider is required")
        if _provider_is_prohibited(provider):
            blockers.append(f"prohibited replay provider: {provider}")
        result[role] = Dataset(role, path, relative, actual_hash, provider)
    for role in sorted(REQUIRED_DATASETS - set(result)):
        blockers.append(f"required dataset role is missing: {role}")
    return result, blockers


def _require_fields(role: str, actual: set[str], required: set[str]) -> list[str]:
    missing = sorted(required - actual)
    return [f"{role} field is missing: {field}" for field in missing]


def validate_inputs(
    *, root: Path, manifest_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the parsed replay model or raise with every detected blocker."""

    root = root.resolve()
    private = _private_root(root)
    manifest_path = manifest_path.resolve()
    try:
        manifest_path.relative_to(private)
    except ValueError as error:
        raise ReplayInputError(
            ["replay input manifest must stay under operations/private"]
        ) from error
    try:
        manifest = _read_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ReplayInputError([f"input manifest is invalid: {error}"]) from error

    blockers: list[str] = []
    if manifest.get("schema_version") != "1.0":
        blockers.append("input manifest schema_version must be 1.0")
    if manifest.get("status") != "READY":
        blockers.append("input manifest status must be READY")
    try:
        generated_at = _aware_datetime(
            manifest.get("generated_at_jst"), "generated_at_jst"
        )
    except (TypeError, ValueError) as error:
        blockers.append(str(error))
        generated_at = datetime.min.replace(tzinfo=JST)
    reference_time = datetime.now(tz=JST)
    if generated_at > reference_time + timedelta(minutes=5):
        blockers.append("manifest generated_at_jst cannot be in the future")
    period = manifest.get("period")
    if not isinstance(period, dict):
        blockers.append("period must be an object")
        period = {}
    try:
        period_from = date.fromisoformat(str(period.get("from", "")))
        period_through = date.fromisoformat(str(period.get("through", "")))
        if period_from > period_through:
            blockers.append("period.from cannot be after period.through")
    except ValueError:
        blockers.append("period.from and period.through must be ISO dates")
        period_from = period_through = date.min
    kind = manifest.get("evaluation_kind")
    if kind not in {"RETROSPECTIVE_STRESS_TEST", "FORWARD_HOLDOUT"}:
        blockers.append("evaluation_kind must be RETROSPECTIVE_STRESS_TEST or FORWARD_HOLDOUT")
    certifications = manifest.get("certifications")
    if not isinstance(certifications, dict):
        certifications = {}
        blockers.append("certifications must be an object")
    for field_name in (
        "point_in_time_security_master",
        "includes_delisted",
        "includes_mergers",
        "includes_corporate_actions",
        "includes_all_material_disclosures",
        "source_rights_reviewed",
        "jquants_excluded",
        "unofficial_prices_excluded",
    ):
        if certifications.get(field_name) is not True:
            blockers.append(f"certifications.{field_name} must be true")
    if manifest.get("price_basis") != "AS_TRADED_UNADJUSTED":
        blockers.append("price_basis must be AS_TRADED_UNADJUSTED")
    try:
        initial_capital = _finite(
            manifest.get("initial_capital"), "initial_capital", positive=True
        )
        fee_rate = _finite(manifest.get("fee_rate"), "fee_rate")
        slippage_rate = _finite(manifest.get("slippage_rate"), "slippage_rate")
        board_lot = _integer(
            manifest.get("board_lot", BOARD_LOT_DEFAULT), "board_lot", minimum=1
        )
        if not 0 <= fee_rate <= 0.05 or not 0 <= slippage_rate <= 0.05:
            blockers.append("fee_rate and slippage_rate must be between 0 and 0.05")
    except (TypeError, ValueError) as error:
        blockers.append(str(error))
        initial_capital = 0.0
        fee_rate = slippage_rate = 0.0
        board_lot = BOARD_LOT_DEFAULT

    holdout_plan: dict[str, Any] | None = None
    if kind == "FORWARD_HOLDOUT":
        holdout = manifest.get("holdout")
        if not isinstance(holdout, dict):
            blockers.append("forward holdout metadata is required")
            holdout = {}
        if holdout.get("retuning_count") != 0:
            blockers.append("forward holdout retuning_count must be 0")
        relative_plan = str(holdout.get("plan_path", "")).strip()
        plan_path = (private / relative_plan).resolve()
        try:
            plan_path.relative_to(private)
        except ValueError:
            blockers.append("holdout plan_path must stay under operations/private")
        else:
            if not plan_path.is_file():
                blockers.append(f"holdout plan is missing: {relative_plan}")
            elif holdout.get("plan_sha256") != _sha256(plan_path):
                blockers.append("holdout plan hash does not match")
            else:
                try:
                    holdout_plan = _read_json(plan_path)
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    blockers.append(f"holdout plan is invalid: {error}")
        if holdout_plan:
            if holdout_plan.get("schema_version") != "1.0":
                blockers.append("holdout plan schema_version must be 1.0")
            if holdout_plan.get("status") != "FROZEN":
                blockers.append("holdout plan status must be FROZEN")
            if holdout_plan.get("decision") != "START_FORWARD_HOLDOUT":
                blockers.append("holdout plan decision must be START_FORWARD_HOLDOUT")
            if holdout_plan.get("rule_version") != "v0.4":
                blockers.append("holdout plan rule_version must be v0.4")
            if not isinstance(holdout_plan.get("declared_by"), str) or not holdout_plan.get(
                "declared_by", ""
            ).strip():
                blockers.append("holdout plan declared_by is required")
            if holdout_plan.get("period") != period:
                blockers.append("holdout plan period does not match the input manifest")
            try:
                frozen_at = _aware_datetime(
                    holdout_plan.get("frozen_at_jst"), "holdout plan frozen_at_jst"
                )
                rule_frozen_at = _aware_datetime(
                    holdout_plan.get("rule_frozen_at_jst"),
                    "holdout plan rule_frozen_at_jst",
                )
                if rule_frozen_at.date() < date(2026, 9, 1):
                    blockers.append("v0.4 cannot be frozen before its 2026-09-01 effective date")
                if frozen_at < rule_frozen_at:
                    blockers.append("holdout plan cannot precede the v0.4 rule freeze")
                if frozen_at.date() >= period_from:
                    blockers.append("holdout plan must be frozen before period.from")
            except (TypeError, ValueError) as error:
                blockers.append(str(error))
            criteria = holdout_plan.get("acceptance_criteria")
            if not isinstance(criteria, dict):
                blockers.append("holdout plan acceptance_criteria must be an object")
            else:
                for field_name in (
                    "minimum_monthly_evaluation_count",
                    "minimum_trade_count",
                    "maximum_drawdown_floor_pct",
                    "maximum_single_name_loss_floor_pct",
                    "maximum_industry_loss_floor_pct",
                ):
                    if not isinstance(criteria.get(field_name), (int, float)) or isinstance(
                        criteria.get(field_name), bool
                    ):
                        blockers.append(f"holdout acceptance criterion is missing: {field_name}")
                for field_name in (
                    "minimum_monthly_evaluation_count",
                    "minimum_trade_count",
                ):
                    value = criteria.get(field_name)
                    if isinstance(value, (int, float)) and (
                        isinstance(value, bool) or int(value) != value or value < 1
                    ):
                        blockers.append(f"{field_name} must be a positive integer")
                for field_name in (
                    "maximum_drawdown_floor_pct",
                    "maximum_single_name_loss_floor_pct",
                    "maximum_industry_loss_floor_pct",
                ):
                    value = criteria.get(field_name)
                    if isinstance(value, (int, float)) and not -100 <= value <= 0:
                        blockers.append(f"{field_name} must be between -100 and 0")
            acknowledgements = holdout_plan.get("acknowledgements")
            if not isinstance(acknowledgements, dict):
                blockers.append("holdout plan acknowledgements must be an object")
            else:
                for field_name in (
                    "period_was_unobserved_when_frozen",
                    "inputs_and_execution_rules_frozen",
                    "thresholds_frozen_before_results",
                    "changes_restart_the_holdout",
                    "jquants_will_not_be_used",
                ):
                    if acknowledgements.get(field_name) is not True:
                        blockers.append(f"holdout plan acknowledgement must be true: {field_name}")

    datasets, dataset_blockers = _dataset_contract(root=root, manifest=manifest)
    blockers.extend(dataset_blockers)
    if blockers and set(datasets) != REQUIRED_DATASETS:
        raise ReplayInputError(blockers)

    csv_rows: dict[str, list[dict[str, str]]] = {}
    field_contracts = {
        "source_register": {
            "source_id", "provider", "title", "published_at_jst", "url",
            "official", "replay_authorized", "content_sha256",
        },
        "security_master": {
            "issue_id", "code", "name", "sector", "market", "effective_from",
            "effective_through", "domestic_common_stock", "event_type", "source_id",
            "known_at_jst",
        },
        "trading_calendar": {
            "date", "is_trading_day", "source_id", "known_at_jst",
        },
        "daily_prices": {
            "date", "issue_id", "open", "high", "low", "close", "volume",
            "turnover", "status", "source_id", "available_at_jst",
        },
        "corporate_actions": {
            "effective_date", "issue_id", "action_type", "ratio",
            "successor_issue_id", "cash_consideration", "fractional_cash_price",
            "source_id", "announced_at_jst",
        },
        "review_events": {
            "event_id", "issue_id", "event_date", "review_due_date",
            "review_type", "source_id", "available_at_jst",
        },
        "market_regime": {
            "evaluation_date", "state", "entry_multiplier", "source_ids",
            "available_at_jst",
        },
        "benchmark": {"date", "close", "source_id", "available_at_jst"},
    }
    for role, required_fields in field_contracts.items():
        try:
            rows, fields = _read_csv(datasets[role].path)
        except (OSError, csv.Error) as error:
            blockers.append(f"{role} is invalid: {error}")
            continue
        blockers.extend(_require_fields(role, fields, required_fields))
        csv_rows[role] = rows
    try:
        assessment_documents = _read_jsonl(datasets["assessments"].path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        blockers.append(f"assessments is invalid: {error}")
        assessment_documents = []

    sources: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(csv_rows.get("source_register", []), 2):
        prefix = f"source_register:{index}"
        source_id = row.get("source_id", "").strip()
        if not source_id or source_id in sources:
            blockers.append(f"{prefix} source_id is missing or duplicated")
            continue
        try:
            published = _aware_datetime(row.get("published_at_jst"), f"{prefix}.published_at_jst")
            official = _boolean(row.get("official"), f"{prefix}.official")
            authorized = _boolean(row.get("replay_authorized"), f"{prefix}.replay_authorized")
        except (TypeError, ValueError) as error:
            blockers.append(str(error))
            continue
        provider = row.get("provider", "").strip()
        if _provider_is_prohibited(provider):
            blockers.append(f"{prefix} uses prohibited provider: {provider}")
        if not provider or not row.get("title", "").strip() or not row.get("url", "").strip():
            blockers.append(f"{prefix} provider, title and url are required")
        if not official or not authorized:
            blockers.append(f"{prefix} must be official and replay_authorized")
        digest = row.get("content_sha256", "").strip()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            blockers.append(f"{prefix}.content_sha256 must be lowercase SHA-256")
        sources[source_id] = {**row, "published": published}

    masters: list[dict[str, Any]] = []
    known_events: set[str] = set()
    for index, row in enumerate(csv_rows.get("security_master", []), 2):
        prefix = f"security_master:{index}"
        try:
            effective_from = date.fromisoformat(row.get("effective_from", ""))
            effective_through = (
                date.fromisoformat(row["effective_through"])
                if row.get("effective_through", "").strip()
                else None
            )
            known_at = _aware_datetime(row.get("known_at_jst"), f"{prefix}.known_at_jst")
            domestic = _boolean(row.get("domestic_common_stock"), f"{prefix}.domestic_common_stock")
        except (TypeError, ValueError) as error:
            blockers.append(str(error))
            continue
        if effective_through and effective_through < effective_from:
            blockers.append(f"{prefix} effective range is reversed")
        if known_at.date() > effective_from:
            blockers.append(f"{prefix} was not known by effective_from")
        source_id = row.get("source_id", "").strip()
        source_record = sources.get(source_id)
        if source_record is None:
            blockers.append(f"{prefix} source_id is unknown: {source_id}")
        elif source_record["published"] > known_at:
            blockers.append(f"{prefix} source was published after known_at_jst")
        issue_id = row.get("issue_id", "").strip()
        if not issue_id or not row.get("code", "").strip():
            blockers.append(f"{prefix} issue_id and code are required")
        event_type = row.get("event_type", "").strip().upper()
        if event_type not in {"LISTED", "CONTINUING", "DELISTED", "MERGED", "CODE_CHANGE"}:
            blockers.append(f"{prefix} event_type is invalid")
        known_events.add(event_type)
        masters.append(
            {
                **row,
                "effective_from_date": effective_from,
                "effective_through_date": effective_through,
                "known_at": known_at,
                "domestic": domestic,
            }
        )
    masters_by_issue: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in masters:
        masters_by_issue[record["issue_id"]].append(record)
    for issue_id, records in masters_by_issue.items():
        ordered = sorted(records, key=lambda item: item["effective_from_date"])
        for previous, current in zip(ordered, ordered[1:]):
            if current["effective_from_date"] <= (
                previous["effective_through_date"] or date.max
            ):
                blockers.append(f"security master effective ranges overlap: {issue_id}")

    calendar: dict[date, bool] = {}
    for index, row in enumerate(csv_rows.get("trading_calendar", []), 2):
        prefix = f"trading_calendar:{index}"
        try:
            day = date.fromisoformat(row.get("date", ""))
            is_trading_day = _boolean(
                row.get("is_trading_day"), f"{prefix}.is_trading_day"
            )
            known_at = _aware_datetime(row.get("known_at_jst"), f"{prefix}.known_at_jst")
        except (TypeError, ValueError) as error:
            blockers.append(str(error))
            continue
        if known_at.date() > day:
            blockers.append(f"{prefix} was not known by the calendar date")
        source_record = sources.get(row.get("source_id", "").strip())
        if source_record is None:
            blockers.append(f"{prefix} source_id is unknown")
        elif source_record["published"] > known_at:
            blockers.append(f"{prefix} source was published after known_at_jst")
        if day in calendar:
            blockers.append(f"duplicate trading calendar date: {day}")
        calendar[day] = is_trading_day
    expected_calendar_days = {
        period_from + timedelta(days=offset)
        for offset in range((period_through - period_from).days + 1)
    }
    missing_calendar_days = sorted(expected_calendar_days - set(calendar))
    extra_calendar_days = sorted(
        day for day in calendar if day < period_from or day > period_through
    )
    if missing_calendar_days:
        blockers.append(
            "trading calendar is incomplete: "
            + ", ".join(item.isoformat() for item in missing_calendar_days[:10])
        )
    if extra_calendar_days:
        blockers.append("trading calendar contains dates outside the replay period")
    sessions = sorted(
        day
        for day, is_trading_day in calendar.items()
        if is_trading_day and period_from <= day <= period_through
    )
    if not sessions:
        blockers.append("trading calendar does not define replay sessions")

    benchmark: dict[date, float] = {}
    for index, row in enumerate(csv_rows.get("benchmark", []), 2):
        prefix = f"benchmark:{index}"
        try:
            day = date.fromisoformat(row.get("date", ""))
            close = _finite(row.get("close"), f"{prefix}.close", positive=True)
            available = _aware_datetime(row.get("available_at_jst"), f"{prefix}.available_at_jst")
        except (TypeError, ValueError) as error:
            blockers.append(str(error))
            continue
        if available.date() != day:
            blockers.append(f"{prefix} must become available on its trading date")
        source_record = sources.get(row.get("source_id", "").strip())
        if source_record is None:
            blockers.append(f"{prefix} source_id is unknown")
        elif source_record["published"] > available:
            blockers.append(f"{prefix} source was published after available_at_jst")
        if available.astimezone(JST).time() < datetime.strptime("15:30", "%H:%M").time():
            blockers.append(f"{prefix} cannot expose a closing price before 15:30 JST")
        if day in benchmark:
            blockers.append(f"duplicate benchmark date: {day}")
        benchmark[day] = close
    benchmark_days = {day for day in benchmark if period_from <= day <= period_through}
    if benchmark_days != set(sessions):
        for day in sorted(set(sessions) - benchmark_days)[:10]:
            blockers.append(f"benchmark is missing for trading session: {day}")
        for day in sorted(benchmark_days - set(sessions))[:10]:
            blockers.append(f"benchmark exists on a non-trading day: {day}")

    bars: dict[str, dict[date, Bar]] = defaultdict(dict)
    for index, row in enumerate(csv_rows.get("daily_prices", []), 2):
        prefix = f"daily_prices:{index}"
        try:
            day = date.fromisoformat(row.get("date", ""))
            available = _aware_datetime(row.get("available_at_jst"), f"{prefix}.available_at_jst")
            status = row.get("status", "").strip().upper()
            if status not in {"OK", "NO_TRADE", "HALTED"}:
                raise ValueError(f"{prefix}.status is invalid")
            open_price = _finite(row.get("open"), f"{prefix}.open", positive=True)
            high = _finite(row.get("high"), f"{prefix}.high", positive=True)
            low = _finite(row.get("low"), f"{prefix}.low", positive=True)
            close = _finite(row.get("close"), f"{prefix}.close", positive=True)
            volume = _integer(row.get("volume"), f"{prefix}.volume")
            turnover = _finite(row.get("turnover"), f"{prefix}.turnover")
        except (TypeError, ValueError) as error:
            blockers.append(str(error))
            continue
        if not low <= min(open_price, close) <= max(open_price, close) <= high:
            blockers.append(f"{prefix} OHLC is inconsistent")
        if status == "OK" and (volume <= 0 or turnover <= 0):
            blockers.append(f"{prefix} OK quote requires positive volume and turnover")
        if available.date() != day:
            blockers.append(f"{prefix} must become available on its trading date")
        source_record = sources.get(row.get("source_id", "").strip())
        if source_record is None:
            blockers.append(f"{prefix} source_id is unknown")
        elif source_record["published"] > available:
            blockers.append(f"{prefix} source was published after available_at_jst")
        if available.astimezone(JST).time() < datetime.strptime("15:30", "%H:%M").time():
            blockers.append(f"{prefix} cannot expose OHLCV before 15:30 JST")
        issue_id = row.get("issue_id", "").strip()
        if day in bars[issue_id]:
            blockers.append(f"duplicate daily price: {issue_id} {day}")
        bars[issue_id][day] = Bar(day, open_price, high, low, close, volume, turnover, status)

    assessments: list[Assessment] = []
    assessment_keys: set[tuple[date, str, str]] = set()
    for index, row in enumerate(assessment_documents, 1):
        prefix = f"assessments:{index}"
        try:
            evaluation_day = date.fromisoformat(str(row.get("evaluation_date", "")))
            decision_at = _aware_datetime(row.get("decision_at_jst"), f"{prefix}.decision_at_jst")
            latest_published = _aware_datetime(
                row.get("latest_source_published_at_jst"),
                f"{prefix}.latest_source_published_at_jst",
            )
            review_type = str(row.get("review_type", "")).strip().upper()
            if review_type not in {"MONTHLY", "QUARTERLY", "IMMEDIATE", "MILESTONE"}:
                raise ValueError(f"{prefix}.review_type is invalid")
            status = str(row.get("status", "")).strip().upper()
            if status not in {"COMPLETE_PASS", "COMPLETE_FAIL"}:
                raise ValueError(f"{prefix}.status must be COMPLETE_PASS or COMPLETE_FAIL")
            hard_gates = _boolean(row.get("hard_gates_passed"), f"{prefix}.hard_gates_passed")
            score = _finite(row.get("score"), f"{prefix}.score")
            market_score = _finite(row.get("market_score"), f"{prefix}.market_score")
            reverse_score = _finite(row.get("reverse_score"), f"{prefix}.reverse_score")
            liquidity = _boolean(row.get("liquidity_passed"), f"{prefix}.liquidity_passed")
            entry_ready = _boolean(row.get("entry_ready"), f"{prefix}.entry_ready")
            add_ready = _boolean(row.get("add_ready"), f"{prefix}.add_ready")
            required_cagr = _finite(
                row.get("required_revenue_cagr_pct"), f"{prefix}.required_revenue_cagr_pct"
            )
            dilution = _finite(row.get("dilution_outlook_pct"), f"{prefix}.dilution_outlook_pct")
            other_score = _finite(row.get("other_score"), f"{prefix}.other_score")
            kpi_missed = _boolean(row.get("major_kpi_missed"), f"{prefix}.major_kpi_missed")
        except (TypeError, ValueError) as error:
            blockers.append(str(error))
            continue
        if decision_at.date() != evaluation_day or latest_published > decision_at:
            blockers.append(f"{prefix} contains a look-ahead timestamp")
        issue_id = str(row.get("issue_id", "")).strip()
        active_master = next(
            (
                record
                for record in masters
                if record["issue_id"] == issue_id
                and record["effective_from_date"] <= evaluation_day
                and evaluation_day <= (record["effective_through_date"] or date.max)
            ),
            None,
        )
        if active_master is None:
            blockers.append(f"{prefix} has no point-in-time security master")
        else:
            if str(row.get("code", "")).strip() != active_master["code"]:
                blockers.append(f"{prefix}.code does not match the security master")
            if str(row.get("sector", "")).strip() != active_master["sector"]:
                blockers.append(f"{prefix}.sector does not match the security master")
        if (
            not 0 <= score <= 100
            or not 0 <= market_score <= 15
            or not 0 <= reverse_score <= 15
            or not 0 <= other_score <= 70
        ):
            blockers.append(f"{prefix} score is outside the rule range")
        if not math.isclose(
            score, other_score + market_score + reverse_score, abs_tol=1e-9
        ):
            blockers.append(f"{prefix}.score does not equal its three score components")
        expected_entry = (
            hard_gates and score >= 70 and market_score >= 8
            and reverse_score >= 10 and liquidity and status == "COMPLETE_PASS"
        )
        if entry_ready != expected_entry:
            blockers.append(f"{prefix}.entry_ready does not match v0.2 hard gates")
        if add_ready and (score < 75 or not hard_gates or not liquidity):
            blockers.append(f"{prefix}.add_ready violates the quarterly add gates")
        source_ids = row.get("source_ids")
        if not isinstance(source_ids, list) or not source_ids:
            blockers.append(f"{prefix}.source_ids must be a non-empty array")
            source_ids = []
        for source_id in source_ids:
            registered = sources.get(str(source_id))
            if not registered:
                blockers.append(f"{prefix} source_id is unknown: {source_id}")
            elif registered["published"] > decision_at:
                blockers.append(f"{prefix} source was published after the decision: {source_id}")
        exit_rule = str(row.get("exit_rule", "")).strip().upper()
        if exit_rule and exit_rule not in IMMEDIATE_EXIT_RULES | QUARTERLY_EXIT_RULES:
            blockers.append(f"{prefix}.exit_rule is unsupported: {exit_rule}")
        if exit_rule in IMMEDIATE_EXIT_RULES and review_type != "IMMEDIATE":
            blockers.append(f"{prefix} immediate exit rule requires IMMEDIATE review_type")
        if exit_rule in QUARTERLY_EXIT_RULES and review_type != "QUARTERLY":
            blockers.append(f"{prefix} quarterly exit rule requires QUARTERLY review_type")
        key = (evaluation_day, issue_id, review_type)
        if key in assessment_keys:
            blockers.append(f"duplicate assessment: {key}")
        assessment_keys.add(key)
        assessments.append(
            Assessment(
                evaluation_day, decision_at, review_type,
                str(row.get("issue_id", "")).strip(), str(row.get("code", "")).strip(),
                str(row.get("sector", "")).strip(), status, hard_gates, score,
                market_score, reverse_score, liquidity, entry_ready, add_ready,
                required_cagr, dilution, other_score, kpi_missed, exit_rule,
                tuple(str(item) for item in source_ids), latest_published,
            )
        )

    review_event_ids: set[str] = set()
    required_review_count = 0
    completed_review_count = 0
    for index, row in enumerate(csv_rows.get("review_events", []), 2):
        prefix = f"review_events:{index}"
        if not any(str(value).strip() for value in row.values()):
            continue
        try:
            event_day = date.fromisoformat(row.get("event_date", ""))
            due_day = date.fromisoformat(row.get("review_due_date", ""))
            available = _aware_datetime(
                row.get("available_at_jst"), f"{prefix}.available_at_jst"
            )
        except (TypeError, ValueError) as error:
            blockers.append(str(error))
            continue
        event_id = row.get("event_id", "").strip()
        issue_id = row.get("issue_id", "").strip()
        review_type = row.get("review_type", "").strip().upper()
        if not event_id or event_id in review_event_ids:
            blockers.append(f"{prefix}.event_id is missing or duplicated")
        review_event_ids.add(event_id)
        if review_type not in {"QUARTERLY", "IMMEDIATE"}:
            blockers.append(f"{prefix}.review_type must be QUARTERLY or IMMEDIATE")
        if event_day > due_day or due_day not in sessions:
            blockers.append(f"{prefix}.review_due_date must be a trading session on or after event_date")
        if available.date() > due_day:
            blockers.append(f"{prefix} was not available by review_due_date")
        source_id = row.get("source_id", "").strip()
        source_record = sources.get(source_id)
        if source_record is None:
            blockers.append(f"{prefix} source_id is unknown")
        elif source_record["published"] > available:
            blockers.append(f"{prefix} source was published after available_at_jst")
        if not any(record["issue_id"] == issue_id for record in masters):
            blockers.append(f"{prefix}.issue_id is unknown")
        required_review_count += 1
        if (due_day, issue_id, review_type) in assessment_keys:
            completed_review_count += 1
        else:
            blockers.append(
                f"required disclosure assessment is missing: {due_day} {issue_id} {review_type}"
            )

    regimes: dict[date, tuple[str, float]] = {}
    for index, row in enumerate(csv_rows.get("market_regime", []), 2):
        prefix = f"market_regime:{index}"
        try:
            day = date.fromisoformat(row.get("evaluation_date", ""))
            state = row.get("state", "").strip().upper()
            multiplier = _finite(row.get("entry_multiplier"), f"{prefix}.entry_multiplier")
            available = _aware_datetime(row.get("available_at_jst"), f"{prefix}.available_at_jst")
        except (TypeError, ValueError) as error:
            blockers.append(str(error))
            continue
        expected = {"NORMAL": 1.0, "CAUTION": 0.5, "STRESS": 0.0, "UNAVAILABLE": 0.0}
        if state not in expected or multiplier != expected.get(state):
            blockers.append(f"{prefix} state/multiplier does not match MRS-v0.1")
        if available.date() != day:
            blockers.append(f"{prefix} contains a look-ahead timestamp")
        source_ids = [item for item in row.get("source_ids", "").split("|") if item]
        if not source_ids or any(item not in sources for item in source_ids):
            blockers.append(f"{prefix}.source_ids are missing or unknown")
        for source_id in source_ids:
            if source_id in sources and sources[source_id]["published"] > available:
                blockers.append(f"{prefix} source was published after available_at_jst")
        if day in regimes:
            blockers.append(f"duplicate market regime: {day}")
        regimes[day] = (state, multiplier)

    actions: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(csv_rows.get("corporate_actions", []), 2):
        prefix = f"corporate_actions:{index}"
        if not any(str(value).strip() for value in row.values()):
            continue
        try:
            effective_day = date.fromisoformat(row.get("effective_date", ""))
            announced = _aware_datetime(row.get("announced_at_jst"), f"{prefix}.announced_at_jst")
            ratio = _finite(row.get("ratio") or 0, f"{prefix}.ratio")
            cash = _finite(row.get("cash_consideration") or 0, f"{prefix}.cash_consideration")
            fractional_cash = _finite(
                row.get("fractional_cash_price") or 0,
                f"{prefix}.fractional_cash_price",
            )
        except (TypeError, ValueError) as error:
            blockers.append(str(error))
            continue
        action_type = row.get("action_type", "").strip().upper()
        if action_type not in {
            "SPLIT", "REVERSE_SPLIT", "CASH_MERGER", "STOCK_MERGER",
            "CODE_CHANGE", "DELISTING",
        }:
            blockers.append(f"{prefix}.action_type is invalid")
        if announced.date() > effective_day:
            blockers.append(f"{prefix} was announced after its effective date")
        if action_type in {"SPLIT", "REVERSE_SPLIT", "STOCK_MERGER"} and ratio <= 0:
            blockers.append(f"{prefix}.ratio must be positive")
        if fractional_cash < 0:
            blockers.append(f"{prefix}.fractional_cash_price cannot be negative")
        if (
            action_type in {"SPLIT", "REVERSE_SPLIT", "STOCK_MERGER"}
            and ratio > 0
            and not math.isclose(board_lot * ratio, round(board_lot * ratio), abs_tol=1e-9)
            and fractional_cash <= 0
        ):
            blockers.append(
                f"{prefix}.fractional_cash_price is required when a board lot creates fractions"
            )
        if action_type == "CASH_MERGER" and cash <= 0:
            blockers.append(f"{prefix}.cash_consideration must be positive")
        if action_type == "STOCK_MERGER" and not row.get("successor_issue_id", "").strip():
            blockers.append(f"{prefix}.successor_issue_id is required")
        issue_id = row.get("issue_id", "").strip()
        if not issue_id:
            blockers.append(f"{prefix}.issue_id is required")
        elif not any(record["issue_id"] == issue_id for record in masters):
            blockers.append(f"{prefix}.issue_id is unknown")
        if action_type == "CODE_CHANGE" and not any(
            record["issue_id"] == issue_id
            and record["effective_from_date"] <= effective_day
            and effective_day <= (record["effective_through_date"] or date.max)
            for record in masters
        ):
            blockers.append(f"{prefix} requires an effective security master row")
        if action_type == "DELISTING" and cash <= 0:
            blockers.append(
                f"{prefix}.cash_consideration must contain the documented terminal settlement price"
            )
        successor_id = row.get("successor_issue_id", "").strip()
        if action_type == "STOCK_MERGER" and not any(
            record["issue_id"] == successor_id
            and record["effective_from_date"] <= effective_day
            and effective_day <= (record["effective_through_date"] or date.max)
            for record in masters
        ):
            blockers.append(f"{prefix} successor security master is missing")
        source_record = sources.get(row.get("source_id", "").strip())
        if source_record is None:
            blockers.append(f"{prefix} source_id is unknown")
        elif source_record["published"] > announced:
            blockers.append(f"{prefix} source was published after announced_at_jst")
        processing_day = next((session for session in sessions if session >= effective_day), None)
        if period_from <= effective_day <= period_through and processing_day is None:
            blockers.append(f"{prefix} has no replay session on or after its effective date")
        if processing_day is not None:
            actions[processing_day].append(
                {
                    **row,
                    "effective_day": effective_day,
                    "ratio_value": ratio,
                    "cash_value": cash,
                    "fractional_cash_value": fractional_cash,
                }
            )

    monthly = [
        day
        for index, day in enumerate(sessions)
        if (
            (index + 1 < len(sessions) and sessions[index + 1].month != day.month)
            or (index == len(sessions) - 1 and day.day >= 28)
        )
    ]
    required_count = 0
    evaluated_count = 0
    universe_rows: list[dict[str, Any]] = []
    monthly_assessment_keys = {
        (item.evaluation_day, item.issue_id) for item in assessments if item.review_type == "MONTHLY"
    }
    for evaluation_day in monthly:
        if evaluation_day not in sessions:
            blockers.append(f"monthly assessment is not on a trading session: {evaluation_day}")
        active: dict[str, dict[str, Any]] = {}
        for record in masters:
            through = record["effective_through_date"] or date.max
            if (
                record["domestic"]
                and record["effective_from_date"] <= evaluation_day <= through
            ):
                active[record["issue_id"]] = record
        if not active:
            blockers.append(f"point-in-time universe is empty: {evaluation_day}")
        if evaluation_day not in regimes:
            blockers.append(f"market regime is missing: {evaluation_day}")
        for issue_id, record in sorted(active.items()):
            required_count += 1
            present = (evaluation_day, issue_id) in monthly_assessment_keys
            evaluated_count += int(present)
            universe_rows.append(
                {
                    "evaluation_date": evaluation_day.isoformat(),
                    "issue_id": issue_id,
                    "code": record["code"],
                    "assessment_present": str(present).lower(),
                }
            )
            if not present:
                blockers.append(f"full-universe assessment is missing: {evaluation_day} {issue_id}")
    if not monthly:
        blockers.append("at least one full-universe MONTHLY assessment is required")

    for day in sessions:
        active_ids = {
            record["issue_id"]
            for record in masters
            if record["domestic"]
            and record["effective_from_date"] <= day
            and day <= (record["effective_through_date"] or date.max)
        }
        for issue_id in sorted(active_ids):
            if day not in bars.get(issue_id, {}):
                blockers.append(f"daily price/status row is missing: {day} {issue_id}")

    if generated_at.date() < period_through:
        blockers.append("manifest cannot be generated before period.through")
    if period_through > reference_time.date():
        blockers.append("replay period cannot end in the future")
    if kind == "RETROSPECTIVE_STRESS_TEST" and manifest.get("holdout_claimed") is not False:
        blockers.append("retrospective replay must set holdout_claimed to false")
    if kind == "FORWARD_HOLDOUT" and manifest.get("holdout_claimed") is not True:
        blockers.append("forward holdout must set holdout_claimed to true")

    if blockers:
        raise ReplayInputError(blockers)
    model = {
        "manifest": manifest,
        "manifest_path": manifest_path,
        "datasets": datasets,
        "sources": sources,
        "masters": masters,
        "sessions": sessions,
        "bars": bars,
        "assessments": assessments,
        "regimes": regimes,
        "benchmark": benchmark,
        "calendar": calendar,
        "actions": actions,
        "period_from": period_from,
        "period_through": period_through,
        "initial_capital": initial_capital,
        "fee_rate": fee_rate,
        "slippage_rate": slippage_rate,
        "board_lot": board_lot,
        "holdout_plan": holdout_plan,
        "universe_rows": universe_rows,
        "required_count": required_count,
        "evaluated_count": evaluated_count,
    }
    quality = {
        "schema_version": "1.0",
        "status": "COMPLETED",
        "missing_hard_gate_inputs": 0,
        "lookahead_violations": 0,
        "required_universe_evaluations": required_count,
        "completed_universe_evaluations": evaluated_count,
        "required_disclosure_reviews": required_review_count,
        "completed_disclosure_reviews": completed_review_count,
        "monthly_evaluation_count": len(monthly),
        "trading_session_count": len(sessions),
        "corporate_action_count": sum(len(items) for items in actions.values()),
        "security_event_types": sorted(known_events),
        "prohibited_provider_rows": 0,
    }
    return model, quality


def _master_on(model: dict[str, Any], issue_id: str, day: date) -> dict[str, Any] | None:
    matches = [
        item for item in model["masters"]
        if item["issue_id"] == issue_id
        and item["effective_from_date"] <= day
        and day <= (item["effective_through_date"] or date.max)
    ]
    return matches[-1] if matches else None


def _next_session_index(sessions: list[date], day: date) -> int | None:
    for index, session in enumerate(sessions):
        if session > day:
            return index
    return None


def _trailing_turnover(model: dict[str, Any], issue_id: str, index: int) -> float:
    observations = [
        model["bars"][issue_id][day].turnover
        for day in model["sessions"][max(0, index - 20):index]
        if model["bars"][issue_id][day].status == "OK"
    ]
    return sum(observations) / len(observations) if observations else 0.0


def _month_end(sessions: list[date], index: int) -> bool:
    if index + 1 < len(sessions):
        return sessions[index + 1].month != sessions[index].month
    return sessions[index].day >= 28


def _latest_assessment(
    assessments: list[Assessment], issue_id: str, day: date
) -> Assessment | None:
    matches = [item for item in assessments if item.issue_id == issue_id and item.evaluation_day <= day]
    return max(matches, key=lambda item: item.decision_at) if matches else None


def _regime_on_or_before(
    regimes: dict[date, tuple[str, float]], day: date
) -> tuple[str, float]:
    available = [candidate for candidate in regimes if candidate <= day]
    return regimes[max(available)] if available else ("UNAVAILABLE", 0.0)


def _anniversary(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # February 29 in a non-leap target year.
        return day.replace(year=day.year + years, day=28)


def _position_costs(positions: dict[str, Position]) -> tuple[float, dict[str, float]]:
    total = sum(item.acquisition_cost for item in positions.values() if item.quantity > 0)
    industries: dict[str, float] = defaultdict(float)
    for item in positions.values():
        if item.quantity > 0:
            industries[item.sector] += item.acquisition_cost
    return total, industries


def _schedule_sell(
    pending: list[Order], *, position: Position, rule_id: str, decision_day: date,
    execute_index: int | None, quantity: int | None = None, immediate: bool = False,
) -> None:
    if execute_index is None or position.quantity <= 0:
        return
    requested = position.quantity if quantity is None else min(position.quantity, max(0, quantity))
    if requested <= 0:
        return
    existing = next(
        (item for item in pending if item.issue_id == position.issue_id and item.side == "SELL"),
        None,
    )
    if existing:
        existing.requested_quantity = max(existing.requested_quantity or 0, requested)
        existing.immediate = existing.immediate or immediate
        if requested == position.quantity:
            existing.rule_id = rule_id
        return
    pending.append(
        Order(position.issue_id, "SELL", rule_id, decision_day, execute_index, requested, immediate=immediate)
    )


def _apply_corporate_actions(
    *, model: dict[str, Any], day: date, positions: dict[str, Position], cash: float,
    trades: list[dict[str, Any]], rule_version: str,
) -> float:
    for action in model["actions"].get(day, []):
        issue_id = action.get("issue_id", "").strip()
        position = positions.get(issue_id)
        if not position or position.quantity <= 0:
            continue
        kind = action["action_type"].strip().upper()
        ratio = float(action["ratio_value"])
        if kind in {"SPLIT", "REVERSE_SPLIT"}:
            raw_quantity = position.quantity * ratio
            new_quantity = math.floor(raw_quantity + 1e-9)
            fraction = max(0.0, raw_quantity - new_quantity)
            if fraction > 1e-9:
                fractional_price = float(action["fractional_cash_value"])
                if fractional_price <= 0:
                    raise ReplayInputError(
                        [f"fractional cash price is missing: {issue_id} {day}"]
                    )
                gross = fraction * fractional_price
                cash += gross
                position.sale_gross += gross
                trades.append(
                    {
                        "rule_version": rule_version,
                        "decision_date": action["effective_day"].isoformat(),
                        "trade_date": day.isoformat(),
                        "issue_id": issue_id,
                        "code": position.code,
                        "sector": position.sector,
                        "side": "SELL",
                        "rule_id": "CORPORATE_ACTION_FRACTIONAL_CASH",
                        "quantity": fraction,
                        "price": fractional_price,
                        "gross": gross,
                        "fee": 0.0,
                    }
                )
            old_quantity = position.quantity
            if old_quantity > 0:
                position.acquisition_cost *= new_quantity / raw_quantity
            position.quantity = new_quantity
            position.q0 *= ratio
            position.average_price = (
                position.acquisition_cost / new_quantity if new_quantity else 0.0
            )
        elif kind == "CASH_MERGER":
            gross = position.quantity * float(action["cash_value"])
            cash += gross
            position.sale_gross += gross
            trades.append(
                {
                    "rule_version": rule_version, "decision_date": day.isoformat(),
                    "trade_date": day.isoformat(), "issue_id": issue_id,
                    "code": position.code, "sector": position.sector, "side": "SELL",
                    "rule_id": "CORPORATE_ACTION_CASH_MERGER", "quantity": position.quantity,
                    "price": action["cash_value"], "gross": gross, "fee": 0.0,
                }
            )
            position.quantity = 0
            position.acquisition_cost = 0.0
            position.permanent_rebuy_block = True
        elif kind == "STOCK_MERGER":
            successor_id = action.get("successor_issue_id", "").strip()
            successor_master = _master_on(model, successor_id, day)
            if not successor_master:
                raise ReplayInputError([f"successor security is missing: {successor_id} {day}"])
            successor = positions.setdefault(
                successor_id,
                Position(successor_id, successor_master["code"], successor_master["sector"]),
            )
            raw_transferred = position.quantity * ratio
            transferred = math.floor(raw_transferred + 1e-9)
            fraction = max(0.0, raw_transferred - transferred)
            if fraction > 1e-9:
                fractional_price = float(action["fractional_cash_value"])
                if fractional_price <= 0:
                    raise ReplayInputError(
                        [f"fractional cash price is missing: {issue_id} {day}"]
                    )
                gross = fraction * fractional_price
                cash += gross
                position.sale_gross += gross
                trades.append(
                    {
                        "rule_version": rule_version,
                        "decision_date": action["effective_day"].isoformat(),
                        "trade_date": day.isoformat(),
                        "issue_id": issue_id,
                        "code": position.code,
                        "sector": position.sector,
                        "side": "SELL",
                        "rule_id": "CORPORATE_ACTION_FRACTIONAL_CASH",
                        "quantity": fraction,
                        "price": fractional_price,
                        "gross": gross,
                        "fee": 0.0,
                    }
                )
            transferred_q0 = position.q0 * ratio
            transferable_cost = (
                position.acquisition_cost * transferred / raw_transferred
                if raw_transferred > 0
                else 0.0
            )
            successor.quantity += transferred
            successor.q0 += transferred_q0
            successor.acquisition_cost += transferable_cost
            successor.average_price = (
                successor.acquisition_cost / successor.quantity if successor.quantity else 0.0
            )
            successor.initial_session_index = position.initial_session_index
            successor.add_count += position.add_count
            successor.add_banned = successor.add_banned or position.add_banned
            successor.five_x_done = successor.five_x_done or position.five_x_done
            successor.ten_x_done = successor.ten_x_done or position.ten_x_done
            successor.ten_x_day = successor.ten_x_day or position.ten_x_day
            successor.quarterly_rule = position.quarterly_rule
            successor.quarterly_streak = max(
                successor.quarterly_streak, position.quarterly_streak
            )
            successor.c6_checked = successor.c6_checked or position.c6_checked
            successor.purchase_gross += position.purchase_gross
            successor.sale_gross += position.sale_gross
            successor.fees += position.fees
            position.quantity = 0
            position.acquisition_cost = 0.0
            position.purchase_gross = 0.0
            position.sale_gross = 0.0
            position.fees = 0.0
            position.permanent_rebuy_block = True
        elif kind == "CODE_CHANGE":
            current_master = _master_on(model, issue_id, day)
            if not current_master:
                raise ReplayInputError(
                    [f"code change security master is missing: {issue_id} {day}"]
                )
            position.code = current_master["code"]
            position.sector = current_master["sector"]
        elif kind == "DELISTING":
            settlement = float(action["cash_value"])
            gross = position.quantity * settlement
            cash += gross
            position.sale_gross += gross
            trades.append(
                {
                    "rule_version": rule_version,
                    "decision_date": action["effective_day"].isoformat(),
                    "trade_date": day.isoformat(),
                    "issue_id": issue_id,
                    "code": position.code,
                    "sector": position.sector,
                    "side": "SELL",
                    "rule_id": "CORPORATE_ACTION_DELISTING",
                    "quantity": position.quantity,
                    "price": settlement,
                    "gross": gross,
                    "fee": 0.0,
                }
            )
            position.quantity = 0
            position.acquisition_cost = 0.0
            position.average_price = 0.0
            position.permanent_rebuy_block = True
    return cash


def replay_version(*, model: dict[str, Any], rule_version: str) -> ReplayRun:
    initial_capital = model["initial_capital"]
    fee_rate = model["fee_rate"]
    slippage_rate = model["slippage_rate"]
    board_lot = model["board_lot"]
    caps = allocation_caps(rule_version)
    sessions: list[date] = model["sessions"]
    assessments: list[Assessment] = model["assessments"]
    assessments_by_day: dict[date, list[Assessment]] = defaultdict(list)
    for item in assessments:
        assessments_by_day[item.evaluation_day].append(item)
    positions: dict[str, Position] = {}
    pending: list[Order] = []
    cash = initial_capital
    peak_nav = initial_capital
    maximum_drawdown = 0.0
    run = ReplayRun(rule_version)
    latest_scores: dict[str, Assessment] = {}
    issue_loss_min: dict[str, float] = defaultdict(float)
    industry_loss_min: dict[str, float] = defaultdict(float)

    for index, day in enumerate(sessions):
        cash = _apply_corporate_actions(
            model=model, day=day, positions=positions, cash=cash,
            trades=run.trades, rule_version=rule_version,
        )
        todays_orders = [item for item in pending if item.execute_index <= index]
        pending = [item for item in pending if item.execute_index > index]
        for order in todays_orders:
            bar = model["bars"].get(order.issue_id, {}).get(day)
            if not bar or bar.status != "OK":
                if order.side == "SELL" and index + 1 < len(sessions):
                    order.execute_index = index + 1
                    pending.append(order)
                else:
                    run.skipped.append(
                        {"rule_version": rule_version, "date": day.isoformat(),
                         "issue_id": order.issue_id, "side": order.side,
                         "rule_id": order.rule_id, "reason": "NO_TRADABLE_QUOTE"}
                    )
                continue
            position = positions.get(order.issue_id)
            if order.side == "BUY":
                if position is None:
                    assessment = latest_scores[order.issue_id]
                    position = Position(order.issue_id, assessment.code, assessment.sector)
                    positions[order.issue_id] = position
                assessment = latest_scores[order.issue_id]
                evaluation_bar = model["bars"][order.issue_id][order.decision_day]
                limit = evaluation_bar.close * 1.15
                if bar.open <= limit:
                    price = min(bar.open * (1 + slippage_rate), limit)
                elif bar.low <= limit:
                    price = limit
                else:
                    run.skipped.append(
                        {"rule_version": rule_version, "date": day.isoformat(),
                         "issue_id": order.issue_id, "side": "BUY", "rule_id": order.rule_id,
                         "reason": "LIMIT_NOT_FILLED"}
                    )
                    continue
                multiplier = _regime_on_or_before(model["regimes"], order.decision_day)[1]
                tranche_pct = caps.initial_entry_pct if order.tranche == "INITIAL" else caps.add_entry_pct
                total_cost, industry_costs = _position_costs(positions)
                available = min(
                    initial_capital * tranche_pct * multiplier / 100,
                    initial_capital * caps.single_name_cost_pct / 100 - position.acquisition_cost,
                    initial_capital * caps.candidate_pool_cost_pct / 100 - total_cost,
                    initial_capital * caps.industry_cost_pct / 100 - industry_costs[position.sector],
                    _trailing_turnover(model, order.issue_id, index) * 0.05 * 5
                    - position.acquisition_cost,
                    cash / (1 + fee_rate),
                )
                quantity = math.floor(max(0.0, available) / price / board_lot) * board_lot
                if quantity <= 0:
                    run.skipped.append(
                        {"rule_version": rule_version, "date": day.isoformat(),
                         "issue_id": order.issue_id, "side": "BUY", "rule_id": order.rule_id,
                         "reason": "CAP_LIQUIDITY_CASH_OR_BOARD_LOT"}
                    )
                    continue
                gross = quantity * price
                fee = gross * fee_rate
                cash -= gross + fee
                old_quantity = position.quantity
                position.quantity += quantity
                position.q0 += quantity
                position.acquisition_cost += gross
                position.purchase_gross += gross
                position.fees += fee
                position.average_price = (
                    ((position.average_price * old_quantity) + gross) / position.quantity
                )
                if position.initial_session_index is None:
                    position.initial_session_index = index
                position.approved_required_revenue_cagr_pct = (
                    assessment.required_revenue_cagr_pct
                )
                position.approved_dilution_outlook_pct = assessment.dilution_outlook_pct
                if order.tranche == "ADD":
                    position.add_count += 1
                run.trades.append(
                    {"rule_version": rule_version, "decision_date": order.decision_day.isoformat(),
                     "trade_date": day.isoformat(), "issue_id": order.issue_id,
                     "code": position.code, "sector": position.sector, "side": "BUY",
                     "rule_id": order.rule_id, "quantity": quantity, "price": price,
                     "gross": gross, "fee": fee}
                )
            else:
                if not position or position.quantity <= 0:
                    continue
                requested = min(position.quantity, order.requested_quantity or position.quantity)
                participation = 0.10 if order.immediate else 0.05
                capacity = _trailing_turnover(model, order.issue_id, index) * participation
                price = bar.close * (1 - slippage_rate)
                capacity_quantity = math.floor(capacity / price / board_lot) * board_lot
                if requested < board_lot and requested * price <= capacity:
                    quantity = requested
                else:
                    quantity = min(requested, capacity_quantity)
                if quantity <= 0:
                    if index + 1 < len(sessions):
                        order.execute_index = index + 1
                        pending.append(order)
                    continue
                gross = quantity * price
                fee = gross * fee_rate
                cash += gross - fee
                cost_reduction = position.acquisition_cost * quantity / position.quantity
                position.quantity -= quantity
                position.acquisition_cost -= cost_reduction
                position.sale_gross += gross
                position.fees += fee
                if position.quantity == 0:
                    position.acquisition_cost = 0.0
                    position.average_price = 0.0
                    position.rebuy_after_index = index + 63
                    if order.rule_id in PERMANENT_REBUY_BLOCK_RULES:
                        position.permanent_rebuy_block = True
                run.trades.append(
                    {"rule_version": rule_version, "decision_date": order.decision_day.isoformat(),
                     "trade_date": day.isoformat(), "issue_id": order.issue_id,
                     "code": position.code, "sector": position.sector, "side": "SELL",
                     "rule_id": order.rule_id, "quantity": quantity, "price": price,
                     "gross": gross, "fee": fee}
                )
                remainder = requested - quantity
                if remainder > 0 and index + 1 < len(sessions):
                    order.execute_index = index + 1
                    order.requested_quantity = remainder
                    pending.append(order)

        for assessment in sorted(assessments_by_day.get(day, []), key=lambda item: item.review_type):
            latest_scores[assessment.issue_id] = assessment
            position = positions.get(assessment.issue_id)
            execute_index = _next_session_index(sessions, day)
            if position and position.quantity > 0 and assessment.exit_rule:
                if assessment.exit_rule in IMMEDIATE_EXIT_RULES:
                    _schedule_sell(
                        pending, position=position, rule_id=assessment.exit_rule,
                        decision_day=day, execute_index=execute_index, immediate=True,
                    )
                else:
                    if position.quarterly_rule == assessment.exit_rule:
                        position.quarterly_streak += 1
                    else:
                        position.quarterly_rule = assessment.exit_rule
                        position.quarterly_streak = 1
                    rule = assessment.exit_rule
                    if rule == "S-B5" or position.quarterly_streak >= 2:
                        quantity = position.quantity
                    elif rule in {"S-B1", "S-B2"}:
                        quantity = math.ceil(position.q0 / 3)
                    elif rule == "S-B4":
                        quantity = math.ceil(position.q0 / 2)
                    else:  # S-B3 first occurrence is an add ban and special review.
                        quantity = 0
                    position.add_banned = True
                    if quantity:
                        _schedule_sell(
                            pending, position=position, rule_id=rule,
                            decision_day=day, execute_index=execute_index, quantity=quantity,
                        )
            elif position and assessment.review_type == "QUARTERLY":
                position.quarterly_rule = ""
                position.quarterly_streak = 0

        month_end = _month_end(sessions, index)
        execute_index = _next_session_index(sessions, day)
        position_stats: dict[str, tuple[Assessment | None, float, float, int]] = {}
        for position in list(positions.values()):
            if position.quantity <= 0:
                continue
            bar = model["bars"][position.issue_id][day]
            latest = _latest_assessment(assessments, position.issue_id, day)
            score = latest.score if latest else 0.0
            initial_index = position.initial_session_index
            elapsed = index - initial_index if initial_index is not None else 0
            history = [
                model["bars"][position.issue_id][session].close
                for session in sessions[max(0, index - 19):index + 1]
            ]
            ma20 = sum(history) / len(history)
            position.highest_close = max(position.highest_close, bar.close)
            position.highest_ma20 = max(position.highest_ma20 or ma20, ma20)
            dd20 = ma20 / position.highest_ma20 - 1
            position_stats[position.issue_id] = (latest, ma20, dd20, elapsed)

            time_rule = ""
            current_return = bar.close / position.average_price - 1
            milestone = next(
                (
                    item for item in assessments_by_day.get(day, [])
                    if item.issue_id == position.issue_id and item.review_type == "MILESTONE"
                ),
                None,
            )
            if elapsed == 63 and current_return <= -0.30:
                if milestone is None:
                    raise ReplayInputError(
                        [f"S-C2 milestone reassessment is missing: {day} {position.issue_id}"]
                    )
                if milestone.score < 70 or milestone.major_kpi_missed:
                    time_rule = "S-C2"
            elif elapsed == 126 and current_return <= -0.40:
                time_rule = "S-C3"
            elif elapsed == 252 and current_return <= -0.20:
                if milestone is None:
                    raise ReplayInputError(
                        [f"S-C4 milestone reassessment is missing: {day} {position.issue_id}"]
                    )
                if milestone.score < 70 or position.highest_close < 1.2 * position.average_price:
                    time_rule = "S-C4"
            elif elapsed == 504 and current_return < 0:
                if milestone is None:
                    raise ReplayInputError(
                        [f"S-C5 milestone reassessment is missing: {day} {position.issue_id}"]
                    )
                if milestone.score < 75 or position.highest_close < 1.5 * position.average_price:
                    time_rule = "S-C5"
            elif (
                not position.c6_checked
                and initial_index is not None
                and day >= _anniversary(sessions[initial_index], 3)
            ):
                position.c6_checked = True
                if ma20 < 10 * position.average_price:
                    time_rule = "S-C6"
            if time_rule:
                _schedule_sell(
                    pending, position=position, rule_id=time_rule,
                    decision_day=day, execute_index=execute_index,
                )

        if month_end:
            for position in list(positions.values()):
                if position.quantity <= 0 or position.issue_id not in position_stats:
                    continue
                bar = model["bars"][position.issue_id][day]
                latest, ma20, dd20, _ = position_stats[position.issue_id]
                score = latest.score if latest else 0.0
                full_rule = ""
                if bar.close <= 0.5 * position.average_price:
                    full_rule = "S-C1"
                elif position.ten_x_done and dd20 <= -0.5:
                    full_rule = "S-D4"
                elif (
                    position.ten_x_day and day >= position.ten_x_day + timedelta(days=365)
                    and ma20 < 8 * position.average_price and score < 75
                ):
                    full_rule = "S-D5"
                if full_rule:
                    _schedule_sell(
                        pending, position=position, rule_id=full_rule,
                        decision_day=day, execute_index=execute_index,
                    )
                    continue
                if position.ten_x_done and dd20 <= -0.3 and score < 70:
                    _schedule_sell(
                        pending, position=position, rule_id="S-D3",
                        decision_day=day, execute_index=execute_index,
                        quantity=math.ceil(position.q0 * 0.25),
                    )
                profit_quantity = 0
                profit_rules: list[str] = []
                if bar.close >= 5 * position.average_price and not position.five_x_done:
                    position.five_x_done = True
                    position.add_banned = True
                    profit_quantity += math.ceil(position.q0 * 0.20)
                    profit_rules.append("S-D1")
                if bar.close >= 10 * position.average_price and not position.ten_x_done:
                    position.ten_x_done = True
                    position.ten_x_day = day
                    profit_quantity += math.ceil(position.q0 * 0.30)
                    profit_rules.append("S-D2")
                if profit_quantity:
                    _schedule_sell(
                        pending, position=position, rule_id="+".join(profit_rules),
                        decision_day=day, execute_index=execute_index,
                        quantity=profit_quantity,
                    )

            monthly_assessments = [
                item for item in assessments_by_day.get(day, []) if item.review_type == "MONTHLY"
            ]
            held_count = sum(item.quantity > 0 for item in positions.values())
            candidates = sorted(
                (item for item in monthly_assessments if item.entry_ready),
                key=lambda item: (-item.score, -item.reverse_score, -item.market_score, item.code),
            )
            for assessment in candidates:
                position = positions.get(assessment.issue_id)
                if position and position.quantity > 0:
                    continue
                if position and (position.permanent_rebuy_block or index < position.rebuy_after_index):
                    continue
                if held_count >= caps.max_holdings:
                    break
                if execute_index is not None and _regime_on_or_before(model["regimes"], day)[1] > 0:
                    latest_scores[assessment.issue_id] = assessment
                    pending.append(
                        Order(assessment.issue_id, "BUY", "ENTRY", day, execute_index, tranche="INITIAL")
                    )
                    held_count += 1

        for assessment in assessments_by_day.get(day, []):
            if assessment.review_type != "QUARTERLY" or not assessment.add_ready:
                continue
            position = positions.get(assessment.issue_id)
            if (
                position and position.quantity > 0 and not position.add_banned
                and not position.five_x_done and position.add_count < caps.max_adds
                and position.approved_required_revenue_cagr_pct is not None
                and assessment.required_revenue_cagr_pct
                <= position.approved_required_revenue_cagr_pct
                and position.approved_dilution_outlook_pct is not None
                and assessment.dilution_outlook_pct
                <= position.approved_dilution_outlook_pct
                and _regime_on_or_before(model["regimes"], day)[1] > 0
            ):
                execute_index = _next_session_index(sessions, day)
                if execute_index is not None:
                    latest_scores[assessment.issue_id] = assessment
                    pending.append(
                        Order(assessment.issue_id, "BUY", "QUARTERLY_ADD", day, execute_index, tranche="ADD")
                    )

        market_value = sum(
            position.quantity * model["bars"][issue_id][day].close
            for issue_id, position in positions.items()
            if position.quantity > 0 and day in model["bars"].get(issue_id, {})
        )
        nav = cash + market_value
        peak_nav = max(peak_nav, nav)
        drawdown = nav / peak_nav - 1
        maximum_drawdown = min(maximum_drawdown, drawdown)
        contributions: dict[str, float] = {}
        industry_contributions: dict[str, float] = defaultdict(float)
        for issue_id, position in positions.items():
            value = position.quantity * model["bars"].get(issue_id, {}).get(
                day, Bar(day, 0, 0, 0, 0, 0, 0, "HALTED")
            ).close
            contribution = value + position.sale_gross - position.purchase_gross - position.fees
            contributions[issue_id] = contribution
            industry_contributions[position.sector] += contribution
            issue_loss_min[issue_id] = min(issue_loss_min[issue_id], contribution)
        for sector, contribution in industry_contributions.items():
            industry_loss_min[sector] = min(industry_loss_min[sector], contribution)
        run.daily.append(
            {"date": day.isoformat(), "cash": cash, "market_value": market_value,
             "nav": nav, "drawdown_pct": drawdown * 100,
             "holding_count": sum(item.quantity > 0 for item in positions.values())}
        )

        if month_end and execute_index is not None:
            for position in positions.values():
                if position.quantity <= 0:
                    continue
                value = position.quantity * model["bars"][position.issue_id][day].close
                if value / nav > 0.20:
                    target_quantity = math.ceil((value - 0.15 * nav) / model["bars"][position.issue_id][day].close)
                    _schedule_sell(
                        pending, position=position, rule_id="S-D6",
                        decision_day=day, execute_index=execute_index,
                        quantity=target_quantity,
                    )

    final_nav = run.daily[-1]["nav"]
    run.summary = {
        "rule_version": rule_version,
        "initial_capital": initial_capital,
        "final_nav": round(final_nav, 2),
        "return_pct": round((final_nav / initial_capital - 1) * 100, 6),
        "maximum_drawdown_pct": round(maximum_drawdown * 100, 6),
        "maximum_single_name_loss_contribution_pct": round(
            min(issue_loss_min.values(), default=0.0) / initial_capital * 100, 6
        ),
        "maximum_industry_loss_contribution_pct": round(
            min(industry_loss_min.values(), default=0.0) / initial_capital * 100, 6
        ),
        "trade_count": len(run.trades),
        "buy_count": sum(item["side"] == "BUY" for item in run.trades),
        "sell_count": sum(item["side"] == "SELL" for item in run.trades),
        "skipped_order_count": len(run.skipped),
        "open_order_count": len(pending),
    }
    return run


def _monthly_returns(run: ReplayRun) -> list[dict[str, Any]]:
    endpoints: dict[str, dict[str, Any]] = {}
    for row in run.daily:
        endpoints[row["date"][:7]] = row
    result: list[dict[str, Any]] = []
    previous = float(run.summary["initial_capital"])
    for month, row in sorted(endpoints.items()):
        nav = float(row["nav"])
        result.append(
            {"rule_version": run.rule_version, "month": month,
             "ending_nav": round(nav, 2), "return_pct": round((nav / previous - 1) * 100, 6)}
        )
        previous = nav
    return result


def write_replay(*, root: Path, manifest_path: Path, output_dir: Path) -> dict[str, Any]:
    model, quality = validate_inputs(root=root, manifest_path=manifest_path)
    private = _private_root(root)
    output_dir = output_dir.resolve()
    try:
        output_dir.relative_to(private)
    except ValueError as error:
        raise ReplayInputError(["output directory must stay under operations/private"]) from error
    output_dir.mkdir(parents=True, exist_ok=True)
    quality_path = output_dir / "input-quality.json"
    universe_path = output_dir / "universe-validation.csv"
    _write_json(quality_path, quality)
    _write_csv(
        universe_path,
        ["evaluation_date", "issue_id", "code", "assessment_present"],
        model["universe_rows"],
    )
    point_manifest_path = output_dir / "point-in-time-validation.json"
    point_manifest = {
        "schema_version": "1.0", "status": "COMPLETED",
        "generated_at_jst": datetime.now(tz=JST).isoformat(timespec="seconds"),
        "as_of_date": model["period_through"].isoformat(),
        "universe": {
            "required_count": model["required_count"],
            "evaluated_count": model["evaluated_count"],
            "point_in_time_security_master": True, "includes_delisted": True,
            "includes_mergers": True, "includes_corporate_actions": True,
        },
        "quality": {
            "missing_hard_gate_inputs": 0,
            "lookahead_violations": 0,
            "required_disclosure_reviews": quality["required_disclosure_reviews"],
            "completed_disclosure_reviews": quality["completed_disclosure_reviews"],
        },
        "artifacts": [
            {"role": "source_snapshot", "path": _private_relative(root, manifest_path),
             "sha256": _sha256(manifest_path)},
            {"role": "universe_validation", "path": _private_relative(root, universe_path),
             "sha256": _sha256(universe_path)},
            {"role": "quality_report", "path": _private_relative(root, quality_path),
             "sha256": _sha256(quality_path)},
        ],
    }
    _write_json(point_manifest_path, point_manifest)

    runs = [replay_version(model=model, rule_version=version) for version in ("v0.2", "v0.4")]
    trade_path = output_dir / "trade-log.csv"
    daily_path = output_dir / "daily-metrics.csv"
    monthly_path = output_dir / "monthly-returns.csv"
    metrics_path = output_dir / "metrics.json"
    _write_csv(
        trade_path,
        ["rule_version", "decision_date", "trade_date", "issue_id", "code", "sector",
         "side", "rule_id", "quantity", "price", "gross", "fee"],
        (trade for run in runs for trade in run.trades),
    )
    _write_csv(
        daily_path,
        ["rule_version", "date", "cash", "market_value", "nav", "drawdown_pct", "holding_count"],
        ({"rule_version": run.rule_version, **row} for run in runs for row in run.daily),
    )
    monthly_rows = [row for run in runs for row in _monthly_returns(run)]
    _write_csv(
        monthly_path,
        ["rule_version", "month", "ending_nav", "return_pct"],
        monthly_rows,
    )
    comparisons = {run.rule_version: run.summary for run in runs}
    _write_json(metrics_path, {"schema_version": "1.0", "comparisons": comparisons})
    benchmark_start = model["benchmark"][model["sessions"][0]]
    benchmark_end = model["benchmark"][model["sessions"][-1]]
    result_name = (
        "replay-result-2025-2026.json"
        if model["manifest"]["evaluation_kind"] == "RETROSPECTIVE_STRESS_TEST"
        else "replay-result-v04-forward-holdout.json"
    )
    result_path = output_dir / result_name
    result = {
        "schema_version": "1.0", "status": "COMPLETED", "rule_version": "v0.4",
        "evaluation_kind": model["manifest"]["evaluation_kind"],
        "holdout_claimed": model["manifest"]["holdout_claimed"],
        "generated_at_jst": datetime.now(tz=JST).isoformat(timespec="seconds"),
        "period": {"from": model["period_from"].isoformat(), "through": model["period_through"].isoformat()},
        "point_in_time_manifest_path": _private_relative(root, point_manifest_path),
        "point_in_time_manifest_sha256": _sha256(point_manifest_path),
        "quality": {
            "missing_hard_gate_inputs": 0,
            "lookahead_violations": 0,
            "required_disclosure_reviews": quality["required_disclosure_reviews"],
            "completed_disclosure_reviews": quality["completed_disclosure_reviews"],
        },
        "metrics": {
            "trade_count": comparisons["v0.4"]["trade_count"],
            "total_return_pct": comparisons["v0.4"]["return_pct"],
            "max_drawdown_pct": comparisons["v0.4"]["maximum_drawdown_pct"],
            "benchmark_return_pct": round((benchmark_end / benchmark_start - 1) * 100, 6),
        },
        "comparisons": comparisons,
        "artifacts": [
            {"role": "trade_log", "path": _private_relative(root, trade_path), "sha256": _sha256(trade_path)},
            {"role": "metrics", "path": _private_relative(root, metrics_path), "sha256": _sha256(metrics_path)},
            {"role": "monthly_returns", "path": _private_relative(root, monthly_path), "sha256": _sha256(monthly_path)},
            {"role": "daily_metrics", "path": _private_relative(root, daily_path), "sha256": _sha256(daily_path)},
        ],
    }
    if model["manifest"]["evaluation_kind"] == "FORWARD_HOLDOUT":
        plan = model["holdout_plan"] or {}
        criteria = plan.get("acceptance_criteria", {})
        v04 = comparisons["v0.4"]
        observations = {
            "monthly_evaluation_count": quality["monthly_evaluation_count"],
            "trade_count": v04["trade_count"],
        }
        failures: list[str] = []
        if observations["monthly_evaluation_count"] < criteria.get(
            "minimum_monthly_evaluation_count", math.inf
        ):
            failures.append("minimum_monthly_evaluation_count")
        if observations["trade_count"] < criteria.get("minimum_trade_count", math.inf):
            failures.append("minimum_trade_count")
        for metric_name, criterion_name in (
            ("maximum_drawdown_pct", "maximum_drawdown_floor_pct"),
            ("maximum_single_name_loss_contribution_pct", "maximum_single_name_loss_floor_pct"),
            ("maximum_industry_loss_contribution_pct", "maximum_industry_loss_floor_pct"),
        ):
            if v04[metric_name] < criteria.get(criterion_name, math.inf):
                failures.append(criterion_name)
        result["holdout"] = {
            **model["manifest"].get("holdout", {}),
            "plan_path": (
                Path("operations/private")
                / str(model["manifest"].get("holdout", {}).get("plan_path", ""))
            ).as_posix(),
            "predeclared": True,
            "retuning_count": model["manifest"].get("holdout", {}).get("retuning_count"),
            "plan_frozen_at_jst": plan.get("frozen_at_jst"),
            "rule_frozen_at_jst": plan.get("rule_frozen_at_jst"),
            "acceptance_criteria": criteria,
            "observations": observations,
            "criteria_met": not failures,
            "criterion_failures": failures,
        }
    _write_json(result_path, result)
    return {"point_in_time_manifest": point_manifest_path, "replay_result": result_path, "result": result}


def initialize_input_bundle(
    *, root: Path, destination: Path, evaluation_kind: str,
    period_from: date, period_through: date, initial_capital: float,
) -> Path:
    """Create a non-overwriting private input skeleton for manual official data."""

    if period_from > period_through:
        raise ReplayInputError(["period.from cannot be after period.through"])
    if not math.isfinite(initial_capital) or initial_capital <= 0:
        raise ReplayInputError(["initial capital must be finite and positive"])
    private = _private_root(root)
    destination = destination.resolve()
    try:
        destination.relative_to(private)
    except ValueError as error:
        raise ReplayInputError(["input destination must stay under operations/private"]) from error
    if destination.exists() and any(destination.iterdir()):
        raise ReplayInputError([f"input destination is not empty: {destination}"])
    destination.mkdir(parents=True, exist_ok=True)
    contracts = {
        "source_register": (
            "source-register.csv",
            ["source_id", "provider", "title", "published_at_jst", "url", "official",
             "replay_authorized", "content_sha256"],
        ),
        "security_master": (
            "security-master.csv",
            ["issue_id", "code", "name", "sector", "market", "effective_from",
             "effective_through", "domestic_common_stock", "event_type", "source_id",
             "known_at_jst"],
        ),
        "trading_calendar": (
            "trading-calendar.csv",
            ["date", "is_trading_day", "source_id", "known_at_jst"],
        ),
        "daily_prices": (
            "daily-prices.csv",
            ["date", "issue_id", "open", "high", "low", "close", "volume", "turnover",
             "status", "source_id", "available_at_jst"],
        ),
        "corporate_actions": (
            "corporate-actions.csv",
            ["effective_date", "issue_id", "action_type", "ratio", "successor_issue_id",
             "cash_consideration", "fractional_cash_price", "source_id",
             "announced_at_jst"],
        ),
        "review_events": (
            "review-events.csv",
            ["event_id", "issue_id", "event_date", "review_due_date", "review_type",
             "source_id", "available_at_jst"],
        ),
        "market_regime": (
            "market-regime.csv",
            ["evaluation_date", "state", "entry_multiplier", "source_ids", "available_at_jst"],
        ),
        "benchmark": (
            "benchmark.csv", ["date", "close", "source_id", "available_at_jst"],
        ),
    }
    datasets: list[dict[str, Any]] = []
    for role, (filename, fields) in contracts.items():
        path = destination / filename
        _write_csv(path, fields, [])
        datasets.append(
            {"role": role, "path": path.relative_to(private).as_posix(),
             "sha256": _sha256(path), "provider": "REPLACE_WITH_OFFICIAL_PROVIDER",
             "official": False, "replay_authorized": False}
        )
    assessments = destination / "assessments.jsonl"
    assessments.write_text("", encoding="utf-8")
    datasets.append(
        {"role": "assessments", "path": assessments.relative_to(private).as_posix(),
         "sha256": _sha256(assessments), "provider": "DERIVED_FROM_OFFICIAL_PRIMARY_SOURCES",
         "official": False, "replay_authorized": False}
    )
    manifest: dict[str, Any] = {
        "schema_version": "1.0", "status": "DRAFT",
        "generated_at_jst": datetime.now(tz=JST).isoformat(timespec="seconds"),
        "evaluation_kind": evaluation_kind,
        "holdout_claimed": evaluation_kind == "FORWARD_HOLDOUT",
        "period": {"from": period_from.isoformat(), "through": period_through.isoformat()},
        "initial_capital": initial_capital, "fee_rate": 0.0015,
        "slippage_rate": 0.001, "board_lot": BOARD_LOT_DEFAULT,
        "price_basis": "AS_TRADED_UNADJUSTED",
        "certifications": {
            "point_in_time_security_master": False, "includes_delisted": False,
            "includes_mergers": False, "includes_corporate_actions": False,
            "includes_all_material_disclosures": False,
            "source_rights_reviewed": False, "jquants_excluded": True,
            "unofficial_prices_excluded": True,
        },
        "datasets": datasets,
    }
    if evaluation_kind == "FORWARD_HOLDOUT":
        manifest["holdout"] = {
            "plan_path": "evidence/v04-holdout-plan.json",
            "plan_sha256": "{{V04_HOLDOUT_PLAN_SHA256}}",
            "retuning_count": 0,
        }
    manifest_path = destination.parent / "input-manifest.json"
    if manifest_path.exists():
        raise ReplayInputError([f"input manifest already exists: {manifest_path}"])
    _write_json(manifest_path, manifest)
    return manifest_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--manifest", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize = subparsers.add_parser(
        "init", help="create a private non-overwriting input skeleton"
    )
    initialize.add_argument(
        "--kind", choices=("RETROSPECTIVE_STRESS_TEST", "FORWARD_HOLDOUT"), required=True
    )
    initialize.add_argument("--from", dest="period_from", type=date.fromisoformat, required=True)
    initialize.add_argument("--through", dest="period_through", type=date.fromisoformat, required=True)
    initialize.add_argument("--initial-capital", type=float, default=10_000_000)
    initialize.add_argument("--destination", type=Path)
    subparsers.add_parser("validate", help="validate all point-in-time inputs without writing")
    run = subparsers.add_parser("run", help="write hash-bound replay outputs")
    run.add_argument("--output-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = args.root.resolve()
    private = _private_root(root)
    manifest = args.manifest or (
        private / "historical-replay/2025-2026/input-manifest.json"
    )
    try:
        if args.command == "init":
            destination = args.destination or (
                private / "historical-replay"
                / ("2025-2026/input" if args.kind == "RETROSPECTIVE_STRESS_TEST" else "v04-forward/input")
            )
            initialized = initialize_input_bundle(
                root=root, destination=destination, evaluation_kind=args.kind,
                period_from=args.period_from, period_through=args.period_through,
                initial_capital=args.initial_capital,
            )
            result = {"initialized": True, "manifest": _private_relative(root, initialized)}
        elif args.command == "validate":
            model, quality = validate_inputs(root=root, manifest_path=manifest)
            result = {"valid": True, "quality": quality, "period": {
                "from": model["period_from"].isoformat(), "through": model["period_through"].isoformat()}}
        else:
            output = args.output_dir or (manifest.resolve().parent / "output")
            written = write_replay(root=root, manifest_path=manifest, output_dir=output)
            result = {
                "valid": True,
                "point_in_time_manifest": _private_relative(root, written["point_in_time_manifest"]),
                "replay_result": _private_relative(root, written["replay_result"]),
                "metrics": written["result"]["comparisons"],
            }
    except ReplayInputError as error:
        print(json.dumps({"valid": False, "blockers": error.blockers}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
