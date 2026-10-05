from datetime import date, timedelta
import hashlib
import json
import unittest

from stock_jp.research.mrs_validation import adjusted_breadth, dataset_rows


class MRSValidationTest(unittest.TestCase):
    def test_breadth_retains_missing_seasoned_issues_in_denominator(self):
        day = date(2026, 9, 30)
        values = [(day - timedelta(days=199 - i), 100 + i) for i in range(200)]
        appearances = {str(i): 200 for i in range(100)}
        master = set(appearances) | {"IPO"}
        appearances["IPO"] = 100
        history = {str(i): values for i in range(97)}
        result = adjusted_breadth(history, appearances, master, day)
        self.assertEqual(result["status"], "NA")
        self.assertEqual(result["seasoned_universe"], 100)
        self.assertEqual(result["coverage"], 0.97)
        history["97"] = values
        result = adjusted_breadth(history, appearances, master, day)
        self.assertEqual(result["value"], 100)
        self.assertEqual(result["excluded_short_history"], 1)

    def test_breadth_rejects_stale_and_future_latest_closes(self):
        day = date(2026, 9, 30)
        for last in (day - timedelta(days=8), day + timedelta(days=1)):
            values = [(last - timedelta(days=199 - i), 100) for i in range(200)]
            self.assertEqual(adjusted_breadth({"A": values}, {"A": 200}, {"A"}, day)["status"], "NA")

    def test_dataset_rows_verifies_pinned_page_hash_and_counts(self):
        raw = json.dumps({"data": [{"Code": "40520", "C": 100}]}).encode()
        page = {"path": "page.json", "sha256": hashlib.sha256(raw).hexdigest(), "row_count": 1}
        meta = {"status": "complete", "dataset": "equities_daily", "row_count": 1, "pages": [page]}

        class Store:
            def read(self, name):
                return raw if name == "jquants/page.json" else json.dumps(meta).encode()

        self.assertEqual(dataset_rows(Store(), "equities_daily", "2026-09-30")[0]["C"], 100)
        page["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "checksum"):
            dataset_rows(Store(), "equities_daily", "2026-09-30")
        page["sha256"] = hashlib.sha256(raw).hexdigest()
        meta["row_count"] = 2
        with self.assertRaisesRegex(ValueError, "row count"):
            dataset_rows(Store(), "equities_daily", "2026-09-30")
