"""Private, resumable J-Quants file delivery (no authenticated calls on import).

Official references: /en/spec/bulk-list, bulk-get, td-list, td-files, td-bulk
at https://jpx-jquants.com. Public docs do not identify the production storage
hosts. Live downloads therefore require an operator-reviewed exact host list in
Client.download_hosts. No wildcard, HTTP, redirects, cookies or API-key forwarding.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zlib
from typing import TYPE_CHECKING, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

if TYPE_CHECKING:
    from scripts.jquants_catalog import Dataset
    from scripts.jquants_ingest import Client


class DeliveryError(ValueError):
    """Sanitized delivery failure; never contains a signed URL or provider body."""


def _encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _sha(value: object) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _safe_path(root: Path, path: Path, *, directory: bool = False, parents: bool = False) -> Path:
    """Reject every symlink below the archive, before reads, staging or writes."""
    anchor = root.absolute()
    candidate = path.absolute()
    try:
        parts = candidate.relative_to(anchor).parts
    except ValueError:
        raise DeliveryError("delivery object escaped private archive") from None
    if ".." in parts:
        raise DeliveryError("delivery object escaped private archive")
    try:
        if anchor.is_symlink():
            raise DeliveryError("delivery archive symlink rejected")
        anchor.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not anchor.is_dir():
            raise DeliveryError("invalid delivery archive directory")
        anchor.chmod(0o700)
        current = anchor
        for index, part in enumerate(parts):
            current = current / part
            last = index == len(parts) - 1
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                if (not last and parents) or (last and directory):
                    current.mkdir(mode=0o700)
                    mode = current.lstat().st_mode
                else:
                    continue
            if stat.S_ISLNK(mode):
                raise DeliveryError("delivery archive symlink rejected")
            if not last or directory:
                if not stat.S_ISDIR(mode):
                    raise DeliveryError("invalid delivery archive directory")
                current.chmod(0o700)
        if not candidate.resolve().is_relative_to(anchor.resolve()):
            raise DeliveryError("delivery object escaped private archive")
        return candidate
    except OSError:
        raise DeliveryError("delivery archive path validation failed") from None


def _write(root: Path, path: Path, value: object) -> None:
    path = _safe_path(root, path, parents=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        _sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read(root: Path, path: Path) -> dict | None:
    path = _safe_path(root, path)
    try:
        if not path.exists():
            return None
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise DeliveryError("invalid delivery metadata") from None


def _archive_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise DeliveryError("invalid delivery object path")
    return _safe_path(root, root / relative)


def _verify(root: Path, ref: dict) -> None:
    try:
        path = _archive_path(root, ref["path"])
        h, size = hashlib.sha256(), 0
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(block)
                size += len(block)
        if h.hexdigest() != ref["sha256"] or size != ref["bytes"]:
            raise DeliveryError("delivery object checksum or size mismatch")
    except (KeyError, TypeError, OSError):
        raise DeliveryError("delivery object unavailable or invalid") from None


def _url(url: str, hosts: tuple[str, ...]) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username or parsed.password or
                parsed.port not in (None, 443) or parsed.fragment or not parsed.path or
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or
                host.endswith((".local", ".localhost", ".internal")) or "." not in host or
                host not in hosts or re.fullmatch(r"[0-9.]+", host) or any(c in url for c in "\r\n\t")):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise DeliveryError("download requires HTTPS and an explicitly reviewed exact public storage hostname") from None
    return url


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DeliveryError("download redirect rejected")


class _RetryDownload(DeliveryError):
    pass


def _fetch(root: Path, url: str, client: Client, *, compressed: bool, expected_size: int | None = None) -> dict:
    """Stream bytes, validate complete gzip CRC/trailer, then atomically promote."""
    _url(url, tuple(getattr(client, "download_hosts", ())))
    folder = _safe_path(root, root / "blobs", directory=True, parents=True)
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=folder)
    h, size = hashlib.sha256(), 0
    try:
        # Dedicated opener: no API session, API header, cookie jar, or redirect.
        request = Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "stock.jp-private-research/1.0"})
        try:
            with os.fdopen(fd, "wb") as target:
                with build_opener(_NoRedirect()).open(request, timeout=300) as response:
                    if response.status != 200:
                        raise _RetryDownload("file download failed")
                    length = response.headers.get("Content-Length")
                    for block in iter(lambda: response.read(1024 * 1024), b""):
                        target.write(block)
                        h.update(block)
                        size += len(block)
                target.flush()
                os.fsync(target.fileno())
            if size == 0 or (expected_size is not None and size != expected_size):
                raise _RetryDownload("download byte count mismatch")
            if length is not None and (not length.isdigit() or size != int(length)):
                raise _RetryDownload("download Content-Length mismatch")
            if compressed:
                with gzip.open(temporary, "rb") as stream:
                    while stream.read(1024 * 1024):
                        pass
        except HTTPError as error:
            if error.code in {401, 403, 408, 429, 500, 502, 503, 504}:
                raise _RetryDownload("temporary or expired file download") from None
            raise DeliveryError("file download rejected") from None
        except (URLError, TimeoutError):
            raise _RetryDownload("file download network failure") from None
        except (gzip.BadGzipFile, EOFError, zlib.error):
            raise _RetryDownload("download gzip validation failed") from None
        sha = h.hexdigest()
        relative = Path("blobs") / sha[:2] / (sha + (".csv.gz" if compressed else ".bin"))
        path = _safe_path(root, root / relative, parents=True)
        ref = {"path": relative.as_posix(), "sha256": sha, "bytes": size, "gzip_validated": compressed}
        if path.exists():
            _verify(root, ref)
        else:
            os.replace(temporary, path)
            _sync_dir(path.parent)
        return ref
    except OSError:
        raise DeliveryError("file download or local storage failure") from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _download(root: Path, client: Client, get_url: Callable[[], str], *, compressed: bool, expected_size: int | None = None) -> dict:
    for attempt in range(3):
        # A fresh signed URL is requested after each failed attempt. URLs stay in memory.
        url = get_url()
        try:
            return _fetch(root, url, client, compressed=compressed, expected_size=expected_size)
        except _RetryDownload:
            if attempt == 2:
                raise DeliveryError("file download failed after bounded retries; resume required") from None
            client.sleep(min(60, 2 ** attempt))
    raise AssertionError("unreachable")


def _receipt(root: Path, folder: Path, identity: dict, get_url: Callable[[], str], client: Client,
             *, compressed: bool, expected_size: int | None = None) -> tuple[dict, bool]:
    path = folder / "items" / (_sha(identity) + ".json")
    old = _read(root, path)
    if old is not None:
        if old.get("identity") != identity:
            raise DeliveryError("delivery receipt identity mismatch")
        if not isinstance(old.get("blob"), dict):
            raise DeliveryError("invalid delivery receipt")
        _verify(root, old["blob"])
        return old, True
    blob = _download(root, client, get_url, compressed=compressed, expected_size=expected_size)
    value = {"identity": identity, "blob": blob}
    _write(root, path, value)
    return value, False


def _signed(payload: dict) -> str:
    value = payload.get("url")
    if not isinstance(value, str) or not value:
        raise DeliveryError("missing signed download URL")
    return value


def _bulk(root: Path, dataset: Dataset, client: Client, start: date, end: date, folder: Path) -> list[tuple[dict, bool]]:
    payload = client.get("/bulk/list", {"endpoint": dataset.endpoint, "from": start.isoformat(), "to": end.isoformat()})
    rows = payload.get("data")
    if not isinstance(rows, list) or payload.get("pagination_key"):
        raise DeliveryError("unexpected bulk inventory shape or unsupported pagination")
    identities, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise DeliveryError("invalid bulk inventory entry")
        key, modified, size = row.get("Key"), row.get("LastModified"), row.get("Size")
        if not isinstance(key, str) or not key or not isinstance(modified, str) or not modified or type(size) is not int or size <= 0:
            raise DeliveryError("invalid bulk file identity")
        if not key.startswith(dataset.endpoint.lstrip("/") + "/") or "?" in key or "#" in key or ".." in key.split("/"):
            raise DeliveryError("bulk file key is outside requested dataset")
        try:
            datetime.fromisoformat(modified.replace("Z", "+00:00"))
        except ValueError:
            raise DeliveryError("invalid bulk LastModified") from None
        identity = {"Key": key, "LastModified": modified, "Size": size}
        if key in seen:
            raise DeliveryError("duplicate bulk inventory key")
        seen.add(key)
        identities.append(identity)
    # Never persist arbitrary provider fields, particularly temporary URLs.
    _write(root, folder / "inventory.json", {"endpoint": dataset.endpoint, "files": identities})
    output = []
    for identity in identities:
        output.append(_receipt(root, folder, identity,
                               lambda key=identity["Key"]: _signed(client.get("/bulk/get", {"key": key})),
                               client, compressed=True, expected_size=identity["Size"]))
    return output


def _td_bulk(root: Path, client: Client, folder: Path) -> list[tuple[dict, bool]]:
    first = client.get("/td/bulk", {})
    modified = first.get("lastUpdated")
    if not isinstance(modified, str) or not modified:
        raise DeliveryError("TD bulk lastUpdated is missing")
    try:
        datetime.fromisoformat(modified.replace("Z", "+00:00"))
    except ValueError:
        raise DeliveryError("invalid TD bulk lastUpdated") from None
    pending = [first]
    def get_url():
        payload = pending.pop() if pending else client.get("/td/bulk", {})
        if payload.get("lastUpdated") != modified:
            raise DeliveryError("TD bulk snapshot changed while downloading; resume required")
        return _signed(payload)
    return [_receipt(root, folder, {"endpoint": "/td/bulk", "lastUpdated": modified}, get_url, client, compressed=True)]


def _index(root: Path, start: date, end: date) -> dict[str, set[str]]:
    disclosures: dict[str, set[str]] = {}
    day = start
    while day <= end:
        manifest = _read(root, root / "datasets" / "td_index" / day.isoformat() / "latest.json")
        if not manifest or manifest.get("status") != "complete" or not isinstance(manifest.get("pages"), list):
            raise DeliveryError("TD files require complete td_index partitions for every requested date")
        for ref in manifest["pages"]:
            _verify(root, ref)
            page = _read(root, _archive_path(root, ref["path"]))
            if not page or not isinstance(page.get("data"), list):
                raise DeliveryError("invalid TD index page")
            for row in page["data"]:
                if not isinstance(row, dict):
                    raise DeliveryError("invalid TD index record")
                no, docs = row.get("DiscNo"), row.get("Docs")
                if not isinstance(no, str) or not re.fullmatch(r"\d{14}", no) or not isinstance(docs, list) or any(not isinstance(doc, str) or doc not in {"g", "s", "x"} for doc in docs):
                    raise DeliveryError("invalid disclosure number or document types in TD index")
                disclosures.setdefault(no, set()).update(docs)
        day += timedelta(days=1)
    return disclosures


def _td_files(root: Path, client: Client, start: date, end: date, folder: Path) -> list[tuple[dict, bool]]:
    docs_map = {"g": "pdf", "s": "summaryPdf", "x": "xbrl"}
    output = []
    for no, docs in sorted(_index(root, start, end).items()):
        pending: dict = {}
        def file_url(kind: str):
            nonlocal pending
            if not pending:
                pending = client.get("/td/files", {"discNo": no})
            if pending.get("discNo") != no or not isinstance(pending.get("files"), dict):
                raise DeliveryError("TD file response does not match requested disclosure")
            value = pending["files"].get(kind)
            pending = {}  # Each retry receives a fresh signed URL; never persist this map.
            if not isinstance(value, str) or not value:
                raise DeliveryError("TD file missing for an advertised document type")
            return value
        for doc in sorted(docs):
            kind = docs_map[doc]
            output.append(_receipt(root, folder, {"discNo": no, "document": kind},
                                   lambda kind=kind: file_url(kind), client, compressed=False))
    return output


def collect_delivery(root: Path, dataset: Dataset, client: Client, start: date, end: date) -> dict:
    """Archive all delivered files or return an explicit fail-closed host blocker.

