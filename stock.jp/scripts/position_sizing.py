"""Frozen purchase-allocation caps for each tenbagger rule version."""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class AllocationCaps:
    initial_entry_pct: float
    add_entry_pct: float
    max_adds: int
    single_name_cost_pct: float
    candidate_pool_cost_pct: float
    industry_cost_pct: float
    max_holdings: int


ALLOCATION_CAPS = {
    "v0.2": AllocationCaps(1.0, 0.5, 2, 3.0, 20.0, 6.0, 12),
    "v0.3": AllocationCaps(1.0, 0.5, 2, 3.0, 20.0, 6.0, 12),
    "v0.4": AllocationCaps(5.0, 2.5, 2, 10.0, 100.0, 20.0, 12),
}


def allocation_caps(rule_version: str) -> AllocationCaps:
    try:
        return ALLOCATION_CAPS[rule_version]
    except KeyError as error:
        raise ValueError(f"unsupported allocation rule version: {rule_version}") from error


def validate_purchase_increment(
    *, rule_version: str, action: str, position_pct: float
) -> None:
    """Reject a BUY/ADD ticket whose incremental cost exceeds its tranche cap."""

    if not math.isfinite(position_pct) or position_pct <= 0:
        raise ValueError("position_pct must be > 0")
    normalized_action = action.strip().upper()
    if normalized_action not in {"BUY", "ADD"}:
        return
    caps = allocation_caps(rule_version)
    cap = caps.initial_entry_pct if normalized_action == "BUY" else caps.add_entry_pct
    if position_pct > cap:
        raise ValueError(
            f"{normalized_action} position_pct {position_pct:g}% exceeds "
            f"{rule_version} tranche cap {cap:g}%"
        )
