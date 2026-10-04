"""Synthetic file delivery tests: no J-Quants credentials or network."""
from tests.operation_test_support import prepare_write
from datetime import date
import gzip
import hashlib
import io
import json
import os
import stat
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import stock_jp.market_data.jquants.delivery as delivery
from stock_jp.market_data.jquants.catalog import BY_NAME
from stock_jp.market_data.jquants.ingest import Client, RequestBudgetExhausted, atomic_json, save_object

HOST = "reviewed-fixture.s3.ap-northeast-1.amazonaws.com"
URL = "https://" + HOST + "/file?X-Amz-Signature=SIGNED_SECRET"
DAY = date(2026, 9, 30)


class Response(io.BytesIO):
    status = 200
    def __init__(self, body, length=None):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body) if length is None else length)}


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.body = gzip.compress(b"Date,Code,Price\n2026-09-30,12340,100\n", mtime=0)
        self.row = {"Key": "equities/trades/2026/sample.csv.gz", "LastModified": "2026-09-30T10:00:00Z", "Size": len(self.body)}
        self.calls, self.downloads = [], []
        def transport(endpoint, params):
            self.calls.append((endpoint, params))
            if endpoint == "/bulk/list":
                return {"data": [dict(self.row)]}
            if endpoint == "/bulk/get":
                return {"url": URL}
            if endpoint == "/td/bulk":
                return {"lastUpdated": self.row["LastModified"], "url": URL}
            if endpoint == "/td/files":
                return {"discNo": params["discNo"], "files": {"pdf": URL, "summaryPdf": URL, "xbrl": URL}}
            self.fail("unexpected endpoint")
        self.client = Client(plan="light", transport=transport, sleep=lambda _: None)
        self.client.download_hosts = (HOST,)
        def opened(request, timeout):
            self.downloads.append(request)
            return Response(self.body)
        self.opener = patch.object(delivery, "build_opener")
        self.mock = self.opener.start()
        self.mock.return_value.open.side_effect = opened
        self.addCleanup(self.opener.stop)
        self.addCleanup(self.temp.cleanup)

    def collect(self, name="equities_ticks"):
        return delivery.collect_delivery(self.root, BY_NAME[name], self.client, DAY, DAY)

    def assert_no_secrets(self):
        metadata = b"".join(p.read_bytes() for p in self.root.rglob("*.json"))
        self.assertNotIn(b"SIGNED_SECRET", metadata)
        self.assertNotIn(b"X-Amz", metadata)
        for req in self.downloads:
            self.assertNotIn("X-api-key", req.headers)
            self.assertNotIn("Authorization", req.headers)
            self.assertNotIn("Cookie", req.headers)

    def test_bulk_download_resume_and_no_credentials(self):
        first = self.collect()
        self.assertEqual(first["downloaded_files"], 1)
        self.assertEqual(first["collection_status"], "complete")
        again = self.collect()
        self.assertEqual(again["reused_files"], 1)
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.calls[0][1], {"endpoint": "/equities/trades", "from": DAY.isoformat(), "to": DAY.isoformat()})
        self.assert_no_secrets()

    def test_correction_new_identity_preserves_old_receipt(self):
        self.collect()
        self.row["LastModified"] = "2026-10-01T10:00:00Z"
        self.assertEqual(self.collect()["downloaded_files"], 1)
        self.assertEqual(len(list((self.root / "deliveries/equities_ticks/items").glob("*.json"))), 2)

    def test_tampered_blob_fails_before_skip(self):
        self.collect()
        prepare_write(next((self.root / "blobs").rglob("*.csv.gz"))).write_bytes(b"bad")
        with self.assertRaisesRegex(delivery.DeliveryError, "checksum"):
            self.collect()
        self.assertEqual(len(self.downloads), 1)

    def test_missing_hosts_explicit_block_without_api(self):
        self.client.download_hosts = ()
        result = self.collect()
        self.assertEqual(result["collection_status"], "blocked_download_host")
        self.assertEqual(self.calls, [])

    def test_bad_urls_rejected(self):
        for url in ["http://" + HOST + "/a", "https://evil.example/a", "https://" + HOST + ".evil.example/a",
                    "https://name:pass@" + HOST + "/a", "https://" + HOST + ":444/a",
                    "https://127.0.0.1/a", "https://[::1]/a", "https://127.1/a"]:
            with self.subTest(url=url):
                with self.assertRaises(delivery.DeliveryError):
                    delivery._url(url, (HOST, "127.0.0.1", "127.1", "::1"))
        self.assertEqual(delivery._url(URL, (HOST,)), URL)

    def test_redirect_rejected(self):
        with self.assertRaisesRegex(delivery.DeliveryError, "redirect"):
            delivery._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example/")

    def test_gzip_corruption_never_promoted(self):
        self.body = self.body[:-4]
        self.row["Size"] = len(self.body)
        with self.assertRaisesRegex(delivery.DeliveryError, "bounded retries"):
            self.collect()
        self.assertEqual(len(self.downloads), 3)
        self.assertEqual(list((self.root / "blobs").rglob("*.gz")), [])
        self.assertEqual(list((self.root / "blobs").glob(".download-*")), [])
        self.assertFalse((self.root / "deliveries/equities_ticks/latest.json").exists())

    def test_size_mismatch_never_promoted(self):
        self.row["Size"] += 1
        with self.assertRaises(delivery.DeliveryError):
            self.collect()
        self.assertFalse((self.root / "deliveries/equities_ticks/latest.json").exists())

    def test_expired_url_gets_fresh_presign(self):
        original = self.mock.return_value.open.side_effect
        attempts = []
        def opened(req, timeout):
            attempts.append(True)
            if len(attempts) == 1:
                raise HTTPError(URL, 403, "signed URL expired", {}, None)
            return original(req, timeout)
        self.mock.return_value.open.side_effect = opened
        self.assertEqual(self.collect()["downloaded_files"], 1)
        self.assertEqual(sum(path == "/bulk/get" for path, _ in self.calls), 2)
        self.assert_no_secrets()

    def test_budget_interrupt_resumes_from_completed_file(self):
        self.client.max_requests = 1
        with self.assertRaises(RequestBudgetExhausted):
            self.collect()
        self.client.max_requests = None
        self.assertEqual(self.collect()["downloaded_files"], 1)

    def test_interrupted_multifile_resume_preserves_completed_receipt(self):
        transport = self.client.transport
        def two_files(endpoint, params):
            if endpoint == "/bulk/list":
                return {"data": [dict(self.row), {**self.row, "Key": "equities/trades/2026/second.csv.gz"}]}
            return transport(endpoint, params)
        self.client.transport = two_files
        self.client.max_requests = 2
        with self.assertRaises(RequestBudgetExhausted):
            self.collect()
        self.assertEqual(len(self.downloads), 1)
        self.client.max_requests = None
        result = self.collect()
        self.assertEqual((result["downloaded_files"], result["reused_files"]), (1, 1))
        self.assertEqual(len(self.downloads), 2)

    def test_content_length_mismatch_rejected(self):
        self.mock.return_value.open.side_effect = lambda req, timeout: Response(self.body, length=len(self.body) + 1)
        with self.assertRaisesRegex(delivery.DeliveryError, "bounded retries"):
            self.collect()
        self.assertFalse((self.root / "deliveries/equities_ticks/latest.json").exists())

    def test_td_bulk_snapshot_change_fails_without_promotion(self):
        calls = []
        def changing(endpoint, params):
            calls.append(True)
            return {"lastUpdated": "2026-09-30T10:00:00Z" if len(calls) == 1 else "2026-09-30T10:01:00Z", "url": URL}
        self.client.transport = changing
        self.mock.return_value.open.side_effect = HTTPError(URL, 403, "expired", {}, None)
        with self.assertRaisesRegex(delivery.DeliveryError, "snapshot changed"):
            self.collect("td_bulk")
        self.assertFalse((self.root / "deliveries/td_bulk/latest.json").exists())

    def test_td_bulk_saves_no_url_and_reuses_timestamp(self):
        self.assertEqual(self.collect("td_bulk")["downloaded_files"], 1)
        self.assertEqual(self.collect("td_bulk")["reused_files"], 1)
        self.assert_no_secrets()

    def index(self, rows):
        ref = save_object(self.root, {"data": rows})
        atomic_json(self.root / "datasets/td_index" / DAY.isoformat() / "latest.json", {"status": "complete", "pages": [ref]})

    def test_td_files_download_all_advertised_types_and_resume(self):
        self.index([{"DiscNo": "20260930123456", "Docs": ["g", "s", "x"]}])
        self.body = b"synthetic document bytes"
        self.assertEqual(self.collect("td_files")["file_count"], 3)
        self.assertEqual(self.collect("td_files")["reused_files"], 3)
        self.assertEqual(sum(p == "/td/files" for p, _ in self.calls), 3)
        self.assert_no_secrets()

    def test_td_files_fail_when_any_index_partition_missing(self):
        with self.assertRaisesRegex(delivery.DeliveryError, "complete td_index"):
            self.collect("td_files")
        self.assertEqual(self.calls, [])

    def test_td_file_advertised_but_missing_not_complete(self):
        self.index([{"DiscNo": "20260930123456", "Docs": ["g"]}])
        self.client.transport = lambda endpoint, params: {"discNo": "20260930123456", "files": {}}
        with self.assertRaisesRegex(delivery.DeliveryError, "advertised"):
            self.collect("td_files")

    def test_bulk_unknown_fields_not_saved(self):
        self.row["url"] = URL
        self.collect()
        self.assert_no_secrets()

    def test_inventory_invalid_key_not_saved(self):
        self.row["Key"] = "https://evil.example/?SIGNED_SECRET"
        with self.assertRaises(delivery.DeliveryError):
            self.collect()
        self.assertEqual(len(self.downloads), 0)

    def test_all_delivery_write_descendants_reject_symlinks(self):
        paths = ["deliveries", "deliveries/equities_ticks", "blobs",
                 "deliveries/equities_ticks/items", "deliveries/equities_ticks/revisions",
                 "deliveries/equities_ticks/latest.json", "deliveries/equities_ticks/inventory.json",
                 "deliveries/equities_ticks/items/" + delivery._sha(self.row) + ".json",
                 "blobs/" + hashlib.sha256(self.body).hexdigest()[:2]]
        original_root = self.root
        for index, relative in enumerate(paths):
            with self.subTest(relative=relative):
                self.root = original_root / ("case" + str(index))
                self.root.mkdir(parents=True)
                outside = original_root / ("outside" + str(index))
                outside.mkdir(parents=True)
                marker = outside / "marker"
                prepare_write(marker).write_bytes(b"untouched")
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(delivery.DeliveryError, "symlink"):
                    self.collect()
                self.assertEqual(list(outside.iterdir()), [marker])
                self.assertEqual(marker.read_bytes(), b"untouched")
        self.root = original_root

    def test_blob_file_symlink_rejected_on_resume(self):
        self.collect()
        blob = next((self.root / "blobs").rglob("*.csv.gz"))
        outside = self.root / "outside-file"
        prepare_write(outside).write_bytes(blob.read_bytes())
        blob.unlink()
        blob.symlink_to(outside)
        with self.assertRaisesRegex(delivery.DeliveryError, "symlink"):
            self.collect()

    def test_new_and_existing_archive_directories_private(self):
        for relative in ["blobs", "deliveries/equities_ticks/items", "deliveries/equities_ticks/revisions"]:
            path = self.root / relative
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o755)
        self.collect()
        for path in [self.root, *self.root.rglob("*")]:
            if path.is_dir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, str(path))
            elif path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, str(path))

    def test_td_index_path_escape(self):
        atomic_json(self.root / "datasets/td_index" / DAY.isoformat() / "latest.json", {"status": "complete", "pages": [{"path": "../secret", "sha256": "x", "bytes": 1}]})
        with self.assertRaisesRegex(delivery.DeliveryError, "escaped"):
            self.collect("td_files")


if __name__ == "__main__":
    unittest.main()
