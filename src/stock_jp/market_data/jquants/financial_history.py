"""Resumable Light-plan financial and earnings-history bulk downloads."""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
import gzip
import io
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

from stock_jp.market_data import storage
from stock_jp.market_data.jquants import delivery, ingest
from stock_jp.market_data.jquants.catalog import BY_NAME, period

PREFIX = "jquants/financial-history/"
DATASETS = ("financial_summary", "earnings_dates")
DEFAULT_BUDGET = 256


def require(condition, message="invalid financial-history checkpoint"):
    if not condition:
        raise ingest.IngestionError(message)


def file_period(key, endpoint):
    require(isinstance(key, str) and key.startswith(endpoint.lstrip("/") + "/")
            and ".." not in key.split("/") and "?" not in key and "#" not in key,
            "bulk file is outside the financial dataset")
    match = re.search(r"_(\d{6}(?:\d{2})?)\.csv\.gz$", key)
    require(match is not None, "unrecognized financial bulk-file date")
    text = match[1]
    start = date(int(text[:4]), int(text[4:6]), int(text[6:8]) if len(text) == 8 else 1)
    if len(text) == 8:
        return start, start
    next_month = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start, next_month - timedelta(days=1)


def inventory(payload, endpoint, start, end, calendar=None):
    require(isinstance(payload.get("data"), list) and not payload.get("pagination_key"),
            "bulk inventory is incomplete")
    result, seen = [], set()
    for row in payload["data"]:
        require(isinstance(row, dict))
        key = row.get("Key")
        first, last = file_period(key, endpoint)
        if last < start or first > end:
            continue
        size, modified = row.get("Size"), row.get("LastModified")
        require(type(size) in (int, float) and math.isfinite(size) and size == int(size),
                "bulk file size must be an exact integer byte count")
        size = int(size)
        require(0 < size <= storage.MAX_SHARD,
                "financial bulk file exceeds the Git shard limit")
        require(isinstance(modified, str) and datetime.fromisoformat(modified.replace("Z", "+00:00")).tzinfo is not None)
        require(key not in seen, "duplicate financial bulk-file key")
        seen.add(key)
        result.append({"Key": key, "Size": size, "LastModified": modified,
                       "from": first.isoformat(), "to": last.isoformat()})
    require(bool(result), "no financial bulk files cover the requested period")
    covered = set()
    for item in result:
        day = max(start, date.fromisoformat(item["from"]))
        while day <= min(end, date.fromisoformat(item["to"])):
            covered.add(day)
            day += timedelta(days=1)
    # Historical files cover whole months; live files are delivered on cash
    # sessions. Closed dates need no fabricated empty financial CSV.
    monthly_end = max((date.fromisoformat(item["to"]) for item in result
                       if len(re.search(r"_(\d+)\.csv\.gz$", item["Key"])[1]) == 6), default=start-timedelta(days=1))
    calendar = {row["Date"]: str(row["HolDiv"]) for row in (calendar or [])}
    day = start
    while day <= end:
        if day not in covered:
            require(day > monthly_end and calendar.get(day.isoformat()) in {"0", "3"},
                    "financial bulk inventory has a date gap")
        day += timedelta(days=1)
    return sorted(result, key=lambda item: (item["from"], item["Key"]))


def csv_rows(path, dataset):
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"DiscDate", "DiscTime", "Code", "DiscNo"} if dataset == "financial_summary" else {"PubDate", "SchDate", "Code"}
        require(reader.fieldnames is not None and required <= set(reader.fieldnames),
                "financial bulk CSV has an unexpected schema")
        require(len(reader.fieldnames) == len(set(reader.fieldnames)), "duplicate bulk CSV column")
        for row in reader:
            require(None not in row and all(isinstance(value, str) for value in row.values()),
                    "invalid financial bulk CSV row")
            yield row


