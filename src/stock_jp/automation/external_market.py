"""One owner-authorized external market collector; durable exclusion, PR only.

Production execution requires the repair on current main so every current writer
understands the fence. This does not revive the retired cooperative runner.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

from stock_jp.automation import nightly as n
from stock_jp.automation.provider_budget import shared_usage
from stock_jp.market_data.jquants import daily, ingest


def reject_external_writer(git):
    revision = git.ref(n.CHECKPOINT, optional=True)
    if revision is not None:
        tree = git.tree(revision)
        n.require(n.JOURNAL in tree, "shared checkpoint missing")
        document = json.loads(git.read(tree[n.JOURNAL]))
        n.require("external_writer" not in document, "external market-data writer owns checkpoint")


def no_actions_writer(git):
    # A queued/waiting run can become active while we are collecting. The
    # durable fence stops new current-main writers; this check drains old ones.
    for status in ("queued", "in_progress", "waiting", "pending", "requested"):
        result = git.api("GET", "/actions/runs?status=" + status + "&per_page=1")
        n.require(result.get("total_count") == 0 and result.get("workflow_runs") == [],
                  "Actions run pending or active; external writer cannot start or continue")


class Transport(n.http.GitHubTransport):
    """Keep dispatch, approval, merge and unrelated checkpoint writes unreachable."""
    def __call__(self, method, path, body=None):
        if method != "GET":
            allowed = {n.PREFIX + item for item in
                       ("/git/blobs", "/git/trees", "/git/commits", "/git/refs", "/pulls")}
            n.require(method == "POST" and path in allowed
                      or method == "PATCH" and path == n.PREFIX + "/git/refs/heads/" + n.CHECKPOINT,
                      "external writer forbids this GitHub mutation")
            if path.endswith("/git/refs"):
                ref = (body or {}).get("ref", "")
                n.require(re.fullmatch(r"refs/heads/daily/market/\d{4}-\d{2}-\d{2}", ref),
                          "external writer may only create a daily data branch")
        return super().__call__(method, path, body)


def checkout_main(root, main, *, publication=False):
    # No network, replace objects, hooks, or lazy promisor fetches in this check.
    env = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
           "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": ""}
    def git(*args):
        result = subprocess.run(["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", *args],
                                env=env, capture_output=True)
        n.require(result.returncode == 0, "external checkout validation failed")
        return result.stdout
    n.require(git("rev-parse", "HEAD").decode().strip() == main,
              "external repair must be adopted on current main before provider I/O")
    changed = git("diff", "--name-only", "-z", "HEAD").split(b"\0")
    changed += git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    for raw in filter(None, changed):
        path = raw.decode()
        n.require(publication and (path.startswith("data/market/jquants/") or n.observations.publication_path(path)),
                  "external collector code, configuration or operating state differs from main")
    n.require(Path(__file__).resolve() == root / "src/stock_jp/automation/external_market.py",
              "external code must load from the pinned checkout")


def validate_identity(identity):
    n.require(isinstance(identity, dict) and set(identity) == {
        "kind", "session", "main", "date", "resolved_at"}
        and identity["kind"] == "external-market-v1"
        and re.fullmatch(r"[0-9a-f]{32}", identity["session"] or ""), "invalid external identity")
    n.sha(identity["main"])
    n.require(n.scheduled_day(daily.aware(identity["resolved_at"])).isoformat() == identity["date"],
              "external logical date binding changed")


@contextmanager
def session(root, name, main, now):
    n.require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name), "invalid session name")
    directory = root / ".work/external-market"
    directory.mkdir(parents=True, exist_ok=True)
    n.require(not directory.is_symlink() and directory.resolve().is_relative_to(root),
              "unsafe external session directory")
    # All session names in one checkout share a process lock. Other checkouts
    # receive different UUIDs and lose the remote checkpoint CAS claim.
    descriptor = os.open(directory / "writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise n.RemoteStop("another local external writer is active") from None
        path = directory / (name + ".json")
        n.require(not path.is_symlink(), "unsafe external session receipt")
        if path.exists():
            identity = ingest.read_json(path)
        else:
            identity = {"kind": "external-market-v1", "session": uuid.uuid4().hex, "main": main,
                        "date": n.scheduled_day(now).isoformat(), "resolved_at": now.isoformat()}
            ingest.atomic_json(path, identity)
        validate_identity(identity)
        n.require(identity["main"] == main, "session main changed; reconcile the saved writer before resuming")
        yield identity
    finally:
        os.close(descriptor)


class Checkpoint(n.Checkpoint):
    def __init__(self, root, git, main, identity, *, verify):
        validate_identity(identity)
        self.identity, self.verify = identity, verify
        super().__init__(root, git, main, date.fromisoformat(identity["date"]),
                         {"event": "external_market", "sha": main})
        n.require(self.revision is not None, "existing shared checkpoint required; cannot initialize a new budget")

    def validate_writer(self):
        n.require(self.document.get("external_writer", self.identity) == self.identity,
                  "another external session owns checkpoint; no takeover or expiry")

    def claim(self):
        self.verify()
        no_actions_writer(self.git)
        self.document["external_writer"] = dict(self.identity)
        self.flush()  # Git non-fast-forward CAS + readback precede any reservation.
        self.tree = self.git.tree(self.revision)
        self.observe()

    def observe(self):
        super().observe()
        self.verify()

    def before_reservation(self):
        n.require(self.document.get("external_writer") == self.identity, "external writer fence missing")
        no_actions_writer(self.git)
        super().before_reservation()

    def before_provider_io(self):
        self.observe()
        no_actions_writer(self.git)
        n.require(self.document.get("external_writer") == self.identity, "external writer fence missing")
        persisted = json.loads(self.git.read(self.tree[n.JOURNAL]))
        n.require(persisted.get("external_writer") == self.identity
                  and persisted["runs"][self.state["run_date"]] == self.state
                  and shared_usage(persisted, self.state["run_date"]) <= self.state["budget"],
                  "provider reservation not durably acknowledged")
        self.observe()  # Recheck after potentially paced reads, immediately before I/O.

    def release(self):
        self.observe()
        n.require(self.document.get("external_writer") == self.identity, "external writer fence missing")
        del self.document["external_writer"]
        self.flush()


def execute(root, request, identity, *, at=None, transport=None, sleep=None):
    root = Path(root).resolve()
    git = n.Git(request)
    main = git.ref("main")
    publishing = False
    verify = lambda: checkout_main(root, main, publication=publishing)
    verify()
    validate_identity(identity)
    n.require(identity["main"] == main, "external session main mismatch")
    now = at or datetime.now(daily.JST)
    n.require(date.fromisoformat(identity["date"]) <= n.scheduled_day(now), "future external target")
    policy = ingest.read_json(root / "config/policy.json")
    n.require(policy["operation_mode"] == "PAPER" and policy["broker_submission"] == "HUMAN_ONLY",
              "external market collection requires PAPER / HUMAN_ONLY")
    n.require(ingest.read_json(root / "config/sources.json")["market_data_git"]["automatic_upload"] is True,
              "automatic market-data publication is disabled")
    git.private_metadata()
    opened = git.api("GET", "/pulls?state=open&base=main&per_page=100")
    branch = "daily/market/" + identity["date"]
    n.require(len(opened) < 100 and all(not pr["head"]["ref"].startswith("daily/market/")
                                      or pr["head"]["ref"] == branch for pr in opened),
              "another data PR must be reconciled before collection")
    cp = Checkpoint(root, git, main, identity, verify=verify)
    cp.claim()
    # Any exception keeps the fence and reservation. Resume with the same local
    # receipt; never automatically expire, refund, or steal an uncertain writer.
    result = daily.run(root, at=now, remote=cp, transport=transport, sleep=sleep)
    result = {k: v for k, v in result.items() if k not in {"remote_saved", "readiness"}}
    result.update(checkpoint_saved=True, analysis_readiness="NOT_EVALUATED",
                  writer=identity, prior_requests_used=cp.old_charged,
                  requests_for_date=shared_usage(cp.document, result["run_date"]),
                  paper_requests_used=len(cp.document.get("paper_requests", {}).get(result["run_date"], [])))
    if result["status"] not in {"COLLECTED", "HOLIDAY"}:
        raise n.Incomplete(result)
    publishing = True
    report = n.publish(root, cp, result, pr_only=True)
    cp.release()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--session", required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        request = n.rate.BudgetedTransport(Transport(os.environ.get("GH_TOKEN")))
        main_sha = n.Git(request).ref("main")
        checkout_main(root, main_sha)  # No session/checkpoint write on an unmerged branch.
        with session(root, args.session, main_sha, datetime.now(daily.JST)) as identity:
            report = execute(root, request, identity)
        code = 0
    except n.Incomplete as error:
        report, code = error.result, 2
    except (n.RemoteStop, ingest.IngestionError, n.storage.ArchiveError) as error:
        report, code = {"status": "BLOCKED", "reason": str(error)}, 2
    except Exception:
        report, code = {"status": "BLOCKED", "reason": "external collection stopped; preserve session and checkpoint"}, 2
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
