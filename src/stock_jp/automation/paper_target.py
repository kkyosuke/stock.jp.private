"""Bind PAPER preparation to the adopted daily target, retaining real observation time."""
import json

from stock_jp.automation import nightly as n
from stock_jp.market_data.jquants import ingest


def resolve(root, checkpoint, now):
    """Validate adoption before freezing the first PAPER cutoff in the shared journal."""
    cp = checkpoint
    n.require(cp.revision is not None, "PAPER target requires a durable daily checkpoint")
    cp.before_provider_io()  # Read-only active-writer check before any checkpoint mutation.
    cp.bind_schedule(now)
    day = cp.target_day.isoformat()
    binding = cp.document['scheduled_targets'][str(cp.context['run_id'])]
    state = cp.document['runs'].get(day)
    n.require(state and state['status'] in {'COLLECTED', 'HOLIDAY'}, 'PAPER target has no completed daily acquisition')
    name = n.RECORDS + day + '.json'
    main_tree = cp.git.tree(cp.main)
    local = root / name
    n.require(name in main_tree and local.is_file(), 'PAPER target has not been adopted on main')
    raw = local.read_bytes()
    n.require(n.git_blob(raw) == main_tree[name], 'local PAPER adoption record differs from main')
    record = json.loads(raw)
    for key in ('run_date', 'status', 'data_date', 'requests', 'tasks', 'budget', 'config_sha256'):
        n.require(record.get(key) == state.get(key), 'PAPER target adoption differs from daily checkpoint')
    n.require(record['config_sha256'] == ingest.digest((root / 'config/sources.json').read_bytes()),
              'PAPER target source configuration changed')
    saved = root / 'operations/runs' / day / 'source-scan-cutoff.json'
    cutoffs = {b['paper_cutoff_at_jst'] for b in cp.document['scheduled_targets'].values()
               if b['date'] == day and 'paper_cutoff_at_jst' in b}
    n.require(len(cutoffs) <= 1, 'conflicting PAPER cutoffs for the logical day')
    cutoff = next(iter(cutoffs), None)
    if saved.exists():
        previous = json.loads(saved.read_text())['cutoff_at_jst']
        n.require(cutoff is None or cutoff == previous, 'saved PAPER cutoff differs from logical target')
        cutoff = previous
    if cutoff is None:
        inbox = cp.git.ref(n.PAPER_CHECKPOINT, optional=True)
        if inbox:
            tree = cp.git.tree(inbox)
            n.require(n.PAPER_JOURNAL in tree, 'PAPER inbox journal missing')
            previous = json.loads(cp.git.read(tree[n.PAPER_JOURNAL]))
            n.require(previous.get('version') == 1 and previous.get('operation_mode') == 'PAPER'
                      and previous.get('broker_submission') == 'HUMAN_ONLY', 'invalid PAPER inbox')
            cutoff_name = f'operations/runs/{day}/source-scan-cutoff.json'
            if previous['day'] == day and previous['config_sha256'] == record['config_sha256']:
                item = previous['files'].get(cutoff_name)
                if item:
                    name = n.PAPER_RAW_PREFIX + item['sha256'] + '/' + cutoff_name
                    n.require(tree.get(name) == item['blob'] and cp.git.modes[inbox].get(name) == '100644',
                              'PAPER inbox cutoff member missing')
                    raw = cp.git.read(item['blob'])
                    n.require(len(raw) == item['bytes'] and ingest.digest(raw) == item['sha256'],
                              'PAPER inbox cutoff checksum mismatch')
                    cutoff = json.loads(raw)['cutoff_at_jst']
    cutoff = cutoff or now.isoformat(timespec='seconds')
    observed = n.daily.aware(cutoff)
    n.require(observed <= now and n.scheduled_day(observed).isoformat() == day,
              'PAPER cutoff must belong to the bound publication night; cannot invent a past observation')
    if 'paper_cutoff_at_jst' not in binding:
        binding['paper_cutoff_at_jst'] = cutoff
        cp.observe()
        cp.flush()
    return {'day': day, 'cutoff_at_jst': cutoff, 'daily_status': record['status']}
