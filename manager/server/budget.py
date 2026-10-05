"""Durable estimated budget reservations; unknown usage remains held for reconciliation.

Reservations protect concurrency and crash recovery. Provider tokenization/pricing is
not controlled here, so estimated budgets are not a hard provider billing guarantee.
"""
from __future__ import annotations
import json
import math
import time
from . import db, observability, budgetowner, ledger

SCHEMA = '''
CREATE TABLE IF NOT EXISTS budget_reservations (
 request_id TEXT PRIMARY KEY, key_id INTEGER NOT NULL, created_at INTEGER NOT NULL,
 state TEXT NOT NULL, reserved_tokens INTEGER NOT NULL DEFAULT 0,
 reserved_credit REAL NOT NULL DEFAULT 0, charged_tokens INTEGER NOT NULL DEFAULT 0,
 charged_credit REAL NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,
 resolution TEXT
);
CREATE INDEX IF NOT EXISTS idx_budget_key_state ON budget_reservations(key_id,state);
CREATE TABLE IF NOT EXISTS budget_adjustments (
 request_id TEXT PRIMARY KEY, key_id INTEGER NOT NULL, ts INTEGER NOT NULL,
 token_delta INTEGER NOT NULL, credit_delta REAL NOT NULL, actor TEXT NOT NULL,
 reason TEXT NOT NULL
);
'''

class BudgetRejected(Exception):
    def __init__(self, message, code='budget_unavailable'):
        super().__init__(message)
        self.code = code

def initialize():
    # Schema initialization belongs to startup, outside an existing transaction.
    for statement in SCHEMA.split(';'):
        if statement.strip(): db.execute(statement)
    with db.transaction() as conn:
        columns = {r[1] for r in conn.execute('PRAGMA table_info(budget_reservations)')}
        for name, declaration in (('owner', 'TEXT'), ('fence', 'INTEGER NOT NULL DEFAULT 0'),
                                  ('lease_until', 'INTEGER NOT NULL DEFAULT 0')):
            if name not in columns:
                conn.execute(f'ALTER TABLE budget_reservations ADD COLUMN {name} {declaration}')

def recover():
    """A startup must not reclaim another running worker's reservations."""
    initialize()
    budgetowner.identity()
    with db.transaction():
        for row in db.query("SELECT request_id,state,owner,fence FROM budget_reservations WHERE state IN ('reserved','forwarded')"):
            live = budgetowner.alive(row['owner'])
            if live is True:
                continue
            # Only a demonstrably dead local owner with an unsent reservation can
            # be released. Missing/legacy/remote identity remains uncertain.
            target = 'released' if live is False and row['state'] == 'reserved' else 'unknown'
            db.execute('UPDATE budget_reservations SET state=?,fence=fence+1,updated_at=? WHERE request_id=? AND fence=?',
                       (target, int(time.time()), row['request_id'], row['fence']))

def heartbeat():
    now = int(time.time())
    db.execute("UPDATE budget_reservations SET lease_until=? WHERE owner=? AND state IN ('reserved','forwarded')",
               (now + 60, budgetowner.identity()))

def _owned(row, state):
    return row['owner'] == budgetowner.identity() and row['fence'] == getattr(state, 'budget_fence', None)

