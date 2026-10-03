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
        remote.gate(self.checkpoint.authorization)
        pending=self.checkpoint.document.get('publication_intent')
        remote.require(not pending or pending['state']=='ACKED','publication write outcome unresolved')
        digest=codec.digest(codec.encoded([method,path,body]))
        identifiers={}
        if path.endswith('/git/refs'):
            identifiers={'ref':body['ref'],'sha':body['sha']}
        elif path.endswith('/pulls'):
            identifiers={'head':body['head'],'base':body['base']}
        elif re.fullmatch(r'/repos/kkyosuke/stock\.jp\.private/pulls/[1-9][0-9]*/merge',path):
            identifiers={'sha':body['sha']}
        intent=dict(id=uuid.uuid4().hex,state='PENDING',method=method,path=path,
                    request_sha256=digest,identifiers=identifiers,response=None)
        self.checkpoint.document['publication_intent']=intent
        self.checkpoint.commit()
        # Crash/lost ACK leaves PENDING. No error response text/body is stored.
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
                     and p['head']['repo']['full_name']==remote.REPO]
            remote.require(len(matches)==1,'PR outcome not uniquely confirmed')
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
