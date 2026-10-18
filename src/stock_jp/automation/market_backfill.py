"""Shared historical-day planning and existing-budget resume, without new grants."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path

from stock_jp.automation import external_market as external, nightly as n
from stock_jp.automation.provider_budget import historical_debit, shared_usage, validate_charges
from stock_jp.market_data.jquants import catalog, daily, ingest


def targets(config, now, *, target=None, start=None, through=None):
    n.require(now.tzinfo is not None and now.utcoffset() is not None, "aware backfill time required")
    n.require((target is not None and start is None and through is None)
              or (target is None and start is not None and through is not None),
              "provide one target date or an inclusive date range")
    first, last = (target, target) if target is not None else (start, through)
    n.require(type(first) is date and type(last) is date and first <= last, "invalid backfill date range")
    n.require((last - first).days < 31, "backfill selection exceeds 31 calendar days")
    n.require(last < n.scheduled_day(now), "backfill must precede the current logical day")
    source = config["jquants"]
    n.require(source["enabled"] is True and source["plan"] in {"light", "standard"}
              and source["addons"] == [] and source["base_url"] == catalog.BASE_URL,
              "backfill source contract mismatch")
    for name in daily.BASE_NAMES if source["plan"] == "light" else daily.NAMES:
        item = catalog.BY_NAME[name]
        if item.query == "recent":
            # A current-only feed cannot be re-fetched as historical evidence.
            # Existing receipts may be verified; plan() refuses missing ones.
            continue
        low, high = catalog.period(item, source["plan"], now.astimezone(daily.JST).date())
        n.require(catalog.entitlement(item, source["plan"]) and low <= first <= last <= high,
                  "backfill date outside configured dataset contract: " + name)
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def plan(root, document, days):
    """Journal claims are not proof of archive completeness; execution revalidates."""
    validate_charges(document)
    rows = []
    config = (root / "config/sources.json").read_bytes()
    for day in days:
        key = day.isoformat()
        state = document["runs"].get(key)
        if state is None:
            # Historical debits, including zero, are NOT authority to mint a run.
            historical_debit(root, key)
            rows.append({"date": key, "action": "BUDGET_ALLOCATION_REQUIRED", "remaining": None})
            continue
        daily.validate_state(state)
        n.require(state["run_date"] == key and state["config_sha256"] == ingest.digest(config),
                  "backfill journal date or source configuration differs")
        basis = document.get("provider_debit_bases", {}).get(key)
        n.require(basis is not None, "existing backfill run lacks a verified debit basis")
        n.require(all(basis[k] == v for k, v in historical_debit(root, key).items()),
                  "historical debit changed; backfill cannot rebind it")
        used = shared_usage(document, key)
        action = "VERIFY_COMPLETE" if state["status"] in {"COLLECTED", "HOLIDAY"} else "RESUME"
        if action == "RESUME" and (used >= state["budget"] or state["status"] in daily.TERMINAL):
            action = "OPERATOR_RECONCILIATION_REQUIRED"
        if action == "RESUME" and any(
                name + ":" + key not in state["tasks"] for name in daily.NAMES
                if catalog.BY_NAME[name].query == "recent"):
            action = "SOURCE_SCOPE_REQUIRED"
        rows.append({"date": key, "action": action, "requests_used": used,
                     "remaining": state["budget"] - used, "recorded_status": state["status"]})
    blocked = any(row["action"] in {"BUDGET_ALLOCATION_REQUIRED", "OPERATOR_RECONCILIATION_REQUIRED", "SOURCE_SCOPE_REQUIRED"}
                  for row in rows)
    return {"status": "BLOCKED" if blocked else "PLAN_READY", "targets": rows,
            "budget_policy": "EXISTING_RUNS_ONLY_NO_NEW_ALLOWANCE",
            "analysis_readiness": "NOT_EVALUATED", "provider_calls": 0}


def execute(root, request, now, days, *, context=None, identity=None, plan_only=False,
            transport=None, sleep=None):
    root = Path(root).resolve()
    n.require((context is None) != (identity is None), "choose one backfill writer mode")
    n.require(bool(days), "empty backfill selection")
    # Revalidate public-library callers as well as CLI arguments.
    selected = targets(ingest.read_json(root / "config/sources.json"), now,
                       start=days[0], through=days[-1])
    n.require(days == selected, "backfill targets must be contiguous, unique and oldest first")
    git = n.Git(request)
    main = git.ref("main")
    if context is not None:
        n.trusted_context(root, context, False)
        n.require(context["event"] == "workflow_dispatch" and context["sha"] == main,
                  "backfill requires an explicit current-main Actions run")
        cp = n.Checkpoint(root, git, main, days[0], context)
    else:
        external.checkout_main(root, main)
        external.validate_identity(identity)
        n.require(identity["main"] == main, "backfill session main mismatch")
        cp = external.Checkpoint(root, git, main, identity,
                                 verify=lambda: external.checkout_main(root, main))
    n.require(cp.revision is not None, "backfill requires the existing durable checkpoint")
    report = plan(root, cp.document, days)
    if plan_only or report["status"] == "BLOCKED":
        return report
    policy = ingest.read_json(root / "config/policy.json")
    n.require(policy["operation_mode"] == "PAPER" and policy["broker_submission"] == "HUMAN_ONLY",
              "backfill requires PAPER / HUMAN_ONLY")
    n.require(ingest.read_json(root / "config/sources.json")["market_data_git"]["automatic_upload"] is True,
              "automatic market-data publication is disabled")
    git.private_metadata()
    # This feature never creates a budget, including during mode switches.
    if identity is not None:
        cp.claim()
    else:
        cp.before_provider_io()
    results = []
    for day in days:
        cp.target_day = day
        result = daily.run(root, at=now, remote=cp, transport=transport, sleep=sleep)
        result = {k: v for k, v in result.items() if k not in {"remote_saved", "readiness"}}
        result.update(requests_for_date=shared_usage(cp.document, day.isoformat()),
                      checkpoint_saved=True, analysis_readiness="NOT_EVALUATED")
        results.append(result)
        if result["status"] not in {"COLLECTED", "HOLIDAY"}:
            raise n.Incomplete({**result, "backfill_results": results})
    # Publication uses main as parent and the existing strict data-only whitelist.
    if identity is not None:
        cp.verify = lambda: external.checkout_main(root, main, publication=True)
    publication = n.publish(root, cp, results[-1], pr_only=True)
    if identity is not None:
        cp.release()
    return {**publication, "backfill_results": results, "budget_policy": report["budget_policy"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--mode", choices=("external", "actions"), default="external")
    parser.add_argument("--session")
    parser.add_argument("--target-date", type=date.fromisoformat)
    parser.add_argument("--from-date", type=date.fromisoformat)
    parser.add_argument("--through-date", type=date.fromisoformat)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    root, now = args.root.resolve(), datetime.now(daily.JST)
    try:
        days = targets(ingest.read_json(root / "config/sources.json"), now, target=args.target_date,
                       start=args.from_date, through=args.through_date)
        if args.mode == "actions":
            context = {key: os.environ.get(variable, "") for key, variable in {
                "repository": "GITHUB_REPOSITORY", "event": "GITHUB_EVENT_NAME", "ref": "GITHUB_REF",
                "sha": "GITHUB_SHA", "actor": "GITHUB_ACTOR", "triggering_actor": "GITHUB_TRIGGERING_ACTOR",
            }.items()}
            context["run_id"] = int(os.environ.get("GITHUB_RUN_ID", "0"))
            request = n.rate.BudgetedTransport(external.Transport(os.environ.get("GITHUB_TOKEN")))
            report = execute(root, request, now, days, context=context, plan_only=args.plan_only)
        else:
            n.require(args.session is not None, "external backfill session name required")
            request = n.rate.BudgetedTransport(external.Transport(os.environ.get("GH_TOKEN")))
            main_sha = n.Git(request).ref("main")
            external.checkout_main(root, main_sha)
            with external.session(root, args.session, main_sha, now) as identity:
                report = execute(root, request, now, days, identity=identity, plan_only=args.plan_only)
        code = 2 if report["status"] == "BLOCKED" else 0
    except n.Incomplete as error:
        report, code = error.result, 2
    except (n.RemoteStop, ingest.IngestionError, n.storage.ArchiveError) as error:
        report, code = {"status": "BLOCKED", "reason": str(error)}, 2
    except Exception:
        report, code = {"status": "BLOCKED", "reason": "backfill validation failed; preserve saved state"}, 2
    ingest.atomic_json(root / ".work/market-backfill.json", report)
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
