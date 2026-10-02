import csv
from datetime import date, datetime
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.nightly_artifacts import (
    create_nightly_artifacts,
    next_trading_date,
    validate_nightly_artifacts,
)
from scripts.nightly_operation import finalize_nightly_run, start_nightly_run
from scripts.operation_state import PROJECT_ROOT, initialize_or_migrate_workspace
from scripts.order_ticket import _require_entry_assessment, propose_order
from tests.operation_test_support import write_price_archive


FIXTURES = PROJECT_ROOT / "tests/fixtures/official-source-scan"


class NightlyScheduleTest(unittest.TestCase):
    def test_finalize_uses_original_run_day_without_changing_cutoff(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "operations/private/runs/2026-08-31"
            run.mkdir(parents=True)
            original = {"next_run_at_jst": None}
            (run / "handoff.json").write_text(json.dumps(original))
            cutoff = "2026-08-31T21:00:00+09:00"
            (run / "source-scan-cutoff.json").write_text(json.dumps({"cutoff_at_jst": cutoff}))
            def inspect_completion(**kwargs):
                self.assertEqual(kwargs["run_id"], "2026-08-31")
                self.assertEqual(kwargs["source_cutoff"], cutoff)
                self.assertEqual(json.loads((run / "handoff.json").read_text())["next_run_at_jst"],
                                 "2026-09-01T21:00:00+09:00")
                raise RuntimeError("isolated completion gate")
            with patch("scripts.nightly_operation.complete_run", side_effect=inspect_completion):
                with self.assertRaisesRegex(RuntimeError, "isolated completion gate"):
                    finalize_nightly_run(run_id="2026-08-31", run_token="synthetic",
                                         completed_at="2026-09-01T01:00:00+09:00",
                                         source_cutoff=cutoff, price_date="2026-08-31",
                                         summary="isolated scheduling test", root=root)
            self.assertEqual(json.loads((run / "handoff.json").read_text()), original)

    def test_next_eligible_slot_uses_jst_clock_and_completed_run_day(self) -> None:
        from scripts.nightly_operation import _next_weeknight
        for now, run_id, expected in (
            ("2026-09-01T01:00:00+09:00", "2026-08-31", "2026-09-01T21:00:00+09:00"),
            ("2026-09-01T20:59:59+09:00", "2026-08-31", "2026-09-01T21:00:00+09:00"),
            ("2026-09-01T21:00:00+09:00", "2026-08-31", "2026-09-02T21:00:00+09:00"),
            ("2026-09-01T21:00:01+09:00", "2026-08-31", "2026-09-02T21:00:00+09:00"),
            ("2026-09-01T18:00:00+09:00", "2026-09-01", "2026-09-02T21:00:00+09:00"),
            ("2026-09-04T01:00:00+09:00", "2026-09-03", "2026-09-04T21:00:00+09:00"),
            ("2026-09-04T21:00:00+09:00", "2026-09-03", "2026-09-07T21:00:00+09:00"),
            ("2026-09-05T01:00:00+09:00", "2026-08-31", "2026-09-07T21:00:00+09:00"),
            ("2026-09-06T23:00:00+09:00", "2026-08-31", "2026-09-07T21:00:00+09:00"),
            ("2026-09-07T01:00:00+09:00", "2026-08-31", "2026-09-07T21:00:00+09:00"),
            ("2026-08-31T16:00:00+00:00", "2026-08-31", "2026-09-01T21:00:00+09:00"),
        ):
            with self.subTest(now=now, run_id=run_id):
                self.assertEqual(_next_weeknight(datetime.fromisoformat(now), completed_run_id=run_id), expected)
        with self.assertRaises(ValueError):
            _next_weeknight(datetime(2026, 9, 1, 1))

    def test_next_weeknight_uses_2100_jst_and_skips_weekends(self) -> None:
        from scripts.nightly_operation import _next_weeknight, _parse_jst
        for now, expected in (
            ("2026-10-01T21:30:00+09:00", "2026-10-02T21:00:00+09:00"),
            ("2026-10-02T21:30:00+09:00", "2026-10-05T21:00:00+09:00"),
            ("2026-10-03T21:30:00+09:00", "2026-10-05T21:00:00+09:00"),
        ):
            self.assertEqual(_next_weeknight(_parse_jst(now)), expected)

    def test_new_workspace_schedule_template_uses_2100(self) -> None:
        template = Path(__file__).resolve().parents[1] / "operations/templates/daily-run-state-template.json"
        self.assertEqual(json.loads(template.read_text())["schedule"], "weekdays 21:00")


class NightlyOperationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "operations").mkdir()
        shutil.copytree(
            PROJECT_ROOT / "operations/templates", self.root / "operations/templates"
        )
        initialize_or_migrate_workspace(self.root)
        # Exercise the optional fixture adapter explicitly; production defaults
        # remain disabled and do not use J-Quants.
        config_path = self.root / "operations/private/source-config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["jquants"].update(
            {
                "enabled": True,
                "listed_master_enabled": True,
                "daily_bars_enabled": True,
                "financial_summary_enabled": True,
                "earnings_calendar_enabled": True,
                "trading_calendar_enabled": True,
            }
        )
        config_path.write_text(json.dumps(config) + "\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _add_holding(self, code: str = "1234") -> None:
        path = self.root / "operations/private/portfolio-register.csv"
        with path.open(encoding="utf-8", newline="") as source:
            fields = next(csv.reader(source))
        row = dict.fromkeys(fields, "")
        row.update({"code": code, "company": "Example", "status": "OPEN"})
        with path.open("a", encoding="utf-8", newline="") as destination:
            csv.DictWriter(destination, fieldnames=fields).writerow(row)

    def _start(self) -> dict[str, object]:
        targets: list[str] = []
        private = self.root / "operations/private"
        for filename, field, statuses in (
            ("portfolio-register.csv", "status", {"OPEN", "ACTIVE", "HELD"}),
            ("watchlist.csv", "active", {"TRUE", "1", "YES", "ACTIVE"}),
        ):
            with (private / filename).open(encoding="utf-8", newline="") as source:
                for row in csv.DictReader(source):
                    if str(row.get(field, "")).strip().upper() in statuses:
                        targets.append(str(row.get("code", "")).strip())
        write_price_archive(self.root, sorted(set(targets)) or ["9999"])
        return start_nightly_run(
            at="2026-08-31T18:45:00+09:00",
            cutoff="2026-08-31T18:30:00+09:00",
            fixture_dir=FIXTURES,
            root=self.root,
        )

    def _complete_research_queue(self, run_dir: Path) -> None:
        path = run_dir / "research-queue.json"
        queue = json.loads(path.read_text(encoding="utf-8"))
        for task in queue["tasks"]:
            task["status"] = "COMPLETED"
            task["evidence_source_ids"] = task["source_ids"] or ["manual-jpx"]
        path.write_text(
            json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def test_explicit_resume_after_midnight_preserves_run_and_success_state(self) -> None:
        first = self._start()
        state = self.root / "operations/private/state.json"
        before = state.read_bytes()
        resumed = start_nightly_run(
            at="2026-09-01T01:00:00+09:00", cutoff="2026-08-31T18:30:00+09:00",
            resume_run_id="2026-08-31", run_token=str(first["run_token"]),
            fixture_dir=FIXTURES, root=self.root,
        )
        self.assertEqual(resumed["run_id"], "2026-08-31")
        self.assertNotEqual(resumed["run_token"], first["run_token"])  # expired lease
        self.assertFalse((self.root / "operations/private/runs/2026-09-01").exists())
        self.assertEqual(state.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "original source-scan cutoff"):
            finalize_nightly_run(
                run_id="2026-08-31", run_token=str(resumed["run_token"]),
                completed_at="2026-09-01T01:01:00+09:00", source_cutoff="2026-08-31T19:00:00+09:00",
                price_date="2026-08-31", summary="must not complete", root=self.root,
            )
        with self.assertRaisesRegex(ValueError, "original source-scan cutoff"):
            start_nightly_run(
                at="2026-09-01T01:01:00+09:00", cutoff="2026-08-31T19:00:00+09:00",
                resume_run_id="2026-08-31", run_token=str(resumed["run_token"]),
                fixture_dir=FIXTURES, root=self.root,
            )

    def test_resume_cannot_create_or_go_back_before_completed_run(self) -> None:
        from scripts.daily_operation import prepare_run
        with self.assertRaises(FileNotFoundError):
            prepare_run(at="2026-09-01T21:00:00+09:00", resume_run_id="2026-08-31", root=self.root)
        self._start()
        path = self.root / "operations/private/state.json"
        state = json.loads(path.read_text())
        state["last_run_id"] = "2026-09-01"  # isolated fixture only
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, "latest completed run"):
            prepare_run(at="2026-09-02T21:00:00+09:00", resume_run_id="2026-08-31", root=self.root)

    def _complete_primary_checks(self, run_dir: Path) -> None:
        with (run_dir / "sources.csv").open(
            "a", encoding="utf-8", newline=""
        ) as destination:
            writer = csv.writer(destination)
            common = [
                "2026-08-31T18:20:00+09:00",
                "2026-08-31T18:50:00+09:00",
            ]
            writer.writerow(
                [
                    "manual-tdnet",
                    "tdnet",
                    "",
                    "TDnet checked through cutoff",
                    *common,
                    "https://www.release.tdnet.info/inbs/I_main_00.html",
                    "true",
                    "true",
                    "manual check",
                ]
            )
            writer.writerow(
                [
                    "manual-jpx",
                    "jpx",
                    "",
                    "JPX notices, prices and calendar checked",
                    *common,
                    "https://www.jpx.co.jp/",
                    "true",
                    "true",
                    "manual check",
                ]
            )
        (run_dir / "trading-calendar.json").write_text(
            json.dumps(
                {"rows": [{"date": "2026-09-01", "holiday_division": "1"}]},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        for name in ("coverage.json", "handoff.json"):
            path = run_dir / name
            document = json.loads(path.read_text(encoding="utf-8"))
            for gap in document["data_gaps"]:
                gap["status"] = "RESOLVED"
                gap["resolved_at_jst"] = "2026-08-31T18:50:00+09:00"
                gap["resolution_evidence"] = "manual-jpx"
            if name == "coverage.json":
                document["official_sources"]["tdnet"]["status"] = "CHECKED"
                document["official_sources"]["jpx"]["status"] = "CHECKED"
            path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        plan_path = run_dir / "work-plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["trading_calendar_confirmed"] = True
        plan["next_trading_date"] = "2026-09-01"
        plan_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        actions_path = run_dir / "next-day-actions.csv"
        with actions_path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            fields = list(reader.fieldnames or [])
            actions = list(reader)
        for action in actions:
            action["trade_date"] = "2026-09-01"
        with actions_path.open("w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(actions)
        self._complete_research_queue(run_dir)
        assessment_path = run_dir / "assessment-status.json"
        assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
        assessment["market_regime"] = {
            "status": "CURRENT",
            "state": "NORMAL",
            "entry_multiplier": 1.0,
        }
        assessment["companies"] = [
            {
                "code": row["code"],
                "status": "PASS",
                "entry_ready": True,
            }
            for row in assessment.get("companies", [])
        ]
        assessment_path.write_text(
            json.dumps(assessment, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_start_confirms_next_trade_date_and_builds_due_work(self) -> None:
        self._add_holding()
        result = self._start()
        run_dir = self.root / str(result["run_dir"])
        plan = json.loads((run_dir / "work-plan.json").read_text(encoding="utf-8"))
        with (run_dir / "next-day-actions.csv").open(
            encoding="utf-8", newline=""
        ) as source:
            actions = list(csv.DictReader(source))

        self.assertEqual(result["source_scan_status"], "PARTIAL")
        self.assertTrue(result["paper_go"])
        self.assertFalse(result["live_go"])
        self.assertTrue(result["trading_calendar_confirmed"])
        self.assertEqual(result["next_trading_date"], "2026-09-01")
        self.assertIn("2026-08-31-daily-event", result["due_task_ids"])
        self.assertEqual(actions[0]["code"], "1234")
        self.assertEqual(actions[0]["trade_date"], "2026-09-01")
        self.assertEqual(plan["status"], "IN_PROGRESS")

    def test_incomplete_investment_case_blocks_entry_order(self) -> None:
        self._add_holding()
        started = self._start()
        run_dir = self.root / str(started["run_dir"])

        with self.assertRaisesRegex(ValueError, "current formal MRS"):
            _require_entry_assessment(run_dir, "1234")

    def test_system_action_refreshes_after_assessment_is_calculated(self) -> None:
        self._add_holding()
        started = self._start()
        run_dir = self.root / str(started["run_dir"])
        assessment_path = run_dir / "assessment-status.json"
        assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
        assessment["market_regime"] = {"status": "CURRENT", "state": "NORMAL"}
        assessment["companies"] = [
            {"code": "1234", "status": "PASS", "entry_ready": True}
        ]
        assessment_path.write_text(
            json.dumps(assessment, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        create_nightly_artifacts(
            run_id="2026-08-31",
            at="2026-08-31T18:45:00+09:00",
            root=self.root,
        )

        with (run_dir / "next-day-actions.csv").open(
            encoding="utf-8", newline=""
        ) as source:
            action = next(csv.DictReader(source))
        self.assertEqual(action["next_action"], "WATCH")
        self.assertEqual(action["rule_ids"], "OPS-ASSESSMENT-PASS")

    def test_reviewed_action_is_not_overwritten_by_assessment_refresh(self) -> None:
        self._add_holding()
        started = self._start()
        run_dir = self.root / str(started["run_dir"])
        actions_path = run_dir / "next-day-actions.csv"
        with actions_path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            fields = list(reader.fieldnames or [])
            actions = list(reader)
        actions[0]["next_action"] = "KEEP"
        actions[0]["evidence_source_ids"] = "internal:portfolio-register"
        with actions_path.open("w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=fields)
            writer.writeheader()
            writer.writerows(actions)
        assessment_path = run_dir / "assessment-status.json"
        assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
        assessment["market_regime"] = {"status": "CURRENT", "state": "NORMAL"}
        assessment["companies"] = [
            {"code": "1234", "status": "PASS", "entry_ready": True}
        ]
        assessment_path.write_text(
            json.dumps(assessment, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        create_nightly_artifacts(
            run_id="2026-08-31",
            at="2026-08-31T18:45:00+09:00",
            root=self.root,
        )

        with actions_path.open(encoding="utf-8", newline="") as source:
            action = next(csv.DictReader(source))
        self.assertEqual(action["next_action"], "KEEP")

    def test_cash_equity_calendar_skips_holiday_trading_division(self) -> None:
        run_dir = self.root / "calendar"
        run_dir.mkdir()
        (run_dir / "trading-calendar.json").write_text(
            json.dumps(
                {
                    "rows": [
                        {"date": "2026-09-01", "holiday_division": "3"},
                        {"date": "2026-09-02", "holiday_division": "1"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        next_date, confirmed = next_trading_date(
            run_dir, date.fromisoformat("2026-08-31")
        )

        self.assertTrue(confirmed)
        self.assertEqual(next_date, "2026-09-02")

    def test_paper_proposal_updates_action_handoff_and_trade_ledger_once(self) -> None:
        self._add_holding()
        started = self._start()
        run_dir = self.root / str(started["run_dir"])
        self._complete_primary_checks(run_dir)
        actions_path = run_dir / "next-day-actions.csv"
        with actions_path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            fields = list(reader.fieldnames or [])
            actions = list(reader)
        actions[0].update(
            {
                "next_action": "BUY",
                "rule_ids": "E-1;E-2",
                "trigger_condition": "全エントリー条件を充足",
                "evidence_source_ids": "manual-tdnet",
            }
        )
        with actions_path.open("w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=fields)
            writer.writeheader()
            writer.writerows(actions)

        args = {
            "run_id": "2026-08-31",
            "run_token": str(started["run_token"]),
            "action_id": "2026-08-31-1234-next",
            "code": "1234",
            "company": "Example",
            "side": "BUY",
            "action": "BUY",
            "rule_ids": "E-1;E-2",
            "trade_date": "2026-09-01",
            "limit_price": "1000",
            "quantity": "100",
            "position_pct": "1.0",
            "valid_until": "2026-09-01T15:30:00+09:00",
            "participation_cap_pct": "5.0",
            "decision_id": "operations/private/decisions/2026-08-31-1234.md",
            "at": "2026-08-31T19:00:00+09:00",
            "root": self.root,
        }
        decision_path = self.root / args["decision_id"]
        decision_path.write_text("# decision\n", encoding="utf-8")
        first = propose_order(**args)
        second = propose_order(**args)

        self.assertEqual(first["status"], "PAPER_PROPOSED")
        self.assertFalse(first["broker_submitted"])
        self.assertTrue(second["already_proposed"])
        with (run_dir / "orders.csv").open(encoding="utf-8", newline="") as source:
            orders = list(csv.DictReader(source))
        with (self.root / "operations/private/trade-event-ledger.csv").open(
            encoding="utf-8", newline=""
        ) as source:
            events = list(csv.DictReader(source))
        handoff = json.loads((run_dir / "handoff.json").read_text(encoding="utf-8"))
        self.assertEqual(len(orders), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "PAPER_PROPOSED")
        self.assertIn(first["ticket_id"], handoff["pending_orders"])

    def test_deferred_research_cannot_support_an_order(self) -> None:
        self._add_holding()
        started = self._start()
        run_dir = self.root / str(started["run_dir"])
        queue_path = run_dir / "research-queue.json"
        self._complete_primary_checks(run_dir)
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        queue["tasks"][0]["status"] = "DEFERRED"
        queue_path.write_text(json.dumps(queue), encoding="utf-8")
        handoff_path = run_dir / "handoff.json"
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        handoff["pending_reviews"] = [queue["tasks"][0]["task_id"]]
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        actions_path = run_dir / "next-day-actions.csv"
        with actions_path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            fields = list(reader.fieldnames or [])
            actions = list(reader)
        actions[0].update(
            {
                "next_action": "BUY",
                "rule_ids": "E-1",
                "evidence_source_ids": "tdnet-20260831000001",
            }
        )
        with actions_path.open("w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=fields)
            writer.writeheader()
            writer.writerows(actions)
        decision_path = self.root / "operations/private/decisions/deferred.md"
        decision_path.write_text("# decision\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "must be completed"):
            propose_order(
                run_id="2026-08-31",
                run_token=str(started["run_token"]),
                action_id="2026-08-31-1234-next",
                code="1234",
                company="Example",
                side="BUY",
                action="BUY",
                rule_ids="E-1",
                trade_date="2026-09-01",
                limit_price="1000",
                quantity="100",
                position_pct="1",
                valid_until="2026-09-01T15:30:00+09:00",
                participation_cap_pct="5",
                decision_id="operations/private/decisions/deferred.md",
                at="2026-08-31T19:00:00+09:00",
                root=self.root,
            )

    def test_paused_policy_blocks_order_ticket(self) -> None:
        self._add_holding()
        started = self._start()
        run_dir = self.root / str(started["run_dir"])
        self._complete_primary_checks(run_dir)
        policy_path = self.root / "operations/private/operation-policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["operation_mode"] = "PAUSED"
        policy_path.write_text(
            json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        actions_path = run_dir / "next-day-actions.csv"
        with actions_path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            fields = list(reader.fieldnames or [])
            actions = list(reader)
        actions[0].update(
            {
                "next_action": "BUY",
                "rule_ids": "E-1",
                "evidence_source_ids": "source-1",
            }
        )
        with actions_path.open("w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=fields)
            writer.writeheader()
            writer.writerows(actions)
        with self.assertRaisesRegex(PermissionError, "blocks order tickets"):
            propose_order(
                run_id="2026-08-31",
                run_token=str(started["run_token"]),
                action_id="2026-08-31-1234-next",
                code="1234",
                company="Example",
                side="BUY",
                action="BUY",
                rule_ids="E-1",
                trade_date="2026-09-01",
                limit_price="1000",
                quantity="100",
                position_pct="1",
                valid_until="2026-09-01T15:30:00+09:00",
                participation_cap_pct="5",
                decision_id="decision-1",
                at="2026-08-31T19:00:00+09:00",
                root=self.root,
            )

    def test_trade_action_without_order_is_rejected(self) -> None:
        self._add_holding()
        started = self._start()
        run_dir = self.root / str(started["run_dir"])
        actions_path = run_dir / "next-day-actions.csv"
        with actions_path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            fields = list(reader.fieldnames or [])
            actions = list(reader)
        actions[0].update(
            {
                "next_action": "BUY",
                "rule_ids": "E-1",
                "evidence_source_ids": "source-1",
            }
        )
        with actions_path.open("w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=fields)
            writer.writeheader()
            writer.writerows(actions)
        errors = validate_nightly_artifacts(
            root=self.root,
            run_id="2026-08-31",
            handoff=json.loads((run_dir / "handoff.json").read_text(encoding="utf-8")),
            coverage=json.loads(
                (run_dir / "coverage.json").read_text(encoding="utf-8")
            ),
            orders=[],
        )
        self.assertTrue(
            any("requires a matching order ticket" in error for error in errors)
        )

    def test_honest_uncertainty_text_is_not_treated_as_a_template_marker(self) -> None:
        self._add_holding()
        started = self._start()
        run_dir = self.root / str(started["run_dir"])
        (run_dir / "research-results.md").write_text(
            "# 夜間調査結果\n\n- 状態: `COMPLETED`\n"
            "- 情報カットオフ（JST）: 2026-08-31T18:30:00+09:00\n"
            "- 翌営業日: 2026-09-01\n- 対象件数: 0\n"
            "- 未解決事項: なし\n\n将来の影響は未確定であり、追加確認する。\n",
            encoding="utf-8",
        )

        errors = validate_nightly_artifacts(
            root=self.root,
            run_id="2026-08-31",
            handoff=json.loads((run_dir / "handoff.json").read_text(encoding="utf-8")),
            coverage=json.loads(
                (run_dir / "coverage.json").read_text(encoding="utf-8")
            ),
            orders=[],
        )

        self.assertFalse(any("unresolved marker: 未確定" in error for error in errors))

    def test_empty_universe_starts_initial_review_with_global_no_action(self) -> None:
        started = self._start()
        run_dir = self.root / str(started["run_dir"])
        plan = json.loads((run_dir / "work-plan.json").read_text(encoding="utf-8"))
        with (run_dir / "next-day-actions.csv").open(
            encoding="utf-8", newline=""
        ) as source:
            actions = list(csv.DictReader(source))

        self.assertTrue(started["paper_go"])
        self.assertIn(
            "2026-08-31-initial-universe-review",
            {task["task_id"] for task in plan["tasks"]},
        )
        self.assertEqual(actions[0]["code"], "GLOBAL")
        self.assertEqual(actions[0]["next_action"], "NO-ACTION")

    def test_empty_universe_does_not_repeat_initial_review_after_a_prior_run(
        self,
    ) -> None:
        state_path = self.root / "operations/private/state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["last_run_id"] = "2026-08-28"
        state["last_successful_run_at_jst"] = "2026-08-28T19:00:00+09:00"
        state["last_disclosure_cutoff_jst"] = "2026-08-28T18:30:00+09:00"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        started = self._start()
        run_dir = self.root / str(started["run_dir"])
        plan = json.loads((run_dir / "work-plan.json").read_text(encoding="utf-8"))

        self.assertNotIn(
            "2026-08-31-initial-universe-review",
            {task["task_id"] for task in plan["tasks"]},
        )

    def test_failed_finalize_restores_next_run_handoff(self) -> None:
        self._add_holding()
        started = self._start()
        run_dir = self.root / str(started["run_dir"])
        handoff_path = run_dir / "handoff.json"
        before = json.loads(handoff_path.read_text(encoding="utf-8"))

        with self.assertRaises(ValueError):
            finalize_nightly_run(
                run_id="2026-08-31",
                run_token=str(started["run_token"]),
                completed_at="2026-08-31T19:05:00+09:00",
                source_cutoff="2026-08-31T18:30:00+09:00",
                price_date="2026-08-31",
                summary="must fail",
                root=self.root,
            )

        after = json.loads(handoff_path.read_text(encoding="utf-8"))
        self.assertEqual(after.get("next_run_at_jst"), before.get("next_run_at_jst"))


if __name__ == "__main__":
    unittest.main()
