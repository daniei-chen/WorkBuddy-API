"""Durable events are authoritative; legacy REAL summaries remain UI projections.

Amounts use millionths of a provider credit, rounded half-even. Unknown is NULL.
No prompt, credential, URL or provider error text is retained in these events.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
import time

SCALE = 1_000_000
SCHEMA = '''
CREATE TABLE IF NOT EXISTS usage_events (
 event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, attempt INTEGER NOT NULL,
 event_type TEXT NOT NULL, ts INTEGER NOT NULL, key_id INTEGER, model TEXT,
 realm TEXT, prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL,
 credit_units INTEGER, counted INTEGER NOT NULL, terminal_state TEXT,
 provenance TEXT NOT NULL, release_id TEXT,
 UNIQUE(request_id,attempt,event_type)
);
CREATE INDEX IF NOT EXISTS idx_usage_events_key_ts ON usage_events(key_id,ts);
CREATE TABLE IF NOT EXISTS forwarding_events (
 request_id TEXT NOT NULL, attempt INTEGER NOT NULL, phase TEXT NOT NULL,
 ts INTEGER NOT NULL, upstream_id TEXT, requested_model TEXT, resolved_model TEXT,
 realm TEXT, protocol TEXT, transform TEXT, config_hash TEXT, outcome TEXT,
 PRIMARY KEY(request_id,attempt,phase)
);
CREATE TABLE IF NOT EXISTS adjustment_events (
 event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, ts INTEGER NOT NULL,
 key_id INTEGER NOT NULL, token_delta INTEGER NOT NULL, credit_units_delta INTEGER NOT NULL,
 actor TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, request_id TEXT,
 actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, outcome TEXT NOT NULL,
 status INTEGER, previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempt_events (
 request_id TEXT NOT NULL, attempt INTEGER NOT NULL, phase TEXT NOT NULL,
 ts INTEGER NOT NULL, account_uid TEXT NOT NULL, resolved_model TEXT NOT NULL,
 realm TEXT NOT NULL, status INTEGER NOT NULL, config_hash TEXT NOT NULL,
 PRIMARY KEY(request_id,attempt,phase)
);
CREATE TRIGGER IF NOT EXISTS attempt_events_no_update BEFORE UPDATE ON attempt_events
 BEGIN SELECT RAISE(ABORT,'attempt events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS attempt_events_no_delete BEFORE DELETE ON attempt_events
 BEGIN SELECT RAISE(ABORT,'attempt events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS usage_events_no_update BEFORE UPDATE ON usage_events
 BEGIN SELECT RAISE(ABORT,'usage events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS usage_events_no_delete BEFORE DELETE ON usage_events
 BEGIN SELECT RAISE(ABORT,'usage events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS adjustment_events_no_update BEFORE UPDATE ON adjustment_events
 BEGIN SELECT RAISE(ABORT,'adjustment events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS adjustment_events_no_delete BEFORE DELETE ON adjustment_events
 BEGIN SELECT RAISE(ABORT,'adjustment events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS admin_events_no_update BEFORE UPDATE ON admin_events
 BEGIN SELECT RAISE(ABORT,'admin events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS admin_events_no_delete BEFORE DELETE ON admin_events
 BEGIN SELECT RAISE(ABORT,'admin events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forwarding_events_no_update BEFORE UPDATE ON forwarding_events
 BEGIN SELECT RAISE(ABORT,'forwarding events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forwarding_events_no_delete BEFORE DELETE ON forwarding_events
 BEGIN SELECT RAISE(ABORT,'forwarding events are immutable'); END;
'''

def units(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError('invalid credit')
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or abs(amount) > Decimal('9000000000000'):
            raise ValueError('credit out of range')
        return int((amount * SCALE).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError('invalid credit') from exc

def record(request_id, key_id, model, realm, pt, ct, credit, counted, terminal, release):
    from . import db
    if not request_id:
        return
    db.execute('INSERT OR IGNORE INTO usage_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (request_id + ':1:usage', request_id, 1, 'usage', int(time.time()), key_id,
                model, realm, pt, ct, units(credit), int(counted), terminal,
                'provider-reported' if credit is not None else 'unknown-credit', release))

def attempt(value):
    """Collect explicit gateway request IDs; never infer a correlation from time."""
    import re
    from . import db
    if not isinstance(value,dict) or value.get('event')!='workbuddy_attempt':return False
    rid=value.get('request_id');number=value.get('attempt');phase=value.get('phase')
    phases={'selected','capacity_lost','refresh_failed','refresh_write_failed','upstream_headers','stream_terminal','aggregate_terminal'}
    if not isinstance(rid,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',rid):return False
    if isinstance(number,bool) or not isinstance(number,int) or not 1<=number<=32 or phase not in phases:return False
    uid=value.get('account_uid');model=value.get('resolved_model');realm=value.get('realm');status=value.get('status');config_hash=value.get('config_hash','')
    if not all(isinstance(v,str) and len(v)<=128 and not any(ord(c)<32 for c in v) for v in (uid,model)) or realm not in ('cn','global'):return False
    if isinstance(status,bool) or not isinstance(status,int) or not 0<=status<=599:return False
    if not isinstance(config_hash,str) or config_hash and not re.fullmatch(r'[a-f0-9]{64}',config_hash):return False
    with db.transaction():
        db.execute('INSERT OR IGNORE INTO attempt_events VALUES(?,?,?,?,?,?,?,?,?)',
                   (rid,number,phase,int(time.time()),uid,model,realm,status,config_hash))
        if uid:
            db.execute("UPDATE request_logs SET account=?,account_source='request_id',account_attempt=? WHERE request_id=? AND coalesce(account_attempt,0)<=?",
                       (uid,number,rid,number))
    return True

def forwarding(state, phase, outcome=None):
    from . import db
    db.execute('INSERT OR IGNORE INTO forwarding_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (state.request_id,1,phase,int(time.time()),getattr(state,'upstream_id','default'),
                getattr(state,'requested_model',None),getattr(state,'resolved_model',None),
                getattr(state,'realm',None),state.protocol,getattr(state,'transform',None),
                getattr(state,'config_hash',None),outcome))

def adjust(request_id, key_id, td, cd, actor, reason):
    from . import db
    db.execute('INSERT INTO adjustment_events VALUES(?,?,?,?,?,?,?,?)',
               (request_id + ':reconcile', request_id, int(time.time()), key_id, td,
                units(cd), str(actor)[:128], str(reason)[:500]))

def admin(actor, action, target='', outcome='completed', status=None, request_id=None):
    from . import db
    # Transaction serializes the hash chain across processes. This detects accidental
    # corruption; host administrators can rewrite SQLite and are outside this boundary.
    with db.transaction():
        previous = db.query_one('SELECT event_hash FROM admin_events ORDER BY id DESC LIMIT 1')
        prev = previous['event_hash'] if previous else '0' * 64
        fields = [int(time.time()), request_id, str(actor or 'anonymous')[:128],
                  str(action)[:128], str(target)[:256], outcome, status, prev]
        digest = hashlib.sha256(json.dumps(fields, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        db.execute('INSERT INTO admin_events(ts,request_id,actor,action,target,outcome,status,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?,?)', (*fields, digest))

def verify_chain():
    from . import db
    prev = '0' * 64
    for row in db.query('SELECT * FROM admin_events ORDER BY id'):
        fields = [row[k] for k in ('ts','request_id','actor','action','target','outcome','status','previous_hash')]
        digest = hashlib.sha256(json.dumps(fields, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        if row['previous_hash'] != prev or row['event_hash'] != digest:
            return False
        prev = digest
    return True
