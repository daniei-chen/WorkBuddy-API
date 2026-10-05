import asyncio
import sqlite3
import time
import unittest
from unittest import mock
from server import budget,budgetowner,config,db,ledger,observability,keysvc
from server.bodyguard import APIBodyGuard
from server.tests import test_full_upgrade
from server.upstreamschema import validate

class LedgerContracts(test_full_upgrade.DurableBudgetTests):
    def test_events_survive_log_cleanup_and_reject_mutation(self):
        with db.transaction():
            ledger.record('test',1,'fixture','cn',10,2,0.01,True,'completed','fixture')
            ledger.record('test',1,'fixture','cn',10,2,0.01,True,'completed','fixture')
        self.assertEqual(len(db.query('SELECT * FROM usage_events')),1)
        self.assertEqual(db.query_one('SELECT credit_units FROM usage_events')[0],10000)
        db.execute('DELETE FROM request_logs')
        for sql in ('DELETE FROM usage_events','UPDATE usage_events SET credit_units=0'):
            with self.assertRaises(sqlite3.IntegrityError):
                with db.transaction():db.execute(sql)
        self.assertEqual(len(db.query('SELECT * FROM usage_events')),1)
    def test_integer_credit_totals(self):
        with db.transaction():
            for _ in range(100):
                keysvc.touch(self.key,'fixture',1,0.01)
                db.bump_usage(1,'fixture',1,0,0.01)
        self.assertEqual(db.query_one('SELECT used_credit_units FROM api_keys WHERE id=1')[0],1000000)
        self.assertEqual(db.query_one('SELECT credit,credit_units FROM usage_daily')[0],1.0)
        self.assertEqual(db.query_one('SELECT credit,credit_units FROM usage_daily')[1],1000000)
    def test_event_and_summary_rollback_together(self):
        with self.assertRaises(RuntimeError):
            with db.transaction():
                ledger.record('rollback',1,'fixture','cn',1,1,0.01,True,'completed','fixture')
                keysvc.touch(self.key,'fixture',2,0.01)
                raise RuntimeError('simulated I/O failure')
        self.assertEqual(db.query_one('SELECT count(*) FROM usage_events')[0],0)
        self.assertEqual(db.query_one('SELECT used_tokens FROM api_keys WHERE id=1')[0],0)
    def test_audit_chain_survives_ui_clear(self):
        db.add_audit_log('fixture','change','fixture')
        db.clear_audit_logs()
        self.assertTrue(ledger.verify_chain())
        self.assertEqual(db.query_one('SELECT count(*) FROM admin_events')[0],1)
        with self.assertRaises(sqlite3.IntegrityError):
            with db.transaction():db.execute('DELETE FROM admin_events')
    def test_expired_lease_does_not_release_live_owner(self):
        budget.prepare(self.key,{'messages':[]})
        db.execute('UPDATE budget_reservations SET lease_until=0')
        budget.recover()
        self.assertEqual(budget.unresolved()[0]['state'],'reserved')
    def test_recovered_fence_cannot_forward(self):
        budget.prepare(self.key,{'messages':[]});budgetowner.close();budget.recover()
        with self.assertRaises(budget.BudgetRejected):budget.forwarded()
    def test_legacy_owner_is_frozen(self):
        db.execute("INSERT INTO budget_reservations(request_id,key_id,created_at,state,updated_at) VALUES('legacy',1,1,'reserved',1)")
        budget.recover()
        self.assertEqual(budget.unresolved()[0]['state'],'unknown')
    def test_money_invalid_and_half_even(self):
        self.assertIsNone(ledger.units(None))
        self.assertEqual(ledger.units('0.0000005'),0)
        self.assertEqual(ledger.units('0.0000015'),2)
        for value in (True,float('nan'),float('inf'),'x'):
            with self.assertRaises(ValueError):ledger.units(value)
    def test_log_repair_cannot_replace_new_financial_events(self):
        with db.transaction():
            ledger.record('guard',1,'fixture','cn',1,1,0.01,True,'completed','fixture')
        for operation in (db.backfill_usage_from_logs,db.rebuild_usage_from_logs):
            with self.assertRaises(ValueError):operation()
        self.assertEqual(db.query_one('SELECT count(*) FROM usage_events')[0],1)
    def test_exact_attempt_trace_is_idempotent_and_immutable(self):
        db.add_request_log(ts=1,key_id=1,ip='fixture',model='fixture',mapped_model='fixture',status=200,prompt_tokens=1,completion_tokens=1,latency_ms=1,stream=0,request_id='exact-fixture')
        event={'event':'workbuddy_attempt','request_id':'exact-fixture','attempt':2,
               'account_uid':'synthetic-account','resolved_model':'fixture','realm':'cn',
               'phase':'stream_terminal','status':200,'config_hash':'a'*64,'secret':'discarded'}
        self.assertTrue(ledger.attempt(event));self.assertTrue(ledger.attempt(event))
        self.assertEqual(db.query_one('SELECT count(*) FROM attempt_events')[0],1)
        row=db.query_one('SELECT account,account_source,account_attempt FROM request_logs WHERE request_id=?',('exact-fixture',))
        self.assertEqual(tuple(row),('synthetic-account','request_id',2))
        self.assertFalse(ledger.attempt(dict(event,request_id='')))
        self.assertFalse(ledger.attempt(dict(event,attempt=True)))
        with self.assertRaises(sqlite3.IntegrityError):
            with db.transaction():db.execute('DELETE FROM attempt_events')

