"""Rebuild the saved diagnostics offline and compare every result byte."""
from datetime import date
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from stock_jp.research.exit_validation import build as build_exits
from stock_jp.research.mrs_validation import build as build_mrs
from stock_jp.strategy.scoring import evaluate_score
from stock_jp.workspace import project_root

root = project_root()
saved = root / "research/formal-validation-2026-10-05"
prior = root / "research/validation-2026-10-05"
with TemporaryDirectory(prefix=".formal-reproduce-", dir=root / "research") as temporary:
    output = Path(temporary)
    score = evaluate_score(json.loads((saved / "4052-score-input.json").read_text()), root=root)
    (output / "4052-score.json").write_text(json.dumps(score, ensure_ascii=False, indent=2) + "\n")
    build_mrs(root, price_register=prior / "price-input-register.json",
              market_components=prior / "market-components.json",
              ci_vintages=saved / "ci-vintages.json", output=output / "mrs-results.json")
    build_exits(root, register_path=prior / "price-input-register.json",
                candidate_path=prior / "continuous-candidates.csv", split_path=prior / "splits.json",
                start=date(2025, 1, 6), through=date(2026, 10, 2), output=output / "exits")
    mismatches = []
    results = {}
    for generated in sorted(output.rglob("*")):
        if not generated.is_file():
            continue
        relative = generated.relative_to(output).as_posix()
        digest = hashlib.sha256(generated.read_bytes()).hexdigest()
        expected = saved / relative
        results[relative] = digest
        if not expected.is_file() or hashlib.sha256(expected.read_bytes()).hexdigest() != digest:
            mismatches.append(relative)
    print(json.dumps({"matched": not mismatches, "mismatches": mismatches, "output_sha256": results}, indent=2))
    raise SystemExit(bool(mismatches))
