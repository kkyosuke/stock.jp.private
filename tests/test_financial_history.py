"""Financial bulk coverage, crash accounting and host inspection without network."""
from datetime import date
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stock_jp.market_data.jquants import financial_history as h, ingest


class FinancialHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.day = date(2026, 10, 2)
        ingest.atomic_json(self.root / "config/sources.json", {"jquants": {
            "plan": "light", "enabled": True, "ingestion": {"download_hosts": []}}})
        self.calls = []

    def inventory(self, endpoint):
        rows = []
        for year in range(2021, 2027):
            for month in range(1, 13):
                rows.append({"Key": endpoint.lstrip("/") + f"/historical/{year}/data_{year}{month:02d}.csv.gz",
                             "Size": 100, "LastModified": "2026-10-03T01:00:00Z"})
        return {"data": rows}

    def provider(self, endpoint, params):
        self.calls.append((endpoint, dict(params)))
        if endpoint == "/bulk/list":
            return self.inventory(params["endpoint"])
        return {"url": "https://reviewed-fixture.s3.ap-northeast-1.amazonaws.com/file?X-Amz-Signature=NEVER_PERSIST"}

    def state(self):
        return json.loads((self.root / ".work/market-data" / h.PREFIX / "2026-10-02.json").read_text())

    def test_inspection_collects_inventories_and_keeps_signed_url_out_of_state(self):
        state = h.collect(self.root, self.day, transport=self.provider, sleep=lambda _: None)
        self.assertEqual(state["status"], "INSPECTED")
        self.assertEqual(len(state["requests"]), 3)
        self.assertEqual(len(state["inventories"]["financial_summary"]), 61)
        self.assertEqual(state["observed_download_hosts"], ["reviewed-fixture.s3.ap-northeast-1.amazonaws.com"])
        self.assertNotIn("NEVER_PERSIST", json.dumps(state))
        self.assertNotIn("X-Amz", json.dumps(state))

    def test_inventory_gap_never_claims_complete(self):
        payload = self.inventory("/fins/summary")
        payload["data"] = [row for row in payload["data"] if "202504" not in row["Key"]]
        with self.assertRaisesRegex(ingest.IngestionError, "date gap"):
            h.inventory(payload, "/fins/summary", date(2021, 10, 2), self.day)

        with self.assertRaisesRegex(ingest.IngestionError, "date gap"):
            h.collect(self.root, self.day, transport=lambda *_: payload, sleep=lambda _: None)
        self.assertEqual(self.state()["status"], "PAUSED")
        self.assertEqual(len(self.state()["requests"]), 1)
        self.assertEqual(self.state()["files"], {})

    def test_failed_request_is_reserved_before_provider_and_retained(self):
        def denied(*_):
            self.assertEqual(len(self.state()["requests"]), 1)
            raise ingest.ApiError(403, "authentication_failed")
        with self.assertRaises(ingest.ApiError):
            h.collect(self.root, self.day, transport=denied, sleep=lambda _: None)
        self.assertEqual(len(self.state()["requests"]), 1)
        resumed = h.collect(self.root, self.day, transport=self.provider, sleep=lambda _: None)
        self.assertEqual(len(resumed["requests"]), 4)

    def test_crash_does_not_reset_the_request_budget(self):
        def crash(*_):
            self.assertEqual(len(self.state()["requests"]), 1)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            h.collect(self.root, self.day, transport=crash, sleep=lambda _: None)
        resumed = h.collect(self.root, self.day, transport=self.provider, sleep=lambda _: None)
        self.assertEqual(len(resumed["requests"]), 4)

    def test_429_cooldown_prevents_an_immediate_retry(self):
        def throttle(*_):
            raise ingest.ApiError(429)
        with self.assertRaises(ingest.ApiError):
            h.collect(self.root, self.day, transport=throttle, sleep=lambda _: None)
        self.assertIsNotNone(self.state()["retry_not_before"])
        self.assertEqual(h.collect(self.root, self.day, transport=self.provider)["status"], "PAUSED")
        self.assertEqual(self.calls, [])

    def test_unrelated_or_duplicate_bulk_keys_are_rejected(self):
        payload = self.inventory("/fins/summary")
        for extra in [payload["data"][-3], {**payload["data"][-3], "Key": "other/data_202610.csv.gz"}]:
            with self.assertRaises(ingest.IngestionError):
                h.inventory({"data": payload["data"] + [extra]}, "/fins/summary", date(2021, 10, 2), self.day)

    def test_saved_inventory_is_reused_and_consumption_is_monotonic(self):
        h.collect(self.root, self.day, transport=self.provider, sleep=lambda _: None)
        self.calls.clear()
        again = h.collect(self.root, self.day, transport=self.provider, sleep=lambda _: None)
        self.assertEqual(len(again["requests"]), 4)
        self.assertEqual([endpoint for endpoint, _ in self.calls], ["/bulk/get"])

    def test_verified_download_survives_parse_crash_and_resumes_without_redownload(self):
        ingest.atomic_json(self.root / "config/sources.json", {"jquants": {
            "plan": "light", "enabled": True, "ingestion": {"download_hosts": ["reviewed.invalid"]}}})
        blobs = {"/fins/summary": gzip.compress(b"DiscDate,DiscTime,Code,DiscNo\n2026-10-02,17:30:00,12340,123\n"),
                 "/fins/earnings-date": gzip.compress(b"PubDate,SchDate,Code\n2026-10-02,2026-10-30,12340\n")}
        keys = {endpoint: endpoint.lstrip("/") + "/historical/2026/data_202610.csv.gz" for endpoint in blobs}
        downloads = []

        def provider(endpoint, params):
            self.calls.append((endpoint, dict(params)))
            if endpoint == "/bulk/list":
                dataset = params["endpoint"]
                return {"data": [{"Key": keys[dataset], "Size": float(len(blobs[dataset])),
                                   "LastModified": "2026-10-03T01:00:00Z"}]}
            return {"url": "https://reviewed.invalid/" + params["key"]}

        def download(folder, url, *_args, **_kwargs):
            endpoint = next(endpoint for endpoint, key in keys.items() if url.endswith(key))
            downloads.append(endpoint)
            raw = blobs[endpoint]
            sha = ingest.digest(raw)
            relative = f"blobs/{sha[:2]}/{sha}.csv.gz"
            ingest.atomic_bytes(folder / relative, raw)
            return {"path": relative, "sha256": sha, "bytes": len(raw), "gzip_validated": True}

        with patch.object(h, "period", return_value=(date(2026, 10, 1), self.day)), \
             patch.object(h.delivery, "_fetch", side_effect=download):
            with patch.object(h, "csv_rows", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    h.collect(self.root, self.day, transport=provider, sleep=lambda _: None)
            self.assertEqual(len(self.state()["files"]), 1)
            self.assertIsNone(next(iter(self.state()["files"].values()))["rows"])
            self.assertEqual(len(self.state()["requests"]), 3)
            complete = h.collect(self.root, self.day, transport=provider, sleep=lambda _: None)
            self.assertEqual(complete["status"], "COMPLETE")
            self.assertEqual(len(complete["requests"]), 4)
            self.assertEqual(downloads, list(blobs))
            self.assertEqual(sum(item["rows"] for item in complete["files"].values()), 2)
            self.calls.clear()
            h.collect(self.root, self.day, transport=lambda *_: self.fail("unexpected provider call"))
            item = next(iter(complete["files"].values()))
            ingest.atomic_bytes(self.root / ".work/market-data" / h.PREFIX / item["path"], b"corrupt")
            with self.assertRaisesRegex(ingest.IngestionError, "checksum"):
                h.collect(self.root, self.day, transport=lambda *_: self.fail("unexpected provider call"))