class BodyContracts(unittest.IsolatedAsyncioTestCase):
    async def exercise(self,chunks,headers=(),delay=0,timeout=0.2):
        self.called=False;self.body=b''
        async def app(scope,receive,send):
            self.called=True
            self.body=(await receive())['body']
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'ok'})
        pending=list(chunks)
        async def receive():
            if delay:await asyncio.sleep(delay)
            if not pending:return {'type':'http.disconnect'}
            value=pending.pop(0)
            return {'type':'http.request','body':value,'more_body':bool(pending)}
        out=[]
        async def send(msg):out.append(msg)
        await APIBodyGuard(app,maximum=10,timeout=timeout)({'type':'http','path':'/api/login','headers':headers},receive,send)
        return out[0]['status'] if out else None
    async def test_chunked_overflow(self):
        self.assertEqual(await self.exercise([b'12345',b'123456']),413);self.assertFalse(self.called)
    async def test_deceptive_length(self):
        self.assertEqual(await self.exercise([b'12345'],[(b'content-length',b'1')]),400)
    async def test_valid_chunked(self):
        self.assertEqual(await self.exercise([b'12345',b'12345']),200);self.assertEqual(self.body,b'1234512345')
    async def test_invalid_lengths(self):
        for value in (b'-1',b'x'):
            self.assertEqual(await self.exercise([b'x'],[(b'content-length',value)]),400)
        self.assertEqual(await self.exercise([b'x'],[(b'content-length',b'1'),(b'content-length',b'1')]),400)
    async def test_timeout(self):
        self.assertEqual(await self.exercise([b'x'],delay=0.05,timeout=0.01),408)

class SchemaContracts(unittest.TestCase):
    def test_reject_unknown_section_field_type(self):
        for value in ({'api_key':'x'},{'server':{'api_key':'x'}},{'pool':{'max_inflite':4}}, {'session_sticky':{'enabled':'false'}},{'pool':{'max_in_flight':True}}):
            with self.assertRaises(ValueError):validate(value)
    def test_preserve_custom_contract(self):
        validate({'pool':{'max_in_flight':6,'idle_weight_per_hour':0.5,'expiring_soon':'168h'},'session_sticky':{'enabled':True,'ttl':'24h'},'prompt':{'mode':'passthrough'}})
