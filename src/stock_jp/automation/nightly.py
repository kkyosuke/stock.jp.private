"""Scheduled market-data collection with durable Git checkpoints and data PRs."""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import time

from stock_jp.workspace import project_root
from stock_jp.automation import http, rate, publication
from stock_jp.automation.daily_remote import REPO, RemoteStop, git_blob, require, sha
from stock_jp.market_data import storage
from stock_jp.market_data import observations
from stock_jp.market_data.jquants import daily, ingest

PREFIX = "/repos/" + REPO
CHECKPOINT = "automation/nightly-market-data"
JOURNAL = "operations/acquisition/nightly-checkpoint.json"
RAW_PREFIX = "nightly-checkpoint/"
WORKFLOW = ".github/workflows/market-data-manual.yml"
CI_WORKFLOW = ".github/workflows/ci.yml"
RECORDS = "operations/acquisition/nightly/"
CHECKS = {"format"}  # Required CI job includes tests and PAPER guardrails.
FINANCIAL_CHECKPOINT = "automation/financial-history"
FINANCIAL_JOURNAL = "operations/acquisition/financial-history-checkpoint.json"
FINANCIAL_RAW_PREFIX = "financial-history-checkpoint/"
PAPER_CHECKPOINT = "automation/paper-source-inbox"
PAPER_JOURNAL = "operations/acquisition/paper-source-inbox.json"
PAPER_RAW_PREFIX = "paper-source-inbox/"
# Bound the serialized JSON, including escaped Unicode, well below HTTP's cap.
MAX_TREE_BODY = 4 * 1024 * 1024


class Incomplete(RemoteStop):
    def __init__(self, result):
        self.result = {**result, "analysis_readiness": "NOT_EVALUATED", "checkpoint_saved": True}
        super().__init__("nightly collection incomplete: " + result["status"])


