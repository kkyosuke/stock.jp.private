#!/usr/bin/env python3
"""Validate and apply a fail-closed, capital-capped LIMITED_LIVE stage."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import tempfile
from typing import Any
from zoneinfo import ZoneInfo

try:
    from scripts.live_gate_evidence import (
        evaluate_official_coverage,
        evaluate_personal_risk,
        evaluate_repository_recovery,
    )
except ModuleNotFoundError:  # Direct execution from scripts/
    from live_gate_evidence import (
        evaluate_official_coverage,
        evaluate_personal_risk,
        evaluate_repository_recovery,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[1]
JST = ZoneInfo("Asia/Tokyo")
DEFAULT_PLAN = Path("operations/private/evidence/limited-live-plan.json")
DEFAULT_POLICY = Path("operations/private/operation-policy.json")
REQUIRED_ACKNOWLEDGEMENTS = (
    "capital_is_separate_risk_money",
    "capital_limit_is_an_absolute_ceiling",
    "capital_ledger_funding_matches_limit",
    "retrospective_result_is_not_forward_evidence",
    "maximum_drawdown_reviewed",
    "additional_purchases_are_disabled",
    "one_new_order_per_run",
    "human_only_submission",
    "missing_official_source_means_no_action",
    "full_live_still_requires_every_live_gate",
    "returns_are_not_guaranteed",
)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_path(root: Path, path: Path) -> str:
    """Preserve the logical private path when operations/private is a symlink."""

    root = root.resolve()
    resolved = path.resolve()
    private = (root / "operations/private").resolve()
    try:
        return (Path("operations/private") / resolved.relative_to(private)).as_posix()
    except ValueError:
        return resolved.relative_to(root).as_posix()


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(JST)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        json.dump(value, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(path)


def _result(
    *, blockers: list[str], metrics: dict[str, Any], inputs: list[dict[str, str]]
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "gate": "limited_live_pilot",
        "evaluated_at_jst": datetime.now(tz=JST).isoformat(timespec="seconds"),
        "eligible": not blockers,
        "blockers": sorted(set(blockers)),
        "metrics": metrics,
        "inputs": inputs,
    }


def evaluate_limited_live_plan(
    *,
    root: Path = PROJECT_ROOT,
    plan_path: Path | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Recompute every prerequisite for the restricted real-money stage."""

    root = root.resolve()
    private = (root / "operations/private").resolve()
    evidence_root = (private / "evidence").resolve()
    plan_path = (plan_path or (root / DEFAULT_PLAN)).resolve()
    blockers: list[str] = []
    inputs: list[dict[str, str]] = []
    try:
        plan_path.relative_to(evidence_root)
    except ValueError:
        blockers.append("limited live plan must stay under operations/private/evidence")
    if not plan_path.is_file():
        return _result(
            blockers=[*blockers, f"limited live plan is missing: {plan_path}"],
            metrics={},
            inputs=[],
        )
    try:
        plan = _read_object(plan_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return _result(
            blockers=[*blockers, f"limited live plan is invalid: {error}"],
            metrics={},
            inputs=[],
        )
    inputs.append(
        {
            "role": "limited_live_plan",
            "path": _relative_path(root, plan_path),
            "sha256": _sha256(plan_path),
        }
    )
    if plan.get("schema_version") != "1.0":
        blockers.append("limited live plan schema_version must be 1.0")
    if plan.get("status") != "APPROVED":
        blockers.append("limited live plan status must be APPROVED")
    if plan.get("decision") != "START_LIMITED_LIVE":
        blockers.append("limited live plan decision must be START_LIMITED_LIVE")
    if plan.get("rule_version") != "v0.4":
        blockers.append("limited live plan rule_version must be v0.4")

    capital = plan.get("capital_limit_jpy")
    if (
        not isinstance(capital, int)
        or isinstance(capital, bool)
        or capital <= 0
    ):
        blockers.append("limited live capital_limit_jpy must be a positive integer")
        capital = None
    loss_stop = plan.get("maximum_total_loss_pct")
    if (
        not isinstance(loss_stop, (int, float))
        or isinstance(loss_stop, bool)
        or not math.isfinite(loss_stop)
        or not 0 < loss_stop <= 100
    ):
        blockers.append("limited live maximum_total_loss_pct must be > 0 and <= 100")
        loss_stop = None
    if plan.get("maximum_new_orders_per_run") != 1:
        blockers.append("limited live maximum_new_orders_per_run must be 1")
    if plan.get("additional_purchases_enabled") is not False:
        blockers.append("limited live additional purchases must remain disabled")

    capital_ledger_path = private / "capital-ledger.csv"
    capital_rows = _csv_rows(capital_ledger_path)
    funding_failures, opening_cash = _validate_pilot_funding(capital_rows, capital)
    blockers.extend(f"limited live {failure}" for failure in funding_failures)
    if capital_ledger_path.is_file():
        inputs.append(
            {
                "role": "capital_ledger",
                "path": _relative_path(root, capital_ledger_path),
                "sha256": _sha256(capital_ledger_path),
            }
        )

    approved_at = _aware(plan.get("approved_at_jst"))
    reference_time = (at or datetime.now(tz=JST)).astimezone(JST)
    if not isinstance(plan.get("approved_by"), str) or not plan.get(
        "approved_by", ""
    ).strip():
        blockers.append("limited live approved_by is required")
    if approved_at is None:
        blockers.append("limited live approved_at_jst must include a UTC offset")
    elif approved_at > reference_time:
        blockers.append("limited live approval cannot be in the future")

    diagnostic = plan.get("retrospective_diagnostic")
    diagnostic_drawdown: float | None = None
    if not isinstance(diagnostic, dict):
        diagnostic = {}
        blockers.append("limited live retrospective_diagnostic must be an object")
    relative_diagnostic = diagnostic.get("path")
    diagnostic_path: Path | None = None
    if isinstance(relative_diagnostic, str) and relative_diagnostic.strip():
        diagnostic_path = (root / relative_diagnostic).resolve()
        try:
            diagnostic_path.relative_to((root / "data").resolve())
        except ValueError:
            blockers.append("retrospective diagnostic must stay under data")
            diagnostic_path = None
    else:
        blockers.append("retrospective diagnostic path is required")
    if diagnostic_path is not None:
        if not diagnostic_path.is_file():
            blockers.append("retrospective diagnostic is missing")
        elif diagnostic.get("sha256") != _sha256(diagnostic_path):
            blockers.append("retrospective diagnostic hash does not match")
        else:
            try:
                summary = _read_object(diagnostic_path)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                blockers.append(f"retrospective diagnostic is invalid: {error}")
            else:
                inputs.append(
                    {
                        "role": "retrospective_diagnostic",
                        "path": _relative_path(root, diagnostic_path),
                        "sha256": _sha256(diagnostic_path),
                    }
                )
                if summary.get("status") != "ALLOCATION_DIAGNOSTIC_ONLY":
                    blockers.append("retrospective input must remain diagnostic-only")
                if summary.get("forward_paper_gate_satisfied") is not False:
                    blockers.append("retrospective diagnostic cannot claim forward evidence")
                v04 = summary.get("results", {}).get("v0.4", {})
                value = v04.get("maximum_drawdown_pct") if isinstance(v04, dict) else None
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                ):
                    blockers.append("retrospective v0.4 maximum drawdown is missing")
                else:
                    diagnostic_drawdown = float(value)
                    if diagnostic.get("accepted_maximum_drawdown_pct") != value:
                        blockers.append("accepted maximum drawdown does not match diagnostic")

    acknowledgements = plan.get("acknowledgements")
    if not isinstance(acknowledgements, dict):
        acknowledgements = {}
        blockers.append("limited live acknowledgements must be an object")
    for name in REQUIRED_ACKNOWLEDGEMENTS:
        if acknowledgements.get(name) is not True:
            blockers.append(f"limited live acknowledgement {name} must be true")

    official = evaluate_official_coverage(root=root)
    recovery = evaluate_repository_recovery(root=root, at=reference_time)
    personal = evaluate_personal_risk(root=root, at=reference_time)
    for name, result in (
        ("official_source_coverage", official),
        ("private_repository_recovery", recovery),
        ("personal_risk_and_broker_check", personal),
    ):
        if result.get("eligible") is not True:
            blockers.extend(
                f"{name}: {blocker}" for blocker in result.get("blockers", [])
            )
        for item in result.get("inputs", []):
            if isinstance(item, dict):
                inputs.append({**item, "role": f"{name}:{item.get('role', 'input')}"})
    personal_stop = personal.get("metrics", {}).get("risk_limits_pct", {}).get(
        "maximum_total_loss_stop"
    )
    if loss_stop is not None and personal_stop != loss_stop:
        blockers.append(
            "limited live maximum_total_loss_pct must match the personal checklist"
        )

    return _result(
        blockers=blockers,
        metrics={
            "capital_limit_jpy": capital,
            "maximum_total_loss_pct": loss_stop,
            "maximum_new_orders_per_run": plan.get("maximum_new_orders_per_run"),
            "additional_purchases_enabled": plan.get("additional_purchases_enabled"),
            "recorded_opening_cash_jpy": opening_cash,
            "retrospective_maximum_drawdown_pct": diagnostic_drawdown,
            "approved_by": plan.get("approved_by"),
            "approved_at_jst": approved_at.isoformat(timespec="seconds")
            if approved_at
            else None,
        },
        inputs=inputs,
    )


