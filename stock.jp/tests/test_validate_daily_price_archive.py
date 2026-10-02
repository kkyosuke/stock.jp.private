from __future__ import annotations

import csv
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_daily_price_archive import (  # noqa: E402
    ArchiveValidationError,
    CSV_FIELDS,
    validate_archive,
)


class DailyPriceArchiveValidationTest(unittest.TestCase):
    def _write(self, root: Path, rows: list[dict[str, str]]) -> Path:
        path = root / "2025/2025-01-06.csv"
        path.parent.mkdir(parents=True)
        with path.open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=CSV_FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        return path

    @staticmethod
    def _row(**overrides: str) -> dict[str, str]:
        row = {
            "日付": "2025-01-06",
            "銘柄コード": "130A",
            "銘柄名": "テスト",
            "市場・商品区分": "グロース（内国株式）",
            "33業種区分": "情報・通信業",
            "始値": "100",
            "高値": "110",
            "安値": "95",
            "終値": "105",
            "前日比": "5",
            "前日比％": "5",
            "売買高(株)": "123400",
            "取得状態": "OK",
        }
        row.update(overrides)
        return row

    def test_valid_archive_emits_counts_and_hash(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(
                root,
                [
                    self._row(),
                    self._row(
                        **{
                            "銘柄コード": "9999",
                            "始値": "",
                            "高値": "",
                            "安値": "",
                            "終値": "",
                            "前日比": "",
                            "前日比％": "",
                            "売買高(株)": "",
                            "取得状態": "NO_QUOTE",
                        }
                    ),
                ],
            )

            result = validate_archive(root, source_label="fixture")

            self.assertEqual(result["archive_schema"], "daily-prices-pr14-v1")
            self.assertEqual(result["session_count"], 1)
            self.assertEqual(result["total_rows"], 2)
            self.assertEqual(result["total_quotes"], 1)
            self.assertEqual(result["total_no_quotes"], 1)
            self.assertEqual(len(result["sessions"][0]["sha256"]), 64)

    def test_rejects_duplicate_code(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, [self._row(), self._row()])

            with self.assertRaisesRegex(ArchiveValidationError, "duplicate code"):
                validate_archive(root, source_label="fixture")

    def test_rejects_bad_ohlc(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, [self._row(**{"高値": "99"})])

            with self.assertRaisesRegex(ArchiveValidationError, "OHLC ordering"):
                validate_archive(root, source_label="fixture")


if __name__ == "__main__":
    unittest.main()
