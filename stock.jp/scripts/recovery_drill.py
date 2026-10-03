"""Generate recovery evidence from two actual checkouts, without marking gaps passed.

Prepare a separate clean GitHub clone and restore its private archive beforehand.
The output is a new reviewable evidence file, not an automatic policy promotion.
"""
import argparse
import csv
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
try:
    from scripts.live_gate_evidence import REPOSITORY_LAYOUT_REVISION, JST, evaluate_official_coverage
    from scripts.operation_state import validate_workspace
except ModuleNotFoundError:
    from live_gate_evidence import REPOSITORY_LAYOUT_REVISION, JST, evaluate_official_coverage
    from operation_state import validate_workspace

REPO='kkyosuke/stock.jp.private'


def git(root,*args):
    return subprocess.check_output(['git','-C',str(root),*args],text=True,stderr=subprocess.DEVNULL).strip()


def hashes(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and p.name!='.ingestion.lock' and 'market-data-staging' not in p.parts}


def _bootstrap(recovered,at):
    spec=importlib.util.spec_from_file_location('recovery_health',recovered/'scripts/operation_health.py')
    health=importlib.util.module_from_spec(spec);spec.loader.exec_module(health)
    return health.command_json('operation_bootstrap.py',at)


def generate(source,recovered,operator):
    started=datetime.now(JST);source=Path(source).resolve();recovered=Path(recovered).resolve()
    if source==recovered or source in recovered.parents or recovered in source.parents:raise ValueError('separate checkouts required')
    if not operator.strip():raise ValueError('actual operator identity required')
    remote='https://github.com/'+REPO+'.git'
    for root in (source,recovered):
        if git(root,'remote','get-url','origin') not in (remote,'git@github.com:'+REPO+'.git'):raise ValueError('unexpected repository')
        if git(root,'branch','--show-current')!='main' or git(root,'status','--porcelain'):raise ValueError('clean main required')
        if not (root/'.git').is_dir() or (root/'.git/objects/info/alternates').exists():raise ValueError('independent clone required')
        if (root/'stock.jp/operations/private').resolve()!=(root/'operations/private').resolve():raise ValueError('workspace private link missing')
    metadata=json.loads(subprocess.check_output(['gh','api','repos/'+REPO],text=True))
    if metadata.get('private') is not True:raise ValueError('private visibility not verified')
    main=json.loads(subprocess.check_output(['gh','api','repos/'+REPO+'/git/ref/heads/main'],text=True))['object']['sha']
    source_sha=git(source,'rev-parse','HEAD');recovered_sha=git(recovered,'rev-parse','HEAD')
    if source_sha!=main or recovered_sha!=main:raise ValueError('both checkouts must match current remote main')
    original=hashes(source/'operations/private');restored=hashes(recovered/'operations/private')
    archive_before={k:v for k,v in original.items() if k.startswith('market-data/')}
    archive_after={k:v for k,v in restored.items() if k.startswith('market-data/')}
    archive_equal=bool(archive_before) and archive_before==archive_after
    public=recovered/'stock.jp';errors=validate_workspace(public)
    bootstrap=_bootstrap(recovered,started.isoformat())
    with (recovered/'operations/private/run-history.csv').open() as stream:
        history=list(csv.DictReader(stream))
    completed=[r for r in history if r.get('status')=='COMPLETED' and r.get('operation_mode')=='PAPER']
    latest=completed[-1] if completed else None
    handoff=(recovered/'operations/private/runs'/latest['run_id']/'handoff.json') if latest else None
    handoff_ok=bool(handoff and handoff.is_file() and json.loads(handoff.read_text()).get('status')=='completed')
    coverage=evaluate_official_coverage(root=public)
    ledger_keys=[k for k in original if 'ledger' in Path(k).name]
    ledger_ok=bool(ledger_keys) and all(restored.get(k)==original[k] for k in ledger_keys)
    state_equal=restored.get('state.json')==original.get('state.json')
    checks={'clean_clone_single_repository':archive_equal,'workspace_setup':True,'state_validation':not errors,
            'paper_bootstrap_check':bootstrap.get('paper_go') is True,'latest_successful_run':bool(latest) and coverage['eligible'],
            'latest_handoff':handoff_ok,'all_ledgers':ledger_ok,'unreconciled_orders':state_equal and ledger_ok and not errors}
    if hashes(source/'operations/private')!=original or hashes(recovered/'operations/private')!=restored:raise ValueError('recovery inspection changed private files')
    return {'schema_version':'1.1','drill_id':'recovery-'+started.strftime('%Y%m%dT%H%M%S'),'operator':operator,
            'repository_visibility':'PRIVATE','reason':'REPOSITORY_LAYOUT_CHANGE','repository_layout_revision':REPOSITORY_LAYOUT_REVISION,
            'started_at_jst':started.isoformat(),'completed_at_jst':datetime.now(JST).isoformat(),
            'source_private_commit':source_sha,'recovered_private_commit':recovered_sha,
            'source_code_tree':git(source,'rev-parse','HEAD:stock.jp'),'recovered_code_tree':git(recovered,'rev-parse','HEAD:stock.jp'),
            'checks':checks,'result_notes':'Generated from actual clones. Missing PAPER/source evidence stays false; no user risk approval is inferred.'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('source','recovered','output'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--operator',required=True);a=p.parse_args()
    if a.output.exists():raise ValueError('never overwrite a prior recovery drill')
    result=generate(a.source,a.recovered,a.operator);a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'checks':result['checks'],'output':str(a.output)}))
    return 0 if all(result['checks'].values()) else 2


if __name__=='__main__':raise SystemExit(main())
