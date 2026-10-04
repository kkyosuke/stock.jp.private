import base64,tempfile,unittest
from pathlib import Path
from scripts.actions_cache import ImmutableCache
from scripts import actions_daily_remote as r

class CacheTests(unittest.TestCase):
    def test_only_immutable_ids_reuse_transport_reads(self):
        calls=[]
        def request(m,p,b):calls.append((m,p));return {'value':len(calls)}
        with tempfile.TemporaryDirectory() as tmp:
            cache=ImmutableCache(request,tmp);prefix='/repos/'+r.REPO
            immutable=prefix+'/git/trees/'+'a'*40+'?recursive=1'
            self.assertEqual(cache('GET',immutable),cache('GET',immutable));self.assertEqual(len(calls),1)
            for path in ['/git/ref/heads/main','/pulls/1','/pulls/1/reviews','/contents/file?ref=main']:
                self.assertNotEqual(cache('GET',prefix+path),cache('GET',prefix+path))
            self.assertEqual(len(calls),9)
    def test_verified_blob_reuse_and_bounded_eviction(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache=ImmutableCache(lambda *a:self.fail('verified immutable blob should not refetch'),tmp)
            raw=b'synthetic';sha=r.git_blob(raw);cache.seed_blob(sha,raw)
            self.assertTrue(cache.has_verified_blob(sha,raw))
            self.assertFalse(cache.has_verified_blob('b'*40,b'unknown'))
            result=cache('GET','/repos/'+r.REPO+'/git/blobs/'+sha)
            self.assertEqual(base64.b64decode(result['content']),raw)
            with self.assertRaises(ValueError):cache.seed_blob('a'*40,raw)
            cache.limit=128
            for i in range(8):cache.save(str(i),{'payload':'x'*80})
            self.assertLessEqual(cache.bytes,cache.limit)
            self.assertEqual(sum(p.stat().st_size for p in Path(tmp).iterdir()),cache.bytes)

    def test_checkpoint_reuses_only_verified_remote_blob(self):
        from scripts.actions_sharded import ShardedStore
        from test_actions_daily_remote import authorization
        raw=b'read from adopted main';sha=r.git_blob(raw);calls=[]
        def request(method,path,body):
            calls.append((method,path))
            if method=='GET':return dict(size=len(raw),encoding='base64',content=base64.b64encode(raw).decode())
            return {'sha':r.git_blob(base64.b64decode(body['content']))}
        with tempfile.TemporaryDirectory() as tmp:
            cache=ImmutableCache(request,Path(tmp)/'http')
            cache('GET','/repos/'+r.REPO+'/git/blobs/'+sha)
            store=ShardedStore(cache,authorization=authorization(),cache=Path(tmp)/'shards')
            self.assertEqual(store.put_blob(raw),sha)
            self.assertEqual(len(calls),1)
            store.put_blob(b'not previously in remote cache')
            self.assertEqual([method for method,_ in calls],['GET','POST'])
