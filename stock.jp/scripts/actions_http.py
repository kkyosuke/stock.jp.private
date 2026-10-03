"""Fixed-origin transports. No retries, redirects, body/error logging or env lookup."""
import json
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener

try:
    from scripts.actions_daily_remote import RemoteStop, REPO
    from scripts import jquants_ingest as ingest
    from scripts.jquants_daily import NAMES
    from scripts.jquants_catalog import BY_NAME, BASE_URL
except ModuleNotFoundError:
    from actions_daily_remote import RemoteStop, REPO
    import jquants_ingest as ingest
    from jquants_daily import NAMES
    from jquants_catalog import BY_NAME, BASE_URL


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class FixedHTTP:
    def __init__(self, origin, headers, *, opener=None, limit=32*1024*1024, timeout=30):
        if origin not in {'https://api.github.com', BASE_URL}:
            raise RemoteStop('unreviewed HTTP origin')
        self.origin, self.headers = origin, dict(headers)
        self.opener, self.limit, self.timeout = opener or build_opener(NoRedirect()), limit, timeout

    def json(self, method, path, body=None):
        # Reject alternate authorities, traversal, fragments and encoded separators.
        parsed = urlsplit(path)
        if (method not in {'GET','POST','PATCH','PUT'} or not path.startswith('/') or path.startswith('//')
                or parsed.scheme or parsed.netloc or parsed.fragment or '\\' in path
                or any(p in {'.','..'} for p in parsed.path.split('/'))
                or re.search(r'%(?:2f|5c|2e|00|0a|0d)', parsed.path, re.I)):
            raise RemoteStop('unsafe HTTP request target')
        data = None if body is None else json.dumps(body, separators=(',', ':')).encode()
        if data is not None and len(data) > self.limit:
            raise RemoteStop('HTTP request exceeds limit')
        request = Request(self.origin + path, data=data, method=method,
                          headers={**self.headers, 'Accept':'application/vnd.github+json' if self.origin.endswith('github.com') else 'application/json',
                                   'Content-Type':'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                status = response.status
                if response.geturl() != request.full_url:
                    raise RemoteStop('redirect rejected')
                if not 200 <= status < 300:
                    return status, None
                raw = response.read(self.limit + 1)
                if len(raw) > self.limit:
                    raise RemoteStop('HTTP response exceeds limit')
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError()
                        result[key] = value
                    return result
                return status, json.loads(raw, object_pairs_hook=unique,
                                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except HTTPError as error:
            # Do not read the error response, headers, URL, or its exception text.
            return error.code, None
        except Exception:
            raise RemoteStop('HTTP outcome unknown or invalid; no automatic retry') from None


class GitHubTransport:
    def __init__(self, token, *, opener=None):
        if not token or '\n' in token or '\r' in token:
            raise RemoteStop('GitHub credential unavailable')
        self.http = FixedHTTP('https://api.github.com', {'Authorization':'Bearer '+token,
                              'X-GitHub-Api-Version':'2022-11-28'}, opener=opener)

    def __call__(self, method, path, body=None):
        prefix = '/repos/' + REPO
        if not (path == prefix or path.startswith(prefix + '/')):
            raise RemoteStop('unexpected GitHub repository')
        if method != 'GET':
            suffix = urlsplit(path).path[len(prefix):]
            if not (method == 'POST' and suffix in {'/git/blobs','/git/trees','/git/commits','/git/refs','/pulls'}
                    or method == 'PATCH' and suffix == '/git/refs/heads/automation/daily-checkpoint-v1'
                    or method == 'PUT' and re.fullmatch(r'/pulls/[1-9][0-9]*/merge',suffix)):
                raise RemoteStop('unreviewed GitHub write operation')
            if suffix == '/git/refs':
                ref = (body or {}).get('ref','')
                if not (ref == 'refs/heads/automation/daily-checkpoint-v1'
                        or re.fullmatch(r'refs/heads/daily/market/[0-9]{4}-[0-9]{2}-[0-9]{2}',ref)):
                    raise RemoteStop('unreviewed writable ref')
            if method == 'PATCH' and (body or {}).get('force') is not False:
                raise RemoteStop('force update forbidden')
            if method == 'PUT' and ((body or {}).get('merge_method') != 'squash'
                                   or not re.fullmatch('[0-9a-f]{40}',(body or {}).get('sha',''))):
                raise RemoteStop('exact-head squash required')
        status, payload = self.http.json(method, path, body)
        if not 200 <= status < 300:
            raise RemoteStop('GitHub request rejected; status=' + str(status))
        return payload

    def review_decision(self, number, head, reviews):
        if type(number) is not int or number < 1:
            raise RemoteStop('invalid PR number')
        query = '''query($number:Int!) { repository(owner:"kkyosuke",name:"stock.jp.private") {
          pullRequest(number:$number) { headRefOid reviewDecision } } }'''
        status,payload = self.http.json('POST','/graphql',{'query':query,'variables':{'number':number}})
        if status != 200 or payload.get('errors'):
            raise RemoteStop('review evidence unavailable')
        pr = payload['data']['repository']['pullRequest']
        if pr['headRefOid'] != head or pr['reviewDecision'] not in {None,'APPROVED'}:
            raise RemoteStop('review or exact head not satisfied')
        return pr['reviewDecision']


class ProviderTransport:
    def __init__(self, key, *, opener=None):
        if not key or '\n' in key or '\r' in key:
            raise RemoteStop('provider credential unavailable')
        self.http = FixedHTTP(BASE_URL, {'x-api-key':key}, opener=opener, limit=8*1024*1024)

    def __call__(self, endpoint, query):
        if endpoint not in {BY_NAME[n].endpoint for n in NAMES}:
            raise RemoteStop('unreviewed provider endpoint')
        try:
            status, payload = self.http.json('GET', endpoint + ('?' + urlencode(query) if query else ''))
        except RemoteStop:
            raise ingest.ApiError(0, 'network_or_invalid_response') from None
        if status != 200:
            raise ingest.ApiError(status, 'provider_error')
        return payload
