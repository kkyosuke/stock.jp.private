"""Collect raw point-in-time series required by MRS-v0.1."""

from __future__ import annotations

import csv
from datetime import date, datetime, time as daytime
from html import unescape
from io import BytesIO, StringIO
import re
import math
import unicodedata
import ssl
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import certifi
import pdfplumber
from pypdf import PdfReader


JST = ZoneInfo("Asia/Tokyo")
TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class MarketRegimeSourceError(RuntimeError):
    pass


def _download(url: str, *, timeout: int, attempts: int = 3) -> tuple[bytes, str]:
    request = Request(
        url,
        headers={
            "User-Agent": "stock.jp research operation/1.0 (+https://github.com/kkyosuke/stock.jp.private)",
            "Accept": "application/json,text/csv,text/html,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
        },
    )
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout, context=TLS_CONTEXT) as response:
                return response.read(), response.geturl()
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt + 1 == attempts:
                raise MarketRegimeSourceError(
                    f"HTTP {error.code} from {url}"
                ) from error
        except URLError as error:
            if attempt + 1 == attempts:
                raise MarketRegimeSourceError(
                    f"network error from {url}: {error.reason}"
                ) from error
        time.sleep(0.5 * (2**attempt))
    raise AssertionError("unreachable")


def _jpx_pdf_links(page_urls: list[str], *, timeout: int) -> list[str]:
    links: set[str] = set()
    for page_url in page_urls:
        page_bytes, final_page_url = _download(page_url, timeout=timeout)
        try:
            page = page_bytes.decode("utf-8")
        except UnicodeDecodeError:
            page = page_bytes.decode("shift_jis")
        for match in re.finditer(
            r'href=["\']([^"\']*/03_sisu\d{4}\.pdf)["\']', page, flags=re.IGNORECASE
        ):
            links.add(urljoin(final_page_url, unescape(match.group(1))))
    return sorted(links)


# Reviewed table 3-1 layouts; the names describe every numeric column.
_JPX_BASE = ("TOPIX", "Dividend-included TOPIX", "Ex-TOPIX", "Prime Market",
             "Standard Market", "Growth Market", "JPX Prime 150 Index")
_JPX_2025 = _JPX_BASE + ("Growth250", "REIT Index", "Infrastructure Funds Index",
                       "Prime weighted average", "Prime arithmetic average",
                       "Standard weighted average", "Standard arithmetic average")
_JPX_2026 = _JPX_BASE + ("JPX Start-Up Acceleration 100 Index", "Growth250",
                       "REIT Index", "Infrastructure Funds Index")
JPX_EXTRACTION_VERSION = "jpx-table-3-1-named-layout-v2"


def _jpx_layout(text: str, period: date, headers: tuple[str, ...] = ()) -> tuple[str, ...]:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    required = ("TOPIX", "JPXPrime150Index", "TokyoStockExchangeGrowthMarket250Index",
                "REITIndex", "InfrastructureFundsIndex", "StandardMarket", "GrowthMarket")
    if not all(label in compact for label in required):
        raise MarketRegimeSourceError("unknown JPX table 3-1 column labels; re-extraction review required")
    old = "StockPriceIndex&StockPriceAverage(EndofYear/Month/Daily)" in compact
    new = "StockPriceIndex(EndofYear/Month/Daily)" in compact
    startup = "JPXStart-UpAcceleration100Index" in compact
    if old and not new and not startup and date(2025, 1, 1) <= period < date(2026, 3, 1):
        columns = _JPX_2025
    elif new and not old and startup and period >= date(2026, 3, 1):
        columns = _JPX_2026
    else:
        raise MarketRegimeSourceError("unknown or ambiguous JPX table 3-1 layout")
    if headers != columns:
        raise MarketRegimeSourceError("missing, extra, reordered or unknown JPX physical column headers")
    return columns



