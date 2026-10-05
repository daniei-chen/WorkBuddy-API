"""Request identity and bounded SSE audit. Does not retain messages or credentials."""
from __future__ import annotations
import codecs
import contextvars
import json
import logging
import time
import uuid
import anyio
import threading
import asyncio
from starlette.responses import StreamingResponse
from dataclasses import dataclass

RELEASE_ID = "v1.0.79+closure.20261005.4"
log = logging.getLogger("workbuddy.audit")

@dataclass
class RequestAudit:
    request_id: str
    endpoint: str
    method: str
    protocol: str
    started: float
    http_status: int = 0
    terminal_state: str | None = None
    error_stage: str | None = None
    account: str | None = None
    first_byte_ms: int | None = None
    recorded: bool = False
    key_slot: int | None = None
    key_id: int | None = None
    budget_reserved: bool = False

current: contextvars.ContextVar[RequestAudit | None] = contextvars.ContextVar("wb_request_audit", default=None)
_key_slots={}
_key_lock=threading.RLock()
_outcome_writes=0

class CapacityError(Exception): pass

def admit_key(key):
    state=current.get()
    if state is None or state.key_slot is not None or state.protocol=='http': return True
    key_id=int(key['id'])
    limit=1 if key.get('quota') or key.get('quota_credit') else 4
    with _key_lock:
        if _key_slots.get(key_id,0)>=limit: return False
        _key_slots[key_id]=_key_slots.get(key_id,0)+1; state.key_slot=key_id
    return True

def release_key(state):
    if state.key_slot is None: return
    with _key_lock:
        key=state.key_slot; _key_slots[key]=max(0,_key_slots.get(key,0)-1)
        if not _key_slots[key]: _key_slots.pop(key,None)
        state.key_slot=None

def usage_int(value):
    try:
        return min(2**53-1,max(0,int(value or 0)))
    except (ValueError,TypeError,OverflowError):
        return 0

def sse_error(message):
    return ('data: '+json.dumps({'error':{'type':'api_error','code':'stream_incomplete','message':message}},ensure_ascii=False)+'\n\ndata: [DONE]\n\n').encode()

class StreamAudit:
    """Incremental UTF-8/SSE parser. Bounds an unfinished event, preserves raw transport."""
    def __init__(self, maximum=1024 * 1024):
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.pending = ""
        self.data = []
        self.event_size = 0
        self.maximum = maximum
        self.usage = {}
        self.done_seen = False
        self.finish_seen = False
        self.saw_useful = False
        self.first_useful = None
        self.error = None
        self.error_stage = None
        self.error_hint = None
        self.error_code = None

    @property
    def completed(self):
        return (self.done_seen or self.finish_seen) and self.error is None

    def _event(self):
        raw = "\n".join(self.data).strip()
        self.data.clear()
        self.event_size = 0
        if not raw:
            return []
        if raw == "[DONE]":
            self.done_seen = True
            return []
        try:
            obj = json.loads(raw)
        except ValueError:
            self.error = self.error or "上游 SSE 包含无法解析的 JSON 事件"
            self.error_stage = "protocol"
            return []
        if not isinstance(obj, dict):
            return []
        if isinstance(obj.get("error"), dict):
            # Error body can contain private text; retain only a bounded provider message.
            self.error = str(obj["error"].get("message") or obj["error"].get("code") or "上游流错误")[:500]
            self.error_stage = "upstream"
            hint=obj['error'].get('gateway_hint')
            self.error_hint=hint.strip()[:300] if isinstance(hint,str) and hint.strip() else None
            code=obj['error'].get('code')
            self.error_code=str(code)[:64] if code else None
        if isinstance(obj.get("usage"), dict):
            self.usage.update(obj["usage"])
        choices = obj.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason") is not None:
                    self.finish_seen = True
                delta = choice.get("delta") or choice.get("message") or {}
                if isinstance(delta, dict) and any(delta.get(k) for k in (
                        "content", "reasoning_content", "reasoning", "tool_calls", "function_call")):
                    self.saw_useful = True
        return [obj]

    def _consume(self, text):
        self.pending += text
        out = []
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                out.extend(self._event())
            elif line.startswith("data:"):
                item = line[5:].lstrip(" ")
                self.event_size += len(item)
                self.data.append(item)
            if self.event_size > self.maximum:
                raise ValueError("上游 SSE 事件超过审计缓冲上限")
        if len(self.pending) + self.event_size > self.maximum:
            raise ValueError("上游 SSE 事件超过审计缓冲上限")
        return out

    def feed(self, chunk):
        return self._consume(self.decoder.decode(chunk))

    def finish(self):
        out = self._consume(self.decoder.decode(b"", final=True))
        if self.pending:
            out.extend(self._consume("\n"))
        if self.data:
            out.extend(self._event())
        return out

def set_stream_state(observer, error=None, cancelled=False):
    state = current.get()
    if state is None:
        return
    if cancelled:
        state.terminal_state, state.error_stage = "client_cancelled", "client"
    elif error and not observer.error:
        state.terminal_state = ('upstream_error' if observer.error_stage == 'upstream' else
                                'protocol_incomplete' if observer.error_stage in (None, 'protocol') else 'transport_error')
        state.error_stage = observer.error_stage or 'protocol'
    elif observer.error:
        state.terminal_state = ("upstream_error" if observer.error_stage == "upstream" else
                                "protocol_incomplete" if observer.error_stage=='protocol' else "transport_error")
        state.error_stage = observer.error_stage or "stream_read"
    elif observer.completed:
        state.terminal_state, state.error_stage = "completed", None
    else:
        state.terminal_state, state.error_stage = "protocol_incomplete", "protocol"

