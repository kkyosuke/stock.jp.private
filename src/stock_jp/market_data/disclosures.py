"""Scan official Japanese-stock sources and build a durable research queue."""

from __future__ import annotations
from stock_jp.workspace import project_root, record_path

import argparse
import csv
from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
import ssl
from pathlib import Path
import tempfile
import time as time_module
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import certifi

from stock_jp.market_data.jpx import collect_non_edinet_sources
from stock_jp.operation.state import initialize_or_migrate_workspace, secure_private_tree
from stock_jp.market_data.prices import validate_tracked_price_snapshot
from stock_jp.operation.integrity import SOURCE_FIELDS, require_run_lease


PROJECT_ROOT = project_root()
JST = ZoneInfo("Asia/Tokyo")
TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class SourceScanError(RuntimeError):
    pass


def _parse_jst(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("datetime must include a UTC offset")
    return parsed.astimezone(JST)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
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


def _source_config(root: Path, private: Path) -> dict[str, Any]:
    template = _read_json(root / 'templates/sources.json')
    configured = _read_json(record_path(private.parent, 'sources.json'))
    merged = {**template, **configured}
    for section in (
        "price_source",
        "edinet",
        "jpx_listed_master",
        "jquants",
        "manual_primary_sources",
    ):
        merged[section] = {
            **template.get(section, {}),
            **configured.get(section, {}),
        }
    return merged


def _normalize_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    if len(code) == 5 and code.endswith("0") and code.isascii() and code.isalnum():
        return code[:4]
    return code


def _normalize_timestamp(date_value: Any, time_value: Any = "") -> str:
    date_text = str(date_value or "").strip().replace("/", "-")
    time_text = str(time_value or "").strip()
    if "T" in date_text or " " in date_text:
        parsed = datetime.fromisoformat(date_text.replace(" ", "T"))
    else:
        if len(date_text) == 8 and date_text.isdigit():
            date_text = f"{date_text[:4]}-{date_text[4:6]}-{date_text[6:]}"
        if not time_text:
            time_text = "00:00:00"
        elif len(time_text) == 4 and time_text.isdigit():
            time_text = f"{time_text[:2]}:{time_text[2:]}:00"
        elif len(time_text) == 5:
            time_text += ":00"
        parsed = datetime.fromisoformat(f"{date_text}T{time_text}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST).isoformat(timespec="seconds")


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return digest


def _date_range(start: date, end: date) -> list[date]:
    if start > end:
        raise ValueError("scan start cannot be after cutoff")
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _payload_status(payload: dict[str, Any]) -> str | None:
    """Return the response status carried inside a JSON body, when present.

    EDINET answers an invalid or expired subscription key with HTTP 200 and a
    body whose StatusCode is 401, so checking the HTTP status alone turns a
    rejected request into a day that simply has no filings.
    """

    status = payload.get("StatusCode")
    if status is None:
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            status = metadata.get("status")
    if status is None:
        return None
    return str(status).strip()


def _request_json(
    *,
    base_url: str,
    path: str,
    params: dict[str, str],
    headers: dict[str, str],
    timeout: int,
    max_attempts: int = 3,
    backoff_seconds: float = 0.5,
) -> dict[str, Any]:
    public_url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    request = Request(
        f"{public_url}?{urlencode(params)}" if params else public_url,
        headers=headers,
    )
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    for attempt in range(max_attempts):
        try:
            with urlopen(request, timeout=timeout, context=TLS_CONTEXT) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except HTTPError as error:
            retryable = error.code == 429 or error.code >= 500
            if not retryable or attempt + 1 == max_attempts:
                raise SourceScanError(f"HTTP {error.code} from {public_url}") from error
        except URLError as error:
            if attempt + 1 == max_attempts:
                raise SourceScanError(
                    f"network error from {public_url}: {error.reason}"
                ) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SourceScanError(f"invalid JSON from {public_url}") from error
        time_module.sleep(backoff_seconds * (2**attempt))
    if not isinstance(payload, dict):
        raise SourceScanError(f"unexpected JSON shape from {public_url}")
    status = _payload_status(payload)
    if status is not None and status != "200":
        message = str(payload.get("message") or "").strip()
        detail = f": {message}" if message else ""
        raise SourceScanError(f"status {status} in body from {public_url}{detail}")
    return payload


def _fixture_payload(fixture_dir: Path, stem: str) -> dict[str, Any]:
    path = fixture_dir / f"{stem}.json"
    if not path.is_file():
        raise SourceScanError(f"fixture missing: {path.name}")
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise SourceScanError(f"fixture must contain an object: {path.name}")
    return payload


def _fixture_rows(fixture_dir: Path, stem: str, data_key: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    payload = _fixture_payload(fixture_dir, stem)
    data = payload.get(data_key, [])
    if not isinstance(data, list):
        raise SourceScanError(f"fixture {stem}.json has invalid {data_key}")
    return [row for row in data if isinstance(row, dict)], [payload]


def _query_evidence(
    *,
    category: str,
    provider: str,
    scan_date: date,
    cutoff: datetime,
    retrieved_at: datetime,
    count: int,
    url: str,
) -> dict[str, str]:
    end_of_date = datetime.combine(scan_date, time(23, 59, 59), tzinfo=JST)
    published = min(cutoff, end_of_date).isoformat(timespec="seconds")
    return {
        "source_id": f"query-{provider}-{scan_date.isoformat()}",
        "category": category,
        "code": "",
        "title": f"{provider} query {scan_date.isoformat()} ({count} records)",
        "published_at_jst": published,
        "retrieved_at_jst": retrieved_at.isoformat(timespec="seconds"),
        "url": url,
        "primary_source": "true",
        "used_for_decision": "true",
        "notes": "query coverage evidence; count is not a disclosure conclusion",
    }


def _source_row(
    *,
    source_id: str,
    category: str,
    code: str,
    title: str,
    published_at: str,
    retrieved_at: str,
    url: str,
    notes: str = "",
) -> dict[str, str]:
    return {
        "source_id": source_id,
        "category": category,
        "code": code,
        "title": title,
        "published_at_jst": published_at,
        "retrieved_at_jst": retrieved_at,
        "url": url,
        "primary_source": "true",
        "used_for_decision": "true",
        "notes": notes,
    }


def _append_sources(path: Path, rows: list[dict[str, str]]) -> int:
    with path.open(encoding="utf-8", newline="") as source:
        existing = list(csv.DictReader(source))
    existing_ids = {row.get("source_id", "") for row in existing}
    new_rows: list[dict[str, str]] = []
    seen_ids = set(existing_ids)
    for row in rows:
        source_id = row["source_id"]
        if source_id in seen_ids:
            continue
        seen_ids.add(source_id)
        new_rows.append(row)
    with path.open("a", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=SOURCE_FIELDS, lineterminator="\n")
        writer.writerows(new_rows)
    return len(new_rows)


def _merge_tasks(path: Path, generated_at: str, tasks: list[dict[str, Any]]) -> None:
    queue = _read_json(path)
    existing = {
        str(task.get("task_id")): task
        for task in queue.get("tasks", [])
        if isinstance(task, dict)
    }
    for task in tasks:
        prior = existing.get(task["task_id"])
        if prior and prior.get("status") in {"COMPLETED", "DEFERRED"}:
            task = {**task, **prior}
        existing[task["task_id"]] = task
    queue["generated_at_jst"] = generated_at
    queue["tasks"] = [existing[key] for key in sorted(existing)]
    _atomic_json(path, queue)


def _task(
    *,
    task_id: str,
    task_type: str,
    priority: str,
    reason: str,
    code: str = "",
    source_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_type": task_type,
        "priority": priority,
        "code": code,
        "reason": reason,
        "source_ids": source_ids or [],
        "status": "PENDING",
        "evidence_source_ids": [],
        "decision_log_path": None,
        "notes": "",
    }


def _gap(
    *, gap_id: str, source: str, impact: str, retry_after: str
) -> dict[str, Any]:
    return {
        "gap_id": gap_id,
        "status": "OPEN",
        "severity": "CRITICAL",
        "source": source,
        "impact": impact,
        "retry_after_jst": retry_after,
        "resolved_at_jst": None,
        "resolution_evidence": None,
    }


def _merge_gaps(container: dict[str, Any], gaps: list[dict[str, Any]],
                *, failed_sources: set[str] | None = None) -> None:
    existing = {
        str(gap.get("gap_id")): gap
        for gap in container.get("data_gaps", [])
        if isinstance(gap, dict) and gap.get("gap_id")
    }
    for gap in gaps:
        prior = existing.get(gap["gap_id"])
        history = list(prior.get("resolution_history", [])) if prior else []
        if prior and str(prior.get("status", "")).upper() == "RESOLVED":
            if gap.get("source") not in (failed_sources or set()):
                continue  # A recurring manual reminder does not invalidate its evidence.
            history.append({key: value for key, value in prior.items() if key != "resolution_history"})
        merged = dict(gap)
        if history:
            merged["resolution_history"] = history
        existing[gap["gap_id"]] = merged
    container["data_gaps"] = [existing[key] for key in sorted(existing)]


def _resolve_gaps(
    container: dict[str, Any], *, sources: set[str], resolved_at: str
) -> None:
    for gap in container.get("data_gaps", []):
        if not isinstance(gap, dict) or gap.get("source") not in sources:
            continue
        if str(gap.get("status", "OPEN")).upper() == "RESOLVED":
            continue
        gap["status"] = "RESOLVED"
        gap["resolved_at_jst"] = resolved_at
        gap["resolution_evidence"] = "official source scan succeeded"


def scan_sources(
    *,
    run_id: str,
    run_token: str,
    cutoff: str,
    at: str,
    fixture_dir: Path | None = None,
    root: Path = PROJECT_ROOT,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    parsed_run_id = date.fromisoformat(run_id)
    if parsed_run_id.isoformat() != run_id:
        raise ValueError("run ID must be YYYY-MM-DD")
    initialize_or_migrate_workspace(root)
    environment = os.environ if environ is None else environ
    private = root / 'operations'
    run_dir = record_path(private.parent, 'runs') / run_id
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run does not exist: {run_id}")
    started = _parse_jst(at)
    cutoff_at = _parse_jst(cutoff)
    if started < cutoff_at:
        raise ValueError("scan time cannot be before cutoff")
    require_run_lease(
        run_dir=run_dir,
        run_token=run_token,
        at=started.isoformat(timespec="seconds"),
    )
    frozen_path = run_dir / "source-scan-cutoff.json"
    frozen = {"cutoff_at_jst": cutoff_at.isoformat(timespec="seconds")}
    if frozen_path.exists():
        if _read_json(frozen_path) != frozen:
            raise ValueError("resume must preserve the original source-scan cutoff")
    else:
        _atomic_json(frozen_path, frozen)
    config = _source_config(root, private)
    coverage_path = run_dir / "coverage.json"
    coverage = _read_json(coverage_path)
    handoff_path = run_dir / "handoff.json"
    handoff = _read_json(handoff_path)
    health_path = run_dir / "provider-health.json"
    health = _read_json(health_path)
    health.update(
        {
            "input_mode": "FIXTURE" if fixture_dir else "LIVE_NETWORK",
            "status": "RUNNING",
            "started_at_jst": started.isoformat(timespec="seconds"),
            "completed_at_jst": None,
            "providers": {},
            "blocking_gaps": [],
            "notes": [],
        }
    )
    _atomic_json(health_path, health)

    previous = handoff.get("previous_disclosure_cutoff_jst")
    if previous:
        scan_start = (_parse_jst(previous) - timedelta(minutes=10)).date()
    elif fixture_dir:
        scan_start = cutoff_at.date()
    else:
        lookback = max(1, int(config.get("initial_lookback_days", 1)))
        scan_start = cutoff_at.date() - timedelta(days=lookback - 1)
    scan_dates = _date_range(scan_start, cutoff_at.date())
    targets = {
        _normalize_code(value)
        for value in coverage.get("universe", {})
        .get("holdings", {})
        .get("expected", [])
    } | {
        _normalize_code(value)
        for value in coverage.get("universe", {})
        .get("watchlist", {})
        .get("expected", [])
    }
    timeout = int(config.get("request_timeout_seconds", 30))
    retrieved = started.isoformat(timespec="seconds")
    raw_dir = run_dir / "raw-sources"
    raw_dir.mkdir(exist_ok=True, mode=0o700)
    source_rows: list[dict[str, str]] = []
    tasks: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    successful_gap_sources: set[str] = set()
    retry_after = (cutoff_at + timedelta(days=1)).replace(
        hour=21, minute=0, second=0
    ).isoformat(timespec="seconds")

    def record_provider(name: str, status: str, requests: int, records: int, error: str = "") -> None:
        health["providers"][name] = {
            "status": status,
            "request_count": requests,
            "record_count": records,
            "error": error,
        }

    price_snapshot, price_failures = validate_tracked_price_snapshot(
        root=root,
        active_targets=targets,
        cutoff=cutoff_at,
        config=config,
    )
    if price_snapshot:
        record_provider(
            "yahoo_tracked_archive",
            "OK",
            0,
            int(price_snapshot["quote_count"]),
        )
        source_rows.append(
            {
                "source_id": (
                    f"yahoo-price-{price_snapshot['price_date']}-"
                    f"{str(price_snapshot['session_sha256'])[:12]}"
                ),
                "category": "market_data",
                "code": "",
                "title": (
                    "Checksum-verified tracked Yahoo daily archive "
                    f"({price_snapshot['target_count']} active targets)"
                ),
                "published_at_jst": (
                    f"{price_snapshot['price_date']}T15:30:00+09:00"
                ),
                "retrieved_at_jst": retrieved,
                "url": (
                    "https://github.com/kkyosuke/stock.jp.private/blob/main/"
                    f"{price_snapshot['session_path']}"
                ),
                "primary_source": "false",
                "used_for_decision": "true",
                "notes": (
                    "unofficial secondary source; every target is present with OK OHLCV; "
                    "official price and corporate-action confirmation remains pending"
                ),
            }
        )
    else:
        error = "; ".join(price_failures)
        record_provider("yahoo_tracked_archive", "ERROR", 0, 0, error)
        gap = _gap(
            gap_id=f"{run_id}-tracked_market_data",
            source="tracked_market_data",
            impact="tracked daily prices are incomplete; every price-based decision is blocked",
            retry_after=retry_after,
        )
        gaps.append(gap)
        health["blocking_gaps"].append(gap["gap_id"])

    edinet_config = config.get("edinet", {})
    edinet_key = environment.get(str(edinet_config.get("api_key_env", "")), "")
    edinet_records = 0
    edinet_requests = 0
    edinet_ok = bool(edinet_config.get("enabled"))
    if not edinet_ok:
        record_provider("edinet", "DISABLED", 0, 0, "provider disabled")
    if edinet_ok and not fixture_dir and not edinet_key:
        edinet_ok = False
        record_provider("edinet", "MISSING_CREDENTIAL", 0, 0, "EDINET_API_KEY is not set")
    if edinet_ok:
        try:
            for scan_date in scan_dates:
                if fixture_dir:
                    rows, pages = _fixture_rows(
                        fixture_dir, f"edinet-{scan_date.isoformat()}", "results"
                    )
                else:
                    payload = _request_json(
                        base_url=str(edinet_config["base_url"]),
                        path="documents.json",
                        params={
                            "date": scan_date.isoformat(),
                            "type": "2",
                            "Subscription-Key": edinet_key,
                        },
                        headers={},
                        timeout=timeout,
                    )
                    rows, pages = (
                        [row for row in payload.get("results", []) if isinstance(row, dict)],
                        [payload],
                    )
                edinet_requests += 1
                edinet_records += len(rows)
                _atomic_json(
                    raw_dir / f"edinet-{scan_date.isoformat()}.json",
                    {"pages": pages},
                )
                source_rows.append(
                    _query_evidence(
                        category="edinet",
                        provider="edinet",
                        scan_date=scan_date,
                        cutoff=cutoff_at,
                        retrieved_at=started,
                        count=len(rows),
                        url=f"{str(edinet_config['base_url']).rstrip('/')}/documents.json?date={scan_date.isoformat()}&type=2",
                    )
                )
                for row in rows:
                    code = _normalize_code(row.get("secCode"))
                    if code not in targets:
                        continue
                    doc_id = str(row.get("docID", ""))
                    published_at = _normalize_timestamp(
                        row.get("submitDateTime") or scan_date.isoformat()
                    )
                    if _parse_jst(published_at) > cutoff_at:
                        continue
                    source_id = f"edinet-{doc_id or _stable_id(json.dumps(row, sort_keys=True))}"
                    source_rows.append(
                        _source_row(
                            source_id=source_id,
                            category="edinet",
                            code=code,
                            title=str(row.get("docDescription") or row.get("filerName") or "EDINET filing"),
                            published_at=published_at,
                            retrieved_at=retrieved,
                            url=f"{str(edinet_config['base_url']).rstrip('/')}/documents/{doc_id}",
                            notes=f"docTypeCode={row.get('docTypeCode', '')}",
                        )
                    )
                    tasks.append(
                        _task(
                            task_id=f"review-{source_id}",
                            task_type="DISCLOSURE_REVIEW",
                            priority="HIGH",
                            code=code,
                            reason="New EDINET filing for an active target",
                            source_ids=[source_id],
                        )
                    )
            record_provider("edinet", "OK", edinet_requests, edinet_records)
            coverage["official_sources"]["edinet"]["status"] = "CHECKED"
            successful_gap_sources.add("edinet")
        except (KeyError, SourceScanError, ValueError) as error:
            edinet_ok = False
            record_provider("edinet", "ERROR", edinet_requests, edinet_records, str(error))
    if not edinet_ok:
        gap = _gap(
            gap_id=f"{run_id}-edinet",
            source="edinet",
            impact="statutory filings could be missing; all actions remain unverified",
            retry_after=retry_after,
        )
        gaps.append(gap)
        health["blocking_gaps"].append(gap["gap_id"])
        coverage["official_sources"]["edinet"]["status"] = "UNAVAILABLE"

    non_edinet = collect_non_edinet_sources(
        run_id=run_id,
        scan_dates=scan_dates,
        targets=targets,
        cutoff_at=cutoff_at,
        started=started,
        config=config,
        run_dir=run_dir,
        private=private,
        fixture_dir=fixture_dir,
        environ=environment,
    )
    health["providers"].update(non_edinet["providers"])
    source_rows.extend(non_edinet["source_rows"])
    tasks.extend(non_edinet["tasks"])
    successful_gap_sources.update(non_edinet["successful_gap_sources"])
    for failure in non_edinet["blocking_failures"]:
        source = str(failure["source"])
        gap = _gap(
            gap_id=f"{run_id}-{source}",
            source=source,
            impact=str(failure["impact"]),
            retry_after=retry_after,
        )
        gaps.append(gap)
        health["blocking_gaps"].append(gap["gap_id"])
        tasks.append(
            _task(
                task_id=f"manual-{source}-{run_id}",
                task_type="MANUAL_PRIMARY_SOURCE_CHECK",
                priority="URGENT",
                reason=str(failure["reason"]),
            )
        )

    # TDnet and the cash-equity calendar are always checked against first-party
    # public pages. Official target prices are required only when a holding or
    # active watchlist target exists; an empty-universe candidate review cannot
    # create an order and therefore has no target price to confirm.
    manual_checks = [
        (
            "tdnet",
            "material timely disclosures could be missing; trade decisions are blocked",
            "Check TDnet disclosures through the cutoff and attach query evidence",
        )
    ]
    if targets and "official_market_data" not in successful_gap_sources:
        manual_checks.append(
            (
                "official_market_data",
                "official price inputs are incomplete; new and additional buys are blocked",
                "Confirm target prices and corporate actions with JPX, company IR, or brokerage evidence",
            )
        )
    if "trading_calendar" not in successful_gap_sources:
        manual_checks.append(
            (
                "trading_calendar",
                "the next trading date is unconfirmed; order tickets are blocked",
                "Confirm the next cash-equity trading date with JPX calendar evidence",
            )
        )
    for source, impact, reason in manual_checks:
        gap = _gap(
            gap_id=f"{run_id}-{source}",
            source=source,
            impact=impact,
            retry_after=retry_after,
        )
        gaps.append(gap)
        health["blocking_gaps"].append(gap["gap_id"])
        tasks.append(
            _task(
                task_id=f"manual-{source}-{run_id}",
                task_type="MANUAL_PRIMARY_SOURCE_CHECK",
                priority="URGENT",
                reason=reason,
            )
        )
    record_provider(
        "first_party_public_checks",
        "PENDING",
        0,
        0,
        "research evidence is required before decisions can be completed",
    )
    coverage["official_sources"]["tdnet"]["status"] = "PENDING"

    for code in sorted(targets):
        tasks.append(
            _task(
                task_id=f"manual-company-ir-{run_id}-{code}",
                task_type="MANUAL_PRIMARY_SOURCE_CHECK",
                priority="HIGH",
                code=code,
                reason="Check company IR through the cutoff and record query evidence",
            )
        )
    tasks.append(
        _task(
            task_id=f"manual-jpx-notices-{run_id}",
            task_type="MANUAL_PRIMARY_SOURCE_CHECK",
            priority="URGENT",
            reason="Check JPX trading halts, supervision/delisting, and market notices",
        )
    )

    added_sources = _append_sources(run_dir / "sources.csv", source_rows)
    _merge_tasks(run_dir / "research-queue.json", retrieved, tasks)
    _resolve_gaps(
        coverage, sources=successful_gap_sources, resolved_at=retrieved
    )
    _resolve_gaps(handoff, sources=successful_gap_sources, resolved_at=retrieved)
    failed_sources = {str(failure["source"]) for failure in non_edinet["blocking_failures"]}
    if price_failures:
        failed_sources.add("tracked_market_data")
    _merge_gaps(coverage, gaps, failed_sources=failed_sources)
    _merge_gaps(handoff, gaps, failed_sources=failed_sources)
    open_blocking_gaps = sorted(
        {
            str(gap.get("gap_id"))
            for gap in coverage.get("data_gaps", [])
            if isinstance(gap, dict)
            and gap.get("gap_id")
            and str(gap.get("status", "OPEN")).upper() != "RESOLVED"
            and str(gap.get("severity", "")).upper() in {"CRITICAL", "BLOCKING"}
        }
    )
    health["blocking_gaps"] = open_blocking_gaps
    _atomic_json(coverage_path, coverage)
    _atomic_json(handoff_path, handoff)
    health["completed_at_jst"] = retrieved
    health["status"] = "PARTIAL" if open_blocking_gaps else "COMPLETED"
    _atomic_json(health_path, health)
    secure_private_tree(root)
    return {
        "run_id": run_id,
        "status": health["status"],
        "scan_dates": [value.isoformat() for value in scan_dates],
        "target_count": len(targets),
        "source_rows_added": added_sources,
        "research_task_count": len(_read_json(run_dir / "research-queue.json")["tasks"]),
        "blocking_gap_count": len(open_blocking_gaps),
        "provider_health": f'operations/runs/{run_id}/provider-health.json',
        "research_queue": f'operations/runs/{run_id}/research-queue.json',
        "reference_data_manifest": (
            f"operations/runs/{run_id}/{non_edinet['manifest_path']}"
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-token", required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--at", required=True)
    parser.add_argument("--fixture-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = scan_sources(
        run_id=args.run_id,
        run_token=args.run_token,
        cutoff=args.cutoff,
        at=args.at,
        fixture_dir=args.fixture_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
