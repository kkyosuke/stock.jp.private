"""Start real PAPER research and preserve unfinished review without advancing cutoff."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_jp.market_data.primary_sources import confined
from stock_jp.operation.nightly import start_nightly_run
from stock_jp.operation.readiness import check_readiness
from stock_jp.operation.runs import fail_run
from stock_jp.workspace import project_root

JST = ZoneInfo("Asia/Tokyo")


def prepare(root: Path, *, at: datetime | None = None, allow_before_publication=False, cutoff=None, logical_target=None, budget_remaining=None):
    at = at or datetime.now(JST)
    if at.tzinfo is None:
        raise ValueError("PAPER preparation time must have a UTC offset")
    at = at.astimezone(JST)
    policy = json.loads((root / "config/policy.json").read_text())
    if policy.get("operation_mode") != "PAPER" or policy.get("broker_submission") != "HUMAN_ONLY":
        raise ValueError("automatic research preparation requires PAPER/HUMAN_ONLY")
    day = at.date().isoformat()
    if logical_target is not None:
        from stock_jp.automation.nightly import scheduled_day
        day = logical_target["day"]
        selected = datetime.fromisoformat(logical_target["cutoff_at_jst"])
        if (selected.tzinfo is None or selected > at or scheduled_day(selected).isoformat() != day
                or day > scheduled_day(at).isoformat()):
            raise ValueError("invalid logical PAPER target or publication cutoff")
        if cutoff and cutoff != logical_target["cutoff_at_jst"]:
            raise ValueError("restored PAPER cutoff differs from logical target")
        cutoff = logical_target["cutoff_at_jst"]
    run_dir = root / "operations/runs" / day
    handoff_path = run_dir / "handoff.json"
    if handoff_path.is_file():
        handoff = json.loads(handoff_path.read_text())
        if (handoff.get("run_id") != run_dir.name
                or handoff.get("operation_mode") != policy["operation_mode"]
                or handoff.get("active_rule_version") != policy.get("active_rule_version")
                or handoff.get("status") not in {"completed", "failed", "in_progress"}):
            raise ValueError("saved run identity or policy differs from current PAPER policy")
        completed = handoff["status"] == "completed"
        # A retry must not regenerate canonical review work or acquire its lease.
        # The operator resumes the saved artifacts using main's offline entry.
        return {"status": "ALREADY_COMPLETED" if completed else "REVIEW_REQUIRED",
                "completed_run": completed, "existing_run_preserved": True,
                "source_cutoff_preserved": True,
                "run": {"run_id": run_dir.name, "status": handoff["status"],
                        "run_dir": run_dir.relative_to(root).as_posix()},
                "review_command": None if completed else [
                    "python", "-m", "stock_jp", "operation", "run", "resume-review",
                    "--run-id", run_dir.name, "--at", at.isoformat(timespec="seconds")],
                "interpretation": "Saved canonical work is unchanged. Resume its review offline; no provider calls or investment-readiness claim."}
    if logical_target and logical_target["daily_status"] == "HOLIDAY":
        return {"status": "HOLIDAY", "completed_run": False, "logical_target": logical_target}
    if budget_remaining is not None and budget_remaining <= 0:
        return {"status": "BUDGET_EXHAUSTED", "completed_run": False, "logical_target": logical_target,
                "interpretation": "No scan started. Keep saved sources for offline review; no budget reset or completion claim."}
    readiness = check_readiness(root=root, at=at.isoformat(timespec="seconds"))
    if not readiness["paper_go"]:
        return {"status": "INPUTS_BLOCKED", "readiness": readiness, "completed_run": False}
    if logical_target is None and at.hour < 21 and not allow_before_publication:
        return {"status": "WAIT_PUBLICATION", "completed_run": False,
                "interpretation": "Real nightly preparation starts after the declared 21:00 JST publication window."}
    cutoff_path = run_dir / "source-scan-cutoff.json"
    if cutoff_path.exists():
        existing = json.loads(cutoff_path.read_text())["cutoff_at_jst"]
        if cutoff and existing != cutoff:
            raise ValueError("existing PAPER cutoff differs; inspect the saved run before resuming")
        cutoff = existing
    cutoff = cutoff or at.isoformat(timespec="seconds")
    selected = datetime.fromisoformat(cutoff)
    if selected.tzinfo is None:
        raise ValueError("PAPER cutoff must have a UTC offset")
    selected = selected.astimezone(JST)
    if logical_target is None and (selected.date() != at.date() or selected > at or selected.hour < 21 and not allow_before_publication):
        raise ValueError("PAPER resume cutoff must be from this publication night or an explicit preview")
    # One scan uses the configured invocation budget, including retries.
    # Recreating its client after a budget pause would multiply that allowance.
    started = start_nightly_run(root=root, at=at.isoformat(timespec="seconds"), cutoff=cutoff,
                                **({"target_run_id": day} if logical_target else {}))
    public = {k: v for k, v in started.items() if k != "run_token"}
    if started["status"] == "completed":
        return {"status": "ALREADY_COMPLETED", "completed_run": True, "run": public}
    if started["status"] == "locked":
        return {"status": "LOCKED", "completed_run": False, "run": public}
    # A source scan cannot approve manual checks or stock judgements.
    closed = fail_run(root=root, run_id=started["run_id"], run_token=started["run_token"],
                      completed_at=datetime.now(JST).isoformat(timespec="seconds"),
                      summary="PAPER research prepared; primary-source and due-review completion required; no orders proposed")
    public["status"] = closed["status"]
    manifest = run_dir / "reference-data/manifest.json"
    checkpoint = json.loads(manifest.read_text()).get("jquants_checkpoint", {}) if manifest.exists() else {}
    return {"status": "REVIEW_REQUIRED", "completed_run": False, "run": public,
            "attempt": closed, "jquants_checkpoint": {"request_count": checkpoint.get("request_count", 0),
                "cache_hits": checkpoint.get("cache_hits", 0)}, "source_cutoff_preserved": True,
            "interpretation": "Review and finalize the saved work plan. Preparation is not a completed PAPER run or a holdout evaluation."}


def main(*, logical_target=None, budget_remaining=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-before-publication", action="store_true", help="owner's explicit preview only")
    parser.add_argument("--resume-receipt", type=Path, help="validated query-cache restore receipt")
    args = parser.parse_args()
    root = project_root()
    output = confined(root, str(args.output))
    receipt = json.loads(confined(root, str(args.resume_receipt)).read_text()) if args.resume_receipt else {}
    result = prepare(root, allow_before_publication=args.allow_before_publication,
                     cutoff=receipt.get("resume_cutoff_at_jst"), logical_target=logical_target, budget_remaining=budget_remaining)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "completed_run": result["completed_run"]}))
    return int(not result["completed_run"])


if __name__ == "__main__":
    raise SystemExit(main())
