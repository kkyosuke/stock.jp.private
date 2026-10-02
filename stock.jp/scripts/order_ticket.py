"""Create an auditable PAPER or approved live-stage order; never submit it."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import tempfile
from typing import Any
from zoneinfo import ZoneInfo

try:
    from scripts.limited_live import validate_limited_live_order
    from scripts.nightly_artifacts import ACTION_FIELDS, TRADE_ACTIONS
    from scripts.operation_policy import policy_status
    from scripts.operation_state import (
        initialize_or_migrate_workspace,
        secure_private_tree,
    )
    from scripts.position_sizing import validate_purchase_increment
    from scripts.run_integrity import OPEN_TICKET_STATUSES, require_run_lease
except ModuleNotFoundError:  # Direct execution from scripts/
    from limited_live import validate_limited_live_order
    from nightly_artifacts import ACTION_FIELDS, TRADE_ACTIONS
    from operation_policy import policy_status
    from operation_state import initialize_or_migrate_workspace, secure_private_tree
    from position_sizing import validate_purchase_increment
    from run_integrity import OPEN_TICKET_STATUSES, require_run_lease


PROJECT_ROOT = Path(__file__).resolve().parents[1]
JST = ZoneInfo("Asia/Tokyo")


def _parse_jst(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("datetime must include a UTC offset")
    return parsed.astimezone(JST)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        json.dump(value, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(path)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        return list(reader.fieldnames or []), list(reader)


def _atomic_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as temporary:
        writer = csv.DictWriter(temporary, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(path)


def _ticket_id(
    *, run_id: str, action_id: str, code: str, side: str, trade_date: str
) -> str:
    digest = hashlib.sha256(
        f"{run_id}|{action_id}|{code}|{side}|{trade_date}".encode("utf-8")
    ).hexdigest()[:10]
    return f"{run_id}-{code}-{side}-{digest}"


def _positive(value: str | float | int, name: str) -> str:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be numeric") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be > 0")
    return str(value)


def _all_open_orders(private: Path) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for path in sorted((private / "runs").glob("*/orders.csv")):
        _, rows = _read_csv(path)
        result.extend(
            row
            for row in rows
            if row.get("status", "").strip().upper() in OPEN_TICKET_STATUSES
        )
    return result


def _require_entry_assessment(run_dir: Path, code: str) -> None:
    path = run_dir / "assessment-status.json"
    if not path.is_file():
        raise ValueError(
            "BUY/ADD requires assessment-status.json from this nightly run"
        )
    assessment = _read_json(path)
    market = assessment.get("market_regime", {})
    if not isinstance(market, dict) or market.get("status") != "CURRENT":
        raise ValueError("BUY/ADD requires a current formal MRS")
    if market.get("state") not in {"NORMAL", "CAUTION"}:
        raise ValueError("BUY/ADD requires MRS NORMAL or CAUTION")
    matches = [
        item
        for item in assessment.get("companies", [])
        if isinstance(item, dict) and str(item.get("code", "")).strip() == code
    ]
    if (
        len(matches) != 1
        or matches[0].get("status") != "PASS"
        or matches[0].get("entry_ready") is not True
    ):
        status = matches[0].get("status") if matches else "MISSING"
        raise ValueError(
            f"BUY/ADD requires a PASS investment assessment; status={status}"
        )


def propose_order(
    *,
    run_id: str,
    run_token: str,
    action_id: str,
    code: str,
    company: str,
    side: str,
    action: str,
    rule_ids: str,
    trade_date: str,
    limit_price: str,
    quantity: str,
    position_pct: str,
    valid_until: str,
    participation_cap_pct: str,
    decision_id: str,
    at: str,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    initialize_or_migrate_workspace(root)
    parsed_run = date.fromisoformat(run_id)
    parsed_trade = date.fromisoformat(trade_date)
    if parsed_run.isoformat() != run_id or parsed_trade.isoformat() != trade_date:
        raise ValueError("run_id and trade_date must be YYYY-MM-DD")
    prepared = _parse_jst(at)
    expires = _parse_jst(valid_until)
    if expires <= prepared:
        raise ValueError("valid_until must be after prepared time")
    if expires.date() != parsed_trade:
        raise ValueError("valid_until must be on trade_date")
    normalized_action = action.strip().upper()
    normalized_side = side.strip().upper()
    if normalized_action not in TRADE_ACTIONS:
        raise ValueError("order action must be BUY, ADD, REDUCE, or SELL")
    expected_side = "BUY" if normalized_action in {"BUY", "ADD"} else "SELL"
    if normalized_side != expected_side:
        raise ValueError(f"{normalized_action} requires side {expected_side}")
    if not rule_ids.strip() or not decision_id.strip() or not code.strip():
        raise ValueError("code, decision_id, and rule_ids are required")
    limit_text = _positive(limit_price, "limit_price")
    quantity_text = _positive(quantity, "quantity")
    position_text = _positive(position_pct, "position_pct")
    participation_text = _positive(participation_cap_pct, "participation_cap_pct")
    if not float(quantity_text).is_integer():
        raise ValueError("quantity must be a whole number of shares")
    if float(position_text) > 100:
        raise ValueError("position_pct must be <= 100")
    if float(participation_text) > 10:
        raise ValueError("participation_cap_pct must be <= 10")

    private = root / "operations/private"
    run_dir = private / "runs" / run_id
    require_run_lease(
        run_dir=run_dir,
        run_token=run_token,
        at=prepared.isoformat(timespec="seconds"),
    )
    policy = _read_json(private / "operation-policy.json")
    status = policy_status(policy)
    if not status["valid"]:
        raise ValueError(
            "invalid operation policy: " + "; ".join(status["validation_errors"])
        )
    if status["ticket_status"] == "BLOCKED":
        failures = (
            status["limited_live_gate_failures"]
            if status["operation_mode"] == "LIMITED_LIVE"
            else status["live_gate_failures"]
        )
        detail = ", ".join(failures) or str(status["operation_mode"])
        raise PermissionError(f"operation policy blocks order tickets: {detail}")
    validate_purchase_increment(
        rule_version=str(policy["active_rule_version"]),
        action=normalized_action,
        position_pct=float(position_text),
    )
    limited_failures = validate_limited_live_order(
        root=root,
        policy=policy,
        run_id=run_id,
        action=normalized_action,
        limit_price=float(limit_text),
        quantity=int(float(quantity_text)),
        position_pct=float(position_text),
        at=prepared,
    )
    if limited_failures:
        raise PermissionError(
            "LIMITED_LIVE order blocked: " + "; ".join(limited_failures)
        )

    decision_path = root / decision_id.strip()
    decisions_root = (private / "decisions").resolve()
    try:
        decision_path.resolve().relative_to(decisions_root)
    except ValueError as error:
        raise ValueError(
            "decision_id must be a path under operations/private/decisions"
        ) from error
    if not decision_path.is_file():
        raise ValueError("decision log must exist before proposing an order")

    coverage = _read_json(run_dir / "coverage.json")
    blocking_gaps = [
        gap
        for gap in coverage.get("data_gaps", [])
        if isinstance(gap, dict)
        and str(gap.get("status", "OPEN")).upper() != "RESOLVED"
        and str(gap.get("severity", "")).upper() in {"CRITICAL", "BLOCKING"}
    ]
    if blocking_gaps:
        raise ValueError(
            "blocking source gaps must be resolved before proposing an order"
        )
    handoff_path = run_dir / "handoff.json"
    handoff = _read_json(handoff_path)
    blocking_handoff_gaps = [
        gap
        for gap in handoff.get("data_gaps", [])
        if isinstance(gap, dict)
        and str(gap.get("status", "OPEN")).upper() != "RESOLVED"
        and str(gap.get("severity", "")).upper() in {"CRITICAL", "BLOCKING"}
    ]
    if blocking_handoff_gaps:
        raise ValueError(
            "blocking handoff gaps must be resolved before proposing an order"
        )
    queue = _read_json(run_dir / "research-queue.json")
    unfinished = [
        str(task.get("task_id", "<blank>"))
        for task in queue.get("tasks", [])
        if isinstance(task, dict) and str(task.get("status", "")).upper() != "COMPLETED"
    ]
    if unfinished:
        raise ValueError(
            "research tasks must be completed before proposing an order: "
            + ", ".join(unfinished)
        )
    if normalized_action in {"BUY", "ADD"}:
        _require_entry_assessment(run_dir, code)

    plan = _read_json(run_dir / "work-plan.json")
    if not plan.get("trading_calendar_confirmed"):
        raise ValueError("trading calendar is not confirmed")
    if plan.get("next_trading_date") != trade_date:
        raise ValueError("trade_date must equal the confirmed next trading date")
    actions_path = run_dir / "next-day-actions.csv"
    action_fields, actions = _read_csv(actions_path)
    if action_fields != ACTION_FIELDS:
        raise ValueError("next-day-actions.csv schema mismatch")
    matches = [row for row in actions if row.get("action_id", "").strip() == action_id]
    if len(matches) != 1:
        raise ValueError("action_id must identify exactly one next-day action")
    selected = matches[0]
    if (
        selected.get("code", "").strip() != code
        or selected.get("next_action", "").strip().upper() != normalized_action
    ):
        raise ValueError("order does not match the selected next-day action")
    if selected.get("trade_date", "").strip() != trade_date:
        raise ValueError("action trade_date does not match")
    if selected.get("rule_ids", "").strip() != rule_ids.strip():
        raise ValueError("order rule_ids do not match the selected action")
    if not selected.get("evidence_source_ids", "").strip():
        raise ValueError("trade action requires primary-source evidence")
    _, source_rows = _read_csv(run_dir / "sources.csv")
    valid_source_ids = {
        row.get("source_id", "").strip()
        for row in source_rows
        if row.get("source_id", "").strip()
        and row.get("primary_source", "").strip().lower() in {"true", "1"}
        and row.get("used_for_decision", "").strip().lower() in {"true", "1"}
    }
    cited_source_ids = {
        value.strip()
        for value in selected.get("evidence_source_ids", "")
        .replace("|", ";")
        .replace(",", ";")
        .split(";")
        if value.strip()
    }
    unknown_source_ids = sorted(cited_source_ids - valid_source_ids)
    if unknown_source_ids:
        raise ValueError(
            "trade action cites unknown source IDs: " + ", ".join(unknown_source_ids)
        )

    ticket_id = _ticket_id(
        run_id=run_id,
        action_id=action_id,
        code=code,
        side=normalized_side,
        trade_date=trade_date,
    )
    orders_path = run_dir / "orders.csv"
    order_fields, orders = _read_csv(orders_path)
    existing_ticket = next(
        (row for row in orders if row.get("ticket_id", "").strip() == ticket_id), None
    )
    if existing_ticket:
        return {
            "run_id": run_id,
            "ticket_id": ticket_id,
            "status": existing_ticket.get("status"),
            "already_proposed": True,
            "broker_submitted": False,
        }
    for row in _all_open_orders(private):
        if row.get("code", "").strip() == code:
            raise ValueError(f"code has an unreconciled order: {code}")

    order = dict.fromkeys(order_fields, "")
    order.update(
        {
            "ticket_id": ticket_id,
            "decision_id": decision_id.strip(),
            "prepared_at_jst": prepared.isoformat(timespec="seconds"),
            "trade_date": trade_date,
            "operation_mode": str(policy["operation_mode"]),
            "rule_version": str(policy["active_rule_version"]),
            "code": code,
            "company": company,
            "side": normalized_side,
            "action": normalized_action,
            "rule_ids": rule_ids.strip(),
            "order_type": "LIMIT",
            "limit_price": limit_text,
            "quantity_private": quantity_text,
            "position_pct": position_text,
            "valid_until_jst": expires.isoformat(timespec="seconds"),
            "participation_cap_pct": participation_text,
            "status": str(status["ticket_status"]),
            "pretrade_check": (
                "PENDING"
                if policy["operation_mode"] in {"LIMITED_LIVE", "LIVE"}
                else "PAPER_ONLY"
            ),
            "notes": "proposed by nightly operation; brokerage submission is human-only",
        }
    )
    orders.append(order)
    _atomic_csv(orders_path, order_fields, orders)

    selected["rule_ids"] = rule_ids.strip()
    selected["limit_price"] = limit_text
    selected["position_pct"] = position_text
    selected["ticket_id"] = ticket_id
    selected["human_action"] = (
        "翌朝8:45-8:55にpretrade確認後、承認時のみ手入力"
        if policy["operation_mode"] in {"LIMITED_LIVE", "LIVE"}
        else "PAPER仮想注文の結果を翌夜に照合"
    )
    selected["decision_log_path"] = decision_id.strip()
    _atomic_csv(actions_path, ACTION_FIELDS, actions)

    pending = handoff.setdefault("pending_orders", [])
    if ticket_id not in pending:
        pending.append(ticket_id)
    _atomic_json(handoff_path, handoff)

    ledger_path = private / "trade-event-ledger.csv"
    ledger_fields, ledger_rows = _read_csv(ledger_path)
    event_id = f"{ticket_id}-{status['ticket_status']}"
    if not any(row.get("event_id") == event_id for row in ledger_rows):
        event = dict.fromkeys(ledger_fields, "")
        event.update(
            {
                "event_id": event_id,
                "occurred_at_jst": prepared.isoformat(timespec="seconds"),
                "trade_date": trade_date,
                "ticket_id": ticket_id,
                "decision_id": decision_id.strip(),
                "code": code,
                "side": normalized_side,
                "event_type": str(status["ticket_status"]),
                "operation_mode": str(policy["operation_mode"]),
                "rule_version": str(policy["active_rule_version"]),
                "order_type": "LIMIT",
                "limit_price": limit_text,
                "quantity_private": quantity_text,
                "evidence_path": f"operations/private/runs/{run_id}/orders.csv",
                "notes": "proposal only; not submitted to a broker",
            }
        )
        ledger_rows.append(event)
        _atomic_csv(ledger_path, ledger_fields, ledger_rows)

    pretrade_path = run_dir / "pretrade-check.md"
    pretrade = pretrade_path.read_text(encoding="utf-8")
    if "- 対象取引日: 未確定" in pretrade:
        pretrade_path.write_text(
            pretrade.replace("- 対象取引日: 未確定", f"- 対象取引日: {trade_date}"),
            encoding="utf-8",
        )
    secure_private_tree(root)
    return {
        "run_id": run_id,
        "ticket_id": ticket_id,
        "status": status["ticket_status"],
        "already_proposed": False,
        "broker_submitted": False,
        "orders": orders_path.relative_to(root).as_posix(),
        "actions": actions_path.relative_to(root).as_posix(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    propose = commands.add_parser("propose")
    for name in (
        "run-id",
        "run-token",
        "action-id",
        "code",
        "company",
        "side",
        "action",
        "rule-ids",
        "trade-date",
        "limit-price",
        "quantity",
        "position-pct",
        "valid-until",
        "participation-cap-pct",
        "decision-id",
        "at",
    ):
        propose.add_argument(f"--{name}", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = propose_order(
        run_id=args.run_id,
        run_token=args.run_token,
        action_id=args.action_id,
        code=args.code,
        company=args.company,
        side=args.side,
        action=args.action,
        rule_ids=args.rule_ids,
        trade_date=args.trade_date,
        limit_price=args.limit_price,
        quantity=args.quantity,
        position_pct=args.position_pct,
        valid_until=args.valid_until,
        participation_cap_pct=args.participation_cap_pct,
        decision_id=args.decision_id,
        at=args.at,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
