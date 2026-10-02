from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "docs/operations/nightly-operation-v0.1.md"


class NightlyOperationDocumentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = DOCUMENT.read_text(encoding="utf-8")

    def test_local_markdown_links_resolve(self) -> None:
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", self.text):
            if "://" in target:
                continue
            self.assertTrue((DOCUMENT.parent / target).resolve().exists(), target)

    def test_one_request_outputs_and_wait_are_explicit(self) -> None:
        for phrase in (
            "今日の夜間運用を実行して",
            "next-day-actions.csv",
            "research-results.md",
            "次回の平日21:00まで",
            "次回夜まで待機",
        ):
            self.assertIn(phrase, self.text)

    def test_no_automated_broker_submission(self) -> None:
        for phrase in (
            "証券会社へ送信しない",
            "翌朝8:45〜8:55",
            "手入力",
            "HUMAN_ONLY",
        ):
            self.assertIn(phrase, self.text)


if __name__ == "__main__":
    unittest.main()
