"""Private, cutoff-scoped page checkpoints for bounded nightly source scans."""

from __future__ import annotations

from stock_jp.workspace import record_path
from pathlib import Path
from datetime import datetime

from stock_jp.market_data.jquants.ingest import Client, IngestionError, archive_lock, atomic_json, checked_payload, confined, digest, encoded, read_json, save_object


class NightlyClient:
    """Reuse only validated pages for the exact frozen cutoff and query.

    A page is committed atomically before requesting the next one. A crash before
    that commit can repeat that one read, but cannot mark a partial query complete.
    Every transport attempt, including retries, shares the invocation's budget.
    """

    def __init__(self, *, private: Path, cutoff: str, retrieved: str,
                 plan: str, budget: int = 12, timeout: int = 30, **test_transport):
        if type(budget) is not int or not 1 <= budget <= 12:
            raise IngestionError("nightly request budget must be an integer from 1 to 12")
        self.client = Client(plan=plan, timeout=timeout, max_requests=budget, **test_transport)
        self.identity = {"version": 1, "origin": "https://api.jquants.com/v2",
                         "plan": plan, "cutoff": cutoff}
        self.root = confined(record_path(private.parent, "market-data"), "nightly-source-cache",
                             digest(encoded(self.identity)))
        self.retrieved = retrieved
        self.cache_hits = 0
        self.receipts: dict[str, dict] = {}

    @property
    def requests(self):
        return self.client.requests

    def get(self, endpoint: str, params: dict) -> dict:
        identity = {**self.identity, "endpoint": endpoint, "params": params}
        key = digest(encoded(identity))
        with archive_lock(self.root):
            path = confined(self.root, "pages", key + ".json")
            existed = path.exists()
            receipt = read_json(path)
            if existed:
                if not isinstance(receipt, dict) or receipt.get("identity") != identity:
                    raise IngestionError("nightly page checkpoint identity mismatch")
                try:
                    payload = checked_payload(self.root, receipt["object"])
                    if not isinstance(receipt["retrieved_at_jst"], str):
                        raise ValueError()
                    observed = datetime.fromisoformat(receipt["retrieved_at_jst"])
                    if observed.tzinfo is None or observed.utcoffset() is None:
                        raise ValueError()
                except (KeyError, TypeError, ValueError):
                    raise IngestionError("invalid nightly page checkpoint") from None
                self.cache_hits += 1
            else:
                payload = self.client.get(endpoint, params)
            if not isinstance(payload, dict):
                raise IngestionError("J-Quants returned an invalid data schema")
            data = payload.get("data")
            cursor = payload.get("pagination_key")
            if (not isinstance(data, list) or any(not isinstance(row, dict) for row in data)
                    or (cursor is not None and not isinstance(cursor, str))):
                raise IngestionError("J-Quants returned an invalid data schema")
            if receipt is None:
                receipt = {"identity": identity, "object": save_object(self.root, payload),
                           "retrieved_at_jst": self.retrieved}
                atomic_json(path, receipt)
            # Receipts carry actual retrieval times, including on cached replay.
            self.receipts[key] = receipt
            return payload
