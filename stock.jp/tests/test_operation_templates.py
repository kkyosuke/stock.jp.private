import re
import unicodedata
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = sorted((ROOT / "operations/templates").glob("*.md"))

DELIMITER = re.compile(r":?-+:?")


def _display_width(text: str) -> int:
    """Width of one table cell as a terminal or linter counts it."""

    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _cells(line: str) -> list[str]:
    return [cell for cell in line.strip()[1:-1].split("|")]


def _is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _tables(text: str) -> list[list[str]]:
    tables: list[list[str]] = []
    current: list[str] = []
    for line in text.split("\n"):
        if _is_table_row(line):
            current.append(line)
            continue
        if current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


class OperationTemplateTableTest(unittest.TestCase):
    """Templates are copied verbatim into the private run directories, which are
    linted with markdownlint. A delimiter row written as ``|---|---|`` under a
    padded header trips MD060/table-column-style and fails the private
    format-check for every run that includes the file."""

    def test_templates_are_present(self) -> None:
        self.assertTrue(TEMPLATES)

    def test_table_columns_line_up_with_the_header(self) -> None:
        for template in TEMPLATES:
            for table in _tables(template.read_text(encoding="utf-8")):
                widths = [_display_width(cell) for cell in _cells(table[0])]
                for row in table[1:]:
                    self.assertEqual(
                        [_display_width(cell) for cell in _cells(row)],
                        widths,
                        f"{template.name}: {row}",
                    )

    def test_column_alignment_markers_are_kept(self) -> None:
        for template in TEMPLATES:
            for table in _tables(template.read_text(encoding="utf-8")):
                if len(table) < 2:
                    continue
                cells = [cell.strip() for cell in _cells(table[1])]
                if not all(DELIMITER.fullmatch(cell) for cell in cells):
                    continue
                for cell in cells:
                    self.assertTrue(
                        cell.strip("-:") == "" and "-" in cell,
                        f"{template.name}: {cell}",
                    )


if __name__ == "__main__":
    unittest.main()
