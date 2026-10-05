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


def prepare(root: Path, *, at: datetime | None = None, allow_before_publication=False):
    at = at or datetime.now(JST)
    if at.tzinfo is None:
        raise ValueError("PAPER preparation time must have a UTC offset")
    at = at.astimezone(JST)
    policy = json.loads((root / "config/policy.json").read_text())
    if policy.get("operation_mode") != "PAPER" or policy.get("broker_submission") != "HUMAN_ONLY":
        raise ValueError("automatic research preparation requires PAPER/HUMAN_ONLY")
    readiness = check_readiness(root=root, at=at.isoformat(timespec="seconds"))
    if not readiness["paper_go"]:
        return {"status": "INPUTS_BLOCKED", "readiness": readiness, "completed_run": False}
    if at.hour < 21 and not allow_before_publication:
        return {"status": "WAIT_PUBLICATION", "completed_run": False,
                "interpretation": "Real nightly preparation starts after the declared 21:00 JST publication window."}
    run_dir = root / "operations/runs" / at.date().isoformat()
    cutoff = at.isoformat(timespec="seconds")
    cutoff_path = run_dir / "source-scan-cutoff.json"
    if cutoff_path.exists():
        cutoff = json.loads(cutoff_path.read_text())["cutoff_at_jst"]
    passes = []
    # The existing scanner spends at most 12 requests per invocation. Resume
    # its exact query pages, at the same cutoff, for at most three passes.
    for _ in range(3):
        pass_at = datetime.now(JST) if passes else at
        started = start_nightly_run(root=root, at=pass_at.isoformat(timespec="seconds"), cutoff=cutoff)
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
        health_path = run_dir / "provider-health.json"
        health = json.loads(health_path.read_text()) if health_path.exists() else {}
        budget_pause = any("request budget reached" in p.get("error", "") for p in health.get("providers", {}).values())
        requests = checkpoint.get("request_count", 0)
        passes.append({"attempt": closed, "request_count": requests, "cache_hits": checkpoint.get("cache_hits", 0)})
        if not budget_pause or requests == 0:
            break
    return {"status": "REVIEW_REQUIRED", "completed_run": False, "run": public,
            "attempt": closed, "passes": passes, "source_cutoff_preserved": True,
            "interpretation": "Review and finalize the saved work plan. Preparation is not a completed PAPER run or a holdout evaluation."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-before-publication", action="store_true", help="owner's explicit preview only")
    args = parser.parse_args()
    root = project_root()
    output = confined(root, str(args.output))
    result = prepare(root, allow_before_publication=args.allow_before_publication)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "completed_run": result["completed_run"]}))
    return int(not result["completed_run"])


if __name__ == "__main__":
    raise SystemExit(main())
