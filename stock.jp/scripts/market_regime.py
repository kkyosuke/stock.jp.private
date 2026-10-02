"""Calculate the frozen MRS-v0.1 market-regime risk overlay."""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MarketRegimeInput:
    as_of: str
    topix_close: float
    topix_ma200: float
    growth_close: float
    growth_ma200: float
    breadth_pct: float
    nikkei_vi: float
    nikkei_vi_p80_3y: float
    leading_ci: float
    leading_ci_3m_ago: float


@dataclass(frozen=True)
class MarketRegimeResult:
    as_of: str
    rule_version: str
    components: dict[str, int]
    score: int
    state: str
    entry_multiplier: float


@dataclass(frozen=True)
class BreadthResult:
    as_of: str
    breadth_pct: float
    latest_code_count: int
    universe_code_count: int
    eligible_code_count: int
    above_ma200_count: int
    excluded_code_count: int
    insufficient_history_code_count: int
    data_coverage_ratio: float
    session_count: int
    archive_session_count: int


def _validate(values: MarketRegimeInput) -> None:
    date.fromisoformat(values.as_of)
    numeric = {key: value for key, value in asdict(values).items() if key != "as_of"}
    for key, value in numeric.items():
        if not math.isfinite(value):
            raise ValueError(f"{key} must be finite")
        if key != "breadth_pct" and value <= 0:
            raise ValueError(f"{key} must be positive")
    if not 0 <= values.breadth_pct <= 100:
        raise ValueError("breadth_pct must be between 0 and 100")


def evaluate_market_regime(values: MarketRegimeInput) -> MarketRegimeResult:
    """Apply the five frozen MRS-v0.1 binary conditions."""

    _validate(values)
    components = {
        "M1": int(values.topix_close >= values.topix_ma200),
        "M2": int(values.growth_close >= values.growth_ma200),
        "M3": int(values.breadth_pct >= 50),
        "M4": int(values.nikkei_vi <= values.nikkei_vi_p80_3y),
        "M5": int(values.leading_ci >= values.leading_ci_3m_ago),
    }
    score = sum(components.values())
    if score >= 4:
        state, multiplier = "NORMAL", 1.0
    elif score >= 2:
        state, multiplier = "CAUTION", 0.5
    else:
        state, multiplier = "STRESS", 0.0
    return MarketRegimeResult(
        as_of=values.as_of,
        rule_version="MRS-v0.1",
        components=components,
        score=score,
        state=state,
        entry_multiplier=multiplier,
    )


