"""Owner-triggered bulk collection, isolated from the nightly writer checkpoint."""
from __future__ import annotations

import argparse
import copy
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time

from stock_jp.automation import http, rate
from stock_jp.automation.daily_remote import REPO, RemoteStop, require, git_blob
from stock_jp.automation.nightly import Git, trusted_context, WORKFLOW
from stock_jp.market_data import storage
from stock_jp.market_data.jquants import financial_history as history, ingest
from stock_jp.workspace import project_root

CHECKPOINT = "automation/financial-history"
JOURNAL = "operations/acquisition/financial-history-checkpoint.json"
RAW_PREFIX = "financial-history-checkpoint/"


class Checkpoint:
    def __init__(self, root, git, context):
        self.root, self.git, self.context = root, git, context
        self.source = root / ".work/market-data"
        self.revision = git.ref(CHECKPOINT, optional=True)
        self.parent = self.revision or git.ref("main")
        self.tree = git.tree(self.parent)
        self.local = root / ".work/financial-history-remote"
        self.local.mkdir(parents=True, exist_ok=True)
        self.askpass = self.local / "askpass.py"
        self.askpass.write_text('#!/usr/bin/env python3\nimport os,sys\nprint("x-access-token" if "Username" in sys.argv[1] else os.environ["GITHUB_TOKEN"])\n')
        self.askpass.chmod(0o700)
        self.env = {**os.environ, "GIT_ASKPASS": str(self.askpass), "GIT_TERMINAL_PROMPT": "0"}
        self._git("init", "--quiet")
        self._git("config", "user.name", "github-actions[bot]")
        self._git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        self._git("config", "core.hooksPath", "/dev/null")
        self._git("config", "remote.origin.url", "https://github.com/" + REPO + ".git")
        self._git("config", "remote.origin.promisor", "true")
        self._git("config", "remote.origin.partialclonefilter", "blob:none")
        self._git("fetch", "--filter=blob:none", "--depth=1", "--no-tags", "origin", self.parent)
        self._git("reset", "--soft", "FETCH_HEAD")
        self._git("read-tree", "FETCH_HEAD")
        self.last_push = 0.0
        self.signatures = {}
        self.document = {"version": 1, "files": {}, "github_budget": None}
        if self.revision:
            require(JOURNAL in self.tree, "financial checkpoint journal missing")
            self.document = json.loads(git.read(self.tree[JOURNAL]))
            require(set(self.document) == {"version", "files", "github_budget"}
                    and self.document["version"] == 1, "invalid financial checkpoint")
            if hasattr(git.request, "restore"):
                git.request.restore(self.document["github_budget"])
            for name, entry in self.document["files"].items():
                storage.safe_name(name)
                require(name.startswith(history.PREFIX), "unexpected financial checkpoint file")
                path = RAW_PREFIX + name
                require(self.tree.get(path) == entry["blob"] and git.modes[self.revision][path] == "100644",
                        "financial checkpoint blob missing")
                raw = self._git("cat-file", "blob", entry["blob"])
                require(storage.digest(raw) == entry["sha256"], "financial checkpoint checksum mismatch")
                storage.atomic(self.source / name, raw)
                p = self.source / name
                self.signatures[name] = (p.stat().st_size, p.stat().st_mtime_ns)
        self.saved = copy.deepcopy(self.document["files"])

    def _git(self, *args):
        completed = subprocess.run(["git", "-c", "gc.auto=0", "-C", str(self.local), *args],
                                   env=self.env, capture_output=True)
        require(completed.returncode == 0, "financial checkpoint Git operation failed")
        return completed.stdout

    def push(self, files):
        require(self.git.ref(CHECKPOINT, optional=True) == self.revision,
                "financial checkpoint moved")
        for name, raw in files.items():
            require(name == JOURNAL or name.startswith(RAW_PREFIX), "unexpected financial checkpoint path")
            if name != JOURNAL:
                storage.safe_name(name.removeprefix(RAW_PREFIX))
            storage.scan(raw)
            storage.atomic(self.local / name, raw)
        self._git("add", "--", *sorted(files))
        self._git("commit", "--quiet", "-m", "Save financial-history checkpoint")
        # One atomic, non-forced Git push per checkpoint; avoid four REST writes
        # for every provider receipt. Keep writes at least 10.1 seconds apart.
        time.sleep(max(0, self.last_push + 10.1 - time.monotonic()))
        self._git("push", "--quiet", "origin", "HEAD:refs/heads/" + CHECKPOINT)
        self.last_push = time.monotonic()
        head = self._git("rev-parse", "HEAD").decode().strip()
        require(self.git.ref(CHECKPOINT) == head, "financial checkpoint acknowledgement missing")
        return head

    def active_run(self):
        run = self.git.api("GET", "/actions/runs/" + str(self.context["run_id"]))
        require(run["status"] == "in_progress" and run["head_sha"] == self.context["sha"]
                and run["path"] == WORKFLOW, "financial collection Actions run is inactive")

    def before_request(self):
        self.active_run()
        if hasattr(self.git.request, "before_provider"):
            self.git.request.before_provider()

    def sync(self):
        files = {}
        for path in (self.source / history.PREFIX).rglob("*"):
            if not path.is_file():
                continue
            name = path.relative_to(self.source).as_posix()
            storage.safe_name(name)
            signature = (path.stat().st_size, path.stat().st_mtime_ns)
            if self.signatures.get(name) == signature:
                continue
            raw = path.read_bytes()
            require(len(raw) <= storage.MAX_SHARD, "financial checkpoint file exceeds the shard limit")
            entry = {"sha256": storage.digest(raw), "blob": git_blob(raw)}
            if self.document["files"].get(name) != entry:
                self.document["files"][name] = entry
                files[RAW_PREFIX + name] = raw
            self.signatures[name] = signature
        if self.saved == self.document["files"]:
            return
        self.active_run()
        self.git.private_metadata()
        if hasattr(self.git.request, "state"):
            self.document["github_budget"] = self.git.request.state()
        files[JOURNAL] = ingest.encoded(self.document)
        self.revision = self.push(files)
        self.tree.update({name: git_blob(raw) for name, raw in files.items()})
        self.saved = copy.deepcopy(self.document["files"])