Caller owns entitlement checks and archive lock. Files are immutable snapshots;
provider inventories are re-read on each run so corrected files are re-ingested.
TD bulk cannot be limited to a historical as-of date: it is the latest full index.
"""
    if not getattr(client, "download_hosts", ()):
        return {"status": "not_available", "collection_status": "blocked_download_host",
                "reason": "Set ingestion.download_hosts to independently reviewed exact provider storage hostnames; public J-Quants docs contain placeholder URLs only",
                "downloaded_files": 0, "reused_files": 0, "file_count": 0}
    if start > end:
        raise DeliveryError("delivery period is reversed")
    folder = _safe_path(root, root / "deliveries" / dataset.name, directory=True, parents=True)
    if dataset.endpoint == "/td/files":
        results = _td_files(root, client, start, end, folder)
    elif dataset.endpoint == "/td/bulk":
        results = _td_bulk(root, client, folder)
    elif dataset.query == "bulk" and dataset.endpoint == "/equities/trades":
        results = _bulk(root, dataset, client, start, end, folder)
    else:
        raise DeliveryError("unsupported specialized delivery dataset")
    manifest = {"schema_version": 1, "dataset": dataset.name, "endpoint": dataset.endpoint,
                "requested_period": {"from": start.isoformat(), "to": end.isoformat()},
                "status": "complete", "files": [item for item, _ in results],
                "scope": "latest provider five-year index snapshot" if dataset.endpoint == "/td/bulk" else "all files identified for requested period"}
    _write(root, folder / "revisions" / (_sha(manifest) + ".json"), manifest)
    _write(root, folder / "latest.json", manifest)
    return {"status": "supported", "collection_status": "complete", "file_count": len(results),
            "downloaded_files": sum(not reused for _, reused in results), "reused_files": sum(reused for _, reused in results),
            "bytes": sum(item["blob"]["bytes"] for item, _ in results),
            "coverage": {"from": start.isoformat(), "to": end.isoformat(), "scope": manifest["scope"]},
            "entitlement_basis": "successful_provider_inventory_and_verified_downloads"}