class Git:
    def __init__(self, request):
        self.request = request
        self.blobs = set()
        self.modes = {}
        self.commit_trees = {}
        self.write_permission_verified = False

    def api(self, method, path, body=None):
        return self.request(method, PREFIX + path, body)

    def ref(self, branch, *, optional=False):
        try:
            return sha(self.api("GET", "/git/ref/heads/" + branch)["object"]["sha"])
        except http.Rejected as error:
            if optional and error.status == 404:
                return None
            raise

    def tree_sha(self, commit):
        commit = sha(commit)
        if commit not in self.commit_trees:
            self.commit_trees[commit] = sha(self.api("GET", "/git/commits/" + commit)["tree"]["sha"])
        return self.commit_trees[commit]

    def tree(self, commit):
        tree_sha = self.tree_sha(commit)
        tree = self.api("GET", "/git/trees/" + sha(tree_sha) + "?recursive=1")
        require(tree.get("truncated") is False, "Git tree is truncated")
        members = [item for item in tree["tree"] if item["type"] != "tree"]
        result = {item["path"]: item["sha"] for item in members}
        self.modes[commit] = {item["path"]: item["mode"] for item in members}
        self.blobs.update(item["sha"] for item in members if item["type"] == "blob")
        return result

    def read(self, blob):
        value = self.api("GET", "/git/blobs/" + sha(blob))
        require(value.get("encoding") == "base64", "invalid Git blob encoding")
        raw = base64.b64decode(value["content"])
        require(git_blob(raw) == blob, "Git blob checksum mismatch")
        return raw

    def _write_tree(self, base, files):
        """Batch UTF-8 contents; binary/large objects still use verified blobs."""
        entries = []
        expected = {}
        # SHA length is fixed, so this overhead also applies to later batches.
        overhead = len(json.dumps({"base_tree": base, "tree": []}, separators=(",", ":")).encode())
        size = overhead
        for path, raw in sorted(files.items()):
            blob = git_blob(raw)
            expected[path] = blob
            entry = {"path": path, "mode": "100644", "type": "blob", "sha": blob}
            if blob not in self.blobs:
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    content = None
                inline = {"path": path, "mode": "100644", "type": "blob", "content": content}
                if (content is not None and "\0" not in content
                        and overhead + len(json.dumps(inline, separators=(",", ":")).encode()) <= MAX_TREE_BODY):
                    entry = inline
                else:
                    value = self.api("POST", "/git/blobs", {
                        "encoding": "base64", "content": base64.b64encode(raw).decode(),
                    })
                    require(value.get("sha") == blob, "uploaded blob checksum mismatch")
                    self.blobs.add(blob)
            entry_size = len(json.dumps(entry, separators=(",", ":")).encode())
            require(overhead + entry_size <= MAX_TREE_BODY, "Git tree entry exceeds limit")
            if entries and size + 1 + entry_size > MAX_TREE_BODY:
                base = sha(self.api("POST", "/git/trees", {"base_tree": base, "tree": entries})["sha"])
                entries, size = [], overhead
            size += entry_size + bool(entries)
            entries.append(entry)
        tree = sha(self.api("POST", "/git/trees", {"base_tree": base, "tree": entries})["sha"])
        # Verify the created blobs before publishing a commit/ref or caching their
        # identities. A failed batch leaves only unreferenced immutable objects.
        result = self.api("GET", "/git/trees/" + tree + "?recursive=1")
        require(result.get("truncated") is False, "uploaded Git tree is truncated")
        actual = {item["path"]: item for item in result["tree"]}
        require(all(path in actual and actual[path].get("sha") == blob
                    and actual[path].get("mode") == "100644" and actual[path].get("type") == "blob"
                    for path, blob in expected.items()), "uploaded tree checksum mismatch")
        self.blobs.update(expected.values())
        return tree

    def write(self, branch, parent, files, *, exists):
        checkpoints = {CHECKPOINT: (JOURNAL, RAW_PREFIX),
                       FINANCIAL_CHECKPOINT: (FINANCIAL_JOURNAL, FINANCIAL_RAW_PREFIX),
                       PAPER_CHECKPOINT: (PAPER_JOURNAL, PAPER_RAW_PREFIX)}
        require(branch in checkpoints or re.fullmatch(r"daily/market/\d{4}-\d{2}-\d{2}(?:-preview)?", branch),
                "unexpected publication branch")
        if exists:
            require(self.ref(branch) == parent, "checkpoint or data branch moved")
        tree = self.tree_sha(parent)
        for path, raw in sorted(files.items()):
            if branch in checkpoints:
                journal, prefix = checkpoints[branch]
                require(path == journal or path.startswith(prefix), "unexpected checkpoint path")
                if path != journal:
                    member = path.removeprefix(prefix)
                    if branch == PAPER_CHECKPOINT:
                        parsed = PurePosixPath(member)
                        require(not parsed.is_absolute() and parsed.as_posix() == member
                                and "\\" not in member and all(not part.startswith(".") for part in parsed.parts)
                                and (re.fullmatch(r"snapshots/[0-9a-f]{64}\.json", member)
                                     or len(parsed.parts) >= 3 and re.fullmatch(r"[0-9a-f]{64}", parsed.parts[0])
                                     and parsed.parts[1] in {"operations", "data", "research", "nightly-source-cache"}),
                                "unsafe PAPER checkpoint member")
                    else:
                        storage.safe_name(member)
            else:
                require(path == "data/market/jquants/current.json" or re.fullmatch(
                    r"data/market/jquants/(?:objects|generations)/[0-9a-f]{2}/[0-9a-f]{64}\.(?:json|csv|bin|chunk)", path)
                    or re.fullmatch(RECORDS + r"\d{4}-\d{2}-\d{2}\.json", path)
                    or observations.publication_path(path),
                    "unexpected data publication path")
            storage.scan(raw)
        tree = self._write_tree(tree, files)
        child = sha(self.api("POST", "/git/commits", {
            "message": "Save nightly market data", "tree": tree, "parents": [parent],
        })["sha"])
        self.commit_trees[child] = tree
        if exists:
            self.api("PATCH", "/git/refs/heads/" + branch, {"sha": child, "force": False})
        else:
            self.api("POST", "/git/refs", {"ref": "refs/heads/" + branch, "sha": child})
        require(self.ref(branch) == child, "Git write acknowledgement missing")
        return child

    def private_metadata(self):
        value = self.api("GET", "")
        require(value.get("private") is True and value["owner"]["login"].lower() == "kkyosuke",
                "private owner repository required")
        # Installation tokens (including GITHUB_TOKEN) omit user permissions.
        # Exercise Contents write before provider I/O instead of inventing a flag.
        if value.get("permissions", {}).get("push") is not True and not self.write_permission_verified:
            raw = ingest.encoded({"version": 1, "purpose": "market-data contents-write preflight"})
            blob = self.api("POST", "/git/blobs", {
                "encoding": "base64", "content": base64.b64encode(raw).decode(),
            })
            require(blob.get("sha") == git_blob(raw), "Contents write permission unverified")
            self.blobs.add(blob["sha"])
            self.write_permission_verified = True
        return {"repository_full_name": REPO, "visibility": "private",
                "owner": {"login": "kkyosuke"}, "permissions": {"push": True},
                "checked_at": datetime.now(timezone.utc).isoformat()}

