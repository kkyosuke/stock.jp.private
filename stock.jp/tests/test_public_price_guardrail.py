from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.validate_daily_price_archive import validate_archive
from scripts.validate_public_price_guardrail import validate_stored_archive
from scripts.validate_scheduled_price_update import ScheduledUpdateError
from tests import test_validate_daily_price_archive as archive_test_support


class PublicPriceGuardrailTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.archive = Path(self.temporary.name)
        helper = archive_test_support.DailyPriceArchiveValidationTest()
        helper._write(self.archive, [helper._row()])
        latest = validate_archive(self.archive, source_label="fixture")["sessions"][-1]
        self.manifest = {
            "schema_version": 1,
            "source": {"provider": "Yahoo Finance", "official": False},
            "latest_trading_date": latest["date"],
            "latest_session": latest,
            "universe": {"count": 1, "provider": "JPX TSE-listed issues monthly spreadsheet", "scope": "current domestic stocks"},
            "fetch": {"success_count": 1, "error_count": 0},
        }
        self.save()

    def save(self) -> None:
        (self.archive / "latest.json").write_text(json.dumps(self.manifest))

    def test_historical_validation_is_read_only(self) -> None:
        before = {p.relative_to(self.archive): p.read_bytes() for p in self.archive.rglob("*") if p.is_file()}
        validate_stored_archive(self.archive, check_freshness=False)
        after = {p.relative_to(self.archive): p.read_bytes() for p in self.archive.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_scheduled_validation_rejects_stale_data(self) -> None:
        with self.assertRaisesRegex(ScheduledUpdateError, "stale"):
            validate_stored_archive(self.archive)

    def test_checksum_and_count_tampering_fail_closed(self) -> None:
        for key, value in (("sha256", "0" * 64), ("quote_count", 0), ("file", "../private.csv")):
            with self.subTest(key=key):
                previous = self.manifest["latest_session"][key]
                self.manifest["latest_session"][key] = value
                self.save()
                with self.assertRaisesRegex(ValueError, key):
                    validate_stored_archive(self.archive, check_freshness=False)
                self.manifest["latest_session"][key] = previous

    def test_unofficial_source_boundary_is_required(self) -> None:
        self.manifest["source"]["official"] = True
        self.save()
        with self.assertRaisesRegex(ValueError, "unofficial"):
            validate_stored_archive(self.archive, check_freshness=False)

    def test_missing_manifest_fails_without_creation(self) -> None:
        (self.archive / "latest.json").unlink()
        with self.assertRaises(FileNotFoundError):
            validate_stored_archive(self.archive, check_freshness=False)
        self.assertFalse((self.archive / "latest.json").exists())

    def test_manifest_identity_must_match_runtime_contract(self) -> None:
        self.manifest["schema_version"] = 2
        self.save()
        with self.assertRaisesRegex(ValueError, "schema_version"):
            validate_stored_archive(self.archive, check_freshness=False)
        self.manifest["schema_version"] = 1
        self.manifest["universe"]["scope"] = "selected holdings"
        self.save()
        with self.assertRaisesRegex(ValueError, "universe identity"):
            validate_stored_archive(self.archive, check_freshness=False)

    def test_historical_mode_still_rejects_future_prices(self) -> None:
        tomorrow = (datetime.now(ZoneInfo("Asia/Tokyo")) + timedelta(days=1)).date().isoformat()
        latest = {**self.manifest["latest_session"], "date": tomorrow}
        self.manifest["latest_trading_date"] = tomorrow
        self.manifest["latest_session"] = latest
        self.save()
        with patch("scripts.validate_public_price_guardrail.validate_archive", return_value={"sessions": [latest]}):
            with self.assertRaisesRegex(ValueError, "future"):
                validate_stored_archive(self.archive, check_freshness=False)
