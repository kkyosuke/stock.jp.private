"""Recalculate dilution, SAM/SOM and the three-year ten-bagger path.

The input is deliberately private and evidence-oriented.  This module never
fills an unknown assumption with an optimistic default: absent inputs produce
``INCOMPLETE`` and a precise list of fields the operator must research.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any


RULE_VERSION = "tenbagger-v0.2"
SAM_EVIDENCE_POINTS = {
    "THIRD_PARTY_RECALCULABLE": 3,
    "COMPANY_VERIFIABLE": 1,
    "UNVERIFIABLE": 0,
}
CAPACITY_EVIDENCE_POINTS = {
    "CURRENT_OR_CONTRACTED": 4,
    "FUNDED_PLAN": 2,
    "NARRATIVE": 0,
}
N3_COMPLETENESS_POINTS = {"COMPLETE": 4, "PARTIAL": 2, "NONE": 0}
KPI_DECOMPOSITION_POINTS = {"COMPLETE": 4, "PARTIAL": 2, "MULTIPLE_ONLY": 0}
COMPETITOR_SCALE_POINTS = {"CONSISTENT": 3, "PARTIAL": 1, "UNSUPPORTED": 0}


class InvestmentCaseError(ValueError):
    """The document is malformed rather than merely incomplete."""


def _get(document: dict[str, Any], dotted: str) -> Any:
    value: Any = document
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _number(
    value: Any, dotted: str, missing: list[str], *, zero: bool = False
) -> float | None:
    if value is None or value == "":
        missing.append(dotted)
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise InvestmentCaseError(f"{dotted} must be numeric") from error
    if not math.isfinite(parsed) or parsed < 0 or (not zero and parsed == 0):
        comparator = ">= 0" if zero else "> 0"
        raise InvestmentCaseError(f"{dotted} must be finite and {comparator}")
    return parsed


def _choice(
    value: Any, dotted: str, choices: dict[str, int], missing: list[str]
) -> str | None:
    if value is None or str(value).strip() == "":
        missing.append(dotted)
        return None
    normalized = str(value).strip().upper()
    if normalized not in choices:
        raise InvestmentCaseError(f"{dotted} must be one of: {', '.join(choices)}")
    return normalized


def _boolean(value: Any, dotted: str, missing: list[str]) -> bool | None:
    if value is None or value == "":
        missing.append(dotted)
        return None
    if not isinstance(value, bool):
        raise InvestmentCaseError(f"{dotted} must be boolean")
    return value


def _source_ids(document: dict[str, Any], dotted: str, missing: list[str]) -> list[str]:
    value = _get(document, dotted)
    if not isinstance(value, list) or not [item for item in value if str(item).strip()]:
        missing.append(dotted)
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise InvestmentCaseError("competitor valuation values are empty")
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _result(
    status: str, document: dict[str, Any], missing: list[str], **values: Any
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "rule_version": RULE_VERSION,
        "code": str(document.get("code", "")).strip(),
        "company": str(document.get("company", "")).strip(),
        "as_of_jst": document.get("as_of_jst"),
        "status": status,
        "entry_ready": False,
        "missing_fields": sorted(set(missing)),
        **values,
    }


def evaluate_investment_case(document: dict[str, Any]) -> dict[str, Any]:
    """Return a complete, incomplete, or failed entry assessment.

    Monetary inputs must use the same currency and unit.  Share inputs are raw
    share counts.  Percentages are expressed as percentages (for example 20,
    not 0.20).
    """

    if document.get("schema_version") != "1.0":
        raise InvestmentCaseError("schema_version must be 1.0")
    missing: list[str] = []
    code = str(document.get("code", "")).strip()
    if not code:
        missing.append("code")
    as_of = document.get("as_of_jst")
    if not as_of:
        missing.append("as_of_jst")
    else:
        try:
            parsed_as_of = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
            if parsed_as_of.tzinfo is None:
                raise ValueError
        except ValueError as error:
            raise InvestmentCaseError("as_of_jst must include a UTC offset") from error

    price = _number(_get(document, "price.value"), "price.value", missing)
    issued = _number(
        _get(document, "capital.issued_shares"), "capital.issued_shares", missing
    )
    treasury = _number(
        _get(document, "capital.treasury_shares"),
        "capital.treasury_shares",
        missing,
        zero=True,
    )
    cash = _number(_get(document, "capital.cash"), "capital.cash", missing, zero=True)
    debt = _number(_get(document, "capital.debt"), "capital.debt", missing, zero=True)
    base_new = _number(
        _get(document, "capital.planned_base_new_shares"),
        "capital.planned_base_new_shares",
        missing,
        zero=True,
    )
    downside_new = _number(
        _get(document, "capital.planned_downside_new_shares"),
        "capital.planned_downside_new_shares",
        missing,
        zero=True,
    )
    securities = _get(document, "capital.potential_securities")
    potential_shares = 0.0
    exercise_cash = 0.0
    exercise_cash_complete = True
    variable_strike_ids: list[str] = []
    if not isinstance(securities, list):
        missing.append("capital.potential_securities")
    else:
        for index, security in enumerate(securities):
            prefix = f"capital.potential_securities[{index}]"
            if not isinstance(security, dict):
                raise InvestmentCaseError(f"{prefix} must be an object")
            shares = _number(
                security.get("shares_at_10x_price"),
                f"{prefix}.shares_at_10x_price",
                missing,
                zero=True,
            )
            proceeds = _number(
                security.get("exercise_cash"),
                f"{prefix}.exercise_cash",
                missing,
                zero=True,
            )
            variable = _boolean(
                security.get("variable_strike"), f"{prefix}.variable_strike", missing
            )
            if not isinstance(security.get("source_ids"), list) or not security.get(
                "source_ids"
            ):
                missing.append(f"{prefix}.source_ids")
            if shares is not None:
                potential_shares += shares
            if proceeds is not None:
                exercise_cash += proceeds
            else:
                exercise_cash_complete = False
            if variable is True:
                variable_strike_ids.append(str(security.get("id") or index))

    for section in (
        "price",
        "capital",
        "financials",
        "market",
        "valuation",
        "kpi_path",
        "eligibility",
    ):
        _source_ids(document, f"{section}.source_ids", missing)

    revenue = _number(
        _get(document, "financials.ttm_revenue"), "financials.ttm_revenue", missing
    )
    net_margin = _number(
        _get(document, "financials.assumed_normalized_net_margin_pct"),
        "financials.assumed_normalized_net_margin_pct",
        missing,
    )
    sam = _number(_get(document, "market.sam_3y"), "market.sam_3y", missing)
    attainable_share = _number(
        _get(document, "market.attainable_share_pct"),
        "market.attainable_share_pct",
        missing,
        zero=True,
    )
    capacity_limit = _number(
        _get(document, "market.capacity_revenue_limit"),
        "market.capacity_revenue_limit",
        missing,
    )
    leader_share = _number(
        _get(document, "market.leader_share_pct"),
        "market.leader_share_pct",
        missing,
    )
    sam_evidence = _choice(
        _get(document, "market.sam_evidence"),
        "market.sam_evidence",
        SAM_EVIDENCE_POINTS,
        missing,
    )
    capacity_evidence = _choice(
        _get(document, "market.capacity_evidence"),
        "market.capacity_evidence",
        CAPACITY_EVIDENCE_POINTS,
        missing,
    )
    market_growth = _boolean(
        _get(document, "market.market_growth_verified"),
        "market.market_growth_verified",
        missing,
    )
    share_gain = _boolean(
        _get(document, "market.share_gain_verified"),
        "market.share_gain_verified",
        missing,
    )
    overtake = _boolean(
        _get(document, "market.share_overtake_supported"),
        "market.share_overtake_supported",
        missing,
    )

    exit_pe = _number(
        _get(document, "valuation.proposed_exit_pe"),
        "valuation.proposed_exit_pe",
        missing,
    )
    if exit_pe is not None and exit_pe > 40:
        raise InvestmentCaseError("valuation.proposed_exit_pe must be <= 40")
    competitors = _get(document, "valuation.direct_competitors")
    competitor_pes: list[float] = []
    competitor_margins: list[float] = []
    competitor_caps: list[float] = []
    if not isinstance(competitors, list) or not 3 <= len(competitors) <= 5:
        missing.append("valuation.direct_competitors[3..5]")
    else:
        for index, competitor in enumerate(competitors):
            prefix = f"valuation.direct_competitors[{index}]"
            if not isinstance(competitor, dict):
                raise InvestmentCaseError(f"{prefix} must be an object")
            pe = _number(competitor.get("exit_pe"), f"{prefix}.exit_pe", missing)
            margin = _number(
                competitor.get("net_margin_pct"), f"{prefix}.net_margin_pct", missing
            )
            cap = _number(competitor.get("market_cap"), f"{prefix}.market_cap", missing)
            if not isinstance(competitor.get("source_ids"), list) or not competitor.get(
                "source_ids"
            ):
                missing.append(f"{prefix}.source_ids")
            if pe is not None:
                competitor_pes.append(pe)
            if margin is not None:
                competitor_margins.append(margin)
            if cap is not None:
                competitor_caps.append(cap)
    margin_exception = _boolean(
        _get(document, "valuation.margin_exception_supported"),
        "valuation.margin_exception_supported",
        missing,
    )
    n3_quality = _choice(
        _get(document, "valuation.n3_completeness"),
        "valuation.n3_completeness",
        N3_COMPLETENESS_POINTS,
        missing,
    )
    kpi_quality = _choice(
        _get(document, "kpi_path.decomposition"),
        "kpi_path.decomposition",
        KPI_DECOMPOSITION_POINTS,
        missing,
    )
    competitor_scale = _choice(
        _get(document, "valuation.competitor_scale_consistency"),
        "valuation.competitor_scale_consistency",
        COMPETITOR_SCALE_POINTS,
        missing,
    )
    year3_revenue = _number(
        _get(document, "kpi_path.year3_revenue"), "kpi_path.year3_revenue", missing
    )
    year3_margin = _number(
        _get(document, "kpi_path.year3_net_margin_pct"),
        "kpi_path.year3_net_margin_pct",
        missing,
    )
    drivers = _get(document, "kpi_path.drivers")
    if not isinstance(drivers, list) or not 1 <= len(drivers) <= 3:
        missing.append("kpi_path.drivers[1..3]")
    else:
        for index, driver in enumerate(drivers):
            if not isinstance(driver, dict):
                raise InvestmentCaseError(
                    f"kpi_path.drivers[{index}] must be an object"
                )
            for field in ("name", "current", "year3", "unit", "source_ids"):
                value = driver.get(field)
                if value is None or value == "" or value == []:
                    missing.append(f"kpi_path.drivers[{index}].{field}")

    hard_gates = _boolean(
        _get(document, "eligibility.hard_gates_passed"),
        "eligibility.hard_gates_passed",
        missing,
    )
    other_score = _number(
        _get(document, "eligibility.other_score"),
        "eligibility.other_score",
        missing,
        zero=True,
    )
    liquidity = _boolean(
        _get(document, "eligibility.liquidity_passed"),
        "eligibility.liquidity_passed",
        missing,
    )
    red_flags = _get(document, "risk.unresolved_major_red_flags")
    if not isinstance(red_flags, list):
        missing.append("risk.unresolved_major_red_flags")

    if treasury is not None and issued is not None and treasury > issued:
        raise InvestmentCaseError("capital.treasury_shares cannot exceed issued_shares")
    if base_new is not None and downside_new is not None and downside_new < base_new:
        raise InvestmentCaseError(
            "capital.planned_downside_new_shares cannot be below the base scenario"
        )
    for value, dotted in (
        (net_margin, "financials.assumed_normalized_net_margin_pct"),
        (attainable_share, "market.attainable_share_pct"),
        (leader_share, "market.leader_share_pct"),
        (year3_margin, "kpi_path.year3_net_margin_pct"),
    ):
        if value is not None and value > 100:
            raise InvestmentCaseError(f"{dotted} must be <= 100")
    if other_score is not None and other_score > 70:
        raise InvestmentCaseError("eligibility.other_score must be <= 70")

    if missing:
        partial: dict[str, Any] = {}
        if (
            price is not None
            and issued is not None
            and treasury is not None
            and isinstance(securities, list)
            and not any(field.endswith("shares_at_10x_price") for field in missing)
        ):
            partial_n0 = issued - treasury + potential_shares
            if partial_n0 > 0:
                partial["dilution"] = {
                    "n0_fully_diluted_shares": partial_n0,
                    "potential_shares_at_10x": potential_shares,
                    "exercise_cash_separate_from_shares": (
                        exercise_cash if exercise_cash_complete else None
                    ),
                }
                partial["current_fully_diluted_market_cap"] = price * partial_n0
        if variable_strike_ids:
            return _result(
                "FAIL",
                document,
                missing,
                partial_calculations=partial,
                failures=[
                    "variable-strike securities prevent a fixed fully diluted share count"
                ],
            )
        return _result("INCOMPLETE", document, missing, partial_calculations=partial)

    # All required values are known beyond this point.
    assert None not in (price, issued, treasury, cash, debt, base_new, downside_new)
    assert None not in (
        revenue,
        net_margin,
        sam,
        attainable_share,
        capacity_limit,
        leader_share,
    )
    assert None not in (
        exit_pe,
        year3_revenue,
        year3_margin,
        hard_gates,
        other_score,
        liquidity,
    )
    assert (
        sam_evidence
        and capacity_evidence
        and n3_quality
        and kpi_quality
        and competitor_scale
    )
    n0 = issued - treasury + potential_shares
    if n0 <= 0:
        raise InvestmentCaseError("fully diluted N0 must be > 0")
    n3_base = n0 + base_new
    n3_downside = n0 + downside_new
    current_market_cap = price * n0
    current_ev = current_market_cap + debt - cash - exercise_cash
    m10 = 10 * price * n3_base
    required_profit = m10 / exit_pe
    required_revenue = required_profit / (net_margin / 100)
    required_cagr_3y = (required_revenue / revenue) ** (1 / 3) - 1
    required_share_pct = required_revenue / sam * 100
    demand_limit = sam * attainable_share / 100
    som = min(demand_limit, capacity_limit)
    margin_p75 = _percentile(competitor_margins, 0.75)
    pe_median = median(competitor_pes)
    pe_p75 = _percentile(competitor_pes, 0.75)

    if required_share_pct <= leader_share / 2:
        share_points = 4
    elif required_share_pct <= leader_share:
        share_points = 3
    elif overtake:
        share_points = 1
    else:
        share_points = 0
    growth_points = (
        4 if market_growth and share_gain else 2 if market_growth or share_gain else 0
    )
    market_points = (
        SAM_EVIDENCE_POINTS[sam_evidence]
        + share_points
        + CAPACITY_EVIDENCE_POINTS[capacity_evidence]
        + growth_points
    )

    exit_points = 4 if exit_pe <= pe_median else 2 if exit_pe <= pe_p75 else 0
    reverse_points = (
        N3_COMPLETENESS_POINTS[n3_quality]
        + KPI_DECOMPOSITION_POINTS[kpi_quality]
        + exit_points
        + COMPETITOR_SCALE_POINTS[competitor_scale]
    )
    if required_share_pct > leader_share and not overtake:
        reverse_points = min(reverse_points, 6)
    total_score = other_score + market_points + reverse_points
    failures: list[str] = []
    if variable_strike_ids:
        failures.append(
            "variable-strike securities prevent a fixed fully diluted share count"
        )
    if n3_quality != "COMPLETE":
        failures.append(
            "N3 does not fully reflect existing dilution and required financing"
        )
    if required_revenue > som:
        failures.append("required year-3 revenue exceeds SOM-3Y")
    if year3_revenue < required_revenue:
        failures.append("KPI path does not reach required year-3 revenue")
    if year3_margin < net_margin:
        failures.append("KPI path margin is below the valuation assumption")
    if net_margin > margin_p75 and not margin_exception:
        failures.append(
            "assumed net margin exceeds competitor 75th percentile without evidence"
        )
    if m10 > max(competitor_caps) and competitor_scale != "CONSISTENT":
        failures.append(
            "M10 exceeds the largest direct competitor without a quantitative path"
        )
    if market_points < 8:
        failures.append("market score is below 8")
    if reverse_points < 10:
        failures.append("reverse-calculation score is below 10")
    if not hard_gates:
        failures.append("one or more hard gates failed")
    if total_score < 70:
        failures.append("total score is below 70")
    if not liquidity:
        failures.append("liquidity gate failed")
    if red_flags:
        failures.append("unresolved major red flags remain")

    status = "FAIL" if failures else "PASS"
    result = _result(
        status,
        document,
        [],
        entry_ready=status == "PASS",
        dilution={
            "issued_shares": issued,
            "treasury_shares": treasury,
            "potential_shares_at_10x": potential_shares,
            "exercise_cash_separate_from_shares": exercise_cash,
            "n0_fully_diluted_shares": n0,
            "n3_base_shares": n3_base,
            "n3_downside_shares": n3_downside,
            "n3_base_dilution_pct": (n3_base / n0 - 1) * 100,
        },
        valuation={
            "current_fully_diluted_market_cap": current_market_cap,
            "current_ev_after_exercise_cash": current_ev,
            "m10_required_market_cap": m10,
            "exit_pe": exit_pe,
            "competitor_pe_median": pe_median,
            "competitor_pe_p75": pe_p75,
            "required_normalized_net_income": required_profit,
            "required_revenue": required_revenue,
            "required_revenue_cagr_3y_pct": required_cagr_3y * 100,
        },
        market={
            "sam_3y": sam,
            "required_share_pct": required_share_pct,
            "demand_revenue_limit": demand_limit,
            "capacity_revenue_limit": capacity_limit,
            "som_3y": som,
            "required_revenue_within_som": required_revenue <= som,
        },
        kpi_path={
            "year3_revenue": year3_revenue,
            "year3_net_margin_pct": year3_margin,
            "reaches_required_revenue": year3_revenue >= required_revenue,
        },
        scores={
            "market": market_points,
            "reverse_calculation": reverse_points,
            "other_sections": other_score,
            "total": total_score,
        },
        failures=failures,
    )
    result["entry_ready"] = status == "PASS"
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = evaluate_investment_case(
        json.loads(args.input.read_text(encoding="utf-8"))
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