def percentile(values: list[float], percentile_value: float) -> float:
    """Return the linearly interpolated percentile used by M4."""

    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0 <= percentile_value <= 100:
        raise ValueError("percentile must be between 0 and 100")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile_value / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def calculate_breadth(
    archive_root: Path,
    *,
    as_of: date,
    window: int = 200,
    lookback_sessions: int = 280,
    maximum_close_age_days: int = 7,
    minimum_coverage: float = 0.98,
) -> BreadthResult:
    """Calculate M3 from the tracked point-in-time whole-market archive.

    A 200-session moving average uses each security's latest 200 valid closes.
    Newly listed securities without 200 appearances are outside the eligible
    universe.  Seasoned securities with too few closes or a stale latest close
    reduce the coverage ratio and can stop the formal calculation.
    """

    if window <= 0:
        raise ValueError("breadth window must be positive")
    if lookback_sessions < window:
        raise ValueError("breadth lookback_sessions must be at least window")
    if maximum_close_age_days < 0:
        raise ValueError("breadth maximum_close_age_days cannot be negative")
    if not 0 <= minimum_coverage <= 1:
        raise ValueError("breadth minimum_coverage must be between 0 and 1")

    sessions: list[tuple[date, Path]] = []
    for path in archive_root.glob("*/*.csv"):
        try:
            session_date = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if session_date <= as_of:
            sessions.append((session_date, path))
    sessions.sort()
    if len(sessions) < window:
        raise ValueError(
            f"breadth requires {window} archived sessions; found {len(sessions)}"
        )
    selected = sessions[-lookback_sessions:]
    closes: dict[str, list[tuple[date, float]]] = {}
    appearances: dict[str, int] = {}
    latest_codes: set[str] = set()
    for index, (session_date, path) in enumerate(selected):
        session_codes: set[str] = set()
        with path.open(encoding="utf-8", newline="") as source:
            for row in csv.DictReader(source):
                code = str(row.get("銘柄コード", "")).strip()
                if not code:
                    continue
                session_codes.add(code)
                if index == len(selected) - 1:
                    latest_codes.add(code)
                close_text = str(row.get("終値", "")).strip()
                if not close_text:
                    continue
                try:
                    close = float(close_text)
                except ValueError:
                    continue
                if not math.isfinite(close) or close <= 0:
                    continue
                closes.setdefault(code, []).append((session_date, close))
        for code in session_codes:
            appearances[code] = appearances.get(code, 0) + 1
    latest_session = selected[-1][0]
    universe = {code for code in latest_codes if appearances.get(code, 0) >= window}
    eligible = {
        code: values
        for code, values in closes.items()
        if code in universe
        and len(values) >= window
        and (latest_session - values[-1][0]).days <= maximum_close_age_days
    }
    if not universe or not eligible:
        raise ValueError("breadth has no seasoned codes with a usable MA history")
    coverage = len(eligible) / len(universe)
    if coverage < minimum_coverage:
        raise ValueError(
            "breadth data coverage "
            f"{coverage:.2%} is below required {minimum_coverage:.2%}"
        )
    above = sum(
        values[-1][1] >= sum(close for _, close in values[-window:]) / window
        for values in eligible.values()
    )
    return BreadthResult(
        as_of=latest_session.isoformat(),
        breadth_pct=above / len(eligible) * 100,
        latest_code_count=len(latest_codes),
        universe_code_count=len(universe),
        eligible_code_count=len(eligible),
        above_ma200_count=above,
        excluded_code_count=len(universe - set(eligible)),
        insufficient_history_code_count=len(latest_codes - universe),
        data_coverage_ratio=coverage,
        session_count=window,
        archive_session_count=len(selected),
    )


