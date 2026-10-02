from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.historical_replay import (
    ReplayInputError,
    initialize_input_bundle,
    validate_inputs,
    write_replay,
)


def _csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(root: Path) -> Path:
    private = root / "operations/private"
    data = private / "historical-replay/input"
    source_fields = [
        "source_id", "provider", "title", "published_at_jst", "url",
        "official", "replay_authorized", "content_sha256",
    ]
    sources = [
        {
            "source_id": source_id,
            "provider": "JPX or issuer primary source",
            "title": source_id,
            "published_at_jst": published,
            "url": f"https://example.test/{source_id}",
            "official": "true",
            "replay_authorized": "true",
            "content_sha256": character * 64,
        }
        for source_id, published, character in (
            ("master", "2024-01-01T18:00:00+09:00", "a"),
            ("fundamentals", "2025-01-01T18:00:00+09:00", "b"),
            ("prices", "2025-01-01T18:00:00+09:00", "c"),
            ("benchmark", "2025-01-01T18:00:00+09:00", "d"),
            ("regime", "2025-01-01T18:00:00+09:00", "e"),
        )
    ]
    _csv(data / "source-register.csv", source_fields, sources)
    _csv(
        data / "security-master.csv",
        [
            "issue_id", "code", "name", "sector", "market", "effective_from",
            "effective_through", "domestic_common_stock", "event_type", "source_id",
            "known_at_jst",
        ],
        [
            {
                "issue_id": "JP0001", "code": "1234", "name": "Example",
                "sector": "Services", "market": "Growth", "effective_from": "2024-01-02",
                "effective_through": "", "domestic_common_stock": "true",
                "event_type": "LISTED", "source_id": "master",
                "known_at_jst": "2024-01-01T18:00:00+09:00",
            }
        ],
    )
    sessions = ["2025-01-30", "2025-01-31", "2025-02-03", "2025-02-28", "2025-03-03"]
    period_from = date.fromisoformat(sessions[0])
    period_through = date.fromisoformat(sessions[-1])
    session_dates = {date.fromisoformat(day) for day in sessions}
    _csv(
        data / "trading-calendar.csv",
        ["date", "is_trading_day", "source_id", "known_at_jst"],
        [
            {
                "date": day.isoformat(),
                "is_trading_day": str(day in session_dates).lower(),
                "source_id": "benchmark",
                "known_at_jst": "2025-01-01T18:00:00+09:00",
            }
            for day in (
                period_from + timedelta(days=offset)
                for offset in range((period_through - period_from).days + 1)
            )
        ],
    )
    prices = []
    for index, day in enumerate(sessions):
        close = 100 + index * 5
        prices.append(
            {
                "date": day, "issue_id": "JP0001", "open": close,
                "high": close + 1, "low": close - 1, "close": close,
                "volume": 100000, "turnover": close * 100000, "status": "OK",
                "source_id": "prices", "available_at_jst": f"{day}T16:00:00+09:00",
            }
        )
    _csv(
        data / "daily-prices.csv",
        [
            "date", "issue_id", "open", "high", "low", "close", "volume",
            "turnover", "status", "source_id", "available_at_jst",
        ],
        prices,
    )
    _csv(
        data / "corporate-actions.csv",
        [
            "effective_date", "issue_id", "action_type", "ratio",
            "successor_issue_id", "cash_consideration", "fractional_cash_price",
            "source_id", "announced_at_jst",
        ],
        [],
    )
    assessments = []
    for day in ("2025-01-31", "2025-02-28"):
        assessments.append(
            {
                "evaluation_date": day,
                "decision_at_jst": f"{day}T18:00:00+09:00",
                "review_type": "MONTHLY",
                "issue_id": "JP0001",
                "code": "1234",
                "sector": "Services",
                "status": "COMPLETE_PASS",
                "hard_gates_passed": True,
                "score": 80,
                "market_score": 10,
                "reverse_score": 12,
                "other_score": 58,
                "liquidity_passed": True,
                "entry_ready": True,
                "add_ready": False,
                "required_revenue_cagr_pct": 30,
                "dilution_outlook_pct": 5,
                "major_kpi_missed": False,
                "exit_rule": "",
                "source_ids": ["fundamentals"],
                "latest_source_published_at_jst": "2025-01-01T18:00:00+09:00",
            }
        )
    _jsonl(data / "assessments.jsonl", assessments)
    _csv(
        data / "review-events.csv",
        [
            "event_id", "issue_id", "event_date", "review_due_date",
            "review_type", "source_id", "available_at_jst",
        ],
        [],
    )
    _csv(
        data / "market-regime.csv",
        ["evaluation_date", "state", "entry_multiplier", "source_ids", "available_at_jst"],
        [
            {
                "evaluation_date": day, "state": "NORMAL", "entry_multiplier": 1,
                "source_ids": "regime", "available_at_jst": f"{day}T18:00:00+09:00",
            }
            for day in ("2025-01-31", "2025-02-28")
        ],
    )
    _csv(
        data / "benchmark.csv",
        ["date", "close", "source_id", "available_at_jst"],
        [
            {
                "date": day, "close": 2000 + index * 10, "source_id": "benchmark",
                "available_at_jst": f"{day}T16:00:00+09:00",
            }
            for index, day in enumerate(sessions)
        ],
    )
    roles = {
        "source_register": "source-register.csv",
        "security_master": "security-master.csv",
        "trading_calendar": "trading-calendar.csv",
        "daily_prices": "daily-prices.csv",
        "corporate_actions": "corporate-actions.csv",
        "review_events": "review-events.csv",
        "assessments": "assessments.jsonl",
        "market_regime": "market-regime.csv",
        "benchmark": "benchmark.csv",
    }
    manifest = {
        "schema_version": "1.0",
        "status": "READY",
        "generated_at_jst": "2025-03-04T08:00:00+09:00",
        "evaluation_kind": "RETROSPECTIVE_STRESS_TEST",
        "holdout_claimed": False,
        "period": {"from": sessions[0], "through": sessions[-1]},
        "initial_capital": 1_000_000,
        "fee_rate": 0.0015,
        "slippage_rate": 0.001,
        "board_lot": 100,
        "price_basis": "AS_TRADED_UNADJUSTED",
        "certifications": {
            "point_in_time_security_master": True,
            "includes_delisted": True,
            "includes_mergers": True,
            "includes_corporate_actions": True,
            "includes_all_material_disclosures": True,
            "source_rights_reviewed": True,
            "jquants_excluded": True,
            "unofficial_prices_excluded": True,
        },
        "datasets": [
            {
                "role": role,
                "path": f"historical-replay/input/{filename}",
                "sha256": _hash(data / filename),
                "provider": "Derived from authorised official primary sources",
                "official": True,
                "replay_authorized": True,
            }
            for role, filename in roles.items()
        ],
    }
    path = private / "historical-replay/input-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


