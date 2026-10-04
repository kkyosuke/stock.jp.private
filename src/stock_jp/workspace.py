"""Canonical locations for this single repository.

All CLI commands use the same repository root. Record names identify a document,
not a second filesystem layout; callers never construct their own storage roots.
"""
from __future__ import annotations

import os
from pathlib import Path

RECORD_PATHS = {
    "sources.json": "config/sources.json",
    "policy.json": "config/policy.json",
    "state.json": "operations/state.json",
    "source-watermarks.json": "operations/source-watermarks.json",
    "run-history.csv": "operations/run-history.csv",
    "watchlist.csv": "operations/inputs/watchlist.csv",
    "portfolio-register.csv": "operations/ledgers/portfolio-register.csv",
    "trade-event-ledger.csv": "operations/ledgers/trade-event-ledger.csv",
    "capital-ledger.csv": "operations/ledgers/capital-ledger.csv",
    "recovered-capital-ledger.csv": "operations/ledgers/recovered-capital-ledger.csv",
    "rebuy-restrictions.csv": "operations/ledgers/rebuy-restrictions.csv",
    "industry-exposure.csv": "operations/ledgers/industry-exposure.csv",
    "rule-review-log.csv": "operations/ledgers/rule-review-log.csv",
    "market-regime-log.csv": "operations/ledgers/market-regime-log.csv",
    "schema-migration-log.csv": "operations/ledgers/schema-migration-log.csv",
    "corporate-actions.csv": "data/market/observations/corporate-actions/latest.csv",
    "forecast-history.csv": "data/market/observations/forecast-history/latest.csv",
    "financial-history.csv": "data/market/observations/financial-history/latest.csv",
    "share-count-history.csv": "data/market/observations/share-count-history/latest.csv",
    "earnings-calendar-history.csv": "data/market/observations/earnings-calendar-history/latest.csv",
    "market-data": ".work/market-data",
    "market-data-staging": ".work/staging",
}
MARKET_STORE = "data/market/jquants"
MARKET_WORK = ".work/market-data"
AUTOMATION_SCOPE = "config/automation.json"
AUTOMATION_RETIREMENT = "operations/evidence/automation-retirement.json"
ACQUISITION_SCOPE = "config/acquisition.json"
ACQUISITION_LEDGER = "operations/acquisition/date-grants.json"
DATE_STORE = "data/market/manual-dates/"
ACQUISITION_WORKFLOW = ".github/workflows/market-data-manual.yml"
CODE_TREE = "src/stock_jp"
LAYOUT_REVISION = 5


def project_root() -> Path:
    configured = os.environ.get("STOCK_JP_ROOT")
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def record_path(root: Path, name: str | Path) -> Path:
    name = str(name)
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("record must have a workspace-relative name")
    if name in RECORD_PATHS:
        return root / RECORD_PATHS[name]
    for identity in ("market-data", "market-data-staging", "historical-replay"):
        if relative.parts and relative.parts[0] == identity:
            base = RECORD_PATHS.get(identity, "research/historical-replay")
            return root / base / Path(*relative.parts[1:])
    if relative.parts and relative.parts[0] in {".work", "research", "data", "config", "templates", "operations"}:
        return root / relative
    return root / "operations" / relative


def module_file(root: Path, module: str) -> Path:
    if not module.startswith("stock_jp.") or any(not part.isidentifier() for part in module.split(".")):
        raise ValueError("expected a stock_jp module")
    return root / "src" / (module.replace(".", "/") + ".py")
