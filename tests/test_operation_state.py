from tests.operation_test_support import prepare_write
from stock_jp.workspace import record_path
import csv
import json
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from stock_jp.operation.state import PROJECT_ROOT, initialize_or_migrate_workspace, validate_workspace

LEGACY_PORTFOLIO_HEADER = (
    "as_of_jst,code,company,status,entry_date,average_cost,position_cost_pct,"
    "position_market_pct,q0_normalized,current_fraction,score,market_space_score,"
    "reverse_score,thesis_status,kpi_status,last_quarterly_review,next_review,"
    "active_rule_ids,private_log_path\n"
)


class OperationStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "operations").mkdir(parents=True)
        shutil.copytree(
            PROJECT_ROOT / 'templates',
            self.root / 'templates',
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_initializes_v2_ledgers_and_secure_permissions(self) -> None:
        result = initialize_or_migrate_workspace(self.root)
        private = self.root / 'operations'
        state = json.loads((record_path(private.parent, 'state.json')).read_text(encoding="utf-8"))

        self.assertEqual(result["schema_version"], "2.1")
        self.assertEqual(state["schema_version"], "2.1")
        for filename in (
            "trade-event-ledger.csv",
            "recovered-capital-ledger.csv",
            "capital-ledger.csv",
            "corporate-actions.csv",
            "forecast-history.csv",
            "share-count-history.csv",
            "earnings-calendar-history.csv",
            "rebuy-restrictions.csv",
            "industry-exposure.csv",
        ):
            self.assertTrue((record_path(private.parent, filename)).is_file())
        if os.name == "posix":
            self.assertEqual(private.stat().st_mode & 0o777, 0o700)
            self.assertEqual(record_path(private.parent, 'state.json').stat().st_mode & 511, 0o600)
        self.assertEqual(validate_workspace(self.root), [])

    def test_migrates_legacy_state_and_portfolio_without_losing_values(self) -> None:
        private = self.root / 'operations'
        private.mkdir(exist_ok=True, parents=True)
        (record_path(private.parent, 'runs')).mkdir(parents=True)
        legacy_state = {
            "schema_version": "1.0",
            "timezone": "Asia/Tokyo",
            "schedule": "weekdays 18:30",
            "last_successful_run_at_jst": "2026-08-28T19:00:00+09:00",
            "last_disclosure_cutoff_jst": "2026-08-28T18:30:00+09:00",
            "last_price_date": "2026-08-28",
            "last_run_id": "2026-08-28",
            "pending_reviews": ["1234-quarterly"],
            "pending_orders": ["ticket-1"],
            "data_gaps": [],
            "next_run_at_jst": "2026-08-31T18:30:00+09:00",
            "last_backup_at_jst": "2026-08-28T20:00:00+09:00",
            "last_backup_path": 'operations/backups/legacy.zip.age',
            "last_backup_sha256": "legacy",
            "last_backup_verified_before_encryption": True,
        }
        (prepare_write(record_path(private.parent, 'state.json'))).write_text(
            json.dumps(legacy_state, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        portfolio_row = (
            '2026-08-28T18:30:00+09:00,1234,Example,OPEN,2026-01-05,1000,1.0,1.2,1.0,1.0,75,10,12,VALID,ON_TRACK,2026-08-01,2026-11-01,E-1,operations/decisions/example.md\n'
        )
        (prepare_write(record_path(private.parent, 'portfolio-register.csv'))).write_text(
            LEGACY_PORTFOLIO_HEADER + portfolio_row, encoding="utf-8"
        )

        first = initialize_or_migrate_workspace(self.root)
        second = initialize_or_migrate_workspace(self.root)
        state = json.loads((record_path(private.parent, 'state.json')).read_text(encoding="utf-8"))
        with (record_path(private.parent, 'portfolio-register.csv')).open(
            encoding="utf-8", newline=""
        ) as source:
            positions = list(csv.DictReader(source))
        with (record_path(private.parent, 'schema-migration-log.csv')).open(
            encoding="utf-8", newline=""
        ) as source:
            migrations = list(csv.DictReader(source))

        self.assertEqual(state["schema_version"], "2.1")
        self.assertNotIn("last_backup_at_jst", state)
        self.assertNotIn("last_backup_path", state)
        self.assertNotIn("last_backup_sha256", state)
        self.assertNotIn("last_backup_verified_before_encryption", state)
        self.assertEqual(state["pending_orders"], ["ticket-1"])
        self.assertEqual(state["unreconciled_ticket_ids"], ["ticket-1"])
        self.assertEqual(positions[0]["code"], "1234")
        self.assertEqual(positions[0]["average_cost"], "1000")
        self.assertEqual(positions[0]["five_x_taken"], "")
        self.assertIn('operations/state.json', first["migrated"])
        self.assertIsNotNone(first["migration_snapshot_dir"])
        self.assertEqual(second["migrated"], [])
        self.assertEqual(len(migrations), 1)

    def test_rejects_duplicate_ledger_ids_and_invalid_position_state(self) -> None:
        initialize_or_migrate_workspace(self.root)
        private = self.root / 'operations'
        portfolio = record_path(private.parent, 'portfolio-register.csv')
        with portfolio.open(encoding="utf-8", newline="") as source:
            fields = next(csv.reader(source))
        invalid_position = dict.fromkeys(fields, "")
        invalid_position.update(
            {
                "code": "1234",
                "five_x_taken": "maybe",
                "sb_consecutive_quarters": "-1",
                "corporate_action_factor": "0",
            }
        )
        with prepare_write(portfolio).open("a", encoding="utf-8", newline="") as destination:
            csv.DictWriter(destination, fieldnames=fields).writerow(invalid_position)

        ledger = record_path(private.parent, 'trade-event-ledger.csv')
        with ledger.open(encoding="utf-8", newline="") as source:
            ledger_fields = next(csv.reader(source))
        event = dict.fromkeys(ledger_fields, "")
        event["event_id"] = "event-1"
        with prepare_write(ledger).open("a", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=ledger_fields)
            writer.writerow(event)
            writer.writerow(event)

        errors = validate_workspace(self.root)

        self.assertIn("1234: five_x_taken must be boolean", errors)
        self.assertIn("1234: sb_consecutive_quarters must be >= 0", errors)
        self.assertIn("1234: corporate_action_factor must be > 0", errors)
        self.assertIn("duplicate trade-event-ledger.csv event_id: event-1", errors)

    def test_migrates_legacy_source_config_and_records_its_schema(self) -> None:
        initialize_or_migrate_workspace(self.root)
        private = self.root / 'operations'
        config_path = record_path(private.parent, 'sources.json')
        legacy = {
            "schema_version": "1.0",
            "jquants": {"enabled": True},
            "price_source": {
                "provider": "yahoo_finance_unofficial",
                "base_urls": ["https://query1.finance.yahoo.com"],
                "minimum_daily_archive_coverage": 0.5,
                "minimum_active_target_coverage": 0.5,
                "maximum_latest_price_age_days": 30,
            },
            "market_regime": {
                "minimum_vi_observations": 1,
                "breadth_lookback_sessions": 200,
                "maximum_breadth_close_age_days": 30,
                "minimum_breadth_coverage": 0.5,
            },
        }
        prepare_write(config_path).write_text(json.dumps(legacy) + "\n", encoding="utf-8")

        result = initialize_or_migrate_workspace(self.root)
        migrated = json.loads(config_path.read_text(encoding="utf-8"))
        with (record_path(private.parent, 'schema-migration-log.csv')).open(
            encoding="utf-8", newline=""
        ) as source:
            entries = list(csv.DictReader(source))

        self.assertIn('config/sources.json', result["migrated"])
        self.assertEqual(migrated["schema_version"], "1.5")
        self.assertEqual(
            migrated["price_source"]["provider"],
            "jquants_v2_private_archive",
        )
        self.assertTrue(migrated["jquants"]["enabled"])
        self.assertEqual(
            migrated["jquants"]["base_url"], "https://api.jquants.com/v2"
        )
        self.assertTrue(migrated["jpx_listed_master"]["enabled"])
        self.assertNotIn("base_urls", migrated["price_source"])
        self.assertEqual(
            migrated["price_source"]["minimum_daily_archive_coverage"], 0.98
        )
        self.assertEqual(
            migrated["price_source"]["minimum_active_target_coverage"], 1.0
        )
        self.assertEqual(
            migrated["price_source"]["maximum_latest_price_age_days"], 7
        )
        self.assertEqual(migrated["market_regime"]["minimum_vi_observations"], 500)
        self.assertEqual(migrated["market_regime"]["breadth_lookback_sessions"], 280)
        self.assertEqual(
            migrated["market_regime"]["maximum_breadth_close_age_days"], 7
        )
        self.assertEqual(
            migrated["market_regime"]["minimum_breadth_coverage"], 0.98
        )
        self.assertEqual(entries[-1]["from_schema"], "1.0")
        self.assertEqual(entries[-1]["to_schema"], "1.5")

    def test_migrates_legacy_migration_log_snapshot_column(self) -> None:
        initialize_or_migrate_workspace(self.root)
        private = self.root / 'operations'
        migration_log = record_path(private.parent, 'schema-migration-log.csv')
        prepare_write(migration_log).write_text(
            'migrated_at_jst,from_schema,to_schema,changed_paths,backup_dir,result,notes\n2026-08-31T00:00:00+09:00,1.0,2.0,state.json,operations/migrations/legacy,SUCCESS,legacy migration\n',
            encoding="utf-8",
        )

        result = initialize_or_migrate_workspace(self.root)
        with migration_log.open(encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))

        self.assertIn(
            'operations/ledgers/schema-migration-log.csv', result["migrated"]
        )
        self.assertEqual(
            rows[0]["migration_snapshot_dir"],
            'operations/migrations/legacy',
        )

    def test_retires_elapsed_time_gates_without_changing_other_decisions(self) -> None:
        initialize_or_migrate_workspace(self.root)
        private = self.root / 'operations'
        policy_path = record_path(private.parent, 'policy.json')
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["schema_version"] = "1.2"
        policy["live_gates"]["minimum_12_month_paper_trade"] = True
        policy["live_gate_evidence"]["minimum_12_month_paper_trade"] = (
            'operations/evidence/paper-12-months.json'
        )
        policy["live_gates"]["twenty_day_shadow_run"] = True
        policy["live_gate_evidence"]["twenty_day_shadow_run"] = (
            'operations/evidence/shadow-20-days.json'
        )
        policy["live_gates"]["historical_replay_2025_2026_accepted"] = True
        policy["live_gate_evidence"]["historical_replay_2025_2026_accepted"] = (
            'operations/evidence/replay.md'
        )
        prepare_write(policy_path).write_text(json.dumps(policy) + "\n", encoding="utf-8")

        result = initialize_or_migrate_workspace(self.root)
        migrated = json.loads(policy_path.read_text(encoding="utf-8"))

        self.assertIn('config/policy.json', result["migrated"])
        self.assertEqual(migrated["schema_version"], "1.5")
        self.assertNotIn("minimum_12_month_paper_trade", migrated["live_gates"])
        self.assertNotIn("twenty_day_shadow_run", migrated["live_gates"])
        self.assertNotIn(
            "minimum_12_month_paper_trade", migrated["live_gate_evidence"]
        )
        self.assertNotIn("twenty_day_shadow_run", migrated["live_gate_evidence"])
        self.assertTrue(
            migrated["live_gates"]["historical_replay_2025_2026_accepted"]
        )

    def test_migrates_legacy_policy_schema_without_promoting_v04(self) -> None:
        initialize_or_migrate_workspace(self.root)
        private = self.root / 'operations'
        policy_path = record_path(private.parent, 'policy.json')
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["schema_version"] = "1.0"
        policy["active_rule_version"] = "v0.2"
        policy["shadow_rule_versions"] = ["v0.3"]
        policy.pop("v04_holdout_promotion")
        prepare_write(policy_path).write_text(json.dumps(policy) + "\n", encoding="utf-8")

        result = initialize_or_migrate_workspace(self.root)
        migrated = json.loads(policy_path.read_text(encoding="utf-8"))

        self.assertIn('config/policy.json', result["migrated"])
        self.assertEqual(migrated["schema_version"], "1.5")
        self.assertEqual(migrated["active_rule_version"], "v0.2")
        self.assertFalse(migrated["v04_holdout_promotion"])
        self.assertIsNone(migrated["limited_live"]["capital_limit_jpy"])

    def test_initialization_does_not_reintroduce_waived_recovery_requirements(self) -> None:
        initialize_or_migrate_workspace(self.root)
        private = self.root / 'operations'
        policy_path = record_path(private.parent, 'policy.json')
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["schema_version"] = "1.1"
        policy["live_gates"]["private_repository_recovery"] = False
        policy["live_gate_evidence"]["private_repository_recovery"] = None
        policy["live_gates"]["backup_restore_drill"] = True
        policy["live_gate_evidence"]["backup_restore_drill"] = (
            'operations/evidence/recovery.md'
        )
        prepare_write(policy_path).write_text(json.dumps(policy) + "\n", encoding="utf-8")

        initialize_or_migrate_workspace(self.root)
        migrated = json.loads(policy_path.read_text(encoding="utf-8"))

        self.assertEqual(migrated["schema_version"], "1.5")
        self.assertNotIn("private_repository_recovery", migrated["live_gates"])
        self.assertNotIn("private_repository_recovery", migrated["live_gate_evidence"])
        self.assertNotIn("backup_restore_drill", migrated["live_gates"])
        self.assertNotIn("backup_restore_drill", migrated["live_gate_evidence"])


if __name__ == "__main__":
    unittest.main()
