"""Verify pinned, separately reviewed approval scope; never create approvals."""
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import re

import stock_jp.market_data.storage as codec
import stock_jp.market_data.jquants.history as history
from stock_jp.automation.daily_remote import require, sha, REPO

MANIFEST = 'config/automation.json'


def verify_scope(request, *, approved_manifest_sha256, mode, writer, source, handover_probe,
                 now=None, allow_pending_publication=False, clock=None, allow_legacy_debit=False):
    """Pin comes from reviewed deployment, not workflow_dispatch/PR input.

    Version 1 requires the independent scheduler/lease probe. Version 2 requires
    the caller's verified retirement receipt and current GitHub run. Neither has
    a default success implementation.
    """
    now = now or datetime.now(timezone.utc)
    require(mode in {'daily','history'}, 'invalid acquisition scope')
    require(isinstance(approved_manifest_sha256, str) and len(approved_manifest_sha256) == 64,
            'reviewed approval pin is missing')
    prefix = '/repos/' + REPO
    repo = request('GET', prefix, None)
    require(repo.get('private') is True and repo.get('full_name') == REPO, 'private repository mismatch')
    main = sha(request('GET', prefix+'/git/ref/heads/main', None)['object']['sha'])
    content = request('GET', prefix+'/contents/'+MANIFEST+'?ref='+main, None)
    require(content.get('encoding') == 'base64' and content.get('type') == 'file', 'approval manifest unavailable')
    raw = base64.b64decode(''.join(content['content'].split()), validate=True)
    require(codec.digest(raw) == approved_manifest_sha256, 'approval manifest differs from reviewed pin')
    codec.scan(raw)
    scope = json.loads(raw)
    require(scope.get('version') in {1,2} and scope.get('enabled') is True, 'scope disabled')
    require(scope.get('repository') == REPO and scope.get('writer') == writer and mode in scope.get('modes', []),
            'writer or operation is outside approval scope')
    rejections = scope.get('publication_rejections')
    if scope.get('legacy_debit') is not None:
        require(scope['version'] == 2 and allow_legacy_debit,
                'legacy debit requires the target-date budget adapter')
        import stock_jp.automation.legacy_debit as debit
        debit.validate(scope['legacy_debit'], scope['history_campaign'])
        retained = dict(source_commit=scope['legacy_debit']['source_commit'],
                        journal_sha256=scope['legacy_debit']['journal_sha256'],
                        disposition='PAYLOAD_EXCLUDED_BUDGET_RETAINED')
        require(rejections == [retained], 'legacy rejection must remain explicitly recorded in pinned scope')
    else:
        require(rejections == [], 'publication rejection or unresolved campaign remains')
    require(scope.get('unresolved_campaigns') == [], 'publication rejection or unresolved campaign remains')
    require(scope.get('mode') == 'PAPER' and scope.get('submission') == 'HUMAN_ONLY'
            and scope.get('plan') == 'light' and scope.get('addons') == [], 'policy mismatch')
    require(scope.get('daily_limit') == 12 and scope.get('history_limits') == [280,600,50,12]
            and scope.get('price_coverage_min') == .98 and scope.get('active_coverage_min') == 1., 'scope limits changed')
    expiry = datetime.fromisoformat(scope['expires_at'])
    require(expiry.tzinfo is not None and now < expiry, 'approval expired')
    require(scope.get('data_publication_authorized') is True and scope.get('checkpoint_authorized') is True,
            'data/checkpoint publication not approved')
    # Bind the restored archive, not a payload copied from an acquisition branch.
    baseline = scope['baseline']
    baseline_sha = sha(baseline['main_sha'])
    comparison = request('GET', prefix+'/compare/'+baseline_sha+'...'+main, None)
    require(comparison.get('status') in {'ahead','identical'},'approved baseline is not an ancestor')
    baseline_tree=complete_tree(request,baseline_sha)
    current_tree=complete_tree(request,main)
    allowed_approval={MANIFEST,'.github/stock-automation-handover.json','.github/stock-automation-controller-evidence.json'}
    if scope['version'] == 2: allowed_approval.add('operations/evidence/automation-retirement.json')
    for name in baseline_tree.keys() | current_tree.keys():
        before,after=baseline_tree.get(name),current_tree.get(name)
        if before==after:continue
        require(after is not None and after[0:2]==('100644','blob'),'deletion or unsafe mode beyond approved scope')
        if name in allowed_approval:continue
        require(name.startswith('data/market/jquants/') and codec.store_member(name.removeprefix('data/market/jquants/'))
                and (before is None or name.endswith('/current.json')),
                'main changed beyond approved data-only scope; rebind required')
    pointer = request('GET', prefix + '/contents/data/market/jquants/current.json?ref=' + main, None)
    pointer_value = json.loads(base64.b64decode(''.join(pointer['content'].split()), validate=True))
    original = request('GET', prefix + '/contents/data/market/jquants/current.json?ref=' + baseline_sha, None)
    require(json.loads(base64.b64decode(''.join(original['content'].split()),validate=True))['generation']==baseline['generation'],
            'approved baseline generation differs')
    files = {p.relative_to(source).as_posix():codec.digest(p.read_bytes())
             for p in Path(source).rglob('*') if p.is_file() and p.name != '.ingestion.lock'}
    codec.safe_tree(Path(source))
    from stock_jp.automation.publication import download_archive
    with tempfile.TemporaryDirectory() as tmp:
        store,restored=Path(tmp)/'store',Path(tmp)/'restored'
        download_archive(request,main,store)
        require(json.loads((store/'current.json').read_bytes())['generation']==pointer_value['generation'], 'main pointer changed')
        codec.restore(store,restored)
        expected={p.relative_to(restored).as_posix():codec.digest(p.read_bytes()) for p in restored.rglob('*') if p.is_file()}
        require(files==expected,'restored main file hashes differ')
        baseline_store,baseline_source=Path(tmp)/'baseline-store',Path(tmp)/'baseline-source'
        download_archive(request,baseline_sha,baseline_store)
        codec.restore(baseline_store,baseline_source)
        if (baseline_source/'jquants/history/state.json').exists():
            immutable={p.relative_to(baseline_source).as_posix():codec.digest(p.read_bytes())
                       for p in baseline_source.rglob('*') if p.is_file() and
                       ('/history/' in '/'+p.relative_to(baseline_source).as_posix()
                        or p.name=='history-marker.json' or '/objects/' in '/'+p.relative_to(baseline_source).as_posix()
                        or '/revisions/' in '/'+p.relative_to(baseline_source).as_posix())}
            fixed=AdoptedHistory(baseline_source,immutable)
            fixed.check(source)
    if scope['version'] == 2:
        observed = handover_probe(None)
        require(observed.get('retirement_verified') is True and observed.get('github_run_verified') is True,
                'retirement or current GitHub run verification missing')
        require((clock() if clock else now) < expiry, 'approval expired during verification')
    else:
        observed = handover_probe(scope['old_scheduler_id'])
        observed_at = datetime.fromisoformat(observed['observed_at'])
        checked_at=clock() if clock else now
        require(checked_at<expiry,'approval expired during verification')
        require(observed_at.tzinfo is not None and 0 <= (checked_at-observed_at).total_seconds() <= 60,
                'stale writer handover evidence')
        require(observed['scheduler_id'] == scope['old_scheduler_id'] and observed['enabled'] is False
                and observed['active_writers'] == 0 and observed['next_writer'] == writer,
                'old writer has not handed over')
    from stock_jp.automation.publication import pages
    require(allow_pending_publication or not any(p['head']['ref'].startswith('daily/market/') for p in pages(request,'/pulls?state=open')),
            'existing daily PR requires adoption/recovery first')
    require(request('GET',prefix+'/git/ref/heads/main',None)['object']['sha']==main,
            'main moved during authorization verification')
    return scope, dict(enabled=True, publication_authorized=True, checkpoint_authorized=True,
                      old_scheduler_stopped=True, main_restored_and_verified=True, exclusive_writer=True,
                      publication_rejected=False, unresolved_history=False, open_daily_pr=False,
                      repository=REPO, visibility='private', mode='PAPER', submission='HUMAN_ONLY', plan='light',
                      expires_at=scope['expires_at'])


