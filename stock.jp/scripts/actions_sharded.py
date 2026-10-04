"""Reachable Git blob shards; checkpoint JSON holds identities, never payloads."""
import base64
import copy
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
try:
    from scripts import actions_daily_remote as r, market_data_git as codec
except ModuleNotFoundError:
    import actions_daily_remote as r, market_data_git as codec


def member(name):
    return codec.store_member(name)


def validate_snapshot(snapshot):
    r.require(set(snapshot)=={'format','files'} and snapshot['format']=='git-shards-v1','invalid sharded snapshot')
    total=0
    for name,entry in snapshot['files'].items():
        r.require(member(name) and set(entry)=={'sha256','bytes','chunks'},'invalid snapshot member')
        r.require(re.fullmatch('[0-9a-f]{64}',entry['sha256']) and type(entry['bytes']) is int
                  and entry['bytes']>=0 and isinstance(entry['chunks'],list),'invalid shard index')
        r.require(len(entry['chunks'])==(entry['bytes']+codec.CHUNK-1)//codec.CHUNK,'invalid shard count')
        for digest in entry['chunks']:r.sha(digest)
        total+=entry['bytes']
    r.require('current.json' in snapshot['files'] and total<=codec.MAX_STORE,'snapshot store limit')


class ShardedStore(r.GitCheckpointStore):
    def __init__(self, request, *, authorization, cache):
        super().__init__(request,authorization=authorization)
        self.cache=Path(cache);self.cache.mkdir(parents=True,exist_ok=True)
        self.known=set();self.file_index={};self.snapshot_bytes=0
        self.codec_cache=self.cache.parent/'codec-cache';self.codec_cache.mkdir(exist_ok=True)

    def ensure_capacity(self,source):
        try:
            from scripts import history_resources as limits
        except ModuleNotFoundError:
            import history_resources as limits
        source=Path(source)
        limits.enforce(limits.inspect(source,self.cache.parent))
        # Reserve enough compressed space for the largest accepted provider response.
        used=max(self.snapshot_bytes,sum(p.stat().st_size for p in self.codec_cache.rglob('*') if p.is_file()))
        r.require(used+16*1024**2<codec.MAX_STORE,'checkpoint store lacks response headroom')

    def blob(self, digest, limit=codec.CHUNK):
        r.sha(digest);path=self.cache/digest
        if path.exists():
            data=path.read_bytes()
        else:
            value=self.call('GET','/git/blobs/'+digest)
            r.require(value['encoding']=='base64' and 0<=value['size']<=limit,'oversized shard')
            data=base64.b64decode(''.join(value['content'].split()),validate=True)
            r.require(len(data)==value['size'],'shard length mismatch')
            codec.atomic(path,data)
        r.require(len(data)<=limit and r.git_blob(data)==digest,'shard hash mismatch')
        self.known.add(digest)
        return data

    def put_blob(self,data):
        r.require(len(data)<=r.LIMIT,'Git metadata/blob limit')
        digest=r.git_blob(data)
        if digest not in self.known and hasattr(self.request,'has_verified_blob') and self.request.has_verified_blob(digest,data):
            self.known.add(digest)
        if digest not in self.known:
            value=self.call('POST','/git/blobs',{'content':base64.b64encode(data).decode(),'encoding':'base64'})
            r.require(value['sha']==digest,'uploaded shard hash mismatch')
            if hasattr(self.request,'seed_blob'):self.request.seed_blob(digest,data)
            self.known.add(digest)
        codec.atomic(self.cache/digest,data)
        return digest

    def capture(self,source,history_check):
        """Caller owns daily workspace/archive lock. Read bounded individual files.

        Unlike the old inline format, no full archive copy or base64 payload map
        is built. Complete plain files stay below MAX_SHARD; Git transport alone
        splits them into CHUNK-sized blobs. Old gzip snapshots remain readable.
        """
        source=Path(source);history_check(source);codec.safe_tree(source)
        with tempfile.TemporaryDirectory(dir=self.cache.parent) as tmp:
            store=Path(tmp);entries={}
            paths=sorted(p for p in source.rglob('*') if p.is_file() and p.name!='.ingestion.lock')
            before={p:(p.stat().st_ino,p.stat().st_size,p.stat().st_mtime_ns) for p in paths}
            for path in paths:
                name=path.relative_to(source).as_posix();codec.safe_name(name)
                # Retained response files are independently bounded by the
                # provider/resource limits; codec scan must cover token boundaries.
                r.require(path.stat().st_size<=codec.MAX_SHARD,'plain file exceeds 22 MiB')
                data=path.read_bytes();codec.scan(data)
                cached=self.file_index.get(name)
                if cached and cached[0]==before[path] and cached[1]['sha256']==codec.digest(data):
                    entry=copy.deepcopy(cached[1])
                else:
                    suffix=path.suffix if path.suffix in {'.json','.csv'} else '.bin'
                    entry={'sha256':codec.digest(data),'bytes':len(data),
                        'object':codec.put_plain(self.codec_cache,data,suffix)}
                    self.file_index[name]=(before[path],copy.deepcopy(entry))
                entries[name]=entry
                relative=Path(entry['object'])
                target=store/relative
                if not target.exists():
                    target.parent.mkdir(parents=True,exist_ok=True);os.link(self.codec_cache/relative,target)
            index=codec.encoded({'version':2,'files':entries})
            r.require(len(index)<=codec.MAX_INDEX,'generation index limit')
            generation=Path(codec.put_plain(store,index,'.json','generations')).stem
            pointer=dict(version=2,generation=generation,file_count=len(entries),raw_bytes=sum(x['bytes'] for x in entries.values()))
            codec.atomic(store/'current.json',codec.encoded(pointer));codec.verify(store)
            r.require(before=={p:(p.stat().st_ino,p.stat().st_size,p.stat().st_mtime_ns) for p in paths}
                      and paths==sorted(p for p in source.rglob('*') if p.is_file() and p.name!='.ingestion.lock'),
                      'source changed during checkpoint capture')
            snapshot={'format':'git-shards-v1','files':{}}
            for path in sorted(p for p in store.rglob('*') if p.is_file()):
                data=path.read_bytes()
                snapshot['files'][path.relative_to(store).as_posix()]={'sha256':codec.digest(data),'bytes':len(data),
                    'chunks':[self.put_blob(data[i:i+codec.CHUNK]) for i in range(0,len(data),codec.CHUNK)]}
            validate_snapshot(snapshot)
            self.snapshot_bytes=sum(e['bytes'] for e in snapshot['files'].values())
            referenced={entry['object'] for entry in entries.values()}
            for path in (self.codec_cache/'objects').rglob('*'):
                if path.is_file() and path.relative_to(self.codec_cache).as_posix() not in referenced:path.unlink()
            self.file_index={name:self.file_index[name] for name in entries}
            return snapshot

    def restore(self,snapshot,target,history_check):
        validate_snapshot(snapshot)
        self.snapshot_bytes=sum(e['bytes'] for e in snapshot['files'].values())
        with tempfile.TemporaryDirectory(dir=self.cache.parent) as tmp:
            store=Path(tmp)/'store'
            for name,entry in snapshot['files'].items():
                data=b''.join(self.blob(d) for d in entry['chunks'])
                r.require(len(data)==entry['bytes'] and codec.digest(data)==entry['sha256'],'codec member mismatch')
                codec.atomic(store/name,data)
            codec.restore(store,target);history_check(Path(target))

    def read(self):
        repo=self.call('GET','')
        r.require(repo.get('private') is True and repo.get('full_name')==r.REPO,'private repository required')
        revision=r.sha(self.call('GET','/git/ref/'+r.REF)['object']['sha'])
        commit=self.call('GET','/git/commits/'+revision)
        tree=self.call('GET','/git/trees/'+r.sha(commit['tree']['sha'])+'?recursive=1')
        r.require(tree.get('truncated') is False,'checkpoint tree truncated')
        entries={e['path']:e for e in tree['tree'] if e['type']!='tree'}
        r.require(all(e['type']=='blob' and e['mode']=='100644' for e in entries.values()),'unsafe checkpoint tree')
        r.require('checkpoint.json' in entries,'checkpoint metadata missing')
        raw=self.blob(entries['checkpoint.json']['sha'],r.LIMIT)
        document=json.loads(raw);r.validate(document)
        snapshot=document['snapshot'];validate_snapshot(snapshot)
        self.snapshot_bytes=sum(e['bytes'] for e in snapshot['files'].values())
        shards={d for entry in snapshot['files'].values() for d in entry['chunks']}
        r.require(set(entries)=={'checkpoint.json'}|{'shards/'+d for d in shards},'unreferenced checkpoint tree member')
        r.require(all(entries['shards/'+d]['sha']==d for d in shards),'shard tree identity mismatch')
        self.known.update(shards)
        return revision,document

    def _write(self,document,parent):
        r.validate(document);validate_snapshot(document['snapshot'])
        raw=codec.encoded(document);r.require(len(raw)<=r.LIMIT,'checkpoint metadata limit')
        digest=self.put_blob(raw)
        shards={d for e in document['snapshot']['files'].values() for d in e['chunks']}
        entries=[dict(path='checkpoint.json',mode='100644',type='blob',sha=digest)]
        entries += [dict(path='shards/'+d,mode='100644',type='blob',sha=d) for d in sorted(shards)]
        tree=self.call('POST','/git/trees',{'tree':entries})
        commit=self.call('POST','/git/commits',{'message':'Checkpoint '+document['fence']+' '+uuid.uuid4().hex,
                            'tree':r.sha(tree['sha']),'parents':[] if parent is None else [parent]})
        revision=r.sha(commit['sha'])
        if parent is None:
            response=self.call('POST','/git/refs',{'ref':'refs/'+r.REF,'sha':revision})
        else:
            response=self.call('PATCH','/git/refs/'+r.REF,{'sha':revision,'force':False})
        r.require(response['object']['sha']==revision,'checkpoint ref ACK missing')
        actual,value=self.read();r.require(actual==revision and value==document,'checkpoint readback mismatch')
        # Keep local staging bounded to the live ledger and its reachable shards.
        keep=shards|{digest}
        for path in self.cache.iterdir():
            if path.name not in keep:path.unlink()
        self.known=keep
        return revision

    def create(self,document):return self._write(document,None)

    def cas(self,expected,value):
        actual,old=self.read();r.require(actual==expected,'stale checkpoint parent')
        r.check_transition(old,value)
        return self._write(value,expected)
