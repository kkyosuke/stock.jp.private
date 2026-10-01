"""Read-only operational health report; a successful data sync is not a nightly run."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
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


def remote_alerts(private_repo: str, public_repo: str, now: datetime) -> list[str]:
    # Observe persisted inputs, not retired collection/auto-merge workflow runs.
    tip = github(f"{public_repo}/commits/main")
    pointer = github(f"{private_repo}/contents/stock.jp?ref=main")
    if tip["sha"] != pointer["sha"] and now - parse_time(tip["commit"]["committer"]["date"]) > timedelta(hours=3):
        return ["public main differs from the reviewed private pointer; assistant review required"]
    return []


def command_json(script: str, at: str, action: str = "check") -> dict:
    # bootstrap.check may migrate state. Validate a disposable copy, never the
    # canonical checkout, and reject any implicit migration instead of adopting it.
    with tempfile.TemporaryDirectory(prefix="stock-health-") as directory:
        scratch = Path(directory)
        public = scratch / "stock.jp"
        shutil.copytree(ROOT / "stock.jp", public,
                        ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "private", "__pycache__"))
        private = scratch / "operations/private"
        shutil.copytree(ROOT / "operations/private", private)
        (public / "operations").mkdir(exist_ok=True)
        (public / "operations/private").symlink_to(private, target_is_directory=True)
        def snapshot():
            return {str(p.relative_to(private)): p.read_bytes() for p in private.rglob("*") if p.is_file()}
        before = snapshot()
        command = [sys.executable, str(public / "scripts" / script), action]
        if action == "check":
            command.extend(["--at", at])
        result = subprocess.run(command, text=True, capture_output=True, timeout=180, cwd=public)
        if snapshot() != before:
            raise RuntimeError(f"{script} requires migration; assistant review required")
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

