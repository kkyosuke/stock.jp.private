#!/usr/bin/env python3
"""Resumable, private-only full J-Quants V2 history and correction ingestion.

No key arguments/config files: live authentication only reads JQUANTS_API_KEY.
Use --catalog to inspect planned coverage without authentication or network.
"""
from __future__ import annotations
from stock_jp.workspace import module_file, project_root

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import tempfile
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from stock_jp.market_data.jquants.catalog import BASE_URL, BY_NAME, CATALOG, CATALOG_VERSION, PLANS, Dataset, entitlement, period, query_for


class IngestionError(RuntimeError):
    """Sanitized error safe to persist without provider body or credential."""


class ApiError(IngestionError):
    def __init__(self, status: int, kind: str = "api_error"):
        self.status, self.kind = status, kind
        super().__init__(f"J-Quants {kind} (HTTP {status})")


class RequestBudgetExhausted(IngestionError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError(code, "unexpected_redirect")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encoded(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_bytes(path: Path, data: bytes) -> None:
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    path.parent.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, encoded(value))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise IngestionError("invalid local ingestion metadata") from error


@contextmanager
def archive_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with confined(root, ".ingestion.lock").open("a+") as stream:
        os.chmod(stream.name, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise IngestionError("another ingestion is running") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def private_root(root: Path, configured: str) -> Path:
    relative = Path(configured)
    if relative.is_absolute() or ".." in relative.parts:
        raise IngestionError("ingestion archive must be workspace-relative private storage")
    root = root.resolve()
    allowed = root / '.work/market-data'
    if allowed.resolve() != allowed:
        raise IngestionError('market-data storage must not escape through a symlink')
    target = (root / relative).resolve()
    if target == allowed or not target.is_relative_to(allowed):
        raise IngestionError("ingestion archive must remain under private market-data")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    return target


class Client:
    """Fixed-origin API-key client, serial pacing, bounded transient retries."""
    def __init__(self, *, plan: str, timeout: int = 30, max_requests: int | None = None,
                 transport: Callable | None = None, sleep: Callable = time.sleep,
                 monotonic: Callable = time.monotonic, retries: int = 5, download_hosts: tuple[str, ...] = (), max_response_bytes: int | None = None):
        if plan not in PLANS:
            raise IngestionError("verify and configure the purchased plan before collection")
        self._key = os.environ.get("JQUANTS_API_KEY", "") if transport is None else ""
        if transport is None and not self._key:
            raise IngestionError("JQUANTS_API_KEY is not set; collection blocked")
        if max_requests is not None and max_requests < 1:
            raise ValueError("max_requests must be positive")
        self.max_response_bytes = max_response_bytes
        self.transport, self.timeout, self.max_requests = transport, timeout, max_requests
        self.download_hosts = download_hosts
        self.sleep, self.monotonic, self.retries = sleep, monotonic, retries
        self.interval, self.last_request, self.requests = 60 / min(PLANS[plan][2], 60) + 0.05, None, 0
        self.opener = build_opener(NoRedirect())

    def _request(self, endpoint: str, params: dict) -> dict:
        if endpoint not in {item.endpoint for item in CATALOG}:
            raise IngestionError("endpoint is not in the reviewed catalog")
        if self.transport:
            return self.transport(endpoint, dict(params))
        request = Request(BASE_URL + endpoint + ("?" + urlencode(params) if params else ""),
                          headers={"x-api-key": self._key, "Accept": "application/json", "User-Agent": "stock.jp-private-research/1.0"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status == 210:
                    raise ApiError(210, "not_available")
                data = response.read() if self.max_response_bytes is None else response.read(self.max_response_bytes + 1)
                if self.max_response_bytes is not None and len(data) > self.max_response_bytes:
                    raise ApiError(200, "response_too_large")
                return json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except HTTPError as error:
            # Never persist error bodies, URLs containing signed tokens, or headers.
            kind = "access_denied_unclassified" if error.code == 403 else "api_error"
            if error.code in {401, 403}:
                try:
                    message = json.loads(error.read(self.max_response_bytes + 1) if self.max_response_bytes is not None else error.read()).get("message", "")
                    if message == "The incoming api key is invalid or expired." or error.code == 401:
                        kind = "authentication_failed"
                except (ValueError, TypeError, AttributeError):
                    pass
            raise ApiError(error.code, kind) from None
        except (URLError, TimeoutError, OSError):
            raise ApiError(0, "network_error") from None
        except (ValueError, UnicodeDecodeError):
            raise ApiError(200, "invalid_json") from None

    def get(self, endpoint: str, params: dict) -> dict:
        for attempt in range(self.retries + 1):
            if self.max_requests is not None and self.requests >= self.max_requests:
                raise RequestBudgetExhausted("request budget reached; resume this archive")
            if self.last_request is not None:
                self.sleep(max(0, self.interval - (self.monotonic() - self.last_request)))
            self.requests += 1
            self.last_request = self.monotonic()
            try:
                payload = self._request(endpoint, params)
                if not isinstance(payload, dict):
                    raise ApiError(200, "invalid_response_shape")
                return payload
            except ApiError as error:
                if error.status not in {0, 429, 500, 502, 503, 504} or attempt == self.retries:
                    raise
                # Official 429 guidance: >= 1 minute; 2 minutes preferred.
                delay = 120 * (2 ** attempt) if error.status == 429 else min(60, 2 ** attempt)
                self.sleep(delay + random.random())
        raise AssertionError("unreachable")


def confined(root: Path, *parts: str) -> Path:
    candidate = root.joinpath(*parts).resolve()
    if not candidate.is_relative_to(root.resolve()) or candidate == root.resolve():
        raise IngestionError("archive descendant escaped private storage")
    return candidate


def save_object(root: Path, payload: dict) -> dict:
    data = encoded(payload)
    sha = digest(data)
    relative = Path("objects") / sha[:2] / (sha + ".json")
    path = confined(root, relative.as_posix())
    if path.exists():
        if digest(path.read_bytes()) != sha:
            raise IngestionError("existing immutable object checksum mismatch")
    else:
        atomic_bytes(path, data)
    return {"path": relative.as_posix(), "sha256": sha, "bytes": len(data)}


def checked_payload(root: Path, ref: dict) -> dict:
    path = (root / ref["path"]).resolve()
    if not path.is_relative_to(root.resolve()):
        raise IngestionError("page object escaped private archive")
    data = path.read_bytes()
    if digest(data) != ref["sha256"]:
        raise IngestionError("page checksum mismatch")
    return json.loads(data)


def collect_partition(root: Path, dataset: Dataset, day: date, client: Client, *, page_saved=None) -> dict:
    """Commit page then checkpoint. Publish a partition only when pagination ends."""
    folder = confined(root, "datasets", dataset.name, day.isoformat())
    checkpoint_path = confined(root, "datasets", dataset.name, day.isoformat(), "checkpoint.json")
    params = query_for(dataset, day)
    checkpoint = read_json(checkpoint_path)
    if checkpoint is None:
        checkpoint = {"endpoint": dataset.endpoint, "query": params, "started_at": utc_now(), "pages": [], "next_key": None, "seen_keys": [], "complete": False}
    if checkpoint.get("endpoint") != dataset.endpoint or checkpoint.get("query") != params:
        raise IngestionError("checkpoint query does not match catalog")
    # Verify durable pages before resuming, not merely their filenames.
    for page in checkpoint["pages"]:
        checked_payload(root, page)
    while not checkpoint["complete"]:
        query = {**params}
        if checkpoint["next_key"]:
            query["pagination_key"] = checkpoint["next_key"]
        payload = client.get(dataset.endpoint, query)
        rows = payload.get("data")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ApiError(200, "invalid_data_schema")
        next_key = payload.get("pagination_key")
        if next_key is not None and not isinstance(next_key, str):
            raise ApiError(200, "invalid_pagination_key")
        if next_key and next_key in checkpoint["seen_keys"]:
            raise ApiError(200, "pagination_cycle")
        ref = save_object(root, payload)
        ref.update({"row_count": len(rows), "retrieved_at": utc_now(), "page_number": len(checkpoint["pages"]) + 1})
        checkpoint["pages"].append(ref)
        checkpoint["next_key"] = next_key or None
        if next_key:
            checkpoint["seen_keys"].append(next_key)
        checkpoint["complete"] = not bool(next_key)
        atomic_json(checkpoint_path, checkpoint)
        if page_saved is not None:
            page_saved()  # synchronous remote ACK before requesting another page
    partition = {"schema_version": 1, "provider": "J-Quants API V2", "api_version": "v2", "dataset": dataset.name,
                 "endpoint": BASE_URL + dataset.endpoint, "query": params, "period": {"from": day.isoformat(), "to": day.isoformat()},
                 "universe": "all returned issues; no configured watchlist filter", "units": dataset.units,
                 "started_at": checkpoint["started_at"], "completed_at": utc_now(), "pages": checkpoint["pages"],
                 "row_count": sum(page["row_count"] for page in checkpoint["pages"]), "status": "complete"}
    version = digest(encoded(partition))
    atomic_json(confined(root, "datasets", dataset.name, day.isoformat(), "revisions", version + ".json"), partition)
    atomic_json(confined(root, "datasets", dataset.name, day.isoformat(), "latest.json"), partition)
    # An interrupted unlink only causes an idempotent re-publication on resume.
    checkpoint_path.unlink(missing_ok=True)
    return partition


def partition_rows(root: Path, dataset_name: str, day: date) -> list[dict]:
    manifest = read_json(confined(root, "datasets", dataset_name, day.isoformat(), "latest.json"))
    if not manifest or manifest.get("status") != "complete":
        raise IngestionError("required partition is incomplete")
    return [row for page in manifest["pages"] for row in checked_payload(root, page)["data"]]


def planned_status(plan: str, addons: tuple[str, ...], as_of: date) -> dict:
    datasets = {}
    for item in CATALOG:
        start, end = period(item, plan, as_of)
        allowed = entitlement(item, plan, addons)
        datasets[item.name] = {**item.metadata(), "status": "supported" if allowed else "not_entitled",
                              "entitlement_basis": "operator_declared_subscription; API access not yet verified",
                              "collection_status": "pending" if allowed else "skipped_declared_subscription",
                              "requested_period": {"from": start.isoformat(), "to": end.isoformat()}}
        if item.role == "delivery":
            datasets[item.name]["collection_status"] = "delivery_mechanism; not an independent dataset"
    return {"schema_version": 1, "provider": "J-Quants API V2", "api_version": "v2", "catalog_version": CATALOG_VERSION,
            "as_of": as_of.isoformat(), "declared_plan": plan, "declared_addons": list(addons), "datasets": datasets,
            "all_entitled_complete": False, "live_execution_authorized": False}


def days_between(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


_snapshot_exports = {}


def publish_snapshot(root: Path, archive: Path, day: date) -> dict:
    from stock_jp.market_data.jquants.snapshot import export_daily_snapshot
    config = read_json(root / 'config/sources.json', {})
    target = private_root(root, config.get("price_source", {}).get("archive_root", '.work/market-data/daily-prices'))
    if target == archive.resolve() or target.is_relative_to(archive.resolve()) or archive.resolve().is_relative_to(target):
        raise IngestionError("raw and normalized archives must be separate sibling directories")
    inputs={name:confined(archive,'datasets',name,day.isoformat(),'latest.json').read_bytes()
            for name in ('equities_master','equities_daily')}
    provenance={"api_version":"v2","archive_root":config['jquants']['ingestion']['archive_root'],
        "partitions":[{"dataset":name,"path":f"datasets/{name}/{day.isoformat()}/revisions/{digest(raw)}.json",
                       "sha256":digest(raw)} for name,raw in inputs.items()]}
    # Re-exporting an unchanged acquisition must not manufacture new retrieval
    # times/revisions on every history batch. Corrections have a new input hash.
    retrieved=max(datetime.fromisoformat(json.loads(raw)['completed_at'].replace('Z','+00:00')) for raw in inputs.values()).isoformat()
    # A same-process history continuation may reuse an output we actually
    # generated and semantically checked, only after rechecking every input page
    # and output hash. Cold processes recompute once; stable input time keeps
    # their immutable output identical as well.
    module_root=Path(__file__).resolve().parents[4]
    binding=digest(b''.join(module_file(module_root, name).read_bytes() for name in
                           ('stock_jp.market_data.jquants.snapshot','stock_jp.market_data.prices','stock_jp.market_data.legacy_prices'))+encoded(provenance))
    cache_key=(str(root.resolve()),day.isoformat());cached=_snapshot_exports.get(cache_key)
    if cached and cached['binding']==binding:
        for raw in inputs.values():
            for page in json.loads(raw)['pages']:
                content=confined(archive,page['path']).read_bytes()
                if digest(content)!=page['sha256'] or len(content)!=page['bytes']:
                    raise IngestionError('raw page changed during snapshot reuse')
        for name,checksum in cached['files'].items():
            if digest(confined(target,name).read_bytes())!=checksum:
                raise IngestionError('normalized snapshot changed during reuse')
        return dict(cached['summary'])
    manifest = export_daily_snapshot(
        root, day.isoformat(), partition_rows(archive, "equities_master", day),
        partition_rows(archive, "equities_daily", day), retrieved, raw_provenance=provenance)
    summary={"status": "exported", "date": day.isoformat(), "quote_count": manifest["latest_session"]["quote_count"],
            "universe_count": manifest["universe"]["count"], "readiness": "independent_coverage_freshness_and_active_target_validation_required"}
    metadata=(json.dumps(manifest,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode()
    session=manifest['latest_session'];manifest_name=(Path(session['file']).parent/('manifest-'+digest(metadata)+'.json')).as_posix()
    _snapshot_exports[cache_key]={'binding':binding,'summary':summary,'files':{
        session['file']:session['sha256'],session['canonical_file']:session['sha256'],manifest_name:digest(metadata)}}
    while len(_snapshot_exports)>280:_snapshot_exports.pop(next(iter(_snapshot_exports)))
    return summary


def initial_snapshot(root: Path, archive: Path, plan: str, as_of: date, client: Client) -> dict:
    """A bounded recent-date preflight; full history completeness is separate."""
    end = period(BY_NAME["equities_daily"], plan, as_of)[1]
    for offset in range(14):
        day = end - timedelta(days=offset)
        for name in ("equities_daily", "equities_master"):
            dataset = BY_NAME[name]
            state_path = confined(archive, "datasets", name, "state.json")
            state = read_json(state_path, {"last_full_reconciled": None, "completed_days": {}})
            old = state["completed_days"].get(day.isoformat())
            if not old or old.get("checked_as_of") != as_of.isoformat():
                partition = collect_partition(archive, dataset, day, client)
                if state["last_full_reconciled"] is None and not state.get("reconciliation_cycle"):
                    state["reconciliation_cycle"] = as_of.isoformat()
                state["completed_days"][day.isoformat()] = {"checked_as_of": as_of.isoformat(),
                    "cycle": state.get("reconciliation_cycle") or as_of.isoformat(), "row_count": partition["row_count"]}
                atomic_json(state_path, state)
            if name == "equities_daily" and not partition_rows(archive, name, day):
                break
        else:
            return publish_snapshot(root, archive, day)
    return {"status": "not_available", "reason": "no daily bars in latest 14 calendar days; no snapshot promoted"}


def ingest(*, root: Path, archive: Path, plan: str, addons: tuple[str, ...], as_of: date, client: Client,
           overlap_days: int = 30, reconcile_every_days: int = 30, dataset_names: tuple[str, ...] | None = None) -> dict:
    if overlap_days < 1 or reconcile_every_days < 1:
        raise IngestionError("correction overlap and full reconciliation cadence must be positive")
    selected = set(dataset_names or BY_NAME)
    if selected - BY_NAME.keys():
        raise IngestionError("unknown dataset selection")
    report = planned_status(plan, addons, as_of)
    report.update({"started_at": utc_now(), "scope": "all" if dataset_names is None else "explicit_subset"})
    with archive_lock(archive):
        atomic_json(confined(archive, "latest.json"), report)
        stopped = False
        runtime_config = read_json(root / 'config/sources.json', {})
        if dataset_names is None and runtime_config.get("price_source", {}).get("provider") == "jquants_v2_private_archive":
            try:
                report["daily_snapshot"] = initial_snapshot(root, archive, plan, as_of, client)
            except RequestBudgetExhausted:
                report["daily_snapshot"] = {"status": "pending_resume"}
                stopped = True
            except ApiError as error:
                report["daily_snapshot"] = {"status": "error", "error": str(error)}
                if error.kind == "authentication_failed":
                    stopped = True
            except (IngestionError, OSError, ValueError):
                report["daily_snapshot"] = {"status": "error", "error": "snapshot validation failed; no readiness claimed"}
        for dataset in CATALOG:
            entry = report["datasets"][dataset.name]
            if not entitlement(dataset, plan, addons) or dataset.role == "delivery":
                continue
            if dataset.name not in selected:
                entry["collection_status"] = "not_selected"
                continue
            if stopped:
                entry["collection_status"] = "pending_resume"
                continue
            start, end = period(dataset, plan, as_of)
            if dataset.query in {"bulk", "files"}:
                try:
                    from stock_jp.market_data.jquants.delivery import collect_delivery
                    entry.update(collect_delivery(archive, dataset, client, start, end))
                except RequestBudgetExhausted:
                    entry['collection_status'] = 'pending_resume'
                    stopped = True
                except ApiError as error:
                    entry.update(status='error', collection_status='incomplete', error=str(error))
                    if error.kind == 'authentication_failed':
                        stopped = True
                except (IngestionError, OSError, ValueError):
                    entry.update(status='error', collection_status='incomplete', error='delivery validation failed; no completeness claimed')
                atomic_json(confined(archive, "latest.json"), report)
                continue
            state_path = confined(archive, "datasets", dataset.name, "state.json")
            state = read_json(state_path, {"last_full_reconciled": None, "completed_days": {}})
            last_full = date.fromisoformat(state["last_full_reconciled"]) if state["last_full_reconciled"] else None
            full_due = last_full is None or (as_of - last_full).days >= reconcile_every_days
            if full_due and not state.get("reconciliation_cycle"):
                state["reconciliation_cycle"] = as_of.isoformat()
                atomic_json(state_path, state)
            cycle = state.get("reconciliation_cycle") if full_due else as_of.isoformat()
            refresh_from = start if full_due else max(start, as_of - timedelta(days=overlap_days))
            completed = state["completed_days"]
            entry.update(collection_status="running", completed_partitions=0, row_count=0, full_reconciliation=full_due)
            try:
                # Newest first within each dataset; initial operational snapshot is handled separately.
                for day in reversed(list(days_between(start, end))):
                    old = completed.get(day.isoformat())
                    due = not old or (day >= refresh_from and old.get("cycle") != cycle)
                    if due:
                        partition = collect_partition(archive, dataset, day, client)
                        completed[day.isoformat()] = {"checked_as_of": as_of.isoformat(), "cycle": cycle, "row_count": partition["row_count"]}
                        atomic_json(state_path, state)
                    else:
                        # Coverage is only durable if the partition and all page hashes remain valid.
                        partition_rows(archive, dataset.name, day)
                    entry["completed_partitions"] += 1
                    entry["row_count"] += completed[day.isoformat()]["row_count"]
                    if dataset.name == "equities_daily" and completed[day.isoformat()]["row_count"] and runtime_config.get("price_source", {}).get("provider") == "jquants_v2_private_archive":
                        try:
                            publish_snapshot(root, archive, day)
                        except (IngestionError, OSError, ValueError):
                            entry.setdefault("snapshot_export_failures", []).append(day.isoformat())
                entry["collection_status"] = "complete"
                entry["entitlement_basis"] = "successful_api_responses_for_requested_period"
                entry["coverage"] = {"from": start.isoformat(), "to": end.isoformat(), "partition_count": entry["completed_partitions"], "empty_partitions_preserved": True}
                if full_due:
                    state["last_full_reconciled"] = as_of.isoformat()
                    state["reconciliation_cycle"] = None
                    atomic_json(state_path, state)
            except RequestBudgetExhausted:
                entry["collection_status"] = "pending_resume"
                stopped = True
            except ApiError as error:
                entry.update(status="not_available" if error.status == 210 else "error", collection_status="incomplete", error=str(error))
                if error.kind == "authentication_failed":
                    stopped = True
            except (IngestionError, OSError, ValueError):
                entry.update(status="error", collection_status="incomplete", error="local validation or storage failure; no data promoted")
            atomic_json(confined(archive, "latest.json"), report)
        report["all_entitled_complete"] = all(
            entry["status"] == "not_entitled" or entry["role"] == "delivery" or entry["collection_status"] == "complete"
            for entry in report["datasets"].values()) and dataset_names is None
        report.update(completed_at=utc_now(), requests_this_run=client.requests)
        atomic_json(confined(archive, "runs", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json"), report)
        atomic_json(confined(archive, "latest.json"), report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=project_root())
    parser.add_argument("--catalog", action="store_true", help="show catalog and declared subscription scope; no network")
    parser.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(ZoneInfo("Asia/Tokyo")).date())
    args = parser.parse_args(argv)
    try:
        config = read_json(args.root / 'config/sources.json', {})
        settings = config.get('jquants', {})
        plan = settings.get('plan')
        if plan not in PLANS:
            raise IngestionError('jquants.plan must be explicitly verified and set before collection')
        addons = tuple(settings.get('addons', []))
        if set(addons) - {'minute_tick', 'tdnet'}:
            raise IngestionError('unknown J-Quants add-on declaration')
        if args.catalog:
            print(json.dumps(planned_status(plan, addons, args.as_of), ensure_ascii=False, indent=2))
            return 0
        ingestion = settings.get('ingestion', {})
        if not settings.get('enabled') or not ingestion.get('enabled'):
            raise IngestionError('J-Quants full ingestion is disabled')
        if settings.get('base_url') != BASE_URL or settings.get('api_key_env') != 'JQUANTS_API_KEY':
            raise IngestionError('J-Quants origin and credential environment name must match the fixed contract')
        client = Client(plan=plan, max_requests=ingestion.get('max_requests_per_run'), download_hosts=tuple(ingestion.get('download_hosts', [])))
        archive = private_root(args.root, ingestion.get('archive_root', '.work/market-data/jquants'))
        report = ingest(root=args.root, archive=archive, plan=plan, addons=addons, as_of=args.as_of, client=client, overlap_days=int(ingestion.get('overlap_days', 30)), reconcile_every_days=int(ingestion.get('reconcile_every_days', 30)))
        print(json.dumps({'all_entitled_complete': report['all_entitled_complete'], 'requests_this_run': client.requests, 'datasets': {name: {'status': item['status'], 'collection_status': item['collection_status']} for name, item in report['datasets'].items()}}, ensure_ascii=False))
        return 0 if report['all_entitled_complete'] else 2
    except (IngestionError, ValueError, TypeError) as error:
        print(json.dumps({'status': 'blocked', 'reason': str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