def validate_applied_limited_live(
    *, root: Path = PROJECT_ROOT, policy: dict[str, Any], at: datetime | None = None
) -> list[str]:
    """Validate the immutable plan bound into an active LIMITED_LIVE policy."""

    if policy.get("operation_mode") != "LIMITED_LIVE":
        return []
    root = root.resolve()
    limited = policy.get("limited_live")
    if not isinstance(limited, dict):
        return ["limited_live policy object is missing"]
    relative = limited.get("evidence_path")
    if not isinstance(relative, str) or not relative.strip():
        return ["limited_live evidence_path is missing"]
    plan_path = (root / relative).resolve()
    try:
        plan_path.relative_to((root / "operations/private/evidence").resolve())
    except ValueError:
        return ["limited_live evidence path is outside private evidence"]
    if not plan_path.is_file():
        return ["limited_live evidence file is missing"]
    if limited.get("evidence_sha256") != _sha256(plan_path):
        return ["limited_live evidence hash does not match"]
    result = evaluate_limited_live_plan(root=root, plan_path=plan_path, at=at)
    failures = [f"limited_live: {item}" for item in result.get("blockers", [])]
    plan = _read_object(plan_path)
    for name in (
        "capital_limit_jpy",
        "maximum_total_loss_pct",
        "maximum_new_orders_per_run",
        "additional_purchases_enabled",
        "approved_by",
        "approved_at_jst",
    ):
        if limited.get(name) != plan.get(name):
            failures.append(f"limited_live policy {name} does not match evidence")
    return sorted(set(failures))


