import asyncio
import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from server import budget,config,db,observability,pooledhttp,budgetowner

def _reserve_from_process(database,request_id,gate,results,key_id):
    gate.wait()
    if db._conn:
        try:db._conn.close()
        except Exception:pass
    db._conn=None;config.DB_PATH=Path(database);db.connect()
    state=observability.RequestAudit(request_id,'/v1/chat/completions','POST','openai',time.monotonic())
    token=observability.current.set(state)
    try:
        budget.prepare(dict(db.query_one('SELECT * FROM api_keys WHERE id=?',(key_id,))),{'messages':[]})
        results.put('reserved')
    except budget.BudgetRejected:results.put('rejected')
    finally:observability.current.reset(token)

class ManagedReleaseGuardTests(unittest.TestCase):
    def test_managed_marker_blocks_all_web_update_targets(self):
        from server.services import updater
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'deploy').mkdir()
            (root / 'deploy' / 'managed-release.json').write_text('{}')
            with mock.patch.object(config, 'ROOT', root), mock.patch.object(updater.subprocess, 'Popen') as spawn:
                for target in ('manager', 'upstream', 'both'):
                    ok, message = updater.start_update(target)
                    self.assertFalse(ok)
                    self.assertIn('受控维护版本', message)
                spawn.assert_not_called()

class DurableBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old=config.DB_PATH
        if db._conn: db._conn.close()
        db._conn=None;config.DB_PATH=Path(self.temp.name)/'ledger.db'
        db.connect();budget.initialize()
        db.execute("INSERT INTO api_keys(id,name,key_hash,prefix,quota,quota_credit,created_at) VALUES(1,'fixture','hash','fixture',30000,10,?)",(int(time.time()),))
        self.key=dict(db.query_one('SELECT * FROM api_keys WHERE id=1'))
        self.state=observability.RequestAudit('rid1','/v1/chat/completions','POST','openai',time.monotonic())
        self.token=observability.current.set(self.state)
    def tearDown(self):
        budgetowner.close()
        observability.current.reset(self.token)
        if db._conn: db._conn.close()
        db._conn=None;config.DB_PATH=self.old;self.temp.cleanup()
    def test_reserve_and_unknown_hold(self):
        body={'messages':[{'role':'user','content':'fixture'}]}
        budget.prepare(self.key,body);budget.forwarded();budget.finish(self.state)
        self.assertEqual(budget.unresolved()[0]['state'],'unknown')
        self.state.request_id='rid2';self.state.budget_reserved=False
        with self.assertRaises(budget.BudgetRejected):budget.prepare(self.key,body)
    def test_crash_recovery_does_not_release_forwarded(self):
        budget.prepare(self.key,{'messages':[]});budget.forwarded();budgetowner.close();budget.recover()
        self.assertEqual(budget.unresolved()[0]['state'],'unknown')
    def test_unforwarded_crash_releases(self):
        budget.prepare(self.key,{'messages':[]});budgetowner.close();budget.recover()
        self.assertEqual(budget.unresolved(),[])
    def test_complete_settlement(self):
        budget.prepare(self.key,{'messages':[]});budget.forwarded()
        self.state.terminal_state='completed'
        with db.transaction():budget.settle({'prompt_tokens':12,'completion_tokens':4},12,4,0.5)
        self.assertEqual(budget.unresolved(),[])
    def test_partial_usage_does_not_unlock_budget(self):
        budget.prepare(self.key,{'messages':[]});budget.forwarded()
        self.state.terminal_state='protocol_incomplete'
        budget.settle({'prompt_tokens':12,'completion_tokens':4},12,4,0.5)
        self.assertEqual(budget.unresolved()[0]['state'],'unknown')
    def test_manual_resolution_is_idempotent_and_audited(self):
        budget.prepare(self.key,{'messages':[]});budget.forwarded();budget.finish(self.state)
        self.assertTrue(budget.resolve('rid1',16,0.5,'admin','verified fixture usage'))
        self.assertFalse(budget.resolve('rid1',16,0.5,'admin','retry'))
        key=db.query_one('SELECT used_tokens,used_credit FROM api_keys WHERE id=1')
        self.assertEqual(key['used_tokens'],16);self.assertEqual(key['used_credit'],0.5)
        self.assertEqual(len(db.query('SELECT * FROM audit_logs')),1)
        self.assertEqual(len(db.query('SELECT * FROM adjustment_events')),1)
    def test_startup_leaves_live_owner_untouched(self):
        budget.prepare(self.key,{'messages':[]});budget.forwarded();budget.recover()
        self.assertEqual(budget.unresolved()[0]['state'],'forwarded')
    def test_output_clamped_before_forward(self):
        body={'messages':[],'max_tokens':100000}
        budget.prepare(self.key,body)
        self.assertLess(body['max_tokens'],30000)
    def test_estimated_input_rejected(self):
        with self.assertRaises(budget.BudgetRejected):budget.prepare(self.key,{'messages':['x'*30000]})
    def test_transaction_rollback_keeps_hold(self):
        budget.prepare(self.key,{'messages':[]});budget.forwarded();self.state.terminal_state='completed'
        with self.assertRaises(RuntimeError):
            with db.transaction():
                budget.settle({'prompt_tokens':1,'completion_tokens':1},1,1,0.1)
                raise RuntimeError('fixture commit failure')
        self.assertEqual(budget.unresolved()[0]['state'],'forwarded')
    def test_two_processes_cannot_reserve_the_same_key(self):
        context=multiprocessing.get_context('spawn')
        gate=context.Event();results=context.Queue()
        workers=[context.Process(target=_reserve_from_process,args=(str(config.DB_PATH),f'parallel-{i}',gate,results,1)) for i in range(2)]
        for worker in workers:worker.start()
        gate.set()
        outcomes=[results.get(timeout=8),results.get(timeout=8)]
        for worker in workers:
            worker.join(8)
            if worker.is_alive():worker.terminate();worker.join();self.fail('reservation worker hung')
            self.assertEqual(worker.exitcode,0)
        self.assertEqual(sorted(outcomes),['rejected','reserved'])
        self.assertEqual(len(budget.unresolved()),1)

class PoolIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):await pooledhttp.close()
    async def test_same_identity_reuses_transport_with_isolated_cookies(self):
        a=pooledhttp.client(10,None,'tenant-a');b=pooledhttp.client(10,None,'tenant-a')
        self.assertIs(a.entry,b.entry)
        a.cookies.set('fixture','secret')
        self.assertNotIn('fixture',b.cookies)
        await a.aclose();await a.aclose();await b.aclose()
        self.assertEqual(a.entry.references,0)
    async def test_identity_and_proxy_separate(self):
        clients=[pooledhttp.client(10,None,'a'),pooledhttp.client(10,None,'b'),pooledhttp.client(10,'http://127.0.0.1:9','a')]
        self.assertEqual(len({id(c.entry) for c in clients}),3)
        for c in clients:await c.aclose()
    async def test_context_manager_releases_lease(self):
        async with pooledhttp.client(10,None,'a') as client:entry=client.entry
        self.assertEqual(entry.references,0)
    async def test_cancelled_request_releases_transport_lease(self):
        accepted=asyncio.Event()
        client=pooledhttp.client(30,None,'cancel-fixture')
        entry=client.entry
        async def hold(request):
            accepted.set()
            await asyncio.Event().wait()
        entry.transport.handle_async_request=hold
        task=asyncio.create_task(client.get('https://example.invalid/'))
        await asyncio.wait_for(accepted.wait(),2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        await client.aclose()
        self.assertEqual(entry.references,0)
    async def test_idle_pool_eviction_remains_bounded(self):
        previous=pooledhttp.MAX_POOLS;pooledhttp.MAX_POOLS=2
        try:
            first=pooledhttp.client(10,None,'bound-a')
            second=pooledhttp.client(10,None,'bound-b')
            await second.aclose()
            third=pooledhttp.client(10,None,'bound-c')
            self.assertLessEqual(len(pooledhttp.entries),2)
            self.assertIn(first.entry,pooledhttp.entries.values())
            await first.aclose();await third.aclose()
        finally:pooledhttp.MAX_POOLS=previous
