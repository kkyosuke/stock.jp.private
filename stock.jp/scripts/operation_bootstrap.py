"""Initialize state and report fail-closed PAPER and LIVE readiness."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from scripts.limited_live import validate_applied_limited_live
    from scripts.live_gate_evidence import validate_promoted_evidence_bundle
    from scripts.operation_policy import policy_status
    from scripts.operation_state import (
        initialize_or_migrate_workspace,
        validate_workspace,
    )
    from scripts.price_snapshot import (
        active_target_codes,
        validate_tracked_price_snapshot,
    )
except ModuleNotFoundError:  # Direct execution from scripts/
    from limited_live import validate_applied_limited_live
    from live_gate_evidence import validate_promoted_evidence_bundle
    from operation_policy import policy_status
    from operation_state import initialize_or_migrate_workspace, validate_workspace
    from price_snapshot import active_target_codes, validate_tracked_price_snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[1]
JST = ZoneInfo("Asia/Tokyo")


def _parse_jst(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("datetime must include a UTC offset")
    return parsed.astimezone(JST)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _source_config(root: Path, private: Path) -> dict[str, Any]:
    template = _read_json(root / "operations/templates/source-config-template.json")
    configured = _read_json(private / "source-config.json")
    merged = {**template, **configured}
    for section in ("price_source", "edinet", "manual_primary_sources"):
        merged[section] = {
            **template.get(section, {}),
            **configured.get(section, {}),
        }
    return merged


def _private_evidence_failure(
    *, root: Path, private: Path, label: str, relative: Any
) -> str | None:
    if not isinstance(relative, str) or not relative.strip():
        return f"{label} evidence path is missing"
    path = (root / relative).resolve()
    if not path.is_relative_to(private.resolve()):
        return f"{label} evidence path is outside operations/private"
    if not path.is_file() or path.stat().st_size == 0:
        return f"{label} evidence file is missing or empty"
    return None


def _live_evidence_failures(
    *, root: Path, private: Path, policy: dict[str, Any], now: datetime
) -> list[str]:
    failures: list[str] = []
    gates = policy.get("live_gates", {})
    evidence = policy.get("live_gate_evidence", {})
    if isinstance(gates, dict) and isinstance(evidence, dict):
        for name, passed in gates.items():
            if passed is True:
                failure = _private_evidence_failure(
                    root=root,
                    private=private,
                    label=f"LIVE gate {name}",
                    relative=evidence.get(name),
                )
                if failure:
                    failures.append(failure)
    approval = policy.get("approval", {})
    if isinstance(approval, dict):
        failure = _private_evidence_failure(
            root=root,
            private=private,
            label="LIVE approval",
            relative=approval.get("evidence_path"),
        )
        if failure:
            failures.append(failure)
        approved_at = approval.get("approved_at_jst")
        if approved_at:
            try:
                if _parse_jst(str(approved_at)) > now:
                    failures.append("LIVE approval timestamp is in the future")
            except ValueError:
                # Policy validation reports the malformed timestamp separately.
                pass
    if policy.get("operation_mode") == "LIVE":
        failures.extend(validate_promoted_evidence_bundle(root=root, policy=policy))
    return failures


def check_readiness(
    *,
    root: Path = PROJECT_ROOT,
    environ: Mapping[str, str] | None = None,
    fixture_dir: Path | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    initialization = initialize_or_migrate_workspace(root)
    environment = os.environ if environ is None else environ
    private = root / "operations/private"
    policy = _read_json(private / "operation-policy.json")
    policy_result = policy_status(policy)
    config = _source_config(root, private)
    now = _parse_jst(at) if at else datetime.now(JST)

    edinet = config.get("edinet", {})
    variable = str(edinet.get("api_key_env", ""))
    fixture_ready = fixture_dir is not None and fixture_dir.is_dir()
    edinet_configured = fixture_ready or bool(variable and environment.get(variable))
    automatic_source_blockers = (
        []
        if not edinet.get("enabled") or edinet_configured
        else [f"missing environment credential: {variable or 'edinet'}"]
    )

    targets = active_target_codes(private)
    base_blockers = list(validate_workspace(root))
    if not policy_result["valid"]:
        base_blockers.extend(policy_result["validation_errors"])
    if policy.get("broker_submission") != "HUMAN_ONLY":
        base_blockers.append("broker submission must remain HUMAN_ONLY")
    try:
        if _parse_jst(str(policy.get("effective_at_jst"))) > now:
            base_blockers.append("operation policy is not effective yet")
    except ValueError:
        pass
    price_evidence: dict[str, Any] | None = None
    price_evidence, price_failures = validate_tracked_price_snapshot(
        root=root,
        active_targets=targets,
        cutoff=now,
        config=config,
    )
    base_blockers.extend(price_failures)

    mode = str(policy.get("operation_mode", ""))
    paper_blockers = list(base_blockers)
    if mode != "PAPER":
        paper_blockers.append("operation_mode must be PAPER for PAPER operation")

    limited_live_blockers = list(base_blockers) + automatic_source_blockers
    if mode == "LIMITED_LIVE":
        limited_live_blockers.extend(
            validate_applied_limited_live(root=root, policy=policy, at=now)
        )
    else:
        limited_live_blockers.append(
            "operation_mode has not been explicitly promoted to LIMITED_LIVE"
        )
    if not policy_result["limited_live_orders_allowed"]:
        limited_live_blockers.append(
            "LIMITED_LIVE controls incomplete: "
            + ", ".join(policy_result["limited_live_gate_failures"])
        )

    live_blockers = list(base_blockers) + automatic_source_blockers
    live_blockers.extend(
        _live_evidence_failures(
            root=root, private=private, policy=policy, now=now
        )
    )
    if mode != "LIVE":
        live_blockers.append("operation_mode has not been explicitly promoted to LIVE")
    if not policy_result["live_orders_allowed"]:
        live_blockers.append(
            "LIVE gates incomplete: " + ", ".join(policy_result["live_gate_failures"])
        )
    warnings: list[str] = []
    if not targets:
        warnings.append(
            "active universe is empty; PAPER is limited to GLOBAL NO-ACTION and "
            "an initial full-market candidate review until candidates are activated"
        )
    if automatic_source_blockers and config.get("paper_manual_primary_source_fallback"):
        warnings.append(
            "EDINET API is unavailable; PAPER requires manual EDINET, TDnet, company IR, "
            "official price/corporate-action, JPX notice, and calendar evidence"
        )
    if config.get("price_source", {}).get("provider") == "yahoo_finance_unofficial_tracked_archive":
        warnings.append(
            "tracked Yahoo prices are unofficial secondary data; official target prices and "
            "corporate actions remain mandatory before a decision is finalized"
        )
    else:
        warnings.append(
            "J-Quants prices are official unadjusted data; corporate-action, "
            "liquidity, disclosure, and execution checks remain mandatory"
        )

    paper_blockers = sorted(set(paper_blockers))
    limited_live_blockers = sorted(set(limited_live_blockers))
    live_blockers = sorted(set(live_blockers))
    if mode == "LIVE":
        active_blockers = live_blockers
    elif mode == "LIMITED_LIVE":
        active_blockers = limited_live_blockers
    else:
        active_blockers = paper_blockers
    return {
        "status": (
            "BLOCKED"
            if active_blockers
            else ("READY_WITH_WARNINGS" if warnings else "READY")
        ),
        "ready": not active_blockers,
        "paper_go": not paper_blockers,
        "limited_live_go": (
            not limited_live_blockers
            and policy_result["limited_live_orders_allowed"]
        ),
        "live_go": not live_blockers and policy_result["live_orders_allowed"],
        "operation_mode": mode,
        "active_rule_version": policy.get("active_rule_version"),
        "credentials": {"edinet": edinet_configured},
        "broker_submission": policy.get("broker_submission"),
        "active_target_count": len(targets),
        "price_snapshot": price_evidence,
        "blockers": active_blockers,
        "paper_blockers": paper_blockers,
        "limited_live_blockers": limited_live_blockers,
        "live_blockers": live_blockers,
        "automatic_source_blockers": automatic_source_blockers,
        "warnings": warnings,
        "created": initialization["created"],
        "migrated": initialization["migrated"],
        "next_command": (
            ".venv/bin/python scripts/nightly_operation.py start --at <JST> --cutoff <JST>"
            if not active_blockers
            else "resolve every blocker and rerun this check"
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", nargs="?")
    parser.add_argument("--fixture-dir", type=Path)
    parser.add_argument("--at")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = check_readiness(fixture_dir=args.fixture_dir, at=args.at)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