def _csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def _validate_pilot_funding(
    rows: list[dict[str, str]], capital: int | float | None
) -> tuple[list[str], float | None]:
    """Bind the pilot to one separately recorded funding event."""

    failures: list[str] = []
    funding_rows = [
        row
        for row in rows
        if row.get("event_type", "").strip().upper() == "LIMITED_LIVE_FUNDING"
    ]
    if len(funding_rows) != 1:
        failures.append(
            "capital ledger must contain exactly one LIMITED_LIVE_FUNDING event"
        )
        return failures, None
    if capital is None:
        return failures, None
    funding = funding_rows[0]
    try:
        amount = float(funding["amount_private"])
        opening_cash = float(funding["running_cash_private"])
    except (KeyError, TypeError, ValueError):
        failures.append("LIMITED_LIVE_FUNDING values are invalid")
        return failures, None
    if not math.isfinite(amount) or not math.isfinite(opening_cash):
        failures.append("LIMITED_LIVE_FUNDING values are invalid")
    elif abs(amount - float(capital)) > 1 or abs(opening_cash - float(capital)) > 1:
        failures.append("LIMITED_LIVE_FUNDING must equal capital_limit_jpy")
    return failures, opening_cash


def validate_limited_live_order(
    *,
    root: Path,
    policy: dict[str, Any],
    run_id: str,
    action: str,
    limit_price: float,
    quantity: int,
    position_pct: float,
    at: datetime | None = None,
) -> list[str]:
    """Enforce the absolute pilot sleeve and stop before a risk-increasing order."""

    if policy.get("operation_mode") != "LIMITED_LIVE":
        return []
    normalized_action = action.strip().upper()
    if normalized_action in {"SELL", "REDUCE"}:
        return []
    failures = validate_applied_limited_live(root=root, policy=policy, at=at)
    limited = policy.get("limited_live", {})
    if normalized_action == "ADD" and limited.get("additional_purchases_enabled") is not True:
        failures.append("LIMITED_LIVE additional purchases are disabled")
    capital = limited.get("capital_limit_jpy")
    if not isinstance(capital, (int, float)) or isinstance(capital, bool) or capital <= 0:
        failures.append("LIMITED_LIVE capital limit is invalid")
        return sorted(set(failures))

    private = root.resolve() / "operations/private"
    notional = limit_price * quantity
    declared_ceiling = float(capital) * position_pct / 100
    if notional > declared_ceiling + 1:
        failures.append(
            "LIMITED_LIVE order notional exceeds its declared position percentage"
        )

    current_run_orders = _csv_rows(private / "runs" / run_id / "orders.csv")
    risk_increasing_count = sum(
        row.get("action", "").strip().upper() in {"BUY", "ADD"}
        for row in current_run_orders
    )
    maximum_orders = limited.get("maximum_new_orders_per_run")
    if isinstance(maximum_orders, int) and risk_increasing_count >= maximum_orders:
        failures.append("LIMITED_LIVE allows only one new order per run")

    open_commitment = 0.0
    for run_orders in sorted((private / "runs").glob("*/orders.csv")):
        for row in _csv_rows(run_orders):
            if row.get("side", "").strip().upper() != "BUY":
                continue
            if row.get("status", "").strip().upper() not in {
                "PROPOSED",
                "SUBMITTED",
                "PARTIAL_FILL",
            }:
                continue
            try:
                open_commitment += float(row["limit_price"]) * float(
                    row["quantity_private"]
                )
            except (KeyError, TypeError, ValueError):
                failures.append("LIMITED_LIVE open order commitment is invalid")

    acquisition_cost = 0.0
    market_value = 0.0
    for row in _csv_rows(private / "portfolio-register.csv"):
        if row.get("status", "").strip().upper() in {
            "CLOSED",
            "SOLD",
            "EXITED",
            "INACTIVE",
            "REJECTED",
        }:
            continue
        if not row.get("code", "").strip():
            continue
        try:
            held_quantity = float(row["quantity_private"])
            acquisition_cost += float(row["average_cost"]) * held_quantity
            market_value += float(row["last_close"]) * held_quantity
        except (KeyError, TypeError, ValueError):
            failures.append(
                f"LIMITED_LIVE holding values are incomplete: {row.get('code', '<blank>')}"
            )
    if acquisition_cost + open_commitment + notional > float(capital) + 1:
        failures.append("LIMITED_LIVE absolute capital limit would be exceeded")

    capital_rows = _csv_rows(private / "capital-ledger.csv")
    funding_failures, _ = _validate_pilot_funding(capital_rows, capital)
    failures.extend(f"LIMITED_LIVE {failure}" for failure in funding_failures)
    if capital_rows:
        try:
            current_cash = float(capital_rows[-1]["running_cash_private"])
            if not math.isfinite(current_cash) or current_cash < 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            failures.append("LIMITED_LIVE current cash balance is invalid")
        else:
            if notional > current_cash + 1:
                failures.append("LIMITED_LIVE order exceeds the recorded cash balance")
            loss_stop = limited.get("maximum_total_loss_pct")
            if isinstance(loss_stop, (int, float)) and not isinstance(loss_stop, bool):
                current_nav = current_cash + market_value
                stop_nav = float(capital) * (1 - float(loss_stop) / 100)
                if current_nav <= stop_nav:
                    failures.append("LIMITED_LIVE total loss stop has been reached")
    return sorted(set(failures))


