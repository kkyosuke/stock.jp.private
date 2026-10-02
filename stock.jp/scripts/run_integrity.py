"""Run leases, coverage manifests, and completion integrity checks."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import urlparse
import uuid
from zoneinfo import ZoneInfo

try:
    from scripts.operation_policy import policy_status
except ModuleNotFoundError:  # Direct execution from scripts/
    from operation_policy import policy_status

try:
    from scripts.nightly_artifacts import validate_nightly_artifacts
except ModuleNotFoundError:  # Direct execution from scripts/
    from nightly_artifacts import validate_nightly_artifacts


JST = ZoneInfo("Asia/Tokyo")
LEASE_HOURS = 6
SOURCE_FIELDS = [
    "source_id",
    "category",
    "code",
    "title",
    "published_at_jst",
    "retrieved_at_jst",
    "url",
    "primary_source",
    "used_for_decision",
    "notes",
]
OFFICIAL_SOURCE_KEYS = ("tdnet", "edinet", "company_ir", "jpx")
SOURCE_ALIASES = {
    "tdnet": "tdnet",
    "edinet": "edinet",
    "company_ir": "company_ir",
    "ir": "company_ir",
    "jpx": "jpx",
    "jpx_notice": "jpx",
}
VALID_ACTIONS = {
    "BUY",
    "WATCH",
    "WAIT",
    "KEEP",
    "ADD",
    "REDUCE",
    "SELL",
    "NO-ACTION",
}
TRADE_ACTIONS = {"BUY", "ADD", "REDUCE", "SELL"}
OPEN_TICKET_STATUSES = {"PAPER_PROPOSED", "PROPOSED", "SUBMITTED", "PARTIAL_FILL"}


class RunIntegrityError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("run integrity failed:\n- " + "\n- ".join(errors))
        self.errors = errors


def _parse_aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("datetime must include a UTC offset")
    return parsed.astimezone(JST)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        json.dump(value, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def _identifier_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    identifiers: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            identifiers.append(value.strip())
        elif isinstance(value, dict):
            identifier = value.get("ticket_id") or value.get("review_id")
            if isinstance(identifier, str) and identifier.strip():
                identifiers.append(identifier.strip())
    return identifiers


def _active_codes(path: Path, *, active_field: str | None = None) -> list[str]:
    if not path.is_file():
        return []
    _, rows = _read_csv(path)
    codes: list[str] = []
    for row in rows:
        code = row.get("code", "").strip()
        if not code:
            continue
        status = row.get("status", "").strip().upper()
        if status in {"CLOSED", "SOLD", "EXITED", "INACTIVE", "REJECTED"}:
            continue
        if active_field:
            active = row.get(active_field, "").strip().lower()
            if active not in {"1", "true", "yes", "active"}:
                continue
        codes.append(code)
    return sorted(set(codes))


def build_coverage_manifest(
    *, root: Path, run_id: str, state: dict[str, Any]
) -> dict[str, Any]:
    private = root / "operations/private"
    template = _read_json(root / "operations/templates/run-coverage-template.json")
    holdings = _active_codes(private / "portfolio-register.csv")
    watchlist = _active_codes(private / "watchlist.csv", active_field="active")
    pending_orders = _identifier_list(state.get("unreconciled_ticket_ids", []))
    due_reviews = _identifier_list(state.get("pending_reviews", []))
    template["run_id"] = run_id
    template["source_window"]["from_exclusive_jst"] = state.get(
        "last_disclosure_cutoff_jst"
    )
    template["universe"]["holdings"]["expected"] = holdings
    template["universe"]["pending_orders"]["expected"] = pending_orders
    template["universe"]["watchlist"]["expected"] = watchlist
    template["universe"]["due_reviews"]["expected"] = due_reviews
    template["official_sources"]["company_ir"]["required"] = bool(
        holdings or watchlist
    )
    return template


def write_coverage_manifest(
    *, root: Path, run_dir: Path, run_id: str, state: dict[str, Any]
) -> Path:
    path = run_dir / "coverage.json"
    _atomic_write_json(
        path, build_coverage_manifest(root=root, run_id=run_id, state=state)
    )
    return path


def acquire_run_lease(
    *, run_dir: Path, run_id: str, at: str, run_token: str | None = None
) -> dict[str, Any]:
    now = _parse_aware(at)
    path = run_dir / "lease.json"
    existed = path.is_file()
    if path.is_file():
        lease = _read_json(path)
        expires = _parse_aware(lease["expires_at_jst"])
        if lease.get("status") == "ACTIVE" and expires > now:
            if run_token and run_token == lease.get("run_token"):
                return {
                    "acquired": True,
                    "resumed": True,
                    "run_token": run_token,
                    "expires_at_jst": lease["expires_at_jst"],
                }
            return {
                "acquired": False,
                "locked": True,
                "expires_at_jst": lease["expires_at_jst"],
            }

    token = uuid.uuid4().hex
    lease = {
        "schema_version": "1.0",
        "run_id": run_id,
        "run_token": token,
        "status": "ACTIVE",
        "acquired_at_jst": now.isoformat(timespec="seconds"),
        "expires_at_jst": (now + timedelta(hours=LEASE_HOURS)).isoformat(
            timespec="seconds"
        ),
        "released_at_jst": None,
    }
    _atomic_write_json(path, lease)
    return {
        "acquired": True,
        "resumed": False,
        "reclaimed": existed,
        "run_token": token,
        "expires_at_jst": lease["expires_at_jst"],
    }


def require_run_lease(*, run_dir: Path, run_token: str, at: str) -> None:
    path = run_dir / "lease.json"
    if not path.is_file():
        raise RunIntegrityError(["missing active run lease"])
    lease = _read_json(path)
    errors: list[str] = []
    if lease.get("status") != "ACTIVE":
        errors.append("run lease is not active")
    if not run_token or lease.get("run_token") != run_token:
        errors.append("run token does not own the active lease")
    if _parse_aware(lease["expires_at_jst"]) <= _parse_aware(at):
        errors.append("run lease expired before close")
    if errors:
        raise RunIntegrityError(errors)


def release_run_lease(*, run_dir: Path, run_token: str, at: str) -> None:
    require_run_lease(run_dir=run_dir, run_token=run_token, at=at)
    path = run_dir / "lease.json"
    lease = _read_json(path)
    lease["status"] = "RELEASED"
    lease["released_at_jst"] = _parse_aware(at).isoformat(timespec="seconds")
    _atomic_write_json(path, lease)


def _validate_report(
    *, report: str, source_cutoff: str, price_date: str
) -> list[str]:
    errors: list[str] = []
    required_fragments = (
        "- 実行状態: `COMPLETED`",
        f"- 今回の開示カットオフ（JST）: {source_cutoff}",
        f"- 株価基準日: {price_date}",
    )
    for fragment in required_fragments:
        if fragment not in report:
            errors.append(f"report missing completed field: {fragment}")
    for marker in (
        "{{RUN_ID}}",
        "{{STARTED_AT_JST}}",
        "{{PREVIOUS_CUTOFF_JST}}",
        "{{OPERATION_MODE}}",
        "{{ACTIVE_RULE_VERSION}}",
        "- 総合結果: 未確定",
    ):
        if marker in report:
            errors.append(f"report contains unresolved marker: {marker}")
    return errors


def _validate_coverage(
    *,
    coverage: dict[str, Any],
    run_id: str,
    previous_cutoff: str | None,
    source_cutoff: str,
    completed_at: str,
) -> list[str]:
    errors: list[str] = []
    if coverage.get("run_id") != run_id:
        errors.append("coverage run_id mismatch")
    if coverage.get("status") != "COMPLETED":
        errors.append("coverage status must be COMPLETED")
    window = coverage.get("source_window", {})
    if window.get("from_exclusive_jst") != previous_cutoff:
        errors.append("coverage lower cutoff differs from prior successful cutoff")
    if window.get("through_inclusive_jst") != source_cutoff:
        errors.append("coverage upper cutoff differs from completion cutoff")
    if coverage.get("completed_at_jst") != completed_at:
        errors.append("coverage completed_at_jst mismatch")
    universe = coverage.get("universe", {})
    for name in ("holdings", "pending_orders", "watchlist", "due_reviews"):
        item = universe.get(name, {})
        expected = item.get("expected")
        checked = item.get("checked")
        if not isinstance(expected, list) or not isinstance(checked, list):
            errors.append(f"coverage {name} expected/checked must be lists")
            continue
        missing = sorted(set(map(str, expected)) - set(map(str, checked)))
        if missing:
            errors.append(f"coverage {name} missing: {', '.join(missing)}")
    sources = coverage.get("official_sources", {})
    for name in OFFICIAL_SOURCE_KEYS:
        source = sources.get(name, {})
        required = source.get("required") is True
        status = source.get("status")
        if required and status != "CHECKED":
            errors.append(f"required source {name} must be CHECKED")
        if not required and status not in {"CHECKED", "NOT_APPLICABLE"}:
            errors.append(f"optional source {name} must be CHECKED or NOT_APPLICABLE")
    return errors


def _validate_sources(
    *,
    fields: list[str],
    rows: list[dict[str, str]],
    coverage: dict[str, Any],
    source_cutoff: str,
    completed_at: str,
) -> list[str]:
    errors: list[str] = []
    if fields != SOURCE_FIELDS:
        errors.append("sources.csv schema mismatch")
        return errors
    ids = [row.get("source_id", "").strip() for row in rows]
    if any(not value for value in ids):
        errors.append("every source row requires source_id")
    duplicates = sorted({value for value in ids if value and ids.count(value) > 1})
    if duplicates:
        errors.append(f"duplicate source_id: {', '.join(duplicates)}")
    cutoff = _parse_aware(source_cutoff)
    completed = _parse_aware(completed_at)
    categories: set[str] = set()
    for row in rows:
        category = row.get("category", "").strip().lower().replace("-", "_")
        categories.add(SOURCE_ALIASES.get(category, category))
        url = row.get("url", "").strip()
        if urlparse(url).scheme not in {"http", "https"}:
            errors.append(f"source {row.get('source_id', '<blank>')} has invalid URL")
        try:
            published = _parse_aware(row.get("published_at_jst", ""))
            retrieved = _parse_aware(row.get("retrieved_at_jst", ""))
            if published > cutoff:
                errors.append(
                    f"source {row.get('source_id', '<blank>')} published after cutoff"
                )
            if retrieved > completed:
                errors.append(
                    f"source {row.get('source_id', '<blank>')} retrieved after completion"
                )
        except (TypeError, ValueError):
            errors.append(
                f"source {row.get('source_id', '<blank>')} has invalid timestamps"
            )
        if row.get("primary_source", "").strip().lower() not in {
            "true",
            "false",
            "1",
            "0",
        }:
            errors.append(
                f"source {row.get('source_id', '<blank>')} primary_source invalid"
            )
        if row.get("used_for_decision", "").strip().lower() not in {
            "true",
            "false",
            "1",
            "0",
        }:
            errors.append(
                f"source {row.get('source_id', '<blank>')} used_for_decision invalid"
            )
    for name, source in coverage.get("official_sources", {}).items():
        if source.get("status") == "CHECKED" and name not in categories:
            errors.append(f"checked source {name} has no sources.csv evidence row")
    target_codes = set(
        map(
            str,
            coverage.get("universe", {}).get("holdings", {}).get("expected", []),
        )
    ) | set(
        map(
            str,
            coverage.get("universe", {}).get("watchlist", {}).get("expected", []),
        )
    )
    if (
        target_codes
        and coverage.get("official_sources", {})
        .get("company_ir", {})
        .get("status")
        == "CHECKED"
    ):
        ir_codes = {
            row.get("code", "").strip()
            for row in rows
            if SOURCE_ALIASES.get(
                row.get("category", "").strip().lower().replace("-", "_"), ""
            )
            == "company_ir"
        }
        missing_ir = sorted(target_codes - ir_codes)
        if missing_ir:
            errors.append(
                f"company_ir evidence missing for codes: {', '.join(missing_ir)}"
            )
    return errors


def _validate_data_gaps(*containers: Any) -> list[str]:
    errors: list[str] = []
    for values in containers:
        if not isinstance(values, list):
            errors.append("data_gaps must be a list")
            continue
        for value in values:
            if isinstance(value, str):
                errors.append(f"unstructured data gap blocks completion: {value}")
                continue
            if not isinstance(value, dict):
                errors.append("data gap must be an object")
                continue
            if str(value.get("status", "OPEN")).upper() == "RESOLVED":
                if not value.get("resolved_at_jst") or not value.get(
                    "resolution_evidence"
                ):
                    errors.append(
                        "resolved data gap requires resolved_at_jst and resolution_evidence"
                    )
                continue
            severity = str(value.get("severity", "")).upper()
            if severity in {"CRITICAL", "BLOCKING"}:
                errors.append(
                    f"blocking data gap: {value.get('source', '<unknown source>')}"
                )
            elif severity not in {"LOW", "NON_CRITICAL"}:
                errors.append("data gap severity must be LOW, NON_CRITICAL, or CRITICAL")
            if not value.get("impact") or not value.get("retry_after_jst"):
                errors.append("non-blocking data gap requires impact and retry_after_jst")
    return errors


def _validate_research_artifacts(
    *,
    health: dict[str, Any],
    queue: dict[str, Any],
    run_id: str,
    handoff: dict[str, Any],
    valid_source_ids: set[str],
) -> list[str]:
    errors: list[str] = []
    if health.get("run_id") != run_id:
        errors.append("provider-health run_id mismatch")
    if health.get("status") not in {"COMPLETED", "PARTIAL"}:
        errors.append("official source scan has not run")
    if not health.get("started_at_jst") or not health.get("completed_at_jst"):
        errors.append("provider-health requires start and completion timestamps")
    if queue.get("run_id") != run_id:
        errors.append("research-queue run_id mismatch")
    if not queue.get("generated_at_jst"):
        errors.append("research queue has not been generated")
    pending_reviews = set(_identifier_list(handoff.get("pending_reviews", [])))
    tasks = queue.get("tasks")
    if not isinstance(tasks, list):
        return errors + ["research queue tasks must be a list"]
    task_ids: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            errors.append("research task must be an object")
            continue
        task_id = str(task.get("task_id", "")).strip()
        task_ids.append(task_id)
        if not task_id:
            errors.append("research task requires task_id")
        status = str(task.get("status", "")).upper()
        if status == "PENDING":
            errors.append(f"research task remains pending: {task_id or '<blank>'}")
        elif status == "DEFERRED":
            if task_id not in pending_reviews:
                errors.append(
                    f"deferred research task missing from handoff: {task_id or '<blank>'}"
                )
        elif status != "COMPLETED":
            errors.append(
                f"research task has invalid terminal status: {task_id or '<blank>'}"
            )
        if status == "COMPLETED" and not task.get("evidence_source_ids"):
            errors.append(
                f"completed research task lacks evidence: {task_id or '<blank>'}"
            )
        if status == "COMPLETED" and isinstance(task.get("evidence_source_ids"), list):
            cited = {
                str(value).strip()
                for value in task["evidence_source_ids"]
                if str(value).strip()
            }
            unknown = sorted(cited - valid_source_ids)
            if unknown:
                errors.append(
                    f"research task {task_id or '<blank>'} cites unknown evidence: {', '.join(unknown)}"
                )
    duplicates = sorted(
        {value for value in task_ids if value and task_ids.count(value) > 1}
    )
    if duplicates:
        errors.append(f"duplicate research task_id: {', '.join(duplicates)}")
    return errors


def _previous_open_orders(
    *, private: Path, current_run_id: str
) -> tuple[set[str], set[str]]:
    open_ids: set[str] = set()
    open_codes: set[str] = set()
    for path in sorted((private / "runs").glob("*/orders.csv")):
        if path.parent.name >= current_run_id:
            continue
        _, rows = _read_csv(path)
        for row in rows:
            if row.get("status", "").strip().upper() in OPEN_TICKET_STATUSES:
                ticket_id = row.get("ticket_id", "").strip()
                code = row.get("code", "").strip()
                if ticket_id:
                    open_ids.add(ticket_id)
                if code:
                    open_codes.add(code)
    return open_ids, open_codes


def _validate_orders(
    *,
    root: Path,
    run_id: str,
    fields: list[str],
    rows: list[dict[str, str]],
    handoff: dict[str, Any],
    policy: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    template_fields, _ = _read_csv(
        root / "operations/templates/order-ticket-template.csv"
    )
    if fields != template_fields:
        return ["orders.csv schema mismatch"]
    status = policy_status(policy)
    if not status["valid"]:
        errors.extend(f"invalid operation policy: {e}" for e in status["validation_errors"])
    if status["ticket_status"] == "BLOCKED" and rows:
        errors.append("operation policy blocks order tickets")
    ids: list[str] = []
    keys: list[tuple[str, str, str]] = []
    private = root / "operations/private"
    previous_ids, previous_codes = _previous_open_orders(
        private=private, current_run_id=run_id
    )
    for row in rows:
        ticket_id = row.get("ticket_id", "").strip()
        code = row.get("code", "").strip()
        side = row.get("side", "").strip().upper()
        trade_date = row.get("trade_date", "").strip()
        ids.append(ticket_id)
        keys.append((code, side, trade_date))
        if not ticket_id or not row.get("decision_id", "").strip() or not code:
            errors.append("each order requires ticket_id, decision_id, and code")
        if not row.get("rule_ids", "").strip():
            errors.append(f"order {ticket_id or '<blank>'} requires rule_ids")
        try:
            date_value = datetime.fromisoformat(trade_date).date()
            if date_value.isoformat() != trade_date:
                raise ValueError
        except ValueError:
            errors.append(f"order {ticket_id or '<blank>'} has invalid trade_date")
        prepared_at: datetime | None = None
        try:
            prepared_at = _parse_aware(row.get("prepared_at_jst", ""))
        except (TypeError, ValueError):
            errors.append(f"order {ticket_id or '<blank>'} has invalid prepared_at_jst")
        try:
            valid_until = _parse_aware(row.get("valid_until_jst", ""))
            if valid_until.date().isoformat() != trade_date:
                errors.append(
                    f"order {ticket_id or '<blank>'} valid_until_jst must be on trade_date"
                )
            if prepared_at is not None and valid_until <= prepared_at:
                errors.append(
                    f"order {ticket_id or '<blank>'} valid_until_jst must be after preparation"
                )
        except (TypeError, ValueError):
            errors.append(f"order {ticket_id or '<blank>'} has invalid valid_until_jst")
        if row.get("order_type", "").strip().upper() not in {
            "LIMIT",
            "当日限り指値",
            "分割指値",
        }:
            errors.append(f"order {ticket_id or '<blank>'} must use a limit order")
        numeric_values: dict[str, float] = {}
        for numeric_field in (
            "limit_price",
            "quantity_private",
            "position_pct",
            "participation_cap_pct",
        ):
            try:
                numeric = float(row.get(numeric_field, ""))
                if not math.isfinite(numeric) or numeric <= 0:
                    raise ValueError
                numeric_values[numeric_field] = numeric
            except ValueError:
                errors.append(
                    f"order {ticket_id or '<blank>'} {numeric_field} must be > 0"
                )
        quantity = numeric_values.get("quantity_private")
        if quantity is not None and not quantity.is_integer():
            errors.append(f"order {ticket_id or '<blank>'} quantity_private must be an integer")
        position_pct = numeric_values.get("position_pct")
        if position_pct is not None and position_pct > 100:
            errors.append(f"order {ticket_id or '<blank>'} position_pct must be <= 100")
        participation = numeric_values.get("participation_cap_pct")
        if participation is not None and participation > 10:
            errors.append(
                f"order {ticket_id or '<blank>'} participation_cap_pct must be <= 10"
            )
        if row.get("operation_mode", "").strip() != policy.get("operation_mode"):
            errors.append(f"order {ticket_id or '<blank>'} operation_mode mismatch")
        if row.get("rule_version", "").strip() != policy.get("active_rule_version"):
            errors.append(f"order {ticket_id or '<blank>'} rule_version mismatch")
        if row.get("status", "").strip() != status["ticket_status"]:
            errors.append(f"order {ticket_id or '<blank>'} has disallowed status")
        action = row.get("action", "").strip().upper()
        if action not in TRADE_ACTIONS:
            errors.append(f"order {ticket_id or '<blank>'} has invalid action")
        if side not in {"BUY", "SELL"}:
            errors.append(f"order {ticket_id or '<blank>'} side must be BUY or SELL")
        expected_side = "BUY" if action in {"BUY", "ADD"} else "SELL"
        if action in TRADE_ACTIONS and side != expected_side:
            errors.append(
                f"order {ticket_id or '<blank>'} {action} requires side {expected_side}"
            )
        for field in (
            "submitted_at_jst",
            "broker_order_id_private",
            "filled_quantity_private",
            "average_fill_price",
            "fees",
            "tax",
        ):
            if row.get(field, "").strip():
                errors.append(
                    f"proposed order {ticket_id or '<blank>'} must not contain {field}"
                )
        if ticket_id in previous_ids:
            errors.append(f"order ticket already exists in an earlier run: {ticket_id}")
        if code in previous_codes:
            errors.append(f"code has an unreconciled earlier order: {code}")
    if policy.get("operation_mode") == "LIMITED_LIVE":
        limited = policy.get("limited_live", {})
        risk_increasing = [
            row
            for row in rows
            if row.get("action", "").strip().upper() in {"BUY", "ADD"}
        ]
        maximum_orders = limited.get("maximum_new_orders_per_run")
        if not isinstance(maximum_orders, int) or len(risk_increasing) > maximum_orders:
            errors.append("LIMITED_LIVE allows only one new order per run")
        if limited.get("additional_purchases_enabled") is not True and any(
            row.get("action", "").strip().upper() == "ADD" for row in rows
        ):
            errors.append("LIMITED_LIVE additional purchases are disabled")
        capital = limited.get("capital_limit_jpy")
        if isinstance(capital, (int, float)) and not isinstance(capital, bool):
            for row in risk_increasing:
                try:
                    notional = float(row["limit_price"]) * float(
                        row["quantity_private"]
                    )
                    declared = float(capital) * float(row["position_pct"]) / 100
                except (KeyError, TypeError, ValueError):
                    continue
                if notional > declared + 1:
                    errors.append(
                        "LIMITED_LIVE order notional exceeds its declared position percentage"
                    )
    duplicate_ids = sorted({value for value in ids if value and ids.count(value) > 1})
    if duplicate_ids:
        errors.append(f"duplicate ticket_id: {', '.join(duplicate_ids)}")
    duplicate_keys = sorted({value for value in keys if keys.count(value) > 1})
    if duplicate_keys:
        errors.append("duplicate code/side/trade_date order")
    pending = set(_identifier_list(handoff.get("pending_orders", [])))
    missing_pending = sorted(set(filter(None, ids)) - pending)
    if missing_pending:
        errors.append(
            f"order tickets missing from handoff pending_orders: {', '.join(missing_pending)}"
        )
    return errors


def validate_run_artifacts(
    *,
    root: Path,
    run_id: str,
    completed_at: str,
    source_cutoff: str,
    price_date: str,
) -> dict[str, Any]:
    private = root / "operations/private"
    run_dir = private / "runs" / run_id
    required = (
        "report.md",
        "orders.csv",
        "sources.csv",
        "pretrade-check.md",
        "handoff.json",
        "coverage.json",
        "lease.json",
        "provider-health.json",
        "research-queue.json",
        "work-plan.json",
        "research-results.md",
        "next-day-actions.csv",
    )
    missing = [name for name in required if not (run_dir / name).is_file()]
    if missing:
        raise RunIntegrityError([f"run is missing {name}" for name in missing])
    completed = _parse_aware(completed_at)
    cutoff = _parse_aware(source_cutoff)
    if cutoff > completed:
        raise RunIntegrityError(["source cutoff cannot be after completion time"])

    handoff = _read_json(run_dir / "handoff.json")
    coverage = _read_json(run_dir / "coverage.json")
    policy = _read_json(private / "operation-policy.json")
    watermarks = _read_json(private / "source-watermarks.json")
    health = _read_json(run_dir / "provider-health.json")
    research_queue = _read_json(run_dir / "research-queue.json")
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    source_fields, source_rows = _read_csv(run_dir / "sources.csv")
    order_fields, order_rows = _read_csv(run_dir / "orders.csv")
    previous_cutoff = handoff.get("previous_disclosure_cutoff_jst")

    errors = _validate_report(
        report=report, source_cutoff=source_cutoff, price_date=price_date
    )
    errors.extend(
        _validate_coverage(
            coverage=coverage,
            run_id=run_id,
            previous_cutoff=previous_cutoff,
            source_cutoff=source_cutoff,
            completed_at=completed_at,
        )
    )
    for name, watermark in watermarks.get("sources", {}).items():
        previous = watermark.get("last_successful_cutoff_jst")
        if previous and cutoff < _parse_aware(previous):
            errors.append(f"watermark for {name} cannot move backwards")
    errors.extend(
        _validate_sources(
            fields=source_fields,
            rows=source_rows,
            coverage=coverage,
            source_cutoff=source_cutoff,
            completed_at=completed_at,
        )
    )
    errors.extend(
        _validate_orders(
            root=root,
            run_id=run_id,
            fields=order_fields,
            rows=order_rows,
            handoff=handoff,
            policy=policy,
        )
    )
    errors.extend(
        _validate_data_gaps(
            handoff.get("data_gaps", []), coverage.get("data_gaps", [])
        )
    )
    errors.extend(
        _validate_research_artifacts(
            health=health,
            queue=research_queue,
            run_id=run_id,
            handoff=handoff,
            valid_source_ids={
                row.get("source_id", "").strip()
                for row in source_rows
                if row.get("source_id", "").strip()
            },
        )
    )
    errors.extend(
        validate_nightly_artifacts(
            root=root,
            run_id=run_id,
            handoff=handoff,
            coverage=coverage,
            orders=order_rows,
        )
    )
    if handoff.get("operation_mode") != policy.get("operation_mode"):
        errors.append("handoff operation_mode differs from current policy")
    if handoff.get("active_rule_version") != policy.get("active_rule_version"):
        errors.append("handoff active_rule_version differs from current policy")
    if errors:
        raise RunIntegrityError(errors)
    return {
        "source_rows": len(source_rows),
        "order_rows": len(order_rows),
        "coverage": coverage,
        "source_rows_data": source_rows,
    }


def advance_source_watermarks(
    *,
    root: Path,
    source_cutoff: str,
    coverage: dict[str, Any],
    source_rows: list[dict[str, str]],
) -> None:
    path = root / "operations/private/source-watermarks.json"
    watermarks = _read_json(path)
    by_category: dict[str, list[dict[str, str]]] = {
        name: [] for name in OFFICIAL_SOURCE_KEYS
    }
    for row in source_rows:
        category = row.get("category", "").strip().lower().replace("-", "_")
        normalized = SOURCE_ALIASES.get(category, category)
        if normalized in by_category:
            by_category[normalized].append(row)
    for name in OFFICIAL_SOURCE_KEYS:
        if coverage.get("official_sources", {}).get(name, {}).get("status") != "CHECKED":
            continue
        current = watermarks["sources"][name]
        previous = current.get("last_successful_cutoff_jst")
        if previous and _parse_aware(source_cutoff) < _parse_aware(previous):
            raise RunIntegrityError([f"watermark for {name} cannot move backwards"])
        current["last_successful_cutoff_jst"] = source_cutoff
        if by_category[name]:
            latest = max(
                by_category[name],
                key=lambda row: (
                    _parse_aware(row["published_at_jst"]), row["source_id"]
                ),
            )
            current["last_published_at_jst"] = latest["published_at_jst"]
            current["last_source_id"] = latest["source_id"]
    _atomic_write_json(path, watermarks)
