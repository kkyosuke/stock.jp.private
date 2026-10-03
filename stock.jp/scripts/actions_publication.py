"""Explicit publication/recovery operations; no automatic dispatch or bypass."""
import base64
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
import uuid

try:
    from scripts import actions_daily_remote as remote, market_data_git as codec, daily_publication as publication
except ModuleNotFoundError:
    import actions_daily_remote as remote, market_data_git as codec, daily_publication as publication

require = remote.require
PREFIX = '/repos/' + remote.REPO


def pages(request, path, key=None):
    """Explicit numbered pages; stop on excessive, duplicate or changed pages."""
    result, seen = [], set()
    for page in range(1,1001):
        separator = '&' if '?' in path else '?'
        payload = request('GET', PREFIX+path+separator+f'per_page=100&page={page}', None)
        rows = payload if key is None else payload[key]
        require(isinstance(rows,list) and len(rows)<=100, 'invalid pagination')
        for row in rows:
            ident = row.get('id', row.get('filename'))
            require(ident is not None and ident not in seen, 'duplicate or unstable paginated evidence')
            seen.add(ident);result.append(row)
        if len(rows)<100:
            if key is not None and 'total_count' in payload:
                require(len(result)==payload['total_count'], 'incomplete pagination')
            return result
    raise remote.RemoteStop('pagination limit exceeded')


def download_archive(request, commit, target):
    """Download only adopted/candidate archive blobs, then verify the codec."""
    remote.sha(commit)
    item = request('GET', PREFIX+'/git/commits/'+commit, None)
    require(item['sha']==commit, 'commit identity mismatch')
    tree = request('GET', PREFIX+'/git/trees/'+remote.sha(item['tree']['sha'])+'?recursive=1', None)
    require(tree.get('truncated') is False, 'archive tree truncated')
    root = 'stock.jp/data/jquants-archive/'
    target=Path(target)
    require(not target.exists(), 'archive target must be new')
    total=0
    for member in tree['tree']:
        if not member['path'].startswith(root) or member['type']=='tree':
            continue
        name=member['path'][len(root):]
        require(member['mode']=='100644' and member['type']=='blob' and
                (name=='current.json' or re.fullmatch(r'(objects|generations)/[0-9a-f]{2}/[0-9a-f]{64}\.gz',name)),
                'unsafe archive tree member')
        blob=request('GET', PREFIX+'/git/blobs/'+remote.sha(member['sha']),None)
        require(blob['encoding']=='base64', 'unsupported archive blob')
        raw=base64.b64decode(''.join(blob['content'].split()),validate=True)
        require(remote.git_blob(raw)==member['sha'] and len(raw)==blob['size'], 'remote archive hash mismatch')
        total+=len(raw);require(total<=codec.MAX_STORE,'archive size exceeded')
        codec.atomic(target/name,raw)
    return codec.verify(target)


def evidence(request, number, proof, *, review_probe, now=None):
    """review_probe must obtain effective reviewDecision, never infer from PR body."""
    now=now or datetime.now(timezone.utc)
    repo=request('GET',PREFIX,None)
    pr=request('GET',PREFIX+'/pulls/'+str(number),None)
    head=proof['head_sha']
    checks=pages(request,'/commits/'+head+'/check-runs','check_runs')
    statuses=pages(request,'/commits/'+head+'/statuses')
    runs=pages(request,'/actions/runs?head_sha='+head+'&event=pull_request','workflow_runs')
    reviews=pages(request,'/pulls/'+str(number)+'/reviews')
    try:
        from scripts.actions_http import reject_negative_reviews
    except ModuleNotFoundError:
        from actions_http import reject_negative_reviews
    reject_negative_reviews(reviews)
    files=pages(request,'/pulls/'+str(number)+'/files')
    decision=review_probe(number,head,reviews)
    require(decision in {None,'APPROVED'}, 'required review or requested changes remains')
    main=request('GET',PREFIX+'/git/ref/heads/main',None)['object']['sha']
    latest=request('GET',PREFIX+'/pulls/'+str(number),None)
    require(pr['head']['sha']==latest['head']['sha']==head and pr['base']['sha']==latest['base']['sha']==main,
            'PR changed during evidence collection')
    return dict(repository=repo['full_name'], visibility='private' if repo['private'] else 'public',
                auto_merge_allowed=repo.get('allow_auto_merge') is True, fetched_at=now.isoformat(), main_sha=main,
                reviews_complete=True, checks_complete=True, files_complete=True, statuses_complete=True,
                runs_complete=True, changed_paths=[f['filename'] for f in files],
                pr=dict(number=number,state=pr['state'],draft=pr['draft'],merged=pr['merged'],
                        base_ref=pr['base']['ref'],head_repository=pr['head']['repo']['full_name'],
                        author_login=pr['user']['login'],head_ref=pr['head']['ref'],head_sha=head,
                        base_sha=main,mergeable=latest['mergeable'],review_decision=decision),
                checks=[dict(name=c['name'],head_sha=c['head_sha'],status=c['status'],conclusion=c['conclusion'],
                             app_slug=c['app']['slug']) for c in checks],
                statuses=latest_statuses(statuses,head),workflow_runs=runs)


