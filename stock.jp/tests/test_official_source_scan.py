import csv
import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from scripts.daily_operation import complete_run, prepare_run
from scripts.official_source_scan import (
    SOURCE_FIELDS,
    SourceScanError,
    _append_sources,
    _request_json,
    scan_sources,
)
from scripts.operation_state import PROJECT_ROOT, initialize_or_migrate_workspace
from tests.operation_test_support import write_price_archive

FIXTURES = PROJECT_ROOT / "tests/fixtures/official-source-scan"


class OfficialSourceScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "operations").mkdir()
        shutil.copytree(
            PROJECT_ROOT / "operations/templates",
            self.root / "operations/templates",
        )
        initialize_or_migrate_workspace(self.root)
        # The project default is no J-Quants.  These fixture tests opt in only
        # to keep the dormant adapter covered without changing that default.
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
        portfolio = self.root / "operations/private/portfolio-register.csv"
        with portfolio.open(encoding="utf-8", newline="") as source:
            fields = next(csv.reader(source))
        position = dict.fromkeys(fields, "")
        position.update({"code": "1234", "company": "Example", "status": "OPEN"})
        with portfolio.open("a", encoding="utf-8", newline="") as destination:
            csv.DictWriter(destination, fieldnames=fields).writerow(position)
        write_price_archive(self.root, ["1234"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _prepare(self) -> dict[str, object]:
        return prepare_run(at="2026-08-31T18:30:00+09:00", root=self.root)

    def test_fixture_scan_builds_evidence_health_and_research_queue(self) -> None:
        prepared = self._prepare()
        result = scan_sources(
            run_id="2026-08-31",
            run_token=str(prepared["run_token"]),
            cutoff="2026-08-31T18:30:00+09:00",
            at="2026-08-31T18:45:00+09:00",
            fixture_dir=FIXTURES,
            root=self.root,
            environ={},
        )
        run_dir = self.root / str(prepared["run_dir"])
        health = json.loads(
            (run_dir / "provider-health.json").read_text(encoding="utf-8")
        )
        coverage = json.loads(
            (run_dir / "coverage.json").read_text(encoding="utf-8")
        )
        queue = json.loads(
            (run_dir / "research-queue.json").read_text(encoding="utf-8")
        )
        with (run_dir / "sources.csv").open(encoding="utf-8", newline="") as source:
            sources = list(csv.DictReader(source))

        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(health["providers"]["edinet"]["status"], "OK")
        self.assertEqual(
            health["providers"]["yahoo_tracked_archive"]["status"], "OK"
        )
        self.assertEqual(health["providers"]["jpx_listed_master"]["status"], "OK")
        self.assertEqual(health["providers"]["jquants_market_data"]["status"], "OK")
        self.assertEqual(health["providers"]["jquants_financials"]["status"], "OK")
        self.assertEqual(
            health["providers"]["jquants_earnings_calendar"]["status"], "OK"
        )
        self.assertEqual(
            health["providers"]["jquants_trading_calendar"]["status"], "OK"
        )
        self.assertEqual(
            health["providers"]["first_party_public_checks"]["status"], "PENDING"
        )
        self.assertEqual(coverage["official_sources"]["edinet"]["status"], "CHECKED")
        self.assertEqual(coverage["official_sources"]["tdnet"]["status"], "PENDING")
        self.assertEqual(coverage["official_sources"]["company_ir"]["status"], "PENDING")
        self.assertIn("edinet-S100TEST", {row["source_id"] for row in sources})
        self.assertIn(
            "manual-company-ir-2026-08-31-1234",
            {task["task_id"] for task in queue["tasks"]},
        )
        self.assertIn(
            "manual-tdnet-2026-08-31",
            {task["task_id"] for task in queue["tasks"]},
        )
        self.assertNotIn(
            "manual-official_market_data-2026-08-31",
            {task["task_id"] for task in queue["tasks"]},
        )
        self.assertNotIn(
            "manual-trading_calendar-2026-08-31",
            {task["task_id"] for task in queue["tasks"]},
        )
        self.assertTrue((run_dir / "raw-sources/edinet-2026-08-31.json").is_file())
        self.assertTrue((run_dir / "reference-data/manifest.json").is_file())
        self.assertTrue((run_dir / "reference-data/liquidity-20d.csv").is_file())
        self.assertTrue((run_dir / "trading-calendar.json").is_file())
        self.assertGreater(
            (self.root / "operations/private/forecast-history.csv").stat().st_size,
            len("source_id\n"),
        )
        self.assertGreater(
            (self.root / "operations/private/share-count-history.csv").stat().st_size,
            len("source_id\n"),
        )
        self.assertGreater(
            (self.root / "operations/private/earnings-calendar-history.csv").stat().st_size,
            len("source_id\n"),
        )

    def test_missing_credentials_create_blocking_gaps_without_network(self) -> None:
        prepared = self._prepare()
        config_path = self.root / "operations/private/source-config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["jpx_listed_master"]["enabled"] = False
        config_path.write_text(json.dumps(config) + "\n", encoding="utf-8")
        result = scan_sources(
            run_id="2026-08-31",
            run_token=str(prepared["run_token"]),
            cutoff="2026-08-31T18:30:00+09:00",
            at="2026-08-31T18:45:00+09:00",
            root=self.root,
            environ={},
        )
        run_dir = self.root / str(prepared["run_dir"])
        coverage = json.loads(
            (run_dir / "coverage.json").read_text(encoding="utf-8")
        )

        self.assertEqual(result["status"], "PARTIAL")
        self.assertGreaterEqual(result["blocking_gap_count"], 4)
        self.assertEqual(
            coverage["official_sources"]["edinet"]["status"], "UNAVAILABLE"
        )
        self.assertEqual(coverage["official_sources"]["tdnet"]["status"], "PENDING")
        self.assertTrue(
            all(gap["severity"] == "CRITICAL" for gap in coverage["data_gaps"])
        )

    def test_source_append_deduplicates_within_one_batch(self) -> None:
        path = self.root / "sources.csv"
        with path.open("w", encoding="utf-8", newline="") as destination:
            csv.DictWriter(destination, fieldnames=SOURCE_FIELDS).writeheader()
        row = dict.fromkeys(SOURCE_FIELDS, "")
        row["source_id"] = "same-source"

        added = _append_sources(path, [row, dict(row)])

        with path.open(encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(added, 1)
        self.assertEqual(len(rows), 1)

    @patch("scripts.official_source_scan.time_module.sleep")
    @patch("scripts.official_source_scan.urlopen")
    def test_transient_network_error_is_retried(self, mocked_urlopen, mocked_sleep) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"results": []}'
        mocked_urlopen.side_effect = [URLError("temporary"), response]

        payload = _request_json(
            base_url="https://example.com",
            path="documents.json",
            params={},
            headers={},
            timeout=1,
            backoff_seconds=0,
        )

        self.assertEqual(payload, {"results": []})
        self.assertEqual(mocked_urlopen.call_count, 2)
        mocked_sleep.assert_called_once()

    @patch("scripts.official_source_scan.urlopen")
    def test_rejected_key_answered_with_http_200_is_an_error(self, mocked_urlopen) -> None:
        # EDINET answers an invalid or expired subscription key with HTTP 200 and
        # a body whose StatusCode is 401. Reading only the HTTP status would turn
        # a rejected request into a day that simply has no filings.
        response = MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"StatusCode": 401,"message": "Access denied due to invalid '
            b'subscription key.Make sure to provide a valid key for an active '
            b'subscription."}'
        )
        mocked_urlopen.return_value = response

        with self.assertRaises(SourceScanError) as raised:
            _request_json(
                base_url="https://example.com",
                path="documents.json",
                params={},
                headers={},
                timeout=1,
                backoff_seconds=0,
            )

        self.assertIn("401", str(raised.exception))

    @patch("scripts.official_source_scan.urlopen")
    def test_metadata_status_other_than_200_is_an_error(self, mocked_urlopen) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"metadata": {"status": "404", "message": "Not Found"}}'
        )
        mocked_urlopen.return_value = response

        with self.assertRaises(SourceScanError):
            _request_json(
                base_url="https://example.com",
                path="documents.json",
                params={},
                headers={},
                timeout=1,
                backoff_seconds=0,
            )

    @patch("scripts.official_source_scan.urlopen")
    def test_successful_metadata_with_no_filings_is_accepted(self, mocked_urlopen) -> None:
        # A market holiday returns status 200 with an empty result set.
        response = MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"metadata": {"status": "200", "message": "OK"}, "results": []}'
        )
        mocked_urlopen.return_value = response

        payload = _request_json(
            base_url="https://example.com",
            path="documents.json",
            params={},
            headers={},
            timeout=1,
            backoff_seconds=0,
        )

        self.assertEqual(payload["results"], [])

    def test_carried_critical_gap_keeps_scan_partial(self) -> None:
        prepared = self._prepare()
        run_dir = self.root / str(prepared["run_dir"])
        gap = {
            "gap_id": "carried-company-ir",
            "status": "OPEN",
            "severity": "CRITICAL",
            "source": "company_ir",
            "impact": "manual IR check remains incomplete",
            "retry_after_jst": "2026-09-01T18:30:00+09:00",
            "resolved_at_jst": None,
            "resolution_evidence": None,
        }
        for name in ("coverage.json", "handoff.json"):
            path = run_dir / name
            document = json.loads(path.read_text(encoding="utf-8"))
            document["data_gaps"] = [gap]
            path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        result = scan_sources(
            run_id="2026-08-31",
            run_token=str(prepared["run_token"]),
            cutoff="2026-08-31T18:30:00+09:00",
            at="2026-08-31T18:45:00+09:00",
            fixture_dir=FIXTURES,
            root=self.root,
            environ={},
        )

        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["blocking_gap_count"], 2)

    def test_agent_can_close_fixture_run_after_manual_primary_checks(self) -> None:
        prepared = self._prepare()
        scan_sources(
            run_id="2026-08-31",
            run_token=str(prepared["run_token"]),
            cutoff="2026-08-31T18:30:00+09:00",
            at="2026-08-31T18:45:00+09:00",
            fixture_dir=FIXTURES,
            root=self.root,
            environ={},
        )
        run_dir = self.root / str(prepared["run_dir"])
        with (run_dir / "sources.csv").open("a", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=(
                "source_id", "category", "code", "title", "published_at_jst",
                "retrieved_at_jst", "url", "primary_source", "used_for_decision", "notes",
            ))
            common = {
                "published_at_jst": "2026-08-31T18:00:00+09:00",
                "retrieved_at_jst": "2026-08-31T18:50:00+09:00",
                "primary_source": "true",
                "used_for_decision": "true",
                "notes": "manual fixture evidence",
            }
            writer.writerow({
                **common,
                "source_id": "company-ir-1234",
                "category": "company_ir",
                "code": "1234",
                "title": "Company IR checked",
                "url": "https://example.com/company-ir/1234",
            })
            writer.writerow({
                **common,
                "source_id": "jpx-notices-check",
                "category": "jpx",
                "code": "",
                "title": "JPX notices checked",
                "url": "https://www.jpx.co.jp/",
            })
            writer.writerow({
                **common,
                "source_id": "tdnet-query-check",
                "category": "tdnet",
                "code": "",
                "title": "TDnet checked through cutoff",
                "url": "https://www.release.tdnet.info/inbs/I_main_00.html",
            })

        queue_path = run_dir / "research-queue.json"
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        for task in queue["tasks"]:
            task["status"] = "COMPLETED"
            task["evidence_source_ids"] = task["source_ids"] or [
                "company-ir-1234" if task["code"] else "jpx-notices-check"
            ]
        queue_path.write_text(
            json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        coverage_path = run_dir / "coverage.json"
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        coverage["status"] = "COMPLETED"
        coverage["source_window"]["through_inclusive_jst"] = (
            "2026-08-31T18:30:00+09:00"
        )
        coverage["universe"]["holdings"]["checked"] = ["1234"]
        coverage["official_sources"]["company_ir"]["status"] = "CHECKED"
        coverage["official_sources"]["tdnet"]["status"] = "CHECKED"
        coverage["official_sources"]["jpx"]["status"] = "CHECKED"
        for gap in coverage["data_gaps"]:
            gap["status"] = "RESOLVED"
            gap["resolved_at_jst"] = "2026-08-31T19:00:00+09:00"
            gap["resolution_evidence"] = "jpx-notices-check"
        coverage["completed_at_jst"] = "2026-08-31T19:00:00+09:00"
        coverage_path.write_text(
            json.dumps(coverage, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        handoff_path = run_dir / "handoff.json"
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        for gap in handoff["data_gaps"]:
            gap["status"] = "RESOLVED"
            gap["resolved_at_jst"] = "2026-08-31T19:00:00+09:00"
            gap["resolution_evidence"] = "jpx-notices-check"
        handoff_path.write_text(
            json.dumps(handoff, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        report_path = run_dir / "report.md"
        report = report_path.read_text(encoding="utf-8")
        report = (
            report.replace("- 実行状態: `IN-PROGRESS`", "- 実行状態: `COMPLETED`")
            .replace(
                "- 今回の開示カットオフ（JST）: 未確定",
                "- 今回の開示カットオフ（JST）: 2026-08-31T18:30:00+09:00",
            )
            .replace("- 株価基準日: 未確定", "- 株価基準日: 2026-08-31")
            .replace("- 総合結果: 未確定", "- 総合結果: 確認完了")
        )
        report_path.write_text(report, encoding="utf-8")

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
            task["evidence_source_ids"] = ["company-ir-1234"]
        plan_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (run_dir / "research-results.md").write_text(
            "# 夜間調査結果\n\n- 状態: `COMPLETED`\n"
            "- 情報カットオフ（JST）: 2026-08-31T18:30:00+09:00\n"
            "- 翌営業日: 2026-09-01\n- 対象件数: 1\n- 未解決事項: なし\n\n"
            "## 調査結果\n\n一次資料を確認し、継続保有と判定。\n",
            encoding="utf-8",
        )
        (run_dir / "global-risk.md").write_text(
            "# 世界情勢・市場リスク確認\n\n- 状態: `COMPLETED`\n"
            "- 情報カットオフ（JST）: 2026-08-31T18:30:00+09:00\n"
            "- 判定: `NORMAL`\n\n公的情報を確認済み。\n",
            encoding="utf-8",
        )
        actions_path = run_dir / "next-day-actions.csv"
        with actions_path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            fields = list(reader.fieldnames or [])
            actions = list(reader)
        actions[0].update(
            {
                "next_action": "KEEP",
                "rule_ids": "H-1",
                "trigger_condition": "即時撤退条件なし",
                "human_action": "なし",
                "evidence_source_ids": "company-ir-1234;jpx-notices-check",
            }
        )
        with actions_path.open("w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=fields)
            writer.writeheader()
            writer.writerows(actions)

        result = complete_run(
            run_id="2026-08-31",
            completed_at="2026-08-31T19:00:00+09:00",
            source_cutoff="2026-08-31T18:30:00+09:00",
            price_date="2026-08-31",
            summary="fixture scan and manual primary checks completed",
            run_token=str(prepared["run_token"]),
            root=self.root,
        )

        self.assertEqual(result["status"], "completed")


if __name__ == "__main__":
    unittest.main()