def scheduled_day(at):
    """Latest elapsed 21:00 JST boundary, not GitHub's nominal cron date."""
    require(at.tzinfo is not None and at.utcoffset() is not None, "aware timestamp required")
    local = at.astimezone(daily.JST)
    return local.date() - timedelta(days=int(local.hour < 21))


class Checkpoint:
    profile = "daily"

    def __init__(self, root, git, main, target, context, *, preview=False):
        self.root, self.git, self.main = root, git, main
        self.target_day, self.context, self.preview = target, context, preview
        self.revision = git.ref(CHECKPOINT, optional=True)
        self.tree = git.tree(self.revision or main)
        if self.revision:
            require(JOURNAL in self.tree and git.modes[self.revision][JOURNAL] == "100644",
                    "nightly checkpoint journal missing or not a regular file")
            self.document = json.loads(git.read(self.tree[JOURNAL]))
            require(self.document.get("version") == 1
                    and {"version", "runs", "files", "github_budget"} <= set(self.document)
                    and set(self.document) <= {"version", "runs", "files", "github_budget", "scheduled_targets", "paper_requests", "provider_debit_bases", "external_writer"},
                    "invalid nightly checkpoint")
            for day, state in self.document["runs"].items():
                daily.validate_state(state)
                require(day == state["run_date"], "checkpoint run date mismatch")
            if hasattr(git.request, "restore"):
                git.request.restore(self.document["github_budget"])
        else:
            require(not any(p.startswith(RECORDS) for p in self.tree),
                    "published nightly records exist but checkpoint was deleted")
            self.document = {"version": 1, "runs": {}, "files": {}, "github_budget": None}
        self.validate_writer()
        from stock_jp.automation.provider_budget import validate_charges
        validate_charges(self.document)
        bindings = self.document.get("scheduled_targets", {})
        require(isinstance(bindings, dict), "invalid scheduled targets")
        for run_id, binding in bindings.items():
            require(re.fullmatch(r"[1-9][0-9]*", run_id) is not None
                    and isinstance(binding, dict) and {"date", "resolved_at"} <= set(binding) <= {"date", "resolved_at", "paper_cutoff_at_jst"},
                    "invalid scheduled target identity")
            require(scheduled_day(daily.aware(binding["resolved_at"])).isoformat() == binding["date"],
                    "invalid scheduled target date")
            if "paper_cutoff_at_jst" in binding:
                require(scheduled_day(daily.aware(binding["paper_cutoff_at_jst"])).isoformat() == binding["date"],
                        "invalid PAPER target cutoff")
        self.saved = ingest.encoded({k: v for k, v in self.document.items() if k != "github_budget"})
        self.pending_bytes = {}
        self.signatures = {}

    def validate_writer(self):
        require("external_writer" not in self.document, "external market-data writer owns checkpoint")

    def bind_schedule(self, now):
        # Persist before preparing a collector: even a failed preparation must
        # not move this Actions run to a different logical date on its retry.
        key = str(self.context["run_id"])
        bindings = self.document.setdefault("scheduled_targets", {})
        if key not in bindings:
            bindings[key] = {"date": scheduled_day(now).isoformat(), "resolved_at": now.isoformat()}
            self.flush()
            self.tree = self.git.tree(self.revision)
        self.target_day = date.fromisoformat(bindings[key]["date"])
        require(self.target_day <= scheduled_day(now), "scheduled target is ahead of publication window")

    def observe(self):
        require(self.git.ref("main") == self.main, "main moved; resume against current main")
        if self.revision:
            require(self.git.ref(CHECKPOINT) == self.revision, "another market-data writer is active")

    def prepare(self, source):
        self.source = source
        if not source.exists():
            storage.restore(self.root / "data/market/jquants", source)
        prices = ingest.read_json(source / "daily-prices/latest.json", {})
        latest_day = prices.get("latest_trading_date")
        recorded_complete = self.document["runs"].get(self.target_day.isoformat(), {}).get("status") in {"COLLECTED", "HOLIDAY"}
        require(not latest_day or self.target_day >= date.fromisoformat(latest_day) or recorded_complete,
                "historical backfill must use its existing campaign and budget")
        self.signatures = {p.relative_to(source).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
                           for p in source.rglob("*") if p.is_file()}
        path = daily.run_path(source / "jquants", self.target_day)
        initial = daily.read_state(path) if path.exists() else {"requests": []}
        self.baseline_requests = len(initial["requests"])
        from stock_jp.automation.provider_budget import historical_debit
        debit = historical_debit(self.root, self.target_day.isoformat())
        self.old_charged = self.baseline_requests + debit["prior_requests"] + debit["legacy_requests"]
        for name, entry in self.document["files"].items():
            storage.safe_name(name)
            member = RAW_PREFIX + name
            require(member in self.tree and self.tree[member] == entry["blob"]
                    and self.git.modes[self.revision][member] == "100644",
                    "checkpoint member missing")
            local = ingest.confined(source, name)
            if local.is_file():
                raw = local.read_bytes()
                if storage.digest(raw) == entry["sha256"] and git_blob(raw) == entry["blob"]:
                    continue  # Adopted bytes need no GitHub download on each later night.
            raw = self.git.read(entry["blob"])
            require(storage.digest(raw) == entry["sha256"], "checkpoint member checksum mismatch")
            ingest.atomic_bytes(local, raw)
        # Only an explicit owner retry may reopen an authentication failure.
        if path.exists() and self.context["event"] == "workflow_dispatch":
            state = daily.read_state(path)
            if state["status"] == "BLOCKED" and state["http_status"] in {401, 403}:
                state["status"] = "PAUSED"
                ingest.atomic_json(path, state)
                self.document["runs"][state["run_date"]] = copy.deepcopy(state)
            # An owner retry can bind an unused pre-publication placeholder to
            # current configuration. Charged or partially collected runs retain
            # their original contract. Both durable copies must prove zero I/O.
            current_sha = ingest.digest((self.root / "config/sources.json").read_bytes())
            started = daily.aware(state["started_at"])
            entry = self.document["files"].get(path.relative_to(source).as_posix())
            raw = path.read_bytes()
            unused = (state["status"] == "WAIT_PUBLICATION" and not state["requests"]
                      and not state["tasks"] and self.old_charged == 0
                      and all(state[k] is None for k in ("data_date", "error", "http_status", "retry_not_before"))
                      and started.date() == self.target_day and started.hour < 21
                      and self.document["runs"].get(state["run_date"]) == state
                      and entry and storage.digest(raw) == entry["sha256"]
                      and git_blob(raw) == entry["blob"])
            if state["config_sha256"] != current_sha and unused:
                configured_budget = ingest.read_json(self.root / "config/sources.json")["jquants"]["ingestion"]["max_requests_per_run"]
                require(state["budget"] == configured_budget, "unused placeholder budget cannot change")
                previous = copy.deepcopy(state)
                state["config_sha256"] = current_sha
                receipt = {"version": 1, "reason": "owner retry of verified unused publication placeholder",
                           "action_run_id": self.context["run_id"], "previous_state": previous,
                           "new_config_sha256": current_sha, "requests_preserved": 0}
                audit = source / "jquants/rebindings" / (state["run_date"] + "-" + current_sha + "-" + str(self.context["run_id"]) + ".json")
                require(not audit.exists() or audit.read_bytes() == ingest.encoded(receipt), "configuration audit changed")
                ingest.atomic_json(audit, receipt)
                ingest.atomic_json(path, state)
                self.document["runs"][state["run_date"]] = copy.deepcopy(state)
                self.sync(state)  # Persist the audit before any provider request.

    def can_defer(self, state):
        # Keep a legacy future WAIT journal intact, and defer only when both
        # durable copies prove that no request or result was ever recorded.
        day = date.fromisoformat(state["run_date"])
        if not (day > self.target_day and state["status"] == "WAIT_PUBLICATION"
                and not state["requests"] and not state["tasks"]
                and all(state[k] is None for k in ("data_date", "error", "http_status", "retry_not_before"))
                and daily.aware(state["started_at"]).astimezone(daily.JST).date() == day
                and daily.aware(state["started_at"]).astimezone(daily.JST).hour < 21
                and self.document["runs"].get(state["run_date"]) == state):
            return False
        path = daily.run_path(self.source / "jquants", day)
        entry = self.document["files"].get(path.relative_to(self.source).as_posix())
        if not entry or not path.is_file():
            return False
        raw = path.read_bytes()
        return (json.loads(raw) == state and storage.digest(raw) == entry["sha256"]
                and git_blob(raw) == entry["blob"])

    def attach(self, state):
        previous = self.document["runs"].get(state["run_date"])
        if previous:
            require(state == previous, "durable run and restored journal disagree")
        from stock_jp.automation.provider_budget import ensure_debit_basis
        ensure_debit_basis(self, state)
        self.state = state
        self.sync(state)

    def before_reservation(self):
        self.observe()
        from stock_jp.automation.provider_budget import shared_usage
        if shared_usage(self.document, self.state["run_date"], requests=self.state["requests"]) >= self.state["budget"]:
            raise ingest.RequestBudgetExhausted("prior and nightly requests exhaust the daily allowance")
        if hasattr(self.git.request, "before_provider"):
            self.git.request.before_provider()

    def reserve(self, state):
        self.sync(state)

    def before_provider_io(self):
        self.observe()
        run = self.git.api("GET", "/actions/runs/" + str(self.context["run_id"]))
        require(run["status"] == "in_progress" and run["head_sha"] == self.context["sha"]
                and run["path"] == WORKFLOW, "current Actions run is no longer active")

    def failed(self, status):
        pass  # daily.run persists the sanitized HTTP status in its final sync.

    def page_saved(self, state):
        self.sync(state)

    def sync(self, state, *, force=False):
        daily.validate_state(state)
        previous = self.document["runs"].get(state["run_date"])
        if previous:
            require(state["requests"][:len(previous["requests"])] == previous["requests"]
                    and state["budget"] == previous["budget"]
                    and state["config_sha256"] == previous["config_sha256"],
                    "durable requests or run configuration changed")
        self.document["runs"][state["run_date"]] = copy.deepcopy(state)
        seen = set()
        for path in self.source.rglob("*"):
            if not path.is_file():
                continue
            name = path.relative_to(self.source).as_posix()
            if path.name == ".ingestion.lock":
                continue
            require(not path.name.startswith(".pending-"), "unfinished checkpoint write")
            seen.add(name)
            signature = (path.stat().st_size, path.stat().st_mtime_ns)
            if self.signatures.get(name) == signature:
                continue
            raw = path.read_bytes()
            require(len(raw) <= storage.MAX_SHARD, "checkpoint member exceeds shard limit")
            storage.scan(raw)
            self.document["files"][name] = {"sha256": storage.digest(raw), "blob": git_blob(raw)}
            self.pending_bytes[RAW_PREFIX + name] = raw
            self.signatures[name] = signature
        for name in self.document["files"].keys() - seen:
            del self.document["files"][name]
        self.flush(force=force)

    def flush(self, *, force=False):
        encoded = ingest.encoded({k: v for k, v in self.document.items() if k != "github_budget"})
        if encoded == self.saved and not force:
            return
        self.observe()
        self.git.private_metadata()
        if hasattr(self.git.request, "state"):
            self.document["github_budget"] = self.git.request.state()
        files = {**self.pending_bytes, JOURNAL: ingest.encoded(self.document)}
        self.revision = self.git.write(CHECKPOINT, self.revision or self.main, files,
                                       exists=self.revision is not None)
        self.tree.update({path: git_blob(raw) for path, raw in files.items()})
        self.pending_bytes.clear()
        self.saved = encoded


