#!/usr/bin/env python3
"""Collect daily OHLCV for every current TSE domestic stock.

The stock universe comes from JPX's monthly spreadsheet. Prices come from the
unofficial Yahoo Finance chart endpoint and are therefore discovery data, not
an official input for order decisions.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import tempfile
import time
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from stock_jp.research.price_episodes import Issue, load_issues


JPX_LIST_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/"
    "tvdivq0000001vg2-att/data_j.xlsx"
)
YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
USER_AGENT = "Mozilla/5.0 (compatible; stock.jp daily research/0.1)"
JST = ZoneInfo("Asia/Tokyo")
CSV_FIELDS = (
    "日付",
    "銘柄コード",
    "銘柄名",
    "市場・商品区分",
    "33業種区分",
    "始値",
    "高値",
    "安値",
    "終値",
    "前日比",
    "前日比％",
    "売買高(株)",
    "取得状態",
)


class PriceCollectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class DailyBar:
    day: date
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: int | None


@dataclass(frozen=True)
class SymbolResult:
    issue: Issue
    bars: tuple[DailyBar, ...]
    error: str | None = None


JsonRequester = Callable[[str, int], dict[str, Any]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jpx-xls", type=Path)
    parser.add_argument("--jpx-url", default=JPX_LIST_URL)
    parser.add_argument("--output-dir", type=Path, default=Path("data/daily-prices"))
    parser.add_argument("--through-date", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=7,
        help="calendar days to write; extra history is fetched for the prior close",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--minimum-fetch-coverage",
        type=float,
        default=0.98,
        help="abort without changing tracked data below this successful-request ratio",
    )
    parser.add_argument("--summary-output", type=Path)
    parser.add_argument("--limit", type=int, help=argparse.SUPPRESS)
    return parser.parse_args()


def _download_file(url: str, destination: Path, timeout: int) -> None:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            content = response.read()
    except (HTTPError, URLError, TimeoutError) as error:
        raise PriceCollectionError(f"JPX list download failed: {error}") from error
    if len(content) < 10_000:
        raise PriceCollectionError("JPX list download was unexpectedly small")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)


def _request_json(url: str, timeout: int) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get_content_type()
        body = response.read()
    if content_type not in {"application/json", "text/plain"}:
        raise PriceCollectionError(f"unexpected Yahoo content type: {content_type}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PriceCollectionError("Yahoo returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise PriceCollectionError("Yahoo returned an unexpected JSON shape")
    return payload


def _number_at(values: Any, index: int) -> float | None:
    if not isinstance(values, list) or index >= len(values):
        return None
    value = values[index]
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_chart_payload(payload: dict[str, Any]) -> tuple[DailyBar, ...]:
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        raise PriceCollectionError("Yahoo response is missing chart")
    error = chart.get("error")
    if error:
        if isinstance(error, dict):
            description = error.get("description") or error.get("code") or str(error)
        else:
            description = str(error)
        raise PriceCollectionError(f"Yahoo chart error: {description}")
    results = chart.get("result")
    if not isinstance(results, list) or not results:
        raise PriceCollectionError("Yahoo response has no chart result")
    result = results[0]
    if not isinstance(result, dict):
        raise PriceCollectionError("Yahoo chart result is invalid")

    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quotes = indicators.get("quote") or []
    if not isinstance(timestamps, list) or not quotes or not isinstance(quotes[0], dict):
        return ()
    quote = quotes[0]
    metadata = result.get("meta") or {}
    timezone_name = metadata.get("exchangeTimezoneName") or "Asia/Tokyo"
    try:
        exchange_timezone = ZoneInfo(str(timezone_name))
    except Exception:
        exchange_timezone = JST

    bars: list[DailyBar] = []
    for index, raw_timestamp in enumerate(timestamps):
        close = _number_at(quote.get("close"), index)
        if close is None or close <= 0:
            continue
        volume_number = _number_at(quote.get("volume"), index)
        volume = int(volume_number) if volume_number is not None else None
        # Yahoo can include a carried-forward holiday placeholder. It is not a
        # trading session and must not become a daily file.
        if volume is not None and volume <= 0:
            continue
        try:
            day = datetime.fromtimestamp(int(raw_timestamp), exchange_timezone).date()
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        bars.append(
            DailyBar(
                day=day,
                open=_number_at(quote.get("open"), index),
                high=_number_at(quote.get("high"), index),
                low=_number_at(quote.get("low"), index),
                close=close,
                volume=volume,
            )
        )
    return tuple(sorted(bars, key=lambda item: item.day))


def _chart_url(host: str, issue: Issue, start: date, end: date) -> str:
    period1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    exclusive_end = end + timedelta(days=1)
    period2 = int(
        datetime(
            exclusive_end.year,
            exclusive_end.month,
            exclusive_end.day,
            tzinfo=timezone.utc,
        ).timestamp()
    )
    params = urlencode(
        {
            "period1": period1,
            "period2": period2,
            "interval": "1d",
            "events": "div,splits",
        }
    )
    return f"https://{host}/v8/finance/chart/{issue.symbol}?{params}"


def fetch_issue(
    issue: Issue,
    *,
    start: date,
    end: date,
    timeout: int,
    max_attempts: int,
    requester: JsonRequester = _request_json,
) -> SymbolResult:
    errors: list[str] = []
    for host in YAHOO_HOSTS:
        url = _chart_url(host, issue, start, end)
        for attempt in range(max_attempts):
            try:
                return SymbolResult(issue=issue, bars=parse_chart_payload(requester(url, timeout)))
            except (HTTPError, URLError, TimeoutError, PriceCollectionError) as error:
                errors.append(f"{host}: {type(error).__name__}: {error}")
                retryable = not isinstance(error, HTTPError) or error.code == 429 or error.code >= 500
                if not retryable:
                    break
                if attempt + 1 < max_attempts:
                    time.sleep((2**attempt) + random.random())
    return SymbolResult(issue=issue, bars=(), error=" | ".join(errors)[-1000:])


def _format_number(value: float | None, *, percent: bool = False) -> str:
    if value is None or not math.isfinite(value):
        return ""
    precision = 6 if percent else 4
    text = f"{value:.{precision}f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def _csv_text(rows: Iterable[dict[str, str]]) -> str:
    import io

    destination = io.StringIO(newline="")
    writer = csv.DictWriter(destination, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return destination.getvalue()


def _write_if_changed(path: Path, content: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)
    return True


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def write_dataset(
    *,
    output_dir: Path,
    issues: list[Issue],
    results: list[SymbolResult],
    output_start: date,
    through_date: date,
) -> dict[str, Any]:
    by_code = {result.issue.code: result for result in results}
    trading_days = sorted(
        {
            bar.day
            for result in results
            for bar in result.bars
            if output_start <= bar.day <= through_date
        }
    )
    if not trading_days:
        raise PriceCollectionError("no trading sessions were returned in the requested window")

    changed_files: list[str] = []
    day_summaries: list[dict[str, Any]] = []
    for trading_day in trading_days:
        rows: list[dict[str, str]] = []
        quote_count = 0
        no_quote_count = 0
        fetch_error_count = 0
        for issue in issues:
            result = by_code[issue.code]
            bars_by_day = {bar.day: (index, bar) for index, bar in enumerate(result.bars)}
            indexed_bar = bars_by_day.get(trading_day)
            if result.error:
                status = "FETCH_ERROR"
                bar = None
                previous = None
                fetch_error_count += 1
            elif indexed_bar is None:
                status = "NO_QUOTE"
                bar = None
                previous = None
                no_quote_count += 1
            else:
                index, bar = indexed_bar
                previous = result.bars[index - 1] if index else None
                status = "OK"
                quote_count += 1

            change = None
            change_pct = None
            if bar is not None and previous is not None and previous.close:
                change = bar.close - previous.close
                change_pct = change / previous.close * 100
            rows.append(
                {
                    "日付": trading_day.isoformat(),
                    "銘柄コード": issue.code,
                    "銘柄名": issue.name,
                    "市場・商品区分": issue.market,
                    "33業種区分": issue.sector,
                    "始値": _format_number(bar.open if bar else None),
                    "高値": _format_number(bar.high if bar else None),
                    "安値": _format_number(bar.low if bar else None),
                    "終値": _format_number(bar.close if bar else None),
                    "前日比": _format_number(change),
                    "前日比％": _format_number(change_pct, percent=True),
                    "売買高(株)": str(bar.volume) if bar and bar.volume is not None else "",
                    "取得状態": status,
                }
            )
        content = _csv_text(rows)
        relative_path = Path(str(trading_day.year)) / f"{trading_day.isoformat()}.csv"
        if _write_if_changed(output_dir / relative_path, content):
            changed_files.append(relative_path.as_posix())
        day_summaries.append(
            {
                "date": trading_day.isoformat(),
                "file": relative_path.as_posix(),
                "sha256": _sha256_text(content),
                "quote_count": quote_count,
                "no_quote_count": no_quote_count,
                "fetch_error_count": fetch_error_count,
            }
        )

    latest = day_summaries[-1]
    error_codes = sorted(result.issue.code for result in results if result.error)
    manifest = {
        "schema_version": 1,
        "source": {
            "provider": "Yahoo Finance",
            "endpoint": "/v8/finance/chart/<code>.T",
            "official": False,
        },
        "universe": {
            "provider": "JPX TSE-listed issues monthly spreadsheet",
            "scope": "current domestic stocks",
            "count": len(issues),
        },
        "latest_trading_date": latest["date"],
        "fetch": {
            "success_count": len(results) - len(error_codes),
            "error_count": len(error_codes),
            "error_codes": error_codes,
        },
        "latest_session": latest,
    }
    manifest_content = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if _write_if_changed(output_dir / "latest.json", manifest_content):
        changed_files.append("latest.json")
    return {**manifest, "changed_files": changed_files, "sessions_written": len(trading_days)}


def collect(
    *,
    issues: list[Issue],
    output_dir: Path,
    through_date: date,
    lookback_days: int,
    workers: int,
    timeout: int,
    max_attempts: int,
    minimum_fetch_coverage: float,
    requester: JsonRequester = _request_json,
) -> dict[str, Any]:
    if lookback_days < 1:
        raise ValueError("lookback_days must be >= 1")
    if workers < 1:
        raise ValueError("workers must be >= 1")
    if not 0 < minimum_fetch_coverage <= 1:
        raise ValueError("minimum_fetch_coverage must be in (0, 1]")
    if not issues:
        raise PriceCollectionError("JPX universe is empty")

    from concurrent.futures import ThreadPoolExecutor

    output_start = through_date - timedelta(days=lookback_days - 1)
    # The leading overlap provides the previous trading close for the first
    # output date, even across a long weekend.
    fetch_start = output_start - timedelta(days=10)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(
            executor.map(
                lambda issue: fetch_issue(
                    issue,
                    start=fetch_start,
                    end=through_date,
                    timeout=timeout,
                    max_attempts=max_attempts,
                    requester=requester,
                ),
                issues,
            )
        )

    success_count = sum(result.error is None for result in results)
    coverage = success_count / len(results)
    if coverage < minimum_fetch_coverage:
        raise PriceCollectionError(
            f"fetch coverage {coverage:.2%} is below {minimum_fetch_coverage:.2%}; "
            "tracked data was not changed"
        )
    return write_dataset(
        output_dir=output_dir,
        issues=issues,
        results=results,
        output_start=output_start,
        through_date=through_date,
    )


def main() -> int:
    # Storage approval is not approval for automated acquisition or provider use.
    raise SystemExit(
        "market-data collection is disabled pending provider permission review; "
        "use the separately configured approved provider; private storage does not authorize Yahoo scraping"
    )


if __name__ == "__main__":
    raise SystemExit(main())
