import importlib.util
import fcntl
import hashlib
import os
import shutil
import subprocess
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("operation_health", ROOT / "scripts/operation_health.py")
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class OperationHealthTest(unittest.TestCase):
    def prepare_root(self, root):
        (root / "stock.jp/scripts").mkdir(parents=True)
        private = root / "operations/private"
        archive = private / "market-data/daily-prices"
        archive.mkdir(parents=True)
        (archive / "latest.json").write_text('{"latest_trading_date":"2026-09-11"}')
        (private / "source-config.json").write_text(json.dumps({
            "price_source": {"manifest_path": "latest.json"},
        }))
        (root / "stock.jp/operations").mkdir()
        (root / "stock.jp/operations/private").symlink_to(private)
        # Storage boundary cases are tested in the public helper's own suite.
        # This fixture also exercises the health script's checkout-local import.
        (root / "stock.jp/scripts/market_data_storage.py").write_text(
            'def resolve_archive_root(root):\n'
            '    p=(root/"operations/private/market-data/daily-prices").resolve()\n'
            '    if not p.is_dir():\n'
            '        raise ValueError("private market-data archive is missing; no public fallback")\n'
            '    return p\n'
        )
        return private, archive

    def test_static_validation_does_not_read_or_require_market_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, archive = self.prepare_root(root)
            (root / "stock.jp/scripts/operation_state.py").write_text("print('{\"valid\": true}')\n")
            with patch.object(health, "ROOT", root), patch.object(health, "configured_archive", side_effect=AssertionError("raw data accessed")):
                self.assertEqual(health.command_json("operation_state.py", "2026-10-01T21:00:00+09:00", "validate", include_market_data=False), {"valid": True})
                with self.assertRaisesRegex(ValueError, "only for static"):
                    health.command_json("operation_bootstrap.py", "2026-10-01T21:00:00+09:00", include_market_data=False)

    def test_retired_workflow_history_is_not_required(self):
        def api(path):
            self.assertNotIn("actions/workflows", path)
            return {"sha": "same"}
        with patch.object(health, "github", side_effect=api), patch.object(health.subprocess, "check_output", return_value="same\n"):
            self.assertEqual(health.remote_alerts("owner/private", health.parse_time("2026-09-11T21:00:00+09:00")), [])

    def test_pointer_drift_requires_review(self):
        def api(path):
            return {"sha": "public", "commit": {"committer": {"date": "2026-09-01T00:00:00Z"}}} if "commits/main" in path else {"sha": "private"}
        with patch.object(health, "github", side_effect=api), patch.object(health.subprocess, "check_output", return_value="same\n"):
            self.assertTrue(health.remote_alerts("owner/private", health.parse_time("2026-09-11T21:00:00+09:00")))

    def test_successful_data_sync_does_not_hide_missed_research(self):
        report = health.local_report(
            {"last_run_id": "old", "data_gaps": [{"status": "OPEN"}]},
            {"status": "MISSED", "alerts": ["nightly run overdue"]},
            {"paper_go": True, "broker_submission": "HUMAN_ONLY", "status": "READY_WITH_WARNINGS"},
        )
        self.assertIn("nightly: MISSED", report["alerts"])
        self.assertEqual(report["open_data_gap_count"], 1)

    def test_invalid_readiness_and_submission_mode_fail_closed(self):
        report = health.local_report({}, {"status": "OK"}, {"paper_go": False, "blockers": ["stale prices"]})
        self.assertIn("stale prices", report["alerts"])
        self.assertIn("broker submission boundary must be HUMAN_ONLY", report["alerts"])

    def test_validation_isolated_and_migrations_rejected(self):
        for script in ("operation_bootstrap.py", "operation_watchdog.py", "operation_state.py"):
            with self.subTest(script=script), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                private, archive = self.prepare_root(root)
                (private / "state.json").write_text('{"original":true}')
                (root / "stock.jp/scripts" / script).write_text(
                    'from pathlib import Path\n'
                    'p=Path(__file__).resolve().parents[1]/"operations/private/state.json"\n'
                    'p.write_text("{}")\nprint("{}")\n'
                )
                with patch.object(health, "ROOT", root), self.assertRaisesRegex(RuntimeError, "requires migration"):
                    health.command_json(script, "2026-09-11T21:00:00+09:00", "validate" if script == "operation_state.py" else "check")
                self.assertEqual((private / "state.json").read_text(), '{"original":true}')

    def test_readonly_validator_can_return_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare_root(root)
            (root / "stock.jp/scripts/check.py").write_text('print(\'{"paper_go":true}\')')
            with patch.object(health, "ROOT", root):
                self.assertEqual(health.command_json("check.py", "2026-09-11T21:00:00+09:00"), {"paper_go": True})

    def test_only_configured_private_archive_is_copied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, archive = self.prepare_root(root)
            (archive / "synthetic.csv").write_text("code,close\nSYNTHETIC,100\n")
            (private / "market-data/unrelated").mkdir()
            (private / "market-data/unrelated/sentinel").write_text("do not copy")
            (private / "market-data-staging").mkdir()
            (private / "market-data-staging/sentinel").write_text("do not copy")
            (private / "historical-replay/raw").mkdir(parents=True)
            (private / "historical-replay/raw/sentinel").write_text("do not copy")
            (root / "stock.jp/data/daily-prices").mkdir(parents=True)
            (root / "stock.jp/data/daily-prices/sentinel").write_text("do not copy")
            (root / "stock.jp/scripts/check.py").write_text(
                'from pathlib import Path\n'
                'root=Path(__file__).resolve().parents[1]\n'
                'archive=root/"operations/private/market-data/daily-prices"\n'
                'assert (archive/"synthetic.csv").is_file()\n'
                'assert not archive.is_symlink()\n'
                'assert not (root/"operations/private/market-data/unrelated").exists()\n'
                'assert not (root/"operations/private/market-data-staging").exists()\n'
                'assert not (root/"operations/private/historical-replay/raw").exists()\n'
                'assert not (root/"data/daily-prices").exists()\n'
                'print(\'{"paper_go":true}\')\n'
            )
            with patch.object(health, "ROOT", root):
                self.assertTrue(health.command_json("check.py", "2026-09-11T21:00:00+09:00")["paper_go"])
            self.assertEqual((archive / "synthetic.csv").read_text(), "code,close\nSYNTHETIC,100\n")

    def test_validator_cannot_modify_canonical_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, archive = self.prepare_root(root)
            before = (archive / "latest.json").read_bytes()
            (root / "stock.jp/scripts/check.py").write_text(
                'from pathlib import Path\n'
                'p=Path(__file__).resolve().parents[1]/"operations/private/market-data/daily-prices/latest.json"\n'
                'p.write_text("{}")\nprint("{}")\n'
            )
            with patch.object(health, "ROOT", root), self.assertRaisesRegex(RuntimeError, "changed isolated market data"):
                health.command_json("check.py", "2026-09-11T21:00:00+09:00")
            self.assertEqual((archive / "latest.json").read_bytes(), before)

    def test_archive_symlinks_are_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, archive = self.prepare_root(root)
            (archive / "unsafe.csv").symlink_to(private / "source-config.json")
            with patch.object(health, "ROOT", root), self.assertRaisesRegex(ValueError, "unsafe entry"):
                health.command_json("check.py", "2026-09-11T21:00:00+09:00")

    def test_missing_private_archive_cannot_use_public_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, archive = self.prepare_root(root)
            (archive / "latest.json").unlink()
            archive.rmdir()
            (private / "state.json").write_text("{}")
            public_archive = root / "stock.jp/data/daily-prices"
            public_archive.mkdir(parents=True)
            (public_archive / "latest.json").write_text('{"latest_trading_date":"2099-01-01"}')
            output = root / "health.json"
            with patch.object(health, "ROOT", root), patch("sys.argv", [
                "operation_health.py", "--offline", "--output", str(output),
            ]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(health.main(), 1)
            report = json.loads(output.read_text())
            self.assertEqual(report["status"], "ATTENTION")
            self.assertIn("no public fallback", report["alerts"][0])
            self.assertNotIn("price_date", report)

    def test_manifest_is_private_and_cannot_escape_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, archive = self.prepare_root(root)
            with patch.object(health, "ROOT", root):
                self.assertEqual(health.private_price_manifest()["latest_trading_date"], "2026-09-11")
                (archive / "latest.json").unlink()
                (archive / "latest.json").symlink_to(private / "source-config.json")
                with self.assertRaisesRegex(ValueError, "escapes"):
                    health.private_price_manifest()

    def test_health_workflow_does_not_mask_failures(self):
        workflow = (ROOT / ".github/workflows/operation-health.yml").read_text()
        self.assertIn("python scripts/operation_health.py --persisted-only --output", workflow)
        self.assertNotIn("--report-only", workflow)

    def test_naive_time_is_rejected(self):
        with self.assertRaises(ValueError):
            health.parse_time("2026-09-11T21:00:00")

    def test_health_report_only_does_not_change_nightly_readiness(self):
        self.assertIn("--report-only", health.main.__code__.co_consts)

    def validator_fixture(self, root, body):
        private, archive = self.prepare_root(root)
        (root / "stock.jp/scripts/check.py").write_text(
            'from pathlib import Path\nimport os\n'
            'p=Path(__file__).resolve().parents[1]/"operations/private/market-data/daily-prices/latest.json"\n'
            + body + '\nprint(\'{"paper_go":true}\')\n')
        return private, archive

    def test_redundant_chmod_reproduces_false_positive_and_real_helper_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            private, archive=self.validator_fixture(root,'p.chmod(0o600)')
            with patch.object(health,'ROOT',root), self.assertRaisesRegex(RuntimeError,'changed isolated market data'):
                health.command_json('check.py','2026-10-03T21:00:00+09:00')
            shutil.copy2(ROOT/'stock.jp/scripts/operation_state.py',root/'stock.jp/scripts/operation_state.py')
            (root/'stock.jp/scripts/check.py').write_text(
                'from operation_state import secure_private_tree\n'
                'secure_private_tree()\nsecure_private_tree()\nprint(\'{"paper_go":true}\')\n')
            for mode in (0o644,0o600):
                with self.subTest(mode=mode):
                    (archive/'latest.json').chmod(mode)
                    before=health.inspect_archive(archive)
                    with patch.object(health,'ROOT',root):
                        self.assertTrue(health.command_json('check.py','2026-10-03T21:00:00+09:00')['paper_go'])
                    self.assertEqual(health.inspect_archive(archive),before)

    def test_strict_baseline_rejects_mode_content_inode_and_symlink_changes(self):
        changes=[
            'p.chmod(0o644)',
            's=p.stat();p.write_bytes(b"X"*s.st_size);os.utime(p,ns=(s.st_atime_ns,s.st_mtime_ns))',
            's=p.stat();q=p.with_name("replacement");q.write_bytes(p.read_bytes());os.utime(q,ns=(s.st_atime_ns,s.st_mtime_ns));q.replace(p)',
            'q=p.with_name("replacement");q.write_bytes(p.read_bytes());p.unlink();p.symlink_to(q)',
        ]
        for change in changes:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);_,archive=self.validator_fixture(root,change);before=health.inspect_archive(archive)
                with patch.object(health,'ROOT',root),self.assertRaisesRegex(RuntimeError,'changed isolated market data'):
                    health.command_json('check.py','2026-10-03T21:00:00+09:00')
                self.assertEqual(health.inspect_archive(archive),before)

    def test_active_writer_blocks_before_validator(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);private,_=self.validator_fixture(root,'pass')
            for relative in ('market-data-staging/daily-runner.lock','market-data/jquants/.ingestion.lock'):
                lock=private/relative;lock.parent.mkdir(parents=True,exist_ok=True)
                with lock.open('a+b') as writer:
                    fcntl.flock(writer,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    with patch.object(health,'ROOT',root),patch.object(health.subprocess,'run') as run:
                        with self.assertRaisesRegex(RuntimeError,'active market-data writer'):
                            health.command_json('check.py','2026-10-03T21:00:00+09:00')
                        run.assert_not_called()

    def test_source_change_during_validator_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);_,archive=self.validator_fixture(root,'pass')
            def mutate(*args,**kwargs):
                p=archive/'latest.json';s=p.stat();p.write_bytes(b'X'*s.st_size);os.utime(p,ns=(s.st_atime_ns,s.st_mtime_ns))
                return subprocess.CompletedProcess(args,0,'{"paper_go":true}','')
            with patch.object(health,'ROOT',root),patch.object(health.subprocess,'run',side_effect=mutate):
                with self.assertRaisesRegex(RuntimeError,'canonical archive changed'):
                    health.command_json('check.py','2026-10-03T21:00:00+09:00')

    def test_hash_and_nofollow_close_leaf_open_race(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);_,archive=self.prepare_root(root)
            path=archive/'latest.json';expected=hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(health.inspect_archive(archive)['latest.json'][1],expected)
            outside=root/'outside';outside.write_text('must not be followed')
            original=health.os.open;attacked=False
            def race(name,flags,*args,**kwargs):
                nonlocal attacked
                if name=='latest.json' and not attacked:
                    attacked=True;path.unlink();path.symlink_to(outside)
                return original(name,flags,*args,**kwargs)
            with patch.object(health.os,'open',side_effect=race),self.assertRaises((OSError,ValueError)):
                health.inspect_archive(archive)
            self.assertTrue(attacked)

    def test_pinned_directory_does_not_follow_root_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);_,archive=self.prepare_root(root)
            outside=root/'outside';outside.mkdir();(outside/'sentinel').write_text('must not copy')
            original=health.os.listdir;attacked=False;inode=archive.stat().st_ino
            def race(fd):
                nonlocal attacked
                if os.fstat(fd).st_ino==inode and not attacked:
                    attacked=True;archive.rename(archive.with_name('original'));archive.symlink_to(outside,target_is_directory=True)
                return original(fd)
            with patch.object(health.os,'listdir',side_effect=race),self.assertRaises((OSError,ValueError)):
                health.copy_archive(archive,root/'copied')
            self.assertFalse((root/'copied/sentinel').exists())

    def test_new_writer_lock_during_inspection_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            private=Path(directory)
            with self.assertRaisesRegex(RuntimeError,'writer lock identity changed'):
                with health.reader_guard(private):
                    (private/'market-data-staging').mkdir();(private/'market-data-staging/daily-runner.lock').touch()

    def test_trusted_temporary_base_alias_is_resolved_before_child_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();self.validator_fixture(root,'pass')
            base=root/'temp-base';base.mkdir();alias=root/'temp-alias';alias.symlink_to(base,target_is_directory=True)
            with patch.object(health,'ROOT',root),patch.object(health.tempfile,'gettempdir',return_value=str(alias)):
                self.assertTrue(health.command_json('check.py','2026-10-03T21:00:00+09:00')['paper_go'])
            self.assertEqual(list(base.iterdir()),[])
            # Resolving the trusted temp base must not weaken archive ancestry.
            untrusted=alias/'untrusted';untrusted.mkdir()
            with self.assertRaises(OSError):health.inspect_archive(untrusted)

    def test_special_writer_locks_are_rejected_without_hanging(self):
        import socket
        import textwrap
        with tempfile.TemporaryDirectory() as directory:
            private=Path(directory).resolve();lock=private/'market-data-staging/daily-runner.lock';lock.parent.mkdir()
            command=textwrap.dedent("""
                import importlib.util,sys
                from pathlib import Path
                spec=importlib.util.spec_from_file_location('health',sys.argv[1])
                health=importlib.util.module_from_spec(spec);spec.loader.exec_module(health)
                try:
                    with health.reader_guard(Path(sys.argv[2])):pass
                except (ValueError,OSError):print('rejected')
                else:raise AssertionError('special lock accepted')
            """)
            for kind in ('fifo','socket','device_symlink'):
                with self.subTest(kind=kind):
                    sock=None
                    if kind=='fifo':os.mkfifo(lock)
                    elif kind=='socket':
                        sock=socket.socket(socket.AF_UNIX);sock.bind(str(lock))
                    else:lock.symlink_to('/dev/null')
                    try:
                        result=subprocess.run([health.sys.executable,'-c',command,str(ROOT/'scripts/operation_health.py'),str(private)],capture_output=True,text=True,timeout=3,check=True)
                        self.assertEqual(result.stdout.strip(),'rejected')
                    finally:
                        if sock is not None:sock.close()
                        lock.unlink()
            # Direct device metadata is rejected before open, without requiring
            # privileged mknod in the test environment.
            with patch.object(Path,'lstat',return_value=os.stat('/dev/null')),patch.object(health.os,'open') as opened:
                with self.assertRaisesRegex(ValueError,'unsafe writer lock'):
                    with health.reader_guard(private):pass
                opened.assert_not_called()

    def test_regular_lock_replaced_with_fifo_before_open_does_not_block(self):
        import textwrap
        with tempfile.TemporaryDirectory() as directory:
            private=Path(directory).resolve();lock=private/'market-data-staging/daily-runner.lock';lock.parent.mkdir();lock.touch()
            command=textwrap.dedent("""
                import importlib.util,sys,os
                from pathlib import Path
                from unittest.mock import patch
                spec=importlib.util.spec_from_file_location('health',sys.argv[1])
                health=importlib.util.module_from_spec(spec);spec.loader.exec_module(health)
                private=Path(sys.argv[2]);lock=private/'market-data-staging/daily-runner.lock'
                original=os.open
                def race(path,flags,*args,**kwargs):
                    if path==lock:
                        assert flags & os.O_NONBLOCK
                        lock.unlink();os.mkfifo(lock)
                    return original(path,flags,*args,**kwargs)
                with patch.object(health.os,'open',side_effect=race):
                    try:
                        with health.reader_guard(private):pass
                    except ValueError:print('rejected')
                    else:raise AssertionError('FIFO replacement accepted')
            """)
            result=subprocess.run([health.sys.executable,'-c',command,str(ROOT/'scripts/operation_health.py'),str(private)],capture_output=True,text=True,timeout=3,check=True)
            self.assertEqual(result.stdout.strip(),'rejected')


if __name__ == "__main__":
    unittest.main()