def trusted_context(root, context, preview):
    require(context["repository"] == REPO and context["event"] in {"schedule", "workflow_dispatch"},
            "unexpected Actions context")
    require(context["actor"].lower() == "kkyosuke"
            and context["triggering_actor"].lower() == "kkyosuke", "owner Actions run required")
    require(context["ref"] == "refs/heads/main" or preview and context["event"] == "workflow_dispatch",
            "production collection requires main")
    sha(context["sha"])
    require(type(context["run_id"]) is int and context["run_id"] > 0, "Actions run identity missing")
    policy = ingest.read_json(root / "config/policy.json")
    require(policy["operation_mode"] == "PAPER" and policy["broker_submission"] == "HUMAN_ONLY",
            "market-data automation requires PAPER / HUMAN_ONLY")
    require(ingest.read_json(root / "config/sources.json")["market_data_git"]["automatic_upload"] is True,
            "automatic market-data publication is disabled")


def wait_ci(git, run_id, head, branch, *, event, timeout, sleep, monotonic):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        run = git.api("GET", "/actions/runs/" + str(run_id))
        require(run.get("id") == run_id and run.get("path") == CI_WORKFLOW
                and run.get("event") == event and run.get("head_sha") == head
                and run.get("head_branch") == branch, "nightly CI run identity or head changed")
        if run.get("status") != "completed":
            sleep(30)
            continue
        require(run.get("conclusion") == "success", "nightly data PR CI failed; data and budget remain saved")
        suite_id = run.get("check_suite_id")
        require(type(suite_id) is int and suite_id > 0, "nightly CI check suite unavailable")
        checks = publication.pages(git.request, "/check-suites/" + str(suite_id) + "/check-runs", "check_runs")
        latest = {}
        for check in checks:
            if (check["name"] in CHECKS and check.get("app", {}).get("slug") == "github-actions"
                    and check.get("head_sha") == head and check.get("check_suite", {}).get("id") == suite_id):
                if check["id"] > latest.get(check["name"], {}).get("id", 0):
                    latest[check["name"]] = check
        if set(latest) == CHECKS and all(c["status"] == "completed" for c in latest.values()):
            require(all(c["conclusion"] == "success" for c in latest.values()),
                    "nightly data PR CI failed; data and budget remain saved")
            return
        sleep(30)
    raise RemoteStop("nightly data PR CI pending; data and budget remain saved")


