"""Source projections retain dated records, exact values and unconfirmed signals."""
import csv
from pathlib import Path
import tempfile
import unittest

from stock_jp.market_data import observations as o
from stock_jp.market_data.jquants import ingest
from stock_jp.workspace import record_path
from tests import test_jquants_daily as daily_fixture


class MarketObservationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def financial(self, day, number, retrieved="2026-10-04T12:00:00Z", **values):
        return o.financial_records({"DiscDate": day, "DiscTime": "17:30:00", "Code": "12340",
                                    "DiscNo": number, **values}, retrieved, "data/source.json")

    def read(self, path):
        with path.open(newline="") as stream:
            return list(csv.DictReader(stream))

    def test_year_partitions_preserve_history_and_latest_is_newest_per_issue(self):
        old = self.financial("2025-01-02", "old")[0]
        new = self.financial("2026-04-03", "new")[0]
        result = o.save_rows(self.root, "financial-history.csv", [new, old])
        self.assertEqual(result, {"history_rows": 2, "latest_rows": 1, "years": ["2025", "2026"]})
        path = record_path(self.root, "financial-history.csv")
        self.assertEqual(self.read(path)[0]["source_id"], "jq-fin-new")
        before = {p: p.read_bytes() for p in o.publication_files(self.root)}
        o.save_rows(self.root, "financial-history.csv", [old, new])
        self.assertEqual(before, {p: p.read_bytes() for p in o.publication_files(self.root)})

    def test_exact_reported_numbers_and_missing_values_are_not_filled(self):
        actual, forecast, shares = self.financial("2026-10-02", "values",
            Sales="123456789012345678901.12", OP="0", FSales="", ShOutFY="100000000000001", TrShFY="")
        self.assertEqual(actual["sales"], "123456789012345678901.12")
        self.assertEqual(actual["operating_profit"], "0")
        self.assertEqual(actual["net_profit"], "")
        self.assertEqual(forecast["forecast_sales"], "")
        self.assertEqual(shares["issued_shares_fy"], "100000000000001")
        self.assertEqual(actual["retrieved_at_jst"], "2026-10-04T21:00:00+09:00")
        with self.assertRaisesRegex(ValueError, "time is missing"):
            o.financial_records({"Code": "12340", "DiscDate": "2026-10-02"}, "2026-10-04T12:00:00Z", "evidence")

    def test_newer_retrieval_wins_across_timezones(self):
        old = self.financial("2026-10-02", "same", "2026-10-04T17:00:00+09:00", Sales="1")[0]
        new = self.financial("2026-10-02", "same", "2026-10-04T12:00:00Z", Sales="2")[0]
        o.save_rows(self.root, "financial-history.csv", [old, new, old])
        self.assertEqual(next(iter(o.load_history(self.root, "financial-history.csv").values()))["sales"], "2")

    def test_wrong_year_and_unexpected_publication_path_are_rejected(self):
        o.save_rows(self.root, "financial-history.csv", [self.financial("2026-10-02", "test")[0]])
        directory = record_path(self.root, "financial-history.csv").parent
        (directory / "2026.csv").rename(directory / "2025.csv")
        with self.assertRaisesRegex(ValueError, "wrong year"):
            o.load_history(self.root, "financial-history.csv")
        self.assertFalse(o.publication_path("data/market/observations/financial-history/../../policy.csv"))
        self.assertFalse(o.publication_path("data/market/observations/unknown/latest.csv"))

    def test_rebuild_is_offline_repeatable_and_preserves_unapplied_action(self):
        fixture = daily_fixture.DailyTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)

        def provider(endpoint, params):
            result = fixture.transport(endpoint, params)
            if endpoint == "/equities/bars/daily":
                result["data"][0]["AdjFactor"] = "0.5"
            return result

        fixture.run_daily(transport=provider)
        root = fixture.root
        first = o.rebuild(root)
        action = self.read(record_path(root, "corporate-actions.csv"))[0]
        self.assertEqual(action["action_type"], "JQUANTS_ADJUSTMENT_FACTOR")
        self.assertEqual(action["applied_at_jst"], "")
        self.assertEqual(action["ratio_numerator"], "")
        self.assertTrue(action["evidence_path"].startswith("data/market/jquants/objects/"))
        action["notes"] = "Human evidence annotation retained"
        o.save_rows(root, "corporate-actions.csv", [action])
        before = {p: p.read_bytes() for p in o.publication_files(root)}
        self.assertEqual(first["rows"], o.rebuild(root)["rows"])
        self.assertEqual(before, {p: p.read_bytes() for p in o.publication_files(root)})
        self.assertEqual(len(fixture.calls), 9)
        manifest = ingest.read_json(next((fixture.archive / "datasets/financial_summary").glob("*/revisions/*.json")))
        blob = fixture.archive / manifest["pages"][0]["path"]
        ingest.atomic_bytes(blob, b"{}")
        with self.assertRaisesRegex(ValueError, "checksum"):
            o.rebuild(root)
