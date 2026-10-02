"""Detect missed nightly runs, stale leases, and outstanding handoff work."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from scripts.operation_state import initialize_or_migrate_workspace
except ModuleNotFoundError:  # Direct execution from scripts/
    from operation_state import initialize_or_migrate_workspace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
JST = ZoneInfo("Asia/Tokyo")


def _parse_jst(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("datetime must include a UTC offset")
    return parsed.astimezone(JST)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def watchdog_status(
    *, at: str, grace_minutes: int = 120, root: Path = PROJECT_ROOT
) -> dict[str, Any]:
    if grace_minutes < 0:
        raise ValueError("grace_minutes cannot be negative")
    initialize_or_migrate_workspace(root)
    now = _parse_jst(at)
    private = root / "operations/private"
    state = _read_json(private / "state.json")
    alerts: list[str] = []
    stale_runs: list[str] = []
    active_runs: list[str] = []
    for handoff_path in sorted((private / "runs").glob("*/handoff.json")):
        handoff = _read_json(handoff_path)
        if handoff.get("status") != "in_progress":
            continue
        lease_path = handoff_path.parent / "lease.json"
        if not lease_path.is_file():
            stale_runs.append(handoff_path.parent.name)
            continue
        lease = _read_json(lease_path)
        if lease.get("status") != "ACTIVE" or _parse_jst(lease["expires_at_jst"]) <= now:
            stale_runs.append(handoff_path.parent.name)
        else:
            active_runs.append(handoff_path.parent.name)

    next_run_text = state.get("next_run_at_jst")
    next_run = _parse_jst(next_run_text) if next_run_text else None
    if stale_runs:
        status = "STALE_RUN"
        alerts.append("stale or ownerless run: " + ", ".join(stale_runs))
    elif active_runs:
        status = "RUNNING"
    elif next_run and now > next_run + timedelta(minutes=grace_minutes):
        status = "MISSED"
        alerts.append(f"nightly run overdue since {next_run.isoformat(timespec='seconds')}")
    elif next_run and now >= next_run:
        status = "DUE"
    elif not state.get("last_run_id"):
        status = "NEEDS_FIRST_RUN"
    else:
        status = "OK"

    if state.get("data_gaps"):
        alerts.append(f"{len(state['data_gaps'])} data gap(s) carried forward")
    if state.get("unreconciled_ticket_ids"):
        alerts.append(
            f"{len(state['unreconciled_ticket_ids'])} order ticket(s) await reconciliation"
        )
    return {
        "status": status,
        "checked_at_jst": now.isoformat(timespec="seconds"),
        "last_run_id": state.get("last_run_id"),
        "last_successful_run_at_jst": state.get("last_successful_run_at_jst"),
        "next_run_at_jst": next_run_text,
        "active_runs": active_runs,
        "stale_runs": stale_runs,
        "alerts": alerts,
        "requires_attention": status in {"MISSED", "STALE_RUN"} or bool(alerts),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", nargs="?")
    parser.add_argument("--at", required=True)
    parser.add_argument("--grace-minutes", type=int, default=120)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = watchdog_status(at=args.at, grace_minutes=args.grace_minutes)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] in {"MISSED", "STALE_RUN"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
