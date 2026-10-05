"""Replay observable exit branches on a fixed, source-bound diagnostic cohort."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import asdict
from datetime import date
import hashlib
import io
import json
import math
from pathlib import Path

from stock_jp.research.allocation import load_splits
from stock_jp.research.financial_inputs import FrozenStore, local_path
from stock_jp.research.price_episodes import Price
from stock_jp.research.price_exit import simulate_episode
from stock_jp.strategy.price_rules import simulate_v03_price_rules
from stock_jp.workspace import project_root


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def build(root, *, register_path, candidate_path, split_path, start, through, output):
    register_path, candidate_path, split_path = (p.resolve() for p in (register_path, candidate_path, split_path))
    if any(not p.is_relative_to(root.resolve()) for p in (register_path, candidate_path, split_path)):
        raise ValueError("exit inputs must stay inside the workspace")
    if not output.resolve().is_relative_to((root / "research").resolve()):
        raise ValueError("exit output must stay under research")
    register = json.loads(register_path.read_text())
    store = FrozenStore(root, register["adopted_generation"])
    with candidate_path.open(encoding="utf-8", newline="") as stream:
        candidates = list(csv.DictReader(stream))
    codes = {row["code"] for row in candidates}
    if len(codes) != len(candidates):
        raise ValueError("duplicate diagnostic cohort code")
    if any(date.fromisoformat(row["selection_date"]) >= start for row in candidates):
        raise ValueError("cohort must be fixed before entry")
    splits = load_splits(split_path)
    prices = {code: [] for code in codes}
    sessions, source_hashes = [], {}
    for source in register["files"]:
        day = date.fromisoformat(source["date"])
        if not start <= day <= through:
            continue
        relative = source["path"]
        raw = store.read(relative.removeprefix("adopted-store:")) if relative.startswith("adopted-store:") else local_path(root, relative).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != source["sha256"]:
            raise ValueError("exit price checksum mismatch")
        source_hashes[relative] = digest
        found = set()
        for row in csv.DictReader(io.StringIO(raw.decode("utf-8"))):
            code = row["銘柄コード"]
            if code not in codes:
                continue
            if code in found or row["日付"] != str(day) or row["取得状態"] != "OK" or not row["終値"]:
                raise ValueError("ambiguous or missing cohort close; cannot silently skip")
            found.add(code)
            close = float(row["終値"])
            if not math.isfinite(close) or close <= 0:
                raise ValueError("cohort close must be finite and positive")
            # Normalize to terminal split units. PA and Q0 use the same units;
            # this transforms the path without changing the underlying prices.
            for split in splits:
                if split.code == code and day < split.price_adjustment_day <= through:
                    close *= split.denominator / split.numerator
            prices[code].append(Price(day, close, int(row["売買高(株)"])))
        if found != codes:
            raise ValueError(f"missing cohort closes on {day}; stop the comparison")
        sessions.append(day)
    if not sessions or sessions != sorted(set(sessions)) or sessions[0] != start or sessions[-1] != through:
        raise ValueError("exit calendar endpoints or order mismatch")
    results, trades = [], []
    for candidate in candidates:
        code = candidate["code"]
        values = prices[code]
        v02 = simulate_episode(code=code, name=candidate["name"], label="PRICE_PROXY_ONLY",
                               evaluation_day=date.fromisoformat(candidate["selection_date"]),
                               entry_day=start, entry_price=values[0].close,
                               prices=values, trading_days=sessions)
        v03 = simulate_v03_price_rules(entry_day=start, entry_price=values[0].close, prices=values)
        for version, simulation in (("v0.2", v02), ("v0.3", v03), ("v0.4", v02)):
            entry = values[0].close
            gross = sum(t.fraction_of_q0 * t.execution_price / entry for t in simulation.trades) + simulation.remaining_fraction * values[-1].close / entry
            sell_fees = sum(t.fraction_of_q0 * t.execution_price / entry * 0.0015 for t in simulation.trades)
            net = (gross - sell_fees) / 1.0015
            hold_net = (values[-1].close / entry) / 1.0015
            results.append({"version": version, "code": code, "company": candidate["name"],
                            "entry_date": str(start), "entry_price_terminal_split_units": entry,
                            "terminal_date": str(through), "gross_multiple": gross,
                            "net_multiple_marked_open_positions": net, "hold_net_multiple": hold_net,
                            "difference_vs_hold": net - hold_net,
                            "remaining_fraction": simulation.remaining_fraction,
                            "trade_count": len(simulation.trades), "pending_rule": simulation.pending_rule,
                            "full_exit_validation": False})
            for trade in simulation.trades:
                row = asdict(trade)
                if row["execution_day"] <= row["trigger_day"]:
                    raise ValueError("exit cannot execute at the trigger close")
                trades.append({"version": version, "code": code, **{key: str(value) if isinstance(value, date) else value for key, value in row.items()}})
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "positions.csv", results)
    if trades:
        write_csv(output / "trades.csv", trades)
    else:
        (output / "trades.csv").write_text("version,code,rule,trigger_day,execution_day,fraction_of_q0,execution_price\n")
    summary = {
        "schema_version": "1.0", "status": "OBSERVABLE_EXIT_BRANCH_DIAGNOSTIC",
        "period": {"from": str(start), "through": str(through)}, "sessions": len(sessions),
        "cohort_count": len(candidates), "cohort_basis": "2024-12-30 frozen price/liquidity proxy; not approved formal entries",
        "formal_exit_validation_completed": False, "forward_holdout_claimed": False,
        "fees": {"entry_rate": 0.0015, "exit_rate": 0.0015, "tax": "NOT_INCLUDED", "spread_and_slippage": "NOT_INCLUDED"},
        "executed_rule_counts": dict(sorted(Counter(row["version"] + ":" + row["rule"] for row in trades).items())),
        "unverified_rules": ["S-A1..A6 disclosure and formal score", "S-B1..B5 quarterly inputs", "S-C2 and score-dependent branches of C4/C5", "S-D3/D5 formal rescore", "S-D6/V3-P6 portfolio concentration"],
        "right_censored_rules": ["S-C5 504 sessions", "S-C6/V3-P2 three calendar years", "tenbagger three-year success labels"],
        "interpretation": "A missing financial exit can invalidate the later hypothetical price path. These trades are counterfactual branch checks, not actual eligible strategy trades or performance evidence.",
        "versions": {version: {"aggregate_equal_entry_unit_net_multiple": sum(row["net_multiple_marked_open_positions"] for row in results if row["version"] == version),
                                "aggregate_hold_multiple": sum(row["hold_net_multiple"] for row in results if row["version"] == version),
                                "exit_count": sum(row["trade_count"] for row in results if row["version"] == version)} for version in ("v0.2", "v0.3", "v0.4")},
        "source_hashes": source_hashes, "generation": register["adopted_generation"],
        "input_hashes": {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in (register_path, candidate_path, split_path)},
    }
    (output / "exit-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--price-register", required=True, type=Path)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--splits", required=True, type=Path)
    parser.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
    parser.add_argument("--through", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = build(project_root(), register_path=args.price_register, candidate_path=args.candidates,
                   split_path=args.splits, start=args.start, through=args.through, output=args.output)
    print(json.dumps({key: result[key] for key in ("status", "sessions", "cohort_count", "executed_rule_counts", "versions")}, ensure_ascii=False))
    return 0
