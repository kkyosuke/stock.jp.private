"""Manual date acquisition: reviewed main reservation, then one execution attempt."""
from stock_jp.workspace import project_root
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo
import stock_jp.automation.date_authorization as approval
import stock_jp.automation.date_grants as grants
import stock_jp.automation.date_git as remote
import stock_jp.automation.authorization as auth
import stock_jp.automation.daily_remote as r
import stock_jp.automation.http as http
import stock_jp.automation.publication as pub
import stock_jp.market_data.storage as codec
import stock_jp.automation.rate as rate
from stock_jp.automation.git import git


class Driver:
    def __init__(self, root, request, provider, context, pin, *, clock=None, sleep=time.sleep):
        self.root, self.request, self.provider, self.context, self.pin = Path(root), request, provider, context, pin
        self.clock = clock or (lambda:datetime.now(ZoneInfo('Asia/Tokyo')))
        self.sleep = sleep

    def execute(self, target):
        grants.day(target)
        scope, main, tree, ledger = approval.verify(self.request, self.context, self.pin, self.clock())
        r.require(git(self.root,'rev-parse','HEAD').decode().strip() == main
                  and not git(self.root,'status','--porcelain').strip(), 'checkout must be clean current main')
        r.require(git(self.root,'remote','get-url','origin').decode().strip() in
                  {'https://github.com/'+r.REPO+'.git','https://github.com/'+r.REPO,'git@github.com:'+r.REPO+'.git'}, 'execution remote differs')
        campaign = ledger['legacy_debit']['campaign']
        grants.check_target(target, campaign, self.clock().astimezone(ZoneInfo('Asia/Tokyo')).date())
        selected = next((g for g in ledger['grants'] if g['date'] == target), None)
        # Adopted results are verified from current main before returning NOOP or
        # allowing the next date. Missing/partial results never free the grant.
        for grant in ledger['grants']:
            path = grants.result_prefix(grant)+'result.json'
            if path in tree:
                state = json.loads(remote.read(self.request, tree, path))
                grants.verify_result(state, grant, lambda p:remote.read(self.request, tree, p))
                if grant == selected: return dict(status='ALREADY_ADOPTED', target_date=target, provider_requests=0, collection_status=state['status'], analysis_readiness='NOT_EVALUATED')
            else:
                r.require(grant == selected, 'previous grant needs adopted result or explicit reconciliation')
        prs = pub.pages(self.request, '/pulls?state=all')
        r.require(not any(p['state'] == 'open' and
            (p['head']['ref'].startswith('daily/market/') or
             p['head']['ref'].startswith('date-acquisition/') and not p['head']['ref'].endswith('/'+target))
            for p in prs), 'another data publication remains unresolved')
        if selected is None:
            proposed, grant = grants.propose(ledger, target, self.context)
            r.require(grant['allowance'] <= scope['request_limit'], 'reservation exceeds configured request limit')
            branch = remote.Branch(self.request, main, target, 'reserve')
            if branch.created:
                saved = auth.complete_tree(self.request, branch.revision)
                r.require(json.loads(remote.read(self.request, saved, grants.LEDGER)) == proposed, 'existing reservation differs; review required')
            else:
                r.require(not any(p['head']['ref'] == branch.branch for p in prs), 'prior reservation PR remains in history; no replacement')
                branch.save({grants.LEDGER:codec.encoded(proposed)})
            return dict(status='WAIT_RESERVATION_MERGE', target_date=target, next_attempt=grant['attempt'],
                        charged_allowance=grant['allowance'], provider_requests=0, collection_status='NOT_STARTED', analysis_readiness='NOT_EVALUATED', pull_request=branch.publish())
        branch = remote.Branch(self.request, main, target, 'result')
        if branch.created:
            candidate = auth.complete_tree(self.request, branch.revision)
            # No resume of sends. Only a fully saved result can resume PR creation.
            state = json.loads(remote.read(self.request, candidate, grants.result_prefix(selected)+'result.json'))
            grants.verify_result(state, selected, lambda p:remote.read(self.request, candidate, p))
            return dict(status='WAIT_RESULT_MERGE', target_date=target, provider_requests=0, collection_status=state['status'], analysis_readiness='NOT_EVALUATED', pull_request=branch.publish())
        r.require(not any(p['head']['ref'] == branch.branch for p in prs), 'result branch missing; grant remains charged')
        r.require((selected['run_id'],selected['attempt'],selected['head_sha']) ==
                  (self.context['run_id'],self.context['run_attempt'],self.context['sha']), 'one-use attempt grant mismatch; no replacement')
        previous = self.request('GET', pub.PREFIX+'/actions/runs/'+str(selected['run_id'])+'/attempts/'+str(selected['attempt']-1), None)
        r.require(previous.get('status') == 'completed' and previous.get('head_sha') == selected['head_sha'], 'previous attempt termination unconfirmed')
        def observe():
            start = self.clock()
            approval.run(self.request, self.context); branch.observe()
            r.require(self.clock() < datetime.fromisoformat(scope['expires_at'])
                      and 0 <= (self.clock()-start).total_seconds() <= 60, 'approval or live observation expired')
        observe()
        state = grants.execute(selected, self.context, self.provider, branch.save, observe, self.clock, self.sleep)
        if state['status'] == 'INCOMPLETE':
            return dict(status='INCOMPLETE', target_date=target, provider_requests=len(state['requests']),
                        charged_allowance=selected['allowance'], raw_quality=state['raw_quality'],
                        analysis_readiness='NOT_EVALUATED')
        restored = auth.complete_tree(self.request, branch.revision)
        grants.verify_result(state, selected, lambda p:remote.read(self.request, restored, p))
        return dict(status='WAIT_RESULT_MERGE', target_date=target, provider_requests=len(state['requests']),
                    charged_allowance=selected['allowance'], collection_status=state['status'],
                    analysis_readiness='NOT_EVALUATED', pull_request=branch.publish())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-date', required=True)
    parser.add_argument('--root', type=Path, default=project_root())
    args = parser.parse_args()
    try:
        r.require(os.environ.get('STOCK_ACTIONS_ENABLED') == 'true', 'execution disabled')
        context = dict(repository=os.environ.get('GITHUB_REPOSITORY'), ref=os.environ.get('GITHUB_REF'),
            sha=os.environ.get('GITHUB_SHA'), event=os.environ.get('GITHUB_EVENT_NAME'),
            actor=os.environ.get('GITHUB_ACTOR'), triggering_actor=os.environ.get('GITHUB_TRIGGERING_ACTOR'),
            run_id=int(os.environ.get('GITHUB_RUN_ID','0')), run_attempt=int(os.environ.get('GITHUB_RUN_ATTEMPT','0')))
        # No provider request is made during reservation preparation.
        result = Driver(args.root, rate.BudgetedTransport(remote.Transport(os.environ.get('AUTOMATION_PAT'))),
            http.ProviderTransport(os.environ.get('JQUANTS_API_KEY')), context,
            os.environ.get('STOCK_ACTIONS_APPROVAL_SHA256')).execute(args.target_date)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] in {'WAIT_RESERVATION_MERGE','WAIT_RESULT_MERGE','ALREADY_ADOPTED'} else 2
    except r.RemoteStop as error:
        print(json.dumps(dict(status='BLOCKED', reason=str(error)))); return 2
    except Exception:
        print(json.dumps(dict(status='BLOCKED', reason='date_grant_verification_failed'))); return 2


if __name__ == '__main__': raise SystemExit(main())