def approve_data_ci(git, pr, branch, head, *, timeout, sleep, monotonic):
    # The publisher has checked the complete candidate tree against its exact
    # generated data. Approve and track that PR's own CI once; it is also the
    # check suite used by branch protection.
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        runs = publication.pages(git.request, "/actions/workflows/ci.yml/runs?event=pull_request&head_sha=" + head,
                                 "workflow_runs")
        matches = [run for run in runs if run.get("path") == CI_WORKFLOW and run.get("head_branch") == branch
                   and run.get("head_sha") == head and run.get("event") == "pull_request"
                   and any(p.get("number") == pr["number"] and p.get("head", {}).get("sha") == head
                           for p in run.get("pull_requests", []))]
        if not matches:
            sleep(30)
            continue
        run = max(matches, key=lambda item: item["id"])
        require(type(run.get("id")) is int and run["id"] > 0, "CI run identity unavailable")
        if run.get("conclusion") == "action_required":
            require(run.get("actor", {}).get("login") == "github-actions[bot]", "unexpected data CI approval actor")
            git.api("POST", "/actions/runs/" + str(run["id"]) + "/approve")
        wait_ci(git, run["id"], head, branch, event="pull_request", timeout=max(0, deadline-monotonic()),
                sleep=sleep, monotonic=monotonic)
        return run["id"]
    raise RemoteStop("nightly data PR CI missing or unmatched; data and budget remain saved")



