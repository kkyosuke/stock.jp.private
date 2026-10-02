"""Versioned J-Quants V2 catalog; plan declarations are not proof of entitlement.

Source: https://jpx-jquants.com/en/spec/data-spec (reviewed 2026-10-01).
Every resource in the official reference is represented, including delivery APIs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta

CATALOG_VERSION = "2026-10-01"
BASE_URL = "https://api.jquants.com/v2"
PLANS = {"free": (0, 2, 5), "light": (1, 5, 60), "standard": (2, 10, 120), "premium": (3, 20, 500)}


@dataclass(frozen=True)
class Dataset:
    name: str
    endpoint: str
    minimum_plan: str
    earliest: str
    query: str = "date"
    addon: str | None = None
    role: str = "dataset"
    units: str = "provider_native; see endpoint schema; null remains null"

    def metadata(self) -> dict:
        return asdict(self)


CATALOG = (
    Dataset("equities_master", "/equities/master", "free", "2008-05-07"),
    Dataset("equities_daily", "/equities/bars/daily", "free", "2008-05-07", units="JPY unadjusted O/H/L/C; shares Vo; adjusted Adj* preserved separately"),
    Dataset("equities_valuation", "/equities/valuation", "free", "2008-07-08"),
    Dataset("financial_summary", "/fins/summary", "free", "2008-07-07"),
    Dataset("earnings_dates", "/fins/earnings-date", "free", "2014-09-01"),
    Dataset("earnings_calendar", "/equities/earnings-calendar", "free", "2008-01-01", "recent"),
    Dataset("trading_calendar", "/markets/calendar", "free", "2008-01-01", "range"),
    Dataset("investor_types", "/equities/investor-types", "light", "2008-01-16", "range"),
    Dataset("topix_daily", "/indices/bars/daily/topix", "light", "2008-05-07", "range"),
    Dataset("indices_daily", "/indices/bars/daily", "standard", "2008-05-07"),
    Dataset("options_225", "/derivatives/bars/daily/options/225", "standard", "2008-05-07"),
    Dataset("margin_interest", "/markets/margin-interest", "standard", "2012-02-10"),
    Dataset("short_ratio", "/markets/short-ratio", "standard", "2008-11-05"),
    Dataset("short_positions", "/markets/short-sale-report", "standard", "2013-11-07", "disc_date"),
    Dataset("margin_alert", "/markets/margin-alert", "standard", "2008-05-08"),
    Dataset("major_shareholders", "/edinet/major-shareholders", "standard", "2016-06-01"),
    Dataset("cross_shareholdings", "/edinet/cross-shareholdings", "standard", "2020-03-31"),
    Dataset("large_volume_shareholders", "/edinet/large-volume-shareholders", "standard", "2021-07-01"),
    Dataset("futures_daily", "/derivatives/bars/daily/futures", "premium", "2008-05-07"),
    Dataset("options_daily", "/derivatives/bars/daily/options", "premium", "2008-05-07"),
    Dataset("breakdown", "/markets/breakdown", "premium", "2015-04-01"),
    Dataset("morning_daily", "/equities/bars/daily/am", "premium", "2008-01-01", "recent"),
    Dataset("dividends", "/fins/dividend", "premium", "2013-02-20"),
    Dataset("financial_details", "/fins/details", "premium", "2009-01-13"),
    Dataset("equities_minute", "/equities/bars/minute", "free", "2008-01-01", "minute", "minute_tick"),
    Dataset("equities_ticks", "/equities/trades", "free", "2008-01-01", "bulk", "minute_tick"),
    Dataset("td_index", "/td/list", "free", "2008-01-01", "date", "tdnet"),
    Dataset("td_files", "/td/files", "free", "2008-01-01", "files", "tdnet"),
    Dataset("td_bulk", "/td/bulk", "free", "2008-01-01", "bulk", "tdnet"),
    Dataset("bulk_list", "/bulk/list", "free", "2008-01-01", "delivery", role="delivery"),
    Dataset("bulk_get", "/bulk/get", "free", "2008-01-01", "delivery", role="delivery"),
)
BY_NAME = {item.name: item for item in CATALOG}


def years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def entitlement(dataset: Dataset, plan: str, addons: tuple[str, ...] = ()) -> bool:
    if plan not in PLANS:
        raise ValueError("explicit verified plan must be free, light, standard, or premium")
    if dataset.addon:
        return dataset.addon in addons
    return PLANS[plan][0] >= PLANS[dataset.minimum_plan][0]


def period(dataset: Dataset, plan: str, as_of: date) -> tuple[date, date]:
    years = (2 if dataset.addon == "minute_tick" else 5) if dataset.addon else PLANS[plan][1]
    start = max(date.fromisoformat(dataset.earliest), years_before(as_of, years))
    end = as_of - timedelta(weeks=12) if plan == "free" and not dataset.addon else as_of
    if dataset.name == "trading_calendar" and plan != "free":
        end = date(as_of.year + 1, 12, 31)
    if dataset.query == "recent":
        start = end = as_of
    return start, end


def query_for(dataset: Dataset, day: date) -> dict[str, str]:
    if dataset.query == "recent":
        return {}
    if dataset.query == "range":
        return {"from": day.isoformat(), "to": day.isoformat()}
    if dataset.query == "disc_date":
        return {"disc_date": day.isoformat()}
    if dataset.query in {"date", "minute"}:
        return {"date": day.isoformat()}
    raise ValueError("dataset requires specialized delivery adapter")
