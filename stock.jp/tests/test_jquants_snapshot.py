import copy
import csv
from datetime import datetime
import json
from pathlib import Path
import unittest
from tempfile import TemporaryDirectory

from scripts.collect_daily_prices import CSV_FIELDS
from scripts.jquants_snapshot import export_daily_snapshot, JQuantsSnapshotError, SOURCE
from scripts.operation_state import _normalized_source_config
from scripts.price_snapshot import normalize_code, validate_tracked_price_snapshot
from tests.operation_test_support import write_price_archive

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-08-31"
RETRIEVED = "2026-08-31T18:30:00+09:00"


def master(code="12340", day=DAY, **extra):
    return {"Date": day, "Code": code, "CoName": f"Example {code}",
            "Mkt": "0111", "MktNm": "プライム", "S33Nm": "情報・通信業",
            "ProdCat": "011", **extra}


def bar(code="12340", day=DAY, **extra):
    return {"Date": day, "Code": code, "O": 100, "H": 110, "L": 90,
            "C": 105, "Vo": 1200, "Va": 126000, "AdjO": 50, "AdjH": 55, "AdjL": 45,
            "AdjC": 52.5, "AdjVo": 2400, "AdjFactor": 0.5, **extra}


class JQuantsSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "operations/private/market-data/daily-prices"
        self.archive.mkdir(parents=True)
        self.config = json.loads((ROOT / "operations/templates/source-config-template.json").read_text())
        (self.root / "operations/private/source-config.json").write_text(json.dumps(self.config))

    def export(self, masters=None, bars=None, **kwargs):
        return export_daily_snapshot(self.root, DAY, masters if masters is not None else [master()],
                                     bars if bars is not None else [bar()], RETRIEVED, **kwargs)

    def rows(self, manifest):
        with (self.archive / manifest["latest_session"]["file"]).open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            self.assertEqual(tuple(reader.fieldnames), CSV_FIELDS)
            return list(reader)

    def readiness(self, targets=None, config=None):
        return validate_tracked_price_snapshot(root=self.root, active_targets=targets or {"1234"},
            cutoff=datetime.fromisoformat("2026-09-01T18:30:00+09:00"), config=config or self.config)

    def test_unadjusted_csv_and_manifest_identity(self):
        manifest = self.export(raw_provenance={"master": "raw/master.json", "bars": "raw/bars.json"})
        row = self.rows(manifest)[0]
        self.assertEqual(row["終値"], "105")
        self.assertEqual(row["始値"], "100")
        self.assertEqual(row["高値"], "110")
        self.assertEqual(row["安値"], "90")
        self.assertEqual(row["売買高(株)"], "1200")
        self.assertEqual(row["前日比"], "")
        self.assertEqual(manifest["source"], SOURCE)
        self.assertEqual(manifest["raw_provenance"]["master"], "raw/master.json")
        self.assertEqual(manifest["universe"]["master_date"], DAY)
        self.assertEqual(self.readiness()[1], [])
        self.assertEqual(self.readiness()[0]["provider"], "jquants_v2_private_archive")
        self.assertIn("PAPER", self.readiness()[0]["decision_use"])
        session = self.archive / manifest["latest_session"]["file"]
        self.assertEqual(session.stem, DAY)
        self.assertEqual(session.read_bytes(), (self.archive / "2026" / f"{DAY}.csv").read_bytes())
        self.assertEqual(session.stat().st_mode & 0o777, 0o600)
        self.assertEqual(session.parent.stat().st_mode & 0o777, 0o700)

    def test_domestic_common_and_alphanumeric_codes_without_current_market_filter(self):
        masters = [master(), master("285A0", Mkt="0101", MktNm="東証一部"),
                   master("99990", ProdCat="021"), master("155A0", ProdCat="014"),
                   master("12345"), master("84210", ProdCat="012")]
        manifest = self.export(masters, [bar(item["Code"]) for item in masters])
        self.assertEqual([row["銘柄コード"] for row in self.rows(manifest)], ["1234", "285A"])
        self.assertEqual(manifest["universe"]["count"], 2)
        self.assertEqual(self.readiness({"285A"})[1], [])
        self.assertEqual(normalize_code("285a0"), "285A")
        self.assertEqual(normalize_code("12345"), "12345")
        self.assertEqual(normalize_code(" 1234 "), "1234")

    def test_missing_quote_retains_full_universe_and_blocks_coverage(self):
        manifest = self.export([master(), master("56780")], [bar()])
        self.assertEqual(manifest["universe"]["count"], 2)
        self.assertEqual(manifest["latest_session"]["quote_count"], 1)
        self.assertEqual(self.rows(manifest)[1]["取得状態"], "NO_QUOTE")
        self.assertIn("J-Quants full-universe coverage is below the daily minimum", self.readiness()[1])

    def test_pro_market_excluded_even_with_valid_price(self):
        manifest = self.export([master(), master("56780", Mkt="0105")], [bar(), bar("56780")])
        self.assertEqual(manifest["universe"]["count"], 1)
        self.assertEqual(manifest["universe"]["excluded_pro_market_count"], 1)
        self.assertEqual(manifest["acquisition"]["bar_row_count"], 2)
        self.assertEqual(manifest["acquisition"]["diagnostics"]["VALID_PRICE"], 2)
        self.assertEqual([row["銘柄コード"] for row in self.rows(manifest)], ["1234"])
        self.assertEqual(self.readiness()[1], [])
        self.assertIsNone(self.readiness({"5678"})[0])

    def test_missing_unknown_market_is_not_silently_excluded(self):
        for market in (None, "", "9999", "0109"):
            with self.subTest(market=market), self.assertRaisesRegex(JQuantsSnapshotError, "market"):
                self.export([master(), master("56780", Mkt=market)], [bar()])

    def test_dated_market_change_changes_membership_independently_of_prices(self):
        first = self.export([master(), master("56780", Mkt="0105")], [bar(), bar("56780")])
        old_day = "2021-08-31"
        second = export_daily_snapshot(self.root, old_day,
            [master(day=old_day), master("56780", day=old_day, Mkt="0101")],
            [bar(day=old_day)], RETRIEVED)
        self.assertEqual(first["universe"]["count"], 1)
        self.assertEqual(second["universe"]["count"], 2)
        self.assertEqual(second["latest_session"]["diagnostics"]["ROW_MISSING"], 1)

    def test_acquisition_and_price_diagnostics_are_independent(self):
        nulls = dict.fromkeys(("O", "H", "L", "C", "Vo", "Va"))
        masters = [master(f"{n}0") for n in range(1234, 1238)]
        manifest = self.export(masters, [bar(), bar("12350", **nulls), bar("12360", C=None)])
        self.assertEqual(manifest["acquisition"]["row_coverage_ratio"], .75)
        self.assertEqual(manifest["latest_session"]["diagnostics"], {
            "VALID_PRICE": 1, "NO_EXECUTION": 1, "PARTIAL_NULL_OR_INVALID": 1, "ROW_MISSING": 1})
        self.assertEqual(manifest["latest_session"]["quote_count"], 1)
        self.assertEqual(manifest["universe"]["count"], 4)
        for bad in ({"Va": None}, {"Va": 0}, {"Vo": 0}, {"Va": "NaN"}):
            with self.subTest(bad=bad):
                result = self.export(bars=[bar(**bad)])
                self.assertEqual(result["latest_session"]["diagnostics"]["PARTIAL_NULL_OR_INVALID"], 1)
        missing = bar()
        del missing["Va"]
        self.assertEqual(self.export(bars=[missing])["latest_session"]["quote_count"], 0)

    def test_daily_population_regression_and_active_target_gate(self):
        # Synthetic population sizes match the privately checked daily partition.
        masters = [master(f"{n}0", Mkt="0105" if n >= 4689 else "0111")
                   for n in range(1000, 4880)]
        nulls = dict.fromkeys(("O", "H", "L", "C", "Vo", "Va"))
        bars = [bar(item["Code"], **({} if n < 4659 or n == 4689 else nulls))
                for n, item in zip(range(1000, 4880), masters)]
        manifest = self.export(masters, bars)
        self.assertEqual(manifest["universe"]["count"], 3689)
        self.assertEqual(manifest["latest_session"]["quote_count"], 3659)
        self.assertEqual(manifest["acquisition"]["row_coverage_ratio"], 1)
        evidence, failures = self.readiness({"1000"})
        self.assertEqual(failures, [])
        self.assertAlmostEqual(evidence["coverage_ratio"], 3659 / 3689)
        self.assertIn("J-Quants archive does not cover every active target", self.readiness({"4659"})[1])

    def test_legacy_scope_requires_regeneration_not_relabeling(self):
        manifest = self.export()
        manifest["universe"]["scope"] = "point-in-time domestic stocks"
        (self.archive / "latest.json").write_text(json.dumps(manifest))
        self.assertIsNone(self.readiness()[0])

    def test_adjusted_prices_are_never_used_as_missing_raw_fallback(self):
        manifest = self.export(bars=[{"Date": DAY, "Code": "12340", "AdjO": 100, "AdjH": 110,
                                      "AdjL": 90, "AdjC": 105, "AdjVo": 1200}])
        self.assertEqual(self.rows(manifest)[0]["終値"], "")
        self.assertEqual(manifest["latest_session"]["quote_count"], 0)
        self.assertIsNone(self.readiness()[0])

    def test_invalid_ohlcv_never_counted_as_valid_quote(self):
        for bad in ({"C": "NaN"}, {"Vo": -1}, {"Vo": 1.5}, {"O": True}, {"H": 104}, {"L": 106}):
            with self.subTest(bad=bad):
                manifest = self.export(bars=[bar(**bad)])
                self.assertEqual(self.rows(manifest)[0]["取得状態"], "FETCH_ERROR")
                self.assertEqual(manifest["latest_session"]["quote_count"], 0)
                self.assertIsNone(self.readiness()[0])

    def test_missing_product_category_and_wrong_date_fail_before_export(self):
        for masters, bars in (([master(Date="2026-09-01")], [bar()]),
                              ([master(ProdCat=None)], [bar()]),
                              ([master()], [bar(Date="2026-09-01")]),
                              ([master(), master()], [bar()]),
                              ([master()], [bar(), bar()]),
                              ([master()], [bar("56780")])):
            with self.subTest(masters=masters, bars=bars):
                with self.assertRaises(JQuantsSnapshotError):
                    self.export(masters, bars)
                self.assertFalse((self.archive / "latest.json").exists())

    def test_revisions_and_manifest_provenance_are_retained(self):
        first = self.export(raw_provenance={"bars": "v1"})
        first_path = self.archive / first["latest_session"]["file"]
        content = first_path.read_bytes()
        second = self.export(bars=[bar(C=106)], raw_provenance={"bars": "v2"})
        self.assertNotEqual(first["latest_session"]["file"], second["latest_session"]["file"])
        self.assertEqual(first_path.read_bytes(), content)
        self.assertEqual(self.rows(second)[0]["終値"], "106")
        third = self.export(bars=[bar(C=106)], raw_provenance={"bars": "v3"})
        self.assertEqual(second["latest_session"]["file"], third["latest_session"]["file"])
        revision_dir = (self.archive / third["latest_session"]["file"]).parent
        self.assertEqual(len(list(revision_dir.glob("manifest-*.json"))), 2)
        latest = json.loads((self.archive / "latest.json").read_text())
        self.assertEqual(latest["raw_provenance"], {"bars": "v3"})

    def test_historical_backfill_does_not_regress_latest_and_uses_historical_master(self):
        self.export()
        old_day = "2021-08-31"
        old = export_daily_snapshot(self.root, old_day, [master("56780", old_day, Mkt="0101")],
                                    [bar("56780", old_day)], RETRIEVED)
        self.assertEqual([row["銘柄コード"] for row in self.rows(old)], ["5678"])
        self.assertEqual(json.loads((self.archive / "latest.json").read_text())["latest_trading_date"], DAY)

    def test_revision_corruption_fails_and_preserves_latest(self):
        manifest = self.export()
        original_manifest = (self.archive / "latest.json").read_bytes()
        (self.archive / manifest["latest_session"]["file"]).write_text("corrupted")
        with self.assertRaisesRegex(JQuantsSnapshotError, "modified"):
            self.export()
        self.assertEqual((self.archive / "latest.json").read_bytes(), original_manifest)
        self.assertIn("J-Quants session checksum does not match", self.readiness()[1])

    def test_export_cannot_escape_private_archive_via_symlink(self):
        outside = self.root / "public"
        outside.mkdir()
        (self.archive / "revisions").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(JQuantsSnapshotError, "escapes"):
            self.export()
        self.assertEqual(list(outside.iterdir()), [])

    def test_jquants_rejects_yahoo_and_accepts_only_explicit_yahoo_config(self):
        write_price_archive(self.root, ["1234"], provider="yahoo_finance_unofficial_tracked_archive")
        self.assertIsNone(self.readiness()[0])
        legacy_config = copy.deepcopy(self.config)
        legacy_config["price_source"]["provider"] = "yahoo_finance_unofficial_tracked_archive"
        self.assertEqual(self.readiness(config=legacy_config)[1], [])
        self.assertIn("SECONDARY", self.readiness(config=legacy_config)[0]["decision_use"])
        del legacy_config["price_source"]["provider"]
        self.assertIsNone(self.readiness(config=legacy_config)[0])

    def test_readiness_rejects_wrong_units_basis_version_and_scope(self):
        for section, key, value in (("source", "price_basis", "adjusted"),
                                    ("source", "volume_unit", "lots"),
                                    ("source", "api_version", "v1"),
                                    ("universe", "scope", "current domestic stocks")):
            with self.subTest(key=key):
                manifest = self.export()
                manifest[section][key] = value
                (self.archive / "latest.json").write_text(json.dumps(manifest))
                self.assertIsNone(self.readiness()[0])

    def test_migration_activates_paid_provider_without_guessing_plan(self):
        legacy = copy.deepcopy(self.config)
        legacy["schema_version"] = "1.4"
        legacy["price_source"]["enabled"] = False
        legacy["price_source"]["provider"] = "yahoo_finance_unofficial_tracked_archive"
        legacy["jquants"].update({"enabled": False, "daily_bars_enabled": False,
                                   "base_url": "https://example.invalid", "api_key_env": "OTHER_KEY",
                                   "plan": "premium", "addons": ["tdnet"],
                                   "ingestion": {"enabled": False, "datasets": ["equities_daily"]}})
        migrated = _normalized_source_config(legacy, self.config)
        self.assertEqual(migrated["schema_version"], "1.5")
        self.assertEqual(migrated["price_source"]["provider"], "jquants_v2_private_archive")
        self.assertTrue(migrated["price_source"]["enabled"])
        self.assertTrue(migrated["jquants"]["enabled"])
        self.assertTrue(migrated["jquants"]["daily_bars_enabled"])
        self.assertEqual(migrated["jquants"]["base_url"], "https://api.jquants.com/v2")
        self.assertEqual(migrated["jquants"]["api_key_env"], "JQUANTS_API_KEY")
        self.assertIsNone(migrated["jquants"]["plan"])
        self.assertEqual(migrated["jquants"]["addons"], [])
        self.assertTrue(migrated["jquants"]["ingestion"]["enabled"])
        self.assertEqual(migrated["jquants"]["ingestion"]["datasets"], "all")
        self.assertEqual(_normalized_source_config(migrated, self.config), migrated)
        migrated["jquants"]["plan"] = "light"
        self.assertEqual(_normalized_source_config(migrated, self.config)["jquants"]["plan"], "light")


if __name__ == "__main__":
    unittest.main()
