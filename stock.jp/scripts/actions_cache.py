"""Bounded per-execution cache for immutable Git objects only; mutable facts stay live."""
import hashlib
import base64
import json
from pathlib import Path
import re
try:
    from scripts import market_data_git as codec
except ModuleNotFoundError:
    import market_data_git as codec


class ImmutableCache:
    def __init__(self,request,root):
        self.request=request;self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.order={};self.bytes=0;self.limit=2*codec.MAX_STORE
    def __getattr__(self,name):return getattr(self.request,name)
    def __call__(self,method,path,body=None):
        immutable=method=='GET' and (re.search(r'/git/(?:commits|trees|blobs)/[0-9a-f]{40}(?:\?recursive=1)?$',path)
                    or re.search(r'/contents/[^?]+\?ref=[0-9a-f]{40}$',path))
        if not immutable:return self.request(method,path,body)
        key=hashlib.sha256(path.encode()).hexdigest();target=self.root/key
        if key in self.order:
            size=self.order.pop(key);self.order[key]=size
            return json.loads(target.read_bytes())
        result=self.request(method,path,body)
        self.save(path,result)
        return result

    def has_verified_blob(self,digest,raw):
        """Only reuse a blob already read from GitHub or acknowledged on upload."""
        path='/repos/kkyosuke/stock.jp.private/git/blobs/'+digest
        key=hashlib.sha256(path.encode()).hexdigest()
        if key not in self.order:return False
        actual=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        if actual!=digest:raise ValueError('immutable cache identity mismatch')
        value=json.loads((self.root/key).read_bytes())
        if value.get('encoding')!='base64' or value.get('size')!=len(raw):
            raise ValueError('immutable cache blob metadata mismatch')
        if base64.b64decode(''.join(value['content'].split()),validate=True)!=raw:
            raise ValueError('immutable cache blob bytes mismatch')
        return True

    def seed_blob(self,digest,raw):
        actual=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        if actual!=digest:raise ValueError('immutable cache identity mismatch')
        self.save('/repos/kkyosuke/stock.jp.private/git/blobs/'+digest,
                  dict(sha=digest,encoding='base64',content=base64.b64encode(raw).decode(),size=len(raw)))

    def save(self,path,result):
        key=hashlib.sha256(path.encode()).hexdigest();target=self.root/key
        if key in self.order:return
        raw=codec.encoded(result)
        if len(raw)<=self.limit:
            while self.order and self.bytes+len(raw)>self.limit:
                oldest=next(iter(self.order));self.bytes-=self.order.pop(oldest);(self.root/oldest).unlink()
            codec.atomic(target,raw);self.order[key]=len(raw);self.bytes+=len(raw)