class HistoricalReplayTest(unittest.TestCase):
    def test_valid_full_universe_inputs_write_hash_bound_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = _fixture(root)
            written = write_replay(
                root=root,
                manifest_path=manifest,
                output_dir=root / "operations/private/historical-replay/output",
            )
            result = written["result"]
            point = json.loads(written["point_in_time_manifest"].read_text())

        self.assertEqual(result["evaluation_kind"], "RETROSPECTIVE_STRESS_TEST")
        self.assertFalse(result["holdout_claimed"])
        self.assertEqual(set(result["comparisons"]), {"v0.2", "v0.4"})
        self.assertGreater(result["comparisons"]["v0.4"]["final_nav"], 1_000_000)
        self.assertEqual(point["universe"]["required_count"], 2)
        self.assertEqual(point["quality"]["lookahead_violations"], 0)

    def test_jquants_provider_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = _fixture(root)
            manifest = json.loads(manifest_path.read_text())
            manifest["datasets"][0]["provider"] = "J Quants API v2"
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(ReplayInputError) as caught:
                validate_inputs(root=root, manifest_path=manifest_path)
        self.assertTrue(any("prohibited replay provider" in item for item in caught.exception.blockers))

    def test_missing_trading_calendar_day_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = _fixture(root)
            calendar_path = (
                root / "operations/private/historical-replay/input/trading-calendar.csv"
            )
            with calendar_path.open(encoding="utf-8", newline="") as source:
                rows = list(csv.DictReader(source))
            _csv(calendar_path, list(rows[0]), rows[1:])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            next(
                item for item in manifest["datasets"] if item["role"] == "trading_calendar"
            )["sha256"] = _hash(calendar_path)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ReplayInputError) as caught:
                validate_inputs(root=root, manifest_path=manifest_path)
        self.assertTrue(
            any("trading calendar is incomplete" in item for item in caught.exception.blockers)
        )

    def test_missing_disclosure_assessment_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = _fixture(root)
            events_path = (
                root / "operations/private/historical-replay/input/review-events.csv"
            )
            _csv(
                events_path,
                [
                    "event_id", "issue_id", "event_date", "review_due_date",
                    "review_type", "source_id", "available_at_jst",
                ],
                [
                    {
                        "event_id": "earnings-1",
                        "issue_id": "JP0001",
                        "event_date": "2025-01-31",
                        "review_due_date": "2025-02-03",
                        "review_type": "QUARTERLY",
                        "source_id": "fundamentals",
                        "available_at_jst": "2025-01-31T18:00:00+09:00",
                    }
                ],
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            next(
                item for item in manifest["datasets"] if item["role"] == "review_events"
            )["sha256"] = _hash(events_path)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ReplayInputError) as caught:
                validate_inputs(root=root, manifest_path=manifest_path)
        self.assertTrue(
            any(
                "required disclosure assessment is missing" in item
                for item in caught.exception.blockers
            )
        )

    def test_lookahead_assessment_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = _fixture(root)
            assessment_path = root / "operations/private/historical-replay/input/assessments.jsonl"
            rows = [json.loads(line) for line in assessment_path.read_text().splitlines()]
            rows[0]["latest_source_published_at_jst"] = "2025-02-01T09:00:00+09:00"
            _jsonl(assessment_path, rows)
            manifest = json.loads(manifest_path.read_text())
            for item in manifest["datasets"]:
                if item["role"] == "assessments":
                    item["sha256"] = _hash(assessment_path)
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(ReplayInputError) as caught:
                validate_inputs(root=root, manifest_path=manifest_path)
        self.assertTrue(any("look-ahead" in item for item in caught.exception.blockers))

    def test_missing_monthly_universe_assessment_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = _fixture(root)
            assessment_path = root / "operations/private/historical-replay/input/assessments.jsonl"
            rows = [json.loads(line) for line in assessment_path.read_text().splitlines()][0:1]
            _jsonl(assessment_path, rows)
            manifest = json.loads(manifest_path.read_text())
            for item in manifest["datasets"]:
                if item["role"] == "assessments":
                    item["sha256"] = _hash(assessment_path)
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(ReplayInputError) as caught:
                validate_inputs(root=root, manifest_path=manifest_path)
        self.assertTrue(
            any("full-universe assessment is missing" in item for item in caught.exception.blockers)
        )

    def test_delisting_uses_documented_terminal_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = _fixture(root)
            action_path = (
                root
                / "operations/private/historical-replay/input/corporate-actions.csv"
            )
            _csv(
                action_path,
                [
                    "effective_date", "issue_id", "action_type", "ratio",
                    "successor_issue_id", "cash_consideration",
                    "fractional_cash_price", "source_id", "announced_at_jst",
                ],
                [
                    {
                        "effective_date": "2025-02-28",
                        "issue_id": "JP0001",
                        "action_type": "DELISTING",
                        "ratio": "",
                        "successor_issue_id": "",
                        "cash_consideration": 90,
                        "fractional_cash_price": "",
                        "source_id": "master",
                        "announced_at_jst": "2025-01-01T18:00:00+09:00",
                    }
                ],
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            next(
                item for item in manifest["datasets"] if item["role"] == "corporate_actions"
            )["sha256"] = _hash(action_path)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            written = write_replay(
                root=root,
                manifest_path=manifest_path,
                output_dir=root / "operations/private/historical-replay/output",
            )
            with written["replay_result"].parent.joinpath("trade-log.csv").open(
                encoding="utf-8"
            ) as source:
                trade_rows = list(csv.DictReader(source))
            self.assertEqual(
                sum(
                    row["rule_id"] == "CORPORATE_ACTION_DELISTING"
                    for row in trade_rows
                ),
                1,
            )
            self.assertTrue(
                all(
                    float(row["price"]) == 90
                    for row in trade_rows
                    if row["rule_id"] == "CORPORATE_ACTION_DELISTING"
                )
            )

    def test_reverse_split_records_official_fractional_cash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = _fixture(root)
            action_path = (
                root
                / "operations/private/historical-replay/input/corporate-actions.csv"
            )
            _csv(
                action_path,
                [
                    "effective_date", "issue_id", "action_type", "ratio",
                    "successor_issue_id", "cash_consideration",
                    "fractional_cash_price", "source_id", "announced_at_jst",
                ],
                [
                    {
                        "effective_date": "2025-02-28",
                        "issue_id": "JP0001",
                        "action_type": "REVERSE_SPLIT",
                        "ratio": 0.013,
                        "successor_issue_id": "",
                        "cash_consideration": "",
                        "fractional_cash_price": 80,
                        "source_id": "master",
                        "announced_at_jst": "2025-01-01T18:00:00+09:00",
                    }
                ],
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            next(
                item for item in manifest["datasets"] if item["role"] == "corporate_actions"
            )["sha256"] = _hash(action_path)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            written = write_replay(
                root=root,
                manifest_path=manifest_path,
                output_dir=root / "operations/private/historical-replay/output",
            )
            with written["replay_result"].parent.joinpath("trade-log.csv").open(
                encoding="utf-8"
            ) as source:
                trade_rows = list(csv.DictReader(source))
            fractional_rows = [
                row
                for row in trade_rows
                if row["rule_id"] == "CORPORATE_ACTION_FRACTIONAL_CASH"
            ]

        self.assertEqual(len(fractional_rows), 1)  # v0.4; v0.2 cannot afford one lot.
        self.assertTrue(all(float(row["price"]) == 80 for row in fractional_rows))
        self.assertAlmostEqual(float(fractional_rows[0]["quantity"]), 0.2)

    def test_initialization_is_private_draft_and_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "operations/private/historical-replay/2025-2026/input"
            destination.parent.mkdir(parents=True)
            manifest_path = initialize_input_bundle(
                root=root,
                destination=destination,
                evaluation_kind="RETROSPECTIVE_STRESS_TEST",
                period_from=date(2025, 1, 1),
                period_through=date(2026, 8, 31),
                initial_capital=10_000_000,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            with self.assertRaises(ReplayInputError):
                initialize_input_bundle(
                    root=root,
                    destination=destination,
                    evaluation_kind="RETROSPECTIVE_STRESS_TEST",
                    period_from=date(2025, 1, 1),
                    period_through=date(2026, 8, 31),
                    initial_capital=10_000_000,
                )
        self.assertEqual(manifest["status"], "DRAFT")
        self.assertFalse(manifest["holdout_claimed"])
        self.assertTrue(manifest["certifications"]["jquants_excluded"])
        self.assertTrue(all(item["official"] is False for item in manifest["datasets"]))

    def test_forward_holdout_is_bound_to_a_frozen_unobserved_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = _fixture(root)
            data = root / "operations/private/historical-replay/input"
            for path in data.iterdir():
                path.write_text(
                    path.read_text(encoding="utf-8").replace("2025", "2027"),
                    encoding="utf-8",
                )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update(
                {
                    "generated_at_jst": "2027-03-04T08:00:00+09:00",
                    "evaluation_kind": "FORWARD_HOLDOUT",
                    "holdout_claimed": True,
                    "period": {"from": "2027-01-30", "through": "2027-03-03"},
                }
            )
            for item in manifest["datasets"]:
                item["sha256"] = _hash(
                    root / "operations/private" / item["path"]
                )
            period = manifest["period"]
            criteria = {
                "minimum_monthly_evaluation_count": 2,
                "minimum_trade_count": 1,
                "maximum_drawdown_floor_pct": -100.0,
                "maximum_single_name_loss_floor_pct": -100.0,
                "maximum_industry_loss_floor_pct": -100.0,
            }
            plan = {
                "schema_version": "1.0",
                "status": "FROZEN",
                "decision": "START_FORWARD_HOLDOUT",
                "rule_version": "v0.4",
                "rule_frozen_at_jst": "2026-09-01T22:21:26+09:00",
                "frozen_at_jst": "2026-12-31T09:00:00+09:00",
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
            plan_path = root / "operations/private/evidence/v04-holdout-plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            manifest["holdout"] = {
                "plan_path": "evidence/v04-holdout-plan.json",
                "plan_sha256": _hash(plan_path),
                "retuning_count": 0,
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            class FrozenDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    value = cls.fromisoformat("2027-04-01T09:00:00+09:00")
                    return value if tz is None else value.astimezone(tz)

            with patch("scripts.historical_replay.datetime", FrozenDateTime):
                written = write_replay(
                    root=root,
                    manifest_path=manifest_path,
                    output_dir=root
                    / "operations/private/historical-replay/v04-forward/output",
                )
            result = written["result"]
        self.assertEqual(result["evaluation_kind"], "FORWARD_HOLDOUT")
        self.assertTrue(result["holdout_claimed"])
        self.assertTrue(result["holdout"]["criteria_met"])
        self.assertEqual(result["holdout"]["criterion_failures"], [])


if __name__ == "__main__":
    unittest.main()