def publish(root, checkpoint, result, *, pr_only=False, timeout=300, sleep=time.sleep, monotonic=time.monotonic):
    checkpoint.observe()
    observation_report = observations.rebuild(root)
    candidate = storage.stage(root, checkpoint.git.private_metadata())
    report = {**result, "generation": candidate["generation"], "store_bytes": candidate["store_bytes"],
              "observations": observation_report,
              "analysis_readiness": "NOT_EVALUATED", "preview": checkpoint.preview}
    git = checkpoint.git
    base = git.tree(checkpoint.main)
    files = {}
    prefix = "data/market/jquants/"
    for path in (root / prefix).rglob("*"):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if relative not in base or relative == prefix + "current.json":
                raw = path.read_bytes()
                if base.get(relative) != git_blob(raw):
                    files[relative] = raw
    for day, state in checkpoint.document["runs"].items():
        record = {"version": 1, "run_date": day, "status": state["status"],
                  "data_date": state["data_date"], "requests": state["requests"],
                  "tasks": state["tasks"], "budget": state["budget"],
                  "config_sha256": state["config_sha256"], "analysis_readiness": "NOT_EVALUATED"}
        raw = ingest.encoded(record)
        name = RECORDS + day + ".json"
        if base.get(name) != git_blob(raw):
            files[name] = raw
    for member in observations.publication_files(root):
        path = member.relative_to(root).as_posix()
        raw = member.read_bytes()
        require(len(raw) <= storage.MAX_SHARD, "observation table exceeds the Git shard limit")
        if base.get(path) != git_blob(raw):
            files[path] = raw
    if not files:
        return {**report, "status": "ALREADY_ADOPTED"}
    branch = "daily/market/" + result["run_date"] + ("-preview" if checkpoint.preview else "")
    existing = git.ref(branch, optional=True)
    checkpoint.observe()
    head = existing or git.write(branch, checkpoint.main, files, exists=False)
    final_tree = git.tree(head)
    changed = {p for p in base.keys() | final_tree.keys()
               if (base.get(p), git.modes[checkpoint.main].get(p))
               != (final_tree.get(p), git.modes[head].get(p))}
    require(changed == set(files) and all(final_tree[p] == git_blob(files[p])
            and git.modes[head][p] == "100644" for p in changed),
            "candidate contains unexpected changes")
    opened = git.api("GET", "/pulls?state=open&base=main&head=kkyosuke:" + branch + "&per_page=100")
    require(len(opened) <= 1, "duplicate nightly data PRs")
    pr = opened[0] if opened else git.api("POST", "/pulls", {
        "base": "main", "head": branch, "draft": checkpoint.preview,
        "title": "市場データ更新 " + result["run_date"] + (" (preview)" if checkpoint.preview else ""),
        "body": ("公式市場データを取得・検証し、復元可能な世代に保存しました。\n\n"
                 f"状態: {result['status']}、通信: {result['requests_for_date']}/{result['budget']}。\n"
                 "取得の完了はPAPER運用の完了を意味しません。analysis_readiness=NOT_EVALUATED。\n"
                 "データ以外の変更を含まないことと、このheadの全CI成功を確認してから採用します。"),
    })
    report["pull_request"] = pr["html_url"]
    require(pr["head"]["sha"] == head and pr["state"] == "open"
            and pr["draft"] == checkpoint.preview, "nightly PR changed before CI approval")
    checkpoint.observe()
    if pr_only:
        return {**report, "status": "DATA_PR_CREATED", "data_status": result["status"],
                "head": head, "checkpoint_saved": True, "required_ci": "NOT_VERIFIED"}
    require(git.ref(branch) == head, "nightly candidate branch moved before CI approval")
    run_id = approve_data_ci(git, pr, branch, head, timeout=timeout, sleep=sleep, monotonic=monotonic)
    report["ci_run_id"] = report["pull_request_ci_run_id"] = run_id
    checkpoint.observe()
    reviews = publication.pages(git.request, "/pulls/" + str(pr["number"]) + "/reviews")
    http.reject_negative_reviews(reviews, head)
    if hasattr(git.request, "review_decision"):
        git.request.review_decision(pr["number"], head, reviews)
    git.private_metadata()
    checkpoint.sync(checkpoint.document["runs"][result["run_date"]], force=True)
    if checkpoint.preview:
        return {**report, "status": "PREVIEW_COMPLETE", "checkpoint_saved": True}
    checkpoint.observe()
    current = git.api("GET", "/pulls/" + str(pr["number"]))
    require(current["head"]["sha"] == head and current["state"] == "open" and not current["draft"],
            "nightly PR changed before merge")
    merged = git.api("PUT", "/pulls/" + str(pr["number"]) + "/merge",
                     {"sha": head, "merge_method": "squash"})
    require(merged.get("merged") is True, "nightly data PR was not adopted")
    return {**report, "status": "ADOPTED", "main_sha": merged["sha"]}


