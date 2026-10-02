from pathlib import Path
import re
import unittest


ROOT = Path(__file__).parents[1]
RULE = ROOT / "docs" / "rules" / "tenbagger-rule-v0.2.md"


class RuleV02Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = RULE.read_text()

    def test_all_sell_rule_ids_are_present(self) -> None:
        expected = {
            *(f"S-A{number}" for number in range(1, 7)),
            *(f"S-B{number}" for number in range(1, 6)),
            *(f"S-C{number}" for number in range(1, 7)),
            *(f"S-D{number}" for number in range(1, 7)),
        }
        found = re.findall(r"`(S-[ABCD]\d)`", self.text)

        self.assertEqual(set(found), expected)

    def test_price_and_time_exit_milestones_are_fixed(self) -> None:
        for milestone in ("63営業日", "126営業日", "252営業日", "504営業日", "3暦年"):
            self.assertIn(milestone, self.text)
        self.assertIn("`R126 <= -40%`", self.text)
        self.assertIn("`R252 <= -20%`", self.text)
        self.assertIn("`R504 < 0%`", self.text)

    def test_local_markdown_links_resolve(self) -> None:
        for target in re.findall(r"\[[^]]*\]\(([^)]+)\)", self.text):
            if "://" in target or target.startswith("#"):
                continue
            local_path = target.split("#", 1)[0]
            self.assertTrue((RULE.parent / local_path).exists(), target)


if __name__ == "__main__":
    unittest.main()