def validate_state(state, read):
    require(set(state) == {"version", "as_of", "from", "plan", "budget", "requests", "inventories", "files", "status", "observed_download_hosts", "error", "retry_not_before", "calendar"})
    end, start = date.fromisoformat(state["as_of"]), date.fromisoformat(state["from"])
    require(state["version"] == 1 and state["plan"] == "light" and start == period(BY_NAME["financial_summary"], "light", end)[0])
    require(type(state["budget"]) is int and state["budget"] == DEFAULT_BUDGET)
    require(state["status"] in {"RUNNING", "INSPECTED", "PAUSED", "COMPLETE"})
    require(isinstance(state["requests"], list) and len(state["requests"]) <= state["budget"])
    for receipt in state["requests"]:
        require(set(receipt) == {"endpoint", "query_sha256", "reserved_at"}
                and receipt["endpoint"] in {"/bulk/list", "/bulk/get"}
                and re.fullmatch("[0-9a-f]{64}", receipt["query_sha256"]) is not None)
        require(datetime.fromisoformat(receipt["reserved_at"]).tzinfo is not None)
    require(set(state["inventories"]) <= set(DATASETS))
    calendar = None
    if state["calendar"]:
        ref = state["calendar"]
        require(set(ref) == {"path", "sha256", "bytes"} and ref["path"] == "calendar.json")
        raw = read(PREFIX + ref["path"])
        require(ingest.digest(raw) == ref["sha256"] and len(raw) == ref["bytes"], "financial calendar checksum mismatch")
        calendar = json.loads(raw)["data"]
    expected = {}
    for name, items in state["inventories"].items():
        checked = inventory({"data": items}, BY_NAME[name].endpoint, start, end, calendar)
        require(checked == items)
        expected.update({item["Key"]: (name, item) for item in items})
    require(set(state["files"]) <= set(expected))
    for key, item in state["files"].items():
        require(set(item) == {"path", "sha256", "bytes", "gzip_validated", "retrieved_at", "rows"})
        sha = item["sha256"]
        require(re.fullmatch("[0-9a-f]{64}", sha) is not None
                and item["path"] == f"blobs/{sha[:2]}/{sha}.csv.gz"
                and item["bytes"] == expected[key][1]["Size"] and item["gzip_validated"] is True)
        raw = read(PREFIX + item["path"])
        require(ingest.digest(raw) == sha and len(raw) == item["bytes"], "financial bulk checksum mismatch")
        require(datetime.fromisoformat(item["retrieved_at"]).tzinfo is not None)
        require(item["rows"] is None or type(item["rows"]) is int and item["rows"] >= 0)
    if state["status"] == "COMPLETE":
        require(set(state["inventories"]) == set(DATASETS) and set(state["files"]) == set(expected))
        require(all(type(item["rows"]) is int for item in state["files"].values()))


class Client(ingest.Client):
    def __init__(self, state, save, remote=None, **kwargs):
        super().__init__(plan="light", retries=0, max_response_bytes=8*1024*1024, **kwargs)
        self.state, self.save, self.remote = state, save, remote

    def _request(self, endpoint, params):
        require(endpoint in {"/bulk/list", "/bulk/get"}, "unexpected financial-history endpoint")
        if self.remote:
            self.remote.before_request()
        require(len(self.state["requests"]) < self.state["budget"], "financial-history request budget exhausted")
        prior = self.state["requests"]
        if prior:
            elapsed = (datetime.now().astimezone() - datetime.fromisoformat(prior[-1]["reserved_at"])).total_seconds()
            self.sleep(max(0, self.interval - elapsed))
        prior.append({"endpoint": endpoint, "query_sha256": ingest.digest(ingest.encoded(params)),
                      "reserved_at": datetime.now().astimezone().isoformat()})
        self.save()  # Persist before provider I/O; failures and crashes remain charged.
        if self.remote:
            self.remote.active_run()
        return super()._request(endpoint, params)