def latest_statuses(statuses,head):
    latest={}
    for item in statuses:
        context=item.get('context')
        require(isinstance(context,str) and bool(context),'status context missing')
        stamp=datetime.fromisoformat(item['created_at'].replace('Z','+00:00'))
        require(stamp.tzinfo is not None and type(item['id']) is int,'invalid status order')
        require(item['state'] in {'pending','success','failure','error'},'unknown status state')
        order=(stamp,item['id'])
        if context not in latest or order>latest[context][0]:latest[context]=(order,item)
    return [dict(sha=head,context=context,state=item['state']) for context,(_,item) in sorted(latest.items())]


def merge_and_restore(request, proof, number, *, review_probe, expected_generation, authorization=None):
    remote.gate(authorization)
    observed=evidence(request,number,proof,review_probe=review_probe)
    publication.allow_auto_merge(proof,observed)
    # Server still enforces current review/branch rules. No bypass/admin flag.
    response=request('PUT',PREFIX+'/pulls/'+str(number)+'/merge',
                     {'sha':proof['head_sha'],'merge_method':'squash'})
    require(response.get('merged') is True, 'merge not confirmed')
    merge_sha=remote.sha(response['sha'])
    main=request('GET',PREFIX+'/git/ref/heads/main',None)['object']['sha']
    require(main==merge_sha, 'main advanced after merge; verify separately')
    with tempfile.TemporaryDirectory() as tmp:
        store=Path(tmp)/'store'
        download_archive(request,main,store)
        pointer=json.loads((store/'current.json').read_bytes())
        require(pointer['generation']==expected_generation,'merged generation mismatch')
        codec.restore(store,Path(tmp)/'restored')
    return {'status':'RESTORED','main_sha':main,'generation':expected_generation}


def publish_prevalidated(request, *, proof, tree_sha, entries, blobs, authorization=None, root=None):
    """Publish only a locally validated tree, binding remote commit to same tree.

    proof must be produced by daily_publication.validate_local on an isolated
    local commit. Caller supplies its Git tree identity and bytes for changed
    paths; no arbitrary PR branch or ref input is accepted.
    """
    remote.gate(authorization)
    require(proof.get('validated') is True and proof['kind']=='market' and proof['repository']==remote.REPO,
            'validated market proof required')
    require(root is not None, 'isolated validated checkout required')
    fresh=publication.validate_local(root,'market',proof['run_id'],proof['base_sha'],proof['head_sha'])
    require({k:v for k,v in fresh.items() if k!='validated_at'} ==
            {k:v for k,v in proof.items() if k!='validated_at'}, 'local semantic proof changed')
    require(publication.git(root,'rev-parse',proof['head_sha']+'^{tree}').decode().strip()==tree_sha,
            'validated tree identity differs')
    publication.classify('market',proof['run_id'],entries)
    require(sorted(blobs)==sorted(proof['paths'])==sorted(e['path'] for e in entries),'publication paths differ')
    require(all(publication.git(root,'show',proof['head_sha']+':'+name)==data for name,data in blobs.items()),
            'upload bytes differ from validated local commit')
    main=request('GET',PREFIX+'/git/ref/heads/main',None)['object']['sha']
    require(main==proof['base_sha'],'main changed before publication')
    require(not any(p['head']['ref'].startswith('daily/market/') for p in pages(request,'/pulls?state=open')),
            'existing daily PR requires recovery')
    base=request('GET',PREFIX+'/git/commits/'+main,None)
    members=[]
    for name,data in sorted(blobs.items()):
        obj=request('POST',PREFIX+'/git/blobs',{'content':base64.b64encode(data).decode(),'encoding':'base64'})
        require(obj['sha']==remote.git_blob(data),'uploaded blob differs')
        members.append(dict(path=name,mode='100644',type='blob',sha=obj['sha']))
    tree=request('POST',PREFIX+'/git/trees',{'base_tree':base['tree']['sha'],'tree':members})
    require(tree['sha']==remote.sha(tree_sha),'remote tree differs from validated local tree')
    commit=request('POST',PREFIX+'/git/commits',{'tree':tree['sha'],'parents':[main],
                   'message':'Daily market generation '+uuid.uuid4().hex})
    head=remote.sha(commit['sha'])
    check=request('GET',PREFIX+'/git/commits/'+head,None)
    require(check['tree']['sha']==tree_sha and [p['sha'] for p in check['parents']]==[main], 'remote commit differs')
    branch='daily/market/'+proof['run_id']
    request('POST',PREFIX+'/git/refs',{'ref':'refs/heads/'+branch,'sha':head})
    pr=request('POST',PREFIX+'/pulls',{'base':'main','head':branch,'draft':False,
                'title':'Daily market archive '+proof['run_id'],
                'body':'Verified private archive generation. PAPER/HUMAN_ONLY; no execution readiness claim.'})
    updated={**copy.deepcopy(proof),'head_sha':head,'validated_at':datetime.now(timezone.utc).isoformat()}
    return {'number':pr['number'],'proof':updated,'status':'WAIT_CI'}


