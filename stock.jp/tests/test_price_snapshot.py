import hashlib
import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from scripts.price_snapshot import validate_tracked_price_snapshot
from tests.operation_test_support import write_price_archive


JST = ZoneInfo("Asia/Tokyo")
CONFIG = {
    "price_source": {
        "enabled": True,
        "provider": "jquants_v2_private_archive",
        "storage": "private_local",
        "archive_root": "operations/private/market-data/daily-prices",
        "manifest_path": "latest.json",
        "minimum_daily_archive_coverage": 0.98,
        "minimum_active_target_coverage": 1.0,
        "maximum_latest_price_age_days": 7,
    }
}


class PriceSnapshotTest(unittest.TestCase):
    def test_validates_checksum_full_coverage_and_active_subset(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_price_archive(root, ["1234", "5678"])

            evidence, failures = validate_tracked_price_snapshot(
                root=root,
                active_targets={"1234"},
                cutoff=datetime(2026, 9, 1, 18, 30, tzinfo=JST),
                config=CONFIG,
            )

            self.assertEqual(failures, [])
            self.assertEqual(evidence["target_codes"], ["1234"])
            self.assertEqual(evidence["target_coverage_ratio"], 1.0)

    def test_empty_active_subset_still_validates_the_full_archive(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_price_archive(root, ["1234"])

            evidence, failures = validate_tracked_price_snapshot(
                root=root,
                active_targets=set(),
                cutoff=datetime(2026, 9, 1, 18, 30, tzinfo=JST),
                config=CONFIG,
            )

            self.assertEqual(failures, [])
            self.assertEqual(evidence["target_count"], 0)
            self.assertEqual(evidence["target_coverage_ratio"], 1.0)

    def test_missing_active_target_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_price_archive(root, ["1234"])

            evidence, failures = validate_tracked_price_snapshot(
                root=root,
                active_targets={"1234", "5678"},
                cutoff=datetime(2026, 9, 1, 18, 30, tzinfo=JST),
                config=CONFIG,
            )

            self.assertIsNone(evidence)
            self.assertIn(
                "J-Quants archive does not cover every active target", failures
            )

    def test_stale_or_modified_session_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = write_price_archive(root, ["1234"])
            session.write_text(session.read_text(encoding="utf-8") + "\n", encoding="utf-8")

            evidence, failures = validate_tracked_price_snapshot(
                root=root,
                active_targets={"1234"},
                cutoff=datetime(2026, 9, 10, 18, 30, tzinfo=JST),
                config=CONFIG,
            )

            self.assertIsNone(evidence)
            self.assertIn("J-Quants price date is stale", failures)
            self.assertIn("J-Quants session checksum does not match", failures)

    def test_rejects_non_official_jquants_manifest_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_price_archive(root, ["1234"])
            path = root / "operations/private/market-data/daily-prices/latest.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["source"]["official"] = False
            path.write_text(json.dumps(manifest), encoding="utf-8")

            evidence, failures = validate_tracked_price_snapshot(
                root=root,
                active_targets={"1234"},
                cutoff=datetime(2026, 9, 1, 18, 30, tzinfo=JST),
                config=CONFIG,
            )

            self.assertIsNone(evidence)
            self.assertEqual(
                failures,
                ["J-Quants latest.json has invalid readiness evidence"],
            )

    def test_rejects_invalid_active_ohlc_even_with_matching_checksum(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = write_price_archive(root, ["1234"])
            text = session.read_text(encoding="utf-8")
            session.write_text(
                text.replace(",105,99,103,", ",101,99,103,"), encoding="utf-8"
            )
            manifest_path = root / "operations/private/market-data/daily-prices/latest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["latest_session"]["sha256"] = hashlib.sha256(
                session.read_bytes()
            ).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            evidence, failures = validate_tracked_price_snapshot(
                root=root,
                active_targets={"1234"},
                cutoff=datetime(2026, 9, 1, 18, 30, tzinfo=JST),
                config=CONFIG,
            )

            self.assertIsNone(evidence)
            self.assertIn(
                "J-Quants archive does not cover every active target", failures
            )

    def test_rejects_manifest_count_that_does_not_match_csv(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_price_archive(root, ["1234"])
            manifest_path = root / "operations/private/market-data/daily-prices/latest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["universe"]["count"] = 2
            manifest["latest_session"]["quote_count"] = 2
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            evidence, failures = validate_tracked_price_snapshot(
                root=root,
                active_targets={"1234"},
                cutoff=datetime(2026, 9, 1, 18, 30, tzinfo=JST),
                config=CONFIG,
            )

            self.assertIsNone(evidence)
            self.assertIn("J-Quants latest session CSV is invalid", failures)


if __name__ == "__main__":
    unittest.main()
