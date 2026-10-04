"""Initialize, migrate, and validate the private operation state."""

from __future__ import annotations
from stock_jp.workspace import project_root, record_path

import argparse
import csv
import json
import os
import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = project_root()
JST = ZoneInfo("Asia/Tokyo")
STATE_SCHEMA_VERSION = "2.1"
RETIRED_LIVE_GATES = {
    "minimum_12_month_paper_trade",
    "twenty_day_shadow_run",
}

TEMPLATE_TO_PRIVATE = {
    "daily-run-state-template.json": "state.json",
    'policy.json': 'policy.json',
    "rule-review-log.csv": "rule-review-log.csv",
    "watchlist.csv": "watchlist.csv",
    "portfolio-register.csv": "portfolio-register.csv",
    "run-history-template.csv": "run-history.csv",
    "market-regime-log.csv": "market-regime-log.csv",
    "trade-event-ledger.csv": "trade-event-ledger.csv",
    "recovered-capital-ledger.csv": "recovered-capital-ledger.csv",
    "capital-ledger.csv": "capital-ledger.csv",
    "corporate-actions.csv": "corporate-actions.csv",
    "forecast-history.csv": "forecast-history.csv",
    "share-count-history.csv": "share-count-history.csv",
    "earnings-calendar-history.csv": "earnings-calendar-history.csv",
    "rebuy-restrictions.csv": "rebuy-restrictions.csv",
    "industry-exposure.csv": "industry-exposure.csv",
    "schema-migration-log.csv": "schema-migration-log.csv",
    "source-watermarks.json": "source-watermarks.json",
    'sources.json': 'sources.json',
}

CSV_MIGRATIONS = {
    "portfolio-register.csv": "portfolio-register.csv",
    "run-history.csv": "run-history-template.csv",
    "schema-migration-log.csv": "schema-migration-log.csv",
}

LEDGER_IDS = {
    "trade-event-ledger.csv": "event_id",
    "recovered-capital-ledger.csv": "recovery_id",
    "capital-ledger.csv": "event_id",
    "corporate-actions.csv": "action_id",
    "forecast-history.csv": "source_id",
    "share-count-history.csv": "source_id",
    "earnings-calendar-history.csv": "source_id",
    "rebuy-restrictions.csv": "restriction_id",
}


def _private_root(root: Path) -> Path:
    return root / 'operations'


def _template_root(root: Path) -> Path:
    return root / 'templates'


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        temporary.write(value)
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(path)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_write_text(
        path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    )


def _normalized_state(state: dict[str, Any]) -> dict[str, Any]:
    version = state.get("schema_version")
    if version not in {"1.0", "2.0", STATE_SCHEMA_VERSION}:
        raise ValueError(f"unsupported state schema: {version!r}")
    normalized = dict(state)
    normalized["schema_version"] = STATE_SCHEMA_VERSION
    normalized.setdefault("state_revision", 0)
    if "unreconciled_ticket_ids" not in normalized:
        legacy_pending = normalized.get("pending_orders", [])
        normalized["unreconciled_ticket_ids"] = (
            list(legacy_pending) if isinstance(legacy_pending, list) else []
        )
    normalized.setdefault("last_ledger_reconciliation_at_jst", None)
    normalized.setdefault("consecutive_successful_runs", 0)
    reviews = normalized.get("last_rule_reviews")
    if not isinstance(reviews, dict):
        reviews = {}
    normalized["last_rule_reviews"] = {
        "monthly_operations": reviews.get("monthly_operations"),
        "quarterly_performance": reviews.get("quarterly_performance"),
        "annual_promotion": reviews.get("annual_promotion"),
    }
    for legacy_backup_field in (
        "last_backup_at_jst",
        "last_backup_path",
        "last_backup_sha256",
        "last_backup_verified_before_encryption",
    ):
        normalized.pop(legacy_backup_field, None)
    return normalized


