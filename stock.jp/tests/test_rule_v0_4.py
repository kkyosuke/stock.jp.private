from pathlib import Path
import re
import unittest


ROOT = Path(__file__).parents[1]
RULE = ROOT / "docs" / "rules" / "tenbagger-rule-v0.4.md"


class RuleV04Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = RULE.read_text(encoding="utf-8")

    def test_full_strategy_mandate_has_no_fixed_safety_allocation(self) -> None:
        for phrase in (
            "テンバガー戦略資金 | 100%",
            "固定的な安全資産 | 0%",
            "戦略待機資金",
            "固定的に80%を残すこともルールではない",
        ):
            self.assertIn(phrase, self.text)

    def test_staged_caps_can_reach_full_deployment(self) -> None:
        for phrase in (
            "初回購入 | 運用資産の1% | 運用資産の5%",
            "1回の追加購入 | 運用資産の0.5% | 運用資産の2.5%",
            "1銘柄の累計取得原価 | 運用資産の3% | 運用資産の10%",
            "候補群の累計取得原価 | 運用資産の20% | 運用資産の100%",
            "同一業種の累計取得原価 | 運用資産の6% | 運用資産の20%",
        ):
            self.assertIn(phrase, self.text)

    def test_live_has_no_elapsed_time_or_run_count_gate(self) -> None:
        self.assertIn("PAPERの経過日数と連続run数には最低条件を設けない", self.text)
        self.assertNotIn("v0.4で最低12か月の前向きPAPER", self.text)
        self.assertIn("v04_holdout_promotion", self.text)

    def test_local_markdown_links_resolve(self) -> None:
        for target in re.findall(r"\[[^]]*\]\(([^)]+)\)", self.text):
            if "://" in target or target.startswith("#"):
                continue
            local_path = target.split("#", 1)[0]
            self.assertTrue((RULE.parent / local_path).exists(), target)


if __name__ == "__main__":
    unittest.main()
