"""Preserve an unreviewed PAPER source inbox in private Git; reuse only query pages."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path

from stock_jp.automation import http, nightly as n, rate
from stock_jp.market_data import storage
from stock_jp.market_data.jquants import cache_archive, ingest
from stock_jp.market_data.jquants.cache import cache_identity
from stock_jp.market_data.primary_sources import confined, JST
from stock_jp.workspace import project_root


def allowed(name, day):
    return (name in {"operations/state.json", "operations/run-history.csv"}
            or name.startswith(f"operations/runs/{day}/")
            or name.startswith("data/market/primary-sources/")
            or name.startswith("research/historical-replay/v04-forward/")
            or cache_archive.valid_name(name))


def observe(git, context, main, revision):
    run = git.api("GET", "/actions/runs/" + str(context["run_id"]))
    n.require(run.get("status") == "in_progress" and run.get("head_sha") == context["sha"]
              and run.get("path") == n.WORKFLOW, "PAPER checkpoint requires the active owner workflow")
    n.require(git.ref("main") == main and git.ref(n.PAPER_CHECKPOINT, optional=True) == revision,
              "main or PAPER source checkpoint moved")


def run(root, context, git, *, save, preview=False):
    root = root.resolve()
    n.trusted_context(root, context, preview)
    git.private_metadata()
    main = git.ref("main")
    revision = git.ref(n.PAPER_CHECKPOINT, optional=True)
    tree = git.tree(revision or main)
    if preview and context["sha"] != main:
        git.tree(context["sha"])  # Reuse source blobs already present in the verified execution commit.
    now = datetime.now(JST)
    day = now.date().isoformat()
    config_raw = (root / "config/sources.json").read_bytes()
    config_sha = storage.digest(config_raw)
    previous = json.loads(git.read(tree[n.PAPER_JOURNAL])) if revision else None
    if previous:
        n.require(previous.get("version") == 1 and previous.get("operation_mode") == "PAPER"
                  and previous.get("broker_submission") == "HUMAN_ONLY", "invalid PAPER inbox")
        for name, item in previous["files"].items():
            n.require(allowed(name, previous["day"]), "unexpected PAPER inbox member")
            path = n.PAPER_RAW_PREFIX + item["sha256"] + "/" + name
            n.require(tree.get(path) == item["blob"] and git.modes[revision].get(path) == "100644",
                      "PAPER inbox member is missing")
    if not save:
        cached = {}
        resume_cutoff = None
        if previous and previous["config_sha256"] == config_sha:
            cutoff_name = f"operations/runs/{day}/source-scan-cutoff.json"
            if previous["day"] == day and cutoff_name in previous["files"]:
                item = previous["files"][cutoff_name]
                raw = git.read(item["blob"])
                n.require(len(raw) == item["bytes"] and storage.digest(raw) == item["sha256"], "PAPER cutoff checksum mismatch")
                cutoff = n.daily.aware(json.loads(raw)["cutoff_at_jst"]).astimezone(JST)
                if cutoff.date().isoformat() == day and cutoff <= now and (cutoff.hour >= 21 or preview):
                    resume_cutoff = cutoff.isoformat(timespec="seconds")
            if resume_cutoff:
                plan = json.loads(config_raw)["jquants"]["plan"]
                scope = ingest.digest(ingest.encoded(cache_identity(plan=plan, cutoff=resume_cutoff)))
                prefix = cache_archive.PREFIX + scope + "/"
                for name, item in previous["files"].items():
                    if not name.startswith(prefix):
                        continue
                    raw = git.read(item["blob"])
                    n.require(len(raw) == item["bytes"] and storage.digest(raw) == item["sha256"], "PAPER cache checksum mismatch")
                    cached[name] = raw
                cache_archive.validate(cached)
            observe(git, context, main, revision)
            for name, raw in cached.items():
                path = confined(root, ".work/market-data/" + name)
                n.require(not path.exists() or path.read_bytes() == raw, "adopted and inbox cache disagree")
                ingest.atomic_bytes(path, raw)
        return {"status": "QUERY_CACHE_RESTORED", "file_count": len(cached),
                "resume_cutoff_at_jst": resume_cutoff, "canonical_state_restored": False}
    files, uploads = {}, {}
    for directory in (f"operations/runs/{day}", "data/market/primary-sources",
                      "research/historical-replay/v04-forward", ".work/market-data/nightly-source-cache"):
        folder = confined(root, directory)
        for path in sorted(folder.rglob("*")):
            if path.is_file() and path.name != ".ingestion.lock":
                name = path.relative_to(root).as_posix().removeprefix(".work/market-data/")
                n.require(allowed(name, day) and not path.is_symlink(), "unsafe PAPER inbox path")
                raw = path.read_bytes()
                n.require(len(raw) <= storage.MAX_SHARD, "PAPER inbox file exceeds the Git limit")
                digest = storage.digest(raw)
                files[name] = {"sha256": digest, "bytes": len(raw), "blob": n.git_blob(raw)}
                uploads[n.PAPER_RAW_PREFIX + digest + "/" + name] = raw
    for name in ("operations/state.json", "operations/run-history.csv"):
        raw = confined(root, name).read_bytes()
        digest = storage.digest(raw)
        files[name] = {"sha256": digest, "bytes": len(raw), "blob": n.git_blob(raw)}
        uploads[n.PAPER_RAW_PREFIX + digest + "/" + name] = raw
    n.require(files and sum(item["bytes"] for item in files.values()) <= 256 * 1024**2,
              "PAPER inbox is empty or exceeds its bounded snapshot size")
    cache_archive.validate({name: uploads[n.PAPER_RAW_PREFIX + item["sha256"] + "/" + name]
                            for name, item in files.items() if cache_archive.valid_name(name)})
    snapshot = {"version": 1, "operation_mode": "PAPER", "broker_submission": "HUMAN_ONLY",
                "day": day, "action_run_id": context["run_id"], "base_main": main,
                "execution_sha": context["sha"],
                "captured_at_jst": datetime.now(JST).isoformat(timespec="seconds"),
                "config_sha256": config_sha, "files": files, "review_completed": False,
                "interpretation": "Durable collection inbox, not adopted inputs, a completed run, or LIVE approval."}
    raw = ingest.encoded(snapshot)
    uploads[n.PAPER_JOURNAL] = raw
    uploads[n.PAPER_RAW_PREFIX + "snapshots/" + storage.digest(raw) + ".json"] = raw
    observe(git, context, main, revision)
    child = git.write(n.PAPER_CHECKPOINT, revision or main, uploads, exists=revision is not None)
    return {"status": "UNREVIEWED_INBOX_SAVED", "checkpoint_branch": n.PAPER_CHECKPOINT,
            "commit": child, "file_count": len(files), "review_completed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("restore", "save"))
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    context = {"repository": os.environ["GITHUB_REPOSITORY"], "event": os.environ["GITHUB_EVENT_NAME"],
               "ref": os.environ["GITHUB_REF"], "sha": os.environ["GITHUB_SHA"],
               "actor": os.environ["GITHUB_ACTOR"], "triggering_actor": os.environ["GITHUB_TRIGGERING_ACTOR"],
               "run_id": int(os.environ["GITHUB_RUN_ID"])}
    root = project_root()
    result = run(root, context, n.Git(rate.BudgetedTransport(http.GitHubTransport(os.environ["GITHUB_TOKEN"]))),
                 save=args.action == "save", preview=args.preview)
    output = confined(root, str(args.output))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
