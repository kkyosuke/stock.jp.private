from tests.operation_test_support import prepare_write
from stock_jp.workspace import project_root, record_path
import copy
import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stock_jp.market_data.paths import MarketDataStorageError, resolve_archive_root
from stock_jp.operation.state import _normalized_source_config
from stock_jp.market_data.prices import validate_tracked_price_snapshot
from tests.operation_test_support import write_price_archive
from tests.test_price_snapshot import CONFIG, JST


class MarketDataStorageTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = copy.deepcopy(CONFIG)

    def test_missing_private_data_never_falls_back_to_public(self):
        public = self.root / 'data/daily-prices'
        public.mkdir(parents=True)
        (prepare_write(public / 'latest.json')).write_text('{}')
        evidence, failures = validate_tracked_price_snapshot(
            root=self.root, active_targets=set(),
            cutoff=datetime(2026, 9, 1, tzinfo=JST), config=self.config)
        self.assertIsNone(evidence)
        self.assertIn('no public fallback', failures[0])

    def test_explicit_private_directory_and_configuration_required(self):
        write_price_archive(self.root, ['1234'])
        self.assertTrue(resolve_archive_root(self.root, self.config).is_dir())
        for key in ['storage', 'archive_root']:
            config = copy.deepcopy(self.config)
            del config['price_source'][key]
            with self.subTest(key=key), self.assertRaises(MarketDataStorageError):
                resolve_archive_root(self.root, config)
        with self.assertRaises(MarketDataStorageError):
            resolve_archive_root(self.root)

    def test_path_escape_public_path_and_absolute_path_rejected(self):
        write_price_archive(self.root, ['1234'])
        for path in ['data/daily-prices', '../external', str(self.root), '.work/market-data']:
            self.config['price_source']['archive_root'] = path
            with self.subTest(path=path), self.assertRaises(MarketDataStorageError):
                resolve_archive_root(self.root, self.config)

    def test_archive_escape_symlink_is_rejected(self):
        write_price_archive(self.root, ['1234'])
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / '.work/market-data/escape').symlink_to(outside, target_is_directory=True)
        self.config['price_source']['archive_root'] = '.work/market-data/escape'
        with self.assertRaises(MarketDataStorageError):
            resolve_archive_root(self.root, self.config)

    def test_manifest_symlink_cannot_escape_private_archive(self):
        write_price_archive(self.root, ['1234'])
        manifest = resolve_archive_root(self.root, self.config) / 'latest.json'
        outside = self.root / 'outside.json'
        manifest.rename(outside)
        manifest.symlink_to(outside)
        evidence, failures = validate_tracked_price_snapshot(
            root=self.root, active_targets=set(),
            cutoff=datetime(2026, 9, 1, tzinfo=JST), config=self.config)
        self.assertIsNone(evidence)
        self.assertIn('escapes', failures[0])

    def test_migration_preserves_explicit_location_and_thresholds(self):
        template = json.loads((project_root() / 'templates/sources.json').read_text())
        config = copy.deepcopy(template)
        config['price_source']['archive_root'] = '.work/market-data/approved-feed'
        config['price_source']['minimum_daily_archive_coverage'] = 1.0
        migrated = _normalized_source_config(config, template)
        self.assertEqual(migrated['price_source']['archive_root'], config['price_source']['archive_root'])
        self.assertEqual(migrated['price_source']['minimum_daily_archive_coverage'], 1.0)
        self.assertEqual(_normalized_source_config(migrated, template), migrated)

    def test_legacy_configuration_migrates_without_copying_public_data(self):
        template = json.loads((project_root() / 'templates/sources.json').read_text())
        migrated = _normalized_source_config({'schema_version': '1.3', 'price_source': {'manifest_path': 'data/market/legacy-prices/latest.json'}}, template)
        self.assertEqual(migrated['price_source']['storage'], 'private_local')
        self.assertEqual(migrated['price_source']['manifest_path'], 'latest.json')
        with self.assertRaises(MarketDataStorageError):
            resolve_archive_root(self.root, migrated)

    def test_yahoo_cli_is_disabled_before_any_network_or_write(self):
        from stock_jp.market_data.legacy_prices import main
        with patch('stock_jp.market_data.legacy_prices._download_file') as download, patch('stock_jp.market_data.legacy_prices.collect') as collect:
            with self.assertRaisesRegex(SystemExit, 'disabled'):
                main()
            download.assert_not_called()
            collect.assert_not_called()
