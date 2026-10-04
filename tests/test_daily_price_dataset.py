from stock_jp.workspace import project_root
import csv
import hashlib
import json
from pathlib import Path
import sys
import unittest
from tempfile import TemporaryDirectory
from tests.operation_test_support import write_price_archive


ROOT = project_root()
sys.path.insert(0, str(ROOT))

from stock_jp.market_data.validate_prices import validate_archive  # noqa: E402


class DailyPriceDatasetTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        write_price_archive(root, ["1234", "5678"], price_date="2026-08-28")
        write_price_archive(root, ["1234", "5678"], price_date="2026-08-31")
        self.data = root / '.work/market-data/daily-prices'

    def test_synthetic_archive_retains_session_coverage(self):
        archive = validate_archive(self.data, source_label="synthetic fixture")
        self.assertEqual([item["date"] for item in archive["sessions"]], ["2026-08-28", "2026-08-31"])
        for session in archive["sessions"]:
            self.assertEqual(session["row_count"], 2)
            self.assertEqual(session["quote_count"], 2)
            self.assertEqual(session["fetch_error_count"], 0)

    def test_latest_manifest_matches_latest_csv(self) -> None:
        manifest = json.loads((self.data / "latest.json").read_text(encoding="utf-8"))
        latest = manifest["latest_session"]
        csv_path = self.data / latest["file"]
        content = csv_path.read_bytes()
        with csv_path.open(encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))

        self.assertEqual(hashlib.sha256(content).hexdigest(), latest["sha256"])
        self.assertEqual(len(rows), manifest["universe"]["count"])
        self.assertEqual(len({row["銘柄コード"] for row in rows}), len(rows))
        self.assertTrue(all(row["日付"] == latest["date"] for row in rows))

        counts = {"OK": 0, "NO_QUOTE": 0, "FETCH_ERROR": 0}
        for row in rows:
            self.assertIn(row["取得状態"], counts)
            counts[row["取得状態"]] += 1
        self.assertEqual(counts["OK"], latest["quote_count"])
        self.assertEqual(counts["NO_QUOTE"], latest["no_quote_count"])
        self.assertEqual(counts["FETCH_ERROR"], latest["fetch_error_count"])
        self.assertEqual(manifest["fetch"]["error_count"], counts["FETCH_ERROR"])


if __name__ == "__main__":
    unittest.main()