def apply_limited_live(
    *, root: Path = PROJECT_ROOT, plan_path: Path | None = None
) -> dict[str, Any]:
    """Atomically promote PAPER to LIMITED_LIVE after every prerequisite passes."""

    root = root.resolve()
    plan_path = (plan_path or (root / DEFAULT_PLAN)).resolve()
    result = evaluate_limited_live_plan(root=root, plan_path=plan_path)
    if result.get("eligible") is not True:
        raise ValueError("ineligible LIMITED_LIVE promotion cannot be applied")
    policy_path = root / DEFAULT_POLICY
    policy = _read_object(policy_path)
    if policy.get("operation_mode") != "PAPER":
        raise ValueError("only a PAPER policy can be promoted to LIMITED_LIVE")
    plan = _read_object(plan_path)
    policy["operation_mode"] = "LIMITED_LIVE"
    policy["limited_live"] = {
        name: plan[name]
        for name in (
            "capital_limit_jpy",
            "maximum_total_loss_pct",
            "maximum_new_orders_per_run",
            "additional_purchases_enabled",
            "approved_by",
            "approved_at_jst",
        )
    }
    policy["limited_live"].update(
        {
            "evidence_path": _relative_path(root, plan_path),
            "evidence_sha256": _sha256(plan_path),
        }
    )
    _atomic_json(policy_path, policy)
    return policy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "apply"))
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--plan", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan_path = args.plan
    if plan_path is not None and not plan_path.is_absolute():
        plan_path = args.root / plan_path
    if args.command == "apply":
        policy = apply_limited_live(root=args.root, plan_path=plan_path)
        print(json.dumps(policy, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    result = evaluate_limited_live_plan(root=args.root, plan_path=plan_path)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
