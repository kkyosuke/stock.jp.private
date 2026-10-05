"""Rebuild the frozen financial panel offline, or check it without changing it."""
import argparse
from datetime import date
import json
from pathlib import Path
import tempfile

from stock_jp.research.financial_inputs import build
from stock_jp.workspace import project_root

ROOT = project_root().resolve()
RESULT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    options = dict(price_register=ROOT / "research/validation-2026-10-05/price-input-register.json",
                   start=date(2024, 12, 30), through=date(2026, 8, 31))
    if args.check:
        expected = json.loads((RESULT / "summary.json").read_text())
        with tempfile.TemporaryDirectory(prefix=".financial-reproduction-", dir=ROOT / "research") as folder:
            regenerated = build(ROOT, output=Path(folder), **options)
            if regenerated != expected:
                raise ValueError("frozen financial inputs do not reproduce; review inputs and code")
        print("PASS: all financial panel outputs and provenance reproduce without network")
    else:
        summary = build(ROOT, output=RESULT, **options)
        print(json.dumps({key: summary[key] for key in ("status", "issue_month_rows", "ttm_sales_available",
                        "eight_summary_quarters", "full_rule_assessments_generated")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
