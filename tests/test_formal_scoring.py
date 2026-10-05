from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stock_jp.strategy.scoring import CHOICES, GATES, ITEMS, NUMERIC, evaluate_score, item_points
from stock_jp.workspace import project_root


def fixture(root):
    (root / "source.txt").write_text("Synthetic evidence only")
    evidence = {"source_ids": ["s"], "locator": "p.1", "reason": "Synthetic boundary fixture", "calculation": "Explicit fixture value"}
    items = {}
    for key in ITEMS:
        value = next(k for k, v in CHOICES[key][2].items() if v == max(CHOICES[key][2].values())) if key in CHOICES else 31 if key in NUMERIC else 0 if key == "4.6.2" else 11
        items[key] = {"status": "KNOWN", "value": value, **evidence}
    return {"schema_version": "1.0", "code": "0001", "information_cutoff_at_jst": "2026-09-30T15:30:00+09:00",
            "sources": {"s": {"path": "source.txt", "sha256": hashlib.sha256((root / "source.txt").read_bytes()).hexdigest(), "url": "https://example.test/fixture", "primary_source": True, "published_at_jst": "2026-09-29T14:00:00+09:00", "retrieved_at_jst": "2026-10-05T12:00:00+09:00"}},
            "hard_gates": {key: {"status": "PASS", **evidence} for key in GATES},
            "items": items, "major_red_flags_resolved": True,
            "reverse_score_cap": 15, "reverse_cap_evidence": evidence}


class FormalScoringTest(unittest.TestCase):
    def test_all_frozen_score_boundaries(self):
        values = {
            "4.1.1": [(-1, 0), (0, 1), (9.99, 1), (10, 3), (20, 6), (30, 8)],
            "4.1.2": [(0, 1), (10, 3), (20, 6), (30, 8)],
            "4.1.3": [(-0.01, 0), (0, 2), (5, 4)],
            "4.2.1": [(0, 0), (0.01, 2), (5, 4), (10, 5)],
            "4.2.2": [(0, 0), (0.01, 2), (5, 4), (10, 5)],
            "4.3.1": [(-1, 0), (0, 2), (2, 4), (5, 6)],
            "4.3.2": [(-1, 0), (0, 1), (5, 3), (10, 5)],
            "4.6.2": [(-1, 3), (0, 3), (0.01, 2), (3, 2), (3.01, 1), (7, 0)],
        }
        self.assertEqual(sum(value[0] for value in ITEMS.values()), 100)
        self.assertEqual(len(ITEMS), 25)
        for key, cases in values.items():
            for value, expected in cases:
                with self.subTest(item=key, value=value):
                    self.assertEqual(item_points(key, value), expected)
        for key, (_, _, choices) in CHOICES.items():
            for value, expected in choices.items():
                self.assertEqual(item_points(key, value), expected)

    def test_complete_candidate_does_not_authorize_order(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = evaluate_score(fixture(root), root=root)
            self.assertEqual(result["formal_total"], 100)
            self.assertTrue(result["primary_candidate"])
            self.assertFalse(result["entry_ready"])

    def test_missing_inputs_are_not_zero_and_gate_fail_does_not_create_score(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            doc = fixture(root)
            doc["items"]["4.1.1"] = {"status": "NA", "reason": "No diluted denominator"}
            result = evaluate_score(doc, root=root)
            self.assertIsNone(result["formal_total"])
            self.assertEqual(result["status"], "WAIT")
            self.assertIn("4.1.1", result["missing_inputs"])
            self.assertFalse(result["primary_candidate"])
            doc = fixture(root)
            doc["hard_gates"]["HG2"]["status"] = "FAIL"
            result = evaluate_score(doc, root=root)
            self.assertEqual(result["status"], "HARD_GATE_FAIL")
            self.assertIsNone(result["formal_total"])

    def test_source_hash_future_timestamp_and_reason_are_required(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = fixture(root)
            for field, value in (("published_at_jst", "2026-10-01T00:00:00+09:00"), ("sha256", "0" * 64), ("path", "../outside.txt")):
                bad = deepcopy(good)
                bad["sources"]["s"][field] = value
                with self.assertRaises(ValueError):
                    evaluate_score(bad, root=root)
            bad = deepcopy(good)
            bad["items"]["4.5.1"].pop("locator")
            with self.assertRaises(ValueError):
                evaluate_score(bad, root=root)

    def test_reverse_cap_and_unresolved_red_flags(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            doc = fixture(root)
            doc["reverse_score_cap"] = 6
            result = evaluate_score(doc, root=root)
            self.assertEqual(result["formal_total"], 91)
            self.assertFalse(result["primary_candidate"])
            doc["reverse_score_cap"] = 15
            doc["major_red_flags_resolved"] = False
            self.assertFalse(evaluate_score(doc, root=root)["primary_candidate"])

    def test_actual_ficha_review_remains_wait_with_no_fabricated_total(self):
        root = project_root()
        doc = json.loads((root / "research/formal-validation-2026-10-05/4052-score-input.json").read_text())
        result = evaluate_score(doc, root=root)
        self.assertEqual(result["status"], "WAIT")
        self.assertIsNone(result["formal_total"])
        self.assertFalse(result["entry_ready"])
        self.assertEqual(result["reviewed_item_count"], 7)
