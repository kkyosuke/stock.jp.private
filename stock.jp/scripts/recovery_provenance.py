"""Content-addressed recovery observations, verified against an immutable Git commit."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
try:
    from scripts import market_data_git as codec
except ModuleNotFoundError:
    import market_data_git as codec

VALIDATORS=('recovery_drill.py','recovery_provenance.py','live_gate_evidence.py','run_integrity.py',
            'operation_state.py','operation_bootstrap.py','market_data_git.py')


def encoded(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()
def digest(data):return hashlib.sha256(data).hexdigest()
def git(root,*args):return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL)
def blob(root,commit,path):return git(root,'show',commit+':'+path)
def names(root,commit,prefix):return git(root,'ls-tree','--full-tree','-r','--name-only',commit,'--',prefix).decode().splitlines()
def require(ok,message):
    if not ok:raise ValueError(message)


def coverage_decision(result):
    """Compare all decision evidence, excluding only the audit execution clock.

    Coverage uses the selected run's persisted price date and source cutoff,
    not evaluated_at_jst, as its decision reference. The original audit clock
    remains in the checksummed manifest; revalidation records a new clock.
    """
    require(isinstance(result,dict),'invalid coverage result')
    return {key:value for key,value in result.items() if key!='evaluated_at_jst'}


def capture(source,recovered,selected,checks,details,*,inspection_at_jst):
    commit=git(source,'rev-parse','HEAD').decode().strip()
    paths=names(source,commit,'operations/private')
    private_hashes={p:digest(blob(source,commit,p)) for p in paths}
    require(private_hashes and 'operations/private/state.json' in private_hashes,'missing committed private state')
    for p,sha in private_hashes.items():
        require(digest((source/p).read_bytes())==sha and digest((recovered/p).read_bytes())==sha,'private files differ from inspected commit')
    store=source/'stock.jp/data/jquants-archive';pointer=json.loads((store/'current.json').read_text())
    require((store/'current.json').read_bytes()==blob(source,commit,'stock.jp/data/jquants-archive/current.json'),'archive pointer differs from commit')
    codec.verify(store)
    manifest=json.loads(codec.unpack(store,pointer['generation'],codec.MAX_INDEX,'generations'))
    archive_hashes={p:v['sha256'] for p,v in manifest['files'].items()}
    require(bool(archive_hashes),'empty recovery archive')
    for root in (source,recovered):
        actual={str(p.relative_to(root/'operations/private/market-data')):digest(p.read_bytes())
                for p in (root/'operations/private/market-data').rglob('*') if p.is_file() and p.name!='.ingestion.lock'}
        require(actual==archive_hashes,'restored archive differs from immutable generation')
    validators={f'stock.jp/scripts/{name}':digest(blob(source,commit,f'stock.jp/scripts/{name}')) for name in VALIDATORS}
    document={'version':1,'commit':commit,'code_tree':git(source,'rev-parse','HEAD:stock.jp').decode().strip(),
              'inspection_at_jst':inspection_at_jst,
              'archive_pointer':pointer,'archive_hashes':archive_hashes,'private_hashes':private_hashes,
              'validators':validators,'selected_run':selected,'checks':checks,'validation_details':details}
    return {'sha256':digest(encoded(document)),'manifest':document}


def verify(root,drill):
    root=Path(git(root,'rev-parse','--show-toplevel').decode().strip())
    proof=drill.get('provenance')
    require(isinstance(proof,dict) and set(proof)=={'sha256','manifest'},'recovery provenance manifest is required')
    m=proof['manifest'];require(isinstance(m,dict) and digest(encoded(m))==proof['sha256'],'recovery provenance checksum mismatch')
    require(set(m)=={'version','commit','code_tree','inspection_at_jst','archive_pointer','archive_hashes','private_hashes','validators','selected_run','checks','validation_details'} and m['version']==1,'invalid recovery provenance schema')
    from datetime import datetime
    inspection_at=m['inspection_at_jst']
    require(isinstance(inspection_at,str) and datetime.fromisoformat(inspection_at).utcoffset() is not None,
            'invalid recorded inspection clock')
    require(inspection_at==drill['started_at_jst'],'recorded inspection clock differs from drill start')
    commit=m['commit'];require(isinstance(commit,str) and re.fullmatch('[0-9a-f]{40}',commit),'invalid inspected commit')
    require(commit==drill['source_private_commit']==drill['recovered_private_commit'],'provenance commit mismatch')
    require(m['code_tree']==drill['source_code_tree']==drill['recovered_code_tree'],'provenance code identity mismatch')
    require(git(root,'rev-parse',commit+':stock.jp').decode().strip()==m['code_tree'],'inspected code tree mismatch')
    expected_private={p:digest(blob(root,commit,p)) for p in names(root,commit,'operations/private')}
    require(bool(expected_private) and m['private_hashes']==expected_private,'committed private artifact hashes mismatch')
    expected_validators={f'stock.jp/scripts/{name}':digest(blob(root,commit,f'stock.jp/scripts/{name}')) for name in VALIDATORS}
    require(m['validators']==expected_validators,'validator code hashes mismatch')
    require(m['checks']==drill['checks'] and all(m['checks'].values()),'provenance checks are incomplete')
    require(m['selected_run']==drill.get('selected_run') and bool(m['selected_run']),'selected run binding missing')
    # Reconstruct the inspected inputs rather than compare with today's mutable
    # ledgers. A later routine daily commit does not invalidate this drill.
    with tempfile.TemporaryDirectory(prefix='recovery-proof-') as directory:
        snapshot=Path(directory)
        selected_paths=set(expected_private)
        for prefix in ('stock.jp/scripts','stock.jp/docs','stock.jp/operations/templates','stock.jp/data'):
            selected_paths.update(p for p in names(root,commit,prefix) if not p.startswith('stock.jp/data/daily-prices/'))
        for p in selected_paths:
            require(not Path(p).is_absolute() and '..' not in Path(p).parts,'unsafe inspected path')
            target=snapshot/p;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(blob(root,commit,p))
        public=snapshot/'stock.jp';(public/'operations').mkdir(parents=True,exist_ok=True)
        (public/'operations/private').symlink_to(snapshot/'operations/private',target_is_directory=True)
        store=public/'data/jquants-archive'
        require(json.loads((store/'current.json').read_text())==m['archive_pointer'],'inspected generation pointer mismatch')
        codec.restore(store,snapshot/'operations/private/market-data')
        manifest=json.loads(codec.unpack(store,m['archive_pointer']['generation'],codec.MAX_INDEX,'generations'))
        require({p:v['sha256'] for p,v in manifest['files'].items()}==m['archive_hashes'],'archive manifest differs from observed files')
        try:
            from scripts.recovery_drill import validated_paper_run, hashes
            from scripts.operation_state import validate_workspace
            from scripts.operation_bootstrap import check_readiness
        except ModuleNotFoundError:
            from recovery_drill import validated_paper_run, hashes
            from operation_state import validate_workspace
            from operation_bootstrap import check_readiness
        state_errors=validate_workspace(public)
        before=hashes(snapshot/'operations/private')
        bootstrap=check_readiness(root=public,environ={},at=inspection_at)
        require(before==hashes(snapshot/'operations/private'),'historical bootstrap required mutation')
        coverage,selected,ok,errors=validated_paper_run(public)
        require(not state_errors and bootstrap['paper_go'] and ok and not errors,'historical recovery validators failed')
        require(selected==m['selected_run'],'historical run/attempt or artifact hashes changed')
        details=m['validation_details']
        require(details.get('state_errors')==state_errors and details.get('paper_go') is True and
                details.get('run_errors')==errors and
                coverage_decision(details.get('coverage'))==coverage_decision(coverage),'stored validation details differ')
    # We revalidate historical inputs using the current process's validators;
    # recording old Git blobs does not mean those blobs were executed.
    executing_validators={f'stock.jp/scripts/{name}':digest((Path(__file__).parent/name).read_bytes()) for name in VALIDATORS}
    return {'manifest_sha256':proof['sha256'],'inspected_commit':commit,'inspection_at_jst':inspection_at,
            'validation_code_mode':'CURRENT_CODE_HISTORICAL_INPUTS','recorded_validator_hashes':m['validators'],
            'revalidation_validator_hashes':executing_validators,'selected_run_id':m['selected_run']['run_id'],
            'selected_attempt':m['selected_run']['attempt'],'private_artifact_count':len(expected_private)}
