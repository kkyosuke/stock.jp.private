import copy
import unittest

from scripts.investment_case import InvestmentCaseError, evaluate_investment_case


def complete_case() -> dict:
    sources = ["source-1"]
    return {
        "schema_version": "1.0",
        "code": "1234",
        "company": "Example",
        "as_of_jst": "2026-08-31T18:30:00+09:00",
        "price": {"value": 100, "source_ids": sources},
        "capital": {
            "issued_shares": 1_000_000,
            "treasury_shares": 10_000,
            "potential_securities": [
                {
                    "id": "option-1",
                    "shares_at_10x_price": 10_000,
                    "variable_strike": False,
                    "exercise_cash": 2_000_000,
                    "source_ids": sources,
                }
            ],
            "planned_base_new_shares": 0,
            "planned_downside_new_shares": 50_000,
            "cash": 20_000_000,
            "debt": 5_000_000,
            "source_ids": sources,
        },
        "financials": {
            "ttm_revenue": 200_000_000,
            "assumed_normalized_net_margin_pct": 20,
            "source_ids": sources,
        },
        "market": {
            "sam_3y": 10_000_000_000,
            "attainable_share_pct": 20,
            "capacity_revenue_limit": 2_000_000_000,
            "leader_share_pct": 30,
            "sam_evidence": "THIRD_PARTY_RECALCULABLE",
            "capacity_evidence": "CURRENT_OR_CONTRACTED",
            "market_growth_verified": True,
            "share_gain_verified": True,
            "share_overtake_supported": False,
            "source_ids": sources,
        },
        "valuation": {
            "proposed_exit_pe": 20,
            "n3_completeness": "COMPLETE",
            "margin_exception_supported": False,
            "competitor_scale_consistency": "CONSISTENT",
            "direct_competitors": [
                {
                    "name": "A",
                    "exit_pe": 20,
                    "net_margin_pct": 20,
                    "market_cap": 2_000_000_000,
                    "source_ids": sources,
                },
                {
                    "name": "B",
                    "exit_pe": 25,
                    "net_margin_pct": 25,
                    "market_cap": 3_000_000_000,
                    "source_ids": sources,
                },
                {
                    "name": "C",
                    "exit_pe": 30,
                    "net_margin_pct": 30,
                    "market_cap": 4_000_000_000,
                    "source_ids": sources,
                },
            ],
            "source_ids": sources,
        },
        "kpi_path": {
            "decomposition": "COMPLETE",
            "year3_revenue": 1_000_000_000,
            "year3_net_margin_pct": 20,
            "drivers": [
                {
                    "name": "customers",
                    "current": 100,
                    "year3": 500,
                    "unit": "companies",
                    "source_ids": sources,
                }
            ],
            "source_ids": sources,
        },
        "eligibility": {
            "hard_gates_passed": True,
            "other_score": 50,
            "liquidity_passed": True,
            "source_ids": sources,
        },
        "risk": {"unresolved_major_red_flags": []},
    }


class InvestmentCaseTest(unittest.TestCase):
    def test_complete_case_calculates_dilution_som_and_ten_x_path(self) -> None:
        result = evaluate_investment_case(complete_case())

        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["entry_ready"])
        self.assertEqual(result["dilution"]["n0_fully_diluted_shares"], 1_000_000)
        self.assertEqual(result["valuation"]["m10_required_market_cap"], 1_000_000_000)
        self.assertEqual(result["valuation"]["required_revenue"], 250_000_000)
        self.assertEqual(result["market"]["som_3y"], 2_000_000_000)
        self.assertGreaterEqual(result["scores"]["market"], 8)
        self.assertGreaterEqual(result["scores"]["reverse_calculation"], 10)

    def test_unknown_values_are_reported_without_guessing(self) -> None:
        case = complete_case()
        case["market"]["sam_3y"] = None
        case["capital"]["potential_securities"] = None

        result = evaluate_investment_case(case)

        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertFalse(result["entry_ready"])
        self.assertIn("market.sam_3y", result["missing_fields"])
        self.assertIn("capital.potential_securities", result["missing_fields"])

    def test_partial_dilution_does_not_turn_unknown_exercise_cash_into_zero(
        self,
    ) -> None:
        case = complete_case()
        case["capital"]["potential_securities"][0]["exercise_cash"] = None
        case["market"]["sam_3y"] = None

        result = evaluate_investment_case(case)

        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(
            result["partial_calculations"]["dilution"]["n0_fully_diluted_shares"],
            1_000_000,
        )
        self.assertIsNone(
            result["partial_calculations"]["dilution"][
                "exercise_cash_separate_from_shares"
            ]
        )

    def test_som_shortfall_is_a_final_failure_not_an_incomplete_result(self) -> None:
        case = complete_case()
        case["market"]["capacity_revenue_limit"] = 100_000_000

        result = evaluate_investment_case(case)

        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["entry_ready"])
        self.assertIn("required year-3 revenue exceeds SOM-3Y", result["failures"])

    def test_variable_strike_security_cannot_be_treated_as_fixed_dilution(self) -> None:
        case = complete_case()
        case["capital"]["potential_securities"][0]["variable_strike"] = True

        result = evaluate_investment_case(case)

        self.assertEqual(result["status"], "FAIL")
        self.assertIn("variable-strike securities", result["failures"][0])

    def test_unknown_variable_strike_share_count_is_a_final_failure(self) -> None:
        case = complete_case()
        case["capital"]["potential_securities"][0]["variable_strike"] = True
        case["capital"]["potential_securities"][0]["shares_at_10x_price"] = None

        result = evaluate_investment_case(case)

        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["entry_ready"])
        self.assertIn(
            "capital.potential_securities[0].shares_at_10x_price",
            result["missing_fields"],
        )

    def test_exit_pe_safety_cap_is_enforced(self) -> None:
        case = copy.deepcopy(complete_case())
        case["valuation"]["proposed_exit_pe"] = 41

        with self.assertRaisesRegex(InvestmentCaseError, "must be <= 40"):
            evaluate_investment_case(case)

    def test_unsupported_leader_overtake_caps_reverse_score_at_six(self) -> None:
        case = complete_case()
        case["market"]["sam_3y"] = 500_000_000
        case["market"]["leader_share_pct"] = 20
        case["market"]["share_overtake_supported"] = False

        result = evaluate_investment_case(case)

        self.assertLessEqual(result["scores"]["reverse_calculation"], 6)
        self.assertEqual(result["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
