#!/usr/bin/env python3
"""Read-only EDINET API v2 check for a date range and an optional code filter.

The API key is read from EDINET_API_KEY and is never written to stdout, the
report, or the process arguments. EDINET answers an invalid key with HTTP 200
and a JSON body carrying StatusCode 401, so the body is always inspected.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
BASE_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
TIMEOUT_SECONDS = 30


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _fetch(day: date, key: str) -> dict:
    query = urllib.parse.urlencode({"date": day.isoformat(), "type": "2"})
    request = urllib.request.Request(
        f"{BASE_URL}?{query}",
        headers={
            "Subscription-Key": key,
            "Accept": "application/json",
            "User-Agent": "stock-jp-private-edinet-source-check",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    status = payload.get("metadata", {}).get("status") or payload.get("StatusCode")
    if str(status) != "200":
        message = payload.get("message") or payload.get("metadata", {}).get("message", "")
        raise RuntimeError(f"EDINET returned status {status}: {message}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--through", dest="date_through", required=True)
    parser.add_argument("--codes", default="", help="comma-separated 4-digit codes")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    key = os.environ.get("EDINET_API_KEY", "").strip()
    if not key:
        print("EDINET_API_KEY is not set", file=sys.stderr)
        return 2

    codes = {c.strip() for c in args.codes.split(",") if c.strip()}
    start, end = _parse_date(args.date_from), _parse_date(args.date_through)
    if end < start:
        print("--through must not precede --from", file=sys.stderr)
        return 2

    report = {
        "schema_version": "1.0",
        "checked_at_jst": datetime.now(JST).isoformat(timespec="seconds"),
        "from": start.isoformat(),
        "through": end.isoformat(),
        "codes": sorted(codes),
        "days": [],
        "matches": [],
        "errors": [],
    }

    day = start
    while day <= end:
        try:
            payload = _fetch(day, key)
        except (urllib.error.URLError, RuntimeError, ValueError) as error:
            report["errors"].append(f"{day.isoformat()}: {error}")
            report["days"].append({"date": day.isoformat(), "status": "ERROR", "count": None})
            day += timedelta(days=1)
            continue
        results = payload.get("results") or []
        report["days"].append(
            {"date": day.isoformat(), "status": "OK", "count": len(results)}
        )
        for item in results:
            sec_code = (item.get("secCode") or "").strip()
            if codes and sec_code[:4] not in codes:
                continue
            report["matches"].append(
                {
                    "date": day.isoformat(),
                    "doc_id": item.get("docID"),
                    "sec_code": sec_code,
                    "filer": item.get("filerName"),
                    "doc_type_code": item.get("docTypeCode"),
                    "description": item.get("docDescription"),
                    "submitted_at": item.get("submitDateTime"),
                }
            )
        day += timedelta(days=1)

    with open(args.output, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("## EDINET 一次情報チェック\n\n```json\n")
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n```\n")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
