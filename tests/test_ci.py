"""Real Git comparisons and archive checks for the inexpensive data CI path."""
import base64
import csv
import json
from pathlib import Path
import tempfile
import unittest

from stock_jp.automation import ci
from stock_jp.market_data import observations, storage
import tests.test_nightly_market_data as nightly_fixture


class DataCITest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / '.work/source'
        self.store = self.root / ci.ARCHIVE
        (self.root / '.gitignore').write_text('.work/\n')
        storage.atomic(self.source / 'jquants/empty.json', b'{"data":[]}\n')
        storage.pack(self.source, self.store)
        self.git('init')
        self.git('config', 'user.email', 'synthetic@example.test')
        self.git('config', 'user.name', 'Synthetic')
        self.base = self.commit()

    def git(self, *args):
        return ci.publication.git(self.root, *args).decode().strip()

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '--allow-empty', '-m', 'Synthetic CI fixture')
        return self.git('rev-parse', 'HEAD')

    def new_archive(self):
        storage.atomic(self.source / 'jquants/new.json', b'{"data":[],"synthetic":true}\n')
        storage.pack(self.source, self.store)
        return self.commit()

    def test_data_change_uses_real_archive_verification(self):
        head = self.new_archive()
        self.assertEqual(ci.plan(self.root, self.base, head)['mode'], 'data')
        self.assertTrue(ci.validate_data(self.root, self.base, head)['verified'])
        pointer = json.loads((self.store / 'current.json').read_text())
        manifest = json.loads(storage.manifest_bytes(self.store, pointer))
        member = manifest['files']['jquants/new.json']['object']
        storage.atomic(self.store / member, b'corrupt')
        head = self.commit()
        with self.assertRaisesRegex(ValueError, 'checksum'):
            ci.validate_data(self.root, self.base, head)

    def test_source_change_beyond_300_data_files_still_runs_code_suite(self):
        for index in range(305):
            storage.atomic(self.store / f'synthetic-{index}.json', b'{}')
        storage.atomic(self.root / 'src/changed.py', b'print("synthetic")\n')
        head = self.commit()
        self.assertEqual(ci.plan(self.root, self.base, head), {'mode': 'code', 'changed_files': 306})

    def test_unknown_empty_and_nonancestor_comparisons_use_full_suite(self):
        head = self.new_archive()
        for base, target in (('', head), ('0' * 40, head), ('f' * 40, head),
                             (head, head), (head, self.base)):
            with self.subTest(base=base):
                self.assertEqual(ci.plan(self.root, base, target)['mode'], 'full')

    def test_config_workflow_rules_and_source_deletion_require_code_suite(self):
        for path in ('config/policy.json', '.github/workflows/ci.yml', 'docs/rules/policy.md', 'src/logic.py'):
            with self.subTest(path=path):
                storage.atomic(self.root / path, b'{}\n')
                head = self.commit()
                self.assertEqual(ci.plan(self.root, self.base, head)['mode'], 'code')
                (self.root / path).unlink()
                deleted = self.commit()
                self.assertEqual(ci.plan(self.root, head, deleted)['mode'], 'code')

    def test_only_documentation_runs_document_contracts_but_mixed_changes_do_not(self):
        for path in ('README.md', 'docs/operations/automation.md'):
            storage.atomic(self.root / path, b'# Synthetic documentation\n')
        docs = self.commit()
        self.assertEqual(ci.plan(self.root, self.base, docs)['mode'], 'docs')
        (self.root / 'README.md').unlink()
        deleted = self.commit()
        self.assertEqual(ci.plan(self.root, docs, deleted)['mode'], 'docs')
        for path in ('docs/example.py', 'pyproject.toml', 'data/market/observations/new.csv'):
            with self.subTest(path=path):
                storage.atomic(self.root / path, b'synthetic')
                head = self.commit()
                self.assertEqual(ci.plan(self.root, self.base, head)['mode'], 'code')
                (self.root / path).unlink()

    def test_immutable_archive_rewrites_deletions_and_symlinks_are_rejected(self):
        original = next((self.store / 'objects').rglob('*.json'))
        data = original.read_bytes()
        for kind in ('rewrite', 'delete', 'symlink'):
            with self.subTest(kind=kind):
                if original.exists() or original.is_symlink():
                    original.unlink()
                if kind == 'rewrite':
                    original.write_bytes(b'{}')
                elif kind == 'symlink':
                    original.symlink_to(self.source / 'jquants/empty.json')
                head = self.commit()
                self.assertEqual(ci.plan(self.root, self.base, head)['mode'], 'data')
                with self.assertRaises(ValueError):
                    ci.validate_data(self.root, self.base, head)
                if original.is_symlink():
                    original.unlink()
                original.write_bytes(data)

    def test_added_orphan_payload_cannot_hide_in_data_only_change(self):
        self.new_archive()
        raw = b'{"unreferenced":true}\n'
        digest = storage.digest(raw)
        storage.atomic(self.store / f'objects/{digest[:2]}/{digest}.json', raw)
        head = self.commit()
        with self.assertRaisesRegex(ValueError, 'unreferenced'):
            ci.validate_data(self.root, self.base, head)

    def test_unexpected_data_path_is_rejected_instead_of_skipping_validation(self):
        storage.atomic(self.store / 'unexpected.py', b'pass\n')
        head = self.commit()
        self.assertEqual(ci.plan(self.root, self.base, head)['mode'], 'data')
        with self.assertRaisesRegex(ValueError, 'unexpected archive path'):
            ci.validate_data(self.root, self.base, head)

    def test_observation_latest_must_match_schema_and_history(self):
        row = dict.fromkeys(observations.CORPORATE_FIELDS, '')
        row.update(action_id='synthetic', code='1234', effective_date='2026-10-02')
        observations.save_rows(self.root, 'corporate-actions.csv', [row])
        head = self.commit()
        self.assertEqual(ci.validate_data(self.root, self.base, head)['observation_tables'], 1)
        latest = self.root / ci.OBSERVATIONS / 'corporate-actions/latest.csv'
        row['code'] = '9999'
        with latest.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=observations.CORPORATE_FIELDS)
            writer.writeheader()
            writer.writerow(row)
        head = self.commit()
        with self.assertRaisesRegex(ValueError, 'latest view differs'):
            ci.validate_data(self.root, self.base, head)

    def test_actual_nightly_candidate_and_tampered_acquisition_record(self):
        fixture = nightly_fixture.NightlyMarketDataTest('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)

        def copy_tree(head):
            for path, blob in fixture.repo.tree(head).items():
                storage.atomic(self.root / path, base64.b64decode(fixture.repo.objects[blob]['content']))

        copy_tree(fixture.context['sha'])
        self.base = self.commit()
        fixture.run_preview()
        copy_tree(fixture.repo.pulls[0]['head']['sha'])
        head = self.commit()
        result = ci.validate_data(self.root, self.base, head)
        self.assertTrue(result['verified'])
        self.assertEqual(result['acquisition_records'], 1)
        record = next((self.root / ci.RECORDS).glob('*.json'))
        value = json.loads(record.read_text())
        value['requests'] = []
        record.write_text(json.dumps(value))
        head = self.commit()
        with self.assertRaisesRegex(ValueError, 'differs from archived journal'):
            ci.validate_data(self.root, self.base, head)
