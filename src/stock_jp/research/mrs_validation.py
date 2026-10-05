"""Reconstruct actual MRS components, retaining every source and chronology gap."""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, time
import hashlib
import json
import math
from pathlib import Path

from stock_jp.market_data.jquants.snapshot import domestic_common_master
from stock_jp.market_data.prices import normalize_code
from stock_jp.research.financial_inputs import FrozenStore, local_path
from stock_jp.strategy.regime import MarketRegimeInput, evaluate_market_regime, nikkei_vi_window, percentile
from stock_jp.workspace import project_root
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def dataset_rows(store, dataset, day):
    name = f"jquants/datasets/{dataset}/{day}/latest.json"
    metadata = json.loads(store.read(name))
    if metadata.get("status") != "complete" or metadata.get("dataset") != dataset:
        raise ValueError("MRS dataset is incomplete or incorrectly identified")
    rows = []
    for page in metadata["pages"]:
        raw = store.read("jquants/" + page["path"])
        if hashlib.sha256(raw).hexdigest() != page["sha256"]:
            raise ValueError("MRS page checksum mismatch")
        values = json.loads(raw)["data"]
        if len(values) != page["row_count"]:
            raise ValueError("MRS page row count mismatch")
        rows.extend(values)
    if len(rows) != metadata["row_count"]:
        raise ValueError("MRS dataset row count mismatch")
    return rows


def adjusted_breadth(history, appearances, master, day):
    universe = {code for code in master if appearances.get(code, 0) >= 200}
    eligible = {code: history[code][-200:] for code in universe
                if len(history.get(code, [])) >= 200 and 0 <= (day - history[code][-1][0]).days <= 7}
    coverage = len(eligible) / len(universe) if universe else 0
    if coverage < 0.98:
        return {"status": "NA", "coverage": coverage, "reason": "200-close native history/98% coverage unavailable", "seasoned_universe": len(universe), "eligible": len(eligible)}
    above = sum(values[-1][1] >= sum(value for _, value in values) / 200 for values in eligible.values())
    return {"status": "CALCULATED", "value": above / len(eligible) * 100,
            "coverage": coverage, "seasoned_universe": len(universe), "eligible": len(eligible),
            "above_ma200": above, "excluded_short_history": len(master) - len(universe),
            "price_basis": "C multiplied by subsequent observed AdjFactor through this evaluation; future factors excluded"}


