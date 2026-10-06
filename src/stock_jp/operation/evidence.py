"""Fail-closed validators for evidence used by LIVE promotion gates."""

from __future__ import annotations
from stock_jp.workspace import project_root, record_path

from stock_jp.market_data.paths import resolve_archive_root, MarketDataStorageError

import argparse
import csv
from datetime import date, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import subprocess
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from stock_jp.operation.integrity import RunIntegrityError, validate_run_artifacts

from stock_jp.operation.policy import START_CONDITIONS_VERSION, validate_policy


PROJECT_ROOT = project_root()
JST = ZoneInfo("Asia/Tokyo")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
POINT_IN_TIME_GATE = "point_in_time_full_universe_validation"
HISTORICAL_REPLAY_GATE = "historical_replay_2025_2026_accepted"
PAPER_DURATION_GATE = "minimum_12_month_paper_trade"
SHADOW_RUN_GATE = "twenty_day_shadow_run"
OFFICIAL_COVERAGE_GATE = "official_source_coverage"
REPOSITORY_RECOVERY_GATE = "private_repository_recovery"
from stock_jp.workspace import LAYOUT_REVISION
REPOSITORY_LAYOUT_REVISION = LAYOUT_REVISION
PERSONAL_RISK_GATE = "personal_risk_and_broker_check"
V04_PROMOTION_GATE = "v04_holdout_promotion"
LIVE_PROMOTION_GATE = "live_promotion"
LIVE_REQUIREMENT_EVIDENCE = {
    POINT_IN_TIME_GATE: 'operations/evidence/point-in-time.json',
    HISTORICAL_REPLAY_GATE: 'operations/evidence/historical-replay.json',
    OFFICIAL_COVERAGE_GATE: 'operations/evidence/official-coverage.json',
    PERSONAL_RISK_GATE: 'operations/evidence/personal-risk.json',
}
POLICY_LIVE_GATES = tuple(
    name for name in LIVE_REQUIREMENT_EVIDENCE if name != V04_PROMOTION_GATE
)
OFFICIAL_SOURCE_NAMES = ("tdnet", "edinet", "company_ir", "jpx")
OFFICIAL_SOURCE_ALIASES = {
    "tdnet": "tdnet",
    "edinet": "edinet",
    "company_ir": "company_ir",
    "ir": "company_ir",
    "jpx": "jpx",
    "jpx_notice": "jpx",
}


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _directory_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = child.relative_to(path).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        content = child.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _safe_project_path(root: Path, relative: Any) -> Path | None:
    if not isinstance(relative, str) or not relative.strip():
        return None
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None
    candidate = (root / relative_path).resolve()
    allowed_roots = (root.resolve(),)
    for allowed in allowed_roots:
        try:
            candidate.relative_to(allowed)
        except ValueError:
            continue
        return candidate
    return None


def _path_is_under_evidence_roots(root: Path, path: Path) -> bool:
    candidate = path.resolve()
    for allowed in (root.resolve(),):
        try:
            candidate.relative_to(allowed)
        except ValueError:
            continue
        return True
    return False


