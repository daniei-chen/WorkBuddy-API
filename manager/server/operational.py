"""Readiness and actionable local health state; no generation probes or secrets."""
import asyncio
import logging
import os
import time
from pathlib import Path
from . import db, config, ledger

log = logging.getLogger('workbuddy.operations')
ledger_write_healthy = True
alerts = {}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS readiness_probe(id INTEGER PRIMARY KEY CHECK(id=1), ts INTEGER NOT NULL);
'''

def database_writable():
    with db.transaction() as conn:
        conn.execute('INSERT INTO readiness_probe VALUES(1,?) ON CONFLICT(id) DO UPDATE SET ts=excluded.ts',(int(time.time()),))
    return True

def readiness():
    try:
        return {'ok': database_writable() and ledger_write_healthy,
                'database_write': True, 'ledger_write': ledger_write_healthy}
    except Exception:
        return {'ok': False, 'database_write': False, 'ledger_write': ledger_write_healthy}

def transition(name, active, detail):
    old = alerts.get(name)
    if active:
        if old is None:
            alerts[name] = {'name':name,'since':int(time.time()),'detail':detail}
            log.error('operational alert name=%s detail=%s',name,detail)
    elif old is not None:
        alerts.pop(name)
        log.info('operational recovery name=%s',name)

async def monitor():
    failures = 0
    while True:
        status = readiness()
        failures = 0 if status['ok'] else failures+1
        transition('readiness',failures>=2,'database/ledger readiness failed twice')
        now=int(time.time())
        try:
            unknown=db.query_one("SELECT count(*) n FROM budget_reservations WHERE state='unknown' AND updated_at<?",(now-3600,))
            transition('budget_unknown',bool(unknown['n']),'unknown budget older than 1 hour')
            row=db.query_one("SELECT count(*) n,sum(CASE WHEN terminal_state IN ('upstream_error','transport_error','protocol_incomplete','application_error','interrupted') THEN 1 ELSE 0 END) failed FROM request_outcomes WHERE protocol!='http' AND ts>=? AND terminal_state!='rejected'",(now-300,))
            transition('model_completion',row['n']>=20 and (row['failed'] or 0)/row['n']>0.01,'model terminal failures exceed 1% / 5 minutes / >=20 requests')
            free=os.statvfs(config.DATA_DIR).f_bavail*os.statvfs(config.DATA_DIR).f_frsize if hasattr(os,'statvfs') else None
            transition('disk_free',free is not None and free<2*1024**3,'application volume free space below 2 GiB')
            # The installed host job is daily. Keep that honest: use a 26-hour
            # freshness guard until operations can install a more frequent job.
            latest=Path('/root/backups/workbuddy-daily/latest.json')
            if latest.is_file():
                import json
                data=json.loads(latest.read_text())
                transition('backup_stale',now-latest.stat().st_mtime>26*3600,'verified backup older than 26 hours')
                transition('backup_publish',not bool(data.get('offsite')),'encrypted offsite publication failed')
                transition('backup_size',data.get('bytes',0)>45*1024**2,'backup approaching 50 MiB ciphertext limit')
        except Exception:
            log.exception('operational health evaluation failed')
        await asyncio.sleep(30)

def status():
    return {'readiness':readiness(),'alerts':list(alerts.values()),
            'notification_delivery':'local journal and authenticated API only',
            'backup_freshness_hours':26,'credit_scale':ledger.SCALE,
            'financial_events_since_release': 'v1.0.79+closure.20261005.1'}
