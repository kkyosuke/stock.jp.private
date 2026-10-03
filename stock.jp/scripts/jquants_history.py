"""One fixed, reviewed history campaign: <=280 sessions, 600 calls, 50 x 12 batches.

No automatic batch advance, retry, Git write, or readiness claim. CLI is usable
only after adoption on clean current main. Daily and history share both locks.
"""
from __future__ import annotations
import argparse
from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import re
try:
    from scripts import jquants_daily as daily, jquants_ingest as ingest
    from scripts import market_data_git as codec, history_resources as resources
    from scripts.jquants_catalog import BASE_URL, BY_NAME
except ModuleNotFoundError:
    import jquants_daily as daily, jquants_ingest as ingest
    import market_data_git as codec
    import history_resources as resources
    from jquants_catalog import BASE_URL, BY_NAME

NAMES = ('trading_calendar', 'topix_daily', 'equities_master', 'equities_daily')
require = daily.require


def validate_history(state, read):
    require(set(state) == {'version','as_of','sessions','config_sha256','batch_limit','batches',
        'ranges','days','tasks','status','retry_not_before','resource_checks'}, 'invalid history journal schema')
    end = date.fromisoformat(state['as_of'])
    require(state['version'] == 1 and type(state['sessions']) is int and 1 <= state['sessions'] <= 280,
            'invalid history extent')
    require(type(state['batch_limit']) is int and 1 <= state['batch_limit'] <= 12, 'invalid batch limit')
    require(re.fullmatch('[0-9a-f]{64}', state['config_sha256']) is not None, 'invalid config digest')
    require(state['status'] in {'RUNNING','PAUSED','BATCH_EXHAUSTED','EXHAUSTED','BLOCKED','COMPLETE'}, 'invalid history status')
    if state['retry_not_before']: daily.aware(state['retry_not_before'])
    batches = state['batches']
    require(isinstance(batches,list) and 1 <= len(batches) <= 50, 'invalid batch count')
    require(sum(len(b) for b in batches) <= 600, 'campaign budget exceeded')
    require(all(len(b)==state['batch_limit'] for b in batches[:-1]),'prior batch was not exhausted')
    for batch in batches:
        require(isinstance(batch,list) and len(batch) <= state['batch_limit'], 'batch budget exceeded')
        for receipt in batch:
            require(set(receipt) == {'endpoint','query_sha256','reserved_at'} and
                    receipt['endpoint'] in {BY_NAME[n].endpoint for n in NAMES}, 'invalid request receipt')
            require(re.fullmatch('[0-9a-f]{64}', receipt['query_sha256']) is not None, 'invalid query digest')
            daily.aware(receipt['reserved_at'])
    require(isinstance(state['resource_checks'],dict) and set(state['resource_checks']) <= {str(i) for i in range(1,len(batches)+1)}, 'invalid resource checks')
    for report in state['resource_checks'].values():
        require(set(report)=={'raw_bytes','staging_bytes','free_bytes','max_raw_bytes','max_staging_bytes','min_free_bytes','max_response_bytes','max_address_space_bytes'} and all(type(v) is int and v>=0 for v in report.values()), 'invalid resource report')
    require(isinstance(state['ranges'],dict) and set(state['ranges']) <= {'trading_calendar','topix_daily'}, 'invalid range tasks')
    for name, r in state['ranges'].items():
        require(set(r)=={'query','pages','next_key','seen_keys','complete'}, 'invalid range checkpoint')
        expected_start = (end-timedelta(days=550)).isoformat() if name=='trading_calendar' else state['days'][0]
        require(r['query']=={'from':expected_start,'to':state['as_of']}, 'range query changed')
        require(type(r['complete']) is bool and isinstance(r['pages'],list), 'invalid range state')
        tokens=[]
        for index, ref in enumerate(r['pages']):
            require(set(ref)=={'path','sha256','bytes'}, 'invalid range page ref')
            require(ref['path']==f"objects/{ref['sha256'][:2]}/{ref['sha256']}.json", 'invalid range page path')
            blob=read('jquants/'+ref['path']); require(ingest.digest(blob)==ref['sha256'] and len(blob)==ref['bytes'],'range page corruption')
            payload=json.loads(blob);rows=payload['data'];token=payload.get('pagination_key')
            require(isinstance(rows,list) and all(isinstance(row,dict) for row in rows), 'invalid range rows')
            require(token is None or isinstance(token,str), 'invalid range token')
            if token:tokens.append(token)
            require(all(expected_start <= row.get('Date','') <= state['as_of'] for row in rows), 'out of range response')
            if index < len(r['pages'])-1: require(bool(token),'premature range end')
        require(len(tokens)==len(set(tokens)) and r['seen_keys']==tokens, 'range pagination cycle')
        require(r['next_key']==(tokens[-1] if r['pages'] and token else None), 'range continuation mismatch')
        require(r['complete']==bool(r['pages'] and not token), 'range completion mismatch')
    days=state['days']
    require(isinstance(days,list) and days==sorted(set(days)) and len(days) in (0,state['sessions']), 'invalid selected sessions')
    if days:
        calendar=state['ranges'].get('trading_calendar',{})
        require(calendar.get('complete') is True,'calendar incomplete')
        rows=[row for ref in calendar['pages'] for row in json.loads(read('jquants/'+ref['path']))['data']]
        by_day={row['Date']:str(row.get('HolDiv')) for row in rows}
        require(len(by_day)==len(rows) and set(by_day.values()) <= {'0','1','2','3'},'invalid calendar')
        expected=[d for d,h in sorted(by_day.items()) if h in {'1','2'}][-state['sessions']:]
        require(days==expected,'selected sessions differ from calendar')
    require(isinstance(state['tasks'],dict), 'invalid history receipts')
    for task, sha in state['tasks'].items():
        name,day=task.split(':');require(name in {'equities_master','equities_daily'} and day in days,'unexpected history task')
        require(re.fullmatch('[0-9a-f]{64}',sha) is not None,'invalid partition hash')
        blob=read(f'jquants/datasets/{name}/{day}/revisions/{sha}.json');require(ingest.digest(blob)==sha,'history receipt corruption')
    if state['status']=='COMPLETE':
        require(len(state['tasks'])==2*state['sessions'] and state['ranges']['topix_daily']['complete'], 'incomplete campaign')


