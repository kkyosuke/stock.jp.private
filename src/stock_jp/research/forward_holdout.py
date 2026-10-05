"""Freeze a future PAPER research contract and observe persisted inputs honestly."""
from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_jp.research.financial_inputs import FrozenStore
from stock_jp.research.historical_replay import ReplayInputError, evaluate_holdout, replay_version, validate_inputs
from stock_jp.workspace import project_root

JST = ZoneInfo("Asia/Tokyo")
PLAN = "operations/evidence/v04-holdout-plan.json"
MANIFEST = "research/historical-replay/v04-forward/input-manifest.json"
CODE = (
    "docs/rules/tenbagger-rule-v0.2.md", "docs/rules/tenbagger-rule-v0.3.md",
    "docs/rules/tenbagger-rule-v0.4.md", "src/stock_jp/strategy/scoring.py",
    "src/stock_jp/strategy/company.py", "src/stock_jp/strategy/regime.py",
    "src/stock_jp/strategy/sizing.py", "src/stock_jp/strategy/price_rules.py",
    "src/stock_jp/research/historical_replay.py", "src/stock_jp/research/price_exit.py",
    "src/stock_jp/research/mrs_validation.py", "src/stock_jp/research/forward_holdout.py",
    "src/stock_jp/research/financial_inputs.py", "src/stock_jp/market_data/storage.py",
    "src/stock_jp/market_data/observations.py", "src/stock_jp/market_data/prices.py",
    "src/stock_jp/market_data/jquants/snapshot.py", "src/stock_jp/market_data/jquants/financial_history.py",
    "src/stock_jp/market_data/regime_sources.py", "src/stock_jp/market_data/jpx.py",
    "src/stock_jp/research/allocation.py", "src/stock_jp/cli.py",
    ".github/workflows/forward-holdout.yml",
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now():
    return datetime.now(JST)


def freeze(root, *, start, through):
    at = now()
    if start <= at.date() or through < start:
        raise ValueError("holdout must start in an unobserved future period")
    path = root / PLAN
    document = json.loads(path.read_text())
    if document.get("status") == "FROZEN":
        raise ValueError("frozen holdout cannot be overwritten; changes need an explicit new study")
    document.update(status="FROZEN", decision="START_FORWARD_HOLDOUT",
                    frozen_at_jst=at.isoformat(timespec="seconds"),
                    declared_by="Codex: user-requested PAPER research; no owner LIVE approval",
                    period={"from": str(start), "through": str(through)})
    document["authorization"] = {
        "instruction": "正式採点・MRS・出口検証・forward holdout も行なってください",
        "scope": "PAPER_RESEARCH_ONLY", "owner_risk_acceptance": False, "live_promotion_approved": False,
    }
    document["acknowledgements"] = {key: True for key in document["acknowledgements"]}
    document["frozen_files"] = {name: sha(root / name) for name in CODE}
    document["inputs_and_execution_contract"] = {
        "universe": "T0 dated TSE domestic common stocks; excludes PRO/foreign/funds; separate v0.2 section 2 tracks",
        "price_and_master": "J-Quants V2 raw dated master, unadjusted daily bars, and corporate-action records; preserve original generations",
        "fundamental_sources": "Issuer IR/TDnet/EDINET disclosures public by T0 close; all 7 hard gates and 25 score items; NA stops selection",
        "market_regime": "MRS-v0.1: official daily TOPIX/Growth250 (J-Quants), adjusted native breadth, full three-year Nikkei VI and latest Cabinet Office CI vintage available by T0",
        "index_access": "indices_daily entitlement and replay rights must be verified before use; no paid upgrade or substitute index is authorized by this plan",
        "material_disclosures": "All-universe material-disclosure coverage must be certified; absence of a collected document is not proof of no event",
        "input_roles": ["source_register", "security_master", "trading_calendar", "daily_prices", "corporate_actions", "review_events", "market_regime", "benchmark", "assessments"],
        "source_rate_limit_per_minute": 60,
        "evaluation_cutoff": "Final TSE trading close at every completed month end; no same-close model fills",
        "model_execution": "Buy at the next eligible session open with T0 close +15% limit; sell at the next eligible session close; board lot 100; participation normal 5%/immediate 10%; fee 0.15%, slippage 0.10% separately; taxes excluded",
        "model_initial_capital_jpy": 10000000,
        "portfolio_comparison": ["v0.2", "v0.4"], "shadow_price_comparison": "v0.3",
        "retuning_count": 0, "missing_data": "WAIT; no forced trades; no synthetic run or retrospective proxy counts",
        "period_closure": "Do not extend dates or relax the 10-trade requirement after seeing outcomes; insufficient observations are INCONCLUSIVE",
        "recovery_drill_required": False, "broker_submission": "HUMAN_ONLY; this plan creates no broker order",
    }
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return document


def observe(root):
    at = now()
    plan_path = root / PLAN
    plan = json.loads(plan_path.read_text())
    if plan.get("status") != "FROZEN":
        raise ValueError("a frozen future research plan is required")
    start, through = (date.fromisoformat(plan["period"][key]) for key in ("from", "through"))
    frozen = datetime.fromisoformat(plan["frozen_at_jst"])
    if frozen.tzinfo is None or frozen.date() >= start or frozen > at:
        raise ValueError("invalid holdout freeze chronology")
    bindings = plan.get("frozen_files", {})
    if set(bindings) != set(CODE):
        raise ValueError("holdout must bind all frozen rules and execution code")
    changed = [name for name, digest in bindings.items() if not (root / name).is_file() or sha(root / name) != digest]
    policy = json.loads((root / "config/policy.json").read_text())
    if policy.get("operation_mode") != "PAPER" or policy.get("active_rule_version") != "v0.4":
        changed.append("PAPER/v0.4 operation policy")
    pointer = json.loads((root / "data/market/jquants/current.json").read_text())
    store = FrozenStore(root, pointer["generation"])
    days = sorted(name.split("/")[-2] for name in store.files
                  if name.startswith("jquants/datasets/equities_daily/") and name.endswith("/latest.json"))
    latest = days[-1] if days else None
    latest_price = None
    if latest:
        metadata = json.loads(store.read(f"jquants/datasets/equities_daily/{latest}/latest.json"))
        latest_price = {key: metadata[key] for key in ("status", "row_count", "completed_at")}
    manifest_path = root / MANIFEST
    model = quality = None
    if manifest_path.is_file():
        try:
            model, quality = validate_inputs(root=root, manifest_path=manifest_path)
            input_blockers = []
            if model["manifest"].get("evaluation_kind") != "FORWARD_HOLDOUT" or model["manifest"].get("period") != plan["period"]:
                input_blockers.append("Input manifest does not belong to this forward study")
        except ReplayInputError as error:
            input_blockers = list(error.blockers)
    else:
        input_blockers = ["Formal nine-role forward input manifest is not populated"]
    if not any(name.startswith("jquants/datasets/indices_daily/") and name.endswith("/latest.json") for name in store.files):
        input_blockers.append("Official daily Growth250 indices_daily is absent; the declared Light plan does not grant it")
    if latest_price and (latest_price["status"] != "complete" or latest_price["row_count"] <= 0):
        input_blockers.append("Adopted daily-price dataset is incomplete or empty")
    latest_day = date.fromisoformat(latest) if latest else None
    if latest_day is None or latest_day > at.date() or (min(at.date(), through) - min(latest_day, through)).days > 7:
        input_blockers.append("Adopted daily-price inputs are stale or absent")
    phase = "PRE_PERIOD_SETUP" if at.date() < start else "OBSERVING" if at.date() <= through else "AWAITING_FINAL_REPLAY"
    status = "INVALIDATED" if changed else phase
    final_evaluation = None
    if at.date() > through and not changed and not input_blockers:
        run = replay_version(model=model, rule_version="v0.4")
        final_evaluation = evaluate_holdout(plan=plan, quality=quality, v04=run.summary)
        final_evaluation["metrics"] = run.summary
        status = "COMPLETED"
    return {
        "schema_version": "1.0", "status": status, "observed_at_jst": at.isoformat(timespec="seconds"),
        "plan_path": PLAN, "plan_sha256": sha(plan_path), "period": plan["period"],
        "frozen_files_changed": changed, "generation": pointer["generation"],
        "generation_pointer_sha256": sha(root / "data/market/jquants/current.json"),
        "latest_price_date": latest, "latest_price_dataset": latest_price,
        "input_blockers": input_blockers, "formal_inputs_ready": not input_blockers,
        "formal_monthly_evaluations_credited": final_evaluation["observations"]["monthly_evaluation_count"] if final_evaluation else 0,
        "formal_trades_credited": final_evaluation["observations"]["trade_count"] if final_evaluation else 0,
        "forward_holdout_completed": final_evaluation is not None,
        "final_evaluation": final_evaluation, "live_promotion_approved": False,
        "interpretation": "Snapshot count, acquisition completion, and synthetic runs do not satisfy monthly evaluation or trade requirements. Only a validated formal replay after the frozen period closes can credit observations and evaluate the criteria.",
        "raw_pinned_inputs": store.used,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    p = subparsers.add_parser("freeze")
    p.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    p.add_argument("--through", type=date.fromisoformat, required=True)
    p = subparsers.add_parser("observe")
    p.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = project_root()
    if args.command == "freeze":
        result = freeze(root, start=args.start, through=args.through)
        print(json.dumps({key: result[key] for key in ("status", "frozen_at_jst", "period")}))
    else:
        result = observe(root)
        if not args.output.resolve().is_relative_to(root):
            raise ValueError("holdout observation output must stay inside the workspace")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({key: result[key] for key in ("status", "latest_price_date", "formal_inputs_ready", "forward_holdout_completed")}))
        if result["status"] == "INVALIDATED":
            return 2
        if result["status"] != "PRE_PERIOD_SETUP" and not result["formal_inputs_ready"]:
            return 1
    return 0
