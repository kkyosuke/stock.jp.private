"""Select code tests or validate a data-only change, without provider I/O."""
from __future__ import annotations

import argparse
import csv
from datetime import date
import json
import os
from pathlib import Path
import re
import subprocess

from stock_jp.automation import pr_validation as publication
from stock_jp.market_data import observations, storage
from stock_jp.market_data.jquants import daily
from stock_jp.workspace import project_root

ARCHIVE = "data/market/jquants/"
OBSERVATIONS = "data/market/observations/"
RECORDS = "operations/acquisition/nightly/"
DATA_PREFIXES = (ARCHIVE, OBSERVATIONS, RECORDS)
require = publication.require


def plan(root, base, head):
    # Compare the complete Git diff, including deleted and renamed source paths.
    # Branch names and GitHub's truncated PR-file lists cannot establish scope.
    if not re.fullmatch(r"[0-9a-f]{40}", base or "") or base == "0" * 40:
        return {"mode": "full", "reason": "no comparison base"}
    try:
        if publication.git(root, "merge-base", base, head).decode().strip() != base:
            return {"mode": "full", "reason": "base is not an ancestor"}
        paths = publication.git(root, "diff", "--no-renames", "--name-only", "-z", base, head).split(b"\0")
        paths = [path.decode() for path in paths if path]
    except (subprocess.CalledProcessError, UnicodeError):
        return {"mode": "full", "reason": "comparison unavailable"}
    data_only = bool(paths) and all(path.startswith(DATA_PREFIXES) for path in paths)
    return {"mode": "data" if data_only else "full", "changed_files": len(paths)}


def validate_record(root, path, store, pointer, manifest, base, status):
    day = Path(path).stem
    require(date.fromisoformat(day).isoformat() == day, "invalid acquisition record date")
    record = publication.strict_metadata((root / path).read_bytes())
    journal = "jquants/runs/" + day.replace("-", "") + "T120000Z.json"
    state = publication.strict_metadata(storage.member_bytes(store, pointer, manifest["files"][journal]))
    daily.validate_state(state)
    expected = {"version": 1, "analysis_readiness": "NOT_EVALUATED",
                **{key: state[key] for key in ("run_date", "status", "data_date", "requests", "tasks",
                                              "budget", "config_sha256")}}
    require(record == expected and state["run_date"] == day, "acquisition record differs from archived journal")
    if status == "M":
        previous = publication.read_json(root, base, path)
        old_requests = previous["requests"]
        require(record["requests"][:len(old_requests)] == old_requests, "acquisition request history was rewritten")


def validate_observations(root, names):
    for name in sorted(names):
        fields, identity = observations.TABLES[name]
        history = observations.load_history(root, name)
        ordered = sorted(history.values(), key=lambda row: observations.row_order(row, identity))
        latest = {row["code"]: row for row in ordered}
        expected = ordered if name == "corporate-actions.csv" else sorted(
            latest.values(), key=lambda row: observations.row_order(row, identity))
        path = root / OBSERVATIONS / Path(name).stem / "latest.csv"
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            require(tuple(reader.fieldnames or ()) == tuple(fields), "observation latest CSV schema differs")
            require(list(reader) == expected, "observation latest view differs from its history")


def validate_data(root, base, head):
    require(plan(root, base, head)["mode"] == "data", "data validation requires a data-only comparison")
    entries = publication.changes(root, base, head)
    archive_entries, record_entries, tables = [], [], set()
    for entry in entries:
        path, status = entry["path"], entry["status"]
        if path.startswith(ARCHIVE):
            require(storage.store_member(path.removeprefix(ARCHIVE)), "unexpected archive path")
            require(path == ARCHIVE + "current.json" or status == "A", "immutable archive member changed")
            archive_entries.append(entry)
        elif re.fullmatch(RECORDS + r"[0-9]{4}-[0-9]{2}-[0-9]{2}\.json", path):
            record_entries.append(entry)
        else:
            require(observations.publication_path(path), "unexpected data publication path")
            tables.add(Path(path).parent.name + ".csv")
        require((root / path).is_file() and not (root / path).is_symlink(), "missing or unsafe publication file")
        require((root / path).stat().st_size <= storage.MAX_SHARD, "publication file exceeds shard limit")
        if not path.endswith(".gz"):
            storage.scan((root / path).read_bytes())
    if archive_entries:
        require(any(entry["path"] == ARCHIVE + "current.json" for entry in archive_entries),
                "changed archive requires a generation pointer")
    store = root / ARCHIVE
    publication.validate_added_archive_members(store, archive_entries)
    report = storage.verify(store)
    pointer = publication.strict_metadata((store / "current.json").read_bytes())
    manifest = publication.strict_metadata(storage.manifest_bytes(store, pointer))
    for entry in record_entries:
        validate_record(root, entry["path"], store, pointer, manifest, base, entry["status"])
    validate_observations(root, tables)
    return {"mode": "data", "changed_files": len(entries), **report,
            "acquisition_records": len(record_entries), "observation_tables": len(tables),
            "runtime_readiness": "NOT_EVALUATED"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "validate"))
    parser.add_argument("--base", default="")
    args = parser.parse_args()
    root = project_root()
    head = publication.git(root, "rev-parse", "HEAD").decode().strip()
    try:
        report = plan(root, args.base, head) if args.command == "plan" else validate_data(root, args.base, head)
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError):
        print(json.dumps({"valid": False, "reason": "data scope, integrity or schema validation failed"}))
        return 1
    print(json.dumps(report))
    if args.command == "plan" and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write("mode=" + report["mode"] + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
