from __future__ import annotations
from tests.operation_test_support import prepare_write
from stock_jp.workspace import project_root, record_path

import hashlib
import json
import csv
from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stock_jp.operation.evidence import evaluate_historical_replay, evaluate_official_coverage, evaluate_paper_duration, evaluate_personal_risk, evaluate_point_in_time, evaluate_repository_recovery, evaluate_shadow_run, evaluate_v04_promotion, evaluate_live_promotion, apply_live_promotion, validate_promoted_evidence_bundle, LIVE_REQUIREMENT_EVIDENCE, REPOSITORY_LAYOUT_REVISION, write_private_evidence


def _write(path: Path, value: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    prepare_write(path).write_text(value, encoding="utf-8")
    return hashlib.sha256(value.encode()).hexdigest()


def _valid_point_manifest(root: Path) -> Path:
    artifacts = []
    for role in ("source_snapshot", "trade_log", "metrics"):
        relative = f"data/replay/{role}.json"
        digest = _write(root / relative, f'{{"role":"{role}"}}\n')
        artifacts.append({"role": role, "path": relative, "sha256": digest})
    manifest = {
        "schema_version": "1.0",
        "status": "COMPLETED",
        "generated_at_jst": "2026-09-01T20:00:00+09:00",
        "as_of_date": "2026-08-31",
        "universe": {
            "required_count": 3800,
            "evaluated_count": 3800,
            "point_in_time_security_master": True,
            "includes_delisted": True,
            "includes_mergers": True,
            "includes_corporate_actions": True,
        },
        "quality": {
            "missing_hard_gate_inputs": 0,
            "lookahead_violations": 0,
            "required_disclosure_reviews": 12,
            "completed_disclosure_reviews": 12,
        },
        "artifacts": artifacts,
    }
    path = root / "research/historical-replay-2025-2026/output/point-in-time-validation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    prepare_write(path).write_text(json.dumps(manifest), encoding="utf-8")
    return path


class PointInTimeEvidenceTest(unittest.TestCase):

    def test_missing_manifest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = evaluate_point_in_time(root=Path(directory))
        self.assertFalse(result["eligible"])
        self.assertIn("manifest is missing", result["blockers"][0])

    def test_complete_hashed_full_universe_is_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _valid_point_manifest(root)
            result = evaluate_point_in_time(root=root)
        self.assertTrue(result["eligible"], result["blockers"])
        self.assertEqual(result["metrics"]["verified_artifact_count"], 3)

    def test_artifact_tampering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _valid_point_manifest(root)
            (prepare_write(root / "data/replay/trade_log.json")).write_text("tampered\n")
            result = evaluate_point_in_time(root=root)
        self.assertFalse(result["eligible"])
        self.assertIn(
            "artifact hash mismatch: data/replay/trade_log.json",
            result["blockers"],
        )

    def test_manifest_outside_project_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            container = Path(directory)
            root = container / "project"
            root.mkdir(parents=True)
            manifest = container / "manifest.json"
            prepare_write(manifest).write_text("{}", encoding="utf-8")
            result = evaluate_point_in_time(root=root, manifest_path=manifest)
        self.assertFalse(result["eligible"])
        self.assertEqual(
            result["blockers"],
            ['manifest path must stay under project or operations'],
        )

    def test_future_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = _valid_point_manifest(root)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["generated_at_jst"] = "2999-01-01T20:00:00+09:00"
            manifest["as_of_date"] = "2999-01-01"
            prepare_write(manifest_path).write_text(json.dumps(manifest), encoding="utf-8")
            result = evaluate_point_in_time(root=root)
        self.assertFalse(result["eligible"])
        self.assertIn("generated_at_jst cannot be in the future", result["blockers"])
        self.assertIn("as_of_date cannot be in the future", result["blockers"])

    def test_only_successful_private_evidence_can_be_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _valid_point_manifest(root)
            result = evaluate_point_in_time(root=root)
            target = root / 'operations/evidence/point-in-time.json'
            write_private_evidence(root=root, path=target, result=result)
            self.assertTrue(target.is_file())
            with self.assertRaisesRegex(ValueError, "must be under"):
                write_private_evidence(
                    root=root, path=root / "public.json", result=result
                )
            result["eligible"] = False
            with self.assertRaisesRegex(ValueError, "ineligible"):
                write_private_evidence(root=root, path=target, result=result)


class HistoricalReplayEvidenceTest(unittest.TestCase):
    def _valid_replay(self, root: Path) -> tuple[Path, Path]:
        point_manifest = _valid_point_manifest(root)
        artifacts = []
        for role in ("trade_log", "metrics", "monthly_returns"):
            relative = f"data/replay/final-{role}.csv"
            digest = _write(root / relative, f"{role}\n")
            artifacts.append({"role": role, "path": relative, "sha256": digest})
        result = {
            "schema_version": "1.0",
            "status": "COMPLETED",
            "rule_version": "v0.4",
            "evaluation_kind": "RETROSPECTIVE_STRESS_TEST",
            "holdout_claimed": False,
            "generated_at_jst": "2026-09-01T20:00:00+09:00",
            "period": {"from": "2025-01-01", "through": "2026-08-31"},
            "point_in_time_manifest_sha256": hashlib.sha256(
                point_manifest.read_bytes()
            ).hexdigest(),
            "quality": {
                "missing_hard_gate_inputs": 0,
                "lookahead_violations": 0,
                "required_disclosure_reviews": 12,
                "completed_disclosure_reviews": 12,
            },
            "metrics": {
                "trade_count": 42,
                "total_return_pct": 8.1,
                "max_drawdown_pct": -12.5,
                "benchmark_return_pct": 5.0,
            },
            "comparisons": {
                "v0.2": {
                    "return_pct": 4.0,
                    "maximum_drawdown_pct": -8.0,
                    "maximum_single_name_loss_contribution_pct": -1.0,
                    "maximum_industry_loss_contribution_pct": -2.0,
                    "trade_count": 38,
                },
                "v0.4": {
                    "return_pct": 8.1,
                    "maximum_drawdown_pct": -12.5,
                    "maximum_single_name_loss_contribution_pct": -3.0,
                    "maximum_industry_loss_contribution_pct": -5.0,
                    "trade_count": 42,
                },
            },
            "artifacts": artifacts,
        }
        result_path = root / "research/historical-replay-2025-2026/output/replay-result-2025-2026.json"
        prepare_write(result_path).write_text(json.dumps(result), encoding="utf-8")
        review = {
            "schema_version": "1.1",
            "decision": "ACCEPT",
            "rule_version": "v0.4",
            "accepted_by": "portfolio-owner",
            "accepted_at_jst": "2026-09-02T08:00:00+09:00",
            "replay_result_sha256": hashlib.sha256(
                result_path.read_bytes()
            ).hexdigest(),
            "drawdown_reviewed": True,
            "concentration_loss_reviewed": True,
            "data_limitations_reviewed": True,
            "accepted_v04_metrics": result["comparisons"]["v0.4"],
        }
        review_path = root / 'operations/evidence/historical-replay-review.json'
        review_path.parent.mkdir(parents=True)
        prepare_write(review_path).write_text(json.dumps(review), encoding="utf-8")
        return result_path, review_path

    def test_complete_replay_and_bound_human_review_are_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._valid_replay(root)
            result = evaluate_historical_replay(root=root)
        self.assertTrue(result["eligible"], result["blockers"])
        self.assertEqual(result["metrics"]["trade_count"], 42)

    def test_replay_without_private_acceptance_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, review_path = self._valid_replay(root)
            review_path.unlink()
            result = evaluate_historical_replay(root=root)
        self.assertFalse(result["eligible"])
        self.assertTrue(
            any("private replay review is missing" in item for item in result["blockers"])
        )

    def test_review_is_invalid_after_replay_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result_path, _ = self._valid_replay(root)
            replay = json.loads(result_path.read_text(encoding="utf-8"))
            replay["metrics"]["max_drawdown_pct"] = -30.0
            prepare_write(result_path).write_text(json.dumps(replay), encoding="utf-8")
            result = evaluate_historical_replay(root=root)
        self.assertFalse(result["eligible"])
        self.assertIn(
            "private review does not bind the current replay result",
            result["blockers"],
        )


class PaperDurationEvidenceTest(unittest.TestCase):
    FIELDS = [
        "run_id",
        "attempt",
        "started_at_jst",
        "completed_at_jst",
        "status",
        "operation_mode",
        "active_rule_version",
        "source_cutoff_jst",
        "price_date",
        "report_path",
        "order_count",
        "alert_count",
        "data_gap_count",
        "next_run_at_jst",
        "summary",
    ]

    def _write_history(self, root: Path, *, short: bool = False) -> Path:
        private = root / 'operations'
        private.mkdir(parents=True)
        _write(record_path(private.parent, 'sources.json'), json.dumps({"price_source": {"storage": "private_local", "archive_root": '.work/market-data/daily-prices'}}))
        rows = []
        for offset in range(13):
            year = 2025 + (8 + offset) // 12
            month = (8 + offset) % 12 + 1
            run_date = date(year, month, 1)
            if short and offset > 2:
                break
            run_id = run_date.isoformat()
            report = record_path(private.parent, f'runs/{run_id}/report.md')
            _write(report, f"# {run_id}\n")
            rows.append(
                {
                    "run_id": run_id,
                    "attempt": "1",
                    "started_at_jst": f"{run_id}T18:30:00+09:00",
                    "completed_at_jst": f"{run_id}T19:00:00+09:00",
                    "status": "COMPLETED",
                    "operation_mode": "PAPER",
                    "active_rule_version": "v0.4",
                    "source_cutoff_jst": f"{run_id}T18:30:00+09:00",
                    "price_date": run_id,
                    "report_path": f'operations/runs/{run_id}/report.md',
                    "order_count": "0",
                    "alert_count": "0",
                    "data_gap_count": "0",
                    "next_run_at_jst": "",
                    "summary": "test",
                }
            )
        history = record_path(private.parent, 'run-history.csv')
        with prepare_write(history).open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=self.FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return history

    def test_365_days_with_every_calendar_month_is_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_history(root)
            result = evaluate_paper_duration(root=root)
        self.assertTrue(result["eligible"], result["blockers"])
        self.assertEqual(result["metrics"]["elapsed_days"], 365)
        self.assertEqual(result["metrics"]["calendar_month_count"], 13)

    def test_short_paper_history_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_history(root, short=True)
            result = evaluate_paper_duration(root=root)
        self.assertFalse(result["eligible"])
        self.assertIn(
            "v0.4 PAPER elapsed duration is less than 365 days", result["blockers"]
        )

    def test_missing_report_is_not_counted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_history(root)
            (root / 'operations/runs/2025-12-01/report.md').unlink()
            result = evaluate_paper_duration(root=root)
        self.assertFalse(result["eligible"])
        self.assertIn("completed run report is missing: 2025-12-01", result["blockers"])

    def test_future_run_cannot_complete_the_duration_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_history(root)
            result = evaluate_paper_duration(
                root=root,
                at=datetime.fromisoformat("2026-08-31T23:00:00+09:00"),
            )
        self.assertFalse(result["eligible"])
        self.assertIn(
            "completed PAPER run cannot be in the future: 2026-09-01",
            result["blockers"],
        )

    def test_any_live_run_before_promotion_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = self._write_history(root)
            text = history.read_text(encoding="utf-8")
            prepare_write(history).write_text(text.replace(",PAPER,v0.4,", ",LIVE,v0.4,", 1))
            result = evaluate_paper_duration(root=root)
        self.assertFalse(result["eligible"])
        self.assertIn("pre-promotion LIVE run exists in run history", result["blockers"])


class ShadowRunEvidenceTest(unittest.TestCase):
    def _write_shadow(self, root: Path, *, count: int = 20) -> Path:
        private = root / 'operations'
        private.mkdir(parents=True)
        _write(record_path(private.parent, 'sources.json'), json.dumps({"price_source": {"storage": "private_local", "archive_root": '.work/market-data/daily-prices'}}))
        rows = []
        day = date(2026, 8, 1)
        trading_dates = []
        while len(trading_dates) < count:
            if day.weekday() < 5:
                trading_dates.append(day)
            day = date.fromordinal(day.toordinal() + 1)
        for run_date in trading_dates:
            run_id = run_date.isoformat()
            price = root / f'.work/market-data/daily-prices/{run_date.year}/{run_id}.csv'
            _write(price, "code,close\n1301,1000\n")
            run_dir = record_path(private.parent, 'runs') / run_id
            _write(run_dir / "report.md", f"# {run_id}\n")
            _write(run_dir / "orders.csv", "ticket_id,code,side,trade_date\n")
            rows.append(
                {
                    "run_id": run_id,
                    "attempt": "1",
                    "started_at_jst": f"{run_id}T18:30:00+09:00",
                    "completed_at_jst": f"{run_id}T19:00:00+09:00",
                    "status": "COMPLETED",
                    "operation_mode": "PAPER",
                    "active_rule_version": "v0.4",
                    "source_cutoff_jst": f"{run_id}T18:30:00+09:00",
                    "price_date": run_id,
                    "report_path": f'operations/runs/{run_id}/report.md',
                    "order_count": "0",
                    "alert_count": "0",
                    "data_gap_count": "0",
                    "next_run_at_jst": "",
                    "summary": "test",
                }
            )
        history = record_path(private.parent, 'run-history.csv')
        with prepare_write(history).open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(
                target, fieldnames=PaperDurationEvidenceTest.FIELDS
            )
            writer.writeheader()
            writer.writerows(rows)
        return history

    @patch('stock_jp.operation.evidence.validate_run_artifacts')
    def test_latest_20_tracked_sessions_are_eligible(self, validate) -> None:
        validate.return_value = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_shadow(root)
            result = evaluate_shadow_run(root=root)
        self.assertTrue(result["eligible"], result["blockers"])
        self.assertEqual(result["metrics"]["consecutive_session_count"], 20)
        self.assertEqual(validate.call_count, 20)

    @patch('stock_jp.operation.evidence.validate_run_artifacts')
    def test_nineteen_sessions_fail_closed(self, validate) -> None:
        validate.return_value = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_shadow(root, count=19)
            result = evaluate_shadow_run(root=root)
        self.assertFalse(result["eligible"])
        self.assertIn(
            "fewer than 20 completed v0.4 PAPER trading sessions", result["blockers"]
        )

    @patch('stock_jp.operation.evidence.validate_run_artifacts')
    def test_duplicate_ticket_across_runs_is_rejected(self, validate) -> None:
        validate.return_value = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = self._write_shadow(root)
            with history.open(encoding="utf-8", newline="") as source:
                rows = list(csv.DictReader(source))
            for row in rows[:2]:
                row["order_count"] = "1"
                order = root / f"operations/runs/{row['run_id']}/orders.csv"
                _write(
                    order,
                    "ticket_id,code,side,trade_date\n"
                    f"DUPLICATE,1301,BUY,{row['price_date']}\n",
                )
            with prepare_write(history).open("w", encoding="utf-8", newline="") as target:
                writer = csv.DictWriter(
                    target, fieldnames=PaperDurationEvidenceTest.FIELDS
                )
                writer.writeheader()
                writer.writerows(rows)
            result = evaluate_shadow_run(root=root)
        self.assertFalse(result["eligible"])
        self.assertIn(
            "duplicate ticket_id across shadow runs: DUPLICATE", result["blockers"]
        )

    @patch('stock_jp.operation.evidence.validate_run_artifacts')
    def test_shadow_window_must_end_at_latest_archive_session(self, validate) -> None:
        validate.return_value = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_shadow(root)
            _write(
                root / '.work/market-data/daily-prices/2026/2026-09-01.csv',
                "code,close\n1301,1001\n",
            )
            result = evaluate_shadow_run(root=root)
        self.assertFalse(result["eligible"])
        self.assertIn(
            "shadow window does not end at the latest tracked price session",
            result["blockers"],
        )


class OfficialCoverageEvidenceTest(unittest.TestCase):
    def _write_coverage(self, root: Path) -> list[str]:
        history = ShadowRunEvidenceTest()._write_shadow(root)
        with history.open(encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))
        for row in rows:
            run_id = row["run_id"]
            run_dir = root / f'operations/runs/{run_id}'
            coverage = {
                "schema_version": "1.0",
                "run_id": run_id,
                "status": "COMPLETED",
                "official_sources": {
                    "tdnet": {"required": True, "status": "CHECKED"},
                    "edinet": {"required": True, "status": "CHECKED"},
                    "company_ir": {"required": False, "status": "NOT_APPLICABLE"},
                    "jpx": {"required": True, "status": "CHECKED"},
                },
                "data_gaps": [],
            }
            health = {
                "schema_version": "1.0",
                "run_id": run_id,
                "input_mode": "LIVE_NETWORK",
                "status": "COMPLETED",
                "providers": {
                    "edinet": {"status": "OK", "request_count": 1}
                },
                "blocking_gaps": [],
            }
            _write(run_dir / "coverage.json", json.dumps(coverage))
            _write(run_dir / "provider-health.json", json.dumps(health))
            _write(
                run_dir / "sources.csv",
                "source_id,category,primary_source,used_for_decision,url\n"
                "tdnet-check,tdnet,true,true,https://www.release.tdnet.info/\n"
                "edinet-check,edinet,true,true,https://api.edinet-fsa.go.jp/\n"
                "jpx-check,jpx,true,true,https://www.jpx.co.jp/\n",
            )
        latest_cutoff = rows[-1]["source_cutoff_jst"]
        watermarks = {
            "sources": {
                name: {"last_successful_cutoff_jst": latest_cutoff}
                for name in ("tdnet", "edinet", "jpx")
            }
        }
        _write(
            root / 'operations/source-watermarks.json',
            json.dumps(watermarks),
        )
        return [row["run_id"] for row in rows]

    @patch('stock_jp.operation.evidence.validate_run_artifacts')
    def test_latest_live_network_official_check_is_eligible(self, validate) -> None:
        validate.return_value = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_coverage(root)
            result = evaluate_official_coverage(root=root)
        self.assertTrue(result["eligible"], result["blockers"])
        self.assertEqual(result["metrics"]["checked_run_counts"]["edinet"], 1)
        self.assertEqual(result["metrics"]["run_count"], 1)
        self.assertEqual(validate.call_count, 1)

    @patch('stock_jp.operation.evidence.validate_run_artifacts')
    def test_latest_run_integrity_failure_is_rejected(self, validate) -> None:
        validate.side_effect = ValueError("broken run")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_coverage(root)
            result = evaluate_official_coverage(root=root)
        self.assertFalse(result["eligible"])
        self.assertTrue(
            any("run integrity failed" in item for item in result["blockers"])
        )

    @patch('stock_jp.operation.evidence.validate_run_artifacts')
    def test_fixture_scan_is_rejected(self, validate) -> None:
        validate.return_value = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_ids = self._write_coverage(root)
            path = root / f'operations/runs/{run_ids[-1]}/provider-health.json'
            health = json.loads(path.read_text(encoding="utf-8"))
            health["input_mode"] = "FIXTURE"
            prepare_write(path).write_text(json.dumps(health), encoding="utf-8")
            result = evaluate_official_coverage(root=root)
        self.assertFalse(result["eligible"])
        self.assertTrue(any("not a live network scan" in item for item in result["blockers"]))

    @patch('stock_jp.operation.evidence.validate_run_artifacts')
    def test_non_primary_decision_source_is_rejected(self, validate) -> None:
        validate.return_value = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_ids = self._write_coverage(root)
            path = root / f'operations/runs/{run_ids[-1]}/sources.csv'
            text = path.read_text(encoding="utf-8").replace(
                "tdnet-check,tdnet,true,true", "tdnet-check,tdnet,false,true"
            )
            prepare_write(path).write_text(text, encoding="utf-8")
            result = evaluate_official_coverage(root=root)
        self.assertFalse(result["eligible"])
        self.assertTrue(any("non-primary tdnet" in item for item in result["blockers"]))


