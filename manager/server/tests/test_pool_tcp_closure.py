"""Real TCP keepalive and cancellation; fixtures never contact a provider."""
import asyncio
import json
import os
import unittest
from pathlib import Path
from server import pooledhttp

class RealTCPPoolContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connections=0;self.requests=0;self.writers=set()
        self.server=await asyncio.start_server(self.handle,'127.0.0.1',0)
        self.url='http://127.0.0.1:'+str(self.server.sockets[0].getsockname()[1])
    async def handle(self,reader,writer):
        self.connections+=1;self.writers.add(writer)
        try:
            while True:
                header=await reader.readuntil(b'\r\n\r\n')
                self.requests+=1
                if b'/slow ' in header:
                    await asyncio.sleep(0.04)
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nok')
                await writer.drain()
        except (asyncio.IncompleteReadError,ConnectionError,asyncio.CancelledError):pass
        finally:
            self.writers.discard(writer);writer.close()
            try:await writer.wait_closed()
            except ConnectionError:pass
    async def asyncTearDown(self):
        await pooledhttp.close()
        self.server.close();await self.server.wait_closed()
        for writer in list(self.writers):writer.close()
        await asyncio.sleep(0.06)
    async def test_real_keepalive_across_lease_clients(self):
        for _ in range(100):
            async with pooledhttp.client(2,None,'fixture-tcp') as client:
                response=await client.get(self.url+'/ok')
                self.assertEqual(response.content,b'ok')
        self.assertEqual(self.requests,100);self.assertEqual(self.connections,1)
        self.assertTrue(all(v.references==0 for v in pooledhttp.entries.values()))
    async def test_bounded_load_cancel_and_file_descriptors(self):
        fd=Path('/proc/self/fd')
        before=len(list(fd.iterdir())) if fd.is_dir() else None
        async def request(index,slow=False):
            async with pooledhttp.client(2,None,'fixture-load') as client:
                response=await client.get(self.url+('/slow' if slow else '/ok'))
                self.assertEqual(response.content,b'ok')
        samples=[]
        for batch in range(100):
            await asyncio.gather(*(request(i) for i in range(8)))
            if batch%20==0 and fd.is_dir():samples.append(len(list(fd.iterdir())))
        for batch in range(20):
            tasks=[asyncio.create_task(request(i,True)) for i in range(8)]
            await asyncio.sleep(0.005)
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
        await request(1)
        self.assertTrue(all(v.references==0 for v in pooledhttp.entries.values()))
        if samples:self.assertLessEqual(max(samples)-min(samples),4)
        await pooledhttp.close();await asyncio.sleep(0.1)
        after=len(list(fd.iterdir())) if fd.is_dir() else None
        if before is not None:self.assertLessEqual(after,before+2)
        print(json.dumps({'tcp_requests':self.requests,'connections':self.connections,'fd_samples':samples,'fd_before':before,'fd_after':after}))