def execute(root, context, request, *, target=None, preview=False, at=None, transport=None, sleep=None):
    root = Path(root).resolve()
    trusted_context(root, context, preview)
    now = daily.aware((at or datetime.now(daily.JST)).isoformat())
    day = target or now.astimezone(daily.JST).date()
    require(day <= now.astimezone(daily.JST).date(), "future collection date forbidden")
    git = Git(request)
    git.private_metadata()
    # Exercise the publication read permissions before spending provider budget.
    git.api("GET", "/pulls?state=open&base=main&per_page=1")
    git.api("GET", "/commits/" + context["sha"] + "/check-runs?per_page=1")
    main = git.ref("main")
    require(preview or main == context["sha"], "workflow does not use current main")
    if preview and main != context["sha"]:
        git.tree(context["sha"])  # Reuse verified blobs already present in the preview commit.
    checkpoint = Checkpoint(root, git, main, day, context, preview=preview)
    if context["event"] == "schedule" and target is None:
        checkpoint.bind_schedule(now)
    result = daily.run(root, at=now, remote=checkpoint, transport=transport, sleep=sleep)
    result = {k: v for k, v in result.items() if k not in {"remote_saved", "readiness"}}
    result.update(checkpoint_saved=True, analysis_readiness="NOT_EVALUATED")
    result["prior_requests_used"] = checkpoint.old_charged
    from stock_jp.automation.provider_budget import shared_usage
    result["requests_for_date"] = shared_usage(checkpoint.document, result["run_date"])
    result["paper_requests_used"] = len(checkpoint.document.get("paper_requests", {}).get(result["run_date"], []))
    if result["status"] not in {"COLLECTED", "HOLIDAY"}:
        raise Incomplete(result)
    return publish(root, checkpoint, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-date", type=date.fromisoformat)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--report", type=Path, default=Path(".work/nightly-market-data.json"))
    args = parser.parse_args()
    context = {key: os.environ.get(variable, "") for key, variable in {
        "repository": "GITHUB_REPOSITORY", "event": "GITHUB_EVENT_NAME", "ref": "GITHUB_REF",
        "sha": "GITHUB_SHA", "actor": "GITHUB_ACTOR", "triggering_actor": "GITHUB_TRIGGERING_ACTOR",
    }.items()}
    try:
        context["run_id"] = int(os.environ.get("GITHUB_RUN_ID", "0"))
        request = rate.BudgetedTransport(http.GitHubTransport(os.environ.get("GITHUB_TOKEN")))
        result = execute(project_root(), context, request, target=args.target_date, preview=args.preview)
        code = 0
    except Incomplete as error:
        result = error.result
        code = 2
    except (RemoteStop, ingest.IngestionError, storage.ArchiveError) as error:
        result = {"status": "BLOCKED", "reason": str(error), "analysis_readiness": "NOT_EVALUATED"}
        code = 2
    except Exception:
        result = {"status": "BLOCKED", "reason": "nightly validation or publication failed",
                  "analysis_readiness": "NOT_EVALUATED"}
        code = 2
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