def collect(root, as_of, *, remote=None, transport=None, sleep=None):
    root = Path(root)
    source = root / ".work/market-data"
    folder = source / PREFIX
    state_path = folder / (as_of.isoformat() + ".json")
    config = ingest.read_json(root / "config/sources.json")["jquants"]
    require(config["plan"] == "light" and config["enabled"] is True)
    end = as_of
    require(end < datetime.now().astimezone().date(), "financial history must use a completed date")
    start = period(BY_NAME["financial_summary"], "light", end)[0]
    folder.mkdir(parents=True, exist_ok=True)
    state = ingest.read_json(state_path) or {"version": 1, "as_of": end.isoformat(), "from": start.isoformat(),
        "plan": "light", "budget": DEFAULT_BUDGET, "requests": [], "inventories": {}, "files": {},
        "status": "RUNNING", "observed_download_hosts": [], "error": None, "retry_not_before": None, "calendar": None}
    state.setdefault("calendar", None)  # Retain charges from the initial inventory inspection.
    if state["calendar"] is None and (root / "data/market/jquants/current.json").exists():
        store = root / "data/market/jquants"
        pointer = ingest.read_json(store / "current.json")
        members = json.loads(storage.manifest_bytes(store, pointer))["files"]
        retained = json.loads(storage.member_bytes(store, pointer, members["jquants/history/state.json"]))
        pages = retained["ranges"]["trading_calendar"]["pages"]
        require(len(pages) == 1, "financial history requires the adopted cash calendar")
        raw = storage.member_bytes(store, pointer, members["jquants/" + pages[0]["path"]])
        require(ingest.digest(raw) == pages[0]["sha256"])
        storage.atomic(folder / "calendar.json", raw)
        state["calendar"] = {"path": "calendar.json", "sha256": ingest.digest(raw), "bytes": len(raw)}
    validate_state(state, lambda name: (source / name).read_bytes())
    def save():
        ingest.atomic_json(state_path, state)
        if remote:
            remote.sync()
    if state["status"] == "COMPLETE":
        return state
    if state["retry_not_before"] and datetime.now().astimezone() < datetime.fromisoformat(state["retry_not_before"]):
        return state
    kwargs = {"transport": transport, "download_hosts": tuple(config["ingestion"].get("download_hosts", []))}
    if sleep is not None:
        kwargs["sleep"] = sleep
    client = Client(state, save, remote, **kwargs)
    state["status"], state["error"] = "RUNNING", None
    try:
        for name in DATASETS:
            if name not in state["inventories"]:
                cached_path = folder / "inventories" / (name + ".json")
                cached = ingest.read_json(cached_path)
                query = {"endpoint": BY_NAME[name].endpoint, "from": start.isoformat(), "to": end.isoformat()}
                if cached is None:
                    payload = client.get("/bulk/list", query)
                    require(isinstance(payload.get("data"), list), "invalid bulk inventory shape")
                    cached = {"query": query, "retrieved_at": ingest.utc_now(),
                              "data": [{key: row.get(key) for key in ("Key", "Size", "LastModified")} for row in payload["data"]],
                              "pagination_key": payload.get("pagination_key")}
                    ingest.atomic_json(cached_path, cached)
                    save()
                require(cached["query"] == query, "financial inventory query changed")
                payload = cached
                calendar = json.loads((folder / "calendar.json").read_bytes())["data"] if state["calendar"] else None
                state["inventories"][name] = inventory(payload, BY_NAME[name].endpoint, start, end, calendar)
                save()
        for name in DATASETS:
            for identity in state["inventories"][name]:
                key = identity["Key"]
                if key in state["files"] and state["files"][key]["rows"] is not None:
                    continue
                if key not in state["files"]:
                    payload = client.get("/bulk/get", {"key": key})
                    url = payload.get("url")
                    require(isinstance(url, str), "financial download URL missing")
                    host = urlsplit(url).hostname
                    require(isinstance(host, str), "financial download hostname missing")
                    if not client.download_hosts:
                        state["observed_download_hosts"] = sorted(set(state["observed_download_hosts"]) | {host})
                        state["status"] = "INSPECTED"
                        save()
                        return state
                    blob = delivery._fetch(folder, url, client, compressed=True, expected_size=identity["Size"])
                    state["files"][key] = {**blob, "retrieved_at": ingest.utc_now(), "rows": None}
                    save()
                item = state["files"][key]
                count = sum(1 for _ in csv_rows(folder / item["path"], name))
                item["rows"] = count
                save()
        state["status"] = "COMPLETE"
        validate_state(state, lambda name: (source / name).read_bytes())
    except ingest.ApiError as error:
        state["status"], state["error"] = "PAUSED", str(error)
        if error.status == 429:
            state["retry_not_before"] = (datetime.now().astimezone() + timedelta(seconds=120)).isoformat()
        raise
    except (ValueError, OSError):
        state["status"], state["error"] = "PAUSED", "financial bulk validation or download failed"
        raise
    finally:
        save()
    return state
