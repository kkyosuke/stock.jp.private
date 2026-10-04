from stock_jp.workspace import record_path
import base64
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import stock_jp.automation.sharded as s
import stock_jp.automation.daily_remote as r
import stock_jp.automation.publication as pub
import tests.test_actions_daily_remote as f

class Git(f.GitMock):
    def __call__(self,method,path,body):
        if method=='GET':path=path.split('?')[0]
        if method=='POST' and path.endswith('/git/refs'):
            if self.head is not None:raise OSError('ref exists')
            self.head=body['sha'];return {'object':{'sha':self.head}}
        return super().__call__(method,path,body)

class ShardedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'source';self.source.mkdir(parents=True)
        self.git=Git();self.git.head=None
        self.store=s.ShardedStore(self.git,authorization=f.authorization(),cache=self.root/'cache')
    def test_roundtrip_and_json_has_no_inline_payload(self):
        pub.bootstrap_checkpoint(self.store,self.source,'actions-v1')
        revision,document=self.store.read()
        self.assertEqual(document['snapshot']['format'],'git-shards-v1')
        self.store.restore(document['snapshot'],self.root/'restored',r.reject_history)
        original=self.git.head
        document['fence']='f'*32;self.store.cas(revision,document)
        self.assertNotEqual(original,self.git.head)
        with self.assertRaises(r.RemoteStop):self.store.cas(revision,document)

    def test_legacy_snapshot_cold_restore_then_plain_capture_preserves_ledger(self):
        import json
        codec=s.codec
        legacy=self.root/'legacy'
        raw=b'{"data":[]}\n'
        chunk=codec.put(legacy,raw)
        index=codec.encoded(dict(version=1,files={'jquants/empty.json':
            dict(sha256=codec.digest(raw),bytes=len(raw),chunks=[chunk])}))
        generation=codec.put(legacy,index,'generations')
        codec.atomic(legacy/'current.json',codec.encoded(dict(version=1,generation=generation,
            file_count=1,raw_bytes=len(raw))))
        snapshot={'format':'git-shards-v1','files':{}}
        for path in legacy.rglob('*'):
            if path.is_file():
                data=path.read_bytes()
                snapshot['files'][path.relative_to(legacy).as_posix()]={
                    'sha256':codec.digest(data),'bytes':len(data),'chunks':[self.store.put_blob(data)]}
        document=dict(version=2,writer='actions-v1',fence='a'*32,blocked_campaigns=[],runs={},
            snapshot=snapshot,github_budget=dict(window=1,reads=12,writes=4,last_read=0.,last_write=0.,blocked=0.))
        revision=self.store.create(document)
        cold=s.ShardedStore(self.git,authorization=f.authorization(),cache=self.root/'cold-cache')
        _,loaded=cold.read()
        target=self.root/'cold-source';cold.restore(loaded['snapshot'],target,r.reject_history)
        self.assertEqual((target/'jquants/empty.json').read_bytes(),raw)
        before=copy.deepcopy(loaded)
        loaded['snapshot']=cold.capture(target,r.reject_history)
        self.assertFalse(any(name.endswith('.gz') for name in loaded['snapshot']['files']))
        loaded['fence']='b'*32
        cold.cas(revision,loaded)
        _,after=cold.read()
        for key in set(before)-{'snapshot','fence'}:self.assertEqual(before[key],after[key])
        cold.restore(after['snapshot'],self.root/'plain-restored',r.reject_history)
        self.assertEqual((self.root/'plain-restored/jquants/empty.json').read_bytes(),raw)
    def test_large_store_shards_stay_under_existing_limits(self):
        # 24 MiB of independent incompressible wire chunks. No provider rows.
        # Metadata stays small; inline snapshot would exceed its 20 MiB cap.
        import hashlib
        files={};members=[]
        for index in range(24):
            raw=(hashlib.sha256(str(index).encode()).digest()*(s.codec.CHUNK//32))
            digest=self.store.put_blob(raw)
            name='objects/'+hashlib.sha256(raw).hexdigest()[:2]+'/'+hashlib.sha256(raw).hexdigest()+'.gz'
            files[name]={'sha256':s.codec.digest(raw),'bytes':len(raw),'chunks':[digest]}
        raw=b'{}';d=self.store.put_blob(raw)
        files['current.json']={'sha256':s.codec.digest(raw),'bytes':len(raw),'chunks':[d]}
        snapshot={'format':'git-shards-v1','files':files}
        s.validate_snapshot(snapshot)
        self.assertGreater(sum(e['bytes'] for e in files.values()),r.LIMIT)
        self.assertLess(len(s.codec.encoded(snapshot)),r.LIMIT)
        self.assertTrue(all(len(base64.b64decode(body['content']))<=s.codec.CHUNK
                            for method,path,body in self.git.calls if path.endswith('/git/blobs') and method=='POST'))
        with patch.object(s.codec,'MAX_STORE',1024):
            with self.assertRaises(r.RemoteStop):s.validate_snapshot(snapshot)
    def test_real_daily_runner_with_sharded_restart(self):
        fixture=f.RemoteDailyTests('test_normal_and_fresh_runner_same_day_dedup');fixture.setUp();self.addCleanup(fixture.doCleanups)
        source=record_path(fixture.private.parent, 'market-data');source.mkdir(parents=True,exist_ok=True)
        pub.bootstrap_checkpoint(self.store,source,'actions-v1')
        remote=lambda:r.RunnerCheckpoint(self.store,writer='actions-v1',authorization=f.authorization(),clock=lambda:fixture.at)
        first=fixture.run_daily(remote=remote());self.assertEqual(first['status'],'COLLECTED')
        fixture.fresh_disk()
        self.assertEqual(fixture.run_daily(remote=remote()),first)
        self.assertEqual(len(fixture.calls),9)

    def test_280_sessions_4000_bars_real_codec_capacity(self):
        import random,json,hashlib
        randomizer=random.Random(20261003);raw_bytes=0
        for day in range(280):
            rows=[dict(Date=f'synthetic-{day}',Code=str(code),O=randomizer.randrange(100,10000000)/100,
                       H=randomizer.randrange(100,10000000)/100,L=randomizer.randrange(100,10000000)/100,
                       C=randomizer.randrange(100,10000000)/100,Vo=randomizer.randrange(1,1000000000),
                       Va=randomizer.randrange(1,100000000000)) for code in range(4000)]
            data=json.dumps({'data':rows},separators=(',',':')).encode();digest=hashlib.sha256(data).hexdigest()
            s.codec.atomic(self.source/'jquants/objects'/digest[:2]/(digest+'.json'),data);raw_bytes+=len(data)
        snapshot=self.store.capture(self.source,r.reject_history)
        compressed=sum(e['bytes'] for e in snapshot['files'].values())
        metadata=len(s.codec.encoded(snapshot))
        self.assertGreater(compressed*4//3,r.LIMIT)
        self.assertLess(metadata,r.LIMIT//10)
        self.assertLess(compressed,s.codec.MAX_STORE)
        calls=len(self.git.calls);self.assertEqual(self.store.capture(self.source,r.reject_history),snapshot)
        self.assertEqual(len(self.git.calls),calls)  # all immutable shards reused
        self.store.restore(snapshot,self.root/'restored',r.reject_history)
        for path in self.source.rglob('*.json'):
            restored=self.root/'restored'/path.relative_to(self.source)
            self.assertEqual(s.codec.digest(path.read_bytes()),s.codec.digest(restored.read_bytes()))
        print(json.dumps(dict(capacity_fixture='280x4000',raw_bytes=raw_bytes,stored_bytes=compressed,ledger_snapshot_bytes=metadata)))

    def test_restored_snapshot_counts_toward_first_reservation_headroom(self):
        import random
        rng=random.Random(59)
        # Valid retained object with high-entropy rows: the restored wire store
        # exceeds 8 MiB, leaving less than the reserved 16 MiB at a 24 MiB cap.
        for _ in range(3):
            payload=s.codec.encoded({'data':[{'synthetic':rng.randbytes(64).hex()} for _ in range(48000)]})
            self.assertLess(len(payload),8*1024**2)
            digest=s.codec.digest(payload)
            s.codec.atomic(self.source/'jquants/objects'/digest[:2]/(digest+'.json'),payload)
        snapshot=self.store.capture(self.source,r.reject_history)
        total=sum(e['bytes'] for e in snapshot['files'].values())
        self.assertGreater(total,8*1024**2);self.assertLess(total,24*1024**2)
        cold=s.ShardedStore(self.git,authorization=f.authorization(),cache=self.root/'cold'/'cache')
        cold.restore(snapshot,self.root/'restored',r.reject_history)
        self.assertEqual(sum(p.stat().st_size for p in cold.codec_cache.rglob('*') if p.is_file()),0)
        with patch.object(s.codec,'MAX_STORE',24*1024**2):
            with self.assertRaisesRegex(r.RemoteStop,'headroom'):cold.ensure_capacity(self.root/'restored')
