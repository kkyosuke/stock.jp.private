"""Validate the private PAPER/LIMITED_LIVE/LIVE operation policy."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = PROJECT_ROOT / "operations/private/operation-policy.json"
POLICY_SCHEMA_VERSION = "1.4"
VALID_MODES = {"PAPER", "LIMITED_LIVE", "LIVE", "PAUSED"}
VALID_SUBMISSION = {"HUMAN_ONLY"}
REQUIRED_LIVE_GATES = {
    "point_in_time_full_universe_validation",
    "historical_replay_2025_2026_accepted",
    "official_source_coverage",
    "private_repository_recovery",
    "personal_risk_and_broker_check",
}
VALID_RULE_VERSIONS = {"v0.2", "v0.3", "v0.4"}


def _is_aware_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_policy(policy: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        errors.append(f"schema_version must be {POLICY_SCHEMA_VERSION}")
    if policy.get("operation_mode") not in VALID_MODES:
        errors.append("operation_mode must be PAPER, LIMITED_LIVE, LIVE, or PAUSED")
    if policy.get("broker_submission") not in VALID_SUBMISSION:
        errors.append("broker_submission must be HUMAN_ONLY")
    active = policy.get("active_rule_version")
    if active not in VALID_RULE_VERSIONS:
        errors.append("active_rule_version must be v0.2, v0.3, or v0.4")
    shadows = policy.get("shadow_rule_versions")
    if not isinstance(shadows, list):
        errors.append("shadow_rule_versions must be a list")
    else:
        invalid_shadows = [value for value in shadows if value not in VALID_RULE_VERSIONS]
        if invalid_shadows:
            errors.append("shadow_rule_versions contains an unsupported version")
        if len(shadows) != len(set(map(str, shadows))):
            errors.append("shadow_rule_versions must not contain duplicates")
        if active in shadows:
            errors.append("active_rule_version cannot also be a shadow version")
    if not _is_aware_timestamp(policy.get("effective_at_jst")):
        errors.append("effective_at_jst must be an aware ISO timestamp")
    gates = policy.get("live_gates")
    if not isinstance(gates, dict):
        errors.append("live_gates must be an object")
        gates = {}
    missing = REQUIRED_LIVE_GATES - set(gates)
    if missing:
        errors.append(f"missing live gates: {', '.join(sorted(missing))}")
    for name in REQUIRED_LIVE_GATES & set(gates):
        if not isinstance(gates[name], bool):
            errors.append(f"live gate {name} must be boolean")
    evidence = policy.get("live_gate_evidence")
    if not isinstance(evidence, dict):
        errors.append("live_gate_evidence must be an object")
        evidence = {}
    missing_evidence = REQUIRED_LIVE_GATES - set(evidence)
    if missing_evidence:
        errors.append(
            f"missing live gate evidence: {', '.join(sorted(missing_evidence))}"
        )
    for name in REQUIRED_LIVE_GATES & set(evidence):
        if evidence[name] is not None and not isinstance(evidence[name], str):
            errors.append(f"live gate evidence {name} must be a path or null")
    if not isinstance(policy.get("v03_holdout_promotion"), bool):
        errors.append("v03_holdout_promotion must be boolean")
    if not isinstance(policy.get("v04_holdout_promotion"), bool):
        errors.append("v04_holdout_promotion must be boolean")
    limited = policy.get("limited_live")
    if not isinstance(limited, dict):
        errors.append("limited_live must be an object")
        limited = {}
    for name in ("capital_limit_jpy", "maximum_total_loss_pct"):
        value = limited.get(name)
        if value is not None and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
        ):
            errors.append(f"limited_live.{name} must be > 0 or null")
    loss_stop = limited.get("maximum_total_loss_pct")
    if isinstance(loss_stop, (int, float)) and not isinstance(loss_stop, bool):
        if loss_stop > 100:
            errors.append("limited_live.maximum_total_loss_pct must be <= 100")
    maximum_orders = limited.get("maximum_new_orders_per_run")
    if (
        not isinstance(maximum_orders, int)
        or isinstance(maximum_orders, bool)
        or maximum_orders < 1
    ):
        errors.append("limited_live.maximum_new_orders_per_run must be a positive integer")
    if not isinstance(limited.get("additional_purchases_enabled"), bool):
        errors.append("limited_live.additional_purchases_enabled must be boolean")
    for name in ("approved_by", "approved_at_jst", "evidence_path", "evidence_sha256"):
        if limited.get(name) is not None and not isinstance(limited.get(name), str):
            errors.append(f"limited_live.{name} must be a string or null")
    limited_approved_at = limited.get("approved_at_jst")
    if limited_approved_at is not None and not _is_aware_timestamp(limited_approved_at):
        errors.append("limited_live.approved_at_jst must be an aware ISO timestamp or null")
    evidence_sha256 = limited.get("evidence_sha256")
    if evidence_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", evidence_sha256
    ):
        errors.append("limited_live.evidence_sha256 must be a lowercase SHA-256 or null")
    approval = policy.get("approval")
    if not isinstance(approval, dict):
        errors.append("approval must be an object")
    else:
        for name in ("approved_by", "approved_at_jst", "evidence_path"):
            if approval.get(name) is not None and not isinstance(approval.get(name), str):
                errors.append(f"approval.{name} must be a string or null")
        approved_at = approval.get("approved_at_jst")
        if approved_at is not None and not _is_aware_timestamp(approved_at):
            errors.append("approval.approved_at_jst must be an aware ISO timestamp or null")
    return errors


def live_gate_failures(policy: dict[str, Any]) -> list[str]:
    failures = [
        name
        for name in sorted(REQUIRED_LIVE_GATES)
        if policy.get("live_gates", {}).get(name) is not True
    ]
    failures.extend(
        f"live_gate_evidence.{name}"
        for name in sorted(REQUIRED_LIVE_GATES)
        if policy.get("live_gates", {}).get(name) is True
        and not policy.get("live_gate_evidence", {}).get(name)
    )
    if policy.get("active_rule_version") == "v0.3" and not policy.get(
        "v03_holdout_promotion"
    ):
        failures.append("v03_holdout_promotion")
    if policy.get("active_rule_version") == "v0.4" and not policy.get(
        "v04_holdout_promotion"
    ):
        failures.append("v04_holdout_promotion")
    approval = policy.get("approval", {})
    for name in ("approved_by", "approved_at_jst", "evidence_path"):
        if not approval.get(name):
            failures.append(f"approval.{name}")
    return failures


def limited_live_failures(policy: dict[str, Any]) -> list[str]:
    """Return configuration blockers for the deliberately restricted live stage."""

    limited = policy.get("limited_live", {})
    failures: list[str] = []
    if policy.get("active_rule_version") != "v0.4":
        failures.append("active_rule_version must be v0.4 for LIMITED_LIVE")
    for name in (
        "capital_limit_jpy",
        "maximum_total_loss_pct",
        "approved_by",
        "approved_at_jst",
        "evidence_path",
        "evidence_sha256",
    ):
        if not limited.get(name):
            failures.append(f"limited_live.{name}")
    if limited.get("maximum_new_orders_per_run") != 1:
        failures.append("limited_live.maximum_new_orders_per_run must remain 1")
    if limited.get("additional_purchases_enabled") is not False:
        failures.append("limited_live.additional_purchases_enabled must remain false")
    return failures


def policy_status(policy: dict[str, Any]) -> dict[str, Any]:
    errors = validate_policy(policy)
    failures = live_gate_failures(policy) if not errors else []
    limited_failures = limited_live_failures(policy) if not errors else []
    mode = policy.get("operation_mode")
    live_orders_allowed = mode == "LIVE" and not errors and not failures
    limited_live_orders_allowed = (
        mode == "LIMITED_LIVE" and not errors and not limited_failures
    )
    if errors:
        ticket_status = "BLOCKED"
    elif mode == "PAPER":
        ticket_status = "PAPER_PROPOSED"
    elif limited_live_orders_allowed or live_orders_allowed:
        ticket_status = "PROPOSED"
    else:
        ticket_status = "BLOCKED"
    return {
        "valid": not errors,
        "operation_mode": mode,
        "active_rule_version": policy.get("active_rule_version"),
        "broker_submission": policy.get("broker_submission"),
        "limited_live_orders_allowed": limited_live_orders_allowed,
        "live_orders_allowed": live_orders_allowed,
        "ticket_status": ticket_status,
        "validation_errors": errors,
        "limited_live_gate_failures": limited_failures,
        "live_gate_failures": failures,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "status"))
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    status = policy_status(load_policy(args.policy))
    print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
    if args.command == "validate" and not status["valid"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
