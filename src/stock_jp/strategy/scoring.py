"""Evidence-bound 25-item scoring under frozen tenbagger v0.2/v0.4 rules."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

from stock_jp.strategy.company import (
    SAM_EVIDENCE_POINTS, CAPACITY_EVIDENCE_POINTS, N3_COMPLETENESS_POINTS,
    KPI_DECOMPOSITION_POINTS, COMPETITOR_SCALE_POINTS,
)
from stock_jp.workspace import project_root

# Numeric inputs use percentage points (20 means 20%, never 0.20).
NUMERIC = {
    "4.1.1": (8, "sales_per_fd_share_cagr_3y_pct", ((0, 0), (10, 1), (20, 3), (30, 6))),
    "4.1.2": (8, "ttm_sales_per_fd_share_yoy_pct", ((0, 0), (10, 1), (20, 3), (30, 6))),
    "4.1.3": (4, "gross_profit_per_share_growth_less_sales_growth_pt", ((0, 0), (5, 2))),
    "4.3.1": (6, "operating_margin_yoy_change_pt", ((0, 0), (2, 2), (5, 4))),
    "4.3.2": (5, "gross_profit_growth_less_sga_growth_pt", ((0, 0), (5, 1), (10, 3))),
}
CHOICES = {
    "4.3.3": (4, "normalized_fcf_margin", {"NEGATIVE_WORSENING": 0, "NEGATIVE_IMPROVING": 1, "ZERO_TO_FIVE": 2, "FIVE_TO_TEN": 3, "TEN_OR_MORE": 4}),
    "4.4.1": (3, "sam_evidence", SAM_EVIDENCE_POINTS),
    "4.4.2": (4, "required_share_vs_leader", {"AT_MOST_HALF": 4, "AT_MOST_LEADER": 3, "ABOVE_WITH_PATH": 1, "NO_PATH": 0}),
    "4.4.3": (4, "capacity_evidence", CAPACITY_EVIDENCE_POINTS),
    "4.4.4": (4, "market_and_share_growth", {"BOTH": 4, "ONE": 2, "NEITHER": 0}),
    "4.5.1": (4, "retention", {"TWO_YEARS": 4, "ONE_YEAR": 2, "UNDISCLOSED_OR_WORSENING": 0}),
    "4.5.2": (3, "customer_unit_economics", {"IMPROVING": 3, "PARTIAL": 1, "UNCONFIRMED": 0}),
    "4.5.3": (3, "differentiation", {"QUANTITATIVE": 3, "QUALITATIVE": 1, "NONE": 0}),
    "4.6.1": (4, "funding_runway", {"FCF_POSITIVE_OR_36_PLUS": 4, "24_TO_36": 3, "12_TO_24": 1}),
    "4.6.3": (3, "cash_conversion_3y", {"AT_LEAST_EIGHTY": 3, "POSITIVE_BELOW_EIGHTY": 1, "NONPOSITIVE_OR_UNCALCULABLE": 0, "ALTERNATIVE_IMPROVING": 3, "ALTERNATIVE_FLAT": 1, "ALTERNATIVE_WORSENING": 0}),
    "4.7.1": (2, "kpi_continuity", {"THREE_YEARS": 2, "REASON_AND_BRIDGE": 1, "UNDISCLOSED_OR_UNBRIDGED": 0}),
    "4.7.2": (2, "initial_forecast_accuracy", {"AT_MOST_TEN": 2, "TEN_TO_TWENTY_FIVE": 1, "ABOVE_TWENTY_FIVE": 0}),
    "4.7.3": (1, "related_party_and_allocation", {"VERIFIABLE": 1, "UNVERIFIABLE": 0}),
    "4.8.1": (4, "n3_completeness", N3_COMPLETENESS_POINTS),
    "4.8.2": (4, "kpi_decomposition", KPI_DECOMPOSITION_POINTS),
    "4.8.3": (4, "exit_multiple_vs_peers", {"AT_MOST_MEDIAN": 4, "AT_MOST_P75": 2, "ABOVE_P75": 0}),
    "4.8.4": (3, "competitor_scale", COMPETITOR_SCALE_POINTS),
}
SPECIAL = {
    "4.2.1": (5, "sales_per_share_growth_acceleration_pt"),
    "4.2.2": (5, "gross_profit_per_share_growth_acceleration_pt"),
    "4.6.2": (3, "fully_diluted_shares_cagr_3y_pct"),
}
ITEMS = {key: (value[0], value[1]) for table in (NUMERIC, CHOICES, SPECIAL) for key, value in table.items()}
GATES = tuple(f"HG{i}" for i in range(1, 8))


def numeric(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError("score metric must be a finite number, not a boolean")
    return float(value)


def item_points(key, value):
    """Apply the exact frozen boundaries; no fitted coefficients or defaults."""
    if key in CHOICES:
        choices = CHOICES[key][2]
        if not isinstance(value, str) or value not in choices:
            raise ValueError(f"invalid choice for {key}: {value}")
        return choices[value]
    value = numeric(value)
    if key in NUMERIC:
        maximum, _, boundaries = NUMERIC[key]
        return next((points for bound, points in boundaries if value < bound), maximum)
    if key in {"4.2.1", "4.2.2"}:
        return 0 if value <= 0 else 2 if value < 5 else 4 if value < 10 else 5
    if key == "4.6.2":
        return 3 if value <= 0 else 2 if value <= 3 else 1 if value < 7 else 0
    raise ValueError(f"unknown item {key}")


def aware(value):
    result = datetime.fromisoformat(str(value))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("assessment timestamps must include a timezone")
    return result


def evaluate_score(document, *, root: Path):
    if document.get("schema_version") != "1.0":
        raise ValueError("scoring schema_version must be 1.0")
    cutoff = aware(document["information_cutoff_at_jst"])
    if not document.get("code"):
        raise ValueError("scoring code is required")
    sources = document.get("sources", {})
    verified = {}
    for source_id, row in sources.items():
        path = (root / row["path"]).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError("scoring source must be a workspace file")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("scoring source checksum mismatch")
        if row.get("primary_source") is not True or not row.get("url"):
            raise ValueError("scoring source requires an official URL and primary-source designation")
        if aware(row["published_at_jst"]) > cutoff:
            raise ValueError("scoring source was published after the cutoff")
        aware(row["retrieved_at_jst"])
        verified[source_id] = row

    def evidence(row):
        ids = row.get("source_ids", [])
        if not ids or any(value not in verified for value in ids) or not row.get("reason"):
            raise ValueError("known assessment requires verified source IDs and reasoning")
        if not row.get("locator"):
            raise ValueError("known assessment requires a source page or field locator")

    gates, missing, failed = {}, [], []
    if set(document.get("hard_gates", {})) - set(GATES):
        raise ValueError("unknown hard gate")
    for key in GATES:
        row = document.get("hard_gates", {}).get(key, {"status": "NA"})
        status = row.get("status")
        if status not in {"PASS", "FAIL", "NA"}:
            raise ValueError("hard gate must be PASS, FAIL or NA")
        if status != "NA":
            evidence(row)
        else:
            missing.append(key)
        if status == "FAIL":
            failed.append(key)
        gates[key] = row

    if set(document.get("items", {})) - set(ITEMS):
        raise ValueError("unknown score item")
    rows = []
    for key in sorted(ITEMS):
        maximum, name = ITEMS[key]
        row = document.get("items", {}).get(key, {"status": "NA"})
        status = row.get("status")
        if status not in {"KNOWN", "NA"}:
            raise ValueError("score item must be KNOWN or NA")
        points = None
        if status == "KNOWN":
            evidence(row)
            if not row.get("calculation"):
                raise ValueError("known score requires a formula or evidence category explanation")
            points = item_points(key, row["value"])
        else:
            missing.append(key)
        rows.append({"rule_id": key, "metric": name, "maximum": maximum, **row, "points": points})
    sections = {f"4.{i}": None for i in range(1, 9)}
    for section in sections:
        section_rows = [row for row in rows if row["rule_id"].startswith(section + ".")]
        if all(row["points"] is not None for row in section_rows):
            sections[section] = sum(row["points"] for row in section_rows)
    complete = not missing and not failed
    total = sum(sections.values()) if complete else None
    flags = document.get("major_red_flags_resolved")
    if type(flags) is not bool:
        raise ValueError("major_red_flags_resolved must be an explicitly reviewed boolean")
    # Score caps require a reviewed reverse model, never an implicit optimistic default.
    reverse_cap = document.get("reverse_score_cap")
    if complete:
        if type(reverse_cap) is not int or not 0 <= reverse_cap <= 15:
            raise ValueError("complete scoring needs an explicitly reviewed reverse_score_cap")
        evidence(document.get("reverse_cap_evidence", {}))
        total -= max(0, sections["4.8"] - reverse_cap)
        sections["4.8"] = min(sections["4.8"], reverse_cap)
    eligible = complete and flags and total >= 70 and sections["4.4"] >= 8 and sections["4.8"] >= 10
    return {
        "schema_version": "1.0", "rule_version": "v0.2-score/v0.4-entry",
        "code": document["code"], "information_cutoff_at_jst": cutoff.isoformat(),
        "status": "HARD_GATE_FAIL" if failed else "WAIT" if missing else "COMPLETE_PASS" if eligible else "COMPLETE_FAIL",
        "primary_candidate": eligible, "entry_ready": False,
        "failed_hard_gates": failed, "missing_inputs": missing,
        "formal_total": total, "sections": sections, "items": rows, "hard_gates": gates,
        "reviewed_item_count": sum(row["points"] is not None for row in rows),
        "raw_reviewed_points": sum(row["points"] or 0 for row in rows),
        "raw_reviewed_points_are_a_total_score": False,
        "source_sha256": {key: row["sha256"] for key, row in verified.items()},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = project_root()
    if any(not path.resolve().is_relative_to(root) for path in (args.input, args.output)):
        raise ValueError("scoring inputs and output must stay inside the workspace")
    result = evaluate_score(json.loads(args.input.read_text()), root=root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("code", "status", "formal_total", "reviewed_item_count", "entry_ready")}, ensure_ascii=False))
    return 0