class RepositoryRecoveryEvidenceTest(unittest.TestCase):
    def _write_drill(self, root: Path) -> Path:
        commit = "1" * 40
        public_commit = "2" * 40
        drill = {
            "schema_version": "1.1",
            "drill_id": "pre-live-clean-clone",
            "operator": "portfolio-owner",
            "repository_visibility": "PRIVATE",
            "reason": "PRE_LIVE",
            "repository_layout_revision": REPOSITORY_LAYOUT_REVISION,
            "started_at_jst": "2026-08-31T20:00:00+09:00",
            "completed_at_jst": "2026-08-31T21:00:00+09:00",
            "source_private_commit": commit,
            "recovered_private_commit": commit,
            "source_code_tree": public_commit,
            "recovered_code_tree": public_commit,
            "checks": {
                name: True
                for name in (
                    "clean_clone_single_repository",
                    "workspace_setup",
                    "state_validation",
                    "paper_bootstrap_check",
                    "latest_successful_run",
                    "latest_handoff",
                    "all_ledgers",
                    "unreconciled_orders",
                )
            },
            "result_notes": "clean checkout reconciled for the current layout",
        }
        path = root / 'operations/evidence/recovery-drill.json'
        _write(path, json.dumps(drill))
        return path

    def test_legacy_boolean_only_recovery_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_drill(root)
            result = evaluate_repository_recovery(
                root=root,
                at=datetime.fromisoformat("2026-09-01T09:00:00+09:00"),
            )
        self.assertFalse(result["eligible"])
        self.assertTrue(any("provenance" in item for item in result["blockers"]))
        self.assertEqual(result["metrics"]["passed_check_count"], 8)

    def test_public_template_tracks_the_current_layout_revision(self) -> None:
        template = json.loads(
            (
                project_root() / 'templates/live-gate-evidence/recovery-drill-template.json'
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            template["repository_layout_revision"], REPOSITORY_LAYOUT_REVISION
        )
        self.assertNotIn("access_controlled_mirror_restore", template["checks"])
        self.assertNotIn("private_remote_restore", template["checks"])

    def test_clean_clone_does_not_expire_by_age(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_drill(root)
            result = evaluate_repository_recovery(
                root=root,
                at=datetime.fromisoformat("2026-12-15T09:00:00+09:00"),
            )
        self.assertFalse(result["eligible"])
        self.assertTrue(any("provenance" in item for item in result["blockers"]))

    def test_layout_revision_change_requires_a_new_clean_clone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._write_drill(root)
            drill = json.loads(path.read_text(encoding="utf-8"))
            drill["repository_layout_revision"] = REPOSITORY_LAYOUT_REVISION - 1
            prepare_write(path).write_text(json.dumps(drill), encoding="utf-8")
            result = evaluate_repository_recovery(
                root=root,
                at=datetime.fromisoformat("2026-09-01T09:00:00+09:00"),
            )
        self.assertFalse(result["eligible"])
        self.assertIn(
            "recovery repository_layout_revision does not match the current layout",
            result["blockers"],
        )

    def test_boolean_is_not_accepted_as_a_layout_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._write_drill(root)
            drill = json.loads(path.read_text(encoding="utf-8"))
            drill["repository_layout_revision"] = True
            prepare_write(path).write_text(json.dumps(drill), encoding="utf-8")
            result = evaluate_repository_recovery(
                root=root,
                at=datetime.fromisoformat("2026-09-01T09:00:00+09:00"),
            )
        self.assertFalse(result["eligible"])
        self.assertIn(
            "recovery repository_layout_revision does not match the current layout",
            result["blockers"],
        )

    def test_commit_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._write_drill(root)
            drill = json.loads(path.read_text(encoding="utf-8"))
            drill["recovered_private_commit"] = "3" * 40
            prepare_write(path).write_text(json.dumps(drill), encoding="utf-8")
            result = evaluate_repository_recovery(
                root=root,
                at=datetime.fromisoformat("2026-09-01T09:00:00+09:00"),
            )
        self.assertFalse(result["eligible"])
        self.assertIn("recovered private commit does not match source", result["blockers"])


class PersonalRiskEvidenceTest(unittest.TestCase):
    def _write_checklist(self, root: Path) -> Path:
        confirmations = {
            name: True
            for name in (
                "loss_does_not_affect_living",
                "living_tax_and_five_year_funds_excluded",
                "borrowed_funds_excluded",
                "emergency_fund_separate",
                "total_loss_stop_documented",
                "tax_residency_and_account_type_checked",
                "fees_tax_and_filing_obligations_checked",
                "cash_equity_and_trading_unit_checked",
                "limit_order_tick_and_validity_checked",
                "broker_rules_checked_from_official_source",
                "available_for_0845_0855_manual_check",
                "broker_submission_human_only",
                "no_automatic_broker_api",
                "correct_broker_url_bookmarked",
                "mfa_or_passkey_enabled",
                "login_and_trade_notifications_enabled",
                "phishing_and_account_lockout_procedure_checked",
                "emergency_cancel_and_pause_procedure_checked",
                "investment_can_lose_all_allocated_capital",
                "returns_are_not_guaranteed",
            )
        }
        checklist = {
            "schema_version": "1.0",
            "owner": "portfolio-owner",
            "reviewed_at_jst": "2026-09-01T08:00:00+09:00",
            "broker_name": "sample broker",
            "broker_rules_url": "https://broker.example/rules",
            "broker_rules_checked_at_jst": "2026-09-01T07:30:00+09:00",
            "confirmations": confirmations,
            "risk_limits_pct": {
                "maximum_total_loss_stop": 25.0,
                "maximum_single_name": 10.0,
                "maximum_industry": 20.0,
                "maximum_initial_purchase": 5.0,
                "maximum_additional_purchase": 2.5,
                "maximum_daily_participation": 10.0,
            },
            "emergency_contact_location": "offline emergency runbook",
            "review_notes": "broker and tax conditions reviewed",
        }
        path = root / 'operations/evidence/personal-risk-and-broker.json'
        _write(path, json.dumps(checklist))
        return path

    def test_complete_recent_personal_review_is_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_checklist(root)
            result = evaluate_personal_risk(
                root=root,
                at=datetime.fromisoformat("2026-09-01T09:00:00+09:00"),
            )
        self.assertTrue(result["eligible"], result["blockers"])
        self.assertEqual(result["metrics"]["confirmed_count"], 20)

    def test_personal_review_expires_after_90_days(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_checklist(root)
            result = evaluate_personal_risk(
                root=root,
                at=datetime.fromisoformat("2026-12-15T09:00:00+09:00"),
            )
        self.assertFalse(result["eligible"])
        self.assertIn(
            "personal risk and broker review is older than 90 days", result["blockers"]
        )

    def test_strategy_cap_cannot_be_relaxed_in_personal_checklist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._write_checklist(root)
            checklist = json.loads(path.read_text(encoding="utf-8"))
            checklist["risk_limits_pct"]["maximum_single_name"] = 15.0
            prepare_write(path).write_text(json.dumps(checklist), encoding="utf-8")
            result = evaluate_personal_risk(
                root=root,
                at=datetime.fromisoformat("2026-09-01T09:00:00+09:00"),
            )
        self.assertFalse(result["eligible"])
        self.assertIn(
            "personal risk limit maximum_single_name must be > 0 and <= 10",
            result["blockers"],
        )

    def test_broker_rules_cannot_be_checked_after_review_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._write_checklist(root)
            checklist = json.loads(path.read_text(encoding="utf-8"))
            checklist["broker_rules_checked_at_jst"] = "2026-09-01T08:30:00+09:00"
            prepare_write(path).write_text(json.dumps(checklist), encoding="utf-8")
            result = evaluate_personal_risk(
                root=root,
                at=datetime.fromisoformat("2026-09-01T09:00:00+09:00"),
            )
        self.assertFalse(result["eligible"])
        self.assertIn(
            "broker rules must be checked before completing the review",
            result["blockers"],
        )


class V04PromotionEvidenceTest(unittest.TestCase):
    def _write_promotion(self, root: Path) -> tuple[Path, Path, Path]:
        private = root / 'operations'
        period = {"from": "2026-10-01", "through": "2027-03-31"}
        criteria = {
            "minimum_monthly_evaluation_count": 6,
            "minimum_trade_count": 10,
            "maximum_drawdown_floor_pct": -30.0,
            "maximum_single_name_loss_floor_pct": -10.0,
            "maximum_industry_loss_floor_pct": -20.0,
        }
        plan = {
            "schema_version": "1.0",
            "status": "FROZEN",
            "decision": "START_FORWARD_HOLDOUT",
            "rule_version": "v0.4",
            "rule_frozen_at_jst": "2026-09-01T22:21:26+09:00",
            "frozen_at_jst": "2026-09-06T08:00:00+09:00",
            "declared_by": "portfolio-owner",
            "period": period,
            "acceptance_criteria": criteria,
            "acknowledgements": {
                "period_was_unobserved_when_frozen": True,
                "inputs_and_execution_rules_frozen": True,
                "thresholds_frozen_before_results": True,
                "changes_restart_the_holdout": True,
                "jquants_will_not_be_used": True,
            },
        }
        plan_path = record_path(private.parent, 'evidence/v04-holdout-plan.json')
        _write(plan_path, json.dumps(plan))
        replay = {
            "schema_version": "1.0",
            "status": "COMPLETED",
            "rule_version": "v0.4",
            "evaluation_kind": "FORWARD_HOLDOUT",
            "holdout_claimed": True,
            "generated_at_jst": "2027-04-01T21:00:00+09:00",
            "period": period,
            "quality": {
                "missing_hard_gate_inputs": 0,
                "lookahead_violations": 0,
                "required_disclosure_reviews": 6,
                "completed_disclosure_reviews": 6,
            },
            "holdout": {
                "predeclared": True,
                "plan_path": 'operations/evidence/v04-holdout-plan.json',
                "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                "plan_frozen_at_jst": "2026-09-06T08:00:00+09:00",
                "rule_frozen_at_jst": "2026-09-01T22:21:26+09:00",
                "retuning_count": 0,
                "acceptance_criteria": criteria,
                "observations": {"monthly_evaluation_count": 6, "trade_count": 20},
                "criteria_met": True,
                "criterion_failures": [],
            },
            "comparisons": {
                "v0.2": {
                    "return_pct": 4.0,
                    "maximum_drawdown_pct": -8.0,
                    "maximum_single_name_loss_contribution_pct": -1.0,
                    "maximum_industry_loss_contribution_pct": -2.0,
                    "trade_count": 20,
                },
                "v0.4": {
                    "return_pct": 8.0,
                    "maximum_drawdown_pct": -25.0,
                    "maximum_single_name_loss_contribution_pct": -7.0,
                    "maximum_industry_loss_contribution_pct": -12.0,
                    "trade_count": 20,
                },
            },
        }
        replay_path = (
            record_path(private.parent, 'historical-replay/v04-forward/output/replay-result-v04-forward-holdout.json')
        )
        _write(replay_path, json.dumps(replay))
        historical_path = record_path(private.parent, 'evidence/historical-replay.json')
        _write(
            historical_path,
            json.dumps(
                {
                    "schema_version": "1.0",
                    "gate": "historical_replay_2025_2026_accepted",
                    "evaluated_at_jst": "2026-09-06T09:00:00+09:00",
                    "eligible": True,
                    "blockers": [],
                }
            ),
        )
        review = {
            "schema_version": "1.1",
            "decision": "PROMOTE_V0_4_TO_LIVE",
            "rule_version": "v0.4",
            "approved_by": "portfolio-owner",
            "approved_at_jst": "2027-04-02T09:00:00+09:00",
            "replay_result_sha256": hashlib.sha256(replay_path.read_bytes()).hexdigest(),
            "historical_replay_evidence_sha256": hashlib.sha256(
                historical_path.read_bytes()
            ).hexdigest(),
            "holdout_plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            "acknowledgements": {
                name: True
                for name in (
                    "allocation_amplification_reviewed",
                    "maximum_drawdown_reviewed",
                    "single_name_concentration_loss_reviewed",
                    "industry_concentration_loss_reviewed",
                    "waiting_cash_is_not_safe_asset",
                    "no_holdout_retuning",
                    "holdout_chronology_reviewed",
                    "frozen_acceptance_criteria_reviewed",
                )
            },
            "accepted_v04_metrics": {
                name: replay["comparisons"]["v0.4"][name]
                for name in (
                    "maximum_drawdown_pct",
                    "maximum_single_name_loss_contribution_pct",
                    "maximum_industry_loss_contribution_pct",
                )
            },
        }
        review_path = record_path(private.parent, 'evidence/v04-holdout-review.json')
        _write(review_path, json.dumps(review))
        return replay_path, historical_path, review_path

    def test_bound_explicit_v04_promotion_is_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_promotion(root)
            result = evaluate_v04_promotion(
                root=root, at=datetime.fromisoformat("2027-04-03T09:00:00+09:00")
            )
        self.assertTrue(result["eligible"], result["blockers"])
        self.assertEqual(result["metrics"]["acknowledged_count"], 8)

    def test_changed_replay_invalidates_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            replay_path, _, _ = self._write_promotion(root)
            replay = json.loads(replay_path.read_text(encoding="utf-8"))
            replay["comparisons"]["v0.4"]["maximum_drawdown_pct"] = -40.0
            prepare_write(replay_path).write_text(json.dumps(replay), encoding="utf-8")
            result = evaluate_v04_promotion(
                root=root, at=datetime.fromisoformat("2027-04-03T09:00:00+09:00")
            )
        self.assertFalse(result["eligible"])
        self.assertIn("v0.4 review replay_result_sha256 does not match", result["blockers"])

    def test_promotion_does_not_require_duration_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_promotion(root)
            self.assertFalse(
                (root / 'operations/evidence/paper-12-months.json').exists()
            )
            result = evaluate_v04_promotion(
                root=root, at=datetime.fromisoformat("2027-04-03T09:00:00+09:00")
            )
        self.assertTrue(result["eligible"], result["blockers"])

    def test_unacknowledged_or_anonymous_holdout_plan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            replay_path, _, _ = self._write_promotion(root)
            plan_path = root / 'operations/evidence/v04-holdout-plan.json'
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["declared_by"] = None
            plan["acknowledgements"]["period_was_unobserved_when_frozen"] = False
            prepare_write(plan_path).write_text(json.dumps(plan), encoding="utf-8")
            replay = json.loads(replay_path.read_text(encoding="utf-8"))
            replay["holdout"]["plan_sha256"] = hashlib.sha256(
                plan_path.read_bytes()
            ).hexdigest()
            prepare_write(replay_path).write_text(json.dumps(replay), encoding="utf-8")
            result = evaluate_v04_promotion(
                root=root, at=datetime.fromisoformat("2027-04-03T09:00:00+09:00")
            )
        self.assertFalse(result["eligible"])
        self.assertIn("holdout plan declared_by is required", result["blockers"])
        self.assertIn(
            "holdout plan acknowledgement period_was_unobserved_when_frozen must be true",
            result["blockers"],
        )

    def test_future_holdout_result_plan_and_review_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_promotion(root)
            result = evaluate_v04_promotion(
                root=root, at=datetime.fromisoformat("2026-09-05T12:00:00+09:00")
            )
        self.assertFalse(result["eligible"])
        self.assertIn(
            "forward holdout result cannot be generated in the future",
            result["blockers"],
        )
        self.assertIn(
            "holdout plan frozen_at_jst cannot be in the future", result["blockers"]
        )
        self.assertIn(
            "v0.4 review approved_at_jst cannot be in the future", result["blockers"]
        )


class FinalLivePromotionTest(unittest.TestCase):
    def _write_bundle(self, root: Path) -> tuple[dict[str, dict], Path, Path]:
        private = root / 'operations'
        policy = json.loads(
            (project_root() / 'templates/policy.json')
            .read_text(encoding="utf-8")
        )
        policy_path = record_path(private.parent, 'policy.json')
        _write(policy_path, json.dumps(policy))
        fresh = {}
        for index, (gate, relative) in enumerate(LIVE_REQUIREMENT_EVIDENCE.items()):
            evidence = {
                "schema_version": "1.0",
                "gate": gate,
                "evaluated_at_jst": f"2026-09-01T08:{index:02d}:00+09:00",
                "eligible": True,
                "blockers": [],
                "metrics": {"index": index},
                "inputs": [
                    {
                        "role": "fixture",
                        "path": f"fixture-{index}.json",
                        "sha256": str(index) * 64,
                    }
                ],
            }
            fresh[gate] = evidence
            _write(root / relative, json.dumps(evidence))
        approval = {
            "schema_version": "1.0",
            "decision": "LIVE",
            "start_conditions_version": "readiness-v2-2026-10-07",
            "rule_version": "v0.4",
            "broker_submission": "HUMAN_ONLY",
            "approved_by": "portfolio-owner",
            "approved_at_jst": "2026-09-01T09:00:00+09:00",
            "private_commit_reviewed": "1" * 40,
            "code_tree_reviewed": "2" * 40,
            "pre_promotion_policy_sha256": hashlib.sha256(
                policy_path.read_bytes()
            ).hexdigest(),
            "evidence": {
                gate: {
                    "path": relative,
                    "sha256": hashlib.sha256((root / relative).read_bytes()).hexdigest(),
                }
                for gate, relative in LIVE_REQUIREMENT_EVIDENCE.items()
            },
            "acknowledgements": {
                name: True
                for name in (
                    "all_requirement_evidence_reviewed",
                    "first_live_order_requires_pretrade_check",
                    "human_only_submission",
                    "pause_on_any_blocking_gap",
                    "no_automatic_order_submission",
                    "live_decision_is_not_a_profit_guarantee",
                    "forward_performance_not_established_at_start",
                )
            },
        }
        approval_path = record_path(private.parent, 'evidence/live-approval.json')
        _write(approval_path, json.dumps(approval))
        return fresh, approval_path, policy_path

    def test_start_requirements_do_not_evaluate_forward_observation_counts(self):
        import stock_jp.operation.evidence as module
        with patch.object(module, 'evaluate_v04_promotion') as forward, \
             patch.object(module, 'evaluate_point_in_time', return_value={'eligible': False}), \
             patch.object(module, 'evaluate_historical_replay', return_value={'eligible': False}), \
             patch.object(module, 'evaluate_official_coverage', return_value={'eligible': False}), \
             patch.object(module, 'evaluate_personal_risk', return_value={'eligible': False}):
            results = module.evaluate_all_live_requirements(root=Path('/synthetic'))
        forward.assert_not_called()
        self.assertEqual(set(results), {
            'point_in_time_full_universe_validation', 'historical_replay_2025_2026_accepted',
            'official_source_coverage', 'personal_risk_and_broker_check'})
        self.assertTrue(all(item['eligible'] is False for item in results.values()))

    @patch('stock_jp.operation.evidence.evaluate_all_live_requirements')
    def test_old_approval_cannot_authorize_new_start_conditions(self, evaluate):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fresh, approval_path, policy_path = self._write_bundle(root)
            evaluate.return_value = fresh
            approval = json.loads(approval_path.read_text())
            approval.pop('start_conditions_version')
            _write(approval_path, json.dumps(approval))
            before = policy_path.read_bytes()
            result = evaluate_live_promotion(root=root)
            self.assertFalse(result['eligible'])
            self.assertIn('final approval start_conditions_version must match current start conditions',
                          result['blockers'])
            with self.assertRaises(ValueError):
                apply_live_promotion(root=root, result=result, approval_path=approval_path,
                                     policy_path=policy_path)
            self.assertEqual(before, policy_path.read_bytes())

    @patch('stock_jp.operation.evidence.evaluate_all_live_requirements')
    def test_unproven_forward_performance_requires_explicit_acknowledgement(self, evaluate):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fresh, approval_path, _ = self._write_bundle(root)
            evaluate.return_value = fresh
            approval = json.loads(approval_path.read_text())
            approval['acknowledgements']['forward_performance_not_established_at_start'] = False
            _write(approval_path, json.dumps(approval))
            result = evaluate_live_promotion(root=root)
            self.assertFalse(result['eligible'])
            self.assertIn('final approval acknowledgement forward_performance_not_established_at_start must be true',
                          result['blockers'])

    @patch('stock_jp.operation.evidence.evaluate_all_live_requirements')
    def test_all_bound_requirements_and_live_decision_are_eligible(self, evaluate) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fresh, approval_path, policy_path = self._write_bundle(root)
            evaluate.return_value = fresh
            result = evaluate_live_promotion(root=root)
        self.assertTrue(result["eligible"], result["blockers"])
        self.assertEqual(result["metrics"]["eligible_requirement_count"], 4)

    @patch('stock_jp.operation.evidence.evaluate_all_live_requirements')
    def test_tampered_stored_evidence_is_rejected(self, evaluate) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fresh, _, _ = self._write_bundle(root)
            path = root / LIVE_REQUIREMENT_EVIDENCE["official_source_coverage"]
            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["inputs"] = []
            prepare_write(path).write_text(json.dumps(stored), encoding="utf-8")
            evaluate.return_value = fresh
            result = evaluate_live_promotion(root=root)
        self.assertFalse(result["eligible"])
        self.assertIn(
            "stored evidence is stale or does not match: official_source_coverage",
            result["blockers"],
        )
        self.assertIn(
            "final approval evidence hash mismatch: official_source_coverage",
            result["blockers"],
        )

    @patch('stock_jp.operation.evidence.evaluate_all_live_requirements')
    def test_future_final_approval_is_rejected(self, evaluate) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fresh, approval_path, _ = self._write_bundle(root)
            approval = json.loads(approval_path.read_text(encoding="utf-8"))
            approval["approved_at_jst"] = "2999-01-01T09:00:00+09:00"
            prepare_write(approval_path).write_text(json.dumps(approval), encoding="utf-8")
            evaluate.return_value = fresh
            result = evaluate_live_promotion(root=root)
        self.assertFalse(result["eligible"])
        self.assertIn(
            "final approval approved_at_jst cannot be in the future",
            result["blockers"],
        )

    @patch('stock_jp.operation.evidence.evaluate_all_live_requirements')
    def test_apply_atomically_promotes_policy_and_typed_bundle_remains_valid(self, evaluate) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fresh, approval_path, policy_path = self._write_bundle(root)
            evaluate.return_value = fresh
            result = evaluate_live_promotion(root=root)
            promoted = apply_live_promotion(
                root=root,
                result=result,
                approval_path=approval_path,
                policy_path=policy_path,
            )
            persisted = json.loads(policy_path.read_text(encoding="utf-8"))
            failures = validate_promoted_evidence_bundle(root=root, policy=persisted)
        self.assertEqual(promoted["operation_mode"], "LIVE")
        self.assertEqual(persisted["broker_submission"], "HUMAN_ONLY")
        self.assertTrue(all(persisted["live_gates"].values()))
        self.assertFalse(persisted["v04_holdout_promotion"])
        self.assertEqual(failures, [])

    @patch('stock_jp.operation.evidence.evaluate_all_live_requirements')
    def test_limited_live_can_be_promoted_after_all_full_live_gates_pass(
        self, evaluate
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fresh, approval_path, policy_path = self._write_bundle(root)
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["operation_mode"] = "LIMITED_LIVE"
            policy["limited_live"].update(
                {
                    "capital_limit_jpy": 3_000_000,
                    "maximum_total_loss_pct": 10.0,
                    "approved_by": "portfolio-owner",
                    "approved_at_jst": "2026-09-01T08:30:00+09:00",
                    "evidence_path": (
                        'operations/evidence/limited-live-plan.json'
                    ),
                    "evidence_sha256": "a" * 64,
                }
            )
            prepare_write(policy_path).write_text(json.dumps(policy), encoding="utf-8")
            approval = json.loads(approval_path.read_text(encoding="utf-8"))
            approval["pre_promotion_policy_sha256"] = hashlib.sha256(
                policy_path.read_bytes()
            ).hexdigest()
            prepare_write(approval_path).write_text(json.dumps(approval), encoding="utf-8")
            evaluate.return_value = fresh
            result = evaluate_live_promotion(root=root)
            promoted = apply_live_promotion(
                root=root,
                result=result,
                approval_path=approval_path,
                policy_path=policy_path,
            )

        self.assertTrue(result["eligible"], result["blockers"])
        self.assertEqual(promoted["operation_mode"], "LIVE")

    def test_ineligible_result_cannot_mutate_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, approval_path, policy_path = self._write_bundle(root)
            before = policy_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "ineligible"):
                apply_live_promotion(
                    root=root,
                    result={"eligible": False},
                    approval_path=approval_path,
                    policy_path=policy_path,
                )
            after = policy_path.read_bytes()
        self.assertEqual(before, after)

    @patch('stock_jp.operation.evidence.evaluate_all_live_requirements')
    def test_bootstrap_bundle_rejects_tampered_final_acknowledgement(self, evaluate) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fresh, approval_path, policy_path = self._write_bundle(root)
            evaluate.return_value = fresh
            result = evaluate_live_promotion(root=root)
            apply_live_promotion(
                root=root,
                result=result,
                approval_path=approval_path,
                policy_path=policy_path,
            )
            approval = json.loads(approval_path.read_text(encoding="utf-8"))
            approval["acknowledgements"]["human_only_submission"] = False
            prepare_write(approval_path).write_text(json.dumps(approval), encoding="utf-8")
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            failures = validate_promoted_evidence_bundle(root=root, policy=policy)
        self.assertIn(
            "typed LIVE approval acknowledgement is false: human_only_submission",
            failures,
        )


if __name__ == "__main__":
    unittest.main()
