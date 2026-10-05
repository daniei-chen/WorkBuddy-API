"""Actual process death and live-worker recovery, using a synthetic SQLite DB."""
import multiprocessing
import time
import unittest
from pathlib import Path
from server import budget,budgetowner,config,db,observability
from server.tests import test_full_upgrade

def hold(database,pipe,forward):
    if db._conn:db._conn.close()
    db._conn=None;config.DB_PATH=Path(database)
    key=dict(db.query_one('SELECT * FROM api_keys WHERE id=1'))
    state=observability.RequestAudit('child-fixture','/v1/chat/completions','POST','openai',time.monotonic())
    observability.current.set(state)
    budget.prepare(key,{'messages':[]})
    if forward:budget.forwarded()
    pipe.send('ready')
    pipe.recv()  # Hold the lifetime lock until the parent deliberately kills us.

class ProcessDeathContracts(test_full_upgrade.DurableBudgetTests):
    def exercise_kill(self,forward):
        context=multiprocessing.get_context('spawn')
        parent,child=context.Pipe()
        worker=context.Process(target=hold,args=(str(config.DB_PATH),child,forward))
        worker.start()
        try:
            self.assertTrue(parent.poll(8));self.assertEqual(parent.recv(),'ready')
            db.execute('UPDATE budget_reservations SET lease_until=0')
            budget.recover()
            self.assertEqual(budget.unresolved()[0]['state'],'forwarded' if forward else 'reserved')
            worker.kill();worker.join(8)
            self.assertFalse(worker.is_alive())
            budget.recover()
            self.assertEqual(budget.unresolved()[0]['state'],'unknown') if forward else self.assertEqual(budget.unresolved(),[])
        finally:
            if worker.is_alive():worker.kill();worker.join(8)
            parent.close();child.close()
    def test_sigkill_after_forward_is_frozen(self):self.exercise_kill(True)
    def test_sigkill_before_forward_is_released(self):self.exercise_kill(False)
