"""Create a private operation decision log from the tracked template."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = PROJECT_ROOT / "operations/templates/decision-log-template.md"
DEFAULT_LOG_ROOT = PROJECT_ROOT / "operations/private/decisions"
VALID_MODES = (
    "new-entry",
    "daily-event",
    "weekly",
    "monthly",
    "quarterly",
    "ad-hoc",
)


def render_log(*, day: str, code: str, company: str, mode: str) -> str:
    date.fromisoformat(day)
    if not re.fullmatch(r"[0-9A-Za-z-]+", code):
        raise ValueError("code must contain only letters, digits, or hyphens")
    if not company.strip():
        raise ValueError("company is required")
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {', '.join(VALID_MODES)}")
    decision_id = f"{day}-{code}-{mode}"
    return (
        TEMPLATE_PATH.read_text(encoding="utf-8")
        .replace("{{DECISION_ID}}", decision_id)
        .replace("{{DATE}}", day)
        .replace("{{MODE}}", mode)
        .replace("{{CODE}}", code)
        .replace("{{COMPANY}}", company.strip())
    )


def create_log(
    *, day: str, code: str, company: str, mode: str, output: Path | None = None
) -> Path:
    destination = output or DEFAULT_LOG_ROOT / f"{day}-{code}-{mode}.md"
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        render_log(day=day, code=code, company=company, mode=mode),
        encoding="utf-8",
    )
    return destination


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--code", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--mode", choices=VALID_MODES, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--stdout", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.stdout:
        print(
            render_log(
                day=args.date,
                code=args.code,
                company=args.company,
                mode=args.mode,
            ),
            end="",
        )
        return
    destination = create_log(
        day=args.date,
        code=args.code,
        company=args.company,
        mode=args.mode,
        output=args.output,
    )
    print(destination)


if __name__ == "__main__":
    main()
