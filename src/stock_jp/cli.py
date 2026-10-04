"""One entry point for collection, operation, research and automation."""
from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import sys

from stock_jp.workspace import MARKET_STORE, MARKET_WORK, project_root

COMMANDS = {
    "data": {
        "collect": "market_data.jquants.ingest",
        "daily": "market_data.jquants.daily",
        "history": "market_data.jquants.history",
        "catalog": "market_data.jquants.ingest",
        "sample": "market_data.jquants.size_sample",
        "verify": "market_data.storage",
        "pack": "market_data.storage",
        "restore": "market_data.storage",
        "stage": "market_data.storage",
        "edinet": "market_data.edinet",
        "scan": "market_data.disclosures",
        "prices": "market_data.validate_prices",
    },
    "strategy": {
        "regime": "strategy.regime",
        "company": "strategy.company",
        "sizing": "strategy.sizing",
        "liquidity": "strategy.liquidity",
        "price-rules": "strategy.price_rules",
    },
    "operation": {
        "run": "operation.nightly",
        "status": "operation.preparation",
        "state": "operation.state",
        "policy": "operation.policy",
        "orders": "operation.orders",
        "decision": "operation.decisions",
        "assess": "operation.assessment",
        "gates": "operation.evidence",
        "limited-live": "operation.limited_live",
        "recovery": "operation.recovery",
        "readiness": "operation.readiness",
        "smoke": "operation.smoke",
    },
    "research": {
        "replay": "research.historical_replay",
        "allocation": "research.allocation",
        "episodes": "research.price_episodes",
        "price-exit": "research.price_exit",
    },
    "automation": {
        "acquire": "automation.date_driver",
        "readiness": "automation.readiness",
        "health": "automation.health",
        "validate": "automation.checkout",
        "publication": "automation.pr_validation",
    },
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="stock-jp", description=__doc__, add_help=False)
    parser.add_argument("--root", type=Path, default=project_root(), help="repository root for every command")
    args, remaining = parser.parse_known_args(argv)
    if not remaining or remaining == ["--help"] or remaining == ["-h"]:
        parser.print_help()
        print("\nCommands:")
        for group, commands in COMMANDS.items():
            print(f"  {group}: {', '.join(commands)}")
        return 0
    if len(remaining) < 2 or remaining[0] not in COMMANDS or remaining[1] not in COMMANDS[remaining[0]]:
        parser.error("select a command group and command; use --help for the list")
    group, command, *forwarded = remaining
    root = args.root.resolve()
    os.environ["STOCK_JP_ROOT"] = str(root)
    if group == "data":
        if command == "catalog":
            forwarded.insert(0, "--catalog")
        elif command in {"verify", "pack", "restore", "stage"}:
            forwarded.insert(0, command)
            if command in {"verify", "restore"} and "--store" not in forwarded:
                forwarded.extend(["--store", str(root / MARKET_STORE)])
            if command == "restore" and "--target" not in forwarded:
                forwarded.extend(["--target", str(root / MARKET_WORK)])
            if command == "pack":
                if "--source" not in forwarded:
                    forwarded.extend(["--source", str(root / MARKET_WORK)])
                if "--store" not in forwarded:
                    forwarded.extend(["--store", str(root / ".work/staging/store")])
    module = importlib.import_module("stock_jp." + COMMANDS[group][command])
    previous = sys.argv
    sys.argv = [f"stock-jp {group} {command}", *forwarded]
    try:
        result = module.main()
        return result if isinstance(result, int) else 0
    finally:
        sys.argv = previous
