import json
import shutil
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stock_jp.automation import nightly as n
from stock_jp.automation.paper_checkpoint import run
from stock_jp.market_data.primary_sources import JST
from stock_jp.market_data.jquants import ingest
from stock_jp.market_data.jquants.cache import NightlyClient
from tests.test_nightly_market_data import Repository


class PaperCheckpointTest(unittest.TestCase):
    def root_and_context(self, root):
        (root / "config").mkdir()
        ingest.atomic_json(root / "config/policy.json", {"operation_mode": "PAPER", "broker_submission": "HUMAN_ONLY"})
        ingest.atomic_json(root / "config/sources.json", {"market_data_git": {"automatic_upload": True}})
        (root / "operations").mkdir()
        ingest.atomic_json(root / "operations/state.json", {"last_successful_run_at_jst": "unchanged"})
        (root / "operations/run-history.csv").write_text("status\nfailed\n")
        repo = Repository(root)
        context = {"repository": n.REPO, "event": "workflow_dispatch", "ref": "refs/heads/main", "sha": repo.refs["main"],
                   "actor": "KKyosuke", "triggering_actor": "KKyosuke", "run_id": 123}
        return repo, context

    def test_raw_inbox_is_durable_and_never_replaces_main_or_canonical_state(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repo, context = self.root_and_context(root)
            original = (root / "operations/state.json").read_bytes()
            main = repo.refs["main"]
            result = run(root, context, n.Git(repo), save=True)
            self.assertEqual(result["status"], "UNREVIEWED_INBOX_SAVED")
            self.assertFalse(result["review_completed"])
            self.assertEqual(repo.refs["main"], main)
            self.assertEqual((root / "operations/state.json").read_bytes(), original)
            (root / "operations/state.json").write_text('{"new_canonical_state":true}\n')
            restored = run(root, context, n.Git(repo), save=False)
            self.assertFalse(restored["canonical_state_restored"])
            self.assertEqual(json.loads((root / "operations/state.json").read_text()), {"new_canonical_state": True})

    def test_a_stopped_actions_run_cannot_write_the_inbox(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repo, context = self.root_and_context(root)
            def stopped(method, path, body=None):
                if "/actions/runs/" in path:
                    return {"status": "completed", "head_sha": context["sha"], "path": n.WORKFLOW}
                return repo(method, path, body)
            with self.assertRaisesRegex(n.RemoteStop, "active owner"):
                run(root, context, n.Git(stopped), save=True)
            self.assertNotIn(n.PAPER_CHECKPOINT, repo.refs)

    def test_query_pages_restore_with_original_cutoff_and_no_provider_call(self):
        with TemporaryDirectory() as temporary, patch("stock_jp.automation.paper_checkpoint.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 6, 21, 20, tzinfo=JST)
            root = Path(temporary).resolve()
            repo, context = self.root_and_context(root)
            cutoff = "2026-10-06T21:00:00+09:00"
            cutoff_path = root / "operations/runs/2026-10-06/source-scan-cutoff.json"
            ingest.atomic_json(cutoff_path, {"cutoff_at_jst": cutoff})
            client = NightlyClient(private=root / "operations", cutoff=cutoff, retrieved=cutoff,
                                   plan="light", transport=lambda *_: {"data": [{"synthetic": True}]},
                                   sleep=lambda _: None)
            expected = client.get("/equities/master", {"date": "2026-10-06"})
            original = {p.relative_to(root).as_posix(): p.read_bytes() for p in client.root.rglob("*.json")}
            run(root, context, n.Git(repo), save=True)
            shutil.rmtree(root / ".work/market-data/nightly-source-cache")
            cutoff_path.unlink()
            restored = run(root, context, n.Git(repo), save=False)
            self.assertEqual(restored["file_count"], len(original))
            for name, raw in original.items():
                self.assertEqual((root / name).read_bytes(), raw)
            replay = NightlyClient(private=root / "operations", cutoff=cutoff, retrieved=cutoff,
                                   plan="light", transport=lambda *_: self.fail("unexpected provider call"),
                                   sleep=lambda _: None)
            self.assertEqual(replay.get("/equities/master", {"date": "2026-10-06"}), expected)
            self.assertEqual((replay.requests, replay.cache_hits), (0, 1))
            self.assertFalse(cutoff_path.exists())

    def test_writer_rejects_a_path_that_escapes_the_inbox(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repo, context = self.root_and_context(root)
            for name in (n.PAPER_RAW_PREFIX + "../config/policy.json", n.PAPER_RAW_PREFIX + "a/.git/config"):
                with self.assertRaisesRegex(n.RemoteStop, "unsafe PAPER"):
                    n.Git(repo).write(n.PAPER_CHECKPOINT, repo.refs["main"], {name: b"x"}, exists=False)

    def test_resume_metadata_reuses_only_this_night_and_never_imports_the_run(self):
        for hour, preview, expected in ((21, False, True), (8, False, False), (8, True, True)):
            with self.subTest(hour=hour, preview=preview), TemporaryDirectory() as temporary, \
                    patch("stock_jp.automation.paper_checkpoint.datetime") as clock:
                clock.now.return_value = datetime(2026, 10, 6, 21, 20, tzinfo=JST)
                root = Path(temporary).resolve()
                repo, context = self.root_and_context(root)
                path = root / "operations/runs/2026-10-06/source-scan-cutoff.json"
                cutoff = datetime(2026, 10, 6, hour, tzinfo=JST).isoformat(timespec="seconds")
                ingest.atomic_json(path, {"cutoff_at_jst": cutoff})
                client = NightlyClient(private=root / "operations", cutoff=cutoff, retrieved=cutoff,
                                       plan="light", transport=lambda *_: {"data": []}, sleep=lambda _: None)
                client.get("/equities/master", {"date": "2026-10-06"})
                run(root, context, n.Git(repo), save=True)
                path.unlink()
                shutil.rmtree(root / ".work/market-data/nightly-source-cache")
                restored = run(root, context, n.Git(repo), save=False, preview=preview)
                self.assertEqual(restored["resume_cutoff_at_jst"], cutoff if expected else None)
                self.assertEqual(restored["file_count"], 2 if expected else 0)
                self.assertFalse(path.exists())
                self.assertFalse(restored["canonical_state_restored"])

    def test_past_cache_stays_in_git_without_accumulating_in_the_next_night(self):
        with TemporaryDirectory() as temporary, patch("stock_jp.automation.paper_checkpoint.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 6, 21, 20, tzinfo=JST)
            root = Path(temporary).resolve()
            repo, context = self.root_and_context(root)
            cutoff = "2026-10-06T21:00:00+09:00"
            ingest.atomic_json(root / "operations/runs/2026-10-06/source-scan-cutoff.json", {"cutoff_at_jst": cutoff})
            client = NightlyClient(private=root / "operations", cutoff=cutoff, retrieved=cutoff,
                                   plan="light", transport=lambda *_: {"data": []}, sleep=lambda _: None)
            client.get("/equities/master", {"date": "2026-10-06"})
            saved = run(root, context, n.Git(repo), save=True)
            previous_tree = repo.tree(saved["commit"])
            shutil.rmtree(root / ".work/market-data/nightly-source-cache")
            clock.now.return_value = datetime(2026, 10, 7, 21, 20, tzinfo=JST)
            restored = run(root, context, n.Git(repo), save=False)
            self.assertEqual(restored["file_count"], 0)
            self.assertIsNone(restored["resume_cutoff_at_jst"])
            self.assertFalse((root / ".work/market-data/nightly-source-cache").exists())
            self.assertEqual(repo.tree(repo.refs[n.PAPER_CHECKPOINT]), previous_tree)

    def test_snapshot_names_are_relative_to_the_resolved_workspace(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            root = folder / "workspace"
            root.mkdir()
            repo, context = self.root_and_context(root)
            ingest.atomic_json(root / "data/market/primary-sources/example.json", {"source": "example"})
            alias = folder / "alias"
            alias.symlink_to(root, target_is_directory=True)
            result = run(alias, context, n.Git(repo), save=True)
            self.assertEqual(result["status"], "UNREVIEWED_INBOX_SAVED")
            journal = n.Git(repo).read(repo.tree(repo.refs[n.PAPER_CHECKPOINT])[n.PAPER_JOURNAL])
            self.assertIn("data/market/primary-sources/example.json", json.loads(journal)["files"])