def _english_header(text: str) -> str:
    # Japanese and English are separate lines in the reviewed bilingual cells.
    # Keep all English fragments; unknown English labels must not be discarded.
    lines = [line for line in unicodedata.normalize("NFKC", text).splitlines()
             if not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", line)]
    return re.sub(r"[^A-Za-z0-9]", "", "".join(lines))


_JPX_HEADER_NAMES = {
    "TOPIX": "TOPIX", "TOPIXTOPIX": "TOPIX",
    "DividendincludedTOPIX": "Dividend-included TOPIX",
    "TOPIXDividendincludedTOPIX": "Dividend-included TOPIX",
    "ExTOPIX": "Ex-TOPIX", "TOPIXExTOPIX": "Ex-TOPIX",
    "PrimeMarket": "Prime Market", "StandardMarket": "Standard Market",
    "GrowthMarket": "Growth Market", "JPXPrime150Index": "JPX Prime 150 Index",
    "JPXStartUpAcceleration100Index": "JPX Start-Up Acceleration 100 Index",
    "TokyoStockExchangeGrowthMarket250Index": "Growth250",
    "REITIndex": "REIT Index", "InfrastructureFundsIndex": "Infrastructure Funds Index",
    "PrimeMarketWeightedStockPriceAverage": "Prime weighted average",
    "PrimeMarketArithmeticStockPriceAverage": "Prime arithmetic average",
    "StandardMarketWeightedStockPriceAverage": "Standard weighted average",
    "StandardMarketArithmeticStockPriceAverage": "Standard arithmetic average",
}


def _jpx_physical_headers(payload: bytes) -> tuple[str, ...]:
    """Read headers in physical left-to-right cells, including merged parents.

    No plain-text-order fallback: without a complete ruled table the format is
    unknown. All header cells and numeric columns must be accounted for.
    """
    recognized = []
    with pdfplumber.open(BytesIO(payload)) as document:
        page = document.pages[0]
        for table in page.find_tables():
            matrix = table.extract()
            body = None
            header_end = None
            indices = []
            for row_index, row in enumerate(matrix):
                numeric = [i for i, cell in enumerate(row) if cell and re.fullmatch(
                    r"[◎●r\s]*\d[\d,]*\.\d+[◎●r\s]*", cell)]
                if header_end is None and len(numeric) >= 2:
                    header_end = row_index
                if len(numeric) in {11, 14}:
                    body, indices = row_index, numeric
                    break
            if body is None or not header_end:
                continue
            if indices != list(range(indices[0], indices[0] + len(indices))):
                raise MarketRegimeSourceError("non-contiguous JPX numeric columns")
            cells = [table.rows[body].cells[i] for i in indices]
            if any(cell is None for cell in cells):
                raise MarketRegimeSourceError("missing JPX body cell geometry")
            centers = [(cell[0] + cell[2]) / 2 for cell in cells]
            fragments = [[] for _ in cells]
            seen = set()
            for row in table.rows[:header_end]:
                for box in row.cells:
                    if box is None or box in seen:
                        continue
                    seen.add(box)
                    label = _english_header(page.crop(box).extract_text() or "")
                    if not label or label in {"StockPriceIndex", "StockPriceAverage", "YearMonthorDay"}:
                        continue
                    covered = [i for i, center in enumerate(centers) if box[0] < center < box[2]]
                    if not covered:
                        raise MarketRegimeSourceError("extra JPX header without a numeric column")
                    for i in covered:
                        fragments[i].append(label)
            labels = tuple(_JPX_HEADER_NAMES.get("".join(parts), "UNKNOWN:" + "".join(parts))
                           for parts in fragments)
            recognized.append(labels)
    if len(recognized) != 1:
        raise MarketRegimeSourceError("JPX physical header table is absent or ambiguous")
    return recognized[0]


def _jpx_month_points(
    payload: bytes, url: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    match = re.search(r"03_sisu(\d{2})(\d{2})\.pdf", url)
    if not match:
        raise MarketRegimeSourceError(f"cannot identify year/month from {url}")
    year, month = 2000 + int(match.group(1)), int(match.group(2))
    try:
        text = PdfReader(BytesIO(payload)).pages[0].extract_text()
    except Exception as error:
        raise MarketRegimeSourceError(
            f"cannot parse JPX monthly index PDF {url}"
        ) from error
    try:
        headers = _jpx_physical_headers(payload)
    except MarketRegimeSourceError:
        raise
    except Exception as error:
        raise MarketRegimeSourceError("cannot verify JPX physical column layout") from error
    columns = _jpx_layout(text, date(year, month, 1), headers)
    lines = text.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(rf"^{month}\.\d{{1,2}}(?:\s+|$)", line.strip())
        ),
        None,
    )
    if start is None:
        raise MarketRegimeSourceError(f"daily index table was not found in {url}")
    topix: list[dict[str, Any]] = []
    growth: list[dict[str, Any]] = []
    previous_day = 0
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("平均"):
            break
        first = re.match(rf"^(?:{month}\.)?(\d{{1,2}})(?:\s+|$)", stripped)
        if not first:
            continue
        day = int(first.group(1))
        try:
            session = date(year, month, day)
        except ValueError as error:
            raise MarketRegimeSourceError("invalid JPX daily date") from error
        if day <= previous_day:
            raise MarketRegimeSourceError("duplicate or unordered JPX daily date")
        previous_day = day
        cells = re.sub(r"[◎●r]", "", stripped[first.end() :]).strip()
        if not cells:
            continue  # Explicit blank calendar rows are holidays.
        if not re.fullmatch(r"[\d, .\s]+", cells):
            raise MarketRegimeSourceError("unrecognized JPX daily value or annotation")
        tokens = cells.split()
        if any(not re.fullmatch(r"\d[\d,]*\.\d+", value) for value in tokens):
            raise MarketRegimeSourceError("malformed JPX numeric cell")
        numbers = [float(value.replace(",", "")) for value in tokens]
        if len(numbers) != len(columns) or any(not math.isfinite(n) or n <= 0 for n in numbers):
            raise MarketRegimeSourceError("JPX daily row does not match the reviewed named layout")
        values = dict(zip(columns, numbers))
        topix.append({"date": session.isoformat(), "value": values["TOPIX"]})
        growth.append({"date": session.isoformat(), "value": values["Growth250"]})
    if not topix:
        raise MarketRegimeSourceError(f"no daily index values parsed from {url}")
    return topix, growth


