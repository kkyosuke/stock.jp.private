import unittest

from scripts.position_sizing import allocation_caps, validate_purchase_increment


class PositionSizingTest(unittest.TestCase):
    def test_v04_can_deploy_the_full_strategy_account(self) -> None:
        caps = allocation_caps("v0.4")

        self.assertEqual(caps.initial_entry_pct, 5.0)
        self.assertEqual(caps.add_entry_pct, 2.5)
        self.assertEqual(caps.single_name_cost_pct, 10.0)
        self.assertEqual(caps.candidate_pool_cost_pct, 100.0)
        self.assertEqual(caps.industry_cost_pct, 20.0)
        self.assertEqual(caps.max_holdings, 12)

    def test_v04_rejects_an_oversized_initial_tranche(self) -> None:
        with self.assertRaisesRegex(ValueError, "v0.4 tranche cap 5%"):
            validate_purchase_increment(
                rule_version="v0.4", action="BUY", position_pct=5.01
            )

    def test_v04_rejects_an_oversized_add_tranche(self) -> None:
        with self.assertRaisesRegex(ValueError, "v0.4 tranche cap 2.5%"):
            validate_purchase_increment(
                rule_version="v0.4", action="ADD", position_pct=2.51
            )

    def test_sales_are_not_limited_by_purchase_tranche_caps(self) -> None:
        validate_purchase_increment(
            rule_version="v0.4", action="SELL", position_pct=100.0
        )


if __name__ == "__main__":
    unittest.main()
