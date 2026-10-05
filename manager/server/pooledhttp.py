"""Bounded transports per loop, upstream identity and proxy, with per-lease cookies."""
from __future__ import annotations
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import httpx

@dataclass
class Entry:
    transport: httpx.AsyncHTTPTransport
    references: int = 0

entries = OrderedDict()
closing = set()
MAX_POOLS = 64

class BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(self, entry): self.entry = entry
    async def handle_async_request(self, request):
        return await self.entry.transport.handle_async_request(request)
    async def aclose(self): pass

class LeaseClient(httpx.AsyncClient):
    def __init__(self, entry, **kwargs):
        self.entry = entry
        self.released = False
        entry.references += 1
        super().__init__(transport=BorrowedTransport(entry), **kwargs)
    async def aclose(self):
        if self.released: return
        try: await super().aclose()
        finally:
            self.released = True
            self.entry.references -= 1
    async def __aexit__(self, *args): await self.aclose()

def client(timeout, proxy, identity):
    if not identity:
        kwargs = {'timeout':timeout,'trust_env':False}
        if proxy: kwargs['proxy'] = proxy
        return httpx.AsyncClient(**kwargs)
    loop = asyncio.get_running_loop()
    key = (id(loop), hashlib.sha256((identity+'\0'+(proxy or '')).encode()).hexdigest())
    entry = entries.get(key)
    if entry is None:
        if len(entries) >= MAX_POOLS:
            victim = next((k for k,v in entries.items() if v.references==0 and k[0]==id(loop)), None)
            if victim is None: return client(timeout,proxy,None)
            old = entries.pop(victim)
            task = loop.create_task(old.transport.aclose())
            closing.add(task); task.add_done_callback(closing.discard)
        entry = Entry(httpx.AsyncHTTPTransport(proxy=proxy or None,trust_env=False,
                       limits=httpx.Limits(max_connections=8,max_keepalive_connections=4,keepalive_expiry=15)))
        entries[key] = entry
    entries.move_to_end(key)
    return LeaseClient(entry,timeout=timeout,trust_env=False,follow_redirects=False)

async def close():
    loop_id = id(asyncio.get_running_loop())
    keys = [k for k in entries if k[0]==loop_id]
    for key in keys:
        entry = entries.pop(key)
        await entry.transport.aclose()
    if closing: await asyncio.gather(*list(closing),return_exceptions=True)
