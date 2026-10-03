"""Read-only operational health report; a successful data sync is not a nightly run."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import stat
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


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _open_directory(path):
    # Pin every ancestor; O_NOFOLLOW on the leaf alone would not prevent a
    # concurrent parent-directory symlink swap from escaping the archive.
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(path).absolute().parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def inspect_archive(source, destination=None):
    """Stream hashes through pinned no-follow descriptors; never retain raw rows."""
    result = {}
    def walk(fd, relative):
        before = os.fstat(fd)
        result[relative] = (_identity(before), None)
        if destination is not None:
            (destination / relative).mkdir(parents=True, exist_ok=False)
        for name in sorted(os.listdir(fd)):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            rel = str(Path(relative) / name)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if _identity(os.fstat(child)) != _identity(info):
                        raise ValueError('archive directory changed during inspection')
                    walk(child, rel)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(child, 'rb') as stream:
                    if _identity(os.fstat(stream.fileno())) != _identity(info):
                        raise ValueError('archive file changed before read')
                    digest = hashlib.sha256()
                    with contextlib.ExitStack() as stack:
                        output = stack.enter_context((destination / rel).open('xb')) if destination is not None else None
                        while data := stream.read(1024 * 1024):
                            digest.update(data)
                            if output is not None:
                                output.write(data)
                    if _identity(os.fstat(stream.fileno())) != _identity(info):
                        raise ValueError('archive file changed during read')
                result[rel] = (_identity(info), digest.hexdigest())
            else:
                raise ValueError('private market-data archive contains an unsafe entry')
            if _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _identity(info):
                raise ValueError('archive path changed during inspection')
        if _identity(os.fstat(fd)) != _identity(before):
            raise ValueError('archive directory changed during inspection')
    fd = _open_directory(source)
    try:
        walk(fd, '.')
        # Reopen via the path to detect ancestor/root replacement while the
        # original descriptor remained safely pinned.
        current = _open_directory(source)
        try:
            if _identity(os.fstat(current)) != result['.'][0]:
                raise ValueError('archive root path changed during inspection')
        finally:
            os.close(current)
    finally:
        os.close(fd)
    return result


def copy_archive(source: Path, destination: Path) -> None:
    before = inspect_archive(source)
    copied = inspect_archive(source, destination)
    if before != copied or before != inspect_archive(source):
        raise ValueError('canonical archive changed during copy')
    if {k:v[1] for k,v in before.items()} != {k:v[1] for k,v in inspect_archive(destination).items()}:
        raise ValueError('isolated archive hashes differ')


@contextlib.contextmanager
def reader_guard(private):
    """Honor existing writer locks without creating/chmod-ing canonical files."""
    paths = [private/'market-data-staging/daily-runner.lock', private/'market-data/jquants/.ingestion.lock']
    observed = {}
    with contextlib.ExitStack() as stack:
        for path in paths:
            if not path.exists() and not path.is_symlink():
                observed[path] = None
                continue
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('unsafe writer lock')
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('active market-data writer; readonly inspection stopped') from None
            observed[path] = (info.st_dev, info.st_ino)
        yield
        for path, identity in observed.items():
            info = path.lstat() if path.exists() or path.is_symlink() else None
            actual = (info.st_dev, info.st_ino) if info and stat.S_ISREG(info.st_mode) else None
            if actual != identity or (info is not None and not stat.S_ISREG(info.st_mode)):
                raise RuntimeError('writer lock identity changed during inspection')


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
    with reader_guard((ROOT / "operations/private").resolve()):
        archive = configured_archive(ROOT / "stock.jp") if include_market_data else None
        before = inspect_archive(archive) if archive is not None else None
        result = _command_json(script, at, action, include_market_data=include_market_data)
        if archive is not None and inspect_archive(archive) != before:
            raise RuntimeError('canonical archive changed during readonly inspection')
        return result


def _command_json(script: str, at: str, action: str = "check", *, include_market_data: bool = True) -> dict:
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
        # Normalize only the disposable copy, before taking the strict baseline.
        for path in [private, *private.rglob('*')]:
            if path.is_symlink():
                raise ValueError('unsafe isolated private entry')
            desired = 0o700 if path.is_dir() else 0o600
            if stat.S_IMODE(path.stat().st_mode) != desired:
                path.chmod(desired)
        def archive_snapshot():
            if relative_archive is None:
                return {}
            try:
                return inspect_archive(private / relative_archive)
            except (ValueError, OSError) as error:
                raise RuntimeError(f'{script} changed isolated market data') from error
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
