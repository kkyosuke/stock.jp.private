"""Owner-initiated manual acquisition; approval gates remain default-off."""
import argparse
import json
import os
from pathlib import Path
import tempfile
try:
    from scripts import actions_driver as legacy, actions_owner as owner, actions_targets as targets
    from scripts import actions_daily_remote as r, actions_publication as pub, actions_authorization as auth
    from scripts import jquants_daily as daily, market_data_git as codec, actions_http as http, actions_rate as rate
    from scripts.actions_sharded import ShardedStore
    from scripts.actions_cache import ImmutableCache
except ModuleNotFoundError:
    import actions_driver as legacy, actions_owner as owner, actions_targets as targets
    import actions_daily_remote as r, actions_publication as pub, actions_authorization as auth
    import jquants_daily as daily, market_data_git as codec, actions_http as http, actions_rate as rate
    from actions_sharded import ShardedStore
    from actions_cache import ImmutableCache


class Driver(legacy.Driver):
    def execute(self, target_date):
        owner.manual_context(self.context)
        target = targets.day(target_date)
        r.require(target <= self.clock().astimezone(daily.JST).date(), 'future target_date forbidden')
        r.require(isinstance(self.pin, str) and len(self.pin) == 64, 'reviewed approval pin missing')
        main = self.request('GET', pub.PREFIX + '/git/ref/heads/main', None)['object']['sha']
        legacy.trusted_context(self.root, self.context, main)
        r.require(self.context['event'] == 'workflow_dispatch', 'target Action requires explicit date dispatch')
        raw = legacy.content(self.request, auth.MANIFEST, main)
        r.require(codec.digest(raw) == self.pin, 'approval hash differs')
        scope = json.loads(raw)
        r.require(scope.get('version') == 2, 'target-date scope review required before activation')
        approved_campaign = targets.campaign(scope)
        initial = r.sha(scope.get('checkpoint_initial_sha'))
        # A missing ref is a hard stop. Runtime never creates a replacement ledger.
        head = self.request('GET', pub.PREFIX + '/git/ref/' + r.REF, None)['object']['sha']
        owner.ancestry(self.request, initial, head)
        binding = dict(manifest_sha256=self.pin, baseline=scope['baseline'],
                       activation_id=scope['activation_id'], checkpoint_initial_sha=initial)
        current = owner.identity(self.context, scope)
        with tempfile.TemporaryDirectory(prefix='stock-target-') as tmp:
            work = Path(tmp)
            original = self.request
            self.request = ImmutableCache(original, work/'http-cache')
            try:
                if head != initial and hasattr(self.request, 'restore'):
                    legacy.restore_read_budget(self.request, head, self.writer, binding)
                # Fixed receipt is checked here, never rewritten with a fresh timestamp.
                owner.retirement(self.request, scope, self.pin, main, self.clock())
                owner.run(self.request, current, active=True)
                adopted, archive = work/'adopted', work/'archive'
                pub.download_archive(self.request, main, archive)
                codec.restore(archive, adopted)
                # Profile is immutable once a date journal exists. The provisional
                # mode only verifies the allowed modes; actual date planning follows.
                r.require(set(scope.get('modes', [])) == {'daily', 'history'}, 'common Action scopes missing')
                scope, facts = auth.verify_scope(self.request, approved_manifest_sha256=self.pin, mode='daily',
                    writer=self.writer, source=adopted, now=self.clock(), clock=self.clock,
                    allow_pending_publication=True,
                    handover_probe=lambda _:dict(retirement_verified=True, github_run_verified=True))
                store = ShardedStore(self.request, authorization=facts, cache=work/'cache')
                revision, document = store.read()
                if hasattr(self.request, 'restore'): self.request.restore(document.get('github_budget'))
                owner.ancestry(self.request, initial, revision)
                r.require(document['writer'] == self.writer, 'checkpoint writer mismatch')
                owner.healthy(document)
                if document.get('approval_binding'):
                    r.require(document['approval_binding'] == binding, 'manifest cannot reset or rebind ledger')
                else:
                    r.require(revision == initial and not document.get('targets') and not document.get('run_owner'),
                              'only approved initial checkpoint can be bound')
                    document['approval_binding'] = binding
                    document['targets'] = dict(campaign=approved_campaign,
                        profiles={day:'daily' for day in document['runs']}, batches=[[]], ranges={})
                r.require(document['targets']['campaign'] == approved_campaign, 'campaign change cannot reset budget')
                profile = document['targets']['profiles'].get(target_date)
                if profile is None:
                    profile = 'daily' if target == self.clock().astimezone(daily.JST).date() else 'history'
                if profile == 'history':
                    r.require(not document.get('history'),
                              'legacy history budget mapping requires separate review; no fresh campaign reset')
                    r.require(approved_campaign['start_date'] <= target_date <= approved_campaign['as_of']
                              and targets.day(approved_campaign['as_of']) < self.clock().astimezone(daily.JST).date(),
                              'target outside approved past-date campaign')
                history_check = (auth.AdoptedHistory(adopted, legacy.immutable_history(adopted)).check
                                 if (adopted/'jquants/history/state.json').exists() else r.reject_history)
                if document.get('adopted_history'):
                    history_check = auth.AdoptedHistory(adopted, document['adopted_history']).check
                if any(v['phase'] == 'RESTORED' for v in document['runs'].values()):
                    expected = work/'checkpoint-evidence'
                    store.restore(document['snapshot'], expected, history_check)
                    targets.verify_adopted_targets(document, expected, adopted)
                runtime = work/'runtime'
                private = runtime/'operations/private'
                private.mkdir(parents=True)
                for name in ('source-config.json', 'operation-policy.json'):
                    codec.atomic(private/name, legacy.content(self.request, 'operations/private/'+name, main))
                policy = json.loads((private/'operation-policy.json').read_bytes())
                r.require(policy['operation_mode'] == 'PAPER' and policy['broker_submission'] == 'HUMAN_ONLY',
                          'runtime policy changed')
                codec.atomic(runtime/auth.MANIFEST, raw)
                checkpoint = targets.TargetCheckpoint(store, writer=self.writer, authorization=facts,
                    history_check=history_check, clock=self.clock, target_date=target_date, profile=profile)
                checkpoint.revision, checkpoint.document = revision, document
                checkpoint.source = private/'market-data'
                transaction = checkpoint.document.get('transaction')
                if transaction:
                    r.require(transaction['mode'] == 'target' and transaction['day'] == target_date,
                              'resume existing target publication first')
                    pending = pub.pages(self.request, '/pulls?state=open')
                    r.require(all(not p['head']['ref'].startswith('daily/market/') or
                        p['head']['ref'] == 'daily/market/'+target_date
                        and p['head']['sha'] == transaction['head']
                        and transaction['number'] is not None and p['number'] == transaction['number']
                        and p['head']['repo']['full_name'] == r.REPO
                        and p['base']['repo']['full_name'] == r.REPO and p['base']['ref'] == 'main'
                        for p in pending), 'another market publication is unresolved')
                current = owner.claim(checkpoint, scope, self.context)
                observation = {}
                def observe():
                    observation['at'] = owner.observe(checkpoint, current)
                def fresh():
                    r.require('at' in observation and 0 <= (self.clock()-observation['at']).total_seconds() <= 60,
                              'GitHub owner observation expired at send boundary')
                    r.gate(facts, now=self.clock())
                facts.update(handover_check=observe, publication_io_prepare=observe, publication_io_check=fresh)
                if transaction:
                    checkpoint.day = target_date
                    store.restore(document['snapshot'], checkpoint.source, history_check)
                    return self.publish(checkpoint, main)
                # A same-name closed/rejected PR is not proof that a new request
                # succeeded. Ref collisions are rejected before provider I/O.
                prs = pub.pages(self.request, '/pulls?state=all')
                restored = document['runs'].get(target_date, {}).get('phase') == 'RESTORED'
                r.require(all(not p['head']['ref'].startswith('daily/market/') or
                              p['state'] != 'open' for p in prs), 'unresolved market PR')
                r.require(restored or not any(p['head']['ref'] == 'daily/market/'+target_date for p in prs),
                          'prior same-date PR requires explicit review')
                try:
                    report = daily.run(runtime, at=self.clock(), transport=self.provider, sleep=self.sleep, remote=checkpoint)
                except rate.BudgetPause:
                    if not checkpoint.poisoned:
                        checkpoint.document['snapshot'] = checkpoint.capture_snapshot()
                        checkpoint.commit()
                    return {'status':'PAUSED_GITHUB_BUDGET', 'target_date':target_date}
                if report['status'] not in {'COLLECTED', 'HOLIDAY'}: return report
                if checkpoint.document['runs'][target_date]['phase'] == 'RESTORED':
                    return {'status':'ALREADY_RESTORED', 'target_date':target_date}
                with pub.prepare_candidate(self.root, checkpoint.source, target_date, main) as candidate:
                    if candidate['status'] == 'NOOP':
                        pub.mark_adopted(checkpoint, verified_main=main, verified_generation=candidate['generation'],
                                         actual_generation=candidate['generation'])
                        return {'status':'RESTORED', 'target_date':target_date, 'generation':candidate['generation']}
                    checkpoint.document['transaction'] = dict(mode='target', day=target_date, base=main,
                        tree=candidate['tree_sha'], generation=candidate['generation'], stage='BLOBS', index=0,
                        head=None, number=None)
                    checkpoint.commit()
                return self.publish(checkpoint, main)
            finally:
                self.request = original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-date', required=True)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        if os.environ.get('STOCK_ACTIONS_ENABLED') != 'true':
            print(json.dumps({'status':'BLOCKED', 'reason':'execution_disabled'}))
            return 2
        try:
            from scripts.history_resources import constrain_memory
        except ModuleNotFoundError:
            from history_resources import constrain_memory
        constrain_memory()
        request = rate.BudgetedTransport(http.GitHubTransport(os.environ.get('AUTOMATION_PAT')))
        provider = http.ProviderTransport(os.environ.get('JQUANTS_API_KEY'))
        context = dict(repository=os.environ.get('GITHUB_REPOSITORY'), ref=os.environ.get('GITHUB_REF'),
            sha=os.environ.get('GITHUB_SHA'), event=os.environ.get('GITHUB_EVENT_NAME'),
            actor=os.environ.get('GITHUB_ACTOR'), triggering_actor=os.environ.get('GITHUB_TRIGGERING_ACTOR'),
            run_id=int(os.environ.get('GITHUB_RUN_ID', '0')), run_attempt=int(os.environ.get('GITHUB_RUN_ATTEMPT', '0')))
        result = Driver(args.root, request, provider, pin=os.environ.get('STOCK_ACTIONS_APPROVAL_SHA256'),
                        writer='actions-v1', context=context).execute(args.target_date)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] in {'RESTORED','ALREADY_RESTORED','WAIT_CI','PAUSED_GITHUB_BUDGET','WAIT_TARGET_ORDER'} else 2
    except r.RemoteStop as error:
        # RemoteStop messages are fixed, sanitized precondition/status strings.
        print(json.dumps({'status':'BLOCKED', 'reason':str(error)}))
        return 2
    except Exception:
        print(json.dumps({'status':'BLOCKED', 'reason':'target_scope_owner_or_checkpoint_verification_failed'}))
        return 2


if __name__ == '__main__': raise SystemExit(main())
