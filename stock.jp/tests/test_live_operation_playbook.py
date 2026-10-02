from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = ROOT / "docs/operations/live-operation-playbook-v0.1.md"


class LiveOperationPlaybookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = PLAYBOOK.read_text(encoding="utf-8")

    def test_local_markdown_links_resolve(self) -> None:
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", self.text):
            if "://" in target:
                continue
            resolved = (PLAYBOOK.parent / target).resolve()
            self.assertTrue(resolved.exists(), target)

    def test_review_frequencies_and_actions_are_fixed(self) -> None:
        for phrase in (
            "毎営業日",
            "毎週末",
            "各月末",
            "四半期開示後",
            "BUY / WATCH / WAIT / KEEP / ADD / REDUCE / SELL / NO-ACTION",
        ):
            self.assertIn(phrase, self.text)

    def test_entry_timing_and_non_chasing_are_explicit(self) -> None:
        for phrase in (
            "8:45〜8:55",
            "9:00より前に当日限りの指値注文",
            "午後に指値を引き上げず",
            "成行、引成、成行化する不成注文は新規購入に使わない",
        ):
            self.assertIn(phrase, self.text)

    def test_market_regime_only_controls_new_risk(self) -> None:
        for phrase in (
            "銘柄の100点スコアへ加点・減点しない",
            "指数だけを理由に既存銘柄を売却せず",
            "反実仮想もログへ残し",
        ):
            self.assertIn(phrase, self.text)

    def test_private_operation_data_is_ignored(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("operations/private/", gitignore.splitlines())
        self.assertIn(".usagi/", gitignore.splitlines())


if __name__ == "__main__":
    unittest.main()
