from datetime import date, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stock_jp.research.forward_collection import initialize, capture
from stock_jp.research.forward_holdout import CODE, JST, PLAN, freeze, sha
from stock_jp.research.historical_replay import ReplayInputError, validate_inputs
from tests.test_forward_holdout import fixture, Store


class ForwardCollectionTest(unittest.TestCase):
    def frozen_root(self, root):
        fixture(root)
        with patch("stock_jp.research.forward_holdout.now", return_value=datetime(2026, 10, 5, tzinfo=JST)):
            freeze(root, start=date(2026, 10, 6), through=date(2027, 4, 1))

    def test_draft_binds_canonical_paths_and_existing_frozen_plan_without_approval(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.frozen_root(root)
            before = {p: sha(root / p) for p in CODE}
            plan_hash = sha(root / PLAN)
            path = initialize(root)
            raw = path.read_bytes()
            self.assertEqual(initialize(root).read_bytes(), raw)
            manifest = json.loads(raw)
            self.assertEqual(manifest["holdout"]["plan_sha256"], plan_hash)
            self.assertEqual(manifest["holdout"]["plan_path"], PLAN)
            self.assertTrue(all((root / d["path"]).is_file() for d in manifest["datasets"]))
            self.assertEqual(manifest["status"], "DRAFT")
            with self.assertRaises(ReplayInputError) as caught:
                validate_inputs(root=root, manifest_path=path)
            self.assertNotIn("holdout plan hash does not match", caught.exception.blockers)
            self.assertFalse(any("dataset is missing" in x for x in caught.exception.blockers))
            self.assertEqual(before, {p: sha(root / p) for p in CODE})
            self.assertEqual(sha(root / PLAN), plan_hash)

    def test_new_collection_cannot_replace_or_restart_modified_study(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.frozen_root(root)
            path = initialize(root)
            (root / CODE[0]).write_text("Changed rule")
            with self.assertRaisesRegex(ValueError, "contract changed"):
                initialize(root)
            self.assertEqual(json.loads(path.read_text())["status"], "DRAFT")

    def test_receipt_credits_no_formal_evaluations_and_keeps_adopted_generation(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.frozen_root(root)
            pointer = (root / "data/market/jquants/current.json").read_bytes()
            with patch("stock_jp.research.forward_collection.FrozenStore", Store), \
                    patch("stock_jp.research.forward_collection.collect", return_value={"sources": [], "blockers": []}):
                result, receipt = capture(root, cutoff=datetime(2026, 10, 6, tzinfo=JST))
            self.assertTrue(receipt.is_file())
            self.assertEqual(result["generation"], "b" * 64)
            self.assertEqual(result["formal_trades_credited"], 0)
            self.assertEqual(result["formal_monthly_evaluations_credited"], 0)
            self.assertFalse(result["live_promotion_approved"])
            self.assertTrue(result["formal_input_blockers"])
            self.assertEqual((root / "data/market/jquants/current.json").read_bytes(), pointer)