def bootstrap_checkpoint(store, source, writer, *, history_check=remote.reject_history):
    """Explicit first creation only; ref conflicts/unknown ACKs never reset state."""
    remote.gate(store.authorization)
    source=Path(source);history_check(source)
    snapshot=store.capture(source,history_check) if hasattr(store,'capture') else remote.capture(source,history_check)
    document=dict(version=2,writer=writer,fence=uuid.uuid4().hex,blocked_campaigns=[],runs={},snapshot=snapshot)
    try:
        from scripts import jquants_daily as daily
    except ModuleNotFoundError:
        import jquants_daily as daily
    for path in (source/'jquants/runs').glob('*T120000Z.json'):
        state=daily.read_state(path)
        require(state['status'] in {'COLLECTED','HOLIDAY'},'unresolved adopted daily journal')
        document['runs'][state['run_date']]=dict(state=state,phase='RESTORED',retry_not_before=None,
            reservation_ids=[codec.digest(codec.encoded([state['run_date'],index,receipt]))[:32]
                             for index,receipt in enumerate(state['requests'])])
    path=source/'jquants/history/state.json'
    if path.exists():
        state=json.loads(path.read_bytes());require(state['status']=='COMPLETE','history not adopted terminal')
        document['history']=dict(state=state,phase='RESTORED',retry_not_before=None,
            reservation_ids=[codec.digest(codec.encoded([state['as_of'],index,receipt]))[:32]
                             for index,receipt in enumerate(remote.history_receipts(state))])
    remote.validate(document)
    if hasattr(store,'create'):
        return store.create(document)
    raw=codec.encoded(document);require(len(raw)<=remote.LIMIT,'initial checkpoint too large')
    blob=store.call('POST','/git/blobs',{'content':base64.b64encode(raw).decode(),'encoding':'base64'})
    require(blob['sha']==remote.git_blob(raw),'bootstrap blob differs')
    tree=store.call('POST','/git/trees',{'tree':[dict(path='checkpoint.json',mode='100644',type='blob',sha=blob['sha'])]})
    commit=store.call('POST','/git/commits',{'message':'Initialize reviewed writer '+uuid.uuid4().hex,
                                         'tree':remote.sha(tree['sha']),'parents':[]})
    revision=remote.sha(commit['sha'])
    ack=store.call('POST','/git/refs',{'ref':'refs/'+remote.REF,'sha':revision})
    require(ack['object']['sha']==revision,'bootstrap ref ACK differs')
    actual,restored=store.read()
    require(actual==revision and restored==document,'bootstrap readback failed')
    return revision


def mark_adopted(checkpoint, *, verified_main, verified_generation, actual_generation):
    remote.sha(verified_main)
    require(verified_generation==actual_generation and re.fullmatch('[0-9a-f]{64}',actual_generation),
            'adopted generation mismatch')
    run=checkpoint.document['history'] if isinstance(checkpoint,remote.HistoryCheckpoint) else checkpoint.document['runs'][checkpoint.day]
    require(run['phase']=='COLLECTED','collection incomplete')
    run['phase']='RESTORED'
    intent=checkpoint.document.get('publication_intent')
    require(not intent or intent['state']=='ACKED','publication write outcome unresolved')
    checkpoint.document.pop('publication_intent',None)
    checkpoint.commit()


from contextlib import contextmanager
import subprocess
import os


try:
    from scripts.git_sandbox import git as isolated_git
except ModuleNotFoundError:
    from git_sandbox import git as isolated_git


@contextmanager
def prepare_candidate(root, source, run_id, base):
    """Offline isolated Git candidate; never changes caller checkout or settings."""
    remote.sha(base)
    require(isolated_git(root,'rev-parse',base+'^{commit}').decode().strip()==base,'base commit unavailable locally')
    with tempfile.TemporaryDirectory(prefix='actions-candidate-') as tmp:
        checkout=Path(tmp)/'checkout';checkout.mkdir()
        def git(*args):
            return isolated_git(checkout,*args)
        git('init','--quiet')
        git('fetch','--quiet','--no-tags',str(Path(root).resolve()),base)
        git('checkout','--quiet','--detach',base)
        store=checkout/'stock.jp/data/jquants-archive'
        pointer=codec.pack(source,store)
        git('add','--','stock.jp/data/jquants-archive')
        if not git('diff','--cached','--name-only').strip():
            yield {'status':'NOOP','generation':pointer['generation'],'base':base}
            return
        tree=git('write-tree').decode().strip()
        # commit-tree avoids hooks or branch updates; only this isolated object
        # database receives a candidate. No credentials or personal Git config.
        head=git('-c','user.name=Stock Automation','-c','user.email=automation@invalid.example',
                 'commit-tree',tree,'-p',base,'-m','Offline archive candidate').decode().strip()
        proof=publication.validate_local(checkout,'market',run_id,base,head)
        entries=publication.changes(checkout,base,head)
        blobs={name:publication.git(checkout,'show',head+':'+name) for name in proof['paths']}
        yield dict(status='CANDIDATE',root=checkout,proof=proof,tree_sha=tree,entries=entries,
                   blobs=blobs,generation=pointer['generation'])
