"""Read-only operational health report; a successful data sync is not a nightly run."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
import importlib.util
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


def configured_archive(public: Path) -> Path:
    # Use exactly the same fail-closed resolver as the operation scripts. Load
    # from this checkout rather than an unrelated installed scripts package.
    helper = public / "scripts/market_data_storage.py"
    spec = importlib.util.spec_from_file_location("health_market_data_storage", helper)
    if spec is None or spec.loader is None:
        raise RuntimeError("private market-data storage helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.resolve_archive_root(public)


def private_price_manifest() -> dict:
    public = ROOT / "stock.jp"
    archive = configured_archive(public)
    price = read_json(ROOT / "operations/private/source-config.json")["price_source"]
    if price.get("manifest_path") != "latest.json":
        raise ValueError("private price manifest_path must be latest.json")
    manifest = archive / "latest.json"
    if not manifest.resolve().is_relative_to(archive):
        raise ValueError("private price manifest escapes the configured archive")
    return read_json(manifest)


def copy_archive(source: Path, destination: Path) -> None:
    # A symlink into canonical raw storage would allow a validator regression
    # to write to the source. Keep the only raw copy in the auto-deleted private
    # temporary workspace instead. Never follow links into unrelated data.
    for path in source.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("private market-data archive contains an unsafe entry")
    shutil.copytree(source, destination)


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


def remote_alerts(private_repo: str, now: datetime) -> list[str]:
    tip = github(f"{private_repo}/commits/main")
    current = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    if tip["sha"] != current:
        return ["checkout differs from canonical private main; assistant review required"]
    return []


def command_json(script: str, at: str, action: str = "check", *, include_market_data: bool = True) -> dict:
    # bootstrap.check may migrate state. Validate a disposable copy, never the
    # canonical checkout, and reject any implicit migration instead of adopting it.
    source_private = (ROOT / "operations/private").resolve()
    if not include_market_data and (script, action) not in {
        ("operation_state.py", "validate"), ("operation_policy.py", "validate"),
        ("operation_watchdog.py", "check"),
    }:
        raise ValueError("market data may be omitted only for static state/policy validation or persisted watchdog")
    archive = configured_archive(ROOT / "stock.jp") if include_market_data else None
    relative_archive = archive.relative_to(source_private) if archive else None
    with tempfile.TemporaryDirectory(prefix="stock-health-") as directory:
        scratch = Path(directory)
        public = scratch / "stock.jp"
        shutil.copytree(ROOT / "stock.jp", public,
                        ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "private", "__pycache__", "daily-prices", "jquants-archive", "raw"))
        private = scratch / "operations/private"
        shutil.copytree(source_private, private,
                        ignore=shutil.ignore_patterns("market-data", "market-data-staging", "raw"))
        if archive is not None:
            copy_archive(archive, private / relative_archive)
        (public / "operations").mkdir(exist_ok=True)
        (public / "operations/private").symlink_to(private, target_is_directory=True)
        def snapshot():
            # State is small and tracked. Raw market data is deliberately not
            # loaded into memory, logs, reports, artifacts, or Git snapshots.
            return {str(p.relative_to(private)): p.read_bytes()
                    for p in private.rglob("*")
                    if p.is_file() and "market-data" not in p.relative_to(private).parts}
        def archive_snapshot():
            # Detect accidental writes even in the isolated archive without
            # reading raw prices into the report or the in-memory state copy.
            if relative_archive is None:
                return {}
            copied_archive = private / relative_archive
            return {str(p.relative_to(copied_archive)):
                    (p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
                    for p in copied_archive.rglob("*") if p.is_file()}
        before = snapshot()
        archive_before = archive_snapshot()
        command = [sys.executable, str(public / "scripts" / script), action]
        if action == "check":
            command.extend(["--at", at])
        result = subprocess.run(command, text=True, capture_output=True, timeout=180, cwd=public)
        if snapshot() != before:
            raise RuntimeError(f"{script} requires migration; assistant review required")
        if archive_snapshot() != archive_before:
            raise RuntimeError(f"{script} changed isolated market data; assistant review required")
        if result.returncode not in {0, 1}:
            raise RuntimeError(f"{script} failed (exit {result.returncode})")
        return json.loads(result.stdout)


def persisted_run_report(state: dict, watchdog: dict, rows: list[dict], now: datetime) -> dict:
    """Observe saved run metadata only; never assert current input readiness."""
    alerts = list(watchdog.get("alerts", []))
    if watchdog.get("status") in {"MISSED", "STALE_RUN", "NEEDS_FIRST_RUN"}:
        alerts.append(f"nightly: {watchdog['status']}")
    try:
        last = parse_time(state["last_successful_run_at_jst"])
        cutoff = parse_time(state["last_disclosure_cutoff_jst"])
        next_run = parse_time(state["next_run_at_jst"])
        if next_run <= last:
            raise ValueError("next recorded run must follow last success")
        if cutoff > last or last > now:
            raise ValueError("invalid saved run timestamps")
        if now - last > timedelta(days=4):
            alerts.append("recorded successful run is stale (more than 96 hours)")
        if not rows:
            raise ValueError("saved run history is missing")
        latest = max(rows, key=lambda row: (row.get("run_id", ""), int(row.get("attempt", "0"))))
        if latest.get("status", "").upper() != "COMPLETED":
            alerts.append("latest recorded run is not completed")
        if latest.get("run_id") != state.get("last_run_id"):
            alerts.append("saved run state and history disagree")
        if parse_time(latest["completed_at_jst"]) != last or parse_time(latest["source_cutoff_jst"]) != cutoff:
            alerts.append("saved run timestamps and history disagree")
        if latest.get("operation_mode") != "PAPER":
            alerts.append("recorded run must remain PAPER")
    except (KeyError, TypeError, ValueError) as error:
        alerts.append(f"recorded run evidence invalid or absent: {error}")
    return {
        "monitoring_scope": "PERSISTED_RUN_METADATA_ONLY",
        "runtime_readiness": "NOT_EVALUATED", "paper_go": None,
        "nightly_status": watchdog.get("status"),
        "last_run_id": state.get("last_run_id"),
        "last_successful_run_at_jst": state.get("last_successful_run_at_jst"),
        "last_disclosure_cutoff_jst": state.get("last_disclosure_cutoff_jst"),
        "next_run_at_jst": state.get("next_run_at_jst"),
        "alerts": alerts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--at", default=datetime.now(JST).isoformat(timespec="seconds"))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--persisted-only", action="store_true",
                        help="monitor saved run metadata only; never assert runtime readiness")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="write the report and return success even when attention is required",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    now = parse_time(args.at)
    report = {"checked_at_jst": now.isoformat(), "alerts": [],
              "runtime_readiness": "NOT_EVALUATED", "paper_go": None,
              "monitoring_scope": "PERSISTED_RUN_METADATA_ONLY" if args.persisted_only else "PRIVATE_RUNTIME_INPUTS"}
    try:
        state = read_json(ROOT / "operations/private/state.json")
        if args.persisted_only:
            try:
                from scripts.validate_checkout import validate_checkout
            except ModuleNotFoundError:
                from validate_checkout import validate_checkout
            checked = validate_checkout()
            if not checked["valid"]:
                raise ValueError("tracked state or PAPER policy validation failed")
            watchdog = command_json("operation_watchdog.py", args.at, include_market_data=False)
            with (ROOT / "operations/private/run-history.csv").open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            report.update(persisted_run_report(state, watchdog, rows, now))
        else:
            watchdog = command_json("operation_watchdog.py", args.at)
            readiness = command_json("operation_bootstrap.py", args.at)
            report.update(local_report(state, watchdog, readiness))
            report["runtime_readiness"] = "READY" if readiness.get("paper_go") is True else "BLOCKED"
            report["paper_go"] = readiness.get("paper_go") is True
            manifest = private_price_manifest()
            report["price_date"] = manifest["latest_trading_date"]
        if not args.offline:
            report["alerts"].extend(remote_alerts(os.environ["GITHUB_REPOSITORY"], now))
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
