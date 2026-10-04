"""Private Git archive codec. No network, authentication, automatic commit or push."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

CHUNK = 1024 * 1024
MAX_SHARD = 22 * 1024 * 1024  # base64 + wrapped JSON stays below the 32 MiB HTTP cap
MAX_STORE = 750 * 1024 * 1024
MAX_INDEX = 64 * 1024 * 1024
MAX_RESTORE = 8 * 1024 * 1024 * 1024
DESTINATION = 'kkyosuke/stock.jp.private'

try:
    from scripts import nightly_cache_archive as nightly_cache
    from scripts.jquants_archive_validation import validate_archive
except ModuleNotFoundError:
    import nightly_cache_archive as nightly_cache
    from jquants_archive_validation import validate_archive


class ArchiveError(ValueError):
    """Only fixed, sanitized messages may leave the codec."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def safe_tree(root):
    if root.is_symlink() or any(p.is_symlink() for p in root.rglob('*')):
        raise ArchiveError('symlink in archive tree')
    if any(not p.is_file() and not p.is_dir() for p in root.rglob('*')):
        raise ArchiveError('special file in archive tree')


def safe_name(name):
    p = PurePosixPath(name)
    if (p.is_absolute() or '..' in p.parts or '\\' in name or p.as_posix() != name
            or not p.parts or (p.parts[0] not in {'jquants', 'daily-prices'}
                              and not nightly_cache.valid_name(name))
            or any(part.startswith('.') for part in p.parts)):
        raise ArchiveError('unsafe archive member')
    return p


def scan(data):
    # Reject authentication material without reading any runtime secret.
    patterns = (rb'-----BEGIN [A-Z ]*PRIVATE KEY-----',
                rb'(?i)(?:x-api-key|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token)\s*["\x27]?\s*[:=]',
                rb'(?i)https?://[^\s"<>]*[?&](?:[^\s"<>]*)(?:signature|token|credential|x-amz-|x-goog-)',
                rb'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})')
    if any(re.search(p, data) for p in patterns):
        raise ArchiveError('credential or signed URL pattern rejected')


