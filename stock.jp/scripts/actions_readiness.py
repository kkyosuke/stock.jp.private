"""Report local setup omissions without network access or credential values.

Diagnostic only: the target driver still verifies authenticated approval,
retirement, ownership, checkpoint and budgets before acquiring data.
"""
import argparse
import json
import os
from pathlib import Path
import re


def report(root=None, environ=None):
    root = Path.cwd() if root is None else Path(root)
    env = os.environ if environ is None else environ
    blockers = []
    if env.get('STOCK_ACTIONS_ENABLED') != 'true':
        blockers.append('execution_disabled')
    if not re.fullmatch('[0-9a-f]{64}', env.get('STOCK_ACTIONS_APPROVAL_SHA256', '')):
        blockers.append('reviewed_approval_pin_missing_or_invalid')
    credentials = {name: bool(env.get(name)) for name in ('AUTOMATION_PAT', 'JQUANTS_API_KEY')}
    for name, present in credentials.items():
        if not present:
            blockers.append(name.lower() + '_missing')
    for name, reason in (
        ('.github/stock-automation-approval.json', 'approval_manifest_missing'),
        ('.github/stock-automation-retirement.json', 'retirement_receipt_missing'),
    ):
        if not (root / name).is_file():
            blockers.append(reason)
    return {
        'status': 'BLOCKED' if blockers else 'LOCAL_CONFIGURATION_PRESENT',
        'reason': blockers[0] if blockers else 'authenticated_runtime_verification_required',
        'blockers': blockers,
        'credential_presence': credentials,
        'credential_validity':'UNVERIFIED',
        'runtime_readiness': 'NOT_EVALUATED',
        'operation_mode':'PAPER',
        'broker_submission':'HUMAN_ONLY',
        'driver':'actions_target_driver.py',
        'remaining_runtime_checks':[
            'current private main and owner-initiated GitHub run',
            'pinned scope, genuine scheduler/writer retirement and owner evidence',
            'existing checkpoint ancestry, ownership and preserved request budgets',
            'unresolved or rejected campaigns and publication transactions',
            'adopted data hashes and PAPER/HUMAN_ONLY policy',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    args = parser.parse_args()
    result = report(args.root)
    print(json.dumps(result, sort_keys=True))
    return 2 if result['blockers'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
