import csv
from decimal import Decimal
from pathlib import Path
import unittest


DATASET = (
    Path(__file__).parents[1]
    / "data"
    / "tenbagger-v0.2-price-only-pnl-2016-2026.csv"
)


class V02PriceOnlyPnlDatasetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with DATASET.open(encoding="utf-8") as handle:
            cls.rows = list(csv.DictReader(handle))

    def test_reported_aggregate(self) -> None:
        total_value = sum(
            Decimal(row["gross_value_multiple"]) for row in self.rows
        )
        buy_hold_value = sum(
            Decimal(row["buy_hold_multiple"]) for row in self.rows
        )

        self.assertEqual(len(self.rows), 83)
        self.assertEqual(total_value, Decimal("762.989326"))
        self.assertEqual(buy_hold_value, Decimal("769.887346"))
        self.assertEqual(
            sum(Decimal(row["gross_value_multiple"]) > 1 for row in self.rows),
            76,
        )

    def test_each_q0_is_fully_sold_or_marked(self) -> None:
        for row in self.rows:
            sold = sum(
                Decimal(action.split(":", 1)[1].split("Q0", 1)[0])
                for action in row["actions"].split("; ")
                if action
            )
            self.assertEqual(
                sold + Decimal(row["remaining_fraction"]),
                1,
                row["code"],
            )


if __name__ == "__main__":
    unittest.main()
