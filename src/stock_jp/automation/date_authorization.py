"""Separately pinned scope for main-adopted, one-use acquisition allowances.

Existing scope/owner gates are untouched. Both Actions use the SAME deployment
pin, so this new manifest's pin cannot activate the old checkpoint Action.
"""
from stock_jp.workspace import ACQUISITION_SCOPE, ACQUISITION_WORKFLOW
from datetime import datetime
import json
import re
import stock_jp.automation.daily_remote as r
import stock_jp.market_data.storage as codec
import stock_jp.automation.authorization as auth
import stock_jp.automation.owner as owner
import stock_jp.automation.publication as pub
import stock_jp.automation.date_git as git
import stock_jp.automation.date_grants as grants

MANIFEST = ACQUISITION_SCOPE
WORKFLOW = ACQUISITION_WORKFLOW


def run(request, context):
    owner.manual_context(context)
    r.require(context.get('repository') == r.REPO and context.get('ref') == 'refs/heads/main', 'untrusted manual Action')
    r.sha(context['sha'])
    r.require(all(type(context.get(k)) is int and context[k] > 0 for k in ('run_id','run_attempt')), 'run identity missing')
    value = request('GET', pub.PREFIX+'/actions/runs/'+str(context['run_id'])+'/attempts/'+str(context['run_attempt']), None)
    r.require(value.get('id') == context['run_id'] and value.get('run_attempt') == context['run_attempt']
              and value.get('head_sha') == context['sha'] and value.get('head_branch') == 'main'
              and value.get('path') == WORKFLOW and value.get('event') == 'workflow_dispatch'
              and value.get('repository', {}).get('full_name') == r.REPO
              and value.get('actor', {}).get('login') == owner.MANUAL_ACTOR
              and value.get('triggering_actor', {}).get('login') == owner.MANUAL_ACTOR
              and value.get('status') == 'in_progress', 'current GitHub run identity mismatch')


def transition(before, after, read_before, read_after):
    """Every first-parent main commit must preserve code, payloads and charges."""
    for path in before.keys() | after.keys():
        if before.get(path) == after.get(path): continue
        r.require(after.get(path, ())[:2] == ('100644','blob'), 'deleted or unsafe adopted file')
        if path == MANIFEST: continue
        if path == grants.LEDGER:
            grants.extends(json.loads(read_before(path)), json.loads(read_after(path)))
        else:
            r.require(path.startswith(grants.ROOT) and re.fullmatch(
                re.escape(grants.ROOT)+r'[0-9]{4}-[0-9]{2}-[0-9]{2}/(?:result\.json|(?:trading_calendar|equities_master|equities_daily)/[0-9]{3}\.json)', path)
                      and path not in before, 'main changed outside reviewed data-only scope')


