import csv
import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.daily_operation import (
    PROJECT_ROOT,
    complete_run,
    fail_run,
    initialize_workspace,
    prepare_run,
)


def complete_artifacts(
    root: Path,
    prepared: dict[str, object],
    *,
    completed_at: str = "2026-08-31T19:05:00+09:00",
    source_cutoff: str = "2026-08-31T18:30:00+09:00",
    price_date: str = "2026-08-31",
) -> None:
    run_dir = root / str(prepared["run_dir"])
    report_path = run_dir / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report = (
        report.replace("- 実行状態: `IN-PROGRESS`", "- 実行状態: `COMPLETED`")
        .replace(
            "- 今回の開示カットオフ（JST）: 未確定",
            f"- 今回の開示カットオフ（JST）: {source_cutoff}",
        )
        .replace("- 株価基準日: 未確定", f"- 株価基準日: {price_date}")
        .replace("- 総合結果: 未確定", "- 総合結果: 確認完了")
    )
    report_path.write_text(report, encoding="utf-8")

    coverage_path = run_dir / "coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    coverage["status"] = "COMPLETED"
    coverage["source_window"]["through_inclusive_jst"] = source_cutoff
    coverage["completed_at_jst"] = completed_at
    for item in coverage["universe"].values():
        item["checked"] = list(item["expected"])
    for name, source in coverage["official_sources"].items():
        source["status"] = "CHECKED" if source["required"] else "NOT_APPLICABLE"
    coverage_path.write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    health_path = run_dir / "provider-health.json"
    health = json.loads(health_path.read_text(encoding="utf-8"))
    health.update(
        {
            "status": "COMPLETED",
            "started_at_jst": "2026-08-31T18:31:00+09:00",
            "completed_at_jst": "2026-08-31T18:45:00+09:00",
        }
    )
    health_path.write_text(
        json.dumps(health, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    queue_path = run_dir / "research-queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    queue["generated_at_jst"] = "2026-08-31T18:45:00+09:00"
    queue_path.write_text(
        json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    plan_path = run_dir / "work-plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan.update(
        {
            "status": "COMPLETED",
            "next_trading_date": "2026-09-01",
            "trading_calendar_confirmed": True,
        }
    )
    for task in plan["tasks"]:
        task["status"] = "COMPLETED"
        task["evidence_source_ids"] = ["jpx-check-1"]
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "research-results.md").write_text(
        "# 夜間調査結果\n\n- 状態: `COMPLETED`\n"
        f"- 情報カットオフ（JST）: {source_cutoff}\n"
        "- 翌営業日: 2026-09-01\n- 対象件数: 0\n- 未解決事項: なし\n\n"
        "## 調査結果\n\n一次資料を確認済み。\n",
        encoding="utf-8",
    )
    (run_dir / "global-risk.md").write_text(
        "# 世界情勢・市場リスク確認\n\n- 状態: `COMPLETED`\n"
        f"- 情報カットオフ（JST）: {source_cutoff}\n- 判定: `NORMAL`\n\n"
        "公的情報を確認し、対象への重大な伝播なし。\n",
        encoding="utf-8",
    )
    actions_path = run_dir / "next-day-actions.csv"
    with actions_path.open(encoding="utf-8", newline="") as source:
        action_fields = next(csv.reader(source))
    action = dict.fromkeys(action_fields, "")
    action.update(
        {
            "action_id": "2026-08-31-GLOBAL-next",
            "priority": "NORMAL",
            "trade_date": "2026-09-01",
            "code": "GLOBAL",
            "company": "対象なし",
            "current_status": "EMPTY",
            "next_action": "NO-ACTION",
            "rule_ids": "OPS-EMPTY-UNIVERSE",
            "trigger_type": "nightly_review",
            "trigger_condition": "対象なし",
            "human_action": "なし",
            "evidence_source_ids": "jpx-check-1",
        }
    )
    with actions_path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=action_fields)
        writer.writeheader()
        writer.writerow(action)

    with (run_dir / "sources.csv").open("a", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        for category in ("tdnet", "edinet", "jpx"):
            writer.writerow(
                [
                    f"{category}-check-1",
                    category,
                    "",
                    f"{category} checked",
                    "2026-08-31T18:00:00+09:00",
                    "2026-08-31T18:40:00+09:00",
                    f"https://example.com/{category}",
                    "true",
                    "true",
                    "test fixture",
                ]
            )
        if coverage["official_sources"]["company_ir"]["status"] == "CHECKED":
            ir_codes = sorted(
                set(coverage["universe"]["holdings"]["expected"])
                | set(coverage["universe"]["watchlist"]["expected"])
            )
            for code in ir_codes:
                writer.writerow(
                    [
                        f"company-ir-{code}",
                        "company_ir",
                        code,
                        f"company IR checked for {code}",
                        "2026-08-31T18:00:00+09:00",
                        "2026-08-31T18:40:00+09:00",
                        f"https://example.com/ir/{code}",
                        "true",
                        "true",
                        "test fixture",
                    ]
                )


def append_paper_order(
    root: Path,
    prepared: dict[str, object],
    *,
    ticket_id: str = "2026-08-31-1234-BUY",
    code: str = "1234",
) -> None:
    run_dir = root / str(prepared["run_dir"])
    with (run_dir / "orders.csv").open(encoding="utf-8", newline="") as source:
        fields = next(csv.reader(source))
    order = dict.fromkeys(fields, "")
    order.update(
        {
            "ticket_id": ticket_id,
            "decision_id": f"2026-08-31-{code}-monthly",
            "prepared_at_jst": "2026-08-31T18:50:00+09:00",
            "trade_date": "2026-09-01",
            "operation_mode": "PAPER",
            "rule_version": "v0.4",
            "code": code,
            "company": "Example",
            "side": "BUY",
            "action": "BUY",
            "rule_ids": "E-1",
            "order_type": "LIMIT",
            "limit_price": "1000",
            "quantity_private": "100",
            "position_pct": "1.0",
            "valid_until_jst": "2026-09-01T15:30:00+09:00",
            "participation_cap_pct": "5.0",
            "status": "PAPER_PROPOSED",
        }
    )
    with (run_dir / "orders.csv").open("a", encoding="utf-8", newline="") as file:
        csv.DictWriter(file, fieldnames=fields).writerow(order)
    actions_path = run_dir / "next-day-actions.csv"
    with actions_path.open(encoding="utf-8", newline="") as source:
        action_reader = csv.DictReader(source)
        action_fields = list(action_reader.fieldnames or [])
        actions = list(action_reader)
    action_row = dict.fromkeys(action_fields, "")
    action_row.update(
        {
            "action_id": f"2026-08-31-{code}-next",
            "priority": "HIGH",
            "trade_date": "2026-09-01",
            "code": code,
            "company": "Example",
            "current_status": "WATCH",
            "next_action": "BUY",
            "rule_ids": "E-1",
            "trigger_type": "monthly",
            "trigger_condition": "entry gate passed",
            "limit_price": "1000",
            "position_pct": "1.0",
            "ticket_id": ticket_id,
            "human_action": "PAPER reconciliation",
            "evidence_source_ids": "jpx-check-1",
            "decision_log_path": f"2026-08-31-{code}-monthly",
        }
    )
    actions.append(action_row)
    with actions_path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=action_fields)
        writer.writeheader()
        writer.writerows(actions)
    handoff_path = run_dir / "handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    pending = handoff.setdefault("pending_orders", [])
    if ticket_id not in pending:
        pending.append(ticket_id)
    handoff_path.write_text(
        json.dumps(handoff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


class DailyOperationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "operations").mkdir()
        shutil.copytree(
            PROJECT_ROOT / "operations/templates",
            self.root / "operations/templates",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_init_is_idempotent_and_never_overwrites_private_files(self) -> None:
        first = initialize_workspace(self.root)
        watchlist = self.root / "operations/private/watchlist.csv"
        watchlist.write_text("private-value\n", encoding="utf-8")
        second = initialize_workspace(self.root)

        self.assertIn("operations/private/state.json", first["created"])
        self.assertIn("operations/private/watchlist.csv", second["existing"])
        self.assertEqual(watchlist.read_text(encoding="utf-8"), "private-value\n")

    def test_prepare_creates_daily_bundle_and_resumes_without_overwrite(self) -> None:
        first = prepare_run(at="2026-08-31T18:30:00+09:00", root=self.root)
        report = self.root / first["report"]
        report.write_text("work in progress", encoding="utf-8")
        second = prepare_run(
            at="2026-08-31T20:00:00+09:00",
            run_token=str(first["run_token"]),
            root=self.root,
        )

        self.assertEqual(first["run_id"], "2026-08-31")
        self.assertFalse(first["resumed"])
        self.assertTrue(second["resumed"])
        self.assertEqual(second["status"], "in_progress")
        self.assertEqual(report.read_text(encoding="utf-8"), "work in progress")
        for filename in (
            "report.md",
            "orders.csv",
            "sources.csv",
            "pretrade-check.md",
            "coverage.json",
            "lease.json",
            "provider-health.json",
            "research-queue.json",
            "work-plan.json",
            "research-results.md",
            "next-day-actions.csv",
            "handoff.json",
        ):
            self.assertTrue(
                (self.root / "operations/private/runs/2026-08-31" / filename).is_file()
            )

    def test_complete_advances_state_and_is_idempotent(self) -> None:
        prepared = prepare_run(at="2026-08-31T18:30:00+09:00", root=self.root)
        complete_artifacts(self.root, prepared)
        run_dir = self.root / prepared["run_dir"]
        append_paper_order(self.root, prepared)
        handoff_path = run_dir / "handoff.json"
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        handoff.update(
            {
                "pending_orders": ["2026-08-31-1234-BUY"],
                "pending_reviews": ["1234-quarterly"],
                "data_gaps": [
                    {
                        "severity": "NON_CRITICAL",
                        "source": "industry association",
                        "impact": "does not affect an immediate action",
                        "retry_after_jst": "2026-09-01T18:30:00+09:00",
                    }
                ],
                "next_run_at_jst": "2026-09-01T18:30:00+09:00",
            }
        )
        handoff_path.write_text(
            json.dumps(handoff, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = complete_run(
            run_id="2026-08-31",
            completed_at="2026-08-31T19:05:00+09:00",
            source_cutoff="2026-08-31T18:30:00+09:00",
            price_date="2026-08-31",
            summary="completed",
            run_token=str(prepared["run_token"]),
            alert_count=1,
            root=self.root,
        )
        repeated = complete_run(
            run_id="2026-08-31",
            completed_at="2026-08-31T19:05:00+09:00",
            source_cutoff="2026-08-31T18:30:00+09:00",
            price_date="2026-08-31",
            summary="completed",
            run_token=str(prepared["run_token"]),
            alert_count=1,
            root=self.root,
        )

        state = json.loads(
            (self.root / "operations/private/state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(result["order_count"], 1)
        self.assertEqual(result["data_gap_count"], 1)
        self.assertTrue(repeated["already_closed"])
        self.assertEqual(state["last_run_id"], "2026-08-31")
        self.assertEqual(
            state["last_disclosure_cutoff_jst"], "2026-08-31T18:30:00+09:00"
        )
        self.assertEqual(state["pending_reviews"], ["1234-quarterly"])
        self.assertEqual(state["schema_version"], "2.1")
        self.assertEqual(state["state_revision"], 1)
        self.assertEqual(state["consecutive_successful_runs"], 1)
        self.assertEqual(
            state["unreconciled_ticket_ids"], ["2026-08-31-1234-BUY"]
        )
        with (self.root / "operations/private/run-history.csv").open(
            encoding="utf-8", newline=""
        ) as history_file:
            history = list(csv.DictReader(history_file))
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "completed")
        self.assertEqual(history[0]["order_count"], "1")
        watermarks = json.loads(
            (
                self.root / "operations/private/source-watermarks.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            watermarks["sources"]["tdnet"]["last_successful_cutoff_jst"],
            "2026-08-31T18:30:00+09:00",
        )

    def test_blank_report_and_zero_sources_cannot_advance_cutoff(self) -> None:
        prepared = prepare_run(
            at="2026-08-31T18:30:00+09:00", root=self.root
        )

        with self.assertRaisesRegex(ValueError, "report missing completed field"):
            complete_run(
                run_id="2026-08-31",
                completed_at="2026-08-31T19:05:00+09:00",
                source_cutoff="2026-08-31T18:30:00+09:00",
                price_date="2026-08-31",
                summary="must not complete",
                run_token=str(prepared["run_token"]),
                root=self.root,
            )
        state = json.loads(
            (self.root / "operations/private/state.json").read_text(encoding="utf-8")
        )
        self.assertIsNone(state["last_disclosure_cutoff_jst"])

        complete_artifacts(self.root, prepared)
        run_dir = self.root / str(prepared["run_dir"])
        (run_dir / "sources.csv").write_text(
            ",".join(
                (
                    "source_id",
                    "category",
                    "code",
                    "title",
                    "published_at_jst",
                    "retrieved_at_jst",
                    "url",
                    "primary_source",
                    "used_for_decision",
                    "notes",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "has no sources.csv evidence row"):
            complete_run(
                run_id="2026-08-31",
                completed_at="2026-08-31T19:05:00+09:00",
                source_cutoff="2026-08-31T18:30:00+09:00",
                price_date="2026-08-31",
                summary="must not complete",
                run_token=str(prepared["run_token"]),
                root=self.root,
            )

    def test_duplicate_orders_and_wrong_lease_owner_are_rejected(self) -> None:
        prepared = prepare_run(
            at="2026-08-31T18:30:00+09:00", root=self.root
        )
        complete_artifacts(self.root, prepared)
        append_paper_order(self.root, prepared)
        append_paper_order(self.root, prepared)

        with self.assertRaisesRegex(ValueError, "does not own the active lease"):
            complete_run(
                run_id="2026-08-31",
                completed_at="2026-08-31T19:05:00+09:00",
                source_cutoff="2026-08-31T18:30:00+09:00",
                price_date="2026-08-31",
                summary="must not complete",
                run_token="wrong-token",
                root=self.root,
            )
        with self.assertRaisesRegex(ValueError, "duplicate ticket_id"):
            complete_run(
                run_id="2026-08-31",
                completed_at="2026-08-31T19:05:00+09:00",
                source_cutoff="2026-08-31T18:30:00+09:00",
                price_date="2026-08-31",
                summary="must not complete",
                run_token=str(prepared["run_token"]),
                root=self.root,
            )

    def test_malformed_order_values_cannot_complete_a_run(self) -> None:
        prepared = prepare_run(at="2026-08-31T18:30:00+09:00", root=self.root)
        complete_artifacts(self.root, prepared)
        append_paper_order(self.root, prepared)
        orders_path = self.root / str(prepared["run_dir"]) / "orders.csv"
        with orders_path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            fields = list(reader.fieldnames or [])
            orders = list(reader)
        orders[0].update(
            {
                "side": "SELL",
                "quantity_private": "nan",
                "valid_until_jst": "2026-09-02T15:30:00+09:00",
                "participation_cap_pct": "11",
                "broker_order_id_private": "must-not-exist",
            }
        )
        with orders_path.open("w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=fields)
            writer.writeheader()
            writer.writerows(orders)

        with self.assertRaisesRegex(
            ValueError, "quantity_private must be > 0"
        ) as raised:
            complete_run(
                run_id="2026-08-31",
                completed_at="2026-08-31T19:05:00+09:00",
                source_cutoff="2026-08-31T18:30:00+09:00",
                price_date="2026-08-31",
                summary="must not complete",
                run_token=str(prepared["run_token"]),
                root=self.root,
            )
        message = str(raised.exception)
        self.assertIn("BUY requires side BUY", message)
        self.assertIn("valid_until_jst must be on trade_date", message)
        self.assertIn("participation_cap_pct must be <= 10", message)
        self.assertIn("must not contain broker_order_id_private", message)

    def test_failed_run_does_not_advance_cutoff_and_can_be_resumed(self) -> None:
        prepare_run(at="2026-08-31T18:30:00+09:00", root=self.root)
        prepared = prepare_run(
            at="2026-08-31T18:31:00+09:00",
            root=self.root,
        )
        self.assertEqual(prepared["status"], "locked")
        lease = json.loads(
            (
                self.root / "operations/private/runs/2026-08-31/lease.json"
            ).read_text(encoding="utf-8")
        )
        fail_run(
            run_id="2026-08-31",
            completed_at="2026-08-31T18:45:00+09:00",
            summary="required source unavailable",
            run_token=lease["run_token"],
            root=self.root,
        )
        state = json.loads(
            (self.root / "operations/private/state.json").read_text(encoding="utf-8")
        )
        resumed = prepare_run(at="2026-08-31T19:00:00+09:00", root=self.root)
        handoff = json.loads(
            (
                self.root
                / "operations/private/runs/2026-08-31/handoff.json"
            ).read_text(encoding="utf-8")
        )

        self.assertIsNone(state["last_disclosure_cutoff_jst"])
        self.assertEqual(resumed["status"], "in_progress")
        self.assertEqual(handoff["attempt"], 2)
        self.assertEqual(handoff["status"], "in_progress")
        self.assertEqual(state["consecutive_successful_runs"], 0)

    def test_prepare_rejects_datetime_without_offset(self) -> None:
        with self.assertRaises(ValueError):
            prepare_run(at="2026-08-31T18:30:00", root=self.root)


if __name__ == "__main__":
    unittest.main()
