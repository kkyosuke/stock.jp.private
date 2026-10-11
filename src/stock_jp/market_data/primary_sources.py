"""Preserve configured public primary sources without certifying their contents."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import ssl
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from zoneinfo import ZoneInfo

import certifi

JST = ZoneInfo("Asia/Tokyo")
MAX_BYTES = 22 * 1024 * 1024
CONFIG = "operations/inputs/primary-sources.json"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPEN = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where()))).open


def confined(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError("primary-source path escapes the repository")
    return path


def checked_url(url, hosts):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username
            or parsed.password or parsed.port not in {None, 443}):
        raise ValueError("primary source must use a configured HTTPS origin")
    return url


class Download:
    """One serial stream, at most 60 requests a minute, including failures."""

    def __init__(self, hosts, *, limit=60, opener=OPEN, sleep=time.sleep, clock=time.monotonic):
        self.hosts, self.limit = set(hosts), limit
        self.opener, self.sleep, self.clock = opener, sleep, clock
        self.requests, self.last = 0, None

    def __call__(self, url):
        checked_url(url, self.hosts)
        if self.requests >= self.limit:
            raise ValueError("primary-source request budget exhausted")
        if self.last is not None:
            self.sleep(max(0, 1.05 - (self.clock() - self.last)))
        self.last = self.clock()
        self.requests += 1
        request = Request(url, headers={"User-Agent": "stock.jp private PAPER research"})
        # Redirects are rejected instead of forwarding a request to an unreviewed origin.
        with self.opener(request, timeout=30) as response:
            if response.geturl() != url:
                raise ValueError("primary-source redirect requires source configuration review")
            raw = response.read(MAX_BYTES + 1)
        if not raw or len(raw) > MAX_BYTES:
            raise ValueError("primary source is empty or exceeds the Git file limit")
        return raw


def preserve(root, raw, suffix):
    if suffix not in {".pdf", ".js", ".csv", ".html"}:
        raise ValueError("unsupported primary-source format")
    if not raw or len(raw) > MAX_BYTES or (suffix == ".pdf" and not raw.startswith(b"%PDF-")):
        raise ValueError("invalid primary-source bytes")
    digest = hashlib.sha256(raw).hexdigest()
    path = confined(root, f"data/market/primary-sources/{digest}{suffix}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != raw:
        raise ValueError("immutable primary-source object changed")
    if not path.exists():
        path.write_bytes(raw)
    return {"path": path.relative_to(root).as_posix(), "sha256": digest, "bytes": len(raw)}


def feed_items(raw, code):
    text = raw.decode("utf-8-sig").strip()
    # Decode JSONP as data; never execute the provider's JavaScript.
    match = re.fullmatch(r"eolparts_announcement_\d+\(\s*(\{.*\})\s*\);?", text, re.S)
    if not match:
        raise ValueError("unknown issuer IR feed layout")
    payload = json.loads(match[1])
    if (not payload.get("header") or payload["header"][0].get("customer_code") != code
            or not isinstance(payload.get("item"), list)
            or str(payload.get("item_count")) != str(len(payload["item"]))):
        raise ValueError("IR feed issuer or complete item count differs")
    return payload["item"]


def available_at(value):
    parsed = datetime.strptime(value, "%Y/%m/%d %H:%M:%S").replace(tzinfo=JST)
    date_only = parsed.time().isoformat() == "00:00:00"
    if date_only:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return parsed, "DATE_ONLY" if date_only else "SECOND"


def collect(root: Path, *, cutoff: datetime, download=None, logical_day=None):
    if cutoff.tzinfo is None:
        raise ValueError("primary-source cutoff needs a UTC offset")
    cutoff = cutoff.astimezone(JST)
    config_path = confined(root, CONFIG)
    config = json.loads(config_path.read_text())
    source_config_path = confined(root, "config/sources.json")
    market_regime = json.loads(source_config_path.read_text())["market_regime"]
    fetch = download or Download(config["allowed_hosts"], limit=config["max_requests"])
    from stock_jp.market_data.primary_reuse import load
    reused = load(root, day=logical_day or cutoff.date().isoformat(), cutoff=cutoff.isoformat(timespec="seconds"))
    hits = 0

    def obtain(url):
        nonlocal hits
        if url in reused:
            hits += 1
            source = reused[url]
            raw = confined(root, source["path"]).read_bytes()
            if len(raw) != source["bytes"] or hashlib.sha256(raw).hexdigest() != source["sha256"]:
                raise ValueError("primary source changed after reuse validation")
            return raw
        return fetch(url)

    def retrieved(url):
        return reused[url]["retrieved_at_jst"] if url in reused else datetime.now(JST).isoformat(timespec="seconds")

    sources, blockers = [], []
    observed = datetime.now(JST).isoformat(timespec="seconds")
    for issuer in config["issuers"]:
        code = issuer["code"]
        if not re.fullmatch(r"[0-9A-Z]{4}", code):
            raise ValueError("invalid configured issuer code")
        try:
            checked_url(issuer["feed_url"], config["allowed_hosts"])
            raw = obtain(issuer["feed_url"])
            items = feed_items(raw, code)
            sources.append({"code": code, "title": "Official issuer IR announcement feed",
                            "url": issuer["feed_url"], "retrieved_at_jst": retrieved(issuer["feed_url"]),
                            "item_count": len(items), **preserve(root, raw, ".js")})
            floor = datetime.fromisoformat(issuer["from_at_jst"]).astimezone(JST)
            for item in items:
                published, precision = available_at(item["date"])
                if not floor <= published <= cutoff or item.get("type") != "pdf":
                    continue
                url = checked_url(item["link"], config["allowed_hosts"])
                if not urlsplit(url).path.startswith(f"/doc/{code}/"):
                    raise ValueError("IR document does not belong to configured issuer")
                raw = obtain(url)
                sources.append({"code": code, "source_id": f"company-{code}-{item['guid']}",
                                "title": item["title"], "url": url,
                                "published_at_jst": published.isoformat(timespec="seconds"),
                                "publication_precision": precision, "retrieved_at_jst": retrieved(url),
                                "review_status": "COLLECTED_NOT_REVIEWED",
                                "original_historical_vintage_verified": False,
                                **preserve(root, raw, ".pdf")})
        except (OSError, ValueError, KeyError, TypeError) as error:
            blockers.append({"source": f"company_ir:{code}", "error": str(error)})
    for series in config["public_series"]:
        try:
            url = checked_url(market_regime[series["config_key"]], config["allowed_hosts"])
            raw = obtain(url)
            sources.append({"series": series["id"], "title": series["title"], "url": url,
                            "retrieved_at_jst": retrieved(url), "review_status": "COLLECTED_NOT_REVIEWED",
                            "original_historical_vintage_verified": False,
                            **preserve(root, raw, series["suffix"])})
        except (OSError, ValueError, KeyError, TypeError) as error:
            blockers.append({"source": series["id"], "error": str(error)})
    return {"schema_version": "1.0", "scope": "CONFIGURED_ACTIVE_TARGETS_AND_MACRO_SOURCES",
            "cutoff_at_jst": cutoff.isoformat(timespec="seconds"), "retrieved_at_jst": observed,
            "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "source_config_sha256": hashlib.sha256(source_config_path.read_bytes()).hexdigest(),
            "sources": sources, "blockers": blockers,
            "request_count": getattr(fetch, "requests", None), "cache_hits": hits,
            "full_universe_disclosure_coverage_certified": False,
            "interpretation": "Collection does not certify disclosure completeness, source rights, original historical vintages, or scores."}
