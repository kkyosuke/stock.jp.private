"""Prepare unreviewed primary evidence offline, without creating a canonical run."""
import argparse
import json
import os
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
        env = {**os.environ, 'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_NO_LAZY_FETCH': '1',
               'GIT_ALLOW_PROTOCOL': '', 'GIT_TERMINAL_PROMPT': '0'}
        try:
            return subprocess.check_output(
                ['git', '--no-replace-objects', '--no-lazy-fetch', '--no-optional-locks',
                 '-C', str(self.root), *args], env=env, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError):
            raise ValueError('offline Git read failed: required objects/refs must exist locally; '
                             'Git must support no-lazy-fetch; fetching is disabled') from None

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
    reuse.identity(root, day, cutoff)
    git = LocalGit(root)
    origin = git.command('remote', 'get-url', 'origin').decode().strip()
    if origin not in {'https://github.com/kkyosuke/stock.jp.private', 'https://github.com/kkyosuke/stock.jp.private.git',
                      'git@github.com:kkyosuke/stock.jp.private.git'}:
        raise ValueError('offline origin is not the private repository')
    git.pin(n.PAPER_CHECKPOINT, inbox_commit)
    git.pin(n.CHECKPOINT, checkpoint_commit)
    tree = git.tree(checkpoint_commit)
    journal = json.loads(git.read(tree[n.JOURNAL]))
    n.require(git.modes[checkpoint_commit][n.JOURNAL] == '100644', 'unsafe offline checkpoint mode')
    from stock_jp.operation.paper_offline_budget import validate_budget
    validate_budget(root, journal, day=day, cutoff=cutoff)
    files, bundle = paper_sources.stage(root, git, inbox_commit, git.tree(inbox_commit), day=day, cutoff=cutoff)
    if not bundle['receipts']:
        raise ValueError('no verified primary evidence for the selected target')
    git.pin(n.PAPER_CHECKPOINT, inbox_commit)
    git.pin(n.CHECKPOINT, checkpoint_commit)
    result = review_packet(root, day=day, cutoff=cutoff, checkpoint_commit=checkpoint_commit,
                           files=files, write=False)
    files[packet_name(day)] = ingest.encoded(result)
    # All output conflicts (including dangling symlinks) are checked before any install.
    paper_sources.install(root, files)
    return result


def packet_name(day):
    return f'.work/paper-research/offline/{day}/review.json'


def review_packet(root, *, day, cutoff, checkpoint_commit=None, files=None, write=True):
    sources = reuse.load(root, day=day, cutoff=cutoff, files=files)
    if not sources:
        raise ValueError('no verified primary evidence for offline review')
    result = {'status': 'OFFLINE_PRIMARY_REVIEW_REQUIRED', 'logical_day': day, 'cutoff_at_jst': cutoff,
              'operation_mode': 'PAPER', 'broker_submission': 'HUMAN_ONLY', 'completed_run': False,
              'canonical_run_created': False, 'review_command': None, 'checkpoint_commit': checkpoint_commit,
              'provenance': json.loads(files[reuse.BUNDLE] if files is not None else reuse.confined(root, reuse.BUNDLE).read_bytes()),
              'sources': list(sources.values()), 'review_completed': False,
              'blocking_gaps': ['Source content, PIT availability, source rights and disclosure coverage remain unverified.',
                                'Canonical run preparation and finalization are separate; this packet cannot finalize a run.'],
              'investment_readiness': 'NOT_EVALUATED'}
    output = {packet_name(day): ingest.encoded(result)}
    paper_sources.preflight(root, output)
    if write:
        paper_sources.install(root, output)
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
