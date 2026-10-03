"""Persist publication write intents before HTTP; never replay unknown writes."""
import copy
import json
import re
import uuid
try:
    from scripts import actions_daily_remote as remote, market_data_git as codec
except ModuleNotFoundError:
    import actions_daily_remote as remote, market_data_git as codec


class IntentTransport:
    def __init__(self, checkpoint, request):
        self.checkpoint,self.request=checkpoint,request

    def __call__(self, method, path, body=None):
        if method=='GET':
            return self.request(method,path,body)
        clock=getattr(self.checkpoint,'clock',None)
        remote.gate(self.checkpoint.authorization,now=clock() if callable(clock) else None)
        if hasattr(self.request,'before_publication'):self.request.before_publication()
        if self.checkpoint.authorization.get('handover_check'):self.checkpoint.authorization['handover_check']()
        pending=self.checkpoint.document.get('publication_intent')
        remote.require(not pending or pending['state']=='ACKED','publication write outcome unresolved')
        digest=codec.digest(codec.encoded([method,path,body]))
        identifiers={}
        if path.endswith('/git/refs'):
            identifiers={'ref':body['ref'],'sha':body['sha']}
        elif path.endswith('/pulls'):
            transaction=self.checkpoint.document.get('transaction') or {}
            remote.require(all(k in transaction for k in ('head','tree','generation')),'PR intent identity missing')
            identifiers={'head':body['head'],'base':body['base'],'sha':transaction['head'],
                         'tree':transaction['tree'],'generation':transaction['generation'],
                         'repository':remote.REPO,'author':'kkyosuke'}
        elif re.fullmatch(r'/repos/kkyosuke/stock\.jp\.private/pulls/[1-9][0-9]*/merge',path):
            identifiers={'sha':body['sha']}
        intent=dict(id=uuid.uuid4().hex,state='PENDING',method=method,path=path,
                    request_sha256=digest,identifiers=identifiers,response=None)
        self.checkpoint.document['publication_intent']=intent
        self.checkpoint.commit()
        # Crash/lost ACK leaves PENDING. No error response text/body is stored.
        is_merge=bool(re.fullmatch(r'/repos/kkyosuke/stock\.jp\.private/pulls/[1-9][0-9]*/merge',path))
        merge_prepare=self.checkpoint.authorization.get('merge_io_prepare') if is_merge else None
        owner_prepare=self.checkpoint.authorization.get('publication_io_prepare')
        def prepare_all():
            if owner_prepare:owner_prepare()
            if merge_prepare:merge_prepare()
        prepare_io=prepare_all if owner_prepare or merge_prepare else None
        merge_check=self.checkpoint.authorization.get('merge_io_check') if is_merge else None
        remote.require(not is_merge or callable(merge_prepare) and callable(merge_check),'merge send-boundary evidence unavailable')
        def before_io():
            check=self.checkpoint.authorization.get('publication_io_check') or self.checkpoint.authorization.get('handover_check')
            if check:check()
            if merge_check:merge_check()
            clock=getattr(self.checkpoint,'clock',None)
            remote.gate(self.checkpoint.authorization,now=clock() if callable(clock) else None)
        if hasattr(self.request,'mutation'):
            response=self.request.mutation(method,path,body,before_io=before_io,prepare_io=prepare_io)
        else:
            remote.require(not hasattr(self.request,'before_publication'),'publication transport lacks guarded mutation boundary')
            if prepare_io:prepare_io()
            before_io()
            response=self.request(method,path,body)
        allowed={'sha','number','merged','object'}
        saved={k:copy.deepcopy(v) for k,v in response.items() if k in allowed}
        if 'object' in saved:
            saved['object']={'sha':saved['object']['sha']}
        codec.scan(codec.encoded(saved))
        intent['response']=saved;intent['state']='ACKED'
        self.checkpoint.commit()
        return response

    def recover_readonly(self):
        """Reconcile refs, PR creation and merge; opaque object writes stay stopped."""
        intent=self.checkpoint.document.get('publication_intent')
        remote.require(intent is not None and intent['state']=='PENDING','no pending publication intent')
        path,ids=intent['path'],intent['identifiers']
        prefix='/repos/'+remote.REPO
        if path.endswith('/git/refs'):
            remote.require(ids['ref'].startswith('refs/heads/daily/market/'),'unexpected publication ref')
            response=self.request('GET',prefix+'/git/ref/'+ids['ref'].removeprefix('refs/'),None)
            remote.require(response['object']['sha']==ids['sha'],'ref outcome not confirmed')
            response={'object':{'sha':ids['sha']}}
        elif path.endswith('/pulls'):
            try:
                from scripts.actions_publication import pages
            except ModuleNotFoundError:
                from actions_publication import pages
            matches=[p for p in pages(self.request,'/pulls?state=all')
                     if p['head']['ref']==ids['head'] and p['base']['ref']==ids['base']
                     and p['head']['repo']['full_name']==remote.REPO
                     and p['base']['repo']['full_name']==ids['repository']
                     and p['user']['login']==ids['author'] and p['base']['ref']=='main'
                     and p['head']['sha']==ids['sha'] and p['state']=='open'
                     and not p.get('draft',True) and not p.get('merged',False)]
            remote.require(len(matches)==1,'PR outcome not uniquely confirmed')
            commit=self.request('GET',prefix+'/git/commits/'+ids['sha'],None)
            remote.require(commit['tree']['sha']==ids['tree'],'PR recovered tree mismatch')
            import tempfile
            from pathlib import Path
            try:
                from scripts.actions_publication import download_archive
            except ModuleNotFoundError:
                from actions_publication import download_archive
            with tempfile.TemporaryDirectory() as tmp:
                target=Path(tmp)/'store'
                download_archive(self.request,ids['sha'],target)
                remote.require(json.loads((target/'current.json').read_bytes())['generation']==ids['generation'],
                               'PR recovered generation mismatch')
            response={'number':matches[0]['number']}
        elif path.endswith('/merge'):
            pr=self.request('GET',path.removesuffix('/merge'),None)
            remote.require(pr['merged'] is True and pr['head']['sha']==ids['sha'], 'merge outcome not confirmed')
            response={'merged':True,'sha':remote.sha(pr['merge_commit_sha'])}
        else:
            raise remote.RemoteStop('opaque object write outcome requires operator review; no replay')
        intent['response']=response;intent['state']='ACKED'
        self.checkpoint.commit()
        return response
