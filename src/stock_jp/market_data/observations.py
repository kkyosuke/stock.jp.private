"""Rebuild source-backed financial, capital and calendar observations offline."""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import io
import json
from pathlib import Path
import re

from stock_jp.market_data import jpx, storage
from stock_jp.market_data.jquants import ingest, financial_history
from stock_jp.workspace import project_root, record_path

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


def publication_path(path):
    return bool(re.fullmatch(r"data/market/observations/(?:" + "|".join(
        re.escape(Path(name).stem) for name in TABLES
    ) + r")/(?:[0-9]{4}|latest)\.csv", path))


def publication_files(root):
    directory = Path(root) / "data/market/observations"
    storage.safe_tree(directory)
    for path in sorted(directory.rglob("*.csv")):
        if not publication_path(path.relative_to(root).as_posix()):
            raise ValueError("unexpected observation table path")
        yield path


def row_date(row):
    value = row.get("disclosed_at_jst") or row.get("published_date") or row.get("effective_date", "")
    date.fromisoformat(value[:10])
    return value


def row_order(row, identity):
    return row_date(row), row["code"], row[identity]


def merge_row(rows, row, identity):
    key = row.get(identity)
    if not key or not row.get("code"):
        raise ValueError("missing observation identity or issue code")
    row_date(row)
    previous = rows.get(key)
    if previous and row.get("retrieved_at_jst") and previous.get("retrieved_at_jst"):
        if datetime.fromisoformat(retrieved_jst(row["retrieved_at_jst"])) < datetime.fromisoformat(
                retrieved_jst(previous["retrieved_at_jst"])):
            return
    rows[key] = row


def load_history(root, name):
    fields, identity = TABLES[name]
    directory = record_path(root, name).parent
    storage.safe_tree(directory)
    rows = {}
    years = sorted(directory.glob("[0-9][0-9][0-9][0-9].csv"))
    paths = years or ([directory / "latest.csv"] if (directory / "latest.csv").is_file() else [])
    for path in paths:
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != tuple(fields):
                raise ValueError("observation CSV schema differs from the canonical fields")
            for row in reader:
                if None in row or None in row.values() or row.get(identity) in rows:
                    raise ValueError("duplicate or malformed observation record")
                if path.stem != "latest" and row_date(row)[:4] != path.stem:
                    raise ValueError("observation stored in the wrong year")
                merge_row(rows, row, identity)
    return rows


def save_rows(root, name, new_rows):
    fields, identity = TABLES[name]
    rows = load_history(root, name)
    for row in new_rows:
        merge_row(rows, row, identity)
    ordered = sorted(rows.values(), key=lambda row: row_order(row, identity))
    partitions, latest = {}, {}
    for row in ordered:
        partitions.setdefault(row_date(row)[:4], []).append(row)
        latest[row["code"]] = row
    directory = record_path(root, name).parent
    views = {year + ".csv": members for year, members in partitions.items()}
    # Replay needs every adjustment event, rather than the newest event per issue.
    views["latest.csv"] = ordered if name == "corporate-actions.csv" else sorted(
        latest.values(), key=lambda row: row_order(row, identity))
    for filename, members in views.items():
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(members)
        raw = stream.getvalue().encode()
        if len(raw) > storage.MAX_SHARD:
            raise ValueError("observation year exceeds the Git shard limit")
        storage.scan(raw)
        storage.atomic(directory / filename, raw)
    return {"history_rows": len(ordered), "latest_rows": len(views["latest.csv"]),
            "years": sorted(partitions)}


def retrieved_jst(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("observation retrieval timestamp must include a timezone")
    return stamp.astimezone(jpx.JST).isoformat(timespec="seconds")


def financial_records(row, retrieved, evidence):
    code = jpx._normalize_code(row.get("Code"))
    if not code:
        raise ValueError("financial record has no issue code")
    if not jpx._text(row.get("DiscTime")):
        raise ValueError("financial disclosure time is missing")
    disclosed = jpx._disclosed_at(row)
    retrieved = retrieved_jst(retrieved)
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
    retrieved = retrieved_jst(retrieved)
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


def adjustment_record(row, evidence, history):
    text = jpx._text(row.get("AdjFactor"))
    if not text:
        return None
    try:
        factor = Decimal(text)
    except InvalidOperation:
        raise ValueError("invalid official adjustment factor") from None
    if not factor.is_finite() or factor <= 0:
        raise ValueError("invalid official adjustment factor")
    if factor == 1:
        return None
    code, day = jpx._normalize_code(row.get("Code")), jpx._date_text(row.get("Date"))
    if not code or not day:
        raise ValueError("adjustment signal lacks its issue code or effective date")
    action_id = f"jq-adjustment-{code}-{day}-{jpx._stable_id(text)}"
    previous = history.get(action_id, {})
    action = {field: previous.get(field, "") for field in CORPORATE_FIELDS}
    action.update(action_id=action_id, effective_date=day, code=code,
                  normalization_factor=text, source_url=ingest.BASE_URL + "/equities/bars/daily",
                  evidence_path=evidence)
    if not action["action_type"]:
        action["action_type"] = "JQUANTS_ADJUSTMENT_FACTOR"
    if not action["notes"]:
        action["notes"] = "Official adjustment signal; company IR must confirm event type, ratio and effective date before application."
    return action


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

    def add(name, row):
        merge_row(output[name], row, TABLES[name][1])

    # Retain manually confirmed evidence and events outside the restored source.
    for name in TABLES:
        output[name] = load_history(root, name)

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
            action = adjustment_record(row, evidence, output["corporate-actions.csv"])
            if action:
                add("corporate-actions.csv", action)
    counts = {}
    views = {}
    for name in TABLES:
        views[name] = save_rows(root, name, output[name].values())
        counts[name] = views[name]["history_rows"]
    return {"status": "GENERATED", "rows": counts, "analysis_readiness": "NOT_EVALUATED",
            "views": views,
            "corporate_action_confirmation": "REQUIRED", "financial_history_knowledge": "RETRIEVED_NOW_NOT_POINT_IN_TIME"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = rebuild(project_root())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
