import json
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.jquants_catalog import BY_NAME, CATALOG, entitlement, period, query_for
from scripts.jquants_ingest import (
    ApiError, Client, IngestionError, RequestBudgetExhausted, archive_lock,
    collect_partition, ingest, partition_rows, planned_status, private_root,
)


class JQuantsCatalogTests(unittest.TestCase):
    def test_catalog_accounts_for_all_documented_resources(self):
        self.assertEqual(len(CATALOG), 31)
        self.assertEqual(len({item.endpoint for item in CATALOG}), 31)
        report = planned_status("light", (), date(2026, 10, 1))
        supported = {name for name, item in report["datasets"].items() if item["status"] == "supported" and item["role"] == "dataset"}
        self.assertEqual(supported, {"equities_master", "equities_daily", "equities_valuation", "financial_summary", "earnings_dates", "earnings_calendar", "trading_calendar", "investor_types", "topix_daily"})
        self.assertFalse(report["all_entitled_complete"])
        self.assertEqual(report["datasets"]["financial_details"]["status"], "not_entitled")

    def test_exact_dates_and_no_tier_inference(self):
        day = date(2026, 10, 1)
        self.assertEqual(period(BY_NAME["equities_daily"], "light", day), (date(2021, 10, 1), day))
        self.assertEqual(period(BY_NAME["trading_calendar"], "light", day)[1], date(2027, 12, 31))
        self.assertEqual(query_for(BY_NAME["short_positions"], day), {"disc_date": "2026-10-01"})
        self.assertEqual(query_for(BY_NAME["earnings_calendar"], day), {})
        self.assertEqual(query_for(BY_NAME["topix_daily"], day), {"from": "2026-10-01", "to": "2026-10-01"})
        with self.assertRaises(ValueError):
            entitlement(BY_NAME["equities_daily"], "paid")
        self.assertFalse(entitlement(BY_NAME["equities_minute"], "premium"))
        self.assertTrue(entitlement(BY_NAME["equities_minute"], "light", ("minute_tick",)))


