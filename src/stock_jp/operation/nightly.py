"""Single entry point for a resumable nightly Japanese-stock operation."""

from __future__ import annotations
from stock_jp.workspace import project_root

import argparse
import json
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from stock_jp.operation.assessment import run_assessments
from stock_jp.operation.runs import complete_run, fail_run, prepare_run, read_status
from stock_jp.operation.artifacts import create_nightly_artifacts
from stock_jp.market_data.disclosures import scan_sources
from stock_jp.operation.readiness import check_readiness
from stock_jp.operation.state import initialize_or_migrate_workspace, validate_workspace
from stock_jp.operation.watchdog import watchdog_status


PROJECT_ROOT = project_root()
JST = ZoneInfo("Asia/Tokyo")


def _parse_jst(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("datetime must include a UTC offset")
    return parsed.astimezone(JST)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        json.dump(value, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(path)


def _next_weeknight(value: datetime, *, completed_run_id: str | None = None) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a UTC offset")
    now = value.astimezone(JST)
    completed_day = date.fromisoformat(completed_run_id) if completed_run_id else None
    candidate = now.replace(hour=21, minute=0, second=0, microsecond=0)
    # An older run completed after midnight must not consume today's slot.
    # Conversely, a run already completed for today must not be scheduled twice.
    while (candidate <= now or candidate.weekday() >= 5
           or (completed_day is not None and candidate.date() <= completed_day)):
        candidate += timedelta(days=1)
    return candidate.isoformat(timespec="seconds")


def start_nightly_run(
    *,
    at: str,
    cutoff: str,
    run_token: str | None = None,
    resume_run_id: str | None = None,
    fixture_dir: Path | None = None,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    initialize_or_migrate_workspace(root)
    readiness = check_readiness(root=root, fixture_dir=fixture_dir, at=at)
    if not readiness["ready"]:
        raise PermissionError(
            "nightly operation readiness blocked: " + "; ".join(readiness["blockers"])
        )
    watchdog = watchdog_status(at=at, root=root)
    errors = validate_workspace(root)
    if errors:
        raise ValueError("workspace validation failed: " + "; ".join(errors))
    prepared = prepare_run(at=at, run_token=run_token, root=root, resume_run_id=resume_run_id)
    if prepared["status"] in {"completed", "locked"}:
        return prepared
    token = str(prepared["run_token"])
    scan = scan_sources(
        run_id=str(prepared["run_id"]),
        run_token=token,
        cutoff=cutoff,
        at=at,
        fixture_dir=fixture_dir,
        root=root,
    )
    assessments = run_assessments(
        run_id=str(prepared["run_id"]),
        at=at,
        fixture_dir=fixture_dir,
        root=root,
    )
    artifacts = create_nightly_artifacts(
        run_id=str(prepared["run_id"]), at=at, root=root
    )
    return {
        **prepared,
        "readiness_status": readiness["status"],
        "paper_go": readiness["paper_go"],
        "live_go": readiness["live_go"],
        "watchdog_before_start": watchdog,
        "source_scan_status": scan["status"],
        "blocking_gap_count": scan["blocking_gap_count"],
        "research_task_count": scan["research_task_count"],
        **assessments,
        **artifacts,
        "next_step": (
            "resolve blocking source gaps before making decisions"
            if scan["blocking_gap_count"]
            else (
                "resolve or defer assessment input tasks before making decisions"
                if assessments["assessment_blocker_count"]
                else "complete research queue, work plan, report, coverage, and actions"
            )
        ),
        "broker_submission": "HUMAN_ONLY",
    }


def finalize_nightly_run(
    *,
    run_id: str,
    run_token: str,
    completed_at: str,
    source_cutoff: str,
    price_date: str,
    summary: str,
    alert_count: int = 0,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    run_dir = root / 'operations/runs' / run_id
    frozen_path = run_dir / "source-scan-cutoff.json"
    if frozen_path.exists() and _read_json(frozen_path) != {
        "cutoff_at_jst": _parse_jst(source_cutoff).isoformat(timespec="seconds")
    }:
        raise ValueError("finalize must preserve the original source-scan cutoff")
    handoff_path = run_dir / "handoff.json"
    handoff = _read_json(handoff_path)
    previous_next_run = handoff.get("next_run_at_jst")
    already_completed = handoff.get("status") == "completed"
    next_run_at = previous_next_run
    if not already_completed:
        next_run_at = _next_weeknight(_parse_jst(completed_at), completed_run_id=run_id)
        handoff["next_run_at_jst"] = next_run_at
        _atomic_json(handoff_path, handoff)
    try:
        result = complete_run(
            run_id=run_id,
            run_token=run_token,
            completed_at=completed_at,
            source_cutoff=source_cutoff,
            price_date=price_date,
            summary=summary,
            alert_count=alert_count,
            root=root,
        )
    except Exception:
        if not already_completed:
            handoff["next_run_at_jst"] = previous_next_run
            _atomic_json(handoff_path, handoff)
        raise
    return {
        **result,
        "next_run_at_jst": next_run_at,
        "wait_instruction": "次回夜間実行まで待機。LIMITED_LIVE/LIVE注文候補だけ翌朝に人間が確認する。",
    }


def nightly_status(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    state = read_status(root)
    run_id = state.get("last_run_id")
    result = {
        "last_run_id": run_id,
        "last_successful_run_at_jst": state.get("last_successful_run_at_jst"),
        "next_run_at_jst": state.get("next_run_at_jst"),
        "pending_reviews": state.get("pending_reviews", []),
        "pending_orders": state.get("pending_orders", []),
        "data_gaps": state.get("data_gaps", []),
    }
    if run_id:
        run_dir = root / 'operations/runs' / str(run_id)
        if (run_dir / "handoff.json").is_file():
            result["handoff"] = _read_json(run_dir / "handoff.json")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--at", required=True)
    start.add_argument("--cutoff", required=True)
    start.add_argument("--run-token")
    start.add_argument("--resume-run-id", help="resume an existing run, including after JST midnight")
    start.add_argument("--fixture-dir", type=Path)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--run-id", required=True)
    finalize.add_argument("--run-token", required=True)
    finalize.add_argument("--completed-at", required=True)
    finalize.add_argument("--source-cutoff", required=True)
    finalize.add_argument("--price-date", required=True)
    finalize.add_argument("--summary", required=True)
    finalize.add_argument("--alert-count", type=int, default=0)
    fail = commands.add_parser("fail")
    fail.add_argument("--run-id", required=True)
    fail.add_argument("--run-token", required=True)
    fail.add_argument("--completed-at", required=True)
    fail.add_argument("--summary", required=True)
    commands.add_parser("status")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "start":
        result = start_nightly_run(
            at=args.at,
            cutoff=args.cutoff,
            run_token=args.run_token,
            resume_run_id=args.resume_run_id,
            fixture_dir=args.fixture_dir,
        )
    elif args.command == "finalize":
        result = finalize_nightly_run(
            run_id=args.run_id,
            run_token=args.run_token,
            completed_at=args.completed_at,
            source_cutoff=args.source_cutoff,
            price_date=args.price_date,
            summary=args.summary,
            alert_count=args.alert_count,
        )
    elif args.command == "fail":
        result = fail_run(
            run_id=args.run_id,
            run_token=args.run_token,
            completed_at=args.completed_at,
            summary=args.summary,
        )
    else:
        result = nightly_status()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
