"""Private Git archive codec. No network, authentication, automatic commit or push."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
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
MAX_SHARD = 25 * 1024 * 1024
MAX_STORE = 750 * 1024 * 1024
MAX_INDEX = 64 * 1024 * 1024
MAX_RESTORE = 8 * 1024 * 1024 * 1024
DESTINATION = 'kkyosuke/stock.jp.private'


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
            or not p.parts or p.parts[0] not in {'jquants', 'daily-prices'}
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
        raise ArchiveError('compressed shard exceeds 25 MiB')
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


def pack(source, store):
    source, store = Path(source), Path(store)
    if any(p.is_symlink() for p in (*source.parents, *store.parents)):
        raise ArchiveError('symlink in archive ancestor')
    if not source.is_dir() or store.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(store.resolve()):
        raise ArchiveError('source and store must be separate directories')
    safe_tree(source)
    safe_tree(store)
    with writer_lock(source):
        entries = {}
        for path in sorted(source.rglob('*')):
            if path.is_dir() or path.name == '.ingestion.lock':
                continue
            name = path.relative_to(source).as_posix()
            safe_name(name)
            if path.name.startswith('.pending-'):
                raise ArchiveError('unfinished atomic write')
            data = path.read_bytes()
            scan(data)
            chunks = [put(store, data[i:i + CHUNK]) for i in range(0, len(data), CHUNK)]
            entries[name] = {'sha256': digest(data), 'bytes': len(data), 'chunks': chunks}
        manifest = encoded({'version': 1, 'files': entries})
        if len(manifest) > MAX_INDEX:
            raise ArchiveError('generation index exceeds limit')
        generation = put(store, manifest, 'generations')
        pointer = {'version': 1, 'generation': generation, 'file_count': len(entries),
                   'raw_bytes': sum(e['bytes'] for e in entries.values())}
        # Validate the complete candidate before making it visible.
        verify(store, pointer)
        atomic(store / 'current.json', encoded(pointer))
        return {**pointer, 'store_bytes': sum(p.stat().st_size for p in store.rglob('*') if p.is_file())}


def verify(store, pointer=None, destination=None):
    store = Path(store)
    safe_tree(store)
    pointer = pointer or json.loads((store / 'current.json').read_bytes())
    if pointer.get('version') != 1:
        raise ArchiveError('unsupported pointer version')
    manifest = json.loads(unpack(store, pointer['generation'], MAX_INDEX, 'generations'))
    if manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
        raise ArchiveError('invalid generation index')
    total = 0
    for name, entry in manifest['files'].items():
        relative = safe_name(name)
        if type(entry.get('bytes')) is not int or not 0 <= entry['bytes'] <= MAX_RESTORE:
            raise ArchiveError('invalid file size')
        total += entry['bytes']
        if total > MAX_RESTORE or not isinstance(entry.get('chunks'), list):
            raise ArchiveError('restore exceeds limit')
        if len(entry['chunks']) != (entry['bytes'] + CHUNK - 1) // CHUNK:
            raise ArchiveError('invalid chunk count')
        data = b''.join(unpack(store, sha, CHUNK) for sha in entry['chunks'])
        if len(data) != entry['bytes'] or digest(data) != entry['sha256']:
            raise ArchiveError('file checksum mismatch')
        scan(data)
        if destination is not None:
            atomic(Path(destination) / relative, data)
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