def _normalized_source_config(
    config: dict[str, Any], template: dict[str, Any]
) -> dict[str, Any]:
    version = str(config.get("schema_version", ""))
    if version not in {"1.0", "1.1", "1.2", "1.3", "1.4", "1.5"}:
        raise ValueError(f"unsupported source config schema: {version!r}")
    normalized = {**template, **config}
    configured_price = config.get("price_source", {})
    if not isinstance(configured_price, dict):
        configured_price = {}
    template_price = template.get("price_source", {})
    normalized["price_source"] = {
        key: configured_price.get(key, value) for key, value in template_price.items()
    }
    # Preserve explicit private locations. Legacy public pointers migrate to an
    # empty private location, never to a copy or fallback of public data.
    normalized["price_source"]["provider"] = template_price["provider"]
    if "storage" not in configured_price:
        for key in ("storage", "archive_root", "manifest_path"):
            normalized["price_source"][key] = template_price[key]
    normalized["price_source"]["minimum_daily_archive_coverage"] = max(
        float(template_price["minimum_daily_archive_coverage"]),
        float(
            configured_price.get(
                "minimum_daily_archive_coverage",
                template_price["minimum_daily_archive_coverage"],
            )
        ),
    )
    normalized["price_source"]["minimum_active_target_coverage"] = 1.0
    normalized["price_source"]["maximum_latest_price_age_days"] = min(
        int(template_price["maximum_latest_price_age_days"]),
        int(
            configured_price.get(
                "maximum_latest_price_age_days",
                template_price["maximum_latest_price_age_days"],
            )
        ),
    )
    for section in (
        "edinet",
        "jpx_listed_master",
        "jquants",
        "market_regime",
        "manual_primary_sources",
    ):
        configured_section = config.get(section, {})
        normalized[section] = {
            **template.get(section, {}),
            **(configured_section if isinstance(configured_section, dict) else {}),
        }
    template_market = template.get("market_regime", {})
    configured_market = (
        config.get("market_regime", {})
        if isinstance(config.get("market_regime"), dict)
        else {}
    )
    for field in ("minimum_vi_observations", "breadth_lookback_sessions"):
        normalized["market_regime"][field] = max(
            int(template_market[field]),
            int(configured_market.get(field, template_market[field])),
        )
    normalized["market_regime"]["minimum_breadth_coverage"] = max(
        float(template_market["minimum_breadth_coverage"]),
        float(
            configured_market.get(
                "minimum_breadth_coverage",
                template_market["minimum_breadth_coverage"],
            )
        ),
    )
    normalized["market_regime"]["maximum_breadth_close_age_days"] = min(
        int(template_market["maximum_breadth_close_age_days"]),
        int(
            configured_market.get(
                "maximum_breadth_close_age_days",
                template_market["maximum_breadth_close_age_days"],
            )
        ),
    )
    normalized["initial_lookback_days"] = max(
        int(template.get("initial_lookback_days", 7)),
        int(config.get("initial_lookback_days", 0)),
    )
    normalized["jpx_listed_master"]["url"] = template["jpx_listed_master"]["url"]
    normalized["jpx_listed_master"]["minimum_issue_count"] = max(
        int(template["jpx_listed_master"]["minimum_issue_count"]),
        int(
            normalized["jpx_listed_master"].get(
                "minimum_issue_count",
                template["jpx_listed_master"]["minimum_issue_count"],
            )
        ),
    )
    template_jquants = template["jquants"]
    normalized["jquants"]["base_url"] = "https://api.jquants.com/v2"
    normalized["jquants"]["api_key_env"] = "JQUANTS_API_KEY"
    configured_ingestion = normalized["jquants"].get("ingestion", {})
    normalized["jquants"]["ingestion"] = {
        **template_jquants.get("ingestion", {}),
        **(configured_ingestion if isinstance(configured_ingestion, dict) else {}),
    }
    if version != "1.5":
        # The authorized paid-provider migration must activate old disabled
        # settings. It does not establish the user's plan or add-on entitlement.
        normalized["price_source"]["enabled"] = True
        for field in template_jquants:
            if field == "enabled" or field.endswith("_enabled"):
                normalized["jquants"][field] = True
        normalized["jquants"]["plan"] = None
        normalized["jquants"]["addons"] = []
        normalized["jquants"]["ingestion"]["enabled"] = True
        normalized["jquants"]["ingestion"]["datasets"] = "all"
    normalized["schema_version"] = "1.5"
    return normalized