def verify(request, context, pin, now):
    r.require(isinstance(pin, str) and re.fullmatch('[0-9a-f]{64}', pin), 'reviewed approval pin missing')
    repo = request('GET', pub.PREFIX, None)
    r.require(repo.get('private') is True and repo.get('full_name') == r.REPO, 'private repository mismatch')
    run(request, context)
    main = git.ref(request, 'main'); r.sha(main)
    tree = auth.complete_tree(request, main)
    raw = git.read(request, tree, MANIFEST)
    r.require(codec.digest(raw) == pin, 'approval differs from reviewed deployment pin')
    scope = json.loads(raw)
    r.require(scope.get('version') == 1 and scope.get('enabled') is True
              and scope.get('writer') == 'date-grant-v1' and scope.get('repository') == r.REPO
              and scope.get('workflow') == WORKFLOW and scope.get('assurance') == 'retired-external-cooperative-github'
              and scope.get('data_publication_authorized') is True and scope.get('reservation_authorized') is True, 'date scope disabled or incorrect')
    r.require((scope.get('mode'),scope.get('submission'),scope.get('plan'),scope.get('addons')) ==
              ('PAPER','HUMAN_ONLY','light',[]) and scope.get('daily_limit') == 12
              and scope.get('history_limits') == [280,600,50,12]
              and scope.get('price_coverage_min') == .98 and scope.get('active_coverage_min') == 1., 'policy or limits changed')
    expiry = datetime.fromisoformat(scope['expires_at'])
    r.require(expiry.tzinfo is not None and now < expiry, 'approval expired')
    previous = git.read(request, tree, auth.MANIFEST)
    r.require(codec.digest(previous) == scope.get('previous_approval_sha256'), 'previous approval or debit changed')
    previous = json.loads(previous)
    r.require(previous.get('unresolved_campaigns') == [] and previous.get('legacy_debit') is not None, 'previous consumption is unresolved')
    number = scope.get('approval_pr')
    r.require(type(number) is int and number > 0, 'approval PR missing')
    pr = request('GET', pub.PREFIX+'/pulls/'+str(number), None)
    r.require(pr.get('merged') is True and pr['base']['ref'] == 'main' and pr['base']['repo']['full_name'] == r.REPO, 'approval PR not adopted')
    r.require(any(c['user']['login'] == owner.MANUAL_ACTOR and c['body'] == 'APPROVE_STOCK_AUTOMATION sha256='+pin
                  for c in pub.pages(request, '/issues/'+str(number)+'/comments')), 'independent owner approval statement missing')
    owner.retirement_receipt(request, scope, main, now)
    baseline = r.sha(scope['baseline']['main_sha'])
    cursor, current_tree = main, tree
    context_seen = False
    for _ in range(500):
        context_seen |= cursor == context['sha']
        if cursor == baseline: break
        commit = request('GET', pub.PREFIX+'/git/commits/'+cursor, None)
        r.require(commit.get('sha') == cursor and commit.get('parents'), 'baseline ancestry unavailable')
        parent = r.sha(commit['parents'][0]['sha'])
        parent_tree = auth.complete_tree(request, parent)
        transition(parent_tree, current_tree, lambda p:git.read(request,parent_tree,p), lambda p:git.read(request,current_tree,p))
        cursor, current_tree = parent, parent_tree
    else: raise r.RemoteStop('main ancestry verification bound exceeded')
    r.require(context_seen, 'run head outside approved main history')
    seed_raw = git.read(request, current_tree, grants.LEDGER)
    r.require(codec.digest(seed_raw) == scope['baseline']['ledger_sha256'], 'reviewed initial budget missing')
    seed = json.loads(seed_raw); grants.validate(seed)
    r.require(seed['legacy_debit'] == previous.get('legacy_debit') and not seed['grants'], 'initial recorded charges differ')
    # The initial cost floor covers every adopted daily-prices date, without
    # restoring or importing provider payload. Missing exact legacy daily journals
    # are charged at the full daily bound, explicitly NOT claimed as 12 sends.
    pointer = json.loads(git.read(request, current_tree, 'data/market/jquants/current.json'))
    r.require(pointer.get('version') == 2, 'plain adopted archive required')
    digest = pointer['generation']
    index_raw = git.read(request, current_tree, 'data/market/jquants/generations/' + digest[:2] + '/' + digest + '.json')
    r.require(codec.digest(index_raw) == digest, 'adopted index hash mismatch')
    adopted_dates = {m.group(1) for name in json.loads(index_raw)['files']
                     if (m := re.fullmatch(r'daily-prices/[0-9]{4}/([0-9]{4}-[0-9]{2}-[0-9]{2})\.csv', name))}
    r.require(seed['prior_charges'] == {d:12 for d in adopted_dates}
              and seed['prior_batches'] == len(adopted_dates), 'adopted daily cost floor missing')
    ledger = json.loads(git.read(request, tree, grants.LEDGER)); grants.extends(seed, ledger)
    config = json.loads(git.read(request, tree, 'config/sources.json'))['jquants']
    ingestion = config['ingestion']
    r.require(config.get('plan') == 'light' and config.get('addons') == [] and config.get('enabled') is True
              and config.get('base_url') == 'https://api.jquants.com/v2' and config.get('api_key_env') == 'JQUANTS_API_KEY'
              and ingestion.get('enabled') is True and type(ingestion.get('max_requests_per_run')) is int
              and 1 <= ingestion['max_requests_per_run'] <= 12, 'Light source contract mismatch')
    r.require(all(g['allowance'] <= ingestion['max_requests_per_run'] for g in ledger['grants']), 'grant exceeds source limit')
    policy = json.loads(git.read(request, tree, 'config/policy.json'))
    r.require(policy.get('operation_mode') == 'PAPER' and policy.get('broker_submission') == 'HUMAN_ONLY', 'runtime policy changed')
    r.require(git.ref(request, 'main') == main, 'main moved during approval verification')
    return {**scope, "request_limit":ingestion["max_requests_per_run"]}, main, tree, ledger
