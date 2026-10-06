"""Share the existing logical-day allowance across market and PAPER requests.

PAPER's urllib boundary covers the existing provider clients, including frozen
research collectors. Every HTTP attempt (also redirects and retries) reserves
durably before urllib opens the connection. Cached reads do not enter urllib.
GitHub checkpoint traffic retains its separate BudgetedTransport allowance.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
import re
import sys
from urllib.parse import urlsplit

from stock_jp.automation.daily_remote import require
from stock_jp.market_data.jquants import daily, ingest

_active = None
_installed = False


def validate_charges(document):
    charges = document.get("paper_requests", {})
    require(isinstance(charges, dict), "invalid shared provider charges")
    for day, entries in charges.items():
        require(isinstance(day, str) and date.fromisoformat(day).isoformat() == day
                and day in document["runs"] and isinstance(entries, list)
                and len(entries) + len(document["runs"][day]["requests"]) <= document["runs"][day]["budget"], "invalid provider charge day")
        for index, entry in enumerate(entries, 1):
            require(isinstance(entry, dict) and set(entry) == {
                "sequence", "host", "reserved_at", "action_run_id", "execution_sha"},
                "invalid provider charge")
            require(type(entry["sequence"]) is int and entry["sequence"] == index
                    and isinstance(entry["host"], str)
                    and re.fullmatch(r"[a-z0-9.-]+", entry["host"]) is not None
                    and type(entry["action_run_id"]) is int and entry["action_run_id"] > 0
                    and isinstance(entry["execution_sha"], str)
                    and re.fullmatch(r"[0-9a-f]{40}", entry["execution_sha"]) is not None,
                    "invalid provider charge identity")
            daily.aware(entry["reserved_at"])


class PaperBudget:
    def __init__(self, root, context, git, *, preview=False, clock=None):
        from stock_jp.automation import nightly as n
        self.clock = clock or (lambda: datetime.now(daily.JST))
        n.trusted_context(root, context, preview)
        git.private_metadata()
        now = self.clock()
        target = n.scheduled_day(now)
        self.checkpoint = n.Checkpoint(root, git, git.ref("main"), target, context, preview=preview)
        cp = self.checkpoint
        require(cp.revision is not None, "shared daily checkpoint is required before PAPER acquisition")
        cp.bind_schedule(now)  # Also pins explicit retries across the next publication boundary.
        self.day = cp.target_day.isoformat()
        require(self.day <= target.isoformat(), "provider budget day is ahead of publication")
        require(self.day in cp.document["runs"], "daily budget must exist before PAPER acquisition")
        state = cp.document["runs"][self.day]
        config = ingest.read_json(root / "config/sources.json")
        self.limit = config["jquants"]["ingestion"]["max_requests_per_run"]
        require(type(self.limit) is int and 1 <= self.limit <= 12
                and state["budget"] == self.limit, "shared daily budget differs from configured allowance")
        require(state["config_sha256"] == ingest.digest((root / "config/sources.json").read_bytes()),
                "shared budget source configuration changed")
        grants = ingest.read_json(root / "operations/acquisition/date-grants.json", {})
        self.prior = grants.get("prior_charges", {}).get(self.day, 0)
        require(type(self.prior) is int and self.prior >= 0, "invalid prior provider charges")
        # Do not assume historical PAPER traffic was free on the migration day.
        if self.day not in cp.document.get("paper_requests", {}):
            inbox = git.ref(n.PAPER_CHECKPOINT, optional=True)
            if inbox:
                tree = git.tree(inbox)
                require(n.PAPER_JOURNAL in tree, "PAPER inbox journal missing")
                import json
                previous = json.loads(git.read(tree[n.PAPER_JOURNAL]))
                captured_day = n.scheduled_day(daily.aware(previous["captured_at_jst"])).isoformat()
                require(previous["day"] != self.day and captured_day != self.day,
                        "legacy PAPER traffic has no shared charges; reconcile this day's budget before acquisition")
            cp.document.setdefault("paper_requests", {})[self.day] = []
            cp.flush()
        self.poisoned = False

    def reserve(self, url):
        cp = self.checkpoint
        require(not self.poisoned, "provider budget session stopped after uncertain reservation")
        parsed = urlsplit(url)
        require(parsed.scheme == "https" and parsed.hostname and not parsed.username
                and not parsed.password and parsed.port in {None, 443}, "unsafe provider request origin")
        # Never store the URL, headers, request body, credentials or query values.
        cp.observe()
        entries = cp.document.setdefault("paper_requests", {}).setdefault(self.day, [])
        used = max(self.prior, len(cp.document["runs"][self.day]["requests"])) + len(entries)
        if used >= self.limit:
            raise ingest.RequestBudgetExhausted("shared logical-day provider allowance exhausted")
        entries.append({"sequence": len(entries) + 1, "host": parsed.hostname,
                        "reserved_at": self.clock().isoformat(),
                        "action_run_id": cp.context["run_id"], "execution_sha": cp.context["sha"]})
        self.poisoned = True
        cp.flush()
        cp.before_provider_io()
        self.poisoned = False


def _audit(event, args):
    if event == "urllib.Request" and _active is not None:
        url = args[0]
        # Fixed-origin GitHub transport is the only control-plane exemption.
        if urlsplit(url).scheme == "https" and urlsplit(url).netloc == "api.github.com":
            return
        _active.reserve(url)


@contextmanager
def guard(budget):
    global _installed, _active
    require(_active is None, "nested provider budget session")
    if not _installed:
        sys.addaudithook(_audit)
        _installed = True
    _active = budget
    try:
        yield
    finally:
        _active = None


def main():
    import os
    from stock_jp.automation import http, nightly as n, rate
    from stock_jp.workspace import project_root
    from stock_jp.operation import paper_preparation
    from stock_jp.research import forward_collection

    require(len(sys.argv) > 1 and sys.argv[1] in {"forward", "prepare"}, "expected forward or prepare")
    action = sys.argv.pop(1)
    context = {"repository": os.environ["GITHUB_REPOSITORY"], "event": os.environ["GITHUB_EVENT_NAME"],
               "ref": os.environ["GITHUB_REF"], "sha": os.environ["GITHUB_SHA"],
               "actor": os.environ["GITHUB_ACTOR"], "triggering_actor": os.environ["GITHUB_TRIGGERING_ACTOR"],
               "run_id": int(os.environ["GITHUB_RUN_ID"])}
    git = n.Git(rate.BudgetedTransport(http.GitHubTransport(os.environ["GITHUB_TOKEN"])))
    budget = PaperBudget(project_root(), context, git, preview=os.environ.get("STOCK_PREVIEW") == "true")
    with guard(budget):
        return (forward_collection.main if action == "forward" else paper_preparation.main)()


if __name__ == "__main__":
    raise SystemExit(main())
