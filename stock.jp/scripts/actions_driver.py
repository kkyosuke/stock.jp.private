"""Single-writer daily/history execution, resumable publication and verified adoption.

No scheduler changes or approval creation. CLI is fail-closed without a separately
reviewed manifest, controller evidence and exact trusted main checkout.
"""
import argparse
import base64
import copy
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
from zoneinfo import ZoneInfo

try:
    from scripts import actions_daily_remote as r, actions_publication as pub, actions_authorization as auth
    from scripts import actions_attestation as attest, actions_http as http, actions_rate as rate
    from scripts import jquants_daily as daily, jquants_history as history, market_data_git as codec
    from scripts.actions_sharded import ShardedStore
    from scripts.actions_cache import ImmutableCache
    from scripts.actions_intent import IntentTransport
    from scripts.git_sandbox import git
except ModuleNotFoundError:
    import actions_daily_remote as r, actions_publication as pub, actions_authorization as auth
    import actions_attestation as attest, actions_http as http, actions_rate as rate
    import jquants_daily as daily, jquants_history as history, market_data_git as codec
    from actions_sharded import ShardedStore
    from actions_cache import ImmutableCache
    from actions_intent import IntentTransport
    from git_sandbox import git


def content(request,path,commit):
    item=request('GET',pub.PREFIX+'/contents/'+path+'?ref='+commit,None)
    r.require(item.get('encoding')=='base64' and item.get('type')=='file','required approved file missing')
    return base64.b64decode(''.join(item['content'].split()),validate=True)


def trusted_context(root,context,main):
    r.require(context.get('repository')==r.REPO and context.get('ref')=='refs/heads/main'
              and context.get('event') in {'schedule','workflow_dispatch'}
              and context.get('sha')==main,'untrusted workflow context')
    r.require(git(root,'rev-parse','HEAD').decode().strip()==main,'checkout differs from current main')
    r.require(not git(root,'status','--porcelain').strip(),'dirty execution checkout')
    r.require(git(root,'remote','get-url','origin').decode().strip() in
              {'https://github.com/'+r.REPO+'.git','https://github.com/'+r.REPO,'git@github.com:'+r.REPO+'.git'},'unexpected execution remote')


def immutable_history(source):
    return {p.relative_to(source).as_posix():codec.digest(p.read_bytes()) for p in source.rglob('*')
            if p.is_file() and ('/history/' in '/'+p.relative_to(source).as_posix()
            or p.name=='history-marker.json' or '/objects/' in '/'+p.relative_to(source).as_posix()
            or '/revisions/' in '/'+p.relative_to(source).as_posix())}