async def close_stream(response, client):
    # Starlette uses level cancellation; shield cleanup and keep it bounded.
    try:
        with anyio.move_on_after(5, shield=True):
            try:
                await response.aclose()
            finally:
                await client.aclose()
    except Exception:
        state=current.get()
        log.exception('stream close failed request_id=%s',state.request_id if state else None)

class ManagedStreamingResponse(StreamingResponse):
    """Close the iterator even if ASGI send fails while the generator is paused at yield."""
    def __init__(self,*args,cleanup=None,**kwargs):
        self.cleanup=cleanup
        super().__init__(*args,**kwargs)

    async def __call__(self,scope,receive,send):
        try:
            await super().__call__(scope,receive,send)
        except asyncio.CancelledError:
            state=current.get()
            if state and state.terminal_state is None:
                state.terminal_state,state.error_stage='client_cancelled','client'
            raise
        finally:
            try:
                with anyio.move_on_after(6,shield=True):
                    close=getattr(self.body_iterator,'aclose',None)
                    if close: await close()
            except Exception:
                log.exception('stream iterator cleanup failed')
            finally:
                if self.cleanup: await self.cleanup()

class RequestAuditMiddleware:
    """Pure ASGI wrapper; mutable context survives streaming tasks and is reset per request."""
    def __init__(self, app):
        self.app = app
        # One worker: bounded admission before body allocation, no unbounded queue.
        self.active=0; self.large=0; self.maximum=8; self.large_maximum=2

    async def __call__(self, scope, receive, send):
        global _outcome_writes
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        from . import config
        if config.BASE_PATH and (path == config.BASE_PATH or path.startswith(config.BASE_PATH + '/')):
            path = path[len(config.BASE_PATH):] or '/'
        protocol = ("anthropic" if "/messages" in path else
                    "responses" if path.endswith("/responses") else
                    "openai" if "/chat/completions" in path else "http")
        state = RequestAudit(uuid.uuid4().hex, path[:256], scope.get("method", ""), protocol, time.monotonic())
        token = current.set(state)
        scope.setdefault("state", {})["request_id"] = state.request_id
        tracked = path.startswith(("/v1/", "/v2/", "/api/")) or path in ("/responses", "/messages")
        inference=state.protocol!='http' and state.method=='POST'
        admitted=False; large=False; received=0
        headers=dict(scope.get('headers',[]))
        try: declared=int(headers.get(b'content-length',b'0'))
        except ValueError: declared=0
        async def receive_audited():
            nonlocal received,large
            message=await receive(); received+=len(message.get('body',b''))
            if inference and received>1024*1024 and not large:
                if self.large>=self.large_maximum: raise CapacityError('large context capacity exceeded')
                self.large+=1; large=True
            return message
        async def send_audited(message):
            if message["type"] == "http.response.start":
                state.http_status = message["status"]
                message = dict(message)
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in (b"x-request-id", b"x-workbuddy-release")]
                headers.extend([(b"x-request-id", state.request_id.encode()),
                                (b"x-workbuddy-release", RELEASE_ID.encode())])
                message["headers"] = headers
            await send(message)
        try:
            if inference and (self.active>=self.maximum or declared>1024*1024 and self.large>=self.large_maximum):
                state.terminal_state,state.error_stage='rejected','admission'
                body=json.dumps({'error':{'message':'网关处理容量已满，请稍后重试','type':'server_error','code':'gateway_capacity_exceeded'}},ensure_ascii=False).encode()
                await send_audited({'type':'http.response.start','status':503,'headers':[(b'content-type',b'application/json'),(b'retry-after',b'1')]})
                await send_audited({'type':'http.response.body','body':body})
            else:
                if inference:
                    self.active+=1; admitted=True
                    if declared>1024*1024: self.large+=1; large=True
                await self.app(scope, receive_audited, send_audited)
        except BaseException:
            if state.terminal_state is None:
                state.terminal_state, state.error_stage = "interrupted", "application"
            raise
        finally:
            try:
                if tracked:
                    from . import db
                    status = state.http_status or 500
                    terminal = state.terminal_state or ("rejected" if 400 <= status < 500 else
                               "application_error" if status >= 500 else "completed")
                    db.execute(
                        "INSERT OR IGNORE INTO request_outcomes(request_id, ts, endpoint, method, protocol, "
                        "http_status, terminal_state, error_stage, latency_ms, release_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (state.request_id, int(time.time()), state.endpoint, state.method, state.protocol,
                         status, terminal, state.error_stage, int((time.monotonic()-state.started)*1000), RELEASE_ID))
                    if path.startswith('/api/') and (state.method not in ('GET','HEAD','OPTIONS') or status >= 400):
                        from . import ledger
                        route = scope.get('route')
                        action = state.method + ' ' + (getattr(route, 'path', '/api/unmatched'))
                        ledger.admin(scope.get('state', {}).get('audit_actor'), action,
                                     outcome='denied' if status in (401,403) else 'failed' if status >= 400 else 'completed',
                                     status=status, request_id=state.request_id)
                    _outcome_writes+=1
                    if _outcome_writes>=2000:
                        _outcome_writes=0
                        db.execute('DELETE FROM request_outcomes WHERE request_id IN '
                                   '(SELECT request_id FROM request_outcomes WHERE ts<? ORDER BY ts LIMIT 2000)',
                                   (int(time.time())-90*86400,))
            except Exception:
                log.exception("request outcome persistence failed request_id=%s", state.request_id)
            finally:
                from . import budget
                try:
                    budget.finish(state)
                except Exception:
                    log.exception("budget finalization failed request_id=%s",state.request_id)
                release_key(state)
                if admitted: self.active-=1
                if large: self.large-=1
                current.reset(token)