def _aware_datetime(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _parse_aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("datetime must include a UTC offset")
    return parsed.astimezone(JST)


def _iso_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _result(
    *,
    gate: str,
    blockers: list[str],
    metrics: dict[str, Any],
    inputs: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "gate": gate,
        "evaluated_at_jst": datetime.now(tz=JST).isoformat(timespec="seconds"),
        "eligible": not blockers,
        "blockers": blockers,
        "metrics": metrics,
        "inputs": inputs,
    }


def evaluate_point_in_time(
    *, root: Path = PROJECT_ROOT, manifest_path: Path | None = None
) -> dict[str, Any]:
    """Validate a point-in-time full-universe replay manifest and its artifacts."""
    root = root.resolve()
    manifest_path = manifest_path or (root / "research/historical-replay-2025-2026/output/point-in-time-validation.json")
    if not _path_is_under_evidence_roots(root, manifest_path):
        return _result(
            gate=POINT_IN_TIME_GATE,
            blockers=['manifest path must stay under project or operations'],
            metrics={},
            inputs=[],
        )
    if not manifest_path.is_file():
        return _result(
            gate=POINT_IN_TIME_GATE,
            blockers=[f"manifest is missing: {manifest_path}"],
            metrics={},
            inputs=[],
        )

    try:
        manifest = _read_object(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return _result(
            gate=POINT_IN_TIME_GATE,
            blockers=[f"manifest is invalid: {error}"],
            metrics={},
            inputs=[],
        )

    blockers: list[str] = []
    if manifest.get("schema_version") != "1.0":
        blockers.append("schema_version must be 1.0")
    if manifest.get("status") != "COMPLETED":
        blockers.append("status must be COMPLETED")
    if not _iso_date(manifest.get("as_of_date")):
        blockers.append("as_of_date must be an ISO date")
    if not _aware_datetime(manifest.get("generated_at_jst")):
        blockers.append("generated_at_jst must include a UTC offset")
    elif _parse_aware_datetime(manifest["generated_at_jst"]) > datetime.now(tz=JST):
        blockers.append("generated_at_jst cannot be in the future")
    if _iso_date(manifest.get("as_of_date")) and date.fromisoformat(
        manifest["as_of_date"]
    ) > datetime.now(tz=JST).date():
        blockers.append("as_of_date cannot be in the future")

    universe = manifest.get("universe")
    if not isinstance(universe, dict):
        universe = {}
        blockers.append("universe must be an object")
    required_count = universe.get("required_count")
    evaluated_count = universe.get("evaluated_count")
    if not isinstance(required_count, int) or required_count <= 0:
        blockers.append("universe.required_count must be a positive integer")
    if not isinstance(evaluated_count, int) or evaluated_count != required_count:
        blockers.append("universe.evaluated_count must equal required_count")
    for field in (
        "point_in_time_security_master",
        "includes_delisted",
        "includes_mergers",
        "includes_corporate_actions",
    ):
        if universe.get(field) is not True:
            blockers.append(f"universe.{field} must be true")

    quality = manifest.get("quality")
    if not isinstance(quality, dict):
        quality = {}
        blockers.append("quality must be an object")
    for field in ("missing_hard_gate_inputs", "lookahead_violations"):
        if quality.get(field) != 0:
            blockers.append(f"quality.{field} must be 0")
    required_reviews = quality.get("required_disclosure_reviews")
    completed_reviews = quality.get("completed_disclosure_reviews")
    if (
        not isinstance(required_reviews, int)
        or isinstance(required_reviews, bool)
        or required_reviews < 0
    ):
        blockers.append("quality.required_disclosure_reviews must be a non-negative integer")
    if (
        not isinstance(completed_reviews, int)
        or isinstance(completed_reviews, bool)
        or completed_reviews != required_reviews
    ):
        blockers.append(
            "quality.completed_disclosure_reviews must equal required_disclosure_reviews"
        )

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        artifacts = []
        blockers.append("artifacts must be a non-empty array")
    roles: set[str] = set()
    checked_inputs: list[dict[str, str]] = []
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            blockers.append(f"artifacts[{index}] must be an object")
            continue
        role = artifact.get("role")
        relative = artifact.get("path")
        expected_hash = artifact.get("sha256")
        if not isinstance(role, str) or not role:
            blockers.append(f"artifacts[{index}].role is required")
        else:
            roles.add(role)
        path = _safe_project_path(root, relative)
        if path is None:
            blockers.append(f"artifacts[{index}].path must stay under project root")
            continue
        if not path.is_file():
            blockers.append(f"artifact is missing: {relative}")
            continue
        if not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(
            expected_hash
        ):
            blockers.append(f"artifacts[{index}].sha256 is invalid")
            continue
        actual_hash = _sha256(path)
        if actual_hash != expected_hash:
            blockers.append(f"artifact hash mismatch: {relative}")
            continue
        checked_inputs.append(
            {"role": str(role), "path": str(relative), "sha256": actual_hash}
        )
    if "source_snapshot" not in roles:
        blockers.append("artifact role is missing: source_snapshot")
    if not roles.intersection({"universe_validation", "trade_log"}):
        blockers.append("artifact role is missing: universe_validation")
    if not roles.intersection({"quality_report", "metrics"}):
        blockers.append("artifact role is missing: quality_report")

    inputs = [
        {
            "role": "manifest",
            "path": os.path.relpath(manifest_path.resolve(), root),
            "sha256": _sha256(manifest_path),
        },
        *checked_inputs,
    ]
    metrics = {
        "required_count": required_count,
        "evaluated_count": evaluated_count,
        "missing_hard_gate_inputs": quality.get("missing_hard_gate_inputs"),
        "lookahead_violations": quality.get("lookahead_violations"),
        "required_disclosure_reviews": required_reviews,
        "completed_disclosure_reviews": completed_reviews,
        "verified_artifact_count": len(checked_inputs),
    }
    return _result(
        gate=POINT_IN_TIME_GATE,
        blockers=blockers,
        metrics=metrics,
        inputs=inputs,
    )


def evaluate_historical_replay(
    *,
    root: Path = PROJECT_ROOT,
    result_path: Path | None = None,
    review_path: Path | None = None,
) -> dict[str, Any]:
    """Validate the fixed 2025-2026 replay and a private human acceptance."""
    root = root.resolve()
    result_path = result_path or (root / "research/historical-replay-2025-2026/output/replay-result-2025-2026.json")
    review_path = review_path or (
        root / 'operations/evidence/historical-replay-review.json'
    )
    blockers: list[str] = []
    inputs: list[dict[str, str]] = []

    if not _path_is_under_evidence_roots(root, result_path):
        blockers.append('replay result path must stay under project or operations')
    if not result_path.is_file():
        blockers.append(f"replay result is missing: {result_path}")
        result: dict[str, Any] = {}
    else:
        try:
            result = _read_object(result_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            blockers.append(f"replay result is invalid: {error}")
            result = {}
        else:
            inputs.append(
                {
                    "role": "replay_result",
                    "path": os.path.relpath(result_path.resolve(), root),
                    "sha256": _sha256(result_path),
                }
            )

    private_root = (root / 'operations').resolve()
    try:
        review_path.resolve().relative_to(private_root)
    except ValueError:
        blockers.append('review path must stay under operations')
    if not review_path.is_file():
        blockers.append(f"private replay review is missing: {review_path}")
        review: dict[str, Any] = {}
    else:
        try:
            review = _read_object(review_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            blockers.append(f'private replay review is invalid: {error}')
            review = {}
        else:
            inputs.append({'role': 'private_review', 'path': 'operations/evidence/historical-replay-review.json', 'sha256': _sha256(review_path)})

    if result:
        if result.get("schema_version") != "1.0":
            blockers.append("replay result schema_version must be 1.0")
        if result.get("status") != "COMPLETED":
            blockers.append("replay result status must be COMPLETED")
        if result.get("rule_version") != "v0.4":
            blockers.append("replay result rule_version must be v0.4")
        if result.get("evaluation_kind") != "RETROSPECTIVE_STRESS_TEST":
            blockers.append("2025-2026 replay must be a RETROSPECTIVE_STRESS_TEST")
        if result.get("holdout_claimed") is not False:
            blockers.append("2025-2026 retrospective replay cannot claim holdout status")
        period = result.get("period")
        if not isinstance(period, dict):
            period = {}
            blockers.append("replay result period must be an object")
        if period.get("from") != "2025-01-01":
            blockers.append("replay period.from must be 2025-01-01")
        if period.get("through") != "2026-08-31":
            blockers.append("replay period.through must be 2026-08-31")
        if not _aware_datetime(result.get("generated_at_jst")):
            blockers.append("replay generated_at_jst must include a UTC offset")
            replay_generated_at = None
        else:
            replay_generated_at = _parse_aware_datetime(result["generated_at_jst"])
            if replay_generated_at.date() < date(2026, 8, 31):
                blockers.append("replay cannot be generated before period.through")
            if replay_generated_at > datetime.now(tz=JST):
                blockers.append("replay generated_at_jst cannot be in the future")
        quality = result.get("quality")
        if not isinstance(quality, dict):
            quality = {}
            blockers.append("replay quality must be an object")
        for field in ("missing_hard_gate_inputs", "lookahead_violations"):
            if quality.get(field) != 0:
                blockers.append(f"replay quality.{field} must be 0")
        required_reviews = quality.get("required_disclosure_reviews")
        completed_reviews = quality.get("completed_disclosure_reviews")
        if (
            not isinstance(required_reviews, int)
            or isinstance(required_reviews, bool)
            or required_reviews < 0
            or not isinstance(completed_reviews, int)
            or isinstance(completed_reviews, bool)
            or completed_reviews != required_reviews
        ):
            blockers.append("replay disclosure review coverage must be complete")
        metrics = result.get("metrics")
        if not isinstance(metrics, dict):
            metrics = {}
            blockers.append("replay metrics must be an object")
        for field in (
            "trade_count",
            "total_return_pct",
            "max_drawdown_pct",
            "benchmark_return_pct",
        ):
            value = metrics.get(field)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                blockers.append(f"replay metrics.{field} must be numeric")
        if not isinstance(metrics.get("trade_count"), int) or metrics.get(
            "trade_count", 0
        ) <= 0:
            blockers.append("replay metrics.trade_count must be a positive integer")
        comparisons = result.get("comparisons")
        if not isinstance(comparisons, dict):
            comparisons = {}
            blockers.append("replay comparisons must be an object")
        comparison_fields = (
            "return_pct",
            "maximum_drawdown_pct",
            "maximum_single_name_loss_contribution_pct",
            "maximum_industry_loss_contribution_pct",
            "trade_count",
        )
        for version in ("v0.2", "v0.4"):
            version_metrics = comparisons.get(version)
            if not isinstance(version_metrics, dict):
                version_metrics = {}
                blockers.append(f"replay comparisons.{version} must be an object")
            for name in comparison_fields:
                value = version_metrics.get(name)
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                ):
                    blockers.append(f"replay comparisons.{version}.{name} must be numeric")
        v04_metrics = comparisons.get("v0.4", {})
        if isinstance(v04_metrics, dict):
            for summary_name, comparison_name in (
                ("trade_count", "trade_count"),
                ("total_return_pct", "return_pct"),
                ("max_drawdown_pct", "maximum_drawdown_pct"),
            ):
                if metrics.get(summary_name) != v04_metrics.get(comparison_name):
                    blockers.append(
                        f"replay metrics.{summary_name} does not match comparisons.v0.4"
                    )

        point_relative = result.get("point_in_time_manifest_path")
        point_manifest = _safe_project_path(root, point_relative)
        if point_manifest is None:
            point_manifest = (root / "research/historical-replay-2025-2026/output/point-in-time-validation.json")
        point_evaluation = evaluate_point_in_time(
            root=root, manifest_path=point_manifest
        )
        if not point_evaluation["eligible"]:
            blockers.append("point-in-time full-universe validation is not eligible")
        expected_point_hash = result.get("point_in_time_manifest_sha256")
        if not point_manifest.is_file() or expected_point_hash != _sha256(
            point_manifest
        ):
            blockers.append("point-in-time manifest hash does not match replay result")

        artifacts = result.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            artifacts = []
            blockers.append("replay artifacts must be a non-empty array")
        roles: set[str] = set()
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, dict):
                blockers.append(f"replay artifacts[{index}] must be an object")
                continue
            role = artifact.get("role")
            relative = artifact.get("path")
            expected_hash = artifact.get("sha256")
            if isinstance(role, str):
                roles.add(role)
            path = _safe_project_path(root, relative)
            if path is None or not path.is_file():
                blockers.append(f"replay artifact is missing or unsafe: {relative}")
            elif not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(
                expected_hash
            ):
                blockers.append(f"replay artifacts[{index}].sha256 is invalid")
            elif _sha256(path) != expected_hash:
                blockers.append(f"replay artifact hash mismatch: {relative}")
            else:
                inputs.append(
                    {"role": str(role), "path": str(relative), "sha256": expected_hash}
                )
        for role in ("trade_log", "metrics", "monthly_returns"):
            if role not in roles:
                blockers.append(f"replay artifact role is missing: {role}")
    else:
        metrics = {}
        comparisons = {}
        replay_generated_at = None

    if review:
        if review.get("schema_version") != "1.1":
            blockers.append("private review schema_version must be 1.1")
        if review.get("decision") != "ACCEPT":
            blockers.append("private review decision must be ACCEPT")
        if review.get("rule_version") != "v0.4":
            blockers.append("private review rule_version must be v0.4")
        if not isinstance(review.get("accepted_by"), str) or not review.get(
            "accepted_by", ""
        ).strip():
            blockers.append("private review accepted_by is required")
        if not _aware_datetime(review.get("accepted_at_jst")):
            blockers.append("private review accepted_at_jst must include a UTC offset")
        else:
            accepted_at = _parse_aware_datetime(review["accepted_at_jst"])
            if accepted_at > datetime.now(tz=JST):
                blockers.append("private review accepted_at_jst cannot be in the future")
            if replay_generated_at and accepted_at < replay_generated_at:
                blockers.append("private review cannot precede replay generation")
        if result_path.is_file() and review.get("replay_result_sha256") != _sha256(
            result_path
        ):
            blockers.append("private review does not bind the current replay result")
        for field in (
            "drawdown_reviewed",
            "concentration_loss_reviewed",
            "data_limitations_reviewed",
        ):
            if review.get(field) is not True:
                blockers.append(f"private review {field} must be true")
        accepted_metrics = review.get("accepted_v04_metrics")
        if not isinstance(accepted_metrics, dict):
            accepted_metrics = {}
            blockers.append("private review accepted_v04_metrics must be an object")
        v04_metrics = comparisons.get("v0.4", {})
        for name in (
            "return_pct",
            "maximum_drawdown_pct",
            "maximum_single_name_loss_contribution_pct",
            "maximum_industry_loss_contribution_pct",
            "trade_count",
        ):
            if not isinstance(v04_metrics, dict) or accepted_metrics.get(name) != v04_metrics.get(name):
                blockers.append(f"private review accepted metric does not match replay: {name}")

    return _result(
        gate=HISTORICAL_REPLAY_GATE,
        blockers=blockers,
        metrics={
            "period_from": result.get("period", {}).get("from")
            if isinstance(result.get("period"), dict)
            else None,
            "period_through": result.get("period", {}).get("through")
            if isinstance(result.get("period"), dict)
            else None,
            "trade_count": metrics.get("trade_count"),
            "max_drawdown_pct": metrics.get("max_drawdown_pct"),
            "human_accepted": review.get("decision") == "ACCEPT",
        },
        inputs=inputs,
    )


def evaluate_paper_duration(
    *,
    root: Path = PROJECT_ROOT,
    history_path: Path | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Require at least 365 elapsed days of recorded v0.4 PAPER operation."""
    root = root.resolve()
    private_root = (root / 'operations').resolve()
    reference_time = (at or datetime.now(tz=JST)).astimezone(JST)
    history_path = history_path or (record_path(private_root.parent, 'run-history.csv'))
    blockers: list[str] = []
    inputs: list[dict[str, str]] = []
    try:
        history_path.resolve().relative_to(private_root)
    except ValueError:
        blockers.append('run history path must stay under operations')
    if not history_path.is_file():
        return _result(
            gate=PAPER_DURATION_GATE,
            blockers=[*blockers, f"run history is missing: {history_path}"],
            metrics={},
            inputs=[],
        )

    try:
        with history_path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            rows = list(reader)
            fields = set(reader.fieldnames or [])
    except (OSError, csv.Error) as error:
        return _result(
            gate=PAPER_DURATION_GATE,
            blockers=[*blockers, f"run history is invalid: {error}"],
            metrics={},
            inputs=[],
        )
    required_fields = {
        "run_id",
        "attempt",
        "completed_at_jst",
        "status",
        "operation_mode",
        "active_rule_version",
        "price_date",
        "report_path",
    }
    missing_fields = required_fields - fields
    if missing_fields:
        blockers.append(
            "run history fields are missing: " + ", ".join(sorted(missing_fields))
        )

    latest_by_run: dict[str, tuple[int, dict[str, str]]] = {}
    for index, row in enumerate(rows, start=2):
        run_id = row.get("run_id", "").strip()
        try:
            attempt = int(row.get("attempt", ""))
        except ValueError:
            blockers.append(f"run history row {index} has an invalid attempt")
            continue
        if not run_id:
            blockers.append(f"run history row {index} has no run_id")
            continue
        previous = latest_by_run.get(run_id)
        if previous is None or attempt > previous[0]:
            latest_by_run[run_id] = (attempt, row)

    latest_rows = [value[1] for value in latest_by_run.values()]
    if any(row.get("operation_mode", "").strip() == "LIVE" for row in latest_rows):
        blockers.append("pre-promotion LIVE run exists in run history")

    successful: list[tuple[datetime, date, str]] = []
    for row in latest_rows:
        if not (
            row.get("status", "").strip() == "COMPLETED"
            and row.get("operation_mode", "").strip() == "PAPER"
            and row.get("active_rule_version", "").strip() == "v0.4"
        ):
            continue
        run_id = row.get("run_id", "").strip()
        try:
            completed = _parse_aware_datetime(row.get("completed_at_jst", ""))
            price_date = date.fromisoformat(row.get("price_date", ""))
        except ValueError:
            blockers.append(f"completed v0.4 PAPER run has invalid dates: {run_id}")
            continue
        if run_id != price_date.isoformat():
            blockers.append(f"completed PAPER run_id must equal price_date: {run_id}")
            continue
        if completed > reference_time or price_date > reference_time.date():
            blockers.append(f"completed PAPER run cannot be in the future: {run_id}")
            continue
        report_value = row.get("report_path", "").strip()
        if not report_value:
            blockers.append(f"completed run report path is unsafe: {run_id}")
            continue
        report = (root / report_value).resolve()
        try:
            report.relative_to(private_root)
        except ValueError:
            blockers.append(f"completed run report is outside private storage: {run_id}")
            continue
        expected_report = (record_path(private_root.parent, 'runs') / run_id / 'report.md').resolve()
        if report != expected_report:
            blockers.append(f"completed run report path does not match run_id: {run_id}")
            continue
        if not report.is_file():
            blockers.append(f"completed run report is missing: {run_id}")
            continue
        successful.append((completed, price_date, run_id))

    successful.sort()
    if successful:
        first_completed = successful[0][0]
        last_completed = successful[-1][0]
        elapsed_days = (last_completed.date() - first_completed.date()).days
        months = sorted({completed.strftime("%Y-%m") for completed, _, _ in successful})
        if elapsed_days < 365:
            blockers.append("v0.4 PAPER elapsed duration is less than 365 days")
        if len(months) < 12:
            blockers.append("v0.4 PAPER does not contain successful runs in 12 months")
        cursor = date(first_completed.year, first_completed.month, 1)
        final_month = date(last_completed.year, last_completed.month, 1)
        expected_months: list[str] = []
        while cursor <= final_month:
            expected_months.append(cursor.strftime("%Y-%m"))
            if cursor.month == 12:
                cursor = date(cursor.year + 1, 1, 1)
            else:
                cursor = date(cursor.year, cursor.month + 1, 1)
        empty_months = sorted(set(expected_months) - set(months))
        if empty_months:
            blockers.append(
                "v0.4 PAPER has calendar months without a successful run: "
                + ", ".join(empty_months)
            )
    else:
        first_completed = None
        last_completed = None
        elapsed_days = 0
        months = []
        blockers.append("no completed v0.4 PAPER runs were found")

    inputs.append(
        {
            "role": "run_history",
            "path": 'operations/run-history.csv',
            "sha256": _sha256(history_path),
        }
    )
    return _result(
        gate=PAPER_DURATION_GATE,
        blockers=blockers,
        metrics={
            "first_completed_at_jst": first_completed.isoformat(timespec="seconds")
            if first_completed
            else None,
            "last_completed_at_jst": last_completed.isoformat(timespec="seconds")
            if last_completed
            else None,
            "elapsed_days": elapsed_days,
            "successful_run_count": len(successful),
            "calendar_month_count": len(months),
        },
        inputs=inputs,
    )


def evaluate_shadow_run(
    *, root: Path = PROJECT_ROOT, history_path: Path | None = None
) -> dict[str, Any]:
    """Validate the latest 20 tracked trading sessions as real v0.4 PAPER runs."""
    root = root.resolve()
    private_root = (root / 'operations').resolve()
    history_path = history_path or (record_path(private_root.parent, 'run-history.csv'))
    blockers: list[str] = []
    inputs: list[dict[str, str]] = []
    if not history_path.is_file():
        return _result(
            gate=SHADOW_RUN_GATE,
            blockers=[f"run history is missing: {history_path}"],
            metrics={},
            inputs=[],
        )
    try:
        history_path.resolve().relative_to(private_root)
    except ValueError:
        blockers.append('run history path must stay under operations')
    try:
        with history_path.open(encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))
    except (OSError, csv.Error) as error:
        return _result(
            gate=SHADOW_RUN_GATE,
            blockers=[*blockers, f"run history is invalid: {error}"],
            metrics={},
            inputs=[],
        )

    latest_by_run: dict[str, tuple[int, dict[str, str]]] = {}
    for index, row in enumerate(rows, start=2):
        run_id = row.get("run_id", "").strip()
        try:
            attempt = int(row.get("attempt", ""))
        except ValueError:
            blockers.append(f"run history row {index} has an invalid attempt")
            continue
        if not run_id:
            blockers.append(f"run history row {index} has no run_id")
            continue
        previous = latest_by_run.get(run_id)
        if previous is None or attempt > previous[0]:
            latest_by_run[run_id] = (attempt, row)

    candidates: list[tuple[date, dict[str, str]]] = []
    for _, row in latest_by_run.values():
        if not (
            row.get("status", "").strip() == "COMPLETED"
            and row.get("operation_mode", "").strip() == "PAPER"
            and row.get("active_rule_version", "").strip() == "v0.4"
        ):
            continue
        try:
            price_date = date.fromisoformat(row.get("price_date", ""))
            _parse_aware_datetime(row.get("completed_at_jst", ""))
            _parse_aware_datetime(row.get("source_cutoff_jst", ""))
        except ValueError:
            blockers.append(
                f"completed shadow run has invalid timestamps: {row.get('run_id', '')}"
            )
            continue
        candidates.append((price_date, row))
    candidates.sort(key=lambda item: (item[0], item[1].get("run_id", "")))
    selected = candidates[-20:]
    selected_dates = [item[0] for item in selected]
    if len(selected) < 20:
        blockers.append("fewer than 20 completed v0.4 PAPER trading sessions")
    if len(set(selected_dates)) != len(selected_dates):
        blockers.append("shadow runs contain duplicate price dates")

    archive_dates: list[date] = []
    archive_by_date: dict[date, Path] = {}
    try:
        price_files = list(resolve_archive_root(root).glob("????/????-??-??.csv"))
    except MarketDataStorageError as error:
        blockers.append(str(error))
        price_files = []
    for price_file in price_files:
        try:
            archive_date = date.fromisoformat(price_file.stem)
        except ValueError:
            continue
        archive_dates.append(archive_date)
        archive_by_date[archive_date] = price_file
    if selected_dates:
        expected_dates = sorted(
            item for item in archive_dates if item <= selected_dates[-1]
        )[-20:]
        if len(expected_dates) < 20:
            blockers.append("private price archive has fewer than 20 trading sessions")
        elif selected_dates != expected_dates:
            blockers.append(
                "latest shadow runs do not cover 20 consecutive tracked trading sessions"
            )
        if archive_dates and selected_dates[-1] != max(archive_dates):
            blockers.append("shadow window does not end at the latest tracked price session")
    else:
        expected_dates = []

    ticket_ids: set[str] = set()
    order_keys: set[tuple[str, str, str]] = set()
    order_count = 0
    for price_date, row in selected:
        run_id = row.get("run_id", "").strip()
        if run_id != price_date.isoformat():
            blockers.append(f"shadow run_id must equal price_date: {run_id}")
        try:
            alert_count = int(row.get("alert_count", ""))
            data_gap_count = int(row.get("data_gap_count", ""))
            recorded_order_count = int(row.get("order_count", ""))
        except ValueError:
            blockers.append(f"shadow run counts are invalid: {run_id}")
            continue
        if alert_count != 0:
            blockers.append(f"shadow run has alerts: {run_id}")
        if data_gap_count != 0:
            blockers.append(f"shadow run has data gaps: {run_id}")
        try:
            validate_run_artifacts(
                root=root,
                run_id=run_id,
                completed_at=row.get("completed_at_jst", ""),
                source_cutoff=row.get("source_cutoff_jst", ""),
                price_date=price_date.isoformat(),
            )
        except (RunIntegrityError, OSError, ValueError, json.JSONDecodeError) as error:
            blockers.append(f"shadow run integrity failed for {run_id}: {error}")
            continue
        run_dir = record_path(private_root.parent, 'runs') / run_id
        orders_path = run_dir / "orders.csv"
        try:
            with orders_path.open(encoding="utf-8", newline="") as source:
                orders = list(csv.DictReader(source))
        except (OSError, csv.Error) as error:
            blockers.append(f"shadow orders are invalid for {run_id}: {error}")
            continue
        if len(orders) != recorded_order_count:
            blockers.append(f"shadow order_count mismatch: {run_id}")
        order_count += len(orders)
        for order in orders:
            ticket_id = order.get("ticket_id", "").strip()
            key = (
                order.get("code", "").strip(),
                order.get("side", "").strip(),
                order.get("trade_date", "").strip(),
            )
            if ticket_id in ticket_ids:
                blockers.append(f"duplicate ticket_id across shadow runs: {ticket_id}")
            if key in order_keys:
                blockers.append("duplicate code/side/trade_date across shadow runs")
            ticket_ids.add(ticket_id)
            order_keys.add(key)
        inputs.append(
            {
                "role": "run_artifacts",
                "path": f'operations/runs/{run_id}',
                "sha256": _directory_sha256(run_dir),
            }
        )
        price_file = archive_by_date.get(price_date)
        if price_file:
            inputs.append(
                {
                    "role": "price_session",
                    "path": os.path.relpath(price_file, root),
                    "sha256": _sha256(price_file),
                }
            )

    inputs.insert(
        0,
        {
            "role": "run_history",
            "path": 'operations/run-history.csv',
            "sha256": _sha256(history_path),
        },
    )
    return _result(
        gate=SHADOW_RUN_GATE,
        blockers=blockers,
        metrics={
            "consecutive_session_count": len(selected),
            "first_price_date": selected_dates[0].isoformat()
            if selected_dates
            else None,
            "last_price_date": selected_dates[-1].isoformat()
            if selected_dates
            else None,
            "alert_count": sum(
                int(row.get("alert_count", "0"))
                for _, row in selected
                if row.get("alert_count", "").isdigit()
            ),
            "data_gap_count": sum(
                int(row.get("data_gap_count", "0"))
                for _, row in selected
                if row.get("data_gap_count", "").isdigit()
            ),
            "order_count": order_count,
            "duplicate_ticket_count": len(
                [item for item in blockers if item.startswith("duplicate ticket_id")]
            ),
        },
        inputs=inputs,
    )


def evaluate_official_coverage(
    *, root: Path = PROJECT_ROOT, history_path: Path | None = None
) -> dict[str, Any]:
    """Validate official-source evidence for the latest completed PAPER run."""
    root = root.resolve()
    private_root = (root / 'operations').resolve()
    history_path = history_path or (record_path(private_root.parent, 'run-history.csv'))
    blockers: list[str] = []
    if not history_path.is_file():
        return _result(
            gate=OFFICIAL_COVERAGE_GATE,
            blockers=[*blockers, f"run history is missing: {history_path}"],
            metrics={},
            inputs=[],
        )
    with history_path.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    latest: dict[str, tuple[int, dict[str, str]]] = {}
    for row in rows:
        run_id = row.get("run_id", "").strip()
        try:
            attempt = int(row.get("attempt", ""))
        except ValueError:
            continue
        if not run_id:
            continue
        if run_id not in latest or attempt > latest[run_id][0]:
            latest[run_id] = (attempt, row)
    completed = [
        row
        for _, row in latest.values()
        if row.get("status", "").strip() == "COMPLETED"
        and row.get("operation_mode", "").strip() == "PAPER"
        and row.get("active_rule_version", "").strip() == "v0.4"
    ]
    completed.sort(key=lambda row: row.get("price_date", ""))
    selected = completed[-1:]
    if not selected:
        blockers.append("official coverage requires a completed v0.4 PAPER run")

    inputs = [
        {
            "role": "run_history",
            "path": 'operations/run-history.csv',
            "sha256": _sha256(history_path),
        }
    ]
    checked_counts = {name: 0 for name in OFFICIAL_SOURCE_NAMES}
    live_network_count = 0
    latest_cutoffs: dict[str, datetime] = {}
    for row in selected:
        run_id = row.get("run_id", "").strip()
        try:
            price_date = date.fromisoformat(row.get("price_date", ""))
            alert_count = int(row.get("alert_count", ""))
            data_gap_count = int(row.get("data_gap_count", ""))
            _parse_aware_datetime(row.get("completed_at_jst", ""))
            _parse_aware_datetime(row.get("source_cutoff_jst", ""))
        except (TypeError, ValueError):
            blockers.append(f"official coverage run metadata is invalid for {run_id}")
            continue
        if run_id != price_date.isoformat():
            blockers.append(f"official coverage run_id must equal price_date: {run_id}")
        if alert_count != 0:
            blockers.append(f"official coverage run has alerts: {run_id}")
        if data_gap_count != 0:
            blockers.append(f"official coverage run has data gaps: {run_id}")
        try:
            validate_run_artifacts(
                root=root,
                run_id=run_id,
                completed_at=row.get("completed_at_jst", ""),
                source_cutoff=row.get("source_cutoff_jst", ""),
                price_date=price_date.isoformat(),
            )
        except (RunIntegrityError, OSError, ValueError, json.JSONDecodeError) as error:
            blockers.append(f"official coverage run integrity failed for {run_id}: {error}")
        run_dir = record_path(private_root.parent, 'runs') / run_id
        coverage_path = run_dir / "coverage.json"
        health_path = run_dir / "provider-health.json"
        sources_path = run_dir / "sources.csv"
        for path in (coverage_path, health_path, sources_path):
            if not path.is_file():
                blockers.append(f"official coverage artifact is missing: {run_id}/{path.name}")
        if not all(path.is_file() for path in (coverage_path, health_path, sources_path)):
            continue
        try:
            coverage = _read_object(coverage_path)
            health = _read_object(health_path)
            with sources_path.open(encoding="utf-8", newline="") as source:
                source_rows = list(csv.DictReader(source))
        except (OSError, ValueError, json.JSONDecodeError, csv.Error) as error:
            blockers.append(f"official coverage artifacts are invalid for {run_id}: {error}")
            continue
        if coverage.get("run_id") != run_id or coverage.get("status") != "COMPLETED":
            blockers.append(f"coverage is not completed for {run_id}")
        if coverage.get("data_gaps") not in ([], None):
            unresolved = [
                gap
                for gap in coverage.get("data_gaps", [])
                if not isinstance(gap, dict)
                or str(gap.get("status", "OPEN")).upper() != "RESOLVED"
            ]
            if unresolved:
                blockers.append(f"coverage has unresolved data gaps for {run_id}")
        if health.get("run_id") != run_id:
            blockers.append(f"provider-health run_id mismatch for {run_id}")
        if health.get("input_mode") != "LIVE_NETWORK":
            blockers.append(f"provider-health is not a live network scan for {run_id}")
        else:
            live_network_count += 1
        if health.get("status") not in {"COMPLETED", "PARTIAL"}:
            blockers.append(f"provider-health has not completed for {run_id}")
        edinet_provider = health.get("providers", {}).get("edinet", {})
        if edinet_provider.get("status") != "OK" or not isinstance(
            edinet_provider.get("request_count"), int
        ) or edinet_provider.get("request_count", 0) < 1:
            blockers.append(f"EDINET network scan is incomplete for {run_id}")

        by_category: dict[str, list[dict[str, str]]] = {
            name: [] for name in OFFICIAL_SOURCE_NAMES
        }
        for source_row in source_rows:
            category = source_row.get("category", "").strip().lower().replace("-", "_")
            normalized = OFFICIAL_SOURCE_ALIASES.get(category, category)
            if normalized in by_category:
                by_category[normalized].append(source_row)
            if (
                source_row.get("used_for_decision", "").strip().lower()
                in {"true", "1"}
                and normalized in by_category
                and source_row.get("primary_source", "").strip().lower()
                not in {"true", "1"}
            ):
                blockers.append(
                    f"decision used a non-primary {normalized} source for {run_id}"
                )
        official = coverage.get("official_sources")
        if not isinstance(official, dict):
            blockers.append(f"official_sources is missing for {run_id}")
            official = {}
        for name in OFFICIAL_SOURCE_NAMES:
            item = official.get(name, {})
            required = item.get("required") is True
            source_status = item.get("status")
            if required and source_status != "CHECKED":
                blockers.append(f"required source {name} is not CHECKED for {run_id}")
                continue
            if not required and source_status not in {"CHECKED", "NOT_APPLICABLE"}:
                blockers.append(
                    f"optional source {name} is not terminal for {run_id}"
                )
                continue
            if source_status == "CHECKED":
                checked_counts[name] += 1
                if not by_category[name]:
                    blockers.append(f"source {name} has no evidence row for {run_id}")
                elif not any(
                    evidence.get("primary_source", "").strip().lower()
                    in {"true", "1"}
                    for evidence in by_category[name]
                ):
                    blockers.append(f"source {name} has no primary evidence for {run_id}")
        try:
            cutoff = _parse_aware_datetime(row.get("source_cutoff_jst", ""))
        except ValueError:
            blockers.append(f"source cutoff is invalid for {run_id}")
        else:
            for name in OFFICIAL_SOURCE_NAMES:
                if official.get(name, {}).get("status") == "CHECKED":
                    latest_cutoffs[name] = max(cutoff, latest_cutoffs.get(name, cutoff))
        inputs.extend(
            {
                "role": role,
                "path": f'operations/runs/{run_id}/{path.name}',
                "sha256": _sha256(path),
            }
            for role, path in (
                ("coverage", coverage_path),
                ("provider_health", health_path),
                ("sources", sources_path),
            )
        )
        inputs.append(
            {
                "role": "run_artifacts",
                "path": f'operations/runs/{run_id}',
                "sha256": _directory_sha256(run_dir),
            }
        )

    watermarks_path = record_path(private_root.parent, 'source-watermarks.json')
    if not watermarks_path.is_file():
        blockers.append("source-watermarks.json is missing")
    else:
        try:
            watermarks = _read_object(watermarks_path).get("sources", {})
        except (OSError, ValueError, json.JSONDecodeError) as error:
            blockers.append(f"source-watermarks.json is invalid: {error}")
            watermarks = {}
        for name, cutoff in latest_cutoffs.items():
            value = watermarks.get(name, {}).get("last_successful_cutoff_jst")
            try:
                watermark = _parse_aware_datetime(value)
            except (TypeError, ValueError):
                blockers.append(f"source watermark is missing or invalid: {name}")
                continue
            if watermark < cutoff:
                blockers.append(f"source watermark is behind the covered run: {name}")
        inputs.append(
            {
                "role": "source_watermarks",
                "path": 'operations/source-watermarks.json',
                "sha256": _sha256(watermarks_path),
            }
        )

    return _result(
        gate=OFFICIAL_COVERAGE_GATE,
        blockers=blockers,
        metrics={
            "run_count": len(selected),
            "selected_run": ({
                **{key: selected[0].get(key, "") for key in
                   ("run_id", "attempt", "price_date", "completed_at_jst", "source_cutoff_jst")},
                "history_row_sha256": hashlib.sha256(json.dumps(selected[0], sort_keys=True,
                    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
            } if selected else None),
            "live_network_run_count": live_network_count,
            "checked_run_counts": checked_counts,
        },
        inputs=inputs,
    )


def evaluate_repository_recovery(
    *,
    root: Path = PROJECT_ROOT,
    drill_path: Path | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Validate a clean clone for the current repository layout revision."""
    root = root.resolve()
    private_root = (root / 'operations').resolve()
    drill_path = drill_path or (record_path(private_root.parent, 'evidence/recovery-drill.json'))
    blockers: list[str] = []
    try:
        drill_path.resolve().relative_to(record_path(private_root.parent, 'evidence'))
    except ValueError:
        blockers.append('recovery drill path must stay under private evidence')
    if not drill_path.is_file():
        return _result(
            gate=REPOSITORY_RECOVERY_GATE,
            blockers=[*blockers, f"recovery drill is missing: {drill_path}"],
            metrics={},
            inputs=[],
        )
    try:
        drill = _read_object(drill_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return _result(
            gate=REPOSITORY_RECOVERY_GATE,
            blockers=[*blockers, f"recovery drill is invalid: {error}"],
            metrics={},
            inputs=[],
        )
    if drill.get("schema_version") != "1.2":
        blockers.append("recovery drill schema_version must be 1.2")
    if not isinstance(drill.get("drill_id"), str) or not drill.get(
        "drill_id", ""
    ).strip():
        blockers.append("recovery drill_id is required")
    if not isinstance(drill.get("operator"), str) or not drill.get(
        "operator", ""
    ).strip():
        blockers.append("recovery operator is required")
    if drill.get("repository_visibility") != "PRIVATE":
        blockers.append("repository_visibility must be PRIVATE")
    if drill.get("reason") not in ("PRE_LIVE", "REPOSITORY_LAYOUT_CHANGE"):
        blockers.append(
            "recovery reason must be PRE_LIVE or REPOSITORY_LAYOUT_CHANGE"
        )
    layout_revision = drill.get("repository_layout_revision")
    if (
        type(layout_revision) is not int
        or layout_revision != REPOSITORY_LAYOUT_REVISION
    ):
        blockers.append(
            "recovery repository_layout_revision does not match the current layout"
        )

    started: datetime | None = None
    completed: datetime | None = None
    try:
        started = _parse_aware_datetime(drill.get("started_at_jst", ""))
        completed = _parse_aware_datetime(drill.get("completed_at_jst", ""))
    except (TypeError, ValueError):
        blockers.append("recovery timestamps must include UTC offsets")
    reference_time = (at or datetime.now(tz=JST)).astimezone(JST)
    if started and completed:
        if completed < started:
            blockers.append("recovery completed_at_jst cannot precede started_at_jst")
        if completed > reference_time:
            blockers.append("recovery completed_at_jst cannot be in the future")
        if (completed - started).total_seconds() > 24 * 60 * 60:
            blockers.append("recovery drill duration exceeds 24 hours")

    source_commit = drill.get("source_private_commit")
    recovered_commit = drill.get("recovered_private_commit")
    source_public = drill.get("source_code_tree")
    recovered_public = drill.get("recovered_code_tree")
    for name, value in (
        ("source_private_commit", source_commit),
        ("recovered_private_commit", recovered_commit),
        ("source_code_tree", source_public),
        ("recovered_code_tree", recovered_public),
    ):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
            blockers.append(f"recovery {name} must be a full commit SHA")
    if source_commit != recovered_commit:
        blockers.append("recovered private commit does not match source")
    if source_public != recovered_public:
        blockers.append("recovered vendored code tree does not match source")

    checks = drill.get("checks")
    if not isinstance(checks, dict):
        checks = {}
        blockers.append("recovery checks must be an object")
    required_checks = (
        "clean_clone_single_repository",
        "workspace_setup",
        "state_validation",
        "paper_bootstrap_check",
        "latest_successful_run",
        "latest_handoff",
        "all_ledgers",
        "unreconciled_orders",
    )
    for name in required_checks:
        if checks.get(name) is not True:
            blockers.append(f"recovery check {name} must be true")
    if not isinstance(drill.get("result_notes"), str) or not drill.get(
        "result_notes", ""
    ).strip():
        blockers.append("recovery result_notes is required")

    provenance_metrics = {}
    try:
        from stock_jp.operation.provenance import verify as verify_recovery_provenance
        provenance_metrics = verify_recovery_provenance(root, drill)
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        blockers.append(f'recovery provenance validation failed: {error}')

    return _result(
        gate=REPOSITORY_RECOVERY_GATE,
        blockers=blockers,
        metrics={
            **provenance_metrics,
            "drill_id": drill.get("drill_id"),
            "completed_at_jst": completed.isoformat(timespec="seconds")
            if completed
            else None,
            "repository_layout_revision": drill.get("repository_layout_revision"),
            "required_check_count": len(required_checks),
            "passed_check_count": sum(checks.get(name) is True for name in required_checks),
        },
        inputs=[
            {
                "role": "recovery_drill",
                "path": 'operations/evidence/recovery-drill.json',
                "sha256": _sha256(drill_path),
            }
        ],
    )


def evaluate_personal_risk(
    *,
    root: Path = PROJECT_ROOT,
    checklist_path: Path | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Validate the owner's private risk, tax, broker, and security checklist."""
    root = root.resolve()
    private_root = (root / 'operations').resolve()
    checklist_path = checklist_path or (
        record_path(private_root.parent, 'evidence/personal-risk-and-broker.json')
    )
    blockers: list[str] = []
    try:
        checklist_path.resolve().relative_to(record_path(private_root.parent, 'evidence'))
    except ValueError:
        blockers.append('personal checklist path must stay under private evidence')
    if not checklist_path.is_file():
        return _result(
            gate=PERSONAL_RISK_GATE,
            blockers=[*blockers, f"personal checklist is missing: {checklist_path}"],
            metrics={},
            inputs=[],
        )
    try:
        checklist = _read_object(checklist_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return _result(
            gate=PERSONAL_RISK_GATE,
            blockers=[*blockers, f"personal checklist is invalid: {error}"],
            metrics={},
            inputs=[],
        )
    if checklist.get("schema_version") != "1.0":
        blockers.append("personal checklist schema_version must be 1.0")
    for field in ("owner", "broker_name"):
        if not isinstance(checklist.get(field), str) or not checklist.get(
            field, ""
        ).strip():
            blockers.append(f"personal checklist {field} is required")
    reference_time = (at or datetime.now(tz=JST)).astimezone(JST)
    reviewed: datetime | None = None
    try:
        reviewed = _parse_aware_datetime(checklist.get("reviewed_at_jst", ""))
    except (TypeError, ValueError):
        blockers.append("personal checklist reviewed_at_jst must include a UTC offset")
    if reviewed:
        if reviewed > reference_time:
            blockers.append("personal checklist reviewed_at_jst cannot be in the future")
        if (reference_time - reviewed).total_seconds() > 90 * 24 * 60 * 60:
            blockers.append("personal risk and broker review is older than 90 days")

    confirmations = checklist.get("confirmations")
    if not isinstance(confirmations, dict):
        confirmations = {}
        blockers.append("personal checklist confirmations must be an object")
    required_confirmations = (
        "loss_does_not_affect_living",
        "living_tax_and_five_year_funds_excluded",
        "borrowed_funds_excluded",
        "emergency_fund_separate",
        "total_loss_stop_documented",
        "tax_residency_and_account_type_checked",
        "fees_tax_and_filing_obligations_checked",
        "cash_equity_and_trading_unit_checked",
        "limit_order_tick_and_validity_checked",
        "broker_rules_checked_from_official_source",
        "available_for_0845_0855_manual_check",
        "broker_submission_human_only",
        "no_automatic_broker_api",
        "correct_broker_url_bookmarked",
        "mfa_or_passkey_enabled",
        "login_and_trade_notifications_enabled",
        "phishing_and_account_lockout_procedure_checked",
        "emergency_cancel_and_pause_procedure_checked",
        "investment_can_lose_all_allocated_capital",
        "returns_are_not_guaranteed",
    )
    for name in required_confirmations:
        if confirmations.get(name) is not True:
            blockers.append(f"personal confirmation {name} must be true")

    limits = checklist.get("risk_limits_pct")
    if not isinstance(limits, dict):
        limits = {}
        blockers.append("personal checklist risk_limits_pct must be an object")
    limit_caps = {
        "maximum_total_loss_stop": 100.0,
        "maximum_single_name": 10.0,
        "maximum_industry": 20.0,
        "maximum_initial_purchase": 5.0,
        "maximum_additional_purchase": 2.5,
        "maximum_daily_participation": 10.0,
    }
    for name, cap in limit_caps.items():
        value = limits.get(name)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
            or value > cap
        ):
            blockers.append(f"personal risk limit {name} must be > 0 and <= {cap:g}")

    rules_url = checklist.get("broker_rules_url")
    if not isinstance(rules_url, str) or urlparse(rules_url).scheme != "https":
        blockers.append("broker_rules_url must be an HTTPS official page")
    try:
        rules_checked = _parse_aware_datetime(
            checklist.get("broker_rules_checked_at_jst", "")
        )
    except (TypeError, ValueError):
        rules_checked = None
        blockers.append("broker_rules_checked_at_jst must include a UTC offset")
    if rules_checked and reviewed and rules_checked.date() != reviewed.date():
        blockers.append("broker rules must be checked on the review date")
    if rules_checked and reviewed and rules_checked > reviewed:
        blockers.append("broker rules must be checked before completing the review")
    if rules_checked and rules_checked > reference_time:
        blockers.append("broker_rules_checked_at_jst cannot be in the future")
    if not isinstance(checklist.get("emergency_contact_location"), str) or not checklist.get(
        "emergency_contact_location", ""
    ).strip():
        blockers.append("emergency_contact_location is required")
    if not isinstance(checklist.get("review_notes"), str) or not checklist.get(
        "review_notes", ""
    ).strip():
        blockers.append("personal checklist review_notes is required")

    return _result(
        gate=PERSONAL_RISK_GATE,
        blockers=blockers,
        metrics={
            "reviewed_at_jst": reviewed.isoformat(timespec="seconds")
            if reviewed
            else None,
            "age_days": (reference_time - reviewed).days if reviewed else None,
            "confirmation_count": len(required_confirmations),
            "confirmed_count": sum(
                confirmations.get(name) is True for name in required_confirmations
            ),
            "risk_limits_pct": limits,
        },
        inputs=[
            {
                "role": "personal_risk_and_broker_checklist",
                "path": 'operations/evidence/personal-risk-and-broker.json',
                "sha256": _sha256(checklist_path),
            }
        ],
    )


def evaluate_v04_promotion(
    *,
    root: Path = PROJECT_ROOT,
    replay_path: Path | None = None,
    historical_evidence_path: Path | None = None,
    review_path: Path | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Validate a post-freeze forward holdout and explicit owner promotion."""
    root = root.resolve()
    reference_time = (at or datetime.now(tz=JST)).astimezone(JST)
    private_root = (root / 'operations').resolve()
    replay_path = replay_path or (
        record_path(private_root.parent, 'historical-replay/v04-forward/output/replay-result-v04-forward-holdout.json')
    )
    historical_evidence_path = historical_evidence_path or (
        record_path(private_root.parent, 'evidence/historical-replay.json')
    )
    review_path = review_path or (record_path(private_root.parent, 'evidence/v04-holdout-review.json'))
    blockers: list[str] = []
    inputs: list[dict[str, str]] = []
    documents: dict[str, dict[str, Any]] = {}
    for role, path in (
        ("replay_result", replay_path),
        ("historical_replay_evidence", historical_evidence_path),
        ("v04_holdout_review", review_path),
    ):
        if role == "replay_result":
            allowed = _path_is_under_evidence_roots(root, path)
        else:
            try:
                path.resolve().relative_to(private_root)
                allowed = True
            except ValueError:
                allowed = False
        if not allowed:
            blockers.append(f"{role} path is outside its allowed root")
        if not path.is_file():
            blockers.append(f"{role} is missing: {path}")
            documents[role] = {}
            continue
        try:
            documents[role] = _read_object(path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            blockers.append(f"{role} is invalid: {error}")
            documents[role] = {}
            continue
        inputs.append(
            {
                "role": role,
                "path": os.path.relpath(path.resolve(), root),
                "sha256": _sha256(path),
            }
        )

    replay = documents["replay_result"]
    historical = documents["historical_replay_evidence"]
    review = documents["v04_holdout_review"]
    holdout: dict[str, Any] = {}
    plan: dict[str, Any] = {}
    plan_path: Path | None = None
    comparisons: dict[str, Any] = {}
    if replay:
        if replay.get("schema_version") != "1.0" or replay.get("status") != "COMPLETED":
            blockers.append("v0.4 promotion requires a completed replay schema 1.0")
        if replay.get("rule_version") != "v0.4":
            blockers.append("v0.4 promotion replay rule_version must be v0.4")
        if replay.get("evaluation_kind") != "FORWARD_HOLDOUT":
            blockers.append("v0.4 promotion requires a FORWARD_HOLDOUT replay")
        if replay.get("holdout_claimed") is not True:
            blockers.append("v0.4 promotion replay must claim only a genuine forward holdout")
        period = replay.get("period")
        if not isinstance(period, dict) or not _iso_date(period.get("from")) or not _iso_date(
            period.get("through")
        ):
            period = {}
            blockers.append("forward holdout period must contain ISO dates")
        elif period["from"] > period["through"]:
            blockers.append("forward holdout period is reversed")
        if not _aware_datetime(replay.get("generated_at_jst")):
            blockers.append("replay generated_at_jst must include a UTC offset")
        else:
            replay_generated_at = _parse_aware_datetime(replay["generated_at_jst"])
            if replay_generated_at > reference_time:
                blockers.append("forward holdout result cannot be generated in the future")
            if period and replay_generated_at.date() < date.fromisoformat(period["through"]):
                blockers.append("forward holdout result was generated before period.through")
        quality = replay.get("quality")
        if not isinstance(quality, dict):
            quality = {}
            blockers.append("replay quality must be an object")
        for name in ("missing_hard_gate_inputs", "lookahead_violations"):
            if quality.get(name) != 0:
                blockers.append(f"replay quality.{name} must be 0")
        required_reviews = quality.get("required_disclosure_reviews")
        completed_reviews = quality.get("completed_disclosure_reviews")
        if (
            not isinstance(required_reviews, int)
            or isinstance(required_reviews, bool)
            or required_reviews < 0
            or not isinstance(completed_reviews, int)
            or isinstance(completed_reviews, bool)
            or completed_reviews != required_reviews
        ):
            blockers.append("forward replay disclosure review coverage must be complete")
        holdout = replay.get("holdout")
        if not isinstance(holdout, dict):
            holdout = {}
            blockers.append("replay holdout metadata is required")
        if holdout.get("predeclared") is not True:
            blockers.append("replay holdout.predeclared must be true")
        if holdout.get("retuning_count") != 0:
            blockers.append("replay holdout.retuning_count must be 0")
        for name in ("plan_frozen_at_jst", "rule_frozen_at_jst"):
            if not _aware_datetime(holdout.get(name)):
                blockers.append(f"replay holdout.{name} must include a UTC offset")
        if _aware_datetime(holdout.get("rule_frozen_at_jst")) and _parse_aware_datetime(
            holdout["rule_frozen_at_jst"]
        ).date() < date(2026, 9, 1):
            blockers.append("v0.4 rule freeze cannot predate its 2026-09-01 effective date")
        if period and _aware_datetime(holdout.get("plan_frozen_at_jst")) and _parse_aware_datetime(
            holdout["plan_frozen_at_jst"]
        ).date() >= date.fromisoformat(period["from"]):
            blockers.append("holdout plan must be frozen before the forward period")
        plan_path = _safe_project_path(root, holdout.get("plan_path"))
        if plan_path is None or not plan_path.is_file():
            blockers.append("holdout plan path is missing or unsafe")
        elif holdout.get("plan_sha256") != _sha256(plan_path):
            blockers.append("holdout plan hash does not match")
        else:
            try:
                plan = _read_object(plan_path)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                blockers.append(f"holdout plan is invalid: {error}")
            else:
                inputs.append(
                    {"role": "holdout_plan", "path": str(holdout["plan_path"]),
                     "sha256": _sha256(plan_path)}
                )
                if plan.get("schema_version") != "1.0" or plan.get("status") != "FROZEN":
                    blockers.append("holdout plan must be frozen schema 1.0")
                if plan.get("decision") != "START_FORWARD_HOLDOUT":
                    blockers.append("holdout plan decision must be START_FORWARD_HOLDOUT")
                if plan.get("rule_version") != "v0.4" or plan.get("period") != period:
                    blockers.append("holdout plan rule or period does not match replay")
                if not isinstance(plan.get("declared_by"), str) or not plan.get(
                    "declared_by", ""
                ).strip():
                    blockers.append("holdout plan declared_by is required")
                if plan.get("frozen_at_jst") != holdout.get("plan_frozen_at_jst"):
                    blockers.append("holdout plan frozen timestamp does not match replay")
                if plan.get("rule_frozen_at_jst") != holdout.get("rule_frozen_at_jst"):
                    blockers.append("holdout rule frozen timestamp does not match replay")
                if plan.get("acceptance_criteria") != holdout.get("acceptance_criteria"):
                    blockers.append("holdout acceptance criteria do not match the frozen plan")
                if _aware_datetime(plan.get("frozen_at_jst")) and _aware_datetime(
                    plan.get("rule_frozen_at_jst")
                ) and _parse_aware_datetime(plan["frozen_at_jst"]) < _parse_aware_datetime(
                    plan["rule_frozen_at_jst"]
                ):
                    blockers.append("holdout plan cannot precede the v0.4 rule freeze")
                if _aware_datetime(plan.get("frozen_at_jst")) and _parse_aware_datetime(
                    plan["frozen_at_jst"]
                ) > reference_time:
                    blockers.append("holdout plan frozen_at_jst cannot be in the future")
                plan_criteria = plan.get("acceptance_criteria")
                if not isinstance(plan_criteria, dict):
                    plan_criteria = {}
                    blockers.append("holdout plan acceptance_criteria must be an object")
                for name in (
                    "minimum_monthly_evaluation_count",
                    "minimum_trade_count",
                ):
                    value = plan_criteria.get(name)
                    if (
                        not isinstance(value, int)
                        or isinstance(value, bool)
                        or value < 1
                    ):
                        blockers.append(f"holdout plan {name} must be a positive integer")
                for name in (
                    "maximum_drawdown_floor_pct",
                    "maximum_single_name_loss_floor_pct",
                    "maximum_industry_loss_floor_pct",
                ):
                    value = plan_criteria.get(name)
                    if (
                        not isinstance(value, (int, float))
                        or isinstance(value, bool)
                        or not math.isfinite(value)
                        or not -100 <= value <= 0
                    ):
                        blockers.append(
                            f"holdout plan {name} must be numeric between -100 and 0"
                        )
                plan_acknowledgements = plan.get("acknowledgements")
                if not isinstance(plan_acknowledgements, dict):
                    plan_acknowledgements = {}
                    blockers.append("holdout plan acknowledgements must be an object")
                for name in (
                    "period_was_unobserved_when_frozen",
                    "inputs_and_execution_rules_frozen",
                    "thresholds_frozen_before_results",
                    "changes_restart_the_holdout",
                ):
                    if plan_acknowledgements.get(name) is not True:
                        blockers.append(f"holdout plan acknowledgement {name} must be true")
        if holdout.get("criteria_met") is not True or holdout.get("criterion_failures") != []:
            blockers.append("forward holdout did not meet its frozen acceptance criteria")

        comparisons = replay.get("comparisons")
        if not isinstance(comparisons, dict):
            comparisons = {}
            blockers.append("replay comparisons must be an object")
        required_metrics = (
            "return_pct", "maximum_drawdown_pct",
            "maximum_single_name_loss_contribution_pct",
            "maximum_industry_loss_contribution_pct", "trade_count",
        )
        for version in ("v0.2", "v0.4"):
            version_metrics = comparisons.get(version)
            if not isinstance(version_metrics, dict):
                version_metrics = {}
                blockers.append(f"replay comparisons.{version} must be an object")
            for name in required_metrics:
                value = version_metrics.get(name)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                    blockers.append(f"replay comparisons.{version}.{name} must be numeric")
        criteria = holdout.get("acceptance_criteria", {})
        observations = holdout.get("observations", {})
        v04 = comparisons.get("v0.4", {}) if isinstance(comparisons.get("v0.4"), dict) else {}
        if isinstance(criteria, dict) and isinstance(observations, dict):
            checks = (
                (observations.get("monthly_evaluation_count"), criteria.get("minimum_monthly_evaluation_count"), ">="),
                (observations.get("trade_count"), criteria.get("minimum_trade_count"), ">="),
                (v04.get("maximum_drawdown_pct"), criteria.get("maximum_drawdown_floor_pct"), ">="),
                (v04.get("maximum_single_name_loss_contribution_pct"), criteria.get("maximum_single_name_loss_floor_pct"), ">="),
                (v04.get("maximum_industry_loss_contribution_pct"), criteria.get("maximum_industry_loss_floor_pct"), ">="),
            )
            if any(
                not isinstance(actual, (int, float))
                or isinstance(actual, bool)
                or not isinstance(expected, (int, float))
                or isinstance(expected, bool)
                or actual < expected
                for actual, expected, _ in checks
            ):
                blockers.append("frozen holdout acceptance criteria are not satisfied")

    if historical:
        if historical.get("schema_version") != "1.0":
            blockers.append("historical replay evidence schema_version must be 1.0")
        if historical.get("gate") != HISTORICAL_REPLAY_GATE or historical.get("eligible") is not True:
            blockers.append("historical replay evidence is not eligible")
        if historical.get("blockers") != []:
            blockers.append("historical replay evidence contains blockers")

    if review:
        if review.get("schema_version") != "1.1":
            blockers.append("v0.4 review schema_version must be 1.1")
        if review.get("decision") != "PROMOTE_V0_4_TO_LIVE":
            blockers.append("v0.4 review decision must be PROMOTE_V0_4_TO_LIVE")
        if review.get("rule_version") != "v0.4":
            blockers.append("v0.4 review rule_version must be v0.4")
        if not isinstance(review.get("approved_by"), str) or not review.get("approved_by", "").strip():
            blockers.append("v0.4 review approved_by is required")
        if not _aware_datetime(review.get("approved_at_jst")):
            blockers.append("v0.4 review approved_at_jst must include a UTC offset")
            promotion_approved_at = None
        else:
            promotion_approved_at = _parse_aware_datetime(review["approved_at_jst"])
            if promotion_approved_at > reference_time:
                blockers.append("v0.4 review approved_at_jst cannot be in the future")
            if _aware_datetime(replay.get("generated_at_jst")) and promotion_approved_at < _parse_aware_datetime(
                replay["generated_at_jst"]
            ):
                blockers.append("v0.4 review cannot precede replay generation")
        for name, path in (
            ("replay_result_sha256", replay_path),
            ("historical_replay_evidence_sha256", historical_evidence_path),
            ("holdout_plan_sha256", plan_path),
        ):
            if path is None or not path.is_file() or review.get(name) != _sha256(path):
                blockers.append(f"v0.4 review {name} does not match")
        evaluated_at = historical.get("evaluated_at_jst")
        if historical and not _aware_datetime(evaluated_at):
            blockers.append("historical replay evidence evaluated_at_jst is invalid")
        elif historical:
            historical_evaluated_at = _parse_aware_datetime(evaluated_at)
            if historical_evaluated_at > reference_time:
                blockers.append("historical replay evidence cannot be evaluated in the future")
            if promotion_approved_at and promotion_approved_at < historical_evaluated_at:
                blockers.append("v0.4 review predates historical replay evidence")
        acknowledgements = review.get("acknowledgements")
        if not isinstance(acknowledgements, dict):
            acknowledgements = {}
            blockers.append("v0.4 review acknowledgements must be an object")
        required_acknowledgements = (
            "allocation_amplification_reviewed", "maximum_drawdown_reviewed",
            "single_name_concentration_loss_reviewed", "industry_concentration_loss_reviewed",
            "waiting_cash_is_not_safe_asset", "no_holdout_retuning",
            "holdout_chronology_reviewed", "frozen_acceptance_criteria_reviewed",
        )
        for name in required_acknowledgements:
            if acknowledgements.get(name) is not True:
                blockers.append(f"v0.4 acknowledgement {name} must be true")
        reviewed_metrics = review.get("accepted_v04_metrics")
        v04_metrics = comparisons.get("v0.4", {})
        if not isinstance(reviewed_metrics, dict):
            reviewed_metrics = {}
            blockers.append("v0.4 review accepted_v04_metrics must be an object")
        for name in (
            "maximum_drawdown_pct", "maximum_single_name_loss_contribution_pct",
            "maximum_industry_loss_contribution_pct",
        ):
            if reviewed_metrics.get(name) != v04_metrics.get(name):
                blockers.append(f"v0.4 accepted metric does not match replay: {name}")
    else:
        required_acknowledgements = ()
        acknowledgements = {}

    return _result(
        gate=V04_PROMOTION_GATE,
        blockers=blockers,
        metrics={
            "decision": review.get("decision"), "approved_at_jst": review.get("approved_at_jst"),
            "holdout_period": replay.get("period"), "criteria_met": holdout.get("criteria_met"),
            "v02": comparisons.get("v0.2"), "v04": comparisons.get("v0.4"),
            "acknowledgement_count": len(required_acknowledgements),
            "acknowledged_count": sum(
                acknowledgements.get(name) is True for name in required_acknowledgements
            ),
        },
        inputs=inputs,
    )


def write_private_evidence(*, root: Path, path: Path, result: dict[str, Any]) -> None:
    'Write only successful evidence, and only below operations/evidence.'
    if result.get("eligible") is not True:
        raise ValueError("ineligible gate evidence cannot be written")
    evidence_root = (root / 'operations/evidence').resolve()
    target = path.resolve()
    try:
        target.relative_to(evidence_root)
    except ValueError as error:
        raise ValueError('evidence path must be under operations/evidence') from error
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, delete=False
    ) as temporary:
        json.dump(result, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(target)


def _atomic_write_object(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        json.dump(value, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(path)


def evaluate_all_live_requirements(*, root: Path = PROJECT_ROOT) -> dict[str, dict[str, Any]]:
    """Re-evaluate every pre-promotion requirement from its current source data."""
    return {
        POINT_IN_TIME_GATE: evaluate_point_in_time(root=root),
        HISTORICAL_REPLAY_GATE: evaluate_historical_replay(root=root),
        OFFICIAL_COVERAGE_GATE: evaluate_official_coverage(root=root),
        PERSONAL_RISK_GATE: evaluate_personal_risk(root=root),
    }


def _evidence_core(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": evidence.get("schema_version"),
        "gate": evidence.get("gate"),
        "eligible": evidence.get("eligible"),
        "blockers": evidence.get("blockers"),
        "inputs": evidence.get("inputs"),
    }


def evaluate_live_promotion(
    *,
    root: Path = PROJECT_ROOT,
    approval_path: Path | None = None,
    policy_path: Path | None = None,
) -> dict[str, Any]:
    """Re-run every gate and validate the owner's final LIVE approval bundle."""
    root = root.resolve()
    private_root = (root / 'operations').resolve()
    approval_path = approval_path or (record_path(private_root.parent, 'evidence/live-approval.json'))
    policy_path = policy_path or (record_path(private_root.parent, 'policy.json'))
    blockers: list[str] = []
    inputs: list[dict[str, str]] = []
    try:
        approval_path.resolve().relative_to(record_path(private_root.parent, 'evidence'))
        policy_path.resolve().relative_to((root / 'config').resolve())
    except ValueError:
        blockers.append('approval and policy paths must stay under operations')

    if not policy_path.is_file():
        policy: dict[str, Any] = {}
        blockers.append(f"operation policy is missing: {policy_path}")
    else:
        try:
            policy = _read_object(policy_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            policy = {}
            blockers.append(f'operation policy is invalid: {error}')
        else:
            inputs.append({'role': 'pre_promotion_policy', 'path': 'config/policy.json', 'sha256': _sha256(policy_path)})
            blockers.extend((f'operation policy: {item}' for item in validate_policy(policy)))
            if policy.get('operation_mode') not in {'PAPER', 'LIMITED_LIVE'}:
                blockers.append('operation_mode must still be PAPER or LIMITED_LIVE before promotion')
            if policy.get('active_rule_version') != 'v0.4':
                blockers.append('active_rule_version must be v0.4')
            if policy.get('broker_submission') != 'HUMAN_ONLY':
                blockers.append('broker_submission must remain HUMAN_ONLY')

    fresh = evaluate_all_live_requirements(root=root)
    stored: dict[str, dict[str, Any]] = {}
    for gate, relative in LIVE_REQUIREMENT_EVIDENCE.items():
        path = (root / relative).resolve()
        private_evidence_root = (record_path(private_root.parent, 'evidence')).resolve()
        try:
            path.relative_to(private_evidence_root)
        except ValueError:
            blockers.append(f"stored evidence path escaped private storage: {gate}")
            stored[gate] = {}
            continue
        if not path.is_file():
            blockers.append(f"stored evidence is missing: {gate}")
            stored[gate] = {}
            continue
        try:
            evidence = _read_object(path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            blockers.append(f"stored evidence is invalid for {gate}: {error}")
            stored[gate] = {}
            continue
        stored[gate] = evidence
        current = fresh.get(gate, {})
        if current.get("eligible") is not True:
            blockers.append(f"current requirement is not eligible: {gate}")
        if _evidence_core(evidence) != _evidence_core(current):
            blockers.append(f"stored evidence is stale or does not match: {gate}")
        inputs.append(
            {"role": gate, "path": relative, "sha256": _sha256(path)}
        )

    if not approval_path.is_file():
        approval: dict[str, Any] = {}
        blockers.append(f"final LIVE approval is missing: {approval_path}")
    else:
        try:
            approval = _read_object(approval_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            approval = {}
            blockers.append(f'final LIVE approval is invalid: {error}')
        else:
            inputs.append({'role': 'final_live_approval', 'path': 'operations/evidence/live-approval.json', 'sha256': _sha256(approval_path)})
    if approval:
        if approval.get("start_conditions_version") != START_CONDITIONS_VERSION:
            blockers.append("final approval start_conditions_version must match current start conditions")
        if approval.get("schema_version") != "1.0":
            blockers.append("final approval schema_version must be 1.0")
        if approval.get("decision") != "LIVE":
            blockers.append("final approval decision must be LIVE")
        if approval.get("rule_version") != "v0.4":
            blockers.append("final approval rule_version must be v0.4")
        if approval.get("broker_submission") != "HUMAN_ONLY":
            blockers.append("final approval broker_submission must be HUMAN_ONLY")
        if not isinstance(approval.get("approved_by"), str) or not approval.get(
            "approved_by", ""
        ).strip():
            blockers.append("final approval approved_by is required")
        if not _aware_datetime(approval.get("approved_at_jst")):
            blockers.append("final approval approved_at_jst must include a UTC offset")
            approved_at = None
        else:
            approved_at = _parse_aware_datetime(approval["approved_at_jst"])
            if approved_at > datetime.now(tz=JST):
                blockers.append("final approval approved_at_jst cannot be in the future")
        for name in ("private_commit_reviewed", "code_tree_reviewed"):
            value = approval.get(name)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
                blockers.append(f"final approval {name} must be a full commit SHA")
        if policy_path.is_file() and approval.get(
            "pre_promotion_policy_sha256"
        ) != _sha256(policy_path):
            blockers.append("final approval does not bind the current PAPER policy")
        approved_evidence = approval.get("evidence")
        if not isinstance(approved_evidence, dict):
            approved_evidence = {}
            blockers.append("final approval evidence must be an object")
        for gate, relative in LIVE_REQUIREMENT_EVIDENCE.items():
            item = approved_evidence.get(gate)
            path = root / relative
            if not isinstance(item, dict):
                blockers.append(f"final approval evidence is missing: {gate}")
                continue
            if item.get("path") != relative:
                blockers.append(f"final approval evidence path mismatch: {gate}")
            if not path.is_file() or item.get("sha256") != _sha256(path):
                blockers.append(f"final approval evidence hash mismatch: {gate}")
            evaluated_at = stored.get(gate, {}).get("evaluated_at_jst")
            if approved_at and _aware_datetime(evaluated_at):
                if approved_at < _parse_aware_datetime(evaluated_at):
                    blockers.append(f"final approval predates evidence: {gate}")
            elif stored.get(gate):
                blockers.append(f"stored evidence evaluated_at_jst is invalid: {gate}")
        acknowledgements = approval.get("acknowledgements")
        if not isinstance(acknowledgements, dict):
            acknowledgements = {}
            blockers.append("final approval acknowledgements must be an object")
        required_acknowledgements = (
            "all_requirement_evidence_reviewed",
            "first_live_order_requires_pretrade_check",
            "human_only_submission",
            "pause_on_any_blocking_gap",
            "no_automatic_order_submission",
            "live_decision_is_not_a_profit_guarantee",
            "forward_performance_not_established_at_start",
        )
        for name in required_acknowledgements:
            if acknowledgements.get(name) is not True:
                blockers.append(f"final approval acknowledgement {name} must be true")
    else:
        required_acknowledgements = ()
        acknowledgements = {}

    return _result(
        gate=LIVE_PROMOTION_GATE,
        blockers=blockers,
        metrics={
            "requirement_count": len(LIVE_REQUIREMENT_EVIDENCE),
            "eligible_requirement_count": sum(
                result.get("eligible") is True for result in fresh.values()
            ),
            "decision": approval.get("decision"),
            "approved_by": approval.get("approved_by"),
            "approved_at_jst": approval.get("approved_at_jst"),
            "acknowledgement_count": len(required_acknowledgements),
            "acknowledged_count": sum(
                acknowledgements.get(name) is True
                for name in required_acknowledgements
            ),
        },
        inputs=inputs,
    )


def validate_promoted_evidence_bundle(
    *, root: Path, policy: dict[str, Any]
) -> list[str]:
    """Validate immutable promotion evidence during subsequent LIVE bootstraps."""
    root = root.resolve()
    private_root = (root / 'operations').resolve()
    failures: list[str] = []
    approval_relative = policy.get("approval", {}).get("evidence_path")
    if not isinstance(approval_relative, str):
        return ["typed LIVE approval evidence path is missing"]
    approval_path = (root / approval_relative).resolve()
    try:
        approval_path.relative_to(record_path(private_root.parent, 'evidence'))
    except ValueError:
        return ['typed LIVE approval evidence path is outside private evidence']
    if not approval_path.is_file():
        return ["typed LIVE approval evidence file is missing"]
    try:
        approval = _read_object(approval_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"typed LIVE approval evidence is invalid: {error}"]
    if approval.get("start_conditions_version") != START_CONDITIONS_VERSION:
        failures.append("typed LIVE approval start_conditions_version mismatch")
    if approval.get("decision") != "LIVE":
        failures.append("typed LIVE approval decision is not LIVE")
    if approval.get("schema_version") != "1.0":
        failures.append("typed LIVE approval schema_version is not 1.0")
    if approval.get("broker_submission") != "HUMAN_ONLY":
        failures.append("typed LIVE approval broker_submission is not HUMAN_ONLY")
    if approval.get("approved_by") != policy.get("approval", {}).get("approved_by"):
        failures.append("typed LIVE approval approved_by differs from policy")
    if approval.get("approved_at_jst") != policy.get("approval", {}).get(
        "approved_at_jst"
    ):
        failures.append("typed LIVE approval timestamp differs from policy")
    if approval.get("rule_version") != policy.get("active_rule_version"):
        failures.append("typed LIVE approval rule_version differs from policy")
    for name in ("private_commit_reviewed", "code_tree_reviewed"):
        value = approval.get(name)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
            failures.append(f"typed LIVE approval {name} is not a full commit SHA")
    required_acknowledgements = (
        "all_requirement_evidence_reviewed",
        "first_live_order_requires_pretrade_check",
        "human_only_submission",
        "pause_on_any_blocking_gap",
        "no_automatic_order_submission",
        "live_decision_is_not_a_profit_guarantee",
            "forward_performance_not_established_at_start",
    )
    acknowledgements = approval.get("acknowledgements", {})
    for name in required_acknowledgements:
        if acknowledgements.get(name) is not True:
            failures.append(f"typed LIVE approval acknowledgement is false: {name}")
    approved_evidence = approval.get("evidence", {})
    for gate, expected_relative in LIVE_REQUIREMENT_EVIDENCE.items():
        item = approved_evidence.get(gate, {})
        relative = item.get("path")
        if relative != expected_relative:
            failures.append(f"typed evidence path mismatch: {gate}")
            continue
        if gate in POLICY_LIVE_GATES and policy.get("live_gate_evidence", {}).get(
            gate
        ) != relative:
            failures.append(f"policy evidence path mismatch: {gate}")
        path = (root / relative).resolve()
        try:
            path.relative_to(record_path(private_root.parent, 'evidence'))
        except ValueError:
            failures.append(f'typed evidence path escaped private storage: {gate}')
            continue
        if not path.is_file():
            failures.append(f"typed evidence file is missing: {gate}")
            continue
        if item.get("sha256") != _sha256(path):
            failures.append(f"typed evidence hash mismatch: {gate}")
            continue
        try:
            evidence = _read_object(path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            failures.append(f"typed evidence is invalid for {gate}: {error}")
            continue
        if (
            evidence.get("schema_version") != "1.0"
            or evidence.get("gate") != gate
            or evidence.get("eligible") is not True
            or evidence.get("blockers") != []
        ):
            failures.append(f"typed evidence is not an eligible {gate} result")
    return failures


def apply_live_promotion(
    *, root: Path, result: dict[str, Any], approval_path: Path, policy_path: Path
) -> dict[str, Any]:
    """Atomically promote a PAPER policy only after the full guard passes."""
    if result.get("eligible") is not True:
        raise ValueError("ineligible LIVE promotion cannot be applied")
    root = root.resolve()
    approval = _read_object(approval_path)
    policy = _read_object(policy_path)
    if policy.get("operation_mode") not in {"PAPER", "LIMITED_LIVE"}:
        raise ValueError("only a PAPER or LIMITED_LIVE policy can be promoted")
    if approval.get("pre_promotion_policy_sha256") != _sha256(policy_path):
        raise ValueError("approval does not bind the current policy")
    policy["operation_mode"] = "LIVE"
    policy["effective_at_jst"] = approval["approved_at_jst"]
    policy["active_rule_version"] = "v0.4"
    policy["broker_submission"] = "HUMAN_ONLY"
    for gate in POLICY_LIVE_GATES:
        policy["live_gates"][gate] = True
        policy["live_gate_evidence"][gate] = LIVE_REQUIREMENT_EVIDENCE[gate]
    # Starting LIVE does not complete or approve the preserved holdout study.
    approval_private_relative = approval_path.resolve().relative_to(
        (root / 'operations').resolve()
    )
    policy["approval"] = {
        "start_conditions_version": approval["start_conditions_version"],
        "approved_by": approval["approved_by"],
        "approved_at_jst": approval["approved_at_jst"],
        "evidence_path": (Path('operations') / approval_private_relative).as_posix(),
    }
    errors = validate_policy(policy)
    if errors:
        raise ValueError("promoted policy is invalid: " + "; ".join(errors))
    bundle_failures = validate_promoted_evidence_bundle(root=root, policy=policy)
    if bundle_failures:
        raise ValueError("promoted evidence is invalid: " + "; ".join(bundle_failures))
    _atomic_write_object(policy_path, policy)
    return policy


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)
    point_in_time = subparsers.add_parser(
        "point-in-time", help="validate the full-universe point-in-time replay"
    )
    point_in_time.add_argument("--manifest", type=Path)
    point_in_time.add_argument("--write-evidence", type=Path)
    replay = subparsers.add_parser(
        "historical-replay", help="validate and accept the fixed 2025-2026 replay"
    )
    replay.add_argument("--result", type=Path)
    replay.add_argument("--review", type=Path)
    replay.add_argument("--write-evidence", type=Path)
    paper_duration = subparsers.add_parser(
        "paper-duration", help="optional diagnostic for v0.4 PAPER duration"
    )
    paper_duration.add_argument("--history", type=Path)
    paper_duration.add_argument("--at", type=str)
    paper_duration.add_argument("--write-evidence", type=Path)
    shadow = subparsers.add_parser(
        "shadow-run", help="optional diagnostic for 20 real-data PAPER sessions"
    )
    shadow.add_argument("--history", type=Path)
    shadow.add_argument("--write-evidence", type=Path)
    coverage = subparsers.add_parser(
        "official-coverage", help="validate current official-source coverage"
    )
    coverage.add_argument("--history", type=Path)
    coverage.add_argument("--write-evidence", type=Path)
    recovery = subparsers.add_parser(
        "repository-recovery",
        help="validate a clean clone for the current repository layout",
    )
    recovery.add_argument("--drill", type=Path)
    recovery.add_argument("--at", type=str)
    recovery.add_argument("--write-evidence", type=Path)
    personal = subparsers.add_parser(
        "personal-risk", help="validate the owner's risk and broker checklist"
    )
    personal.add_argument("--checklist", type=Path)
    personal.add_argument("--at", type=str)
    personal.add_argument("--write-evidence", type=Path)
    promotion = subparsers.add_parser(
        "v04-promotion", help="optional post-start v0.4 holdout research validation"
    )
    promotion.add_argument("--replay", type=Path)
    promotion.add_argument("--historical-evidence", type=Path)
    promotion.add_argument("--review", type=Path)
    promotion.add_argument("--write-evidence", type=Path)
    live = subparsers.add_parser(
        "promote-live", help="validate all requirements and optionally apply LIVE mode"
    )
    live.add_argument("--approval", type=Path)
    live.add_argument("--policy", type=Path)
    live.add_argument("--apply", action="store_true")
    live.add_argument("--write-evidence", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "point-in-time":
        result = evaluate_point_in_time(
            root=args.root,
            manifest_path=args.manifest,
        )
    elif args.command == "historical-replay":
        result = evaluate_historical_replay(
            root=args.root,
            result_path=args.result,
            review_path=args.review,
        )
    elif args.command == "paper-duration":
        result = evaluate_paper_duration(
            root=args.root,
            history_path=args.history,
            at=_parse_aware_datetime(args.at) if args.at else None,
        )
    elif args.command == "shadow-run":
        result = evaluate_shadow_run(
            root=args.root,
            history_path=args.history,
        )
    elif args.command == "official-coverage":
        result = evaluate_official_coverage(
            root=args.root,
            history_path=args.history,
        )
    elif args.command == "repository-recovery":
        result = evaluate_repository_recovery(
            root=args.root,
            drill_path=args.drill,
            at=_parse_aware_datetime(args.at) if args.at else None,
        )
    elif args.command == "personal-risk":
        result = evaluate_personal_risk(
            root=args.root,
            checklist_path=args.checklist,
            at=_parse_aware_datetime(args.at) if args.at else None,
        )
    elif args.command == "v04-promotion":
        result = evaluate_v04_promotion(
            root=args.root,
            replay_path=args.replay,
            historical_evidence_path=args.historical_evidence,
            review_path=args.review,
        )
    elif args.command == "promote-live":
        approval_path = args.approval or (
            args.root / 'operations/evidence/live-approval.json'
        )
        policy_path = args.policy or (
            args.root / 'config/policy.json'
        )
        result = evaluate_live_promotion(
            root=args.root,
            approval_path=approval_path,
            policy_path=policy_path,
        )
        if args.apply and result["eligible"]:
            apply_live_promotion(
                root=args.root,
                result=result,
                approval_path=approval_path,
                policy_path=policy_path,
            )
    else:  # pragma: no cover - argparse prevents this branch
        raise AssertionError(args.command)
    if args.write_evidence:
        write_private_evidence(root=args.root, path=args.write_evidence, result=result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
