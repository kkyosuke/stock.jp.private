"""Capture adopted generations and primary-source receipts for the frozen study."""
from __future__ import annotations

import argparse
from datetime import date, datetime
import csv
import hashlib
import json
from pathlib import Path

from stock_jp.market_data.primary_sources import collect, confined, MAX_BYTES
from stock_jp.research.financial_inputs import FrozenStore
from stock_jp.research.forward_holdout import JST, MANIFEST, PLAN, CODE, sha
from stock_jp.research.historical_replay import initialize_input_bundle, validate_inputs, ReplayInputError
from stock_jp.workspace import project_root


def initialize(root):
    plan_path = root / PLAN
    plan = json.loads(plan_path.read_text())
    if plan.get("status") != "FROZEN" or any(sha(root / p) != h for p, h in plan["frozen_files"].items()):
        raise ValueError("frozen research contract changed; collection cannot restart the study")
    if set(plan["frozen_files"]) != set(CODE):
        raise ValueError("frozen study code bindings are incomplete")
    path = root / MANIFEST
    if path.exists():
        manifest = json.loads(path.read_text())
        if (manifest.get("period") != plan["period"]
                or manifest.get("holdout", {}).get("plan_sha256") != sha(plan_path)
                or manifest.get("evaluation_kind") != "FORWARD_HOLDOUT"):
            raise ValueError("existing forward input belongs to another study")
        return path
    path = initialize_input_bundle(
        root=root, destination=path.parent / "input", evaluation_kind="FORWARD_HOLDOUT",
        period_from=date.fromisoformat(plan["period"]["from"]),
        period_through=date.fromisoformat(plan["period"]["through"]),
        initial_capital=plan["inputs_and_execution_contract"]["model_initial_capital_jpy"],
    )
    manifest = json.loads(path.read_text())
    # The legacy initializer returns research-relative paths. Bind canonical root-relative paths.
    for dataset in manifest["datasets"]:
        dataset["path"] = "research/" + dataset["path"]
    manifest["holdout"] = {"plan_path": PLAN, "plan_sha256": sha(plan_path), "retuning_count": 0}
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return path


def capture(root: Path, *, cutoff: datetime, download=None, logical_day=None):
    if cutoff.tzinfo is None or cutoff > datetime.now(JST):
        raise ValueError("collection cutoff must be aware and cannot be in the future")
    policy = json.loads((root / "config/policy.json").read_text())
    if policy.get("operation_mode") != "PAPER" or policy.get("active_rule_version") != "v0.4":
        raise ValueError("forward collection is authorised only for v0.4 PAPER")
    manifest_path = initialize(root)
    plan = json.loads((root / PLAN).read_text())
    pointer_path = root / "data/market/jquants/current.json"
    pointer = json.loads(pointer_path.read_text())
    store = FrozenStore(root, pointer["generation"])
    partitions = {}
    for role in ("equities_master", "equities_daily", "financial_summary", "trading_calendar", "topix_daily", "indices_daily"):
        names = sorted(n for n in store.files if n.startswith(f"jquants/datasets/{role}/") and n.endswith("/latest.json"))
        partitions[role] = [{"path": n, **store.files[n]} for n in names]
    # Verify the latest dated price metadata; other entries remain an input inventory.
    price_dates = [n["path"].split("/")[-2] for n in partitions["equities_daily"]]
    latest = max(price_dates) if price_dates else None
    latest_metadata = None
    if latest:
        latest_metadata = json.loads(store.read(f"jquants/datasets/equities_daily/{latest}/latest.json"))
        if date.fromisoformat(latest) > cutoff.astimezone(JST).date():
            raise ValueError("adopted prices are after the collection cutoff")
    primary = collect(root, cutoff=cutoff, download=download, logical_day=logical_day)
    register_sources(root, manifest_path, primary)
    try:
        _, quality = validate_inputs(root=root, manifest_path=manifest_path)
        blockers = []
    except ReplayInputError as error:
        quality, blockers = None, error.blockers
    if not partitions["indices_daily"]:
        blockers.append("Official daily Growth250 indices_daily absent; declared plan remains Light")
    result = {"schema_version": "1.0", "status": "INPUTS_COLLECTED_REVIEW_REQUIRED",
              "captured_at_jst": datetime.now(JST).isoformat(timespec="seconds"),
              "cutoff_at_jst": cutoff.astimezone(JST).isoformat(timespec="seconds"),
              "period": plan["period"], "plan_sha256": sha(root / PLAN),
              "generation": pointer["generation"], "pointer_sha256": sha(pointer_path),
              "latest_price_date": latest, "latest_price_metadata": latest_metadata,
              "partitions": partitions, "primary_sources": primary,
              "input_manifest_sha256": sha(manifest_path), "formal_input_quality": quality,
              "formal_input_blockers": blockers, "raw_pinned_inputs_verified": store.used,
              "formal_monthly_evaluations_credited": 0, "formal_trades_credited": 0,
              "live_promotion_approved": False}
    raw = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode()
    # Append-only receipts preserve the actual generation and retrieval chronology.
    if len(raw) > MAX_BYTES:
        raise ValueError("collection receipt exceeds the Git file limit")
    digest = hashlib.sha256(raw).hexdigest()
    # JSON receipts are separate from provider raw objects.
    receipt = confined(root, f"research/historical-replay/v04-forward/collection/{cutoff.astimezone(JST).date()}/{digest}.json")
    receipt.parent.mkdir(parents=True, exist_ok=True)
    if receipt.exists() and receipt.read_bytes() != raw:
        raise ValueError("immutable collection receipt changed")
    receipt.write_bytes(raw)
    return result, receipt


def register_sources(root, manifest_path, primary):
    """Append explicit publication evidence; never approve replay rights."""
    manifest = json.loads(manifest_path.read_text())
    if manifest["status"] != "DRAFT":
        return
    dataset = next(d for d in manifest["datasets"] if d["role"] == "source_register")
    path = confined(root, dataset["path"])
    if sha(path) != dataset["sha256"]:
        raise ValueError("source register changed outside its input manifest")
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        fields, rows = reader.fieldnames, list(reader)
    ids = {row["source_id"]: row for row in rows}
    for source in primary["sources"]:
        if not source.get("published_at_jst") or not source.get("source_id"):
            continue
        row = {"source_id": source["source_id"], "provider": "Issuer IR via EIR Parts",
               "title": source["title"], "published_at_jst": source["published_at_jst"],
               "url": source["url"], "official": "true", "replay_authorized": "false",
               "content_sha256": source["sha256"]}
        if row["source_id"] in ids:
            if ids[row["source_id"]] != row:
                raise ValueError("registered document or review changed; manual reconciliation required")
        else:
            rows.append(row)
            ids[row["source_id"]] = row
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    dataset["sha256"] = sha(path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def main(*, bound_cutoff=None, logical_day=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", help="aware JST cutoff; defaults to actual collection time")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = project_root()
    if bound_cutoff is not None and args.cutoff not in (None, bound_cutoff):
        raise ValueError("forward cutoff differs from bound PAPER target")
    selected = bound_cutoff or args.cutoff
    cutoff = datetime.fromisoformat(selected) if selected else datetime.now(JST)
    output = confined(root, str(args.output))
    result, receipt = capture(root, cutoff=cutoff, logical_day=logical_day)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "receipt": receipt.relative_to(root).as_posix(),
                      "primary_source_count": len(result["primary_sources"]["sources"]),
                      "formal_input_blocker_count": len(result["formal_input_blockers"])}))
    return int(bool(result["primary_sources"]["blockers"]))


if __name__ == "__main__":
    raise SystemExit(main())
