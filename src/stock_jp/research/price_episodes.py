#!/usr/bin/env python3
"""Scan current TSE domestic stocks for historical 2Y/3Y tenbagger episodes.

The listing universe comes from the official JPX monthly spreadsheet. Price data
is fetched from Yahoo Finance's public chart endpoint and is used only as a
candidate-discovery source; it is not an official JPX dataset. Securities that
are no longer listed are absent, so the output is not a full-universe backtest.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import random
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook
import xlrd


YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "Mozilla/5.0 (compatible; stock.jp research/0.1)"
DEFAULT_MAX_ADJACENT_MULTIPLE = 2.5
DEFAULT_MAX_TRADING_GAP_DAYS = 45
DEFAULT_MAX_ENTRY_LAG_DAYS = 7


@dataclass(frozen=True)
class Issue:
    code: str
    name: str
    market: str
    sector: str

    @property
    def symbol(self) -> str:
        return f"{self.code}.T"


@dataclass(frozen=True)
class Price:
    day: date
    close: float
    volume: int | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jpx-xls", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/yahoo-daily"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scan-start", type=date.fromisoformat, default=date(2016, 8, 1))
    parser.add_argument("--data-start", type=date.fromisoformat, default=date(2015, 7, 1))
    parser.add_argument("--data-end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--max-adjacent-multiple",
        type=float,
        default=DEFAULT_MAX_ADJACENT_MULTIPLE,
        help="exclude a code when adjacent adjusted closes exceed this ratio",
    )
    parser.add_argument(
        "--max-trading-gap-days",
        type=int,
        default=DEFAULT_MAX_TRADING_GAP_DAYS,
        help="exclude a code when its price history has a longer calendar gap",
    )
    parser.add_argument(
        "--max-entry-lag-days",
        type=int,
        default=DEFAULT_MAX_ENTRY_LAG_DAYS,
        help="maximum calendar days from evaluation month-end to entry close",
    )
    return parser.parse_args()


def _issues_from_rows(rows: Iterable[tuple[Any, ...]]) -> list[Issue]:
    issues: list[Issue] = []
    for values in rows:
        if len(values) <= 5:
            continue
        market = str(values[3] or "").strip()
        if "内国株式" not in market:
            continue
        raw_code = values[1]
        code = str(int(raw_code)) if isinstance(raw_code, float) else str(raw_code).strip()
        issues.append(
            Issue(
                code=code,
                name=str(values[2] or "").strip(),
                market=market,
                sector=str(values[5] or "").strip(),
            )
        )
    return issues


def load_issues(path: Path) -> list[Issue]:
    if path.suffix.lower() == ".xlsx":
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = workbook.worksheets[0]
            return _issues_from_rows(
                tuple(values)
                for values in sheet.iter_rows(min_row=2, values_only=True)
            )
        finally:
            workbook.close()

    sheet = xlrd.open_workbook(path).sheet_by_index(0)
    return _issues_from_rows(
        tuple(sheet.cell_value(row, column) for column in range(sheet.ncols))
        for row in range(1, sheet.nrows)
    )


def unix_start(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def fetch_chart(
    issue: Issue,
    cache_dir: Path,
    start: date,
    end: date,
    refresh: bool,
) -> tuple[Issue, dict[str, Any] | None, str | None]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{issue.code}.json"
    if cache_path.exists() and not refresh:
        try:
            return issue, json.loads(cache_path.read_text()), None
        except (json.JSONDecodeError, OSError):
            pass

    params = urllib.parse.urlencode(
        {
            "period1": unix_start(start),
            "period2": unix_start(end) + 86400,
            "interval": "1d",
            "events": "div,splits",
        }
    )
    url = f"{YAHOO_CHART_URL.format(symbol=issue.symbol)}?{params}"
    for attempt in range(5):
        try:
            response = subprocess.run(
                [
                    "curl",
                    "-A",
                    USER_AGENT,
                    "-L",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--max-time",
                    "30",
                    url,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=35,
            )
            if response.returncode != 0:
                raise RuntimeError(response.stderr.strip() or f"curl exit {response.returncode}")
            payload = json.loads(response.stdout)
            error = payload.get("chart", {}).get("error")
            if error:
                return issue, None, str(error)
            cache_path.write_text(json.dumps(payload, ensure_ascii=False))
            return issue, payload, None
        except (json.JSONDecodeError, RuntimeError, subprocess.TimeoutExpired) as exc:
            if attempt == 4:
                return issue, None, f"{type(exc).__name__}: {exc}"
            time.sleep((2**attempt) + random.random())
    return issue, None, "unreachable"


def parse_prices(payload: dict[str, Any]) -> list[Price]:
    result = payload["chart"]["result"][0]
    timestamps = result.get("timestamp") or []
    quote = result["indicators"]["quote"][0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    timezone_name = result.get("meta", {}).get("exchangeTimezoneName", "Asia/Tokyo")
    try:
        from zoneinfo import ZoneInfo

        exchange_timezone = ZoneInfo(timezone_name)
    except Exception:
        exchange_timezone = timezone.utc

    prices: list[Price] = []
    for index, timestamp in enumerate(timestamps):
        close = closes[index] if index < len(closes) else None
        if close is None or close <= 0:
            continue
        volume = volumes[index] if index < len(volumes) else None
        # Yahoo may emit zero-volume weekday placeholders on TSE holidays with
        # the previous close carried forward. They are not executable sessions.
        if volume is not None and volume <= 0:
            continue
        prices.append(
            Price(
                day=datetime.fromtimestamp(timestamp, exchange_timezone).date(),
                close=float(close),
                volume=int(volume) if volume is not None else None,
            )
        )
    return prices


def month_ends(prices: Iterable[Price]) -> list[Price]:
    by_month: dict[tuple[int, int], Price] = {}
    for price in prices:
        by_month[(price.day.year, price.day.month)] = price
    return sorted(by_month.values(), key=lambda item: item.day)


def add_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(month=2, day=28, year=day.year + years)


def data_quality_flags(
    daily: list[Price],
    max_adjacent_multiple: float,
    max_trading_gap_days: int,
) -> list[dict[str, Any]]:
    """Find patterns likely caused by code reuse or adjustment corruption.

    Split-adjusted daily closes should not change by more than 2.5x overnight
    on the TSE. A gap longer than 45 calendar days can be a long suspension, but
    it can also join two different securities that reused the same code. Both
    cases are conservatively excluded and written to a separate audit file.
    """

    flags: list[dict[str, Any]] = []
    minimum_multiple = 1 / max_adjacent_multiple
    for previous, current in zip(daily, daily[1:]):
        gap_days = (current.day - previous.day).days
        if gap_days > max_trading_gap_days:
            flags.append(
                {
                    "kind": "long_trading_gap",
                    "from_date": previous.day.isoformat(),
                    "to_date": current.day.isoformat(),
                    "value": gap_days,
                }
            )
        multiple = current.close / previous.close
        if multiple > max_adjacent_multiple or multiple < minimum_multiple:
            flags.append(
                {
                    "kind": "impossible_adjacent_return",
                    "from_date": previous.day.isoformat(),
                    "to_date": current.day.isoformat(),
                    "value": round(multiple, 6),
                }
            )
    return flags


def maximum_drawdown(prices: list[Price]) -> float | None:
    if not prices:
        return None
    peak = prices[0].close
    drawdown = 0.0
    for price in prices:
        peak = max(peak, price.close)
        drawdown = min(drawdown, price.close / peak - 1)
    return drawdown


def earliest_episode(
    daily: list[Price],
    monthly: list[Price],
    scan_start: date,
    data_end: date,
    years: int,
    max_entry_lag_days: int,
) -> dict[str, Any] | None:
    daily_days = [price.day for price in daily]
    monthly_days = [price.day for price in monthly]
    for evaluation in monthly:
        if evaluation.day < scan_start:
            continue
        entry_index = bisect.bisect_right(daily_days, evaluation.day)
        if entry_index >= len(daily):
            continue
        entry = daily[entry_index]
        if (entry.day - evaluation.day).days > max_entry_lag_days:
            continue
        deadline = add_years(entry.day, years)
        if deadline > data_end:
            continue
        month_start = bisect.bisect_left(monthly_days, entry.day)
        month_end = bisect.bisect_right(monthly_days, deadline)
        future_months = monthly[month_start:month_end]
        if not future_months:
            continue
        peak = max(future_months, key=lambda item: item.close / entry.close)
        multiple = peak.close / entry.close
        if multiple < 10:
            continue
        hit = next(price for price in future_months if price.close / entry.close >= 10)
        hit_index = bisect.bisect_right(daily_days, hit.day)
        daily_window = daily[entry_index:hit_index]
        trough = min(daily_window, key=lambda item: item.close) if daily_window else entry
        after_entry = daily[entry_index:]
        after_hit = daily[max(entry_index, hit_index - 1) :]
        latest = after_entry[-1]
        lifetime_peak = max(after_entry, key=lambda item: item.close / entry.close)
        post_hit_low = min(after_hit, key=lambda item: item.close) if after_hit else hit
        post_hit_drawdown = maximum_drawdown(after_hit)
        return {
            "evaluation_date": evaluation.day.isoformat(),
            "entry_date": entry.day.isoformat(),
            "entry_close": round(entry.close, 6),
            "hit_date": hit.day.isoformat(),
            "hit_close": round(hit.close, 6),
            "peak_date": peak.day.isoformat(),
            "peak_close": round(peak.close, 6),
            "peak_multiple": round(multiple, 4),
            "days_to_10x": (hit.day - entry.day).days,
            "pre_hit_drawdown": round(trough.close / entry.close - 1, 4),
            "latest_date": latest.day.isoformat(),
            "latest_close": round(latest.close, 6),
            "latest_multiple": round(latest.close / entry.close, 4),
            "retained_10x": latest.close / entry.close >= 10,
            "lifetime_peak_date": lifetime_peak.day.isoformat(),
            "lifetime_peak_close": round(lifetime_peak.close, 6),
            "lifetime_peak_multiple": round(lifetime_peak.close / entry.close, 4),
            "peak_to_latest_drawdown": round(latest.close / lifetime_peak.close - 1, 4),
            "post_hit_low_multiple": round(post_hit_low.close / entry.close, 4),
            "post_hit_max_drawdown": (
                round(post_hit_drawdown, 4) if post_hit_drawdown is not None else ""
            ),
            "fell_below_entry_after_hit": post_hit_low.close < entry.close,
        }
    return None


def scan_issue(
    issue: Issue,
    payload: dict[str, Any],
    scan_start: date,
    data_end: date,
    max_adjacent_multiple: float,
    max_trading_gap_days: int,
    max_entry_lag_days: int,
) -> dict[str, Any]:
    daily = parse_prices(payload)
    quality_flags = data_quality_flags(
        daily,
        max_adjacent_multiple=max_adjacent_multiple,
        max_trading_gap_days=max_trading_gap_days,
    )
    monthly = month_ends(daily)
    episode_2y = None
    episode_3y = None
    if not quality_flags:
        episode_2y = earliest_episode(
            daily, monthly, scan_start, data_end, 2, max_entry_lag_days
        )
        episode_3y = earliest_episode(
            daily, monthly, scan_start, data_end, 3, max_entry_lag_days
        )
    return {
        "code": issue.code,
        "name": issue.name,
        "market": issue.market,
        "sector": issue.sector,
        "price_start": daily[0].day.isoformat() if daily else "",
        "price_end": daily[-1].day.isoformat() if daily else "",
        "observations": len(daily),
        "quality_flags": quality_flags,
        "episode_2y": episode_2y,
        "episode_3y": episode_3y,
    }


def flatten_candidate(result: dict[str, Any]) -> dict[str, Any]:
    row = {
        key: result[key]
        for key in ("code", "name", "market", "sector", "price_start", "price_end")
    }
    row["data_quality"] = "ok"
    for years in (2, 3):
        episode = result[f"episode_{years}y"]
        prefix = f"{years}y_"
        row[prefix + "qualified"] = bool(episode)
        for key in (
            "evaluation_date",
            "entry_date",
            "entry_close",
            "hit_date",
            "hit_close",
            "peak_date",
            "peak_close",
            "peak_multiple",
            "days_to_10x",
            "pre_hit_drawdown",
            "latest_date",
            "latest_close",
            "latest_multiple",
            "retained_10x",
            "lifetime_peak_date",
            "lifetime_peak_close",
            "lifetime_peak_multiple",
            "peak_to_latest_drawdown",
            "post_hit_low_multiple",
            "post_hit_max_drawdown",
            "fell_below_entry_after_hit",
        ):
            row[prefix + key] = episode[key] if episode else ""
    return row


def write_csv(path: Path, results: list[dict[str, Any]]) -> None:
    rows = [flatten_candidate(result) for result in results if result["episode_2y"] or result["episode_3y"]]
    rows.sort(key=lambda row: (not row["3y_qualified"], row["3y_evaluation_date"] or "9999", row["code"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_quality_csv(path: Path, results: list[dict[str, Any]]) -> int:
    rows: list[dict[str, Any]] = []
    for result in results:
        for flag in result["quality_flags"]:
            rows.append(
                {
                    "code": result["code"],
                    "name": result["name"],
                    "market": result["market"],
                    "kind": flag["kind"],
                    "from_date": flag["from_date"],
                    "to_date": flag["to_date"],
                    "value": flag["value"],
                }
            )
    if not rows:
        if path.exists():
            path.unlink()
        return 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return len({row["code"] for row in rows})


def main() -> int:
    args = parse_args()
    issues = load_issues(args.jpx_xls)
    if args.limit is not None:
        issues = issues[: args.limit]
    print(f"universe={len(issues)}", flush=True)
    fetched: list[tuple[Issue, dict[str, Any]]] = []
    errors: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                fetch_chart,
                issue,
                args.cache_dir,
                args.data_start,
                args.data_end,
                args.refresh,
            ): issue
            for issue in issues
        }
        for index, future in enumerate(as_completed(futures), 1):
            issue, payload, error = future.result()
            if payload is None:
                errors.append((issue.code, error or "unknown error"))
            else:
                fetched.append((issue, payload))
            if index % 100 == 0 or index == len(futures):
                print(f"fetched={index}/{len(futures)} errors={len(errors)}", flush=True)

    results = [
        scan_issue(
            issue,
            payload,
            args.scan_start,
            args.data_end,
            args.max_adjacent_multiple,
            args.max_trading_gap_days,
            args.max_entry_lag_days,
        )
        for issue, payload in sorted(fetched, key=lambda item: item[0].code)
    ]
    write_csv(args.output, results)
    quality_path = args.output.with_suffix(".quality.csv")
    excluded_quality = write_quality_csv(quality_path, results)
    candidates = sum(1 for result in results if result["episode_2y"] or result["episode_3y"])
    print(
        f"usable={len(results)} errors={len(errors)} "
        f"quality_excluded={excluded_quality} candidates={candidates} output={args.output}",
        flush=True,
    )
    if excluded_quality:
        print(f"quality_output={quality_path}", flush=True)
    if errors:
        error_path = args.output.with_suffix(".errors.csv")
        with error_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["code", "error"])
            writer.writerows(sorted(errors))
        print(f"errors_output={error_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
