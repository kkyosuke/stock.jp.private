"""Reproduce this allocation diagnostic from pinned local sources, without network."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from stock_jp.market_data.storage import manifest_bytes, member_bytes
from stock_jp.research.allocation import run_replay, write_outputs, load_splits
from datetime import date
from stock_jp.workspace import project_root

ROOT = project_root().resolve()
RESULT = Path(__file__).resolve().parent
ARCHIVE = ROOT / ".work/validation-2026-10-05/price-archive"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(name, value):
    (RESULT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prepare_prices():
    register = json.loads((RESULT / "price-input-register.json").read_text())
    store = ROOT / "data/market/jquants"
    pointer = dict(version=2, generation=register["adopted_generation"])
    members = json.loads(manifest_bytes(store, pointer))["files"]
    for row in register["files"]:
        path = Path(row["path"])
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("unsafe price input path")
        if row["path"].startswith(".work/market-data/"):
            name = path.relative_to(".work/market-data").as_posix()
            payload = member_bytes(store, pointer, members[name])
        else:
            payload = (ROOT / path).read_bytes()
        if hashlib.sha256(payload).hexdigest() != row["sha256"]:
            raise ValueError(f"price checksum mismatch: {row['date']}")
        target = ARCHIVE / row["date"][:4] / (row["date"] + ".csv")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != payload:
                raise ValueError(f"scratch archive differs: {row['date']}")
        else:
            target.write_bytes(payload)
    expected = {row["date"] for row in register["files"]}
    if {path.stem for path in ARCHIVE.glob("*/*.csv")} != expected:
        raise ValueError("scratch archive contains unregistered sessions")
    return register


def primary_sources():
    paths = set()
    company = json.loads((RESULT / "company-primary-sources.json").read_text())
    market = json.loads((RESULT / "market-primary-sources.json").read_text())
    if digest(ROOT / company["feed_path"]) != company["feed_sha256"]:
        raise ValueError("company feed hash mismatch")
    for row in [*company["documents"], *market["sources"]]:
        path = ROOT / row["path"]
        if not path.resolve().is_relative_to(ROOT) or digest(path) != row["sha256"]:
            raise ValueError("primary source hash mismatch")
        paths.add(row["path"])
    return company, market, paths


def benchmark_and_calendar(sessions):
    market = json.loads((RESULT / "market-components.json").read_text())
    topix, growth = {}, {}
    for month in market["jpx_index_months"]:
        if month["status"] != "PARSED":
            raise ValueError("JPX month is not parsed")
        for key, target in (("topix", topix), ("growth250", growth)):
            for row in month[key]:
                if row["date"] in target:
                    raise ValueError("duplicate benchmark session")
                target[row["date"]] = row["value"]
    days = [day.isoformat() for day in sessions]
    calendar_days = sorted(day for day in topix if days[0] <= day <= days[-1])
    if calendar_days != days or any(day not in growth for day in days):
        raise ValueError("price sessions differ from independent JPX daily index sessions")
    selection = "2024-12-30"
    with (RESULT / "benchmark.csv").open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["date", "topix_price_index", "growth250_price_index"])
        for day in [selection, *days]:
            writer.writerow([day, topix[day], growth[day]])
    return {"source": "JPX official monthly statistics table 3-1",
            "price_calendar_verified_sessions": len(days), "missing_sessions": [],
            "from_close": selection, "through_close": days[-1],
            "topix_price_return_pct": (topix[days[-1]] / topix[selection] - 1) * 100,
            "growth250_price_return_pct": (growth[days[-1]] / growth[selection] - 1) * 100,
            "dividends_fees_taxes_included": False,
            "point_in_time_mrs_inputs_certified": False}


def coverage(register, sessions, company, market, primary_paths):
    required = []
    monthly = {day.strftime("%Y-%m"): day for day in [date(2024, 12, 30), *sessions]}
    assessment_path = ROOT / "research/historical-replay-2025-2026/input/assessments.jsonl"
    assessments = [json.loads(line) for line in assessment_path.read_text().splitlines() if line.strip()]
    for month, day in sorted(monthly.items()):
        path = ARCHIVE / str(day.year) / f"{day}.csv"
        with path.open() as stream:
            codes = {row["銘柄コード"] for row in csv.DictReader(stream)
                     if row["市場・商品区分"] in {"プライム", "スタンダード", "グロース",
                        "プライム（内国株式）", "スタンダード（内国株式）", "グロース（内国株式）"}}
        required.append(dict(month=month, evaluation_date=str(day), price_observed_domestic_issues=len(codes)))
    tables = {}
    for role in ("financial", "share-count", "forecast", "earnings-calendar"):
        folder = ROOT / f"data/market/observations/{role}-history"
        counts = {}
        for path in sorted(folder.glob("20[0-9][0-9].csv")):
            with path.open() as stream:
                counts[path.stem] = sum(1 for _ in csv.DictReader(stream))
        tables[role] = dict(by_year=counts, total=sum(counts.values()))
    return dict(status="FORMAL_STRATEGY_REPLAY_BLOCKED", price_files=len(register["files"]),
                replay_sessions=len(sessions), price_source_hashes_verified=True,
                full_universe_point_in_time_certified=False,
                monthly_price_observed_universe=required,
                monthly_price_observed_issue_evaluations=sum(row["price_observed_domestic_issues"] for row in required),
                full_rule_assessments_provided=len(assessments), historical_tables=tables,
                company_documents_collected=len(company["documents"]),
                company_document_pages=sum(row["pages"] for row in company["documents"]),
                market_sources_collected=len(market["sources"]), unique_primary_blobs=len(primary_paths),
                market_sources_original_historical_vintage_verified=False,
                outstanding_inputs=[
                    "当時の全銘柄security masterと廃止・合併の確定記録。日足に登場しない銘柄も必要。",
                    "全月末・全母集団のハードゲート、100点採点、SAM/SOM、競合比較、10倍逆算。",
                    "公表当時の財務原本・訂正履歴、潜在株式、粗利益、非定常損益・CFの調整。",
                    "全社の重要開示、四半期・即時判定、未約定・停止・部分利確・出口条件。",
                    "過去月末で公表済みのMRS全入力。月次PDFの当月値は翌月公開でその月末には使えない。",
                    "2025年初のVI算定に必要な2022年系列と、各月末当時公表済みのCI vintage。",
                    "v0.4発効後に事前固定した未観測期間のholdoutと本人の承認。"],
                recovery_drill_required=False, recovery_drill_performed=False,
                live_promotion_approved=False)


def main():
    register = prepare_prices()
    company, market, primary_paths = primary_sources()
    sensitivity = []
    sessions = None
    for fee, prefix in ((0.0015, "continuous"), (0.0, "fee-000"), (0.005, "fee-050")):
        candidates, eligible, sessions, _, runs = run_replay(
            archive=ARCHIVE, initial_capital=10_000_000, selection_day=date(2024, 12, 30),
            start=date(2025, 1, 6), end=date(2026, 8, 31),
            splits=load_splits(RESULT / "splits.json"), fee_rate=fee)
        write_outputs(candidates=candidates, eligible_count=eligible, sessions=sessions, runs=runs,
                      initial_capital=10_000_000, selection_day=date(2024, 12, 30), fee_rate=fee,
                      archive=ARCHIVE.relative_to(ROOT), splits_path=(RESULT / "splits.json").relative_to(ROOT),
                      **{f"{role}_path": (RESULT / f"{prefix}-{name}").relative_to(ROOT)
                         for role, name in (("candidates", "candidates.csv"), ("trades", "trades.csv"),
                                            ("daily", "daily.csv"), ("summary", "summary.json"),
                                            ("report", "allocation.md"))})
        sensitivity.append(dict(fee_rate=fee, results={run.rule_version: run.summary for run in runs}))
        print(f"replayed {len(sessions)} sessions, cost {fee:.2%}", flush=True)
    benchmark = benchmark_and_calendar(sessions)
    save("benchmark-summary.json", benchmark)
    save("fee-sensitivity.json", dict(status="ALLOCATION_DIAGNOSTIC_ONLY", results=sensitivity))
    save("coverage-audit.json", coverage(register, sessions, company, market, primary_paths))
    completed = subprocess.run([sys.executable, "-m", "stock_jp", "research", "replay", "validate"],
                               cwd=ROOT, capture_output=True, text=True, check=False)
    if completed.returncode != 1:
        raise ValueError("formal validator unexpectedly changed result; review rather than accepting it")
    save("formal-validation.json", dict(exit_code=completed.returncode, result=json.loads(completed.stdout)))
    attempted = subprocess.run([sys.executable, "-m", "stock_jp", "research", "replay", "run",
                               "--output-dir", str(RESULT / "formal-output")],
                              cwd=ROOT, capture_output=True, text=True, check=False)
    if attempted.returncode != 1 or (RESULT / "formal-output").exists():
        raise ValueError("formal replay unexpectedly produced outputs with incomplete inputs")
    save("formal-run.json", dict(exit_code=attempted.returncode, result=json.loads(attempted.stdout)))
    with (RESULT / "continuous-daily.csv").open() as stream:
        control = next(row for row in csv.DictReader(stream) if row["date"] == "2025-12-30")
    old = json.loads((ROOT / "research/allocation-replay-2025/summary.json").read_text())
    for version, key in (("v0.2", "v02_nav"), ("v0.4", "v04_nav")):
        if float(control[key]) != old["results"][version]["final_nav"]:
            raise ValueError("2025 control does not reproduce the previously frozen result")
    save("provenance.json", dict(base_main_commit="82f3a2ca5324c260b4dab779d43e923883d6ec2f",
         adopted_generation=register["adopted_generation"], reproduction_network_requests=0,
         jquants_requests_this_task=0,
         corporate_action_source="splits.json", baseline_2025_reproduced=True,
         inputs={name: digest(RESULT / name) for name in ("price-input-register.json", "splits.json",
                   "company-primary-sources.json", "market-primary-sources.json", "market-components.json")},
         code={name: digest(ROOT / name) for name in ("src/stock_jp/research/allocation.py",
                    "src/stock_jp/research/historical_replay.py", "src/stock_jp/market_data/regime_sources.py",
                    "research/validation-2026-10-05/reproduce.py", "research/validation-2026-10-05/plot.py")},
         outputs={path.name: digest(path) for path in sorted(RESULT.glob("*"))
                  if path.suffix in {".csv", ".json"} and path.name != "provenance.json"}))


if __name__ == "__main__":
    main()