class Driver:
    def __init__(self,root,request,provider,*,pin,writer,context,clock=None,sleep=None,live_handover_probe=None):
        self.root=Path(root);self.request=request;self.provider=provider
        self.pin,self.writer,self.context=pin,writer,context
        self.clock=clock or (lambda:datetime.now(ZoneInfo('Asia/Tokyo')))
        self.sleep=sleep
        self.live_handover_probe=live_handover_probe

    def execute(self,mode):
        r.require(mode in {'daily','history'},'invalid mode')
        r.require(callable(self.live_handover_probe),'live independent scheduler/lease transport unavailable; activation blocked')
        r.require(isinstance(self.pin,str) and len(self.pin)==64,'reviewed approval pin missing')
        main=self.request('GET',pub.PREFIX+'/git/ref/heads/main',None)['object']['sha']
        trusted_context(self.root,self.context,main)
        raw=content(self.request,auth.MANIFEST,main)
        r.require(codec.digest(raw)==self.pin,'approval hash differs')
        scope=json.loads(raw)
        try:
            self.request('GET',pub.PREFIX+'/git/ref/'+r.REF,None);initialized=True
        except http.Rejected as error:
            if error.status!=404:raise
            initialized=False
        with tempfile.TemporaryDirectory(prefix='stock-actions-') as tmp:
            work=Path(tmp);adopted=work/'adopted';archive=work/'archive'
            original_request=self.request
            self.request=ImmutableCache(original_request,work/'http-cache')
            try:
                pub.download_archive(self.request,main,archive);codec.restore(archive,adopted)
                scope,facts=auth.verify_scope(self.request,approved_manifest_sha256=self.pin,mode=mode,
                    writer=self.writer,source=adopted,now=self.clock(),clock=self.clock,allow_pending_publication=initialized,
                    handover_probe=lambda _:attest.verify(self.request,scope,self.pin,main=main,
                                                          initialized=initialized,now=self.clock(),live_probe=self.live_handover_probe,clock=self.clock))
                def revalidate_handover():
                    return attest.verify(self.request,scope,self.pin,main=main,initialized=initialized,now=self.clock(),live_probe=self.live_handover_probe,clock=self.clock)
                facts['handover_check']=revalidate_handover
                store=ShardedStore(self.request,authorization=facts,cache=work/'cache')
                if (adopted/'jquants/history/state.json').exists():
                    # verify_scope already compared this against the pinned baseline.
                    history_check=auth.AdoptedHistory(adopted,immutable_history(adopted)).check
                else:history_check=r.reject_history
                binding={'manifest_sha256':self.pin,'baseline':scope['baseline'],'handover_epoch':scope['handover_epoch']}
                if not initialized:
                    r.require(not pub.pages(self.request,'/pulls?state=open'),'bootstrap has pending PRs')
                    store.ensure_capacity(adopted)
                    pub.bootstrap_checkpoint(store,adopted,self.writer,history_check=history_check)
                revision,document=store.read()
                if hasattr(self.request,'restore'):self.request.restore(document.get('github_budget'))
                if document.get('adopted_history'):
                    history_check=auth.AdoptedHistory(adopted,document['adopted_history']).check
                r.require(document['writer']==self.writer,'another writer owns checkpoint')
                if document.get('approval_binding'):
                    r.require(document['approval_binding']==binding,'checkpoint approval differs; no reset')
                else:
                    r.require(not initialized,'existing unbound checkpoint cannot be adopted automatically')
                runtime=work/'runtime';private=runtime/'operations/private';private.mkdir(parents=True)
                codec.atomic(private/'source-config.json',content(self.request,'operations/private/source-config.json',main))
                codec.atomic(private/'operation-policy.json',content(self.request,'operations/private/operation-policy.json',main))
                policy=json.loads((private/'operation-policy.json').read_bytes())
                r.require(policy['operation_mode']=='PAPER' and policy['broker_submission']=='HUMAN_ONLY','runtime policy changed')
                codec.atomic(runtime/auth.MANIFEST,raw)
                campaign=scope.get('history_campaign')
                if mode=='history':
                    r.require(isinstance(campaign,dict) and type(campaign.get('sessions')) is int and 1<=campaign['sessions']<=280,'approved history campaign missing')
                    identity=dict(as_of=campaign['as_of'],sessions=campaign['sessions'],batch_limit=12,
                        config_sha256=codec.digest((private/'source-config.json').read_bytes()))
                    checkpoint=r.HistoryCheckpoint(store,writer=self.writer,authorization=facts,campaign=identity,clock=self.clock)
                else:checkpoint=r.RunnerCheckpoint(store,writer=self.writer,authorization=facts,history_check=history_check,clock=self.clock)
                checkpoint.revision,checkpoint.document=revision,document
                checkpoint.source=private/'market-data'
                if not initialized:
                    checkpoint.document['approval_binding']=binding;checkpoint.commit()
                transaction=document.get('transaction')
                if transaction:
                    r.require(transaction['mode']==mode,'pending publication belongs to another mode')
                    pending=pub.pages(self.request,'/pulls?state=open')
                    r.require(all(not p['head']['ref'].startswith('daily/market/') or
                                  p['head']['sha']==transaction['head'] and
                                  (transaction['number'] is None or p['number']==transaction['number'])
                                  for p in pending),'another daily publication is unresolved')
                    checkpoint.day=transaction['day']
                    store.restore(document['snapshot'],checkpoint.source,checkpoint.history_check)
                    return self.publish(checkpoint,main)
                r.require(not document.get('publication_intent'),'unbound publication intent requires review')
                r.require(not any(p['head']['ref'].startswith('daily/market/') for p in pub.pages(self.request,'/pulls?state=open')),
                          'unowned daily PR requires review')
                try:
                    if mode=='daily':
                        report=daily.run(runtime,at=self.clock(),transport=self.provider,sleep=self.sleep,remote=checkpoint)
                        day=report['run_date']
                    else:
                        batch=len(document['history']['state']['batches']) if document.get('history') else 1
                        previous=document.get('history')
                        if previous and previous['phase'] in {'IDLE','PAUSED'} and batch<50:
                            previous_state=previous['state']
                            if len(previous_state['batches'][-1])==previous_state['batch_limit'] and previous_state['status'] in {'RUNNING','PAUSED','BATCH_EXHAUSTED'}:
                                batch+=1  # all reservations have known durable outcomes; never advance IN_FLIGHT
                        for _ in range(50):
                            report=history.run(runtime,as_of=date.fromisoformat(campaign['as_of']),sessions=campaign['sessions'],
                                batch=batch,transport=self.provider,sleep=self.sleep,remote=checkpoint)
                            if report['status']!='BATCH_EXHAUSTED':break
                            batch=report['batch']+1
                        day=campaign['as_of']
                except rate.BudgetPause:
                    # Runner's finally/save hooks have already preserved all known
                    # reservations. Never clear IN_FLIGHT or start a replacement run.
                    checkpoint.document['github_pause']={'reason':'checkpoint_headroom','at':self.clock().isoformat()}
                    if not checkpoint.poisoned:
                        checkpoint.document['snapshot']=checkpoint.capture_snapshot();checkpoint.commit()
                    return {'status':'PAUSED_GITHUB_BUDGET','mode':mode}
                if report['status'] not in {'COLLECTED','HOLIDAY','COMPLETE'}:return report
                run=checkpoint.document['history'] if mode=='history' else checkpoint.document['runs'][day]
                if run['phase']=='RESTORED':
                    if hasattr(self.request,'state'):checkpoint.commit()
                    return {'status':'ALREADY_RESTORED','mode':mode}
                checkpoint.day=day
                if mode=='history':
                    # History completes price/master/TOPIX only, never all Light datasets.
                    status=daily.ingest.planned_status('light',(),date.fromisoformat(day))
                    status.update(scope='history_bounded',all_entitled_complete=False)
                    codec.atomic(checkpoint.source/'jquants/latest.json',codec.encoded(status))
                    checkpoint.document['snapshot']=checkpoint.capture_snapshot();checkpoint.commit()
                with pub.prepare_candidate(self.root,checkpoint.source,day,main) as candidate:
                    if candidate['status']=='NOOP':
                        pub.mark_adopted(checkpoint,verified_main=main,verified_generation=candidate['generation'],
                                         actual_generation=candidate['generation'])
                        return {'status':'RESTORED','generation':candidate['generation'],'main_sha':main}
                    checkpoint.document['transaction']=dict(mode=mode,day=day,base=main,tree=candidate['tree_sha'],
                        generation=candidate['generation'],stage='BLOBS',index=0,head=None,number=None)
                    checkpoint.commit()
                return self.publish(checkpoint,main)
            finally:
                self.request=original_request

    def publish(self,checkpoint,main):
        try:return self._publish(checkpoint,main)
        except rate.BudgetPause:
            if not checkpoint.poisoned:
                checkpoint.document['github_pause']={'reason':'publication_headroom','at':self.clock().isoformat()}
                checkpoint.commit()
            return {'status':'PAUSED_GITHUB_BUDGET','mode':checkpoint.document['transaction']['mode']}

    def _publish(self,checkpoint,main):
        transaction=checkpoint.document['transaction'];t=transaction
        transport=IntentTransport(checkpoint,self.request)
        def write(method,path,body,advance):
            intent=checkpoint.document.get('publication_intent')
            if intent and intent['state']=='PENDING':transport.recover_readonly();intent=checkpoint.document['publication_intent']
            if intent:
                r.require(intent['state']=='ACKED' and intent['request_sha256']==codec.digest(codec.encoded([method,path,body])),
                          'publication recovery operation mismatch')
                result=intent['response']
            else:result=transport(method,path,body)
            advance(result)
            checkpoint.document.pop('publication_intent',None);checkpoint.commit()
            return result
        if t['stage'] in {'MERGE','RESTORE'}:
            return self.adopt(checkpoint,transport,write)
        r.require(main==t['base'],'main advanced; publication requires separate review')
        with pub.prepare_candidate(self.root,checkpoint.source,t['day'],t['base']) as candidate:
            r.require(candidate['status']=='CANDIDATE' and candidate['tree_sha']==t['tree']
                      and candidate['generation']==t['generation'],'regenerated candidate differs')
            names=sorted(candidate['blobs'])
            while t['stage']=='BLOBS' and t['index']<len(names):
                name=names[t['index']];data=candidate['blobs'][name]
                snapshot=checkpoint.document['snapshot']
                member=snapshot.get('files',{}).get(name.removeprefix('stock.jp/data/jquants-archive/'))
                if member and member['sha256']==codec.digest(data) and member['bytes']==len(data) and member['chunks']==[r.git_blob(data)]:
                    # The verified checkpoint tree already makes this exact Git
                    # blob reachable in this repository. No upload/intent replay.
                    t['index']+=1
                    if t['index']==len(names):t['stage']='TREE'
                    continue
                def ack(result):
                    r.require(result['sha']==r.git_blob(data),'publication blob differs');t['index']+=1
                    if t['index']==len(names):t['stage']='TREE'
                write('POST',pub.PREFIX+'/git/blobs',{'encoding':'base64','content':base64.b64encode(data).decode()},ack)
            if t['stage']=='TREE':
                checkpoint.commit()  # coalesce all immutable-blob reuse progress
                base=self.request('GET',pub.PREFIX+'/git/commits/'+t['base'],None)
                members=[dict(path=n,mode='100644',type='blob',sha=r.git_blob(candidate['blobs'][n])) for n in names]
                def ack(result):
                    r.require(result['sha']==t['tree'],'candidate tree differs');t['stage']='COMMIT'
                write('POST',pub.PREFIX+'/git/trees',{'base_tree':base['tree']['sha'],'tree':members},ack)
            if t['stage']=='COMMIT':
                def ack(result):t.update(head=r.sha(result['sha']),stage='REF')
                write('POST',pub.PREFIX+'/git/commits',{'tree':t['tree'],'parents':[t['base']],
                      'message':'Verified archive '+t['generation']},ack)
            if t['stage']=='REF':
                obj=self.request('GET',pub.PREFIX+'/git/commits/'+t['head'],None)
                r.require(obj['tree']['sha']==t['tree'] and [p['sha'] for p in obj['parents']]==[t['base']], 'publication commit differs')
                def ack(result):
                    r.require(result['object']['sha']==t['head'],'publication ref differs');t['stage']='PR'
                write('POST',pub.PREFIX+'/git/refs',{'ref':'refs/heads/daily/market/'+t['day'],'sha':t['head']},ack)
            if t['stage']=='PR':
                def ack(result):t.update(number=result['number'],stage='CI')
                write('POST',pub.PREFIX+'/pulls',{'base':'main','head':'daily/market/'+t['day'],'draft':False,
                      'title':'Daily market archive '+t['day'],
                      'body':'Verified private archive generation. PAPER/HUMAN_ONLY.'},ack)
            r.require(t['stage']=='CI','unknown publication stage')
            proof={**candidate['proof'],'head_sha':t['head']}
            observed=pub.evidence(self.request,t['number'],proof,review_probe=self.request.review_decision)
            try:pub.publication.allow_auto_merge(proof,observed)
            except ValueError:
                if hasattr(self.request,'state'):checkpoint.commit()
                return {'status':'WAIT_CI','number':t['number'],'head':t['head']}
            t['stage']='MERGE';checkpoint.commit()
            return self.adopt(checkpoint,transport,write)

    def adopt(self,checkpoint,transport,write):
        t=checkpoint.document['transaction']
        if t['stage']=='MERGE':
            # Recheck all CI/reviews on every fresh merge attempt. An unresolved
            # prior merge is recovered read-only and is never sent again.
            if not checkpoint.document.get('publication_intent'):
                with pub.prepare_candidate(self.root,checkpoint.source,t['day'],t['base']) as candidate:
                    r.require(candidate['tree_sha']==t['tree'] and candidate['generation']==t['generation'],'merge candidate differs')
                    proof={**candidate['proof'],'head_sha':t['head']}
                    observed=pub.evidence(self.request,t['number'],proof,review_probe=self.request.review_decision)
                    pub.publication.allow_auto_merge(proof,observed)
            def ack(result):
                r.require(result.get('merged') is True,'merge not confirmed');t.update(merged=r.sha(result['sha']),stage='RESTORE')
            write('PUT',pub.PREFIX+'/pulls/'+str(t['number'])+'/merge',{'sha':t['head'],'merge_method':'squash'},ack)
        main=self.request('GET',pub.PREFIX+'/git/ref/heads/main',None)['object']['sha']
        r.require(main==t['merged'],'main advanced after merge; verify separately')
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'archive';pub.download_archive(self.request,main,target)
            restored=Path(tmp)/'restored';codec.restore(target,restored)
            actual=json.loads((target/'current.json').read_bytes())['generation']
            r.require(actual==t['generation'],'adopted generation differs')
            expected={p.relative_to(checkpoint.source).as_posix():codec.digest(p.read_bytes())
                      for p in checkpoint.source.rglob('*') if p.is_file() and p.name!='.ingestion.lock'}
            received={p.relative_to(restored).as_posix():codec.digest(p.read_bytes()) for p in restored.rglob('*') if p.is_file()}
            r.require(expected==received,'adopted raw hashes differ')
        if t['mode']=='history' and not checkpoint.document.get('adopted_history'):
            checkpoint.document['adopted_history']=immutable_history(checkpoint.source)
        checkpoint.document.pop('transaction')
        pub.mark_adopted(checkpoint,verified_main=main,verified_generation=t['generation'],actual_generation=actual)
        return {'status':'RESTORED','number':t['number'],'main_sha':main,'generation':actual}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['daily','history'],required=True)
    parser.add_argument('--root',type=Path,default=Path.cwd())
    args=parser.parse_args()
    try:
        r.require(os.environ.get('STOCK_ACTIONS_ENABLED')=='true','execution disabled')
        try:
            from scripts.history_resources import constrain_memory
        except ModuleNotFoundError:
            from history_resources import constrain_memory
        constrain_memory()
        request=rate.BudgetedTransport(http.GitHubTransport(os.environ.get('AUTOMATION_PAT')))
        provider=http.ProviderTransport(os.environ.get('JQUANTS_API_KEY'))
        result=Driver(args.root,request,provider,pin=os.environ.get('STOCK_ACTIONS_APPROVAL_SHA256'),
            writer='actions-v1',context=dict(repository=os.environ.get('GITHUB_REPOSITORY'),
                ref=os.environ.get('GITHUB_REF'),sha=os.environ.get('GITHUB_SHA'),event=os.environ.get('GITHUB_EVENT_NAME'))).execute(args.mode)
        print(json.dumps(result,sort_keys=True))
        return 0 if result['status'] in {'RESTORED','ALREADY_RESTORED','WAIT_CI','PAUSED_GITHUB_BUDGET'} else 2
    except Exception:
        # Provider bodies, credentials and private configuration never enter logs.
        print(json.dumps({'status':'BLOCKED','reason':'execution_evidence_or_checkpoint_failed'}));return 2


if __name__=='__main__':raise SystemExit(main())
