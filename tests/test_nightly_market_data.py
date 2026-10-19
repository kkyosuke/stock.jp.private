"""Real collector/codec with synthetic provider data and an in-memory Git API."""
import base64
import copy
import contextlib
from datetime import date, datetime
import hashlib
import io
import json
import shutil
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from stock_jp.automation import nightly as n
from stock_jp.market_data.jquants import daily, ingest
import tests.test_jquants_daily as fixture


class Repository:
    def __init__(self, root):
        self.objects, self.refs, self.calls, self.pulls = {}, {}, [], []
        self.checks = [{"id": i, "name": name, "app": {"slug": "github-actions"},
                        "status": "completed", "conclusion": "success"}
                       for i, name in enumerate(sorted(n.CHECKS), 1)]
        self.fail_patch = False
        self.fail_dispatch = False
        self.ci_runs = {}
        self.run_change = {}
        self.reviews = []
        self.pull_ci_runs = []
        files = {p.relative_to(root).as_posix(): self.blob(p.read_bytes())
                 for p in root.rglob("*") if p.is_file() and ".work" not in p.parts}
        self.refs["main"] = self.commit(files)
        self.workflow_sha = self.refs["main"]

    def put(self, value):
        ident = hashlib.sha1(ingest.encoded(value)).hexdigest()
        self.objects[ident] = copy.deepcopy(value)
        return ident

    def blob(self, raw):
        ident = n.git_blob(raw)
        self.objects[ident] = {"encoding": "base64", "content": base64.b64encode(raw).decode()}
        return ident

    def commit(self, files, modes=None):
        tree = self.put({"tree": [{"path": p, "sha": s, "mode": (modes or {}).get(p, "100644"), "type": "blob"}
                                  for p, s in sorted(files.items())], "truncated": False})
        return self.put({"tree": {"sha": tree}, "parents": []})

    def tree(self, commit):
        return {e["path"]: e["sha"] for e in self.objects[self.objects[commit]["tree"]["sha"]]["tree"]}

    def journal(self):
        blob = self.tree(self.refs[n.CHECKPOINT])[n.JOURNAL]
        return json.loads(base64.b64decode(self.objects[blob]["content"]))

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        path = path.removeprefix(n.PREFIX)
        route = path.split("?")[0]
        if method == "GET":
            if path == "":
                return {"private": True, "owner": {"login": "KKyosuke"}, "permissions": {"push": True}}
            if route.startswith("/git/ref/heads/"):
                branch = route.removeprefix("/git/ref/heads/")
                if branch not in self.refs:
                    raise n.http.Rejected(404)
                return {"object": {"sha": self.refs[branch]}}
            if route.startswith(("/git/blobs/", "/git/trees/", "/git/commits/")):
                return copy.deepcopy(self.objects[route.rsplit("/", 1)[-1]])
            if route.startswith("/actions/runs/"):
                run_id = int(route.rsplit("/", 1)[-1])
                if run_id in self.ci_runs:
                    return {**copy.deepcopy(self.ci_runs[run_id]), **self.run_change}
                return {"status": "in_progress", "head_sha": self.workflow_sha, "path": n.WORKFLOW}
            if route == "/actions/workflows/ci.yml/runs":
                return {"workflow_runs": copy.deepcopy(self.pull_ci_runs)}
            if route.endswith("/check-runs"):
                if route.startswith("/check-suites/"):
                    suite_id = int(route.split("/")[2])
                    run = next(r for r in self.ci_runs.values() if r["check_suite_id"] == suite_id)
                    return {"check_runs": [{"head_sha": run["head_sha"], "check_suite": {"id": suite_id},
                                            **copy.deepcopy(c)} for c in self.checks]}
                return {"check_runs": copy.deepcopy(self.checks)}
            if route.endswith("/reviews"):
                return copy.deepcopy(self.reviews)
            if route == "/pulls":
                query = parse_qs(path.partition("?")[2])
                branch = query.get("head", [""])[0].partition(":")[2]
                return [copy.deepcopy(p) for p in self.pulls if p["state"] == "open"
                        and (not branch or p["head"]["ref"] == branch)]
            if route.startswith("/pulls/"):
                return copy.deepcopy(self.pulls[int(route.rsplit("/", 1)[-1]) - 1])
        if method == "POST":
            if route.endswith("/approve"):
                run_id = int(route.split("/")[-2])
                self.ci_runs[run_id].update(status="completed", conclusion="success")
                return {}
            if route == "/actions/workflows/ci.yml/dispatches":
                if self.fail_dispatch:
                    raise n.http.Rejected(403)
                run_id = 1000 + len(self.ci_runs)
                self.ci_runs[run_id] = {"id": run_id, "path": n.CI_WORKFLOW,
                    "event": "workflow_dispatch", "head_sha": self.refs[body["ref"]],
                    "head_branch": body["ref"], "status": "completed", "conclusion": "success",
                    "check_suite_id": 2000 + len(self.ci_runs)}
                return copy.deepcopy(getattr(self, "dispatch_response", {"workflow_run_id": run_id}))
            if route == "/git/blobs":
                return {"sha": self.blob(base64.b64decode(body["content"]))}
            if route == "/git/trees":
                tree = {e["path"]: e for e in self.objects[body["base_tree"]]["tree"]}
                for entry in body["tree"]:
                    entry = dict(entry)
                    if "content" in entry:
                        if "sha" in entry:
                            raise AssertionError("tree entry has both content and SHA")
                        entry["sha"] = self.blob(entry.pop("content").encode("utf-8"))
                    tree[entry["path"]] = entry
                return {"sha": self.put({"tree": list(tree.values()), "truncated": False})}
            if route == "/git/commits":
                return {"sha": self.put({"tree": {"sha": body["tree"]}, "parents": body["parents"]})}
            if route == "/git/refs":
                branch = body["ref"].removeprefix("refs/heads/")
                if branch in self.refs:
                    raise n.http.Rejected(422)
                self.refs[branch] = body["sha"]
                return {}
            if route == "/pulls":
                pr = {"number": len(self.pulls) + 1, "state": "open", "html_url": "synthetic-pr",
                      "draft": body["draft"],
                      "head": {"sha": self.refs[body["head"]], "ref": body["head"]}}
                self.pulls.append(pr)
                run_id = 3000 + len(self.pulls)
                run = {"id": run_id, "path": n.CI_WORKFLOW, "event": "pull_request",
                       "head_sha": pr["head"]["sha"], "head_branch": body["head"],
                       "status": "completed", "conclusion": "success", "check_suite_id": run_id + 1000,
                       "pull_requests": [{"number": pr["number"], "head": {"sha": pr["head"]["sha"]}}]}
                self.pull_ci_runs.append(run)
                self.ci_runs[run_id] = copy.deepcopy(run)
                return copy.deepcopy(pr)
        if method == "PATCH":
            if self.fail_patch:
                raise n.http.Rejected(503)
            if body["force"] is not False:
                raise AssertionError("force write")
            self.refs[route.removeprefix("/git/refs/heads/")] = body["sha"]
            return {}
        if method == "PUT" and route.endswith("/merge"):
            pr = self.pulls[int(route.split("/")[2]) - 1]
            if body != {"sha": pr["head"]["sha"], "merge_method": "squash"}:
                raise AssertionError("merge without exact head")
            self.refs["main"] = pr["head"]["sha"]
            pr["state"] = "closed"
            return {"merged": True, "sha": self.refs["main"]}
        raise AssertionError((method, path))


class NightlyMarketDataTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.DailyTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.fixture.config["market_data_git"] = {
            "automatic_upload": True, "repository": n.REPO, "owner_only_access_confirmed": True,
            "store_root": "data/market/jquants"}
        self.fixture.save_config()
        ingest.atomic_json(self.root / "config/policy.json",
                           {"operation_mode": "PAPER", "broker_submission": "HUMAN_ONLY"})
        self.fixture.archive.parent.mkdir(parents=True)
        n.storage.pack(self.fixture.archive.parent, self.root / "data/market/jquants")
        self.repo = Repository(self.root)
        self.context = {"repository": n.REPO, "event": "workflow_dispatch", "ref": "refs/heads/main",
                        "sha": self.repo.refs["main"], "actor": "KKyosuke",
                        "triggering_actor": "KKyosuke", "run_id": 123}

    def run_preview(self, provider=None):
        with patch.object(ingest, "utc_now", return_value=self.fixture.at.isoformat()):
            return n.execute(self.root, self.context, self.repo, preview=True,
                             at=self.fixture.at, transport=provider or self.fixture.transport,
                             sleep=lambda _: None)

    def checkpoint(self):
        checkpoint = n.Checkpoint(self.root, n.Git(self.repo), self.repo.refs["main"],
                                  self.fixture.at.date(), self.context)
        checkpoint.prepare(self.fixture.archive.parent)
        return checkpoint

    def test_schedule_boundaries_and_timezone(self):
        for timestamp, expected in (
            ("2026-10-04T20:59:00+09:00", "2026-10-03"),
            ("2026-10-04T21:00:00+09:00", "2026-10-04"),
            ("2026-10-04T23:59:00+09:00", "2026-10-04"),
            ("2026-10-05T00:01:00+09:00", "2026-10-04"),
            ("2026-10-04T15:01:00+00:00", "2026-10-04"),
        ):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(n.scheduled_day(datetime.fromisoformat(timestamp)).isoformat(), expected)

    def test_schedule_binding_survives_failed_prepare_and_next_day_retry(self):
        self.context["event"] = "schedule"
        def stopped(root, *, remote, **kwargs):
            self.assertEqual(remote.target_day, date(2026, 10, 4))
            self.assertEqual(self.repo.journal()["scheduled_targets"]["123"]["date"], "2026-10-04")
            raise n.RemoteStop("synthetic preparation stop")
        for timestamp in ("2026-10-05T00:01:00+09:00", "2026-10-06T23:59:00+09:00"):
            with patch.object(daily, "run", side_effect=stopped):
                with self.assertRaisesRegex(n.RemoteStop, "synthetic preparation stop"):
                    n.execute(self.root, self.context, self.repo, at=datetime.fromisoformat(timestamp))
        self.assertFalse(self.fixture.calls)
        self.assertEqual(self.repo.journal()["scheduled_targets"]["123"]["resolved_at"],
                         "2026-10-05T00:01:00+09:00")

    def test_naive_execution_time_is_rejected_before_binding_write(self):
        self.context["event"] = "schedule"
        with self.assertRaisesRegex(ingest.IngestionError, "aware timestamp required"):
            n.execute(self.root, self.context, self.repo,
                      at=datetime(2026, 10, 2, 21), transport=self.fixture.transport)
        self.assertFalse(self.repo.calls)
        self.assertNotIn(n.CHECKPOINT, self.repo.refs)
        self.assertFalse(self.fixture.calls)
        self.assertEqual(self.run_preview()["status"], "PREVIEW_COMPLETE")
        binding = self.repo.journal()["scheduled_targets"]["123"]
        self.assertIsNotNone(datetime.fromisoformat(binding["resolved_at"]).tzinfo)

    def test_naive_binding_time_cannot_poison_existing_checkpoint(self):
        checkpoint = self.checkpoint()
        checkpoint.bind_schedule(self.fixture.at)
        saved = copy.deepcopy(self.repo.journal())
        self.context["run_id"] = 124
        checkpoint = self.checkpoint()
        with self.assertRaisesRegex(n.RemoteStop, "aware timestamp required"):
            checkpoint.bind_schedule(datetime(2026, 10, 3, 21))
        self.assertEqual(self.repo.journal(), saved)
        self.checkpoint()  # The durable schema remains readable.

    def test_new_schedule_run_resolves_its_own_date(self):
        checkpoint = self.checkpoint()
        checkpoint.bind_schedule(datetime.fromisoformat("2026-10-05T00:01:00+09:00"))
        self.context["run_id"] = 124
        checkpoint = self.checkpoint()
        checkpoint.bind_schedule(datetime.fromisoformat("2026-10-05T21:00:00+09:00"))
        self.assertEqual(checkpoint.target_day, date(2026, 10, 5))
        self.assertEqual(self.repo.journal()["scheduled_targets"]["123"]["date"], "2026-10-04")

    def test_inconsistent_schedule_binding_is_rejected(self):
        checkpoint = self.checkpoint()
        checkpoint.bind_schedule(datetime.fromisoformat("2026-10-05T00:01:00+09:00"))
        document = self.repo.journal()
        document["scheduled_targets"]["123"]["date"] = "2026-10-05"
        files = self.repo.tree(self.repo.refs[n.CHECKPOINT])
        files[n.JOURNAL] = self.repo.blob(ingest.encoded(document))
        self.repo.refs[n.CHECKPOINT] = self.repo.commit(files)
        with self.assertRaisesRegex(n.RemoteStop, "invalid scheduled target date"):
            self.checkpoint()
        self.assertFalse(self.fixture.calls)

    def test_manual_explicit_target_is_unchanged(self):
        def stopped(root, *, remote, **kwargs):
            self.assertEqual(remote.target_day, date(2026, 10, 2))
            self.assertNotIn("scheduled_targets", remote.document)
            raise n.RemoteStop("synthetic stop")
        with patch.object(daily, "run", side_effect=stopped):
            with self.assertRaisesRegex(n.RemoteStop, "synthetic stop"):
                n.execute(self.root, self.context, self.repo, target=date(2026, 10, 2),
                          at=datetime.fromisoformat("2026-10-05T00:01:00+09:00"))

    def test_schedule_binding_ack_failure_stops_before_collector(self):
        self.context["event"] = "schedule"
        original = self.repo.__call__
        def request(method, path, body=None):
            if method == "POST" and path.endswith("/git/refs"):
                raise n.http.Rejected(503)
            return original(method, path, body)
        with patch.object(daily, "run") as collector:
            with self.assertRaises(n.http.Rejected):
                n.execute(self.root, self.context, request, at=self.fixture.at)
            collector.assert_not_called()
        self.assertFalse(self.fixture.calls)

    def future_wait(self):
        self.fixture.at = datetime.fromisoformat("2026-10-05T01:07:00+09:00")
        with self.assertRaises(n.Incomplete):
            self.run_preview()
        return copy.deepcopy(self.repo.journal()["runs"]["2026-10-05"])

    def test_delayed_schedule_defers_only_verified_empty_future_wait(self):
        future = self.future_wait()
        self.context.update(event="schedule", run_id=124)
        self.fixture.at = datetime.fromisoformat("2026-10-05T01:08:00+09:00")
        result = self.run_preview()
        self.assertEqual((result["run_date"], result["status"], result["requests_used"]),
                         ("2026-10-04", "PREVIEW_COMPLETE", 1))
        self.assertEqual(self.repo.journal()["runs"]["2026-10-05"], future)
        self.assertEqual(self.fixture.calls, [("/markets/calendar", {"from": "2026-10-04", "to": "2026-10-04"})])
        # New workspace plus next-day retry retains date and request receipts.
        shutil.rmtree(self.fixture.archive.parent)
        self.fixture.at = datetime.fromisoformat("2026-10-06T23:59:00+09:00")
        again = self.run_preview()
        self.assertEqual((again["run_date"], again["requests_used"]), ("2026-10-04", 1))
        self.assertEqual(len(self.fixture.calls), 1)

    def test_future_wait_with_different_config_can_defer_but_cannot_resume(self):
        self.fixture.config["jquants"]["ingestion"]["download_hosts"] = ["example.invalid"]
        self.fixture.save_config()
        future = self.future_wait()
        self.fixture.config["jquants"]["ingestion"]["download_hosts"] = []
        self.fixture.save_config()
        self.context.update(event="schedule", run_id=124)
        result = self.run_preview()
        self.assertEqual((result["run_date"], result["requests_used"]), ("2026-10-04", 1))
        saved = self.repo.journal()
        self.assertNotEqual(saved["runs"]["2026-10-04"]["config_sha256"], future["config_sha256"])
        self.assertEqual(saved["runs"]["2026-10-05"], future)
        calls = len(self.fixture.calls)
        self.fixture.at = datetime.fromisoformat("2026-10-05T21:00:00+09:00")
        self.context["run_id"] = 125
        with self.assertRaisesRegex(ingest.IngestionError, "run configuration changed"):
            self.run_preview()
        self.assertEqual(len(self.fixture.calls), calls)
        self.assertEqual(self.repo.journal()["runs"], saved["runs"])

    def test_future_wait_deferral_rejects_unverified_or_nonempty_journals(self):
        future = self.future_wait()
        checkpoint = self.checkpoint()
        checkpoint.target_day = date(2026, 10, 4)
        self.assertTrue(checkpoint.can_defer(future))
        changes = [
            {"status": "BLOCKED"}, {"status": "PAUSED"}, {"status": "BUDGET_EXHAUSTED"},
            {"requests": [{"endpoint": "/markets/calendar", "query_sha256": "a" * 64,
                           "reserved_at": future["started_at"]}]},
            {"tasks": {"record": {}}}, {"data_date": "2026-10-05"},
            {"error": "publication_pending"}, {"http_status": 0},
            {"retry_not_before": future["started_at"]},
            {"started_at": "2026-10-05T21:00:00+09:00"},
        ]
        path = daily.run_path(self.fixture.archive, date(2026, 10, 5))
        name = path.relative_to(checkpoint.source).as_posix()
        for change in changes:
            with self.subTest(change=change):
                state = {**future, **change}
                checkpoint.document["runs"]["2026-10-05"] = state
                ingest.atomic_json(path, state)
                raw = path.read_bytes()
                checkpoint.document["files"][name] = {"sha256": n.storage.digest(raw), "blob": n.git_blob(raw)}
                self.assertFalse(checkpoint.can_defer(state))
        checkpoint.document["runs"]["2026-10-05"] = future
        ingest.atomic_json(path, future)
        self.assertFalse(checkpoint.can_defer(future))  # checksum differs
        checkpoint.document["files"].pop(name)
        self.assertFalse(checkpoint.can_defer(future))
        checkpoint.target_day = date(2026, 10, 5)
        self.assertFalse(checkpoint.can_defer(future))

    def test_schedule_does_not_bypass_existing_configuration_hash(self):
        self.fixture.at = datetime.fromisoformat("2026-10-04T20:59:00+09:00")
        with self.assertRaises(n.Incomplete):
            self.run_preview()
        old = self.repo.journal()["runs"]["2026-10-04"]
        self.fixture.config["extra"] = "changed"
        self.fixture.save_config()
        self.context.update(event="schedule", run_id=124)
        self.fixture.at = datetime.fromisoformat("2026-10-05T01:00:00+09:00")
        with self.assertRaisesRegex(ingest.IngestionError, "run configuration changed"):
            self.run_preview()
        self.assertEqual(self.repo.journal()["runs"]["2026-10-04"], old)
        self.assertFalse(self.fixture.calls)

    def test_owner_retry_can_rebind_verified_unused_publication_placeholder(self):
        self.fixture.at = datetime.fromisoformat("2026-10-02T20:59:00+09:00")
        with self.assertRaises(n.Incomplete):
            self.run_preview()
        old = copy.deepcopy(self.repo.journal()["runs"]["2026-10-02"])
        self.fixture.config["extra"] = "changed before any request"
        self.fixture.save_config()
        self.fixture.at = datetime.fromisoformat("2026-10-02T22:00:00+09:00")
        result = self.run_preview()
        self.assertEqual(result["status"], "PREVIEW_COMPLETE")
        state = self.repo.journal()["runs"]["2026-10-02"]
        self.assertEqual(state["budget"], old["budget"])
        self.assertEqual(len(state["requests"]), 9)
        audits = list((self.fixture.archive / "rebindings").glob("*.json"))
        self.assertEqual(len(audits), 1)
        self.assertEqual(json.loads(audits[0].read_text())["previous_state"], old)

    def test_owner_retry_cannot_rebind_a_placeholder_with_prior_charges(self):
        self.fixture.at = datetime.fromisoformat("2026-10-02T20:59:00+09:00")
        with self.assertRaises(n.Incomplete):
            self.run_preview()
        old = copy.deepcopy(self.repo.journal()["runs"]["2026-10-02"])
        ingest.atomic_json(self.root / "operations/acquisition/date-grants.json", {"prior_charges": {"2026-10-02": 1}})
        self.fixture.config["extra"] = "changed"
        self.fixture.save_config()
        self.fixture.at = datetime.fromisoformat("2026-10-02T22:00:00+09:00")
        with self.assertRaisesRegex(ingest.IngestionError, "run configuration changed"):
            self.run_preview()
        self.assertEqual(self.repo.journal()["runs"]["2026-10-02"], old)
        self.assertFalse(self.fixture.calls)

    def test_collect_verify_duplicate_without_more_provider_requests(self):
        result = self.run_preview()
        self.assertEqual(result["status"], "PREVIEW_COMPLETE")
        self.assertEqual(result["requests_used"], 9)
        self.assertEqual(self.repo.journal()["runs"]["2026-10-02"]["status"], "COLLECTED")
        self.assertEqual(self.run_preview()["generation"], result["generation"])
        self.assertEqual(len(self.fixture.calls), 9)
        self.assertEqual(len(self.repo.pulls), 1)
        self.assertTrue(self.repo.pulls[0]["draft"])
        self.assertEqual(self.repo.refs["main"], self.context["sha"])
        run = self.repo.ci_runs[result["ci_run_id"]]
        self.assertEqual(run["head_branch"], "daily/market/2026-10-02-preview")
        self.assertEqual(run["head_sha"], self.repo.pulls[0]["head"]["sha"])

    def test_crash_reservation_is_durable_and_resume_retains_cost(self):
        self.context["event"] = "schedule"
        def crash(*_):
            self.assertEqual(len(self.repo.journal()["runs"]["2026-10-02"]["requests"]), 1)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_preview(crash)
        shutil.rmtree(self.fixture.archive.parent)
        self.fixture.at = datetime.fromisoformat("2026-10-03T21:00:00+09:00")
        result = self.run_preview()
        self.assertEqual(result["run_date"], "2026-10-02")
        self.assertEqual(result["requests_used"], 10)
        self.assertEqual(len(self.fixture.calls), 9)

    def test_failed_checkpoint_never_calls_provider(self):
        self.repo.fail_patch = True
        with self.assertRaises(n.http.Rejected):
            self.run_preview()
        self.assertFalse(self.fixture.calls)
        self.assertEqual(len(self.repo.journal()["runs"]["2026-10-02"]["requests"]), 0)

    def test_existing_date_charges_cannot_be_refunded(self):
        ingest.atomic_json(self.root / "operations/acquisition/date-grants.json",
                           {"prior_charges": {"2026-10-02": 12}})
        with self.assertRaises(n.Incomplete) as caught:
            self.run_preview()
        self.assertEqual(caught.exception.result["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(caught.exception.result["requests_for_date"], 12)
        self.assertFalse(self.fixture.calls)

    def test_authentication_retry_keeps_failed_request(self):
        def denied(*_):
            raise ingest.ApiError(401)
        with self.assertRaises(n.Incomplete) as caught:
            self.run_preview(denied)
        self.assertEqual(caught.exception.result["http_status"], 401)
        shutil.rmtree(self.fixture.archive.parent)
        self.assertEqual(self.run_preview()["requests_used"], 10)

    def test_weekend_has_no_price_day(self):
        self.fixture.at = datetime.fromisoformat("2026-10-03T21:00:00+09:00")
        result = self.run_preview()
        self.assertEqual(result["requests_used"], 1)
        self.assertIsNone(result["data_date"])
        self.assertEqual(self.repo.journal()["runs"]["2026-10-03"]["status"], "HOLIDAY")
        self.assertFalse((self.fixture.archive.parent / "daily-prices/latest.json").exists())

    def test_waiting_for_publication_makes_no_request(self):
        self.fixture.at = datetime.fromisoformat("2026-10-02T20:59:00+09:00")
        with self.assertRaises(n.Incomplete) as caught:
            self.run_preview()
        self.assertEqual(caught.exception.result["status"], "WAIT_PUBLICATION")
        self.assertFalse(self.fixture.calls)

    def test_historical_target_cannot_create_a_new_campaign_budget(self):
        self.run_preview()
        count = len(self.fixture.calls)
        with self.assertRaisesRegex(n.RemoteStop, "existing campaign"):
            n.execute(self.root, self.context, self.repo, target=date(2026, 10, 1),
                      preview=True, at=self.fixture.at, transport=self.fixture.transport,
                      sleep=lambda _: None)
        self.assertEqual(len(self.fixture.calls), count)

    def test_main_drift_stops_before_provider(self):
        original = self.repo.__call__
        def request(method, path, body=None):
            response = original(method, path, body)
            if method == "POST" and path.endswith("/git/refs"):
                self.repo.refs["main"] = "f" * 40
            return response
        with self.assertRaisesRegex(n.RemoteStop, "main moved"):
            n.execute(self.root, self.context, request, preview=True, at=self.fixture.at,
                      transport=self.fixture.transport, sleep=lambda _: None)
        self.assertFalse(self.fixture.calls)

    def test_run_and_owner_context_are_required(self):
        for change in ({"actor": "other"}, {"repository": "other/repo"}, {"run_id": 0},
                       {"event": "pull_request"}, {"ref": "refs/heads/other"}):
            with self.assertRaises(n.RemoteStop):
                n.trusted_context(self.root, {**self.context, **change}, False)

    def test_budget_reset_is_rejected(self):
        self.run_preview()
        checkpoint = self.checkpoint()
        checkpoint.prepare(self.fixture.archive.parent)
        state = copy.deepcopy(checkpoint.document["runs"]["2026-10-02"])
        state["requests"] = []
        with self.assertRaisesRegex(n.RemoteStop, "durable requests"):
            checkpoint.sync(state)

    def test_data_only_candidate_is_adopted_after_all_ci(self):
        result = self.run_preview()
        old = self.repo.refs["main"]
        self.repo.calls.clear()
        report = n.publish(self.root, self.checkpoint(), result)
        self.assertEqual(report["status"], "ADOPTED")
        before, after = self.repo.tree(old), self.repo.tree(self.repo.refs["main"])
        changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
        self.assertTrue(all(p.startswith(("data/market/jquants/", n.RECORDS)) or n.observations.publication_path(p)
                            for p in changed))
        operations = [c[0] for c in self.repo.calls if "/check-runs" in c[1]
                      or c[1].endswith(("/dispatches", "/merge"))]
        self.assertEqual(operations, ["POST", "GET", "GET", "PUT"])

    def test_installation_token_proves_write_permission_once_before_collection(self):
        def request(method, path, body=None):
            result = self.repo(method, path, body)
            if method == 'GET' and path == n.PREFIX:
                result.pop('permissions')
            return result
        git = n.Git(request)
        self.assertTrue(git.private_metadata()['permissions']['push'])
        self.assertTrue(git.private_metadata()['permissions']['push'])
        probes = [call for call in self.repo.calls if call[0] == 'POST']
        self.assertEqual(len(probes), 1)
        self.assertTrue(probes[0][1].endswith('/git/blobs'))
        self.assertFalse(self.fixture.calls)

    def test_only_exact_bot_data_ci_is_approved_and_checked(self):
        head, branch = self.repo.refs["main"], "daily/market/2026-10-02"
        run = {"id": 900, "path": n.CI_WORKFLOW, "event": "pull_request", "head_sha": head,
               "head_branch": branch, "status": "completed", "conclusion": "action_required",
               "actor": {"login": "github-actions[bot]"}, "check_suite_id": 1900,
               "pull_requests": [{"number": 7, "head": {"sha": head}}]}
        self.repo.pull_ci_runs = [run]
        self.repo.ci_runs[900] = copy.deepcopy(run)
        result = n.approve_data_ci(n.Git(self.repo), {"number": 7}, branch, head,
                                   timeout=60, sleep=lambda _: None, monotonic=lambda: 0)
        self.assertEqual(result, 900)
        self.assertEqual(len([c for c in self.repo.calls if c[1].endswith("/approve")]), 1)
        self.assertTrue(any("/check-suites/1900/check-runs" in c[1] for c in self.repo.calls))
        self.repo.calls.clear()
        ticks = iter([0, 0, 90])
        with self.assertRaisesRegex(n.RemoteStop, "CI missing or unmatched"):
            n.approve_data_ci(n.Git(self.repo), {"number": 8}, branch, head,
                             timeout=60, sleep=lambda _: None, monotonic=lambda: next(ticks))
        self.assertFalse(any(c[0] != "GET" for c in self.repo.calls))

    def test_failed_pull_request_ci_cannot_be_replaced_by_dispatched_success(self):
        head, branch = self.repo.refs["main"], "daily/market/2026-10-02"
        run = {"id": 900, "path": n.CI_WORKFLOW, "event": "pull_request", "head_sha": head,
               "head_branch": branch, "status": "completed", "conclusion": "failure",
               "pull_requests": [{"number": 7, "head": {"sha": head}}]}
        self.repo.pull_ci_runs = [run]
        self.repo.ci_runs[900] = copy.deepcopy(run)
        with self.assertRaisesRegex(n.RemoteStop, "CI failed"):
            n.approve_data_ci(n.Git(self.repo), {"number": 7}, branch, head,
                              timeout=60, sleep=lambda _: None, monotonic=lambda: 0)
        self.assertFalse(any(c[0] != "GET" for c in self.repo.calls))

    def test_delayed_exact_pull_ci_waits_for_association(self):
        head, branch = self.repo.refs['main'], 'daily/market/2026-10-02'
        run = {'id':900,'path':n.CI_WORKFLOW,'event':'pull_request','head_sha':head,
               'head_branch':branch,'status':'completed','conclusion':'success','check_suite_id':1900,
               'pull_requests':[{'number':7,'head':{'sha':head}}]}
        self.repo.ci_runs[900]=copy.deepcopy(run)
        tick=[0]; sleeps=[]
        def sleep(seconds):
            tick[0]+=seconds; sleeps.append(seconds)
            self.repo.pull_ci_runs=[copy.deepcopy(run)]
        result=n.approve_data_ci(n.Git(self.repo),{'number':7},branch,head,
                                 timeout=60,sleep=sleep,monotonic=lambda:tick[0])
        self.assertEqual(result,900);self.assertEqual(sleeps,[30])

    def test_unmatched_pull_ci_is_not_approved(self):
        head, branch = self.repo.refs['main'], 'daily/market/2026-10-02'
        good={'id':900,'path':n.CI_WORKFLOW,'event':'pull_request','head_sha':head,
              'head_branch':branch,'status':'completed','conclusion':'action_required',
              'actor':{'login':'github-actions[bot]'},
              'pull_requests':[{'number':7,'head':{'sha':head}}]}
        for change in ({'head_sha':'f'*40},{'head_branch':'other'}, {'pull_requests':[]},
                       {'pull_requests':[{'number':7,'head':{'sha':'f'*40}}]}):
            with self.subTest(change=change):
                self.repo.pull_ci_runs=[{**good,**change}];self.repo.calls.clear()
                tick=[0]
                def sleep(seconds):tick[0]+=seconds
                with self.assertRaisesRegex(n.RemoteStop,'CI missing or unmatched'):
                    n.approve_data_ci(n.Git(self.repo),{'number':7},branch,head,
                                     timeout=60,sleep=sleep,monotonic=lambda:tick[0])
                self.assertFalse(any(c[0]!='GET' for c in self.repo.calls))

    def test_missing_pull_ci_blocks_publish_after_successful_dispatch(self):
        result=self.run_preview();old=self.repo.refs['main'];tick=[0]
        def request(method,path,body=None):
            value=self.repo(method,path,body)
            if '/actions/workflows/ci.yml/runs?' in path:return {'workflow_runs':[]}
            return value
        checkpoint=self.checkpoint();checkpoint.git=n.Git(request)
        def sleep(seconds):tick[0]+=seconds
        with self.assertRaisesRegex(n.RemoteStop,'CI missing or unmatched'):
            n.publish(self.root,checkpoint,result,timeout=60,sleep=sleep,monotonic=lambda:tick[0])
        self.assertEqual(self.repo.refs['main'],old)
        self.assertFalse(any(c[0]=='PUT' for c in self.repo.calls))
        self.assertTrue(any(r['event']=='workflow_dispatch' and r['conclusion']=='success'
                            for r in self.repo.ci_runs.values()))

    def test_adopted_checkpoint_bytes_are_not_downloaded_each_night(self):
        result = self.run_preview()
        n.publish(self.root, self.checkpoint(), result)
        shutil.rmtree(self.fixture.archive.parent)
        n.storage.restore(self.root / "data/market/jquants", self.fixture.archive.parent)
        checkpoint = self.checkpoint()
        self.repo.calls.clear()
        checkpoint.prepare(self.fixture.archive.parent)
        self.assertFalse(any("/git/blobs/" in c[1] for c in self.repo.calls))

    def test_failed_ci_preserves_pr_and_retry_reuses_it(self):
        result = self.run_preview()
        old = self.repo.refs["main"]
        self.repo.checks[0]["conclusion"] = "failure"
        with self.assertRaisesRegex(n.RemoteStop, "CI failed"):
            n.publish(self.root, self.checkpoint(), result)
        self.assertEqual(self.repo.refs["main"], old)
        self.assertEqual(len(self.repo.pulls), 2)
        self.repo.checks[0]["conclusion"] = "success"
        self.assertEqual(n.publish(self.root, self.checkpoint(), result)["status"], "ADOPTED")
        self.assertEqual(len(self.repo.pulls), 2)

    def test_missing_ci_never_adopts(self):
        result = self.run_preview()
        self.repo.checks.pop()
        ticks = iter([0, 0, 90])
        with self.assertRaisesRegex(n.RemoteStop, "CI pending"):
            n.publish(self.root, self.checkpoint(), result, timeout=60,
                      monotonic=lambda: next(ticks), sleep=lambda _: None)
        self.assertFalse(any(c[0] == "PUT" for c in self.repo.calls))

    def test_modified_candidate_branch_is_rejected(self):
        result = self.run_preview()
        files = self.repo.tree(self.repo.refs["main"])
        files["src/unexpected.py"] = self.repo.blob(b"unexpected\n")
        self.repo.refs["daily/market/2026-10-02"] = self.repo.commit(files, {"src/unexpected.py": "100755"})
        with self.assertRaisesRegex(n.RemoteStop, "unexpected changes"):
            n.publish(self.root, self.checkpoint(), result)
        self.assertEqual(len(self.repo.pulls), 1)

    def test_cli_collects_saves_and_checks_ci_with_only_actions_token(self):
        execute = n.execute
        env = {"GITHUB_TOKEN": "synthetic-actions-token", "JQUANTS_API_KEY": "synthetic-provider-key",
               "GITHUB_REPOSITORY": n.REPO, "GITHUB_EVENT_NAME": "workflow_dispatch",
               "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": self.context["sha"],
               "GITHUB_ACTOR": "KKyosuke", "GITHUB_TRIGGERING_ACTOR": "KKyosuke", "GITHUB_RUN_ID": "123"}
        output = io.StringIO()
        with patch.dict(n.os.environ, env, clear=True), \
             patch("sys.argv", ["nightly", "--preview", "--report", str(self.root / "report.json")]), \
             patch.object(n, "project_root", return_value=self.root), \
             patch.object(n.http, "GitHubTransport", return_value=self.repo) as github, \
             patch.object(n.rate, "BudgetedTransport", side_effect=lambda request: request), \
             patch.object(n, "execute", side_effect=lambda root, context, request, **kwargs:
                          execute(root, context, request, at=self.fixture.at,
                                  transport=self.fixture.transport, sleep=lambda _: None, **kwargs)), \
             patch.object(ingest, "utc_now", return_value=self.fixture.at.isoformat()), \
             contextlib.redirect_stdout(output):
            self.assertEqual(n.main(), 0)
        github.assert_called_once_with("synthetic-actions-token")
        self.assertEqual(json.loads(output.getvalue())["status"], "PREVIEW_COMPLETE")
        self.assertEqual(len(self.fixture.calls), 9)
        self.assertTrue(self.repo.ci_runs)
        self.assertNotIn("synthetic-actions-token", (self.root / "report.json").read_text())

    def test_dispatch_failure_keeps_collected_data_and_pr_without_merge(self):
        result = self.run_preview()
        old, count = self.repo.refs["main"], len(self.fixture.calls)
        self.repo.fail_dispatch = True
        with self.assertRaises(n.http.Rejected):
            n.publish(self.root, self.checkpoint(), result)
        self.assertEqual(self.repo.refs["main"], old)
        self.assertEqual(len(self.fixture.calls), count)
        self.assertEqual(self.repo.journal()["runs"]["2026-10-02"]["status"], "COLLECTED")
        self.assertEqual(len(self.repo.pulls), 2)
        self.assertFalse(any(c[0] == "PUT" for c in self.repo.calls))
        self.repo.fail_dispatch = False
        self.assertEqual(n.publish(self.root, self.checkpoint(), result)["status"], "ADOPTED")
        self.assertEqual(len(self.repo.pulls), 2)

    def test_candidate_drift_stops_before_dispatch(self):
        result = self.run_preview()
        checkpoint = self.checkpoint()
        original = self.repo.__call__
        def request(method, path, body=None):
            response = original(method, path, body)
            if method == "POST" and path.endswith("/pulls"):
                self.repo.refs[body["head"]] = self.repo.refs["main"]
            return response
        checkpoint.git.request = request
        self.repo.calls.clear()
        with self.assertRaisesRegex(n.RemoteStop, "branch moved before CI dispatch"):
            n.publish(self.root, checkpoint, result)
        self.assertFalse(any(c[0] == "PUT" or c[1].endswith("/dispatches") for c in self.repo.calls))

    def test_missing_dispatch_run_identity_never_merges(self):
        result = self.run_preview()
        for response in (None, {}, {"workflow_run_id": True}, {"workflow_run_id": 0}):
            with self.subTest(response=response):
                self.repo.dispatch_response = response
                with self.assertRaisesRegex(n.RemoteStop, "run identity unavailable"):
                    n.publish(self.root, self.checkpoint(), result)
        self.assertFalse(any(c[0] == "PUT" for c in self.repo.calls))

    def test_ci_run_must_match_dispatched_workflow_branch_and_head(self):
        result = self.run_preview()
        for change in ({"id": 1}, {"path": ".github/workflows/other.yml"}, {"event": "pull_request"},
                       {"head_sha": "f" * 40}, {"head_branch": "main"}):
            with self.subTest(change=change):
                self.repo.run_change = change
                with self.assertRaisesRegex(n.RemoteStop, "CI run identity or head changed"):
                    n.publish(self.root, self.checkpoint(), result)
        self.assertFalse(any(c[0] == "PUT" for c in self.repo.calls))

    def test_previous_success_cannot_replace_pending_dispatched_ci(self):
        result = self.run_preview()
        self.repo.run_change = {"status": "in_progress", "conclusion": None}
        ticks = iter([0, 0, 90])
        with self.assertRaisesRegex(n.RemoteStop, "CI pending"):
            n.publish(self.root, self.checkpoint(), result, timeout=60,
                      monotonic=lambda: next(ticks), sleep=lambda _: None)
        self.assertFalse(any(c[0] == "PUT" for c in self.repo.calls))

    def test_ci_checks_must_match_dispatched_suite_and_head(self):
        result = self.run_preview()
        for change in ({"head_sha": "f" * 40}, {"check_suite": {"id": 1}}):
            with self.subTest(change=change):
                self.repo.checks[0].update(change)
                ticks = iter([0, 0, 90])
                with self.assertRaisesRegex(n.RemoteStop, "CI pending"):
                    n.publish(self.root, self.checkpoint(), result, timeout=60,
                              monotonic=lambda: next(ticks), sleep=lambda _: None)
                self.repo.checks[0].pop(next(iter(change)))
        self.assertFalse(any(c[0] == "PUT" for c in self.repo.calls))

    def test_failed_dispatched_run_cannot_use_previous_success(self):
        result = self.run_preview()
        self.repo.run_change = {"conclusion": "failure"}
        with self.assertRaisesRegex(n.RemoteStop, "CI failed"):
            n.publish(self.root, self.checkpoint(), result)
        self.assertFalse(any(c[0] == "PUT" for c in self.repo.calls))

    def test_deleted_checkpoint_after_adoption_cannot_reset_budget(self):
        result = self.run_preview()
        n.publish(self.root, self.checkpoint(), result)
        del self.repo.refs[n.CHECKPOINT]
        with self.assertRaisesRegex(n.RemoteStop, "checkpoint was deleted"):
            self.checkpoint()


if __name__ == "__main__":
    unittest.main()