def _jpx_index_points(
    page_urls: list[str], *, as_of: date, timeout: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    links = _jpx_pdf_links(page_urls, timeout=timeout)
    applicable = [
        url
        for url in links
        if (match := re.search(r"03_sisu(\d{2})(\d{2})\.pdf", url))
        and date(2000 + int(match.group(1)), int(match.group(2)), 1)
        < as_of.replace(day=1)
    ]
    applicable.sort(
        key=lambda url: tuple(
            map(int, re.search(r"03_sisu(\d{2})(\d{2})\.pdf", url).groups())
        )
    )
    selected = applicable[-14:]
    if not selected:
        raise MarketRegimeSourceError(
            "JPX monthly statistics contain no applicable index PDFs"
        )
    topix_by_date: dict[str, dict[str, Any]] = {}
    growth_by_date: dict[str, dict[str, Any]] = {}
    used_urls: list[str] = []
    for url in selected:
        payload, final_url = _download(url, timeout=timeout)
        topix, growth = _jpx_month_points(payload, final_url)
        topix_by_date.update(
            {
                point["date"]: point
                for point in topix
                if date.fromisoformat(point["date"]) <= as_of
            }
        )
        growth_by_date.update(
            {
                point["date"]: point
                for point in growth
                if date.fromisoformat(point["date"]) <= as_of
            }
        )
        used_urls.append(final_url)
    topix_points = [topix_by_date[key] for key in sorted(topix_by_date)]
    growth_points = [growth_by_date[key] for key in sorted(growth_by_date)]
    if len(topix_points) < 200 or len(growth_points) < 200:
        raise MarketRegimeSourceError(
            f"JPX monthly PDFs yielded TOPIX={len(topix_points)}, Growth250={len(growth_points)} observations"
        )
    if topix_points[-1]["date"] != as_of.isoformat() or growth_points[-1]["date"] != as_of.isoformat():
        raise MarketRegimeSourceError(
            "JPX monthly PDFs cannot supply an as_of close available on that date; verified daily index evidence is required"
        )
    return topix_points, growth_points, used_urls


def _nikkei_vi_points(
    url: str, *, as_of: date, timeout: int
) -> tuple[list[dict[str, Any]], str]:
    payload, final_url = _download(url, timeout=timeout)
    try:
        try:
            decoded = payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            decoded = payload.decode("cp932")
        rows = csv.DictReader(StringIO(decoded))
        points = [
            {
                "date": datetime.strptime(row["Date of Data"], "%Y/%m/%d")
                .date()
                .isoformat(),
                "value": float(row["Close"]),
            }
            for row in rows
            if row.get("Close")
            and datetime.strptime(row["Date of Data"], "%Y/%m/%d").date() <= as_of
        ]
    except (KeyError, UnicodeDecodeError, ValueError) as error:
        raise MarketRegimeSourceError("invalid Nikkei VI daily CSV") from error
    if not points:
        raise MarketRegimeSourceError("Nikkei VI daily CSV contained no usable rows")
    return points, final_url


def _information_cutoff(as_of: date, cutoff_at: datetime | None = None) -> datetime:
    cutoff = cutoff_at or datetime.combine(as_of, daytime.max, tzinfo=JST)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise MarketRegimeSourceError("information cutoff must include a UTC offset")
    cutoff = cutoff.astimezone(JST)
    if cutoff.date() != as_of:
        raise MarketRegimeSourceError("information cutoff must remain on the declared as_of date")
    return cutoff


def _ci_vintage(payload: bytes, url: str) -> tuple[list[dict[str, Any]], datetime]:
    if not url.startswith("https://www.esri.cao.go.jp/jp/stat/di/"):
        raise MarketRegimeSourceError("CI vintage must use the official Cabinet Office origin")
    match = re.search(r"/(20\d{2})(\d{2})(?:report|rsummary)\.pdf$", url)
    if not match:
        raise MarketRegimeSourceError("unrecognized Cabinet Office CI vintage URL")
    year, month = map(int, match.groups())
    try:
        pages = [unicodedata.normalize("NFKC", p.extract_text() or "")
                 for p in PdfReader(BytesIO(payload)).pages[:3]]
        dates = re.findall(r"^令和\d+\((20\d{2})\)年(\d{1,2})月(\d{1,2})日$",
                           re.sub(r"[ \t]", "", pages[0]), flags=re.MULTILINE)
        if len(set(dates)) != 1:
            raise ValueError("publication date is absent or ambiguous")
        published = datetime(*map(int, dates[0]), 14, tzinfo=JST)
        rows = []
        for page in pages:
            lines = page.splitlines()
            for index, line in enumerate(lines):
                found = re.fullmatch(r"\s*CI先行指数\s+([\d.\s]+)", line)
                if not found:
                    continue
                months = re.findall(r"(\d{1,2})月", lines[index - 1]) if index else []
                values = [float(v) for v in found.group(1).split()]
                expected = [((month - 6 + i) % 12) + 1 for i in range(6)]
                if list(map(int, months)) != expected or len(values) != 6:
                    raise ValueError("CI six-month table layout is invalid")
                if any(not math.isfinite(v) or v <= 0 for v in values):
                    raise ValueError("CI value is invalid")
                rows.append([{"period": f"{year if m <= month else year - 1:04d}-{m:02d}", "value": v}
                             for m, v in zip(expected, values)])
        if len(rows) != 1 or published.date() <= date(year, month, 1):
            raise ValueError("CI vintage table is absent or ambiguous")
        return rows[0], published
    except Exception as error:
        raise MarketRegimeSourceError("invalid Cabinet Office CI vintage; manual verification required") from error


def _leading_ci(page_url: str, *, as_of: date, timeout: int,
                cutoff_at: datetime | None = None) -> tuple[list[dict[str, Any]], str, str]:
    cutoff = _information_cutoff(as_of, cutoff_at)
    page_bytes, final_page = _download(page_url, timeout=timeout)
    page = page_bytes.decode("utf-8")
    # Each PDF contains its own revised history. Never splice a current workbook
    # into an earlier release or combine latest/prior observations across vintages.
    links = {urljoin(final_page, unescape(m.group(1))) for m in re.finditer(
        r'href=["\']([^"\']*20\d{4}(?:report|rsummary)\.pdf)["\']', page)}
    month_id = as_of.year * 12 + as_of.month
    candidates = []
    for url in links:
        match = re.search(r"/(20\d{2})(\d{2})(?:report|rsummary)\.pdf$", url)
        year, month = map(int, match.groups())
        if 0 <= month_id - (year * 12 + month) <= 3:
            candidates.append(url)
    if not candidates or len(candidates) > 8:
        raise MarketRegimeSourceError("no bounded set of historical CI vintages; manual verification required")
    available = []
    for url in sorted(candidates):
        payload, final_url = _download(url, timeout=timeout)
        points, published = _ci_vintage(payload, final_url)
        if published <= cutoff:
            available.append((published, final_url, points))
    if not available:
        raise MarketRegimeSourceError("no CI vintage was available at the information cutoff")
    latest = max(item[0] for item in available)
    selected = [item for item in available if item[0] == latest]
    if len(selected) != 1:
        raise MarketRegimeSourceError("ambiguous CI release vintage at information cutoff")
    published, url, points = selected[0]
    return points, url, published.isoformat(timespec="seconds")


def collect_market_regime_series(
    *, as_of: date, config: dict[str, Any], cutoff_at: datetime | None = None
) -> dict[str, Any]:
    """Download raw inputs; any missing provider fails the whole formal MRS."""

    timeout = int(config.get("request_timeout_seconds", 30))
    market = config.get("market_regime", {})
    if not isinstance(market, dict) or market.get("enabled") is not True:
        raise MarketRegimeSourceError("market_regime collection is disabled")
    pages = market.get("jpx_monthly_statistics_pages")
    if not isinstance(pages, list) or not pages:
        raise MarketRegimeSourceError(
            "market_regime.jpx_monthly_statistics_pages is empty"
        )
    topix, growth, jpx_urls = _jpx_index_points(
        [str(value) for value in pages], as_of=as_of, timeout=timeout
    )
    vi, vi_url = _nikkei_vi_points(
        str(market["nikkei_vi_csv_url"]), as_of=as_of, timeout=timeout
    )
    ci, ci_url, ci_available = _leading_ci(
        str(market["leading_ci_page_url"]), as_of=as_of, timeout=timeout, cutoff_at=cutoff_at
    )
    return {
        "schema_version": "1.0",
        "as_of": as_of.isoformat(),
        "information_cutoff_at_jst": _information_cutoff(as_of, cutoff_at).isoformat(),
        "minimum_vi_observations": int(market.get("minimum_vi_observations", 500)),
        "minimum_breadth_coverage": float(market.get("minimum_breadth_coverage", 0.98)),
        "breadth_lookback_sessions": int(market.get("breadth_lookback_sessions", 280)),
        "maximum_breadth_close_age_days": int(
            market.get("maximum_breadth_close_age_days", 7)
        ),
        "series": {
            "topix": {
                "source_url": "|".join(jpx_urls),
                "primary_source": True,
                "points": topix,
            },
            "growth250": {
                "extraction_version": JPX_EXTRACTION_VERSION,
                "source_url": "|".join(jpx_urls),
                "primary_source": True,
                "points": growth,
            },
            "nikkei_vi": {"source_url": vi_url, "primary_source": True, "points": vi},
            "leading_ci": {
                "source_url": ci_url,
                "primary_source": True,
                "available_at_jst": ci_available,
                "points": ci,
            },
        },
    }