class JQuantsClientTests(unittest.TestCase):
    def test_key_missing_fails_closed(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(IngestionError, "JQUANTS_API_KEY"):
                Client(plan="light")

    def test_transient_retry_waits_two_minutes_on_429(self):
        calls, sleeps = [], []
        def transport(endpoint, query):
            calls.append(query)
            if len(calls) == 1:
                raise ApiError(429)
            return {"data": []}
        client = Client(plan="light", transport=transport, sleep=sleeps.append)
        self.assertEqual(client.get("/equities/master", {}), {"data": []})
        self.assertTrue(any(delay >= 120 for delay in sleeps))
        self.assertEqual(client.requests, 2)

    def test_403_is_error_not_entitlement_or_retry(self):
        calls = []
        def transport(endpoint, query):
            calls.append(query)
            raise ApiError(403, "access_denied_unclassified")
        with self.assertRaises(ApiError):
            Client(plan="light", transport=transport, sleep=lambda _: None).get("/equities/master", {})
        self.assertEqual(len(calls), 1)

    def test_endpoint_allowlist_also_applies_to_fixtures(self):
        client = Client(plan="light", transport=lambda *_: {"data": []})
        with self.assertRaises(IngestionError):
            client.get("https://untrusted.invalid/key", {})

    def test_premium_paces_under_dedicated_60_per_minute(self):
        client = Client(plan="premium", transport=lambda *_: {"data": []})
        self.assertGreaterEqual(client.interval, 1)


class JQuantsArchiveTests(unittest.TestCase):
    day = date(2026, 10, 1)

    def test_pages_resume_and_preserve_nested_data(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            calls = []
            def transport(endpoint, params):
                calls.append(params)
                if not params.get("pagination_key"):
                    return {"data": [{"Code": "285A0", "nested": {"value": None}}], "pagination_key": "next-page"}
                return {"data": [{"Code": "12340"}]}
            client = Client(plan="light", max_requests=1, transport=transport)
            with self.assertRaises(RequestBudgetExhausted):
                collect_partition(root, BY_NAME["equities_master"], self.day, client)
            self.assertFalse((root / "datasets/equities_master/2026-10-01/latest.json").exists())
            result = collect_partition(root, BY_NAME["equities_master"], self.day,
                                       Client(plan="light", transport=transport))
            self.assertEqual(result["row_count"], 2)
            self.assertEqual(calls[-1]["pagination_key"], "next-page")
            self.assertEqual(len(calls), 2)
            rows = partition_rows(root, "equities_master", self.day)
            self.assertIsNone(rows[0]["nested"]["value"])
            self.assertEqual(rows[0]["Code"], "285A0")

    def test_revisions_preserve_previous_payload(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for value in [100, 101]:
                collect_partition(root, BY_NAME["equities_daily"], self.day,
                                  Client(plan="light", transport=lambda *_: {"data": [{"C": value}]}))
            folder = root / "datasets/equities_daily/2026-10-01"
            self.assertEqual(len(list((folder / "revisions").glob("*.json"))), 2)
            self.assertEqual(partition_rows(root, "equities_daily", self.day)[0]["C"], 101)
            self.assertEqual(len(list((root / "objects").glob("*/*.json"))), 2)

    def test_pagination_cycle_does_not_publish(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = Client(plan="light", transport=lambda *_: {"data": [], "pagination_key": "same"}, sleep=lambda _: None)
            with self.assertRaisesRegex(ApiError, "pagination_cycle"):
                collect_partition(root, BY_NAME["equities_daily"], self.day, client)
            self.assertFalse((root / "datasets/equities_daily/2026-10-01/latest.json").exists())

    def test_missing_data_or_invalid_row_does_not_publish(self):
        for payload in [{}, {"data": [None]}, {"data": {}}, {"data": [], "pagination_key": 3}]:
            with self.subTest(payload=payload), TemporaryDirectory() as temp:
                with self.assertRaises(ApiError):
                    collect_partition(Path(temp), BY_NAME["equities_daily"], self.day,
                                      Client(plan="light", transport=lambda *_: payload))

    def test_valid_empty_partition_counts_as_observed_not_unavailable(self):
        with TemporaryDirectory() as temp:
            result = collect_partition(Path(temp), BY_NAME["equities_daily"], self.day,
                                       Client(plan="light", transport=lambda *_: {"data": []}))
            self.assertEqual(result["row_count"], 0)
            self.assertEqual(result["status"], "complete")

    def test_checksum_tampering_blocks_resume(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = Client(plan="light", max_requests=1, transport=lambda *_: {"data": [], "pagination_key": "next"})
            with self.assertRaises(RequestBudgetExhausted):
                collect_partition(root, BY_NAME["equities_daily"], self.day, client)
            next((root / "objects").glob("*/*.json")).write_text("{}")
            with self.assertRaisesRegex(IngestionError, "checksum"):
                collect_partition(root, BY_NAME["equities_daily"], self.day, client)

    def test_private_storage_rejects_public_absolute_and_symlink_escape(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for destination in ["data/daily-prices", "operations/private/../public", str(root / "outside")]:
                with self.assertRaises(IngestionError):
                    private_root(root, destination)
            (root / "operations/private/market-data").mkdir(parents=True)
            (root / "operations/private/market-data/escape").symlink_to(root)
            with self.assertRaises(IngestionError):
                private_root(root, "operations/private/market-data/escape")

    def test_concurrent_ingestion_fails_closed(self):
        with TemporaryDirectory() as temp:
            with archive_lock(Path(temp)):
                with self.assertRaises(IngestionError):
                    with archive_lock(Path(temp)):
                        pass

    def test_descendant_symlinks_never_write_outside_archive(self):
        for relative in ["datasets", "objects", ".ingestion.lock", "latest.json"]:
            with self.subTest(relative=relative), TemporaryDirectory() as temp:
                base = Path(temp)
                root, outside = base / "archive", base / "outside"
                root.mkdir(); outside.mkdir()
                destination = outside / "file" if relative.endswith(".json") or relative.startswith(".") else outside
                if destination != outside:
                    destination.write_text("sentinel")
                (root / relative).symlink_to(destination)
                with self.assertRaises(IngestionError):
                    if relative in {"datasets", "objects"}:
                        collect_partition(root, BY_NAME["equities_daily"], self.day,
                                          Client(plan="light", transport=lambda *_: {"data": []}))
                    else:
                        ingest(root=root, archive=root, plan="light", addons=(), as_of=self.day,
                               client=Client(plan="light", transport=lambda *_: {"data": []}))
                self.assertEqual(sorted(item.name for item in outside.iterdir()), ["file"] if destination != outside else [])

    def test_snapshot_preflight_and_complete_backfill_resume(self):
        from scripts.price_snapshot import validate_tracked_price_snapshot
        from datetime import datetime, timezone
        first = date(2026, 9, 29)
        with TemporaryDirectory() as temp, patch("scripts.jquants_ingest.period", return_value=(first, self.day)):
            root = Path(temp)
            template = json.loads((Path(__file__).resolve().parents[1] / "operations/templates/source-config-template.json").read_text())
            template["jquants"]["plan"] = "light"
            private = root / "operations/private"
            private.mkdir(parents=True)
            (private / "source-config.json").write_text(json.dumps(template))
            archive = private_root(root, template["jquants"]["ingestion"]["archive_root"])
            calls = []
            def transport(endpoint, params):
                calls.append((endpoint, params))
                day = params.get("date", params.get("from"))
                if endpoint == "/equities/bars/daily":
                    return {"data": [{"Code": "285A0", "Date": day, "O": 100, "H": 102, "L": 99, "C": 101, "Vo": 1000, "Va": 101000, "AdjC": 50.5}]}
                if endpoint == "/equities/master":
                    return {"data": [{"Code": "285A0", "Date": day, "ProdCat": "011", "Mkt": "0111", "CoName": "Synthetic"}]}
                return {"data": []}
            report = ingest(root=root, archive=archive, plan="light", addons=(), as_of=self.day,
                            client=Client(plan="light", max_requests=2, transport=transport, sleep=lambda _: None))
            self.assertFalse(report["all_entitled_complete"])
            self.assertEqual(report["daily_snapshot"]["status"], "exported")
            self.assertEqual([call[0] for call in calls], ["/equities/bars/daily", "/equities/master"])
            evidence, failures = validate_tracked_price_snapshot(root=root, active_targets={"285A"},
                cutoff=datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc), config=template)
            self.assertEqual(failures, [])
            self.assertEqual(evidence["price_date"], "2026-10-01")
            report = ingest(root=root, archive=archive, plan="light", addons=(), as_of=self.day,
                            client=Client(plan="light", transport=transport, sleep=lambda _: None))
            self.assertTrue(report["all_entitled_complete"])
            normalized = root / template["price_source"]["archive_root"]
            self.assertEqual(len(list((normalized / "2026").glob("*.csv"))), 3)
            latest = json.loads((normalized / "latest.json").read_text())
            self.assertEqual(latest["latest_trading_date"], "2026-10-01")
            for reference in latest["raw_provenance"]["partitions"]:
                self.assertTrue((archive / reference["path"]).is_file())
                self.assertIn("/revisions/", reference["path"])
            for path in archive.rglob("*"):
                if path.is_dir():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_operational_alphanumeric_normalization_is_consistent(self):
        from scripts.non_edinet_sources import _normalize_code as non_edinet
        from scripts.official_source_scan import _normalize_code as official
        from scripts.price_snapshot import normalize_code
        for function in (non_edinet, official, normalize_code):
            self.assertEqual(function("285a0"), "285A")
            self.assertEqual(function("285A1"), "285A1")

    def test_budget_resume_status_and_full_coverage(self):
        with TemporaryDirectory() as temp, patch("scripts.jquants_ingest.period", return_value=(self.day, self.day)):
            root = Path(temp)
            report = ingest(root=root, archive=root, plan="light", addons=(), as_of=self.day,
                            client=Client(plan="light", max_requests=1, transport=lambda *_: {"data": []}))
            self.assertFalse(report["all_entitled_complete"])
            self.assertEqual(report["datasets"]["equities_master"]["collection_status"], "complete")
            self.assertEqual(report["datasets"]["equities_daily"]["collection_status"], "pending_resume")
            report = ingest(root=root, archive=root, plan="light", addons=(), as_of=self.day,
                            client=Client(plan="light", transport=lambda *_: {"data": []}, sleep=lambda _: None))
            self.assertTrue(report["all_entitled_complete"])
            self.assertEqual(report["requests_this_run"], 8)

    def test_initial_reconciliation_resumes_across_days_without_restarting(self):
        first = date(2026, 9, 30)
        with TemporaryDirectory() as temp, patch("scripts.jquants_ingest.period", return_value=(first, self.day)):
            root = Path(temp)
            report = ingest(root=root, archive=root, plan="light", addons=(), as_of=self.day,
                            dataset_names=("equities_master",), client=Client(plan="light", max_requests=1, transport=lambda *_: {"data": []}))
            self.assertEqual(report["datasets"]["equities_master"]["collection_status"], "pending_resume")
            calls = []
            def transport(endpoint, params):
                calls.append(params)
                return {"data": []}
            ingest(root=root, archive=root, plan="light", addons=(), as_of=date(2026, 10, 2),
                   dataset_names=("equities_master",), client=Client(plan="light", transport=transport))
            self.assertEqual(calls, [{"date": "2026-09-30"}])


if __name__ == "__main__":
    unittest.main()
