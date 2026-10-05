"""Offline protocol and transactional ledger regressions for the managed release."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from server import observability as obs


class StreamAuditTest(unittest.TestCase):
    def test_every_utf8_boundary(self):
        raw=('data: '+json.dumps({'choices':[{'delta':{'content':'中文🙂'},'finish_reason':None}]},ensure_ascii=False)+
             '\n\ndata: {"choices":[{"finish_reason":"stop","delta":{}}]}\n\ndata: [DONE]\n\n').encode()
        for cut in range(len(raw)+1):
            audit=obs.StreamAudit()
            objects=audit.feed(raw[:cut])+audit.feed(raw[cut:])+audit.finish()
            self.assertEqual(objects[0]['choices'][0]['delta']['content'],'中文🙂')
            self.assertTrue(audit.completed)
            self.assertTrue(audit.saw_useful)

    def test_multiline_and_trailing_event(self):
        audit=obs.StreamAudit()
        objects=audit.feed(b'data: {"choices":\r\ndata: [{"delta":{"tool_calls":[{"index":0}]},"finish_reason":"tool_calls"}]}')
        self.assertFalse(objects)
        objects=audit.finish()
        self.assertTrue(audit.completed)
        self.assertTrue(audit.saw_useful)
        self.assertEqual(len(objects),1)

    def test_error_and_early_eof(self):
        audit=obs.StreamAudit()
        audit.feed(b'data: {"error":{"message":"failed"}}\n\ndata: [DONE]\n\n')
        audit.finish()
        self.assertFalse(audit.completed)
        state=obs.RequestAudit('id','/v1/messages','POST','anthropic',time.monotonic())
        token=obs.current.set(state)
        try:
            obs.set_stream_state(audit,audit.error)
            self.assertEqual(state.terminal_state,'upstream_error')
            obs.set_stream_state(obs.StreamAudit())
            self.assertEqual(state.terminal_state,'protocol_incomplete')
            audit=obs.StreamAudit(); audit.error_stage='upstream'
            obs.set_stream_state(audit,'HTTP 503')
            self.assertEqual(state.terminal_state,'upstream_error')
            obs.set_stream_state(audit,'HTTP 503',True)
            self.assertEqual(state.terminal_state,'client_cancelled')
        finally:
            obs.current.reset(token)

    def test_buffer_and_invalid_utf8(self):
        with self.assertRaises(ValueError):
            obs.StreamAudit(20).feed(b'data: '+b'x'*30)
        with self.assertRaises(UnicodeDecodeError):
            obs.StreamAudit().feed(b'\xff')

    def test_error_frame_has_real_sse_delimiters(self):
        raw=obs.sse_error('early EOF')
        self.assertIn(b'\n\ndata: [DONE]\n\n',raw)
        audit=obs.StreamAudit(); audit.feed(raw); audit.finish()
        self.assertEqual(audit.error,'early EOF'); self.assertFalse(audit.completed)

    def test_unicode_validation(self):
        from server.routers.gateway import _invalid_unicode_path
        self.assertIsNone(_invalid_unicode_path({'messages':[{'content':'中文🙂'}]}))
        self.assertIsNotNone(_invalid_unicode_path({'messages':[{'content':'\ud800'}]}))
        self.assertIsNotNone(_invalid_unicode_path({'\udfff':'value'}))
        nested='\ud800'
        for _ in range(1200): nested=[nested]
        self.assertIsNotNone(_invalid_unicode_path(nested))

    def test_cleanup_closes_both(self):
        class Stream:
            closed=False
            async def aclose(self): self.closed=True; raise ValueError('mock close')
        class Client:
            closed=False
            async def aclose(self): self.closed=True
        response,client=Stream(),Client()
        asyncio.run(obs.close_stream(response,client))
        self.assertTrue(response.closed and client.closed)


class TransactionTest(unittest.TestCase):
    def setUp(self):
        from server import config,db
        self.db=db; self.config=config
        self.tmp=tempfile.TemporaryDirectory()
        self.original=(config.DB_PATH,db._conn)
        config.DB_PATH=Path(self.tmp.name)/'ledger.db'; db._conn=None
        db.connect()

    def tearDown(self):
        if self.db._conn: self.db._conn.close()
        self.config.DB_PATH,self.db._conn=self.original
        self.tmp.cleanup()

    def test_nested_transaction_rolls_back_all_writes(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction():
                self.db.execute("INSERT INTO request_outcomes(request_id,ts) VALUES('first',1)")
                with self.db.transaction():
                    self.db.execute("INSERT INTO request_outcomes(request_id,ts) VALUES('second',1)")
                raise RuntimeError('inject failure')
        self.assertEqual(self.db.query_one('SELECT count(*) FROM request_outcomes')[0],0)

    def test_ledger_idempotent_and_failure_rolls_back(self):
        from server import keysvc
        from server.routers import gateway
        key=keysvc.create_key(name='offline-ledger')
        state=obs.RequestAudit('ledger-1','/v1/chat/completions','POST','openai',time.monotonic(),http_status=200,terminal_state='completed')
        token=obs.current.set(state)
        try:
            for _ in range(2):
                gateway._record(key,'127.0.0.1','model','model',200,11,7,1,None,None,True,
                                usage={'prompt_tokens':11,'completion_tokens':7})
            row=self.db.query_one('SELECT prompt_tokens,completion_tokens,http_status,terminal_state FROM request_logs WHERE request_id=?',('ledger-1',))
            self.assertEqual(tuple(row),(11,7,200,'completed'))
            self.assertEqual(self.db.query_one('SELECT count(*) FROM request_logs')[0],1)
            counters=self.db.query_one('SELECT used_tokens FROM api_keys WHERE id=?',(key['id'],))
            self.assertEqual(counters[0],18)
            state.request_id='ledger-2'
            with mock.patch.object(keysvc,'touch',side_effect=RuntimeError('injected settlement failure')):
                gateway._record(key,'127.0.0.1','model','model',200,2,3,1,None,None,True,usage={})
            self.assertIsNone(self.db.query_one('SELECT id FROM request_logs WHERE request_id=?',('ledger-2',)))
            self.assertEqual(self.db.query_one('SELECT used_tokens FROM api_keys WHERE id=?',(key['id'],))[0],18)
        finally:
            obs.current.reset(token)


class MiddlewareTest(unittest.TestCase):
    def test_limited_key_slot_releases(self):
        key={'id':987654,'quota':100,'quota_credit':0}
        first=obs.RequestAudit('first','/v1/messages','POST','anthropic',time.monotonic())
        second=obs.RequestAudit('second','/v1/messages','POST','anthropic',time.monotonic())
        token=obs.current.set(first)
        try:
            self.assertTrue(obs.admit_key(key)); self.assertTrue(obs.admit_key(key))
            obs.current.set(second); self.assertFalse(obs.admit_key(key))
            obs.release_key(first); self.assertTrue(obs.admit_key(key)); obs.release_key(second)
            self.assertNotIn(key['id'],obs._key_slots)
        finally:
            obs.release_key(first); obs.release_key(second); obs.current.reset(token)

    def test_global_capacity_rejection(self):
        async def run():
            async def app(scope,receive,send): raise AssertionError('capacity rejection reached application')
            async def receive(): return {'type':'http.request','body':b''}
            sent=[]
            async def send(message): sent.append(message)
            wrapper=obs.RequestAuditMiddleware(app); wrapper.active=wrapper.maximum
            from server import db
            with mock.patch.object(db,'execute'):
                await wrapper({'type':'http','path':'/v1/messages','method':'POST'},receive,send)
            self.assertEqual(sent[0]['status'],503)
            self.assertEqual(wrapper.active,wrapper.maximum)
            self.assertIsNone(obs.current.get())
        asyncio.run(run())

    def test_request_identity_and_context_reset(self):
        async def run():
            seen=[]
            async def app(scope,receive,send):
                seen.append(obs.current.get().request_id)
                await send({'type':'http.response.start','status':200,'headers':[]})
                await send({'type':'http.response.body','body':b'ok'})
            messages=[]
            async def send(message): messages.append(message)
            async def receive(): return {'type':'http.request','body':b''}
            wrapper=obs.RequestAuditMiddleware(app)
            for _ in range(2):
                await wrapper({'type':'http','path':'/offline','method':'GET'},receive,send)
                self.assertIsNone(obs.current.get())
            self.assertNotEqual(seen[0],seen[1])
            headers=dict(messages[0]['headers'])
            self.assertEqual(headers[b'x-request-id'].decode(),seen[0])
            self.assertEqual(headers[b'x-workbuddy-release'].decode(),obs.RELEASE_ID)
        asyncio.run(run())
