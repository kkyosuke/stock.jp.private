"""External operator evidence. Never creates, edits, or invents an attestation."""
import base64
from datetime import datetime,timezone
import json
try:
    from scripts import actions_daily_remote as r, actions_publication as pub, market_data_git as codec
except ModuleNotFoundError:
    import actions_daily_remote as r, actions_publication as pub, market_data_git as codec

HANDOVER='.github/stock-automation-handover.json'


def verify(request,scope,pin,*,main,initialized=False,now=None):
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
              ('controller','scheduler_id','enabled','active_run_ids','observed_at','epoch','observed_by')),
              'controller source evidence differs from handover receipt')
    observed=datetime.fromisoformat(receipt['observed_at'])
    r.require(observed.tzinfo is not None and observed<=now,'invalid handover clock')
    if not initialized:r.require((now-observed).total_seconds()<=900,'bootstrap needs a fresh controller observation')
    # After initialization the same epoch is fenced by the durable writer and
    # legacy CLI guard. This does not pretend to poll ChatGPT from GitHub.
    return dict(scheduler_id=receipt['scheduler_id'],enabled=False,active_writers=0,
                next_writer=receipt['next_writer'],observed_at=now.isoformat())
