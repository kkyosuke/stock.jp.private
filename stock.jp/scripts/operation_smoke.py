"""Run a disposable multi-day state-transition smoke simulation without network or trades."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from scripts.daily_operation import prepare_run
    from scripts.nightly_operation import finalize_nightly_run
    from scripts.operation_state import (
        initialize_or_migrate_workspace,
        validate_workspace,
    )
except ModuleNotFoundError:  # Direct execution from scripts/
    from daily_operation import prepare_run
    from nightly_operation import finalize_nightly_run
    from operation_state import initialize_or_migrate_workspace, validate_workspace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
JST = ZoneInfo("Asia/Tokyo")


def _business_dates(start: date, count: int) -> list[date]:
    if count <= 0:
        raise ValueError("days must be positive")
    result: list[date] = []
    candidate = start
    while len(result) < count:
        if candidate.weekday() < 5:
            result.append(candidate)
        candidate += timedelta(days=1)
    return result


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _complete_artifacts(run_dir: Path, run_date: date) -> None:
    run_id = run_date.isoformat()
    cutoff = datetime.combine(run_date, time(18, 30), tzinfo=JST).isoformat(
        timespec="seconds"
    )
    completed = datetime.combine(run_date, time(19, 5), tzinfo=JST).isoformat(
        timespec="seconds"
    )
    evidence_id = f"jpx-smoke-{run_id}"
    with (run_dir / "sources.csv").open("a", encoding="utf-8", newline="") as destination:
        writer = csv.writer(destination, lineterminator="\n")
        for category in ("tdnet", "edinet", "jpx"):
            writer.writerow(
                [
                    evidence_id if category == "jpx" else f"{category}-smoke-{run_id}",
                    category,
                    "",
                    f"{category} smoke coverage",
                    datetime.combine(run_date, time(18, 0), tzinfo=JST).isoformat(timespec="seconds"),
                    datetime.combine(run_date, time(18, 45), tzinfo=JST).isoformat(timespec="seconds"),
                    f"https://example.com/{category}/{run_id}",
                    "true",
                    "true",
                    "offline state-transition smoke fixture",
                ]
            )

    health_path = run_dir / "provider-health.json"
    health = json.loads(health_path.read_text(encoding="utf-8"))
    health.update(
        {
            "status": "COMPLETED",
            "started_at_jst": datetime.combine(run_date, time(18, 31), tzinfo=JST).isoformat(timespec="seconds"),
            "completed_at_jst": datetime.combine(run_date, time(18, 45), tzinfo=JST).isoformat(timespec="seconds"),
        }
    )
    _write_json(health_path, health)
    queue_path = run_dir / "research-queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    queue["generated_at_jst"] = health["completed_at_jst"]
    _write_json(queue_path, queue)

    coverage_path = run_dir / "coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    coverage["status"] = "COMPLETED"
    coverage["source_window"]["through_inclusive_jst"] = cutoff
    coverage["completed_at_jst"] = completed
    for item in coverage["universe"].values():
        item["checked"] = list(item["expected"])
    for source in coverage["official_sources"].values():
        source["status"] = "CHECKED" if source["required"] else "NOT_APPLICABLE"
    _write_json(coverage_path, coverage)

    plan_path = run_dir / "work-plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["status"] = "COMPLETED"
    for task in plan["tasks"]:
        task["status"] = "COMPLETED"
        task["evidence_source_ids"] = [evidence_id]
    _write_json(plan_path, plan)
    (run_dir / "research-results.md").write_text(
        f"# 夜間調査結果 {run_id}\n\n- 状態: `COMPLETED`\n"
        f"- 情報カットオフ（JST）: {cutoff}\n- 翌営業日: 対象なし\n"
        "- 対象件数: 0\n- 未解決事項: なし\n\n"
        "## 調査結果\n\nオフライン状態遷移試験を完了。\n",
        encoding="utf-8",
    )
    (run_dir / "global-risk.md").write_text(
        "# 世界情勢・市場リスク確認\n\n- 状態: `COMPLETED`\n"
        f"- 情報カットオフ（JST）: {cutoff}\n- 判定: `NORMAL`\n\n"
        "オフライン状態遷移試験のため外部判断は行わない。\n",
        encoding="utf-8",
    )
    report_path = run_dir / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report = (
        report.replace("- 実行状態: `IN-PROGRESS`", "- 実行状態: `COMPLETED`")
        .replace("- 今回の開示カットオフ（JST）: 未確定", f"- 今回の開示カットオフ（JST）: {cutoff}")
        .replace("- 株価基準日: 未確定", f"- 株価基準日: {run_id}")
        .replace("- 総合結果: 未確定", "- 総合結果: オフライン試験完了")
    )
    report_path.write_text(report, encoding="utf-8")


def simulate_operations(
    *, days: int = 20, start_date: str = "2026-09-01", root: Path = PROJECT_ROOT
) -> dict[str, Any]:
    first = date.fromisoformat(start_date)
    run_dates = _business_dates(first, days)
    with tempfile.TemporaryDirectory(prefix=".nightly-smoke-", dir=root) as temporary:
        sandbox = Path(temporary)
        (sandbox / "operations").mkdir()
        shutil.copytree(root / "operations/templates", sandbox / "operations/templates")
        initialize_or_migrate_workspace(sandbox)
        for run_date in run_dates:
            at = datetime.combine(run_date, time(18, 30), tzinfo=JST).isoformat(timespec="seconds")
            prepared = prepare_run(at=at, root=sandbox)
            run_dir = sandbox / str(prepared["run_dir"])
            _complete_artifacts(run_dir, run_date)
            finalize_nightly_run(
                run_id=run_date.isoformat(),
                run_token=str(prepared["run_token"]),
                completed_at=datetime.combine(run_date, time(19, 5), tzinfo=JST).isoformat(timespec="seconds"),
                source_cutoff=datetime.combine(run_date, time(18, 30), tzinfo=JST).isoformat(timespec="seconds"),
                price_date=run_date.isoformat(),
                summary="offline smoke completed",
                root=sandbox,
            )
        errors = validate_workspace(sandbox)
        state = json.loads(
            (sandbox / "operations/private/state.json").read_text(encoding="utf-8")
        )
        with (sandbox / "operations/private/run-history.csv").open(
            encoding="utf-8", newline=""
        ) as source:
            history = list(csv.DictReader(source))
        return {
            "status": "PASSED" if not errors and len(history) == days else "FAILED",
            "days_requested": days,
            "completed_runs": len(history),
            "first_run_id": run_dates[0].isoformat(),
            "last_run_id": state.get("last_run_id"),
            "consecutive_successful_runs": state.get("consecutive_successful_runs"),
            "next_run_at_jst": state.get("next_run_at_jst"),
            "validation_errors": errors,
            "sandbox_removed_after_test": True,
            "broker_orders_submitted": 0,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=20)
    parser.add_argument("--start-date", default="2026-09-01")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = simulate_operations(days=args.days, start_date=args.start_date)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
