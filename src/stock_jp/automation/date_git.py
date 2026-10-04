"""Narrow private Git adapter for reviewed date grants and result PRs.

No main writes, merges, force updates, branch deletion or credential discovery.
"""
import base64
import json
import re
import stock_jp.automation.http as http
import stock_jp.automation.daily_remote as r
import stock_jp.automation.authorization as auth
import stock_jp.automation.publication as pub
import stock_jp.market_data.storage as codec
import stock_jp.automation.date_grants as grants

PREFIX = pub.PREFIX
BRANCH = r'date-acquisition/(?:reserve|result)/[0-9]{4}-[0-9]{2}-[0-9]{2}'


class Transport:
    def __init__(self, token):
        r.require(bool(token) and '\n' not in token and '\r' not in token, 'GitHub credential unavailable')
        self.http = http.FixedHTTP('https://api.github.com', {'Authorization':'Bearer '+token, 'X-GitHub-Api-Version':'2022-11-28'})

    def __call__(self, method, path, body=None):
        r.require(path == PREFIX or path.startswith(PREFIX+'/'), 'unexpected repository')
        suffix = path[len(PREFIX):]
        if method != 'GET':
            allowed = method == 'POST' and suffix in {'/git/blobs','/git/trees','/git/commits','/git/refs','/pulls'}
            allowed |= method == 'PATCH' and bool(re.fullmatch('/git/refs/heads/'+BRANCH, suffix)) and (body or {}).get('force') is False
            r.require(allowed, 'unreviewed date Action write')
            if suffix == '/git/refs':
                r.require(bool(re.fullmatch('refs/heads/'+BRANCH, (body or {}).get('ref',''))), 'unreviewed date ref')
            if suffix == '/pulls':
                r.require((body or {}).get('base') == 'main' and body.get('draft') is True
                          and re.fullmatch(BRANCH, body.get('head','')), 'unreviewed date PR')
        status, value = self.http.json(method, path, body)
        if not 200 <= status < 300: raise http.Rejected(status)
        return value


def blob(request, digest):
    item = request('GET', PREFIX+'/git/blobs/'+r.sha(digest), None)
    r.require(item.get('encoding') == 'base64' and type(item.get('size')) is int and item['size'] <= codec.MAX_SHARD, 'invalid data blob')
    raw = base64.b64decode(''.join(item['content'].split()), validate=True)
    r.require(len(raw) == item['size'] and r.git_blob(raw) == digest, 'data blob hash mismatch')
    codec.scan(raw)
    return raw


def read(request, tree, path):
    entry = tree.get(path)
    r.require(entry is not None and entry[:2] == ('100644','blob'), 'required adopted file missing')
    return blob(request, entry[2])


def ref(request, branch):
    try:
        return r.sha(request('GET', PREFIX+'/git/ref/heads/'+branch, None)['object']['sha'])
    except http.Rejected as error:
        if error.status == 404: return None
        raise


