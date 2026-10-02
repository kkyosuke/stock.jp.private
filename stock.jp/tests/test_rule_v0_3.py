from pathlib import Path
import re
import unittest


ROOT = Path(__file__).parents[1]
RULE = ROOT / "docs" / "rules" / "tenbagger-rule-v0.3.md"


class RuleV03Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = RULE.read_text()

    def test_price_rule_set_is_small_and_complete(self) -> None:
        found = set(re.findall(r"`(V3-P\d)`", self.text))

        self.assertEqual(found, {f"V3-P{number}" for number in range(1, 7)})

    def test_v03_has_no_new_numeric_thresholds(self) -> None:
        self.assertIn("新しい数値閾値は追加せず", self.text)
        self.assertIn("v0.2の12条件からv0.3の6条件へ減り", self.text)
        self.assertIn("売買条件は5条件", self.text)

    def test_recovered_capital_is_not_assumed_to_be_reinvested(self) -> None:
        for phrase in (
            "合格銘柄がなければ現金のまま保有",
            "回収から再投資までの日数",
            "再投資できた場合だけ再配分の効果として計上",
        ):
            self.assertIn(phrase, self.text)

    def test_holdout_contract_is_explicit(self) -> None:
        for phrase in (
            "83成功銘柄でv0.3の優位性を判定しない",
            "比較するチャレンジャーは本書の1案だけ",
            "3年のパージ期間",
            "同じ未見期間で再判定しない",
        ):
            self.assertIn(phrase, self.text)

    def test_local_markdown_links_resolve(self) -> None:
        for target in re.findall(r"\[[^]]*\]\(([^)]+)\)", self.text):
            if "://" in target or target.startswith("#"):
                continue
            local_path = target.split("#", 1)[0]
            self.assertTrue((RULE.parent / local_path).exists(), target)


if __name__ == "__main__":
    unittest.main()
