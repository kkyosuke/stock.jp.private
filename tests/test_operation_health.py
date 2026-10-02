import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("operation_health", ROOT / "scripts/operation_health.py")
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class OperationHealthTest(unittest.TestCase):
    def prepare_root(self, root):
        (root / "stock.jp/scripts").mkdir(parents=True)
        private = root / "operations/private"
        archive = private / "market-data/daily-prices"
        archive.mkdir(parents=True)
        (archive / "latest.json").write_text('{"latest_trading_date":"2026-09-11"}')
        (private / "source-config.json").write_text(json.dumps({
            "price_source": {"manifest_path": "latest.json"},
        }))
        (root / "stock.jp/operations").mkdir()
        (root / "stock.jp/operations/private").symlink_to(private)
        # Storage boundary cases are tested in the public helper's own suite.
        # This fixture also exercises the health script's checkout-local import.
        (root / "stock.jp/scripts/market_data_storage.py").write_text(
            'def resolve_archive_root(root):\n'
            '    p=(root/"operations/private/market-data/daily-prices").resolve()\n'
            '    if not p.is_dir():\n'
            '        raise ValueError("private market-data archive is missing; no public fallback")\n'
            '    return p\n'
        )
        return private, archive

    def test_static_validation_does_not_read_or_require_market_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, archive = self.prepare_root(root)
            (root / "stock.jp/scripts/operation_state.py").write_text("print('{\"valid\": true}')\n")
            with patch.object(health, "ROOT", root), patch.object(health, "configured_archive", side_effect=AssertionError("raw data accessed")):
                self.assertEqual(health.command_json("operation_state.py", "2026-10-01T21:00:00+09:00", "validate", include_market_data=False), {"valid": True})
                with self.assertRaisesRegex(ValueError, "only for static"):
                    health.command_json("operation_bootstrap.py", "2026-10-01T21:00:00+09:00", include_market_data=False)

    def test_retired_workflow_history_is_not_required(self):
        def api(path):
            self.assertNotIn("actions/workflows", path)
            return {"sha": "same"}
        with patch.object(health, "github", side_effect=api), patch.object(health.subprocess, "check_output", return_value="same\n"):
            self.assertEqual(health.remote_alerts("owner/private", health.parse_time("2026-09-11T21:00:00+09:00")), [])

    def test_pointer_drift_requires_review(self):
        def api(path):
            return {"sha": "public", "commit": {"committer": {"date": "2026-09-01T00:00:00Z"}}} if "commits/main" in path else {"sha": "private"}
        with patch.object(health, "github", side_effect=api), patch.object(health.subprocess, "check_output", return_value="same\n"):
            self.assertTrue(health.remote_alerts("owner/private", health.parse_time("2026-09-11T21:00:00+09:00")))

    def test_successful_data_sync_does_not_hide_missed_research(self):
        report = health.local_report(
            {"last_run_id": "old", "data_gaps": [{"status": "OPEN"}]},
            {"status": "MISSED", "alerts": ["nightly run overdue"]},
            {"paper_go": True, "broker_submission": "HUMAN_ONLY", "status": "READY_WITH_WARNINGS"},
        )
        self.assertIn("nightly: MISSED", report["alerts"])
        self.assertEqual(report["open_data_gap_count"], 1)

    def test_invalid_readiness_and_submission_mode_fail_closed(self):
        report = health.local_report({}, {"status": "OK"}, {"paper_go": False, "blockers": ["stale prices"]})
        self.assertIn("stale prices", report["alerts"])
        self.assertIn("broker submission boundary must be HUMAN_ONLY", report["alerts"])

    def test_validation_isolated_and_migrations_rejected(self):
        for script in ("operation_bootstrap.py", "operation_watchdog.py", "operation_state.py"):
            with self.subTest(script=script), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                private, archive = self.prepare_root(root)
                (private / "state.json").write_text('{"original":true}')
                (root / "stock.jp/scripts" / script).write_text(
                    'from pathlib import Path\n'
                    'p=Path(__file__).resolve().parents[1]/"operations/private/state.json"\n'
                    'p.write_text("{}")\nprint("{}")\n'
                )
                with patch.object(health, "ROOT", root), self.assertRaisesRegex(RuntimeError, "requires migration"):
                    health.command_json(script, "2026-09-11T21:00:00+09:00", "validate" if script == "operation_state.py" else "check")
                self.assertEqual((private / "state.json").read_text(), '{"original":true}')

    def test_readonly_validator_can_return_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare_root(root)
            (root / "stock.jp/scripts/check.py").write_text('print(\'{"paper_go":true}\')')
            with patch.object(health, "ROOT", root):
                self.assertEqual(health.command_json("check.py", "2026-09-11T21:00:00+09:00"), {"paper_go": True})

    def test_only_configured_private_archive_is_copied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, archive = self.prepare_root(root)
            (archive / "synthetic.csv").write_text("code,close\nSYNTHETIC,100\n")
            (private / "market-data/unrelated").mkdir()
            (private / "market-data/unrelated/sentinel").write_text("do not copy")
            (private / "market-data-staging").mkdir()
            (private / "market-data-staging/sentinel").write_text("do not copy")
            (private / "historical-replay/raw").mkdir(parents=True)
            (private / "historical-replay/raw/sentinel").write_text("do not copy")
            (root / "stock.jp/data/daily-prices").mkdir(parents=True)
            (root / "stock.jp/data/daily-prices/sentinel").write_text("do not copy")
            (root / "stock.jp/scripts/check.py").write_text(
                'from pathlib import Path\n'
                'root=Path(__file__).resolve().parents[1]\n'
                'archive=root/"operations/private/market-data/daily-prices"\n'
                'assert (archive/"synthetic.csv").is_file()\n'
                'assert not archive.is_symlink()\n'
                'assert not (root/"operations/private/market-data/unrelated").exists()\n'
                'assert not (root/"operations/private/market-data-staging").exists()\n'
                'assert not (root/"operations/private/historical-replay/raw").exists()\n'
                'assert not (root/"data/daily-prices").exists()\n'
                'print(\'{"paper_go":true}\')\n'
            )
            with patch.object(health, "ROOT", root):
                self.assertTrue(health.command_json("check.py", "2026-09-11T21:00:00+09:00")["paper_go"])
            self.assertEqual((archive / "synthetic.csv").read_text(), "code,close\nSYNTHETIC,100\n")

    def test_validator_cannot_modify_canonical_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, archive = self.prepare_root(root)
            before = (archive / "latest.json").read_bytes()
            (root / "stock.jp/scripts/check.py").write_text(
                'from pathlib import Path\n'
                'p=Path(__file__).resolve().parents[1]/"operations/private/market-data/daily-prices/latest.json"\n'
                'p.write_text("{}")\nprint("{}")\n'
            )
            with patch.object(health, "ROOT", root), self.assertRaisesRegex(RuntimeError, "changed isolated market data"):
                health.command_json("check.py", "2026-09-11T21:00:00+09:00")
            self.assertEqual((archive / "latest.json").read_bytes(), before)

    def test_archive_symlinks_are_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, archive = self.prepare_root(root)
            (archive / "unsafe.csv").symlink_to(private / "source-config.json")
            with patch.object(health, "ROOT", root), self.assertRaisesRegex(ValueError, "unsafe entry"):
                health.command_json("check.py", "2026-09-11T21:00:00+09:00")

    def test_missing_private_archive_cannot_use_public_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, archive = self.prepare_root(root)
            (archive / "latest.json").unlink()
            archive.rmdir()
            (private / "state.json").write_text("{}")
            public_archive = root / "stock.jp/data/daily-prices"
            public_archive.mkdir(parents=True)
            (public_archive / "latest.json").write_text('{"latest_trading_date":"2099-01-01"}')
            output = root / "health.json"
            with patch.object(health, "ROOT", root), patch("sys.argv", [
                "operation_health.py", "--offline", "--output", str(output),
            ]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(health.main(), 1)
            report = json.loads(output.read_text())
            self.assertEqual(report["status"], "ATTENTION")
            self.assertIn("no public fallback", report["alerts"][0])
            self.assertNotIn("price_date", report)

    def test_manifest_is_private_and_cannot_escape_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, archive = self.prepare_root(root)
            with patch.object(health, "ROOT", root):
                self.assertEqual(health.private_price_manifest()["latest_trading_date"], "2026-09-11")
                (archive / "latest.json").unlink()
                (archive / "latest.json").symlink_to(private / "source-config.json")
                with self.assertRaisesRegex(ValueError, "escapes"):
                    health.private_price_manifest()

    def test_health_workflow_does_not_mask_failures(self):
        workflow = (ROOT / ".github/workflows/operation-health.yml").read_text()
        self.assertIn("python scripts/operation_health.py --persisted-only --output", workflow)
        self.assertNotIn("--report-only", workflow)

    def test_naive_time_is_rejected(self):
        with self.assertRaises(ValueError):
            health.parse_time("2026-09-11T21:00:00")

    def test_health_report_only_does_not_change_nightly_readiness(self):
        self.assertIn("--report-only", health.main.__code__.co_consts)


if __name__ == "__main__":
    unittest.main()
