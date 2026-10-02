"""Create, resume, and close private daily-operation runs."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any
from zoneinfo import ZoneInfo

try:
    from scripts.operation_state import (
        initialize_or_migrate_workspace,
        secure_private_tree,
    )
except ModuleNotFoundError:  # Direct execution: python scripts/daily_operation.py
    from operation_state import initialize_or_migrate_workspace, secure_private_tree

try:
    from scripts.run_integrity import (
        SOURCE_FIELDS,
        acquire_run_lease,
        advance_source_watermarks,
        release_run_lease,
        require_run_lease,
        validate_run_artifacts,
        write_coverage_manifest,
    )
except ModuleNotFoundError:  # Direct execution: python scripts/daily_operation.py
    from run_integrity import (
        SOURCE_FIELDS,
        acquire_run_lease,
        advance_source_watermarks,
        release_run_lease,
        require_run_lease,
        validate_run_artifacts,
        write_coverage_manifest,
    )

try:
    from scripts.nightly_artifacts import create_nightly_artifacts
except ModuleNotFoundError:  # Direct execution: python scripts/daily_operation.py
    from nightly_artifacts import create_nightly_artifacts


PROJECT_ROOT = Path(__file__).resolve().parents[1]
JST = ZoneInfo("Asia/Tokyo")
SOURCE_HEADER = ",".join(SOURCE_FIELDS) + "\n"


def _parse_jst(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("datetime must include a UTC offset")
    return parsed.astimezone(JST)


def _iso_jst(value: str) -> str:
    return _parse_jst(value).isoformat(timespec="seconds")


def _private_root(root: Path) -> Path:
    return root / "operations/private"


def _validate_run_id(value: str) -> str:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("run ID must be YYYY-MM-DD")
    return value


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        json.dump(value, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def initialize_workspace(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    return initialize_or_migrate_workspace(root)


def prepare_run(
    *, at: str, run_token: str | None = None, root: Path = PROJECT_ROOT
) -> dict[str, Any]:
    initialize_workspace(root)
    started = _parse_jst(at)
    started_iso = started.isoformat(timespec="seconds")
    run_id = started.date().isoformat()
    private = _private_root(root)
    state = _read_json(private / "state.json")
    policy = _read_json(private / "operation-policy.json")
    run_dir = private / "runs" / run_id
    handoff_path = run_dir / "handoff.json"

    if run_dir.exists():
        if not handoff_path.is_file():
            raise FileNotFoundError(f"existing run is missing {handoff_path}")
        handoff = _read_json(handoff_path)
        if handoff["status"] == "completed":
            return {
                "run_id": run_id,
                "resumed": True,
                "status": "completed",
                "run_dir": _relative(run_dir, root),
                "report": _relative(run_dir / "report.md", root),
                "orders": _relative(run_dir / "orders.csv", root),
                "sources": _relative(run_dir / "sources.csv", root),
                "coverage": _relative(run_dir / "coverage.json", root),
                "provider_health": _relative(run_dir / "provider-health.json", root),
                "research_queue": _relative(run_dir / "research-queue.json", root),
                "work_plan": _relative(run_dir / "work-plan.json", root),
                "research_results": _relative(run_dir / "research-results.md", root),
                "next_day_actions": _relative(run_dir / "next-day-actions.csv", root),
                "handoff": _relative(handoff_path, root),
            }
        lease = acquire_run_lease(
            run_dir=run_dir, run_id=run_id, at=started_iso, run_token=run_token
        )
        if not lease["acquired"]:
            return {
                "run_id": run_id,
                "resumed": False,
                "status": "locked",
                "lease_expires_at_jst": lease["expires_at_jst"],
                "run_dir": _relative(run_dir, root),
            }
        if handoff["status"] == "failed" or lease.get("reclaimed"):
            handoff.update(
                {
                    "status": "in_progress",
                    "completed_at_jst": None,
                    "summary": None,
                    "resumed_at_jst": started_iso,
                    "attempt_started_at_jst": started_iso,
                    "attempt": handoff.get("attempt", 1) + 1,
                }
            )
            _atomic_write_json(handoff_path, handoff)
        return {
            "run_id": run_id,
            "resumed": True,
            "status": handoff["status"],
            "previous_disclosure_cutoff_jst": handoff.get(
                "previous_disclosure_cutoff_jst"
            ),
            "run_dir": _relative(run_dir, root),
            "report": _relative(run_dir / "report.md", root),
            "orders": _relative(run_dir / "orders.csv", root),
            "sources": _relative(run_dir / "sources.csv", root),
            "coverage": _relative(run_dir / "coverage.json", root),
            "provider_health": _relative(run_dir / "provider-health.json", root),
            "research_queue": _relative(run_dir / "research-queue.json", root),
            "work_plan": _relative(run_dir / "work-plan.json", root),
            "research_results": _relative(run_dir / "research-results.md", root),
            "next_day_actions": _relative(run_dir / "next-day-actions.csv", root),
            "handoff": _relative(handoff_path, root),
            "lease": _relative(run_dir / "lease.json", root),
            "run_token": lease["run_token"],
            "lease_expires_at_jst": lease["expires_at_jst"],
        }

    run_dir.mkdir(parents=True)
    lease = acquire_run_lease(run_dir=run_dir, run_id=run_id, at=started_iso)
    previous_cutoff = state.get("last_disclosure_cutoff_jst")
    report = (root / "operations/templates/daily-report-template.md").read_text(
        encoding="utf-8"
    )
    report = (
        report.replace("{{RUN_ID}}", run_id)
        .replace("{{STARTED_AT_JST}}", started_iso)
        .replace("{{PREVIOUS_CUTOFF_JST}}", previous_cutoff or "初回実行")
        .replace("{{OPERATION_MODE}}", policy["operation_mode"])
        .replace("{{ACTIVE_RULE_VERSION}}", policy["active_rule_version"])
    )
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    shutil.copyfile(
        root / "operations/templates/order-ticket-template.csv",
        run_dir / "orders.csv",
    )
    (run_dir / "sources.csv").write_text(SOURCE_HEADER, encoding="utf-8")
    write_coverage_manifest(root=root, run_dir=run_dir, run_id=run_id, state=state)
    for template_name, destination_name in (
        ("provider-health-template.json", "provider-health.json"),
        ("research-queue-template.json", "research-queue.json"),
    ):
        value = _read_json(root / "operations/templates" / template_name)
        value["run_id"] = run_id
        _atomic_write_json(run_dir / destination_name, value)
    pretrade = (
        root / "operations/templates/pretrade-check-template.md"
    ).read_text(encoding="utf-8")
    (run_dir / "pretrade-check.md").write_text(
        pretrade.replace("{{RUN_ID}}", run_id), encoding="utf-8"
    )

    handoff = {
        "schema_version": "2.0",
        "run_id": run_id,
        "status": "in_progress",
        "started_at_jst": started_iso,
        "completed_at_jst": None,
        "previous_run_id": state.get("last_run_id"),
        "previous_disclosure_cutoff_jst": previous_cutoff,
        "operation_mode": policy["operation_mode"],
        "active_rule_version": policy["active_rule_version"],
        "source_cutoff_jst": None,
        "price_date": None,
        "pending_reviews": state.get("pending_reviews", []),
        "pending_orders": state.get("pending_orders", []),
        "data_gaps": state.get("data_gaps", []),
        "next_run_at_jst": state.get("next_run_at_jst"),
        "summary": None,
        "attempt": 1,
        "attempt_started_at_jst": started_iso,
    }
    _atomic_write_json(handoff_path, handoff)
    nightly = create_nightly_artifacts(run_id=run_id, at=started_iso, root=root)
    secure_private_tree(root)
    return {
        "run_id": run_id,
        "resumed": False,
        "status": "in_progress",
        "previous_disclosure_cutoff_jst": previous_cutoff,
        "run_dir": _relative(run_dir, root),
        "report": _relative(run_dir / "report.md", root),
        "orders": _relative(run_dir / "orders.csv", root),
        "sources": _relative(run_dir / "sources.csv", root),
        "coverage": _relative(run_dir / "coverage.json", root),
        "provider_health": _relative(run_dir / "provider-health.json", root),
        "research_queue": _relative(run_dir / "research-queue.json", root),
        **nightly,
        "handoff": _relative(handoff_path, root),
        "lease": _relative(run_dir / "lease.json", root),
        "run_token": lease["run_token"],
        "lease_expires_at_jst": lease["expires_at_jst"],
    }


def _count_csv_rows(path: Path) -> int:
    with path.open(encoding="utf-8", newline="") as source:
        return sum(1 for _ in csv.DictReader(source))


def _append_history(
    *,
    root: Path,
    handoff: dict[str, Any],
    alert_count: int,
    data_gap_count: int,
) -> None:
    private = _private_root(root)
    history_path = private / "run-history.csv"
    run_dir = private / "runs" / handoff["run_id"]
    policy = _read_json(private / "operation-policy.json")
    with history_path.open("a", encoding="utf-8", newline="") as destination:
        writer = csv.writer(destination)
        writer.writerow(
            [
                handoff["run_id"],
                handoff.get("attempt", 1),
                handoff.get("attempt_started_at_jst", handoff["started_at_jst"]),
                handoff["completed_at_jst"],
                handoff["status"],
                handoff.get("operation_mode", policy.get("operation_mode", "")),
                handoff.get(
                    "active_rule_version", policy.get("active_rule_version", "")
                ),
                handoff.get("source_cutoff_jst") or "",
                handoff.get("price_date") or "",
                _relative(run_dir / "report.md", root),
                _count_csv_rows(run_dir / "orders.csv"),
                alert_count,
                data_gap_count,
                handoff.get("next_run_at_jst") or "",
                handoff.get("summary") or "",
            ]
        )


def _history_has_attempt_status(
    *, root: Path, run_id: str, attempt: int, status: str
) -> bool:
    history_path = _private_root(root) / "run-history.csv"
    with history_path.open(encoding="utf-8", newline="") as source:
        return any(
            row.get("run_id") == run_id
            and row.get("attempt") == str(attempt)
            and row.get("status") == status
            for row in csv.DictReader(source)
        )


def _persist_completed_state(*, root: Path, handoff: dict[str, Any]) -> None:
    state_path = _private_root(root) / "state.json"
    state = _read_json(state_path)
    last_run_id = state.get("last_run_id")
    if last_run_id is not None and last_run_id > handoff["run_id"]:
        return
    if (
        last_run_id == handoff["run_id"]
        and state.get("last_successful_run_at_jst") == handoff["completed_at_jst"]
    ):
        return
    state.update(
        {
            "state_revision": state.get("state_revision", 0) + 1,
            "last_successful_run_at_jst": handoff["completed_at_jst"],
            "last_disclosure_cutoff_jst": handoff["source_cutoff_jst"],
            "last_price_date": handoff["price_date"],
            "last_run_id": handoff["run_id"],
            "pending_reviews": handoff.get("pending_reviews", []),
            "pending_orders": handoff.get("pending_orders", []),
            "data_gaps": handoff.get("data_gaps", []),
            "next_run_at_jst": handoff.get("next_run_at_jst"),
            "unreconciled_ticket_ids": handoff.get("pending_orders", []),
            "consecutive_successful_runs": state.get(
                "consecutive_successful_runs", 0
            )
            + 1,
        }
    )
    _atomic_write_json(state_path, state)


def complete_run(
    *,
    run_id: str,
    completed_at: str,
    source_cutoff: str,
    price_date: str,
    summary: str,
    run_token: str,
    alert_count: int = 0,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    _validate_run_id(run_id)
    date.fromisoformat(price_date)
    if alert_count < 0:
        raise ValueError("alert count cannot be negative")
    if not summary.strip():
        raise ValueError("summary is required")
    completed_iso = _iso_jst(completed_at)
    cutoff_iso = _iso_jst(source_cutoff)
    private = _private_root(root)
    run_dir = private / "runs" / run_id
    handoff_path = run_dir / "handoff.json"
    handoff = _read_json(handoff_path)
    if handoff["status"] == "completed":
        _persist_completed_state(root=root, handoff=handoff)
        if not _history_has_attempt_status(
            root=root,
            run_id=run_id,
            attempt=handoff.get("attempt", 1),
            status="completed",
        ):
            _append_history(
                root=root,
                handoff=handoff,
                alert_count=handoff.get("alert_count", 0),
                data_gap_count=len(handoff.get("data_gaps", [])),
            )
        return {"run_id": run_id, "status": "completed", "already_closed": True}
    if handoff["status"] != "in_progress":
        raise ValueError(f"cannot complete run with status {handoff['status']}")
    require_run_lease(run_dir=run_dir, run_token=run_token, at=completed_iso)

    previous_cutoff = handoff.get("previous_disclosure_cutoff_jst")
    if previous_cutoff and _parse_jst(cutoff_iso) < _parse_jst(previous_cutoff):
        raise ValueError("source cutoff cannot move backwards")

    integrity = validate_run_artifacts(
        root=root,
        run_id=run_id,
        completed_at=completed_iso,
        source_cutoff=cutoff_iso,
        price_date=price_date,
    )

    handoff.update(
        {
            "status": "completed",
            "completed_at_jst": completed_iso,
            "source_cutoff_jst": cutoff_iso,
            "price_date": price_date,
            "summary": summary.strip(),
            "alert_count": alert_count,
        }
    )
    _atomic_write_json(handoff_path, handoff)
    _persist_completed_state(root=root, handoff=handoff)
    advance_source_watermarks(
        root=root,
        source_cutoff=cutoff_iso,
        coverage=integrity["coverage"],
        source_rows=integrity["source_rows_data"],
    )
    release_run_lease(run_dir=run_dir, run_token=run_token, at=completed_iso)
    data_gap_count = len(handoff.get("data_gaps", []))
    if not _history_has_attempt_status(
        root=root,
        run_id=run_id,
        attempt=handoff.get("attempt", 1),
        status="completed",
    ):
        _append_history(
            root=root,
            handoff=handoff,
            alert_count=alert_count,
            data_gap_count=data_gap_count,
        )
    return {
        "run_id": run_id,
        "status": "completed",
        "already_closed": False,
        "source_cutoff_jst": cutoff_iso,
        "order_count": _count_csv_rows(run_dir / "orders.csv"),
        "data_gap_count": data_gap_count,
    }


def fail_run(
    *,
    run_id: str,
    completed_at: str,
    summary: str,
    run_token: str,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    _validate_run_id(run_id)
    if not summary.strip():
        raise ValueError("summary is required")
    private = _private_root(root)
    handoff_path = private / "runs" / run_id / "handoff.json"
    handoff = _read_json(handoff_path)
    if handoff["status"] == "failed":
        if not _history_has_attempt_status(
            root=root,
            run_id=run_id,
            attempt=handoff.get("attempt", 1),
            status="failed",
        ):
            _append_history(
                root=root,
                handoff=handoff,
                alert_count=0,
                data_gap_count=len(handoff.get("data_gaps", [])),
            )
        return {"run_id": run_id, "status": "failed", "already_closed": True}
    if handoff["status"] != "in_progress":
        raise ValueError(f"cannot fail run with status {handoff['status']}")
    failed_at = _iso_jst(completed_at)
    require_run_lease(run_dir=handoff_path.parent, run_token=run_token, at=failed_at)
    handoff.update(
        {
            "status": "failed",
            "completed_at_jst": failed_at,
            "summary": summary.strip(),
        }
    )
    _atomic_write_json(handoff_path, handoff)
    state_path = private / "state.json"
    state = _read_json(state_path)
    state["state_revision"] = state.get("state_revision", 0) + 1
    state["consecutive_successful_runs"] = 0
    _atomic_write_json(state_path, state)
    release_run_lease(
        run_dir=handoff_path.parent, run_token=run_token, at=failed_at
    )
    if not _history_has_attempt_status(
        root=root,
        run_id=run_id,
        attempt=handoff.get("attempt", 1),
        status="failed",
    ):
        _append_history(
            root=root,
            handoff=handoff,
            alert_count=0,
            data_gap_count=len(handoff.get("data_gaps", [])),
        )
    return {"run_id": run_id, "status": "failed", "already_closed": False}


def read_status(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    initialize_workspace(root)
    return _read_json(_private_root(root) / "state.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--at", required=True)
    prepare.add_argument("--run-token")
    complete = commands.add_parser("complete")
    complete.add_argument("--run-id", required=True)
    complete.add_argument("--completed-at", required=True)
    complete.add_argument("--source-cutoff", required=True)
    complete.add_argument("--price-date", required=True)
    complete.add_argument("--summary", required=True)
    complete.add_argument("--run-token", required=True)
    complete.add_argument("--alert-count", type=int, default=0)
    fail = commands.add_parser("fail")
    fail.add_argument("--run-id", required=True)
    fail.add_argument("--completed-at", required=True)
    fail.add_argument("--summary", required=True)
    fail.add_argument("--run-token", required=True)
    commands.add_parser("status")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "init":
        result = initialize_workspace()
    elif args.command == "prepare":
        result = prepare_run(at=args.at, run_token=args.run_token)
    elif args.command == "complete":
        result = complete_run(
            run_id=args.run_id,
            completed_at=args.completed_at,
            source_cutoff=args.source_cutoff,
            price_date=args.price_date,
            summary=args.summary,
            run_token=args.run_token,
            alert_count=args.alert_count,
        )
    elif args.command == "fail":
        result = fail_run(
            run_id=args.run_id,
            completed_at=args.completed_at,
            summary=args.summary,
            run_token=args.run_token,
        )
    else:
        result = read_status()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