def build(root, *, price_register, market_components, ci_vintages, output):
    price_register, market_components, ci_vintages = (p.resolve() for p in (price_register, market_components, ci_vintages))
    if not output.resolve().is_relative_to((root / "research").resolve()):
        raise ValueError("MRS research output must stay under research")
    if any(not p.is_relative_to(root.resolve()) for p in (price_register, market_components, ci_vintages)):
        raise ValueError("MRS inputs must stay inside the workspace")
    register = json.loads(price_register.read_text())
    components = json.loads(market_components.read_text())
    vintages = json.loads(ci_vintages.read_text())
    for record in vintages:
        if record.get("path"):
            if hashlib.sha256(local_path(root, record["path"]).read_bytes()).hexdigest() != record["sha256"]:
                raise ValueError("CI raw vintage checksum mismatch")
    market_register = market_components.with_name("market-primary-sources.json")
    market_sources = json.loads(market_register.read_text())["sources"]
    for record in market_sources:
        if hashlib.sha256(local_path(root, record["path"]).read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("market raw source checksum mismatch")
    index = {name: {} for name in ("topix", "growth250")}
    for month in components["jpx_index_months"]:
        if month["status"] != "PARSED":
            continue
        for name in index:
            for point in month[name]:
                old = index[name].get(point["date"])
                if old is not None and old != point["value"]:
                    raise ValueError("conflicting official monthly index values")
                index[name][point["date"]] = point["value"]
    vi = components["component_results"]["nikkei_vi"]["points"]
    days = [date.fromisoformat(row["date"]) for row in register["files"]]
    evaluations = {day for day, following in zip(days, days[1:]) if day.month != following.month or day.year != following.year}
    store = FrozenStore(root, register["adopted_generation"])
    native = sorted(name.split("/")[-2] for name in store.files
                    if name.startswith("jquants/datasets/equities_daily/") and name.endswith("/latest.json"))
    history, appearances = defaultdict(list), defaultdict(int)
    breadth = {}
    for day_text in native:
        day = date.fromisoformat(day_text)
        if day > max(evaluations):
            break
        seen = set()
        for row in dataset_rows(store, "equities_daily", day_text):
            code = normalize_code(row["Code"])
            if code in seen or row["Date"] != day_text:
                raise ValueError("duplicate or incorrectly dated MRS raw bar")
            seen.add(code)
            appearances[code] += 1
            factor = row.get("AdjFactor")
            if not isinstance(factor, (int, float)) or isinstance(factor, bool) or not math.isfinite(factor) or factor <= 0:
                raise ValueError("MRS raw bar requires a valid adjustment factor")
            if factor != 1:
                history[code] = [(old_day, close * factor) for old_day, close in history[code]]
            close = row.get("C")
            if isinstance(close, (int, float)) and not isinstance(close, bool) and math.isfinite(close) and close > 0:
                history[code].append((day, float(close)))
                history[code] = history[code][-280:]
        if day in evaluations:
            master, _, _ = domestic_common_master(day_text, dataset_rows(store, "equities_master", day_text))
            breadth[day_text] = adjusted_breadth(history, appearances, master, day)
    rows = []
    for day in sorted(evaluations):
        if day < date(2024, 12, 1):
            continue
        values, bits, gaps = {}, {}, []
        for name, key in (("topix", "M1"), ("growth250", "M2")):
            points = sorted((date.fromisoformat(d), value) for d, value in index[name].items() if date.fromisoformat(d) <= day)
            if len(points) < 200 or points[-1][0] != day:
                bits[key] = None
                gaps.append(name + ": 200 daily closes including the evaluation date unavailable")
            else:
                close = points[-1][1]
                average = sum(value for _, value in points[-200:]) / 200
                field = "growth" if name == "growth250" else name
                values[field + "_close"] = close
                values[field + "_ma200"] = average
                bits[key] = int(close >= average)
        b = breadth.get(str(day), {"status": "NA", "reason": "native dated master and adjusted daily history unavailable"})
        if b["status"] == "CALCULATED":
            values["breadth_pct"] = b["value"]
            bits["M3"] = int(b["value"] >= 50)
        else:
            bits["M3"] = None
            gaps.append("breadth: " + b["reason"])
        try:
            selected_vi = nikkei_vi_window([(date.fromisoformat(p["date"]), p["value"]) for p in vi], as_of=day)
        except ValueError as error:
            selected_vi = []
            bits["M4"] = None
            gaps.append("VI: " + str(error))
        else:
            values["nikkei_vi"] = selected_vi[-1][1]
            values["nikkei_vi_p80_3y"] = percentile([value for _, value in selected_vi], 80)
            bits["M4"] = int(values["nikkei_vi"] <= values["nikkei_vi_p80_3y"])
        cutoff = datetime.combine(day, time(15, 30), tzinfo=JST)
        available = [v for v in vintages if v.get("status") == "PARSED" and datetime.fromisoformat(v["published_at_jst"]) <= cutoff]
        selected_ci = max(available, key=lambda v: v["published_at_jst"]) if available else None
        if selected_ci:
            ci_points = sorted(selected_ci["points"], key=lambda p: p["period"])
            latest = ci_points[-1]
            year, month = map(int, latest["period"].split("-"))
            n = year * 12 + month - 1 - 3
            prior_period = f"{n // 12:04d}-{n % 12 + 1:02d}"
            prior = next((p for p in ci_points if p["period"] == prior_period), None)
        else:
            prior = None
        if prior:
            values["leading_ci"] = latest["value"]
            values["leading_ci_3m_ago"] = prior["value"]
            bits["M5"] = int(latest["value"] >= prior["value"])
        else:
            bits["M5"] = None
            gaps.append("CI: contemporaneous release with exact three-month-prior value unavailable")
        result = asdict(evaluate_market_regime(MarketRegimeInput(as_of=str(day), **values))) if not gaps else None
        rows.append({"evaluation_date": str(day), "components": bits, "calculated_values": values,
                     "breadth": b, "vi_window_observations": len(selected_vi),
                     "ci_source_sha256": selected_ci["sha256"] if selected_ci else None,
                     "ci_published_at_jst": selected_ci["published_at_jst"] if selected_ci else None,
                     "retrospective_calculation": result, "numeric_gaps": gaps,
                     "formal_entry_multiplier": 0.0,
                     "formal_mrs_status": "UNAVAILABLE",
                     "chronology_blockers": ["JPX monthly index report was released after the evaluated month end; original timely daily vintage not certified"]})
    output.parent.mkdir(parents=True, exist_ok=True)
    result = {"schema_version": "1.0", "status": "RETROSPECTIVE_MRS_COMPONENT_RECONSTRUCTION",
              "formal_mrs_completed": False, "evaluation_count": len(rows),
              "complete_numeric_calculations": sum(row["retrospective_calculation"] is not None for row in rows),
              "evaluations": rows, "raw_pinned_inputs": store.used,
              "input_hashes": {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in (price_register, market_components, market_register, ci_vintages)},
              "generation": register["adopted_generation"]}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--price-register", type=Path, required=True)
    parser.add_argument("--market-components", type=Path, required=True)
    parser.add_argument("--ci-vintages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(project_root(), price_register=args.price_register, market_components=args.market_components,
                   ci_vintages=args.ci_vintages, output=args.output)
    print(json.dumps({key: result[key] for key in ("status", "evaluation_count", "complete_numeric_calculations", "formal_mrs_completed")}))
    return 0