def _normalized_policy(
    policy: dict[str, Any], template: dict[str, Any]
) -> dict[str, Any]:
    if policy.get("schema_version") not in {"1.0", "1.1", "1.2", "1.3", "1.4"}:
        return policy
    normalized = dict(policy)
    normalized["schema_version"] = "1.4"
    gates = policy.get("live_gates")
    evidence = policy.get("live_gate_evidence")
    normalized["live_gates"] = {
        **template.get("live_gates", {}),
        **(gates if isinstance(gates, dict) else {}),
    }
    normalized["live_gate_evidence"] = {
        **template.get("live_gate_evidence", {}),
        **(evidence if isinstance(evidence, dict) else {}),
    }
    legacy_gate = "backup_restore_drill"
    replacement_gate = "private_repository_recovery"
    if isinstance(gates, dict) and legacy_gate in gates:
        normalized["live_gates"][replacement_gate] = gates[legacy_gate]
    if isinstance(evidence, dict) and legacy_gate in evidence:
        normalized["live_gate_evidence"][replacement_gate] = evidence[legacy_gate]
    normalized["live_gates"].pop(legacy_gate, None)
    normalized["live_gate_evidence"].pop(legacy_gate, None)
    for retired_gate in RETIRED_LIVE_GATES:
        normalized["live_gates"].pop(retired_gate, None)
        normalized["live_gate_evidence"].pop(retired_gate, None)
    normalized.setdefault("v04_holdout_promotion", False)
    configured_limited = policy.get("limited_live")
    normalized["limited_live"] = {
        **template.get("limited_live", {}),
        **(configured_limited if isinstance(configured_limited, dict) else {}),
    }
    return normalized


def _csv_header(path: Path) -> list[str]:
    with path.open(encoding="utf-8", newline="") as source:
        return next(csv.reader(source), [])


def _template_header(path: Path) -> list[str]:
    return _csv_header(path)