class Branch:
    def __init__(self, request, main, target, kind):
        r.require(kind in {'reserve','result'}, 'invalid date branch kind')
        grants.day(target)
        self.request, self.main, self.target, self.kind = request, main, target, kind
        self.branch = 'date-acquisition/'+kind+'/'+target
        self.revision = ref(request, self.branch)
        self.created = self.revision is not None

    def observe(self):
        r.require(ref(self.request, 'main') == self.main, 'main changed; stop before further provider calls')
        r.require(ref(self.request, self.branch) == self.revision, 'date branch changed or disappeared')

    def save(self, files):
        self.observe()
        parent = self.revision or self.main
        tree = auth.complete_tree(self.request, parent)
        commit = self.request('GET', PREFIX+'/git/commits/'+parent, None)
        inventory = self.request('GET', PREFIX+'/git/trees/'+r.sha(commit['tree']['sha'])+'?recursive=1', None)
        r.require(inventory.get('truncated') is False, 'data size inventory truncated')
        total = 0
        for entry in inventory['tree']:
            if (entry['path'].startswith('data/market/') or entry['path'] == grants.LEDGER) and entry['type'] == 'blob' and entry['path'] not in files:
                r.require(type(entry.get('size')) is int and entry['size'] >= 0, 'data size unavailable')
                total += entry['size']
        r.require(total + sum(len(raw) for raw in files.values()) <= codec.MAX_STORE, 'private data store size budget exceeded')
        items = []
        for path, raw in sorted(files.items()):
            valid = path == grants.LEDGER if self.kind == 'reserve' else bool(re.fullmatch(
                re.escape(grants.ROOT+self.target+'/')+r'(?:result\.json|(?:trading_calendar|equities_master|equities_daily)/[0-9]{3}\.json)', path))
            r.require(valid and len(raw) <= codec.MAX_SHARD, 'unreviewed result file')
            codec.scan(raw); json.loads(raw)
            if path in tree and path != grants.LEDGER and not path.endswith('/result.json'):
                r.require(tree[path] == ('100644','blob',r.git_blob(raw)), 'immutable response overwrite')
            sha = self.request('POST', PREFIX+'/git/blobs', dict(content=base64.b64encode(raw).decode(), encoding='base64'))['sha']
            r.require(sha == r.git_blob(raw), 'uploaded blob identity mismatch')
            items.append(dict(path=path, mode='100644', type='blob', sha=sha))
        commit = self.request('GET', PREFIX+'/git/commits/'+parent, None)
        newtree = self.request('POST', PREFIX+'/git/trees', dict(base_tree=r.sha(commit['tree']['sha']), tree=items))['sha']
        child = self.request('POST', PREFIX+'/git/commits', dict(message='Record date acquisition '+self.kind, tree=r.sha(newtree), parents=[parent]))['sha']
        r.sha(child); self.observe()
        if self.revision:
            self.request('PATCH', PREFIX+'/git/refs/heads/'+self.branch, dict(sha=child, force=False))
        else:
            self.request('POST', PREFIX+'/git/refs', dict(ref='refs/heads/'+self.branch, sha=child))
        # Unknown writes never cause another send. Acknowledgement includes both
        # exact ref and the uploaded files read back from their Git object IDs.
        r.require(ref(self.request, self.branch) == child, 'date commit acknowledgement missing')
        actual = auth.complete_tree(self.request, child)
        for path, raw in files.items():
            r.require(read(self.request, actual, path) == raw, 'date commit restore mismatch')
        self.revision = child

    def publish(self):
        self.observe()
        base = auth.complete_tree(self.request, self.main)
        head = auth.complete_tree(self.request, self.revision)
        for path in base.keys() | head.keys():
            if base.get(path) == head.get(path): continue
            allowed = path == grants.LEDGER if self.kind == 'reserve' else bool(re.fullmatch(
                re.escape(grants.ROOT+self.target+'/')+r'(?:result\.json|(?:trading_calendar|equities_master|equities_daily)/[0-9]{3}\.json)', path))
            r.require(allowed and head.get(path, ())[:2] == ('100644','blob')
                      and (self.kind == 'reserve' or path not in base), 'candidate is not an additive date data PR')
        if self.kind == 'reserve':
            before, after = (json.loads(read(self.request, t, grants.LEDGER)) for t in (base, head))
            grants.extends(before, after)
            r.require(len(after['grants']) == len(before['grants'])+1 and after['grants'][-1]['date'] == self.target, 'reservation PR must add exactly one grant')
        prs = pub.pages(self.request, '/pulls?state=all')
        matches = [p for p in prs if p['head']['ref'] == self.branch]
        if matches:
            r.require(len(matches) == 1 and matches[0]['head']['sha'] == self.revision
                      and matches[0]['head']['repo']['full_name'] == r.REPO
                      and matches[0]['state'] == 'open', 'existing date PR requires review')
            return matches[0]['html_url']
        item = self.request('POST', PREFIX+'/pulls', dict(base='main', head=self.branch, draft=True,
            title='Date acquisition '+self.kind+' '+self.target,
            body=('Data-only '+self.kind+' for '+self.target+'. PAPER / HUMAN_ONLY. '
                  'The full reserved allowance remains charged; no automatic merge or refund.')))
        return item['html_url']
