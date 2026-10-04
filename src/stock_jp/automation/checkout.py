"""Validate tracked state/policy without requiring or claiming real-data readiness."""
from __future__ import annotations

from datetime import datetime
import json

from stock_jp.automation.health import command_json, JST


def validate_checkout() -> dict:
    at = datetime.now(JST).isoformat()
    state = command_json("stock_jp.operation.state", at, "validate", include_market_data=False)
    policy = command_json("stock_jp.operation.policy", at, "validate", include_market_data=False)
    errors = []
    if state.get("valid") is not True:
        errors.append("tracked private state/schema validation failed")
    if policy.get("valid") is not True:
        errors.append("tracked operation policy validation failed")
    if policy.get("operation_mode") != "PAPER":
        errors.append("checkout policy must remain PAPER")
    if policy.get("broker_submission") != "HUMAN_ONLY":
        errors.append("broker submission boundary must remain HUMAN_ONLY")
    if policy.get("live_orders_allowed") is not False or policy.get("limited_live_orders_allowed") is not False:
        errors.append("checkout must not permit live orders")
    return {
        "validation_scope": "TRACKED_STATE_AND_POLICY_ONLY",
        "valid": not errors,
        "errors": errors,
        "runtime_readiness": "NOT_EVALUATED",
        "paper_go": None,
        "operation_mode": policy.get("operation_mode"),
        "broker_submission": policy.get("broker_submission"),
    }


def main() -> int:
    try:
        report = validate_checkout()
    except Exception as error:
        report = {"valid": False, "validation_scope": "TRACKED_STATE_AND_POLICY_ONLY",
                  "runtime_readiness": "NOT_EVALUATED", "paper_go": None,
                  "errors": [f"checkout validation failed: {type(error).__name__}: {error}"]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