def _migrate_csv(path: Path, template_path: Path) -> None:
    target_fields = _template_header(template_path)
    with path.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    if "migration_snapshot_dir" in target_fields:
        for row in rows:
            row.setdefault("migration_snapshot_dir", row.get("backup_dir", ""))
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as temporary:
        writer = csv.DictWriter(
            temporary,
            fieldnames=target_fields,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(path)


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _planned_migrations(root: Path) -> list[tuple[Path, Path | None]]:
    private = _private_root(root)
    templates = _template_root(root)
    planned: list[tuple[Path, Path | None]] = []
    state_path = record_path(private.parent, 'state.json')
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if _normalized_state(state) != state:
            planned.append((state_path, None))
    source_config_path = record_path(private.parent, 'sources.json')
    if source_config_path.is_file():
        source_config = json.loads(source_config_path.read_text(encoding="utf-8"))
        source_template = json.loads(
            (templates / 'sources.json').read_text(encoding="utf-8")
        )
        if _normalized_source_config(source_config, source_template) != source_config:
            planned.append((source_config_path, None))
    policy_path = record_path(private.parent, 'policy.json')
    if policy_path.is_file():
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy_template = json.loads(
            (templates / 'policy.json').read_text(encoding="utf-8")
        )
        if _normalized_policy(policy, policy_template) != policy:
            planned.append((policy_path, None))
    for private_name, template_name in CSV_MIGRATIONS.items():
        path = record_path(private.parent, private_name)
        template = templates / template_name
        if path.is_file() and _csv_header(path) != _template_header(template):
            planned.append((path, template))
    order_template = templates / "order-ticket-template.csv"
    for path in sorted((record_path(private.parent, 'runs')).glob("*/orders.csv")):
        if _csv_header(path) != _template_header(order_template):
            planned.append((path, order_template))
    return planned


def _snapshot_migrations(
    *, root: Path, planned: list[tuple[Path, Path | None]], migrated_at: datetime
) -> Path | None:
    if not planned:
        return None
    private = _private_root(root)
    snapshot = record_path(private.parent, 'migrations') / migrated_at.strftime('%Y%m%dT%H%M%S.%f%z')
    for source, _ in planned:
        relative = source.relative_to(root)
        destination = snapshot / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return snapshot


def secure_private_tree(root: Path = PROJECT_ROOT) -> None:
    private = _private_root(root)
    if not private.exists():
        return
    for directory, child_directories, filenames in os.walk(private):
        directory_path = Path(directory)
        if not directory_path.is_symlink():
            if directory_path.stat().st_mode & 0o7777 != 0o700:
                directory_path.chmod(0o700)
        child_directories[:] = [
            name
            for name in child_directories
            if not (directory_path / name).is_symlink()
        ]
        for name in child_directories:
            child = directory_path / name
            if child.stat().st_mode & 0o7777 != 0o700:
                child.chmod(0o700)
        for name in filenames:
            path = directory_path / name
            if not path.is_symlink():
                if path.stat().st_mode & 0o7777 != 0o600:
                    path.chmod(0o600)


def initialize_or_migrate_workspace(
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    templates = _template_root(root)
    private = _private_root(root)
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    (record_path(private.parent, 'runs')).mkdir(exist_ok=True, mode=0o700)
    (record_path(private.parent, 'decisions')).mkdir(exist_ok=True, mode=0o700)
    (record_path(private.parent, 'inputs') / 'market-regime').mkdir(
        parents=True, exist_ok=True, mode=0o700
    )
    (record_path(private.parent, 'inputs') / 'investment-cases').mkdir(
        parents=True, exist_ok=True, mode=0o700
    )

    planned = _planned_migrations(root)
    migrated_at = datetime.now(JST)
    migration_snapshot = _snapshot_migrations(
        root=root, planned=planned, migrated_at=migrated_at
    )

    created: list[str] = []
    existing: list[str] = []
    for template_name, private_name in TEMPLATE_TO_PRIVATE.items():
        destination = record_path(private.parent, private_name)
        if destination.exists():
            existing.append(_relative(destination, root))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(templates / template_name, destination)
        destination.chmod(0o600)
        created.append(_relative(destination, root))

    migrated: list[str] = []
    schema_changes: list[tuple[str, str, str]] = []
    for path, template in planned:
        if path.name == "state.json":
            state = json.loads(path.read_text(encoding="utf-8"))
            source_schema = str(state.get("schema_version"))
            _atomic_write_json(path, _normalized_state(state))
            schema_changes.append(("state", source_schema, STATE_SCHEMA_VERSION))
        elif path.name == 'sources.json':
            config = json.loads(path.read_text(encoding="utf-8"))
            source_schema = str(config.get("schema_version"))
            template = json.loads(
                (templates / 'sources.json').read_text(encoding="utf-8")
            )
            normalized = _normalized_source_config(config, template)
            _atomic_write_json(path, normalized)
            schema_changes.append(
                ("source-config", source_schema, str(normalized["schema_version"]))
            )
        elif path.name == 'policy.json':
            policy = json.loads(path.read_text(encoding="utf-8"))
            source_schema = str(policy.get("schema_version"))
            policy_template = json.loads(
                (templates / 'policy.json').read_text(encoding="utf-8")
            )
            normalized = _normalized_policy(policy, policy_template)
            _atomic_write_json(path, normalized)
            schema_changes.append(
                ("operation-policy", source_schema, str(normalized["schema_version"]))
            )
        else:
            if template is None:
                raise AssertionError("CSV migration requires a template")
            _migrate_csv(path, template)
        migrated.append(_relative(path, root))

    if migrated:
        if len(schema_changes) == 1:
            _, from_schema, to_schema = schema_changes[0]
        elif schema_changes:
            from_schema = "|".join(
                f"{name}:{version}" for name, version, _ in schema_changes
            )
            to_schema = "|".join(
                f"{name}:{version}" for name, _, version in schema_changes
            )
        else:
            from_schema = STATE_SCHEMA_VERSION
            to_schema = STATE_SCHEMA_VERSION
        migration_log = record_path(private.parent, 'schema-migration-log.csv')
        with migration_log.open("a", encoding="utf-8", newline="") as destination:
            csv.writer(destination, lineterminator="\n").writerow(
                [
                    migrated_at.isoformat(timespec="seconds"),
                    from_schema,
                    to_schema,
                    "|".join(migrated),
                    _relative(migration_snapshot, root) if migration_snapshot else "",
                    "SUCCESS",
                    "automatic non-destructive migration",
                ]
            )

    secure_private_tree(root)
    return {
        "created": created,
        "existing": existing,
        "migrated": migrated,
        "migration_snapshot_dir": (
            _relative(migration_snapshot, root) if migration_snapshot else None
        ),
        "schema_version": STATE_SCHEMA_VERSION,
    }


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def _duplicate_values(rows: list[dict[str, str]], key: str) -> list[str]:
    values = [row.get(key, "").strip() for row in rows if row.get(key, "").strip()]
    return sorted({value for value in values if values.count(value) > 1})


def validate_workspace(root: Path = PROJECT_ROOT) -> list[str]:
    private = _private_root(root)
    templates = _template_root(root)
    errors: list[str] = []
    state_path = record_path(private.parent, 'state.json')
    if not state_path.is_file():
        return ['missing operations/state.json']
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        errors.append(f"state schema must be {STATE_SCHEMA_VERSION}")
    if not isinstance(state.get("unreconciled_ticket_ids"), list):
        errors.append("unreconciled_ticket_ids must be a list")

    for template_name, private_name in TEMPLATE_TO_PRIVATE.items():
        path = record_path(private.parent, private_name)
        if not path.is_file():
            errors.append(f'missing operations/{private_name}')
            continue
        if path.suffix == ".csv" and _csv_header(path) != _template_header(
            templates / template_name
        ):
            errors.append(f'schema mismatch: operations/{private_name}')

    portfolio_path = record_path(private.parent, 'portfolio-register.csv')
    if portfolio_path.is_file():
        positions = _read_csv(portfolio_path)
        for code in _duplicate_values(positions, "code"):
            errors.append(f"duplicate portfolio code: {code}")
        for row in positions:
            code = row.get("code", "<blank>")
            for field in ("five_x_taken", "ten_x_taken"):
                if row.get(field, "").strip().lower() not in {
                    "",
                    "true",
                    "false",
                    "0",
                    "1",
                }:
                    errors.append(f"{code}: {field} must be boolean")
            streak = row.get("sb_consecutive_quarters", "").strip()
            if streak:
                try:
                    if int(streak) < 0:
                        raise ValueError
                except ValueError:
                    errors.append(f"{code}: sb_consecutive_quarters must be >= 0")
            factor = row.get("corporate_action_factor", "").strip()
            if factor:
                try:
                    if float(factor) <= 0:
                        raise ValueError
                except ValueError:
                    errors.append(f"{code}: corporate_action_factor must be > 0")

    for filename, id_field in LEDGER_IDS.items():
        path = record_path(private.parent, filename)
        if not path.is_file():
            continue
        for value in _duplicate_values(_read_csv(path), id_field):
            errors.append(f"duplicate {filename} {id_field}: {value}")

    restrictions = record_path(private.parent, 'rebuy-restrictions.csv')
    if restrictions.is_file():
        for row in _read_csv(restrictions):
            value = row.get("no_rebuy_until", "").strip()
            if value:
                try:
                    date.fromisoformat(value)
                except ValueError:
                    errors.append(
                        f"{row.get('restriction_id', '<blank>')}: invalid no_rebuy_until"
                    )
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "validate", "status"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "init":
        result = initialize_or_migrate_workspace()
    elif args.command == "validate":
        errors = validate_workspace()
        result = {"valid": not errors, "errors": errors}
    else:
        state = json.loads(
            (record_path(PROJECT_ROOT, 'state.json')).read_text(encoding="utf-8")
        )
        result = {
            "schema_version": state.get("schema_version"),
            "state_revision": state.get("state_revision"),
            "unreconciled_ticket_count": len(state.get("unreconciled_ticket_ids", [])),
            "validation_errors": validate_workspace(),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