class HistoryClient(ingest.Client):
    def __init__(self,state,save,check_resources,**kwargs):
        super().__init__(plan='light',retries=0,max_response_bytes=resources.MAX_RESPONSE,**kwargs)
        self.state,self.save,self.check_resources=state,save,check_resources
    def _request(self,endpoint,params):
        s=self.state
        if len(s['batches'][-1])>=s['batch_limit'] or sum(map(len,s['batches']))>=600:
            raise ingest.RequestBudgetExhausted('bounded history budget')
        require(endpoint in {BY_NAME[n].endpoint for n in NAMES},'history endpoint not allowed')
        self.check_resources()
        previous=next((b[-1] for b in reversed(s['batches']) if b),None)
        if previous:
            elapsed=(datetime.now(daily.JST)-daily.aware(previous['reserved_at'])).total_seconds()
            self.sleep(max(0,self.interval-elapsed))
        s['batches'][-1].append({'endpoint':endpoint,'query_sha256':ingest.digest(ingest.encoded(params)),
                              'reserved_at':datetime.now(daily.JST).isoformat()})
        self.save()  # Ambiguous crashes and failures remain charged.
        result=super()._request(endpoint,params)
        if len(ingest.encoded(result))>resources.MAX_RESPONSE:raise ingest.ApiError(200,'response_too_large')
        return result


