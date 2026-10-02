import csv
from pathlib import Path
from statistics import median
import unittest


DATASET = (
    Path(__file__).parents[1]
    / "data"
    / "tenbagger-survivor-price-episodes-2016-2026.csv"
)


class ValidationDatasetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with DATASET.open(encoding="utf-8") as handle:
            cls.rows = list(csv.DictReader(handle))

    def test_reported_candidate_counts(self) -> None:
        two_year = [row for row in self.rows if row["2y_qualified"] == "True"]
        three_year = [row for row in self.rows if row["3y_qualified"] == "True"]

        self.assertEqual(len(self.rows), 83)
        self.assertEqual(len(two_year), 55)
        self.assertEqual(len(three_year), 82)

    def test_reported_three_year_outcomes(self) -> None:
        rows = [row for row in self.rows if row["3y_qualified"] == "True"]
        drawdowns = [float(row["3y_post_hit_max_drawdown"]) for row in rows]
        latest_multiples = [float(row["3y_latest_multiple"]) for row in rows]

        self.assertEqual(sum(row["3y_retained_10x"] == "True" for row in rows), 24)
        self.assertEqual(sum(drawdown <= -0.8 for drawdown in drawdowns), 35)
        self.assertEqual(sum(multiple < 1 for multiple in latest_multiples), 5)
        self.assertAlmostEqual(median(drawdowns), -0.76595)
        self.assertAlmostEqual(median(latest_multiples), 6.0707)


if __name__ == "__main__":
    unittest.main()