def _points(
    series: dict[str, Any], *, period: bool = False
) -> list[tuple[date, float]]:
    key = "period" if period else "date"
    result: list[tuple[date, float]] = []
    for point in series.get("points", []):
        if not isinstance(point, dict):
            continue
        try:
            point_date = (
                date.fromisoformat(f"{point[key]}-01")
                if period
                else date.fromisoformat(str(point[key]))
            )
            value = float(point["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            result.append((point_date, value))
    return sorted(result)


def _subtract_months(value: date, months: int) -> tuple[int, int]:
    month_index = value.year * 12 + value.month - 1 - months
    return month_index // 12, month_index % 12 + 1


def derive_market_regime(
    document: dict[str, Any], *, archive_root: Path
) -> tuple[MarketRegimeInput, MarketRegimeResult, dict[str, Any]]:
    """Derive all nine MRS values from raw point-in-time series."""

    if document.get("schema_version") != "1.0":
        raise ValueError("market-regime input schema_version must be 1.0")
    as_of = date.fromisoformat(str(document.get("as_of")))
    series = document.get("series")
    if not isinstance(series, dict):
        raise ValueError("market-regime input requires series")
    derived: dict[str, Any] = {}
    index_values: dict[str, tuple[float, float]] = {}
    for name in ("topix", "growth250"):
        points = [
            (day, value) for day, value in _points(series.get(name, {})) if day <= as_of
        ]
        if len(points) < 200:
            raise ValueError(f"{name} requires 200 point-in-time daily observations")
        selected = points[-200:]
        if selected[-1][0] != as_of:
            raise ValueError(
                f"{name} latest official value {selected[-1][0]} does not match MRS as_of {as_of}"
            )
        index_values[name] = (
            selected[-1][1],
            sum(value for _, value in selected) / 200,
        )
        derived[name] = {
            "last_date": selected[-1][0].isoformat(),
            "close": selected[-1][1],
            "ma200": index_values[name][1],
            "observation_count": len(selected),
        }
    breadth = calculate_breadth(
        archive_root,
        as_of=as_of,
        lookback_sessions=int(document.get("breadth_lookback_sessions", 280)),
        maximum_close_age_days=int(document.get("maximum_breadth_close_age_days", 7)),
        minimum_coverage=float(document.get("minimum_breadth_coverage", 0.98)),
    )
    if breadth.as_of != as_of.isoformat():
        raise ValueError(
            f"breadth latest session {breadth.as_of} does not match MRS as_of {as_of}"
        )
    derived["breadth"] = asdict(breadth)

    vi_start_year = as_of.year - 3
    try:
        vi_start = as_of.replace(year=vi_start_year)
    except ValueError:
        vi_start = as_of.replace(year=vi_start_year, day=28)
    vi_points = [
        (day, value)
        for day, value in _points(series.get("nikkei_vi", {}))
        if vi_start < day <= as_of
    ]
    minimum_vi = int(document.get("minimum_vi_observations", 500))
    if len(vi_points) < minimum_vi:
        raise ValueError(
            f"nikkei_vi requires at least {minimum_vi} three-year observations"
        )
    vi_latest = vi_points[-1][1]
    if vi_points[-1][0] != as_of:
        raise ValueError(
            f"nikkei_vi latest value {vi_points[-1][0]} does not match MRS as_of {as_of}"
        )
    vi_p80 = percentile([value for _, value in vi_points], 80)
    derived["nikkei_vi"] = {
        "last_date": vi_points[-1][0].isoformat(),
        "close": vi_latest,
        "p80_3y": vi_p80,
        "observation_count": len(vi_points),
    }

    leading_series = series.get("leading_ci", {})
    available_at = (
        leading_series.get("available_at_jst")
        if isinstance(leading_series, dict)
        else None
    )
    if not available_at:
        raise ValueError("leading_ci.available_at_jst is required")
    available = datetime.fromisoformat(str(available_at).replace("Z", "+00:00"))
    if available.tzinfo is None:
        raise ValueError("leading_ci.available_at_jst must include a UTC offset")
    if available.date() > as_of:
        raise ValueError("leading CI release was not available by MRS as_of")
    leading_points = [
        (period, value)
        for period, value in _points(leading_series, period=True)
        if period <= as_of
    ]
    if not leading_points:
        raise ValueError("leading_ci has no value available by MRS as_of")
    latest_period, latest_ci = leading_points[-1]
    prior_year, prior_month = _subtract_months(latest_period, 3)
    prior_matches = [
        value
        for period, value in leading_points
        if (period.year, period.month) == (prior_year, prior_month)
    ]
    if not prior_matches:
        raise ValueError("leading_ci is missing the exact three-month-prior value")
    prior_ci = prior_matches[-1]
    derived["leading_ci"] = {
        "latest_period": latest_period.strftime("%Y-%m"),
        "latest": latest_ci,
        "three_month_prior_period": f"{prior_year:04d}-{prior_month:02d}",
        "three_month_prior": prior_ci,
        "available_at_jst": available.isoformat(timespec="seconds"),
    }

    values = MarketRegimeInput(
        as_of=as_of.isoformat(),
        topix_close=index_values["topix"][0],
        topix_ma200=index_values["topix"][1],
        growth_close=index_values["growth250"][0],
        growth_ma200=index_values["growth250"][1],
        breadth_pct=breadth.breadth_pct,
        nikkei_vi=vi_latest,
        nikkei_vi_p80_3y=vi_p80,
        leading_ci=latest_ci,
        leading_ci_3m_ago=prior_ci,
    )
    return values, evaluate_market_regime(values), derived


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--topix-close", type=float, required=True)
    parser.add_argument("--topix-ma200", type=float, required=True)
    parser.add_argument("--growth-close", type=float, required=True)
    parser.add_argument("--growth-ma200", type=float, required=True)
    parser.add_argument("--breadth-pct", type=float, required=True)
    parser.add_argument("--nikkei-vi", type=float, required=True)
    parser.add_argument("--nikkei-vi-p80-3y", type=float, required=True)
    parser.add_argument("--leading-ci", type=float, required=True)
    parser.add_argument("--leading-ci-3m-ago", type=float, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = evaluate_market_regime(
        MarketRegimeInput(
            as_of=args.as_of,
            topix_close=args.topix_close,
            topix_ma200=args.topix_ma200,
            growth_close=args.growth_close,
            growth_ma200=args.growth_ma200,
            breadth_pct=args.breadth_pct,
            nikkei_vi=args.nikkei_vi,
            nikkei_vi_p80_3y=args.nikkei_vi_p80_3y,
            leading_ci=args.leading_ci,
            leading_ci_3m_ago=args.leading_ci_3m_ago,
        )
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