def run(root, *, as_of, sessions=280, batch=1, transport=None, sleep=None):
    root=Path(root).resolve();private=(root/'operations/private').resolve();source=private/'market-data'
    config_path=private/'source-config.json';config=ingest.read_json(config_path);c=config['jquants'];settings=c['ingestion']
    require(c['plan']=='light' and c.get('addons')==[] and c['enabled'] is True and
            c['base_url']==BASE_URL and c['api_key_env']=='JQUANTS_API_KEY' and settings['enabled'] is True,'history Light contract mismatch')
    require(type(settings['max_requests_per_run']) is int and 1<=settings['max_requests_per_run']<=12,'invalid configured budget')
    require(settings['archive_root']=='operations/private/market-data/jquants' and
            config['price_source']['archive_root']=='operations/private/market-data/daily-prices','unexpected history storage')
    require(type(sessions) is int and 1<=sessions<=280 and type(batch) is int and 1<=batch<=50,'invalid history bounds')
    require(date.today()-timedelta(days=3*365)<=as_of<datetime.now(daily.JST).date(),'history outside bounded completed-date window')
    with daily.workspace_lock(private):
        require(source.is_dir(),'restore adopted archive before history collection')
        codec.safe_tree(source);archive=source/'jquants'
        with ingest.archive_lock(archive):
            members={p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()}
            codec.validate_archive(members,lambda n:(source/n).read_bytes())
            for p in (archive/'runs').glob('*.json'):
                require(daily.read_state(p)['status'] in {'COLLECTED','HOLIDAY'},'unfinished daily run blocks history')
            for p in (archive/'datasets').glob('*/*/checkpoint.json'):
                daily.validate_checkpoint(archive,p.parent.parent.name,date.fromisoformat(p.parent.name))
                daily.recover_published_checkpoint(archive,p.parent.parent.name,date.fromisoformat(p.parent.name))
            path=archive/'history/state.json';state=ingest.read_json(path)
            cfg=ingest.digest(config_path.read_bytes())
            if state is None:
                require(batch==1,'missing history journal; cannot recreate a later batch')
                require(not (archive/'history').exists() and not (archive/'history-marker.json').exists(),'history journal missing; do not reset campaign')
                state={'version':1,'as_of':as_of.isoformat(),'sessions':sessions,'config_sha256':cfg,
                       'batch_limit':settings['max_requests_per_run'],'batches':[[]],'ranges':{},'days':[],
                       'tasks':{},'status':'RUNNING','retry_not_before':None,'resource_checks':{}}
            else:
                validate_history(state,lambda n:(source/n).read_bytes())
                require((state['as_of'],state['sessions'],state['config_sha256'],state['batch_limit'])==
                        (as_of.isoformat(),sessions,cfg,settings['max_requests_per_run']),'history campaign changed')
            def save():
                validate_history(state,lambda n:(source/n).read_bytes());ingest.atomic_json(path,state)
            def result():
                return {'status':state['status'],'batch':len(state['batches']),'requests_used':sum(map(len,state['batches'])),
                        'sessions_completed':len(state['tasks'])//2,'readiness':'NOT_EVALUATED','remote_saved':False}
            # A crash can leave the final reservation durable but not its outcome.
            # Never refund it or call the batch successfully exhausted: PAUSED
            # remains ineligible for automatic publication. An explicitly selected
            # next batch can resume the existing checkpoints within the total cap.
            if state['status']=='RUNNING' and len(state['batches'][-1])>=state['batch_limit']:
                state['status']='EXHAUSTED' if len(state['batches'])>=50 or sum(map(len,state['batches']))>=600 else 'PAUSED'
                save()
            if state['status'] in {'COMPLETE','EXHAUSTED','BLOCKED'}:return result()
            if state['retry_not_before'] and datetime.now(daily.JST)<daily.aware(state['retry_not_before']):return result()
            if batch==len(state['batches'])+1:
                require(state['status'] in {'BATCH_EXHAUSTED','PAUSED'} and len(state['batches'][-1])==state['batch_limit'],'previous batch is not exhausted')
                require(sum(map(len,state['batches']))<600,'campaign exhausted')
                state['batches'].append([])
            else:require(batch==len(state['batches']),'batch identity mismatch')
            if len(state['batches'][-1])>=state['batch_limit']:return result()
            save()
            marker=archive/'history-marker.json'
            if not marker.exists():ingest.atomic_json(marker,{'as_of':state['as_of'],'sessions':state['sessions']})
            require(ingest.read_json(marker)=={'as_of':state['as_of'],'sessions':state['sessions']},'history marker mismatch')
            kwargs={'transport':transport}
            if sleep is not None:kwargs['sleep']=sleep
            def check_resources():
                report=resources.inspect(source,private/'market-data-staging')
                state['resource_checks'][str(len(state['batches']))]=report;save();resources.enforce(report)
            client=HistoryClient(state,save,check_resources,**kwargs)
            def range_rows(name,start):
                r=state['ranges'].setdefault(name,{'query':{'from':start,'to':state['as_of']},'pages':[],
                                                  'next_key':None,'seen_keys':[],'complete':False})
                while not r['complete']:
                    query=dict(r['query'])
                    if r['next_key']:query['pagination_key']=r['next_key']
                    payload=client.get(BY_NAME[name].endpoint,query);rows=payload.get('data');token=payload.get('pagination_key')
                    require(isinstance(rows,list) and all(isinstance(x,dict) for x in rows),'invalid range response')
                    require(token is None or isinstance(token,str),'invalid pagination')
                    require(not token or token not in r['seen_keys'],'pagination cycle')
                    require(all(start<=row.get('Date','')<=state['as_of'] for row in rows),'out of range rows')
                    r['pages'].append(ingest.save_object(archive,payload));r['next_key']=token or None
                    if token:r['seen_keys'].append(token)
                    r['complete']=not bool(token);save()
                return [row for ref in r['pages'] for row in ingest.checked_payload(archive,ref)['data']]
            try:
                state['status']='RUNNING';state['retry_not_before']=None;save()
                rows=range_rows('trading_calendar',(as_of-timedelta(days=550)).isoformat())
                by_day={r['Date']:str(r.get('HolDiv')) for r in rows}
                require(len(by_day)==len(rows) and set(by_day.values())<={'0','1','2','3'},'invalid calendar')
                # Calendar gaps must not silently change the trading-session denominator.
                require(all((as_of-timedelta(days=i)).isoformat() in by_day for i in range(551)),'calendar date missing')
                days=[d for d,h in sorted(by_day.items()) if h in {'1','2'}][-sessions:]
                require(len(days)==sessions,'insufficient calendar sessions');state['days']=days;save()
                topix=range_rows('topix_daily',days[0]);require(len({r['Date'] for r in topix})==len(topix),'duplicate TOPIX dates')
                require(set(days)<={r['Date'] for r in topix},'TOPIX session missing')
                require(all(all(not isinstance(r.get(k),bool) and math.isfinite(float(r[k])) and float(r[k])>0 for k in ('O','H','L','C')) for r in topix),'invalid TOPIX values')
                for day_text in days:
                    day=date.fromisoformat(day_text)
                    for name in ('equities_master','equities_daily'):
                        key=name+':'+day_text
                        checkpoint=archive/'datasets'/name/day_text/'checkpoint.json'
                        if key in state['tasks'] and not checkpoint.exists():continue
                        existing=daily.partition(archive,name,day)
                        if checkpoint.exists() or existing is None or not existing[1]:
                            ingest.collect_partition(archive,BY_NAME[name],day,client)
                            existing=daily.partition(archive,name,day)
                        require(existing and existing[1] and all(r.get('Date')==day_text for r in existing[1]),'missing or invalid daily partition')
                        state['tasks'][key]=existing[2];save()
                    ingest.publish_snapshot(root,archive,day)
                state['status']='COMPLETE'
            except resources.ResourceLimit:
                state['status']='PAUSED'
            except ingest.RequestBudgetExhausted:
                state['status']='EXHAUSTED' if len(state['batches'])>=50 or sum(map(len,state['batches']))>=600 else 'BATCH_EXHAUSTED'
            except ingest.ApiError as error:
                state['status']='BLOCKED' if error.status in {200,401,403} else 'PAUSED'
                if state['status']=='PAUSED' and len(state['batches'][-1])>=state['batch_limit']:
                    state['status']='EXHAUSTED' if len(state['batches'])>=50 else 'PAUSED'
                if error.status==429:state['retry_not_before']=(datetime.now(daily.JST)+timedelta(seconds=120)).isoformat()
            except (ValueError,KeyError,TypeError,OSError,ingest.IngestionError):
                state['status']='BLOCKED'
            save();return result()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True)
    p.add_argument('--as-of',type=date.fromisoformat,required=True);p.add_argument('--sessions',type=int,default=280)
    p.add_argument('--batch',type=int,required=True);a=p.parse_args()
    try:
        resources.constrain_memory();daily.checkout_preflight(a.root);result=run(a.root,as_of=a.as_of,sessions=a.sessions,batch=a.batch)
    except Exception:result={'status':'BLOCKED','error':'history_preflight_or_validation','remote_saved':False}
    print(json.dumps(result));return 0 if result['status']=='COMPLETE' else 2


if __name__=='__main__':raise SystemExit(main())