def prepare(key, body):
    state = observability.current.get()
    if state is None: return
    state.key_id = int(key['id'])
    # Unlimited keys also get a durable forwarding marker, allowing a killed
    # process or failed settlement to expose uncertain spending after recovery.
    limited = bool(key.get('quota') or key.get('quota_credit'))
    initialize()
    rid = state.request_id
    with db.transaction():
        fresh = db.query_one('SELECT quota,used_tokens,quota_credit,used_credit,used_credit_units FROM api_keys WHERE id=?', (key['id'],))
        if fresh is None: raise BudgetRejected('密钥已被删除', 'invalid_api_key')
        states = "('reserved','forwarded','unknown')" if limited else "('reserved','forwarded')"
        pending = db.query_one('SELECT count(*) n FROM budget_reservations WHERE key_id=? AND state IN '+states, (key['id'],))
        if pending['n'] >= (1 if limited else 4):
            raise BudgetRejected('该密钥存在进行中或用量待核对的请求，请完成后再试', 'budget_pending')
        q, used = int(fresh['quota']), int(fresh['used_tokens'])
        qc = ledger.units(fresh['quota_credit'])
        uc = fresh['used_credit_units'] if fresh['used_credit_units'] is not None else ledger.units(fresh['used_credit'])
        # Conservative byte estimate, with framing headroom; images use extra room.
        encoded = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        estimated_input = len(encoded) + 2048
        if b'"image_url"' in encoded: estimated_input += 32768
        output_field = 'max_completion_tokens' if 'max_completion_tokens' in body else 'max_tokens'
        requested = body.get(output_field)
        if requested is not None and (isinstance(requested,bool) or not isinstance(requested,int) or requested <= 0):
            raise BudgetRejected('max_tokens 必须为正整数', 'invalid_max_tokens')
        output = requested or 4096
        if q:
            remaining = q - used
            if remaining <= estimated_input:
                raise BudgetRejected('剩余 token 预算不足以覆盖当前输入估算', 'insufficient_quota')
            output = min(output, remaining - estimated_input)
            body[output_field] = output
            tokens = estimated_input + output
        else:
            tokens = 0
        credit = max(0, qc - uc) / ledger.SCALE if qc else 0.0
        if qc and credit <= 0:
            raise BudgetRejected('积分预算已用尽', 'credit_quota_exhausted')
        now = int(time.time())
        db.execute('INSERT INTO budget_reservations(request_id,key_id,created_at,state,reserved_tokens,reserved_credit,updated_at,owner,fence,lease_until) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (rid,key['id'],now,'reserved',tokens,credit,now,budgetowner.identity(),1,now+60))
    state.budget_reserved = True
    state.budget_fence = 1

def forwarded():
    state = observability.current.get()
    if state and state.budget_reserved:
        with db.transaction() as conn:
            changed = conn.execute("UPDATE budget_reservations SET state='forwarded',updated_at=? WHERE request_id=? AND state='reserved' AND owner=? AND fence=?", (int(time.time()),state.request_id,budgetowner.identity(),state.budget_fence)).rowcount
            if changed != 1:
                raise BudgetRejected('预算预留归属已变化，禁止转发', 'budget_fenced')

def settle(usage, pt, ct, credit):
    """Runs in the same transaction as request log and key usage updates."""
    state = observability.current.get()
    if state is None or not state.budget_reserved: return
    row = db.query_one('SELECT state,owner,fence FROM budget_reservations WHERE request_id=?', (state.request_id,))
    if row is None or row['state'] in ('settled','released'): return
    if not _owned(row, state):
        raise BudgetRejected('预算预留已被恢复进程隔离', 'budget_fenced')
    fresh = db.query_one('SELECT quota_credit FROM api_keys WHERE id=?', (state.key_id,))
    known = state.terminal_state == 'completed' and isinstance(usage,dict) and all(k in usage for k in ('prompt_tokens','completion_tokens'))
    if fresh and fresh['quota_credit'] and credit is None: known = False
    next_state = 'settled' if known else 'released' if row['state']=='reserved' else 'unknown'
    db.execute('UPDATE budget_reservations SET state=?,charged_tokens=?,charged_credit=?,updated_at=? WHERE request_id=?',
               (next_state,pt+ct,credit or 0,int(time.time()),state.request_id))

def finish(state):
    if not state.budget_reserved: return
    db.execute("UPDATE budget_reservations SET state=CASE WHEN state='reserved' THEN 'released' ELSE 'unknown' END,updated_at=? "
               "WHERE request_id=? AND owner=? AND fence=? AND state IN ('reserved','forwarded')", (int(time.time()),state.request_id,budgetowner.identity(),state.budget_fence))

def unresolved(key_id=None):
    args = () if key_id is None else (key_id,)
    clause = '' if key_id is None else ' AND key_id=?'
    return [dict(r) for r in db.query("SELECT * FROM budget_reservations WHERE state IN ('reserved','forwarded','unknown')" + clause + ' ORDER BY created_at LIMIT 1000', args)]

def resolve(request_id, tokens, credit, actor, reason):
    if tokens < 0 or credit < 0 or not math.isfinite(credit) or not reason.strip():
        raise ValueError('需要非负实际用量和核对原因')
    with db.transaction():
        row = db.query_one('SELECT * FROM budget_reservations WHERE request_id=?', (request_id,))
        if row is None: raise ValueError('预留不存在')
        if row['state']=='settled': return False
        if row['state']!='unknown': raise ValueError('仅允许核对 unknown 请求，进行中请求不可手动释放')
        td = tokens-row['charged_tokens']
        cd_units = ledger.units(credit) - ledger.units(row['charged_credit'])
        cd = cd_units / ledger.SCALE
        db.execute('UPDATE api_keys SET used_tokens=max(0,used_tokens+?),used_credit_units=max(0,coalesce(used_credit_units,round(used_credit*1000000))+?) WHERE id=?', (td,cd_units,row['key_id']))
        db.execute('UPDATE api_keys SET used_credit=used_credit_units*1.0/? WHERE id=?', (ledger.SCALE,row['key_id']))
        db.execute('INSERT INTO budget_adjustments VALUES(?,?,?,?,?,?,?)', (request_id,row['key_id'],int(time.time()),td,cd,actor[:128],reason[:500]))
        ledger.adjust(request_id, row['key_id'], td, cd, actor, reason)
        db.execute("UPDATE budget_reservations SET state='settled',charged_tokens=?,charged_credit=?,resolution=?,updated_at=? WHERE request_id=?", (tokens,credit,reason[:500],int(time.time()),request_id))
        db.add_audit_log(actor,'budget.resolve',request_id,reason[:500])
    return True
