"""Rebuild source-backed financial, capital and calendar observations offline."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
import io
import json
from pathlib import Path

from stock_jp.market_data import jpx, storage
from stock_jp.market_data.jquants import ingest, financial_history
from stock_jp.workspace import project_root

FINANCIAL_MAP = {
    "current_period_type": "CurPerType", "period_start": "CurPerSt", "period_end": "CurPerEn",
    "fiscal_year_start": "CurFYSt", "fiscal_year_end": "CurFYEn", "sales": "Sales",
    "operating_profit": "OP", "ordinary_profit": "OdP", "net_profit": "NP", "eps": "EPS",
    "diluted_eps": "DEPS", "total_assets": "TA", "equity": "Eq", "operating_cash_flow": "CFO",
    "investing_cash_flow": "CFI", "financing_cash_flow": "CFF", "cash_and_equivalents": "CashEq",
}
FINANCIAL_FIELDS = ("source_id", "disclosed_at_jst", "code", "disclosure_number", "document_type",
                    *FINANCIAL_MAP, "retrieved_at_jst", "evidence_path")
FORECAST_MAP = {
    "forecast_sales": "FSales", "forecast_operating_profit": "FOP", "forecast_ordinary_profit": "FOdP",
    "forecast_net_profit": "FNP", "forecast_eps": "FEPS", "next_forecast_sales": "NxFSales",
    "next_forecast_operating_profit": "NxFOP", "next_forecast_ordinary_profit": "NxFOdP",
    "next_forecast_net_profit": "NxFNp", "next_forecast_eps": "NxFEPS", "material_change_submitted": "MatChgSub",
}
CORPORATE_FIELDS = ("action_id", "announced_at_jst", "effective_date", "code", "action_type",
                    "ratio_numerator", "ratio_denominator", "old_code", "new_code", "normalization_factor",
                    "source_url", "applied_at_jst", "applied_by", "evidence_path", "notes")
TABLES = {"financial-history.csv": (FINANCIAL_FIELDS, "source_id"),
          "forecast-history.csv": (jpx.FORECAST_FIELDS, "source_id"),
          "share-count-history.csv": (jpx.SHARE_FIELDS, "source_id"),
          "earnings-calendar-history.csv": (jpx.EARNINGS_FIELDS, "source_id"),
          "corporate-actions.csv": (CORPORATE_FIELDS, "action_id")}


def financial_records(row, retrieved, evidence):
    code = jpx._normalize_code(row.get("Code"))
    if not code:
        raise ValueError("financial record has no issue code")
    disclosed = jpx._disclosed_at(row)
    number = jpx._text(row.get("DiscNo"))
    source_id = "jq-fin-" + (number or jpx._stable_id(disclosed, code, json.dumps(row, sort_keys=True)))
    common = {"source_id": source_id, "disclosed_at_jst": disclosed, "code": code,
              "disclosure_number": number, "document_type": jpx._text(row.get("DocType")),
              "retrieved_at_jst": retrieved, "evidence_path": evidence}
    actual = {**common, **{key: jpx._text(row.get(field)) for key, field in FINANCIAL_MAP.items()}}
    forecast = {**common, "current_period_type": jpx._text(row.get("CurPerType")),
                "current_fiscal_year_end": jpx._date_text(row.get("CurFYEn")),
                **{key: jpx._text(row.get(field)) for key, field in FORECAST_MAP.items()}}
    shares = {key: common[key] for key in ("source_id", "disclosed_at_jst", "code", "retrieved_at_jst", "evidence_path")}
    shares.update(fiscal_year_end=jpx._date_text(row.get("CurFYEn")),
                  issued_shares_fy=jpx._text(row.get("ShOutFY")), treasury_shares_fy=jpx._text(row.get("TrShFY")),
                  average_shares=jpx._text(row.get("AvgSh")),
                  notes="Reported fiscal-period values; current issued shares and dilution require disclosure review.")
    return actual, forecast, shares if any(shares[k] for k in ("issued_shares_fy", "treasury_shares_fy", "average_shares")) else None


def earnings_record(row, retrieved, evidence):
    code = jpx._normalize_code(row.get("Code"))
    published = jpx._date_text(row.get("PubDate"))
    scheduled = jpx._date_text(row.get("SchDate"))
    if not code or not published:
        raise ValueError("earnings-calendar record lacks its publication date or issue code")
    return {"source_id": "jq-earnings-" + jpx._stable_id(published, code, jpx._text(row.get("FQName")), scheduled),
            "published_date": published, "scheduled_date": scheduled,
            "fiscal_quarter": jpx._text(row.get("FQName")), "fiscal_year_end_mmdd": jpx._text(row.get("FYE")),
            "code": code, "company_name": jpx._text(row.get("CoName")), "company_name_en": jpx._text(row.get("CoNameEn")),
            "retrieved_at_jst": retrieved, "evidence_path": evidence}


def evidence_path(raw, suffix):
    sha = storage.digest(raw)
    return f"data/market/jquants/objects/{sha[:2]}/{sha}{suffix}"


def partition_pages(archive, dataset):
    for path in sorted((archive / "datasets" / dataset).glob("*/revisions/*.json")):
        manifest = ingest.read_json(path)
        if manifest.get("status") != "complete":
            raise ValueError("observation partition is incomplete")
        if ingest.digest(path.read_bytes()) != path.stem:
            raise ValueError("observation manifest checksum mismatch")
        for page in manifest["pages"]:
            raw = (archive / page["path"]).read_bytes()
            if ingest.digest(raw) != page["sha256"] or len(raw) != page["bytes"]:
                raise ValueError("observation page checksum mismatch")
            yield json.loads(raw)["data"], page["retrieved_at"], evidence_path(raw, ".json")


def rebuild(root, *, source=None):
    root = Path(root)
    source = Path(source) if source else root / ".work/market-data"
    archive = source / "jquants"
    storage.safe_tree(archive)
    output = {name: {} for name in TABLES}
    directory = root / "data/market/observations"

    def add(name, row):
        key = row[TABLES[name][1]]
        prior = output[name].get(key)
        if prior is None or row.get("retrieved_at_jst", "") >= prior.get("retrieved_at_jst", ""):
            output[name][key] = row

    # Retain manually confirmed evidence and events outside the restored source.
    for name, (fields, key) in TABLES.items():
        path = directory / name
        if path.exists():
            with path.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                if tuple(reader.fieldnames or ()) != tuple(fields):
                    raise ValueError("observation CSV schema differs from the canonical fields")
                for row in reader:
                    if not row.get(key) or row[key] in output[name]:
                        raise ValueError("duplicate or missing observation identity")
                    output[name][row[key]] = row

    def financial(rows, retrieved, evidence, start=None, end=None):
        for row in rows:
            day = jpx._date_text(row.get("DiscDate"))
            if start and not start <= day <= end:
                continue
            actual, forecast, shares = financial_records(row, retrieved, evidence)
            add("financial-history.csv", actual)
            add("forecast-history.csv", forecast)
            if shares:
                add("share-count-history.csv", shares)

    def earnings(rows, retrieved, evidence, start=None, end=None):
        for row in rows:
            day = jpx._date_text(row.get("PubDate"))
            if start and not start <= day <= end:
                continue
            add("earnings-calendar-history.csv", earnings_record(row, retrieved, evidence))

    for path in sorted((source / financial_history.PREFIX).glob("????-??-??.json")):
        state = ingest.read_json(path)
        financial_history.validate_state(state, lambda name: (source / name).read_bytes())
        if state["status"] != "COMPLETE":
            raise ValueError("financial-history collection is unfinished")
        by_key = {row["Key"]: name for name, items in state["inventories"].items() for row in items}
        for key, item in state["files"].items():
            name = by_key[key]
            blob = source / financial_history.PREFIX / item["path"]
            evidence = evidence_path(blob.read_bytes(), ".bin")
            rows = financial_history.csv_rows(blob, name)
            (financial if name == "financial_summary" else earnings)(rows, item["retrieved_at"], evidence, state["from"], state["as_of"])
    for rows, retrieved, evidence in partition_pages(archive, "financial_summary"):
        financial(rows, retrieved, evidence)
    for rows, retrieved, evidence in partition_pages(archive, "earnings_dates"):
        earnings(rows, retrieved, evidence)
    for rows, retrieved, evidence in partition_pages(archive, "equities_daily"):
        for row in rows:
            text = jpx._text(row.get("AdjFactor"))
            if not text:
                continue
            try:
                factor = Decimal(text)
            except InvalidOperation:
                raise ValueError("invalid official adjustment factor") from None
            if not factor.is_finite() or factor <= 0:
                raise ValueError("invalid official adjustment factor")
            if factor == 1:
                continue
            code, day = jpx._normalize_code(row.get("Code")), jpx._date_text(row.get("Date"))
            action_id = f"jq-adjustment-{code}-{day}-{jpx._stable_id(text)}"
            previous = output["corporate-actions.csv"].get(action_id, {})
            action = {field: previous.get(field, "") for field in CORPORATE_FIELDS}
            action.update(action_id=action_id, effective_date=day, code=code,
                          action_type="JQUANTS_ADJUSTMENT_FACTOR", normalization_factor=text,
                          source_url=ingest.BASE_URL + "/equities/bars/daily", evidence_path=evidence)
            if not action["notes"]:
                action["notes"] = "Official adjustment signal; company IR must confirm event type, ratio and effective date before application."
            add("corporate-actions.csv", action)
    counts = {}
    for name, (fields, key) in TABLES.items():
        rows = sorted(output[name].values(), key=lambda row: (row.get("disclosed_at_jst") or row.get("published_date") or row.get("effective_date", ""), row["code"], row[key]))
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        raw = stream.getvalue().encode()
        storage.scan(raw)
        storage.atomic(directory / name, raw)
        counts[name] = len(rows)
    return {"status": "GENERATED", "rows": counts, "analysis_readiness": "NOT_EVALUATED",
            "corporate_action_confirmation": "REQUIRED", "financial_history_knowledge": "RETRIEVED_NOW_NOT_POINT_IN_TIME"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = rebuild(project_root())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
