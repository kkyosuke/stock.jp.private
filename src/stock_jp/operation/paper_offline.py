"""Prepare unreviewed primary evidence offline, without creating a canonical run."""
import argparse
import json
import re
import subprocess

from stock_jp.automation import nightly as n, paper_sources
from stock_jp.market_data import primary_reuse as reuse
from stock_jp.market_data.jquants import ingest
from stock_jp.workspace import project_root


class LocalGit:
    """Only read pinned local Git objects; no fetch, checkpoint writes or provider I/O."""
    def __init__(self, root):
        self.root, self.modes = root, {}

    def command(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL)

    def pin(self, branch, commit):
        if not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise ValueError('offline preparation requires an exact commit')
        ref = 'refs/remotes/origin/' + branch
        if self.command('rev-parse', '--verify', ref).decode().strip() != commit:
            raise ValueError('offline pin differs from the locally verified origin ref')
        if self.command('cat-file', '-t', commit).strip() != b'commit':
            raise ValueError('offline pin is not a commit')

    def tree(self, commit):
        result, modes = {}, {}
        for entry in self.command('ls-tree', '-rz', commit).split(b'\0'):
            if not entry:
                continue
            meta, name = entry.decode().split('\t', 1)
            mode, kind, sha = meta.split()
            if kind == 'blob':
                result[name], modes[name] = sha, mode
        self.modes[commit] = modes
        return result

    def read(self, blob):
        if not re.fullmatch(r'[0-9a-f]{40}', blob):
            raise ValueError('invalid offline blob identity')
        raw = self.command('cat-file', 'blob', blob)
        if n.git_blob(raw) != blob:
            raise ValueError('offline Git blob identity mismatch')
        return raw


def prepare(root, *, day, cutoff, inbox_commit, checkpoint_commit):
    policy = json.loads((root / 'config/policy.json').read_text())
    if (policy.get('operation_mode'), policy.get('broker_submission')) != ('PAPER', 'HUMAN_ONLY'):
        raise ValueError('offline preparation requires PAPER/HUMAN_ONLY')
    bound = reuse.identity(root, day, cutoff)
    git = LocalGit(root)
    origin = git.command('remote', 'get-url', 'origin').decode().strip()
    if origin not in {'https://github.com/kkyosuke/stock.jp.private', 'https://github.com/kkyosuke/stock.jp.private.git',
                      'git@github.com:kkyosuke/stock.jp.private.git'}:
        raise ValueError('offline origin is not the private repository')
    git.pin(n.PAPER_CHECKPOINT, inbox_commit)
    git.pin(n.CHECKPOINT, checkpoint_commit)
    tree = git.tree(checkpoint_commit)
    journal = json.loads(git.read(tree[n.JOURNAL]))
    cutoffs = {b['paper_cutoff_at_jst'] for b in journal['scheduled_targets'].values()
               if b['date'] == day and 'paper_cutoff_at_jst' in b}
    state = journal['runs'][day]
    if cutoffs != {cutoff} or state['config_sha256'] != bound['source_config_sha256']:
        raise ValueError('offline logical target differs from pinned checkpoint')
    if type(state['budget']) is not int or not 1 <= state['budget'] <= 12:
        raise ValueError('offline target budget differs from shared allowance')
    if state['status'] not in {'HOLIDAY', 'COLLECTED'}:
        raise ValueError('offline target acquisition is incomplete')
    n.require(git.modes[checkpoint_commit][n.JOURNAL] == '100644', 'unsafe offline checkpoint mode')
    # Preserve and validate the budget evidence, without ever initializing/reserving it.
    from stock_jp.automation.provider_budget import validate_charges
    validate_charges(journal)
    files, bundle = paper_sources.stage(root, git, inbox_commit, git.tree(inbox_commit), day=day, cutoff=cutoff)
    if not bundle['receipts']:
        raise ValueError('no verified primary evidence for the selected target')
    git.pin(n.PAPER_CHECKPOINT, inbox_commit)
    git.pin(n.CHECKPOINT, checkpoint_commit)
    paper_sources.install(root, files)
    return review_packet(root, day=day, cutoff=cutoff, checkpoint_commit=checkpoint_commit)


def review_packet(root, *, day, cutoff, checkpoint_commit=None):
    sources = reuse.load(root, day=day, cutoff=cutoff)
    if not sources:
        raise ValueError('no verified primary evidence for offline review')
    result = {'status': 'OFFLINE_PRIMARY_REVIEW_REQUIRED', 'logical_day': day, 'cutoff_at_jst': cutoff,
              'operation_mode': 'PAPER', 'broker_submission': 'HUMAN_ONLY', 'completed_run': False,
              'canonical_run_created': False, 'review_command': None, 'checkpoint_commit': checkpoint_commit,
              'provenance': json.loads(reuse.confined(root, reuse.BUNDLE).read_text()),
              'sources': list(sources.values()), 'review_completed': False,
              'blocking_gaps': ['Source content, PIT availability, source rights and disclosure coverage remain unverified.',
                                'Canonical run preparation and finalization are separate; this packet cannot finalize a run.'],
              'investment_readiness': 'NOT_EVALUATED'}
    path = reuse.confined(root, f'.work/paper-research/offline/{day}/review.json')
    raw = ingest.encoded(result)
    if path.exists() and path.read_bytes() != raw:
        raise ValueError('offline review packet already exists; preserve existing review work')
    ingest.atomic_bytes(path, raw)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', required=True)
    parser.add_argument('--cutoff', required=True)
    parser.add_argument('--inbox-commit', required=True)
    parser.add_argument('--checkpoint-commit', required=True)
    args = parser.parse_args()
    result = prepare(project_root(), day=args.day, cutoff=args.cutoff,
                     inbox_commit=args.inbox_commit, checkpoint_commit=args.checkpoint_commit)
    print(json.dumps({k: result[k] for k in ('status', 'logical_day', 'completed_run', 'canonical_run_created')}))
    return 1  # Preparation is deliberately incomplete review, never an operational success.


if __name__ == '__main__':
    raise SystemExit(main())
