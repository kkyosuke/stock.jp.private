"""Read verified append-only inbox snapshots; stage only scoped primary evidence."""
import json

from stock_jp.automation import nightly as n
from stock_jp.market_data import primary_reuse as reuse
from stock_jp.market_data.jquants import ingest


def stage(root, git, revision, tree, *, day, cutoff):
    bound = reuse.identity(root, day, cutoff)
    prefix = n.PAPER_RAW_PREFIX + 'snapshots/'
    snapshots = [(name, sha) for name, sha in tree.items() if name.startswith(prefix)]
    if len(snapshots) > 4096:
        raise ValueError('primary snapshot inventory exceeds bounded restore')
    receipts, sources, files = {}, [], {}
    # The latest journal alone loses the first attempt's successful sources.
    for name, sha in snapshots:
        raw = git.read(sha)
        if (git.modes[revision].get(name) != '100644' or len(raw) > 22 * 1024**2
                or name != prefix + reuse.digest(raw) + '.json'):
            raise ValueError('primary snapshot identity mismatch')
        snapshot = json.loads(raw)
        if snapshot['day'] != day:
            continue
        if (snapshot.get('version') != 1 or snapshot.get('operation_mode') != 'PAPER'
                or snapshot.get('broker_submission') != 'HUMAN_ONLY'
                or snapshot.get('review_completed') is not False):
            raise ValueError('invalid unreviewed primary snapshot')
        if snapshot['config_sha256'] != bound['source_config_sha256']:
            raise ValueError('primary snapshot configuration differs')
        members = snapshot['files']

        def read(member):
            item = members[member]
            path = n.PAPER_RAW_PREFIX + item['sha256'] + '/' + member
            if tree.get(path) != item['blob'] or git.modes[revision].get(path) != '100644':
                raise ValueError('primary inbox member missing')
            data = git.read(item['blob'])
            if len(data) != item['bytes'] or reuse.digest(data) != item['sha256']:
                raise ValueError('primary inbox checksum mismatch')
            return data

        receipt_prefix = 'research/historical-replay/v04-forward/collection/' + reuse.aware(cutoff).date().isoformat() + '/'
        for member in members:
            if not member.startswith(receipt_prefix):
                continue
            data = read(member)
            if member != receipt_prefix + reuse.digest(data) + '.json':
                raise ValueError('noncanonical primary receipt path')
            value = json.loads(data)
            if value['cutoff_at_jst'] != cutoff:
                continue
            primary = value['primary_sources']
            if any(primary[k] != bound[k] for k in ('cutoff_at_jst', 'config_sha256', 'source_config_sha256')):
                raise ValueError('primary receipt configuration/cutoff differs')
            if any(reuse.aware(s['retrieved_at_jst']) > reuse.aware(value['captured_at_jst']) for s in primary['sources']):
                raise ValueError('primary retrieval exceeds collection time')
            if reuse.aware(value['captured_at_jst']) > reuse.aware(snapshot['captured_at_jst']):
                raise ValueError('primary receipt exceeds snapshot time')
            _, objects = reuse.validate_sources(root, primary['sources'], cutoff=cutoff, read=read)
            files.update(objects)
            files[member] = data
            sources.extend(primary['sources'])
            receipts[member] = {'path': member, 'sha256': reuse.digest(data)}
            if sum(map(len, files.values())) > 256 * 1024**2:
                raise ValueError('primary restore exceeds bounded snapshot size')
    reuse.validate_sources(root, sources, cutoff=cutoff, read=files.__getitem__)
    bundle = {'identity': bound, 'inbox_commit': revision, 'receipts': list(receipts.values()),
              'review_completed': False, 'canonical_state_restored': False}
    files[reuse.BUNDLE] = ingest.encoded(bundle)
    return files, bundle


def install(root, files):
    # Preflight all conflicts before writing; never overwrite adopted evidence.
    for name, raw in files.items():
        lexical = root / name
        if any(p.is_symlink() for p in [lexical, *lexical.parents] if p.is_relative_to(root)):
            raise ValueError('primary evidence path contains a symlink')
        path = reuse.confined(root, name)
        if path.exists() and path.read_bytes() != raw:
            raise ValueError('local and restored primary evidence disagree')
    for name, raw in files.items():
        ingest.atomic_bytes(reuse.confined(root, name), raw)
