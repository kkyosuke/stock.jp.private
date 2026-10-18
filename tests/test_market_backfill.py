"""Historical selection never silently allocates per-date provider allowances."""
import copy
from datetime import date, datetime
import unittest
from unittest.mock import patch

from stock_jp.automation import market_backfill as b, nightly as n, external_market as e
from stock_jp.market_data.jquants import ingest
from tests import test_external_market as fixture


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.support = fixture.ExternalMarketTest(methodName="runTest")
        self.support.setUp()
        self.addCleanup(self.support.doCleanups)
        self.root, self.repo, self.data = self.support.root, self.support.repo, self.support.fixture
        self.now = datetime.fromisoformat("2026-10-04T21:00:00+09:00")
        self.identity = {**self.support.identity, "date": "2026-10-04", "resolved_at": self.now.isoformat()}
        self.day = self.data.at.date()

    def seed_partial(self, endpoint="/indices/bars/daily/topix"):
        def provider(name, params):
            if name == endpoint:
                raise ingest.ApiError(0)
            return self.data.transport(name, params)
        with self.assertRaises(n.Incomplete), patch.object(ingest, "utc_now", return_value=self.data.at.isoformat()):
            n.execute(self.root, self.support.support.context, self.repo, at=self.data.at,
                      transport=provider, sleep=lambda _: None)
        self.data.calls.clear()

    def execute(self, days=None, *, mode="external", **kwargs):
        writer = {"identity": self.identity} if mode == "external" else {"context": self.support.support.context}
        with patch.object(ingest, "utc_now", return_value=self.now.isoformat()):
            return b.execute(self.root, self.repo, self.now, days or [self.day],
                             transport=self.data.transport, sleep=lambda _: None, **writer, **kwargs)

    def test_new_dates_block_without_writes_provider_or_new_budgets(self):
        before = copy.deepcopy(self.repo.journal())
        self.repo.calls.clear()
        report = self.execute([date(2026, 10, 1), date(2026, 10, 2)])
        self.assertEqual(report["status"], "BLOCKED")
        self.assertTrue(all(row["action"] == "BUDGET_ALLOCATION_REQUIRED" for row in report["targets"]))
        self.assertEqual(self.repo.journal(), before)
        self.assertFalse(self.data.calls)
        self.assertFalse(any(method != "GET" for method, _, _ in self.repo.calls))

    def test_date_and_contract_validation(self):
        config = self.data.config
        for args in ({}, {"target": date(2026, 10, 4)}, {"target": date(2026, 10, 5)},
                     {"start": date(2026, 10, 2)},
                     {"start": date(2026, 10, 3), "through": date(2026, 10, 2)},
                     {"start": date(2026, 8, 1), "through": date(2026, 10, 2)},
                     {"target": date(2010, 1, 1)},
                     {"target": self.day, "start": self.day, "through": self.day}):
            with self.subTest(args=args), self.assertRaises(n.RemoteStop):
                b.targets(config, self.now, **args)
        self.assertEqual(b.targets(config, self.now, start=self.day, through=date(2026, 10, 3)),
                         [self.day, date(2026, 10, 3)])
        with self.assertRaises(n.RemoteStop):
            b.targets(config, self.now.replace(tzinfo=None), target=self.day)

    def test_partial_saved_run_resumes_in_both_modes_without_reset(self):
        self.seed_partial()
        before = copy.deepcopy(self.repo.journal()["runs"][self.day.isoformat()]["requests"])
        report = self.execute()
        self.assertEqual(report["status"], "DATA_PR_CREATED")
        self.assertEqual(report["requests_for_date"], len(before) + 1)
        self.assertEqual(self.data.calls, [("/indices/bars/daily/topix", {"from": "2026-10-02", "to": "2026-10-02"})])
        self.data.calls.clear()
        self.execute(mode="actions")
        self.assertFalse(self.data.calls)
        self.assertEqual(self.repo.journal()["runs"][self.day.isoformat()]["requests"][:len(before)], before)
        self.assertFalse(any("dispatches" in path or path.endswith("/merge") for _, path, _ in self.repo.calls))

    def test_missing_current_only_receipt_does_not_fetch_present_feed_as_past(self):
        self.seed_partial("/equities/earnings-calendar")
        before = copy.deepcopy(self.repo.journal())
        result = self.execute()
        self.assertEqual(result["targets"][0]["action"], "SOURCE_SCOPE_REQUIRED")
        self.assertEqual(self.repo.journal(), before)
        self.assertFalse(self.data.calls)

    def test_plan_only_preserves_partial_state_and_per_date_consumption(self):
        self.seed_partial()
        before = copy.deepcopy(self.repo.journal())
        result = self.execute(plan_only=True)
        self.assertEqual(result["status"], "PLAN_READY")
        self.assertEqual(result["targets"][0]["remaining"], 3)
        self.assertEqual(self.repo.journal(), before)
        self.assertFalse(self.data.calls)

    def test_shared_paper_exhaustion_blocks_without_fence_or_budget_reset(self):
        self.seed_partial()
        cp = n.Checkpoint(self.root, n.Git(self.repo), self.identity["main"], self.day, self.support.support.context)
        used = len(cp.document["runs"][self.day.isoformat()]["requests"])
        cp.document["paper_requests"] = {self.day.isoformat(): [
            {"sequence": i, "host": "example.jp", "reserved_at": self.data.at.isoformat(),
             "action_run_id": 123, "execution_sha": self.identity["main"]} for i in range(1, 13-used)]}
        cp.flush()
        before = copy.deepcopy(self.repo.journal())
        result = self.execute()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["targets"][0]["remaining"], 0)
        self.assertEqual(self.repo.journal(), before)
        self.assertFalse(self.data.calls)

    def test_other_writer_and_noncontiguous_range_rejected(self):
        self.seed_partial()
        cp = e.Checkpoint(self.root, n.Git(self.repo), self.identity["main"],
                          {**self.identity, "session": "b"*32}, verify=lambda: None)
        cp.claim()
        with self.assertRaises(n.RemoteStop):
            self.execute()
        with self.assertRaises(n.RemoteStop):
            self.execute(mode="actions")
        with self.assertRaises(n.RemoteStop):
            self.execute([date(2026, 10, 1), date(2026, 10, 3)])
        self.assertFalse(self.data.calls)

    def test_missing_budget_basis_is_not_created_by_backfill(self):
        self.seed_partial()
        cp = n.Checkpoint(self.root, n.Git(self.repo), self.identity["main"], self.day, self.support.support.context)
        del cp.document["provider_debit_bases"]
        cp.flush()
        before = copy.deepcopy(self.repo.journal())
        with self.assertRaisesRegex(n.RemoteStop, "lacks a verified debit basis"):
            self.execute()
        self.assertEqual(self.repo.journal(), before)

    def test_weekend_journal_reuse_makes_no_trading_day(self):
        saturday = datetime.fromisoformat("2026-10-03T21:00:00+09:00")
        with patch.object(ingest, "utc_now", return_value=saturday.isoformat()):
            cp = n.Checkpoint(self.root, n.Git(self.repo), self.identity["main"], saturday.date(), self.support.support.context)
            result = n.daily.run(self.root, at=saturday, remote=cp, transport=self.data.transport, sleep=lambda _: None)
        self.assertEqual(result["status"], "HOLIDAY")
        self.data.calls.clear()
        result = self.execute([saturday.date()])
        self.assertIsNone(result["backfill_results"][0]["data_date"])
        self.assertFalse(self.data.calls)

    def test_paginated_partial_resume_preserves_saved_page_and_failed_charge(self):
        def provider(endpoint, params):
            if endpoint == "/indices/bars/daily/topix":
                if params.get("pagination_key"):
                    raise ingest.ApiError(0)
                return {"data": [], "pagination_key": "page-two"}
            return self.data.transport(endpoint, params)
        with self.assertRaises(n.Incomplete), patch.object(ingest, "utc_now", return_value=self.data.at.isoformat()):
            n.execute(self.root, self.support.support.context, self.repo, at=self.data.at,
                      transport=provider, sleep=lambda _: None)
        before = copy.deepcopy(self.repo.journal()["runs"][self.day.isoformat()]["requests"])
        self.data.calls.clear()
        result = self.execute(mode="actions")
        self.assertEqual(result["requests_for_date"], len(before) + 1)
        self.assertEqual(len(self.data.calls), 1)
        self.assertEqual(self.data.calls[0][1]["pagination_key"], "page-two")

    def test_completed_historical_range_does_not_regress_latest_prices(self):
        for stamp in ("2026-10-02T21:00:00+09:00", "2026-10-03T21:00:00+09:00", "2026-10-05T21:00:00+09:00"):
            at = datetime.fromisoformat(stamp)
            cp = n.Checkpoint(self.root, n.Git(self.repo), self.identity["main"], at.date(), self.support.support.context)
            with patch.object(ingest, "utc_now", return_value=at.isoformat()):
                n.daily.run(self.root, at=at, remote=cp, transport=self.data.transport, sleep=lambda _: None)
        latest = self.root / ".work/market-data/daily-prices/latest.json"
        before = latest.read_bytes()
        self.data.calls.clear()
        result = self.execute([self.day, date(2026, 10, 3)])
        self.assertEqual([row["run_date"] for row in result["backfill_results"]], ["2026-10-02", "2026-10-03"])
        self.assertEqual(latest.read_bytes(), before)
        self.assertFalse(self.data.calls)
