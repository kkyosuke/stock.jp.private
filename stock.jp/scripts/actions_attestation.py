"""External operator evidence. Never creates, edits, or invents an attestation."""
import base64
from datetime import datetime,timezone
import json
try:
    from scripts import actions_daily_remote as r, actions_publication as pub, market_data_git as codec
except ModuleNotFoundError:
    import actions_daily_remote as r, actions_publication as pub, market_data_git as codec

HANDOVER='.github/stock-automation-handover.json'


def verify(request,scope,pin,*,main,initialized=False,now=None,live_probe=None,clock=None):
    now=now or datetime.now(timezone.utc)
    number=scope.get('approval_pr')
    r.require(type(number) is int and number>0,'external approval PR missing')
    pr=request('GET',pub.PREFIX+'/pulls/'+str(number),None)
    r.require(pr['merged'] is True and pr['base']['ref']=='main'
              and pr['base']['repo']['full_name']==r.REPO,'approval PR not adopted')
    comments=pub.pages(request,'/issues/'+str(number)+'/comments')
    statement='APPROVE_STOCK_AUTOMATION sha256='+pin
    r.require(any(c['user']['login']=='kkyosuke' and c['body']==statement for c in comments),
              'independent owner approval statement missing')
    raw=request('GET',pub.PREFIX+'/contents/'+HANDOVER+'?ref='+main,None)
    content=base64.b64decode(''.join(raw['content'].split()),validate=True)
    r.require(codec.digest(content)==scope['handover_sha256'],'handover receipt differs from approved hash')
    receipt=json.loads(content)
    identity={'scheduler_id':scope['old_scheduler_id'],'owner':scope['old_writer'],
              'lease_id':scope['old_lease_id'],'epoch':scope['handover_epoch'],'next_writer':scope['writer']}
    r.require(all(isinstance(value,str) and value for value in identity.values()),'handover identity missing')
    r.require(all(receipt.get(key)==value for key,value in identity.items()),'handover identity mismatch')
    r.require(receipt.get('lease_state')=='released' and 'lease_owner' in receipt and receipt['lease_owner'] is None,
              'old writer lease is active or unobserved')
    r.require(receipt['controller']=='chatgpt-automations' and receipt['scheduler_id']==scope['old_scheduler_id']
              and receipt['enabled'] is False and receipt['active_run_ids']==[]
              and receipt['next_writer']==scope['writer'] and receipt['epoch']==scope['handover_epoch'],
              'old scheduler handover not independently attested')
    r.require(receipt.get('source_evidence_sha256') and receipt.get('observed_by')=='kkyosuke',
              'controller evidence identity missing')
    evidence=request('GET',pub.PREFIX+'/contents/.github/stock-automation-controller-evidence.json?ref='+main,None)
    evidence_raw=base64.b64decode(''.join(evidence['content'].split()),validate=True)
    r.require(codec.digest(evidence_raw)==receipt['source_evidence_sha256'],'controller source evidence hash mismatch')
    evidence_value=json.loads(evidence_raw)
    r.require(all(evidence_value.get(k)==receipt[k] for k in
              ('controller','scheduler_id','enabled','active_run_ids','observed_at','epoch','observed_by',
               'owner','lease_id','lease_state','lease_owner','next_writer')),
              'controller source evidence differs from handover receipt')
    return verify_live(scope,live_probe=live_probe,now=now,clock=clock)


def verify_live(scope,*,live_probe,now=None,clock=None):
    """Independent controller/lease read only: safe at the final HTTP boundary.

    GitHub approval/receipt reads belong to verify(), before request pacing.
    Never perform those paced reads after this final observation.
    """
    identity={'scheduler_id':scope['old_scheduler_id'],'owner':scope['old_writer'],
              'lease_id':scope['old_lease_id'],'epoch':scope['handover_epoch'],'next_writer':scope['writer']}
    r.require(all(isinstance(value,str) and value for value in identity.values()),'handover identity missing')
    r.require(callable(live_probe),'live independent scheduler/lease transport unavailable; activation blocked')
    try:observation=live_probe(dict(identity))
    except Exception:
        raise r.RemoteStop('independent scheduler/lease observation unavailable') from None
    r.require(isinstance(observation,dict) and all(observation.get(k)==v for k,v in identity.items()),
              'live handover identity mismatch')
    r.require(observation.get('controller')=='chatgpt-automations'
              and observation.get('observed_by')=='kkyosuke'
              and observation.get('enabled') is False and observation.get('active_run_ids')==[]
              and observation.get('lease_state')=='released' and 'lease_owner' in observation
              and observation['lease_owner'] is None,'old scheduler or lease is active or unobserved')
    try:observed=datetime.fromisoformat(observation['observed_at'])
    except (KeyError,TypeError,ValueError):
        raise r.RemoteStop('invalid independent scheduler/lease clock') from None
    checked_at=clock() if clock else now or datetime.now(timezone.utc)
    r.require(observed.tzinfo is not None and 0 <= (checked_at-observed).total_seconds() <= 60,
              'stale independent scheduler/lease observation')
    expiry=datetime.fromisoformat(scope['expires_at'])
    r.require(expiry.tzinfo is not None and checked_at<expiry,'approval expired at publication boundary')
    return dict(scheduler_id=observation['scheduler_id'],enabled=observation['enabled'],
                active_writers=len(observation['active_run_ids']),next_writer=observation['next_writer'],
                observed_at=observation['observed_at'],handover_identity=identity,
                lease_id=observation['lease_id'],lease_state=observation['lease_state'],
                lease_owner=observation['lease_owner'],owner=observation['owner'])
