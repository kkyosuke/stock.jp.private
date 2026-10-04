"""Real collector/codec with synthetic provider data and an in-memory Git API."""
import base64
import copy
from datetime import date, datetime
import hashlib
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
        self.reviews = []
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
                return {"status": "in_progress", "head_sha": self.workflow_sha, "path": n.WORKFLOW}
            if route.endswith("/check-runs"):
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
            if route == "/git/blobs":
                return {"sha": self.blob(base64.b64decode(body["content"]))}
            if route == "/git/trees":
                tree = {e["path"]: e for e in self.objects[body["base_tree"]]["tree"]}
                tree.update({e["path"]: e for e in body["tree"]})
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

    def test_crash_reservation_is_durable_and_resume_retains_cost(self):
        def crash(*_):
            self.assertEqual(len(self.repo.journal()["runs"]["2026-10-02"]["requests"]), 1)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_preview(crash)
        shutil.rmtree(self.fixture.archive.parent)
        result = self.run_preview()
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
        self.assertTrue(all(p.startswith(("data/market/jquants/", n.RECORDS)) for p in changed))
        operations = [c[0] for c in self.repo.calls if "/check-runs" in c[1] or c[1].endswith("/merge")]
        self.assertEqual(operations, ["GET", "PUT"])

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

    def test_checks_use_actions_token_and_writes_use_pat(self):
        calls = []
        def writer(method, path, body=None):
            calls.append(("PAT", method))
        def checks(method, path, body=None):
            calls.append(("ACTIONS", method))
        request = n.GitHubRequests(writer, checks)
        request("GET", n.PREFIX + "/commits/" + self.context["sha"] + "/check-runs")
        request("GET", n.PREFIX + "/pulls")
        request("POST", n.PREFIX + "/pulls", {})
        self.assertEqual(calls, [("ACTIONS", "GET"), ("PAT", "GET"), ("PAT", "POST")])

    def test_deleted_checkpoint_after_adoption_cannot_reset_budget(self):
        result = self.run_preview()
        n.publish(self.root, self.checkpoint(), result)
        del self.repo.refs[n.CHECKPOINT]
        with self.assertRaisesRegex(n.RemoteStop, "checkpoint was deleted"):
            self.checkpoint()


if __name__ == "__main__":
    unittest.main()
