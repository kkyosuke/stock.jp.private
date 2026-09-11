"""Read-only operational health report; a successful data sync is not a nightly run."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
JST = ZoneInfo("Asia/Tokyo")


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone is required")
    return result.astimezone(JST)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def workflow_alerts(name: str, runs: list, now: datetime, max_age_hours: int) -> list[str]:
    eligible = [r for r in runs if r["head_branch"] == "main" and r["event"] in {"schedule", "workflow_dispatch"}]
    if not eligible:
        return [f"{name}: no main workflow runs"]
    latest = max(eligible, key=lambda r: r["created_at"])
    alerts = []
    if now - parse_time(latest["created_at"]) > timedelta(hours=max_age_hours):
        alerts.append(f"{name}: no execution within {max_age_hours} hours")
    if latest["status"] == "completed" and latest["conclusion"] != "success":
        alerts.append(f"{name}: latest run is {latest['conclusion']}")
    if latest["status"] != "completed" and now - parse_time(latest["created_at"]) > timedelta(hours=3):
        alerts.append(f"{name}: run queued or active for more than 3 hours")
    return alerts


def required_price_cycle(now: datetime) -> datetime:
    # Six hours for collection, GitHub scheduling delays, checks and merge.
    cutoff = now.astimezone(JST) - timedelta(hours=6)
    cycle = cutoff.replace(hour=18, minute=30, second=0, microsecond=0)
    if cycle > cutoff:
        cycle -= timedelta(days=1)
    while cycle.weekday() >= 5:
        cycle -= timedelta(days=1)
    return cycle


def local_report(state: dict, watchdog: dict, readiness: dict) -> dict:
    alerts = list(watchdog.get("alerts", []))
    if watchdog.get("status") in {"MISSED", "STALE_RUN", "NEEDS_FIRST_RUN"}:
        alerts.append(f"nightly: {watchdog['status']}")
    if readiness.get("paper_go") is not True:
        alerts.extend(readiness.get("blockers") or ["PAPER readiness failed"])
    if readiness.get("broker_submission") != "HUMAN_ONLY":
        alerts.append("broker submission boundary must be HUMAN_ONLY")
    return {
        "alerts": alerts,
        "nightly_status": watchdog.get("status"),
        "last_run_id": state.get("last_run_id"),
        "last_successful_run_at_jst": state.get("last_successful_run_at_jst"),
        "next_run_at_jst": state.get("next_run_at_jst"),
        "pending_reviews": state.get("pending_reviews", []),
        "open_data_gap_count": len(state.get("data_gaps", [])),
        "readiness_status": readiness.get("status"),
        "operation_mode": readiness.get("operation_mode"),
        "broker_submission": readiness.get("broker_submission"),
    }


def github(path: str):
    request = Request(f"https://api.github.com/repos/{path}", headers={
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "stock-jp-operation-health",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def expiry_alerts(value: str | None, now: datetime, label: str) -> list[str]:
    if not value:
        return [f"{label}: expiration not reported; check token settings"]
    expiration = parse_time(value.replace(" UTC", "+00:00"))
    if expiration - now <= timedelta(days=14):
        return [f"{label}: expires within 14 days ({expiration.date().isoformat()})"]
    return []


def private_pat_alerts(repo: str, now: datetime) -> list[str]:
    token = os.environ.get("AUTOMATION_PAT")
    if not token:
        return ["AUTOMATION_PAT is missing"]
    request = Request(f"https://api.github.com/repos/{repo}", headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": "stock-jp-operation-health",
        "Accept": "application/vnd.github+json",
    })
    with urlopen(request, timeout=30) as response:
        return expiry_alerts(response.headers.get("GitHub-Authentication-Token-Expiration"), now, "private AUTOMATION_PAT")


def remote_alerts(private_repo: str, public_repo: str, now: datetime) -> list[str]:
    alerts = []
    # Public has no weekend schedule; 84 hours covers Friday -> Monday plus delays.
    for repo, workflow, hours, branch in (
        (public_repo, "daily-stock-prices.yml", 84, "automation/daily-stock-prices"),
        (private_repo, "update-public-submodule.yml", 36, "automation/update-public-submodule"),
    ):
        meta = github(f"{repo}/actions/workflows/{workflow}")
        if meta["state"] != "active":
            alerts.append(f"{repo}/{workflow}: disabled")
        runs = github(f"{repo}/actions/workflows/{workflow}/runs?branch=main&per_page=100")
        alerts.extend(workflow_alerts(f"{repo}/{workflow}", runs["workflow_runs"], now, hours))
        if repo == public_repo and not any(
            r["head_branch"] == "main" and r["event"] in {"schedule", "workflow_dispatch"}
            and r["conclusion"] == "success" and parse_time(r["created_at"]) >= required_price_cycle(now)
            for r in runs["workflow_runs"]
        ):
            alerts.append("public prices: required weekday collection has not succeeded")
        pulls = github(f"{repo}/pulls?state=open&base=main&head={repo.split('/')[0]}:{branch}&per_page=100")
        for pr in pulls:
            if pr["head"]["ref"] == branch and now - parse_time(pr["created_at"]) > timedelta(hours=3):
                alerts.append(f"{repo}: automation PR #{pr['number']} pending more than 3 hours")
    tip = github(f"{public_repo}/commits/main")
    pointer = github(f"{private_repo}/contents/stock.jp?ref=main")
    if tip["sha"] != pointer["sha"] and now - parse_time(tip["commit"]["committer"]["date"]) > timedelta(hours=3):
        alerts.append("private public pointer is behind public main for more than 3 hours")
    return alerts


def command_json(script: str, at: str) -> dict:
    command = [sys.executable, str(ROOT / "stock.jp/scripts" / script), "check", "--at", at]
    result = subprocess.run(command, text=True, capture_output=True, timeout=180)
    if result.returncode not in {0, 1}:
        raise RuntimeError(f"{script} failed (exit {result.returncode})")
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--at", default=datetime.now(JST).isoformat(timespec="seconds"))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="write the report and return success even when attention is required",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    now = parse_time(args.at)
    report = {"checked_at_jst": now.isoformat(), "alerts": []}
    try:
        state = read_json(ROOT / "operations/private/state.json")
        report.update(local_report(state, command_json("operation_watchdog.py", args.at), command_json("operation_bootstrap.py", args.at)))
        manifest = read_json(ROOT / "stock.jp/data/daily-prices/latest.json")
        report["price_date"] = manifest["latest_trading_date"]
        if not args.offline:
            report["alerts"].extend(remote_alerts(os.environ["GITHUB_REPOSITORY"], "kkyosuke/stock.jp", now))
            report["alerts"].extend(private_pat_alerts(os.environ["GITHUB_REPOSITORY"], now))
    except Exception as error:
        report["alerts"].append(f"health collection failed: {type(error).__name__}: {error}")
    report["status"] = "ATTENTION" if report["alerts"] else "OK"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("## 定期運用の状態\n\n```json\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n```\n")
    return 0 if args.report_only else (1 if report["alerts"] else 0)


if __name__ == "__main__":
    raise SystemExit(main())
