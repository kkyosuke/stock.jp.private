"""Strict, offline validation of persisted nightly J-Quants cache members."""
from datetime import date, datetime
import hashlib
import json
import math
import re

PREFIX = 'nightly-source-cache/'
HEX = r'[0-9a-f]{64}'
MEMBER = re.compile(rf'{PREFIX}({HEX})/(?:pages/({HEX})\.json|objects/([0-9a-f]{{2}})/({HEX})\.json)')


def valid_name(name):
    match = MEMBER.fullmatch(name)
    return bool(match and (match[4] is None or match[3] == match[4][:2]))


def require(ok):
    if not ok:
        raise ValueError('invalid nightly cache archive')


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def strict_json(data):
    def finite_float(value):
        number = float(value)
        require(math.isfinite(number))
        return number

    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs, parse_float=finite_float,
                      parse_constant=lambda _: require(False))


def aware(value):
    require(isinstance(value, str))
    stamp = datetime.fromisoformat(value)
    require(stamp.tzinfo is not None and stamp.utcoffset() is not None)
    return stamp


def validate_query(endpoint, params):
    require(isinstance(params, dict))
    require(all(isinstance(v, str) and 0 < len(v) <= 4096 for v in params.values()))
    keys = set(params) - {'pagination_key'}
    shapes = {
        '/equities/master': [{'date'}],
        '/equities/bars/daily': [{'date'}, {'code', 'from', 'to'}],
        '/fins/summary': [{'date'}],
        '/fins/earnings-date': [{'date'}],
        '/equities/earnings-calendar': [set()],
        '/markets/calendar': [{'from', 'to'}],
    }
    require(endpoint in shapes and keys in shapes[endpoint])
    for key in keys & {'date', 'from', 'to'}:
        require(date.fromisoformat(params[key]).isoformat() == params[key])
    if 'from' in keys:
        require(params['from'] <= params['to'])
    if 'code' in keys:
        require(re.fullmatch(r'[0-9A-Z]{4,5}', params['code']) is not None)


def validate(members):
    """Check complete receipt/object linkage without claiming source authenticity.

    Hashes detect mismatches, not a maliciously re-signed local cache. Receipts
    retain the original cutoff/query/retrieval provenance; no values are rewritten.
    """
    referenced = set()
    objects = set()
    for name, data in members.items():
        require(valid_name(name))
        match = MEMBER.fullmatch(name)
        if match[4] is not None:
            require(digest(data) == match[4])
            payload = strict_json(data)
            require(isinstance(payload, dict) and set(payload) <= {'data', 'pagination_key'})
            require(isinstance(payload.get('data'), list)
                    and all(isinstance(row, dict) for row in payload['data']))
            require(payload.get('pagination_key') is None or isinstance(payload['pagination_key'], str))
            objects.add(name)
            continue
        receipt = strict_json(data)
        require(isinstance(receipt, dict) and set(receipt) == {'identity', 'object', 'retrieved_at_jst'})
        identity = receipt['identity']
        require(isinstance(identity, dict) and set(identity) == {'version', 'origin', 'plan', 'cutoff', 'endpoint', 'params'})
        require(type(identity['version']) is int and identity['version'] == 1
                and identity['origin'] == 'https://api.jquants.com/v2'
                and isinstance(identity['plan'], str) and identity['plan'] in {'light', 'standard'})
        require(aware(receipt['retrieved_at_jst']) >= aware(identity['cutoff']))
        validate_query(identity['endpoint'], identity['params'])
        scope = {k: identity[k] for k in ('version', 'origin', 'plan', 'cutoff')}
        require(digest(encoded(scope)) == match[1] and digest(encoded(identity)) == match[2])
        ref = receipt['object']
        require(isinstance(ref, dict) and set(ref) == {'path', 'sha256', 'bytes'})
        sha = ref['sha256']
        require(isinstance(sha, str) and re.fullmatch(HEX, sha) is not None)
        require(ref['path'] == f'objects/{sha[:2]}/{sha}.json')
        target = f'{PREFIX}{match[1]}/{ref["path"]}'
        require(target in members and type(ref['bytes']) is int and ref['bytes'] == len(members[target]))
        referenced.add(target)
    require(objects == referenced)
