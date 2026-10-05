"""Disclosure cutoffs, cumulative amounts and missing evidence in offline research."""
from datetime import date, datetime, timedelta
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from stock_jp.market_data import observations, storage
from stock_jp.research import financial_inputs as f
from stock_jp.workspace import project_root


def raw_statement(year, quarter, *, code="40520", sales=None, disclosed=None, number=None, **changes):
    end = date(year, quarter * 3 + 1, 1) - timedelta(days=1) if quarter < 4 else date(year, 12, 31)
    released = disclosed or datetime.combine(end + timedelta(days=30), datetime.min.time()).replace(hour=15)
    kind = (f"{quarter}Q" if quarter < 4 else "FY") + "FinancialStatements_Consolidated_JP"
    amount = sales if sales is not None else sum(10 * (year - 2021 + q) for q in range(quarter))
    raw = dict(DiscDate=str(released.date()), DiscTime=released.strftime("%H:%M:%S"), Code=code,
               DiscNo=number or str(year * 10 + quarter), DocType=kind,
               CurPerType=f"{quarter}Q" if quarter < 4 else "FY", CurPerSt=f"{year}-01-01",
               CurPerEn=str(end), CurFYSt=f"{year}-01-01", CurFYEn=f"{year}-12-31",
               Sales=str(amount), OP=str(amount // 5), NP=str(amount // 10), Eq="500", CashEq="1000",
               CFO="50", CFI="-30", ShOutFY="100", TrShFY="0", AvgSh="100",
               MatChgSub="false", SigChgInC="false", ChgByASRev="false", ChgNoASRev="false",
               ChgAcEst="false", RetroRst="false")
    raw.update(changes)
    return raw


def report(raw):
    row, _, _ = observations.financial_records(raw, "2026-10-05T09:00:00+09:00", "fixture.json")
    return f.make_report(row, raw)


def series():
    return [report(raw_statement(year, q)) for year in (2022, 2023, 2024) for q in (1, 2, 3, 4)]


def cutoff(value="2025-01-31T15:30:00+09:00"):
    return datetime.fromisoformat(value)


class FinancialSnapshotTests(unittest.TestCase):
    def test_cumulative_statements_are_differenced_before_ttm_and_year_on_year(self):
        values = f.financial_snapshot(series(), cutoff())
        self.assertEqual(values["contiguous_summary_quarters"], 8)
        self.assertEqual(values["ttm_sales"], "180")
        self.assertEqual(values["previous_ttm_sales"], "140")
        self.assertEqual(values["ttm_sales_yoy_pct"], "28.571429")
        self.assertEqual(values["reported_ytd_sales"], "180")
        self.assertEqual(values["ttm_operating_profit"], "36")
        self.assertEqual(values["input_status"], "SUMMARY_INPUTS_EXTRACTED")
        self.assertNotIn("score", values)
        self.assertNotIn("hard_gates_passed", values)

    def test_disclosure_at_close_is_used_and_after_close_is_not(self):
        close = report(raw_statement(2024, 4, disclosed=datetime(2025, 1, 31, 15, 30)))
        late = report(raw_statement(2024, 4, sales=999, number="1",
                                    disclosed=datetime(2025, 1, 31, 16, 0)))
        values = f.financial_snapshot([*series()[:-1], close, late], cutoff())
        self.assertEqual(values["latest_source_id"], close.source_id)
        self.assertEqual(values["reported_ytd_sales"], "180")
        self.assertNotIn(late.source_id, values["lineage_source_ids"].split("|"))

    def test_later_correction_has_no_effect_before_it_is_disclosed(self):
        later = report(raw_statement(2024, 4, sales=200, number="1",
                                     disclosed=datetime(2025, 2, 4, 10)))
        self.assertEqual(f.financial_snapshot(series(), cutoff()),
                         f.financial_snapshot([*series(), later], cutoff()))
        after = f.financial_snapshot([*series(), later], cutoff("2025-02-04T15:30:00+09:00"))
        self.assertEqual(after["latest_source_id"], "jq-fin-1")
        self.assertEqual(after["reported_ytd_sales"], "200")
        self.assertEqual(after["ttm_sales"], "")
        self.assertIn("CORRECTION_REQUIRES_BRIDGE", after["review_flags"])

    def test_correction_to_older_period_does_not_replace_latest_period(self):
        older = report(raw_statement(2023, 3, sales=91, number="99999999",
                                     disclosed=datetime(2025, 1, 31, 9)))
        values = f.financial_snapshot([*series(), older], cutoff())
        self.assertEqual(values["period_end"], "2024-12-31")
        self.assertEqual(values["reported_ytd_sales"], "180")
        # The newest TTM is intact; the prior-year comparison crosses a correction.
        self.assertEqual(values["ttm_sales"], "180")
        self.assertEqual(values["previous_ttm_sales"], "")
        self.assertIn("CORRECTION_REQUIRES_BRIDGE", values["review_flags"])

    def test_missing_cumulative_predecessor_never_becomes_zero(self):
        reports = [item for item in series() if item.row["source_id"] != "jq-fin-20243"]
        values = f.financial_snapshot(reports, cutoff())
        self.assertEqual(values["contiguous_summary_quarters"], 0)
        self.assertEqual(values["ttm_sales"], "")
        self.assertIn("MISSING_PREVIOUS_YTD", values["review_flags"])

    def test_basis_transition_never_joins_consolidated_and_nonconsolidated_amounts(self):
        single = report(raw_statement(2024, 4, DocType="FYFinancialStatements_NonConsolidated_JP"))
        values = f.financial_snapshot([*series()[:-1], single], cutoff())
        self.assertEqual(values["accounting_scope"], "NonConsolidated")
        self.assertEqual(values["reported_ytd_sales"], "180")
        self.assertEqual(values["ttm_sales"], "")
        self.assertIn("BASIS_CHANGE_REQUIRES_BRIDGE", values["review_flags"])

    def test_accounting_changes_stop_comparison_without_a_primary_bridge(self):
        for change in (dict(SigChgInC="true"), dict(ChgNoASRev="true"), dict(RetroRst="true")):
            with self.subTest(change=change):
                changed = report(raw_statement(2024, 4, **change))
                values = f.financial_snapshot([*series()[:-1], changed], cutoff())
                self.assertEqual(values["ttm_sales"], "")
                self.assertIn("REQUIRES_BRIDGE", values["review_flags"])

    def test_ambiguous_disclosures_do_not_use_disclosure_number_as_tie_breaker(self):
        same_time = report(raw_statement(2024, 4, sales=999, number="99999999"))
        values = f.financial_snapshot([*series(), same_time], cutoff())
        self.assertEqual(values["input_status"], "AMBIGUOUS_DISCLOSURE")
        self.assertEqual(values["reported_ytd_sales"], "")

    def test_absent_cash_stays_missing_instead_of_using_an_old_balance(self):
        latest = report(raw_statement(2024, 4, CashEq="", CFO="", CFI=""))
        values = f.financial_snapshot([*series()[:-1], latest], cutoff())
        self.assertEqual(values["reported_cash_and_equivalents"], "")
        self.assertEqual(values["reported_operating_cash_flow"], "")
        self.assertIn("CASH_NOT_REPORTED_IN_LATEST_STATEMENT", values["review_flags"])
        self.assertNotIn("fcf", values)
        self.assertNotIn("runway_months", values)

    def test_nonpositive_prior_sales_do_not_produce_growth_or_a_passing_score(self):
        reports = [report(raw_statement(year, q, sales=0 if year == 2023 else 10 * q))
                   for year in (2022, 2023, 2024) for q in (1, 2, 3, 4)]
        values = f.financial_snapshot(reports, cutoff())
        self.assertEqual(values["ttm_sales"], "40")
        self.assertEqual(values["previous_ttm_sales"], "0")
        self.assertEqual(values["ttm_sales_yoy_pct"], "")
        self.assertIn("NONPOSITIVE_PREVIOUS_TTM_SALES", values["review_flags"])

    def test_forecast_revision_does_not_count_as_a_financial_quarter(self):
        raw = raw_statement(2024, 4, DocType="EarnForecastRevision", Sales="")
        self.assertIsNone(report(raw))
        values = f.financial_snapshot([], cutoff())
        self.assertEqual(values["input_status"], "NO_FINANCIAL_STATEMENT")

    def test_disagreeing_period_type_is_retained_for_review_without_a_quarter_value(self):
        changed = report(raw_statement(2024, 4, CurPerType="3Q"))
        values = f.financial_snapshot([*series()[:-1], changed], cutoff())
        self.assertEqual(values["reported_ytd_sales"], "180")
        self.assertEqual(values["ttm_sales"], "")
        self.assertIn("DOCUMENT_PERIOD_TYPE_REQUIRES_REVIEW", values["review_flags"])

    def test_invalid_reported_values_or_naive_cutoffs_are_rejected(self):
        for changes in (dict(Sales="NaN"), dict(OP="Infinity"), dict(RetroRst="yes"),
                        dict(CurPerEn="not-a-date")):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                report(raw_statement(2024, 4, **changes))
        with self.assertRaises(ValueError):
            f.financial_snapshot(series(), datetime(2025, 1, 31, 15, 30))

    def test_reported_period_after_disclosure_is_retained_as_an_unusable_input(self):
        bad = report(raw_statement(2024, 4, CurPerEn="2025-02-01"))
        values = f.financial_snapshot([*series()[:-1], bad], cutoff())
        self.assertEqual(values["ttm_sales"], "")
        self.assertIn("REPORTED_PERIOD_REQUIRES_REVIEW", values["review_flags"])


class FinancialInputBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = self.root / "data/market/jquants"
        self.files = {}
        for name in f.CODE_INPUTS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((project_root() / name).read_bytes())
        raws = [raw_statement(year, q) for year in (2022, 2023, 2024) for q in (1, 2, 3, 4)]
        payload = storage.encoded({"data": raws})
        self.financial_object = self.member("jquants/objects/financial.json", payload)
        self.member("jquants/datasets/financial_summary/4052/latest.json", storage.encoded({
            "status": "complete", "row_count": len(raws), "pages": [{"path": "objects/financial.json",
                "sha256": storage.digest(payload), "bytes": len(payload), "row_count": len(raws),
                "retrieved_at": "2026-10-05T09:00:00+09:00"}]}))
        actual, capital = [], []
        for raw in raws:
            a, _, s = observations.financial_records(raw, "2026-10-05T09:00:00+09:00",
                                                    "data/market/jquants/" + self.financial_object)
            actual.append(a)
            capital.append(s)
        observations.save_rows(self.root, "financial-history.csv", actual)
        observations.save_rows(self.root, "share-count-history.csv", capital)
        self.day = "2025-01-31"
        master = [dict(Date=self.day, Code="40520", ProdCat="011", Mkt="0113", CoName="フィーチャ", S33Nm="情報・通信業"),
                  dict(Date=self.day, Code="12340", ProdCat="011", Mkt="0111", CoName="欠損株", S33Nm="建設業"),
                  dict(Date=self.day, Code="99990", ProdCat="012", Mkt="0111", CoName="外国株", S33Nm=""),
                  dict(Date=self.day, Code="88881", ProdCat="011", Mkt="0111", CoName="優先株", S33Nm=""),
                  dict(Date=self.day, Code="77770", ProdCat="011", Mkt="0105", CoName="PRO", S33Nm="")]
        page = storage.encoded({"data": master})
        self.member("jquants/objects/master.json", page)
        self.member(f"jquants/datasets/equities_master/{self.day}/latest.json", storage.encoded({
            "status": "complete", "query": {"date": self.day}, "period": {"from": self.day, "to": self.day},
            "row_count": len(master), "pages": [{"path": "objects/master.json", "sha256": storage.digest(page),
                         "bytes": len(page), "row_count": len(master)}]}))
        stream = io.StringIO()
        fields = ["日付", "銘柄コード", "銘柄名", "市場・商品区分", "33業種区分", "取得状態"]
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerow(dict(zip(fields, [self.day, "4052", "フィーチャ", "グロース", "情報・通信業", "OK"])))
        prices = stream.getvalue().encode()
        path = f"daily-prices/2025/{self.day}.csv"
        self.member(path, prices, suffix=".csv")
        generation = storage.put_plain(self.store, storage.encoded({"version": 2, "files": self.files}),
                                       ".json", "generations")
        self.register = self.root / "research/prices.json"
        self.register.parent.mkdir(exist_ok=True)
        self.register.write_text(json.dumps(dict(adopted_generation=Path(generation).stem, no_unofficial_fallback=True,
            files=[dict(date=self.day, path=".work/market-data/" + path, sha256=storage.digest(prices)),
                   dict(date="2025-02-03", path="not-read.csv", sha256="not-read")])) + "\n")
        self.output = self.root / "research/results"

    def member(self, name, raw, suffix=".json"):
        obj = storage.put_plain(self.store, raw, suffix)
        self.files[name] = {"object": obj, "sha256": storage.digest(raw), "bytes": len(raw)}
        return obj

    def build(self):
        return f.build(self.root, price_register=self.register, start=date(2025, 1, 1),
                       through=date(2025, 1, 31), output=self.output)

    def test_build_keeps_master_issues_without_prices_and_creates_no_assessments(self):
        summary = self.build()
        with (self.output / "monthly/2025-01-31.csv").open() as stream:
            rows = {row["code"]: row for row in csv.DictReader(stream)}
        self.assertEqual(set(rows), {"4052", "1234"})
        self.assertEqual(rows["1234"]["price_status"], "PRICE_ROW_MISSING")
        self.assertEqual(rows["1234"]["input_status"], "NO_FINANCIAL_STATEMENT")
        self.assertEqual(rows["4052"]["ttm_sales"], "180")
        self.assertEqual(summary["dated_master_months"], 1)
        self.assertFalse(summary["historical_original_values_certified"])
        self.assertFalse(summary["full_strategy_backtest"])
        self.assertFalse(summary["live_gate_satisfied"])
        self.assertEqual(summary["full_rule_assessments_generated"], 0)

    def test_monthly_outputs_are_deterministic(self):
        first = self.build()
        self.assertEqual(self.build(), first)
        for name, sha in first["outputs"].items():
            self.assertEqual(storage.digest((self.output / name).read_bytes()), sha)

    def test_an_incomplete_last_month_is_not_called_month_end(self):
        register = json.loads(self.register.read_text())
        store = f.FrozenStore(self.root, register["adopted_generation"])
        days = [day for day, *_ in f.monthly_prices(self.root, register, store,
                date(2025, 1, 1), date(2025, 2, 3))]
        self.assertEqual(days, [date(2025, 1, 31)])

    def test_corrupt_raw_disclosure_is_rejected_before_outputs(self):
        (self.store / self.financial_object).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_mutable_observations_and_current_pointer_do_not_change_a_frozen_result(self):
        original = self.build()
        p = self.root / "data/market/observations/financial-history/2025.csv"
        p.write_text(p.read_text().replace(",180,36,", ",181,36,"))
        (self.store / "current.json").write_text('{"generation":"future-not-used"}')
        self.assertEqual(self.build(), original)

    def test_stale_month_files_fail_before_existing_outputs_are_modified(self):
        original = self.build()
        p = self.output / "monthly/2026-09-30.csv"
        p.write_text("stale")
        with self.assertRaisesRegex(ValueError, "unexpected monthly"):
            self.build()
        for name, sha in original["outputs"].items():
            self.assertEqual(storage.digest((self.output / name).read_bytes()), sha)

    def test_paths_outside_repository_or_research_are_rejected(self):
        with self.assertRaises(ValueError):
            f.local_path(self.root, "../outside.csv")
        with self.assertRaises(ValueError):
            f.local_path(self.root, "/outside.csv")
        with self.assertRaises(ValueError):
            f.build(self.root, price_register=self.register, start=date(2025, 1, 1),
                    through=date(2025, 1, 31), output=self.root / "operations")


if __name__ == "__main__":
    unittest.main()
