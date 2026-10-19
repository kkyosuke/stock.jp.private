"""External acquisition uses the real collector and synthetic Git/provider I/O."""
import copy
from datetime import datetime
import json
import subprocess
import unittest
from unittest.mock import patch

from stock_jp.automation import external_market as e, nightly as n
from stock_jp.automation import financial_history as financial
from stock_jp.market_data.jquants import daily, ingest
from tests import test_nightly_market_data as nightly_fixture


class ExternalRepository(nightly_fixture.Repository):
    active = False

    def __call__(self, method, path, body=None):
        if path.startswith(n.PREFIX + "/actions/runs?"):
            self.calls.append((method, path, body))
            return {"total_count": int(self.active), "workflow_runs": [{}] if self.active else []}
        return super().__call__(method, path, body)


class ExternalMarketTest(unittest.TestCase):
    def setUp(self):
        self.support = nightly_fixture.NightlyMarketDataTest(methodName="runTest")
        self.support.setUp()
        self.addCleanup(self.support.doCleanups)
        self.root, self.fixture = self.support.root, self.support.fixture
        self.repo = ExternalRepository(self.root)
        cp = n.Checkpoint(self.root, n.Git(self.repo), self.repo.refs["main"],
                          self.fixture.at.date(), self.support.context)
        cp.flush(force=True)
        self.identity = {"kind": "external-market-v1", "session": "a" * 32,
                         "main": self.repo.refs["main"], "date": self.fixture.at.date().isoformat(),
                         "resolved_at": self.fixture.at.isoformat()}
        self.addCleanup(patch.stopall)
        patch.object(e, "checkout_main").start()

    def run_external(self, provider=None, identity=None, at=None):
        with patch.object(ingest, "utc_now", return_value=self.fixture.at.isoformat()):
            return e.execute(self.root, self.repo, identity or self.identity,
                             at=at or self.fixture.at, transport=provider or self.fixture.transport,
                             sleep=lambda _: None)

    def test_real_collection_pr_only_and_same_day_resume_no_fetch(self):
        def provider(endpoint, params):
            journal = self.repo.journal()
            self.assertEqual(journal["external_writer"], self.identity)
            state = journal["runs"][self.identity["date"]]
            self.assertEqual(len(state["requests"]), len(self.fixture.calls) + 1)
            return self.fixture.transport(endpoint, params)
        report = self.run_external(provider)
        self.assertEqual(report["status"], "DATA_PR_CREATED")
        self.assertEqual(report["required_ci"], "NOT_VERIFIED")
        self.assertNotIn("external_writer", self.repo.journal())
        calls = len(self.fixture.calls)
        self.run_external()
        self.assertEqual(len(self.fixture.calls), calls)
        self.assertEqual(self.repo.refs["main"], self.identity["main"])
        self.assertFalse(any("dispatches" in path or path.endswith(("/merge", "/approve"))
                             for _, path, _ in self.repo.calls))
        base = self.repo.tree(self.identity["main"])
        final = self.repo.tree(report["head"])
        changed = {p for p in base.keys() | final.keys() if base.get(p) != final.get(p)}
        self.assertTrue(all(p.startswith(("data/market/", n.RECORDS)) for p in changed))

    def test_actions_active_before_claim_no_mutation_or_provider(self):
        self.repo.active = True
        old = self.repo.refs[n.CHECKPOINT]
        with self.assertRaisesRegex(n.RemoteStop, "Actions run"):
            self.run_external()
        self.assertEqual(self.repo.refs[n.CHECKPOINT], old)
        self.assertFalse(self.fixture.calls)

    def test_failed_durable_claim_never_calls_provider(self):
        self.repo.fail_patch = True
        with self.assertRaises(n.RemoteStop):
            self.run_external()
        self.assertFalse(self.fixture.calls)

    def test_crash_keeps_reservation_and_fence_blocks_other_writers(self):
        def crash(*_):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_external(crash)
        saved = copy.deepcopy(self.repo.journal())
        self.assertEqual(len(saved["runs"][self.identity["date"]]["requests"]), 1)
        with self.assertRaisesRegex(n.RemoteStop, "another external"):
            self.run_external(identity={**self.identity, "session": "b" * 32})
        with self.assertRaisesRegex(n.RemoteStop, "external market-data writer"):
            n.Checkpoint(self.root, n.Git(self.repo), self.identity["main"],
                         self.fixture.at.date(), self.support.context)
        with self.assertRaisesRegex(n.RemoteStop, "external market-data writer"):
            e.reject_external_writer(n.Git(self.repo))
        checkpoint = object.__new__(financial.Checkpoint)
        checkpoint.git = n.Git(self.repo)
        with self.assertRaisesRegex(n.RemoteStop, "external market-data writer"):
            checkpoint.active_run()
        report = self.run_external()
        self.assertEqual(report["requests_for_date"], 10)

    def test_twelve_unknown_outcomes_cannot_reset_daily_allowance(self):
        def fail(*_):
            raise ingest.ApiError(0)
        for count in range(1, 13):
            with self.assertRaises(n.Incomplete):
                self.run_external(fail)
            self.assertEqual(len(self.repo.journal()["runs"][self.identity["date"]]["requests"]), count)
        with self.assertRaises(n.Incomplete) as caught:
            self.run_external()
        self.assertEqual(caught.exception.result["status"], "BUDGET_EXHAUSTED")
        self.assertFalse(self.fixture.calls)

    def test_paper_charges_share_the_existing_twelve_request_limit(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_external(lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        cp = e.Checkpoint(self.root, n.Git(self.repo), self.identity["main"], self.identity,
                          verify=lambda: None)
        day = self.identity["date"]
        cp.document["paper_requests"] = {day: [
            {"sequence": i, "host": "example.jp", "reserved_at": self.fixture.at.isoformat(),
             "action_run_id": 123, "execution_sha": self.identity["main"]} for i in range(1, 12)]}
        cp.flush()
        before = copy.deepcopy(self.repo.journal()["paper_requests"])
        with self.assertRaises(n.Incomplete) as caught:
            self.run_external()
        self.assertEqual(caught.exception.result["requests_for_date"], 12)
        self.assertEqual(self.repo.journal()["paper_requests"], before)
        self.assertFalse(self.fixture.calls)

    def test_unknown_reservation_acknowledgement_stops_without_refund(self):
        original = n.Git.write
        def ambiguous(git, *args, **kwargs):
            result = original(git, *args, **kwargs)
            runs = self.repo.journal()["runs"]
            if runs.get(self.identity["date"], {}).get("requests"):
                raise n.RemoteStop("synthetic unknown checkpoint acknowledgement")
            return result
        with patch.object(n.Git, "write", ambiguous):
            with self.assertRaises(n.RemoteStop):
                self.run_external()
        self.assertFalse(self.fixture.calls)
        self.assertEqual(len(self.repo.journal()["runs"][self.identity["date"]]["requests"]), 1)
        self.assertEqual(self.run_external()["requests_for_date"], 10)

    def test_restart_across_midnight_preserves_logical_date(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_external(lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        report = self.run_external(at=datetime.fromisoformat("2026-10-03T06:00:00+09:00"))
        self.assertEqual(report["run_date"], "2026-10-02")
        self.assertEqual(report["requests_for_date"], 10)

    def test_sunday_morning_pins_saturday_and_creates_no_trading_day(self):
        at = datetime.fromisoformat("2026-10-04T07:08:00+09:00")
        identity = {**self.identity, "date": "2026-10-03", "resolved_at": at.isoformat()}
        report = self.run_external(identity=identity, at=at)
        self.assertEqual(report["data_status"], "HOLIDAY")
        self.assertIsNone(report["data_date"])
        self.assertEqual(report["requests_for_date"], 1)

    def test_main_moves_after_reservation_no_provider_io(self):
        original = e.Checkpoint.before_provider_io
        def moved(cp):
            self.repo.refs["main"] = "f" * 40
            original(cp)
        with patch.object(e.Checkpoint, "before_provider_io", moved):
            with self.assertRaises(n.RemoteStop):
                self.run_external()
        self.assertFalse(self.fixture.calls)
        self.assertEqual(len(self.repo.journal()["runs"][self.identity["date"]]["requests"]), 1)

    def test_actions_appear_after_reservation_no_provider_io(self):
        original = e.Checkpoint.before_provider_io
        def appeared(cp):
            self.repo.active = True
            original(cp)
        with patch.object(e.Checkpoint, "before_provider_io", appeared):
            with self.assertRaises(n.RemoteStop):
                self.run_external()
        self.assertFalse(self.fixture.calls)
        self.assertEqual(len(self.repo.journal()["runs"][self.identity["date"]]["requests"]), 1)

    def test_session_receipt_and_local_lock(self):
        with e.session(self.root, "recovery", self.identity["main"], self.fixture.at) as first:
            with self.assertRaisesRegex(n.RemoteStop, "another local"):
                with e.session(self.root, "other", self.identity["main"], self.fixture.at):
                    self.fail("concurrent local writer")
        with e.session(self.root, "recovery", self.identity["main"], self.fixture.at) as resumed:
            self.assertEqual(first, resumed)
        with self.assertRaisesRegex(n.RemoteStop, "main changed"):
            with e.session(self.root, "recovery", "f" * 40, self.fixture.at):
                self.fail("changed main")

    def test_interrupted_actions_to_external_preserves_charge_and_resumes(self):
        context = {**self.support.context, "sha": self.identity["main"]}
        with self.assertRaises(n.Incomplete):
            n.execute(self.root, context, self.repo, at=self.fixture.at,
                      transport=lambda *_: (_ for _ in ()).throw(ingest.ApiError(0)), sleep=lambda _: None)
        original = copy.deepcopy(self.repo.journal()["runs"][self.identity["date"]]["requests"])
        report = self.run_external()
        self.assertEqual(report["requests_for_date"], 10)
        self.assertEqual(self.repo.journal()["runs"][self.identity["date"]]["requests"][:1], original)

    def test_interrupted_external_explicit_handoff_to_actions_retains_charge(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_external(lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        before = copy.deepcopy(self.repo.journal())
        result = e.handoff(self.root, self.repo, self.identity)
        self.assertEqual(result["status"], "EXTERNAL_WRITER_RELEASED")
        self.assertFalse(result["actions_dispatched"])
        after = self.repo.journal()
        self.assertNotIn("external_writer", after)
        self.assertEqual({k: v for k, v in before.items() if k != "external_writer"}, after)
        context = {**self.support.context, "sha": self.identity["main"]}
        with patch.object(ingest, "utc_now", return_value=self.fixture.at.isoformat()):
            report = n.execute(self.root, context, self.repo, at=self.fixture.at, preview=True,
                               transport=self.fixture.transport, sleep=lambda _: None)
        self.assertEqual(report["status"], "PREVIEW_COMPLETE")
        self.assertEqual(report["requests_for_date"], 10)
        self.assertEqual(after["runs"][self.identity["date"]]["requests"],
                         self.repo.journal()["runs"][self.identity["date"]]["requests"][:1])

    def test_completed_external_to_actions_reuses_without_provider_calls(self):
        external = self.run_external()
        calls = len(self.fixture.calls)
        context = {**self.support.context, "sha": self.identity["main"]}
        report = n.execute(self.root, context, self.repo, at=self.fixture.at, preview=True,
                           transport=self.fixture.transport, sleep=lambda _: None)
        self.assertEqual(report["requests_for_date"], external["requests_for_date"])
        self.assertEqual(len(self.fixture.calls), calls)

    def test_handoff_rejects_other_session_or_active_actions_without_mutation(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_external(lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        revision = self.repo.refs[n.CHECKPOINT]
        with self.assertRaises(n.RemoteStop):
            e.handoff(self.root, self.repo, {**self.identity, "session": "b" * 32})
        self.repo.active = True
        with self.assertRaises(n.RemoteStop):
            e.handoff(self.root, self.repo, self.identity)
        self.assertEqual(self.repo.refs[n.CHECKPOINT], revision)


class ExternalBoundaryTest(unittest.TestCase):
    def test_transport_cannot_dispatch_approve_merge_or_write_other_checkpoint(self):
        transport = e.Transport("synthetic-test-token")
        cases = [("POST", "/actions/workflows/ci.yml/dispatches", {}),
                 ("PUT", "/pulls/1/merge", {}), ("POST", "/actions/runs/1/approve", None),
                 ("PATCH", "/git/refs/heads/" + n.PAPER_CHECKPOINT, {"force": False}),
                 ("POST", "/git/refs", {"ref": "refs/heads/" + n.CHECKPOINT})]
        with patch.object(transport.http, "json") as network:
            for method, path, body in cases:
                with self.subTest(path=path), self.assertRaises(n.RemoteStop):
                    transport(method, n.PREFIX + path, body)
            network.assert_not_called()

    def test_unmerged_checkout_fails_before_remote_mutation(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"]).decode().strip()
        with self.assertRaisesRegex(n.RemoteStop, "adopted on current main"):
            e.checkout_main(root, "0" * 40 if head != "0" * 40 else "f" * 40)
