"""Validate a scoped, unreviewed source bundle before any cache hit or offline review."""
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from stock_jp.market_data.primary_sources import CONFIG, JST, MAX_BYTES, available_at, checked_url, confined, feed_items

BUNDLE = '.work/paper-research/primary-reuse.json'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def aware(value):
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ValueError('primary provenance timestamp needs an offset')
    return at.astimezone(JST)


def identity(root, day, cutoff):
    at = aware(cutoff)
    if date.fromisoformat(day).isoformat() != day or at > datetime.now(JST):
        raise ValueError('invalid primary logical day or future cutoff')
    start = datetime.fromisoformat(day).replace(hour=21, tzinfo=JST)
    if not start <= at < start + timedelta(days=1):
        raise ValueError('primary cutoff differs from logical publication day')
    return {'logical_day': day, 'cutoff_at_jst': cutoff,
            'config_sha256': digest((root / CONFIG).read_bytes()),
            'source_config_sha256': digest((root / 'config/sources.json').read_bytes())}


def validate_sources(root, sources, *, cutoff, read):
    """Reconstruct provenance from the configured issuer feed, never a bare URL/hash."""
    config = json.loads((root / CONFIG).read_text())
    regime = json.loads((root / 'config/sources.json').read_text())['market_regime']
    selected = aware(cutoff)
    expected, objects, suffixes = {}, {}, {}
    for source in sources:
        name, sha = source['path'], source['sha256']
        suffix = Path(name).suffix
        if suffix not in {'.js', '.pdf', '.csv', '.html'} or name != f'data/market/primary-sources/{sha}{suffix}':
            raise ValueError('noncanonical primary object path')
        raw = read(name)
        if (not raw or len(raw) > MAX_BYTES or len(raw) != source['bytes'] or digest(raw) != sha
                or suffix == '.pdf' and not raw.startswith(b'%PDF-')):
            raise ValueError('primary object checksum or format mismatch')
        retrieved = aware(source['retrieved_at_jst'])
        if retrieved > datetime.now(JST):
            raise ValueError('primary retrieval is in the future')
        checked_url(source['url'], config['allowed_hosts'])
        objects[name] = raw
    for issuer in config['issuers']:
        url, code = issuer['feed_url'], issuer['code']
        feeds = [s for s in sources if s['url'] == url]
        for feed in feeds:
            items = feed_items(objects[feed['path']], code)
            suffixes[url] = '.js'
            expected[url] = {'code': code, 'title': 'Official issuer IR announcement feed', 'item_count': len(items)}
            for item in items:
                published, precision = available_at(item['date'])
                if not aware(issuer['from_at_jst']) <= published <= selected or item.get('type') != 'pdf':
                    continue
                link = checked_url(item['link'], config['allowed_hosts'])
                if not urlsplit(link).path.startswith(f'/doc/{code}/'):
                    raise ValueError('primary document issuer differs')
                suffixes[link] = '.pdf'
                expected[link] = {'code': code, 'source_id': f"company-{code}-{item['guid']}",
                                  'title': item['title'], 'published_at_jst': published.isoformat(timespec='seconds'),
                                  'publication_precision': precision, 'review_status': 'COLLECTED_NOT_REVIEWED',
                                  'original_historical_vintage_verified': False}
    for series in config['public_series']:
        suffixes[regime[series['config_key']]] = series['suffix']
        expected[regime[series['config_key']]] = {'series': series['id'], 'title': series['title'],
                                                'review_status': 'COLLECTED_NOT_REVIEWED',
                                                'original_historical_vintage_verified': False}
    result = {}
    for source in sources:
        url = source['url']
        provenance = {k: v for k, v in source.items() if k not in {'url', 'path', 'sha256', 'bytes', 'retrieved_at_jst'}}
        if url not in expected or provenance != expected[url] or Path(source['path']).suffix != suffixes[url]:
            raise ValueError('primary source provenance differs from configured source/feed')
        if url in result:
            if any(result[url][k] != source[k] for k in source if k != 'retrieved_at_jst'):
                raise ValueError('conflicting primary source versions at fixed cutoff')
            if aware(result[url]['retrieved_at_jst']) <= aware(source['retrieved_at_jst']):
                continue
        result[url] = source
    return result, objects


def load(root, *, day, cutoff):
    path = confined(root, BUNDLE)
    if not path.exists():
        return {}
    bundle = json.loads(path.read_text())
    if bundle['identity'] != identity(root, day, cutoff):
        raise ValueError('primary reuse logical day, cutoff or configuration mismatch')
    sources = []
    for receipt in bundle['receipts']:
        raw = confined(root, receipt['path']).read_bytes()
        if digest(raw) != receipt['sha256'] or Path(receipt['path']).stem != receipt['sha256']:
            raise ValueError('primary collection receipt checksum mismatch')
        value = json.loads(raw)
        primary = value['primary_sources']
        if value['cutoff_at_jst'] != cutoff or any(primary[k] != bundle['identity'][k] for k in
                ('cutoff_at_jst', 'config_sha256', 'source_config_sha256')):
            raise ValueError('primary receipt scope mismatch')
        sources.extend(primary['sources'])
    result, _ = validate_sources(root, sources, cutoff=cutoff, read=lambda name: confined(root, name).read_bytes())
    return result
