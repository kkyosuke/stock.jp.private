"""Read-only verification of the mandatory target-day shared budget evidence."""
from datetime import datetime
import json
import re

from stock_jp.automation import nightly as n, provider_budget as budget
from stock_jp.market_data.jquants import daily, ingest


def validate_budget(root, journal, *, day, cutoff):
    try:
        _validate(root, journal, day=day, cutoff=cutoff)
    except (KeyError, TypeError, ValueError, ingest.IngestionError, n.RemoteStop) as error:
        raise ValueError('offline budget evidence rejected: ' + str(error)) from None


def _validate(root, journal, *, day, cutoff):
    required = {'version', 'runs', 'files', 'github_budget', 'scheduled_targets',
                'provider_debit_bases', 'paper_requests'}
    n.require(isinstance(journal, dict) and set(journal) == required
              and type(journal['version']) is int and journal['version'] == 1, 'invalid checkpoint schema')
    for key in required - {'version', 'github_budget'}:
        n.require(isinstance(journal[key], dict), 'invalid checkpoint mapping')
    n.require(all(day in journal[key] for key in ('runs', 'provider_debit_bases', 'paper_requests')),
              'target-day daily, debit basis and PAPER reservations are mandatory')
    for key, state in journal['runs'].items():
        daily.validate_state(state)
        n.require(key == state['run_date'] and type(state['daily_runner_version']) is int,
                  'daily identity/version differs')
    state = journal['runs'][day]
    settings = json.loads((root / 'config/sources.json').read_text())['jquants']['ingestion']
    n.require(type(settings['max_requests_per_run']) is int
              and state['budget'] == settings['max_requests_per_run']
              and state['config_sha256'] == ingest.digest((root / 'config/sources.json').read_bytes()),
              'target source configuration or budget differs')
    n.require(state['status'] in {'HOLIDAY', 'COLLECTED'}
              and all(state[k] is None for k in ('error', 'http_status', 'retry_not_before')),
              'target daily acquisition is incomplete or inconsistent')
    n.require(state['data_date'] == (day if state['status'] == 'COLLECTED' else None)
              and state['tasks'].get('trading_calendar:' + day, {}).get('rows') == 1,
              'daily completion is missing its calendar receipt or has an inconsistent data date')
    now, selected = datetime.now(daily.JST), daily.aware(cutoff)
    started = daily.aware(state['started_at'])
    n.require(started <= selected <= now, 'invalid target chronology')
    cutoffs = set()
    for run_id, binding in journal['scheduled_targets'].items():
        n.require(re.fullmatch(r'[1-9][0-9]*', run_id) is not None and isinstance(binding, dict)
                  and {'date', 'resolved_at'} <= set(binding) <= {'date', 'resolved_at', 'paper_cutoff_at_jst'},
                  'invalid scheduled target identity')
        resolved = daily.aware(binding['resolved_at'])
        n.require(n.scheduled_day(resolved).isoformat() == binding['date'] and resolved <= now,
                  'invalid scheduled target date')
        if 'paper_cutoff_at_jst' in binding:
            frozen = daily.aware(binding['paper_cutoff_at_jst'])
            n.require(n.scheduled_day(frozen).isoformat() == binding['date'] and frozen <= now,
                      'invalid scheduled PAPER cutoff')
            if binding['date'] == day:
                cutoffs.add(binding['paper_cutoff_at_jst'])
    n.require(cutoffs == {cutoff}, 'offline logical target differs from pinned checkpoint')
    budget.validate_charges(journal)
    n.require((root / 'operations/acquisition/date-grants.json').is_file(), 'historical debit ledger is required')
    historical = budget.historical_debit(root, day)
    n.require(all(journal['provider_debit_bases'][day][k] == v for k, v in historical.items()),
              'historical debit basis differs from preserved ledger')
    basis = journal['provider_debit_bases'][day]
    n.require(basis['initial_daily_count'] == 0 or historical['prior_requests'] + historical['legacy_requests'] == 0,
              'historical and initial daily reservation overlap is unknown')
    n.require(budget.shared_usage(journal, day) <= state['budget'] <= 12, 'shared cumulative allowance exceeded')
    previous = started
    for receipt in state['requests']:
        reserved = daily.aware(receipt['reserved_at'])
        n.require(previous <= reserved <= selected, 'invalid daily reservation chronology')
        previous = reserved
    previous = selected
    for receipt in journal['paper_requests'][day]:
        reserved = daily.aware(receipt['reserved_at'])
        binding = journal['scheduled_targets'][str(receipt['action_run_id'])]
        n.require(previous <= reserved <= now and daily.aware(binding['resolved_at']) <= reserved
                  and binding['date'] == day and binding.get('paper_cutoff_at_jst') == cutoff,
                  'PAPER reservation is not bound to the logical target/cutoff')
        previous = reserved
