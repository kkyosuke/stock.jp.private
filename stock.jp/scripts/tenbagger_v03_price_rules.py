"""Executable price-rule core for the frozen v0.3 challenger.

The module intentionally has no parameter search or historical optimization.
It exists to make V3-P1 through V3-P5 mechanically testable. V3-P6 needs a
time-series portfolio and is outside a single-position simulation.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date

from scripts.tenbagger_price_scan import Price, add_years
from scripts.tenbagger_v02_price_exit_sim import (
    Trade,
    month_end_indexes,
    moving_averages,
    price_index_on_or_after,
)


@dataclass(frozen=True)
class V03PriceResult:
    trades: tuple[Trade, ...]
    review_days: tuple[date, ...]
    remaining_fraction: float
    pending_rule: str


def simulate_v03_price_rules(
    *,
    entry_day: date,
    entry_price: float,
    prices: list[Price],
) -> V03PriceResult:
    """Apply V3-P1 to V3-P5 to one position with Q0 normalized to one."""

    stock_days = [price.day for price in prices]
    entry_index = bisect.bisect_left(stock_days, entry_day)
    if entry_index >= len(prices) or prices[entry_index].day != entry_day:
        raise ValueError(f"missing entry quote for {entry_day}")
    if abs(prices[entry_index].close / entry_price - 1) > 1e-6:
        raise ValueError(
            f"entry mismatch expected={entry_price} actual={prices[entry_index].close}"
        )

    month_ends = month_end_indexes(prices, entry_index)
    averages = moving_averages(prices)
    p2_index = price_index_on_or_after(stock_days, add_years(entry_day, 3))
    remaining = 1.0
    trades: list[Trade] = []
    review_days: list[date] = []
    pending_rule = ""
    tenx_reached = False
    p3_done = False
    p4_done = False
    highest_ma20: float | None = None

    def execute(rule: str, trigger_index: int, requested_fraction: float) -> bool:
        nonlocal remaining, pending_rule
        execution_index = trigger_index + 1
        if execution_index >= len(prices):
            pending_rule = rule
            return False
        fraction = min(remaining, requested_fraction)
        if fraction <= 1e-12:
            return True
        trades.append(
            Trade(
                rule=rule,
                trigger_day=prices[trigger_index].day,
                execution_day=prices[execution_index].day,
                fraction_of_q0=fraction,
                execution_price=prices[execution_index].close,
            )
        )
        remaining = max(0.0, remaining - fraction)
        return True

    for index in range(entry_index, len(prices)):
        price = prices[index]
        ma20 = averages[index]
        if ma20 is not None:
            highest_ma20 = ma20 if highest_ma20 is None else max(highest_ma20, ma20)
        is_month_end = index in month_ends

        if is_month_end and price.close <= 0.5 * entry_price:
            review_days.append(price.day)

        if (
            p2_index is not None
            and index == p2_index
            and ma20 is not None
            and ma20 < 10 * entry_price
        ):
            if execute("V3-P2", index, remaining):
                break
            continue

        if is_month_end and price.close >= 10 * entry_price:
            tenx_reached = True

        drawdown20 = (
            ma20 / highest_ma20 - 1
            if ma20 is not None and highest_ma20 is not None
            else None
        )
        if (
            is_month_end
            and tenx_reached
            and drawdown20 is not None
            and drawdown20 <= -0.5
        ):
            if execute("V3-P5", index, remaining):
                break
            continue

        pending_rules: list[str] = []
        if is_month_end and price.close >= 5 * entry_price and not p3_done:
            if execute("V3-P3", index, 0.2):
                p3_done = True
            else:
                pending_rules.append("V3-P3")

        if is_month_end and price.close >= 10 * entry_price and not p4_done:
            if execute("V3-P4", index, 0.3):
                p4_done = True
            else:
                pending_rules.append("V3-P4")

        if pending_rules:
            pending_rule = "+".join(pending_rules)

    return V03PriceResult(
        trades=tuple(trades),
        review_days=tuple(review_days),
        remaining_fraction=remaining,
        pending_rule=pending_rule,
    )