def execute(root, context, request, as_of, *, transport=None, sleep=None):
    trusted_context(root, context, True)
    require(context["event"] == "workflow_dispatch", "financial history is owner-triggered only")
    git = Git(request)
    git.private_metadata()
    checkpoint = Checkpoint(root, git, context)
    state = history.collect(root, as_of, remote=checkpoint, transport=transport, sleep=sleep)
    return {"status": state["status"], "from": state["from"], "as_of": state["as_of"],
            "requests_used": len(state["requests"]), "budget": state["budget"],
            "inventory_file_count": sum(map(len, state["inventories"].values())),
            "downloaded_file_count": len(state["files"]),
            "rows": sum(item["rows"] or 0 for item in state["files"].values()),
            "stored_bytes": sum(item["bytes"] for item in state["files"].values()),
            "download_hosts": state["observed_download_hosts"], "checkpoint_saved": True,
            "adopted_on_main": False, "analysis_readiness": "NOT_EVALUATED"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    context = {key: os.environ.get(variable, "") for key, variable in {
        "repository": "GITHUB_REPOSITORY", "event": "GITHUB_EVENT_NAME", "ref": "GITHUB_REF",
        "sha": "GITHUB_SHA", "actor": "GITHUB_ACTOR", "triggering_actor": "GITHUB_TRIGGERING_ACTOR",
    }.items()}
    try:
        context["run_id"] = int(os.environ.get("GITHUB_RUN_ID", "0"))
        request = rate.BudgetedTransport(http.GitHubTransport(os.environ.get("GITHUB_TOKEN")))
        result = execute(project_root(), context, request, args.as_of)
        code = 0 if result["status"] in {"COMPLETE", "INSPECTED"} else 2
    except (RemoteStop, ingest.IngestionError, history.delivery.DeliveryError) as error:
        result = {"status": "BLOCKED", "reason": str(error), "analysis_readiness": "NOT_EVALUATED"}
        code = 2
    except Exception:
        result = {"status": "BLOCKED", "reason": "financial history collection incomplete; resume the saved checkpoint",
                  "analysis_readiness": "NOT_EVALUATED"}
        code = 2
    report = project_root() / ".work/financial-history.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
