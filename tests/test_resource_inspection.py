"""Resource accounting remains fresh and counts the same filesystem members."""
import os
from pathlib import Path
import tempfile
import unittest

from stock_jp.automation import resources


class ResourceInspectionTests(unittest.TestCase):
    def test_nested_files_links_and_missing_staging_match_original_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            nested = source / 'nested' / 'empty'
            nested.mkdir(parents=True)
            (source / 'first').write_bytes(b'123')
            (nested.parent / 'second').write_bytes(b'12345')
            (source / 'file-link').symlink_to(source / 'first')
            os.link(source / 'first', source / 'hard-link')
            (source / 'broken-link').symlink_to(root / 'missing')
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'excluded').write_bytes(b'x' * 100)
            (source / 'directory-link').symlink_to(outside, target_is_directory=True)
            expected = sum(path.stat().st_size for path in source.rglob('*') if path.is_file())
            report = resources.inspect(source, root / 'missing-staging')
            self.assertEqual(expected, 14)
            self.assertEqual(report['raw_bytes'], expected)
            self.assertEqual(report['staging_bytes'], 0)

    def test_next_inspection_observes_new_changed_and_deleted_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            staging = root / 'staging'
            source.mkdir()
            staging.mkdir()
            path = source / 'response'
            path.write_bytes(b'123')
            self.assertEqual(resources.inspect(source, staging)['raw_bytes'], 3)
            path.write_bytes(b'12345')
            (staging / 'pending').write_bytes(b'12')
            report = resources.inspect(source, staging)
            self.assertEqual((report['raw_bytes'], report['staging_bytes']), (5, 2))
            path.unlink()
            self.assertEqual(resources.inspect(source, staging)['raw_bytes'], 0)
