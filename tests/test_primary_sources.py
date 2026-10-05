from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stock_jp.market_data.primary_sources import Download, JST, available_at, checked_url, collect, confined, feed_items, preserve


class PrimarySourceTest(unittest.TestCase):
    def test_jsonp_is_data_and_issuer_and_complete_count_are_checked(self):
        payload = {"header": [{"customer_code": "4052"}], "item_count": 1, "item": [{"title": "a"}]}
        raw = ("\ufeffeolparts_announcement_0(" + json.dumps(payload) + ");").encode()
        self.assertEqual(feed_items(raw, "4052"), payload["item"])
        payload["item_count"] = "1"
        self.assertEqual(feed_items(("eolparts_announcement_0(" + json.dumps(payload) + ")").encode(), "4052"), payload["item"])
        with self.assertRaises(ValueError):
            feed_items(raw, "9999")
        with self.assertRaises(ValueError):
            feed_items(b"alert('script');", "4052")
        payload["item_count"] = 2
        with self.assertRaises(ValueError):
            feed_items(("eolparts_announcement_0(" + json.dumps(payload) + ")").encode(), "4052")

    def test_date_only_publication_is_available_after_the_trading_close(self):
        value, precision = available_at("2026/09/28 00:00:00")
        self.assertEqual(precision, "DATE_ONLY")
        self.assertEqual(value.isoformat(), "2026-09-28T23:59:59+09:00")
        self.assertEqual(available_at("2026/09/30 10:04:00")[1], "SECOND")

    def test_paths_and_origins_cannot_escape_and_raw_objects_are_immutable(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("../out.json", "/etc/passwd"):
                with self.assertRaises(ValueError):
                    confined(root, name)
            raw = b"%PDF-1.7 synthetic test fixture"
            receipt = preserve(root, raw, ".pdf")
            self.assertEqual(preserve(root, raw, ".pdf"), receipt)
            (root / receipt["path"]).write_bytes(b"corrupted")
            with self.assertRaises(ValueError):
                preserve(root, raw, ".pdf")
        for url in ("http://allowed/a", "https://unreviewed/a", "https://key@allowed/a", "https://allowed:123/a"):
            with self.assertRaises(ValueError):
                checked_url(url, {"allowed"})

    def test_failed_attempts_consume_budget_and_wait_between_requests(self):
        times = [0.0]
        attempts = []
        def sleep(seconds):
            times[0] += seconds
        def opener(request, timeout):
            attempts.append(times[0])
            raise OSError("unavailable")
        fetch = Download({"allowed"}, limit=2, opener=opener, clock=lambda: times[0], sleep=sleep)
        for _ in range(2):
            with self.assertRaises(OSError):
                fetch("https://allowed/document")
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            fetch("https://allowed/document")
        self.assertEqual(fetch.requests, 2)
        self.assertGreaterEqual(attempts[1] - attempts[0], 1.05)

    def test_collection_preserves_partial_sources_and_never_certifies_universe(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "operations/inputs/primary-sources.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"max_requests": 5, "allowed_hosts": ["allowed"], "issuers": [],
                                       "public_series": [{"id": "vi", "title": "VI", "url": "https://allowed/vi", "suffix": ".csv"},
                                                         {"id": "ci", "title": "CI", "url": "https://allowed/ci", "suffix": ".html"}]}))
            def download(url):
                if url.endswith("ci"):
                    raise OSError("unavailable")
                return b"Date,Close\n2026-10-02,20\n"
            result = collect(root, cutoff=datetime(2026, 10, 6, tzinfo=JST), download=download)
            self.assertEqual(len(result["sources"]), 1)
            self.assertEqual(len(result["blockers"]), 1)
            self.assertFalse(result["full_universe_disclosure_coverage_certified"])
            self.assertEqual(result["sources"][0]["review_status"], "COLLECTED_NOT_REVIEWED")
