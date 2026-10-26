"""Refresh complete book coverage without interrupting a continuous diff stream."""
import asyncio
import contextlib
import logging
import time

LOG=logging.getLogger(__name__)


class DepthRefresh:
    def __init__(self, fetch, interval=15, stamp=None):
        self.fetch=fetch;self.interval=max(.001,float(interval));self.stamp=stamp or (lambda:int(time.time()*1000))
        self.task=None;self.payload=None;self.buffer=[];self.start_id=None
        self.next_at=0.;self.observed_ms=0;self.last_success_ms=0;self.count=0;self.error=None

    def remember(self,event):
        if self.task is not None or self.payload is not None:
            self.buffer.append(event)
            if len(self.buffer)>4096:
                self._discard('Depth snapshot replay buffer exceeded',self.interval)

    def _discard(self,error,delay):
        if self.task is not None:self.task.cancel()
        self.task=None;self.payload=None;self.buffer=[]
        self.next_at=time.monotonic()+max(self.interval,delay)
        self.error=str(error)
        LOG.warning('Book coverage refresh deferred: %s',self.error)

    def poll(self,book):
        now=time.monotonic()
        if self.task is not None and self.task.done():
            try:
                self.payload=self.task.result();self.observed_ms=self.stamp()
                if self.payload['lastUpdateId']<self.start_id:
                    raise ValueError('REST snapshot predates replay buffer')
            except Exception as error:
                self._discard(error,getattr(error,'retry_after',self.interval));return None
            self.task=None
        if self.payload is not None and self.payload['lastUpdateId']<=book.update_id:
            try:
                candidate=book.refreshed(self.payload,self.buffer)
            except Exception as error:
                self._discard(error,self.interval);return None
            self.payload=None;self.buffer=[];self.next_at=now+self.interval
            self.last_success_ms=self.observed_ms;self.count+=1;self.error=None
            return candidate
        if self.task is None and self.payload is None and now>=self.next_at:
            self.start_id=book.update_id;self.buffer=[]
            self.task=asyncio.create_task(asyncio.to_thread(self.fetch))
        return None

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError,Exception):await self.task
        self.task=None;self.payload=None;self.buffer=[]