@contextmanager
def writer_lock(source):
    folder = source / 'jquants'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / '.ingestion.lock').open('a+b') as stream:
        os.chmod(stream.name, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ArchiveError('another archive writer holds the lock') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def object_path(store, sha, kind='objects'):
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
        raise ArchiveError('invalid checksum identifier')
    return store / kind / sha[:2] / (sha + '.gz')


def put(store, data, kind='objects'):
    compressed = gzip.compress(data, mtime=0)
    if len(compressed) > MAX_SHARD:
        raise ArchiveError('compressed shard exceeds 22 MiB')
    sha = digest(compressed)
    path = object_path(store, sha, kind)
    if path.exists():
        if path.read_bytes() != compressed:
            raise ArchiveError('existing object checksum mismatch')
    else:
        size = sum(p.stat().st_size for p in store.rglob('*') if p.is_file())
        if size + len(compressed) > MAX_STORE:
            raise ArchiveError('private Git store exceeds conservative size budget')
        atomic(path, compressed)
    return sha


def unpack(store, sha, limit, kind='objects'):
    path = object_path(store, sha, kind)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SHARD:
        raise ArchiveError('missing or oversized object')
    blob = path.read_bytes()
    if digest(blob) != sha:
        raise ArchiveError('object checksum mismatch')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ArchiveError('expanded object exceeds limit')
        return data
    except (OSError, EOFError):
        raise ArchiveError('invalid compressed object') from None


def store_member(name):
    return name == 'current.json' or bool(re.fullmatch(
        r'(?:objects/[0-9a-f]{2}/[0-9a-f]{64}\.(?:gz|json|csv|bin)|generations/[0-9a-f]{2}/[0-9a-f]{64}\.(?:gz|json))', name))


def put_plain(store, data, suffix, kind='objects'):
    if suffix not in {'.json', '.csv', '.bin'} or kind not in {'objects', 'generations'}:
        raise ArchiveError('invalid plain object type')
    if len(data) > MAX_SHARD:
        raise ArchiveError('plain file exceeds 22 MiB; no implicit lossy splitting')
    sha = digest(data)
    name = f'{kind}/{sha[:2]}/{sha}{suffix}'
    path = Path(store)/name
    if path.exists():
        if path.read_bytes() != data:
            raise ArchiveError('existing plain object checksum mismatch')
    else:
        size = sum(p.stat().st_size for p in Path(store).rglob('*') if p.is_file())
        if size + len(data) > MAX_STORE:
            raise ArchiveError('private Git store exceeds conservative size budget')
        atomic(path, data)
    return name


def plain_bytes(store, name, sha, limit):
    if (not store_member(name) or name.endswith('.gz') or Path(name).stem != sha
            or Path(name).parent.name != sha[:2]):
        raise ArchiveError('invalid plain object identity')
    path = Path(store)/name
    if not path.is_file() or path.is_symlink() or path.stat().st_size > min(limit, MAX_SHARD):
        raise ArchiveError('missing or oversized plain file')
    data = path.read_bytes()
    if digest(data) != sha:
        raise ArchiveError('plain file checksum mismatch')
    return data


def manifest_bytes(store, pointer):
    if pointer.get('version') == 1:
        return unpack(store, pointer['generation'], MAX_INDEX, 'generations')
    if pointer.get('version') == 2:
        sha = pointer['generation']
        return plain_bytes(store, f'generations/{sha[:2]}/{sha}.json', sha, MAX_INDEX)
    raise ArchiveError('unsupported pointer version')


def member_bytes(store, pointer, entry):
    if pointer['version'] == 1:
        if set(entry) != {'sha256', 'bytes', 'chunks'} or not isinstance(entry['chunks'], list):
            raise ArchiveError('invalid legacy file metadata')
        if len(entry['chunks']) != (entry['bytes'] + CHUNK - 1) // CHUNK:
            raise ArchiveError('invalid chunk count')
        return b''.join(unpack(store, sha, CHUNK) for sha in entry['chunks'])
    if set(entry) != {'sha256', 'bytes', 'object'} or not entry['object'].startswith('objects/'):
        raise ArchiveError('invalid plain file metadata')
    return plain_bytes(store, entry['object'], entry['sha256'], MAX_SHARD)


def reachable_members(store, pointer):
    manifest = json.loads(manifest_bytes(store, pointer))
    if pointer['version'] == 1:
        names = {object_path(Path(store), pointer['generation'], 'generations').relative_to(store).as_posix()}
        for entry in manifest['files'].values():
            names.update(object_path(Path(store), sha).relative_to(store).as_posix() for sha in entry['chunks'])
        return names
    return {f"generations/{pointer['generation'][:2]}/{pointer['generation']}.json"} | {
        entry['object'] for entry in manifest['files'].values()}


def pack(source, store):
    source, store = Path(source), Path(store)
    if any(p.is_symlink() for p in (*source.parents, *store.parents)):
        raise ArchiveError('symlink in archive ancestor')
    if not source.is_dir() or store.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(store.resolve()):
        raise ArchiveError('source and store must be separate directories')
    safe_tree(source)
    safe_tree(store)
    with ExitStack() as locks:
        locks.enter_context(writer_lock(source))
        cache_root = source / 'nightly-source-cache'
        if cache_root.exists():
            for scope in sorted(cache_root.iterdir()):
                if not scope.is_dir() or not re.fullmatch('[0-9a-f]{64}', scope.name):
                    raise ArchiveError('invalid nightly cache scope')
                stream = locks.enter_context((scope / '.ingestion.lock').open('a+b'))
                os.chmod(stream.name, 0o600)
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ArchiveError('another nightly cache writer holds the lock') from None
        safe_tree(source)
        entries = {}
        for path in sorted(source.rglob('*')):
            if path.is_dir():
                continue
            name = path.relative_to(source).as_posix()
            if name == 'jquants/.ingestion.lock' or re.fullmatch(r'nightly-source-cache/[0-9a-f]{64}/\.ingestion.lock', name):
                continue
            safe_name(name)
            if path.name.startswith('.pending-'):
                raise ArchiveError('unfinished atomic write')
            if path.stat().st_size > MAX_SHARD:
                raise ArchiveError('plain file exceeds 22 MiB')
            data = path.read_bytes()
            scan(data)
            suffix = path.suffix if path.suffix in {'.json', '.csv'} else '.bin'
            member = put_plain(store, data, suffix)
            entries[name] = {'sha256': digest(data), 'bytes': len(data), 'object': member}
        manifest = encoded({'version': 2, 'files': entries})
        if len(manifest) > MAX_INDEX:
            raise ArchiveError('generation index exceeds limit')
        generation = Path(put_plain(store, manifest, '.json', 'generations')).stem
        pointer = {'version': 2, 'generation': generation, 'file_count': len(entries),
                   'raw_bytes': sum(e['bytes'] for e in entries.values())}
        # Validate the complete candidate before making it visible.
        verify(store, pointer)
        atomic(store / 'current.json', encoded(pointer))
        return {**pointer, 'store_bytes': sum(p.stat().st_size for p in store.rglob('*') if p.is_file())}


def verify(store, pointer=None, destination=None):
    store = Path(store)
    safe_tree(store)
    pointer = pointer or json.loads((store / 'current.json').read_bytes())
    if sum(p.stat().st_size for p in store.rglob('*') if p.is_file()) > MAX_STORE:
        raise ArchiveError('private Git store exceeds conservative size budget')
    manifest = json.loads(manifest_bytes(store, pointer))
    if manifest.get('version') != pointer['version'] or not isinstance(manifest.get('files'), dict):
        raise ArchiveError('invalid generation index')
    total = 0
    cache_members = {}
    for name, entry in manifest['files'].items():
        relative = safe_name(name)
        if type(entry.get('bytes')) is not int or not 0 <= entry['bytes'] <= MAX_RESTORE:
            raise ArchiveError('invalid file size')
        total += entry['bytes']
        if total > MAX_RESTORE:
            raise ArchiveError('restore exceeds limit')
        if pointer['version'] == 2:
            suffix = Path(name).suffix if Path(name).suffix in {'.json', '.csv'} else '.bin'
            if entry.get('object') != f"objects/{entry['sha256'][:2]}/{entry['sha256']}{suffix}":
                raise ArchiveError('plain member type or identity differs')
        data = member_bytes(store, pointer, entry)
        if len(data) != entry['bytes'] or digest(data) != entry['sha256']:
            raise ArchiveError('file checksum mismatch')
        scan(data)
        if name.startswith(nightly_cache.PREFIX):
            cache_members[name] = data
        if destination is not None:
            atomic(Path(destination) / relative, data)
    try:
        nightly_cache.validate(cache_members)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ArchiveError('invalid nightly cache archive') from None
    def read_member(name):
        entry = manifest['files'][name]
        return member_bytes(store, pointer, entry)
    try:
        validate_archive(manifest['files'], read_member)
    except (ValueError, TypeError, KeyError, OSError):
        raise ArchiveError('invalid retained J-Quants revision or page') from None
    if len(manifest['files']) != pointer['file_count'] or total != pointer['raw_bytes']:
        raise ArchiveError('generation totals mismatch')
    return {'verified': True, 'generation': pointer['generation'], 'file_count': len(manifest['files']), 'raw_bytes': total}


def restore(store, target):
    target = Path(target)
    if any(p.is_symlink() for p in target.parents):
        raise ArchiveError('symlink in restore ancestor')
    if target.exists() or target.is_symlink():
        raise ArchiveError('restore target must not exist; never overlay checkpoints')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    scratch = Path(tempfile.mkdtemp(prefix='.restore-', dir=target.parent))
    try:
        result = verify(store, destination=scratch)
        os.rename(scratch, target)
        return result
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)


