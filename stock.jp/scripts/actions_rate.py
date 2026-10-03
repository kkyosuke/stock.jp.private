"""Conservative independent GitHub pacing and per-hour budgets."""
import time
try:
    from scripts.actions_daily_remote import RemoteStop
except ModuleNotFoundError:
    from actions_daily_remote import RemoteStop

class BudgetPause(RemoteStop):pass
class RateLimited(RemoteStop):
    def __init__(self,until):self.until=until;super().__init__('GitHub rate limit; known rejection')

class BudgetedTransport:
    READ_LIMIT=2400
    WRITE_LIMIT=360
    def __init__(self,request,*,clock=time.time,sleep=time.sleep,deadline=None):
        self.request=request;self.clock=clock;self.sleep=sleep;self.deadline=deadline or clock()+5*3600
        self.restored=False
        self.window=int(clock()//3600);self.reads=self.writes=0;self.last_read=self.last_write=0.;self.blocked=0.
    def state(self):
        # Allowance for checkpoint CAS/readback after this state is serialized.
        return dict(window=self.window,reads=self.reads+24,writes=self.writes+8,
                    last_read=self.last_read,last_write=self.last_write,blocked=self.blocked)
    def restore(self,value):
        if self.restored:return
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
    def _paced(self,method,operation):
        for attempt in range(2):
            self._roll();write=method!='GET'
            earliest=max(self.clock(),self.blocked,self.last_read+1.6,self.last_write+10.1 if write else 0)
            if self.reads>=self.READ_LIMIT or write and self.writes>=self.WRITE_LIMIT:
                earliest=max(earliest,(self.window+1)*3600)
            if earliest>=self.deadline:raise BudgetPause('GitHub budget deadline; stop without sending')
            self.sleep(max(0,earliest-self.clock()));self._roll()
            self.reads+=1;self.last_read=self.clock()
            if write:self.writes+=1;self.last_write=self.clock()
            try:return operation()
            except RateLimited as error:
                self.blocked=max(self.blocked,error.until)
                if attempt:raise BudgetPause('GitHub rate pause; known rejection') from None
        raise AssertionError()