def complete_tree(request,commit):
    """Full immutable tree comparison, independent of compare API's 300-file cap."""
    prefix='/repos/'+REPO
    item=request('GET',prefix+'/git/commits/'+sha(commit),None)
    require(item.get('sha')==commit,'commit identity mismatch')
    tree=request('GET',prefix+'/git/trees/'+sha(item['tree']['sha'])+'?recursive=1',None)
    require(tree.get('truncated') is False and isinstance(tree.get('tree'),list),'scope tree truncated or unavailable')
    result={};seen=set()
    for member in tree['tree']:
        name=member['path']
        require(isinstance(name,str) and name and name not in seen and not name.startswith('/')
                and '\\' not in name and all(p not in {'','.','..'} for p in name.split('/')),'invalid scope tree path')
        seen.add(name)
        if member['type']=='tree':
            require(member['mode']=='040000','invalid directory mode');sha(member['sha']);continue
        require(member['type'] in {'blob','commit'},'invalid scope tree type')
        result[name]=(member['mode'],member['type'],sha(member['sha']))
    return result


class AdoptedHistory:
    """Keep COMPLETE history evidence immutable; accept only verified main scope."""
    def __init__(self, source, expected_files):
        self.files = dict(expected_files)
        require('jquants/history/state.json' in self.files and 'jquants/history-marker.json' in self.files,
                'adopted history evidence missing')
        self.check(source)
        state = json.loads((Path(source)/'jquants/history/state.json').read_bytes())
        history.validate_history(state, lambda n:(Path(source)/n).read_bytes())
        require(state['status'] == 'COMPLETE', 'history not terminal')
        marker = json.loads((Path(source)/'jquants/history-marker.json').read_bytes())
        require(marker == {'as_of':state['as_of'], 'sessions':state['sessions']}, 'history marker mismatch')
        # Include ALL immutable pages and receipt revisions, not mutable latest pointers.
        required = {'jquants/history/state.json', 'jquants/history-marker.json'}
        for value in state['ranges'].values():
            required.update('jquants/'+p['path'] for p in value['pages'])
        for task, digest in state['tasks'].items():
            dataset, day = task.split(':')
            name = f'jquants/datasets/{dataset}/{day}/revisions/{digest}.json'
            required.add(name)
            manifest = json.loads((Path(source)/name).read_bytes())
            required.update('jquants/'+p['path'] for p in manifest['pages'])
        require(required <= self.files.keys(), 'history immutable evidence incomplete')

    def check(self, source):
        source = Path(source)
        if not source.exists():
            return  # fresh runner will restore the verified remote snapshot
        for name, digest in self.files.items():
            codec.safe_name(name)
            path = source/name
            require(path.is_file() and not path.is_symlink() and codec.digest(path.read_bytes()) == digest,
                    'adopted history evidence changed or missing')
