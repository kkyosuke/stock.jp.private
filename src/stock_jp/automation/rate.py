"""Conservative independent GitHub pacing and per-hour budgets."""
import time
import math
from stock_jp.automation.daily_remote import RemoteStop

class BudgetPause(RemoteStop):pass
class RateLimited(RemoteStop):
    def __init__(self,until):self.until=until;super().__init__('GitHub rate limit; known rejection')

class BudgetedTransport:
    # GITHUB_TOKEN allows 1,000 REST requests/hour/repository. Count writes too
    # and leave room for Actions requests outside this transport.
    READ_LIMIT=800
    WRITE_LIMIT=360
    def __init__(self,request,*,clock=time.time,sleep=time.sleep,deadline=None):
        self.request=request;self.clock=clock;self.sleep=sleep;self.deadline=deadline or clock()+5*3600
        self.restored=False;self.wait_epoch=0
        self.window=int(clock()//3600);self.reads=self.writes=0;self.last_read=self.last_write=0.;self.blocked=0.
    def state(self):
        # Allowance for checkpoint CAS/readback after this state is serialized.
        return dict(window=self.window,reads=self.reads+24,writes=self.writes+8,
                    last_read=self.last_read,last_write=self.last_write,blocked=self.blocked)
    def restore(self,value):
        if self.restored:return
        if value is not None:
            if not isinstance(value,dict) or set(value)!={'window','reads','writes','last_read','last_write','blocked'}:
                raise RemoteStop('invalid saved GitHub budget')
            if any(type(value[key]) is not int or value[key]<0 for key in ('window','reads','writes')) or any(
                type(value[key]) not in (int,float) or not math.isfinite(value[key]) or value[key]<0
                for key in ('last_read','last_write','blocked')):
                raise RemoteStop('invalid saved GitHub budget')
        self.restored=True
        if value and value['window']==self.window:
            self.reads+=value['reads'];self.writes+=value['writes']
            self.last_read=max(self.last_read,value['last_read']);self.last_write=max(self.last_write,value['last_write'])
            self.blocked=max(self.blocked,value['blocked'])
    def _roll(self):
        if int(self.clock()//3600)!=self.window:self.window=int(self.clock()//3600);self.reads=self.writes=0
    def _headroom(self,reads,writes,slack):
        self._roll()
        earliest=max(self.clock(),self.blocked)
        if self.reads+reads>self.READ_LIMIT or self.writes+writes>self.WRITE_LIMIT:
            earliest=max(earliest,(self.window+1)*3600)
        if earliest+slack>self.deadline:
            raise BudgetPause('GitHub checkpoint headroom unavailable before new work')
        # Existing reservations/pages are already durable. Waiting at this point
        # consumes no provider reservation, retains the live object cache, and
        # does not reset the persisted acquisition budget.
        self.sleep(max(0,earliest-self.clock()));self._roll()
    def before_publication(self):self._headroom(80,24,600)
    def before_provider(self):self._headroom(160,64,900)
    def review_decision(self,number,head,reviews):
        return self._paced('GET',lambda:self.request.review_decision(number,head,reviews))
    def __call__(self,method,path,body=None):
        return self._paced(method,lambda:self.request(method,path,body))
    def mutation(self,method,path,body,*,before_io,prepare_io=None):
        if method=='GET':raise RemoteStop('guarded mutation requires a write')
        return self._paced(method,lambda:self.request(method,path,body),before_io=before_io,prepare_io=prepare_io)
    def _paced(self,method,operation,*,before_io=None,prepare_io=None):
        for attempt in range(2):
            write=method!='GET';prepared=False
            while True:
                self._roll()
                earliest=max(self.clock(),self.blocked,self.last_read+1.6,self.last_write+10.1 if write else 0)
                quota_wait=self.reads>=self.READ_LIMIT or write and self.writes>=self.WRITE_LIMIT
                if quota_wait:
                    earliest=max(earliest,(self.window+1)*3600)
                if earliest>=self.deadline:raise BudgetPause('GitHub budget deadline; stop without sending')
                if earliest>self.clock() and (quota_wait or self.clock()<self.blocked):
                    self.wait_epoch+=1;prepared=False
                self.sleep(max(0,earliest-self.clock()));self._roll()
                if prepare_io and not prepared:
                    epoch=self.wait_epoch
                    prepare_io();prepared=epoch==self.wait_epoch
                    continue  # re-budget/pace after evidence reads; no final observation yet
                if before_io:before_io()
                if self.clock()>=self.deadline:raise BudgetPause('GitHub budget deadline after final observation')
                self._roll()
                # A slow observation (or callback doing paced reads) can change
                # the time window/counters. Any further wait needs a new check.
                if before_io and (self.clock()<max(self.blocked,self.last_read+1.6,self.last_write+10.1 if write else 0)
                    or self.reads>=self.READ_LIMIT or write and self.writes>=self.WRITE_LIMIT):continue
                break
            self.reads+=1;self.last_read=self.clock()
            if write:self.writes+=1;self.last_write=self.clock()
            try:return operation()
            except RateLimited as error:
                self.blocked=max(self.blocked,error.until)
                if attempt:raise BudgetPause('GitHub rate pause; known rejection') from None
        raise AssertionError()