def publication_preflight(metadata):
    """Metadata is obtained with an authorized read tool, never guessed from a URL."""
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(metadata['checked_at'])).total_seconds()
        valid = (metadata['repository_full_name'] == DESTINATION
                 and metadata['visibility'] == 'private'
                 and metadata['owner']['login'] == 'kkyosuke'
                 and metadata['permissions']['push'] is True
                 and 0 <= age <= 300)
    except (KeyError, ValueError, TypeError):
        valid = False
    if not valid:
        raise ArchiveError('fresh private repository metadata with push access required')
    return True


def stage(root, metadata):
    """Explicit local publication gate; still never commits or uploads."""
    publication_preflight(metadata)
    root = Path(root).resolve()
    if (root / 'operations/private').is_symlink():
        raise ArchiveError('--root must be the single repository root')
    config = json.loads((root / 'operations/private/source-config.json').read_text())['market_data_git']
    if config.get('store_root') != 'stock.jp/data/jquants-archive':
        raise ArchiveError('unexpected single-repository store path')
    if config['repository'] != DESTINATION or config.get('owner_only_access_confirmed') is not True:
        raise ArchiveError('private destination owner-only access is not confirmed')
    # The operator must recheck remote visibility and access before each push.
    private = (root / 'operations/private').resolve()
    source = private / 'market-data'
    store = root / 'stock.jp/data/jquants-archive'
    if not source.resolve().is_relative_to(private) or not store.resolve().is_relative_to(root):
        raise ArchiveError('private paths escape project boundary')
    return pack(source, store)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('pack')
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--store', type=Path, required=True)
    p = sub.add_parser('verify')
    p.add_argument('--store', type=Path, required=True)
    p = sub.add_parser('restore')
    p.add_argument('--store', type=Path, required=True)
    p.add_argument('--target', type=Path, required=True)
    p = sub.add_parser('stage')
    p.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    p.add_argument('--metadata', type=Path, required=True, help='fresh sanitized repository metadata from authorized read tool')
    args = parser.parse_args()
    try:
        if args.command == 'pack':
            result = pack(args.source, args.store)
        elif args.command == 'verify':
            result = verify(args.store)
        elif args.command == 'restore':
            result = restore(args.store, args.target)
        else:
            result = stage(args.root, json.loads(args.metadata.read_text()))
        print(json.dumps(result))
        return 0
    except (ArchiveError, OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'status': 'blocked', 'reason': 'archive validation, access gate or storage failure; no upload attempted'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
