"""Extract disclosure-time financial inputs offline; never approve an assessment."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import io
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from stock_jp.market_data import observations, storage
from stock_jp.market_data.jquants import financial_history, snapshot
from stock_jp.research.allocation import DOMESTIC_MARKETS
from stock_jp.workspace import project_root

JST = ZoneInfo("Asia/Tokyo")
QUARTERS = {"1Q": 1, "2Q": 2, "3Q": 3, "FY": 4}
FLOW_FIELDS = ("sales", "operating_profit", "net_profit")
CHANGE_FIELDS = ("ChgByASRev", "ChgNoASRev", "ChgAcEst", "RetroRst")
CODE_INPUTS = (
    "src/stock_jp/research/financial_inputs.py", "src/stock_jp/market_data/observations.py",
    "src/stock_jp/market_data/jquants/snapshot.py", "src/stock_jp/market_data/jquants/financial_history.py",
    "src/stock_jp/market_data/storage.py", "src/stock_jp/market_data/jpx.py",
    "src/stock_jp/market_data/prices.py", "src/stock_jp/research/allocation.py",
)
FIELDS = (
    "evaluation_date", "cutoff_at_jst", "code", "company", "sector", "universe_basis",
    "price_status", "latest_source_id", "latest_disclosed_at_jst", "period_end",
    "accounting_scope", "accounting_standard", "reported_ytd_sales",
    "reported_ytd_operating_profit", "reported_ytd_net_profit", "reported_equity",
    "reported_cash_and_equivalents", "reported_operating_cash_flow",
    "reported_investing_cash_flow", "reported_issued_shares", "reported_treasury_shares",
    "reported_average_shares", "contiguous_summary_quarters", "ttm_sales",
    "ttm_operating_profit", "ttm_net_profit", "previous_ttm_sales", "ttm_sales_yoy_pct",
    "input_status", "review_flags", "lineage_source_ids",
)


def local_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    resolved = (root / path).resolve()
    if path.is_absolute() or ".." in path.parts or not resolved.is_relative_to(root):
        raise ValueError("research input path must stay inside the repository")
    return resolved


def number(value: str) -> Decimal | None:
    if value == "":
        return None
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("invalid reported financial number") from error
    if not result.is_finite():
        raise ValueError("reported financial number must be finite")
    return result


def numeric_text(value: Decimal | None) -> str:
    return "" if value is None else format(value, "f")


@dataclass(frozen=True)
class Report:
    row: dict[str, str]
    raw: dict[str, str]
    disclosed: datetime
    start: date
    end: date
    fy_start: date
    fy_end: date
    scope: str
    standard: str
    quarter: int | None

    @property
    def source_id(self):
        return self.row["source_id"]

    @property
    def key(self):
        return self.fy_start, self.fy_end, self.end, self.scope, self.standard

    @property
    def basis(self):
        return self.scope, self.standard

    @property
    def change_flags(self):
        scope_change = self.raw.get("SigChgInC") or self.raw.get("MatChgSub", "")
        flags = []
        if not self.fy_start <= self.start <= self.end <= self.fy_end or self.end > self.disclosed.date():
            flags.append("REPORTED_PERIOD_REQUIRES_REVIEW")
        if (not self.row["document_type"].startswith("OtherPeriod")
                and self.row["document_type"].split("FinancialStatements")[0] != self.row["current_period_type"]):
            flags.append("DOCUMENT_PERIOD_TYPE_REQUIRES_REVIEW")
        if scope_change == "true":
            flags.append("SCOPE_CHANGE_REQUIRES_BRIDGE")
        if any(self.raw.get(key) == "true" for key in CHANGE_FIELDS):
            flags.append("ACCOUNTING_CHANGE_REQUIRES_BRIDGE")
        return flags


def make_report(row, raw) -> Report | None:
    kind = row["document_type"]
    if "FinancialStatements_" not in kind:
        return None  # Forecast/dividend revisions are not actual financial statements.
    match = re.fullmatch(
        r"(1Q|2Q|3Q|FY|OtherPeriod)FinancialStatements_"
        r"(Consolidated|NonConsolidated)_(JP|IFRS|US|JMIS|Foreign|REIT)", kind
    )
    if not match:
        raise ValueError("unrecognized financial statement document type")
    disclosed = datetime.fromisoformat(row["disclosed_at_jst"])
    if disclosed.tzinfo is None:
        raise ValueError("financial disclosure timestamp must include a timezone")
    start, end, fy_start, fy_end = (
        date.fromisoformat(row[key]) for key in
        ("period_start", "period_end", "fiscal_year_start", "fiscal_year_end")
    )
    for key in (*FLOW_FIELDS, "equity", "cash_and_equivalents", "operating_cash_flow",
                "investing_cash_flow"):
        number(row[key])
    for key in (*CHANGE_FIELDS, "MatChgSub", "SigChgInC"):
        if raw.get(key, "") not in {"", "true", "false"}:
            raise ValueError("invalid reported accounting change flag")
    quarter = QUARTERS.get(row["current_period_type"])
    if match[1] != row["current_period_type"]:
        quarter = None
    return Report(row, raw, disclosed.astimezone(JST), start, end, fy_start, fy_end,
                  match[2], match[3], quarter if match[1] != "OtherPeriod" else None)


class FrozenStore:
    """Read hash-bound adopted objects directly, without a mutable restoration cache."""

    def __init__(self, root: Path, generation: str):
        self.root = root
        self.store = (root / "data/market/jquants").resolve()
        if not self.store.is_relative_to(root):
            raise ValueError("adopted store must stay inside the repository")
        self.pointer = {"version": 2, "generation": generation}
        self.files = json.loads(storage.manifest_bytes(self.store, self.pointer))["files"]
        self.used: dict[str, str] = {}

    def read(self, name):
        entry = self.files[name]
        raw = storage.member_bytes(self.store, self.pointer, entry)
        if len(raw) != entry["bytes"] or storage.digest(raw) != entry["sha256"]:
            raise ValueError("adopted input size or checksum mismatch")
        self.used[name] = entry["sha256"]
        return raw

def financial_sources(store: FrozenStore):
    """Yield the same pinned bulk/partition sources used by observation rebuilding."""
    for name in sorted(store.files):
        if re.fullmatch(r"jquants/financial-history/\d{4}-\d{2}-\d{2}\.json", name):
            state = json.loads(store.read(name))
            financial_history.validate_state(state, store.read)
            if state["status"] != "COMPLETE":
                raise ValueError("pinned financial history is incomplete")
            keys = {row["Key"] for row in state["inventories"]["financial_summary"]}
            for key in sorted(keys):
                item = state["files"][key]
                member = financial_history.PREFIX + item["path"]
                payload = store.read(member)
                if storage.digest(payload) != item["sha256"]:
                    raise ValueError("financial bulk source differs from its checkpoint")
                evidence = observations.evidence_path(payload, ".bin")
                yield (financial_history.csv_rows(io.BytesIO(payload), "financial_summary"),
                       item["retrieved_at"], evidence, state["from"], state["as_of"])
        elif re.fullmatch(r"jquants/datasets/financial_summary/[^/]+/latest\.json", name):
            manifest = json.loads(store.read(name))
            if manifest["status"] != "complete":
                raise ValueError("pinned financial partition is incomplete")
            count = 0
            for page in manifest["pages"]:
                payload = store.read("jquants/" + page["path"])
                content = json.loads(payload)
                if (storage.digest(payload) != page["sha256"] or len(payload) != page["bytes"]
                        or len(content["data"]) != page["row_count"]
                        or content.get("pagination_key") and page == manifest["pages"][-1]):
                    raise ValueError("financial page differs from its complete partition")
                count += len(content["data"])
                yield content["data"], page["retrieved_at"], observations.evidence_path(payload, ".json"), None, None
            if count != manifest["row_count"]:
                raise ValueError("financial partition row count mismatch")


def load_financial_observations(store: FrozenStore):
    actual, shares, original = {}, {}, {}
    for rows, retrieved, evidence, start, end in financial_sources(store):
        seen = set()
        for raw in rows:
            if start and not start <= str(raw["DiscDate"]) <= end:
                continue
            row, _, capital = observations.financial_records(raw, retrieved, evidence)
            source_id = row["source_id"]
            if source_id in seen:
                raise ValueError("duplicate disclosure identity in a raw source")
            seen.add(source_id)
            observations.merge_row(actual, row, "source_id")
            if actual[source_id] == row:
                original[source_id] = raw
            if capital:
                observations.merge_row(shares, capital, "source_id")
    if not actual:
        raise ValueError("pinned generation has no financial disclosure sources")
    return actual, shares, original


def load_reports(store: FrozenStore):
    actual, shares, original = load_financial_observations(store)
    reports = defaultdict(list)
    for source_id, row in actual.items():
        item = make_report(row, original[source_id])
        if item is not None:
            reports[row["code"]].append(item)
    for values in reports.values():
        values.sort(key=lambda item: (item.disclosed, item.source_id))
    return reports, len(actual), len(shares)


def financial_snapshot(reports: list[Report], cutoff: datetime) -> dict[str, str | int]:
    """Use only already disclosed versions; stop at gaps or unbridged changes."""
    if cutoff.tzinfo is None:
        raise ValueError("financial cutoff must include a timezone")
    eligible = [item for item in reports if item.disclosed <= cutoff]
    result: dict[str, str | int] = {key: "" for key in FIELDS[7:]}
    result["contiguous_summary_quarters"] = 0
    if not eligible:
        result.update(input_status="NO_FINANCIAL_STATEMENT", review_flags="NO_FINANCIAL_STATEMENT")
        return result
    versions = defaultdict(list)
    for item in eligible:
        versions[item.key].append(item)
    selected = {key: max(values, key=lambda item: item.disclosed) for key, values in versions.items()}
    # An old-period correction must not replace a newer accounting period.
    latest_end = max(item.end for item in selected.values())
    latest_candidates = [item for item in selected.values() if item.end == latest_end]
    latest_time = max(item.disclosed for item in latest_candidates)
    latest = [item for item in latest_candidates if item.disclosed == latest_time]
    if len(latest) != 1:
        result.update(input_status="AMBIGUOUS_DISCLOSURE", review_flags="AMBIGUOUS_DISCLOSURE",
                      lineage_source_ids="|".join(sorted(item.source_id for item in latest)))
        return result
    latest = latest[0]
    if sum(item.disclosed == latest.disclosed for item in versions[latest.key]) > 1:
        result.update(input_status="AMBIGUOUS_DISCLOSURE", review_flags="AMBIGUOUS_DISCLOSURE",
                      lineage_source_ids="|".join(sorted(item.source_id for item in versions[latest.key]
                                                        if item.disclosed == latest.disclosed)))
        return result
    result.update(latest_source_id=latest.source_id,
                  latest_disclosed_at_jst=latest.disclosed.isoformat(),
                  period_end=str(latest.end), accounting_scope=latest.scope,
                  accounting_standard=latest.standard)
    for output, field in (
        ("reported_ytd_sales", "sales"), ("reported_ytd_operating_profit", "operating_profit"),
        ("reported_ytd_net_profit", "net_profit"), ("reported_equity", "equity"),
        ("reported_cash_and_equivalents", "cash_and_equivalents"),
        ("reported_operating_cash_flow", "operating_cash_flow"),
        ("reported_investing_cash_flow", "investing_cash_flow"),
    ):
        result[output] = latest.row[field]
    for output, field in (("reported_issued_shares", "ShOutFY"),
                          ("reported_treasury_shares", "TrShFY"), ("reported_average_shares", "AvgSh")):
        value = number(str(latest.raw.get(field, "")))
        if value is not None and (value < 0 or value != value.to_integral_value()):
            raise ValueError("reported shares must be nonnegative integers")
        result[output] = numeric_text(value)
    flags = set()
    lineage = {latest.source_id}
    quarters = []
    current = latest

    def bridge_flags(item):
        findings = set(item.change_flags)
        if sum(other.disclosed == item.disclosed for other in versions[item.key]) > 1:
            findings.add("AMBIGUOUS_DISCLOSURE")
        if any(any(old.row[key] != item.row[key] for key in FLOW_FIELDS) for old in versions[item.key]):
            findings.add("CORRECTION_REQUIRES_BRIDGE")
        if findings:
            lineage.update(other.source_id for other in versions[item.key])
        return findings

    while len(quarters) < 8:
        lineage.add(current.source_id)
        if current.quarter is None or current.start != current.fy_start or not 350 <= (
                current.fy_end - current.fy_start).days + 1 <= 380:
            flags.add("IRREGULAR_REPORTING_PERIOD")
            flags.update(current.change_flags)
            break
        # Different amounts for a corrected cumulative period need a bridge
        # before they can be subtracted from other quarters' original values.
        current_flags = bridge_flags(current)
        if current_flags:
            flags.update(current_flags)
            break
        prior = None
        if current.quarter > 1:
            matches = [item for item in selected.values() if item.basis == current.basis
                       and item.fy_start == current.fy_start and item.fy_end == current.fy_end
                       and item.quarter == current.quarter - 1 and item.end < current.end]
            if len(matches) != 1:
                other_basis = any(item.end < current.end and item.fy_start == current.fy_start
                                  and item.basis != current.basis for item in selected.values())
                flags.add("BASIS_CHANGE_REQUIRES_BRIDGE" if other_basis else "MISSING_PREVIOUS_YTD")
                break
            prior = matches[0]
            lineage.add(prior.source_id)
            if prior.start != prior.fy_start:
                flags.add("IRREGULAR_REPORTING_PERIOD")
                break
            prior_flags = bridge_flags(prior)
            if prior_flags:
                flags.update(prior_flags)
                break
        quarter_start = prior.end + timedelta(days=1) if prior else current.fy_start
        if not 80 <= (current.end - quarter_start).days + 1 <= 100:
            flags.add("IRREGULAR_REPORTING_PERIOD")
            break
        values = {}
        for key in FLOW_FIELDS:
            value = number(current.row[key])
            previous = number(prior.row[key]) if prior else Decimal(0)
            values[key] = None if value is None or previous is None else value - previous
        if any(value is None for value in values.values()):
            flags.add("MISSING_FLOW_VALUE")
            break
        quarters.append(values)
        if prior is not None:
            current = prior
        else:
            matches = [item for item in selected.values()
                       if item.basis == current.basis and item.end == current.fy_start - timedelta(days=1)
                       and item.quarter == 4]
            if len(matches) != 1:
                other_basis = any(item.end == current.fy_start - timedelta(days=1)
                                  and item.basis != current.basis for item in selected.values())
                flags.add("BASIS_CHANGE_REQUIRES_BRIDGE" if other_basis else "MISSING_PREVIOUS_FISCAL_YEAR")
                break
            current = matches[0]
    count = len(quarters)
    result["contiguous_summary_quarters"] = count
    if count >= 4:
        for output, key in (("ttm_sales", "sales"), ("ttm_operating_profit", "operating_profit"),
                            ("ttm_net_profit", "net_profit")):
            result[output] = numeric_text(sum(item[key] for item in quarters[:4]))
    if count >= 8:
        previous = sum(item["sales"] for item in quarters[4:8])
        result["previous_ttm_sales"] = numeric_text(previous)
        if previous > 0:
            growth = (number(str(result["ttm_sales"])) / previous - 1) * 100
            result["ttm_sales_yoy_pct"] = numeric_text(growth.quantize(Decimal("0.000001")))
        else:
            flags.add("NONPOSITIVE_PREVIOUS_TTM_SALES")
    if not result["reported_cash_and_equivalents"]:
        flags.add("CASH_NOT_REPORTED_IN_LATEST_STATEMENT")
    if not result["reported_issued_shares"] or not result["reported_treasury_shares"]:
        flags.add("SHARES_NOT_REPORTED_IN_LATEST_STATEMENT")
    if number(str(result["reported_equity"])) is not None and number(str(result["reported_equity"])) < 0:
        flags.add("NEGATIVE_REPORTED_EQUITY")
    result.update(input_status="SUMMARY_INPUTS_EXTRACTED" if count == 8 else "SUMMARY_SERIES_NEEDS_REVIEW",
                  review_flags="|".join(sorted(flags)), lineage_source_ids="|".join(sorted(lineage)))
    return result


def monthly_prices(root: Path, register, store: FrozenStore, start: date, through: date):
    by_day = {}
    for entry in register["files"]:
        day = date.fromisoformat(entry["date"])
        if day in by_day:
            raise ValueError("duplicate registered price session")
        by_day[day] = entry
    # A registered later session determines the actual final session of a month;
    # never call a truncated selection (for example October 2) a month end.
    monthly = {}
    for day in sorted(by_day):
        monthly[day.strftime("%Y-%m")] = day
    last_registered_month = max(monthly)
    for day in sorted(value for value in monthly.values() if start <= value <= through):
        next_month = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
        if day.strftime("%Y-%m") == last_registered_month and day != next_month - timedelta(days=1):
            continue
        entry = by_day[day]
        native = f"daily-prices/{day.year}/{day}.csv"
        if native in store.files:
            # The older allocation register is a price diagnostic. Use the
            # adopted dated export when available rather than its legacy labels.
            reference = entry
            entry = dict(date=str(day), path=".work/market-data/" + native,
                         sha256=store.files[native]["sha256"],
                         provider="J-Quants V2 adopted dated export", registered_session_reference=reference)
            payload = store.read(native)
        elif entry["path"].startswith(".work/market-data/"):
            payload = store.read(entry["path"].removeprefix(".work/market-data/"))
        else:
            payload = local_path(root, entry["path"]).read_bytes()
        if storage.digest(payload) != entry["sha256"]:
            raise ValueError("monthly price checksum mismatch")
        rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8"))))
        if any(row["日付"] != str(day) for row in rows):
            raise ValueError("monthly price contains a different session date")
        if len({row["銘柄コード"] for row in rows}) != len(rows):
            raise ValueError("duplicate issue code in monthly prices")
        domestic = {row["銘柄コード"]: row for row in rows if row["市場・商品区分"] in DOMESTIC_MARKETS}
        name = f"jquants/datasets/equities_master/{day}/latest.json"
        basis = "PRICE_OBSERVED_UNIVERSE_NOT_CERTIFIED"
        if name in store.files:
            manifest = json.loads(store.read(name))
            if (manifest["status"] != "complete" or manifest["query"] != {"date": str(day)}
                    or manifest["period"] != {"from": str(day), "to": str(day)}):
                raise ValueError("dated master partition is incomplete or has a different date")
            masters = []
            for page in manifest["pages"]:
                raw = store.read("jquants/" + page["path"])
                content = json.loads(raw)
                if (storage.digest(raw) != page["sha256"] or len(raw) != page["bytes"]
                        or content.get("pagination_key") and page == manifest["pages"][-1]):
                    raise ValueError("dated master page is incomplete or differs from its manifest")
                if len(content["data"]) != page["row_count"]:
                    raise ValueError("dated master page row count mismatch")
                masters.extend(content["data"])
            if len(masters) != manifest["row_count"]:
                raise ValueError("dated master partition row count mismatch")
            issues, _, _ = snapshot.domestic_common_master(str(day), masters)
            basis = "DATED_MASTER_DOMESTIC_COMMON"
            if set(domestic) - set(issues):
                raise ValueError("monthly prices contain an issue outside the dated domestic master")
            domestic = {
                code: {"銘柄名": issue["CoName"], "33業種区分": issue["S33Nm"],
                       "取得状態": domestic.get(code, {}).get("取得状態", "PRICE_ROW_MISSING")}
                for code, issue in issues.items()
            }
        if not domestic:
            raise ValueError("monthly domestic input population is empty")
        yield day, basis, domestic, entry


def write_csv(path: Path, fields, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    payload = stream.getvalue().encode()
    if len(payload) > storage.MAX_SHARD:
        raise ValueError("research output exceeds the Git file limit")
    storage.scan(payload)
    storage.atomic(path, payload)


def build(root: Path, *, price_register: Path, start: date, through: date, output: Path):
    root = root.resolve()
    output = output.resolve()
    if not output.is_relative_to(root / "research"):
        raise ValueError("financial research outputs must stay under research/")
    if start > through or start < date(2024, 11, 5):
        raise ValueError("financial input dates require the 15:30 TSE close and an ordered period")
    register_path = local_path(root, price_register.relative_to(root).as_posix())
    register = json.loads(register_path.read_bytes())
    if register.get("no_unofficial_fallback") is not True:
        raise ValueError("financial input register must exclude unofficial prices")
    store = FrozenStore(root, register["adopted_generation"])
    reports, financial_count, share_count = load_reports(store)
    monthly = list(monthly_prices(root, register, store, start, through))
    expected_monthly = {str(day) + ".csv" for day, *_ in monthly}
    if (output / "monthly").exists() and {path.name for path in (output / "monthly").iterdir()} - expected_monthly:
        raise ValueError("unexpected monthly outputs; use a separate research directory")
    months, latest, output_hashes = [], {}, {}
    for day, basis, issues, price in monthly:
        cutoff = datetime.combine(day, time(15, 30), JST)
        rows = []
        for code, issue in sorted(issues.items()):
            row = dict(evaluation_date=str(day), cutoff_at_jst=cutoff.isoformat(), code=code,
                       company=issue["銘柄名"], sector=issue["33業種区分"], universe_basis=basis,
                       price_status=issue.get("取得状態", "UNKNOWN"))
            row.update(financial_snapshot(reports.get(code, []), cutoff))
            rows.append(row)
            latest[code] = row
        target = output / "monthly" / (str(day) + ".csv")
        write_csv(target, FIELDS, rows)
        output_hashes[target.relative_to(output).as_posix()] = storage.digest(target.read_bytes())
        months.append(dict(evaluation_date=str(day), universe_basis=basis, rows=len(rows),
                           input_status_counts=dict(sorted(Counter(row["input_status"] for row in rows).items())),
                           ttm_sales_available=sum(bool(row["ttm_sales"]) for row in rows),
                           eight_summary_quarters=sum(row["contiguous_summary_quarters"] == 8 for row in rows),
                           price_not_ok=sum(row["price_status"] != "OK" for row in rows),
                           negative_reported_equity=sum("NEGATIVE_REPORTED_EQUITY" in row["review_flags"] for row in rows),
                           price_input=price))
    if not months:
        raise ValueError("no registered monthly evaluation dates in the requested period")
    queue = []
    for row in latest.values():
        flags = row["review_flags"]
        if row["input_status"] in {"NO_FINANCIAL_STATEMENT", "AMBIGUOUS_DISCLOSURE"}:
            work = "FINANCIAL_DISCLOSURE_IDENTITY_AND_MISSING_REPORTS"
        elif "BRIDGE" in flags or "IRREGULAR" in flags:
            work = "ACCOUNTING_SCOPE_POLICY_CORRECTION_BRIDGE"
        elif "NEGATIVE_REPORTED_EQUITY" in flags:
            work = "EQUITY_AND_GOING_CONCERN_PRIMARY_REVIEW"
        elif row["contiguous_summary_quarters"] < 8:
            work = "EIGHT_QUARTER_SUMMARY_RECONSTRUCTION"
        else:
            work = "PRIMARY_HARD_GATES_GROSS_PROFIT_DILUTION_SAM_AND_SCORE"
        queue.append({key: row[key] for key in ("code", "company", "evaluation_date", "latest_source_id",
                     "period_end", "input_status", "review_flags")} | {"next_input_group": work})
    queue.sort(key=lambda row: row["code"])
    queue_path = output / "review-queue.csv"
    write_csv(queue_path, [*queue[0]], queue)
    output_hashes[queue_path.name] = storage.digest(queue_path.read_bytes())
    anomaly_fields = ("code", "source_id", "disclosed_at_jst", "document_type", "current_period_type",
                      "period_start", "period_end", "fiscal_year_start", "fiscal_year_end", "evidence_path")
    anomalies = []
    for values in reports.values():
        for item in values:
            findings = set(item.change_flags) & {
                "REPORTED_PERIOD_REQUIRES_REVIEW", "DOCUMENT_PERIOD_TYPE_REQUIRES_REVIEW"
            }
            if findings:
                anomalies.append({key: item.row[key] for key in anomaly_fields} |
                                 {"review_flags": "|".join(sorted(findings))})
    anomalies.sort(key=lambda row: (row["disclosed_at_jst"], row["code"], row["source_id"]))
    anomaly_path = output / "source-anomalies.csv"
    write_csv(anomaly_path, (*anomaly_fields, "review_flags"), anomalies)
    output_hashes[anomaly_path.name] = storage.digest(anomaly_path.read_bytes())
    summary = dict(schema_version="1.0", status="FINANCIAL_INPUTS_ONLY_FORMAL_REPLAY_BLOCKED",
        period={"from": str(start), "through": str(through)}, cutoff="TSE_CLOSE_15_30_JST",
        currency="JPY", source_amounts="XBRL_AS_REPORTED_NO_ADDITIONAL_ROUNDING",
        financial_observations_verified_against_raw=financial_count,
        share_observations_verified_against_raw=share_count,
        adopted_generation=register["adopted_generation"], monthly=months,
        issue_month_rows=sum(row["rows"] for row in months), unique_codes=len(latest),
        dated_master_months=sum(row["universe_basis"] == "DATED_MASTER_DOMESTIC_COMMON" for row in months),
        ttm_sales_available=sum(row["ttm_sales_available"] for row in months),
        eight_summary_quarters=sum(row["eight_summary_quarters"] for row in months),
        reported_metadata_anomalies=len(anomalies),
        review_queue_groups=dict(sorted(Counter(row["next_input_group"] for row in queue).items())),
        historical_original_values_certified=False, permanent_issue_identity_verified=False,
        full_rule_assessments_generated=0, full_strategy_backtest=False, live_gate_satisfied=False,
        network_requests=0, jquants_requests=0,
        common_outstanding_inputs=["PROVIDER_DATA_CORRECTION_HISTORY_AND_ORIGINAL_VINTAGE",
            "PERMANENT_ISSUE_ID_LISTING_DELISTING_AND_CORPORATE_ACTIONS",
            "FULLY_DILUTED_SHARES_CASH_RESTRICTIONS_DEBT_AUDIT_GOING_CONCERN_AND_RUNWAY",
            "GROSS_PROFIT_KPI_NORMALIZATION_SAM_SOM_COMPETITORS_AND_100_POINT_SCORE",
            "MATERIAL_DISCLOSURES_EXIT_REVIEWS_POINT_IN_TIME_MRS_AND_FORWARD_HOLDOUT"],
        inputs={"price_register": {"path": register_path.relative_to(root).as_posix(),
                                   "sha256": storage.digest(register_path.read_bytes())},
                "adopted_members": dict(sorted(store.used.items()))},
        code={name: storage.digest((root / name).read_bytes()) for name in CODE_INPUTS},
        outputs=output_hashes)
    storage.atomic(output / "summary.json", (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode())
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--price-register", type=Path, required=True)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--through", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = project_root().resolve()
    try:
        summary = build(root, price_register=(root / args.price_register).resolve(), start=args.start,
                        through=args.through, output=root / args.output)
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({"status": "BLOCKED", "reason": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({key: summary[key] for key in ("status", "issue_month_rows", "dated_master_months",
                     "ttm_sales_available", "eight_summary_quarters", "full_rule_assessments_generated")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
